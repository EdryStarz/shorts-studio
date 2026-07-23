from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.router as router_module
from app.ingestion import youtube_links
from app.ingestion.youtube_links import (
    DownloadedVideo, download_youtube_video, fetch_youtube_metadata,
    list_channel_videos, normalize_video_url, normalize_youtube_url,
)
from app.main import app


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=7", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ],
)
def test_normalize_youtube_url(source: str, expected: str):
    assert normalize_youtube_url(source) == expected


def test_normalize_kick_vod_url():
    source = "https://kick.com/mr_rubkio/videos/be3755a3-91bd-4b71-b380-2d7d3b971edd?foo=bar"
    assert normalize_video_url(source) == (
        "https://kick.com/mr_rubkio/videos/be3755a3-91bd-4b71-b380-2d7d3b971edd"
    )


@pytest.mark.parametrize(
    "source",
    [
        "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://example.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/watch?v=too-short",
    ],
)
def test_normalize_youtube_url_rejects_non_video_or_insecure_links(source: str):
    with pytest.raises(ValueError):
        normalize_youtube_url(source)


def test_download_uses_browser_session_in_memory_only_after_anonymous_failure(monkeypatch, tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    work_dir = tmp_path / "work"
    upload_dir.mkdir()
    work_dir.mkdir()
    ffmpeg_bin = tmp_path / "bin" / "ffmpeg.exe"
    ffmpeg_bin.parent.mkdir()
    ffmpeg_bin.write_bytes(b"fake-ffmpeg")
    settings = SimpleNamespace(
        upload_dir=upload_dir,
        work_dir=work_dir,
        storage_root=tmp_path,
        max_upload_bytes=10 * 1024 * 1024,
        ffmpeg_bin=str(ffmpeg_bin),
        youtube_max_download_height=1080,
        youtube_use_external_downloader=True,
    )
    monkeypatch.setattr(youtube_links, "get_settings", lambda: settings)
    monkeypatch.setattr(
        youtube_links.shutil,
        "which",
        lambda name: f"C:/bundle/bin/{name}.exe" if name in {"node", "curl"} else None,
    )
    attempts = []

    class FakeYDL:
        def __init__(self, options):
            self.options = options
            attempts.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            assert download is True
            if "cookiesfrombrowser" not in self.options:
                raise RuntimeError("Sign in to confirm your age")
            output = Path(self.options["outtmpl"].replace("%(id)s", "dQw4w9WgXcQ").replace("%(ext)s", "mp4"))
            output.write_bytes(b"synthetic-video")
            return {"id": "dQw4w9WgXcQ", "title": "Synthetic title"}

    result = download_youtube_video(
        "https://youtu.be/dQw4w9WgXcQ", "firefox", ydl_factory=FakeYDL,
    )

    assert "cookiesfrombrowser" not in attempts[0]
    assert attempts[1]["cookiesfrombrowser"] == ("firefox", None, None, None)
    assert attempts[1]["format"] == (
        "bv*[ext=mp4][height<=?1080]+ba[ext=m4a]/"
        "b[ext=mp4][height<=?1080]/b[height<=?1080]/b"
    )
    assert attempts[1]["format_sort"] == ["res", "fps", "vcodec", "br"]
    assert attempts[1]["http_chunk_size"] == 10 * 1024 * 1024
    assert attempts[1]["cachedir"] is False
    assert "max_downloads" not in attempts[1]
    assert attempts[1]["ffmpeg_location"] == str(ffmpeg_bin.resolve().parent)
    assert attempts[1]["external_downloader"] == "C:/bundle/bin/curl.exe"
    assert attempts[1]["js_runtimes"] == {"node": {"path": "C:/bundle/bin/node.exe"}}
    assert "cookiefile" not in attempts[1]
    assert result.path.is_file()
    assert result.original_name == "Synthetic_title.mp4"
    assert not list(tmp_path.rglob("cookies.txt"))


def test_public_download_does_not_require_browser_cookies(monkeypatch, tmp_path: Path):
    upload_dir = tmp_path / "uploads"
    work_dir = tmp_path / "work"
    upload_dir.mkdir()
    work_dir.mkdir()
    settings = SimpleNamespace(
        upload_dir=upload_dir,
        work_dir=work_dir,
        storage_root=tmp_path,
        max_upload_bytes=10 * 1024 * 1024,
        ffmpeg_bin="ffmpeg",
        youtube_max_download_height=1080,
    )
    monkeypatch.setattr(youtube_links, "get_settings", lambda: settings)
    attempts = []

    class FakeYDL:
        def __init__(self, options):
            self.options = options
            attempts.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            output = Path(self.options["outtmpl"].replace("%(id)s", "dQw4w9WgXcQ").replace("%(ext)s", "mp4"))
            output.write_bytes(b"public-video")
            return {"id": "dQw4w9WgXcQ", "title": "Public video"}

    result = download_youtube_video(
        "https://youtu.be/dQw4w9WgXcQ", "edge", ydl_factory=FakeYDL,
    )

    assert len(attempts) == 1
    assert "cookiesfrombrowser" not in attempts[0]
    assert result.path.is_file()


def test_channel_listing_and_metadata_fetch_do_not_download_media():
    calls = []

    class FakeYDL:
        def __init__(self, options):
            self.options = options
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download):
            assert download is False
            if "@example" in url:
                return {"entries": [{
                    "id": "dQw4w9WgXcQ", "title": "Новая реакция", "duration": 3600,
                    "upload_date": "20260717",
                }]}
            return {
                "id": "dQw4w9WgXcQ", "title": "Новая реакция", "description": "Разбор",
                "duration": 3600, "tags": ["реакция"], "live_status": "not_live",
            }

    entries = list_channel_videos("https://www.youtube.com/@example/videos", ydl_factory=FakeYDL)
    metadata = fetch_youtube_metadata(entries[0].url, ydl_factory=FakeYDL)
    assert entries[0].video_id == "dQw4w9WgXcQ"
    assert metadata.tags == ("реакция",)
    assert all(options["skip_download"] is True for options in calls)


def test_youtube_import_route_creates_source(monkeypatch, tmp_path: Path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    downloaded = DownloadedVideo(video, "video.mp4", "a" * 64, video.stat().st_size)
    monkeypatch.setattr(router_module, "download_youtube_video", lambda *_args: downloaded)

    with TestClient(app) as client:
        response = client.post(
            "/api/youtube/import",
            json={
                "url": "https://youtu.be/dQw4w9WgXcQ",
                "browser": "firefox",
                "rights_confirmed": True,
                "adult_content": True,
                "adult_access_confirmed": True,
            },
        )

    assert response.status_code == 201
    assert response.json()["original_name"] == "video.mp4"


def test_youtube_import_route_rejects_unconfirmed_adult_access(monkeypatch):
    called = False

    def download_should_not_run(*_args):
        nonlocal called
        called = True

    monkeypatch.setattr(router_module, "download_youtube_video", download_should_not_run)
    with TestClient(app) as client:
        response = client.post(
            "/api/youtube/import",
            json={
                "url": "https://youtu.be/dQw4w9WgXcQ",
                "browser": "firefox",
                "rights_confirmed": True,
                "adult_content": True,
                "adult_access_confirmed": False,
            },
        )

    assert response.status_code == 422
    assert called is False
