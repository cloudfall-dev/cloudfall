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
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, cast

from jsonschema.exceptions import ValidationError

from cloudfall import record_files
from cloudfall.audit import AuditStatus, audit_inventory
from cloudfall.decision import ApprovalChannel, Interruption
from cloudfall.domain import AlertSeverity, ResourceId
from cloudfall.lifecycle import (
    LifecycleError,
    converge_services,
    run_engine_playbook,
)
from cloudfall.observation import load_observations
from cloudfall.secrets import OwnedRoleRecord

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

    from cloudfall.audit import AuditReport
    from cloudfall.inventory import OperatorPolicyInventory, PlatformInventory
    from cloudfall.lifecycle import EngineContext
    from cloudfall.secrets import SopsSecretProvider
    from cloudfall.validation import SchemaCatalog

type Executor = Callable[[OperatorProposal], tuple[OwnedRoleRecord, ...] | None]
"""Runs one proposal's operation; returns the owner passwords it set, if any."""

_API_VERSION = "cloudfall/v1"
_PROPOSAL_SCHEMA = "operator-proposal.schema.json"
PROPOSAL_SCHEMAS: Mapping[str, str] = MappingProxyType(
    {_API_VERSION: _PROPOSAL_SCHEMA}
)
"""The schema a receipt is read with, by its ``apiVersion``: a v2 sits beside v1."""
ERROR_RECORD_INVALID = "operator_record_invalid"
"""Code of the error for a receipt that names no schema this Cloudfall reads."""
_FINGERPRINT_LENGTH = 16
ERROR_FEED_UNREACHABLE = "operator_feed_unreachable"
ERROR_FEED_INVALID = "operator_feed_invalid"
ERROR_GATEWAY_MATERIAL_INVALID = "operator_gateway_material_invalid"
_ERROR_PROPOSAL_EXISTS = "operator_proposal_exists"
ERROR_PROPOSAL_MISSING = "operator_proposal_missing"
"""Code of the error ``ProposalStore`` raises for an id it holds no receipt for."""
_ERROR_PROPOSAL_NOT_OPEN = "operator_proposal_not_open"
ERROR_PROPOSAL_INTERRUPTED = "operator_proposal_run_interrupted"
"""Code of the refusal of a proposal whose run started and recorded no outcome."""
ERROR_PROPOSAL_NOT_RUNNING = "operator_proposal_not_running"
"""Code of the refusal to close a proposal as interrupted that is not running."""
_NOT_OPEN_CODES = frozenset({_ERROR_PROPOSAL_NOT_OPEN, ERROR_PROPOSAL_INTERRUPTED})
_ERROR_CHANNEL_UNKNOWN = "operator_approval_channel_unknown"
_ERROR_OPERATION_UNSUPPORTED = "operator_operation_unsupported"
ERROR_GATEWAY_UNDECLARED = "operator_gateway_undeclared"
ERROR_AGENT_PROPOSAL_UNSUPPORTED = "operator_agent_proposal_unsupported"
"""Code of the refusal to run an agent proposal from the store (S-018)."""
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
    RUNNING = "running"
    """Approved, and its execution started; no outcome is recorded yet.

    Recorded before the executor runs, so an execution whose process ended
    before it recorded an outcome stays ``running``: it was interrupted, and
    it is never approved again.
    """

    INTERRUPTED = "interrupted"
    """A ``running`` receipt a person closed once they confirmed it is dead.

    It no longer blocks a new proposal for the same trigger.
    """


class TriggerKind(StrEnum):
    """What kind of evidence raised a proposal."""

    ALERT = "alert"
    DRIFT = "drift"
    AGENT = "agent"
    """An agent's call of a gated MCP tool in a project (S-018)."""


class OperationKind(StrEnum):
    """The operation classes an ``OperatorPolicy`` grant names.

    The operator proposes the two converge kinds; the others are the classes
    of the server-changing MCP tools of a project (S-018).
    """

    CONVERGE_SERVICES = "converge-services"
    CONVERGE_BASELINE = "converge-baseline"
    DEPLOY = "deploy"
    ROLLBACK = "rollback"
    RESTART = "restart"
    MIGRATE = "migrate"
    DATA_COPY = "data-copy"
    DOMAINS_APPLY = "domains-apply"
    CONVERGE_DOMAINS = "converge-domains"


class ApprovalMode(StrEnum):
    """Who licensed an execution."""

    HUMAN = "human"
    AUTONOMOUS = "autonomous"
    """The operator's autonomous pass, on a policy grant."""
    AGENT = "agent"
    """An agent's own confirmed call, on a policy grant (S-018)."""


_RATE_LIMITED_MODES = frozenset({ApprovalMode.AUTONOMOUS, ApprovalMode.AGENT})
"""Approvals a policy's ``maxAutonomousPerHour`` counts: those no person gave."""


_OPERATION_PLAYBOOKS: Mapping[OperationKind, str] = {
    OperationKind.CONVERGE_SERVICES: "services.yml",
    OperationKind.CONVERGE_BASELINE: "baseline.yml",
}

_BLOCKING_STATUSES = frozenset(
    {
        ProposalStatus.PROPOSED,
        ProposalStatus.RUNNING,
        ProposalStatus.EXECUTED,
        ProposalStatus.FAILED,
    }
)

# The audit check ``server_type.id`` is ``baseline.id`` since 0.8.0 (D-11). A
# proposal recorded before names the old id; its verifier and the drift pass's
# duplicate check read it as the new one. This read has no ``removed_in``: a
# proposal is a record, and a record's fields are never deprecated within
# ``cloudfall/v1`` (spec 017, S-017-11).
_RENAMED_CHECKS: Mapping[str, str] = {"server_type.id": "baseline.id"}


def _current_checks(checks: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(_RENAMED_CHECKS.get(check, check) for check in checks)


def _drift_fingerprint(
    server: ResourceId, checks: tuple[str, ...], operation_kind: OperationKind
) -> str:
    return fingerprint_of(
        {
            "server": server.value,
            "operation": operation_kind.value,
            "checks": ",".join(sorted(checks)),
        }
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
class AgentClient:
    """The MCP client that called, as it named itself at initialize."""

    name: str
    version: str

    UNKNOWN = "unknown"
    """What a client that did not name itself, or its version, reads as."""

    def __post_init__(self) -> None:
        """Reject an empty name or version: an unnamed one reads as unknown."""
        if not self.name.strip() or not self.version.strip():
            message = "an agent client names itself and its version, or unknown"
            raise ValueError(message)

    def as_dict(self) -> dict[str, object]:
        """Serialize the client for the proposal receipt."""
        return {"name": self.name, "version": self.version}


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """Who licensed an execution: a person, the operator, or an agent on a grant.

    A person's approval says how it arrived (``via``); a policy's has no
    channel, since no one typed anything. An agent's names the policy, the
    operation class it granted, and the MCP client that called (S-018).
    """

    mode: ApprovalMode
    policy: ResourceId | None
    via: ApprovalChannel | None = None
    grant: OperationKind | None = None
    client: AgentClient | None = None

    def __post_init__(self) -> None:
        """Reject a person's approval without a channel, or a policy's with one."""
        human = self.mode is ApprovalMode.HUMAN
        if (self.via is None) is human:
            message = (
                f"a {self.mode.value} approval "
                f"{'names' if human else 'has no'} the channel it arrived through"
            )
            raise ValueError(message)
        agent = self.mode is ApprovalMode.AGENT
        if agent and (self.policy is None or self.grant is None or self.client is None):
            message = "an agent approval names its policy, grant and client"
            raise ValueError(message)
        if not agent and (self.grant is not None or self.client is not None):
            message = f"a {self.mode.value} approval names no grant or client"
            raise ValueError(message)

    def as_dict(self) -> dict[str, object]:
        """Serialize the approval for the proposal receipt."""
        result: dict[str, object] = {"mode": self.mode.value}
        if self.policy is not None:
            result["policy"] = self.policy.value
        if self.via is not None:
            result["via"] = self.via.value
        if self.grant is not None:
            result["grant"] = self.grant.value
        if self.client is not None:
            result["client"] = self.client.as_dict()
        return result


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """An agent's call of a gated MCP tool: what an agent proposal runs."""

    tool: str
    arguments: Mapping[str, object]
    """The call's arguments, the ones the server binds left out."""
    reason: str | None = None
    """Why the agent asked, when it said."""

    def as_dict(self) -> dict[str, object]:
        """Serialize the request for the proposal receipt."""
        result: dict[str, object] = {
            "tool": self.tool,
            "arguments": dict(self.arguments),
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result


@dataclass(frozen=True, slots=True)
class ProposalBinding:
    """What an agent proposal was computed against (D-20, S-018).

    ``plan_sha256`` is the sha256 of the plan the command computed; in a git
    project ``git_commit`` is HEAD and ``worktree_digest`` the digest of the
    uncommitted changes outside ``decisions/``, ``record/`` and ``tmp/``.
    """

    plan_sha256: str
    git_commit: str | None = None
    worktree_digest: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Serialize the binding for the proposal receipt."""
        result: dict[str, object] = {"planSha256": self.plan_sha256}
        if self.git_commit is not None:
            result["gitCommit"] = self.git_commit
        if self.worktree_digest is not None:
            result["worktreeDigest"] = self.worktree_digest
        return result


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """What actually happened after an approval."""

    executed_at: str
    verified_at: str | None
    result: str
    detail: str
    owned_roles: tuple[OwnedRoleRecord, ...] = ()
    """Owner role passwords the run set: reference, key and hash, never a value."""

    def as_dict(self) -> dict[str, object]:
        """Serialize the outcome for the proposal receipt."""
        result: dict[str, object] = {
            "executedAt": self.executed_at,
            "verifiedAt": self.verified_at,
            "result": self.result,
            "detail": self.detail,
        }
        if self.owned_roles:
            result["ownedRoles"] = [role.as_dict() for role in self.owned_roles]
        return result


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
    operation_server: ResourceId | None
    """The one server an alert or drift proposal runs on; none for an agent's."""
    operation_service: ResourceId | None
    command: tuple[str, ...]
    approval: ApprovalRecord | None
    outcome: ProposalOutcome | None
    interruption: Interruption | None = None
    """Who closed its ``running`` receipt as interrupted, and when."""
    request: AgentRequest | None = None
    """The agent's call, for an agent proposal (S-018)."""
    operation_servers: tuple[ResourceId, ...] = ()
    """Every server an agent proposal's run changes."""
    operation_environments: tuple[ResourceId, ...] = ()
    """The environments of those servers."""
    binding: ProposalBinding | None = None
    """What an agent proposal was computed against."""

    def __post_init__(self) -> None:
        """Reject proposals whose trigger payload contradicts its kind."""
        payloads = {
            TriggerKind.ALERT: self.alert is not None,
            TriggerKind.DRIFT: self.drift is not None,
            TriggerKind.AGENT: self.request is not None,
        }
        if any(
            present is not (kind is self.trigger_kind)
            for kind, present in payloads.items()
        ):
            message = (
                "proposal trigger payload does not match its kind: "
                f"{self.resource_id.value}"
            )
            raise ValueError(message)
        agent = self.trigger_kind is TriggerKind.AGENT
        # A run that changes no declared server names none and no
        # environment; one that names servers names their environments.
        servers_match = bool(self.operation_servers) is bool(
            self.operation_environments
        )
        if agent and (not servers_match or self.binding is None):
            message = (
                "an agent proposal names the servers it changes with their "
                f"environments, and its binding: {self.resource_id.value}"
            )
            raise ValueError(message)
        if not agent and self.operation_server is None:
            message = (
                "an alert or drift proposal names the server it runs on: "
                f"{self.resource_id.value}"
            )
            raise ValueError(message)

    @property
    def servers(self) -> tuple[ResourceId, ...]:
        """Every server the proposal's run changes."""
        if self.operation_server is not None:
            return (self.operation_server, *(
                server
                for server in self.operation_servers
                if server != self.operation_server
            ))
        return self.operation_servers

    def _trigger(self) -> dict[str, object]:
        trigger: dict[str, object] = {
            "kind": self.trigger_kind.value,
            "fingerprint": self.fingerprint,
        }
        if self.alert is not None:
            trigger["alert"] = self.alert.as_dict()
        if self.drift is not None:
            trigger["drift"] = self.drift.as_dict()
        if self.request is not None:
            trigger["request"] = self.request.as_dict()
        return trigger

    def _operation(self) -> dict[str, object]:
        operation: dict[str, object] = {"kind": self.operation_kind.value}
        if self.operation_server is not None:
            operation["server"] = self.operation_server.value
        if self.operation_servers or self.trigger_kind is TriggerKind.AGENT:
            operation["servers"] = [server.value for server in self.operation_servers]
        if self.operation_environments or self.trigger_kind is TriggerKind.AGENT:
            operation["environments"] = [
                environment.value for environment in self.operation_environments
            ]
        operation["command"] = list(self.command)
        if self.operation_service is not None:
            operation["service"] = self.operation_service.value
        return operation

    def as_document(self) -> dict[str, object]:
        """Serialize the proposal as a schema-valid receipt document."""
        spec: dict[str, object] = {
            "createdAt": self.created_at,
            "status": self.status.value,
            "trigger": self._trigger(),
            "diagnosis": {
                "summary": self.diagnosis_summary,
                "evidence": list(self.evidence),
            },
            "operation": self._operation(),
        }
        if self.binding is not None:
            spec["binding"] = self.binding.as_dict()
        if self.approval is not None:
            spec["approval"] = self.approval.as_dict()
        if self.outcome is not None:
            spec["outcome"] = self.outcome.as_dict()
        if self.interruption is not None:
            spec["interruption"] = self.interruption.as_dict()
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
        (
            f"labels: severity={alert.severity.value} "
            f"environment={alert.environment.value} "
            f"server={alert.server.value} service={alert.service.value}"
        ),
        f"fingerprint {alert.fingerprint}",
    )
    return OperatorProposal(
        resource_id=proposal_identifier(alert.fingerprint, now),
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
    fingerprint = _drift_fingerprint(server, checks, operation_kind)
    playbook = _OPERATION_PLAYBOOKS[operation_kind]
    summary = (
        f"Audit reports drift on server {server.value}: "
        f"{', '.join(checks)}; converging through {playbook} restores the "
        "declared state"
    )
    evidence = tuple(f"audit check drifted: {check}" for check in checks)
    drift = DriftTrigger(server=server, checks=checks, fingerprint=fingerprint)
    return OperatorProposal(
        resource_id=proposal_identifier(fingerprint, now),
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


def proposal_identifier(fingerprint: str, now: str) -> ResourceId:
    """Return a proposal's id: its creation moment and its trigger's fingerprint."""
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
        content = self._content(proposal)
        with record_files.locked(self.directory):
            try:
                record_files.create(path, content)
            except FileExistsError as error:
                message = f"proposal already exists: {path}"
                raise OperatorError(_ERROR_PROPOSAL_EXISTS, message) from error
        return path

    def open_or_save(self, proposal: OperatorProposal) -> tuple[OperatorProposal, Path]:
        """Return the open agent proposal of the same call, or save this one.

        Under the store's lock, a ``proposed`` agent record with the same
        fingerprint (tool, arguments, servers) and the same binding is the
        proposal of this call, and nothing is written; otherwise ``proposal``
        is saved. A record the store cannot read matches nothing.
        """
        with record_files.locked(self.directory):
            for existing in self.list().records:
                if (
                    existing.trigger_kind is TriggerKind.AGENT
                    and existing.status is ProposalStatus.PROPOSED
                    and existing.fingerprint == proposal.fingerprint
                    and existing.binding == proposal.binding
                ):
                    return existing, self._path(existing.resource_id)
            path = self._path(proposal.resource_id)
            try:
                record_files.create(path, self._content(proposal))
            except FileExistsError as error:
                message = f"proposal already exists: {path}"
                raise OperatorError(_ERROR_PROPOSAL_EXISTS, message) from error
        return proposal, path

    def update(self, proposal: OperatorProposal) -> Path:
        """Persist a state change of an existing proposal."""
        with record_files.locked(self.directory):
            return self._replace(proposal)

    def start(self, running: OperatorProposal) -> Path:
        """Record an approved execution as started, if its receipt is still open.

        The receipt on disk is read again under the store's lock: a second
        approval of the same proposal, a person's or a policy's, finds it
        ``running`` and is refused before anything runs.
        """
        with record_files.locked(self.directory):
            refuse_not_open(self.load(running.resource_id))
            return self._replace(running)

    def interrupt(
        self, proposal_id: ResourceId, interruption: Interruption
    ) -> OperatorProposal:
        """Close a ``running`` or ``executed`` receipt as interrupted.

        An ``executed`` receipt is one whose verification never recorded an
        outcome. The receipt on disk is read again under the store's lock: an
        execution that recorded its outcome since keeps that outcome.
        """
        with record_files.locked(self.directory):
            current = self.load(proposal_id)
            if current.status not in _INTERRUPTIBLE_STATUSES:
                message = (
                    f"proposal {proposal_id.value} is {current.status.value}, not "
                    "running or executed, so there is no started execution to "
                    "close as interrupted"
                )
                raise OperatorError(ERROR_PROPOSAL_NOT_RUNNING, message)
            closed = replace(
                current, status=ProposalStatus.INTERRUPTED, interruption=interruption
            )
            self._replace(closed)
            return closed

    def load(self, proposal_id: ResourceId) -> OperatorProposal:
        """Load and re-validate one proposal receipt."""
        path = self._path(proposal_id)
        if not path.is_file():
            message = f"proposal does not exist: {path}"
            raise OperatorError(ERROR_PROPOSAL_MISSING, message)
        return self.read(path)

    def read(self, path: Path) -> OperatorProposal:
        """Read and validate one receipt file, by the schema its apiVersion names."""
        document = cast("object", json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(document, dict):
            message = f"proposal receipt is not a JSON object: {path}"
            raise OperatorError(ERROR_RECORD_INVALID, message)
        record = cast("dict[str, object]", document)
        try:
            schema = record_files.schema_for(record, PROPOSAL_SCHEMAS)
        except LookupError as error:
            message = f"proposal receipt cannot be read: {path}: {error}"
            raise OperatorError(ERROR_RECORD_INVALID, message) from error
        self.catalog.validate_named(schema, record)
        return _proposal_from_document(record)

    def list(self) -> record_files.Listing[OperatorProposal]:
        """Read every proposal receipt, ordered by creation time.

        A receipt that cannot be read stops nothing: it is reported, with
        its path and why, beside the receipts that could be.
        """
        proposals: list[OperatorProposal] = []
        unreadable: list[record_files.UnreadableRecord] = []
        for path in record_files.record_paths(self.directory):
            try:
                proposals.append(self.read(path))
            except OperatorError as error:
                if error.code != ERROR_RECORD_INVALID:
                    raise
                unreadable.append(record_files.UnreadableRecord(path, error.message))
            except ValidationError as error:
                field = ".".join(str(part) for part in error.absolute_path) or "(root)"
                reason = (
                    f"proposal receipt does not match its schema: {path}: "
                    f"{field}: {error.message}"
                )
                unreadable.append(record_files.UnreadableRecord(path, reason))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                reason = f"proposal receipt is not valid JSON: {path}: {error}"
                unreadable.append(record_files.UnreadableRecord(path, reason))
            except OSError as error:
                reason = f"proposal receipt cannot be read: {path}: {error}"
                unreadable.append(record_files.UnreadableRecord(path, reason))
        proposals.sort(key=lambda proposal: proposal.created_at)
        return record_files.Listing(
            records=tuple(proposals), unreadable=tuple(unreadable)
        )

    def blocking_fingerprints(self) -> frozenset[str]:
        """Fingerprints already covered by an open or failed proposal.

        A drift proposal naming a renamed check also covers the fingerprint
        the same drift has under the check's current id.
        """
        fingerprints: set[str] = set()
        for proposal in self.list().records:
            if proposal.status not in _BLOCKING_STATUSES:
                continue
            fingerprints.add(proposal.fingerprint)
            drift = proposal.drift
            if drift is not None and _current_checks(drift.checks) != drift.checks:
                fingerprints.add(
                    _drift_fingerprint(
                        drift.server,
                        _current_checks(drift.checks),
                        proposal.operation_kind,
                    )
                )
        return frozenset(fingerprints)

    def _path(self, proposal_id: ResourceId) -> Path:
        return self.directory / f"{proposal_id.value}.json"

    def _replace(self, proposal: OperatorProposal) -> Path:
        """Replace one existing receipt; the caller holds the store's lock."""
        path = self._path(proposal.resource_id)
        content = self._content(proposal)
        if not path.exists():
            message = f"proposal does not exist: {path}"
            raise OperatorError(ERROR_PROPOSAL_MISSING, message)
        record_files.replace(path, content)
        return path

    def _content(self, proposal: OperatorProposal) -> str:
        document = proposal.as_document()
        spec = cast("dict[str, object]", document["spec"])
        spec[record_files.WRITER_VERSION_FIELD] = record_files.writer_version()
        self.catalog.validate_named(PROPOSAL_SCHEMAS[_API_VERSION], document)
        return json.dumps(document, indent=2) + "\n"


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
    request = None
    raw_request = trigger.get("request")
    if isinstance(raw_request, dict):
        raw_reason = raw_request.get("reason")
        request = AgentRequest(
            tool=cast("str", raw_request["tool"]),
            arguments=MappingProxyType(
                dict(cast("Mapping[str, object]", raw_request["arguments"]))
            ),
            reason=None if raw_reason is None else cast("str", raw_reason),
        )
    raw_approval = spec.get("approval")
    approval = None
    if isinstance(raw_approval, dict):
        raw_policy = raw_approval.get("policy")
        mode = ApprovalMode(cast("str", raw_approval["mode"]))
        raw_grant = raw_approval.get("grant")
        raw_client = raw_approval.get("client")
        approval = ApprovalRecord(
            mode=mode,
            policy=(
                ResourceId.from_boundary(raw_policy)
                if raw_policy is not None
                else None
            ),
            # A person's approval written before the channel reads as unknown.
            via=(
                ApprovalChannel.from_record(raw_approval.get("via"))
                if mode is ApprovalMode.HUMAN
                else None
            ),
            grant=None if raw_grant is None else OperationKind(cast("str", raw_grant)),
            client=(
                None
                if not isinstance(raw_client, dict)
                else AgentClient(
                    name=cast("str", raw_client["name"]),
                    version=cast("str", raw_client["version"]),
                )
            ),
        )
    raw_binding = spec.get("binding")
    binding = None
    if isinstance(raw_binding, dict):
        binding = ProposalBinding(
            plan_sha256=cast("str", raw_binding["planSha256"]),
            git_commit=cast("str | None", raw_binding.get("gitCommit")),
            worktree_digest=cast("str | None", raw_binding.get("worktreeDigest")),
        )
    raw_outcome = spec.get("outcome")
    outcome = None
    if isinstance(raw_outcome, dict):
        outcome = ProposalOutcome(
            executed_at=cast("str", raw_outcome["executedAt"]),
            verified_at=cast("str | None", raw_outcome["verifiedAt"]),
            result=cast("str", raw_outcome["result"]),
            detail=cast("str", raw_outcome["detail"]),
            owned_roles=tuple(
                OwnedRoleRecord.from_dict(cast("Mapping[str, object]", role))
                for role in cast("list[object]", raw_outcome.get("ownedRoles", []))
            ),
        )
    raw_service = operation.get("service")
    raw_server = operation.get("server")
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
        operation_server=(
            ResourceId.from_boundary(raw_server) if raw_server is not None else None
        ),
        operation_service=(
            ResourceId.from_boundary(raw_service)
            if raw_service is not None
            else None
        ),
        command=tuple(cast("list[str]", operation["command"])),
        approval=approval,
        outcome=outcome,
        interruption=Interruption.from_record(spec.get("interruption")),
        request=request,
        operation_servers=tuple(
            ResourceId.from_boundary(server)
            for server in cast("list[object]", operation.get("servers", []))
        ),
        operation_environments=tuple(
            ResourceId.from_boundary(environment)
            for environment in cast("list[object]", operation.get("environments", []))
        ),
        binding=binding,
    )


@dataclass(frozen=True, slots=True)
class RunReport:
    """Structured result of one operator pass."""

    proposed: tuple[str, ...]
    skipped: tuple[SkippedAlert, ...]
    open_proposals: int
    unreadable: tuple[record_files.UnreadableRecord, ...] = ()
    """Receipts the pass could not read, so it neither counted nor matched them."""

    def as_dict(self) -> dict[str, object]:
        """Serialize for structured CLI output."""
        result: dict[str, object] = {
            "proposed": list(self.proposed),
            "skipped": [skipped.as_dict() for skipped in self.skipped],
            "openProposals": self.open_proposals,
        }
        return _with_unreadable(result, self.unreadable)


def _with_unreadable(
    result: dict[str, object],
    unreadable: tuple[record_files.UnreadableRecord, ...],
) -> dict[str, object]:
    """Name the receipts a pass skipped, when there are any."""
    if unreadable:
        result["unreadable"] = [record.as_dict() for record in unreadable]
    return result


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
    return _run_report(store, tuple(proposed), tuple(skipped))


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
    return _run_report(store, tuple(proposed), ())


def _run_report(
    store: ProposalStore,
    proposed: tuple[str, ...],
    skipped: tuple[SkippedAlert, ...],
) -> RunReport:
    """Count the open proposals and name the receipts that could not be read."""
    listing = store.list()
    return RunReport(
        proposed=proposed,
        skipped=skipped,
        open_proposals=sum(
            1
            for proposal in listing.records
            if proposal.status is ProposalStatus.PROPOSED
        ),
        unreadable=listing.unreadable,
    )


def engine_executor(
    context: EngineContext,
    secrets: SopsSecretProvider,
) -> Executor:
    """Executor running the proposal's engine entry point on its server alone.

    The proposal names one server, and the policy that approved it was
    chosen by that server's environment, so every playbook of the run is
    limited to it; an operation kind with no playbook that can be limited
    is refused before anything runs. ``converge-services`` reads every owner
    role's password from ``secrets`` first, so a missing one fails before
    any playbook runs.
    """

    def _execute(proposal: OperatorProposal) -> tuple[OwnedRoleRecord, ...]:
        playbook = _OPERATION_PLAYBOOKS.get(proposal.operation_kind)
        if playbook is None:
            message = (
                "unsupported operation kind: "
                f"{proposal.operation_kind.value}"
            )
            raise OperatorError(_ERROR_OPERATION_UNSUPPORTED, message)
        server = proposal.operation_server
        if server is None:
            # An agent proposal names no one server; this executor never runs
            # one on every server.
            message = (
                f"proposal {proposal.resource_id.value} names no one server to "
                "limit its run to"
            )
            raise OperatorError(_ERROR_OPERATION_UNSUPPORTED, message)
        if proposal.operation_kind is OperationKind.CONVERGE_SERVICES:
            passwords = converge_services(context, secrets, server)
            return tuple(password.record() for password in passwords)
        run_engine_playbook(context, playbook, {}, server)
        return ()

    return _execute


def engine_auditor(
    context: EngineContext,
    inventory: PlatformInventory,
    observation_directory: Path,
    limit: ResourceId | None = None,
) -> Callable[[], AuditReport]:
    """Auditor refreshing observations through the inspect playbook.

    ``limit`` refreshes one declared server's observation alone, as a
    proposal's verify step does; the others keep their last one.
    """

    def _audit() -> AuditReport:
        run_engine_playbook(
            context,
            "inspect.yml",
            {
                "cloudfall_inspect_output_directory": str(
                    observation_directory.resolve()
                )
            },
            limit,
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
        return not (set(_current_checks(proposal.drift.checks)) & still_drifting)

    return _verify


@dataclass(frozen=True, slots=True)
class ApproveOptions:
    """Verification behavior for an approval."""

    verify_timeout_seconds: float = 180.0
    poll_interval_seconds: float = 10.0
    sleep: Callable[[float], None] = field(default=time.sleep)


def approve(  # noqa: PLR0913 - the run's contract plus how the approval came.
    store: ProposalStore,
    proposal_id: ResourceId,
    executor: Executor,
    verifier: Callable[[OperatorProposal], bool],
    options: ApproveOptions | None = None,
    *,
    via: ApprovalChannel,
) -> OperatorProposal:
    """Execute a human-approved proposal and verify its trigger resolves.

    ``via`` is how the person's approval arrived; a new approval cannot be
    of an unknown channel.
    """
    if via is ApprovalChannel.UNKNOWN:
        message = (
            "an approval records how it arrived; a new one cannot be of an "
            "unknown channel"
        )
        raise OperatorError(_ERROR_CHANNEL_UNKNOWN, message)
    proposal = store.load(proposal_id)
    refuse_not_open(proposal)
    refuse_agent_proposal(proposal)
    return _execute_and_verify(
        store,
        proposal,
        executor,
        verifier,
        ApprovalRecord(mode=ApprovalMode.HUMAN, policy=None, via=via),
        options=options,
    )


def refuse_agent_proposal(proposal: OperatorProposal) -> None:
    """Refuse to run an agent proposal, before anything runs.

    Its request is a gated MCP tool's call, which this release does not run
    from the proposal store; the receipt stays as it is.
    """
    if proposal.trigger_kind is not TriggerKind.AGENT:
        return
    message = (
        f"proposal {proposal.resource_id.value} is an agent's call of "
        f"{proposal.request.tool if proposal.request is not None else 'a tool'}, "
        "which this release does not run from the proposal store"
    )
    raise OperatorError(ERROR_AGENT_PROPOSAL_UNSUPPORTED, message)


def refuse_not_open(proposal: OperatorProposal) -> None:
    """Refuse a proposal that is not open, before anything runs."""
    if proposal.status is ProposalStatus.PROPOSED:
        return
    if proposal.status is ProposalStatus.RUNNING:
        message = (
            f"proposal {proposal.resource_id.value} was approved and its "
            "execution started, but no outcome is recorded: it is still running "
            "or it was interrupted. It is not approved again, and it blocks a "
            "new proposal for the same trigger; check the server, and once its "
            "execution is dead, close it with `cloudfall operator "
            f"mark-interrupted {proposal.resource_id.value} --confirm-dead`, "
            "so the operator can propose it again"
        )
        raise OperatorError(ERROR_PROPOSAL_INTERRUPTED, message)
    message = (
        f"proposal {proposal.resource_id.value} is {proposal.status.value}, "
        "only proposed proposals can be approved"
    )
    raise OperatorError(_ERROR_PROPOSAL_NOT_OPEN, message)


def mark_interrupted(
    store: ProposalStore,
    proposal_id: ResourceId,
    confirmed_by: str,
    now: Callable[[], str] | None = None,
) -> OperatorProposal:
    """Close a ``running`` proposal as interrupted, once a person confirmed it.

    Only the person can tell the execution's process is gone. Nothing on
    the server changes; the receipt says who confirmed it and when, and it
    no longer blocks a new proposal for the same trigger.
    """
    interruption = Interruption(
        confirmed_by=confirmed_by.strip(),
        confirmed_at=(now or _utc_now)(),
    )
    return store.interrupt(proposal_id, interruption)


def _execution_failure_detail(
    error: OperatorError | LifecycleError, stage: str = "execution"
) -> str:
    """Name the failure's code and keep the end of its message within the receipt cap.

    An engine failure carries the tail of the Ansible output, and the last
    lines, the failing task and the play recap, are the ones that say why.
    """
    head = f"{stage} failed: {error.code}: "
    message = str(error).removeprefix(f"{error.code}: ")
    if len(head) + len(message) <= _OUTCOME_DETAIL_MAX:
        return head + message
    marker = "..."
    return head + marker + message[-(_OUTCOME_DETAIL_MAX - len(head) - len(marker)) :]


def _execute_and_verify(  # noqa: PLR0913 - internal execution contract.
    store: ProposalStore,
    proposal: OperatorProposal,
    executor: Executor,
    verifier: Callable[[OperatorProposal], bool],
    approval: ApprovalRecord,
    *,
    options: ApproveOptions | None = None,
) -> OperatorProposal:
    resolved_options = options if options is not None else ApproveOptions()
    executed_at = _utc_now()
    # The approval and the started execution are on record before the
    # executor runs: one killed midway stays running, never approvable.
    store.start(replace(proposal, status=ProposalStatus.RUNNING, approval=approval))
    try:
        owned_roles = executor(proposal) or ()
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
            owned_roles=owned_roles,
        ),
    )
    store.update(executed)
    waited = 0.0
    while True:
        try:
            resolved = verifier(proposal)
        except (OperatorError, LifecycleError) as error:
            # An error from the verifier is an unverified run: the receipt
            # settles failed, which suspends autonomy for the kind (#358).
            failed = replace(
                executed,
                status=ProposalStatus.FAILED,
                outcome=ProposalOutcome(
                    executed_at=executed_at,
                    verified_at=None,
                    result="failed",
                    detail=_execution_failure_detail(error, "verification"),
                    owned_roles=owned_roles,
                ),
            )
            store.update(failed)
            raise
        if resolved:
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
                    owned_roles=owned_roles,
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
                    owned_roles=owned_roles,
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


_SUSPENDING_STATUSES = frozenset({ProposalStatus.FAILED, ProposalStatus.INTERRUPTED})
"""Receipts that suspend autonomy for their kind when they are its latest.

An interrupted run is at least as uncertain as a failed one: what it changed
on the server is unknown.
"""

_SETTLED_STATUSES = frozenset({ProposalStatus.VERIFIED}) | _SUSPENDING_STATUSES
"""Receipts whose run is over, the autonomy history of their kind."""

_INTERRUPTIBLE_STATUSES = frozenset({ProposalStatus.RUNNING, ProposalStatus.EXECUTED})
"""Receipts whose run started and recorded no final outcome a person may close.

An ``executed`` one is left by a verification that never settled, such as one
an older release did not guard.
"""


def _rate_limit_moment(receipt: OperatorProposal) -> str | None:
    """When a receipt's run counts in the hourly rate limit, if it does.

    A run with an outcome counts from its ``executedAt``. An interrupted run
    has none, so it counts from when a person confirmed it dead, the latest
    moment it is known to have run. A run still in progress does not count.
    """
    if receipt.outcome is not None:
        return receipt.outcome.executed_at
    if receipt.interruption is not None:
        return receipt.interruption.confirmed_at
    return None


def autonomy_decision(
    proposal: OperatorProposal,
    policy: OperatorPolicyInventory,
    store: ProposalStore,
    now: datetime,
    environments_by_server: Mapping[ResourceId, ResourceId],
) -> AutonomyDecision:
    """Decide whether the declared policy licenses this execution now.

    Autonomy is earned per environment (D-19): the history the grant is
    judged on, and the suspension rule, count only the settled receipts of
    servers in the policy's environment, which is the proposal's server's.
    ``environments_by_server`` maps each declared server to its environment;
    a receipt of a server no longer declared counts for no environment.
    """
    return grant_decision(
        proposal.operation_kind,
        policy,
        store.list().records,
        now,
        environments_by_server,
        exclude=proposal.resource_id,
    )


def grant_decision(  # noqa: PLR0913 - the grant's whole contract.
    operation_kind: OperationKind,
    policy: OperatorPolicyInventory,
    receipts: tuple[OperatorProposal, ...],
    now: datetime,
    environments_by_server: Mapping[ResourceId, ResourceId],
    *,
    exclude: ResourceId | None = None,
) -> AutonomyDecision:
    """Decide whether ``policy`` grants a run of ``operation_kind`` now.

    The rules of ``autonomy_decision``, over ``receipts`` read once: a
    receipt is in the policy's environment when one of the servers it
    changes is (D-19), and ``exclude`` leaves out the proposal being decided.
    """
    grant = policy.grant_for(operation_kind.value)
    if grant is None:
        reason = (
            f"policy {policy.resource_id.value} does not grant autonomy "
            f"for {operation_kind.value}"
        )
        return AutonomyDecision(granted=False, reason=reason)
    history = [
        receipt
        for receipt in receipts
        if receipt.operation_kind is operation_kind
        and receipt.resource_id != exclude
        and receipt.status in _SETTLED_STATUSES
        and any(
            environments_by_server.get(server) == policy.environment
            for server in receipt.servers
        )
    ]
    verified = sum(
        1
        for receipt in history
        if receipt.status is ProposalStatus.VERIFIED
    )
    if verified < grant.required_verified_runs.value:
        reason = (
            f"insufficient verified history for "
            f"{operation_kind.value} in {policy.environment.value}: "
            f"{verified} of {grant.required_verified_runs.value} required"
        )
        return AutonomyDecision(granted=False, reason=reason)
    if history and history[-1].status in _SUSPENDING_STATUSES:
        last = history[-1].status
        outcome = "failed" if last is ProposalStatus.FAILED else "was interrupted"
        reason = (
            f"most recent {operation_kind.value} receipt {outcome} "
            f"in {policy.environment.value}; "
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
        for receipt in receipts
        if receipt.approval is not None
        and receipt.approval.mode in _RATE_LIMITED_MODES
        and (ran_at := _rate_limit_moment(receipt)) is not None
        and datetime.fromisoformat(ran_at).timestamp() > hour_ago
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
        f"{operation_kind.value}: {verified} verified runs, "
        "rate limit and quiet hours clear"
    )
    return AutonomyDecision(granted=True, reason=reason)


@dataclass(frozen=True, slots=True)
class AutonomyReport:
    """Structured result of one autonomous execution pass."""

    executed: tuple[tuple[str, str], ...]
    withheld: tuple[tuple[str, str], ...]
    owned_roles: Mapping[str, tuple[OwnedRoleRecord, ...]] = field(
        default_factory=dict
    )
    """Owner role passwords each executed proposal set, by proposal id."""

    unreadable: tuple[record_files.UnreadableRecord, ...] = ()
    """Receipts the pass could not read; while there are any, nothing is granted."""

    def as_dict(self) -> dict[str, object]:
        """Serialize for structured run output."""
        result: dict[str, object] = {
            "executed": [
                _executed_entry(proposal_id, status, self.owned_roles.get(proposal_id))
                for proposal_id, status in self.executed
            ],
            "withheld": [
                {"proposal": proposal_id, "reason": reason}
                for proposal_id, reason in self.withheld
            ],
        }
        return _with_unreadable(result, self.unreadable)


def autonomous_pass(  # noqa: PLR0913 - boundary mirrors approve().
    store: ProposalStore,
    inventory: PlatformInventory,
    executor: Executor,
    verifier_for: Callable[
        [OperatorProposal], Callable[[OperatorProposal], bool]
    ],
    *,
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
    owned_roles: dict[str, tuple[OwnedRoleRecord, ...]] = {}
    listing = store.list()
    for proposal in listing.records:
        if proposal.status is not ProposalStatus.PROPOSED:
            continue
        if (
            proposal.trigger_kind is TriggerKind.AGENT
            or proposal.operation_server is None
        ):
            # An agent proposal waits for a person, or for the agent to call
            # again once a grant covers its run (S-018), even one that also
            # names a single server.
            withheld.append(
                (
                    proposal.resource_id.value,
                    "an agent proposal is approved only by a person",
                )
            )
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
        decision = (
            incomplete_history(listing.unreadable)
            if listing.unreadable
            else autonomy_decision(
                proposal, policy, store, moment, environments_by_server
            )
        )
        if not decision.granted:
            withheld.append((proposal.resource_id.value, decision.reason))
            continue
        try:
            result = _execute_and_verify(
                store,
                proposal,
                executor,
                verifier_for(proposal),
                ApprovalRecord(
                    mode=ApprovalMode.AUTONOMOUS, policy=policy.resource_id
                ),
                options=options,
            )
        except OperatorError as error:
            # A person approved it since this pass listed the store.
            if error.code not in _NOT_OPEN_CODES:
                raise
            withheld.append((proposal.resource_id.value, error.message))
            continue
        executed.append((result.resource_id.value, result.status.value))
        if result.outcome is not None and result.outcome.owned_roles:
            owned_roles[result.resource_id.value] = result.outcome.owned_roles
    return AutonomyReport(
        executed=tuple(executed),
        withheld=tuple(withheld),
        owned_roles=owned_roles,
        unreadable=listing.unreadable,
    )


def incomplete_history(
    unreadable: tuple[record_files.UnreadableRecord, ...],
) -> AutonomyDecision:
    """Withhold a grant while a receipt cannot be read.

    The unreadable receipt could be the failure that suspends autonomy, so
    no grant is made on a history with a gap in it.
    """
    paths = ", ".join(str(record.path) for record in unreadable)
    reason = (
        f"{len(unreadable)} proposal receipt(s) cannot be read, so the "
        f"verified history is incomplete: {paths}"
    )
    return AutonomyDecision(granted=False, reason=reason)


def _executed_entry(
    proposal_id: str, status: str, owned_roles: tuple[OwnedRoleRecord, ...] | None
) -> dict[str, object]:
    entry: dict[str, object] = {"proposal": proposal_id, "status": status}
    if owned_roles:
        entry["ownedRoles"] = [role.as_dict() for role in owned_roles]
    return entry


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
