"""Resumable migration orchestrator tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cloudfall.agent_tools import AgentConfig
from cloudfall.cli import main
from cloudfall.domain import (
    ConnectionAddress,
    Hostname,
    Ipv4Address,
    Ipv6NetworkCidr,
)
from cloudfall.inventory import PlatformInventory, ServerNetwork
from cloudfall.migrate import (
    MigrateError,
    MigrateOptions,
    _expected_addresses,
    _resolve_names,
    execute_migration,
)
from cloudfall.validation import validate_config

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from cloudfall.inventory import ServerInventory

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
    assert payload["data"]["effect"] == "would_update"
    assert payload["meta"]["dry_run"] is True
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


@pytest.mark.parametrize(
    ("flags", "code"),
    [
        (["--data-schema", "crm=public"], "migrate_data_schema_without_data"),
        (
            ["--data", "crm=SOURCE", "--data-schema", "crm=Bad;Name"],
            "data_copy_schema_invalid",
        ),
        (
            [
                "--data",
                "crm=SOURCE",
                "--data-schema",
                "crm=app",
                "--data-schema",
                "crm=app",
            ],
            "data_copy_schema_invalid",
        ),
    ],
)
def test_s007_12_cli_migrate_refuses_a_bad_data_schema_before_any_playbook(
    flags: list[str],
    code: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source.url"
    source.write_text("postgresql://u:p@db.example.test/crm\n", encoding="utf-8")

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
            "--inventory-file",
            str(tmp_path / "inventory.json"),
            "--build",
            "crm-backend=main",
            *(flag.replace("SOURCE", str(source)) for flag in flags),
            "--yes",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["code"] == "PRECONDITION"
    assert payload["error"]["context"]["code"] == code
    assert "u:p@" not in json.dumps(payload)
    assert not (tmp_path / "plan.json").exists()
    assert not (tmp_path / "inventory.json").exists()


@pytest.mark.parametrize("step_id", ["audit", "verify-routes"])
def test_cli_migrate_names_missing_observations_at_the_verify_step(
    step_id: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A verify step without server snapshots fails typed, not crashed (#183)."""
    options = _options(tmp_path, execute=True)
    # Every step before this one completes; the CLI run resumes here.
    stop = MigrateError("migrate_step_failed", "stopped for the test")
    execute_migration(
        _config(tmp_path),
        options,
        runners=_runners([], {step_id: stop}),
    )
    observed = tmp_path / "observed"

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
            str(options.plan_file),
            "--observed",
            str(observed),
            "--service-observed",
            str(tmp_path / "observed-services"),
            "--build",
            "crm-backend=main",
            "--yes",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] != "HANDLER_CRASHED"
    assert payload["error"]["code"] == "ENGINE_STEP_FAILED"
    assert exit_code == 87
    context = payload["error"]["context"]
    assert context["step"] == step_id
    assert context["code"] == "migrate_observations_missing"
    assert str(observed) in context["message"]
    assert "cloudfall observe" in context["message"]
    assert payload["data"]["next"] == step_id
    plan = json.loads(options.plan_file.read_text(encoding="utf-8"))
    pending = [
        step["id"] for step in plan["steps"] if step["status"] == "pending"
    ]
    assert pending[0] == step_id


# dns-verify's proxy addresses, pinned as they were before domains apply.


def _proxy_server(address: str) -> ServerInventory:
    state = validate_config(EXAMPLES, SCHEMAS)
    (server,) = (
        server
        for server in PlatformInventory.from_state(state).servers
        if server.resource_id.value == "h2"
    )
    return replace(
        server,
        address=ConnectionAddress(address),
        network=ServerNetwork(
            ipv4=Ipv4Address("192.0.2.10"),
            ipv6_cidr=Ipv6NetworkCidr("2001:db8::/64"),
            reverse_dns=Hostname("h2.example.test"),
        ),
    )


def test_dns_verify_counts_no_address_for_an_unresolvable_server_hostname() -> None:
    """A hostname that does not resolve adds nothing and raises nothing.

    ``.invalid`` never resolves (RFC 6761): offline the lookup fails, online
    it answers NXDOMAIN, and both are an ``OSError`` dns-verify swallows.
    """
    server = _proxy_server("h2.cloudfall.invalid")

    assert _resolve_names("h2.cloudfall.invalid") == set()
    assert _expected_addresses(server) == {"192.0.2.10"}


def test_dns_verify_keeps_a_scoped_link_local_address_as_written() -> None:
    server = _proxy_server("fe80::1%eth0")

    assert _expected_addresses(server) == {"fe80::1%eth0", "192.0.2.10"}
