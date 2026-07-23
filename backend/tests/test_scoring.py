from app.clip_scoring.service import (
    _complete_range, assess_source_virality, assess_video_metadata, build_candidates,
    score_candidate, source_clip_budget,
)
from app.scene_detection.service import Scene
from app.transcription.service import Segment, Word


def test_dense_dynamic_fragment_scores_above_silent_fragment():
    strong, _ = score_candidate(
        duration=30, word_count=75, scene_changes=5, mean_probability=0.92,
        leading_silence=0.2, trailing_silence=0.2,
    )
    weak, _ = score_candidate(
        duration=30, word_count=3, scene_changes=0, mean_probability=0.4,
        leading_silence=8, trailing_silence=8,
    )
    assert strong > weak
    assert 0 <= weak <= 100
    assert 0 <= strong <= 100


def test_score_uses_the_configured_research_formula_exactly():
    score, details = score_candidate(
        duration=30, word_count=60, scene_changes=5, mean_probability=0.9,
        leading_silence=0, trailing_silence=0,
        hook=0.8, payoff=0.7, completeness=0.9, streamer_reaction=0.6,
        audio_contrast=0.5, visual_progression=0.4, novelty=0.3,
        boundary_quality=0.8, cutoff_penalty=0.1, context_debt=0.2,
        ad_or_outro=0.0,
    )
    expected = 100 * (
        0.24 * 0.8 + 0.20 * 0.7 + 0.16 * 0.9 + 0.12 * 0.6
        + 0.10 * 0.5 + 0.08 * 0.4 + 0.06 * 0.3 + 0.04 * 0.8
        - 0.28 * 0.1 - 0.12 * 0.2
    )
    assert score == round(expected, 2)
    assert details["hook"] == 0.8


def test_source_budget_rejects_weak_videos_and_produces_three_to_six_clips():
    assert source_clip_budget(0.43) == 0
    assert source_clip_budget(0.45) == 3
    assert source_clip_budget(0.56) == 5
    assert source_clip_budget(0.70) == 6
    assert source_clip_budget(0.98) == 6


def test_metadata_filter_skips_live_and_low_interest_videos_before_download():
    accepted = assess_video_metadata(
        "Хесус реагирует на скандал: полный разбор и голосовое",
        "Почему это заявление вызвало реакцию — смотрим вместе.",
        ("реакция", "новости"), 3600,
    )
    rejected = assess_video_metadata("стрим", "", (), 60, "is_live")
    assert accepted.accepted is True
    assert accepted.score >= accepted.threshold
    assert rejected.accepted is False


def test_source_filter_rejects_filler_and_accepts_a_specific_reaction_stream():
    strong_texts = [
        "Путин неожиданно ответил на вопрос, и сейчас сравним его реакцию по годам!",
        "Почему это заявление вызвало скандал? Хесус показывает полную запись.",
        "Что произошло дальше? Вот кульминация и сильная реакция стримера.",
    ] * 4
    strong = [
        Segment(index * 8.0, index * 8.0 + 7.0, text, ())
        for index, text in enumerate(strong_texts)
    ]
    weak = [
        Segment(index * 8.0, index * 8.0 + 7.0, "Ну вот, продолжаем сидеть дальше.", ())
        for index in range(12)
    ]
    accepted = assess_source_virality("Хесус обсуждает важное заявление", strong, 100.0)
    rejected = assess_source_virality("Обычный стрим", weak, 100.0)
    assert accepted.accepted is True
    assert rejected.accepted is False
    assert accepted.score > rejected.score


def test_candidates_respect_duration_and_overlap_constraints():
    words = tuple(Word(float(i), float(i) + 0.4, f"w{i}", 0.95) for i in range(120))
    segments = [Segment(0, 120, "speech", words)]
    scenes = [Scene(float(i), float(i + 10)) for i in range(0, 120, 10)]
    candidates = build_candidates(segments, scenes, 120, min_seconds=15, max_seconds=60, limit=4)
    assert 1 <= len(candidates) <= 4
    assert all(15 <= item.end - item.start <= 60 for item in candidates)


def test_semantic_interest_is_added_to_candidate_details():
    words = tuple(Word(float(i), float(i) + 0.4, f"слово{i}", 0.95) for i in range(90))
    segments = [Segment(0, 90, "эмоциональная реакция", words)]
    scenes = [Scene(float(i), float(i + 10)) for i in range(0, 90, 10)]

    def semantic_scorer(texts: list[str]) -> list[float]:
        return [0.9 for _ in texts]

    candidates = build_candidates(
        segments, scenes, 90, min_seconds=15, max_seconds=60, limit=3,
        semantic_scorer=semantic_scorer,
    )
    assert candidates
    assert all(item.details["semantic_interest"] == 0.9 for item in candidates)


def test_unscored_candidates_cannot_displace_semantic_pool():
    words = tuple(Word(float(i), float(i) + 0.4, f"слово{i}", 0.95) for i in range(1000))
    segments = [Segment(0, 1000, "длинный стрим", words)]
    scenes = [Scene(float(i), float(i + 5)) for i in range(0, 1000, 5)]

    candidates = build_candidates(
        segments, scenes, 1000, min_seconds=15, max_seconds=60, limit=12,
        semantic_scorer=lambda texts: [0.8 for _ in texts],
    )
    assert len(candidates) == 12
    assert all(item.details["semantic_interest"] == 0.8 for item in candidates)


def test_story_candidate_keeps_voice_message_and_full_reaction():
    texts = [
        "И дальше здесь у нас аудиозапись и голосовое сообщение.",
        "Когда к нему сядут в машину, вот что надо сказать.",
        "Короче, я так понял, никто не сел, пацанам скажу.",
        "Полчаса ждём и, наверное, давай разъезжаться.",
        "Что это? Бред какой-то.",
        "Голос немного похож, но так может говорить много людей.",
        "Что это за диалог вообще? Куда разъезжаться?",
        "Здесь всё непонятно, контекста нет.",
        "Почему нельзя выложить полностью все голосовые сообщения?",
        "Из этой части сделать выводы невозможно.",
        "Ничего нет, но обвинений очень много.",
        "Теперь перейдём к другой теме.",
    ]
    segments = []
    for index, text in enumerate(texts):
        start = index * 12.0
        words = (Word(start, start + 1.0, text.split()[0], 0.95),)
        segments.append(Segment(start, start + 10.0, text, words))

    candidates = build_candidates(
        segments, [], 144.0, min_seconds=15, max_seconds=180, limit=1,
        semantic_scorer=lambda texts: [0.9 for _ in texts],
    )
    assert len(candidates) == 1
    assert candidates[0].start == 0.0
    assert candidates[0].end >= 130.0
    assert candidates[0].details["story_arc"] == 1.0


def test_short_candidate_keeps_trigger_before_reaction():
    texts = [
        "Обычная подводка к теме.",
        "Вот ключевая фраза из записи, которую сейчас обсуждают.",
        "Никто не приехал, полчаса ждём и будем разъезжаться.",
        "Что это за бред? Куда разъезжаться вообще?",
        "Это невозможно понять без полного контекста.",
        "Ничего не доказано, а обвинений очень много.",
    ]
    segments = []
    for index, text in enumerate(texts):
        start = index * 8.0
        words = tuple(
            Word(start + word_index * 0.4, start + word_index * 0.4 + 0.3, word, 0.95)
            for word_index, word in enumerate(text.split())
        )
        segments.append(Segment(start, start + 7.0, text, words))

    candidates = build_candidates(segments, [], 48.0, 12.0, 55.0, limit=1)
    assert candidates
    assert candidates[0].start <= 16.0
    assert candidates[0].end >= 39.0
    assert candidates[0].details["reaction_peak"] > 0.7


def test_approximate_end_extends_to_sentence_boundary():
    segments = [
        Segment(0.0, 7.0, "Начало мысли", ()),
        Segment(7.0, 14.0, "которая ещё продолжается", ()),
        Segment(14.0, 21.0, "и здесь наконец заканчивается.", ()),
        Segment(22.0, 28.0, "Новая тема.", ()),
    ]
    assert _complete_range(0.0, 14.0, segments, 10.0, 30.0) == (0.0, 21.0)
