"""Arrow schemas, and the translation from Hacker News JSON to rows.

Every conversion here is total: a value that does not fit its column becomes
NULL rather than raising, because one odd field on one item must not fail the
batch of a hundred items it arrived in. The API promises only that ``id`` is
present; every other field is optional and simply absent when it does not apply.

Three columns are renamed from the API's field names, and each for a reason:

* ``by`` -> ``author``: ``BY`` is a DuckDB keyword and cannot be selected
  unquoted, so keeping the name would make every query that mentions it quote it.
* ``time`` -> ``created_at``: the value changes type (Unix seconds become a
  timestamp), and ``time`` reads as the SQL ``TIME`` type.
* a user's ``id`` -> ``username``: an item's ``id`` is a number and a user's is
  a string, and one name meaning two types across the catalog invites joins
  that compare an integer with a name.

Absence is also given a meaning where the API documents one. ``deleted`` and
``dead`` are sent only when true, so a missing flag is ``false`` — never NULL,
which would make ``WHERE NOT dead`` silently match nothing. A missing ``kids``
means no replies, so it is an empty list.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa

from vgi_hackernews.meta import field

#: Hacker News timestamps are Unix seconds; they become UTC instants.
TIMESTAMP = pa.timestamp("us", tz="UTC")

#: The window a nanosecond-resolution client can hold. A value outside it does
#: not degrade — it raises ``OverflowError`` when a pandas or numpy client
#: materializes the row — so it becomes NULL instead.
_NS_FLOOR = datetime(1678, 1, 1, tzinfo=UTC)
_NS_CEILING = datetime(2262, 1, 1, tzinfo=UTC)

ITEM_TYPES = ("story", "comment", "job", "poll", "pollopt")

_ITEM_FIELDS: list[pa.Field] = [
    field(
        "id",
        pa.int64(),
        "Unique item id, assigned in increasing order across every kind of item; the key item() "
        "and comments() take, and the value kids, parent, poll and parts refer to.",
        nullable=False,
    ),
    field(
        "type",
        pa.string(),
        "What the item is: 'story', 'comment', 'job', 'poll' or 'pollopt' (one option of a poll). "
        "Ask HN and Show HN posts are stories.",
    ),
    field(
        "author",
        pa.string(),
        "Username of whoever posted it (the API's `by` field); the key user() takes. NULL on deleted items.",
    ),
    field(
        "created_at",
        TIMESTAMP,
        "When the item was posted, as a UTC instant (the API's `time`, in Unix seconds).",
    ),
    field(
        "title",
        pa.string(),
        "Headline of a story, poll or job, as plain text. NULL for comments and poll options.",
    ),
    field(
        "url",
        pa.string(),
        "Address a link story points to. NULL for text posts such as Ask HN, and for comments.",
    ),
    field(
        "text",
        pa.string(),
        "Body of a comment, text post, job or poll option, as HTML: entity-escaped, with <p> "
        "paragraph breaks and <a> links. html_to_text() turns it into plain text. NULL when "
        "the item has no body.",
    ),
    field(
        "score",
        pa.int64(),
        "Points (net upvotes) on a story, poll or job, or the number of votes cast for a poll "
        "option. NULL for comments, whose scores Hacker News does not publish.",
    ),
    field(
        "descendants",
        pa.int64(),
        "Number of live comments in the whole thread under a story or poll, at every depth "
        "(deleted and dead comments are not counted). NULL for other item types.",
    ),
    field(
        "parent",
        pa.int64(),
        "Id of the item a comment replies to (another comment, or the story at the top of the "
        "thread). NULL for anything that is not a comment.",
    ),
    field("poll", pa.int64(), "For a poll option, the item id of the poll (its parent). NULL otherwise."),
    field(
        "kids",
        pa.list_(pa.int64()),
        "Ids of the direct replies, in the ranked order Hacker News displays them. Empty when "
        "there are none.",
    ),
    field(
        "parts",
        pa.list_(pa.int64()),
        "For a poll, the ids of its options in display order. NULL for other item types.",
    ),
    field(
        "deleted",
        pa.bool_(),
        "Whether the item was deleted. A deleted item keeps its id, type and place in its thread "
        "but loses its author and body. Never NULL.",
    ),
    field(
        "dead",
        pa.bool_(),
        "Whether the item is dead: killed by moderators or flags, and hidden from readers who "
        "have not opted in to seeing it. Never NULL.",
    ),
]

#: Every item, whatever its type. Columns that do not apply to a type are NULL.
ITEM_SCHEMA = pa.schema(_ITEM_FIELDS)


def _ranked(rank_comment: str, fields: Sequence[pa.Field]) -> pa.Schema:
    return pa.schema([field("rank", pa.int32(), rank_comment, nullable=False), *fields])


#: A story list: the items in the order Hacker News ranks them.
FEED_SCHEMA = _ranked(
    "1-based position in the list when it was read; 1 is the top slot. Positions move from one "
    "read to the next, so the rank is a snapshot, not an identity.",
    _ITEM_FIELDS,
)

#: Recently changed items, in the order the change feed lists them.
UPDATED_ITEMS_SCHEMA = _ranked(
    "1-based position in the change feed as served; the feed is a rolling window of recent "
    "changes, not a ranking by importance.",
    _ITEM_FIELDS,
)

#: A comment tree, flattened. ``path`` orders it the way the site displays it.
COMMENT_SCHEMA = pa.schema(
    [
        field(
            "root_id",
            pa.int64(),
            "Id of the item whose thread this comment belongs to: the id comments() was called with.",
            nullable=False,
        ),
        field(
            "depth",
            pa.int32(),
            "Levels below the root item: 1 is a direct reply, 2 a reply to a reply, and so on.",
            nullable=False,
        ),
        field(
            "path",
            pa.list_(pa.int32()),
            "The comment's 1-based position among its siblings at each level from the root down, "
            "so sorting by path lists the thread exactly as Hacker News displays it.",
            nullable=False,
        ),
        *_ITEM_FIELDS,
    ]
)

_USER_FIELDS: list[pa.Field] = [
    field(
        "username",
        pa.string(),
        "Unique, case-sensitive username (the API's user `id`); what author holds on an item.",
        nullable=False,
    ),
    field("created_at", TIMESTAMP, "When the account was created, as a UTC instant."),
    field(
        "karma",
        pa.int64(),
        "Karma points (roughly the upvotes the user's stories and comments have received, less "
        "their downvotes).",
    ),
    field("about", pa.string(), "Self-description from the profile, as HTML. NULL when blank."),
    field(
        "submitted",
        pa.list_(pa.int64()),
        "Ids of every story, comment, poll and job the user has posted, newest first. Can run to "
        "tens of thousands of ids; submissions() hydrates them a page at a time.",
    ),
]

#: One user profile.
USER_SCHEMA = pa.schema(_USER_FIELDS)

#: Recently changed profiles, in the order the change feed lists them.
UPDATED_USERS_SCHEMA = _ranked(
    "1-based position in the change feed as served; the feed is a rolling window of recent "
    "profile changes, not a ranking.",
    _USER_FIELDS,
)

#: The newest item id: one row.
MAX_ITEM_SCHEMA = pa.schema(
    [
        field(
            "id",
            pa.int64(),
            "Largest item id assigned so far, so the id of the newest item on the site. Ids are "
            "handed out in sequence, which also makes it a running count of everything posted.",
            nullable=False,
        )
    ]
)


# --------------------------------------------------------------------------
# JSON -> row dictionaries
# --------------------------------------------------------------------------


def normalize_item(raw: dict[str, Any]) -> dict[str, Any]:
    """Map one API item to a row keyed by :data:`ITEM_SCHEMA`'s column names."""
    return {
        "id": raw.get("id"),
        "type": raw.get("type"),
        "author": raw.get("by"),
        "created_at": raw.get("time"),
        "title": raw.get("title"),
        # A job with no link is sent as `"url": ""`; an absent link is NULL
        # everywhere else, and one spelling of "no link" is easier to filter.
        "url": raw.get("url") or None,
        "text": raw.get("text") or None,
        "score": raw.get("score"),
        "descendants": raw.get("descendants"),
        "parent": raw.get("parent"),
        "poll": raw.get("poll"),
        "kids": raw.get("kids") or [],
        "parts": raw.get("parts"),
        "deleted": bool(raw.get("deleted")),
        "dead": bool(raw.get("dead")),
    }


def normalize_user(raw: dict[str, Any]) -> dict[str, Any]:
    """Map one API user to a row keyed by :data:`USER_SCHEMA`'s column names."""
    return {
        "username": raw.get("id"),
        "created_at": raw.get("created"),
        "karma": raw.get("karma"),
        "about": raw.get("about") or None,
        "submitted": raw.get("submitted") or [],
    }


# --------------------------------------------------------------------------
# Row dictionaries -> Arrow
# --------------------------------------------------------------------------


def to_timestamp(value: Any) -> datetime | None:
    """Unix seconds to an aware UTC datetime, or None when absent or unrepresentable."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = datetime.fromtimestamp(float(value), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return parsed if _NS_FLOOR <= parsed <= _NS_CEILING else None


def to_integer(value: Any, kind: pa.DataType) -> int | None:
    """An integer that fits ``kind``, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    bits = kind.bit_width - 1
    return number if -(2**bits) <= number < 2**bits else None


def _integer_list(value: Any, kind: pa.DataType) -> list[int] | None:
    if not isinstance(value, list):
        return None
    return [n for n in (to_integer(v, kind) for v in value) if n is not None]


def column(rows: Sequence[dict[str, Any]], f: pa.Field) -> pa.Array:
    """Build the Arrow array ``f`` declares from ``rows``, converting each value totally."""
    values = [row.get(f.name) for row in rows]
    kind = f.type
    if pa.types.is_timestamp(kind):
        return pa.array([to_timestamp(v) for v in values], type=kind)
    if pa.types.is_boolean(kind):
        return pa.array([None if v is None else bool(v) for v in values], type=kind)
    if pa.types.is_integer(kind):
        return pa.array([to_integer(v, kind) for v in values], type=kind)
    if pa.types.is_string(kind):
        return pa.array([None if v is None else str(v) for v in values], type=kind)
    if pa.types.is_list(kind) and pa.types.is_integer(kind.value_type):
        return pa.array([_integer_list(v, kind.value_type) for v in values], type=kind)
    raise TypeError(f"no conversion for column {f.name} of type {kind}")


def batch_from_rows(rows: Sequence[dict[str, Any]], schema: pa.Schema) -> pa.RecordBatch:
    """One RecordBatch holding exactly ``schema``'s columns, pulled from ``rows`` by name.

    ``schema`` is normally the function's *projected* output schema, so only
    the columns the query asked for are converted. A projection can be empty —
    ``count(*)`` needs rows, not columns — and a batch built from no arrays has
    no rows either, so that case is built from a struct array that keeps the
    count.
    """
    if len(schema) == 0:
        return pa.RecordBatch.from_struct_array(pa.array([{}] * len(rows), type=pa.struct([])))
    return pa.RecordBatch.from_arrays([column(rows, f) for f in schema], schema=schema)
