import hashlib

import httpx
import pytest

from app.platform_connectors.mock import MockPublisher
from app.platform_connectors.oauth_config import OAuthConfig, get_oauth_config, save_oauth_config
from app.platform_connectors.youtube import YouTubePublisher
from app.platform_connectors.tiktok import TikTokPublisher
from app.platform_connectors.instagram import InstagramPublisher
from app.database.session import SessionLocal
from app.main import app
from fastapi.testclient import TestClient


def test_mock_publisher_full_contract(tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"video")
    publisher = MockPublisher("youtube")
    credentials = publisher.exchange_code("approved")
    assert publisher.validate_account(credentials)["valid"]
    upload = publisher.upload_media(media, credentials, {"title": "Test"})
    result = publisher.publish(upload, credentials, {"title": "Test"})
    assert publisher.get_status(result.post_id, credentials) == "published"
    assert result.post_id.startswith("mock-")


def test_oauth_application_config_is_saved_without_exposing_secret(monkeypatch):
    import app.platform_connectors.oauth_config as module

    monkeypatch.setattr(module, "encrypt_credentials", lambda value: f"encrypted:{value['client_secret']}")
    monkeypatch.setattr(
        module, "decrypt_credentials",
        lambda value: {"client_secret": value.removeprefix("encrypted:")},
    )
    with SessionLocal() as db:
        saved = save_oauth_config(db, "tiktok", "client-key", "client-secret")
        loaded = get_oauth_config("tiktok", db)
    assert saved.configured
    assert loaded.client_id == "client-key"
    assert loaded.client_secret == "client-secret"


def test_youtube_desktop_oauth_allows_optional_secret_and_requests_account_scope(monkeypatch):
    import app.platform_connectors.youtube as module

    monkeypatch.setattr(
        module, "get_oauth_config",
        lambda platform: OAuthConfig(
            platform, "desktop-client", "", "http://127.0.0.1:8765/api/oauth/youtube/callback",
        ),
    )
    url = YouTubePublisher().authorize("signed-state")
    assert "youtube.force-ssl" in url
    assert "youtube.upload" in url
    assert "youtube.readonly" in url
    assert "desktop-client" in url


def test_youtube_scheduled_upload_is_private_until_publish_time(monkeypatch, tmp_path):
    import app.platform_connectors.youtube as module

    media = tmp_path / "short.mp4"
    media.write_bytes(b"video")
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs["json"])
        return httpx.Response(
            200, request=httpx.Request("POST", url),
            headers={"Location": "https://upload.youtube.test/video"},
        )

    monkeypatch.setattr(module.httpx, "post", fake_post)
    monkeypatch.setattr(
        module.httpx, "put",
        lambda url, **kwargs: httpx.Response(
            200, request=httpx.Request("PUT", url), json={"id": "scheduled-video"},
        ),
    )
    upload = YouTubePublisher().upload_media(media, {"access_token": "token"}, {
        "title": "Scheduled Short", "privacy": "public",
        "publish_at": "2026-07-16T14:00:00Z",
    })
    assert upload["id"] == "scheduled-video"
    assert captured["status"] == {
        "privacyStatus": "private", "publishAt": "2026-07-16T14:00:00Z",
    }


def test_tiktok_desktop_oauth_uses_pkce_and_reuses_verifier_for_exchange(monkeypatch):
    import app.platform_connectors.tiktok as module

    monkeypatch.setattr(
        module, "get_oauth_config",
        lambda platform: OAuthConfig(
            platform, "desktop-key", "desktop-secret",
            "http://127.0.0.1:8765/api/oauth/tiktok/callback",
        ),
    )
    posted = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "token"}

    def fake_post(url, *, data, timeout):
        posted.update(data)
        return Response()

    monkeypatch.setattr(module.httpx, "post", fake_post)
    publisher = TikTokPublisher()
    state = "tiktok.123.nonce.signature"
    url = publisher.authorize(state)
    verifier = publisher._code_verifier(state, "desktop-secret")
    assert "code_challenge_method=S256" in url
    assert hashlib.sha256(verifier.encode()).hexdigest() in url
    publisher.exchange_code("approved", state)
    assert posted["code_verifier"] == verifier


def test_tiktok_never_silently_changes_selected_privacy():
    with pytest.raises(ValueError, match="privacy"):
        TikTokPublisher._select_privacy("PUBLIC_TO_EVERYONE", ["SELF_ONLY"])
    assert TikTokPublisher._select_privacy(
        "PUBLIC_TO_EVERYONE", ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
    ) == "PUBLIC_TO_EVERYONE"


def test_tiktok_upload_uses_creator_selected_interactions(monkeypatch, tmp_path):
    import app.platform_connectors.tiktok as module

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    publisher = TikTokPublisher()
    monkeypatch.setattr(publisher, "validate_account", lambda credentials: {
        "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
    })
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs["json"]["post_info"])
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "data": {"publish_id": "post-1", "upload_url": "https://upload.tiktok.test/video"},
            "error": {"code": "ok", "message": ""},
        })

    monkeypatch.setattr(module.httpx, "post", fake_post)
    monkeypatch.setattr(
        module.httpx,
        "put",
        lambda url, **kwargs: httpx.Response(200, request=httpx.Request("PUT", url)),
    )
    publisher.upload_media(media, {"access_token": "token"}, {
        "title": "Editable title #creator",
        "privacy": "PUBLIC_TO_EVERYONE",
        "allow_comment": True,
        "allow_duet": False,
        "allow_stitch": True,
        "brand_content_toggle": False,
        "brand_organic_toggle": False,
    })
    assert captured["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert captured["disable_comment"] is False
    assert captured["disable_duet"] is True
    assert captured["disable_stitch"] is False


def test_client_rate_limit_is_not_retried_in_a_burst():
    publisher = MockPublisher("tiktok")
    calls = 0

    def rate_limited():
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError("rate limited", request=request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        publisher.retry(rate_limited)
    assert calls == 1


def test_instagram_uploads_local_reel_with_resumable_container(monkeypatch, tmp_path):
    import app.platform_connectors.instagram as module

    media = tmp_path / "reel.mp4"
    media.write_bytes(b"video-bytes")
    calls = []

    def response(method, url, status_code=200, payload=None):
        return httpx.Response(
            status_code,
            request=httpx.Request(method, url),
            json=payload or {},
        )

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/media"):
            return response("POST", url, payload={
                "id": "container-1",
                "uri": "https://rupload.facebook.com/ig-api-upload/v25.0/container-1",
            })
        if url.startswith("https://rupload.facebook.com/"):
            assert kwargs["headers"]["file_size"] == str(media.stat().st_size)
            assert kwargs["headers"]["Authorization"] == "OAuth token"
            return response("POST", url, payload={"success": True})
        raise AssertionError(url)

    monkeypatch.setattr(module.httpx, "post", fake_post)
    monkeypatch.setattr(
        module.httpx,
        "get",
        lambda url, **kwargs: response("GET", url, payload={"status_code": "FINISHED"}),
    )
    publisher = InstagramPublisher()
    upload = publisher.upload_media(
        media,
        {"access_token": "token", "user_id": "ig-user"},
        {"title": "Reel caption"},
    )
    assert upload == {"container_id": "container-1", "account_id": "ig-user"}
    assert calls[0][1]["data"]["upload_type"] == "resumable"
    assert calls[0][1]["data"]["media_type"] == "REELS"


def test_instagram_falls_back_to_temporary_https_when_resumable_is_unavailable(monkeypatch, tmp_path):
    import app.platform_connectors.instagram as module

    media = tmp_path / "short.mp4"
    media.write_bytes(b"video")

    monkeypatch.setattr(
        module.httpx,
        "post",
        lambda url, **kwargs: httpx.Response(
            400,
            request=httpx.Request("POST", url),
            json={"error": {"code": 100, "message": "The parameter video_url is required."}},
        ),
    )
    publisher = InstagramPublisher()
    monkeypatch.setattr(
        publisher,
        "_upload_via_temporary_https",
        lambda media, credentials, metadata, account_id: {
            "container_id": "fallback-container", "account_id": account_id,
        },
    )

    upload = publisher.upload_media(
        media,
        {"access_token": "token", "user_id": "ig-user"},
        {"title": "Reel caption"},
    )
    assert upload == {"container_id": "fallback-container", "account_id": "ig-user"}


def test_instagram_confirms_published_media_by_id_not_container_status(monkeypatch):
    import app.platform_connectors.instagram as module

    captured = {}

    def fake_get(url, **kwargs):
        captured.update(kwargs["params"])
        return httpx.Response(
            200, request=httpx.Request("GET", url), json={"id": "media-1"},
        )

    monkeypatch.setattr(module.httpx, "get", fake_get)
    assert InstagramPublisher().get_status("media-1", {"access_token": "token"}) == "FINISHED"
    assert captured["fields"] == "id"


def test_instagram_oauth_upgrades_to_long_lived_token(monkeypatch):
    import app.platform_connectors.instagram as module

    monkeypatch.setattr(
        module,
        "get_oauth_config",
        lambda platform: OAuthConfig(
            platform,
            "instagram-client",
            "instagram-secret",
            "https://localhost:8766/api/oauth/instagram/callback",
        ),
    )

    def response(method, url, payload):
        return httpx.Response(200, request=httpx.Request(method, url), json=payload)

    monkeypatch.setattr(
        module.httpx,
        "post",
        lambda url, **kwargs: response("POST", url, {"access_token": "short", "user_id": "ig-user"}),
    )
    monkeypatch.setattr(
        module.httpx,
        "get",
        lambda url, **kwargs: response("GET", url, {"access_token": "long", "expires_in": 5184000}),
    )
    credentials = InstagramPublisher().exchange_code("approved")
    assert credentials["access_token"] == "long"
    assert credentials["user_id"] == "ig-user"
    assert credentials["expires_in"] == 5184000
    assert credentials["expires_at"] > module.time.time() + 5183990


def test_instagram_oauth_defaults_missing_long_lived_expiry(monkeypatch):
    import app.platform_connectors.instagram as module

    monkeypatch.setattr(
        module,
        "get_oauth_config",
        lambda platform: OAuthConfig(
            platform,
            "instagram-client",
            "instagram-secret",
            "https://localhost:8766/api/oauth/instagram/callback",
        ),
    )

    def response(method, url, payload):
        return httpx.Response(200, request=httpx.Request(method, url), json=payload)

    monkeypatch.setattr(
        module.httpx,
        "post",
        lambda url, **kwargs: response("POST", url, {"access_token": "short", "user_id": "ig-user"}),
    )
    monkeypatch.setattr(
        module.httpx,
        "get",
        lambda url, **kwargs: response("GET", url, {"access_token": "long"}),
    )
    credentials = InstagramPublisher().exchange_code("approved")
    assert credentials["expires_in"] == 5184000
    assert credentials["expires_at"] > module.time.time() + 5183990


def test_oauth_config_api_never_returns_client_secret(monkeypatch):
    import app.platform_connectors.oauth_config as module

    monkeypatch.setattr(module, "encrypt_credentials", lambda value: f"encrypted:{value['client_secret']}")
    monkeypatch.setattr(
        module, "decrypt_credentials",
        lambda value: {"client_secret": value.removeprefix("encrypted:")},
    )
    with TestClient(app) as client:
        response = client.put("/api/oauth/tiktok/config", json={
            "client_id": "client-key", "client_secret": "do-not-return-this",
        })
        assert response.status_code == 200
        assert response.json()["configured"] is True
        assert "do-not-return-this" not in response.text
        configs = client.get("/api/oauth/config").json()
        assert next(item for item in configs if item["platform"] == "tiktok")["secret_configured"]
