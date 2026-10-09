"""Operator propose-and-approve loop tests."""

from __future__ import annotations

import io
import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cloudfall.app import app
from cloudfall.audit import AuditCheck, AuditReport, AuditStatus, ServerAudit
from cloudfall.cli import main
from cloudfall.decision import ApprovalChannel
from cloudfall.domain import ResourceId
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import LifecycleError
from cloudfall.operator import (
    ApproveOptions,
    AutonomyReport,
    DriftCheck,
    GatewayAlertFeed,
    OperatorAlert,
    OperatorError,
    PassKind,
    ProposalStatus,
    ProposalStore,
    RunReport,
    SkippedAlert,
    TriggerKind,
    alert_resolution_verifier,
    approve,
    drift_pass,
    drift_resolution_verifier,
    fingerprint_of,
    parse_prometheus_alerts,
    run_once,
    watch,
)
from cloudfall.validation import SchemaCatalog, validate_config
from jsonschema.exceptions import ValidationError
from test_decision import _Terminal
from treaty import CommandPath

_FAST_APPROVE = ApproveOptions(
    verify_timeout_seconds=2.0,
    poll_interval_seconds=1.0,
    sleep=lambda _: None,
)

TERMINAL = ApprovalChannel.TERMINAL
ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"

_LABELS = {
    "alertname": "postgresql_down",
    "cloudfall_rule": "postgresql-down",
    "severity": "critical",
    "environment": "production",
    "server": "h1",
    "service": "postgresql-main",
    "instance": "postgresql:///var/run/postgresql:5432/postgres",
    "job": "integrations/postgres",
}


def _alerts_payload(state: str = "firing") -> str:
    return json.dumps(
        {
            "status": "success",
            "data": {
                "alerts": [
                    {
                        "labels": _LABELS,
                        "annotations": {"summary": "postgres is down"},
                        "state": state,
                        "activeAt": "2026-09-10T14:45:45Z",
                        "value": "0e+00",
                    }
                ]
            },
        }
    )


@dataclass
class FakeFeed:
    """Scripted alert feed: one payload per fetch, last repeats."""

    payloads: list[str] = field(default_factory=list)
    fetches: int = 0

    def fetch(self) -> tuple[OperatorAlert, ...]:
        """Return the alerts parsed from the next scripted payload."""
        index = min(self.fetches, len(self.payloads) - 1)
        self.fetches += 1
        return parse_prometheus_alerts(self.payloads[index])


def _inventory() -> PlatformInventory:
    return PlatformInventory.from_state(validate_config(EXAMPLES, SCHEMAS))


def _store(tmp_path: Path) -> ProposalStore:
    return ProposalStore(
        directory=tmp_path / "proposals", catalog=SchemaCatalog(SCHEMAS)
    )


def test_parse_prometheus_alerts_returns_firing_actionable_alerts() -> None:
    alerts = parse_prometheus_alerts(_alerts_payload())

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.name == "postgresql_down"
    assert alert.cloudfall_rule.value == "postgresql-down"
    assert alert.server.value == "h1"
    assert alert.service.value == "postgresql-main"
    assert alert.fingerprint == fingerprint_of(_LABELS)


def test_parse_prometheus_alerts_ignores_pending_alerts() -> None:
    assert parse_prometheus_alerts(_alerts_payload(state="pending")) == ()


def test_parse_prometheus_alerts_rejects_invalid_payload() -> None:
    with pytest.raises(OperatorError) as caught:
        parse_prometheus_alerts("{}")
    assert caught.value.code == "operator_feed_invalid"


def test_gateway_feed_reports_missing_tls_material(tmp_path: Path) -> None:
    feed = GatewayAlertFeed(
        url="https://127.0.0.1:9/api/v1/alerts",
        ca_path=tmp_path / "ca.crt",
        certificate_path=tmp_path / "operator.crt",
        key_path=tmp_path / "operator.key",
    )

    with pytest.raises(OperatorError) as caught:
        feed.fetch()

    assert caught.value.code == "operator_gateway_material_invalid"


def test_run_once_writes_a_schema_valid_proposal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    feed = FakeFeed(payloads=[_alerts_payload()])

    report = run_once(feed, _inventory(), store)

    assert isinstance(report, RunReport)
    assert len(report.proposed) == 1
    assert report.skipped == ()
    assert report.open_proposals == 1
    proposal = store.load(ResourceId.from_boundary(report.proposed[0]))
    assert proposal.status is ProposalStatus.PROPOSED
    assert proposal.alert is not None
    assert proposal.alert.service.value == "postgresql-main"
    assert proposal.operation_kind.value == "converge-services"
    assert "converging declared services" in proposal.diagnosis_summary


def test_run_once_does_not_duplicate_open_proposals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    feed = FakeFeed(payloads=[_alerts_payload()])

    first = run_once(feed, _inventory(), store)
    second = run_once(feed, _inventory(), store)

    assert len(first.proposed) == 1
    assert second.proposed == ()
    assert len(store.list()) == 1


def test_run_once_skips_undeclared_services(tmp_path: Path) -> None:
    labels = dict(_LABELS, service="mystery-db")
    payload = json.dumps(
        {
            "status": "success",
            "data": {
                "alerts": [
                    {
                        "labels": labels,
                        "state": "firing",
                        "activeAt": "2026-09-10T14:45:45Z",
                    }
                ]
            },
        }
    )
    store = _store(tmp_path)

    report = run_once(FakeFeed(payloads=[payload]), _inventory(), store)

    assert report.proposed == ()
    assert len(report.skipped) == 1
    assert isinstance(report.skipped[0], SkippedAlert)
    assert "not declared" in report.skipped[0].reason
    assert store.list() == ()


def test_approve_executes_and_verifies_resolution(tmp_path: Path) -> None:
    store = _store(tmp_path)
    resolved = json.dumps({"status": "success", "data": {"alerts": []}})
    feed = FakeFeed(payloads=[_alerts_payload(), resolved])
    report = run_once(feed, _inventory(), store)
    executed: list[str] = []

    result = approve(
        store,
        ResourceId.from_boundary(report.proposed[0]),
        lambda proposal: executed.append(proposal.resource_id.value),
        alert_resolution_verifier(feed),
        _FAST_APPROVE,
        via=TERMINAL,
    )

    assert executed == [report.proposed[0]]
    assert result.status is ProposalStatus.VERIFIED
    assert result.outcome is not None
    assert result.outcome.result == "verified"
    reloaded = store.load(result.resource_id)
    assert reloaded.status is ProposalStatus.VERIFIED


def test_approve_marks_unresolved_alerts_failed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    feed = FakeFeed(payloads=[_alerts_payload()])
    report = run_once(feed, _inventory(), store)

    result = approve(
        store,
        ResourceId.from_boundary(report.proposed[0]),
        lambda _proposal: None,
        alert_resolution_verifier(feed),
        _FAST_APPROVE,
        via=TERMINAL,
    )

    assert result.status is ProposalStatus.FAILED
    assert result.outcome is not None
    assert "unresolved" in result.outcome.detail


def test_approve_records_execution_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    feed = FakeFeed(payloads=[_alerts_payload()])
    report = run_once(feed, _inventory(), store)
    proposal_id = ResourceId.from_boundary(report.proposed[0])

    def _boom(_proposal: object) -> None:
        code = "operator_test_boom"
        message = "engine unavailable"
        raise OperatorError(code, message)

    with pytest.raises(OperatorError):
        approve(
            store,
            proposal_id,
            _boom,
            alert_resolution_verifier(feed),
            _FAST_APPROVE,
            via=TERMINAL,
        )

    failed = store.load(proposal_id)
    assert failed.status is ProposalStatus.FAILED
    assert failed.outcome is not None
    assert "execution failed" in failed.outcome.detail


def test_approve_records_a_long_engine_failure_by_its_tail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    feed = FakeFeed(payloads=[_alerts_payload()])
    report = run_once(feed, _inventory(), store)
    proposal_id = ResourceId.from_boundary(report.proposed[0])
    log = "TASK [Gathering Facts] " * 200 + "PLAY RECAP h1 unreachable=1"

    def _boom(_proposal: object) -> None:
        code = "lifecycle_execution_failed"
        message = f"run baseline.yml failed: {log}"
        raise LifecycleError(code, message)

    with pytest.raises(LifecycleError):
        approve(
            store,
            proposal_id,
            _boom,
            alert_resolution_verifier(feed),
            _FAST_APPROVE,
            via=TERMINAL,
        )

    failed = store.load(proposal_id)
    assert failed.status is ProposalStatus.FAILED
    assert failed.outcome is not None
    detail = failed.outcome.detail
    assert len(detail) == 1000
    assert detail.startswith("execution failed: lifecycle_execution_failed: ...")
    assert detail.endswith("PLAY RECAP h1 unreachable=1")


def test_approve_refuses_non_open_proposals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    resolved = json.dumps({"status": "success", "data": {"alerts": []}})
    feed = FakeFeed(payloads=[_alerts_payload(), resolved])
    report = run_once(feed, _inventory(), store)
    proposal_id = ResourceId.from_boundary(report.proposed[0])
    approve(
        store,
        proposal_id,
        lambda _proposal: None,
        alert_resolution_verifier(feed),
        _FAST_APPROVE,
        via=TERMINAL,
    )

    with pytest.raises(OperatorError) as caught:
        approve(
            store,
            proposal_id,
            lambda _proposal: None,
            alert_resolution_verifier(feed),
            _FAST_APPROVE,
            via=TERMINAL,
        )
    assert caught.value.code == "operator_proposal_not_open"


def _audit_report(*checks: tuple[str, AuditStatus]) -> AuditReport:
    server_checks = tuple(
        AuditCheck(
            check=name,
            status=status,
            desired={"declared": True},
            observed=None,
            message="synthetic",
        )
        for name, status in checks
    )
    statuses = {check.status for check in server_checks}
    status = (
        AuditStatus.DRIFT
        if AuditStatus.DRIFT in statuses
        else AuditStatus.COMPLIANT
    )
    server = ServerAudit(
        server_id="h1",
        server_type_id="debian-application",
        status=status,
        observation="synthetic",
        observed_at="2026-09-10T15:00:00Z",
        checks=server_checks,
    )
    return AuditReport(
        status=status, servers=(server,), unmatched_observations=()
    )


def test_drift_pass_splits_service_and_baseline_proposals(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    report = drift_pass(
        lambda: _audit_report(
            ("services.bind[postgresql-main]", AuditStatus.DRIFT),
            ("packages.required[curl]", AuditStatus.DRIFT),
            ("os.distribution", AuditStatus.COMPLIANT),
        ),
        store,
    )

    assert len(report.proposed) == 2
    proposals = store.list()
    kinds = {
        proposal.operation_kind.value: proposal for proposal in proposals
    }
    assert set(kinds) == {"converge-services", "converge-baseline"}
    services = kinds["converge-services"]
    assert services.trigger_kind is TriggerKind.DRIFT
    assert services.drift is not None
    assert services.drift.checks == ("services.bind[postgresql-main]",)
    baseline = kinds["converge-baseline"]
    assert baseline.drift is not None
    assert baseline.drift.checks == ("packages.required[curl]",)


def test_drift_pass_does_not_duplicate_open_proposals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    auditor = lambda: _audit_report(  # noqa: E731
        ("packages.required[curl]", AuditStatus.DRIFT)
    )

    first = drift_pass(auditor, store)
    second = drift_pass(auditor, store)

    assert len(first.proposed) == 1
    assert second.proposed == ()


def test_approve_drift_proposal_verifies_through_the_audit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    report = drift_pass(
        lambda: _audit_report(("packages.required[curl]", AuditStatus.DRIFT)),
        store,
    )
    proposal_id = ResourceId.from_boundary(report.proposed[0])
    executed: list[str] = []

    result = approve(
        store,
        proposal_id,
        lambda proposal: executed.append(proposal.operation_kind.value),
        drift_resolution_verifier(
            lambda: _audit_report(
                ("packages.required[curl]", AuditStatus.COMPLIANT)
            )
        ),
        _FAST_APPROVE,
        via=TERMINAL,
    )

    assert executed == ["converge-baseline"]
    assert result.status is ProposalStatus.VERIFIED


def test_approve_drift_proposal_fails_when_drift_persists(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    auditor = lambda: _audit_report(  # noqa: E731
        ("packages.required[curl]", AuditStatus.DRIFT)
    )
    report = drift_pass(auditor, store)

    result = approve(
        store,
        ResourceId.from_boundary(report.proposed[0]),
        lambda _proposal: None,
        drift_resolution_verifier(auditor),
        _FAST_APPROVE,
        via=TERMINAL,
    )

    assert result.status is ProposalStatus.FAILED


def test_cli_operator_list_emits_structured_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "operator",
            "list",
            "--project",
            str(EXAMPLES),
            "--schemas",
            str(SCHEMAS),
            "--proposals",
            str(tmp_path / "proposals"),
        ]
    )

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert exit_code == 0
    assert (document["ok"], document["error"], document["warnings"]) == (
        True,
        None,
        [],
    )
    assert document["data"] == {"status": "ok", "proposals": []}


def _operator_show(tmp_path: Path, proposal: str) -> list[str]:
    return [
        "operator",
        "show",
        proposal,
        "--project",
        str(EXAMPLES),
        "--schemas",
        str(SCHEMAS),
        "--proposals",
        str(tmp_path / "proposals"),
    ]


def test_cli_operator_show_wraps_the_proposal_in_the_result_envelope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path)
    report = run_once(FakeFeed(payloads=[_alerts_payload()]), _inventory(), store)
    proposal_id = report.proposed[0]

    exit_code = main(_operator_show(tmp_path, proposal_id))

    captured = capsys.readouterr()
    assert exit_code == 0
    document = json.loads(captured.out)
    assert document["data"] == {
        "status": "ok",
        "proposal": store.load(ResourceId.from_boundary(proposal_id)).as_document(),
    }


def test_cli_operator_errors_use_the_shared_error_envelope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(_operator_show(tmp_path, "ghost"))

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 5
    assert (document["ok"], document["data"]) == (False, None)
    assert document["error"]["code"] == "NOT_FOUND"
    assert document["error"]["context"]["code"] == "operator_proposal_missing"


def test_watch_without_an_interval_runs_one_round(tmp_path: Path) -> None:
    feed = FakeFeed(payloads=[_alerts_payload()])
    slept: list[float] = []

    passes = list(
        watch(
            feed,
            _inventory(),
            _store(tmp_path),
            drift=None,
            autonomy=None,
            interval_seconds=None,
            sleep=slept.append,
        )
    )

    assert [report.kind for report in passes] == [PassKind.ALERTS]
    assert passes[0].changed
    assert passes[0].as_dict()["pass"] == PassKind.ALERTS.value
    assert (feed.fetches, slept) == (1, [])


def test_watch_runs_drift_when_due_and_autonomy_every_round(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audits: list[int] = []

    def auditor() -> AuditReport:
        audits.append(len(audits))
        return _audit_report(("os.distribution", AuditStatus.COMPLIANT))

    ticks = itertools.count()
    passes = watch(
        FakeFeed(payloads=[_alerts_payload()]),
        _inventory(),
        store,
        drift=DriftCheck(auditor, interval_seconds=2),
        autonomy=lambda: AutonomyReport(executed=(), withheld=()),
        interval_seconds=1,
        clock=lambda: float(next(ticks)),
        sleep=lambda _: None,
    )

    kinds = [report.kind for report in itertools.islice(passes, 8)]

    assert kinds[:3] == [PassKind.ALERTS, PassKind.DRIFT, PassKind.AUTONOMY]
    assert kinds.count(PassKind.AUTONOMY) == kinds.count(PassKind.ALERTS)
    assert 1 < len(audits) < kinds.count(PassKind.ALERTS)


def test_cli_operator_run_refuses_unreadable_gateway_material(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.pem"

    exit_code = main(
        [
            "operator",
            "run",
            "--project",
            str(EXAMPLES),
            "--schemas",
            str(SCHEMAS),
            "--proposals",
            str(tmp_path / "proposals"),
            "--gateway-url",
            "https://127.0.0.1:9/api/v1/alerts",
            "--gateway-ca",
            str(missing),
            "--gateway-cert",
            str(missing),
            "--gateway-key",
            str(missing),
        ]
    )

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert exit_code == 4
    assert lines[-1]["error"]["code"] == "PRECONDITION"
    assert (
        lines[-1]["error"]["context"]["code"] == "operator_gateway_material_invalid"
    )


@pytest.mark.parametrize("flag", ["--interval", "--drift-interval"])
def test_cli_operator_run_refuses_a_non_positive_interval(
    flag: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "operator",
            "run",
            "--project",
            str(EXAMPLES),
            *("--gateway-ca", "ca.pem", "--gateway-cert", "c.pem"),
            *("--gateway-key", "k.pem", flag, "0"),
            "--proposals",
            str(tmp_path / "proposals"),
        ]
    )

    document = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert exit_code == 2
    assert flag.removeprefix("--") in json.dumps(document["error"])


def _policied_inventory() -> PlatformInventory:
    inventory = _inventory()
    assert len(inventory.operator_policies) == 1
    return inventory


def _seed_verified_drift(
    store: ProposalStore, check: str, kind: str = "converge-baseline"
) -> None:
    from cloudfall.operator import (  # noqa: PLC0415 - test-local.
        OperationKind,
        propose_for_drift,
    )

    proposal = propose_for_drift(
        ResourceId.from_boundary("h1"),
        (check,),
        OperationKind(kind),
        "2026-09-10T10:00:00+00:00",
    )
    store.save(proposal)
    approve(
        store,
        proposal.resource_id,
        lambda _proposal: None,
        lambda _proposal: True,
        _FAST_APPROVE,
        via=TERMINAL,
    )


def test_autonomy_withheld_without_verified_history(tmp_path: Path) -> None:
    from datetime import UTC, datetime  # noqa: PLC0415 - test-local.

    from cloudfall.operator import autonomous_pass  # noqa: PLC0415

    store = _store(tmp_path)
    drift_pass(
        lambda: _audit_report(("packages.required[curl]", AuditStatus.DRIFT)),
        store,
    )

    report = autonomous_pass(
        store,
        _policied_inventory(),
        lambda _proposal: None,
        lambda _proposal: lambda _p: True,
        _FAST_APPROVE,
        now=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )

    assert report.executed == ()
    assert len(report.withheld) == 1
    assert "insufficient verified history" in report.withheld[0][1]


def test_autonomy_executes_once_history_is_earned(tmp_path: Path) -> None:
    from datetime import UTC, datetime  # noqa: PLC0415 - test-local.

    from cloudfall.operator import autonomous_pass  # noqa: PLC0415

    store = _store(tmp_path)
    _seed_verified_drift(store, "packages.required[git]")
    _seed_verified_drift(store, "packages.required[rsync]")
    drift_pass(
        lambda: _audit_report(("packages.required[curl]", AuditStatus.DRIFT)),
        store,
    )
    executed: list[str] = []

    report = autonomous_pass(
        store,
        _policied_inventory(),
        lambda proposal: executed.append(proposal.operation_kind.value),
        lambda _proposal: lambda _p: True,
        _FAST_APPROVE,
        now=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )

    assert executed == ["converge-baseline"]
    assert len(report.executed) == 1
    proposal_id, status = report.executed[0]
    assert status == "verified"
    receipt = store.load(ResourceId.from_boundary(proposal_id))
    assert receipt.approval is not None
    assert receipt.approval.mode == "autonomous"
    assert receipt.approval.policy is not None
    assert receipt.approval.policy.value == "production-operator"


def _autonomous_receipt(tmp_path: Path) -> tuple[ProposalStore, Path]:
    from datetime import UTC, datetime  # noqa: PLC0415 - test-local.

    from cloudfall.operator import autonomous_pass  # noqa: PLC0415

    store = _store(tmp_path)
    _seed_verified_drift(store, "packages.required[git]")
    _seed_verified_drift(store, "packages.required[rsync]")
    drift_pass(
        lambda: _audit_report(("packages.required[curl]", AuditStatus.DRIFT)),
        store,
    )
    report = autonomous_pass(
        store,
        _policied_inventory(),
        lambda _proposal: None,
        lambda _proposal: lambda _p: True,
        _FAST_APPROVE,
        now=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )
    proposal_id, _status = report.executed[0]
    return store, tmp_path / "proposals" / f"{proposal_id}.json"


def test_an_autonomous_approval_is_written_and_loaded_without_a_channel(
    tmp_path: Path,
) -> None:
    store, path = _autonomous_receipt(tmp_path)

    document = json.loads(path.read_text(encoding="utf-8"))
    loaded = store.load(ResourceId.from_boundary(path.stem))

    assert "via" not in document["spec"]["approval"]
    assert loaded.approval is not None
    assert (loaded.approval.mode, loaded.approval.via) == ("autonomous", None)


@pytest.mark.parametrize("via", ["terminal", "unknown"])
def test_an_autonomous_approval_with_a_channel_is_refused_on_load(
    tmp_path: Path, via: str
) -> None:
    """A policy's approval has no channel; a receipt claiming one does not load."""
    store, path = _autonomous_receipt(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["spec"]["approval"]["via"] = via
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValidationError) as caught:
        store.load(ResourceId.from_boundary(path.stem))

    assert list(caught.value.absolute_path) == ["spec", "approval"]


def test_autonomy_respects_quiet_hours(tmp_path: Path) -> None:
    from datetime import UTC, datetime  # noqa: PLC0415 - test-local.

    from cloudfall.operator import autonomous_pass  # noqa: PLC0415

    store = _store(tmp_path)
    _seed_verified_drift(store, "packages.required[git]")
    _seed_verified_drift(store, "packages.required[rsync]")
    drift_pass(
        lambda: _audit_report(("packages.required[curl]", AuditStatus.DRIFT)),
        store,
    )

    report = autonomous_pass(
        store,
        _policied_inventory(),
        lambda _proposal: None,
        lambda _proposal: lambda _p: True,
        _FAST_APPROVE,
        now=datetime(2026, 9, 10, 2, 30, tzinfo=UTC),
    )

    assert report.executed == ()
    assert "quiet hours" in report.withheld[0][1]


def test_autonomy_suspends_after_a_failed_receipt(tmp_path: Path) -> None:
    from datetime import UTC, datetime  # noqa: PLC0415 - test-local.

    from cloudfall.operator import (  # noqa: PLC0415 - test-local.
        OperationKind,
        autonomous_pass,
        propose_for_drift,
    )

    store = _store(tmp_path)
    _seed_verified_drift(store, "packages.required[git]")
    _seed_verified_drift(store, "packages.required[rsync]")
    failing = propose_for_drift(
        ResourceId.from_boundary("h1"),
        ("packages.required[acl]",),
        OperationKind.CONVERGE_BASELINE,
        "2026-09-10T11:00:00+00:00",
    )
    store.save(failing)
    approve(
        store,
        failing.resource_id,
        lambda _proposal: None,
        lambda _proposal: False,
        _FAST_APPROVE,
        via=TERMINAL,
    )
    drift_pass(
        lambda: _audit_report(("packages.required[curl]", AuditStatus.DRIFT)),
        store,
    )

    report = autonomous_pass(
        store,
        _policied_inventory(),
        lambda _proposal: None,
        lambda _proposal: lambda _p: True,
        _FAST_APPROVE,
        now=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
    )

    assert report.executed == ()
    assert "receipt failed" in report.withheld[0][1]


def test_autonomy_enforces_the_rate_limit(tmp_path: Path) -> None:
    from dataclasses import replace as dc_replace  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415 - test-local.

    from cloudfall.domain import PositiveCount  # noqa: PLC0415
    from cloudfall.inventory import (  # noqa: PLC0415 - test-local.
        AutonomyGrant,
    )
    from cloudfall.operator import autonomous_pass  # noqa: PLC0415

    inventory = _policied_inventory()
    policy = dc_replace(
        inventory.operator_policies[0],
        grants=(
            AutonomyGrant(
                operation_kind="converge-baseline",
                required_verified_runs=PositiveCount.from_boundary(1),
            ),
        ),
        max_autonomous_per_hour=PositiveCount.from_boundary(1),
        quiet_hours=None,
    )
    inventory = dc_replace(inventory, operator_policies=(policy,))
    store = _store(tmp_path)
    _seed_verified_drift(store, "packages.required[git]")
    drift_pass(
        lambda: _audit_report(("packages.required[curl]", AuditStatus.DRIFT)),
        store,
    )

    first = autonomous_pass(
        store,
        inventory,
        lambda _proposal: None,
        lambda _proposal: lambda _p: True,
        _FAST_APPROVE,
        now=datetime.now(UTC),
    )
    drift_pass(
        lambda: _audit_report(("packages.required[unzip]", AuditStatus.DRIFT)),
        store,
    )
    second = autonomous_pass(
        store,
        inventory,
        lambda _proposal: None,
        lambda _proposal: lambda _p: True,
        _FAST_APPROVE,
        now=datetime.now(UTC),
    )

    assert len(first.executed) == 1
    assert second.executed == ()
    assert "rate limit reached" in second.withheld[0][1]


def _drift_proposal(tmp_path: Path) -> ResourceId:
    report = drift_pass(
        lambda: _audit_report(("packages.required[curl]", AuditStatus.DRIFT)),
        _store(tmp_path),
    )
    return ResourceId.from_boundary(report.proposed[0])


def _operator_approve(tmp_path: Path, proposal: str, *flags: str) -> list[str]:
    return [
        *("operator", "approve", proposal),
        *("--project", str(EXAMPLES), "--schemas", str(SCHEMAS)),
        *("--proposals", str(tmp_path / "proposals")),
        *flags,
    ]


def _approve_at(
    tmp_path: Path, proposal: str, typed: str, *, stdin_tty: bool, stdout_tty: bool
) -> tuple[int, dict[str, object]]:
    """Run ``operator approve`` with stdin and stdout each a terminal or not."""
    stdout = _Terminal() if stdout_tty else io.StringIO()
    stdin = _Terminal(f"{typed}\n") if stdin_tty else io.StringIO(f"{typed}\n")
    exit_code = app.run(
        _operator_approve(tmp_path, proposal, "--format", "json"),
        stdin=stdin,
        stdout=stdout,
        stderr=io.StringIO(),
    )
    return exit_code, json.loads(stdout.getvalue())


def _still_proposed(tmp_path: Path, proposal_id: ResourceId) -> None:
    loaded = _store(tmp_path).load(proposal_id)
    assert loaded.status is ProposalStatus.PROPOSED
    assert loaded.approval is None
    assert loaded.outcome is None


@pytest.mark.parametrize("flags", [["--yes"], []], ids=["yes", "bare"])
def test_cli_operator_approve_off_a_terminal_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    """An agent's shell call cannot approve its own proposal, ``--yes`` or not (#35)."""
    proposal_id = _drift_proposal(tmp_path)

    exit_code = main(_operator_approve(tmp_path, proposal_id.value, *flags))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["code"] == "PERSON_REQUIRED"
    assert payload["error"]["retryable"] is False
    _still_proposed(tmp_path, proposal_id)


def test_cli_operator_approve_raw_payload_cannot_approve_off_a_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proposal_id = _drift_proposal(tmp_path)
    payload_in = {
        "proposal": proposal_id.value,
        "project": str(EXAMPLES),
        "schemas": str(SCHEMAS),
        "proposals": str(tmp_path / "proposals"),
        "yes": True,
    }

    exit_code = main(["operator", "approve", "--raw-payload", json.dumps(payload_in)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["code"] == "PERSON_REQUIRED"
    _still_proposed(tmp_path, proposal_id)


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty"),
    [(True, False), (False, True)],
    ids=["stdin-only", "stdout-only"],
)
def test_cli_operator_approve_needs_a_whole_terminal(
    tmp_path: Path, *, stdin_tty: bool, stdout_tty: bool
) -> None:
    proposal_id = _drift_proposal(tmp_path)

    exit_code, payload = _approve_at(
        tmp_path,
        proposal_id.value,
        proposal_id.value,
        stdin_tty=stdin_tty,
        stdout_tty=stdout_tty,
    )

    error = payload["error"]
    assert isinstance(error, dict)
    assert exit_code == 4
    assert error["code"] == "PERSON_REQUIRED"
    _still_proposed(tmp_path, proposal_id)


def test_cli_operator_approve_with_a_mistyped_id_runs_nothing(tmp_path: Path) -> None:
    proposal_id = _drift_proposal(tmp_path)

    exit_code, payload = _approve_at(
        tmp_path, proposal_id.value, "yes", stdin_tty=True, stdout_tty=True
    )

    error = payload["error"]
    assert isinstance(error, dict)
    assert exit_code == 4
    assert error["code"] == "ATTESTATION_MISMATCH"
    _still_proposed(tmp_path, proposal_id)


def test_cli_operator_approve_runs_once_a_person_typed_the_id(tmp_path: Path) -> None:
    """The typed id lets the playbook run, and the receipt says it came by terminal.

    The example hosts do not resolve, so the run fails fast at the engine step;
    what matters is that it ran and that the approval was recorded.
    """
    proposal_id = _drift_proposal(tmp_path)

    exit_code, payload = _approve_at(
        tmp_path, proposal_id.value, proposal_id.value, stdin_tty=True, stdout_tty=True
    )

    error = payload["error"]
    assert isinstance(error, dict)
    assert exit_code != 4
    assert error["code"] == "ENGINE_STEP_FAILED"
    loaded = _store(tmp_path).load(proposal_id)
    assert loaded.status is ProposalStatus.FAILED
    assert loaded.approval is not None
    assert (loaded.approval.mode, loaded.approval.via) == ("human", TERMINAL)


def test_cli_operator_approve_keeps_its_gateway_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An alert proposal still names the gateway flags it needs, and takes them."""
    run_once(FakeFeed(payloads=[_alerts_payload()]), _inventory(), _store(tmp_path))
    (proposal,) = _store(tmp_path).list()
    material = tmp_path / "material.pem"
    material.write_text("not a certificate\n", encoding="utf-8")

    missing = main(_operator_approve(tmp_path, proposal.resource_id.value))
    refused = json.loads(capsys.readouterr().out)
    given = main(
        _operator_approve(
            tmp_path,
            proposal.resource_id.value,
            *("--gateway-url", "https://127.0.0.1:9/api/v1/alerts"),
            *("--gateway-ca", str(material), "--gateway-cert", str(material)),
            *("--gateway-key", str(material)),
        )
    )
    asked = json.loads(capsys.readouterr().out)

    assert missing == 4
    assert refused["error"]["context"]["code"] == "operator_gateway_material_missing"
    assert given == 4
    assert asked["error"]["code"] == "PERSON_REQUIRED"
    _still_proposed(tmp_path, proposal.resource_id)


def test_the_operator_approval_command_is_a_persons() -> None:
    command = app.commands[CommandPath("operator.approve")]

    assert command.requires_person is True
    assert command.mcp is False


def test_a_human_approval_records_that_it_came_through_a_terminal(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    proposal_id = _drift_proposal(tmp_path)

    approve(
        store,
        proposal_id,
        lambda _proposal: None,
        lambda _proposal: True,
        _FAST_APPROVE,
        via=TERMINAL,
    )

    loaded = store.load(proposal_id)
    assert loaded.approval is not None
    assert (loaded.approval.mode, loaded.approval.via) == ("human", TERMINAL)
    on_disk = json.loads(
        (tmp_path / "proposals" / f"{proposal_id.value}.json").read_text(
            encoding="utf-8"
        )
    )
    assert on_disk["spec"]["approval"] == {"mode": "human", "via": "terminal"}


def test_a_human_approval_written_before_the_channel_reads_as_unknown(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    proposal_id = _drift_proposal(tmp_path)
    approve(
        store,
        proposal_id,
        lambda _proposal: None,
        lambda _proposal: True,
        _FAST_APPROVE,
        via=TERMINAL,
    )
    path = tmp_path / "proposals" / f"{proposal_id.value}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["spec"]["approval"]["via"]
    path.write_text(json.dumps(document), encoding="utf-8")

    loaded = store.load(proposal_id)

    assert loaded.approval is not None
    assert loaded.approval.via is ApprovalChannel.UNKNOWN


def test_a_new_operator_approval_names_how_it_arrived(tmp_path: Path) -> None:
    proposal_id = _drift_proposal(tmp_path)

    with pytest.raises(OperatorError) as caught:
        approve(
            _store(tmp_path),
            proposal_id,
            lambda _proposal: None,
            lambda _proposal: True,
            _FAST_APPROVE,
            via=ApprovalChannel.UNKNOWN,
        )

    assert caught.value.code == "operator_approval_channel_unknown"
    _still_proposed(tmp_path, proposal_id)
