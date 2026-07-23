from app.profanity_filter.service import (
    bleep_filter, find_all_matches, find_matches, mask_transcript, mask_word, mask_words,
    mute_filter, normalize,
)


def test_normalization_handles_case_and_punctuation():
    assert normalize("БЛЯТЬ!") == "блять"
    assert normalize("f*ck") == "fck"


def test_masks_only_complete_detected_words_and_builds_timed_filter():
    words = [
        {"word": "Well", "start": 1.0, "end": 1.2},
        {"word": "shit!", "start": 1.3, "end": 1.7},
        {"word": "shiitake", "start": 1.8, "end": 2.2},
    ]
    matches = find_matches(words, "en", padding=0.1)
    assert len(matches) == 1
    assert matches[0].masked == "s**t!"
    assert mask_transcript("Well shit! shiitake", matches) == "Well s**t! shiitake"
    assert "between(t,0.200,0.800)" in (mute_filter(matches, clip_start=1.0) or "")
    assert mask_word("ab") == "**"


def test_russian_inflections_are_masked_and_receive_bleeps():
    words = [
        {"word": "Охуевший", "start": 10.0, "end": 10.5},
        {"word": "пиздец!", "start": 10.6, "end": 11.0},
        {"word": "художник", "start": 11.1, "end": 11.5},
        {"word": "себя", "start": 11.6, "end": 11.9},
        {"word": "выебать", "start": 12.0, "end": 12.4},
    ]
    matches = find_matches(words, "ru", padding=0.1)
    assert [match.word for match in matches] == ["Охуевший", "пиздец!", "выебать"]
    graph = bleep_filter(matches, clip_start=10.0, clip_duration=2.0) or ""
    assert "sine=frequency=1000" in graph
    assert "[aout]" in graph


def test_masks_edited_subtitle_words_and_extended_russian_terms():
    words = [
        {"word": "пидорас!", "start": 2.0, "end": 2.4},
        {"word": "Ёпт", "start": 2.5, "end": 2.8},
        {"word": "обычный", "start": 2.9, "end": 3.2},
    ]
    matches = find_all_matches(words)
    censored = mask_words(words, matches)
    assert [word["word"] for word in censored] == ["п*****с!", "Ё*т", "обычный"]
    assert len(matches) == 2
