"""Changed and unchanged hosts come from the play's recap, not its last task (#76)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from ansible.executor.stats import AggregateStats
from ansible.plugins.loader import callback_loader
from cloudfall.callback_plugins import cloudfall_host_stats
from cloudfall.decision import (
    HOST_STATS_CALLBACK,
    HOST_STATS_VARIABLE,
    ApprovalChannel,
    ApprovalRequest,
    DecisionError,
    DecisionStatus,
    PlaybookInvocation,
    approve,
    propose,
    run_command,
)
from cloudfall.recording import Recording
from test_catalog import _repository
from test_decision import MOMENT, _proposal, _store, _write_recap

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

PLUGIN_DIRECTORY = Path(cloudfall_host_stats.__file__).parent

# The first task changes the host and the last one reports ok, so a reader
# of the last task's result alone sees nothing changed.
CHANGED_EARLY = (
    "---\n- hosts: localhost\n  connection: local\n  gather_facts: false\n"
    "  tasks:\n"
    "    - name: Change\n      ansible.builtin.debug:\n        msg: changed\n"
    "      changed_when: true\n"
    "    - name: Report\n      ansible.builtin.debug:\n        msg: ok\n"
)


def _callback(directory: Path) -> cloudfall_host_stats.CallbackModule:
    """Load the plugin the way ``ansible-playbook`` does, from its directory."""
    callback_loader.add_directory(str(PLUGIN_DIRECTORY))
    # Ansible imports the file under its own module name, so the class is
    # a copy of the one imported here rather than the same object.
    callback: cloudfall_host_stats.CallbackModule = callback_loader.get(
        HOST_STATS_CALLBACK
    )
    assert callback.CALLBACK_NAME == HOST_STATS_CALLBACK
    callback.set_options(direct={"directory": str(directory)})
    return callback


def test_the_callback_writes_each_hosts_recap_counts(tmp_path: Path) -> None:
    stats = AggregateStats()
    for name in ("ok", "changed", "ok", "ok"):
        stats.increment(name, "hz1")
    stats.increment("ok", "hz2")
    stats.increment("failures", "hz3")
    stats.increment("dark", "hz4")

    _callback(tmp_path / "out").v2_playbook_on_stats(stats)

    written = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "out").iterdir()
    }
    assert written == {
        "hz1": {"ok": 3, "changed": 1, "failed": 0, "unreachable": 0},
        "hz2": {"ok": 1, "changed": 0, "failed": 0, "unreachable": 0},
        "hz3": {"ok": 0, "changed": 0, "failed": 1, "unreachable": 0},
        "hz4": {"ok": 0, "changed": 0, "failed": 0, "unreachable": 1},
    }


def test_the_callback_documents_the_variable_the_sdk_sets() -> None:
    assert HOST_STATS_VARIABLE in cloudfall_host_stats.DOCUMENTATION
    assert f"name: {HOST_STATS_CALLBACK}" in cloudfall_host_stats.DOCUMENTATION


def test_a_run_enables_the_callback_and_keeps_the_team_plugin_path(
    tmp_path: Path,
) -> None:
    """Setting the path replaces it, so the team's directories are carried over."""
    repository = _repository(tmp_path)
    (repository / "ansible.cfg").write_text(
        "[defaults]\ncallback_plugins = plugins/callback\n", encoding="utf-8"
    )

    _, environment = run_command(
        PlaybookInvocation(
            playbook=Path("playbooks/deploy.yml"),
            repository=repository,
            host_stats=repository / "host-stats",
        )
    )

    assert environment["ANSIBLE_CALLBACKS_ENABLED"] == HOST_STATS_CALLBACK
    assert environment["ANSIBLE_CALLBACK_PLUGINS"].split(os.pathsep) == [
        str(PLUGIN_DIRECTORY),
        str((repository / "plugins" / "callback").resolve()),
    ]
    assert environment[HOST_STATS_VARIABLE] == str(
        (repository / "host-stats").resolve()
    )
    assert "ANSIBLE_CALLBACK_TREE_DIR" not in environment


def test_a_host_changed_early_is_changed_in_check_run_and_verify(
    tmp_path: Path,
) -> None:
    """The issue's repro on a real ansible-playbook: a verify that changed fails."""
    repository = _repository(tmp_path)
    for name in ("deploy.yml", "health.yml"):
        (repository / "playbooks" / name).write_text(CHANGED_EARLY, encoding="utf-8")
    store = _store(repository)

    proposed = propose(_proposal(repository, target="localhost"), store, lambda: MOMENT)
    approved = approve(
        ApprovalRequest(
            decision=proposed,
            approver="roman",
            repository=repository,
            via=ApprovalChannel.TERMINAL,
        ),
        store,
        lambda: MOMENT,
    )

    assert proposed.check.exit_code == 0
    assert proposed.check.changed == ("localhost",)
    assert approved.execution is not None
    assert approved.execution.changed == ("localhost",)
    assert approved.verification is not None
    assert approved.verification.exit_code == 0
    assert approved.verification.changed == ("localhost",)
    assert approved.status is DecisionStatus.FAILED
    assert approved.verdict is not None
    assert "the verify step changed localhost" in approved.verdict


def test_a_recap_without_a_changed_count_is_refused(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    def run(_: Sequence[str], environment: Mapping[str, str], diff: Path) -> int:
        directory = Path(environment[HOST_STATS_VARIABLE])
        directory.mkdir(parents=True)
        (directory / "web-1").write_text('{"changed": true}', encoding="utf-8")
        diff.write_text("", encoding="utf-8")
        return 0

    with pytest.raises(DecisionError) as error:
        propose(_proposal(repository), _store(repository), lambda: MOMENT, run)

    assert error.value.code == "decision_host_stats_invalid"


def test_a_replay_leaves_what_the_callback_would_have_written(
    tmp_path: Path,
) -> None:
    """A played-back run is sorted into changed and unchanged as a live one is."""
    repository = _repository(tmp_path)
    recorded = propose(
        _proposal(repository),
        _store(repository),
        lambda: MOMENT,
        _changed_web_1,
    )
    replayed_repository = _repository(tmp_path / "replay")

    replayed = propose(
        _proposal(replayed_repository),
        _store(replayed_repository),
        lambda: MOMENT,
        Recording.load(repository / "decisions").runner(replayed_repository),
    )

    assert recorded.check.changed == replayed.check.changed == ("web-1",)
    assert recorded.check.unchanged == replayed.check.unchanged == ("web-2",)


def test_a_replay_matches_inputs_holding_jinja_as_recorded(tmp_path: Path) -> None:
    """The argv marks inputs as data; the recording keeps them as written (#181)."""
    repository = _repository(tmp_path)
    inputs: dict[str, object] = {"version": "{{ 1.4.0 }}", "force": True}
    recorded = propose(
        _proposal(repository, inputs=inputs),
        _store(repository),
        lambda: MOMENT,
        _changed_web_1,
    )
    replayed_repository = _repository(tmp_path / "replay")

    replayed = propose(
        _proposal(replayed_repository, inputs=inputs),
        _store(replayed_repository),
        lambda: MOMENT,
        Recording.load(repository / "decisions").runner(replayed_repository),
    )

    assert recorded.inputs == replayed.inputs == inputs
    assert replayed.check.exit_code == 0
    assert replayed.check.changed == ("web-1",)


def _changed_web_1(
    _: Sequence[str], environment: Mapping[str, str], diff: Path
) -> int:
    """Run check mode as Ansible would, changing web-1 and leaving web-2 alone."""
    directory = Path(environment[HOST_STATS_VARIABLE])
    directory.mkdir(parents=True)
    _write_recap(directory / "web-1", ok=2, changed=1)
    _write_recap(directory / "web-2", ok=2, changed=0)
    diff.write_text("--- before\n+++ after\n", encoding="utf-8")
    return 0
