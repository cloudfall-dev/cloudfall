"""The ``cloudfall_rabbitmq`` role replacing a lost node and rejoining an empty one.

S-023-14 and S-023-15 (#510). A live run needs three Debian 13 hosts and a
fourth to replace one, so the role's membership decisions run for real by
``ansible-playbook`` with the role's own filters, against the stand-in
``rabbitmq-diagnostics`` and ``rabbitmqctl`` that keep a cluster in a file;
every node of the inventory is localhost, so a task delegated to a peer asks
that same stand-in. What only live hosts prove (a publisher on ``h1`` and
``h3`` throughout, reconciliation growing each quorum queue onto ``h4``, the
new node's first start joining the cluster) is listed in the pull request as
unverified.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from rabbitmq_node import fresh_state, write_ctl
from test_s023_rabbitmq_cluster_role import _play
from test_s023_rabbitmq_role import (
    JOBS,
    ROLE,
    _defaults,
    _service,
    _task,
    _tasks,
    _template,
    _variables,
)

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

requires_ansible = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook is not on PATH"
)
SERVERS = ["h1", "h2", "h3", "h4"]
HOSTVARS = {server: f"{server}.example.internal" for server in SERVERS}
REPLACED = ["h1", "h3", "h4"]


def _load() -> ModuleType:
    path = ROLE / "filter_plugins" / "rabbitmq_membership.py"
    spec = importlib.util.spec_from_file_location("rabbitmq_membership", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MEMBERSHIP = _load()


def _status(members: list[str], running: list[str]) -> dict[str, object]:
    return {
        "disk_nodes": [f"rabbit@{node}" for node in members],
        "ram_nodes": [],
        "running_nodes": [f"rabbit@{node}" for node in running],
        "alarms": [],
        "partitions": {},
    }


def _raw(path: Path) -> list[dict[str, Any]]:
    tasks = yaml.safe_load(path.read_text("utf-8"))
    assert isinstance(tasks, list)
    return tasks


def _log(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text("utf-8").splitlines()]


# proves: S-023-14
def test_s023_14_only_a_member_neither_declared_nor_running_is_forgettable() -> None:
    declared = [f"rabbit@{node}" for node in REPLACED]

    # h2 destroyed and replaced by h4 in nodes
    lost = json.dumps(_status(["h1", "h2", "h3"], ["h1", "h3"]))
    assert MEMBERSHIP.forgettable(lost, declared) == ["rabbit@h2"]
    # h2 merely stopped, still declared: never forgotten
    stopped = json.dumps(_status(["h1", "h2", "h3"], ["h1", "h3"]))
    assert (
        MEMBERSHIP.forgettable(stopped, ["rabbit@h1", "rabbit@h2", "rabbit@h3"]) == []
    )
    # h2 no longer declared but running: never forgotten
    running = json.dumps(_status(["h1", "h2", "h3"], ["h1", "h2", "h3"]))
    assert MEMBERSHIP.forgettable(running, declared) == []
    with pytest.raises(MEMBERSHIP.MembershipReadError):
        MEMBERSHIP.forgettable("Error: unable to perform an operation", declared)


def _membership_play(
    tmp_path: Path, cluster: dict[str, object], nodes: list[str]
) -> tuple[subprocess.CompletedProcess[str], int | None, Path, Path]:
    state = fresh_state()
    state["cluster"] = cluster
    ctl, state_path, log = write_ctl(tmp_path / "node", state)
    done, changed = _play(
        tmp_path,
        _raw(ROLE / "tasks" / "membership.yml"),
        {
            "cloudfall_server_id": "h1",
            "cloudfall_hostname": "h1.example.internal",
            "cloudfall_rabbitmq_service": _service(JOBS, nodes=nodes),
            "cloudfall_rabbitmq_ctl": str(ctl),
            "cloudfall_rabbitmq_diagnostics": str(ctl),
        },
        hostvars=HOSTVARS,
    )
    return done, changed, state_path, log


# proves: S-023-14
@requires_ansible
def test_s023_14_the_home_forgets_the_replaced_server_once(tmp_path: Path) -> None:
    done, changed, state_path, log = _membership_play(
        tmp_path / "first", _status(["h1", "h2", "h3"], ["h1", "h3"]), REPLACED
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert changed == 1
    assert ["forget_cluster_node", "rabbit@h2"] in _log(log)
    cluster = json.loads(state_path.read_text("utf-8"))["cluster"]
    # No membership of h2 is left; h4 joins on its own turn
    assert cluster["disk_nodes"] == ["rabbit@h1", "rabbit@h3"]

    # A second converge finds nothing to forget
    again, changed, _, log = _membership_play(tmp_path / "second", cluster, REPLACED)
    assert again.returncode == 0, again.stdout + again.stderr
    assert changed == 0
    assert not any("forget_cluster_node" in call for call in _log(log))


# proves: S-023-14
@pytest.mark.parametrize(
    ("nodes", "running"),
    [
        # h2 stopped, still declared
        pytest.param(["h1", "h2", "h3"], ["h1", "h3"], id="declared and stopped"),
        # h2 left out of nodes, but the cluster sees it running
        pytest.param(REPLACED, ["h1", "h2", "h3"], id="undeclared and running"),
    ],
)
@requires_ansible
def test_s023_14_a_declared_or_running_node_keeps_its_membership(
    tmp_path: Path, nodes: list[str], running: list[str]
) -> None:
    done, changed, state_path, log = _membership_play(
        tmp_path, _status(["h1", "h2", "h3"], running), nodes
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert changed == 0
    assert not any("forget_cluster_node" in call for call in _log(log))
    cluster = json.loads(state_path.read_text("utf-8"))["cluster"]
    assert "rabbit@h2" in cluster["disk_nodes"]


# proves: S-023-14
def test_s023_14_the_home_forgets_once_it_runs_and_before_the_health_wait() -> None:
    names = [task["name"] for task in _tasks("main.yml")]
    forget = names.index("Remove the membership of a lost server on the service's home")
    assert names.index("Wait until the node runs in the home's cluster") < forget
    assert forget < names.index("Wait until the service is healthy")
    task = _task(
        "Remove the membership of a lost server on the service's home",
        _tasks("main.yml"),
    )
    assert task["when"] == [
        "cloudfall_rabbitmq_clustered | bool",
        "cloudfall_rabbitmq_home | bool",
        "not ansible_check_mode",
    ]


def _settings_play(
    tmp_path: Path, old_nodes: list[str] | None, nodes: list[str], group_size: int
) -> subprocess.CompletedProcess[str]:
    """Run main.yml's cluster settings tasks on a node that runs with ``old_nodes``.

    The handler writes a marker, so a test sees whether the node restarts.
    """
    conf = tmp_path / "rabbitmq"
    (conf / "conf.d").mkdir(parents=True)
    templates = tmp_path / "templates"
    templates.mkdir()
    shutil.copy(ROLE / "templates" / "92-cloudfall-cluster.conf.j2", templates)
    main = _raw(ROLE / "tasks" / "main.yml")
    names = (
        "Read the cluster settings the node runs with",
        "Install the cluster settings",
        "Restart the node for its changed cluster settings",
    )
    tasks = [_task(name, main) for name in names]
    for task in tasks:
        if "ansible.builtin.template" in task:
            task["ansible.builtin.template"].pop("owner")
            task["ansible.builtin.template"].pop("group")
    base: dict[str, object] = {
        **_defaults(),
        "cloudfall_server_id": "h1",
        "cloudfall_hostname": "h1.example.internal",
        "cloudfall_rabbitmq_config_directory": str(conf),
    }
    if old_nodes is not None:
        # What the node runs with: the settings a converge of old_nodes wrote
        old = _settings_render(tmp_path / "old", base, old_nodes, 3)
        (conf / "conf.d" / "92-cloudfall-cluster.conf").write_text(old, "utf-8")
    marker = tmp_path / "restarted"
    play = [
        {
            "name": "Replay the cluster settings of cloudfall_rabbitmq",
            "hosts": "h1",
            "connection": "local",
            "gather_facts": False,
            "vars": {
                **base,
                "cloudfall_rabbitmq_service": _service(JOBS, nodes=nodes),
                "cloudfall_rabbitmq_target_group_size": group_size,
            },
            "tasks": tasks,
            "handlers": [
                {
                    "name": "Restart RabbitMQ",
                    "ansible.builtin.copy": {
                        "content": "restarted",
                        "dest": str(marker),
                        "mode": "0600",
                    },
                }
            ],
        }
    ]
    return _run(tmp_path, play)


def _inventory(tmp_path: Path) -> Path:
    path = tmp_path / "inventory.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "all": {
                    "hosts": {
                        server: {
                            "cloudfall_hostname": hostname,
                            "ansible_connection": "local",
                            "ansible_python_interpreter": sys.executable,
                        }
                        for server, hostname in HOSTVARS.items()
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _run(
    tmp_path: Path, play: list[dict[str, Any]]
) -> subprocess.CompletedProcess[str]:
    playbook = tmp_path / "settings-play.yml"
    playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
    binary = shutil.which("ansible-playbook")
    assert binary is not None
    return subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [binary, "--inventory", str(_inventory(tmp_path)), str(playbook)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "ANSIBLE_FILTER_PLUGINS": str(ROLE / "filter_plugins")},
    )


def _settings_render(
    tmp_path: Path, base: dict[str, object], nodes: list[str], group_size: int
) -> str:
    """Render the cluster settings for ``nodes`` with Ansible itself."""
    tmp_path.mkdir(parents=True)
    out = tmp_path / "92.conf"
    templates = tmp_path / "templates"
    templates.mkdir()
    shutil.copy(ROLE / "templates" / "92-cloudfall-cluster.conf.j2", templates)
    play = [
        {
            "name": "Render the cluster settings",
            "hosts": "h1",
            "connection": "local",
            "gather_facts": False,
            "vars": {
                **base,
                "cloudfall_rabbitmq_service": _service(JOBS, nodes=nodes),
                "cloudfall_rabbitmq_target_group_size": group_size,
            },
            "tasks": [
                {
                    "name": "Render",
                    "ansible.builtin.template": {
                        "src": "92-cloudfall-cluster.conf.j2",
                        "dest": str(out),
                        "mode": "0644",
                    },
                }
            ],
        }
    ]
    done = _run(tmp_path, play)
    assert done.returncode == 0, done.stdout + done.stderr
    return out.read_text("utf-8")


# proves: S-023-14
@requires_ansible
def test_s023_14_replacing_a_node_restarts_no_running_node(tmp_path: Path) -> None:
    done = _settings_play(tmp_path, ["h1", "h2", "h3"], REPLACED, 3)

    assert done.returncode == 0, done.stdout + done.stderr
    # The node list is written, and h1 and h3 keep running: their majority
    # takes publishes throughout
    conf = tmp_path / "rabbitmq" / "conf.d" / "92-cloudfall-cluster.conf"
    assert "cluster_formation.classic_config.nodes.3 = rabbit@h4" in conf.read_text(
        "utf-8"
    )
    assert not (tmp_path / "restarted").exists()


@pytest.mark.parametrize(
    ("old_nodes", "group_size"),
    [
        # A new node: the handler is what starts it
        pytest.param(None, 3, id="a new node"),
        # A setting beyond the node list
        pytest.param(["h1", "h2", "h3"], 5, id="another setting"),
    ],
)
@requires_ansible
def test_another_cluster_setting_still_restarts_the_node(
    tmp_path: Path, old_nodes: list[str] | None, group_size: int
) -> None:
    done = _settings_play(tmp_path, old_nodes, ["h1", "h2", "h3"], group_size)

    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "restarted").exists()


def test_the_cluster_settings_without_the_node_list() -> None:
    settings = "\n".join(  # noqa: FLY002 - one setting per line
        (
            "cluster_formation.peer_discovery_backend = classic_config",
            "cluster_formation.classic_config.nodes.1 = rabbit@h1",
            "cluster_formation.classic_config.nodes.2 = rabbit@h4",
            "quorum_queue.continuous_membership_reconciliation.enabled = true",
        )
    )
    assert MEMBERSHIP.without_node_list(settings + "\n").splitlines() == [
        "cluster_formation.peer_discovery_backend = classic_config",
        "quorum_queue.continuous_membership_reconciliation.enabled = true",
    ]
    names = [task["name"] for task in _tasks("main.yml")]
    # Every other managed file restarts the node as before
    loop = _task("Install the managed settings", _tasks("main.yml"))["loop"]
    assert "reject('equalto', cloudfall_rabbitmq_cluster_file)" in loop
    assert (
        _defaults()["cloudfall_rabbitmq_cluster_file"]
        in (
            _defaults()["cloudfall_rabbitmq_managed_files"]  # type: ignore[operator]
        )
    )
    assert names.index("Install the cluster settings") < names.index(
        "Start the node with its settings"
    )


# proves: S-023-15
def test_s023_15_the_first_peer_that_answers_is_the_cluster() -> None:
    plan = MEMBERSHIP.rejoin_plan
    answer = {"rc": 0, "stdout": json.dumps(_status(["h1", "h2", "h3"], ["h1", "h3"]))}
    assert plan(
        [
            {"item": "h1", "unreachable": True},
            {"item": "h3", **answer},
        ],
        "rabbit@h2",
    ) == {
        "peer": "h3",
        "members": ["rabbit@h1", "rabbit@h2", "rabbit@h3"],
        "forget": True,
    }
    # A peer that does not run answers nothing
    assert (
        plan(
            [{"item": "h1", "rc": 69, "stdout": ""}, {"item": "h3", **answer}],
            "rabbit@h2",
        )["peer"]
        == "h3"
    )
    # A new node, which the cluster does not hold: nothing to forget
    assert plan([{"item": "h1", **answer}], "rabbit@h4")["forget"] is False
    # The cluster sees the node running: never forgotten
    running = {
        "rc": 0,
        "stdout": json.dumps(_status(["h1", "h2", "h3"], ["h1", "h2", "h3"])),
    }
    assert plan([{"item": "h1", **running}], "rabbit@h2")["forget"] is False
    assert plan([{"item": "h1", "rc": 69, "stdout": ""}], "rabbit@h2") == {
        "peer": "",
        "members": [],
        "forget": False,
    }


def _rejoin_play(
    tmp_path: Path,
    server: str,
    cluster: dict[str, object] | None,
    *,
    holds_data: bool,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    state = fresh_state()
    if cluster is not None:
        state["cluster"] = cluster
    ctl, state_path, log = write_ctl(tmp_path / "node", state)
    data = tmp_path / "data" / f"rabbit@{server}"
    if holds_data:
        (data / "coordination").mkdir(parents=True)
        (data / "coordination" / "00000001.wal").write_text("log", "utf-8")
    done, _ = _play(
        tmp_path,
        _raw(ROLE / "tasks" / "rejoin.yml"),
        {
            "cloudfall_server_id": server,
            "cloudfall_hostname": f"{server}.example.internal",
            "cloudfall_rabbitmq_service": _service(JOBS, nodes=["h1", "h2", "h3"]),
            "cloudfall_rabbitmq_ctl": str(ctl),
            "cloudfall_rabbitmq_diagnostics": str(ctl),
            "cloudfall_rabbitmq_node_data_path": str(data),
        },
        hostvars=HOSTVARS,
    )
    return done, state_path, log


# proves: S-023-15
@requires_ansible
def test_s023_15_an_empty_declared_node_is_forgotten_and_joins_again(
    tmp_path: Path,
) -> None:
    done, state_path, log = _rejoin_play(
        tmp_path,
        "h2",
        _status(["h1", "h2", "h3"], ["h1", "h3"]),
        holds_data=False,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert ["forget_cluster_node", "rabbit@h2"] in _log(log)
    cluster = json.loads(state_path.read_text("utf-8"))["cluster"]
    # It joins as a new node when it starts; reconciliation gives it a member
    # of every quorum queue (the health wait checks it)
    assert cluster["disk_nodes"] == ["rabbit@h1", "rabbit@h3"]


# proves: S-023-15
@pytest.mark.parametrize(
    ("server", "cluster", "holds_data"),
    [
        # A node with data rejoins the members it knows, or stops at the join
        # wait: it is never forgotten, and nobody else is asked
        pytest.param(
            "h2", _status(["h1", "h2", "h3"], ["h1", "h3"]), True, id="holds data"
        ),
        # A new node the cluster does not hold
        pytest.param("h3", _status(["h1", "h2"], ["h1", "h2"]), False, id="new"),
        # The cluster sees it running
        pytest.param(
            "h2", _status(["h1", "h2", "h3"], ["h1", "h2", "h3"]), False, id="running"
        ),
        # The home of a new cluster: nobody answers yet, and it starts alone
        pytest.param("h1", None, False, id="the home of a new cluster"),
    ],
)
@requires_ansible
def test_s023_15_nothing_is_removed_otherwise(
    tmp_path: Path, server: str, cluster: dict[str, object] | None, *, holds_data: bool
) -> None:
    done, state_path, log = _rejoin_play(
        tmp_path, server, cluster, holds_data=holds_data
    )

    assert done.returncode == 0, done.stdout + done.stderr
    calls = _log(log)
    assert not any("forget_cluster_node" in call for call in calls)
    if holds_data:
        assert calls == []
    if cluster is not None:
        final = json.loads(state_path.read_text("utf-8"))["cluster"]
        assert final["disk_nodes"] == cluster["disk_nodes"]


# proves: S-023-15
@requires_ansible
def test_s023_15_an_empty_node_nobody_answers_for_is_not_started(
    tmp_path: Path,
) -> None:
    done, _, log = _rejoin_play(tmp_path, "h2", None, holds_data=False)

    assert done.returncode != 0
    assert "it would form a cluster of its own" in done.stdout
    assert not any("forget_cluster_node" in call for call in _log(log))


# proves: S-023-15
def test_s023_15_the_rejoin_comes_before_the_node_starts_and_never_resets() -> None:
    names = [task["name"] for task in _tasks("main.yml")]
    rejoin = names.index("Let a node that came back empty join again")
    assert names.index("Install the pinned RabbitMQ server") < rejoin
    assert rejoin < names.index("Start the node with its settings")
    forget = _task(
        "Remove the membership the node left on its old volume", _tasks("rejoin.yml")
    )
    assert "cloudfall_rabbitmq_rejoin" in forget["delegate_to"]
    assert forget["when"] == [
        "cloudfall_rabbitmq_node_data.matched == 0",
        "cloudfall_rabbitmq_rejoin.forget | bool",
    ]
    assert _template_peers("h2") == ["h1", "h3"]
    assert _template_peers("h1") == ["h2", "h3"]
    # A node that holds data is never emptied
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROLE / "tasks").glob("*.yml")
    )
    for command in ("- reset", "force_reset", "- stop_app"):
        assert command not in text
    assert _defaults()["cloudfall_rabbitmq_node_data_path"] == (
        "{{ cloudfall_rabbitmq_data_path }}/mnesia/{{ cloudfall_rabbitmq_node_name }}"
    )


def _template_peers(server: str) -> object:
    return _template(
        "{{ cloudfall_rabbitmq_peers }}",
        _variables(
            cloudfall_server_id=server,
            cloudfall_rabbitmq_service=_service(JOBS, nodes=["h1", "h2", "h3"]),
        ),
    )
