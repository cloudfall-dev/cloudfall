"""The deploy role's health gate, evaluated with Ansible's own templating.

The role needs a Debian host with systemd, so these tests replay what it
decides rather than running it: which gate tasks run for each health-check
type, which rescue steps run when a gate task fails, and how a hand-made
unit of the same name is kept and restored.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from ansible.module_utils.common.arg_spec import ArgumentSpecValidator
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
RECORD_FAILED = "Record the health check result of the failed release"
RESTORE = "Restore the previous release symlink"
RESTORE_MANAGED_UNIT = "Restore the service unit of the previous release"
RESTART_RESTORED = "Restart the restored release"
GATE_RESTORED = "Gate the restored release on its health check"
RESTORED_GATE = "Run the health gate against the restored release"
FAIL_ROLLBACK = "Fail the deployment whose rollback failed its health check"
STOP_FIRST = "Stop the failed first release"
FAIL = "Fail the deployment after rolling back"
RESTORE_UNIT = "Restore the pre-Cloudfall service unit"
RELOAD_UNIT = "Reload systemd for the restored pre-Cloudfall unit"
ENABLE_UNIT = "Restore the enabled state of the pre-Cloudfall unit"
START_UNIT = "Start the pre-Cloudfall unit that was active before the deploy"
RECOGNIZE = "Recognize a hand-made service unit"
INSPECT_KEPT = "Inspect the kept pre-Cloudfall service unit"
REFUSE_STALE = (
    "Refuse to replace a hand-made unit that differs from the one already kept"
)
WAS_ENABLED = "Record whether the hand-made service unit is enabled"
WAS_ACTIVE = "Record whether the hand-made service unit is active"
REPORT_KEEP = "Report the hand-made service unit a deploy would replace"
KEEP = "Keep the hand-made service unit before replacing it"
ADOPTION = [INSPECT_KEPT, REFUSE_STALE, WAS_ENABLED, WAS_ACTIVE, REPORT_KEEP, KEEP]
INSTALL = "Install the managed service unit"
HAND_MADE_UNIT = """[Unit]
Description=asrest

[Service]
ExecStart=/opt/asrest/bin/asrest

[Install]
WantedBy=multi-user.target
"""
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


def _main() -> list[dict[str, object]]:
    tasks = _load(ROLE / "tasks" / "main.yml")
    assert isinstance(tasks, list)
    return tasks


def _activation() -> dict[str, object]:
    return _task(ACTIVATE, _main())


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


def _rescue_variables(  # noqa: PLR0913 - one knob per rescue condition.
    previous_directory: str,
    *,
    hand_made: bool = False,
    managed_before: bool = False,
    was_active: bool = True,
    was_enabled: str = "enabled",
    check_mode: bool = False,
) -> dict[str, object]:
    return {
        "ansible_check_mode": check_mode,
        "cloudfall_server_id": "h1",
        "cloudfall_deploy_previous_directory": previous_directory,
        "cloudfall_deploy_failed_health_result": None,
        "cloudfall_deploy_unit_hand_made": hand_made,
        "cloudfall_deploy_unit_managed_before": managed_before,
        "cloudfall_deploy_unit_was_active": {"rc": 0 if was_active else 3},
        "cloudfall_deploy_unit_was_enabled": {"stdout": was_enabled},
        "cloudfall_deploy_unit_path": "/etc/systemd/system/asrest.service",
        "cloudfall_deploy_unit_kept_path": (
            "/etc/systemd/system/asrest.service.pre-cloudfall"
        ),
        "cloudfall_deploy_release": "r2",
        "cloudfall_deploy_component": {"id": "asrest", "service": {"name": "asrest"}},
    }


def _rescue(
    previous_directory: str,
    *,
    hand_made: bool = False,
    managed_before: bool = False,
    was_active: bool = True,
    check_mode: bool = False,
) -> set[str]:
    """Name every rescue step that runs once a gate task has failed."""
    variables = _rescue_variables(
        previous_directory,
        hand_made=hand_made,
        managed_before=managed_before,
        was_active=was_active,
        check_mode=check_mode,
    )
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
    assert _rescue("") == {RECORD_FAILED, STOP_FIRST, FAIL}


def test_a_release_whose_unit_exits_rolls_back() -> None:
    assert _rescue("/srv/apps/crm/worker/releases/r1") == {
        RECORD_FAILED,
        RESTORE,
        RESTART_RESTORED,
        GATE_RESTORED,
        FAIL,
    }


def _hand_made(content: str | None) -> object:
    """Decide, as the role does, whether the unit file found is hand-made."""
    expression = _task(RECOGNIZE, _main())["ansible.builtin.set_fact"]
    assert isinstance(expression, dict)
    variables: dict[str, object] = (
        {
            "cloudfall_deploy_unit_existing": {"stat": {"exists": False}},
            "cloudfall_deploy_unit_existing_content": {"skipped": True},
        }
        if content is None
        else {
            "cloudfall_deploy_unit_existing": {"stat": {"exists": True, "isreg": True}},
            "cloudfall_deploy_unit_existing_content": {
                "content": base64.b64encode(content.encode()).decode()
            },
        }
    )
    return _templar(variables).template(
        trust_as_template(expression["cloudfall_deploy_unit_hand_made"])
    )


def _adoption(*, hand_made: bool, check_mode: bool) -> list[str]:
    """Name, in order, every adoption task whose ``when`` holds."""
    variables: dict[str, object] = {
        "ansible_check_mode": check_mode,
        "cloudfall_deploy_unit_hand_made": hand_made,
    }
    return [
        str(task["name"])
        for task in _main()
        if task["name"] in ADOPTION
        and all(_holds(c, variables) for c in _conditions(task))
    ]


def test_a_unit_without_the_cloudfall_header_is_hand_made() -> None:
    assert _hand_made(HAND_MADE_UNIT) is True


def test_a_unit_rendered_by_cloudfall_is_not_hand_made() -> None:
    template = (ROLE / "templates" / "component.service.j2").read_text()

    assert _hand_made(template.splitlines()[0] + "\n[Unit]\n") is False


def test_a_missing_unit_is_not_hand_made() -> None:
    assert _hand_made(None) is False


def test_a_hand_made_unit_is_kept_with_its_state_before_the_install() -> None:
    names = [str(task["name"]) for task in _main()]

    assert _adoption(hand_made=True, check_mode=False) == [
        INSPECT_KEPT,
        REFUSE_STALE,
        WAS_ENABLED,
        WAS_ACTIVE,
        KEEP,
    ]
    assert names.index(KEEP) < names.index(INSTALL)


def test_a_cloudfall_unit_takes_no_copy() -> None:
    assert _adoption(hand_made=False, check_mode=False) == []
    assert _adoption(hand_made=False, check_mode=True) == []


def test_check_mode_reports_the_hand_made_unit_without_copying_it() -> None:
    assert _adoption(hand_made=True, check_mode=True) == [
        INSPECT_KEPT,
        REFUSE_STALE,
        REPORT_KEEP,
    ]


def test_the_kept_copy_is_root_owned_beside_the_unit_and_never_overwritten() -> None:
    copy = _task(KEEP, _main())["ansible.builtin.copy"]
    assert isinstance(copy, dict)
    paths = _task("Compute service unit paths", _main())["ansible.builtin.set_fact"]
    assert isinstance(paths, dict)
    variables: dict[str, object] = {
        "cloudfall_deploy_component": {"service": {"name": "asrest"}}
    }

    kept = _templar(variables).template(
        trust_as_template(paths["cloudfall_deploy_unit_kept_path"])
    )

    assert kept == "/etc/systemd/system/asrest.service.pre-cloudfall"
    assert copy["remote_src"] is True
    assert copy["force"] is False
    assert (copy["owner"], copy["group"], copy["mode"]) == ("root", "root", "0644")


@pytest.mark.parametrize(
    ("stale", "outcome"),
    [(None, "accepted"), ("a" * 64, "accepted"), ("b" * 64, "refused")],
)
def test_a_kept_copy_of_another_unit_refuses_the_deploy(
    stale: str | None, outcome: str
) -> None:
    assertion = _task(REFUSE_STALE, _main())["ansible.builtin.assert"]
    assert isinstance(assertion, dict)
    variables: dict[str, object] = {
        "cloudfall_deploy_unit_existing": {"stat": {"checksum": "a" * 64}},
        "cloudfall_deploy_unit_kept_before": {
            "stat": {"exists": False}
            if stale is None
            else {"exists": True, "checksum": stale}
        },
    }

    holds = all(_holds(c, variables) for c in assertion["that"])

    assert holds is (outcome == "accepted")


def test_a_failed_first_release_over_a_hand_made_unit_restores_and_starts_it() -> None:
    assert _rescue("", hand_made=True) == {
        RECORD_FAILED,
        STOP_FIRST,
        RESTORE_UNIT,
        RELOAD_UNIT,
        ENABLE_UNIT,
        START_UNIT,
        FAIL,
    }
    names = [str(task["name"]) for task in _section("rescue")]
    assert names.index(STOP_FIRST) < names.index(RESTORE_UNIT)
    assert names.index(RESTORE_UNIT) < names.index(RELOAD_UNIT)
    assert names.index(RELOAD_UNIT) < names.index(START_UNIT)


def test_a_restored_unit_that_was_stopped_stays_stopped() -> None:
    assert _rescue("", hand_made=True, was_active=False) == {
        RECORD_FAILED,
        STOP_FIRST,
        RESTORE_UNIT,
        RELOAD_UNIT,
        ENABLE_UNIT,
        FAIL,
    }


def test_the_restore_copies_the_kept_unit_back_over_the_managed_one() -> None:
    copy = _task(RESTORE_UNIT, _section("rescue"))["ansible.builtin.copy"]
    assert isinstance(copy, dict)

    assert (copy["src"], copy["dest"]) == (
        "{{ cloudfall_deploy_unit_kept_path }}",
        "{{ cloudfall_deploy_unit_path }}",
    )
    assert copy["remote_src"] is True
    assert copy["force"] is True


@pytest.mark.parametrize(
    ("was_enabled", "verb"),
    [("enabled", "reenable"), ("disabled", "disable"), ("static", "disable")],
)
def test_the_restored_unit_gets_back_its_enabled_state(
    was_enabled: str, verb: str
) -> None:
    command = _task(ENABLE_UNIT, _section("rescue"))["ansible.builtin.command"]
    assert isinstance(command, dict)
    variables = _rescue_variables("", hand_made=True, was_enabled=was_enabled)

    argv = [
        _templar(variables).template(trust_as_template(part))
        for part in command["argv"]
    ]

    assert argv == ["/usr/bin/systemctl", verb, "asrest.service"]


def test_a_rollback_to_a_previous_release_leaves_a_kept_unit_alone() -> None:
    assert _rescue("/srv/apps/asrest/releases/r1", hand_made=True) == {
        RECORD_FAILED,
        RESTORE,
        RESTART_RESTORED,
        GATE_RESTORED,
        FAIL,
    }


def test_a_first_release_over_a_cloudfall_unit_does_not_resurrect_a_kept_copy() -> None:
    """``current`` removed by hand leaves no previous release, not a hand-made unit."""
    assert _rescue("", hand_made=False) == {RECORD_FAILED, STOP_FIRST, FAIL}


def test_check_mode_never_restores_a_unit() -> None:
    assert _rescue("", hand_made=True, check_mode=True) == {
        RECORD_FAILED,
        STOP_FIRST,
        FAIL,
    }


def _failure(*, hand_made: bool, was_active: bool = True) -> str:
    fail = _task(FAIL, _section("rescue"))["ansible.builtin.fail"]
    assert isinstance(fail, dict)
    variables = _rescue_variables("", hand_made=hand_made, was_active=was_active)
    return str(_templar(variables).template(trust_as_template(fail["msg"])))


def test_the_failure_says_the_hand_made_unit_was_restored() -> None:
    assert _failure(hand_made=True) == (
        "Release r2 failed its health check and was stopped because no previous"
        " release exists; the pre-Cloudfall unit was restored from"
        " /etc/systemd/system/asrest.service.pre-cloudfall and started"
    )
    assert _failure(hand_made=True, was_active=False).endswith(
        " and left stopped as it was before the deploy"
    )


def test_the_failure_of_a_first_release_without_a_kept_unit_is_unchanged() -> None:
    assert _failure(hand_made=False) == (
        "Release r2 failed its health check and was stopped because no previous"
        " release exists"
    )


# A same-release redeploy never writes into the live directory (#223).

RELEASE = "20261028T000000Z-abcdef2"
PREVIOUS = "20261027T000000Z-abcdef1"
RELEASES = "/srv/apps/crm/backend/releases"
COMPUTE = "Compute release paths"
INSPECT_CURRENT = "Inspect the currently active release"
CREATE = "Create release and shared directories"


def _release_directory(previous_directory: str) -> str:
    """Answer the directory a deploy unpacks into with this ``current`` target."""
    paths = _task(COMPUTE, _main())["ansible.builtin.set_fact"]
    assert isinstance(paths, dict)
    variables: dict[str, object] = {
        "cloudfall_deploy_release": RELEASE,
        "cloudfall_deploy_previous_directory": previous_directory,
        "cloudfall_deploy_component": {
            "id": "crm-backend",
            "install_root": "/srv/apps/crm/backend",
        },
    }
    return str(
        _templar(variables).template(
            trust_as_template(paths["cloudfall_deploy_release_directory"])
        )
    )


@pytest.mark.parametrize(
    "previous_directory",
    ["", f"{RELEASES}/{PREVIOUS}", f"{RELEASES}/{PREVIOUS}.redeploy-20261101T000000Z"],
    ids=["first-release", "another-release", "a-redeployed-other-release"],
)
def test_a_new_release_unpacks_into_its_own_directory(previous_directory: str) -> None:
    assert _release_directory(previous_directory) == f"{RELEASES}/{RELEASE}"


@pytest.mark.parametrize(
    "previous_directory",
    [f"{RELEASES}/{RELEASE}", f"{RELEASES}/{RELEASE}.redeploy-20261101T000000Z"],
    ids=["first-copy", "earlier-redeploy"],
)
def test_the_current_release_redeploys_into_a_fresh_directory(
    previous_directory: str,
) -> None:
    directory = _release_directory(previous_directory)

    assert re.fullmatch(
        rf"{RELEASES}/{RELEASE}\.redeploy-[0-9]{{8}}T[0-9]{{6}}Z", directory
    )
    assert directory != previous_directory


def test_the_release_directory_is_chosen_after_reading_current() -> None:
    names = [str(task["name"]) for task in _main()]

    assert names.index(INSPECT_CURRENT) < names.index(COMPUTE)
    assert names.index(COMPUTE) < names.index(CREATE)


def test_the_failure_names_the_release_a_redeploy_rolled_back_to() -> None:
    fail = _task(FAIL, _section("rescue"))["ansible.builtin.fail"]
    assert isinstance(fail, dict)
    variables = _rescue_variables(
        f"{RELEASES}/{RELEASE}.redeploy-20261101T000000Z"
    )

    message = str(_templar(variables).template(trust_as_template(fail["msg"])))

    assert message == (
        f"Release r2 failed its health check and was rolled back to {RELEASE}"
    )


# The previous release gets its own unit back, and its gate (#223).

MANAGED_UNIT = """# Managed by Cloudfall. Local changes will be overwritten.
[Unit]
Description=Cloudfall component asrest

[Service]
ExecStart=/srv/apps/asrest/current/bin/asrest "previous"
"""


def _managed_before(content: str | None) -> object:
    """Decide, as the role does, whether the unit found is a managed one."""
    expression = _task(RECOGNIZE, _main())["ansible.builtin.set_fact"]
    assert isinstance(expression, dict)
    variables: dict[str, object] = (
        {
            "cloudfall_deploy_unit_existing": {"stat": {"exists": False}},
            "cloudfall_deploy_unit_existing_content": {"skipped": True},
        }
        if content is None
        else {
            "cloudfall_deploy_unit_existing": {"stat": {"exists": True, "isreg": True}},
            "cloudfall_deploy_unit_existing_content": {
                "content": base64.b64encode(content.encode()).decode()
            },
        }
    )
    return _templar(variables).template(
        trust_as_template(expression["cloudfall_deploy_unit_managed_before"])
    )


def test_a_unit_rendered_by_cloudfall_is_the_previous_releases_unit() -> None:
    assert _managed_before(MANAGED_UNIT) is True
    assert _managed_before(HAND_MADE_UNIT) is False
    assert _managed_before(None) is False


def test_a_failed_release_puts_back_the_unit_of_the_release_it_restores() -> None:
    assert _rescue(f"{RELEASES}/{PREVIOUS}", managed_before=True) == {
        RECORD_FAILED,
        RESTORE,
        RESTORE_MANAGED_UNIT,
        RESTART_RESTORED,
        GATE_RESTORED,
        FAIL,
    }
    names = [str(task["name"]) for task in _section("rescue")]
    assert names.index(RESTORE) < names.index(RESTORE_MANAGED_UNIT)
    assert names.index(RESTORE_MANAGED_UNIT) < names.index(RESTART_RESTORED)
    restart = _task(RESTART_RESTORED, _section("rescue"))
    service = restart["ansible.builtin.systemd_service"]
    assert isinstance(service, dict)
    assert service["daemon_reload"] is True


def test_the_unit_put_back_is_the_one_read_before_the_install() -> None:
    copy = _task(RESTORE_MANAGED_UNIT, _section("rescue"))["ansible.builtin.copy"]
    assert isinstance(copy, dict)
    variables: dict[str, object] = {
        **_rescue_variables(f"{RELEASES}/{PREVIOUS}", managed_before=True),
        "cloudfall_deploy_unit_existing_content": {
            "content": base64.b64encode(MANAGED_UNIT.encode()).decode()
        },
    }
    names = [str(task["name"]) for task in _main()]

    content = _templar(variables).template(trust_as_template(copy["content"]))

    assert content == MANAGED_UNIT
    assert copy["dest"] == "{{ cloudfall_deploy_unit_path }}"
    assert (copy["owner"], copy["group"], copy["mode"]) == ("root", "root", "0644")
    assert names.index(RECOGNIZE) < names.index(INSTALL)


def test_check_mode_never_puts_a_unit_back() -> None:
    restored = _rescue(f"{RELEASES}/{PREVIOUS}", managed_before=True, check_mode=True)

    assert RESTORE_MANAGED_UNIT not in restored
    assert GATE_RESTORED not in restored


def test_the_restored_release_is_gated_after_its_restart_and_receipt() -> None:
    gate = _task(GATE_RESTORED, _section("rescue"))
    block = gate["block"]
    assert isinstance(block, list)
    names = [str(task["name"]) for task in _section("rescue")]

    assert [task["ansible.builtin.include_tasks"] for task in block] == [
        "health_gate.yml"
    ]
    assert names.index(RESTART_RESTORED) < names.index(GATE_RESTORED)
    assert names.index(
        "Receipt the release that failed its command health check"
    ) < names.index(GATE_RESTORED)
    assert names.index(GATE_RESTORED) < names.index(FAIL)


def test_a_restored_release_that_fails_its_gate_is_its_own_failure() -> None:
    rescue = _task(GATE_RESTORED, _section("rescue"))["rescue"]
    assert isinstance(rescue, list)
    fail = _task(FAIL_ROLLBACK, rescue)["ansible.builtin.fail"]
    assert isinstance(fail, dict)
    variables: dict[str, object] = {
        **_rescue_variables(f"{RELEASES}/{PREVIOUS}.redeploy-20261101T000000Z"),
        "ansible_failed_result": {"msg": "Status code was -1 and not [200]"},
    }

    message = str(_templar(variables).template(trust_as_template(fail["msg"])))

    assert message == (
        f"Release r2 failed its health gate on h1 and was rolled back to {PREVIOUS},"
        " but the restored release failed its health check too, so asrest has no"
        " healthy release there: Status code was -1 and not [200]"
    )


def test_the_failure_names_the_failed_release_not_the_restored_releases_check() -> None:
    """The restored release's command check overwrites the shared result."""
    fail = _task(FAIL, _section("rescue"))["ansible.builtin.fail"]
    assert isinstance(fail, dict)
    variables: dict[str, object] = {
        **_rescue_variables(f"{RELEASES}/{PREVIOUS}"),
        "cloudfall_deploy_failed_health_result": {
            "attempts": 1,
            "exitCode": 3,
            "timedOut": False,
        },
        "cloudfall_deploy_command_health_result": {
            "attempts": 1,
            "exitCode": 0,
            "timedOut": False,
        },
        "cloudfall_deploy_component": {
            "id": "asrest",
            "service": {"name": "asrest"},
            "healthCheck": {"type": "command", "timeoutSeconds": 60},
        },
    }
    record = _task(RECORD_FAILED, _section("rescue"))["ansible.builtin.set_fact"]
    assert isinstance(record, dict)

    message = str(_templar(variables).template(trust_as_template(fail["msg"])))

    assert message == (
        "Release r2 failed the command health check of asrest on h1 (exit code 3"
        f" on its only attempt) and was rolled back to {PREVIOUS}"
    )
    assert _templar(
        {"cloudfall_deploy_command_health_result": {"exitCode": 3}}
    ).template(trust_as_template(record["cloudfall_deploy_failed_health_result"])) == {
        "exitCode": 3
    }
    assert (
        _templar({}).template(
            trust_as_template(record["cloudfall_deploy_failed_health_result"])
        )
        is None
    )


COMMAND_CHECK: dict[str, object] = {
    "type": "command",
    "command": [".venv/bin/python", "-m", "app.smoke"],
    "timeoutSeconds": 60,
    "attempts": 3,
}
REFUSE_COMMAND = "Refuse a command health check this engine does not run yet"


def test_s009_2_a_deploy_no_longer_refuses_a_command_check() -> None:
    assert REFUSE_COMMAND not in [str(task["name"]) for task in _main()]


@pytest.mark.parametrize("entry_point", ["restart.yml", "rollback.yml", "health.yml"])
def test_s009_10_no_entry_point_refuses_a_command_check(entry_point: str) -> None:
    tasks = _load(ROLE / "tasks" / entry_point)
    assert isinstance(tasks, list)
    assert REFUSE_COMMAND not in [str(task["name"]) for task in tasks]


@pytest.mark.parametrize("health_check", [HTTP_CHECK, NO_CHECK, COMMAND_CHECK])
def test_s009_1_the_role_argument_spec_accepts_every_declared_check(
    health_check: dict[str, object],
) -> None:
    specs = _load(ROLE / "meta" / "argument_specs.yml")
    assert isinstance(specs, dict)
    component_spec = specs["argument_specs"]["main"]["options"][
        "cloudfall_deploy_component"
    ]["options"]["healthCheck"]
    validator = ArgumentSpecValidator(component_spec["options"])

    result = validator.validate(dict(health_check))

    assert result.error_messages == []
