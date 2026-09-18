"""Catalog metadata invariants, checked offline.

vgi-lint checks the same things against a running worker; these catch the
cheap mistakes before a worker is ever started, and pin the few properties
the linter cannot see — that the private graders and the public prompts stay
in step, for one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import yaml

from vgi_hackernews import hn_api
from vgi_hackernews.meta import column_comments, result_columns_schema
from vgi_hackernews.worker import _HACKERNEWS_CATALOG

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = _HACKERNEWS_CATALOG.schemas[0]
FUNCTIONS = list(SCHEMA.functions)
TABLES = list(SCHEMA.tables)


def _meta(func: type) -> Any:
    return func.get_metadata()  # type: ignore[attr-defined]


def _categories() -> set[str]:
    return {c["name"] for c in json.loads(SCHEMA.tags["vgi.categories"])}


class TestFunctions:
    @pytest.mark.parametrize("func", FUNCTIONS, ids=lambda f: _meta(f).name)
    def test_documented(self, func: type) -> None:
        meta = _meta(func)
        assert meta.description
        for key in ("vgi.doc_llm", "vgi.doc_md", "vgi.category", "vgi.example_queries"):
            assert meta.tags.get(key), f"{meta.name} lacks {key}"
        assert meta.tags["vgi.category"] in _categories()

    @pytest.mark.parametrize(
        "func", [f for f in FUNCTIONS if hasattr(f, "FIXED_SCHEMA")], ids=lambda f: _meta(f).name
    )
    def test_declared_result_schema_is_the_real_one(self, func: Any) -> None:
        assert _meta(func).tags["vgi.result_columns_schema"] == result_columns_schema(func.FIXED_SCHEMA)

    @pytest.mark.parametrize("func", FUNCTIONS, ids=lambda f: _meta(f).name)
    def test_examples_are_catalog_qualified(self, func: type) -> None:
        for example in json.loads(_meta(func).tags["vgi.example_queries"]):
            assert "hackernews.main." in example["sql"], example["sql"]

    def test_names_are_unique(self) -> None:
        names = [_meta(f).name for f in FUNCTIONS]
        assert len(names) == len(set(names))


class TestTables:
    @pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
    def test_documented(self, table: Any) -> None:
        assert table.comment
        for key in ("vgi.doc_llm", "vgi.doc_md", "vgi.category", "vgi.title", "vgi.keywords"):
            assert table.tags.get(key), f"{table.name} lacks {key}"
        assert table.tags["vgi.category"] in _categories()

    @pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
    def test_every_column_is_commented(self, table: Any) -> None:
        schema: pa.Schema = table.function.FIXED_SCHEMA
        assert table.column_comments == column_comments(schema)
        assert set(table.column_comments) == set(schema.names)

    @pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
    def test_has_a_primary_key(self, table: Any) -> None:
        assert table.primary_key

    def test_every_feed_has_a_table(self) -> None:
        bound = {t.arguments.positional[0].as_py() for t in TABLES if t.arguments is not None}
        assert bound == set(hn_api.FEEDS)

    def test_backing_functions_are_registered(self) -> None:
        """A table's scan is dispatched by function name, so it must be in the schema."""
        for table in TABLES:
            assert table.function in FUNCTIONS, table.name

    def test_descriptions_are_distinct(self) -> None:
        """A table and the same-named function behind it still describe different objects."""
        descriptions = [t.comment for t in TABLES] + [_meta(f).description for f in FUNCTIONS]
        assert len(descriptions) == len(set(descriptions))


class TestAgentSuite:
    PRIVATE = {"reference_sql", "check_sql", "success_criteria", "unordered", "ignore_column_names"}

    def _public(self) -> list[dict[str, Any]]:
        return json.loads(_HACKERNEWS_CATALOG.tags["vgi.agent_test_tasks"])

    def _private(self) -> list[dict[str, Any]]:
        return yaml.safe_load((ROOT / "vgi-agent-tests.yaml").read_text())["tasks"]

    def test_the_catalog_publishes_prompts_only(self) -> None:
        """Anything in the tag is visible to the agent being graded."""
        for task in self._public():
            assert set(task) == {"name", "prompt"}, task["name"]

    def test_every_prompt_has_a_grader_and_every_grader_a_prompt(self) -> None:
        assert [t["name"] for t in self._public()] == [t["name"] for t in self._private()]

    def test_every_grader_has_a_reference(self) -> None:
        for task in self._private():
            assert task.get("reference_sql"), task["name"]
            assert set(task) - {"name"} <= self.PRIVATE, task["name"]

    def test_the_suite_touches_every_object(self) -> None:
        text = " ".join(f"{t.get('reference_sql', '')} {t.get('check_sql', '')}" for t in self._private())
        names = {t.name for t in TABLES} | {_meta(f).name for f in FUNCTIONS}
        missing = {n for n in names if f"hackernews.main.{n}" not in text}
        assert missing == set()


class TestExecutableExamples:
    def test_well_formed_and_qualified(self) -> None:
        examples = json.loads(_HACKERNEWS_CATALOG.tags["vgi.executable_examples"])
        assert 1 <= len(examples) <= 10
        for example in examples:
            assert example["description"] and "hackernews.main." in example["sql"]
            assert "expected_result" in example
