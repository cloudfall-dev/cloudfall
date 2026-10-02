"""Resumable migration orchestrator tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cloudfall.agent_tools import AgentConfig
from cloudfall.cli import main
from cloudfall.migrate import (
    MigrateError,
    MigrateOptions,
    execute_migration,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"

EXPECTED_STEPS = [
    "baseline",
    "services",
    "ttl-lower",
    "build:crm-backend",
    "deploy:crm-backend",
    "domains-http",
    "parallel-run",
    "dns-verify",
    "domains-tls",
    "inspect",
    "audit",
    "verify-routes",
    "rollback-window",
]


def _config(tmp_path: Path) -> AgentConfig:
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
    )


def _options(tmp_path: Path, *, execute: bool = False) -> MigrateOptions:
    return MigrateOptions(
        plan_file=tmp_path / "plan.json",
        builds={"crm-backend": "main"},
        releases={},
        environment_files={},
        execute=execute,
    )


def _runners(
    calls: list[str],
    failing: Mapping[str, MigrateError] | None = None,
) -> dict[str, Callable[[], dict[str, object]]]:
    failures = dict(failing or {})

    def _runner(step_id: str) -> Callable[[], dict[str, object]]:
        def _run() -> dict[str, object]:
            calls.append(step_id)
            failure = failures.get(step_id)
            if failure is not None:
                raise failure
            return {"step": step_id}

        return _run

    return {step_id: _runner(step_id) for step_id in EXPECTED_STEPS}


def test_preview_shows_the_computed_plan(tmp_path: Path) -> None:
    result = execute_migration(_config(tmp_path), _options(tmp_path))

    assert result["status"] == "plan"
    steps = result["steps"]
    assert isinstance(steps, list)
    assert [step["id"] for step in steps] == EXPECTED_STEPS
    assert result["next"] == "baseline"
    assert not (tmp_path / "plan.json").exists()


def test_components_without_a_ref_or_release_are_rejected(
    tmp_path: Path,
) -> None:
    options = MigrateOptions(
        plan_file=tmp_path / "plan.json",
        builds={},
        releases={},
        environment_files={},
    )

    with pytest.raises(MigrateError) as error:
        execute_migration(_config(tmp_path), options)

    assert error.value.code == "migrate_component_unbuilt"
    assert "crm-backend" in error.value.detail


def test_unknown_component_references_are_rejected(tmp_path: Path) -> None:
    options = MigrateOptions(
        plan_file=tmp_path / "plan.json",
        builds={"crm-backend": "main", "billing": "main"},
        releases={},
        environment_files={},
    )

    with pytest.raises(MigrateError) as error:
        execute_migration(_config(tmp_path), options)

    assert error.value.code == "migrate_component_unknown"
    assert "billing" in error.value.detail


def test_failed_steps_persist_progress_and_resume(tmp_path: Path) -> None:
    config = _config(tmp_path)
    options = _options(tmp_path, execute=True)
    first_calls: list[str] = []
    failure = MigrateError("migrate_step_failed", "deployment exploded")

    first = execute_migration(
        config,
        options,
        runners=_runners(first_calls, {"deploy:crm-backend": failure}),
    )

    assert first["status"] == "error"
    assert first["step"] == "deploy:crm-backend"
    assert first["completed"] == 4
    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    completed = [
        step["id"] for step in plan["steps"] if step["status"] == "completed"
    ]
    assert completed == [
        "baseline",
        "services",
        "ttl-lower",
        "build:crm-backend",
    ]

    second_calls: list[str] = []
    second = execute_migration(config, options, runners=_runners(second_calls))

    assert second["status"] == "ok"
    assert second["completed"] == len(EXPECTED_STEPS)
    assert "baseline" not in second_calls
    assert second_calls[0] == "deploy:crm-backend"


def test_dns_verification_failure_pauses_the_plan(tmp_path: Path) -> None:
    config = _config(tmp_path)
    options = _options(tmp_path, execute=True)
    calls: list[str] = []
    pause = MigrateError(
        "migrate_dns_unverified", "point DNS at the proxy server"
    )

    result = execute_migration(
        config, options, runners=_runners(calls, {"dns-verify": pause})
    )

    assert result["status"] == "paused"
    assert result["step"] == "dns-verify"
    assert result["next"] == "dns-verify"


def test_a_stale_plan_requires_an_explicit_restart(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[str] = []
    execute_migration(
        config, _options(tmp_path, execute=True), runners=_runners(calls)
    )
    pinned = MigrateOptions(
        plan_file=tmp_path / "plan.json",
        builds={},
        releases={"crm-backend": "20260101T000000Z-abcdef0"},
        environment_files={},
        execute=True,
    )

    with pytest.raises(MigrateError) as error:
        execute_migration(config, pinned, runners=_runners(calls))

    assert error.value.code == "migrate_plan_stale"


def test_cli_migrate_previews_without_executing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "migrate",
            "--project",
            str(EXAMPLES),
            "--schemas",
            str(SCHEMAS),
            "--engine",
            str(ENGINE),
            "--plan-file",
            str(tmp_path / "plan.json"),
            "--build",
            "crm-backend=main",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["data"]["status"] == "plan"
    assert payload["data"]["effect"] == "noop"
    assert payload["data"]["next"] == "baseline"


def test_cli_migrate_rejects_malformed_component_pairs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "migrate",
            "--project",
            str(EXAMPLES),
            "--schemas",
            str(SCHEMAS),
            "--plan-file",
            str(tmp_path / "plan.json"),
            "--build",
            "crm-backend",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"]["code"] == "ARG_ERROR"
    assert "COMPONENT" in payload["error"]["message"] or "NAME=VALUE" in (
        payload["error"]["message"]
    )
