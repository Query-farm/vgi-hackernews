"""Shared fixtures: a hermetic connection pool, and a fake Hacker News to talk to.

The offline suite never touches the network. :class:`FakeSite` serves the API's
URL shapes from in-memory dictionaries through an ``httpx2.MockTransport``, and
records every path requested, so a test can assert both what a function
returned and what it cost.
"""

from __future__ import annotations

import json
import re
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pyarrow as pa
import pytest

from vgi_hackernews import hn_api


@pytest.fixture(autouse=True)
def _hermetic_connection_pool() -> Generator[None]:
    """Give every test a clean process-wide client.

    ``hn_api`` keeps one pooled client for the life of the worker. Without a
    reset, a client built against one test's mock transport would serve the
    next test, and test order would decide the outcome.
    """
    hn_api.reset_shared_client()
    yield
    hn_api.reset_shared_client()


_ITEM = re.compile(r"^/v0/item/(-?\d+)\.json$")
_USER = re.compile(r"^/v0/user/([^/]+)\.json$")


def _json(value: Any) -> httpx2.Response:
    """A JSON response, spelled out.

    ``Response(json=None)`` would send an empty body, where the real API sends
    the literal ``null`` for a missing item.
    """
    body = json.dumps(value).encode()
    return httpx2.Response(200, content=body, headers={"content-type": "application/json"})


@dataclass
class FakeSite:
    """An in-memory Hacker News, served at the real API's paths."""

    items: dict[int, dict[str, Any]] = field(default_factory=dict)
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    feeds: dict[str, list[int]] = field(default_factory=dict)
    updated_items: list[int] = field(default_factory=list)
    updated_users: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        self.requests.append(path)
        if match := _ITEM.match(path):
            return _json(self.items.get(int(match.group(1))))
        if match := _USER.match(path):
            return _json(self.users.get(match.group(1)))
        if path == "/v0/maxitem.json":
            return _json(max(self.items, default=0))
        if path == "/v0/updates.json":
            return _json({"items": self.updated_items, "profiles": self.updated_users})
        for name, endpoint in hn_api.FEEDS.items():
            if path == f"/v0{endpoint}":
                return _json(self.feeds.get(name, []))
        return httpx2.Response(404, text=f"no route for {path}")

    def item_requests(self) -> list[int]:
        return [int(m.group(1)) for p in self.requests if (m := _ITEM.match(p))]


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> FakeSite:
    """A fake Hacker News installed as the process-wide client."""
    fake = FakeSite()
    client = httpx2.Client(transport=httpx2.MockTransport(fake.handle))
    monkeypatch.setattr(hn_api, "shared_client", lambda: client)
    monkeypatch.setattr(hn_api, "_RETRY_BASE_SECONDS", 0.0)
    return fake


def story(item_id: int, **extra: Any) -> dict[str, Any]:
    """A plausible story payload."""
    return {
        "id": item_id,
        "type": "story",
        "by": f"user{item_id}",
        "time": 1_700_000_000 + item_id,
        "title": f"Story {item_id}",
        "url": f"https://example.com/{item_id}",
        "score": item_id,
        "descendants": 0,
        **extra,
    }


def comment(item_id: int, parent: int, **extra: Any) -> dict[str, Any]:
    """A plausible comment payload."""
    return {
        "id": item_id,
        "type": "comment",
        "by": f"user{item_id}",
        "time": 1_700_000_000 + item_id,
        "parent": parent,
        "text": f"comment {item_id}",
        **extra,
    }


# --------------------------------------------------------------------------
# Driving a function body without a worker
# --------------------------------------------------------------------------


@dataclass
class Collector:
    """Stands in for the framework's output collector."""

    batches: list[pa.RecordBatch] = field(default_factory=list)
    parent_rows: list[list[int] | None] = field(default_factory=list)
    cache_controls: list[Any] = field(default_factory=list)
    finished: bool = False

    def emit(self, batch: pa.RecordBatch, **kwargs: Any) -> None:
        assert not self.finished, "emitted after finish()"
        self.batches.append(batch)
        self.parent_rows.append(kwargs.get("parent_rows"))
        self.cache_controls.append(kwargs.get("cache_control"))

    def finish(self) -> None:
        self.finished = True

    def rows(self) -> list[dict[str, Any]]:
        return [row for batch in self.batches for row in batch.to_pylist()]


@dataclass
class Params:
    """Stands in for ``ProcessParams``: the arguments and the projected schema."""

    args: Any
    output_schema: pa.Schema


def run_scan(func: Any, args: Any, schema: pa.Schema | None = None, *, max_ticks: int = 1000) -> Collector:
    """Tick a ``TableFunctionGenerator`` until it finishes, as the framework would."""
    params = Params(args=args, output_schema=schema or func.FIXED_SCHEMA)
    out = Collector()
    initial = getattr(func, "initial_state", None)
    state = initial(params) if initial is not None else None
    for _ in range(max_ticks):
        if out.finished:
            return out
        func.process(params, state, out)
    raise AssertionError(f"{func.__name__} did not finish within {max_ticks} ticks")


def run_blended(
    func: Any, args: Any, column: str, values: list[Any], schema: pa.Schema | None = None
) -> Collector:
    """Call a blended function's ``process()`` with one input batch."""
    params = Params(args=args, output_schema=schema or func.FIXED_SCHEMA)
    out = Collector()
    field_type = pa.int64() if all(isinstance(v, int) or v is None for v in values) else pa.string()
    batch = pa.RecordBatch.from_arrays([pa.array(values, type=field_type)], names=[column])
    func.process(params, None, batch, out)
    return out
