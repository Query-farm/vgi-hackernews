"""Read-only HTTP access to the Hacker News Firebase API.

Every request goes through :func:`_get`, the single place an HTTP call is made,
and it only ever issues ``GET``. The read-only guard in the test suite
(``tests/test_readonly_guard.py``) asserts both properties, and that no other
module in the package so much as imports the HTTP client.

The API is, in its own words, "essentially a dump of our in-memory data
structures". It has no search, no filtering and no batch endpoint: a list
endpoint returns bare item ids, and each id costs one more request to hydrate.
Everything interesting about this module follows from that — the worker's cost
is dominated by per-item round trips, so they are issued concurrently over one
pooled connection set (:func:`fetch_items`), and results are returned in the
order the ids were given, because the order of a ranking *is* its content.

Three facts about the API that the rest of the package leans on, all verified
against the live service:

* A missing item or user is ``HTTP 200`` with the JSON body ``null``, not a 404.
* Every response carries ``Cache-Control: no-cache``, so there is no origin
  freshness policy to forward — caching is opt-in per call (``cache_ttl``).
* There is no documented rate limit, and none was hit at 32 concurrent
  requests. Transient failures are still retried: a ``LATERAL`` fan-out issues
  thousands of requests, and one dropped connection must not fail all of them.
"""

from __future__ import annotations

import atexit
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

import httpx2

#: Production base URL. ``v0`` is the only version Hacker News has published.
DEFAULT_BASE_URL = "https://hacker-news.firebaseio.com/v0"

#: Concurrent requests in flight when hydrating a list of ids. Measured against
#: the live API: 500 items took 3.6s at 8, 3.2s at 16 and 1.1s at 32. Above
#: that the gain is marginal and the load on a free public service is not.
CONCURRENCY = 32

#: Connect and read timeouts for every request.
TIMEOUT = httpx2.Timeout(20.0, connect=10.0)

#: Statuses worth retrying: throttling plus the transient 5xx a CDN produces.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Exponential backoff for a retryable request: ~0.25s, 0.5s, 1s, 2s.
_RETRY_ATTEMPTS = 5
_RETRY_BASE_SECONDS = 0.25

#: A Hacker News username: letters, digits, dash and underscore. Anything else
#: cannot name a user, so it is answered locally as "no such user" instead of
#: being sent — which also means no value from SQL can reshape the request path.
_USERNAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: The six ranked story lists, keyed by the name this worker exposes them under.
FEEDS: dict[str, str] = {
    "top": "/topstories.json",
    "new": "/newstories.json",
    "best": "/beststories.json",
    "ask": "/askstories.json",
    "show": "/showstories.json",
    "job": "/jobstories.json",
}


class HackerNewsError(RuntimeError):
    """A non-2xx response, or a 2xx that was not JSON, from the Hacker News API."""

    def __init__(self, status: int, path: str, body: str) -> None:
        super().__init__(f"Hacker News API {status} for {path}: {body[:400]}")
        self.status = status
        self.path = path


def base_url() -> str:
    """The API base URL, overridable with ``HN_BASE_URL`` (for a mirror or a test server)."""
    return os.environ.get("HN_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def open_client() -> httpx2.Client:
    """A client sized for :data:`CONCURRENCY` pooled connections.

    The only HTTP client object the package constructs, so the timeout and
    pool policy live in one place.
    """
    limits = httpx2.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    return httpx2.Client(timeout=TIMEOUT, limits=limits)


_shared: httpx2.Client | None = None
_pool: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def shared_client() -> httpx2.Client:
    """A process-wide client, so consecutive scan ticks reuse warm connections.

    A paged scan fetches one chunk per ``process()`` tick with no enclosing
    scope to hold a client open across the walk. A client per tick would pay a
    TLS handshake on every connection of every tick. ``httpx2.Client`` is safe
    to share across threads.
    """
    global _shared
    if _shared is None or _shared.is_closed:
        with _lock:
            if _shared is None or _shared.is_closed:
                _shared = open_client()
    return _shared


def _executor() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="hn-fetch")
    return _pool


def reset_shared_client() -> None:
    """Close and forget the process pool, so the next call opens a fresh one.

    Operationally this forces a reconnect; in tests it keeps the pool
    hermetic, since a client built against one test's mock transport would
    otherwise serve the next.
    """
    global _shared
    with _lock:
        if _shared is not None and not _shared.is_closed:
            _shared.close()
        _shared = None


atexit.register(reset_shared_client)


def _get(path: str, *, client: httpx2.Client | None = None) -> Any:
    """GET ``path`` under the API base and return the decoded JSON value.

    The one and only outbound-HTTP chokepoint. The value may be any JSON
    type: an object for an item, an integer for ``maxitem``, a list for a
    story ranking, and ``None`` for an item or user that does not exist.

    A ``GET`` is idempotent, so every transient failure is retried with
    exponential backoff — throttling, 5xx, and transport errors alike.

    Raises:
        HackerNewsError: A non-2xx status after retries, or a 2xx body that
            was not JSON (an error page from something in front of the API).
        httpx2.TransportError: Every attempt failed to reach the API.
    """
    http = client or shared_client()
    url = f"{base_url()}{path}"
    for attempt in range(_RETRY_ATTEMPTS):
        last_attempt = attempt == _RETRY_ATTEMPTS - 1
        try:
            response = http.get(url)
        except httpx2.TransportError:
            if last_attempt:
                raise
        else:
            if response.status_code not in _RETRYABLE_STATUSES or last_attempt:
                break
        time.sleep(_RETRY_BASE_SECONDS * (2**attempt))
    if response.status_code >= 400:
        raise HackerNewsError(response.status_code, path, response.text)
    try:
        return response.json()
    except ValueError as exc:
        raise HackerNewsError(
            response.status_code,
            path,
            f"expected JSON, got {response.headers.get('content-type', 'no content-type')}: {response.text}",
        ) from exc


def _fan_out[K, V](keys: Sequence[K], fetch: Callable[[K], V]) -> list[V]:
    """Apply ``fetch`` to every key concurrently, returning results in key order.

    ``ThreadPoolExecutor.map`` preserves input order, which is what makes a
    concurrently hydrated ranking still a ranking. The first exception is
    re-raised; a request that exhausted its retries is a failed query, not a
    silently missing row.
    """
    if not keys:
        return []
    if len(keys) == 1:
        return [fetch(keys[0])]
    return list(_executor().map(fetch, keys))


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------


def item(item_id: int, *, client: httpx2.Client | None = None) -> dict[str, Any] | None:
    """One item by id, or ``None`` when no such item exists.

    Non-positive ids are answered locally: item ids start at 1.
    """
    if item_id <= 0:
        return None
    payload = _get(f"/item/{int(item_id)}.json", client=client)
    return payload if isinstance(payload, dict) else None


def fetch_items(
    item_ids: Sequence[int], *, client: httpx2.Client | None = None
) -> list[dict[str, Any] | None]:
    """Hydrate many item ids concurrently, returning one entry per id, in order.

    Duplicate ids are fetched once and fanned back out, so a ``LATERAL`` whose
    driving rows repeat an id pays for it once per batch.
    """
    unique = list(dict.fromkeys(item_ids))
    found = dict(zip(unique, _fan_out(unique, lambda i: item(i, client=client)), strict=True))
    return [found[i] for i in item_ids]


def max_item(*, client: httpx2.Client | None = None) -> int:
    """The largest item id assigned so far — the newest item on the site."""
    return int(_get("/maxitem.json", client=client))


def feed(name: str, *, client: httpx2.Client | None = None) -> list[int]:
    """The ranked item ids of one story list (``top``, ``new``, ``best``, ...).

    Deduplicated, keeping each id's first position: the tables built on these
    lists declare ``id`` a primary key, and the optimizer is entitled to trust
    that, so a repeated id must not reach it.

    Raises:
        ValueError: ``name`` is not one of :data:`FEEDS`.
    """
    if name not in FEEDS:
        raise ValueError(f"unknown feed {name!r}; expected one of {', '.join(FEEDS)}")
    return _unique(int_list(_get(FEEDS[name], client=client)))


def updates(*, client: httpx2.Client | None = None) -> tuple[list[int], list[str]]:
    """Recently changed ``(item ids, usernames)``, in the order served, each deduplicated.

    Deduplicated for the same reason as :func:`feed`: ``updated_items`` and
    ``updated_users`` declare their key columns primary keys.
    """
    payload = _get("/updates.json", client=client)
    if not isinstance(payload, dict):
        return [], []
    profiles = payload.get("profiles") or []
    names = [p for p in profiles if isinstance(p, str) and p]
    return _unique(int_list(payload.get("items"))), _unique(names)


def _unique[T](values: list[T]) -> list[T]:
    """``values`` without repeats, each kept at its first position."""
    return list(dict.fromkeys(values))


def int_list(value: Any) -> list[int]:
    """Coerce a JSON list of ids to ints, dropping anything that is not one."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, int) and not isinstance(v, bool)]


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------


def is_username(value: str) -> bool:
    """Whether ``value`` could be a Hacker News username at all."""
    return bool(_USERNAME.match(value))


def segment(value: str) -> str:
    """Percent-encode one path segment, so a value cannot escape its position.

    :func:`user` only sends names that pass :func:`is_username`, which already
    rules out every character that could change the path. This is the second
    lock on the same door.
    """
    return quote(value, safe="")


def user(username: str, *, client: httpx2.Client | None = None) -> dict[str, Any] | None:
    """One user profile by username, or ``None`` when no such user exists.

    Usernames are case-sensitive: ``pg`` exists and ``PG`` does not. Only users
    with public activity are visible to the API.
    """
    if not is_username(username):
        return None
    payload = _get(f"/user/{segment(username)}.json", client=client)
    return payload if isinstance(payload, dict) else None


def fetch_users(
    usernames: Iterable[str], *, client: httpx2.Client | None = None
) -> list[dict[str, Any] | None]:
    """Hydrate many usernames concurrently, returning one entry per name, in order."""
    names = list(usernames)
    unique = list(dict.fromkeys(names))
    found = dict(zip(unique, _fan_out(unique, lambda n: user(n, client=client)), strict=True))
    return [found[n] for n in names]
