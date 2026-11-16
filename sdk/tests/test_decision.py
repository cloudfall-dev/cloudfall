"""The gate and the decision record."""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cloudfall.ansible_data import unmarked
from cloudfall.app import app
from cloudfall.catalog import RiskLevel, TargetScope, load_catalog
from cloudfall.cli import main
from cloudfall.decision import (
    APPROVED_RUN_TIMEOUT_SECONDS,
    CHECK_TIMEOUT_SECONDS,
    DECISION_DIRECTORY,
    HOST_STATS_VARIABLE,
    ApprovalChannel,
    ApprovalRequest,
    DecisionError,
    DecisionStatus,
    DecisionStore,
    PlaybookInvocation,
    ProposalRequest,
    Requirement,
    Targets,
    approve,
    approved_runner,
    gate,
    mark_interrupted,
    propose,
    raw_output_directory,
    read_basis,
    refuse_unapprovable,
    run_command,
)
from cloudfall.domain import ResourceId
from cloudfall.resources import default_schema_directory
from cloudfall.validation import SchemaCatalog
from test_catalog import DEPLOY, FACTS, _repository
from treaty import CommandPath

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from cloudfall.decision import CheckRunner, Decision
    from cloudfall.record_files import UnreadableRecord

SCHEMAS = default_schema_directory()
MOMENT = datetime(2026, 9, 21, 14, 30, 12, tzinfo=UTC)
TERMINAL = ApprovalChannel.TERMINAL

WIPE = """\
id: wipe
description: Remove what nothing points at any more
playbook: playbooks/deploy.yml
risk: destructive
targets: fleet
verify:
  playbook: playbooks/health.yml
"""

ECHO = """\
id: echo
playbook: playbooks/echo.yml
risk: read
targets: fleet
inputs:
  message: string
  count: integer
  force: boolean
"""

# Writes the inputs as the play sees them, even in check mode, and their
# types with them: a number or a boolean marked as text would arrive quoted.
ECHO_PLAYBOOK = """\
---
- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - name: Keep the inputs
      check_mode: false
      ansible.builtin.copy:
        content: "{{{{ dict(message=message, count=count, force=force) | to_json }}}}"
        dest: {seen}
        mode: "0644"
"""

LATER = datetime(2026, 9, 21, 14, 45, 0, tzinfo=UTC)
LATEST = datetime(2026, 9, 21, 15, 0, 0, tzinfo=UTC)


def _store(repository: Path) -> DecisionStore:
    return DecisionStore(
        directory=repository / DECISION_DIRECTORY,
        catalog=SchemaCatalog(SCHEMAS),
    )


def _observations(repository: Path, *hosts: str) -> Path:
    directory = repository / "tmp" / "observed"
    directory.mkdir(parents=True, exist_ok=True)
    for host in hosts:
        (directory / f"{host}.json").write_text(
            json.dumps({"spec": {"observedAt": "2026-09-21T09:55:24Z"}}),
            encoding="utf-8",
        )
    return directory


def _proposal(
    repository: Path,
    operation_id: str = "deploy",
    *,
    target: str | None = "web-1",
    inputs: dict[str, object] | None = None,
) -> ProposalRequest:
    catalog = load_catalog(repository, SCHEMAS)
    operation = catalog.get(ResourceId.from_boundary(operation_id))
    return ProposalRequest(
        operation=operation,
        targets=Targets(scope=operation.targets, pattern=target),
        inputs=inputs if inputs is not None else {"version": "1.4.0"},
        repository=repository,
        observations=_observations(repository, "web-1"),
    )


def _legacy_read(
    repository: Path, store: DecisionStore, moment: datetime
) -> Decision:
    """Record a facts read as a version before #26 did: ``proposed``, no exit code."""
    decision = propose(
        _proposal(repository, "facts", target=None, inputs={}),
        store,
        lambda: moment,
        _Runner(),
    )
    path = repository / DECISION_DIRECTORY / f"{decision.decision_id.value}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["spec"]["status"] = "proposed"
    del document["spec"]["ranExitCode"]
    del document["spec"]["verdict"]
    path.write_text(json.dumps(document), encoding="utf-8")
    return store.load(decision.decision_id)


class _Runner:
    """A runner that writes what Ansible would write, stage by stage.

    An idempotent fleet changes on the run and changes nothing on the
    verify that follows, so the two stages are modelled separately. Every
    stage observes the hosts the play touched, unless ``verify_observed``
    names the only hosts the verify step observed.
    """

    def __init__(
        self,
        exit_code: int = 0,
        changed: tuple[str, ...] = ("web-1",),
        verify_changed: tuple[str, ...] = (),
        verify_observed: tuple[str, ...] | None = None,
    ) -> None:
        self.exit_code = exit_code
        self.changed = changed
        self.verify_changed = verify_changed
        self.verify_observed = verify_observed
        self.recorded: list[tuple[str, ...]] = []

    def __call__(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
        diff_path: Path,
    ) -> int:
        host_stats = Path(environment[HOST_STATS_VARIABLE])
        verifying = self._verifying(host_stats)
        changed = self.verify_changed if verifying else self.changed
        observed = (*self.changed, "web-2")
        if verifying and self.verify_observed is not None:
            observed = self.verify_observed
        self.recorded.append(tuple(argv))
        host_stats.mkdir(parents=True, exist_ok=True)
        for host in dict.fromkeys((*observed, *changed)):
            _write_recap(
                host_stats / host, ok=3, changed=1 if host in changed else 0
            )
        diff_path.write_text("--- before\n+++ after\n", encoding="utf-8")
        return self.exit_code

    @staticmethod
    def _verifying(host_stats: Path) -> bool:
        return host_stats.name.endswith("-verify")


def _write_recap(path: Path, *, ok: int, changed: int) -> None:
    """Write one host's recap counts as the host stats callback does."""
    path.write_text(
        json.dumps({"changed": changed, "failed": 0, "ok": ok, "unreachable": 0}),
        encoding="utf-8",
    )


def test_the_gate_follows_the_risk_level() -> None:
    """The table is the product's: a human cannot be skipped by policy."""
    assert gate(RiskLevel.READ)[0] is Requirement.RUNS_FREELY
    assert gate(RiskLevel.MUTATING)[0] is Requirement.CHECK_THEN_APPROVE
    assert gate(RiskLevel.DESTRUCTIVE)[0] is Requirement.HUMAN_REQUIRED
    assert "cannot be undone" in gate(RiskLevel.DESTRUCTIVE)[1]


def test_a_proposal_records_what_check_mode_would_change(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    run = _Runner()

    decision = propose(
        _proposal(repository), _store(repository), lambda: MOMENT, run
    )

    assert decision.decision_id.value == "deploy-20260921143012"
    assert decision.status is DecisionStatus.PROPOSED
    assert decision.requirement is Requirement.CHECK_THEN_APPROVE
    assert decision.check.changed == ("web-1",)
    assert decision.check.unchanged == ("web-2",)
    assert decision.check.diff.size == len(b"--- before\n+++ after\n")
    # A committed record names paths as the repository sees them.
    assert not decision.check.diff.path.is_absolute()
    artifact = repository / decision.check.diff.path
    assert artifact.read_text(encoding="utf-8").startswith("---")
    assert not decision.basis.observations.is_absolute()


def test_a_proposal_cites_the_evidence_it_was_made_against(
    tmp_path: Path,
) -> None:
    """An audit entry has to say what the agent was looking at."""
    repository = _repository(tmp_path)

    decision = propose(
        _proposal(repository), _store(repository), lambda: MOMENT, _Runner()
    )

    assert decision.basis.observed_at == {"web-1": "2026-09-21T09:55:24Z"}


def test_a_proposal_against_no_evidence_says_so(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    basis = read_basis(repository / "tmp" / "absent")

    assert basis.observed_at == {}


def test_the_check_run_is_check_mode_with_a_diff(tmp_path: Path) -> None:
    """Nothing may change while a proposal is being recorded."""
    repository = _repository(tmp_path)
    run = _Runner()

    propose(_proposal(repository), _store(repository), lambda: MOMENT, run)

    argv = run.recorded[0]
    assert "--check" in argv
    assert "--diff" in argv
    assert argv[argv.index("--limit") + 1] == "web-1"
    extra_vars = argv[argv.index("--extra-vars") + 1]
    assert json.loads(extra_vars, object_hook=unmarked) == {"version": "1.4.0"}
    assert argv[-1].endswith("playbooks/deploy.yml")


def test_inputs_reach_the_playbook_as_data_never_as_templates(
    tmp_path: Path,
) -> None:
    """A string input holding Jinja is text to the team's playbook (#181).

    Ansible templates extra vars, so an unmarked ``{{ lookup('pipe', ...) }}``
    would run on the management host when the playbook starts.
    """
    marker = tmp_path / "pwned"
    payload = "{{ lookup('pipe', 'touch " + str(marker) + "') }}"
    seen = tmp_path / "inputs.json"
    repository = _repository(tmp_path, echo=ECHO)
    (repository / "playbooks" / "echo.yml").write_text(
        ECHO_PLAYBOOK.format(seen=seen), encoding="utf-8"
    )

    decision = propose(
        _proposal(
            repository,
            "echo",
            target=None,
            inputs={"message": payload, "count": 3, "force": True},
        ),
        _store(repository),
        lambda: MOMENT,
    )

    assert decision.check.exit_code == 0
    assert not marker.exists()
    assert json.loads(seen.read_text(encoding="utf-8")) == {
        "message": payload,
        "count": 3,
        "force": True,
    }
    assert decision.inputs == {"message": payload, "count": 3, "force": True}


def test_the_team_configuration_runs_their_playbook(tmp_path: Path) -> None:
    """Their playbook needs their roles and their connection settings."""
    repository = _repository(tmp_path)
    (repository / "ansible.cfg").write_text(
        "[defaults]\nroles_path = roles\n", encoding="utf-8"
    )
    catalog = load_catalog(repository, SCHEMAS)
    operation = catalog.get(ResourceId.from_boundary("deploy"))

    _, environment = run_command(
        PlaybookInvocation(
            playbook=operation.playbook,
            repository=repository,
            host_stats=repository / "host-stats",
            pattern="web-1",
        ),
        check=True,
    )

    assert environment["ANSIBLE_CONFIG"] == str(repository / "ansible.cfg")
    assert "ANSIBLE_ROLES_PATH" not in environment


def test_a_recorded_decision_round_trips_through_the_store(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)

    decision = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    loaded = store.load(decision.decision_id)

    assert loaded == decision
    assert [entry.decision_id for entry in store.list().records] == [
        decision.decision_id
    ]


def _target_diff(
    argv: Sequence[str], environment: Mapping[str, str], diff_path: Path
) -> int:
    """Write a diff naming the run's target, so each proposal's diff is its own."""
    Path(environment[HOST_STATS_VARIABLE]).mkdir(parents=True, exist_ok=True)
    target = argv[list(argv).index("--limit") + 1]
    diff_path.write_text(f"+++ {target}\n", encoding="utf-8")
    return 0


def test_two_proposals_in_one_second_each_keep_their_own_diff(
    tmp_path: Path,
) -> None:
    """Two proposals of one operation in one second are two records (#356)."""
    repository = _repository(tmp_path)
    store = _store(repository)

    first = propose(
        _proposal(repository, target="web-1"), store, lambda: MOMENT, _target_diff
    )
    second = propose(
        _proposal(repository, target="web-2"), store, lambda: MOMENT, _target_diff
    )

    assert first.decision_id.value == "deploy-20260921143012"
    assert second.decision_id.value == "deploy-20260921143012-2"
    assert {entry.decision_id for entry in store.list().records} == {
        first.decision_id,
        second.decision_id,
    }
    for decision, target in ((first, "web-1"), (second, "web-2")):
        diff = repository / store.load(decision.decision_id).check.diff.path
        assert diff.read_text(encoding="utf-8") == f"+++ {target}\n"
    # No reservation outlives its proposal.
    assert not list(store.directory.glob(".*.reserved"))


def test_a_failed_proposal_leaves_no_record_and_frees_its_id(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)

    def broken(
        _argv: Sequence[str], _environment: Mapping[str, str], diff_path: Path
    ) -> int:
        diff_path.write_text("--- half\n", encoding="utf-8")
        message = "the check run broke"
        raise OSError(message)

    with pytest.raises(OSError, match="the check run broke"):
        propose(_proposal(repository), store, lambda: MOMENT, broken)

    assert store.list().records == ()
    assert not list(store.directory.glob(".*.reserved"))
    retried = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    assert retried.decision_id.value == "deploy-20260921143012"


def test_a_fleet_operation_takes_no_target(tmp_path: Path) -> None:
    repository = _repository(tmp_path, deploy=DEPLOY, wipe=WIPE)

    with pytest.raises(DecisionError) as error:
        propose(
            _proposal(repository, "wipe", target="web-1", inputs={}),
            _store(repository),
            lambda: MOMENT,
            _Runner(),
        )

    assert error.value.code == "decision_target_unexpected"


def test_a_host_operation_requires_one(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(DecisionError) as error:
        propose(
            _proposal(repository, target=None),
            _store(repository),
            lambda: MOMENT,
            _Runner(),
        )

    assert error.value.code == "decision_target_required"


def test_a_destructive_operation_records_that_a_human_is_required(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path, deploy=DEPLOY, wipe=WIPE)

    decision = propose(
        _proposal(repository, "wipe", target=None, inputs={}),
        _store(repository),
        lambda: MOMENT,
        _Runner(),
    )

    assert decision.risk is RiskLevel.DESTRUCTIVE
    assert decision.requirement is Requirement.HUMAN_REQUIRED
    assert decision.targets.scope is TargetScope.FLEET


def test_an_undeclared_input_is_refused(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(DecisionError) as error:
        propose(
            _proposal(repository, inputs={"release": "1.4.0"}),
            _store(repository),
            lambda: MOMENT,
            _Runner(),
        )

    assert error.value.code == "decision_input_unknown"
    assert "force, version" in error.value.detail


def test_a_missing_required_input_is_refused(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(DecisionError) as error:
        propose(
            _proposal(repository, inputs={}),
            _store(repository),
            lambda: MOMENT,
            _Runner(),
        )

    assert error.value.code == "decision_input_missing"


def test_an_input_is_coerced_to_its_declared_type(tmp_path: Path) -> None:
    """Values arrive from a command line or an agent as text."""
    repository = _repository(tmp_path)

    decision = propose(
        _proposal(repository, inputs={"version": "1.4.0", "force": "yes"}),
        _store(repository),
        lambda: MOMENT,
        _Runner(),
    )

    assert decision.inputs == {"force": True, "version": "1.4.0"}


def test_an_input_that_is_not_its_declared_type_is_refused(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(DecisionError) as error:
        propose(
            _proposal(repository, inputs={"version": "1.4.0", "force": "maybe"}),
            _store(repository),
            lambda: MOMENT,
            _Runner(),
        )

    assert error.value.code == "decision_input_type"


def test_a_read_operation_is_gated_by_nothing(tmp_path: Path) -> None:
    repository = _repository(tmp_path, deploy=DEPLOY, facts=FACTS)

    decision = propose(
        _proposal(repository, "facts", target=None, inputs={}),
        _store(repository),
        lambda: MOMENT,
        _Runner(),
    )

    assert decision.requirement is Requirement.RUNS_FREELY
    assert decision.verify_playbook is None


def test_a_read_operation_is_recorded_as_ran_with_its_exit_code(
    tmp_path: Path,
) -> None:
    """A read has run by the time it is recorded; it never waits (#26)."""
    repository = _repository(tmp_path, deploy=DEPLOY, facts=FACTS)
    store = _store(repository)

    decision = propose(
        _proposal(repository, "facts", target=None, inputs={}),
        store,
        lambda: MOMENT,
        _Runner(exit_code=2),
    )

    assert decision.status is DecisionStatus.RAN
    assert decision.ran_exit_code == 2
    assert decision.check.exit_code == 2
    assert store.load(decision.decision_id) == decision
    on_disk = json.loads(
        (repository / DECISION_DIRECTORY / f"{decision.decision_id.value}.json")
        .read_text(encoding="utf-8")
    )
    assert on_disk["spec"]["status"] == "ran"
    assert on_disk["spec"]["ranExitCode"] == 2


def test_a_mutating_proposal_still_waits_and_has_no_run_exit_code(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    decision = propose(
        _proposal(repository), _store(repository), lambda: MOMENT, _Runner()
    )

    assert decision.status is DecisionStatus.PROPOSED
    assert decision.ran_exit_code is None
    assert "ranExitCode" not in json.dumps(decision.as_document())


def test_a_new_proposal_supersedes_the_open_one_of_the_same_call(
    tmp_path: Path,
) -> None:
    """Proposing the same thing twice leaves one open proposal, not two (#26)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    first = propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    second = propose(_proposal(repository), store, lambda: LATER, _Runner())

    older = store.load(first.decision_id)
    assert older.status is DecisionStatus.SUPERSEDED
    assert older.superseded_by == second.decision_id
    assert older.approval is None
    assert older.verdict is not None
    assert second.decision_id.value in older.verdict
    assert store.load(second.decision_id).status is DecisionStatus.PROPOSED
    on_disk = json.loads(
        (repository / DECISION_DIRECTORY / f"{first.decision_id.value}.json")
        .read_text(encoding="utf-8")
    )
    assert on_disk["spec"]["supersededBy"] == second.decision_id.value


@pytest.mark.parametrize(
    ("target", "inputs"),
    [
        ("web-2", {"version": "1.4.0"}),
        ("web-1", {"version": "1.5.0"}),
        ("web-1", {"version": "1.4.0", "force": "yes"}),
    ],
    ids=["other-target", "other-input", "extra-input"],
)
def test_a_proposal_of_another_call_supersedes_nothing(
    tmp_path: Path, target: str, inputs: dict[str, object]
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    first = propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    propose(
        _proposal(repository, target=target, inputs=inputs),
        store,
        lambda: LATER,
        _Runner(),
    )

    assert store.load(first.decision_id).status is DecisionStatus.PROPOSED


def test_superseding_never_touches_a_decision_that_ran(tmp_path: Path) -> None:
    """Only an open proposal is replaced: what ran stays what it was."""
    repository = _repository(tmp_path)
    store = _store(repository)
    first = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    approved = approve(
        ApprovalRequest(
            decision=first, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        _Runner(),
    )

    propose(_proposal(repository), store, lambda: LATER, _Runner())

    assert store.load(first.decision_id) == approved
    assert approved.status is DecisionStatus.VERIFIED


def test_a_read_run_supersedes_a_read_recorded_before_ran_existed(
    tmp_path: Path,
) -> None:
    """A record on disk that says ``proposed`` for a read still loads (#26)."""
    repository = _repository(tmp_path, deploy=DEPLOY, facts=FACTS)
    store = _store(repository)
    old = _legacy_read(repository, store, MOMENT)
    assert old.status is DecisionStatus.PROPOSED

    new = propose(
        _proposal(repository, "facts", target=None, inputs={}),
        store,
        lambda: LATER,
        _Runner(),
    )

    assert new.status is DecisionStatus.RAN
    assert store.load(old.decision_id).superseded_by == new.decision_id


def test_a_superseded_read_does_not_point_at_a_run_to_approve(
    tmp_path: Path,
) -> None:
    """The read that replaced it ran already; there is nothing to approve."""
    repository = _repository(tmp_path, deploy=DEPLOY, facts=FACTS)
    store = _store(repository)
    old = _legacy_read(repository, store, MOMENT)
    new = propose(
        _proposal(repository, "facts", target=None, inputs={}),
        store,
        lambda: LATER,
        _Runner(),
    )

    with pytest.raises(DecisionError) as error:
        refuse_unapprovable(store.load(old.decision_id))

    assert error.value.code == "decision_not_proposed"
    assert new.decision_id.value in error.value.detail
    assert "approve that one" not in error.value.detail
    assert "ran when it was proposed" in error.value.detail


@pytest.mark.parametrize("status", ["ran", "superseded"])
def test_approving_a_decision_that_is_not_open_is_refused(
    tmp_path: Path, status: str
) -> None:
    repository = _repository(tmp_path, deploy=DEPLOY, facts=FACTS)
    store = _store(repository)
    if status == "ran":
        decision = propose(
            _proposal(repository, "facts", target=None, inputs={}),
            store,
            lambda: MOMENT,
            _Runner(),
        )
    else:
        first = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
        propose(_proposal(repository), store, lambda: LATER, _Runner())
        decision = store.load(first.decision_id)
    run = _Runner()

    with pytest.raises(DecisionError) as error:
        approve(
            ApprovalRequest(
                decision=decision,
                approver="roman",
                repository=repository,
                via=TERMINAL,
            ),
            store,
            lambda: MOMENT,
            run,
        )

    assert error.value.code == "decision_not_proposed"
    assert run.recorded == []
    assert store.load(decision.decision_id) == decision


def test_the_issue_repro_lists_ran_superseded_and_proposed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A read, then the same mutating proposal twice (#26)."""
    repository = _repository(tmp_path, deploy=DEPLOY, facts=FACTS)
    store = _store(repository)
    propose(
        _proposal(repository, "facts", target=None, inputs={}),
        store,
        lambda: MOMENT,
        _Runner(),
    )
    propose(_proposal(repository), store, lambda: LATER, _Runner())
    propose(_proposal(repository), store, lambda: LATEST, _Runner())

    exit_code = main(["decisions", "list", "--repository", str(repository)])
    every = json.loads(capsys.readouterr().out)
    open_only = main(
        [
            *("decisions", "list", "--repository", str(repository)),
            *("--status", "proposed"),
        ]
    )
    waiting = json.loads(capsys.readouterr().out)
    settled = main(
        [
            *("decisions", "list", "--repository", str(repository)),
            *("--status", "ran", "--status", "superseded"),
        ]
    )
    closed = json.loads(capsys.readouterr().out)

    assert exit_code == open_only == settled == 0
    assert [entry["spec"]["status"] for entry in every["data"]["decisions"]] == [
        "ran",
        "superseded",
        "proposed",
    ]
    assert [entry["metadata"]["id"] for entry in waiting["data"]["decisions"]] == [
        "deploy-20260921150000"
    ]
    assert [entry["spec"]["status"] for entry in closed["data"]["decisions"]] == [
        "ran",
        "superseded",
    ]


def test_an_unknown_status_filter_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)

    exit_code = main(
        [
            *("decisions", "list", "--repository", str(repository)),
            *("--status", "stale"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"]["code"] == "ARG_ERROR"


def test_the_cli_proposes_and_then_lists_the_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    _observations(repository, "web-1")
    (repository / "playbooks" / "deploy.yml").write_text(
        "---\n- hosts: localhost\n  connection: local\n  gather_facts: false\n"
        "  tasks:\n    - name: Nothing\n      ansible.builtin.debug:\n"
        "        msg: nothing\n",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "operations",
            "propose",
            "deploy",
            "--repository",
            str(repository),
            "--target",
            "localhost",
            "--input",
            "version=1.4.0",
        ]
    )

    proposed = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert proposed["data"]["decision"]["spec"]["status"] == "proposed"
    assert proposed["data"]["decision"]["spec"]["gate"]["requirement"] == (
        "check-then-approve"
    )

    listed = main(["decisions", "list", "--repository", str(repository)])

    payload = json.loads(capsys.readouterr().out)
    assert listed == 0
    assert len(payload["data"]["decisions"]) == 1
    assert payload["data"]["decisions"][0]["spec"]["operation"]["id"] == "deploy"


def test_the_cli_returns_what_a_read_operation_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    (repository / "playbooks" / "facts.yml").write_text(
        "---\n- hosts: localhost\n  connection: local\n  gather_facts: false\n"
        "  tasks:\n    - name: Report\n      ansible.builtin.debug:\n"
        "        msg: /var/log/shop holds 36G\n",
        encoding="utf-8",
    )

    exit_code = main(
        ["operations", "propose", "facts", "--repository", str(repository)]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert "/var/log/shop holds 36G" in payload["data"]["output"]
    assert payload["data"]["_trusted"] is False
    assert payload["data"]["next"] == []


def test_the_cli_returns_no_output_for_a_proposed_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    _observations(repository, "web-1")
    (repository / "playbooks" / "deploy.yml").write_text(
        "---\n- hosts: localhost\n  connection: local\n  gather_facts: false\n"
        "  tasks:\n    - name: Nothing\n      ansible.builtin.debug:\n"
        "        msg: nothing\n",
        encoding="utf-8",
    )

    main(
        [
            "operations",
            "propose",
            "deploy",
            "--repository",
            str(repository),
            "--target",
            "localhost",
            "--input",
            "version=1.4.0",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["output"] is None


def test_the_cli_reports_a_gate_refusal_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)

    exit_code = main(
        [
            "operations",
            "propose",
            "deploy",
            "--repository",
            str(repository),
            "--input",
            "version=1.4.0",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["code"] == "PRECONDITION"
    assert payload["error"]["context"]["code"] == "decision_target_required"


def test_the_record_is_readable_without_a_catalog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """What was proposed stays readable however the catalog changes."""
    repository = _repository(tmp_path)
    propose(_proposal(repository), _store(repository), lambda: MOMENT, _Runner())
    for path in (repository / "operations").iterdir():
        path.unlink()
    (repository / "operations").rmdir()

    exit_code = main(["decisions", "list", "--repository", str(repository)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert [entry["metadata"]["id"] for entry in payload["data"]["decisions"]] == [
        "deploy-20260921143012"
    ]


def test_an_approval_runs_the_recorded_proposal_and_verifies_it(
    tmp_path: Path,
) -> None:
    """The record decides what runs, so a human approves what they read."""
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    run = _Runner()

    approved = approve(
        ApprovalRequest(
            decision=proposed, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        run,
    )

    assert approved.status is DecisionStatus.VERIFIED
    assert approved.approval is not None
    assert approved.approval.approver == "roman"
    assert approved.approval.approved_at == "2026-09-21T14:30:12Z"
    assert approved.execution is not None
    assert approved.execution.changed == ("web-1",)
    assert approved.verification is not None
    assert approved.verification.changed == ()
    assert approved.verdict == (
        "the run succeeded and the verify step changed nothing"
    )
    assert [argv[1] for argv in run.recorded] == ["--diff", "--diff"]
    assert run.recorded[0][-1].endswith("playbooks/deploy.yml")
    assert run.recorded[1][-1].endswith("playbooks/health.yml")


def test_an_approved_run_is_not_check_mode(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    run = _Runner()

    approve(
        ApprovalRequest(
            decision=proposed, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        run,
    )

    assert all("--check" not in argv for argv in run.recorded)


def _approved(
    repository: Path, store: DecisionStore, proposed: Decision, run: _Runner
) -> Decision:
    return approve(
        ApprovalRequest(
            decision=proposed, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        run,
    )


def _refused_as_changed(
    repository: Path, store: DecisionStore, proposed: Decision
) -> str:
    """Approve, expecting a refusal before anything runs; return its message."""
    run = _Runner()
    with pytest.raises(DecisionError) as error:
        _approved(repository, store, proposed, run)
    assert error.value.code == "decision_binding_changed"
    assert run.recorded == []
    assert store.load(proposed.decision_id).status is DecisionStatus.PROPOSED
    return error.value.detail


def _git(repository: Path, *arguments: str) -> None:
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603 - resolved binary, fixed arguments.
        (
            git,
            *("-c", "user.name=Test", "-c", "user.email=test@example.invalid"),
            *("-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"),
            "-C",
            str(repository),
            *arguments,
        ),
        check=True,
        capture_output=True,
        encoding="utf-8",
    )


def _git_project(tmp_path: Path) -> Path:
    """Return a project committed to git, with a role its playbook reads."""
    repository = _repository(tmp_path)
    role = repository / "roles" / "web" / "tasks"
    role.mkdir(parents=True)
    (role / "main.yml").write_text("---\n[]\n", encoding="utf-8")
    _git(repository, "init", "--quiet")
    _git(repository, "add", "--all")
    _git(repository, "commit", "--quiet", "--message", "fleet")
    return repository


@pytest.mark.parametrize(
    ("edited", "named"),
    [
        ("playbooks/deploy.yml", "the playbook playbooks/deploy.yml"),
        ("playbooks/health.yml", "the verify playbook playbooks/health.yml"),
        (
            "tmp/output/decisions/deploy-20260921143012.diff",
            "the diff file tmp/output/decisions/",
        ),
    ],
    ids=["playbook", "verify-playbook", "diff"],
)
def test_an_edit_after_the_proposal_refuses_its_approval(
    tmp_path: Path, edited: str, named: str
) -> None:
    """The approval is of what was checked: an edit since is refused (#352)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    assert proposed.binding is not None
    assert proposed.binding.commit is None
    assert proposed.binding.worktree_sha256 is None

    (repository / edited).write_text("--- edited\n", encoding="utf-8")

    message = _refused_as_changed(repository, store, proposed)
    assert named in message
    assert "propose it again" in message


_SECRET = "hunter2-the-database-password"  # noqa: S105 - a fixture, printed on purpose.


class _SecretRunner(_Runner):
    """A team playbook that templates a password without ``no_log``: it prints it."""

    def __call__(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
        diff_path: Path,
    ) -> int:
        exit_code = super().__call__(argv, environment, diff_path)
        diff_path.write_text(f"+password={_SECRET}\n", encoding="utf-8")
        return exit_code


def test_a_secret_a_playbook_prints_reaches_no_file_under_decisions(
    tmp_path: Path,
) -> None:
    """The raw diff and run logs stay under the ignored tmp/ (#357, D-24)."""
    repository = _git_project(tmp_path)
    store = _store(repository)

    proposed = propose(
        _proposal(repository), store, lambda: MOMENT, _SecretRunner()
    )
    approved = _approved(repository, store, proposed, _SecretRunner())

    assert approved.status is DecisionStatus.VERIFIED
    assert approved.execution is not None
    assert approved.verification is not None
    committed = [path for path in store.directory.rglob("*") if path.is_file()]
    assert committed
    for path in committed:
        assert _SECRET not in path.read_text(encoding="utf-8"), path
    raw = repository / "tmp" / "output" / "decisions"
    for artifact in (
        proposed.check.diff,
        approved.execution.log,
        approved.verification.log,
    ):
        assert not artifact.path.is_absolute()
        assert (repository / artifact.path).parent == raw
        content = (repository / artifact.path).read_bytes()
        assert _SECRET.encode() in content
        assert artifact.size == len(content)
        assert (repository / artifact.path).stat().st_mode & 0o777 == 0o600


def test_approve_without_the_raw_diff_is_refused_naming_it(tmp_path: Path) -> None:
    """Another clone has the record, not the diff: nobody approves unseen (#357)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    (repository / proposed.check.diff.path).unlink()
    run = _Runner()

    with pytest.raises(DecisionError) as error:
        _approved(repository, store, proposed, run)

    assert error.value.code == "decision_diff_missing"
    assert str(proposed.check.diff.path) in error.value.detail
    assert proposed.check.diff.sha256 in error.value.detail
    assert run.recorded == []
    assert store.load(proposed.decision_id).status is DecisionStatus.PROPOSED


@pytest.mark.parametrize(
    ("store", "output"),
    [
        ("decisions", "tmp/output/decisions"),
        ("audit/decisions", "tmp/output/audit/decisions"),
        ("tmp/eval/0/decisions", "tmp/eval/0/decisions"),
    ],
    ids=["default", "elsewhere-in-the-project", "already-ignored"],
)
def test_every_store_keeps_its_raw_output_out_of_git(
    tmp_path: Path, store: str, output: str
) -> None:
    """A ``--decisions`` folder gets its own raw output folder under tmp/ (#357)."""
    assert raw_output_directory(tmp_path, tmp_path / store) == tmp_path / output


def test_a_record_whose_diff_is_beside_it_still_approves(tmp_path: Path) -> None:
    """A record written before #357 cites its diff in decisions/, read in place."""
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    legacy = Path(DECISION_DIRECTORY) / f"{proposed.decision_id.value}.diff"
    (repository / proposed.check.diff.path).rename(repository / legacy)
    path = repository / DECISION_DIRECTORY / f"{proposed.decision_id.value}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["spec"]["check"]["diff"]["path"] = legacy.as_posix()
    path.write_text(json.dumps(document), encoding="utf-8")
    older = store.load(proposed.decision_id)

    approved = _approved(repository, store, older, _Runner())

    assert approved.status is DecisionStatus.VERIFIED
    assert approved.check.diff.path == legacy


def test_a_role_edited_in_a_git_project_refuses_the_approval(
    tmp_path: Path,
) -> None:
    """Roles, templates and inventory are bound through git (#352)."""
    repository = _git_project(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    assert proposed.binding is not None
    assert proposed.binding.commit is not None
    assert proposed.binding.worktree_sha256 is not None

    (repository / "roles" / "web" / "tasks" / "main.yml").write_text(
        "---\n- name: Something else\n", encoding="utf-8"
    )

    message = _refused_as_changed(repository, store, proposed)
    assert "the uncommitted changes in the project" in message


def test_a_commit_after_the_proposal_refuses_the_approval(tmp_path: Path) -> None:
    repository = _git_project(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    (repository / "inventory.yml").write_text("all: {}\n", encoding="utf-8")
    _git(repository, "add", "inventory.yml")
    _git(repository, "commit", "--quiet", "--message", "inventory")

    message = _refused_as_changed(repository, store, proposed)
    assert "the git commit" in message


def test_an_untouched_git_project_approves_as_before(tmp_path: Path) -> None:
    """What propose writes itself, the record, diff and observations, is not a change.

    The tree was dirty when the proposal was made, which is allowed: only a
    change after it refuses the approval.
    """
    repository = _git_project(tmp_path)
    (repository / "playbooks" / "uncommitted.yml").write_text(
        "---\n[]\n", encoding="utf-8"
    )
    (repository / "record").mkdir()
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    (repository / "record" / "receipt.json").write_text("{}\n", encoding="utf-8")
    (repository / "tmp" / "observed" / "web-2.json").write_text(
        "{}\n", encoding="utf-8"
    )

    approved = _approved(repository, store, proposed, _Runner())

    assert approved.status is DecisionStatus.VERIFIED
    assert store.load(proposed.decision_id).binding == proposed.binding


def test_a_git_project_with_no_commit_yet_binds_its_files(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _git(repository, "init", "--quiet")
    _git(repository, "add", "--all")
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    assert proposed.binding is not None
    assert proposed.binding.commit is None
    assert proposed.binding.worktree_sha256 is not None

    (repository / "operations" / "deploy.yml").write_text(
        DEPLOY + "# edited\n", encoding="utf-8"
    )

    message = _refused_as_changed(repository, store, proposed)
    assert "the uncommitted changes in the project" in message


def test_an_edit_inside_a_dirty_submodule_refuses_the_approval(
    tmp_path: Path,
) -> None:
    """A role kept as a submodule is bound by its content, not its dirty flag."""
    role = tmp_path / "role"
    (role / "tasks").mkdir(parents=True)
    (role / "tasks" / "main.yml").write_text("---\n[]\n", encoding="utf-8")
    _git(role, "init", "--quiet")
    _git(role, "add", "--all")
    _git(role, "commit", "--quiet", "--message", "role")
    repository = _git_project(tmp_path)
    _git(
        repository,
        *("-c", "protocol.file.allow=always", "submodule", "add", "--quiet"),
        *(str(role), "roles/db"),
    )
    _git(repository, "commit", "--quiet", "--message", "db role")
    task = repository / "roles" / "db" / "tasks" / "main.yml"
    task.write_text("---\n- name: Reviewed\n", encoding="utf-8")
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    task.write_text("---\n- name: Not what was reviewed\n", encoding="utf-8")

    message = _refused_as_changed(repository, store, proposed)
    assert "the uncommitted changes in the project" in message


def test_binding_does_not_run_the_projects_fsmonitor_hook(tmp_path: Path) -> None:
    """The project's git config runs no command while a proposal is bound."""
    repository = _git_project(tmp_path)
    marker = tmp_path / "fsmonitor-ran"
    hook = tmp_path / "fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    _git(repository, "config", "core.fsmonitor", str(hook))
    store = _store(repository)

    propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    assert not marker.exists()


def test_a_proposal_recorded_before_bindings_is_refused(tmp_path: Path) -> None:
    """Nothing says what an older proposal was checked against (#352)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    path = repository / DECISION_DIRECTORY / f"{proposed.decision_id.value}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["spec"]["binding"]
    path.write_text(json.dumps(document), encoding="utf-8")
    older = store.load(proposed.decision_id)
    assert older.binding is None

    message = _refused_as_changed(repository, store, older)
    assert "proposed before Cloudfall recorded" in message


def test_the_cli_refuses_a_changed_proposal_before_asking_for_its_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    (repository / "playbooks" / "deploy.yml").write_text(
        "--- edited\n", encoding="utf-8"
    )

    exit_code = main(
        [
            *("decisions", "approve", proposed.decision_id.value),
            *("--repository", str(repository), "--approver", "roman"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["code"] == "PRECONDITION"
    assert payload["error"]["context"]["code"] == "decision_binding_changed"
    assert store.load(proposed.decision_id).status is DecisionStatus.PROPOSED


def test_a_failed_run_is_recorded_as_failed_and_skips_verify(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    run = _Runner(exit_code=2)

    approved = approve(
        ApprovalRequest(
            decision=proposed, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        run,
    )

    assert approved.status is DecisionStatus.FAILED
    assert approved.verification is None
    assert len(run.recorded) == 1


def test_a_run_that_verify_rejects_is_not_verified(tmp_path: Path) -> None:
    """A run nobody verified is not a run that worked."""
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    exit_codes = iter((0, 1))

    class _Staged(_Runner):
        def __call__(
            self,
            argv: Sequence[str],
            environment: Mapping[str, str],
            diff_path: Path,
        ) -> int:
            self.exit_code = next(exit_codes)
            return super().__call__(argv, environment, diff_path)

    approved = approve(
        ApprovalRequest(
            decision=proposed, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        _Staged(),
    )

    assert approved.status is DecisionStatus.FAILED
    assert approved.execution is not None
    assert approved.execution.exit_code == 0
    assert approved.verification is not None
    assert approved.verification.exit_code == 1


# Prints, starts a child that outlives it unless its process group is
# stopped, names that child's pid, then sleeps past any limit a test sets.
_SLOW_PLAYBOOK = """\
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(f"started {child.pid}", flush=True)
time.sleep(60)
"""


# Exits zero when it is told to stop, as a playbook that traps TERM might.
_ZERO_ON_TERM_PLAYBOOK = """\
import signal, sys, time
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print("started", flush=True)
time.sleep(60)
"""


def _slow_run(limit: int, script: str = _SLOW_PLAYBOOK) -> CheckRunner:
    """Run the approved runner with a short limit on a script, not on Ansible."""
    runner = approved_runner(timeout_seconds=limit)

    def run(_: Sequence[str], environment: Mapping[str, str], log: Path) -> int:
        return runner((sys.executable, "-c", script), environment, log)

    return run


def _gone(pid: int) -> bool:
    """Return whether the process has exited, waiting a moment for it to be reaped."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_an_approved_run_past_its_limit_is_stopped_and_settled(
    tmp_path: Path,
) -> None:
    """The record settles as failed and the log keeps what the run printed (#359)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    approved = approve(
        ApprovalRequest(
            decision=proposed, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        _slow_run(1),
    )

    assert approved.status is DecisionStatus.FAILED
    assert approved.verdict == "the run timed out after 1s and was stopped"
    assert approved.verification is None
    assert approved.execution is not None
    assert approved.execution.exit_code == 128 + signal.SIGTERM
    assert store.load(proposed.decision_id).status is DecisionStatus.FAILED
    log = repository / approved.execution.log.path
    started = log.read_text(encoding="utf-8")
    assert started.startswith("started ")
    assert approved.execution.log.size == len(started.encode())
    assert _gone(int(started.split()[1]))


def test_a_stopped_run_that_exits_zero_still_fails(tmp_path: Path) -> None:
    """A run stopped at its limit did not finish, whatever its exit code says."""
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    approved = approve(
        ApprovalRequest(
            decision=proposed, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        _slow_run(1, _ZERO_ON_TERM_PLAYBOOK),
    )

    assert approved.status is DecisionStatus.FAILED
    assert approved.verdict == "the run timed out after 1s and was stopped"
    assert approved.verification is None


def test_the_approved_run_has_its_own_limit_and_the_check_keeps_its_own() -> None:
    assert APPROVED_RUN_TIMEOUT_SECONDS == 21600
    assert CHECK_TIMEOUT_SECONDS == 1800


def test_an_operation_without_verify_is_executed_not_verified(
    tmp_path: Path,
) -> None:
    """Only a read declares no verify step, and only one recorded before #26 waits."""
    repository = _repository(tmp_path, deploy=DEPLOY, facts=FACTS)
    store = _store(repository)
    proposed = _legacy_read(repository, store, MOMENT)

    approved = approve(
        ApprovalRequest(
            decision=proposed, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        _Runner(),
    )

    assert approved.status is DecisionStatus.EXECUTED


def test_one_proposal_is_approved_once(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    request = ApprovalRequest(
        decision=proposed, approver="roman", repository=repository, via=TERMINAL
    )
    approved = approve(request, store, lambda: MOMENT, _Runner())

    with pytest.raises(DecisionError) as error:
        approve(
            ApprovalRequest(
                decision=approved, approver="roman", repository=repository, via=TERMINAL
            ),
            store,
            lambda: MOMENT,
            _Runner(),
        )

    assert error.value.code == "decision_not_proposed"


class _KilledError(RuntimeError):
    """Stands in for the process dying while a playbook runs."""


class _DyingRunner:
    """A runner whose playbook never returns: the process ends mid-run (#217)."""

    def __init__(self, store: DecisionStore) -> None:
        self.store = store
        self.seen: list[DecisionStatus] = []

    def __call__(self, *_invocation: object) -> int:
        self.seen.extend(entry.status for entry in self.store.list().records)
        message = "killed while the playbook ran"
        raise _KilledError(message)


def test_the_approval_is_recorded_before_the_first_playbook_starts(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    run = _DyingRunner(store)

    with pytest.raises(_KilledError):
        approve(
            ApprovalRequest(
                decision=proposed, approver="roman", repository=repository, via=TERMINAL
            ),
            store,
            lambda: MOMENT,
            run,
        )

    assert run.seen == [DecisionStatus.RUNNING]
    interrupted = store.load(proposed.decision_id)
    assert interrupted.status is DecisionStatus.RUNNING
    assert interrupted.approval is not None
    assert interrupted.approval.approver == "roman"
    assert interrupted.execution is None


def test_a_run_killed_midway_is_never_approved_again(tmp_path: Path) -> None:
    """The record a killed run left is not an open proposal (#217)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    request = ApprovalRequest(
        decision=proposed, approver="roman", repository=repository, via=TERMINAL
    )
    with pytest.raises(_KilledError):
        approve(request, store, lambda: MOMENT, _DyingRunner(store))
    run = _Runner()

    # The caller still holds the proposal as it was read before the run.
    with pytest.raises(DecisionError) as stale:
        approve(request, store, lambda: LATER, run)
    with pytest.raises(DecisionError) as reloaded:
        approve(
            replace(request, decision=store.load(proposed.decision_id)),
            store,
            lambda: LATER,
            run,
        )

    assert stale.value.code == "decision_run_interrupted"
    assert reloaded.value.code == "decision_run_interrupted"
    assert run.recorded == []
    assert store.load(proposed.decision_id).status is DecisionStatus.RUNNING


def test_the_cli_refuses_an_interrupted_run_as_a_precondition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    with pytest.raises(_KilledError):
        approve(
            ApprovalRequest(
                decision=proposed, approver="roman", repository=repository, via=TERMINAL
            ),
            store,
            lambda: MOMENT,
            _DyingRunner(store),
        )

    exit_code = main(
        [
            *("decisions", "approve", proposed.decision_id.value),
            *("--repository", str(repository), "--approver", "roman"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["code"] == "PRECONDITION"
    assert payload["error"]["context"]["code"] == "decision_run_interrupted"
    assert "interrupted" in payload["error"]["message"]
    assert (
        f"cloudfall decisions mark-interrupted {proposed.decision_id.value} "
        "--confirm-dead" in payload["error"]["message"]
    )


def _killed(repository: Path, store: DecisionStore) -> Decision:
    """Propose and approve one decision whose run dies midway; it stays running."""
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    with pytest.raises(_KilledError):
        approve(
            ApprovalRequest(
                decision=proposed, approver="roman", repository=repository, via=TERMINAL
            ),
            store,
            lambda: MOMENT,
            _DyingRunner(store),
        )
    return store.load(proposed.decision_id)


def test_a_run_a_person_confirmed_dead_is_closed_as_interrupted(
    tmp_path: Path,
) -> None:
    """A running record has a way out, and the record says who took it (#244)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    running = _killed(repository, store)

    closed = mark_interrupted(store, running.decision_id, "alice", lambda: LATER)

    assert closed.status is DecisionStatus.INTERRUPTED
    assert closed.interruption is not None
    assert closed.interruption.confirmed_by == "alice"
    assert closed.interruption.confirmed_at == "2026-09-21T14:45:00Z"
    assert closed.approval == running.approval
    assert closed.execution is None
    assert closed.verdict is not None
    assert "alice" in closed.verdict
    assert store.load(running.decision_id) == closed
    on_disk = json.loads(
        (
            repository / DECISION_DIRECTORY / f"{running.decision_id.value}.json"
        ).read_text(encoding="utf-8")
    )
    assert on_disk["spec"]["status"] == "interrupted"
    assert on_disk["spec"]["interruption"] == {
        "confirmedBy": "alice",
        "confirmedAt": "2026-09-21T14:45:00Z",
    }


def test_an_interrupted_decision_is_never_approved(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    running = _killed(repository, store)
    closed = mark_interrupted(store, running.decision_id, "alice", lambda: LATER)

    with pytest.raises(DecisionError) as error:
        refuse_unapprovable(closed)

    assert error.value.code == "decision_not_proposed"


@pytest.mark.parametrize("status", ["proposed", "interrupted"])
def test_only_a_running_decision_is_closed_as_interrupted(
    tmp_path: Path, status: str
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    if status == "proposed":
        record = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    else:
        running = _killed(repository, store)
        record = mark_interrupted(store, running.decision_id, "alice", lambda: LATER)

    with pytest.raises(DecisionError) as error:
        mark_interrupted(store, record.decision_id, "bob", lambda: LATEST)

    assert error.value.code == "decision_not_running"
    assert store.load(record.decision_id) == record


@pytest.mark.parametrize("confirmed_by", ["  ", "al\nice", "x" * 129])
def test_an_interruption_records_a_readable_name(
    tmp_path: Path, confirmed_by: str
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    running = _killed(repository, store)

    with pytest.raises(DecisionError) as error:
        mark_interrupted(store, running.decision_id, confirmed_by, lambda: LATER)

    assert error.value.code == "record_confirmer_invalid"
    assert store.load(running.decision_id) == running


def _mark_interrupted(repository: Path, decision_id: str, *flags: str) -> list[str]:
    return [
        *("decisions", "mark-interrupted", decision_id),
        *("--repository", str(repository)),
        *flags,
    ]


def test_the_cli_closes_a_running_decision_only_once_the_person_confirms(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    running = _killed(repository, store)
    identifier = running.decision_id.value

    unconfirmed = main(_mark_interrupted(repository, identifier, "--by", "alice"))
    refused = json.loads(capsys.readouterr().out)
    still = store.load(running.decision_id)
    confirmed = main(
        _mark_interrupted(repository, identifier, "--by", "alice", "--confirm-dead")
    )
    closed = json.loads(capsys.readouterr().out)

    assert unconfirmed == 2, refused
    assert "--confirm-dead" in refused["error"]["message"]
    assert still.status is DecisionStatus.RUNNING
    assert confirmed == 0, closed
    assert closed["data"]["decision"]["spec"]["status"] == "interrupted"
    assert closed["data"]["decision"]["spec"]["interruption"]["confirmedBy"] == "alice"
    assert store.load(running.decision_id).status is DecisionStatus.INTERRUPTED


def test_the_cli_refuses_to_close_a_decision_that_is_not_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    exit_code = main(
        _mark_interrupted(
            repository, proposed.decision_id.value, "--by", "alice", "--confirm-dead"
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["code"] == "PRECONDITION"
    assert payload["error"]["context"]["code"] == "decision_not_running"
    assert store.load(proposed.decision_id) == proposed


def test_closing_a_run_as_interrupted_is_never_a_tool() -> None:
    command = app.commands[CommandPath("decisions.mark-interrupted")]

    assert command.mcp is False


def test_a_new_record_is_created_once_and_no_temporary_file_stays(
    tmp_path: Path,
) -> None:
    """Two writers of one id: exactly one record, written whole (#217)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    decision = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    path = repository / DECISION_DIRECTORY / f"{decision.decision_id.value}.json"
    written = path.read_text(encoding="utf-8")
    rival = replace(decision, reason="another writer of the same id")

    with pytest.raises(DecisionError) as error:
        _store(repository).save(rival)

    assert error.value.code == "decision_exists"
    assert path.read_text(encoding="utf-8") == written
    leftovers = [
        entry.name for entry in path.parent.iterdir() if entry.name.startswith(".")
    ]
    assert leftovers == []


def test_concurrent_writers_of_one_id_save_it_once(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    decision = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    fresh = replace(decision, decision_id=ResourceId.from_boundary("deploy-twin"))
    start = threading.Barrier(8)

    def _save(_index: int) -> str:
        start.wait()
        try:
            _store(repository).save(fresh)
        except DecisionError as error:
            return error.code
        return "saved"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = sorted(pool.map(_save, range(8)))

    assert outcomes == ["decision_exists"] * 7 + ["saved"]
    assert store.load(fresh.decision_id) == fresh


def test_a_proposal_approved_during_a_newer_check_is_not_superseded(
    tmp_path: Path,
) -> None:
    """An older proposal approved while a newer check runs keeps its run (#217).

    The newer proposal reads the store before its check mode starts.
    """
    repository = _repository(tmp_path)
    store = _store(repository)
    older = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    approvals: list[Decision] = []
    check = _Runner()

    def _check_while_approved(
        argv: Sequence[str], environment: Mapping[str, str], diff_path: Path
    ) -> int:
        approvals.append(
            approve(
                ApprovalRequest(
                    decision=older,
                    approver="roman",
                    repository=repository,
                    via=TERMINAL,
                ),
                store,
                lambda: MOMENT,
                _Runner(),
            )
        )
        return check(argv, environment, diff_path)

    propose(_proposal(repository), store, lambda: LATER, _check_while_approved)

    kept = store.load(older.decision_id)
    assert kept.status is approvals[0].status
    assert kept.approval is not None
    assert kept.execution is not None
    assert kept.superseded_by is None


def test_a_replayed_decision_is_never_approved(tmp_path: Path) -> None:
    """Its check output came from a recording, not the hosts (#36)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    request = _proposal(repository)
    replayed = propose(
        ProposalRequest(
            operation=request.operation,
            targets=request.targets,
            inputs=request.inputs,
            repository=request.repository,
            observations=request.observations,
            replay="recordings/disk-full",
        ),
        store,
        lambda: MOMENT,
        _Runner(),
    )
    run = _Runner()

    with pytest.raises(DecisionError) as error:
        approve(
            ApprovalRequest(
                decision=store.load(replayed.decision_id),
                approver="roman",
                repository=repository,
                via=TERMINAL,
            ),
            store,
            lambda: MOMENT,
            run,
        )

    assert replayed.replay == "recordings/disk-full"
    assert error.value.code == "decision_replayed"
    assert "recordings/disk-full" in error.value.detail
    assert run.recorded == []
    assert store.load(replayed.decision_id) == replayed


def test_a_replayed_decision_since_superseded_is_refused_as_replayed(
    tmp_path: Path,
) -> None:
    """Replayed is said first: no later proposal makes it approvable (#36, #26)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    request = _proposal(repository)
    replayed = propose(
        replace(request, replay="recordings/disk-full"),
        store,
        lambda: MOMENT,
        _Runner(),
    )
    newer = propose(
        replace(request, replay="recordings/disk-full"),
        store,
        lambda: LATER,
        _Runner(),
    )
    superseded = store.load(replayed.decision_id)
    run = _Runner()

    with pytest.raises(DecisionError) as error:
        approve(
            ApprovalRequest(
                decision=superseded,
                approver="roman",
                repository=repository,
                via=TERMINAL,
            ),
            store,
            lambda: MOMENT,
            run,
        )

    assert superseded.status is DecisionStatus.SUPERSEDED
    assert superseded.superseded_by == newer.decision_id
    assert error.value.code == "decision_replayed"
    assert "replay of recordings/disk-full" in error.value.detail
    assert run.recorded == []
    assert store.load(replayed.decision_id) == superseded


@pytest.mark.parametrize(
    ("older_replay", "newer_replay", "supersedes"),
    [
        (None, None, True),
        (None, "recordings/disk-full", False),
        ("recordings/disk-full", None, False),
        ("recordings/disk-full", "recordings/disk-full", True),
        ("recordings/disk-full", "recordings/other", False),
    ],
    ids=[
        "live-then-live",
        "live-then-replay",
        "replay-then-live",
        "replay-then-same-replay",
        "replay-then-other-replay",
    ],
)
def test_only_a_proposal_of_the_same_replay_value_supersedes(
    tmp_path: Path,
    *,
    older_replay: str | None,
    newer_replay: str | None,
    supersedes: bool,
) -> None:
    """A replay never closes a live proposal, nor a live run a replayed one (#58)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    request = _proposal(repository)
    older = propose(
        replace(request, replay=older_replay), store, lambda: MOMENT, _Runner()
    )

    newer = propose(
        replace(request, replay=newer_replay), store, lambda: LATER, _Runner()
    )

    reloaded = store.load(older.decision_id)
    if supersedes:
        assert reloaded.status is DecisionStatus.SUPERSEDED
        assert reloaded.superseded_by == newer.decision_id
    else:
        assert reloaded == older
        assert reloaded.status is DecisionStatus.PROPOSED


def test_a_replayed_read_says_its_output_was_played_back(tmp_path: Path) -> None:
    """It is recorded ran, but it never ran on a host (#58)."""
    repository = _repository(tmp_path, deploy=DEPLOY, facts=FACTS)
    store = _store(repository)

    decision = propose(
        replace(
            _proposal(repository, "facts", target=None, inputs={}),
            replay="recordings/disk-full",
        ),
        store,
        lambda: MOMENT,
        _Runner(exit_code=2),
    )

    assert decision.status is DecisionStatus.RAN
    assert decision.ran_exit_code == 2
    assert decision.verdict is not None
    assert "played back from the recording recordings/disk-full" in decision.verdict
    assert "not run on the hosts" in decision.verdict
    assert "runs when it is proposed" not in decision.verdict


def test_a_live_read_keeps_its_verdict(tmp_path: Path) -> None:
    repository = _repository(tmp_path, deploy=DEPLOY, facts=FACTS)

    decision = propose(
        _proposal(repository, "facts", target=None, inputs={}),
        _store(repository),
        lambda: MOMENT,
        _Runner(exit_code=0),
    )

    assert decision.verdict == (
        "a read operation runs when it is proposed; it exited 0"
    )


def test_a_record_without_replay_reads_as_live(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)

    decision = propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    assert store.load(decision.decision_id).replay is None
    assert "replay" not in json.dumps(decision.as_document())


def test_an_approval_records_who_gave_it(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    with pytest.raises(DecisionError) as error:
        approve(
            ApprovalRequest(
                decision=proposed, approver="  ", repository=repository, via=TERMINAL
            ),
            store,
            lambda: MOMENT,
            _Runner(),
        )

    assert error.value.code == "decision_approver_unknown"


class _Terminal(io.StringIO):
    """A stream that says it is a terminal, as a person's shell is."""

    def isatty(self) -> bool:
        return True


LOCAL_PLAY = (
    "---\n- hosts: localhost\n  connection: local\n  gather_facts: false\n"
    "  tasks:\n    - name: Nothing\n      ansible.builtin.debug:\n"
    "        msg: nothing\n"
)


def _proposed_locally(repository: Path, capsys: pytest.CaptureFixture[str]) -> str:
    """Propose deploy against localhost through the CLI; return the decision id."""
    _observations(repository, "web-1")
    for name in ("deploy.yml", "health.yml"):
        (repository / "playbooks" / name).write_text(LOCAL_PLAY, encoding="utf-8")
    exit_code = main(
        [
            *("operations", "propose", "deploy"),
            *("--repository", str(repository), "--target", "localhost"),
            *("--input", "version=1.4.0"),
        ]
    )
    proposed = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    return str(proposed["data"]["decision"]["metadata"]["id"])


def _approve_at_terminal(
    repository: Path, decision_id: str, typed: str
) -> tuple[int, dict[str, object]]:
    """Run ``decisions approve`` as a person at a terminal who types ``typed``."""
    stdout = _Terminal()
    exit_code = app.run(
        [
            *("decisions", "approve", decision_id),
            *("--repository", str(repository), "--approver", "roman"),
            *("--format", "json"),
        ],
        stdin=_Terminal(f"{typed}\n"),
        stdout=stdout,
        stderr=io.StringIO(),
    )
    return exit_code, json.loads(stdout.getvalue())


def test_a_person_at_a_terminal_approves_by_typing_the_decision_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    decision_id = _proposed_locally(repository, capsys)

    exit_code, payload = _approve_at_terminal(repository, decision_id, decision_id)

    assert exit_code == 0, payload
    loaded = _store(repository).load(ResourceId.from_boundary(decision_id))
    assert loaded.status is DecisionStatus.VERIFIED
    assert loaded.approval is not None
    assert loaded.approval.approver == "roman"
    assert loaded.approval.via is ApprovalChannel.TERMINAL
    on_disk = json.loads(
        (repository / DECISION_DIRECTORY / f"{decision_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert on_disk["spec"]["approval"]["via"] == "terminal"


def test_a_mistyped_decision_id_approves_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    decision_id = _proposed_locally(repository, capsys)

    exit_code, payload = _approve_at_terminal(repository, decision_id, "yes")

    error = payload["error"]
    assert isinstance(error, dict)
    assert exit_code == 4
    assert error["code"] == "ATTESTATION_MISMATCH"
    loaded = _store(repository).load(ResourceId.from_boundary(decision_id))
    assert loaded.status is DecisionStatus.PROPOSED
    assert loaded.approval is None


@pytest.mark.parametrize("flags", [["--yes"], []], ids=["yes", "bare"])
def test_an_approval_off_a_terminal_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    """An agent's shell call cannot approve, ``--yes`` or not (#25)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    exit_code = main(
        [
            *("decisions", "approve", proposed.decision_id.value),
            *("--repository", str(repository), "--approver", "roman"),
            *flags,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["code"] == "PERSON_REQUIRED"
    assert payload["error"]["retryable"] is False
    assert store.load(proposed.decision_id).status is DecisionStatus.PROPOSED


def test_the_cli_refuses_a_superseded_decision_before_asking_for_its_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nobody types an id only to hear there was nothing to approve (#26)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    first = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    second = propose(_proposal(repository), store, lambda: LATER, _Runner())

    exit_code = main(
        [
            *("decisions", "approve", first.decision_id.value),
            *("--repository", str(repository), "--approver", "roman"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["code"] == "PRECONDITION"
    assert payload["error"]["context"]["code"] == "decision_not_proposed"
    assert second.decision_id.value in payload["error"]["message"]
    assert store.load(first.decision_id).status is DecisionStatus.SUPERSEDED


def test_a_raw_payload_cannot_approve_off_a_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    payload_in = {
        "decision": proposed.decision_id.value,
        "repository": str(repository),
        "approver": "roman",
        "yes": True,
    }

    exit_code = main(
        ["decisions", "approve", "--raw-payload", json.dumps(payload_in)]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["code"] == "PERSON_REQUIRED"
    assert store.load(proposed.decision_id).status is DecisionStatus.PROPOSED


def test_the_approval_command_is_a_persons_and_never_a_tool() -> None:
    command = app.commands[CommandPath("decisions.approve")]

    assert command.requires_person is True
    assert command.mcp is False


def test_a_record_written_before_the_channel_reads_as_unknown(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    approved = approve(
        ApprovalRequest(
            decision=proposed, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        _Runner(),
    )
    path = repository / DECISION_DIRECTORY / f"{approved.decision_id.value}.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["spec"]["approval"]["via"]
    path.write_text(json.dumps(document), encoding="utf-8")

    loaded = store.load(approved.decision_id)

    assert loaded.approval is not None
    assert loaded.approval.via is ApprovalChannel.UNKNOWN
    shown = json.loads(json.dumps(loaded.as_document()))
    assert shown["spec"]["approval"]["via"] == "unknown"


def test_a_new_approval_names_how_it_arrived(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    with pytest.raises(DecisionError) as error:
        approve(
            ApprovalRequest(
                decision=proposed,
                approver="roman",
                repository=repository,
                via=ApprovalChannel.UNKNOWN,
            ),
            store,
            lambda: MOMENT,
            _Runner(),
        )

    assert error.value.code == "decision_approval_channel_unknown"
    assert store.load(proposed.decision_id).status is DecisionStatus.PROPOSED


def test_an_approved_decision_round_trips_through_the_store(
    tmp_path: Path,
) -> None:
    """What came of a run is the part of the record that matters most."""
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    approved = approve(
        ApprovalRequest(
            decision=proposed, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        _Runner(),
    )

    loaded = store.load(approved.decision_id)

    assert loaded == approved
    assert loaded.execution is not None
    assert loaded.execution.exit_code == 0
    assert loaded.verification is not None
    assert loaded.status is DecisionStatus.VERIFIED


def test_a_verify_step_that_changes_the_fleet_did_not_verify_it(
    tmp_path: Path,
) -> None:
    """Exiting zero is not enough: verification is a claim about state."""
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    approved = approve(
        ApprovalRequest(
            decision=proposed, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        _Runner(verify_changed=("web-1",)),
    )

    assert approved.status is DecisionStatus.FAILED
    assert approved.verification is not None
    assert approved.verification.exit_code == 0
    assert approved.verification.changed == ("web-1",)
    assert approved.verdict is not None
    assert "the verify step changed web-1" in approved.verdict


@pytest.mark.parametrize(
    ("observed", "unobserved"),
    [((), "web-1, web-2"), (("web-1",), "web-2")],
    ids=["no-host", "one-host-skipped"],
)
def test_a_verify_step_that_did_not_observe_a_target_did_not_verify_it(
    tmp_path: Path, observed: tuple[str, ...], unobserved: str
) -> None:
    """A verify play that matched no host, or skipped one, proves nothing (#360)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    approved = _approved(
        repository, store, proposed, _Runner(verify_observed=observed)
    )

    assert approved.status is DecisionStatus.FAILED
    assert approved.verification is not None
    assert approved.verification.exit_code == 0
    assert approved.verification.changed == ()
    assert approved.verdict == (
        f"the verify step did not observe {unobserved}, so nothing verified "
        "those hosts"
    )
    assert store.load(proposed.decision_id).status is DecisionStatus.FAILED


def test_a_run_that_observed_no_host_left_nothing_to_verify(tmp_path: Path) -> None:
    """The callback wrote nothing for the run, so no host was verified (#360)."""
    repository = _repository(tmp_path)
    store = _store(repository)

    def silent(
        argv: Sequence[str], environment: Mapping[str, str], diff_path: Path
    ) -> int:
        del argv, environment
        diff_path.write_text("", encoding="utf-8")
        return 0

    proposed = propose(_proposal(repository), store, lambda: MOMENT, silent)
    approved = approve(
        ApprovalRequest(
            decision=proposed, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        silent,
    )

    assert approved.status is DecisionStatus.FAILED
    assert approved.verdict == (
        "neither the check nor the run observed any host, so the verify step "
        "had nothing to verify"
    )


def test_every_outcome_says_why_in_the_record(tmp_path: Path) -> None:
    repository = _repository(tmp_path, deploy=DEPLOY, facts=FACTS)
    store = _store(repository)

    failed = approve(
        ApprovalRequest(
            decision=propose(
                _proposal(repository), store, lambda: MOMENT, _Runner()
            ),
            approver="roman",
            repository=repository,
            via=TERMINAL,
        ),
        store,
        lambda: MOMENT,
        _Runner(exit_code=2),
    )
    executed = approve(
        ApprovalRequest(
            decision=_legacy_read(repository, store, MOMENT),
            approver="roman",
            repository=repository,
            via=TERMINAL,
        ),
        store,
        lambda: MOMENT,
        _Runner(),
    )

    assert failed.verdict == "the run exited 2"
    assert executed.verdict == (
        "the run succeeded; the operation declares no verify step"
    )


def _not_json(path: Path) -> str:
    path.write_text("{not json", encoding="utf-8")
    return "not valid JSON"


def _bad_status(path: Path) -> str:
    document = json.loads(path.read_text(encoding="utf-8"))
    document["spec"]["status"] = "bogus"
    path.write_text(json.dumps(document), encoding="utf-8")
    return "spec.status"


def _bad_via(path: Path) -> str:
    document = json.loads(path.read_text(encoding="utf-8"))
    document["spec"]["approval"]["via"] = "pigeon"
    path.write_text(json.dumps(document), encoding="utf-8")
    return "spec.approval.via"


def _old_api_version(path: Path) -> str:
    document = json.loads(path.read_text(encoding="utf-8"))
    document["apiVersion"] = "cloudfall/v0"
    path.write_text(json.dumps(document), encoding="utf-8")
    return "cloudfall/v0"


ONE_RECORD_COMMANDS: dict[str, tuple[str, ...]] = {
    "show": ("decisions", "show", "{id}"),
    "approve": ("decisions", "approve", "{id}", "--approver", "roman"),
}
"""Commands that read one record: a broken one answers RECORD_INVALID."""

LISTING_COMMANDS: dict[str, tuple[str, ...]] = {
    "list": ("decisions", "list"),
    "why": ("why",),
}
"""Commands that read every record: a broken one is skipped and named (#218)."""

RECORD_COMMANDS = {**ONE_RECORD_COMMANDS, **LISTING_COMMANDS}


def _run_on_record(
    command: str, repository: Path, decision_id: str
) -> tuple[int, dict[str, object]]:
    """Run one record-reading command as a person at a terminal."""
    argv = [part.replace("{id}", decision_id) for part in RECORD_COMMANDS[command]]
    stdout = _Terminal()
    exit_code = app.run(
        [*argv, "--repository", str(repository), "--format", "json"],
        stdin=_Terminal(f"{decision_id}\n"),
        stdout=stdout,
        stderr=io.StringIO(),
    )
    return exit_code, json.loads(stdout.getvalue())


CORRUPTIONS = pytest.mark.parametrize(
    "corrupt",
    [_not_json, _bad_status, _bad_via, _old_api_version],
    ids=["json", "status", "via", "api-version"],
)


def _approved_record(repository: Path) -> tuple[Decision, Path]:
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    approve(
        ApprovalRequest(
            decision=proposed, approver="roman", repository=repository, via=TERMINAL
        ),
        store,
        lambda: MOMENT,
        _Runner(),
    )
    path = repository / DECISION_DIRECTORY / f"{proposed.decision_id.value}.json"
    return proposed, path


@pytest.mark.parametrize("command", sorted(ONE_RECORD_COMMANDS))
@CORRUPTIONS
def test_an_invalid_record_answers_record_invalid_naming_the_file(
    tmp_path: Path, command: str, corrupt: Callable[[Path], str]
) -> None:
    """A broken record is a typed RECORD_INVALID, never a crash (#41)."""
    repository = _repository(tmp_path)
    proposed, path = _approved_record(repository)
    named = corrupt(path)

    exit_code, payload = _run_on_record(command, repository, proposed.decision_id.value)

    error = payload["error"]
    assert isinstance(error, dict)
    assert exit_code == 86, payload
    assert error["code"] == "RECORD_INVALID"
    assert error["context"]["code"] == "decision_record_invalid"
    assert str(path) in error["message"]
    assert named in error["message"]


@pytest.mark.parametrize("command", sorted(LISTING_COMMANDS))
@CORRUPTIONS
def test_a_listing_skips_an_invalid_record_and_names_it(
    tmp_path: Path, command: str, corrupt: Callable[[Path], str]
) -> None:
    """One broken record no longer hides the rest of the record (#218)."""
    repository = _repository(tmp_path)
    proposed, path = _approved_record(repository)
    store = _store(repository)
    readable = propose(_proposal(repository), store, lambda: LATER, _Runner())
    named = corrupt(path)

    exit_code, payload = _run_on_record(command, repository, proposed.decision_id.value)

    assert exit_code == 0, payload
    data = payload["data"]
    assert isinstance(data, dict)
    listed = (
        [entry["metadata"]["id"] for entry in data["decisions"]]
        if command == "list"
        else [entry["id"] for entry in data["answers"]]
    )
    assert listed == [readable.decision_id.value]
    warnings = payload["warnings"]
    assert isinstance(warnings, list)
    [warning] = warnings
    assert warning["code"] == "RECORD_UNREADABLE"
    assert warning["context"]["path"] == str(path)
    assert named in warning["context"]["reason"]


def test_a_store_refuses_an_invalid_record_with_a_decision_error(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    path = repository / DECISION_DIRECTORY / f"{proposed.decision_id.value}.json"
    _bad_status(path)

    with pytest.raises(DecisionError) as error:
        store.load(proposed.decision_id)

    assert error.value.code == "decision_record_invalid"
    assert str(path) in error.value.detail
    assert "spec.status" in error.value.detail


def test_a_store_lists_what_it_can_read_and_names_what_it_cannot(
    tmp_path: Path,
) -> None:
    """A truncated record next to good ones (#217, #218)."""
    repository = _repository(tmp_path)
    store = _store(repository)
    first = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    second = propose(
        _proposal(repository, target="web-2"), store, lambda: LATER, _Runner()
    )
    path = repository / DECISION_DIRECTORY / f"{first.decision_id.value}.json"
    path.write_text(path.read_text(encoding="utf-8")[:40], encoding="utf-8")

    listing = store.list()

    assert [entry.decision_id for entry in listing.records] == [second.decision_id]
    [unreadable] = listing.unreadable
    assert unreadable.path == path
    assert "not valid JSON" in unreadable.reason


def test_a_proposal_goes_on_past_an_unreadable_record_and_names_it(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    first = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    path = repository / DECISION_DIRECTORY / f"{first.decision_id.value}.json"
    _not_json(path)
    skipped: list[UnreadableRecord] = []

    second = propose(
        _proposal(repository), store, lambda: LATER, _Runner(), skipped=skipped.append
    )

    assert store.load(second.decision_id).status is DecisionStatus.PROPOSED
    assert [record.path for record in skipped] == [path]
    assert path.read_text(encoding="utf-8") == "{not json"


def test_the_cli_proposes_past_an_unreadable_record_with_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    first = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    path = repository / DECISION_DIRECTORY / f"{first.decision_id.value}.json"
    _not_json(path)
    for name in ("deploy.yml", "health.yml"):
        (repository / "playbooks" / name).write_text(LOCAL_PLAY, encoding="utf-8")

    exit_code = main(
        [
            *("operations", "propose", "deploy"),
            *("--repository", str(repository), "--target", "localhost"),
            *("--input", "version=1.4.0"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0, payload
    decision_id = payload["data"]["decision"]["metadata"]["id"]
    assert store.load(ResourceId.from_boundary(decision_id)).status is (
        DecisionStatus.PROPOSED
    )
    [warning] = payload["warnings"]
    assert warning["code"] == "RECORD_UNREADABLE"
    assert warning["context"]["path"] == str(path)


def test_a_new_record_names_the_cloudfall_version_that_wrote_it(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    decision = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    path = repository / DECISION_DIRECTORY / f"{decision.decision_id.value}.json"

    document = json.loads(path.read_text(encoding="utf-8"))

    assert document["spec"]["writerVersion"] == metadata.version("cloudfall")
    assert store.load(decision.decision_id) == decision
