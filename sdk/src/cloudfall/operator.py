"""Alert- and drift-driven operator loop: watch, diagnose, propose, verify.

The operator is deliberately deterministic. It never invents actions: every
proposal maps a declared trigger — a firing declared alert or an audited
drift — onto an existing engine entry point, carries the evidence that
justifies it, and waits for an explicit approval before anything mutates.
Outcomes are verified against the same evidence source the proposal came
from and every state change lands in a schema-validated receipt.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import time
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast

from cloudfall.audit import AuditStatus, audit_inventory
from cloudfall.domain import AlertSeverity, ResourceId
from cloudfall.lifecycle import LifecycleError, run_engine_playbook
from cloudfall.observation import load_observations

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

    from cloudfall.audit import AuditReport
    from cloudfall.inventory import OperatorPolicyInventory, PlatformInventory
    from cloudfall.lifecycle import EngineContext
    from cloudfall.validation import SchemaCatalog

_PROPOSAL_SCHEMA = "operator-proposal.schema.json"
_FINGERPRINT_LENGTH = 16
ERROR_FEED_UNREACHABLE = "operator_feed_unreachable"
ERROR_FEED_INVALID = "operator_feed_invalid"
ERROR_GATEWAY_MATERIAL_INVALID = "operator_gateway_material_invalid"
_ERROR_PROPOSAL_EXISTS = "operator_proposal_exists"
ERROR_PROPOSAL_MISSING = "operator_proposal_missing"
"""Code of the error ``ProposalStore`` raises for an id it holds no receipt for."""
_ERROR_PROPOSAL_NOT_OPEN = "operator_proposal_not_open"
_ERROR_OPERATION_UNSUPPORTED = "operator_operation_unsupported"
ERROR_GATEWAY_UNDECLARED = "operator_gateway_undeclared"
_SERVICE_CHECK_PREFIXES = ("services.bind[", "alerting.rules[")
_OUTCOME_DETAIL_MAX = 1000
"""``spec.outcome.detail`` ``maxLength`` in the proposal schema."""


class OperatorError(RuntimeError):
    """Structured operator failure suitable for CLI and agent consumers."""

    def __init__(self, code: str, message: str) -> None:
        """Capture a stable error code alongside the human message."""
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, object]:
        """Serialize the error envelope for system boundaries."""
        return {
            "status": "error",
            "error": {"code": self.code, "message": self.message},
        }


class ProposalStatus(StrEnum):
    """Receipted lifecycle of one operator proposal."""

    PROPOSED = "proposed"
    EXECUTED = "executed"
    VERIFIED = "verified"
    FAILED = "failed"


class TriggerKind(StrEnum):
    """What kind of evidence raised a proposal."""

    ALERT = "alert"
    DRIFT = "drift"


class OperationKind(StrEnum):
    """Existing engine entry points the operator may propose."""

    CONVERGE_SERVICES = "converge-services"
    CONVERGE_BASELINE = "converge-baseline"


_OPERATION_PLAYBOOKS: Mapping[OperationKind, str] = {
    OperationKind.CONVERGE_SERVICES: "services.yml",
    OperationKind.CONVERGE_BASELINE: "baseline.yml",
}

_BLOCKING_STATUSES = frozenset(
    {ProposalStatus.PROPOSED, ProposalStatus.EXECUTED, ProposalStatus.FAILED}
)


@dataclass(frozen=True, slots=True)
class OperatorAlert:
    """One firing declared alert with the labels the operator acts on."""

    name: str
    cloudfall_rule: ResourceId
    severity: AlertSeverity
    environment: ResourceId
    server: ResourceId
    service: ResourceId
    active_at: str
    fingerprint: str

    def as_dict(self) -> dict[str, object]:
        """Serialize the alert for the proposal receipt."""
        return {
            "name": self.name,
            "cloudfallRule": self.cloudfall_rule.value,
            "severity": self.severity.value,
            "environment": self.environment.value,
            "server": self.server.value,
            "service": self.service.value,
            "activeAt": self.active_at,
        }


@dataclass(frozen=True, slots=True)
class DriftTrigger:
    """One server's audited drift with the checks that failed."""

    server: ResourceId
    checks: tuple[str, ...]
    fingerprint: str

    def as_dict(self) -> dict[str, object]:
        """Serialize the drift trigger for the proposal receipt."""
        return {
            "server": self.server.value,
            "checks": list(self.checks),
        }


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """Who licensed an execution: a human confirm or a declared policy."""

    mode: str
    policy: ResourceId | None

    def as_dict(self) -> dict[str, object]:
        """Serialize the approval for the proposal receipt."""
        result: dict[str, object] = {"mode": self.mode}
        if self.policy is not None:
            result["policy"] = self.policy.value
        return result


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """What actually happened after an approval."""

    executed_at: str
    verified_at: str | None
    result: str
    detail: str

    def as_dict(self) -> dict[str, object]:
        """Serialize the outcome for the proposal receipt."""
        return {
            "executedAt": self.executed_at,
            "verifiedAt": self.verified_at,
            "result": self.result,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class OperatorProposal:
    """One evidence-backed remediation proposal."""

    resource_id: ResourceId
    created_at: str
    status: ProposalStatus
    trigger_kind: TriggerKind
    fingerprint: str
    alert: OperatorAlert | None
    drift: DriftTrigger | None
    diagnosis_summary: str
    evidence: tuple[str, ...]
    operation_kind: OperationKind
    operation_server: ResourceId
    operation_service: ResourceId | None
    command: tuple[str, ...]
    approval: ApprovalRecord | None
    outcome: ProposalOutcome | None

    def __post_init__(self) -> None:
        """Reject proposals whose trigger payload contradicts its kind."""
        has_alert = self.alert is not None
        expects_alert = self.trigger_kind is TriggerKind.ALERT
        if has_alert is not expects_alert or (self.drift is None) is not (
            self.trigger_kind is not TriggerKind.DRIFT
        ):
            message = (
                "proposal trigger payload does not match its kind: "
                f"{self.resource_id.value}"
            )
            raise ValueError(message)

    def as_document(self) -> dict[str, object]:
        """Serialize the proposal as a schema-valid receipt document."""
        trigger: dict[str, object] = {
            "kind": self.trigger_kind.value,
            "fingerprint": self.fingerprint,
        }
        if self.alert is not None:
            trigger["alert"] = self.alert.as_dict()
        if self.drift is not None:
            trigger["drift"] = self.drift.as_dict()
        operation: dict[str, object] = {
            "kind": self.operation_kind.value,
            "server": self.operation_server.value,
            "command": list(self.command),
        }
        if self.operation_service is not None:
            operation["service"] = self.operation_service.value
        spec: dict[str, object] = {
            "createdAt": self.created_at,
            "status": self.status.value,
            "trigger": trigger,
            "diagnosis": {
                "summary": self.diagnosis_summary,
                "evidence": list(self.evidence),
            },
            "operation": operation,
        }
        if self.approval is not None:
            spec["approval"] = self.approval.as_dict()
        if self.outcome is not None:
            spec["outcome"] = self.outcome.as_dict()
        return {
            "apiVersion": "cloudfall/v1",
            "kind": "OperatorProposal",
            "metadata": {
                "id": self.resource_id.value,
                "description": self.diagnosis_summary[:500],
            },
            "spec": spec,
        }


class AlertFeed(Protocol):
    """Source of firing declared alerts."""

    def fetch(self) -> tuple[OperatorAlert, ...]:
        """Return the currently firing actionable alerts."""
        ...


@dataclass(frozen=True, slots=True)
class GatewayAlertFeed:
    """mTLS client for the logging gateway's read-only alerts route."""

    url: str
    ca_path: Path
    certificate_path: Path
    key_path: Path
    timeout_seconds: float = 10.0

    def fetch(self) -> tuple[OperatorAlert, ...]:
        """Fetch firing alerts through the gateway."""
        try:
            context = ssl.create_default_context(cafile=str(self.ca_path))
            context.load_cert_chain(
                certfile=str(self.certificate_path), keyfile=str(self.key_path)
            )
        except OSError as error:
            message = (
                "gateway TLS material could not be loaded "
                f"(ca {self.ca_path}, certificate {self.certificate_path}, "
                f"key {self.key_path}): {error}"
            )
            raise OperatorError(ERROR_GATEWAY_MATERIAL_INVALID, message) from error
        request = urllib.request.Request(self.url)  # noqa: S310 - declared https gateway
        try:
            with urllib.request.urlopen(  # noqa: S310 - declared https gateway
                request, timeout=self.timeout_seconds, context=context
            ) as response:
                body = response.read()
        except OSError as error:
            message = f"alert feed unreachable: {self.url}: {error}"
            raise OperatorError(ERROR_FEED_UNREACHABLE, message) from error
        return parse_prometheus_alerts(body.decode("utf-8"))


def parse_prometheus_alerts(raw: str) -> tuple[OperatorAlert, ...]:
    """Parse a Prometheus alerts API payload into actionable alerts."""
    try:
        payload = cast("object", json.loads(raw))
    except json.JSONDecodeError as error:
        message = f"alert feed returned invalid JSON: {error.msg}"
        raise OperatorError(ERROR_FEED_INVALID, message) from error
    if not isinstance(payload, dict) or payload.get("status") != "success":
        message = "alert feed returned a non-success payload"
        raise OperatorError(ERROR_FEED_INVALID, message)
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("alerts"), list):
        message = "alert feed payload is missing data.alerts"
        raise OperatorError(ERROR_FEED_INVALID, message)
    alerts: list[OperatorAlert] = []
    for item in cast("list[object]", data["alerts"]):
        if not isinstance(item, dict):
            continue
        alert = _actionable_alert(cast("Mapping[str, object]", item))
        if alert is not None:
            alerts.append(alert)
    return tuple(alerts)


def _actionable_alert(item: Mapping[str, object]) -> OperatorAlert | None:
    if item.get("state") != "firing":
        return None
    labels = item.get("labels")
    if not isinstance(labels, dict):
        return None
    required = ("alertname", "cloudfall_rule", "severity", "environment",
                "server", "service")
    if not all(isinstance(labels.get(key), str) for key in required):
        return None
    active_at = item.get("activeAt")
    if not isinstance(active_at, str):
        return None
    return OperatorAlert(
        name=cast("str", labels["alertname"]),
        cloudfall_rule=ResourceId.from_boundary(labels["cloudfall_rule"]),
        severity=AlertSeverity.from_boundary(labels["severity"]),
        environment=ResourceId.from_boundary(labels["environment"]),
        server=ResourceId.from_boundary(labels["server"]),
        service=ResourceId.from_boundary(labels["service"]),
        active_at=active_at,
        fingerprint=fingerprint_of(cast("Mapping[str, object]", labels)),
    )


def fingerprint_of(content: Mapping[str, object]) -> str:
    """Return a stable short fingerprint over a trigger's identity."""
    canonical = json.dumps(
        {key: content[key] for key in sorted(content)},
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:_FINGERPRINT_LENGTH]


@dataclass(frozen=True, slots=True)
class SkippedAlert:
    """One firing alert the operator refused to act on, with the reason."""

    name: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        """Serialize for structured run output."""
        return {"name": self.name, "reason": self.reason}


def propose_for_alert(
    alert: OperatorAlert,
    inventory: PlatformInventory,
    now: str,
) -> OperatorProposal | SkippedAlert:
    """Map a firing alert onto a declared remediation, or refuse."""
    declared = next(
        (
            service
            for service in inventory.services
            if service.resource_id == alert.service
            and service.server_id == alert.server
        ),
        None,
    )
    if declared is None:
        reason = (
            f"service {alert.service.value} is not declared on "
            f"server {alert.server.value}"
        )
        return SkippedAlert(name=alert.name, reason=reason)
    summary = (
        f"Declared alert {alert.cloudfall_rule.value} is firing: service "
        f"{alert.service.value} on server {alert.server.value} stopped "
        "satisfying its rule; converging declared services restores the "
        "declared state"
    )
    evidence = (
        f"alert {alert.name} firing since {alert.active_at}",
        f"labels: severity={alert.severity.value} "
        f"environment={alert.environment.value} "
        f"server={alert.server.value} service={alert.service.value}",
        f"fingerprint {alert.fingerprint}",
    )
    return OperatorProposal(
        resource_id=_proposal_identifier(alert.fingerprint, now),
        created_at=now,
        status=ProposalStatus.PROPOSED,
        trigger_kind=TriggerKind.ALERT,
        fingerprint=alert.fingerprint,
        alert=alert,
        drift=None,
        diagnosis_summary=summary,
        evidence=evidence,
        operation_kind=OperationKind.CONVERGE_SERVICES,
        operation_server=alert.server,
        operation_service=alert.service,
        command=("cloudfall-engine", "playbook", "services.yml"),
        approval=None,
        outcome=None,
    )


def propose_for_drift(
    server: ResourceId,
    checks: tuple[str, ...],
    operation_kind: OperationKind,
    now: str,
) -> OperatorProposal:
    """Map one server's audited drift onto a convergence proposal."""
    fingerprint = fingerprint_of(
        {
            "server": server.value,
            "operation": operation_kind.value,
            "checks": ",".join(sorted(checks)),
        }
    )
    playbook = _OPERATION_PLAYBOOKS[operation_kind]
    summary = (
        f"Audit reports drift on server {server.value}: "
        f"{', '.join(checks)}; converging through {playbook} restores the "
        "declared state"
    )
    evidence = tuple(f"audit check drifted: {check}" for check in checks)
    drift = DriftTrigger(server=server, checks=checks, fingerprint=fingerprint)
    return OperatorProposal(
        resource_id=_proposal_identifier(fingerprint, now),
        created_at=now,
        status=ProposalStatus.PROPOSED,
        trigger_kind=TriggerKind.DRIFT,
        fingerprint=fingerprint,
        alert=None,
        drift=drift,
        diagnosis_summary=summary,
        evidence=evidence,
        operation_kind=operation_kind,
        operation_server=server,
        operation_service=None,
        command=("cloudfall-engine", "playbook", playbook),
        approval=None,
        outcome=None,
    )


def _proposal_identifier(fingerprint: str, now: str) -> ResourceId:
    stamp = (
        now.replace("-", "").replace(":", "").replace("+0000", "")
        .replace("t", "").replace("T", "").split(".")[0].lower()
    )
    return ResourceId.from_boundary(f"op-{stamp}-{fingerprint}")


@dataclass(frozen=True, slots=True)
class ProposalStore:
    """Schema-validated proposal receipts in one directory."""

    directory: Path
    catalog: SchemaCatalog

    def save(self, proposal: OperatorProposal) -> Path:
        """Persist a new proposal, refusing to overwrite an existing one."""
        path = self._path(proposal.resource_id)
        if path.exists():
            message = f"proposal already exists: {path}"
            raise OperatorError(_ERROR_PROPOSAL_EXISTS, message)
        return self._write(path, proposal)

    def update(self, proposal: OperatorProposal) -> Path:
        """Persist a state change of an existing proposal."""
        path = self._path(proposal.resource_id)
        if not path.exists():
            message = f"proposal does not exist: {path}"
            raise OperatorError(ERROR_PROPOSAL_MISSING, message)
        return self._write(path, proposal)

    def load(self, proposal_id: ResourceId) -> OperatorProposal:
        """Load and re-validate one proposal receipt."""
        path = self._path(proposal_id)
        if not path.is_file():
            message = f"proposal does not exist: {path}"
            raise OperatorError(ERROR_PROPOSAL_MISSING, message)
        document = cast(
            "dict[str, object]", json.loads(path.read_text(encoding="utf-8"))
        )
        self.catalog.validate_named(_PROPOSAL_SCHEMA, document)
        return _proposal_from_document(document)

    def list(self) -> tuple[OperatorProposal, ...]:
        """Load every proposal receipt ordered by creation time."""
        self.directory.mkdir(parents=True, exist_ok=True)
        proposals = [
            self.load(ResourceId.from_boundary(path.stem))
            for path in sorted(self.directory.glob("*.json"))
        ]
        proposals.sort(key=lambda proposal: proposal.created_at)
        return tuple(proposals)

    def blocking_fingerprints(self) -> frozenset[str]:
        """Fingerprints already covered by an open or failed proposal."""
        return frozenset(
            proposal.fingerprint
            for proposal in self.list()
            if proposal.status in _BLOCKING_STATUSES
        )

    def _path(self, proposal_id: ResourceId) -> Path:
        return self.directory / f"{proposal_id.value}.json"

    def _write(self, path: Path, proposal: OperatorProposal) -> Path:
        document = proposal.as_document()
        self.catalog.validate_named(_PROPOSAL_SCHEMA, document)
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )
        return path


def _proposal_from_document(document: Mapping[str, object]) -> OperatorProposal:
    metadata = cast("Mapping[str, object]", document["metadata"])
    spec = cast("Mapping[str, object]", document["spec"])
    trigger = cast("Mapping[str, object]", spec["trigger"])
    diagnosis = cast("Mapping[str, object]", spec["diagnosis"])
    operation = cast("Mapping[str, object]", spec["operation"])
    fingerprint = cast("str", trigger["fingerprint"])
    alert = None
    raw_alert = trigger.get("alert")
    if isinstance(raw_alert, dict):
        alert = OperatorAlert(
            name=cast("str", raw_alert["name"]),
            cloudfall_rule=ResourceId.from_boundary(raw_alert["cloudfallRule"]),
            severity=AlertSeverity.from_boundary(raw_alert["severity"]),
            environment=ResourceId.from_boundary(raw_alert["environment"]),
            server=ResourceId.from_boundary(raw_alert["server"]),
            service=ResourceId.from_boundary(raw_alert["service"]),
            active_at=cast("str", raw_alert["activeAt"]),
            fingerprint=fingerprint,
        )
    drift = None
    raw_drift = trigger.get("drift")
    if isinstance(raw_drift, dict):
        drift = DriftTrigger(
            server=ResourceId.from_boundary(raw_drift["server"]),
            checks=tuple(cast("list[str]", raw_drift["checks"])),
            fingerprint=fingerprint,
        )
    raw_approval = spec.get("approval")
    approval = None
    if isinstance(raw_approval, dict):
        raw_policy = raw_approval.get("policy")
        approval = ApprovalRecord(
            mode=cast("str", raw_approval["mode"]),
            policy=(
                ResourceId.from_boundary(raw_policy)
                if raw_policy is not None
                else None
            ),
        )
    raw_outcome = spec.get("outcome")
    outcome = None
    if isinstance(raw_outcome, dict):
        outcome = ProposalOutcome(
            executed_at=cast("str", raw_outcome["executedAt"]),
            verified_at=cast("str | None", raw_outcome["verifiedAt"]),
            result=cast("str", raw_outcome["result"]),
            detail=cast("str", raw_outcome["detail"]),
        )
    raw_service = operation.get("service")
    return OperatorProposal(
        resource_id=ResourceId.from_boundary(metadata["id"]),
        created_at=cast("str", spec["createdAt"]),
        status=ProposalStatus(cast("str", spec["status"])),
        trigger_kind=TriggerKind(cast("str", trigger["kind"])),
        fingerprint=fingerprint,
        alert=alert,
        drift=drift,
        diagnosis_summary=cast("str", diagnosis["summary"]),
        evidence=tuple(cast("list[str]", diagnosis["evidence"])),
        operation_kind=OperationKind(cast("str", operation["kind"])),
        operation_server=ResourceId.from_boundary(operation["server"]),
        operation_service=(
            ResourceId.from_boundary(raw_service)
            if raw_service is not None
            else None
        ),
        command=tuple(cast("list[str]", operation["command"])),
        approval=approval,
        outcome=outcome,
    )


@dataclass(frozen=True, slots=True)
class RunReport:
    """Structured result of one operator pass."""

    proposed: tuple[str, ...]
    skipped: tuple[SkippedAlert, ...]
    open_proposals: int

    def as_dict(self) -> dict[str, object]:
        """Serialize for structured CLI output."""
        return {
            "proposed": list(self.proposed),
            "skipped": [skipped.as_dict() for skipped in self.skipped],
            "openProposals": self.open_proposals,
        }


def run_once(
    feed: AlertFeed,
    inventory: PlatformInventory,
    store: ProposalStore,
    now: str | None = None,
) -> RunReport:
    """One watch pass: fetch alerts and propose for anything new."""
    timestamp = now if now is not None else _utc_now()
    blocking = store.blocking_fingerprints()
    proposed: list[str] = []
    skipped: list[SkippedAlert] = []
    for alert in feed.fetch():
        if alert.fingerprint in blocking:
            continue
        result = propose_for_alert(alert, inventory, timestamp)
        if isinstance(result, SkippedAlert):
            skipped.append(result)
            continue
        store.save(result)
        proposed.append(result.resource_id.value)
        blocking = blocking | {alert.fingerprint}
    return RunReport(
        proposed=tuple(proposed),
        skipped=tuple(skipped),
        open_proposals=_open_count(store),
    )


def drift_pass(
    auditor: Callable[[], AuditReport],
    store: ProposalStore,
    now: str | None = None,
) -> RunReport:
    """One drift pass: refresh observations, audit, propose for drift."""
    timestamp = now if now is not None else _utc_now()
    report = auditor()
    blocking = store.blocking_fingerprints()
    proposed: list[str] = []
    for server_audit in report.servers:
        drifted = tuple(
            check.check
            for check in server_audit.checks
            if check.status is AuditStatus.DRIFT
        )
        if not drifted:
            continue
        server = ResourceId.from_boundary(server_audit.server_id)
        service_checks = tuple(
            check
            for check in drifted
            if check.startswith(_SERVICE_CHECK_PREFIXES)
        )
        baseline_checks = tuple(
            check
            for check in drifted
            if not check.startswith(_SERVICE_CHECK_PREFIXES)
        )
        groups = (
            (OperationKind.CONVERGE_SERVICES, service_checks),
            (OperationKind.CONVERGE_BASELINE, baseline_checks),
        )
        for operation_kind, checks in groups:
            if not checks:
                continue
            proposal = propose_for_drift(server, checks, operation_kind, timestamp)
            if proposal.fingerprint in blocking:
                continue
            store.save(proposal)
            proposed.append(proposal.resource_id.value)
            blocking = blocking | {proposal.fingerprint}
    return RunReport(
        proposed=tuple(proposed),
        skipped=(),
        open_proposals=_open_count(store),
    )


def _open_count(store: ProposalStore) -> int:
    return sum(
        1
        for proposal in store.list()
        if proposal.status is ProposalStatus.PROPOSED
    )


def engine_executor(
    context: EngineContext,
) -> Callable[[OperatorProposal], None]:
    """Executor running the proposal's engine entry point."""

    def _execute(proposal: OperatorProposal) -> None:
        playbook = _OPERATION_PLAYBOOKS.get(proposal.operation_kind)
        if playbook is None:
            message = (
                "unsupported operation kind: "
                f"{proposal.operation_kind.value}"
            )
            raise OperatorError(_ERROR_OPERATION_UNSUPPORTED, message)
        run_engine_playbook(context, playbook, {})

    return _execute


def engine_auditor(
    context: EngineContext,
    inventory: PlatformInventory,
    observation_directory: Path,
) -> Callable[[], AuditReport]:
    """Auditor refreshing observations through the inspect playbook."""

    def _audit() -> AuditReport:
        run_engine_playbook(
            context,
            "inspect.yml",
            {
                "cloudfall_inspect_output_directory": str(
                    observation_directory.resolve()
                )
            },
        )
        observations = load_observations(
            observation_directory, context.schema_directory
        )
        return audit_inventory(inventory, observations)

    return _audit


def alert_resolution_verifier(
    feed: AlertFeed,
) -> Callable[[OperatorProposal], bool]:
    """Build a verifier passing once the proposal's alert stops firing."""

    def _verify(proposal: OperatorProposal) -> bool:
        firing = {alert.fingerprint for alert in feed.fetch()}
        return proposal.fingerprint not in firing

    return _verify


def drift_resolution_verifier(
    auditor: Callable[[], AuditReport],
) -> Callable[[OperatorProposal], bool]:
    """Build a verifier passing once the drifted checks are compliant."""

    def _verify(proposal: OperatorProposal) -> bool:
        if proposal.drift is None:
            return False
        report = auditor()
        server_audit = next(
            (
                candidate
                for candidate in report.servers
                if candidate.server_id == proposal.drift.server.value
            ),
            None,
        )
        if server_audit is None:
            return False
        still_drifting = {
            check.check
            for check in server_audit.checks
            if check.status is AuditStatus.DRIFT
        }
        return not (set(proposal.drift.checks) & still_drifting)

    return _verify


@dataclass(frozen=True, slots=True)
class ApproveOptions:
    """Verification behavior for an approval."""

    verify_timeout_seconds: float = 180.0
    poll_interval_seconds: float = 10.0
    sleep: Callable[[float], None] = field(default=time.sleep)


def approve(
    store: ProposalStore,
    proposal_id: ResourceId,
    executor: Callable[[OperatorProposal], None],
    verifier: Callable[[OperatorProposal], bool],
    options: ApproveOptions | None = None,
) -> OperatorProposal:
    """Execute a human-approved proposal and verify its trigger resolves."""
    proposal = store.load(proposal_id)
    if proposal.status is not ProposalStatus.PROPOSED:
        message = (
            f"proposal {proposal_id.value} is {proposal.status.value}, "
            "only proposed proposals can be approved"
        )
        raise OperatorError(_ERROR_PROPOSAL_NOT_OPEN, message)
    return _execute_and_verify(
        store,
        proposal,
        executor,
        verifier,
        ApprovalRecord(mode="human", policy=None),
        options,
    )


def _execution_failure_detail(error: OperatorError | LifecycleError) -> str:
    """Name the failure's code and keep the end of its message within the receipt cap.

    An engine failure carries the tail of the Ansible output, and the last
    lines, the failing task and the play recap, are the ones that say why.
    """
    head = f"execution failed: {error.code}: "
    message = str(error).removeprefix(f"{error.code}: ")
    if len(head) + len(message) <= _OUTCOME_DETAIL_MAX:
        return head + message
    marker = "..."
    return head + marker + message[-(_OUTCOME_DETAIL_MAX - len(head) - len(marker)) :]


def _execute_and_verify(  # noqa: PLR0913 - internal execution contract.
    store: ProposalStore,
    proposal: OperatorProposal,
    executor: Callable[[OperatorProposal], None],
    verifier: Callable[[OperatorProposal], bool],
    approval: ApprovalRecord,
    options: ApproveOptions | None = None,
) -> OperatorProposal:
    resolved_options = options if options is not None else ApproveOptions()
    executed_at = _utc_now()
    try:
        executor(proposal)
    except (OperatorError, LifecycleError) as error:
        failed = replace(
            proposal,
            status=ProposalStatus.FAILED,
            approval=approval,
            outcome=ProposalOutcome(
                executed_at=executed_at,
                verified_at=None,
                result="failed",
                detail=_execution_failure_detail(error),
            ),
        )
        store.update(failed)
        raise
    executed = replace(
        proposal,
        status=ProposalStatus.EXECUTED,
        approval=approval,
        outcome=ProposalOutcome(
            executed_at=executed_at,
            verified_at=None,
            result="failed",
            detail="executed, verification pending",
        ),
    )
    store.update(executed)
    waited = 0.0
    while True:
        if verifier(proposal):
            verified = replace(
                executed,
                status=ProposalStatus.VERIFIED,
                outcome=ProposalOutcome(
                    executed_at=executed_at,
                    verified_at=_utc_now(),
                    result="verified",
                    detail=(
                        "trigger evidence resolved after convergence; "
                        "observed outcome matches the declared state"
                    ),
                ),
            )
            store.update(verified)
            return verified
        if waited >= resolved_options.verify_timeout_seconds:
            failed = replace(
                executed,
                status=ProposalStatus.FAILED,
                outcome=ProposalOutcome(
                    executed_at=executed_at,
                    verified_at=_utc_now(),
                    result="failed",
                    detail=(
                        "trigger evidence unresolved after "
                        f"{int(resolved_options.verify_timeout_seconds)}s; "
                        "remediation did not restore the declared state"
                    ),
                ),
            )
            store.update(failed)
            return failed
        resolved_options.sleep(resolved_options.poll_interval_seconds)
        waited += resolved_options.poll_interval_seconds


@dataclass(frozen=True, slots=True)
class AutonomyDecision:
    """Whether a declared policy licenses one execution, and why."""

    granted: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        """Serialize for structured run output."""
        return {"granted": self.granted, "reason": self.reason}


def autonomy_decision(
    proposal: OperatorProposal,
    policy: OperatorPolicyInventory,
    store: ProposalStore,
    now: datetime,
) -> AutonomyDecision:
    """Decide whether the declared policy licenses this execution now."""
    grant = policy.grant_for(proposal.operation_kind.value)
    if grant is None:
        reason = (
            f"policy {policy.resource_id.value} does not grant autonomy "
            f"for {proposal.operation_kind.value}"
        )
        return AutonomyDecision(granted=False, reason=reason)
    history = [
        receipt
        for receipt in store.list()
        if receipt.operation_kind is proposal.operation_kind
        and receipt.resource_id != proposal.resource_id
        and receipt.status
        in (ProposalStatus.VERIFIED, ProposalStatus.FAILED)
    ]
    verified = sum(
        1
        for receipt in history
        if receipt.status is ProposalStatus.VERIFIED
    )
    if verified < grant.required_verified_runs.value:
        reason = (
            f"insufficient verified history for "
            f"{proposal.operation_kind.value}: {verified} of "
            f"{grant.required_verified_runs.value} required"
        )
        return AutonomyDecision(granted=False, reason=reason)
    if history and history[-1].status is ProposalStatus.FAILED:
        reason = (
            f"most recent {proposal.operation_kind.value} receipt failed; "
            "autonomy suspended until a human-approved run verifies"
        )
        return AutonomyDecision(granted=False, reason=reason)
    if policy.quiet_hours is not None and policy.quiet_hours.contains(
        now.hour * 60 + now.minute
    ):
        reason = (
            "quiet hours "
            f"{policy.quiet_hours.start.value}-"
            f"{policy.quiet_hours.end.value} are in effect"
        )
        return AutonomyDecision(granted=False, reason=reason)
    hour_ago = now.timestamp() - 3600
    recent_autonomous = sum(
        1
        for receipt in store.list()
        if receipt.approval is not None
        and receipt.approval.mode == "autonomous"
        and receipt.outcome is not None
        and datetime.fromisoformat(receipt.outcome.executed_at).timestamp()
        > hour_ago
    )
    if recent_autonomous >= policy.max_autonomous_per_hour.value:
        reason = (
            "rate limit reached: "
            f"{recent_autonomous} autonomous executions in the last hour "
            f"(policy allows {policy.max_autonomous_per_hour.value})"
        )
        return AutonomyDecision(granted=False, reason=reason)
    reason = (
        f"policy {policy.resource_id.value} grants "
        f"{proposal.operation_kind.value}: {verified} verified runs, "
        "rate limit and quiet hours clear"
    )
    return AutonomyDecision(granted=True, reason=reason)


@dataclass(frozen=True, slots=True)
class AutonomyReport:
    """Structured result of one autonomous execution pass."""

    executed: tuple[tuple[str, str], ...]
    withheld: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, object]:
        """Serialize for structured run output."""
        return {
            "executed": [
                {"proposal": proposal_id, "status": status}
                for proposal_id, status in self.executed
            ],
            "withheld": [
                {"proposal": proposal_id, "reason": reason}
                for proposal_id, reason in self.withheld
            ],
        }


def autonomous_pass(  # noqa: PLR0913 - boundary mirrors approve().
    store: ProposalStore,
    inventory: PlatformInventory,
    executor: Callable[[OperatorProposal], None],
    verifier_for: Callable[
        [OperatorProposal], Callable[[OperatorProposal], bool]
    ],
    options: ApproveOptions | None = None,
    now: datetime | None = None,
) -> AutonomyReport:
    """Execute open proposals the declared policy licenses, receipted."""
    moment = now if now is not None else datetime.now(UTC)
    policies_by_environment = {
        policy.environment: policy for policy in inventory.operator_policies
    }
    environments_by_server = {
        server.resource_id: server.environment for server in inventory.servers
    }
    executed: list[tuple[str, str]] = []
    withheld: list[tuple[str, str]] = []
    for proposal in store.list():
        if proposal.status is not ProposalStatus.PROPOSED:
            continue
        environment = environments_by_server.get(proposal.operation_server)
        policy = (
            policies_by_environment.get(environment)
            if environment is not None
            else None
        )
        if policy is None:
            withheld.append(
                (
                    proposal.resource_id.value,
                    "no operator policy declared for this environment",
                )
            )
            continue
        decision = autonomy_decision(proposal, policy, store, moment)
        if not decision.granted:
            withheld.append((proposal.resource_id.value, decision.reason))
            continue
        result = _execute_and_verify(
            store,
            proposal,
            executor,
            verifier_for(proposal),
            ApprovalRecord(mode="autonomous", policy=policy.resource_id),
            options,
        )
        executed.append((result.resource_id.value, result.status.value))
    return AutonomyReport(executed=tuple(executed), withheld=tuple(withheld))


def gateway_feed(
    inventory: PlatformInventory,
    ca_path: Path,
    certificate_path: Path,
    key_path: Path,
    url_override: str | None = None,
) -> GatewayAlertFeed:
    """Build the alert feed from the declared logging gateway."""
    url = url_override
    if url is None:
        stack = next(
            (
                stack
                for stack in inventory.logging_stacks
                if stack.alerting is not None
            ),
            None,
        )
        if stack is None:
            message = (
                "no LoggingStack declares alerting; pass an explicit "
                "gateway URL or declare an alerting block"
            )
            raise OperatorError(ERROR_GATEWAY_UNDECLARED, message)
        url = (
            f"https://{stack.gateway.server_name.value}:"
            f"{stack.gateway.port.value}/api/v1/alerts"
        )
    return GatewayAlertFeed(
        url=url,
        ca_path=ca_path,
        certificate_path=certificate_path,
        key_path=key_path,
    )


class PassKind(StrEnum):
    """What one pass of the watch loop looked at."""

    ALERTS = "alerts"
    DRIFT = "drift"
    AUTONOMY = "autonomy"


@dataclass(frozen=True, slots=True)
class PassReport:
    """One pass of the watch loop and what it found or did."""

    kind: PassKind
    report: RunReport | AutonomyReport

    @property
    def changed(self) -> bool:
        """The pass wrote a proposal or executed one."""
        if isinstance(self.report, AutonomyReport):
            return bool(self.report.executed)
        return bool(self.report.proposed)

    def as_dict(self) -> dict[str, object]:
        """Serialize for structured run output."""
        return {"pass": self.kind.value, **self.report.as_dict()}


@dataclass(frozen=True, slots=True)
class DriftCheck:
    """An audited drift pass, run at most once per ``interval_seconds``."""

    auditor: Callable[[], AuditReport]
    interval_seconds: float


def watch(  # noqa: PLR0913 - the loop's whole schedule is its boundary.
    feed: AlertFeed,
    inventory: PlatformInventory,
    store: ProposalStore,
    *,
    drift: DriftCheck | None,
    autonomy: Callable[[], AutonomyReport] | None,
    interval_seconds: float | None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[PassReport]:
    """Run watch passes until stopped, one report per pass.

    Each round runs the alerts pass, then the drift pass when it is due
    (the first round always runs it), then the autonomy pass when one is
    given. Without ``interval_seconds`` the loop stops after one round.
    """
    drift_due = clock()
    while True:
        yield PassReport(PassKind.ALERTS, run_once(feed, inventory, store))
        if drift is not None and clock() >= drift_due:
            yield PassReport(PassKind.DRIFT, drift_pass(drift.auditor, store))
            drift_due = clock() + drift.interval_seconds
        if autonomy is not None:
            yield PassReport(PassKind.AUTONOMY, autonomy())
        if interval_seconds is None:
            return
        sleep(interval_seconds)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
