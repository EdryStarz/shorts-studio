from functools import lru_cache

import numpy as np

from app.core.config import get_settings


HIGHLIGHT_PROMPTS = (
    "Самая сильная эмоциональная реакция стримера, без пауз и лишней подводки.",
    "Короткий законченный эпизод: ключевая фраза из голосового или видео и полная немедленная реакция стримера.",
    "Самый яркий фрагмент длительностью примерно от двадцати до сорока пяти секунд.",
    "Момент с быстрым хуком, кульминацией и понятным завершением без обрезанной фразы.",
    "Неожиданный, шокирующий или очень интересный факт.",
    "Смешной самодостаточный момент с понятной кульминацией.",
    "Спорное мнение, конфликт или сильный аргумент, вызывающий обсуждение.",
    "Законченный фрагмент, который понятен без длинного контекста и удерживает внимание.",
)


@lru_cache(maxsize=1)
def _embedding_model():
    from fastembed import TextEmbedding

    settings = get_settings()
    cache_dir = settings.storage_root / "models" / "fastembed"
    cache_dir.mkdir(parents=True, exist_ok=True)
    local_files_only = any(cache_dir.rglob("*.onnx"))
    return TextEmbedding(
        model_name=settings.semantic_model,
        cache_dir=str(cache_dir),
        threads=4,
        local_files_only=local_files_only,
    )


def score_highlight_texts(texts: list[str]) -> list[float]:
    """Return local semantic-interest scores in the 0..1 range."""
    if not texts:
        return []
    values = [*HIGHLIGHT_PROMPTS, *(text.strip()[:1800] for text in texts)]
    embeddings = np.asarray(list(_embedding_model().embed(values, batch_size=16)), dtype=np.float32)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    embeddings = embeddings / np.maximum(norms, 1e-8)
    prompts = embeddings[:len(HIGHLIGHT_PROMPTS)]
    candidates = embeddings[len(HIGHLIGHT_PROMPTS):]
    similarities = candidates @ prompts.T
    strongest = np.max(similarities, axis=1)
    top_two = np.sort(similarities, axis=1)[:, -2:].mean(axis=1)
    combined = 0.7 * strongest + 0.3 * top_two
    return [float(np.clip((value - 0.15) / 0.6, 0.0, 1.0)) for value in combined]
