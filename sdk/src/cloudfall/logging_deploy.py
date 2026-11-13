"""Deploy the declared LoggingStack with what Cloudfall provides for it (S-011).

The plan resolves every requirement and its state, and changes nothing. The
deploy first meets what the controller can meet: missing generated secrets
are written to the store, and a secret only a person can supply stops the
run before any server is touched. Then every host that needs a certificate
makes its own key, if it has none, and a signing request
(``logging-requests.yml``); the project CA signs the requests whose
certificate is missing or due, so private keys never leave their hosts.
Finally each secret, each new certificate and its CA are staged in files in
a private temp dir and ``logging.yml`` copies them to the host paths the
stack declares. A secret file already on a host is kept; a certificate is
replaced only when it was just signed. The staged files are removed when the
run ends, whether it passed or failed.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from cloudfall.pki import CertificateState
from cloudfall.requirements import (
    CertificateRequirement,
    CertificateUsage,
    RequirementState,
    SecretRequirement,
    SecretSource,
    ServerRequirements,
    requirements_of,
)
from cloudfall.secret_store import (
    SecretStore,
    StoredSecret,
    needs_input,
    requirement_states,
)

if TYPE_CHECKING:
    import datetime
    from collections.abc import Iterator

    from cloudfall.domain import ResourceId
    from cloudfall.inventory import LoggingStackInventory, PlatformInventory
    from cloudfall.pki import ProjectCa

DELIVERED_FILES_VARIABLE = "cloudfall_logging_delivered_files"
"""``logging.yml``'s variable: server id, then the files its roles copy."""
REQUESTS_VARIABLE = "cloudfall_logging_certificate_requests"
"""The ``logging-requests.yml`` variable: server id, then the certificates it needs."""
REQUESTS_DIRECTORY_VARIABLE = "cloudfall_logging_request_directory"
"""Where ``logging-requests.yml`` writes each request on the controller."""
PLAYBOOK = "logging.yml"
REQUESTS_PLAYBOOK = "logging-requests.yml"
ERROR_STACK_UNDECLARED = "logging_stack_undeclared"
ERROR_REQUEST_MISSING = "logging_certificate_request_missing"
_STAGED_MODE = 0o600
_PRIVATE_MODE = "0600"
_PUBLIC_MODE = "0644"


class DeliveryRole(StrEnum):
    """The role that copies a staged file to its host."""

    BACKEND = "backend"
    COLLECTOR = "collector"


class LoggingDeployError(RuntimeError):
    """A deploy that cannot start, named by a stable code."""

    def __init__(self, code: str, message: str) -> None:
        """Capture the code beside the message."""
        super().__init__(message)
        self.code = code
        self.message = message


class InputMissingError(RuntimeError):
    """Secrets only a person can supply are missing; nothing ran."""

    def __init__(self, missing: tuple[dict[str, object], ...]) -> None:
        """Name each missing secret."""
        names = ", ".join(str(item["name"]) for item in missing)
        super().__init__(f"secrets only a person can supply are missing: {names}")
        self.missing = missing


@dataclass(frozen=True, slots=True)
class StagedFile:
    """One file a role copies to its host; ``content`` never leaves this process."""

    server: ResourceId
    role: DeliveryRole
    name: str
    path: str
    content: str = field(repr=False)
    mode: str
    force: bool
    """Replace a file already there: only a certificate signed in this run."""


@dataclass(frozen=True, slots=True)
class DeliveredFile:
    """One file staged for a server: where it lands and its hash, never the content."""

    server: ResourceId
    name: str
    path: str
    sha256: str
    replaces: bool

    def as_dict(self) -> dict[str, object]:
        """Serialize the delivery; ``replaces`` says a file there is overwritten."""
        return {
            "server": self.server.value,
            "name": self.name,
            "path": self.path,
            "sha256": self.sha256,
            "replaces": self.replaces,
        }


@dataclass(frozen=True, slots=True)
class HeldCertificate:
    """What one host reported for one certificate requirement."""

    request: str
    certificate: str | None


@dataclass(frozen=True, slots=True)
class CertificateOutcome:
    """The state a held certificate was in, and whether it was signed now."""

    server: ResourceId
    name: str
    state: CertificateState

    def as_dict(self) -> dict[str, object]:
        """Serialize the outcome."""
        return {
            "server": self.server.value,
            "name": self.name,
            "state": self.state.value,
        }


def declared_stack(inventory: PlatformInventory) -> LoggingStackInventory:
    """Return the one declared LoggingStack, or fail naming why there is none."""
    if not inventory.logging_stacks:
        message = "no LoggingStack is declared; declare one under logging-stacks/"
        raise LoggingDeployError(ERROR_STACK_UNDECLARED, message)
    return inventory.logging_stacks[0]


def stack_requirements(
    inventory: PlatformInventory, stack: LoggingStackInventory
) -> tuple[ServerRequirements, ...]:
    """Return only the requirements ``stack`` places on its servers."""
    return tuple(
        ServerRequirements(
            server.server_id,
            tuple(
                item
                for item in server.requirements
                if item.owner.resource_id == stack.resource_id
            ),
        )
        for server in requirements_of(inventory)
    )


def preview_logging_deploy(
    inventory: PlatformInventory, store: SecretStore
) -> dict[str, object]:
    """Return the plan: the stack, its servers, and each requirement's state."""
    stack = declared_stack(inventory)
    servers = stack_requirements(inventory, stack)
    answered = requirement_states(servers, store)
    to_generate = [
        spec.name.value
        for spec in _secrets(servers)
        if store.state(spec) is RequirementState.WILL_CREATE
    ]
    effect = (
        f"deploy LoggingStack {stack.resource_id.value} with {PLAYBOOK}: backend "
        f"{stack.backend.server_id.value}, collectors "
        f"{', '.join(server.value for server in stack.collectors.server_ids)}"
    )
    if to_generate:
        effect += f"; first generate {', '.join(to_generate)} into the secret store"
    effect += (
        "; copy each stored secret to its host path, keeping a file already "
        "there; make a key and a signing request on each host that needs a "
        "certificate, sign the missing and due ones with the project CA, and "
        "copy them with the CA"
    )
    return {
        "status": "plan",
        "action": "logging-deploy",
        "loggingStack": stack.resource_id.value,
        "servers": answered,
        "missing": needs_input(answered),
        "wouldRun": effect,
    }


def prepare_secrets(
    inventory: PlatformInventory, store: SecretStore
) -> tuple[tuple[StoredSecret, ...], tuple[ServerRequirements, ...]]:
    """Meet the secret requirements the controller can meet, before any server.

    Raises ``InputMissingError`` when a secret only a person can supply is not
    stored; nothing is generated then, so a refused run changes nothing.
    """
    stack = declared_stack(inventory)
    servers = stack_requirements(inventory, stack)
    missing = needs_input(requirement_states(servers, store))
    if missing:
        raise InputMissingError(tuple(missing))
    generated = tuple(
        store.generate(spec.reference)
        for spec in _secrets(servers)
        if spec.source is SecretSource.GENERATE and not store.holds(spec.reference)
    )
    return generated, servers


def secret_files(
    servers: tuple[ServerRequirements, ...], store: SecretStore
) -> list[StagedFile]:
    """Return every stored secret as a file the backend role copies, never replacing."""
    return [
        StagedFile(
            server.server_id,
            DeliveryRole.BACKEND,
            requirement.spec.name.value,
            requirement.spec.path.value,
            store.value(requirement.spec.reference),
            _PRIVATE_MODE,
            force=False,
        )
        for server in servers
        for requirement in server.requirements
        if isinstance(requirement.spec, SecretRequirement)
    ]


def certificate_requests(
    servers: tuple[ServerRequirements, ...],
) -> dict[str, list[dict[str, str]]]:
    """Return, by server, the certificates whose key and request a host makes."""
    requests: dict[str, list[dict[str, str]]] = {}
    for server, spec in _certificates(servers):
        requests.setdefault(server.value, []).append(
            {
                "name": spec.name.value,
                "keyPath": spec.key_path.value,
                "certificatePath": spec.certificate_path.value,
                "commonName": spec.subject_names[0].value.removesuffix("."),
            }
        )
    return requests


def read_requests(
    directory: Path, servers: tuple[ServerRequirements, ...]
) -> dict[tuple[str, str], HeldCertificate]:
    """Read what ``logging-requests.yml`` wrote: one JSON file per certificate."""
    held: dict[tuple[str, str], HeldCertificate] = {}
    for server, spec in _certificates(servers):
        source = directory / server.value / f"{spec.name.value}.json"
        if not source.is_file():
            message = (
                f"{server.value} wrote no signing request for {spec.name.value}; "
                f"expected {source}"
            )
            raise LoggingDeployError(ERROR_REQUEST_MISSING, message)
        content = json.loads(source.read_text(encoding="utf-8"))
        request = content.get("request")
        certificate = content.get("certificate")
        if not isinstance(request, str) or not (
            certificate is None or isinstance(certificate, str)
        ):
            message = f"{source} is not a signing request report"
            raise LoggingDeployError(ERROR_REQUEST_MISSING, message)
        held[(server.value, spec.name.value)] = HeldCertificate(request, certificate)
    return held


def certificate_files(
    servers: tuple[ServerRequirements, ...],
    held: dict[tuple[str, str], HeldCertificate],
    ca: ProjectCa,
    now: datetime.datetime,
) -> tuple[list[StagedFile], list[CertificateOutcome]]:
    """Sign the missing and due certificates; stage each with the CA beside it.

    A ``met`` certificate and a ``foreign`` one (another CA's, provided by
    hand) are left as they are, and so is the CA file next to them.
    """
    files: list[StagedFile] = []
    outcomes: list[CertificateOutcome] = []
    for server, spec in _certificates(servers):
        report = held[(server.value, spec.name.value)]
        state = ca.state(report.certificate, spec, now)
        outcomes.append(CertificateOutcome(server, spec.name.value, state))
        if state not in {CertificateState.WILL_CREATE, CertificateState.WILL_RENEW}:
            continue
        role = (
            DeliveryRole.BACKEND
            if spec.usage is CertificateUsage.SERVER
            else DeliveryRole.COLLECTOR
        )
        files.append(
            StagedFile(
                server,
                role,
                spec.name.value,
                spec.certificate_path.value,
                ca.sign(report.request, spec, now),
                _PUBLIC_MODE,
                force=True,
            )
        )
        files.append(
            StagedFile(
                server,
                role,
                f"{spec.name.value}-ca",
                spec.ca_path.value,
                ca.certificate_pem,
                _PUBLIC_MODE,
                force=True,
            )
        )
    return files, outcomes


@contextmanager
def staged_files(
    files: list[StagedFile],
) -> Iterator[tuple[dict[str, object], tuple[DeliveredFile, ...]]]:
    """Stage each file in a private temp dir; yield the extra vars and the hashes.

    Contents pass through memory and the temp dir only; the directory is
    removed when the block ends.
    """
    with tempfile.TemporaryDirectory(prefix="cloudfall-logging-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        delivered: dict[str, list[dict[str, object]]] = {}
        hashes: list[DeliveredFile] = []
        for index, item in enumerate(files):
            source = root / f"{index}-{item.server.value}-{item.name}"
            descriptor = os.open(
                source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _STAGED_MODE
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(item.content)
            delivered.setdefault(item.server.value, []).append(
                {
                    "role": item.role.value,
                    "path": item.path,
                    "source": str(source),
                    "mode": item.mode,
                    "force": item.force,
                }
            )
            hashes.append(
                DeliveredFile(
                    item.server,
                    item.name,
                    item.path,
                    hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
                    item.force,
                )
            )
        yield {DELIVERED_FILES_VARIABLE: delivered}, tuple(hashes)


def _certificates(
    servers: tuple[ServerRequirements, ...],
) -> list[tuple[ResourceId, CertificateRequirement]]:
    return [
        (server.server_id, requirement.spec)
        for server in servers
        for requirement in server.requirements
        if isinstance(requirement.spec, CertificateRequirement)
    ]


def _secrets(servers: tuple[ServerRequirements, ...]) -> list[SecretRequirement]:
    return [
        requirement.spec
        for server in servers
        for requirement in server.requirements
        if isinstance(requirement.spec, SecretRequirement)
    ]
