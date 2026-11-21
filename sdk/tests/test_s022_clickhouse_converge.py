"""What the controller derives and stages to converge one ClickHouse replica (S-022).

``services.yml`` needs a Debian 13 host, so the converge runs with a recording
step runner that reads every staged file mid-run; each output is grepped for
the passwords. The role's own decisions are tested in
``engine/tests/test_s022_clickhouse_role.py``.
"""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from clickhouse_project import SCHEMAS, clickhouse_service, write_project
from cloudfall.audit import AuditStatus, audit_inventory
from cloudfall.clickhouse import owner_salt, sha256_password_hash
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import (
    ERROR_SERVICES_SECRET,
    EngineContext,
    ExecutionStep,
    LifecycleError,
    converge_services,
)
from cloudfall.observation import load_observations
from cloudfall.requirements import (
    PackageRequirement,
    SecretRequirement,
    SecretSource,
    VolumePlacement,
    VolumeRequirement,
    requirements_of,
)
from cloudfall.secrets import CLICKHOUSE_FILES_VARIABLE, SopsSecretProvider
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from elasticsearch_hosts import FakeNodes

ROOT = Path(__file__).parents[2]
EVENTS_PASSWORD = "synthetic-events-value"  # noqa: S105 - grepped for
CLOUDFALL_PASSWORD = "synthetic-cloudfall-value"  # noqa: S105 - grepped for
SECRETS = (
    f"EVENTS_PASSWORD={EVENTS_PASSWORD}\nCLOUDFALL_PASSWORD={CLOUDFALL_PASSWORD}\n"
)
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"


def _one_replica() -> dict[str, object]:
    return clickhouse_service(
        replicas=["h1"], keeper=None, clientAccess=None, maxServerMemoryMb=4096
    )


def _project(tmp_path: Path, secrets: str | None) -> Path:
    project = write_project(tmp_path / "project", _one_replica(), servers=("h1",))
    if secrets is not None:
        source = project / "secrets" / "production" / "services" / "clickhouse-main.env"
        source.parent.mkdir(parents=True)
        source.write_text(secrets, encoding="utf-8")
    return project


@dataclass(frozen=True)
class Finished:
    """A step that exited 0 and printed nothing."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class Recorder:
    """Steps a converge started, and every staged ClickHouse file's content and mode."""

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
                "dict[str, dict[str, object]]",
                extra.get(CLICKHOUSE_FILES_VARIABLE, {}),
            )
            for service in files.values():
                owners = cast("dict[str, str]", service["owners"])
                for raw in (str(service["cloudfallPassword"]), *owners.values()):
                    path = Path(raw)
                    self.staged[path] = (
                        path.read_text(encoding="utf-8"),
                        stat.S_IMODE(path.stat().st_mode),
                    )
        return Finished()


def _context(project: Path, recorder: Recorder) -> EngineContext:
    return EngineContext(
        project_directory=project,
        schema_directory=SCHEMAS,
        engine_directory=ROOT / "engine",
        inventory_file=project / "tmp" / "ansible-inventory.json",
        run=recorder,
    )


def _provider(project: Path) -> SopsSecretProvider:
    return SopsSecretProvider(
        secrets_directory=project / "secrets",
        decrypt=lambda path: path.read_text(encoding="utf-8"),
    )


def _converge(project: Path) -> Recorder:
    recorder = Recorder()
    converge_services(_context(project, recorder), _provider(project))
    return recorder


# proves: S-022-3
def test_s022_3_one_replica_needs_its_mirrored_volume_packages_and_password(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, None)
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (server,) = requirements_of(inventory)
    specs = [item.spec for item in server.requirements]

    assert server.server_id.value == "h1"
    (volume,) = (spec for spec in specs if isinstance(spec, VolumeRequirement))
    assert volume.name.value == "clickhouse"
    assert volume.path.value == "/var/lib/clickhouse"
    assert volume.minimum_size.value == 20 * 1024**3
    assert volume.placement is VolumePlacement.MIRRORED
    assert [unit.value for unit in volume.units] == ["clickhouse-server.service"]
    packages = [spec for spec in specs if isinstance(spec, PackageRequirement)]
    assert sorted((item.package.value, item.version.value) for item in packages) == [
        ("clickhouse-client", "26.8.37.168"),
        ("clickhouse-common-static", "26.8.37.168"),
        ("clickhouse-server", "26.8.37.168"),
    ]
    (password,) = (spec for spec in specs if isinstance(spec, SecretRequirement))
    assert password.source is SecretSource.GENERATE
    assert password.path.value == "/etc/clickhouse-server/cloudfall-client.xml"
    assert password.reference.path.value == "/services/clickhouse-main"
    assert password.reference.key.value == "CLOUDFALL_PASSWORD"
    # One replica on loopback: no port, no certificate
    assert len(specs) == 5


# proves: S-022-3
def test_s022_3_the_converge_decides_the_clickhouse_volume_before_services(
    tmp_path: Path,
) -> None:
    recorder = _converge(_project(tmp_path, SECRETS))

    assert recorder.nodes.playbooks[0] == "logging-volumes.yml"
    assert recorder.nodes.hosts["logging-volumes.yml"] == "h1"
    actions = cast(
        "dict[str, list[dict[str, object]]]", recorder.extra["cloudfall_volume_actions"]
    )
    (volume,) = actions["h1"]
    assert volume["name"] == "clickhouse"
    assert volume["path"] == "/var/lib/clickhouse"
    assert volume["type"] == "raid1"
    assert volume["action"] == "create"


# proves: S-022-6
def test_s022_6_only_hashes_of_owners_are_staged_and_no_password_reaches_output(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, SECRETS)

    recorder = _converge(project)

    files = cast(
        "dict[str, dict[str, object]]", recorder.extra[CLICKHOUSE_FILES_VARIABLE]
    )
    service = files["clickhouse-main"]
    password = Path(str(service["cloudfallPassword"]))
    owner = Path(cast("dict[str, str]", service["owners"])["events"])
    assert recorder.staged[password] == (CLOUDFALL_PASSWORD, 0o600)
    # The owner's file holds its salted SHA-256 and the salt, never the password
    content, mode = recorder.staged[owner]
    salt = owner_salt(CLOUDFALL_PASSWORD, "clickhouse-main", "events")
    assert (content, mode) == (
        f"{sha256_password_hash(EVENTS_PASSWORD, salt)}:{salt}",
        0o600,
    )
    assert (
        content.split(":")[0]
        == hashlib.sha256((EVENTS_PASSWORD + salt).encode()).hexdigest()
    )
    assert EVENTS_PASSWORD not in content
    # Removed after the run, and never inside the project
    for staged in (password, owner):
        assert not staged.exists()
        assert project not in staged.parents
    inventory = json.dumps(
        render_ansible_inventory(
            PlatformInventory.from_state(validate_config(project, SCHEMAS))
        )
    )
    for secret in (EVENTS_PASSWORD, CLOUDFALL_PASSWORD):
        assert secret not in inventory
        for step in recorder.steps:
            assert secret not in " ".join(step.argv)
            assert secret not in json.dumps(dict(step.environment))


# proves: S-022-4
def test_s022_4_the_same_password_stages_the_same_hash(tmp_path: Path) -> None:
    project = _project(tmp_path, SECRETS)

    first = _converge(project)
    second = _converge(project)

    def owner_hash(recorder: Recorder) -> str:
        files = cast(
            "dict[str, dict[str, object]]", recorder.extra[CLICKHOUSE_FILES_VARIABLE]
        )
        path = Path(
            cast("dict[str, str]", files["clickhouse-main"]["owners"])["events"]
        )
        return recorder.staged[path][0]

    assert owner_hash(first) == owner_hash(second)


# proves: S-022-6
@pytest.mark.parametrize(
    ("secrets", "expected", "names"),
    [
        pytest.param(
            None,
            "clickhouse-main.env",
            "user events of database events of ClickHouse service clickhouse-main",
            id="a missing file",
        ),
        pytest.param(
            f"CLOUDFALL_PASSWORD={CLOUDFALL_PASSWORD}\n",
            "EVENTS_PASSWORD is missing",
            "user events of database events of ClickHouse service clickhouse-main",
            id="a missing passwordRef key",
        ),
        pytest.param(
            f"EVENTS_PASSWORD={EVENTS_PASSWORD}\n",
            "CLOUDFALL_PASSWORD is missing",
            "cloudfall password of ClickHouse service clickhouse-main",
            id="a missing cloudfall password",
        ),
    ],
)
def test_s022_6_a_missing_secret_stops_before_any_playbook(
    tmp_path: Path, secrets: str | None, expected: str, names: str
) -> None:
    project = _project(tmp_path, secrets)
    recorder = Recorder()

    with pytest.raises(LifecycleError) as error:
        converge_services(_context(project, recorder), _provider(project))

    # The converge command answers a LifecycleError with PRECONDITION
    assert error.value.code == ERROR_SERVICES_SECRET
    assert expected in error.value.detail
    assert names in error.value.detail
    assert EVENTS_PASSWORD not in error.value.detail
    assert CLOUDFALL_PASSWORD not in error.value.detail
    assert recorder.steps == []


def test_a_project_without_clickhouse_stages_nothing_for_it(tmp_path: Path) -> None:
    project = write_project(tmp_path / "project", servers=("h1",))

    recorder = _converge(project)

    assert CLICKHOUSE_FILES_VARIABLE not in recorder.extra


def _accounts(**fields: object) -> dict[str, object]:
    return {
        "service": "clickhouse-main",
        "available": True,
        "databases": ["INFORMATION_SCHEMA", "default", "events", "system"],
        "managedDatabases": ["events"],
        "users": ["cloudfall", "events"],
        "managedUsers": ["events"],
        "lockedUsers": [],
        **fields,
    }


def _audit(tmp_path: Path, evidence: dict[str, object]) -> dict[str, object]:
    project = _project(tmp_path, None)
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    observations = tmp_path / "observed"
    observations.mkdir()
    document = json.loads((COMPLIANT / "h1.json").read_text(encoding="utf-8"))
    document["spec"]["clickhouseAccounts"] = [evidence]
    (observations / "h1.json").write_text(json.dumps(document), encoding="utf-8")
    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))
    (server,) = (item for item in report.servers if item.server_id == "h1")
    (check,) = (
        item
        for item in server.checks
        if item.check == "clickhouse.accounts[clickhouse-main]"
    )
    return {
        "status": check.status,
        "observed": check.observed,
        "message": check.message,
    }


# proves: S-022-3
def test_s022_3_audit_finds_the_declared_database_and_owner(tmp_path: Path) -> None:
    found = _audit(tmp_path, _accounts())

    assert found["status"] is AuditStatus.COMPLIANT


# proves: S-022-3
def test_s022_3_a_default_user_is_drift(tmp_path: Path) -> None:
    found = _audit(tmp_path, _accounts(users=["cloudfall", "default", "events"]))

    assert found["status"] is AuditStatus.DRIFT
    assert "the default user exists" in str(found["message"])


# proves: S-022-7
def test_s022_7_audit_lists_the_removed_database_and_a_hand_made_user(
    tmp_path: Path,
) -> None:
    found = _audit(
        tmp_path,
        _accounts(
            databases=["default", "events", "reports", "scratch", "system"],
            managedDatabases=["events", "reports"],
            users=["cloudfall", "events", "ops", "reports"],
            managedUsers=["events", "reports"],
            lockedUsers=["reports"],
        ),
    )

    # The converge kept the removed database and locked its owner out: the
    # declared outcome, not drift
    assert found["status"] is AuditStatus.COMPLIANT
    observed = cast("dict[str, object]", found["observed"])
    assert observed["undeclaredDatabases"] == ["reports"]
    assert observed["undeclaredUsers"] == ["reports"]
    assert observed["unmanagedDatabases"] == ["scratch"]
    assert observed["unmanagedUsers"] == ["ops"]
    message = str(found["message"])
    assert "undeclared (made by Cloudfall, kept, owner can't log in)" in message
    assert "unmanaged (not made by Cloudfall, left as they are): databases scratch" in (
        message
    )


# proves: S-022-7
def test_s022_7_an_undeclared_owner_that_can_still_log_in_is_drift(
    tmp_path: Path,
) -> None:
    found = _audit(
        tmp_path,
        _accounts(
            databases=["events", "reports"],
            managedDatabases=["events", "reports"],
            users=["cloudfall", "events", "reports"],
            managedUsers=["events", "reports"],
        ),
    )

    assert found["status"] is AuditStatus.DRIFT
    observed = cast("dict[str, object]", found["observed"])
    assert observed["undeclaredUsersThatLogIn"] == ["reports"]


def test_a_locked_declared_owner_is_drift(tmp_path: Path) -> None:
    found = _audit(tmp_path, _accounts(lockedUsers=["events"]))

    assert found["status"] is AuditStatus.DRIFT


def test_audit_is_unknown_when_the_server_was_not_read(tmp_path: Path) -> None:
    found = _audit(tmp_path, {"service": "clickhouse-main", "available": False})

    assert found["status"] is AuditStatus.UNKNOWN
