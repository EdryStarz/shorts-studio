"""Standalone Windows entry point: serves the bundled UI and local FastAPI backend."""

from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
import webbrowser
import ctypes
from multiprocessing import freeze_support
from pathlib import Path


def bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


ROOT = bundle_root()
DATA_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ShortsStudio"
BIN_ROOT = ROOT / "bin"
DATA_ROOT.mkdir(parents=True, exist_ok=True)
STARTUP_LOG = DATA_ROOT / "startup.log"
TOKEN_KEY_FILE = DATA_ROOT / "token.key"
LOCALHOST_CERT_FILE = DATA_ROOT / "localhost.crt.pem"
LOCALHOST_KEY_FILE = DATA_ROOT / "localhost.key.pem"
_INSTANCE_MUTEX = None


def acquire_single_instance() -> bool:
    """Keep exactly one Shorts Studio backend bound to the local ports."""
    global _INSTANCE_MUTEX
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\ShortsStudioDesktopServer")
    if not handle:
        return True
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _INSTANCE_MUTEX = handle
    return True


def token_encryption_key() -> str:
    if TOKEN_KEY_FILE.is_file():
        return TOKEN_KEY_FILE.read_text(encoding="ascii").strip()
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    TOKEN_KEY_FILE.write_text(key, encoding="ascii")
    return key


def startup_log(message: str) -> None:
    with STARTUP_LOG.open("a", encoding="utf-8") as stream:
        stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


startup_log("entry")
os.environ.update(
    {
        "STORAGE_ROOT": str(DATA_ROOT),
        "DATABASE_URL": f"sqlite:///{(DATA_ROOT / 'shorts.db').as_posix()}",
        "FFMPEG_BIN": str(BIN_ROOT / "ffmpeg.exe"),
        "FFPROBE_BIN": str(BIN_ROOT / "ffprobe.exe"),
        "PATH": f"{BIN_ROOT}{os.pathsep}{os.environ.get('PATH', '')}",
        "SYNC_PROCESSING": "true",
        "REAL_PUBLISHING_ENABLED": os.environ.get("REAL_PUBLISHING_ENABLED", "true"),
        "MULTIMODAL_ANALYSIS_ENABLED": "true",
        "LOCAL_DIRECTOR_ENABLED": "true",
        "LOCAL_DIRECTOR_AUTO_DOWNLOAD": "true",
        "CHANNEL_WATCH_ENABLED": os.environ.get("CHANNEL_WATCH_ENABLED", "true"),
        "CHANNEL_WATCH_URL": "https://www.youtube.com/@example/videos",
        "CHANNEL_WATCH_AUTO_QUEUE": "true",
        "YOUTUBE_MAX_DOWNLOAD_HEIGHT": "1080",
        "YOUTUBE_USE_EXTERNAL_DOWNLOADER": os.environ.get(
            "YOUTUBE_USE_EXTERNAL_DOWNLOADER", "true",
        ),
        "LINK_IMPORT_TIMEOUT_SECONDS": os.environ.get(
            "LINK_IMPORT_TIMEOUT_SECONDS", "7200",
        ),
        "EXPORT_CRF": "16",
        "EXPORT_MAX_WEBCAM_UPSCALE": "4.0",
        "EXPORT_PREFER_SOURCE_FPS": "true",
        "TOKEN_ENCRYPTION_KEY": token_encryption_key(),
        "CORS_ORIGINS": "http://127.0.0.1:8765,http://localhost:8765",
        "INSTAGRAM_REDIRECT_URI": "https://localhost:8766/api/oauth/instagram/callback",
        "STANDALONE_STARTUP_LOG": str(STARTUP_LOG),
    }
)
startup_log("environment configured")

from fastapi.staticfiles import StaticFiles  # noqa: E402
from uvicorn import Config, Server  # noqa: E402

from app.main import app  # noqa: E402
from app.core.localhost_tls import ensure_localhost_certificate  # noqa: E402
from app.local_https import callback_app  # noqa: E402


startup_log("FastAPI application imported")
FRONTEND_ROOT = ROOT / "frontend"
if (FRONTEND_ROOT / "dist" / "index.html").is_file():
    # Source/development launches must serve Vite's compiled output. Serving
    # frontend/index.html directly points the browser at /src/main.tsx, which
    # StaticFiles returns as plain text and leaves the application completely
    # white. In a PyInstaller bundle the compiled files already live directly
    # under ROOT/frontend, so the fallback remains unchanged.
    FRONTEND_ROOT = FRONTEND_ROOT / "dist"
app.mount("/", StaticFiles(directory=str(FRONTEND_ROOT), html=True), name="frontend")
startup_log(f"static frontend mounted from {FRONTEND_ROOT}")


def open_when_ready() -> None:
    url = "http://127.0.0.1:8765"
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except OSError:
            time.sleep(0.1)


def run_https_callback() -> None:
    ensure_localhost_certificate(LOCALHOST_CERT_FILE, LOCALHOST_KEY_FILE)
    startup_log("starting localhost HTTPS callback")
    callback_config = Config(
        callback_app,
        host="127.0.0.1",
        port=8766,
        ssl_certfile=str(LOCALHOST_CERT_FILE),
        ssl_keyfile=str(LOCALHOST_KEY_FILE),
        log_level="warning",
        log_config=None,
        access_log=False,
        lifespan="off",
    )
    Server(callback_config).run()


def warm_local_director() -> None:
    from app.local_director.service import warm_up

    warm_up()


if __name__ == "__main__":
    freeze_support()
    if not acquire_single_instance():
        startup_log("existing instance detected; opening it")
        webbrowser.open("http://127.0.0.1:8765")
        raise SystemExit(0)
    startup_log("starting server")
    threading.Thread(target=open_when_ready, daemon=True).start()
    threading.Thread(target=run_https_callback, daemon=True).start()
    threading.Thread(target=warm_local_director, daemon=True).start()
    startup_log("building uvicorn config")
    config = Config(
        app,
        host="127.0.0.1",
        port=8765,
        log_level="warning",
        log_config=None,
        access_log=False,
    )
    startup_log("uvicorn config built")
    server = Server(config)
    startup_log("uvicorn server built")
    server.run()
