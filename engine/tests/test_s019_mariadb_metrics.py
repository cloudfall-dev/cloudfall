"""MariaDB metrics and the evidence audit reads (S-019-19, S-019-20, #425).

The collector configuration is rendered from a validated project with the
template Ansible uses; the services dashboard is read as shipped; the role's
metrics account tasks run for real on localhost against the stand-in
``mariadb`` client; and inspect's reading of ``SHOW SLAVE STATUS`` is
evaluated with Ansible's own templating. A live scrape needs a host with
Alloy and MariaDB and is listed in the pull request as unverified.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml
from ansible.template import Templar, trust_as_template
from cloudfall.inventory import PlatformInventory
from cloudfall.mariadb import METRICS_USER
from cloudfall.validation import validate_config
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from mariadb_project import (
    EXAMPLES,
    SCHEMAS,
    mariadb_service,
    primary_replication,
    replica_service,
    write_project,
)
from mariadb_server import fresh_state, write_client

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import ModuleType

ROOT = Path(__file__).parents[2]
ROLES = ROOT / "engine" / "ansible" / "roles"
ROLE = ROLES / "cloudfall_mariadb"
COLLECTOR = ROLES / "cloudfall_logging_collector"
INSPECT = ROLES / "cloudfall_inspect"
DASHBOARDS = ROLES / "cloudfall_logging_backend" / "files" / "dashboards"
SERVICES_DASHBOARD = DASHBOARDS / "cloudfall-services.json"
RECAP = re.compile(r"localhost\s*:\s*ok=\d+\s+changed=(\d+)\s")


def _yaml(path: Path) -> Any:  # noqa: ANN401 - a YAML document of any shape.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _trusted(value: object) -> object:
    """Trust a role's own files as templates, as Ansible trusts them."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return {key: _trusted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_trusted(item) for item in value]
    return value


def _logging_stack() -> dict[str, object]:
    inventory = PlatformInventory.from_state(validate_config(EXAMPLES, SCHEMAS))
    return inventory.logging_stacks[0].as_dict()


def _services(project: Path) -> list[dict[str, object]]:
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    return [service.as_dict() for service in inventory.services]


def _collector(services: list[dict[str, object]], server: str) -> str:
    """Render the collector configuration of ``server`` as the role writes it."""
    environment = Environment(
        loader=FileSystemLoader(COLLECTOR / "templates"),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
    )
    context = {
        **_yaml(COLLECTOR / "defaults" / "main.yml"),
        "cloudfall_logging_collector_stack": _logging_stack(),
        "cloudfall_server_id": server,
        "cloudfall_services": [
            service for service in services if service["server"] == server
        ],
    }
    return environment.get_template("config.alloy.j2").render(**context)


# proves: S-019-20
def test_s019_20_metrics_render_one_mysql_exporter_labelled_with_the_service(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", mariadb_service())

    rendered = _collector(_services(project), "h1")

    assert rendered.count("prometheus.exporter.mysql ") == 1
    assert 'prometheus.exporter.mysql "mariadb_main"' in rendered
    # Alloy's Linux user through the server's socket: no password anywhere
    assert 'data_source_name = "alloy@unix(/run/mysqld/mysqld.sock)/"' in rendered
    assert "password" not in rendered
    assert (
        "targets         = prometheus.exporter.mysql.mariadb_main.targets" in rendered
    )
    assert "forward_to      = [prometheus.relabel.mariadb_main.receiver]" in rendered
    relabel = rendered.split('prometheus.relabel "mariadb_main"', 1)[1].split("}\n", 2)
    assert 'target_label = "service"' in relabel[0] + relabel[1]
    assert 'replacement  = "mariadb-main"' in relabel[0] + relabel[1]


# proves: S-019-20
def test_s019_20_no_mysql_exporter_without_metrics(tmp_path: Path) -> None:
    service = mariadb_service()
    spec = cast("dict[str, object]", service["spec"])
    del spec["metrics"]
    project = write_project(tmp_path / "project", service)

    rendered = _collector(_services(project), "h1")

    assert "prometheus.exporter.mysql" not in rendered
    assert "mariadb_main" not in rendered


def test_each_server_of_a_pair_scrapes_only_its_own_service(tmp_path: Path) -> None:
    project = write_project(
        tmp_path / "project",
        mariadb_service(replication=primary_replication()),
        replica_service(),
        servers=("h1", "h2"),
    )
    services = _services(project)

    primary = _collector(services, "h1")
    replica = _collector(services, "h2")

    assert primary.count("prometheus.exporter.mysql ") == 1
    assert 'replacement  = "mariadb-main"' in primary
    assert replica.count("prometheus.exporter.mysql ") == 1
    assert 'replacement  = "mariadb-replica"' in replica


def test_the_exporter_account_is_the_one_the_role_makes() -> None:
    collector = _yaml(COLLECTOR / "defaults" / "main.yml")
    role = _yaml(ROLE / "defaults" / "main.yml")

    assert collector["cloudfall_logging_collector_metrics_role"] == METRICS_USER
    assert role["cloudfall_mariadb_metrics_user"] == METRICS_USER
    # Never declared, locked or listed as unmanaged by the accounts converge
    assert METRICS_USER in role["cloudfall_mariadb_system_users"]


# proves: S-019-20
def test_s019_20_the_services_dashboard_shows_replication_lag() -> None:
    dashboard = json.loads(SERVICES_DASHBOARD.read_text(encoding="utf-8"))
    panels = {panel["title"]: panel for panel in dashboard["panels"]}

    lag = panels["MariaDB replication lag"]
    (target,) = lag["targets"]
    assert target["expr"] == (
        "max by (service) (mysql_slave_status_seconds_behind_master"
        '{environment="$environment", service=~"$service"})'
    )
    assert lag["fieldConfig"]["defaults"]["unit"] == "s"
    assert {"MariaDB up", "MariaDB connections", "MariaDB queries"} <= set(panels)
    (service,) = (
        variable
        for variable in dashboard["templating"]["list"]
        if variable["name"] == "service"
    )
    assert "mysql_up" in service["definition"]


def _strip_ownership(tasks: list[dict[str, Any]]) -> None:
    for task in tasks:
        for module in ("ansible.builtin.file", "ansible.builtin.copy"):
            if module in task:
                task[module].pop("owner", None)
                task[module].pop("group", None)


class Server:
    """A replay of the role's metrics account tasks against the stand-in."""

    def __init__(self, tmp_path: Path, state: dict[str, object] | None = None) -> None:
        """Write the stand-in client and the tasks."""
        self.tmp_path = tmp_path
        self.client, self.state_path, self.log = write_client(
            tmp_path / "server", state if state is not None else fresh_state()
        )
        self.tasks = tmp_path / "metrics.yml"
        tasks = _yaml(ROLE / "tasks" / "metrics.yml")
        _strip_ownership(tasks)
        self.tasks.write_text(yaml.safe_dump(tasks), encoding="utf-8")

    def converge(self) -> tuple[subprocess.CompletedProcess[str], int]:
        """Run the tasks; return the run and its change count."""
        play = [
            {
                "name": "Replay the metrics account tasks of cloudfall_mariadb",
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "vars": {
                    **_yaml(ROLE / "defaults" / "main.yml"),
                    "cloudfall_mariadb_service": mariadb_service()["spec"],
                    "cloudfall_mariadb_client": str(self.client),
                },
                "tasks": [
                    {
                        "name": "Converge the metrics account",
                        "ansible.builtin.include_tasks": str(self.tasks),
                    }
                ],
            }
        ]
        playbook = self.tmp_path / "metrics-play.yml"
        playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
        binary = shutil.which("ansible-playbook")
        assert binary is not None
        done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
            [binary, "--diff", "--inventory", "localhost,", str(playbook)],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            env=dict(os.environ),
        )
        recap = RECAP.search(done.stdout)
        assert recap is not None, done.stdout + done.stderr
        assert done.returncode == 0, done.stdout + done.stderr
        return done, int(recap[1])

    @property
    def state(self) -> dict[str, Any]:
        """Return what the server holds now."""
        return cast("dict[str, Any]", json.loads(self.state_path.read_text("utf-8")))

    def writes(self) -> list[str]:
        """Return the statements that change the server, not the reads."""
        found = [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]
        return [
            argv[argv.index("--execute") + 1]
            for argv in found
            if not argv[argv.index("--execute") + 1].lstrip().startswith("SELECT")
        ]


# proves: S-019-4
def test_s019_4_the_metrics_account_is_made_once_and_a_second_converge_changes_nothing(
    tmp_path: Path,
) -> None:
    server = Server(tmp_path)

    _, first = server.converge()
    writes = server.writes()
    _, second = server.converge()

    assert first == 2
    assert second == 0
    assert server.writes() == writes
    alloy = server.state["users"][METRICS_USER]
    # Socket-only: Alloy's Linux user, no password or hash
    assert alloy["plugin"] == "unix_socket"
    assert alloy["hash"] == ""
    # Status and replication state, nothing else
    assert server.state["privileges"] == {METRICS_USER: ["PROCESS", "SLAVE MONITOR"]}
    # Every write is outside the binary log, so nothing of it replicates
    assert all(text.startswith("SET SESSION sql_log_bin = 0;") for text in writes)
    assert len(server.state["unlogged"]) == len(writes)


def test_a_metrics_account_made_by_hand_is_turned_socket_only(tmp_path: Path) -> None:
    state = fresh_state()
    users = cast("dict[str, dict[str, object]]", state["users"])
    users[METRICS_USER] = {
        "plugin": "mysql_native_password",
        "hash": "*" + "C" * 40,
        "authOr": False,
        "locked": False,
        "managed": False,
    }
    server = Server(tmp_path, state)

    _, changed = server.converge()

    assert changed == 2
    assert server.state["users"][METRICS_USER]["plugin"] == "unix_socket"
    assert server.state["users"][METRICS_USER]["hash"] == ""


def _include(name: str) -> dict[str, Any]:
    tasks = cast("list[dict[str, Any]]", _yaml(ROLE / "tasks" / "main.yml"))
    return next(task for task in tasks if task["name"] == name)


def test_the_metrics_account_is_made_on_a_replica_too_and_only_with_metrics() -> None:
    include = _include("Converge the account the collector scrapes the server as")

    assert include["ansible.builtin.include_tasks"] == "metrics.yml"
    conditions = include["when"]
    assert "cloudfall_mariadb_service.metrics is defined" in conditions
    assert not any("writer" in condition for condition in conditions)


SLAVE_STATUS = """\
*************************** 1. row ***************************
                Slave_IO_State: Waiting for master to send event
                   Master_Host: 203.0.113.11
                   Master_User: cloudfall_repl
                   Master_Port: 3306
              Slave_IO_Running: Yes
             Slave_SQL_Running: Yes
         Seconds_Behind_Master: 42
                 Last_IO_Error:
              Master_Server_Id: 1
       Slave_SQL_Running_State: Slave has read all relay log
"""


def _inspected(
    read_only: dict[str, object], link: dict[str, object]
) -> dict[str, object]:
    """Evaluate the evidence inspect attaches, from what the client answered."""
    tasks = cast("Iterable[dict[str, Any]]", _yaml(INSPECT / "tasks" / "mariadb.yml"))
    (attach,) = (
        task for task in tasks if task["name"] == "Attach MariaDB server evidence"
    )
    variables = {
        **cast("dict[str, object]", _trusted(_yaml(INSPECT / "defaults" / "main.yml"))),
        **cast("dict[str, object]", _trusted(attach["vars"])),
        "cloudfall_inspect_mariadb_service": {"id": "mariadb-replica"},
        "cloudfall_inspect_mariadb_settings_file": {"stat": {"isreg": True}},
        "cloudfall_inspect_mariadb_settings_content": {"content": "W21hcmlhZGJkXQo="},
        "cloudfall_inspect_mariadb_read_only": read_only,
        "cloudfall_inspect_mariadb_link": link,
    }
    templar = Templar(variables=variables)
    evidence = templar.template(
        trust_as_template("{{ cloudfall_inspect_mariadb_evidence }}")
    )
    assert isinstance(evidence, dict)
    return cast("dict[str, object]", evidence)


def _answer(stdout: str, rc: int = 0) -> dict[str, object]:
    return {"rc": rc, "stdout": stdout, "stdout_lines": stdout.splitlines()}


# proves: S-019-19
def test_s019_19_inspect_reads_the_replicas_threads_lag_and_read_only() -> None:
    evidence = _inspected(_answer("1\n"), _answer(SLAVE_STATUS))

    assert evidence == {
        "service": "mariadb-replica",
        "configurationPath": "/etc/mysql/mariadb.conf.d/90-cloudfall.cnf",
        "configuration": "[mariadbd]\n",
        "available": True,
        "readOnly": True,
        "replication": {
            "ioRunning": "Yes",
            "sqlRunning": "Yes",
            "secondsBehindMaster": 42,
            "sourceServerId": 1,
        },
    }


@pytest.mark.parametrize(
    ("read_only", "link", "expected"),
    [
        pytest.param(
            _answer("1\n"),
            _answer(
                SLAVE_STATUS.replace("Slave_IO_Running: Yes", "Slave_IO_Running: No")
                .replace("Slave_SQL_Running: Yes", "Slave_SQL_Running: No")
                .replace("Seconds_Behind_Master: 42", "Seconds_Behind_Master: NULL")
            ),
            {
                "readOnly": True,
                "replication": {
                    "ioRunning": "No",
                    "sqlRunning": "No",
                    "secondsBehindMaster": None,
                    "sourceServerId": 1,
                },
            },
            id="stopped replication",
        ),
        pytest.param(
            _answer("0\n"),
            _answer(""),
            {"readOnly": False, "replication": None},
            id="a writable server with no link",
        ),
        pytest.param(
            _answer("", rc=1),
            _answer("", rc=1),
            {"available": False, "readOnly": None, "replication": None},
            id="a server that does not answer",
        ),
    ],
)
def test_inspect_reads_a_stopped_link_no_link_and_no_answer(
    read_only: dict[str, object],
    link: dict[str, object],
    expected: dict[str, object],
) -> None:
    evidence = _inspected(read_only, link)

    assert {key: evidence[key] for key in expected} == expected


def _replication_filter() -> ModuleType:
    path = ROLE / "filter_plugins" / "mariadb_replication.py"
    spec = importlib.util.spec_from_file_location("mariadb_replication", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_only_accepted(read_only: dict[str, object]) -> bool:
    """Evaluate inspect's check of the read_only it read, as Ansible would."""
    tasks = cast("Iterable[dict[str, Any]]", _yaml(INSPECT / "tasks" / "mariadb.yml"))
    name = "Check the MariaDB server's read_only is a value inspect reads"
    (check,) = (task for task in tasks if task["name"] == name)
    defaults = _trusted(_yaml(INSPECT / "defaults" / "main.yml"))
    templar = Templar(
        variables={
            **cast("dict[str, object]", defaults),
            "cloudfall_inspect_mariadb_read_only": read_only,
        }
    )
    if not templar.evaluate_conditional(trust_as_template(check["when"])):
        return True
    return all(
        templar.evaluate_conditional(trust_as_template(condition))
        for condition in check["ansible.builtin.assert"]["that"]
    )


# proves: S-019-19
@pytest.mark.parametrize(
    ("reported", "read_only"),
    [
        # the older series' boolean
        ("1", True),
        ("0", False),
        # 11.8's enum by name (#450), and by its index
        ("ON", True),
        ("OFF", False),
        ("NO_LOCK", True),
        ("NO_LOCK_NO_ADMIN", True),
        ("2", True),
        ("3", True),
    ],
)
def test_inspect_reads_read_only_in_every_form_the_role_reads(
    reported: str,
    read_only: bool,  # noqa: FBT001 - the parametrized reading
) -> None:
    answer = _answer(f"{reported}\n")

    evidence = _inspected(answer, _answer(""))

    assert evidence["available"] is True
    assert evidence["readOnly"] is read_only
    assert _read_only_accepted(answer)


def test_inspect_fails_on_a_read_only_it_does_not_know() -> None:
    # Not a server that is down: audit would call it unavailable
    assert not _read_only_accepted(_answer("YES\n"))
    assert not _read_only_accepted(_answer("\n"))
    # A server that does not answer is read as unavailable, not checked
    assert _read_only_accepted(_answer("", rc=1))


def test_inspect_reads_read_only_as_the_role_does() -> None:
    # The role's filter and inspect take the same values the same way (#450):
    # read-only "1" is True, writable "0" is False
    role = _replication_filter()
    inspect = _yaml(INSPECT / "defaults" / "main.yml")[
        "cloudfall_inspect_mariadb_read_only_values"
    ]

    assert {key: role.read_only(key) == "1" for key in inspect} == inspect
    assert set(inspect) == set(role._READ_ONLY)  # noqa: SLF001 - the role's table
