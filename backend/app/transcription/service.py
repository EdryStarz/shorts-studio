from dataclasses import asdict, dataclass
from functools import lru_cache
import math
from pathlib import Path

from app.core.config import get_settings


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    word: str
    probability: float


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    words: tuple[Word, ...]


def _model_reference(model_root: Path, configured_model: str) -> str:
    configured_path = Path(configured_model)
    if configured_path.is_dir():
        return str(configured_path)
    model_name = configured_model.rsplit("/", 1)[-1].lower()
    aliases = {model_name, f"faster-whisper-{model_name}"}
    if model_name == "turbo":
        aliases.update({"large-v3-turbo", "faster-whisper-large-v3-turbo"})
    snapshots = sorted(
        model_root.glob("models--*--*/snapshots/*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for snapshot in snapshots:
        cache_name = snapshot.parents[1].name.lower()
        matches_model = any(alias in cache_name for alias in aliases)
        if (
            matches_model
            and (snapshot / "model.bin").is_file()
            and (snapshot / "config.json").is_file()
        ):
            return str(snapshot)
    return configured_model


def _device() -> str:
    settings = get_settings()
    if settings.whisper_device != "auto":
        return settings.whisper_device
    try:
        import ctranslate2

        return "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
    except Exception:
        return "cpu"


def _load_model(configured_model: str, device: str):
    from faster_whisper import WhisperModel

    settings = get_settings()
    model_root = settings.storage_root / "models" / "whisper"
    model_root.mkdir(parents=True, exist_ok=True)
    model_reference = _model_reference(model_root, configured_model)
    compute_type = (
        settings.whisper_compute_type if device == "cuda" else settings.whisper_cpu_compute_type
    )
    return WhisperModel(
        model_reference, device=device, compute_type=compute_type,
        download_root=str(model_root), cpu_threads=8, num_workers=1,
    )


@lru_cache(maxsize=1)
def _model():
    settings = get_settings()
    device = _device()
    if device == "cpu" and settings.whisper_device == "auto":
        return _fallback_model()
    try:
        return _load_model(settings.whisper_model, device)
    except Exception:
        if device == "cpu":
            raise
        return _fallback_model()


@lru_cache(maxsize=1)
def _fallback_model():
    settings = get_settings()
    try:
        return _load_model(settings.whisper_fallback_model, _device())
    except Exception:
        return _load_model(settings.whisper_fallback_model, "cpu")


@lru_cache(maxsize=1)
def _cpu_model():
    settings = get_settings()
    return _load_model(settings.whisper_fallback_model, "cpu")


def _audio_duration(audio_path: Path) -> float:
    import av

    with av.open(str(audio_path)) as container:
        if container.duration is not None:
            return float(container.duration / av.time_base)
        audio_stream = next((stream for stream in container.streams if stream.type == "audio"), None)
        if audio_stream and audio_stream.duration is not None:
            return float(audio_stream.duration * audio_stream.time_base)
    raise ValueError(f"cannot determine audio duration: {audio_path}")


def _decode_once(
    model, audio, language: str | None, *, time_offset: float = 0.0,
) -> list[Segment]:
    settings = get_settings()
    segments, _ = model.transcribe(
        audio,
        language=language or settings.whisper_language or None,
        task="transcribe",
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 300},
        word_timestamps=True,
        beam_size=settings.whisper_beam_size,
        patience=1.2,
        temperature=(0.0, 0.2, 0.4),
        condition_on_previous_text=True,
        prompt_reset_on_temperature=0.5,
        repetition_penalty=1.05,
        no_repeat_ngram_size=3,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        hotwords=settings.whisper_hotwords,
        suppress_blank=True,
        hallucination_silence_threshold=2.0,
    )
    output: list[Segment] = []
    for segment in segments:
        words = tuple(
            Word(
                float(word.start) + time_offset,
                float(word.end) + time_offset,
                word.word.strip(),
                float(word.probability),
            )
            for word in (segment.words or ())
            if word.start is not None and word.end is not None and word.word.strip()
        )
        output.append(Segment(
            float(segment.start) + time_offset,
            float(segment.end) + time_offset,
            segment.text.strip(), words,
        ))
    return output


def _keep_core(segment: Segment, core_start: float, core_end: float, last: bool) -> Segment | None:
    selected = (
        word for word in segment.words
        if (word.start + word.end) / 2 >= core_start
        and (last or (word.start + word.end) / 2 < core_end)
    )
    words = tuple(
        Word(
            max(word.start, core_start),
            word.end if last else min(word.end, core_end),
            word.word,
            word.probability,
        )
        for word in selected
    )
    if not words:
        return None
    return Segment(words[0].start, words[-1].end, " ".join(word.word for word in words), words)


def _decode(model, audio_path: Path, language: str | None) -> list[Segment]:
    settings = get_settings()
    duration = _audio_duration(audio_path)
    window = max(30.0, settings.whisper_window_seconds)
    context = max(0.0, min(settings.whisper_context_seconds, window / 4))
    if duration <= window:
        return _decode_once(model, str(audio_path), language)

    from faster_whisper.audio import decode_audio

    sampling_rate = 16_000
    waveform = decode_audio(str(audio_path), sampling_rate=sampling_rate)
    duration = len(waveform) / sampling_rate
    window_count = math.ceil(duration / window)
    output: list[Segment] = []
    for index in range(window_count):
        core_start = index * window
        core_end = min(duration, core_start + window)
        decode_start = max(0.0, core_start - context)
        decode_end = min(duration, core_end + context)
        chunk = waveform[int(decode_start * sampling_rate):int(decode_end * sampling_rate)]
        decoded = _decode_once(model, chunk, language, time_offset=decode_start)
        for segment in decoded:
            kept = _keep_core(segment, core_start, core_end, index == window_count - 1)
            if kept is not None:
                output.append(kept)
    return output


def transcribe(audio_path: Path, language: str | None = None) -> list[Segment]:
    try:
        return _decode(_model(), audio_path, language)
    except Exception:
        settings = get_settings()
        if settings.whisper_device not in {"auto", "cuda"}:
            raise
        try:
            return _decode(_fallback_model(), audio_path, language)
        except Exception:
            return _decode(_cpu_model(), audio_path, language)


def serialize(segments: list[Segment]) -> list[dict]:
    return [asdict(segment) for segment in segments]
