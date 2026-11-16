"""The release puts back the interface snapshot the previous release left (#457).

`treaty changelog-add` diffs the manifest against the snapshot beside
`schema-changelog.json`. Both files live only in release commits, which are not
on main, so `scripts/restore_interface_snapshot.py` reads them from the previous
release's tag before the release runs `changelog-add`. These tests build real git
repositories shaped as shipmill leaves them: an rc commit off main, a stable
release on main or off it, a hotfix on release/X.Y.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "restore_interface_snapshot.py"
PACKAGE = Path("sdk") / "src" / "cloudfall"
SNAPSHOT = PACKAGE / "cloudfall.manifest.json"
CHANGELOG = PACKAGE / "schema-changelog.json"
GIT = shutil.which("git")
TREATY = shutil.which("treaty", path=str(Path(sys.executable).parent))


class Repository:
    """A git repository in a temporary directory, isolated from the user's config."""

    def __init__(self, path: Path) -> None:
        """Create the repository at path with one commit on main."""
        assert GIT is not None
        self.path = path
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(path.parent),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Cloudfall Test",
            "GIT_AUTHOR_EMAIL": "test@example.test",
            "GIT_COMMITTER_NAME": "Cloudfall Test",
            "GIT_COMMITTER_EMAIL": "test@example.test",
        }
        path.mkdir()
        self.git("init", "--quiet", "--initial-branch=main")
        self.commit("start", {"README.md": "start\n"})

    def git(self, *arguments: str) -> str:
        """Run git in the repository; return its output."""
        assert GIT is not None
        done = subprocess.run(  # noqa: S603 - git, fixture repository
            [GIT, *arguments],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            check=True,
            encoding="utf-8",
        )
        return done.stdout

    def write(self, files: dict[Path | str, str]) -> None:
        """Write the files into the working tree."""
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def commit(self, message: str, files: dict[Path | str, str]) -> None:
        """Write the files and commit them."""
        self.write(files)
        self.git("add", "--all")
        self.git("commit", "--quiet", "--message", message)

    def release(self, tag: str, files: dict[Path | str, str]) -> None:
        """Tag a release commit on HEAD, off its branch, as shipmill's rc lane does."""
        branch = self.git("branch", "--show-current").strip()
        self.git("checkout", "--quiet", "--detach")
        self.commit(f"Release {tag}", files)
        self.git("tag", "--annotate", tag, "--message", tag)
        self.git("checkout", "--quiet", branch)

    def restore(self) -> subprocess.CompletedProcess[str]:
        """Run the script under test in the repository."""
        return subprocess.run(  # noqa: S603 - the script under test
            [sys.executable, str(SCRIPT)],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            check=False,
            encoding="utf-8",
        )

    def read(self, name: Path) -> str | None:
        """Read a file of the working tree; None when it is missing."""
        target = self.path / name
        return target.read_text(encoding="utf-8") if target.is_file() else None


def _files(label: str) -> dict[Path | str, str]:
    return {SNAPSHOT: f"snapshot {label}\n", CHANGELOG: f"changelog {label}\n"}


def test_an_rc_from_main_restores_the_newest_release_by_version(
    tmp_path: Path,
) -> None:
    repository = Repository(tmp_path / "repository")
    # a stable release placed on main, as v0.7.0 is, from before the snapshot
    repository.git("tag", "--annotate", "v0.7.0", "--message", "v0.7.0")
    repository.release("v0.8.0rc2", _files("rc2"))
    repository.commit("work", {"a.txt": "a\n"})
    repository.release("v0.8.0rc10", _files("rc10"))
    repository.commit("more work", {"b.txt": "b\n"})
    # tags of another form are not releases of this project
    repository.release("v9.9.9-test", _files("other"))
    repository.git("tag", "--annotate", "latest", "--message", "latest")

    done = repository.restore()

    assert done.returncode == 0, done.stderr
    # v0.8.0rc10 is newer than v0.8.0rc2 by version, though not by spelling
    assert repository.read(SNAPSHOT) == "snapshot rc10\n"
    assert repository.read(CHANGELOG) == "changelog rc10\n"
    assert "v0.8.0rc10" in done.stderr


def test_a_hotfix_restores_the_newest_release_on_its_branch(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "repository")
    repository.release("v0.8.0rc1", _files("0.8.0rc1"))
    repository.release("v0.8.0", _files("0.8.0"))
    repository.git("branch", "release/0.8", "v0.8.0")
    repository.commit("main moves on", {"a.txt": "a\n"})
    # newer by version, but released from main after release/0.8 was cut
    repository.release("v0.9.0rc1", _files("0.9.0rc1"))
    repository.git("checkout", "--quiet", "release/0.8")
    repository.commit("the hotfix", {"fix.txt": "fix\n"})

    done = repository.restore()

    assert done.returncode == 0, done.stderr
    assert repository.read(SNAPSHOT) == "snapshot 0.8.0\n"
    assert repository.read(CHANGELOG) == "changelog 0.8.0\n"


def test_no_earlier_release_restores_nothing(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "repository")

    done = repository.restore()

    assert done.returncode == 0, done.stderr
    assert repository.read(SNAPSHOT) is None
    assert repository.read(CHANGELOG) is None
    assert "no earlier release" in done.stderr
    assert repository.git("status", "--porcelain") == ""


def test_a_release_without_the_files_restores_nothing(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "repository")
    repository.release("v0.8.0rc1", _files("rc1"))
    # the newest release has the changelog but lost its snapshot
    repository.release("v0.8.0rc2", {CHANGELOG: "changelog rc2\n"})

    done = repository.restore()

    assert done.returncode == 0, done.stderr
    assert repository.read(SNAPSHOT) is None
    assert repository.read(CHANGELOG) is None
    assert "v0.8.0rc2" in done.stderr
    assert str(SNAPSHOT.as_posix()) in done.stderr


# The app `treaty changelog-add` reads in the S-017-15 test: named cloudfall, so its
# snapshot is cloudfall.manifest.json, with its version in a file a release stamps.
_APP = """\
from dataclasses import dataclass
from pathlib import Path

from treaty import App, Ctx

HERE = Path(__file__).parent
app = App(
    "cloudfall",
    version=(HERE / "VERSION").read_text(encoding="utf-8").strip(),
    schema_changelog=HERE / "schema-changelog.json",
)


@dataclass(frozen=True, slots=True)
class Args:
    pass


@dataclass(frozen=True, slots=True)
class Out:
    text: str


@app.command("hello", description="Say hello", exit_codes=(), danger_level="safe")
def hello(args: Args, ctx: Ctx) -> Out:
    return Out("hello")
"""

_BYE = """

@app.command("bye", description="Say goodbye", exit_codes=(), danger_level="safe")
def bye(args: Args, ctx: Ctx) -> Out:
    return Out("bye")
"""


def _stamp(repository: Repository, version: str) -> None:
    """Release version from HEAD as shipmill does, and tag it.

    The release stamps the version, then runs the after_stamp steps of
    .github/shipmill.toml past `uv lock --check`, then commits what they wrote.
    """
    assert TREATY is not None
    repository.git("checkout", "--quiet", "--detach")
    repository.write({PACKAGE / "VERSION": f"{version}\n"})
    restored = repository.restore()
    assert restored.returncode == 0, restored.stderr
    added = subprocess.run(  # noqa: S603 - treaty from this environment
        [TREATY, "changelog-add", "relapp:app"],
        cwd=repository.path / PACKAGE,
        env=repository.env,
        capture_output=True,
        check=False,
        encoding="utf-8",
    )
    assert added.returncode == 0, added.stdout + added.stderr
    repository.git("add", "--all")
    repository.git("commit", "--quiet", "--message", f"Release {version}")
    repository.git("tag", "--annotate", f"v{version}", "--message", version)
    repository.git("checkout", "--quiet", "main")


def _entries(repository: Repository, tag: str) -> list[dict[str, object]]:
    text = repository.git("show", f"{tag}:{CHANGELOG.as_posix()}")
    entries = json.loads(text)
    assert isinstance(entries, list)
    return entries


# proves: S-017-15
def test_s017_15_an_unchanged_interface_adds_no_entry(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "repository")
    repository.commit(
        "the app", {PACKAGE / "relapp.py": _APP, PACKAGE / "VERSION": "0.1.0\n"}
    )

    _stamp(repository, "0.1.0rc1")
    repository.commit("no interface change", {"a.txt": "a\n"})
    _stamp(repository, "0.1.0rc2")
    # a stable release promoted from the last rc, from main's commit
    _stamp(repository, "0.1.0")

    first = _entries(repository, "v0.1.0rc1")
    assert [entry["version"] for entry in first] == ["0.1.0-rc.1"]
    assert _entries(repository, "v0.1.0rc2") == first
    assert _entries(repository, "v0.1.0") == first

    repository.commit("a new command", {PACKAGE / "relapp.py": _APP + _BYE})
    _stamp(repository, "0.2.0rc1")

    latest, *older = _entries(repository, "v0.2.0rc1")
    assert older == first
    assert latest["version"] == "0.2.0-rc.1"
    # only the new command and its fields, not the whole interface again
    added = latest["added"]
    assert isinstance(added, list)
    assert "bye" in added
    assert all(path == "bye" or str(path).startswith("bye.") for path in added)
    assert latest["removed"] == []
    assert latest["breaking"] is False
