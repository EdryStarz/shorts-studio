from __future__ import annotations

from collections import Counter
import re

from app.content_filter.service import AD_MARKERS, CTA_MARKERS, normalize


BASE_TAGS = ("jesusavgn", "хесус")
STOPWORDS = {
    "этот", "эта", "это", "эти", "такой", "такая", "который", "которая", "которые",
    "вообще", "просто", "потом", "тогда", "сейчас", "здесь", "очень", "тоже", "даже",
    "потому", "поэтому", "как", "что", "чтобы", "если", "или", "для", "при", "про",
    "вот", "там", "тут", "уже", "еще", "только", "тебя", "тебе", "тебе", "меня",
    "себя", "свой", "свои", "свою", "они", "она", "оно", "его", "ему", "него",
    "быть", "будет", "было", "были", "есть", "нет", "можно", "может", "могут",
    "говорил", "говорит", "сказал", "короче", "ладно", "блин", "ну", "чего", "чето",
    "человек", "людей", "истории", "ситуации", "момент", "стрим", "стриме",
    "появляется", "удалить", "нельзя", "работает", "прямо",
    "знаю", "будут", "дальше", "когда", "само", "решение", "нужно",
    "делал", "делать", "люди", "пришли", "момента", "моментам",
    "этой", "всей", "таки", "какие", "сидел", "квартиру", "квартиры", "квартире",
    "значит", "по моему", "нормально", "ничего", "сделать", "другим",
    "странно", "рублей", "рубля", "хорошо",
}
TOPIC_ALIASES = (
    (("max",), "max"),
    (("дуров",), "дуров"),
    (("фсб",), "фсб"),
    (("голосов",), "голосовое"),
    (("переписк", "без контекста"), "разбор"),
    (("мобилизац", "военком", "повестк"), "мобилизация"),
    (("росси",), "россия"),
    (("украин",), "украина"),
    (("беларус",), "беларусь"),
    (("лагод",), "лагода"),
    (("федоров", "фёдоров"), "федоров"),
    (("зеленск",), "зеленский"),
    (("министр оборон",), "минобороны"),
    (("киевстонер",), "киевстонер"),
    (("телеграм",), "телеграм"),
    (("блокиров",), "блокировки"),
    (("нейросет",), "нейросети"),
    (("новост",), "новости"),
    (("запрет",), "запрет"),
    (("фейк", "фейk"), "фейк"),
    (("квартир", "не выходя"), "изоляция"),
    (("счет", "счета"), "банковскиесчета"),
    (("за границ", "уехать из россии"), "эмиграция"),
    (("наличк",), "наличные"),
    (("паник",), "паника"),
    (("аудитори",), "аудитория"),
    (("совкомбанк",), "совкомбанк"),
    (("робот", "гуманоид"), "роботы"),
    (("wildberries", "вайлдбер", "валдырис"), "wildberries"),
    (("дрон",), "дроны"),
    (("склад",), "склад"),
    (("пожар", "горит", "сгорел"), "пожар"),
)
REACTION_WORDS = (
    "почему", "зачем", "как так", "что это", "невероят", "абсурд", "шок", "бред",
    "неожидан", "серьезно", "реакц", "инсайд", "побег", "запрет", "ошиб",
)
TENSION_WORDS = (
    "заявил", "ответил", "показал", "запрет", "блокиров", "удалил", "скандал",
    "конфликт", "обман", "ошибка", "впервые", "по годам", "выяснилось",
)
PROFANITY = re.compile(r"\b(?:бл[яе]\w*|ху[йеяи]\w*|пизд\w*|еб\w*|ёб\w*)\b", re.IGNORECASE)
TAG_STEMS = {
    "россия": ("росси",), "украина": ("украин",), "беларусь": ("беларус",),
    "мобилизация": ("мобилизац", "военком", "повестк"),
    "блокировки": ("блокиров",), "эмиграция": ("за границ", "уехать"),
    "голосовое": ("голосов",), "банковскиесчета": ("счет",),
    "совкомбанк": ("совкомбанк",), "роботы": ("робот", "гуманоид"),
    "wildberries": ("wildberries", "вайлдбер", "валдырис"),
    "дроны": ("дрон",), "склад": ("склад",),
    "пожар": ("пожар", "горит", "сгорел"),
}


def _topic_present(tag: str, normalized_text: str) -> bool:
    return any(stem in normalized_text for stem in TAG_STEMS.get(tag, (tag,)))


def _tag_key(tag: str) -> str:
    """Collapse grammatical forms and aliases to one hashtag identity."""
    normalized_tag = normalize(tag)
    for aliases, canonical in TOPIC_ALIASES:
        if normalized_tag == canonical or any(alias in normalized_tag for alias in aliases):
            return canonical
    return normalized_tag


def _remove_non_content(text: str) -> str:
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    kept = []
    for chunk in chunks:
        normalized = normalize(chunk)
        if any(marker in normalized for marker in (*AD_MARKERS, *CTA_MARKERS)):
            continue
        kept.append(chunk)
    value = " ".join(kept)
    outro = re.compile(r"(?:\bа\s+в?\s*(?:сша|ша|саша)\b[\s,.!?]*){2,}", re.IGNORECASE)
    return " ".join(outro.sub(" ", value).split())


def _brand_safe(text: str) -> str:
    return PROFANITY.sub(lambda match: f"{match.group(0)[0]}***", text)


def _limit_words(text: str, limit: int) -> str:
    value = " ".join(text.split()).strip(" -—,.;:")
    dangling = {"а", "и", "в", "во", "на", "из", "по", "для", "без", "с", "со", "что"}
    while value.split() and value.split()[-1].casefold().strip(".,!?—") in dangling:
        value = value.rsplit(" ", 1)[0].rstrip(" -—,.;:")
    if len(value) <= limit:
        return value
    shortened = value[: limit + 1].rsplit(" ", 1)[0].rstrip(" -—,.;:")
    while shortened.split() and shortened.split()[-1].casefold().strip(".,!?—") in dangling:
        shortened = shortened.rsplit(" ", 1)[0].rstrip(" -—,.;:")
    return f"{shortened}…"


def _sentence_candidates(text: str) -> list[str]:
    raw = [
        part.strip(" -—,.;:")
        for part in re.split(r"(?<=[.!?])\s+|\n+|(?<=[а-яёa-z0-9?!])\s+(?=[А-ЯЁA-Z])", text)
    ]
    merged: list[str] = []
    for part in (value for value in raw if value):
        if (
            merged and not merged[-1].endswith((".", "!", "?"))
            and (len(merged[-1].split()) < 6 or len(part.split()) < 9)
            and len(merged[-1].split()) + len(part.split()) <= 22
        ):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    output: list[str] = []
    for sentence in merged:
        words = sentence.split()
        if 4 <= len(words) <= 24:
            output.append(sentence)
        elif len(words) > 24:
            for start in range(0, len(words), 14):
                chunk = words[start:start + 18]
                if len(chunk) >= 4:
                    output.append(" ".join(chunk))
    return output


def _keywords(text: str) -> list[str]:
    normalized = normalize(text)
    words = re.findall(r"[a-zа-я0-9]+", normalized)
    counts = Counter(
        word for word in words
        if len(word) >= 4 and word not in STOPWORDS and not PROFANITY.fullmatch(word)
    )
    tags: list[str] = []
    tag_keys: set[str] = set()
    for aliases, tag in TOPIC_ALIASES:
        if any(alias in normalized for alias in aliases):
            key = _tag_key(tag)
            if key not in tag_keys:
                tags.append(tag)
                tag_keys.add(key)
    for word, count in counts.most_common(10):
        if count < 2:
            continue
        cleaned = re.sub(r"[^a-zа-я0-9]", "", word)
        key = _tag_key(cleaned)
        if cleaned and key not in tag_keys:
            tags.append(cleaned)
            tag_keys.add(key)
        if len(tags) >= 4:
            break
    return tags[:4]


def topic_cluster(text: str) -> str:
    """Return a stable cluster used by the scheduler to separate adjacent posts."""
    keywords = _keywords(_remove_non_content(text))
    return keywords[0] if keywords else "общая-тема"


def infer_hook_type(text: str) -> str:
    normalized = normalize(text)
    if "по годам" in normalized or "сравн" in normalized:
        return "comparison"
    if "?" in text or any(word in normalized for word in ("почему", "зачем", "как так")):
        return "question"
    if any(word in normalized for word in TENSION_WORDS):
        return "conflict"
    if any(word in normalized for word in REACTION_WORDS):
        return "reaction"
    return "statement"


def _format_tags(text: str) -> list[str]:
    normalized = normalize(text)
    if any(marker in normalized for marker in ("новост", "репортаж", "сми", "телеканал")):
        return ["новости"]
    if any(marker in normalized for marker in REACTION_WORDS):
        return ["реакция"]
    return []


def _smart_title(text: str) -> str:
    normalized_text = normalize(text)
    if "совкомбанк" in normalized_text and "туалетн" in normalized_text:
        return "Акции Совкомбанка дешевле туалетной бумаги — реакция Хесуса"
    if ("робот" in normalized_text or "гуманоид" in normalized_text) and any(
        marker in normalized_text for marker in ("борьб", "турнир", "ринге", "драк")
    ):
        return "Роботы устроили драку на ринге — реакция Хесуса"
    if any(marker in normalized_text for marker in ("wildberries", "вайлдбер", "валдырис")):
        if "дрон" in normalized_text or "склад" in normalized_text:
            return "Что скрывалось на складе Wildberries? — реакция Хесуса"
        return "Что произошло с Wildberries? — реакция Хесуса"
    has_fedorov = "федоров" in normalized_text or "фёдоров" in normalized_text
    if has_fedorov and any(marker in normalized_text for marker in ("что за люди", "кто это")):
        return "Кто заменит Фёдорова и что будет дальше? — реакция Хесуса"
    if has_fedorov and "65 тысяч" in normalized_text and "зрител" in normalized_text:
        return "65 тысяч зрителей пришли на брифинг Фёдорова — Хесус удивлён"
    if has_fedorov and "играет на руку" in normalized_text:
        return "Почему отставка Фёдорова играет на руку Кремлю? — Хесус"
    if "мобилизац" in normalized_text and "конфликт" in normalized_text:
        return "Всеобщая мобилизация усилит конфликт? — реакция Хесуса"
    if has_fedorov and "министр" in normalized_text:
        return "Фёдоров останется министром обороны? — реакция Хесуса"
    if "65 тысяч" in normalized_text and "зрител" in normalized_text:
        return "Почему 65 тысяч зрителей стали проблемой? — Хесус"
    if "украин" in normalized_text and "решени" in normalized_text:
        return "Что изменит это решение для Украины? — реакция Хесуса"
    if "дальше" in normalized_text and "решени" in normalized_text:
        return "Какие решения будут дальше? — реакция Хесуса"
    if "запрет" in normalized_text and "фей" in normalized_text and "бред" in normalized_text:
        return "Абсурдный запрет: Хесус надеется, что это фейк"
    keywords = _keywords(text)
    candidates = _sentence_candidates(text)
    if not candidates:
        topic = " и ".join(keywords[:2]) or "главную тему стрима"
        return _limit_words(f"Хесус разбирает {topic}", 92)
    keyword_set = set(keywords)

    def score(sentence: str) -> float:
        normalized = normalize(sentence)
        words = normalized.split()
        value = 0.0
        value += 2.5 if "?" in sentence else 0.0
        value += 2.0 * sum(marker in normalized for marker in REACTION_WORDS)
        value += 1.7 * sum(marker in normalized for marker in TENSION_WORDS)
        value += 1.5 if re.search(r"\b[A-ZА-ЯЁ][a-zа-яё]{3,}\b", sentence) else 0.0
        value += 1.4 * sum(_topic_present(keyword, normalized) for keyword in keyword_set)
        value += 1.0 if 6 <= len(words) <= 16 else 0.0
        value -= 2.0 if words and words[0] in {"ну", "ладно", "короче", "вот"} else 0.0
        return value

    hook = max(candidates, key=score).strip()
    hook_words = hook.split()
    if len(hook_words) > 16:
        best_window = hook_words[:10]
        best_window_score = -1.0
        for size in range(7, 15):
            for start in range(0, len(hook_words) - size + 1):
                window = hook_words[start:start + size]
                normalized_window = normalize(" ".join(window))
                window_score = (
                    2.0 * sum(marker in normalized_window for marker in REACTION_WORDS)
                    + 1.7 * sum(marker in normalized_window for marker in TENSION_WORDS)
                    + 1.4 * sum(
                        _topic_present(keyword, normalized_window) for keyword in keyword_set
                    )
                    - 0.08 * size
                    - (0.8 if window[0] and window[0][0].islower() else 0.0)
                )
                if window_score > best_window_score:
                    best_window, best_window_score = window, window_score
        hook = " ".join(best_window)
    hook = _brand_safe(hook).strip().rstrip(",.;:")
    lead_topic = keywords[0] if keywords else ""
    display_topic = {"max": "MAX", "фсб": "ФСБ"}.get(lead_topic, lead_topic.capitalize())
    topic_stems = TAG_STEMS.get(lead_topic, (lead_topic,))
    hook_starts_with_topic = any(normalize(hook).startswith(stem) for stem in topic_stems)
    if lead_topic and not hook_starts_with_topic and len(hook) <= 68:
        hook = f"{display_topic}: {hook[0].lower()}{hook[1:]}"
    if hook.endswith("?"):
        suffix = " | Хесус"
        title = f"{_limit_words(hook, 75 - len(suffix))}{suffix}"
    elif "хесус" in hook.casefold():
        title = _limit_words(hook, 75)
    else:
        suffix = " — реакция Хесуса"
        title = f"{_limit_words(hook, 75 - len(suffix))}{suffix}"
    return title


def build_publication_metadata(text: str, platform: str) -> dict:
    """Generate grounded titles and topic hashtags locally, without inventing new facts."""
    content = _remove_non_content(text)
    title = _smart_title(content)
    topic_tags = _keywords(content)[:3]
    format_tags = _format_tags(content)[:1]
    platform_tag = {
        "youtube": "shorts", "tiktok": "тикток", "instagram": "reels",
    }.get(platform, "shorts")
    tags = list(dict.fromkeys(
        (*BASE_TAGS, *topic_tags, *format_tags, "стримнарезка", platform_tag)
    ))[:8]
    # Every exported clip is a HESUS stream reaction.  These two grounded
    # fallbacks keep sparse transcripts inside the requested 5-8 tag range.
    for fallback in ("реакция", "стрим"):
        if len(tags) >= 5:
            break
        if fallback not in tags:
            tags.append(fallback)
    hashtags = " ".join(f"#{tag}" for tag in tags)
    caption = f"{title}\n\n{hashtags}"
    privacy = "SELF_ONLY" if platform == "tiktok" else "public"
    return {
        "display_title": title,
        "title": caption if platform in {"tiktok", "instagram"} else title[:100],
        "description": caption,
        "tags": tags,
        "privacy": privacy,
        "topic_cluster": topic_cluster(content),
        "hook_type": infer_hook_type(content),
        "virality_config_version": "shorts-studio-virality-v1",
    }
