"""The PostgreSQL role's owner roles, and what an owned database goes through.

The role needs a Debian host with PostgreSQL, so its decisions are evaluated
with Ansible's own templating, and the password tasks and the data-copy
receipt tasks run for real on localhost with a stand-in for ``psql``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.secrets import scram_sha256_verifier
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parents[2]
ROLES = ROOT / "engine" / "ansible" / "roles"
POSTGRESQL = ROLES / "cloudfall_postgresql"
DATA_MIGRATION = ROLES / "cloudfall_data_migration"
PASSWORD = "s3cr3t-billing-value"  # noqa: S105 - the value every output is grepped for
# What the controller stages in place of the password (D-9).
VERIFIER = scram_sha256_verifier(PASSWORD, salt=bytes(range(16)))
BILLING: dict[str, object] = {
    "name": "billing",
    "owner": "billing",
    "passwordRef": {
        "environment": "production",
        "path": "/billing/postgresql",
        "key": "BILLING_PASSWORD",
    },
}
CRM: dict[str, object] = {"name": "crm", "application": "crm", "owner": "crm"}


def _service(*databases: dict[str, object]) -> dict[str, object]:
    return {
        "id": "postgresql-billing",
        "serviceKind": "postgresql",
        "environment": "production",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": 5432},
        "postgresql": {"majorVersion": "17", "databases": list(databases)},
        "backup": {
            "directory": "/var/backups/cloudfall/postgresql-billing",
            "onCalendar": "*-*-* 02:00:00 UTC",
            "restoreCheckOnCalendar": "*-*-* 05:00:00 UTC",
            "retentionDays": 14,
        },
    }


def _tasks(role: Path, name: str = "main.yml") -> list[dict[str, object]]:
    tasks = DataLoader().load_from_file(str(role / "tasks" / name))
    assert isinstance(tasks, list)
    return tasks


def _task(name: str, tasks: list[dict[str, object]]) -> dict[str, object]:
    return next(task for task in tasks if task["name"] == name)


def _templar(variables: dict[str, object]) -> Templar:
    return Templar(loader=DataLoader(), variables=variables)


def _holds(expression: object, variables: dict[str, object]) -> bool:
    return bool(
        _templar(variables).evaluate_conditional(trust_as_template(str(expression)))
    )


def _loop(task: dict[str, object], variables: dict[str, object]) -> object:
    return _templar(variables).template(trust_as_template(str(task["loop"])))


def _conditions(task: dict[str, object]) -> list[object]:
    value = task.get("when", [])
    return value if isinstance(value, list) else [value]


def _variables(**extra: object) -> dict[str, object]:
    return {
        "ansible_check_mode": False,
        "cloudfall_postgresql_service": _service(BILLING, CRM),
        "cloudfall_postgresql_password_files": {"billing": "/tmp/staged/billing"},  # noqa: S108
        **extra,
    }


# proves: S-006-5
def test_s006_5_the_owner_gets_a_login_role_and_its_password() -> None:
    tasks = _tasks(POSTGRESQL)
    variables = _variables()

    create = _task("Create missing database owner login roles", tasks)
    include = _task("Set each owner role password from its staged file", tasks)

    roles = _loop(create, variables)
    owners = _loop(include, variables)

    assert roles == ["billing", "crm"]
    assert owners == ["billing"]


# proves: S-006-5
def test_s006_5_the_owned_database_is_created_and_kept_owned_by_its_role() -> None:
    tasks = _tasks(POSTGRESQL)
    create = _task("Create missing application databases", tasks)
    give = _task("Give each owned database to its declared owner role", tasks)
    variables = _variables(
        cloudfall_postgresql_database_owners={
            "stdout_lines": ["billing=postgres", "crm=crm"]
        },
    )

    assert cast("dict[str, list[str]]", create["ansible.builtin.command"])["argv"][
        -1
    ] == 'CREATE DATABASE "{{ item.name }}" OWNER "{{ item.owner }}"'
    assert _loop(give, variables) == [BILLING]
    assert all(
        _holds(condition, {**variables, "item": BILLING})
        for condition in _conditions(give)
    )
    owned = {**variables, "item": BILLING}
    owned["cloudfall_postgresql_database_owners"] = {
        "stdout_lines": ["billing=billing"]
    }
    assert not all(_holds(condition, owned) for condition in _conditions(give))


# proves: S-006-5
def test_s006_5_the_password_is_stored_as_scram_from_the_staged_file() -> None:
    (block,) = _tasks(POSTGRESQL, "owner_password.yml")
    store = _task(
        "Store the owner role password as SCRAM-SHA-256",
        cast("list[dict[str, object]]", block["block"]),
    )
    command = cast("dict[str, str]", store["ansible.builtin.command"])
    stdin = str(
        _templar(
            {
                "cloudfall_postgresql_owner": "billing",
                "cloudfall_postgresql_password_path": "/var/lib/postgresql/.pw",
            }
        ).template(trust_as_template(command["stdin"]))
    )

    assert stdin.splitlines() == [
        "SET password_encryption = 'scram-sha-256';",
        r"\set cloudfall_password `cat /var/lib/postgresql/.pw`",
        "ALTER ROLE \"billing\" WITH LOGIN PASSWORD :'cloudfall_password';",
    ]
    assert store["become_user"] == "postgres"


def test_a_converge_without_a_staged_password_stops_before_any_change() -> None:
    (check, *_) = (
        task
        for task in _tasks(POSTGRESQL)
        if task["name"] == "Require a staged password for every owner role"
    )
    that = cast("dict[str, list[str]]", check["ansible.builtin.assert"])["that"]

    assert all(_holds(condition, _variables()) for condition in that)
    unstaged = _variables(cloudfall_postgresql_password_files={})
    assert not all(_holds(condition, unstaged) for condition in that)
    application_only = _variables(
        cloudfall_postgresql_service=_service(CRM),
        cloudfall_postgresql_password_files={},
    )
    assert all(_holds(condition, application_only) for condition in that)


# proves: S-006-6
def test_s006_6_every_task_that_sees_the_password_is_no_log() -> None:
    (block,) = _tasks(POSTGRESQL, "owner_password.yml")
    tasks = [
        *cast("list[dict[str, object]]", block["block"]),
        *cast("list[dict[str, object]]", block["always"]),
    ]

    assert len(tasks) == 3
    assert all(task.get("no_log") is True for task in tasks)


FAKE_PSQL = """\
#!{python}
import os, pathlib, re, sys
log = pathlib.Path({log!r})
script = sys.stdin.read()
staged = re.search(r"`cat (.*)`", script).group(1)
log.joinpath("stdin").write_text(script)
log.joinpath("argv").write_text("\\n".join(sys.argv[1:]))
log.joinpath("value").write_text(pathlib.Path(staged).read_text())
log.joinpath("mode").write_text(oct(os.stat(staged).st_mode & 0o777))
"""


def _password_play(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write the owner password tasks as a local play with a stand-in psql."""
    log = tmp_path / "psql-log"
    log.mkdir()
    psql = tmp_path / "psql"
    psql.write_text(FAKE_PSQL.format(python=sys.executable, log=str(log)), "utf-8")
    psql.chmod(0o700)
    staged = tmp_path / "controller" / "postgresql-billing-billing"
    staged.parent.mkdir()
    staged.write_text(VERIFIER, encoding="utf-8")
    host_path = tmp_path / "host" / ".cloudfall-owner-password"
    host_path.parent.mkdir()
    # Plain YAML, not Ansible's loader: the tasks are written out again.
    (block,) = yaml.safe_load(
        (POSTGRESQL / "tasks" / "owner_password.yml").read_text(encoding="utf-8")
    )
    stage, store = block["block"]
    del stage["ansible.builtin.copy"]["owner"], stage["ansible.builtin.copy"]["group"]
    store["ansible.builtin.command"]["argv"][0] = str(psql)
    del store["become"], store["become_user"]
    play = [
        {
            "name": "Replay the owner password tasks of cloudfall_postgresql",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {
                "cloudfall_postgresql_owner": "billing",
                "cloudfall_postgresql_password_files": {"billing": str(staged)},
                "cloudfall_postgresql_password_path": str(host_path),
                "cloudfall_postgresql_service": _service(BILLING),
            },
            "tasks": [block],
        }
    ]
    playbook = tmp_path / "owner-password.yml"
    playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
    return playbook, log, host_path


# proves: S-006-6
def test_s006_6_the_verifier_reaches_psql_through_a_removed_0600_file(
    tmp_path: Path,
) -> None:
    playbook, log, host_path = _password_play(tmp_path)
    binary = shutil.which("ansible-playbook")
    assert binary is not None

    done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [binary, "-vvv", "--inventory", "localhost,", str(playbook)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    for secret in (PASSWORD, VERIFIER):
        assert secret not in done.stdout + done.stderr
        assert secret not in (log / "stdin").read_text(encoding="utf-8")
        assert secret not in (log / "argv").read_text(encoding="utf-8")
    assert (log / "value").read_text(encoding="utf-8") == VERIFIER
    assert (log / "mode").read_text(encoding="utf-8") == "0o600"
    assert not host_path.exists()


def _render(name: str, service: dict[str, object]) -> str:
    environment = Environment(
        loader=FileSystemLoader(POSTGRESQL / "templates"),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    return environment.get_template(name).render(cloudfall_postgresql_service=service)


# proves: S-006-13
def test_s006_13_the_backup_and_the_drill_cover_the_owned_database() -> None:
    service = _service(BILLING)

    backup = _render("backup.sh.j2", service)
    drill = _render("restore-check.sh.j2", service)

    assert '--file "${backup_directory}/billing-${timestamp}.dump"' in backup
    assert "-name 'billing-*.dump'" in drill
    assert 'scratch="billing_restore_check"' in drill
    assert "restore check passed for billing:" in drill


DATA_TASKS = (
    "Validate the migration target service",
    "Select the declared target database",
    "Require exactly one declared database entry",
    "Record the declared database owner",
    "Ensure the controller receipt directory exists",
    "Write the data-migration receipt on the controller",
)


def _data_copy_receipt(tmp_path: Path) -> dict[str, object]:
    """Replay the data-copy selection and receipt tasks for the owned database."""
    tasks = yaml.safe_load(
        (DATA_MIGRATION / "tasks" / "main.yml").read_text(encoding="utf-8")
    )
    named = {task["name"]: task for task in tasks}
    block = {
        task["name"]: task
        for task in named["Migrate and verify the declared database"]["block"]
    }
    restore = block["Restore the dump into the declared database"]
    tables = block["Record the verified tables for the receipt"]
    validate, select, require, record, directory, write = (
        named[name] for name in DATA_TASKS
    )
    receipts = tmp_path / "data-migrations"
    restored_as = {
        "name": "Record the role the restore runs as",
        "ansible.builtin.copy": {
            "content": "{{ " + _role_argument(restore) + " }}",
            "dest": str(tmp_path / "restore-role"),
            "mode": "0600",
        },
    }
    counts = {
        "name": "Stand in for the selected schemas and the source row counts",
        "ansible.builtin.set_fact": {
            "cloudfall_data_migration_selected_schemas": ["public"],
            "cloudfall_data_migration_source_counts": {
                "stdout_lines": ["public,invoices,3"]
            },
        },
    }
    play = [
        {
            "name": "Replay the data-copy receipt of cloudfall_data_migration",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": True,
            "gather_subset": ["min"],
            "vars": {
                "cloudfall_server_id": "h1",
                "cloudfall_data_migration_service": _service(BILLING),
                "cloudfall_data_migration_database": "billing",
                "cloudfall_data_migration_receipt_directory": str(receipts),
            },
            "tasks": [
                validate,
                select,
                require,
                record,
                restored_as,
                counts,
                tables,
                directory,
                write,
            ],
        }
    ]
    playbook = tmp_path / "data-copy.yml"
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
    receipt = json.loads(
        (receipts / "postgresql-billing-billing.json").read_text(encoding="utf-8")
    )
    assert isinstance(receipt, dict)
    return receipt


def _role_argument(restore: dict[str, object]) -> str:
    argv = cast("dict[str, list[str]]", restore["ansible.builtin.command"])["argv"]
    role = argv[argv.index("--role") + 1]
    return role.removeprefix("{{").removesuffix("}}").strip()


# proves: S-006-13
def test_s006_13_data_copy_restores_as_the_owner_and_receipts_it(
    tmp_path: Path,
) -> None:
    receipt = _data_copy_receipt(tmp_path)

    assert (tmp_path / "restore-role").read_text(encoding="utf-8").strip() == "billing"
    spec = receipt["spec"]
    assert isinstance(spec, dict)
    assert spec["owner"] == "billing"
    assert spec["database"] == "billing"
    assert "application" not in spec
    assert spec["tables"] == [{"schema": "public", "table": "invoices", "rows": 3}]
    assert PASSWORD not in json.dumps(receipt)


def test_the_role_argument_spec_takes_an_owned_database() -> None:
    specs = yaml.safe_load(
        (POSTGRESQL / "meta" / "argument_specs.yml").read_text(encoding="utf-8")
    )
    options = specs["argument_specs"]["main"]["options"]
    database = options["cloudfall_postgresql_service"]["options"]["postgresql"][
        "options"
    ]["databases"]["options"]

    assert "required" not in database["application"]
    assert database["passwordRef"]["options"]["key"]["required"] is True
    remote_access = options["cloudfall_postgresql_service"]["options"]["postgresql"][
        "options"
    ]["remoteAccess"]["options"]
    assert remote_access["listenAddress"]["required"] is True
    assert remote_access["allowedSources"]["elements"] == "str"
    assert options["cloudfall_postgresql_password_files"]["type"] == "dict"
