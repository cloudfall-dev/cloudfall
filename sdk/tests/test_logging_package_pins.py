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


def _run(tmp_path: Path, role: str, version: str) -> subprocess.CompletedProcess[str]:
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
                        f"cloudfall_logging_{role}_pins": [
                            {"name": "loki", "version": version}
                        ]
                    },
                    "tasks": [
                        {
                            "ansible.builtin.include_tasks": str(
                                ROLES
                                / f"cloudfall_logging_{role}"
                                / "tasks"
                                / "pins.yml"
                            )
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
            str(playbook),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"},
    )


@pytest.mark.parametrize("role", ["backend", "collector"])
def test_an_offered_pin_passes(tmp_path: Path, role: str) -> None:
    done = _run(tmp_path, role, "3.7.2")

    assert done.returncode == 0, done.stdout


@pytest.mark.parametrize("role", ["backend", "collector"])
def test_a_missing_pin_stops_the_run_naming_what_is_offered(
    tmp_path: Path, role: str
) -> None:
    done = _run(tmp_path, role, "9.9.9")

    assert done.returncode != 0
    assert (
        "loki=9.9.9 is not in this host's APT sources; they offer: 3.7.7, 3.7.2"
        in done.stdout
    )
