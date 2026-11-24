"""What the controller derives and stages to converge Kibana on a node (S-024, #569).

``services.yml`` needs a Debian 13 host, so the converge runs with a recording
step runner that reads every staged Kibana file mid-run; each output is
grepped for the keys and the passwords. The role's own decisions are tested
in ``engine/tests/test_s024_kibana_role.py``.
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from cloudfall.inventory import PlatformInventory
from cloudfall.kibana import KibanaEncryptionKey
from cloudfall.lifecycle import (
    ERROR_SERVICES_SECRET,
    EngineContext,
    ExecutionStep,
    LifecycleError,
    converge_services,
)
from cloudfall.requirements import (
    PackageRequirement,
    SecretRequirement,
    SecretSource,
    requirements_of,
)
from cloudfall.secret_store import SecretStore, secret_requirements
from cloudfall.secrets import (
    ELASTICSEARCH_FILES_VARIABLE,
    KIBANA_FILES_VARIABLE,
    SopsSecretProvider,
)
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from elasticsearch_hosts import FakeNodes
from kibana_project import SCHEMAS, kibana_service, search_main, write_project

ROOT = Path(__file__).parents[2]
CLOUDFALL_PASSWORD = "synthetic-cloudfall-value"  # noqa: S105 - grepped for
ELASTIC_PASSWORD = "synthetic-elastic-value"  # noqa: S105 - grepped for
ANALYST_PASSWORD = "synthetic-analyst-value"  # noqa: S105 - grepped for
KEYS = {
    "SECURITY_ENCRYPTION_KEY": "synthetic-security-key-0123456789abcdef",
    "SAVED_OBJECTS_ENCRYPTION_KEY": "synthetic-saved-objects-key-0123456789",
    "REPORTING_ENCRYPTION_KEY": "synthetic-reporting-key-0123456789abcd",
}
SEARCH_SECRETS = (
    f"CLOUDFALL_PASSWORD={CLOUDFALL_PASSWORD}\n"
    f"ELASTIC_PASSWORD={ELASTIC_PASSWORD}\n"
    f"ANALYST_PASSWORD={ANALYST_PASSWORD}\n"
)
KIBANA_SECRETS = "".join(f"{key}={value}\n" for key, value in KEYS.items())


def _project(tmp_path: Path, kibana_secrets: str | None = KIBANA_SECRETS) -> Path:
    project = write_project(tmp_path / "project", search_main(), kibana_service())
    services = project / "secrets" / "production" / "services"
    services.mkdir(parents=True)
    (services / "search-main.env").write_text(SEARCH_SECRETS, encoding="utf-8")
    if kibana_secrets is not None:
        (services / "search-kibana.env").write_text(kibana_secrets, encoding="utf-8")
    return project


@dataclass(frozen=True)
class Finished:
    """A step that exited 0 and printed nothing."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class Recorder:
    """Steps a converge started, and every staged Kibana file's content and mode."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.steps: list[ExecutionStep] = []
        self.extra: dict[str, object] = {}
        self.staged: dict[str, tuple[Path, str, int]] = {}
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
                "dict[str, dict[str, str]]", extra.get(KIBANA_FILES_VARIABLE, {})
            )
            for service, staged in files.items():
                for name, raw in staged.items():
                    path = Path(raw)
                    self.staged[f"{service}.{name}"] = (
                        path,
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


def _inventory(project: Path) -> PlatformInventory:
    return PlatformInventory.from_state(validate_config(project, SCHEMAS))


# proves: S-024-3
def test_s024_3_the_server_needs_kibana_at_the_clusters_version_and_three_keys(
    tmp_path: Path,
) -> None:
    (server,) = requirements_of(_inventory(_project(tmp_path)))
    kibana = [
        item.spec
        for item in server.requirements
        if item.owner.resource_id.value == "search-kibana"
    ]

    (package,) = (spec for spec in kibana if isinstance(spec, PackageRequirement))
    # The version of search-main, from the same repository: no version of its own
    assert (package.package.value, package.version.value) == ("kibana", "9.2.4")
    secrets = [spec for spec in kibana if isinstance(spec, SecretRequirement)]
    assert [spec.name.value for spec in secrets] == [
        "kibana-security-encryption-key",
        "kibana-saved-objects-encryption-key",
        "kibana-reporting-encryption-key",
    ]
    for spec in secrets:
        assert spec.source is SecretSource.GENERATE
        assert spec.path.value == "/etc/kibana/kibana.keystore"
        assert spec.reference.environment.value == "production"
        assert spec.reference.path.value == "/services/search-kibana"
    assert [spec.reference.key.value for spec in secrets] == list(KEYS)
    # No volume, port or certificate: Kibana keeps its state in the cluster
    assert len(kibana) == 4


# proves: S-024-5
def test_s024_5_each_key_is_a_generated_secret_of_the_store(tmp_path: Path) -> None:
    """``secrets generate`` makes the three keys, random and long enough for Kibana."""
    project = _project(tmp_path, kibana_secrets=None)
    store = SecretStore(
        project / "secrets",
        decrypt=lambda path: path.read_text(encoding="utf-8"),
        encrypt=lambda _path, text: text,
    )
    wanted = [
        spec
        for spec in secret_requirements(requirements_of(_inventory(project)))
        if spec.reference.path.value == "/services/search-kibana"
    ]

    first = [store.generate(spec.reference) for spec in wanted]
    values = [store.value(spec.reference) for spec in wanted]

    assert [stored.reference.key.value for stored in first] == list(KEYS)
    assert all(len(value) >= 32 for value in values)  # Kibana's minimum
    assert len(set(values)) == len(KEYS)
    assert all(store.holds(spec.reference) for spec in wanted)


# proves: S-024-5
def test_s024_5_the_keys_are_staged_0600_and_reach_no_output(tmp_path: Path) -> None:
    project = _project(tmp_path)

    recorder = Recorder()
    converge_services(_context(project, recorder), _provider(project))

    files = cast("dict[str, dict[str, str]]", recorder.extra[KIBANA_FILES_VARIABLE])
    assert set(files) == {"search-kibana"}
    staged = {
        name: recorder.staged[f"search-kibana.{name}"]
        for name in ("securityKey", "savedObjectsKey", "reportingKey")
    }
    assert {name: content for name, (_, content, _) in staged.items()} == {
        "securityKey": KEYS["SECURITY_ENCRYPTION_KEY"],
        "savedObjectsKey": KEYS["SAVED_OBJECTS_ENCRYPTION_KEY"],
        "reportingKey": KEYS["REPORTING_ENCRYPTION_KEY"],
    }
    assert {mode for _, _, mode in staged.values()} == {0o600}
    # Removed after the run, and never inside the project
    for path, _, _ in staged.values():
        assert not path.exists()
        assert project not in path.parents
    # The role reads Kibana's status with the cluster's cloudfall password
    accounts = cast(
        "dict[str, dict[str, object]]", recorder.extra[ELASTICSEARCH_FILES_VARIABLE]
    )
    assert "cloudfallPassword" in accounts["search-main"]
    inventory = json.dumps(render_ansible_inventory(_inventory(project)))
    for secret in (
        *KEYS.values(),
        CLOUDFALL_PASSWORD,
        ELASTIC_PASSWORD,
        ANALYST_PASSWORD,
    ):
        assert secret not in inventory
        for step in recorder.steps:
            assert secret not in " ".join(step.argv)
            assert secret not in json.dumps(dict(step.environment))


# proves: S-024-5
@pytest.mark.parametrize("key", list(KibanaEncryptionKey))
def test_s024_5_a_missing_key_stops_before_any_playbook(
    tmp_path: Path, key: KibanaEncryptionKey
) -> None:
    present = {name: value for name, value in KEYS.items() if name != key.store_key}
    project = _project(
        tmp_path, "".join(f"{name}={value}\n" for name, value in present.items())
    )
    recorder = Recorder()

    with pytest.raises(LifecycleError) as error:
        converge_services(_context(project, recorder), _provider(project))

    # The converge command answers a LifecycleError with PRECONDITION
    assert error.value.code == ERROR_SERVICES_SECRET
    assert f"{key.store_key} is missing" in error.value.detail
    assert f"{key.value} of Kibana service search-kibana" in error.value.detail
    assert "cloudfall secrets generate" in error.value.detail
    for value in KEYS.values():
        assert value not in error.value.detail
    assert recorder.steps == []


def test_a_project_without_kibana_stages_nothing_for_it(tmp_path: Path) -> None:
    project = write_project(tmp_path / "project", search_main())
    services = project / "secrets" / "production" / "services"
    services.mkdir(parents=True)
    (services / "search-main.env").write_text(SEARCH_SECRETS, encoding="utf-8")

    recorder = Recorder()
    converge_services(_context(project, recorder), _provider(project))

    assert KIBANA_FILES_VARIABLE not in recorder.extra
    (server,) = requirements_of(_inventory(project))
    assert all(
        not item.spec.name.value.startswith("kibana") for item in server.requirements
    )
