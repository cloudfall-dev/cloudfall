"""The ``cloudfall`` console script: every command runs on ``cloudfall.app``."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from treaty import FrameworkCode

from cloudfall.app import (
    INVESTIGATION_STOPPED,
    app,
    stream_stopped,
    stream_unrecorded,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

OUTPUT_CLOSED = (0, 141)
"""What treaty exits with when the reader closed stdout: after an event, before any."""


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command in-process and return its exit code."""
    _forget_streams()
    return _stopped(app.run(list(sys.argv[1:] if argv is None else argv)))


def run() -> None:
    """Installed console-script entry point."""
    _forget_streams()
    try:
        app.main()
    except SystemExit as exiting:
        # app.main() exits with treaty's code; only a stopped stream's changes.
        code = exiting.code
        if isinstance(code, int):
            stopped = _stopped(code)
            if stopped != code:
                raise SystemExit(stopped) from None
        raise


def _forget_streams() -> None:
    stream_stopped.clear()
    stream_unrecorded.clear()


def _stopped(code: int) -> int:
    """Return the exit code of an investigation whose reader left it mid-run.

    INVESTIGATION_STOPPED when its record was kept; PRECONDITION, with the
    reason on stderr, when the record could not be written.
    """
    if code not in OUTPUT_CLOSED:
        return code
    if stream_unrecorded:
        error = stream_unrecorded[-1]
        line = {
            "code": FrameworkCode.PRECONDITION.name,
            "message": error.detail,
            "context": {"code": error.code},
        }
        sys.stderr.write(f"{json.dumps(line)}\n")
        return int(FrameworkCode.PRECONDITION)
    if stream_stopped.is_set():
        return INVESTIGATION_STOPPED
    return code
