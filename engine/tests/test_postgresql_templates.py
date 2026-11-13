"""PostgreSQL service template tests."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parents[2]
TEMPLATES = (
    ROOT / "engine" / "ansible" / "roles" / "cloudfall_postgresql" / "templates"
)


def _service() -> dict[str, object]:
    return {
        "id": "postgresql-main",
        "serviceKind": "postgresql",
        "environment": "production",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": 5432},
        "postgresql": {
            "majorVersion": "17",
            "databases": [
                {"name": "crm", "application": "crm", "owner": "crm"},
            ],
        },
        "backup": {
            "directory": "/var/backups/cloudfall/postgresql-main",
            "onCalendar": "*-*-* 02:00:00 UTC",
            "restoreCheckOnCalendar": "*-*-* 05:00:00 UTC",
            "retentionDays": 14,
        },
    }


def _render(name: str) -> str:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
    )
    return environment.get_template(name).render(
        cloudfall_postgresql_service=_service(),
    )


def test_cluster_configuration_binds_to_loopback() -> None:
    rendered = _render("cloudfall.conf.j2")

    assert "listen_addresses = '127.0.0.1'" in rendered
    assert "port = 5432" in rendered


def test_backup_script_dumps_and_prunes_each_database() -> None:
    rendered = _render("backup.sh.j2")

    assert "set -euo pipefail" in rendered
    assert 'pg_dump --port "${port}" --format=custom' in rendered
    assert '"crm"' in rendered
    assert 'retention_days="14"' in rendered
    assert '-mtime "+${retention_days}" -delete' in rendered


def test_backup_script_dumps_the_globals_beside_the_databases() -> None:
    rendered = _render("backup.sh.j2")

    assert (
        'pg_dumpall --port "${port}" --globals-only --no-role-passwords' in rendered
    )
    assert '--file "${backup_directory}/globals-${timestamp}.sql"' in rendered
    assert "-name '*.dump' -o -name 'globals-*.sql'" in rendered


def test_restore_check_restores_into_a_scratch_database() -> None:
    rendered = _render("restore-check.sh.j2")

    assert "set -euo pipefail" in rendered
    assert 'scratch="crm_restore_check"' in rendered
    assert 'pg_restore --port "${port}" --dbname "${scratch}"' in rendered
    assert "--exit-on-error" in rendered
    assert 'dropdb --port "${port}" "${scratch}"' in rendered
    assert "no backup found for crm" in rendered


def test_restore_check_counts_each_table_without_its_children() -> None:
    """A dump's COPY block for a parent table holds only the parent's rows."""
    rendered = _render("restore-check.sh.j2")

    assert "SELECT count(*) AS c FROM ONLY %I.%I" in rendered
    assert "count(*) AS c FROM %I.%I" not in rendered


def test_backup_units_run_as_postgres_on_the_declared_calendar() -> None:
    service = _render("backup.service.j2")
    timer = _render("backup.timer.j2")

    assert "Type=oneshot" in service
    assert "User=postgres" in service
    assert "ExecStart=/usr/local/sbin/cloudfall-postgresql-backup" in service
    assert "After=postgresql@17-main.service" in service
    assert "OnCalendar=*-*-* 02:00:00 UTC" in timer
    assert "Persistent=true" in timer


def test_restore_drill_units_run_on_the_declared_calendar() -> None:
    service = _render("restore-check.service.j2")
    timer = _render("restore-check.timer.j2")

    assert "Type=oneshot" in service
    assert "User=postgres" in service
    assert (
        "ExecStart=/usr/local/sbin/cloudfall-postgresql-restore-check"
        in service
    )
    assert "After=postgresql@17-main.service" in service
    assert "OnCalendar=*-*-* 05:00:00 UTC" in timer
    assert "Persistent=true" in timer
