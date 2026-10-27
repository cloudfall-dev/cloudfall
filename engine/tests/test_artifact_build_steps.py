"""Component build steps in ``artifact build`` (S-002).

Every build runs ``cloudfall-engine artifact build`` as a child process with
its own environment: ``PATH`` holds only a temporary directory with a ``git``
that logs its argv before running the real one, ``env``, and stand-ins for
``node`` and ``pnpm``, so the tests need neither installed. The deploy test
runs the deploy and rollback role's own unpack and switch tasks under
``ansible-playbook`` against a directory on the controller.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from cloudfall.validation import SchemaCatalog
from cloudfall_engine.cli import main

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"
ROLE = ENGINE / "ansible" / "roles" / "cloudfall_deploy"
_GIT = shutil.which("git") or "git"
_ENV = shutil.which("env") or "/usr/bin/env"
ARTIFACT_BUILD_FAILED = 81
SECRET = "hcloud-secret-token-value"  # noqa: S105 - a planted test value.
INSTALL = ["pnpm", "install", "--frozen-lockfile"]
BUILD = ["pnpm", "build"]

GIT_WRAPPER = """#!{python}
import os, sys
with open({log!r}, "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
os.execv({git!r}, [{git!r}, *sys.argv[1:]])
"""

NODE = """#!{python}
import sys
if sys.argv[1:] == ["--version"]:
    print({version!r})
    sys.exit(0)
sys.exit(64)
"""

PNPM = """#!{python}
import json, os, pathlib, sys
arguments = sys.argv[1:]
if arguments == ["--version"]:
    print({version!r})
    sys.exit(0)
if arguments[0] == "install":
    print("installing packages to stdout")
    modules = pathlib.Path("node_modules/left-pad")
    modules.mkdir(parents=True)
    (modules / "index.js").write_text("module.exports = 1\\n")
elif arguments[0] == "build":
    print("building to stdout")
    dist = pathlib.Path("dist")
    dist.mkdir(exist_ok=True)
    (dist / "index.html").write_text(pathlib.Path("src/index.html").read_text())
    (dist / "env.json").write_text(json.dumps(dict(os.environ)))
elif arguments[0] == "fail":
    print("compiling to stdout", flush=True)
    print("error TS2322: boom", file=sys.stderr)
    sys.exit(3)
elif arguments[0] == "sleep":
    import time
    time.sleep(60)
elif arguments[0] == "noop":
    print("nothing to do")
else:
    sys.exit(64)
"""


def _git(*arguments: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixture repository setup.
        [
            _GIT,
            "-c",
            "user.email=test@example.test",
            "-c",
            "user.name=Cloudfall Test",
            *arguments,
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@dataclass(frozen=True, slots=True)
class _Fixture:
    """A component repository, its project, and the controller ``PATH``."""

    root: Path

    @property
    def repository(self) -> Path:
        return self.root / "repository"

    @property
    def project(self) -> Path:
        return self.root / "config"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def git_log(self) -> Path:
        return self.root / "git.log"

    def commit(self, page: str) -> str:
        (self.repository / "web" / "src" / "index.html").write_text(
            page, encoding="utf-8"
        )
        _git("-C", str(self.repository), "add", ".")
        _git("-C", str(self.repository), "commit", "--quiet", "--message", page)
        return _git("-C", str(self.repository), "rev-parse", "HEAD").strip()

    def tracked(self, commit: str) -> list[str]:
        listed = _git(
            "-C", str(self.repository), "ls-tree", "-r", "--name-only", commit
        )
        return listed.splitlines()

    def build(self, ref: str) -> tuple[int, dict[str, Any], str]:
        """Run ``artifact build``; answer its exit code, envelope, and stderr."""
        completed = subprocess.run(  # noqa: S603 - the CLI under test.
            [
                sys.executable,
                "-m",
                "cloudfall_engine",
                "artifact",
                "build",
                "crm-backend",
                "--ref",
                ref,
                "--project",
                str(self.project),
                "--schemas",
                str(SCHEMAS),
                "--output-dir",
                str(self.artifacts),
                "--format",
                "json",
            ],
            env={
                "PATH": str(self.bin),
                "HOME": str(self.root / "home"),
                "LANG": "C.UTF-8",
                "HCLOUD_TOKEN": SECRET,
                "SOPS_AGE_KEY": SECRET,
            },
            capture_output=True,
            check=False,
            text=True,
        )
        envelope = cast("dict[str, Any]", json.loads(completed.stdout))
        return completed.returncode, envelope, completed.stderr

    def built(self, ref: str) -> dict[str, Any]:
        exit_code, envelope, _ = self.build(ref)
        assert exit_code == 0, envelope["error"]
        return cast("dict[str, Any]", envelope["data"])

    def git_runs(self) -> set[str]:
        """Every git subcommand the logging wrapper saw."""
        if not self.git_log.exists():
            return set()
        lines = self.git_log.read_text(encoding="utf-8").splitlines()
        return {word for line in lines for word in line.split()}

    def written(self) -> list[Path]:
        if not self.artifacts.exists():
            return []
        return [
            path
            for path in self.artifacts.rglob("*")
            if path.name.endswith((".tar.gz", ".json"))
        ]


def _fixture(  # noqa: PLR0913 - one knob per criterion.
    tmp_path: Path,
    *,
    steps: list[list[str]] | None = None,
    include: list[str] | None = None,
    build: bool = True,
    node: str | None = "v22.3.0",
    pnpm: str = "9.1.0",
) -> _Fixture:
    fixture = _Fixture(tmp_path)
    repository = fixture.repository
    (repository / "web" / "src").mkdir(parents=True)
    _git("-C", str(repository), "-c", "init.defaultBranch=main", "init", "--quiet")
    (repository / "app.py").write_text("print('crm')\n", encoding="utf-8")
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "crm-backend"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    (repository / ".gitignore").write_text(
        "web/dist/\nweb/node_modules/\n", encoding="utf-8"
    )
    (repository / "web" / "package.json").write_text("{}\n", encoding="utf-8")
    fixture.commit("<h1>v1</h1>\n")

    shutil.copytree(EXAMPLES, fixture.project)
    path = fixture.project / "components" / "crm-backend.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec = document["spec"]
    spec["repository"]["url"] = f"file://{repository}"
    if build:
        spec["buildTools"] = {"node": "22.0.0", "pnpm": "9.0.0"}
        spec["build"] = [
            {"workingDirectory": "web", "command": command}
            for command in (steps if steps is not None else [INSTALL, BUILD])
        ]
        for step in spec["build"]:
            if step["command"] == ["pnpm", "sleep"]:
                step["timeoutSeconds"] = 1
        spec["artifact"] = {"include": include or ["web/dist"]}
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    fixture.bin.mkdir()
    (tmp_path / "home").mkdir()
    python = sys.executable
    _executable(
        fixture.bin / "git",
        GIT_WRAPPER.format(python=python, log=str(fixture.git_log), git=_GIT),
    )
    (fixture.bin / "env").symlink_to(_ENV)
    if node is not None:
        _executable(fixture.bin / "node", NODE.format(python=python, version=node))
    _executable(fixture.bin / "pnpm", PNPM.format(python=python, version=pnpm))
    return fixture


def _members(archive: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(archive) as opened:
        return {member.name: member for member in opened.getmembers()}


def _read(archive: Path, name: str) -> str:
    with tarfile.open(archive) as opened:
        extracted = opened.extractfile(name)
        assert extracted is not None
        return extracted.read().decode("utf-8")


def _failure(envelope: dict[str, Any]) -> dict[str, Any]:
    error = cast("dict[str, Any]", envelope["error"])
    return cast("dict[str, Any]", error["context"])


def test_s002_2_the_tarball_holds_the_gitignored_build_output(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    commit = _git("-C", str(fixture.repository), "rev-parse", "HEAD").strip()

    built = fixture.built(commit)

    archive = Path(built["archive"])
    assert built["git_commit"] == commit
    assert "./web/dist/index.html" in _members(archive)
    assert _read(archive, "./web/dist/index.html") == "<h1>v1</h1>\n"


def test_s002_3_the_tarball_holds_every_tracked_file_and_no_node_modules(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    commit = _git("-C", str(fixture.repository), "rev-parse", "HEAD").strip()

    names = set(_members(Path(fixture.built(commit)["archive"])))

    assert {f"./{path}" for path in fixture.tracked(commit)} <= names
    assert not [name for name in names if "node_modules" in Path(name).parts]
    assert all(".git" not in Path(name).parts for name in names)


def test_s002_4_the_metadata_records_the_steps_and_tool_versions(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    metadata_path = Path(fixture.built("main")["metadata"])

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["spec"]["build"] == {
        "steps": [
            {"workingDirectory": "web", "command": INSTALL},
            {"workingDirectory": "web", "command": BUILD},
        ],
        "tools": {"node": "22.3.0", "pnpm": "9.1.0"},
    }
    SchemaCatalog(SCHEMAS).validate_named("artifact.schema.json", metadata)


def test_s002_5_a_missing_tool_fails_before_any_clone(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, node=None)

    exit_code, envelope, _ = fixture.build("main")

    assert exit_code == ARTIFACT_BUILD_FAILED
    context = _failure(envelope)
    assert context["code"] == "artifact_build_tool_missing"
    assert context["tool"] == "node"
    assert "node" in envelope["error"]["message"]
    assert "clone" not in fixture.git_runs()
    assert fixture.written() == []


def test_s002_6_an_old_tool_names_the_tool_and_both_versions(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, node="v20.11.1")

    exit_code, envelope, _ = fixture.build("main")

    assert exit_code == ARTIFACT_BUILD_FAILED
    context = _failure(envelope)
    assert context["code"] == "artifact_build_tool_unsupported"
    assert context["tool"] == "node"
    assert context["found"] == "20.11.1"
    assert context["minimum"] == "22.0.0"
    message = envelope["error"]["message"]
    assert "node" in message
    assert "20.11.1" in message
    assert "22.0.0" in message


def test_s002_6_a_tool_that_prints_no_version_is_unsupported(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, pnpm="unknown")

    exit_code, envelope, _ = fixture.build("main")

    assert exit_code == ARTIFACT_BUILD_FAILED
    assert _failure(envelope)["code"] == "artifact_build_tool_unsupported"
    assert _failure(envelope)["tool"] == "pnpm"


def test_s002_7_a_failing_step_names_itself_and_writes_nothing(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, steps=[INSTALL, ["pnpm", "fail"], BUILD])

    exit_code, envelope, stderr = fixture.build("main")

    assert exit_code == ARTIFACT_BUILD_FAILED
    context = _failure(envelope)
    assert context["code"] == "artifact_build_step_failed"
    assert context["step"] == 1
    assert context["argv"] == ["pnpm", "fail"]
    assert context["exitCode"] == 3
    message = envelope["error"]["message"]
    assert "step 1 (pnpm fail) exited with code 3" in message
    assert "error TS2322: boom" in message
    assert "error TS2322: boom" in stderr
    assert fixture.written() == []


def test_s002_7_a_step_past_its_timeout_fails_the_build(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, steps=[["pnpm", "sleep"], BUILD])

    exit_code, envelope, _ = fixture.build("main")

    assert exit_code == ARTIFACT_BUILD_FAILED
    context = _failure(envelope)
    assert context["code"] == "artifact_build_step_failed"
    assert context["step"] == 0
    assert context["timeoutSeconds"] == 1
    assert "1s timeout" in envelope["error"]["message"]
    assert fixture.written() == []


def test_s002_8_a_missing_include_path_writes_no_artifact(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, steps=[INSTALL, ["pnpm", "noop"]])

    exit_code, envelope, _ = fixture.build("main")

    assert exit_code == ARTIFACT_BUILD_FAILED
    context = _failure(envelope)
    assert context["code"] == "artifact_include_missing"
    assert context["path"] == "web/dist"
    assert fixture.written() == []


def test_s002_9_step_stdout_stays_out_of_the_json_envelope(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    exit_code, envelope, stderr = fixture.build("main")

    assert exit_code == 0, envelope["error"]
    assert envelope["ok"] is True
    assert "installing packages to stdout" in stderr
    assert "building to stdout" in stderr


def test_s002_9_stdout_is_exactly_one_envelope(tmp_path: Path) -> None:
    """``json.loads`` of the whole stdout fails on any stray line."""
    fixture = _fixture(tmp_path, steps=[INSTALL, ["pnpm", "fail"]])

    exit_code, envelope, _ = fixture.build("main")

    assert exit_code == ARTIFACT_BUILD_FAILED
    assert envelope["ok"] is False


def test_s002_10_a_step_sees_ci_and_none_of_the_controller_secrets(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    archive = Path(fixture.built("main")["archive"])

    environment = json.loads(_read(archive, "./web/dist/env.json"))
    assert environment["CI"] == "true"
    # macOS sets __CF_USER_TEXT_ENCODING in every new process itself.
    allowed = {"PATH", "HOME", "LANG", "TMPDIR", "CI", "__CF_USER_TEXT_ENCODING"}
    assert set(environment) <= allowed
    assert "HCLOUD_TOKEN" not in environment
    assert "SOPS_AGE_KEY" not in environment
    assert SECRET not in json.dumps(environment)
    assert environment["PATH"] == str(fixture.bin)
    assert environment["HOME"] == str(tmp_path / "home")
    assert environment["LANG"] == "C.UTF-8"


def test_s002_13_a_component_without_build_packages_the_clean_tree(
    tmp_path: Path,
) -> None:
    """Without ``build`` the archive is the clean checkout as before.

    Every tracked file and directory, nothing else, and no ``spec.build``.
    """
    fixture = _fixture(tmp_path, build=False)
    commit = _git("-C", str(fixture.repository), "rev-parse", "HEAD").strip()

    built = fixture.built("main")

    tracked = [Path(path) for path in fixture.tracked(commit)]
    expected = {"."}
    for path in tracked:
        expected.add(f"./{path.as_posix()}")
        expected.update(
            f"./{parent.as_posix()}" for parent in path.parents if parent != Path()
        )
    assert set(_members(Path(built["archive"]))) == expected
    metadata = json.loads(Path(built["metadata"]).read_text(encoding="utf-8"))
    assert "build" not in metadata["spec"]
    SchemaCatalog(SCHEMAS).validate_named("artifact.schema.json", metadata)
    runs = fixture.git_runs()
    assert "clone" in runs
    assert "ls-files" not in runs


def test_s002_13_the_built_archive_lists_entries_like_the_plain_one(
    tmp_path: Path,
) -> None:
    """A build adds its include paths; every other entry is the plain tree's."""
    fixture = _fixture(tmp_path)
    commit = _git("-C", str(fixture.repository), "rev-parse", "HEAD").strip()

    members = _members(Path(fixture.built(commit)["archive"]))

    tracked = {f"./{path}" for path in fixture.tracked(commit)}
    built_output = {
        "./web/dist",
        "./web/dist/index.html",
        "./web/dist/env.json",
    }
    directories = {".", "./web", "./web/src"}
    assert set(members) == tracked | built_output | directories
    assert {member.uname for member in members.values()} == {"root"}


# Deploy and rollback of a built release (S-002-12).

DEPLOY_TASKS = (
    "Compute release paths",
    "Create release and shared directories",
    "Verify the transferred artifact digest",
    "Refuse a release whose digest does not match its metadata",
    "Unpack the verified release",
    "Inspect the currently active release",
    "Record the rollback candidate",
    "Switch the current release symlink",
)
ROLLBACK_TASKS = (
    "Compute rollback paths",
    "Inspect the rollback target release",
    "Require the rollback target on the server",
    "Switch the current release symlink to the rollback target",
)


def _gnu_tar() -> bool:
    tar = shutil.which("gtar") or shutil.which("tar")
    if tar is None:
        return False
    completed = subprocess.run(  # noqa: S603 - probing the controller's tar.
        [tar, "--version"], capture_output=True, check=False, text=True
    )
    return "GNU tar" in completed.stdout


def _role_tasks(file: str, names: tuple[str, ...]) -> list[dict[str, object]]:
    """Load the role's own tasks by name, with owner and group dropped.

    The role chowns to the application user as root; the test runs as the
    controller's user, so the tasks keep their paths, digest check, unpack,
    and symlink switch, and lose only the ownership.
    """
    loaded = DataLoader().load_from_file(str(ROLE / "tasks" / file))
    assert isinstance(loaded, list)
    flat: list[dict[str, object]] = []
    for task in loaded:
        flat.append(task)
        block = task.get("block")
        if isinstance(block, list):
            flat.extend(block)
    tasks: list[dict[str, object]] = []
    for name in names:
        # A JSON round trip drops Ansible's tagged types, which YAML can't dump.
        task = json.loads(json.dumps(next(t for t in flat if t["name"] == name)))
        for module in task.values():
            if isinstance(module, dict):
                module.pop("owner", None)
                module.pop("group", None)
        tasks.append(task)
    return tasks


def _play(
    tmp_path: Path,
    name: str,
    variables: dict[str, object],
    tasks: list[dict[str, object]],
) -> int:
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "all": {
                    "hosts": {
                        "localhost": {
                            "ansible_connection": "local",
                            "ansible_python_interpreter": sys.executable,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    playbook = tmp_path / f"{name}.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "name": name,
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": variables,
                    "tasks": tasks,
                }
            ]
        ),
        encoding="utf-8",
    )
    return main(
        [
            "playbook",
            "run",
            str(playbook),
            "--engine",
            str(ENGINE),
            "--inventory",
            str(inventory),
        ]
    )


def _deploy(tmp_path: Path, install_root: Path, built: dict[str, Any]) -> int:
    archive = Path(built["archive"])
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == built["archive_sha256"]
    staged: dict[str, object] = {
        "name": "Stage the artifact where the transfer would",
        "ansible.builtin.set_fact": {"cloudfall_deploy_staging_archive": str(archive)},
    }
    tasks = _role_tasks("main.yml", DEPLOY_TASKS)
    return _play(
        tmp_path,
        f"deploy-{built['release']}",
        {
            "cloudfall_deploy_component": {
                "id": "crm-backend",
                "install_root": str(install_root),
                "application_user": "crm",
            },
            "cloudfall_deploy_release": built["release"],
            "cloudfall_deploy_artifact_sha256": built["archive_sha256"],
        },
        [tasks[0], staged, *tasks[1:]],
    )


def _rollback(tmp_path: Path, install_root: Path, release: str) -> int:
    return _play(
        tmp_path,
        f"rollback-{release}",
        {
            "cloudfall_deploy_component": {
                "id": "crm-backend",
                "install_root": str(install_root),
            },
            "cloudfall_deploy_release": release,
        },
        _role_tasks("rollback.yml", ROLLBACK_TASKS),
    )


@pytest.mark.skipif(not _gnu_tar(), reason="Ansible's unarchive needs GNU tar")
def test_s002_12_deploy_and_rollback_carry_the_built_files(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    first = fixture.built(fixture.commit("<h1>first</h1>\n"))
    second = fixture.built(fixture.commit("<h1>second</h1>\n"))
    install_root = tmp_path / "srv" / "crm-backend"
    current = install_root / "current" / "web" / "dist" / "index.html"

    assert _deploy(tmp_path, install_root, first) == 0
    assert _deploy(tmp_path, install_root, second) == 0

    for built, page in ((first, "first"), (second, "second")):
        index = install_root / "releases" / built["release"] / "web" / "dist"
        assert (index / "index.html").read_text(encoding="utf-8") == (
            f"<h1>{page}</h1>\n"
        )
    assert current.read_text(encoding="utf-8") == "<h1>second</h1>\n"

    assert _rollback(tmp_path, install_root, first["release"]) == 0

    assert (install_root / "current").readlink() == (
        install_root / "releases" / first["release"]
    )
    assert current.read_text(encoding="utf-8") == "<h1>first</h1>\n"
