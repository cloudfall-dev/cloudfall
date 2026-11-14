"""What the controller prepares to converge an Elasticsearch service (S-012, #310).

``services.yml`` needs a Debian 13 host, so these tests run the path that
starts it with a recording step runner: it reads the files staged for the
service's ``cloudfall`` and ``elastic`` accounts the way the role's tasks do,
and every output is grepped for the passwords.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import bcrypt
import pytest
from cloudfall.inventory import PlatformInventory
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
from cloudfall.secrets import (
    ELASTICSEARCH_FILES_VARIABLE,
    PASSWORD_FILES_VARIABLE,
    SopsSecretProvider,
    elasticsearch_bcrypt_hash,
)
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from elasticsearch_project import SCHEMAS, elasticsearch_service, write_project

ROOT = Path(__file__).parents[2]
ENGINE = ROOT / "engine"
CLOUDFALL = Path(sys.executable).with_name("cloudfall")
CLOUDFALL_PASSWORD = "synthetic-cloudfall-value"  # noqa: S105 - grepped for
ELASTIC_PASSWORD = "synthetic-elastic-value"  # noqa: S105 - grepped for


def _project(tmp_path: Path) -> Path:
    """Write the S-012-1 project: one server, its baseline, no declared users."""
    return write_project(tmp_path / "project", elasticsearch_service())


def _secrets(project: Path, content: str) -> Path:
    secrets = project / "secrets"
    source = secrets / "production" / "services" / "search-main.env"
    source.parent.mkdir(parents=True)
    source.write_text(content, encoding="utf-8")
    return secrets


def _plaintext(source: Path) -> str:
    """Stand in for sops: the test's secret files are not encrypted."""
    return source.read_text(encoding="utf-8")


@dataclass(frozen=True)
class Finished:
    """A step that exited 0 and printed nothing."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Recorder:
    """Steps a converge started, and what the staged files held mid-run."""

    steps: list[ExecutionStep] = field(default_factory=list)
    extra: dict[str, object] = field(default_factory=dict)
    staged: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    paths: list[Path] = field(default_factory=list)

    def __call__(self, step: ExecutionStep) -> Finished:
        """Record the step and read every account file it was handed."""
        self.steps.append(step)
        if "--extra-vars" in step.argv:
            self.extra = json.loads(step.argv[step.argv.index("--extra-vars") + 1])
            services = cast(
                "dict[str, dict[str, str]]",
                self.extra.get(ELASTICSEARCH_FILES_VARIABLE, {}),
            )
            for service, files in services.items():
                for name, raw in files.items():
                    path = Path(raw)
                    self.paths.append(path)
                    self.staged[f"{service}/{name}"] = (
                        path.read_text(encoding="utf-8"),
                        stat.S_IMODE(path.stat().st_mode),
                        stat.S_IMODE(path.parent.stat().st_mode),
                    )
        return Finished()


def _context(project: Path, recorder: Recorder) -> EngineContext:
    return EngineContext(
        project_directory=project,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=project / "tmp" / "ansible-inventory.json",
        run=recorder,
    )


def _provider(secrets: Path) -> SopsSecretProvider:
    return SopsSecretProvider(secrets_directory=secrets, decrypt=_plaintext)


def _verifies(hashed: str, password: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# proves: S-012-3
def test_s012_3_converge_stages_the_cloudfall_password_and_hashes_only(
    tmp_path: Path,
) -> None:
    """Cloudfall authenticates as ``cloudfall``; ``elastic`` reaches the node as a hash.

    The node keeps the ``cloudfall`` password in its 0600 file, as the spec
    says, and the file realm only its bcrypt hash; ``elastic`` is staged as
    a hash alone (D-9).
    """
    project = _project(tmp_path)
    secrets = _secrets(
        project,
        f"CLOUDFALL_PASSWORD={CLOUDFALL_PASSWORD}\nELASTIC_PASSWORD={ELASTIC_PASSWORD}\n",
    )
    recorder = Recorder()

    passwords = converge_services(_context(project, recorder), _provider(secrets))

    assert passwords == ()
    render, playbook = recorder.steps
    assert playbook.argv[-1].endswith("services.yml")
    assert PASSWORD_FILES_VARIABLE not in recorder.extra
    assert set(recorder.staged) == {
        "search-main/cloudfallPassword",
        "search-main/cloudfallHash",
        "search-main/elasticHash",
    }
    plain, plain_mode, directory_mode = recorder.staged["search-main/cloudfallPassword"]
    assert (plain, plain_mode, directory_mode) == (CLOUDFALL_PASSWORD, 0o600, 0o700)
    cloudfall_hash, cloudfall_mode, _ = recorder.staged["search-main/cloudfallHash"]
    elastic_hash, elastic_mode, _ = recorder.staged["search-main/elasticHash"]
    assert cloudfall_mode == elastic_mode == 0o600
    assert cloudfall_hash.startswith("$2a$10$")
    assert elastic_hash.startswith("$2a$10$")
    assert _verifies(cloudfall_hash, CLOUDFALL_PASSWORD)
    assert _verifies(elastic_hash, ELASTIC_PASSWORD)
    assert ELASTIC_PASSWORD not in "".join(
        content for content, _, _ in recorder.staged.values()
    )
    assert CLOUDFALL_PASSWORD not in cloudfall_hash + elastic_hash
    assert all(not path.exists() for path in recorder.paths)
    assert all(project not in path.parents for path in recorder.paths)
    for step in (render, playbook):
        for secret in (CLOUDFALL_PASSWORD, ELASTIC_PASSWORD):
            assert secret not in " ".join(step.argv)
            assert secret not in json.dumps(dict(step.environment))


def test_the_hash_is_elasticsearch_bcrypt_of_the_password() -> None:
    hashed = elasticsearch_bcrypt_hash(CLOUDFALL_PASSWORD)

    assert hashed.startswith("$2a$10$")
    assert _verifies(hashed, CLOUDFALL_PASSWORD)
    assert not _verifies(hashed, "another-value")
    assert CLOUDFALL_PASSWORD not in hashed


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, "production/services/search-main.env"),
        (f"CLOUDFALL_PASSWORD={CLOUDFALL_PASSWORD}\n", "ELASTIC_PASSWORD is missing"),
    ],
    ids=["missing-file", "missing-elastic-key"],
)
def test_a_missing_account_password_stops_before_any_playbook(
    tmp_path: Path, content: str | None, expected: str
) -> None:
    project = _project(tmp_path)
    secrets = project / "secrets" if content is None else _secrets(project, content)
    recorder = Recorder()

    with pytest.raises(LifecycleError) as error:
        converge_services(_context(project, recorder), _provider(secrets))

    assert error.value.code == ERROR_SERVICES_SECRET
    assert expected in error.value.detail
    assert "`cloudfall secrets generate`" in error.value.detail
    assert recorder.steps == []


def test_each_node_needs_the_package_and_the_generated_cloudfall_password(
    tmp_path: Path,
) -> None:
    inventory = PlatformInventory.from_state(
        validate_config(_project(tmp_path), SCHEMAS)
    )

    (server,) = requirements_of(inventory)

    assert server.server_id.value == "h1"
    by_type = {type(item.spec): item for item in server.requirements}
    password = by_type[SecretRequirement]
    package = by_type[PackageRequirement]
    assert password.owner.as_dict() == {"kind": "Service", "id": "search-main"}
    assert password.spec.as_dict() == {
        "source": SecretSource.GENERATE.value,
        "path": "/etc/elasticsearch/cloudfall-password",
        "reference": {
            "environment": "production",
            "path": "/services/search-main",
            "key": "CLOUDFALL_PASSWORD",
        },
    }
    assert package.spec.as_dict() == {"package": "elasticsearch", "version": "9.2.4"}
    rendered = json.dumps(render_ansible_inventory(inventory))
    assert "cloudfall_certificates" not in rendered
    assert "cloudfall_volumes" not in rendered


FAKE_SOPS = f"""#!{sys.executable}
'''Stand in for sops: "encrypts" by appending sops metadata, values stay in clear.'''
import sys

args = sys.argv[1:]
if args[0] == "--decrypt":
    with open(args[1], encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    kept = [line for line in lines if not line.startswith("sops_")]
    sys.stdout.write("".join(f"{{line}}\\n" for line in kept))
elif args[0] == "--encrypt":
    sys.stdout.write(sys.stdin.read() + "sops_mac=fake\\nsops_version=3.13.3\\n")
else:
    sys.exit(f"unexpected sops call: {{args}}")
"""


def _cloudfall(project: Path, tools: Path, *argv: str) -> dict[str, object]:
    done = subprocess.run(  # noqa: S603 - the installed CLI, fixed arguments
        [str(CLOUDFALL), *argv, "--project", str(project)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"},
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    data = json.loads(done.stdout)["data"]
    assert isinstance(data, dict)
    return data


def test_secrets_generate_makes_both_account_passwords_once(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / ".sops.yaml").write_text(
        "creation_rules:\n  - path_regex: secrets/.*\\.env$\n    age: age1example\n",
        encoding="utf-8",
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    sops = tools / "sops"
    sops.write_text(FAKE_SOPS, encoding="utf-8")
    sops.chmod(0o755)

    generated = _cloudfall(project, tools, "secrets", "generate")
    again = _cloudfall(project, tools, "secrets", "generate")

    written = cast("list[dict[str, object]]", generated["written"])
    assert [(item["name"], item["key"]) for item in written] == [
        ("elasticsearch-cloudfall-password", "CLOUDFALL_PASSWORD"),
        ("elasticsearch-elastic-password", "ELASTIC_PASSWORD"),
    ]
    assert again["written"] == []
    stored = (project / "secrets" / "production" / "services" / "search-main.env")
    keys = [
        line.split("=", 1)[0]
        for line in stored.read_text(encoding="utf-8").splitlines()
        if not line.startswith("sops_")
    ]
    assert keys == ["CLOUDFALL_PASSWORD", "ELASTIC_PASSWORD"]
