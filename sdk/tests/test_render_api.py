"""Render API import tests against a recorded fake workspace."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cloudfall.cli import main
from cloudfall.domain import ResourceId
from cloudfall.importer import ImportTargets, RenderImportError
from cloudfall.render_api import (
    HttpRenderApiClient,
    import_render_api,
    read_api_key,
    synthesize_blueprint,
)

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"

_INTERNAL_URL = "postgres://acme:secret@dpg-internal/acme_db"
_EXTERNAL_URL = "postgres://acme:secret@dpg.render.com/acme_db"

_SERVICES: tuple[dict[str, object], ...] = (
    {
        "id": "srv-web",
        "name": "acme-api",
        "type": "web_service",
        "repo": "https://github.com/example/acme.git",
        "branch": "main",
        "suspended": "not_suspended",
        "serviceDetails": {
            "env": "python",
            "healthCheckPath": "/health",
            "envSpecificDetails": {
                "buildCommand": "uv sync",
                "startCommand": "uvicorn acme.asgi:application",
            },
        },
    },
    {
        "id": "srv-worker",
        "name": "acme-worker",
        "type": "background_worker",
        "repo": "https://github.com/example/acme.git",
        "branch": "main",
        "suspended": "suspended",
        "serviceDetails": {
            "env": "python",
            "envSpecificDetails": {
                "buildCommand": "uv sync",
                "startCommand": "celery -A acme worker",
            },
        },
    },
    {
        "id": "srv-cron",
        "name": "acme-report",
        "type": "cron_job",
        "repo": "https://github.com/example/acme.git",
        "suspended": "not_suspended",
        "serviceDetails": {
            "env": "python",
            "schedule": "0 6 * * *",
            "envSpecificDetails": {"startCommand": "python report.py"},
        },
    },
    {
        "id": "srv-static",
        "name": "acme-site",
        "type": "static_site",
        "suspended": "not_suspended",
        "serviceDetails": {},
    },
)

_ENV_VARS: dict[str, tuple[dict[str, object], ...]] = {
    "srv-web": (
        {"key": "DATABASE_URL", "value": _INTERNAL_URL},
        {"key": "APP_SECRET"},
        {"key": "LOG_LEVEL", "value": "info"},
    ),
    "srv-worker": (
        {"key": "DATABASE_URL", "value": _EXTERNAL_URL},
    ),
    "srv-cron": (),
}

_POSTGRES: tuple[dict[str, object], ...] = (
    {
        "id": "dpg-1",
        "name": "acme-db",
        "databaseName": "acme_db",
        "databaseUser": "acme",
        "version": "16.4",
    },
)


@dataclass
class FakeRenderClient:
    """Recorded workspace answering the client protocol."""

    env_var_requests: list[str] = field(default_factory=list)

    def list_services(self) -> tuple[dict[str, object], ...]:
        """Return the recorded services."""
        return _SERVICES

    def list_env_vars(self, service_id: str) -> tuple[dict[str, object], ...]:
        """Return the recorded environment variables."""
        self.env_var_requests.append(service_id)
        return _ENV_VARS[service_id]

    def list_postgres(self) -> tuple[dict[str, object], ...]:
        """Return the recorded managed databases."""
        return _POSTGRES

    def postgres_connection_strings(self, postgres_id: str) -> tuple[str, ...]:
        """Return the recorded connection strings."""
        assert postgres_id == "dpg-1"
        return (_INTERNAL_URL, _EXTERNAL_URL)


def test_synthesize_blueprint_maps_the_live_workspace() -> None:
    blueprint, gaps = synthesize_blueprint(FakeRenderClient())

    services = blueprint["services"]
    assert isinstance(services, list)
    by_name = {service["name"]: service for service in services}
    web = by_name["acme-api"]
    assert web["type"] == "web"
    assert web["runtime"] == "python"
    assert web["healthCheckPath"] == "/health"
    assert web["startCommand"] == "uvicorn acme.asgi:application"
    assert web["envVars"] == [
        {
            "key": "DATABASE_URL",
            "fromDatabase": {"name": "acme-db", "property": "connectionString"},
        },
        {"key": "APP_SECRET", "sync": False},
        {"key": "LOG_LEVEL", "value": "info"},
    ]
    assert by_name["acme-worker"]["type"] == "worker"
    assert by_name["acme-report"]["type"] == "cron"
    assert by_name["acme-report"]["schedule"] == "0 6 * * *"
    assert by_name["acme-site"]["type"] == "static"
    databases = blueprint["databases"]
    assert databases == [
        {
            "name": "acme-db",
            "databaseName": "acme_db",
            "postgresMajorVersion": "16",
            "user": "acme",
        }
    ]
    assert any("suspended" in gap.detail for gap in gaps)


def test_import_render_api_writes_state_and_resolved_env(
    tmp_path: Path,
) -> None:
    targets = ImportTargets(
        application_id=ResourceId.from_boundary("acme"),
        server_id=ResourceId.from_boundary("h1"),
        project_directory=tmp_path / "config",
        environment_directory=tmp_path / "env",
    )
    client = FakeRenderClient()

    result = import_render_api(client, targets, SCHEMAS)

    assert result.components == ("acme-api", "acme-worker")
    assert result.services == ("postgresql-main",)
    web_env = (tmp_path / "env" / "acme-api.env").read_text(encoding="utf-8")
    assert (
        "DATABASE_URL=postgresql:///acme_db?host=/var/run/postgresql"
        in web_env
    )
    assert "APP_SECRET=" in web_env
    assert "LOG_LEVEL=info" in web_env
    assert _INTERNAL_URL not in web_env
    worker_env = (tmp_path / "env" / "acme-worker.env").read_text(
        encoding="utf-8"
    )
    assert (
        "DATABASE_URL=postgresql:///acme_db?host=/var/run/postgresql"
        in worker_env
    )
    gap_details = " | ".join(gap.detail for gap in result.gaps)
    assert "suspended" in gap_details
    assert "0 6 * * *" in gap_details
    assert "static" in gap_details
    service_path = tmp_path / "config" / "services" / "postgresql-main.yaml"
    assert service_path.is_file()
    assert "majorVersion: '16'" in service_path.read_text(encoding="utf-8")
    assert client.env_var_requests == ["srv-web", "srv-worker", "srv-cron"]


def test_http_client_paginates_with_cursors() -> None:
    calls: list[str] = []
    first_page = [
        {"service": {"id": f"srv-{index}", "name": f"s{index}"},
         "cursor": f"c{index}"}
        for index in range(100)
    ]
    second_page = [
        {"service": {"id": "srv-last", "name": "last"}, "cursor": "c-last"}
    ]

    def transport(url: str, api_key: str) -> object:
        assert api_key == "test-key"
        calls.append(url)
        return second_page if "cursor=" in url else first_page

    client = HttpRenderApiClient(api_key="test-key", transport=transport)
    services = client.list_services()

    assert len(services) == 101
    assert services[-1]["id"] == "srv-last"
    assert calls == [
        "https://api.render.com/v1/services?limit=100",
        "https://api.render.com/v1/services?limit=100&cursor=c99",
    ]


def test_read_api_key_requires_a_non_empty_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    empty = tmp_path / "empty"
    empty.write_text("", encoding="utf-8")

    with pytest.raises(RenderImportError) as caught:
        read_api_key(missing)
    assert caught.value.code == "render_api_key_missing"
    with pytest.raises(RenderImportError):
        read_api_key(empty)


def test_cli_render_api_reports_missing_key_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "import",
            "render-api",
            "--project",
            str(tmp_path),
            "--api-key-file",
            str(tmp_path / "missing"),
            "--application",
            "acme",
            "--server",
            "h1",
            "--output-dir",
            str(tmp_path / "config"),
            "--env-dir",
            str(tmp_path / "env"),
            "--schemas",
            str(SCHEMAS),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["context"]["code"] == "render_api_key_missing"


def test_import_refuses_an_empty_workspace_with_a_structured_error(
    tmp_path: Path,
) -> None:
    @dataclass
    class EmptyClient:
        """Workspace holding only an unimportable Docker service."""

        def list_services(self) -> tuple[dict[str, object], ...]:
            """Return one suspended Docker service."""
            return (
                {
                    "id": "srv-docker",
                    "name": "legacy",
                    "type": "web_service",
                    "suspended": "suspended",
                    "serviceDetails": {"env": "docker"},
                },
            )

        def list_env_vars(
            self, _service_id: str
        ) -> tuple[dict[str, object], ...]:
            """Return no environment variables."""
            return ()

        def list_postgres(self) -> tuple[dict[str, object], ...]:
            """Return no managed databases."""
            return ()

        def postgres_connection_strings(
            self, _postgres_id: str
        ) -> tuple[str, ...]:
            """Return no connection strings."""
            return ()

    targets = ImportTargets(
        application_id=ResourceId.from_boundary("legacy"),
        server_id=ResourceId.from_boundary("h1"),
        project_directory=tmp_path / "config",
        environment_directory=tmp_path / "env",
    )

    with pytest.raises(RenderImportError) as caught:
        import_render_api(EmptyClient(), targets, SCHEMAS)

    assert caught.value.code == "render_import_empty"
    assert "container runtimes" in str(caught.value)
    assert not (tmp_path / "config").exists()
