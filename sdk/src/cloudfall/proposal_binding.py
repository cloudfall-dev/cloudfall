"""Bind a proposal to what it was checked against, so an approval runs that.

A person approves the diff check mode showed. The run that follows reads
the playbook, its roles, templates and inventory from the project as they
are at approval time, so the record keeps what they were at proposal time:
the sha256 of the playbook and of the verify playbook and, in a git work
tree, the HEAD commit and a digest of the uncommitted changes (D-20).

The digest leaves out the directories the propose step writes itself: the
decisions directory, ``record/`` and ``tmp/``. Files git ignores are not in
it either, so roles, templates and inventory are bound only when git sees
them. A project outside git binds the two playbooks alone.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cloudfall.domain import Sha256Digest
from cloudfall.project import RECORD_DIRECTORY

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

RUNTIME_DIRECTORY = "tmp"
"""The project's throwaway directory, which the binding leaves out."""

_GIT_TIMEOUT_SECONDS = 60
_GIT_NOT_A_REPOSITORY_EXIT_CODE = 128
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_ERROR_GIT_FAILED = "decision_binding_git_failed"


class BindingError(RuntimeError):
    """A binding could not be read: git failed in the project."""

    def __init__(self, code: str, message: str) -> None:
        """Record the failure code and human-readable detail."""
        self.code = code
        self.detail = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class CommitId:
    """A full lowercase git commit id, SHA-1 or SHA-256."""

    value: str

    def __post_init__(self) -> None:
        """Enforce 40 or 64 lowercase hexadecimal characters."""
        if not _COMMIT_PATTERN.fullmatch(self.value):
            message = f"invalid git commit: {self.value!r}"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class Binding:
    """What one proposal was checked against, as its record keeps it."""

    playbook_sha256: Sha256Digest
    verify_playbook_sha256: Sha256Digest | None
    """None when the operation declares no verify playbook."""

    commit: CommitId | None
    """None outside a git work tree, or in one with no commit yet."""

    worktree_sha256: Sha256Digest | None
    """None outside a git work tree."""

    def as_dict(self) -> dict[str, object]:
        """Serialize the binding for the record."""
        return {
            "playbookSha256": self.playbook_sha256.value,
            "verifyPlaybookSha256": _value(self.verify_playbook_sha256),
            "commit": None if self.commit is None else self.commit.value,
            "worktreeSha256": _value(self.worktree_sha256),
        }

    @classmethod
    def from_record(cls, value: object) -> Binding | None:
        """Read the binding a record holds; None for a record made before it."""
        if not isinstance(value, dict):
            return None
        record = cast("Mapping[str, object]", value)
        verify = record["verifyPlaybookSha256"]
        commit = record["commit"]
        worktree = record["worktreeSha256"]
        return cls(
            playbook_sha256=Sha256Digest.from_boundary(record["playbookSha256"]),
            verify_playbook_sha256=(
                None if verify is None else Sha256Digest.from_boundary(verify)
            ),
            commit=None if commit is None else CommitId(str(commit)),
            worktree_sha256=(
                None if worktree is None else Sha256Digest.from_boundary(worktree)
            ),
        )


@dataclass(frozen=True, slots=True)
class BoundProject:
    """The project a binding is read from, and what the digest leaves out."""

    repository: Path
    decisions: Path
    """The decisions directory, where propose writes the record and its diff."""

    def excluded(self) -> tuple[Path, ...]:
        """Return the directories the propose step writes, relative to the project."""
        root = self.repository.resolve()
        excluded = [Path(RECORD_DIRECTORY), Path(RUNTIME_DIRECTORY)]
        decisions = self.decisions.resolve()
        if decisions.is_relative_to(root) and decisions != root:
            excluded.append(decisions.relative_to(root))
        return tuple(excluded)


def bind(
    project: BoundProject, playbook: Path, verify_playbook: Path | None
) -> Binding:
    """Read what the proposal is checked against, before its check run.

    The playbook paths are the project's, as the catalog declares them; a
    declared playbook exists, so a missing one is an error here.
    """
    commit, worktree = _git_state(project)
    return Binding(
        playbook_sha256=_required_sha256(project.repository / playbook),
        verify_playbook_sha256=(
            None
            if verify_playbook is None
            else _required_sha256(project.repository / verify_playbook)
        ),
        commit=commit,
        worktree_sha256=worktree,
    )


def changes(
    recorded: Binding,
    project: BoundProject,
    playbook: Path,
    verify_playbook: Path | None,
) -> tuple[str, ...]:
    """Name each part of the binding that differs from the project now.

    Empty when the project is as it was when the proposal was checked.
    """
    found: list[str] = []
    if file_sha256(project.repository / playbook) != recorded.playbook_sha256:
        found.append(f"the playbook {playbook}")
    current_verify = (
        None
        if verify_playbook is None
        else file_sha256(project.repository / verify_playbook)
    )
    if current_verify != recorded.verify_playbook_sha256:
        found.append(f"the verify playbook {verify_playbook}")
    commit, worktree = _git_state(project)
    if commit != recorded.commit:
        was = "none" if recorded.commit is None else recorded.commit.value
        now = "none" if commit is None else commit.value
        found.append(f"the git commit (was {was}, now {now})")
    if worktree != recorded.worktree_sha256:
        found.append("the uncommitted changes in the project")
    return tuple(found)


def file_sha256(path: Path) -> Sha256Digest | None:
    """Return the sha256 of a file's content; None when there is no such file."""
    if not path.is_file():
        return None
    return Sha256Digest(hashlib.sha256(path.read_bytes()).hexdigest())


def _required_sha256(path: Path) -> Sha256Digest:
    return Sha256Digest(hashlib.sha256(path.read_bytes()).hexdigest())


def _value(digest: Sha256Digest | None) -> str | None:
    return None if digest is None else digest.value


def _git_state(project: BoundProject) -> tuple[CommitId | None, Sha256Digest | None]:
    """Return the HEAD commit and the uncommitted-changes digest, or none outside git.

    A controller without git cannot tell, so it binds as outside git: a
    record bound in git then differs at approval and is refused.
    """
    git = shutil.which("git")
    if git is None:
        return None, None
    inside = _git(git, project.repository, "rev-parse", "--is-inside-work-tree")
    if inside.returncode == _GIT_NOT_A_REPOSITORY_EXIT_CODE:
        return None, None
    _succeeded(inside, project.repository, "rev-parse")
    if inside.stdout.decode("utf-8").strip() != "true":
        return None, None
    head = _git(git, project.repository, "rev-parse", "--verify", "--quiet", "HEAD")
    commit: CommitId | None
    if head.returncode == 0:
        commit = CommitId(head.stdout.decode("utf-8").strip())
        base = commit.value
    else:
        # A repository with no commit yet: everything git tracks is a change
        # from the empty tree.
        commit = None
        empty = _git(
            git, project.repository, "hash-object", "-t", "tree", "--stdin"
        )
        base = _succeeded(empty, project.repository, "hash-object").strip()
    return commit, _worktree_digest(git, project, base)


def _worktree_digest(git: str, project: BoundProject, base: str) -> Sha256Digest:
    """Hash the tracked files' changes from ``base`` and the untracked files.

    Each part is length-prefixed, so no two different states hash alike.
    """
    pathspec = (
        "--",
        ".",
        *(f":(exclude){path.as_posix()}" for path in project.excluded()),
    )
    tracked = _git(
        git,
        project.repository,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--no-renames",
        "--binary",
        "--full-index",
        "--submodule=diff",
        base,
        *pathspec,
    )
    _succeeded(tracked, project.repository, "diff")
    untracked = _git(
        git,
        project.repository,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        *pathspec,
    )
    _succeeded(untracked, project.repository, "ls-files")
    digest = hashlib.sha256()
    _feed(digest.update, b"tracked", tracked.stdout)
    for name in sorted(entry for entry in untracked.stdout.split(b"\0") if entry):
        _feed(digest.update, b"untracked", name, _untracked_content(project, name))
    return Sha256Digest(digest.hexdigest())


def _untracked_content(project: BoundProject, name: bytes) -> bytes:
    """Return what git would store for one untracked entry."""
    path = project.repository / os.fsdecode(name)
    if path.is_symlink():
        return os.fsencode(path.readlink())
    if path.is_dir():
        # A nested repository git lists as one entry: its path is what shows.
        return b""
    return path.read_bytes()


def _feed(update: Callable[[bytes], None], *parts: bytes) -> None:
    for part in parts:
        update(len(part).to_bytes(8, "big"))
        update(part)


def _git(
    git: str, repository: Path, *arguments: str
) -> subprocess.CompletedProcess[bytes]:
    """Run one git command in the project, with empty input.

    Its output is kept as bytes: a diff or an untracked path need not be
    UTF-8, and each text answer is decoded as UTF-8 where it is read. The
    project's own git config does not get to run a command here: its
    ``core.fsmonitor`` hook is turned off, as are external diff and
    textconv drivers in the diff itself.
    """
    try:
        return subprocess.run(  # noqa: S603 - resolved binary, built argv.
            (git, "-c", "core.fsmonitor=false", "-C", str(repository), *arguments),
            check=False,
            capture_output=True,
            input=b"",
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        message = f"git {arguments[0]} in {repository} timed out: {error}"
        raise BindingError(_ERROR_GIT_FAILED, message) from error


def _succeeded(
    completed: subprocess.CompletedProcess[bytes], repository: Path, name: str
) -> str:
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        message = (
            f"git {name} in {repository} failed with exit code "
            f"{completed.returncode}: {stderr}"
        )
        raise BindingError(_ERROR_GIT_FAILED, message)
    return completed.stdout.decode("utf-8")
