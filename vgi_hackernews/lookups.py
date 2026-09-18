"""Point lookups by key: ``item(id)`` and ``user(username)``, plus ``html_to_text()``.

``item`` and ``user`` are **blended** (:class:`~vgi.table_in_out_function.RowTransformFunction`)
table functions: their positional argument *is* the per-row input column, so one
registration serves a literal call and a correlated ``LATERAL`` alike::

    SELECT title FROM hackernews.main.item(8863);

    SELECT s.title, u.karma
    FROM hackernews.main.top_stories s,
         LATERAL hackernews.main.user(s.author) u;

The ``LATERAL`` form is the one that matters. The API's answer to "what are the
children of this item" is "load the item and get their ids, then load them",
and that is exactly a correlated join over ``kids`` — which works here only
because a whole input batch of ids is fetched concurrently rather than one row
at a time.

A blended function must emit everything for its input batch in a single
``process()`` call, and may not keep state across batches (DuckDB forbids a
final pass under a correlated ``LATERAL``). Each lookup is one bounded request,
so neither constraint costs anything here. The walks — a comment tree, a
user's history, the newest items — live in :mod:`vgi_hackernews.walks`, which
explains why the first is blended too and the other two are not.
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, cast

import pyarrow as pa
from vgi import Param, Returns, ScalarFunction
from vgi.arguments import Arg
from vgi.cache_control import CacheControl
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, ProcessParams
from vgi.table_in_out_function import RowTransformFunction
from vgi_rpc.rpc import OutputCollector

from vgi_hackernews import hn_api as api
from vgi_hackernews.meta import docs, examples
from vgi_hackernews.schemas import ITEM_SCHEMA, USER_SCHEMA, batch_from_rows, normalize_item, normalize_user

if TYPE_CHECKING:
    from vgi.protocol import VgiOutputCollector

#: Grace window for serving a cached lookup when a refetch fails.
STALE_IF_ERROR = 300


def _opt_in_cache_control(ttl: int) -> CacheControl | None:
    """Cache metadata for a lookup the caller has asked to cache.

    Hacker News marks every response ``Cache-Control: no-cache``, so nothing is
    cached unless the caller passes ``cache_ttl``. When they do, ``per_value``
    memoization lets a ``LATERAL`` that repeats a key — every comment by the
    same author, say — serve the repeat from the client's cache.
    """
    if ttl <= 0:
        return None
    return CacheControl(ttl=ttl, stale_if_error=STALE_IF_ERROR, per_value=True)


def _emit_lookups(
    out: OutputCollector,
    params: ProcessParams[Any],
    rows: Sequence[dict[str, Any]],
    parent_rows: Sequence[int],
    cache_control: CacheControl | None,
) -> None:
    """Emit a 1->0/1 batch with provenance.

    ``parent_rows[i]`` is the index, within this call's input batch, of the row
    that produced output row ``i``. A key that resolves to nothing emits
    nothing, so the mapping is not the identity and has to be sent: without it
    the batched-``LATERAL`` operator cannot stamp the driving row's columns
    onto the right output rows.
    """
    batch = batch_from_rows(rows, params.output_schema)
    cast("VgiOutputCollector", out).emit(batch, parent_rows=list(parent_rows), cache_control=cache_control)


@dataclass(slots=True, frozen=True, kw_only=True)
class ItemArgs:
    """``item(id)``: an item-id input column, plus an opt-in cache TTL."""

    id: Annotated[int, Arg(0, doc="Id of the item to fetch")]
    cache_ttl: Annotated[
        int,
        Arg("cache_ttl", doc="Seconds to cache each result (0 turns caching off)", default=0, ge=0),
    ] = 0


ITEM_DOCS = docs(
    category="items",
    result_schema=ITEM_SCHEMA,
    llm=(
        "Any Hacker News item — story, comment, job, poll or poll option — by its numeric id. "
        "This is the building block for following the site's links: a story's `kids` are the "
        "ids of its top-level comments, a comment's `parent` points back up, and a poll's "
        "`parts` are its options, and each of them is one `item()` call. It composes under a "
        "correlated LATERAL, so a whole list of ids is looked up in one query. An id with no "
        "item behind it returns no row rather than an error."
    ),
    md=(
        "One row per id, in the same shape every other item-returning object uses.\n\n"
        "### Following links\n\n"
        "The API stores relationships as bare ids, so moving through the site is a chain of "
        "lookups. Unnest a list of ids and join this function to it with `LATERAL`, and the "
        "ids in each input batch are fetched concurrently rather than one by one. For a whole "
        "comment tree at every depth, `comments()` does the walking for you.\n\n"
        "### Missing and deleted items\n\n"
        "An id that was never assigned yields no row. A deleted item still yields its row, "
        "with `deleted` set and `author` and `text` gone, because its replies may still be "
        "there.\n\n"
        "### Caching\n\n"
        "Hacker News marks every response uncacheable, so each call refetches. Pass "
        "`cache_ttl => N` to cache each item for N seconds, which also turns on per-value "
        "memoization for a `LATERAL` that looks the same id up repeatedly."
    ),
    example_queries=examples(
        (
            "The Dropbox launch post, from 2007",
            "SELECT title, author, created_at, url FROM hackernews.main.item(8863)",
        ),
        (
            "The top-level comments on the current number one story, in display order",
            "SELECT k.position, c.author, hackernews.main.html_to_text(c.text) AS comment "
            "FROM (SELECT unnest(kids) AS kid, generate_subscripts(kids, 1) AS position "
            "FROM hackernews.main.top_stories WHERE rank = 1) k, "
            "LATERAL hackernews.main.item(k.kid) c ORDER BY k.position",
        ),
        (
            "How a poll's votes split across its options",
            "SELECT o.text AS option, o.score AS votes FROM "
            "(SELECT unnest(parts) AS part FROM hackernews.main.item(126809)) p, "
            "LATERAL hackernews.main.item(p.part) o ORDER BY votes DESC",
        ),
    ),
)


class ItemFunction(RowTransformFunction[ItemArgs]):
    """One item by id — 1->0/1, fetched concurrently across the input batch."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ITEM_SCHEMA

    class Meta:
        name = "item"
        description = "Look up any Hacker News story, comment, job or poll by its numeric id"
        categories = ["items"]
        projection_pushdown = True
        tags = ITEM_DOCS
        examples = [
            FunctionExample(
                sql="SELECT title, author, created_at, url FROM hackernews.main.item(8863)",
                description="The Dropbox launch post, from 2007",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[ItemArgs]) -> BindResponse:
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def process(
        cls,
        params: ProcessParams[ItemArgs],
        state: None,
        batch: pa.RecordBatch,
        out: OutputCollector,
    ) -> None:
        ids = batch.column("id").to_pylist()
        wanted = [(index, int(value)) for index, value in enumerate(ids) if value is not None]
        found = api.fetch_items([item_id for _, item_id in wanted])
        rows: list[dict[str, Any]] = []
        parents: list[int] = []
        for (index, _), raw in zip(wanted, found, strict=True):
            if raw is None or not isinstance(raw.get("id"), int):
                continue
            rows.append(normalize_item(raw))
            parents.append(index)
        _emit_lookups(out, params, rows, parents, _opt_in_cache_control(params.args.cache_ttl))


@dataclass(slots=True, frozen=True, kw_only=True)
class UserArgs:
    """``user(username)``: a username input column, plus an opt-in cache TTL."""

    username: Annotated[str, Arg(0, doc="Username to fetch; case-sensitive")]
    cache_ttl: Annotated[
        int,
        Arg("cache_ttl", doc="Seconds to cache each result (0 turns caching off)", default=0, ge=0),
    ] = 0


USER_DOCS = docs(
    category="users",
    result_schema=USER_SCHEMA,
    llm=(
        "A Hacker News user's public profile — account age, karma, the about text and the ids "
        "of everything they have posted — by username. Usernames are case-sensitive. Feed it "
        "the `author` column of any item-returning object under a correlated LATERAL to put a "
        "profile beside each post. Only users with public activity exist in the API; an unknown "
        "name returns no row."
    ),
    md=(
        "One row per username.\n\n"
        "### Case matters\n\n"
        "`pg` and `PG` are different names. Matching is exact, as the site does it, so a name "
        "in the wrong case finds nothing rather than the account you meant.\n\n"
        "### Posts\n\n"
        "`submitted` is every id the user has posted, newest first, and can hold tens of "
        "thousands of them. To read the posts themselves a page at a time, use "
        "`submissions()`.\n\n"
        "### Caching\n\n"
        "Every call refetches unless `cache_ttl => N` is passed, which caches each profile for "
        "N seconds and memoizes repeated names within a `LATERAL`."
    ),
    example_queries=examples(
        (
            "Paul Graham's account",
            "SELECT username, created_at, karma, len(submitted) AS posts FROM hackernews.main.user('pg')",
        ),
        (
            "Karma of the people who posted today's front page",
            "SELECT s.rank, s.author, u.karma, u.created_at AS joined "
            "FROM (SELECT rank, author FROM hackernews.main.top_stories WHERE rank <= 30) s, "
            "LATERAL hackernews.main.user(s.author, cache_ttl => 300) u ORDER BY s.rank",
        ),
    ),
)


class UserFunction(RowTransformFunction[UserArgs]):
    """One user profile by username — 1->0/1, fetched concurrently across the input batch."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = USER_SCHEMA

    class Meta:
        name = "user"
        description = "Look up a Hacker News user's public profile and karma by username"
        categories = ["users"]
        projection_pushdown = True
        tags = USER_DOCS
        examples = [
            FunctionExample(
                sql=(
                    "SELECT username, created_at, karma, len(submitted) AS posts "
                    "FROM hackernews.main.user('pg')"
                ),
                description="Paul Graham's account",
            ),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[UserArgs]) -> BindResponse:
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def process(
        cls,
        params: ProcessParams[UserArgs],
        state: None,
        batch: pa.RecordBatch,
        out: OutputCollector,
    ) -> None:
        names = batch.column("username").to_pylist()
        wanted = [(index, str(value)) for index, value in enumerate(names) if value]
        found = api.fetch_users([name for _, name in wanted])
        rows: list[dict[str, Any]] = []
        parents: list[int] = []
        for (index, _), raw in zip(wanted, found, strict=True):
            if raw is None or not raw.get("id"):
                continue
            rows.append(normalize_user(raw))
            parents.append(index)
        _emit_lookups(out, params, rows, parents, _opt_in_cache_control(params.args.cache_ttl))


# --------------------------------------------------------------------------
# html_to_text
# --------------------------------------------------------------------------

#: A link. Hacker News shortens long URLs in the anchor text ("https://exa...")
#: but keeps the whole address in href, so the href is what survives.
_LINK = re.compile(r"<a\s[^>]*?href=\"([^\"]*)\"[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)
_PARAGRAPH = re.compile(r"<p>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(value: str | None) -> str | None:
    """Convert Hacker News' comment HTML to plain text.

    The site's markup is a small, fixed vocabulary: ``<p>`` between paragraphs
    (never closed), ``<i>``, ``<a href>`` and ``<pre><code>``, with every
    special character entity-escaped. Paragraph breaks become blank lines,
    links become their full address, other tags are dropped, and entities are
    decoded last so an escaped ``&lt;`` in the text cannot turn into a tag.
    """
    if value is None:
        return None
    text = _LINK.sub(lambda match: match.group(1), value)
    text = _PARAGRAPH.sub("\n\n", text)
    text = _TAG.sub("", text)
    # Only surrounding newlines go: a comment that opens with a code block
    # keeps the indentation of its first line.
    return html.unescape(text).strip("\n")


class HtmlToTextFunction(ScalarFunction):
    """Plain text from the HTML Hacker News stores in ``text`` and ``about``."""

    class Meta:
        name = "html_to_text"
        description = "Turn Hacker News' HTML (comment bodies, text posts, profiles) into plain text"
        categories = ["items"]
        tags = docs(
            category="items",
            llm=(
                "Converts the HTML Hacker News stores in `text` and `about` into plain text you "
                "can search, count words in, or show a reader. Use it before any `LIKE`, "
                "`regexp_matches` or full-text work on comment bodies — in the raw column an "
                "apostrophe is `&#x27;` and a slash is `&#x2F;`, so a plain-text pattern "
                "silently misses."
            ),
            md=(
                "Decodes entities, turns `<p>` into blank lines, replaces each link with its full "
                "address, and drops the remaining tags.\n\n"
                "### Why the raw column needs this\n\n"
                "Every special character in a comment is entity-escaped, so the word *don't* is "
                "stored as `don&#x27;t` and a URL's slashes as `&#x2F;`. Searching the raw "
                "column for either finds nothing, without an error to say why.\n\n"
                "### Links\n\n"
                "The site shortens long URLs in the visible link text but keeps the whole "
                "address in the link itself, so the full address is what this returns."
            ),
            example_queries=examples(
                (
                    "Decode a snippet of comment HTML",
                    "SELECT hackernews.main.html_to_text('Don&#x27;t <i>panic</i> &amp; carry on') AS plain",
                ),
                (
                    "Search comment text on a thread without tripping over HTML entities",
                    "SELECT id, author FROM hackernews.main.comments(8863) "
                    "WHERE hackernews.main.html_to_text(text) ILIKE '%don''t%' ORDER BY path",
                ),
            ),
        )
        examples = [
            FunctionExample(
                sql="SELECT hackernews.main.html_to_text('Don&#x27;t <i>panic</i> &amp; carry on') AS plain",
                description="Decode a snippet of comment HTML",
            ),
        ]

    @classmethod
    def compute(
        cls,
        value: Annotated[pa.StringArray, Param(doc="HTML from an item's text or a user's about")],
    ) -> Annotated[pa.StringArray, Returns()]:
        """Convert each value; NULL stays NULL."""
        return pa.array([html_to_text(v) for v in value.to_pylist()], type=pa.string())


LOOKUP_FUNCTIONS: list[type] = [ItemFunction, UserFunction, HtmlToTextFunction]
