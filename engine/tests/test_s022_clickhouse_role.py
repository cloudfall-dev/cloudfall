"""The ``cloudfall_clickhouse`` role for one replica (S-022, #568).

The role needs a Debian 13 host with ClickHouse, so its decisions are
evaluated with Ansible's own templating, its managed files rendered from its
templates and read back as ClickHouse reads XML, and its database and owner
tasks run for real on localhost against a stand-in ``clickhouse-client`` that
keeps the server's accounts in a file: a second converge must change nothing.
What a live host alone can prove (the packages and their hold, the
listeners ClickHouse and its Keeper open, a client's login and the engine of
the table it creates) is listed in the pull request as unverified.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar, trust_as_template
from clickhouse_client import (
    ONLY_REPLICATED,
    create_table,
    creates_users,
    fresh_state,
    logs_in,
    write_client,
)
from cloudfall.clickhouse import (
    CLOUDFALL_USER,
    MANAGED_COMMENT,
    MANAGED_ROLE,
    OWNER_PROFILE,
    SUPPORTED_SERIES,
    SYSTEM_DATABASES,
    owner_salt,
    sha256_password_hash,
)
from cloudfall.requirements import (
    CLICKHOUSE_CLIENT_CONFIG_PATH,
    CLICKHOUSE_REPLICA_PACKAGES,
    CLICKHOUSE_VOLUME_PATH,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import ModuleType

init_plugin_loader()

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_clickhouse"
PLAYBOOKS = ROOT / "engine" / "ansible" / "playbooks"
SERVICES = PLAYBOOKS / "services.yml"
CLOUDFALL_PASSWORD = "s3cr3t-cloudfall-<&>-value"  # noqa: S105 - grepped for
PASSWORD = "s3cr3t-events-value"  # noqa: S105 - grepped for
RECAP = re.compile(r"localhost\s+:\s+ok=\d+\s+changed=(\d+)")
EVENTS: dict[str, object] = {
    "name": "events",
    "owner": "events",
    "passwordRef": {
        "environment": "production",
        "path": "/services/clickhouse-main",
        "key": "EVENTS_PASSWORD",
    },
}
# The tasks of main.yml that only read or refuse; every one before the first
# task that changes the host
REFUSALS = (
    "Validate ClickHouse service target and declaration",
    "Refuse a service of two replicas or with client access",
    "Refuse a memory limit above nine tenths of the server's memory",
    "Refuse a series Cloudfall does not install",
    "Require the staged cloudfall password and every owner's hash",
    "Read the installed ClickHouse server package",
    "Refuse a version other than the installed one",
)


def _service(*databases: dict[str, object], **clickhouse: object) -> dict[str, object]:
    return {
        "id": "clickhouse-main",
        "serviceKind": "clickhouse",
        "environment": "production",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": 9000},
        "clickhouse": {
            "version": "26.8.37.168",
            "maxServerMemoryMb": 4096,
            "replicas": ["h1"],
            "keeper": {"servers": ["h1"]},
            "databases": list(databases),
            **clickhouse,
        },
        "backup": {
            "directory": "/var/backups/cloudfall/clickhouse-main",
            "onCalendar": "*-*-* 02:40:00 UTC",
            "retentionDays": 14,
        },
        "metrics": {"enabled": True},
    }


def _filters() -> ModuleType:
    path = ROLE / "filter_plugins" / "clickhouse_accounts.py"
    spec = importlib.util.spec_from_file_location("clickhouse_accounts", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FILTERS = _filters()


def _tasks(name: str) -> list[dict[str, Any]]:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / name))
    assert isinstance(tasks, list)
    return cast("list[dict[str, Any]]", tasks)


def _task(name: str, tasks: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return next(task for task in tasks if task["name"] == name)


def _defaults() -> dict[str, object]:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    assert isinstance(defaults, dict)
    return cast("dict[str, object]", defaults)


def _trusted(value: object) -> object:
    """Trust the role's own defaults as templates, as Ansible trusts a role's files."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return {key: _trusted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_trusted(item) for item in value]
    return value


def _staged(tmp_path: Path) -> dict[str, object]:
    """Write the controller's staged files as the SDK does; return their paths."""
    directory = tmp_path / "controller"
    directory.mkdir(exist_ok=True)
    password = directory / "clickhouse-main-cloudfall.password"
    password.write_text(CLOUDFALL_PASSWORD, encoding="utf-8")
    return {"cloudfallPassword": str(password), "owners": {}}


def _variables(tmp_path: Path, **extra: object) -> dict[str, object]:
    return {
        **{key: _trusted(value) for key, value in _defaults().items()},
        "ansible_check_mode": False,
        "cloudfall_server_id": "h1",
        "ansible_facts": {
            "distribution": "Debian",
            "distribution_major_version": "13",
            "distribution_release": "trixie",
            "service_mgr": "systemd",
            "memtotal_mb": 8192,
        },
        "cloudfall_clickhouse_service": _service(EVENTS),
        "cloudfall_clickhouse_files": {
            **_staged(tmp_path),
            "owners": {"events": "/tmp/staged/clickhouse-main-events.sha256"},  # noqa: S108
        },
        **extra,
    }


def _holds(task: dict[str, Any], variables: dict[str, object]) -> bool:
    that = cast("dict[str, list[str]]", task["ansible.builtin.assert"])["that"]
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(
        templar.evaluate_conditional(trust_as_template(str(condition)))
        for condition in that
    )


def _template(expression: str, variables: dict[str, object]) -> object:
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(expression))


def _render(name: str, variables: dict[str, object]) -> str:
    text = (ROLE / "templates" / name).read_text(encoding="utf-8")
    return str(_template(text, variables))


def _xml(name: str, variables: dict[str, object]) -> ET.Element:
    return ET.fromstring(_render(name, variables))  # noqa: S314 - the role's own file


def _texts(root: ET.Element, path: str) -> list[str]:
    return [str(item.text) for item in root.findall(path)]


# proves: S-022-3
def test_s022_3_the_managed_settings_are_the_declared_ones(tmp_path: Path) -> None:
    variables = _variables(tmp_path)

    listeners = _xml("90-cloudfall-listeners.xml.j2", variables)
    memory = _xml("91-cloudfall-memory.xml.j2", variables)
    keeper = _xml("92-cloudfall-keeper.xml.j2", variables)
    access = _xml("93-cloudfall-access.xml.j2", variables)

    # Clients on loopback alone: the native port of bind.port and HTTP
    assert _texts(listeners, "listen_host") == ["127.0.0.1"]
    assert _texts(listeners, "tcp_port") == ["9000"]
    assert _texts(listeners, "http_port") == ["8123"]
    # max_server_memory_usage is maxServerMemoryMb, in bytes
    assert _texts(memory, "max_server_memory_usage") == [str(4096 * 1024 * 1024)]
    # Keeper inside the server, its one voter on loopback
    assert _texts(keeper, "keeper_server/server_id") == ["1"]
    assert _texts(keeper, "keeper_server/tcp_port") == ["9181"]
    servers = keeper.findall("keeper_server/raft_configuration/server")
    assert [(_texts(item, "id"), _texts(item, "hostname")) for item in servers] == [
        (["1"], ["127.0.0.1"])
    ]
    assert _texts(keeper, "keeper_server/log_storage_path")[0].startswith(
        CLICKHOUSE_VOLUME_PATH.value
    )
    assert _texts(keeper, "zookeeper/node/host") == ["127.0.0.1"]
    # The pair it waits to form: the replica is the server, the shard the service
    assert _texts(keeper, "macros/shard") == ["clickhouse-main"]
    assert _texts(keeper, "macros/replica") == ["h1"]
    # The users made by SQL live in Keeper; cloudfall in users.d
    directories = access.find("user_directories")
    assert directories is not None
    assert directories.get("replace") == "replace"
    assert [item.tag for item in directories] == ["users_xml", "replicated"]
    assert _texts(directories, "replicated/zookeeper_path") == ["/clickhouse/access/"]
    # The managed files sort after an operator's own
    names = cast("list[str]", _defaults()["cloudfall_clickhouse_managed_config_files"])
    assert all(name.startswith("9") for name in names)


# proves: S-022-3
# proves: S-022-5
# proves: S-022-6
def test_s022_3_default_is_removed_and_cloudfall_is_loopback_only(
    tmp_path: Path,
) -> None:
    variables = _variables(tmp_path)
    # cloudfall is defined in users.d, which the users_xml directory reads
    # from the server's own files: it logs in with Keeper stopped (S-022-5),
    # while the owners made by SQL live in the replicated directory
    access = _xml("93-cloudfall-access.xml.j2", variables)
    assert _texts(access, "user_directories/users_xml/path") == ["users.xml"]

    text = _render("90-cloudfall-users.xml.j2", variables)
    users = ET.fromstring(text)  # noqa: S314 - the role's own file

    default = users.find("users/default")
    assert default is not None
    assert default.get("remove") == "remove"
    assert list(default) == []
    cloudfall = users.find(f"users/{CLOUDFALL_USER}")
    assert cloudfall is not None
    # users.d holds the SHA-256 alone, never the password (S-022-6)
    assert _texts(cloudfall, "password_sha256_hex") == [
        hashlib.sha256(CLOUDFALL_PASSWORD.encode()).hexdigest()
    ]
    assert cloudfall.find("password") is None
    assert CLOUDFALL_PASSWORD not in text
    assert _texts(cloudfall, "networks/ip") == ["127.0.0.1", "::1"]
    assert _texts(cloudfall, "access_management") == ["1"]


def test_the_client_configuration_logs_cloudfall_in_on_loopback(
    tmp_path: Path,
) -> None:
    client = _xml("cloudfall-client.xml.j2", _variables(tmp_path))

    assert _texts(client, "host") == ["127.0.0.1"]
    assert _texts(client, "port") == ["9000"]
    assert _texts(client, "user") == [CLOUDFALL_USER]
    # Escaped in the file, the password reads back as it is
    assert _texts(client, "password") == [CLOUDFALL_PASSWORD]
    assert (
        _defaults()["cloudfall_clickhouse_client_config_path"]
        == CLICKHOUSE_CLIENT_CONFIG_PATH.value
    )


# proves: S-022-3
def test_s022_3_the_packages_come_from_lts_pinned_and_held(tmp_path: Path) -> None:
    variables = _variables(tmp_path)
    main = _tasks("main.yml")

    install = _task("Install the pinned ClickHouse packages", main)
    assert _template(install["ansible.builtin.apt"]["name"], variables) == [
        "clickhouse-common-static=26.8.37.168",
        "clickhouse-client=26.8.37.168",
        "clickhouse-server=26.8.37.168",
    ]
    assert install["ansible.builtin.apt"]["policy_rc_d"] == 101
    hold = _task("Hold the ClickHouse packages", main)
    assert hold["ansible.builtin.dpkg_selections"]["selection"] == "hold"
    assert hold["loop"] == "{{ cloudfall_clickhouse_packages }}"
    assert set(cast("list[str]", _defaults()["cloudfall_clickhouse_packages"])) == {
        package.value for package in CLICKHOUSE_REPLICA_PACKAGES
    }
    repository = _task("Configure ClickHouse's LTS repository", _tasks("package.yml"))
    deb822 = repository["ansible.builtin.deb822_repository"]
    assert _template(deb822["suites"][0], variables) == "lts"
    assert _template(deb822["uris"][0], variables) == (
        "https://packages.clickhouse.com/deb"
    )


# proves: S-022-3
def test_s022_3_the_data_directory_must_be_the_clickhouse_volume(
    tmp_path: Path,
) -> None:
    actions = _template(
        cast("str", _defaults()["cloudfall_clickhouse_volume_actions"]),
        _variables(
            tmp_path,
            cloudfall_volume_actions={
                "h1": [{"name": "clickhouse"}, {"name": "mariadb"}],
            },
        ),
    )
    assert actions == [{"name": "clickhouse"}]
    refuse = _task(
        "Refuse a data directory that is not its volume", _tasks("storage.yml")
    )
    assert refuse["ansible.builtin.assert"]["that"] == [
        "cloudfall_clickhouse_data_mount.rc == 0"
    ]
    assert _defaults()["cloudfall_clickhouse_data_path"] == CLICKHOUSE_VOLUME_PATH.value
    names = [task["name"] for task in _tasks("main.yml")]
    # The volume is mounted before the packages write their first files there
    assert names.index("Prepare the data volume") < names.index(
        "Install the pinned ClickHouse packages"
    )


# proves: S-022-3
def test_s022_3_a_listener_outside_loopback_is_refused() -> None:
    server = 'users:(("clickhouse-serv",pid=9,fd=1))'
    sockets = "\n".join(
        [
            *(
                f"LISTEN 0 4096 127.0.0.1:{port} 0.0.0.0:* {server}"
                for port in (9000, 8123, 9181, 9234)
            ),
            'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))',
        ]
    )
    assert FILTERS.public_listeners(sockets) == []
    exposed = (
        sockets
        + '\nLISTEN 0 4096 [::]:9234 [::]:* users:(("clickhouse-serv",pid=9,fd=5))'
    )
    assert FILTERS.public_listeners(exposed) == ["[::]:9234 clickhouse-serv"]
    refuse = _task(
        "Refuse a listener of ClickHouse or Keeper outside loopback", _tasks("main.yml")
    )
    assert refuse["ansible.builtin.assert"]["that"] == [
        "cloudfall_clickhouse_public | length == 0"
    ]


# proves: S-022-3
def test_s022_3_the_server_must_report_the_declared_version(tmp_path: Path) -> None:
    refuse = _task(
        "Refuse a server that does not run the declared version", _tasks("main.yml")
    )
    answered = {"rc": 0, "stdout": "26.8.37.168\n"}
    assert _holds(refuse, _variables(tmp_path, cloudfall_clickhouse_version=answered))
    other = {"rc": 0, "stdout": "26.8.36.1\n"}
    assert not _holds(refuse, _variables(tmp_path, cloudfall_clickhouse_version=other))
    wait = _task("Wait until the server answers cloudfall", _tasks("main.yml"))
    argv = wait["ansible.builtin.command"]["argv"]
    assert argv[-2:] == ["--query", "SELECT version()"]
    assert "--config-file" in argv


def test_the_tables_and_names_are_the_sdks() -> None:
    defaults = _defaults()
    assert defaults["cloudfall_clickhouse_supported_series"] == [
        series.label for series in SUPPORTED_SERIES
    ]
    assert defaults["cloudfall_clickhouse_user"] == CLOUDFALL_USER
    assert defaults["cloudfall_clickhouse_managed_role"] == MANAGED_ROLE
    assert defaults["cloudfall_clickhouse_managed_comment"] == MANAGED_COMMENT
    assert defaults["cloudfall_clickhouse_owner_profile"] == OWNER_PROFILE
    assert set(
        cast("list[str]", defaults["cloudfall_clickhouse_system_databases"])
    ) == (SYSTEM_DATABASES)
    inspect = yaml.safe_load(
        (
            ROOT
            / "engine"
            / "ansible"
            / "roles"
            / "cloudfall_inspect"
            / "defaults"
            / "main.yml"
        ).read_text("utf-8")
    )
    assert inspect["cloudfall_inspect_clickhouse_client_config_path"] == (
        CLICKHOUSE_CLIENT_CONFIG_PATH.value
    )
    queries = cast("dict[str, str]", inspect["cloudfall_inspect_clickhouse_queries"])
    assert f"'{MANAGED_COMMENT}'" in queries["managedDatabases"]
    assert f"'{MANAGED_ROLE}'" in queries["managedUsers"]


# proves: S-022-4
def test_s022_4_a_memory_limit_above_nine_tenths_is_refused(tmp_path: Path) -> None:
    refuse = _task(
        "Refuse a memory limit above nine tenths of the server's memory",
        _tasks("main.yml"),
    )
    # 8192 MB: 7372 is the most nine tenths allow
    at_limit = _service(EVENTS, maxServerMemoryMb=7372)
    assert _holds(refuse, _variables(tmp_path, cloudfall_clickhouse_service=at_limit))
    above = _service(EVENTS, maxServerMemoryMb=7373)
    assert not _holds(refuse, _variables(tmp_path, cloudfall_clickhouse_service=above))


# proves: S-022-4
def test_s022_4_every_refusal_comes_before_any_task_that_changes_the_host(
    tmp_path: Path,
) -> None:
    names = [task["name"] for task in _tasks("main.yml")]
    assert names[: len(REFUSALS)] == list(REFUSALS)
    read = _task("Read the installed ClickHouse server package", _tasks("main.yml"))
    assert read["changed_when"] is False
    assert read["check_mode"] is False
    # services.yml refuses it in pre_tasks, before any service role runs
    play = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))[0]
    pre_task = _task(
        "Refuse a ClickHouse memory limit above nine tenths of the server's memory",
        play["pre_tasks"],
    )
    service = _service(EVENTS, maxServerMemoryMb=7373)
    variables = {
        **_variables(tmp_path),
        "cloudfall_services": [service],
        "cloudfall_service_item": service,
    }
    assert not _holds(pre_task, variables)
    assert _template(pre_task["loop"], variables) == [service]
    # The firewall, the first task that changes the host, comes after it
    assert (
        play["tasks"][0]["name"]
        == "Enforce the declared firewall with the services' rules"
    )
    deploy = _task("Deploy declared ClickHouse services", play["tasks"])
    assert deploy["ansible.builtin.include_role"]["name"] == "cloudfall_clickhouse"


# proves: S-022-4
def test_s022_4_the_managed_files_render_the_same_every_time(tmp_path: Path) -> None:
    variables = _variables(tmp_path)
    for name in (
        *cast("list[str]", _defaults()["cloudfall_clickhouse_managed_config_files"]),
        *cast("list[str]", _defaults()["cloudfall_clickhouse_managed_users_files"]),
        "cloudfall-client.xml",
    ):
        assert _render(f"{name}.j2", variables) == _render(f"{name}.j2", variables)
    # Only the server's settings and its packages restart it: users.d is read
    # again by the server itself, and the client configuration is cloudfall's
    main = _tasks("main.yml")
    restarting = {task["name"] for task in main if task.get("notify")}
    assert restarting == {
        "Install the pinned ClickHouse packages",
        "Install the managed server settings",
    }


def test_two_replicas_or_client_access_are_refused_for_now(tmp_path: Path) -> None:
    refuse = _task(
        "Refuse a service of two replicas or with client access", _tasks("main.yml")
    )
    assert _holds(refuse, _variables(tmp_path))
    pair = _service(
        EVENTS, replicas=["h1", "h2"], keeper={"servers": ["h1", "h2", "h3"]}
    )
    assert not _holds(refuse, _variables(tmp_path, cloudfall_clickhouse_service=pair))
    reachable = _service(EVENTS, clientAccess={"allowedSources": ["10.0.0.0/24"]})
    assert not _holds(
        refuse, _variables(tmp_path, cloudfall_clickhouse_service=reachable)
    )


def test_a_version_other_than_the_installed_one_is_refused(tmp_path: Path) -> None:
    refuse = _task("Refuse a version other than the installed one", _tasks("main.yml"))
    for stdout, holds in (
        ("", True),
        ("installed|26.8.37.168", True),
        ("installed|26.8.36.1", False),
    ):
        installed = {"rc": 0 if stdout else 1, "stdout": stdout}
        assert (
            _holds(
                refuse, _variables(tmp_path, cloudfall_clickhouse_installed=installed)
            )
            is holds
        )


# proves: S-022-6
def test_s022_6_no_secret_reaches_ansible_output() -> None:
    main = _tasks("main.yml")
    for name in (
        "Install the managed accounts",
        "Install the cloudfall account's client configuration",
    ):
        assert _task(name, main)["no_log"] is True
    (block,) = _tasks("owner_password.yml")
    for task in block["block"]:
        if task["name"] != "Refuse a staged hash of an unexpected shape":
            assert task["no_log"] is True
    (remove,) = block["always"]
    assert remove["ansible.builtin.file"]["state"] == "absent"
    # The statement is read by the client from its file, never an argument
    store = _task("Store the owner's hash", block["block"])
    assert "--queries-file" in store["ansible.builtin.command"]["argv"]


class Replica:
    """A replay of the role's database and owner tasks against the stand-in."""

    def __init__(self, tmp_path: Path, state: dict[str, object] | None = None) -> None:
        """Write the stand-in, the client configuration and the tasks."""
        self.tmp_path = tmp_path
        self.client, self.state_path, self.log = write_client(
            tmp_path / "replica", state if state is not None else fresh_state()
        )
        self.config = tmp_path / "replica" / "cloudfall-client.xml"
        self.config.write_text("<config/>\n", encoding="utf-8")
        for name in ("accounts.yml", "owner_password.yml"):
            tasks = yaml.safe_load((ROLE / "tasks" / name).read_text("utf-8"))
            _strip_ownership(tasks)
            (tmp_path / name).write_text(yaml.safe_dump(tasks), encoding="utf-8")
        self.host_state = tmp_path / "host-state"
        self.controller = tmp_path / "controller"
        self.controller.mkdir(exist_ok=True)

    def converge(
        self, service: dict[str, object], passwords: dict[str, str]
    ) -> tuple[subprocess.CompletedProcess[str], int]:
        """Run the tasks at -vvv with --diff; return the run and its change count."""
        staged = _staged(self.tmp_path)
        owners: dict[str, str] = {}
        for owner, value in passwords.items():
            salt = owner_salt(CLOUDFALL_PASSWORD, "clickhouse-main", owner)
            path = self.controller / f"clickhouse-main-{owner}.sha256"
            path.write_text(f"{sha256_password_hash(value, salt)}:{salt}", "utf-8")
            owners[owner] = str(path)
        play = [
            {
                "name": "Replay the database and owner tasks of cloudfall_clickhouse",
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "vars": {
                    **_defaults(),
                    "cloudfall_server_id": "h1",
                    "cloudfall_clickhouse_service": service,
                    "cloudfall_clickhouse_files": {**staged, "owners": owners},
                    "cloudfall_clickhouse_client": str(self.client),
                    "cloudfall_clickhouse_client_config_path": str(self.config),
                    "cloudfall_clickhouse_state_directory": str(self.host_state),
                },
                "tasks": [
                    {
                        "name": "Converge the databases and owners",
                        "ansible.builtin.include_tasks": str(
                            self.tmp_path / "accounts.yml"
                        ),
                    }
                ],
            }
        ]
        playbook = self.tmp_path / "accounts-play.yml"
        playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
        binary = shutil.which("ansible-playbook")
        assert binary is not None
        done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
            [binary, "-vvv", "--diff", "--inventory", "localhost,", str(playbook)],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            env={
                **os.environ,
                "ANSIBLE_FILTER_PLUGINS": str(ROLE / "filter_plugins"),
            },
        )
        recap = RECAP.search(done.stdout)
        assert recap is not None, done.stdout + done.stderr
        return done, int(recap[1])

    def converged(
        self, service: dict[str, object], passwords: dict[str, str]
    ) -> tuple[subprocess.CompletedProcess[str], int]:
        """Converge, and require the run to pass."""
        done, changed = self.converge(service, passwords)
        assert done.returncode == 0, done.stdout + done.stderr
        return done, changed

    @property
    def state(self) -> dict[str, Any]:
        """Return what the server holds now."""
        return cast("dict[str, Any]", json.loads(self.state_path.read_text("utf-8")))

    def write_state(self, state: dict[str, Any]) -> None:
        """Change what the server holds, as a person would by hand."""
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        """Return every argv the stand-in was given, and each queries file read."""
        return [
            cast("list[str]", json.loads(line))
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]

    def statements(self) -> list[str]:
        """Return each statement sent, the reads too, in order."""
        found: list[str] = []
        for call in self.calls():
            if call[0] == "queried":
                found.append(call[1])
            elif "--query" in call:
                found.append(call[call.index("--query") + 1])
        return found

    def writes(self) -> list[str]:
        """Return the statements that change the server, not the reads."""
        return [
            statement
            for statement in self.statements()
            if not statement.lstrip().startswith("SELECT")
        ]

    def database(self, name: str) -> dict[str, Any] | None:
        """Return one database of the server, or None."""
        return next(
            (item for item in self.state["databases"] if item["name"] == name), None
        )


def _strip_ownership(tasks: list[dict[str, Any]]) -> None:
    """Drop owner and group, which only root may set, from file tasks."""
    for task in tasks:
        for module in ("ansible.builtin.file", "ansible.builtin.copy"):
            if module in task:
                task[module].pop("owner", None)
                task[module].pop("group", None)
        for key in ("block", "always"):
            if key in task:
                _strip_ownership(task[key])


requires_ansible = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook is not on PATH"
)


def _reports() -> dict[str, object]:
    return {
        "name": "reports",
        "owner": "reports",
        "passwordRef": {
            "environment": "production",
            "path": "/services/clickhouse-main",
            "key": "REPORTS_PASSWORD",
        },
    }


# proves: S-022-3
# proves: S-022-5
@requires_ansible
def test_s022_5_a_fresh_replica_gets_its_replicated_database_and_owner(
    tmp_path: Path,
) -> None:
    replica = Replica(tmp_path)

    _, changed = replica.converged(_service(EVENTS), {"events": PASSWORD})

    assert changed > 0
    state = replica.state
    # The declared database has the Replicated engine, at a path every
    # replica shares, with Cloudfall's comment (S-022-3)
    events = replica.database("events")
    assert events == {
        "name": "events",
        "engine": "Replicated",
        "comment": MANAGED_COMMENT,
        "path": "/clickhouse/databases/events",
    }
    # events logs in on loopback with its password; default does not exist
    assert logs_in(state, "events", PASSWORD)
    assert not logs_in(state, "events", "another-value")
    assert not logs_in(state, "default", "")
    # Its MergeTree table is replicated, a Memory table is refused, another
    # database is refused, and it can't create a user
    assert create_table(state, "events", "events", "MergeTree") == (
        "ReplicatedMergeTree"
    )
    assert create_table(state, "events", "events", "Memory") is None
    assert create_table(state, "events", "default", "MergeTree") is None
    assert not creates_users(state, "events")
    assert [(row["user_name"], row["database"]) for row in state["grants"]] == [
        ("events", "events")
    ]
    # The profile that admits replicated tables alone is a constant
    assert {
        (row["profile_name"], row["setting_name"], row["value"], row["writability"])
        for row in state["settings_profile_elements"]
        if row["profile_name"] == OWNER_PROFILE
    } == {(OWNER_PROFILE, ONLY_REPLICATED, "1", "CONST")}
    assert state["role_grants"] == [
        {"user_name": "events", "granted_role_name": MANAGED_ROLE}
    ]


# proves: S-022-4
@requires_ansible
def test_s022_4_a_second_converge_changes_nothing(tmp_path: Path) -> None:
    replica = Replica(tmp_path)
    replica.converged(_service(EVENTS), {"events": PASSWORD})
    before = replica.state
    writes = len(replica.writes())

    _, changed = replica.converged(_service(EVENTS), {"events": PASSWORD})

    assert changed == 0
    assert replica.state == before
    assert len(replica.writes()) == writes


@requires_ansible
def test_a_rotated_password_sets_the_new_hash(tmp_path: Path) -> None:
    replica = Replica(tmp_path)
    replica.converged(_service(EVENTS), {"events": PASSWORD})

    _, changed = replica.converged(_service(EVENTS), {"events": "rotated-value"})

    assert changed > 0
    assert logs_in(replica.state, "events", "rotated-value")
    assert not logs_in(replica.state, "events", PASSWORD)
    assert replica.writes()[-1].startswith("ALTER USER events IDENTIFIED WITH")


# proves: S-022-6
@requires_ansible
def test_s022_6_only_a_hash_reaches_the_server_and_the_statement_is_gone(
    tmp_path: Path,
) -> None:
    replica = Replica(tmp_path)

    done, _ = replica.converged(_service(EVENTS), {"events": PASSWORD})

    salt = owner_salt(CLOUDFALL_PASSWORD, "clickhouse-main", "events")
    hashed = sha256_password_hash(PASSWORD, salt)
    queried = [call[1] for call in replica.calls() if call[0] == "queried"]
    assert queried == [
        (
            f"CREATE USER events IDENTIFIED WITH sha256_hash BY '{hashed}' "
            f"SALT '{salt}' HOST LOCAL SETTINGS PROFILE '{OWNER_PROFILE}'"
        )
    ]
    # The staged statement is removed; only the digests of the hashes stay
    assert sorted(path.name for path in replica.host_state.iterdir()) == [
        "clickhouse-main-owners.json"
    ]
    for secret in (PASSWORD, CLOUDFALL_PASSWORD, hashed):
        assert secret not in done.stdout
        assert secret not in done.stderr
        assert all(
            secret not in " ".join(call)
            for call in replica.calls()
            if call[0] != "queried"
        )
        assert secret not in (
            replica.host_state / "clickhouse-main-owners.json"
        ).read_text("utf-8")
    assert PASSWORD not in queried[0]


# proves: S-022-7
@requires_ansible
def test_s022_7_a_removed_database_is_kept_and_its_owner_locked_out(
    tmp_path: Path,
) -> None:
    replica = Replica(tmp_path)
    replica.converged(
        _service(EVENTS, _reports()), {"events": PASSWORD, "reports": "r-value"}
    )
    state = replica.state
    state["tables"]["reports"] = [{"name": "daily", "engine": "ReplicatedMergeTree"}]
    # A user created by hand, and its database
    state["users"].append(
        {
            "name": "ops",
            "host_ip": [],
            "host_names": ["localhost"],
            "host_names_regexp": [],
            "host_names_like": [],
            "auth": {"hash": sha256_password_hash("ops-value", "00"), "salt": "00"},
        }
    )
    state["databases"].append({"name": "scratch", "engine": "Atomic", "comment": ""})
    replica.write_state(state)

    done, changed = replica.converged(_service(EVENTS), {"events": PASSWORD})

    assert changed > 0
    # The database and its table stay; dropping is never a converge's side effect
    assert replica.database("reports") is not None
    assert replica.state["tables"]["reports"] == [
        {"name": "daily", "engine": "ReplicatedMergeTree"}
    ]
    assert not any("DROP" in statement for statement in replica.writes())
    # The owner can no longer log in, and is not dropped
    assert not logs_in(replica.state, "reports", "r-value")
    assert "ALTER USER reports HOST NONE" in replica.writes()
    # A user made by hand is left alone
    assert logs_in(replica.state, "ops", "ops-value")
    assert "databases reports, users reports" in done.stdout
    assert "databases scratch, users ops" in done.stdout
    # And the next converge changes nothing
    _, again = replica.converged(_service(EVENTS), {"events": PASSWORD})
    assert again == 0


def _inspect(replica: Replica, tmp_path: Path) -> list[dict[str, object]]:
    """Run inspect's ClickHouse tasks against the replica; return their evidence."""
    inspect = ROOT / "engine" / "ansible" / "roles" / "cloudfall_inspect"
    defaults = yaml.safe_load((inspect / "defaults" / "main.yml").read_text("utf-8"))
    evidence = tmp_path / "evidence.json"
    play = [
        {
            "name": "Replay inspect's ClickHouse account tasks",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {
                "cloudfall_inspect_clickhouse_client": str(replica.client),
                "cloudfall_inspect_clickhouse_client_config_path": str(replica.config),
                "cloudfall_inspect_clickhouse_queries": defaults[
                    "cloudfall_inspect_clickhouse_queries"
                ],
                "cloudfall_inspect_clickhouse_service": _service(EVENTS),
            },
            "tasks": [
                {
                    "name": "Collect ClickHouse account evidence",
                    "ansible.builtin.include_tasks": str(
                        inspect / "tasks" / "clickhouse_accounts.yml"
                    ),
                },
                {
                    "name": "Write the evidence",
                    "ansible.builtin.copy": {
                        "content": (
                            "{{ cloudfall_inspect_clickhouse_accounts | to_json }}"
                        ),
                        "dest": str(evidence),
                        "mode": "0600",
                    },
                },
            ],
        }
    ]
    playbook = tmp_path / "inspect-play.yml"
    playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
    binary = shutil.which("ansible-playbook")
    assert binary is not None
    done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [binary, "--inventory", "localhost,", str(playbook)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return cast("list[dict[str, object]]", json.loads(evidence.read_text("utf-8")))


# proves: S-022-7
@requires_ansible
def test_s022_7_inspect_reads_what_audit_lists(tmp_path: Path) -> None:
    replica = Replica(tmp_path)
    replica.converged(
        _service(EVENTS, _reports()), {"events": PASSWORD, "reports": "r-value"}
    )
    replica.converged(_service(EVENTS), {"events": PASSWORD})

    (evidence,) = _inspect(replica, tmp_path)

    assert evidence == {
        "service": "clickhouse-main",
        "available": True,
        "databases": [
            "INFORMATION_SCHEMA",
            "default",
            "events",
            "information_schema",
            "reports",
            "system",
        ],
        "managedDatabases": ["events", "reports"],
        "users": ["cloudfall", "events", "reports"],
        "managedUsers": ["events", "reports"],
        "lockedUsers": ["reports"],
    }


@requires_ansible
def test_a_declared_database_that_is_not_replicated_is_refused(
    tmp_path: Path,
) -> None:
    state = fresh_state()
    databases = cast("list[dict[str, object]]", state["databases"])
    databases.append({"name": "events", "engine": "Atomic", "comment": ""})
    replica = Replica(tmp_path, state)

    done, _ = replica.converge(_service(EVENTS), {"events": PASSWORD})

    assert done.returncode != 0
    assert "database events exists with the Atomic engine" in done.stdout
    assert replica.writes() == []
    assert replica.state == state


@requires_ansible
def test_an_owner_grant_elsewhere_is_revoked(tmp_path: Path) -> None:
    replica = Replica(tmp_path)
    replica.converged(_service(EVENTS), {"events": PASSWORD})
    state = replica.state
    state["grants"].append(
        {"user_name": "events", "database": "default", "is_partial_revoke": 0}
    )
    replica.write_state(state)

    replica.converged(_service(EVENTS), {"events": PASSWORD})

    assert [(row["user_name"], row["database"]) for row in replica.state["grants"]] == [
        ("events", "events")
    ]


def test_services_yml_converges_the_clickhouse_kind(tmp_path: Path) -> None:
    play = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))[0]
    refuse = _task(
        "Refuse a service of a kind this playbook does not converge", play["pre_tasks"]
    )
    known = {"cloudfall_services": [_service(EVENTS)]}
    assert _holds(refuse, {**_variables(tmp_path), **known})
    deploy = _task("Deploy declared ClickHouse services", play["tasks"])
    files = _template(
        deploy["vars"]["cloudfall_clickhouse_files"],
        {
            "cloudfall_service_item": _service(EVENTS),
            "cloudfall_services_clickhouse_files": {"clickhouse-main": {"a": "b"}},
        },
    )
    assert files == {"a": "b"}
