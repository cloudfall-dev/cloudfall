"""Decide a RabbitMQ version change on the controller (S-023, Supported versions).

A change of ``version`` inside a series, or to the next accepted series, is a
rolling change. RabbitMQ does not support downgrades, so an older version than
one a node runs is refused, and so is a series that is neither the one a node
runs nor the next accepted one: the series before it, or one past the next.
``cluster_status`` names the version each running member reports, and
``list_feature_flags`` the state of every feature flag.

Ansible loads this file itself; it imports nothing from the SDK.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-\d+)?$")


class VersionReadError(ValueError):
    """A tool printed a version or a list this filter does not read."""


def _parts(version: str) -> tuple[int, int, int]:
    """Return ``X.Y.Z`` of a version or of its package (``4.4.1-1``)."""
    match = _VERSION.match(version)
    if match is None:
        message = f"{version!r} is not a RabbitMQ version X.Y.Z"
        raise VersionReadError(message)
    return int(match[1]), int(match[2]), int(match[3])


def _series(parts: tuple[int, int, int]) -> tuple[int, int]:
    return parts[0], parts[1]


def _label(series: tuple[int, int]) -> str:
    return f"{series[0]}.{series[1]}"


def version_refusal(installed: list[str], declared: str, accepted: list[str]) -> str:
    """Return why ``declared`` can't replace each ``installed``; empty when it can.

    ``installed`` holds the version each node runs or has installed (empty
    strings are nodes with none), ``accepted`` the series the role installs.
    The next accepted series of an installed one is the lowest accepted
    series above it.
    """
    target = _parts(declared)
    series = sorted(
        {_series(_parts(f"{item}.0")) for item in accepted},
    )
    for item in installed:
        if not item:
            continue
        current = _parts(item)
        if target < current:
            return (
                f"{declared} is older than the {'.'.join(map(str, current))} a "
                "node runs, and RabbitMQ does not support downgrades"
            )
        following = [found for found in series if found > _series(current)]
        allowed = {_series(current), *following[:1]}
        if _series(target) not in allowed:
            return (
                f"{declared} is not of series {_label(_series(current))}, which "
                f"a node runs, nor of the next accepted series"
                f"{' ' + _label(following[0]) if following else ''}: "
                "change the version one series at a time"
            )
    return ""


def _object(text: str, command: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        message = f"{command} did not print JSON"
        raise VersionReadError(message) from error


def node_versions(cluster_status: str) -> dict[str, str]:
    """Return the RabbitMQ version each running member reports, by node name."""
    status = _object(cluster_status, "cluster_status")
    if not isinstance(status, dict):
        message = "cluster_status did not print an object"
        raise VersionReadError(message)
    # A running node always prints the map; without it no member is checked
    raw = cast("dict[str, object]", status).get("versions")
    if not isinstance(raw, dict):
        message = "cluster_status's versions is not an object"
        raise VersionReadError(message)
    found: dict[str, str] = {}
    for node, versions in cast("dict[str, object]", raw).items():
        version = (
            cast("dict[str, object]", versions).get("rabbitmq_version")
            if isinstance(versions, dict)
            else None
        )
        if not isinstance(version, str):
            message = f"cluster_status names no rabbitmq_version for {node}"
            raise VersionReadError(message)
        found[str(node)] = version
    return found


def disabled_stable_flags(feature_flags: str) -> list[str]:
    """Return the stable feature flags that are not enabled, by name."""
    listed = _object(feature_flags, "list_feature_flags")
    if not isinstance(listed, list):
        message = "list_feature_flags did not print a list"
        raise VersionReadError(message)
    found: list[str] = []
    for item in cast("list[object]", listed):
        if not isinstance(item, dict):
            message = "list_feature_flags printed an entry that is not an object"
            raise VersionReadError(message)
        flag = cast("dict[str, object]", item)
        name, state, stability = (
            flag.get("name"),
            flag.get("state"),
            flag.get("stability"),
        )
        if not (
            isinstance(name, str)
            and isinstance(state, str)
            and isinstance(stability, str)
        ):
            message = (
                "list_feature_flags printed an entry without its name, state "
                "and stability"
            )
            raise VersionReadError(message)
        if stability in ("stable", "required") and state != "enabled":
            found.append(name)
    return sorted(found)


class FilterModule:
    """The role's version filters."""

    def filters(self) -> dict[str, Callable[..., object]]:
        """Name the filters for Ansible."""
        return {
            "cloudfall_rabbitmq_version_refusal": version_refusal,
            "cloudfall_rabbitmq_node_versions": node_versions,
            "cloudfall_rabbitmq_disabled_stable_flags": disabled_stable_flags,
        }
