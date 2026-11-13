"""Records written by released versions still read (#218).

``config/tests/records`` holds decision records and proposal receipts in
the shape each released version wrote them, one file per shape the schema
history added. Every one must read with today's schema and code; a change
that breaks one breaks a team's committed record.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from cloudfall.decision import (
    API_VERSION,
    DECISION_SCHEMAS,
    ApprovalChannel,
    DecisionStatus,
    DecisionStore,
)
from cloudfall.operator import PROPOSAL_SCHEMAS, ProposalStatus, ProposalStore
from cloudfall.resources import default_schema_directory
from cloudfall.validation import SchemaCatalog

ROOT = Path(__file__).parents[2]
RECORDS = ROOT / "config" / "tests" / "records"
DECISIONS = sorted((RECORDS / "decisions").glob("*.json"))
PROPOSALS = sorted((RECORDS / "proposals").glob("*.json"))
SCHEMAS = default_schema_directory()


def _copied(fixtures: list[Path], directory: Path) -> Path:
    """Copy fixtures into a store directory, named by their id as a store names them."""
    directory.mkdir(parents=True)
    for fixture in fixtures:
        shutil.copy(fixture, _in_store(fixture, directory))
    return directory


def _in_store(fixture: Path, directory: Path) -> Path:
    document = json.loads(fixture.read_text(encoding="utf-8"))
    return directory / f"{document['metadata']['id']}.json"


def _status(path: Path) -> str:
    return str(json.loads(path.read_text(encoding="utf-8"))["spec"]["status"])


def test_there_is_a_fixture_of_each_record_kind() -> None:
    assert len(DECISIONS) >= 5
    assert len(PROPOSALS) >= 5


def test_every_decision_record_a_release_wrote_still_lists(tmp_path: Path) -> None:
    store = DecisionStore(
        directory=_copied(DECISIONS, tmp_path / "decisions"),
        catalog=SchemaCatalog(SCHEMAS),
    )

    listing = store.list()

    assert listing.unreadable == ()
    assert len(listing.records) == len(DECISIONS)


@pytest.mark.parametrize("fixture", DECISIONS, ids=[path.stem for path in DECISIONS])
def test_a_decision_record_reads_as_it_was_written(
    tmp_path: Path, fixture: Path
) -> None:
    directory = _copied([fixture], tmp_path / "decisions")
    store = DecisionStore(directory=directory, catalog=SchemaCatalog(SCHEMAS))
    path = _in_store(fixture, directory)

    decision = store.read(path)
    store.update(decision)
    rewritten = json.loads(path.read_text(encoding="utf-8"))

    written = json.loads(fixture.read_text(encoding="utf-8"))["spec"]
    assert decision.status is DecisionStatus(written["status"])
    if decision.approval is not None:
        assert decision.approval.via is ApprovalChannel.from_record(
            written["approval"].get("via")
        )
    assert rewritten["spec"]["writerVersion"]
    assert store.read(path) == decision


@pytest.mark.parametrize("fixture", PROPOSALS, ids=[path.stem for path in PROPOSALS])
def test_a_proposal_receipt_reads_as_it_was_written(
    tmp_path: Path, fixture: Path
) -> None:
    directory = _copied([fixture], tmp_path / "proposals")
    store = ProposalStore(directory=directory, catalog=SchemaCatalog(SCHEMAS))
    path = _in_store(fixture, directory)

    proposal = store.read(path)
    store.update(proposal)

    assert proposal.status is ProposalStatus(_status(fixture))
    assert json.loads(path.read_text(encoding="utf-8"))["spec"]["writerVersion"]
    assert store.read(path) == proposal


def test_every_proposal_receipt_a_release_wrote_still_lists(tmp_path: Path) -> None:
    store = ProposalStore(
        directory=_copied(PROPOSALS, tmp_path / "proposals"),
        catalog=SchemaCatalog(SCHEMAS),
    )

    listing = store.list()

    assert listing.unreadable == ()
    assert len(listing.records) == len(PROPOSALS)


def test_the_schema_is_picked_by_the_record_api_version(tmp_path: Path) -> None:
    """A record of a version this Cloudfall does not read is named, not fatal."""
    directory = _copied(DECISIONS[:2], tmp_path / "decisions")
    later = directory / "from-a-later-release.json"
    document = json.loads(DECISIONS[0].read_text(encoding="utf-8"))
    document["apiVersion"] = "cloudfall/v2"
    document["metadata"]["id"] = "from-a-later-release"
    later.write_text(json.dumps(document), encoding="utf-8")
    store = DecisionStore(directory=directory, catalog=SchemaCatalog(SCHEMAS))

    listing = store.list()

    assert set(DECISION_SCHEMAS) == {API_VERSION}
    assert set(PROPOSAL_SCHEMAS) == {API_VERSION}
    assert len(listing.records) == 2
    [unreadable] = listing.unreadable
    assert unreadable.path == later
    assert "'cloudfall/v2' is not one this Cloudfall reads (cloudfall/v1)" in (
        unreadable.reason
    )
