"""``cloudfall doctor``'s tool checks read only what the tool itself reports (#524)."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING

import pytest
from cloudfall.app import app

if TYPE_CHECKING:
    from pathlib import Path

ANSIBLE_STARTED = """\
ansible-playbook [core 2.21.0]
  config file = None
  python version = 3.15.2 (main, Nov  4 2026, 10:00:00) [Clang 17.0.0]
  jinja version = 3.1.6
"""
ANSIBLE_FAILED_AT_START = """\
Traceback (most recent call last):
  File "/opt/venv/bin/ansible-playbook", line 5, in <module>
  File "/opt/venv/lib/python3.15/site-packages/ansible/cli/__init__.py", line 40
AttributeError: module 'dataclasses' has no attribute '_is_type'
"""


def _program(directory: Path, name: str, output: str, status: int) -> None:
    """Write a program to ``directory`` that prints ``output`` and exits ``status``.

    It uses only the shell's builtins, since PATH holds just ``directory``.
    """
    program = directory / name
    quoted = output.replace("'", "'\\''")
    program.write_text(
        f"#!/bin/sh\nprintf '%s' '{quoted}'\nexit {status}\n", encoding="utf-8"
    )
    program.chmod(0o755)


def _ansible_check(tmp_path: Path, output: str, status: int) -> dict[str, object]:
    """Run ``cloudfall doctor`` with a fake ``ansible-playbook``; return its check."""
    programs = tmp_path / "bin"
    programs.mkdir()
    _program(programs, "ansible-playbook", output, status)
    _program(programs, "git", "git version 2.47.0\n", 0)
    _program(programs, "sops", "sops 3.9.0\n", 0)
    stdout = io.StringIO()

    app.run(
        ["doctor", "--format", "json"],
        stdout=stdout,
        stderr=io.StringIO(),
        env={"PATH": str(programs), "HOME": str(tmp_path / "home")},
    )

    checks = json.loads(stdout.getvalue())["data"]["checks"]
    return next(c for c in checks if c["name"] == "ansible-playbook")


@pytest.mark.parametrize("status", [0, 1])
def test_a_traceback_naming_python_is_no_ansible_version(
    tmp_path: Path, status: int
) -> None:
    check = _ansible_check(tmp_path, ANSIBLE_FAILED_AT_START, status)

    assert check["ok"] is False
    assert check["version"] is None
    assert check["error"] == "no version in the output of ansible-playbook --version"
    assert check["fix"] == "install ansible-playbook 2.21.0 or newer"


def test_ansible_reports_its_core_version_not_python_s(tmp_path: Path) -> None:
    check = _ansible_check(tmp_path, ANSIBLE_STARTED, 0)

    assert check["ok"] is True
    assert check["version"] == "2.21.0"
    assert check["required"] == "2.21.0"


def test_an_older_ansible_fails_with_its_core_version(tmp_path: Path) -> None:
    check = _ansible_check(
        tmp_path, ANSIBLE_STARTED.replace("[core 2.21.0]", "[core 2.20.4]"), 0
    )

    assert check["ok"] is False
    assert check["version"] == "2.20.4"
