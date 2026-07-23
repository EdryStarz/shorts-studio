from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Shorts Studio"
    environment: str = "development"
    database_url: str = "sqlite:///./data/shorts.db"
    redis_url: str = "redis://redis:6379/0"
    storage_root: Path = Path("./data")
    max_upload_bytes: int = 10 * 1024 * 1024 * 1024
    allowed_video_extensions: str = ".mp4,.mov,.mkv,.webm,.m4v"
    whisper_model: str = "large-v3-turbo"
    whisper_fallback_model: str = "small"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8_float16"
    whisper_cpu_compute_type: str = "int8"
    whisper_language: str = "ru"
    whisper_beam_size: int = 5
    whisper_window_seconds: float = 120.0
    whisper_context_seconds: float = 4.0
    whisper_hotwords: str = "KICK JESUSAVGN YouTube Minecraft стример новости чат"
    semantic_ranking_enabled: bool = True
    semantic_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    multimodal_analysis_enabled: bool = True
    signal_bucket_seconds: float = 1.0
    local_director_enabled: bool = True
    local_director_auto_download: bool = False
    local_director_repo: str = "Qwen/Qwen3-4B-GGUF"
    local_director_filename: str = "Qwen3-4B-Q4_K_M.gguf"
    local_director_model_path: str = ""
    local_director_cli: str = ""
    local_director_threads: int = 8
    local_director_max_candidates: int = 12
    local_director_timeout_seconds: int = 900
    min_clip_seconds: float = 12.0
    max_clip_seconds: float = 60.0
    target_clip_count: int = 6
    channel_watch_enabled: bool = True
    channel_watch_url: str = "https://www.youtube.com/@example/videos"
    channel_watch_interval_seconds: int = 1800
    channel_watch_lookback_videos: int = 12
    channel_watch_max_videos_per_cycle: int = 1
    channel_watch_browser: str = "edge"
    channel_watch_auto_queue: bool = True
    channel_watch_auto_publish_tiktok: bool = False
    youtube_max_download_height: int = 1080
    youtube_use_external_downloader: bool = False
    link_import_timeout_seconds: int = 7200
    export_crf: int = 16
    export_max_webcam_upscale: float = 4.0
    export_prefer_source_fps: bool = True
    webcam_region: str = "bottom_right"
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    cors_origins: str = "http://localhost:5173,http://localhost:3000"
    real_publishing_enabled: bool = False
    sync_processing: bool = False
    token_encryption_key: str = Field(default="", repr=False)
    youtube_client_id: str = ""
    youtube_client_secret: str = Field(default="", repr=False)
    youtube_redirect_uri: str = "http://127.0.0.1:8765/api/oauth/youtube/callback"
    tiktok_client_key: str = ""
    tiktok_client_secret: str = Field(default="", repr=False)
    tiktok_redirect_uri: str = "http://127.0.0.1:8765/api/oauth/tiktok/callback"
    instagram_client_id: str = ""
    instagram_client_secret: str = Field(default="", repr=False)
    instagram_redirect_uri: str = "https://localhost:8766/api/oauth/instagram/callback"

    @property
    def upload_dir(self) -> Path:
        return self.storage_root / "uploads"

    @property
    def work_dir(self) -> Path:
        return self.storage_root / "work"

    @property
    def export_dir(self) -> Path:
        return self.storage_root / "exports"

    def ensure_directories(self) -> None:
        for directory in (self.storage_root, self.upload_dir, self.work_dir, self.export_dir):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
