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

import re
from dataclasses import dataclass, replace
from enum import StrEnum
from ipaddress import ip_address
from typing import TYPE_CHECKING

from cloudfall.domain import (
    AbsolutePath,
    BlockDevicePath,
    ByteSize,
    Hostname,
    IpAddress,
    NetworkProtocol,
    PackageName,
    PackageVersion,
    ResourceId,
    ResourceKind,
    SecretKeyName,
    SystemdUnitName,
    TcpPort,
)
from cloudfall.elasticsearch import ElasticsearchAccount
from cloudfall.elasticsearch_cluster import (
    DATA_PATH,
    TRANSPORT_CA_PATH,
    TRANSPORT_CERTIFICATE_PATH,
    TRANSPORT_KEY_PATH,
    TRANSPORT_PORT,
    cluster_nodes,
    transport_address,
)
from cloudfall.inventory import SecretValueReference, StoragePool
from cloudfall.kibana import KibanaEncryptionKey
from cloudfall.mariadb import MariadbReplicationRole
from cloudfall.rabbitmq_access import CA_PATH as RABBITMQ_CA_PATH
from cloudfall.rabbitmq_access import CERTIFICATE_PATH as RABBITMQ_CERTIFICATE_PATH
from cloudfall.rabbitmq_access import KEY_PATH as RABBITMQ_KEY_PATH
from cloudfall.rabbitmq_cluster import (
    DISTRIBUTION_PORT as RABBITMQ_DISTRIBUTION_PORT,
)
from cloudfall.rabbitmq_cluster import (
    EPMD_PORT as RABBITMQ_EPMD_PORT,
)
from cloudfall.rabbitmq_cluster import (
    node_certificate_names as rabbitmq_node_certificate_names,
)

if TYPE_CHECKING:
    from cloudfall.inventory import (
        BaselineInventory,
        LoggingStackInventory,
        PlatformInventory,
        ServerInventory,
        ServiceInventory,
    )

GIBIBYTE = 1024**3
LOGGING_VOLUME_SIZE = ByteSize(10 * GIBIBYTE)
"""The starting size of each logging data volume; it grows, never shrinks."""

_REQUIREMENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_REQUIREMENT_NAME_MAX_LENGTH = 127
"""Room for a fixed prefix in front of the longest resource id (63)."""


@dataclass(frozen=True, slots=True)
class RequirementName:
    """The name of a requirement, unique per type on one server.

    It is often derived from a resource id with a prefix (the SMTP password
    of receiver ``ops`` is ``smtp-password-ops``), so it allows more than the
    63 characters of a resource id.
    """

    value: str

    def __post_init__(self) -> None:
        """Enforce the resource-id syntax with the longer bound."""
        if not _REQUIREMENT_NAME_PATTERN.fullmatch(self.value):
            message = f"invalid requirement name: {self.value!r}"
            raise ValueError(message)
        if len(self.value) > _REQUIREMENT_NAME_MAX_LENGTH:
            message = (
                f"requirement name exceeds {_REQUIREMENT_NAME_MAX_LENGTH} "
                f"characters: {self.value!r}"
            )
            raise ValueError(message)


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
    SERVER_AND_CLIENT = "server-and-client"
    """Both sides: a cluster node's transport, which accepts and opens connections."""


class RequirementState(StrEnum):
    """Where a requirement stands, as the provider that meets it observes it."""

    MET = "met"
    WILL_CREATE = "will-create"
    WILL_GROW = "will-grow"
    NEEDS_INPUT = "needs-input"
    UNCHECKED = "unchecked"
    """No provider observes this type yet."""


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
    """A filesystem mounted at ``path`` before ``units`` start (S-014).

    It lives in ``pool`` as ``lv_<name>``: linear on the pool's primary
    physical volume when expendable, ``raid1`` over primary and secondary
    when mirrored.
    """

    name: RequirementName
    path: AbsolutePath
    minimum_size: ByteSize
    placement: VolumePlacement
    units: tuple[SystemdUnitName, ...]
    pool: StoragePool

    requirement_type = RequirementType.VOLUME

    def __post_init__(self) -> None:
        """Refuse a mirrored volume in a pool with one physical volume."""
        if self.placement is VolumePlacement.MIRRORED and self.pool.secondary is None:
            message = (
                f"volume {self.name.value} is mirrored, but pool "
                f"{self.pool.resource_id.value} has no secondary physical volume"
            )
            raise ValueError(message)

    @property
    def logical_volume(self) -> str:
        """Return the LV name: ``lv_`` and the requirement's name."""
        return f"lv_{self.name.value.replace('-', '_')}"

    @property
    def volume_type(self) -> str:
        """Return the LVM segment type the placement maps to."""
        return "raid1" if self.placement is VolumePlacement.MIRRORED else "linear"

    @property
    def physical_volumes(self) -> tuple[BlockDevicePath, ...]:
        """Return the physical volumes every ``lvcreate`` and ``lvextend`` names."""
        if self.placement is VolumePlacement.MIRRORED and self.pool.secondary:
            return (self.pool.primary, self.pool.secondary)
        return (self.pool.primary,)

    def held_as(self, segment: object) -> VolumeRequirement:
        """Return what an existing volume of LVM type ``segment`` is measured against.

        A mirrored volume meets an expendable requirement (D-18): a ``raid1``
        volume in a pool with a secondary stays the mirror it is, and is
        checked, grown and audited as one. A linear volume still never meets
        a mirrored requirement.
        """
        if (
            segment == "raid1"
            and self.placement is VolumePlacement.EXPENDABLE
            and self.pool.secondary is not None
        ):
            return replace(self, placement=VolumePlacement.MIRRORED)
        return self

    def as_dict(self) -> dict[str, object]:
        """Serialize the volume requirement with where it lives."""
        return {
            "path": self.path.value,
            "minimumBytes": self.minimum_size.value,
            "placement": self.placement.value,
            "units": [unit.value for unit in self.units],
            "pool": self.pool.resource_id.value,
            "volumeGroup": self.pool.volume_group.value,
            "logicalVolume": self.logical_volume,
            "type": self.volume_type,
            "physicalVolumes": [item.value for item in self.physical_volumes],
        }


@dataclass(frozen=True, slots=True)
class PortRequirement:
    """An inbound port open to the named servers only."""

    name: RequirementName
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
    """A secret value, kept in the project's store, that lands on the host at ``path``.

    ``reference`` names where the store keeps it: one sops-encrypted dotenv
    file per owning resource, one key per secret.
    """

    name: RequirementName
    source: SecretSource
    path: AbsolutePath
    reference: SecretValueReference

    requirement_type = RequirementType.SECRET

    def as_dict(self) -> dict[str, object]:
        """Serialize the secret requirement; it never carries a value."""
        return {
            "source": self.source.value,
            "path": self.path.value,
            "reference": self.reference.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class CertificateRequirement:
    """A certificate and key chained to the project CA, with that CA beside it.

    A subject name is a DNS name or an IP address a client reaches it at.
    """

    name: RequirementName
    usage: CertificateUsage
    subject_names: tuple[Hostname | IpAddress, ...]
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

    name: RequirementName
    hostname: Hostname

    requirement_type = RequirementType.DNS_NAME

    def as_dict(self) -> dict[str, object]:
        """Serialize the DNS name requirement."""
        return {"hostname": self.hostname.value}


@dataclass(frozen=True, slots=True)
class PackageRequirement:
    """A package pinned to a version that must exist for the host's release."""

    name: RequirementName
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
        backend = servers[stack.backend.server_id.value]
        baseline = inventory.baseline(backend.baseline_id)
        for server_id, requirement in _logging_stack_requirements(
            stack, servers, baseline
        ):
            by_server.setdefault(server_id.value, []).append(requirement)
    for server_id, requirement in _alert_channel_requirements(inventory):
        held = by_server.setdefault(server_id.value, [])
        if requirement not in held:
            held.append(requirement)
    for service in inventory.services:
        for server_id, requirement in _elasticsearch_requirements(service, inventory):
            by_server.setdefault(server_id.value, []).append(requirement)
        for server_id, requirement in _mariadb_requirements(service, inventory):
            by_server.setdefault(server_id.value, []).append(requirement)
        for server_id, requirement in _rabbitmq_requirements(service, inventory):
            by_server.setdefault(server_id.value, []).append(requirement)
        for server_id, requirement in _kibana_requirements(service, inventory):
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


ELASTICSEARCH_PACKAGE = PackageName("elasticsearch")
ELASTICSEARCH_VOLUME_NAME = RequirementName("elasticsearch")
ELASTICSEARCH_VOLUME_SIZE = ByteSize(10 * GIBIBYTE)
"""The starting size of each node's data volume; it grows, never shrinks."""
ELASTICSEARCH_TRANSPORT_NAME = RequirementName("elasticsearch-transport")
ELASTICSEARCH_PASSWORD_NAME = RequirementName("elasticsearch-cloudfall-password")
ELASTICSEARCH_PASSWORD_PATH = AbsolutePath("/etc/elasticsearch/cloudfall-password")
"""Where each node keeps the ``cloudfall`` account's password, 0600 elasticsearch."""
ELASTIC_PASSWORD_NAME = "elasticsearch-elastic-password"  # noqa: S105 - a name
"""How ``secrets generate`` names the ``elastic`` password; no host keeps it."""
_ELASTICSEARCH_ACCOUNT_KEYS = {
    ElasticsearchAccount.CLOUDFALL: SecretKeyName("CLOUDFALL_PASSWORD"),
    ElasticsearchAccount.ELASTIC: SecretKeyName("ELASTIC_PASSWORD"),
}


def elasticsearch_account_reference(
    environment: ResourceId, service_id: ResourceId, account: ElasticsearchAccount
) -> SecretValueReference:
    """Return where the store keeps an account's password: ``/services/<id>``."""
    return SecretValueReference(
        environment=environment,
        path=AbsolutePath(f"/services/{service_id.value}"),
        key=_ELASTICSEARCH_ACCOUNT_KEYS[account],
    )


def _elasticsearch_requirements(
    service: ServiceInventory, inventory: PlatformInventory
) -> list[tuple[ResourceId, Requirement]]:
    """Place what each node of an Elasticsearch service needs (S-012).

    Every node needs its data volume, its package and Cloudfall's generated
    password; the role writes the password on the node and only its bcrypt
    hash into the file realm. A node of a cluster of two or more also needs
    the transport port, open to the other nodes only; one node keeps its
    transport on loopback, unless it joins an ``existingCluster``. A node of a
    cluster, or of a service with ``remoteAccess``, needs its one certificate
    from the project CA, which
    the transport and HTTP over TLS share: the other nodes verify it at its
    IPv4, a client at the listen address.
    """
    elasticsearch = service.elasticsearch
    if elasticsearch is None:
        return []
    owner = RequirementOwner(ResourceKind.SERVICE, service.resource_id)
    servers = {server.resource_id: server for server in inventory.servers}
    nodes = cluster_nodes(service, inventory.servers)
    # One Cloudfall node keeps the only copy it will hold once an existing
    # cluster it joined is drained, so it stays mirrored.
    placement = (
        VolumePlacement.EXPENDABLE
        if len(elasticsearch.nodes) > 1
        else VolumePlacement.MIRRORED
    )
    placed: list[tuple[ResourceId, Requirement]] = []
    for node_id in elasticsearch.nodes:
        baseline = inventory.baseline(servers[node_id].baseline_id)
        placed.append(
            (
                node_id,
                Requirement(
                    owner,
                    VolumeRequirement(
                        ELASTICSEARCH_VOLUME_NAME,
                        DATA_PATH,
                        ELASTICSEARCH_VOLUME_SIZE,
                        placement,
                        (SystemdUnitName("elasticsearch.service"),),
                        _pool(baseline, None, ELASTICSEARCH_VOLUME_NAME.value),
                    ),
                ),
            )
        )
    for node in nodes:
        others = tuple(
            item.server_id for item in nodes if item.server_id != node.server_id
        )
        if not others:
            # The only Cloudfall node of a joined cluster: its peers are the
            # existing cluster's nodes, which are no declared servers.
            continue
        placed.append(
            (
                node.server_id,
                Requirement(
                    owner,
                    PortRequirement(
                        ELASTICSEARCH_TRANSPORT_NAME,
                        TRANSPORT_PORT,
                        NetworkProtocol.TCP,
                        others,
                    ),
                ),
            )
        )
    remote_access = elasticsearch.remote_access
    if nodes or remote_access is not None:
        listen_address = None if remote_access is None else remote_access.listen_address
        placed.extend(
            (
                node_id,
                Requirement(
                    owner,
                    CertificateRequirement(
                        ELASTICSEARCH_TRANSPORT_NAME,
                        CertificateUsage.SERVER_AND_CLIENT,
                        elasticsearch_node_names(servers[node_id], listen_address),
                        TRANSPORT_CERTIFICATE_PATH,
                        TRANSPORT_KEY_PATH,
                        TRANSPORT_CA_PATH,
                    ),
                ),
            )
            for node_id in elasticsearch.nodes
        )
    password = SecretRequirement(
        ELASTICSEARCH_PASSWORD_NAME,
        SecretSource.GENERATE,
        ELASTICSEARCH_PASSWORD_PATH,
        elasticsearch_account_reference(
            service.environment, service.resource_id, ElasticsearchAccount.CLOUDFALL
        ),
    )
    package = PackageRequirement(
        RequirementName("elasticsearch"),
        ELASTICSEARCH_PACKAGE,
        PackageVersion(elasticsearch.version.value),
    )
    return placed + [
        (node_id, Requirement(owner, spec))
        for node_id in elasticsearch.nodes
        for spec in (password, package)
    ]


def elasticsearch_node_names(
    server: ServerInventory, listen_address: IpAddress | None = None
) -> tuple[Hostname | IpAddress, ...]:
    """Return the names of a node's one certificate: hostname, then every address.

    The addresses are the server's IPv4 (its ``network`` or an IP ``address``),
    an IP ``address`` of another family, and the ``remoteAccess`` listen
    address, each once, in that order (S-012, What each node needs).
    """
    addresses: list[str] = []
    ipv4 = transport_address(server)
    if ipv4 is not None:
        addresses.append(str(ipv4))
    try:
        address = ip_address(server.address.value)
    except ValueError:
        address = None  # a DNS name: the hostname names it already
    if address is not None:
        addresses.append(str(address))
    if listen_address is not None:
        addresses.append(listen_address.value)
    return (server.hostname, *(IpAddress(item) for item in dict.fromkeys(addresses)))


MARIADB_PACKAGE = PackageName("mariadb-server")
MARIADB_REQUIREMENT_NAME = RequirementName("mariadb")
MARIADB_VOLUME_PATH = AbsolutePath("/var/lib/mysql")
MARIADB_VOLUME_SIZE = ByteSize(10 * GIBIBYTE)
"""The starting size of the data volume; it grows, never shrinks."""
MARIADB_REPLICATION_NAME = RequirementName("mariadb-replication")
MARIADB_CERTIFICATE_PATH = AbsolutePath("/etc/mysql/cloudfall/server.crt")
MARIADB_KEY_PATH = AbsolutePath("/etc/mysql/cloudfall/server.key")
MARIADB_CA_PATH = AbsolutePath("/etc/mysql/cloudfall/ca.crt")
MARIADB_REPLICATION_PASSWORD_NAME = "mariadb-replication-password"  # noqa: S105 - a name
"""How ``secrets generate`` names a primary's ``cloudfall_repl`` password.

No host keeps it, so it is no requirement: the primary holds only its hash,
which the controller computes (D-9), and a replica the password in a 0600
file for the run, then where MariaDB keeps it (S-019, Replication).
"""
MARIADB_REPLICATION_PASSWORD_KEY = SecretKeyName("REPLICATION_PASSWORD")


def mariadb_replication_reference(
    environment: ResourceId, service_id: ResourceId
) -> SecretValueReference:
    """Return where the store keeps a primary's ``cloudfall_repl`` password."""
    return SecretValueReference(
        environment=environment,
        path=AbsolutePath(f"/services/{service_id.value}"),
        key=MARIADB_REPLICATION_PASSWORD_KEY,
    )


def _mariadb_requirements(
    service: ServiceInventory, inventory: PlatformInventory
) -> list[tuple[ResourceId, Requirement]]:
    """Place what the server of a MariaDB service needs (S-019).

    Every server needs its mirrored data volume and its package at the
    declared version. A primary needs its port open to the servers of its
    replicas only. A server with ``remoteAccess`` or ``replication`` needs
    one server certificate from the project CA, named after its hostname and
    addresses. The generated password of a primary's replication account is
    no requirement: no host keeps it (``MARIADB_REPLICATION_PASSWORD_NAME``).
    """
    mariadb = service.mariadb
    if mariadb is None:
        return []
    owner = RequirementOwner(ResourceKind.SERVICE, service.resource_id)
    servers = {server.resource_id: server for server in inventory.servers}
    server = servers[service.server_id]
    baseline = inventory.baseline(server.baseline_id)
    specs: list[RequirementSpec] = [
        VolumeRequirement(
            MARIADB_REQUIREMENT_NAME,
            MARIADB_VOLUME_PATH,
            MARIADB_VOLUME_SIZE,
            VolumePlacement.MIRRORED,
            (SystemdUnitName("mariadb.service"),),
            _pool(baseline, None, MARIADB_REQUIREMENT_NAME.value),
        ),
        PackageRequirement(
            MARIADB_REQUIREMENT_NAME,
            MARIADB_PACKAGE,
            PackageVersion(mariadb.version.value),
        ),
    ]
    replication = mariadb.replication
    primary = (
        replication is not None and replication.role is MariadbReplicationRole.PRIMARY
    )
    replicas = tuple(
        other.server_id
        for other in inventory.services
        if other.mariadb is not None
        and other.mariadb.replication is not None
        and other.mariadb.replication.primary == service.resource_id
    )
    if primary and replicas:
        specs.append(
            PortRequirement(
                MARIADB_REPLICATION_NAME,
                service.bind.port,
                NetworkProtocol.TCP,
                replicas,
            )
        )
    if mariadb.remote_access is not None or replication is not None:
        listen_addresses = tuple(
            address
            for address in (
                None
                if mariadb.remote_access is None
                else mariadb.remote_access.listen_address,
                None if replication is None else replication.listen_address,
            )
            if address is not None
        )
        specs.append(
            CertificateRequirement(
                MARIADB_REQUIREMENT_NAME,
                CertificateUsage.SERVER,
                mariadb_server_names(server, listen_addresses),
                MARIADB_CERTIFICATE_PATH,
                MARIADB_KEY_PATH,
                MARIADB_CA_PATH,
            )
        )
    return [(service.server_id, Requirement(owner, spec)) for spec in specs]


RABBITMQ_PACKAGE = PackageName("rabbitmq-server")
RABBITMQ_REQUIREMENT_NAME = RequirementName("rabbitmq")
RABBITMQ_VOLUME_PATH = AbsolutePath("/var/lib/rabbitmq")
RABBITMQ_VOLUME_SIZE = ByteSize(20 * GIBIBYTE)
"""The starting size of each node's data volume; it grows, never shrinks."""
RABBITMQ_COOKIE_NAME = RequirementName("rabbitmq-erlang-cookie")
RABBITMQ_COOKIE_PATH = AbsolutePath("/var/lib/rabbitmq/.erlang.cookie")
"""Where each node and its command-line tools read the shared Erlang cookie."""
RABBITMQ_COOKIE_KEY = SecretKeyName("ERLANG_COOKIE")
RABBITMQ_EPMD_NAME = RequirementName("rabbitmq-epmd")
RABBITMQ_DISTRIBUTION_NAME = RequirementName("rabbitmq-distribution")


def rabbitmq_cookie_reference(
    environment: ResourceId, service_id: ResourceId
) -> SecretValueReference:
    """Return where the store keeps a service's Erlang cookie: ``/services/<id>``."""
    return SecretValueReference(
        environment=environment,
        path=AbsolutePath(f"/services/{service_id.value}"),
        key=RABBITMQ_COOKIE_KEY,
    )


def _rabbitmq_requirements(
    service: ServiceInventory, inventory: PlatformInventory
) -> list[tuple[ResourceId, Requirement]]:
    """Place what each node of a RabbitMQ service needs (S-023).

    Every node needs its data volume, mirrored when it is the only node and
    expendable with three (the copies are on the other nodes), the package at
    the declared version, and the Erlang cookie Cloudfall generates, which
    the nodes and their command-line tools share. A node of three also needs
    node discovery and the inter-node port open to the other nodes only. A
    node of three, or one with ``clientAccess``, needs its one certificate
    from the project CA, usage server and client: the nodes verify each other
    with it in both directions, a node of three's naming its short hostname
    too, which a connecting node checks, and the clients of ``clientAccess``
    verify the node; the TLS client port is opened to ``allowedSources``, networks
    rather than servers, by the firewall alone. One node without
    ``clientAccess`` listens on loopback only and needs neither.
    """
    rabbitmq = service.rabbitmq
    if rabbitmq is None:
        return []
    owner = RequirementOwner(ResourceKind.SERVICE, service.resource_id)
    servers = {server.resource_id: server for server in inventory.servers}
    placement = (
        VolumePlacement.EXPENDABLE
        if len(rabbitmq.nodes) > 1
        else VolumePlacement.MIRRORED
    )
    cookie = SecretRequirement(
        RABBITMQ_COOKIE_NAME,
        SecretSource.GENERATE,
        RABBITMQ_COOKIE_PATH,
        rabbitmq_cookie_reference(service.environment, service.resource_id),
    )
    package = PackageRequirement(
        RABBITMQ_REQUIREMENT_NAME,
        RABBITMQ_PACKAGE,
        PackageVersion(rabbitmq.version.value),
    )
    placed: list[tuple[ResourceId, Requirement]] = []
    for node_id in rabbitmq.nodes:
        baseline = inventory.baseline(servers[node_id].baseline_id)
        volume = VolumeRequirement(
            RABBITMQ_REQUIREMENT_NAME,
            RABBITMQ_VOLUME_PATH,
            RABBITMQ_VOLUME_SIZE,
            placement,
            (SystemdUnitName("rabbitmq-server.service"),),
            _pool(baseline, None, RABBITMQ_REQUIREMENT_NAME.value),
        )
        placed.extend(
            (node_id, Requirement(owner, spec)) for spec in (volume, cookie, package)
        )
    if len(rabbitmq.nodes) > 1:
        for node_id in rabbitmq.nodes:
            others = tuple(item for item in rabbitmq.nodes if item != node_id)
            placed.extend(
                (
                    node_id,
                    Requirement(
                        owner, PortRequirement(name, port, NetworkProtocol.TCP, others)
                    ),
                )
                for name, port in (
                    (RABBITMQ_EPMD_NAME, RABBITMQ_EPMD_PORT),
                    (RABBITMQ_DISTRIBUTION_NAME, RABBITMQ_DISTRIBUTION_PORT),
                )
            )
    if len(rabbitmq.nodes) == 1 and rabbitmq.client_access is None:
        return placed
    # One certificate per node from the project CA serves the nodes of three
    # between them and the clients of clientAccess, which verify it at its IPv4;
    # a node of three's also names its short hostname, the host part of its
    # node name, which the connecting node checks
    placed.extend(
        (
            node_id,
            Requirement(
                owner,
                CertificateRequirement(
                    RABBITMQ_REQUIREMENT_NAME,
                    CertificateUsage.SERVER_AND_CLIENT,
                    rabbitmq_node_certificate_names(
                        elasticsearch_node_names(servers[node_id]), servers[node_id]
                    )
                    if len(rabbitmq.nodes) > 1
                    else elasticsearch_node_names(servers[node_id]),
                    RABBITMQ_CERTIFICATE_PATH,
                    RABBITMQ_KEY_PATH,
                    RABBITMQ_CA_PATH,
                ),
            ),
        )
        for node_id in rabbitmq.nodes
    )
    return placed


KIBANA_PACKAGE = PackageName("kibana")
KIBANA_REQUIREMENT_NAME = RequirementName("kibana")
KIBANA_KEYSTORE_PATH = AbsolutePath("/etc/kibana/kibana.keystore")
"""Where Kibana keeps its three encryption keys, beside its service token."""


def kibana_key_reference(
    environment: ResourceId, service_id: ResourceId, key: KibanaEncryptionKey
) -> SecretValueReference:
    """Return where the store keeps a Kibana service's key: ``/services/<id>``."""
    return SecretValueReference(
        environment=environment,
        path=AbsolutePath(f"/services/{service_id.value}"),
        key=SecretKeyName(key.store_key),
    )


def _kibana_requirements(
    service: ServiceInventory, inventory: PlatformInventory
) -> list[tuple[ResourceId, Requirement]]:
    """Place what the server of a Kibana service needs (S-024).

    The package at the version of the Elasticsearch service it reads, from
    that service's repository, and the three encryption keys Cloudfall
    generates, which the role puts into Kibana's keystore. No volume, port or
    certificate: Kibana keeps its state in the cluster and listens on loopback.
    """
    kibana = service.kibana
    if kibana is None:
        return []
    cluster = next(
        item for item in inventory.services if item.resource_id == kibana.elasticsearch
    )
    if cluster.elasticsearch is None:
        message = (
            f"Kibana service {service.resource_id.value} reads "
            f"{kibana.elasticsearch.value}, which is no Elasticsearch service"
        )
        raise ValueError(message)
    owner = RequirementOwner(ResourceKind.SERVICE, service.resource_id)
    specs: list[RequirementSpec] = [
        SecretRequirement(
            RequirementName(key.requirement_name),
            SecretSource.GENERATE,
            KIBANA_KEYSTORE_PATH,
            kibana_key_reference(service.environment, service.resource_id, key),
        )
        for key in KibanaEncryptionKey
    ]
    specs.append(
        PackageRequirement(
            KIBANA_REQUIREMENT_NAME,
            KIBANA_PACKAGE,
            PackageVersion(cluster.elasticsearch.version.value),
        )
    )
    return [(service.server_id, Requirement(owner, spec)) for spec in specs]


def mariadb_server_names(
    server: ServerInventory, listen_addresses: tuple[IpAddress, ...] = ()
) -> tuple[Hostname | IpAddress, ...]:
    """Return the names of a MariaDB server's certificate: hostname, then addresses.

    The addresses are the server's IPv4, an IP ``address`` of another family,
    and the ``remoteAccess`` and replication listen addresses, each once.
    """
    names = elasticsearch_node_names(server)
    known = {name.value for name in names}
    extra = tuple(
        address
        for address in dict.fromkeys(listen_addresses)
        if address.value not in known
    )
    return (*names, *extra)


TELEGRAM_TOKEN_NAME = RequirementName("telegram-bot-token")
TELEGRAM_TOKEN_KEY = SecretKeyName("TELEGRAM_BOT_TOKEN")


def alert_channel_token_path(channel: ResourceId) -> AbsolutePath:
    """Return where a host keeps a channel's bot token (S-015)."""
    return AbsolutePath(f"/etc/cloudfall/alerting/{channel.value}/bot-token")


def _alert_channel_requirements(
    inventory: PlatformInventory,
) -> list[tuple[ResourceId, Requirement]]:
    """Every server that sends to a channel needs its bot token (S-015).

    A server sends when its baseline names the channel, and the logging
    backend sends when one of its stack's receivers names it.
    """
    placed: list[tuple[ResourceId, Requirement]] = []
    environments = {
        server.resource_id.value: server.environment for server in inventory.servers
    }

    def place(server_id: ResourceId, channel: ResourceId) -> None:
        owner = RequirementOwner(ResourceKind.ALERT_CHANNEL, channel)
        spec = SecretRequirement(
            TELEGRAM_TOKEN_NAME,
            SecretSource.INPUT,
            alert_channel_token_path(channel),
            SecretValueReference(
                environment=environments[server_id.value],
                path=AbsolutePath(f"/alert-channels/{channel.value}"),
                key=TELEGRAM_TOKEN_KEY,
            ),
        )
        placed.append((server_id, Requirement(owner, spec)))

    for server in inventory.servers:
        channel = inventory.baseline(server.baseline_id).alert_channel
        if channel is not None:
            place(server.resource_id, channel)
    for stack in inventory.logging_stacks:
        if stack.alerting is None:
            continue
        for receiver in stack.alerting.receivers:
            if receiver.channel is not None:
                place(stack.backend.server_id, receiver.channel)
    return placed


def _logging_stack_requirements(
    stack: LoggingStackInventory,
    servers: dict[str, ServerInventory],
    baseline: BaselineInventory,
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
            RequirementName("loki"),
            stack.backend.storage.path,
            LOGGING_VOLUME_SIZE,
            VolumePlacement.EXPENDABLE,
            (SystemdUnitName("loki.service"),),
            _pool(baseline, stack.backend.storage.pool, "loki"),
        ),
    )
    place(
        backend,
        VolumeRequirement(
            RequirementName("prometheus"),
            stack.metrics.backend.storage_path,
            LOGGING_VOLUME_SIZE,
            VolumePlacement.EXPENDABLE,
            (SystemdUnitName("prometheus.service"),),
            _pool(baseline, stack.metrics.backend.storage_pool, "prometheus"),
        ),
    )
    remote_collectors = tuple(
        server_id for server_id in collectors if server_id != backend
    )
    if remote_collectors:
        place(
            backend,
            PortRequirement(
                RequirementName("logging-gateway"),
                stack.gateway.port,
                NetworkProtocol.TCP,
                remote_collectors,
            ),
        )
    place(
        backend,
        _secret(
            stack,
            RequirementName("grafana-admin-password"),
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
                _secret(
                    stack,
                    RequirementName(f"smtp-password-{receiver.resource_id.value}"),
                    SecretSource.INPUT,
                    email.auth_password_file,
                ),
            )
    tls = stack.gateway.tls
    place(
        backend,
        CertificateRequirement(
            RequirementName("logging-gateway"),
            CertificateUsage.SERVER,
            (stack.gateway.server_name,),
            tls.certificate_path,
            tls.key_path,
            tls.client_ca_path,
        ),
    )
    place(
        backend,
        DnsNameRequirement(
            RequirementName("logging-gateway"), stack.gateway.server_name
        ),
    )
    for package, version in _backend_packages(stack):
        place(
            backend,
            PackageRequirement(RequirementName(package), PackageName(package), version),
        )
    client_tls = stack.collectors.client_tls
    for server_id in collectors:
        place(
            server_id,
            CertificateRequirement(
                RequirementName("logging-collector"),
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
                RequirementName("alloy"),
                PackageName("alloy"),
                stack.software.alloy,
            ),
        )
    return placed


def _pool(
    baseline: BaselineInventory, pool_id: ResourceId | None, volume: str
) -> StoragePool:
    """Return the pool a volume goes into; validation refuses one that is missing."""
    pool = baseline.pool(pool_id)
    if pool is None:
        wanted = "its only pool" if pool_id is None else f"pool {pool_id.value}"
        message = (
            f"volume {volume} needs {wanted} on Baseline/{baseline.resource_id.value}"
        )
        raise ValueError(message)
    return pool


def _secret(
    stack: LoggingStackInventory,
    name: RequirementName,
    source: SecretSource,
    path: AbsolutePath,
) -> SecretRequirement:
    """Keep a stack's secrets in ``secrets/<environment>/logging-stacks/<id>.env``."""
    return SecretRequirement(
        name,
        source,
        path,
        SecretValueReference(
            environment=stack.environment,
            path=AbsolutePath(f"/logging-stacks/{stack.resource_id.value}"),
            key=SecretKeyName(name.value.upper().replace("-", "_")),
        ),
    )


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
