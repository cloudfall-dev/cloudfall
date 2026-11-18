"""Read a RabbitMQ cluster's membership on the controller (S-023).

``rabbitmq-diagnostics --formatter json cluster_status`` names the members of
the cluster (``disk_nodes``, and ``ram_nodes`` on older nodes) and those that
run (``running_nodes``). A cluster of more than one member has its majority
while more than half of the members run; without it, the node that is left
serves nothing that needs a majority, and the role changes nothing.

Ansible loads this file itself; it imports nothing from the SDK.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable


class ClusterStatusError(ValueError):
    """``cluster_status`` printed something this filter does not read."""


def _names(status: dict[str, object], key: str) -> list[str]:
    raw = status.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        message = f"cluster_status's {key} is not a list of node names"
        raise ClusterStatusError(message)
    return cast("list[str]", raw)


def membership(cluster_status: str) -> dict[str, object]:
    """Return the members, the running members, and whether they hold a majority.

    One member always holds its majority: a service of one node, or the
    first node of three before the others joined.
    """
    try:
        status = json.loads(cluster_status)
    except json.JSONDecodeError as error:
        message = "rabbitmq-diagnostics cluster_status did not print JSON"
        raise ClusterStatusError(message) from error
    if not isinstance(status, dict):
        message = "rabbitmq-diagnostics cluster_status did not print an object"
        raise ClusterStatusError(message)
    typed = cast("dict[str, object]", status)
    members = sorted({*_names(typed, "disk_nodes"), *_names(typed, "ram_nodes")})
    running = sorted(set(_names(typed, "running_nodes")) & set(members))
    return {
        "members": members,
        "running": running,
        "majority": len(members) <= 1 or 2 * len(running) > len(members),
    }


def hosts_lines(nodes: list[dict[str, str]]) -> str:
    """Return ``/etc/hosts`` lines naming each node at its IPv4.

    Each line is the address, the hostname and the short hostname the node's
    name is made of: ``203.0.113.12 h2.example.internal h2``.
    """
    lines = []
    for node in nodes:
        hostname = node["hostname"]
        short = hostname.split(".")[0]
        names = [hostname] if short == hostname else [hostname, short]
        lines.append(" ".join([node["address"], *names]))
    return "\n".join(lines)


class FilterModule:
    """The role's cluster filters."""

    def filters(self) -> dict[str, Callable[..., object]]:
        """Name the filters for Ansible."""
        return {
            "cloudfall_rabbitmq_membership": membership,
            "cloudfall_rabbitmq_hosts_lines": hosts_lines,
        }
