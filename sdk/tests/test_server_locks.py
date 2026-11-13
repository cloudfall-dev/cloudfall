"""The component's lock on each of its servers, read and cleared (#221).

``lock.yml`` runs for real under ``ansible-playbook``, with both declared
servers as local connections that keep their locks in separate directories,
so a lock left on one server is read there and cleared only once shown.
``cloudfall lock clear`` runs the same two calls behind the person's typed
component id; its prompt and refusals are in ``test_component_lock.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from cloudfall.domain import ResourceId
from cloudfall.lifecycle import EngineContext, ExecutionStep, LifecycleError, StepRun
from cloudfall.server_locks import (
    ServerLock,
    ServerLocks,
    clear_server_locks,
    read_server_locks,
)
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ENGINE = ROOT / "engine"
COMPONENT = ResourceId("crm-backend")
TOKEN = "f" * 32


@dataclass
class _LocalRun:
    """Run each step for real, with every server a local connection."""

    inventory_file: Path
    locks: Path
    unreachable: tuple[str, ...] = ()
    playbooks: list[str] = field(default_factory=list)

    def __call__(self, step: ExecutionStep) -> StepRun:
        if step.description.startswith("run "):
            self._connect_locally()
            self.playbooks.append(step.description)
        return subprocess.run(  # noqa: S603 - argv built by the SDK.
            list(step.argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ, **step.environment},
        )

    def directory(self, server: str) -> Path:
        return self.locks / server

    def _connect_locally(self) -> None:
        rendered = json.loads(self.inventory_file.read_text(encoding="utf-8"))
        hosts = rendered["all"]["children"]["cloudfall_servers"]["hosts"]
        for name, host in hosts.items():
            host["cloudfall_component_lock_directory"] = str(self.directory(name))
            if name in self.unreachable:
                host.update(
                    {
                        "ansible_connection": "ssh",
                        "ansible_host": "127.0.0.1",
                        "ansible_port": 1,
                        "ansible_ssh_common_args": "-o ConnectTimeout=5",
                    }
                )
                continue
            host.update(
                {
                    "ansible_connection": "local",
                    "ansible_python_interpreter": sys.executable,
                    "ansible_become": False,
                }
            )
        self.inventory_file.write_text(json.dumps(rendered), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class _Fleet:
    context: EngineContext
    run: _LocalRun

    def leave_lock(self, server: str, *, hours_ago: int, token: str = TOKEN) -> Path:
        """Write the record another run's deploy holds on ``server``."""
        since = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=hours_ago)
        record = {
            "component": COMPONENT.value,
            "action": "deploy",
            "user": "someone",
            "host": "elsewhere.invalid",
            "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "token": token,
        }
        lock = self.run.directory(server) / f"{COMPONENT.value}.json"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps(record), encoding="utf-8")
        return lock


def _fleet(tmp_path: Path, unreachable: tuple[str, ...] = ()) -> _Fleet:
    project = tmp_path / "project"
    copy_examples(project)
    inventory = project / "tmp" / "ansible-inventory.json"
    run = _LocalRun(inventory, tmp_path / "locks", unreachable)
    context = EngineContext(
        project_directory=project,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=inventory,
        run=run,
    )
    return _Fleet(context=context, run=run)


def test_a_lock_left_on_a_server_is_read_with_its_holder_and_age(
    tmp_path: Path,
) -> None:
    fleet = _fleet(tmp_path)
    lock = fleet.leave_lock("h2", hours_ago=3)

    found = read_server_locks(fleet.context, COMPONENT)

    (held,) = found.held
    assert held.server == ResourceId("h2")
    assert held.holder.action == "deploy"
    assert held.holder.host == "elsewhere.invalid"
    assert held.age_seconds >= 3 * 3600
    assert found.unread == ()
    assert held.describe().startswith(
        "component crm-backend is locked on server h2 by a deploy that someone "
        "started on elsewhere.invalid at"
    )
    assert held.describe().endswith("3 hours ago")
    assert lock.exists(), "reading clears nothing"


def test_clearing_removes_exactly_the_locks_shown(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path)
    lock = fleet.leave_lock("h2", hours_ago=1)
    shown = read_server_locks(fleet.context, COMPONENT)

    clear_server_locks(fleet.context, COMPONENT, shown)

    assert not lock.exists()
    assert read_server_locks(fleet.context, COMPONENT) == ServerLocks(
        held=(), unread=()
    )


def test_a_lock_taken_again_since_it_was_shown_is_kept(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path)
    fleet.leave_lock("h2", hours_ago=1)
    shown = read_server_locks(fleet.context, COMPONENT)
    lock = fleet.leave_lock("h2", hours_ago=0, token="e" * 32)

    with pytest.raises(LifecycleError) as raised:
        clear_server_locks(fleet.context, COMPONENT, shown)

    assert raised.value.code == "component_lock_changed"
    assert "on server h2 was released or taken again" in raised.value.detail
    assert json.loads(lock.read_text(encoding="utf-8"))["token"] == "e" * 32


def test_the_lock_is_compared_and_removed_in_one_command_on_the_server() -> None:
    """No round trip sits between reading the token and removing the lock (#271).

    Read in one task and removed in another, a lock a run took in between
    went with the one shown. The token goes to the command as an argument,
    never into its script.
    """
    plays = yaml.safe_load((ENGINE / "ansible/playbooks/lock.yml").read_text())
    tasks = [task for play in plays for task in play.get("tasks", [])]
    removing = [
        task
        for task in tasks
        if "rm -f" in json.dumps(task)
        or task.get("ansible.builtin.file", {}).get("state") == "absent"
    ]

    (step,) = removing
    argv = step["ansible.builtin.command"]["argv"]
    assert argv[-1] == "{{ cloudfall_component_lock_clear_token }}"
    assert "clear_token" not in argv[2], "the script itself never holds the token"
    reads = [
        index
        for index, task in enumerate(tasks)
        if {"ansible.builtin.stat", "ansible.builtin.slurp"} & set(task)
    ]
    assert tasks.index(step) < min(reads), (
        "nothing reads the lock before the step that compares and removes it"
    )


@pytest.mark.parametrize(
    "token",
    ['$(touch "$HOME/pwned")', 'x" "y', "`touch pwned`; rm -rf ./locks"],
    ids=["substitution", "quote", "backtick"],
)
def test_a_token_is_never_run_as_shell(tmp_path: Path, token: str) -> None:
    fleet = _fleet(tmp_path)
    lock = fleet.leave_lock("h2", hours_ago=1)
    shown = read_server_locks(fleet.context, COMPONENT)
    (held,) = shown.held
    forged = ServerLocks(
        held=(
            ServerLock(
                server=held.server,
                holder=replace(held.holder, token=token),
                age_seconds=held.age_seconds,
            ),
        ),
        unread=(),
    )

    with pytest.raises(LifecycleError) as raised:
        clear_server_locks(fleet.context, COMPONENT, forged)

    assert raised.value.code == "component_lock_changed"
    assert lock.exists()
    assert not (tmp_path / "pwned").exists()
    assert list(tmp_path.rglob("pwned")) == []


def test_an_unreachable_server_is_reported_and_the_others_are_read(
    tmp_path: Path,
) -> None:
    fleet = _fleet(tmp_path, unreachable=("h1",))
    fleet.leave_lock("h2", hours_ago=1)

    found = read_server_locks(fleet.context, COMPONENT)

    assert [lock.server for lock in found.held] == [ResourceId("h2")]
    assert found.unread == (ResourceId("h1"),)
    assert "server h1 could not be reached" in found.describe()


def test_a_malformed_lock_on_a_server_is_refused(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path)
    lock = fleet.leave_lock("h2", hours_ago=1)
    lock.write_text("{}", encoding="utf-8")

    with pytest.raises(LifecycleError) as raised:
        read_server_locks(fleet.context, COMPONENT)

    assert raised.value.code == "component_lock_record_invalid"
    assert "on server h2 is malformed" in raised.value.detail


def test_a_component_the_config_no_longer_declares_reads_no_server(
    tmp_path: Path,
) -> None:
    fleet = _fleet(tmp_path)

    found = read_server_locks(fleet.context, ResourceId("retired-worker"))

    assert found == ServerLocks(held=(), unread=())
    assert fleet.run.playbooks == []
