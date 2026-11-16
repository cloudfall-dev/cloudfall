"""``cloudfall-engine`` with a MariaDB service: secrets and inspect (S-019, #420)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.ansible_data import as_ansible_data
from cloudfall.inventory import PlatformInventory
from cloudfall.mariadb import mysql_native_password_hash
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cloudfall_engine.cli import main
from mariadb_project import SCHEMAS, mariadb_service, write_project

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).parents[2]
ENGINE = ROOT / "engine"
INSPECT = ENGINE / "ansible" / "roles" / "cloudfall_inspect"
PASSWORD = "s3cr3t-billing-value"  # noqa: S105 - the value every output is grepped for


def _inventory(tmp_path: Path) -> Path:
    """Render the service of S-019-1, its host reached locally."""
    project = write_project(tmp_path / "project", mariadb_service())
    rendered = cast(
        "dict[str, Any]",
        as_ansible_data(
            render_ansible_inventory(
                PlatformInventory.from_state(validate_config(project, SCHEMAS))
            )
        ),
    )
    host = rendered["all"]["children"]["cloudfall_servers"]["hosts"]["h1"]
    host.update(
        {
            "ansible_connection": "local",
            "ansible_become": False,
            "ansible_host": "localhost",
            "ansible_python_interpreter": sys.executable,
        }
    )
    path = tmp_path / "ansible-inventory.json"
    path.write_text(json.dumps(rendered), encoding="utf-8")
    return path


FAKE_SOPS = """\
#!{python}
import pathlib, sys
assert sys.argv[1] == "--decrypt"
sys.stdout.write(pathlib.Path(sys.argv[2]).read_text())
"""


def _engine(
    inventory: Path, secrets: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    directory = secrets.parent / "bin"
    directory.mkdir(exist_ok=True)
    sops = directory / "sops"
    sops.write_text(FAKE_SOPS.format(python=sys.executable), encoding="utf-8")
    sops.chmod(0o700)
    return subprocess.run(  # noqa: S603 - fixed interpreter and arguments.
        [
            sys.executable,
            "-m",
            "cloudfall_engine",
            "playbook",
            "run",
            "services",
            "--engine",
            str(ENGINE),
            "--inventory",
            str(inventory),
            "--secrets-dir",
            str(secrets),
            *extra,
        ],
        env={
            **os.environ,
            "PATH": os.pathsep.join((str(directory), os.environ["PATH"])),
        },
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


# proves: S-019-6
def test_s019_6_a_missing_password_file_stops_before_ansible_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory = _inventory(tmp_path)
    secrets = tmp_path / "secrets"

    exit_code = main(
        [
            "playbook",
            "run",
            "services",
            "--engine",
            str(ENGINE),
            "--inventory",
            str(inventory),
            "--secrets-dir",
            str(secrets),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert payload["error"]["code"] == "PRECONDITION"
    message = payload["error"]["message"]
    assert str(secrets / "production" / "services" / "mariadb-main.env") in message
    assert "user billing of MariaDB service mariadb-main" in message


# proves: S-019-6
def test_s019_6_a_missing_password_key_stops_before_ansible_starts(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)
    secrets = tmp_path / "secrets"
    source = secrets / "production" / "services" / "mariadb-main.env"
    source.parent.mkdir(parents=True)
    source.write_text("OTHER=x\n", encoding="utf-8")

    done = _engine(inventory, secrets)

    payload = json.loads(done.stdout)
    assert payload["error"]["code"] == "PRECONDITION", done.stdout
    assert "BILLING_PASSWORD is missing from" in payload["error"]["message"]
    assert "PLAY [" not in done.stderr


# proves: S-019-6
def test_s019_6_the_run_hands_over_a_hash_file_and_prints_no_password(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)
    secrets = tmp_path / "secrets"
    source = secrets / "production" / "services" / "mariadb-main.env"
    source.parent.mkdir(parents=True)
    source.write_text(f"BILLING_PASSWORD={PASSWORD}\n", encoding="utf-8")

    # A tag no task carries: Ansible gathers facts and changes nothing.
    done = _engine(inventory, secrets, "--tags", "s019-no-task", "-vvv")

    assert done.returncode == 0, done.stdout + done.stderr
    staged = re.search(
        r"cloudfall-passwords-[^/]+/mariadb/mariadb-main-billing\.hash", done.stderr
    )
    assert staged is not None, done.stderr
    assert not any(Path(tempfile.gettempdir()).glob(staged.group(0)))
    output = done.stdout + done.stderr
    assert PASSWORD not in output
    assert mysql_native_password_hash(PASSWORD) not in output
    assert PASSWORD not in inventory.read_text(encoding="utf-8")
    # The answer names no MariaDB owner: owned_roles stays PostgreSQL's
    assert json.loads(done.stdout)["data"]["owned_roles"] == []


FAKE_ANSIBLE = """\
#!{python}
import json, pathlib, sys
if "--version" in sys.argv:
    print("ansible-playbook [core 2.21.2]")
    sys.exit(0)
extra = {{}}
for index, item in enumerate(sys.argv):
    if item == "--extra-vars" and sys.argv[index + 1].startswith("{{"):
        extra.update(json.loads(sys.argv[index + 1]))
directory = pathlib.Path(extra["cloudfall_services_mariadb_report_directory"])
(directory / "mariadb-main.json").write_text({report!r})
print("fatal: [h1]: FAILED! PRECONDITION", file=sys.stderr)
sys.exit(2)
"""


# proves: S-019-14
def test_s019_14_a_refusal_the_role_reports_answers_precondition(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)
    secrets = tmp_path / "secrets"
    source = secrets / "production" / "services" / "mariadb-main.env"
    source.parent.mkdir(parents=True)
    source.write_text(f"BILLING_PASSWORD={PASSWORD}\n", encoding="utf-8")
    report = {
        "service": "mariadb-main",
        "server": "h1",
        "outcome": "refused",
        "action": "refuse",
        "code": "service_mariadb_promotion_unsafe",
        "message": "the source is at GTID 0-1-12, this replica has applied 0-1-9",
        "sourcePosition": "0-1-12",
        "replicaPosition": "0-1-9",
    }
    directory = tmp_path / "bin"
    directory.mkdir()
    ansible = directory / "ansible-playbook"
    ansible.write_text(
        FAKE_ANSIBLE.format(python=sys.executable, report=json.dumps(report)),
        encoding="utf-8",
    )
    ansible.chmod(0o700)

    done = _engine(inventory, secrets)

    error = json.loads(done.stdout)["error"]
    assert error["code"] == "PRECONDITION", done.stdout + done.stderr
    assert "0-1-12" in error["message"]
    assert "0-1-9" in error["message"]


def test_inspect_reads_the_mariadb_services_of_its_server_only() -> None:
    play = cast(
        "list[dict[str, Any]]",
        DataLoader().load_from_file(
            str(ENGINE / "ansible" / "playbooks" / "inspect.yml")
        ),
    )
    expression = play[0]["roles"][0]["cloudfall_inspect_mariadb_services"]
    home = {"id": "mariadb-main", "serviceKind": "mariadb", "server": "h1"}
    elsewhere = {"id": "mariadb-replica", "serviceKind": "mariadb", "server": "h2"}
    variables = {
        "cloudfall_server_id": "h1",
        "cloudfall_services": [
            home,
            elsewhere,
            {"serviceKind": "redis", "server": "h1"},
        ],
    }

    selected = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(expression)
    )

    assert selected == [home]


def _evidence(results: list[dict[str, object]]) -> object:
    tasks = cast(
        "list[dict[str, Any]]",
        DataLoader().load_from_file(str(INSPECT / "tasks" / "mariadb_accounts.yml")),
    )
    attach = next(
        task
        for task in tasks
        if task["name"] == "Attach the MariaDB server's databases and users"
    )
    defaults = cast(
        "dict[str, Any]",
        DataLoader().load_from_file(str(INSPECT / "defaults" / "main.yml")),
    )
    variables: dict[str, object] = {
        **{
            key: trust_as_template(value) if isinstance(value, str) else value
            for key, value in attach["vars"].items()
        },
        "cloudfall_inspect_mariadb_queries": defaults[
            "cloudfall_inspect_mariadb_queries"
        ],
        "cloudfall_inspect_mariadb_service": {"id": "mariadb-main"},
        "cloudfall_inspect_mariadb_read": {"results": results},
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(
        trust_as_template("{{ cloudfall_inspect_mariadb_evidence }}")
    )


# proves: S-019-7
def test_s019_7_inspect_hands_audit_the_names_never_a_hash() -> None:
    answers = {
        "users": ["root@localhost", "crm@localhost", "analyst@%"],
        "managedUsers": ["crm@localhost"],
        "lockedUsers": [],
        "sslTypes": ["analyst@% NONE", "billing@10.0.0.0/255.255.255.0 ANY"],
        "databases": ["mysql", "crm"],
        "managedDatabases": ["crm"],
    }
    results: list[dict[str, object]] = [
        {"item": {"key": key}, "rc": 0, "stdout_lines": lines}
        for key, lines in answers.items()
    ]

    assert _evidence(results) == {
        "service": "mariadb-main",
        "available": True,
        **answers,
    }
    results[0] = {"item": {"key": "users"}, "rc": 1, "stdout_lines": []}
    assert _evidence(results) == {"service": "mariadb-main", "available": False}
    # Each read names users and databases only
    defaults = cast(
        "dict[str, Any]",
        DataLoader().load_from_file(str(INSPECT / "defaults" / "main.yml")),
    )
    queries = " ".join(defaults["cloudfall_inspect_mariadb_queries"].values())
    assert "authentication_string" not in queries
    assert "password" not in queries.lower()


# proves: S-019-11
def test_s019_11_inspect_reads_each_remote_accounts_tls_requirement() -> None:
    # REQUIRE SSL removed by hand must reach audit (#435): the read names each
    # remote account with its ssl_type, an absent one being NONE
    defaults = cast(
        "dict[str, Any]",
        DataLoader().load_from_file(str(INSPECT / "defaults" / "main.yml")),
    )
    query = " ".join(defaults["cloudfall_inspect_mariadb_queries"]["sslTypes"].split())

    assert "JSON_VALUE(Priv, '$.ssl_type')" in query
    assert "IFNULL(JSON_VALUE(Priv, '$.ssl_type'), '0') + 1" in query
    assert "'NONE', 'ANY', 'X509', 'SPECIFIED'" in query
    assert query.endswith("FROM mysql.global_priv WHERE Host NOT IN ('', 'localhost')")
