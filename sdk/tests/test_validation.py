"""State validation acceptance tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from cloudfall.cli import main
from cloudfall.domain import ResourceId, ResourceKind
from cloudfall.validation import ConfigValidationError, validate_config

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"


def test_example_state_validates_and_builds_typed_index() -> None:
    state = validate_config(ROOT / "config" / "examples", SCHEMAS)

    assert state.resource_count == 12
    assert state.counts_by_kind() == {
        "AlertRule": 1,
        "OperatorPolicy": 1,
        "Component": 1,
        "Domain": 1,
        "ServerType": 1,
        "LoggingStack": 1,
        "Application": 1,
        "Server": 2,
        "Service": 2,
        "SshPublicKey": 1,
    }
    component = state.get(ResourceKind.COMPONENT, ResourceId("crm-backend"))
    assert component.key.resource_id == ResourceId("crm-backend")
    logging = state.get(ResourceKind.LOGGING_STACK, ResourceId("operations"))
    assert logging.key.resource_id == ResourceId("operations")


def test_unknown_component_field_fails_schema_validation() -> None:
    invalid_state = ROOT / "config" / "tests" / "invalid" / "schema"

    with pytest.raises(ConfigValidationError) as error:
        validate_config(invalid_state, SCHEMAS)

    assert error.value.issue.code == "schema_validation_failed"
    assert "unsupportedField" in error.value.issue.message


def test_missing_server_reference_fails_semantic_validation() -> None:
    invalid_state = ROOT / "config" / "tests" / "invalid" / "missing-server"

    with pytest.raises(ConfigValidationError) as error:
        validate_config(invalid_state, SCHEMAS)

    assert error.value.issue.code == "resource_reference_missing"
    assert "Server/h9" in error.value.issue.message


def test_component_install_root_must_stay_below_application_root(
    tmp_path: Path,
) -> None:
    project_directory = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "examples", project_directory)
    component_path = project_directory / "components" / "crm-backend.yaml"
    component = component_path.read_text(encoding="utf-8").replace(
        "/srv/apps/crm/backend", "/opt/crm/backend"
    )
    component_path.write_text(component, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "component_install_root_invalid"


@pytest.mark.parametrize(
    ("original", "replacement", "field"),
    [
        ("type: python", "type: node", "type"),
        ("type: python", "type: pm2", "type"),
        ("packageManager: uv", "packageManager: npm", "packageManager"),
        ("packageManager: uv", "packageManager: pnpm", "packageManager"),
        ("packageManager: uv", "packageManager: yarn", "packageManager"),
    ],
    ids=["node", "pm2", "npm", "pnpm", "yarn"],
)
def test_s002_14_reserved_runtimes_are_refused_as_config_invalid(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    original: str,
    replacement: str,
    field: str,
) -> None:
    project_directory = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "examples", project_directory)
    component_path = project_directory / "components" / "crm-backend.yaml"
    component_path.write_text(
        component_path.read_text(encoding="utf-8").replace(original, replacement, 1),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "config",
            "validate",
            "--project",
            str(project_directory),
            "--schemas",
            str(SCHEMAS),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 80
    assert payload["error"]["code"] == "CONFIG_INVALID"
    context = payload["error"]["context"]
    assert context["code"] == "component_runtime_unsupported"
    assert context["field"] == f"spec.runtime.{field}"
    assert f"runtime.{field}" in context["message"]


def test_server_type_rejects_duplicate_configuration_paths(
    tmp_path: Path,
) -> None:
    project_directory = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "examples", project_directory)
    server_type_path = project_directory / "server-types" / "debian-application.yaml"
    duplicated = server_type_path.read_text(encoding="utf-8") + (
        "      - path: /etc/ssh/sshd_config\n        capture: metadata\n"
    )
    server_type_path.write_text(duplicated, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "server_type_entry_duplicate"


def test_ssh_public_key_algorithm_must_match_wire_payload(
    tmp_path: Path,
) -> None:
    project_directory = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "examples", project_directory)
    key_directory = project_directory / "ssh-public-keys"
    example_key = (
        key_directory / "example-admin.yaml"
    ).read_text(encoding="utf-8")
    mismatched = example_key.replace("ssh-rsa", "ssh-ed25519", 1).replace(
        "example-admin", "mismatched"
    )
    (key_directory / "mismatched.yaml").write_text(mismatched, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "ssh_public_key_invalid"


def test_server_type_rejects_duplicate_firewall_rules(
    tmp_path: Path,
) -> None:
    project_directory = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "examples", project_directory)
    server_type_path = project_directory / "server-types" / "debian-application.yaml"
    duplicated = server_type_path.read_text(encoding="utf-8").replace(
        "      - port: 443\n        protocol: tcp\n        description: https",
        "      - port: 80\n        protocol: tcp\n        description: duplicate",
    )
    server_type_path.write_text(duplicated, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "firewall_rule_duplicate"


def test_service_database_must_reference_a_declared_application(
    tmp_path: Path,
) -> None:
    project_directory = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "examples", project_directory)
    service_path = project_directory / "services" / "postgresql-main.yaml"
    invalid = service_path.read_text(encoding="utf-8").replace(
        "application: crm", "application: billing"
    )
    service_path.write_text(invalid, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "resource_reference_missing"
    assert "Application/billing" in error.value.issue.message


def test_service_rejects_duplicate_database_names(tmp_path: Path) -> None:
    project_directory = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "examples", project_directory)
    service_path = project_directory / "services" / "postgresql-main.yaml"
    duplicated = service_path.read_text(encoding="utf-8").replace(
        "      - name: crm\n        application: crm",
        "      - name: crm\n        application: crm\n"
        "      - name: crm\n        application: billing",
    )
    service_path.write_text(duplicated, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "service_database_duplicate"


def test_second_service_of_one_kind_per_server_is_rejected(
    tmp_path: Path,
) -> None:
    project_directory = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "examples", project_directory)
    original = project_directory / "services" / "postgresql-main.yaml"
    second = original.read_text(encoding="utf-8").replace(
        "id: postgresql-main", "id: postgresql-second"
    ).replace("port: 5432", "port: 5433")
    (project_directory / "services" / "postgresql-second.yaml").write_text(
        second, encoding="utf-8"
    )

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "service_placement_conflict"


def test_logging_stack_rejects_conflicting_listener_ports(
    tmp_path: Path,
) -> None:
    project_directory = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "examples", project_directory)
    logging_path = project_directory / "logging-stacks" / "operations.yaml"
    conflicting = logging_path.read_text(encoding="utf-8").replace(
        "port: 9090", "port: 3100", 1
    )
    logging_path.write_text(conflicting, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "logging_port_conflict"


def test_logging_collector_must_reference_an_existing_server(
    tmp_path: Path,
) -> None:
    project_directory = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "examples", project_directory)
    logging_path = project_directory / "logging-stacks" / "operations.yaml"
    invalid = logging_path.read_text(encoding="utf-8").replace(
        "      - h2", "      - h9", 1
    )
    logging_path.write_text(invalid, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "resource_reference_missing"
    assert "Server/h9" in error.value.issue.message


def test_logging_migration_cannot_disable_legacy_agents(
    tmp_path: Path,
) -> None:
    project_directory = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "examples", project_directory)
    logging_path = project_directory / "logging-stacks" / "operations.yaml"
    invalid = logging_path.read_text(encoding="utf-8").replace(
        "preserveLegacyAgents: true", "preserveLegacyAgents: false"
    )
    logging_path.write_text(invalid, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "schema_validation_failed"


def test_logging_secret_paths_stay_in_managed_directory(
    tmp_path: Path,
) -> None:
    project_directory = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "examples", project_directory)
    logging_path = project_directory / "logging-stacks" / "operations.yaml"
    invalid = logging_path.read_text(encoding="utf-8").replace(
        "/etc/cloudfall/logging/server.key", "/var/lib/cloudfall-unsafe/server.key"
    )
    logging_path.write_text(invalid, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project_directory, SCHEMAS)

    assert error.value.issue.code == "logging_secret_path_invalid"


def test_cli_emits_structured_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "config",
            "validate",
            "--project",
            str(ROOT / "config" / "examples"),
            "--schemas",
            str(SCHEMAS),
        ]
    )

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert exit_code == 0
    assert (document["ok"], document["error"], document["warnings"]) == (
        True,
        None,
        [],
    )
    assert document["meta"]["command"] == "config.validate"
    assert document["data"] == {
        "status": "ok",
        "resources": 12,
        "by_kind": {
            "AlertRule": 1,
            "OperatorPolicy": 1,
            "Component": 1,
            "Domain": 1,
            "ServerType": 1,
            "LoggingStack": 1,
            "Application": 1,
            "Server": 2,
            "Service": 2,
            "SshPublicKey": 1,
        },
    }


def test_cli_emits_structured_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid_state = ROOT / "config" / "tests" / "invalid" / "missing-server"
    exit_code = main(
        [
            "config",
            "validate",
            "--project",
            str(invalid_state),
            "--schemas",
            str(SCHEMAS),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 80
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CONFIG_INVALID"
    assert payload["error"]["context"]["code"] == "resource_reference_missing"


def _project_with_release(tmp_path: Path, release: object) -> Path:
    project_directory = tmp_path / "config"
    shutil.copytree(ROOT / "config" / "examples", project_directory)
    component_path = project_directory / "components" / "crm-backend.yaml"
    component = component_path.read_text(encoding="utf-8").replace(
        "  healthCheck:\n",
        f"  release: {json.dumps(release)}\n  healthCheck:\n",
    )
    component_path.write_text(component, encoding="utf-8")
    return project_directory


@pytest.mark.parametrize(
    "release",
    [
        {"command": [".venv/bin/alembic"], "workingDirectory": "/srv/api"},
        {"command": [".venv/bin/alembic"], "workingDirectory": "../api"},
        {"command": [".venv/bin/alembic"], "workingDirectory": "services/../.."},
        {"command": []},
        {"command": [""]},
        {"command": [".venv/bin/alembic"], "timeoutSeconds": 0},
        {"command": [".venv/bin/alembic"], "timeoutSeconds": 1801},
        {"workingDirectory": "services/api"},
    ],
)
def test_s005_11_config_validate_refuses_a_malformed_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], release: object
) -> None:
    project_directory = _project_with_release(tmp_path, release)

    exit_code = main(
        [
            "config",
            "validate",
            "--project",
            str(project_directory),
            "--schemas",
            str(SCHEMAS),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 80
    assert payload["error"]["code"] == "CONFIG_INVALID"
    assert payload["error"]["context"]["code"] == "schema_validation_failed"


@pytest.mark.parametrize(
    "release",
    [
        {"command": [".venv/bin/alembic", "upgrade", "head"]},
        {
            "command": [".venv/bin/alembic", "upgrade", "head"],
            "workingDirectory": "services/api",
            "timeoutSeconds": 1800,
        },
        {"command": ["/usr/bin/true"], "timeoutSeconds": 1},
    ],
)
def test_s005_11_config_validate_accepts_a_declared_release(
    tmp_path: Path, release: object
) -> None:
    state = validate_config(_project_with_release(tmp_path, release), SCHEMAS)

    assert state.counts_by_kind()["Component"] == 1
