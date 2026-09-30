"""Create Cloudfall projects.

A project is one directory holding everything Cloudfall needs to run one
user's stack: the fleet (servers, server types, SSH public keys), the
applications (applications, components, services, domains), the operating
declarations (alert rules, operator policies, logging stacks), and the
``pyproject.toml`` that pins the released Cloudfall the project runs with.
Every ``cloudfall`` command runs inside a project: the current directory
when it is one, else ``--project``, else ``CLOUDFALL_PROJECT``.
Evidence, receipts, and rendered files land under ``tmp/`` inside the
project and are never committed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

from cloudfall.commands import EXIT_CODES, CommandEffect, commands_with_effect
from cloudfall.domain import ResourceKind

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

_PROJECT_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_RELEASE_VERSION_PATTERN = re.compile(
    r"^\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?$"
)
_PROJECT_NAME_MAX_LENGTH = 64
_PROJECT_DESCRIPTION_MAX_LENGTH = 512
_GIT_TIMEOUT_SECONDS = 30
_GIT_NOT_A_REPOSITORY_EXIT_CODE = 128

REPOSITORY_URL = "https://github.com/romamo/cloudfall"
"""Location of the Cloudfall repository, for links to its files."""

SECRETS_DIRECTORY = "secrets"
"""Project directory holding sops-encrypted secret fragments."""

SOPS_CONFIGURATION_FILE = ".sops.yaml"
"""Project file telling sops which key encrypts the secret fragments."""

SECRETS_GUIDE_PATH = "docs/secrets-guide.md"
"""Path of the secrets guide inside the Cloudfall repository."""

EXAMPLES_PATH = "config/examples"
"""Path of the reference resource set inside the Cloudfall repository."""

AGENT_CONTRACT_FILE = "AGENTS.md"
"""Project file stating the terms on which an AI agent operates it."""

CLAUDE_POINTER_FILE = "CLAUDE.md"
"""One-line project file pointing Claude Code at the agent contract."""

_ERROR_NAME_UNDERIVED = "project_name_underived"
_ERROR_DIRECTORY_MISSING = "project_directory_missing"
_ERROR_DIRECTORY_UNRESOLVED = "project_directory_unresolved"
_ERROR_VERSION_UNRESOLVED = "project_version_unresolved"
_ERROR_NOT_A_DIRECTORY = "project_directory_not_a_directory"
_ERROR_NOT_EMPTY = "project_directory_not_empty"
_ERROR_GIT_UNAVAILABLE = "project_git_unavailable"
_ERROR_GIT_FAILED = "project_git_failed"


RUNTIME_DIRECTORY = "tmp"
"""Project directory holding evidence and receipts; never a resource source."""

PROJECT_DIRECTORY_VARIABLE = "CLOUDFALL_PROJECT"
"""Environment variable naming the project when ``--project`` is absent."""


class ProjectError(RuntimeError):
    """Fail-fast project error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        """Record the failure code and human-readable detail."""
        self.code = code
        self.detail = message
        super().__init__(f"{code}: {message}")

    def as_dict(self) -> dict[str, object]:
        """Serialize the error envelope for system boundaries."""
        return {
            "status": "error",
            "error": {"code": self.code, "message": self.detail},
        }


@dataclass(frozen=True, slots=True)
class ReleaseVersion:
    """Released Cloudfall version a project pins from the package index."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the release-version invariant at construction time."""
        if not _RELEASE_VERSION_PATTERN.fullmatch(self.value):
            message = f"release version must be a PEP 440 release: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ReleaseVersion:
        """Coerce a boundary value while keeping internal APIs strictly typed."""
        if not isinstance(value, str):
            message = f"release version must be a string, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value.strip())

    def __str__(self) -> str:
        """Return the serialized version."""
        return self.value


@dataclass(frozen=True, slots=True)
class ProjectName:
    """Package name of a project, as written into its ``pyproject.toml``."""

    value: str

    def __post_init__(self) -> None:
        """Enforce a normalized PEP 503 style name."""
        if not _PROJECT_NAME_PATTERN.fullmatch(self.value):
            message = (
                "project name must be lowercase letters, digits, '.', '_' or '-' "
                f"and start and end with a letter or digit: {self.value!r}"
            )
            raise ValueError(message)
        if len(self.value) > _PROJECT_NAME_MAX_LENGTH:
            message = f"project name exceeds 64 characters: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ProjectName:
        """Coerce a boundary value while keeping internal APIs strictly typed."""
        if not isinstance(value, str):
            message = f"project name must be a string, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value.strip().lower())

    @classmethod
    def from_directory(cls, directory: Path) -> ProjectName:
        """Derive the name from the directory, lowercased and dash-joined."""
        raw = directory.resolve().name
        normalized = re.sub(r"[^a-z0-9._-]+", "-", raw.lower()).strip("-._")
        if not normalized:
            message = f"cannot derive a project name from directory {directory}"
            raise ProjectError(_ERROR_NAME_UNDERIVED, message)
        return cls(normalized)

    def __str__(self) -> str:
        """Return the serialized name."""
        return self.value


@dataclass(frozen=True, slots=True)
class ProjectDescription:
    """One line saying what a project manages, for its README and metadata."""

    value: str

    def __post_init__(self) -> None:
        """Require one trimmed, printable line that TOML can hold unescaped."""
        if not self.value or self.value != self.value.strip():
            message = f"project description must be a trimmed line: {self.value!r}"
            raise ValueError(message)
        if not self.value.isprintable() or any(c in self.value for c in '"\\'):
            message = (
                "project description must be one printable line without "
                f"quotes or backslashes: {self.value!r}"
            )
            raise ValueError(message)
        if len(self.value) > _PROJECT_DESCRIPTION_MAX_LENGTH:
            message = (
                "project description exceeds "
                f"{_PROJECT_DESCRIPTION_MAX_LENGTH} characters"
            )
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ProjectDescription:
        """Coerce a boundary value while keeping internal APIs strictly typed."""
        if not isinstance(value, str):
            message = (
                f"project description must be a string, got {type(value).__name__}"
            )
            raise TypeError(message)
        return cls(value.strip())

    def __str__(self) -> str:
        """Return the serialized description."""
        return self.value


@dataclass(frozen=True, slots=True)
class InitOptions:
    """Everything ``cloudfall init`` needs to lay out one project."""

    directory: Path
    name: ProjectName
    version: ReleaseVersion
    description: ProjectDescription | None = None


class GitSetup(Enum):
    """What ``cloudfall init`` did about version control for the project."""

    INITIALIZED = "initialized"
    """A new repository was created in the project directory."""

    ENCLOSED = "enclosed"
    """The directory already lies inside a repository, which was left alone."""


@dataclass(frozen=True, slots=True)
class ProjectScaffold:
    """Files written by ``cloudfall init``, for the CLI and agent envelope."""

    directory: Path
    name: ProjectName
    version: ReleaseVersion
    files: tuple[str, ...]
    git: GitSetup

    def as_dict(self) -> dict[str, object]:
        """Serialize the scaffold result for system boundaries."""
        return {
            "status": "ok",
            "project": {
                "directory": str(self.directory),
                "name": str(self.name),
                "version": str(self.version),
                "git": self.git.value,
            },
            "files": list(self.files),
            "next": [
                f"cd {self.directory} && uv sync",
                "uv run cloudfall add ssh-key ~/.ssh/id_ed25519.pub --owner <you>",
                "uv run cloudfall add server h1 --address <ip-or-hostname>",
                "uv run cloudfall config validate",
            ],
        }


def is_project(directory: Path) -> bool:
    """Return whether a directory holds at least one resource kind directory."""
    return directory.is_dir() and any(
        (directory / kind.directory).is_dir() for kind in ResourceKind
    )


def resolve_project_directory(
    explicit: Path | None, environment: Mapping[str, str], current: Path
) -> Path:
    """Pick the project directory a command runs in.

    Precedence: ``--project``, then ``CLOUDFALL_PROJECT``, then the
    current directory when it is a project. An explicitly named directory
    must exist; whether it holds resources is validation's concern.
    """
    if explicit is not None:
        candidate, origin = explicit, "--project"
    elif environment.get(PROJECT_DIRECTORY_VARIABLE):
        candidate = Path(environment[PROJECT_DIRECTORY_VARIABLE])
        origin = PROJECT_DIRECTORY_VARIABLE
    elif is_project(current):
        return current.resolve()
    else:
        message = (
            f"{current} is not a project: it has no resource directory such as "
            f"{ResourceKind.SERVER.directory}/. Run from inside a project, pass "
            f"--project, or set {PROJECT_DIRECTORY_VARIABLE}"
        )
        raise ProjectError(_ERROR_DIRECTORY_UNRESOLVED, message)
    if not candidate.is_dir():
        message = f"project directory from {origin} does not exist: {candidate}"
        raise ProjectError(_ERROR_DIRECTORY_MISSING, message)
    return candidate.resolve()


@contextmanager
def project_context(
    explicit: Path | None, environment: Mapping[str, str]
) -> Iterator[Path]:
    """Run the body as if started inside the resolved project directory.

    Relative paths, including every ``tmp/`` default, then land in the
    project, whatever directory the command was launched from. The previous
    working directory is restored afterwards.
    """
    previous = Path.cwd()
    directory = resolve_project_directory(explicit, environment, previous)
    os.chdir(directory)
    try:
        yield directory
    finally:
        os.chdir(previous)


def project_path(value: str) -> Path:
    """Return a runtime path, refusing a relative path that leaves the project.

    Relative paths resolve against the project (see ``project_context``), so
    one whose normalized form climbs above it (``../../x``, ``tmp/../../x``)
    would read or write outside the project without saying so. Locations
    outside the project stay possible, but only as an explicit absolute path.
    """
    path = Path(value)
    if not path.is_absolute() and Path(os.path.normpath(value)).parts[:1] == (
        "..",
    ):
        message = (
            f"relative path {value!r} leaves the project directory; "
            "pass an absolute path to use a location outside the project"
        )
        raise ValueError(message)
    return path


def resolve_installed_version(
    read_version: Callable[[], str] | None = None,
) -> ReleaseVersion:
    """Return the released version of the running Cloudfall.

    A project depends on ``cloudfall==<version>`` and resolves it from the
    package index like any other dependency. An install whose version is not
    a release cannot be pinned that way, which is a checkout of this
    repository between releases.
    """
    raw = (read_version or _installed_version)()
    try:
        return ReleaseVersion.from_boundary(raw)
    except ValueError as error:
        message = (
            f"the installed cloudfall version {raw!r} is not a release, so a "
            "project cannot pin it; install a released cloudfall, or set the "
            "dependency in the project's pyproject.toml by hand"
        )
        raise ProjectError(_ERROR_VERSION_UNRESOLVED, message) from error


def _installed_version() -> str:
    return metadata.version("cloudfall")


def _git_binary() -> str:
    git = shutil.which("git")
    if git is None:
        message = "git is required but is not installed"
        raise ProjectError(_ERROR_GIT_UNAVAILABLE, message)
    return git


def _run_git(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - fixed binary, fixed arguments.
            [_git_binary(), "-C", str(directory), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        message = f"git {arguments[0]} in {directory} timed out: {error}"
        raise ProjectError(_ERROR_GIT_FAILED, message) from error


def _git_or_fail(directory: Path, *arguments: str) -> str:
    completed = _run_git(directory, *arguments)
    if completed.returncode != 0:
        message = (
            f"git {arguments[0]} in {directory} failed with exit code "
            f"{completed.returncode}: {completed.stderr.strip()}"
        )
        raise ProjectError(_ERROR_GIT_FAILED, message)
    return completed.stdout


def _initialize_git(directory: Path) -> GitSetup:
    """Create a repository in the directory unless one already encloses it."""
    enclosing = _run_git(directory, "rev-parse", "--is-inside-work-tree")
    if enclosing.returncode == 0 and enclosing.stdout.strip() == "true":
        return GitSetup.ENCLOSED
    if enclosing.returncode != _GIT_NOT_A_REPOSITORY_EXIT_CODE:
        message = (
            f"git rev-parse in {directory} failed with exit code "
            f"{enclosing.returncode}: {enclosing.stderr.strip()}"
        )
        raise ProjectError(_ERROR_GIT_FAILED, message)
    _git_or_fail(directory, "init", "--quiet")
    return GitSetup.INITIALIZED


def init_project(
    options: InitOptions,
    initialize_git: Callable[[Path], GitSetup] | None = None,
) -> ProjectScaffold:
    """Lay out a new project in an empty or absent directory."""
    directory = options.directory
    if directory.exists() and not directory.is_dir():
        message = f"project path exists and is not a directory: {directory}"
        raise ProjectError(_ERROR_NOT_A_DIRECTORY, message)
    if directory.is_dir() and any(directory.iterdir()):
        message = f"project directory is not empty: {directory}"
        raise ProjectError(_ERROR_NOT_EMPTY, message)
    directory.mkdir(parents=True, exist_ok=True)
    git = (initialize_git or _initialize_git)(directory)

    files: dict[str, str] = {
        "pyproject.toml": _pyproject(options),
        ".gitignore": _GITIGNORE,
        SOPS_CONFIGURATION_FILE: _SOPS_CONFIGURATION,
        "README.md": _readme(options, git),
        AGENT_CONTRACT_FILE: _agent_contract(options),
        CLAUDE_POINTER_FILE: _CLAUDE_POINTER,
    }
    for kind in ResourceKind:
        files[f"{kind.directory}/.gitkeep"] = ""
    files[f"{SECRETS_DIRECTORY}/.gitkeep"] = ""
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return ProjectScaffold(
        directory=directory,
        name=options.name,
        version=options.version,
        files=tuple(files),
        git=git,
    )


def _pyproject(options: InitOptions) -> str:
    description = (
        str(options.description)
        if options.description is not None
        else "Cloudfall project: fleet, applications, and operating declarations"
    )
    return f"""[project]
name = "{options.name}"
version = "0"
description = "{description}"
requires-python = ">=3.14"
# Cloudfall is an installed package: the schema catalog and the Ansible engine
# ship inside the wheel. Bump this version to move the project to a newer
# Cloudfall, then run `uv sync`. Add the `mcp` extra (`cloudfall[mcp]`) to run
# the `cloudfall-mcp` agent server.
dependencies = ["cloudfall=={options.version}"]

[tool.uv]
package = false
"""


_GITIGNORE = f"""# Runtime state: evidence, receipts, rendered inventory, built
# artifacts, rendered environment files, and the persisted migration plan
{RUNTIME_DIRECTORY}/

# Local environment and tooling
.venv/
__pycache__/
"""


_SOPS_CONFIGURATION = f"""\
# sops encrypts every file matching a rule below with the listed age keys,
# so fragments under {SECRETS_DIRECTORY}/ are safe to commit. One-time setup:
#
#   age-keygen -o ~/.config/cloudfall/age.key
#   export SOPS_AGE_KEY_FILE=~/.config/cloudfall/age.key
#
# Replace the `age` value with the public key (age1...) that age-keygen
# printed, then write fragments with `sops {SECRETS_DIRECTORY}/production/<app>.env`.
creation_rules:
  - path_regex: {SECRETS_DIRECTORY}/.*\\.env$
    age: age1replace-with-your-public-key
"""


def _readme(options: InitOptions, git: GitSetup) -> str:
    tmp = RUNTIME_DIRECTORY
    secrets = SECRETS_DIRECTORY
    browse = REPOSITORY_URL
    ref = f"v{options.version}"
    secrets_guide = f"{browse}/blob/{ref}/{SECRETS_GUIDE_PATH}"
    examples = f"{browse}/tree/{ref}/{EXAMPLES_PATH}"
    about = (
        str(options.description)
        if options.description is not None
        else (
            "Say what this project manages: which servers, which applications "
            "run on them, and who operates them. Pass `--description` to "
            "`cloudfall init` to fill this in at creation time."
        )
    )
    git_note = (
        "This directory is a fresh git repository: add a private remote and\n"
        "make the first commit, then install Cloudfall and validate:"
        if git is GitSetup.INITIALIZED
        else "This directory lies inside an existing git repository, so commit\n"
        "it there. Install Cloudfall and validate:"
    )
    rows = "\n".join(
        f"| `{kind.directory}/` | `{kind.value}` |" for kind in ResourceKind
    )
    return f"""# {options.name}

A [Cloudfall][cloudfall] project: the fleet, the applications that run on
it, and the declarations that operate them, in one private repository. Every
`cloudfall` command run from inside this directory finds it on its own; from
elsewhere, pass `--project` or set `CLOUDFALL_PROJECT`.

## About

{about}

## Layout

| Directory | Resource kind |
|---|---|
{rows}

Only these directories are read as resources, so playbooks, roles, docs, and
tooling files may live anywhere else in the project. `{tmp}/` holds evidence,
receipts, rendered inventory, built artifacts, and rendered environment files
and is ignored by git. Secret values never enter the resources: declare
`secretRefs` and keep sops-encrypted fragments in `{secrets}/`, which
`.sops.yaml` encrypts to your age key once you put the key there (see the
[secrets guide][secrets-guide]).

## Start

{git_note}

```console
uv sync
uv run cloudfall config validate
```

Validation fails until the project declares its first resources. Declare
your SSH key and your first server; the server's type is created from the
bundled Debian 13 baseline when it does not exist yet:

```console
uv run cloudfall add ssh-key ~/.ssh/id_ed25519.pub --owner <you>
uv run cloudfall add server h1 --address <ip-or-hostname>
uv run cloudfall config validate
```

Edit the written files freely; the [reference set][examples] in the
Cloudfall repository shows every kind. Then bring the host under management
and prove it:

```console
uv run cloudfall-engine inventory render --output {tmp}/ansible-inventory.json
uv run cloudfall-engine playbook run inspect --inventory {tmp}/ansible-inventory.json \\
  --extra-vars cloudfall_inspect_output_directory=$PWD/{tmp}/observed
uv run cloudfall audit --observed {tmp}/observed
```

Add applications by hand or with `cloudfall import render`, then drive the
whole migration with the resumable plan:

```console
uv run cloudfall migrate --build <component>=main
uv run cloudfall migrate --build <component>=main --yes
```

Cloudfall is pinned to one released version in `pyproject.toml`; bump it
and run `uv sync` to move the project to a newer release. The links below
point at the pinned release.

[cloudfall]: {browse}
[secrets-guide]: {secrets_guide}
[examples]: {examples}
"""


_CLAUDE_POINTER = f"""\
Read `{AGENT_CONTRACT_FILE}` before doing anything in this project; it is the
operating contract for AI agents here.
"""


def _agent_contract(options: InitOptions) -> str:
    tmp = RUNTIME_DIRECTORY
    secrets = SECRETS_DIRECTORY
    kinds = textwrap.fill(
        ", ".join(f"`{kind.directory}/`" for kind in ResourceKind), width=72
    )
    purpose = (
        str(options.description)
        if options.description is not None
        else (
            "Not stated yet: the project was created without `--description`.\n"
            "Ask the human what this project manages before planning any\n"
            "change; do not infer it from the resources."
        )
    )
    reads = _command_rows(CommandEffect.READ)
    writes = _command_rows(CommandEffect.PROJECT)
    mutations = _command_rows(CommandEffect.SERVERS, with_gate=True)
    exit_rows = "\n".join(
        f"| `{exit_code.code}` | {exit_code.meaning} |" for exit_code in EXIT_CODES
    )
    return f"""# Agent operating contract

This is a [Cloudfall](https://cloudfall.dev) project: `{options.name}`.
Humans and AI agents both edit the resources and both run the CLI. An
agent drafts and validates; a human approves anything that changes a
server. These are the terms.

## What this project manages

{purpose}

## Invocation

Run every command as `uv run cloudfall …` (or `uv run cloudfall-engine …`)
from this directory, which the CLI recognizes as the project. From
elsewhere, pass `--project <dir>` or set `CLOUDFALL_PROJECT=<dir>`.
Relative paths, including every `{tmp}/` default, resolve against the
project either way. Run `uv sync` once after cloning and after any change
to the `rev` pin in `pyproject.toml`.

Resources are plain YAML, one document per file, in the kind directories,
and only those directories are read as resources:

{kinds}

Write or edit them freely, then run `uv run cloudfall config validate`
before anything else. Never write a value you would have to guess: ask for
addresses, key owners, git refs, and the like.

## Commands that change nothing on servers

Run these whenever they help. They read the project and evidence, may
probe servers and public endpoints read-only, and write only under
`{tmp}/`.

| Command | Does |
|---|---|
{reads}

## Commands that write project files

They write resources into this project on the controller; commit the
result. Servers are untouched.

| Command | Does |
|---|---|
{writes}

## Commands that change servers

Every command below changes servers. Do not run one, and do not add its
gate, unless a human has approved that specific run. The agent's job is to
prepare it: draft the resources, run the read-only checks, run the command
without its gate to obtain the plan, and show the plan to the human. The
human then runs the gated command, or tells the agent to run it. Nothing
on this list is safe to run on your own initiative.

| Command | Does | Gate |
|---|---|---|
{mutations}

There is no other way to change a server from this project: no ad hoc SSH,
no hand-written playbook run outside `cloudfall-engine playbook run`.

## Output contract

Every command prints one JSON document on stdout and nothing else, with the
same top level whatever the command: `ok` is true exactly when the exit
code is 0, `status` is the command's verdict (`ok`, `plan`, `drift`,
`unhealthy`, `paused`, `failed`), `data` holds the result, and `error` is
`null`. A failure prints `{{"ok": false, "status": "error", "data": null,
"error": {{"code": …, "message": …, "exit_code": …}}}}` on stderr, with the
code the process exits with; the `code` is stable: branch on it, not on
the message. A failed `migrate` step is the one
failure on stdout, with its steps under `data`. `--output json` is accepted
and changes nothing; `--quiet` writes nothing to stderr, not even the error,
and `--warnings-as-errors` fails a result that carries a warning. `--help`
is not JSON: use `cloudfall --schema` for the interface. Read the exit code
first:

| Exit | Meaning |
|---|---|
{exit_rows}

`cloudfall-engine` keeps its own contract: its envelope has no `status`, a
failure is an envelope on stdout too, and its codes are listed by
`cloudfall-engine manifest` (79 to 82 for engine failures, with the
snake_case code in `error.context.code`).

## Evidence

`{tmp}/` holds everything derived or produced: observations, receipts,
rendered inventory, built artifacts, rendered environment files, and the
persisted migration plan. It is ignored by git. Read anything there to
answer questions about state. Exception: `{tmp}/env/` and any other
rendered environment file contain secret values; do not read or quote them.

Status comes from evidence, never from assumption. A deployment counts as
done when its receipt exists; a server counts as compliant when `audit`
says so against a fresh observation. Do not report success you cannot
point to a receipt or observation for.

## Secrets

Resources declare `secretRefs`; they never hold values. Secret values live
only in sops-encrypted fragments under `{secrets}/`, which a human writes
and commits. Never read, decrypt, write, or echo a secret value, and never
place one in a resource, a command line, a commit, or a message. If a
command needs a secret file, ask the human to provide the path.

## Git

Resources, `{secrets}/`, `.sops.yaml`, `pyproject.toml`, `uv.lock`, and the
documents in this directory are committed. `{tmp}/` and `.venv/` are not.
Commit resource changes with a message saying what changed and why; do not
commit on the human's behalf unless asked.
"""


def _command_rows(effect: CommandEffect, *, with_gate: bool = False) -> str:
    rows = []
    for command in commands_with_effect(effect):
        cells = [f"`{command.invocation}`", command.summary]
        if with_gate:
            cells.append(command.gate or "")
        rows.append(f"| {' | '.join(cells)} |")
    return "\n".join(rows)
