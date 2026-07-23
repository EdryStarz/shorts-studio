from pathlib import Path

from app.transcription import service


class _FakeModel:
    def __init__(self):
        self.options = None

    def transcribe(self, _path, **options):
        self.options = options
        return iter(()), None


def test_transcription_uses_quality_settings(monkeypatch):
    model = _FakeModel()
    monkeypatch.setattr(service, "_model", lambda: model)
    monkeypatch.setattr(service, "_audio_duration", lambda _path: 10.0)
    result = service.transcribe(Path("audio.wav"))
    assert result == []
    assert model.options["language"] == "ru"
    assert model.options["beam_size"] == 5
    assert model.options["word_timestamps"] is True
    assert "JESUSAVGN" in model.options["hotwords"]


def test_model_reference_prefers_downloaded_snapshot(tmp_path):
    snapshot = tmp_path / "models--Systran--faster-whisper-small" / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"model")
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    assert service._model_reference(tmp_path, "small") == str(snapshot)


def test_model_reference_does_not_select_different_cached_model(tmp_path):
    snapshot = tmp_path / "models--Systran--faster-whisper-small" / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"model")
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    assert service._model_reference(tmp_path, "large-v3-turbo") == "large-v3-turbo"


def test_time_window_core_removes_overlap_words():
    words = (
        service.Word(117.0, 118.0, "до", 0.9),
        service.Word(120.0, 121.0, "границы", 0.9),
        service.Word(123.0, 124.0, "после", 0.9),
    )
    segment = service.Segment(117.0, 124.0, "до границы после", words)
    kept = service._keep_core(segment, 118.0, 122.0, False)
    assert kept is not None
    assert [word.word for word in kept.words] == ["границы"]


def test_time_window_core_clips_word_crossing_boundary():
    segment = service.Segment(
        115.0,
        121.0,
        "слово",
        (service.Word(115.0, 121.0, "слово", 0.9),),
    )
    kept = service._keep_core(segment, 0.0, 120.0, False)
    assert kept is not None
    assert kept.end == 120.0
