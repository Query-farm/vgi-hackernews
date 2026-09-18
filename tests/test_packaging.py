"""The entry-point scripts must be runnable on their own, and the license detectable.

`uv run hackernews_worker.py` resolves dependencies from the script's PEP 723
header, not from `pyproject.toml`, so the two lists drift silently: the package
imports fine under pytest while the worker a client actually launches dies at
import.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ("hackernews_worker.py", "serve.py")

#: PEP 723 inline script metadata: a `# /// script` ... `# ///` comment block.
_BLOCK = re.compile(r"^# /// script$(.+?)^# ///$", re.MULTILINE | re.DOTALL)


def _name(requirement: str) -> str:
    """The bare distribution name from a requirement string."""
    return re.split(r"[\[><=!~;\s]", requirement, maxsplit=1)[0].strip().lower()


def _script_dependencies(path: Path) -> set[str]:
    match = _BLOCK.search(path.read_text())
    assert match is not None, f"{path.name} has no PEP 723 script header"
    body = "".join(
        line.removeprefix("# ").removeprefix("#") for line in match.group(1).splitlines(keepends=True)
    )
    return {_name(spec) for spec in tomllib.loads(body)["dependencies"]}


@pytest.fixture(scope="module")
def project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


class TestScriptHeaders:
    @pytest.mark.parametrize("script", SCRIPTS)
    def test_header_covers_every_runtime_dependency(self, script: str, project: dict) -> None:
        missing = {_name(d) for d in project["dependencies"]} - _script_dependencies(ROOT / script)
        assert missing == set(), f"{script} would fail at import without {sorted(missing)}"

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_header_declares_nothing_unknown(self, script: str, project: dict) -> None:
        """The scripts also pull vgi-rpc directly, but nothing beyond that."""
        known = {_name(d) for d in project["dependencies"]} | {"vgi-rpc"}
        assert _script_dependencies(ROOT / script) - known == set()


class TestLicense:
    def test_license_is_an_spdx_expression(self, project: dict) -> None:
        assert project["license"] == "MIT"

    def test_no_deprecated_license_classifier(self, project: dict) -> None:
        """PEP 639: a license classifier beside an expression is what packaging tools reject."""
        assert [c for c in project.get("classifiers", []) if c.startswith("License ::")] == []

    def test_license_file_is_bare_mit(self) -> None:
        """Anything appended after the MIT text breaks GitHub's license detection."""
        text = (ROOT / "LICENSE").read_text()
        assert text.startswith("MIT License")
        assert text.rstrip().endswith("SOFTWARE.")

    def test_content_terms_live_in_notice(self) -> None:
        notice = (ROOT / "NOTICE").read_text()
        assert "news.ycombinator.com" in notice
        assert "MIT License" in notice
