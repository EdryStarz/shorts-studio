"""Upload one rendered Short as a YouTube server-scheduled publication."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from app.core.secrets import decrypt_credentials, encrypt_credentials
from app.platform_connectors.youtube import YouTubePublisher


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--clip", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--publish-at", required=True)
    args = parser.parse_args()

    connection = sqlite3.connect(args.database.resolve(strict=True), timeout=30)
    account = connection.execute(
        "select id,encrypted_credentials from connected_accounts "
        "where platform='youtube' and revoked=0",
    ).fetchone()
    media, publication_id = connection.execute(
        "select c.export_path,p.id from clips c join publications p "
        "on p.clip_id=c.id and p.platform='youtube' where c.id=?",
        (args.clip,),
    ).fetchone()

    publisher = YouTubePublisher()
    credentials = publisher.refresh_credentials(decrypt_credentials(account[1]))
    with connection:
        connection.execute(
            "update connected_accounts set encrypted_credentials=? where id=?",
            (encrypt_credentials(credentials), account[0]),
        )

    hashtags = " ".join(f"#{tag}" for tag in args.tag)
    metadata = {
        "title": args.title,
        "description": f"{args.title}\n\n{hashtags}",
        "tags": args.tag,
        "privacy": "public",
        "publish_at": args.publish_at,
    }
    upload = publisher.upload_media(Path(media), credentials, metadata)
    scheduled = datetime.fromisoformat(args.publish_at.replace("Z", "+00:00")).replace(tzinfo=None)
    with connection:
        connection.execute(
            "update publications set status='published',platform_post_id=?,scheduled_for=?,"
            "timezone_name='Asia/Tbilisi',attempts=0,error_log='[]',publish_metadata=? where id=?",
            (upload["id"], scheduled.isoformat(" "), json.dumps(metadata, ensure_ascii=False), publication_id),
        )
    print(upload["id"], flush=True)


if __name__ == "__main__":
    main()
