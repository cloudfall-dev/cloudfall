"""Playbook runs under the engine configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cloudfall_engine.cli import app, main
from cloudfall_engine.playbook import (
    PlaybookError,
    PlaybookRun,
    bundled_playbooks,
    playbook_command,
    resolve_playbook,
)

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"


@pytest.fixture
def inventory_file(tmp_path: Path) -> Path:
    state = validate_config(EXAMPLES, SCHEMAS)
    rendered = render_ansible_inventory(PlatformInventory.from_state(state))
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(rendered), encoding="utf-8")
    return path


def test_bundled_playbooks_are_listed_by_name() -> None:
    names = bundled_playbooks(ENGINE)
    assert "inspect" in names
    assert "baseline" in names
    assert names == tuple(sorted(names))


def test_bare_name_resolves_inside_the_engine() -> None:
    expected = ENGINE / "ansible" / "playbooks" / "inspect.yml"
    assert resolve_playbook("inspect", ENGINE) == expected
    assert resolve_playbook("inspect.yml", ENGINE) == expected


def test_path_reference_resolves_outside_the_engine(tmp_path: Path) -> None:
    fleet = tmp_path / "fleet.yml"
    fleet.write_text("- hosts: all\n", encoding="utf-8")
    assert resolve_playbook(str(fleet), ENGINE) == fleet


def test_missing_playbook_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PlaybookError, match="playbook does not exist"):
        resolve_playbook("nope", ENGINE)
    with pytest.raises(PlaybookError, match="playbook does not exist"):
        resolve_playbook(str(tmp_path / "gone.yml"), ENGINE)


def test_run_requires_inventory_and_roles(tmp_path: Path) -> None:
    playbook = resolve_playbook("inspect", ENGINE)
    with pytest.raises(PlaybookError, match="inventory file does not exist"):
        PlaybookRun(playbook=playbook, inventory_file=tmp_path / "missing")
    inventory = tmp_path / "inventory.json"
    inventory.write_text("{}", encoding="utf-8")
    with pytest.raises(PlaybookError, match="role directory does not exist"):
        PlaybookRun(
            playbook=playbook,
            inventory_file=inventory,
            role_directories=(tmp_path / "roles",),
        )


def test_command_carries_engine_config_and_role_path(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.json"
    inventory.write_text("{}", encoding="utf-8")
    fleet_roles = tmp_path / "roles"
    fleet_roles.mkdir()
    run = PlaybookRun(
        playbook=resolve_playbook("time", ENGINE),
        inventory_file=inventory,
        role_directories=(fleet_roles,),
        extra_vars=("a=1", '{"b": 2}'),
        tags=("certbot", "nginx"),
        limit="cloudfall_servers",
        check=True,
        diff=True,
    )
    command = playbook_command(run, ENGINE)
    assert command.argv[0].endswith("ansible-playbook")
    assert command.argv[1:] == (
        "--inventory",
        str(inventory),
        "--check",
        "--diff",
        "--limit",
        "cloudfall_servers",
        "--extra-vars",
        "a=1",
        "--extra-vars",
        '{"b": 2}',
        "--tags",
        "certbot,nginx",
        str(ENGINE / "ansible" / "playbooks" / "time.yml"),
    )
    ansible = (ENGINE / "ansible").resolve()
    assert command.environment["ANSIBLE_CONFIG"] == str(ansible / "ansible.cfg")
    assert command.environment["ANSIBLE_ROLES_PATH"].split(":") == [
        str(fleet_roles.resolve()),
        str(ansible / "roles"),
    ]


def test_cli_lists_bundled_playbooks(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["playbook", "list", "--engine", str(ENGINE)]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["engine"] == str(ENGINE)
    assert "inspect" in data["playbooks"]


def test_cli_syntax_checks_a_bundled_playbook(
    inventory_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "playbook",
            "run",
            "inspect",
            "--engine",
            str(ENGINE),
            "--inventory",
            str(inventory_file),
            "--syntax-check",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["data"]["effect"] == "noop"


def test_cli_reports_unknown_playbook_before_running(
    inventory_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "playbook",
            "run",
            "nope",
            "--engine",
            str(ENGINE),
            "--inventory",
            str(inventory_file),
        ]
    )

    error = json.loads(capsys.readouterr().out)["error"]
    assert exit_code == 2
    assert error["phase"] == "validation"
    assert error["context"]["code"] == "playbook_missing"


def test_a_usage_error_is_a_json_document(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["playbook", "list", "--bogus"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(captured.out)["error"]["code"] == "ARG_ERROR"


def test_check_mode_is_the_dry_run_flag(
    inventory_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ansible's --check is spelled --dry-run, like every treaty command."""
    exit_code = main(
        [
            "playbook",
            "run",
            "inspect",
            "--engine",
            str(ENGINE),
            "--inventory",
            str(inventory_file),
            "--check",
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "ARG_ERROR"


def test_every_engine_failure_code_is_declared() -> None:
    commands = cast("dict[str, dict[str, Any]]", app.manifest()["commands"])
    declared = {
        int(number): entry["name"]
        for command in commands.values()
        for number, entry in command["exit_codes"].items()
    }

    assert {declared[n] for n in (79, 80, 81, 82)} == {
        "PROJECT_INVALID",
        "CONFIG_INVALID",
        "ARTIFACT_BUILD_FAILED",
        "PLAYBOOK_FAILED",
    }
