"""The ``cloudfall_mariadb`` role for a standalone server (S-019, #420).

The role needs a Debian 13 host with MariaDB, so its decisions are evaluated
with Ansible's own templating, and its database and user tasks run for real
on localhost against a stand-in ``mariadb`` client that keeps the server's
state in a file: a second converge must change nothing. What a live host
alone can prove (the package, the listener, the plugins' logins) is listed in
the pull request as unverified.
"""

from __future__ import annotations

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
from cloudfall.mariadb import ed25519_password_hash, mysql_native_password_hash
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from mariadb_server import fresh_state, write_client

if TYPE_CHECKING:
    from collections.abc import Iterable

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_mariadb"
SERVICES = ROOT / "engine" / "ansible" / "playbooks" / "services.yml"
PASSWORD = "s3cr3t-billing-value"  # noqa: S105 - the value every output is grepped for
NATIVE = mysql_native_password_hash(PASSWORD)
ED25519 = ed25519_password_hash(PASSWORD)
PASSWORD_REF = {
    "environment": "production",
    "path": "/services/mariadb-main",
    "key": "BILLING_PASSWORD",
}
CRM: dict[str, object] = {"name": "crm", "application": "crm", "owner": "crm"}
BILLING: dict[str, object] = {
    "name": "billing",
    "owner": "billing",
    "passwordRef": PASSWORD_REF,
    "authentication": "mysql_native_password",
}
# The tasks of main.yml that only read or refuse; every one before the first
# task that changes the host
REFUSALS = (
    "Validate MariaDB service target and declaration",
    "Refuse replication, which this role does not converge yet",
    "Refuse remote access that is not narrowed to declared sources",
    "Refuse a buffer pool above three quarters of the server's memory",
    "Refuse a series MariaDB publishes no package of for this Debian release",
    "Require a staged hash for every owner",
    "Read the installed MariaDB server package",
    "Refuse a version of another series than the installed one",
)


def _service(*databases: dict[str, object], **mariadb: object) -> dict[str, object]:
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
        "metrics": {"enabled": True},
    }


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


def _variables(**extra: object) -> dict[str, object]:
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
        "cloudfall_mariadb_service": _service(CRM, BILLING),
        "cloudfall_mariadb_hash_files": {"billing": "/tmp/staged/billing.hash"},  # noqa: S108
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


# proves: S-019-4
def test_s019_4_a_buffer_pool_above_three_quarters_of_memory_is_refused() -> None:
    refuse = _task(
        "Refuse a buffer pool above three quarters of the server's memory",
        _tasks("main.yml"),
    )

    assert _holds(refuse, _variables())
    at_limit = _service(CRM, innodbBufferPoolMb=6144)
    assert _holds(refuse, _variables(cloudfall_mariadb_service=at_limit))
    above = _service(CRM, innodbBufferPoolMb=6145)
    assert not _holds(refuse, _variables(cloudfall_mariadb_service=above))


# proves: S-019-4
def test_s019_4_every_refusal_comes_before_any_task_that_changes_the_host() -> None:
    names = [task["name"] for task in _tasks("main.yml")]

    assert names[: len(REFUSALS)] == list(REFUSALS)
    read = _task("Read the installed MariaDB server package", _tasks("main.yml"))
    assert read["changed_when"] is False
    assert read["check_mode"] is False
    # services.yml refuses it too, before any service role runs
    play = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))[0]
    pre_task = _task(
        "Refuse a MariaDB buffer pool above three quarters of the server's memory",
        play["pre_tasks"],
    )
    service = _service(CRM, innodbBufferPoolMb=6145)
    variables = {
        **_variables(),
        "cloudfall_services": [service],
        "cloudfall_service_item": service,
    }
    assert not _holds(pre_task, variables)
    selected = _template(pre_task["loop"], variables)
    assert selected == [service]
    deploy = _task("Deploy declared MariaDB services", play["tasks"])
    assert deploy["ansible.builtin.include_role"]["name"] == "cloudfall_mariadb"


def test_a_series_without_a_package_for_the_debian_release_is_refused() -> None:
    refuse = _task(
        "Refuse a series MariaDB publishes no package of for this Debian release",
        _tasks("main.yml"),
    )

    assert _holds(refuse, _variables())
    older = _service(CRM, version="11.4.8")
    assert not _holds(refuse, _variables(cloudfall_mariadb_service=older))
    bookworm = {
        **cast("dict[str, object]", _variables()["ansible_facts"]),
        "distribution_major_version": "12",
    }
    assert _holds(
        refuse, _variables(cloudfall_mariadb_service=older, ansible_facts=bookworm)
    )


@pytest.mark.parametrize(
    ("stdout", "rc", "installed", "outcome"),
    [
        pytest.param("", 1, "", "allowed", id="nothing installed"),
        pytest.param(
            "installed|1:11.8.3+maria~deb13", 0, "11.8.3", "allowed", id="patch"
        ),
        pytest.param(
            "installed|1:11.8.3-0+deb13u1", 0, "11.8.3", "allowed", id="debian"
        ),
        pytest.param(
            "installed|1:11.4.8+maria~deb13", 0, "11.4.8", "refused", id="series"
        ),
        pytest.param(
            "config-files|1:11.4.8+maria~deb13", 0, "", "allowed", id="removed"
        ),
    ],
)
def test_a_version_of_another_series_than_the_installed_one_is_refused(
    stdout: str, rc: int, installed: str, outcome: str
) -> None:
    refuse = _task(
        "Refuse a version of another series than the installed one",
        _tasks("main.yml"),
    )
    variables = _variables(cloudfall_mariadb_installed={"stdout": stdout, "rc": rc})

    assert _template("{{ cloudfall_mariadb_installed_version }}", variables) == (
        installed
    )
    assert _holds(refuse, variables) is (outcome == "allowed")


# proves: S-019-3
def test_s019_3_the_package_is_the_declared_release_from_the_archive_and_held() -> None:
    tasks = _tasks("package.yml")
    variables = _variables()
    install = _task("Install the pinned MariaDB server", tasks)
    repository = _task(
        "Configure the archive repository of the declared release", tasks
    )
    hold = _task("Hold the MariaDB server package", tasks)

    name = _template(install["ansible.builtin.apt"]["name"], variables)
    assert name == "mariadb-server=1:11.8.9+maria~deb13"
    assert install["ansible.builtin.apt"]["policy_rc_d"] == 101
    assert install["notify"] == "Restart MariaDB"
    uri = _template(
        repository["ansible.builtin.deb822_repository"]["uris"][0], variables
    )
    assert uri == "https://archive.mariadb.org/mariadb-11.8.9/repo/debian"
    assert hold["ansible.builtin.dpkg_selections"] == {
        "name": "mariadb-server",
        "selection": "hold",
    }


def _render_settings(service: dict[str, object]) -> str:
    environment = Environment(
        loader=FileSystemLoader(ROLE / "templates"),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    return environment.get_template("90-cloudfall.cnf.j2").render(
        cloudfall_mariadb_service=service, cloudfall_mariadb_data_path="/var/lib/mysql"
    )


# proves: S-019-3
def test_s019_3_the_managed_file_holds_the_buffer_pool_and_a_loopback_listener() -> (
    None
):
    settings = _render_settings(_service(CRM))
    lines = [line for line in settings.splitlines() if not line.startswith("#")]

    assert lines[0] == "[mariadbd]"
    assert "bind-address = 127.0.0.1" in lines
    assert "port = 3306" in lines
    assert "datadir = /var/lib/mysql" in lines
    assert "innodb_buffer_pool_size = 4096M" in lines
    assert "character-set-server = utf8mb4" in lines
    # No binary log on a standalone server
    assert not any("log-bin" in line or "log_bin" in line for line in lines)
    assert _defaults()["cloudfall_mariadb_settings_path"] == (
        "/etc/mysql/mariadb.conf.d/90-cloudfall.cnf"
    )


# proves: S-019-3
def test_s019_3_the_data_directory_is_the_mounted_mariadb_volume() -> None:
    tasks = _tasks("storage.yml")
    names = [task["name"] for task in tasks]
    refuse = _task("Refuse a data directory that is not its volume", tasks)
    main = [task["name"] for task in _tasks("main.yml")]

    # The volume is there before the package writes its first database
    assert main.index("Create, grow and mount the data volume") < main.index(
        "Install the pinned MariaDB server"
    )
    assert names.index("Read what is mounted at the data directory") < names.index(
        "Give the data directory to the server"
    )
    variables = _variables(cloudfall_mariadb_data_mount={"rc": 1})
    assert not _holds(refuse, variables)
    assert _holds(refuse, _variables(cloudfall_mariadb_data_mount={"rc": 0}))
    actions = {
        "h1": [
            {"name": "mariadb", "path": "/var/lib/mysql"},
            {"name": "elasticsearch", "path": "/var/lib/elasticsearch"},
        ]
    }
    selected = _template(
        "{{ cloudfall_mariadb_volume_actions }}",
        _variables(cloudfall_volume_actions=actions),
    )
    assert selected == [{"name": "mariadb", "path": "/var/lib/mysql"}]


def test_replication_is_refused_until_it_converges_and_remote_access_is_not() -> None:
    refuse = _task(
        "Refuse replication, which this role does not converge yet",
        _tasks("main.yml"),
    )
    replication = {"role": "primary", "serverId": 1, "listenAddress": "10.0.0.2"}
    remote_access = {"listenAddress": "10.0.0.2", "allowedSources": ["10.0.0.0/24"]}

    assert _holds(refuse, _variables())
    assert not _holds(
        refuse,
        _variables(cloudfall_mariadb_service=_service(CRM, replication=replication)),
    )
    assert _holds(
        refuse,
        _variables(
            cloudfall_mariadb_service=_service(CRM, remoteAccess=remote_access)
        ),
    )


def test_a_converge_without_a_staged_hash_stops_before_any_change() -> None:
    require = _task("Require a staged hash for every owner", _tasks("main.yml"))

    assert _holds(require, _variables())
    assert not _holds(require, _variables(cloudfall_mariadb_hash_files={}))
    application_only = _variables(
        cloudfall_mariadb_service=_service(CRM), cloudfall_mariadb_hash_files={}
    )
    assert _holds(require, application_only)


# proves: S-019-6
def test_s019_6_every_task_that_sees_the_hash_is_no_log() -> None:
    (block,) = _tasks("owner_password.yml")
    tasks = [*block["block"], *block["always"]]
    quiet = {
        "Refuse a staged hash of an unexpected shape",
        "Create the MariaDB role state directory",
    }

    assert all(
        task.get("no_log") is True for task in tasks if task["name"] not in quiet
    )
    refuse = _task("Refuse a staged hash of an unexpected shape", tasks)
    message = refuse["ansible.builtin.assert"]["fail_msg"]
    assert "cloudfall_mariadb_hash" not in message
    assert "stdout" not in message


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
    """A replay of the role's database and user tasks against the stand-in."""

    def __init__(self, tmp_path: Path, state: dict[str, object] | None = None) -> None:
        """Write the stand-in client, the controller's hash file and the tasks."""
        self.tmp_path = tmp_path
        self.client, self.state_path, self.log = write_client(
            tmp_path / "server", state if state is not None else fresh_state()
        )
        self.tasks = tmp_path / "tasks"
        self.tasks.mkdir()
        for name in ("accounts.yml", "owner_password.yml"):
            tasks = yaml.safe_load((ROLE / "tasks" / name).read_text("utf-8"))
            _strip_ownership(tasks)
            (self.tasks / name).write_text(yaml.safe_dump(tasks), encoding="utf-8")
        self.host_state = tmp_path / "host-state"

    def converge(
        self,
        service: dict[str, object],
        hashes: dict[str, str],
        *,
        succeed: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], int]:
        """Run the tasks at -vvv with --diff; return the run and its change count."""
        staged: dict[str, str] = {}
        controller = self.tmp_path / "controller"
        controller.mkdir(exist_ok=True)
        for owner, value in hashes.items():
            path = controller / f"mariadb-main-{owner}.hash"
            path.write_text(value, encoding="utf-8")
            staged[owner] = str(path)
        play = [
            {
                "name": "Replay the database and user tasks of cloudfall_mariadb",
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "vars": {
                    **_defaults(),
                    "cloudfall_mariadb_service": service,
                    "cloudfall_mariadb_hash_files": staged,
                    "cloudfall_mariadb_client": str(self.client),
                    "cloudfall_mariadb_state_directory": str(self.host_state),
                },
                "tasks": [
                    {
                        "name": "Converge the declared databases and users",
                        "ansible.builtin.include_tasks": str(
                            self.tasks / "accounts.yml"
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
            env=dict(os.environ),
        )
        recap = RECAP.search(done.stdout)
        assert recap is not None, done.stdout + done.stderr
        assert (done.returncode == 0) is succeed, done.stdout + done.stderr
        return done, int(recap[1])

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


# proves: S-019-4
# proves: S-019-5
def test_s019_5_a_fresh_server_gets_the_socket_user_the_owner_and_their_grants(
    tmp_path: Path,
) -> None:
    server = Server(tmp_path)

    first, changed = server.converge(_service(CRM, BILLING), {"billing": NATIVE})

    assert changed > 0
    state = server.state
    assert {"crm", "billing"} <= set(state["databases"])
    # crm connects through the socket only, with no password
    assert state["users"]["crm"]["plugin"] == "unix_socket"
    assert state["users"]["crm"]["hash"] == ""
    # billing logs in with its password, stored as its hash
    assert state["users"]["billing"]["plugin"] == "mysql_native_password"
    assert state["users"]["billing"]["hash"] == NATIVE
    # Each user has every privilege on its own database and none elsewhere
    assert sorted(state["grants"]) == ["billing|billing", "crm|crm"]
    assert state["users"]["crm"]["managed"] is True
    assert state["users"]["billing"]["managed"] is True
    assert state["roles"] == ["cloudfall-managed"]
    assert not state["ed25519"]
    assert "GRANT ALL PRIVILEGES ON `crm`.* TO 'crm'@'localhost'" in server.writes()
    _assert_nothing_leaked(first, server)


# proves: S-019-4
def test_s019_4_a_second_converge_changes_nothing(tmp_path: Path) -> None:
    server = Server(tmp_path)
    server.converge(_service(CRM, BILLING), {"billing": NATIVE})
    writes = len(server.writes())

    second, changed = server.converge(_service(CRM, BILLING), {"billing": NATIVE})

    assert changed == 0, second.stdout
    assert len(server.writes()) == writes
    assert not (server.host_state / "mariadb-main-owner.sql").exists()


def test_a_rotated_password_sets_the_new_hash_and_nothing_else(tmp_path: Path) -> None:
    server = Server(tmp_path)
    server.converge(_service(CRM, BILLING), {"billing": NATIVE})
    writes = len(server.writes())
    rotated = mysql_native_password_hash("a-rotated-value")

    _, changed = server.converge(_service(CRM, BILLING), {"billing": rotated})

    # The staged statement, the statement itself, and its removal
    assert changed == 3
    command, sourced = server.writes()[writes:]
    assert command.startswith("source ")
    assert f"USING '{rotated}'" in sourced
    assert server.state["users"]["billing"]["hash"] == rotated


# proves: S-019-6
def test_s019_6_the_statement_staged_for_billing_holds_its_hash_never_the_password(
    tmp_path: Path,
) -> None:
    server = Server(tmp_path)

    done, _ = server.converge(_service(CRM, BILLING), {"billing": NATIVE})

    (sourced,) = (text for text in server.statements() if " USING " in text)
    assert sourced.splitlines() == [
        (
            "CREATE USER IF NOT EXISTS 'billing'@'localhost' IDENTIFIED VIA "
            f"mysql_native_password USING '{NATIVE}';"
        ),
        (
            "ALTER USER 'billing'@'localhost' IDENTIFIED VIA "
            f"mysql_native_password USING '{NATIVE}';"
        ),
    ]
    assert PASSWORD not in sourced
    # Staged 0600 for the run, then removed
    assert not (server.host_state / "mariadb-main-owner.sql").exists()
    assert oct((server.host_state).stat().st_mode & 0o777) == "0o700"
    _assert_nothing_leaked(done, server)


def _assert_nothing_leaked(
    done: subprocess.CompletedProcess[str], server: Server
) -> None:
    """Neither the password nor a hash is in the output or a client argument."""
    arguments = [
        json.loads(line) for line in server.log.read_text(encoding="utf-8").splitlines()
    ]
    argv = json.dumps([item for item in arguments if item[0] != "sourced"])
    for secret in (PASSWORD, NATIVE, ED25519):
        assert secret not in done.stdout + done.stderr
        assert secret not in argv


# proves: S-019-21
def test_s019_21_an_ed25519_owner_gets_a_user_of_that_plugin(tmp_path: Path) -> None:
    server = Server(tmp_path)
    billing = {**BILLING, "authentication": "ed25519"}

    first, _ = server.converge(_service(CRM, billing), {"billing": ED25519})
    second, changed = server.converge(_service(CRM, billing), {"billing": ED25519})

    state = server.state
    assert state["ed25519"] is True
    assert state["users"]["billing"]["plugin"] == "ed25519"
    assert state["users"]["billing"]["hash"] == ED25519
    assert "INSTALL SONAME 'auth_ed25519'" in server.writes()
    assert changed == 0, second.stdout
    _assert_nothing_leaked(first, server)


def test_a_staged_hash_of_another_shape_stops_without_quoting_it(
    tmp_path: Path,
) -> None:
    server = Server(tmp_path)

    done, _ = server.converge(
        _service(CRM, BILLING), {"billing": PASSWORD}, succeed=False
    )

    assert done.returncode != 0
    assert "is not a mysql_native_password hash" in done.stdout
    assert PASSWORD not in done.stdout + done.stderr
    assert not any(" USING " in text for text in server.statements())


# proves: S-019-7
def test_s019_7_a_removed_database_keeps_its_rows_and_its_user_is_locked(
    tmp_path: Path,
) -> None:
    server = Server(tmp_path)
    server.converge(_service(CRM, BILLING), {"billing": NATIVE})
    # A user made by hand, which Cloudfall never touches
    state = server.state
    state["users"]["analyst"] = {
        "plugin": "mysql_native_password",
        "hash": "*" + "A" * 40,
        "authOr": False,
        "locked": False,
        "managed": False,
    }
    server.state_path.write_text(json.dumps(state), encoding="utf-8")
    writes = len(server.writes())

    _, changed = server.converge(_service(CRM), {})

    removed = server.writes()[writes:]
    assert removed == ["ALTER USER 'billing'@'localhost' ACCOUNT LOCK"]
    assert changed == 1
    after = server.state
    assert "billing" in after["databases"]
    assert "billing|billing" in after["grants"]
    assert after["users"]["billing"]["locked"] is True
    assert after["users"]["analyst"] == state["users"]["analyst"]
    # Nothing a converge sends ever drops data
    assert not any("DROP" in text.upper() for text in server.statements())
    # Converging again changes nothing; declaring it again unlocks it
    _, again = server.converge(_service(CRM), {})
    assert again == 0
    _, back = server.converge(_service(CRM, BILLING), {"billing": NATIVE})
    assert back == 1
    assert server.state["users"]["billing"]["locked"] is False


def test_a_declared_user_made_by_hand_is_adopted(tmp_path: Path) -> None:
    state = fresh_state()
    users = cast("dict[str, dict[str, object]]", state["users"])
    users["crm"] = {
        "plugin": "mysql_native_password",
        "hash": "*" + "B" * 40,
        "authOr": False,
        "locked": False,
        "managed": False,
    }
    cast("list[str]", state["databases"]).append("crm")
    server = Server(tmp_path, state)

    server.converge(_service(CRM), {})

    after = server.state
    assert after["users"]["crm"]["plugin"] == "unix_socket"
    assert after["users"]["crm"]["managed"] is True
    assert after["databases"].count("crm") == 1


def test_an_underscore_in_a_database_name_is_escaped_in_its_grant(
    tmp_path: Path,
) -> None:
    server = Server(tmp_path)
    ledger: dict[str, object] = {
        "name": "crm_ledger",
        "application": "crm",
        "owner": "crm",
    }

    server.converge(_service(ledger), {})
    _, changed = server.converge(_service(ledger), {})

    assert "crm|crm\\_ledger" in server.state["grants"]
    assert changed == 0


def test_the_role_argument_spec_takes_a_standalone_service() -> None:
    specs = yaml.safe_load(
        (ROLE / "meta" / "argument_specs.yml").read_text(encoding="utf-8")
    )
    options = specs["argument_specs"]["main"]["options"]
    database = options["cloudfall_mariadb_service"]["options"]["mariadb"]["options"][
        "databases"
    ]["options"]

    assert database["authentication"]["choices"] == [
        "mysql_native_password",
        "ed25519",
    ]
    assert database["passwordRef"]["options"]["key"]["required"] is True
    assert options["cloudfall_mariadb_hash_files"]["type"] == "dict"
