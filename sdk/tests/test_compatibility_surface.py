"""The stable names a release keeps, and the deprecation windows it closes (S-017).

``surface/exit-codes.json`` and ``surface/mcp-tools.json`` hold the exit-code
tables of both CLIs and the MCP tool names served for ``config/examples`` and
for a team's Ansible repository. A release that drops a name or renumbers a
code fails here, unless the list declared the name deprecated with a
``removed_in`` the package version has reached. A new name fails until it is
added to the list, so every change to the surface shows in a diff.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from ansible_repository import MARKER, PURGE, ansible_repository
from cloudfall import ansible_reader, validation
from cloudfall.app import app
from cloudfall_engine.cli import app as engine_app

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

    from treaty import App

ROOT = Path(__file__).parents[2]
SURFACE = Path(__file__).parent / "surface"
EXIT_CODES = SURFACE / "exit-codes.json"
MCP_TOOLS = SURFACE / "mcp-tools.json"
EXAMPLES = ROOT / "config" / "examples"
SERVE = ("-c", "from cloudfall.cli import run; run()", "mcp", "serve")
MARKED_SOURCES = (
    ROOT / "sdk" / "src" / "cloudfall" / "app.py",
    ROOT / "engine" / "src" / "cloudfall_engine" / "cli.py",
)
NAME_READERS = (validation, ansible_reader)
PACKAGE_VERSION = version("cloudfall")
_RELEASE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _release(text: str, *, exact: bool) -> tuple[int, int, int]:
    found = _RELEASE.fullmatch(text) if exact else _RELEASE.match(text)
    if found is None:
        message = f"{text!r} is not a release such as 0.9.0"
        raise ValueError(message)
    major, minor, patch = found.groups()
    return int(major), int(minor), int(patch)


def reached(package: str, removed_in: str) -> bool:
    """Whether ``package`` is the release that removes the name, or later.

    A pre-release counts as its release: 0.9.0rc1 is a candidate of 0.9.0, so
    it ships without what 0.9.0 removes. ``removed_in`` is a plain release.
    """
    return _release(package, exact=False) >= _release(removed_in, exact=True)


def surface_drift(
    listed: Mapping[str, int | None],
    served: Mapping[str, int | None],
    deprecated: Mapping[str, str],
    package: str,
    where: str,
) -> list[str]:
    """Return each way ``served`` breaks the ``listed`` names, as a message.

    A tool name has no number, so both sides map it to None.
    """
    problems: list[str] = []
    for name, number in listed.items():
        if name not in served:
            removed_in = deprecated.get(name)
            if removed_in is None or not reached(package, removed_in):
                problems.append(
                    f"{where}: {name} is no longer served; a stable name goes only "
                    "after a deprecation window (S-017); restore it"
                )
        elif served[name] != number:
            problems.append(
                f"{where}: {name} is {served[name]}, the list says {number}; "
                "a stable exit code keeps its number"
            )
        elif name in deprecated and reached(package, deprecated[name]):
            problems.append(
                f"{where}: {name} is still served though {package} reached its "
                f"removed_in {deprecated[name]}; remove it"
            )
    problems.extend(
        f"{where}: {name} is declared deprecated but not listed; list it or "
        'drop it from "deprecated"'
        for name in deprecated
        if name not in listed
    )
    problems.extend(
        f"{where}: {name} is new; add it to {SURFACE.name}/ in the same change"
        for name in served
        if name not in listed
    )
    return problems


def _listed(path: Path) -> dict[str, dict[str, object]]:
    return cast("dict[str, dict[str, object]]", json.loads(path.read_text("utf-8")))


def _exit_codes(cli: App) -> dict[str, int | None]:
    """Every exit code the CLI's manifest names, by name."""
    manifest = cli.manifest()
    commands = cast("dict[str, dict[str, Any]]", manifest["commands"])
    tables = [
        cast("dict[str, dict[str, Any]]", manifest["exit_codes"]),
        *(command["exit_codes"] for command in commands.values()),
    ]
    codes: dict[str, int | None] = {}
    for table in tables:
        for number, entry in table.items():
            name = entry["name"]
            assert codes.setdefault(name, int(number)) == int(number), name
    return codes


def _tool_names(*flags: str, cwd: Path) -> dict[str, int | None]:
    done = subprocess.run(  # noqa: S603 - fixed interpreter and arguments.
        [sys.executable, *SERVE, *flags, "--list-tools"],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return dict.fromkeys(tool["name"] for tool in json.loads(done.stdout)["tools"])


def _check(
    entry: dict[str, object], served: Mapping[str, int | None], where: str
) -> list[str]:
    raw = entry["codes"] if "codes" in entry else entry["tools"]
    listed: dict[str, int | None] = (
        cast("dict[str, int | None]", raw)
        if isinstance(raw, dict)
        else dict.fromkeys(cast("list[str]", raw))
    )
    deprecated = cast("dict[str, str]", entry["deprecated"])
    return surface_drift(listed, served, deprecated, PACKAGE_VERSION, where)


@pytest.mark.parametrize(
    ("cli", "name"), [(app, "cloudfall"), (engine_app, "cloudfall-engine")]
)
def test_s017_12_each_cli_keeps_its_exit_codes(cli: App, name: str) -> None:
    entry = _listed(EXIT_CODES)[name]

    assert _check(entry, _exit_codes(cli), f"{EXIT_CODES.name} {name}") == []


def test_s017_12_the_examples_project_keeps_its_mcp_tools(tmp_path: Path) -> None:
    served = _tool_names("--project", str(EXAMPLES), cwd=tmp_path)
    entry = _listed(MCP_TOOLS)["config/examples"]

    assert _check(entry, served, f"{MCP_TOOLS.name} config/examples") == []


def test_s017_12_an_ansible_repository_keeps_its_mcp_tools(tmp_path: Path) -> None:
    repository = ansible_repository(
        tmp_path, **{"write-marker": MARKER, "purge": PURGE}
    )
    served = _tool_names("--repository", str(repository), cwd=tmp_path)
    entry = _listed(MCP_TOOLS)["ansible-repository"]

    assert _check(entry, served, f"{MCP_TOOLS.name} ansible-repository") == []


def test_s017_12_a_missing_renumbered_or_unlisted_name_fails() -> None:
    listed: dict[str, int | None] = {"CONFIG_INVALID": 80, "DRIFT": 83, "OLD": 99}
    served: dict[str, int | None] = {"CONFIG_INVALID": 81, "NEW": 100}

    problems = surface_drift(listed, served, {}, "0.8.0", "codes")

    assert len(problems) == 4  # one per broken name
    assert "CONFIG_INVALID is 81, the list says 80" in problems[0]
    assert "DRIFT is no longer served" in problems[1]
    assert "OLD is no longer served" in problems[2]
    assert "NEW is new; add it" in problems[3]


def test_s017_12_a_name_goes_once_its_removed_in_is_reached() -> None:
    listed: dict[str, int | None] = {"add_server-type": None, "add_baseline": None}
    without: dict[str, int | None] = {"add_baseline": None}
    deprecated = {"add_server-type": "0.9.0"}

    assert surface_drift(listed, without, deprecated, "0.9.0", "t") == []
    assert surface_drift(listed, without, deprecated, "0.9.0rc1", "t") == []
    early = surface_drift(listed, without, deprecated, "0.8.3", "t")
    assert ["add_server-type is no longer served" in p for p in early] == [True]
    stretched = surface_drift(listed, listed, deprecated, "0.9.0", "t")
    assert ["still served" in p for p in stretched] == [True]


def test_s017_12_an_added_name_passes_once_listed() -> None:
    served: dict[str, int | None] = {"CONFIG_INVALID": 80, "NEW": 100}

    assert surface_drift(served, served, {}, "0.8.0", "codes") == []


@dataclass(frozen=True, slots=True)
class Removal:
    """One deprecated name still in the code, and the release that removes it."""

    where: str
    removed_in: str | None


def deprecation_marks(source: Path) -> list[Removal]:
    """Every ``Deprecated(...)`` declared in ``source``, with its ``removed_in``."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    marks: list[Removal] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Deprecated"
        ):
            continue
        given = {keyword.arg: keyword.value for keyword in node.keywords}
        value = given.get("removed_in")
        removed_in = (
            value.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
            else None
        )
        marks.append(Removal(f"{source.name}:{node.lineno}", removed_in))
    return marks


def deprecated_names(module: ModuleType) -> list[Removal]:
    """Every ``LEGACY_*`` name ``module`` still reads, with its ``removed_in``."""
    removals = cast("Mapping[str, str]", module.DEPRECATED_NAMES_REMOVED_IN)
    return [
        Removal(f"{module.__name__}.{constant} ({value})", removals.get(value))
        for constant, value in vars(module).items()
        if constant.startswith("LEGACY_") and isinstance(value, str)
    ]


def overdue(package: str, removals: list[Removal]) -> list[str]:
    """Return each removal with no ``removed_in``, or one ``package`` reached."""
    problems: list[str] = []
    for removal in removals:
        if removal.removed_in is None:
            problems.append(f"{removal.where} has no literal removed_in")
        elif reached(package, removal.removed_in):
            problems.append(
                f"{removal.where} is still declared though {package} reached its "
                f"removed_in {removal.removed_in}; remove it"
            )
    return problems


def _every_removal() -> list[Removal]:
    return [
        *(mark for source in MARKED_SOURCES for mark in deprecation_marks(source)),
        *(name for module in NAME_READERS for name in deprecated_names(module)),
    ]


def test_s017_13_no_deprecation_outlives_its_removed_in() -> None:
    removals = _every_removal()

    # D-11's marks and names are found: the scan is not looking elsewhere.
    assert len(removals) >= 6  # two marks, four names
    assert overdue(PACKAGE_VERSION, removals) == []


def test_s017_13_the_d11_names_fail_once_0_9_0_is_reached() -> None:
    problems = overdue("0.9.0", _every_removal())

    assert len(problems) == len(_every_removal())
    assert any("app.py" in problem for problem in problems)
    assert any("cloudfall_server_types" in problem for problem in problems)
    assert any("serverType" in problem for problem in problems)


def test_s017_13_a_mark_without_removed_in_fails(tmp_path: Path) -> None:
    source = tmp_path / "cli.py"
    source.write_text(
        'OLD = Deprecated(since="0.8.0", replacement="new")\n', encoding="utf-8"
    )

    problems = overdue("0.8.0", deprecation_marks(source))

    assert problems == ["cli.py:1 has no literal removed_in"]
