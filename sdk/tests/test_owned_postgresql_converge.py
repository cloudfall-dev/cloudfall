"""Converging a PostgreSQL database owned by a declared role (S-006).

``services.yml`` needs real hosts, so these tests run every path that starts
it (the lifecycle call the MCP tool, the operator, and ``cloudfall migrate``
share) with a recording step runner: it reads the staged password file the
way the role's copy task does, and the tests grep every output for the value.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
import yaml
from cloudfall.agent_tools import AgentConfig
from cloudfall.cli import main
from cloudfall.domain import ResourceId
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import (
    ERROR_SERVICES_SECRET,
    EngineContext,
    ExecutionStep,
    LifecycleError,
    converge_services,
)
from cloudfall.migrate import MigrateError, MigrateOptions, execute_migration
from cloudfall.operator import (
    OperationKind,
    ProposalStatus,
    engine_executor,
    propose_for_drift,
)
from cloudfall.secrets import PASSWORD_FILES_VARIABLE, SopsSecretProvider
from cloudfall.validation import ConfigValidationError, validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import TextContent

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ENGINE = ROOT / "engine"
PASSWORD = "s3cr3t-billing-value"  # noqa: S105 - the value every output is grepped for
PASSWORD_SHA256 = hashlib.sha256(PASSWORD.encode()).hexdigest()
PASSWORD_REF = {
    "environment": "production",
    "path": "/billing/postgresql",
    "key": "BILLING_PASSWORD",
}


def _project(tmp_path: Path) -> Path:
    """One Server, its server type, and the owned PostgreSQL service of S-006-1."""
    project = tmp_path / "project"
    for kind in ("servers", "server-types", "services"):
        (project / kind).mkdir(parents=True)
    shutil.copy(EXAMPLES / "servers" / "h1.yaml", project / "servers" / "h1.yaml")
    shutil.copy(
        EXAMPLES / "server-types" / "debian-application.yaml",
        project / "server-types" / "debian-application.yaml",
    )
    service = {
        "apiVersion": "cloudfall/v1",
        "kind": "Service",
        "metadata": {"id": "postgresql-billing"},
        "spec": {
            "serviceKind": "postgresql",
            "environment": "production",
            "server": "h1",
            "bind": {"address": "127.0.0.1", "port": 5432},
            "postgresql": {
                "majorVersion": "17",
                "databases": [
                    {"name": "billing", "owner": "billing", "passwordRef": PASSWORD_REF}
                ],
            },
            "backup": {
                "directory": "/var/backups/cloudfall/postgresql-billing",
                "onCalendar": "*-*-* 02:00:00 UTC",
                "retentionDays": 14,
            },
        },
    }
    (project / "services" / "postgresql-billing.yaml").write_text(
        yaml.safe_dump(service, sort_keys=False), encoding="utf-8"
    )
    return project


def _secrets(project: Path, content: str) -> Path:
    secrets = project / "secrets"
    source = secrets / "production" / "billing" / "postgresql.env"
    source.parent.mkdir(parents=True)
    source.write_text(content, encoding="utf-8")
    return secrets


def _plaintext(source: Path) -> str:
    """Stand in for sops: the test's secret files are not encrypted."""
    return source.read_text(encoding="utf-8")


@dataclass
class Recorder:
    """Steps a converge started, and what its staged files held mid-run."""

    steps: list[ExecutionStep] = field(default_factory=list)
    staged: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    paths: list[Path] = field(default_factory=list)

    def __call__(self, step: ExecutionStep) -> Finished:
        """Record the step and read every password file it was handed."""
        self.steps.append(step)
        if "--extra-vars" in step.argv:
            extra = json.loads(step.argv[step.argv.index("--extra-vars") + 1])
            files = cast(
                "dict[str, dict[str, str]]", extra.get(PASSWORD_FILES_VARIABLE, {})
            )
            for service, owners in files.items():
                for owner, raw in owners.items():
                    path = Path(raw)
                    self.paths.append(path)
                    self.staged[f"{service}/{owner}"] = (
                        path.read_text(encoding="utf-8"),
                        stat.S_IMODE(path.stat().st_mode),
                        stat.S_IMODE(path.parent.stat().st_mode),
                    )
        return Finished()


@dataclass(frozen=True)
class Finished:
    """A step that exited 0 and printed nothing."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _context(project: Path, recorder: Recorder) -> EngineContext:
    return EngineContext(
        project_directory=project,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=project / "tmp" / "ansible-inventory.json",
        run=recorder,
    )


def _provider(secrets: Path) -> SopsSecretProvider:
    return SopsSecretProvider(secrets_directory=secrets, decrypt=_plaintext)


def _everything_written(project: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in project.rglob("*")
        if path.is_file() and "secrets" not in path.parts
    )


# proves: S-006-6
def test_s006_6_converge_stages_the_password_and_answers_its_hash(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    secrets = _secrets(project, f"OTHER=x\nBILLING_PASSWORD={PASSWORD}\n")
    recorder = Recorder()

    passwords = converge_services(_context(project, recorder), _provider(secrets))

    render, playbook = recorder.steps
    assert playbook.argv[-1].endswith("services.yml")
    assert recorder.staged == {
        "postgresql-billing/billing": (PASSWORD, 0o600, 0o700)
    }
    (staged,) = recorder.paths
    assert not staged.exists()
    assert not staged.parent.exists()
    assert project not in staged.parents
    (answer,) = [password.as_dict() for password in passwords]
    assert answer == {
        "service": "postgresql-billing",
        "owner": "billing",
        "passwordRef": PASSWORD_REF,
        "key": "BILLING_PASSWORD",
        "sha256": PASSWORD_SHA256,
    }
    assert PASSWORD not in repr(passwords)
    for step in (render, playbook):
        assert PASSWORD not in " ".join(step.argv)
        assert PASSWORD not in json.dumps(dict(step.environment))
    assert PASSWORD not in _everything_written(project)


# proves: S-006-6
def test_s006_6_the_rendered_inventory_carries_the_reference_only(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _secrets(project, f"BILLING_PASSWORD={PASSWORD}\n")

    inventory = render_ansible_inventory(
        PlatformInventory.from_state(validate_config(project, SCHEMAS))
    )

    rendered = json.dumps(inventory)
    assert PASSWORD not in rendered
    assert '"passwordRef": {"environment": "production"' in rendered


# proves: S-006-7
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, "production/billing/postgresql.env"),
        ("OTHER=x\n", "BILLING_PASSWORD is missing from"),
    ],
    ids=["missing-file", "missing-key"],
)
def test_s006_7_a_missing_file_or_key_stops_before_any_playbook(
    tmp_path: Path, content: str | None, expected: str
) -> None:
    project = _project(tmp_path)
    secrets = (
        project / "secrets" if content is None else _secrets(project, content)
    )
    recorder = Recorder()

    with pytest.raises(LifecycleError) as error:
        converge_services(_context(project, recorder), _provider(secrets))

    assert error.value.code == ERROR_SERVICES_SECRET
    assert expected in error.value.detail
    assert "BILLING_PASSWORD" in error.value.detail
    assert str(secrets / "production" / "billing" / "postgresql.env") in (
        error.value.detail
    )
    assert recorder.steps == []


def test_an_empty_password_is_refused_like_a_missing_key(tmp_path: Path) -> None:
    project = _project(tmp_path)
    secrets = _secrets(project, "BILLING_PASSWORD=\n")
    recorder = Recorder()

    with pytest.raises(LifecycleError, match="holds an empty value"):
        converge_services(_context(project, recorder), _provider(secrets))

    assert recorder.steps == []


def test_a_project_without_an_owned_database_converges_as_before(
    tmp_path: Path,
) -> None:
    recorder = Recorder()
    context = EngineContext(
        project_directory=EXAMPLES,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=tmp_path / "inventory.json",
        run=recorder,
    )

    passwords = converge_services(context, _provider(tmp_path / "no-secrets"))

    assert passwords == ()
    _, playbook = recorder.steps
    assert json.loads(playbook.argv[playbook.argv.index("--extra-vars") + 1]) == {}


# proves: S-006-7
def test_s006_7_the_operator_marks_the_proposal_failed_and_runs_nothing(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    recorder = Recorder()
    execute = engine_executor(
        _context(project, recorder), _provider(project / "secrets")
    )
    proposal = propose_for_drift(
        ResourceId("h1"),
        ("services.bind[postgresql-billing]",),
        OperationKind.CONVERGE_SERVICES,
        "2026-10-27T10:00:00Z",
    )

    with pytest.raises(LifecycleError) as error:
        execute(proposal)

    assert error.value.code == ERROR_SERVICES_SECRET
    assert "BILLING_PASSWORD" in error.value.detail
    assert recorder.steps == []
    assert proposal.status is ProposalStatus.PROPOSED


# proves: S-006-6
def test_s006_6_the_operator_converge_stages_the_password(tmp_path: Path) -> None:
    project = _project(tmp_path)
    secrets = _secrets(project, f"BILLING_PASSWORD={PASSWORD}\n")
    recorder = Recorder()
    execute = engine_executor(_context(project, recorder), _provider(secrets))

    execute(
        propose_for_drift(
            ResourceId("h1"),
            ("services.bind[postgresql-billing]",),
            OperationKind.CONVERGE_SERVICES,
            "2026-10-27T10:00:00Z",
        )
    )

    assert recorder.staged["postgresql-billing/billing"][0] == PASSWORD
    assert all(PASSWORD not in " ".join(step.argv) for step in recorder.steps)


def _agent_config(project: Path, recorder: Recorder, secrets: Path) -> AgentConfig:
    return AgentConfig(
        project_directory=project,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=project / "tmp" / "inventory.json",
        observed_directory=project / "tmp" / "observed",
        service_observed_directory=project / "tmp" / "observed-services",
        deployments_directory=project / "tmp" / "deployments",
        releases_directory=project / "tmp" / "releases",
        artifacts_directory=project / "tmp" / "artifacts",
        secrets_directory=secrets,
        run=recorder,
        decrypt=_plaintext,
    )


def _migrate_options(project: Path) -> MigrateOptions:
    return MigrateOptions(
        plan_file=project / "tmp" / "plan.json",
        builds={},
        releases={},
        environment_files={},
        execute=True,
    )


# proves: S-006-7
def test_s006_7_migrate_stops_before_the_baseline_playbook(tmp_path: Path) -> None:
    project = _project(tmp_path)
    recorder = Recorder()
    config = _agent_config(project, recorder, project / "secrets")

    with pytest.raises(MigrateError) as error:
        execute_migration(config, _migrate_options(project))

    assert error.value.code == ERROR_SERVICES_SECRET
    assert "BILLING_PASSWORD" in error.value.detail
    assert recorder.steps == []


# proves: S-006-6
def test_s006_6_migrate_records_the_hash_in_its_services_step(tmp_path: Path) -> None:
    project = _project(tmp_path)
    secrets = _secrets(project, f"BILLING_PASSWORD={PASSWORD}\n")
    recorder = Recorder()

    # Every playbook "passes"; the run stops at the audit, which reads
    # snapshots no stand-in wrote, after the services step was saved.
    with pytest.raises(ConfigValidationError, match="observation directory"):
        execute_migration(
            _agent_config(project, recorder, secrets), _migrate_options(project)
        )

    plan = (project / "tmp" / "plan.json").read_text(encoding="utf-8")
    services = next(
        step
        for step in cast("list[dict[str, object]]", json.loads(plan)["steps"])
        if step["id"] == "services"
    )
    assert services["status"] == "completed"
    assert services["detail"] == {
        "playbook": "services.yml",
        "ownedRoles": [
            {
                "service": "postgresql-billing",
                "owner": "billing",
                "passwordRef": PASSWORD_REF,
                "key": "BILLING_PASSWORD",
                "sha256": PASSWORD_SHA256,
            }
        ],
    }
    assert recorder.staged["postgresql-billing/billing"][0] == PASSWORD
    assert PASSWORD not in plan
    assert all(PASSWORD not in " ".join(step.argv) for step in recorder.steps)


# proves: S-006-7
def test_s006_7_cli_migrate_answers_precondition_naming_path_and_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)

    exit_code = main(
        [
            "migrate",
            "--project",
            str(project),
            "--schemas",
            str(SCHEMAS),
            "--engine",
            str(ENGINE),
            "--secrets-dir",
            "private",
            "--yes",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "PRECONDITION", payload
    assert exit_code != 0
    message = payload["error"]["message"]
    assert str(project / "private" / "production" / "billing" / "postgresql.env") in (
        message
    )
    assert "BILLING_PASSWORD" in message
    assert not (project / "tmp" / "plan.json").exists()


async def _converge_over_mcp(project: Path) -> dict[str, object]:
    server = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "from cloudfall.cli import run; run()",
            "mcp",
            "serve",
            "--project",
            str(project),
            "--secrets-dir",
            "private",
        ],
        cwd=str(project),
    )
    async with (
        stdio_client(server) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "converge_services", {"confirm_destructive": True}
        )
        content = result.content[0]
        assert isinstance(content, TextContent)
        answer = json.loads(content.text)
        assert isinstance(answer, dict)
        return answer


# proves: S-006-7
def test_s006_7_the_converge_services_tool_answers_precondition(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)

    answer = asyncio.run(_converge_over_mcp(project))

    error = cast("dict[str, object]", answer["error"])
    assert error["code"] == "PRECONDITION", answer
    message = str(error["message"])
    assert str(project / "private" / "production" / "billing" / "postgresql.env") in (
        message
    )
    assert "BILLING_PASSWORD" in message
    assert not (project / "tmp" / "ansible-inventory.json").exists()
