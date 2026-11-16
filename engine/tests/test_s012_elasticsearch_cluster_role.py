"""The Elasticsearch role grows a cluster and rolls a patch one node at a time.

S-012, #312. Both need live hosts for the whole proof; here the rendered
inventory of a two-node service is real, each node's configuration and the
firewall ruleset are rendered from their templates, the role's decisions are
evaluated with Ansible's own templating, and the rollout order is read from
Ansible's inventory manager.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.ansible_data import as_ansible_data
from cloudfall.cli import main
from cloudfall.domain import ResourceId
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import EngineContext, converge_services
from cloudfall.pki import load_or_create_ca
from cloudfall.secret_store import SecretStore
from cloudfall.secrets import SopsSecretProvider
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import (
    _host_firewall,
    render_ansible_inventory,
)
from cloudfall_engine.cli import _rendered_elasticsearch_services
from elasticsearch_hosts import FakeNodes
from elasticsearch_project import (
    SCHEMAS,
    SNAPSHOT_REPOSITORY,
    elasticsearch_service,
    write_project,
)

if TYPE_CHECKING:
    from cloudfall.lifecycle import ExecutionStep

ROOT = Path(__file__).parents[2]
ROLES = ROOT / "engine" / "ansible" / "roles"
ROLE = ROLES / "cloudfall_elasticsearch"
SERVICES = ROOT / "engine" / "ansible" / "playbooks" / "services.yml"
FIREWALL = ROLES / "cloudfall_firewall" / "templates" / "nftables.conf.j2"


def _rendered_project(tmp_path: Path, *, server: str = "h1") -> Path:
    """Write the project of S-012-5: the service grown to h1 and h2."""
    return write_project(
        tmp_path / "project",
        elasticsearch_service(
            server=server, nodes=["h1", "h2"], snapshotRepository=SNAPSHOT_REPOSITORY
        ),
        servers=("h1", "h2"),
    )


def _rendered(tmp_path: Path, *, server: str = "h1") -> dict[str, Any]:
    """Render the inventory of S-012-5: the service grown to h1 and h2."""
    project = _rendered_project(tmp_path, server=server)
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    return cast("dict[str, Any]", render_ansible_inventory(inventory))


def _inventory_file(rendered: dict[str, Any], path: Path) -> Path:
    """Write the inventory as ``cloudfall-engine inventory render`` does."""
    path.write_text(json.dumps(as_ansible_data(rendered)), encoding="utf-8")
    return path


def _hosts(rendered: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return cast(
        "dict[str, dict[str, Any]]",
        rendered["all"]["children"]["cloudfall_servers"]["hosts"],
    )


def _load(name: str, directory: str = "tasks") -> list[dict[str, Any]]:
    tasks = DataLoader().load_from_file(str(ROLE / directory / name))
    assert isinstance(tasks, list)
    return tasks


def _task(tasks: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(task for task in tasks if task["name"] == name)


def _trusted(values: dict[str, Any]) -> dict[str, Any]:
    return {
        name: trust_as_template(value) if isinstance(value, str) else value
        for name, value in values.items()
    }


def _variables(host: dict[str, Any], **extra: object) -> dict[str, Any]:
    """Return the role's variables on one node, as services.yml passes them."""
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    (service,) = [
        item
        for item in host["cloudfall_services"]
        if item["serviceKind"] == "elasticsearch"
    ]
    return {
        **_trusted(defaults),
        **host,
        "ansible_check_mode": False,
        "cloudfall_elasticsearch_service": service,
        "cloudfall_elasticsearch_account_files": {
            "cloudfallPassword": "/tmp/staged/cloudfall.password",  # noqa: S108
        },
        **extra,
    }


def _templar(variables: dict[str, Any]) -> Templar:
    return Templar(loader=DataLoader(), variables=variables)


def _value(expression: str, variables: dict[str, Any]) -> object:
    return _templar(variables).template(trust_as_template(expression))


def _true(condition: object, variables: dict[str, Any]) -> bool:
    conditions = condition if isinstance(condition, list) else [condition]
    return all(
        _templar(variables).evaluate_conditional(trust_as_template(str(item)))
        for item in conditions
    )


def _holds(task: dict[str, Any], variables: dict[str, Any]) -> bool:
    scoped = {**variables, **_trusted(task.get("vars", {}))}
    return _true(task["ansible.builtin.assert"]["that"], scoped)


def _configuration(variables: dict[str, Any]) -> dict[str, object]:
    """Render elasticsearch.yml the way the role's install task does."""
    install = _task(
        _load("main.yml"), "Install the managed Elasticsearch configuration"
    )
    bootstrap = _value(install["vars"]["cloudfall_elasticsearch_bootstrap"], variables)
    text = (ROLE / "templates" / "elasticsearch.yml.j2").read_text(encoding="utf-8")
    rendered = _templar(
        {**variables, "cloudfall_elasticsearch_bootstrap": bootstrap}
    ).template(trust_as_template(text))
    return cast("dict[str, object]", yaml.safe_load(str(rendered)))


def _formed(*, exists: bool) -> dict[str, object]:
    return {"cloudfall_elasticsearch_cluster_formed": {"stat": {"exists": exists}}}


# proves: S-012-5
def test_s012_5_the_joining_node_is_configured_to_join_and_never_bootstraps(
    tmp_path: Path,
) -> None:
    hosts = _hosts(_rendered(tmp_path))
    joining = _configuration(_variables(hosts["h2"], **_formed(exists=False)))
    home = _configuration(_variables(hosts["h1"], **_formed(exists=True)))

    tls = {
        "xpack.security.transport.ssl.enabled": True,
        "xpack.security.transport.ssl.verification_mode": "full",
        "xpack.security.transport.ssl.client_authentication": "required",
        "xpack.security.transport.ssl.key": "/etc/elasticsearch/certs/transport.key",
        "xpack.security.transport.ssl.certificate": (
            "/etc/elasticsearch/certs/transport.crt"
        ),
        "xpack.security.transport.ssl.certificate_authorities": [
            "/etc/elasticsearch/certs/ca.crt"
        ],
    }
    seeds = ["203.0.113.11:9300", "203.0.113.12:9300"]
    for node, rendered, address in (
        ("h2", joining, "203.0.113.12"),
        ("h1", home, "203.0.113.11"),
    ):
        assert rendered["cluster.name"] == "search-main"
        assert rendered["node.name"] == node
        # HTTP stays on loopback; only the transport leaves it.
        assert rendered["network.host"] == "127.0.0.1"
        assert rendered["transport.host"] == address
        assert rendered["transport.port"] == 9300
        assert rendered["discovery.seed_hosts"] == seeds
        assert rendered.items() >= tls.items()
        assert "discovery.type" not in rendered
        # S-012-3's rule for every node: a joining node never bootstraps.
        assert "cluster.initial_master_nodes" not in rendered


def test_a_fresh_cluster_is_bootstrapped_by_its_home_alone(tmp_path: Path) -> None:
    hosts = _hosts(_rendered(tmp_path))

    home = _configuration(_variables(hosts["h1"], **_formed(exists=False)))
    other = _configuration(_variables(hosts["h2"], **_formed(exists=False)))

    assert home["cluster.initial_master_nodes"] == ["h1"]
    assert "cluster.initial_master_nodes" not in other


def test_a_home_that_finds_a_running_node_refuses_to_bootstrap(tmp_path: Path) -> None:
    hosts = _hosts(_rendered(tmp_path))
    tasks = _load("transport.yml")
    look = _task(
        tasks, "Look for a running node before the home bootstraps the cluster"
    )
    refuse = _task(tasks, "Refuse to bootstrap a second cluster beside a running node")
    fresh = _variables(hosts["h1"], **_formed(exists=False))

    assert _value(look["loop"], fresh) == ["203.0.113.12:9300"]
    assert _true(look["when"], fresh)
    assert not _true(look["when"], _variables(hosts["h1"], **_formed(exists=True)))
    assert not _true(look["when"], _variables(hosts["h2"], **_formed(exists=False)))

    def answered(*results: dict[str, object]) -> bool:
        return _holds(
            refuse, {**fresh, "cloudfall_elasticsearch_peers": {"results": results}}
        )

    timeout = {"failed": False, "msg": "Timeout when waiting for 203.0.113.12:9300"}
    assert answered(timeout)
    assert answered({"skipped": True})
    assert not answered({"failed": False, "state": "started", "port": 9300})


# proves: S-012-5
def test_s012_5_the_second_node_admits_9300_from_the_first_node_only(
    tmp_path: Path,
) -> None:
    hosts = _hosts(_rendered(tmp_path))

    for node, other in (("h2", "11"), ("h1", "12")):
        ruleset = hosts[node]["cloudfall_firewall"]
        transport = [rule for rule in ruleset["allowedInbound"] if rule["port"] == 9300]
        assert transport == [
            {
                "port": 9300,
                "protocol": "tcp",
                "sources": [f"203.0.113.{other}/32", f"2001:db8:{other}::/64"],
                "description": "search-main transport",
            }
        ]
        text = FIREWALL.read_text(encoding="utf-8")
        rendered = str(
            _templar({"cloudfall_firewall_ruleset": ruleset}).template(
                trust_as_template(text)
            )
        )
        lines = [line.strip() for line in rendered.splitlines() if "dport 9300" in line]
        assert lines == [
            (
                f'ip saddr 203.0.113.{other}/32 tcp dport 9300 accept comment '
                '"search-main transport"'
            ),
            (
                f'ip6 saddr 2001:db8:{other}::/64 tcp dport 9300 accept comment '
                '"search-main transport"'
            ),
        ]


def test_each_node_carries_its_transport_and_the_engine_reads_the_service_once(
    tmp_path: Path,
) -> None:
    rendered = _rendered(tmp_path)
    hosts = _hosts(rendered)

    for node, address in (("h1", "203.0.113.11"), ("h2", "203.0.113.12")):
        transport = hosts[node]["cloudfall_elasticsearch_transport"]["search-main"]
        assert transport["address"] == address
        assert transport["seedHosts"] == ["203.0.113.11:9300", "203.0.113.12:9300"]
    inventory = _inventory_file(rendered, tmp_path / "ansible-inventory.json")
    services = _rendered_elasticsearch_services(inventory)
    assert [(item.value, environment.value) for item, environment in services] == [
        ("search-main", "production")
    ]


# proves: S-012-5
def test_s012_5_the_home_node_converges_before_the_node_that_joins_it(
    tmp_path: Path,
) -> None:
    """Declared h1, h2 with h2 as home: services.yml must run h2 first.

    A play keeps its hosts in inventory order (``order`` unset) and ``serial:
    1`` takes them one by one in that order; ``--list-hosts`` prints a set,
    so the order is read from Ansible's inventory manager the play uses.
    """
    rendered = _rendered(tmp_path, server="h2")
    inventory = _inventory_file(rendered, tmp_path / "ansible-inventory.json")
    (play,) = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))

    manager = InventoryManager(loader=DataLoader(), sources=[str(inventory)])

    assert play["serial"] == 1
    assert "order" not in play
    hosts = manager.get_hosts(play["hosts"], order="inventory")
    assert [host.name for host in hosts] == ["h2", "h1"]
    declared = _rendered(tmp_path / "declared")
    assert list(_hosts(declared)) == ["h1", "h2"]


def test_the_role_converges_every_node_of_the_service(tmp_path: Path) -> None:
    hosts = _hosts(_rendered(tmp_path))
    validate = _task(
        _load("main.yml"), "Validate Elasticsearch service target and declaration"
    )
    facts = {
        "ansible_facts": {
            "distribution": "Debian",
            "distribution_major_version": "13",
            "service_mgr": "systemd",
        }
    }

    for node in ("h1", "h2"):
        assert _holds(validate, _variables(hosts[node], **facts))
    stranger = _variables(hosts["h2"], **facts, cloudfall_server_id="h3")
    assert not _holds(validate, stranger)


def test_remote_access_is_refused_on_a_cluster_of_two_or_more(tmp_path: Path) -> None:
    hosts = _hosts(_rendered(tmp_path))
    refuse = _task(
        _load("main.yml"), "Refuse remote access on a cluster of two or more"
    )
    remote = {"listenAddress": "203.0.113.11", "allowedSources": ["10.0.0.0/24"]}

    def holds(nodes: list[str], **elasticsearch: object) -> bool:
        variables = _variables(hosts["h1"])
        service = dict(variables["cloudfall_elasticsearch_service"])
        service["elasticsearch"] = {
            **service["elasticsearch"],
            "nodes": nodes,
            **elasticsearch,
        }
        return _holds(refuse, {**variables, "cloudfall_elasticsearch_service": service})

    assert holds(["h1", "h2"])
    assert holds(["h1"], remoteAccess=remote)
    assert not holds(["h1", "h2"], remoteAccess=remote)


def test_the_transport_is_opened_and_its_certificate_in_place_before_the_config(
) -> None:
    names = [task["name"] for task in _load("main.yml")]
    transport = [task["name"] for task in _load("transport.yml")]

    assert names.index("Read whether the cluster has formed") < names.index(
        "Open the transport to the other nodes, with its certificate"
    )
    assert names.index(
        "Open the transport to the other nodes, with its certificate"
    ) < names.index("Install the managed Elasticsearch configuration")
    assert transport.index(
        "Refuse to bootstrap a second cluster beside a running node"
    ) < transport.index("Enforce the declared firewall with the transport rule")
    # One certificate serves the transport and remoteAccess's HTTP: it is
    # installed before either checks it.
    install = names.index("Install the node certificate and the project CA")
    assert install < names.index("Prepare the node certificate for HTTP over TLS")
    assert install < names.index(
        "Open the transport to the other nodes, with its certificate"
    )
    key = _task(_load("transport.yml"), "Let the node read its transport key")
    assert key["ansible.builtin.file"]["group"] == "elasticsearch"
    assert key["ansible.builtin.file"]["mode"] == "0640"
    assert "notify" not in key


def _health(status: str, nodes: int) -> dict[str, object]:
    return {
        "cloudfall_elasticsearch_health_before": {
            "json": {"status": status, "number_of_nodes": nodes}
        }
    }


# proves: S-012-6
def test_s012_6_a_node_stops_only_from_a_cluster_that_stays_out_of_red(
    tmp_path: Path,
) -> None:
    hosts = _hosts(_rendered(tmp_path))
    (block,) = [task for task in _load("node-stop.yml") if "block" in task]
    steps = block["block"]
    refuse = _task(steps, "Refuse to stop a node of a cluster that is not healthy")
    keep = _task(steps, "Keep replicas where they are while the node is away")

    def allowed(status: str, nodes: int) -> bool:
        return _holds(refuse, _variables(hosts["h1"], **_health(status, nodes)))

    assert allowed("green", 2)
    assert allowed("green", 1)
    assert allowed("yellow", 1)
    assert not allowed("yellow", 2)
    assert not allowed("red", 2)
    assert not allowed("red", 1)
    assert [step["name"] for step in steps] == [
        "Read the cluster's health before the node stops",
        "Refuse to stop a node of a cluster that is not healthy",
        "Keep replicas where they are while the node is away",
        "Flush the node's indices so it recovers from disk",
        "Stop Elasticsearch",
        "Remember what the cluster had before the node stopped",
    ]
    assert keep["ansible.builtin.uri"]["body"] == {
        "persistent": {"cluster.routing.allocation.enable": "primaries"}
    }
    assert _true(keep["when"], _variables(hosts["h1"], **_health("green", 2)))
    assert not _true(keep["when"], _variables(hosts["h1"], **_health("green", 1)))
    # A node that is not running, or that this run already stopped, is left alone.
    running = {"cloudfall_elasticsearch_active": {"rc": 0}}
    assert _true(block["when"], _variables(hosts["h1"], **running))
    assert not _true(
        block["when"],
        _variables(hosts["h1"], cloudfall_elasticsearch_active={"rc": 3}),
    )
    assert not _true(
        block["when"],
        _variables(
            hosts["h1"],
            **running,
            cloudfall_elasticsearch_rolling={"status": "green", "nodes": "2"},
        ),
    )


# proves: S-012-6
def test_s012_6_a_restarted_node_rejoins_on_the_new_version_before_the_next(
    tmp_path: Path,
) -> None:
    hosts = _hosts(_rendered(tmp_path))
    tasks = _load("node-start.yml")
    guarded, back = tasks
    start, rejoining = guarded["block"]
    steps = [*rejoining["block"], *guarded["always"], *back["block"]]
    rolling = {"cloudfall_elasticsearch_rolling": {"status": "green", "nodes": "2"}}
    variables = _variables(hosts["h2"], **rolling)

    assert start["ansible.builtin.systemd_service"]["state"] == "restarted"
    assert "when" not in start
    assert [step["name"] for step in steps] == [
        "Wait for the node to rejoin its cluster",
        "Read the version the node reports",
        "Refuse a node that came back on another version",
        "Let replicas move again",
        "Wait for the cluster to return to the status it had",
        "Forget the restart this node finished",
    ]
    rejoin = _task(steps, "Wait for the node to rejoin its cluster")
    assert str(_value(rejoin["ansible.builtin.uri"]["url"], variables)).endswith(
        "/_cluster/health?wait_for_nodes=%3E%3D2&timeout=10s"
    )
    recovered = _task(steps, "Wait for the cluster to return to the status it had")
    assert str(_value(recovered["ansible.builtin.uri"]["url"], variables)).endswith(
        "/_cluster/health?wait_for_status=green&timeout=10s"
    )
    allocation = _task(steps, "Let replicas move again")
    assert allocation["ansible.builtin.uri"]["body"] == {
        "persistent": {"cluster.routing.allocation.enable": None}
    }
    version = _task(steps, "Refuse a node that came back on another version")

    def reports(answer: str) -> bool:
        reply = {"json": {"nodes": {"Qx9": {"version": answer}}}}
        return _holds(
            version, {**variables, "cloudfall_elasticsearch_node_version": reply}
        )

    assert reports("9.2.4")
    assert not reports("9.2.3")


def _names(tasks: list[dict[str, Any]]) -> list[str]:
    """Return every task name, the tasks of nested blocks included."""
    found: list[str] = []
    for task in tasks:
        found.append(str(task.get("name")))
        for section in ("block", "rescue", "always"):
            found.extend(_names(task.get(section, [])))
    return found


# proves: S-012-6
def test_s012_6_replicas_move_again_even_when_the_node_does_not_come_back(
    tmp_path: Path,
) -> None:
    """A failed restart, rejoin or version check must not leave allocation held."""
    hosts = _hosts(_rendered(tmp_path))
    tasks = _load("node-start.yml")
    (guarded,) = [task for task in tasks if "always" in task]
    rolling = {"cloudfall_elasticsearch_rolling": {"status": "green", "nodes": "2"}}

    assert {
        "Start Elasticsearch on its new package or configuration",
        "Wait for the node to rejoin its cluster",
        "Refuse a node that came back on another version",
    } <= set(_names(guarded["block"]))
    (allocation,) = guarded["always"]
    assert allocation["name"] == "Let replicas move again"
    assert _true(allocation["when"], _variables(hosts["h2"], **rolling))
    one = {"cloudfall_elasticsearch_rolling": {"status": "yellow", "nodes": "1"}}
    assert not _true(allocation["when"], _variables(hosts["h2"], **one))
    assert not _true(allocation["when"], _variables(hosts["h2"]))


# proves: S-012-6
def test_s012_6_a_patch_stops_the_node_before_apt_and_a_major_is_refused(
    tmp_path: Path,
) -> None:
    hosts = _hosts(_rendered(tmp_path))
    tasks = _load("main.yml")
    names = [task["name"] for task in tasks]
    stop = _task(tasks, "Take the node out of its cluster before its package changes")
    refuse = _task(tasks, "Refuse a version the installed node can't move to")

    def installed(stdout: str, version: str = "9.2.5", rc: int = 0) -> dict[str, Any]:
        variables = _variables(
            hosts["h1"],
            cloudfall_elasticsearch_installed={"rc": rc, "stdout": stdout},
        )
        service = dict(variables["cloudfall_elasticsearch_service"])
        service["elasticsearch"] = {**service["elasticsearch"], "version": version}
        return {**variables, "cloudfall_elasticsearch_service": service}

    assert stop["ansible.builtin.include_tasks"] == "node-stop.yml"
    assert (
        names.index(str(refuse["name"]))
        < names.index(str(stop["name"]))
        < names.index("Install the declared version, or put a stopped node back")
    )
    assert _true(stop["when"], installed("installed 9.2.4"))
    assert not _true(stop["when"], installed("installed 9.2.5"))
    assert not _true(stop["when"], installed("", rc=1))
    assert _holds(refuse, installed("installed 9.2.4"))
    # Across a major, the converge stops before any node does.
    assert not _holds(refuse, installed("installed 8.19.6", version="9.2.5"))
    handler = _load("main.yml", "handlers")[0]
    assert handler["ansible.builtin.include_tasks"] == (
        "{{ role_path }}/tasks/restart.yml"
    )
    assert [task["ansible.builtin.include_tasks"] for task in _load("restart.yml")] == [
        "node-stop.yml",
        "node-start.yml",
    ]


def test_only_the_home_node_sets_the_elastic_password(tmp_path: Path) -> None:
    hosts = _hosts(_rendered(tmp_path))
    block = _task(_load("main.yml"), "Set the elastic password once")

    assert _true(block["when"], _variables(hosts["h1"]))
    assert not _true(block["when"], _variables(hosts["h2"]))


def test_the_inventory_refuses_a_transport_on_a_server_without_a_firewall(
    tmp_path: Path,
) -> None:
    """Validation refuses it first; the render never drops 9300's rule silently."""
    project = _rendered_project(tmp_path)
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    (server, _) = inventory.servers
    baseline = dataclasses.replace(
        inventory.baseline(server.baseline_id), firewall=None
    )

    with pytest.raises(ValueError, match="an Elasticsearch transport needs a firewall"):
        _host_firewall(server, baseline, inventory)


# proves: S-012-6
def test_s012_6_a_failed_install_starts_the_stopped_node_and_frees_allocation(
    tmp_path: Path,
) -> None:
    """APT failing after node-stop.yml must not leave a node down and replicas held."""
    hosts = _hosts(_rendered(tmp_path))
    guarded = _task(
        _load("main.yml"), "Install the declared version, or put a stopped node back"
    )
    (install,) = guarded["block"]
    put_back, stop = guarded["rescue"]
    (start,) = put_back["block"]
    (allocation,) = put_back["always"]
    rolling = {"cloudfall_elasticsearch_rolling": {"status": "green", "nodes": "2"}}

    assert install["name"] == "Install the declared Elasticsearch version"
    assert install["notify"] == "Restart Elasticsearch"
    # Only a node this run stopped is started again
    assert _true(put_back["when"], _variables(hosts["h1"], **rolling))
    assert not _true(put_back["when"], _variables(hosts["h1"]))
    assert start["ansible.builtin.systemd_service"] == {
        "name": "elasticsearch.service",
        "state": "started",
    }
    # Allocation is let go even when the start fails, once the node answers
    assert allocation["ansible.builtin.uri"]["body"] == {
        "persistent": {"cluster.routing.allocation.enable": None}
    }
    assert allocation["until"] == (
        "cloudfall_elasticsearch_allocation_back.status == 200"
    )
    assert allocation["no_log"] is True
    assert _true(allocation["when"], _variables(hosts["h1"], **rolling))
    one = {"cloudfall_elasticsearch_rolling": {"status": "yellow", "nodes": "1"}}
    assert not _true(allocation["when"], _variables(hosts["h1"], **one))
    # The converge still stops, naming the failed install
    assert "ansible.builtin.fail" in stop
    message = _value(
        stop["ansible.builtin.fail"]["msg"],
        _variables(
            hosts["h1"], **rolling, ansible_failed_result={"msg": "no such version"}
        ),
    )
    said = " ".join(str(message).split())
    assert "Installing elasticsearch=9.2.4 failed on h1: no such version" in said
    assert "started again on the package it had" in said


ACCOUNTS = (
    "CLOUDFALL_PASSWORD=synthetic-cloudfall\nELASTIC_PASSWORD=synthetic-elastic\n"
    "S3=SYNTHETICACCESSKEY:synthetic/secret+key\n"
)
FACTS = {
    "distribution": "Debian",
    "distribution_major_version": "13",
    "service_mgr": "systemd",
    "memtotal_mb": 16384,
}


def _guards(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the top-level refusals before the package install, in order.

    The later ones read what the running node answers.
    """
    names = [task["name"] for task in tasks]
    install = "Install the declared version, or put a stopped node back"
    before = tasks[: names.index(install)] if install in names else tasks
    return [task for task in before if "ansible.builtin.assert" in task]


@dataclass
class _Services:
    """Run the converge's steps against fake nodes; check the role at services.yml."""

    rendered: dict[str, Any]
    nodes: FakeNodes = field(default_factory=FakeNodes)
    passed: dict[str, list[str]] = field(default_factory=dict)
    keystore: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def __call__(self, step: ExecutionStep) -> _Finished:
        if "--extra-vars" not in step.argv:
            return _Finished()
        extra = json.loads(step.argv[step.argv.index("--extra-vars") + 1])
        self.nodes.answer(Path(step.argv[-1]).name, extra)
        if step.argv[-1].endswith("services.yml"):
            self._role(extra)
        return _Finished()

    def _role(self, extra: dict[str, Any]) -> None:
        """Evaluate each node's guards with what services.yml was handed."""
        main = _load("main.yml")
        transport = _load("transport.yml")
        keystore = _load("keystore.yml")[0]["block"]
        add = _task(keystore, "Put each S3 key in the node's keystore")
        for node, host in _hosts(self.rendered).items():
            files = extra["cloudfall_services_elasticsearch_files"]["search-main"]
            # What main.yml copied from the converge's delivered files
            copied = extra["cloudfall_services_delivered_files"][node]
            delivered = {item["path"] for item in copied}
            paths = host["cloudfall_elasticsearch_transport"]["search-main"]
            variables = _variables(
                {**host, **extra},
                ansible_facts=FACTS,
                cloudfall_elasticsearch_account_files=files,
                cloudfall_elasticsearch_installed={"rc": 1, "stdout": ""},
                cloudfall_elasticsearch_peers={"results": []},
                cloudfall_elasticsearch_transport_files={
                    # The key the node made in logging-requests.yml never left it
                    "results": [
                        {"stat": {"exists": paths["certificatePath"] in delivered}},
                        {"stat": {"exists": True}},
                        {"stat": {"exists": paths["caPath"] in delivered}},
                    ]
                },
                **_formed(exists=False),
            )
            passed: list[str] = []
            for guard in [*_guards(main), *_guards(transport)]:
                if "when" in guard and not _true(guard["when"], variables):
                    continue
                assert _holds(guard, variables), (node, guard["name"])
                passed.append(str(guard["name"]))
            self.passed[node] = passed
            # The keystore reads each key from its staged file, by stdin
            self.keystore[node] = [
                (
                    str(item["setting"]),
                    Path(str(_value(item["source"], variables))).read_text("utf-8"),
                )
                for item in add["loop"]
            ]


@dataclass(frozen=True)
class _Finished:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


# proves: S-012-5
def test_s012_5_a_two_node_s3_project_validates_renders_and_passes_the_guards(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The project S-012-5 converges: no guard of the role stops either node."""
    project = _rendered_project(tmp_path)
    assert main(
        ["config", "validate", "--project", str(project), "--schemas", str(SCHEMAS)]
    ) == 0, capsys.readouterr().out
    rendered = cast(
        "dict[str, Any]",
        render_ansible_inventory(
            PlatformInventory.from_state(validate_config(project, SCHEMAS))
        ),
    )
    text = json.dumps(as_ansible_data(rendered))
    assert "SYNTHETICACCESSKEY" not in text
    assert "synthetic/secret+key" not in text
    for host in _hosts(rendered).values():
        (service,) = host["cloudfall_services"]
        assert service["elasticsearch"]["snapshotRepository"] == SNAPSHOT_REPOSITORY
    secrets = project / "secrets" / "production" / "services" / "search-main.env"
    secrets.parent.mkdir(parents=True)
    secrets.write_text(ACCOUNTS, encoding="utf-8")
    store = SecretStore(
        project / "secrets", decrypt=_plaintext, encrypt=lambda _, value: value
    )
    now = dt.datetime.now(dt.UTC)
    load_or_create_ca(store, ResourceId("production"), now)
    services = _Services(rendered)

    converge_services(
        EngineContext(
            project_directory=project,
            schema_directory=SCHEMAS,
            engine_directory=ROOT / "engine",
            inventory_file=project / "tmp" / "ansible-inventory.json",
            run=services,
        ),
        SopsSecretProvider(secrets_directory=project / "secrets", decrypt=_plaintext),
    )

    assert services.nodes.playbooks[-1] == "services.yml"
    assert set(services.passed) == {"h1", "h2"}
    for node in ("h1", "h2"):
        assert {
            "Refuse a heap above half of the host's memory",
            "Validate Elasticsearch service target and declaration",
            "Require the staged S3 keys of a snapshot repository",
            "Refuse remote access on a cluster of two or more",
            "Require the staged account files of the service",
            "Refuse a version the installed node can't move to",
            "Require the node's rendered transport",
            "Refuse to bootstrap a second cluster beside a running node",
        } <= set(services.passed[node])
        assert services.keystore[node] == [
            ("s3.client.default.access_key", "SYNTHETICACCESSKEY"),
            ("s3.client.default.secret_key", "synthetic/secret+key"),
        ]


def _plaintext(source: Path) -> str:
    """Stand in for sops: the test's secret files are not encrypted."""
    return source.read_text(encoding="utf-8")
