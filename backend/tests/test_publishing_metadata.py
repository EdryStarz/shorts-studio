from app.publishing_metadata import build_publication_metadata


def test_builds_topic_title_and_youtube_tags():
    metadata = build_publication_metadata(
        "Если у ФСБ есть переписка, почему выложили только часть голосового без контекста?",
        "youtube",
    )
    assert "голосового" in metadata["title"]
    assert metadata["title"].endswith("| Хесус")
    assert "фсб" in metadata["tags"]
    assert "голосовое" in metadata["tags"]
    assert "#jesusavgn" in metadata["description"]
    assert metadata["privacy"] == "public"


def test_tiktok_title_includes_hashtags_and_private_privacy_for_unaudited_app():
    metadata = build_publication_metadata(
        "Появляется ошибка, удалить нельзя. Дуров, MAX вообще не работает.",
        "tiktok",
    )
    assert metadata["title"].startswith("MAX:")
    assert "#max" in metadata["title"]
    assert "#дуров" in metadata["title"]
    assert "#shorts" not in metadata["title"]
    assert {"jesusavgn", "хесус"}.issubset(metadata["tags"])
    assert "hesusavgn" not in metadata["tags"]
    assert 5 <= len(metadata["tags"]) <= 8
    assert metadata["topic_cluster"] == "max"
    assert metadata["privacy"] == "SELF_ONLY"


def test_fallback_is_not_empty():
    metadata = build_publication_metadata("Очень неожиданный момент прямо на стриме", "instagram")
    assert "реакция Хесуса" in metadata["title"]
    assert metadata["tags"]


def test_uses_a_clean_semantic_title_instead_of_a_profanity_quote():
    metadata = build_publication_metadata(
        "Ну тогда запретить им участие. Такой бред. Надеюсь, это фейк. Это что такое?",
        "instagram",
    )
    assert metadata["title"].startswith(
        "Абсурдный запрет: Хесус надеется, что это фейк"
    )
    assert "#запрет" in metadata["title"]
    assert "#фейк" in metadata["title"]


def test_advertisement_is_not_used_for_title_or_hashtags():
    metadata = build_publication_metadata(
        "Почему нельзя выйти из квартиры? Мы разыгрываем Nissan. Покупай игры, "
        "пополняй Steam и используй промокод PlayRock.",
        "youtube",
    )
    combined = f"{metadata['title']} {' '.join(metadata['tags'])}".casefold()
    assert "playrock" not in combined
    assert "промокод" not in combined
    assert "разыгрываем" not in combined


def test_aliases_in_different_cases_do_not_duplicate_hashtags():
    metadata = build_publication_metadata(
        "Фёдоров рассказал о решении. Отставка Фёдорова изменила планы министра.",
        "youtube",
    )
    fedorov_tags = [tag for tag in metadata["tags"] if tag.startswith(("федоров", "фёдоров"))]
    assert fedorov_tags == ["федоров"]


def test_clip_specific_topics_get_clean_titles_and_tags():
    bank = build_publication_metadata(
        "Туалетная бумага стоит 9,99 рублей, а акции Совкомбанка — 9 рублей.",
        "youtube",
    )
    assert bank["display_title"] == "Акции Совкомбанка дешевле туалетной бумаги — реакция Хесуса"
    assert "совкомбанк" in bank["tags"]

    robots = build_publication_metadata(
        "Первый глобальный турнир по борьбе роботов-гуманоидов прошёл в Китае.",
        "youtube",
    )
    assert robots["display_title"] == "Роботы устроили драку на ринге — реакция Хесуса"
    assert "роботы" in robots["tags"]

    warehouse = build_publication_metadata(
        "На Wildberries продавались детали оружия, рядом горит склад с дронами.",
        "youtube",
    )
    assert warehouse["display_title"] == "Что скрывалось на складе Wildberries? — реакция Хесуса"
    assert {"wildberries", "дроны", "склад"}.issubset(warehouse["tags"])
