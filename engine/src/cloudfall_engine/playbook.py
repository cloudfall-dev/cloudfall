"""Run Ansible playbooks under the engine's configuration.

A project keeps its own playbooks and roles next to its config.
This module runs those, and the bundled engine playbooks, with the
engine's ``ansible.cfg`` and a role search path that always ends with the
bundled roles, so project playbooks may reuse them without a checkout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_PLAYBOOK_SUFFIX = ".yml"
_ERROR_PLAYBOOK_MISSING = "playbook_missing"
_ERROR_INVENTORY_MISSING = "inventory_missing"
_ERROR_ROLES_MISSING = "roles_missing"
_ERROR_ANSIBLE_MISSING = "ansible_missing"
_ERROR_ARGUMENT = "argument_invalid"


class PlaybookError(RuntimeError):
    """Raised when a playbook run cannot be prepared."""

    def __init__(self, code: str, detail: str) -> None:
        """Record a machine-readable code alongside the human detail."""
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def as_dict(self) -> dict[str, object]:
        """Return the error in the CLI's JSON error envelope."""
        return {
            "error": {"code": self.code, "message": self.detail},
            "status": "error",
        }


def playbook_directory(engine_directory: Path) -> Path:
    """Return the directory holding the engine's bundled playbooks."""
    return engine_directory / "ansible" / "playbooks"


def bundled_playbooks(engine_directory: Path) -> tuple[str, ...]:
    """Return the names of every bundled playbook, sorted."""
    directory = playbook_directory(engine_directory)
    if not directory.is_dir():
        detail = f"engine playbook directory does not exist: {directory}"
        raise PlaybookError(_ERROR_PLAYBOOK_MISSING, detail)
    return tuple(
        sorted(
            path.stem
            for path in directory.iterdir()
            if path.is_file() and path.suffix == _PLAYBOOK_SUFFIX
        )
    )


def resolve_playbook(reference: str, engine_directory: Path) -> Path:
    """Resolve a bare name inside the engine, or a path as given.

    ``inspect`` and ``inspect.yml`` both name the bundled playbook; anything
    containing a path separator is a playbook file outside the engine.
    """
    if not reference:
        detail = "playbook reference must not be empty"
        raise PlaybookError(_ERROR_ARGUMENT, detail)
    candidate = Path(reference)
    if len(candidate.parts) == 1:
        name = reference
        if candidate.suffix != _PLAYBOOK_SUFFIX:
            name = f"{reference}{_PLAYBOOK_SUFFIX}"
        candidate = playbook_directory(engine_directory) / name
    if not candidate.is_file():
        detail = f"playbook does not exist: {candidate}"
        raise PlaybookError(_ERROR_PLAYBOOK_MISSING, detail)
    return candidate


@dataclass(frozen=True, slots=True)
class PlaybookRun:
    """One validated ``ansible-playbook`` invocation."""

    playbook: Path
    inventory_file: Path
    role_directories: tuple[Path, ...] = ()
    extra_vars: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    limit: str | None = None
    check: bool = False
    diff: bool = False
    syntax_check: bool = False

    def __post_init__(self) -> None:
        """Reject inputs Ansible would only fail on later."""
        if not self.playbook.is_file():
            detail = f"playbook does not exist: {self.playbook}"
            raise PlaybookError(_ERROR_PLAYBOOK_MISSING, detail)
        if not self.inventory_file.is_file():
            detail = f"inventory file does not exist: {self.inventory_file}"
            raise PlaybookError(_ERROR_INVENTORY_MISSING, detail)
        for directory in self.role_directories:
            if not directory.is_dir():
                detail = f"role directory does not exist: {directory}"
                raise PlaybookError(_ERROR_ROLES_MISSING, detail)
        if self.limit is not None and not self.limit:
            detail = "limit must not be empty"
            raise PlaybookError(_ERROR_ARGUMENT, detail)
        if any(not value for value in self.extra_vars):
            detail = "extra vars must not be empty"
            raise PlaybookError(_ERROR_ARGUMENT, detail)
        if any(not value for value in self.tags):
            detail = "tags must not be empty"
            raise PlaybookError(_ERROR_ARGUMENT, detail)


@dataclass(frozen=True, slots=True)
class PlaybookCommand:
    """The process a playbook run becomes."""

    argv: tuple[str, ...]
    environment: dict[str, str]


def playbook_command(run: PlaybookRun, engine_directory: Path) -> PlaybookCommand:
    """Translate a run into ``ansible-playbook`` arguments and environment."""
    binary = shutil.which("ansible-playbook")
    if binary is None:
        detail = "ansible-playbook is not installed on this controller"
        raise PlaybookError(_ERROR_ANSIBLE_MISSING, detail)
    ansible_directory = engine_directory / "ansible"
    configuration = ansible_directory / "ansible.cfg"
    bundled_roles = ansible_directory / "roles"
    for required in (configuration, bundled_roles):
        if not required.exists():
            detail = f"engine is incomplete, missing {required}"
            raise PlaybookError(_ERROR_PLAYBOOK_MISSING, detail)

    argv: list[str] = [binary, "--inventory", str(run.inventory_file)]
    if run.check:
        argv.append("--check")
    if run.diff:
        argv.append("--diff")
    if run.syntax_check:
        argv.append("--syntax-check")
    if run.limit is not None:
        argv.extend(("--limit", run.limit))
    for value in run.extra_vars:
        argv.extend(("--extra-vars", value))
    if run.tags:
        argv.extend(("--tags", ",".join(run.tags)))
    argv.append(str(run.playbook))

    roles = (*run.role_directories, bundled_roles)
    environment = {
        "ANSIBLE_CONFIG": str(configuration.resolve()),
        "ANSIBLE_ROLES_PATH": os.pathsep.join(
            str(directory.resolve()) for directory in roles
        ),
    }
    return PlaybookCommand(argv=tuple(argv), environment=environment)


def execute_playbook(run: PlaybookRun, engine_directory: Path) -> int:
    """Run the playbook in the foreground and return its exit code."""
    command = playbook_command(run, engine_directory)
    # Stdout belongs to the caller's result document, so Ansible's play log
    # streams to stderr, live, for the person watching the run.
    completed = subprocess.run(  # noqa: S603 - fixed binary, validated args.
        command.argv,
        env={**os.environ, **command.environment},
        stdout=sys.__stderr__,
        check=False,
    )
    return completed.returncode
