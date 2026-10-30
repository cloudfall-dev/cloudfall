"""A component's ``healthCheck`` of ``type: command`` (S-009-1, S-009-13).

The config contract and the plans only: the engine running the check is
cloudfall-dev/cloudfall#164. The engine directory the CLI is pointed at is a
stand-in whose playbooks only leave a marker file, so a test sees whether
``ansible-playbook`` ran at all without contacting any host.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest
from cloudfall.cli import main
from cloudfall.domain import (
    CommandArgv,
    HealthCheckAttempts,
    HealthCheckTimeout,
    HealthCheckType,
)
from cloudfall.inventory import (
    ComponentCommandHealthCheck,
    ComponentHealthCheck,
    PlatformInventory,
)
from cloudfall.validation import validate_config

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"
RELEASE = "20260101T000000Z-abcdef0"
DECLARED_HTTP_CHECK = (
    "  healthCheck:\n"
    "    type: http\n"
    "    scheme: http\n"
    "    port: 8100\n"
    "    path: /health\n"
    "    expectedStatuses:\n"
    "      - 200\n"
    "    timeoutSeconds: 5\n"
    "    attempts: 5\n"
)
COMMAND_CHECK: dict[str, object] = {
    "type": "command",
    "command": [".venv/bin/python", "-m", "app.smoke"],
    "timeoutSeconds": 60,
    "attempts": 3,
}


def _project(tmp_path: Path, health_check: object) -> Path:
    """Copy the examples with crm-backend's ``healthCheck`` replaced."""
    project = tmp_path / "config"
    shutil.copytree(EXAMPLES, project)
    path = project / "components" / "crm-backend.yaml"
    text = path.read_text(encoding="utf-8")
    assert DECLARED_HTTP_CHECK in text
    path.write_text(
        text.replace(
            DECLARED_HTTP_CHECK, f"  healthCheck: {json.dumps(health_check)}\n"
        ),
        encoding="utf-8",
    )
    return project


def _validate(project: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, Any]:
    exit_code = main(
        ["config", "validate", "--project", str(project), "--schemas", str(SCHEMAS)]
    )
    return exit_code, json.loads(capsys.readouterr().out)


def test_s009_1_config_validate_accepts_a_command_health_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, payload = _validate(_project(tmp_path, COMMAND_CHECK), capsys)

    assert exit_code == 0, payload
    assert payload["ok"] is True


@pytest.mark.parametrize(
    "health_check",
    [
        {
            **COMMAND_CHECK,
            "command": ["/usr/bin/true"],
            "timeoutSeconds": 1,
            "attempts": 1,
        },
        {**COMMAND_CHECK, "timeoutSeconds": 300, "attempts": 20},
    ],
    ids=["lower-bounds", "upper-bounds"],
)
def test_s009_1_config_validate_accepts_the_bounds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], health_check: object
) -> None:
    exit_code, payload = _validate(_project(tmp_path, health_check), capsys)

    assert exit_code == 0, payload


def _without(key: str) -> dict[str, object]:
    return {name: value for name, value in COMMAND_CHECK.items() if name != key}


@pytest.mark.parametrize(
    "health_check",
    [
        {**COMMAND_CHECK, "command": []},
        {**COMMAND_CHECK, "command": [""]},
        {**COMMAND_CHECK, "command": [".venv/bin/python", ""]},
        {**COMMAND_CHECK, "command": "app.smoke"},
        {**COMMAND_CHECK, "command": ["bad\u0000arg"]},
        _without("command"),
        _without("timeoutSeconds"),
        _without("attempts"),
        {**COMMAND_CHECK, "timeoutSeconds": 0},
        {**COMMAND_CHECK, "timeoutSeconds": 301},
        {**COMMAND_CHECK, "timeoutSeconds": "60"},
        {**COMMAND_CHECK, "attempts": 0},
        {**COMMAND_CHECK, "attempts": 21},
        {**COMMAND_CHECK, "attempts": True},
        {**COMMAND_CHECK, "port": 8100},
        {**COMMAND_CHECK, "scheme": "http"},
        {**COMMAND_CHECK, "path": "/health"},
        {**COMMAND_CHECK, "expectedStatuses": [200]},
        {**COMMAND_CHECK, "workingDirectory": "services/api"},
    ],
    ids=[
        "empty-command",
        "empty-argument",
        "empty-later-argument",
        "string-command",
        "nul-in-argument",
        "missing-command",
        "missing-timeout",
        "missing-attempts",
        "timeout-0",
        "timeout-301",
        "timeout-string",
        "attempts-0",
        "attempts-21",
        "attempts-boolean",
        "http-port",
        "http-scheme",
        "http-path",
        "http-expected-statuses",
        "unknown-key",
    ],
)
def test_s009_1_config_validate_refuses_a_malformed_command_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], health_check: object
) -> None:
    exit_code, payload = _validate(_project(tmp_path, health_check), capsys)

    assert exit_code == 80, payload
    assert payload["error"]["code"] == "CONFIG_INVALID"
    assert payload["error"]["context"]["code"] == "schema_validation_failed"


def test_s009_1_the_command_check_parses_into_value_objects(tmp_path: Path) -> None:
    inventory = PlatformInventory.from_state(
        validate_config(_project(tmp_path, COMMAND_CHECK), SCHEMAS)
    )
    health_check = inventory.components[0].health_check

    assert health_check == ComponentHealthCheck(
        check_type=HealthCheckType.COMMAND,
        http=None,
        command=ComponentCommandHealthCheck(
            command=CommandArgv((".venv/bin/python", "-m", "app.smoke")),
            timeout_seconds=HealthCheckTimeout(60),
            attempts=HealthCheckAttempts(3),
        ),
    )
    assert health_check.as_dict() == COMMAND_CHECK


@pytest.mark.parametrize("value", [0, 301, True])
def test_s009_1_the_timeout_refuses_values_outside_1_to_300(value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        HealthCheckTimeout.from_boundary(value)


@pytest.mark.parametrize("value", [0, 21, True, None])
def test_s009_1_attempts_refuse_values_outside_1_to_20(value: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        HealthCheckAttempts.from_boundary(value)


def test_s009_1_a_check_carries_exactly_the_contract_of_its_type() -> None:
    command = ComponentCommandHealthCheck(
        command=CommandArgv(("/usr/bin/true",)),
        timeout_seconds=HealthCheckTimeout(1),
        attempts=HealthCheckAttempts(1),
    )
    with pytest.raises(ValueError, match="command contract"):
        ComponentHealthCheck(check_type=HealthCheckType.COMMAND, http=None)
    with pytest.raises(ValueError, match="command contract"):
        ComponentHealthCheck(
            check_type=HealthCheckType.NONE, http=None, command=command
        )


def test_s009_1_http_and_none_checks_keep_their_shape(tmp_path: Path) -> None:
    """Q-3: the existing checks parse and serialize exactly as before."""
    examples = PlatformInventory.from_state(validate_config(EXAMPLES, SCHEMAS))
    http = examples.components[0].health_check
    none = PlatformInventory.from_state(
        validate_config(_project(tmp_path, {"type": "none"}), SCHEMAS)
    ).components[0].health_check

    assert http.command is None
    assert http.as_dict() == {
        "type": "http",
        "scheme": "http",
        "port": 8100,
        "path": "/health",
        "expectedStatuses": [200],
        "timeoutSeconds": 5,
        "attempts": 5,
    }
    assert none.command is None
    assert none.as_dict() == {"type": "none"}


def _marker_engine(tmp_path: Path) -> Path:
    """Lay out an engine whose playbooks only write a marker."""
    engine = tmp_path / "engine"
    playbooks = engine / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    shutil.copy(ENGINE / "ansible" / "ansible.cfg", engine / "ansible")
    for name in ("deploy.yml", "rollback.yml", "restart.yml"):
        marker = tmp_path / f"ran-{name}"
        (playbooks / name).write_text(
            "---\n- name: Leave a marker\n  hosts: localhost\n"
            "  connection: local\n  gather_facts: false\n  tasks:\n"
            "    - name: Write the marker\n      ansible.builtin.copy:\n"
            f"        dest: {marker}\n        content: ran\n        mode: '0600'\n",
            encoding="utf-8",
        )
    return engine


def _write_artifact(tmp_path: Path) -> Path:
    artifact_directory = tmp_path / "artifacts"
    component_directory = artifact_directory / "crm-backend"
    component_directory.mkdir(parents=True)
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print('crm')\n", encoding="utf-8")
    archive_path = component_directory / f"{RELEASE}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source, arcname=".")
    metadata = {
        "apiVersion": "cloudfall/v1",
        "kind": "Artifact",
        "metadata": {"id": "crm-backend"},
        "spec": {
            "component": "crm-backend",
            "release": RELEASE,
            "gitRef": "main",
            "gitCommit": "a" * 40,
            "builtAt": "2026-01-01T00:00:00Z",
            "archive": archive_path.name,
            "archiveSha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            "sizeBytes": archive_path.stat().st_size,
        },
    }
    (component_directory / f"{RELEASE}.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    return artifact_directory


def _plan(
    action: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    health_check: object,
) -> dict[str, Any]:
    arguments = {
        "deploy": [
            "--release",
            RELEASE,
            "--artifacts",
            str(_write_artifact(tmp_path)),
        ],
        "rollback": ["--release", RELEASE],
        "restart": [],
    }[action]
    exit_code = main(
        [
            action,
            "crm-backend",
            *arguments,
            "--project",
            str(_project(tmp_path, health_check)),
            "--schemas",
            str(SCHEMAS),
            "--engine",
            str(_marker_engine(tmp_path)),
            "--inventory-file",
            str(tmp_path / "inventory.json"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0, payload
    assert not any(tmp_path.glob("ran-*"))
    assert not (tmp_path / "inventory.json").exists()
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["status"] == "plan"
    return data


@pytest.mark.parametrize("action", ["deploy", "rollback", "restart"])
def test_s009_13_the_plan_names_the_command_health_check(
    action: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = _plan(action, tmp_path, capsys, COMMAND_CHECK)

    assert (
        "command health check .venv/bin/python -m app.smoke runs on each server "
        "after its restart"
    ) in data["would_run"]


@pytest.mark.parametrize("action", ["deploy", "rollback", "restart"])
@pytest.mark.parametrize(
    "health_check",
    [
        {
            "type": "http",
            "scheme": "http",
            "port": 8100,
            "path": "/health",
            "expectedStatuses": [200],
            "timeoutSeconds": 5,
            "attempts": 5,
        },
        {"type": "none"},
    ],
    ids=["http", "none"],
)
def test_s009_13_the_plan_gains_no_field(
    action: str,
    health_check: object,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command check changes only ``would_run``; other checks change nothing."""
    other = _plan(action, tmp_path / "other", capsys, health_check)
    command = _plan(action, tmp_path / "command", capsys, COMMAND_CHECK)

    assert set(command) == set(other)
    assert "command health check" not in other["would_run"]
