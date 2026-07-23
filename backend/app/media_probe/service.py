import json
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.process import run_media_command


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    video_codec: str
    audio_codec: str | None


def _fps(value: str) -> float:
    numerator, _, denominator = value.partition("/")
    return float(numerator) / max(float(denominator or 1), 1)


def probe_media(path: Path) -> MediaInfo:
    settings = get_settings()
    result = run_media_command([
        settings.ffprobe_bin, "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ], timeout=120)
    payload = json.loads(result.stdout)
    video = next((stream for stream in payload["streams"] if stream["codec_type"] == "video"), None)
    if not video:
        raise ValueError("uploaded file has no video stream")
    audio = next((stream for stream in payload["streams"] if stream["codec_type"] == "audio"), None)
    duration = float(payload.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise ValueError("unable to determine a positive video duration")
    return MediaInfo(
        duration=duration,
        width=int(video["width"]),
        height=int(video["height"]),
        fps=_fps(video.get("avg_frame_rate", "0/1")),
        has_audio=audio is not None,
        video_codec=str(video.get("codec_name", "unknown")),
        audio_codec=str(audio["codec_name"]) if audio else None,
    )


def extract_audio(source: Path, destination: Path) -> Path:
    settings = get_settings()
    destination.parent.mkdir(parents=True, exist_ok=True)
    run_media_command([
        settings.ffmpeg_bin, "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(destination),
    ])
    return destination

