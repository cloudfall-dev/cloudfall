"""GTID sets and replication status for a pair of MariaDB servers (S-019, #423).

MariaDB names a transaction ``domain-server-sequence``. A binary log's state
(``@@gtid_binlog_state``) holds the last GTID of each domain and server that
wrote to it; a position (``@@gtid_binlog_pos``, ``@@gtid_slave_pos``) holds
the last GTID of each domain. In strict mode a domain's sequence numbers only
grow along one history.

- ``cloudfall_mariadb_gtid_unseen(mine, theirs)``: the GTIDs of the binlog
  state ``mine`` that the binlog state ``theirs`` never saw: a domain and
  server ``theirs`` has no GTID of, or whose last sequence there is lower.
  A former primary is attached to a new one only when this is empty: a write
  that reached it alone is a GTID the new primary never saw (S-019-16). It
  compares the sets of each domain and server, not one position
- ``cloudfall_mariadb_gtid_applied(replica, source)``: whether the position
  ``replica`` has applied the position ``source``: each domain of the source
  has, on the replica, the same GTID or a higher sequence (S-019-14)
- ``cloudfall_mariadb_vertical(stdout)``: the first row of ``--vertical``
  output (``SHOW SLAVE STATUS``) as a mapping; ``{}`` with no row

Ansible loads this file itself, into ``ansible-playbook``; it imports nothing
from the SDK, so it works whichever interpreter runs it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_GTID = re.compile(r"(0|[1-9][0-9]{0,9})-(0|[1-9][0-9]{0,9})-(0|[1-9][0-9]{0,19})")
_ROW_MARK = re.compile(r"^\*+ \d+\. row \*+$")


def gtids(text: str) -> list[tuple[int, int, int]]:
    """Parse ``0-1-10,1-2-5`` into ``(domain, server, sequence)``; strictly."""
    if not isinstance(text, str):
        message = f"a GTID list must be a string, got {type(text).__name__}"
        raise TypeError(message)
    found: list[tuple[int, int, int]] = []
    for item in text.replace("\n", ",").split(","):
        value = item.strip()
        if not value:
            continue
        match = _GTID.fullmatch(value)
        if match is None:
            message = f"not a MariaDB GTID: {value!r}"
            raise ValueError(message)
        found.append((int(match[1]), int(match[2]), int(match[3])))
    return found


def _text(gtid: tuple[int, int, int]) -> str:
    return "-".join(str(part) for part in gtid)


def gtid_unseen(mine: str, theirs: str) -> list[str]:
    """Return the GTIDs of the state ``mine`` that the state ``theirs`` lacks."""
    known: dict[tuple[int, int], int] = {}
    for domain, server, sequence in gtids(theirs):
        known[(domain, server)] = max(sequence, known.get((domain, server), 0))
    return [
        _text(gtid)
        for gtid in gtids(mine)
        if known.get((gtid[0], gtid[1]), -1) < gtid[2]
    ]


def gtid_applied(replica: str, source: str) -> bool:
    """Return whether the position ``replica`` has applied the position ``source``."""
    applied = {
        domain: (server, sequence) for domain, server, sequence in gtids(replica)
    }
    for domain, server, sequence in gtids(source):
        if domain not in applied:
            return False
        held_server, held_sequence = applied[domain]
        if held_sequence < sequence:
            return False
        if held_sequence == sequence and held_server != server:
            return False
    return True


def vertical(stdout: str) -> dict[str, str]:
    """Return the first row of ``mariadb --vertical`` output as field to value."""
    if not isinstance(stdout, str):
        message = f"vertical output must be a string, got {type(stdout).__name__}"
        raise TypeError(message)
    row: dict[str, str] = {}
    seen_row = False
    for line in stdout.splitlines():
        stripped = line.strip()
        if _ROW_MARK.fullmatch(stripped):
            if seen_row:
                break
            seen_row = True
            continue
        if not seen_row or not stripped:
            continue
        name, separator, value = stripped.partition(":")
        if not separator:
            message = f"not a field of vertical output: {stripped!r}"
            raise ValueError(message)
        row[name.strip()] = value.strip()
    return row


_DECLARED_ROLES = frozenset({"standalone", "primary", "replica"})
_STATE_FIELDS = (
    "readOnly",
    "serverId",
    "binlogPos",
    "binlogState",
    "slavePos",
    "currentPos",
    "databases",
)
_UNSAFE = "service_mariadb_promotion_unsafe"
_UNATTACHABLE = "service_mariadb_replica_unattachable"
_DIVERGED = "service_mariadb_replica_diverged"


def _state(value: object, name: str, *, required: bool) -> dict[str, str]:
    """Check one server's state as replication_check.yml keeps it."""
    if not isinstance(value, dict):
        message = f"{name} must be a mapping, got {type(value).__name__}"
        raise TypeError(message)
    if not value and not required:
        return {}
    missing = [field for field in _STATE_FIELDS if field not in value]
    if missing:
        message = f"{name} lacks {', '.join(missing)}"
        raise ValueError(message)
    return {field: str(value[field]) for field in _STATE_FIELDS}


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        message = f"{name} must be a mapping, got {type(value).__name__}"
        raise TypeError(message)
    return value


def replication_plan(  # noqa: C901, PLR0911, PLR0913 - one decision table, read top down.
    local: dict[str, object],
    *,
    service: str,
    declared: str,
    link: dict[str, object],
    links: dict[str, object],
    peer: dict[str, object],
    other: dict[str, object],
    other_link: dict[str, object],
) -> dict[str, str]:
    """Decide what replication does on a server; see replication_check.yml.

    ``local`` and ``other`` are this server's and the other server's state
    (``other`` empty when it can't be reached), ``link`` and ``other_link``
    their ``SHOW SLAVE STATUS`` rows, ``peer`` the declared service the other
    server holds, and ``links`` the pair as rendered for this service.
    """
    if declared not in _DECLARED_ROLES:
        message = f"unknown declared replication role: {declared!r}"
        raise ValueError(message)
    mine = _state(local, "this server's state", required=True)
    theirs = _state(other, "the other server's state", required=False)
    link = {key: str(value) for key, value in _mapping(link, "link").items()}
    other_link = {
        key: str(value) for key, value in _mapping(other_link, "other link").items()
    }
    peer = _mapping(peer, "peer")
    links = _mapping(links, "links")
    reached = bool(theirs)
    holds = int(mine["databases"]) > 0
    history = bool(mine["binlogState"] or mine["slavePos"])
    positions = {
        "sourcePosition": theirs.get("binlogPos", ""),
        "replicaPosition": mine["slavePos"],
    }

    def refuse(code: str, message: str, **extra: str) -> dict[str, str]:
        return {
            "action": "refuse",
            "code": code,
            "message": message,
            **positions,
            **extra,
        }

    def act(action: str, reason: str = "") -> dict[str, str]:
        found = {"action": action, **positions}
        if reason:
            found["reason"] = reason
        return found

    if link and declared != "replica":
        return _promotion(mine, theirs, service, link, peer, positions)
    if declared != "replica":
        return {"action": declared}
    primary = _mapping(links.get("primary", {}), "primary")
    if not primary:
        message = f"replica {service} has no rendered primary"
        raise ValueError(message)
    if link:
        if link.get("Master_Host") == primary["host"] and int(
            link.get("Master_Port", "0")
        ) == int(str(primary["port"])):
            return act("follow")
        return refuse(
            _UNATTACHABLE,
            f"service {service} replicates from {link.get('Master_Host')}:"
            f"{link.get('Master_Port')}, not from its declared primary "
            f"{primary['service']} at {primary['host']}:{primary['port']}; "
            "Cloudfall does not move a replica to another source. Nothing changed",
        )
    if not reached:
        why = (
            "to attach to it: it holds databases, which are never dropped"
            if holds
            else "to be seeded from it"
        )
        return refuse(
            _UNATTACHABLE,
            f"service {service} can not reach its primary {primary['service']} on "
            f"{primary['server']} {why}; converge the primary first. Nothing changed",
        )
    if other_link:
        if int(other_link.get("Master_Server_Id", "0")) == int(mine["serverId"]):
            return act(
                "demote",
                f"{primary['service']} still follows {service}; {service} turns "
                f"read-only now and attaches once {primary['service']} is promoted",
            )
        return refuse(
            _UNATTACHABLE,
            f"the primary {primary['service']} of service {service} is itself a "
            f"replica of {other_link.get('Master_Host')}; promote it first. "
            "Nothing changed",
        )
    if not holds and not history:
        return act("seed")
    if not history:
        return refuse(
            _UNATTACHABLE,
            f"service {service} holds {mine['databases']} database(s) and no GTID "
            f"history, so it can not attach to {primary['service']} and is never "
            "seeded over its data. Nothing was dropped; recreating its volume, "
            "after which it is seeded, is your step",
        )
    unseen = gtid_unseen(mine["binlogState"], theirs["binlogState"])
    if unseen:
        return {
            **refuse(
                _DIVERGED,
                f"service {service} holds GTIDs its primary {primary['service']} "
                f"never saw: {', '.join(unseen)} (its binary log state "
                f"{mine['binlogState']}, the primary's {theirs['binlogState']}). It "
                "diverged, so it is not attached and not emptied; nothing changed. "
                "Recreating its volume, after which it is seeded, is your step",
            ),
            "sourcePosition": theirs["binlogState"],
            "replicaPosition": mine["binlogState"],
        }
    return act("attach")


def _promotion(  # noqa: PLR0913 - the promotion's inputs, named.
    mine: dict[str, str],
    theirs: dict[str, str],
    service: str,
    link: dict[str, str],
    peer: dict[str, object],
    positions: dict[str, str],
) -> dict[str, str]:
    """Decide a promotion: a replica declared primary or standalone (S-019-14)."""
    applied = mine["slavePos"] or "(none)"
    if not peer:
        if link.get("Slave_IO_Running") == "Yes":
            return {
                "action": "refuse",
                "code": _UNSAFE,
                "message": (
                    f"service {service} replicates from server_id "
                    f"{link.get('Master_Server_Id')} at {link.get('Master_Host')}, "
                    "no declared MariaDB service, and its link to it is running: "
                    "Cloudfall can not tell that it is read-only. Stop it, then "
                    f"converge again; nothing changed. This replica has applied "
                    f"{applied}"
                ),
                **positions,
            }
        return {
            "action": "promote",
            "reason": "its source can not be reached",
            **positions,
        }
    if not theirs:
        return {
            "action": "promote",
            "reason": "its source can not be reached",
            **positions,
        }
    source = str(peer.get("service"))
    if theirs["readOnly"] == "1" and gtid_applied(
        mine["slavePos"], theirs["binlogPos"]
    ):
        return {
            "action": "promote",
            "reason": f"its source {source} is read-only and applied",
            **positions,
        }
    if (
        theirs["readOnly"] != "1"
        and peer.get("role") == "replica"
        and peer.get("primary") == service
    ):
        return {
            "action": "wait-promotion",
            "reason": (
                f"{source} is still writable; it turns read-only as the replica "
                f"of {service} when its server converges"
            ),
            **positions,
        }
    state = (
        "writable"
        if theirs["readOnly"] != "1"
        else "read-only, and this replica has not applied its position"
    )
    return {
        "action": "refuse",
        "code": _UNSAFE,
        "message": (
            f"service {service} still replicates from {source} on "
            f"{peer.get('server')}, which is running and {state}: the source is at "
            f"GTID {theirs['binlogPos'] or '(none)'}, this replica has applied "
            f"{applied}. Promoting it would drop what it has not applied; stop the "
            f"writers and make {source} read-only (declare it the replica of "
            f"{service}) or stop it, then converge again. Nothing changed and the "
            "replica still replicates"
        ),
        **positions,
    }


class FilterModule:
    """The role's replication filters."""

    def filters(self) -> dict[str, Callable[..., object]]:
        """Name the filters for Ansible."""
        return {
            "cloudfall_mariadb_gtid_unseen": gtid_unseen,
            "cloudfall_mariadb_gtid_applied": gtid_applied,
            "cloudfall_mariadb_vertical": vertical,
            "cloudfall_mariadb_replication_plan": replication_plan,
        }
