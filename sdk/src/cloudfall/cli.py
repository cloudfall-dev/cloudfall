"""The ``cloudfall`` console script: every command runs on ``cloudfall.app``."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from cloudfall.app import app

if TYPE_CHECKING:
    from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command in-process and return its exit code."""
    return app.run(list(sys.argv[1:] if argv is None else argv))


def run() -> None:
    """Installed console-script entry point."""
    app.main()
