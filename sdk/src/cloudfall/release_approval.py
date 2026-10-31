"""A person's approval of one release of one component (S-008).

``cloudfall release approve`` writes one ``ReleaseApproval`` record once a
person typed the release id at a terminal. The record names the archive
digest, the git commit and the servers it approves, and when it expires; it
holds no secret. This module builds and writes the record, and reads the
records back for ``deploy``, which counts one only when it still names the
archive and the servers being deployed and has not expired. Nothing here
deploys.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from jsonschema.exceptions import ValidationError

from cloudfall.decision import ApprovalChannel
from cloudfall.domain import DeploymentApproval, ReleaseId, ResourceId, Sha256Digest
from cloudfall.lifecycle import LifecycleError, find_component, verify_artifact
from cloudfall.validation import SchemaCatalog

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from cloudfall.inventory import PlatformInventory

RELEASE_APPROVAL_SCHEMA = "release-approval.schema.json"
APPROVALS_DIRECTORY = "decisions/releases"
"""Where approvals are kept by default: beside the decision records."""

ERROR_APPROVAL_NOT_REQUIRED = "release_approval_not_required"
"""The component's application deploys ``automatic``: a record would gate nothing."""
ERROR_APPROVER_MISSING = "release_approver_missing"
ERROR_APPROVER_INVALID = "release_approver_invalid"
ERROR_APPROVAL_EXISTS = "release_approval_exists"
"""Two approvals of one release in the same second share an id."""
ERROR_CHANNEL_UNKNOWN = "release_approval_channel_unknown"
ERROR_VALIDITY_INVALID = "release_approval_validity_invalid"
ERROR_RECORD_INVALID = "release_approval_record_invalid"
"""A file in the approvals folder is not a valid ``ReleaseApproval``."""
ERROR_DEPLOYMENT_APPROVAL_MISSING = "deployment_approval_missing"
"""A ``manual`` application's release has no approval that counts."""

VALID_FOR_MINIMUM = 1
VALID_FOR_MAXIMUM = 168
"""An approval counts for one hour at least and a week at most."""
VALID_FOR_DEFAULT = 24

_KIND = "ReleaseApproval"
_API_VERSION = "cloudfall/v1"
_ID_PREFIX = "approval"
_FINGERPRINT_LENGTH = 16
_APPROVER_MAX_LENGTH = 200
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}")
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ReleaseApprovalError(RuntimeError):
    """Fail-fast release approval error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        """Record the failure code and human-readable detail."""
        self.code = code
        self.detail = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ValidityHours:
    """How long an approval counts: whole hours, from one to a week."""

    value: int

    def __post_init__(self) -> None:
        """Refuse a non-integer, or a count outside one to 168 hours."""
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int)
            or not VALID_FOR_MINIMUM <= self.value <= VALID_FOR_MAXIMUM
        ):
            message = (
                f"an approval is valid for {VALID_FOR_MINIMUM} to "
                f"{VALID_FOR_MAXIMUM} whole hours, got {self.value!r}"
            )
            raise ReleaseApprovalError(ERROR_VALIDITY_INVALID, message)

    @property
    def duration(self) -> timedelta:
        """Return the validity as a time span."""
        return timedelta(hours=self.value)


@dataclass(frozen=True, slots=True)
class GitCommit:
    """A full lowercase git commit id, as artifact metadata records it."""

    value: str

    def __post_init__(self) -> None:
        """Enforce 40 lowercase hexadecimal characters."""
        if not _GIT_COMMIT.fullmatch(self.value):
            message = f"invalid git commit: {self.value!r}"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Approver:
    """Who approved: a non-blank name, as ``decisions approve`` records it."""

    value: str

    def __post_init__(self) -> None:
        """Refuse a blank, overlong or unprintable approver."""
        if not self.value.strip():
            message = "no approver: pass --approver or set USER"
            raise ReleaseApprovalError(ERROR_APPROVER_MISSING, message)
        if len(self.value) > _APPROVER_MAX_LENGTH:
            message = f"the approver is longer than {_APPROVER_MAX_LENGTH} characters"
            raise ReleaseApprovalError(ERROR_APPROVER_INVALID, message)
        # A plan shows the approver later: a newline, an escape sequence or a
        # bidi override could forge or hide a line there.
        if not self.value.isprintable():
            message = f"the approver has a control character: {self.value!r}"
            raise ReleaseApprovalError(ERROR_APPROVER_INVALID, message)


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    """One verified release of one component of a ``manual`` application."""

    application_id: ResourceId
    component_id: ResourceId
    release: ReleaseId
    artifact_sha256: Sha256Digest
    git_commit: GitCommit
    server_ids: tuple[ResourceId, ...]

    def describe(self) -> str:
        """Name what a person approves, for the prompt."""
        return (
            f"Release {self.release.value} of component {self.component_id} "
            f"(application {self.application_id}): git commit "
            f"{self.git_commit.value}, archive SHA-256 "
            f"{self.artifact_sha256.value}, servers "
            f"{', '.join(server.value for server in self.server_ids)}"
        )


def release_candidate(
    inventory: PlatformInventory,
    component_id: ResourceId,
    release: ReleaseId,
    artifacts: Path,
    schemas: Path,
) -> ReleaseCandidate:
    """Return what would be approved, checked as ``deploy`` checks it.

    Raises ``LifecycleError`` for an undeclared component or an artifact
    that does not verify, and ``ReleaseApprovalError`` for a component of
    an ``automatic`` application.
    """
    candidate = deployment_candidate(
        inventory, component_id, release, artifacts, schemas
    )
    if candidate is None:
        component = find_component(inventory, component_id)
        message = (
            f"application {component.application_id} deploys automatic; an "
            f"approval of {component_id} would gate nothing"
        )
        raise ReleaseApprovalError(ERROR_APPROVAL_NOT_REQUIRED, message)
    return candidate


def deployment_candidate(
    inventory: PlatformInventory,
    component_id: ResourceId,
    release: ReleaseId,
    artifacts: Path,
    schemas: Path,
) -> ReleaseCandidate | None:
    """Return what a deploy of ``release`` would need approved now.

    ``None`` when the component's application deploys ``automatic``: then
    nothing needs approving and the artifact is left to the deploy itself.
    Raises ``LifecycleError`` for an undeclared component or an artifact
    that does not verify.
    """
    component = find_component(inventory, component_id)
    if application_approval(inventory, component_id) is DeploymentApproval.AUTOMATIC:
        return None
    artifact = verify_artifact(artifacts, component_id, release, schemas)
    return ReleaseCandidate(
        application_id=component.application_id,
        component_id=component_id,
        release=release,
        artifact_sha256=Sha256Digest(artifact.sha256),
        git_commit=GitCommit(artifact.git_commit),
        server_ids=component.server_ids,
    )


def application_approval(
    inventory: PlatformInventory, component_id: ResourceId
) -> DeploymentApproval:
    """Return the ``approval`` the component's application declares.

    Raises ``LifecycleError`` for an undeclared component.
    """
    component = find_component(inventory, component_id)
    return next(
        application.approval
        for application in inventory.applications
        if application.resource_id == component.application_id
    )


@dataclass(frozen=True, slots=True)
class ReleaseApproval:
    """One person's approval of one release, valid until it expires."""

    approval_id: ResourceId
    candidate: ReleaseCandidate
    approver: Approver
    approved_at: datetime
    expires_at: datetime
    via: ApprovalChannel

    def as_document(self) -> dict[str, object]:
        """Serialize the record as ``release-approval.schema.json`` defines it."""
        return {
            "apiVersion": _API_VERSION,
            "kind": _KIND,
            "metadata": {"id": self.approval_id.value},
            "spec": _spec(
                self.candidate,
                self.approver,
                self.approved_at,
                self.expires_at,
                self.via,
            ),
        }

    def path(self, directory: Path) -> Path:
        """Return where the record is kept under ``directory``."""
        return directory / f"{self.approval_id.value}.json"


def approve_release(
    candidate: ReleaseCandidate,
    approver: Approver,
    via: ApprovalChannel,
    valid_for: ValidityHours,
    approved_at: datetime,
) -> ReleaseApproval:
    """Return the approval a person gave at ``approved_at``, an aware time."""
    if via is ApprovalChannel.UNKNOWN:
        message = "an approval must say how it arrived"
        raise ReleaseApprovalError(ERROR_CHANNEL_UNKNOWN, message)
    if approved_at.utcoffset() is None:
        message = f"the approval time has no time zone: {approved_at!r}"
        raise ValueError(message)
    # The record keeps whole seconds, so its two times differ by exactly the
    # validity.
    moment = approved_at.astimezone(UTC).replace(microsecond=0)
    expires_at = moment + valid_for.duration
    spec = _spec(candidate, approver, moment, expires_at, via)
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    stamp = moment.strftime("%Y%m%dt%H%M%S")
    return ReleaseApproval(
        approval_id=ResourceId(
            f"{_ID_PREFIX}-{stamp}-{fingerprint[:_FINGERPRINT_LENGTH]}"
        ),
        candidate=candidate,
        approver=approver,
        approved_at=moment,
        expires_at=expires_at,
        via=via,
    )


def write_approval(
    approval: ReleaseApproval, directory: Path, catalog: SchemaCatalog
) -> Path:
    """Validate the record and write it as a new file; never overwrite one."""
    document = approval.as_document()
    catalog.validate_named(RELEASE_APPROVAL_SCHEMA, document)
    directory.mkdir(parents=True, exist_ok=True)
    path = approval.path(directory)
    try:
        handle = path.open("x", encoding="utf-8")
    except FileExistsError as error:
        message = f"an identical approval is already recorded at {path}"
        raise ReleaseApprovalError(ERROR_APPROVAL_EXISTS, message) from error
    with handle:
        handle.write(json.dumps(document, indent=2) + "\n")
    return path


def _spec(
    candidate: ReleaseCandidate,
    approver: Approver,
    approved_at: datetime,
    expires_at: datetime,
    via: ApprovalChannel,
) -> dict[str, object]:
    return {
        "application": candidate.application_id.value,
        "component": candidate.component_id.value,
        "release": candidate.release.value,
        "artifactSha256": candidate.artifact_sha256.value,
        "gitCommit": candidate.git_commit.value,
        "servers": [server.value for server in candidate.server_ids],
        "approver": approver.value,
        "approvedAt": approved_at.astimezone(UTC).strftime(_TIME_FORMAT),
        "expiresAt": expires_at.astimezone(UTC).strftime(_TIME_FORMAT),
        "via": via.value,
    }


def _time(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_TIME_FORMAT)


# Which approval counts: deploy reads the records back (S-008).

AUTOMATIC_PLAN: Mapping[str, object] = {
    "policy": DeploymentApproval.AUTOMATIC.value
}
"""The plan's ``approval`` for an ``automatic`` application: nothing to approve."""


class NotCountingReason(StrEnum):
    """Why a record for the release being deployed does not count."""

    DIGEST_MISMATCH = "digest_mismatch"
    """The archive being deployed is not the one approved."""
    SERVERS_CHANGED = "servers_changed"
    """The component is declared on other servers than the ones approved."""
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class StoredApproval:
    """One approval record as read back from the approvals folder."""

    path: Path
    application_id: ResourceId
    component_id: ResourceId
    release: ReleaseId
    artifact_sha256: Sha256Digest
    server_ids: frozenset[ResourceId]
    approver: Approver
    approved_at: datetime
    expires_at: datetime

    def names(self, candidate: ReleaseCandidate) -> bool:
        """Whether the record names the candidate's application, component, release."""
        return (
            self.application_id == candidate.application_id
            and self.component_id == candidate.component_id
            and self.release == candidate.release
        )

    def reason_against(
        self, candidate: ReleaseCandidate, now: datetime
    ) -> NotCountingReason | None:
        """Return why the record does not count for the candidate, if it doesn't."""
        if self.artifact_sha256 != candidate.artifact_sha256:
            return NotCountingReason.DIGEST_MISMATCH
        if self.server_ids != frozenset(candidate.server_ids):
            return NotCountingReason.SERVERS_CHANGED
        if self.expires_at <= now:
            return NotCountingReason.EXPIRED
        return None


@dataclass(frozen=True, slots=True)
class NotCounting:
    """A record for the release that does not count, and why."""

    approval: StoredApproval
    reason: NotCountingReason

    def as_dict(self) -> dict[str, object]:
        """Serialize the record's path and the reason."""
        return {"path": str(self.approval.path), "reason": self.reason.value}


@dataclass(frozen=True, slots=True)
class ApprovalCheck:
    """What the approvals folder says about one release of a ``manual`` application."""

    candidate: ReleaseCandidate
    counting: StoredApproval | None
    """The latest approval that counts, if any."""
    not_counting: tuple[NotCounting, ...]

    @property
    def command(self) -> str:
        """The command a person runs to approve the release."""
        return (
            f"cloudfall release approve {self.candidate.component_id} "
            f"--release {self.candidate.release.value}"
        )

    def plan(self) -> dict[str, object]:
        """Serialize the plan's ``approval``."""
        result: dict[str, object] = {
            "policy": DeploymentApproval.MANUAL.value,
            "status": "missing" if self.counting is None else "approved",
        }
        if self.counting is not None:
            result["path"] = str(self.counting.path)
            result["approver"] = self.counting.approver.value
            result["expiresAt"] = _time(self.counting.expires_at)
        result["notCounting"] = [entry.as_dict() for entry in self.not_counting]
        if self.counting is None:
            result["command"] = self.command
        return result

    def used(self) -> dict[str, object]:
        """Serialize the deploy result's ``approval``: the record it went on."""
        if self.counting is None:
            message = "no approval counts, so the deploy used none"
            raise TypeError(message)
        return {
            "policy": DeploymentApproval.MANUAL.value,
            "path": str(self.counting.path),
            "approver": self.counting.approver.value,
            "approvedAt": _time(self.counting.approved_at),
        }

    def refusal(self) -> LifecycleError:
        """Return the ``deployment_approval_missing`` error naming the way out."""
        candidate = self.candidate
        detail = (
            f"application {candidate.application_id} deploys only approved "
            f"releases (approval: manual), and no approval counts for release "
            f"{candidate.release.value} of component {candidate.component_id}; "
            f"a person approves it at a terminal with `{self.command}`"
        )
        if self.not_counting:
            detail += "; not counting: " + ", ".join(
                f"{entry.approval.path} ({entry.reason.value})"
                for entry in self.not_counting
            )
        return LifecycleError(
            ERROR_DEPLOYMENT_APPROVAL_MISSING,
            detail,
            {
                "application": candidate.application_id.value,
                "component": candidate.component_id.value,
                "release": candidate.release.value,
                "command": self.command,
                "notCounting": [entry.as_dict() for entry in self.not_counting],
            },
        )


def check_approval(
    candidate: ReleaseCandidate,
    directory: Path,
    catalog: SchemaCatalog,
    now: datetime,
) -> ApprovalCheck:
    """Find the approval that counts for ``candidate`` at ``now``, an aware time.

    Every record in ``directory`` is read and validated first, so a broken
    one raises ``ReleaseApprovalError`` (``release_approval_record_invalid``)
    naming the file. Records about another application, component or release
    are left out; the others either count or are listed with their reason.
    """
    if now.utcoffset() is None:
        message = f"the check time has no time zone: {now!r}"
        raise ValueError(message)
    counting: list[StoredApproval] = []
    not_counting: list[NotCounting] = []
    for stored in load_approvals(directory, catalog):
        if not stored.names(candidate):
            continue
        reason = stored.reason_against(candidate, now)
        if reason is None:
            counting.append(stored)
        else:
            not_counting.append(NotCounting(stored, reason))
    latest = max(counting, key=lambda stored: stored.approved_at, default=None)
    return ApprovalCheck(candidate, latest, tuple(not_counting))


def deployment_approval(  # noqa: PLR0913 - one deploy's whole approval question.
    inventory: PlatformInventory,
    component_id: ResourceId,
    release: ReleaseId,
    artifacts: Path,
    approvals: Path,
    schemas: Path,
) -> ApprovalCheck | None:
    """Check the release's approval now; ``None`` for an ``automatic`` application.

    An ``automatic`` application's approvals folder is never read. Raises
    ``LifecycleError`` for an undeclared component or an artifact that does
    not verify, and ``ReleaseApprovalError`` for a broken approval record.
    """
    candidate = deployment_candidate(
        inventory, component_id, release, artifacts, schemas
    )
    if candidate is None:
        return None
    return check_approval(
        candidate, approvals, SchemaCatalog(schemas), datetime.now(UTC)
    )


# rollback needs no approval (S-008-13).

ROLLBACK_NOT_REQUIRED_REASON = (
    "a rollback only activates a release still retained on the servers, which "
    "went live through a deploy; it is the recovery path the health gate takes "
    "on its own, so it does not wait for a person's approval"
)


def rollback_approval(
    inventory: PlatformInventory, component_id: ResourceId
) -> dict[str, object]:
    """Serialize a rollback plan's ``approval``: never required.

    Raises ``LifecycleError`` for an undeclared component.
    """
    policy = application_approval(inventory, component_id)
    if policy is DeploymentApproval.AUTOMATIC:
        return dict(AUTOMATIC_PLAN)
    return {
        "policy": policy.value,
        "required": False,
        "reason": ROLLBACK_NOT_REQUIRED_REASON,
    }


def load_approvals(
    directory: Path, catalog: SchemaCatalog
) -> tuple[StoredApproval, ...]:
    """Read every ``*.json`` record at the top of ``directory``, by file name."""
    if not directory.is_dir():
        return ()
    return tuple(
        _load_approval(path, catalog) for path in sorted(directory.glob("*.json"))
    )


def _load_approval(path: Path, catalog: SchemaCatalog) -> StoredApproval:
    try:
        document = cast("object", json.loads(path.read_text(encoding="utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        message = f"release approval record is not valid JSON: {path}: {error}"
        raise ReleaseApprovalError(ERROR_RECORD_INVALID, message) from error
    except IsADirectoryError as error:
        message = f"release approval record is a directory: {path}"
        raise ReleaseApprovalError(ERROR_RECORD_INVALID, message) from error
    if not isinstance(document, dict):
        message = f"release approval record is not a JSON object: {path}"
        raise ReleaseApprovalError(ERROR_RECORD_INVALID, message)
    record = cast("Mapping[str, object]", document)
    try:
        catalog.validate_named(RELEASE_APPROVAL_SCHEMA, record)
    except ValidationError as error:
        field = ".".join(str(part) for part in error.absolute_path) or "(root)"
        message = (
            f"release approval record does not match its schema: {path}: "
            f"{field}: {error.message}"
        )
        raise ReleaseApprovalError(ERROR_RECORD_INVALID, message) from error
    spec = cast("Mapping[str, object]", record["spec"])
    try:
        return StoredApproval(
            path=path,
            application_id=ResourceId.from_boundary(spec["application"]),
            component_id=ResourceId.from_boundary(spec["component"]),
            release=ReleaseId(cast("str", spec["release"])),
            artifact_sha256=Sha256Digest(cast("str", spec["artifactSha256"])),
            server_ids=frozenset(
                ResourceId.from_boundary(server)
                for server in cast("list[object]", spec["servers"])
            ),
            approver=Approver(cast("str", spec["approver"])),
            approved_at=_record_time(cast("str", spec["approvedAt"])),
            expires_at=_record_time(cast("str", spec["expiresAt"])),
        )
    except (ValueError, ReleaseApprovalError) as error:
        message = f"release approval record is invalid: {path}: {error}"
        raise ReleaseApprovalError(ERROR_RECORD_INVALID, message) from error


def _record_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() is None:
        message = f"time without a time zone: {value!r}"
        raise ValueError(message)
    return moment.astimezone(UTC)
