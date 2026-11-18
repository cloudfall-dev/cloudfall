"""What the controller derives and stages to converge one RabbitMQ node (S-023, #506).

``services.yml`` needs a Debian 13 host, so the converge runs with a recording
step runner that reads every staged file mid-run; each output is grepped for
the password and the cookie. The role's own decisions are tested in
``engine/tests/test_s023_rabbitmq_role.py``.
"""

from __future__ import annotations

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
from cloudfall.observation import load_observations
from cloudfall.requirements import (
    PackageRequirement,
    SecretRequirement,
    SecretSource,
    VolumePlacement,
    VolumeRequirement,
    requirements_of,
)
from cloudfall.secrets import RABBITMQ_FILES_VARIABLE, SopsSecretProvider
from cloudfall.validation import ConfigValidationError, validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from elasticsearch_hosts import FakeNodes
from rabbitmq_project import SCHEMAS, edit_server, rabbitmq_service, write_project

ROOT = Path(__file__).parents[2]
JOBS_PASSWORD = "synthetic-jobs-value"  # noqa: S105 - grepped for
COOKIE = "synthetic-erlang-cookie-value"
SECRETS = f"JOBS_PASSWORD={JOBS_PASSWORD}\nERLANG_COOKIE={COOKIE}\n"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"


def _one_node() -> dict[str, object]:
    return rabbitmq_service(nodes=["h1"], clientAccess=None)


def _project(tmp_path: Path, secrets: str | None) -> Path:
    project = write_project(tmp_path / "project", _one_node(), servers=("h1",))
    if secrets is not None:
        source = project / "secrets" / "production" / "services" / "rabbitmq-main.env"
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
    """Steps a converge started, and every staged RabbitMQ file's content and mode."""

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
                "dict[str, dict[str, object]]", extra.get(RABBITMQ_FILES_VARIABLE, {})
            )
            for service in files.values():
                owners = cast("dict[str, str]", service["owners"])
                for raw in (str(service["cookie"]), *owners.values()):
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


# proves: S-023-3
def test_s023_3_one_node_needs_its_mirrored_volume_package_and_cookie(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, None)
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (server,) = requirements_of(inventory)
    specs = [item.spec for item in server.requirements]

    assert server.server_id.value == "h1"
    (volume,) = (spec for spec in specs if isinstance(spec, VolumeRequirement))
    assert volume.name.value == "rabbitmq"
    assert volume.path.value == "/var/lib/rabbitmq"
    assert volume.minimum_size.value == 20 * 1024**3
    assert volume.placement is VolumePlacement.MIRRORED
    assert [unit.value for unit in volume.units] == ["rabbitmq-server.service"]
    (package,) = (spec for spec in specs if isinstance(spec, PackageRequirement))
    assert (package.package.value, package.version.value) == (
        "rabbitmq-server",
        "4.4.1",
    )
    (cookie,) = (spec for spec in specs if isinstance(spec, SecretRequirement))
    assert cookie.source is SecretSource.GENERATE
    assert cookie.path.value == "/var/lib/rabbitmq/.erlang.cookie"
    assert cookie.reference.path.value == "/services/rabbitmq-main"
    assert cookie.reference.key.value == "ERLANG_COOKIE"
    # One node on loopback: no port, no certificate
    assert len(specs) == 3


# proves: S-023-3
def test_s023_3_the_converge_decides_the_rabbitmq_volume_before_services(
    tmp_path: Path,
) -> None:
    recorder = _converge(_project(tmp_path, SECRETS))

    assert recorder.nodes.playbooks[0] == "logging-volumes.yml"
    assert recorder.nodes.hosts["logging-volumes.yml"] == "h1"
    assert "logging-requests.yml" not in recorder.nodes.playbooks
    actions = cast(
        "dict[str, list[dict[str, object]]]", recorder.extra["cloudfall_volume_actions"]
    )
    (volume,) = actions["h1"]
    assert volume["name"] == "rabbitmq"
    assert volume["path"] == "/var/lib/rabbitmq"
    assert volume["type"] == "raid1"
    assert volume["action"] == "create"


# proves: S-023-6
def test_s023_6_the_password_and_cookie_reach_no_output(tmp_path: Path) -> None:
    project = _project(tmp_path, SECRETS)

    recorder = _converge(project)

    files = cast(
        "dict[str, dict[str, object]]", recorder.extra[RABBITMQ_FILES_VARIABLE]
    )
    service = files["rabbitmq-main"]
    cookie = Path(str(service["cookie"]))
    password = Path(cast("dict[str, str]", service["owners"])["jobs"])
    assert recorder.staged[cookie] == (COOKIE, 0o600)
    # Only the controller reads it: the role hashes it there (D-9)
    assert recorder.staged[password] == (JOBS_PASSWORD, 0o600)
    # Removed after the run, and never inside the project
    for staged in (cookie, password):
        assert not staged.exists()
        assert project not in staged.parents
    inventory = json.dumps(
        render_ansible_inventory(
            PlatformInventory.from_state(validate_config(project, SCHEMAS))
        )
    )
    for secret in (JOBS_PASSWORD, COOKIE):
        assert secret not in inventory
        for step in recorder.steps:
            assert secret not in " ".join(step.argv)
            assert secret not in json.dumps(dict(step.environment))


# proves: S-023-6
@pytest.mark.parametrize(
    ("secrets", "expected", "names"),
    [
        pytest.param(
            None,
            "rabbitmq-main.env",
            "user jobs of virtual host jobs of RabbitMQ service rabbitmq-main",
            id="a missing file",
        ),
        pytest.param(
            f"ERLANG_COOKIE={COOKIE}\n",
            "JOBS_PASSWORD is missing",
            "user jobs of virtual host jobs of RabbitMQ service rabbitmq-main",
            id="a missing passwordRef key",
        ),
        pytest.param(
            f"JOBS_PASSWORD={JOBS_PASSWORD}\n",
            "ERLANG_COOKIE is missing",
            "Erlang cookie of RabbitMQ service rabbitmq-main",
            id="a missing cookie",
        ),
    ],
)
def test_s023_6_a_missing_secret_stops_before_any_playbook(
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
    assert JOBS_PASSWORD not in error.value.detail
    assert COOKIE not in error.value.detail
    assert recorder.steps == []


def test_a_project_without_rabbitmq_stages_nothing_for_it(tmp_path: Path) -> None:
    project = write_project(tmp_path / "project", servers=("h1",))

    recorder = _converge(project)

    assert RABBITMQ_FILES_VARIABLE not in recorder.extra


# proves: S-023-18
def test_s023_18_two_nodes_with_one_short_hostname_are_refused(
    tmp_path: Path,
) -> None:
    # RabbitMQ names both rabbit@h2: the first label is the node's name
    project = write_project(tmp_path / "project", rabbitmq_service())
    edit_server(project, "h3", hostname="h2.other.internal")

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project, SCHEMAS)

    assert "service_rabbitmq_hostname" in str(error.value.as_dict())
    assert "short hostname h2" in str(error.value.as_dict())


def _accounts(**fields: object) -> dict[str, object]:
    return {
        "service": "rabbitmq-main",
        "available": True,
        "users": ["jobs"],
        "usersWithoutPassword": [],
        "vhosts": ["jobs"],
        "vhostTags": ["jobs|cloudfall-managed,cloudfall-owner:jobs"],
        **fields,
    }


def _audit(tmp_path: Path, evidence: dict[str, object]) -> dict[str, object]:
    project = _project(tmp_path, None)
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    observations = tmp_path / "observed"
    observations.mkdir()
    document = json.loads((COMPLIANT / "h1.json").read_text(encoding="utf-8"))
    document["spec"]["rabbitmqAccounts"] = [evidence]
    (observations / "h1.json").write_text(json.dumps(document), encoding="utf-8")
    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))
    (server,) = (item for item in report.servers if item.server_id == "h1")
    (check,) = (
        item
        for item in server.checks
        if item.check == "rabbitmq.accounts[rabbitmq-main]"
    )
    return {
        "status": check.status,
        "observed": check.observed,
        "message": check.message,
    }


# proves: S-023-3
def test_s023_3_audit_finds_the_declared_vhost_and_owner(tmp_path: Path) -> None:
    found = _audit(tmp_path, _accounts())

    assert found["status"] is AuditStatus.COMPLIANT


# proves: S-023-3
def test_s023_3_a_guest_user_is_drift(tmp_path: Path) -> None:
    found = _audit(tmp_path, _accounts(users=["jobs", "guest"]))

    assert found["status"] is AuditStatus.DRIFT
    assert "the guest user exists" in str(found["message"])


# proves: S-023-7
def test_s023_7_audit_lists_the_removed_vhost_and_a_hand_made_user(
    tmp_path: Path,
) -> None:
    found = _audit(
        tmp_path,
        _accounts(
            users=["jobs", "reports", "ops"],
            usersWithoutPassword=["reports"],
            vhosts=["jobs", "reports", "/"],
            vhostTags=[
                "jobs|cloudfall-managed,cloudfall-owner:jobs",
                "reports|cloudfall-managed,cloudfall-owner:reports",
                "/|",
            ],
        ),
    )

    # The converge kept the removed vhost and cleared its owner's password:
    # the declared outcome, not drift
    assert found["status"] is AuditStatus.COMPLIANT
    observed = cast("dict[str, object]", found["observed"])
    assert observed["undeclaredVhosts"] == ["reports"]
    assert observed["undeclaredUsers"] == ["reports"]
    assert observed["unmanagedUsers"] == ["ops"]
    assert observed["unmanagedVhosts"] == ["/"]
    message = str(found["message"])
    assert "undeclared (made by Cloudfall, kept, owner can't log in)" in message
    assert "unmanaged (not made by Cloudfall, left as they are)" in message


# proves: S-023-7
def test_s023_7_an_undeclared_owner_that_can_still_log_in_is_drift(
    tmp_path: Path,
) -> None:
    found = _audit(
        tmp_path,
        _accounts(
            users=["jobs", "reports"],
            vhosts=["jobs", "reports"],
            vhostTags=[
                "jobs|cloudfall-managed,cloudfall-owner:jobs",
                "reports|cloudfall-managed,cloudfall-owner:reports",
            ],
        ),
    )

    assert found["status"] is AuditStatus.DRIFT
    observed = cast("dict[str, object]", found["observed"])
    assert observed["undeclaredUsersWithPassword"] == ["reports"]


def test_audit_is_unknown_when_the_cluster_was_not_read(tmp_path: Path) -> None:
    found = _audit(tmp_path, {"service": "rabbitmq-main", "available": False})

    assert found["status"] is AuditStatus.UNKNOWN
