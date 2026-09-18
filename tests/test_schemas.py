"""JSON to rows to Arrow: renames, the meaning of absence, and total conversion."""

from __future__ import annotations

from datetime import UTC, datetime

import pyarrow as pa
import pytest

from vgi_hackernews.lookups import html_to_text
from vgi_hackernews.schemas import (
    COMMENT_SCHEMA,
    FEED_SCHEMA,
    ITEM_SCHEMA,
    USER_SCHEMA,
    batch_from_rows,
    normalize_item,
    normalize_user,
    to_integer,
    to_timestamp,
)

from .conftest import comment, story


class TestNormalizeItem:
    def test_renamed_fields(self) -> None:
        row = normalize_item(story(8863, by="dhouston", time=1175714200))
        assert row["author"] == "dhouston"
        assert row["created_at"] == 1175714200
        assert "by" not in row and "time" not in row

    def test_absent_flags_are_false_not_null(self) -> None:
        """`WHERE NOT dead` must not silently match nothing."""
        row = normalize_item(story(1))
        assert row["deleted"] is False
        assert row["dead"] is False

    def test_present_flags_are_true(self) -> None:
        row = normalize_item({"id": 2, "deleted": True, "dead": True})
        assert row["deleted"] is True and row["dead"] is True

    def test_absent_kids_are_an_empty_list(self) -> None:
        assert normalize_item(comment(3, parent=1))["kids"] == []

    def test_absent_parts_stay_null(self) -> None:
        """parts only applies to polls; an empty list would claim a poll with no options."""
        assert normalize_item(story(4))["parts"] is None

    def test_an_empty_url_is_null(self) -> None:
        """Jobs send `"url": ""`; one spelling of 'no link' is easier to filter."""
        assert normalize_item({"id": 5, "type": "job", "url": ""})["url"] is None

    def test_a_deleted_comment_keeps_its_place(self) -> None:
        raw = {"id": 6, "deleted": True, "parent": 1, "time": 1, "type": "comment", "kids": [7]}
        row = normalize_item(raw)
        assert row["author"] is None and row["text"] is None
        assert row["parent"] == 1 and row["kids"] == [7]


class TestNormalizeUser:
    def test_fields(self) -> None:
        row = normalize_user({"id": "pg", "created": 1160418092, "karma": 157316, "submitted": [1, 2]})
        assert row == {
            "username": "pg",
            "created_at": 1160418092,
            "karma": 157316,
            "about": None,
            "submitted": [1, 2],
        }


class TestTotalConversions:
    def test_timestamp_from_unix_seconds(self) -> None:
        assert to_timestamp(1175714200) == datetime(2007, 4, 4, 19, 16, 40, tzinfo=UTC)

    @pytest.mark.parametrize("value", [None, "1175714200", True, 10**20, -(10**20), float("nan")])
    def test_unusable_timestamps_are_null(self, value: object) -> None:
        assert to_timestamp(value) is None

    def test_an_integer_that_does_not_fit_is_null(self) -> None:
        assert to_integer(2**40, pa.int32()) is None
        assert to_integer(2**40, pa.int64()) == 2**40

    @pytest.mark.parametrize("value", [None, True, "x", 1.5j])
    def test_non_integers_are_null(self, value: object) -> None:
        assert to_integer(value, pa.int64()) is None

    def test_one_bad_value_does_not_fail_the_batch(self) -> None:
        rows = [
            normalize_item(story(1)),
            normalize_item({"id": 2, "time": "yesterday", "score": "lots", "kids": [3, "x", None]}),
        ]
        batch = batch_from_rows(rows, ITEM_SCHEMA)
        assert batch.num_rows == 2
        second = batch.to_pylist()[1]
        assert second["created_at"] is None and second["score"] is None
        assert second["kids"] == [3]


class TestSchemas:
    def test_a_projected_schema_builds_only_its_columns(self) -> None:
        projected = pa.schema([ITEM_SCHEMA.field("title"), ITEM_SCHEMA.field("id")])
        batch = batch_from_rows([normalize_item(story(9))], projected)
        assert batch.schema.names == ["title", "id"]

    def test_an_empty_projection_still_counts_rows(self) -> None:
        """`SELECT count(*)` may project nothing; the row count must survive."""
        rows = [normalize_item(story(1)), normalize_item(story(2))]
        batch = batch_from_rows(rows, pa.schema([]))
        assert (batch.num_rows, batch.num_columns) == (2, 0)

    @pytest.mark.parametrize("schema", [FEED_SCHEMA, COMMENT_SCHEMA, USER_SCHEMA])
    def test_every_column_is_documented(self, schema: pa.Schema) -> None:
        for f in schema:
            assert f.metadata and f.metadata.get(b"comment"), f.name

    def test_item_columns_are_shared_verbatim(self) -> None:
        """Every item-shaped result has the same item columns, in the same order."""
        names = ITEM_SCHEMA.names
        assert FEED_SCHEMA.names[1:] == names
        assert COMMENT_SCHEMA.names[3:] == names

    def test_no_column_needs_quoting(self) -> None:
        """`by` is a DuckDB keyword; it was renamed so nobody has to quote it."""
        for schema in (ITEM_SCHEMA, USER_SCHEMA, COMMENT_SCHEMA, FEED_SCHEMA):
            assert "by" not in schema.names


class TestHtmlToText:
    def test_entities_are_decoded(self) -> None:
        assert html_to_text("Don&#x27;t <i>panic</i> &amp; carry on") == "Don't panic & carry on"

    def test_paragraphs_become_blank_lines(self) -> None:
        assert html_to_text("one<p>two<p>three") == "one\n\ntwo\n\nthree"

    def test_a_link_becomes_its_full_address(self) -> None:
        """The anchor text is truncated with '...'; the href is not."""
        value = (
            '<a href="https:&#x2F;&#x2F;example.com&#x2F;a&#x2F;very&#x2F;long&#x2F;path" rel="nofollow">'
            "https:&#x2F;&#x2F;example.com&#x2F;a&#x2F;very...</a>"
        )
        assert html_to_text(f"see {value}") == "see https://example.com/a/very/long/path"

    def test_an_escaped_tag_stays_text(self) -> None:
        """Entities decode last, so `&lt;b&gt;` in a comment is not stripped as markup."""
        assert html_to_text("use &lt;b&gt; for bold") == "use <b> for bold"

    def test_code_blocks_keep_their_lines(self) -> None:
        assert html_to_text("<pre><code>  a\n  b\n</code></pre>") == "  a\n  b"

    def test_null_is_null(self) -> None:
        assert html_to_text(None) is None
