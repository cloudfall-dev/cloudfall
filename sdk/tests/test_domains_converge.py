"""``domains.yml`` for one Domain: the SDK side of spec S-004.

The engine steps run through a recording runner, so no host is contacted:
these tests read the argv each step would run.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from cloudfall.cli import main
from cloudfall.domain import ResourceId
from cloudfall.lifecycle import (
    DomainsRun,
    EngineContext,
    ExecutionStep,
    LifecycleError,
    converge_domains,
)
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import TextContent

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"
SERVE = ("-c", "from cloudfall.cli import run; run()", "mcp", "serve")
SHA = "a" * 64

SECOND_DOMAIN = """\
---
apiVersion: cloudfall/v1
kind: Domain
metadata:
  id: crm-admin
  description: A second route on the same proxy server
spec:
  primaryName: admin.crm.example.test
  aliases: []
  proxy:
    server: h2
    configurationPath: /etc/nginx/sites-available/admin.crm.example.test.conf
    service: nginx.service
    upstream:
      address: 127.0.0.1
      port: 8101
  origin:
    server: h1
    configurationPath: /etc/nginx/sites-available/crm-admin-origin.conf
    service: nginx.service
    scheme: http
    port: 8101
    serverName: admin.crm.example.test
  edge:
    provider: cloudflare
    mode: dns-only
  tls:
    mode: required
  healthCheck:
    scheme: https
    path: /health
    expectedStatuses:
      - 200
    timeoutSeconds: 5
"""


@dataclass(frozen=True, slots=True)
class _Done:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(slots=True)
class _Recorder:
    """Answer each step with an exit code, keeping what it was asked to run."""

    playbook_exit: int = 0
    steps: list[ExecutionStep] = field(default_factory=list)

    def __call__(self, step: ExecutionStep) -> _Done:
        self.steps.append(step)
        if step.description.startswith("run "):
            return _Done(self.playbook_exit, stderr="nginx: [emerg] rejected")
        return _Done(0)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    (project / "domains" / "crm-admin.yaml").write_text(
        SECOND_DOMAIN, encoding="utf-8"
    )
    return project


def _context(project: Path, recorder: _Recorder) -> EngineContext:
    return EngineContext(
        project_directory=project,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=project / "tmp" / "inventory.json",
        run=recorder,
    )


def _playbook_argv(recorder: _Recorder) -> Sequence[str]:
    render, playbook = recorder.steps
    assert render.description == "render Ansible inventory"
    assert playbook.argv[-1].endswith("domains.yml")
    return playbook.argv


def _extra_vars(argv: Sequence[str]) -> dict[str, object]:
    extra_vars = json.loads(argv[list(argv).index("--extra-vars") + 1])
    assert isinstance(extra_vars, dict)
    return extra_vars


def test_s004_2_one_domain_runs_on_its_proxy_and_names_only_it(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    recorder = _Recorder()

    converge_domains(
        _context(project, recorder),
        DomainsRun(
            issue_certificates=False,
            receipt_directory=project / "tmp" / "deployments",
            domain_id=ResourceId("crm-site"),
        ),
    )

    argv = _playbook_argv(recorder)
    assert argv[argv.index("--limit") + 1] == "h2"
    assert _extra_vars(argv)["cloudfall_domains_domain_id"] == "crm-site"


def test_s004_17_without_a_domain_the_run_is_as_before(tmp_path: Path) -> None:
    project = _project(tmp_path)
    recorder = _Recorder()
    receipts = project / "tmp" / "deployments"

    converge_domains(
        _context(project, recorder),
        DomainsRun(issue_certificates=True, receipt_directory=receipts),
    )

    argv = _playbook_argv(recorder)
    assert "--limit" not in argv
    assert _extra_vars(argv) == {
        "cloudfall_domains_issue_certificates": True,
        "cloudfall_domains_receipt_directory": str(receipts),
    }


def test_s004_17_an_unknown_domain_is_refused_before_any_step(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    recorder = _Recorder()

    with pytest.raises(LifecycleError) as error:
        converge_domains(
            _context(project, recorder),
            DomainsRun(
                issue_certificates=False,
                receipt_directory=project / "tmp" / "deployments",
                domain_id=ResourceId("nope"),
            ),
        )

    assert error.value.code == "lifecycle_domain_missing"
    assert recorder.steps == []


def test_s004_13_a_rejected_vhost_fails_the_run_as_an_engine_step(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    recorder = _Recorder(playbook_exit=2)

    with pytest.raises(LifecycleError) as error:
        converge_domains(
            _context(project, recorder),
            DomainsRun(
                issue_certificates=False,
                receipt_directory=project / "tmp" / "deployments",
                domain_id=ResourceId("crm-site"),
            ),
        )

    # lifecycle_execution_failed is what the CLI and MCP answer as
    # ENGINE_STEP_FAILED (``_lifecycle_failed``).
    assert error.value.code == "lifecycle_execution_failed"
    assert "nginx: [emerg] rejected" in error.value.detail


async def _call(
    server: StdioServerParameters, name: str, arguments: dict[str, Any]
) -> tuple[Any, dict[str, Any]]:
    async with (
        stdio_client(server) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        result = await session.call_tool(name, arguments)
        content = result.content[0]
        assert isinstance(content, TextContent)
        return tools[name], json.loads(content.text)


def test_s004_17_the_tool_takes_a_domain_and_refuses_an_unknown_one(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    server = StdioServerParameters(
        command=sys.executable,
        args=[*SERVE, "--project", str(project)],
        cwd=str(tmp_path),
    )

    tool, answer = asyncio.run(
        _call(
            server,
            "converge_domains",
            {"domain": "nope", "confirm_destructive": True},
        )
    )

    assert "domain" in tool.input_schema["properties"]
    assert answer["ok"] is False, answer
    assert answer["error"]["code"] == "PRECONDITION"
    assert answer["error"]["context"]["code"] == "lifecycle_domain_missing"
    assert not (project / "tmp" / "inventory.json").exists()


@pytest.mark.parametrize("domain", ["h1,h2", "all", "*", "h1:h2", "!h1", "CRM"])
def test_s004_17_a_domain_that_is_no_resource_id_is_refused(
    domain: str, tmp_path: Path
) -> None:
    """A value Ansible's --limit would read as a pattern never reaches it."""
    project = _project(tmp_path)
    server = StdioServerParameters(
        command=sys.executable,
        args=[*SERVE, "--project", str(project)],
        cwd=str(tmp_path),
    )

    _, answer = asyncio.run(
        _call(
            server,
            "converge_domains",
            {"domain": domain, "confirm_destructive": True},
        )
    )

    assert answer["ok"] is False, answer
    assert not (project / "tmp" / "inventory.json").exists()


def _receipt(directory: Path, spec: dict[str, object]) -> None:
    directory.mkdir(parents=True)
    receipt = {
        "apiVersion": "cloudfall/v1",
        "kind": "DeploymentReceipt",
        "metadata": {"id": "crm-site", "description": "written by an older run"},
        "spec": {
            "domain": "crm-site",
            "server": "h2",
            "deployedAt": "2026-10-01T00:00:00Z",
            "configurationPath": "/etc/nginx/sites-available/crm.example.test.conf",
            "configurationSha256": SHA,
            **spec,
        },
    }
    (directory / "crm-site.json").write_text(json.dumps(receipt), encoding="utf-8")


def _services_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    observed = tmp_path / "observed"
    observed.mkdir()
    exit_code = main(
        [
            "services",
            "status",
            "--project",
            str(EXAMPLES),
            "--schemas",
            str(SCHEMAS),
            "--observed",
            str(observed),
            "--deployments",
            str(tmp_path / "deployments"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    return exit_code, payload


@pytest.mark.parametrize(
    "spec",
    [{}, {"previousConfigurationSha256": None}, {"previousConfigurationSha256": SHA}],
    ids=["without-the-field", "null", "a-hash"],
)
def test_s004_12_a_receipt_with_or_without_the_previous_hash_loads(
    spec: dict[str, object],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _receipt(tmp_path / "deployments", spec)

    exit_code, payload = _services_status(tmp_path, capsys)

    assert exit_code == 0, payload
    (service,) = payload["data"]["services"]
    assert service["lifecycle"]["deployed"] == "yes"
    assert service["lifecycle"]["deployed_at"] == "2026-10-01T00:00:00Z"


def test_a_malformed_previous_hash_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _receipt(tmp_path / "deployments", {"previousConfigurationSha256": "not-a-hash"})

    exit_code, payload = _services_status(tmp_path, capsys)

    assert exit_code != 0
    assert payload["error"]["code"] == "CONFIG_INVALID"
