"""Where the nodes of an Elasticsearch cluster meet each other (S-012).

A service of one node keeps its transport on loopback, as a cluster of one.
A service of two or more nodes binds each node's transport to its IPv4
address, from its ``network`` or an IP ``address``: the other nodes find it
there (``discovery.seed_hosts``), its firewall admits that port from the
other nodes' addresses only, and its transport certificate names that address
beside the hostname.

A service with ``existingCluster`` joins a cluster Cloudfall did not create,
so even one node of it leaves loopback the same way: its seed hosts are the
existing cluster's first, then the Cloudfall nodes, its firewall also admits
the existing nodes' addresses, and it trusts the existing cluster's CA beside
the project CA. Removing ``existingCluster`` drops all three.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, ip_address
from typing import TYPE_CHECKING

from cloudfall.domain import (
    AbsolutePath,
    IpNetworkCidr,
    NetworkProtocol,
    ResourceId,
    TcpPort,
)
from cloudfall.inventory import SourceRestrictedFirewallRule, server_source_networks

if TYPE_CHECKING:
    from cloudfall.elasticsearch import ElasticsearchSeedHost
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
EXISTING_CLUSTER_CA_PATH = AbsolutePath(
    f"{CERTIFICATE_DIRECTORY}/existing-cluster-ca.crt"
)
"""The existing cluster's CA, trusted beside the project CA while it is declared."""


def is_cluster(service: ServiceInventory) -> bool:
    """Return whether the service's transport leaves loopback.

    It does with two or more nodes, and with ``existingCluster``, whose nodes
    join a cluster that runs elsewhere.
    """
    elasticsearch = service.elasticsearch
    return elasticsearch is not None and (
        len(elasticsearch.nodes) > 1 or elasticsearch.existing_cluster is not None
    )


def seed_host_network(seed_host: ElasticsearchSeedHost) -> IpNetworkCidr:
    """Return the one-address network a seed host's traffic comes from.

    Validation refuses a seed host named by DNS: the firewall admits addresses.
    """
    try:
        address = ip_address(seed_host.host)
    except ValueError as error:
        message = f"seed host {seed_host.value} is not an IP address"
        raise ValueError(message) from error
    return IpNetworkCidr(f"{address}/{address.max_prefixlen}")


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


def cluster_nodes(
    service: ServiceInventory, servers: tuple[ServerInventory, ...]
) -> tuple[ClusterNode, ...]:
    """Return the nodes whose transport leaves loopback, in declared order.

    A service of one node that joins no existing cluster has none: its
    transport stays on loopback.
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
    """Return the transport port opened to the other nodes of each cluster only.

    The other nodes are the Cloudfall nodes, then an existing cluster's seed
    hosts, whose nodes open connections to a joining node too.
    """
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
        for seed_host in _existing_seed_hosts(service):
            network = seed_host_network(seed_host)
            if network not in sources:
                sources.append(network)
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
    """Return, by service id, the node's transport address and every seed host.

    With ``existingCluster``, the existing cluster's seed hosts come first and
    ``existingCaPath`` names where its CA lands; without it, neither appears.
    """
    found: dict[str, dict[str, object]] = {}
    for service in services:
        nodes = cluster_nodes(service, servers)
        own = next(
            (node for node in nodes if node.server_id == server.resource_id), None
        )
        if own is None:
            continue
        existing = _existing_seed_hosts(service)
        transport: dict[str, object] = {
            "address": str(own.address),
            "port": TRANSPORT_PORT.value,
            "seedHosts": [
                *(seed_host.value for seed_host in existing),
                *(node.seed_host for node in nodes),
            ],
            "certificatePath": TRANSPORT_CERTIFICATE_PATH.value,
            "keyPath": TRANSPORT_KEY_PATH.value,
            "caPath": TRANSPORT_CA_PATH.value,
        }
        if existing:
            transport["existingCaPath"] = EXISTING_CLUSTER_CA_PATH.value
        found[service.resource_id.value] = transport
    return found


def _existing_seed_hosts(
    service: ServiceInventory,
) -> tuple[ElasticsearchSeedHost, ...]:
    """Return the seed hosts of the existing cluster the service joins, if any."""
    elasticsearch = service.elasticsearch
    if elasticsearch is None or elasticsearch.existing_cluster is None:
        return ()
    return elasticsearch.existing_cluster.seed_hosts
