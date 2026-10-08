"""The gate and the decision record."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cloudfall.app import app
from cloudfall.catalog import RiskLevel, TargetScope, load_catalog
from cloudfall.cli import main
from cloudfall.decision import (
    DECISION_DIRECTORY,
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
    gate,
    propose,
    read_basis,
    run_command,
)
from cloudfall.domain import ResourceId
from cloudfall.resources import default_schema_directory
from cloudfall.validation import SchemaCatalog
from test_catalog import DEPLOY, FACTS, _repository
from treaty import CommandPath

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

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


class _Runner:
    """A runner that writes what Ansible would write, stage by stage.

    An idempotent fleet changes on the run and changes nothing on the
    verify that follows, so the two stages are modelled separately.
    """

    def __init__(
        self,
        exit_code: int = 0,
        changed: tuple[str, ...] = ("web-1",),
        verify_changed: tuple[str, ...] = (),
    ) -> None:
        self.exit_code = exit_code
        self.changed = changed
        self.verify_changed = verify_changed
        self.recorded: list[tuple[str, ...]] = []

    def __call__(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
        diff_path: Path,
    ) -> int:
        tree = Path(environment["ANSIBLE_CALLBACK_TREE_DIR"])
        changed = self.verify_changed if self._verifying(tree) else self.changed
        self.recorded.append(tuple(argv))
        tree.mkdir(parents=True, exist_ok=True)
        for host in changed:
            (tree / host).write_text('{"changed": true}', encoding="utf-8")
        (tree / "web-2").write_text('{"changed": false}', encoding="utf-8")
        diff_path.write_text("--- before\n+++ after\n", encoding="utf-8")
        return self.exit_code

    @staticmethod
    def _verifying(tree: Path) -> bool:
        return tree.name.endswith("-verify")


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
    assert json.loads(argv[argv.index("--extra-vars") + 1]) == {"version": "1.4.0"}
    assert argv[-1].endswith("playbooks/deploy.yml")


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
            tree=repository / "tree",
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
    assert [entry.decision_id for entry in store.list()] == [decision.decision_id]


def test_the_same_decision_is_never_overwritten(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    with pytest.raises(DecisionError) as error:
        propose(_proposal(repository), store, lambda: MOMENT, _Runner())

    assert error.value.code == "decision_exists"


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


def test_a_read_operation_without_verify_is_executed_not_verified(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path, deploy=DEPLOY, facts=FACTS)
    store = _store(repository)
    proposed = propose(
        _proposal(repository, "facts", target=None, inputs={}),
        store,
        lambda: MOMENT,
        _Runner(),
    )

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
            decision=propose(
                _proposal(repository, "facts", target=None, inputs={}),
                store,
                lambda: MOMENT,
                _Runner(),
            ),
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


RECORD_COMMANDS: dict[str, tuple[str, ...]] = {
    "show": ("decisions", "show", "{id}"),
    "list": ("decisions", "list"),
    "approve": ("decisions", "approve", "{id}", "--approver", "roman"),
    "why": ("why",),
}


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


@pytest.mark.parametrize("command", sorted(RECORD_COMMANDS))
@pytest.mark.parametrize(
    "corrupt", [_not_json, _bad_status, _bad_via], ids=["json", "status", "via"]
)
def test_an_invalid_record_answers_record_invalid_naming_the_file(
    tmp_path: Path, command: str, corrupt: Callable[[Path], str]
) -> None:
    """A broken record is a typed RECORD_INVALID, never a crash (#41)."""
    repository = _repository(tmp_path)
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
    named = corrupt(path)

    exit_code, payload = _run_on_record(command, repository, proposed.decision_id.value)

    error = payload["error"]
    assert isinstance(error, dict)
    assert exit_code == 86, payload
    assert error["code"] == "RECORD_INVALID"
    assert error["context"]["code"] == "decision_record_invalid"
    assert str(path) in error["message"]
    assert named in error["message"]


def test_a_store_refuses_an_invalid_record_with_a_decision_error(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    store = _store(repository)
    proposed = propose(_proposal(repository), store, lambda: MOMENT, _Runner())
    path = repository / DECISION_DIRECTORY / f"{proposed.decision_id.value}.json"
    _bad_status(path)

    with pytest.raises(DecisionError) as error:
        store.list()

    assert error.value.code == "decision_record_invalid"
    assert str(path) in error.value.detail
    assert "spec.status" in error.value.detail
