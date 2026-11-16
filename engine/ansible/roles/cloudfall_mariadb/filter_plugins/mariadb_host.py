"""The host part of an owner's account for one allowed source (S-019, Access).

With ``remoteAccess`` an owner has one account per allowed source, each
requiring TLS. MariaDB matches an IPv4 network in its netmask form,
``10.0.0.0/255.255.255.0``, and one address as itself. It has no netmask form
for IPv6, so an IPv6 source is an account only when it is one address; any
other gives an empty string, which the role refuses before it changes
anything.

Ansible loads this file itself, into ``ansible-playbook``; it imports nothing
from the SDK, so it works whichever interpreter runs it. Its answer is the
one ``cloudfall.mariadb.account_host`` gives, as a test checks.
"""

from __future__ import annotations

from ipaddress import IPv4Network, ip_network
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def mariadb_host(source: str) -> str:
    """Return the account host of ``source``, or ``''`` when MariaDB has none."""
    if not isinstance(source, str):
        message = f"an allowed source must be a string, got {type(source).__name__}"
        raise TypeError(message)
    network = ip_network(source, strict=True)
    if network.num_addresses == 1:
        return str(network.network_address)
    if isinstance(network, IPv4Network):
        return f"{network.network_address}/{network.netmask}"
    return ""


class FilterModule:
    """The role's filters."""

    def filters(self) -> dict[str, Callable[[str], str]]:
        """Name the filters for Ansible."""
        return {"cloudfall_mariadb_host": mariadb_host}
