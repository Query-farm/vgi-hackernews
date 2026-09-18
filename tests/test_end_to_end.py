"""SQL executed against a real ATTACH, through haybarn and the vgi extension.

The offline suite drives function bodies directly; this tier checks the
contract the extension actually holds the worker to — that a LIMIT stops a
paged scan, that a LATERAL stamps each driving row onto its own output, that
every value can be materialized by a client. Marked ``live`` because a real
ATTACH necessarily talks to Hacker News.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

ROOT = Path(__file__).resolve().parent.parent

#: Dropbox's 2007 launch post: old enough that nothing about its thread moves.
DROPBOX = 8863


@pytest.fixture(scope="module")
def con() -> Iterator[Any]:
    """A haybarn connection with the worker attached."""
    haybarn = pytest.importorskip("haybarn")
    connection = haybarn.connect()
    try:
        connection.execute("FORCE INSTALL vgi FROM community")
        connection.execute("LOAD vgi")
        location = f"uv run --project {ROOT} {ROOT / 'hackernews_worker.py'}"
        connection.execute(f"ATTACH 'hackernews' (TYPE vgi, LOCATION '{location}')")
    except Exception as exc:  # pragma: no cover - environment, not the worker
        pytest.skip(f"cannot attach the worker: {exc}")
    yield connection
    connection.close()


def one(con: Any, sql: str) -> Any:
    row = con.execute(sql).fetchone()
    assert row is not None
    return row[0] if len(row) == 1 else row


class TestScansStream:
    """A LIMIT must stop a paged scan: one page of requests, not the whole list."""

    @pytest.mark.parametrize(
        "relation",
        [
            "hackernews.main.new_stories",
            "hackernews.main.stories('top')",
            "hackernews.main.submissions('pg')",
            "hackernews.main.recent_items(50000)",
        ],
    )
    def test_a_limit_returns_promptly(self, con: Any, relation: str) -> None:
        start = time.perf_counter()
        rows = con.execute(f"SELECT id FROM {relation} LIMIT 10").fetchall()
        elapsed = time.perf_counter() - start
        assert len(rows) == 10
        assert elapsed < 10, f"{relation} took {elapsed:.1f}s to yield ten rows"

    def test_a_count_over_a_projected_scan_counts_rows(self, con: Any) -> None:
        assert one(con, "SELECT count(*) FROM hackernews.main.job_stories") == one(
            con, "SELECT max(rank) FROM hackernews.main.job_stories"
        )


class TestLateral:
    def test_comments_land_on_their_own_story(self, con: Any) -> None:
        mismatched = one(
            con,
            "SELECT count(*) FILTER (WHERE c.root_id <> s.id) FROM "
            "(SELECT id FROM hackernews.main.top_stories WHERE rank <= 3) s, "
            "LATERAL hackernews.main.comments(s.id, max_depth => 2) c",
        )
        assert mismatched == 0

    def test_item_lookups_follow_kids(self, con: Any) -> None:
        wrong_parent = one(
            con,
            f"SELECT count(*) FILTER (WHERE c.parent <> {DROPBOX}) FROM "
            f"(SELECT unnest(kids) AS kid FROM hackernews.main.item({DROPBOX})) k, "
            "LATERAL hackernews.main.item(k.kid) c",
        )
        assert wrong_parent == 0

    def test_a_subquery_key_reaches_a_blended_function(self, con: Any) -> None:
        """An ordinary table function refuses subqueries; the blended ones take them."""
        title = one(con, f"SELECT title FROM hackernews.main.item((SELECT {DROPBOX}))")
        assert title.startswith("My YC app: Dropbox")


class TestThreads:
    def test_descendants_counts_the_live_comments(self, con: Any) -> None:
        live, descendants = one(
            con,
            f"SELECT (SELECT count(*) FROM hackernews.main.comments({DROPBOX}) "
            f"WHERE NOT deleted AND NOT dead), "
            f"(SELECT descendants FROM hackernews.main.item({DROPBOX}))",
        )
        assert live == descendants

    def test_path_order_matches_the_kids_order(self, con: Any) -> None:
        first_by_path, first_kid = one(
            con,
            f"SELECT (SELECT id FROM hackernews.main.comments({DROPBOX}) ORDER BY path LIMIT 1), "
            f"(SELECT kids[1] FROM hackernews.main.item({DROPBOX}))",
        )
        assert first_by_path == first_kid


class TestRowsMaterialize:
    """Every value must survive the trip into a client, not merely be produced."""

    def test_every_table(self, con: Any) -> None:
        from vgi_hackernews.worker import _HACKERNEWS_CATALOG

        for table in _HACKERNEWS_CATALOG.schemas[0].tables:
            con.execute(f"SELECT * FROM hackernews.main.{table.name} LIMIT 5").fetchall()

    def test_timestamps_are_utc_instants(self, con: Any) -> None:
        assert one(con, f"SELECT epoch(created_at) FROM hackernews.main.item({DROPBOX})") == 1175714200

    def test_html_to_text_on_real_comments(self, con: Any) -> None:
        leftovers = one(
            con,
            "SELECT count(*) FROM (SELECT hackernews.main.html_to_text(text) AS t "
            "FROM hackernews.main.recent_items(200) WHERE text IS NOT NULL) "
            "WHERE t LIKE '%&#x27;%' OR t LIKE '%<p>%'",
        )
        assert leftovers == 0

    def test_usernames_are_case_sensitive(self, con: Any) -> None:
        assert one(con, "SELECT count(*) FROM hackernews.main.user('pg')") == 1
        assert one(con, "SELECT count(*) FROM hackernews.main.user('pg/../x')") == 0
