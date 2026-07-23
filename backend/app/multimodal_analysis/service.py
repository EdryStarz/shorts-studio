from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import wave

import numpy as np

from app.core.logging import log
from app.transcription.service import Segment


@dataclass(frozen=True)
class MomentSignal:
    start: float
    end: float
    speech: float = 0.0
    energy: float = 0.0
    energy_delta: float = 0.0
    flux: float = 0.0
    audio_reaction: float = 0.0
    visual_motion: float = 0.0
    visual_change: float = 0.0
    webcam_motion: float = 0.0
    speaker: int = -1
    speaker_change: float = 0.0


def serialize(signals: list[MomentSignal]) -> list[dict]:
    return [asdict(signal) for signal in signals]


def deserialize(values: list[dict]) -> list[MomentSignal]:
    fields = set(MomentSignal.__dataclass_fields__)
    return [MomentSignal(**{key: value for key, value in item.items() if key in fields}) for item in values]


def _robust_scale(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return values.astype(np.float32)
    low, high = np.percentile(values, [20, 95])
    if high - low < 1e-8:
        return np.zeros(values.shape, dtype=np.float32)
    return np.clip((values - low) / (high - low), 0.0, 1.0).astype(np.float32)


def _speech_mask(segments: list[Segment], starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    mask = np.zeros(starts.shape, dtype=np.float32)
    segment_index = 0
    ordered = sorted(segments, key=lambda item: item.start)
    for index, (start, end) in enumerate(zip(starts, ends, strict=False)):
        while segment_index < len(ordered) and ordered[segment_index].end <= start:
            segment_index += 1
        cursor = segment_index
        while cursor < len(ordered) and ordered[cursor].start < end:
            overlap = max(0.0, min(end, ordered[cursor].end) - max(start, ordered[cursor].start))
            mask[index] = max(mask[index], min(1.0, overlap / max(end - start, 1e-6)))
            cursor += 1
    return mask


def _cluster_speakers(signatures: np.ndarray, active: np.ndarray) -> np.ndarray:
    """Return stable, anonymous speaker clusters without accounts or API tokens."""
    labels = np.full(len(signatures), -1, dtype=np.int16)
    indexes = np.flatnonzero(active)
    if len(indexes) < 8:
        labels[indexes] = 0
        return labels
    values = signatures[indexes]
    first = int(np.argmax(np.linalg.norm(values - values.mean(axis=0), axis=1)))
    second = int(np.argmax(np.linalg.norm(values - values[first], axis=1)))
    centers = np.stack((values[first], values[second]))
    if np.linalg.norm(centers[0] - centers[1]) < 0.08:
        labels[indexes] = 0
        return labels
    assignments = np.zeros(len(values), dtype=np.int16)
    for _ in range(8):
        distances = np.linalg.norm(values[:, None, :] - centers[None, :, :], axis=2)
        next_assignments = np.argmin(distances, axis=1).astype(np.int16)
        if np.array_equal(assignments, next_assignments):
            break
        assignments = next_assignments
        for cluster in (0, 1):
            members = values[assignments == cluster]
            if len(members):
                centers[cluster] = members.mean(axis=0)
    labels[indexes] = assignments
    # A three-frame majority filter prevents false turns from noisy single buckets.
    smoothed = labels.copy()
    for index in indexes:
        nearby = labels[max(0, index - 1):min(len(labels), index + 2)]
        nearby = nearby[nearby >= 0]
        if len(nearby):
            counts = np.bincount(nearby, minlength=2)
            smoothed[index] = int(np.argmax(counts))
    return smoothed


def analyze_audio(
    audio_path: Path, segments: list[Segment], duration: float, bucket_seconds: float = 1.0,
) -> list[MomentSignal]:
    if not audio_path.is_file() or duration <= 0:
        return []
    with wave.open(str(audio_path), "rb") as stream:
        sample_rate = stream.getframerate()
        channels = stream.getnchannels()
        sample_width = stream.getsampwidth()
        raw = stream.readframes(stream.getnframes())
    if sample_width != 2:
        raise ValueError("reaction analysis expects 16-bit PCM audio")
    waveform = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if channels > 1:
        waveform = waveform.reshape(-1, channels).mean(axis=1)
    waveform /= 32768.0
    bucket_size = max(1, int(sample_rate * max(0.5, bucket_seconds)))
    count = max(1, int(np.ceil(len(waveform) / bucket_size)))
    energies = np.zeros(count, dtype=np.float32)
    zcr = np.zeros(count, dtype=np.float32)
    flux = np.zeros(count, dtype=np.float32)
    signatures = np.zeros((count, 12), dtype=np.float32)
    previous_spectrum: np.ndarray | None = None
    window = np.hanning(bucket_size).astype(np.float32)
    for index in range(count):
        chunk = waveform[index * bucket_size:(index + 1) * bucket_size]
        if len(chunk) < bucket_size:
            chunk = np.pad(chunk, (0, bucket_size - len(chunk)))
        energies[index] = float(np.sqrt(np.mean(chunk * chunk) + 1e-10))
        zcr[index] = float(np.mean(np.abs(np.diff(np.signbit(chunk)))))
        spectrum = np.abs(np.fft.rfft(chunk * window)).astype(np.float32)
        spectrum /= max(float(np.linalg.norm(spectrum)), 1e-8)
        if previous_spectrum is not None:
            flux[index] = float(np.linalg.norm(np.maximum(spectrum - previous_spectrum, 0.0)))
        previous_spectrum = spectrum
        bands = np.array_split(spectrum[1:], 12)
        signature = np.asarray([np.log1p(float(part.mean()) * 1000.0) for part in bands])
        signatures[index] = signature / max(float(np.linalg.norm(signature)), 1e-8)

    starts = np.arange(count, dtype=np.float32) * bucket_size / sample_rate
    ends = np.minimum(starts + bucket_size / sample_rate, duration)
    speech = _speech_mask(segments, starts, ends)
    scaled_energy = _robust_scale(energies)
    scaled_zcr = _robust_scale(zcr)
    scaled_flux = _robust_scale(flux)
    energy_delta = np.zeros_like(scaled_energy)
    if len(energy_delta) > 1:
        energy_delta[1:] = np.abs(np.diff(scaled_energy))
    scaled_energy_delta = _robust_scale(energy_delta)
    # This score intentionally combines independent acoustic changes. It remains useful when
    # a heavyweight event classifier is unavailable in the desktop bundle.
    reaction = np.clip(
        0.42 * scaled_energy + 0.27 * scaled_flux + 0.16 * scaled_zcr
        + 0.15 * scaled_energy_delta,
        0.0,
        1.0,
    )
    active = (speech >= 0.2) & (scaled_energy >= 0.08)
    speakers = _cluster_speakers(signatures, active)
    changes = np.zeros(count, dtype=np.float32)
    previous = -1
    for index, speaker in enumerate(speakers):
        if speaker >= 0:
            if previous >= 0 and speaker != previous:
                changes[index] = 1.0
            previous = int(speaker)
    return [
        MomentSignal(
            start=float(starts[index]), end=float(ends[index]), speech=float(speech[index]),
            energy=float(scaled_energy[index]), flux=float(scaled_flux[index]),
            energy_delta=float(scaled_energy_delta[index]),
            audio_reaction=float(reaction[index]), speaker=int(speakers[index]),
            speaker_change=float(changes[index]),
        )
        for index in range(count)
        if starts[index] < duration
    ]


def add_visual_motion(
    video_path: Path, signals: list[MomentSignal], duration: float, max_samples: int = 900,
) -> list[MomentSignal]:
    if not signals or not video_path.is_file() or duration <= 0:
        return signals
    try:
        import cv2

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            return signals
        fps = max(1.0, float(capture.get(cv2.CAP_PROP_FPS) or 0.0))
        frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or duration * fps))
        interval_frames = max(1, frame_count // max(max_samples, 1))
        sampled: list[tuple[float, float, float]] = []
        previous = None
        # Repeated timestamp seeks decode the preceding GOP again and again,
        # making hour-long sources stall at 55/65%. Walk the stream once and
        # only convert the sampled frames.
        for frame_index in range(frame_count):
            ok = capture.grab()
            if not ok:
                break
            if frame_index % interval_frames:
                continue
            ok, frame = capture.retrieve()
            if not ok:
                continue
            timestamp = frame_index / fps
            height, width = frame.shape[:2]
            frame = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
            if previous is not None:
                difference = np.abs(gray - previous)
                whole = float(difference.mean())
                # Shorts Studio renders the streamer in the lower half; this feature focuses on
                # head/hand motion there without relying on fragile OpenCV cascade classifiers.
                lower = float(difference[45:, :].mean())
                sampled.append((timestamp, whole, lower))
            previous = gray
        capture.release()
        if not sampled:
            return signals
        whole_scaled = _robust_scale(np.asarray([item[1] for item in sampled]))
        lower_scaled = _robust_scale(np.asarray([item[2] for item in sampled]))
        output: list[MomentSignal] = []
        for signal in signals:
            nearest = min(range(len(sampled)), key=lambda idx: abs(sampled[idx][0] - signal.start))
            values = asdict(signal)
            values["visual_motion"] = float(whole_scaled[nearest])
            values["visual_change"] = float(whole_scaled[nearest])
            values["webcam_motion"] = float(lower_scaled[nearest])
            output.append(MomentSignal(**values))
        return output
    except Exception as exc:
        log.warning("visual_signal_fallback", error=str(exc))
        return signals


def analyze(
    video_path: Path, audio_path: Path, segments: list[Segment], duration: float,
    bucket_seconds: float = 1.0,
) -> list[MomentSignal]:
    signals = analyze_audio(audio_path, segments, duration, bucket_seconds)
    if duration > 1800:
        log.info(
            "visual_signal_skipped_for_long_source",
            duration=duration, audio_signal_count=len(signals),
        )
        return signals
    return add_visual_motion(video_path, signals, duration)


def summarize(signals: list[MomentSignal], start: float, end: float) -> dict[str, float]:
    selected = [signal for signal in signals if signal.end > start and signal.start < end]
    if not selected:
        return {
            "multimodal_interest": 0.0, "turn_structure": 0.0,
            "reaction_completion": 0.0, "active_end": 0.0,
            "hook": 0.0, "payoff": 0.0, "streamer_reaction": 0.0,
            "audio_contrast": 0.0, "visual_progression": 0.0,
        }
    reactions = np.asarray([
        0.62 * signal.audio_reaction + 0.25 * signal.webcam_motion
        + 0.13 * signal.visual_motion
        for signal in selected
    ], dtype=np.float32)
    peak_index = int(np.argmax(reactions))
    peak = float(reactions[peak_index])
    top_count = max(1, int(np.ceil(len(reactions) * 0.2)))
    interest = float(np.sort(reactions)[-top_count:].mean())
    changes = [index for index, signal in enumerate(selected) if signal.speaker_change > 0.5]
    ordered_turn = any(index < peak_index for index in changes)
    followthrough = (len(selected) - peak_index - 1) / max(len(selected) * 0.35, 1.0)
    turn_structure = min(1.0, 0.45 * min(1.0, len(changes) / 2.0) + 0.55 * float(ordered_turn))
    tail = reactions[-min(3, len(reactions)):]
    tail_level = float(tail.mean())
    completion = min(1.0, max(0.0, followthrough)) * min(
        1.0, max(0.0, (peak - tail_level + 0.18) / 0.45),
    )
    last = selected[-1]
    active_end = max(last.speech, last.audio_reaction, last.webcam_motion)
    peak_position = peak_index / max(len(reactions) - 1, 1)
    payoff_position = max(0.0, 1.0 - abs(peak_position - 0.68) / 0.68)
    payoff = min(1.0, 0.52 * peak + 0.28 * payoff_position + 0.20 * completion)

    audio_values = np.asarray([signal.audio_reaction for signal in selected], dtype=np.float32)
    energy_deltas = np.asarray([signal.energy_delta for signal in selected], dtype=np.float32)
    audio_contrast = min(
        1.0,
        0.60 * float(np.ptp(audio_values))
        + 0.40 * float(np.sort(energy_deltas)[-max(1, len(energy_deltas) // 5):].mean()),
    )
    visual_values = np.asarray([signal.visual_change for signal in selected], dtype=np.float32)
    visual_peaks = int(np.count_nonzero(visual_values >= 0.60))
    visual_progression = min(
        1.0,
        0.55 * float(np.sort(visual_values)[-max(1, len(visual_values) // 5):].mean())
        + 0.45 * min(1.0, visual_peaks / max(len(visual_values) / 5.0, 1.0)),
    )
    webcam_values = np.asarray([signal.webcam_motion for signal in selected], dtype=np.float32)
    streamer_reaction = min(
        1.0,
        0.55 * float(webcam_values.max(initial=0.0))
        + 0.30 * peak
        + 0.15 * turn_structure,
    )
    hook_count = max(1, min(len(selected), int(np.ceil(2.5 / max(selected[0].end - selected[0].start, 0.5)))))
    hook_values = reactions[:hook_count]
    hook = min(
        1.0,
        0.60 * float(hook_values.max(initial=0.0))
        + 0.25 * float(np.asarray([item.energy_delta for item in selected[:hook_count]]).max(initial=0.0))
        + 0.15 * float(np.asarray([item.visual_change for item in selected[:hook_count]]).max(initial=0.0)),
    )
    return {
        "multimodal_interest": round(min(1.0, 0.55 * peak + 0.45 * interest), 4),
        "turn_structure": round(turn_structure, 4),
        "reaction_completion": round(completion, 4),
        "active_end": round(float(active_end), 4),
        "hook": round(hook, 4),
        "payoff": round(payoff, 4),
        "streamer_reaction": round(streamer_reaction, 4),
        "audio_contrast": round(audio_contrast, 4),
        "visual_progression": round(visual_progression, 4),
    }


def reaction_ranges(
    signals: list[MomentSignal], min_seconds: float, max_seconds: float,
) -> list[tuple[float, float]]:
    """Propose stimulus-to-reaction windows from speaker turns and acoustic peaks."""
    if not signals:
        return []
    reactions = np.asarray([
        0.65 * signal.audio_reaction + 0.25 * signal.webcam_motion
        + 0.10 * signal.visual_motion
        for signal in signals
    ])
    ranges: set[tuple[float, float]] = set()
    for index in range(1, len(signals) - 1):
        peak = reactions[index]
        local_peak = peak >= reactions[index - 1] and peak >= reactions[index + 1]
        if peak < 0.62 or not local_peak:
            continue
        prior_changes = [
            cursor for cursor in range(max(0, index - 24), index + 1)
            if signals[cursor].speaker_change > 0.5
        ]
        if prior_changes:
            transition = prior_changes[-1]
            source_speaker = signals[max(0, transition - 1)].speaker
            source_start = max(0, transition - 1)
            while source_start > 0 and signals[source_start - 1].speaker == source_speaker:
                source_start -= 1
            # Keep a brief spoken setup before the complete source-audio turn.
            start_index = max(0, source_start - 4)
        else:
            start_index = max(0, index - 12)
        end_index = min(len(signals) - 1, index + 16)
        # Preserve the complete response until speech/reaction settles, within the hard limit.
        while end_index + 1 < len(signals):
            candidate_end = signals[end_index + 1].end
            if candidate_end - signals[start_index].start > max_seconds:
                break
            current = signals[end_index]
            if current.speech < 0.15 and current.audio_reaction < 0.28:
                break
            end_index += 1
        start, end = signals[start_index].start, signals[end_index].end
        if end - start < min_seconds:
            end = min(signals[-1].end, start + min_seconds)
        if min_seconds <= end - start <= max_seconds:
            ranges.add((round(start, 3), round(end, 3)))
    return sorted(ranges)
