"""What the controller computes and stages to converge a MariaDB service (S-019, #420).

``services.yml`` needs a Debian 13 host, so the converge runs with a recording
step runner that reads every staged file mid-run; each output is grepped for
the passwords. The hashes are checked against the values MariaDB documents.
"""

from __future__ import annotations

import base64
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from cloudfall.audit import AuditStatus, audit_inventory
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import (
    ERROR_SERVICES_SECRET,
    EngineContext,
    ExecutionStep,
    LifecycleError,
    converge_services,
)
from cloudfall.mariadb import (
    MariadbAuthentication,
    ed25519_password_hash,
    mysql_native_password_hash,
)
from cloudfall.observation import load_observations
from cloudfall.secrets import MARIADB_FILES_VARIABLE, SopsSecretProvider
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from elasticsearch_hosts import FakeNodes
from mariadb_project import (
    BILLING_PASSWORD_REF,
    SCHEMAS,
    mariadb_service,
    write_project,
)

ROOT = Path(__file__).parents[2]
BILLING_PASSWORD = "synthetic-billing-value"  # noqa: S105 - grepped for
REPLICATION_PASSWORD = "synthetic-replication-value"  # noqa: S105 - grepped for
SECRETS = (
    f"BILLING_PASSWORD={BILLING_PASSWORD}\n"
    f"REPLICATION_PASSWORD={REPLICATION_PASSWORD}\n"
)
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"


# proves: S-019-6
def test_s019_6_the_native_hash_is_the_one_mariadb_stores() -> None:
    # PASSWORD('secret') in MariaDB's documentation
    assert (
        mysql_native_password_hash("secret")
        == "*14E65567ABDB5135D0CFD9A70B3032C179A49EE7"
    )
    assert MariadbAuthentication.MYSQL_NATIVE_PASSWORD.password_hash("secret") == (
        "*14E65567ABDB5135D0CFD9A70B3032C179A49EE7"
    )
    with pytest.raises(ValueError, match="must not be empty"):
        mysql_native_password_hash("")


# proves: S-019-21
def test_s019_21_the_ed25519_hash_is_the_one_mariadb_stores() -> None:
    # ed25519_password('secret') in MariaDB's documentation
    assert ed25519_password_hash("secret") == (
        "ZIgUREUg5PVgQ6LskhXmO+eZLS0nC8be6HPjYWR4YJY"
    )
    assert MariadbAuthentication.ED25519.password_hash("secret") == (
        ed25519_password_hash("secret")
    )
    # A 32-byte password is a standard Ed25519 seed: the same public key
    seed = b"0123456789abcdef0123456789abcdef"
    public = (
        Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    assert base64.b64decode(ed25519_password_hash(seed.decode()) + "=") == public
    # No salt: the same password gives the same hash, so it is compared as is
    assert ed25519_password_hash(BILLING_PASSWORD) == ed25519_password_hash(
        BILLING_PASSWORD
    )
    assert ed25519_password_hash(BILLING_PASSWORD) != ed25519_password_hash("other")


class Recorder:
    """Steps a converge started, and every staged MariaDB file's content and mode."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.steps: list[ExecutionStep] = []
        self.extra: dict[str, object] = {}
        self.staged: dict[Path, tuple[str, int]] = {}
        self.nodes = FakeNodes()

    def __call__(self, step: ExecutionStep) -> Finished:
        """Record the step and read the files services.yml was handed."""
        self.steps.append(step)
        if "--extra-vars" in step.argv:
            extra = json.loads(step.argv[step.argv.index("--extra-vars") + 1])
            self.nodes.answer(Path(step.argv[-1]).name, extra)
            if not step.argv[-1].endswith("services.yml"):
                return Finished()
            self.extra = extra
            files = cast(
                "dict[str, dict[str, str]]", extra.get(MARIADB_FILES_VARIABLE, {})
            )
            for owners in files.values():
                for raw in owners.values():
                    path = Path(raw)
                    self.staged[path] = (
                        path.read_text(encoding="utf-8"),
                        stat.S_IMODE(path.stat().st_mode),
                    )
        return Finished()


@dataclass(frozen=True)
class Finished:
    """A step that exited 0 and printed nothing."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _project(
    tmp_path: Path,
    secrets: str | None,
    databases: list[dict[str, object]] | None = None,
) -> Path:
    service = (
        mariadb_service() if databases is None else mariadb_service(databases=databases)
    )
    project = write_project(tmp_path / "project", service)
    if secrets is not None:
        source = project / "secrets" / "production" / "services" / "mariadb-main.env"
        source.parent.mkdir(parents=True)
        source.write_text(secrets, encoding="utf-8")
    return project


def _converge(project: Path) -> Recorder:
    recorder = Recorder()
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
    converge_services(context, provider)
    return recorder


# proves: S-019-6
def test_s019_6_the_file_staged_for_billing_holds_its_hash_only(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, SECRETS)

    recorder = _converge(project)

    files = cast("dict[str, dict[str, str]]", recorder.extra[MARIADB_FILES_VARIABLE])
    staged = Path(files["mariadb-main"]["billing"])
    content, mode = recorder.staged[staged]
    assert mode == 0o600
    assert content == mysql_native_password_hash(BILLING_PASSWORD)
    # Only the owner with a password: the application user has none
    assert set(files["mariadb-main"]) == {"billing"}
    # Removed after the run, and never inside the project
    assert not staged.exists()
    assert project not in staged.parents
    inventory = json.dumps(
        render_ansible_inventory(
            PlatformInventory.from_state(validate_config(project, SCHEMAS))
        )
    )
    for secret in (BILLING_PASSWORD, REPLICATION_PASSWORD):
        assert secret not in inventory
        assert secret not in content
        for step in recorder.steps:
            assert secret not in " ".join(step.argv)
            assert secret not in json.dumps(dict(step.environment))


# proves: S-019-21
def test_s019_21_an_ed25519_owner_is_staged_as_its_ed25519_hash(
    tmp_path: Path,
) -> None:
    databases: list[dict[str, object]] = [
        {"name": "crm", "application": "crm"},
        {
            "name": "billing",
            "owner": "billing",
            "passwordRef": BILLING_PASSWORD_REF,
            "authentication": "ed25519",
        },
    ]
    project = _project(tmp_path, SECRETS, databases=databases)

    recorder = _converge(project)

    files = cast("dict[str, dict[str, str]]", recorder.extra[MARIADB_FILES_VARIABLE])
    content, _ = recorder.staged[Path(files["mariadb-main"]["billing"])]
    assert content == ed25519_password_hash(BILLING_PASSWORD)
    assert BILLING_PASSWORD not in content


# proves: S-019-6
@pytest.mark.parametrize(
    ("secrets", "expected"),
    [
        pytest.param(None, "mariadb-main.env", id="a missing file"),
        pytest.param("OTHER=x\n", "BILLING_PASSWORD is missing", id="a missing key"),
    ],
)
def test_s019_6_a_missing_password_ref_stops_before_any_playbook(
    tmp_path: Path, secrets: str | None, expected: str
) -> None:
    project = _project(tmp_path, secrets)
    recorder = Recorder()
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

    with pytest.raises(LifecycleError) as error:
        converge_services(context, provider)

    assert error.value.code == ERROR_SERVICES_SECRET
    assert expected in error.value.detail
    assert "user billing of MariaDB service mariadb-main" in error.value.detail
    assert recorder.steps == []


# proves: S-019-3
def test_s019_3_the_converge_decides_the_mariadb_volume_before_services(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, SECRETS)

    recorder = _converge(project)

    assert recorder.nodes.playbooks[0] == "logging-volumes.yml"
    assert recorder.nodes.hosts["logging-volumes.yml"] == "h1"
    # No certificate is asked for: a standalone server needs none
    assert "logging-requests.yml" not in recorder.nodes.playbooks
    actions = cast(
        "dict[str, list[dict[str, object]]]", recorder.extra["cloudfall_volume_actions"]
    )
    (volume,) = actions["h1"]
    assert volume["name"] == "mariadb"
    assert volume["path"] == "/var/lib/mysql"
    assert volume["type"] == "raid1"
    assert volume["action"] == "create"
    assert volume["units"] == ["mariadb.service"]


def _accounts(**fields: object) -> dict[str, object]:
    return {
        "service": "mariadb-main",
        "available": True,
        "users": [
            "root@localhost",
            "mysql@localhost",
            "mariadb.sys@localhost",
            "crm@localhost",
            "billing@localhost",
        ],
        "managedUsers": ["crm@localhost", "billing@localhost"],
        "lockedUsers": ["mariadb.sys@localhost"],
        "databases": [
            "information_schema",
            "mysql",
            "performance_schema",
            "sys",
            "crm",
            "billing",
        ],
        "managedDatabases": ["crm", "billing"],
        **fields,
    }


def _audit(
    tmp_path: Path,
    evidence: dict[str, object],
    databases: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    project = _project(tmp_path, None, databases)
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    observations = tmp_path / "observed"
    observations.mkdir()
    document = json.loads((COMPLIANT / "h1.json").read_text(encoding="utf-8"))
    document["spec"]["mariadbAccounts"] = [evidence]
    (observations / "h1.json").write_text(json.dumps(document), encoding="utf-8")
    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))
    (server,) = (item for item in report.servers if item.server_id == "h1")
    (check,) = (
        item for item in server.checks if item.check == "mariadb.accounts[mariadb-main]"
    )
    return {
        "status": check.status,
        "observed": check.observed,
        "message": check.message,
    }


def test_audit_finds_the_declared_databases_and_users(tmp_path: Path) -> None:
    found = _audit(tmp_path, _accounts())

    assert found["status"] is AuditStatus.COMPLIANT
    observed = cast("dict[str, object]", found["observed"])
    assert observed["unmanagedUsers"] == []
    assert observed["undeclaredUsers"] == []


# proves: S-019-7
def test_s019_7_audit_lists_the_removed_database_and_a_hand_made_user(
    tmp_path: Path,
) -> None:
    evidence = _accounts(
        users=[
            "root@localhost",
            "crm@localhost",
            "billing@localhost",
            "analyst@%",
        ],
        lockedUsers=["billing@localhost"],
        databases=["mysql", "crm", "billing", "scratch"],
    )

    # billing is no longer declared: only crm is
    found = _audit(
        tmp_path, evidence, databases=[{"name": "crm", "application": "crm"}]
    )

    observed = cast("dict[str, object]", found["observed"])
    assert observed["undeclaredDatabases"] == ["billing"]
    assert observed["undeclaredUsers"] == ["billing@localhost"]
    assert observed["unmanagedUsers"] == ["analyst@%"]
    assert observed["unmanagedDatabases"] == ["scratch"]
    # The locked, kept user is the declared outcome, not drift
    assert found["status"] is AuditStatus.COMPLIANT
    message = cast("str", found["message"])
    assert "undeclared (made by Cloudfall, kept, user locked)" in message
    assert "databases billing" in message
    assert "unmanaged (not made by Cloudfall, left as they are)" in message
    assert "users analyst@%" in message


def test_an_undeclared_user_left_unlocked_or_a_missing_one_is_drift(
    tmp_path: Path,
) -> None:
    unlocked = _audit(
        tmp_path / "unlocked",
        _accounts(lockedUsers=[]),
        databases=[{"name": "crm", "application": "crm"}],
    )
    missing = _audit(
        tmp_path / "missing",
        _accounts(users=["root@localhost", "crm@localhost"]),
    )

    assert unlocked["status"] is AuditStatus.DRIFT
    observed = cast("dict[str, object]", unlocked["observed"])
    assert observed["unlockedUndeclaredUsers"] == ["billing@localhost"]
    assert missing["status"] is AuditStatus.DRIFT


def test_audit_is_unknown_when_the_server_was_not_read(tmp_path: Path) -> None:
    found = _audit(tmp_path, {"service": "mariadb-main", "available": False})

    assert found["status"] is AuditStatus.UNKNOWN
