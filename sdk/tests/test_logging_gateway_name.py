"""S-011 DNS provider: collectors resolve the gateway name to the backend."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cloudfall.inventory import PlatformInventory, logging_gateway_address
from cloudfall.validation import validate_config
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
TASKS = ROOT / "engine/ansible/roles/cloudfall_logging_collector/tasks/gateway_name.yml"


def _addresses(project: Path) -> dict[str, str | None]:
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    stack = inventory.logging_stacks[0]
    return {
        server.value: logging_gateway_address(stack, server, inventory.servers)
        for server in stack.collectors.server_ids
    }


def test_collectors_reach_the_gateway_on_loopback_or_the_backend_ipv4() -> None:
    assert _addresses(EXAMPLES) == {"h1": "127.0.0.1", "h2": "203.0.113.11"}


def test_a_backend_known_only_by_name_leaves_the_name_to_dns(tmp_path: Path) -> None:
    project = tmp_path / "config"
    copy_examples(project)
    h1 = project / "servers" / "h1.yaml"
    text = h1.read_text(encoding="utf-8")
    start = text.index("  network:\n")
    end = text.index("  ssh:\n")
    h1.write_text(text[:start] + text[end:], encoding="utf-8")

    assert _addresses(project)["h2"] is None
    assert _addresses(project)["h1"] == "127.0.0.1"


def test_the_role_pins_the_name_once_and_keeps_other_lines(tmp_path: Path) -> None:
    hosts = tmp_path / "hosts"
    hosts.write_text(
        "127.0.0.1 localhost\n"
        "10.9.9.9 logs.example.internal # stale\n"
        "10.1.1.1 logs.example.internal2\n",
        encoding="utf-8",
    )
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "cloudfall_logging_collector_hosts_file": str(hosts),
                        "cloudfall_logging_gateway_address": "203.0.113.11",
                        "cloudfall_logging_collector_stack": {
                            "gateway": {"serverName": "logs.example.internal"}
                        },
                    },
                    "tasks": [{"ansible.builtin.include_tasks": str(TASKS)}],
                }
            ]
        ),
        encoding="utf-8",
    )

    def run() -> str:
        done = subprocess.run(  # noqa: S603 - the venv's ansible-playbook
            [
                str(ANSIBLE_PLAYBOOK),
                "--inventory",
                "localhost,",
                "--connection",
                "local",
                str(playbook),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
        return done.stdout

    run()
    second = run()

    assert hosts.read_text(encoding="utf-8").splitlines() == [
        "127.0.0.1 localhost",
        "203.0.113.11 logs.example.internal # cloudfall logging gateway",
        "10.1.1.1 logs.example.internal2",
    ]
    assert "changed=0" in second
