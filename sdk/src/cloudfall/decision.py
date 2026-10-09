"""The gate and the record: what an agent proposed and who let it.

An operation from the catalog is not run because an agent decided to run
it. A `read` operation runs freely; a `mutating` one runs in check mode
first, and what the diff showed is recorded before anyone approves; a
`destructive` one always waits for a human. The gate lives here rather
than in the agent's instructions, because an agent can be talked out of
its instructions and cannot be talked out of a process.

What the record holds is the point of the product. A job history says a
job ran and a client's tool log says a tool was called. This says which
operation was proposed against which targets, which fleet evidence it was
based on, what check mode reported before anything changed, who approved
it, and what came of it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, cast

from jsonschema.exceptions import ValidationError

from cloudfall.catalog import RiskLevel, TargetScope
from cloudfall.domain import ResourceId

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from cloudfall.catalog import Operation
    from cloudfall.validation import SchemaCatalog

type CheckRunner = Callable[[Sequence[str], Mapping[str, str], Path], int]
"""Runs one check-mode invocation and returns its exit code."""

API_VERSION = "cloudfall/v1"
DECISION_KIND = "OperationDecision"
DECISION_SCHEMA = "operation-decision.schema.json"
DECISION_DIRECTORY = "decisions"
"""Where decision records and their artifacts land inside the repository.

Beside the operations rather than under ``tmp/``: the record is what the
team keeps, and the runtime directory is what they throw away. Commit the
records; the diffs and logs beside them are raw Ansible output, so they
belong in git only in a repository whose tasks set ``no_log`` where it
matters.
"""

_TREE_DIRECTORY = "tree"
_DIFF_SUFFIX = ".diff"
_ANSIBLE_CONFIG_FILE = "ansible.cfg"
CHECK_TIMEOUT_SECONDS = 1800
_CHANGED_KEY = "changed"

_ERROR_ANSIBLE_MISSING = "decision_ansible_missing"
_ERROR_TARGET_REQUIRED = "decision_target_required"
_ERROR_TARGET_UNEXPECTED = "decision_target_unexpected"
_ERROR_INPUT_UNKNOWN = "decision_input_unknown"
_ERROR_INPUT_MISSING = "decision_input_missing"
_ERROR_INPUT_TYPE = "decision_input_type"
_ERROR_DECISION_EXISTS = "decision_exists"
ERROR_DECISION_MISSING = "decision_missing"
_ERROR_NOT_PROPOSED = "decision_not_proposed"
_ERROR_APPROVER_UNKNOWN = "decision_approver_unknown"
_ERROR_CHANNEL_UNKNOWN = "decision_approval_channel_unknown"
ERROR_RECORD_INVALID = "decision_record_invalid"


class DecisionError(RuntimeError):
    """Fail-fast gate or record error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        """Record the failure code and human-readable detail."""
        self.code = code
        self.detail = message
        super().__init__(f"{code}: {message}")

    def as_dict(self) -> dict[str, object]:
        """Serialize the error envelope for system boundaries."""
        return {
            "status": "error",
            "error": {"code": self.code, "message": self.detail},
        }


class Requirement(StrEnum):
    """What the gate requires before an operation may change the fleet."""

    RUNS_FREELY = "runs-freely"
    """A read operation: nothing to gate."""

    CHECK_THEN_APPROVE = "check-then-approve"
    """Check mode first, the diff recorded, then an approval."""

    HUMAN_REQUIRED = "human-required"
    """A destructive operation: only a person may let it through."""


class DecisionStatus(StrEnum):
    """Where one decision stands."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    VERIFIED = "verified"
    FAILED = "failed"
    RAN = "ran"
    """A read operation: it ran when it was proposed; nothing waits on it."""

    SUPERSEDED = "superseded"
    """A newer proposal of the same operation, targets and inputs replaced it."""


def gate(risk: RiskLevel) -> tuple[Requirement, str]:
    """Return what the risk level requires, and why, for the record.

    The table is the design's: a read runs freely, a mutating operation
    needs check mode and an approval, and a destructive one needs a human
    whatever any policy says.
    """
    if risk is RiskLevel.READ:
        return (
            Requirement.RUNS_FREELY,
            "a read operation changes nothing, so nothing gates it",
        )
    if risk is RiskLevel.MUTATING:
        return (
            Requirement.CHECK_THEN_APPROVE,
            "a mutating operation runs in check mode first and waits for an "
            "approval of the recorded diff",
        )
    return (
        Requirement.HUMAN_REQUIRED,
        "a destructive operation cannot be undone, so only a person may "
        "let it through",
    )


@dataclass(frozen=True, slots=True)
class Targets:
    """What one proposed run is aimed at."""

    scope: TargetScope
    pattern: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Serialize the targets for the record."""
        result: dict[str, object] = {"scope": self.scope.value}
        if self.pattern is not None:
            result["pattern"] = self.pattern
        return result


@dataclass(frozen=True, slots=True)
class DiffArtifact:
    """The check-mode diff a human reads, cited by content."""

    path: Path
    sha256: str
    size: int

    def as_dict(self) -> dict[str, object]:
        """Serialize the artifact reference for the record."""
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "bytes": self.size,
        }


@dataclass(frozen=True, slots=True)
class CheckPreview:
    """What check mode reported before anything changed."""

    exit_code: int
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    diff: DiffArtifact

    def as_dict(self) -> dict[str, object]:
        """Serialize the preview for the record."""
        return {
            "exitCode": self.exit_code,
            "changed": list(self.changed),
            "unchanged": list(self.unchanged),
            "diff": self.diff.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class RunRecord:
    """What one real run did, kept beside what check mode predicted."""

    ran_at: str
    exit_code: int
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    log: DiffArtifact

    def as_dict(self) -> dict[str, object]:
        """Serialize the run for the record."""
        return {
            "ranAt": self.ran_at,
            "exitCode": self.exit_code,
            "changed": list(self.changed),
            "unchanged": list(self.unchanged),
            "log": self.log.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class Basis:
    """The fleet evidence a proposal was made against."""

    observations: Path
    observed_at: Mapping[str, str]

    def as_dict(self) -> dict[str, object]:
        """Serialize the basis for the record."""
        return {
            "observations": str(self.observations),
            "observedAt": dict(sorted(self.observed_at.items())),
        }


class ApprovalChannel(StrEnum):
    """How an approval reached the record.

    A person typing the decision id back at a terminal is the only channel
    an approval is given through. A record written before the channel was
    recorded says nothing about it, and reads as unknown.
    """

    TERMINAL = "terminal"
    """A person typed the decision id at a terminal."""

    UNKNOWN = "unknown"
    """The record predates the channel; it cannot say how the approval came."""

    @classmethod
    def from_record(cls, value: object) -> ApprovalChannel:
        """Read the channel a record names; a record that names none is unknown."""
        if value is None:
            return cls.UNKNOWN
        return cls(str(value))


@dataclass(frozen=True, slots=True)
class Approval:
    """Who let one operation through, when, and how it arrived."""

    approver: str
    approved_at: str
    via: ApprovalChannel

    def as_dict(self) -> dict[str, object]:
        """Serialize the approval for the record."""
        return {
            "approver": self.approver,
            "approvedAt": self.approved_at,
            "via": self.via.value,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """One decision: the proposal, its basis, its check run and its fate."""

    decision_id: ResourceId
    proposed_at: str
    operation_id: ResourceId
    playbook: Path
    risk: RiskLevel
    verify_playbook: Path | None
    targets: Targets
    inputs: Mapping[str, object]
    requirement: Requirement
    reason: str
    basis: Basis
    check: CheckPreview
    status: DecisionStatus
    approval: Approval | None = None
    execution: RunRecord | None = None
    verification: RunRecord | None = None
    verdict: str | None = None
    ran_exit_code: int | None = None
    superseded_by: ResourceId | None = None

    def same_call(self, other: Decision) -> bool:
        """Return whether both name the same operation, targets and inputs."""
        return (
            self.operation_id == other.operation_id
            and self.targets == other.targets
            and _canonical(self.inputs) == _canonical(other.inputs)
        )

    def as_document(self) -> dict[str, object]:
        """Serialize the decision as a schema-valid record document."""
        operation: dict[str, object] = {
            "id": self.operation_id.value,
            "playbook": str(self.playbook),
            "risk": self.risk.value,
        }
        if self.verify_playbook is not None:
            operation["verifyPlaybook"] = str(self.verify_playbook)
        spec: dict[str, object] = {
            "proposedAt": self.proposed_at,
            "operation": operation,
            "targets": self.targets.as_dict(),
            "inputs": dict(sorted(self.inputs.items())),
            "gate": {"requirement": self.requirement.value, "reason": self.reason},
            "basis": self.basis.as_dict(),
            "check": self.check.as_dict(),
            "status": self.status.value,
        }
        if self.approval is not None:
            spec["approval"] = self.approval.as_dict()
        if self.execution is not None:
            spec["execution"] = self.execution.as_dict()
        if self.verification is not None:
            spec["verify"] = self.verification.as_dict()
        if self.verdict is not None:
            spec["verdict"] = self.verdict
        if self.ran_exit_code is not None:
            spec["ranExitCode"] = self.ran_exit_code
        if self.superseded_by is not None:
            spec["supersededBy"] = self.superseded_by.value
        return {
            "apiVersion": API_VERSION,
            "kind": DECISION_KIND,
            "metadata": {"id": self.decision_id.value},
            "spec": spec,
        }

    def as_dict(self) -> dict[str, object]:
        """Serialize the decision for system boundaries."""
        return {"status": "ok", "decision": self.as_document()}


@dataclass(frozen=True, slots=True)
class DecisionStore:
    """Schema-validated decision records in one directory."""

    directory: Path
    catalog: SchemaCatalog

    def save(self, decision: Decision) -> Path:
        """Persist a new decision, refusing to overwrite an existing one."""
        path = self._path(decision.decision_id)
        if path.exists():
            message = f"decision already exists: {path}"
            raise DecisionError(_ERROR_DECISION_EXISTS, message)
        return self._write(path, decision)

    def update(self, decision: Decision) -> Path:
        """Persist a state change of an existing decision."""
        path = self._path(decision.decision_id)
        if not path.exists():
            message = f"decision does not exist: {path}"
            raise DecisionError(ERROR_DECISION_MISSING, message)
        return self._write(path, decision)

    def load(self, decision_id: ResourceId) -> Decision:
        """Load and re-validate one decision record."""
        path = self._path(decision_id)
        if not path.is_file():
            message = f"decision does not exist: {path}"
            raise DecisionError(ERROR_DECISION_MISSING, message)
        try:
            document = cast(
                "dict[str, object]", json.loads(path.read_text(encoding="utf-8"))
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            message = f"decision record is not valid JSON: {path}: {error}"
            raise DecisionError(ERROR_RECORD_INVALID, message) from error
        try:
            self.catalog.validate_named(DECISION_SCHEMA, document)
        except ValidationError as error:
            field = ".".join(str(part) for part in error.absolute_path) or "(root)"
            message = (
                f"decision record does not match its schema: {path}: "
                f"{field}: {error.message}"
            )
            raise DecisionError(ERROR_RECORD_INVALID, message) from error
        return _decision_from_document(document)

    def list(self) -> tuple[Decision, ...]:
        """Load every decision record, oldest proposal first."""
        if not self.directory.is_dir():
            return ()
        decisions = [
            self.load(ResourceId.from_boundary(path.stem))
            for path in sorted(self.directory.glob("*.json"))
        ]
        return tuple(sorted(decisions, key=lambda entry: entry.proposed_at))

    def _path(self, decision_id: ResourceId) -> Path:
        return self.directory / f"{decision_id.value}.json"

    def _write(self, path: Path, decision: Decision) -> Path:
        document = decision.as_document()
        self.catalog.validate_named(DECISION_SCHEMA, document)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{json.dumps(document, indent=2, sort_keys=True)}\n", encoding="utf-8"
        )
        return path


@dataclass(frozen=True, slots=True)
class ProposalRequest:
    """One proposed run of a declared operation."""

    operation: Operation
    targets: Targets
    inputs: Mapping[str, object]
    repository: Path
    observations: Path


def propose(
    request: ProposalRequest,
    store: DecisionStore,
    now: Callable[[], datetime] | None = None,
    run: CheckRunner | None = None,
) -> Decision:
    """Run the operation in check mode and record what it would do.

    Nothing on the fleet changes: check mode is the whole point of the
    step. The record it leaves is what an approval is given against.

    A read operation has run by the time it is recorded, so it is recorded
    as ``ran`` with its exit code rather than left waiting. An older open
    proposal of the same operation, targets and inputs is marked
    ``superseded`` by the new one: approving the older one would run what
    the newer check already replaced.
    """
    operation = request.operation
    declared = _validated_inputs(operation, request.inputs)
    pattern = _validated_targets(operation, request.targets)
    # Read before anything runs: a record that does not load stops the
    # proposal here rather than after check mode ran.
    recorded = store.list()
    moment = (now or _utc_now)()
    decision_id = _decision_identifier(operation.operation_id, moment)
    diff_path = store.directory / f"{decision_id.value}{_DIFF_SUFFIX}"
    tree = store.directory / _TREE_DIRECTORY / decision_id.value
    argv, environment = run_command(
        PlaybookInvocation(
            playbook=operation.playbook,
            repository=request.repository,
            tree=tree,
            pattern=pattern,
            inputs=declared,
        ),
        check=True,
    )
    diff_path.parent.mkdir(parents=True, exist_ok=True)
    exit_code = (run or _run_check)(argv, environment, diff_path)
    requirement, reason = gate(operation.risk)
    ran = requirement is Requirement.RUNS_FREELY
    decision = Decision(
        decision_id=decision_id,
        proposed_at=_timestamp(moment),
        operation_id=operation.operation_id,
        playbook=operation.playbook,
        risk=operation.risk,
        verify_playbook=(
            operation.verify.playbook if operation.verify is not None else None
        ),
        targets=Targets(scope=request.targets.scope, pattern=pattern),
        inputs=declared,
        requirement=requirement,
        reason=reason,
        basis=read_basis(request.observations, request.repository),
        check=_preview(exit_code, tree, diff_path, request.repository),
        status=DecisionStatus.RAN if ran else DecisionStatus.PROPOSED,
        verdict=(
            f"a read operation runs when it is proposed; it exited {exit_code}"
            if ran
            else None
        ),
        ran_exit_code=exit_code if ran else None,
    )
    store.save(decision)
    for older in recorded:
        if older.status is DecisionStatus.PROPOSED and older.same_call(decision):
            store.update(
                replace(
                    older,
                    status=DecisionStatus.SUPERSEDED,
                    superseded_by=decision_id,
                    verdict=(
                        f"{decision_id.value} proposed the same operation, "
                        "targets and inputs again"
                    ),
                )
            )
    return decision


@dataclass(frozen=True, slots=True)
class PlaybookInvocation:
    """One playbook run against one set of targets."""

    playbook: Path
    repository: Path
    tree: Path
    pattern: str | None = None
    inputs: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """One approval of a recorded proposal."""

    decision: Decision
    approver: str
    repository: Path
    via: ApprovalChannel


@dataclass(frozen=True, slots=True)
class _Stage:
    """What every stage of one approved decision shares."""

    request: ApprovalRequest
    store: DecisionStore
    runner: CheckRunner


def approve(
    request: ApprovalRequest,
    store: DecisionStore,
    now: Callable[[], datetime] | None = None,
    run: CheckRunner | None = None,
) -> Decision:
    """Record who let one proposal through, run it, and verify it.

    Everything the run needs comes from the record rather than from the
    caller or the catalog: the approval is of what was proposed, so the
    playbook, the targets and the inputs cannot change between the diff a
    human read and the run that follows it.
    """
    decision = request.decision
    refuse_unapprovable(decision)
    if not request.approver.strip():
        message = "an approval records who gave it; pass --approver or set USER"
        raise DecisionError(_ERROR_APPROVER_UNKNOWN, message)
    if request.via is ApprovalChannel.UNKNOWN:
        message = (
            "an approval records how it arrived; a new one cannot be of an "
            "unknown channel"
        )
        raise DecisionError(_ERROR_CHANNEL_UNKNOWN, message)
    clock = now or _utc_now
    stage = _Stage(request=request, store=store, runner=run or _run_check)
    moment = clock()
    execution = _run_stage(stage, decision.playbook, "execution", moment)
    verification = (
        _run_stage(stage, decision.verify_playbook, "verify", clock())
        if decision.verify_playbook is not None and execution.exit_code == 0
        else None
    )
    status, verdict = _outcome(execution, verification, decision.verify_playbook)
    approved = replace(
        decision,
        approval=Approval(
            approver=request.approver.strip(),
            approved_at=_timestamp(moment),
            via=request.via,
        ),
        execution=execution,
        verification=verification,
        status=status,
        verdict=verdict,
    )
    store.update(approved)
    return approved


def refuse_unapprovable(decision: Decision) -> None:
    """Refuse a decision that is not an open proposal, before anything runs.

    Callers that ask a person for the decision id check this first, so
    nobody types an id only to hear there was nothing to approve.
    """
    if decision.status is DecisionStatus.PROPOSED:
        return
    if decision.status is DecisionStatus.SUPERSEDED:
        # A read is recorded as ran, so the decision that replaced an old
        # read record has nothing left to approve either.
        instead = (
            "it ran when it was proposed, so there is nothing to approve"
            if decision.requirement is Requirement.RUNS_FREELY
            else "approve that one instead"
        )
        message = (
            f"decision {decision.decision_id} is superseded by "
            f"{decision.superseded_by}, a newer proposal of the same operation, "
            f"targets and inputs; {instead}"
        )
    elif decision.status is DecisionStatus.RAN:
        message = (
            f"decision {decision.decision_id} is a read operation that ran when "
            "it was proposed, so there is nothing to approve"
        )
    else:
        message = (
            f"decision {decision.decision_id} is {decision.status.value}, so "
            "there is nothing left to approve"
        )
    raise DecisionError(_ERROR_NOT_PROPOSED, message)


def _outcome(
    execution: RunRecord,
    verification: RunRecord | None,
    verify_playbook: Path | None,
) -> tuple[DecisionStatus, str]:
    """Return the status one run earned, and why, for the record.

    Verification is a claim about the fleet's state, so a verify run that
    changed something did not verify anything: it found the fleet not as
    the operation left it and converged it further. Exiting zero is not
    enough.
    """
    if execution.exit_code != 0:
        return (
            DecisionStatus.FAILED,
            f"the run exited {execution.exit_code}",
        )
    if verify_playbook is None:
        return (
            DecisionStatus.EXECUTED,
            "the run succeeded; the operation declares no verify step",
        )
    if verification is None:  # pragma: no cover - callers verify after a clean run
        return (DecisionStatus.FAILED, "the verify step did not run")
    if verification.exit_code != 0:
        return (
            DecisionStatus.FAILED,
            f"the verify step exited {verification.exit_code}",
        )
    if verification.changed:
        changed = ", ".join(verification.changed)
        return (
            DecisionStatus.FAILED,
            "the verify step changed "
            f"{changed}, so the fleet was not in the state the run claimed",
        )
    return (
        DecisionStatus.VERIFIED,
        "the run succeeded and the verify step changed nothing",
    )


def _run_stage(
    stage: _Stage, playbook: Path, name: str, moment: datetime
) -> RunRecord:
    """Run one stage of an approved decision and record what it did."""
    decision = stage.request.decision
    identifier = decision.decision_id.value
    log_path = stage.store.directory / f"{identifier}-{name}.log"
    invocation = PlaybookInvocation(
        playbook=playbook,
        repository=stage.request.repository,
        tree=stage.store.directory / _TREE_DIRECTORY / f"{identifier}-{name}",
        pattern=decision.targets.pattern,
        inputs=decision.inputs,
    )
    argv, environment = run_command(invocation)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    exit_code = stage.runner(argv, environment, log_path)
    preview = _preview(
        exit_code, invocation.tree, log_path, stage.request.repository
    )
    return RunRecord(
        ran_at=_timestamp(moment),
        exit_code=exit_code,
        changed=preview.changed,
        unchanged=preview.unchanged,
        log=preview.diff,
    )


def run_command(
    invocation: PlaybookInvocation, *, check: bool = False
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Return one ``ansible-playbook`` invocation and its environment.

    The team's own configuration decides how Ansible connects and where
    their roles live, because this is their playbook: nothing about the
    engine belongs in this run.
    """
    binary = shutil.which("ansible-playbook")
    if binary is None:
        message = "ansible-playbook is not installed on this controller"
        raise DecisionError(_ERROR_ANSIBLE_MISSING, message)
    argv = [binary, "--check", "--diff"] if check else [binary, "--diff"]
    if invocation.pattern is not None:
        argv.extend(("--limit", invocation.pattern))
    if invocation.inputs:
        argv.extend(
            ("--extra-vars", json.dumps(dict(invocation.inputs), sort_keys=True))
        )
    argv.append(str(invocation.repository / invocation.playbook))
    environment = {
        "ANSIBLE_CALLBACKS_ENABLED": _TREE_DIRECTORY,
        "ANSIBLE_CALLBACK_TREE_DIR": str(invocation.tree.resolve()),
    }
    configuration = invocation.repository / _ANSIBLE_CONFIG_FILE
    if configuration.is_file():
        environment["ANSIBLE_CONFIG"] = str(configuration.resolve())
    return tuple(argv), environment


def repository_relative(path: Path, repository: Path) -> Path:
    """Return the path as the repository sees it, when it lies inside it.

    Records are committed, so a path that names one machine's home
    directory is worse than useless to everyone else reading the trail.
    """
    try:
        return path.resolve().relative_to(repository.resolve())
    except ValueError:
        return path


def read_basis(observations: Path, repository: Path | None = None) -> Basis:
    """Return the observation evidence a proposal cites.

    A proposal made against no evidence says so, rather than implying it
    looked at the fleet.
    """
    observed_at: dict[str, str] = {}
    if observations.is_dir():
        for path in sorted(observations.glob("*.json")):
            document = cast(
                "object", json.loads(path.read_text(encoding="utf-8"))
            )
            if not isinstance(document, dict):
                continue
            spec = document.get("spec")
            if isinstance(spec, dict) and isinstance(spec.get("observedAt"), str):
                observed_at[path.stem] = str(spec["observedAt"])
    return Basis(
        observations=(
            repository_relative(observations, repository)
            if repository is not None
            else observations
        ),
        observed_at=observed_at,
    )


def _preview(
    exit_code: int, tree: Path, diff_path: Path, repository: Path | None = None
) -> CheckPreview:
    changed: list[str] = []
    unchanged: list[str] = []
    if tree.is_dir():
        for path in sorted(tree.iterdir()):
            if not path.is_file():
                continue
            document = cast(
                "object", json.loads(path.read_text(encoding="utf-8"))
            )
            reported = (
                bool(document.get(_CHANGED_KEY))
                if isinstance(document, dict)
                else False
            )
            (changed if reported else unchanged).append(path.name)
    content = diff_path.read_bytes() if diff_path.is_file() else b""
    return CheckPreview(
        exit_code=exit_code,
        changed=tuple(changed),
        unchanged=tuple(unchanged),
        diff=DiffArtifact(
            path=(
                repository_relative(diff_path, repository)
                if repository is not None
                else diff_path
            ),
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
        ),
    )


def _validated_targets(operation: Operation, targets: Targets) -> str | None:
    if targets.scope is not operation.targets:
        message = (
            f"operation {operation.operation_id} targets "
            f"{operation.targets.value}, not {targets.scope.value}"
        )
        raise DecisionError(_ERROR_TARGET_UNEXPECTED, message)
    if operation.targets is TargetScope.FLEET:
        if targets.pattern is not None:
            message = (
                f"operation {operation.operation_id} targets the fleet, so it "
                "takes no target pattern"
            )
            raise DecisionError(_ERROR_TARGET_UNEXPECTED, message)
        return None
    if targets.pattern is None or not targets.pattern.strip():
        message = (
            f"operation {operation.operation_id} targets one "
            f"{operation.targets.value}; name it with --target"
        )
        raise DecisionError(_ERROR_TARGET_REQUIRED, message)
    return targets.pattern.strip()


def _validated_inputs(
    operation: Operation, inputs: Mapping[str, object]
) -> dict[str, object]:
    declared = {str(entry.name): entry for entry in operation.inputs}
    for name in inputs:
        if name not in declared:
            message = (
                f"operation {operation.operation_id} declares no input {name}; "
                f"declared inputs: {', '.join(sorted(declared)) or 'none'}"
            )
            raise DecisionError(_ERROR_INPUT_UNKNOWN, message)
    validated: dict[str, object] = {}
    for name, entry in sorted(declared.items()):
        if name not in inputs:
            if entry.required:
                message = (
                    f"operation {operation.operation_id} requires input {name} "
                    f"of type {entry.type.value}"
                )
                raise DecisionError(_ERROR_INPUT_MISSING, message)
            continue
        validated[name] = _coerced(operation, entry.type.value, name, inputs[name])
    return validated


def _coerced(
    operation: Operation, declared_type: str, name: str, value: object
) -> object:
    if declared_type == "string":
        return str(value)
    text = str(value).strip()
    if declared_type == "integer":
        try:
            return int(text)
        except ValueError as error:
            message = (
                f"input {name} of operation {operation.operation_id} must be an "
                f"integer, got {value!r}"
            )
            raise DecisionError(_ERROR_INPUT_TYPE, message) from error
    lowered = text.lower()
    if lowered in {"true", "yes", "1"}:
        return True
    if lowered in {"false", "no", "0"}:
        return False
    message = (
        f"input {name} of operation {operation.operation_id} must be a boolean, "
        f"got {value!r}"
    )
    raise DecisionError(_ERROR_INPUT_TYPE, message)


def _canonical(inputs: Mapping[str, object]) -> str:
    """Return the inputs as one comparable text: ``1`` and ``true`` differ."""
    return json.dumps(dict(inputs), sort_keys=True)


def _decision_identifier(operation_id: ResourceId, moment: datetime) -> ResourceId:
    stamp = moment.strftime("%Y%m%d%H%M%S")
    return ResourceId.from_boundary(f"{operation_id.value}-{stamp}")


def _timestamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _run_check(
    argv: Sequence[str], environment: Mapping[str, str], diff_path: Path
) -> int:
    """Run check mode, keeping the diff a human reads as an artifact."""
    with diff_path.open("w", encoding="utf-8") as stream:
        completed = subprocess.run(  # noqa: S603 - resolved binary, built argv.
            tuple(argv),
            env={**os.environ, **environment},
            stdout=stream,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=CHECK_TIMEOUT_SECONDS,
        )
    return completed.returncode


def _run_from_document(recorded: object) -> RunRecord | None:
    """Read back what a run did, or nothing when it has not run yet."""
    if not isinstance(recorded, dict):
        return None
    log = cast("Mapping[str, object]", recorded["log"])
    return RunRecord(
        ran_at=str(recorded["ranAt"]),
        exit_code=int(cast("int", recorded["exitCode"])),
        changed=tuple(cast("Sequence[str]", recorded["changed"])),
        unchanged=tuple(cast("Sequence[str]", recorded["unchanged"])),
        log=DiffArtifact(
            path=Path(str(log["path"])),
            sha256=str(log["sha256"]),
            size=int(cast("int", log["bytes"])),
        ),
    )


def _decision_from_document(document: Mapping[str, object]) -> Decision:
    metadata = cast("Mapping[str, object]", document["metadata"])
    spec = cast("Mapping[str, object]", document["spec"])
    operation = cast("Mapping[str, object]", spec["operation"])
    targets = cast("Mapping[str, object]", spec["targets"])
    gate_record = cast("Mapping[str, object]", spec["gate"])
    basis = cast("Mapping[str, object]", spec["basis"])
    check = cast("Mapping[str, object]", spec["check"])
    diff = cast("Mapping[str, object]", check["diff"])
    approval = spec.get("approval")
    verify = operation.get("verifyPlaybook")
    pattern = targets.get("pattern")
    return Decision(
        decision_id=ResourceId.from_boundary(metadata["id"]),
        proposed_at=str(spec["proposedAt"]),
        operation_id=ResourceId.from_boundary(operation["id"]),
        playbook=Path(str(operation["playbook"])),
        risk=RiskLevel(str(operation["risk"])),
        verify_playbook=Path(str(verify)) if verify is not None else None,
        targets=Targets(
            scope=TargetScope(str(targets["scope"])),
            pattern=str(pattern) if pattern is not None else None,
        ),
        inputs=cast("Mapping[str, object]", spec["inputs"]),
        requirement=Requirement(str(gate_record["requirement"])),
        reason=str(gate_record["reason"]),
        basis=Basis(
            observations=Path(str(basis["observations"])),
            observed_at=cast("Mapping[str, str]", basis["observedAt"]),
        ),
        check=CheckPreview(
            exit_code=int(cast("int", check["exitCode"])),
            changed=tuple(cast("Sequence[str]", check["changed"])),
            unchanged=tuple(cast("Sequence[str]", check["unchanged"])),
            diff=DiffArtifact(
                path=Path(str(diff["path"])),
                sha256=str(diff["sha256"]),
                size=int(cast("int", diff["bytes"])),
            ),
        ),
        status=DecisionStatus(str(spec["status"])),
        verdict=str(spec["verdict"]) if "verdict" in spec else None,
        ran_exit_code=(
            int(cast("int", spec["ranExitCode"])) if "ranExitCode" in spec else None
        ),
        superseded_by=(
            ResourceId.from_boundary(spec["supersededBy"])
            if "supersededBy" in spec
            else None
        ),
        execution=_run_from_document(spec.get("execution")),
        verification=_run_from_document(spec.get("verify")),
        approval=_approval_from_document(approval),
    )


def _approval_from_document(approval: object) -> Approval | None:
    if not isinstance(approval, dict):
        return None
    record = cast("Mapping[str, object]", approval)
    return Approval(
        approver=str(record["approver"]),
        approved_at=str(record["approvedAt"]),
        via=ApprovalChannel.from_record(record.get("via")),
    )
