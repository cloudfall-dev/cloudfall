"""Typed, read-only inventory projections over validated config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from cloudfall.domain import (
    ARTIFACT_CLONE_TIMEOUT_SECONDS,
    AbsolutePath,
    AlertDuration,
    AlertSeverity,
    AlertSummary,
    BuildCommand,
    BuildTimeoutSeconds,
    BuildToolName,
    BuildToolVersion,
    ByteSize,
    CommandArgv,
    ConfigCapture,
    ConnectionAddress,
    DatacenterCode,
    DayTime,
    DeploymentApproval,
    DnsMode,
    DomainRoutePath,
    EmailAddress,
    FileMode,
    FilesystemName,
    FirewallPolicy,
    HealthCheckType,
    Hostname,
    HttpScheme,
    HttpStatusCode,
    IpAddress,
    IpNetworkCidr,
    Ipv4Address,
    Ipv6NetworkCidr,
    LinuxUser,
    LoggingMigrationMode,
    LoggingStorageType,
    NetworkProtocol,
    OpenSshPublicKey,
    OperatingSystemDistribution,
    OperatingSystemMajorVersion,
    PackageName,
    PackageVersion,
    PositiveCount,
    PostgresDatabaseName,
    PostgresMajorVersion,
    PostgresRoleName,
    PromqlExpression,
    ProviderServerId,
    RaidLevel,
    RelativeDirectory,
    RelativeRepositoryPath,
    ReleaseCommandTimeout,
    ReleaseRelativePath,
    RepositoryUrl,
    RequiredServiceState,
    RequiredServiceStatus,
    ResourceDocument,
    ResourceId,
    ResourceKind,
    RuntimePackageManager,
    RuntimeType,
    RuntimeVersion,
    SecretKeyName,
    SecretScope,
    ServerLifecycle,
    ServiceCommand,
    ServiceKind,
    ServiceManager,
    ServiceName,
    Sha256Digest,
    SmtpSmarthost,
    SmtpUsername,
    SshPublicKeyLifecycle,
    SystemdCalendar,
    SystemdUnitName,
    TcpPort,
    TlsMode,
    WebhookUrl,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloudfall.validation import ValidatedConfig


@dataclass(frozen=True, slots=True)
class ServerLabel:
    """Typed server label entry."""

    key: ResourceId
    value: str

    def __post_init__(self) -> None:
        """Reject empty label values."""
        if not self.value:
            message = "server label value must not be empty"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class ServerProvider:
    """Infrastructure-provider identity for a server."""

    name: ResourceId
    server_id: ProviderServerId | None
    datacenter: DatacenterCode | None

    def as_dict(self) -> dict[str, object]:
        """Serialize provider identity."""
        result: dict[str, object] = {"name": self.name.value}
        if self.server_id is not None:
            result["serverId"] = self.server_id.value
        if self.datacenter is not None:
            result["datacenter"] = self.datacenter.value
        return result


@dataclass(frozen=True, slots=True)
class ServerNetwork:
    """Public network allocation and reverse DNS for a server."""

    ipv4: Ipv4Address
    ipv6_cidr: Ipv6NetworkCidr
    reverse_dns: Hostname

    def as_dict(self) -> dict[str, object]:
        """Serialize public network details."""
        return {
            "ipv4": self.ipv4.value,
            "ipv6Cidr": self.ipv6_cidr.value,
            "reverseDns": self.reverse_dns.value,
        }


@dataclass(frozen=True, slots=True)
class ServerInventory:
    """Connection and placement data for one managed server."""

    resource_id: ResourceId
    hostname: Hostname
    address: ConnectionAddress
    environment: ResourceId
    server_type_id: ResourceId
    lifecycle: ServerLifecycle
    ssh_user: LinuxUser
    ssh_port: TcpPort
    labels: tuple[ServerLabel, ...]
    provider: ServerProvider | None
    network: ServerNetwork | None

    def as_dict(self) -> dict[str, object]:
        """Serialize the server without exposing secrets."""
        result: dict[str, object] = {
            "id": self.resource_id.value,
            "hostname": self.hostname.value,
            "address": self.address.value,
            "environment": self.environment.value,
            "serverType": self.server_type_id.value,
            "lifecycle": self.lifecycle.value,
            "ssh": {
                "user": self.ssh_user.value,
                "port": self.ssh_port.value,
            },
            "labels": {label.key.value: label.value for label in self.labels},
        }
        if self.provider is not None:
            result["provider"] = self.provider.as_dict()
        if self.network is not None:
            result["network"] = self.network.as_dict()
        return result


@dataclass(frozen=True, slots=True)
class OperatingSystemRequirement:
    """Desired operating-system contract for a server type."""

    distribution: OperatingSystemDistribution
    versions: tuple[OperatingSystemMajorVersion, ...]
    service_manager: ServiceManager


@dataclass(frozen=True, slots=True)
class RaidRequirement:
    """Desired Linux software RAID contract."""

    level: RaidLevel
    minimum_active_devices: PositiveCount
    minimum_usable_bytes: ByteSize


@dataclass(frozen=True, slots=True)
class MountRequirement:
    """Desired mounted filesystem contract."""

    path: AbsolutePath
    filesystem: FilesystemName
    minimum_bytes: ByteSize


@dataclass(frozen=True, slots=True)
class PackageRequirement:
    """Desired installed package and optional exact version."""

    name: PackageName
    version: PackageVersion | None


@dataclass(frozen=True, slots=True)
class ServiceRequirement:
    """Desired systemd service- or timer-unit state."""

    name: SystemdUnitName
    state: RequiredServiceState
    status: RequiredServiceStatus


@dataclass(frozen=True, slots=True)
class FirewallRule:
    """One allowed inbound transport-layer endpoint."""

    port: TcpPort
    protocol: NetworkProtocol
    description: str | None

    def as_dict(self) -> dict[str, object]:
        """Serialize the rule for engine and audit consumers."""
        result: dict[str, object] = {
            "port": self.port.value,
            "protocol": self.protocol.value,
        }
        if self.description is not None:
            result["description"] = self.description
        return result


@dataclass(frozen=True, slots=True)
class FirewallRequirement:
    """Desired default-deny inbound firewall contract."""

    policy: FirewallPolicy
    allowed_inbound: tuple[FirewallRule, ...]

    def __post_init__(self) -> None:
        """Reject duplicate port/protocol rules."""
        seen: set[tuple[int, NetworkProtocol]] = set()
        for rule in self.allowed_inbound:
            identity = (rule.port.value, rule.protocol)
            if identity in seen:
                message = (
                    "duplicate firewall rule: "
                    f"{rule.port.value}/{rule.protocol.value}"
                )
                raise ValueError(message)
            seen.add(identity)

    def as_dict(self) -> dict[str, object]:
        """Serialize the desired firewall contract."""
        return {
            "policy": self.policy.value,
            "allowedInbound": [rule.as_dict() for rule in self.allowed_inbound],
        }


@dataclass(frozen=True, slots=True)
class ConfigurationFileRequirement:
    """Allowlisted non-secret configuration evidence contract."""

    path: AbsolutePath
    capture: ConfigCapture
    owner: LinuxUser | None
    group: LinuxUser | None
    mode: FileMode | None
    sha256: Sha256Digest | None

    def as_dict(self) -> dict[str, object]:
        """Serialize the safe configuration collection contract."""
        result: dict[str, object] = {
            "path": self.path.value,
            "capture": self.capture.value,
        }
        if self.owner is not None:
            result["owner"] = self.owner.value
        if self.group is not None:
            result["group"] = self.group.value
        if self.mode is not None:
            result["mode"] = self.mode.value
        if self.sha256 is not None:
            result["sha256"] = self.sha256.value
        return result


@dataclass(frozen=True, slots=True)
class ServerTypeInventory:
    """Reusable desired host contract."""

    resource_id: ResourceId
    os: OperatingSystemRequirement
    raid: RaidRequirement | None
    mounts: tuple[MountRequirement, ...]
    required_packages: tuple[PackageRequirement, ...]
    forbidden_packages: tuple[PackageName, ...]
    required_services: tuple[ServiceRequirement, ...]
    firewall: FirewallRequirement | None
    configuration_files: tuple[ConfigurationFileRequirement, ...]

    def as_dict(self) -> dict[str, object]:
        """Serialize the server_type for inspection and audit consumers."""
        required_packages: list[dict[str, object]] = []
        for package in self.required_packages:
            item: dict[str, object] = {"name": package.name.value}
            if package.version is not None:
                item["version"] = package.version.value
            required_packages.append(item)
        storage: dict[str, object] = {
            "mounts": [
                {
                    "path": mount.path.value,
                    "filesystem": mount.filesystem.value,
                    "minimumBytes": mount.minimum_bytes.value,
                }
                for mount in self.mounts
            ]
        }
        if self.raid is not None:
            storage["softwareRaid"] = {
                "level": self.raid.level.value,
                "minimumActiveDevices": self.raid.minimum_active_devices.value,
                "minimumUsableBytes": self.raid.minimum_usable_bytes.value,
            }
        result: dict[str, object] = {
            "id": self.resource_id.value,
            "os": {
                "distribution": self.os.distribution.value,
                "versions": [version.value for version in self.os.versions],
                "serviceManager": self.os.service_manager.value,
            },
            "storage": storage,
            "packages": {
                "required": required_packages,
                "forbidden": [package.value for package in self.forbidden_packages],
            },
            "services": {
                "required": [
                    {
                        "name": service.name.value,
                        "state": service.state.value,
                        "status": service.status.value,
                    }
                    for service in self.required_services
                ]
            },
            "configuration": {
                "files": [item.as_dict() for item in self.configuration_files]
            },
        }
        if self.firewall is not None:
            result["firewall"] = self.firewall.as_dict()
        return result


@dataclass(frozen=True, slots=True)
class SecretReference:
    """Typed pointer to one secret source; never carries a value."""

    scope: SecretScope
    environment: ResourceId
    path: AbsolutePath

    def as_dict(self) -> dict[str, object]:
        """Serialize the reference itself (references are not secret)."""
        return {
            "scope": self.scope.value,
            "environment": self.environment.value,
            "path": self.path.value,
        }


@dataclass(frozen=True, slots=True)
class ApplicationInventory:
    """Composition and ownership data for one SaaS application."""

    resource_id: ResourceId
    linux_user: LinuxUser
    approval: DeploymentApproval
    component_ids: tuple[ResourceId, ...]
    secret_refs: tuple[SecretReference, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Serialize application composition for inventory consumers."""
        return {
            "id": self.resource_id.value,
            "linuxUser": self.linux_user.value,
            "approval": self.approval.value,
            "components": [component.value for component in self.component_ids],
        }


@dataclass(frozen=True, slots=True)
class ComponentRepository:
    """Source repository for one deployable component."""

    url: RepositoryUrl
    subdirectory: str | None

    def as_dict(self) -> dict[str, object]:
        """Serialize the repository reference."""
        result: dict[str, object] = {"url": self.url.value}
        if self.subdirectory is not None:
            result["subdirectory"] = self.subdirectory
        return result


@dataclass(frozen=True, slots=True)
class ComponentRuntime:
    """Declared runtime and package manager for a component."""

    runtime_type: RuntimeType
    version: RuntimeVersion
    package_manager: RuntimePackageManager

    def as_dict(self) -> dict[str, object]:
        """Serialize the runtime contract."""
        return {
            "type": self.runtime_type.value,
            "version": self.version.value,
            "packageManager": self.package_manager.value,
        }


@dataclass(frozen=True, slots=True)
class ComponentService:
    """Managed systemd service owned by a component."""

    manager: ServiceManager
    name: ResourceId
    command: ServiceCommand

    def as_dict(self) -> dict[str, object]:
        """Serialize the service contract."""
        return {
            "manager": self.manager.value,
            "name": self.name.value,
            "command": list(self.command.value),
        }


@dataclass(frozen=True, slots=True)
class ComponentRelease:
    """Command a deploy runs once against a new release before it goes live."""

    command: CommandArgv
    working_directory: RelativeDirectory | None
    timeout_seconds: ReleaseCommandTimeout

    def as_dict(self) -> dict[str, object]:
        """Serialize the release command contract."""
        result: dict[str, object] = {"command": list(self.command.value)}
        if self.working_directory is not None:
            result["workingDirectory"] = self.working_directory.value
        result["timeoutSeconds"] = self.timeout_seconds.value
        return result


@dataclass(frozen=True, slots=True)
class ComponentHttpHealthCheck:
    """HTTP endpoint contract for a component health gate."""

    scheme: HttpScheme
    port: TcpPort
    path: str
    expected_statuses: tuple[HttpStatusCode, ...]
    timeout_seconds: PositiveCount
    attempts: PositiveCount


@dataclass(frozen=True, slots=True)
class ComponentHealthCheck:
    """Health gate for a deployed component."""

    check_type: HealthCheckType
    http: ComponentHttpHealthCheck | None

    def __post_init__(self) -> None:
        """Require the HTTP contract exactly for HTTP health checks."""
        if (self.check_type is HealthCheckType.HTTP) != (self.http is not None):
            message = "HTTP health checks require an HTTP contract"
            raise ValueError(message)

    def as_dict(self) -> dict[str, object]:
        """Serialize the health contract."""
        result: dict[str, object] = {"type": self.check_type.value}
        if self.http is not None:
            result.update(
                {
                    "scheme": self.http.scheme.value,
                    "port": self.http.port.value,
                    "path": self.http.path,
                    "expectedStatuses": [
                        status.value for status in self.http.expected_statuses
                    ],
                    "timeoutSeconds": self.http.timeout_seconds.value,
                    "attempts": self.http.attempts.value,
                }
            )
        return result


@dataclass(frozen=True, slots=True)
class ComponentBuildTool:
    """One tool a build step runs and its minimum version."""

    name: BuildToolName
    minimum_version: BuildToolVersion


@dataclass(frozen=True, slots=True)
class ComponentBuildStep:
    """One build step: an argv list run in a directory of the packaged root."""

    working_directory: RelativeRepositoryPath
    command: BuildCommand
    timeout_seconds: BuildTimeoutSeconds

    def as_dict(self) -> dict[str, object]:
        """Serialize the step contract."""
        return {
            "workingDirectory": self.working_directory.value,
            "command": list(self.command.value),
            "timeoutSeconds": self.timeout_seconds.value,
        }


@dataclass(frozen=True, slots=True)
class ComponentBuild:
    """Controller-side build steps that run before a component is packaged."""

    tools: tuple[ComponentBuildTool, ...]
    steps: tuple[ComponentBuildStep, ...]
    include: tuple[RelativeRepositoryPath, ...] = ()

    def __post_init__(self) -> None:
        """Require steps, unique tools and includes, and declared executables."""
        if not self.steps:
            message = "component build requires at least one step"
            raise ValueError(message)
        tool_names = [tool.name for tool in self.tools]
        if len(set(tool_names)) != len(tool_names):
            message = "component build tools must be unique"
            raise ValueError(message)
        if len(set(self.include)) != len(self.include):
            message = "component artifact include paths must be unique"
            raise ValueError(message)
        declared = frozenset(tool_names)
        for index, step in enumerate(self.steps):
            if step.command.executable not in declared:
                message = (
                    f"build step {index} runs {step.command.executable.value!r}, "
                    "which spec.buildTools does not declare"
                )
                raise ValueError(message)

    def as_dict(self) -> dict[str, object]:
        """Serialize the build contract in the Component spec's shape."""
        result: dict[str, object] = {
            "buildTools": {
                tool.name.value: tool.minimum_version.value for tool in self.tools
            },
            "build": [step.as_dict() for step in self.steps],
        }
        if self.include:
            result["artifact"] = {"include": [path.value for path in self.include]}
        return result


@dataclass(frozen=True, slots=True)
class ComponentInventory:
    """Placement and deployment contract for one deployable component."""

    resource_id: ResourceId
    application_id: ResourceId
    server_ids: tuple[ResourceId, ...]
    install_root: AbsolutePath
    retain_until_cleanup: bool
    repository: ComponentRepository
    runtime: ComponentRuntime
    service: ComponentService
    health_check: ComponentHealthCheck
    secret_refs: tuple[SecretReference, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    build: ComponentBuild | None = None
    release: ComponentRelease | None = None

    @property
    def artifact_timeout_seconds(self) -> int:
        """How long ``artifact build`` may run: the clone plus every step's limit."""
        steps = () if self.build is None else self.build.steps
        return ARTIFACT_CLONE_TIMEOUT_SECONDS + sum(
            step.timeout_seconds.value for step in steps
        )

    @property
    def release_server_id(self) -> ResourceId:
        """The server a release command runs on: the first deployment server."""
        return self.server_ids[0]

    def as_dict(self) -> dict[str, object]:
        """Serialize the component for inventory consumers."""
        result: dict[str, object] = {
            "id": self.resource_id.value,
            "application": self.application_id.value,
            "servers": [server.value for server in self.server_ids],
            "installRoot": self.install_root.value,
            "retainUntilCleanup": self.retain_until_cleanup,
            "repository": self.repository.as_dict(),
            "runtime": self.runtime.as_dict(),
            "service": self.service.as_dict(),
            "healthCheck": self.health_check.as_dict(),
        }
        if self.environment:
            result["environment"] = dict(self.environment)
        if self.build is not None:
            result.update(self.build.as_dict())
        if self.release is not None:
            result["release"] = self.release.as_dict()
        return result


@dataclass(frozen=True, slots=True)
class DomainProxy:
    """Desired public proxy placement and configuration evidence path."""

    server_id: ResourceId
    configuration_path: AbsolutePath
    service_name: ServiceName
    upstream_address: IpAddress | None = None
    upstream_port: TcpPort | None = None


@dataclass(frozen=True, slots=True)
class DomainOrigin:
    """Pinned origin endpoint behind a public proxy."""

    server_id: ResourceId
    configuration_path: AbsolutePath
    service_name: ServiceName
    scheme: HttpScheme
    port: TcpPort
    server_name: Hostname


@dataclass(frozen=True, slots=True)
class DomainEdge:
    """Desired external DNS/edge provider behavior."""

    provider: ResourceId
    mode: DnsMode


@dataclass(frozen=True, slots=True)
class DomainHealthCheck:
    """End-to-end public and origin health contract."""

    scheme: HttpScheme
    path: str
    expected_statuses: tuple[HttpStatusCode, ...]
    timeout_seconds: PositiveCount


@dataclass(frozen=True, slots=True)
class DomainProxyRoute:
    """A path a Domain proxies to an upstream, keeping the full request path."""

    path: DomainRoutePath
    upstream_address: IpAddress
    upstream_port: TcpPort

    def as_dict(self) -> dict[str, object]:
        """Serialize the route in its config shape."""
        return {
            "path": self.path.value,
            "upstream": {
                "address": self.upstream_address.value,
                "port": self.upstream_port.value,
            },
        }


@dataclass(frozen=True, slots=True)
class DomainStaticRoute:
    """A path a Domain serves as files from a component's current release."""

    path: DomainRoutePath
    component_id: ResourceId
    directory: ReleaseRelativePath
    spa_fallback: ReleaseRelativePath | None = None

    def __post_init__(self) -> None:
        """Keep the alias-safe trailing slash and the release root out."""
        if not self.path.is_directory:
            message = f"static route path must end with '/': {self.path.value!r}"
            raise ValueError(message)
        if self.directory.is_release_root:
            message = "static route directory must not be the release root"
            raise ValueError(message)

    def as_dict(self) -> dict[str, object]:
        """Serialize the route in its config shape."""
        static: dict[str, object] = {
            "component": self.component_id.value,
            "directory": self.directory.value,
        }
        if self.spa_fallback is not None:
            static["spaFallback"] = self.spa_fallback.value
        return {"path": self.path.value, "static": static}


type DomainRoute = DomainProxyRoute | DomainStaticRoute


@dataclass(frozen=True, slots=True)
class DomainInventory:
    """Typed desired state for one public domain route."""

    resource_id: ResourceId
    primary_name: Hostname
    aliases: tuple[Hostname, ...]
    proxy: DomainProxy
    origin: DomainOrigin
    edge: DomainEdge
    tls_mode: TlsMode
    health_check: DomainHealthCheck
    routes: tuple[DomainRoute, ...] = ()

    def __post_init__(self) -> None:
        """Hold at most one route source: ``proxy.upstream`` or ``routes``."""
        if self.routes and self.proxy.upstream_address is not None:
            message = "a Domain declares proxy.upstream or routes, not both"
            raise ValueError(message)

    def as_dict(self) -> dict[str, object]:
        """Serialize public routing intent without secret material."""
        proxy: dict[str, object] = {
            "server": self.proxy.server_id.value,
            "configurationPath": self.proxy.configuration_path.value,
            "service": self.proxy.service_name.value,
        }
        if (
            self.proxy.upstream_address is not None
            and self.proxy.upstream_port is not None
        ):
            proxy["upstream"] = {
                "address": self.proxy.upstream_address.value,
                "port": self.proxy.upstream_port.value,
            }
        result: dict[str, object] = {
            "id": self.resource_id.value,
            "primaryName": self.primary_name.value,
            "aliases": [alias.value for alias in self.aliases],
            "proxy": proxy,
            "origin": {
                "server": self.origin.server_id.value,
                "configurationPath": self.origin.configuration_path.value,
                "service": self.origin.service_name.value,
                "scheme": self.origin.scheme.value,
                "port": self.origin.port.value,
                "serverName": self.origin.server_name.value,
            },
            "edge": {
                "provider": self.edge.provider.value,
                "mode": self.edge.mode.value,
            },
            "tls": {"mode": self.tls_mode.value},
            "healthCheck": {
                "scheme": self.health_check.scheme.value,
                "path": self.health_check.path,
                "expectedStatuses": [
                    status.value for status in self.health_check.expected_statuses
                ],
                "timeoutSeconds": self.health_check.timeout_seconds.value,
            },
        }
        if self.routes:
            result["routes"] = [route.as_dict() for route in self.routes]
        return result


@dataclass(frozen=True, slots=True)
class ServiceBind:
    """Loopback listener contract for an infrastructure service."""

    address: IpAddress
    port: TcpPort


@dataclass(frozen=True, slots=True)
class PostgresDatabase:
    """One application-owned PostgreSQL database."""

    name: PostgresDatabaseName
    application_id: ResourceId
    owner: LinuxUser

    def as_dict(self) -> dict[str, object]:
        """Serialize the database with its resolved owner role."""
        return {
            "name": self.name.value,
            "application": self.application_id.value,
            "owner": self.owner.value,
        }


@dataclass(frozen=True, slots=True)
class SecretValueReference:
    """Pointer to one key of one secret source; never carries the value."""

    environment: ResourceId
    path: AbsolutePath
    key: SecretKeyName

    def as_dict(self) -> dict[str, object]:
        """Serialize the reference itself (references are not secret)."""
        return {
            "environment": self.environment.value,
            "path": self.path.value,
            "key": self.key.value,
        }


@dataclass(frozen=True, slots=True)
class OwnedPostgresDatabase:
    """One PostgreSQL database owned by a declared role, not an application."""

    name: PostgresDatabaseName
    owner: PostgresRoleName
    password_ref: SecretValueReference

    def as_dict(self) -> dict[str, object]:
        """Serialize the database with its role and password reference."""
        return {
            "name": self.name.value,
            "owner": self.owner.value,
            "passwordRef": self.password_ref.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class PostgresRemoteAccess:
    """Listener and client networks for outside apps reaching owned roles."""

    listen_address: IpAddress
    allowed_sources: tuple[IpNetworkCidr, ...]

    def as_dict(self) -> dict[str, object]:
        """Serialize the remote access declaration."""
        return {
            "listenAddress": self.listen_address.value,
            "allowedSources": [source.value for source in self.allowed_sources],
        }


@dataclass(frozen=True, slots=True)
class PostgresqlService:
    """PostgreSQL-specific service contract."""

    major_version: PostgresMajorVersion
    package_version: PackageVersion | None
    databases: tuple[PostgresDatabase | OwnedPostgresDatabase, ...]
    remote_access: PostgresRemoteAccess | None = None


@dataclass(frozen=True, slots=True)
class RedisService:
    """Redis-specific service contract."""

    package_version: PackageVersion | None
    maxmemory_mb: PositiveCount
    append_only: bool


@dataclass(frozen=True, slots=True)
class ServiceBackup:
    """Scheduled dump-and-prune backup contract."""

    directory: AbsolutePath
    on_calendar: SystemdCalendar
    retention_days: PositiveCount
    restore_check_on_calendar: SystemdCalendar | None = None


@dataclass(frozen=True, slots=True)
class ServiceInventory:
    """Typed desired state for one infrastructure service."""

    resource_id: ResourceId
    service_kind: ServiceKind
    environment: ResourceId
    server_id: ResourceId
    bind: ServiceBind
    postgresql: PostgresqlService | None
    redis: RedisService | None
    backup: ServiceBackup
    metrics_enabled: bool

    def __post_init__(self) -> None:
        """Reject services whose payload contradicts the declared kind."""
        expected_postgresql = self.service_kind is ServiceKind.POSTGRESQL
        if (self.postgresql is not None) is not expected_postgresql or (
            self.redis is not None
        ) is not (self.service_kind is ServiceKind.REDIS):
            message = (
                "service payload does not match its declared kind: "
                f"{self.resource_id.value}"
            )
            raise ValueError(message)

    def as_dict(self) -> dict[str, object]:
        """Serialize the service without secret material."""
        backup: dict[str, object] = {
            "directory": self.backup.directory.value,
            "onCalendar": self.backup.on_calendar.value,
            "retentionDays": self.backup.retention_days.value,
        }
        if self.backup.restore_check_on_calendar is not None:
            backup["restoreCheckOnCalendar"] = (
                self.backup.restore_check_on_calendar.value
            )
        result: dict[str, object] = {
            "id": self.resource_id.value,
            "serviceKind": self.service_kind.value,
            "environment": self.environment.value,
            "server": self.server_id.value,
            "bind": {
                "address": self.bind.address.value,
                "port": self.bind.port.value,
            },
            "backup": backup,
        }
        if self.postgresql is not None:
            postgresql: dict[str, object] = {
                "majorVersion": self.postgresql.major_version.value,
                "databases": [
                    database.as_dict()
                    for database in self.postgresql.databases
                ],
            }
            if self.postgresql.package_version is not None:
                postgresql["packageVersion"] = (
                    self.postgresql.package_version.value
                )
            if self.postgresql.remote_access is not None:
                postgresql["remoteAccess"] = (
                    self.postgresql.remote_access.as_dict()
                )
            result["postgresql"] = postgresql
        if self.redis is not None:
            redis: dict[str, object] = {
                "maxmemoryMb": self.redis.maxmemory_mb.value,
                "appendOnly": self.redis.append_only,
            }
            if self.redis.package_version is not None:
                redis["packageVersion"] = self.redis.package_version.value
            result["redis"] = redis
        if self.metrics_enabled:
            result["metrics"] = {"enabled": True}
        return result


@dataclass(frozen=True, slots=True)
class AlertRuleInventory:
    """Typed desired state for one declared alert rule."""

    resource_id: ResourceId
    environment: ResourceId
    expr: PromqlExpression
    for_duration: AlertDuration
    severity: AlertSeverity
    summary: AlertSummary

    def as_dict(self) -> dict[str, object]:
        """Serialize the alert rule for rendering and evidence."""
        return {
            "id": self.resource_id.value,
            "environment": self.environment.value,
            "expr": self.expr.value,
            "for": self.for_duration.value,
            "severity": self.severity.value,
            "summary": self.summary.value,
        }


@dataclass(frozen=True, slots=True)
class AutonomyGrant:
    """Autonomy earned per operation kind after enough verified runs."""

    operation_kind: str
    required_verified_runs: PositiveCount

    def as_dict(self) -> dict[str, object]:
        """Serialize the grant for policy consumers."""
        return {
            "kind": self.operation_kind,
            "requiredVerifiedRuns": self.required_verified_runs.value,
        }


@dataclass(frozen=True, slots=True)
class QuietHours:
    """Wall-clock window during which autonomy is suspended."""

    start: DayTime
    end: DayTime

    def contains(self, minutes_since_midnight: int) -> bool:
        """Return whether the window covers the given wall-clock minute."""
        start = self.start.minutes
        end = self.end.minutes
        if start <= end:
            return start <= minutes_since_midnight < end
        return minutes_since_midnight >= start or minutes_since_midnight < end

    def as_dict(self) -> dict[str, object]:
        """Serialize the window for policy consumers."""
        return {"start": self.start.value, "end": self.end.value}


@dataclass(frozen=True, slots=True)
class OperatorPolicyInventory:
    """Declared autonomy bounds for one environment's operator."""

    resource_id: ResourceId
    environment: ResourceId
    grants: tuple[AutonomyGrant, ...]
    max_autonomous_per_hour: PositiveCount
    quiet_hours: QuietHours | None

    def grant_for(self, operation_kind: str) -> AutonomyGrant | None:
        """Return the declared grant for one operation kind, if any."""
        return next(
            (
                grant
                for grant in self.grants
                if grant.operation_kind == operation_kind
            ),
            None,
        )

    def as_dict(self) -> dict[str, object]:
        """Serialize the policy without secret material."""
        autonomy: dict[str, object] = {
            "operations": [grant.as_dict() for grant in self.grants],
            "maxAutonomousPerHour": self.max_autonomous_per_hour.value,
        }
        if self.quiet_hours is not None:
            autonomy["quietHours"] = self.quiet_hours.as_dict()
        return {
            "id": self.resource_id.value,
            "environment": self.environment.value,
            "autonomy": autonomy,
        }


@dataclass(frozen=True, slots=True)
class SshPublicKeyInventory:
    """Validated public key scoped to one Cloudfall environment."""

    resource_id: ResourceId
    owner: ResourceId
    environment: ResourceId
    public_key: OpenSshPublicKey
    lifecycle: SshPublicKeyLifecycle

    @property
    def is_active(self) -> bool:
        """Return whether the key may be offered to execution consumers."""
        return self.lifecycle is SshPublicKeyLifecycle.ACTIVE

    def as_dict(self) -> dict[str, object]:
        """Serialize non-secret SSH public-key state."""
        return {
            "id": self.resource_id.value,
            "owner": self.owner.value,
            "environment": self.environment.value,
            "algorithm": self.public_key.algorithm,
            "publicKey": self.public_key.value,
            "lifecycle": self.lifecycle.value,
        }


@dataclass(frozen=True, slots=True)
class LoggingSoftware:
    """Pinned package versions for the observability stack."""

    loki: PackageVersion
    grafana: PackageVersion
    alloy: PackageVersion
    prometheus: PackageVersion | None


@dataclass(frozen=True, slots=True)
class MetricsBackend:
    """Loopback-only Prometheus backend colocated with the logging backend."""

    listen_address: IpAddress
    port: TcpPort
    storage_path: AbsolutePath
    retention_hours: PositiveCount


@dataclass(frozen=True, slots=True)
class MetricsCollection:
    """Host metrics collection contract for every declared collector."""

    interval_seconds: PositiveCount


@dataclass(frozen=True, slots=True)
class LoggingMetrics:
    """Push-only host metrics topology through the mTLS gateway."""

    backend: MetricsBackend
    collection: MetricsCollection


@dataclass(frozen=True, slots=True)
class LoggingBackendStorage:
    """Loki storage contract for the first single-node slice."""

    storage_type: LoggingStorageType
    path: AbsolutePath


@dataclass(frozen=True, slots=True)
class LoggingBackend:
    """Loopback-only Loki backend placement and retention."""

    server_id: ResourceId
    listen_address: IpAddress
    port: TcpPort
    storage: LoggingBackendStorage
    retention_hours: PositiveCount


@dataclass(frozen=True, slots=True)
class LoggingGatewayTls:
    """Server-side mTLS material paths for the ingestion gateway."""

    certificate_path: AbsolutePath
    key_path: AbsolutePath
    client_ca_path: AbsolutePath


@dataclass(frozen=True, slots=True)
class LoggingGateway:
    """mTLS-only Loki ingestion endpoint."""

    server_name: Hostname
    listen_address: IpAddress
    port: TcpPort
    tls: LoggingGatewayTls


@dataclass(frozen=True, slots=True)
class LoggingGrafana:
    """Loopback-only Grafana endpoint colocated with Loki."""

    listen_address: IpAddress
    port: TcpPort
    admin_password_path: AbsolutePath


@dataclass(frozen=True, slots=True)
class LoggingClientTls:
    """Collector-side mTLS material paths."""

    ca_path: AbsolutePath
    certificate_path: AbsolutePath
    key_path: AbsolutePath


@dataclass(frozen=True, slots=True)
class LoggingJournalSource:
    """Bounded systemd-journal collection contract."""

    enabled: bool
    max_age_hours: PositiveCount


@dataclass(frozen=True, slots=True)
class LoggingFileSource:
    """Explicit application log-file source and ownership labels."""

    resource_id: ResourceId
    path: AbsolutePath
    server_ids: tuple[ResourceId, ...]
    application_id: ResourceId | None
    component_id: ResourceId | None

    def as_dict(self) -> dict[str, object]:
        """Serialize one source without including log content."""
        result: dict[str, object] = {
            "id": self.resource_id.value,
            "path": self.path.value,
            "servers": [server.value for server in self.server_ids],
        }
        if self.application_id is not None:
            result["application"] = self.application_id.value
        if self.component_id is not None:
            result["component"] = self.component_id.value
        return result


@dataclass(frozen=True, slots=True)
class EmailReceiverInventory:
    """SMTP delivery contract for one declared alert receiver."""

    smarthost: SmtpSmarthost
    sender: EmailAddress
    recipient: EmailAddress
    auth_username: SmtpUsername | None
    auth_password_file: AbsolutePath | None

    def as_dict(self) -> dict[str, object]:
        """Serialize the email contract without secret material."""
        result: dict[str, object] = {
            "smarthost": self.smarthost.value,
            "from": self.sender.value,
            "to": self.recipient.value,
        }
        if self.auth_username is not None:
            result["authUsername"] = self.auth_username.value
        if self.auth_password_file is not None:
            result["authPasswordFile"] = self.auth_password_file.value
        return result


@dataclass(frozen=True, slots=True)
class AlertReceiverInventory:
    """Exactly one declared alert delivery channel."""

    resource_id: ResourceId
    webhook: WebhookUrl | None
    email: EmailReceiverInventory | None

    def __post_init__(self) -> None:
        """Reject receivers without exactly one channel."""
        if (self.webhook is None) == (self.email is None):
            message = (
                "alert receiver must declare exactly one channel: "
                f"{self.resource_id.value}"
            )
            raise ValueError(message)

    def as_dict(self) -> dict[str, object]:
        """Serialize the receiver for rendering and evidence."""
        result: dict[str, object] = {"id": self.resource_id.value}
        if self.webhook is not None:
            result["webhook"] = {"url": self.webhook.value}
        if self.email is not None:
            result["email"] = self.email.as_dict()
        return result


@dataclass(frozen=True, slots=True)
class AlertmanagerBackend:
    """Loopback-only Alertmanager colocated with the logging backend."""

    listen_address: IpAddress
    port: TcpPort
    package_version: PackageVersion | None


@dataclass(frozen=True, slots=True)
class LoggingAlerting:
    """Declared alert delivery topology."""

    alertmanager: AlertmanagerBackend
    receivers: tuple[AlertReceiverInventory, ...]

    def as_dict(self) -> dict[str, object]:
        """Serialize the alerting topology without secret material."""
        alertmanager: dict[str, object] = {
            "listenAddress": self.alertmanager.listen_address.value,
            "port": self.alertmanager.port.value,
        }
        if self.alertmanager.package_version is not None:
            alertmanager["packageVersion"] = (
                self.alertmanager.package_version.value
            )
        return {
            "alertmanager": alertmanager,
            "receivers": [receiver.as_dict() for receiver in self.receivers],
        }


@dataclass(frozen=True, slots=True)
class LoggingCollectors:
    """Fleet placement and sources for Grafana Alloy."""

    server_ids: tuple[ResourceId, ...]
    client_tls: LoggingClientTls
    journal: LoggingJournalSource
    files: tuple[LoggingFileSource, ...]


@dataclass(frozen=True, slots=True)
class LoggingStackInventory:
    """Safe parallel-migration logging topology."""

    resource_id: ResourceId
    environment: ResourceId
    migration_mode: LoggingMigrationMode
    preserve_legacy_agents: bool
    software: LoggingSoftware
    metrics: LoggingMetrics
    backend: LoggingBackend
    gateway: LoggingGateway
    grafana: LoggingGrafana
    collectors: LoggingCollectors
    alerting: LoggingAlerting | None

    def as_dict(self) -> dict[str, object]:
        """Serialize the logging topology without certificate contents."""
        software: dict[str, object] = {
            "lokiPackageVersion": self.software.loki.value,
            "grafanaPackageVersion": self.software.grafana.value,
            "alloyPackageVersion": self.software.alloy.value,
        }
        if self.software.prometheus is not None:
            software["prometheusPackageVersion"] = self.software.prometheus.value
        result: dict[str, object] = {
            "id": self.resource_id.value,
            "environment": self.environment.value,
            "migration": {
                "mode": self.migration_mode.value,
                "preserveLegacyAgents": self.preserve_legacy_agents,
            },
            "software": software,
            "metrics": {
                "backend": {
                    "listenAddress": self.metrics.backend.listen_address.value,
                    "port": self.metrics.backend.port.value,
                    "storagePath": self.metrics.backend.storage_path.value,
                    "retentionHours": (
                        self.metrics.backend.retention_hours.value
                    ),
                },
                "collection": {
                    "intervalSeconds": (
                        self.metrics.collection.interval_seconds.value
                    ),
                },
            },
            "backend": {
                "server": self.backend.server_id.value,
                "listenAddress": self.backend.listen_address.value,
                "port": self.backend.port.value,
                "storage": {
                    "type": self.backend.storage.storage_type.value,
                    "path": self.backend.storage.path.value,
                },
                "retentionHours": self.backend.retention_hours.value,
            },
            "gateway": {
                "serverName": self.gateway.server_name.value,
                "listenAddress": self.gateway.listen_address.value,
                "port": self.gateway.port.value,
                "tls": {
                    "certificatePath": self.gateway.tls.certificate_path.value,
                    "keyPath": self.gateway.tls.key_path.value,
                    "clientCaPath": self.gateway.tls.client_ca_path.value,
                },
            },
            "grafana": {
                "listenAddress": self.grafana.listen_address.value,
                "port": self.grafana.port.value,
                "adminPasswordPath": self.grafana.admin_password_path.value,
            },
            "collectors": {
                "servers": [server.value for server in self.collectors.server_ids],
                "clientTls": {
                    "caPath": self.collectors.client_tls.ca_path.value,
                    "certificatePath": (
                        self.collectors.client_tls.certificate_path.value
                    ),
                    "keyPath": self.collectors.client_tls.key_path.value,
                },
                "journal": {
                    "enabled": self.collectors.journal.enabled,
                    "maxAgeHours": self.collectors.journal.max_age_hours.value,
                },
                "files": [source.as_dict() for source in self.collectors.files],
            },
        }
        if self.alerting is not None:
            result["alerting"] = self.alerting.as_dict()
        return result


@dataclass(frozen=True, slots=True)
class PlatformInventory:
    """Typed inventory of validated servers, applications, and components."""

    servers: tuple[ServerInventory, ...]
    server_types: tuple[ServerTypeInventory, ...]
    applications: tuple[ApplicationInventory, ...]
    components: tuple[ComponentInventory, ...]
    domains: tuple[DomainInventory, ...]
    services: tuple[ServiceInventory, ...]
    ssh_public_keys: tuple[SshPublicKeyInventory, ...]
    logging_stacks: tuple[LoggingStackInventory, ...]
    alert_rules: tuple[AlertRuleInventory, ...]
    operator_policies: tuple[OperatorPolicyInventory, ...]

    @classmethod
    def from_state(cls, state: ValidatedConfig) -> PlatformInventory:
        """Application validated state into stable inventory records."""
        servers = tuple(
            _server_inventory(document)
            for document in state.resources(ResourceKind.SERVER)
        )
        server_types = tuple(
            _server_type_inventory(document)
            for document in state.resources(ResourceKind.HOST_PROFILE)
        )
        applications = tuple(
            _application_inventory(document)
            for document in state.resources(ResourceKind.APPLICATION)
        )
        components = tuple(
            _component_inventory(document)
            for document in state.resources(ResourceKind.COMPONENT)
        )
        domains = tuple(
            _domain_inventory(document)
            for document in state.resources(ResourceKind.DOMAIN)
        )
        applications_by_id = {
            application.resource_id: application for application in applications
        }
        services = tuple(
            _service_inventory(document, applications_by_id)
            for document in state.resources(ResourceKind.SERVICE)
        )
        ssh_public_keys = tuple(
            _ssh_public_key_inventory(document)
            for document in state.resources(ResourceKind.SSH_PUBLIC_KEY)
        )
        logging_stacks = tuple(
            _logging_stack_inventory(document)
            for document in state.resources(ResourceKind.LOGGING_STACK)
        )
        alert_rules = tuple(
            _alert_rule_inventory(document)
            for document in state.resources(ResourceKind.ALERT_RULE)
        )
        operator_policies = tuple(
            _operator_policy_inventory(document)
            for document in state.resources(ResourceKind.OPERATOR_POLICY)
        )
        return cls(
            servers=servers,
            server_types=server_types,
            applications=applications,
            components=components,
            domains=domains,
            services=services,
            ssh_public_keys=ssh_public_keys,
            logging_stacks=logging_stacks,
            alert_rules=alert_rules,
            operator_policies=operator_policies,
        )

    def server_type(self, server_type_id: ResourceId) -> ServerTypeInventory:
        """Return an existing desired server type."""
        server_type = next(
            (
                candidate
                for candidate in self.server_types
                if candidate.resource_id == server_type_id
            ),
            None,
        )
        if server_type is None:
            message = f"server type does not exist: {server_type_id}"
            raise KeyError(message)
        return server_type

    def components_on_server(
        self, server_id: ResourceId
    ) -> tuple[ComponentInventory, ...]:
        """Return components assigned to an existing server."""
        if not any(server.resource_id == server_id for server in self.servers):
            message = f"server does not exist: {server_id}"
            raise KeyError(message)
        return tuple(
            component
            for component in self.components
            if server_id in component.server_ids
        )

    def servers_for_component(
        self, component_id: ResourceId
    ) -> tuple[ServerInventory, ...]:
        """Return servers assigned to an existing component."""
        component = next(
            (
                candidate
                for candidate in self.components
                if candidate.resource_id == component_id
            ),
            None,
        )
        if component is None:
            message = f"component does not exist: {component_id}"
            raise KeyError(message)
        assigned = frozenset(component.server_ids)
        return tuple(
            server for server in self.servers if server.resource_id in assigned
        )

    def as_dict(self) -> dict[str, object]:
        """Serialize the complete non-secret platform inventory."""
        return {
            "servers": [server.as_dict() for server in self.servers],
            "serverTypes": [
                server_type.as_dict() for server_type in self.server_types
            ],
            "applications": [
                application.as_dict() for application in self.applications
            ],
            "components": [component.as_dict() for component in self.components],
            "domains": [domain.as_dict() for domain in self.domains],
            "services": [service.as_dict() for service in self.services],
            "sshPublicKeys": [key.as_dict() for key in self.ssh_public_keys],
            "loggingStacks": [stack.as_dict() for stack in self.logging_stacks],
            "alertRules": [rule.as_dict() for rule in self.alert_rules],
            "operatorPolicies": [
                policy.as_dict() for policy in self.operator_policies
            ],
        }


def _server_inventory(document: ResourceDocument) -> ServerInventory:
    spec = _mapping(document.content, "spec")
    ssh = _mapping(spec, "ssh")
    labels = _labels(spec.get("labels", {}))
    return ServerInventory(
        resource_id=document.key.resource_id,
        hostname=Hostname.from_boundary(spec.get("hostname")),
        address=ConnectionAddress.from_boundary(spec.get("address")),
        environment=ResourceId.from_boundary(spec.get("environment")),
        server_type_id=ResourceId.from_boundary(spec.get("serverType")),
        lifecycle=ServerLifecycle.from_boundary(spec.get("lifecycle")),
        ssh_user=LinuxUser.from_boundary(ssh.get("user")),
        ssh_port=TcpPort.from_boundary(ssh.get("port")),
        labels=labels,
        provider=_server_provider(spec.get("provider")),
        network=_server_network(spec.get("network")),
    )


def _server_provider(value: object) -> ServerProvider | None:
    if value is None:
        return None
    provider = _boundary_mapping(value, "server provider")
    server_id = provider.get("serverId")
    datacenter = provider.get("datacenter")
    return ServerProvider(
        name=ResourceId.from_boundary(provider.get("name")),
        server_id=(
            ProviderServerId.from_boundary(server_id) if server_id is not None else None
        ),
        datacenter=(
            DatacenterCode.from_boundary(datacenter) if datacenter is not None else None
        ),
    )


def _server_network(value: object) -> ServerNetwork | None:
    if value is None:
        return None
    network = _boundary_mapping(value, "server network")
    return ServerNetwork(
        ipv4=Ipv4Address.from_boundary(network.get("ipv4")),
        ipv6_cidr=Ipv6NetworkCidr.from_boundary(network.get("ipv6Cidr")),
        reverse_dns=Hostname.from_boundary(network.get("reverseDns")),
    )


def _server_type_inventory(document: ResourceDocument) -> ServerTypeInventory:
    spec = _mapping(document.content, "spec")
    os_requirement = _mapping(spec, "os")
    storage = _mapping(spec, "storage")
    raw_raid = storage.get("softwareRaid")
    raid = None if raw_raid is None else _mapping(storage, "softwareRaid")
    packages = _mapping(spec, "packages")
    services = _mapping(spec, "services")
    configuration = _mapping(spec, "configuration")
    return ServerTypeInventory(
        resource_id=document.key.resource_id,
        os=OperatingSystemRequirement(
            distribution=OperatingSystemDistribution.from_boundary(
                os_requirement.get("distribution")
            ),
            versions=tuple(
                OperatingSystemMajorVersion.from_boundary(item)
                for item in _list(os_requirement.get("versions"), "OS versions")
            ),
            service_manager=ServiceManager.from_boundary(
                os_requirement.get("serviceManager")
            ),
        ),
        raid=(
            None
            if raid is None
            else RaidRequirement(
                level=RaidLevel.from_boundary(raid.get("level")),
                minimum_active_devices=PositiveCount.from_boundary(
                    raid.get("minimumActiveDevices")
                ),
                minimum_usable_bytes=ByteSize.from_boundary(
                    raid.get("minimumUsableBytes")
                ),
            )
        ),
        mounts=tuple(
            _mount_requirement(item)
            for item in _mapping_list(storage.get("mounts"), "mount requirements")
        ),
        required_packages=tuple(
            _package_requirement(item)
            for item in _mapping_list(packages.get("required"), "required packages")
        ),
        forbidden_packages=tuple(
            PackageName.from_boundary(item)
            for item in _list(packages.get("forbidden"), "forbidden packages")
        ),
        required_services=tuple(
            _service_requirement(item)
            for item in _mapping_list(services.get("required"), "required services")
        ),
        firewall=_firewall_requirement(spec.get("firewall")),
        configuration_files=tuple(
            _configuration_requirement(item)
            for item in _mapping_list(configuration.get("files"), "configuration files")
        ),
    )


def _application_inventory(document: ResourceDocument) -> ApplicationInventory:
    spec = _mapping(document.content, "spec")
    return ApplicationInventory(
        resource_id=document.key.resource_id,
        linux_user=LinuxUser.from_boundary(spec.get("linuxUser")),
        approval=DeploymentApproval.from_boundary(spec.get("approval")),
        component_ids=_resource_ids(spec.get("components"), "application components"),
        secret_refs=_secret_references(spec.get("secretRefs")),
    )


def _component_inventory(document: ResourceDocument) -> ComponentInventory:
    spec = _mapping(document.content, "spec")
    deployment = _mapping(spec, "deployment")
    repository = _mapping(spec, "repository")
    runtime = _mapping(spec, "runtime")
    service = _mapping(spec, "service")
    health = _mapping(spec, "healthCheck")
    raw_subdirectory = repository.get("subdirectory")
    if raw_subdirectory is not None and not isinstance(raw_subdirectory, str):
        message = "validated repository subdirectory is not a string"
        raise TypeError(message)
    return ComponentInventory(
        resource_id=document.key.resource_id,
        application_id=ResourceId.from_boundary(spec.get("application")),
        server_ids=_resource_ids(deployment.get("servers"), "component servers"),
        install_root=AbsolutePath.from_boundary(deployment.get("installRoot")),
        retain_until_cleanup=_boolean(
            deployment.get("retainUntilCleanup"), "retain until cleanup"
        ),
        repository=ComponentRepository(
            url=RepositoryUrl.from_boundary(repository.get("url")),
            subdirectory=raw_subdirectory,
        ),
        runtime=ComponentRuntime(
            runtime_type=RuntimeType.from_boundary(runtime.get("type")),
            version=RuntimeVersion.from_boundary(runtime.get("version")),
            package_manager=RuntimePackageManager.from_boundary(
                runtime.get("packageManager")
            ),
        ),
        service=ComponentService(
            manager=ServiceManager.from_boundary(service.get("manager")),
            name=ResourceId.from_boundary(service.get("name")),
            command=ServiceCommand.from_boundary(service.get("command")),
        ),
        health_check=_component_health_check(health),
        secret_refs=_secret_references(spec.get("secretRefs")),
        environment=_declared_environment(spec.get("environment")),
        build=_component_build(spec),
        release=_component_release(spec.get("release")),
    )


def _component_build(spec: Mapping[str, object]) -> ComponentBuild | None:
    if spec.get("build") is None:
        if spec.get("buildTools") is not None or spec.get("artifact") is not None:
            message = "validated component has buildTools or artifact without build"
            raise ValueError(message)
        return None
    raw_tools = spec.get("buildTools")
    tools = () if raw_tools is None else tuple(
        ComponentBuildTool(
            name=BuildToolName.from_boundary(name),
            minimum_version=BuildToolVersion.from_boundary(version),
        )
        for name, version in _boundary_mapping(raw_tools, "build tools").items()
    )
    raw_artifact = spec.get("artifact")
    include = () if raw_artifact is None else tuple(
        RelativeRepositoryPath.from_boundary(item)
        for item in _list(
            _mapping(spec, "artifact").get("include"), "artifact include paths"
        )
    )
    return ComponentBuild(
        tools=tools,
        steps=tuple(
            ComponentBuildStep(
                working_directory=RelativeRepositoryPath.from_boundary(
                    step.get("workingDirectory")
                ),
                command=BuildCommand.from_boundary(step.get("command")),
                timeout_seconds=BuildTimeoutSeconds.from_boundary(
                    step.get("timeoutSeconds")
                ),
            )
            for step in _mapping_list(spec.get("build"), "build steps")
        ),
        include=include,
    )


def _component_release(value: object) -> ComponentRelease | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        message = "validated component release is not a mapping"
        raise TypeError(message)
    raw_directory = value.get("workingDirectory")
    return ComponentRelease(
        command=CommandArgv.from_boundary(value.get("command")),
        working_directory=(
            None
            if raw_directory is None
            else RelativeDirectory.from_boundary(raw_directory)
        ),
        timeout_seconds=ReleaseCommandTimeout.from_boundary(
            value.get("timeoutSeconds")
        ),
    )


def _declared_environment(value: object) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(entry, str)
        for key, entry in value.items()
    ):
        message = "validated component environment is not a string mapping"
        raise TypeError(message)
    return tuple(value.items())


def _secret_references(value: object) -> tuple[SecretReference, ...]:
    if value is None:
        return ()
    return tuple(
        SecretReference(
            scope=SecretScope.from_boundary(item.get("scope")),
            environment=ResourceId.from_boundary(item.get("environment")),
            path=AbsolutePath.from_boundary(item.get("path")),
        )
        for item in _mapping_list(value, "secret references")
    )


def _component_health_check(
    health: Mapping[str, object],
) -> ComponentHealthCheck:
    check_type = HealthCheckType.from_boundary(health.get("type"))
    if check_type is HealthCheckType.NONE:
        return ComponentHealthCheck(check_type=check_type, http=None)
    return ComponentHealthCheck(
        check_type=check_type,
        http=ComponentHttpHealthCheck(
            scheme=HttpScheme.from_boundary(health.get("scheme")),
            port=TcpPort.from_boundary(health.get("port")),
            path=_string(health, "path"),
            expected_statuses=tuple(
                HttpStatusCode.from_boundary(item)
                for item in _list(
                    health.get("expectedStatuses"), "expected HTTP statuses"
                )
            ),
            timeout_seconds=PositiveCount.from_boundary(
                health.get("timeoutSeconds")
            ),
            attempts=PositiveCount.from_boundary(health.get("attempts")),
        ),
    )


def _domain_inventory(document: ResourceDocument) -> DomainInventory:
    spec = _mapping(document.content, "spec")
    proxy = _mapping(spec, "proxy")
    raw_proxy_upstream = proxy.get("upstream")
    proxy_upstream = (
        None if raw_proxy_upstream is None else _mapping(proxy, "upstream")
    )
    origin = _mapping(spec, "origin")
    edge = _mapping(spec, "edge")
    tls = _mapping(spec, "tls")
    health = _mapping(spec, "healthCheck")
    return DomainInventory(
        resource_id=document.key.resource_id,
        primary_name=Hostname.from_boundary(spec.get("primaryName")),
        aliases=tuple(
            Hostname.from_boundary(item)
            for item in _list(spec.get("aliases"), "domain aliases")
        ),
        proxy=DomainProxy(
            server_id=ResourceId.from_boundary(proxy.get("server")),
            configuration_path=AbsolutePath.from_boundary(
                proxy.get("configurationPath")
            ),
            service_name=ServiceName.from_boundary(proxy.get("service")),
            upstream_address=(
                None
                if proxy_upstream is None
                else IpAddress.from_boundary(proxy_upstream.get("address"))
            ),
            upstream_port=(
                None
                if proxy_upstream is None
                else TcpPort.from_boundary(proxy_upstream.get("port"))
            ),
        ),
        origin=DomainOrigin(
            server_id=ResourceId.from_boundary(origin.get("server")),
            configuration_path=AbsolutePath.from_boundary(
                origin.get("configurationPath")
            ),
            service_name=ServiceName.from_boundary(origin.get("service")),
            scheme=HttpScheme.from_boundary(origin.get("scheme")),
            port=TcpPort.from_boundary(origin.get("port")),
            server_name=Hostname.from_boundary(origin.get("serverName")),
        ),
        edge=DomainEdge(
            provider=ResourceId.from_boundary(edge.get("provider")),
            mode=DnsMode.from_boundary(edge.get("mode")),
        ),
        tls_mode=TlsMode.from_boundary(tls.get("mode")),
        health_check=DomainHealthCheck(
            scheme=HttpScheme.from_boundary(health.get("scheme")),
            path=_string(health, "path"),
            expected_statuses=tuple(
                HttpStatusCode.from_boundary(item)
                for item in _list(
                    health.get("expectedStatuses"), "expected HTTP statuses"
                )
            ),
            timeout_seconds=PositiveCount.from_boundary(health.get("timeoutSeconds")),
        ),
        routes=(
            ()
            if spec.get("routes") is None
            else tuple(
                _domain_route(item)
                for item in _mapping_list(spec.get("routes"), "domain routes")
            )
        ),
    )


def _domain_route(route: Mapping[str, object]) -> DomainRoute:
    path = DomainRoutePath.from_boundary(route.get("path"))
    if route.get("static") is None:
        upstream = _mapping(route, "upstream")
        return DomainProxyRoute(
            path=path,
            upstream_address=IpAddress.from_boundary(upstream.get("address")),
            upstream_port=TcpPort.from_boundary(upstream.get("port")),
        )
    if route.get("upstream") is not None:
        message = f"route {path.value!r} declares both upstream and static"
        raise ValueError(message)
    static = _mapping(route, "static")
    raw_spa_fallback = static.get("spaFallback")
    return DomainStaticRoute(
        path=path,
        component_id=ResourceId.from_boundary(static.get("component")),
        directory=ReleaseRelativePath.from_boundary(static.get("directory")),
        spa_fallback=(
            None
            if raw_spa_fallback is None
            else ReleaseRelativePath.from_boundary(raw_spa_fallback)
        ),
    )


def _service_inventory(
    document: ResourceDocument,
    applications_by_id: Mapping[ResourceId, ApplicationInventory],
) -> ServiceInventory:
    spec = _mapping(document.content, "spec")
    bind = _mapping(spec, "bind")
    backup = _mapping(spec, "backup")
    service_kind = ServiceKind.from_boundary(spec.get("serviceKind"))
    postgresql = None
    if service_kind is ServiceKind.POSTGRESQL:
        raw_postgresql = _mapping(spec, "postgresql")
        raw_package_version = raw_postgresql.get("packageVersion")
        postgresql = PostgresqlService(
            major_version=PostgresMajorVersion.from_boundary(
                raw_postgresql.get("majorVersion")
            ),
            package_version=(
                PackageVersion.from_boundary(raw_package_version)
                if raw_package_version is not None
                else None
            ),
            databases=tuple(
                _postgres_database(item, applications_by_id)
                for item in _mapping_list(
                    raw_postgresql.get("databases"), "service databases"
                )
            ),
            remote_access=(
                _postgres_remote_access(_mapping(raw_postgresql, "remoteAccess"))
                if "remoteAccess" in raw_postgresql
                else None
            ),
        )
    redis = None
    if service_kind is ServiceKind.REDIS:
        raw_redis = _mapping(spec, "redis")
        raw_redis_package = raw_redis.get("packageVersion")
        redis = RedisService(
            package_version=(
                PackageVersion.from_boundary(raw_redis_package)
                if raw_redis_package is not None
                else None
            ),
            maxmemory_mb=PositiveCount.from_boundary(
                raw_redis.get("maxmemoryMb")
            ),
            append_only=_boolean(
                raw_redis.get("appendOnly"), "redis append only"
            ),
        )
    return ServiceInventory(
        resource_id=document.key.resource_id,
        service_kind=service_kind,
        environment=ResourceId.from_boundary(spec.get("environment")),
        server_id=ResourceId.from_boundary(spec.get("server")),
        bind=ServiceBind(
            address=IpAddress.from_boundary(bind.get("address")),
            port=TcpPort.from_boundary(bind.get("port")),
        ),
        postgresql=postgresql,
        redis=redis,
        backup=ServiceBackup(
            directory=AbsolutePath.from_boundary(backup.get("directory")),
            on_calendar=SystemdCalendar.from_boundary(backup.get("onCalendar")),
            retention_days=PositiveCount.from_boundary(
                backup.get("retentionDays")
            ),
            restore_check_on_calendar=(
                SystemdCalendar.from_boundary(
                    backup["restoreCheckOnCalendar"]
                )
                if "restoreCheckOnCalendar" in backup
                else None
            ),
        ),
        metrics_enabled=_service_metrics_enabled(spec, document),
    )


def _alert_rule_inventory(document: ResourceDocument) -> AlertRuleInventory:
    spec = _mapping(document.content, "spec")
    return AlertRuleInventory(
        resource_id=document.key.resource_id,
        environment=ResourceId.from_boundary(spec.get("environment")),
        expr=PromqlExpression.from_boundary(spec.get("expr")),
        for_duration=AlertDuration.from_boundary(spec.get("for")),
        severity=AlertSeverity.from_boundary(spec.get("severity")),
        summary=AlertSummary.from_boundary(spec.get("summary")),
    )


def _operator_policy_inventory(
    document: ResourceDocument,
) -> OperatorPolicyInventory:
    spec = _mapping(document.content, "spec")
    autonomy = _mapping(spec, "autonomy")
    raw_quiet = autonomy.get("quietHours")
    quiet_hours = None
    if raw_quiet is not None:
        quiet = _mapping(autonomy, "quietHours")
        quiet_hours = QuietHours(
            start=DayTime.from_boundary(quiet.get("start")),
            end=DayTime.from_boundary(quiet.get("end")),
        )
    return OperatorPolicyInventory(
        resource_id=document.key.resource_id,
        environment=ResourceId.from_boundary(spec.get("environment")),
        grants=tuple(
            AutonomyGrant(
                operation_kind=_string(item, "kind"),
                required_verified_runs=PositiveCount.from_boundary(
                    item.get("requiredVerifiedRuns")
                ),
            )
            for item in _mapping_list(
                autonomy.get("operations"), "autonomy operations"
            )
        ),
        max_autonomous_per_hour=PositiveCount.from_boundary(
            autonomy.get("maxAutonomousPerHour")
        ),
        quiet_hours=quiet_hours,
    )


def _service_metrics_enabled(
    spec: Mapping[str, object], document: ResourceDocument
) -> bool:
    raw_metrics = spec.get("metrics")
    if raw_metrics is None:
        return False
    metrics = _mapping(spec, "metrics")
    if metrics.get("enabled") is not True:
        message = (
            "validated service metrics block must declare enabled: true "
            f"({document.source.display()})"
        )
        raise ValueError(message)
    return True


def _postgres_database(
    content: Mapping[str, object],
    applications_by_id: Mapping[ResourceId, ApplicationInventory],
) -> PostgresDatabase | OwnedPostgresDatabase:
    if "owner" in content:
        password_ref = _mapping(content, "passwordRef")
        return OwnedPostgresDatabase(
            name=PostgresDatabaseName.from_boundary(content.get("name")),
            owner=PostgresRoleName.from_boundary(content.get("owner")),
            password_ref=SecretValueReference(
                environment=ResourceId.from_boundary(password_ref.get("environment")),
                path=AbsolutePath.from_boundary(password_ref.get("path")),
                key=SecretKeyName.from_boundary(password_ref.get("key")),
            ),
        )
    application_id = ResourceId.from_boundary(content.get("application"))
    application = applications_by_id.get(application_id)
    if application is None:
        message = (
            f"service database references an unknown application: {application_id}"
        )
        raise KeyError(message)
    return PostgresDatabase(
        name=PostgresDatabaseName.from_boundary(content.get("name")),
        application_id=application_id,
        owner=application.linux_user,
    )


def _postgres_remote_access(content: Mapping[str, object]) -> PostgresRemoteAccess:
    raw_sources = content.get("allowedSources")
    if not isinstance(raw_sources, list) or not raw_sources:
        message = "validated remote access sources are not a non-empty list"
        raise ValueError(message)
    return PostgresRemoteAccess(
        listen_address=IpAddress.from_boundary(content.get("listenAddress")),
        allowed_sources=tuple(
            IpNetworkCidr.from_boundary(source) for source in raw_sources
        ),
    )


def _ssh_public_key_inventory(
    document: ResourceDocument,
) -> SshPublicKeyInventory:
    spec = _mapping(document.content, "spec")
    return SshPublicKeyInventory(
        resource_id=document.key.resource_id,
        owner=ResourceId.from_boundary(spec.get("owner")),
        environment=ResourceId.from_boundary(spec.get("environment")),
        public_key=OpenSshPublicKey.from_boundary(spec.get("publicKey")),
        lifecycle=SshPublicKeyLifecycle.from_boundary(spec.get("lifecycle")),
    )


def _logging_stack_inventory(
    document: ResourceDocument,
) -> LoggingStackInventory:
    spec = _mapping(document.content, "spec")
    migration = _mapping(spec, "migration")
    software = _mapping(spec, "software")
    raw_prometheus = software.get("prometheusPackageVersion")
    metrics = _mapping(spec, "metrics")
    metrics_backend = _mapping(metrics, "backend")
    metrics_collection = _mapping(metrics, "collection")
    backend = _mapping(spec, "backend")
    storage = _mapping(backend, "storage")
    gateway = _mapping(spec, "gateway")
    gateway_tls = _mapping(gateway, "tls")
    grafana = _mapping(spec, "grafana")
    collectors = _mapping(spec, "collectors")
    client_tls = _mapping(collectors, "clientTls")
    journal = _mapping(collectors, "journal")
    alerting = (
        _logging_alerting(_mapping(spec, "alerting"))
        if spec.get("alerting") is not None
        else None
    )
    return LoggingStackInventory(
        resource_id=document.key.resource_id,
        environment=ResourceId.from_boundary(spec.get("environment")),
        migration_mode=LoggingMigrationMode.from_boundary(migration.get("mode")),
        preserve_legacy_agents=_boolean(
            migration.get("preserveLegacyAgents"), "preserve legacy agents"
        ),
        software=LoggingSoftware(
            loki=PackageVersion.from_boundary(software.get("lokiPackageVersion")),
            grafana=PackageVersion.from_boundary(software.get("grafanaPackageVersion")),
            alloy=PackageVersion.from_boundary(software.get("alloyPackageVersion")),
            prometheus=(
                PackageVersion.from_boundary(raw_prometheus)
                if raw_prometheus is not None
                else None
            ),
        ),
        metrics=LoggingMetrics(
            backend=MetricsBackend(
                listen_address=IpAddress.from_boundary(
                    metrics_backend.get("listenAddress")
                ),
                port=TcpPort.from_boundary(metrics_backend.get("port")),
                storage_path=AbsolutePath.from_boundary(
                    metrics_backend.get("storagePath")
                ),
                retention_hours=PositiveCount.from_boundary(
                    metrics_backend.get("retentionHours")
                ),
            ),
            collection=MetricsCollection(
                interval_seconds=PositiveCount.from_boundary(
                    metrics_collection.get("intervalSeconds")
                ),
            ),
        ),
        backend=LoggingBackend(
            server_id=ResourceId.from_boundary(backend.get("server")),
            listen_address=IpAddress.from_boundary(backend.get("listenAddress")),
            port=TcpPort.from_boundary(backend.get("port")),
            storage=LoggingBackendStorage(
                storage_type=LoggingStorageType.from_boundary(storage.get("type")),
                path=AbsolutePath.from_boundary(storage.get("path")),
            ),
            retention_hours=PositiveCount.from_boundary(backend.get("retentionHours")),
        ),
        gateway=LoggingGateway(
            server_name=Hostname.from_boundary(gateway.get("serverName")),
            listen_address=IpAddress.from_boundary(gateway.get("listenAddress")),
            port=TcpPort.from_boundary(gateway.get("port")),
            tls=LoggingGatewayTls(
                certificate_path=AbsolutePath.from_boundary(
                    gateway_tls.get("certificatePath")
                ),
                key_path=AbsolutePath.from_boundary(gateway_tls.get("keyPath")),
                client_ca_path=AbsolutePath.from_boundary(
                    gateway_tls.get("clientCaPath")
                ),
            ),
        ),
        grafana=LoggingGrafana(
            listen_address=IpAddress.from_boundary(grafana.get("listenAddress")),
            port=TcpPort.from_boundary(grafana.get("port")),
            admin_password_path=AbsolutePath.from_boundary(
                grafana.get("adminPasswordPath")
            ),
        ),
        collectors=LoggingCollectors(
            server_ids=_resource_ids(
                collectors.get("servers"), "logging collector servers"
            ),
            client_tls=LoggingClientTls(
                ca_path=AbsolutePath.from_boundary(client_tls.get("caPath")),
                certificate_path=AbsolutePath.from_boundary(
                    client_tls.get("certificatePath")
                ),
                key_path=AbsolutePath.from_boundary(client_tls.get("keyPath")),
            ),
            journal=LoggingJournalSource(
                enabled=_boolean(journal.get("enabled"), "journal enabled"),
                max_age_hours=PositiveCount.from_boundary(journal.get("maxAgeHours")),
            ),
            files=tuple(
                _logging_file_source(item)
                for item in _mapping_list(
                    collectors.get("files"), "logging file sources"
                )
            ),
        ),
        alerting=alerting,
    )


def _logging_alerting(content: Mapping[str, object]) -> LoggingAlerting:
    alertmanager = _mapping(content, "alertmanager")
    raw_package_version = alertmanager.get("packageVersion")
    return LoggingAlerting(
        alertmanager=AlertmanagerBackend(
            listen_address=IpAddress.from_boundary(
                alertmanager.get("listenAddress")
            ),
            port=TcpPort.from_boundary(alertmanager.get("port")),
            package_version=(
                PackageVersion.from_boundary(raw_package_version)
                if raw_package_version is not None
                else None
            ),
        ),
        receivers=tuple(
            _alert_receiver(item)
            for item in _mapping_list(
                content.get("receivers"), "alert receivers"
            )
        ),
    )


def _alert_receiver(content: Mapping[str, object]) -> AlertReceiverInventory:
    raw_webhook = content.get("webhook")
    raw_email = content.get("email")
    webhook = None
    if raw_webhook is not None:
        webhook_mapping = _boundary_mapping(raw_webhook, "webhook receiver")
        webhook = WebhookUrl.from_boundary(webhook_mapping.get("url"))
    email = None
    if raw_email is not None:
        email_mapping = _boundary_mapping(raw_email, "email receiver")
        raw_auth_username = email_mapping.get("authUsername")
        raw_auth_password_file = email_mapping.get("authPasswordFile")
        email = EmailReceiverInventory(
            smarthost=SmtpSmarthost.from_boundary(
                email_mapping.get("smarthost")
            ),
            sender=EmailAddress.from_boundary(email_mapping.get("from")),
            recipient=EmailAddress.from_boundary(email_mapping.get("to")),
            auth_username=(
                SmtpUsername.from_boundary(raw_auth_username)
                if raw_auth_username is not None
                else None
            ),
            auth_password_file=(
                AbsolutePath.from_boundary(raw_auth_password_file)
                if raw_auth_password_file is not None
                else None
            ),
        )
    return AlertReceiverInventory(
        resource_id=ResourceId.from_boundary(content.get("id")),
        webhook=webhook,
        email=email,
    )


def _logging_file_source(content: Mapping[str, object]) -> LoggingFileSource:
    raw_application = content.get("application")
    raw_component = content.get("component")
    return LoggingFileSource(
        resource_id=ResourceId.from_boundary(content.get("id")),
        path=AbsolutePath.from_boundary(content.get("path")),
        server_ids=_resource_ids(content.get("servers"), "log-file servers"),
        application_id=(
            ResourceId.from_boundary(raw_application)
            if raw_application is not None
            else None
        ),
        component_id=(
            ResourceId.from_boundary(raw_component)
            if raw_component is not None
            else None
        ),
    )


def _mount_requirement(content: Mapping[str, object]) -> MountRequirement:
    return MountRequirement(
        path=AbsolutePath.from_boundary(content.get("path")),
        filesystem=FilesystemName.from_boundary(content.get("filesystem")),
        minimum_bytes=ByteSize.from_boundary(content.get("minimumBytes")),
    )


def _package_requirement(content: Mapping[str, object]) -> PackageRequirement:
    raw_version = content.get("version")
    version = (
        PackageVersion.from_boundary(raw_version) if raw_version is not None else None
    )
    return PackageRequirement(
        name=PackageName.from_boundary(content.get("name")),
        version=version,
    )


def _service_requirement(content: Mapping[str, object]) -> ServiceRequirement:
    return ServiceRequirement(
        name=SystemdUnitName.from_boundary(content.get("name")),
        state=RequiredServiceState.from_boundary(content.get("state")),
        status=RequiredServiceStatus.from_boundary(content.get("status")),
    )


def _firewall_requirement(value: object) -> FirewallRequirement | None:
    if value is None:
        return None
    firewall = _boundary_mapping(value, "server type firewall")
    return FirewallRequirement(
        policy=FirewallPolicy.from_boundary(firewall.get("policy")),
        allowed_inbound=tuple(
            _firewall_rule(item)
            for item in _mapping_list(
                firewall.get("allowedInbound"), "firewall rules"
            )
        ),
    )


def _firewall_rule(content: Mapping[str, object]) -> FirewallRule:
    raw_description = content.get("description")
    if raw_description is not None and not isinstance(raw_description, str):
        message = "validated firewall rule description is not a string"
        raise TypeError(message)
    return FirewallRule(
        port=TcpPort.from_boundary(content.get("port")),
        protocol=NetworkProtocol.from_boundary(content.get("protocol")),
        description=raw_description,
    )


def firewall_rules_for_server(
    firewall: FirewallRequirement, ssh_port: TcpPort
) -> tuple[FirewallRule, ...]:
    """Return effective inbound rules with the server's SSH port guaranteed."""
    declared = {
        (rule.port.value, rule.protocol) for rule in firewall.allowed_inbound
    }
    if (ssh_port.value, NetworkProtocol.TCP) in declared:
        return firewall.allowed_inbound
    ssh_rule = FirewallRule(
        port=ssh_port,
        protocol=NetworkProtocol.TCP,
        description="ssh",
    )
    return (ssh_rule, *firewall.allowed_inbound)


@dataclass(frozen=True, slots=True)
class SourceRestrictedFirewallRule:
    """One inbound endpoint opened to declared source networks only."""

    port: TcpPort
    protocol: NetworkProtocol
    sources: tuple[IpNetworkCidr, ...]
    description: str

    def as_dict(self) -> dict[str, object]:
        """Serialize the rule for engine and audit consumers."""
        return {
            "port": self.port.value,
            "protocol": self.protocol.value,
            "sources": [source.value for source in self.sources],
            "description": self.description,
        }


def remote_access_rules_for_server(
    server: ServerInventory, services: tuple[ServiceInventory, ...]
) -> tuple[SourceRestrictedFirewallRule, ...]:
    """Return one source-restricted inbound rule per remotely reachable service.

    The rule carries the declared sources, so the firewall opens the service
    port to those networks only; a service without ``remoteAccess`` adds none.
    """
    rules: list[SourceRestrictedFirewallRule] = []
    for service in services:
        if service.server_id != server.resource_id or service.postgresql is None:
            continue
        remote_access = service.postgresql.remote_access
        if remote_access is None:
            continue
        if any(source.is_unrestricted for source in remote_access.allowed_sources):
            message = (
                "PostgreSQL remoteAccess refuses a source of every address: "
                f"{service.resource_id.value}"
            )
            raise ValueError(message)
        rules.append(
            SourceRestrictedFirewallRule(
                port=service.bind.port,
                protocol=NetworkProtocol.TCP,
                sources=remote_access.allowed_sources,
                description=f"{service.resource_id.value} remoteAccess",
            )
        )
    return tuple(rules)


def _configuration_requirement(
    content: Mapping[str, object],
) -> ConfigurationFileRequirement:
    raw_owner = content.get("owner")
    raw_group = content.get("group")
    raw_mode = content.get("mode")
    raw_sha256 = content.get("sha256")
    return ConfigurationFileRequirement(
        path=AbsolutePath.from_boundary(content.get("path")),
        capture=ConfigCapture.from_boundary(content.get("capture")),
        owner=LinuxUser.from_boundary(raw_owner) if raw_owner is not None else None,
        group=LinuxUser.from_boundary(raw_group) if raw_group is not None else None,
        mode=FileMode.from_boundary(raw_mode) if raw_mode is not None else None,
        sha256=(
            Sha256Digest.from_boundary(raw_sha256) if raw_sha256 is not None else None
        ),
    )


def _mapping(content: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = content.get(key)
    if not isinstance(value, dict) or not all(
        isinstance(child_key, str) for child_key in value
    ):
        message = f"validated field {key!r} is not an object"
        raise TypeError(message)
    return cast("Mapping[str, object]", value)


def _string(content: Mapping[str, object], key: str) -> str:
    value = content.get(key)
    if not isinstance(value, str):
        message = f"validated field {key!r} is not a string"
        raise TypeError(message)
    return value


def _boundary_mapping(value: object, concept: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        message = f"validated {concept} is not an object"
        raise TypeError(message)
    return cast("Mapping[str, object]", value)


def _resource_ids(value: object, concept: str) -> tuple[ResourceId, ...]:
    if not isinstance(value, list):
        message = f"validated {concept} is not a list"
        raise TypeError(message)
    return tuple(ResourceId.from_boundary(item) for item in value)


def _list(value: object, concept: str) -> tuple[object, ...]:
    if not isinstance(value, list):
        message = f"validated {concept} is not a list"
        raise TypeError(message)
    return tuple(value)


def _mapping_list(value: object, concept: str) -> tuple[Mapping[str, object], ...]:
    items = _list(value, concept)
    mappings: list[Mapping[str, object]] = []
    for item in items:
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            message = f"validated {concept} contains a non-object item"
            raise TypeError(message)
        mappings.append(cast("Mapping[str, object]", item))
    return tuple(mappings)


def _labels(value: object) -> tuple[ServerLabel, ...]:
    if not isinstance(value, dict):
        message = "validated server labels are not an object"
        raise TypeError(message)
    labels: list[ServerLabel] = []
    for key, label_value in value.items():
        if not isinstance(label_value, str):
            message = f"validated server label {key!r} is not a string"
            raise TypeError(message)
        labels.append(
            ServerLabel(
                key=ResourceId.from_boundary(key),
                value=label_value,
            )
        )
    return tuple(sorted(labels, key=lambda label: label.key.value))


def _boolean(value: object, concept: str) -> bool:
    if not isinstance(value, bool):
        message = f"validated {concept} is not a boolean"
        raise TypeError(message)
    return value
