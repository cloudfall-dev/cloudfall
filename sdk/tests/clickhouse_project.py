"""A project of three example servers and the ClickHouse declaration of S-022."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
SCHEMAS = ROOT / "config" / "schemas" / "v1"
REPLICAS = ("h1", "h2")
VOTERS = ("h1", "h2", "h3")
EVENTS_PASSWORD_REF = {
    "environment": "production",
    "path": "/services/clickhouse-main",
    "key": "EVENTS_PASSWORD",
}
DATABASES: list[dict[str, object]] = [
    {"name": "events", "owner": "events", "passwordRef": EVENTS_PASSWORD_REF},
]


def clickhouse_service(
    service_id: str = "clickhouse-main",
    *,
    server: str = "h1",
    port: int = 9000,
    environment: str = "production",
    **clickhouse: object,
) -> dict[str, object]:
    """Return the spec's declaration, with ``clickhouse`` merged into its block.

    A field given as ``None`` is left out.
    """
    block: dict[str, object] = {
        "version": "26.8.37.168",
        "maxServerMemoryMb": 16384,
        "replicas": list(REPLICAS),
        "keeper": {"servers": list(VOTERS)},
        "databases": copy.deepcopy(DATABASES),
        "clientAccess": {"allowedSources": ["10.0.0.0/24"]},
    }
    block.update(clickhouse)
    return {
        "apiVersion": "cloudfall/v1",
        "kind": "Service",
        "metadata": {"id": service_id, "description": "Product events and reports"},
        "spec": {
            "serviceKind": "clickhouse",
            "environment": environment,
            "server": server,
            "bind": {"address": "127.0.0.1", "port": port},
            "clickhouse": {
                key: value for key, value in block.items() if value is not None
            },
            "backup": {
                "directory": f"/var/backups/cloudfall/{service_id}",
                "onCalendar": "*-*-* 02:40:00 UTC",
                "restoreCheckOnCalendar": "Sun *-*-* 05:30:00 UTC",
                "retentionDays": 14,
            },
            "metrics": {"enabled": True},
        },
    }


def _server(server: str) -> dict[str, object]:
    """Return an example server; ``h3`` is ``h2`` under its own names."""
    if server != "h3":
        loaded: dict[str, object] = yaml.safe_load(
            (EXAMPLES / "servers" / f"{server}.yaml").read_text(encoding="utf-8")
        )
        return loaded
    h3: dict[str, object] = yaml.safe_load(
        (EXAMPLES / "servers" / "h2.yaml").read_text(encoding="utf-8")
    )
    h3["metadata"] = {"id": "h3", "description": "Third Keeper voter"}
    spec = h3["spec"]
    assert isinstance(spec, dict)
    spec["hostname"] = "h3.example.internal"
    spec["address"] = "h3.example.internal"
    spec["network"] = {
        "ipv4": "203.0.113.13",
        "ipv6Cidr": "2001:db8:13::/64",
        "reverseDns": "h3.example.internal",
    }
    return h3


def write_project(
    directory: Path,
    *services: dict[str, object],
    servers: tuple[str, ...] = VOTERS,
) -> Path:
    """Write ``servers``, their baseline, and ``services``."""
    for folder in ("servers", "baselines", "services"):
        (directory / folder).mkdir(parents=True)
    for server in servers:
        (directory / "servers" / f"{server}.yaml").write_text(
            yaml.safe_dump(_server(server), sort_keys=False), encoding="utf-8"
        )
    shutil.copy(
        EXAMPLES / "baselines" / "debian-application.yaml",
        directory / "baselines" / "debian-application.yaml",
    )
    for service in services:
        metadata = service["metadata"]
        assert isinstance(metadata, dict)
        (directory / "services" / f"{metadata['id']}.yaml").write_text(
            yaml.safe_dump(service, sort_keys=False), encoding="utf-8"
        )
    return directory


def edit_server(project: Path, server: str, **spec: object) -> None:
    """Replace, or with ``None`` remove, fields of a server's spec."""
    path = project / "servers" / f"{server}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key, value in spec.items():
        if value is None:
            del document["spec"][key]
        else:
            document["spec"][key] = value
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


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
