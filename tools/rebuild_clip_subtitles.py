from __future__ import annotations

import os
from pathlib import Path
import sys

import httpx
from sqlalchemy import select


ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ["LOCALAPPDATA"]) / "ShortsStudio"
os.environ.update({
    "STORAGE_ROOT": str(DATA),
    "DATABASE_URL": f"sqlite:///{(DATA / 'shorts.db').as_posix()}",
    "TOKEN_ENCRYPTION_KEY": (DATA / "token.key").read_text(encoding="ascii").strip(),
})
sys.path.insert(0, str(ROOT / "backend"))

from app.database.models import Clip, TranscriptSegment  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402


SOURCE_ID = "0779314f-a06c-4bf3-8959-cfa19af7edae"
# Render the next scheduled clip first, then the later clips, then the already
# published first clip for the corrected local archive.
CLIP_IDS = [
    "f1397b60-2822-4418-b574-3ad4b09c9654",
    "64f18f0b-1ab1-47a6-b387-2e645889a2fe",
    "8887c1b0-d526-4ef5-badb-03d767683e4e",
    "428f831f-c0d8-48ae-bf50-11276649f148",
    "c2d5f2d0-f178-4614-8a45-3e156d4aef63",
    "1498db0e-a8b2-4e1e-a09f-4f6067a267fa",
]


def words_for_clip(clip: Clip, segments: list[TranscriptSegment]) -> list[dict]:
    words: list[dict] = []
    for segment in segments:
        if segment.end <= clip.start or segment.start >= clip.end:
            continue
        for raw_word in segment.words or []:
            word = dict(raw_word)
            word_start = float(word.get("start", segment.start))
            word_end = float(word.get("end", segment.end))
            if word_end > clip.start and word_start < clip.end:
                words.append(word)
    return words


def main() -> None:
    with SessionLocal() as db:
        segments = list(db.scalars(
            select(TranscriptSegment).where(
                TranscriptSegment.source_id == SOURCE_ID,
            ).order_by(TranscriptSegment.start)
        ).all())
        clips = {clip.id: clip for clip in db.scalars(
            select(Clip).where(Clip.id.in_(CLIP_IDS))
        ).all()}
        for clip_id in CLIP_IDS:
            clip = clips[clip_id]
            words = words_for_clip(clip, segments)
            if not words:
                raise RuntimeError(f"No transcript words found for {clip_id}")
            clip.words = words
            clip.transcript = " ".join(str(word.get("word", "")) for word in words).strip()
            clip.subtitle_text = clip.transcript
            clip.export_path = None
            clip.subtitle_path = None
            clip.checksum = None
            print(
                f"prepared {clip_id}: {len(words)} words, "
                f"{float(words[0]['start']):.2f}-{float(words[-1]['end']):.2f}"
            )
        db.commit()

    with httpx.Client(base_url="http://127.0.0.1:8765", timeout=300) as client:
        for clip_id in CLIP_IDS:
            response = client.post(
                f"/api/clips/{clip_id}/render",
                json={"mode": "split", "dynamic_subtitles": True},
            )
            response.raise_for_status()
            result = response.json()
            print(f"rendered {clip_id}: {result['export_path']}")


if __name__ == "__main__":
    main()
