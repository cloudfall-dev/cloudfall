"""Point smartd's DEVICESCAN line at the sender, keeping its options (S-015-3)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

SCAN = re.compile(r"^DEVICESCAN(?:\s|$)")
# The line a host without one gets, the Debian package's own default.
DEFAULT_OPTIONS = ("-d", "removable", "-n", "standby")


def smartd_scan(conf: str, sender: str) -> str:
    """Return the DEVICESCAN line of ``conf`` with its warnings run by ``sender``.

    Every option of the host's line stays; only an ``-M exec`` is replaced, and
    a line without ``-m`` gets ``-m root``, since ``-M exec`` needs a recipient.
    A file without the line gets the Debian default. Applying it to its own
    output returns the same line.
    """
    lines = [line for line in conf.splitlines() if SCAN.match(line)]
    if len(lines) > 1:
        message = "smartd.conf has more than one DEVICESCAN line; keep one"
        raise ValueError(message)
    if not lines:
        options = [*DEFAULT_OPTIONS, "-m", "root", "-M", "exec", sender]
        return " ".join(["DEVICESCAN", *options])
    directive, hash_sign, comment = lines[0].partition("#")
    if directive.rstrip().endswith("\\"):
        message = "smartd.conf's DEVICESCAN line is continued on the next line"
        raise ValueError(message)
    words = directive.split()[1:]
    kept: list[str] = []
    index = 0
    while index < len(words):
        if words[index : index + 2] == ["-M", "exec"]:
            if index + 2 >= len(words):
                message = "smartd.conf's DEVICESCAN line has -M exec without a program"
                raise ValueError(message)
            index += 3
            continue
        kept.append(words[index])
        index += 1
    if "-m" not in kept:
        kept += ["-m", "root"]
    line = " ".join(["DEVICESCAN", *kept, "-M", "exec", sender])
    return f"{line} {hash_sign}{comment}" if hash_sign else line


class FilterModule:
    """The role's filters."""

    def filters(self) -> dict[str, Callable[[str, str], str]]:
        """Name the filters for Ansible."""
        return {"cloudfall_smartd_scan": smartd_scan}
