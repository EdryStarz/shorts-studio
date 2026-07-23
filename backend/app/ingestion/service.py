import hashlib
import re
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.core.config import get_settings


def safe_filename(filename: str) -> str:
    name = Path(filename).name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return stem[:180] or "video.mp4"


async def persist_upload(upload: UploadFile) -> tuple[Path, str, int]:
    settings = get_settings()
    original = safe_filename(upload.filename or "video.mp4")
    extension = Path(original).suffix.lower()
    allowed = {item.strip().lower() for item in settings.allowed_video_extensions.split(",")}
    if extension not in allowed:
        raise ValueError(f"unsupported video extension: {extension}")
    temp_path = settings.upload_dir / f"incoming-{uuid.uuid4().hex}{extension}"
    digest = hashlib.sha256()
    size = 0
    try:
        with temp_path.open("wb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise ValueError("upload exceeds configured size limit")
                digest.update(chunk)
                output.write(chunk)
        checksum = digest.hexdigest()
        final_path = settings.upload_dir / f"{checksum}{extension}"
        if final_path.exists():
            temp_path.unlink(missing_ok=True)
        else:
            temp_path.replace(final_path)
        return final_path.resolve(), checksum, size
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()


def checksum_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
