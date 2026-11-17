"""A replica of a MariaDB server Cloudfall does not manage (S-019-17, S-019-18, #424).

The role's preflight and replication tasks run for real on h2, rendered from
a validated project whose only service is a replica declared with
``source``. The source is a ``mariadb_pair`` stand-in that is in no
inventory, behind ``mariadb_source``'s server, which speaks the MariaDB
protocol over TLS from a test CA: the preflight's client module logs in to
it before anything is installed, and the seed's dump and the link reach the
same stand-in. A live source's login, TLS handshake and replication filter
are the model's; the pull request lists them as unverified.
"""

from __future__ import annotations

import importlib.util
import json
import re
import socket
import subprocess
import threading
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml
from cloudfall.domain import AbsolutePath, ResourceId, SecretKeyName
from cloudfall.inventory import SecretValueReference
from cloudfall.mariadb import mysql_native_password_hash
from cloudfall.secrets import MariadbSourceLogin
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from mariadb_pair import server_state, write_server
from mariadb_project import SOURCE_CREDENTIALS_REF, mariadb_service, replica_service
from mariadb_source import SourceServer, write_tls
from test_s019_mariadb_replication_role import ADDRESSES, PROBE, ROLE, Pair, Run

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

SOURCE_USER = "mover"
SOURCE_PASSWORD = "synthetic-source-password-9876"  # noqa: S105 - grepped for
SOURCE_ACCOUNT = f"{SOURCE_USER}@{ADDRESSES['h2']}"
SOURCE_RIGHTS = [
    "REPLICATION SLAVE",
    "BINLOG MONITOR",
    "SELECT",
    "SHOW VIEW",
    "TRIGGER",
    "EVENT",
    "RELOAD",
]
CA_FILE = "pki/old-db-ca.pem"
# A task that reads a staged password into its arguments or its content
READS_PASSWORD = re.compile(
    r"lookup\('ansible\.builtin\.file', cloudfall_mariadb_"
    r"(replication_files\.sourcePassword|link_password_file)\)"
)
RECEIPT_HASH = mysql_native_password_hash(SOURCE_PASSWORD)


class Moving:
    """h1 and h2 of the pair harness, and a source of an older series."""

    def __init__(self, tmp_path: Path) -> None:
        """Write the source stand-in, its TLS, and start its protocol server."""
        self.pair = Pair(tmp_path)
        self.ca, certificate, key = write_tls(tmp_path / "source-tls", "127.0.0.1")
        state = server_state(
            address="127.0.0.1",
            names=["127.0.0.1"],
            server_id=11,
            log_bin=True,
            version="10.11.9-MariaDB-log",
            ca=self.ca.strip(),
        )
        users = cast("dict[str, Any]", state["users"])
        users[SOURCE_ACCOUNT] = {
            "plugin": "mysql_native_password",
            "hash": RECEIPT_HASH,
            "ssl": True,
            "locked": False,
            "privileges": list(SOURCE_RIGHTS),
        }
        # The source's own application account, which never reaches the replica
        users["legacy_app@localhost"] = {
            "plugin": "mysql_native_password",
            "hash": mysql_native_password_hash("legacy-app-value"),
            "ssl": False,
            "locked": False,
            "privileges": [],
        }
        network = json.loads(self.pair.network.read_text(encoding="utf-8"))
        self.client, self.dump, _, self.state_path, self.log = write_server(
            tmp_path / "source", state, self.pair.network, PROBE
        )
        self.server = SourceServer(self.state_path, self.client, (certificate, key))
        network[f"127.0.0.1:{self.server.port}"] = str(self.state_path)
        self.pair.network.write_text(json.dumps(network), encoding="utf-8")
        # The team's data, written with GTIDs of the source's own history
        for table, rows in (("invoices", 3), ("lines", 7)):
            assert self.sql(f"CLOUDFALL-STANDIN-WRITE root billing {table} {rows}")
        assert self.sql("CLOUDFALL-STANDIN-WRITE root crm contacts 5")
        # What the test itself sent the source; the converge's statements follow
        self.written = len(self.statements())
        self.pair.sources = (
            MariadbSourceLogin(
                ResourceId("mariadb-replica"),
                SecretValueReference(
                    environment=ResourceId("production"),
                    path=AbsolutePath("/services/mariadb-main"),
                    key=SecretKeyName("SOURCE_REPLICATION"),
                ),
                SOURCE_USER,
                SOURCE_PASSWORD,
                self.ca,
            ),
        )

    def sql(self, statement: str) -> bool:
        """Run one statement on the source through its stand-in client."""
        ran = subprocess.run(  # noqa: S603 - the stand-in client the test wrote.
            [str(self.client), "--batch", "--execute", statement],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return ran.returncode == 0

    def state(self) -> dict[str, Any]:
        """Return what the source holds now."""
        return cast("dict[str, Any]", json.loads(self.state_path.read_text("utf-8")))

    def edit(self, **fields: object) -> None:
        """Change the source by hand, as its team would."""
        state = self.state()
        state.update(fields)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        self.written = len(self.statements())

    def statements(self) -> list[str]:
        """Return every statement the source's client was given."""
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [
            argv[argv.index("--execute") + 1]
            for argv in (json.loads(line) for line in lines)
            if "--execute" in argv
        ]

    def declare(self, **source: object) -> None:
        """Declare h2 the replica of the source, with ``source`` merged in."""
        block = {
            "host": "127.0.0.1",
            "port": self.server.port,
            "credentialsRef": SOURCE_CREDENTIALS_REF,
            "caCertificateFile": CA_FILE,
            **source,
        }
        replica = replica_service()
        replication = cast("dict[str, Any]", replica["spec"])["mariadb"]["replication"]
        del replication["primary"]
        replication["source"] = {k: v for k, v in block.items() if v is not None}
        self.pair.declare(replica, files={CA_FILE: self.ca})

    def converge(self, *, succeed: bool = True) -> Run:
        """Converge h2 alone: h1 holds no service."""
        return self.pair.converge(limit="h2", succeed=succeed)


@pytest.fixture
def moving(tmp_path: Path) -> Iterator[Moving]:
    """Return the source, running, and h2 declared its replica."""
    found = Moving(tmp_path)
    found.declare()
    yield found
    found.server.stop()


def _assert_no_password(moving: Moving, run: Run) -> None:
    """Find the source's password in no output, argument, inventory or answer."""
    assert SOURCE_PASSWORD not in run.output
    assert SOURCE_PASSWORD not in moving.pair.arguments("h2")
    assert SOURCE_PASSWORD not in json.dumps(moving.statements())
    assert moving.pair.inventory is not None
    rendered = json.dumps(render_ansible_inventory(moving.pair.inventory))
    assert SOURCE_PASSWORD not in rendered
    assert SOURCE_USER not in rendered
    for outcome in run.outcomes.values():
        assert SOURCE_PASSWORD not in json.dumps(outcome.as_dict())


def _assert_source_unchanged(moving: Moving, before: dict[str, Any]) -> None:
    """Every statement the converge sent the source only read; it holds the same."""
    after = moving.state()
    assert after == before
    reads = moving.statements()[moving.written :]
    assert reads
    assert all(sql.lstrip().upper().startswith(("SELECT", "SHOW")) for sql in reads)


# proves: S-019-17
def test_s019_17_a_replica_of_an_older_source_is_seeded_without_its_users(
    moving: Moving,
) -> None:
    before = moving.state()

    run = moving.converge()

    seeded = run.outcomes["mariadb-replica"]
    assert seeded.outcome == "seed"
    replica = moving.pair.state("h2")
    # Every database and row of the source, at its position, and no user of it
    assert replica["databases"] == before["databases"]
    assert set(replica["users"]) == {"root@localhost"}
    assert replica["slavePos"] == f"0-11-{before['binlogState']['0-11']}"
    # It follows by GTID, over TLS verified against caCertificateFile, read-only
    link = replica["link"]
    assert (link["io"], link["sql"], link["started"]) == ("Yes", "Yes", True)
    assert (link["host"], link["port"]) == ("127.0.0.1", moving.server.port)
    assert link["user"] == SOURCE_USER
    assert link["useGtid"] == "slave_pos"
    assert (link["ssl"], link["verify"]) == (True, True)
    assert replica["readOnly"] is True
    assert replica["ignoreMysql"] is True
    _assert_no_password(moving, run)
    _assert_source_unchanged(moving, before)
    assert moving.dump.exists()
    # A second converge follows and changes nothing
    again = moving.converge()
    assert again.changed == 0, again.output
    assert again.outcomes == {}


# proves: S-019-17
def test_s019_17_rows_follow_and_a_user_made_on_the_source_does_not(
    moving: Moving,
) -> None:
    moving.converge()

    assert moving.sql("CLOUDFALL-STANDIN-WRITE root billing invoices 2")
    hashed = mysql_native_password_hash("eve-value")
    assert moving.sql(
        "CREATE USER IF NOT EXISTS 'eve'@'localhost' IDENTIFIED VIA "
        f"mysql_native_password USING '{hashed}'"
    )
    assert moving.sql("CLOUDFALL-STANDIN-WRITE root billing invoices 1")

    replica = moving.pair.state("h2")
    assert replica["databases"]["billing"]["invoices"] == 6
    assert "eve@localhost" not in replica["users"]
    assert "eve@localhost" in moving.state()["users"]
    # Its GTID still counts as applied: the link runs on past it
    assert replica["slavePos"] == f"0-11-{moving.state()['binlogState']['0-11']}"
    assert replica["link"]["sql"] == "Yes"


# proves: S-019-17
def test_s019_17_a_promoted_replica_of_a_stopped_source_takes_writes(
    moving: Moving,
) -> None:
    moving.converge()
    applied = moving.pair.state("h2")["slavePos"]
    # The team stops its old server; the replica is declared the primary
    moving.server.stop()
    moving.edit(running=False)
    moving.pair.declare(
        mariadb_service(
            "mariadb-replica",
            server="h2",
            databases=None,
            replication={
                "role": "primary",
                "serverId": 2,
                "listenAddress": ADDRESSES["h2"],
            },
        )
    )

    run = moving.converge()

    promoted = run.outcomes["mariadb-replica"]
    assert promoted.outcome == "promote"
    assert promoted.get("lastAppliedGtid") == applied
    state = moving.pair.state("h2")
    assert state["link"] is None
    assert state["readOnly"] is False
    assert state["ignoreMysql"] is False
    assert moving.pair.write("h2", "root", "invoices", 1)


# proves: S-019-17
def test_s019_17_promotion_over_a_running_source_is_refused(moving: Moving) -> None:
    moving.converge()
    moving.pair.declare(
        mariadb_service(
            "mariadb-replica",
            server="h2",
            databases=None,
            replication={
                "role": "primary",
                "serverId": 2,
                "listenAddress": ADDRESSES["h2"],
            },
        )
    )

    run = moving.converge(succeed=False)

    refused = run.outcomes["mariadb-replica"]
    assert refused.get("code") == "service_mariadb_promotion_unsafe"
    assert "no declared MariaDB service" in refused.get("message")
    assert moving.pair.state("h2")["link"]["io"] == "Yes"


def _refused(moving: Moving, run: Run) -> str:
    refused = run.outcomes["mariadb-replica"]
    assert refused.outcome == "refused"
    assert refused.get("code") == "service_mariadb_source_unfit"
    assert "PRECONDITION service_mariadb_source_unfit" in run.done.stdout
    # The replica's server was not touched: no client of it ran
    assert moving.pair.hosts["h2"]["log"].read_text(encoding="utf-8") == ""
    _assert_no_password(moving, run)
    return refused.get("message")


# proves: S-019-18
@pytest.mark.parametrize(
    ("change", "said"),
    [
        pytest.param({"logBin": False}, "writes no binary log", id="no binary log"),
        pytest.param(
            {"binlogFormat": "STATEMENT"},
            "its binary log is STATEMENT, not row-based",
            id="statement-based",
        ),
        pytest.param(
            {"version": "12.3.1-MariaDB"},
            "newer than this replica's version 11.8.9",
            id="newer than version",
        ),
        pytest.param(
            {"version": "8.0.36"}, "it runs 8.0.36, not MariaDB", id="mysql"
        ),
        pytest.param(
            {"serverId": 2},
            "its server_id is 2, the serverId this replica declares",
            id="same server_id",
        ),
    ],
)
def test_s019_18_the_preflight_refuses_a_source_it_can_not_follow(
    moving: Moving, change: dict[str, object], said: str
) -> None:
    moving.edit(**change)
    before = moving.state()

    run = moving.converge(succeed=False)

    message = _refused(moving, run)
    assert said in message
    assert f"127.0.0.1:{moving.server.port}" in message
    assert "nothing on the source changed" in message
    _assert_source_unchanged(moving, before)


# proves: S-019-18
def test_s019_18_the_preflight_names_the_right_the_account_lacks(
    moving: Moving,
) -> None:
    state = moving.state()
    state["users"][SOURCE_ACCOUNT]["privileges"].remove("RELOAD")
    moving.edit(users=state["users"])
    before = moving.state()

    run = moving.converge(succeed=False)

    message = _refused(moving, run)
    assert "its account lacks RELOAD ON *.*" in message
    _assert_source_unchanged(moving, before)


# proves: S-019-18
def test_s019_18_the_preflight_comes_before_anything_is_installed() -> None:
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text("utf-8"))
    names = [task["name"] for task in tasks]

    check = names.index("Check the source of a replica Cloudfall does not manage")
    assert check < names.index("Create, grow and mount the data volume")
    assert check < names.index("Install the pinned MariaDB server")
    # Every task before it only reads or refuses
    for task in tasks[:check]:
        assert {"ansible.builtin.assert", "ansible.builtin.command"} & set(task)
        if "ansible.builtin.command" in task:
            assert task["changed_when"] is False


# proves: S-019-18
def test_s019_18_every_task_that_sees_the_source_login_is_no_log() -> None:
    for name in ("source_check.yml", "replication_check.yml", "replication_link.yml"):
        text = (ROLE / "tasks" / name).read_text("utf-8")
        tasks = yaml.safe_load(text)
        flat = []
        for task in tasks:
            flat.append(task)
            for key in ("block", "rescue", "always"):
                for inner in task.get(key, []):
                    flat.append(inner)
                    flat.extend(inner.get("block", []))
        reading = [
            task
            for task in flat
            if "block" not in task and READS_PASSWORD.search(json.dumps(task))
        ]
        assert reading, name
        for task in reading:
            assert task.get("no_log") is True, task["name"]


def test_a_malformed_answer_of_the_source_is_a_source_error() -> None:
    spec = importlib.util.spec_from_file_location(
        "cloudfall_mariadb_source", ROLE / "library" / "cloudfall_mariadb_source.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    listener = socket.create_server(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])

    def answer() -> None:
        stream, _ = listener.accept()
        with stream:
            # A handshake of protocol 10 that ends after its first byte
            stream.sendall(b"\x01\x00\x00\x00\x0a")

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        with pytest.raises(module.SourceError, match="malformed"):
            module.connect(
                "127.0.0.1",
                port,
                user=SOURCE_USER,
                password=SOURCE_PASSWORD,
                ca="",
                timeout=5,
            )
    finally:
        thread.join(timeout=5)
        listener.close()


def test_the_source_check_reads_only() -> None:
    plugin_path = ROLE / "library" / "cloudfall_mariadb_source.py"
    text = plugin_path.read_text("utf-8")

    assert "_READ_ONLY" in text
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    for statement in defaults["cloudfall_mariadb_source_queries"].values():
        assert statement.lstrip().upper().startswith(("SELECT", "SHOW"))
