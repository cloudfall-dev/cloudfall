"""The ``cloudfall_mariadb`` role with ``remoteAccess`` (S-019-11, #421).

The managed file and the host's nftables ruleset are rendered from a
validated project with the templates Ansible uses, the role's decisions are
evaluated with Ansible's own templating, and its account tasks run for real
on localhost against the stand-in ``mariadb`` client: each owner gets one
account per allowed source, requiring TLS, and a second converge changes
nothing. A TLS login from another host, and a plain one refused, need two
live hosts and are listed in the pull request as unverified.
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
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.domain import IpNetworkCidr
from cloudfall.inventory import PlatformInventory
from cloudfall.mariadb import account_host, mysql_native_password_hash
from cloudfall.requirements import (
    MARIADB_CA_PATH,
    MARIADB_CERTIFICATE_PATH,
    MARIADB_KEY_PATH,
)
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from mariadb_project import SCHEMAS, mariadb_service, write_project
from mariadb_server import fresh_state, write_client

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from types import ModuleType

ROOT = Path(__file__).parents[2]
ROLES = ROOT / "engine" / "ansible" / "roles"
ROLE = ROLES / "cloudfall_mariadb"
FIREWALL = ROLES / "cloudfall_firewall"
PASSWORD = "s3cr3t-billing-value"  # noqa: S105 - the value every output is grepped for
NATIVE = mysql_native_password_hash(PASSWORD)
REMOTE_ACCESS: dict[str, object] = {
    "listenAddress": "10.0.0.2",
    "allowedSources": ["10.0.0.0/24"],
}
NETWORK_HOST = "10.0.0.0/255.255.255.0"
CRM: dict[str, object] = {"name": "crm", "application": "crm", "owner": "crm"}
BILLING: dict[str, object] = {
    "name": "billing",
    "owner": "billing",
    "passwordRef": {
        "environment": "production",
        "path": "/services/mariadb-main",
        "key": "BILLING_PASSWORD",
    },
    "authentication": "mysql_native_password",
}
# What the template wrote before remoteAccess converged (#420), for a
# service without it: the same file, byte for byte (Q-3)
STANDALONE_SETTINGS = """\
# Managed by Cloudfall for service mariadb-main; a change
# here is drift. Settings of your own go in a file that sorts before this one.
[mariadbd]
bind-address = 127.0.0.1
port = 3306
datadir = /var/lib/mysql
innodb_buffer_pool_size = 4096M
character-set-server = utf8mb4
# The data volume's ext4 root holds lost+found, which is no database.
ignore-db-dirs = lost+found
"""


def _role_service(*databases: dict[str, object], **mariadb: object) -> dict[str, Any]:
    """Return the service as the rendered inventory hands it to the role."""
    return {
        "id": "mariadb-main",
        "serviceKind": "mariadb",
        "environment": "production",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": 3306},
        "mariadb": {
            "version": "11.8.9",
            "innodbBufferPoolMb": 4096,
            "databases": list(databases),
            **mariadb,
        },
        "backup": {
            "directory": "/var/backups/cloudfall/mariadb-main",
            "onCalendar": "*-*-* 02:30:00 UTC",
            "retentionDays": 14,
        },
    }


def _defaults() -> dict[str, object]:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    assert isinstance(defaults, dict)
    return cast("dict[str, object]", defaults)


def _plugin() -> ModuleType:
    path = ROLE / "filter_plugins" / "mariadb_host.py"
    spec = importlib.util.spec_from_file_location("mariadb_host", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _filter() -> Callable[[str], str]:
    filters = _plugin().FilterModule().filters()
    return cast("Callable[[str], str]", filters["cloudfall_mariadb_host"])


def _jinja(directory: Path) -> Environment:
    """Jinja with the whitespace rules Ansible's template module applies."""
    return Environment(
        loader=FileSystemLoader(directory),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )


def _settings(service: dict[str, object]) -> str:
    defaults = _defaults()
    return (
        _jinja(ROLE / "templates")
        .get_template("90-cloudfall.cnf.j2")
        .render(
            cloudfall_mariadb_service=service,
            **{
                key: defaults[key]
                for key in (
                    "cloudfall_mariadb_data_path",
                    "cloudfall_mariadb_certificate_path",
                    "cloudfall_mariadb_key_path",
                    "cloudfall_mariadb_ca_path",
                )
            },
        )
    )


def _host(project: Path) -> dict[str, object]:
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    document = render_ansible_inventory(inventory)
    children = cast("dict[str, dict[str, object]]", document["all"])["children"]
    servers = cast("dict[str, dict[str, dict[str, object]]]", children)
    return cast("dict[str, object]", servers["cloudfall_servers"]["hosts"]["h1"])


def _rendered_service(host: dict[str, object]) -> dict[str, object]:
    services = cast("list[dict[str, object]]", host["cloudfall_services"])
    return next(service for service in services if service["id"] == "mariadb-main")


def _port_rules(host: dict[str, object], port: int) -> list[str]:
    rendered = (
        _jinja(FIREWALL / "templates")
        .get_template("nftables.conf.j2")
        .render(cloudfall_firewall_ruleset=host["cloudfall_firewall"])
    )
    lines = [line.strip() for line in rendered.splitlines()]
    return [line for line in lines if f"dport {port} " in line]


# proves: S-019-11
def test_s019_11_the_ruleset_admits_3306_from_the_allowed_sources_only(
    tmp_path: Path,
) -> None:
    opened = _host(
        write_project(tmp_path / "open", mariadb_service(remoteAccess=REMOTE_ACCESS))
    )
    closed = _host(write_project(tmp_path / "closed", mariadb_service()))

    rules = _port_rules(opened, 3306)

    assert rules == [
        'ip saddr 10.0.0.0/24 tcp dport 3306 accept comment "mariadb-main remoteAccess"'
    ]
    assert "ip saddr 10.0.0.0/24 tcp dport 3306 accept" in rules[0]
    # No unrestricted rule for 3306, and none at all without remoteAccess
    assert all(rule.startswith("ip saddr ") for rule in rules)
    assert _port_rules(closed, 3306) == []


# proves: S-019-11
def test_s019_11_the_managed_file_listens_on_loopback_and_the_listen_address_with_tls(
    tmp_path: Path,
) -> None:
    project = write_project(
        tmp_path / "project", mariadb_service(remoteAccess=REMOTE_ACCESS)
    )
    service = _rendered_service(_host(project))

    lines = [
        line for line in _settings(service).splitlines() if not line.startswith("#")
    ]

    # Loopback stays for local users; listenAddress opens to the sources
    assert "bind-address = 127.0.0.1,10.0.0.2" in lines
    assert f"ssl_cert = {MARIADB_CERTIFICATE_PATH.value}" in lines
    assert f"ssl_key = {MARIADB_KEY_PATH.value}" in lines
    assert f"ssl_ca = {MARIADB_CA_PATH.value}" in lines
    # TLS is required per account from a source, not for loopback and socket
    assert not any("require_secure_transport" in line for line in lines)
    # The role's paths are the S-011 requirement's, where the converge delivers
    defaults = _defaults()
    assert defaults["cloudfall_mariadb_certificate_path"] == (
        MARIADB_CERTIFICATE_PATH.value
    )
    assert defaults["cloudfall_mariadb_key_path"] == MARIADB_KEY_PATH.value
    assert defaults["cloudfall_mariadb_ca_path"] == MARIADB_CA_PATH.value


def test_without_remote_access_the_managed_file_is_as_before(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", mariadb_service())

    assert _settings(_rendered_service(_host(project))) == STANDALONE_SETTINGS


@pytest.mark.parametrize(
    "source",
    ["10.0.0.0/24", "10.0.0.0/8", "192.0.2.7/32", "2001:db8::5/128", "2001:db8::/64"],
)
def test_the_role_filter_gives_the_account_host_the_sdk_gives(source: str) -> None:
    assert _filter()(source) == (account_host(IpNetworkCidr(source)) or "")


def _tasks(name: str) -> list[dict[str, Any]]:
    tasks = DataLoader().load_from_file(str(ROLE / name))
    assert isinstance(tasks, list)
    return cast("list[dict[str, Any]]", tasks)


def _task(name: str, tasks: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return next(task for task in tasks if task["name"] == name)


def _trusted(value: object) -> object:
    """Trust the role's own defaults as templates, as Ansible trusts a role's files."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return {key: _trusted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_trusted(item) for item in value]
    return value


def _holds(task: dict[str, Any], service: dict[str, Any]) -> bool:
    sources = cast(
        "list[str]",
        service["mariadb"].get("remoteAccess", {}).get("allowedSources", []),
    )
    variables = {
        **{key: _trusted(value) for key, value in _defaults().items()},
        "cloudfall_server_id": "h1",
        "cloudfall_mariadb_service": service,
        # The filter Ansible loads from the role's filter_plugins
        "cloudfall_mariadb_remote_hosts": [_filter()(item) for item in sources],
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    if "when" in task and not templar.evaluate_conditional(
        trust_as_template(str(task["when"]))
    ):
        return True
    own = cast("dict[str, object]", task.get("vars", {}))
    templar = Templar(
        loader=DataLoader(),
        variables={
            **variables,
            **{key: _trusted(value) for key, value in own.items()},
        },
    )
    that = cast("dict[str, list[str]]", task["ansible.builtin.assert"])["that"]
    return all(
        templar.evaluate_conditional(trust_as_template(str(condition)))
        for condition in that
    )


@pytest.mark.parametrize(
    ("remote_access", "databases", "allowed"),
    [
        pytest.param(REMOTE_ACCESS, (CRM, BILLING), True, id="narrowed"),
        pytest.param(None, (CRM,), True, id="no remoteAccess"),
        pytest.param(
            {"listenAddress": "2001:db8::2", "allowedSources": ["2001:db8::5/128"]},
            (BILLING,),
            True,
            id="one IPv6 address",
        ),
        pytest.param(
            {**REMOTE_ACCESS, "listenAddress": "0.0.0.0"},  # noqa: S104 - refused
            (BILLING,),
            False,
            id="wildcard listener",
        ),
        pytest.param(
            {**REMOTE_ACCESS, "allowedSources": ["0.0.0.0/0"]},
            (BILLING,),
            False,
            id="every address",
        ),
        pytest.param(
            {**REMOTE_ACCESS, "allowedSources": ["10.0.0.0/24", "2001:db8::/64"]},
            (BILLING,),
            False,
            id="an IPv6 network",
        ),
        pytest.param(REMOTE_ACCESS, (CRM,), False, id="no owner"),
    ],
)
def test_s019_11_remote_access_not_narrowed_is_refused_before_any_change(
    remote_access: dict[str, object] | None,
    databases: tuple[dict[str, object], ...],
    allowed: bool,  # noqa: FBT001 - a parametrized expectation
) -> None:
    tasks = _tasks("tasks/main.yml")
    refuse = _task(
        "Refuse remote access that is not narrowed to declared sources", tasks
    )
    mariadb = {} if remote_access is None else {"remoteAccess": remote_access}

    assert _holds(refuse, _role_service(*databases, **mariadb)) is allowed
    names = [task["name"] for task in tasks]
    assert names.index(refuse["name"]) < names.index(
        "Create, grow and mount the data volume"
    )


# proves: S-019-11
def test_s019_11_the_certificate_is_installed_before_the_settings_name_it() -> None:
    main = _tasks("tasks/main.yml")
    names = [task["name"] for task in main]
    tls = _tasks("tasks/tls.yml")

    install = _task(
        "Install the server certificate for remote access and replication", main
    )
    assert install["ansible.builtin.include_tasks"] == "tls.yml"
    # Replication needs it too (#423): the link verifies it against the CA
    assert install["when"] == (
        "cloudfall_mariadb_service.mariadb.remoteAccess is defined"
        " or cloudfall_mariadb_service.mariadb.replication is defined"
    )
    assert (
        names.index("Install the pinned MariaDB server")
        < names.index(install["name"])
        < names.index("Install the managed server settings")
    )
    # Only the certificate and the CA are delivered; the key never moves
    copy = _task("Install the server certificate and the project CA", tls)
    assert copy["loop"] == "{{ cloudfall_mariadb_delivered_files }}"
    require = _task("Require the server certificate before the server uses TLS", tls)
    assert "ansible.builtin.assert" in require
    # The key is the mysql account's alone
    key = _task("Let MariaDB alone read the server key", tls)["ansible.builtin.file"]
    assert key == {
        "path": "{{ cloudfall_mariadb_key_path }}",
        "state": "file",
        "owner": "mysql",
        "group": "mysql",
        "mode": "0600",
    }
    # No task reads the key's content
    assert not any(
        "slurp" in json.dumps(task)
        or "lookup('ansible.builtin.file'" in json.dumps(task)
        for task in tls
    )


def test_a_renewed_certificate_is_loaded_without_a_restart() -> None:
    tls = _tasks("tasks/tls.yml")
    handlers = _tasks("handlers/main.yml")

    copy = _task("Install the server certificate and the project CA", tls)
    assert copy["notify"] == "Reload the MariaDB server certificate"
    reload = _task("Reload the MariaDB server certificate", handlers)
    # LOCAL: never a GTID of a replica's own (S-019, Replication)
    assert reload["ansible.builtin.command"]["argv"][-1] == "FLUSH LOCAL SSL"
    # Notified together on the first converge, the restart comes first
    assert [handler["name"] for handler in handlers] == [
        "Restart MariaDB",
        "Reload the MariaDB server certificate",
    ]


RECAP = re.compile(r"localhost\s*:\s*ok=\d+\s+changed=(\d+)\s")


def _strip_ownership(tasks: list[dict[str, Any]]) -> None:
    """Drop owner and group: the replay runs unprivileged on the controller."""
    for task in tasks:
        for module in ("ansible.builtin.file", "ansible.builtin.copy"):
            if module in task:
                task[module].pop("owner", None)
                task[module].pop("group", None)
        for key in ("block", "always"):
            if key in task:
                _strip_ownership(task[key])


class Server:
    """A replay of the role's account tasks, localhost and remote, on the stand-in."""

    def __init__(self, tmp_path: Path) -> None:
        """Write the stand-in client and the tasks."""
        self.tmp_path = tmp_path
        self.client, self.state_path, self.log = write_client(
            tmp_path / "server", fresh_state()
        )
        self.tasks = tmp_path / "tasks"
        self.tasks.mkdir()
        for name in ("accounts.yml", "remote_accounts.yml", "owner_password.yml"):
            tasks = yaml.safe_load((ROLE / "tasks" / name).read_text("utf-8"))
            _strip_ownership(tasks)
            (self.tasks / name).write_text(yaml.safe_dump(tasks), encoding="utf-8")
        self.host_state = tmp_path / "host-state"
        controller = tmp_path / "controller"
        controller.mkdir()
        self.hash_file = controller / "mariadb-main-billing.hash"
        self.hash_file.write_text(NATIVE, encoding="utf-8")

    def converge(self, service: dict[str, Any]) -> tuple[str, int]:
        """Run both task files at -vvv with --diff; return the output and changes."""
        play = [
            {
                "name": "Replay the account tasks of cloudfall_mariadb",
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "vars": {
                    **_defaults(),
                    "cloudfall_server_id": "h1",
                    "cloudfall_mariadb_service": service,
                    "cloudfall_mariadb_hash_files": {"billing": str(self.hash_file)},
                    "cloudfall_mariadb_client": str(self.client),
                    "cloudfall_mariadb_state_directory": str(self.host_state),
                },
                "tasks": [
                    {
                        "name": "Converge the declared databases and users",
                        "ansible.builtin.include_tasks": str(
                            self.tasks / "accounts.yml"
                        ),
                    },
                    {
                        "name": "Converge each owner's accounts from the sources",
                        "ansible.builtin.include_tasks": str(
                            self.tasks / "remote_accounts.yml"
                        ),
                    },
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
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout + done.stderr, int(recap[1])

    @property
    def state(self) -> dict[str, Any]:
        """Return what the server holds now."""
        return cast("dict[str, Any]", json.loads(self.state_path.read_text("utf-8")))

    def statements(self) -> list[str]:
        """Return every --execute argument and sourced file the client was given."""
        found: list[str] = []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            argv = json.loads(line)
            if argv[0] == "sourced":
                found.append(argv[1])
            else:
                found.append(argv[argv.index("--execute") + 1])
        return found

    def writes(self) -> list[str]:
        """Return the statements that change the server, not the reads."""
        return [
            text for text in self.statements() if not text.lstrip().startswith("SELECT")
        ]

    def arguments(self) -> str:
        """Return every client argument, the sourced files left out."""
        return json.dumps(
            [
                item
                for item in (
                    json.loads(line)
                    for line in self.log.read_text(encoding="utf-8").splitlines()
                )
                if item[0] != "sourced"
            ]
        )


SOURCES = {
    "listenAddress": "10.0.0.2",
    "allowedSources": ["10.0.0.0/24", "192.0.2.7/32"],
}


# proves: S-019-11
def test_s019_11_each_owner_gets_one_tls_account_per_allowed_source(
    tmp_path: Path,
) -> None:
    server = Server(tmp_path)

    output, changed = server.converge(_role_service(CRM, BILLING, remoteAccess=SOURCES))

    assert changed > 0
    remote = server.state["remote"]
    # billing from each source, with the same hash, requiring TLS: a plain
    # connection from a source is refused
    assert set(remote) == {f"billing@{NETWORK_HOST}", "billing@192.0.2.7"}
    for account in remote.values():
        assert account["plugin"] == "mysql_native_password"
        assert account["hash"] == NATIVE
        assert account["ssl"] is True
        assert account["managed"] is True
        assert account["locked"] is False
    assert sorted(server.state["remoteGrants"]) == [
        f"billing@{NETWORK_HOST}|billing",
        "billing@192.0.2.7|billing",
    ]
    # The localhost account keeps no TLS requirement: loopback stays as it was
    assert server.state["users"]["billing"]["ssl"] is False
    # crm stays socket-only: no account from any source
    assert not any(key.startswith("crm@") for key in remote)
    sourced = [text for text in server.statements() if f"'@'{NETWORK_HOST}'" in text]
    assert any(
        f"'billing'@'{NETWORK_HOST}' IDENTIFIED VIA mysql_native_password "
        f"USING '{NATIVE}' REQUIRE SSL;" in text
        for text in sourced
    )
    # Neither the password nor the hash is in the output or a client argument
    for secret in (PASSWORD, NATIVE):
        assert secret not in output
        assert secret not in server.arguments()
    assert PASSWORD not in json.dumps(server.statements())


# proves: S-019-11
def test_s019_11_a_second_converge_with_remote_access_changes_nothing(
    tmp_path: Path,
) -> None:
    server = Server(tmp_path)
    service = _role_service(CRM, BILLING, remoteAccess=SOURCES)
    server.converge(service)
    writes = len(server.writes())

    output, changed = server.converge(service)

    assert changed == 0, output
    assert len(server.writes()) == writes


def test_a_source_or_remote_access_no_longer_declared_locks_its_account(
    tmp_path: Path,
) -> None:
    server = Server(tmp_path)
    server.converge(_role_service(CRM, BILLING, remoteAccess=SOURCES))
    writes = len(server.writes())

    _, narrowed = server.converge(
        _role_service(CRM, BILLING, remoteAccess=REMOTE_ACCESS)
    )
    _, closed = server.converge(_role_service(CRM, BILLING))
    _, again = server.converge(_role_service(CRM, BILLING))

    assert server.writes()[writes:] == [
        "ALTER USER 'billing'@'192.0.2.7' ACCOUNT LOCK",
        f"ALTER USER 'billing'@'{NETWORK_HOST}' ACCOUNT LOCK",
    ]
    assert (narrowed, closed, again) == (1, 1, 0)
    remote = server.state["remote"]
    assert all(account["locked"] for account in remote.values())
    # Nothing a converge sends ever drops an account or its grants
    assert not any("DROP" in text.upper() for text in server.statements())
    assert len(server.state["remoteGrants"]) == 2
    # Declaring the source again unlocks its account, and nothing else
    _, reopened = server.converge(
        _role_service(CRM, BILLING, remoteAccess=REMOTE_ACCESS)
    )
    assert reopened == 1
    assert server.state["remote"][f"billing@{NETWORK_HOST}"]["locked"] is False
    assert server.state["remote"]["billing@192.0.2.7"]["locked"] is True


def test_without_remote_access_the_accounts_converge_only_reads_more(
    tmp_path: Path,
) -> None:
    server = Server(tmp_path)

    server.converge(_role_service(CRM, BILLING))
    _, changed = server.converge(_role_service(CRM, BILLING))

    assert server.state["remote"] == {}
    # Every account it writes is at localhost, as before remoteAccess converged
    assert not any(re.search(r"'@'(?!localhost')", text) for text in server.writes())
    assert not any("REQUIRE SSL" in text for text in server.statements())
    assert changed == 0
