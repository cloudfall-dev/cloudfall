"""Hand declared config to Ansible as data, never as a template.

Ansible templates every string it reads from an inventory source or extra
vars, so a declared value holding ``{{ ... }}`` would run as a Jinja
expression on the management host. Every string Cloudfall writes for
Ansible is marked unsafe in the JSON form Ansible's loader reads
(``{"__ansible_unsafe": ...}``), so it reaches its target as written: a
literal ``{{`` in a unit file or a check's argv.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

UNSAFE_KEY = "__ansible_unsafe"


def as_ansible_data(value: object) -> object:
    """Return ``value`` with every string marked unsafe, keys left as they are.

    Mappings and sequences are copied with their strings marked; numbers,
    booleans and null pass through. Keys are not templated by Ansible, and
    JSON keys cannot carry the marker.
    """
    if isinstance(value, str):
        return {UNSAFE_KEY: value}
    if isinstance(value, Mapping):
        return {key: as_ansible_data(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [as_ansible_data(item) for item in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    message = f"cannot hand {type(value).__name__} to Ansible as data"
    raise TypeError(message)


def unmarked(value: dict[str, object]) -> object:
    """Read a marked string back as text; a ``json.loads`` ``object_hook``.

    Cloudfall code that reads a document written for Ansible sees the
    values as Ansible's loader does.
    """
    if list(value) == [UNSAFE_KEY]:
        return value[UNSAFE_KEY]
    return value
