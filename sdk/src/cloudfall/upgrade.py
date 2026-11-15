"""Rewrite a project's deprecated names to the current ones.

A ``Baseline`` was a ``ServerType`` before 0.8.0 (D-11): its files lived in
``server-types/`` and a ``Server`` named it as ``spec.serverType``. Config
validation still reads those names and warns about each file; this module
is the one-step fix the warning names. It moves the files to
``baselines/`` and rewrites only the scalars that carry an old name, found
through the YAML parser's positions, so every other byte of a file,
comments and formatting included, stays as it was. A second run finds
nothing to change.

Receipts and operator proposals lay under ``tmp/`` before 0.8.0, which git
ignores, so the record did not survive losing the management host. They
live in the committed ``record/`` now (D-16), and the upgrade moves each
one there byte for byte. A file already in the record with the same bytes
is a copy, so the one under ``tmp/`` is dropped; a different file there is
refused, since either could be the record to keep. The one change it makes
to a record is to an old release receipt: its release command's output
tail moves to a ``0600`` file under ``tmp/output/releases/``, and the
receipt keeps that file's path and SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import yaml

from cloudfall.domain import ResourceKind
from cloudfall.project import RELEASE_OUTPUT_SUFFIX, RecordStore
from cloudfall.validation import (
    BASELINE_FIELD,
    LEGACY_BASELINE_DIRECTORY,
    LEGACY_BASELINE_FIELD,
    LEGACY_BASELINE_KIND,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

ERROR_UPGRADE_TARGET_EXISTS = "upgrade_target_exists"
ERROR_UPGRADE_SYMLINK = "upgrade_symlink"
ERROR_UPGRADE_RECORD_INVALID = "upgrade_record_invalid"

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

    def as_dict(self) -> dict[str, object]:
        """Serialize for system boundaries."""
        return {
            "status": "ok",
            "project": str(self.project),
            "upgraded": [upgraded.as_dict() for upgraded in self.files],
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


def upgrade_project(project: Path) -> UpgradeResult:
    """Rewrite every deprecated name, and move the record out of ``tmp/``.

    The whole plan is made before the first write, so a move that would
    overwrite an existing file, or a file or folder reached through a
    symlink, fails with nothing changed.
    """
    planned = (*_plan(project), *_record_moves(project))
    for step in planned:
        _refuse_symlink(project, step.source)
        _refuse_symlink(project, step.target)
        if step.target != step.source and _occupied(step):
            message = (
                f"cannot move {step.source.relative_to(project).as_posix()}: "
                f"{step.target.relative_to(project).as_posix()} already exists"
                + (" with different content" if step.record else "")
            )
            raise UpgradeError(ERROR_UPGRADE_TARGET_EXISTS, message)
        if step.output is not None:
            _refuse_symlink(project, step.output.path)
            _refuse_other_output(project, step)
    for step in planned:
        if step.output is not None:
            _keep_output(step.output)
        if step.text is not None:
            step.source.write_text(step.text, encoding="utf-8", newline="")
        if step.target == step.source:
            continue
        if step.record and step.target.exists():
            # A copy of the same bytes is already in the record.
            step.source.unlink()
            continue
        step.target.parent.mkdir(parents=True, exist_ok=True)
        step.source.rename(step.target)
    _remove_empty(project / LEGACY_BASELINE_DIRECTORY)
    for store in RecordStore:
        _remove_empty(project / store.legacy_directory)
    return UpgradeResult(
        project=project,
        files=tuple(
            FileUpgrade(
                path=step.target.relative_to(project).as_posix(),
                moved_from=(
                    step.source.relative_to(project).as_posix()
                    if step.target != step.source
                    else None
                ),
                renamed=step.renamed,
            )
            for step in planned
        ),
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
