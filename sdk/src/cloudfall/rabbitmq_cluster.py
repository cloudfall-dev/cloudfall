"""Where the nodes of a RabbitMQ cluster of three meet each other (S-023).

A service of one node keeps everything on loopback. A service of three runs
node discovery (epmd, 4369) and the inter-node port (25672) on each node's
IPv4, from its ``network`` or an IP ``address``: its firewall admits both
from the other nodes' addresses only, every node resolves the others'
hostnames to those addresses, and every connection between nodes runs TLS
with the node's certificate from the project CA, verified in both directions;
the connecting side also checks the peer's short hostname, the host part of
its node name, which the certificate names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloudfall.domain import Hostname, IpNetworkCidr, NetworkProtocol, TcpPort
from cloudfall.elasticsearch_cluster import transport_address
from cloudfall.inventory import SourceRestrictedFirewallRule, server_source_networks
from cloudfall.rabbitmq_access import CA_PATH, CERTIFICATE_PATH, KEY_PATH

if TYPE_CHECKING:
    from ipaddress import IPv4Address

    from cloudfall.domain import IpAddress
    from cloudfall.inventory import ServerInventory, ServiceInventory

EPMD_PORT = TcpPort(4369)
"""Node discovery: each node asks the others' epmd where their node listens."""
DISTRIBUTION_PORT = TcpPort(25672)
"""The inter-node port, over TLS, that the nodes and the command-line tools use."""
CLUSTER_VARIABLE = "cloudfall_rabbitmq_cluster"
"""The host variable naming, by service id, how a node meets the others."""


def is_cluster(service: ServiceInventory) -> bool:
    """Return whether the service's nodes leave loopback to meet each other."""
    return service.rabbitmq is not None and len(service.rabbitmq.nodes) > 1


def short_hostname(server: ServerInventory) -> Hostname:
    """Return the server's hostname up to its first dot: ``h2`` of ``h2.example``."""
    return Hostname(server.hostname.value.removesuffix(".").split(".")[0])


def node_name(server: ServerInventory) -> str:
    """Return ``rabbit@`` and the server's short hostname, the node's name."""
    return f"rabbit@{short_hostname(server).value}"


def node_certificate_names(
    names: tuple[Hostname | IpAddress, ...], server: ServerInventory
) -> tuple[Hostname | IpAddress, ...]:
    """Return a node of three's certificate names: ``names``, then its short hostname.

    A node connecting to another checks the host part of its node name, the
    short hostname, against the peer's certificate, so the certificate names
    it beside the hostname and addresses a client reaches it at.
    """
    short = short_hostname(server)
    known = {name.value.removesuffix(".").lower() for name in names}
    if short.value.lower() in known:
        return names
    return (*names, short)


def _address(service: ServiceInventory, server: ServerInventory) -> IPv4Address:
    address = transport_address(server)
    if address is None:
        message = (
            f"node {server.resource_id.value} of RabbitMQ service "
            f"{service.resource_id.value} has no IPv4 the other nodes reach"
        )
        raise ValueError(message)
    return address


def _nodes(
    service: ServiceInventory, servers: tuple[ServerInventory, ...]
) -> tuple[ServerInventory, ...]:
    """Return the cluster's servers in declared order, or none for one node.

    Validation refuses a node of three without an IPv4
    (``service_rabbitmq_server_unaddressed``).
    """
    if not is_cluster(service) or service.rabbitmq is None:
        return ()
    by_id = {server.resource_id: server for server in servers}
    return tuple(by_id[node] for node in service.rabbitmq.nodes)


def cluster_rules_for_server(
    server: ServerInventory,
    servers: tuple[ServerInventory, ...],
    services: tuple[ServiceInventory, ...],
) -> tuple[SourceRestrictedFirewallRule, ...]:
    """Return node discovery and the inter-node port, open to the other nodes only."""
    rules: list[SourceRestrictedFirewallRule] = []
    for service in services:
        nodes = _nodes(service, servers)
        if server.resource_id not in {node.resource_id for node in nodes}:
            continue
        sources: list[IpNetworkCidr] = []
        for node in nodes:
            if node.resource_id == server.resource_id:
                continue
            sources.extend(
                item for item in server_source_networks(node) if item not in sources
            )
        rules.extend(
            SourceRestrictedFirewallRule(
                port=port,
                protocol=NetworkProtocol.TCP,
                sources=tuple(sources),
                description=f"{service.resource_id.value} {label}",
            )
            for port, label in (
                (EPMD_PORT, "node discovery"),
                (DISTRIBUTION_PORT, "inter-node"),
            )
        )
    return tuple(rules)


def cluster_for_server(
    server: ServerInventory,
    servers: tuple[ServerInventory, ...],
    services: tuple[ServiceInventory, ...],
) -> dict[str, dict[str, object]]:
    """Return, by service id, the node's address, every node, and its TLS files.

    ``nodes`` holds each node's name, hostname and IPv4 in declared order, the
    service's server first among equals only by declaration: the role makes
    every hostname resolve to that IPv4 on every node.
    """
    found: dict[str, dict[str, object]] = {}
    for service in services:
        nodes = _nodes(service, servers)
        if server.resource_id not in {node.resource_id for node in nodes}:
            continue
        found[service.resource_id.value] = {
            "address": str(_address(service, server)),
            "nodes": [
                {
                    "server": node.resource_id.value,
                    "name": node_name(node),
                    "hostname": node.hostname.value.removesuffix("."),
                    "address": str(_address(service, node)),
                }
                for node in nodes
            ],
            "certificatePath": CERTIFICATE_PATH.value,
            "keyPath": KEY_PATH.value,
            "caPath": CA_PATH.value,
        }
    return found
