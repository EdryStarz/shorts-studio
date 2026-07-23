from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.process import run_media_command
from app.profanity_filter.service import Match, bleep_filter, mute_filter
from app.reframing.service import crop_filter, find_focus_x, find_webcam_focus, split_layout


def _escape_subtitle_path(path: Path) -> str:
    value = path.resolve().as_posix().replace("'", r"\'")
    if len(value) > 1 and value[1] == ":":
        value = value[0] + r"\:" + value[2:]
    return value


def render_vertical_clip(
    *, source: Path, destination: Path, subtitle_path: Path | None, start: float, end: float,
    source_width: int, source_height: int, profanity_matches: list[Match], mode: str = "crop",
    has_audio: bool = True, source_fps: float | None = None,
    quality_report: dict[str, Any] | None = None,
) -> Path:
    if end <= start:
        raise ValueError("clip end must be after start")
    settings = get_settings()
    destination.parent.mkdir(parents=True, exist_ok=True)
    focus_x = find_focus_x(source, start, end)
    if mode == "split":
        layout = split_layout(
            source_width, source_height, find_webcam_focus(source, start, end),
            settings.export_max_webcam_upscale, getattr(settings, "webcam_region", "bottom_right"),
        )
        video_filter = layout.filter
        if quality_report is not None:
            quality_report.update({
                "layout": "split", "webcam_upscale": layout.webcam_upscale,
                "webcam_quality_mode": layout.quality_mode,
                "webcam_crop": list(layout.webcam_crop),
                "webcam_region": getattr(settings, "webcam_region", "bottom_right"),
            })
    else:
        video_filter = crop_filter(source_width, source_height, focus_x, mode)
        if quality_report is not None:
            quality_report.update({"layout": mode, "webcam_quality_mode": "not_applicable"})
    # Normalize SDR output explicitly. Most streaming sources are BT.709;
    # supplying both input and output declarations prevents an untagged MP4
    # when the source omits color metadata.
    video_filter += ",format=yuv420p,colorspace=all=bt709:iall=bt709:fast=1"
    if subtitle_path and subtitle_path.exists():
        video_filter += f",ass=filename='{_escape_subtitle_path(subtitle_path)}'"
    # Platform transcoders handle a constant frame rate much more consistently
    # than a VFR stream.  Preserve 60fps only for a true high-frame-rate source.
    target_fps = 60 if settings.export_prefer_source_fps and (source_fps or 0) >= 50 else 30
    crf = max(12, min(23, int(settings.export_crf)))
    maxrate = "20M" if target_fps == 60 else "16M"
    if quality_report is not None:
        quality_report.update({
            "resolution": [1080, 1920], "fps": target_fps, "crf": crf,
            "maxrate": maxrate, "color_space": "bt709",
        })
    audio_graph = bleep_filter(profanity_matches, start, end - start) if has_audio else None
    audio_filter = mute_filter(profanity_matches, start) or "loudnorm=I=-16:TP=-1.5:LRA=11"
    args = [
        settings.ffmpeg_bin, "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", str(source),
    ]
    if not has_audio:
        args.extend(["-f", "lavfi", "-t", f"{end - start:.3f}", "-i", "anullsrc=r=48000:cl=stereo"])
    args.extend(["-vf", video_filter])
    if has_audio:
        if audio_graph:
            args.extend(["-filter_complex", audio_graph, "-map", "0:v:0", "-map", "[aout]"])
        else:
            args.extend(["-af", audio_filter])
    else:
        args.extend(["-map", "0:v:0", "-map", "1:a:0", "-shortest"])
    args.extend([
        "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-profile:v", "high",
        "-maxrate", maxrate, "-bufsize", "32M", "-g", str(target_fps * 2),
        "-keyint_min", str(target_fps * 2), "-r", str(target_fps), "-fps_mode", "cfr",
        "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", "-color_range", "tv", "-c:a", "aac", "-b:a", "256k",
        "-ar", "48000",
        "-movflags", "+faststart+write_colr", "-max_muxing_queue_size", "2048", str(destination),
    ])
    run_media_command(args)
    return destination
