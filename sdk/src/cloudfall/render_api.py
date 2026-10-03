"""Fetch live Render services and synthesize an importable blueprint.

The Render REST API is read through a small injectable client and adapted
into the exact mapping shape the blueprint importer already consumes, so
both entry points share one mapping pipeline and one gap report. Secret
material (connection strings fetched for matching) is compared in memory
and never written anywhere.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from cloudfall.importer import (
    ImportGap,
    RenderImportError,
    import_render_mapping,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from cloudfall.importer import ImportTargets, RenderImportResult

ERROR_API_UNREACHABLE = "render_api_unreachable"
_ERROR_API_INVALID = "render_api_invalid"
_PAGE_LIMIT = 100
_SERVICE_TYPES: Mapping[str, str] = {
    "web_service": "web",
    "background_worker": "worker",
    "private_service": "pserv",
    "cron_job": "cron",
    "static_site": "static",
    "keyvalue": "keyvalue",
}
_CATEGORY_ACTION = "action-required"
_CATEGORY_ASSUMPTION = "assumption"


class RenderApiClient(Protocol):
    """Read-only view of one Render workspace."""

    def list_services(self) -> tuple[Mapping[str, object], ...]:
        """Return every service object in the workspace."""
        ...

    def list_env_vars(
        self, service_id: str
    ) -> tuple[Mapping[str, object], ...]:
        """Return every environment variable of one service."""
        ...

    def list_postgres(self) -> tuple[Mapping[str, object], ...]:
        """Return every managed PostgreSQL instance."""
        ...

    def postgres_connection_strings(self, postgres_id: str) -> tuple[str, ...]:
        """Return the connection strings of one managed PostgreSQL."""
        ...


RENDER_API_URL = "https://api.render.com/v1"
"""The Render API an import reads; an MCP call cannot point it elsewhere."""


@dataclass(frozen=True, slots=True)
class HttpRenderApiClient:
    """Bearer-authenticated client for the Render REST API."""

    api_key: str
    base_url: str = RENDER_API_URL
    timeout_seconds: float = 30.0
    transport: Callable[[str, str], object] | None = None

    def list_services(self) -> tuple[Mapping[str, object], ...]:
        """Return every service object in the workspace."""
        return self._paginate("/services", "service")

    def list_env_vars(
        self, service_id: str
    ) -> tuple[Mapping[str, object], ...]:
        """Return every environment variable of one service."""
        return self._paginate(f"/services/{service_id}/env-vars", "envVar")

    def list_postgres(self) -> tuple[Mapping[str, object], ...]:
        """Return every managed PostgreSQL instance."""
        return self._paginate("/postgres", "postgres")

    def postgres_connection_strings(self, postgres_id: str) -> tuple[str, ...]:
        """Return the connection strings of one managed PostgreSQL."""
        payload = self._get(f"/postgres/{postgres_id}/connection-info")
        info = _require_mapping(payload, "postgres connection info")
        return tuple(
            value
            for key in ("internalConnectionString", "externalConnectionString")
            if isinstance((value := info.get(key)), str) and value
        )

    def _paginate(
        self, path: str, envelope: str
    ) -> tuple[Mapping[str, object], ...]:
        items: list[Mapping[str, object]] = []
        cursor: str | None = None
        while True:
            query = f"?limit={_PAGE_LIMIT}"
            if cursor is not None:
                query += f"&cursor={cursor}"
            payload = self._get(f"{path}{query}")
            if not isinstance(payload, list):
                message = f"Render API returned a non-list page for {path}"
                raise RenderImportError(_ERROR_API_INVALID, message)
            rows = cast("list[object]", payload)
            for raw_row in rows:
                row = _require_mapping(raw_row, f"{envelope} page row")
                items.append(_require_mapping(row.get(envelope), envelope))
                raw_cursor = row.get("cursor")
                if isinstance(raw_cursor, str):
                    cursor = raw_cursor
            if len(rows) < _PAGE_LIMIT:
                return tuple(items)

    def _get(self, path: str) -> object:
        url = f"{self.base_url}{path}"
        if self.transport is not None:
            return self.transport(url, self.api_key)
        request = urllib.request.Request(  # noqa: S310 - fixed https base
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed https base
                request, timeout=self.timeout_seconds
            ) as response:
                body = response.read()
        except OSError as error:
            message = f"Render API unreachable: {url}: {error}"
            raise RenderImportError(ERROR_API_UNREACHABLE, message) from error
        try:
            return cast("object", json.loads(body.decode("utf-8")))
        except json.JSONDecodeError as error:
            message = f"Render API returned invalid JSON for {path}"
            raise RenderImportError(_ERROR_API_INVALID, message) from error


@dataclass(slots=True)
class _Synthesis:
    """Accumulates the blueprint and API-side gap notes."""

    services: list[dict[str, object]] = field(default_factory=list)
    databases: list[dict[str, object]] = field(default_factory=list)
    gaps: list[ImportGap] = field(default_factory=list)

    def gap(self, category: str, subject: str, detail: str) -> None:
        self.gaps.append(
            ImportGap(category=category, subject=subject, detail=detail)
        )


def synthesize_blueprint(
    client: RenderApiClient,
) -> tuple[dict[str, object], tuple[ImportGap, ...]]:
    """Fetch the live workspace and shape it like a Render blueprint."""
    synthesis = _Synthesis()
    connection_index = _database_connection_index(client, synthesis)
    for service in client.list_services():
        _synthesize_service(client, service, connection_index, synthesis)
    blueprint: dict[str, object] = {"services": synthesis.services}
    if synthesis.databases:
        blueprint["databases"] = synthesis.databases
    return blueprint, tuple(synthesis.gaps)


def read_api_key(path: Path) -> str:
    """Read the Render API key from a controller-side file."""
    code = "render_api_key_missing"
    if not path.is_file():
        message = f"Render API key file does not exist: {path}"
        raise RenderImportError(code, message)
    api_key = path.read_text(encoding="utf-8").strip()
    if not api_key:
        message = f"Render API key file is empty: {path}"
        raise RenderImportError(code, message)
    return api_key


def import_render_api(
    client: RenderApiClient,
    targets: ImportTargets,
    schema_directory: Path,
) -> RenderImportResult:
    """Map one live Render workspace onto validated Cloudfall state files."""
    blueprint, gaps = synthesize_blueprint(client)
    return import_render_mapping(blueprint, targets, schema_directory, gaps)


def _database_connection_index(
    client: RenderApiClient, synthesis: _Synthesis
) -> dict[str, str]:
    """Map known connection strings onto blueprint database names."""
    index: dict[str, str] = {}
    for postgres in client.list_postgres():
        raw_id = postgres.get("id")
        name = str(postgres.get("name", "database"))
        database: dict[str, object] = {"name": name}
        database_name = postgres.get("databaseName")
        if isinstance(database_name, str) and database_name:
            database["databaseName"] = database_name
        version = postgres.get("version")
        if isinstance(version, str) and version:
            database["postgresMajorVersion"] = version.split(".")[0]
        if postgres.get("databaseUser") is not None:
            database["user"] = postgres.get("databaseUser")
        synthesis.databases.append(database)
        if not isinstance(raw_id, str) or not raw_id:
            synthesis.gap(
                _CATEGORY_ACTION,
                name,
                "managed PostgreSQL has no id; connection strings in "
                "environment variables cannot be rewritten automatically",
            )
            continue
        for connection in client.postgres_connection_strings(raw_id):
            index[connection] = name
    return index


def _synthesize_service(
    client: RenderApiClient,
    service: Mapping[str, object],
    connection_index: Mapping[str, str],
    synthesis: _Synthesis,
) -> None:
    name = str(service.get("name", "unnamed"))
    raw_type = str(service.get("type", ""))
    blueprint_type = _SERVICE_TYPES.get(raw_type, raw_type)
    entry = _service_entry(service, name, blueprint_type)
    if service.get("suspended") == "suspended":
        synthesis.gap(
            _CATEGORY_ASSUMPTION,
            name,
            "service is suspended on Render; imported anyway",
        )
    raw_id = service.get("id")
    if isinstance(raw_id, str) and raw_id and blueprint_type not in (
        "static",
        "keyvalue",
    ):
        entry["envVars"] = _synthesize_env_vars(
            client.list_env_vars(raw_id), name, connection_index, synthesis
        )
    synthesis.services.append(entry)


def _service_entry(
    service: Mapping[str, object],
    name: str,
    blueprint_type: str,
) -> dict[str, object]:
    details = _optional_mapping(service.get("serviceDetails"))
    specifics = (
        _optional_mapping(details.get("envSpecificDetails"))
        if details is not None
        else None
    )
    entry: dict[str, object] = {"type": blueprint_type, "name": name}
    detail_keys = (("env", "runtime"), ("healthCheckPath", "healthCheckPath"),
                   ("schedule", "schedule"))
    if details is not None:
        for source_key, target_key in detail_keys:
            value = details.get(source_key)
            if isinstance(value, str) and value:
                entry[target_key] = value
    for source_key in ("repo", "branch"):
        value = service.get(source_key)
        if isinstance(value, str) and value:
            entry[source_key] = value
    if specifics is not None:
        for source_key in ("buildCommand", "startCommand"):
            value = specifics.get(source_key)
            if isinstance(value, str) and value:
                entry[source_key] = value
    return entry


def _synthesize_env_vars(
    env_vars: tuple[Mapping[str, object], ...],
    name: str,
    connection_index: Mapping[str, str],
    synthesis: _Synthesis,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for env_var in env_vars:
        key = env_var.get("key")
        if not isinstance(key, str) or not key:
            synthesis.gap(
                _CATEGORY_ACTION,
                name,
                "the Render API returned an environment entry without a key",
            )
            continue
        value = env_var.get("value")
        if not isinstance(value, str):
            entries.append({"key": key, "sync": False})
            continue
        database_name = connection_index.get(value)
        if database_name is not None:
            entries.append(
                {
                    "key": key,
                    "fromDatabase": {
                        "name": database_name,
                        "property": "connectionString",
                    },
                }
            )
            continue
        entries.append({"key": key, "value": value})
    return entries


def _require_mapping(value: object, concept: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        message = f"Render API {concept} is not an object"
        raise RenderImportError(_ERROR_API_INVALID, message)
    return cast("Mapping[str, object]", value)


def _optional_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        return None
    return cast("Mapping[str, object]", value)
