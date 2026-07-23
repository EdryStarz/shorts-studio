"""Secure YouTube URL import using an existing local browser session.

The application never writes a cookies.txt file and never stores browser
cookies in its database. yt-dlp reads the selected profile in memory for the
duration of one import.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from app.core.config import get_settings
from app.ingestion.service import safe_filename


VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
}
SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
KICK_HOSTS = {"kick.com", "www.kick.com"}
KICK_VOD_PATH = re.compile(
    r"^/([A-Za-z0-9_-]{1,64})/videos/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?$"
)
SUPPORTED_BROWSERS = {"firefox", "chrome", "edge"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrowserProfile:
    name: str
    available: bool
    recommended: bool
    warning: str | None = None


@dataclass(frozen=True)
class DownloadedVideo:
    path: Path
    original_name: str
    checksum: str
    size_bytes: int
    source_url: str = ""
    title: str = ""
    author: str = ""
    duration: float | None = None


@dataclass(frozen=True)
class YouTubeVideoMetadata:
    video_id: str
    url: str
    title: str
    description: str = ""
    duration: float | None = None
    published_at: datetime | None = None
    live_status: str = "not_live"
    tags: tuple[str, ...] = ()


class _YtDlpLogger:
    def __init__(self) -> None:
        self.last_error = ""

    def debug(self, _message: str) -> None:
        return None

    def info(self, _message: str) -> None:
        return None

    def warning(self, _message: str) -> None:
        return None

    def error(self, message: str) -> None:
        self.last_error = message


def normalize_youtube_url(raw_url: str) -> str:
    """Return a canonical HTTPS watch URL for one YouTube video only."""
    parsed = urlparse(raw_url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("use a complete HTTPS YouTube video link")

    host = parsed.hostname.lower()
    video_id = ""
    if host in SHORT_HOSTS:
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif host in YOUTUBE_HOSTS:
        path_parts = [part for part in parsed.path.split("/") if part]
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
            video_id = path_parts[1]
    else:
        raise ValueError("only YouTube video links are accepted")

    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("the link must point to one YouTube video")
    return f"https://www.youtube.com/watch?v={video_id}"


def normalize_video_url(raw_url: str) -> str:
    """Normalize one supported YouTube or Kick VOD URL without accepting playlists."""
    parsed = urlparse(raw_url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("use a complete HTTPS video link")
    if parsed.hostname.lower() in YOUTUBE_HOSTS | SHORT_HOSTS:
        return normalize_youtube_url(raw_url)
    if parsed.hostname.lower() in KICK_HOSTS:
        match = KICK_VOD_PATH.fullmatch(parsed.path)
        if match:
            channel, video_id = match.groups()
            return f"https://kick.com/{channel}/videos/{video_id.lower()}"
    raise ValueError("only YouTube video links and Kick VOD links are accepted")


def browser_profiles(environ: dict[str, str] | None = None) -> list[BrowserProfile]:
    """Report supported browser profiles without opening cookie databases."""
    env = environ or os.environ
    local = Path(env.get("LOCALAPPDATA", ""))
    roaming = Path(env.get("APPDATA", ""))
    paths = {
        "firefox": roaming / "Mozilla" / "Firefox" / "profiles.ini",
        "chrome": local / "Google" / "Chrome" / "User Data",
        "edge": local / "Microsoft" / "Edge" / "User Data",
    }
    chromium_warning = (
        "On Windows, Chromium cookie decryption may fail. Use Firefox if this happens."
    )
    return [
        BrowserProfile("firefox", paths["firefox"].is_file(), True),
        BrowserProfile("chrome", paths["chrome"].is_dir(), False, chromium_warning),
        BrowserProfile("edge", paths["edge"].is_dir(), False, chromium_warning),
    ]


def _published_at(info: dict[str, Any]) -> datetime | None:
    if info.get("timestamp") is not None:
        return datetime.fromtimestamp(float(info["timestamp"]), timezone.utc)
    upload_date = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", upload_date):
        return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
    return None


def _metadata_from_info(info: dict[str, Any]) -> YouTubeVideoMetadata | None:
    video_id = str(info.get("id") or "").strip()
    if not VIDEO_ID.fullmatch(video_id):
        return None
    duration = info.get("duration")
    return YouTubeVideoMetadata(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=str(info.get("title") or video_id),
        description=str(info.get("description") or ""),
        duration=float(duration) if duration is not None else None,
        published_at=_published_at(info),
        live_status=str(info.get("live_status") or "not_live"),
        tags=tuple(str(tag) for tag in (info.get("tags") or ()) if str(tag).strip()),
    )


def list_channel_videos(
    channel_url: str, limit: int = 12,
    *, ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> list[YouTubeVideoMetadata]:
    """Return the latest public channel entries without downloading media."""
    if ydl_factory is None:
        from yt_dlp import YoutubeDL

        ydl_factory = YoutubeDL
    options = {
        "extract_flat": True,
        "playlistend": max(1, int(limit)),
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "socket_timeout": 30,
    }
    with ydl_factory(options) as ydl:
        result = ydl.extract_info(channel_url, download=False)
    entries = result.get("entries", ()) if isinstance(result, dict) else ()
    return [metadata for item in entries if item and (metadata := _metadata_from_info(item))]


def fetch_youtube_metadata(
    raw_url: str, *, ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> YouTubeVideoMetadata:
    """Fetch full metadata for one discovered video before deciding to download it."""
    normalized_url = normalize_youtube_url(raw_url)
    if ydl_factory is None:
        from yt_dlp import YoutubeDL

        ydl_factory = YoutubeDL
    options = {
        "skip_download": True, "noplaylist": True, "quiet": True,
        "no_warnings": True, "cachedir": False, "socket_timeout": 30,
    }
    with ydl_factory(options) as ydl:
        info = ydl.extract_info(normalized_url, download=False)
    metadata = _metadata_from_info(info)
    if metadata is None:
        raise ValueError("YouTube returned invalid video metadata")
    return metadata


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_error_message(browser: str, detail: str, source_url: str = "") -> str:
    lowered = detail.lower()
    service = "Kick" if "kick.com" in source_url else "YouTube"
    if "timed out" in lowered or "timeout" in lowered:
        return f"{service} не ответил вовремя. Проверьте интернет и повторите импорт."
    if any(marker in lowered for marker in ("unable to download", "network", "connection", "http error 5")):
        return f"Не удалось скачать видео с {service}: проверьте интернет-соединение и доступность ссылки."
    if any(marker in lowered for marker in ("private video", "video unavailable", "not available")):
        return f"{service} не предоставляет доступ к этому видео для выбранного аккаунта."
    if "cookie" in lowered or "decrypt" in lowered or "permission" in lowered:
        if browser in {"chrome", "edge"}:
            return (
                "Shorts Studio could not read this Chromium session on Windows. "
                "Sign in to YouTube in Firefox and select Firefox for the import."
            )
        return "Shorts Studio could not read the selected browser session. Sign in to YouTube there first."
    if "sign in" in lowered or "age" in lowered or "confirm your age" in lowered:
        return "YouTube rejected this session. Confirm that the selected browser account can watch the video."
    compact = " ".join(detail.replace("ERROR:", "").split())[:240]
    suffix = f" Причина: {compact}" if compact else ""
    return f"Не удалось импортировать видео с {service}.{suffix}"


def download_youtube_video(
    raw_url: str,
    browser: str,
    *,
    ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> DownloadedVideo:
    """Download one YouTube video or Kick VOD using a local browser session if needed."""
    normalized_url = normalize_video_url(raw_url)
    browser = browser.lower().strip()
    if browser not in SUPPORTED_BROWSERS:
        raise ValueError("unsupported browser")

    settings = get_settings()
    # A deterministic work directory lets yt-dlp resume a large range download
    # after a request timeout or application restart instead of creating a new
    # multi-hundred-megabyte .part file for every retry.
    cache_key = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:32]
    import_root = (settings.work_dir / "youtube-imports" / cache_key).resolve()
    import_root.mkdir(parents=True, exist_ok=True)
    logger = _YtDlpLogger()
    completed = False
    try:
        if ydl_factory is None:
            from yt_dlp import YoutubeDL

            ydl_factory = YoutubeDL
        options: dict[str, Any] = {
            # A split layout crops the face camera out of the source.  720p
            # made that small region visibly soft after it was composed into a
            # 1080x1920 Short, so retain up to 1080p.  The height cap still
            # prevents accidentally downloading a multi-gigabyte 4K stream.
            # Range chunks prevent a signed googlevideo response from sitting
            # forever at a 0-byte .part file on Windows.
            "format": (
                f"bv*[ext=mp4][height<=?{max(720, int(getattr(settings, 'youtube_max_download_height', 1080)))}]"
                "+ba[ext=m4a]/"
                f"b[ext=mp4][height<=?{max(720, int(getattr(settings, 'youtube_max_download_height', 1080)))}]/"
                f"b[height<=?{max(720, int(getattr(settings, 'youtube_max_download_height', 1080)))}]/b"
            ),
            "format_sort": ["res", "fps", "vcodec", "br"],
            "merge_output_format": "mp4",
            "outtmpl": str(import_root / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "max_filesize": settings.max_upload_bytes,
            "cachedir": False,
            "quiet": True,
            "no_warnings": True,
            "logger": logger,
            "retries": 10,
            "fragment_retries": 10,
            "socket_timeout": 30,
            "http_chunk_size": 10 * 1024 * 1024,
            "continuedl": True,
            "overwrites": False,
            "restrictfilenames": True,
        }
        ffmpeg_binary = Path(settings.ffmpeg_bin)
        if ffmpeg_binary.is_file():
            # Keep ffmpeg available for a split-stream merge, but let yt-dlp's
            # native HTTP downloader fetch progressive YouTube media. Passing
            # a signed googlevideo URL to ffmpeg can hang forever at a zero-byte
            # .part file on Windows, while the native downloader starts the
            # same response immediately and preserves yt-dlp retry handling.
            options["ffmpeg_location"] = str(ffmpeg_binary.resolve().parent)
        if node_binary := shutil.which("node"):
            options["js_runtimes"] = {"node": {"path": node_binary}}
        if getattr(settings, "youtube_use_external_downloader", False) and (
            curl_binary := shutil.which("curl")
        ):
            # yt-dlp's in-process HTTP reader can occasionally stall when it is
            # called from FastAPI's worker thread on Windows. The system curl
            # downloader handles the same signed URL reliably in a child
            # process and reports a real failure instead of leaving a 0-byte
            # .part file forever.
            options["external_downloader"] = curl_binary
        # Public videos should not depend on a browser cookie database. Chromium
        # frequently keeps that database locked on Windows, which previously
        # made an otherwise public import fail. Try the anonymous path first and
        # only touch the selected browser session when YouTube actually requires
        # authentication (for example, an age-restricted video).
        anonymous_error = ""
        try:
            with ydl_factory(options) as ydl:
                info = ydl.extract_info(normalized_url, download=True)
        except Exception as exc:
            anonymous_error = logger.last_error or str(exc)
            logger.last_error = ""
            authenticated_options = {
                **options,
                "cookiesfrombrowser": (browser, None, None, None),
            }
            try:
                with ydl_factory(authenticated_options) as ydl:
                    info = ydl.extract_info(normalized_url, download=True)
            except Exception as authenticated_exc:
                detail = logger.last_error or str(authenticated_exc) or anonymous_error
                LOGGER.warning(
                    "YouTube import failed anonymously (%s) and with %s (%s)",
                    anonymous_error,
                    browser,
                    detail,
                )
                raise ValueError(
                    _download_error_message(browser, detail, normalized_url)
                ) from authenticated_exc

        candidates = [
            path for path in import_root.iterdir()
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        ]
        if not candidates:
            raise ValueError(_download_error_message(browser, logger.last_error, normalized_url))
        downloaded = max(candidates, key=lambda item: item.stat().st_size)
        size = downloaded.stat().st_size
        if size <= 0 or size > settings.max_upload_bytes:
            raise ValueError("downloaded video exceeds the configured size limit")

        checksum = _checksum(downloaded)
        extension = downloaded.suffix.lower()
        destination = (settings.upload_dir / f"{checksum}{extension}").resolve()
        if settings.upload_dir.resolve() not in destination.parents:
            raise ValueError("invalid download destination")
        if destination.exists():
            downloaded.unlink()
        else:
            downloaded.replace(destination)

        title = str(info.get("title") or info.get("id") or "video")
        author = str(
            info.get("channel") or info.get("uploader") or info.get("creator") or ""
        )
        raw_duration = info.get("duration")
        duration = float(raw_duration) if raw_duration is not None else None
        original_name = safe_filename(f"{title}{extension}")
        completed = True
        return DownloadedVideo(
            destination, original_name, checksum, size,
            normalized_url, title, author, duration,
        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(
            _download_error_message(browser, logger.last_error or str(exc), normalized_url)
        ) from exc
    finally:
        if completed:
            shutil.rmtree(import_root, ignore_errors=True)
