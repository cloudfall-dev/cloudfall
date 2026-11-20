"""A project of example servers, search-main with ``analyst``, and Kibana (S-024).

``search-main`` is the Elasticsearch service of S-012-1 with the user
``analyst``; ``search-kibana`` is the declaration of S-024 without ``domain``
and ``support``.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

import yaml
from elasticsearch_project import EXAMPLES, SCHEMAS, elasticsearch_service
from elasticsearch_project import write_project as write_elasticsearch_project

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "ANALYST",
    "EXAMPLES",
    "SCHEMAS",
    "kibana_domain",
    "kibana_service",
    "search_main",
    "write_project",
]

ANALYST: dict[str, object] = {
    "name": "analyst",
    "access": "read",
    "indexPatterns": ["crm-*"],
    "passwordRef": {
        "environment": "production",
        "path": "/services/search-main",
        "key": "ANALYST_PASSWORD",
    },
}


def search_main(**elasticsearch: object) -> dict[str, object]:
    """Return the Elasticsearch service of S-012-1 with the user ``analyst``."""
    service = elasticsearch_service()
    spec = service["spec"]
    assert isinstance(spec, dict)
    spec["elasticsearch"].update({"users": [copy.deepcopy(ANALYST)], **elasticsearch})
    return service


def kibana_service(
    service_id: str = "search-kibana",
    *,
    server: str = "h1",
    port: int = 5601,
    environment: str = "production",
    **kibana: object,
) -> dict[str, object]:
    """Return the spec's declaration without ``domain`` and ``support``.

    ``kibana`` is merged into the block; a field given as ``None`` is left out.
    """
    block: dict[str, object] = {
        "elasticsearch": "search-main",
        "heapMb": 1024,
        "users": [{"name": "analyst", "access": "edit"}],
    }
    block.update(kibana)
    return {
        "apiVersion": "cloudfall/v1",
        "kind": "Service",
        "metadata": {"id": service_id, "description": "Dashboards over product search"},
        "spec": {
            "serviceKind": "kibana",
            "environment": environment,
            "server": server,
            "bind": {"address": "127.0.0.1", "port": port},
            "kibana": {
                key: value for key, value in block.items() if value is not None
            },
        },
    }


def kibana_domain(**spec: object) -> dict[str, object]:
    """Return the spec's Domain publishing Kibana, with ``spec`` merged in."""
    content: dict[str, object] = {
        "primaryName": "kibana.example.test",
        "aliases": [],
        "proxy": {
            "server": "h1",
            "configurationPath": (
                "/etc/nginx/sites-available/kibana.example.test.conf"
            ),
            "service": "nginx.service",
            "upstream": {"address": "127.0.0.1", "port": 5601},
        },
        "origin": {
            "server": "h1",
            "configurationPath": "/etc/nginx/sites-available/kibana-origin.conf",
            "service": "nginx.service",
            "scheme": "http",
            "port": 5601,
            "serverName": "kibana.example.test",
        },
        "edge": {"provider": "cloudflare", "mode": "dns-only"},
        "tls": {"mode": "required"},
        "healthCheck": {
            "scheme": "https",
            "path": "/api/status",
            "expectedStatuses": [200],
            "timeoutSeconds": 5,
        },
    }
    content.update(spec)
    return {
        "apiVersion": "cloudfall/v1",
        "kind": "Domain",
        "metadata": {"id": "search-kibana-site"},
        "spec": content,
    }


def write_project(
    directory: Path,
    *services: dict[str, object],
    servers: tuple[str, ...] = ("h1",),
    domains: tuple[dict[str, object], ...] = (),
) -> Path:
    """Write the example servers, their baseline, ``services`` and ``domains``."""
    project = write_elasticsearch_project(directory, *services, servers=servers)
    if domains:
        (project / "domains").mkdir()
    for domain in domains:
        metadata = domain["metadata"]
        assert isinstance(metadata, dict)
        (project / "domains" / f"{metadata['id']}.yaml").write_text(
            yaml.safe_dump(domain, sort_keys=False), encoding="utf-8"
        )
    return project
