"""#294: the logging roles hold every pinned package at its declared version."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping

ROOT = Path(__file__).parents[2]
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
ROLES = ROOT / "engine" / "ansible" / "roles"
INCLUDE = "Hold the pinned packages before they are installed"


def _preference(package: str, version: str) -> str:
    return f"Package: {package}\nPin: version {version}\nPin-Priority: 1001\n"


def _tasks(role: str, name: str) -> list[dict[str, object]]:
    path = ROLES / f"cloudfall_logging_{role}" / "tasks" / name
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(tasks, list)
    return tasks


def _run(
    tmp_path: Path, role: str, stack: Mapping[str, object], *, check_mode: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run the role's own holds include against ``tmp_path/preferences``.

    The files are root:root on a host; a test run is not root, so the copy
    here drops the owner and group and keeps everything else as written.
    """
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
