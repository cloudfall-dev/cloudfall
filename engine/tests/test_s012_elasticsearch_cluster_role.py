"""The Elasticsearch role grows a cluster and rolls a patch one node at a time.

S-012, #312. Both need live hosts for the whole proof; here the rendered
inventory of a two-node service is real, each node's configuration and the
firewall ruleset are rendered from their templates, the role's decisions are
evaluated with Ansible's own templating, and the rollout order is read from
Ansible's inventory manager.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import yaml
from ansible.inventory.manager import InventoryManager
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.ansible_data import as_ansible_data
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cloudfall_engine.cli import _rendered_elasticsearch_services
from elasticsearch_project import (
    SCHEMAS,
    SNAPSHOT_REPOSITORY,
    elasticsearch_service,
    write_project,
)

ROOT = Path(__file__).parents[2]
ROLES = ROOT / "engine" / "ansible" / "roles"
ROLE = ROLES / "cloudfall_elasticsearch"
SERVICES = ROOT / "engine" / "ansible" / "playbooks" / "services.yml"
FIREWALL = ROLES / "cloudfall_firewall" / "templates" / "nftables.conf.j2"


def _rendered(tmp_path: Path, *, server: str = "h1") -> dict[str, Any]:
    """Render the inventory of S-012-5: the service grown to h1 and h2."""
    project = write_project(
        tmp_path / "project",
        elasticsearch_service(
            server=server, nodes=["h1", "h2"], snapshotRepository=SNAPSHOT_REPOSITORY
        ),
        servers=("h1", "h2"),
    )
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
            f'ip saddr 203.0.113.{other}/32 tcp dport 9300 accept comment '
            '"search-main transport"',
            f'ip6 saddr 2001:db8:{other}::/64 tcp dport 9300 accept comment '
            '"search-main transport"',
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
    assert transport.index(
        "Enforce the declared firewall with the transport rule"
    ) < transport.index("Install the transport certificate and the project CA")
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
    (block,) = [task for task in tasks if "block" in task]
    steps = block["block"]
    rolling = {"cloudfall_elasticsearch_rolling": {"status": "green", "nodes": "2"}}
    variables = _variables(hosts["h2"], **rolling)

    assert tasks[0]["ansible.builtin.systemd_service"]["state"] == "restarted"
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
        < names.index("Install the declared Elasticsearch version")
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
