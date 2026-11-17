"""``cloudfall doctor`` names an ansible-core that cannot start on the project's Python.

ansible-core 2.21 fails at start on Python 3.15 (#514); the dependency asks for
2.22 there, and doctor's ``ansible-start`` check names the failure with its fix.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from cloudfall.ansible_start import (
    ANSIBLE_START_CHECK,
    ANSIBLE_START_FIX,
    ansible_start_argv,
    ansible_start_check,
)
from cloudfall.cli import main
from packaging.requirements import Requirement
from packaging.version import Version
from treaty import Completed

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest

ROOT = Path(__file__).parents[2]
INTERPRETER = Path("/project/.venv/bin/python")
FAILED_START = """Traceback (most recent call last):
  File "/project/.venv/lib/python3.15/site-packages/ansible/cli/playbook.py", line 12
    from ansible.cli import CLI
AttributeError: module 'dataclasses' has no attribute '_is_type'
"""
STARTED = """ansible-playbook [core 2.22.0]
  config file = None
  python version = 3.15.2
"""


class Runner:
    """Answer every start with one fixed result, and keep what it was asked to run."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        """Keep the result every start answers."""
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.ran: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> Completed:
        """Record ``argv`` and answer the fixed result."""
        self.ran.append(tuple(argv))
        return Completed(tuple(argv), self.returncode, self.stdout, self.stderr, 1)


def test_a_failed_start_is_reported_with_its_fix() -> None:
    runner = Runner(1, "", FAILED_START)

    check = ansible_start_check(INTERPRETER, "3.15.2", runner)

    assert check.name == ANSIBLE_START_CHECK
    assert not check.ok
    assert check.fix == ANSIBLE_START_FIX
    assert check.error is not None
    assert "Python 3.15.2" in check.error
    assert "AttributeError: module 'dataclasses' has no attribute '_is_type'" in (
        check.error
    )
    assert 'requires-python = ">=3.14,<3.15"' in check.error


def test_a_start_that_prints_no_version_fails() -> None:
    check = ansible_start_check(INTERPRETER, "3.15.2", Runner(0, "", ""))

    assert not check.ok
    assert check.fix == ANSIBLE_START_FIX


def test_a_start_is_ok_with_the_core_version_and_no_fix() -> None:
    check = ansible_start_check(INTERPRETER, "3.15.2", Runner(0, STARTED, ""))

    assert check.ok
    assert check.version == "2.22.0"
    assert check.fix is None
    assert check.error is None


def test_the_check_starts_ansible_on_the_interpreter_and_runs_no_playbook() -> None:
    runner = Runner(0, STARTED, "")

    ansible_start_check(INTERPRETER, "3.15.2", runner)

    assert runner.ran == [ansible_start_argv(INTERPRETER)]
    assert runner.ran[0] == (
        str(INTERPRETER),
        "-m",
        "ansible.cli.playbook",
        "--version",
    )


def test_doctor_lists_the_check_and_ansible_starts_here(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["doctor"])

    document = json.loads(capsys.readouterr().out)
    checks = {check["name"]: check for check in document["data"]["checks"]}
    assert checks[ANSIBLE_START_CHECK]["ok"] is True
    assert "fix" not in checks[ANSIBLE_START_CHECK]
    assert not [
        warning
        for warning in document.get("warnings", [])
        if ANSIBLE_START_CHECK in json.dumps(warning)
    ]


def test_the_dependency_asks_for_ansible_core_2_22_on_python_3_15() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(text) for text in pyproject["project"]["dependencies"]]
    ansible = [r for r in requirements if r.name == "ansible-core"]

    def admitted(python: str, core: str) -> bool:
        environment = {"python_version": python, "python_full_version": f"{python}.0"}
        return any(
            (r.marker is None or r.marker.evaluate(environment))
            and Version(core) in r.specifier
            for r in ansible
        )

    assert not admitted("3.15", "2.21.0")
    assert admitted("3.15", "2.22.0")
    assert admitted("3.14", "2.21.0")
    assert admitted("3.14", "2.22.0")
    assert not admitted("3.14", "2.23.0")
    assert not admitted("3.15", "2.23.0")
