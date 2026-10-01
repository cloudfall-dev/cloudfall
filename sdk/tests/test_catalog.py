"""The operations catalog: what an agent may run, and how it is verified."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from cloudfall.catalog import (
    InputType,
    Precondition,
    RiskLevel,
    TargetScope,
    load_catalog,
)
from cloudfall.cli import main
from cloudfall.domain import ResourceId
from cloudfall.resources import default_schema_directory
from cloudfall.validation import ConfigValidationError

SCHEMAS = default_schema_directory()

DEPLOY = """\
id: deploy
description: Deploy one component release
playbook: playbooks/deploy.yml
risk: mutating
targets: host
inputs:
  version: string
  force:
    type: boolean
    required: false
    description: Deploy even when the release is already current
preconditions:
  - health: ok
verify:
  playbook: playbooks/health.yml
"""

FACTS = """\
id: facts
playbook: playbooks/facts.yml
risk: read
targets: fleet
"""


def _repository(tmp_path: Path, **documents: str) -> Path:
    repository = tmp_path / "fleet"
    (repository / "playbooks").mkdir(parents=True)
    for name in ("deploy.yml", "health.yml", "facts.yml"):
        (repository / "playbooks" / name).write_text("---\n[]\n", encoding="utf-8")
    operations = repository / "operations"
    operations.mkdir()
    declared = documents or {"deploy": DEPLOY, "facts": FACTS}
    for name, body in declared.items():
        (operations / f"{name}.yml").write_text(body, encoding="utf-8")
    return repository


def test_the_catalog_is_every_declared_operation_in_order(tmp_path: Path) -> None:
    catalog = load_catalog(_repository(tmp_path), SCHEMAS)

    assert [operation.operation_id.value for operation in catalog.operations] == [
        "deploy",
        "facts",
    ]
    assert catalog.as_dict()["byRisk"] == {
        "read": 1,
        "mutating": 1,
        "destructive": 0,
    }


def test_an_operation_carries_its_risk_targets_and_verify_step(
    tmp_path: Path,
) -> None:
    catalog = load_catalog(_repository(tmp_path), SCHEMAS)

    deploy = catalog.get(ResourceId.from_boundary("deploy"))
    assert deploy.risk is RiskLevel.MUTATING
    assert deploy.targets is TargetScope.HOST
    assert deploy.playbook == Path("playbooks/deploy.yml")
    assert deploy.verify is not None
    assert deploy.verify.playbook == Path("playbooks/health.yml")
    assert deploy.preconditions == (Precondition.HEALTH_OK,)
    assert deploy.description == "Deploy one component release"


def test_an_input_is_a_type_name_or_a_described_object(tmp_path: Path) -> None:
    """The shorthand keeps a simple operation short; the object adds detail."""
    catalog = load_catalog(_repository(tmp_path), SCHEMAS)

    deploy = catalog.get(ResourceId.from_boundary("deploy"))
    assert [declared.as_dict() for declared in deploy.inputs] == [
        {
            "name": "force",
            "type": "boolean",
            "required": False,
            "description": "Deploy even when the release is already current",
        },
        {"name": "version", "type": "string", "required": True},
    ]
    assert deploy.inputs[1].type is InputType.STRING


def test_a_read_operation_needs_no_verify_step(tmp_path: Path) -> None:
    catalog = load_catalog(_repository(tmp_path), SCHEMAS)

    facts = catalog.get(ResourceId.from_boundary("facts"))
    assert facts.risk is RiskLevel.READ
    assert facts.verify is None


def test_a_mutating_operation_without_verify_is_refused(tmp_path: Path) -> None:
    """The verify step is the field that makes a run an outcome."""
    unverified = textwrap.dedent("""\
        id: deploy
        playbook: playbooks/deploy.yml
        risk: mutating
        targets: host
        """)

    with pytest.raises(ConfigValidationError) as error:
        load_catalog(_repository(tmp_path, deploy=unverified), SCHEMAS)

    assert error.value.issue.code == "schema_validation_failed"
    assert "verify" in error.value.issue.message


def test_a_destructive_operation_without_verify_is_refused(tmp_path: Path) -> None:
    unverified = textwrap.dedent("""\
        id: wipe
        playbook: playbooks/deploy.yml
        risk: destructive
        targets: host
        """)

    with pytest.raises(ConfigValidationError) as error:
        load_catalog(_repository(tmp_path, wipe=unverified), SCHEMAS)

    assert error.value.issue.code == "schema_validation_failed"


def test_a_declared_playbook_that_does_not_exist_is_refused(
    tmp_path: Path,
) -> None:
    """A catalog entry an agent cannot run is worse than no entry."""
    absent = DEPLOY.replace("playbooks/deploy.yml", "playbooks/absent.yml")

    with pytest.raises(ConfigValidationError) as error:
        load_catalog(_repository(tmp_path, deploy=absent), SCHEMAS)

    assert error.value.issue.code == "operation_playbook_missing"
    assert "playbooks/absent.yml" in error.value.issue.message


def test_a_verify_playbook_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    absent = DEPLOY.replace("playbooks/health.yml", "playbooks/absent.yml")

    with pytest.raises(ConfigValidationError) as error:
        load_catalog(_repository(tmp_path, deploy=absent), SCHEMAS)

    assert error.value.issue.code == "operation_playbook_missing"


def test_a_playbook_outside_the_repository_is_refused(tmp_path: Path) -> None:
    escaping = DEPLOY.replace("playbooks/deploy.yml", "../elsewhere/deploy.yml")

    with pytest.raises(ConfigValidationError) as error:
        load_catalog(_repository(tmp_path, deploy=escaping), SCHEMAS)

    assert error.value.issue.code == "schema_validation_failed"


def test_two_documents_may_not_declare_the_same_operation(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError) as error:
        load_catalog(
            _repository(tmp_path, deploy=DEPLOY, redeploy=DEPLOY), SCHEMAS
        )

    assert error.value.issue.code == "operation_duplicate"


def test_an_undeclared_operation_does_not_exist_to_the_agent(
    tmp_path: Path,
) -> None:
    catalog = load_catalog(_repository(tmp_path), SCHEMAS)

    with pytest.raises(ConfigValidationError) as error:
        catalog.get(ResourceId.from_boundary("restart"))

    assert error.value.issue.code == "operation_undeclared"
    assert "deploy, facts" in error.value.issue.message


def test_a_repository_without_a_catalog_says_so(tmp_path: Path) -> None:
    repository = tmp_path / "bare"
    repository.mkdir()

    with pytest.raises(ConfigValidationError) as error:
        load_catalog(repository, SCHEMAS)

    assert error.value.issue.code == "operations_directory_missing"


def test_the_cli_lists_the_catalog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)

    exit_code = main(["operations", "list", "--repository", str(repository)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert [operation["id"] for operation in payload["data"]["operations"]] == [
        "deploy",
        "facts",
    ]
    assert [operation["risk"] for operation in payload["data"]["operations"]] == [
        "mutating",
        "read",
    ]


def test_the_cli_shows_one_operation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)

    exit_code = main(
        ["operations", "show", "deploy", "--repository", str(repository)]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    operation = payload["data"]["operation"]
    assert operation["verify"] == {"playbook": "playbooks/health.yml"}
    assert operation["preconditions"] == ["health:ok"]


def test_the_cli_refuses_an_undeclared_operation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)

    exit_code = main(
        ["operations", "show", "restart", "--repository", str(repository)]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 5
    assert payload["error"]["code"] == "NOT_FOUND"
    assert payload["error"]["context"]["code"] == "operation_undeclared"
