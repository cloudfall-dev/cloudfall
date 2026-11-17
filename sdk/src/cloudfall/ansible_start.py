"""``cloudfall doctor``'s check that ansible-core starts on Cloudfall's Python.

The engine runs every playbook through ansible-core installed beside Cloudfall,
so a lock holding an ansible-core release that cannot start on the project's
Python (2.21 on Python 3.15, #514) fails every playbook at start. This check
starts ``ansible-playbook --version`` with that interpreter, so the failure is
named before a run: it runs no playbook and touches no host.
"""

from __future__ import annotations

import platform
import re
import sys
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from treaty import Check, Completed, Ctx

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

ANSIBLE_START_CHECK = "ansible-start"
"""The check's name in ``doctor``'s ``data.checks``"""
ANSIBLE_START_FIX = "uv lock --upgrade-package ansible-core && uv sync"
"""Relock, so the project gets the ansible-core release its Python supports"""
_CORE_VERSION = re.compile(r"\[core (\d+(?:\.\d+)+)")
_PYTHON_CAP = 'requires-python = ">=3.14,<3.15"'


def ansible_start_argv(interpreter: Path) -> tuple[str, ...]:
    """Return the command that starts ``ansible-playbook`` on ``interpreter``."""
    return (str(interpreter), "-m", "ansible.cli.playbook", "--version")


def ansible_start_check(
    interpreter: Path,
    python_version: str,
    run: Callable[[Sequence[str]], Completed],
) -> Check:
    """Start ``ansible-playbook --version`` through ``run``; say if it started."""
    started = run(ansible_start_argv(interpreter))
    found = _CORE_VERSION.search(started.stdout)
    if started.returncode == 0 and found is not None:
        return Check(ANSIBLE_START_CHECK, ok=True, version=found.group(1))
    lines = [line.strip() for line in started.stderr.splitlines() if line.strip()]
    cause = lines[-1] if lines else f"exit {started.returncode} and no version"
    error = (
        f"ansible-playbook does not start on Python {python_version} "
        f"({interpreter}): {cause}. Relock so the project gets an ansible-core "
        "release that supports this Python, or cap the project's Python with "
        f"{_PYTHON_CAP} in its pyproject.toml, then run uv lock and uv sync"
    )
    return Check(ANSIBLE_START_CHECK, ok=False, fix=ANSIBLE_START_FIX, error=error)


def ansible_starts(ctx: Ctx) -> Check:
    """``doctor``: ansible-playbook starts on the Python running Cloudfall."""
    return ansible_start_check(
        Path(sys.executable),
        platform.python_version(),
        partial(_run, ctx),
    )


def _run(ctx: Ctx, argv: Sequence[str]) -> Completed:
    """Run ``argv`` and keep its exit code, so a failed start is a result."""
    return ctx.run(list(argv), check=False)
