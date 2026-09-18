"""Walks: a comment tree, a user's history, and the newest items on the site.

Each of these follows a chain of ids whose length is not known up front, and
the two kinds of chain get two different shapes of function.

A **comment tree** is bounded — the largest Hacker News threads run to a few
thousand comments — and the questions asked of it are nearly always about a
story found by another query ("what are people saying about the top story?").
So ``comments()`` is *blended*: it accepts a literal, a scalar subquery, or a
correlated column under ``LATERAL``, and walks each tree completely before
returning. DuckDB does not let a subquery or a column reach an ordinary table
function's argument at all ("Table function cannot contain subqueries"), so
this is the only shape in which "the thread under the current #1" is one query.

A **posting history** and the **item sequence** are not bounded in any useful
sense — an old account has tens of thousands of posts, and the sequence tens
of millions of items — so ``submissions()`` and ``recent_items()`` are paged
scans (see :mod:`vgi_hackernews.paging`): each tick fetches one page and emits
it, so a ``LIMIT`` stops the walk and cancellation lands between pages. The
price is that their key must be a literal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, cast

import pyarrow as pa
from vgi.arguments import Arg
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, ProcessParams, TableFunctionGenerator, init_single_worker
from vgi.table_in_out_function import RowTransformFunction
from vgi_rpc import ArrowSerializableDataclass
from vgi_rpc.rpc import OutputCollector

from vgi_hackernews import hn_api as api
from vgi_hackernews.meta import docs, examples
from vgi_hackernews.paging import PAGE_SIZE, IdListState, emit_item_page
from vgi_hackernews.schemas import COMMENT_SCHEMA, ITEM_SCHEMA, batch_from_rows, normalize_item

if TYPE_CHECKING:
    from vgi.protocol import VgiOutputCollector

#: The most items one ``recent_items()`` call may walk back through. A day of
#: Hacker News is roughly 40,000 items; at ~300 items a second a full walk of
#: this bound takes a few minutes, which is the most a single call should be
#: allowed to spend without the caller asking twice.
RECENT_ITEMS_MAX = 50_000

# --------------------------------------------------------------------------
# comments(id)
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class CommentsArgs:
    """``comments(id)``: an item-id input column, plus an optional depth cap."""

    id: Annotated[int, Arg(0, doc="Id of the item whose replies to walk")]
    max_depth: Annotated[
        int,
        Arg(
            "max_depth",
            doc="Deepest level of replies to include; 1 means direct replies only, 0 means no limit",
            default=0,
            ge=0,
        ),
    ] = 0


@dataclass(slots=True)
class _Pending:
    """A reply discovered but not yet fetched, with where it will sit in its thread."""

    item_id: int
    parent_row: int
    root_id: int
    depth: int
    path: list[int]


def _replies(kids: Any, *, parent_row: int, root_id: int, depth: int, path: list[int]) -> list[_Pending]:
    """The ids in ``kids`` as pending replies at ``depth``, numbered in display order."""
    if not isinstance(kids, list):
        return []
    return [
        _Pending(kid, parent_row, root_id, depth, [*path, position])
        for position, kid in enumerate(kids, start=1)
        if isinstance(kid, int) and not isinstance(kid, bool)
    ]


def walk_threads(roots: list[tuple[int, int]], *, max_depth: int) -> tuple[list[dict[str, Any]], list[int]]:
    """Walk the reply trees under ``roots``, a level at a time, across all of them at once.

    Args:
        roots: ``(input row index, root item id)`` pairs.
        max_depth: Deepest level to include; 0 for no limit.

    Returns:
        The rows, breadth-first, and for each the input row it descends from.

    Breadth-first because a whole level can be fetched concurrently, where
    depth-first would fetch one comment at a time; and across every root at
    once, so a ``LATERAL`` over ten stories keeps the connection pool full
    instead of walking ten small trees one after another. Display order is not
    lost — it is carried in each row's ``path``.
    """
    frontier: list[_Pending] = []
    for (row, root_id), raw in zip(roots, api.fetch_items([r for _, r in roots]), strict=True):
        if raw is not None:
            frontier += _replies(raw.get("kids"), parent_row=row, root_id=root_id, depth=1, path=[])
    rows: list[dict[str, Any]] = []
    parents: list[int] = []
    while frontier and not (max_depth and frontier[0].depth > max_depth):
        level, frontier = frontier, []
        for pending, raw in zip(level, api.fetch_items([p.item_id for p in level]), strict=True):
            if raw is None or not isinstance(raw.get("id"), int):
                continue
            position = {"root_id": pending.root_id, "depth": pending.depth, "path": pending.path}
            rows.append({**position, **normalize_item(raw)})
            parents.append(pending.parent_row)
            frontier += _replies(
                raw.get("kids"),
                parent_row=pending.parent_row,
                root_id=pending.root_id,
                depth=pending.depth + 1,
                path=pending.path,
            )
    return rows, parents


class CommentsFunction(RowTransformFunction[CommentsArgs]):
    """Every reply under an item, at every depth — 1->N, walked level by level."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = COMMENT_SCHEMA

    class Meta:
        name = "comments"
        description = "The whole discussion under a Hacker News story or comment, one row per reply"
        categories = ["items"]
        projection_pushdown = True
        tags = docs(
            category="items",
            result_schema=COMMENT_SCHEMA,
            llm=(
                "The complete comment tree under a story (or under any comment), flattened to one "
                "row per reply with its depth and its position in the thread. This is the answer "
                "to 'what are people saying about X': the API itself only returns each item's "
                "direct reply ids, and this walks all of them. Order by `path` to read the thread "
                "as the site shows it. The id can come from a subquery or, under LATERAL, from "
                "another table's column, so finding the story and reading its thread is one query."
            ),
            md=(
                "Walks the reply tree under an item and returns every descendant — the item "
                "itself is not included.\n\n"
                "### Reading the thread in order\n\n"
                "`path` is the comment's position among its siblings at each level, so sorting by "
                "it reproduces the site's nesting exactly: `[1]`, then `[1, 1]` (the first reply "
                "to the first comment), then `[1, 1, 1]`, then `[1, 2]`, then `[2]`. `depth` is "
                "the length of the path.\n\n"
                "### Deleted and dead comments\n\n"
                "They are kept, flagged by `deleted` and `dead`, because replies hang beneath "
                "them. Filter them out when counting what was said; keep them when rebuilding "
                "the tree. A story's `descendants` counts only live comments, so it matches the "
                "row count here once both flags are filtered out.\n\n"
                "### Cost\n\n"
                "One request per comment, fetched a level at a time with 32 in flight; a thread "
                "of a thousand comments takes a few seconds. The whole tree is walked before any "
                "row is returned, so a `LIMIT` does not make it cheaper — `max_depth` does: "
                "`max_depth => 1` fetches only the direct replies.\n\n"
                "### Many threads at once\n\n"
                "Under `LATERAL`, every story in an input batch is walked together, so the "
                "threads of ten stories cost little more wall-clock time than the largest of "
                "them. Keep the driving side small — the front page's top ten, not all 500 — "
                "since every comment under every story is a request."
            ),
            example_queries=examples(
                (
                    "The Dropbox launch thread, read top to bottom",
                    "SELECT depth, author, hackernews.main.html_to_text(text) AS comment "
                    "FROM hackernews.main.comments(8863) WHERE NOT deleted AND NOT dead ORDER BY path",
                ),
                (
                    "Who talked most in the discussion of the current top story",
                    "SELECT author, count(*) AS comments FROM hackernews.main.comments("
                    "(SELECT id FROM hackernews.main.top_stories WHERE rank = 1)) "
                    "WHERE author IS NOT NULL GROUP BY author ORDER BY comments DESC, author LIMIT 10",
                ),
                (
                    "How many direct replies each of the top five stories has drawn",
                    "SELECT s.rank, s.title, count(c.id) AS replies "
                    "FROM (SELECT rank, id, title FROM hackernews.main.top_stories WHERE rank <= 5) s, "
                    "LATERAL hackernews.main.comments(s.id, max_depth => 1) c "
                    "GROUP BY s.rank, s.title ORDER BY s.rank",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT depth, author, hackernews.main.html_to_text(text) AS comment "
                    "FROM hackernews.main.comments(8863) WHERE NOT deleted AND NOT dead ORDER BY path"
                ),
                description="The Dropbox launch thread, read top to bottom",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[CommentsArgs]) -> BindResponse:
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def process(
        cls,
        params: ProcessParams[CommentsArgs],
        state: None,
        batch: pa.RecordBatch,
        out: OutputCollector,
    ) -> None:
        """Walk the tree under every id in the input batch, and emit them as one batch.

        1->N: ``parent_rows`` maps every reply back to the input row whose tree
        it came from, so a ``LATERAL`` stamps each story's columns onto its own
        comments.
        """
        ids = batch.column("id").to_pylist()
        roots = [(index, int(value)) for index, value in enumerate(ids) if value is not None and value > 0]
        rows, parents = walk_threads(roots, max_depth=params.args.max_depth)
        cast("VgiOutputCollector", out).emit(batch_from_rows(rows, params.output_schema), parent_rows=parents)


# --------------------------------------------------------------------------
# submissions(username)
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True, kw_only=True)
class SubmissionsArgs:
    """``submissions(username)``."""

    username: Annotated[str, Arg(0, doc="Username whose posts to read; case-sensitive")]


def _submitted(username: str) -> list[int]:
    profile = api.user(username)
    return api.int_list((profile or {}).get("submitted"))


@init_single_worker
class SubmissionsFunction(TableFunctionGenerator[SubmissionsArgs, IdListState]):
    """A user's posts, newest first, one page per tick."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ITEM_SCHEMA

    class Meta:
        name = "submissions"
        description = "Everything one Hacker News user has posted — stories, comments, polls — newest first"
        categories = ["users"]
        projection_pushdown = True
        tags = docs(
            category="users",
            result_schema=ITEM_SCHEMA,
            llm=(
                "A user's posting history, newest first: their stories, comments, polls and job "
                "ads as full item rows. Reach for this to see what someone writes about, how "
                "their stories scored, or when they are active. The username is case-sensitive. "
                "Long histories stream a page at a time, so add a `LIMIT` for the recent "
                "activity of a prolific account."
            ),
            md=(
                "The items behind a user's `submitted` list, hydrated in the order the profile "
                "lists them, which is newest first.\n\n"
                "### Size\n\n"
                "An active account has thousands of posts and an early one tens of thousands, "
                "every one a request. Rows arrive 100 per batch, so a `LIMIT` stops the walk: "
                "a user's last 50 comments cost about 50 requests whatever their history "
                "holds. Filter on `type` to separate stories from comments — the filter runs "
                "after the fetch, so it narrows the result, not the cost.\n\n"
                "### Unknown users\n\n"
                "An unknown username, or one in the wrong case, returns no rows."
            ),
            example_queries=examples(
                (
                    "Paul Graham's highest-scoring stories among his latest 200 posts",
                    "SELECT title, score, created_at FROM (SELECT * FROM "
                    "hackernews.main.submissions('pg') LIMIT 200) "
                    "WHERE type = 'story' ORDER BY score DESC LIMIT 10",
                ),
                (
                    "What a user has been commenting on lately",
                    "SELECT created_at, parent, hackernews.main.html_to_text(text) AS comment FROM "
                    "(SELECT * FROM hackernews.main.submissions('dang') LIMIT 20) "
                    "WHERE type = 'comment' ORDER BY created_at DESC",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT title, score, created_at FROM (SELECT * FROM "
                    "hackernews.main.submissions('pg') LIMIT 200) "
                    "WHERE type = 'story' ORDER BY score DESC LIMIT 10"
                ),
                description="Paul Graham's highest-scoring stories among his latest 200 posts",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[SubmissionsArgs]) -> BindResponse:
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[SubmissionsArgs]) -> IdListState:
        return IdListState()

    @classmethod
    def process(
        cls, params: ProcessParams[SubmissionsArgs], state: IdListState, out: OutputCollector
    ) -> None:
        """Emit the next page of posts, reading the profile on the first tick."""
        emit_item_page(params, state, out, load=lambda: _submitted(params.args.username), ranked=False)


# --------------------------------------------------------------------------
# recent_items(count)
# --------------------------------------------------------------------------


@dataclass(kw_only=True)
class RecentState(ArrowSerializableDataclass):
    """A backwards walk from the newest id: the next id to fetch, and where to stop.

    Both are fixed on the first tick, so items posted while the walk runs do
    not push the window along — the call covers the ``count`` ids that were
    newest when it began.
    """

    next_id: int = 0
    stop_id: int = 0
    started: bool = False


@dataclass(slots=True, frozen=True, kw_only=True)
class RecentArgs:
    """``recent_items(count)``."""

    count: Annotated[
        int,
        Arg(
            0,
            doc="How many of the newest item ids to walk back through",
            ge=1,
            le=RECENT_ITEMS_MAX,
        ),
    ]


@init_single_worker
class RecentItemsFunction(TableFunctionGenerator[RecentArgs, RecentState]):
    """The newest items on the site, of every type, walking back from the newest id."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ITEM_SCHEMA

    class Meta:
        name = "recent_items"
        description = "The latest items posted anywhere on Hacker News, comments and stories alike"
        categories = ["items"]
        projection_pushdown = True
        tags = docs(
            category="items",
            result_schema=ITEM_SCHEMA,
            llm=(
                "Everything posted to Hacker News most recently, across the whole site: the "
                "newest comments and stories, newest first. The argument is how many item ids to "
                "walk back through from the newest one. Reach for it for site-wide 'right now' "
                "questions — what is being discussed, who is commenting, how fast posts arrive "
                "— which the ranked lists cannot answer because they hold only stories."
            ),
            md=(
                "Walks backwards from the newest item id, one id at a time, and returns every "
                "item it finds.\n\n"
                "### Comments dominate\n\n"
                "Ids are shared by every kind of item and most of what is posted is comments, "
                "so a window of recent ids is mostly comments with a few stories among them. "
                "Filter on `type`.\n\n"
                "### Sizing the window\n\n"
                "The site takes in tens of thousands of items a day, so a few hundred ids reach "
                "back minutes, not hours. Every id is a request, fetched 32 at a time and "
                "emitted 100 per batch, newest first; a `LIMIT` stops the walk. The window is "
                "fixed when the call starts, so posts that arrive during a long walk are not "
                "included."
            ),
            example_queries=examples(
                (
                    "What has been posted in the last few hundred items, by type",
                    "SELECT type, count(*) AS items, min(created_at) AS since "
                    "FROM hackernews.main.recent_items(500) GROUP BY type ORDER BY items DESC",
                ),
                (
                    "The newest stories submitted anywhere on the site",
                    "SELECT id, title, author, created_at FROM hackernews.main.recent_items(1000) "
                    "WHERE type = 'story' ORDER BY id DESC",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT type, count(*) AS items, min(created_at) AS since "
                    "FROM hackernews.main.recent_items(500) GROUP BY type ORDER BY items DESC"
                ),
                description="What has been posted in the last few hundred items, by type",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[RecentArgs]) -> BindResponse:
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[RecentArgs]) -> RecentState:
        return RecentState()

    @classmethod
    def process(cls, params: ProcessParams[RecentArgs], state: RecentState, out: OutputCollector) -> None:
        """Fetch the next page of ids below ``next_id`` and emit what exists."""
        if not state.started:
            state.started = True
            newest = api.max_item()
            count = max(1, min(params.args.count, RECENT_ITEMS_MAX))
            state.next_id = newest
            state.stop_id = max(0, newest - count)
        while state.next_id > state.stop_id:
            low = max(state.stop_id, state.next_id - PAGE_SIZE)
            ids = list(range(state.next_id, low, -1))
            state.next_id = low
            rows = [
                normalize_item(raw)
                for raw in api.fetch_items(ids)
                if raw is not None and isinstance(raw.get("id"), int)
            ]
            if rows:
                out.emit(batch_from_rows(rows, params.output_schema))
                return
        out.finish()


WALK_FUNCTIONS: list[type] = [CommentsFunction, SubmissionsFunction, RecentItemsFunction]
