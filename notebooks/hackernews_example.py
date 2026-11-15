"""Verified official recordings and bounded notebook parameters; SQL does the analysis."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def selection(value: object, name: str, choices: set[int]) -> None:
    """Reject booleans and unsupported windows before any remote work."""
    if type(value) is not int or value not in choices:
        raise ValueError(f"{name} must be one of {sorted(choices)}")


class _Server(ThreadingHTTPServer):
    request_queue_size = 128


class Source:
    """Serve only hash-verified complete recorded response bytes, or explicit live data."""

    def __init__(self, mode: str, scenario: str) -> None:
        if mode not in {"replay", "live"}:
            raise ValueError("DATA_MODE must be replay or live")
        if scenario not in {"ranking", "thread", "recent"}:
            raise ValueError("Unknown recording")
        self.mode = mode
        self.previous = os.environ.get("HN_BASE_URL")
        self.requests: list[str] = []
        self.rejected: list[str] = []
        self.server = None
        self.thread = None
        self.manifest = json.loads(
            gzip.decompress((Path(__file__).parent / "fixtures" / f"{scenario}-pack.json.gz").read_bytes())
        )
        bodies = {}
        for entry in self.manifest["entries"]:
            raw = base64.b64decode(entry["body_base64"], validate=True)
            if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise ValueError(f"Recorded response checksum mismatch: {entry['path']}")
            path = "/v0/" + entry["path"]
            if path in bodies:
                raise ValueError("Duplicate recorded request path")
            bodies[path] = raw
        self.bodies = bodies
        self.started_at = datetime.now(UTC).isoformat()
        if mode == "live":
            os.environ["HN_BASE_URL"] = "https://hacker-news.firebaseio.com/v0"
            return
        requests, rejected = self.requests, self.rejected

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                requests.append(self.path)
                if parsed.path not in bodies or parsed.query:
                    rejected.append(self.path)
                    self.send_error(400, "Request outside recorded coverage")
                    return
                body = bodies[parsed.path]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        os.environ["HN_BASE_URL"] = f"http://127.0.0.1:{self.server.server_port}/v0"

    def as_of(self) -> str:
        if self.mode == "replay":
            return self.manifest["capture_completed_at_utc"]
        return datetime.now(UTC).isoformat()

    def audit(self) -> dict[str, object]:
        if self.rejected:
            raise AssertionError("Unrecorded request attempted")
        return {
            "mode": self.mode,
            "requests": len(self.requests),
            "distinct_paths": len(set(self.requests)),
            "rejected": len(self.rejected),
            "live_network_instrumented": False,
        }

    def close(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        if self.previous is None:
            os.environ.pop("HN_BASE_URL", None)
        else:
            os.environ["HN_BASE_URL"] = self.previous
