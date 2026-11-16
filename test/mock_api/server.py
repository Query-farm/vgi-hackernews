#!/usr/bin/env python3
"""A local Hacker News Firebase API for test/sql/*.test, replaying recorded responses.

Serves the hash-verified recorded responses in notebooks/fixtures/*.json.gz at
their real ``/v0/...`` paths, so the SQL suite is offline and deterministic:

* ``ranking-pack``: ``topstories.json`` and its 500 items (2026-11-14 capture)
* ``thread-pack``: item 8863 (Dropbox's launch post), its replies through
  depth 2, and ``user/dhouston.json``
* ``recent-pack``: ``maxitem.json`` and the 100 ids below it

The API's other feeds and ``updates.json`` were not recorded; they are derived
here from the recorded items, deterministically:

* ``newstories``: the top-story ids, newest (highest id) first
* ``beststories``: the top stories by score, highest first (ties by id)
* ``askstories`` / ``showstories``: top stories titled "Ask HN:" / "Show HN:"
* ``jobstories``: top-list items of type ``job``
* ``updates.json``: the first five top-story ids and the profile ``dhouston``

An id or username outside the recordings answers ``null``, as the real API
does for a missing item. Stdlib only: it runs under ``uv run --no-project``.

Usage::

    python3 test/mock_api/server.py            # prints PORT:<n> once listening
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import gzip
import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

FIXTURES = Path(__file__).resolve().parents[2] / "notebooks" / "fixtures"
PACKS = ("ranking-pack.json.gz", "thread-pack.json.gz", "recent-pack.json.gz")


def load() -> dict[str, bytes]:
    """Every recorded body by its ``/v0/...`` path, plus the derived feeds."""
    bodies: dict[str, bytes] = {}
    for pack in PACKS:
        for entry in json.loads(gzip.decompress((FIXTURES / pack).read_bytes()))["entries"]:
            body = base64.b64decode(entry["body_base64"])
            if hashlib.sha256(body).hexdigest() != entry["sha256"]:
                raise ValueError(f"recorded response checksum mismatch: {pack} {entry['path']}")
            bodies["/v0/" + entry["path"]] = body

    top: list[int] = json.loads(bodies["/v0/topstories.json"])
    items = {i: json.loads(bodies[f"/v0/item/{i}.json"]) for i in top}

    def titled(prefix: str) -> list[int]:
        return [i for i in top if (items[i] or {}).get("title", "").startswith(prefix)]

    derived: dict[str, object] = {
        "newstories.json": sorted(top, reverse=True),
        "beststories.json": sorted(top, key=lambda i: (-(items[i] or {}).get("score", 0), i)),
        "askstories.json": titled("Ask HN:"),
        "showstories.json": titled("Show HN:"),
        "jobstories.json": [i for i in top if (items[i] or {}).get("type") == "job"],
        "updates.json": {"items": top[:5], "profiles": ["dhouston"]},
    }
    for name, value in derived.items():
        bodies["/v0/" + name] = json.dumps(value).encode()
    return bodies


class Handler(BaseHTTPRequestHandler):
    """Serve recorded bodies; ``null`` for a missing item or user; 404 otherwise."""

    bodies: dict[str, bytes] = {}

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        """Log to stderr only, so stdout carries nothing except the ``PORT:`` line."""
        sys.stderr.write("mock_api: " + (format % args) + "\n")

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        """Handle GET requests."""
        path = urlsplit(self.path).path
        body = self.bodies.get(path)
        status = 200
        if body is None:
            if path.startswith(("/v0/item/", "/v0/user/")) and path.endswith(".json"):
                body = b"null"
            else:
                status, body = 404, json.dumps({"error": f"no route for {path}"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    """A listen backlog large enough for the worker's concurrent item fetches."""

    request_queue_size = 128
    daemon_threads = True


def main() -> None:
    """Start the server and print ``PORT:<n>`` once it is listening."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    Handler.bodies = load()
    server = Server((args.host, args.port), Handler)
    print(f"PORT:{server.server_address[1]}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
