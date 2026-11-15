"""The format check every command that reads a project runs first (S-017).

``cloudfall``, ``cloudfall mcp serve`` and ``cloudfall-engine`` call it
once the project directory is resolved, before the project is validated,
before anything is written under its ``tmp/`` and before any Ansible run,
so each refuses an unsupported format with the same exit and context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from treaty import Exit

from cloudfall.project_format import (
    PROJECT_FORMAT_UNSUPPORTED_EXIT,
    SUPPORTED_FORMATS,
    UPGRADE_FIX,
    WARNING_PROJECT_FORMAT_OLD,
    FormatRange,
    ProjectFormat,
    ProjectFormatError,
    check_project_format,
    old_format_message,
)

if TYPE_CHECKING:
    from pathlib import Path

    from treaty import App, CliExit, Ctx

FORMAT_EXIT_NAME = "PROJECT_FORMAT_UNSUPPORTED"
"""The exit a command stops with on a format this release does not read."""


def declare_format_exit(app: App) -> None:
    """Register ``PROJECT_FORMAT_UNSUPPORTED`` on one of Cloudfall's CLIs."""
    app.exit_code(
        FORMAT_EXIT_NAME,
        PROJECT_FORMAT_UNSUPPORTED_EXIT,
        description=(
            "The project's [tool.cloudfall] format is outside the formats this "
            "Cloudfall reads, or malformed"
        ),
        retryable=False,
        side_effects="none",
        suggestion=(
            "read error.context.code: project_format_too_new, raise the cloudfall== "
            "pin and run uv sync; project_format_too_old, run `cloudfall config "
            "upgrade`; project_format_invalid, fix [tool.cloudfall] format"
        ),
    )


def project_format_unsupported(error: ProjectFormatError) -> CliExit:
    """Return the ``PROJECT_FORMAT_UNSUPPORTED`` exit for a refused format."""
    return Exit.PROJECT_FORMAT_UNSUPPORTED(error.detail, context=error.context)


def refuse_unsupported_format(
    directory: Path, supported: FormatRange = SUPPORTED_FORMATS
) -> ProjectFormat:
    """Return the project's format, or stop on one ``supported`` does not read.

    Too new, too old and malformed formats all stop the command.
    """
    try:
        return check_project_format(directory, supported)
    except ProjectFormatError as error:
        raise project_format_unsupported(error) from error


def check_format(
    ctx: Ctx, directory: Path, supported: FormatRange = SUPPORTED_FORMATS
) -> None:
    """Stop on a project format ``supported`` does not read; warn on an old one.

    The range is a parameter so a caller, or a test, can check a project
    against another range than the one this release reads.
    """
    project_format = refuse_unsupported_format(directory, supported)
    if project_format < supported.current:
        ctx.warn(
            WARNING_PROJECT_FORMAT_OLD,
            old_format_message(project_format, supported),
            projectFormat=project_format.value,
            current=supported.current.value,
            fix=UPGRADE_FIX,
        )
