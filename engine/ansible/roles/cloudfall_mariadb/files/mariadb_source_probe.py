"""Try a replication source's port from a replica's own server (S-019-14, #455).

``mariadb_source_probe.py HOST PORT SECONDS`` prints what one connection
attempt to ``HOST:PORT`` read: ``open``, ``refused``, ``timeout``,
``no-route``, ``unresolved`` or ``failed``. Only ``refused`` says nothing
listens there; the others can't tell a stopped source from one this server
can't see (#446).

The role runs it on the replica's server, not on the controller: the source
admitted that server while it replicated from it, so a refused connection
from there means the source's server is not listening, where a firewall
that rejects the controller would read as refused too (#455). It uses the
standard library only, as any Python an Ansible target has does.
"""

from __future__ import annotations

import errno
import socket
import sys

OUTCOMES = ("open", "refused", "timeout", "no-route", "unresolved", "failed")
_NO_ROUTE = frozenset({errno.EHOSTUNREACH, errno.ENETUNREACH})
_ARGUMENTS = 3


def probe_outcome(error: OSError | None) -> str:
    """Name what a connection attempt that raised ``error`` (None: none) read."""
    if error is None:
        return "open"
    if isinstance(error, socket.gaierror):
        return "unresolved"
    if isinstance(error, ConnectionRefusedError):
        return "refused"
    if isinstance(error, TimeoutError):
        return "timeout"
    if error.errno in _NO_ROUTE:
        return "no-route"
    return "failed"


def probe_outcomes(outcomes: list[str]) -> str:
    """Name what the probes of each address of one name read, together.

    One address that answers is ``open``; the name is ``refused`` only when
    every address refused; otherwise the first address that could not tell
    names it, so a refusal at one address never hides what another can't
    read (#446).
    """
    if not outcomes:
        message = "a probe read no address"
        raise ValueError(message)
    if "open" in outcomes:
        return "open"
    return next((each for each in outcomes if each != "refused"), "refused")


def source_probe(host: str, port: int, timeout: int) -> str:
    """Try to connect to ``host:port`` from this server; name what it read.

    Each address the name resolves to is tried, as ``create_connection``
    raises only the last address's error.
    """
    if not isinstance(host, str) or not host:
        message = f"a probe's host must be a non-empty string, got {host!r}"
        raise ValueError(message)
    number = int(port)
    if not 0 < number < 65536:  # noqa: PLR2004 - the port range.
        message = f"not a port: {port!r}"
        raise ValueError(message)
    seconds = int(timeout)
    if seconds <= 0:
        message = f"a probe's timeout must be positive, got {timeout!r}"
        raise ValueError(message)
    try:
        addresses = socket.getaddrinfo(host, number, 0, socket.SOCK_STREAM)
    except socket.gaierror as error:
        return probe_outcome(error)
    outcomes = []
    for family, kind, protocol, _, address in addresses:
        with socket.socket(family, kind, protocol) as connection:
            connection.settimeout(seconds)
            try:
                connection.connect(address)
            except OSError as error:
                outcomes.append(probe_outcome(error))
            else:
                return probe_outcome(None)
    return probe_outcomes(outcomes)


def main(argv: list[str]) -> int:
    """Print what a probe of ``HOST PORT SECONDS`` read."""
    if len(argv) != _ARGUMENTS:
        sys.stderr.write("usage: mariadb_source_probe.py HOST PORT SECONDS\n")
        return 2
    host, port, seconds = argv
    sys.stdout.write(source_probe(host, int(port), int(seconds)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
