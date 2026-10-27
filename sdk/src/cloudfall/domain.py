"""Strict domain values used by the Cloudfall state reader."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address as StandardIPv4Address
from ipaddress import IPv6Network as StandardIPv6Network
from ipaddress import ip_address, ip_network
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_RESOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_RESOURCE_ID_MAX_LENGTH = 63
_HOST_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_LINUX_USER_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_PACKAGE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,199}$")
_SERVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9@_.:-]+\.service$")
_SYSTEMD_UNIT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9@_.:-]+\.(?:service|timer)$")
_BUILD_TOOL_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_BUILD_TOOL_VERSION_PATTERN = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,9}){0,7}")
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
BUILD_STEP_DEFAULT_TIMEOUT_SECONDS = 900
BUILD_STEP_MAXIMUM_TIMEOUT_SECONDS = 3600
_FILE_MODE_PATTERN = re.compile(r"^0[0-7]{3}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_MAX_HOSTNAME_LENGTH = 253
_MAX_TCP_PORT = 65535
_MIN_HTTP_STATUS = 100
_MAX_HTTP_STATUS = 599
_SSH_PUBLIC_KEY_ALGORITHMS = frozenset({"ssh-ed25519", "ssh-rsa"})
_SSH_PUBLIC_KEY_MIN_PARTS = 2
_SSH_WIRE_PREFIX_BYTES = 4


@dataclass(frozen=True, slots=True)
class ResourceId:
    """Stable identifier for a platform resource."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the identifier invariant at construction time."""
        if not _RESOURCE_ID_PATTERN.fullmatch(self.value):
            message = f"invalid resource id: {self.value!r}"
            raise ValueError(message)
        if len(self.value) > _RESOURCE_ID_MAX_LENGTH:
            message = f"resource id exceeds 63 characters: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ResourceId:
        """Coerce a boundary value while keeping internal APIs strictly typed."""
        if not isinstance(value, str):
            message = f"resource id must be a string, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value)

    def __str__(self) -> str:
        """Return the serialized identifier."""
        return self.value


class ResourceKind(StrEnum):
    """Resource kinds supported by the v1 state contract."""

    SERVER = "Server"
    HOST_PROFILE = "ServerType"
    APPLICATION = "Application"
    COMPONENT = "Component"
    DOMAIN = "Domain"
    SSH_PUBLIC_KEY = "SshPublicKey"
    LOGGING_STACK = "LoggingStack"
    SERVICE = "Service"
    ALERT_RULE = "AlertRule"
    OPERATOR_POLICY = "OperatorPolicy"

    @classmethod
    def from_boundary(cls, value: object) -> ResourceKind:
        """Coerce a boundary value into a supported resource kind."""
        if not isinstance(value, str):
            message = f"resource kind must be a string, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value)

    @property
    def directory(self) -> str:
        """Return the project directory that holds resources of this kind."""
        return _RESOURCE_DIRECTORIES[self]


_RESOURCE_DIRECTORIES: Mapping[ResourceKind, str] = {
    ResourceKind.SSH_PUBLIC_KEY: "ssh-public-keys",
    ResourceKind.HOST_PROFILE: "server-types",
    ResourceKind.SERVER: "servers",
    ResourceKind.APPLICATION: "applications",
    ResourceKind.COMPONENT: "components",
    ResourceKind.SERVICE: "services",
    ResourceKind.DOMAIN: "domains",
    ResourceKind.LOGGING_STACK: "logging-stacks",
    ResourceKind.ALERT_RULE: "alert-rules",
    ResourceKind.OPERATOR_POLICY: "operator-policies",
}


class SshPublicKeyLifecycle(StrEnum):
    """Lifecycle of a declarative SSH public key."""

    ACTIVE = "active"
    REVOKED = "revoked"

    @classmethod
    def from_boundary(cls, value: object) -> SshPublicKeyLifecycle:
        """Coerce a boundary value into an SSH-key lifecycle."""
        return cls(_required_string(value, "SSH public-key lifecycle"))


@dataclass(frozen=True, slots=True)
class OpenSshPublicKey:
    """Validated, single-line OpenSSH public key."""

    value: str

    def __post_init__(self) -> None:
        """Validate syntax, base64 encoding, and embedded algorithm."""
        if "\n" in self.value or "\r" in self.value:
            message = "OpenSSH public key must be a single line"
            raise ValueError(message)
        parts = self.value.split(maxsplit=2)
        if len(parts) < _SSH_PUBLIC_KEY_MIN_PARTS:
            message = "OpenSSH public key must include algorithm and key data"
            raise ValueError(message)
        algorithm, encoded = parts[0], parts[1]
        if algorithm not in _SSH_PUBLIC_KEY_ALGORITHMS:
            message = f"unsupported OpenSSH public-key algorithm: {algorithm!r}"
            raise ValueError(message)
        padded = encoded + "=" * (-len(encoded) % 4)
        try:
            decoded = base64.b64decode(padded, validate=True)
        except binascii.Error as error:
            message = "OpenSSH public-key data is not valid base64"
            raise ValueError(message) from error
        if len(decoded) < _SSH_WIRE_PREFIX_BYTES:
            message = "OpenSSH public-key payload is truncated"
            raise ValueError(message)
        algorithm_length = int.from_bytes(
            decoded[:_SSH_WIRE_PREFIX_BYTES], byteorder="big"
        )
        algorithm_end = _SSH_WIRE_PREFIX_BYTES + algorithm_length
        try:
            embedded_algorithm = decoded[_SSH_WIRE_PREFIX_BYTES:algorithm_end].decode(
                "ascii"
            )
        except UnicodeDecodeError as error:
            message = "OpenSSH public-key payload has an invalid algorithm"
            raise ValueError(message) from error
        if algorithm_end > len(decoded) or embedded_algorithm != algorithm:
            message = "OpenSSH public-key algorithm does not match its payload"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> OpenSshPublicKey:
        """Coerce a boundary value into an OpenSSH public key."""
        return cls(_required_string(value, "OpenSSH public key"))

    @property
    def algorithm(self) -> str:
        """Return the validated OpenSSH algorithm name."""
        return self.value.split(maxsplit=1)[0]


@dataclass(frozen=True, slots=True)
class Hostname:
    """Validated DNS hostname."""

    value: str

    def __post_init__(self) -> None:
        """Enforce DNS hostname syntax."""
        candidate = self.value.removesuffix(".")
        labels = candidate.split(".")
        if (
            not candidate
            or len(candidate) > _MAX_HOSTNAME_LENGTH
            or any(not _HOST_LABEL_PATTERN.fullmatch(label) for label in labels)
        ):
            message = f"invalid hostname: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> Hostname:
        """Coerce a boundary value into a hostname."""
        return cls(_required_string(value, "hostname"))


@dataclass(frozen=True, slots=True)
class ConnectionAddress:
    """Validated hostname or IP address used to connect to a server."""

    value: str

    def __post_init__(self) -> None:
        """Enforce hostname-or-IP syntax."""
        try:
            ip_address(self.value)
        except ValueError:
            Hostname(self.value)

    @classmethod
    def from_boundary(cls, value: object) -> ConnectionAddress:
        """Coerce a boundary value into a connection address."""
        return cls(_required_string(value, "connection address"))


@dataclass(frozen=True, slots=True)
class Ipv4Address:
    """Validated IPv4 address assigned to a server."""

    value: str

    def __post_init__(self) -> None:
        """Enforce canonical IPv4 syntax."""
        parsed = StandardIPv4Address(self.value)
        if str(parsed) != self.value:
            message = f"IPv4 address is not canonical: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> Ipv4Address:
        """Coerce a boundary value into an IPv4 address."""
        return cls(_required_string(value, "IPv4 address"))


@dataclass(frozen=True, slots=True)
class IpAddress:
    """Validated canonical IPv4 or IPv6 address."""

    value: str

    def __post_init__(self) -> None:
        """Enforce canonical IP address notation."""
        parsed = ip_address(self.value)
        if str(parsed) != self.value:
            message = f"IP address is not canonical: {self.value!r}"
            raise ValueError(message)

    @property
    def is_unspecified(self) -> bool:
        """Return whether the address is the wildcard of its family."""
        return ip_address(self.value).is_unspecified

    @classmethod
    def from_boundary(cls, value: object) -> IpAddress:
        """Coerce a boundary value into an IP address."""
        return cls(_required_string(value, "IP address"))


@dataclass(frozen=True, slots=True)
class Ipv6NetworkCidr:
    """Validated canonical IPv6 network allocation."""

    value: str

    def __post_init__(self) -> None:
        """Enforce a canonical IPv6 network and prefix."""
        parsed = StandardIPv6Network(self.value, strict=True)
        if str(parsed) != self.value:
            message = f"IPv6 network is not canonical: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> Ipv6NetworkCidr:
        """Coerce a boundary value into an IPv6 network allocation."""
        return cls(_required_string(value, "IPv6 network"))


@dataclass(frozen=True, slots=True)
class ProviderServerId:
    """Opaque server identifier assigned by an infrastructure provider."""

    value: str

    def __post_init__(self) -> None:
        """Reject empty and unsafe provider identifiers."""
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", self.value):
            message = f"invalid provider server id: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ProviderServerId:
        """Coerce a boundary value into a provider server identifier."""
        return cls(_required_string(value, "provider server id"))


@dataclass(frozen=True, slots=True)
class DatacenterCode:
    """Validated provider datacenter code."""

    value: str

    def __post_init__(self) -> None:
        """Enforce a short provider location-code syntax."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.value):
            message = f"invalid datacenter code: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> DatacenterCode:
        """Coerce a boundary value into a datacenter code."""
        return cls(_required_string(value, "datacenter code"))


@dataclass(frozen=True, slots=True)
class LinuxUser:
    """Validated Linux account name."""

    value: str

    def __post_init__(self) -> None:
        """Enforce Linux account naming rules used by Cloudfall."""
        if not _LINUX_USER_PATTERN.fullmatch(self.value):
            message = f"invalid Linux user: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> LinuxUser:
        """Coerce a boundary value into a Linux account name."""
        return cls(_required_string(value, "Linux user"))


@dataclass(frozen=True, slots=True)
class TcpPort:
    """Validated TCP port."""

    value: int

    def __post_init__(self) -> None:
        """Enforce the usable TCP port range."""
        if isinstance(self.value, bool) or not 1 <= self.value <= _MAX_TCP_PORT:
            message = f"invalid TCP port: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> TcpPort:
        """Coerce a boundary value into a TCP port."""
        if isinstance(value, bool) or not isinstance(value, int):
            message = f"TCP port must be an integer, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value)


@dataclass(frozen=True, slots=True)
class AbsolutePath:
    """Validated absolute POSIX path."""

    value: str

    def __post_init__(self) -> None:
        """Reject relative, parent-traversing, and NUL-containing paths."""
        path = PurePosixPath(self.value)
        if (
            not self.value.startswith("/")
            or "\x00" in self.value
            or "//" in self.value
            or ".." in path.parts
        ):
            message = f"invalid absolute path: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> AbsolutePath:
        """Coerce a boundary value into an absolute path."""
        return cls(_required_string(value, "absolute path"))


class ServerLifecycle(StrEnum):
    """Operational lifecycle of a managed server."""

    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    RETIRED = "retired"

    @classmethod
    def from_boundary(cls, value: object) -> ServerLifecycle:
        """Coerce a boundary value into a server lifecycle."""
        return cls(_required_string(value, "server lifecycle"))


class DeploymentApproval(StrEnum):
    """Deployment approval policy owned by an application."""

    AUTOMATIC = "automatic"
    MANUAL = "manual"

    @classmethod
    def from_boundary(cls, value: object) -> DeploymentApproval:
        """Coerce a boundary value into an approval policy."""
        return cls(_required_string(value, "deployment approval"))


class HttpScheme(StrEnum):
    """Supported HTTP transport schemes."""

    HTTP = "http"
    HTTPS = "https"

    @classmethod
    def from_boundary(cls, value: object) -> HttpScheme:
        """Coerce a boundary value into an HTTP scheme."""
        return cls(_required_string(value, "HTTP scheme"))


class DnsMode(StrEnum):
    """How public DNS exposes an Cloudfall domain."""

    DNS_ONLY = "dns-only"
    PROXIED = "proxied"

    @classmethod
    def from_boundary(cls, value: object) -> DnsMode:
        """Coerce a boundary value into a DNS exposure mode."""
        return cls(_required_string(value, "DNS mode"))


class TlsMode(StrEnum):
    """Desired TLS behavior for a public domain."""

    DISABLED = "disabled"
    REQUIRED = "required"

    @classmethod
    def from_boundary(cls, value: object) -> TlsMode:
        """Coerce a boundary value into a TLS mode."""
        return cls(_required_string(value, "TLS mode"))


class LoggingMigrationMode(StrEnum):
    """Supported transition mode for the replacement logging path."""

    PARALLEL = "parallel"

    @classmethod
    def from_boundary(cls, value: object) -> LoggingMigrationMode:
        """Coerce a boundary value into a logging migration mode."""
        return cls(_required_string(value, "logging migration mode"))


class LoggingStorageType(StrEnum):
    """Storage backend supported by the first logging implementation slice."""

    FILESYSTEM = "filesystem"

    @classmethod
    def from_boundary(cls, value: object) -> LoggingStorageType:
        """Coerce a boundary value into a logging storage type."""
        return cls(_required_string(value, "logging storage type"))


@dataclass(frozen=True, slots=True)
class HttpStatusCode:
    """Validated HTTP response status code."""

    value: int

    def __post_init__(self) -> None:
        """Require the standard three-digit HTTP status range."""
        if (
            isinstance(self.value, bool)
            or not _MIN_HTTP_STATUS <= self.value <= _MAX_HTTP_STATUS
        ):
            message = f"invalid HTTP status code: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> HttpStatusCode:
        """Coerce a boundary value into an HTTP status code."""
        if isinstance(value, bool) or not isinstance(value, int):
            message = f"HTTP status must be an integer, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value)


@dataclass(frozen=True, slots=True)
class PackageName:
    """Validated Debian package name."""

    value: str

    def __post_init__(self) -> None:
        """Enforce Debian package naming syntax used by Cloudfall."""
        if not _PACKAGE_NAME_PATTERN.fullmatch(self.value):
            message = f"invalid package name: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> PackageName:
        """Coerce a boundary value into a package name."""
        return cls(_required_string(value, "package name"))


@dataclass(frozen=True, slots=True)
class ServiceName:
    """Validated systemd service-unit name."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the service-unit naming syntax accepted by state."""
        if not _SERVICE_NAME_PATTERN.fullmatch(self.value):
            message = f"invalid service name: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ServiceName:
        """Coerce a boundary value into a service name."""
        return cls(_required_string(value, "service name"))


@dataclass(frozen=True, slots=True)
class SystemdUnitName:
    """Validated systemd service- or timer-unit name."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the unit naming syntax accepted by state."""
        if not _SYSTEMD_UNIT_NAME_PATTERN.fullmatch(self.value):
            message = f"invalid systemd unit name: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> SystemdUnitName:
        """Coerce a boundary value into a systemd unit name."""
        return cls(_required_string(value, "systemd unit name"))

    @property
    def is_timer(self) -> bool:
        """Return whether the unit is a timer."""
        return self.value.endswith(".timer")


class NetworkProtocol(StrEnum):
    """Transport protocols supported by firewall rules."""

    TCP = "tcp"
    UDP = "udp"

    @classmethod
    def from_boundary(cls, value: object) -> NetworkProtocol:
        """Coerce a boundary value into a network protocol."""
        return cls(_required_string(value, "network protocol"))


class FirewallPolicy(StrEnum):
    """Inbound firewall policies supported by Cloudfall v1."""

    DEFAULT_DENY = "default-deny"

    @classmethod
    def from_boundary(cls, value: object) -> FirewallPolicy:
        """Coerce a boundary value into a firewall policy."""
        return cls(_required_string(value, "firewall policy"))


class ServiceKind(StrEnum):
    """Infrastructure service kinds supported by Cloudfall v1."""

    POSTGRESQL = "postgresql"
    REDIS = "redis"

    @classmethod
    def from_boundary(cls, value: object) -> ServiceKind:
        """Coerce a boundary value into a service kind."""
        return cls(_required_string(value, "service kind"))


class HealthCheckType(StrEnum):
    """Component health-check kinds supported by the v1 contract."""

    HTTP = "http"
    NONE = "none"

    @classmethod
    def from_boundary(cls, value: object) -> HealthCheckType:
        """Coerce a boundary value into a health-check kind."""
        return cls(_required_string(value, "health check type"))


class RuntimeType(StrEnum):
    """Component runtimes supported by the v1 contract."""

    PYTHON = "python"
    NODE = "node"
    PM2 = "pm2"

    @classmethod
    def from_boundary(cls, value: object) -> RuntimeType:
        """Coerce a boundary value into a runtime type."""
        return cls(_required_string(value, "runtime type"))


class RuntimePackageManager(StrEnum):
    """Package managers supported by the v1 contract."""

    UV = "uv"
    NPM = "npm"
    PNPM = "pnpm"
    YARN = "yarn"

    @classmethod
    def from_boundary(cls, value: object) -> RuntimePackageManager:
        """Coerce a boundary value into a package manager."""
        return cls(_required_string(value, "runtime package manager"))


@dataclass(frozen=True, slots=True)
class RuntimeVersion:
    """Opaque runtime version requested by a component."""

    value: str

    def __post_init__(self) -> None:
        """Reject empty and multi-line versions."""
        if not self.value or "\n" in self.value or "\r" in self.value:
            message = f"invalid runtime version: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> RuntimeVersion:
        """Coerce a boundary value into a runtime version."""
        return cls(_required_string(value, "runtime version"))


@dataclass(frozen=True, slots=True)
class RepositoryUrl:
    """Validated HTTPS, SSH, or local-file git repository URL."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the transport allowlist and reject control characters."""
        if not re.fullmatch(r"(?:https|ssh|file)://[^\s\x00]+", self.value):
            message = f"invalid repository URL: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> RepositoryUrl:
        """Coerce a boundary value into a repository URL."""
        return cls(_required_string(value, "repository URL"))


@dataclass(frozen=True, slots=True)
class ServiceCommand:
    """Validated argv-style service command."""

    value: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject empty commands and unit-file control characters."""
        if not self.value:
            message = "service command must not be empty"
            raise ValueError(message)
        for argument in self.value:
            if not argument or any(
                character in argument for character in ("\n", "\r", "\x00", '"')
            ):
                message = f"invalid service command argument: {argument!r}"
                raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ServiceCommand:
        """Coerce a boundary value into a service command."""
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            message = "service command must be a list of strings"
            raise TypeError(message)
        return cls(tuple(value))


@dataclass(frozen=True, slots=True)
class RelativeRepositoryPath:
    """Path inside a component's packaged root: relative, never climbing out."""

    value: str

    def __post_init__(self) -> None:
        """Reject absolute paths, ``..`` segments, and control characters."""
        if (
            not self.value
            or _CONTROL_CHARACTER_PATTERN.search(self.value)
            or self.value.startswith("/")
            or ".." in self.value.split("/")
        ):
            message = f"invalid relative repository path: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> RelativeRepositoryPath:
        """Coerce a boundary value into a relative repository path."""
        return cls(_required_string(value, "relative repository path"))


@dataclass(frozen=True, slots=True)
class CommandArgv:
    """Validated argv of a command run directly, not through a unit file."""

    value: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject empty commands, empty arguments, and NUL bytes."""
        if not self.value:
            message = "command must not be empty"
            raise ValueError(message)
        for argument in self.value:
            if not argument or "\x00" in argument:
                message = f"invalid command argument: {argument!r}"
                raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> CommandArgv:
        """Coerce a boundary value into a command argv."""
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            message = "command must be a list of strings"
            raise TypeError(message)
        return cls(tuple(value))


@dataclass(frozen=True, slots=True)
class RelativeDirectory:
    """A relative path that stays inside the directory it is resolved against."""

    value: str

    def __post_init__(self) -> None:
        """Reject absolute paths, ``..`` segments, and control characters."""
        if (
            not self.value
            or self.value.startswith("/")
            or ".." in self.value.split("/")
            or any(not character.isprintable() for character in self.value)
        ):
            message = f"invalid relative directory: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> RelativeDirectory:
        """Coerce a boundary value into a relative directory."""
        return cls(_required_string(value, "relative directory"))


_RELEASE_TIMEOUT_MINIMUM = 1
_RELEASE_TIMEOUT_MAXIMUM = 1800
_RELEASE_TIMEOUT_DEFAULT = 300


@dataclass(frozen=True, slots=True)
class ReleaseCommandTimeout:
    """How long a release command may run before it is stopped, in seconds."""

    value: int

    def __post_init__(self) -> None:
        """Keep the bound well under the limit on a whole playbook run."""
        if isinstance(self.value, bool) or not (
            _RELEASE_TIMEOUT_MINIMUM <= self.value <= _RELEASE_TIMEOUT_MAXIMUM
        ):
            message = f"invalid release command timeout: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ReleaseCommandTimeout:
        """Coerce a boundary value, defaulting an absent one."""
        if value is None:
            return cls(_RELEASE_TIMEOUT_DEFAULT)
        if isinstance(value, bool) or not isinstance(value, int):
            message = (
                f"release command timeout must be an integer, got "
                f"{type(value).__name__}"
            )
            raise TypeError(message)
        return cls(value)


@dataclass(frozen=True, slots=True)
class BuildToolName:
    """Executable a component build step runs, looked up on ``PATH``."""

    value: str

    def __post_init__(self) -> None:
        """Accept a bare executable name only, never a path."""
        if not _BUILD_TOOL_NAME_PATTERN.fullmatch(self.value):
            message = f"invalid build tool name: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> BuildToolName:
        """Coerce a boundary value into a build tool name."""
        return cls(_required_string(value, "build tool name"))


@dataclass(frozen=True, slots=True)
class BuildToolVersion:
    """Minimum dotted numeric version of a build tool."""

    value: str

    def __post_init__(self) -> None:
        """Accept one to eight dotted numeric parts."""
        if not _BUILD_TOOL_VERSION_PATTERN.fullmatch(self.value):
            message = f"invalid build tool version: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> BuildToolVersion:
        """Coerce a boundary value into a build tool version."""
        return cls(_required_string(value, "build tool version"))


@dataclass(frozen=True, slots=True)
class BuildCommand:
    """Argv list of one component build step, never a shell string."""

    value: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject empty argv lists, empty arguments, and NUL bytes."""
        if not self.value:
            message = "build command must not be empty"
            raise ValueError(message)
        for argument in self.value:
            if not argument or "\x00" in argument:
                message = f"invalid build command argument: {argument!r}"
                raise ValueError(message)

    @property
    def executable(self) -> BuildToolName:
        """The tool the step runs: ``command[0]``."""
        return BuildToolName(self.value[0])

    @classmethod
    def from_boundary(cls, value: object) -> BuildCommand:
        """Coerce a boundary argv list into a build command."""
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            message = "build command must be a list of strings"
            raise TypeError(message)
        return cls(tuple(value))


@dataclass(frozen=True, slots=True)
class BuildTimeoutSeconds:
    """Time limit of one build step: 1 to 3600 seconds, 900 by default."""

    value: int = BUILD_STEP_DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """Reject booleans and values outside 1..3600."""
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int)
            or not 1 <= self.value <= BUILD_STEP_MAXIMUM_TIMEOUT_SECONDS
        ):
            message = f"invalid build step timeout: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> BuildTimeoutSeconds:
        """Coerce a boundary value, defaulting when it is absent."""
        if value is None:
            return cls()
        if not isinstance(value, int) or isinstance(value, bool):
            message = "build step timeout must be an integer"
            raise TypeError(message)
        return cls(value)


@dataclass(frozen=True, slots=True)
class PostgresDatabaseName:
    """Validated PostgreSQL database name safe for quoted identifiers."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the conservative database naming accepted by state."""
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.value):
            message = f"invalid PostgreSQL database name: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> PostgresDatabaseName:
        """Coerce a boundary value into a database name."""
        return cls(_required_string(value, "PostgreSQL database name"))


@dataclass(frozen=True, slots=True)
class PostgresRoleName:
    """Validated PostgreSQL role name safe for quoted identifiers."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the database-name pattern state uses for roles."""
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.value):
            message = f"invalid PostgreSQL role name: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> PostgresRoleName:
        """Coerce a boundary value into a role name."""
        return cls(_required_string(value, "PostgreSQL role name"))


@dataclass(frozen=True, slots=True)
class SecretKeyName:
    """Validated key of one ``KEY=VALUE`` line in a secret source."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the dotenv key syntax the secrets reader accepts."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,254}", self.value):
            message = f"invalid secret key name: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> SecretKeyName:
        """Coerce a boundary value into a secret key name."""
        return cls(_required_string(value, "secret key name"))


@dataclass(frozen=True, slots=True)
class IpNetworkCidr:
    """Validated canonical IPv4 or IPv6 network in CIDR notation."""

    value: str

    def __post_init__(self) -> None:
        """Enforce a canonical network with no host bits set."""
        parsed = ip_network(self.value, strict=True)
        if str(parsed) != self.value:
            message = f"IP network is not canonical: {self.value!r}"
            raise ValueError(message)

    @property
    def is_unrestricted(self) -> bool:
        """Return whether the network matches every address of its family."""
        return ip_network(self.value).prefixlen == 0

    @classmethod
    def from_boundary(cls, value: object) -> IpNetworkCidr:
        """Coerce a boundary value into an IP network."""
        return cls(_required_string(value, "IP network"))


@dataclass(frozen=True, slots=True)
class ReleaseId:
    """Validated component release identifier."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the timestamp-and-commit release naming contract."""
        if not re.fullmatch(
            r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{7,40}", self.value
        ):
            message = f"invalid release id: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ReleaseId:
        """Coerce a boundary value into a release identifier."""
        return cls(_required_string(value, "release id"))


@dataclass(frozen=True, slots=True)
class PostgresMajorVersion:
    """Validated PostgreSQL major version."""

    value: str

    def __post_init__(self) -> None:
        """Reject non-numeric major versions."""
        if not re.fullmatch(r"[0-9]{2}", self.value):
            message = f"invalid PostgreSQL major version: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> PostgresMajorVersion:
        """Coerce a boundary value into a PostgreSQL major version."""
        return cls(_required_string(value, "PostgreSQL major version"))


@dataclass(frozen=True, slots=True)
class SystemdCalendar:
    """Validated systemd OnCalendar expression safe for unit rendering."""

    value: str

    def __post_init__(self) -> None:
        """Reject expressions that could escape a unit-file directive."""
        if not re.fullmatch(r"[A-Za-z0-9 *:,.^~/-]{1,100}", self.value):
            message = f"invalid systemd calendar expression: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> SystemdCalendar:
        """Coerce a boundary value into a calendar expression."""
        return cls(_required_string(value, "systemd calendar expression"))


class SecretScope(StrEnum):
    """Ownership scope of one declared secret reference."""

    PLATFORM = "platform"
    APPLICATION = "application"
    COMPONENT = "component"

    @classmethod
    def from_boundary(cls, value: object) -> SecretScope:
        """Coerce a boundary value into a secret scope."""
        return cls(_required_string(value, "secret scope"))


class AlertSeverity(StrEnum):
    """Severity level attached to a declared alert rule."""

    WARNING = "warning"
    CRITICAL = "critical"

    @classmethod
    def from_boundary(cls, value: object) -> AlertSeverity:
        """Coerce a boundary value into an alert severity."""
        return cls(_required_string(value, "alert severity"))


@dataclass(frozen=True, slots=True)
class PromqlExpression:
    """Validated single-line PromQL expression safe for rule rendering."""

    value: str

    def __post_init__(self) -> None:
        """Reject control characters and unbounded expressions."""
        if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,1000}", self.value):
            message = f"invalid promql expression: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> PromqlExpression:
        """Coerce a boundary value into a PromQL expression."""
        return cls(_required_string(value, "promql expression"))


@dataclass(frozen=True, slots=True)
class AlertDuration:
    """Validated Prometheus for-duration guarding alert flappiness."""

    value: str

    def __post_init__(self) -> None:
        """Reject durations outside the simple seconds/minutes/hours form."""
        if not re.fullmatch(r"[0-9]{1,5}[smh]", self.value):
            message = f"invalid alert duration: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> AlertDuration:
        """Coerce a boundary value into an alert duration."""
        return cls(_required_string(value, "alert duration"))


@dataclass(frozen=True, slots=True)
class AlertSummary:
    """Validated single-line human summary rendered into annotations."""

    value: str

    def __post_init__(self) -> None:
        """Reject control characters and unbounded summaries."""
        if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,200}", self.value):
            message = f"invalid alert summary: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> AlertSummary:
        """Coerce a boundary value into an alert summary."""
        return cls(_required_string(value, "alert summary"))


@dataclass(frozen=True, slots=True)
class DayTime:
    """Validated HH:MM wall-clock time for policy windows."""

    value: str

    def __post_init__(self) -> None:
        """Reject values outside the strict 24-hour HH:MM form."""
        if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", self.value):
            message = f"invalid day time: {self.value!r}"
            raise ValueError(message)

    @property
    def minutes(self) -> int:
        """Return the minutes since midnight this time represents."""
        hours, minutes = self.value.split(":")
        return int(hours) * 60 + int(minutes)

    @classmethod
    def from_boundary(cls, value: object) -> DayTime:
        """Coerce a boundary value into a wall-clock time."""
        return cls(_required_string(value, "day time"))


@dataclass(frozen=True, slots=True)
class WebhookUrl:
    """Validated HTTP(S) webhook destination for alert delivery."""

    value: str

    def __post_init__(self) -> None:
        """Reject non-HTTP schemes, spaces, and control characters."""
        if not re.fullmatch(
            r"https?://[^\x00-\x1f\x7f ]{1,490}", self.value
        ):
            message = f"invalid webhook url: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> WebhookUrl:
        """Coerce a boundary value into a webhook URL."""
        return cls(_required_string(value, "webhook url"))


@dataclass(frozen=True, slots=True)
class SmtpSmarthost:
    """Validated host:port SMTP relay address."""

    value: str

    def __post_init__(self) -> None:
        """Reject smarthosts outside the strict host:port form."""
        if not re.fullmatch(r"[a-z0-9.-]{1,253}:[0-9]{1,5}", self.value):
            message = f"invalid smtp smarthost: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> SmtpSmarthost:
        """Coerce a boundary value into an SMTP smarthost."""
        return cls(_required_string(value, "smtp smarthost"))


@dataclass(frozen=True, slots=True)
class EmailAddress:
    """Validated single email address for alert delivery."""

    value: str

    def __post_init__(self) -> None:
        """Reject addresses without a local part and domain."""
        if not re.fullmatch(r"[^@\s]{1,100}@[^@\s]{1,100}", self.value):
            message = f"invalid email address: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> EmailAddress:
        """Coerce a boundary value into an email address."""
        return cls(_required_string(value, "email address"))


@dataclass(frozen=True, slots=True)
class SmtpUsername:
    """Validated SMTP authentication username."""

    value: str

    def __post_init__(self) -> None:
        """Reject control characters and unbounded usernames."""
        if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,200}", self.value):
            message = f"invalid smtp username: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> SmtpUsername:
        """Coerce a boundary value into an SMTP username."""
        return cls(_required_string(value, "smtp username"))


@dataclass(frozen=True, slots=True)
class FileMode:
    """Validated four-digit POSIX file mode."""

    value: str

    def __post_init__(self) -> None:
        """Enforce an explicit octal file mode."""
        if not _FILE_MODE_PATTERN.fullmatch(self.value):
            message = f"invalid file mode: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> FileMode:
        """Coerce a boundary value into a file mode."""
        return cls(_required_string(value, "file mode"))


@dataclass(frozen=True, slots=True)
class Sha256Digest:
    """Validated lowercase SHA-256 digest."""

    value: str

    def __post_init__(self) -> None:
        """Enforce SHA-256 hexadecimal length and alphabet."""
        if not _SHA256_PATTERN.fullmatch(self.value):
            message = f"invalid SHA-256 digest: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> Sha256Digest:
        """Coerce a boundary value into a SHA-256 digest."""
        return cls(_required_string(value, "SHA-256 digest"))


@dataclass(frozen=True, slots=True)
class ByteSize:
    """Strictly positive byte-size requirement."""

    value: int

    def __post_init__(self) -> None:
        """Reject booleans, zero, and negative sizes."""
        if isinstance(self.value, bool) or self.value < 1:
            message = f"invalid positive byte size: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ByteSize:
        """Coerce a boundary value into a positive byte size."""
        if isinstance(value, bool) or not isinstance(value, int):
            message = f"byte size must be an integer, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value)


@dataclass(frozen=True, slots=True)
class PositiveCount:
    """Strictly positive count value."""

    value: int

    def __post_init__(self) -> None:
        """Reject booleans, zero, and negative counts."""
        if isinstance(self.value, bool) or self.value < 1:
            message = f"invalid positive count: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> PositiveCount:
        """Coerce a boundary value into a positive count."""
        if isinstance(value, bool) or not isinstance(value, int):
            message = f"count must be an integer, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value)


class RaidLevel(StrEnum):
    """Supported Linux software RAID levels."""

    RAID0 = "raid0"
    RAID1 = "raid1"
    RAID5 = "raid5"
    RAID6 = "raid6"
    RAID10 = "raid10"

    @classmethod
    def from_boundary(cls, value: object) -> RaidLevel:
        """Coerce a boundary value into a RAID level."""
        return cls(_required_string(value, "RAID level"))


class ConfigCapture(StrEnum):
    """Permitted non-secret configuration evidence levels."""

    METADATA = "metadata"
    HASH = "hash"

    @classmethod
    def from_boundary(cls, value: object) -> ConfigCapture:
        """Coerce a boundary value into a capture level."""
        return cls(_required_string(value, "configuration capture"))


class RequiredServiceState(StrEnum):
    """Desired runtime state for a systemd service."""

    RUNNING = "running"
    STOPPED = "stopped"

    @classmethod
    def from_boundary(cls, value: object) -> RequiredServiceState:
        """Coerce a boundary value into a service state."""
        return cls(_required_string(value, "service state"))


class RequiredServiceStatus(StrEnum):
    """Desired enablement status for a systemd service."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    STATIC = "static"
    INDIRECT = "indirect"

    @classmethod
    def from_boundary(cls, value: object) -> RequiredServiceStatus:
        """Coerce a boundary value into a service status."""
        return cls(_required_string(value, "service status"))


class OperatingSystemDistribution(StrEnum):
    """Operating-system distributions supported by Cloudfall v1."""

    DEBIAN = "Debian"

    @classmethod
    def from_boundary(cls, value: object) -> OperatingSystemDistribution:
        """Coerce a boundary value into a supported distribution."""
        return cls(_required_string(value, "operating-system distribution"))


class ServiceManager(StrEnum):
    """Service managers supported by Cloudfall v1."""

    SYSTEMD = "systemd"

    @classmethod
    def from_boundary(cls, value: object) -> ServiceManager:
        """Coerce a boundary value into a supported service manager."""
        return cls(_required_string(value, "service manager"))


@dataclass(frozen=True, slots=True)
class OperatingSystemMajorVersion:
    """Validated numeric operating-system major version."""

    value: str

    def __post_init__(self) -> None:
        """Reject non-numeric major versions."""
        if not self.value.isdigit():
            message = f"invalid operating-system major version: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> OperatingSystemMajorVersion:
        """Coerce a boundary value into an OS major version."""
        return cls(_required_string(value, "operating-system major version"))


@dataclass(frozen=True, slots=True)
class FilesystemName:
    """Validated filesystem type name."""

    value: str

    @classmethod
    def from_boundary(cls, value: object) -> FilesystemName:
        """Coerce a boundary value into a filesystem name."""
        return cls(_required_string(value, "filesystem name"))


@dataclass(frozen=True, slots=True)
class PackageVersion:
    """Opaque package-manager version string."""

    value: str

    @classmethod
    def from_boundary(cls, value: object) -> PackageVersion:
        """Coerce a boundary value into a package version."""
        return cls(_required_string(value, "package version"))


@dataclass(frozen=True, slots=True)
class ResourceKey:
    """Typed lookup key for the state index."""

    kind: ResourceKind
    resource_id: ResourceId


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """Location of a YAML document on disk."""

    path: Path
    document_number: int

    def display(self) -> str:
        """Return a stable human-readable source reference."""
        return f"{self.path}#{self.document_number}"


@dataclass(frozen=True, slots=True)
class RawDocument:
    """A resource document before validation, from a file or from a reader."""

    content: Mapping[str, object]
    source: SourceLocation


@dataclass(frozen=True, slots=True)
class ResourceDocument:
    """A schema-validated resource document."""

    key: ResourceKey
    content: Mapping[str, object]
    source: SourceLocation


def _required_string(value: object, concept: str) -> str:
    if not isinstance(value, str) or not value:
        message = f"{concept} must be a non-empty string"
        raise TypeError(message)
    return value
