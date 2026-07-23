from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.api.schemas import TikTokPublicationRequest


def payload(**updates):
    value = {
        "clip_id": "clip-1",
        "account_id": "account-1",
        "scheduled_for": datetime.now(timezone.utc),
        "title": "Creator-edited title #shorts",
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "music_usage_confirmed": True,
        "user_confirmed": True,
    }
    value.update(updates)
    return value


def test_tiktok_review_requires_music_and_publish_consent():
    with pytest.raises(ValidationError, match="explicit creator consent"):
        TikTokPublicationRequest(**payload(music_usage_confirmed=False))


def test_tiktok_review_rejects_private_branded_content():
    with pytest.raises(ValidationError, match="branded content cannot be private"):
        TikTokPublicationRequest(**payload(
            privacy_level="SELF_ONLY",
            commercial_content=True,
            branded_content=True,
        ))
