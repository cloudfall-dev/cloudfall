"""Guided data-migration plan and validation tests."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from cloudfall.agent_tools import AgentConfig
from cloudfall.domain import PostgresSchemaName, ResourceId
from cloudfall.lifecycle import (
    DataCopySchemas,
    EngineContext,
    ExecutionPlan,
    ExecutionStep,
    LifecycleError,
    migrate_data,
    plan_data_migration,
    subprocess_step,
)
from cloudfall.migrate import MigrateError, MigrateOptions, execute_migration

if TYPE_CHECKING:
    from cloudfall.lifecycle import StepRunner

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
    assert _schemas("public", "app").describe() == "schemas app, public"


def test_s007_12_data_copy_schemas_refuse_a_repeat_and_a_malformed_name() -> None:
    with pytest.raises(ValueError, match="more than once: app"):
        _schemas("app", "public", "app")
    for malformed in ("Bad;Name", "Public", "1app", "app\n", "", "a" * 64):
        with pytest.raises(ValueError, match=r"\^\[a-z_\]"):
            PostgresSchemaName(malformed)
    with pytest.raises(LifecycleError) as error:
        DataCopySchemas.from_boundary(["app", "app"])
    assert error.value.code == "data_copy_schema_invalid"
    assert DataCopySchemas.from_boundary(["public", "_app"]).values == [
        "_app",
        "public",
    ]


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


def _agent_config(
    tmp_path: Path, run: StepRunner = subprocess_step
) -> AgentConfig:
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
        run=run,
    )


def _succeeding(calls: list[ExecutionStep]) -> StepRunner:
    """Record each engine step and answer it as a run that passed."""

    def run(step: ExecutionStep) -> subprocess.CompletedProcess[str]:
        calls.append(step)
        return subprocess.CompletedProcess(step.argv, 0, "", "")

    return run


def test_s007_13_migrate_copies_only_the_data_schema_as_data_copy_does(
    tmp_path: Path,
) -> None:
    source = _url_file(tmp_path)
    calls: list[ExecutionStep] = []
    config = _agent_config(tmp_path, _succeeding(calls))
    options = MigrateOptions(
        plan_file=tmp_path / "plan.json",
        builds={"crm-backend": "main"},
        releases={},
        environment_files={},
        data_migrations={"crm": source},
        data_schemas={"crm": _schemas("public")},
        execute=True,
    )
    planned = execute_migration(config, replace(options, execute=False))
    steps = cast("list[dict[str, object]]", planned["steps"])
    (described,) = [step for step in steps if step["id"] == "data:crm"]
    assert "schema public" in str(described["description"])
    # Every other step already ran, so the resumed run is the data step alone.
    for step in steps:
        if step["id"] != "data:crm":
            step["status"] = "completed"
    options.plan_file.write_text(
        json.dumps({"version": 1, "steps": steps}), encoding="utf-8"
    )

    result = execute_migration(config, options)

    assert result["status"] == "ok", result
    (playbook,) = [step for step in calls if "--extra-vars" in step.argv]
    copied = plan_data_migration(
        _context(tmp_path),
        ResourceId("postgresql-main"),
        "crm",
        source,
        config.data_migrations_directory,
        _schemas("public"),
    )
    assert playbook.argv == copied.steps[-1].argv
    assert _extra_vars(copied)["cloudfall_data_schemas"] == ["public"]


def test_s007_13_a_resumed_data_step_names_the_schemas_it_copies(
    tmp_path: Path,
) -> None:
    source = _url_file(tmp_path)
    calls: list[ExecutionStep] = []
    config = _agent_config(tmp_path, _succeeding(calls))
    options = MigrateOptions(
        plan_file=tmp_path / "plan.json",
        builds={"crm-backend": "main"},
        releases={},
        environment_files={},
        data_migrations={"crm": source},
        data_schemas={"crm": _schemas("public")},
        execute=True,
    )
    steps = cast(
        "list[dict[str, object]]",
        execute_migration(config, replace(options, execute=False))["steps"],
    )
    for step in steps:
        if step["id"] != "data:crm":
            step["status"] = "completed"
    options.plan_file.write_text(
        json.dumps({"version": 1, "steps": steps}), encoding="utf-8"
    )

    # Resumed without --data-schema, the step copies every schema, and the
    # plan it reports must say so, not the "schema public" it was saved with.
    result = execute_migration(config, replace(options, data_schemas={}))

    (playbook,) = [step for step in calls if "--extra-vars" in step.argv]
    extra_vars = json.loads(playbook.argv[playbook.argv.index("--extra-vars") + 1])
    assert "cloudfall_data_schemas" not in extra_vars
    (described,) = [
        step
        for step in cast("list[dict[str, object]]", result["steps"])
        if step["id"] == "data:crm"
    ]
    assert "every non-system schema" in str(described["description"])


def test_s007_12_migrate_refuses_a_data_schema_without_its_data(
    tmp_path: Path,
) -> None:
    calls: list[ExecutionStep] = []
    options = MigrateOptions(
        plan_file=tmp_path / "plan.json",
        builds={"crm-backend": "main"},
        releases={},
        environment_files={},
        data_schemas={"crm": _schemas("public")},
        execute=True,
    )

    with pytest.raises(MigrateError) as error:
        execute_migration(_agent_config(tmp_path, _succeeding(calls)), options)

    assert error.value.code == "migrate_data_schema_without_data"
    assert calls == []
    assert not options.plan_file.exists()


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
