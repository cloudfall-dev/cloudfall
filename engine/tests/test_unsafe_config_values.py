"""A declared config string reaches Ansible as data, never as a template.

Each test declares a value holding a Jinja expression that would touch a
marker file on the management host, renders the inventory the way a run
does (``cloudfall-engine inventory render``), and runs the play that reads
the value under ``ansible-playbook`` against local hosts. The marker must
never appear, and the text must reach its target exactly as declared.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import cast

import yaml
from cloudfall.inventory import PlatformInventory
from cloudfall.observe import write_overlay
from cloudfall.validation import validate_config
from cloudfall_engine.cli import main

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"
PLAYBOOKS = ENGINE / "ansible" / "playbooks"
UNIT_TEMPLATE = (
    ENGINE / "ansible" / "roles" / "cloudfall_deploy" / "templates"
) / "component.service.j2"
HONEST = "docker inspect --format {{.State.Health.Status}} app"
FAKE_SYSTEMD_RUN = """#!/bin/sh
printf '%s\\n' "$@" > "$0.argv"
"""


def _payload(marker: Path) -> str:
    return "{{ lookup('pipe', 'touch " + str(marker) + "') }}"


def _project(tmp_path: Path, spec: dict[str, object]) -> Path:
    """Copy the example project with ``spec`` merged into ``crm-backend``'s."""
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    path = project / "components" / "crm-backend.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["spec"].update(spec)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return project


def _local(hosts: dict[str, dict[str, object]]) -> None:
    """Point every host at this machine, as the engine's own play tests do."""
    for host in hosts.values():
        host.update(
            {
                "ansible_connection": "local",
                "ansible_python_interpreter": sys.executable,
                "ansible_become": False,
            }
        )


def _rendered_inventory(tmp_path: Path, spec: dict[str, object]) -> Path:
    """Render the inventory as a run does, then point its hosts at this machine."""
    inventory = tmp_path / "inventory.json"
    exit_code = main(
        [
            "inventory",
            "render",
            "--project",
            str(_project(tmp_path, spec)),
            "--schemas",
            str(SCHEMAS),
            "--output",
            str(inventory),
        ]
    )
    assert exit_code == 0
    document = json.loads(inventory.read_text(encoding="utf-8"))
    hosts = document["all"]["children"]["cloudfall_servers"]["hosts"]
    _local(hosts)
    inventory.write_text(json.dumps(document), encoding="utf-8")
    return inventory


def _play(tmp_path: Path, tasks: list[dict[str, object]]) -> Path:
    """One play that hands the selected component over as the playbooks do."""
    playbook = tmp_path / "probe.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Read the selected component as the playbooks do",
                    "hosts": "h1",
                    "gather_facts": False,
                    "tasks": [
                        {
                            "name": "Hand the component over",
                            "ansible.builtin.include_tasks": str(
                                _component_tasks(tmp_path, tasks)
                            ),
                            "vars": {
                                "cloudfall_deploy_component": (
                                    "{{ cloudfall_component_item }}"
                                )
                            },
                            "loop": (
                                "{{ cloudfall_components | default([])"
                                " | selectattr('id', 'equalto', 'crm-backend')"
                                " | list }}"
                            ),
                            "loop_control": {"loop_var": "cloudfall_component_item"},
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    return playbook


def _component_tasks(tmp_path: Path, tasks: list[dict[str, object]]) -> Path:
    path = tmp_path / "component-tasks.yml"
    path.write_text(yaml.safe_dump(tasks), encoding="utf-8")
    return path


def _run(playbook: Path, inventory: Path, extra_vars: dict[str, object]) -> int:
    return main(
        [
            "playbook",
            "run",
            str(playbook),
            "--engine",
            str(ENGINE),
            "--inventory",
            str(inventory),
            "--extra-vars",
            json.dumps(extra_vars),
        ]
    )


def test_a_health_check_command_is_not_templated_by_the_health_play(
    tmp_path: Path,
) -> None:
    """``health.yml`` validates the role's arguments without running the value."""
    marker = tmp_path / "pwned"
    inventory = _rendered_inventory(
        tmp_path,
        {
            "healthCheck": {
                "type": "command",
                "command": ["/bin/true", _payload(marker)],
                "timeoutSeconds": 5,
                "attempts": 1,
            }
        },
    )

    exit_code = main(
        [
            "playbook",
            "run",
            "health",
            "--engine",
            str(ENGINE),
            "--inventory",
            str(inventory),
            "--extra-vars",
            json.dumps({"cloudfall_deploy_component_id": "crm-backend"}),
        ]
    )

    # The role refuses a command health check until #164 runs it.
    assert exit_code != 0
    assert not marker.exists()


def test_a_health_check_command_reaches_the_check_as_declared(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "pwned"
    declared = ["/bin/sh", "-c", HONEST, _payload(marker)]
    inventory = _rendered_inventory(
        tmp_path,
        {
            "healthCheck": {
                "type": "command",
                "command": declared,
                "timeoutSeconds": 5,
                "attempts": 1,
            }
        },
    )
    seen = tmp_path / "health-command.json"
    playbook = _play(
        tmp_path,
        [
            {
                "name": "Keep the health check command",
                "ansible.builtin.copy": {
                    "content": (
                        "{{ cloudfall_deploy_component.healthCheck.command | to_json }}"
                    ),
                    "dest": str(seen),
                    "mode": "0644",
                },
            }
        ],
    )

    assert _run(playbook, inventory, {}) == 0
    assert not marker.exists()
    assert json.loads(seen.read_text(encoding="utf-8")) == declared


def test_a_service_command_reaches_the_unit_file_as_declared(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "pwned"
    inventory = _rendered_inventory(
        tmp_path,
        {
            "service": {
                "manager": "systemd",
                "name": "crm-backend",
                "command": [".venv/bin/app", _payload(marker), "{{.Status}}"],
            }
        },
    )
    unit = tmp_path / "crm-backend.service"
    playbook = _play(
        tmp_path,
        [
            {
                "name": "Render the role's unit template",
                "ansible.builtin.template": {
                    "src": str(UNIT_TEMPLATE),
                    "dest": str(unit),
                    "mode": "0644",
                },
            }
        ],
    )

    assert _run(playbook, inventory, {}) == 0
    assert not marker.exists()
    (exec_start,) = [
        line
        for line in unit.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStart=")
    ]
    assert exec_start == (
        "ExecStart=/srv/apps/crm/backend/current/.venv/bin/app"
        f' "{_payload(marker)}" "{{{{.Status}}}}"'
    )


def test_a_release_command_reaches_its_run_as_declared(tmp_path: Path) -> None:
    """The role's release command tasks run the argv as written."""
    marker = tmp_path / "pwned"
    inventory = _rendered_inventory(
        tmp_path,
        {"release": {"command": ["/bin/echo", _payload(marker)]}},
    )
    systemd_run = tmp_path / "systemd-run"
    systemd_run.write_text(FAKE_SYSTEMD_RUN, encoding="utf-8")
    systemd_run.chmod(0o755)
    playbook = _play(
        tmp_path,
        [
            {
                "name": "Run the role's release command tasks",
                "ansible.builtin.include_role": {
                    "name": "cloudfall_deploy",
                    "tasks_from": "release_command",
                },
                "vars": {
                    "cloudfall_deploy_release": "20261027T000000Z-abcdef1",
                    "cloudfall_deploy_release_directory": str(tmp_path / "release"),
                    "cloudfall_deploy_shared_directory": str(tmp_path / "shared"),
                    "cloudfall_deploy_systemd_run": str(systemd_run),
                },
            }
        ],
    )

    assert _run(playbook, inventory, {}) == 0
    assert not marker.exists()
    argv = (tmp_path / "systemd-run.argv").read_text(encoding="utf-8").splitlines()
    assert argv[-2:] == ["/bin/echo", _payload(marker)]


def test_a_package_version_in_the_observe_overlay_is_not_templated(
    tmp_path: Path,
) -> None:
    """The overlay ``cloudfall observe`` writes hands its strings over as data."""
    marker = tmp_path / "pwned"
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    path = project / "server-types" / "debian-application.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["spec"]["packages"]["required"][0]["version"] = _payload(marker)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    overlay = json.loads(
        write_overlay(inventory, tmp_path / "overlay").read_text(encoding="utf-8")
    )
    hosts = cast(
        "dict[str, dict[str, object]]",
        overlay["all"]["children"]["cloudfall_servers"]["hosts"],
    )
    _local(hosts)
    combined = tmp_path / "inventory.json"
    combined.write_text(json.dumps(overlay), encoding="utf-8")
    seen = tmp_path / "server-type.json"
    playbook = tmp_path / "probe.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Read the observed server type",
                    "hosts": "h1",
                    "gather_facts": False,
                    "tasks": [
                        {
                            "name": "Keep the server type",
                            "ansible.builtin.copy": {
                                "content": "{{ cloudfall_server_type | to_json }}",
                                "dest": str(seen),
                                "mode": "0644",
                            },
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    assert _run(playbook, combined, {}) == 0
    assert not marker.exists()
    server_type = json.loads(seen.read_text(encoding="utf-8"))
    assert server_type["packages"]["required"][0]["version"] == _payload(marker)
