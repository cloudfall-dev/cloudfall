"""The data copy preflight's refusal and the failed restore, as the SDK reads them.

The role writes the preflight findings to a file under the receipt
directory and fails; these tests stand in for the playbook with a runner
that does the same, since no PostgreSQL runs here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest
from cloudfall.agent_tools import AgentConfig
from cloudfall.app import Migration, _migration_failure
from cloudfall.domain import ResourceId
from cloudfall.lifecycle import (
    EngineContext,
    ExecutionStep,
    LifecycleError,
    StepRunner,
    migrate_data,
)
from cloudfall.migrate import MigrateOptions, execute_migration

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"
SOURCE_URL = "postgresql://owner:hunter2@source.example.test/crm"
FINDINGS = "postgresql-main-crm.preflight.json"
NO_FINDINGS: dict[str, list[str]] = {
    "missingSchemas": [],
    "extensions": [],
    "roles": [],
    "authReferences": [],
}
# More than the 2000 characters of output a failed step keeps.
NOISE = "TASK [cloudfall_data_migration : noise] ok\n" * 100


def _source(tmp_path: Path) -> Path:
    url_file = tmp_path / "source.url"
    url_file.write_text(f"{SOURCE_URL}\n", encoding="utf-8")
    return url_file


def _playbook(
    findings: dict[str, list[str]] | None, *, returncode: int
) -> StepRunner:
    """Stand in for data.yml: write the findings file, then end as told."""

    def run(step: ExecutionStep) -> subprocess.CompletedProcess[str]:
        if "--extra-vars" not in step.argv:
            return subprocess.CompletedProcess(step.argv, 0, "", "")
        extra_vars = json.loads(step.argv[step.argv.index("--extra-vars") + 1])
        directory = Path(extra_vars["cloudfall_data_receipt_directory"])
        if findings is not None:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / FINDINGS).write_text(json.dumps(findings), encoding="utf-8")
        return subprocess.CompletedProcess(step.argv, returncode, NOISE, "")

    return run


def _copy(
    tmp_path: Path, run: StepRunner, receipts: Path
) -> dict[str, object]:
    context = EngineContext(
        project_directory=EXAMPLES,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=tmp_path / "inventory.json",
        run=run,
    )
    return migrate_data(
        context, ResourceId("postgresql-main"), "crm", _source(tmp_path), receipts
    )


def _refusal(tmp_path: Path, findings: dict[str, list[str]]) -> LifecycleError:
    receipts = tmp_path / "data-migrations"
    with pytest.raises(LifecycleError) as raised:
        _copy(tmp_path, _playbook(findings, returncode=2), receipts)
    error = raised.value
    assert error.code == "data_copy_preflight_refused"
    assert not (receipts / FINDINGS).exists()
    assert not (receipts / "postgresql-main-crm.json").exists()
    assert "hunter2" not in f"{error.detail}{error.context}"
    return error


def test_s007_6_a_missing_schema_is_refused_with_its_name(tmp_path: Path) -> None:
    error = _refusal(tmp_path, {**NO_FINDINGS, "missingSchemas": ["missing"]})

    assert error.context == {**NO_FINDINGS, "missingSchemas": ["missing"]}
    assert "schemas the source lacks: missing" in error.detail


def test_s007_7_an_extension_the_target_lacks_is_named(tmp_path: Path) -> None:
    error = _refusal(tmp_path, {**NO_FINDINGS, "extensions": ["citext"]})

    assert error.context["extensions"] == ["citext"]


def test_s007_8_policy_roles_the_target_lacks_are_named(tmp_path: Path) -> None:
    error = _refusal(
        tmp_path, {**NO_FINDINGS, "roles": ["anon", "authenticated"]}
    )

    assert error.context["roles"] == ["anon", "authenticated"]


def test_s007_9_auth_references_are_named(tmp_path: Path) -> None:
    references = [
        "function public.current_tenant()",
        "policy notes_owner on public.notes",
    ]
    error = _refusal(tmp_path, {**NO_FINDINGS, "authReferences": references})

    assert error.context["authReferences"] == references


def test_s007_10_every_finding_comes_whole_in_one_answer(tmp_path: Path) -> None:
    # Far more names than the output tail could carry.
    roles = [f"role_{index:04d}" for index in range(400)]
    findings = {
        "missingSchemas": [],
        "extensions": ["postgis"],
        "roles": roles,
        "authReferences": ["function public.current_tenant()"],
    }

    error = _refusal(tmp_path, findings)

    assert error.context == findings
    for named in ("postgis", "role_0399", "public.current_tenant()"):
        assert named in error.detail


def test_s007_11_a_failed_restore_is_an_engine_failure_without_a_receipt(
    tmp_path: Path,
) -> None:
    receipts = tmp_path / "data-migrations"

    with pytest.raises(LifecycleError) as raised:
        _copy(tmp_path, _playbook(NO_FINDINGS, returncode=2), receipts)

    assert raised.value.code == "lifecycle_execution_failed"
    assert not (receipts / FINDINGS).exists()
    assert not (receipts / "postgresql-main-crm.json").exists()
    # Nothing an earlier run left behind stops the next one.
    done = _copy(tmp_path, _playbook(NO_FINDINGS, returncode=0), receipts)
    assert done["status"] == "ok"
    assert not (receipts / FINDINGS).exists()


def test_s007_11_a_stale_findings_file_never_answers_for_a_new_run(
    tmp_path: Path,
) -> None:
    receipts = tmp_path / "data-migrations"
    receipts.mkdir()
    (receipts / FINDINGS).write_text(
        json.dumps({**NO_FINDINGS, "roles": ["anon"]}), encoding="utf-8"
    )

    with pytest.raises(LifecycleError) as raised:
        _copy(tmp_path, _playbook(None, returncode=2), receipts)

    assert raised.value.code == "lifecycle_execution_failed"


def test_a_malformed_findings_file_is_refused(tmp_path: Path) -> None:
    receipts = tmp_path / "data-migrations"

    with pytest.raises(LifecycleError) as raised:
        _copy(
            tmp_path,
            _playbook(
                cast("dict[str, list[str]]", {"roles": "anon"}), returncode=2
            ),
            receipts,
        )

    assert raised.value.code == "data_copy_preflight_findings_invalid"


def _migrate_data_step(tmp_path: Path, run: StepRunner) -> dict[str, object]:
    config = AgentConfig(
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
    options = MigrateOptions(
        plan_file=tmp_path / "plan.json",
        builds={"crm-backend": "main"},
        releases={},
        environment_files={},
        data_migrations={"crm": _source(tmp_path)},
    )
    planned = execute_migration(config, options)
    steps = cast("list[dict[str, object]]", planned["steps"])
    # Every other step already ran, so the resumed run is the data step alone.
    for step in steps:
        if step["id"] != "data:crm":
            step["status"] = "completed"
    options.plan_file.write_text(
        json.dumps({"version": 1, "steps": steps}), encoding="utf-8"
    )
    return execute_migration(
        config,
        MigrateOptions(
            plan_file=options.plan_file,
            builds=options.builds,
            releases={},
            environment_files={},
            data_migrations=options.data_migrations,
            execute=True,
        ),
    )


def _migration(result: dict[str, object]) -> Migration:
    return Migration(
        effect="updated",
        status=str(result["status"]),
        steps=cast("list[dict[str, object]]", result["steps"]),
        completed=int(cast("int", result["completed"])),
        next=cast("str | None", result.get("next")),
        step=cast("str | None", result.get("step")),
    )


def test_s007_10_the_migrate_data_step_surfaces_the_same_refusal(
    tmp_path: Path,
) -> None:
    findings = {
        "missingSchemas": ["missing"],
        "extensions": ["postgis"],
        "roles": ["anon"],
        "authReferences": ["function public.current_tenant()"],
    }

    result = _migrate_data_step(tmp_path, _playbook(findings, returncode=2))
    error = cast("dict[str, object]", result["error"])
    failure = _migration_failure(_migration(result), error)

    assert result["status"] == "error"
    assert result["step"] == "data:crm"
    assert error["code"] == "data_copy_preflight_refused"
    assert failure is not None
    assert failure.code == "PRECONDITION"
    context = failure.context
    assert {key: context[key] for key in findings} == findings
    assert context["step"] == "data:crm"
    assert "hunter2" not in json.dumps(result)


def test_s007_11_a_failed_migrate_data_step_stays_an_engine_failure(
    tmp_path: Path,
) -> None:
    result = _migrate_data_step(tmp_path, _playbook(NO_FINDINGS, returncode=2))
    failure = _migration_failure(
        _migration(result), cast("dict[str, object]", result["error"])
    )

    assert failure is not None
    assert failure.code == "ENGINE_STEP_FAILED"
