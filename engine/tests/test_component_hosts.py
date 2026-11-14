"""Playbooks contact only their servers and stop at one they could not reach.

Each playbook runs for real under ``ansible-playbook`` against two declared
servers: ``h2`` runs locally, and ``h1`` points at a closed local SSH port,
so contacting it makes it unreachable. ``h1`` comes first in inventory order,
so a play over every server would fail on it before it reached ``h2``. The
roles are replaced, through ``--roles``, by one that records each host.
Deploy, rollback, restart, health and backup contact only their servers.
When a playbook changes servers one by one, an unreachable one must stop it
before the next server, which ``any_errors_fatal`` alone does not do.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cloudfall_engine.cli import main
from examples_project import copy_examples

if TYPE_CHECKING:
    from collections.abc import Mapping

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"
RELEASE = "20261027T000000Z-abcdef1"
STUB_TASKS = """---
- name: Contact the host, as the replaced role would
  ansible.builtin.ping:

- name: Record the host this entry point ran on
  ansible.builtin.lineinfile:
    path: "{{ stub_log }}"
    line: "{{ cloudfall_server_id }}"
    create: true
    mode: "0644"
  delegate_to: localhost

- name: See whether the component's lock is held while the role runs
  ansible.builtin.stat:
    path: "{{ cloudfall_component_lock_directory }}/crm-backend.json"
  register: stub_lock
  when: stub_lock_log is defined

- name: Record whether it was
  ansible.builtin.lineinfile:
    path: "{{ stub_lock_log }}"
    line: "{{ cloudfall_server_id }}:{{ stub_lock.stat.exists }}"
    create: true
    mode: "0644"
  delegate_to: localhost
  when: stub_lock_log is defined

- name: Fail as a broken role would
  ansible.builtin.fail:
    msg: the stub role failed
  when: stub_fail | default(false) | bool
"""
EXTRA_VARS: dict[str, dict[str, str]] = {
    "deploy": {
        "cloudfall_deploy_component_id": "crm-backend",
        "cloudfall_deploy_release": RELEASE,
        "cloudfall_deploy_artifact_archive": "/dev/null",
        "cloudfall_deploy_artifact_sha256": "a" * 64,
    },
    "rollback": {
        "cloudfall_deploy_component_id": "crm-backend",
        "cloudfall_deploy_release": RELEASE,
    },
    "restart": {"cloudfall_deploy_component_id": "crm-backend"},
    "health": {"cloudfall_deploy_component_id": "crm-backend"},
}
COMPONENT_PLAYBOOKS = list(EXTRA_VARS)
ROLLOUT_PLAYBOOKS = ["deploy", "rollback", "restart"]
# The other playbooks that change servers one by one (serial: 1), and the
# servers their stubbed roles record when both servers are reached. The
# logging backend is on h2, so the collectors' play is the one rolling over
# h1 and h2.
ROLLING_PLAYBOOKS: dict[str, tuple[dict[str, str], list[str]]] = {
    "baseline": ({}, ["h1", "h2"]),
    "bootstrap": ({}, ["h1", "h2"]),
    "data": (
        {
            "cloudfall_data_service_id": "postgresql-main",
            "cloudfall_data_database": "crm",
            "cloudfall_data_source_url_file": "/dev/null",
        },
        ["h2"],
    ),
    "domains": ({}, ["h2"]),
    "logging": ({}, ["h2", "h1"]),
    "services": ({}, ["h1", "h2"]),
    "time": ({}, ["h1", "h2"]),
}
# Every role a rolling playbook includes, replaced by the recording stub.
STUBBED_ROLES = (
    "cloudfall_access",
    "cloudfall_bootstrap",
    "cloudfall_data_migration",
    "cloudfall_firewall",
    "cloudfall_inspect",
    "cloudfall_logging_backend",
    "cloudfall_logging_collector",
    "cloudfall_nginx_site",
    "cloudfall_postgresql",
    "cloudfall_redis",
    "cloudfall_time",
    "cloudfall_unattended_upgrades",
    "cloudfall_volumes",
)


@dataclass(frozen=True)
class Fleet:
    """Where crm-backend runs, and which declared servers cannot be reached."""

    servers: tuple[str, ...] = ("h2",)
    unreachable: tuple[str, ...] = ("h1",)
    release: bool = False


DEFAULT_FLEET = Fleet()


def _project(tmp_path: Path, fleet: Fleet) -> Path:
    """Copy the examples with crm-backend on its servers, postgresql-main on h2."""
    project = tmp_path / "project"
    copy_examples(project)
    component = project / "components" / "crm-backend.yaml"
    document = yaml.safe_load(component.read_text(encoding="utf-8"))
    document["spec"]["deployment"]["servers"] = list(fleet.servers)
    if fleet.release:
        document["spec"]["release"] = {"command": ["bin/migrate"]}
    component.write_text(yaml.safe_dump(document), encoding="utf-8")
    service = project / "services" / "postgresql-main.yaml"
    document = yaml.safe_load(service.read_text(encoding="utf-8"))
    document["spec"]["server"] = "h2"
    service.write_text(yaml.safe_dump(document), encoding="utf-8")
    logging = project / "logging-stacks" / "operations.yaml"
    document = yaml.safe_load(logging.read_text(encoding="utf-8"))
    document["spec"]["backend"]["server"] = "h2"
    for file_source in document["spec"]["collectors"]["files"]:
        file_source["servers"] = ["h2"]
    logging.write_text(yaml.safe_dump(document), encoding="utf-8")
    return project


def _inventory(tmp_path: Path, fleet: Fleet) -> Path:
    state = validate_config(_project(tmp_path, fleet), SCHEMAS)
    rendered = render_ansible_inventory(PlatformInventory.from_state(state))
    hosts = rendered["all"]["children"]["cloudfall_servers"]["hosts"]  # type: ignore[index]
    assert list(hosts) == ["h1", "h2"]
    for host, variables in hosts.items():
        # Both stand-ins share this machine, so each keeps its locks apart.
        variables["cloudfall_component_lock_directory"] = str(_locks(tmp_path, host))
        if host in fleet.unreachable:
            variables.update(
                {
                    "ansible_connection": "ssh",
                    "ansible_host": "127.0.0.1",
                    "ansible_port": 1,
                    "ansible_ssh_common_args": "-o ConnectTimeout=5",
                }
            )
        else:
            variables.update(
                {
                    "ansible_connection": "local",
                    "ansible_python_interpreter": sys.executable,
                }
            )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(rendered), encoding="utf-8")
    return inventory


def _locks(tmp_path: Path, server: str) -> Path:
    """Where the stand-in server keeps its component locks."""
    return tmp_path / f"{server}-locks"


def _roles(tmp_path: Path) -> Path:
    roles = tmp_path / "roles"
    tasks = roles / "cloudfall_deploy" / "tasks"
    tasks.mkdir(parents=True)
    for name in ("main.yml", "rollback.yml", "restart.yml", "health.yml"):
        (tasks / name).write_text(STUB_TASKS, encoding="utf-8")
    for role in STUBBED_ROLES:
        role_tasks = roles / role / "tasks"
        role_tasks.mkdir(parents=True)
        (role_tasks / "main.yml").write_text(STUB_TASKS, encoding="utf-8")
    # services.yml re-renders the exporters through this entry point (#329)
    (roles / "cloudfall_logging_collector" / "tasks" / "exporters.yml").write_text(
        STUB_TASKS, encoding="utf-8"
    )
    return roles


def _run(
    tmp_path: Path,
    playbook: str,
    extra_vars: Mapping[str, object],
    fleet: Fleet = DEFAULT_FLEET,
) -> int:
    inventory = _inventory(tmp_path, fleet)
    return main(
        [
            *("playbook", "run", playbook),
            *("--engine", str(ENGINE)),
            *("--inventory", str(inventory)),
            *("--roles", str(_roles(tmp_path))),
            "--extra-vars",
            json.dumps({**extra_vars, "ansible_become": False}),
        ]
    )


@pytest.mark.parametrize("playbook", COMPONENT_PLAYBOOKS)
def test_an_unreachable_server_without_the_component_does_not_fail_it(
    playbook: str, tmp_path: Path
) -> None:
    log = tmp_path / "hosts.log"

    exit_code = _run(
        tmp_path, playbook, {**EXTRA_VARS[playbook], "stub_log": str(log)}
    )

    assert exit_code == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["h2"]


@pytest.mark.parametrize("playbook", ROLLOUT_PLAYBOOKS)
def test_an_unreachable_server_of_the_component_stops_the_rollout(
    playbook: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """h1 comes first and is unreachable, so h2 must never be touched."""
    log = tmp_path / "hosts.log"

    exit_code = _run(
        tmp_path,
        playbook,
        {**EXTRA_VARS[playbook], "stub_log": str(log)},
        Fleet(servers=("h1", "h2")),
    )

    output = capsys.readouterr().err
    assert exit_code == 82
    assert "UNREACHABLE!" in output
    assert "Stopped before h2: the run did not finish on h1" in output
    assert not log.exists()


@pytest.mark.parametrize("playbook", ROLLOUT_PLAYBOOKS)
def test_every_reached_server_of_the_component_rolls_out_in_order(
    playbook: str, tmp_path: Path
) -> None:
    log = tmp_path / "hosts.log"

    exit_code = _run(
        tmp_path,
        playbook,
        {**EXTRA_VARS[playbook], "stub_log": str(log)},
        Fleet(servers=("h1", "h2"), unreachable=()),
    )

    assert exit_code == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["h1", "h2"]


@pytest.mark.parametrize("playbook", ROLLING_PLAYBOOKS)
def test_an_unreachable_server_stops_every_rolling_playbook(
    playbook: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """h1 comes first and is unreachable, so h2 must never be changed (#254)."""
    log = tmp_path / "hosts.log"
    extra_vars, _ = ROLLING_PLAYBOOKS[playbook]

    exit_code = _run(tmp_path, playbook, {**extra_vars, "stub_log": str(log)})

    output = capsys.readouterr().err
    assert exit_code == 82
    assert "UNREACHABLE!" in output
    assert "Stopped before h2: the run did not finish on h1" in output
    # The logging backend (h2) comes before the collectors' play, which is
    # the one that stops.
    expected = ["h2"] if playbook == "logging" else []
    assert _logged_hosts(log) == expected


@pytest.mark.parametrize("playbook", ROLLING_PLAYBOOKS)
def test_every_reached_server_runs_each_rolling_playbook(
    playbook: str, tmp_path: Path
) -> None:
    """The check never stops a run whose earlier servers all finished."""
    log = tmp_path / "hosts.log"
    extra_vars, expected = ROLLING_PLAYBOOKS[playbook]

    exit_code = _run(
        tmp_path,
        playbook,
        {**extra_vars, "stub_log": str(log)},
        Fleet(unreachable=()),
    )

    assert exit_code == 0
    assert _logged_hosts(log) == expected


def test_inspect_goes_on_past_an_unreachable_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inspect only reads, so it observes what it can reach (#254)."""
    log = tmp_path / "hosts.log"

    exit_code = _run(tmp_path, "inspect", {"stub_log": str(log)})

    output = capsys.readouterr().err
    assert exit_code == 82
    assert "UNREACHABLE!" in output
    assert "Stopped before" not in output
    assert _logged_hosts(log) == ["h2"]


def _logged_hosts(log: Path) -> list[str]:
    """Each host the stubs recorded, once, in the order it was first seen."""
    if not log.exists():
        return []
    return list(dict.fromkeys(log.read_text(encoding="utf-8").splitlines()))


@pytest.mark.parametrize(("release_server", "other"), [("h1", "h2"), ("h2", "h1")])
def test_an_unreachable_release_server_stops_the_rollout(
    release_server: str,
    other: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The release server deploys in its own play; missing it stops the rest."""
    log = tmp_path / "hosts.log"

    exit_code = _run(
        tmp_path,
        "deploy",
        {**EXTRA_VARS["deploy"], "stub_log": str(log)},
        Fleet(
            servers=(release_server, other),
            unreachable=(release_server,),
            release=True,
        ),
    )

    output = capsys.readouterr().err
    stopped = f"Stopped before {other}: the run did not finish on {release_server}"
    assert exit_code == 82
    assert stopped in output
    assert not log.exists()


def test_a_release_server_deploys_once_without_the_unrelated_server(
    tmp_path: Path,
) -> None:
    """h2 is both the release server and the component's only server."""
    log = tmp_path / "hosts.log"

    exit_code = _run(
        tmp_path,
        "deploy",
        {**EXTRA_VARS["deploy"], "stub_log": str(log)},
        Fleet(release=True),
    )

    assert exit_code == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["h2"]


@pytest.mark.parametrize("playbook", COMPONENT_PLAYBOOKS)
def test_a_component_without_a_group_fails_before_any_host(
    playbook: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "hosts.log"
    extra_vars = {
        **EXTRA_VARS[playbook],
        "cloudfall_deploy_component_id": "billing-backend",
        "stub_log": str(log),
    }

    exit_code = _run(tmp_path, playbook, extra_vars)

    assert exit_code == 82
    assert "component_billing_backend" in capsys.readouterr().err
    assert not log.exists()


@pytest.mark.parametrize("playbook", COMPONENT_PLAYBOOKS)
def test_a_run_without_a_component_id_fails_before_any_host(
    playbook: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "hosts.log"
    extra_vars = {**EXTRA_VARS[playbook], "stub_log": str(log)}
    del extra_vars["cloudfall_deploy_component_id"]

    exit_code = _run(tmp_path, playbook, extra_vars)

    assert exit_code == 82
    assert "cloudfall_deploy_component_id" in capsys.readouterr().err
    assert not log.exists()


def test_backup_contacts_only_the_services_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """h2 has no backup script, so the run fails there, never on h1."""
    exit_code = _run(
        tmp_path,
        "backup",
        {
            "cloudfall_backup_service": "postgresql-main",
            "cloudfall_backup_action": "backup",
            "cloudfall_backup_receipt_directory": str(tmp_path / "receipts"),
        },
    )

    output = capsys.readouterr().err
    assert exit_code == 82
    assert "UNREACHABLE!" not in output
    assert "/usr/local/sbin/cloudfall-postgresql-backup" in output


def test_backup_of_a_service_on_no_server_fails_before_any_host(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = _run(
        tmp_path,
        "backup",
        {
            "cloudfall_backup_service": "absent-service",
            "cloudfall_backup_action": "backup",
            "cloudfall_backup_receipt_directory": str(tmp_path / "receipts"),
        },
    )

    output = capsys.readouterr().err
    assert exit_code == 82
    assert "absent-service" in output
    assert "UNREACHABLE!" not in output


# The component's lock on each server (#221). These runs go through
# cloudfall-engine, as an engine run or an approval's run would, so they hold
# the lock without the CLI's lock on the management host.

HELD_TOKEN = "f" * 32


def _leave_server_lock(tmp_path: Path, server: str, *, hours_ago: int) -> Path:
    """Write the record another run's deploy holds on ``server``."""
    since = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=hours_ago)
    record = {
        "component": "crm-backend",
        "action": "deploy",
        "user": "someone",
        "host": "elsewhere.invalid",
        "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "token": HELD_TOKEN,
    }
    lock = _locks(tmp_path, server) / "crm-backend.json"
    lock.parent.mkdir(parents=True)
    lock.write_text(json.dumps(record), encoding="utf-8")
    return lock


def _held_locks(tmp_path: Path) -> list[Path]:
    """Every component lock, or staged record, left on either stand-in."""
    return sorted(
        path
        for server in ("h1", "h2")
        if _locks(tmp_path, server).is_dir()
        for path in _locks(tmp_path, server).iterdir()
    )


@pytest.mark.parametrize("playbook", ROLLOUT_PLAYBOOKS)
def test_a_lock_another_run_holds_stops_the_run_on_that_server(
    playbook: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The race: before the lock both runs moved current and the unit at once."""
    log = tmp_path / "hosts.log"
    lock = _leave_server_lock(tmp_path, "h2", hours_ago=2)

    exit_code = _run(
        tmp_path, playbook, {**EXTRA_VARS[playbook], "stub_log": str(log)}
    )

    output = " ".join(capsys.readouterr().err.split())
    assert exit_code == 82
    assert (
        "Component crm-backend is locked on server h2 by a deploy that someone "
        "started on elsewhere.invalid at"
    ) in output
    assert "2 hours ago; nothing changed on this server" in output
    assert "`cloudfall lock clear crm-backend`" in output
    assert not log.exists(), "the role ran under another run's lock"
    assert json.loads(lock.read_text(encoding="utf-8"))["token"] == HELD_TOKEN
    assert _held_locks(tmp_path) == [lock]


def test_a_lock_on_the_release_server_stops_the_deploy_before_its_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The release server deploys in its own play, and takes the lock there."""
    log = tmp_path / "hosts.log"
    _leave_server_lock(tmp_path, "h2", hours_ago=0)

    exit_code = _run(
        tmp_path,
        "deploy",
        {**EXTRA_VARS["deploy"], "stub_log": str(log)},
        Fleet(release=True),
    )

    output = " ".join(capsys.readouterr().err.split())
    assert exit_code == 82
    assert "Component crm-backend is locked on server h2 by a deploy" in output
    assert "seconds ago" in output
    assert not log.exists()


@pytest.mark.parametrize("playbook", ROLLOUT_PLAYBOOKS)
@pytest.mark.parametrize("fails", [False, True], ids=["passed", "failed"])
def test_each_server_holds_the_lock_while_its_role_runs_and_releases_it(
    playbook: str, fails: bool, tmp_path: Path  # noqa: FBT001 - a pytest parameter
) -> None:
    log = tmp_path / "hosts.log"
    lock_log = tmp_path / "locks.log"

    exit_code = _run(
        tmp_path,
        playbook,
        {
            **EXTRA_VARS[playbook],
            "stub_log": str(log),
            "stub_lock_log": str(lock_log),
            "stub_fail": fails,
        },
        Fleet(servers=("h1", "h2"), unreachable=()),
    )

    assert exit_code == (82 if fails else 0)
    held = lock_log.read_text(encoding="utf-8").splitlines()
    assert held == (["h1:True"] if fails else ["h1:True", "h2:True"])
    assert _held_locks(tmp_path) == []


def test_an_unreachable_server_leaves_no_other_server_locked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "hosts.log"

    exit_code = _run(
        tmp_path,
        "restart",
        {**EXTRA_VARS["restart"], "stub_log": str(log)},
        Fleet(servers=("h1", "h2"), unreachable=("h2",)),
    )

    assert exit_code == 82
    assert "UNREACHABLE!" in capsys.readouterr().err
    assert log.read_text(encoding="utf-8").splitlines() == ["h1"]
    assert _held_locks(tmp_path) == []
