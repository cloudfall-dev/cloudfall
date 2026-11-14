"""Where the nodes of an Elasticsearch cluster meet each other (S-012).

A service of one node keeps its transport on loopback, as a cluster of one.
A service of two or more nodes binds each node's transport to its IPv4
address, from its ``network`` or an IP ``address``: the other nodes find it
there (``discovery.seed_hosts``), its firewall admits that port from the
other nodes' addresses only, and its transport certificate names that address
beside the hostname.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, ip_address
from typing import TYPE_CHECKING

from cloudfall.domain import (
    AbsolutePath,
    Hostname,
    IpNetworkCidr,
    NetworkProtocol,
    ResourceId,
    TcpPort,
)
from cloudfall.inventory import SourceRestrictedFirewallRule, server_source_networks

if TYPE_CHECKING:
    from cloudfall.inventory import ServerInventory, ServiceInventory

TRANSPORT_PORT = TcpPort(9300)
"""The transport port every node listens on and reaches the others at."""
DATA_PATH = AbsolutePath("/var/lib/elasticsearch")
"""``path.data``, the mount path of the node's data volume."""
CERTIFICATE_DIRECTORY = "/etc/elasticsearch/certs"
"""Inside the configuration directory: Elasticsearch reads TLS files only there."""
TRANSPORT_CERTIFICATE_PATH = AbsolutePath(f"{CERTIFICATE_DIRECTORY}/transport.crt")
TRANSPORT_KEY_PATH = AbsolutePath(f"{CERTIFICATE_DIRECTORY}/transport.key")
TRANSPORT_CA_PATH = AbsolutePath(f"{CERTIFICATE_DIRECTORY}/ca.crt")


def is_cluster(service: ServiceInventory) -> bool:
    """Return whether the service has two or more nodes: transport off loopback."""
    return service.elasticsearch is not None and len(service.elasticsearch.nodes) > 1


def transport_address(server: ServerInventory) -> IPv4Address | None:
    """Return the IPv4 a node binds its transport to, if Cloudfall knows one."""
    if server.network is not None:
        return IPv4Address(server.network.ipv4.value)
    try:
        address = ip_address(server.address.value)
    except ValueError:
        return None
    return address if isinstance(address, IPv4Address) else None


@dataclass(frozen=True, slots=True)
class ClusterNode:
    """One node of a cluster of two or more: its server and transport address."""

    server: ServerInventory
    address: IPv4Address

    @property
    def server_id(self) -> ResourceId:
        """Return the node's server id, also its ``node.name``."""
        return self.server.resource_id

    @property
    def seed_host(self) -> str:
        """Return ``address:port``, the form ``discovery.seed_hosts`` takes."""
        return f"{self.address}:{TRANSPORT_PORT.value}"

    @property
    def subject_names(self) -> tuple[Hostname, ...]:
        """Return its transport certificate's names: hostname, then address."""
        return (self.server.hostname, Hostname(str(self.address)))


def cluster_nodes(
    service: ServiceInventory, servers: tuple[ServerInventory, ...]
) -> tuple[ClusterNode, ...]:
    """Return the nodes of a cluster of two or more, in declared order.

    A service of one node has none: its transport stays on loopback.
    Validation refuses a node without an IPv4
    (``service_elasticsearch_node_unaddressed``).
    """
    if not is_cluster(service) or service.elasticsearch is None:
        return ()
    by_id = {server.resource_id: server for server in servers}
    nodes: list[ClusterNode] = []
    for node_id in service.elasticsearch.nodes:
        server = by_id[node_id]
        address = transport_address(server)
        if address is None:
            message = (
                f"node {node_id.value} of Elasticsearch service "
                f"{service.resource_id.value} has no IPv4 for its transport"
            )
            raise ValueError(message)
        nodes.append(ClusterNode(server, address))
    return tuple(nodes)


def transport_rules_for_server(
    server: ServerInventory,
    servers: tuple[ServerInventory, ...],
    services: tuple[ServiceInventory, ...],
) -> tuple[SourceRestrictedFirewallRule, ...]:
    """Return the transport port opened to the other nodes of each cluster only."""
    rules: list[SourceRestrictedFirewallRule] = []
    for service in services:
        nodes = cluster_nodes(service, servers)
        if server.resource_id not in {node.server_id for node in nodes}:
            continue
        sources: list[IpNetworkCidr] = []
        for node in nodes:
            if node.server_id == server.resource_id:
                continue
            sources.extend(
                item
                for item in server_source_networks(node.server)
                if item not in sources
            )
        rules.append(
            SourceRestrictedFirewallRule(
                port=TRANSPORT_PORT,
                protocol=NetworkProtocol.TCP,
                sources=tuple(sources),
                description=f"{service.resource_id.value} transport",
            )
        )
    return tuple(rules)


def transport_for_server(
    server: ServerInventory,
    servers: tuple[ServerInventory, ...],
    services: tuple[ServiceInventory, ...],
) -> dict[str, dict[str, object]]:
    """Return, by service id, the node's transport address and every seed host."""
    found: dict[str, dict[str, object]] = {}
    for service in services:
        nodes = cluster_nodes(service, servers)
        own = next(
            (node for node in nodes if node.server_id == server.resource_id), None
        )
        if own is None:
            continue
        found[service.resource_id.value] = {
            "address": str(own.address),
            "port": TRANSPORT_PORT.value,
            "seedHosts": [node.seed_host for node in nodes],
            "certificatePath": TRANSPORT_CERTIFICATE_PATH.value,
            "keyPath": TRANSPORT_KEY_PATH.value,
            "caPath": TRANSPORT_CA_PATH.value,
        }
    return found
