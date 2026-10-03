"""Project scaffolding acceptance tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from importlib import metadata
from pathlib import Path

import pytest
from cloudfall.cli import main
from cloudfall.commands import COMMANDS, CommandEffect
from cloudfall.domain import ResourceKind
from cloudfall.project import (
    GitSetup,
    InitOptions,
    ProjectDescription,
    ProjectError,
    ProjectName,
    ReleaseVersion,
    init_project,
    project_context,
    resolve_installed_version,
    resolve_project_directory,
)
from cloudfall.validation import ConfigValidationError, validate_config

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
VERSION = "0.3.0"


def _options(
    directory: Path, description: ProjectDescription | None = None
) -> InitOptions:
    return InitOptions(
        directory=directory,
        name=ProjectName.from_directory(directory),
        version=ReleaseVersion(VERSION),
        description=description,
    )


def _git(directory: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed binary, test directory.
        ["git", "-C", str(directory), *arguments],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_init_lays_out_every_resource_directory_and_pins_cloudfall(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "Acme Fleet"

    scaffold = init_project(_options(directory))

    assert str(scaffold.name) == "acme-fleet"
    for kind in ResourceKind:
        assert (directory / kind.directory / ".gitkeep").is_file()
    pyproject = (directory / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "acme-fleet"' in pyproject
    assert f'dependencies = ["cloudfall=={VERSION}"]' in pyproject
    assert "tmp/" in (directory / ".gitignore").read_text(encoding="utf-8")
    readme = (directory / "README.md").read_text(encoding="utf-8")
    assert "# acme-fleet" in readme
    assert "## About" in readme
    assert "--description" in readme
    assert "pyproject.toml" in scaffold.files
    assert "servers/.gitkeep" in scaffold.files


def test_init_links_the_guide_and_examples_at_the_pinned_release(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "fleet"

    init_project(_options(directory))

    readme = (directory / "README.md").read_text(encoding="utf-8")
    base = "https://github.com/romamo/cloudfall"
    assert "[secrets guide][secrets-guide]" in readme
    assert "[reference set][examples]" in readme
    assert f"[cloudfall]: {base}\n" in readme
    assert f"[secrets-guide]: {base}/blob/v{VERSION}/docs/secrets-guide.md\n" in readme
    assert f"[examples]: {base}/tree/v{VERSION}/config/examples\n" in readme


def test_init_pins_the_version_with_no_source_override(tmp_path: Path) -> None:
    """A project resolves Cloudfall from the index like any dependency."""
    directory = tmp_path / "fleet"

    scaffold = init_project(_options(directory))

    pyproject = (directory / "pyproject.toml").read_text(encoding="utf-8")
    assert f'dependencies = ["cloudfall=={VERSION}"]' in pyproject
    assert "[tool.uv.sources]" not in pyproject
    assert "rev = " not in pyproject
    assert "git = " not in pyproject
    assert pyproject.endswith("package = false\n")
    assert str(scaffold.version) == VERSION



def test_init_lays_out_the_secrets_setup(tmp_path: Path) -> None:
    directory = tmp_path / "fleet"

    scaffold = init_project(_options(directory))

    assert (directory / "secrets" / ".gitkeep").is_file()
    sops = (directory / ".sops.yaml").read_text(encoding="utf-8")
    assert "path_regex: secrets/.*\\.env$" in sops
    assert "age: age1" in sops
    assert ".sops.yaml" in scaffold.files
    assert "secrets/.gitkeep" in scaffold.files
    assert "secrets" not in (directory / ".gitignore").read_text(encoding="utf-8")


def test_init_writes_the_description_into_readme_and_pyproject(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "fleet"
    description = ProjectDescription("Two Hetzner hosts running the CRM for Acme")

    init_project(_options(directory, description))

    readme = (directory / "README.md").read_text(encoding="utf-8")
    pyproject = (directory / "pyproject.toml").read_text(encoding="utf-8")
    assert f"## About\n\n{description}\n" in readme
    assert "--description" not in readme
    assert f'description = "{description}"' in pyproject


def test_project_description_is_one_printable_line() -> None:
    assert str(ProjectDescription.from_boundary("  Acme fleet ")) == "Acme fleet"
    for bad in ("", "two\nlines", 'say "hi"', "back\\slash", "x" * 513):
        with pytest.raises(ValueError, match="project description"):
            ProjectDescription(bad)


def test_init_creates_a_git_repository(tmp_path: Path) -> None:
    directory = tmp_path / "fleet"

    scaffold = init_project(_options(directory))

    assert scaffold.git is GitSetup.INITIALIZED
    assert (directory / ".git").is_dir()
    assert _git(directory, "rev-parse", "--show-toplevel") == str(directory.resolve())
    assert "fresh git repository" in (directory / "README.md").read_text(
        encoding="utf-8"
    )


def test_init_leaves_an_enclosing_git_repository_alone(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--quiet")
    directory = tmp_path / "fleet"

    scaffold = init_project(_options(directory))

    assert scaffold.git is GitSetup.ENCLOSED
    assert not (directory / ".git").exists()
    assert _git(directory, "rev-parse", "--show-toplevel") == str(tmp_path.resolve())
    assert "existing git repository" in (directory / "README.md").read_text(
        encoding="utf-8"
    )


def test_init_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("keep me\n", encoding="utf-8")

    with pytest.raises(ProjectError) as error:
        init_project(_options(tmp_path))

    assert error.value.code == "project_directory_not_empty"
    assert not (tmp_path / "pyproject.toml").exists()


def test_init_refuses_a_file_path(tmp_path: Path) -> None:
    target = tmp_path / "project"
    target.write_text("", encoding="utf-8")

    with pytest.raises(ProjectError) as error:
        init_project(_options(target))

    assert error.value.code == "project_directory_not_a_directory"


def test_project_name_is_strict() -> None:
    assert str(ProjectName.from_boundary(" My-Fleet ")) == "my-fleet"
    with pytest.raises(ValueError, match="project name"):
        ProjectName("-leading-dash")
    with pytest.raises(ValueError, match="project name"):
        ProjectName("has space")


def test_release_version_requires_a_release() -> None:
    assert str(ReleaseVersion.from_boundary(" 1.2.3 ")) == "1.2.3"
    ReleaseVersion("0.3.0rc1")
    ReleaseVersion("1.0")
    for unreleased in ("0.3.0+local.build", "0.3.0.dirty", "v0.3.0", ""):
        with pytest.raises(ValueError, match="release version"):
            ReleaseVersion(unreleased)


def test_installed_version_comes_from_the_distribution() -> None:
    assert resolve_installed_version(lambda: VERSION) == ReleaseVersion(VERSION)


def test_installed_version_refuses_a_version_no_project_can_pin() -> None:
    with pytest.raises(ProjectError) as error:
        resolve_installed_version(lambda: "0.3.0+local.build")

    assert error.value.code == "project_version_unresolved"
    assert "by hand" in error.value.detail


def test_cli_init_pins_the_version_it_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whatever ran `init` is what the project depends on."""
    directory = tmp_path / "fleet"
    installed = metadata.version("cloudfall")

    exit_code = main(["init", str(directory)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert (payload["data"]["status"], payload["data"]["effect"]) == ("ok", "created")
    assert payload["data"]["project"]["name"] == "fleet"
    assert payload["data"]["project"]["version"] == installed
    assert payload["data"]["project"]["git"] == "initialized"
    assert payload["data"]["next"][-1] == "uv run cloudfall config validate"
    pyproject = (directory / "pyproject.toml").read_text(encoding="utf-8")
    assert f'dependencies = ["cloudfall=={installed}"]' in pyproject


def test_cli_init_accepts_a_description(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "fleet"

    exit_code = main(["init", str(directory), "--description", "Acme CRM hosts"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["data"]["project"]["git"] == "initialized"
    assert "Acme CRM hosts" in (directory / "README.md").read_text(encoding="utf-8")

    exit_code = main(["init", str(tmp_path / "other"), "--description", " "])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"]["code"] == "ARG_ERROR"


def test_cli_init_reports_errors_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["init", str(tmp_path), "--rev", "abc"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"]["code"] == "ARG_ERROR"
    assert "--rev" in json.dumps(payload["error"])

    (tmp_path / "x").write_text("", encoding="utf-8")
    exit_code = main(["init", str(tmp_path)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["code"] == "PRECONDITION"
    assert payload["error"]["context"]["code"] == "project_directory_not_empty"


def test_fresh_project_validates_as_empty(tmp_path: Path) -> None:
    directory = tmp_path / "fleet"
    init_project(_options(directory))

    with pytest.raises(ConfigValidationError) as error:
        validate_config(directory, SCHEMAS)

    assert error.value.issue.code == "project_empty"


def test_validation_rejects_a_resource_in_the_wrong_directory(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    misplaced = project / "servers" / "crm.yaml"
    (project / "applications" / "crm.yaml").rename(misplaced)

    with pytest.raises(ConfigValidationError) as error:
        validate_config(project, SCHEMAS)

    assert error.value.issue.code == "resource_misplaced"
    assert "applications/" in error.value.issue.message


def test_validation_reads_only_kind_directories(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    (project / "tmp" / "import" / "config").mkdir(parents=True)
    (project / "tmp" / "import" / "config" / "junk.yaml").write_text(
        "kind: Nonsense\n", encoding="utf-8"
    )
    (project / ".venv" / "lib").mkdir(parents=True)
    (project / ".venv" / "lib" / "vendored.yml").write_text("- 1\n", encoding="utf-8")
    (project / ".sops.yaml").write_text("creation_rules: []\n", encoding="utf-8")
    (project / "playbooks").mkdir()
    (project / "playbooks" / "proxy.yml").write_text("- hosts: all\n", encoding="utf-8")
    (project / "Taskfile.yml").write_text("version: '3'\n", encoding="utf-8")

    state = validate_config(project, SCHEMAS)

    assert state.resource_count == validate_config(EXAMPLES, SCHEMAS).resource_count


def test_missing_project_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError) as error:
        validate_config(tmp_path / "absent", SCHEMAS)

    assert error.value.issue.code == "project_directory_missing"


def test_project_directory_resolution_order(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    from_environment = tmp_path / "from-env"
    current = tmp_path / "current"
    for directory in (explicit, from_environment):
        directory.mkdir()
    shutil.copytree(EXAMPLES, current)
    environment = {"CLOUDFALL_PROJECT": str(from_environment)}

    assert resolve_project_directory(explicit, environment, current) == explicit
    assert resolve_project_directory(None, environment, current) == from_environment
    assert resolve_project_directory(None, {}, current) == current


def test_project_directory_resolution_fails_outside_a_project(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProjectError) as error:
        resolve_project_directory(None, {}, tmp_path)

    assert error.value.code == "project_directory_unresolved"
    assert "--project" in error.value.detail

    with pytest.raises(ProjectError) as error:
        resolve_project_directory(tmp_path / "absent", {}, tmp_path)

    assert error.value.code == "project_directory_missing"


def test_project_context_runs_inside_the_project_and_restores(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    before = Path.cwd()

    with project_context(project, {}) as directory:
        assert directory == project.resolve()
        assert Path.cwd() == project.resolve()

    assert Path.cwd() == before


def test_cli_finds_the_project_in_the_current_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    before = Path.cwd()
    os.chdir(project)
    try:
        exit_code = main(["config", "validate", "--schemas", str(SCHEMAS)])
    finally:
        os.chdir(before)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["data"]["resources"] == 12


def test_cli_reports_a_missing_project_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["audit", "--project", str(tmp_path / "nope"), "--observed", "x"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 79
    assert payload["error"]["code"] == "PROJECT_INVALID"
    assert payload["error"]["context"]["code"] == "project_directory_missing"


def _section(document: str, heading: str) -> str:
    """Return the body of one ``##`` section of a markdown document."""
    marker = f"\n## {heading}\n"
    assert marker in document, heading
    body = document.split(marker, 1)[1]
    return body.split("\n## ", 1)[0]


def test_init_writes_the_agent_contract_and_the_claude_pointer(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "fleet"

    scaffold = init_project(_options(directory))

    assert "AGENTS.md" in scaffold.files
    assert "CLAUDE.md" in scaffold.files
    contract = (directory / "AGENTS.md").read_text(encoding="utf-8")
    assert contract.startswith("# Agent operating contract\n")
    assert "`fleet`" in contract
    assert "`uv run cloudfall …`" in contract
    assert "`--project <dir>`" in contract
    assert "`CLOUDFALL_PROJECT=<dir>`" in contract
    for heading in (
        "What this project manages",
        "Invocation",
        "Commands that change nothing on servers",
        "Commands that write project files",
        "Commands that change servers",
        "Output contract",
        "Evidence",
        "Secrets",
        "Git",
    ):
        assert f"\n## {heading}\n" in contract
    assert "Ask the human what this project manages" in _section(
        contract, "What this project manages"
    )
    output = _section(contract, "Output contract")
    assert "`data.status`" in output
    assert "`uv run cloudfall --schema`" in output
    assert "`uv run cloudfall changelog`" in output
    assert "`secretRefs`" in _section(contract, "Secrets")
    assert "`tmp/`" in _section(contract, "Evidence")
    assert "`.venv/`" in _section(contract, "Git")
    pointer = (directory / "CLAUDE.md").read_text(encoding="utf-8")
    assert pointer.count("\n") <= 2
    assert "`AGENTS.md`" in pointer


def test_agent_contract_states_the_description_as_the_purpose(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "fleet"
    description = ProjectDescription("Two Hetzner hosts running the CRM for Acme")

    init_project(_options(directory, description))

    contract = (directory / "AGENTS.md").read_text(encoding="utf-8")
    assert _section(contract, "What this project manages").strip() == str(description)
    assert "Ask the human what this project manages" not in contract


def test_agent_contract_classifies_every_command_by_effect(tmp_path: Path) -> None:
    """The contract names every server-changing command and no other one."""
    directory = tmp_path / "fleet"
    init_project(_options(directory))
    contract = (directory / "AGENTS.md").read_text(encoding="utf-8")
    sections = {
        CommandEffect.READ: _section(
            contract, "Commands that change nothing on servers"
        ),
        CommandEffect.PROJECT: _section(contract, "Commands that write project files"),
        CommandEffect.SERVERS: _section(contract, "Commands that change servers"),
    }

    for command in COMMANDS:
        row = f"| `{command.invocation}` |"
        for effect, section in sections.items():
            assert (row in section) is (command.effect is effect), command.name
    for name in (
        "deploy",
        "rollback",
        "restart",
        "data migrate",
        "migrate",
        "backup run",
        "backup verify",
        "operator approve",
    ):
        assert f"| `uv run cloudfall {name}` |" in sections[CommandEffect.SERVERS]
    assert (
        "| `uv run cloudfall-engine playbook run` |" in sections[CommandEffect.SERVERS]
    )
    for line in sections[CommandEffect.SERVERS].splitlines():
        if line.startswith("| `uv run"):
            assert line.count(" | ") == 2, line
            assert not line.endswith("|  |"), line
