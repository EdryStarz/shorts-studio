from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2


OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
TOP_PANEL_HEIGHT = 1100
BOTTOM_PANEL_HEIGHT = OUTPUT_HEIGHT - TOP_PANEL_HEIGHT
BOTTOM_PANEL_RATIO = OUTPUT_WIDTH / BOTTOM_PANEL_HEIGHT


@dataclass(frozen=True)
class SplitLayout:
    filter: str
    webcam_upscale: float
    quality_mode: str
    webcam_crop: tuple[int, int, int, int]


def _even(value: float, minimum: int = 2) -> int:
    return max(minimum, int(value) // 2 * 2)


def _clamp_crop(origin: int, size: int, limit: int) -> int:
    return max(0, min(max(0, limit - size), origin))


def _face_centres(path: Path, start: float, end: float, *, lower_only: bool) -> list[tuple[float, float]]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return []
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    positions: list[tuple[float, float]] = []
    try:
        for index in range(12):
            timestamp = start + (end - start) * (index + 0.5) / 12
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
            ok, frame = capture.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5)
            candidates = []
            for x, y, width, height in faces:
                centre_x = (x + width / 2) / frame.shape[1]
                centre_y = (y + height / 2) / frame.shape[0]
                if not lower_only or (centre_x >= 0.62 and centre_y >= 0.42):
                    candidates.append((x, y, width, height))
            if candidates:
                x, y, width, height = max(candidates, key=lambda item: item[2] * item[3])
                positions.append(((x + width / 2) / frame.shape[1], (y + height / 2) / frame.shape[0]))
    finally:
        capture.release()
    return positions


def find_focus_x(path: Path, start: float, end: float, samples: int = 12) -> float:
    """Return the median horizontal face position, with a detail-based fallback."""
    positions = _face_centres(path, start, end, lower_only=False)
    if positions:
        values = sorted(item[0] for item in positions)
        return max(0.15, min(0.85, values[len(values) // 2]))

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return 0.5
    values: list[float] = []
    try:
        for index in range(samples):
            timestamp = start + (end - start) * (index + 0.5) / samples
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
            ok, frame = capture.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (0, 0), 9)
            detail = cv2.absdiff(gray, blurred)
            _, _, _, maximum = cv2.minMaxLoc(detail)
            values.append(maximum[0] / frame.shape[1])
    finally:
        capture.release()
    if not values:
        return 0.5
    values.sort()
    return max(0.15, min(0.85, values[len(values) // 2]))


def find_webcam_focus(path: Path, start: float, end: float) -> tuple[float, float]:
    """Confirm the streamer webcam specifically in the fixed bottom-right region."""
    positions = _face_centres(path, start, end, lower_only=True)
    if not positions:
        raise ValueError(
            "В нижней правой области исходного видео не найдена вебка стримера. "
            "Монтаж остановлен: другая область или случайное лицо не подставлялись."
        )
    xs = sorted(item[0] for item in positions)
    ys = sorted(item[1] for item in positions)
    return (
        max(0.15, min(0.9, xs[len(xs) // 2])),
        max(0.42, min(0.9, ys[len(ys) // 2])),
    )


def _brand_filter() -> str:
    return (
        f"drawbox=x=0:y={TOP_PANEL_HEIGHT - 8}:w=1080:h=16:color=0x53FC18:t=fill,"
        f"drawbox=x=34:y={TOP_PANEL_HEIGHT + 36}:w=590:h=104:color=black@0.72:t=fill,"
        "drawtext=font=Arial:text='KICK':fontsize=58:fontcolor=0x53FC18:"
        f"x=54:y={TOP_PANEL_HEIGHT + 55}:shadowcolor=black:shadowx=2:shadowy=2,"
        "drawtext=font=Arial:text='JESUSAVGN':fontsize=48:fontcolor=white:"
        f"x=220:y={TOP_PANEL_HEIGHT + 62}:shadowcolor=black:shadowx=2:shadowy=2"
    )


def split_layout(
    source_width: int, source_height: int, webcam_focus: tuple[float, float] = (0.86, 0.78),
    max_webcam_upscale: float = 4.0, webcam_region: str = "bottom_right",
) -> SplitLayout:
    """Show the complete source above a full-width, face-focused webcam panel."""
    if source_width < 2 or source_height < 2:
        raise ValueError("source dimensions must be positive")
    if webcam_region != "bottom_right":
        raise ValueError("webcam_region must be bottom_right")
    max_webcam_upscale = max(1.0, float(max_webcam_upscale))
    # Typical streaming captures place the watched video in the central player and
    # the webcam/chat in a separate right-hand column.  Cropping the player
    # before fitting it into the upper panel keeps the whole watched video but
    # prevents the small baked-in webcam from being duplicated above.
    news_x = _even(source_width * 0.177, 0)
    news_y = _even(source_height * 0.106, 0)
    news_width = min(_even(source_width * 0.611), source_width - news_x)
    news_height = min(_even(source_height * 0.75), source_height - news_y)
    minimum_width = _even(OUTPUT_WIDTH / max_webcam_upscale)
    webcam_width = min(source_width, max(minimum_width, _even(source_width * 0.22)))
    webcam_height = _even(webcam_width / BOTTOM_PANEL_RATIO)
    if webcam_height > source_height:
        webcam_height = _even(source_height)
        webcam_width = _even(webcam_height * BOTTOM_PANEL_RATIO)
    news = (
        f"[news]crop={news_width}:{news_height}:{news_x}:{news_y}[newscrop];"
        "[newscrop]split=2[newsbg][newsfg];"
        f"[newsbg]scale=1080:{TOP_PANEL_HEIGHT}:force_original_aspect_ratio=increase:"
        f"flags=lanczos,crop=1080:{TOP_PANEL_HEIGHT},gblur=sigma=24[newsback];"
        f"[newsfg]scale=1080:{TOP_PANEL_HEIGHT}:force_original_aspect_ratio=decrease:"
        "flags=lanczos[newsfront];"
        "[newsback][newsfront]overlay=(W-w)/2:(H-h)/2[newsout]"
    )
    if webcam_height <= source_height and OUTPUT_WIDTH / webcam_width <= max_webcam_upscale:
        # The crop is deliberately anchored to the configured region. Face
        # detection above only validates that this region is really a webcam;
        # it must never move the crop to a different face between clips.
        crop_x = source_width - webcam_width
        crop_y = source_height - webcam_height
        camera = (
            f"[camera]crop={webcam_width}:{webcam_height}:{crop_x}:{crop_y},"
            f"scale=1080:{BOTTOM_PANEL_HEIGHT}:flags=lanczos,"
            "eq=contrast=1.025:saturation=1.035:gamma=1.01[cameraout]"
        )
        mode = "focused"
        scale = OUTPUT_WIDTH / webcam_width
        crop = (webcam_width, webcam_height, crop_x, crop_y)
    else:
        raise ValueError(
            "Вебка в нижней правой области имеет слишком низкое разрешение для экспорта "
            "1080×1920 без размытия или чёрных полос."
        )
    return SplitLayout(
        filter=(
            f"split=2[news][camera];{news};{camera};"
            "[newsout][cameraout]vstack=inputs=2[stacked];[stacked]" + _brand_filter()
        ),
        webcam_upscale=round(scale, 3), quality_mode=mode, webcam_crop=crop,
    )


def crop_filter(
    source_width: int, source_height: int, focus_x: float, mode: str = "crop", *,
        webcam_focus: tuple[float, float] = (0.86, 0.78), max_webcam_upscale: float = 4.0,
) -> str:
    if mode == "blur":
        return (
            "split=2[bg][fg];[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,gblur=sigma=28[blur];"
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[front];"
            "[blur][front]overlay=(W-w)/2:(H-h)/2"
        )
    if mode == "split":
        return split_layout(source_width, source_height, webcam_focus, max_webcam_upscale).filter
    crop_width = min(source_width, int(source_height * 9 / 16))
    maximum_x = max(0, source_width - crop_width)
    crop_x = int(maximum_x * focus_x)
    return f"crop={crop_width}:{source_height}:{crop_x}:0,scale=1080:1920:flags=lanczos"
