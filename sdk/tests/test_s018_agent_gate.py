"""The operation classes of spec 018 and the plan's ``approval`` preview.

Over ``cloudfall mcp serve`` in a project, each server-changing tool runs one
operation class, the ``kind`` an ``OperatorPolicy`` grant names. A call
without its confirmation answers the plan, writes nothing, and the plan says
in ``approval`` whether a grant in every environment the run touches would
let the confirmed call run (S-018-10). The classes are additive: every
record and policy written before still validates, and the served tool names
and exit codes stay as they were (S-018-19).
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from cloudfall.agent_gate import GATED_TOOLS, check_grant, migrate_servers
from cloudfall.app import app
from cloudfall.decision import ApprovalChannel
from cloudfall.domain import ResourceId
from cloudfall.inventory import PlatformInventory
from cloudfall.operator import (
    ERROR_AGENT_PROPOSAL_UNSUPPORTED,
    AgentClient,
    AgentRequest,
    ApprovalMode,
    ApprovalRecord,
    OperationKind,
    OperatorError,
    OperatorProposal,
    ProposalBinding,
    ProposalOutcome,
    ProposalStatus,
    ProposalStore,
    TriggerKind,
    approve,
    autonomous_pass,
)
from cloudfall.validation import SchemaCatalog, validate_config
from examples_project import copy_examples
from jsonschema.exceptions import ValidationError
from test_agent_server import _project_with_artifact, _serve, _talk
from test_compatibility_surface import (
    EXIT_CODES,
    MCP_TOOLS,
    _check,
    _exit_codes,
    _listed,
    _tool_names,
)
from test_operator import ROOT, SCHEMAS

if TYPE_CHECKING:
    from pathlib import Path

EXAMPLES = ROOT / "config" / "examples"
RECORDS = ROOT / "config" / "tests" / "records"
RELEASE = "20260101T000000Z-abcdef0"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
NINE_CLASSES = {
    "converge-services",
    "converge-baseline",
    "deploy",
    "rollback",
    "restart",
    "migrate",
    "data-copy",
    "domains-apply",
    "converge-domains",
}
_POLICY = """\
---
apiVersion: cloudfall/v1
kind: OperatorPolicy
metadata:
  id: production-operator
  description: Grants restart in production after one verified run
spec:
  environment: production
  autonomy:
    operations:
      - kind: restart
        requiredVerifiedRuns: 1
    maxAutonomousPerHour: 4
"""


_TOOL_OF_CLASS = {kind: tool for tool, kind in GATED_TOOLS.items()}


def _example_policy() -> dict[str, Any]:
    import yaml  # noqa: PLC0415 - test-local.

    path = EXAMPLES / "operator-policies" / "production.yaml"
    document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document


def _agent_record(  # noqa: PLR0913 - one record's every varied field.
    identifier: str,
    kind: OperationKind,
    servers: tuple[str, ...],
    environments: tuple[str, ...],
    *,
    status: ProposalStatus = ProposalStatus.VERIFIED,
    approval: ApprovalRecord | None = None,
) -> OperatorProposal:
    """Build an agent record as a later issue will write one (S-018)."""
    settled = status in {ProposalStatus.VERIFIED, ProposalStatus.FAILED}
    return OperatorProposal(
        resource_id=ResourceId(identifier),
        created_at="2026-05-01T10:00:00+00:00",
        status=status,
        trigger_kind=TriggerKind.AGENT,
        fingerprint="0123456789abcdef",
        alert=None,
        drift=None,
        diagnosis_summary="The agent restarts crm-backend after a config change",
        evidence=("restart component crm-backend on h1, h2",),
        operation_kind=kind,
        operation_server=None,
        operation_service=None,
        command=("cloudfall", kind.value),
        approval=(
            approval
            if approval is not None
            else ApprovalRecord(
                mode=ApprovalMode.HUMAN, policy=None, via=ApprovalChannel.TERMINAL
            )
        ),
        outcome=(
            ProposalOutcome(
                executed_at="2026-05-01T10:01:00+00:00",
                verified_at="2026-05-01T10:02:00+00:00",
                result=status.value,
                detail="the health gate passed",
            )
            if settled
            else None
        ),
        request=AgentRequest(
            tool=_TOOL_OF_CLASS[kind],
            arguments={"component": "crm-backend"},
            reason="the config changed",
        ),
        operation_servers=tuple(ResourceId(server) for server in servers),
        operation_environments=tuple(
            ResourceId(environment) for environment in environments
        ),
        binding=ProposalBinding(
            plan_sha256="a" * 64, git_commit="b" * 40, worktree_digest="c" * 64
        ),
    )


def _store(project: Path) -> ProposalStore:
    return ProposalStore(
        directory=project / "record" / "operator" / "proposals",
        catalog=SchemaCatalog(SCHEMAS),
    )


def _inventory(project: Path) -> PlatformInventory:
    return PlatformInventory.from_state(validate_config(project, SCHEMAS))


def _add_staging_server(project: Path) -> None:
    """Declare h3, a server in staging, beside the examples' production ones."""
    h2 = (project / "servers" / "h2.yaml").read_text(encoding="utf-8")
    (project / "servers" / "h3.yaml").write_text(
        h2.replace("h2", "h3")
        .replace("environment: production", "environment: staging")
        .replace("203.0.113.12", "203.0.113.13")
        .replace("2001:db8:12::", "2001:db8:13::"),
        encoding="utf-8",
    )


def _files(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


# S-018-19: the classes are additive


def test_s018_19_the_policy_schema_accepts_each_of_the_nine_classes() -> None:
    catalog = SchemaCatalog(SCHEMAS)

    for kind in sorted(NINE_CLASSES):
        policy = _example_policy()
        policy["spec"]["autonomy"]["operations"] = [
            {"kind": kind, "requiredVerifiedRuns": 1}
        ]
        catalog.validate_named("operator-policy.schema.json", policy)

    policy = _example_policy()
    policy["spec"]["autonomy"]["operations"] = [
        {"kind": "converge-everything", "requiredVerifiedRuns": 1}
    ]
    with pytest.raises(ValidationError):
        catalog.validate_named("operator-policy.schema.json", policy)


def test_s018_19_the_model_and_both_schemas_name_the_same_nine_classes() -> None:
    policy = json.loads(
        (SCHEMAS / "operator-policy.schema.json").read_text(encoding="utf-8")
    )
    proposal = json.loads(
        (SCHEMAS / "operator-proposal.schema.json").read_text(encoding="utf-8")
    )
    operations = policy["properties"]["spec"]["properties"]["autonomy"]
    spec = proposal["properties"]["spec"]["properties"]

    assert {kind.value for kind in OperationKind} == NINE_CLASSES
    assert set(operations["properties"]["operations"]["items"]["properties"]["kind"][
        "enum"
    ]) == NINE_CLASSES
    assert set(spec["operation"]["properties"]["kind"]["enum"]) == NINE_CLASSES
    assert set(spec["approval"]["properties"]["grant"]["enum"]) == NINE_CLASSES
    assert {kind.value for kind in GATED_TOOLS.values()} == NINE_CLASSES


def test_s018_19_every_example_and_fixture_policy_and_proposal_still_validates(
    tmp_path: Path,
) -> None:
    catalog = SchemaCatalog(SCHEMAS)
    fixtures = [
        path
        for path in sorted(RECORDS.rglob("*.json"))
        if json.loads(path.read_text(encoding="utf-8"))["kind"] == "OperatorProposal"
    ]

    validate_config(EXAMPLES, SCHEMAS)
    catalog.validate_named("operator-policy.schema.json", _example_policy())
    assert fixtures
    store = _store(tmp_path)
    for fixture in fixtures:
        document = json.loads(fixture.read_text(encoding="utf-8"))
        catalog.validate_named("operator-proposal.schema.json", document)
        read = store.read(fixture)
        # Today's writer writes back the record it read, field for field.
        written: dict[str, Any] = read.as_document()
        spec = document["spec"]
        assert written["spec"]["operation"] == spec["operation"]
        assert written["spec"]["trigger"] == spec["trigger"]
        assert read.servers == (read.operation_server,)


def test_s018_19_an_alert_or_drift_record_still_needs_its_server() -> None:
    catalog = SchemaCatalog(SCHEMAS)
    fixture = RECORDS / "proposals" / "v0.2.0-alert-proposed.json"
    document = json.loads(fixture.read_text(encoding="utf-8"))
    del document["spec"]["operation"]["server"]

    with pytest.raises(ValidationError):
        catalog.validate_named("operator-proposal.schema.json", document)


def test_s018_19_an_agent_record_round_trips_through_the_store(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    record = _agent_record(
        "op-20260501100000-0123456789abcdef",
        OperationKind.DEPLOY,
        ("h1", "h2"),
        ("production",),
        approval=ApprovalRecord(
            mode=ApprovalMode.AGENT,
            policy=ResourceId("production-operator"),
            grant=OperationKind.DEPLOY,
            client=AgentClient(name="claude-code", version="2.1.0"),
        ),
    )

    path = store.save(record)
    document = json.loads(path.read_text(encoding="utf-8"))

    assert store.load(record.resource_id) == record
    assert document["spec"]["trigger"]["request"] == {
        "tool": "deploy",
        "arguments": {"component": "crm-backend"},
        "reason": "the config changed",
    }
    assert document["spec"]["operation"]["servers"] == ["h1", "h2"]
    assert "server" not in document["spec"]["operation"]
    assert document["spec"]["approval"] == {
        "mode": "agent",
        "policy": "production-operator",
        "grant": "deploy",
        "client": {"name": "claude-code", "version": "2.1.0"},
    }
    assert document["spec"]["binding"] == {
        "planSha256": "a" * 64,
        "gitCommit": "b" * 40,
        "worktreeDigest": "c" * 64,
    }


@pytest.mark.parametrize(
    "drop",
    [
        ("operation", "servers"),
        ("operation", "environments"),
        ("trigger", "request"),
        ("binding", None),
    ],
    ids=["servers", "environments", "request", "binding"],
)
def test_s018_19_an_agent_record_names_its_request_servers_and_binding(
    drop: tuple[str, str | None],
) -> None:
    document = _agent_record(
        "op-20260501100000-0123456789abcdef",
        OperationKind.RESTART,
        ("h1",),
        ("production",),
    ).as_document()
    spec = document["spec"]
    assert isinstance(spec, dict)
    section, field = drop
    if field is None:
        del spec[section]
    else:
        del spec[section][field]

    with pytest.raises(ValidationError):
        SchemaCatalog(SCHEMAS).validate_named(
            "operator-proposal.schema.json", document
        )


def test_a_blank_client_name_is_an_unreadable_record_not_a_crash(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    document = _agent_record(
        "op-20260501100000-0123456789abcdef",
        OperationKind.RESTART,
        ("h1",),
        ("production",),
        approval=ApprovalRecord(
            mode=ApprovalMode.AGENT,
            policy=ResourceId("production-operator"),
            grant=OperationKind.RESTART,
            client=AgentClient(name="unknown", version="unknown"),
        ),
    ).as_document()
    document["spec"]["approval"]["client"]["name"] = " "  # type: ignore[index]
    store.directory.mkdir(parents=True)
    (store.directory / "op-20260501100000-0123456789abcdef.json").write_text(
        json.dumps(document), encoding="utf-8"
    )

    listing = store.list()

    assert listing.records == ()
    assert len(listing.unreadable) == 1


def test_s018_19_the_cli_exit_codes_and_mcp_tool_names_are_unchanged(
    tmp_path: Path,
) -> None:
    project_tools = _tool_names("--project", str(EXAMPLES), cwd=tmp_path)

    assert _check(_listed(EXIT_CODES)["cloudfall"], _exit_codes(app), "cli") == []
    assert (
        _check(_listed(MCP_TOOLS)["config/examples"], project_tools, "project") == []
    )
    assert set(GATED_TOOLS) <= set(project_tools)


# S-018-10: the plan's approval preview


def test_s018_10_no_policy_for_an_environment_withholds_the_grant(
    tmp_path: Path,
) -> None:
    project = copy_examples(tmp_path / "project")
    (project / "operator-policies" / "production.yaml").unlink()

    check = check_grant(
        OperationKind.RESTART,
        (ResourceId("h1"), ResourceId("h2")),
        _inventory(project),
        _store(project),
        NOW,
    )

    assert check.self_confirm is False
    assert check.as_dict() == {
        "selfConfirm": False,
        "environments": [
            {
                "environment": "production",
                "policy": None,
                "granted": False,
                "reason": "no operator policy declared for environment production",
            }
        ],
    }


def test_s018_10_a_grant_with_its_verified_history_self_confirms(
    tmp_path: Path,
) -> None:
    project = copy_examples(tmp_path / "project")
    (project / "operator-policies" / "production.yaml").write_text(
        _POLICY, encoding="utf-8"
    )
    store = _store(project)
    inventory = _inventory(project)
    servers = (ResourceId("h1"), ResourceId("h2"))

    before = check_grant(OperationKind.RESTART, servers, inventory, store, NOW)
    store.save(
        _agent_record(
            "op-20260501100000-0123456789abcdef",
            OperationKind.RESTART,
            ("h1",),
            ("production",),
        )
    )
    after = check_grant(OperationKind.RESTART, servers, inventory, store, NOW)
    other_class = check_grant(OperationKind.DEPLOY, servers, inventory, store, NOW)

    assert before.self_confirm is False
    assert before.environments[0].reason == (
        "insufficient verified history for restart in production: 0 of 1 required"
    )
    assert after.self_confirm is True
    assert after.environments[0].policy == ResourceId("production-operator")
    assert other_class.self_confirm is False
    assert other_class.environments[0].reason == (
        "policy production-operator does not grant autonomy for deploy"
    )


def test_s018_10_one_environment_without_a_grant_withholds_the_whole_run(
    tmp_path: Path,
) -> None:
    project = copy_examples(tmp_path / "project")
    (project / "operator-policies" / "production.yaml").write_text(
        _POLICY, encoding="utf-8"
    )
    _add_staging_server(project)
    store = _store(project)
    store.save(
        _agent_record(
            "op-20260501100000-0123456789abcdef",
            OperationKind.RESTART,
            ("h1",),
            ("production",),
        )
    )

    check = check_grant(
        OperationKind.RESTART,
        (ResourceId("h1"), ResourceId("h3")),
        _inventory(project),
        store,
        NOW,
    )

    assert [entry.environment.value for entry in check.environments] == [
        "production",
        "staging",
    ]
    assert [entry.granted for entry in check.environments] == [True, False]
    assert check.self_confirm is False


@pytest.mark.parametrize(
    "case", ["suspended", "rate-limited", "unreadable", "other-environment"]
)
def test_s018_10_the_operators_rules_withhold_the_grant(
    tmp_path: Path, case: str
) -> None:
    project = copy_examples(tmp_path / "project")
    (project / "operator-policies" / "production.yaml").write_text(
        _POLICY.replace("maxAutonomousPerHour: 4", "maxAutonomousPerHour: 1"),
        encoding="utf-8",
    )
    _add_staging_server(project)
    store = _store(project)
    verified_in = "h3" if case == "other-environment" else "h1"
    store.save(
        _agent_record(
            "op-20260501100000-0123456789abcdef",
            OperationKind.RESTART,
            (verified_in,),
            ("production",),
        )
    )
    expected = {
        "suspended": "most recent restart receipt failed in production",
        "rate-limited": "rate limit reached: 1 autonomous executions",
        "unreadable": "proposal receipt(s) cannot be read",
        "other-environment": "0 of 1 required",
    }[case]
    if case == "suspended":
        store.save(
            _agent_record(
                "op-20260501110000-0123456789abcdef",
                OperationKind.RESTART,
                ("h1",),
                ("production",),
                status=ProposalStatus.FAILED,
            )
        )
    if case == "rate-limited":
        ran = replace_time(
            _agent_record(
                "op-20260601115000-0123456789abcdef",
                OperationKind.RESTART,
                ("h1",),
                ("production",),
                approval=ApprovalRecord(
                    mode=ApprovalMode.AGENT,
                    policy=ResourceId("production-operator"),
                    grant=OperationKind.RESTART,
                    client=AgentClient(name="unknown", version="unknown"),
                ),
            ),
            "2026-06-01T11:50:00+00:00",
        )
        store.save(ran)
    if case == "unreadable":
        (store.directory / "op-broken.json").write_text("{", encoding="utf-8")

    check = check_grant(
        OperationKind.RESTART, (ResourceId("h1"),), _inventory(project), store, NOW
    )

    assert check.self_confirm is False
    assert expected in check.environments[0].reason


def replace_time(record: OperatorProposal, executed_at: str) -> OperatorProposal:
    """Move a record's run to ``executed_at``."""
    assert record.outcome is not None
    return dataclasses.replace(
        record,
        outcome=dataclasses.replace(record.outcome, executed_at=executed_at),
    )


def test_s018_10_migrate_changes_the_servers_of_its_pending_steps(
    tmp_path: Path,
) -> None:
    inventory = _inventory(copy_examples(tmp_path / "project"))

    assert [server.value for server in migrate_servers(inventory, ["baseline"])] == [
        "h1",
        "h2",
    ]
    assert [
        server.value for server in migrate_servers(inventory, ["domains-tls"])
    ] == ["h2"]
    assert migrate_servers(inventory, ["build:crm-backend", "inspect", "audit"]) == ()
    with pytest.raises(ValueError, match="not one this release plans"):
        migrate_servers(inventory, ["teleport"])


def _approval(answer: dict[str, Any]) -> dict[str, Any]:
    assert answer["ok"] is True, answer
    approval = answer["data"]["approval"]
    assert isinstance(approval, dict)
    return approval


def test_s018_10_an_unconfirmed_call_writes_nothing_and_previews_the_grant(
    tmp_path: Path,
) -> None:
    project = _project_with_artifact(tmp_path, RELEASE)
    (project / "tmp" / "source.url").write_text(
        "postgresql://user:pw@db.example.internal/crm\n", encoding="utf-8"
    )
    component = {"component": "crm-backend", "release": RELEASE}
    before = _files(project)

    _, answers = _talk(
        _serve("--project", str(project), cwd=tmp_path),
        ("deploy", component),
        ("rollback", component),
        ("restart", {"component": "crm-backend"}),
        (
            "data_copy",
            {
                "service": "postgresql-main",
                "database": "crm",
                "source_url_file": "tmp/source.url",
            },
        ),
        ("domains_apply", {"domain": "crm-site"}),
        ("migrate", {"build": ["crm-backend=main"]}),
    )

    assert _files(project) == before
    classes = ("deploy", "rollback", "restart", "data-copy", "domains-apply", "migrate")
    for answer, kind in zip(answers, classes, strict=True):
        assert answer["meta"]["dry_run"] is True, answer
        approval = _approval(answer)
        assert approval["self_confirm"] is False, kind
        assert approval["environments"] == [
            {
                "environment": "production",
                "policy": "production-operator",
                "granted": False,
                "reason": (
                    f"policy production-operator does not grant autonomy for {kind}"
                ),
            }
        ]
    # S-008's release approval keeps its keys beside the grant's.
    deploy = _approval(answers[0])
    assert (deploy["policy"], deploy["status"]) == ("manual", "missing")
    assert _approval(answers[1])["policy"] == "manual"


def test_s018_10_self_confirm_is_true_exactly_when_the_grant_holds(
    tmp_path: Path,
) -> None:
    project = copy_examples(tmp_path / "project")
    (project / "operator-policies" / "production.yaml").write_text(
        _POLICY, encoding="utf-8"
    )
    _store(project).save(
        _agent_record(
            "op-20260501100000-0123456789abcdef",
            OperationKind.RESTART,
            ("h1",),
            ("production",),
        )
    )
    before = _files(project)

    _, (granted, withheld) = _talk(
        _serve("--project", str(project), cwd=tmp_path),
        ("restart", {"component": "crm-backend"}),
        ("domains_apply", {"domain": "crm-site"}),
    )
    expected = check_grant(
        OperationKind.RESTART,
        (ResourceId("h1"), ResourceId("h2")),
        _inventory(project),
        _store(project),
        datetime.now(UTC),
    )

    assert _files(project) == before
    assert _approval(granted)["self_confirm"] is expected.self_confirm is True
    assert _approval(granted)["environments"][0]["granted"] is True
    assert _approval(withheld)["self_confirm"] is False


def test_an_agent_proposal_never_runs_through_an_operator_executor(
    tmp_path: Path,
) -> None:
    project = copy_examples(tmp_path / "project")
    store = _store(project)
    proposal = _agent_record(
        "op-20260501100000-0123456789abcdef",
        OperationKind.CONVERGE_SERVICES,
        ("h1", "h2"),
        ("production",),
        status=ProposalStatus.PROPOSED,
    )
    proposal = dataclasses.replace(proposal, approval=None)
    store.save(proposal)
    ran: list[OperatorProposal] = []

    def executor(run: OperatorProposal) -> None:
        ran.append(run)

    report = autonomous_pass(
        store, _inventory(project), executor, lambda _: lambda _: True, now=NOW
    )
    with pytest.raises(OperatorError) as refused:
        approve(
            store,
            proposal.resource_id,
            executor,
            lambda _: True,
            via=ApprovalChannel.TERMINAL,
        )

    assert report.withheld == (
        (proposal.resource_id.value, "an agent proposal is approved only by a person"),
    )
    assert refused.value.code == ERROR_AGENT_PROPOSAL_UNSUPPORTED
    assert ran == []
    assert store.load(proposal.resource_id).status is ProposalStatus.PROPOSED


def test_an_agent_proposal_naming_one_server_is_still_withheld_by_the_pass(
    tmp_path: Path,
) -> None:
    project = copy_examples(tmp_path / "project")
    store = _store(project)
    # The schema and the model let an agent record name `server` too.
    proposal = dataclasses.replace(
        _agent_record(
            "op-20260501100000-0123456789abcdef",
            OperationKind.CONVERGE_SERVICES,
            ("h1",),
            ("production",),
            status=ProposalStatus.PROPOSED,
        ),
        approval=None,
        operation_server=ResourceId("h1"),
    )
    store.save(proposal)
    ran: list[OperatorProposal] = []

    report = autonomous_pass(
        store, _inventory(project), ran.append, lambda _: lambda _: True, now=NOW
    )

    assert report.withheld == (
        (proposal.resource_id.value, "an agent proposal is approved only by a person"),
    )
    assert ran == []


def test_s018_10_a_persons_command_shows_no_grant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cloudfall.cli import main  # noqa: PLC0415 - test-local.

    project = copy_examples(tmp_path / "project")

    exit_code = main(
        [
            *("restart", "crm-backend", "--project", str(project)),
            *("--schemas", str(SCHEMAS)),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0, payload
    assert payload["data"]["status"] == "plan"
    assert not payload["data"]["approval"]
