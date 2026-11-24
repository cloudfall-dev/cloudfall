"""Read the nodes of Kibana's cluster before its package changes (S-024-9).

Elastic supports no Kibana newer than a node of its cluster, so the role
reads every node's version (``GET /_nodes``) and keeps Kibana as it is while
one is older than the declared version. Ansible loads this file itself; it
imports nothing from the SDK.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _parsed(version: object) -> tuple[int, int, int]:
    """Return a node or service version as three numbers; refuse anything else."""
    found = _VERSION.match(version) if isinstance(version, str) else None
    if found is None:
        message = f"{version!r} is not a version of the form 9.2.4"
        raise ValueError(message)
    major, minor, patch = (int(part) for part in found.groups())
    return major, minor, patch


def older_nodes(answer: object, version: str) -> list[str]:
    """Return ``name version`` of each node older than ``version``, by name.

    ``answer`` is the JSON of ``GET /_nodes?filter_path=nodes.*.name,
    nodes.*.version``: ``{"nodes": {"<id>": {"name": ..., "version": ...}}}``.
    An answer without a node, or a node without a name or version, is
    refused: the role never decides on a cluster it could not read.
    """
    wanted = _parsed(version)
    nodes = answer.get("nodes") if isinstance(answer, dict) else None
    if not isinstance(nodes, dict) or not nodes:
        message = "the cluster answered GET /_nodes with no node"
        raise ValueError(message)
    found: list[str] = []
    for node in nodes.values():
        name = node.get("name") if isinstance(node, dict) else None
        if not isinstance(name, str) or not name:
            message = "the cluster answered GET /_nodes with a node without a name"
            raise ValueError(message)
        running = node.get("version")
        if _parsed(running) < wanted:
            found.append(f"{name} {running}")
    return sorted(found)


class FilterModule:
    """The role's filters."""

    def filters(self) -> dict[str, Callable[..., object]]:
        """Name the filters for Ansible."""
        return {"cloudfall_kibana_older_nodes": older_nodes}
