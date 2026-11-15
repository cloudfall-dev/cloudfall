"""The source database URL never reaches the command line of psql or pg_dump (#355).

The role turns the URL into a 0600 libpq service file named in the clients'
environment. These tests check the conversion, read every task of the role
for a command line that could carry the URL, and run the staging and a
source query for real on localhost with a stand-in ``psql``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_data_migration"
PASSWORD = "s3cr3t:p@ss/w%rd"  # noqa: S105 - the value every output is grepped for
ENCODED = "s3cr3t%3Ap%40ss%2Fw%25rd"
SOURCE_TASKS = (
    "Read every non-system schema of the source",
    "Read what the selected schemas need from the source",
    "Dump the source database on the target host",
    "Read source table row counts",
)


def _plugin() -> object:
    path = ROLE / "filter_plugins" / "pg_service.py"
    spec = importlib.util.spec_from_file_location("pg_service", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pg_service() -> Callable[[str, str], str]:
    service: Callable[[str, str], str] = getattr(_plugin(), "pg_service")  # noqa: B009
    return service


def _problem() -> Callable[[str], str]:
    problem: Callable[[str], str] = getattr(_plugin(), "pg_service_problem")  # noqa: B009
    return problem


@pytest.mark.parametrize(
    ("url", "lines"),
    [
        (
            f"postgresql://app:{ENCODED}@db.example.test:6543/crm?sslmode=require\n",
            [
                "user=app",
                f"password={PASSWORD}",
                "host=db.example.test",
                "port=6543",
                "dbname=crm",
                "sslmode=require",
            ],
        ),
        (
            "postgres://us%20er@[2001:db8::1]:5432/my%2Fdb",
            ["user=us er", "host=2001:db8::1", "port=5432", "dbname=my/db"],
        ),
        (
            "postgresql://a:b@h1:5432,h2/crm?target_session_attrs=read-write&ssl=true",
            [
                "user=a",
                "password=b",
                "host=h1,h2",
                "port=5432,",
                "dbname=crm",
                "target_session_attrs=read-write",
                "sslmode=require",
            ],
        ),
        ("postgresql://u:@localhost", ["user=u", "password=", "host=localhost"]),
        (
            "postgresql:///crm?host=%2Fvar%2Frun%2Fpostgresql",
            ["dbname=crm", "host=/var/run/postgresql"],
        ),
    ],
    ids=[
        "password-and-sslmode",
        "ipv6-and-encoded",
        "host-list",
        "empty-password",
        "socket",
    ],
)
def test_the_url_becomes_one_service_section(url: str, lines: list[str]) -> None:
    assert _pg_service()(url, "cloudfall_data_source").splitlines() == [
        "[cloudfall_data_source]",
        *lines,
    ]
    assert _problem()(url) == ""


@pytest.mark.parametrize(
    "url",
    [
        "mysql://zuser:zsecret@db/crm",
        "postgresql://zuser:zsecret@db/crm#zfrag",
        "postgresql://zuser:zsecret%zz@db/crm",
        "postgresql://zuser:zsecret%0Azmore@db/crm",
        "postgresql://zuser:zsecret%20@db/crm",
        "postgresql://zuser:zsecret@[2001:db8::1/crm",
        "postgresql://zuser:zsecret@db:54zx/crm",
        "postgresql://zuser:zsecret@db/crm?zsslmode",
        "postgresql://zuser:zsecret@db/crm?service=zother",
        "postgresql://zuser:zsecret@db/crm?user=zother",
        "postgresql://zuser:zsecret%ff@db/crm",
    ],
    ids=[
        "scheme",
        "fragment",
        "bad-percent",
        "line-break",
        "trailing-space",
        "ipv6-unclosed",
        "port",
        "valueless-parameter",
        "nested-service",
        "user-twice",
        "not-utf8",
    ],
)
def test_a_url_the_service_file_cannot_hold_is_refused_without_quoting_it(
    url: str,
) -> None:
    problem = _problem()(url)

    assert problem
    # Every part the URL carries starts with z; no message word does.
    assert not any(word.startswith("z") for word in problem.split())
    assert "zsecret" not in problem
    assert "%zz" not in problem
    assert "%ff" not in problem


def _flat_tasks() -> list[dict[str, object]]:
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    flat: list[dict[str, object]] = []
    for task in tasks:
        flat.append(task)
        for section in ("block", "always"):
            flat.extend(task.get(section, []))
    return flat


def test_no_task_puts_the_source_url_on_a_command_line() -> None:
    tasks = _flat_tasks()
    commands = [
        task
        for task in tasks
        if "ansible.builtin.shell" in task or "ansible.builtin.command" in task
    ]

    for task in commands:
        module = task.get("ansible.builtin.shell", task.get("ansible.builtin.command"))
        line = json.dumps(module)
        assert "source_url_file" not in line, task["name"]
        assert "lookup(" not in line, task["name"]
        assert "$(" not in line, task["name"]
        assert "service_file" not in line, task["name"]
        assert "password" not in line.lower(), task["name"]
    source = {
        str(task["name"]): task for task in tasks if task.get("name") in SOURCE_TASKS
    }
    assert sorted(source) == sorted(SOURCE_TASKS)
    for task in source.values():
        module = task.get("ansible.builtin.shell", task.get("ansible.builtin.command"))
        assert "--dbname" not in json.dumps(module), task["name"]
        assert task["environment"] == {
            "PGSERVICEFILE": "{{ cloudfall_data_migration_service_file_path }}",
            "PGSERVICE": "{{ cloudfall_data_migration_pg_service }}",
        }
    for task in tasks:
        if "block" not in task and "source_url_file" in json.dumps(task):
            assert task.get("no_log") is True, task["name"]


FAKE_PSQL = """\
#!{python}
import json, os, pathlib, sys
log = pathlib.Path({log!r})
service = pathlib.Path(os.environ["PGSERVICEFILE"])
log.joinpath("seen.json").write_text(json.dumps({{
    "argv": sys.argv[1:],
    "service": os.environ["PGSERVICE"],
    "content": service.read_text(),
    "mode": oct(service.stat().st_mode & 0o777),
}}))
"""


def _staging_play(tmp_path: Path, url_file: Path) -> tuple[Path, Path, Path]:
    """Write the staging and a source query as a local play with a stand-in psql."""
    log = tmp_path / "psql-log"
    log.mkdir()
    psql = tmp_path / "psql"
    psql.write_text(FAKE_PSQL.format(python=sys.executable, log=str(log)), "utf-8")
    psql.chmod(0o700)
    service_file = tmp_path / "host" / ".cloudfall-data-source.pgservice"
    service_file.parent.mkdir()
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    (migrate,) = (
        task
        for task in tasks
        if task["name"] == "Migrate and verify the declared database"
    )
    named = {task["name"]: task for task in migrate["block"]}
    check = named["Check that the source URL can become a libpq service file"]
    refuse = named["Refuse a source URL libpq can't read"]
    stage = named["Stage the source connection as a libpq service file"]
    del stage["ansible.builtin.copy"]["owner"], stage["ansible.builtin.copy"]["group"]
    counts = named["Read source table row counts"]
    counts["ansible.builtin.command"]["argv"][0] = str(psql)
    (remove, _dump) = migrate["always"]
    play = [
        {
            "name": "Replay the source connection tasks of cloudfall_data_migration",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {
                "cloudfall_data_migration_source_url_file": str(url_file),
                "cloudfall_data_migration_service_file_path": str(service_file),
                "cloudfall_data_migration_pg_service": "cloudfall_data_source",
                "cloudfall_data_migration_schemas_variable": (
                    'cloudfall_data_schemas=["public"]'
                ),
                "cloudfall_data_migration_count_query": "SELECT 1",
            },
            "tasks": [
                {
                    "name": "Copy as the role does",
                    "block": [check, refuse, stage, counts],
                    "always": [remove],
                }
            ],
        }
    ]
    playbook = tmp_path / "source-url.yml"
    playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
    return playbook, log, service_file


def _run(playbook: Path) -> subprocess.CompletedProcess[str]:
    binary = shutil.which("ansible-playbook")
    assert binary is not None
    return subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [binary, "-vvv", "--inventory", "localhost,", str(playbook)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
        # The play is the role's tasks outside the role, so it names the
        # role's filters itself.
        env={**os.environ, "ANSIBLE_FILTER_PLUGINS": str(ROLE / "filter_plugins")},
    )


def test_psql_reads_the_password_from_a_removed_0600_service_file(
    tmp_path: Path,
) -> None:
    url_file = tmp_path / "source.url"
    url_file.write_text(f"postgresql://app:{ENCODED}@db.example.test/crm\n", "utf-8")
    playbook, log, service_file = _staging_play(tmp_path, url_file)

    done = _run(playbook)

    assert done.returncode == 0, done.stdout + done.stderr
    seen = json.loads((log / "seen.json").read_text(encoding="utf-8"))
    for secret in (PASSWORD, ENCODED):
        assert secret not in done.stdout + done.stderr
        assert secret not in json.dumps(seen["argv"])
    assert seen["service"] == "cloudfall_data_source"
    assert f"password={PASSWORD}" in seen["content"].splitlines()
    assert seen["mode"] == "0o600"
    assert not service_file.exists()


def test_a_url_libpq_cannot_read_stops_before_any_file_is_written(
    tmp_path: Path,
) -> None:
    url_file = tmp_path / "source.url"
    url_file.write_text(f"mysql://app:{ENCODED}@db.example.test/crm\n", "utf-8")
    playbook, log, service_file = _staging_play(tmp_path, url_file)

    done = _run(playbook)

    assert done.returncode != 0
    assert "must start with postgresql:// or postgres://" in done.stdout
    assert ENCODED not in done.stdout + done.stderr
    assert not (log / "seen.json").exists()
    assert not service_file.exists()
