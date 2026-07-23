from urllib.parse import urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse


callback_app = FastAPI(
    title="Shorts Studio Local OAuth Callback",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@callback_app.get("/api/oauth/instagram/callback")
def instagram_callback(request: Request) -> RedirectResponse:
    target = urlunsplit((
        "http",
        "127.0.0.1:8765",
        "/api/oauth/instagram/callback",
        request.url.query,
        "",
    ))
    return RedirectResponse(target, status_code=302)


@callback_app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}

