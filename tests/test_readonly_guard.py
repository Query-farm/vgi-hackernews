"""Structural guard: this worker only ever reads, and only from the API it names.

The Hacker News API is read-only anyway, so the risk is not a write to Hacker
News — it is a request that goes somewhere unintended. Every value that reaches
a URL comes from user SQL, so the guard is both that there is one place requests
are made, and that no argument can change where they go.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from vgi_hackernews import hn_api

from .conftest import FakeSite

PACKAGE = Path(__file__).resolve().parent.parent / "vgi_hackernews"

#: Client methods that would send anything other than a GET.
WRITE_VERBS = {"post", "put", "patch", "delete", "request", "stream", "send"}


class TestOneReadOnlyChokepoint:
    def test_no_write_shaped_calls_anywhere(self) -> None:
        offenders = [
            f"{path.name}: .{node.func.attr}()"
            for path in PACKAGE.rglob("*.py")
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in WRITE_VERBS
        ]
        assert offenders == []

    def test_only_the_api_module_touches_http(self) -> None:
        users = sorted(path.name for path in PACKAGE.rglob("*.py") if "httpx2" in path.read_text())
        assert users == ["hn_api.py"]

    def test_get_is_called_in_exactly_one_place(self) -> None:
        tree = ast.parse((PACKAGE / "hn_api.py").read_text())
        gets = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "http"
        ]
        assert len(gets) == 1


class TestArgumentsCannotRedirect:
    @pytest.mark.parametrize(
        "name",
        ["../maxitem", "..%2Fmaxitem", "pg/../../item/1", "pg?print=pretty", "pg#x", "pg.json", " pg", ""],
    )
    def test_a_hostile_username_is_never_sent(self, site: FakeSite, name: str) -> None:
        assert hn_api.user(name) is None
        assert site.requests == []

    def test_an_item_id_is_always_an_integer_path(self, site: FakeSite) -> None:
        hn_api.fetch_items([1, 2**62])
        # Fetched concurrently, so compare without depending on arrival order.
        assert sorted(site.requests) == sorted(["/v0/item/1.json", f"/v0/item/{2**62}.json"])

    def test_the_segment_encoder_leaves_nothing_structural(self) -> None:
        assert hn_api.segment("../a?b#c") == "..%2Fa%3Fb%23c"

    def test_requests_stay_under_the_versioned_prefix(self, site: FakeSite) -> None:
        site.feeds = {name: [] for name in hn_api.FEEDS}
        for name in hn_api.FEEDS:
            hn_api.feed(name)
        hn_api.updates()
        hn_api.max_item()
        hn_api.user("pg")
        assert all(path.startswith("/v0/") for path in site.requests), site.requests
