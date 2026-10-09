"""The ``cloudfall`` console script: every command runs on ``cloudfall.app``."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from cloudfall.app import INVESTIGATION_STOPPED, app, stream_stopped

if TYPE_CHECKING:
    from collections.abc import Sequence

OUTPUT_CLOSED = (0, 141)
"""What treaty exits with when the reader closed stdout: after an event, before any."""


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command in-process and return its exit code."""
    stream_stopped.clear()
    return _stopped(app.run(list(sys.argv[1:] if argv is None else argv)))


def run() -> None:
    """Installed console-script entry point."""
    stream_stopped.clear()
    try:
        app.main()
    except SystemExit as exiting:
        # app.main() exits with treaty's code; only a stopped stream's changes.
        code = exiting.code
        if isinstance(code, int) and _stopped(code) != code:
            raise SystemExit(INVESTIGATION_STOPPED) from None
        raise


def _stopped(code: int) -> int:
    """Return INVESTIGATION_STOPPED when the reader left an investigation mid-run."""
    if stream_stopped.is_set() and code in OUTPUT_CLOSED:
        return INVESTIGATION_STOPPED
    return code
