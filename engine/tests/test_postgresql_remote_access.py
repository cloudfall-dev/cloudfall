"""A PostgreSQL service opened to declared sources over TLS (S-006-8, S-006-10).

The service's files and the host's nftables ruleset are rendered from a
validated project with the templates Ansible uses, the role's decisions are
evaluated with Ansible's own templating, and the ``pg_hba.conf`` tasks run for
real on localhost against a copy of Debian's file with a stand-in for ``psql``.
S-006-9 (``psql`` across two hosts) is left for the live proving run.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.domain import IpNetworkCidr
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ROLES = ROOT / "engine" / "ansible" / "roles"
POSTGRESQL = ROLES / "cloudfall_postgresql"
FIREWALL = ROLES / "cloudfall_firewall"
REMOTE_ACCESS: dict[str, object] = {
    "listenAddress": "10.0.0.2",
    "allowedSources": ["10.0.0.0/24"],
}
BILLING: dict[str, object] = {
    "name": "billing",
    "owner": "billing",
    "passwordRef": {
        "environment": "production",
        "path": "/billing/postgresql",
        "key": "BILLING_PASSWORD",
    },
}
HBA_HEAD = """\
# Database administrative login by Unix domain socket
local   all             postgres                                peer

# TYPE  DATABASE        USER            ADDRESS                 METHOD
local   all             all                                     peer
host    all             all             127.0.0.1/32            scram-sha-256
host    all             all             ::1/128                 scram-sha-256
"""
HBA_TAIL = """\
local   replication     all                                     peer
host    replication     all             127.0.0.1/32            scram-sha-256
"""
DEBIAN_HBA = HBA_HEAD + HBA_TAIL


def _project(
    tmp_path: Path,
    remote_access: dict[str, object] | None,
    databases: list[dict[str, object]] | None = None,
) -> Path:
    """Copy the example project and give postgresql-main an owned database."""
    project = tmp_path / "project"
    if not project.exists():
        shutil.copytree(EXAMPLES, project)
    path = project / "services" / "postgresql-main.yaml"
    example = EXAMPLES / "services" / "postgresql-main.yaml"
    service = yaml.safe_load(example.read_text(encoding="utf-8"))
    postgresql = service["spec"]["postgresql"]
    if databases is None:
        databases = [*postgresql["databases"], BILLING]
    postgresql["databases"] = databases
    if remote_access is not None:
        postgresql["remoteAccess"] = remote_access
    path.write_text(yaml.safe_dump(service, sort_keys=False), encoding="utf-8")
    return project


def _inventory(project: Path) -> PlatformInventory:
    return PlatformInventory.from_state(validate_config(project, SCHEMAS))


def _host(inventory: PlatformInventory, host: str = "h1") -> dict[str, object]:
    document = render_ansible_inventory(inventory)
    children = cast("dict[str, dict[str, object]]", document["all"])["children"]
    servers = cast("dict[str, dict[str, dict[str, object]]]", children)
    return cast("dict[str, object]", servers["cloudfall_servers"]["hosts"][host])


def _service(host: dict[str, object]) -> dict[str, object]:
    services = cast("list[dict[str, object]]", host["cloudfall_services"])
    return next(service for service in services if service["id"] == "postgresql-main")


def _environment(role: Path) -> Environment:
    """Jinja with the whitespace rules Ansible's template module applies."""
    return Environment(
        loader=FileSystemLoader(role / "templates"),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )


def _defaults() -> dict[str, object]:
    defaults = yaml.safe_load(
        (POSTGRESQL / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    assert isinstance(defaults, dict)
    return defaults


def _postgresql_file(template: str, service: dict[str, object]) -> str:
    return (
        _environment(POSTGRESQL)
        .get_template(template)
        .render(cloudfall_postgresql_service=service, **_defaults())
    )


def _nftables(host: dict[str, object]) -> list[str]:
    rendered = (
        _environment(FIREWALL)
        .get_template("nftables.conf.j2")
        .render(cloudfall_firewall_ruleset=host["cloudfall_firewall"])
    )
    return [line.strip() for line in rendered.splitlines()]


def _port_rules(lines: list[str], port: int) -> list[str]:
    return [line for line in lines if f"dport {port} " in line]


# proves: S-006-8
def test_s006_8_remote_access_renders_listener_tls_hba_and_firewall(
    tmp_path: Path,
) -> None:
    host = _host(_inventory(_project(tmp_path, REMOTE_ACCESS)))
    service = _service(host)

    conf = _postgresql_file("cloudfall.conf.j2", service).splitlines()
    hba = _postgresql_file("pg_hba-remote-access.j2", service).splitlines()
    rules = _port_rules(_nftables(host), 5432)

    assert "listen_addresses = '127.0.0.1,10.0.0.2'" in conf
    assert "ssl = on" in conf
    assert "ssl_cert_file = '/etc/ssl/certs/ssl-cert-snakeoil.pem'" in conf
    assert "ssl_key_file = '/etc/ssl/private/ssl-cert-snakeoil.key'" in conf
    # postgresql-main's application database (crm) stays socket-only.
    assert hba == ["hostssl billing billing 10.0.0.0/24 scram-sha-256"]
    assert rules == [
        'ip saddr 10.0.0.0/24 tcp dport 5432 accept comment '
        '"postgresql-main remoteAccess"'
    ]
    assert all(rule.startswith("ip saddr ") for rule in rules)


# proves: S-006-8
def test_s006_8_the_firewall_entry_carries_its_sources(tmp_path: Path) -> None:
    host = _host(_inventory(_project(tmp_path, REMOTE_ACCESS)))
    firewall = cast("dict[str, list[dict[str, object]]]", host["cloudfall_firewall"])

    assert firewall["allowedInbound"][-1] == {
        "port": 5432,
        "protocol": "tcp",
        "sources": ["10.0.0.0/24"],
        "description": "postgresql-main remoteAccess",
    }
    unrestricted = [
        rule for rule in firewall["allowedInbound"] if "sources" not in rule
    ]
    assert [rule["port"] for rule in unrestricted] == [22, 80, 443]


def test_one_rule_and_one_hba_line_per_source_and_owned_database(
    tmp_path: Path,
) -> None:
    sources: dict[str, object] = {
        "listenAddress": "10.0.0.2",
        "allowedSources": ["10.0.0.0/24", "2001:db8::/64"],
    }
    reports = {**BILLING, "name": "reports"}
    project = _project(tmp_path, sources, databases=[BILLING, reports])
    host = _host(_inventory(project))

    hba = _postgresql_file("pg_hba-remote-access.j2", _service(host)).splitlines()
    rules = _port_rules(_nftables(host), 5432)

    assert hba == [
        "hostssl billing billing 10.0.0.0/24 scram-sha-256",
        "hostssl billing billing 2001:db8::/64 scram-sha-256",
        "hostssl reports billing 10.0.0.0/24 scram-sha-256",
        "hostssl reports billing 2001:db8::/64 scram-sha-256",
    ]
    assert [rule.split(" accept")[0] for rule in rules] == [
        "ip saddr 10.0.0.0/24 tcp dport 5432",
        "ip6 saddr 2001:db8::/64 tcp dport 5432",
    ]


def test_an_ipv6_listen_address_joins_the_loopback(tmp_path: Path) -> None:
    access: dict[str, object] = {
        "listenAddress": "2001:db8::2",
        "allowedSources": ["2001:db8::/64"],
    }
    host = _host(_inventory(_project(tmp_path, access)))

    conf = _postgresql_file("cloudfall.conf.j2", _service(host)).splitlines()

    assert "listen_addresses = '127.0.0.1,2001:db8::2'" in conf


# proves: S-006-10
def test_s006_10_without_remote_access_the_service_renders_closed(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, REMOTE_ACCESS)
    _project(tmp_path, None)
    host = _host(_inventory(project))
    service = _service(host)

    conf = _postgresql_file("cloudfall.conf.j2", service).splitlines()

    assert conf == [
        "# Managed by Cloudfall. Local changes will be overwritten.",
        "listen_addresses = '127.0.0.1'",
        "port = 5432",
    ]
    assert _port_rules(_nftables(host), 5432) == []
    hba = next(
        task
        for task in _block_tasks()
        if task["name"] == "Write or remove the Cloudfall block in pg_hba.conf"
    )
    state = cast("dict[str, str]", hba["ansible.builtin.blockinfile"])["state"]
    templar = Templar(
        loader=DataLoader(), variables={"cloudfall_postgresql_service": service}
    )
    assert templar.template(trust_as_template(state)) == "absent"


def test_the_renderer_refuses_a_source_of_every_address(tmp_path: Path) -> None:
    inventory = _inventory(_project(tmp_path, REMOTE_ACCESS))
    services = []
    for service in inventory.services:
        if service.postgresql is not None and service.postgresql.remote_access:
            open_access = dataclasses.replace(
                service.postgresql.remote_access,
                allowed_sources=(IpNetworkCidr("0.0.0.0/0"),),
            )
            service = dataclasses.replace(  # noqa: PLW2901 - rebuilt in place.
                service,
                postgresql=dataclasses.replace(
                    service.postgresql, remote_access=open_access
                ),
            )
        services.append(service)

    with pytest.raises(ValueError, match="every address"):
        render_ansible_inventory(
            dataclasses.replace(inventory, services=tuple(services))
        )


def _main_tasks() -> list[dict[str, object]]:
    tasks = DataLoader().load_from_file(str(POSTGRESQL / "tasks" / "main.yml"))
    assert isinstance(tasks, list)
    return tasks


def _block_tasks() -> list[dict[str, object]]:
    block = next(
        task
        for task in _main_tasks()
        if task["name"] == "Converge the remote access block of pg_hba.conf"
    )
    return cast("list[dict[str, object]]", block["block"])


def _holds(expressions: list[str], variables: dict[str, object]) -> bool:
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(
        templar.evaluate_conditional(trust_as_template(expression))
        for expression in expressions
    )


def _remote_access_guard() -> tuple[list[str], dict[str, object]]:
    task = next(
        task
        for task in _main_tasks()
        if task["name"]
        == "Refuse remote access that is not narrowed to declared sources"
    )
    that = cast("dict[str, list[str]]", task["ansible.builtin.assert"])["that"]
    return that, cast("dict[str, object]", task["vars"])


def _guard_service(
    access: dict[str, object], databases: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "id": "postgresql-main",
        "postgresql": {"databases": databases or [BILLING], "remoteAccess": access},
    }


def _guard_holds(service: dict[str, object]) -> bool:
    """Evaluate the remote access guard with its task vars templated."""
    that, task_vars = _remote_access_guard()
    variables: dict[str, object] = {"cloudfall_postgresql_service": service}
    templar = Templar(loader=DataLoader(), variables=variables)
    for name, value in task_vars.items():
        variables[name] = (
            templar.template(trust_as_template(value))
            if isinstance(value, str)
            else value
        )
    return _holds(that, variables)


@pytest.mark.parametrize(
    "access",
    [
        {"listenAddress": "*", "allowedSources": ["10.0.0.0/24"]},
        {"listenAddress": "0.0.0.0", "allowedSources": ["10.0.0.0/24"]},  # noqa: S104
        {"listenAddress": "::", "allowedSources": ["10.0.0.0/24"]},
        {"listenAddress": "10.0.0.2,0.0.0.0", "allowedSources": ["10.0.0.0/24"]},
        {"listenAddress": "10.0.0.2", "allowedSources": []},
        {"listenAddress": "10.0.0.2", "allowedSources": ["0.0.0.0/0"]},
        {"listenAddress": "10.0.0.2", "allowedSources": ["10.0.0.0/24", "::/0"]},
    ],
    ids=[
        "star",
        "any-ipv4",
        "any-ipv6",
        "smuggled-list",
        "no-source",
        "every-ipv4",
        "every-ipv6",
    ],
)
def test_the_role_refuses_remote_access_open_to_everyone(
    access: dict[str, object],
) -> None:
    assert not _guard_holds(_guard_service(access))


def test_the_role_accepts_remote_access_narrowed_to_sources() -> None:
    assert _guard_holds(_guard_service(REMOTE_ACCESS))


@pytest.mark.parametrize(
    ("name", "owner"),
    [
        ("all", "billing"),
        ("billing", "all"),
        ("sameuser", "billing"),
        ("samerole", "billing"),
        ("replication", "billing"),
    ],
)
def test_the_role_refuses_an_owned_name_pg_hba_reads_as_a_keyword(
    name: str, owner: str
) -> None:
    # "hostssl all billing ..." would let billing reach every database,
    # application databases included; "hostssl billing all ..." every role.
    database = {**BILLING, "name": name, "owner": owner}

    assert not _guard_holds(_guard_service(REMOTE_ACCESS, [database]))


def _firewall_asserts() -> dict[str, list[str]]:
    tasks = DataLoader().load_from_file(str(FIREWALL / "tasks" / "main.yml"))
    assert isinstance(tasks, list)
    return {
        task["name"]: task["ansible.builtin.assert"]["that"]
        for task in tasks
        if "ansible.builtin.assert" in task
    }


@pytest.mark.parametrize("sources", [[], ["0.0.0.0/0"], ["10.0.0.0/24", "::/0"]])
def test_the_firewall_role_refuses_a_rule_restricted_to_everyone(
    sources: list[str],
) -> None:
    that = _firewall_asserts()[
        "Refuse a source-restricted rule that opens to every address"
    ]
    ruleset = {
        "policy": "default-deny",
        "allowedInbound": [
            {"port": 22, "protocol": "tcp"},
            {"port": 5432, "protocol": "tcp", "sources": sources},
        ],
    }

    assert not _holds(that, {"cloudfall_firewall_ruleset": ruleset})


def test_a_source_restricted_rule_does_not_count_as_the_ssh_port() -> None:
    that = _firewall_asserts()[
        "Refuse a ruleset that would block the control connection"
    ]
    restricted = {
        "policy": "default-deny",
        "allowedInbound": [
            {"port": 22, "protocol": "tcp", "sources": ["10.0.0.0/24"]},
        ],
    }
    open_ssh = {
        "policy": "default-deny",
        "allowedInbound": [
            {"port": 22, "protocol": "tcp"},
            {"port": 5432, "protocol": "tcp", "sources": ["10.0.0.0/24"]},
        ],
    }

    assert not _holds(that, {"cloudfall_firewall_ruleset": restricted})
    assert _holds(that, {"cloudfall_firewall_ruleset": open_ssh})


FAKE_PSQL = """\
#!{python}
import pathlib, sys
log = pathlib.Path({log!r})
command = sys.argv[sys.argv.index("--command") + 1]
if "pg_hba_file_rules" in command:
    errors = log.joinpath("errors")
    if errors.exists():
        print(errors.read_text(), end="")
elif "pg_reload_conf" in command:
    if log.joinpath("reload-fails").exists():
        print("f")
        sys.exit()
    with log.joinpath("reloads").open("a") as reloads:
        reloads.write("reload\\n")
    print("t")
else:
    sys.exit("unexpected psql command: " + command)
"""


def _state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _pending_reload(tmp_path: Path) -> Path:
    return _state(tmp_path) / "postgresql-main.pg_hba-reload-pending"


def _hba_play(
    tmp_path: Path, service: dict[str, object], hba: Path, log: Path
) -> Path:
    """Write the role's pg_hba.conf block as a local play with a stand-in psql."""
    psql = tmp_path / "psql"
    psql.write_text(FAKE_PSQL.format(python=sys.executable, log=str(log)), "utf-8")
    psql.chmod(0o700)
    # Plain YAML, not Ansible's loader: the tasks are written out again.
    tasks = yaml.safe_load((POSTGRESQL / "tasks" / "main.yml").read_text("utf-8"))
    block = next(
        task
        for task in tasks
        if task["name"] == "Converge the remote access block of pg_hba.conf"
    )
    block["vars"]["cloudfall_postgresql_hba_path"] = str(hba)
    for task in [*block["block"], *block["rescue"]]:
        command = task.get("ansible.builtin.command")
        if command is not None:
            command["argv"][0] = str(psql)
            del task["become"], task["become_user"]
        # The test runs unprivileged; the host's files stay root's.
        for module in ("ansible.builtin.file", "ansible.builtin.copy"):
            if module in task:
                task[module].pop("owner", None)
                task[module].pop("group", None)
    templates = tmp_path / "templates"
    templates.mkdir(exist_ok=True)
    shutil.copy(POSTGRESQL / "templates" / "pg_hba-remote-access.j2", templates)
    play = [
        {
            "name": "Replay the pg_hba.conf tasks of cloudfall_postgresql",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {
                "cloudfall_postgresql_service": service,
                "cloudfall_postgresql_hba_marker": _defaults()[
                    "cloudfall_postgresql_hba_marker"
                ],
                "cloudfall_postgresql_state_directory": str(_state(tmp_path)),
            },
            "tasks": [block],
        }
    ]
    playbook = tmp_path / "pg-hba.yml"
    playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
    return playbook


def _converge_hba(
    tmp_path: Path, service: dict[str, object], hba: Path, log: Path
) -> subprocess.CompletedProcess[str]:
    playbook = _hba_play(tmp_path, service, hba, log)
    binary = shutil.which("ansible-playbook")
    assert binary is not None
    return subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [binary, "--inventory", "localhost,", str(playbook)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _reloads(log: Path) -> int:
    reloads = log / "reloads"
    return len(reloads.read_text().splitlines()) if reloads.exists() else 0


# proves: S-006-8
# proves: S-006-10
def test_s006_8_s006_10_the_hba_block_opens_then_closes_leaving_the_rest(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, REMOTE_ACCESS)
    opened = _service(_host(_inventory(project)))
    _project(tmp_path, None)
    closed = _service(_host(_inventory(project)))
    log = tmp_path / "psql-log"
    log.mkdir()
    hba = tmp_path / "pg_hba.conf"
    hba.write_text(DEBIAN_HBA, encoding="utf-8")

    first = _converge_hba(tmp_path, opened, hba, log)
    after_open = hba.read_text(encoding="utf-8")
    again = _converge_hba(tmp_path, opened, hba, log)
    reloads_while_open = _reloads(log)
    second = _converge_hba(tmp_path, closed, hba, log)

    assert first.returncode == 0, first.stdout + first.stderr
    assert again.returncode == 0, again.stdout + again.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert after_open == (
        DEBIAN_HBA
        + "# BEGIN CLOUDFALL MANAGED REMOTE ACCESS\n"
        + "hostssl billing billing 10.0.0.0/24 scram-sha-256\n"
        + "# END CLOUDFALL MANAGED REMOTE ACCESS\n"
    )
    # An unchanged block neither rewrites the file nor reloads.
    assert reloads_while_open == 1
    assert hba.read_text(encoding="utf-8") == DEBIAN_HBA
    assert _reloads(log) == 2


def test_lines_an_operator_adds_after_the_block_survive_its_removal(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, None)
    closed = _service(_host(_inventory(project)))
    log = tmp_path / "psql-log"
    log.mkdir()
    hba = tmp_path / "pg_hba.conf"
    hba.write_text(
        HBA_HEAD
        + "# BEGIN CLOUDFALL MANAGED REMOTE ACCESS\n"
        + "hostssl billing billing 10.0.0.0/24 scram-sha-256\n"
        + "# END CLOUDFALL MANAGED REMOTE ACCESS\n"
        + HBA_TAIL,
        encoding="utf-8",
    )

    done = _converge_hba(tmp_path, closed, hba, log)

    assert done.returncode == 0, done.stdout + done.stderr
    assert hba.read_text(encoding="utf-8") == DEBIAN_HBA


def test_a_pg_hba_conf_postgresql_cannot_read_is_never_reloaded(
    tmp_path: Path,
) -> None:
    opened = _service(_host(_inventory(_project(tmp_path, REMOTE_ACCESS))))
    log = tmp_path / "psql-log"
    log.mkdir()
    (log / "errors").write_text('3: invalid connection type "hostsssl"\n')
    hba = tmp_path / "pg_hba.conf"
    hba.write_text(DEBIAN_HBA, encoding="utf-8")

    done = _converge_hba(tmp_path, opened, hba, log)

    assert done.returncode != 0
    assert 'invalid connection type \\"hostsssl\\"' in done.stdout
    assert _reloads(log) == 0
    # The opening is closed again, so it never takes effect at a later restart.
    assert hba.read_text(encoding="utf-8") == DEBIAN_HBA


def test_a_failed_reload_is_owed_until_a_later_converge_applies_it(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, REMOTE_ACCESS)
    opened = _service(_host(_inventory(project)))
    _project(tmp_path, None)
    closed = _service(_host(_inventory(project)))
    log = tmp_path / "psql-log"
    log.mkdir()
    hba = tmp_path / "pg_hba.conf"
    hba.write_text(DEBIAN_HBA, encoding="utf-8")
    opening = _converge_hba(tmp_path, opened, hba, log)
    (log / "reload-fails").touch()

    failed = _converge_hba(tmp_path, closed, hba, log)
    marker_after_failure = _pending_reload(tmp_path).read_text(encoding="utf-8")
    (log / "reload-fails").unlink()
    retried = _converge_hba(tmp_path, closed, hba, log)
    reloads_after_retry = _reloads(log)
    settled = _converge_hba(tmp_path, closed, hba, log)

    assert opening.returncode == 0, opening.stdout + opening.stderr
    assert failed.returncode != 0
    assert marker_after_failure == "postgresql-main: pg_hba.conf reload pending\n"
    # The file no longer changes, yet the owed reload runs and is cleared.
    assert retried.returncode == 0, retried.stdout + retried.stderr
    assert reloads_after_retry == 2
    assert not _pending_reload(tmp_path).exists()
    # With neither a change nor a marker, nothing reloads or reports a change.
    assert settled.returncode == 0, settled.stdout + settled.stderr
    assert _reloads(log) == 2
    assert "changed=0" in settled.stdout
    assert hba.read_text(encoding="utf-8") == DEBIAN_HBA


def test_a_pg_hba_conf_postgresql_cannot_read_owes_a_reload(
    tmp_path: Path,
) -> None:
    opened = _service(_host(_inventory(_project(tmp_path, REMOTE_ACCESS))))
    log = tmp_path / "psql-log"
    log.mkdir()
    (log / "errors").write_text('3: invalid connection type "hostsssl"\n')
    hba = tmp_path / "pg_hba.conf"
    hba.write_text(DEBIAN_HBA, encoding="utf-8")

    done = _converge_hba(tmp_path, opened, hba, log)

    assert done.returncode != 0
    assert _pending_reload(tmp_path).exists()
