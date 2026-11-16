"""One record of every kind, as the release that first wrote it wrote it (S-017-11).

``config/tests/records/kinds`` holds one record of each kind Cloudfall
writes as evidence, named by the release that wrote it. Each was written by
that release's own code (v0.7.0; ``ReleaseApproval`` first shipped in
v0.8.0rc11), never by hand to today's schema. Records are read by every
later release with the same ``apiVersion``: each must validate against
today's schema and read through today's readers and commands. A fixture
that fails here is a compatibility bug, never a fixture to edit.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

import pytest
from cloudfall.cli import main
from cloudfall.decision import DECISION_DIRECTORY
from cloudfall.investigation import INVESTIGATION_DIRECTORY
from cloudfall.observation import load_observations
from cloudfall.project import RecordStore
from cloudfall.release_approval import load_approvals
from cloudfall.secrets import load_environment_receipts
from cloudfall.service_evidence import (
    load_deployment_receipts,
    load_domain_observations,
)
from cloudfall.validation import SchemaCatalog
from examples_project import copy_examples
from jsonschema.exceptions import ValidationError
from test_catalog import _repository
from test_operator import ROOT, SCHEMAS

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

KINDS = ROOT / "config" / "tests" / "records" / "kinds"
FIXTURES = sorted(KINDS.glob("*.json"))


def _document(path: Path) -> dict[str, object]:
    document: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return document


SCHEMA_OF_KIND = {
    "DeploymentReceipt": "deployment-receipt.schema.json",
    "ReleaseReceipt": "release-receipt.schema.json",
    "BackupReceipt": "backup-receipt.schema.json",
    "EnvironmentReceipt": "environment-receipt.schema.json",
    "Artifact": "artifact.schema.json",
    "ObservedServer": "observed-server.schema.json",
    "ObservedDomain": "observed-domain.schema.json",
    "OperationDecision": "operation-decision.schema.json",
    "ReleaseApproval": "release-approval.schema.json",
    "OperatorProposal": "operator-proposal.schema.json",
    "AgentInvestigation": "agent-investigation.schema.json",
}
"""Every kind of the spec's Record and receipt schemas row, and its schema."""

FIRST_WRITTEN_BY = {"ReleaseApproval": "v0.8.0rc11"}
"""Kinds v0.7.0 did not write yet: their fixture is from the first release that did."""


def _fixture(kind: str) -> Path:
    [path] = [path for path in FIXTURES if _document(path)["kind"] == kind]
    return path


def _identifier(path: Path) -> str:
    metadata = _document(path)["metadata"]
    assert isinstance(metadata, dict)
    return str(metadata["id"])


def _placed(kind: str, directory: Path) -> Path:
    """Copy one fixture, unchanged, to where a store names it: ``<id>.json``."""
    directory.mkdir(parents=True, exist_ok=True)
    fixture = _fixture(kind)
    placed = directory / f"{_identifier(fixture)}.json"
    shutil.copy(fixture, placed)
    return placed


def test_s017_11_there_is_one_fixture_of_every_record_kind() -> None:
    kinds = [str(_document(path)["kind"]) for path in FIXTURES]

    assert sorted(kinds) == sorted(SCHEMA_OF_KIND)
    for path in FIXTURES:
        kind = str(_document(path)["kind"])
        assert path.name.startswith(f"{FIRST_WRITTEN_BY.get(kind, 'v0.7.0')}-")


@pytest.mark.parametrize("fixture", FIXTURES, ids=[path.stem for path in FIXTURES])
def test_s017_11_every_fixture_validates_against_todays_schema(fixture: Path) -> None:
    document = _document(fixture)
    schema = SCHEMA_OF_KIND[str(document["kind"])]

    SchemaCatalog(SCHEMAS).validate_named(schema, document)


def test_s017_11_a_snapshot_names_its_baseline_by_one_field_only() -> None:
    """v0.7.0 wrote ``serverType``, today writes ``baseline``; never both (D-11)."""
    catalog = SchemaCatalog(SCHEMAS)
    old = _document(_fixture("ObservedServer"))
    spec = old["spec"]
    assert isinstance(spec, dict)
    rest = {key: value for key, value in spec.items() if key != "serverType"}
    current = {**old, "spec": {**rest, "baseline": spec["serverType"]}}
    both = {**old, "spec": {**spec, "baseline": spec["serverType"]}}
    neither = {**old, "spec": rest}

    catalog.validate_named("observed-server.schema.json", old)
    catalog.validate_named("observed-server.schema.json", current)
    for refused in (both, neither):
        with pytest.raises(ValidationError):
            catalog.validate_named("observed-server.schema.json", refused)


def _deployment_receipts(directory: Path) -> int:
    return len(load_deployment_receipts(directory, SCHEMAS).receipts)


def _domain_observations(directory: Path) -> int:
    return len(load_domain_observations(directory, SCHEMAS).snapshots)


def _environment_receipts(directory: Path) -> int:
    return len(load_environment_receipts(directory, SCHEMAS))


def _server_observations(directory: Path) -> int:
    return len(load_observations(directory, SCHEMAS).snapshots)


def _release_approvals(directory: Path) -> int:
    return len(load_approvals(directory, SchemaCatalog(SCHEMAS)))


READERS: dict[str, Callable[[Path], int]] = {
    "DeploymentReceipt": _deployment_receipts,
    "ObservedDomain": _domain_observations,
    "EnvironmentReceipt": _environment_receipts,
    "ObservedServer": _server_observations,
    "ReleaseApproval": _release_approvals,
}
"""The kinds a reader of their own loads from a directory, beside the CLI ones."""


@pytest.mark.parametrize("kind", sorted(READERS))
def test_s017_11_todays_reader_loads_the_fixture(tmp_path: Path, kind: str) -> None:
    _placed(kind, tmp_path / "records")

    assert READERS[kind](tmp_path / "records") == 1


def _cli(capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, object]:
    exit_code = main(list(argv))
    payload: dict[str, object] = json.loads(capsys.readouterr().out)
    assert exit_code == 0, payload
    assert payload["ok"] is True
    assert "RECORD_INVALID" not in json.dumps(payload)
    assert "RECORD_UNREADABLE" not in json.dumps(payload)
    return payload


def test_s017_11_decisions_show_reads_the_fixture_decision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    _placed("OperationDecision", repository / DECISION_DIRECTORY)
    decision = _identifier(_fixture("OperationDecision"))

    payload = _cli(
        capsys, "decisions", "show", decision, "--repository", str(repository)
    )

    data = payload["data"]
    assert isinstance(data, dict)
    assert data["decision"]["metadata"]["id"] == decision
    assert data["decision"]["spec"]["status"] == "verified"


def test_s017_11_operator_show_reads_the_fixture_proposal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The proposal lies where ``config upgrade`` moves it: ``record/`` (D-16)."""
    project = tmp_path / "project"
    copy_examples(project)
    _placed("OperatorProposal", project / RecordStore.PROPOSALS.directory)
    proposal = _identifier(_fixture("OperatorProposal"))

    payload = _cli(
        capsys,
        *("operator", "show", proposal),
        *("--project", str(project), "--schemas", str(SCHEMAS)),
    )

    data = payload["data"]
    assert isinstance(data, dict)
    assert data["proposal"]["metadata"]["id"] == proposal
    assert data["proposal"]["spec"]["status"] == "verified"


def test_s017_11_why_reads_the_fixture_decision_and_investigation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    _placed("OperationDecision", repository / DECISION_DIRECTORY)
    _placed("AgentInvestigation", repository / INVESTIGATION_DIRECTORY)
    decision = _identifier(_fixture("OperationDecision"))
    investigation = _identifier(_fixture("AgentInvestigation"))

    payload = _cli(capsys, "why", "--repository", str(repository))

    data = payload["data"]
    assert isinstance(data, dict)
    [answer] = data["answers"]
    assert answer["id"] == decision
    assert answer["agent"]["investigation"] == investigation
    assert answer["agent"]["turn"] == 1
