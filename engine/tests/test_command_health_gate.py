"""A component's command health check gating a deploy (S-009-2..12).

The role needs a Debian host with systemd, so these tests take the routes
``test_release_command.py`` takes. ``command_health_check.yml`` and
``release_receipt.yml`` run for real under ``ansible-playbook`` on the
controller, with ``systemd-run`` swapped through the role's
``cloudfall_deploy_systemd_run`` variable for a stand-in that honours
``--working-directory``, runs the argv after ``--`` and, unlike the real one,
passes the command's output on: so a marker the command prints reaching the
output would show here. The gate block's order, its rescue and the deploy's
failure message are replayed with Ansible's own templating.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.domain import ResourceId
from cloudfall.lifecycle import EngineContext, ExecutionStep, StepRun, health
from cloudfall.validation import SchemaCatalog
from cloudfall_engine.cli import main
from cloudfall_engine.playbook import PlaybookRun, playbook_command
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ENGINE = ROOT / "engine"
ROLE = ENGINE / "ansible" / "roles" / "cloudfall_deploy"
ACTIVATE = "Activate the release behind the health gate"
SWITCH = "Switch the current release symlink"
RESTART = "Restart the component service"
HTTP_GATE = "Gate the release on its declared health check"
COMMAND_GATE = "Gate the release on its declared command health check"
UNIT_GATE = [
    "Gate the release on an active service unit",
    "Read the service unit restart count before settling",
    "Let the service unit settle",
    "Gate the release on a service unit still active after settling",
    "Read the service unit restart count after settling",
    "Gate the release on a service unit that did not restart while settling",
]
RUN = "Run the command health check"
RESTORE = "Restore the previous release symlink"
RESTART_RESTORED = "Restart the restored release"
STOP_FIRST = "Stop the failed first release"
FAILED_RECEIPT = "Receipt the release that failed its command health check"
FAIL = "Fail the deployment after rolling back"
PASSED_RECEIPT = "Receipt the release that passed its health gate"
PREVIOUS = "20261027T000000Z-abcdef1"
RELEASE = "20261028T000000Z-abcdef2"
MARKER = "cloudfall-smoke-secret-7f3a"
TIMEOUT_MARKER = "cloudfall-test-timeout"
COMPONENT_ID = "crm-backend"
HTTP_CHECK: dict[str, object] = {
    "type": "http",
    "scheme": "http",
    "port": 8100,
    "path": "/healthz",
    "expectedStatuses": [200],
    "timeoutSeconds": 5,
    "attempts": 10,
}
NO_CHECK: dict[str, object] = {"type": "none"}

FAKE_SYSTEMD_RUN = f"""#!/bin/sh
# Stand-in for systemd-run --wait: chdir like --working-directory, then run
# the argv after "--" and pass on its exit code and, unlike systemd-run
# without --pipe, its output.
printf '%s\\n' "$@" > "$0.argv"
directory=
while [ "$1" != "--" ]; do
  case "$1" in
    --working-directory=*) directory="${{1#--working-directory=}}" ;;
  esac
  shift
done
shift
cd "$directory" || exit 200
"$@"
status=$?
if [ -f "$directory/{TIMEOUT_MARKER}" ]; then
  echo "Finished with result: timeout" >&2
  exit 1
fi
exit $status
"""

SMOKE = f"""#!/bin/sh
# smoke <exit-code> <passing-attempt>: counts its runs in the working
# directory, prints a marker on both streams, then exits 0 from the passing
# attempt on (0: never) and with <exit-code> before it.
count=$(( $(cat "$PWD/attempts" 2>/dev/null || echo 0) + 1 ))
echo "$count" > "$PWD/attempts"
date +%s >> "$PWD/attempt-times"
echo "$PWD" > "$PWD/where"
echo "{MARKER} on stdout"
echo "{MARKER} on stderr" >&2
if [ "$2" -gt 0 ] && [ "$count" -ge "$2" ]; then
  exit 0
fi
exit "$1"
"""


@dataclass(frozen=True, slots=True)
class _Run:
    """One run of the check under ``ansible-playbook`` and what it left."""

    tmp_path: Path
    exit_code: int
    output: str

    @property
    def install_root(self) -> Path:
        return self.tmp_path / "app"

    @property
    def release_directory(self) -> Path:
        return self.install_root / "releases" / RELEASE

    @property
    def argv(self) -> list[str]:
        path = self.tmp_path / "systemd-run.argv"
        return path.read_text(encoding="utf-8").splitlines()

    @property
    def attempts(self) -> int:
        path = self.release_directory / "attempts"
        return int(path.read_text(encoding="utf-8"))

    @property
    def attempt_times(self) -> list[int]:
        path = self.release_directory / "attempt-times"
        text = path.read_text(encoding="utf-8")
        return [int(line) for line in text.splitlines()]

    @property
    def receipt_text(self) -> str:
        path = self.tmp_path / "receipts" / f"{COMPONENT_ID}.json"
        return path.read_text(encoding="utf-8")

    @property
    def receipt(self) -> dict[str, object]:
        receipt = cast("dict[str, object]", json.loads(self.receipt_text))
        _validate(receipt)
        return receipt

    @property
    def spec(self) -> dict[str, object]:
        spec = self.receipt["spec"]
        assert isinstance(spec, dict)
        return spec

    @property
    def health_check(self) -> dict[str, object]:
        health_check = self.spec["healthCheck"]
        assert isinstance(health_check, dict)
        return health_check


def _validate(receipt: dict[str, object]) -> None:
    SchemaCatalog(SCHEMAS).validate_named("release-receipt.schema.json", receipt)


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _local_inventory(tmp_path: Path) -> Path:
    inventory = tmp_path / "inventory.json"
    host = {"ansible_connection": "local", "ansible_python_interpreter": sys.executable}
    inventory.write_text(
        json.dumps({"all": {"hosts": {"localhost": host}}}), encoding="utf-8"
    )
    return inventory


def _component(
    health_check: dict[str, object], install_root: Path
) -> dict[str, object]:
    return {
        "id": COMPONENT_ID,
        "application_user": "crm",
        "install_root": str(install_root),
        "service": {"name": COMPONENT_ID, "command": [".venv/bin/app"]},
        "healthCheck": health_check,
    }


def _command_check(
    command: list[str], *, attempts: int = 1, timeout: int = 60
) -> dict[str, object]:
    return {
        "type": "command",
        "command": command,
        "timeoutSeconds": timeout,
        "attempts": attempts,
    }


def _check(  # noqa: PLR0913 - one knob per criterion.
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    exit_code: int = 0,
    passing_attempt: int = 0,
    attempts: int = 1,
    first_release: bool = False,
    timed_out: bool = False,
    absolute: bool = False,
    verbose: bool = False,
) -> _Run:
    """Run the role's command check, receipting it as the gate and rescue do.

    The block's rescue here stands in for the role's: it writes the failed
    receipt through the role's own ``release_receipt.yml`` and fails the play.
    """
    install_root = tmp_path / "app"
    release_directory = install_root / "releases" / RELEASE
    release_directory.mkdir(parents=True)
    if not first_release:
        (install_root / "releases" / PREVIOUS).mkdir()
    (install_root / "current").symlink_to(release_directory)
    if absolute:
        executable = tmp_path / "checks" / "smoke"
        _executable(executable, SMOKE)
        first = str(executable)
    else:
        _executable(release_directory / "bin" / "smoke", SMOKE)
        first = "bin/smoke"
    if timed_out:
        (release_directory / TIMEOUT_MARKER).write_text("", encoding="utf-8")
    systemd_run = tmp_path / "systemd-run"
    _executable(systemd_run, FAKE_SYSTEMD_RUN)
    health_check = _command_check(
        [first, str(exit_code), str(passing_attempt)], attempts=attempts
    )
    variables = {
        "cloudfall_server_id": "h1",
        "cloudfall_deploy_release": RELEASE,
        "cloudfall_deploy_artifact_sha256": "a" * 64,
        "cloudfall_deploy_previous_directory": (
            "" if first_release else str(install_root / "releases" / PREVIOUS)
        ),
        "cloudfall_deploy_receipt_directory": str(tmp_path / "receipts"),
        "cloudfall_deploy_systemd_run": str(systemd_run),
        "cloudfall_deploy_component": _component(health_check, install_root),
    }
    receipt = {
        "ansible.builtin.include_role": {
            "name": "cloudfall_deploy",
            "tasks_from": "release_receipt",
        },
    }
    playbook = tmp_path / "check.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Gate a release on its command health check",
                    "hosts": "localhost",
                    "gather_facts": True,
                    "vars": variables,
                    "tasks": [
                        {
                            "name": "Gate as the role does",
                            "block": [
                                {
                                    "name": "Run the role's command check tasks",
                                    "ansible.builtin.include_role": {
                                        "name": "cloudfall_deploy",
                                        "tasks_from": "command_health_check",
                                    },
                                }
                            ],
                            "rescue": [
                                {
                                    "name": "Receipt the failed check",
                                    **receipt,
                                    "vars": {"cloudfall_deploy_receipt_passed": False},
                                },
                                {
                                    "name": "Fail after receipting",
                                    "ansible.builtin.fail": {"msg": "gate failed"},
                                },
                            ],
                        },
                        {
                            "name": "Receipt the passed check",
                            **receipt,
                            "vars": {"cloudfall_deploy_receipt_passed": True},
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    inventory = _local_inventory(tmp_path)
    if verbose:
        # As verbose as ansible-playbook gets: every task result is printed
        # unless the task is no_log.
        command = playbook_command(PlaybookRun(playbook, inventory), ENGINE)
        done = subprocess.run(  # noqa: S603 - the engine's own argv
            [*command.argv[:1], "-vvvv", *command.argv[1:]],
            env={**os.environ, **command.environment},
            capture_output=True,
            text=True,
            check=False,
        )
        return _Run(tmp_path, done.returncode, f"{done.stdout}\n{done.stderr}")
    code = main(
        [
            "playbook",
            "run",
            str(playbook),
            "--engine",
            str(ENGINE),
            "--inventory",
            str(inventory),
        ]
    )
    captured = capsys.readouterr()
    return _Run(tmp_path, code, f"{captured.out}\n{captured.err}")


# The gate's place in the deploy and its transient unit.


def _main() -> list[dict[str, object]]:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "main.yml"))
    assert isinstance(tasks, list)
    return tasks


def _activation() -> dict[str, object]:
    return next(t for t in _main() if t["name"] == ACTIVATE)


def _section(key: str) -> list[dict[str, object]]:
    section = _activation()[key]
    assert isinstance(section, list)
    return section


def _task(name: str, tasks: list[dict[str, object]]) -> dict[str, object]:
    return next(t for t in tasks if t["name"] == name)


def _check_tasks() -> list[dict[str, object]]:
    path = ROLE / "tasks" / "command_health_check.yml"
    tasks = DataLoader().load_from_file(str(path))
    assert isinstance(tasks, list)
    return tasks


def _templar(variables: dict[str, object]) -> Templar:
    return Templar(loader=DataLoader(), variables=variables)


def _holds(expression: object, variables: dict[str, object]) -> bool:
    return bool(
        _templar(variables).evaluate_conditional(trust_as_template(str(expression)))
    )


def _conditions(task: dict[str, object]) -> list[object]:
    value = task.get("when", [])
    return value if isinstance(value, list) else [value]


def _runs(task: dict[str, object], variables: dict[str, object]) -> bool:
    return all(_holds(c, variables) for c in _conditions(task))


def test_s009_2_the_check_runs_after_the_restart_inside_the_rescued_block() -> None:
    names = [str(task["name"]) for task in _section("block")]
    gate = _task(COMMAND_GATE, _section("block"))

    assert names.index(SWITCH) < names.index(RESTART) < names.index(COMMAND_GATE)
    assert gate["ansible.builtin.include_tasks"] == "command_health_check.yml"
    assert RESTORE in [str(task["name"]) for task in _section("rescue")]
    variables: dict[str, object] = {
        "ansible_check_mode": False,
        "cloudfall_deploy_component": {"healthCheck": _command_check(["bin/smoke"])},
    }
    assert _runs(gate, variables)
    assert not _runs(gate, {**variables, "ansible_check_mode": True})


def test_s009_2_the_check_runs_as_the_user_in_current_with_the_unit_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _check(tmp_path, capsys)

    assert run.exit_code == 0, run.output
    argv = run.argv
    options = argv[: argv.index("--")]
    assert "--unit=cloudfall-health-crm-backend" in options
    assert {"--wait", "--collect", "--service-type=exec"} <= set(options)
    assert "--uid=crm" in options
    assert "--gid=crm" in options
    assert f"--working-directory={run.install_root}/current" in options
    assert "--property=RuntimeMaxSec=60" in options
    assert "--property=TimeoutStopSec=10" in options
    assert "--property=NoNewPrivileges=true" in options
    assert "--property=PrivateTmp=true" in options
    unit = _templar(
        {"cloudfall_deploy_component": _component(NO_CHECK, run.install_root)}
    ).template(
        trust_as_template(
            (ROLE / "templates" / "component.service.j2").read_text(encoding="utf-8")
        )
    )
    unit_line = next(
        line for line in str(unit).splitlines() if line.startswith("EnvironmentFile=")
    )
    assert unit_line.startswith("EnvironmentFile=-")
    assert f"--property={unit_line}" in options
    where = (run.release_directory / "where").read_text(encoding="utf-8").strip()
    assert where == f"{run.install_root}/current"


def test_s009_3_a_relative_first_element_runs_from_current(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _check(tmp_path, capsys)

    argv = run.argv
    assert argv[argv.index("--") + 1 :] == [
        f"{run.install_root}/current/bin/smoke",
        "0",
        "0",
    ]


def test_s009_3_an_absolute_first_element_runs_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _check(tmp_path, capsys, absolute=True)

    assert run.exit_code == 0, run.output
    argv = run.argv
    executable = str(tmp_path / "checks" / "smoke")
    assert argv[argv.index("--") + 1 :] == [executable, "0", "0"]


# Pass, failure and retries.


def test_s009_4_a_passing_check_receipts_its_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _check(tmp_path, capsys)

    assert run.exit_code == 0, run.output
    spec = run.spec
    assert spec["healthCheckPassed"] is True
    assert spec["release"] == RELEASE
    assert spec["previousRelease"] == PREVIOUS
    assert spec["healthCheck"] == {
        "type": "command",
        "command": ["bin/smoke", "0", "0"],
        "attempts": 1,
        "exitCode": 0,
        "timedOut": False,
    }


def test_s009_5_a_check_exiting_3_fails_and_receipts_the_failed_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _check(tmp_path, capsys, exit_code=3, attempts=2)

    assert run.exit_code == 82
    assert (
        "The command health check of crm-backend failed on h1: bin/smoke 3 0"
        " exited with code 3 on the last of 2 attempts"
    ) in run.output
    spec = run.spec
    assert spec["healthCheckPassed"] is False
    assert spec["release"] == RELEASE
    assert spec["previousRelease"] == PREVIOUS
    assert spec["healthCheck"] == {
        "type": "command",
        "command": ["bin/smoke", "3", "0"],
        "attempts": 2,
        "exitCode": 3,
        "timedOut": False,
    }


def test_s009_6_a_failed_first_release_receipts_no_previous_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _check(tmp_path, capsys, exit_code=3, first_release=True)

    assert run.exit_code == 82
    spec = run.spec
    assert spec["healthCheckPassed"] is False
    assert spec["previousRelease"] is None
    assert run.health_check["exitCode"] == 3


def test_s009_7_a_timed_out_check_fails_naming_the_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _check(tmp_path, capsys, timed_out=True)

    assert run.exit_code == 82
    assert "timed out after 60 seconds and was stopped" in run.output
    assert run.spec["healthCheckPassed"] is False
    assert run.health_check["timedOut"] is True
    assert run.health_check["exitCode"] is None


def test_s009_7_a_timed_out_run_counts_as_a_failed_attempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command itself exits 0, but the unit was stopped at its limit."""
    run = _check(tmp_path, capsys, timed_out=True, attempts=2)

    assert run.exit_code == 82
    assert run.attempts == 2


def test_s009_8_a_check_failing_twice_then_passing_records_three_attempts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _check(tmp_path, capsys, exit_code=1, passing_attempt=3, attempts=3)

    assert run.exit_code == 0, run.output
    assert run.attempts == 3
    assert run.spec["healthCheckPassed"] is True
    assert run.health_check["attempts"] == 3
    assert run.health_check["exitCode"] == 0


def test_s009_8_a_check_always_failing_runs_exactly_attempts_times_3s_apart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _check(tmp_path, capsys, exit_code=1, attempts=3)

    assert run.exit_code == 82
    assert run.attempts == 3
    times = run.attempt_times
    assert len(times) == 3
    assert all(3 <= later - earlier < 10 for earlier, later in pairwise(times))
    assert run.health_check["attempts"] == 3


def test_s009_8_a_passing_first_attempt_stops_there(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _check(tmp_path, capsys, attempts=3)

    assert run.exit_code == 0, run.output
    assert run.attempts == 1


# The command's output stays on the server.


def test_s009_9_the_output_reaches_neither_the_receipt_nor_the_ansible_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _check(tmp_path, capsys, exit_code=1, verbose=True)

    assert run.exit_code == 2, run.output
    assert "The command health check of crm-backend failed" in run.output
    assert "cloudfall-health-crm-backend" in run.output
    assert MARKER not in run.output
    assert MARKER not in run.receipt_text
    assert MARKER not in _deploy_failure(_failed_check(timed_out=False))


def test_s009_9_the_run_is_no_log_and_pipes_nothing() -> None:
    run = _task(RUN, _check_tasks())
    command = run["ansible.builtin.command"]
    assert isinstance(command, dict)

    assert run["no_log"] is True
    assert "--pipe" not in str(command["argv"])


@pytest.mark.parametrize("key", ["output", "outputTail", "stdout", "stderr"])
def test_s009_9_the_receipt_schema_refuses_any_other_health_check_key(
    key: str,
) -> None:
    receipt = _receipt_document(
        passed=False,
        health_check={**FAILED_CHECK, key: "secret"},
    )

    with pytest.raises(ValidationError, match="Additional properties"):
        _validate(receipt)


# The receipt schema.

FAILED_CHECK: dict[str, object] = {
    "type": "command",
    "command": ["bin/smoke"],
    "attempts": 3,
    "exitCode": 3,
    "timedOut": False,
}


def _receipt_document(
    *,
    passed: bool,
    health_check: dict[str, object] | None,
    release_command: bool = False,
    previous: str | None = PREVIOUS,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "component": COMPONENT_ID,
        "server": "h1",
        "release": RELEASE,
        "artifactSha256": "a" * 64,
        "deployedAt": "2026-10-28T00:00:00Z",
        "previousRelease": previous,
        "healthCheckPassed": passed,
    }
    if release_command:
        spec["releaseCommand"] = {
            "server": "h1",
            "command": ["bin/migrate"],
            "workingDirectory": None,
            "exitCode": 0,
            "outputTail": "migrated",
        }
    if health_check is not None:
        spec["healthCheck"] = health_check
    return {
        "apiVersion": "cloudfall/v1",
        "kind": "ReleaseReceipt",
        "metadata": {
            "id": COMPONENT_ID,
            "description": "Release receipt written by cloudfall_deploy",
        },
        "spec": spec,
    }


@pytest.mark.parametrize(
    "health_check",
    [None, {"type": "http"}, {**FAILED_CHECK, "exitCode": 0}],
    ids=["no-health-check", "not-a-command-check", "exit-0"],
)
def test_s009_11_a_failed_receipt_needs_a_failed_command_check(
    health_check: dict[str, object] | None,
) -> None:
    with pytest.raises(ValidationError):
        _validate(_receipt_document(passed=False, health_check=health_check))


@pytest.mark.parametrize(
    "health_check",
    [
        {**FAILED_CHECK, "exitCode": None},
        {**FAILED_CHECK, "timedOut": True},
        {**FAILED_CHECK, "attempts": 21},
    ],
    ids=["null-exit-without-timeout", "timeout-with-exit-code", "attempts-21"],
)
def test_s009_11_a_command_check_record_must_be_coherent(
    health_check: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _validate(_receipt_document(passed=False, health_check=health_check))


def test_s009_11_a_passed_receipt_refuses_a_failed_command_check() -> None:
    with pytest.raises(ValidationError):
        _validate(_receipt_document(passed=True, health_check=FAILED_CHECK))


@pytest.mark.parametrize("release_command", [False, True])
@pytest.mark.parametrize("previous", [PREVIOUS, None])
def test_s009_11_every_receipt_written_today_still_validates(
    release_command: bool,  # noqa: FBT001 - a pytest parameter
    previous: str | None,
) -> None:
    """The shape main wrote before #164: passed, no ``healthCheck``."""
    _validate(
        _receipt_document(
            passed=True,
            health_check=None,
            release_command=release_command,
            previous=previous,
        )
    )


def test_s009_11_a_timed_out_failure_validates() -> None:
    _validate(
        _receipt_document(
            passed=False,
            health_check={**FAILED_CHECK, "exitCode": None, "timedOut": True},
        )
    )


# The rescue and the deploy's failure message.


def _failed_check(*, timed_out: bool) -> dict[str, object]:
    return {
        **FAILED_CHECK,
        "exitCode": None if timed_out else 3,
        "timedOut": timed_out,
    }


def _rescue_variables(
    previous_directory: str, result: dict[str, object] | None
) -> dict[str, object]:
    variables: dict[str, object] = {
        "ansible_check_mode": False,
        "cloudfall_server_id": "h1",
        "cloudfall_deploy_previous_directory": previous_directory,
        "cloudfall_deploy_unit_hand_made": False,
        "cloudfall_deploy_unit_was_active": {"rc": 0},
        "cloudfall_deploy_unit_kept_path": (
            "/etc/systemd/system/crm-backend.service.pre-cloudfall"
        ),
        "cloudfall_deploy_release": RELEASE,
        "cloudfall_deploy_component": {
            "id": COMPONENT_ID,
            "service": {"name": COMPONENT_ID},
            "healthCheck": _command_check(["bin/smoke"], attempts=3),
        },
    }
    if result is not None:
        variables["cloudfall_deploy_command_health_result"] = result
    return variables


def _rescue(previous_directory: str, result: dict[str, object] | None) -> list[str]:
    variables = _rescue_variables(previous_directory, result)
    return [
        str(task["name"]) for task in _section("rescue") if _runs(task, variables)
    ]


def _deploy_failure(
    result: dict[str, object], previous_directory: str = f"/srv/releases/{PREVIOUS}"
) -> str:
    fail = _task(FAIL, _section("rescue"))["ansible.builtin.fail"]
    assert isinstance(fail, dict)
    variables = _rescue_variables(previous_directory, result)
    return str(_templar(variables).template(trust_as_template(fail["msg"])))


def test_s009_5_a_failed_check_rolls_back_receipts_then_fails() -> None:
    assert _rescue(f"/srv/releases/{PREVIOUS}", _failed_check(timed_out=False)) == [
        RESTORE,
        RESTART_RESTORED,
        FAILED_RECEIPT,
        FAIL,
    ]
    receipt = _task(FAILED_RECEIPT, _section("rescue"))
    assert receipt["ansible.builtin.include_tasks"] == "release_receipt.yml"
    assert receipt["vars"] == {"cloudfall_deploy_receipt_passed": False}


def test_s009_5_the_deploy_error_names_the_check_its_exit_and_the_rollback() -> None:
    assert _deploy_failure(_failed_check(timed_out=False)) == (
        f"Release {RELEASE} failed the command health check of crm-backend on h1"
        f" (exit code 3 on the last of 3 attempts) and was rolled back to"
        f" {PREVIOUS}"
    )


def test_s009_6_a_failed_first_release_is_stopped_and_receipted() -> None:
    assert _rescue("", _failed_check(timed_out=False)) == [
        STOP_FIRST,
        FAILED_RECEIPT,
        FAIL,
    ]
    assert _deploy_failure(_failed_check(timed_out=False), "").endswith(
        "and was stopped because no previous release exists"
    )


def test_s009_7_the_deploy_error_names_the_timeout() -> None:
    assert _deploy_failure(_failed_check(timed_out=True)) == (
        f"Release {RELEASE} failed the command health check of crm-backend on h1"
        f" (timed out after 60 seconds on the last of 3 attempts) and was rolled"
        f" back to {PREVIOUS}"
    )
    single = {**_failed_check(timed_out=True), "attempts": 1}
    assert "(timed out after 60 seconds on its only attempt)" in _deploy_failure(single)


# http and none keep today's gate and receipt.


def _gate(health_check: dict[str, object]) -> list[str]:
    variables: dict[str, object] = {
        "ansible_check_mode": False,
        "cloudfall_deploy_component": {
            "service": {"name": COMPONENT_ID},
            "healthCheck": health_check,
        },
    }
    return [
        str(task["name"])
        for task in _section("block")
        if task["name"] in {HTTP_GATE, COMMAND_GATE, *UNIT_GATE}
        and _runs(task, variables)
    ]


def test_s009_12_http_and_none_run_the_same_gate_tasks() -> None:
    assert _gate(HTTP_CHECK) == [HTTP_GATE]
    assert _gate(NO_CHECK) == UNIT_GATE
    assert _gate(_command_check(["bin/smoke"])) == [COMMAND_GATE]


def test_s009_12_a_failed_http_or_none_gate_writes_no_receipt() -> None:
    """Their gate never sets the command result, so the rescue skips the receipt."""
    for previous in (f"/srv/releases/{PREVIOUS}", ""):
        assert FAILED_RECEIPT not in _rescue(previous, None)
    variables = _rescue_variables("", None)
    fail = _task(FAIL, _section("rescue"))["ansible.builtin.fail"]
    assert isinstance(fail, dict)
    message = str(_templar(variables).template(trust_as_template(fail["msg"])))
    assert message == (
        f"Release {RELEASE} failed its health check and was stopped because no"
        " previous release exists"
    )


@pytest.mark.parametrize("health_check", [HTTP_CHECK, NO_CHECK], ids=["http", "none"])
def test_s009_12_http_and_none_receipts_have_no_health_check(
    health_check: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    variables = {
        "cloudfall_server_id": "h1",
        "cloudfall_deploy_release": RELEASE,
        "cloudfall_deploy_artifact_sha256": "a" * 64,
        "cloudfall_deploy_previous_directory": "",
        "cloudfall_deploy_receipt_directory": str(tmp_path / "receipts"),
        "cloudfall_deploy_component": _component(health_check, tmp_path / "app"),
    }
    playbook = tmp_path / "receipt.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Receipt a passed gate",
                    "hosts": "localhost",
                    "gather_facts": True,
                    "vars": variables,
                    "tasks": [
                        {
                            "name": "Receipt as the role does",
                            "ansible.builtin.include_role": {
                                "name": "cloudfall_deploy",
                                "tasks_from": "release_receipt",
                            },
                            "vars": {"cloudfall_deploy_receipt_passed": True},
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "playbook",
            "run",
            str(playbook),
            "--engine",
            str(ENGINE),
            "--inventory",
            str(_local_inventory(tmp_path)),
        ]
    )

    assert exit_code == 0, capsys.readouterr()
    run = _Run(tmp_path, exit_code, "")
    assert run.spec["healthCheckPassed"] is True
    assert "healthCheck" not in run.spec


def test_s009_12_the_pass_receipt_is_written_where_it_was() -> None:
    names = [str(task["name"]) for task in _main()]
    receipt = _task(PASSED_RECEIPT, _main())

    assert names.index(ACTIVATE) < names.index(PASSED_RECEIPT)
    assert receipt["ansible.builtin.include_tasks"] == "release_receipt.yml"
    assert receipt["vars"] == {"cloudfall_deploy_receipt_passed": True}
    assert "when" not in receipt


# restart, rollback and health run the same check and write no receipt.

SHARED_GATE = "health_gate.yml"
SHARED_COMMAND_GATE = "Gate the component on its declared command health check"


def _tasks(name: str) -> list[dict[str, object]]:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / name))
    assert isinstance(tasks, list)
    return tasks


def _entry_point(  # noqa: PLR0913 - one knob per case.
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tasks_from: str,
    health_check: dict[str, object] | None = None,
    *,
    exit_code: int = 0,
    attempts: int = 1,
    timed_out: bool = False,
) -> _Run:
    """Run one of the role's task files for real against a release in current.

    ``ansible_become: false`` stands in for the root the restart, rollback
    and health plays reach on a server; the controller here has no sudo.
    """
    install_root = tmp_path / "app"
    release_directory = install_root / "releases" / RELEASE
    release_directory.mkdir(parents=True)
    (install_root / "current").symlink_to(release_directory)
    _executable(release_directory / "bin" / "smoke", SMOKE)
    if timed_out:
        (release_directory / TIMEOUT_MARKER).write_text("", encoding="utf-8")
    systemd_run = tmp_path / "systemd-run"
    _executable(systemd_run, FAKE_SYSTEMD_RUN)
    if health_check is None:
        health_check = _command_check(
            ["bin/smoke", str(exit_code), "0"], attempts=attempts
        )
    playbook = tmp_path / "entry.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "name": f"Run the role's {tasks_from} tasks",
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "ansible_become": False,
                        "cloudfall_server_id": "h1",
                        "cloudfall_deploy_receipt_directory": str(
                            tmp_path / "receipts"
                        ),
                        "cloudfall_deploy_systemd_run": str(systemd_run),
                        "cloudfall_deploy_component": _component(
                            health_check, install_root
                        ),
                    },
                    "tasks": [
                        {
                            "name": "Include the role's tasks",
                            "ansible.builtin.include_role": {
                                "name": "cloudfall_deploy",
                                "tasks_from": tasks_from,
                            },
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    code = main(
        [
            "playbook",
            "run",
            str(playbook),
            "--engine",
            str(ENGINE),
            "--inventory",
            str(_local_inventory(tmp_path)),
        ]
    )
    captured = capsys.readouterr()
    return _Run(tmp_path, code, f"{captured.out}\n{captured.err}")


@pytest.mark.parametrize("entry_point", ["restart.yml", "rollback.yml", "health.yml"])
def test_s009_10_restart_rollback_and_health_end_on_the_shared_gate(
    entry_point: str,
) -> None:
    last = _tasks(entry_point)[-1]

    assert last["ansible.builtin.include_tasks"] == SHARED_GATE


def test_s009_10_the_shared_gate_runs_deploys_command_check_as_root() -> None:
    gate = _task(SHARED_COMMAND_GATE, _tasks(SHARED_GATE))
    include = gate["ansible.builtin.include_tasks"]
    variables: dict[str, object] = {
        "ansible_check_mode": False,
        "cloudfall_deploy_component": {"healthCheck": _command_check(["bin/smoke"])},
    }

    assert include == {"file": "command_health_check.yml", "apply": {"become": True}}
    assert _runs(gate, variables)
    assert not _runs(gate, {**variables, "ansible_check_mode": True})
    for other in (HTTP_CHECK, NO_CHECK):
        assert not _runs(
            gate, {**variables, "cloudfall_deploy_component": {"healthCheck": other}}
        )


def test_s009_12_restart_rollback_and_health_keep_their_http_and_none_gates() -> None:
    def gated(health_check: dict[str, object]) -> list[str]:
        variables: dict[str, object] = {
            "ansible_check_mode": False,
            "cloudfall_deploy_component": {"healthCheck": health_check},
        }
        return [
            str(task["name"])
            for task in _tasks(SHARED_GATE)
            if _runs(task, variables)
        ]

    assert gated(HTTP_CHECK) == ["Gate the component on its declared HTTP health check"]
    assert gated(NO_CHECK) == ["Gate the component on an active service unit"]
    assert gated(_command_check(["bin/smoke"])) == [SHARED_COMMAND_GATE]


@pytest.mark.parametrize(
    "name",
    [
        "restart.yml",
        "rollback.yml",
        "health.yml",
        SHARED_GATE,
        "command_health_check.yml",
    ],
)
def test_s009_10_no_entry_point_includes_the_receipt(name: str) -> None:
    assert "release_receipt" not in (ROLE / "tasks" / name).read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("tasks_from", ["health", "health_gate"])
def test_s009_10_a_passing_check_passes_with_deploys_invocation(
    tasks_from: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _entry_point(tmp_path, capsys, tasks_from)

    assert run.exit_code == 0, run.output
    argv = run.argv
    options = argv[: argv.index("--")]
    assert "--unit=cloudfall-health-crm-backend" in options
    assert "--uid=crm" in options
    assert f"--working-directory={run.install_root}/current" in options
    assert (
        "--property=EnvironmentFile=-"
        f"{run.install_root}/shared/env/{COMPONENT_ID}.env"
    ) in options
    assert "--property=RuntimeMaxSec=60" in options
    assert argv[argv.index("--") + 1 :] == [
        f"{run.install_root}/current/bin/smoke",
        "0",
        "0",
    ]
    assert not (tmp_path / "receipts").exists()


@pytest.mark.parametrize("tasks_from", ["health", "health_gate"])
def test_s009_10_a_failing_check_fails_without_its_output_or_a_receipt(
    tasks_from: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _entry_point(tmp_path, capsys, tasks_from, exit_code=3, attempts=2)

    assert run.exit_code == 82
    assert (
        "The command health check of crm-backend failed on h1: bin/smoke 3 0"
        " exited with code 3 on the last of 2 attempts"
    ) in run.output
    assert run.attempts == 2
    assert MARKER not in run.output
    assert not (tmp_path / "receipts").exists()


def test_s009_10_a_timed_out_check_fails_naming_the_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _entry_point(tmp_path, capsys, "health", timed_out=True)

    assert run.exit_code == 82
    assert "timed out after 60 seconds and was stopped" in run.output
    assert not (tmp_path / "receipts").exists()


def _health(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], exit_code: int
) -> dict[str, object]:
    """``cloudfall health`` with its playbook step running the role's tasks.

    The inventory step is skipped: the play targets this controller, not the
    example project's servers.
    """

    def run(step: ExecutionStep) -> StepRun:
        if step.description == "render Ansible inventory":
            return subprocess.CompletedProcess(step.argv, 0, "", "")
        assert step.argv[-1].endswith("health.yml")
        done = _entry_point(tmp_path, capsys, "health", exit_code=exit_code)
        return subprocess.CompletedProcess(step.argv, done.exit_code, done.output, "")

    context = EngineContext(
        project_directory=ROOT / "config" / "examples",
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=tmp_path / "inventory.json",
        run=run,
    )
    return health(context, ResourceId(COMPONENT_ID)).as_dict()


def test_s009_10_health_reports_unhealthy_when_the_check_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _health(tmp_path, capsys, exit_code=3)

    assert result["status"] == "unhealthy"
    assert result["healthy"] is False
    detail = str(result["detail"])
    assert "The command health check of crm-backend failed" in detail
    assert MARKER not in detail
    assert "receipt" not in result


def test_s009_10_health_reports_ok_when_the_check_exits_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _health(tmp_path, capsys, exit_code=0)

    assert result["status"] == "ok"
    assert result["healthy"] is True
    assert "detail" not in result
    assert "receipt" not in result
