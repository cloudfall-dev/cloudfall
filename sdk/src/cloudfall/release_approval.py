"""A person's approval of one release of one component (S-008).

``cloudfall release approve`` writes one ``ReleaseApproval`` record once a
person typed the release id at a terminal. The record names the archive
digest, the git commit and the servers it approves, and when it expires; it
holds no secret. This module builds and writes the record; nothing here
deploys, and nothing here reads the records back.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import TYPE_CHECKING

from cloudfall.decision import ApprovalChannel
from cloudfall.domain import DeploymentApproval, ResourceId, Sha256Digest
from cloudfall.lifecycle import find_component, verify_artifact

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cloudfall.domain import ReleaseId
    from cloudfall.inventory import PlatformInventory
    from cloudfall.validation import SchemaCatalog

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
    component = find_component(inventory, component_id)
    applications = {
        application.resource_id: application
        for application in inventory.applications
    }
    application = applications[component.application_id]
    if application.approval is DeploymentApproval.AUTOMATIC:
        message = (
            f"application {application.resource_id} deploys automatic; an "
            f"approval of {component_id} would gate nothing"
        )
        raise ReleaseApprovalError(ERROR_APPROVAL_NOT_REQUIRED, message)
    artifact = verify_artifact(artifacts, component_id, release, schemas)
    return ReleaseCandidate(
        application_id=application.resource_id,
        component_id=component_id,
        release=release,
        artifact_sha256=Sha256Digest(artifact.sha256),
        git_commit=GitCommit(artifact.git_commit),
        server_ids=component.server_ids,
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
