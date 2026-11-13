"""Deploy, rollback, restart and backup contact only the servers they act on.

Each playbook runs for real under ``ansible-playbook`` against two declared
servers: ``h2`` runs locally, and ``h1`` points at a closed local SSH port,
so contacting it makes it unreachable. ``h1`` comes first in inventory order,
so a play over every server would fail on it before it reached ``h2``. The
deploy role is replaced, through ``--roles``, by one that records each host.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cloudfall_engine.cli import main

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"
RELEASE = "20261027T000000Z-abcdef1"
STUB_TASKS = """---
- name: Record the host this entry point ran on
  ansible.builtin.lineinfile:
    path: "{{ stub_log }}"
    line: "{{ cloudfall_server_id }}"
    create: true
    mode: "0644"
  delegate_to: localhost
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
}


def _project(tmp_path: Path, *, release: bool) -> Path:
    """Copy the examples with crm-backend and postgresql-main on h2 only."""
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    component = project / "components" / "crm-backend.yaml"
    document = yaml.safe_load(component.read_text(encoding="utf-8"))
    document["spec"]["deployment"]["servers"] = ["h2"]
    if release:
        document["spec"]["release"] = {"command": ["bin/migrate"]}
    component.write_text(yaml.safe_dump(document), encoding="utf-8")
    service = project / "services" / "postgresql-main.yaml"
    document = yaml.safe_load(service.read_text(encoding="utf-8"))
    document["spec"]["server"] = "h2"
    service.write_text(yaml.safe_dump(document), encoding="utf-8")
    logging = project / "logging-stacks" / "operations.yaml"
    document = yaml.safe_load(logging.read_text(encoding="utf-8"))
    for file_source in document["spec"]["collectors"]["files"]:
        file_source["servers"] = ["h2"]
    logging.write_text(yaml.safe_dump(document), encoding="utf-8")
    return project


def _inventory(tmp_path: Path, *, release: bool) -> Path:
    state = validate_config(_project(tmp_path, release=release), SCHEMAS)
    rendered = render_ansible_inventory(PlatformInventory.from_state(state))
    hosts = rendered["all"]["children"]["cloudfall_servers"]["hosts"]  # type: ignore[index]
    assert list(hosts) == ["h1", "h2"]
    hosts["h1"].update(
        {
            "ansible_connection": "ssh",
            "ansible_host": "127.0.0.1",
            "ansible_port": 1,
            "ansible_ssh_common_args": "-o ConnectTimeout=5",
        }
    )
    hosts["h2"].update(
        {
            "ansible_connection": "local",
            "ansible_python_interpreter": sys.executable,
        }
    )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(rendered), encoding="utf-8")
    return inventory


def _roles(tmp_path: Path) -> Path:
    roles = tmp_path / "roles"
    tasks = roles / "cloudfall_deploy" / "tasks"
    tasks.mkdir(parents=True)
    for name in ("main.yml", "rollback.yml", "restart.yml"):
        (tasks / name).write_text(STUB_TASKS, encoding="utf-8")
    return roles


def _run(
    tmp_path: Path,
    playbook: str,
    extra_vars: dict[str, str],
    *,
    release: bool = False,
) -> int:
    return main(
        [
            *("playbook", "run", playbook),
            *("--engine", str(ENGINE)),
            *("--inventory", str(_inventory(tmp_path, release=release))),
            *("--roles", str(_roles(tmp_path))),
            "--extra-vars",
            json.dumps({**extra_vars, "ansible_become": False}),
        ]
    )


@pytest.mark.parametrize("playbook", ["deploy", "rollback", "restart"])
def test_an_unreachable_server_without_the_component_does_not_fail_it(
    playbook: str, tmp_path: Path
) -> None:
    log = tmp_path / "hosts.log"

    exit_code = _run(
        tmp_path, playbook, {**EXTRA_VARS[playbook], "stub_log": str(log)}
    )

    assert exit_code == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["h2"]


def test_a_release_server_deploys_once_without_the_unrelated_server(
    tmp_path: Path,
) -> None:
    """h2 is both the release server and the component's only server."""
    log = tmp_path / "hosts.log"

    exit_code = _run(
        tmp_path,
        "deploy",
        {**EXTRA_VARS["deploy"], "stub_log": str(log)},
        release=True,
    )

    assert exit_code == 0
    assert log.read_text(encoding="utf-8").splitlines() == ["h2"]


@pytest.mark.parametrize("playbook", ["deploy", "rollback", "restart"])
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


@pytest.mark.parametrize("playbook", ["deploy", "rollback", "restart"])
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
