"""A stand-in ``mariadb`` client over a JSON file, for replaying the role's tasks.

It answers the exact reads ``cloudfall_mariadb`` makes and applies the
statements it sends, so a replay can converge twice and count the changes.
Every argument and every sourced file is logged, so a test can grep them.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

CLIENT = """\
#!{python}
import json, pathlib, re, sys
state_path = pathlib.Path({state!r})
log = pathlib.Path({log!r})
state = json.loads(state_path.read_text())
argv = sys.argv[1:]
sql = argv[argv.index("--execute") + 1]
with log.open("a") as handle:
    handle.write(json.dumps(argv) + "\\n")
users = state["users"]
# Accounts at another host than localhost, by user@host; their grants by
# user@host|database
remote = state.setdefault("remote", {{}})
remote_grants = state.setdefault("remoteGrants", [])

def lines(values):
    sys.stdout.write("".join(value + "\\n" for value in values))

def account(name, host):
    return users if host == "localhost" else remote, (
        name if host == "localhost" else name + "@" + host)

def apply(statement):
    statement = statement.strip().rstrip(";").strip()
    match = re.fullmatch(
        r"(CREATE USER IF NOT EXISTS|ALTER USER) '([^']+)'@'([^']+)' "
        r"IDENTIFIED VIA (\\w+) USING '([^']*)'( REQUIRE SSL)?", statement)
    if match:
        table, key = account(match[2], match[3])
        user = table.get(key)
        if match[1] == "ALTER USER" and user is None:
            sys.exit("ERROR 1396: no such user")
        if user is None or match[1] == "ALTER USER":
            old = user or {{"locked": False, "managed": False, "ssl": False}}
            table[key] = {{**old, "plugin": match[4], "hash": match[5],
                           "authOr": False}}
            if match[6] or user is None:
                table[key]["ssl"] = bool(match[6])
        return
    match = re.fullmatch(r"ALTER USER '([^']+)'@'([^']+)' ACCOUNT (LOCK|UNLOCK)",
                         statement)
    if match and match[2] != "localhost":
        table, key = account(match[1], match[2])
        table[key]["locked"] = match[3] == "LOCK"
        return
    match = re.fullmatch(r"GRANT '([^']+)' TO '([^']+)'@'([^']+)'", statement)
    if match and match[3] != "localhost":
        assert match[1] in state["roles"], "no such role"
        table, key = account(match[2], match[3])
        table[key]["managed"] = True
        return
    match = re.fullmatch(
        r"GRANT ALL PRIVILEGES ON `([^`]+)`\\.\\* TO '([^']+)'@'([^']+)'", statement)
    if match and match[3] != "localhost":
        assert match[2] + "@" + match[3] in remote, "no such account"
        remote_grants.append(match[2] + "@" + match[3] + "|" + match[1])
        return
    match = re.fullmatch(
        r"CREATE USER '([^']+)'@'localhost' IDENTIFIED VIA unix_socket", statement)
    if match:
        if match[1] in users:
            sys.exit("ERROR 1396: user exists")
        users[match[1]] = {{"plugin": "unix_socket", "hash": "", "locked": False,
                           "managed": False, "authOr": False}}
        return
    match = re.fullmatch(r"ALTER USER '([^']+)'@'localhost' IDENTIFIED VIA unix_socket",
                         statement)
    if match:
        users[match[1]].update(plugin="unix_socket", hash="", authOr=False)
        return
    match = re.fullmatch(r"ALTER USER '([^']+)'@'localhost' ACCOUNT (LOCK|UNLOCK)",
                         statement)
    if match:
        users[match[1]]["locked"] = match[2] == "LOCK"
        return
    match = re.fullmatch(r"CREATE ROLE '([^']+)'", statement)
    if match:
        state["roles"].append(match[1])
        return
    if statement == "INSTALL SONAME 'auth_ed25519'":
        state["ed25519"] = True
        return
    match = re.fullmatch(r"CREATE DATABASE `([^`]+)` CHARACTER SET utf8mb4", statement)
    if match:
        state["databases"].append(match[1])
        return
    match = re.fullmatch(r"GRANT '([^']+)' TO '([^']+)'@'localhost'", statement)
    if match:
        assert match[1] in state["roles"], "no such role"
        users[match[2]]["managed"] = True
        return
    match = re.fullmatch(
        r"GRANT ALL PRIVILEGES ON `([^`]+)`\\.\\* TO '([^']+)'@'localhost'", statement)
    if match:
        state["grants"].append(match[2] + "|" + match[1])
        return
    sys.exit("unknown statement: " + statement)

if sql.startswith("source "):
    text = pathlib.Path(sql.removeprefix("source ")).read_text()
    with log.open("a") as handle:
        handle.write(json.dumps(["sourced", text]) + "\\n")
    for statement in filter(str.strip, text.split(";\\n")):
        apply(statement)
elif "CONCAT_WS" in sql:
    name = re.search(r"User = '([^']+)'", sql)[1]
    table, key = account(name, re.search(r"Host = '([^']+)'", sql)[1])
    user = table.get(key)
    if user is not None:
        lines([user["plugin"] + "|" + user["hash"] + "|"
               + ("or" if user["authOr"] else "single")
               + (("|1" if user.get("ssl") else "|0") if "ssl_type" in sql else "")])
elif "Host NOT IN ('localhost', '')" in sql:
    if "account_locked" in sql:
        lines(key for key, user in remote.items() if user["locked"])
    elif "roles_mapping" in sql:
        lines(key for key, user in remote.items() if user["managed"])
    elif "FROM mysql.db" in sql:
        lines(remote_grants)
    else:
        lines(remote)
elif "unix_socket" in sql and sql.startswith("SELECT"):
    lines(name for name, user in users.items()
          if user["plugin"] == "unix_socket" and not user["authOr"])
elif "account_locked" in sql:
    lines(name for name, user in users.items() if user["locked"])
elif "roles_mapping" in sql:
    lines(name for name, user in users.items() if user["managed"])
elif "Host = ''" in sql:
    lines(state["roles"])
elif "information_schema.PLUGINS" in sql:
    lines(["ed25519"] if state["ed25519"] else [])
elif "information_schema.SCHEMATA" in sql:
    lines(state["databases"])
elif "FROM mysql.db" in sql:
    lines(state["grants"])
elif sql.startswith("SELECT User FROM mysql.global_priv"):
    lines(users)
else:
    apply(sql)
state_path.write_text(json.dumps(state))
"""

SYSTEM_DATABASES = ["information_schema", "mysql", "performance_schema", "sys"]


def fresh_state() -> dict[str, object]:
    """Return a freshly installed server: Debian's accounts, no Cloudfall state."""
    system = {"plugin": "mysql_native_password", "hash": "invalid", "authOr": True}
    return {
        "users": {
            "root": {**system, "locked": False, "managed": False},
            "mysql": {**system, "locked": False, "managed": False},
            "mariadb.sys": {
                "plugin": "mysql_native_password",
                "hash": "",
                "authOr": False,
                "locked": True,
                "managed": False,
            },
        },
        "roles": [],
        "ed25519": False,
        "databases": list(SYSTEM_DATABASES),
        "grants": [],
    }


def write_client(directory: Path, state: dict[str, object]) -> tuple[Path, Path, Path]:
    """Write the stand-in client and its state; return client, state and log."""
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "server.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    log = directory / "client.log"
    log.touch()
    client = directory / "mariadb"
    client.write_text(
        CLIENT.format(python=sys.executable, state=str(state_path), log=str(log)),
        encoding="utf-8",
    )
    client.chmod(0o700)
    return client, state_path, log
