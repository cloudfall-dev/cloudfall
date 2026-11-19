"""The ``cloudfall_rabbitmq`` role for a cluster of three (S-023, #508).

Three Debian 13 hosts with RabbitMQ are what a live run needs, so the role's
decisions are evaluated with Ansible's own templating, its settings and TLS
options rendered from its templates, and the tasks that read the cluster run
for real on localhost against a stand-in ``rabbitmq-diagnostics`` and
``rabbitmqctl`` that answer from a file. What only three live hosts prove (a
message published through ``h1`` and consumed through ``h3``, a node with
another cookie or CA that does not join, ``h2`` catching up after a stop) is
listed in the pull request as unverified.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml
from rabbitmq_node import fresh_state, write_ctl
from test_s023_rabbitmq_role import (
    FILTERS,
    JOBS,
    ROLE,
    _defaults,
    _holds,
    _render,
    _service,
    _settings,
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
THREE = ["h1", "h2", "h3"]
ADDRESSES = {"h1": "203.0.113.11", "h2": "203.0.113.12", "h3": "203.0.113.13"}
NAMES = [f"rabbit@{node}" for node in THREE]


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


CLUSTER = _load(ROLE / "filter_plugins" / "rabbitmq_cluster.py", "rabbitmq_cluster")
HEALTH = _load(
    ROLE / "library" / "cloudfall_rabbitmq_health.py", "cloudfall_rabbitmq_health"
)


def _rendered_cluster(server: str) -> dict[str, object]:
    """Return what the inventory renders for ``server`` (see the SDK's test)."""
    return {
        "rabbitmq-main": {
            "address": ADDRESSES[server],
            "nodes": [
                {
                    "server": node,
                    "name": f"rabbit@{node}",
                    "hostname": f"{node}.example.internal",
                    "address": ADDRESSES[node],
                }
                for node in THREE
            ],
            "certificatePath": "/etc/rabbitmq/cloudfall/node.crt",
            "keyPath": "/etc/rabbitmq/cloudfall/node.key",
            "caPath": "/etc/rabbitmq/cloudfall/ca.crt",
        }
    }


def _three(server: str = "h1", **extra: object) -> dict[str, object]:
    return _variables(
        **{
            "cloudfall_server_id": server,
            "cloudfall_hostname": f"{server}.example.internal",
            "hostvars": {
                node: {"cloudfall_hostname": f"{node}.example.internal"}
                for node in THREE
            },
            "cloudfall_rabbitmq_service": _service(JOBS, nodes=THREE),
            "cloudfall_rabbitmq_cluster": _rendered_cluster(server),
            "cloudfall_rabbitmq_erl_ssl": {
                "stdout": "/usr/lib/erlang/lib/ssl-11.2/ebin\n"
            },
            **extra,
        }
    )


def _env_block(variables: dict[str, object]) -> str:
    env = _task("Keep the node's name and epmd on loopback", _tasks("main.yml"))
    return str(_template(env["ansible.builtin.blockinfile"]["block"], variables))


# proves: S-023-3
def test_s023_3_one_node_renders_as_it_did() -> None:
    # The settings and the environment of #506's one node, unchanged
    assert _render("90-cloudfall-listeners.conf.j2", _variables()).splitlines() == [
        "# Managed by Cloudfall: replaced on every converge (S-023)",
        "# Clients on loopback only, in plain text",
        "listeners.tcp.default = 127.0.0.1:5672",
        "listeners.ssl = none",
        "# One node: the inter-node port stays on loopback too",
        "distribution.listener.interface = 127.0.0.1",
        "distribution.listener.port_range.min = 25672",
        "distribution.listener.port_range.max = 25672",
    ]
    assert _env_block(_variables()) == (
        "NODENAME=rabbit@h1\nERL_EPMD_ADDRESS=127.0.0.1"
    )
    assert _template("{{ cloudfall_rabbitmq_allowed_listeners }}", _variables()) == []
    names = [task["name"] for task in _tasks("main.yml")]
    for name in (
        "Prepare the node to meet the others over TLS",
        "Refuse a converge while the cluster has no majority",
        "Wait until the node runs in the home's cluster",
        "Wait until the service is healthy",
    ):
        task = _task(name, _tasks("main.yml"))
        assert name in names
        when = task["when"] if isinstance(task["when"], list) else [task["when"]]
        assert "cloudfall_rabbitmq_clustered | bool" in when


# proves: S-023-8
def test_s023_8_each_node_lists_the_three_and_listens_on_its_ipv4() -> None:
    for server in THREE:
        settings = _settings(_three(server))

        assert [
            settings[f"cluster_formation.classic_config.nodes.{index}"]
            for index in (1, 2, 3)
        ] == NAMES
        assert settings["distribution.listener.interface"] == ADDRESSES[server]
        assert settings["distribution.listener.port_range.min"] == "25672"
        # Clients stay on loopback without clientAccess (#507)
        assert settings["listeners.tcp.default"] == "127.0.0.1:5672"
        assert settings["listeners.ssl"] == "none"
        # One copy per node of every quorum queue, grown with no step by a person
        assert (
            settings["quorum_queue.continuous_membership_reconciliation.enabled"]
            == "true"
        )
        assert (
            settings[
                "quorum_queue.continuous_membership_reconciliation.target_group_size"
            ]
            == "3"
        )


# proves: S-023-8
# proves: S-023-19
def test_s023_19_a_node_of_three_takes_clients_and_peers_on_its_ipv4() -> None:
    for server in THREE:
        access = {
            "rabbitmq-main": {
                "address": ADDRESSES[server],
                "port": 5671,
                "certificatePath": "/etc/rabbitmq/cloudfall/node.crt",
                "keyPath": "/etc/rabbitmq/cloudfall/node.key",
                "caPath": "/etc/rabbitmq/cloudfall/ca.crt",
            }
        }
        variables = _three(
            server,
            cloudfall_rabbitmq_service=_service(
                JOBS, nodes=THREE, clientAccess={"allowedSources": ["10.0.0.0/24"]}
            ),
            cloudfall_rabbitmq_client_access=access,
        )

        settings = _settings(variables)

        assert settings["listeners.ssl.default"] == f"{ADDRESSES[server]}:5671"
        assert settings["listeners.tcp.default"] == "127.0.0.1:5672"
        assert settings["distribution.listener.interface"] == ADDRESSES[server]
        # The same certificate serves clients and the other nodes
        options = _render("inter_node_tls.config.j2", variables)
        assert f'{{certfile, "{settings["ssl_options.certfile"]}"}}' in options
        assert _template("{{ cloudfall_rabbitmq_allowed_listeners }}", variables) == [
            f"{ADDRESSES[server]}:4369",
            f"{ADDRESSES[server]}:25672",
            f"{ADDRESSES[server]}:5671",
        ]
        for name in (
            "Require the node's rendered client access",
            "Require the node's rendered cluster",
        ):
            assert _holds(_task(name, _tasks("main.yml")), variables)


# proves: S-023-9
def test_s023_9_the_node_and_the_tools_speak_tls_between_nodes() -> None:
    lines = _env_block(_three("h2")).splitlines()

    args = (
        "-pa /usr/lib/erlang/lib/ssl-11.2/ebin -proto_dist inet_tls "
        "-ssl_dist_optfile /etc/rabbitmq/cloudfall/inter_node_tls.config"
    )
    assert lines == [
        "NODENAME=rabbit@h2",
        "ERL_EPMD_ADDRESS=127.0.0.1,203.0.113.12",
        f'SERVER_ADDITIONAL_ERL_ARGS="{args}"',
        f'CTL_ERL_ARGS="{args}"',
    ]


# proves: S-023-9
def test_s023_9_both_directions_verify_the_project_ca() -> None:
    options = _render("inter_node_tls.config.j2", _three())
    terms = options[options.index("[") :]

    server = terms[terms.index("{server,") : terms.index("{client,")]
    client = terms[terms.index("{client,") :]
    for side in (server, client):
        assert '{cacertfile, "/etc/rabbitmq/cloudfall/ca.crt"}' in side
        assert '{certfile, "/etc/rabbitmq/cloudfall/node.crt"}' in side
        assert '{keyfile, "/etc/rabbitmq/cloudfall/node.key"}' in side
        assert "{verify, verify_peer}" in side
    # A peer without a certificate of the project CA is refused
    assert "{fail_if_no_peer_cert, true}" in server
    assert "verify_none" not in terms
    # The connecting side checks the peer's name: Erlang's default
    # server_name_indication, the host part of the node name, stays in force
    assert "server_name_indication" not in terms
    assert "verify_fun" not in terms
    assert "customize_hostname_check" not in terms
    assert terms.rstrip().endswith("].")


# proves: S-023-9
def test_s023_9_the_key_is_the_nodes_alone_and_the_certificate_required() -> None:
    tls = _tasks("tls.yml")
    key = _task("Let the node alone read its key", tls)
    assert key["ansible.builtin.file"]["owner"] == "rabbitmq"
    assert key["ansible.builtin.file"]["mode"] == "0600"
    require = _task("Require the node certificate before it serves TLS", tls)
    assert require["ansible.builtin.assert"]["that"] == [
        "item.stat.exists",
        "item.stat.isreg",
    ]
    # The certificate is prepared, for a node of three too, before the
    # inter-node options name it
    names = [task["name"] for task in _tasks("main.yml")]
    assert names.index("Prepare the node certificate for TLS") < (
        names.index("Prepare the node to meet the others over TLS")
    )
    assert _template("{{ cloudfall_rabbitmq_needs_certificate }}", _three()) is True
    assert (
        _template("{{ cloudfall_rabbitmq_certificate }}", _three())
        == (_rendered_cluster("h1")["rabbitmq-main"])
    )
    # Only what the services converge staged for this node is copied
    assert _template(
        "{{ cloudfall_rabbitmq_delivered_files }}",
        _three(
            cloudfall_services_rabbitmq_delivered_files={
                "h1": [{"path": "/etc/rabbitmq/cloudfall/node.crt"}],
                "h2": [{"path": "/etc/rabbitmq/cloudfall/node.crt"}],
            }
        ),
    ) == [{"path": "/etc/rabbitmq/cloudfall/node.crt"}]


# proves: S-023-9
def test_s023_9_every_node_resolves_the_others_at_their_ipv4() -> None:
    node = cast("dict[str, Any]", _rendered_cluster("h3")["rabbitmq-main"])
    block = CLUSTER.hosts_lines(node["nodes"])

    assert _defaults()["cloudfall_rabbitmq_hosts_block"] == (
        "{{ cloudfall_rabbitmq_node.nodes | default([]) "
        "| cloudfall_rabbitmq_hosts_lines }}"
    )
    assert str(block).splitlines() == [
        "203.0.113.11 h1.example.internal h1",
        "203.0.113.12 h2.example.internal h2",
        "203.0.113.13 h3.example.internal h3",
    ]
    hosts = _task("Resolve every node's hostname to its address", _tasks("cluster.yml"))
    # Before Debian's 127.0.1.1 line, so the node's own name is its IPv4 too
    assert hosts["ansible.builtin.blockinfile"]["insertbefore"] == "BOF"


# proves: S-023-9
def test_s023_9_only_discovery_and_the_inter_node_port_leave_loopback() -> None:
    allowed = _template("{{ cloudfall_rabbitmq_allowed_listeners }}", _three("h2"))
    assert allowed == ["203.0.113.12:4369", "203.0.113.12:25672"]
    sockets = "\n".join(  # noqa: FLY002 - one socket per line
        (
            'LISTEN 0 128 127.0.0.1:5672 0.0.0.0:* users:(("beam.smp",pid=9,fd=1))',
            'LISTEN 0 128 203.0.113.12:25672 0.0.0.0:* users:(("beam.smp",pid=9,fd=2))',
            'LISTEN 0 128 127.0.0.1:4369 0.0.0.0:* users:(("epmd",pid=8,fd=3))',
            'LISTEN 0 128 203.0.113.12:4369 0.0.0.0:* users:(("epmd",pid=8,fd=4))',
        )
    )
    assert FILTERS.public_listeners(sockets, allowed) == []
    # Clients on the IPv4 are not allowed without clientAccess
    exposed = (
        sockets
        + '\nLISTEN 0 128 203.0.113.12:5672 0.0.0.0:* users:(("beam.smp",pid=9,fd=5))'
    )
    assert FILTERS.public_listeners(exposed, allowed) == ["203.0.113.12:5672 beam.smp"]
    assert FILTERS.public_listeners(sockets) == [
        "203.0.113.12:25672 beam.smp",
        "203.0.113.12:4369 epmd",
    ]


def _status(members: list[str], running: list[str], **extra: object) -> str:
    return json.dumps(
        {"disk_nodes": members, "ram_nodes": [], "running_nodes": running, **extra}
    )


# proves: S-023-11
def test_s023_11_a_majority_is_more_than_half_of_the_members() -> None:
    assert CLUSTER.membership(_status(NAMES, NAMES))["majority"] is True
    assert CLUSTER.membership(_status(NAMES, NAMES[:2]))["majority"] is True
    lost = CLUSTER.membership(_status(NAMES, NAMES[:1]))
    assert lost == {"members": NAMES, "running": ["rabbit@h1"], "majority": False}
    # The first node of three, before the others joined, is its own majority
    assert CLUSTER.membership(_status(NAMES[:1], NAMES[:1]))["majority"] is True
    with pytest.raises(CLUSTER.ClusterStatusError):
        CLUSTER.membership("Error: unable to perform an operation")


# proves: S-023-11
def test_s023_11_the_majority_check_comes_before_any_change() -> None:
    names = [task["name"] for task in _tasks("main.yml")]
    check = names.index("Refuse a converge while the cluster has no majority")
    # Nothing before it changes the host: the role's refusals and reads alone
    assert check < names.index(
        "Install the RabbitMQ repository and the Erlang of the Debian release"
    )
    majority = _tasks("majority.yml")
    read = _task("Read the cluster this node is a member of", majority)
    assert read["changed_when"] is False
    assert read["check_mode"] is False
    refuse = _task("Refuse a converge while the cluster has no majority", majority)
    assert refuse["ansible.builtin.fail"]["msg"].startswith(
        "PRECONDITION {{ cloudfall_rabbitmq_no_majority_code }}"
    )
    assert _defaults()["cloudfall_rabbitmq_no_majority_code"] == (
        "service_rabbitmq_no_majority"
    )


# proves: S-023-8
@pytest.mark.parametrize(
    ("server", "members", "joined"),
    [
        pytest.param("h2", NAMES[:2], True, id="h2 joined the home"),
        # A node that started a cluster of its own never lists the home
        pytest.param("h2", ["rabbit@h2"], False, id="h2 alone"),
        # The home is in its own cluster from its first start
        pytest.param("h1", ["rabbit@h1"], True, id="the home"),
    ],
)
@requires_ansible
def test_s023_8_a_node_waits_until_it_runs_in_the_homes_cluster(
    tmp_path: Path, server: str, members: list[str], *, joined: bool
) -> None:
    wait = _task("Wait until the node runs in the home's cluster", _tasks("main.yml"))

    done, _ = _play(
        tmp_path,
        [
            {
                "name": "Evaluate the join",
                "ansible.builtin.assert": {"that": [str(wait["until"])]},
            }
        ],
        {
            "cloudfall_server_id": server,
            "cloudfall_hostname": f"{server}.example.internal",
            "cloudfall_rabbitmq_service": _service(JOBS, nodes=THREE),
            "cloudfall_rabbitmq_joined": {
                "rc": 0,
                "stdout": _status(members, members),
            },
        },
        hostvars={node: f"{node}.example.internal" for node in THREE},
    )

    assert (done.returncode == 0) is joined, done.stdout + done.stderr


# proves: S-023-8
# proves: S-023-10
def test_s023_10_healthy_means_every_quorum_queue_on_every_node() -> None:
    status = json.loads(_status(NAMES, NAMES, alarms=[], partitions={}))
    queue = {
        "name": "orders",
        "type": "quorum",
        "leader": "rabbit@h1",
        "online": NAMES,
    }
    classic = {"name": "reply", "type": "classic", "leader": "", "online": []}
    assert HEALTH.problems(status, {"jobs": [queue, classic]}, NAMES) == []
    # A queue the first node held before the others joined has one member
    # until reconciliation grows it to three, with no step by a person
    grown = {**queue, "online": ["rabbit@h1"]}
    assert HEALTH.problems(status, {"jobs": [grown]}, NAMES) == [
        "quorum queue jobs/orders has 1 of 3 members online"
    ]
    stopped = json.loads(
        _status(
            NAMES,
            NAMES[:2],
            alarms=[{"node": "rabbit@h1", "resource": "disk"}],
            partitions={"rabbit@h1": ["rabbit@h2"]},
        )
    )
    assert HEALTH.problems(stopped, {"jobs": [{**queue, "leader": ""}]}, NAMES) == [
        "rabbit@h3 is not running",
        "alarm: node rabbit@h1, resource disk",
        "partition reported by rabbit@h1: rabbit@h2",
        "quorum queue jobs/orders has no leader",
    ]
    assert HEALTH.problems(json.loads(_status(NAMES[:2], NAMES[:2])), {}, NAMES) == [
        "rabbit@h3 is not a member of the cluster"
    ]
    health = _task("Wait until the service is healthy", _tasks("main.yml"))
    assert _defaults()["cloudfall_rabbitmq_healthy_timeout"] == 3600
    assert health["cloudfall_rabbitmq_health"]["nodes"] == (
        "{{ cloudfall_rabbitmq_node_names }}"
    )


# proves: S-023-10
def test_s023_10_growing_redeclares_no_queue() -> None:
    # The role never declares, deletes or moves a queue: the queues the first
    # node held stay, and reconciliation adds their members on the new nodes
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROLE / "tasks").glob("*.yml")
    )
    for command in (
        "declare_queue",
        "delete_queue",
        "purge_queue",
        # The tool a person grows or shrinks a quorum queue with
        "rabbitmq-queues",
        "add_member",
    ):
        assert command not in text
    # The node that was alone becomes the home of three: its settings name
    # the others, and only it restarts once for them
    one = _settings(_variables())
    three = _settings(_three("h1"))
    assert one["cluster_formation.classic_config.nodes.1"] == "rabbit@h1"
    assert "cluster_formation.classic_config.nodes.2" not in one
    assert three["cluster_formation.classic_config.nodes.3"] == "rabbit@h3"


# proves: S-023-10
def test_s023_10_every_cluster_read_changes_nothing() -> None:
    reads = [
        _task("Read the cluster this node is a member of", _tasks("majority.yml")),
        _task("Read where Erlang keeps its TLS distribution", _tasks("cluster.yml")),
        _task("Wait until the node runs in the home's cluster", _tasks("main.yml")),
    ]
    assert all(task["changed_when"] is False for task in reads)
    # The rendered files are the same on every converge
    for name in ("90-cloudfall-listeners.conf.j2", "inter_node_tls.config.j2"):
        assert _render(name, _three()) == _render(name, _three())


def _play(
    tmp_path: Path,
    tasks: list[dict[str, Any]],
    variables: dict[str, object],
    hostvars: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], int | None]:
    """Run ``tasks`` locally; with ``hostvars``, as that host of an inventory.

    ``hostvars`` names each declared node's hostname, as the rendered
    inventory gives ``cloudfall_hostname``.
    """
    host = "localhost"
    inventory = "localhost,"
    if hostvars is not None:
        host = str(variables["cloudfall_server_id"])
        path = tmp_path / "inventory.yml"
        path.write_text(
            yaml.safe_dump(
                {
                    "all": {
                        "hosts": {
                            node: {
                                "cloudfall_hostname": hostname,
                                "ansible_connection": "local",
                                "ansible_python_interpreter": sys.executable,
                            }
                            for node, hostname in hostvars.items()
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        inventory = str(path)
    play = [
        {
            "name": "Replay cluster tasks of cloudfall_rabbitmq",
            "hosts": host,
            "connection": "local",
            "gather_facts": False,
            "vars": {**_defaults(), "cloudfall_server_id": "h1", **variables},
            "tasks": tasks,
        }
    ]
    playbook = tmp_path / "cluster-play.yml"
    playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
    binary = shutil.which("ansible-playbook")
    assert binary is not None
    done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [binary, "--inventory", inventory, str(playbook)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
        env={
            **os.environ,
            "ANSIBLE_FILTER_PLUGINS": str(ROLE / "filter_plugins"),
            "ANSIBLE_LIBRARY": str(ROLE / "library"),
        },
    )
    recap = re.search(rf"{host}\s+:\s+ok=\d+\s+changed=(\d+)", done.stdout)
    return done, None if recap is None else int(recap[1])


def _node(tmp_path: Path, cluster: dict[str, object] | None) -> Path:
    state = fresh_state()
    if cluster is not None:
        state["cluster"] = cluster
    ctl, _, _ = write_ctl(tmp_path / "node", state)
    return ctl


# proves: S-023-11
@requires_ansible
def test_s023_11_no_majority_stops_the_converge_with_precondition(
    tmp_path: Path,
) -> None:
    ctl = _node(
        tmp_path,
        {"disk_nodes": NAMES, "ram_nodes": [], "running_nodes": ["rabbit@h1"]},
    )
    reports = tmp_path / "reports"
    reports.mkdir()
    majority = yaml.safe_load((ROLE / "tasks" / "majority.yml").read_text("utf-8"))

    done, _ = _play(
        tmp_path,
        [
            *majority,
            # Never reached: the converge stopped before any change
            {"name": "Change the node", "ansible.builtin.meta": "noop"},
        ],
        {
            "cloudfall_rabbitmq_service": _service(JOBS, nodes=THREE),
            "cloudfall_rabbitmq_diagnostics": str(ctl),
            "cloudfall_rabbitmq_report_directory": str(reports),
        },
    )

    assert done.returncode != 0
    assert "PRECONDITION service_rabbitmq_no_majority" in done.stdout
    assert "Change the node" not in done.stdout
    report = json.loads((reports / "rabbitmq-main.json").read_text("utf-8"))
    assert report["outcome"] == "refused"
    assert report["code"] == "service_rabbitmq_no_majority"
    assert "only rabbit@h1 run" in report["message"]
    assert "no node was restarted" in report["message"]


# proves: S-023-11
@requires_ansible
def test_s023_11_with_one_node_of_three_down_the_converge_goes_on(
    tmp_path: Path,
) -> None:
    ctl = _node(
        tmp_path,
        {"disk_nodes": NAMES, "ram_nodes": [], "running_nodes": NAMES[:2]},
    )
    reports = tmp_path / "reports"
    reports.mkdir()
    majority = yaml.safe_load((ROLE / "tasks" / "majority.yml").read_text("utf-8"))

    done, changed = _play(
        tmp_path,
        majority,
        {
            "cloudfall_rabbitmq_service": _service(JOBS, nodes=THREE),
            "cloudfall_rabbitmq_diagnostics": str(ctl),
            "cloudfall_rabbitmq_report_directory": str(reports),
        },
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert changed == 0
    assert list(reports.iterdir()) == []


def _health_play(
    tmp_path: Path, state: dict[str, object], timeout: int
) -> tuple[subprocess.CompletedProcess[str], int | None]:
    ctl, _, _ = write_ctl(tmp_path / "node", state)
    return _play(
        tmp_path,
        [
            {
                "name": "Wait until the service is healthy",
                "cloudfall_rabbitmq_health": {
                    "ctl": str(ctl),
                    "diagnostics": str(ctl),
                    "nodes": NAMES,
                    "timeout": timeout,
                    "interval": 1,
                },
            }
        ],
        {},
    )


def _cluster_state(online: list[str]) -> dict[str, object]:
    state = fresh_state()
    state["vhosts"] = [
        {"name": "jobs", "metadata": {"tags": [], "default_queue_type": "quorum"}}
    ]
    state["cluster"] = {
        "disk_nodes": NAMES,
        "ram_nodes": [],
        "running_nodes": NAMES,
        "alarms": [],
        "partitions": {},
    }
    state["queueDetails"] = {
        "jobs": [
            {
                "name": "orders",
                "type": "quorum",
                "leader": "rabbit@h1",
                "online": online,
            }
        ]
    }
    return state


# proves: S-023-10
@requires_ansible
def test_s023_10_a_healthy_cluster_waits_for_nothing_and_changes_nothing(
    tmp_path: Path,
) -> None:
    done, changed = _health_play(tmp_path, _cluster_state(NAMES), 30)

    assert done.returncode == 0, done.stdout + done.stderr
    assert changed == 0


# proves: S-023-8
@requires_ansible
def test_s023_8_an_unhealthy_cluster_stops_with_what_is_missing(
    tmp_path: Path,
) -> None:
    done, _ = _health_play(tmp_path, _cluster_state(["rabbit@h1"]), 0)

    assert done.returncode != 0
    assert "quorum queue jobs/orders has 1 of 3 members online" in done.stdout
    assert "A later converge resumes the wait" in done.stdout


def test_the_cluster_variables_of_a_node_hold_the_rendered_cluster() -> None:
    assert (
        cast(
            "dict[str, object]",
            _template("{{ cloudfall_rabbitmq_node }}", _three("h2")),
        )["address"]
        == "203.0.113.12"
    )
    require = _task("Require the node's rendered cluster", _tasks("main.yml"))
    assert _holds(require, _three())
    assert _holds(require, _variables())
    assert not _holds(require, _three(cloudfall_rabbitmq_cluster={}))
