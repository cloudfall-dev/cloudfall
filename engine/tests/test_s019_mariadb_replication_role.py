"""Replication between two Cloudfall MariaDB servers (S-019-12 to S-019-16, #423).

The role's replication tasks run for real, twice over, on two inventory hosts
rendered from a validated project of a primary on h1 and a replica on h2:
the decision before the settings, the managed file rendered by Ansible's own
template module (the stand-in reads ``server_id``, ``log_bin`` and
``read_only`` from it as a restart would), and what was decided. Each host's
``mariadb`` and ``mariadb-dump`` are the stand-ins of ``mariadb_pair``, which
replicate by GTID; the other server of a pair is read on its own host, by
delegation, as on a live fleet. The replication password is staged by the
SDK the way a converge stages it. A live replica's threads, TLS handshake and
``read_only`` refusal are the model's; the pull request lists them as
unverified on two live hosts.
"""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml
from cloudfall.domain import AbsolutePath, ResourceId, SecretKeyName
from cloudfall.inventory import PlatformInventory, SecretValueReference
from cloudfall.mariadb import mysql_native_password_hash
from cloudfall.mariadb_replication import ReplicationOutcome, read_outcomes, refusal
from cloudfall.secrets import (
    MARIADB_REPLICATION_FILES_VARIABLE,
    MariadbReplicationPassword,
    mariadb_replication_pairs,
    staged_password_files,
)
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from mariadb_pair import CA, server_state, write_server
from mariadb_project import (
    SCHEMAS,
    mariadb_service,
    primary_replication,
    replica_service,
    write_project,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

ROOT = Path(__file__).parents[2]
ROLES = ROOT / "engine" / "ansible" / "roles"
ROLE = ROLES / "cloudfall_mariadb"
FIREWALL = ROLES / "cloudfall_firewall"
REPLICATION_PASSWORD = "synthetic-replication-password-0123456789"  # noqa: S105
ADDRESSES = {"h1": "203.0.113.11", "h2": "203.0.113.12"}
PER_HOST = (
    "cloudfall_mariadb_client",
    "cloudfall_mariadb_dump_client",
    "cloudfall_mariadb_state_directory",
    "cloudfall_mariadb_ca_path",
)
RECAP = re.compile(r"^(h1|h2)\s*:\s*ok=\d+\s+changed=(\d+)\s", re.MULTILINE)


def _plugin() -> ModuleType:
    path = ROLE / "filter_plugins" / "mariadb_replication.py"
    spec = importlib.util.spec_from_file_location("mariadb_replication", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _strip_ownership(tasks: list[dict[str, Any]]) -> None:
    """Drop owner and group: the replay runs unprivileged on the controller."""
    for task in tasks:
        for module in ("ansible.builtin.file", "ansible.builtin.copy"):
            if module in task:
                task[module].pop("owner", None)
                task[module].pop("group", None)
        for key in ("block", "always", "rescue"):
            if key in task:
                _strip_ownership(task[key])


def _primary(server_id: int = 1) -> dict[str, object]:
    return mariadb_service(replication=primary_replication(server_id))


def _replica_of(
    primary: str, service_id: str, server: str, server_id: int
) -> dict[str, object]:
    return replica_service(
        service_id, server=server, primary=primary, server_id=server_id
    )


def _new_primary(service_id: str = "mariadb-replica") -> dict[str, object]:
    """Declare the replica of h2 primary, with the primary's databases."""
    return mariadb_service(
        service_id,
        server="h2",
        replication={
            "role": "primary",
            "serverId": 2,
            "listenAddress": ADDRESSES["h2"],
        },
    )


class Run:
    """One converge of the pair: its process, its changes and its outcomes."""

    def __init__(
        self,
        done: subprocess.CompletedProcess[str],
        changed: int,
        outcomes: tuple[ReplicationOutcome, ...],
    ) -> None:
        """Keep what the run printed, changed and reported."""
        self.done = done
        self.changed = changed
        self.outcomes = {outcome.service: outcome for outcome in outcomes}

    @property
    def output(self) -> str:
        """Return everything Ansible printed."""
        return self.done.stdout + self.done.stderr


class Pair:
    """Two stand-in servers and the role's replication tasks to converge them."""

    def __init__(self, tmp_path: Path, locked_flag: str = "true") -> None:
        """Write both servers, fresh, and the role's tasks without ownership.

        ``locked_flag``: how both read a locked account's flag (#452).
        """
        self.tmp_path = tmp_path
        self.network = tmp_path / "network.json"
        self.ca = tmp_path / "ca.crt"
        self.ca.write_text(CA + "\n", encoding="utf-8")
        self.hosts: dict[str, dict[str, Path]] = {}
        network: dict[str, str] = {}
        for host, address in ADDRESSES.items():
            client, dump, state, log = write_server(
                tmp_path / host,
                server_state(
                    address=address,
                    names=[f"{host}.example.internal", address],
                    locked_flag=locked_flag,
                ),
                self.network,
            )
            state_directory = tmp_path / host / "state"
            state_directory.mkdir(mode=0o700)
            self.hosts[host] = {
                "client": client,
                "dump": dump,
                "state": state,
                "log": log,
                "directory": state_directory,
            }
            network[f"{address}:3306"] = str(state)
        self.network.write_text(json.dumps(network), encoding="utf-8")
        self.tasks = tmp_path / "tasks"
        self.tasks.mkdir()
        for path in (ROLE / "tasks").glob("*.yml"):
            tasks = yaml.safe_load(path.read_text("utf-8"))
            _strip_ownership(tasks)
            (self.tasks / path.name).write_text(yaml.safe_dump(tasks), "utf-8")
        self.declarations = 0
        self.inventory: PlatformInventory | None = None

    def declare(self, *services: dict[str, object]) -> None:
        """Write and validate a project of h1 and h2 holding ``services``."""
        self.declarations += 1
        project = write_project(
            self.tmp_path / f"project-{self.declarations}",
            *services,
            servers=("h1", "h2"),
        )
        self.inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    def rendered_host(self, host: str) -> dict[str, Any]:
        """Return a host's variables as the inventory render gives them."""
        assert self.inventory is not None
        document = render_ansible_inventory(self.inventory)
        children = cast("dict[str, Any]", document["all"])["children"]
        return cast("dict[str, Any]", children["cloudfall_servers"]["hosts"][host])

    def converge(
        self,
        *,
        limit: str | None = None,
        order: str = "inventory",
        succeed: bool = True,
    ) -> Run:
        """Run the replication tasks on the pair, one host at a time."""
        assert self.inventory is not None
        document = render_ansible_inventory(self.inventory)
        children = cast("dict[str, Any]", document["all"])["children"]
        for host, variables in children["cloudfall_servers"]["hosts"].items():
            variables.update(
                ansible_connection="local",
                ansible_python_interpreter=sys.executable,
                cloudfall_mariadb_client=str(self.hosts[host]["client"]),
                cloudfall_mariadb_dump_client=str(self.hosts[host]["dump"]),
                cloudfall_mariadb_state_directory=str(self.hosts[host]["directory"]),
                cloudfall_mariadb_ca_path=str(self.ca),
            )
        run_directory = self.tmp_path / f"run-{len(list(self.tmp_path.glob('run-*')))}"
        reports = run_directory / "reports"
        reports.mkdir(parents=True)
        inventory = run_directory / "inventory.json"
        inventory.write_text(json.dumps(document), encoding="utf-8")
        passwords = tuple(
            MariadbReplicationPassword(
                pair,
                SecretValueReference(
                    environment=ResourceId("production"),
                    path=AbsolutePath(f"/services/{pair.primary.value}"),
                    key=SecretKeyName("REPLICATION_PASSWORD"),
                ),
                REPLICATION_PASSWORD,
            )
            for pair in mariadb_replication_pairs(self.inventory)
        )
        with staged_password_files((), replication=passwords) as staged:
            extra = run_directory / "extra.json"
            extra.write_text(json.dumps(staged), encoding="utf-8")
            self.staged = staged
            done = self._play(
                run_directory,
                inventory=inventory,
                extra=extra,
                reports=reports,
                limit=limit,
                order=order,
            )
        changed = sum(int(match[2]) for match in RECAP.finditer(done.stdout))
        assert RECAP.search(done.stdout) is not None, done.stdout + done.stderr
        assert (done.returncode == 0) is succeed, done.stdout + done.stderr
        return Run(done, changed, read_outcomes(reports))

    def _play(  # noqa: PLR0913 - one replay's files and options.
        self,
        directory: Path,
        *,
        inventory: Path,
        extra: Path,
        reports: Path,
        limit: str | None,
        order: str,
    ) -> subprocess.CompletedProcess[str]:
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
        settings = "{{ cloudfall_mariadb_state_directory }}/90-cloudfall.cnf"
        play = [
            {
                "name": "Replay the replication tasks of cloudfall_mariadb",
                "hosts": "cloudfall_servers",
                "gather_facts": False,
                "serial": 1,
                "any_errors_fatal": True,
                "order": order,
                "vars": {
                    **{k: v for k, v in defaults.items() if k not in PER_HOST},
                    "cloudfall_mariadb_service": (
                        "{{ cloudfall_services | selectattr('serviceKind', "
                        "'equalto', 'mariadb') | first }}"
                    ),
                    "cloudfall_mariadb_replication_files": (
                        "{{ (cloudfall_services_mariadb_replication_files"
                        " | default({}))[cloudfall_mariadb_service.id]"
                        " | default({}) }}"
                    ),
                    "cloudfall_mariadb_report_directory": str(reports),
                    "cloudfall_mariadb_link_poll_seconds": 0,
                    "cloudfall_mariadb_promotion_wait_seconds": 1,
                    "cloudfall_mariadb_source_probe_seconds": 1,
                },
                "tasks": [
                    {
                        "name": "Decide what replication does on this server",
                        "ansible.builtin.include_tasks": str(
                            self.tasks / "replication_check.yml"
                        ),
                    },
                    {
                        "name": "Install the managed server settings",
                        "ansible.builtin.template": {
                            "src": str(ROLE / "templates" / "90-cloudfall.cnf.j2"),
                            "dest": settings,
                            "mode": "0644",
                        },
                        "changed_when": False,
                    },
                    {
                        "name": "Restart with them, as the handler would",
                        "ansible.builtin.command": {
                            "argv": [
                                "{{ cloudfall_mariadb_client }}",
                                "--batch",
                                "--execute",
                                f"CLOUDFALL-STANDIN-RESTART {settings}",
                            ]
                        },
                        "changed_when": False,
                    },
                    {
                        "name": "Carry out what replication does on this server",
                        "ansible.builtin.include_tasks": str(
                            self.tasks / "replication.yml"
                        ),
                    },
                ],
            }
        ]
        playbook = directory / "play.yml"
        playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
        binary = shutil.which("ansible-playbook")
        assert binary is not None
        argv = [
            binary,
            "-vv",
            "--inventory",
            str(inventory),
            "--extra-vars",
            f"@{extra}",
            str(playbook),
        ]
        if limit is not None:
            argv[1:1] = ["--limit", limit]
        environment = {
            **os.environ,
            "ANSIBLE_FILTER_PLUGINS": str(ROLE / "filter_plugins"),
            "ANSIBLE_HOST_KEY_CHECKING": "False",
        }
        return subprocess.run(  # noqa: S603 - fixed binary and arguments.
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=environment,
        )

    def state(self, host: str) -> dict[str, Any]:
        """Return what a server holds now, after its replica's threads ran."""
        self.sql(host, "SELECT @@global.gtid_slave_pos")
        return cast(
            "dict[str, Any]",
            json.loads(self.hosts[host]["state"].read_text(encoding="utf-8")),
        )

    def edit(self, host: str, **fields: object) -> None:
        """Change a server's state by hand: stop it, or give it data."""
        path = self.hosts[host]["state"]
        state = json.loads(path.read_text(encoding="utf-8"))
        state.update(fields)
        path.write_text(json.dumps(state), encoding="utf-8")

    def sql(self, host: str, statement: str) -> subprocess.CompletedProcess[str]:
        """Run one statement through a server's stand-in client."""
        return subprocess.run(  # noqa: S603 - the stand-in client the test wrote.
            [str(self.hosts[host]["client"]), "--batch", "--execute", statement],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def route(self, host: str, *, reachable: bool) -> None:
        """Let other servers reach ``host``'s port, or cut it off, as a firewall."""
        network = json.loads(self.network.read_text(encoding="utf-8"))
        key = f"{ADDRESSES[host]}:3306"
        if reachable:
            network[key] = str(self.hosts[host]["state"])
        else:
            network.pop(key)
        self.network.write_text(json.dumps(network), encoding="utf-8")

    def answer_at(self, host: str, address: str) -> None:
        """Let other servers reach ``host`` at ``address`` (host:port) too."""
        network = json.loads(self.network.read_text(encoding="utf-8"))
        network[address] = str(self.hosts[host]["state"])
        self.network.write_text(json.dumps(network), encoding="utf-8")

    def write(self, host: str, user: str, table: str, rows: int) -> bool:
        """Write ``rows`` rows into ``billing.<table>`` as ``user``; True if taken."""
        done = self.sql(host, f"CLOUDFALL-STANDIN-WRITE {user} billing {table} {rows}")
        return done.returncode == 0

    def dumps(self, host: str) -> int:
        """Return how many dumps a server's ``mariadb-dump`` took."""
        lines = self.hosts[host]["log"].read_text(encoding="utf-8").splitlines()
        return sum(json.loads(line)[0] == "dump" for line in lines)

    def arguments(self, host: str) -> str:
        """Return every argument any client of a server was given, not files."""
        lines = self.hosts[host]["log"].read_text(encoding="utf-8").splitlines()
        return json.dumps(
            [json.loads(line) for line in lines if json.loads(line)[0] != "sourced"]
        )

    def sourced(self, host: str) -> str:
        """Return every file a server's client read, as it read them."""
        lines = self.hosts[host]["log"].read_text(encoding="utf-8").splitlines()
        return json.dumps(
            [json.loads(line) for line in lines if json.loads(line)[0] == "sourced"]
        )


def _primary_with_data(pair: Pair) -> None:
    """Give h1 the databases and users a standalone converge left it, no binlog."""
    pair.edit(
        "h1",
        databases={"crm": {"contacts": 5}, "billing": {"invoices": 3, "lines": 7}},
        users={
            **cast("dict[str, Any]", pair.state("h1")["users"]),
            "billing@localhost": {
                "plugin": "mysql_native_password",
                "hash": mysql_native_password_hash("billing-value"),
                "ssl": False,
                "locked": False,
                "privileges": [],
            },
            "crm@localhost": {
                "plugin": "unix_socket",
                "hash": "",
                "ssl": False,
                "locked": False,
                "privileges": [],
            },
        },
    )


@pytest.fixture
def pair(tmp_path: Path) -> Pair:
    """Return h1 holding data, declared primary, and an empty h2, its replica."""
    pair = Pair(tmp_path)
    _primary_with_data(pair)
    pair.declare(_primary(), replica_service())
    return pair


def _seeded(pair: Pair) -> Run:
    first = pair.converge()
    assert first.outcomes["mariadb-replica"].outcome == "seed"
    return first


def _assert_follows(pair: Pair, host: str, primary: str) -> None:
    link = pair.state(host)["link"]
    assert link is not None
    assert link["started"] is True
    assert (link["io"], link["sql"]) == ("Yes", "Yes")
    assert link["host"] == ADDRESSES[primary]
    assert link["useGtid"] == "slave_pos"
    # Encrypted, and the primary's certificate verified against the project CA
    assert link["ssl"] is True
    assert link["verify"] is True
    assert Path(link["sslCa"]).read_text(encoding="utf-8").strip() == CA


# proves: S-019-12
def test_s019_12_an_empty_replica_is_seeded_and_follows_by_gtid_read_only(
    pair: Pair,
) -> None:
    run = _seeded(pair)

    primary, replica = pair.state("h1"), pair.state("h2")
    # Every database and row of the primary
    assert replica["databases"] == primary["databases"]
    assert replica["databases"]["billing"] == {"invoices": 3, "lines": 7}
    # Both threads run, by GTID, over TLS verified against the project CA
    _assert_follows(pair, "h2", "h1")
    assert replica["readOnly"] is True
    assert primary["readOnly"] is False
    # It reached the primary's position, and holds no GTID of its own
    assert replica["slavePos"] == f"0-1-{primary['binlogState']['0-1']}"
    assert list(replica["binlogState"]) == ["0-1"]
    assert "seeded at GTID 0-1-" in run.outcomes["mariadb-replica"].get("reason")
    # A row written on the primary afterwards appears on the replica
    assert pair.write("h1", "billing", "invoices", 2)
    assert pair.state("h2")["databases"]["billing"]["invoices"] == 5
    # The primary's users arrived with the seed; a write as billing is refused
    assert "billing@localhost" in pair.state("h2")["users"]
    assert not pair.write("h2", "billing", "invoices", 1)
    assert pair.state("h2")["databases"]["billing"]["invoices"] == 5


# proves: S-019-12
def test_s019_12_the_primary_holds_the_replication_account_as_a_hash_only(
    pair: Pair,
) -> None:
    run = _seeded(pair)

    users = pair.state("h1")["users"]
    account = users["cloudfall_repl@203.0.113.12"]
    assert account["plugin"] == "mysql_native_password"
    assert account["hash"] == mysql_native_password_hash(REPLICATION_PASSWORD)
    assert account["ssl"] is True
    assert "REPLICATION SLAVE" in account["privileges"]
    assert "SELECT" in account["privileges"]
    assert [key for key in users if key.startswith("cloudfall_repl@")] == [
        "cloudfall_repl@203.0.113.12"
    ]
    # The primary never sees the password, in any statement (D-9)
    assert REPLICATION_PASSWORD not in pair.sourced("h1")
    assert REPLICATION_PASSWORD not in pair.arguments("h1")
    # The replica's staged files held it for the run only, and are gone
    assert REPLICATION_PASSWORD not in pair.arguments("h2")
    assert REPLICATION_PASSWORD not in run.output
    assert list(pair.hosts["h2"]["directory"].glob("*.sql")) == []
    assert list(pair.hosts["h2"]["directory"].glob("*.cnf")) == [
        pair.hosts["h2"]["directory"] / "90-cloudfall.cnf"
    ]
    staged = cast("dict[str, dict[str, str]]", pair.staged)[
        MARIADB_REPLICATION_FILES_VARIABLE
    ]
    assert set(staged["mariadb-main"]) == {"hash"}
    assert set(staged["mariadb-replica"]) == {"password"}


def _port_rules(host: dict[str, Any], port: int) -> list[str]:
    environment = Environment(
        loader=FileSystemLoader(FIREWALL / "templates"),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    rendered = environment.get_template("nftables.conf.j2").render(
        cloudfall_firewall_ruleset=host["cloudfall_firewall"]
    )
    lines = [line.strip() for line in rendered.splitlines()]
    return [line for line in lines if f"dport {port} " in line]


# proves: S-019-12
def test_s019_12_the_primary_admits_3306_from_the_replica_only(pair: Pair) -> None:
    rules = _port_rules(pair.rendered_host("h1"), 3306)

    assert rules == [
        (
            'ip saddr 203.0.113.12/32 tcp dport 3306 accept comment "mariadb-main '
            'replication"'
        )
    ]
    # The replica opens nothing for it
    assert _port_rules(pair.rendered_host("h2"), 3306) == []


# proves: S-019-12
def test_s019_12_the_managed_files_hold_the_binary_log_and_read_only() -> None:
    def settings(service: dict[str, object], *, read_only: bool) -> list[str]:
        environment = Environment(
            loader=FileSystemLoader(ROLE / "templates"),
            undefined=StrictUndefined,
            autoescape=False,  # noqa: S701 - configuration files are not HTML.
            keep_trailing_newline=True,
            trim_blocks=True,
        )
        rendered = environment.get_template("90-cloudfall.cnf.j2").render(
            cloudfall_mariadb_service=cast("dict[str, object]", service["spec"])
            | {"id": cast("dict[str, str]", service["metadata"])["id"]},
            cloudfall_mariadb_data_path="/var/lib/mysql",
            cloudfall_mariadb_certificate_path="/etc/mysql/cloudfall/server.crt",
            cloudfall_mariadb_key_path="/etc/mysql/cloudfall/server.key",
            cloudfall_mariadb_ca_path="/etc/mysql/cloudfall/ca.crt",
            cloudfall_mariadb_read_only=read_only,
        )
        return [line for line in rendered.splitlines() if not line.startswith("#")]

    primary = settings(_primary(), read_only=False)
    replica = settings(replica_service(), read_only=True)

    assert "bind-address = 127.0.0.1,203.0.113.11" in primary
    assert "bind-address = 127.0.0.1" in replica
    for lines, server_id in ((primary, 1), (replica, 2)):
        assert f"server_id = {server_id}" in lines
        assert "log_bin = mariadb-bin" in lines
        assert "binlog_format = ROW" in lines
        assert "gtid_strict_mode = ON" in lines
        assert "log_slave_updates = ON" in lines
        # Kept seven days
        assert "binlog_expire_logs_seconds = 604800" in lines
        assert "ssl_ca = /etc/mysql/cloudfall/ca.crt" in lines
    assert "read_only = ON" in replica
    assert "read_only = ON" not in primary


# proves: S-019-13
def test_s019_13_a_second_converge_changes_nothing_and_does_not_seed_again(
    pair: Pair,
) -> None:
    _seeded(pair)
    before = pair.state("h2")

    second = pair.converge()

    assert second.changed == 0, second.output
    assert pair.dumps("h2") == 1
    assert second.outcomes == {}
    after = pair.state("h2")
    assert after["databases"] == before["databases"]
    assert after["link"] == before["link"]
    _assert_follows(pair, "h2", "h1")


# What JSON_VALUE(Priv, '$.account_locked') reads for a locked account:
# 'true' on the older series, 1 on 11.8 (#452).
LOCKED_FLAGS = pytest.mark.parametrize(
    "locked_flag",
    [
        pytest.param("true", id="locked reads true"),
        pytest.param("1", id="locked reads 1, as on 11.8"),
    ],
)


def _locked_pair(tmp_path: Path, locked_flag: str) -> Pair:
    """Return the fixture's pair, seeded, whose servers read the flag so."""
    pair = Pair(tmp_path, locked_flag)
    _primary_with_data(pair)
    pair.declare(_primary(), replica_service())
    _seeded(pair)
    return pair


# proves: S-019-13
@LOCKED_FLAGS
def test_s019_13_a_replicas_locked_replication_account_is_unlocked(
    tmp_path: Path, locked_flag: str
) -> None:
    pair = _locked_pair(tmp_path, locked_flag)
    # Its account was locked while h2 was no replica of h1 (it was the
    # primary, say): its link is refused the login
    account = f"cloudfall_repl@{ADDRESSES['h2']}"
    users = pair.state("h1")["users"]
    pair.edit("h1", users={**users, account: {**users[account], "locked": True}})
    assert pair.state("h2")["link"]["ioErrno"] == 1045

    pair.converge()

    assert pair.state("h1")["users"][account]["locked"] is False
    _assert_follows(pair, "h2", "h1")
    again = pair.converge()
    assert again.changed == 0, again.output


# proves: S-019-13
@LOCKED_FLAGS
def test_s019_13_the_account_of_a_server_that_left_the_pair_is_locked_once(
    tmp_path: Path, locked_flag: str
) -> None:
    pair = _locked_pair(tmp_path, locked_flag)
    # A former replica, no server of the pair now, kept its account
    gone = "cloudfall_repl@198.51.100.9"
    users = pair.state("h1")["users"]
    pair.edit("h1", users={**users, gone: users[f"cloudfall_repl@{ADDRESSES['h2']}"]})

    first = pair.converge()
    second = pair.converge()

    assert first.changed == 1, first.output
    assert pair.state("h1")["users"][gone]["locked"] is True
    assert second.changed == 0, second.output
    _assert_follows(pair, "h2", "h1")


# proves: S-019-13
def test_s019_13_a_replica_holding_a_database_that_can_not_attach_stops(
    tmp_path: Path,
) -> None:
    pair = Pair(tmp_path)
    _primary_with_data(pair)
    # h2 ran standalone, wrote no binary log, and holds a database of its own
    pair.edit("h2", databases={"ledger": {"entries": 40}})
    pair.declare(_primary(), replica_service())

    run = pair.converge(succeed=False)

    refused = refusal(tuple(run.outcomes.values()))
    assert refused is not None
    assert refused.service == "mariadb-replica"
    assert refused.get("code") == "service_mariadb_replica_unattachable"
    assert "never seeded over its data" in refused.get("message")
    assert "PRECONDITION service_mariadb_replica_unattachable" in run.done.stdout
    # Nothing dropped, nothing seeded, no link
    state = pair.state("h2")
    assert state["databases"] == {"ledger": {"entries": 40}}
    assert state["link"] is None
    assert pair.dumps("h2") == 0
    assert "DROP" not in pair.sourced("h2") + pair.arguments("h2")


def _promote_replica(pair: Pair) -> None:
    """Declare h2's service primary beside h1's, as an operator promoting it."""
    pair.declare(_primary(), _new_primary())


# proves: S-019-14
def test_s019_14_with_the_primary_stopped_the_replica_is_promoted(
    pair: Pair,
) -> None:
    _seeded(pair)
    assert pair.write("h1", "billing", "invoices", 4)
    applied = pair.state("h2")["slavePos"]
    pair.edit("h1", running=False)
    _promote_replica(pair)

    run = pair.converge(limit="h2")

    promoted = run.outcomes["mariadb-replica"]
    assert promoted.outcome == "promote"
    assert promoted.get("lastAppliedGtid") == applied
    assert promoted.get("reason") == "its source can not be reached"
    assert f"last GTID applied {applied}" in run.output
    state = pair.state("h2")
    # Writable, with no replication configured
    assert state["link"] is None
    assert state["readOnly"] is False
    assert pair.write("h2", "billing", "invoices", 1)
    assert state["databases"]["billing"]["invoices"] == 7


# proves: S-019-14
def test_s019_14_with_the_primary_running_and_writable_the_promotion_stops(
    pair: Pair,
) -> None:
    _seeded(pair)
    assert pair.write("h1", "billing", "invoices", 4)
    source = pair.state("h1")
    replica = pair.state("h2")
    _promote_replica(pair)

    run = pair.converge(succeed=False)

    refused = run.outcomes["mariadb-replica"]
    assert refused.outcome == "refused"
    assert refused.get("code") == "service_mariadb_promotion_unsafe"
    assert "PRECONDITION service_mariadb_promotion_unsafe" in run.done.stdout
    # Both positions are named
    source_position = ",".join(
        f"{key}-{value}" for key, value in source["binlogState"].items()
    )
    assert refused.get("sourcePosition") == source_position
    assert refused.get("replicaPosition") == replica["slavePos"]
    assert source_position in refused.get("message")
    assert replica["slavePos"] in refused.get("message")
    # Nothing changed: the replica still replicates and stays read-only
    _assert_follows(pair, "h2", "h1")
    assert pair.state("h2")["readOnly"] is True
    assert pair.write("h1", "billing", "invoices", 2)
    assert pair.state("h2")["databases"]["billing"]["invoices"] == 9


# proves: S-019-14
def test_s019_14_an_unconnected_link_is_not_promoted_over_a_writable_primary(
    pair: Pair,
) -> None:
    _seeded(pair)
    assert pair.write("h1", "billing", "invoices", 4)
    # h2's IO thread can't reach h1 (a firewall change): it keeps connecting
    # and reports no source server_id, while h1 runs and takes writes
    pair.route("h1", reachable=False)
    link = pair.state("h2")["link"]
    assert (link["io"], link["masterServerId"]) == ("Connecting", 0)
    source = pair.state("h1")
    replica = pair.state("h2")
    _promote_replica(pair)

    run = pair.converge(limit="h2", succeed=False)

    refused = run.outcomes["mariadb-replica"]
    assert refused.outcome == "refused"
    assert refused.get("code") == "service_mariadb_promotion_unsafe"
    assert "PRECONDITION service_mariadb_promotion_unsafe" in run.done.stdout
    source_position = ",".join(
        f"{key}-{value}" for key, value in source["binlogState"].items()
    )
    assert refused.get("sourcePosition") == source_position
    assert refused.get("replicaPosition") == replica["slavePos"]
    assert "writable" in refused.get("message")
    # Nothing changed: still a read-only replica, which follows once it connects
    assert pair.state("h2")["readOnly"] is True
    pair.route("h1", reachable=True)
    _assert_follows(pair, "h2", "h1")
    assert pair.write("h1", "billing", "invoices", 2)
    assert pair.state("h2")["databases"]["billing"]["invoices"] == 9


def _closed_port() -> int:
    """Return a port of the loopback address nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return cast("int", probe.getsockname()[1])


def _link_to(pair: Pair, host: str, port: int, **fields: object) -> None:
    """Point h2's link at ``host:port``, as a link to an undeclared source."""
    link = pair.state("h2")["link"]
    pair.edit("h2", link={**link, "host": host, "port": port, **fields})


def _refused_source(pair: Pair) -> None:
    _link_to(pair, "127.0.0.1", _closed_port())


def _silent_source(pair: Pair) -> None:
    # h1 is stopped and the controller sends to a documentation address,
    # which no network answers: the probe times out, or finds no route
    pair.edit("h1", running=False)


def _unresolvable_source(pair: Pair) -> None:
    _link_to(pair, "mariadb-source.invalid", 3306)


def _source_refusing_the_login(pair: Pair) -> None:
    # The replica reaches h1 there, and h1 refuses its password; the
    # controller is refused at that port (a firewall that admits the replica)
    port = _closed_port()
    pair.answer_at("h1", f"127.0.0.1:{port}")
    _link_to(pair, "127.0.0.1", port, password="not-it")  # noqa: S106 - a wrong one.


def _source_failing_tls(pair: Pair) -> None:
    # The replica reaches h1 at a name its certificate does not hold
    port = _closed_port()
    pair.answer_at("h1", f"127.0.0.1:{port}")
    _link_to(pair, "127.0.0.1", port)


# proves: S-019-14
@pytest.mark.parametrize(
    ("setup", "io_errno", "says"),
    [
        pytest.param(_refused_source, "2003", "", id="refused: promoted"),
        pytest.param(
            _silent_source,
            "2003",
            r"probe of that port (timed out|found no route to it)",
            id="timeout or no route",
        ),
        pytest.param(
            _unresolvable_source,
            "2003",
            "could not resolve mariadb-source.invalid",
            id="a name the controller can not resolve",
        ),
        pytest.param(
            _source_refusing_the_login,
            "1045",
            "its last error came from that source",
            id="the source refuses the login",
        ),
        pytest.param(
            _source_failing_tls,
            "2026",
            "its last error came from that source",
            id="a TLS error",
        ),
    ],
)
def test_s019_14_an_undeclared_source_is_promoted_over_only_when_it_refuses(
    pair: Pair, setup: Callable[[Pair], None], io_errno: str, says: str
) -> None:
    _seeded(pair)
    setup(pair)
    replica = pair.state("h2")
    assert replica["link"]["io"] == "Connecting"
    assert str(replica["link"]["ioErrno"]) == io_errno
    # h2 alone is declared, primary: its link's source is no declared service
    pair.declare(_new_primary())

    run = pair.converge(limit="h2", succeed=not says)

    outcome = run.outcomes["mariadb-replica"]
    state = pair.state("h2")
    if not says:
        assert outcome.outcome == "promote"
        assert outcome.get("reason").startswith("its source refuses connections")
        assert state["link"] is None
        assert state["readOnly"] is False
        return
    assert outcome.outcome == "refused"
    assert outcome.get("code") == "service_mariadb_promotion_unsafe"
    assert "PRECONDITION service_mariadb_promotion_unsafe" in run.done.stdout
    assert re.search(says, outcome.get("message")) is not None
    assert replica["slavePos"] in outcome.get("message")
    # Nothing changed: still a read-only replica of that source
    assert state["readOnly"] is True
    assert state["link"] is not None
    assert state["link"]["host"] == replica["link"]["host"]


# proves: S-019-15
@pytest.mark.parametrize("order", ["inventory", "reverse_inventory"])
def test_s019_15_a_role_swap_ends_within_two_converges_with_no_row_lost(
    pair: Pair, order: str
) -> None:
    _seeded(pair)
    assert pair.write("h1", "billing", "invoices", 4)
    rows = pair.state("h1")["databases"]
    # One change, with no writer running: the old primary is the new one's
    # replica, the old replica the primary
    pair.declare(
        _replica_of("mariadb-replica", "mariadb-main", "h1", 1), _new_primary()
    )

    first = pair.converge(order=order)
    second = pair.converge(order=order)

    outcomes = {
        service: outcome.outcome
        for run in (first, second)
        for service, outcome in run.outcomes.items()
    }
    assert outcomes["mariadb-replica"] == "promote"
    assert outcomes["mariadb-main"] == "attach"
    _assert_follows(pair, "h1", "h2")
    new_primary = pair.state("h2")
    assert new_primary["link"] is None
    assert new_primary["readOnly"] is False
    assert pair.state("h1")["readOnly"] is True
    # No row lost, and no seed: h1 kept its data and attached by GTID
    assert new_primary["databases"] == rows
    assert pair.state("h1")["databases"] == rows
    assert pair.dumps("h1") == 0
    assert pair.dumps("h2") == 1
    # The swap is done: a write on the new primary reaches the old one
    assert pair.write("h2", "billing", "lines", 3)
    assert pair.state("h1")["databases"]["billing"]["lines"] == 10
    third = pair.converge(order=order)
    assert third.changed == 0, third.output


# proves: S-019-16
def test_s019_16_a_former_primary_that_diverged_is_not_attached_nor_emptied(
    pair: Pair,
) -> None:
    _seeded(pair)
    pair.edit("h1", running=False)
    _promote_replica(pair)
    pair.converge(limit="h2")
    assert pair.write("h2", "billing", "invoices", 1)
    # The old primary comes back and takes a write the new one never saw
    pair.edit("h1", running=True)
    assert pair.write("h1", "billing", "invoices", 100)
    before = pair.state("h1")
    pair.declare(
        _replica_of("mariadb-replica", "mariadb-main", "h1", 1), _new_primary()
    )

    run = pair.converge(succeed=False)

    refused = run.outcomes["mariadb-main"]
    assert refused.get("code") == "service_mariadb_replica_diverged"
    unseen = max(
        f"0-1-{sequence}" for sequence in before["binlogState"].values()
    )
    assert f"never saw: {unseen}" in refused.get("message")
    assert "PRECONDITION service_mariadb_replica_diverged" in run.done.stdout
    after = pair.state("h1")
    # Not attached, not emptied: its rows, the diverged one too, are there
    assert after["link"] is None
    assert after["databases"] == before["databases"]
    assert after["databases"]["billing"]["invoices"] == 103
    assert pair.dumps("h1") == 0


def test_the_gtid_comparison_reads_sets_not_one_position() -> None:
    plugin = _plugin()

    # The former primary's own write past the promotion point
    assert plugin.gtid_unseen("0-1-11", "0-1-10,0-2-15") == ["0-1-11"]
    # A position behind the new primary's, of the same server, is seen
    assert plugin.gtid_unseen("0-1-10", "0-1-10,0-2-15") == []
    # A domain the other never saw
    assert plugin.gtid_unseen("0-1-3,1-1-2", "0-1-3") == ["1-1-2"]
    # The same sequence by another server is not the same transaction
    assert not plugin.gtid_applied("0-2-10", "0-1-10")
    assert plugin.gtid_applied("0-1-12", "0-1-10")
    assert not plugin.gtid_applied("0-1-9", "0-1-10")
    assert plugin.gtid_applied("", "")
    with pytest.raises(ValueError, match="not a MariaDB GTID"):
        plugin.gtids("0-1")


def _state(**fields: str) -> dict[str, str]:
    return {
        "readOnly": "1",
        "serverId": "2",
        "binlogPos": "0-1-10",
        "binlogState": "0-1-10",
        "slavePos": "0-1-10",
        "currentPos": "0-1-10",
        "databases": "2",
        **fields,
    }


def _down(errno_: str = "2003", **fields: str) -> dict[str, str]:
    """Return an unconnected link to an undeclared source, its last IO error."""
    return {
        "Master_Server_Id": "0",
        "Master_Host": "db.example.internal",
        "Master_Port": "3306",
        "Slave_IO_Running": "Connecting",
        "Last_IO_Errno": errno_,
        "Last_IO_Error": f"error {errno_}",
        **fields,
    }


@pytest.mark.parametrize(
    ("link", "peer", "other", "probe", "action"),
    [
        pytest.param(
            {"Master_Server_Id": "1", "Slave_IO_Running": "Connecting"},
            {"service": "mariadb-main", "server": "h1", "role": "primary"},
            {},
            "",
            "promote",
            id="source down",
        ),
        pytest.param(
            _down(Master_Server_Id="9", Slave_IO_Running="No"),
            {},
            {},
            "refused",
            "promote",
            id="undeclared source, link down, refused",
        ),
        pytest.param(
            _down(), {}, {}, "open", "refuse", id="undeclared source, link down, open"
        ),
        pytest.param(
            _down(), {}, {}, "timeout", "refuse", id="undeclared source, timeout"
        ),
        pytest.param(
            _down(), {}, {}, "no-route", "refuse", id="undeclared source, no route"
        ),
        pytest.param(
            _down(), {}, {}, "unresolved", "refuse", id="undeclared source, no DNS"
        ),
        pytest.param(
            _down(), {}, {}, "failed", "refuse", id="undeclared source, other error"
        ),
        pytest.param(
            _down("1045"),
            {},
            {},
            "refused",
            "refuse",
            id="undeclared source refusing the login, controller refused",
        ),
        pytest.param(
            _down("2026"),
            {},
            {},
            "refused",
            "refuse",
            id="undeclared source failing TLS, controller refused",
        ),
        pytest.param(
            _down("0"),
            {},
            {},
            "refused",
            "promote",
            id="undeclared source, no IO error yet, refused",
        ),
        pytest.param(
            {"Master_Server_Id": "9", "Slave_IO_Running": "Yes"},
            {},
            {},
            "",
            "refuse",
            id="undeclared source, link running",
        ),
        pytest.param(
            {"Master_Server_Id": "1", "Slave_IO_Running": "Yes"},
            {"service": "mariadb-main", "server": "h1", "role": "primary"},
            _state(readOnly="1", serverId="1", binlogPos="0-1-12"),
            "",
            "refuse",
            id="read-only source not applied",
        ),
    ],
)
def test_removing_replication_promotes_only_as_a_role_change_does(
    link: dict[str, str],
    peer: dict[str, str],
    other: dict[str, str],
    probe: str,
    action: str,
) -> None:
    plan = _plugin().replication_plan(
        _state(),
        service="mariadb-replica",
        declared="standalone",
        link=link,
        links={},
        peer=peer,
        other=other,
        other_link={},
        source_probe=probe,
    )

    assert plan["action"] == action
    if action == "refuse":
        assert plan["code"] == "service_mariadb_promotion_unsafe"
        assert "0-1-10" in plan["message"]


def test_an_undeclared_source_not_probed_is_an_error() -> None:
    with pytest.raises(ValueError, match="no probe of its source"):
        _plugin().replication_plan(
            _state(),
            service="mariadb-replica",
            declared="primary",
            link=_down(),
            links={},
            peer={},
            other={},
            other_link={},
            source_probe="",
        )
    with pytest.raises(ValueError, match="unknown source probe outcome"):
        _plugin().replication_plan(
            _state(),
            service="mariadb-replica",
            declared="primary",
            link=_down(),
            links={},
            peer={},
            other={},
            other_link={},
            source_probe="reachable",
        )


# proves: S-019-14
@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        pytest.param(None, "open", id="connected"),
        pytest.param(
            OSError(errno.ECONNREFUSED, "Connection refused"), "refused", id="refused"
        ),
        pytest.param(TimeoutError("timed out"), "timeout", id="timeout"),
        pytest.param(
            OSError(errno.ETIMEDOUT, "Operation timed out"),
            "timeout",
            id="the kernel's timeout",
        ),
        pytest.param(
            OSError(errno.EHOSTUNREACH, "No route to host"), "no-route", id="no route"
        ),
        pytest.param(
            OSError(errno.ENETUNREACH, "Network is unreachable"),
            "no-route",
            id="no network",
        ),
        pytest.param(
            socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
            "unresolved",
            id="an unknown name",
        ),
        pytest.param(
            OSError(errno.ECONNRESET, "Connection reset by peer"),
            "failed",
            id="any other error",
        ),
    ],
)
def test_s019_14_the_source_probe_tells_a_refusal_from_what_it_can_not_read(
    error: OSError | None, outcome: str
) -> None:
    assert _plugin().probe_outcome(error) == outcome


# proves: S-019-14
@pytest.mark.parametrize(
    ("outcomes", "outcome"),
    [
        pytest.param(["refused"], "refused", id="one address refused"),
        pytest.param(["refused", "refused"], "refused", id="every address refused"),
        pytest.param(["refused", "open"], "open", id="one address open"),
        # A name with an IPv4 address the controller times out on (a firewall
        # that drops) and an IPv6 one nothing listens on (bind 0.0.0.0):
        # create_connection raises only the last error, a refusal
        pytest.param(["timeout", "refused"], "timeout", id="timeout, then refused"),
        pytest.param(["refused", "no-route"], "no-route", id="refused, then no route"),
    ],
)
def test_s019_14_a_name_refuses_only_when_each_of_its_addresses_does(
    outcomes: list[str], outcome: str
) -> None:
    assert _plugin().probe_outcomes(outcomes) == outcome


# proves: S-019-14
def test_s019_14_the_source_probe_connects_from_the_controller() -> None:
    plugin = _plugin()
    with socket.socket() as listening:
        listening.bind(("127.0.0.1", 0))
        listening.listen(1)
        port = listening.getsockname()[1]
        assert plugin.source_probe("127.0.0.1", port, 1) == "open"
    assert plugin.source_probe("127.0.0.1", _closed_port(), 1) == "refused"
    assert plugin.source_probe("mariadb-source.invalid", 3306, 1) == "unresolved"
    with pytest.raises(ValueError, match="not a port"):
        plugin.source_probe("127.0.0.1", 0, 1)


_PEERS = [
    {
        "service": "mariadb-main",
        "server": "h1",
        "serverId": 1,
        "role": "primary",
        "addresses": ["h1.example.internal", "203.0.113.11", "2001:db8::11"],
        "port": 3306,
    },
    {
        "service": "mariadb-other",
        "server": "h3",
        "serverId": 3,
        "role": "replica",
        "primary": "mariadb-main",
        "addresses": ["h3.example.internal", "203.0.113.13"],
        "port": 3307,
    },
]


# proves: S-019-14
@pytest.mark.parametrize(
    ("link", "service"),
    [
        pytest.param(
            {"Master_Server_Id": "3", "Master_Host": "203.0.113.11"},
            "mariadb-other",
            id="the server_id it reports first",
        ),
        pytest.param(
            {"Master_Server_Id": "0", "Master_Host": "203.0.113.11"},
            "mariadb-main",
            id="not connected: listen address and port",
        ),
        pytest.param(
            {"Master_Server_Id": "9", "Master_Host": "203.0.113.11"},
            "mariadb-main",
            id="a server_id no peer has: host and port",
        ),
        pytest.param(
            {"Master_Server_Id": "0", "Master_Host": "2001:DB8:0::11"},
            "mariadb-main",
            id="an IPv6 address in another form",
        ),
        pytest.param(
            {"Master_Server_Id": "0", "Master_Host": "H1.Example.Internal."},
            "mariadb-main",
            id="the hostname in another case, with the root dot",
        ),
        pytest.param(
            {"Master_Server_Id": "0", "Master_Host": "203.0.113.13",
             "Master_Port": "3307"},
            "mariadb-other",
            id="a former primary declared a replica, by its server's address",
        ),
        pytest.param(
            {"Master_Server_Id": "0", "Master_Host": "203.0.113.11",
             "Master_Port": "3307"},
            "",
            id="another port",
        ),
        pytest.param(
            {"Master_Server_Id": "0", "Master_Host": "db.example.internal"},
            "",
            id="a name no peer has, never resolved",
        ),
    ],
)
def test_s019_14_a_link_is_matched_to_its_source_by_server_id_or_host_and_port(
    link: dict[str, str], service: str
) -> None:
    found = _plugin().link_peer({"Master_Port": "3306", **link}, _PEERS)

    assert found.get("service", "") == service


# proves: S-019-14
def test_s019_14_a_link_reporting_server_id_0_is_not_promoted_over_its_source() -> (
    None
):
    plugin = _plugin()
    # The replica restarted and its IO thread still connects: no server_id
    link = {
        "Master_Server_Id": "0",
        "Master_Host": "203.0.113.11",
        "Master_Port": "3306",
        "Slave_IO_Running": "Connecting",
    }
    peer = plugin.link_peer(link, _PEERS)

    plan = plugin.replication_plan(
        _state(readOnly="1"),
        service="mariadb-replica",
        declared="primary",
        link=link,
        links={"peers": _PEERS},
        peer=peer,
        other=_state(readOnly="0", serverId="1", binlogPos="0-1-12"),
        other_link={},
        source_probe="",
    )

    assert peer["service"] == "mariadb-main"
    assert plan["action"] == "refuse"
    assert plan["code"] == "service_mariadb_promotion_unsafe"
    assert "running and writable" in plan["message"]
    assert (plan["sourcePosition"], plan["replicaPosition"]) == ("0-1-12", "0-1-10")


def test_the_rendered_pair_names_the_primary_and_its_replicas(pair: Pair) -> None:
    primary = pair.rendered_host("h1")["cloudfall_mariadb_replication"]
    replica = pair.rendered_host("h2")["cloudfall_mariadb_replication"]

    assert primary["mariadb-main"]["replicas"] == [
        {
            "service": "mariadb-replica",
            "server": "h2",
            "serverId": 2,
            "hosts": ["203.0.113.12"],
        }
    ]
    assert replica["mariadb-replica"]["primary"] == {
        "service": "mariadb-main",
        "server": "h1",
        "serverId": 1,
        "host": "203.0.113.11",
        "port": 3306,
    }
    assert [peer["service"] for peer in replica["mariadb-replica"]["peers"]] == [
        "mariadb-main"
    ]
    # A link names one of the names of its primary's certificate, and its port
    peer = replica["mariadb-replica"]["peers"][0]
    assert (peer["addresses"], peer["port"]) == (
        ["h1.example.internal", "203.0.113.11"],
        3306,
    )
    assert primary["mariadb-main"]["peers"][0]["addresses"] == [
        "h2.example.internal",
        "203.0.113.12",
    ]
    # The primary's server runs first, so its replica is seeded from it
    assert pair.inventory is not None
    document = render_ansible_inventory(pair.inventory)
    children = cast("dict[str, Any]", document["all"])["children"]
    assert list(children["cloudfall_servers"]["hosts"]) == ["h1", "h2"]
    pair.declare(
        _replica_of("mariadb-replica", "mariadb-main", "h1", 1), _new_primary()
    )
    document = render_ansible_inventory(pair.inventory)
    children = cast("dict[str, Any]", document["all"])["children"]
    assert list(children["cloudfall_servers"]["hosts"]) == ["h2", "h1"]
