"""Plan a RabbitMQ node's virtual hosts and owners on the controller (S-023).

The play reads the cluster's definitions with ``rabbitmqctl
export_definitions`` and hands them here with the declared virtual hosts and
the controller paths of their owners' passwords. The plan says what to
change and nothing else, so a converge that finds the declared state changes
nothing:

- each owner's password becomes RabbitMQ's salted SHA-256 hash, computed here
  with the salt the user already has (D-25), or four fresh bytes; only a
  hash that differs is imported, in a definitions file (D-9)
- each declared virtual host exists with ``quorum`` as its default queue type
  and Cloudfall's tags, which name its owner
- each owner may configure, write and read everything in its virtual host
  and has no permission elsewhere, and no tag
- ``guest`` is removed, and so is ``/`` while it holds no queue
- a virtual host Cloudfall tagged that is no longer declared is kept, and
  its owner's password cleared, so it can't log in; anything without the
  tags is left alone

Ansible loads this file itself; it imports nothing from the SDK. Its hash is
the one ``cloudfall.rabbitmq.password_hash`` computes, as a test checks. No
message quotes a password or a hash.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from secrets import token_bytes
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

SALT_BYTES = 4
DIGEST_BYTES = 32
ALGORITHM = "rabbit_password_hashing_sha256"
QUORUM = "quorum"
FULL_ACCESS = ".*"
GUEST = "guest"
DEFAULT_VHOST = "/"


class DefinitionsError(ValueError):
    """The definitions the node exported are not what this plan reads."""


def password_hash(password: str, salt: bytes) -> str:
    """Return RabbitMQ's stored form: base64 of salt and SHA-256(salt + password)."""
    if len(salt) != SALT_BYTES:
        message = f"a RabbitMQ salt is {SALT_BYTES} bytes"
        raise ValueError(message)
    digest = hashlib.sha256(salt + password.encode("utf-8")).digest()
    return base64.b64encode(salt + digest).decode("ascii")


def stored_salt(user: Mapping[str, object] | None) -> bytes | None:
    """Return the salt of a user's stored SHA-256 hash, or None for a fresh one.

    A user that does not exist, has no password, or one hashed with another
    algorithm gets a fresh salt; a SHA-256 hash of a wrong shape is refused.
    """
    if user is None:
        return None
    stored = user.get("password_hash")
    if not isinstance(stored, str) or not stored:
        return None
    if user.get("hashing_algorithm", ALGORITHM) != ALGORITHM:
        return None
    try:
        raw = base64.b64decode(stored, validate=True)
    except binascii.Error as error:
        message = "a user's stored password hash is not base64"
        raise DefinitionsError(message) from error
    if len(raw) != SALT_BYTES + DIGEST_BYTES:
        message = "a user's stored SHA-256 password hash has a wrong size"
        raise DefinitionsError(message)
    return raw[:SALT_BYTES]


def owner_hash(password_file: str, user: Mapping[str, object] | None) -> str:
    """Return the hash of the password in ``password_file`` with the user's salt."""
    password = Path(password_file).read_bytes().decode("utf-8")
    if not password:
        message = "the staged owner password is empty"
        raise ValueError(message)
    salt = stored_salt(user)
    return password_hash(password, token_bytes(SALT_BYTES) if salt is None else salt)


def _records(definitions: Mapping[str, object], key: str) -> list[dict[str, object]]:
    raw = definitions.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        message = f"the exported definitions' {key} are not a list of objects"
        raise DefinitionsError(message)
    return cast("list[dict[str, object]]", raw)


def _tags(raw: object) -> list[str]:
    """Read tags as RabbitMQ exports them: a list, or one comma-separated string."""
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item)]
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def _vhost_metadata(vhost: Mapping[str, object]) -> tuple[list[str], str | None]:
    metadata = vhost.get("metadata")
    source = metadata if isinstance(metadata, dict) else vhost
    queue_type = source.get("default_queue_type", vhost.get("default_queue_type"))
    return _tags(source.get("tags", vhost.get("tags"))), (
        queue_type if isinstance(queue_type, str) else None
    )


@dataclass(frozen=True, slots=True)
class _Cluster:
    """The accounts the node exported, by name."""

    users: dict[str, dict[str, object]]
    vhosts: dict[str, dict[str, object]]
    permissions: dict[tuple[str, str], dict[str, object]]

    @classmethod
    def parse(cls, exported: str) -> _Cluster:
        try:
            definitions = json.loads(exported)
        except json.JSONDecodeError as error:
            message = "rabbitmqctl export_definitions did not print JSON"
            raise DefinitionsError(message) from error
        if not isinstance(definitions, dict):
            message = "the exported definitions are not an object"
            raise DefinitionsError(message)
        return cls(
            users={str(item["name"]): item for item in _records(definitions, "users")},
            vhosts={
                str(item["name"]): item for item in _records(definitions, "vhosts")
            },
            permissions={
                (str(item["user"]), str(item["vhost"])): item
                for item in _records(definitions, "permissions")
            },
        )


def _vhost_command(cluster: _Cluster, name: str, tags: list[str]) -> list[str] | None:
    """Return the command that gives a declared virtual host its metadata, if any."""
    wanted = ["--default-queue-type", QUORUM, "--tags", ",".join(tags)]
    if name not in cluster.vhosts:
        return ["add_vhost", name, *wanted]
    found_tags, queue_type = _vhost_metadata(cluster.vhosts[name])
    if queue_type != QUORUM or set(found_tags) != set(tags):
        return ["update_vhost_metadata", name, *wanted]
    return None


def _owner_import(
    cluster: _Cluster, owner: str, password_file: str
) -> dict[str, object] | None:
    """Return the user to import with its hash, or None when it is current."""
    user = cluster.users.get(owner)
    wanted = owner_hash(password_file, user)
    if (
        user is not None
        and user.get("password_hash") == wanted
        and user.get("hashing_algorithm", ALGORITHM) == ALGORITHM
        and not _tags(user.get("tags"))
    ):
        return None
    return {
        "name": owner,
        "password_hash": wanted,
        "hashing_algorithm": ALGORITHM,
        "tags": [],
        "limits": {},
    }


def _full_access(granted: Mapping[str, object] | None) -> bool:
    return granted is not None and all(
        granted.get(kind) == FULL_ACCESS for kind in ("configure", "write", "read")
    )


def _undeclared(
    cluster: _Cluster,
    declared: Mapping[str, str],
    managed_tag: str,
    owner_tag_prefix: str,
) -> tuple[list[str], list[str]]:
    """Return the tagged virtual hosts no longer declared, and their owners."""
    owners = set(declared.values())
    vhosts: list[str] = []
    users: set[str] = set()
    for name, vhost in sorted(cluster.vhosts.items()):
        found_tags, _ = _vhost_metadata(vhost)
        if name in declared or managed_tag not in found_tags:
            continue
        vhosts.append(name)
        users.update(
            owner
            for tag in found_tags
            if tag.startswith(owner_tag_prefix)
            and (owner := tag.removeprefix(owner_tag_prefix)) not in owners
            and owner in cluster.users
        )
    return vhosts, sorted(users)


def account_plan(exported: str, declaration: Mapping[str, object]) -> dict[str, object]:
    """Return what makes the cluster's accounts the declared ones.

    ``exported`` is ``rabbitmqctl export_definitions -``'s JSON.
    ``declaration`` holds ``vhosts`` (the declared ones, with ``name`` and
    ``owner``), ``ownerFiles`` (each owner's staged password file),
    ``defaultVhostQueues`` (how many queues ``/`` holds), ``managedTag`` and
    ``ownerTagPrefix``. The answer: ``definitions``, the users to import with
    their hashes (empty when every hash is current); ``commands``, each a
    ``rabbitmqctl`` argv after the executable, none holding a secret; and
    ``undeclared`` and ``unmanaged``, the names a converge left alone.
    """
    cluster = _Cluster.parse(exported)
    vhosts = cast("list[dict[str, object]]", declaration["vhosts"])
    owner_files = cast("Mapping[str, str]", declaration["ownerFiles"])
    managed_tag = str(declaration["managedTag"])
    owner_tag_prefix = str(declaration["ownerTagPrefix"])
    default_vhost_queues = int(cast("int", declaration["defaultVhostQueues"]))
    declared = {str(item["name"]): str(item["owner"]) for item in vhosts}
    owners = set(declared.values())
    imported: list[dict[str, object]] = []
    commands: list[list[str]] = []
    for name, owner in declared.items():
        command = _vhost_command(
            cluster, name, [managed_tag, f"{owner_tag_prefix}{owner}"]
        )
        if command is not None:
            commands.append(command)
        if owner not in owner_files:
            message = f"no staged password for owner {owner} of virtual host {name}"
            raise ValueError(message)
        user = _owner_import(cluster, owner, owner_files[owner])
        if user is not None:
            imported.append(user)
        if not _full_access(cluster.permissions.get((owner, name))):
            commands.append(
                ["set_permissions", "--vhost", name, owner, *[FULL_ACCESS] * 3]
            )
    commands.extend(
        ["clear_permissions", "--vhost", vhost, user]
        for user, vhost in sorted(cluster.permissions)
        if user in owners and declared.get(vhost) != user
    )
    undeclared_vhosts, undeclared_users = _undeclared(
        cluster, declared, managed_tag, owner_tag_prefix
    )
    commands.extend(
        ["clear_password", user]
        for user in undeclared_users
        if cluster.users[user].get("password_hash")
    )
    if GUEST in cluster.users:
        commands.append(["delete_user", GUEST])
    remove_default = DEFAULT_VHOST in cluster.vhosts and default_vhost_queues == 0
    if remove_default:
        commands.append(["delete_vhost", DEFAULT_VHOST])
    managed_users = owners | set(undeclared_users) | {GUEST}
    return {
        "definitions": {"users": imported} if imported else {},
        "commands": commands,
        "undeclared": {"vhosts": undeclared_vhosts, "users": undeclared_users},
        "unmanaged": {
            "vhosts": sorted(
                name
                for name in cluster.vhosts
                if name not in declared
                and name not in undeclared_vhosts
                and not (name == DEFAULT_VHOST and remove_default)
            ),
            "users": sorted(set(cluster.users) - managed_users),
        },
    }


_POLICY_LINE = re.compile(r"^\s*(Installed|Candidate):\s*(\S+)\s*$", re.MULTILINE)
_ERLANG_VERSION = re.compile(r"^(?:[0-9]+:)?([0-9]+)\.([0-9]+)")
_LISTEN_PROCESSES = ('"beam.smp"', '"epmd"', '"erl_child_setup"', '"inet_gethost"')
_LOOPBACK = re.compile(r"^(127\.[0-9.]+|\[::1\]|::1|\[::ffff:127\.[0-9.]+\]):[0-9]+$")


def erlang_release(policy: str) -> str:
    """Return the Erlang ``major.minor`` ``apt-cache policy erlang-base`` names.

    The installed version wins, then the candidate; an empty string when
    there is neither, so the role refuses the host. ``1:27.3.4.1+dfsg-1``
    reads as ``27.3``.
    """
    found = dict(_POLICY_LINE.findall(policy))
    for field in ("Installed", "Candidate"):
        version = found.get(field, "(none)")
        match = _ERLANG_VERSION.match(version)
        if version != "(none)" and match is not None:
            return f"{int(match[1])}.{int(match[2])}"
    return ""


def erlang_supported(release: str, minimum: str) -> bool:
    """Return whether ``release`` is ``minimum`` or later, both ``major.minor``."""
    if not release:
        return False
    have = tuple(int(part) for part in release.split("."))
    need = tuple(int(part) for part in minimum.split("."))
    return have >= need


def public_listeners(sockets: str, allowed: list[str] | None = None) -> list[str]:
    """Return each listener of RabbitMQ or epmd on an address that is not loopback.

    ``sockets`` is ``ss -Htlnp``'s output; each answer is its local address
    and process, as ``0.0.0.0:4369 epmd``. ``allowed`` names the local
    addresses a node listens on by design, all on its own IPv4: node
    discovery and the inter-node port of a node of three
    (``203.0.113.11:4369``), and the TLS client listener of ``clientAccess``
    (``203.0.113.11:5671``).
    """
    found: list[str] = []
    for line in sockets.splitlines():
        fields = line.split()
        processes = [name for name in _LISTEN_PROCESSES if name in line]
        if len(fields) < 4 or not processes:  # noqa: PLR2004 - ss's local address
            continue
        local = fields[3]
        if not _LOOPBACK.match(local) and local not in (allowed or []):
            found.append(f"{local} {processes[0].strip(chr(34))}")
    return found


class FilterModule:
    """The role's filters."""

    def filters(self) -> dict[str, Callable[..., object]]:
        """Name the filters for Ansible."""
        return {
            "cloudfall_rabbitmq_account_plan": account_plan,
            "cloudfall_rabbitmq_erlang_release": erlang_release,
            "cloudfall_rabbitmq_erlang_supported": erlang_supported,
            "cloudfall_rabbitmq_public_listeners": public_listeners,
        }
