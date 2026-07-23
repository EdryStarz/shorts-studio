import bisect
import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from app.content_filter.service import (
    AD_MARKERS, CTA_MARKERS, blocked_ranges, contains_non_content, normalize,
    split_content_range,
)
from app.multimodal_analysis.service import MomentSignal, reaction_ranges, summarize
from app.scene_detection.service import Scene
from app.transcription.service import Segment


SETUP_MARKERS = (
    "голосов", "аудиозапис", "сообщен", "включ", "послуш", "сравним",
    "смотрим", "покажу", "запись разговора", "видео с ним",
)
REACTION_MARKERS = (
    "что это", "что за", "бред", "непонят", "похож", "невозможно",
    "серьезно", "жесть", "офиг", "оху", "пизд", "ебан", "ебат",
    "ебну", "еб твою", "смешн",
)
OPENING_MARKERS = (
    "короче", "давайте", "сейчас", "здесь у нас", "смотрите", "так вот",
    "и дальше", "вот что", "история",
)

VIRAL_TEXT_MARKERS = (
    "почему", "что произошло", "сравним", "по годам", "впервые", "неожидан",
    "скандал", "конфликт", "запрет", "блокиров", "реакц", "шок", "бред",
    "утечк", "голосов", "показал", "ответил", "заявил", "выяснилось",
)
CONTEXT_DEBT_MARKERS = (
    "как я уже говорил", "об этом потом", "вот это вот", "там короче", "ну это",
    "продолжим", "возвращаясь", "как вы помните", "без контекста",
)

_EMBEDDED_CONFIG = {
    "schema_version": "shorts-studio-virality-v1",
    "source_filter": {
        "minimum_score": 0.44,
        "minimum_speech_coverage": 0.08,
        "semantic_core_sentences": 12,
        "weights": {
            "metadata": 0.16, "semantic_density": 0.28, "reaction_density": 0.20,
            "topic_specificity": 0.16, "speech_coverage": 0.12,
            "lexical_novelty": 0.08,
        },
        "penalties": {"advertisement_or_outro": 0.18, "context_debt": 0.12},
    },
    "scoring": {
        "hook": 0.24, "payoff": 0.20, "completeness": 0.16,
        "streamer_reaction": 0.12, "audio_contrast": 0.10,
        "visual_progression": 0.08, "novelty": 0.06, "boundary_quality": 0.04,
        "penalties": {"cutoff_penalty": 0.28, "context_debt": 0.12, "ad_or_outro": 0.10},
    },
    "scheduler": {
        "model": "contextual_thompson_sampling", "timezone": "Europe/Moscow",
        "minimum_posts_per_day": 4, "maximum_posts_per_day": 7,
        "minimum_gap_minutes": 150, "exploration_rate": 0.2,
        "exploit_slots": ["00:30", "03:00", "18:30"],
        "explore_slots": ["12:30", "21:30"],
        "prevent_adjacent_topic_duplicates": True,
    },
    "channel_watcher": {
        "enabled": True,
        "channel_url": "https://www.youtube.com/@example/videos",
        "poll_interval_seconds": 1800,
        "lookback_videos": 12,
        "max_videos_per_cycle": 1,
        "minimum_metadata_score": 0.20,
        "browser": "edge",
        "process_latest_on_first_run": True,
    },
    "portfolio": {
        "clips_per_source": {"rejected": 0, "ordinary": 3, "strong": 5, "exceptional": 6},
        "strong_threshold": 0.56,
        "exceptional_threshold": 0.70,
        "maximum_clips_from_one_source_per_day": 2,
    },
}


@lru_cache(maxsize=1)
def load_virality_config() -> dict:
    """Load the versioned research profile, with an embedded PyInstaller-safe fallback."""
    path = Path(__file__).resolve().parents[2] / "config" / "shorts-studio-virality-v1.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") == "shorts-studio-virality-v1":
            return value
    except (OSError, ValueError, TypeError):
        pass
    return _EMBEDDED_CONFIG


@dataclass(frozen=True)
class SourceViralityAssessment:
    accepted: bool
    score: float
    threshold: float
    semantic_core: tuple[str, ...]
    details: dict[str, float]


@dataclass(frozen=True)
class MetadataViralityAssessment:
    accepted: bool
    score: float
    threshold: float
    details: dict[str, float]


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _sentence_interest(text: str) -> float:
    normalized = normalize(text)
    marker_score = min(1.0, sum(marker in normalized for marker in VIRAL_TEXT_MARKERS) / 2.0)
    named_or_numbered = bool(re.search(r"\b\d{2,4}\b|\b[A-ZА-ЯЁ][a-zа-яё]{3,}", text))
    punctuation = min(1.0, text.count("?") + text.count("!"))
    return _clamp(0.62 * marker_score + 0.23 * named_or_numbered + 0.15 * punctuation)


def assess_video_metadata(
    title: str, description: str = "", tags: tuple[str, ...] = (),
    duration: float | None = None, live_status: str = "not_live",
    threshold: float | None = None,
) -> MetadataViralityAssessment:
    """Cheap first-pass filter used before a newly discovered video is downloaded."""
    config = load_virality_config()["channel_watcher"]
    threshold = float(config["minimum_metadata_score"] if threshold is None else threshold)
    combined = normalize(f"{title} {description} {' '.join(tags)}")
    topic_markers = (*VIRAL_TEXT_MARKERS, *REACTION_MARKERS, "путин", "дуров", "киевстонер")
    topic_signal = min(1.0, sum(marker in combined for marker in topic_markers) / 3.0)
    specific_words = set(re.findall(r"\b[a-zа-я0-9]{6,}\b", combined))
    specificity = min(1.0, len(specific_words) / 10.0)
    duration_fit = 0.0
    if duration is not None:
        if 20 * 60 <= duration <= 8 * 60 * 60:
            duration_fit = 1.0
        elif 5 * 60 <= duration <= 12 * 60 * 60:
            duration_fit = 0.55
    details = {
        "title_interest": _sentence_interest(title),
        "description_interest": _sentence_interest(description[:1000]),
        "topic_signal": topic_signal,
        "specificity": specificity,
        "duration_fit": duration_fit,
    }
    score = _clamp(
        0.36 * details["title_interest"]
        + 0.14 * details["description_interest"]
        + 0.22 * details["topic_signal"]
        + 0.13 * details["specificity"]
        + 0.15 * details["duration_fit"]
    )
    accepted = score >= threshold and live_status not in {"is_live", "is_upcoming"}
    return MetadataViralityAssessment(
        accepted, round(score, 4), threshold,
        {key: round(value, 4) for key, value in details.items()},
    )


def assess_source_virality(
    metadata_text: str, segments: list[Segment], duration: float,
    threshold: float | None = None,
) -> SourceViralityAssessment:
    """Reject low-potential sources after transcription and before clip generation."""
    config = load_virality_config()["source_filter"]
    threshold = float(config["minimum_score"] if threshold is None else threshold)
    texts = [segment.text.strip() for segment in segments if segment.text.strip()]
    ranked = sorted(texts, key=_sentence_interest, reverse=True)
    semantic_core = tuple(ranked[:int(config["semantic_core_sentences"])])
    combined = normalize(f"{metadata_text} {' '.join(semantic_core)}")
    all_text = normalize(" ".join(texts))
    words = re.findall(r"[a-zа-я0-9]+", all_text)
    content_words = [word for word in words if len(word) >= 4]

    metadata_signal = _sentence_interest(metadata_text)
    semantic_density = (
        sum(_sentence_interest(text) for text in semantic_core) / len(semantic_core)
        if semantic_core else 0.0
    )
    reaction_density = min(
        1.0,
        sum(marker in combined for marker in (*REACTION_MARKERS, *VIRAL_TEXT_MARKERS)) / 5.0,
    )
    topic_specificity = min(
        1.0,
        (len(set(re.findall(r"\b[a-zа-я]{6,}\b", combined))) / max(len(semantic_core), 1)) / 5.0,
    )
    spoken_seconds = sum(max(0.0, segment.end - segment.start) for segment in segments)
    speech_coverage = min(1.0, spoken_seconds / max(duration * 0.45, 1.0))
    lexical_novelty = min(1.0, len(set(content_words)) / max(len(content_words) * 0.42, 1.0))
    non_content = sum(
        any(marker in normalize(text) for marker in (*AD_MARKERS, *CTA_MARKERS))
        for text in texts
    ) / max(len(texts), 1)
    context_debt = sum(marker in all_text for marker in CONTEXT_DEBT_MARKERS) / max(
        len(CONTEXT_DEBT_MARKERS) / 3.0, 1.0,
    )
    details = {
        "metadata": _clamp(metadata_signal),
        "semantic_density": _clamp(semantic_density),
        "reaction_density": _clamp(reaction_density),
        "topic_specificity": _clamp(topic_specificity),
        "speech_coverage": _clamp(speech_coverage),
        "lexical_novelty": _clamp(lexical_novelty),
        "advertisement_or_outro": _clamp(non_content),
        "context_debt": _clamp(context_debt),
    }
    score = sum(float(config["weights"][key]) * details[key] for key in config["weights"])
    score -= sum(
        float(config["penalties"][key]) * details[key] for key in config["penalties"]
    )
    score = _clamp(score)
    accepted = score >= threshold and details["speech_coverage"] >= float(
        config["minimum_speech_coverage"]
    )
    return SourceViralityAssessment(
        accepted, round(score, 4), threshold, semantic_core,
        {key: round(value, 4) for key, value in details.items()},
    )


def source_clip_budget(virality_score: float) -> int:
    """Allocate 0 or 3-6 diverse clips according to the source's virality potential."""
    config = load_virality_config()["portfolio"]
    budgets = config["clips_per_source"]
    if virality_score < float(load_virality_config()["source_filter"]["minimum_score"]):
        return int(budgets["rejected"])
    if virality_score >= float(config["exceptional_threshold"]):
        return int(budgets["exceptional"])
    if virality_score >= float(config["strong_threshold"]):
        return int(budgets["strong"])
    return int(budgets["ordinary"])


@dataclass(frozen=True)
class Candidate:
    start: float
    end: float
    score: float
    details: dict[str, float]
    transcript: str
    words: tuple[dict, ...]


def score_candidate(
    *, duration: float, word_count: int, scene_changes: int, mean_probability: float,
    leading_silence: float, trailing_silence: float,
    boundary_quality: float = 0.5, story_arc: float = 0.0,
    story_completeness: float = 0.0, reaction_peak: float = 0.0,
    cutoff_penalty: float = 0.0, multimodal_interest: float = 0.0,
    turn_structure: float = 0.0, reaction_completion: float = 0.0,
    hook: float | None = None, payoff: float | None = None,
    completeness: float | None = None, streamer_reaction: float | None = None,
    audio_contrast: float | None = None, visual_progression: float | None = None,
    novelty: float = 0.0, context_debt: float = 0.0, ad_or_outro: float = 0.0,
) -> tuple[float, dict[str, float]]:
    if duration <= 0:
        raise ValueError("duration must be positive")
    speech_density = min(1.0, (word_count / duration) / 3.0)
    visual_density = min(1.0, scene_changes / max(duration / 8.0, 1.0))
    audio_confidence = min(1.0, max(0.0, mean_probability))
    pause_penalty = min(1.0, (leading_silence + trailing_silence) / max(duration, 1.0))
    if duration < 18.0:
        length_fit = max(0.0, duration / 18.0)
    elif duration <= 42.0:
        length_fit = 1.0
    elif duration <= 55.0:
        length_fit = max(0.55, 1.0 - (duration - 42.0) / 28.0)
    else:
        length_fit = max(0.1, 0.55 - (duration - 55.0) / 80.0)
    boundary_quality = _clamp(boundary_quality)
    story_arc = _clamp(story_arc)
    story_completeness = _clamp(story_completeness)
    reaction_peak = _clamp(reaction_peak)
    cutoff_penalty = _clamp(cutoff_penalty)
    multimodal_interest = _clamp(multimodal_interest)
    turn_structure = _clamp(turn_structure)
    reaction_completion = _clamp(reaction_completion)
    context_debt = max(_clamp(context_debt), pause_penalty)
    features = {
        "hook": _clamp(
            hook if hook is not None
            else 0.45 * speech_density + 0.30 * audio_confidence + 0.25 * boundary_quality
        ),
        "payoff": _clamp(
            payoff if payoff is not None
            else 0.50 * reaction_peak + 0.30 * story_arc + 0.20 * multimodal_interest
        ),
        "completeness": _clamp(
            completeness if completeness is not None
            else 0.40 * boundary_quality + 0.25 * story_completeness
            + 0.20 * reaction_completion + 0.15 * length_fit
        ),
        "streamer_reaction": _clamp(
            streamer_reaction if streamer_reaction is not None
            else 0.65 * reaction_peak + 0.35 * turn_structure
        ),
        "audio_contrast": _clamp(
            audio_contrast if audio_contrast is not None
            else 0.60 * audio_confidence + 0.40 * speech_density
        ),
        "visual_progression": _clamp(
            visual_progression if visual_progression is not None
            else 0.60 * visual_density + 0.40 * multimodal_interest
        ),
        "novelty": _clamp(novelty),
        "boundary_quality": boundary_quality,
        "cutoff_penalty": cutoff_penalty,
        "context_debt": _clamp(context_debt),
        "ad_or_outro": _clamp(ad_or_outro),
    }
    score = _score_features(features)
    details = {
        "speech_density": round(speech_density, 4),
        "visual_density": round(visual_density, 4),
        "audio_confidence": round(audio_confidence, 4),
        "pause_penalty": round(pause_penalty, 4),
        "length_fit": round(length_fit, 4),
        "boundary_quality": round(boundary_quality, 4),
        "story_arc": round(story_arc, 4),
        "story_completeness": round(story_completeness, 4),
        "reaction_peak": round(reaction_peak, 4),
        "cutoff_penalty": round(cutoff_penalty, 4),
        "multimodal_interest": round(multimodal_interest, 4),
        "turn_structure": round(turn_structure, 4),
        "reaction_completion": round(reaction_completion, 4),
        **{key: round(value, 4) for key, value in features.items()},
    }
    return score, details


def _score_features(features: dict[str, float]) -> float:
    config = load_virality_config()["scoring"]
    positive = sum(
        float(weight) * _clamp(features.get(key, 0.0))
        for key, weight in config.items() if key != "penalties"
    )
    penalty = sum(
        float(weight) * _clamp(features.get(key, 0.0))
        for key, weight in config["penalties"].items()
    )
    return round(100.0 * _clamp(positive - penalty), 2)


def _overlap(a: Candidate, b: Candidate) -> float:
    intersection = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    return intersection / max(1.0, min(a.end - a.start, b.end - b.start))


def _normalized(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def _contains(text: str, markers: tuple[str, ...]) -> bool:
    normalized = _normalized(text)
    return any(_normalized(marker) in normalized for marker in markers)


def _story_arc(segments: list[Segment]) -> float:
    if not segments:
        return 0.0
    setup_indexes = [index for index, segment in enumerate(segments) if _contains(segment.text, SETUP_MARKERS)]
    reaction_indexes = [index for index, segment in enumerate(segments) if _contains(segment.text, REACTION_MARKERS)]
    has_setup = bool(setup_indexes)
    reaction_strength = min(1.0, len(reaction_indexes) / 3.0)
    ordered = any(reaction > setup for setup in setup_indexes for reaction in reaction_indexes)
    return min(1.0, 0.35 * has_setup + 0.40 * reaction_strength + 0.25 * ordered)


def _reaction_peak(segments: list[Segment], start: float, end: float) -> float:
    reactions = [segment for segment in segments if _contains(segment.text, REACTION_MARKERS)]
    if not reactions or end <= start:
        return 0.0
    first = reactions[0]
    position = (first.start - start) / (end - start)
    # A short must show what triggered the reaction before the emotional payoff.
    position_score = max(0.0, 1.0 - abs(position - 0.38) / 0.38)
    has_prelude = min(1.0, max(0.0, (first.start - start) / 12.0))
    has_followthrough = min(1.0, max(0.0, (end - first.end) / 14.0))
    strength = min(1.0, len(reactions) / 3.0)
    return 0.40 * position_score + 0.20 * has_prelude + 0.20 * has_followthrough + 0.20 * strength


def _boundary_quality(segments: list[Segment], all_segments: list[Segment]) -> float:
    if not segments:
        return 0.0
    first = segments[0]
    last = segments[-1]
    first_index = all_segments.index(first)
    last_index = all_segments.index(last)
    leading_gap = first.start if first_index == 0 else max(0.0, first.start - all_segments[first_index - 1].end)
    trailing_gap = 2.0 if last_index + 1 == len(all_segments) else max(0.0, all_segments[last_index + 1].start - last.end)
    pause_score = min(1.0, (leading_gap + trailing_gap) / 1.8)
    ending = last.text.rstrip()
    ending_score = 1.0 if ending.endswith((".", "!", "?", "…")) else 0.35
    opening_score = 1.0 if _contains(first.text, (*SETUP_MARKERS, *OPENING_MARKERS)) else 0.55
    return 0.40 * pause_score + 0.35 * ending_score + 0.25 * opening_score


def _is_natural_end(index: int, segments: list[Segment]) -> bool:
    ending = segments[index].text.rstrip()
    if ending.endswith((".", "!", "?", "…")):
        return True
    if index + 1 == len(segments):
        return True
    return segments[index + 1].start - segments[index].end >= 0.45


def _complete_range(
    start: float, end: float, segments: list[Segment], min_seconds: float, max_seconds: float,
) -> tuple[float, float]:
    """Move an approximate end to a nearby sentence or pause boundary."""
    if not segments:
        return start, end
    end_values = [segment.end for segment in segments]
    index = min(len(segments) - 1, bisect.bisect_left(end_values, end))
    if _is_natural_end(index, segments):
        return start, min(end, segments[index].end)
    for next_index in range(index + 1, len(segments)):
        candidate_end = segments[next_index].end
        if candidate_end - end > 12.0 or candidate_end - start > max_seconds:
            break
        if _is_natural_end(next_index, segments):
            return start, candidate_end
    for previous_index in range(index - 1, -1, -1):
        candidate_end = segments[previous_index].end
        if end - candidate_end > 8.0 or candidate_end - start < min_seconds:
            break
        if _is_natural_end(previous_index, segments):
            return start, candidate_end
    return start, end


def _cutoff_penalty(selected: list[Segment], all_segments: list[Segment]) -> float:
    if not selected:
        return 0.0
    last_index = all_segments.index(selected[-1])
    return 0.0 if _is_natural_end(last_index, all_segments) else 1.0


def _candidate_hook(
    selected: list[Segment], start: float, first_word: float, signal_hook: float,
) -> float:
    opening = " ".join(
        segment.text for segment in selected if segment.start < start + 2.5
    )
    normalized = normalize(opening)
    trigger = min(
        1.0,
        sum(marker in normalized for marker in (*VIRAL_TEXT_MARKERS, *REACTION_MARKERS)) / 2.0,
    )
    immediacy = _clamp(1.0 - max(0.0, first_word - start) / 2.5)
    specificity = float(bool(re.search(r"\b\d{2,4}\b|\b[A-ZА-ЯЁ][a-zа-яё]{3,}", opening)))
    return _clamp(0.36 * trigger + 0.29 * immediacy + 0.15 * specificity + 0.20 * signal_hook)


def _candidate_novelty(selected: list[Segment]) -> float:
    words = re.findall(r"[a-zа-я0-9]+", normalize(" ".join(item.text for item in selected)))
    content = [word for word in words if len(word) >= 4]
    if not content:
        return 0.0
    lexical = len(set(content)) / len(content)
    triggers = sum(marker in " ".join(content) for marker in VIRAL_TEXT_MARKERS)
    return _clamp(0.70 * lexical / 0.55 + 0.30 * min(1.0, triggers / 2.0))


def _candidate_context_debt(selected: list[Segment], start: float) -> float:
    if not selected:
        return 1.0
    opening = normalize(" ".join(segment.text for segment in selected[:2]))
    marker_debt = min(1.0, sum(marker in opening for marker in CONTEXT_DEBT_MARKERS) / 2.0)
    mid_sentence = float(selected[0].start < start - 0.05 or opening.startswith(("и ", "но ", "а ")))
    vague_pronouns = len(re.findall(r"\b(?:это|этот|там|такой|он|она|они)\b", opening))
    return _clamp(0.50 * marker_debt + 0.30 * mid_sentence + 0.20 * min(1.0, vague_pronouns / 4.0))


def _story_ranges(segments: list[Segment], min_seconds: float, max_seconds: float) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    for setup_index, setup in enumerate(segments):
        if not _contains(setup.text, SETUP_MARKERS):
            continue
        reactions: list[int] = []
        last_reaction_end: float | None = None
        for index in range(setup_index + 1, len(segments)):
            segment = segments[index]
            if segment.end - setup.start > max_seconds:
                break
            if _contains(segment.text, REACTION_MARKERS):
                if last_reaction_end is not None and segment.start - last_reaction_end > 45.0:
                    break
                reactions.append(index)
                last_reaction_end = segment.end
        if not reactions:
            continue
        end_index = min(len(segments) - 1, reactions[-1] + 2)
        start_index = setup_index
        if setup_index and setup.start - segments[setup_index - 1].start <= 6.0:
            start_index -= 1
        start = segments[start_index].start
        end = min(segments[end_index].end, start + max_seconds)
        if min_seconds <= end - start <= max_seconds:
            ranges.append((start, end))
    canonical: list[tuple[float, float]] = []
    for start, end in sorted(ranges, key=lambda item: (item[1], item[0])):
        if any(existing_start <= start and abs(existing_end - end) <= 8.0
               for existing_start, existing_end in canonical):
            continue
        canonical.append((start, end))
    return canonical


def build_candidates(
    segments: list[Segment], scenes: list[Scene], total_duration: float,
    min_seconds: float = 15.0, max_seconds: float = 60.0, limit: int = 12,
    semantic_scorer: Callable[[list[str]], list[float]] | None = None,
    signals: list[MomentSignal] | None = None,
) -> list[Candidate]:
    if total_duration < min_seconds:
        return []
    ordered = sorted({0.0, total_duration, *(segment.end for segment in segments)})
    raw: list[Candidate] = []
    anchor_values = {0.0, *(segment.start for segment in segments)}
    for segment in segments:
        if segment.end - segment.start > max_seconds:
            anchor_values.update(
                segment.start + offset
                for offset in range(int(max_seconds), int(segment.end - segment.start), int(max_seconds))
            )
    anchors = sorted(anchor_values)
    protected_anchors = {
        segments[max(0, index - 1)].start
        for index, segment in enumerate(segments)
        if _contains(segment.text, SETUP_MARKERS)
    }
    if len(anchors) > 600:
        stride = math.ceil(len(anchors) / 600)
        anchors = sorted({*anchors[::stride], *protected_anchors})
    complete_story_ranges = {
        (start, end) for start, end in _story_ranges(segments, min_seconds, max_seconds)
    }
    ranges = set(complete_story_ranges)
    signal_ranges = set(reaction_ranges(signals or [], min_seconds, max_seconds))
    ranges.update(signal_ranges)
    # Reaction-centred ranges retain the key stimulus immediately before the
    # payoff, instead of beginning after the interesting source audio/video.
    starts = [segment.start for segment in segments]
    ends = [segment.end for segment in segments]
    for segment in segments:
        if not _contains(segment.text, REACTION_MARKERS):
            continue
        start_index = max(0, bisect.bisect_right(starts, segment.start - 18.0) - 1)
        end_index = min(len(segments) - 1, bisect.bisect_left(ends, segment.end + 30.0))
        start, end = segments[start_index].start, segments[end_index].end
        if end - start > max_seconds:
            end_index = max(start_index, bisect.bisect_right(ends, start + max_seconds) - 1)
            end = segments[end_index].end
        if min_seconds <= end - start <= max_seconds:
            ranges.add((start, end))
    duration_targets = sorted({
        value for value in (min_seconds, 20.0, 28.0, 36.0, 45.0, max_seconds)
        if min_seconds <= value <= max_seconds
    })
    for start in anchors:
        added_for_start = False
        end_indexes = {bisect.bisect_left(ordered, start + target) for target in duration_targets}
        end_indexes.add(bisect.bisect_right(ordered, start + max_seconds) - 1)
        for end_index in end_indexes:
            if end_index < 0 or end_index >= len(ordered):
                continue
            end = ordered[end_index]
            if min_seconds <= end - start <= max_seconds:
                ranges.add((start, end))
                added_for_start = True
        if not added_for_start and start + min_seconds <= total_duration:
            ranges.add((start, min(total_duration, start + max_seconds)))
    ranges = {
        _complete_range(start, end, segments, min_seconds, max_seconds)
        for start, end in ranges
    }
    blocked = blocked_ranges(segments, total_duration)
    ranges = {
        piece
        for start, end in ranges
        for piece in split_content_range(start, end, blocked, min_seconds)
        if min_seconds <= piece[1] - piece[0] <= max_seconds
    }
    for start, end in sorted(ranges):
        duration = end - start
        if not min_seconds <= duration <= max_seconds:
            continue
        selected = [segment for segment in segments if segment.end > start and segment.start < end]
        words = tuple(
            {"start": word.start, "end": word.end, "word": word.word, "probability": word.probability}
            for segment in selected for word in segment.words if word.end > start and word.start < end
        )
        scene_changes = sum(1 for scene in scenes if start < scene.start < end)
        probabilities = [float(word["probability"]) for word in words]
        first_word = float(words[0]["start"]) if words else end
        last_word = float(words[-1]["end"]) if words else start
        story_arc = _story_arc(selected)
        reaction_peak = _reaction_peak(selected, start, end)
        story_completeness = 1.0 if (start, end) in complete_story_ranges else 0.0
        boundary_quality = _boundary_quality(selected, segments)
        cutoff_penalty = _cutoff_penalty(selected, segments)
        signal_details = summarize(signals or [], start, end)
        reaction_peak = max(reaction_peak, signal_details["multimodal_interest"])
        story_arc = max(
            story_arc,
            0.65 * signal_details["turn_structure"]
            + 0.35 * signal_details["reaction_completion"],
        )
        if signal_details["turn_structure"] >= 0.5:
            story_completeness = max(
                story_completeness,
                0.55 * signal_details["turn_structure"]
                + 0.45 * signal_details["reaction_completion"],
            )
        boundary_quality = max(
            0.0,
            min(1.0, 0.78 * boundary_quality + 0.22 * signal_details["reaction_completion"]),
        )
        if signal_details["active_end"] >= 0.58 and signal_details["reaction_completion"] < 0.55:
            cutoff_penalty = max(cutoff_penalty, signal_details["active_end"])
        hook = _candidate_hook(selected, start, first_word, signal_details["hook"])
        payoff = max(
            signal_details["payoff"],
            0.55 * reaction_peak + 0.25 * story_arc + 0.20 * story_completeness,
        )
        completeness = max(
            story_completeness,
            0.45 * boundary_quality + 0.35 * signal_details["reaction_completion"]
            + 0.20 * float(_is_natural_end(segments.index(selected[-1]), segments)),
        ) if selected else 0.0
        streamer_reaction = max(
            signal_details["streamer_reaction"],
            0.65 * reaction_peak + 0.35 * signal_details["turn_structure"],
        )
        audio_contrast = signal_details["audio_contrast"]
        visual_progression = max(
            signal_details["visual_progression"],
            min(1.0, scene_changes / max(duration / 5.0, 1.0)),
        )
        novelty = _candidate_novelty(selected)
        context_debt = _candidate_context_debt(selected, start)
        ad_or_outro = float(any(contains_non_content(segment.text) for segment in selected))
        score, details = score_candidate(
            duration=duration,
            word_count=len(words),
            scene_changes=scene_changes,
            mean_probability=sum(probabilities) / len(probabilities) if probabilities else 0.0,
            leading_silence=max(0.0, first_word - start),
            trailing_silence=max(0.0, end - last_word),
            boundary_quality=boundary_quality,
            story_arc=story_arc,
            story_completeness=story_completeness,
            reaction_peak=reaction_peak,
            cutoff_penalty=cutoff_penalty,
            multimodal_interest=signal_details["multimodal_interest"],
            turn_structure=signal_details["turn_structure"],
            reaction_completion=signal_details["reaction_completion"],
            hook=hook, payoff=payoff, completeness=completeness,
            streamer_reaction=streamer_reaction, audio_contrast=audio_contrast,
            visual_progression=visual_progression, novelty=novelty,
            context_debt=context_debt, ad_or_outro=ad_or_outro,
        )
        raw.append(Candidate(start, end, score, details, " ".join(s.text for s in selected), words))
    if not raw:
        window = min(max_seconds, total_duration)
        for start in range(0, int(total_duration - min_seconds) + 1, max(1, int(window))):
            end = min(total_duration, start + window)
            score, details = score_candidate(
                duration=end - start, word_count=0,
                scene_changes=sum(1 for scene in scenes if start < scene.start < end),
                mean_probability=0, leading_silence=0, trailing_silence=0,
            )
            raw.append(Candidate(float(start), float(end), score, details, "", ()))
    if semantic_scorer:
        ranked = sorted(
            (candidate for candidate in raw if candidate.transcript.strip()),
            key=lambda item: item.score,
            reverse=True,
        )
        story_pool = sorted(ranked, key=lambda item: item.details.get("story_arc", 0.0), reverse=True)[:64]
        pool_by_range = {(candidate.start, candidate.end): candidate for candidate in [*story_pool, *ranked[:160]]}
        pool = list(pool_by_range.values())
        semantic_scores = semantic_scorer([candidate.transcript for candidate in pool])
        replacements: dict[tuple[float, float], Candidate] = {}
        for candidate, semantic_score in zip(pool, semantic_scores, strict=False):
            semantic_score = max(0.0, min(1.0, float(semantic_score)))
            details = {
                **candidate.details,
                "semantic_interest": round(semantic_score, 4),
                "novelty": round(max(candidate.details.get("novelty", 0.0), semantic_score), 4),
            }
            adjusted_score = _score_features(details)
            replacements[(candidate.start, candidate.end)] = Candidate(
                candidate.start, candidate.end, adjusted_score, details,
                candidate.transcript, candidate.words,
            )
        # Only the strongest heuristic pool is semantically evaluated. Keeping
        # unevaluated candidates at their original 0..100 scale would let them
        # outrank the blended scores and silently disable semantic selection.
        if replacements:
            raw = list(replacements.values())

    chosen: list[Candidate] = []
    for candidate in sorted(raw, key=lambda item: item.score, reverse=True):
        if all(_overlap(candidate, existing) < 0.35 for existing in chosen):
            chosen.append(candidate)
        if len(chosen) >= limit:
            break
    return sorted(chosen, key=lambda item: item.start)
