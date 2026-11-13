"""One run at a time per component on the management host (#221).

A deploy, rollback or restart of a component, and a build of its release,
holds the component's lock under ``tmp/locks/`` from before its first step
until it ends, passed, failed or interrupted. A second run started meanwhile
fails before it renders the inventory or contacts any server, naming who
holds the lock and since when. Only ``cloudfall lock clear``, once a person
types the component id at a terminal, removes a lock a run left behind.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from cloudfall.agent_server import PROJECT_COMMANDS
from cloudfall.app import app
from cloudfall.cli import main
from cloudfall.domain import ReleaseId, ResourceId
from cloudfall.lifecycle import (
    DeployOptions,
    EngineContext,
    ExecutionStep,
    LifecycleError,
    StepRun,
    build_release_artifact,
    deploy,
    restart,
    rollback,
)
from examples_project import copy_examples
from treaty import CommandPath

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ENGINE = ROOT / "engine"
RELEASE = "20260101T000000Z-abcdef0"
COMPONENT = ResourceId("crm-backend")
LOCKED = "component_locked"
RESTART_STEPS = ["render Ansible inventory", "run restart.yml"]


@dataclass(frozen=True, slots=True)
class _Done:
    returncode: int = 0
    stdout: str = '{"ok": true, "data": {"release": "' + RELEASE + '"}}'
    stderr: str = ""


@dataclass
class _Fleet:
    """A project whose steps are recorded and answered instead of run."""

    project: Path
    artifacts: Path
    steps: list[str] = field(default_factory=list)
    returncode: int = 0
    interrupt: bool = False
    during_playbook: Callable[[], None] | None = None

    def run(self, step: ExecutionStep) -> StepRun:
        if self.interrupt:
            raise KeyboardInterrupt
        self.steps.append(step.description)
        if step.description.startswith("run ") and self.during_playbook is not None:
            during, self.during_playbook = self.during_playbook, None
            during()
        if self.returncode != 0:
            return _Done(returncode=self.returncode, stdout="", stderr="boom")
        return _Done()

    @property
    def context(self) -> EngineContext:
        return EngineContext(
            project_directory=self.project,
            schema_directory=SCHEMAS,
            engine_directory=ENGINE,
            inventory_file=self.project / "tmp" / "ansible-inventory.json",
            run=self.run,
        )

    def lock(self, component: str = COMPONENT.value) -> Path:
        return self.project / "tmp" / "locks" / f"{component}.json"

    def leave_lock(
        self,
        *,
        pid: int,
        host: str,
        hours_ago: int = 0,
        component: str = COMPONENT.value,
    ) -> None:
        """Write the record a run of ``deploy`` would have left."""
        since = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=hours_ago)
        record = {
            "component": component,
            "action": "deploy",
            "user": "someone",
            "host": host,
            "pid": pid,
            "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "token": "f" * 32,
        }
        self.lock(component).parent.mkdir(parents=True, exist_ok=True)
        self.lock(component).write_text(json.dumps(record), encoding="utf-8")


def _write_artifact(artifacts: Path) -> None:
    directory = artifacts / COMPONENT.value
    directory.mkdir(parents=True)
    source = artifacts.parent / "source"
    source.mkdir()
    (source / "app.py").write_text("print('crm')\n", encoding="utf-8")
    archive = directory / f"{RELEASE}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname=".")
    metadata = {
        "apiVersion": "cloudfall/v1",
        "kind": "Artifact",
        "metadata": {"id": COMPONENT.value},
        "spec": {
            "component": COMPONENT.value,
            "release": RELEASE,
            "gitRef": "main",
            "gitCommit": "a" * 40,
            "builtAt": "2026-01-01T00:00:00Z",
            "archive": archive.name,
            "archiveSha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "sizeBytes": archive.stat().st_size,
        },
    }
    (directory / f"{RELEASE}.json").write_text(json.dumps(metadata), encoding="utf-8")


def _dead_pid() -> int:
    """Return the pid of a process that has exited."""
    process = subprocess.Popen(["true"])  # noqa: S607 - a fixed argv.
    process.wait()
    return process.pid


@pytest.fixture
def fleet(tmp_path: Path) -> _Fleet:
    project = tmp_path / "project"
    copy_examples(project)
    artifacts = project / "tmp" / "artifacts"
    _write_artifact(artifacts)
    return _Fleet(project=project, artifacts=artifacts)


def _deploy(fleet: _Fleet) -> None:
    release = ReleaseId(RELEASE)
    deploy(fleet.context, COMPONENT, release, fleet.artifacts, DeployOptions())


def _rollback(fleet: _Fleet) -> None:
    rollback(fleet.context, COMPONENT, ReleaseId(RELEASE))


def _restart(fleet: _Fleet) -> None:
    restart(fleet.context, COMPONENT)


def _build(fleet: _Fleet) -> None:
    build_release_artifact(fleet.context, COMPONENT, "main", fleet.artifacts)


RUNS: dict[str, Callable[[_Fleet], None]] = {
    "deploy": _deploy,
    "rollback": _rollback,
    "restart": _restart,
    "build": _build,
}
SERVER_RUNS = ("deploy", "rollback", "restart")


@pytest.mark.parametrize("first", SERVER_RUNS)
@pytest.mark.parametrize("second", list(RUNS))
def test_a_second_run_of_the_component_fails_before_any_step(
    first: str, second: str, fleet: _Fleet
) -> None:
    """The race: before the lock both runs reached the engine at once."""
    refused: list[LifecycleError] = []

    def start_the_second_run() -> None:
        before = len(fleet.steps)
        with pytest.raises(LifecycleError) as raised:
            RUNS[second](fleet)
        refused.append(raised.value)
        assert len(fleet.steps) == before, "the second run started a step"

    fleet.during_playbook = start_the_second_run

    RUNS[first](fleet)

    (error,) = refused
    assert error.code == LOCKED
    assert f"component {COMPONENT} is locked by a {first} " in error.detail
    assert "that process is still running" in error.detail
    assert "cloudfall lock clear crm-backend" in error.detail
    holder = error.context["holder"]
    assert isinstance(holder, dict)
    assert holder["action"] == first
    assert holder["pid"] == os.getpid()
    assert error.context["stale"] is False
    assert not fleet.lock().exists()


@pytest.mark.parametrize("action", list(RUNS))
def test_the_lock_is_released_when_a_run_fails(action: str, fleet: _Fleet) -> None:
    fleet.returncode = 2

    with pytest.raises(LifecycleError) as raised:
        RUNS[action](fleet)

    assert raised.value.code == "lifecycle_execution_failed"
    assert not fleet.lock().exists()


@pytest.mark.parametrize("action", list(RUNS))
def test_the_lock_is_released_on_ctrl_c(action: str, fleet: _Fleet) -> None:
    fleet.interrupt = True

    with pytest.raises(KeyboardInterrupt):
        RUNS[action](fleet)

    assert not fleet.lock().exists()


def test_a_lock_a_killed_run_left_is_reported_stale_with_its_age(
    fleet: _Fleet,
) -> None:
    fleet.leave_lock(pid=_dead_pid(), host=socket.gethostname(), hours_ago=3)

    with pytest.raises(LifecycleError) as raised:
        _restart(fleet)

    error = raised.value
    assert error.code == LOCKED
    assert "3 hours ago" in error.detail
    assert "no longer running, so the lock is stale" in error.detail
    assert error.context["stale"] is True
    age = error.context["ageSeconds"]
    assert isinstance(age, int)
    assert age >= 3 * 3600
    assert fleet.steps == []
    assert fleet.lock().exists(), "nothing clears a lock by age"


def test_a_lock_taken_on_another_host_is_not_judged_from_here(
    fleet: _Fleet,
) -> None:
    fleet.leave_lock(pid=4242, host="elsewhere.invalid")

    with pytest.raises(LifecycleError) as raised:
        _deploy(fleet)

    assert "is not known from here" in raised.value.detail
    assert raised.value.context["stale"] is None
    assert fleet.steps == []


def test_a_malformed_lock_record_fails_without_a_step(fleet: _Fleet) -> None:
    fleet.lock().parent.mkdir(parents=True)
    fleet.lock().write_text("{}", encoding="utf-8")

    with pytest.raises(LifecycleError) as raised:
        _rollback(fleet)

    assert raised.value.code == "component_lock_record_invalid"
    assert fleet.steps == []
    assert fleet.lock().read_text(encoding="utf-8") == "{}"


def test_another_components_lock_does_not_hold_this_one_up(fleet: _Fleet) -> None:
    fleet.leave_lock(pid=os.getpid(), host=socket.gethostname(), component="crm-worker")

    _restart(fleet)

    assert fleet.steps == RESTART_STEPS
    assert fleet.lock("crm-worker").exists()


def test_cli_a_locked_restart_answers_conflict(
    fleet: _Fleet, capsys: pytest.CaptureFixture[str]
) -> None:
    fleet.leave_lock(pid=os.getpid(), host=socket.gethostname())

    exit_code = main(
        [
            *("restart", "crm-backend", "--yes"),
            *("--project", str(fleet.project), "--schemas", str(SCHEMAS)),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert payload["error"]["code"] == "CONFLICT"
    assert payload["error"]["context"]["code"] == LOCKED
    assert payload["error"]["context"]["holder"]["action"] == "deploy"


# lock clear


class _Terminal(io.StringIO):
    """A stream that says it is a terminal, as a person's shell is."""

    def isatty(self) -> bool:
        return True


def _clear(fleet: _Fleet, typed: str | None) -> tuple[int, dict[str, Any]]:
    """Run ``lock clear`` at a terminal typing ``typed``; off one for ``None``."""
    argv = [
        *("lock", "clear", "crm-backend"),
        *("--project", str(fleet.project), "--schemas", str(SCHEMAS)),
        *("--format", "json"),
    ]
    at_terminal = typed is not None
    stdout = _Terminal() if at_terminal else io.StringIO()
    stdin = _Terminal(f"{typed}\n") if at_terminal else io.StringIO()
    exit_code = app.run(argv, stdin=stdin, stdout=stdout, stderr=io.StringIO())
    return exit_code, json.loads(stdout.getvalue())


def test_lock_clear_removes_a_stale_lock_once_a_person_types_the_id(
    fleet: _Fleet,
) -> None:
    fleet.leave_lock(pid=_dead_pid(), host=socket.gethostname(), hours_ago=2)

    exit_code, payload = _clear(fleet, "crm-backend")

    assert exit_code == 0, payload
    data = payload["data"]
    assert data["cleared"] is True
    assert data["effect"] == "deleted"
    assert data["lock"]["holder"]["action"] == "deploy"
    assert data["lock"]["stale"] is True
    assert not fleet.lock().exists()
    _restart(fleet)
    assert fleet.steps == RESTART_STEPS


def test_lock_clear_removes_a_lock_from_another_host(fleet: _Fleet) -> None:
    fleet.leave_lock(pid=4242, host="elsewhere.invalid", hours_ago=1)

    exit_code, payload = _clear(fleet, "crm-backend")

    assert exit_code == 0, payload
    assert not fleet.lock().exists()


@pytest.mark.parametrize("typed", [None, "yes"], ids=["off-terminal", "mistyped"])
def test_lock_clear_keeps_the_lock_without_the_typed_id(
    typed: str | None, fleet: _Fleet
) -> None:
    fleet.leave_lock(pid=_dead_pid(), host=socket.gethostname(), hours_ago=2)

    exit_code, payload = _clear(fleet, typed)

    assert exit_code != 0
    assert payload["error"]["code"] in {"PERSON_REQUIRED", "ATTESTATION_MISMATCH"}
    assert fleet.lock().exists()


def test_lock_clear_refuses_a_lock_whose_process_runs_here(fleet: _Fleet) -> None:
    fleet.leave_lock(pid=os.getpid(), host=socket.gethostname())

    exit_code, payload = _clear(fleet, "crm-backend")

    assert exit_code != 0
    assert payload["error"]["code"] == "CONFLICT"
    assert payload["error"]["context"]["code"] == "component_lock_in_use"
    assert fleet.lock().exists()


def test_lock_clear_without_a_lock_changes_nothing(fleet: _Fleet) -> None:
    exit_code, payload = _clear(fleet, None)

    assert exit_code == 0, payload
    assert payload["data"]["cleared"] is False
    assert payload["data"]["effect"] == "noop"
    assert payload["data"]["lock"] == {}


def test_lock_clear_is_a_persons_and_never_a_tool() -> None:
    command = app.commands[CommandPath("lock.clear")]

    assert command.requires_person is True
    assert command.mcp is False
    assert "lock.clear" not in PROJECT_COMMANDS
