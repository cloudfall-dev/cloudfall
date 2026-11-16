"""Where the two servers of a MariaDB pair meet each other (S-019, Replication).

A primary admits its port from the IPv4 of each replica's server only, and
its replication account, ``cloudfall_repl``, exists at those addresses only.
A replica connects to its primary's ``listenAddress`` at the primary's port
and verifies its certificate against the project CA. The role reads the
other server of its pair on that server itself, so the inventory names, for
each service of an environment where a MariaDB service replicates, its
primary, its replicas, and every other such service by ``serverId``: the
server a replica follows is found by the ``server_id`` it reports, after its
declaration has changed too.

The IPv6 networks of a replica's server are not admitted: MariaDB matches
no IPv6 network in an account's host, so they would open a port no account
uses (#434).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cloudfall.domain import IpNetworkCidr, NetworkProtocol
from cloudfall.elasticsearch_cluster import transport_address
from cloudfall.inventory import SourceRestrictedFirewallRule
from cloudfall.mariadb import MariadbReplicationRole

if TYPE_CHECKING:
    from pathlib import Path

    from cloudfall.domain import ResourceId
    from cloudfall.inventory import ServerInventory, ServiceInventory

LINKS_VARIABLE = "cloudfall_mariadb_replication"
"""The host variable naming, by service id, the pair each service is part of."""


@dataclass(frozen=True, slots=True)
class _Declared:
    """A MariaDB service that replicates, with its server."""

    service: ServiceInventory
    server: ServerInventory

    @property
    def role(self) -> MariadbReplicationRole:
        replication = self.service.mariadb.replication if self.service.mariadb else None
        if replication is None:
            message = f"service {self.service.resource_id.value} does not replicate"
            raise ValueError(message)
        return replication.role


def _replicating(
    servers: tuple[ServerInventory, ...], services: tuple[ServiceInventory, ...]
) -> list[_Declared]:
    by_id = {server.resource_id: server for server in servers}
    return [
        _Declared(service, by_id[service.server_id])
        for service in services
        if service.mariadb is not None and service.mariadb.replication is not None
    ]


def replica_address(server: ServerInventory) -> str:
    """Return the IPv4 a replica's server connects from; validation requires one."""
    address = transport_address(server)
    if address is None:
        message = (
            f"server {server.resource_id.value} of a MariaDB replica has no IPv4 "
            "its primary can admit"
        )
        raise ValueError(message)
    return str(address)


def replicas_of(
    primary: ServiceInventory,
    servers: tuple[ServerInventory, ...],
    services: tuple[ServiceInventory, ...],
) -> list[tuple[ServiceInventory, ServerInventory]]:
    """Return the services that name ``primary`` as theirs, with their servers."""
    return [
        (item.service, item.server)
        for item in _replicating(servers, services)
        if item.service.mariadb is not None
        and item.service.mariadb.replication is not None
        and item.service.mariadb.replication.primary == primary.resource_id
    ]


def replication_rules_for_server(
    server: ServerInventory,
    servers: tuple[ServerInventory, ...],
    services: tuple[ServiceInventory, ...],
) -> tuple[SourceRestrictedFirewallRule, ...]:
    """Return a primary's port, opened to the IPv4 of its replicas' servers only."""
    rules: list[SourceRestrictedFirewallRule] = []
    for item in _replicating(servers, services):
        if (
            item.server.resource_id != server.resource_id
            or item.role is not MariadbReplicationRole.PRIMARY
        ):
            continue
        sources = list(
            dict.fromkeys(
                IpNetworkCidr(f"{replica_address(replica_server)}/32")
                for _, replica_server in replicas_of(item.service, servers, services)
            )
        )
        if not sources:
            continue
        rules.append(
            SourceRestrictedFirewallRule(
                port=item.service.bind.port,
                protocol=NetworkProtocol.TCP,
                sources=tuple(sources),
                description=f"{item.service.resource_id.value} replication",
            )
        )
    return tuple(rules)


def replication_links_for_server(
    server: ServerInventory,
    servers: tuple[ServerInventory, ...],
    services: tuple[ServiceInventory, ...],
) -> dict[str, dict[str, object]]:
    """Return, by service id, the pair each MariaDB service of ``server`` is in.

    A service is named when its environment holds a MariaDB service that
    replicates, so one whose replication was just removed still finds the
    server it followed. Nothing is returned otherwise.
    """
    declared = _replicating(servers, services)
    found: dict[str, dict[str, object]] = {}
    for service in services:
        if service.mariadb is None or service.server_id != server.resource_id:
            continue
        others = [
            item
            for item in declared
            if item.service.environment == service.environment
            and item.service.resource_id != service.resource_id
        ]
        replication = service.mariadb.replication
        if replication is None and not others:
            continue
        links: dict[str, object] = {"peers": [_peer(item) for item in others]}
        if replication is not None and replication.primary is not None:
            links["primary"] = _primary(replication.primary, declared)
        if (
            replication is not None
            and replication.role is MariadbReplicationRole.PRIMARY
        ):
            links["replicas"] = [
                {
                    "service": replica.resource_id.value,
                    "server": replica_server.resource_id.value,
                    "serverId": _server_id(replica),
                    "hosts": [replica_address(replica_server)],
                }
                for replica, replica_server in replicas_of(service, servers, services)
            ]
        found[service.resource_id.value] = links
    return found


def _server_id(service: ServiceInventory) -> int:
    if service.mariadb is None or service.mariadb.replication is None:
        message = f"service {service.resource_id.value} does not replicate"
        raise ValueError(message)
    return service.mariadb.replication.server_id.value


def _peer(item: _Declared) -> dict[str, object]:
    replication = item.service.mariadb.replication if item.service.mariadb else None
    if replication is None:
        message = f"service {item.service.resource_id.value} does not replicate"
        raise ValueError(message)
    address = transport_address(item.server)
    peer: dict[str, object] = {
        "service": item.service.resource_id.value,
        "server": item.server.resource_id.value,
        "serverId": replication.server_id.value,
        "role": replication.role.value,
        # A primary keeps the replication account of a server of the pair
        # even while it is not its replica, so a converge that stops a
        # promotion leaves the replica able to reconnect.
        "hosts": [] if address is None else [str(address)],
    }
    if replication.primary is not None:
        peer["primary"] = replication.primary.value
    return peer


def _primary(primary_id: ResourceId, declared: list[_Declared]) -> dict[str, object]:
    item = next(
        (entry for entry in declared if entry.service.resource_id == primary_id), None
    )
    replication = (
        item.service.mariadb.replication
        if item is not None and item.service.mariadb is not None
        else None
    )
    if item is None or replication is None or replication.listen_address is None:
        message = (
            f"primary {primary_id.value} is no MariaDB primary with a listenAddress"
        )
        raise ValueError(message)
    return {
        "service": primary_id.value,
        "server": item.server.resource_id.value,
        "serverId": replication.server_id.value,
        "host": replication.listen_address.value,
        "port": item.service.bind.port.value,
    }


REPORT_DIRECTORY_VARIABLE = "cloudfall_services_mariadb_report_directory"
"""The ``services.yml`` variable naming where the role reports each outcome."""
_OUTCOMES = frozenset(
    {"refused", "seed", "attach", "demote", "promote", "wait-promotion"}
)
_REPORT_FIELDS = frozenset(
    {
        "service",
        "server",
        "outcome",
        "action",
        "code",
        "message",
        "reason",
        "sourcePosition",
        "replicaPosition",
        "lastAppliedGtid",
    }
)


@dataclass(frozen=True, slots=True)
class ReplicationOutcome:
    """What the role did, or refused, for one MariaDB service of a pair.

    A refusal carries its code and message, which name both positions; a
    promotion its reason and the last GTID it applied. No secret is in it.
    """

    service: str
    server: str
    outcome: str
    details: tuple[tuple[str, str], ...]

    def get(self, field: str) -> str:
        """Return one detail, or ``''``."""
        return dict(self.details).get(field, "")

    def as_dict(self) -> dict[str, str]:
        """Serialize the outcome for a command's answer."""
        return {
            "service": self.service,
            "server": self.server,
            "outcome": self.outcome,
            **dict(self.details),
        }


def read_outcomes(directory: Path) -> tuple[ReplicationOutcome, ...]:
    """Read every outcome the role reported, by service id; strictly."""
    found: list[ReplicationOutcome] = []
    for path in sorted(directory.glob("*.json")):
        content = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(content, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in content.items()
        ):
            message = f"replication report {path.name} is not a mapping of strings"
            raise ValueError(message)
        unknown = set(content) - _REPORT_FIELDS
        outcome = content.get("outcome", "")
        if unknown or outcome not in _OUTCOMES or not content.get("service"):
            message = f"replication report {path.name} is malformed"
            raise ValueError(message)
        found.append(
            ReplicationOutcome(
                service=content["service"],
                server=content.get("server", ""),
                outcome=outcome,
                details=tuple(
                    sorted(
                        (key, value)
                        for key, value in content.items()
                        if key not in {"service", "server", "outcome", "action"}
                    )
                ),
            )
        )
    return tuple(found)


def refusal(outcomes: tuple[ReplicationOutcome, ...]) -> ReplicationOutcome | None:
    """Return the outcome that stopped the converge, if one did."""
    return next((item for item in outcomes if item.outcome == "refused"), None)
