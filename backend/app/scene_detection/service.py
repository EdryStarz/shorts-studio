from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Scene:
    start: float
    end: float


def detect_scenes(path: Path, threshold: float = 27.0) -> list[Scene]:
    from scenedetect import ContentDetector, SceneManager, open_video

    video = open_video(str(path))
    manager = SceneManager()
    manager.add_detector(ContentDetector(threshold=threshold))
    # Decoding every full-resolution frame made hour-long streams appear
    # frozen for many minutes. Scene boundaries only need sub-second precision
    # because speech boundaries refine the final clip, so downscale and inspect
    # every third frame.
    manager.auto_downscale = False
    manager.downscale = 8
    manager.detect_scenes(video=video, frame_skip=14, show_progress=False)
    scene_list = manager.get_scene_list(start_in_scene=True)
    return [Scene(start.get_seconds(), end.get_seconds()) for start, end in scene_list]


def audio_activity_from_words(words: list[dict], duration: float, bucket_seconds: float = 1.0) -> list[float]:
    bucket_count = max(1, int(duration / bucket_seconds) + 1)
    activity = [0.0] * bucket_count
    for word in words:
        start_bucket = max(0, int(float(word["start"]) / bucket_seconds))
        end_bucket = min(bucket_count - 1, int(float(word["end"]) / bucket_seconds))
        for index in range(start_bucket, end_bucket + 1):
            activity[index] = 1.0
    return activity
