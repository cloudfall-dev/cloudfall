"""Stale proposals: open ones whose fleet evidence was observed again (S-001)."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from cloudfall.app import app
from cloudfall.cli import main
from cloudfall.decision import (
    DECISION_DIRECTORY,
    ApprovalChannel,
    ApprovalRequest,
    DecisionStatus,
    ObservationClock,
    approve,
    propose,
)
from cloudfall.domain import ResourceId
from test_catalog import _repository
from test_decision import (
    LATER,
    MOMENT,
    _proposal,
    _proposed_locally,
    _Runner,
    _store,
    _Terminal,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from cloudfall.decision import Decision

T1 = "2026-09-21T09:55:24Z"
"""When ``_proposal``'s observation of web-1 says it was made."""

T2 = "2026-09-21T16:02:10Z"


def _observe(repository: Path, host: str, observed_at: str) -> Path:
    """Write the observation file a later ``cloudfall observe`` would leave."""
    path = repository / "tmp" / "observed" / f"{host}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"spec": {"observedAt": observed_at}}), encoding="utf-8"
    )
    return path


def _open_proposal(repository: Path) -> Decision:
    """Propose deploy on web-1 against web-1 observed at T1."""
    return propose(_proposal(repository), _store(repository), lambda: MOMENT, _Runner())


def _run(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    exit_code = main(argv)
    return exit_code, json.loads(capsys.readouterr().out)


def _listed(
    repository: Path, capsys: pytest.CaptureFixture[str], *flags: str
) -> list[dict[str, Any]]:
    exit_code, payload = _run(
        ["decisions", "list", "--repository", str(repository), *flags], capsys
    )
    assert exit_code == 0, payload
    return list(payload["data"]["decisions"])


T2_MOMENT = MOMENT.replace(hour=16)


def _settled(repository: Path) -> tuple[Decision, Decision, Decision, Decision]:
    """Record a verified deploy, a read that ran, a superseded and an open proposal.

    Every one's basis names web-1 observed at T1.
    """
    store = _store(repository)
    verified = approve(
        ApprovalRequest(
            decision=_open_proposal(repository),
            approver="roman",
            repository=repository,
            via=ApprovalChannel.TERMINAL,
        ),
        store,
        lambda: MOMENT,
        _Runner(),
    )
    ran = propose(
        _proposal(repository, "facts", target=None, inputs={}),
        store,
        lambda: LATER,
        _Runner(),
    )
    older = propose(
        _proposal(repository, target="web-2"), store, lambda: LATER, _Runner()
    )
    still_open = propose(
        _proposal(repository, target="web-2"), store, lambda: T2_MOMENT, _Runner()
    )
    superseded = store.load(older.decision_id)
    assert verified.status is DecisionStatus.VERIFIED
    assert ran.status is DecisionStatus.RAN
    assert superseded.status is DecisionStatus.SUPERSEDED
    assert still_open.status is DecisionStatus.PROPOSED
    return verified, ran, superseded, still_open


def test_s001_1_a_newer_observation_makes_the_proposal_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    _open_proposal(repository)
    _observe(repository, "web-1", T2)

    (entry,) = _listed(repository, capsys)

    assert entry["spec"]["basis"]["observedAt"] == {"web-1": T1}
    assert entry["stale"] is True
    assert entry["staleHosts"] == ["web-1"]


def test_s001_2_the_same_missing_or_no_observation_is_not_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    _open_proposal(repository)

    (same,) = _listed(repository, capsys)
    (repository / "tmp" / "observed" / "web-1.json").unlink()
    (missing,) = _listed(repository, capsys)

    for entry in (same, missing):
        assert entry["stale"] is False
        assert "staleHosts" not in entry


def test_s001_2_a_basis_that_names_no_host_is_never_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    empty = repository / "tmp" / "empty"
    empty.mkdir(parents=True)
    request = replace(_proposal(repository), observations=empty)
    propose(request, _store(repository), lambda: MOMENT, _Runner())
    _observe(repository, "web-1", T2)

    (entry,) = _listed(repository, capsys)

    assert entry["spec"]["basis"] == {"observations": "tmp/empty", "observedAt": {}}
    assert entry["stale"] is False
    assert "staleHosts" not in entry


def test_s001_2_times_compare_as_moments_not_as_text(tmp_path: Path) -> None:
    """The same moment in another offset, or an earlier one that sorts later."""
    repository = _repository(tmp_path)
    decision = _open_proposal(repository)

    _observe(repository, "web-1", "2026-09-21T11:55:24+02:00")
    same_moment = ObservationClock(repository=repository).staleness(decision)
    _observe(repository, "web-1", "2026-09-21T10:55:24+02:00")
    earlier = ObservationClock(repository=repository).staleness(decision)
    _observe(repository, "web-1", "2026-09-21T09:55:25+00:00")
    one_second_later = ObservationClock(repository=repository).staleness(decision)

    assert same_moment is not None
    assert not same_moment.stale
    assert earlier is not None
    assert not earlier.stale
    assert one_second_later is not None
    assert one_second_later.newer == {"web-1": "2026-09-21T09:55:25+00:00"}


def test_s001_3_a_decision_that_is_not_proposed_carries_no_staleness(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    *settled, _ = _settled(repository)
    _observe(repository, "web-1", T2)
    root = ("--repository", str(repository))
    settled_ids = {decision.decision_id.value for decision in settled}

    listed = _listed(repository, capsys)
    shown = [
        _run(["decisions", "show", decision_id, *root], capsys)[1]["data"]["decision"]
        for decision_id in sorted(settled_ids)
    ]
    exit_code, why = _run(["why", *root], capsys)

    assert exit_code == 0
    told = [entry["decision"] for entry in why["data"]["answers"]]
    closed = [
        entry
        for entry in (*listed, *shown, *told)
        if entry["metadata"]["id"] in settled_ids
    ]
    assert len(closed) == 9
    for entry in closed:
        assert entry["spec"]["status"] != "proposed"
        assert "stale" not in entry
        assert "staleHosts" not in entry
    # The one open proposal beside them, on the same evidence, is stale.
    assert [entry["stale"] for entry in listed if "stale" in entry] == [True]


def test_s001_4_the_stale_filter_keeps_only_the_stale_and_combines_with_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    *_, stale = _settled(repository)
    empty = repository / "tmp" / "empty"
    empty.mkdir(parents=True)
    fresh = propose(
        replace(_proposal(repository, target="web-3"), observations=empty),
        _store(repository),
        lambda: MOMENT.replace(hour=17),
        _Runner(),
    )
    _observe(repository, "web-1", T2)

    every = _listed(repository, capsys)
    proposed = _listed(repository, capsys, "--status", "proposed")
    only_stale = _listed(repository, capsys, "--stale")
    stale_proposed = _listed(repository, capsys, "--stale", "--status", "proposed")
    stale_verified = _listed(repository, capsys, "--stale", "--status", "verified")

    assert len(every) == 5
    assert {entry["metadata"]["id"] for entry in proposed} == {
        stale.decision_id.value,
        fresh.decision_id.value,
    }
    ids = [entry["metadata"]["id"] for entry in only_stale]
    assert ids == [stale.decision_id.value]
    assert [entry["metadata"]["id"] for entry in stale_proposed] == ids
    assert stale_proposed == only_stale
    assert stale_verified == []


def test_s001_5_why_stale_keeps_only_the_stale_and_reports_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    *_, stale = _settled(repository)
    _observe(repository, "web-1", T2)

    exit_code, payload = _run(
        ["why", "--repository", str(repository), "--stale"], capsys
    )
    (listed,) = _listed(repository, capsys, "--stale")

    assert exit_code == 0, payload
    assert payload["data"]["query"] == {"stale": True}
    assert [entry["id"] for entry in payload["data"]["answers"]] == [
        stale.decision_id.value
    ]
    told = payload["data"]["answers"][0]["decision"]
    assert told["stale"] is True
    assert told["staleHosts"] == ["web-1"]
    assert told == listed


def test_s001_6_show_reports_a_stale_proposal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    decision = _open_proposal(repository)
    _observe(repository, "web-1", T2)

    exit_code, payload = _run(
        [
            *("decisions", "show", decision.decision_id.value),
            *("--repository", str(repository)),
        ],
        capsys,
    )

    assert exit_code == 0, payload
    shown = payload["data"]["decision"]
    assert shown["stale"] is True
    assert shown["staleHosts"] == ["web-1"]


def test_s001_7_working_out_staleness_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    decision = _open_proposal(repository)
    _observe(repository, "web-1", T2)
    record = repository / DECISION_DIRECTORY / f"{decision.decision_id.value}.json"
    before = record.read_bytes()
    stat = record.stat()
    root = ("--repository", str(repository))

    listed = _listed(repository, capsys)
    shown = _run(["decisions", "show", decision.decision_id.value, *root], capsys)
    told = _run(["why", *root], capsys)

    assert listed[0]["stale"] is True
    assert shown[1]["data"]["decision"]["stale"] is True
    assert told[1]["data"]["answers"][0]["decision"]["stale"] is True
    assert record.read_bytes() == before
    assert record.stat().st_mtime_ns == stat.st_mtime_ns
    assert "stale" not in json.loads(before)["spec"]


def test_s001_8_approve_names_the_stale_hosts_and_still_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    decision_id = _proposed_locally(repository, capsys)
    _observe(repository, "web-1", T2)
    stdout = _Terminal()
    stderr = io.StringIO()

    exit_code = app.run(
        [
            *("decisions", "approve", decision_id),
            *("--repository", str(repository), "--approver", "roman"),
            *("--format", "json"),
        ],
        stdin=_Terminal(f"{decision_id}\n"),
        stdout=stdout,
        stderr=stderr,
    )

    prompt = stderr.getvalue() + stdout.getvalue()
    assert exit_code == 0, prompt
    assert "stale" in prompt
    assert f"web-1 was observed again at {T2}" in prompt
    loaded = _store(repository).load(ResourceId.from_boundary(decision_id))
    assert loaded.status is DecisionStatus.VERIFIED
    assert loaded.approval is not None


def test_s001_8_a_fresh_proposal_prompt_says_nothing_of_staleness(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    decision_id = _proposed_locally(repository, capsys)
    stdout = _Terminal()
    stderr = io.StringIO()

    exit_code = app.run(
        [
            *("decisions", "approve", decision_id),
            *("--repository", str(repository), "--approver", "roman"),
            *("--format", "json"),
        ],
        stdin=_Terminal(f"{decision_id}\n"),
        stdout=stdout,
        stderr=stderr,
    )

    prompt = stderr.getvalue() + stdout.getvalue()
    assert exit_code == 0, prompt
    assert "The diff is at" in prompt
    assert "stale" not in prompt


def test_s001_9_an_observation_file_that_is_not_json_is_record_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    _open_proposal(repository)
    broken = repository / "tmp" / "observed" / "web-1.json"
    broken.write_text("{not json", encoding="utf-8")

    exit_code, payload = _run(
        ["decisions", "list", "--repository", str(repository)], capsys
    )

    assert exit_code != 0
    assert payload["error"]["code"] == "RECORD_INVALID"
    assert str(broken) in payload["error"]["message"]
    assert payload["error"]["context"]["code"] == "decision_observation_invalid"
