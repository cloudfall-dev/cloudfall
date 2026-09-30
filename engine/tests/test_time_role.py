"""The time role's decisions, evaluated with Ansible's own templating.

The role only runs on Debian with systemd, so these tests replay what it
decides from ``timedatectl show`` output rather than running it: each
task's ``when`` and each assertion's ``that`` go through Ansible's
evaluator against the host states Cloudfall fleets report.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).parents[2]
TASKS = ROOT / "engine" / "ansible" / "roles" / "cloudfall_time" / "tasks" / "main.yml"
BASELINE = ["Timezone=Etc/UTC", "LocalRTC=no"]
ENABLE = "Enable network time synchronization"
REPORT_DRIFT = "Report NTP drift in check mode"
REPORT_UNSYNCHRONIZED = (
    "Report an unsynchronized clock without systemd-timesyncd in check mode"
)

# systemd-timesyncd installed and running
TIMESYNCD_ON = [*BASELINE, "NTP=yes", "NTPSynchronized=yes", "CanNTP=yes"]
# systemd-timesyncd installed but switched off
TIMESYNCD_OFF = [*BASELINE, "NTP=no", "NTPSynchronized=yes", "CanNTP=yes"]
# ntp, ntpsec, or chrony keeps time; timedatectl cannot switch it
OTHER_DAEMON = [*BASELINE, "NTP=no", "NTPSynchronized=yes", "CanNTP=no"]
# nothing keeps time
NO_DAEMON = [*BASELINE, "NTP=no", "NTPSynchronized=no", "CanNTP=no"]


def _tasks() -> list[dict[str, object]]:
    tasks = DataLoader().load_from_file(str(TASKS))
    assert isinstance(tasks, list)
    return tasks


def _holds(expression: object, variables: dict[str, object]) -> bool:
    templar = Templar(loader=DataLoader(), variables=variables)
    return bool(templar.evaluate_conditional(trust_as_template(str(expression))))


def _conditions(task: dict[str, object], key: str) -> list[object]:
    value = task.get(key, [])
    return value if isinstance(value, list) else [value]


def _variables(
    before: list[str], *, check_mode: bool, after: list[str] | None = None
) -> dict[str, object]:
    return {
        "ansible_check_mode": check_mode,
        "cloudfall_time_timezone": "Etc/UTC",
        "cloudfall_time_before": {"stdout_lines": before},
        "cloudfall_time_after": {"stdout_lines": after or before},
    }


def _runs(before: list[str], *, check_mode: bool) -> set[str]:
    """Name every task whose ``when`` holds for this host and mode."""
    variables = _variables(before, check_mode=check_mode)
    return {
        str(task["name"])
        for task in _tasks()
        if all(_holds(c, variables) for c in _conditions(task, "when"))
    }


def _baseline_assertion_holds(after: list[str]) -> bool:
    task = next(
        t for t in _tasks() if t["name"] == "Require applied UTC time configuration"
    )
    variables = _variables(after, check_mode=False, after=after)
    assert isinstance(task["ansible.builtin.assert"], dict)
    return all(_holds(c, variables) for c in task["ansible.builtin.assert"]["that"])


def test_check_mode_reports_timesyncd_switched_off() -> None:
    runs = _runs(TIMESYNCD_OFF, check_mode=True)

    assert REPORT_DRIFT in runs
    assert ENABLE not in runs


def test_a_deploy_switches_timesyncd_on_and_requires_it() -> None:
    assert ENABLE in _runs(TIMESYNCD_OFF, check_mode=False)
    assert _baseline_assertion_holds(TIMESYNCD_ON)
    assert not _baseline_assertion_holds(TIMESYNCD_OFF)


@pytest.mark.parametrize("check_mode", [True, False])
def test_another_synchronized_daemon_is_left_alone(*, check_mode: bool) -> None:
    """A host on ntp, ntpsec, or chrony has no timesyncd for set-ntp to start."""
    runs = _runs(OTHER_DAEMON, check_mode=check_mode)

    assert REPORT_DRIFT not in runs
    assert REPORT_UNSYNCHRONIZED not in runs
    assert ENABLE not in runs
    assert _baseline_assertion_holds(OTHER_DAEMON)


def test_check_mode_warns_when_nothing_keeps_time() -> None:
    runs = _runs(NO_DAEMON, check_mode=True)

    assert REPORT_UNSYNCHRONIZED in runs
    assert ENABLE not in runs


def test_a_deploy_fails_when_the_clock_never_synchronizes() -> None:
    task = next(t for t in _tasks() if t["name"] == "Require synchronized network time")
    assert isinstance(task["ansible.builtin.assert"], dict)
    (condition,) = task["ansible.builtin.assert"]["that"]

    assert not _holds(condition, {"cloudfall_time_synchronized": {"stdout": "no"}})
    assert _holds(condition, {"cloudfall_time_synchronized": {"stdout": "yes"}})
