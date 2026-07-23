"""Serve an explicit set of rendered clips through an unguessable URL prefix."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import ssl


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--secret", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--clip", action="append", required=True)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--cors-origin")
    return parser.parse_args()


def make_handler(root: Path, secret: str, allowed: set[str], cors_origin: str | None = None):
    route = re.compile(rf"^/{re.escape(secret)}/([0-9a-f-]{{36}})\.mp4$")

    class MediaHandler(BaseHTTPRequestHandler):
        server_version = "ShortsStudioMedia/1"

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._cors_headers()
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()

        def do_HEAD(self) -> None:  # noqa: N802
            self._serve(body=False)

        def do_GET(self) -> None:  # noqa: N802
            self._serve(body=True)

        def _serve(self, body: bool) -> None:
            match = route.fullmatch(self.path.split("?", 1)[0])
            if not match or match.group(1) not in allowed:
                self.send_error(404)
                return
            media = root / match.group(1) / "short.mp4"
            if not media.is_file():
                self.send_error(404)
                return

            size = media.stat().st_size
            start, end = 0, size - 1
            range_header = self.headers.get("Range", "")
            if range_header.startswith("bytes="):
                try:
                    first, last = range_header[6:].split("-", 1)
                    start = int(first) if first else 0
                    end = min(int(last), size - 1) if last else size - 1
                    if start < 0 or start > end:
                        raise ValueError
                except ValueError:
                    self.send_error(416)
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            length = end - start + 1
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self._cors_headers()
            self.end_headers()
            if not body:
                return
            with media.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def _cors_headers(self) -> None:
            if cors_origin:
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Vary", "Origin")

        def log_message(self, fmt: str, *args: object) -> None:
            # Do not write the secret URL path to logs.
            print(f"{self.client_address[0]} {args[1] if len(args) > 1 else ''}", flush=True)

    return MediaHandler


def main() -> None:
    args = parse_args()
    root = args.root.resolve(strict=True)
    handler = make_handler(root, args.secret, set(args.clip))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    if args.cert or args.key:
        if not args.cert or not args.key:
            raise ValueError("both --cert and --key are required for HTTPS")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert.resolve(strict=True), args.key.resolve(strict=True))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
