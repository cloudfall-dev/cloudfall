"""Write fleet resources into a project.

``cloudfall add`` turns the first minutes after ``cloudfall init`` into three
commands instead of three hand-written files: an SSH public key read from
disk, a server type from the bundled Debian baseline, and a server that
references it. Every document is schema-validated before it is written, the
whole project is validated afterwards, and a failed validation removes what
was written so the project never holds a resource it would reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from typing import TYPE_CHECKING

import yaml

from cloudfall.domain import (
    ConnectionAddress,
    Hostname,
    LinuxUser,
    OpenSshPublicKey,
    ResourceId,
    ResourceKind,
    TcpPort,
)
from cloudfall.validation import ConfigValidationError, SchemaCatalog, validate_config

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

API_VERSION = "cloudfall/v1"

BASELINE_PACKAGES: tuple[str, ...] = (
    "acl",
    "ca-certificates",
    "curl",
    "git",
    "rsync",
    "unzip",
)
"""Packages the Cloudfall baseline installs on every managed host."""

BASELINE_ROOT_MINIMUM_BYTES = 20_000_000_000
"""Smallest root filesystem the baseline server type accepts (20 GB)."""

ERROR_KEY_FILE_MISSING = "ssh_key_file_missing"
ERROR_KEY_FILE_INVALID = "ssh_key_file_invalid"
ERROR_RESOURCE_EXISTS = "resource_exists"
ERROR_PROJECT_WRITE_FAILED = "project_write_failed"


class AuthoringError(RuntimeError):
    """Fail-fast authoring error with a stable machine-readable code."""

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


@dataclass(frozen=True, slots=True)
class AddedResource:
    """One resource file written into the project."""

    kind: ResourceKind
    resource_id: ResourceId
    path: str

    def as_dict(self) -> dict[str, object]:
        """Serialize for the CLI and agent envelope."""
        return {"kind": self.kind.value, "id": str(self.resource_id), "path": self.path}


@dataclass(frozen=True, slots=True)
class AddResult:
    """Outcome of one ``cloudfall add`` invocation."""

    project: Path
    added: tuple[AddedResource, ...]

    def as_dict(self) -> dict[str, object]:
        """Serialize for system boundaries."""
        return {
            "status": "ok",
            "project": str(self.project),
            "added": [resource.as_dict() for resource in self.added],
        }


@dataclass(frozen=True, slots=True)
class SshKeyOptions:
    """Inputs for ``cloudfall add ssh-key``."""

    key_path: Path
    owner: ResourceId
    environment: ResourceId
    resource_id: ResourceId | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ServerTypeOptions:
    """Inputs for ``cloudfall add server-type``."""

    resource_id: ResourceId
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ServerOptions:
    """Inputs for ``cloudfall add server``."""

    resource_id: ResourceId
    address: ConnectionAddress
    server_type: ResourceId
    environment: ResourceId
    ssh_user: LinuxUser
    ssh_port: TcpPort
    hostname: Hostname | None = None
    description: str | None = None


def read_public_key(key_path: Path) -> OpenSshPublicKey:
    """Read one OpenSSH public key from a file such as ``~/.ssh/id_ed25519.pub``."""
    if not key_path.is_file():
        message = f"SSH public key file does not exist: {key_path}"
        raise AuthoringError(ERROR_KEY_FILE_MISSING, message)
    lines = [
        line
        for line in key_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(lines) != 1:
        message = f"SSH public key file must hold exactly one key: {key_path}"
        raise AuthoringError(ERROR_KEY_FILE_INVALID, message)
    try:
        return OpenSshPublicKey.from_boundary(lines[0].strip())
    except (TypeError, ValueError) as error:
        message = f"{key_path}: {error}"
        raise AuthoringError(ERROR_KEY_FILE_INVALID, message) from error


def ssh_key_document(
    options: SshKeyOptions, key: OpenSshPublicKey
) -> dict[str, object]:
    """Build the ``SshPublicKey`` resource for one key."""
    resource_id = options.resource_id or options.owner
    description = options.description or f"SSH public key of {options.owner}"
    return {
        "apiVersion": API_VERSION,
        "kind": ResourceKind.SSH_PUBLIC_KEY.value,
        "metadata": {"id": str(resource_id), "description": description},
        "spec": {
            "owner": str(options.owner),
            "environment": str(options.environment),
            "publicKey": key.value,
            "lifecycle": "active",
        },
    }


def baseline_server_type_document(options: ServerTypeOptions) -> dict[str, object]:
    """Build the bundled baseline: a plain Debian 13 host without software RAID."""
    description = options.description or (
        "Debian 13 host with the Cloudfall baseline and no software RAID"
    )
    return {
        "apiVersion": API_VERSION,
        "kind": ResourceKind.HOST_PROFILE.value,
        "metadata": {"id": str(options.resource_id), "description": description},
        "spec": {
            "os": {
                "distribution": "Debian",
                "versions": ["13"],
                "serviceManager": "systemd",
            },
            "storage": {
                "mounts": [
                    {
                        "path": "/",
                        "filesystem": "ext4",
                        "minimumBytes": BASELINE_ROOT_MINIMUM_BYTES,
                    }
                ]
            },
            "packages": {
                "required": [{"name": name} for name in BASELINE_PACKAGES],
                "forbidden": ["telnetd"],
            },
            "services": {
                "required": [
                    {"name": "ssh.service", "state": "running", "status": "enabled"},
                    {
                        "name": "apt-daily.timer",
                        "state": "running",
                        "status": "enabled",
                    },
                ]
            },
            "firewall": {
                "policy": "default-deny",
                "allowedInbound": [
                    {"port": 22, "protocol": "tcp", "description": "ssh"},
                    {"port": 80, "protocol": "tcp", "description": "http"},
                    {"port": 443, "protocol": "tcp", "description": "https"},
                ],
            },
            "configuration": {
                "files": [
                    {
                        "path": "/etc/ssh/sshd_config",
                        "capture": "hash",
                        "owner": "root",
                        "group": "root",
                        "mode": "0644",
                    }
                ]
            },
        },
    }


def _is_ip_address(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True


def server_document(options: ServerOptions) -> dict[str, object]:
    """Build the ``Server`` resource for one host."""
    if options.hostname is not None:
        hostname = options.hostname.value
    elif _is_ip_address(options.address.value):
        hostname = str(options.resource_id)
    else:
        hostname = Hostname(options.address.value).value
    metadata: dict[str, object] = {"id": str(options.resource_id)}
    if options.description is not None:
        metadata["description"] = options.description
    return {
        "apiVersion": API_VERSION,
        "kind": ResourceKind.SERVER.value,
        "metadata": metadata,
        "spec": {
            "hostname": hostname,
            "address": options.address.value,
            "environment": str(options.environment),
            "serverType": str(options.server_type),
            "lifecycle": "active",
            "ssh": {"user": options.ssh_user.value, "port": options.ssh_port.value},
        },
    }


def add_ssh_key(
    project: Path, options: SshKeyOptions, schema_directory: Path
) -> AddResult:
    """Write one ``SshPublicKey`` read from a key file."""
    key = read_public_key(options.key_path)
    document = ssh_key_document(options, key)
    return _commit(project, schema_directory, [(ResourceKind.SSH_PUBLIC_KEY, document)])


def add_server_type(
    project: Path, options: ServerTypeOptions, schema_directory: Path
) -> AddResult:
    """Write one baseline ``ServerType``."""
    document = baseline_server_type_document(options)
    return _commit(project, schema_directory, [(ResourceKind.HOST_PROFILE, document)])


def add_server(
    project: Path, options: ServerOptions, schema_directory: Path
) -> AddResult:
    """Write one ``Server``, and its ``ServerType`` when the project lacks it."""
    documents: list[tuple[ResourceKind, dict[str, object]]] = []
    if not _resource_path(
        project, ResourceKind.HOST_PROFILE, options.server_type
    ).exists():
        server_type = ServerTypeOptions(resource_id=options.server_type)
        documents.append(
            (ResourceKind.HOST_PROFILE, baseline_server_type_document(server_type))
        )
    documents.append((ResourceKind.SERVER, server_document(options)))
    return _commit(project, schema_directory, documents)


def _resource_path(project: Path, kind: ResourceKind, resource_id: ResourceId) -> Path:
    return project / kind.directory / f"{resource_id}.yaml"


def _commit(
    project: Path,
    schema_directory: Path,
    documents: Sequence[tuple[ResourceKind, dict[str, object]]],
) -> AddResult:
    catalog = SchemaCatalog(schema_directory)
    planned: list[tuple[ResourceKind, ResourceId, Path, dict[str, object]]] = []
    for kind, document in documents:
        catalog.validate(kind, document)
        metadata = document["metadata"]
        assert isinstance(metadata, dict)  # noqa: S101 - built above, schema-checked
        resource_id = ResourceId.from_boundary(metadata["id"])
        path = _resource_path(project, kind, resource_id)
        if path.exists():
            message = f"{kind.value}/{resource_id} already exists: {path}"
            raise AuthoringError(ERROR_RESOURCE_EXISTS, message)
        planned.append((kind, resource_id, path, document))

    written: list[Path] = []
    try:
        for _kind, _resource_id, path, document in planned:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"---\n{yaml.safe_dump(document, sort_keys=False)}", encoding="utf-8"
            )
            written.append(path)
        validate_config(project, schema_directory)
    except ConfigValidationError:
        for path in written:
            path.unlink()
        raise
    except OSError as error:
        # A read-only or full project: leave it as it was, then say which
        # file could not be written and why.
        for path in written:
            path.unlink()
        message = f"cannot write {error.filename}: {error.strerror}"
        raise AuthoringError(ERROR_PROJECT_WRITE_FAILED, message) from error
    return AddResult(
        project=project,
        added=tuple(
            AddedResource(kind, resource_id, path.relative_to(project).as_posix())
            for kind, resource_id, path, _document in planned
        ),
    )
