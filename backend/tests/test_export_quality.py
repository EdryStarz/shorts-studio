from pathlib import Path
from types import SimpleNamespace

import app.export_pipeline.service as render_service


def test_render_uses_constant_rate_high_quality_bt709_profile(monkeypatch, tmp_path: Path):
    commands = []
    monkeypatch.setattr(
        render_service, "get_settings", lambda: SimpleNamespace(
            ffmpeg_bin="ffmpeg", export_max_webcam_upscale=4.0,
            export_prefer_source_fps=True, export_crf=16,
        ),
    )
    monkeypatch.setattr(render_service, "find_focus_x", lambda *_args: 0.5)
    monkeypatch.setattr(render_service, "find_webcam_focus", lambda *_args: (0.86, 0.78))
    monkeypatch.setattr(render_service, "run_media_command", lambda args: commands.append(args))

    report = {}
    result = render_service.render_vertical_clip(
        source=tmp_path / "source.mp4", destination=tmp_path / "short.mp4", subtitle_path=None,
        start=1, end=31, source_width=1280, source_height=720, source_fps=60,
        profanity_matches=[], mode="split", has_audio=True, quality_report=report,
    )

    assert result.name == "short.mp4"
    command = commands[0]
    assert command[command.index("-preset") + 1] == "medium"
    assert command[command.index("-crf") + 1] == "16"
    assert command[command.index("-fps_mode") + 1] == "cfr"
    assert command[command.index("-r") + 1] == "60"
    assert command[command.index("-color_primaries") + 1] == "bt709"
    assert command[command.index("-b:a") + 1] == "256k"
    assert command[command.index("-movflags") + 1] == "+faststart+write_colr"
    assert report["webcam_upscale"] <= 4.0
    assert report["webcam_quality_mode"] == "focused"
