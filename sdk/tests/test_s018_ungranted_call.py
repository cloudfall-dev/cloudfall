"""A confirmed MCP call no grant covers only proposes (S-018).

Over ``cloudfall mcp serve`` in a project, a call of a gated tool with
``yes: true`` (or a ``converge_*`` tool with ``confirm_destructive: true``)
that no ``OperatorPolicy`` grant covers in every environment it touches runs
nothing: it leaves one ``proposed`` agent record in the proposal store and
answers ``PRECONDITION`` with ``operator_approval_required``, the proposal id
and the command a person approves it with (S-018-1 to S-018-4, S-018-8,
S-018-9). The checks a run makes first still come first and write nothing
(S-018-15); a person's command and a repository's server are unchanged
(S-018-18); the server's instructions and the changelog say so (S-018-20).

The engine the server is pointed at is a stand-in: each of its playbooks
only touches a marker file, so a marker that is absent proves no
``ansible-playbook`` started.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ansible_repository import MARKER, PURGE, ansible_repository
from cloudfall.agent_gate import GATED_TOOLS, plan_sha256, proposal_plan
from cloudfall.agent_server import McpServeArgs, server_instructions
from cloudfall.cli import main
from cloudfall.component_lock import LOCK_DIRECTORY, LockHolder
from cloudfall.decision import Interruption
from cloudfall.domain import ResourceId
from cloudfall.operator import (
    AgentClient,
    ApprovalMode,
    ApprovalRecord,
    OperationKind,
    OperatorProposal,
    ProposalStatus,
    ProposalStore,
)
from cloudfall.validation import SchemaCatalog, validate_config
from examples_project import copy_examples
from test_agent_server import _serve, _talk
from test_compatibility_surface import MCP_TOOLS, _check, _listed, _tool_names
from test_deploy_approval import _write_artifact
from test_s018_agent_gate import _add_staging_server, _agent_record
from test_s018_granted_run import _engine as _deploy_engine
from test_s018_granted_run import _staging_project

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ENGINE = ROOT / "engine"
RELEASE = "20260101T000000Z-abcdef0"
PROPOSALS = Path("record") / "operator" / "proposals"
SOURCE_URL = "postgresql://crm:source-password@db.example.internal/crm\n"
REASON = "crm-backend needs the invoice fix"

_MARKER_PLAY = """\
---
- name: Stand in for {name}
  hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - name: Mark that a playbook started
      ansible.builtin.file:
        path: {marker}
        state: touch
        mode: '0600'
"""

_COMPONENT = {"component": "crm-backend", "release": RELEASE}
_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("deploy", {**_COMPONENT, "reason": REASON}),
    ("rollback", _COMPONENT),
    ("restart", {"component": "crm-backend"}),
    ("migrate", {"build": ["crm-backend=main"]}),
    (
        "data_copy",
        {
            "service": "postgresql-main",
            "database": "crm",
            "source_url_file": "tmp/source.url",
        },
    ),
    ("domains_apply", {"domain": "crm-site"}),
    ("converge_baseline", {}),
    ("converge_services", {}),
    ("converge_domains", {"domain": "crm-site"}),
)
_CONFIRM = {
    tool: (
        {"confirm_destructive": True}
        if tool.startswith("converge_")
        else {"yes": True}
    )
    for tool in GATED_TOOLS
}


def _marker_engine(directory: Path, marker: Path) -> Path:
    """Write an engine whose every playbook only touches ``marker``."""
    playbooks = directory / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    shutil.copy(ENGINE / "ansible" / "ansible.cfg", directory / "ansible")
    for playbook in sorted((ENGINE / "ansible" / "playbooks").glob("*.yml")):
        (playbooks / playbook.name).write_text(
            _MARKER_PLAY.format(name=playbook.name, marker=marker), encoding="utf-8"
        )
    return directory


def _store(project: Path) -> ProposalStore:
    return ProposalStore(directory=project / PROPOSALS, catalog=SchemaCatalog(SCHEMAS))


def _records(project: Path) -> list[Path]:
    directory = project / PROPOSALS
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def _unpoliced_project(directory: Path) -> Path:
    """Copy the examples: no OperatorPolicy, ``crm`` automatic, a release built."""
    project = copy_examples(directory)
    (project / "operator-policies" / "production.yaml").unlink()
    application = project / "applications" / "crm.yaml"
    application.write_text(
        application.read_text(encoding="utf-8").replace(
            "approval: manual", "approval: automatic"
        ),
        encoding="utf-8",
    )
    _write_artifact(project)
    (project / "tmp" / "source.url").write_text(SOURCE_URL, encoding="utf-8")
    validate_config(project, SCHEMAS)
    return project


@dataclasses.dataclass(frozen=True, slots=True)
class _Session:
    project: Path
    marker: Path
    planned: dict[str, Any]
    """``deploy`` without ``yes``: the plan a proposal binds."""
    refused: dict[str, dict[str, Any]]
    """Each gated tool's confirmed call, by tool."""
    repeated: dict[str, Any]
    """``restart`` confirmed a second time."""


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> _Session:
    """One server, every gated tool confirmed, in a project with no policy."""
    root = tmp_path_factory.mktemp("ungranted")
    project = _unpoliced_project(root / "project")
    marker = root / "playbook-started"
    engine = _marker_engine(root / "engine", marker)
    confirmed = [(tool, {**call, **_CONFIRM[tool]}) for tool, call in _CALLS]
    _, answers = _talk(
        _serve("--project", str(project), "--engine", str(engine), cwd=root),
        ("deploy", dict(_CALLS[0][1])),
        *confirmed,
        confirmed[2],
    )
    planned, *refused, repeated = answers
    tools = (tool for tool, _ in _CALLS)
    return _Session(
        project=project,
        marker=marker,
        planned=planned,
        refused=dict(zip(tools, refused, strict=True)),
        repeated=repeated,
    )


def _refusal(answer: dict[str, Any]) -> dict[str, Any]:
    assert answer["ok"] is False, answer
    assert answer["error"]["code"] == "PRECONDITION", answer
    context = answer["error"]["context"]
    assert isinstance(context, dict)
    assert context["code"] == "operator_approval_required", answer
    return context


_LIFECYCLE_TOOLS = ("deploy", "rollback", "restart", "migrate", "data_copy")


def test_s018_1_each_confirmed_lifecycle_call_proposes_and_runs_nothing(
    session: _Session,
) -> None:
    for tool in (*_LIFECYCLE_TOOLS, "domains_apply"):
        answer = session.refused[tool]
        context = _refusal(answer)
        proposal = context["proposal"]
        assert context["command"] == f"cloudfall operator approve {proposal}", tool
        assert any(
            "production" in line and "no operator policy" in line
            for line in context["reasons"]
        ), context
        assert f"cloudfall operator approve {proposal}" in answer["error"]["message"]
    assert not session.marker.exists()
    project = session.project
    assert not (project / "tmp" / "ansible-inventory.json").exists()
    for receipts in ("releases", "deployments", "data-migrations"):
        assert not (project / "record" / receipts).exists(), receipts
    assert not (project / "tmp" / "migrate").exists()


def test_s018_2_each_confirmed_converge_call_proposes_and_runs_nothing(
    session: _Session,
) -> None:
    for tool in ("converge_baseline", "converge_services", "converge_domains"):
        context = _refusal(session.refused[tool])
        assert context["command"] == f"cloudfall operator approve {context['proposal']}"
        assert any("no operator policy" in line for line in context["reasons"])
    assert not session.marker.exists()
    assert not (session.project / "tmp" / "ansible-inventory.json").exists()


def test_s018_3_each_refusal_writes_one_valid_proposed_agent_record(
    session: _Session,
) -> None:
    store = _store(session.project)
    listing = store.list()
    by_id = {record.resource_id.value: record for record in listing.records}

    assert listing.unreadable == ()
    assert len(_records(session.project)) == len(GATED_TOOLS)
    for tool, call in _CALLS:
        context = _refusal(session.refused[tool])
        record = by_id[context["proposal"]]
        path = Path(context["record"])
        spec = json.loads(path.read_text(encoding="utf-8"))["spec"]
        assert record.status is ProposalStatus.PROPOSED, tool
        assert spec["trigger"]["kind"] == "agent"
        assert spec["trigger"]["request"]["tool"] == tool
        expected = {name: value for name, value in call.items() if name != "reason"}
        assert spec["trigger"]["request"]["arguments"] == expected, tool
        assert spec["operation"]["kind"] == GATED_TOOLS[tool].value
        assert spec["operation"]["servers"], tool
        assert spec["operation"]["environments"] == ["production"], tool
        assert len(spec["binding"]["planSha256"]) == 64
        assert "approval" not in spec
        assert "outcome" not in spec
    deploy = by_id[_refusal(session.refused["deploy"])["proposal"]]
    assert deploy.request is not None
    assert deploy.request.reason == REASON
    assert deploy.diagnosis_summary == REASON
    assert [server.value for server in deploy.operation_servers] == ["h1", "h2"]
    assert deploy.binding is not None
    assert deploy.binding.plan_sha256 == plan_sha256(
        proposal_plan(session.planned["data"])
    )
    # The domain's proxy server alone, not every declared server.
    domains = by_id[_refusal(session.refused["converge_domains"])["proposal"]]
    assert [server.value for server in domains.operation_servers] == ["h2"]


def test_s018_3_no_record_holds_a_secret_value(session: _Session) -> None:
    for path in _records(session.project):
        assert "source-password" not in path.read_text(encoding="utf-8")


def test_s018_4_the_same_call_again_returns_the_same_proposal(
    session: _Session,
) -> None:
    first = _refusal(session.refused["restart"])
    again = _refusal(session.repeated)

    assert again["proposal"] == first["proposal"]
    restarts = [
        record
        for record in _store(session.project).list().records
        if record.operation_kind is OperationKind.RESTART
    ]
    assert len(restarts) == 1


# S-018-8, S-018-9, S-018-15: the gate a confirmed call meets. The server
# binds --proposals on each gated command; a command given it is gated alike.


def _command(
    project: Path, engine: Path, tool: str, *arguments: str
) -> tuple[int, dict[str, Any]]:
    return _run(
        [
            *tool.split("_"),
            *arguments,
            *("--project", str(project), "--schemas", str(SCHEMAS)),
            *("--engine", str(engine)),
            *("--proposals", str(PROPOSALS)),
            "--yes",
        ]
    )


def _run(argv: list[str]) -> tuple[int, dict[str, Any]]:
    import contextlib  # noqa: PLC0415 - test-local.
    import io  # noqa: PLC0415 - test-local.

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exit_code = main(argv)
    payload = json.loads(out.getvalue())
    assert isinstance(payload, dict)
    return exit_code, payload


def test_s018_8_one_environment_without_a_grant_proposes_the_whole_run(
    tmp_path: Path,
) -> None:
    project = _staging_project(tmp_path, "deploy")
    _write_artifact(project)
    # h3, in production, joins crm-backend's staging servers h1 and h2.
    h2 = (project / "servers" / "h2.yaml").read_text(encoding="utf-8")
    (project / "servers" / "h3.yaml").write_text(
        h2.replace("h2", "h3")
        .replace("environment: staging", "environment: production")
        .replace("203.0.113.12", "203.0.113.13")
        .replace("2001:db8:12::", "2001:db8:13::"),
        encoding="utf-8",
    )
    component = project / "components" / "crm-backend.yaml"
    component.write_text(
        component.read_text(encoding="utf-8").replace(
            "      - h2\n", "      - h2\n      - h3\n", 1
        ),
        encoding="utf-8",
    )
    validate_config(project, SCHEMAS)
    marker = tmp_path / "playbook-started"
    before = _records(project)

    _, payload = _command(
        project,
        _marker_engine(tmp_path / "engine", marker),
        "deploy",
        "crm-backend",
        *("--release", RELEASE),
    )

    context = _refusal(payload)
    reasons = context["reasons"]
    assert any(line.startswith("production: ") for line in reasons), reasons
    assert any(
        line.startswith("staging: ") and "grants deploy" in line for line in reasons
    ), reasons
    assert not marker.exists()
    assert not (project / "record" / "releases").exists()
    (written,) = sorted(set(_records(project)) - set(before))
    record = _store(project).read(written)
    assert [env.value for env in record.operation_environments] == [
        "staging",
        "production",
    ]


_RESTART_POLICY = """\
---
apiVersion: cloudfall/v1
kind: OperatorPolicy
metadata:
  id: production-operator
  description: Grants {kind} in production after one verified run
spec:
  environment: production
  autonomy:
    operations:
      - kind: {kind}
        requiredVerifiedRuns: 1
    maxAutonomousPerHour: {per_hour}
{quiet}"""


def _at(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _later(
    record: OperatorProposal, identifier: str, created_at: str
) -> OperatorProposal:
    return dataclasses.replace(
        record, resource_id=ResourceId(identifier), created_at=created_at
    )


_VERIFIED = "op-20260501100000-0123456789abcdef"
_LATER = "op-20260501110000-0123456789abcdef"


def _withheld_project(tmp_path: Path, case: str) -> Path:
    project = copy_examples(tmp_path / "project")
    now = datetime.now(UTC)
    quiet = ""
    if case == "quiet-hours":
        start, end = now - timedelta(hours=1), now + timedelta(hours=1)
        quiet = (
            "    quietHours:\n"
            f'      start: "{start:%H:%M}"\n'
            f'      end: "{end:%H:%M}"\n'
        )
    (project / "operator-policies" / "production.yaml").write_text(
        _RESTART_POLICY.format(
            kind="deploy" if case == "class-not-granted" else "restart",
            per_hour=1 if case.startswith("rate-limited") else 4,
            quiet=quiet,
        ),
        encoding="utf-8",
    )
    _add_staging_server(project)
    store = _store(project)
    verified_on = "h3" if case == "other-environment" else "h1"
    verified = _agent_record(
        _VERIFIED,
        OperationKind.RESTART,
        (verified_on,),
        ("staging" if case == "other-environment" else "production",),
    )
    store.save(verified)
    if case == "failed":
        store.save(
            _later(
                _agent_record(
                    _LATER,
                    OperationKind.RESTART,
                    ("h1",),
                    ("production",),
                    status=ProposalStatus.FAILED,
                ),
                _LATER,
                "2026-05-01T11:00:00+00:00",
            )
        )
    if case == "interrupted":
        running = _agent_record(
            _LATER,
            OperationKind.RESTART,
            ("h1",),
            ("production",),
            status=ProposalStatus.RUNNING,
        )
        store.save(
            dataclasses.replace(
                _later(running, _LATER, "2026-05-01T11:00:00+00:00"),
                status=ProposalStatus.INTERRUPTED,
                interruption=Interruption(
                    confirmed_by="roman", confirmed_at="2026-05-01T11:30:00+00:00"
                ),
            )
        )
    if case.startswith("rate-limited"):
        agent = case == "rate-limited-agent"
        mode = ApprovalMode.AGENT if agent else ApprovalMode.AUTONOMOUS
        approval = ApprovalRecord(
            mode=mode,
            policy=ResourceId("production-operator"),
            grant=OperationKind.RESTART if mode is ApprovalMode.AGENT else None,
            client=(
                AgentClient(name="unknown", version="unknown")
                if mode is ApprovalMode.AGENT
                else None
            ),
        )
        ran = _agent_record(
            _LATER, OperationKind.RESTART, ("h1",), ("production",), approval=approval
        )
        assert ran.outcome is not None
        store.save(
            dataclasses.replace(
                _later(ran, _LATER, _at(now - timedelta(minutes=10))),
                outcome=dataclasses.replace(
                    ran.outcome, executed_at=_at(now - timedelta(minutes=5))
                ),
            )
        )
    if case == "unreadable":
        (store.directory / "op-broken.json").write_text("{", encoding="utf-8")
    validate_config(project, SCHEMAS)
    return project


@pytest.mark.parametrize(
    ("case", "cause"),
    [
        ("class-not-granted", "does not grant autonomy for restart"),
        ("other-environment", "0 of 1 required"),
        ("failed", "most recent restart receipt failed in production"),
        ("interrupted", "most recent restart receipt was interrupted in production"),
        ("quiet-hours", "quiet hours"),
        ("rate-limited-agent", "rate limit reached: 1 autonomous executions"),
        ("rate-limited-autonomous", "rate limit reached: 1 autonomous executions"),
        ("unreadable", "proposal receipt(s) cannot be read"),
    ],
)
def test_s018_9_a_withheld_grant_proposes_and_names_its_cause(
    tmp_path: Path, case: str, cause: str
) -> None:
    project = _withheld_project(tmp_path, case)
    marker = tmp_path / "playbook-started"
    before = set(_records(project))

    _, payload = _command(
        project, _marker_engine(tmp_path / "engine", marker), "restart", "crm-backend"
    )

    context = _refusal(payload)
    (line,) = context["reasons"]
    assert line.startswith("production: "), line
    assert cause in line, line
    assert not marker.exists()
    (written,) = set(_records(project)) - before
    assert context["record"] == str(written)
    assert _store(project).read(written).status is ProposalStatus.PROPOSED


def test_s018_9_the_operators_grant_still_runs_the_call(tmp_path: Path) -> None:
    project = _withheld_project(tmp_path, "granted")
    marker = tmp_path / "playbook-started"

    _, payload = _command(
        project, _marker_engine(tmp_path / "engine", marker), "restart", "crm-backend"
    )

    assert payload["ok"] is True, payload
    assert payload["data"]["approval"]["mode"] == "agent"
    assert marker.exists()


def test_s018_15_a_manual_release_without_approval_refuses_before_any_proposal(
    tmp_path: Path,
) -> None:
    project = _staging_project(tmp_path, "deploy")
    application = project / "applications" / "crm.yaml"
    application.write_text(
        application.read_text(encoding="utf-8").replace(
            "approval: automatic", "approval: manual"
        ),
        encoding="utf-8",
    )
    _write_artifact(project)
    marker = tmp_path / "playbook-started"
    before = _records(project)

    _, payload = _command(
        project,
        _marker_engine(tmp_path / "engine", marker),
        "deploy",
        "crm-backend",
        *("--release", RELEASE),
    )

    assert payload["ok"] is False, payload
    assert payload["error"]["code"] == "PRECONDITION"
    assert payload["error"]["context"]["code"] == "deployment_approval_missing"
    assert _records(project) == before
    assert not marker.exists()


def test_s018_15_a_held_lock_refuses_before_any_proposal(tmp_path: Path) -> None:
    project = _unpoliced_project(tmp_path / "project")
    lock = project / LOCK_DIRECTORY / "crm-backend.json"
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = LockHolder.for_this_process(ResourceId("crm-backend"), "deploy")
    lock.write_text(json.dumps(holder.as_dict()), encoding="utf-8")

    _, payload = _command(
        project,
        _marker_engine(tmp_path / "engine", tmp_path / "marker"),
        "restart",
        "crm-backend",
    )

    assert payload["error"]["code"] == "CONFLICT", payload
    assert payload["error"]["context"]["code"] == "component_locked"
    assert _records(project) == []


def test_s018_18_a_persons_deploy_without_a_policy_runs_and_records_nothing(
    tmp_path: Path,
) -> None:
    project = _staging_project(tmp_path, "deploy")
    (project / "operator-policies" / "staging.yaml").unlink()
    _write_artifact(project)
    before = _records(project)

    exit_code, payload = _run(
        [
            *("deploy", "crm-backend", "--release", RELEASE, "--yes"),
            *("--project", str(project), "--schemas", str(SCHEMAS)),
            *("--engine", str(_deploy_engine(tmp_path, project, healthy=True))),
            *("--inventory-file", str(tmp_path / "inventory.json")),
        ]
    )

    assert exit_code == 0, payload
    assert payload["data"]["effect"] == "updated"
    assert _records(project) == before
    assert (project / "record" / "releases" / "crm-backend.json").is_file()


def test_s018_18_a_repository_server_lists_the_same_tools(tmp_path: Path) -> None:
    repository = ansible_repository(
        tmp_path, **{"write-marker": MARKER, "purge": PURGE}
    )
    served = _tool_names("--repository", str(repository), cwd=tmp_path)

    assert _check(_listed(MCP_TOOLS)["ansible-repository"], served, "fleet") == []
    assert not set(GATED_TOOLS) & set(served)


def test_s018_20_the_instructions_name_the_refusal_and_the_approval() -> None:
    told = server_instructions(McpServeArgs(project=ROOT / "config" / "examples"))

    assert "operator_approval_required" in told
    assert "cloudfall operator approve" in told
    assert "approval.selfConfirm" in told
    assert "call again with yes: true" not in told
    assert "OperatorPolicy" in told


def test_s018_20_the_changelog_names_the_breaking_change() -> None:
    fragment = ROOT / "changelog.d" / "476-ungranted-mcp-calls-propose.md"
    text = fragment.read_text(encoding="utf-8")

    assert text.startswith("### Breaking\n")
    for tool in GATED_TOOLS:
        assert f"`{tool}`" in text, tool
    for named in ("operator_approval_required", "cloudfall operator approve"):
        assert named in text
    assert "OperatorPolicy" in text
