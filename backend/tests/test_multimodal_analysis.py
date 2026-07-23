from pathlib import Path

from app.multimodal_analysis import service
from app.multimodal_analysis.service import MomentSignal, reaction_ranges, summarize


def _signal(index: int, speaker: int, reaction: float, *, change: bool = False) -> MomentSignal:
    return MomentSignal(
        start=float(index), end=float(index + 1), speech=1.0 if index < 45 else 0.0,
        energy=reaction, flux=reaction, audio_reaction=reaction,
        webcam_motion=reaction, speaker=speaker, speaker_change=float(change),
    )


def test_reaction_range_keeps_complete_source_turn_and_response():
    signals = []
    for index in range(60):
        if index < 10:
            signals.append(_signal(index, 0, 0.15))
        elif index < 30:
            signals.append(_signal(index, 1, 0.25, change=index == 10))
        elif index < 43:
            reaction = 0.9 if 32 <= index <= 38 else 0.45
            signals.append(_signal(index, 0, reaction, change=index == 30))
        else:
            signals.append(_signal(index, 0, 0.08))

    ranges = reaction_ranges(signals, min_seconds=12.0, max_seconds=60.0)
    assert ranges
    assert any(start <= 10.0 and end >= 43.0 for start, end in ranges)


def test_completed_reaction_scores_above_mid_reaction_cut():
    signals = [
        _signal(index, 0 if index < 12 or index >= 25 else 1,
                0.9 if 27 <= index <= 32 else 0.15,
                change=index in {12, 25})
        for index in range(40)
    ]
    complete = summarize(signals, 8.0, 39.0)
    cut = summarize(signals, 8.0, 29.0)
    assert complete["turn_structure"] > 0.5
    assert complete["reaction_completion"] > cut["reaction_completion"]
    assert cut["active_end"] > 0.5


def test_long_source_uses_audio_signals_without_expensive_visual_scan(monkeypatch):
    expected = [_signal(0, 0, 0.5)]
    monkeypatch.setattr(service, "analyze_audio", lambda *args, **kwargs: expected)

    def unexpected_visual_scan(*args, **kwargs):
        raise AssertionError("visual scan must be skipped for hour-long sources")

    monkeypatch.setattr(service, "add_visual_motion", unexpected_visual_scan)
    actual = service.analyze(Path("video.mp4"), Path("audio.wav"), [], 3600.0)
    assert actual == expected
