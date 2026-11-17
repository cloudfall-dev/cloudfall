"""A granted MCP run writes an agent record and runs its verify step (S-018).

Over ``cloudfall mcp serve`` in a project, a confirmed call of a gated tool
that an ``OperatorPolicy`` grant covers runs as the command always ran, and
one record in the proposal store says so: written ``running`` before the
first ``ansible-playbook`` starts, then settled ``verified`` or ``failed`` by
the tool's own check (S-018-5, S-018-6, S-018-7, S-018-17).

The engine the server is pointed at is a stand-in whose deploy playbook runs
on the controller alone: it copies the proposal store as it finds it, then
writes the release receipt or fails the health gate.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from cloudfall.agent_gate import (
    AgentCall,
    GrantCheck,
    audit_verdict,
    declared_servers,
    domains_verdict,
    plan_sha256,
)
from cloudfall.agent_server import (
    PROJECT_BOUND_FIELDS,
    McpServeArgs,
    ServerMode,
    ServerRoot,
    bound_arguments,
)
from cloudfall.app import (
    _agent_gate,
    _converge_granted,
    _ConvergeCall,
    _granted,
)
from cloudfall.audit import AuditStatus
from cloudfall.domain import AbsolutePath, ResourceId, Sha256Digest
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import (
    CertificateReading,
    DomainObserved,
    EngineContext,
    ExecutionStep,
    VhostReading,
    run_engine_playbook,
)
from cloudfall.operator import (
    AgentClient,
    ApprovalMode,
    ApprovalRecord,
    OperationKind,
    OperatorProposal,
    ProposalStatus,
    ProposalStore,
)
from cloudfall.service_evidence import DeploymentReceipt, EvidenceTimestamp
from cloudfall.validation import SchemaCatalog, validate_config
from examples_project import copy_examples
from test_agent_server import _serve, _talk
from test_deploy_approval import _write_artifact
from test_operator import _audit_report, _Finished
from test_s018_agent_gate import _agent_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloudfall.lifecycle import StepRunner

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ENGINE = ROOT / "engine"
RELEASE = "20260101T000000Z-abcdef0"
PROPOSALS = Path("record") / "operator" / "proposals"
REASON = "crm-backend 2026-01-01 fixes the invoice export"

_POLICY = """\
---
apiVersion: cloudfall/v1
kind: OperatorPolicy
metadata:
  id: staging-operator
  description: Grants {kind} in staging after one verified run
spec:
  environment: staging
  autonomy:
    operations:
      - kind: {kind}
        requiredVerifiedRuns: 1
    maxAutonomousPerHour: 4
"""

_DEPLOY_PLAY = """\
---
- name: Stand in for a deploy
  hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - name: Copy the proposal store as the first playbook finds it
      ansible.builtin.copy:
        src: {proposals}/
        dest: {seen}/
        mode: '0600'
{outcome}
"""

_HEALTHY = """\
    - name: Make the receipt directory
      ansible.builtin.file:
        path: "{{ cloudfall_deploy_receipt_directory }}"
        state: directory
        mode: '0700'
    - name: Write the release receipt
      ansible.builtin.copy:
        dest: "{{ cloudfall_deploy_receipt_directory }}/crm-backend.json"
        content: '{}'
        mode: '0600'
"""

_FAIL_HEALTH_GATE = """\
    - name: Fail the health gate
      ansible.builtin.fail:
        msg: health check failed; rolled back to the previous release
"""


def _staging_project(tmp_path: Path, kind: str) -> Path:
    """Copy the examples into staging, ``crm`` automatic, a grant of ``kind`` earned."""
    project = copy_examples(tmp_path / "project")
    for path in sorted(project.rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("environment: production", "environment: staging"),
            encoding="utf-8",
        )
    application = project / "applications" / "crm.yaml"
    application.write_text(
        application.read_text(encoding="utf-8").replace(
            "approval: manual", "approval: automatic"
        ),
        encoding="utf-8",
    )
    (project / "operator-policies" / "production.yaml").unlink()
    (project / "operator-policies" / "staging.yaml").write_text(
        _POLICY.format(kind=kind), encoding="utf-8"
    )
    _store(project).save(
        _agent_record(
            "op-20260501100000-0123456789abcdef",
            OperationKind(kind),
            ("h1",),
            ("staging",),
        )
    )
    validate_config(project, SCHEMAS)
    return project


def _store(project: Path) -> ProposalStore:
    return ProposalStore(
        directory=project / PROPOSALS, catalog=SchemaCatalog(SCHEMAS)
    )


def _git(project: Path, *arguments: str) -> str:
    git = shutil.which("git")
    assert git is not None
    done = subprocess.run(  # noqa: S603 - the test's own repository.
        (
            git,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(project),
            *arguments,
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _committed(project: Path) -> str:
    """Make the project a git work tree with everything committed; return HEAD."""
    _git(project, "init", "--quiet")
    _git(project, "add", "--all")
    _git(project, "commit", "--quiet", "--message", "project")
    return _git(project, "rev-parse", "HEAD")


def _engine(tmp_path: Path, project: Path, *, healthy: bool) -> Path:
    engine = tmp_path / ("engine" if healthy else "failing-engine")
    playbooks = engine / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    shutil.copy(ENGINE / "ansible" / "ansible.cfg", engine / "ansible")
    (playbooks / "deploy.yml").write_text(
        _DEPLOY_PLAY.format(
            proposals=project / PROPOSALS,
            seen=tmp_path / "seen",
            outcome=_HEALTHY if healthy else _FAIL_HEALTH_GATE,
        ),
        encoding="utf-8",
    )
    return engine


def _new_records(project: Path) -> list[Path]:
    """Return the records a run wrote, beside the one earned history record."""
    return sorted(
        path
        for path in (project / PROPOSALS).glob("*.json")
        if not path.name.startswith("op-20260501100000-")
    )


def _deploy(
    tmp_path: Path, *, healthy: bool = True
) -> tuple[Path, str, dict[str, Any], dict[str, Any]]:
    """Deploy over MCP with a staging grant: the plan, then the confirmed call."""
    project = _staging_project(tmp_path, "deploy")
    _write_artifact(project)
    head = _committed(project)
    call = {"component": "crm-backend", "release": RELEASE, "reason": REASON}
    _, (planned, confirmed) = _talk(
        _serve(
            "--project",
            str(project),
            "--engine",
            str(_engine(tmp_path, project, healthy=healthy)),
            cwd=tmp_path,
        ),
        ("deploy", call),
        ("deploy", {**call, "yes": True}),
    )
    return project, head, planned, confirmed


def test_s018_5_a_granted_deploy_runs_and_names_its_agent_approval(
    tmp_path: Path,
) -> None:
    project, _, planned, confirmed = _deploy(tmp_path)

    assert planned["ok"] is True, planned
    assert planned["data"]["approval"]["self_confirm"] is True
    assert confirmed["ok"] is True, confirmed
    data = confirmed["data"]
    assert data["status"] == "ok"
    assert data["effect"] == "updated"
    (record,) = _new_records(project)
    approval = data["approval"]
    assert approval["mode"] == "agent"
    assert approval["policy"] == "staging-operator"
    assert approval["grant"] == "deploy"
    assert approval["record"] == str(record)
    assert approval["status"] == "verified"
    # The release approval's own policy keeps its name beside the grant's.
    assert approval["release_policy"] == "automatic"
    assert (project / "record" / "releases" / "crm-backend.json").is_file()


def test_s018_6_the_record_binds_the_plan_and_holds_the_call(
    tmp_path: Path,
) -> None:
    project, head, planned, confirmed = _deploy(tmp_path)
    assert confirmed["ok"] is True, confirmed
    (path,) = _new_records(project)

    # Read through the store: the record validates against the schema.
    record = _store(project).read(path)
    spec = json.loads(path.read_text(encoding="utf-8"))["spec"]

    assert record.status is ProposalStatus.VERIFIED
    assert spec["trigger"]["kind"] == "agent"
    assert spec["trigger"]["request"] == {
        "tool": "deploy",
        "arguments": {"component": "crm-backend", "release": RELEASE},
        "reason": REASON,
    }
    assert spec["diagnosis"]["summary"] == REASON
    assert spec["approval"] == {
        "mode": "agent",
        "policy": "staging-operator",
        "grant": "deploy",
        "client": {"name": "unknown", "version": "unknown"},
    }
    assert spec["binding"]["planSha256"] == plan_sha256(planned["data"])
    assert spec["binding"]["gitCommit"] == head
    assert len(spec["binding"]["worktreeDigest"]) == 64
    assert spec["operation"]["kind"] == "deploy"
    assert spec["operation"]["servers"] == ["h1", "h2"]
    assert spec["operation"]["environments"] == ["staging"]
    assert "server" not in spec["operation"]
    assert spec["outcome"]["result"] == "verified"
    assert "health gate passed" in spec["outcome"]["detail"]


def test_s018_7_the_record_is_running_before_the_first_playbook(
    tmp_path: Path,
) -> None:
    project, _, _, confirmed = _deploy(tmp_path)
    assert confirmed["ok"] is True, confirmed
    (path,) = _new_records(project)

    seen = json.loads((tmp_path / "seen" / path.name).read_text(encoding="utf-8"))

    assert seen["spec"]["status"] == "running"
    assert seen["spec"]["approval"]["mode"] == "agent"
    assert "outcome" not in seen["spec"]


def test_s018_7_a_failed_health_gate_ends_the_record_failed(
    tmp_path: Path,
) -> None:
    project, _, _, confirmed = _deploy(tmp_path, healthy=False)

    assert confirmed["ok"] is False
    assert confirmed["error"]["code"] == "ENGINE_STEP_FAILED", confirmed
    (path,) = _new_records(project)
    spec = _store(project).read(path).as_document()["spec"]
    assert isinstance(spec, dict)
    assert spec["status"] == "failed"
    assert spec["outcome"]["result"] == "failed"
    assert "run failed" in spec["outcome"]["detail"]
    seen = json.loads((tmp_path / "seen" / path.name).read_text(encoding="utf-8"))
    assert seen["spec"]["status"] == "running"


def test_a_record_leaves_out_every_argument_the_server_binds() -> None:
    bound = bound_arguments(McpServeArgs(project=ROOT / "config" / "examples"))

    assert set(bound) == PROJECT_BOUND_FIELDS


def test_a_persons_deploy_writes_no_agent_record(tmp_path: Path) -> None:
    from cloudfall.cli import main  # noqa: PLC0415 - test-local.

    project = _staging_project(tmp_path, "deploy")
    _write_artifact(project)
    before = sorted((project / PROPOSALS).iterdir())

    exit_code = main(
        [
            *("deploy", "crm-backend", "--release", RELEASE, "--yes"),
            *("--project", str(project), "--schemas", str(SCHEMAS)),
            *("--engine", str(_engine(tmp_path, project, healthy=True))),
            *("--inventory-file", str(tmp_path / "inventory.json")),
        ]
    )

    assert exit_code == 0
    assert sorted((project / PROPOSALS).iterdir()) == before


# S-018-17: each tool's own check decides verified


class _Steps:
    """Steps a run started; each exits 0 and prints nothing."""

    def __init__(self) -> None:
        self.started: list[ExecutionStep] = []

    def __call__(self, step: ExecutionStep) -> _Finished:
        self.started.append(step)
        return _Finished()


def _settled(project: Path) -> OperatorProposal:
    (path,) = _new_records(project)
    return _store(project).read(path)


def test_s018_17_a_converge_services_run_whose_audit_fails_ends_failed(
    tmp_path: Path,
) -> None:
    project = _staging_project(tmp_path, "converge-services")
    steps = _Steps()
    context = EngineContext(
        project_directory=project,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=project / "tmp" / "ansible-inventory.json",
        run=steps,
    )
    failing = _audit_report(
        ("os.distribution", AuditStatus.COMPLIANT),
        ("services.bind[postgresql-main]", AuditStatus.DRIFT),
    )

    _, approval = _converge_granted(
        McpServeArgs(project=project, schemas=SCHEMAS),
        ServerRoot(ServerMode.PROJECT, project),
        _ConvergeCall("converge_services", "services.yml", {"reason": REASON}),
        servers=lambda inventory: declared_servers(inventory)[:1],
        execute=lambda watch: run_engine_playbook(
            EngineContext(
                project_directory=context.project_directory,
                schema_directory=context.schema_directory,
                engine_directory=context.engine_directory,
                inventory_file=context.inventory_file,
                run=watch(context.run),
            ),
            "services.yml",
            {},
        ),
        verify=lambda _, __, servers: audit_verdict(failing, servers),
    )

    record = _settled(project)
    assert steps.started
    assert approval is not None
    assert approval["status"] == "failed"
    assert record.status is ProposalStatus.FAILED
    assert record.operation_kind is OperationKind.CONVERGE_SERVICES
    assert record.outcome is not None
    assert "services.bind[postgresql-main] drift" in record.outcome.detail
    assert record.request is not None
    assert record.request.reason == REASON


def _observed(domain: str, server: str, *, vhost: str, declared: str) -> DomainObserved:
    moment = EvidenceTimestamp(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
    path = AbsolutePath.from_boundary(f"/etc/nginx/sites-available/{domain}.conf")
    return DomainObserved(
        domain_id=ResourceId(domain),
        server=ResourceId(server),
        vhost=VhostReading(path=path, sha256=Sha256Digest(vhost)),
        certificate=CertificateReading(
            path=AbsolutePath.from_boundary(f"/etc/letsencrypt/live/{domain}"),
            not_after=None,
        ),
        timer_enabled=True,
        timer_active=True,
        receipt=DeploymentReceipt(
            domain_id=ResourceId(domain),
            server_id=ResourceId(server),
            deployed_at=moment,
            configuration_path=path,
            configuration_sha256=Sha256Digest(declared),
            source=Path(f"record/deployments/{domain}.json"),
        ),
    )


def _domains_apply(
    project: Path, observed: DomainObserved
) -> tuple[ProposalStatus, dict[str, object]]:
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    gate = _agent_gate(PROPOSALS, project, inventory, SCHEMAS)
    assert gate is not None
    check: GrantCheck = gate.check("domains_apply", (ResourceId("h2"),))
    assert check.self_confirm, check
    steps = _Steps()

    def run(watch: Callable[[StepRunner], StepRunner]) -> str:
        watch(steps)(
            ExecutionStep(
                description="render Domain crm-site",
                argv=("ansible-playbook", "domains.yml"),
                environment={},
            )
        )
        return "crm-site"

    _, approval = _granted(
        gate,
        check,
        AgentCall(
            "domains_apply",
            {"domain": "crm-site"},
            None,
            AgentClient(name=AgentClient.UNKNOWN, version=AgentClient.UNKNOWN),
        ),
        plan={"domain": "crm-site", "servers": ["h2"]},
        evidence=("plan: render Domain crm-site on h2",),
        execute=run,
        verify=lambda _: domains_verdict((observed,)),
    )
    return _settled(project).status, approval


def test_s018_17_a_domains_apply_run_whose_observe_matches_ends_verified(
    tmp_path: Path,
) -> None:
    project = _staging_project(tmp_path, "domains-apply")

    status, approval = _domains_apply(
        project, _observed("crm-site", "h2", vhost="a" * 64, declared="a" * 64)
    )
    record = _settled(project)

    assert status is ProposalStatus.VERIFIED
    assert approval["status"] == "verified"
    assert record.approval == ApprovalRecord(
        mode=ApprovalMode.AGENT,
        policy=ResourceId("staging-operator"),
        grant=OperationKind.DOMAINS_APPLY,
        client=AgentClient(name="unknown", version="unknown"),
    )
    assert record.diagnosis_summary == (
        "The agent called domains_apply and gave no reason"
    )
    assert record.outcome is not None
    assert "matches the declared vhost of crm-site" in record.outcome.detail


def test_s018_7_a_run_that_raises_after_its_first_step_ends_failed(
    tmp_path: Path,
) -> None:
    project = _staging_project(tmp_path, "domains-apply")
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    gate = _agent_gate(PROPOSALS, project, inventory, SCHEMAS)
    assert gate is not None
    check = gate.check("domains_apply", (ResourceId("h2"),))
    assert check.self_confirm, check

    def run(watch: Callable[[StepRunner], StepRunner]) -> str:
        watch(_Steps())(
            ExecutionStep(
                description="render Domain crm-site",
                argv=("ansible-playbook", "domains.yml"),
                environment={},
            )
        )
        message = "the receipt directory went away"
        raise OSError(message)

    with pytest.raises(OSError, match="receipt directory went away"):
        _granted(
            gate,
            check,
            AgentCall(
                "domains_apply",
                {"domain": "crm-site"},
                None,
                AgentClient(name=AgentClient.UNKNOWN, version=AgentClient.UNKNOWN),
            ),
            plan={"domain": "crm-site", "servers": ["h2"]},
            evidence=(),
            execute=run,
            verify=lambda _: domains_verdict(()),
        )

    record = _settled(project)
    assert record.status is ProposalStatus.FAILED
    assert record.outcome is not None
    assert "OSError" in record.outcome.detail


def test_s018_17_a_domains_apply_run_whose_observe_differs_ends_failed(
    tmp_path: Path,
) -> None:
    project = _staging_project(tmp_path, "domains-apply")

    status, _ = _domains_apply(
        project, _observed("crm-site", "h2", vhost="b" * 64, declared="a" * 64)
    )

    assert status is ProposalStatus.FAILED
