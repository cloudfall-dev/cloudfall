"""#294: the logging roles hold every pinned package at its declared version."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

ROOT = Path(__file__).parents[2]
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
ROLES = ROOT / "engine" / "ansible" / "roles"
INCLUDE = "Hold the pinned packages before they are installed"


def _preference(package: str, version: str) -> str:
    return f"Package: {package}\nPin: version {version}\nPin-Priority: 999\n"


@dataclass(frozen=True)
class Dpkg:
    """What the fake ``dpkg-query`` and ``dpkg`` on the test's PATH answer.

    ``installed`` maps a package to what ``dpkg-query`` prints for it (status
    and version); one not in it is unknown to dpkg. ``newer`` lists the
    (installed, declared) pairs ``dpkg --compare-versions ... gt ...`` holds
    for; on a host, dpkg itself compares with Debian's version rules.
    """

    installed: Mapping[str, str] = field(default_factory=dict)
    newer: Collection[tuple[str, str]] = ()


def _tools(tmp_path: Path, dpkg: Dpkg) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir()
    shown = "".join(
        f"  {shlex.quote(package)}) printf '%s' {shlex.quote(line)} ;;\n"
        for package, line in dpkg.installed.items()
    )
    compared = "".join(
        f"  {shlex.quote(f'{have} {declared}')}) exit 0 ;;\n"
        for have, declared in dpkg.newer
    )
    scripts = {
        "dpkg-query": (
            '#!/bin/sh\ncase "$4" in\n'
            f"{shown}"
            '  *) echo "dpkg-query: no packages found matching $4" >&2; exit 1 ;;\n'
            "esac\n"
        ),
        "dpkg": (
            '#!/bin/sh\n[ "$1" = --compare-versions ] && [ "$3" = gt ] || exit 2\n'
            f'case "$2 $4" in\n{compared}esac\nexit 1\n'
        ),
    }
    for name, script in scripts.items():
        path = tools / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    return tools


def _tasks(role: str, name: str) -> list[dict[str, object]]:
    path = ROLES / f"cloudfall_logging_{role}" / "tasks" / name
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(tasks, list)
    return tasks


def _run(
    tmp_path: Path,
    role: str,
    stack: Mapping[str, object],
    *,
    check_mode: bool = False,
    dpkg: Dpkg | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the role's own holds include against ``tmp_path/preferences``.

    The files are root:root on a host; a test run is not root, so the copy
    here drops the owner and group and keeps everything else as written.
    Without ``dpkg``, none of the pinned packages is installed.
    """
    tools = _tools(tmp_path, dpkg or Dpkg())
    holds = _tasks(role, "holds.yml")
    for task in holds:
        copy = task.get("ansible.builtin.copy")
        if isinstance(copy, dict):
            assert (copy.pop("owner"), copy.pop("group")) == ("root", "root")
    holds_file = tmp_path / "holds.yml"
    holds_file.write_text(json.dumps(holds), encoding="utf-8")
    include = next(task for task in _tasks(role, "main.yml") if task["name"] == INCLUDE)
    include["ansible.builtin.include_tasks"] = str(holds_file)
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        f"cloudfall_logging_{role}_stack": stack,
                        f"cloudfall_logging_{role}_apt_preferences_dir": str(
                            tmp_path / "preferences"
                        ),
                    },
                    "tasks": [include],
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
            *(["--check"] if check_mode else []),
            str(playbook),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"},
    )


def _held(tmp_path: Path) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((tmp_path / "preferences").iterdir())
    }


def test_the_backend_holds_each_pinned_package_and_releases_an_unpinned_one(
    tmp_path: Path,
) -> None:
    preferences = tmp_path / "preferences"
    preferences.mkdir()
    # An earlier deploy pinned Grafana lower and Prometheus at all
    (preferences / "cloudfall-logging-grafana").write_text(
        _preference("grafana", "13.1.0"), encoding="utf-8"
    )
    (preferences / "cloudfall-logging-prometheus").write_text(
        _preference("prometheus", "2.53.3+ds1-1"), encoding="utf-8"
    )
    stack = {
        "software": {"lokiPackageVersion": "3.7.2", "grafanaPackageVersion": "13.2.6"},
        "alerting": {"alertmanager": {"packageVersion": "1:0.27.0-2"}},
    }

    done = _run(tmp_path, "backend", stack)

    assert done.returncode == 0, done.stdout
    assert _held(tmp_path) == {
        "cloudfall-logging-grafana": _preference("grafana", "13.2.6"),
        "cloudfall-logging-loki": _preference("loki", "3.7.2"),
        "cloudfall-logging-prometheus-alertmanager": _preference(
            "prometheus-alertmanager", "1:0.27.0-2"
        ),
    }
    assert all(
        path.stat().st_mode & 0o777 == 0o644 for path in preferences.iterdir()
    )


def test_the_backend_holds_a_pinned_prometheus_and_no_undeclared_alertmanager(
    tmp_path: Path,
) -> None:
    preferences = tmp_path / "preferences"
    preferences.mkdir()
    stack = {
        "software": {
            "lokiPackageVersion": "3.7.2",
            "grafanaPackageVersion": "13.2.6",
            "prometheusPackageVersion": "2.53.3+ds1-1",
        },
    }

    done = _run(tmp_path, "backend", stack)

    assert done.returncode == 0, done.stdout
    assert sorted(_held(tmp_path)) == [
        "cloudfall-logging-grafana",
        "cloudfall-logging-loki",
        "cloudfall-logging-prometheus",
    ]


def test_the_collector_holds_alloy(tmp_path: Path) -> None:
    (tmp_path / "preferences").mkdir()

    stack = {"software": {"alloyPackageVersion": "1.17.1-1"}}

    done = _run(tmp_path, "collector", stack)

    assert done.returncode == 0, done.stdout
    assert _held(tmp_path) == {
        "cloudfall-logging-alloy": _preference("alloy", "1.17.1-1")
    }


def test_a_dry_run_passes_and_changes_nothing(tmp_path: Path) -> None:
    preferences = tmp_path / "preferences"
    preferences.mkdir()
    stale = preferences / "cloudfall-logging-prometheus"
    stale.write_text(_preference("prometheus", "2.53.3+ds1-1"), encoding="utf-8")
    stack = {
        "software": {"lokiPackageVersion": "3.7.2", "grafanaPackageVersion": "13.2.6"},
    }

    done = _run(tmp_path, "backend", stack, check_mode=True)

    assert done.returncode == 0, done.stdout
    assert sorted(_held(tmp_path)) == ["cloudfall-logging-prometheus"]


DRIFTED = {
    "backend": (
        {
            "software": {
                "lokiPackageVersion": "3.7.2",
                "grafanaPackageVersion": "13.2.6",
            }
        },
        "grafana",
        "13.3.1",
        "13.2.6",
    ),
    "collector": (
        {"software": {"alloyPackageVersion": "1.17.1-1"}},
        "alloy",
        "1.18.0-1",
        "1.17.1-1",
    ),
}


@pytest.mark.parametrize("check_mode", [False, True], ids=["run", "dry-run"])
@pytest.mark.parametrize("role", ["backend", "collector"])
def test_a_package_installed_newer_than_declared_is_refused_and_not_held(
    tmp_path: Path, role: str, *, check_mode: bool
) -> None:
    preferences = tmp_path / "preferences"
    preferences.mkdir()
    stack, package, have, declared = DRIFTED[role]

    done = _run(
        tmp_path,
        role,
        stack,
        check_mode=check_mode,
        dpkg=Dpkg(installed={package: f"installed {have}"}, newer=[(have, declared)]),
    )

    assert done.returncode != 0
    assert (
        f"{package} is installed at {have}, newer than the declared {declared}: "
        f"declare {have}, or downgrade {package} by hand" in done.stdout
    )
    assert _held(tmp_path) == {}


def test_a_package_installed_at_or_below_its_declared_version_is_held(
    tmp_path: Path,
) -> None:
    (tmp_path / "preferences").mkdir()
    stack = {
        "software": {
            "lokiPackageVersion": "3.7.2",
            "grafanaPackageVersion": "13.2.6",
            "prometheusPackageVersion": "2.53.3+ds1-1",
        },
    }

    done = _run(
        tmp_path,
        "backend",
        stack,
        # Loki older, Grafana at the declared version, and a removed
        # Prometheus whose config files remain from a newer one
        dpkg=Dpkg(
            installed={
                "loki": "installed 3.7.0",
                "grafana": "installed 13.2.6",
                "prometheus": "config-files 3.0.0",
            },
            newer=[("3.0.0", "2.53.3+ds1-1")],
        ),
    )

    assert done.returncode == 0, done.stdout
    assert _held(tmp_path) == {
        "cloudfall-logging-grafana": _preference("grafana", "13.2.6"),
        "cloudfall-logging-loki": _preference("loki", "3.7.2"),
        "cloudfall-logging-prometheus": _preference("prometheus", "2.53.3+ds1-1"),
    }


@pytest.mark.parametrize("role", ["backend", "collector"])
def test_the_hold_is_written_after_the_pin_check_and_before_any_pinned_install(
    role: str,
) -> None:
    tasks = _tasks(role, "main.yml")
    names = [task["name"] for task in tasks]
    pinned_installs = [
        index
        for index, task in enumerate(tasks)
        if isinstance(apt := task.get("ansible.builtin.apt"), dict)
        and "=" in str(apt.get("name", ""))
    ]

    hold = names.index(INCLUDE)
    assert pinned_installs
    assert names.index("Check the package pins before anything is installed") < hold
    assert hold < min(pinned_installs)
