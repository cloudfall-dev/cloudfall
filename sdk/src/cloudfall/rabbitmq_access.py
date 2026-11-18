"""How clients outside a RabbitMQ node's server reach it: ``clientAccess`` (S-023).

Without ``clientAccess`` a node answers clients on loopback only, in plain
text. With it, each node also listens on its server's IPv4 over TLS alone
(5671), with its certificate from the project CA, and its firewall admits
that port from ``allowedSources`` only; the plain port stays on loopback, so
a plain connection from outside the host is refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloudfall.domain import AbsolutePath, NetworkProtocol, TcpPort
from cloudfall.elasticsearch_cluster import transport_address
from cloudfall.inventory import SourceRestrictedFirewallRule

if TYPE_CHECKING:
    from cloudfall.inventory import ServerInventory, ServiceInventory

CLIENT_TLS_PORT = TcpPort(5671)
"""AMQP over TLS, the one port ``clientAccess`` opens on a node's IPv4."""
CERTIFICATE_DIRECTORY = "/etc/rabbitmq/cloudfall"
CERTIFICATE_PATH = AbsolutePath(f"{CERTIFICATE_DIRECTORY}/node.crt")
KEY_PATH = AbsolutePath(f"{CERTIFICATE_DIRECTORY}/node.key")
CA_PATH = AbsolutePath(f"{CERTIFICATE_DIRECTORY}/ca.crt")
CLIENT_ACCESS_VARIABLE = "cloudfall_rabbitmq_client_access"
"""The host variable naming, by service id, where a node takes clients over TLS."""


def _is_node(service: ServiceInventory, server: ServerInventory) -> bool:
    return service.rabbitmq is not None and server.resource_id in service.rabbitmq.nodes


def client_access_rules_for_server(
    server: ServerInventory, services: tuple[ServiceInventory, ...]
) -> tuple[SourceRestrictedFirewallRule, ...]:
    """Return the TLS client port of each node, open to ``allowedSources`` only."""
    rules: list[SourceRestrictedFirewallRule] = []
    for service in services:
        if not _is_node(service, server) or service.rabbitmq is None:
            continue
        sources = service.rabbitmq.client_access
        if sources is None:
            continue
        if any(source.is_unrestricted for source in sources):
            message = (
                "clientAccess refuses a source of every address: "
                f"{service.resource_id.value}"
            )
            raise ValueError(message)
        rules.append(
            SourceRestrictedFirewallRule(
                port=CLIENT_TLS_PORT,
                protocol=NetworkProtocol.TCP,
                sources=sources,
                description=f"{service.resource_id.value} clientAccess",
            )
        )
    return tuple(rules)


def client_access_for_server(
    server: ServerInventory, services: tuple[ServiceInventory, ...]
) -> dict[str, dict[str, object]]:
    """Return, by service id, the IPv4 and port a node takes clients at over TLS.

    Validation refuses a node with ``clientAccess`` and no IPv4
    (``service_rabbitmq_server_unaddressed``).
    """
    found: dict[str, dict[str, object]] = {}
    for service in services:
        if (
            not _is_node(service, server)
            or service.rabbitmq is None
            or service.rabbitmq.client_access is None
        ):
            continue
        address = transport_address(server)
        if address is None:
            message = (
                f"node {server.resource_id.value} of RabbitMQ service "
                f"{service.resource_id.value} has no IPv4 for clientAccess"
            )
            raise ValueError(message)
        found[service.resource_id.value] = {
            "address": str(address),
            "port": CLIENT_TLS_PORT.value,
            "certificatePath": CERTIFICATE_PATH.value,
            "keyPath": KEY_PATH.value,
            "caPath": CA_PATH.value,
        }
    return found
