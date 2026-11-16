"""The compatibility policy is published for people (S-017-1).

``docs/compatibility.md`` names each stable surface, the additive and
breaking changes, the release rules before and from 1.0, the deprecation
window, the project format and ``cloudfall config upgrade``; ``README.md``
and ``CONTRIBUTING.md`` link it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from cloudfall.project_format import (
    PROJECT_FORMAT_UNSUPPORTED_EXIT,
    SUPPORTED_FORMATS,
    WARNING_PROJECT_FORMAT_OLD,
    FormatErrorCode,
)

ROOT = Path(__file__).parents[2]
POLICY = ROOT / "docs" / "compatibility.md"
STABLE_SURFACES = (
    "Resource schemas",
    "The CLI envelope",
    "Exit codes",
    "Record and receipt schemas",
    "MCP tool names",
)
RULES = (
    "**additive**",
    "**breaking**",
    "| Patch |",
    "| Minor |",
    "| Major |",
    "Before 1.0",
    "From 1.0",
    "## How long a deprecated name stays",
    "removed in 0.9.0",
    "## Project format version",
    "[tool.cloudfall]",
    f"format = {SUPPORTED_FORMATS.current}",
    "## `cloudfall config upgrade`",
    WARNING_PROJECT_FORMAT_OLD,
    f"`PROJECT_FORMAT_UNSUPPORTED` ({PROJECT_FORMAT_UNSUPPORTED_EXIT})",
    *(code.value for code in FormatErrorCode),
)
_LINK = re.compile(r"\]\((?P<target>[^)#\s]+)")


def _text() -> str:
    return POLICY.read_text(encoding="utf-8")


@pytest.mark.parametrize("surface", STABLE_SURFACES)
def test_s017_1_the_policy_names_each_stable_surface(surface: str) -> None:
    assert f"| {surface} |" in _text()


@pytest.mark.parametrize("rule", RULES)
def test_s017_1_the_policy_states_each_rule(rule: str) -> None:
    assert rule in _text()


@pytest.mark.parametrize("document", ["README.md", "CONTRIBUTING.md"])
def test_s017_1_readme_and_contributing_link_the_policy(document: str) -> None:
    assert "](docs/compatibility.md)" in (ROOT / document).read_text(encoding="utf-8")


def test_s017_1_every_relative_link_of_the_policy_resolves() -> None:
    targets = [
        match["target"]
        for match in _LINK.finditer(_text())
        if "://" not in match["target"]
    ]
    assert targets
    for target in targets:
        assert (POLICY.parent / target).exists(), target
