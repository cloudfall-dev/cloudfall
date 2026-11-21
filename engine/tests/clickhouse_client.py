"""A stand-in ``clickhouse-client`` that keeps a server's accounts in a JSON file.

It answers the reads of the ``cloudfall_clickhouse`` role and of inspect from
its ``system`` tables, runs the statements the role sends the way ClickHouse
would, refuses any other, and logs every argv and the content of every
queries file, so a test can grep them for a password or a hash. It also holds
what a test asks of a server it can't run here: whether a login, a table or a
``CREATE USER`` would be accepted, by ClickHouse's rules for what the role set.
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

ONLY_REPLICATED = "database_replicated_allow_only_replicated_engine"

_SCRIPT = r"""
import json
import re
import sys
from pathlib import Path

STATE = Path(__STATE__)
LOG = Path(__LOG__)
TABLES = {
    "databases": "databases",
    "users": "users",
    "roles": "roles",
    "role_grants": "role_grants",
    "grants": "grants",
    "settings_profiles": "settings_profiles",
    "settings_profile_elements": "settings_profile_elements",
}
NAME = r"([A-Za-z][A-Za-z0-9_]*)"
HEX = r"([0-9a-f]+)"


def fail(code, message):
    print(f"Code: {code}. DB::Exception: {message}", file=sys.stderr)
    return code


def matches(row, condition):
    condition = condition.strip()
    found = re.fullmatch(r"(\w+) IS NOT NULL", condition)
    if found:
        return row.get(found[1]) is not None
    found = re.fullmatch(r"(\w+) = '([^']*)'", condition)
    if found:
        return row.get(found[1]) == found[2]
    found = re.fullmatch(r"(\w+) = (\d+)", condition)
    if found:
        return row.get(found[1], 0) == int(found[2])
    found = re.fullmatch(r"empty\((\w+)\)", condition)
    if found:
        return not row.get(found[1])
    raise ValueError(f"the stand-in reads no condition {condition!r}")


def select(state, query):
    found = re.fullmatch(
        r"SELECT (DISTINCT )?(.+?) FROM system\.(\w+)(?: WHERE (.+?))?"
        r"(?: ORDER BY \w+)? FORMAT (JSONEachRow|TSVRaw)",
        " ".join(query.split()),
    )
    if not found:
        return None
    columns = [item.strip() for item in found[2].split(",")]
    rows = [
        row
        for row in state[TABLES[found[3]]]
        if all(matches(row, part) for part in (found[4] or "").split(" AND ") if part)
    ]
    projected = [{column: row.get(column) for column in columns} for row in rows]
    if found[5] == "TSVRaw":
        values = sorted({str(row[columns[0]]) for row in projected})
        return "".join(f"{value}\n" for value in values)
    return "".join(json.dumps(row) + "\n" for row in projected)


def users(state):
    return {row["name"]: row for row in state["users"]}


def execute(state, statement):
    text = " ".join(statement.split())
    if text == "SELECT version()":
        print(state["version"])
        return 0
    answer = select(state, text)
    if answer is not None:
        sys.stdout.write(answer)
        return 0
    found = re.fullmatch(f"CREATE ROLE {NAME}", text)
    if found:
        if found[1] in [row["name"] for row in state["roles"]]:
            return fail(493, f"role {found[1]} already exists")
        state["roles"].append({"name": found[1]})
        return 0
    found = re.fullmatch(
        f"(CREATE|ALTER) SETTINGS PROFILE {NAME} SETTINGS {NAME} = (\\d+) CONST", text
    )
    if found:
        names = [row["name"] for row in state["settings_profiles"]]
        if (found[1] == "CREATE") == (found[2] in names):
            return fail(493, f"settings profile {found[2]}: {found[1]} refused")
        if found[1] == "CREATE":
            state["settings_profiles"].append({"name": found[2]})
        state["settings_profile_elements"] = [
            row
            for row in state["settings_profile_elements"]
            if row.get("profile_name") != found[2]
        ] + [
            {
                "profile_name": found[2],
                "user_name": None,
                "setting_name": found[3],
                "value": found[4],
                "writability": "CONST",
                "inherit_profile": None,
            }
        ]
        return 0
    found = re.fullmatch(
        f"CREATE DATABASE {NAME} ENGINE = Replicated\\('([^']+)', '\\{{shard\\}}', "
        "'\\{replica\\}'\\) COMMENT '([^']*)'",
        text,
    )
    if found:
        if found[1] in [row["name"] for row in state["databases"]]:
            return fail(82, f"database {found[1]} already exists")
        state["databases"].append(
            {"name": found[1], "engine": "Replicated", "comment": found[3],
             "path": found[2]}
        )
        state.setdefault("tables", {})[found[1]] = []
        return 0
    identified = f"IDENTIFIED WITH sha256_hash BY '{HEX}' SALT '{HEX}'"
    found = re.fullmatch(
        f"CREATE USER {NAME} {identified} HOST LOCAL SETTINGS PROFILE '{NAME}'", text
    )
    if found:
        if found[1] in users(state):
            return fail(493, f"user {found[1]} already exists")
        state["users"].append(
            {"name": found[1], "host_ip": [], "host_names": ["localhost"],
             "host_names_regexp": [], "host_names_like": [],
             "auth": {"hash": found[2], "salt": found[3]}}
        )
        state["settings_profile_elements"].append(
            {"profile_name": None, "user_name": found[1], "setting_name": None,
             "value": None, "writability": None, "inherit_profile": found[4]}
        )
        return 0
    found = re.fullmatch(f"ALTER USER {NAME} {identified}", text)
    if found:
        if found[1] not in users(state):
            return fail(192, f"there is no user {found[1]}")
        users(state)[found[1]]["auth"] = {"hash": found[2], "salt": found[3]}
        return 0
    host = f"HOST (LOCAL SETTINGS PROFILE '{NAME}'|NONE)"
    found = re.fullmatch(f"ALTER USER {NAME} {host}", text)
    if found:
        if found[1] not in users(state):
            return fail(192, f"there is no user {found[1]}")
        user = users(state)[found[1]]
        for key in ("host_ip", "host_names", "host_names_regexp", "host_names_like"):
            user[key] = []
        if found[2] != "NONE":
            user["host_names"] = ["localhost"]
            state["settings_profile_elements"] = [
                row
                for row in state["settings_profile_elements"]
                if row.get("user_name") != found[1]
            ] + [
                {"profile_name": None, "user_name": found[1], "setting_name": None,
                 "value": None, "writability": None, "inherit_profile": found[3]}
            ]
        return 0
    found = re.fullmatch(f"REVOKE ALL ON \\*\\.\\* FROM {NAME}", text)
    if found:
        state["grants"] = [
            row for row in state["grants"] if row["user_name"] != found[1]
        ]
        return 0
    found = re.fullmatch(f"GRANT ALL ON {NAME}\\.\\* TO {NAME}", text)
    if found:
        if found[2] not in users(state):
            return fail(192, f"there is no user {found[2]}")
        state["grants"].append(
            {"user_name": found[2], "database": found[1], "is_partial_revoke": 0}
        )
        return 0
    found = re.fullmatch(f"GRANT {NAME} TO {NAME}", text)
    if found:
        if found[1] not in [row["name"] for row in state["roles"]]:
            return fail(511, f"there is no role {found[1]}")
        if found[2] not in users(state):
            return fail(192, f"there is no user {found[2]}")
        state["role_grants"].append(
            {"user_name": found[2], "granted_role_name": found[1]}
        )
        return 0
    return fail(62, f"the stand-in knows no statement {text!r}")


def main(argv):
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")
    config = Path(argv[argv.index("--config-file") + 1])
    if not config.is_file():
        return fail(210, "no client configuration")
    if "--queries-file" in argv:
        statement = Path(argv[argv.index("--queries-file") + 1]).read_text("utf-8")
        with LOG.open("a", encoding="utf-8") as log:
            log.write(json.dumps(["queried", statement]) + "\n")
    else:
        statement = argv[argv.index("--query") + 1]
    state = json.loads(STATE.read_text(encoding="utf-8"))
    code = execute(state, statement)
    if code == 0:
        STATE.write_text(json.dumps(state), encoding="utf-8")
    return code


sys.exit(main(sys.argv[1:]))
"""


def fresh_state(version: str = "26.8.37.168") -> dict[str, object]:
    """Return what a replica the role just started holds: ClickHouse's own databases.

    ``default`` is not among the users: the role's ``users.d`` removed it, and
    its users are ``cloudfall`` alone, from that file.
    """
    return {
        "version": version,
        "databases": [
            {"name": name, "engine": engine, "comment": ""}
            for name, engine in (
                ("INFORMATION_SCHEMA", "Memory"),
                ("default", "Atomic"),
                ("information_schema", "Memory"),
                ("system", "Atomic"),
            )
        ],
        "users": [
            {
                "name": "cloudfall",
                "host_ip": ["127.0.0.1", "::1"],
                "host_names": [],
                "host_names_regexp": [],
                "host_names_like": [],
                "auth": None,
            }
        ],
        "roles": [],
        "role_grants": [],
        "grants": [],
        "settings_profiles": [{"name": "default"}, {"name": "readonly"}],
        "settings_profile_elements": [],
        "tables": {},
    }


def write_client(directory: Path, state: dict[str, object]) -> tuple[Path, Path, Path]:
    """Write the stand-in, its state and its log; return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "server.json"
    log = directory / "clickhouse-client.log"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    log.write_text("", encoding="utf-8")
    client = directory / "clickhouse-client"
    client.write_text(
        f"#!{sys.executable}\n"
        + _SCRIPT.replace("__STATE__", repr(str(state_path))).replace(
            "__LOG__", repr(str(log))
        ),
        encoding="utf-8",
    )
    client.chmod(0o755)
    return client, state_path, log


def _user(state: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((row for row in state["users"] if row["name"] == name), None)


def logs_in(state: dict[str, Any], name: str, password: str) -> bool:
    """Return whether ClickHouse would accept ``password`` for ``name`` on loopback.

    A user needs a host that admits loopback and a stored hash that is the
    SHA-256 of the password followed by the salt.
    """
    user = _user(state, name)
    if user is None or user.get("auth") is None:
        return False
    if "localhost" not in user["host_names"]:
        return False
    auth = cast("dict[str, str]", user["auth"])
    digest = hashlib.sha256((password + auth["salt"]).encode("utf-8")).hexdigest()
    return digest == auth["hash"]


def _profiles(state: dict[str, Any], name: str) -> list[str]:
    return [
        row["inherit_profile"]
        for row in state["settings_profile_elements"]
        if row.get("user_name") == name
    ]


def only_replicated(state: dict[str, Any], name: str) -> bool:
    """Return whether each profile of ``name`` holds the only-replicated constant."""
    profiles = _profiles(state, name)
    return bool(profiles) and all(
        [
            (row.get("setting_name"), row.get("value"), row.get("writability"))
            for row in state["settings_profile_elements"]
            if row.get("profile_name") == profile
        ]
        == [(ONLY_REPLICATED, "1", "CONST")]
        for profile in profiles
    )


def create_table(
    state: dict[str, Any], user: str, database: str, engine: str
) -> str | None:
    """Return the engine a table ``user`` creates in ``database`` gets, or None.

    ClickHouse needs a grant on the database; in a ``Replicated`` database a
    ``MergeTree`` engine is created as its replicated form, and with the
    only-replicated rule any engine that is not replicated is refused.
    """
    granted = {row["database"] for row in state["grants"] if row["user_name"] == user}
    if database not in granted:
        return None
    found = next(row for row in state["databases"] if row["name"] == database)
    replicated = found["engine"] == "Replicated"
    if (
        replicated
        and engine.endswith("MergeTree")
        and not engine.startswith("Replicated")
    ):
        engine = f"Replicated{engine}"
    if only_replicated(state, user) and not engine.startswith("Replicated"):
        return None
    return engine


def creates_users(state: dict[str, Any], user: str) -> bool:
    """Return whether ``user`` holds ACCESS MANAGEMENT, a global grant."""
    return any(
        row["user_name"] == user and row["database"] is None for row in state["grants"]
    )
