"""Plan a ClickHouse service's databases and owners on the controller (S-022).

The play reads, as ``cloudfall`` on the service's home, the server's
databases, users, roles, grants and settings profiles, one JSON row a line,
and hands them here with the declared databases, the controller paths of
their owners' staged hashes, and the digest of each owner's hash the role
last stored. The plan says what to change and nothing else, so a converge
that finds the declared state changes nothing:

- every declared database exists with the ``Replicated`` engine, at the same
  Keeper path on every replica, with Cloudfall's comment; one that exists
  with another engine is refused, never converted
- every owner logs in from loopback with the salted SHA-256 hash the
  controller computed (D-9): only an owner whose hash differs from the one
  last stored gets a statement, staged in a file, never in the plan
- every owner has every privilege on its own database and none elsewhere,
  the settings profile that admits replicated tables only, and Cloudfall's
  role, which names it as Cloudfall's
- a user Cloudfall made that is no longer declared loses its login
  (``HOST NONE``) and is kept, and so is its database with its rows;
  anything Cloudfall did not make is left alone

Ansible loads this file itself; it imports nothing from the SDK, and no
answer or message quotes a password or a hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

REPLICATED = "Replicated"
LOCAL_HOSTS = ["localhost"]
ONLY_REPLICATED_SETTING = "database_replicated_allow_only_replicated_engine"
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
_HASH_AND_SALT = re.compile(r"^[0-9a-f]{64}:[0-9a-f]{32}$")
_LISTEN_PROCESSES = ('"clickhouse',)
_LOOPBACK = re.compile(r"^(127\.[0-9.]+|\[::1\]|::1|\[::ffff:127\.[0-9.]+\]):[0-9]+$")


class StateError(ValueError):
    """What the server answered is not what this plan reads."""


def _name(value: object, description: str) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        message = f"{description} is not a ClickHouse name Cloudfall declares"
        raise ValueError(message)
    return value


def _rows(reads: Mapping[str, object], key: str) -> list[dict[str, object]]:
    raw = reads.get(key)
    if not isinstance(raw, list):
        message = f"the server's {key} were not read"
        raise StateError(message)
    rows: list[dict[str, object]] = []
    for line in raw:
        if not isinstance(line, str) or not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            message = f"a row of the server's {key} is not JSON"
            raise StateError(message) from error
        if not isinstance(row, dict):
            message = f"a row of the server's {key} is not an object"
            raise StateError(message)
        rows.append(row)
    return rows


def hash_file_digest(path: str) -> str:
    """Return the SHA-256 of a staged ``<hash>:<salt>`` file, checking its shape.

    The digest is what the role keeps on the host to tell an unchanged hash
    from a new one; the message never quotes the file.
    """
    content = Path(path).read_text(encoding="utf-8").strip()
    if not _HASH_AND_SALT.match(content):
        message = "a staged owner hash is not <64 hex digits>:<32 hex digits>"
        raise ValueError(message)
    return hashlib.sha256(content.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class _Server:
    """What the server holds, by name."""

    databases: dict[str, dict[str, object]]
    users: dict[str, dict[str, object]]
    roles: set[str]
    role_grants: set[tuple[str, str]]
    grants: dict[str, set[str | None]]
    profiles: set[str]
    profile_elements: list[dict[str, object]]

    @classmethod
    def parse(cls, reads: Mapping[str, object]) -> _Server:
        grants: dict[str, set[str | None]] = {}
        for row in _rows(reads, "grants"):
            database = row.get("database")
            grants.setdefault(str(row["user_name"]), set()).add(
                database if isinstance(database, str) else None
            )
        return cls(
            databases={str(row["name"]): row for row in _rows(reads, "databases")},
            users={str(row["name"]): row for row in _rows(reads, "users")},
            roles={str(row["name"]) for row in _rows(reads, "roles")},
            role_grants={
                (str(row["user_name"]), str(row["granted_role_name"]))
                for row in _rows(reads, "roleGrants")
            },
            grants=grants,
            profiles={str(row["name"]) for row in _rows(reads, "profiles")},
            profile_elements=_rows(reads, "profileElements"),
        )

    def locked(self, user: str) -> bool:
        """Return whether ``user`` may log in from no host (``HOST NONE``)."""
        row = self.users[user]
        return not any(
            row.get(key)
            for key in ("host_ip", "host_names", "host_names_regexp", "host_names_like")
        )

    def local_only(self, user: str) -> bool:
        """Return whether ``user`` logs in from loopback alone (``HOST LOCAL``)."""
        row = self.users[user]
        return row.get("host_names") == LOCAL_HOSTS and not any(
            row.get(key) for key in ("host_ip", "host_names_regexp", "host_names_like")
        )

    def inherits(self, user: str, profile: str) -> bool:
        """Return whether ``user``'s settings are exactly the ``profile``."""
        elements = [
            row for row in self.profile_elements if row.get("user_name") == user
        ]
        return [row.get("inherit_profile") for row in elements] == [profile]

    def profile_current(self, profile: str) -> bool:
        """Return whether ``profile`` holds the one constant setting it needs."""
        elements = [
            row for row in self.profile_elements if row.get("profile_name") == profile
        ]
        return [
            (row.get("setting_name"), str(row.get("value")), row.get("writability"))
            for row in elements
        ] == [(ONLY_REPLICATED_SETTING, "1", "CONST")]


def _owner_statements(
    server: _Server, database: str, owner: str, profile: str, managed_role: str
) -> list[str]:
    """Return what gives an owner its host, profile, grants and role, if any.

    A new owner is created with its host and profile, so only its grants and
    role follow; an owner with a grant on anything but its own database
    loses every grant first.
    """
    statements: list[str] = []
    if owner in server.users and not (
        server.local_only(owner) and server.inherits(owner, profile)
    ):
        statements.append(f"ALTER USER {owner} HOST LOCAL SETTINGS PROFILE '{profile}'")
    granted = server.grants.get(owner, set())
    elsewhere = bool(granted - {database})
    if elsewhere:
        statements.append(f"REVOKE ALL ON *.* FROM {owner}")
    if elsewhere or database not in granted:
        statements.append(f"GRANT ALL ON {database}.* TO {owner}")
    if (owner, managed_role) not in server.role_grants:
        statements.append(f"GRANT {managed_role} TO {owner}")
    return statements


def account_plan(
    reads: Mapping[str, object], declaration: Mapping[str, object]
) -> dict[str, object]:
    """Return what makes the server's databases and owners the declared ones.

    ``reads`` holds each query's output lines (``databases``, ``users``,
    ``roles``, ``roleGrants``, ``grants``, ``profiles``,
    ``profileElements``). ``declaration`` holds ``databases`` (the declared
    ones, with ``name`` and ``owner``), ``ownerFiles`` (each owner's staged
    hash file), ``digests`` (what the role last stored), and the names
    Cloudfall holds: ``user``, ``managedRole``, ``managedComment``,
    ``ownerProfile``, ``databasePath`` and ``systemDatabases``.

    The answer: ``refused``, why the converge must stop before it changes
    anything; ``before``, the statements that make the role, the profile and
    the databases; ``passwords``, each owner whose hash is set (``create``
    when the user is new); ``after``, the statements that set each owner's
    host, profile, grants and role and lock an undeclared one; ``digests``,
    what to keep for the next converge; and ``undeclared`` and
    ``unmanaged``, the names the converge left alone. No statement holds a
    secret.
    """
    server = _Server.parse(reads)
    databases = cast("list[dict[str, object]]", declaration["databases"])
    owner_files = cast("Mapping[str, str]", declaration["ownerFiles"])
    stored = cast("Mapping[str, str]", declaration["digests"])
    account = _name(declaration["user"], "the cloudfall account")
    managed_role = _name(declaration["managedRole"], "the managed role")
    profile = _name(declaration["ownerProfile"], "the owner profile")
    comment = str(declaration["managedComment"])
    path = str(declaration["databasePath"]).rstrip("/")
    system = set(cast("list[str]", declaration["systemDatabases"]))
    declared = {
        _name(item["name"], "a declared database"): _name(
            item["owner"], "a declared owner"
        )
        for item in databases
    }
    owners = set(declared.values())
    refused = [
        f"database {name} exists with the {server.databases[name].get('engine')} "
        "engine, not Replicated: Cloudfall never converts it, and declared it "
        "would look copied and not be. Move its tables to a Replicated database, "
        "or remove it from the declaration. Nothing changed"
        for name in sorted(declared)
        if name in server.databases
        and server.databases[name].get("engine") != REPLICATED
    ]
    before: list[str] = []
    if managed_role not in server.roles:
        before.append(f"CREATE ROLE {managed_role}")
    setting = f"SETTINGS {ONLY_REPLICATED_SETTING} = 1 CONST"
    if profile not in server.profiles:
        before.append(f"CREATE SETTINGS PROFILE {profile} {setting}")
    elif not server.profile_current(profile):
        before.append(f"ALTER SETTINGS PROFILE {profile} {setting}")
    before.extend(
        f"CREATE DATABASE {name} ENGINE = Replicated('{path}/{name}', "
        f"'{{shard}}', '{{replica}}') COMMENT '{comment}'"
        for name in sorted(declared)
        if name not in server.databases
    )
    digests: dict[str, str] = {}
    passwords: list[dict[str, object]] = []
    after: list[str] = []
    for name, owner in sorted(declared.items()):
        if owner not in owner_files:
            message = f"no staged hash for owner {owner} of database {name}"
            raise ValueError(message)
        digests[owner] = hash_file_digest(owner_files[owner])
        exists = owner in server.users
        if not exists or stored.get(owner) != digests[owner]:
            passwords.append({"owner": owner, "database": name, "create": not exists})
        after.extend(_owner_statements(server, name, owner, profile, managed_role))
    managed_users = {
        user
        for user, role in server.role_grants
        if role == managed_role and user in server.users
    }
    undeclared_users = sorted(managed_users - owners - {account})
    after.extend(
        f"ALTER USER {user} HOST NONE"
        for user in undeclared_users
        if not server.locked(user)
    )
    managed_databases = {
        name for name, row in server.databases.items() if row.get("comment") == comment
    }
    return {
        "refused": refused,
        "before": before,
        "passwords": passwords,
        "after": after,
        "digests": digests,
        "undeclared": {
            "databases": sorted(managed_databases - set(declared) - system),
            "users": undeclared_users,
        },
        "unmanaged": {
            "databases": sorted(
                set(server.databases) - managed_databases - set(declared) - system
            ),
            "users": sorted(set(server.users) - managed_users - owners - {account}),
        },
    }


def public_listeners(sockets: str, allowed: list[str] | None = None) -> list[str]:
    """Return each listener of ClickHouse or Keeper on an address that is not loopback.

    ``sockets`` is ``ss -Htlnp``'s output; each answer is its local address
    and process, as ``0.0.0.0:9234 clickhouse-serv``. ``allowed`` names the
    local addresses a replica listens on by design; one replica without
    ``clientAccess`` has none.
    """
    found: list[str] = []
    for line in sockets.splitlines():
        fields = line.split()
        if len(fields) < 4 or not any(  # noqa: PLR2004 - ss's local address
            name in line for name in _LISTEN_PROCESSES
        ):
            continue
        local = fields[3]
        if not _LOOPBACK.match(local) and local not in (allowed or []):
            process = line[line.index('"clickhouse') :].split('"')[1]
            found.append(f"{local} {process}")
    return found


class FilterModule:
    """The role's filters."""

    def filters(self) -> dict[str, Callable[..., object]]:
        """Name the filters for Ansible."""
        return {
            "cloudfall_clickhouse_account_plan": account_plan,
            "cloudfall_clickhouse_public_listeners": public_listeners,
        }
