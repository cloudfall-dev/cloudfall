"""A project of example servers and one Elasticsearch service (S-012)."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
SCHEMAS = ROOT / "config" / "schemas" / "v1"
PASSWORD_REF = {
    "environment": "production",
    "path": "/services/search-main",
    "key": "CRM_PASSWORD",
}
SNAPSHOT_REPOSITORY = {
    "type": "s3",
    "bucket": "search-snapshots",
    "endpoint": "s3.example.com",
    "basePath": "search",
    "credentialsRef": {
        "environment": "production",
        "path": "/services/search-main",
        "key": "S3",
    },
}


def elasticsearch_service(
    service_id: str = "search-main",
    *,
    server: str = "h1",
    port: int = 9200,
    **elasticsearch: object,
) -> dict[str, object]:
    """Return the spec's declaration, without users, with ``elasticsearch`` merged."""
    return {
        "apiVersion": "cloudfall/v1",
        "kind": "Service",
        "metadata": {"id": service_id, "description": "Product search"},
        "spec": {
            "serviceKind": "elasticsearch",
            "environment": "production",
            "server": server,
            "bind": {"address": "127.0.0.1", "port": port},
            "elasticsearch": {"version": "9.2.4", "heapMb": 4096, **elasticsearch},
            "backup": {
                "directory": f"/var/backups/cloudfall/{service_id}",
                "onCalendar": "*-*-* 03:20:00 UTC",
                "restoreCheckOnCalendar": "Sun *-*-* 05:00:00 UTC",
                "retentionDays": 14,
            },
            "metrics": {"enabled": True},
        },
    }


def write_project(
    directory: Path, *services: dict[str, object], servers: tuple[str, ...] = ("h1",)
) -> Path:
    """Write the example servers, their baseline, and ``services``."""
    (directory / "servers").mkdir(parents=True)
    (directory / "baselines").mkdir()
    (directory / "services").mkdir()
    for server in servers:
        shutil.copy(
            EXAMPLES / "servers" / f"{server}.yaml",
            directory / "servers" / f"{server}.yaml",
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
