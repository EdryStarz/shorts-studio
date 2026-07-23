"""Publish selected local Shorts Studio clips through a short-lived HTTPS tunnel."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import threading
import time

import httpx

from app.core.secrets import decrypt_credentials
from temporary_media_server import make_handler
from http.server import ThreadingHTTPServer


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cloudflared", type=Path, required=True)
    parser.add_argument("--publication", action="append", required=True)
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--caption-offset", type=int, default=0)
    return parser.parse_args()


def api_json(response: httpx.Response, fallback: str) -> dict:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.is_error:
        error = payload.get("error", {})
        raise RuntimeError(f"Instagram {error.get('code', response.status_code)}: {error.get('message', fallback)}")
    return payload


def wait_for_tunnel(process: subprocess.Popen[str]) -> str:
    pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    deadline = time.monotonic() + 60
    assert process.stdout is not None
    public_url = ""
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            raise RuntimeError("temporary HTTPS tunnel exited before becoming ready")
        match = pattern.search(line)
        if match:
            public_url = match.group(0)
        if public_url and "Registered tunnel connection" in line:
            threading.Thread(target=lambda: list(process.stdout), daemon=True).start()
            # Quick tunnels can announce their connector before the public
            # hostname has propagated to every edge used by Meta's fetcher.
            time.sleep(10)
            return public_url
    raise TimeoutError("temporary HTTPS tunnel did not become ready")


def publish_one(base_url: str, secret: str, clip_id: str, caption: str, token: str, user_id: str) -> str:
    video_url = f"{base_url}/{secret}/{clip_id}.mp4"
    with httpx.Client(timeout=45, follow_redirects=True) as client:
        # The local machine can use a DNS resolver that blocks the ephemeral
        # trycloudflare hostname even though Meta's fetcher can reach it. The
        # tunnel process has already confirmed readiness, so hand the URL to
        # Instagram directly and let the container status report fetch errors.
        created = api_json(client.post(f"https://graph.instagram.com/{user_id}/media", data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": token,
        }), "could not create Reel container")
        container_id = created["id"]
        for attempt in range(48):
            status = api_json(client.get(f"https://graph.instagram.com/{container_id}", params={
                "fields": "status_code,status",
                "access_token": token,
            }), "could not read Reel processing status")
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code in {"ERROR", "EXPIRED"}:
                raise RuntimeError(json.dumps(status, ensure_ascii=False))
            if attempt == 47:
                raise TimeoutError("Instagram did not finish processing the Reel within 4 minutes")
            time.sleep(5)
        published = api_json(client.post(f"https://graph.instagram.com/{user_id}/media_publish", data={
            "creation_id": container_id,
            "access_token": token,
        }), "could not publish Reel")
        return published["id"]


def main() -> None:
    args = arguments()
    database = args.database.resolve(strict=True)
    root = args.root.resolve(strict=True)
    cloudflared = args.cloudflared.resolve(strict=True)
    connection = sqlite3.connect(database, timeout=30)
    rows = connection.execute(
        "select id,clip_id,publish_metadata from publications where id in (%s) and platform='instagram'" %
        ",".join("?" * len(args.publication)), args.publication,
    ).fetchall()
    ordered = {
        publication_id: (clip_id, json.loads(metadata or "{}"))
        for publication_id, clip_id, metadata in rows
    }
    if any(publication_id not in ordered for publication_id in args.publication):
        raise RuntimeError("one or more Instagram publications were not found")
    account_row = connection.execute(
        "select encrypted_credentials from connected_accounts where platform='instagram' and revoked=0",
    ).fetchone()
    if not account_row:
        raise RuntimeError("connected Instagram account was not found")
    credentials = decrypt_credentials(account_row[0])

    secret = secrets.token_urlsafe(32)
    allowed = {ordered[item][0] for item in args.publication}
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(root, secret, allowed))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    tunnel = subprocess.Popen(
        [str(cloudflared), "tunnel", "--url", f"http://127.0.0.1:{args.port}", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", creationflags=flags,
    )
    try:
        base_url = wait_for_tunnel(tunnel)
        print("temporary_https_ready", flush=True)
        for publication_id in args.publication:
            clip_id, metadata = ordered[publication_id]
            try:
                post_id = publish_one(
                    base_url, secret, clip_id,
                    metadata.get("title") or metadata.get("description") or "Shorts Studio",
                    credentials["access_token"], credentials["user_id"],
                )
                with connection:
                    connection.execute(
                        "update publications set status='published',attempts=attempts+1,platform_post_id=?,error_log='[]' where id=?",
                        (post_id, publication_id),
                    )
                print(publication_id, "published", post_id, flush=True)
            except Exception as exc:
                safe_error = str(exc)[:800]
                with connection:
                    connection.execute(
                        "update publications set status='failed',attempts=attempts+1,error_log=? where id=?",
                        (json.dumps([{"at": time.time(), "error": safe_error}], ensure_ascii=False), publication_id),
                    )
                print(publication_id, "failed", safe_error, flush=True)
                raise
    finally:
        server.shutdown()
        tunnel.terminate()
        try:
            tunnel.wait(timeout=10)
        except subprocess.TimeoutExpired:
            tunnel.kill()


if __name__ == "__main__":
    main()
