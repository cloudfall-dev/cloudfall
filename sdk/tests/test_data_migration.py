"""Guided data-migration plan and validation tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from cloudfall.agent_tools import AgentConfig
from cloudfall.domain import PostgresSchemaName, ResourceId
from cloudfall.lifecycle import (
    DataCopySchemas,
    EngineContext,
    ExecutionPlan,
    LifecycleError,
    migrate_data,
    plan_data_migration,
)
from cloudfall.migrate import MigrateError, MigrateOptions, execute_migration

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"


def _context(tmp_path: Path) -> EngineContext:
    return EngineContext(
        project_directory=EXAMPLES,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=tmp_path / "inventory.json",
    )


def _url_file(tmp_path: Path) -> Path:
    url_file = tmp_path / "source-url"
    url_file.write_text("postgresql://demo@source.example.test/demo\n")
    return url_file


def test_data_migration_plan_composes_engine_contract_steps(
    tmp_path: Path,
) -> None:
    plan = plan_data_migration(
        _context(tmp_path),
        ResourceId("postgresql-main"),
        "crm",
        _url_file(tmp_path),
        tmp_path / "receipts",
    )

    assert plan.action == "data-migration"
    playbook = plan.steps[-1]
    assert any("data.yml" in argument for argument in playbook.argv)
    extra_vars = json.loads(
        playbook.argv[playbook.argv.index("--extra-vars") + 1]
    )
    assert extra_vars["cloudfall_data_service_id"] == "postgresql-main"
    assert extra_vars["cloudfall_data_database"] == "crm"
    assert Path(extra_vars["cloudfall_data_source_url_file"]).is_absolute()
    assert Path(extra_vars["cloudfall_data_receipt_directory"]).is_absolute()


def _schemas(*names: str) -> DataCopySchemas:
    return DataCopySchemas(tuple(PostgresSchemaName(name) for name in names))


def _extra_vars(plan: ExecutionPlan) -> dict[str, object]:
    playbook = plan.steps[-1]
    extra_vars = json.loads(playbook.argv[playbook.argv.index("--extra-vars") + 1])
    assert isinstance(extra_vars, dict)
    return extra_vars


def test_s007_1_data_migration_plan_passes_the_named_schemas_sorted(
    tmp_path: Path,
) -> None:
    plan = plan_data_migration(
        _context(tmp_path),
        ResourceId("postgresql-main"),
        "crm",
        _url_file(tmp_path),
        tmp_path / "receipts",
        _schemas("public", "app"),
    )

    assert _extra_vars(plan)["cloudfall_data_schemas"] == ["app", "public"]


def test_s007_2_data_migration_plan_without_schemas_is_unchanged(
    tmp_path: Path,
) -> None:
    """No --schema keeps the extra vars a 0.7.0 whole-database copy sent."""
    arguments = (
        _context(tmp_path),
        ResourceId("postgresql-main"),
        "crm",
        _url_file(tmp_path),
        tmp_path / "receipts",
    )

    assert _extra_vars(plan_data_migration(*arguments)) == _extra_vars(
        plan_data_migration(*arguments, DataCopySchemas())
    )
    assert set(_extra_vars(plan_data_migration(*arguments))) == {
        "cloudfall_data_service_id",
        "cloudfall_data_database",
        "cloudfall_data_source_url_file",
        "cloudfall_data_receipt_directory",
    }


def test_data_copy_schemas_describe_the_selection() -> None:
    assert DataCopySchemas().describe() == "every non-system schema"
    assert _schemas("public").describe() == "schema public"
    assert _schemas("public", "app", "public").describe() == "schemas app, public"


def test_data_migration_rejects_an_undeclared_service(tmp_path: Path) -> None:
    with pytest.raises(LifecycleError) as error:
        migrate_data(
            _context(tmp_path),
            ResourceId("no-such-service"),
            "crm",
            _url_file(tmp_path),
        )
    assert error.value.code == "lifecycle_service_missing"


def test_data_migration_rejects_an_undeclared_database(
    tmp_path: Path,
) -> None:
    with pytest.raises(LifecycleError) as error:
        migrate_data(
            _context(tmp_path),
            ResourceId("postgresql-main"),
            "no-such-database",
            _url_file(tmp_path),
        )
    assert error.value.code == "lifecycle_database_missing"


def test_data_migration_rejects_an_empty_source_url_file(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty-url"
    empty.write_text("\n")
    with pytest.raises(LifecycleError) as error:
        migrate_data(
            _context(tmp_path),
            ResourceId("postgresql-main"),
            "crm",
            empty,
        )
    assert error.value.code == "lifecycle_source_url_missing"


def _agent_config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        project_directory=EXAMPLES,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=tmp_path / "inventory.json",
        observed_directory=tmp_path / "observed",
        service_observed_directory=tmp_path / "observed-services",
        deployments_directory=tmp_path / "deployments",
        releases_directory=tmp_path / "releases",
        artifacts_directory=tmp_path / "artifacts",
        data_migrations_directory=tmp_path / "data-migrations",
    )


def test_migrate_plan_orders_data_steps_after_services(
    tmp_path: Path,
) -> None:
    options = MigrateOptions(
        plan_file=tmp_path / "plan.json",
        builds={"crm-backend": "main"},
        releases={},
        environment_files={},
        data_migrations={"crm": _url_file(tmp_path)},
    )

    result = execute_migration(_agent_config(tmp_path), options)

    steps = cast("list[dict[str, object]]", result["steps"])
    step_ids = [str(step["id"]) for step in steps]
    assert "data:crm" in step_ids
    assert step_ids.index("services") < step_ids.index("data:crm")
    assert step_ids.index("data:crm") < step_ids.index("deploy:crm-backend")
    assert step_ids.index("data:crm") < step_ids.index("deploy:crm-backend")


def test_migrate_rejects_an_undeclared_data_migration_database(
    tmp_path: Path,
) -> None:
    options = MigrateOptions(
        plan_file=tmp_path / "plan.json",
        builds={"crm-backend": "main"},
        releases={},
        environment_files={},
        data_migrations={"no-such-database": _url_file(tmp_path)},
    )

    with pytest.raises(MigrateError) as error:
        execute_migration(_agent_config(tmp_path), options)
    assert error.value.code == "migrate_database_unknown"
