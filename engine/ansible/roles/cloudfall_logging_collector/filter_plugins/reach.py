"""Find the directory that keeps a service user from a file it reads (#295)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_OWNER_EXECUTE = 0o100
_GROUP_EXECUTE = 0o010
_OTHER_EXECUTE = 0o001


def parent_directories(path: str) -> list[str]:
    """Return every parent of the absolute ``path``, from ``/`` down."""
    file = PurePosixPath(path)
    if not file.is_absolute():
        message = f"{path!r} is not an absolute path"
        raise ValueError(message)
    return [str(parent) for parent in reversed(file.parents)]


def blocking_parent(
    path: str, user: str, group: str, stats: Sequence[Mapping[str, object]]
) -> dict[str, str] | None:
    """Return the first parent of ``path`` that ``user`` can't traverse.

    ``user`` is the service user that reads the file and ``group`` the
    service's group; ``stats`` are the registered results of an
    ``ansible.builtin.stat`` loop over the parents, each with its ``item``
    (the path) and its ``stat``. The kernel's rule decides: the owner's
    execute bit when the user owns the directory, else the group's when the
    directory's group is the service's, else the other-execute bit. Users
    and groups are compared by name, so a service user its package has not
    created yet is judged by the name it will have. A parent that does not
    exist yet is skipped: the role creates it later with a mode it knows.
    """
    by_path = {str(result["item"]): result for result in stats}
    for directory in parent_directories(path):
        if directory not in by_path:
            message = f"no stat result for {directory}"
            raise ValueError(message)
        stat = by_path[directory]["stat"]
        if not isinstance(stat, Mapping):
            message = f"the stat result for {directory} has no stat"
            raise TypeError(message)
        if not stat["exists"]:
            continue
        mode = int(str(stat["mode"]), 8)
        owner = str(stat.get("pw_name", stat["uid"]))
        owner_group = str(stat.get("gr_name", stat["gid"]))
        if owner == user:
            allowed = mode & _OWNER_EXECUTE
        elif owner_group == group:
            allowed = mode & _GROUP_EXECUTE
        else:
            allowed = mode & _OTHER_EXECUTE
        if not stat["isdir"] or not allowed:
            return {
                "path": directory,
                "mode": f"{mode:04o}",
                "owner": owner,
                "group": owner_group,
            }
    return None


class FilterModule:
    """The role's filters."""

    def filters(self) -> dict[str, Callable[..., object]]:
        """Name the filters for Ansible."""
        return {
            "cloudfall_parent_directories": parent_directories,
            "cloudfall_blocking_parent": blocking_parent,
        }
