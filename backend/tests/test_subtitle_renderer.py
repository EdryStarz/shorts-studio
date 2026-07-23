from app.subtitle_renderer.service import _balanced_line_break, _chunks, write_ass


def test_subtitle_chunks_are_short_and_split_on_pauses_and_sentences():
    words = [
        {"start": 0.0, "end": 0.3, "word": "Это"},
        {"start": 0.35, "end": 0.7, "word": "важная"},
        {"start": 0.75, "end": 1.1, "word": "новость."},
        {"start": 2.0, "end": 2.3, "word": "Вот"},
        {"start": 2.35, "end": 2.7, "word": "моя"},
        {"start": 2.75, "end": 3.1, "word": "реакция"},
        {"start": 3.15, "end": 3.5, "word": "на"},
        {"start": 3.55, "end": 3.9, "word": "неё"},
    ]
    chunks = _chunks(words)
    assert [len(chunk) for chunk in chunks] == [3, 3, 2]
    assert all(len(chunk) <= 3 for chunk in chunks)


def test_long_caption_is_balanced_and_kept_inside_safe_style(tmp_path):
    words = [
        {"start": 0.0, "end": 0.3, "word": "невероятно"},
        {"start": 0.35, "end": 0.7, "word": "важная"},
        {"start": 0.75, "end": 1.1, "word": "реакция"},
    ]
    assert _balanced_line_break(words) in {1, 2}
    content = write_ass(words, tmp_path / "captions.ass").read_text(encoding="utf-8-sig")
    assert "Arial,54" in content
    assert ",110,110,90,1" in content
    assert r"\N" in content


def test_ass_timestamps_are_relative_to_clip_start(tmp_path):
    words = [
        {"start": 341.5, "end": 341.9, "word": "Новые"},
        {"start": 342.0, "end": 342.4, "word": "субтитры"},
    ]
    content = write_ass(
        words, tmp_path / "captions.ass", clip_start=341.0,
    ).read_text(encoding="utf-8-sig")
    assert "Dialogue: 0,0:00:00.50,0:00:00.90" in content
    assert "0:05:41" not in content
