"""Bring a project to the current format (spec S-017).

A project's format says which rewrites it has had. The upgrade runs each
step from the project's format to the current one, in order, from a
registry keyed by the format a step starts from, then writes
``format = <current>`` into the project's ``pyproject.toml``, keeping every
other byte of it. It runs on a project below the oldest format a release
reads, which every other command refuses, since it exists for that project;
it refuses a project written by a newer Cloudfall, and a format it cannot
read, before anything is changed. A project at the current format runs no
step and is left as it is.

The step from format 0 rewrites the deprecated names. A ``Baseline`` was a
``ServerType`` before 0.8.0 (D-11): its files lived in ``server-types/``
and a ``Server`` named it as ``spec.serverType``. Config validation still
reads those names and warns about each file. The step moves the files to
``baselines/`` and rewrites only the scalars that carry an old name, found
through the YAML parser's positions, so every other byte of a file,
comments and formatting included, stays as it was.

Receipts and operator proposals lay under ``tmp/`` before 0.8.0, which git
ignores, so the record did not survive losing the management host. They
live in the committed ``record/`` now (D-16), and the step from format 0
moves each one there byte for byte. A file already in the record with the same bytes
is a copy, so the one under ``tmp/`` is dropped; a different file there is
refused, since either could be the record to keep. The one change it makes
to a record is to an old release receipt: its release command's output
tail moves to a ``0600`` file under ``tmp/output/releases/``, and the
receipt keeps that file's path and SHA-256.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import yaml

from cloudfall.domain import ResourceKind
from cloudfall.project import RELEASE_OUTPUT_SUFFIX, RecordStore
from cloudfall.project_format import (
    PYPROJECT_FILE,
    FormatRange,
    ProjectFormat,
    check_project_format,
    read_project_format,
)
from cloudfall.validation import (
    BASELINE_FIELD,
    LEGACY_BASELINE_DIRECTORY,
    LEGACY_BASELINE_FIELD,
    LEGACY_BASELINE_KIND,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

ERROR_UPGRADE_TARGET_EXISTS = "upgrade_target_exists"
ERROR_UPGRADE_SYMLINK = "upgrade_symlink"
ERROR_UPGRADE_RECORD_INVALID = "upgrade_record_invalid"
ERROR_UPGRADE_FORMAT_UNWRITABLE = "upgrade_format_unwritable"

_FORMAT_TABLE = "tool.cloudfall"
_TABLE_HEADER = re.compile(r"[ \t]*\[(?P<name>[^\[\]]+)\][ \t]*(?:#.*)?")
_FORMAT_KEY = re.compile(r"(?P<lead>[ \t]*format[ \t]*=[ \t]*)(?P<value>[^ \t#\r\n]+)")

_YAML_SUFFIXES = frozenset({".yaml", ".yml"})
_RECORD_PATTERN = "*.json"
_OUTPUT_TAIL = "outputTail"
_OUTPUT_FILE_MODE = 0o600
_OUTPUT_DIRECTORY_MODE = 0o700
_QUOTED_STYLES = frozenset({"'", '"'})


class UpgradeError(RuntimeError):
    """Fail-fast upgrade error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        """Record the failure code and human-readable detail."""
        self.code = code
        self.detail = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class FileUpgrade:
    """One project file the upgrade moved or rewrote."""

    path: str
    """Where the file is now, relative to the project."""

    moved_from: str | None
    """Where it was, when the upgrade moved it."""

    renamed: tuple[str, ...]
    """Each old name rewritten in it, as ``old -> new``."""

    def as_dict(self) -> dict[str, object]:
        """Serialize for the CLI and agent envelope."""
        return {
            "path": self.path,
            "movedFrom": self.moved_from,
            "renamed": list(self.renamed),
        }


@dataclass(frozen=True, slots=True)
class UpgradeResult:
    """Outcome of one ``cloudfall config upgrade``."""

    project: Path
    files: tuple[FileUpgrade, ...]
    from_format: ProjectFormat
    """The format the project was written in before the upgrade."""
    format: ProjectFormat
    """The format the project is written in now: the current one."""

    @property
    def changed(self) -> bool:
        """Whether the upgrade changed any file, ``pyproject.toml`` included."""
        return bool(self.files) or self.from_format != self.format

    def as_dict(self) -> dict[str, object]:
        """Serialize for system boundaries."""
        return {
            "status": "ok",
            "project": str(self.project),
            "upgraded": [upgraded.as_dict() for upgraded in self.files],
            "fromFormat": self.from_format.value,
            "format": self.format.value,
        }


@dataclass(frozen=True, slots=True)
class _Planned:
    source: Path
    target: Path
    text: str | None
    """The rewritten content, or None when only the folder changes."""
    renamed: tuple[str, ...]
    record: bool = False
    """A receipt or proposal moving from ``tmp/`` into ``record/``."""
    output: _Output | None = None
    """Raw output the record held, kept under ``tmp/`` instead."""

    def content(self) -> bytes:
        """Return the bytes the target ends up holding."""
        if self.text is not None:
            return self.text.encode("utf-8")
        return self.source.read_bytes()


@dataclass(frozen=True, slots=True)
class _Output:
    path: Path
    text: str


@dataclass(frozen=True, slots=True)
class _Step:
    """One rewrite from a format to the next one."""

    plan: Callable[[Path], tuple[_Planned, ...]]
    """The files the step moves or rewrites, planned before its first write."""
    emptied: Callable[[Path], tuple[Path, ...]]
    """Folders the step leaves behind, removed once they are empty."""


def upgradable_format(project: Path, supported: FormatRange) -> ProjectFormat:
    """Return the project's format when ``config upgrade`` may run on it.

    It is every other command's check without the too-old refusal: a format
    below ``supported.oldest`` is the project the upgrade exists for. A
    format above ``supported.current`` still raises
    ``project_format_too_new`` and one that cannot be read
    ``project_format_invalid``.
    """
    project_format = read_project_format(project, supported)
    if project_format < supported.oldest:
        return project_format
    return check_project_format(project, supported)


def upgrade_project(project: Path, supported: FormatRange) -> UpgradeResult:
    """Run each step from the project's format to the current one.

    Then write ``format = <current>`` into ``pyproject.toml``. The new
    ``pyproject.toml`` is made, and each step's plan, before that step's
    first write, so a move that would overwrite an existing file, a file or
    folder reached through a symlink, or a ``pyproject.toml`` the format
    cannot be written into without changing another line fails with nothing
    changed. A project at the current format runs no step and changes
    nothing.
    """
    start = upgradable_format(project, supported)
    steps = _steps_between(start, supported.current)
    pyproject = (
        _pyproject_with_format(project, supported.current)
        if start != supported.current
        else None
    )
    files: list[FileUpgrade] = []
    for step in steps:
        files.extend(_run_step(project, step))
    if pyproject is not None:
        pyproject.path.write_text(pyproject.text, encoding="utf-8", newline="")
    return UpgradeResult(
        project=project,
        files=tuple(files),
        from_format=start,
        format=supported.current,
    )


def _steps_between(start: ProjectFormat, current: ProjectFormat) -> tuple[_Step, ...]:
    """Return the steps from ``start`` to ``current``, in order."""
    steps: list[_Step] = []
    for value in range(start.value, current.value):
        step = UPGRADE_STEPS.get(ProjectFormat(value))
        if step is None:
            message = (
                f"no upgrade step from format {value}: a release that raises the "
                "current format adds the step that leads to it"
            )
            raise LookupError(message)
        steps.append(step)
    return tuple(steps)


def _run_step(project: Path, step: _Step) -> tuple[FileUpgrade, ...]:
    """Check one step's whole plan, then carry it out."""
    planned = step.plan(project)
    for item in planned:
        _refuse_symlink(project, item.source)
        _refuse_symlink(project, item.target)
        if item.target != item.source and _occupied(item):
            message = (
                f"cannot move {item.source.relative_to(project).as_posix()}: "
                f"{item.target.relative_to(project).as_posix()} already exists"
                + (" with different content" if item.record else "")
            )
            raise UpgradeError(ERROR_UPGRADE_TARGET_EXISTS, message)
        if item.output is not None:
            _refuse_symlink(project, item.output.path)
            _refuse_other_output(project, item)
    for item in planned:
        if item.output is not None:
            _keep_output(item.output)
        if item.text is not None:
            item.source.write_text(item.text, encoding="utf-8", newline="")
        if item.target == item.source:
            continue
        if item.record and item.target.exists():
            # A copy of the same bytes is already in the record.
            item.source.unlink()
            continue
        item.target.parent.mkdir(parents=True, exist_ok=True)
        item.source.rename(item.target)
    for directory in step.emptied(project):
        _remove_empty(directory)
    return tuple(
        FileUpgrade(
            path=item.target.relative_to(project).as_posix(),
            moved_from=(
                item.source.relative_to(project).as_posix()
                if item.target != item.source
                else None
            ),
            renamed=item.renamed,
        )
        for item in planned
    )


def _pyproject_with_format(project: Path, current: ProjectFormat) -> _Output:
    """Return ``pyproject.toml`` with ``format = <current>``, all else kept.

    A missing file is created with the table alone, a file without the
    table gets it appended, and a ``format`` under a ``[tool.cloudfall]``
    header gets its value replaced. The result is parsed back and must equal
    the old document with only the format changed; a file whose
    ``tool.cloudfall`` is an inline table or dotted keys fails that, and is
    refused for a person to edit.
    """
    path = project / PYPROJECT_FILE
    _refuse_symlink(project, path)
    if not path.exists():
        return _Output(path=path, text=f"[{_FORMAT_TABLE}]\nformat = {current}\n")
    # The format reader has already read it as UTF-8 TOML.
    text = path.read_bytes().decode("utf-8")
    document = tomllib.loads(text)
    expected = copy.deepcopy(document)
    expected.setdefault("tool", {}).setdefault("cloudfall", {})["format"] = (
        current.value
    )
    tool = document.get("tool", {})
    rewritten = (
        _replace_format(text, current)
        if "cloudfall" in tool
        else _append_format(text, current)
    )
    try:
        written = tomllib.loads(rewritten) if rewritten is not None else None
    except tomllib.TOMLDecodeError:
        written = None
    if rewritten is None or written != expected:
        message = (
            f"cannot write `format = {current}` into {PYPROJECT_FILE} without "
            f"changing its other lines; write `{_FORMAT_TABLE}` as a "
            f"`[{_FORMAT_TABLE}]` table with its `format` on a line of its own, "
            "then run the upgrade again"
        )
        raise UpgradeError(ERROR_UPGRADE_FORMAT_UNWRITABLE, message)
    return _Output(path=path, text=rewritten)


def _append_format(text: str, current: ProjectFormat) -> str:
    """Return ``text`` with a ``[tool.cloudfall]`` table appended."""
    newline = "\r\n" if "\r\n" in text else "\n"
    table = f"[{_FORMAT_TABLE}]{newline}format = {current}{newline}"
    if not text:
        return table
    ending = "" if text.endswith("\n") else newline
    return f"{text}{ending}{newline}{table}"


def _replace_format(text: str, current: ProjectFormat) -> str | None:
    """Return ``text`` with the value of its ``[tool.cloudfall]`` format replaced.

    None when no ``format`` line follows a ``[tool.cloudfall]`` header.
    """
    lines = text.splitlines(keepends=True)
    in_table = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith("["):
            header = _TABLE_HEADER.fullmatch(line.rstrip("\r\n"))
            name = "" if header is None else header["name"]
            in_table = re.sub(r"[\s\"']", "", name) == _FORMAT_TABLE
            continue
        if not in_table:
            continue
        key = _FORMAT_KEY.match(line)
        if key is None:
            continue
        start, end = key.span("value")
        lines[index] = f"{line[:start]}{current}{line[end:]}"
        return "".join(lines)
    return None


def _plan_from_format_0(project: Path) -> tuple[_Planned, ...]:
    """D-11's names to the current ones, and D-16's record out of ``tmp/``."""
    return (*_plan(project), *_record_moves(project))


def _emptied_from_format_0(project: Path) -> tuple[Path, ...]:
    return (
        project / LEGACY_BASELINE_DIRECTORY,
        *(project / store.legacy_directory for store in RecordStore),
    )


def _record_moves(project: Path) -> Iterator[_Planned]:
    """Plan each receipt and proposal under ``tmp/`` into ``record/``.

    A release receipt from before 0.8.0 holds its release command's output
    tail, which is raw application output and may hold a secret: the plan
    keeps that text in a file under ``tmp/`` and leaves only its path and
    SHA-256 in the receipt, whether the receipt is moving or already in
    ``record/``.
    """
    for store in RecordStore:
        legacy = project / store.legacy_directory
        if legacy.is_dir():
            for path in sorted(legacy.glob(_RECORD_PATTERN)):
                if path.is_file():
                    yield _record_step(
                        project, store, path, project / store.directory / path.name
                    )
        if store is not RecordStore.RELEASES:
            continue
        current = project / store.directory
        if current.is_dir():
            for path in sorted(current.glob(_RECORD_PATTERN)):
                step = _record_step(project, store, path, path)
                if path.is_file() and step.output is not None:
                    yield step


def _record_step(
    project: Path, store: RecordStore, source: Path, target: Path
) -> _Planned:
    moved = _Planned(source=source, target=target, text=None, renamed=(), record=True)
    if store is not RecordStore.RELEASES or not source.is_file():
        return moved
    text, output = _without_output_tail(project, source)
    if output is None:
        return moved
    reference = output.path.relative_to(project).as_posix()
    return _Planned(
        source=source,
        target=target,
        text=text,
        renamed=(f"spec.releaseCommand.{_OUTPUT_TAIL} -> {reference}",),
        record=True,
        output=output,
    )


def _without_output_tail(project: Path, path: Path) -> tuple[str, _Output | None]:
    """Return the receipt with its output tail replaced by a file reference."""
    shown = path.relative_to(project).as_posix()
    raw = path.read_text(encoding="utf-8")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        message = f"{shown} is not a JSON release receipt: {error}"
        raise UpgradeError(ERROR_UPGRADE_RECORD_INVALID, message) from error
    spec = document.get("spec") if isinstance(document, dict) else None
    if not isinstance(spec, dict):
        return raw, None
    command = spec.get("releaseCommand")
    if not isinstance(command, dict) or _OUTPUT_TAIL not in command:
        return raw, None
    tail = command[_OUTPUT_TAIL]
    component = spec.get("component")
    release = spec.get("release")
    if not all(isinstance(value, str) for value in (tail, component, release)):
        message = (
            f"{shown} holds spec.releaseCommand.{_OUTPUT_TAIL} but no string "
            "component, release and tail to name its output file by"
        )
        raise UpgradeError(ERROR_UPGRADE_RECORD_INVALID, message)
    name = f"{component}.{release}{RELEASE_OUTPUT_SUFFIX}"
    output = _Output(
        path=project / RecordStore.RELEASES.output_directory / name, text=tail
    )
    reference = {
        "path": output.path.relative_to(project).as_posix(),
        "sha256": hashlib.sha256(tail.encode("utf-8")).hexdigest(),
    }
    spec["releaseCommand"] = {
        key: value for key, value in command.items() if key != _OUTPUT_TAIL
    } | {"output": reference}
    return json.dumps(document, indent=4) + "\n", output


def _refuse_other_output(project: Path, step: _Planned) -> None:
    """Refuse to replace a different output file already kept under ``tmp/``."""
    output = step.output
    if output is None or not output.path.exists():
        return
    if output.path.is_file() and output.path.read_bytes() == output.text.encode():
        return
    receipt = step.source.relative_to(project).as_posix()
    message = (
        f"cannot keep the output tail of {receipt}: "
        f"{output.path.relative_to(project).as_posix()} already exists with "
        "different content"
    )
    raise UpgradeError(ERROR_UPGRADE_TARGET_EXISTS, message)


def _keep_output(output: _Output) -> None:
    """Write raw output readable by its owner only: it may hold a secret."""
    output.path.parent.mkdir(mode=_OUTPUT_DIRECTORY_MODE, parents=True, exist_ok=True)
    descriptor = os.open(
        output.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _OUTPUT_FILE_MODE
    )
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(output.text)
    output.path.chmod(_OUTPUT_FILE_MODE)


def _occupied(step: _Planned) -> bool:
    """Whether the target holds something the move would overwrite.

    A record whose target already holds the same bytes is not: it was
    copied there before, and the move only drops the copy under ``tmp/``.
    """
    if not step.target.exists():
        return False
    if not step.record or not step.target.is_file():
        return True
    return step.target.read_bytes() != step.content()


def _plan(project: Path) -> Iterator[_Planned]:
    legacy = project / LEGACY_BASELINE_DIRECTORY
    directories = [project / kind.directory for kind in ResourceKind]
    directories.append(legacy)
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _YAML_SUFFIXES:
                continue
            # newline="" keeps a file's own line endings through the rewrite.
            text = path.read_text(encoding="utf-8", newline="")
            rewritten, renamed = _rewritten(text)
            target = (
                project / ResourceKind.BASELINE.directory / path.relative_to(legacy)
                if directory == legacy
                else path
            )
            if target == path and not renamed:
                continue
            yield _Planned(
                source=path,
                target=target,
                text=rewritten if renamed else None,
                renamed=renamed,
            )


def _rewritten(text: str) -> tuple[str, tuple[str, ...]]:
    """Return ``text`` with its old names replaced, and what was replaced."""
    edits: list[tuple[int, int, str]] = []
    renamed: list[str] = []
    for document in yaml.compose_all(text, Loader=yaml.SafeLoader):
        if not isinstance(document, yaml.MappingNode):
            continue
        kind = _value(document, "kind")
        if not isinstance(kind, yaml.ScalarNode):
            continue
        if kind.value == LEGACY_BASELINE_KIND:
            edits.append(_replacement(kind, ResourceKind.BASELINE.value))
            renamed.append(
                f"kind: {LEGACY_BASELINE_KIND} -> kind: {ResourceKind.BASELINE.value}"
            )
            continue
        if kind.value != ResourceKind.SERVER.value:
            continue
        spec = _value(document, "spec")
        if not isinstance(spec, yaml.MappingNode):
            continue
        key = _key(spec, LEGACY_BASELINE_FIELD)
        if key is not None:
            edits.append(_replacement(key, BASELINE_FIELD))
            renamed.append(f"spec.{LEGACY_BASELINE_FIELD} -> spec.{BASELINE_FIELD}")
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, tuple(renamed)


def _entry(
    mapping: yaml.MappingNode, name: str
) -> tuple[yaml.ScalarNode, yaml.Node] | None:
    pairs = cast("list[tuple[yaml.Node, yaml.Node]]", mapping.value)
    for key, value in pairs:
        if isinstance(key, yaml.ScalarNode) and key.value == name:
            return key, value
    return None


def _key(mapping: yaml.MappingNode, name: str) -> yaml.ScalarNode | None:
    entry = _entry(mapping, name)
    return None if entry is None else entry[0]


def _value(mapping: yaml.MappingNode, name: str) -> yaml.Node | None:
    entry = _entry(mapping, name)
    return None if entry is None else entry[1]


def _replacement(node: yaml.ScalarNode, value: str) -> tuple[int, int, str]:
    """Replace one scalar in place, keeping its quoting."""
    quote = node.style if node.style in _QUOTED_STYLES else ""
    return node.start_mark.index, node.end_mark.index, f"{quote}{value}{quote}"


def _refuse_symlink(project: Path, path: Path) -> None:
    """Refuse a path that is, or passes through, a symlink inside the project.

    Writing or moving through a link could change a file outside the
    project, so the upgrade leaves linked files for a person to rewrite.
    """
    current = project
    for part in path.relative_to(project).parts:
        current /= part
        if current.is_symlink():
            message = (
                f"{current.relative_to(project).as_posix()} is a symlink; "
                "the upgrade does not follow links, rewrite its target by hand"
            )
            raise UpgradeError(ERROR_UPGRADE_SYMLINK, message)


def _remove_empty(directory: Path) -> None:
    """Remove the deprecated folder once the upgrade emptied it."""
    if directory.is_symlink() or not directory.is_dir():
        return
    for child in sorted(directory.rglob("*"), reverse=True):
        if child.is_dir() and not any(child.iterdir()):
            child.rmdir()
    if not any(directory.iterdir()):
        directory.rmdir()


UPGRADE_STEPS: Mapping[ProjectFormat, _Step] = {
    ProjectFormat(0): _Step(plan=_plan_from_format_0, emptied=_emptied_from_format_0),
}
"""Each rewrite, keyed by the format it starts from; it leads to the next one.

A release that raises the current format adds the step that leads to it, and
keeps every step back to format 0, so a project left alone for several
releases upgrades in one run.
"""
