"""What the controller stages for declared Elasticsearch users (S-012, #311).

``services.yml`` needs a Debian 13 host, so the converge runs with a
recording step runner that reads every staged file mid-run; each output is
grepped for the passwords.
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import bcrypt
import pytest
from cloudfall.domain import ResourceId
from cloudfall.elasticsearch import ElasticsearchUserName
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import (
    ERROR_SERVICES_SECRET,
    EngineContext,
    ExecutionStep,
    LifecycleError,
    converge_services,
)
from cloudfall.secrets import (
    ELASTICSEARCH_FILES_VARIABLE,
    SopsSecretProvider,
    elasticsearch_user_bcrypt_hash,
)
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from elasticsearch_project import (
    PASSWORD_REF,
    SCHEMAS,
    elasticsearch_service,
    write_project,
)

ROOT = Path(__file__).parents[2]
CLOUDFALL_PASSWORD = "synthetic-cloudfall-value"  # noqa: S105 - grepped for
ELASTIC_PASSWORD = "synthetic-elastic-value"  # noqa: S105 - grepped for
CRM_PASSWORD = "synthetic-crm-value"  # noqa: S105 - grepped for
CRM = {
    "name": "crm",
    "access": "read",
    "indexPatterns": ["crm-*"],
    "passwordRef": PASSWORD_REF,
}
SECRETS = (
    f"CLOUDFALL_PASSWORD={CLOUDFALL_PASSWORD}\n"
    f"ELASTIC_PASSWORD={ELASTIC_PASSWORD}\n"
    f"CRM_PASSWORD={CRM_PASSWORD}\n"
)


class Recorder:
    """Steps a converge started, and every staged file's content and mode."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.steps: list[ExecutionStep] = []
        self.files: dict[str, dict[str, object]] = {}
        self.staged: dict[Path, tuple[str, int]] = {}

    def __call__(self, step: ExecutionStep) -> Finished:
        """Record the step and read the files it was handed."""
        self.steps.append(step)
        if "--extra-vars" in step.argv:
            extra = json.loads(step.argv[step.argv.index("--extra-vars") + 1])
            self.files = extra[ELASTICSEARCH_FILES_VARIABLE]
            for files in self.files.values():
                users = cast("dict[str, str]", files.get("users", {}))
                others = [value for key, value in files.items() if key != "users"]
                for raw in (*others, *users.values()):
                    path = Path(cast("str", raw))
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


def _converge(tmp_path: Path, secrets: str) -> tuple[Path, Recorder]:
    project = write_project(tmp_path / "project", elasticsearch_service(users=[CRM]))
    source = project / "secrets" / "production" / "services" / "search-main.env"
    source.parent.mkdir(parents=True)
    source.write_text(secrets, encoding="utf-8")
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
    return project, recorder


# proves: S-012-8
def test_s012_8_the_staged_file_of_a_declared_user_holds_its_bcrypt_hash_only(
    tmp_path: Path,
) -> None:
    project, recorder = _converge(tmp_path, SECRETS)

    users = cast("dict[str, str]", recorder.files["search-main"]["users"])
    staged = Path(users["crm"])
    content, mode = recorder.staged[staged]
    assert mode == 0o600
    assert content.startswith("$2a$10$")
    assert bcrypt.checkpw(CRM_PASSWORD.encode(), content.encode())
    assert CRM_PASSWORD not in content
    # Removed after the run, and never inside the project.
    assert not staged.exists()
    assert project not in staged.parents
    # Nothing a step was handed carries a password: argv, environment, the
    # rendered inventory.
    inventory = json.dumps(
        render_ansible_inventory(
            PlatformInventory.from_state(validate_config(project, SCHEMAS))
        )
    )
    for secret in (CLOUDFALL_PASSWORD, ELASTIC_PASSWORD, CRM_PASSWORD):
        assert secret not in inventory
        for step in recorder.steps:
            assert secret not in " ".join(step.argv)
            assert secret not in json.dumps(dict(step.environment))
    assert CRM_PASSWORD not in "".join(text for text, _ in recorder.staged.values())


def test_a_user_hash_is_the_same_for_the_same_password_and_salt_key() -> None:
    def hashed(password: str, user: str = "crm", key: str = CLOUDFALL_PASSWORD) -> str:
        return elasticsearch_user_bcrypt_hash(
            password,
            service=ResourceId("search-main"),
            user=ElasticsearchUserName(user),
            salt_key=key,
        )

    first = hashed(CRM_PASSWORD)

    assert first == hashed(CRM_PASSWORD)
    assert first.startswith("$2a$10$")
    assert bcrypt.checkpw(CRM_PASSWORD.encode(), first.encode())
    assert first != hashed("synthetic-crm-rotated")
    # Another user, or another service key, salts differently.
    assert first[:29] != hashed(CRM_PASSWORD, user="billing")[:29]
    assert first[:29] != hashed(CRM_PASSWORD, key="another-key")[:29]
    with pytest.raises(ValueError, match="non-empty salt key"):
        hashed(CRM_PASSWORD, key="")


def test_a_missing_user_password_stops_before_any_playbook(tmp_path: Path) -> None:
    secrets = (
        f"CLOUDFALL_PASSWORD={CLOUDFALL_PASSWORD}\n"
        f"ELASTIC_PASSWORD={ELASTIC_PASSWORD}\n"
    )

    with pytest.raises(LifecycleError) as error:
        _converge(tmp_path, secrets)

    assert error.value.code == ERROR_SERVICES_SECRET
    assert "CRM_PASSWORD is missing" in error.value.detail
    assert "user crm of Elasticsearch service search-main" in error.value.detail
