"""The live lists: story rankings, the change feed, and the newest item id.

These are the API's unkeyed endpoints — they need no argument beyond which list
to read — so each is exposed as a catalog *table* that a consumer can select
from without parentheses (see :mod:`vgi_hackernews.worker`). The functions here
are the scans behind those tables.

Every table shares its name with the function that scans it, except the six
rankings, which all scan :class:`StoriesFunction` with a different ``feed``
argument. DuckDB keeps tables and table functions in separate namespaces, so
``max_item`` and ``max_item()`` can coexist and the table needs no ``all_``
prefix to tell them apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, ProcessParams, TableFunctionGenerator, init_single_worker
from vgi_rpc.rpc import OutputCollector

from vgi_hackernews import hn_api as api
from vgi_hackernews.meta import docs, examples
from vgi_hackernews.paging import IdListState, emit_item_page
from vgi_hackernews.schemas import (
    FEED_SCHEMA,
    MAX_ITEM_SCHEMA,
    UPDATED_ITEMS_SCHEMA,
    UPDATED_USERS_SCHEMA,
    batch_from_rows,
    normalize_user,
)


@dataclass(slots=True, frozen=True, kw_only=True)
class FeedArgs:
    """``stories(feed)``: which ranked list to read."""

    feed: Annotated[
        str,
        Arg(
            0,
            doc=("Which list to read: top (the front page), new, best, ask (Ask HN), show (Show HN) or job"),
            choices=list(api.FEEDS),
        ),
    ]


@init_single_worker
class StoriesFunction(TableFunctionGenerator[FeedArgs, IdListState]):
    """One ranked story list, hydrated page by page in rank order."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = FEED_SCHEMA

    class Meta:
        name = "stories"
        description = "The stories on one Hacker News list, chosen by name, in the order the site ranks them"
        categories = ["rankings"]
        projection_pushdown = True
        tags = docs(
            category="rankings",
            result_schema=FEED_SCHEMA,
            llm=(
                "Hacker News' ranked story lists, hydrated into full rows with their position. "
                "Each list is also a table (`top_stories`, `new_stories`, ...) that reads as "
                "plain SQL; reach for this function when the list is chosen by a parameter or "
                "you want to compare lists in one query. `top` is the front page, `new` the "
                "latest submissions, `best` the highest-voted recent links, and `ask`, `show` "
                "and `job` the Ask HN, Show HN and jobs pages."
            ),
            md=(
                "One row per story, in the order Hacker News ranks the list, with the whole "
                "item attached.\n\n"
                "### Rank is a snapshot\n\n"
                "The list of ids is read once, when the scan starts, and frozen for the rest of "
                "it, so each `rank` appears exactly once. The stories themselves are fetched "
                "just after, so a `score` can be a few seconds newer than the ranking it sits "
                "in.\n\n"
                "### What each request costs\n\n"
                "The API has no batch endpoint: reading the list is one request, and every row "
                "is one more. Rows are fetched 32 at a time and emitted 100 per batch, in rank "
                "order, so a query that stops early — a bare `LIMIT` — stops fetching too. An "
                "`ORDER BY` has to see every row first, so it reads the whole list.\n\n"
                "### Composing\n\n"
                "This is a streaming scan, which means it cannot be the inner side of a "
                "correlated `LATERAL`. Drive joins from it: its `id` and `author` columns feed "
                "`comments()`, `item()` and `user()`."
            ),
            example_queries=examples(
                (
                    "The first page of Hacker News as it stands right now",
                    "SELECT rank, title, score, descendants AS comments "
                    "FROM hackernews.main.stories('top') ORDER BY rank LIMIT 30",
                ),
                (
                    "Stories on both the front page and the best list",
                    "SELECT t.rank AS top_rank, b.rank AS best_rank, t.title "
                    "FROM hackernews.main.stories('top') t "
                    "JOIN hackernews.main.stories('best') b USING (id) ORDER BY t.rank",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT rank, title, score, descendants AS comments "
                    "FROM hackernews.main.stories('top') ORDER BY rank LIMIT 30"
                ),
                description="The first page of Hacker News as it stands right now",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[FeedArgs]) -> BindResponse:
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[FeedArgs]) -> IdListState:
        return IdListState()

    @classmethod
    def process(cls, params: ProcessParams[FeedArgs], state: IdListState, out: OutputCollector) -> None:
        """Emit the next page of the list, reading the list itself on the first tick."""
        emit_item_page(params, state, out, load=lambda: api.feed(params.args.feed), ranked=True)


@init_single_worker
class UpdatedItemsFunction(TableFunctionGenerator[None, IdListState]):
    """Recently changed items — the scan behind the ``updated_items`` table."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = UPDATED_ITEMS_SCHEMA

    class Meta:
        name = "updated_items"
        description = "Scan function behind the updated_items table: recently changed items, hydrated"
        categories = ["changes"]
        projection_pushdown = True
        tags = docs(
            category="changes",
            result_schema=UPDATED_ITEMS_SCHEMA,
            llm=(
                "The items Hacker News' change feed lists right now, each fetched in its current "
                "state. Prefer the `updated_items` table, which scans this and reads as plain "
                "SQL; the function form exists so the scan itself can be called. Use it to see "
                "what is moving on the site this minute — threads gaining replies, stories "
                "gaining votes — rather than what is ranked."
            ),
            md=(
                "The change feed's item half, hydrated.\n\n"
                "### Prefer the table\n\n"
                "The `updated_items` table is backed by this function and returns exactly the "
                "same rows; it reads without parentheses.\n\n"
                "### What a change is\n\n"
                "The feed does not say what changed, only that something did: a new reply adds "
                "to `kids`, a vote moves `score`, an edit rewrites `text`. It is a short rolling "
                "window, so two reads a minute apart share few rows."
            ),
            example_queries=examples(
                (
                    "Recently changed items, grouped by kind",
                    "SELECT type, count(*) AS changed FROM hackernews.main.updated_items() "
                    "GROUP BY type ORDER BY changed DESC",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT type, count(*) AS changed FROM hackernews.main.updated_items() "
                    "GROUP BY type ORDER BY changed DESC"
                ),
                description="Recently changed items, grouped by kind",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[None]) -> BindResponse:
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[None]) -> IdListState:
        return IdListState()

    @classmethod
    def process(cls, params: ProcessParams[None], state: IdListState, out: OutputCollector) -> None:
        """Emit the next page of changed items, reading the change feed on the first tick."""
        emit_item_page(params, state, out, load=lambda: api.updates()[0], ranked=True)


@init_single_worker
class UpdatedUsersFunction(TableFunctionGenerator[None, None]):
    """Recently changed profiles — the scan behind the ``updated_users`` table."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = UPDATED_USERS_SCHEMA

    class Meta:
        name = "updated_users"
        description = "Scan function behind the updated_users table: recently changed user profiles"
        categories = ["changes"]
        projection_pushdown = True
        tags = docs(
            category="changes",
            result_schema=UPDATED_USERS_SCHEMA,
            llm=(
                "The user profiles Hacker News' change feed lists right now, each fetched in its "
                "current state. Prefer the `updated_users` table, which scans this. A profile "
                "shows up here when anything about it moves — most often karma, as the user's "
                "posts are voted on — so it is a sample of who is active, not a directory."
            ),
            md=(
                "The change feed's profile half, hydrated.\n\n"
                "### Prefer the table\n\n"
                "The `updated_users` table is backed by this function and returns exactly the "
                "same rows.\n\n"
                "### Size\n\n"
                "The feed lists a few dozen profiles, so this is one page, fetched concurrently. "
                "Each row carries the user's whole `submitted` list, which for a long-standing "
                "account runs to thousands of ids."
            ),
            example_queries=examples(
                (
                    "Active users, by karma",
                    "SELECT username, karma, len(submitted) AS posts "
                    "FROM hackernews.main.updated_users() ORDER BY karma DESC",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT username, karma, len(submitted) AS posts "
                    "FROM hackernews.main.updated_users() ORDER BY karma DESC"
                ),
                description="Active users, by karma",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[None]) -> BindResponse:
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Read the change feed and hydrate every profile on it in one batch."""
        _, names = api.updates()
        rows = []
        for rank, raw in enumerate(api.fetch_users(names), start=1):
            if raw is None or not raw.get("id"):
                continue
            rows.append({"rank": rank, **normalize_user(raw)})
        out.emit(batch_from_rows(rows, params.output_schema))
        out.finish()


@init_single_worker
class MaxItemFunction(TableFunctionGenerator[None, None]):
    """The newest item id — the scan behind the ``max_item`` table."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = MAX_ITEM_SCHEMA

    class Meta:
        name = "max_item"
        description = "Scan function behind the max_item table: the largest item id assigned so far"
        categories = ["items"]
        projection_pushdown = True
        tags = docs(
            category="items",
            result_schema=MAX_ITEM_SCHEMA,
            llm=(
                "The id of the newest item on Hacker News, as one row. Prefer the `max_item` "
                "table, which scans this. Ids are assigned in sequence to every story and "
                "comment alike, so this is where a backwards walk through the site starts, and "
                "subtracting two readings counts what was posted in between."
            ),
            md=(
                "One row, one column: the newest item id.\n\n"
                "### Prefer the table\n\n"
                "The `max_item` table is backed by this function and returns the same row.\n\n"
                "### Using it\n\n"
                "`recent_items()` already starts from here, so reach for this directly only "
                "when the number itself is the answer, or to bound a range of ids by hand."
            ),
            example_queries=examples(
                (
                    "How many items have ever been posted",
                    "SELECT id AS items_posted FROM hackernews.main.max_item()",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql="SELECT id AS items_posted FROM hackernews.main.max_item()",
                description="How many items have ever been posted",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[None]) -> BindResponse:
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Read the newest id and emit it as the single row."""
        out.emit(batch_from_rows([{"id": api.max_item()}], params.output_schema))
        out.finish()


LIST_FUNCTIONS: list[type] = [StoriesFunction, UpdatedItemsFunction, UpdatedUsersFunction, MaxItemFunction]
