"""Rewrite a project's deprecated names to the current ones.

A ``Baseline`` was a ``ServerType`` before 0.8.0 (D-11): its files lived in
``server-types/`` and a ``Server`` named it as ``spec.serverType``. Config
validation still reads those names and warns about each file; this module
is the one-step fix the warning names. It moves the files to
``baselines/`` and rewrites only the scalars that carry an old name, found
through the YAML parser's positions, so every other byte of a file,
comments and formatting included, stays as it was. A second run finds
nothing to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import yaml

from cloudfall.domain import ResourceKind
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

_YAML_SUFFIXES = frozenset({".yaml", ".yml"})
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


def upgrade_project(project: Path) -> UpgradeResult:
    """Rewrite every deprecated name in the project's resource files.

    The whole plan is made before the first write, so a move that would
    overwrite an existing file, or a file or folder reached through a
    symlink, fails with nothing changed.
    """
    planned = tuple(_plan(project))
    for step in planned:
        _refuse_symlink(project, step.source)
        _refuse_symlink(project, step.target)
        if step.target != step.source and step.target.exists():
            message = (
                f"cannot move {step.source.relative_to(project).as_posix()}: "
                f"{step.target.relative_to(project).as_posix()} already exists"
            )
            raise UpgradeError(ERROR_UPGRADE_TARGET_EXISTS, message)
    for step in planned:
        if step.text is not None:
            step.source.write_text(step.text, encoding="utf-8", newline="")
        if step.target != step.source:
            step.target.parent.mkdir(parents=True, exist_ok=True)
            step.source.rename(step.target)
    _remove_empty(project / LEGACY_BASELINE_DIRECTORY)
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
