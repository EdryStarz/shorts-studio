from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.secrets import decrypt_credentials, encrypt_credentials
from app.database.models import OAuthApplication
from app.database.session import SessionLocal


PLATFORMS = ("youtube", "tiktok", "instagram")
SETUP_URLS = {
    "youtube": "https://console.cloud.google.com/apis/credentials",
    "tiktok": "https://developers.tiktok.com/apps/",
    "instagram": "https://developers.facebook.com/apps/",
}
REQUIREMENTS = {
    "youtube": "Создайте OAuth Client типа Desktop app и включите YouTube Data API v3.",
    "tiktok": "Добавьте Login Kit и Content Posting API, затем зарегистрируйте Redirect URI.",
    "instagram": "Нужен Instagram Professional и приложение Meta с Instagram API.",
}


@dataclass(frozen=True)
class OAuthConfig:
    platform: str
    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def configured(self) -> bool:
        if self.platform == "youtube":
            return bool(self.client_id)
        return bool(self.client_id and self.client_secret)


def _environment_config(platform: str) -> OAuthConfig:
    settings = get_settings()
    values = {
        "youtube": (
            settings.youtube_client_id, settings.youtube_client_secret,
            settings.youtube_redirect_uri,
        ),
        "tiktok": (
            settings.tiktok_client_key, settings.tiktok_client_secret,
            settings.tiktok_redirect_uri,
        ),
        "instagram": (
            settings.instagram_client_id, settings.instagram_client_secret,
            settings.instagram_redirect_uri,
        ),
    }
    try:
        client_id, secret, redirect_uri = values[platform]
    except KeyError as exc:
        raise ValueError(f"unsupported platform: {platform}") from exc
    return OAuthConfig(platform, client_id.strip(), secret.strip(), redirect_uri)


def get_oauth_config(platform: str, db: Session | None = None) -> OAuthConfig:
    fallback = _environment_config(platform)

    def load(session: Session) -> OAuthConfig:
        stored = session.get(OAuthApplication, platform)
        if not stored:
            return fallback
        secret = ""
        if stored.encrypted_client_secret:
            secret = decrypt_credentials(stored.encrypted_client_secret).get("client_secret", "")
        return OAuthConfig(platform, stored.client_id.strip(), secret.strip(), fallback.redirect_uri)

    if db is not None:
        return load(db)
    with SessionLocal() as session:
        return load(session)


def save_oauth_config(
    db: Session, platform: str, client_id: str, client_secret: str | None,
) -> OAuthConfig:
    current = db.get(OAuthApplication, platform)
    secret_value = client_secret.strip() if client_secret is not None else None
    if secret_value is None and current and current.encrypted_client_secret:
        encrypted = current.encrypted_client_secret
    elif secret_value:
        encrypted = encrypt_credentials({"client_secret": secret_value})
    else:
        encrypted = ""
    if current:
        current.client_id = client_id.strip()
        current.encrypted_client_secret = encrypted
    else:
        current = OAuthApplication(
            platform=platform, client_id=client_id.strip(), encrypted_client_secret=encrypted,
        )
        db.add(current)
    db.commit()
    return get_oauth_config(platform, db)
