"""Hydrating a list of ids as a paged scan.

Almost everything in the Hacker News API is a list of ids — a ranking, a
user's submissions, a comment's replies — and every id costs one request to
turn into a row. Hydrating a whole list inside one ``process()`` call is the
obvious implementation and the wrong one: a ``LIMIT 10`` over the 500-story
``new`` list would still pay for 500 requests, and a scan blocked inside its
first batch cannot be cancelled at all, so a large walk would wedge the client
rather than merely run slowly.

So the list lives in scan state, and each tick hydrates one page of it and
emits one batch. DuckDB sees rows after the first page, a ``LIMIT`` stops the
walk early, and cancellation lands between ticks.

The trade-off: a stateful scan cannot be the inner side of a correlated
``LATERAL``. That is why the point lookups (:mod:`vgi_hackernews.lookups`) are
separate functions — they are bounded by construction and compose freely.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vgi.table_function import ProcessParams
from vgi_rpc import ArrowSerializableDataclass
from vgi_rpc.rpc import OutputCollector

from vgi_hackernews import hn_api as api
from vgi_hackernews.schemas import batch_from_rows, normalize_item

#: Items hydrated per tick. At :data:`~vgi_hackernews.hn_api.CONCURRENCY` in
#: flight a page is roughly three rounds of requests: a few hundred
#: milliseconds, which keeps the first rows prompt without making a long walk
#: pay a per-tick overhead on every handful of items.
PAGE_SIZE = 100


@dataclass(kw_only=True)
class IdListState(ArrowSerializableDataclass):
    """A list of item ids to hydrate, and how far through it the scan has got.

    The list is read once, on the first tick, and frozen here for the rest of
    the walk. Re-reading it per page would let a ranking reshuffle between
    pages, so the same story could appear twice or not at all — a snapshot is
    the only way the ``rank`` column means one thing.
    """

    ids: list[int] = field(default_factory=list)
    offset: int = 0
    loaded: bool = False


def emit_item_page(
    params: ProcessParams[Any],
    state: IdListState,
    out: OutputCollector,
    *,
    load: Callable[[], list[int]],
    ranked: bool,
) -> None:
    """Hydrate and emit the next page of ``state.ids``, or finish when none remain.

    Args:
        params: The tick's parameters; ``output_schema`` is the projected schema.
        state: The frozen id list and the scan's position in it.
        out: Collector for the page, or for ``finish()`` once the list is done.
        load: Reads the id list; called once, on the first tick.
        ranked: Stamp each row with its 1-based position in the list as ``rank``.

    Ids that no longer resolve to an item are skipped. A page in which every id
    was skipped moves straight on to the next one, so an emitted batch always
    carries rows.
    """
    if not state.loaded:
        state.ids = load()
        state.loaded = True
    while state.offset < len(state.ids):
        start = state.offset
        chunk = state.ids[start : start + PAGE_SIZE]
        state.offset += len(chunk)
        rows: list[dict[str, Any]] = []
        for position, raw in enumerate(api.fetch_items(chunk), start=start + 1):
            if raw is None or not isinstance(raw.get("id"), int):
                continue
            row = normalize_item(raw)
            if ranked:
                row["rank"] = position
            rows.append(row)
        if rows:
            out.emit(batch_from_rows(rows, params.output_schema))
            return
    out.finish()
