"""llms.txt must stay a faithful map of the worker.

An agent reads llms.txt instead of the source, so a table that is missing from
it does not exist as far as that agent knows, and a column documented under
its old name produces a query that fails to bind. These tests tie the file to
the catalog it describes. Every recipe in it is also executed against the live
worker, in tests/test_end_to_end.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from vgi_hackernews import hn_api
from vgi_hackernews.schemas import (
    COMMENT_SCHEMA,
    FEED_SCHEMA,
    ITEM_SCHEMA,
    MAX_ITEM_SCHEMA,
    UPDATED_USERS_SCHEMA,
    USER_SCHEMA,
)
from vgi_hackernews.walks import RECENT_ITEMS_MAX
from vgi_hackernews.worker import _HACKERNEWS_CATALOG

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "llms.txt").read_text()
SCHEMA = _HACKERNEWS_CATALOG.schemas[0]

#: A markdown link, as the llms.txt file-list sections use them.
_LINK = re.compile(r"^- \[[^\]]+\]\((https://[^)\s]+)\)(?::\s.+)?$")


def sql_statements(text: str = TEXT) -> list[str]:
    """Every SQL statement in the file's ```sql blocks, comments removed."""
    statements: list[str] = []
    for block in re.findall(r"```sql\n(.*?)```", text, re.DOTALL):
        body = "\n".join(line for line in block.splitlines() if not line.strip().startswith("--"))
        statements += [s.strip() for s in body.split(";") if s.strip()]
    return statements


class TestFormat:
    """The shape https://llmstxt.org specifies, which is what tooling parses."""

    def test_opens_with_the_title_and_a_summary(self) -> None:
        lines = TEXT.splitlines()
        assert lines[0] == "# vgi-hackernews"
        assert next(line for line in lines[1:] if line.strip()).startswith("> ")

    def test_only_one_h1_and_no_deeper_headings(self) -> None:
        """Detail before the first H2 may not use headings; H2s are file lists."""
        outside_code = re.sub(r"```.*?```", "", TEXT, flags=re.DOTALL)
        headings = re.findall(r"^(#+) ", outside_code, re.MULTILINE)
        assert headings[0] == "#"
        assert set(headings[1:]) == {"##"}

    def test_h2_sections_are_link_lists(self) -> None:
        sections = re.split(r"^## .+$", TEXT, flags=re.MULTILINE)[1:]
        assert sections
        for section in sections:
            entries = [line for line in section.splitlines() if line.strip()]
            assert entries
            for entry in entries:
                assert _LINK.match(entry), entry

    def test_an_optional_section_comes_last(self) -> None:
        assert re.findall(r"^## (.+)$", TEXT, re.MULTILINE)[-1] == "Optional"


class TestCoverage:
    @pytest.mark.parametrize("name", [t.name for t in SCHEMA.tables])
    def test_every_table_is_documented(self, name: str) -> None:
        assert f"`{name}`" in TEXT

    @pytest.mark.parametrize("name", [f.get_metadata().name for f in SCHEMA.functions])
    def test_every_function_is_documented_with_its_signature(self, name: str) -> None:
        assert re.search(rf"`{name}\([a-z_]*\)`", TEXT), name

    @pytest.mark.parametrize(
        "column",
        sorted(
            {
                *ITEM_SCHEMA.names,
                *FEED_SCHEMA.names,
                *COMMENT_SCHEMA.names,
                *USER_SCHEMA.names,
                *UPDATED_USERS_SCHEMA.names,
                *MAX_ITEM_SCHEMA.names,
            }
        ),
    )
    def test_every_column_is_documented(self, column: str) -> None:
        assert f"`{column}`" in TEXT

    def test_item_columns_are_listed_in_schema_order(self) -> None:
        documented = re.findall(r"^- `([a-z_]+)` `", TEXT, re.MULTILINE)
        # The last two flags share one bullet, so the pattern sees `deleted` but
        # not `dead`; the coverage test above already requires `dead`.
        assert [c for c in documented if c in ITEM_SCHEMA.names] == ITEM_SCHEMA.names[:-1]

    def test_every_feed_name_is_listed(self) -> None:
        for feed in hn_api.FEEDS:
            assert f"`'{feed}'`" in TEXT

    def test_the_recent_items_bound_matches_the_code(self) -> None:
        assert f"1 to {RECENT_ITEMS_MAX:,}" in TEXT

    def test_the_renames_are_explained(self) -> None:
        """Agents that know the raw API will reach for `by` and `time`."""
        assert "calls this `by`" in TEXT
        assert "calls this `time`" in TEXT


class TestRecipes:
    def test_there_are_recipes(self) -> None:
        assert len(sql_statements()) >= 10

    def test_every_recipe_is_catalog_qualified(self) -> None:
        for statement in sql_statements():
            if statement.upper().startswith(("SELECT", "WITH")):
                assert "hackernews.main." in statement, statement
