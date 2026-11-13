"""S-011 port provider: the logging gateway opens to its remote collectors only."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from cloudfall.inventory import (
    PlatformInventory,
    firewall_rules_for_server,
    logging_gateway_rules_for_server,
)
from cloudfall.validation import ConfigValidationError, validate_config

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
H2_NETWORK = (
    "  network:\n"
    "    ipv4: 203.0.113.12\n"
    "    ipv6Cidr: 2001:db8:12::/64\n"
    "    reverseDns: h2.example.internal\n"
)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "config"
    shutil.copytree(EXAMPLES, project, ignore=shutil.ignore_patterns("tmp"))
    return project


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def _gateway_rules(project: Path) -> list[dict[str, object]]:
    """Render h1's gateway rules as the engine does, baseline rules included."""
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    h1 = next(item for item in inventory.servers if item.resource_id.value == "h1")
    baseline = next(
        item for item in inventory.baselines if item.resource_id == h1.baseline_id
    )
    assert baseline.firewall is not None
    open_ports = frozenset(
        (rule.port.value, rule.protocol)
        for rule in firewall_rules_for_server(baseline.firewall, h1.ssh_port)
    )
    return [
        rule.as_dict()
        for rule in logging_gateway_rules_for_server(
            h1, inventory.servers, inventory.logging_stacks, open_ports
        )
    ]


def test_the_gateway_opens_to_the_remote_collector_networks(tmp_path: Path) -> None:
    assert _gateway_rules(_project(tmp_path)) == [
        {
            "port": 3101,
            "protocol": "tcp",
            "sources": ["203.0.113.12/32", "2001:db8:12::/64"],
            "description": "operations logging gateway",
        }
    ]


def test_a_collector_addressed_by_ip_opens_to_that_address(tmp_path: Path) -> None:
    project = _project(tmp_path)
    h2 = project / "servers" / "h2.yaml"
    _replace(h2, H2_NETWORK, "")
    _replace(h2, "  address: h2.example.internal\n", "  address: 198.51.100.7\n")

    rules = _gateway_rules(project)

    assert [rule["sources"] for rule in rules] == [["198.51.100.7/32"]]


def test_a_single_host_stack_opens_no_port(tmp_path: Path) -> None:
    project = _project(tmp_path)
    stack = project / "logging-stacks" / "operations.yaml"
    lines = stack.read_text(encoding="utf-8").splitlines(keepends=True)
    stack.write_text(
        "".join(line for line in lines if line.strip() != "- h2"), encoding="utf-8"
    )

    assert _gateway_rules(project) == []


def test_a_collector_with_no_ip_is_refused_behind_a_firewall(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _replace(project / "servers" / "h2.yaml", H2_NETWORK, "")

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project, SCHEMAS)

    assert error.value.issue.code == "logging_gateway_collector_unaddressed"


def test_a_port_the_baseline_opens_to_everyone_needs_no_rule(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _replace(project / "servers" / "h2.yaml", H2_NETWORK, "")
    _replace(
        project / "baselines" / "debian-application.yaml",
        "    allowedInbound:\n",
        "    allowedInbound:\n"
        "      - port: 3101\n"
        "        protocol: tcp\n"
        "        description: logging gateway\n",
    )

    assert _gateway_rules(project) == []
