"""``cloudfall-engine playbook run services.yml`` with an owned database (S-006)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cloudfall_engine.cli import main

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ENGINE = ROOT / "engine"
PASSWORD = "s3cr3t-billing-value"  # noqa: S105 - the value every output is grepped for
PASSWORD_REF = {
    "environment": "production",
    "path": "/billing/postgresql",
    "key": "BILLING_PASSWORD",
}


def _inventory(tmp_path: Path) -> Path:
    """Render the owned service of S-006-1, its host reached locally."""
    project = tmp_path / "project"
    for kind in ("servers", "server-types", "services"):
        (project / kind).mkdir(parents=True)
    shutil.copy(EXAMPLES / "servers" / "h1.yaml", project / "servers" / "h1.yaml")
    shutil.copy(
        EXAMPLES / "server-types" / "debian-application.yaml",
        project / "server-types" / "debian-application.yaml",
    )
    service = {
        "apiVersion": "cloudfall/v1",
        "kind": "Service",
        "metadata": {"id": "postgresql-billing"},
        "spec": {
            "serviceKind": "postgresql",
            "environment": "production",
            "server": "h1",
            "bind": {"address": "127.0.0.1", "port": 5432},
            "postgresql": {
                "majorVersion": "17",
                "databases": [
                    {"name": "billing", "owner": "billing", "passwordRef": PASSWORD_REF}
                ],
            },
            "backup": {
                "directory": "/var/backups/cloudfall/postgresql-billing",
                "onCalendar": "*-*-* 02:00:00 UTC",
                "retentionDays": 14,
            },
        },
    }
    (project / "services" / "postgresql-billing.yaml").write_text(
        yaml.safe_dump(service), encoding="utf-8"
    )
    rendered: dict[str, Any] = render_ansible_inventory(
        PlatformInventory.from_state(validate_config(project, SCHEMAS))
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


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, Any]:
    exit_code = main(argv)
    return exit_code, json.loads(capsys.readouterr().out)


# proves: S-006-7
def test_s006_7_a_missing_secret_file_stops_before_ansible_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory = _inventory(tmp_path)
    secrets = tmp_path / "secrets"

    exit_code, payload = _run(
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
        ],
        capsys,
    )

    assert exit_code != 0
    assert payload["error"]["code"] == "PRECONDITION"
    message = payload["error"]["message"]
    assert str(secrets / "production" / "billing" / "postgresql.env") in message
    assert "BILLING_PASSWORD" in message


# proves: S-006-7
def test_s006_7_a_missing_key_stops_before_ansible_starts(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    secrets = tmp_path / "secrets"
    source = secrets / "production" / "billing" / "postgresql.env"
    source.parent.mkdir(parents=True)
    source.write_text("OTHER=x\n", encoding="utf-8")
    fake = _fake_sops(tmp_path)

    done = _engine(inventory, secrets, fake)

    payload = json.loads(done.stdout)
    assert payload["error"]["code"] == "PRECONDITION", done.stdout
    assert "BILLING_PASSWORD is missing from" in payload["error"]["message"]
    assert str(source) in payload["error"]["message"]
    assert "PLAY [" not in done.stderr


def test_other_playbooks_and_syntax_checks_read_no_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory = _inventory(tmp_path)

    exit_code, payload = _run(
        [
            "playbook",
            "run",
            "services",
            "--engine",
            str(ENGINE),
            "--inventory",
            str(inventory),
            "--secrets-dir",
            str(tmp_path / "absent"),
            "--syntax-check",
        ],
        capsys,
    )

    assert exit_code == 0, payload
    assert payload["data"]["owned_roles"] == []


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("hosts.ini", "[web]\nlocalhost ansible_connection=local\n"),
        ("hosts.json", '{"all": {"hosts": {"localhost": {}}}}\n'),
    ],
    ids=["ini", "json-of-another-shape"],
)
def test_a_plain_inventory_runs_services_as_before_without_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, content: str
) -> None:
    inventory = tmp_path / name
    inventory.write_text(content, encoding="utf-8")

    exit_code, payload = _run(
        [
            "playbook",
            "run",
            "services.yml",
            "--engine",
            str(ENGINE),
            "--inventory",
            str(inventory),
            "--secrets-dir",
            str(tmp_path / "absent"),
        ],
        capsys,
    )

    # No cloudfall_servers host: Ansible matches none and exits 0, as on main.
    assert exit_code == 0, payload
    assert payload["data"]["effect"] == "updated"
    assert payload["data"]["owned_roles"] == []


FAKE_SOPS = """\
#!{python}
import pathlib, sys
assert sys.argv[1] == "--decrypt"
sys.stdout.write(pathlib.Path(sys.argv[2]).read_text())
"""


def _fake_sops(tmp_path: Path) -> Path:
    """Write a ``sops`` stand-in for the engine child's PATH: the files are plain."""
    directory = tmp_path / "bin"
    directory.mkdir()
    sops = directory / "sops"
    sops.write_text(FAKE_SOPS.format(python=sys.executable), encoding="utf-8")
    sops.chmod(0o700)
    return directory


def _engine(
    inventory: Path, secrets: Path, fake: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PATH": os.pathsep.join((str(fake), os.environ["PATH"])),
    }
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
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


# proves: S-006-6
def test_s006_6_the_answer_names_the_reference_key_and_hash_only(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)
    secrets = tmp_path / "secrets"
    source = secrets / "production" / "billing" / "postgresql.env"
    source.parent.mkdir(parents=True)
    source.write_text(f"BILLING_PASSWORD={PASSWORD}\n", encoding="utf-8")
    fake = _fake_sops(tmp_path)

    # A tag no task carries: Ansible gathers facts and changes nothing.
    done = _engine(inventory, secrets, fake, "--tags", "s006-no-task", "-vvv")

    assert done.returncode == 0, done.stdout + done.stderr
    payload = json.loads(done.stdout)
    assert payload["data"]["owned_roles"] == [
        {
            "service": "postgresql-billing",
            "owner": "billing",
            "password_ref": PASSWORD_REF,
            "key": "BILLING_PASSWORD",
            "sha256": hashlib.sha256(PASSWORD.encode()).hexdigest(),
        }
    ]
    staged = re.search(
        r"cloudfall-passwords-[^/]+/postgresql-billing-billing", done.stderr
    )
    assert staged is not None, done.stderr
    assert not any(Path(tempfile.gettempdir()).glob(staged.group(0)))
    assert PASSWORD not in done.stdout + done.stderr
    assert PASSWORD not in inventory.read_text(encoding="utf-8")
