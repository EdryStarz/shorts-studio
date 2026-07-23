from app.platform_connectors.base import PlatformPublisher
from app.platform_connectors.instagram import InstagramPublisher
from app.platform_connectors.mock import MockPublisher
from app.platform_connectors.tiktok import TikTokPublisher
from app.platform_connectors.youtube import YouTubePublisher


def get_publisher(platform: str, *, mock: bool = False) -> PlatformPublisher:
    if mock:
        return MockPublisher(platform)
    publishers: dict[str, type[PlatformPublisher]] = {
        "youtube": YouTubePublisher, "tiktok": TikTokPublisher, "instagram": InstagramPublisher,
    }
    try:
        return publishers[platform]()
    except KeyError as exc:
        raise ValueError(f"unsupported platform: {platform}") from exc

