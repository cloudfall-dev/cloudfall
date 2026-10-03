"""``cloudfall mcp serve``, over its real stdio transport."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import TextContent

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
SERVE = ("-c", "from cloudfall.cli import run; run()", "mcp", "serve")

HOSTS = """\
---
all:
  children:
    app_servers:
      hosts:
        web-1:
          ansible_host: 127.0.0.1
"""

GROUP_VARS = """\
---
ansible_user: ansible
cloudfall_server_types:
  web:
    os:
      distribution: Debian
      versions:
        - '13'
      service_manager: systemd
    storage:
      mounts:
        - path: /
          filesystem: ext4
          minimum_bytes: 20000000000
    packages:
      required:
        - name: curl
      forbidden:
        - telnetd
    services:
      required:
        - name: ssh.service
          state: running
          status: enabled
    configuration:
      files:
        - path: /etc/ssh/sshd_config
          capture: hash
"""

WEB_1 = """\
---
cloudfall:
  environment: production
  server_type: web
  lifecycle: active
"""

MARKER = """\
id: write-marker
description: Write the release marker
playbook: playbooks/marker.yml
risk: mutating
targets: host
inputs:
  version: string
verify:
  playbook: playbooks/marker.yml
"""

PURGE = """\
id: purge
description: Remove what nothing points at
playbook: playbooks/marker.yml
risk: destructive
targets: fleet
verify:
  playbook: playbooks/marker.yml
"""

PLAYBOOK = """\
---
- name: Write the marker
  hosts: all
  connection: local
  gather_facts: false
  tasks:
    - name: Place the marker
      ansible.builtin.copy:
        content: "release {{ version | default('none') }}\\n"
        dest: "{{ playbook_dir }}/../marker.txt"
        mode: "0600"
"""


def _repository(tmp_path: Path, **operations: str) -> Path:
    repository = tmp_path / "fleet-ansible"
    inventory = repository / "inventories" / "production"
    (inventory / "group_vars" / "all").mkdir(parents=True)
    (inventory / "host_vars").mkdir(parents=True)
    (inventory / "hosts.yml").write_text(HOSTS, encoding="utf-8")
    (inventory / "group_vars" / "all" / "main.yml").write_text(
        GROUP_VARS, encoding="utf-8"
    )
    (inventory / "host_vars" / "web-1.yml").write_text(WEB_1, encoding="utf-8")
    (repository / "ansible.cfg").write_text(
        textwrap.dedent("""\
            [defaults]
            inventory = inventories/production
            retry_files_enabled = False
            interpreter_python = auto_silent
            """),
        encoding="utf-8",
    )
    (repository / "playbooks").mkdir()
    (repository / "playbooks" / "marker.yml").write_text(PLAYBOOK, encoding="utf-8")
    declared = operations or {"write-marker": MARKER}
    (repository / "operations").mkdir()
    for name, body in declared.items():
        (repository / "operations" / f"{name}.yml").write_text(body, encoding="utf-8")
    return repository


def _serve(*flags: str, cwd: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=[*SERVE, *flags], cwd=str(cwd)
    )


async def _session_calls(
    server: StdioServerParameters, calls: list[tuple[str, dict[str, Any]]]
) -> tuple[list[Any], list[dict[str, Any]]]:
    async with (
        stdio_client(server) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = (await session.list_tools()).tools
        answers = []
        for name, arguments in calls:
            result = await session.call_tool(name, arguments)
            content = result.content[0]
            assert isinstance(content, TextContent)
            answers.append(json.loads(content.text))
        return tools, answers


def _talk(
    server: StdioServerParameters, *calls: tuple[str, dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tools, answers = asyncio.run(_session_calls(server, list(calls)))
    return {tool.name: tool for tool in tools}, answers


def test_a_repository_serves_its_catalog_and_no_approval(tmp_path: Path) -> None:
    repository = _repository(tmp_path, **{"write-marker": MARKER, "purge": PURGE})

    tools, _ = _talk(_serve("--repository", str(repository), cwd=tmp_path))

    assert set(tools) == {
        "audit",
        "inventory_show",
        "observe",
        "operations_decisions",
        "operations_list",
        "operations_show",
        "why",
        "operation_write_marker",
        "operation_purge",
    }
    marker = tools["operation_write_marker"].annotations
    purge = tools["operation_purge"].annotations
    assert (marker.read_only_hint, marker.destructive_hint) == (True, False)
    assert (purge.read_only_hint, purge.destructive_hint) == (True, True)
    assert "cloudfall operations approve" in tools["operation_write_marker"].description


def test_an_operation_tool_records_a_proposal_and_changes_nothing(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    _, (proposed, decisions, why) = _talk(
        _serve(cwd=repository),
        (
            "operation_write_marker",
            {"target": "web-1", "inputs": {"version": "2.0.0"}},
        ),
        ("operations_decisions", {}),
        ("why", {"operation": "write-marker"}),
    )

    assert proposed["ok"] is True, proposed
    decision = proposed["data"]["decision"]
    assert decision["spec"]["status"] == "proposed"
    assert decision["spec"]["inputs"] == {"version": "2.0.0"}
    assert proposed["data"]["next"] == [
        f"cloudfall operations approve {decision['metadata']['id']} --yes"
    ]
    assert not (repository / "marker.txt").exists()
    recorded = decisions["data"]["decisions"]
    assert [entry["spec"]["operation"]["id"] for entry in recorded] == ["write-marker"]
    assert why["ok"] is True


def test_a_call_cannot_reach_another_fleet_or_approve(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    other = _repository(tmp_path / "other")

    _, (redirected, approved) = _talk(
        _serve("--repository", str(repository), cwd=tmp_path),
        ("operations_list", {"repository": str(other)}),
        ("operations_approve", {"decision": "anything", "yes": True}),
    )

    assert redirected["ok"] is False
    assert redirected["error"]["code"] == "ARG_ERROR"
    assert approved["error"]["code"] == "UNKNOWN_TOOL"


def test_a_project_serves_its_commands_with_the_project_bound(tmp_path: Path) -> None:
    tools, (converge,) = _talk(
        _serve("--project", str(EXAMPLES), cwd=tmp_path),
        ("converge_baseline", {}),
    )

    assert {"deploy", "audit", "operator_approve", "build_artifact"} <= set(tools)
    assert not {
        "init",
        "operations_approve",
        "operator_run",
        "dashboard_serve",
        "cleanup",
    } & set(tools)
    for tool in tools.values():
        properties = tool.input_schema.get("properties", {})
        assert not {"project", "schemas", "engine", "inventory"} & set(properties)
    assert tools["converge_baseline"].annotations.destructive_hint is True
    assert converge["ok"] is False
    assert converge["error"]["code"] == "CONFIRMATION_REQUIRED"


def test_a_project_call_cannot_send_a_file_elsewhere_or_write_secrets_out(
    tmp_path: Path,
) -> None:
    # An agent that chose the Render API URL could send any controller file
    # it names as the bearer key to a host of its choosing; one that chose
    # the env file could write decrypted values outside the project; one
    # that chose the gateway URL could answer its own verification.
    secret = tmp_path / "outside.txt"
    secret.write_text("not-a-render-key\n", encoding="utf-8")

    tools, (sent, rendered, approved) = _talk(
        _serve("--project", str(EXAMPLES), cwd=tmp_path),
        (
            "import_render-api",
            {
                "api_key_file": str(secret),
                "api_url": "http://127.0.0.1:9",
                "application": "crm",
                "server": "h1",
            },
        ),
        ("secrets_render", {"component": "crm", "output_file": str(secret)}),
        ("operator_approve", {"proposal": "p", "gateway_url": "http://127.0.0.1:9"}),
    )

    assert "api_url" not in tools["import_render-api"].input_schema["properties"]
    assert "output_file" not in tools["secrets_render"].input_schema["properties"]
    assert "gateway_url" not in tools["operator_approve"].input_schema["properties"]
    for refused in (sent, rendered, approved):
        assert refused["ok"] is False
        assert refused["error"]["code"] == "ARG_ERROR"
    assert secret.read_text(encoding="utf-8") == "not-a-render-key\n"


def test_a_project_server_takes_its_secrets_directory(tmp_path: Path) -> None:
    done = subprocess.run(  # noqa: S603 - fixed interpreter and arguments.
        [
            sys.executable,
            *SERVE,
            "--project",
            str(EXAMPLES),
            "--secrets-dir",
            "private/secrets",
            "--list-tools",
        ],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    tools = {tool["name"] for tool in json.loads(done.stdout)["tools"]}
    assert "secrets_render" in tools


def test_a_project_call_cannot_write_outside_the_project(tmp_path: Path) -> None:
    # Snapshots, receipts, import output and the migrate plan are the
    # server's; an absolute path would land wherever the agent named.
    outside = tmp_path / "outside"
    written = {
        "observed",
        "service_observed",
        "releases",
        "backups",
        "data_migrations",
        "env_receipts",
        "output_dir",
        "env_dir",
        "plan_file",
        "receipts",
    }

    tools, (snapshot, imported) = _talk(
        _serve("--project", str(EXAMPLES), cwd=tmp_path),
        ("observe", {"observed": str(outside)}),
        (
            "import_render",
            {
                "blueprint": str(EXAMPLES / "render.yaml"),
                "application": "crm",
                "server": "h1",
                "env_dir": str(outside),
            },
        ),
    )

    for tool in tools.values():
        assert not written & set(tool.input_schema.get("properties", {})), tool.name
    for refused in (snapshot, imported):
        assert refused["error"]["code"] == "ARG_ERROR"
    assert not outside.exists()


def test_no_fleet_to_serve_fails_before_serving(tmp_path: Path) -> None:
    done = subprocess.run(  # noqa: S603 - fixed interpreter and arguments.
        [sys.executable, *SERVE],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    envelope = json.loads(done.stderr.splitlines()[-1])
    assert done.returncode == 79
    assert done.stdout == ""
    assert envelope["error"]["code"] == "PROJECT_INVALID"


@pytest.mark.parametrize("flags", [("--project", "x", "--repository", "y")])
def test_a_server_serves_one_fleet(flags: tuple[str, ...], tmp_path: Path) -> None:
    done = subprocess.run(  # noqa: S603 - fixed interpreter and arguments.
        [sys.executable, *SERVE, *flags],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 2
    assert "two fleets" in done.stderr


def test_a_repository_without_an_inventory_says_so(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "ansible.cfg").write_text("[defaults]\n", encoding="utf-8")

    done = subprocess.run(  # noqa: S603 - fixed interpreter and arguments.
        [sys.executable, *SERVE, "--repository", str(repository)],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    envelope = json.loads(done.stderr.splitlines()[-1])
    assert done.returncode == 85
    assert envelope["error"]["context"]["code"] == "fleet_inventory_undeclared"
