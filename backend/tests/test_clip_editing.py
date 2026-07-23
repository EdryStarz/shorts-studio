from app.api.router import update_clip
from app.api.schemas import ClipUpdate
from app.database.models import Clip, Source, TranscriptSegment
from app.database.session import SessionLocal


def test_editing_clip_boundaries_rebuilds_caption_word_timestamps(tmp_path):
    with SessionLocal() as db:
        source = Source(
            original_name="source.mp4",
            storage_path=str(tmp_path / "source.mp4"),
            checksum="a" * 64,
            size_bytes=1,
            duration=300,
            rights_confirmed=True,
        )
        db.add(source)
        db.flush()
        db.add_all([
            TranscriptSegment(
                source_id=source.id, start=10, end=11, text="old",
                words=[{"start": 10.0, "end": 10.5, "word": "old"}],
            ),
            TranscriptSegment(
                source_id=source.id, start=100, end=102, text="новый текст",
                words=[
                    {"start": 100.1, "end": 100.5, "word": "новый"},
                    {"start": 100.6, "end": 101.0, "word": "текст"},
                ],
            ),
        ])
        clip = Clip(
            source_id=source.id, start=10, end=30, score=1,
            transcript="old", subtitle_text="old",
            words=[{"start": 10.0, "end": 10.5, "word": "old"}],
            export_path="old.mp4", checksum="b" * 64,
        )
        db.add(clip)
        db.commit()

        updated = update_clip(
            clip.id,
            ClipUpdate(start=100, end=125, subtitle_text="новый текст"),
            db,
        )

        assert [word["word"] for word in updated.words] == ["новый", "текст"]
        assert updated.transcript == "новый текст"
        assert updated.export_path is None
        assert updated.checksum is None
