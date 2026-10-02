"""Typed platform inventory tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cloudfall.cli import main
from cloudfall.domain import (
    FirewallPolicy,
    NetworkProtocol,
    ResourceId,
    TcpPort,
)
from cloudfall.inventory import (
    FirewallRequirement,
    FirewallRule,
    PlatformInventory,
    firewall_rules_for_server,
)
from cloudfall.validation import validate_config

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"


def _inventory() -> PlatformInventory:
    return PlatformInventory.from_state(validate_config(EXAMPLES, SCHEMAS))


def test_effective_firewall_rules_guarantee_the_ssh_port_once() -> None:
    firewall = FirewallRequirement(
        policy=FirewallPolicy.DEFAULT_DENY,
        allowed_inbound=(
            FirewallRule(
                port=TcpPort(22),
                protocol=NetworkProtocol.TCP,
                description=None,
            ),
            FirewallRule(
                port=TcpPort(443),
                protocol=NetworkProtocol.TCP,
                description="https",
            ),
        ),
    )

    declared = firewall_rules_for_server(firewall, TcpPort(22))
    assert [(rule.port.value, rule.protocol.value) for rule in declared] == [
        (22, "tcp"),
        (443, "tcp"),
    ]

    custom = firewall_rules_for_server(firewall, TcpPort(8022))
    assert custom[0].port.value == 8022
    assert custom[0].protocol is NetworkProtocol.TCP
    assert custom[0].description == "ssh"


def test_firewall_requirement_rejects_duplicate_rules() -> None:
    rule = FirewallRule(
        port=TcpPort(80),
        protocol=NetworkProtocol.TCP,
        description=None,
    )
    with pytest.raises(ValueError, match="duplicate firewall rule"):
        FirewallRequirement(
            policy=FirewallPolicy.DEFAULT_DENY,
            allowed_inbound=(rule, rule),
        )


def test_inventory_exposes_typed_placement_queries() -> None:
    inventory = _inventory()

    components = inventory.components_on_server(ResourceId("h1"))
    servers = inventory.servers_for_component(ResourceId("crm-backend"))

    assert [component.resource_id.value for component in components] == ["crm-backend"]
    assert [server.resource_id.value for server in servers] == ["h1", "h2"]


def test_inventory_rejects_unknown_query_targets() -> None:
    inventory = _inventory()

    with pytest.raises(KeyError, match="server does not exist"):
        inventory.components_on_server(ResourceId("h9"))

    with pytest.raises(KeyError, match="component does not exist"):
        inventory.servers_for_component(ResourceId("crm-worker"))


def test_inventory_serialization_excludes_secret_references() -> None:
    payload = _inventory().as_dict()
    servers = payload["servers"]
    server_types = payload["serverTypes"]
    applications = payload["applications"]
    components = payload["components"]
    domains = payload["domains"]
    ssh_public_keys = payload["sshPublicKeys"]
    logging_stacks = payload["loggingStacks"]

    assert isinstance(servers, list)
    assert isinstance(server_types, list)
    assert isinstance(applications, list)
    assert isinstance(components, list)
    assert isinstance(domains, list)
    assert isinstance(ssh_public_keys, list)
    assert len(servers) == 2
    assert len(server_types) == 1
    assert len(applications) == 1
    assert len(components) == 1
    assert len(domains) == 1
    assert domains[0]["id"] == "crm-site"
    assert domains[0]["proxy"]["server"] == "h2"
    assert domains[0]["proxy"]["upstream"] == {
        "address": "127.0.0.1",
        "port": 8100,
    }
    assert domains[0]["tls"] == {"mode": "required"}
    assert len(ssh_public_keys) == 1
    assert ssh_public_keys[0]["id"] == "example-admin"
    assert ssh_public_keys[0]["algorithm"] == "ssh-rsa"
    assert ssh_public_keys[0]["lifecycle"] == "active"
    assert isinstance(logging_stacks, list)
    assert logging_stacks[0]["id"] == "operations"
    assert logging_stacks[0]["migration"]["preserveLegacyAgents"] is True
    assert logging_stacks[0]["backend"]["listenAddress"] == "127.0.0.1"
    assert logging_stacks[0]["metrics"] == {
        "backend": {
            "listenAddress": "127.0.0.1",
            "port": 9090,
            "storagePath": "/var/lib/prometheus-cloudfall",
            "retentionHours": 360,
        },
        "collection": {"intervalSeconds": 60},
    }
    assert "prometheusPackageVersion" not in logging_stacks[0]["software"]
    operator_policies = payload["operatorPolicies"]
    assert isinstance(operator_policies, list)
    assert operator_policies == [
        {
            "id": "production-operator",
            "environment": "production",
            "autonomy": {
                "operations": [
                    {"kind": "converge-services", "requiredVerifiedRuns": 1},
                    {"kind": "converge-baseline", "requiredVerifiedRuns": 2},
                ],
                "maxAutonomousPerHour": 4,
                "quietHours": {"start": "01:00", "end": "05:00"},
            },
        }
    ]
    assert logging_stacks[0]["alerting"] == {
        "alertmanager": {"listenAddress": "127.0.0.1", "port": 9093},
        "receivers": [
            {
                "id": "operations-webhook",
                "webhook": {"url": "https://hooks.example.internal/cloudfall"},
            },
            {
                "id": "operations-email",
                "email": {
                    "smarthost": "smtp.example.internal:587",
                    "from": "alerts@example.internal",
                    "to": "oncall@example.internal",
                    "authUsername": "alerts@example.internal",
                    "authPasswordFile": "/etc/cloudfall/logging/smtp-password",
                },
            },
        ],
    }
    assert server_types[0]["id"] == "debian-application"
    serialized = json.dumps(payload).lower()
    assert "private key" not in serialized
    assert "passwordvalue" not in serialized


def test_inventory_serialization_covers_declared_services() -> None:
    services = _inventory().as_dict()["services"]

    assert isinstance(services, list)
    assert len(services) == 2
    assert services[0]["id"] == "postgresql-main"
    assert services[0]["bind"] == {"address": "127.0.0.1", "port": 5432}
    assert services[0]["postgresql"]["databases"] == [
        {"name": "crm", "application": "crm", "owner": "crm"}
    ]
    assert "packageVersion" not in services[0]["postgresql"]
    assert services[0]["metrics"] == {"enabled": True}
    assert services[1]["id"] == "redis-cache"
    assert services[1]["serviceKind"] == "redis"
    assert services[1]["bind"] == {"address": "127.0.0.1", "port": 6379}
    assert services[1]["redis"] == {"maxmemoryMb": 256, "appendOnly": True}
    assert "postgresql" not in services[1]
    assert services[1]["metrics"] == {"enabled": True}


def test_inventory_cli_emits_structured_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "inventory",
            "show",
            "--project",
            str(EXAMPLES),
            "--schemas",
            str(SCHEMAS),
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["data"]["status"] == "ok"
    assert payload["data"]["inventory"]["components"][0]["id"] == "crm-backend"
    assert payload["data"]["ansible"] == {}
    assert captured.err == ""
