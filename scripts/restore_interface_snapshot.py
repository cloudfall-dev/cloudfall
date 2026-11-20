"""Put back the interface snapshot the previous release left, before changelog-add.

`treaty changelog-add cloudfall.app:app` diffs the manifest against the snapshot
beside `sdk/src/cloudfall/schema-changelog.json`. Both files are written into the
release commit, which shipmill does not merge back to main, so a release starting
from main would find neither and list the whole interface as added (#457). This
step, run by `after_stamp` in `.github/shipmill.toml` before changelog-add, writes
both files as the previous release's tag holds them.

The previous release is the newest release tag, by version, that the commit being
released builds on: the tag's commit is in HEAD's history (a stable release on
main, a hotfix's release/X.Y), or the commit it was cut from is (an rc, whose
release commit sits off main). An rc from main so gets the newest rc, and a hotfix
the newest release of its branch, never a newer rc from main. With no such tag, or
a tag without both files, nothing is restored and changelog-add seeds the files.

Run from the repository root: `uv run --frozen --no-dev python
scripts/restore_interface_snapshot.py`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "sdk/src/cloudfall"
FILES = (f"{PACKAGE}/cloudfall.manifest.json", f"{PACKAGE}/schema-changelog.json")
# The tags shipmill gives this project's releases: vX.Y.Z and vX.Y.ZrcN
_NUMBER = r"(0|[1-9]\d*)"
_RELEASE_TAG = re.compile(rf"v{_NUMBER}\.{_NUMBER}\.{_NUMBER}(?:rc{_NUMBER})?")


@dataclass(frozen=True, slots=True, order=True)
class ReleaseTag:
    """A release tag, ordered by version: an rc sorts before its release (PEP 440)."""

    key: tuple[int, int, int, int, int]
    name: str

    @classmethod
    def parse(cls, name: str) -> ReleaseTag | None:
        """Read a tag name; None for a tag of another form."""
        found = _RELEASE_TAG.fullmatch(name)
        if found is None:
            return None
        major, minor, patch, rc = found.groups()
        final = 1 if rc is None else 0
        return cls((int(major), int(minor), int(patch), final, int(rc or 0)), name)


class Git:
    """The git repository at root."""

    def __init__(self, root: Path) -> None:
        """Find git on PATH; fail when it is missing."""
        found = shutil.which("git")
        if found is None:
            message = "git is not on PATH"
            raise RuntimeError(message)
        self.executable = found
        self.root = root

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(  # noqa: S603 - git with arguments this script builds
            [self.executable, *arguments],
            cwd=self.root,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def _failed(
        arguments: tuple[str, ...], done: subprocess.CompletedProcess[bytes]
    ) -> RuntimeError:
        stderr = done.stderr.decode("utf-8", errors="replace").strip()
        command = " ".join(arguments)
        return RuntimeError(f"git {command} exited {done.returncode}: {stderr}")

    def tags(self) -> list[str]:
        """List every tag name."""
        arguments = ("tag", "--list")
        done = self._run(*arguments)
        if done.returncode != 0:
            raise self._failed(arguments, done)
        return done.stdout.decode("utf-8").split()

    def first_parent(self, tag: str) -> str | None:
        """Name the commit the tag's commit was made on; None for a root commit."""
        done = self._run("rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}^1")
        if done.returncode != 0:
            return None
        return done.stdout.decode("utf-8").strip()

    def is_ancestor_of_head(self, revision: str) -> bool:
        """Tell whether revision is in HEAD's history."""
        arguments = ("merge-base", "--is-ancestor", revision, "HEAD")
        done = self._run(*arguments)
        if done.returncode not in {0, 1}:
            raise self._failed(arguments, done)
        return done.returncode == 0

    def show(self, tag: str, path: str) -> bytes | None:
        """Read the file at path in tag; None when the tag has no such file."""
        if self._run("cat-file", "-e", f"{tag}:{path}").returncode != 0:
            return None
        arguments = ("show", f"{tag}:{path}")
        done = self._run(*arguments)
        if done.returncode != 0:
            raise self._failed(arguments, done)
        return done.stdout


def previous_release(git: Git) -> str | None:
    """Find the newest release tag HEAD builds on; None when there is none."""
    releases = sorted(
        (tag for name in git.tags() if (tag := ReleaseTag.parse(name)) is not None),
        reverse=True,
    )
    for tag in releases:
        if git.is_ancestor_of_head(f"{tag.name}^{{commit}}"):
            return tag.name
        parent = git.first_parent(tag.name)
        if parent is not None and git.is_ancestor_of_head(parent):
            return tag.name
    return None


def restore(root: Path) -> str:
    """Write the files the previous release left under root; say what was done."""
    git = Git(root)
    tag = previous_release(git)
    if tag is None:
        return "no earlier release tag: nothing restored, changelog-add seeds the files"
    found: list[tuple[str, bytes]] = []
    missing: list[str] = []
    for path in FILES:
        content = git.show(tag, path)
        if content is None:
            missing.append(path)
        else:
            found.append((path, content))
    if missing:
        return f"{tag} has no {', '.join(missing)}: nothing restored"
    for path, content in found:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
    return f"restored {', '.join(FILES)} from {tag}"


def main() -> int:
    """Restore into the working directory and report on stderr."""
    sys.stderr.write(f"restore_interface_snapshot: {restore(Path.cwd())}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
