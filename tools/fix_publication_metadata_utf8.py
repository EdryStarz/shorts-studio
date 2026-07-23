from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import httpx
from sqlalchemy import select


ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ["LOCALAPPDATA"]) / "ShortsStudio"
os.environ.update({
    "STORAGE_ROOT": str(DATA),
    "DATABASE_URL": f"sqlite:///{(DATA / 'shorts.db').as_posix()}",
    "TOKEN_ENCRYPTION_KEY": (DATA / "token.key").read_text(encoding="ascii").strip(),
})
sys.path.insert(0, str(ROOT / "backend"))

from app.core.secrets import decrypt_credentials, encrypt_credentials  # noqa: E402
from app.database.models import Clip, ConnectedAccount, Publication  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402
from app.platform_connectors.youtube import YouTubePublisher  # noqa: E402


SOURCE_ID = "0779314f-a06c-4bf3-8959-cfa19af7edae"
CONTENT = {
    "1498db0e-a8b2-4e1e-a09f-4f6067a267fa": (
        "Решение Зеленского играет на руку Путину? Реакция Хесуса",
        ["украина", "зеленский", "путин", "федоров"],
    ),
    "f1397b60-2822-4418-b574-3ad4b09c9654": (
        "Почему убрали Федорова, а Сырский остался? Хесус в шоке",
        ["украина", "федоров", "сырский", "минобороны"],
    ),
    "64f18f0b-1ab1-47a6-b387-2e645889a2fe": (
        "Министра выбирали наугад? Хесус о кадровом хаосе",
        ["украина", "федоров", "минобороны", "новости"],
    ),
    "8887c1b0-d526-4ef5-badb-03d767683e4e": (
        "Война или рейтинги: почему боятся популярности Федорова?",
        ["украина", "федоров", "зеленский", "политика"],
    ),
    "428f831f-c0d8-48ae-bf50-11276649f148": (
        "Почему мобилизацию нельзя «просто улучшить» — разбор Хесуса",
        ["украина", "мобилизация", "тцк", "разбор"],
    ),
    "c2d5f2d0-f178-4614-8a45-3e156d4aef63": (
        "Зачем Федорову пост премьера или советника? Реакция Хесуса",
        ["украина", "федоров", "зеленский", "реакция"],
    ),
}
BASE_TAGS = ["shorts", "хесус", "jesusavgn", "реакция"]


def metadata(platform: str, title: str, topic_tags: list[str], previous: dict) -> dict:
    tags = list(dict.fromkeys([*BASE_TAGS, *topic_tags]))
    caption = f"{title}\n\n" + " ".join(f"#{tag}" for tag in tags)
    if platform == "youtube":
        return {
            "title": title,
            "description": caption,
            "tags": tags,
            "privacy": previous.get("privacy", "public"),
        }
    if platform == "instagram":
        return {
            "title": caption,
            "description": caption,
            "tags": tags,
            "privacy": "public",
        }
    return {
        **previous,
        "title": caption,
        "description": caption,
        "tags": tags,
    }


def update_youtube_video(credentials: dict, video_id: str, data: dict) -> None:
    headers = {"Authorization": f"Bearer {credentials['access_token']}"}
    current = httpx.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "snippet", "id": video_id},
        headers=headers,
        timeout=30,
    )
    current.raise_for_status()
    items = current.json().get("items", [])
    if not items:
        raise RuntimeError(f"YouTube video {video_id} was not found")
    snippet = items[0]["snippet"]
    snippet.update({
        "title": data["title"],
        "description": data["description"],
        "tags": data["tags"],
    })
    updated = httpx.put(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"part": "snippet"},
        headers=headers,
        json={"id": video_id, "snippet": snippet},
        timeout=30,
    )
    if updated.is_error:
        raise RuntimeError(
            f"YouTube metadata update failed ({updated.status_code}): {updated.text}"
        )


def main() -> None:
    with SessionLocal() as db:
        clips = db.scalars(select(Clip).where(Clip.source_id == SOURCE_ID)).all()
        clip_ids = {clip.id for clip in clips}
        publications = db.scalars(
            select(Publication).where(Publication.clip_id.in_(clip_ids))
        ).all()
        published_youtube: list[tuple[str, dict]] = []
        for publication in publications:
            title, topic_tags = CONTENT[publication.clip_id]
            corrected = metadata(
                publication.platform,
                title,
                topic_tags,
                publication.publish_metadata or {},
            )
            publication.publish_metadata = corrected
            if publication.platform == "youtube" and publication.platform_post_id:
                published_youtube.append((publication.platform_post_id, corrected))
        db.commit()

        account = db.scalar(
            select(ConnectedAccount).where(
                ConnectedAccount.platform == "youtube",
                ConnectedAccount.revoked.is_(False),
            )
        )
        if not account:
            raise RuntimeError("Connected YouTube account was not found")
        publisher = YouTubePublisher()
        credentials = decrypt_credentials(account.encrypted_credentials)
        refreshed = publisher.refresh_credentials(credentials)
        if refreshed != credentials:
            account.encrypted_credentials = encrypt_credentials(refreshed)
            db.commit()
        for video_id, corrected in published_youtube:
            update_youtube_video(refreshed, video_id, corrected)
            print(json.dumps({"video_id": video_id, "title": corrected["title"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
