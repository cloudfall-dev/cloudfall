"""Render blueprint importer tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from cloudfall.cli import main
from cloudfall.domain import ResourceId
from cloudfall.importer import (
    ImportTargets,
    RenderImportError,
    RenderImportResult,
    import_render_blueprint,
)
from cloudfall.validation import validate_config

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"

BLUEPRINT = """\
services:
  - type: web
    name: Acme API
    runtime: python
    repo: https://github.com/example/acme-api.git
    branch: main
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn acme.wsgi:application --bind 127.0.0.1:$PORT
    healthCheckPath: /healthz
    domains:
      - acme.example.test
    envVars:
      - key: DJANGO_SETTINGS_MODULE
        value: acme.settings
      - key: SECRET_KEY
        generateValue: true
      - key: DATABASE_URL
        fromDatabase:
          name: acme-db
          property: connectionString
      - fromGroup: shared-settings
  - type: worker
    name: acme-worker
    runtime: python
    repo: https://github.com/example/acme-api.git
    startCommand: celery -A acme worker
  - type: cron
    name: nightly-report
    runtime: python
    schedule: "0 2 * * *"
    startCommand: python report.py
  - type: web
    name: acme-site
    runtime: static
  - type: web
    name: acme-image
    runtime: docker
envVarGroups:
  - name: shared-settings
    envVars:
      - key: TZ
        value: UTC
databases:
  - name: acme-db
    databaseName: acme
    user: acme_user
    postgresMajorVersion: "16"
"""


def _targets(tmp_path: Path) -> ImportTargets:
    return ImportTargets(
        application_id=ResourceId("acme"),
        server_id=ResourceId("h1"),
        project_directory=tmp_path / "config",
        environment_directory=tmp_path / "env",
    )


def _import(tmp_path: Path) -> tuple[ImportTargets, RenderImportResult]:
    blueprint = tmp_path / "render.yaml"
    blueprint.write_text(BLUEPRINT, encoding="utf-8")
    targets = _targets(tmp_path)
    result = import_render_blueprint(blueprint, targets, SCHEMAS)
    return targets, result


def test_imported_state_merges_into_a_valid_directory(tmp_path: Path) -> None:
    targets, result = _import(tmp_path)

    for base in ("servers", "server-types", "ssh-public-keys"):
        shutil.copytree(
            EXAMPLES / base, targets.project_directory / base
        )
    state = validate_config(targets.project_directory, SCHEMAS)

    counts = state.counts_by_kind()
    assert counts["Application"] == 1
    assert counts["Component"] == 2
    assert counts["Service"] == 1
    assert counts["Domain"] == 1
    assert result.components == ("acme-api", "acme-worker")
    assert result.services == ("postgresql-main",)
    assert result.domains == ("acme-example-test",)


def test_importer_maps_ports_health_and_worker_semantics(
    tmp_path: Path,
) -> None:
    targets, _result = _import(tmp_path)

    api = (targets.project_directory / "components" / "acme-api.yaml").read_text(
        encoding="utf-8"
    )
    worker = (
        targets.project_directory / "components" / "acme-worker.yaml"
    ).read_text(encoding="utf-8")
    domain = (
        targets.project_directory / "domains" / "acme-example-test.yaml"
    ).read_text(encoding="utf-8")

    assert "type: http" in api
    assert "port: 8100" in api
    assert "path: /healthz" in api
    assert "packageManager: uv" in api
    assert "subdirectory" not in api
    assert "type: none" in worker
    assert "port: 8100" in domain
    assert "mode: required" in domain


def test_importer_writes_environment_files_outside_state(
    tmp_path: Path,
) -> None:
    targets, result = _import(tmp_path)

    env_path = targets.environment_directory / "acme-api.env"
    content = env_path.read_text(encoding="utf-8").splitlines()
    assert content[0] == "PORT=8100"
    # The 2026-09-08 M4 proving run found a declared PORT emitted twice.
    assert sum(line.startswith("PORT=") for line in content) == 1
    assert "DJANGO_SETTINGS_MODULE=acme.settings" in content
    assert "SECRET_KEY=" in content
    assert "DATABASE_URL=postgresql:///acme?host=/var/run/postgresql" in content
    assert "TZ=UTC" in content
    assert not list(targets.project_directory.rglob("*.env"))
    assert result.environment_files == (env_path,)


def test_importer_reports_gaps_for_unsupported_services(
    tmp_path: Path,
) -> None:
    _targets_value, result = _import(tmp_path)

    gaps = {
        (gap.category, gap.subject) for gap in result.gaps
    }
    assert ("unsupported", "nightly-report") in gaps
    assert ("unsupported", "acme-site") in gaps
    assert ("unsupported", "acme-image") in gaps
    assert any(
        (category == "action" and subject == "SECRET_KEY") or "SECRET_KEY" in detail
        for category, subject, detail in (
            (gap.category, gap.subject, gap.detail)
            for gap in result.gaps
        )
    )
    report = result.report.read_text(encoding="utf-8")
    assert "## Not imported" in report
    assert "## Required actions" in report


def test_importer_rejects_a_application_that_cannot_own_a_linux_user(
    tmp_path: Path,
) -> None:
    blueprint = tmp_path / "render.yaml"
    blueprint.write_text(BLUEPRINT, encoding="utf-8")
    targets = ImportTargets(
        application_id=ResourceId("a" * 40),
        server_id=ResourceId("h1"),
        project_directory=tmp_path / "config",
        environment_directory=tmp_path / "env",
    )

    with pytest.raises(RenderImportError) as error:
        import_render_blueprint(blueprint, targets, SCHEMAS)

    assert error.value.code == "import_application_invalid"


def test_cli_import_render_emits_structured_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    blueprint = tmp_path / "render.yaml"
    blueprint.write_text(BLUEPRINT, encoding="utf-8")

    exit_code = main(
        [
            "import",
            "render",
            "--project",
            str(tmp_path),
            str(blueprint),
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

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["data"]["components"] == ["acme-api", "acme-worker"]
    assert payload["data"]["report"].endswith("IMPORT-REPORT.md")


def test_an_argparse_command_reports_a_read_only_path_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blueprint = tmp_path / "render.yaml"
    blueprint.write_text(BLUEPRINT, encoding="utf-8")
    read_only = tmp_path / "read-only"
    read_only.mkdir()
    read_only.chmod(0o555)
    try:
        code = main(
            [
                *("import", "render", str(blueprint), "--project", str(tmp_path)),
                *("--application", "acme", "--server", "h1"),
                *("--output-dir", str(read_only / "config")),
                *("--env-dir", str(read_only / "env"), "--schemas", str(SCHEMAS)),
            ]
        )
    finally:
        read_only.chmod(0o755)

    captured = capsys.readouterr()
    assert (code, captured.out) == (1, "")
    document = json.loads(captured.err)
    assert document["error"]["code"] == "path_not_writable"
    assert document["error"]["exit_code"] == 1
    assert str(read_only) in document["error"]["message"]
