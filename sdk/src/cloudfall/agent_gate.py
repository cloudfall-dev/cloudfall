"""Which MCP tools change a project's servers, and whether a policy grants their run.

Over ``cloudfall mcp serve`` in project mode an agent confirms its own server
changes; spec 018 gates each such run on an ``OperatorPolicy`` grant per
environment. This module holds what that gate reads: the class each gated
tool runs (the ``kind`` a grant names), the servers a run changes, and the
grant check of every environment those servers are in, which applies the
operator's own grant rules. A run is self-confirmed only when every
environment it touches grants it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from cloudfall.audit import AuditStatus
from cloudfall.operator import (
    AgentClient,
    AgentRequest,
    ApprovalMode,
    ApprovalRecord,
    OperationKind,
    OperatorProposal,
    ProposalOutcome,
    ProposalStatus,
    TriggerKind,
    fingerprint_of,
    grant_decision,
    incomplete_history,
    proposal_identifier,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime
    from pathlib import Path

    from cloudfall.audit import AuditReport
    from cloudfall.decision import ApprovalChannel
    from cloudfall.domain import ResourceId
    from cloudfall.inventory import PlatformInventory
    from cloudfall.lifecycle import DomainObserved, ExecutionStep, StepRun, StepRunner
    from cloudfall.operator import ProposalBinding, ProposalStore

GATED_TOOLS: Mapping[str, OperationKind] = MappingProxyType(
    {
        "deploy": OperationKind.DEPLOY,
        "rollback": OperationKind.ROLLBACK,
        "restart": OperationKind.RESTART,
        "migrate": OperationKind.MIGRATE,
        "data_copy": OperationKind.DATA_COPY,
        "domains_apply": OperationKind.DOMAINS_APPLY,
        "converge_baseline": OperationKind.CONVERGE_BASELINE,
        "converge_services": OperationKind.CONVERGE_SERVICES,
        "converge_domains": OperationKind.CONVERGE_DOMAINS,
    }
)
"""The project's MCP tools that change servers, and the class each runs (S-018).

The first six run with ``yes: true``, the ``converge_*`` tools with
``confirm_destructive: true``.
"""


def tool_class(tool: str) -> OperationKind:
    """Return the operation class a gated MCP tool runs."""
    try:
        return GATED_TOOLS[tool]
    except KeyError as error:
        message = f"tool {tool} is not gated: it changes no server"
        raise ValueError(message) from error


_ALL_SERVER_STEPS = frozenset({"baseline", "services"})
"""``migrate`` steps that converge every declared server."""
_DOMAIN_STEPS = frozenset({"domains-http", "domains-tls"})
"""``migrate`` steps that converge every Domain on its proxy server."""
_CONTROLLER_STEPS = frozenset(
    {
        "ttl-lower",
        "parallel-run",
        "dns-verify",
        "inspect",
        "audit",
        "verify-routes",
        "rollback-window",
    }
)
"""``migrate`` steps that change no server: they read, measure, or record."""


def declared_servers(inventory: PlatformInventory) -> tuple[ResourceId, ...]:
    """Every declared server: what a ``converge_*`` run over the fleet changes."""
    return tuple(server.resource_id for server in inventory.servers)


def domain_servers(
    inventory: PlatformInventory, domain_id: ResourceId | None
) -> tuple[ResourceId, ...]:
    """Return the proxy server of one Domain, or every server when none is named.

    ``converge_domains`` with ``domain`` runs on that Domain's proxy server
    alone; without it, on every declared server.
    """
    if domain_id is None:
        return declared_servers(inventory)
    for domain in inventory.domains:
        if domain.resource_id == domain_id:
            return (domain.proxy.server_id,)
    message = f"domain {domain_id.value} is not declared"
    raise ValueError(message)


def migrate_servers(
    inventory: PlatformInventory, pending_steps: Iterable[str]
) -> tuple[ResourceId, ...]:
    """Return the servers the pending steps of a ``migrate`` plan change.

    ``baseline`` and ``services`` change every declared server, a
    ``data:<database>`` step the server of the service declaring it, a
    ``deploy:<component>`` step the component's servers, and the Domain steps
    every Domain's proxy server; builds and checks change none.
    """
    servers: list[ResourceId] = []
    for step in pending_steps:
        kind, _, subject = step.partition(":")
        if step in _ALL_SERVER_STEPS:
            servers.extend(declared_servers(inventory))
        elif step in _DOMAIN_STEPS:
            servers.extend(domain.proxy.server_id for domain in inventory.domains)
        elif kind == "data" and subject:
            servers.extend(
                service.server_id
                for service in inventory.services
                if service.postgresql is not None
                and any(
                    database.name.value == subject
                    for database in service.postgresql.databases
                )
            )
        elif kind == "deploy" and subject:
            servers.extend(
                server
                for component in inventory.components
                if component.resource_id.value == subject
                for server in component.server_ids
            )
        elif not (kind == "build" and subject) and step not in _CONTROLLER_STEPS:
            message = f"migrate step {step} is not one this release plans"
            raise ValueError(message)
    return _unique(servers)


NO_DECLARED_SERVER = (
    "the run changes no declared server, so no environment's grant covers it "
    "and only a person approves it"
)
"""The reason a run that touches no environment is never self-confirmed."""


@dataclass(frozen=True, slots=True)
class EnvironmentGrant:
    """Whether one environment a run touches grants it, and why."""

    environment: ResourceId
    policy: ResourceId | None
    """The ``OperatorPolicy`` of the environment; none when it declares none."""
    granted: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        """Serialize for a plan's ``approval``."""
        return {
            "environment": self.environment.value,
            "policy": None if self.policy is None else self.policy.value,
            "granted": self.granted,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class GrantCheck:
    """What a confirmed call of a gated tool would do: run, or only propose."""

    operation_kind: OperationKind
    servers: tuple[ResourceId, ...]
    environments: tuple[EnvironmentGrant, ...]

    @property
    def self_confirm(self) -> bool:
        """The run is granted: it touches servers, and every environment grants it.

        A run that changes no declared server touches no environment that
        could grant it, so it is not self-confirmed.
        """
        return bool(self.environments) and all(
            environment.granted for environment in self.environments
        )

    def reasons(self) -> tuple[str, ...]:
        """Return one line per environment: its name and the grant's reason.

        A run that changes no declared server has no environment whose
        grant could cover it: one line says so.
        """
        if not self.environments:
            return (NO_DECLARED_SERVER,)
        return tuple(
            f"{environment.environment.value}: {environment.reason}"
            for environment in self.environments
        )

    def as_dict(self) -> dict[str, object]:
        """Serialize the fields a plan's ``approval`` gains (S-018-10)."""
        return {
            "selfConfirm": self.self_confirm,
            "environments": [
                environment.as_dict() for environment in self.environments
            ],
        }


def check_grant(
    operation_kind: OperationKind,
    servers: Iterable[ResourceId],
    inventory: PlatformInventory,
    store: ProposalStore,
    now: datetime,
) -> GrantCheck:
    """Check each environment of ``servers`` for a grant of ``operation_kind``.

    A server's environment is its ``Server.spec.environment``, and the policy
    is the one whose ``spec.environment`` names it. Each grant is judged by
    the operator's rules on the proposal store's settled records of that
    environment (D-19); a record the store cannot read withholds every
    grant, since it could be the failure that suspends one.
    """
    changed = _unique(servers)
    environments_by_server = {
        server.resource_id: server.environment for server in inventory.servers
    }
    undeclared = [server for server in changed if server not in environments_by_server]
    if undeclared:
        message = (
            "servers are not declared: "
            f"{', '.join(server.value for server in undeclared)}"
        )
        raise ValueError(message)
    policies = {policy.environment: policy for policy in inventory.operator_policies}
    listing = store.list()
    grants: list[EnvironmentGrant] = []
    for environment in _unique(environments_by_server[server] for server in changed):
        policy = policies.get(environment)
        if policy is None:
            grants.append(
                EnvironmentGrant(
                    environment=environment,
                    policy=None,
                    granted=False,
                    reason=(
                        "no operator policy declared for environment "
                        f"{environment.value}"
                    ),
                )
            )
            continue
        decision = (
            incomplete_history(listing.unreadable)
            if listing.unreadable
            else grant_decision(
                operation_kind,
                policy,
                listing.records,
                now,
                environments_by_server,
            )
        )
        grants.append(
            EnvironmentGrant(
                environment=environment,
                policy=policy.resource_id,
                granted=decision.granted,
                reason=decision.reason,
            )
        )
    return GrantCheck(
        operation_kind=operation_kind,
        servers=changed,
        environments=tuple(grants),
    )


def _unique(values: Iterable[ResourceId]) -> tuple[ResourceId, ...]:
    """Keep each value once, in the order first seen."""
    return tuple(dict.fromkeys(values))


# The granted run (S-018): a confirmed call a grant covers runs as the
# command always ran, and one record in the proposal store says so. It is on
# disk, running, before the run's first engine step, and settles verified
# only when the tool's own check passed.

TOOL_COMMANDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "deploy": ("cloudfall", "deploy"),
        "rollback": ("cloudfall", "rollback"),
        "restart": ("cloudfall", "restart"),
        "migrate": ("cloudfall", "migrate"),
        "data_copy": ("cloudfall", "data", "copy"),
        "domains_apply": ("cloudfall", "domains", "apply"),
        "converge_baseline": ("cloudfall-engine", "playbook", "baseline.yml"),
        "converge_services": ("cloudfall-engine", "playbook", "services.yml"),
        "converge_domains": ("cloudfall-engine", "playbook", "domains.yml"),
    }
)
"""The command each gated tool runs, as an agent record's ``operation.command``."""

REASON_MAX_LENGTH = 1000
"""The most characters an agent's ``reason`` holds."""

_SUMMARY_MAX = 1000
_EVIDENCE_MAX = 500
_DETAIL_MAX = 1000
_ELLIPSIS = "..."


def check_reason(reason: str | None) -> str | None:
    """Return the agent's reason, refusing a blank or an overlong one."""
    if reason is None:
        return None
    if not reason.strip():
        message = "reason is blank; give why the run is asked for, or leave it out"
        raise ValueError(message)
    if len(reason) > REASON_MAX_LENGTH:
        message = (
            f"reason holds {len(reason)} characters; it holds at most "
            f"{REASON_MAX_LENGTH}"
        )
        raise ValueError(message)
    return reason


def plan_sha256(plan: Mapping[str, object]) -> str:
    """Return the sha256 of a plan as JSON, its keys sorted and no spaces.

    The plan is the ``data`` an unconfirmed call answers, so an agent can
    hash the plan it was shown and compare it with ``binding.planSha256``.
    """
    canonical = json.dumps(
        plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentCall:
    """One confirmed call of a gated tool, as its record keeps it."""

    tool: str
    arguments: Mapping[str, object]
    """The call's arguments, without the ones the server binds."""
    reason: str | None
    client: AgentClient


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the tool's own check said after the run."""

    verified: bool
    detail: str

    @classmethod
    def passed(cls, detail: str) -> Verdict:
        """Say the check passed: the run is verified."""
        return cls(verified=True, detail=detail)

    @classmethod
    def failed(cls, detail: str) -> Verdict:
        """Say the check did not pass: the run failed."""
        return cls(verified=False, detail=detail)


def _bounded(text: str, limit: int) -> str:
    """Keep ``text`` within ``limit`` characters, its end kept: it says why."""
    if len(text) <= limit:
        return text
    return _ELLIPSIS + text[-(limit - len(_ELLIPSIS)) :]


def failure_detail(stage: str, code: str, message: str) -> str:
    """Name a failed run's or check's code and keep its message's end.

    An engine failure carries the tail of the Ansible output, and its last
    lines, the failing task and the play recap, are the ones that say why.
    """
    head = f"{stage} failed: {code}: "
    return head + _bounded(message, _DETAIL_MAX - len(head))


class GrantedRun:
    """The record of one granted run, written before its first engine step.

    ``watch`` wraps the run's step runner: the record is saved, ``running``,
    when the first step starts, so a call refused before any step (a lock
    held, an artifact that does not verify) writes no record. ``settle``
    records the verdict, ``fail`` a run or check that raised.
    """

    def __init__(self, store: ProposalStore, record: OperatorProposal) -> None:
        """Hold the running record, not yet written."""
        self._store = store
        self._record = record
        self._path: Path | None = None

    @classmethod
    def prepare(  # noqa: PLR0913 - the record's every source.
        cls,
        store: ProposalStore,
        check: GrantCheck,
        call: AgentCall,
        *,
        binding: ProposalBinding,
        evidence: Iterable[str],
        now: datetime,
    ) -> GrantedRun:
        """Build the running record of a call ``check`` grants; write nothing."""
        if not check.self_confirm:
            message = "a run no grant covers writes no agent record of a run"
            raise ValueError(message)
        granted = check.environments[0]
        if granted.policy is None:
            message = "a granted environment names its policy"
            raise ValueError(message)
        record = _agent_record(
            check,
            call,
            status=ProposalStatus.RUNNING,
            approval=ApprovalRecord(
                mode=ApprovalMode.AGENT,
                policy=granted.policy,
                grant=check.operation_kind,
                client=call.client,
            ),
            binding=binding,
            evidence=evidence,
            now=now,
        )
        return cls(store, record)

    @property
    def record(self) -> OperatorProposal:
        """The record as last written, or as it will be."""
        return self._record

    @property
    def started(self) -> bool:
        """The record is on disk: the run's first step started."""
        return self._path is not None

    def begin(self) -> Path:
        """Write the running record, once; return its path."""
        if self._path is None:
            self._path = self._store.save(self._record)
        return self._path

    def watch(self, run: StepRunner) -> StepRunner:
        """Wrap ``run`` so the record is written before the first step starts."""

        def first_records(step: ExecutionStep) -> StepRun:
            self.begin()
            return run(step)

        return first_records

    def settle(self, verdict: Verdict, executed_at: datetime, now: datetime) -> Path:
        """Record the check's verdict: ``verified`` or ``failed``."""
        path = self.begin()
        status = ProposalStatus.VERIFIED if verdict.verified else ProposalStatus.FAILED
        self._record = replace(
            self._record,
            status=status,
            outcome=ProposalOutcome(
                executed_at=executed_at.isoformat(timespec="seconds"),
                verified_at=now.isoformat(timespec="seconds"),
                result=status.value,
                detail=_bounded(verdict.detail, _DETAIL_MAX),
            ),
        )
        self._store.update(self._record)
        return path

    def fail(self, detail: str, executed_at: datetime) -> Path | None:
        """Record a run or check that raised as ``failed``; none if nothing ran.

        A call refused before its first step left no record, and gets none.
        """
        if self._path is None:
            return None
        self._record = replace(
            self._record,
            status=ProposalStatus.FAILED,
            outcome=ProposalOutcome(
                executed_at=executed_at.isoformat(timespec="seconds"),
                verified_at=None,
                result=ProposalStatus.FAILED.value,
                detail=_bounded(detail, _DETAIL_MAX),
            ),
        )
        self._store.update(self._record)
        return self._path

    def approval(self) -> dict[str, object]:
        """Serialize what the tool's result says of the run's approval (S-018-5)."""
        approval = self._record.approval
        if approval is None or approval.policy is None or approval.grant is None:
            message = "a granted run's record names its policy and grant"
            raise ValueError(message)
        return {
            "mode": approval.mode.value,
            "policy": approval.policy.value,
            "grant": approval.grant.value,
            "record": str(self.begin()),
            "status": self._record.status.value,
        }


class ApprovedRun(GrantedRun):
    """The run of an agent proposal a person approved (S-018-11).

    The proposal's own record becomes the run's: ``running`` with the
    person's approval when the first step starts, read again under the
    store's lock so a second approval is refused, then settled by the tool's
    own check as a granted run is.
    """

    @classmethod
    def of(
        cls, store: ProposalStore, proposal: OperatorProposal, via: ApprovalChannel
    ) -> ApprovedRun:
        """Build the run of ``proposal``, approved by a person through ``via``."""
        if proposal.trigger_kind is not TriggerKind.AGENT:
            message = "only an agent proposal is run as its stored request"
            raise ValueError(message)
        return cls(
            store,
            replace(
                proposal,
                status=ProposalStatus.RUNNING,
                approval=ApprovalRecord(mode=ApprovalMode.HUMAN, policy=None, via=via),
            ),
        )

    def begin(self) -> Path:
        """Record the approved run as started, once; return its path."""
        if self._path is None:
            self._path = self._store.start(self._record)
        return self._path

    def approval(self) -> dict[str, object]:
        """Serialize what the result says of the run's approval."""
        approval = self._record.approval
        if approval is None or approval.via is None:
            message = "an approved run's record names the person's channel"
            raise ValueError(message)
        return {
            "mode": approval.mode.value,
            "via": approval.via.value,
            "proposal": self._record.resource_id.value,
            "record": str(self.begin()),
            "status": self._record.status.value,
        }


BINDING_CHANGED = "proposal_binding_changed"
"""``error.context.code`` of an approval whose proposal no longer matches."""


def binding_changes(
    proposal: OperatorProposal,
    *,
    binding: ProposalBinding,
    servers: Iterable[ResourceId],
) -> tuple[str, ...]:
    """Name each part of an agent proposal's binding that differs now (D-20).

    Empty when the run would change what the person read: the same servers,
    the same plan, commit and uncommitted changes.
    """
    recorded = proposal.binding
    if recorded is None:
        return ("the proposal records no binding",)
    found: list[str] = []
    now = tuple(servers)
    if now != proposal.operation_servers:
        was = ", ".join(server.value for server in proposal.operation_servers)
        found.append(
            f"the servers the run changes (were {was}, now "
            f"{', '.join(server.value for server in now) or 'none'})"
        )
    if binding.plan_sha256 != recorded.plan_sha256:
        found.append(
            f"the plan (its sha256 was {recorded.plan_sha256}, now "
            f"{binding.plan_sha256})"
        )
    if binding.git_commit != recorded.git_commit:
        found.append(
            f"the git commit (was {recorded.git_commit or 'none'}, now "
            f"{binding.git_commit or 'none'})"
        )
    if binding.worktree_digest != recorded.worktree_digest:
        found.append("the uncommitted changes in the project")
    return tuple(found)


def _agent_record(  # noqa: PLR0913 - the record's every source.
    check: GrantCheck,
    call: AgentCall,
    *,
    status: ProposalStatus,
    approval: ApprovalRecord | None,
    binding: ProposalBinding,
    evidence: Iterable[str],
    now: datetime,
) -> OperatorProposal:
    """Build the agent record of one confirmed call: a run's or a proposal's."""
    fingerprint = fingerprint_of(
        {
            "tool": call.tool,
            "arguments": dict(call.arguments),
            "servers": [server.value for server in check.servers],
        }
    )
    created_at = now.isoformat(timespec="seconds")
    summary = (
        _bounded(call.reason, _SUMMARY_MAX)
        if call.reason is not None
        else f"The agent called {call.tool} and gave no reason"
    )
    lines = [*evidence, *check.reasons()]
    return OperatorProposal(
        resource_id=proposal_identifier(fingerprint, created_at),
        created_at=created_at,
        status=status,
        trigger_kind=TriggerKind.AGENT,
        fingerprint=fingerprint,
        alert=None,
        drift=None,
        diagnosis_summary=summary,
        evidence=tuple(_bounded(line, _EVIDENCE_MAX) for line in lines if line),
        operation_kind=check.operation_kind,
        operation_server=None,
        operation_service=None,
        command=TOOL_COMMANDS[call.tool],
        approval=approval,
        outcome=None,
        request=AgentRequest(
            tool=call.tool, arguments=call.arguments, reason=call.reason
        ),
        operation_servers=check.servers,
        operation_environments=tuple(
            environment.environment for environment in check.environments
        ),
        binding=binding,
    )


# The call no grant covers (S-018): nothing runs, and one agent proposal in
# the store waits for a person's `cloudfall operator approve`.

APPROVAL_REQUIRED = "operator_approval_required"
"""``error.context.code`` of a confirmed call no grant covers."""

OPERATOR_APPROVE = "cloudfall operator approve"
"""The command a person approves an agent proposal with, at a terminal."""

_GRANT_FIELDS = frozenset({"selfConfirm", "self_confirm", "environments"})
"""The keys a plan's ``approval`` gains from the grant check (S-018-10)."""


def proposal_plan(plan: Mapping[str, object]) -> dict[str, object]:
    """Return the plan an agent proposal binds: the grant check left out.

    The grant check says whether the call could run on its own, which
    changes with the store's history, the hour and the rate limit, not with
    what the run does; a proposal made in quiet hours stays approvable after
    them. What the plan's ``approval`` holds besides (a deploy's release
    approval, S-008) stays, and an ``approval`` the grant alone filled goes.
    """
    bound = dict(plan)
    existing = bound.get("approval")
    if isinstance(existing, dict):
        kept = {
            key: value
            for key, value in existing.items()
            if key not in _GRANT_FIELDS
        }
        if kept:
            bound["approval"] = kept
        else:
            del bound["approval"]
    return bound


@dataclass(frozen=True, slots=True)
class AgentProposal:
    """The agent proposal a confirmed call no grant covers left in the store."""

    record: OperatorProposal
    path: Path
    reasons: tuple[str, ...]
    """One line per environment the run touches: the grant check's reason."""

    @property
    def command(self) -> str:
        """The command a person approves it with."""
        return f"{OPERATOR_APPROVE} {self.record.resource_id.value}"

    def message(self) -> str:
        """Say that nothing ran, and who approves the proposal, and how."""
        tool = self.record.request.tool if self.record.request is not None else "run"
        return (
            f"no OperatorPolicy grant covers this {tool} run in every environment "
            f"it touches, so nothing ran: proposal {self.record.resource_id.value} "
            f"waits for a person, who approves it at a terminal with `{self.command}`"
        )

    def context(self) -> dict[str, object]:
        """Return the refusal's ``error.context`` (S-018-1)."""
        return {
            "code": APPROVAL_REQUIRED,
            "proposal": self.record.resource_id.value,
            "command": self.command,
            "record": str(self.path),
            "reasons": list(self.reasons),
        }


def propose_call(  # noqa: PLR0913 - the proposal's every source.
    store: ProposalStore,
    check: GrantCheck,
    call: AgentCall,
    *,
    binding: ProposalBinding,
    evidence: Iterable[str],
    now: datetime,
) -> AgentProposal:
    """Leave a confirmed call no grant covers in the store, ``proposed``.

    The same call (tool, arguments and servers) bound to the same plan,
    commit and worktree while its proposal is still ``proposed`` returns
    that proposal and writes nothing (S-018-4).
    """
    if check.self_confirm:
        message = "a call a grant covers runs; it is not proposed"
        raise ValueError(message)
    record = _agent_record(
        check,
        call,
        status=ProposalStatus.PROPOSED,
        approval=None,
        binding=binding,
        evidence=evidence,
        now=now,
    )
    kept, path = store.open_or_save(record)
    return AgentProposal(record=kept, path=path, reasons=check.reasons())


def health_verdict(*, healthy: bool, servers: Iterable[ResourceId]) -> Verdict:
    """``deploy``, ``rollback``, ``restart``: the health gate passed."""
    names = ", ".join(server.value for server in servers)
    if healthy:
        return Verdict.passed(f"the health gate passed on {names}")
    return Verdict.failed(f"the health gate did not pass on {names}")


def migration_verdict(status: str, completed: int, total: int) -> Verdict:
    """``migrate``: every step of the plan completed."""
    if status == "ok" and completed == total:
        return Verdict.passed(f"every step of the migration completed ({total})")
    return Verdict.failed(
        f"the migration ended {status} with {completed} of {total} steps completed"
    )


_DATA_COPY_VERIFIED = "per-table row counts matched"
"""What a data-migration receipt's ``verification`` says of a verified copy."""


def data_copy_verdict(receipt: Path | None) -> Verdict:
    """``data_copy``: the copy's receipt reports it verified."""
    if receipt is None or not receipt.is_file():
        return Verdict.failed("the data copy wrote no receipt to verify it by")
    document = json.loads(receipt.read_text(encoding="utf-8"))
    spec = document.get("spec") if isinstance(document, dict) else None
    verification = spec.get("verification") if isinstance(spec, dict) else None
    if verification == _DATA_COPY_VERIFIED:
        return Verdict.passed(f"the copy's receipt {receipt.name}: {verification}")
    return Verdict.failed(
        f"the copy's receipt {receipt.name} does not report the copy verified"
    )


def domains_verdict(observed: Iterable[DomainObserved]) -> Verdict:
    """``domains_apply``, ``converge_domains``: each Domain matches its receipt.

    ``domains observe`` reads the vhost on the proxy server; it matches when
    its sha256 is the one the run's receipt declared.
    """
    readings = tuple(observed)
    if not readings:
        return Verdict.failed("no Domain was observed after the run")
    differing = [
        f"{reading.domain_id.value} on {reading.server.value}"
        for reading in readings
        if reading.receipt is None
        or reading.vhost.sha256 != reading.receipt.configuration_sha256
    ]
    if differing:
        return Verdict.failed(
            "domains observe does not match the declared vhost: "
            + "; ".join(differing)
        )
    names = ", ".join(reading.domain_id.value for reading in readings)
    return Verdict.passed(f"domains observe matches the declared vhost of {names}")


def audit_verdict(report: AuditReport, servers: Iterable[ResourceId]) -> Verdict:
    """``converge_baseline``, ``converge_services``: no changed server fails a check.

    A check that is not compliant fails, drift and unknown alike: the audit
    proves the run, or it did not verify.
    """
    wanted = [server.value for server in servers]
    audited = {audit.server_id: audit for audit in report.servers}
    failing: list[str] = []
    for server in wanted:
        audit = audited.get(server)
        if audit is None:
            failing.append(f"{server}: not audited")
            continue
        failing.extend(
            f"{server}: {check.check} {check.status.value}"
            for check in audit.checks
            if check.status is not AuditStatus.COMPLIANT
        )
    if failing:
        return Verdict.failed(
            "the audit after the run reports failing checks: " + "; ".join(failing)
        )
    return Verdict.passed(f"an audit of {', '.join(wanted)} reports no failing check")
