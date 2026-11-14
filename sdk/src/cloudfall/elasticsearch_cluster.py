"""Where the nodes of an Elasticsearch cluster meet each other (S-012).

A service of one node keeps its transport on loopback, as a cluster of one.
A service of two or more nodes binds each node's transport to its IPv4
address, from its ``network`` or an IP ``address``: the other nodes find it
there (``discovery.seed_hosts``), its firewall admits that port from the
other nodes' addresses only, and its transport certificate names that address
beside the hostname.
"""

from __future__ import annotations

from ipaddress import IPv4Address, ip_address
from typing import TYPE_CHECKING

from cloudfall.domain import AbsolutePath

if TYPE_CHECKING:
    from cloudfall.inventory import ServerInventory

CERTIFICATE_DIRECTORY = "/etc/elasticsearch/certs"
"""Inside the configuration directory: Elasticsearch reads TLS files only there."""
TRANSPORT_CERTIFICATE_PATH = AbsolutePath(f"{CERTIFICATE_DIRECTORY}/transport.crt")
TRANSPORT_KEY_PATH = AbsolutePath(f"{CERTIFICATE_DIRECTORY}/transport.key")
TRANSPORT_CA_PATH = AbsolutePath(f"{CERTIFICATE_DIRECTORY}/ca.crt")


def transport_address(server: ServerInventory) -> IPv4Address | None:
    """Return the IPv4 a node binds its transport to, if Cloudfall knows one."""
    if server.network is not None:
        return IPv4Address(server.network.ipv4.value)
    try:
        address = ip_address(server.address.value)
    except ValueError:
        return None
    return address if isinstance(address, IPv4Address) else None
