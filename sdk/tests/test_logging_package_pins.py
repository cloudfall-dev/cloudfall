"""S-011 package provider: a pin the host's APT sources lack stops the run early."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
ROLES = ROOT / "engine" / "ansible" / "roles"
FAKE_APT_CACHE = """#!/bin/sh
case "$2" in
  loki)
    echo '      loki |      3.7.7 | https://apt.grafana.com stable/main amd64 Packages'
    echo '      loki |      3.7.2 | https://apt.grafana.com stable/main amd64 Packages'
    ;;
esac
"""


def _run(
    tmp_path: Path, version: str, *, check_mode: bool = False
) -> subprocess.CompletedProcess[str]:
    tools = tmp_path / "tools"
    tools.mkdir()
    apt_cache = tools / "apt-cache"
    apt_cache.write_text(FAKE_APT_CACHE, encoding="utf-8")
    apt_cache.chmod(0o755)
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "cloudfall_logging_common_pins": [
                            {"name": "loki", "version": version}
                        ]
                    },
                    "tasks": [
                        {
                            "ansible.builtin.include_role": {
                                "name": "cloudfall_logging_common",
                                "tasks_from": "pins",
                            }
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    return subprocess.run(  # noqa: S603 - the venv's ansible-playbook
        [
            str(ANSIBLE_PLAYBOOK),
            "--inventory",
            "localhost,",
            "--connection",
            "local",
            "--skip-tags",
            "packages",
            *(["--check"] if check_mode else []),
            str(playbook),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        env={
            **os.environ,
            "ANSIBLE_ROLES_PATH": str(ROLES),
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        },
    )


def test_an_offered_pin_passes(tmp_path: Path) -> None:
    done = _run(tmp_path, "3.7.2")

    assert done.returncode == 0, done.stdout


def test_a_missing_pin_stops_the_run_naming_what_is_offered(
    tmp_path: Path,
) -> None:
    done = _run(tmp_path, "9.9.9")

    assert done.returncode != 0
    assert (
        "loki=9.9.9 is not in this host's APT sources; they offer: 3.7.7, 3.7.2"
        in done.stdout
    )


@pytest.mark.parametrize(("version", "passes"), [("3.7.2", True), ("9.9.9", False)])
def test_a_dry_run_checks_the_pins_too(
    tmp_path: Path, version: str, *, passes: bool
) -> None:
    done = _run(tmp_path, version, check_mode=True)

    assert (done.returncode == 0) is passes, done.stdout
    if not passes:
        assert (
            "loki=9.9.9 is not in this host's APT sources; they offer: 3.7.7, 3.7.2"
            in done.stdout
        )
