"""#295: the logging roles stop when a service user can't reach a file it reads."""

from __future__ import annotations

import grp
import importlib.util
import json
import os
import pwd
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    Blocking = Callable[
        [str, str, str, Sequence[Mapping[str, object]]], dict[str, str] | None
    ]

ROOT = Path(__file__).parents[2]
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
ROLES = ROOT / "engine" / "ansible" / "roles"
INCLUDE = "Check the service users reach their files before anything is installed"
PREFLIGHT = {"name": "cloudfall_logging_common", "tasks_from": "preflight"}
PASSWORD = "/etc/cloudfall/logging/grafana-admin-password"  # noqa: S105 - a path


PLUGIN = ROLES / "cloudfall_logging_common" / "filter_plugins" / "reach.py"


def _blocking_parent() -> Blocking:
    spec = importlib.util.spec_from_file_location("reach", PLUGIN)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    blocking: Blocking = module.blocking_parent
    return blocking


def _directory(
    path: str, mode: str, owner: str = "root", group: str = "root"
) -> dict[str, object]:
    return {
        "item": path,
        "stat": {
            "exists": True,
            "isdir": True,
            "mode": mode,
            "pw_name": owner,
            "gr_name": group,
            "uid": 0,
            "gid": 0,
        },
    }


def _chain(cloudfall: dict[str, object]) -> list[dict[str, object]]:
    return [
        _directory("/", "0755"),
        _directory("/etc", "0755"),
        cloudfall,
        _directory("/etc/cloudfall/logging", "0755"),
    ]


def test_a_0700_parent_blocks_the_service_user() -> None:
    stats = _chain(_directory("/etc/cloudfall", "0700"))

    blocking = _blocking_parent()(PASSWORD, "grafana", "grafana", stats)

    assert blocking == {
        "path": "/etc/cloudfall",
        "mode": "0700",
        "owner": "root",
        "group": "root",
    }


@pytest.mark.parametrize(
    ("group", "blocked"), [("grafana", False), ("prometheus", True)]
)
def test_a_0750_parent_lets_only_the_service_group_through(
    group: str,
    blocked: bool,  # noqa: FBT001 - a parametrized expectation
) -> None:
    stats = _chain(_directory("/etc/cloudfall", "0750", group="grafana"))

    blocking = _blocking_parent()(PASSWORD, group, group, stats)

    assert (blocking is not None) is blocked


def test_the_owner_of_a_0700_parent_goes_through() -> None:
    stats = _chain(_directory("/etc/cloudfall", "0700", owner="grafana"))

    assert _blocking_parent()(PASSWORD, "grafana", "grafana", stats) is None


def test_an_all_0755_chain_lets_every_user_through() -> None:
    stats = _chain(_directory("/etc/cloudfall", "0755"))

    assert _blocking_parent()(PASSWORD, "alloy", "alloy", stats) is None


def test_a_parent_not_there_yet_is_skipped() -> None:
    missing: dict[str, object] = {
        "item": "/etc/cloudfall/logging",
        "stat": {"exists": False},
    }
    stats = [*_chain(_directory("/etc/cloudfall", "0755"))[:3], missing]

    assert _blocking_parent()(PASSWORD, "grafana", "grafana", stats) is None


def test_a_parent_that_is_a_file_blocks() -> None:
    cloudfall = _directory("/etc/cloudfall", "0755")
    stat = cloudfall["stat"]
    assert isinstance(stat, dict)
    stat["isdir"] = False
    stats = _chain(cloudfall)

    blocking = _blocking_parent()(PASSWORD, "grafana", "grafana", stats)

    assert blocking is not None
    assert blocking["path"] == "/etc/cloudfall"


def _tasks(role: str) -> list[dict[str, object]]:
    path = ROLES / f"cloudfall_logging_{role}" / "tasks" / "main.yml"
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(tasks, list)
    return tasks


def _play(
    tmp_path: Path,
    task: Mapping[str, object],
    variables: Mapping[str, object],
    *,
    check_mode: bool = False,
) -> subprocess.CompletedProcess[str]:
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": dict(variables),
                    "tasks": [task],
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
        # The shared role is found, and loads its own filters, as in a deploy
        env={**os.environ, "ANSIBLE_ROLES_PATH": str(ROLES)},
    )


def _include(role: str) -> dict[str, object]:
    """Return the role's own include of the shared preflight, with its readers."""
    include = next(task for task in _tasks(role) if task["name"] == INCLUDE)
    assert include["ansible.builtin.include_role"] == PREFLIGHT
    return include


@pytest.mark.parametrize("check_mode", [False, True])
def test_the_preflight_names_the_directory_that_blocks(
    tmp_path: Path, *, check_mode: bool
) -> None:
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    reachable = tmp_path / "reachable"
    reachable.mkdir(mode=0o700)
    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o700)
    blocked.chmod(0o600)
    readers = [
        {
            "user": user,
            "group": group,
            "paths": [
                str(reachable / "file"),
                str(tmp_path / "not-yet" / "file"),
                str(blocked / "file"),
            ],
        }
    ]
    task = {
        "ansible.builtin.include_role": PREFLIGHT,
        "vars": {"cloudfall_logging_common_readers": readers},
    }

    try:
        done = _play(tmp_path, task, {}, check_mode=check_mode)
    finally:
        blocked.chmod(0o700)

    assert done.returncode != 0
    assert (
        f"{blocked} is 0600 {user}:{group}; {user} can't reach {blocked}/file"
        in done.stdout
    )
    assert f"can't reach {reachable}" not in done.stdout
    assert f"can't reach {tmp_path}/not-yet" not in done.stdout


def test_reachable_files_pass(tmp_path: Path) -> None:
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    (tmp_path / "reachable").mkdir(mode=0o700)
    task = {
        "ansible.builtin.include_role": PREFLIGHT,
        "vars": {
            "cloudfall_logging_common_readers": [
                {
                    "user": user,
                    "group": group,
                    "paths": [str(tmp_path / "reachable" / "file")],
                }
            ]
        },
    }

    done = _play(tmp_path, task, {})

    assert done.returncode == 0, done.stdout


def test_the_backend_checks_grafana_and_alertmanager_files(tmp_path: Path) -> None:
    # pytest's base temp directory is 0700 and owned by the test user, so
    # neither grafana nor prometheus can reach anything below it
    password = tmp_path / "grafana-admin-password"
    smtp = tmp_path / "smtp-password"
    token = tmp_path / "bot-token"
    stack = {
        "grafana": {"adminPasswordPath": str(password)},
        "alerting": {
            "receivers": [
                {"id": "hook", "webhook": {"url": "https://hooks.example.internal"}},
                {"id": "mail", "email": {"authPasswordFile": str(smtp)}},
            ]
        },
    }
    channels = [{"receiver": "chat", "botTokenFile": str(token)}]

    done = _play(
        tmp_path,
        _include("backend"),
        {
            "cloudfall_logging_backend_stack": stack,
            "cloudfall_logging_alert_channels": channels,
        },
    )

    assert done.returncode != 0
    readers = (("grafana", password), ("prometheus", smtp), ("prometheus", token))
    for user, path in readers:
        assert f"; {user} can't reach {path}" in done.stdout


def test_the_collector_checks_alloy_tls_files(tmp_path: Path) -> None:
    paths = {
        "caPath": str(tmp_path / "ca.crt"),
        "certificatePath": str(tmp_path / "client.crt"),
        "keyPath": str(tmp_path / "client.key"),
    }

    done = _play(
        tmp_path,
        _include("collector"),
        {"cloudfall_logging_collector_stack": {"collectors": {"clientTls": paths}}},
    )

    assert done.returncode != 0
    for path in paths.values():
        assert f"; alloy can't reach {path}" in done.stdout


@pytest.mark.parametrize("role", ["backend", "collector"])
def test_the_preflight_runs_before_anything_is_installed(role: str) -> None:
    tasks = _tasks(role)
    names = [task["name"] for task in tasks]
    installs = [
        index for index, task in enumerate(tasks) if "ansible.builtin.apt" in task
    ]

    assert names.index(INCLUDE) < min(installs)
