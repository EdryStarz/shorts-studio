from app.content_filter.service import blocked_ranges, split_content_range
from app.transcription.service import Segment


def _segment(start: float, end: float, text: str) -> Segment:
    return Segment(start, end, text, ())


def test_ad_block_is_grouped_and_removed_from_candidate():
    segments = [
        _segment(118.0, 149.0, "Почему нельзя выйти из квартиры?"),
        _segment(149.0, 151.3, "Мы разыгрываем Nissan 370Z"),
        _segment(154.5, 159.2, "PlayRock запускает масштабный розыгрыш"),
        _segment(159.2, 166.5, "Покупай игры и участвуй в розыгрыше машины"),
        _segment(169.7, 173.2, "Steam без комиссии по промокоду на экране"),
        _segment(174.1, 177.0, "Если день рождения, то по-крупному"),
        _segment(178.9, 190.0, "Причины для паники все-таки есть"),
    ]
    blocked = blocked_ranges(segments, 520.0)
    assert len(blocked) == 1
    assert blocked[0][0] <= 149.0
    assert blocked[0][1] >= 177.0
    pieces = split_content_range(118.0, 190.0, blocked, min_seconds=12.0)
    assert pieces[0][1] <= 149.0
    assert all(not (start < 160.0 < end) for start, end in pieces)


def test_repeated_a_v_ssha_outro_is_blocked_only_near_video_end():
    segments = [
        _segment(40.0, 42.0, "А в США приняли новый закон"),
        _segment(509.0, 511.8, "А в США, а в США"),
        _segment(514.2, 518.0, "А в Саша, а в ша"),
    ]
    blocked = blocked_ranges(segments, 518.0)
    assert len(blocked) == 1
    assert blocked[0][0] > 500.0
