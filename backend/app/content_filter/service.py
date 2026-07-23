from __future__ import annotations

import re

from app.transcription.service import Segment


AD_MARKERS = (
    "промокод", "по промо коду", "реклам", "спонсор", "розыгрыш", "разыгрываем",
    "участвуй в розыгрыше", "покупай игры", "пополняй steam", "без комиссии",
    "получай плейтокены", "меняй их на билеты", "множество других призов",
    "playrock", "плейрок", "плейтокен", "оформляй подписки",
    "если день рождения то по крупному",
)
CTA_MARKERS = (
    "подписывайтесь на канал", "подпишись на канал", "ставьте лайк", "поставь лайк",
    "жмите колокольчик", "ссылка в описании", "ссылку в описании", "до новых встреч",
    "спасибо за просмотр", "увидимся в следующем", "смотрите следующий ролик",
)


def normalize(text: str) -> str:
    value = text.casefold().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я]+", " ", value)
    return " ".join(value.split())


def _is_ad(text: str) -> bool:
    normalized = normalize(text)
    return any(marker in normalized for marker in AD_MARKERS)


def _is_outro(text: str, start: float, total_duration: float) -> bool:
    if start < max(0.0, total_duration - 60.0):
        return False
    normalized = normalize(text)
    if any(marker in normalized for marker in CTA_MARKERS):
        return True
    # The source channel uses a repeated "А в США" audio sting. Whisper may render it as
    # "а в ша" or "а в Саша", so match only near the end and require a recognizable variant.
    variants = re.findall(r"\bа\s+в?\s*(?:сша|ша|саша)\b", normalized)
    return bool(variants)


def blocked_ranges(segments: list[Segment], total_duration: float) -> list[tuple[float, float]]:
    flagged = [
        segment for segment in segments
        if _is_ad(segment.text) or _is_outro(segment.text, segment.start, total_duration)
    ]
    if not flagged:
        return []
    groups: list[list[Segment]] = [[flagged[0]]]
    for segment in flagged[1:]:
        if segment.start - groups[-1][-1].end <= 8.0:
            groups[-1].append(segment)
        else:
            groups.append([segment])
    ranges: list[tuple[float, float]] = []
    ordered = sorted(segments, key=lambda item: item.start)
    for group in groups:
        start, end = group[0].start, group[-1].end
        original_end = end
        # Sponsor slogans often omit product words in the final sentence. Include up to six
        # contiguous seconds after a recognized ad block, stopping at the first real pause.
        for segment in ordered:
            if segment.start + 1e-6 < end:
                continue
            if segment.start - end > 0.4 or segment.end - original_end > 6.0:
                break
            end = max(end, segment.end)
        ranges.append((max(0.0, start - 0.15), min(total_duration, end + 0.15)))
    return ranges


def split_content_range(
    start: float, end: float, blocked: list[tuple[float, float]], min_seconds: float,
) -> list[tuple[float, float]]:
    pieces: list[tuple[float, float]] = []
    cursor = start
    for blocked_start, blocked_end in blocked:
        if blocked_end <= cursor or blocked_start >= end:
            continue
        if blocked_start - cursor >= min_seconds:
            pieces.append((cursor, min(end, blocked_start)))
        cursor = max(cursor, blocked_end)
        if cursor >= end:
            break
    if end - cursor >= min_seconds:
        pieces.append((cursor, end))
    return pieces


def contains_non_content(text: str) -> bool:
    normalized = normalize(text)
    return _is_ad(normalized) or any(marker in normalized for marker in CTA_MARKERS)
