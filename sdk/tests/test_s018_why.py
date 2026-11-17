"""``cloudfall why`` answers the agent records beside the decisions (S-018-16).

A granted MCP call leaves an agent record and a withheld one an agent
proposal, both in the proposal store. ``why --host`` lists both for a server
the run changes, naming the tool, the class, the approver, the policy and
grant, the reason, ``planSha256`` and the status; ``--status`` filters them
as it filters decisions.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from cloudfall.agent_gate import plan_sha256
from cloudfall.cli import main
from cloudfall.operator import OperationKind, ProposalStatus
from cloudfall.why import WhyQuery, answer, explain_agent, render_why_html
from test_agent_server import _serve, _talk
from test_decision import _store as _decision_store
from test_deploy_approval import _write_artifact
from test_s018_agent_gate import _agent_record
from test_s018_granted_run import (
    PROPOSALS,
    REASON,
    RELEASE,
    _committed,
    _engine,
    _staging_project,
    _store,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from cloudfall.why import WhyAnswer

OVERRIDE = "\N{RIGHT-TO-LEFT OVERRIDE}"
RESTART_REASON = f"crm-backend leaks memory \x1b[2J{OVERRIDE}again"
"""A reason with a terminal escape and a direction override in it."""


def _recorded(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Deploy over MCP on a grant, then call a restart the policy withholds."""
    project = _staging_project(tmp_path, "deploy")
    _write_artifact(project)
    _committed(project)
    call = {"component": "crm-backend", "release": RELEASE, "reason": REASON}
    _, (planned, confirmed, refused) = _talk(
        _serve(
            "--project",
            str(project),
            "--engine",
            str(_engine(tmp_path, project, healthy=True)),
            cwd=tmp_path,
        ),
        ("deploy", call),
        ("deploy", {**call, "yes": True}),
        (
            "restart",
            {"component": "crm-backend", "yes": True, "reason": RESTART_REASON},
        ),
    )
    assert confirmed["ok"] is True, confirmed
    assert refused["ok"] is False, refused
    assert refused["error"]["context"]["code"] == "operator_approval_required"
    return project, planned, refused


def _answers(result: WhyAnswer) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", result.as_dict()["answers"])


def _why(
    project: Path, capsys: pytest.CaptureFixture[str], *flags: str
) -> tuple[int, dict[str, Any]]:
    exit_code = main(["why", "--repository", str(project), *flags])
    return exit_code, json.loads(capsys.readouterr().out)


def test_s018_16_why_lists_the_agent_record_and_proposal_of_a_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, planned, refused = _recorded(tmp_path)
    proposal_id = refused["error"]["context"]["proposal"]
    capsys.readouterr()

    # h2 is one of crm-backend's servers; the earned history record names h1.
    exit_code, payload = _why(project, capsys, "--host", "h2")

    assert exit_code == 0, payload
    answers = payload["data"]["answers"]
    assert payload["data"]["count"] == 2
    # Newest first: the restart came after the deploy.
    proposed, granted = answers
    assert proposed["id"] == proposal_id
    assert proposed["hosts"] == ["h1", "h2"]
    request = proposed["proposal"]["spec"]["trigger"]["request"]
    assert proposed["agent_record"] == {
        "tool": "restart",
        "class": "restart",
        "approver": None,
        "policy": None,
        "grant": None,
        "reason": request["reason"],
        "plan_sha256": proposed["proposal"]["spec"]["binding"]["planSha256"],
        "status": "proposed",
    }
    # JSON keeps the agent's text as JSON escapes it, the override included.
    assert OVERRIDE in request["reason"]
    assert granted["agent_record"] == {
        "tool": "deploy",
        "class": "deploy",
        "approver": {
            "mode": "agent",
            "client": {"name": "unknown", "version": "unknown"},
        },
        "policy": "staging-operator",
        "grant": "deploy",
        "reason": REASON,
        "plan_sha256": plan_sha256(planned["data"]),
        "status": "verified",
    }
    assert granted["proposal"]["spec"]["trigger"]["kind"] == "agent"
    # The story a person reads names the same facts, the reason escaped.
    told = " ".join(granted["story"])
    for fact in ("deploy tool", "class deploy", "staging-operator", REASON):
        assert fact in told
    assert plan_sha256(planned["data"]) in told
    assert "It ended verified" in told
    reason_line = proposed["story"][1]
    assert reason_line == (
        'It gave the reason "crm-backend leaks memory \\x1b[2J\\u202eagain".'
    )
    assert not {"\x1b", OVERRIDE} & set(" ".join(proposed["story"]))
    assert "cloudfall operator approve " + proposal_id in proposed["story"][-1]

    exit_code, payload = _why(project, capsys, "--host", "h2", "--status", "verified")

    assert exit_code == 0, payload
    assert [entry["id"] for entry in payload["data"]["answers"]] == [granted["id"]]


def test_s018_16_an_agent_record_names_a_person_approver(tmp_path: Path) -> None:
    """A person-approved agent proposal names ``human`` and how it arrived."""
    project = tmp_path / "project"
    _store(project).save(
        _agent_record(
            "op-20260501100000-0123456789abcdef",
            OperationKind.RESTART,
            ("h1",),
            ("staging",),
        )
    )

    result = answer(
        _decision_store(project),
        WhyQuery.from_boundary(host="h1"),
        proposals=_store(project),
    )

    (entry,) = _answers(result)
    assert entry["agent_record"]["approver"] == {"mode": "human", "via": "terminal"}
    assert entry["agent_record"]["policy"] is None
    assert "A person approved it (via terminal)." in entry["story"]


def test_why_leaves_out_agent_records_of_other_hosts_operations_and_staleness(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    _store(project).save(
        _agent_record(
            "op-20260501100000-0123456789abcdef",
            OperationKind.DEPLOY,
            ("h1",),
            ("staging",),
        )
    )
    proposals = _store(project)
    decisions = _decision_store(project)

    def ids(question: WhyQuery) -> list[object]:
        result = answer(decisions, question, proposals=proposals)
        return [entry["id"] for entry in _answers(result)]

    recorded = ["op-20260501100000-0123456789abcdef"]
    assert ids(WhyQuery.from_boundary(host="h1")) == recorded
    assert ids(WhyQuery.from_boundary(host="h2")) == []
    assert ids(WhyQuery.from_boundary(operation="deploy")) == []
    assert ids(WhyQuery(stale=True)) == []
    assert ids(WhyQuery.from_boundary(status=["failed"])) == []
    assert ids(WhyQuery.from_boundary(since="2026-05-02")) == []
    assert ids(WhyQuery.from_boundary(until="2026-05-01T10:01:30Z")) == recorded


def test_why_skips_an_unreadable_agent_record_with_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unreadable record is skipped and named, as an unreadable decision is."""
    project = tmp_path / "project"
    _store(project).save(
        _agent_record(
            "op-20260501100000-0123456789abcdef",
            OperationKind.DEPLOY,
            ("h1",),
            ("staging",),
        )
    )
    broken = project / PROPOSALS / "op-20260501110000-fedcba9876543210.json"
    broken.write_text("{not json", encoding="utf-8")

    exit_code, payload = _why(project, capsys, "--host", "h1")

    assert exit_code == 0, payload
    assert payload["data"]["count"] == 1
    (warning,) = [
        entry for entry in payload["warnings"] if entry["code"] == "RECORD_UNREADABLE"
    ]
    assert warning["context"]["path"] == str(broken)


def test_an_agent_record_renders_as_a_card(tmp_path: Path) -> None:
    project = tmp_path / "project"
    record = _agent_record(
        "op-20260501100000-0123456789abcdef",
        OperationKind.DEPLOY,
        ("h1",),
        ("staging",),
        status=ProposalStatus.FAILED,
    )
    _store(project).save(record)

    page = render_why_html(
        answer(_decision_store(project), WhyQuery(), proposals=_store(project))
    )

    assert "<h3>deploy (deploy)</h3>" in page
    assert 'class="badge critical">failed' in page
    assert explain_agent(record).story[-1] == "It ended failed: the health gate passed."
