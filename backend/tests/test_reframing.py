from pathlib import Path

import pytest

import app.reframing.service as reframing
from app.reframing.service import crop_filter, find_webcam_focus, split_layout


def test_split_layout_uses_news_camera_and_branding():
    result = crop_filter(640, 360, 0.5, "split")
    assert "crop=390:270:112:38" in result
    assert "scale=1080:1100:force_original_aspect_ratio=decrease" in result
    assert "crop=1080:1100" in result
    assert "scale=1080:820" in result
    assert "vstack=inputs=2" in result
    assert "KICK" in result
    assert "JESUSAVGN" in result
    assert "hqdn3d" not in result
    assert "unsharp" not in result


def test_split_layout_fills_the_lower_panel_with_a_face_focused_webcam():
    layout = split_layout(1280, 720, (0.86, 0.78), max_webcam_upscale=4.0)
    assert layout.quality_mode == "focused"
    assert layout.webcam_upscale <= 4.0
    assert "crop=280:212" in layout.filter
    assert "scale=1080:820" in layout.filter


def test_low_resolution_split_still_focuses_the_webcam_at_four_times_upscale():
    layout = split_layout(480, 270, (0.86, 0.78), max_webcam_upscale=4.0)
    assert layout.quality_mode == "focused"
    assert layout.webcam_upscale == 4.0
    assert "crop=270:204" in layout.filter
    assert "scale=1080:820" in layout.filter


def test_missing_bottom_right_webcam_stops_instead_of_selecting_another_face(monkeypatch):
    monkeypatch.setattr(reframing, "_face_centres", lambda *_args, **_kwargs: [])
    with pytest.raises(ValueError, match="нижней правой области"):
        find_webcam_focus(Path("source.mp4"), 0, 30)
