import re
from dataclasses import dataclass


DEFAULT_TERMS = {
    "en": {"fuck", "fucking", "shit", "bitch", "asshole", "cunt", "motherfucker"},
    "ru": {
        "блядь", "блять", "бля", "сука", "сучка", "хуй", "пизда", "ебать", "ёб твою",
        "нахуй", "похуй", "охуеть", "ебаный", "ёбаный", "пиздец", "мудак", "долбоёб",
        "пидор", "пидорас", "гандон", "залупа", "манда", "ёпт", "епт",
    },
}

RU_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"^бл(?:я|яд|ят|ять|ядь|яди|яде|ядск|яц).*$",
    r"^сук(?:а|и|у|е|ой|ою|ин|ищ|оч|ан).*$",
    r"^(?:(?:на|по|за|о|а|про|до|не)?ху(?:й|я|е|и|ю|ёв|ев|йн|яр|ищ)).*$",
    r"^(?:пизд|пезд).*$",
    r"^(?:(?:(?:вы|за|на|по|про|пере|до|у)?[её]б)|(?:(?:в|об|от|под|раз|с)ъ[её]б))"
    r"(?:а|у|н|л|ё|и|к|ч|с|ы|т|о|е|я).*$",
    r"^(?:долбо[её]б|мудак|мудил|гандон|говн|залуп|манда).*$",
    r"^(?:пидор|пидар|педерас|пидорас).*$",
    r"^[её]пт.*$",
))
EN_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"^(?:mother)?fuck.*$", r"^shit.*$", r"^bitch.*$", r"^asshole.*$", r"^cunt.*$",
))


@dataclass(frozen=True)
class Match:
    word: str
    masked: str
    start: float
    end: float


def normalize(value: str) -> str:
    return re.sub(r"[^\wа-яё]", "", value.casefold(), flags=re.IGNORECASE)


def _is_profanity(value: str, language: str, terms: set[str]) -> bool:
    normalized = normalize(value)
    if normalized in terms:
        return True
    patterns = RU_PATTERNS if language == "ru" else EN_PATTERNS if language == "en" else ()
    return any(pattern.fullmatch(normalized) for pattern in patterns)


def mask_word(value: str) -> str:
    letters = list(value)
    indexes = [index for index, character in enumerate(letters) if character.isalnum()]
    if len(indexes) <= 2:
        return "*" * len(value)
    for index in indexes[1:-1]:
        letters[index] = "*"
    return "".join(letters)


def find_matches(words: list[dict], language: str = "en", custom_terms: set[str] | None = None,
                 padding: float = 0.08) -> list[Match]:
    terms = {normalize(term) for term in DEFAULT_TERMS.get(language, set()) | (custom_terms or set())}
    matches: list[Match] = []
    for item in words:
        original = str(item["word"])
        if _is_profanity(original, language, terms):
            matches.append(Match(
                word=original,
                masked=mask_word(original),
                start=max(0.0, float(item["start"]) - padding),
                end=float(item["end"]) + padding,
            ))
    return matches


def find_all_matches(words: list[dict], padding: float = 0.08) -> list[Match]:
    """Find Russian and English profanity without returning duplicate intervals."""
    matches = find_matches(words, "ru", padding=padding) + find_matches(
        words, "en", padding=padding,
    )
    unique: dict[tuple[str, float, float], Match] = {}
    for match in matches:
        key = (normalize(match.word), round(match.start, 3), round(match.end, 3))
        unique[key] = match
    return sorted(unique.values(), key=lambda item: (item.start, item.end))


def mask_words(words: list[dict], matches: list[Match]) -> list[dict]:
    """Return subtitle words with every detected profanity match masked."""
    output: list[dict] = []
    for raw_word in words:
        word = dict(raw_word)
        value = str(word.get("word", ""))
        start = float(word.get("start", 0.0))
        end = float(word.get("end", start))
        match = next((
            item for item in matches
            if normalize(item.word) == normalize(value)
            and item.end >= start and item.start <= end
        ), None)
        if match:
            word["word"] = match.masked
        output.append(word)
    return output


def mask_transcript(text: str, matches: list[Match]) -> str:
    output = text
    for match in sorted(matches, key=lambda item: len(item.word), reverse=True):
        output = re.sub(rf"(?<!\w){re.escape(match.word)}(?!\w)", match.masked, output, flags=re.IGNORECASE)
    return output


def mute_filter(matches: list[Match], clip_start: float) -> str | None:
    if not matches:
        return None
    expressions = "+".join(
        f"between(t,{max(0, item.start - clip_start):.3f},{max(0, item.end - clip_start):.3f})"
        for item in matches
    )
    return f"volume=enable='{expressions}':volume=0,loudnorm=I=-16:TP=-1.5:LRA=11"


def bleep_filter(matches: list[Match], clip_start: float, clip_duration: float) -> str | None:
    """Build an FFmpeg audio graph that mutes profanity and overlays short bleeps."""
    intervals: list[tuple[float, float]] = []
    for item in sorted(matches, key=lambda match: match.start):
        start = max(0.0, item.start - clip_start)
        end = min(clip_duration, max(start + 0.04, item.end - clip_start))
        if start >= clip_duration:
            continue
        if intervals and start <= intervals[-1][1] + 0.03:
            intervals[-1] = (intervals[-1][0], max(intervals[-1][1], end))
        else:
            intervals.append((start, end))
    if not intervals:
        return None
    expression = "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in intervals)
    parts = [f"[0:a:0]volume=enable='{expression}':volume=0[clean]"]
    beep_labels: list[str] = []
    for index, (start, end) in enumerate(intervals):
        duration = max(0.04, end - start)
        fade = min(0.015, duration / 4)
        delay = round(start * 1000)
        label = f"beep{index}"
        parts.append(
            f"sine=frequency=1000:sample_rate=48000:duration={duration:.3f},volume=0.16,"
            f"afade=t=in:st=0:d={fade:.3f},"
            f"afade=t=out:st={max(0.0, duration - fade):.3f}:d={fade:.3f},"
            f"adelay={delay}|{delay}[{label}]"
        )
        beep_labels.append(f"[{label}]")
    inputs = "[clean]" + "".join(beep_labels)
    parts.append(
        f"{inputs}amix=inputs={1 + len(beep_labels)}:duration=first:normalize=0,"
        "loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.95[aout]"
    )
    return ";".join(parts)
