"""What a workload needs from its host, derived from the validated inventory.

A workload kind yields **requirements** per server it lands on (S-011): the
volumes its data lives on, the ports its peers reach it through, the secrets
and certificates it reads, the DNS name it answers to, and the package pins
it installs. Providers satisfy them before the workload's role runs; this
module only derives them, so it reads the inventory and nothing else.

Every requirement names the path, port or name the resource declares, so a
project that already provides a file by hand and a project that lets
Cloudfall provide it describe the same requirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from cloudfall.domain import (
    AbsolutePath,
    ByteSize,
    Hostname,
    NetworkProtocol,
    PackageName,
    PackageVersion,
    ResourceId,
    ResourceKind,
    SystemdUnitName,
    TcpPort,
)

if TYPE_CHECKING:
    from cloudfall.inventory import (
        LoggingStackInventory,
        PlatformInventory,
        ServerInventory,
    )

GIBIBYTE = 1024**3
LOGGING_VOLUME_SIZE = ByteSize(10 * GIBIBYTE)
"""The starting size of each logging data volume; it grows, never shrinks."""


class RequirementType(StrEnum):
    """The kinds of host requirement a provider can satisfy."""

    VOLUME = "volume"
    PORT = "port"
    SECRET = "secret"  # noqa: S105 - a requirement type, not a value
    CERTIFICATE = "certificate"
    DNS_NAME = "dns-name"
    PACKAGE = "package"


class VolumePlacement(StrEnum):
    """How a volume survives the loss of one disk."""

    MIRRORED = "mirrored"
    """Kept on two disks: data that has no copy elsewhere."""
    EXPENDABLE = "expendable"
    """Kept on one disk: data that can be lost or rebuilt."""


class SecretSource(StrEnum):
    """Where a secret's value comes from."""

    GENERATE = "generate"
    """Cloudfall makes a random value."""
    INPUT = "input"
    """A person types the value once, at a terminal."""


class CertificateUsage(StrEnum):
    """Which side of a mutual-TLS connection a certificate serves."""

    SERVER = "server"
    CLIENT = "client"


@dataclass(frozen=True, slots=True)
class RequirementOwner:
    """The resource whose kind yields a requirement."""

    kind: ResourceKind
    resource_id: ResourceId

    def as_dict(self) -> dict[str, object]:
        """Serialize the owner as ``{kind, id}``."""
        return {"kind": self.kind.value, "id": self.resource_id.value}


@dataclass(frozen=True, slots=True)
class VolumeRequirement:
    """A filesystem mounted at ``path`` before ``units`` start."""

    name: ResourceId
    path: AbsolutePath
    minimum_size: ByteSize
    placement: VolumePlacement
    units: tuple[SystemdUnitName, ...]

    requirement_type = RequirementType.VOLUME

    def as_dict(self) -> dict[str, object]:
        """Serialize the volume requirement."""
        return {
            "path": self.path.value,
            "minimumBytes": self.minimum_size.value,
            "placement": self.placement.value,
            "units": [unit.value for unit in self.units],
        }


@dataclass(frozen=True, slots=True)
class PortRequirement:
    """An inbound port open to the named servers only."""

    name: ResourceId
    port: TcpPort
    protocol: NetworkProtocol
    sources: tuple[ResourceId, ...]

    requirement_type = RequirementType.PORT

    def __post_init__(self) -> None:
        """Reject a port open to nobody: such a requirement is not yielded."""
        if not self.sources:
            message = f"port requirement without sources: {self.name.value}"
            raise ValueError(message)

    def as_dict(self) -> dict[str, object]:
        """Serialize the port requirement."""
        return {
            "port": self.port.value,
            "protocol": self.protocol.value,
            "sources": [source.value for source in self.sources],
        }


@dataclass(frozen=True, slots=True)
class SecretRequirement:
    """A secret value that lands on the host as the file at ``path``."""

    name: ResourceId
    source: SecretSource
    path: AbsolutePath

    requirement_type = RequirementType.SECRET

    def as_dict(self) -> dict[str, object]:
        """Serialize the secret requirement; it never carries a value."""
        return {"source": self.source.value, "path": self.path.value}


@dataclass(frozen=True, slots=True)
class CertificateRequirement:
    """A certificate and key chained to the project CA, with that CA beside it."""

    name: ResourceId
    usage: CertificateUsage
    subject_names: tuple[Hostname, ...]
    certificate_path: AbsolutePath
    key_path: AbsolutePath
    ca_path: AbsolutePath

    requirement_type = RequirementType.CERTIFICATE

    def __post_init__(self) -> None:
        """Reject a certificate with no subject name."""
        if not self.subject_names:
            message = f"certificate requirement without names: {self.name.value}"
            raise ValueError(message)

    def as_dict(self) -> dict[str, object]:
        """Serialize the certificate requirement; it never carries key material."""
        return {
            "usage": self.usage.value,
            "subjectNames": [name.value for name in self.subject_names],
            "certificatePath": self.certificate_path.value,
            "keyPath": self.key_path.value,
            "caPath": self.ca_path.value,
        }


@dataclass(frozen=True, slots=True)
class DnsNameRequirement:
    """A DNS name that resolves to the server it is required on."""

    name: ResourceId
    hostname: Hostname

    requirement_type = RequirementType.DNS_NAME

    def as_dict(self) -> dict[str, object]:
        """Serialize the DNS name requirement."""
        return {"hostname": self.hostname.value}


@dataclass(frozen=True, slots=True)
class PackageRequirement:
    """A package pinned to a version that must exist for the host's release."""

    name: ResourceId
    package: PackageName
    version: PackageVersion

    requirement_type = RequirementType.PACKAGE

    def as_dict(self) -> dict[str, object]:
        """Serialize the package requirement."""
        return {"package": self.package.value, "version": self.version.value}


type RequirementSpec = (
    VolumeRequirement
    | PortRequirement
    | SecretRequirement
    | CertificateRequirement
    | DnsNameRequirement
    | PackageRequirement
)


@dataclass(frozen=True, slots=True)
class Requirement:
    """One requirement, the resource that yields it, and what it asks for."""

    owner: RequirementOwner
    spec: RequirementSpec

    def as_dict(self) -> dict[str, object]:
        """Serialize as ``{type, name, owner, spec}``."""
        return {
            "type": self.spec.requirement_type.value,
            "name": self.spec.name.value,
            "owner": self.owner.as_dict(),
            "spec": self.spec.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ServerRequirements:
    """Every requirement placed on one server, in the order providers meet them."""

    server_id: ResourceId
    requirements: tuple[Requirement, ...]

    def __post_init__(self) -> None:
        """Reject two requirements of one type and name on one server."""
        seen: set[tuple[RequirementType, str]] = set()
        for requirement in self.requirements:
            key = (requirement.spec.requirement_type, requirement.spec.name.value)
            if key in seen:
                message = (
                    f"duplicate {key[0].value} requirement {key[1]!r} "
                    f"on server {self.server_id.value}"
                )
                raise ValueError(message)
            seen.add(key)

    def as_dict(self) -> dict[str, object]:
        """Serialize as ``{server, requirements}``."""
        return {
            "server": self.server_id.value,
            "requirements": [item.as_dict() for item in self.requirements],
        }


_TYPE_ORDER = tuple(RequirementType)
"""Providers run in this order: storage and network before secrets and PKI."""


def requirements_of(inventory: PlatformInventory) -> tuple[ServerRequirements, ...]:
    """Return the requirements every declared workload places on its servers.

    Servers appear in their declared order, and only those that carry at
    least one requirement.
    """
    by_server: dict[str, list[Requirement]] = {}
    servers = {server.resource_id.value: server for server in inventory.servers}
    for stack in inventory.logging_stacks:
        for server_id, requirement in _logging_stack_requirements(stack, servers):
            by_server.setdefault(server_id.value, []).append(requirement)
    return tuple(
        ServerRequirements(
            server.resource_id,
            tuple(
                sorted(
                    by_server[server.resource_id.value],
                    key=lambda item: _TYPE_ORDER.index(item.spec.requirement_type),
                )
            ),
        )
        for server in inventory.servers
        if server.resource_id.value in by_server
    )


def _logging_stack_requirements(
    stack: LoggingStackInventory, servers: dict[str, ServerInventory]
) -> list[tuple[ResourceId, Requirement]]:
    owner = RequirementOwner(ResourceKind.LOGGING_STACK, stack.resource_id)
    backend = stack.backend.server_id
    collectors = stack.collectors.server_ids
    placed: list[tuple[ResourceId, Requirement]] = []

    def place(server_id: ResourceId, spec: RequirementSpec) -> None:
        placed.append((server_id, Requirement(owner, spec)))

    place(
        backend,
        VolumeRequirement(
            ResourceId("loki"),
            stack.backend.storage.path,
            LOGGING_VOLUME_SIZE,
            VolumePlacement.EXPENDABLE,
            (SystemdUnitName("loki.service"),),
        ),
    )
    place(
        backend,
        VolumeRequirement(
            ResourceId("prometheus"),
            stack.metrics.backend.storage_path,
            LOGGING_VOLUME_SIZE,
            VolumePlacement.EXPENDABLE,
            (SystemdUnitName("prometheus.service"),),
        ),
    )
    remote_collectors = tuple(
        server_id for server_id in collectors if server_id != backend
    )
    if remote_collectors:
        place(
            backend,
            PortRequirement(
                ResourceId("logging-gateway"),
                stack.gateway.port,
                NetworkProtocol.TCP,
                remote_collectors,
            ),
        )
    place(
        backend,
        SecretRequirement(
            ResourceId("grafana-admin-password"),
            SecretSource.GENERATE,
            stack.grafana.admin_password_path,
        ),
    )
    if stack.alerting is not None:
        for receiver in stack.alerting.receivers:
            email = receiver.email
            if email is None or email.auth_password_file is None:
                continue
            place(
                backend,
                SecretRequirement(
                    ResourceId(f"smtp-password-{receiver.resource_id.value}"),
                    SecretSource.INPUT,
                    email.auth_password_file,
                ),
            )
    tls = stack.gateway.tls
    place(
        backend,
        CertificateRequirement(
            ResourceId("logging-gateway"),
            CertificateUsage.SERVER,
            (stack.gateway.server_name,),
            tls.certificate_path,
            tls.key_path,
            tls.client_ca_path,
        ),
    )
    place(
        backend,
        DnsNameRequirement(ResourceId("logging-gateway"), stack.gateway.server_name),
    )
    for package, version in _backend_packages(stack):
        place(
            backend,
            PackageRequirement(ResourceId(package), PackageName(package), version),
        )
    client_tls = stack.collectors.client_tls
    for server_id in collectors:
        place(
            server_id,
            CertificateRequirement(
                ResourceId("logging-collector"),
                CertificateUsage.CLIENT,
                (servers[server_id.value].hostname,),
                client_tls.certificate_path,
                client_tls.key_path,
                client_tls.ca_path,
            ),
        )
        place(
            server_id,
            PackageRequirement(
                ResourceId("alloy"), PackageName("alloy"), stack.software.alloy
            ),
        )
    return placed


def _backend_packages(
    stack: LoggingStackInventory,
) -> list[tuple[str, PackageVersion]]:
    packages = [("loki", stack.software.loki), ("grafana", stack.software.grafana)]
    if stack.software.prometheus is not None:
        packages.append(("prometheus", stack.software.prometheus))
    if stack.alerting is not None:
        alertmanager = stack.alerting.alertmanager.package_version
        if alertmanager is not None:
            packages.append(("prometheus-alertmanager", alertmanager))
    return packages
