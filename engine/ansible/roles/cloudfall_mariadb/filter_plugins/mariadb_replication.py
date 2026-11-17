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
- ``cloudfall_mariadb_replication_state(stdout)``: the role's state query's
  line as a mapping, ``readOnly`` read as ``1`` or ``0`` whether the server
  said ``ON`` (11.8's enum) or ``1`` (#450); any other value is an error
- ``cloudfall_mariadb_link_peer(link, peers)``: the declared service a link
  replicates from, by the ``server_id`` it reports, else, whatever
  ``server_id`` it reports, by the host and port it names; ``{}`` when it is
  none of them (#440, #446)
- ``cloudfall_mariadb_account_wait(plan, ...)``: a seed or an attach whose
  primary holds no replication account for it yet, and converges later in
  the run, as a wait (#451)
- ``cloudfall_mariadb_source_problems(rows, ...)``: why a replica can not
  follow a source Cloudfall does not manage, from what the preflight read
  there; ``[]`` when it can (S-019-18)

No filter reads the network: what a source's port answers is read on the
replica's own server, by ``files/mariadb_source_probe.py`` (#455), and
handed to ``cloudfall_mariadb_replication_plan``; what an outside source
holds is read by the ``cloudfall_mariadb_source`` module.

Ansible loads this file itself, into ``ansible-playbook``; it imports nothing
from the SDK, so it works whichever interpreter runs it.
"""

from __future__ import annotations

import re
from ipaddress import ip_address
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
# read_only by name (11.8's enum) and by number (its index, or the older
# series' boolean), as read-only "1" or writable "0".
_READ_ONLY = {
    "OFF": "0",
    "0": "0",
    "ON": "1",
    "1": "1",
    "NO_LOCK": "1",
    "2": "1",
    "NO_LOCK_NO_ADMIN": "1",
    "3": "1",
}
_UNSAFE = "service_mariadb_promotion_unsafe"
_UNATTACHABLE = "service_mariadb_replica_unattachable"
_DIVERGED = "service_mariadb_replica_diverged"
# What the probe of a source's port from the replica's server read, as a
# refusal names it; ``open`` and ``refused`` are what it can tell, the
# others are unknown. files/mariadb_source_probe.py prints one of them.
_PROBE_SAYS = {
    "timeout": "timed out",
    "no-route": "found no route to it",
    "failed": "failed with an error other than a refused connection",
}
_PROBE_OUTCOMES = frozenset({"open", "refused", "unresolved", *_PROBE_SAYS})
# A replica's last IO error that only a running source sends back: its login
# refused (access denied, host blocked or not allowed, an authentication
# method it can't use), a TLS handshake that failed, or the source's own
# error reading its binary log. Can't connect (2003) and unknown host (2005)
# are not among them: those say nothing answered.
_SOURCE_ANSWERED = frozenset({1045, 1129, 1130, 1236, 1251, 2026})


def read_only(value: object) -> str:
    """Return ``read_only`` as ``1`` (read-only) or ``0`` (writable).

    MariaDB 11.8 made it an enum, ``OFF``, ``ON``, ``NO_LOCK`` or
    ``NO_LOCK_NO_ADMIN``, which a string context reads by name and a number
    by its index (0 to 3); the older series read ``0`` or ``1`` (#450). Every
    value but ``OFF`` takes no writes from a user without ``READ_ONLY ADMIN``.
    Anything else is an error.
    """
    text = str(value).strip().upper()
    if text not in _READ_ONLY:
        message = f"not a MariaDB read_only value: {value!r}"
        raise ValueError(message)
    return _READ_ONLY[text]


def replication_state(stdout: str) -> dict[str, str]:
    """Parse the role's replication state query, one ``|``-separated line.

    The fields are ``_STATE_FIELDS``, in order; ``readOnly`` is read by
    :func:`read_only`, so it is ``1`` or ``0`` whatever form the server used.
    """
    if not isinstance(stdout, str):
        message = (
            f"the state query's output must be a string, got {type(stdout).__name__}"
        )
        raise TypeError(message)
    values = stdout.strip().split("|")
    if len(values) != len(_STATE_FIELDS):
        message = (
            f"the state query read {len(values)} field(s), not "
            f"{len(_STATE_FIELDS)}: {stdout.strip()!r}"
        )
        raise ValueError(message)
    state = dict(zip(_STATE_FIELDS, values, strict=True))
    state["readOnly"] = read_only(state["readOnly"])
    return state


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
    state = {field: str(value[field]) for field in _STATE_FIELDS}
    state["readOnly"] = read_only(state["readOnly"])
    return state


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        message = f"{name} must be a mapping, got {type(value).__name__}"
        raise TypeError(message)
    return value


def replication_plan(  # noqa: C901, PLR0911, PLR0912, PLR0913 - one decision table, read top down.
    local: dict[str, object],
    *,
    service: str,
    declared: str,
    link: dict[str, object],
    links: dict[str, object],
    peer: dict[str, object],
    other: dict[str, object],
    other_link: dict[str, object],
    other_source: dict[str, object],
    source_probe: str,
) -> dict[str, str]:
    """Decide what replication does on a server; see replication_check.yml.

    ``local`` and ``other`` are this server's and the other server's state
    (``other`` empty when it can't be reached), ``link`` and ``other_link``
    their ``SHOW SLAVE STATUS`` rows, ``peer`` the declared service the other
    server holds, ``other_source`` the declared service ``other_link``
    replicates from as :func:`link_peer` finds it (``{}`` when none),
    ``links`` the pair as rendered for this service, and ``source_probe``
    what this server read at the port of a source that is no declared
    service (``""`` when it did not try).
    """
    if declared not in _DECLARED_ROLES:
        message = f"unknown declared replication role: {declared!r}"
        raise ValueError(message)
    if source_probe and source_probe not in _PROBE_OUTCOMES:
        message = f"unknown source probe outcome: {source_probe!r}"
        raise ValueError(message)
    mine = _state(local, "this server's state", required=True)
    theirs = _state(other, "the other server's state", required=False)
    link = {key: str(value) for key, value in _mapping(link, "link").items()}
    other_link = {
        key: str(value) for key, value in _mapping(other_link, "other link").items()
    }
    peer = _mapping(peer, "peer")
    other_source = _mapping(other_source, "other source")
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
        return _promotion(
            mine=mine,
            theirs=theirs,
            service=service,
            link=link,
            peer=peer,
            positions=positions,
            source_probe=source_probe,
        )
    if declared != "replica":
        return {"action": declared}
    primary = _mapping(links.get("primary") or links.get("source") or {}, "primary")
    if not primary:
        message = f"replica {service} has no rendered primary or source"
        raise ValueError(message)
    # A source Cloudfall does not manage is named by where it is (S-019).
    named = (
        f"primary {primary['service']}"
        if "service" in primary
        else f"source {primary['host']}:{primary['port']}"
    )
    if link:
        if link_host(link.get("Master_Host", "")) == link_host(
            str(primary["host"])
        ) and int(link.get("Master_Port", "0")) == int(str(primary["port"])):
            return act("follow")
        return refuse(
            _UNATTACHABLE,
            f"service {service} replicates from {link.get('Master_Host')}:"
            f"{link.get('Master_Port')}, not from its declared {named} at "
            f"{primary['host']}:{primary['port']}; Cloudfall does not move a "
            "replica to another source. Nothing changed",
        )
    if not reached:
        why = (
            "to attach to it: it holds databases, which are never dropped"
            if holds
            else "to be seeded from it"
        )
        where = f" on {primary['server']}" if "server" in primary else ""
        first = (
            "converge the primary first"
            if "service" in primary
            else "make it reachable from this server"
        )
        return refuse(
            _UNATTACHABLE,
            f"service {service} can not reach its {named}{where} {why}; {first}. "
            "Nothing changed",
        )
    if other_link:
        # It follows this server when it reports this server's server_id, or,
        # while its link has not connected (Master_Server_Id 0, as after a
        # restart), when it names this server's host and port (#451).
        if (
            int(other_link.get("Master_Server_Id", "0") or "0") == int(mine["serverId"])
            or other_source.get("service") == service
        ):
            return act(
                "demote",
                f"{primary['service']} still follows {service}; {service} turns "
                f"read-only now and attaches once {primary['service']} is promoted; "
                "converge again once it is",
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
            f"history, so it can not attach to its {named} and is never "
            "seeded over its data. Nothing was dropped; recreating its volume, "
            "after which it is seeded, is your step",
        )
    unseen = gtid_unseen(mine["binlogState"], theirs["binlogState"])
    if unseen:
        return {
            **refuse(
                _DIVERGED,
                f"service {service} holds GTIDs its {named} "
                f"never saw: {', '.join(unseen)} (its binary log state "
                f"{mine['binlogState']}, the {named.split(maxsplit=1)[0]}'s "
                f"{theirs['binlogState']}). It "
                "diverged, so it is not attached and not emptied; nothing changed. "
                "Recreating its volume, after which it is seeded, is your step",
            ),
            "sourcePosition": theirs["binlogState"],
            "replicaPosition": mine["binlogState"],
        }
    return act("attach")


def account_wait(  # noqa: PLR0913 - the wait's inputs, named.
    plan: dict[str, str],
    *,
    service: str,
    primary: dict[str, object],
    account_hosts: list[object],
    admitted: list[object],
    converged: bool,
) -> dict[str, str]:
    """Turn a seed or an attach whose primary has no account for it yet into a wait.

    ``account_hosts`` are the hosts of ``cloudfall_repl`` on the primary now,
    ``admitted`` the hosts the primary's own declaration gives this replica,
    which its converge creates the account at, and ``converged`` whether no
    converge of the primary's server is left in this run (it already ran, or
    a ``--limit`` leaves it out). A replica that sorts
    before its primary in a run (a former primary declared the replica of
    the server promoted over it) would be refused its login; it waits for
    the primary's converge instead, which creates the account, and attaches
    in the next one (#451). Every other plan is returned as it is: a primary
    that already converged, or whose declaration does not admit this
    replica, still refuses the login, and the link reports it.
    """
    plan = {key: str(value) for key, value in _mapping(plan, "plan").items()}
    primary = _mapping(primary, "primary")
    if not isinstance(account_hosts, list) or not isinstance(admitted, list):
        message = "account hosts and admitted hosts must be lists"
        raise TypeError(message)
    if not isinstance(converged, bool):
        message = f"converged must be a boolean, got {type(converged).__name__}"
        raise TypeError(message)
    held = {str(host) for host in account_hosts}
    missing = [str(host) for host in admitted if str(host) not in held]
    if plan.get("action") not in {"seed", "attach"} or not admitted or not missing:
        return plan
    if converged:
        return plan
    return {
        **plan,
        "action": "wait-primary",
        "reason": (
            f"its primary {primary['service']} on {primary['server']} holds no "
            f"replication account for it at {', '.join(missing)} yet; the converge "
            f"of {primary['server']} creates it, and {service} attaches when you "
            "converge again"
        ),
    }


def link_host(value: object) -> str:
    """Return a host as a link names it, in one form to compare.

    An IP address in its canonical form (``2001:DB8::1`` and ``2001:db8::1``
    are one), a name in lowercase without a trailing dot. A name is never
    resolved: a link to a name matches a peer only by that name.
    """
    if not isinstance(value, str):
        message = f"a host must be a string, got {type(value).__name__}"
        raise TypeError(message)
    text = value.strip()
    try:
        return str(ip_address(text))
    except ValueError:
        return text.lower().removesuffix(".")


def link_peer(link: dict[str, object], peers: list[object]) -> dict[str, object]:
    """Return the declared service a link replicates from, or ``{}``.

    A link names its source's ``server_id`` once its IO thread connected; a
    link that has not (``Master_Server_Id`` 0) names only the host and port
    it was given (#440). So a link whose ``server_id`` is no peer's, 0 or
    not, is matched by those: ``Master_Host`` among a peer's ``addresses``,
    compared by :func:`link_host`, and ``Master_Port`` its ``port``. A link
    to a peer's address and port with a ``server_id`` no peer declares (a
    server whose declared ``server_id`` has not taken effect) is that peer's:
    the promotion reads the server there rather than take it for a source it
    can't read (#446).
    """
    fields = {key: str(value) for key, value in _mapping(link, "link").items()}
    if not isinstance(peers, list):
        message = f"peers must be a list, got {type(peers).__name__}"
        raise TypeError(message)
    declared = [_mapping(peer, "peer") for peer in peers]
    server_id = int(fields.get("Master_Server_Id", "0") or "0")
    by_id = [peer for peer in declared if server_id and peer["serverId"] == server_id]
    if by_id:
        return by_id[0]
    host = link_host(fields.get("Master_Host", ""))
    port = int(fields.get("Master_Port", "0") or "0")
    for peer in declared:
        addresses = peer.get("addresses", [])
        if not isinstance(addresses, list):
            message = f"addresses of peer {peer.get('service')} must be a list"
            raise TypeError(message)
        if (
            host
            and host in {link_host(address) for address in addresses}
            and port == int(str(peer["port"]))
        ):
            return peer
    return {}


def _promotion(  # noqa: PLR0913 - the promotion's inputs, named.
    *,
    mine: dict[str, str],
    theirs: dict[str, str],
    service: str,
    link: dict[str, str],
    peer: dict[str, object],
    positions: dict[str, str],
    source_probe: str,
) -> dict[str, str]:
    """Decide a promotion: a replica declared primary or standalone (S-019-14)."""
    if not peer:
        return _undeclared_promotion(
            mine=mine,
            service=service,
            link=link,
            positions=positions,
            source_probe=source_probe,
        )
    applied = mine["slavePos"] or "(none)"
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
                f"of {service} when its server converges; converge again once it has"
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


def _undeclared_promotion(
    *,
    mine: dict[str, str],
    service: str,
    link: dict[str, str],
    positions: dict[str, str],
    source_probe: str,
) -> dict[str, str]:
    """Decide a promotion over a source that is no declared service (#440, #446).

    Cloudfall can't read that source, so it promotes only on evidence that it
    is stopped: a connection to its port from this replica's own server
    refused, while the replica's link did not hear from it last. The source
    admitted this server while it replicated from it, so a refusal from
    there means nothing listens; one the controller read could be a firewall
    that rejects only the controller (#455). A running link, a last IO error
    only a running source sends, a port that answers, and a probe that can't
    tell (a timeout, no route, a name the server can't resolve) refuse.
    """
    applied = mine["slavePos"] or "(none)"
    host, port = link.get("Master_Host", ""), link.get("Master_Port", "")
    source = f"server_id {link.get('Master_Server_Id')} at {host}:{port}"

    def refuse(why: str, way_out: str) -> dict[str, str]:
        return {
            "action": "refuse",
            "code": _UNSAFE,
            "message": (
                f"service {service} replicates from {source}, no declared MariaDB "
                f"service; {why}. {way_out}, then converge again; nothing changed. "
                f"This replica has applied {applied}"
            ),
            **positions,
        }

    if link.get("Slave_IO_Running") == "Yes":
        return refuse(
            "its link to it is running: Cloudfall can not tell that it is read-only",
            "Stop it",
        )
    io_errno = int(link.get("Last_IO_Errno", "0") or "0")
    if io_errno in _SOURCE_ANSWERED:
        return refuse(
            "its link is not connected, but its last error came from that source "
            f"({io_errno}: {link.get('Last_IO_Error', '')}), so the source is "
            "running and Cloudfall can not tell that it is read-only",
            "Stop it and let this replica's link report that it can not connect",
        )
    if not source_probe:
        message = f"service {service} follows {source} and no probe of its source ran"
        raise ValueError(message)
    if source_probe == "open":
        return refuse(
            "its link is not connected, but this replica's server reaches that "
            "port, so Cloudfall can not tell that the source is stopped or "
            "read-only",
            "Stop it",
        )
    if source_probe != "refused":
        read = (
            f"this replica's server could not resolve {host}"
            if source_probe == "unresolved"
            else "the probe of that port from this replica's server "
            f"{_PROBE_SAYS[source_probe]}"
        )
        return refuse(
            f"its link is not connected and {read}, so Cloudfall can not tell "
            "that the source is stopped",
            f"Let this replica's server reach {host}:{port}, or, once the source "
            "is stopped, remove this replica's link to it (RESET SLAVE ALL)",
        )
    return {
        "action": "promote",
        "reason": (
            f"its source refuses connections on {host}:{port} from this replica's "
            "server"
        ),
        **positions,
    }


_RELEASE = re.compile(r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})")
_GRANT_ON_ALL = re.compile(r"GRANT (.+?) ON \*\.\* TO ")
# Names a server prints for one right, in either release line.
_SAME_RIGHT = {
    "BINLOG MONITOR": {"BINLOG MONITOR", "REPLICATION CLIENT"},
    "REPLICATION SLAVE": {"REPLICATION SLAVE", "REPLICATION REPLICA"},
}


def release(text: object) -> tuple[int, int, int]:
    """Return the ``X.Y.Z`` a server's version string starts with, as numbers."""
    if not isinstance(text, str):
        message = f"a version must be a string, got {type(text).__name__}"
        raise TypeError(message)
    found = _RELEASE.match(text)
    if found is None:
        message = f"not a MariaDB version: {text!r}"
        raise ValueError(message)
    return int(found[1]), int(found[2]), int(found[3])


def source_problems(
    rows: dict[str, object],
    *,
    version: str,
    server_id: int,
    privileges: list[str],
) -> list[str]:
    """Return why a replica can not follow its outside source; ``[]`` when it can.

    ``rows`` is what ``cloudfall_mariadb_source`` read: ``server``, one row
    of ``VERSION()``, ``@@log_bin``, ``@@binlog_format`` and ``@@server_id``,
    and ``grants``, the rows of ``SHOW GRANTS``. The source must be MariaDB,
    write a binary log, row-based, run a release not newer than the
    replica's ``version`` (compared as numbers, never as text), have another
    ``server_id``, and its account hold every one of ``privileges`` on every
    database, as ``cloudfall_repl`` does (S-019-18).
    """
    server = _mapping(rows, "rows").get("server")
    grants = _mapping(rows, "rows").get("grants")
    if not isinstance(server, list) or len(server) != 1 or not isinstance(grants, list):
        message = "the source read lacks its server row or its grants"
        raise ValueError(message)
    text, log_bin, binlog_format, source_id = (str(value) for value in server[0])
    problems: list[str] = []
    if "mariadb" not in text.lower():
        problems.append(
            f"it runs {text}, not MariaDB: a replica follows its source by "
            "MariaDB GTID"
        )
    elif release(text) > release(version):
        problems.append(
            f"it runs MariaDB {text}, newer than this replica's version "
            f"{version}: a replica is never older than its source"
        )
    if log_bin != "1":
        problems.append("it writes no binary log (log_bin is off)")
    elif binlog_format.upper() != "ROW":
        problems.append(
            f"its binary log is {binlog_format}, not row-based (binlog_format = ROW)"
        )
    if int(source_id) == int(server_id):
        problems.append(
            f"its server_id is {source_id}, the serverId this replica declares"
        )
    held: set[str] = set()
    for row in grants:
        found = _GRANT_ON_ALL.match(str(row[0])) if isinstance(row, list) else None
        if found is not None:
            held |= {right.strip().upper() for right in found[1].split(",")}
    missing = [
        right
        for right in privileges
        if "ALL PRIVILEGES" not in held
        and not (_SAME_RIGHT.get(right, {right}) & held)
    ]
    if missing:
        problems.append(
            f"its account lacks {', '.join(missing)} ON *.*, which the seed and "
            "the link need"
        )
    return problems


class FilterModule:
    """The role's replication filters."""

    def filters(self) -> dict[str, Callable[..., object]]:
        """Name the filters for Ansible."""
        return {
            "cloudfall_mariadb_gtid_unseen": gtid_unseen,
            "cloudfall_mariadb_gtid_applied": gtid_applied,
            "cloudfall_mariadb_vertical": vertical,
            "cloudfall_mariadb_replication_state": replication_state,
            "cloudfall_mariadb_link_peer": link_peer,
            "cloudfall_mariadb_source_problems": source_problems,
            "cloudfall_mariadb_replication_plan": replication_plan,
            "cloudfall_mariadb_account_wait": account_wait,
        }
