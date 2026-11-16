"""MariaDB dumps and the restore drill (S-019-8, S-019-9, #422).

The rendered ``cloudfall-mariadb-backup`` and ``cloudfall-mariadb-restore-check``
run for real against stand-ins for ``mariadb-dump`` and ``mariadb`` that keep
the server in a JSON file. The stand-in dump writes extended inserts with the
escapes ``mariadb-dump`` uses, and the stand-in load parses them on its own,
so the count file the backup derives is checked against an independent
reading. What only a live server proves (the snapshot, the binary log, the
socket login) is listed in the pull request as unverified.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_mariadb"
BACKUP_PLAYBOOK = ROOT / "engine" / "ansible" / "playbooks" / "backup.yml"
SCRATCH = "cloudfall-restore-check"
MARKER = "cloudfall restore drill"
DAY = 24 * 60 * 60

STAND_IN = """\
#!{python}
import json, pathlib, re, sys
state_path = pathlib.Path({state!r})
log = pathlib.Path({log!r})
state = json.loads(state_path.read_text())
tool = pathlib.Path(sys.argv[0]).name
argv = sys.argv[1:]
with log.open("a") as handle:
    handle.write(json.dumps([tool, *argv]) + "\\n")
databases = state["databases"]

def quote_name(name):
    return "`" + name.replace("`", "``") + "`"

def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    escaped = (value.replace("\\\\", "\\\\\\\\").replace("'", "\\\\'")
               .replace("\\n", "\\\\n").replace("\\r", "\\\\r")
               .replace('"', '\\\\"'))
    return "'" + escaped + "'"

def dump(name):
    database = databases[name]
    out = ["-- MariaDB dump 10.19  Distrib 11.8.9-MariaDB", "",
           "/*!40101 SET NAMES utf8mb4 */;"]
    for table, rows in database["tables"].items():
        out += ["", "DROP TABLE IF EXISTS " + quote_name(table) + ";",
                "CREATE TABLE " + quote_name(table) + " (",
                "  `id` int(11) NOT NULL,", "  PRIMARY KEY (`id`)",
                ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;", "",
                "LOCK TABLES " + quote_name(table) + " WRITE;"]
        for start in range(0, len(rows), 2):
            chunk = rows[start:start + 2]
            tuples = ",".join("(" + ",".join(map(literal, row)) + ")"
                              for row in chunk)
            # A table with an invisible column is dumped with its column list
            columns = " (`id`,`value`)" if table in database.get("listed", []) else ""
            out.append("INSERT INTO " + quote_name(table) + columns + " VALUES "
                       + tuples + ";")
        out.append("UNLOCK TABLES;")
    if database.get("routines"):
        # Trigger and routine bodies are written as their author wrote them
        out += ["DELIMITER ;;",
                "/*!50003 CREATE*/ /*!50017 DEFINER=`root`@`localhost`*/"
                " /*!50003 TRIGGER `copy` AFTER INSERT ON `customers` FOR EACH ROW",
                "INSERT INTO `empty` VALUES (NEW.id),(NEW.id + 1) */;;",
                "DELIMITER ;",
                "DELIMITER ;;",
                "CREATE DEFINER=`root`@`localhost` PROCEDURE `archive`()",
                "BEGIN",
                "CREATE TABLE `archive_tmp` (",
                "  `id` int",
                ");",
                "END ;;",
                "DELIMITER ;"]
    out += ["", "/*!50001 CREATE VIEW `recent` AS SELECT", " 1 AS `id` */;",
            "", "-- Dump completed"]
    sys.stdout.write("\\n".join(out) + "\\n")

def tuples(values):
    # An independent reading of a VALUES list: walk it, honour quotes and escapes.
    count, depth, quoted, escaped = 0, 0, False, False
    for char in values:
        if escaped:
            escaped = False
        elif quoted:
            if char == "\\\\":
                escaped = True
            elif char == "'":
                quoted = False
        elif char == "'":
            quoted = True
        elif char == "(":
            if depth == 0:
                count += 1
            depth += 1
        elif char == ")":
            depth -= 1
    return count

def load(name, text):
    if state.get("load_fails"):
        sys.exit("ERROR 1064 (42000) at line 12: You have an error in your SQL syntax")
    tables = databases[name]["tables"]
    body = False
    for line in text.splitlines():
        # A trigger or routine body is stored, not run, by the load
        if line in ("DELIMITER ;;", "DELIMITER ;"):
            body = line == "DELIMITER ;;"
            continue
        if body:
            continue
        match = re.match(r"CREATE TABLE `((?:[^`]|``)+)` \\($", line)
        if match:
            tables[match[1].replace("``", "`")] = 0
            continue
        match = re.match(
            r"INSERT INTO `((?:[^`]|``)+)`(?: \\((?:`(?:[^`]|``)*`,?)+\\))?"
            r" VALUES (.*);$", line)
        if match:
            tables[match[1].replace("``", "`")] += tuples(match[2])
    if state.get("lose_row"):
        first = sorted(tables)[0]
        tables[first] -= 1

if tool == "mariadb-dump":
    dump(argv[-1])
    sys.exit(0)

positional = [arg for arg in argv if not arg.startswith("--")]
if "--execute" in argv:
    sql = argv[argv.index("--execute") + 1]
    positional = [arg for arg in positional if arg != sql]
else:
    sql = None
database = positional[-1] if positional else None

def write():
    state_path.write_text(json.dumps(state))

if sql is None:
    text = sys.stdin.read()
    if "information_schema.TABLES" in text:
        for table in sorted(databases[database]["tables"]):
            print("SELECT '" + table.replace("'", "\\\\'") + "', COUNT(*) FROM "
                  + quote_name(table) + ";")
    elif text.startswith("SELECT '"):
        for line in text.splitlines():
            match = re.fullmatch(r"SELECT '(.*)', COUNT\\(\\*\\) FROM `(.*)`;", line)
            table = match[2].replace("``", "`")
            rows = databases[database]["tables"][table]
            print(table + "\\t" + str(rows if isinstance(rows, int) else len(rows)))
    else:
        load(database, text)
        write()
    sys.exit(0)
if "information_schema.SCHEMATA" in sql and "SCHEMA_NAME NOT IN" in sql:
    # A replica lists the databases it holds, but those the query excludes:
    # the names NOT IN lists, and those a NOT LIKE 'prefix%' matches
    names, _, rest = sql.split("NOT IN", 1)[1].partition(")")
    excluded = re.findall(r"'([^']+)'", names)
    prefixes = [like.removesuffix("%")
                for like in re.findall(r"NOT LIKE '([^']+%)'", rest)]
    for name in sorted(databases):
        if name not in excluded and not name.startswith(tuple(prefixes)):
            print(name)
elif "information_schema.SCHEMATA" in sql:
    name = re.search(r"SCHEMA_NAME = '([^']+)'", sql)[1]
    if name in databases:
        print("comment:" + databases[name].get("comment", ""))
elif "ENGINE" in sql:
    name = re.search(r"TABLE_SCHEMA = '([^']+)'", sql)[1]
    for table, engine in sorted(databases[name].get("engines", {{}}).items()):
        if engine != "InnoDB":
            print(table + " (" + engine + ")")
else:
    match = re.fullmatch(r"CREATE DATABASE `([^`]+)` COMMENT '([^']*)'", sql)
    if match:
        if match[1] in databases:
            sys.exit("ERROR 1007 (HY000): Can't create database; database exists")
        databases[match[1]] = {{"tables": {{}}, "comment": match[2]}}
    else:
        match = re.fullmatch(r"DROP DATABASE `([^`]+)`", sql)
        if not match:
            sys.exit("unknown statement: " + sql)
        del databases[match[1]]
    write()
"""


def _source() -> dict[str, Any]:
    """Return a server holding crm, with the strings a naive count would split."""
    return {
        "databases": {
            "crm": {
                "tables": {
                    "customers": [
                        [1, "Ada"],
                        [2, "Grace),(Hopper"],
                        [3, "it's \\ a back),(slash"],
                        [4, "two\nlines), ("],
                        [5, None],
                    ],
                    "Order Lines": [[1, 1], [2, 1], [3, 2]],
                    "odd`name": [[1, "x"]],
                    "empty": [],
                },
                "engines": {"customers": "InnoDB", "Order Lines": "InnoDB"},
            },
            "billing": {
                "tables": {"invoices": [[1, 100], [2, 250]]},
                "engines": {"invoices": "InnoDB"},
            },
        }
    }


EXPECTED_CRM = "Order Lines\t3\ncustomers\t5\nempty\t0\nodd`name\t1\n"


def _render(
    template: str, directory: Path, *names: str, replica: bool = False
) -> str:
    environment = Environment(
        loader=FileSystemLoader(ROLE / "templates"),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - shell scripts are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    return environment.get_template(template).render(
        cloudfall_mariadb_no_database_pattern=defaults[
            "cloudfall_mariadb_no_database_pattern"
        ],
        cloudfall_mariadb_service={
            "id": "mariadb-main",
            "serviceKind": "mariadb",
            "mariadb": {
                "databases": [
                    {"name": name, "application": name, "owner": name}
                    for name in names
                ],
                **(
                    {
                        "replication": {
                            "role": "replica",
                            "serverId": 2,
                            "primary": "mariadb-main",
                        }
                    }
                    if replica
                    else {}
                ),
            },
            "backup": {
                "directory": str(directory),
                "onCalendar": "*-*-* 02:30:00 UTC",
                "restoreCheckOnCalendar": "*-*-* 05:15:00 UTC",
                "retentionDays": 14,
            },
        }
    )


class Host:
    """A backup directory, the stand-in server and the two rendered scripts."""

    def __init__(
        self, tmp_path: Path, *names: str, replica: bool = False
    ) -> None:
        """Write the stand-ins and render the scripts for ``names``."""
        self.tmp_path = tmp_path
        self.backups = tmp_path / "backups"
        self.backups.mkdir()
        self.backups.chmod(0o700)
        self.tools = tmp_path / "bin"
        self.tools.mkdir()
        self.state_path = tmp_path / "server.json"
        self.state_path.write_text(json.dumps(_source()), encoding="utf-8")
        self.log = tmp_path / "calls.log"
        self.log.touch()
        for tool in ("mariadb", "mariadb-dump"):
            path = self.tools / tool
            path.write_text(
                STAND_IN.format(
                    python=sys.executable,
                    state=str(self.state_path),
                    log=str(self.log),
                ),
                encoding="utf-8",
            )
            path.chmod(0o700)
        names = () if replica else (names or ("crm",))
        for template in ("backup.sh.j2", "restore-check.sh.j2"):
            script = tmp_path / template.removesuffix(".j2")
            script.write_text(
                _render(template, self.backups, *names, replica=replica)
            )

    def run(self, script: str) -> subprocess.CompletedProcess[str]:
        """Run one rendered script with the stand-ins first on PATH."""
        return subprocess.run(  # noqa: S603 - the rendered script under test.
            [
                "/usr/bin/env",
                f"PATH={self.tools}:/usr/bin:/bin",
                "bash",
                str(self.tmp_path / script),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def backup(self) -> subprocess.CompletedProcess[str]:
        """Run cloudfall-mariadb-backup and require it to pass."""
        done = self.run("backup.sh")
        assert done.returncode == 0, done.stderr
        return done

    def drill(self) -> subprocess.CompletedProcess[str]:
        """Run cloudfall-mariadb-restore-check."""
        return self.run("restore-check.sh")

    @property
    def state(self) -> dict[str, Any]:
        """Return what the stand-in server holds now."""
        return cast(
            "dict[str, Any]", json.loads(self.state_path.read_text("utf-8"))
        )

    def update(self, **changes: object) -> None:
        """Change the stand-in server's state."""
        self.state_path.write_text(
            json.dumps({**self.state, **changes}), encoding="utf-8"
        )

    def calls(self) -> list[list[str]]:
        """Return every stand-in invocation, tool first."""
        return [
            json.loads(line)
            for line in self.log.read_text("utf-8").splitlines()
        ]

    def files(self) -> list[str]:
        """Return the names in the backup directory."""
        return sorted(path.name for path in self.backups.iterdir())


def _dumps(host: Host, database: str) -> list[Path]:
    return sorted(host.backups.glob(f"{database}-*.sql.gz"))


# proves: S-019-8
def test_s019_8_backup_writes_one_dump_and_one_count_file_per_database(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path, "crm", "billing")

    done = host.backup()

    (crm,) = _dumps(host, "crm")
    (billing,) = _dumps(host, "billing")
    assert len(host.files()) == 4
    assert not [name for name in host.files() if name.endswith(".part")]
    crm_counts = crm.with_name(crm.name.removesuffix(".sql.gz") + ".counts")
    assert crm_counts.read_text() == EXPECTED_CRM
    billing_counts = billing.with_name(
        billing.name.removesuffix(".sql.gz") + ".counts"
    )
    assert billing_counts.read_text() == "invoices\t2\n"
    # The dumps hold data: the backup's files are the owner's alone
    for path in host.backups.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert f"backed up crm: {crm} (4 tables, 9 rows)" in done.stdout
    assert f"backed up billing: {billing} (1 tables, 2 rows)" in done.stdout
    dumps = [call for call in host.calls() if call[0] == "mariadb-dump"]
    assert [call[-1] for call in dumps] == ["crm", "billing"]
    for call in dumps:
        # One consistent snapshot, no table locks, one database and no USE
        assert "--single-transaction" in call
        assert "--databases" not in call
        assert "--lock-tables" not in call
        assert "--master-data" not in " ".join(call)


def test_a_table_outside_the_snapshot_is_named(tmp_path: Path) -> None:
    host = Host(tmp_path)
    state = host.state
    state["databases"]["crm"]["engines"]["empty"] = "Aria"
    host.update(databases=state["databases"])

    done = host.backup()

    assert "crm: not in one snapshot, as they are not InnoDB:" in done.stdout
    assert "empty (Aria)" in done.stdout


# proves: S-019-8
def test_s019_8_trigger_bodies_routines_and_column_lists_count_as_the_load_does(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    state = host.state
    state["databases"]["crm"]["listed"] = ["Order Lines"]
    state["databases"]["crm"]["routines"] = True
    host.update(databases=state["databases"])
    host.backup()
    (counts,) = host.backups.glob("crm-*.counts")

    done = host.drill()

    # A statement in a trigger or a procedure is no table and no row of the dump
    assert counts.read_text() == EXPECTED_CRM
    assert done.returncode == 0, done.stderr


# proves: S-019-8
def test_s019_8_the_drill_restores_into_a_scratch_database_and_drops_it(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path, "crm", "billing")
    host.backup()
    (crm,) = _dumps(host, "crm")

    done = host.drill()

    assert done.returncode == 0, done.stderr
    assert (
        f"restore check passed for crm: {crm} (4 tables, 9 rows, as in the count file)"
    ) in done.stdout
    assert "restore check passed for billing:" in done.stdout
    assert SCRATCH not in host.state["databases"]
    # The source databases were never written to
    assert host.state["databases"]["crm"]["tables"] == _source()["databases"]["crm"][
        "tables"
    ]
    clients = [call for call in host.calls() if call[0] == "mariadb"]
    drill = clients[len(clients) - 2 * 6 :]
    # Every drill session writes nothing to the binary log
    assert all("--init-command=SET SESSION sql_log_bin = 0" in call for call in drill)
    loads = [call for call in drill if "--one-database" in call]
    assert [call[-1] for call in loads] == [SCRATCH, SCRATCH]
    creates = [
        call[-1] for call in drill if call[-1].startswith("CREATE DATABASE")
    ]
    assert creates == [f"CREATE DATABASE `{SCRATCH}` COMMENT '{MARKER}'"] * 2


# proves: S-019-8
def test_s019_8_a_row_written_to_the_source_after_the_dump_does_not_fail_the_drill(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    host.backup()
    state = host.state
    state["databases"]["crm"]["tables"]["customers"].append([6, "Linus"])
    state["databases"]["crm"]["tables"]["empty"].append([1, "late"])
    host.update(databases=state["databases"])

    done = host.drill()

    assert done.returncode == 0, done.stderr
    assert "(4 tables, 9 rows, as in the count file)" in done.stdout


# proves: S-019-8
def test_s019_8_with_no_dump_the_drill_fails_and_says_so(tmp_path: Path) -> None:
    host = Host(tmp_path)
    (host.backups / "crm-notes.sql.gz").write_bytes(b"")

    done = host.drill()

    assert done.returncode != 0
    assert f"no backup found for crm in {host.backups}" in done.stderr
    assert "restore check passed" not in done.stdout
    assert SCRATCH not in host.state["databases"]


# proves: S-019-8
def test_s019_8_a_restore_short_of_a_row_fails_and_drops_the_scratch_database(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    host.backup()
    host.update(lose_row=True)

    done = host.drill()

    assert done.returncode != 0
    assert "restore check failed for crm" in done.stderr
    assert "in the count file, not restored as such:\nOrder Lines\t3" in done.stderr
    assert "restored, not in the count file as such:\nOrder Lines\t2" in done.stderr
    assert "restore check passed" not in done.stdout
    assert SCRATCH not in host.state["databases"]


# proves: S-019-8
def test_s019_8_a_load_that_fails_still_drops_the_scratch_database(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    host.backup()
    host.update(load_fails=True)

    done = host.drill()

    assert done.returncode != 0
    assert "ERROR 1064" in done.stderr
    assert SCRATCH not in host.state["databases"]


def test_a_dump_without_its_count_file_fails(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.backup()
    (counts,) = host.backups.glob("crm-*.counts")
    counts.unlink()

    done = host.drill()

    assert done.returncode != 0
    assert "has no count file" in done.stderr
    assert SCRATCH not in host.state["databases"]


def test_a_dump_without_tables_fails(tmp_path: Path) -> None:
    host = Host(tmp_path)
    state = host.state
    state["databases"]["crm"]["tables"] = {}
    host.update(databases=state["databases"])
    host.backup()

    done = host.drill()

    assert done.returncode != 0
    assert "holds no table" in done.stderr


def test_a_scratch_database_the_drill_did_not_make_is_left_alone(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    host.backup()
    state = host.state
    state["databases"][SCRATCH] = {"tables": {"precious": [[1]]}, "comment": ""}
    host.update(databases=state["databases"])

    done = host.drill()

    assert done.returncode != 0
    assert "the restore drill did not make; it is left as it is" in done.stderr
    assert host.state["databases"][SCRATCH]["tables"] == {"precious": [[1]]}


def test_a_scratch_database_a_stopped_drill_left_is_dropped_first(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    host.backup()
    state = host.state
    state["databases"][SCRATCH] = {"tables": {"customers": 2}, "comment": MARKER}
    host.update(databases=state["databases"])

    done = host.drill()

    assert done.returncode == 0, done.stderr
    assert SCRATCH not in host.state["databases"]


def _age(path: Path, days: int) -> None:
    then = time.time() - days * DAY
    os.utime(path, (then, then))


def _old_dump(host: Host, name: str, days: int) -> tuple[Path, Path]:
    dump = host.backups / f"{name}.sql.gz"
    counts = host.backups / f"{name}.counts"
    for path in (dump, counts):
        path.write_bytes(b"")
        _age(path, days)
    return dump, counts


# proves: S-019-9
def test_s019_9_dumps_older_than_retention_days_are_gone_after_the_next_backup(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    old = _old_dump(host, "crm-20261001T023000Z", 20)
    recent = _old_dump(host, "crm-20261110T023000Z", 5)
    # Files the backup did not write stay, however old
    foreign = [
        host.backups / "crm-manual.sql.gz",
        host.backups / "crmx-20261001T023000Z.sql.gz",
        host.backups / "crm-20261001T023000Z.sql.gz.keep",
    ]
    for path in foreign:
        path.write_bytes(b"")
        _age(path, 30)

    done = host.backup()

    assert not any(path.exists() for path in old)
    assert all(path.exists() for path in recent)
    assert all(path.exists() for path in foreign)
    assert len(_dumps(host, "crm")) == 3  # recent, the new one, crm-manual
    assert "removed crm-20261001T023000Z.sql.gz, older than 14 days" in done.stdout
    # The receipt keeps the last lines: they name the dumps
    assert done.stdout.splitlines()[-1].startswith("backed up crm:")


# proves: S-019-9
def test_s019_9_the_newest_dump_is_kept_even_when_older_than_retention_days(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    # A newest dump, by its name, older than retentionDays (the clock moved)
    newest = _old_dump(host, "crm-20991231T023000Z", 30)
    older = _old_dump(host, "crm-20261001T023000Z", 40)

    host.backup()

    assert all(path.exists() for path in newest)
    assert not any(path.exists() for path in older)


# proves: S-019-9
def test_s019_9_a_failed_backup_deletes_no_dump(tmp_path: Path) -> None:
    host = Host(tmp_path)
    old = _old_dump(host, "crm-20261001T023000Z", 40)
    state = host.state
    del state["databases"]["crm"]
    host.update(databases=state["databases"])

    done = host.run("backup.sh")

    assert done.returncode != 0
    assert all(path.exists() for path in old)
    assert not [name for name in host.files() if name.endswith(".part")]


# proves: S-019-8
def test_s019_8_a_replica_backs_up_and_drills_every_database_it_holds(
    tmp_path: Path,
) -> None:
    # A replica declares no databases: they arrive by replication (#423)
    host = Host(tmp_path, replica=True)
    # The drill's scratch database, left by a drill that runs now, is no
    # database of the replica's
    databases = host.state["databases"]
    databases["cloudfall-restore-check"] = {
        "tables": {},
        "comment": "cloudfall restore drill",
    }
    # Nor is its data volume's lost+found, shown as a database (#449)
    databases["#mysql50#lost+found"] = {"tables": {}}
    host.update(databases=databases)

    done = host.backup()

    (billing,) = _dumps(host, "billing")
    (crm,) = _dumps(host, "crm")
    assert f"backed up billing: {billing} (1 tables, 2 rows)" in done.stdout
    assert f"backed up crm: {crm} (4 tables, 9 rows)" in done.stdout
    dumps = [call for call in host.calls() if call[0] == "mariadb-dump"]
    assert [call[-1] for call in dumps] == ["billing", "crm"]
    assert not _dumps(host, "cloudfall-restore-check")
    assert "lost+found" not in done.stdout
    databases = host.state["databases"]
    del databases["cloudfall-restore-check"]
    host.update(databases=databases)
    drill = host.drill()
    assert drill.returncode == 0, drill.stderr
    assert "restore check passed for billing" in drill.stdout
    assert "restore check passed for crm" in drill.stdout


def test_a_replica_that_holds_no_database_fails_both_scripts(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path, replica=True)
    host.update(databases={})

    backup = host.run("backup.sh")
    drill = host.drill()

    assert backup.returncode != 0
    assert "replica mariadb-main holds no database to back up" in backup.stderr
    assert drill.returncode != 0
    assert "replica mariadb-main holds no database to check" in drill.stderr
    assert host.files() == []


def test_a_service_without_databases_fails_both_scripts(tmp_path: Path) -> None:
    rendered = _render("backup.sh.j2", tmp_path)
    drill = _render("restore-check.sh.j2", tmp_path)

    assert 'echo "service mariadb-main declares no database to back up" >&2' in (
        rendered
    )
    assert 'echo "service mariadb-main declares no database to check" >&2' in drill


def _role_tasks(name: str) -> list[dict[str, Any]]:
    tasks = yaml.safe_load((ROLE / "tasks" / name).read_text("utf-8"))
    assert isinstance(tasks, list)
    return cast("list[dict[str, Any]]", tasks)


def _role_task(name: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return next(task for task in tasks if task["name"] == name)


# proves: S-019-8
def test_s019_8_the_role_installs_the_scripts_and_their_timers_as_root() -> None:
    tasks = _role_tasks("backup.yml")
    directory = _role_task("Create the backup directory", tasks)["ansible.builtin.file"]
    assert (directory["owner"], directory["group"], directory["mode"]) == (
        "root",
        "root",
        "0700",
    )
    installed = {
        task["ansible.builtin.template"]["dest"]: task
        for task in tasks
        if "ansible.builtin.template" in task
    }
    assert set(installed) == {
        "/usr/local/sbin/cloudfall-mariadb-backup",
        "/usr/local/sbin/cloudfall-mariadb-restore-check",
        "/etc/systemd/system/cloudfall-mariadb-backup.service",
        "/etc/systemd/system/cloudfall-mariadb-backup.timer",
        "/etc/systemd/system/cloudfall-mariadb-restore-check.service",
        "/etc/systemd/system/cloudfall-mariadb-restore-check.timer",
    }
    drill_unit = installed["/etc/systemd/system/cloudfall-mariadb-restore-check.timer"]
    assert drill_unit["when"] == (
        "cloudfall_mariadb_service.backup.restoreCheckOnCalendar is defined"
    )
    for unit in ("backup.service.j2", "restore-check.service.j2"):
        text = _render(unit, Path("/var/backups/cloudfall/mariadb-main"), "crm")
        assert "User=root\n" in text
        assert "Requires=mariadb.service\n" in text
    timer = _render("restore-check.timer.j2", Path("/var/backups"), "crm")
    assert "OnCalendar=*-*-* 05:15:00 UTC\n" in timer
    main = [task["name"] for task in _role_tasks("main.yml")]
    # The calendars are checked before the host changes; the scripts come last
    assert main.index(
        "Validate the declared backup and restore-drill calendar expressions"
    ) < main.index("Create, grow and mount the data volume")
    assert main[-1] == "Install the dumps and the restore drill"


# proves: S-019-8
def test_s019_8_backup_run_and_verify_run_the_mariadb_scripts_as_root() -> None:
    plays = yaml.safe_load(BACKUP_PLAYBOOK.read_text("utf-8"))
    task = _role_task(
        "Execute the requested backup action as the service user", plays[1]["tasks"]
    )

    assert "'mariadb': 'root'" in task["become_user"]
    assert "cloudfall-{{ cloudfall_backup_item.serviceKind" in task[
        "ansible.builtin.command"
    ]["argv"][0]
