"""A person approves an agent's proposal with ``cloudfall operator approve`` (S-018).

A confirmed MCP call no grant covers leaves a ``proposed`` agent record. A
person approves it at a terminal, typing its id: the stored call runs
through the tool's own command, recorded ``human`` / ``terminal`` and
settled by the tool's own check (S-018-11), once its plan, HEAD and
uncommitted changes still match what was proposed (S-018-12). The run then
counts in the history a grant is earned on (S-018-13), while the operator's
autonomous pass never runs an agent proposal itself (S-018-14).

The proposals here are made by ``cloudfall deploy --yes --proposals``, the
call ``cloudfall mcp serve`` makes for an agent's ``deploy`` with
``yes: true``. The engine is the stand-in of the granted-run tests: its
deploy playbook runs on the controller and writes the release receipt.
"""

from __future__ import annotations

import dataclasses
import errno
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from cloudfall.agent_gate import (
    ERROR_AGENT_RECORD_UNWRITABLE,
    ApprovedRun,
    Verdict,
    check_grant,
)
from cloudfall.app import (
    ComponentArgs,
    DataMigrateArgs,
    DeployArgs,
    DomainApplyArgs,
    EngineArgs,
    MigrateArgs,
    RollbackArgs,
    _call_arguments,
    _recorded,
    _stored_arguments,
    app,
)
from cloudfall.cli import main
from cloudfall.decision import ApprovalChannel
from cloudfall.domain import ReleaseId, ResourceId
from cloudfall.inventory import PlatformInventory
from cloudfall.operator import (
    ApprovalMode,
    OperationKind,
    OperatorProposal,
    ProposalStatus,
    ProposalStore,
    autonomous_pass,
)
from cloudfall.validation import SchemaCatalog, validate_config
from test_decision import _Terminal
from test_deploy_approval import _write_artifact
from test_s018_agent_gate import _agent_record
from test_s018_granted_run import (
    _committed,
    _engine,
    _FirstUpdateFails,
    _git,
    _render_with,
    _staging_project,
    _Steps,
)
from test_s018_ungranted_call import (
    _command,
    _marker_engine,
    _unpoliced_project,
    server_free_migration,
)
from treaty import CliExit

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
RELEASE = "20260101T000000Z-abcdef0"
PROPOSALS = Path("record") / "operator" / "proposals"
REASON = "crm-backend 2026-01-01 fixes the invoice export"


def _store(project: Path) -> ProposalStore:
    return ProposalStore(directory=project / PROPOSALS, catalog=SchemaCatalog(SCHEMAS))


def _ungranted_project(tmp_path: Path, *, policy: bool) -> Path:
    """Make a staging project, ``crm`` automatic, no deploy history, in git.

    With ``policy``, staging grants ``deploy`` after one verified run, which
    no record has earned yet.
    """
    project = _staging_project(tmp_path, "deploy")
    for path in (project / PROPOSALS).glob("*.json"):
        path.unlink()
    if not policy:
        (project / "operator-policies" / "staging.yaml").unlink()
    _write_artifact(project)
    validate_config(project, SCHEMAS)
    _committed(project)
    return project


def _run(argv: list[str]) -> tuple[int, dict[str, Any]]:
    stdout = io.StringIO()
    exit_code = app.run(
        [*argv, "--format", "json"],
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=io.StringIO(),
    )
    payload = json.loads(stdout.getvalue())
    assert isinstance(payload, dict)
    return exit_code, payload


def _agent_deploy(project: Path, engine: Path) -> tuple[int, dict[str, Any]]:
    """Make the call the MCP server makes for ``deploy`` with ``yes: true``."""
    return _run(
        [
            *("deploy", "crm-backend", "--release", RELEASE, "--yes"),
            *("--project", str(project), "--schemas", str(SCHEMAS)),
            *("--engine", str(engine)),
            *("--proposals", str(PROPOSALS)),
            *("--reason", REASON),
        ]
    )


def _proposal(project: Path, engine: Path) -> str:
    _, payload = _agent_deploy(project, engine)
    assert payload["ok"] is False, payload
    context = payload["error"]["context"]
    assert context["code"] == "operator_approval_required", payload
    proposal = context["proposal"]
    assert isinstance(proposal, str)
    return proposal


def _approve(
    project: Path, engine: Path, proposal: str, *, typed: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Run ``operator approve`` at a terminal, typing ``typed`` (the id)."""
    stdout = _Terminal()
    exit_code = app.run(
        [
            *("operator", "approve", proposal),
            *("--project", str(project), "--schemas", str(SCHEMAS)),
            *("--engine", str(engine), "--format", "json"),
        ],
        stdin=_Terminal(f"{proposal if typed is None else typed}\n"),
        stdout=stdout,
        stderr=io.StringIO(),
    )
    payload = json.loads(stdout.getvalue())
    assert isinstance(payload, dict)
    return exit_code, payload


def _record(project: Path, proposal: str) -> OperatorProposal:
    return _store(project).load(ResourceId(proposal))


def _receipt(project: Path) -> Path:
    return project / "record" / "releases" / "crm-backend.json"


def test_s018_11_a_person_approves_and_the_deploy_runs_verified(
    tmp_path: Path,
) -> None:
    project = _ungranted_project(tmp_path, policy=False)
    engine = _engine(tmp_path, project, healthy=True)
    proposal = _proposal(project, engine)
    assert not _receipt(project).exists()

    exit_code, payload = _approve(project, engine, proposal)

    assert exit_code == 0, payload
    assert payload["data"]["status"] == "ok"
    record = _record(project, proposal)
    assert record.status is ProposalStatus.VERIFIED
    assert record.approval is not None
    assert record.approval.mode is ApprovalMode.HUMAN
    assert record.approval.via is ApprovalChannel.TERMINAL
    assert record.outcome is not None
    assert "health gate passed" in record.outcome.detail
    # The stored request ran: its release, on its servers.
    assert record.request is not None
    assert record.request.arguments == {"component": "crm-backend", "release": RELEASE}
    assert _receipt(project).is_file()
    # The first playbook saw the record running, approved by the person.
    seen = json.loads(
        (tmp_path / "seen" / f"{proposal}.json").read_text(encoding="utf-8")
    )
    assert seen["spec"]["status"] == "running"
    assert seen["spec"]["approval"] == {"mode": "human", "via": "terminal"}
    # One record: the proposal became the run's.
    assert len(list((project / PROPOSALS).glob("*.json"))) == 1


@pytest.mark.parametrize("flags", [["--yes"], []], ids=["yes", "bare"])
def test_s018_11_off_a_terminal_it_exits_person_required_and_runs_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    project = _ungranted_project(tmp_path, policy=False)
    engine = _engine(tmp_path, project, healthy=True)
    proposal = _proposal(project, engine)
    capsys.readouterr()

    exit_code = main(
        [
            *("operator", "approve", proposal, *flags),
            *("--project", str(project), "--schemas", str(SCHEMAS)),
            *("--engine", str(engine)),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["code"] == "PERSON_REQUIRED"
    assert _record(project, proposal).status is ProposalStatus.PROPOSED
    assert not _receipt(project).exists()
    assert not (tmp_path / "seen").exists()


def test_s018_11_a_mistyped_id_runs_nothing(tmp_path: Path) -> None:
    project = _ungranted_project(tmp_path, policy=False)
    engine = _engine(tmp_path, project, healthy=True)
    proposal = _proposal(project, engine)

    exit_code, payload = _approve(project, engine, proposal, typed="op-other")

    assert exit_code != 0
    assert payload["ok"] is False
    assert _record(project, proposal).status is ProposalStatus.PROPOSED
    assert not _receipt(project).exists()


_OVERRIDE = chr(0x202E)
"""RIGHT-TO-LEFT OVERRIDE: shown raw, it reverses the text after it."""


def test_s018_11_the_prompt_shows_the_agents_reason_without_its_control_codes(
    tmp_path: Path,
) -> None:
    # The reason is the agent's text: raw, an escape code could hide the
    # command and servers that follow it on the person's terminal (Q-2).
    project = _ungranted_project(tmp_path, policy=False)
    engine = _engine(tmp_path, project, healthy=True)
    _, refused = _run(
        [
            *("deploy", "crm-backend", "--release", RELEASE, "--yes"),
            *("--project", str(project), "--schemas", str(SCHEMAS)),
            *("--engine", str(engine)),
            *("--proposals", str(PROPOSALS)),
            *("--reason", f"a harmless restart\x1b[8m{_OVERRIDE}"),
        ]
    )
    proposal = refused["error"]["context"]["proposal"]
    stderr = io.StringIO()

    app.run(
        [
            *("operator", "approve", proposal),
            *("--project", str(project), "--schemas", str(SCHEMAS)),
            *("--engine", str(engine), "--format", "json"),
        ],
        stdin=_Terminal(f"{proposal}\n"),
        stdout=_Terminal(),
        stderr=stderr,
    )

    shown = stderr.getvalue()
    assert "a harmless restart\\x1b[8m\\u202e" in shown, shown
    assert "\x1b" not in shown
    assert _OVERRIDE not in shown
    assert "on h1, h2" in shown


def test_s018_11_a_person_approves_a_migrate_that_changes_no_declared_server(
    tmp_path: Path,
) -> None:
    # No grant can ever cover it, so a person's approval is its only way to run.
    project = _unpoliced_project(tmp_path / "project")
    server_free_migration(project, frozenset({"rollback-window"}))
    engine = _marker_engine(tmp_path / "engine", tmp_path / "playbook-started")
    _, refused = _command(project, engine, "migrate", "--build", "crm-backend=main")
    proposal = refused["error"]["context"]["proposal"]

    exit_code, payload = _approve(project, engine, proposal)

    record = _record(project, proposal)
    assert exit_code == 0, payload
    assert record.status is ProposalStatus.VERIFIED, record.outcome
    assert record.operation_servers == ()
    assert record.approval is not None
    assert record.approval.mode is ApprovalMode.HUMAN
    assert record.approval.via is ApprovalChannel.TERMINAL
    plan = json.loads(
        (project / "tmp" / "migrate" / "plan.json").read_text(encoding="utf-8")
    )
    assert {step["status"] for step in plan["steps"]} == {"completed"}


def _requests() -> list[tuple[str, EngineArgs]]:
    component = ResourceId("crm-backend")
    release = ReleaseId(RELEASE)
    return [
        (
            "deploy",
            DeployArgs(
                component=component, release=release, env_file=Path("tmp/crm.env")
            ),
        ),
        ("rollback", RollbackArgs(component=component, release=release)),
        ("restart", ComponentArgs(component=component)),
        ("migrate", MigrateArgs(build=("crm-backend=main",), restart=True)),
        (
            "data_copy",
            DataMigrateArgs(
                service=ResourceId("postgresql-main"),
                database="crm",
                source_url_file=Path("tmp/source.url"),
                source_schema=("public", "app"),
            ),
        ),
        (
            "domains_apply",
            DomainApplyArgs(domain=ResourceId("crm-site"), issue_certificate=True),
        ),
    ]


@pytest.mark.parametrize(
    ("tool", "arguments"), _requests(), ids=[tool for tool, _ in _requests()]
)
def test_a_stored_request_reads_back_as_its_commands_arguments(
    tool: str, arguments: EngineArgs
) -> None:
    stored = json.loads(json.dumps(_call_arguments(arguments)))

    typed = _stored_arguments(type(arguments), tool, stored)

    assert typed == {name: getattr(arguments, name) for name in stored}


def test_a_stored_request_naming_a_bound_argument_is_refused() -> None:
    with pytest.raises(CliExit) as refused:
        _stored_arguments(
            ComponentArgs,
            "restart",
            {"component": "crm-backend", "engine": "/elsewhere"},
        )

    assert refused.value.context["code"] == "proposal_binding_changed"


def _change_component(project: Path) -> None:
    component = project / "components" / "crm-backend.yaml"
    component.write_text(
        component.read_text(encoding="utf-8").replace(
            'FEATURE_SIGNUPS: "true"', 'FEATURE_SIGNUPS: "false"'
        ),
        encoding="utf-8",
    )


def _commit_readme(project: Path) -> None:
    (project / "NOTES.md").write_text("A note\n", encoding="utf-8")
    _git(project, "add", "NOTES.md")
    _git(project, "commit", "--quiet", "--message", "notes")


def _touch_untracked(project: Path) -> None:
    (project / "notes.txt").write_text("not committed\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("change", "named"),
    [
        (_change_component, "the uncommitted changes in the project"),
        (_commit_readme, "the git commit (was "),
        (_touch_untracked, "the uncommitted changes in the project"),
    ],
    ids=["component-config", "new-commit", "uncommitted-file"],
)
def test_s018_12_a_changed_binding_refuses_before_anything_runs(
    tmp_path: Path, change: Callable[[Path], None], named: str
) -> None:
    project = _ungranted_project(tmp_path, policy=False)
    engine = _engine(tmp_path, project, healthy=True)
    proposal = _proposal(project, engine)
    change(project)

    exit_code, payload = _approve(project, engine, proposal)

    assert exit_code != 0
    assert payload["error"]["code"] == "PRECONDITION", payload
    context = payload["error"]["context"]
    assert context["code"] == "proposal_binding_changed"
    assert context["proposal"] == proposal
    assert any(named in line for line in context["changed"]), context
    assert named in payload["error"]["message"]
    assert not (tmp_path / "seen").exists()
    assert not _receipt(project).exists()
    record = _record(project, proposal)
    assert record.status is ProposalStatus.PROPOSED
    assert record.approval is None


def test_s018_12_a_change_in_decisions_record_or_tmp_keeps_the_binding(
    tmp_path: Path,
) -> None:
    project = _ungranted_project(tmp_path, policy=False)
    engine = _engine(tmp_path, project, healthy=True)
    proposal = _proposal(project, engine)
    for directory in ("decisions", "record", "tmp"):
        (project / directory).mkdir(exist_ok=True)
        (project / directory / "scratch.txt").write_text("x\n", encoding="utf-8")

    exit_code, payload = _approve(project, engine, proposal)

    assert exit_code == 0, payload
    assert _record(project, proposal).status is ProposalStatus.VERIFIED


# A person-approved run's record the store can't save (#489)


@dataclasses.dataclass(frozen=True, slots=True)
class _StartFails(ProposalStore):
    """A store whose folder cannot take the approved run's start."""

    def start(self, running: OperatorProposal) -> Path:
        message = f"cannot start {running.resource_id.value}"
        raise OSError(errno.EIO, message)


def _approved_run(
    project: Path, engine: Path, store: ProposalStore
) -> ApprovedRun:
    proposal = _record(project, _proposal(project, engine))
    return ApprovedRun.of(store, proposal, ApprovalChannel.TERMINAL)


def test_an_approved_run_whose_start_cannot_be_saved_exits_precondition(
    tmp_path: Path,
) -> None:
    project = _ungranted_project(tmp_path, policy=False)
    engine = _engine(tmp_path, project, healthy=True)
    store = _StartFails(directory=project / PROPOSALS, catalog=SchemaCatalog(SCHEMAS))
    steps = _Steps()

    with pytest.raises(CliExit) as raised:
        _recorded(
            _approved_run(project, engine, store),
            _render_with(steps),
            lambda _: Verdict.passed("crm-site matches its receipt"),
        )

    assert raised.value.code == "PRECONDITION"
    assert raised.value.context["code"] == ERROR_AGENT_RECORD_UNWRITABLE
    assert str(project / PROPOSALS) in raised.value.message
    assert steps.started == []


def test_a_lost_settle_of_an_approved_run_never_records_it_failed(
    tmp_path: Path,
) -> None:
    project = _ungranted_project(tmp_path, policy=False)
    engine = _engine(tmp_path, project, healthy=True)
    store = _FirstUpdateFails(
        directory=project / PROPOSALS, catalog=SchemaCatalog(SCHEMAS)
    )
    steps = _Steps()

    with pytest.raises(CliExit) as raised:
        _recorded(
            _approved_run(project, engine, store),
            _render_with(steps),
            lambda _: Verdict.passed("crm-site matches its receipt"),
        )

    assert raised.value.code == "PRECONDITION"
    assert raised.value.context["ran"] is True
    assert raised.value.context["verified"] is True
    assert "the run completed and verified" in raised.value.message
    assert store.updates == [ProposalStatus.VERIFIED.value]
    record = _store(project).read(Path(str(raised.value.context["record"])))
    assert record.status is ProposalStatus.RUNNING


def test_s018_13_a_person_approved_run_earns_the_grant(tmp_path: Path) -> None:
    project = _ungranted_project(tmp_path, policy=True)
    engine = _engine(tmp_path, project, healthy=True)
    proposal = _proposal(project, engine)
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    servers = (ResourceId("h1"), ResourceId("h2"))
    before = check_grant(
        OperationKind.DEPLOY, servers, inventory, _store(project), datetime.now(UTC)
    )

    approved, _ = _approve(project, engine, proposal)
    after = check_grant(
        OperationKind.DEPLOY, servers, inventory, _store(project), datetime.now(UTC)
    )
    exit_code, payload = _agent_deploy(project, engine)

    assert "0 of 1 required" in before.environments[0].reason
    assert approved == 0
    assert after.self_confirm is True, after
    assert exit_code == 0, payload
    assert payload["data"]["approval"]["mode"] == "agent"
    assert payload["data"]["approval"]["status"] == "verified"


def test_s018_14_the_autonomous_pass_leaves_an_agent_proposal_to_a_person(
    tmp_path: Path,
) -> None:
    project = _ungranted_project(tmp_path, policy=True)
    proposal = _proposal(project, _engine(tmp_path, project, healthy=True))
    store = _store(project)
    # Since the proposal, its history was earned: the policy's grant of
    # deploy in staging now holds, for a pass that would use it.
    store.save(
        _agent_record(
            "op-20260501100000-0123456789abcdef",
            OperationKind.DEPLOY,
            ("h1",),
            ("staging",),
        )
    )
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    assert check_grant(
        OperationKind.DEPLOY,
        (ResourceId("h1"), ResourceId("h2")),
        inventory,
        store,
        datetime.now(UTC),
    ).self_confirm
    ran: list[OperatorProposal] = []

    report = autonomous_pass(store, inventory, ran.append, lambda _: lambda _: True)

    assert (proposal, "an agent proposal is approved only by a person") in (
        report.withheld
    )
    assert ran == []
    assert _record(project, proposal).status is ProposalStatus.PROPOSED
