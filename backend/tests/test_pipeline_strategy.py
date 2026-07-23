from pathlib import Path

from app.export_pipeline import pipeline


def test_long_source_skips_full_scene_scan(monkeypatch):
    def unexpected_scene_scan(*args, **kwargs):
        raise AssertionError("full scene scan must be skipped for hour-long sources")

    monkeypatch.setattr(pipeline, "detect_scenes", unexpected_scene_scan)
    result = pipeline._scenes(Path("stream.mp4"), 3600.0)

    assert result["scenes"] == []
    assert result["strategy"] == "transcript_and_multimodal_for_long_source"
