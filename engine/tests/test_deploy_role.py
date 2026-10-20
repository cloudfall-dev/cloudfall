"""The deploy role's health gate, evaluated with Ansible's own templating.

The role needs a Debian host with systemd, so these tests replay what it
decides rather than running it: which gate tasks run for each health-check
type, and which rescue steps run when a gate task fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_deploy"
ACTIVATE = "Activate the release behind the health gate"
HTTP_GATE = "Gate the release on its declared health check"
UNIT_STARTED = "Gate the release on an active service unit"
RESTARTS_BEFORE = "Read the service unit restart count before settling"
UNIT_SETTLE = "Let the service unit settle"
UNIT_SETTLED = "Gate the release on a service unit still active after settling"
RESTARTS_AFTER = "Read the service unit restart count after settling"
NO_RESTART = "Gate the release on a service unit that did not restart while settling"
UNIT_GATE = [
    UNIT_STARTED,
    RESTARTS_BEFORE,
    UNIT_SETTLE,
    UNIT_SETTLED,
    RESTARTS_AFTER,
    NO_RESTART,
]
RESTORE = "Restore the previous release symlink"
RESTART_RESTORED = "Restart the restored release"
STOP_FIRST = "Stop the failed first release"
FAIL = "Fail the deployment after rolling back"
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


def _load(path: Path) -> object:
    return DataLoader().load_from_file(str(path))


def _activation() -> dict[str, object]:
    tasks = _load(ROLE / "tasks" / "main.yml")
    assert isinstance(tasks, list)
    return next(t for t in tasks if t["name"] == ACTIVATE)


def _section(key: str) -> list[dict[str, object]]:
    section = _activation()[key]
    assert isinstance(section, list)
    return section


def _task(name: str, tasks: list[dict[str, object]]) -> dict[str, object]:
    return next(t for t in tasks if t["name"] == name)


def _templar(variables: dict[str, object]) -> Templar:
    return Templar(loader=DataLoader(), variables=variables)


def _holds(expression: object, variables: dict[str, object]) -> bool:
    return bool(
        _templar(variables).evaluate_conditional(trust_as_template(str(expression)))
    )


def _conditions(task: dict[str, object]) -> list[object]:
    value = task.get("when", [])
    return value if isinstance(value, list) else [value]


def _gate(health_check: dict[str, object], *, check_mode: bool) -> list[str]:
    """Name, in order, every gate task whose ``when`` holds for this component."""
    variables: dict[str, object] = {
        "ansible_check_mode": check_mode,
        "cloudfall_deploy_component": {
            "service": {"name": "crm-worker"},
            "healthCheck": health_check,
        },
    }
    return [
        str(task["name"])
        for task in _section("block")
        if task["name"] in {HTTP_GATE, *UNIT_GATE}
        and all(_holds(c, variables) for c in _conditions(task))
    ]


def _rescue(previous_directory: str) -> set[str]:
    """Name every rescue step that runs once a gate task has failed."""
    variables: dict[str, object] = {
        "cloudfall_deploy_previous_directory": previous_directory
    }
    return {
        str(task["name"])
        for task in _section("rescue")
        if all(_holds(c, variables) for c in _conditions(task))
    }


def test_an_http_component_keeps_its_http_gate() -> None:
    assert _gate(HTTP_CHECK, check_mode=False) == [HTTP_GATE]


def test_a_component_without_http_is_gated_on_its_unit_staying_active() -> None:
    """The unit must be active after the restart and still active after settling."""
    assert _gate(NO_CHECK, check_mode=False) == UNIT_GATE


@pytest.mark.parametrize("health_check", [HTTP_CHECK, NO_CHECK])
def test_check_mode_skips_the_gate(health_check: dict[str, object]) -> None:
    assert _gate(health_check, check_mode=True) == []


@pytest.mark.parametrize("name", [UNIT_STARTED, UNIT_SETTLED])
def test_an_inactive_unit_fails_its_gate_task(name: str) -> None:
    """``systemctl is-active`` exits non-zero for an exited unit, failing the task."""
    task = _task(name, _section("block"))
    command = task["ansible.builtin.command"]
    assert isinstance(command, dict)

    assert command["argv"][:3] == ["/usr/bin/systemctl", "is-active", "--quiet"]
    assert "failed_when" not in task
    assert "ignore_errors" not in task
    assert task["changed_when"] is False


@pytest.mark.parametrize("name", [RESTARTS_BEFORE, RESTARTS_AFTER])
def test_the_restart_count_is_read_from_systemd(name: str) -> None:
    task = _task(name, _section("block"))
    command = task["ansible.builtin.command"]
    assert isinstance(command, dict)

    assert command["argv"][:4] == [
        "/usr/bin/systemctl",
        "show",
        "--property=NRestarts",
        "--value",
    ]
    assert task["changed_when"] is False


def _settled(before: str, after: str) -> bool:
    """Whether the no-restart assertion holds for these ``NRestarts`` readings."""
    assertion = _task(NO_RESTART, _section("block"))["ansible.builtin.assert"]
    assert isinstance(assertion, dict)
    variables: dict[str, object] = {
        "cloudfall_deploy_unit_restarts_before": {"stdout": before},
        "cloudfall_deploy_unit_restarts_after": {"stdout": after},
    }
    return all(_holds(c, variables) for c in assertion["that"])


def test_a_unit_that_did_not_restart_while_settling_passes() -> None:
    assert _settled("0", "0")
    assert _settled("3", "3")


def test_a_crash_looping_unit_fails_even_when_active_at_the_second_check() -> None:
    """``Restart=on-failure`` can bring a crashing worker back before the check."""
    assert not _settled("0", "1")
    assert not _settled("2", "5")


@pytest.mark.parametrize(("before", "after"), [("", ""), ("0", "[not set]")])
def test_an_unreadable_restart_count_fails_the_gate(before: str, after: str) -> None:
    assert not _settled(before, after)


def test_the_unit_settles_for_the_role_default_before_its_second_check() -> None:
    defaults = _load(ROLE / "defaults" / "main.yml")
    assert isinstance(defaults, dict)
    pause = _task(UNIT_SETTLE, _section("block"))["ansible.builtin.pause"]
    assert isinstance(pause, dict)

    seconds = _templar(defaults).template(trust_as_template(pause["seconds"]))

    assert int(str(seconds)) == 10


def test_a_first_release_whose_unit_exits_is_stopped() -> None:
    assert _rescue("") == {STOP_FIRST, FAIL}


def test_a_release_whose_unit_exits_rolls_back() -> None:
    assert _rescue("/srv/apps/crm/worker/releases/r1") == {
        RESTORE,
        RESTART_RESTORED,
        FAIL,
    }
