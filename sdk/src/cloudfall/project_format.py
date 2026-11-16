"""The format a project is written in, and the formats this release reads.

A project states its format as one integer in its own ``pyproject.toml``::

    [tool.cloudfall]
    format = 1

A project with no ``pyproject.toml``, or one without the table, was written
before formats existed and reads as format 0. ``SUPPORTED_FORMATS`` is the
one place that says which formats this release reads (spec S-017).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

PYPROJECT_FILE = "pyproject.toml"
"""The project file that holds the format, beside the ``cloudfall==`` pin."""

PROJECT_FORMAT_UNSUPPORTED_EXIT = 95
"""Exit code of a command refused because of the project's format."""

WARNING_PROJECT_FORMAT_OLD = "PROJECT_FORMAT_OLD"
"""Warning code of a project read in a format below the current one."""

UPGRADE_FIX = "cloudfall config upgrade"
"""The command that moves a project to the current format."""

PIN_FIX = "raise the cloudfall== pin in the project's pyproject.toml, then run uv sync"
"""What a project written by a newer Cloudfall needs."""


@dataclass(frozen=True, slots=True, order=True)
class ProjectFormat:
    """One project format: a non-negative integer."""

    value: int

    def __post_init__(self) -> None:
        """Refuse a value that is not a non-negative integer."""
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            message = (
                f"project format must be an integer, got {type(self.value).__name__}"
            )
            raise TypeError(message)
        if self.value < 0:
            message = f"project format must not be negative, got {self.value}"
            raise ValueError(message)

    def __str__(self) -> str:
        """Return the format as written in ``pyproject.toml``."""
        return str(self.value)


@dataclass(frozen=True, slots=True)
class FormatRange:
    """The formats a release reads, from ``oldest`` to ``current``."""

    oldest: ProjectFormat
    current: ProjectFormat

    def __post_init__(self) -> None:
        """Refuse a range whose oldest format is above its current one."""
        if self.oldest > self.current:
            message = (
                f"oldest format {self.oldest} is above current format {self.current}"
            )
            raise ValueError(message)


SUPPORTED_FORMATS = FormatRange(oldest=ProjectFormat(0), current=ProjectFormat(1))
"""The formats this release reads; ``current`` is the one ``init`` writes."""


class FormatErrorCode(StrEnum):
    """Why a project's format stops a command, as ``error.context.code``."""

    TOO_NEW = "project_format_too_new"
    TOO_OLD = "project_format_too_old"
    INVALID = "project_format_invalid"


class ProjectFormatError(RuntimeError):
    """A project format this release does not read, or cannot read at all."""

    def __init__(
        self, code: FormatErrorCode, detail: str, context: dict[str, object]
    ) -> None:
        """Keep the code, the message and the ``error.context`` keys."""
        self.code = code
        self.detail = detail
        self.context = {"code": code.value, **context}
        super().__init__(f"{code.value}: {detail}")


def read_project_format(directory: Path, supported: FormatRange) -> ProjectFormat:
    """Return the format the project in ``directory`` is written in.

    No ``pyproject.toml``, or no ``[tool.cloudfall]`` table in it, reads as
    format 0. A file that is not TOML, a ``tool`` or ``tool.cloudfall``
    that is not a table, and a ``format`` that is not a non-negative integer
    raise ``project_format_invalid``.
    """
    path = directory / PYPROJECT_FILE
    if not path.is_file():
        return ProjectFormat(0)
    range_context: dict[str, object] = {
        "projectFormat": None,
        "oldest": supported.oldest.value,
        "current": supported.current.value,
    }
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        detail = (
            f"{PYPROJECT_FILE} is not valid TOML, so its format cannot be read: "
            f"{error}"
        )
        raise _invalid(detail, range_context) from error
    tool = document.get("tool", {})
    if not isinstance(tool, dict):
        detail = f"`tool` in {PYPROJECT_FILE} must be a table"
        raise ProjectFormatError(FormatErrorCode.INVALID, detail, range_context)
    if "cloudfall" not in tool:
        return ProjectFormat(0)
    table = tool["cloudfall"]
    if not isinstance(table, dict):
        detail = f"`tool.cloudfall` in {PYPROJECT_FILE} must be a table"
        raise ProjectFormatError(FormatErrorCode.INVALID, detail, range_context)
    if "format" not in table:
        detail = (
            f"[tool.cloudfall] in {PYPROJECT_FILE} has no `format`; "
            f"write `format = {supported.current}`"
        )
        raise ProjectFormatError(FormatErrorCode.INVALID, detail, range_context)
    value = table["format"]
    try:
        return ProjectFormat(value)
    except (TypeError, ValueError) as error:
        detail = (
            f"`format` in [tool.cloudfall] of {PYPROJECT_FILE} must be a "
            f"non-negative integer, got {value!r}"
        )
        raise _invalid(detail, range_context) from error


def _invalid(detail: str, context: dict[str, object]) -> ProjectFormatError:
    return ProjectFormatError(FormatErrorCode.INVALID, detail, context)


def check_project_format(directory: Path, supported: FormatRange) -> ProjectFormat:
    """Return the project's format when ``supported`` reads it, else raise.

    A format above ``current`` raises ``project_format_too_new`` and one
    below ``oldest`` raises ``project_format_too_old``; the caller warns
    about a format inside the range but below ``current``.
    """
    project_format = read_project_format(directory, supported)
    context: dict[str, object] = {
        "projectFormat": project_format.value,
        "oldest": supported.oldest.value,
        "current": supported.current.value,
    }
    if project_format > supported.current:
        detail = (
            f"the project is written in format {project_format}, newer than the "
            f"formats this Cloudfall reads ({supported.oldest} to "
            f"{supported.current}); {PIN_FIX}"
        )
        raise ProjectFormatError(FormatErrorCode.TOO_NEW, detail, context)
    if project_format < supported.oldest:
        detail = (
            f"the project is written in format {project_format}, older than the "
            f"formats this Cloudfall reads ({supported.oldest} to "
            f"{supported.current}); run `{UPGRADE_FIX}`"
        )
        raise ProjectFormatError(
            FormatErrorCode.TOO_OLD, detail, {**context, "fix": UPGRADE_FIX}
        )
    return project_format


def old_format_message(project_format: ProjectFormat, supported: FormatRange) -> str:
    """Return the ``PROJECT_FORMAT_OLD`` warning text for a readable old format."""
    return (
        f"the project is written in format {project_format}, older than the "
        f"current format {supported.current}; `{UPGRADE_FIX}` moves it to "
        f"format {supported.current}"
    )
