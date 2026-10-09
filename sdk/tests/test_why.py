"""The question the record exists to answer."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from cloudfall.cli import main
from cloudfall.decision import (
    DECISION_DIRECTORY,
    ApprovalChannel,
    ApprovalRequest,
    DecisionStatus,
    approve,
    propose,
)
from cloudfall.why import Instant, WhyError, WhyQuery, answer, render_why_html
from test_catalog import _repository
from test_decision import MOMENT, _proposal, _Runner, _store

if TYPE_CHECKING:
    from pathlib import Path

    from cloudfall.decision import Decision

LATER = datetime(2026, 9, 22, 8, 0, 0, tzinfo=UTC)


def _record(repository: Path) -> tuple[Decision, Decision]:
    """One verified deploy on web-1, then a facts read of the fleet that ran."""
    store = _store(repository)
    deploy = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    deploy = approve(
        ApprovalRequest(
            decision=deploy,
            approver="roman",
            repository=repository,
            via=ApprovalChannel.TERMINAL,
        ),
        store,
        lambda: MOMENT,
        _Runner(),
    )
    facts = propose(
        _proposal(repository, "facts", target=None, inputs={}),
        store,
        lambda: LATER,
        _Runner(changed=("db-1",)),
    )
    return deploy, facts


def test_an_instant_reads_the_record_and_a_bare_date() -> None:
    assert Instant.from_boundary("2026-09-21T14:30:12Z").as_string() == (
        "2026-09-21T14:30:12Z"
    )
    assert Instant.from_boundary("2026-09-21").as_string() == ("2026-09-21T00:00:00Z")
    assert Instant.from_boundary("2026-09-21T16:30:12+02:00").as_string() == (
        "2026-09-21T14:30:12Z"
    )


def test_a_moment_that_is_not_a_moment_is_refused() -> None:
    with pytest.raises(WhyError) as error:
        Instant.from_boundary("last tuesday")
    assert error.value.code == "why_instant_invalid"


def test_a_window_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(WhyError) as error:
        WhyQuery.from_boundary(since="2026-09-22", until="2026-09-21")
    assert error.value.code == "why_window_inverted"


def test_no_question_returns_the_whole_record_newest_first(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _record(repository)

    result = answer(_store(repository), WhyQuery())

    assert [entry.decision.operation_id.value for entry in result.explanations] == [
        "facts",
        "deploy",
    ]


def test_a_host_question_is_answered_from_what_the_record_names(
    tmp_path: Path,
) -> None:
    """A host is in the answer when it is the target or in the evidence."""
    repository = _repository(tmp_path)
    _record(repository)
    store = _store(repository)

    targeted = answer(store, WhyQuery.from_boundary(host="web-1"))
    evidenced = answer(store, WhyQuery.from_boundary(host="db-1"))
    untouched = answer(store, WhyQuery.from_boundary(host="web-2"))
    unknown = answer(store, WhyQuery.from_boundary(host="web-9"))

    assert [e.decision.operation_id.value for e in targeted.explanations] == ["deploy"]
    assert [e.decision.operation_id.value for e in evidenced.explanations] == ["facts"]
    assert len(untouched.explanations) == 2
    assert untouched.explanations[0].hosts == ("db-1", "web-2")
    assert unknown.explanations == ()


def test_an_operation_question_is_answered(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _record(repository)

    result = answer(_store(repository), WhyQuery.from_boundary(operation="deploy"))

    assert [e.decision.decision_id.value for e in result.explanations] == [
        "deploy-20260921143012"
    ]


def test_a_time_window_matches_any_moment_the_record_dates(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _record(repository)
    store = _store(repository)

    first_day = answer(
        store, WhyQuery.from_boundary(since="2026-09-21", until="2026-09-21T23:59:59Z")
    )
    second_day = answer(store, WhyQuery.from_boundary(since="2026-09-22"))
    before = answer(store, WhyQuery.from_boundary(until="2026-09-20"))

    assert [e.decision.operation_id.value for e in first_day.explanations] == ["deploy"]
    assert [e.decision.operation_id.value for e in second_day.explanations] == ["facts"]
    assert before.explanations == ()


def test_every_sentence_of_the_story_cites_the_record(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    deploy, facts = _record(repository)

    told = answer(_store(repository), WhyQuery.from_boundary(operation="deploy"))
    read = answer(_store(repository), WhyQuery.from_boundary(operation="facts"))

    story = told.explanations[0].story
    assert deploy.status is DecisionStatus.VERIFIED
    assert story[0].startswith("It was based on 1 host snapshot in tmp/observed")
    assert "observed at 2026-09-21T09:55:24Z" in story[0]
    assert story[1] == (
        "At 2026-09-21T14:30:12Z the mutating operation deploy "
        "(playbooks/deploy.yml) was proposed against host web-1 with "
        "version=1.4.0."
    )
    assert story[2].startswith("Check mode would have changed web-1 and left 1 host")
    assert deploy.check.diff.sha256[:12] in story[2]
    assert story[3].startswith("It waited for an approval:")
    assert story[4] == (
        "roman approved it at 2026-09-21T14:30:12Z (via terminal): a person "
        "typed its id at a terminal."
    )
    assert story[5].startswith(
        "The run at 2026-09-21T14:30:12Z exited 0 and changed web-1"
    )
    assert story[6].startswith(
        "The verify step at 2026-09-21T14:30:12Z exited 0 and changed no host"
    )
    assert story[7] == (
        "It ended verified: the run succeeded and the verify step changed nothing."
    )

    ran = read.explanations[0].story
    assert facts.status is DecisionStatus.RAN
    assert "was proposed against the whole fleet with no inputs." in ran[1]
    assert ran[3].startswith("Nothing gated it:")
    assert ran[-1] == (
        "It ran when it was proposed and exited 0: a read operation waits for "
        "no approval."
    )


def test_a_replayed_read_is_not_told_as_run_on_the_hosts(tmp_path: Path) -> None:
    """Its output was played back from a recording, never run (#58)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    propose(
        replace(
            _proposal(repository, "facts", target=None, inputs={}),
            replay="recordings/disk-full",
        ),
        store,
        lambda: MOMENT,
        _Runner(),
    )

    story = answer(store, WhyQuery.from_boundary(operation="facts"))
    told = story.explanations[0].story

    assert told[3] == (
        "That check output was played back from the recording "
        "recordings/disk-full, not run on the hosts, so it cannot be approved."
    )
    assert told[-1] == (
        "Its played-back output exited 0: a read operation waits for no approval."
    )
    assert not any("It ran when it was proposed" in line for line in told)


def test_a_superseded_proposal_says_what_replaced_it(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    first = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    second = propose(_proposal(repository), store, lambda: LATER, _Runner())

    told = answer(store, WhyQuery.from_boundary(status=["superseded"]))

    assert [e.decision.decision_id for e in told.explanations] == [first.decision_id]
    assert told.explanations[0].story[-1] == (
        f"It was superseded by {second.decision_id.value}, a newer proposal of "
        "the same operation, targets and inputs; nobody approved it."
    )


def test_a_status_question_is_answered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--status`` is repeatable and keeps only the statuses asked for (#26)."""
    repository = _repository(tmp_path)
    _record(repository)

    exit_code = main(
        [
            *("why", "--repository", str(repository)),
            *("--status", "ran", "--status", "proposed"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["data"]["query"] == {"status": ["proposed", "ran"]}
    assert [entry["id"] for entry in payload["data"]["answers"]] == [
        "facts-20260922080000"
    ]


def test_an_unknown_status_question_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)

    exit_code = main(["why", "--repository", str(repository), "--status", "stale"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"]["code"] == "ARG_ERROR"
    with pytest.raises(WhyError) as error:
        WhyQuery.from_boundary(status=["stale"])
    assert error.value.code == "why_status_unknown"


def test_the_cli_answers_in_json_without_a_catalog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    _record(repository)
    for path in (repository / "operations").iterdir():
        path.unlink()
    (repository / "operations").rmdir()

    exit_code = main(["why", "--repository", str(repository), "--host", "web-1"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["data"]["status"] == "ok"
    assert payload["data"]["query"] == {"host": "web-1"}
    assert payload["data"]["count"] == 1
    entry = payload["data"]["answers"][0]
    assert entry["id"] == "deploy-20260921143012"
    assert entry["hosts"] == ["web-1", "web-2"]
    assert entry["decision"]["spec"]["approval"]["approver"] == "roman"
    assert entry["decision"]["spec"]["approval"]["via"] == "terminal"
    assert entry["decision"]["spec"]["verify"]["changed"] == []
    assert entry["decision"]["spec"]["check"]["diff"]["sha256"]
    assert entry["decision"]["spec"]["basis"]["observedAt"] == {
        "web-1": "2026-09-21T09:55:24Z"
    }


def test_the_cli_answers_as_a_page(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    _record(repository)

    exit_code = main(["why", "--repository", str(repository), "--format", "html"])

    page = capsys.readouterr().out
    assert exit_code == 0
    assert page.startswith("<!doctype html>")
    assert "<title>Cloudfall Why</title>" in page
    assert "2 decisions" in page
    assert 'class="badge healthy">verified' in page
    assert 'class="badge healthy">ran' in page
    assert "roman approved it at 2026-09-21T14:30:12Z (via terminal)" in page


def test_an_approval_recorded_before_the_channel_says_via_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    deploy, _ = _record(repository)
    path = repository / DECISION_DIRECTORY / f"{deploy.decision_id.value}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["spec"]["approval"]["via"]
    path.write_text(json.dumps(document), encoding="utf-8")

    told = answer(_store(repository), WhyQuery.from_boundary(operation="deploy"))
    exit_code = main(["why", "--repository", str(repository), "--host", "web-1"])

    assert told.explanations[0].story[4] == (
        "roman approved it at 2026-09-21T14:30:12Z (via unknown): the record "
        "predates noting how an approval arrived."
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    entry = payload["data"]["answers"][0]
    assert entry["decision"]["spec"]["approval"]["via"] == "unknown"


def test_the_cli_reports_a_bad_window_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)

    exit_code = main(["why", "--repository", str(repository), "--since", "yesterday"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"]["code"] == "ARG_ERROR"
    assert payload["error"]["context"]["code"] == "why_instant_invalid"


def test_an_empty_record_renders_an_empty_page(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    page = render_why_html(answer(_store(repository), WhyQuery()))

    assert "The record holds no decision this question is about." in page
    assert "0 decisions" in page
