from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import zipfile

import requests

from app.clip_scoring.service import Candidate
from app.core.config import get_settings
from app.core.logging import log


_RUNTIME_LOCK = threading.Lock()


def _runtime_directory() -> Path:
    directory = get_settings().storage_root / "tools" / "llama.cpp"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _find_cli() -> Path | None:
    settings = get_settings()
    configured = Path(settings.local_director_cli) if settings.local_director_cli else None
    candidates = [
        configured,
        _runtime_directory() / "llama-cli.exe",
        _runtime_directory() / "llama-cli",
    ]
    located = shutil.which("llama-cli") or shutil.which("llama-cli.exe")
    if located:
        candidates.append(Path(located))
    return next((candidate for candidate in candidates if candidate and candidate.is_file()), None)


def _safe_extract(archive: Path, destination: Path) -> None:
    root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            target = (destination / member.filename).resolve()
            if root != target and root not in target.parents:
                raise ValueError("unsafe llama.cpp archive path")
        package.extractall(destination)
    nested_cli = next(destination.rglob("llama-cli.exe"), None)
    if nested_cli and nested_cli.parent != destination:
        for item in nested_cli.parent.iterdir():
            target = destination / item.name
            if item.is_file() and not target.exists():
                shutil.copy2(item, target)


def _download_cli() -> Path | None:
    directory = _runtime_directory()
    response = requests.get(
        "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest", timeout=30,
        headers={"Accept": "application/vnd.github+json"},
    )
    response.raise_for_status()
    assets = response.json().get("assets", [])
    preferred = [
        asset for asset in assets
        if asset.get("name", "").lower().endswith("bin-win-cpu-x64.zip")
    ]
    if not preferred:
        preferred = [
            asset for asset in assets
            if "bin-win" in asset.get("name", "").lower()
            and "x64" in asset.get("name", "").lower()
            and asset.get("name", "").lower().endswith(".zip")
            and "cuda" not in asset.get("name", "").lower()
        ]
    if not preferred:
        raise RuntimeError("no compatible llama.cpp Windows x64 release found")
    asset = preferred[0]
    archive = directory / "llama.cpp.zip"
    with requests.get(asset["browser_download_url"], stream=True, timeout=180) as download:
        download.raise_for_status()
        with archive.open("wb") as stream:
            for chunk in download.iter_content(1024 * 1024):
                if chunk:
                    stream.write(chunk)
    try:
        _safe_extract(archive, directory)
    finally:
        archive.unlink(missing_ok=True)
    return _find_cli()


def _model_path() -> Path | None:
    settings = get_settings()
    configured = Path(settings.local_director_model_path) if settings.local_director_model_path else None
    if configured and configured.is_file():
        return configured
    model_root = settings.storage_root / "models" / "qwen-director"
    existing = next(model_root.rglob("*.gguf"), None) if model_root.is_dir() else None
    if existing:
        return existing
    if not settings.local_director_auto_download:
        return None
    from huggingface_hub import hf_hub_download

    model_root.mkdir(parents=True, exist_ok=True)
    path = hf_hub_download(
        repo_id=settings.local_director_repo,
        filename=settings.local_director_filename,
        local_dir=model_root,
    )
    return Path(path)


def _existing_model_path() -> Path | None:
    settings = get_settings()
    configured = Path(settings.local_director_model_path) if settings.local_director_model_path else None
    if configured and configured.is_file():
        return configured
    model_root = settings.storage_root / "models" / "qwen-director"
    return next(model_root.rglob("*.gguf"), None) if model_root.is_dir() else None


def _ensure_runtime(*, wait: bool, allow_download: bool) -> tuple[Path, Path] | None:
    acquired = _RUNTIME_LOCK.acquire(blocking=wait)
    if not acquired:
        return None
    try:
        settings = get_settings()
        cli = _find_cli()
        if (
            cli is None and allow_download and settings.local_director_auto_download
            and os.name == "nt"
        ):
            cli = _download_cli()
        model = _model_path() if allow_download else _existing_model_path()
        if cli is None or model is None:
            return None
        return cli, model
    finally:
        _RUNTIME_LOCK.release()


def warm_up() -> None:
    """Download the optional local director in the background; failures keep the fast scorer."""
    try:
        runtime = _ensure_runtime(wait=True, allow_download=True)
        log.info("local_director_ready", ready=runtime is not None)
    except Exception as exc:
        log.warning("local_director_warmup_fallback", error=str(exc))


def _prompt(candidates: list[Candidate]) -> str:
    payload = [
        {
            "id": index,
            "start": round(candidate.start, 2),
            "end": round(candidate.end, 2),
            "base_score": candidate.score,
            "signals": {
                key: candidate.details.get(key, 0.0)
                for key in (
                    "semantic_interest", "story_arc", "story_completeness", "reaction_peak",
                    "multimodal_interest", "turn_structure", "reaction_completion",
                    "cutoff_penalty", "boundary_quality",
                )
            },
            "transcript": candidate.transcript[:1200],
        }
        for index, candidate in enumerate(candidates)
    ]
    return (
        "Ты режиссёр русскоязычных вертикальных Shorts. /no_think\n"
        "Оцени кандидатов. Лучший клип обязан содержать понятную подводку, полностью показать "
        "голосовое/новость/ролик и закончить полную реакцию стримера. Нельзя выбирать обрыв "
        "слова, активного говорящего или незавершённую мысль. Награждай сильный хук, юмор, "
        "конфликт, неожиданность и понятность без внешнего контекста.\n"
        "Верни ТОЛЬКО JSON-массив объектов без markdown: "
        "[{\"id\":0,\"score\":0,\"completeness\":0}]. "
        "score и completeness — целые числа 0..100.\n"
        f"Кандидаты: {json.dumps(payload, ensure_ascii=False)}"
    )


def _parse_scores(output: str, candidate_count: int) -> dict[int, tuple[float, float]]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(output):
        if character != "[":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, list):
            continue
        scores: dict[int, tuple[float, float]] = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            try:
                item_id = int(item["id"])
                if not 0 <= item_id < candidate_count:
                    continue
                score = min(100.0, max(0.0, float(item["score"])))
                completeness = min(100.0, max(0.0, float(item["completeness"])))
                scores[item_id] = (score, completeness)
            except (KeyError, TypeError, ValueError):
                continue
        if scores:
            return scores
    return {}


def _run_director(candidates: list[Candidate]) -> dict[int, tuple[float, float]]:
    # Scoring must never wait for a multi-gigabyte background model download. Until both
    # files are ready, the deterministic multimodal ranker completes the job immediately.
    runtime = _ensure_runtime(wait=False, allow_download=False)
    if runtime is None:
        return {}
    cli, model = runtime
    settings = get_settings()
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as stream:
        stream.write(_prompt(candidates))
        prompt_path = Path(stream.name)
    try:
        command = [
            str(cli), "-m", str(model), "-f", str(prompt_path), "-n", "700",
            "--ctx-size", "8192", "--threads", str(settings.local_director_threads),
            "--temp", "0", "--no-display-prompt", "--simple-io", "--jinja",
        ]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=settings.local_director_timeout_seconds, creationflags=flags, check=False,
        )
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout)[-1000:])
        return _parse_scores(result.stdout, len(candidates))
    finally:
        prompt_path.unlink(missing_ok=True)


def select_and_rerank(candidates: list[Candidate], target: int) -> list[Candidate]:
    if not candidates or target <= 0:
        return []
    settings = get_settings()
    pool = sorted(candidates, key=lambda item: item.score, reverse=True)[
        :settings.local_director_max_candidates
    ]
    scores: dict[int, tuple[float, float]] = {}
    if settings.local_director_enabled:
        try:
            scores = _run_director(pool)
        except Exception as exc:
            log.warning("local_director_fallback", error=str(exc))
    ranked: list[Candidate] = []
    for index, candidate in enumerate(pool):
        if index not in scores:
            ranked.append(candidate)
            continue
        director_score, completeness = scores[index]
        adjusted = 0.68 * candidate.score + 0.22 * director_score + 0.10 * completeness
        details = {
            **candidate.details,
            "director_score": round(director_score / 100.0, 4),
            "director_completeness": round(completeness / 100.0, 4),
        }
        ranked.append(Candidate(
            candidate.start, candidate.end, round(adjusted, 2), details,
            candidate.transcript, candidate.words,
        ))
    return sorted(sorted(ranked, key=lambda item: item.score, reverse=True)[:target], key=lambda item: item.start)
