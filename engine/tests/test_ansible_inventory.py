"""Ansible inventory rendering acceptance tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cloudfall_engine.cli import main

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"


def _rendered_inventory() -> dict[str, object]:
    state = validate_config(EXAMPLES, SCHEMAS)
    return render_ansible_inventory(PlatformInventory.from_state(state))


def _mapping_entry(mapping: dict[str, object], key: str) -> dict[str, object]:
    value = mapping[key]
    assert isinstance(value, dict)
    return value


def _inventory_children(rendered: dict[str, object]) -> dict[str, object]:
    all_group = _mapping_entry(rendered, "all")
    return _mapping_entry(all_group, "children")


def test_inventory_renders_ansible_host_variables() -> None:
    children = _inventory_children(_rendered_inventory())
    servers_group = _mapping_entry(children, "cloudfall_servers")
    hostvars = _mapping_entry(servers_group, "hosts")
    h1 = dict(_mapping_entry(hostvars, "h1"))
    logging_backend = _mapping_entry(h1, "cloudfall_logging_backend")
    logging_collector = _mapping_entry(h1, "cloudfall_logging_collector")
    ssh_public_keys = h1["cloudfall_ssh_public_keys"]
    origin_domains = h1["cloudfall_origin_domains"]
    alert_rules = h1["cloudfall_alert_rules"]
    del h1["cloudfall_logging_backend"]
    del h1["cloudfall_logging_collector"]
    del h1["cloudfall_ssh_public_keys"]
    del h1["cloudfall_origin_domains"]
    del h1["cloudfall_alert_rules"]

    assert isinstance(alert_rules, list)
    assert len(alert_rules) == 1
    assert isinstance(alert_rules[0], dict)
    assert alert_rules[0] == {
        "id": "postgresql-down",
        "environment": "production",
        "expr": 'pg_up{service="postgresql-main"} == 0',
        "for": "1m",
        "severity": "critical",
        "summary": "PostgreSQL postgresql-main is not answering its exporter",
    }

    assert isinstance(origin_domains, list)
    assert len(origin_domains) == 1
    assert isinstance(origin_domains[0], dict)
    assert origin_domains[0]["id"] == "crm-site"
    assert origin_domains[0]["origin"]["server"] == "h1"

    assert isinstance(ssh_public_keys, list)
    assert len(ssh_public_keys) == 1
    assert isinstance(ssh_public_keys[0], dict)
    assert ssh_public_keys[0]["id"] == "example-admin"
    assert ssh_public_keys[0]["algorithm"] == "ssh-rsa"
    assert ssh_public_keys[0]["lifecycle"] == "active"

    assert h1 == {
        "ansible_host": "h1.example.internal",
        "ansible_port": 22,
        "ansible_user": "cloudfall",
        "cloudfall_components": [
            {
                "id": "crm-backend",
                "install_root": "/srv/apps/crm/backend",
                "application": "crm",
                "application_user": "crm",
                "repository": {
                    "url": "https://github.com/example/crm-backend.git",
                },
                "runtime": {
                    "type": "python",
                    "version": "3.14",
                    "packageManager": "uv",
                },
                "service": {
                    "manager": "systemd",
                    "name": "crm-backend",
                    "command": [
                        ".venv/bin/uvicorn",
                        "crm.asgi:application",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8100",
                    ],
                },
                "healthCheck": {
                    "type": "http",
                    "scheme": "http",
                    "port": 8100,
                    "path": "/health",
                    "expectedStatuses": [200],
                    "timeoutSeconds": 5,
                    "attempts": 5,
                },
                "retainUntilCleanup": True,
            }
        ],
        "cloudfall_services": [
            {
                "id": "postgresql-main",
                "serviceKind": "postgresql",
                "environment": "production",
                "server": "h1",
                "bind": {"address": "127.0.0.1", "port": 5432},
                "postgresql": {
                    "majorVersion": "17",
                    "databases": [
                        {"name": "crm", "application": "crm", "owner": "crm"}
                    ],
                },
                "backup": {
                    "directory": "/var/backups/cloudfall/postgresql-main",
                    "onCalendar": "*-*-* 02:00:00 UTC",
                    "restoreCheckOnCalendar": "*-*-* 05:00:00 UTC",
                    "retentionDays": 14,
                },
                "metrics": {"enabled": True},
            },
            {
                "id": "redis-cache",
                "serviceKind": "redis",
                "environment": "production",
                "server": "h1",
                "bind": {"address": "127.0.0.1", "port": 6379},
                "redis": {"maxmemoryMb": 256, "appendOnly": True},
                "backup": {
                    "directory": "/var/backups/cloudfall/redis-cache",
                    "onCalendar": "*-*-* 03:00:00 UTC",
                    "restoreCheckOnCalendar": "*-*-* 05:30:00 UTC",
                    "retentionDays": 7,
                },
                "metrics": {"enabled": True},
            }
        ],
        "cloudfall_domains": [],
        "cloudfall_environment": "production",
        "cloudfall_firewall": {
            "policy": "default-deny",
            "allowedInbound": [
                {"port": 22, "protocol": "tcp", "description": "ssh"},
                {"port": 80, "protocol": "tcp", "description": "http"},
                {"port": 443, "protocol": "tcp", "description": "https"},
            ],
        },
        "cloudfall_server_type": {
            "configuration": {
                "files": [
                    {
                        "capture": "hash",
                        "group": "root",
                        "mode": "0644",
                        "owner": "root",
                        "path": "/etc/ssh/sshd_config",
                    }
                ]
            },
            "id": "debian-application",
            "os": {
                "distribution": "Debian",
                "serviceManager": "systemd",
                "versions": ["12", "13"],
            },
            "packages": {
                "forbidden": ["telnetd"],
                "required": [
                    {"name": "acl"},
                    {"name": "ca-certificates"},
                    {"name": "curl"},
                    {"name": "git"},
                    {"name": "rsync"},
                    {"name": "unzip"},
                ],
            },
            "services": {
                "required": [
                    {
                        "name": "ssh.service",
                        "state": "running",
                        "status": "enabled",
                    },
                    {
                        "name": "apt-daily.timer",
                        "state": "running",
                        "status": "enabled",
                    },
                ]
            },
            "firewall": {
                "policy": "default-deny",
                "allowedInbound": [
                    {"port": 80, "protocol": "tcp", "description": "http"},
                    {"port": 443, "protocol": "tcp", "description": "https"},
                ],
            },
            "storage": {
                "mounts": [
                    {
                        "filesystem": "ext4",
                        "minimumBytes": 850000000000,
                        "path": "/",
                    }
                ],
                "softwareRaid": {
                    "level": "raid1",
                    "minimumActiveDevices": 2,
                    "minimumUsableBytes": 900000000000,
                },
            },
        },
        "cloudfall_hostname": "h1.example.internal",
        "cloudfall_labels": {
            "region": "eu-central",
            "workload": "application",
        },
        "cloudfall_lifecycle": "active",
        "cloudfall_applications": [{"approval": "manual", "id": "crm", "user": "crm"}],
        "cloudfall_server_id": "h1",
    }
    assert logging_backend["id"] == "operations"
    assert _mapping_entry(logging_backend, "backend")["server"] == "h1"
    assert _mapping_entry(logging_collector, "collectors")["servers"] == [
        "h1",
        "h2",
    ]
    assert _mapping_entry(logging_collector, "migration") == {
        "mode": "parallel",
        "preserveLegacyAgents": True,
    }


def test_inventory_renders_environment_application_and_component_groups() -> None:
    children = _inventory_children(_rendered_inventory())
    environments = _mapping_entry(children, "cloudfall_environments")
    environment_groups = _mapping_entry(environments, "children")
    applications = _mapping_entry(children, "cloudfall_applications")
    application_groups = _mapping_entry(applications, "children")
    components = _mapping_entry(children, "cloudfall_components")
    component_groups = _mapping_entry(components, "children")
    logging_backends = _mapping_entry(children, "cloudfall_logging_backends")
    logging_collectors = _mapping_entry(children, "cloudfall_logging_collectors")
    domain_origins = _mapping_entry(children, "cloudfall_domain_origins")

    assert environment_groups["environment_production"] == {
        "hosts": {"h1": {}, "h2": {}},
    }
    assert application_groups["application_crm"] == {
        "hosts": {"h1": {}, "h2": {}},
    }
    assert component_groups["component_crm_backend"] == {
        "hosts": {"h1": {}, "h2": {}},
    }
    assert logging_backends == {"hosts": {"h1": {}}}
    assert logging_collectors == {"hosts": {"h1": {}, "h2": {}}}
    assert domain_origins == {"hosts": {"h1": {}}}
    assert children["ungrouped"] == {"hosts": {}}


def test_engine_cli_emits_ansible_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "inventory",
            "render",
            "--project",
            str(EXAMPLES),
            "--schemas",
            str(SCHEMAS),
        ]
    )

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert document["ok"] is True
    assert document["data"]["output"] is None
    inventory = document["data"]["inventory"]
    assert (
        inventory["all"]["children"]["cloudfall_servers"]["hosts"]["h1"]["ansible_user"]
        == "cloudfall"
    )
    assert (
        "component_crm_backend"
        in inventory["all"]["children"]["cloudfall_components"]["children"]
    )


def test_engine_cli_can_write_inventory_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "inventory.json"
    exit_code = main(
        [
            "inventory",
            "render",
            "--project",
            str(EXAMPLES),
            "--schemas",
            str(SCHEMAS),
            "--output",
            str(output_path),
        ]
    )

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert document["data"]["output"] == str(output_path)
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written == document["data"]["inventory"]
    assert sorted(written["all"]["children"]["cloudfall_servers"]["hosts"]) == [
        "h1",
        "h2",
    ]


def test_engine_cli_writes_a_relative_output_inside_the_project(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    exit_code = main(
        [
            "inventory",
            "render",
            "--project",
            str(project),
            "--schemas",
            str(SCHEMAS),
            "--output",
            "tmp/inventory.json",
        ]
    )

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert document["data"]["output"] == str(project / "tmp" / "inventory.json")
    assert (project / "tmp" / "inventory.json").is_file()


def test_engine_cli_names_a_missing_project(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["inventory", "render", "--project", str(tmp_path / "gone")])

    error = json.loads(capsys.readouterr().out)["error"]
    assert exit_code == 79
    assert error["code"] == "PROJECT_INVALID"
    assert error["context"]["code"] == "project_directory_missing"
