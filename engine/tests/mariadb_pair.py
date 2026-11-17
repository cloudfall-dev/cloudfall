"""Stand-in ``mariadb`` and ``mariadb-dump`` clients of two replicating servers.

The role's replication tasks run for real against them: each server keeps
its state in a JSON file, and a server linked to another pulls that one's
binary log by GTID whenever a client of it runs, the way a replica's threads
would. The model holds what the role relies on and what S-019-12 to S-019-16
are about:

- every write on a server that logs gets the next GTID of domain 0 and its
  ``server_id``; a write by a user other than root is refused while
  ``read_only`` is on; a session with ``sql_log_bin = 0`` logs nothing
- a locked account's flag reads ``true``, as on the older series, or ``1``,
  as on 11.8, and a read of locked accounts finds only the form it names
- a link logs in as ``cloudfall_repl`` at the replica's address only with
  the password whose ``mysql_native_password`` hash the account holds, over
  TLS only, and only when the replica verifies the primary's certificate
  (the address it connects to is a name of it) against the CA that signed it
- a link reports its source's ``server_id`` only while its IO thread is
  connected; before that, ``Master_Server_Id`` is 0
- a link starts after the replica's GTID position in the primary's binary
  log; a position the log does not hold stops it with error 1236, and in
  strict mode a GTID not above the replica's own stops its SQL thread
- the dump stand-in logs in the same way and writes the primary's databases
  and users at its binary log position, as ``--gtid --master-data=2`` does
- the probe stand-in reads a source's port from the replica's own server, as
  the role's probe script does there (#455): a host and port the stand-in
  network routes to a server reads ``open`` while it runs and ``refused``
  once it is stopped; any other is tried for real by the role's own script,
  so a refusal, a timeout and a name that can't be resolved are its own

What the model can't stand in for, a live server's own behaviour, is listed
in the pull request as unverified.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

CA = "project-ca"
"""What the project CA file holds in the model: its identity."""

_COMMON = r"""
import hashlib, json, pathlib, re, sys

STATE = pathlib.Path({state!r})
NETWORK = pathlib.Path({network!r})
LOG = pathlib.Path({log!r})


def load(path):
    return json.loads(pathlib.Path(path).read_text())


def save(path, state):
    pathlib.Path(path).write_text(json.dumps(state, indent=1, sort_keys=True))


def native(password):
    inner = hashlib.sha1(password.encode()).digest()
    return "*" + hashlib.sha1(inner).hexdigest().upper()


def parse(text):
    found = []
    for item in filter(None, (part.strip() for part in text.split(","))):
        domain, server, sequence = (int(part) for part in item.split("-"))
        found.append((domain, server, sequence))
    return found


def text(gtids):
    return ",".join("%d-%d-%d" % gtid for gtid in gtids)


def binlog_state(state):
    return [tuple(int(p) for p in key.split("-")) + (seq,)
            for key, seq in sorted(state["binlogState"].items())]


def binlog_pos(state):
    last = {{}}
    for domain, server, sequence in binlog_state(state):
        if domain not in last or sequence > last[domain][2]:
            last[domain] = (domain, server, sequence)
    return [last[key] for key in sorted(last)]


def current_pos(state):
    last = {{gtid[0]: gtid for gtid in parse(state["slavePos"])}}
    for gtid in binlog_pos(state):
        if gtid[0] not in last or gtid[2] > last[gtid[0]][2]:
            last[gtid[0]] = gtid
    return [last[key] for key in sorted(last)]


def next_gtid(state, domain=0):
    sequences = [gtid[2] for gtid in current_pos(state) if gtid[0] == domain]
    return (domain, state["serverId"], (max(sequences) if sequences else 0) + 1)


def log_event(state, event, gtid=None):
    # Write one event to the binary log, with a new or a replicated GTID.
    if not state["logBin"]:
        return
    gtid = gtid or next_gtid(state)
    event = dict(event, gtid=text([gtid]))
    state["binlog"].append(event)
    state["binlogState"]["%d-%d" % gtid[:2]] = gtid[2]


def apply_event(state, event):
    kind = event["kind"]
    databases = state["databases"]
    if kind == "rows":
        table = databases.setdefault(event["db"], {{}})
        table[event["table"]] = table.get(event["table"], 0) + event["rows"]
    elif kind == "user":
        state["users"][event["account"]] = event["user"]
    else:
        raise SystemExit("unknown event " + kind)


def authenticate(primary, address, user, password, ssl, ca_file, verify, host):
    # Return an error of a login to ``primary``, or None.
    account = primary["users"].get(user + "@" + address)
    if account is None or account.get("locked") or account["hash"] != native(password):
        return (1045, "Access denied for user '%s'@'%s'" % (user, address))
    if account.get("ssl") and not ssl:
        return (1045, "Access denied for '%s'@'%s' (TLS required)" % (user, address))
    if ssl and verify:
        if not ca_file or not pathlib.Path(ca_file).is_file():
            return (2026, "TLS/SSL error: no CA file")
        if pathlib.Path(ca_file).read_text().strip() != primary["ca"]:
            return (2026, "TLS/SSL error: certificate verify failed")
        if host not in primary["certificateNames"]:
            return (2026, "TLS/SSL error: certificate does not match host " + host)
    return None


def resolve(host, port):
    network = load(NETWORK)
    path = network.get("%s:%s" % (host, port))
    if path is None:
        return None, None
    primary = load(path)
    if not primary["running"]:
        return path, None
    return path, primary
"""

_CLIENT = r"""

SYSTEM = {{"information_schema", "mysql", "performance_schema", "sys"}}
session = {{"log_bin": True, "log": True}}
argv = sys.argv[1:]
with LOG.open("a") as handle:
    handle.write(json.dumps(argv) + "\n")
state = load(STATE)
if not state["running"]:
    sys.exit("ERROR 2002 (HY000): Can't connect to local server through socket")


def pump(state):
    # Pull the primary's binary log into a replica whose link runs.
    link = state.get("link")
    if not link or not link["started"]:
        return
    path, primary = resolve(link["host"], link["port"])
    # The source's server_id is learned when the IO thread connects: a link
    # that has not connected reports 0, as after a restart of the replica.
    if primary is None:
        link.update(io="Connecting", ioErrno=2003, masterServerId=0,
                    ioError="Can't connect to server on '%s'" % link["host"])
        return
    error = authenticate(primary, state["address"], link["user"], link["password"],
                         link["ssl"], link["sslCa"], link["verify"], link["host"])
    if error:
        link.update(io="Connecting", ioErrno=error[0], ioError=error[1],
                    masterServerId=0)
        return
    link["masterServerId"] = primary["serverId"]
    position = parse(state["slavePos"])
    events = primary["binlog"]
    start = 0
    for gtid in position:
        index = next((i for i, event in enumerate(events)
                      if parse(event["gtid"])[0] == gtid), None)
        if index is not None:
            start = max(start, index + 1)
        elif gtid not in parse(primary["binlogStart"]):
            link.update(io="No", sql="No", started=False, ioErrno=1236,
                        ioError="Could not find GTID state requested by slave in any "
                                "binlog files; it may have been purged or the slave "
                                "diverged: " + text([gtid]))
            return
    if not position and primary["binlogStart"]:
        link.update(io="No", sql="No", started=False, ioErrno=1236,
                    ioError="the slave has no position and the binary log starts at "
                            + primary["binlogStart"])
        return
    link.update(io="Yes", ioErrno=0, ioError="")
    for event in events[start:]:
        gtid = parse(event["gtid"])[0]
        own = [g for g in current_pos(state) if g[0] == gtid[0]]
        if own and own[0][2] >= gtid[2]:
            link.update(sql="No", started=False, sqlErrno=1950,
                        sqlError="An attempt was made to binlog GTID %s which "
                                 "would create an out-of-order sequence number"
                                 % event["gtid"])
            return
        apply_event(state, event)
        slave = {{g[0]: g for g in parse(state["slavePos"])}}
        slave[gtid[0]] = gtid
        state["slavePos"] = text([slave[key] for key in sorted(slave)])
        log_event(state, event, gtid)
    link["ioPos"] = text(binlog_pos(primary))
    link["sql"] = "Yes"


def write(statement_event):
    if session["log_bin"]:
        log_event(state, statement_event)


def apply(statement):
    statement = " ".join(
        " ".join(line for line in statement.splitlines()
                 if not line.lstrip().startswith("--")).split())
    if not statement:
        return
    if statement == "SET SESSION sql_log_bin = 0":
        session["log_bin"] = False
        return
    if statement == "SET SESSION sql_log_off = 1":
        session["log"] = False
        return
    if statement in ("SET GLOBAL read_only = ON", "SET GLOBAL read_only = OFF"):
        state["readOnly"] = statement.endswith("ON")
        return
    if statement == "STOP SLAVE":
        if state.get("link"):
            state["link"].update(started=False, io="No", sql="No")
        return
    if statement == "RESET SLAVE ALL":
        state["link"] = None
        return
    if statement == "START SLAVE":
        if not state.get("link"):
            sys.exit("ERROR 1200: Misconfigured slave: MASTER_HOST was not set")
        state["link"].update(started=True, ioErrno=0, ioError="", sqlErrno=0,
                             sqlError="", sql="Yes")
        pump(state)
        return
    if statement == "RESET MASTER":
        state.update(binlog=[], binlogState={{}}, binlogStart="")
        return
    match = re.fullmatch(r"SET GLOBAL gtid_binlog_state = '([0-9,-]*)'", statement)
    if match:
        if state["binlog"]:
            sys.exit("ERROR 1948: binlog is not empty")
        state["binlogState"] = {{"%d-%d" % g[:2]: g[2] for g in parse(match[1])}}
        state["binlogStart"] = match[1]
        return
    match = re.fullmatch(r"SET GLOBAL gtid_slave_pos = '([0-9,-]*)'", statement)
    if match:
        if state.get("link") and state["link"]["started"]:
            sys.exit("ERROR 1198: not possible with a running slave")
        state["slavePos"] = match[1]
        return
    if statement.startswith("CHANGE MASTER TO "):
        if session["log"]:
            sys.exit("the link statement was sent in a session that logs statements")
        if state.get("link") and state["link"]["started"]:
            sys.exit("ERROR 1198: not possible with a running slave")
        options = dict(
            (key.strip(), value.strip().strip("'"))
            for key, value in (item.split("=", 1) for item in
                               statement.removeprefix("CHANGE MASTER TO ").split(","))
        )
        state["link"] = {{
            "host": options["MASTER_HOST"], "port": int(options["MASTER_PORT"]),
            "user": options["MASTER_USER"], "password": options["MASTER_PASSWORD"],
            "ssl": options.get("MASTER_SSL") == "1",
            "sslCa": options.get("MASTER_SSL_CA", ""),
            "verify": options.get("MASTER_SSL_VERIFY_SERVER_CERT") == "1",
            "useGtid": options.get("MASTER_USE_GTID", ""),
            "started": False, "io": "No", "sql": "No", "ioErrno": 0, "ioError": "",
            "sqlErrno": 0, "sqlError": "", "masterServerId": 0, "ioPos": "",
        }}
        return
    match = re.fullmatch(
        r"(CREATE USER IF NOT EXISTS|ALTER USER) '([^']+)'@'([^']+)' IDENTIFIED VIA "
        r"(\w+) USING '([^']*)'( REQUIRE SSL)?", statement)
    if match:
        account = match[2] + "@" + match[3]
        user = state["users"].get(account)
        if match[1] == "CREATE USER IF NOT EXISTS" and user is not None:
            return
        user = dict(user or {{"locked": False, "privileges": []}},
                    plugin=match[4], hash=match[5], ssl=bool(match[6]))
        state["users"][account] = user
        write({{"kind": "user", "account": account, "user": user}})
        return
    match = re.fullmatch(r"GRANT (.+) ON \*\.\* TO '([^']+)'@'([^']+)'", statement)
    if match:
        account = match[2] + "@" + match[3]
        user = state["users"][account]
        user["privileges"] = sorted(set(user["privileges"])
                                    | {{p.strip() for p in match[1].split(",")}})
        write({{"kind": "user", "account": account, "user": user}})
        return
    match = re.fullmatch(r"ALTER USER '([^']+)'@'([^']+)' ACCOUNT (LOCK|UNLOCK)",
                         statement)
    if match:
        account = match[1] + "@" + match[2]
        state["users"][account]["locked"] = match[3] == "LOCK"
        write({{"kind": "user", "account": account, "user": state["users"][account]}})
        return
    if statement.startswith("CLOUDFALL-STANDIN-SEED "):
        seed = json.loads(statement.removeprefix("CLOUDFALL-STANDIN-SEED "))
        for name, tables in seed["databases"].items():
            for table, rows in tables.items():
                event = {{"kind": "rows", "db": name, "table": table, "rows": rows}}
                apply_event(state, event)
                write(event)
            state["databases"].setdefault(name, {{}})
        for account, user in seed.get("users", {{}}).items():
            if account not in state["users"]:
                state["users"][account] = user
                write({{"kind": "user", "account": account, "user": user}})
        return
    match = re.fullmatch(r"CLOUDFALL-STANDIN-WRITE (\S+) (\S+) (\S+) (\d+)", statement)
    if match:
        if state["readOnly"] and match[1] != "root":
            sys.exit("ERROR 1290 (HY000): The MariaDB server is running with the "
                     "--read-only option so it cannot execute this statement")
        if match[1] != "root" and match[1] + "@localhost" not in state["users"]:
            sys.exit("ERROR 1045 (28000): Access denied for user " + match[1])
        event = {{"kind": "rows", "db": match[2], "table": match[3],
                  "rows": int(match[4])}}
        apply_event(state, event)
        write(event)
        return
    if statement.startswith("CLOUDFALL-STANDIN-RESTART "):
        settings = pathlib.Path(statement.removeprefix("CLOUDFALL-STANDIN-RESTART "))
        lines = dict(
            (key.strip(), value.strip())
            for key, _, value in (line.partition("=") for line in
                                  settings.read_text().splitlines())
            if key.strip() and not key.startswith(("#", "["))
        )
        state["serverId"] = int(lines.get("server_id", 1))
        state["logBin"] = "log_bin" in lines
        state["readOnly"] = lines.get("read_only") == "ON"
        if state.get("link") and state["link"]["started"]:
            pump(state)
        return
    sys.exit("unknown statement: " + statement)


def listed(name, sql):
    # Whether a SCHEMATA read lists ``name``: not MariaDB's own, and a
    # directory that is no database (#mysql50#lost+found) only if the query
    # does not leave those out.
    if name in SYSTEM:
        return False
    return not (name.startswith("#mysql50#")
                and "SCHEMA_NAME NOT LIKE '#mysql50#%'" in sql)


def answer(sql):
    if "@@global.gtid_binlog_state" in sql:
        databases = [name for name in state["databases"] if listed(name, sql)]
        return ["|".join([
            "1" if state["readOnly"] else "0", str(state["serverId"]),
            text(binlog_pos(state)), text(binlog_state(state)), state["slavePos"],
            text(current_pos(state)), str(len(databases))])]
    match = re.fullmatch(
        r"SELECT CONCAT_WS\('\|', MASTER_GTID_WAIT\('([0-9,-]*)', \d+\), "
        r"@@global.gtid_slave_pos\)", sql)
    if match:
        pump(state)
        waited = "0" if applied(state["slavePos"], match[1]) else "-1"
        return [waited + "|" + state["slavePos"]]
    match = re.fullmatch(r"SELECT MASTER_GTID_WAIT\('([0-9,-]*)', \d+\)", sql)
    if match:
        pump(state)
        return ["0" if applied(state["slavePos"], match[1]) else "-1"]
    if sql == "SELECT @@global.gtid_slave_pos":
        return [state["slavePos"]]
    if sql == "SELECT @@global.gtid_binlog_pos":
        return [text(binlog_pos(state))]
    if sql == "SHOW SLAVE STATUS":
        link = state.get("link")
        if not link:
            return []
        return ["*************************** 1. row ***************************"] + [
            "%32s: %s" % item for item in (
                ("Master_Host", link["host"]), ("Master_User", link["user"]),
                ("Master_Port", link["port"]),
                ("Slave_IO_Running", link["io"] if link["started"] else "No"),
                ("Slave_SQL_Running", link["sql"] if link["started"] else "No"),
                ("Last_IO_Errno", link["ioErrno"]), ("Last_IO_Error", link["ioError"]),
                ("Last_SQL_Errno", link["sqlErrno"]),
                ("Last_SQL_Error", link["sqlError"]),
                ("Master_SSL_Allowed", "Yes" if link["ssl"] else "No"),
                ("Master_SSL_CA_File", link["sslCa"]),
                ("Master_SSL_Verify_Server_Cert", "Yes" if link["verify"] else "No"),
                ("Master_Server_Id", link["masterServerId"]),
                ("Using_Gtid", "Slave_Pos" if link["useGtid"] == "slave_pos" else "No"),
                ("Gtid_IO_Pos", link["ioPos"]),
            )]
    if sql.startswith("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA"):
        return sorted(name for name in state["databases"] if listed(name, sql))
    if sql.startswith("SELECT CONCAT(s.SCHEMA_NAME, '|', COUNT(t.TABLE_NAME))"):
        return ["%s|%d" % (name, len(tables))
                for name, tables in sorted(state["databases"].items())
                if listed(name, sql)]
    match = re.search(r"WHERE Host = '([^']+)' AND User = '([^']+)'", sql)
    if sql.startswith("SELECT CONCAT_WS('|', IFNULL(JSON_VALUE(Priv") and match:
        user = state["users"].get(match[2] + "@" + match[1])
        if user is None:
            return []
        return ["%s|%s|single%s" % (user["plugin"], user["hash"],
                                     ("|1" if user["ssl"] else "|0")
                                     if "ssl_type" in sql else "")]
    match = re.fullmatch(
        r"SELECT Host FROM mysql.global_priv WHERE User = '([^']+)'( AND .*)?", sql)
    if match:
        return [account.split("@", 1)[1]
                for account, user in sorted(state["users"].items())
                if account.split("@", 1)[0] == match[1]
                and (not match[2] or (user["locked"] and locked_read(match[2])))]
    match = re.search(r"WHERE GRANTEE LIKE '{{3}}([^']+)'{{2}}@%'", sql)
    if sql.startswith("SELECT CONCAT(GRANTEE, '|', PRIVILEGE_TYPE)") and match:
        return ["'%s'@'%s'|%s" % (match[1], account.split("@", 1)[1], privilege)
                for account, user in sorted(state["users"].items())
                if account.split("@", 1)[0] == match[1]
                for privilege in user["privileges"]]
    return None


def locked_read(condition):
    # Whether a read of locked accounts finds the server's own form of the
    # flag: 'true' on the older series, 1 on 11.8 (#452).
    match = re.fullmatch(
        r" AND JSON_VALUE\(Priv, '\$\.account_locked'\) (?:= '(\w+)'|IN \(([^)]*)\))",
        condition)
    if match is None:
        sys.exit("an account read of an unknown shape: " + condition)
    accepted = [match[1]] if match[1] else re.findall(r"'(\w+)'", match[2])
    return state["lockedFlag"] in accepted


def applied(replica, source):
    held = {{g[0]: g for g in parse(replica)}}
    for domain, server, sequence in parse(source):
        if domain not in held or held[domain][2] < sequence or (
                held[domain][2] == sequence and held[domain][1] != server):
            return False
    return True


pump(state)
sql = " ".join(argv[argv.index("--execute") + 1].split())
if sql.startswith("source "):
    content = pathlib.Path(sql.removeprefix("source ")).read_text()
    with LOG.open("a") as handle:
        handle.write(json.dumps(["sourced", content]) + "\n")
    for statement in content.split(";\n"):
        apply(statement)
else:
    found = answer(sql)
    if found is None:
        for statement in sql.split(";"):
            apply(statement)
    else:
        sys.stdout.write("".join(line + "\n" for line in found))
save(STATE, state)
"""

_DUMP = r"""

argv = sys.argv[1:]
with LOG.open("a") as handle:
    handle.write(json.dumps(["dump"] + argv) + "\n")
me = load(STATE)
options = {{}}
extra = next(item for item in argv if item.startswith("--defaults-extra-file="))
for line in pathlib.Path(extra.split("=", 1)[1]).read_text().splitlines():
    key, _, value = line.partition("=")
    options[key.strip()] = value.strip()
for flag in ("--single-transaction", "--gtid", "--master-data=2", "--system=users"):
    if flag not in argv:
        sys.exit("the stand-in dump expects " + flag)
path, primary = resolve(options["host"], options["port"])
if primary is None:
    sys.exit("mariadb-dump: Got error: 2003: Can't connect to server")
error = authenticate(primary, me["address"], options["user"], options["password"],
                     "ssl-ca" in options, options.get("ssl-ca"),
                     "ssl-verify-server-cert" in options, options["host"])
if error:
    sys.exit("mariadb-dump: Got error: %d: %s" % error)
account = primary["users"][options["user"] + "@" + me["address"]]
for privilege in ("SELECT", "RELOAD", "BINLOG MONITOR", "SHOW VIEW", "TRIGGER",
                  "EVENT"):
    if privilege not in account["privileges"]:
        sys.exit("mariadb-dump: Got error: 1227: Access denied; you need " + privilege)
names = argv[argv.index("--databases") + 1:] if "--databases" in argv else []
seed = {{
    "databases": {{name: primary["databases"][name] for name in names}},
    "users": {{key: value for key, value in primary["users"].items()
              if not key.startswith(("root@", "mysql@", "mariadb.sys@"))}},
}}
sys.stdout.write(
    "-- MariaDB dump stand-in\n"
    "-- CHANGE MASTER TO MASTER_LOG_FILE='mariadb-bin.000001', MASTER_LOG_POS=4;\n"
    "-- SET GLOBAL gtid_slave_pos='%s';\n"
    "CLOUDFALL-STANDIN-SEED %s;\n" % (text(binlog_pos(primary)), json.dumps(seed))
)
"""


_PROBE = r"""
import runpy

argv = sys.argv[1:]
with LOG.open("a") as handle:
    handle.write(json.dumps(["probe"] + argv) + "\n")
path, source = resolve(argv[0], argv[1])
if path is None:
    runpy.run_path({script!r}, run_name="__main__")
else:
    sys.stdout.write(("open" if source is not None else "refused") + "\n")
"""


def server_state(  # noqa: PLR0913 - one server's settings, each named.
    *,
    address: str,
    names: list[str],
    server_id: int = 1,
    log_bin: bool = False,
    running: bool = True,
    locked_flag: str = "true",
) -> dict[str, object]:
    """Return a freshly installed server: no database, no history, no link.

    ``locked_flag`` is what ``JSON_VALUE(Priv, '$.account_locked')`` returns
    for a locked account: ``true`` on the older series, ``1`` on 11.8 (#452).
    """
    return {
        "lockedFlag": locked_flag,
        "running": running,
        "serverId": server_id,
        "address": address,
        "certificateNames": names,
        "ca": CA,
        "logBin": log_bin,
        "readOnly": False,
        "binlog": [],
        "binlogState": {},
        "binlogStart": "",
        "slavePos": "",
        "databases": {},
        "users": {
            "root@localhost": {"plugin": "unix_socket", "hash": "", "ssl": False,
                               "locked": False, "privileges": ["ALL PRIVILEGES"]},
        },
        "link": None,
    }


def write_server(
    directory: Path, state: dict[str, object], network: Path, probe_script: Path
) -> tuple[Path, Path, Path, Path, Path]:
    """Write one server's stand-ins; return its client, dump, probe, state and log.

    ``probe_script`` is the role's own probe, which the probe stand-in runs
    for a host and port the stand-in network does not route.
    """
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "server.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    log = directory / "client.log"
    log.touch()
    paths = {"state": str(state_path), "network": str(network), "log": str(log)}
    common = f"#!{sys.executable}\n" + _COMMON.format(**paths)
    client = directory / "mariadb"
    client.write_text(common + _CLIENT.format(), encoding="utf-8")
    client.chmod(0o700)
    dump = directory / "mariadb-dump"
    dump.write_text(common + _DUMP.format(), encoding="utf-8")
    dump.chmod(0o700)
    probe = directory / "probe"
    probe.write_text(
        common + _PROBE.format(script=str(probe_script)), encoding="utf-8"
    )
    probe.chmod(0o700)
    return client, dump, probe, state_path, log
