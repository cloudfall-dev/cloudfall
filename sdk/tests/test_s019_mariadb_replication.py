"""What the controller stages and answers for a MariaDB pair (S-019, #423).

``services.yml`` needs two live hosts, so the converge runs with a recording
step runner: it reads every staged replication file mid-run and writes the
report the role writes, a promotion or a refusal, into the directory the
converge handed it. Each output is grepped for the password.
"""

from __future__ import annotations

import datetime
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from cloudfall.domain import ResourceId
from cloudfall.elasticsearch_converge import MARIADB_DELIVERED_VARIABLE
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import (
    ERROR_SERVICES_SECRET,
    EngineContext,
    ExecutionStep,
    LifecycleError,
    converge_services,
)
from cloudfall.mariadb import mysql_native_password_hash
from cloudfall.mariadb_replication import (
    REPORT_DIRECTORY_VARIABLE,
    ReplicationOutcome,
    read_outcomes,
)
from cloudfall.pki import load_or_create_ca
from cloudfall.secret_store import SecretStore
from cloudfall.secrets import (
    MARIADB_REPLICATION_FILES_VARIABLE,
    SopsSecretProvider,
    mariadb_replication_pairs,
)
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from elasticsearch_hosts import FakeNodes
from mariadb_project import (
    SCHEMAS,
    mariadb_service,
    primary_replication,
    replica_service,
    write_project,
)

ROOT = Path(__file__).parents[2]
REPLICATION_PASSWORD = "synthetic-replication-password-0123456789"  # noqa: S105
SECRETS = (
    "BILLING_PASSWORD=synthetic-billing-value\n"
    f"REPLICATION_PASSWORD={REPLICATION_PASSWORD}\n"
)


@dataclass(frozen=True)
class Finished:
    """A step that exited with ``returncode`` and printed nothing."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class Recorder:
    """Steps a converge started, its staged replication files, and a report."""

    def __init__(self, report: dict[str, str] | None, returncode: int = 0) -> None:
        """Write ``report`` as the role would when services.yml runs."""
        self.report = report
        self.returncode = returncode
        self.steps: list[ExecutionStep] = []
        self.staged: dict[str, dict[str, tuple[str, int]]] = {}
        self.delivered: dict[str, list[str]] = {}
        self.nodes = FakeNodes()

    def __call__(self, step: ExecutionStep) -> Finished:
        """Record the step; on services.yml read the files and write the report."""
        self.steps.append(step)
        if "--extra-vars" not in step.argv:
            return Finished()
        extra = json.loads(step.argv[step.argv.index("--extra-vars") + 1])
        self.nodes.answer(Path(step.argv[-1]).name, extra)
        if not step.argv[-1].endswith("services.yml"):
            return Finished()
        self.delivered = {
            server: [item["path"] for item in files]
            for server, files in extra.get(MARIADB_DELIVERED_VARIABLE, {}).items()
        }
        files = cast(
            "dict[str, dict[str, str]]", extra[MARIADB_REPLICATION_FILES_VARIABLE]
        )
        for service, paths in files.items():
            for name, raw in paths.items():
                path = Path(raw)
                self.staged.setdefault(service, {})[name] = (
                    path.read_text(encoding="utf-8"),
                    stat.S_IMODE(path.stat().st_mode),
                )
        if self.report is not None:
            directory = Path(str(extra[REPORT_DIRECTORY_VARIABLE]))
            (directory / f"{self.report['service']}.json").write_text(
                json.dumps(self.report), encoding="utf-8"
            )
        return Finished(self.returncode)


def _project(tmp_path: Path, secrets: str) -> Path:
    project = write_project(
        tmp_path / "project",
        mariadb_service(replication=primary_replication()),
        replica_service(),
        servers=("h1", "h2"),
    )
    source = project / "secrets" / "production" / "services" / "mariadb-main.env"
    source.parent.mkdir(parents=True)
    source.write_text(secrets, encoding="utf-8")
    store = SecretStore(
        project / "secrets",
        decrypt=lambda path: path.read_text(encoding="utf-8"),
        encrypt=lambda _, text: text,
    )
    load_or_create_ca(
        store, ResourceId("production"), datetime.datetime.now(datetime.UTC)
    )
    return project


def _converge(
    project: Path, recorder: Recorder, outcomes: list[ReplicationOutcome]
) -> None:
    context = EngineContext(
        project_directory=project,
        schema_directory=SCHEMAS,
        engine_directory=ROOT / "engine",
        inventory_file=project / "tmp" / "ansible-inventory.json",
        run=recorder,
    )
    provider = SopsSecretProvider(
        secrets_directory=project / "secrets",
        decrypt=lambda path: path.read_text(encoding="utf-8"),
    )
    converge_services(context, provider, replication=outcomes)


# proves: S-019-12
def test_s019_12_the_primary_gets_the_hash_and_the_replica_the_password(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, SECRETS)
    recorder = Recorder(None)

    _converge(project, recorder, [])

    primary = recorder.staged["mariadb-main"]
    replica = recorder.staged["mariadb-replica"]
    assert set(primary) == {"hash"}
    assert set(replica) == {"password"}
    assert primary["hash"] == (mysql_native_password_hash(REPLICATION_PASSWORD), 0o600)
    assert replica["password"] == (REPLICATION_PASSWORD, 0o600)
    # Both servers get their certificate from the project CA, and that CA,
    # which the replica verifies the primary's certificate against
    for server in ("h1", "h2"):
        assert sorted(recorder.delivered[server]) == [
            "/etc/mysql/cloudfall/ca.crt",
            "/etc/mysql/cloudfall/server.crt",
        ]
    inventory = json.dumps(
        render_ansible_inventory(
            PlatformInventory.from_state(validate_config(project, SCHEMAS))
        )
    )
    assert REPLICATION_PASSWORD not in inventory
    for step in recorder.steps:
        assert REPLICATION_PASSWORD not in " ".join(step.argv)
        assert REPLICATION_PASSWORD not in json.dumps(dict(step.environment))


# proves: S-019-14
def test_s019_14_a_promotion_answers_the_last_gtid_it_applied(tmp_path: Path) -> None:
    project = _project(tmp_path, SECRETS)
    recorder = Recorder(
        {
            "service": "mariadb-replica",
            "server": "h2",
            "outcome": "promote",
            "reason": "its source can not be reached",
            "lastAppliedGtid": "0-1-42",
        }
    )
    outcomes: list[ReplicationOutcome] = []

    _converge(project, recorder, outcomes)

    (promoted,) = outcomes
    assert promoted.as_dict() == {
        "service": "mariadb-replica",
        "server": "h2",
        "outcome": "promote",
        "reason": "its source can not be reached",
        "lastAppliedGtid": "0-1-42",
    }


# proves: S-019-14
def test_s019_14_a_refused_promotion_stops_as_precondition_naming_both_positions(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, SECRETS)
    recorder = Recorder(
        {
            "service": "mariadb-replica",
            "server": "h2",
            "outcome": "refused",
            "action": "refuse",
            "code": "service_mariadb_promotion_unsafe",
            "message": "the source is at GTID 0-1-12, this replica has applied 0-1-9",
            "sourcePosition": "0-1-12",
            "replicaPosition": "0-1-9",
        },
        returncode=2,
    )

    with pytest.raises(LifecycleError) as error:
        _converge(project, recorder, [])

    # Not the failed playbook: the role's refusal, which the command answers
    # with PRECONDITION
    assert error.value.code == "service_mariadb_promotion_unsafe"
    assert "0-1-12" in error.value.detail
    assert "0-1-9" in error.value.detail
    assert error.value.context == {
        "service": "mariadb-replica",
        "server": "h2",
        "sourcePosition": "0-1-12",
        "replicaPosition": "0-1-9",
    }


def test_a_failed_run_without_a_refusal_stays_a_failed_playbook(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, SECRETS)

    with pytest.raises(LifecycleError) as error:
        _converge(project, Recorder(None, returncode=2), [])

    assert error.value.code == "lifecycle_execution_failed"


def test_a_missing_replication_password_stops_before_any_playbook(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, "BILLING_PASSWORD=synthetic-billing-value\n")
    recorder = Recorder(None)

    with pytest.raises(LifecycleError) as error:
        _converge(project, recorder, [])

    assert error.value.code == ERROR_SERVICES_SECRET
    assert "REPLICATION_PASSWORD" in error.value.detail
    assert "replication password of MariaDB primary mariadb-main" in error.value.detail
    assert "cloudfall secrets generate" in error.value.detail
    assert recorder.steps == []


def test_a_replication_password_that_can_not_be_quoted_is_refused(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        "BILLING_PASSWORD=synthetic-billing-value\n"
        "REPLICATION_PASSWORD=it's-a-quote-0123456789\n",
    )

    with pytest.raises(LifecycleError) as error:
        _converge(project, Recorder(None), [])

    assert "can not carry" in error.value.detail
    assert "it's-a-quote" not in error.value.detail


def test_the_pairs_name_each_primary_with_its_replicas(tmp_path: Path) -> None:
    project = _project(tmp_path, SECRETS)
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (pair,) = mariadb_replication_pairs(inventory)

    assert pair.primary.value == "mariadb-main"
    assert pair.environment.value == "production"
    assert [replica.value for replica in pair.replicas] == ["mariadb-replica"]


def test_a_malformed_report_is_refused(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text(
        json.dumps({"service": "x", "outcome": "dropped"}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="malformed"):
        read_outcomes(tmp_path)


CLOUDFALL = Path(sys.executable).with_name("cloudfall")
FAKE_SOPS = f"""#!{sys.executable}
import sys

args = sys.argv[1:]
if args[0] == "--decrypt":
    with open(args[1], encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    kept = [line for line in lines if not line.startswith("sops_")]
    sys.stdout.write("".join(f"{{line}}\\n" for line in kept))
elif args[0] == "--encrypt":
    sys.stdout.write(sys.stdin.read() + "sops_mac=fake\\n")
else:
    sys.exit(f"unexpected sops call: {{args}}")
"""


def test_secrets_generate_makes_the_primary_replication_password_once(
    tmp_path: Path,
) -> None:
    project = write_project(
        tmp_path / "project",
        mariadb_service(replication=primary_replication()),
        replica_service(),
        servers=("h1", "h2"),
    )
    (project / ".sops.yaml").write_text(
        "creation_rules:\n  - path_regex: secrets/.*\\.env$\n    age: age1example\n",
        encoding="utf-8",
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    sops = tools / "sops"
    sops.write_text(FAKE_SOPS, encoding="utf-8")
    sops.chmod(0o755)

    def generate() -> dict[str, object]:
        done = subprocess.run(  # noqa: S603 - the installed CLI, fixed arguments
            [str(CLOUDFALL), "secrets", "generate", "--project", str(project)],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"},
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        return cast("dict[str, object]", json.loads(done.stdout)["data"])

    generated = generate()
    again = generate()

    written = cast("list[dict[str, object]]", generated["written"])
    assert [(item["name"], item["key"]) for item in written] == [
        ("mariadb-replication-password", "REPLICATION_PASSWORD")
    ]
    assert again["written"] == []
    stored = project / "secrets" / "production" / "services" / "mariadb-main.env"
    assert stored.read_text(encoding="utf-8").startswith("REPLICATION_PASSWORD=")
