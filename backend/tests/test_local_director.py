from app.local_director import service
from app.local_director.service import _parse_scores


def test_director_json_is_extracted_after_model_reasoning():
    output = '<think>internal text</think>\n[{"id":1,"score":92,"completeness":97}]'
    assert _parse_scores(output, 3) == {1: (92.0, 97.0)}


def test_director_rejects_out_of_range_candidate_ids():
    output = '[{"id":9,"score":200,"completeness":-1}]'
    assert _parse_scores(output, 2) == {}


def test_scoring_does_not_wait_for_background_model_download():
    assert service._RUNTIME_LOCK.acquire(blocking=False)
    try:
        assert service._ensure_runtime(wait=False, allow_download=False) is None
    finally:
        service._RUNTIME_LOCK.release()


def test_scoring_never_starts_a_model_download(monkeypatch):
    monkeypatch.setattr(service, "_find_cli", lambda: None)
    monkeypatch.setattr(
        service, "_download_cli",
        lambda: (_ for _ in ()).throw(AssertionError("download must stay in warmup")),
    )
    assert service._ensure_runtime(wait=False, allow_download=False) is None
