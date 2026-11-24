"""Read the host's listeners for Kibana's port (S-024-3).

Kibana answers on loopback only: the role reads ``ss -Htln`` and refuses a
listener on its port at any other address. Ansible loads this file itself;
it imports nothing from the SDK.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_LOOPBACK = re.compile(r"^(127\.[0-9.]+|\[::1\]|::1|\[::ffff:127\.[0-9.]+\])$")
_LOCAL_ADDRESS_FIELD = 3


def public_listeners(sockets: str, port: int) -> list[str]:
    """Return each local address listening on ``port`` that is not loopback.

    ``sockets`` is ``ss -Htln``'s output, one listener a line, its local
    address and port in the fourth field (``127.0.0.1:5601``,
    ``[::1]:5601``, ``*:5601``).
    """
    found: list[str] = []
    for line in sockets.splitlines():
        fields = line.split()
        if len(fields) <= _LOCAL_ADDRESS_FIELD:
            continue
        local = fields[_LOCAL_ADDRESS_FIELD]
        address, separator, listened = local.rpartition(":")
        if not separator or listened != str(port):
            continue
        # ss may name the interface a listener is bound to: 127.0.0.1%lo:5601
        if not _LOOPBACK.match(address.split("%", 1)[0]):
            found.append(local)
    return found


class FilterModule:
    """The role's filters."""

    def filters(self) -> dict[str, Callable[..., object]]:
        """Name the filters for Ansible."""
        return {"cloudfall_kibana_public_listeners": public_listeners}
