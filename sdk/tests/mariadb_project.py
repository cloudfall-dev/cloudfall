"""A project of example servers and the MariaDB declaration of S-019."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
SCHEMAS = ROOT / "config" / "schemas" / "v1"
BILLING_PASSWORD_REF = {
    "environment": "production",
    "path": "/services/mariadb-main",
    "key": "BILLING_PASSWORD",
}
SOURCE_CREDENTIALS_REF = {
    "environment": "production",
    "path": "/services/mariadb-main",
    "key": "SOURCE_REPLICATION",
}
DATABASES: list[dict[str, object]] = [
    {"name": "crm", "application": "crm"},
    {"name": "billing", "owner": "billing", "passwordRef": BILLING_PASSWORD_REF},
]


def mariadb_service(
    service_id: str = "mariadb-main",
    *,
    server: str = "h1",
    port: int = 3306,
    **mariadb: object,
) -> dict[str, object]:
    """Return the spec's declaration, with ``mariadb`` merged into its block.

    A field given as ``None`` is left out.
    """
    block: dict[str, object] = {
        "version": "11.8.9",
        "innodbBufferPoolMb": 4096,
        "databases": copy.deepcopy(DATABASES),
    }
    block.update(mariadb)
    return _service(
        service_id,
        server,
        port,
        {key: value for key, value in block.items() if value is not None},
    )


def _service(
    service_id: str, server: str, port: int, block: dict[str, object]
) -> dict[str, object]:
    return {
        "apiVersion": "cloudfall/v1",
        "kind": "Service",
        "metadata": {"id": service_id, "description": "CRM and billing databases"},
        "spec": {
            "serviceKind": "mariadb",
            "environment": "production",
            "server": server,
            "bind": {"address": "127.0.0.1", "port": port},
            "mariadb": block,
            "backup": {
                "directory": f"/var/backups/cloudfall/{service_id}",
                "onCalendar": "*-*-* 02:30:00 UTC",
                "restoreCheckOnCalendar": "*-*-* 05:15:00 UTC",
                "retentionDays": 14,
            },
            "metrics": {"enabled": True},
        },
    }


def replica_service(
    service_id: str = "mariadb-replica",
    *,
    server: str = "h2",
    primary: str = "mariadb-main",
    server_id: int = 2,
    port: int = 3306,
    **mariadb: object,
) -> dict[str, object]:
    """Return a replica of ``primary``, without databases, on ``server``."""
    block: dict[str, object] = {
        "version": "11.8.9",
        "innodbBufferPoolMb": 4096,
        "replication": {"role": "replica", "serverId": server_id, "primary": primary},
        **mariadb,
    }
    return _service(service_id, server, port, block)


def primary_replication(server_id: int = 1, **fields: object) -> dict[str, object]:
    """Return the replication block of a primary listening on h1's IPv4."""
    return {
        "role": "primary",
        "serverId": server_id,
        "listenAddress": "203.0.113.11",
        **fields,
    }


def write_project(
    directory: Path, *services: dict[str, object], servers: tuple[str, ...] = ("h1",)
) -> Path:
    """Write ``servers``, their baseline, application ``crm``, and ``services``."""
    for folder in ("servers", "baselines", "applications", "components", "services"):
        (directory / folder).mkdir(parents=True)
    for server in servers:
        shutil.copy(
            EXAMPLES / "servers" / f"{server}.yaml",
            directory / "servers" / f"{server}.yaml",
        )
    shutil.copy(
        EXAMPLES / "baselines" / "debian-application.yaml",
        directory / "baselines" / "debian-application.yaml",
    )
    shutil.copy(
        EXAMPLES / "applications" / "crm.yaml",
        directory / "applications" / "crm.yaml",
    )
    component = yaml.safe_load(
        (EXAMPLES / "components" / "crm-backend.yaml").read_text(encoding="utf-8")
    )
    component["spec"]["deployment"]["servers"] = list(servers)
    (directory / "components" / "crm-backend.yaml").write_text(
        yaml.safe_dump(component, sort_keys=False), encoding="utf-8"
    )
    for service in services:
        metadata = service["metadata"]
        assert isinstance(metadata, dict)
        (directory / "services" / f"{metadata['id']}.yaml").write_text(
            yaml.safe_dump(service, sort_keys=False), encoding="utf-8"
        )
    return directory


def edit_baseline(project: Path, **spec: object) -> None:
    """Replace, or with ``None`` remove, fields of the baseline's spec."""
    path = project / "baselines" / "debian-application.yaml"
    baseline = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key, value in spec.items():
        if value is None:
            del baseline["spec"][key]
        else:
            baseline["spec"][key] = value
    path.write_text(yaml.safe_dump(baseline, sort_keys=False), encoding="utf-8")
