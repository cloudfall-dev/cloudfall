"""Decide which RabbitMQ memberships a converge removes (S-023, #510).

The converge removes a membership in two cases only. A member that is no
longer declared and that the cluster does not see running is a lost server
an operator replaced by naming another (``forget_cluster_node`` on the home).
A declared node whose data directory is empty while the cluster still holds
its membership came back on a new volume: its old membership is removed on a
running node so it joins as a new one. A node that holds data is never
forgotten, and neither is a node the cluster sees running.

Ansible loads this file itself; it imports nothing from the SDK.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

# The lines of the cluster settings that only a node's first start reads
NODE_LIST_PREFIX = "cluster_formation.classic_config.nodes."


class MembershipReadError(ValueError):
    """``cluster_status`` printed something this filter does not read."""


def _members(cluster_status: str) -> tuple[list[str], list[str]]:
    """Return the members of the cluster and those that run."""
    try:
        status = json.loads(cluster_status)
    except json.JSONDecodeError as error:
        message = "rabbitmq-diagnostics cluster_status did not print JSON"
        raise MembershipReadError(message) from error
    if not isinstance(status, dict):
        message = "rabbitmq-diagnostics cluster_status did not print an object"
        raise MembershipReadError(message)
    typed = cast("dict[str, object]", status)
    found: dict[str, list[str]] = {}
    for key in ("disk_nodes", "ram_nodes", "running_nodes"):
        raw = typed.get(key, [])
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            message = f"cluster_status's {key} is not a list of node names"
            raise MembershipReadError(message)
        found[key] = cast("list[str]", raw)
    members = sorted({*found["disk_nodes"], *found["ram_nodes"]})
    return members, sorted(set(found["running_nodes"]))


def forgettable(cluster_status: str, declared: list[str]) -> list[str]:
    """Return the members that are neither declared nor running, by node name."""
    members, running = _members(cluster_status)
    return [node for node in members if node not in declared and node not in running]


def rejoin_plan(answers: list[dict[str, object]], node: str) -> dict[str, object]:
    """Return where an empty ``node`` finds the cluster, and whether to forget it.

    ``answers`` are the registered results of ``cluster_status`` read on each
    other declared node, the home first; the first that answered is the
    cluster. ``forget`` holds when that cluster lists ``node`` as a member it
    does not see running: the membership the node left on its old volume.
    """
    for answer in answers:
        if answer.get("skipped") or answer.get("unreachable"):
            continue
        if answer.get("rc") != 0:
            continue
        members, running = _members(str(answer.get("stdout", "")))
        return {
            "peer": str(answer.get("item")),
            "members": members,
            "forget": node in members and node not in running,
        }
    return {"peer": "", "members": [], "forget": False}


def without_node_list(settings: str) -> str:
    """Return the cluster settings without the node list a running node ignores."""
    return "\n".join(
        line
        for line in settings.splitlines()
        if not line.strip().startswith(NODE_LIST_PREFIX)
    )


class FilterModule:
    """The role's membership filters."""

    def filters(self) -> dict[str, Callable[..., object]]:
        """Name the filters for Ansible."""
        return {
            "cloudfall_rabbitmq_forgettable": forgettable,
            "cloudfall_rabbitmq_rejoin_plan": rejoin_plan,
            "cloudfall_rabbitmq_without_node_list": without_node_list,
        }
