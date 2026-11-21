"""The release flow writes the interface changelog that `cloudfall changelog` serves.

S-017-15 is seen whole on the first release after it ships; these tests hold the
parts the repository decides: the command the release stamp runs, where its files
land, that the wheel carries them, and that no person has to run it.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

from cloudfall.app import app

ROOT = Path(__file__).parents[2]
CHANGELOG = ROOT / "sdk" / "src" / "cloudfall" / "schema-changelog.json"
# `treaty changelog-add` keeps the manifest it diffs against beside the changelog
SNAPSHOT = CHANGELOG.with_name(f"{app.name}.manifest.json")


def _releases_section() -> str:
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    return text.split("\n## Releases\n", 1)[1].split("\n## ", 1)[0]


RESTORE = "uv run --frozen --no-dev python scripts/restore_interface_snapshot.py"
CHANGELOG_ADD = "uv run --frozen --no-dev treaty changelog-add cloudfall.app:app"


def _after_stamp() -> list[list[str]]:
    policy = tomllib.loads(
        (ROOT / ".github" / "shipmill.toml").read_text(encoding="utf-8")
    )
    # shipmill splits each entry with shlex and runs it without a shell
    return [shlex.split(command) for command in policy["after_stamp"]]


def _run_with_stub_uv(
    command: list[str], tree: Path
) -> subprocess.CompletedProcess[str]:
    """Run an after_stamp entry in tree, with a uv on PATH that logs its arguments."""
    bin_dir = tree / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "uv"
    stub.write_text('#!/bin/sh\necho "$@" > uv-args\nexit 7\n', encoding="utf-8")
    stub.chmod(0o755)
    return subprocess.run(  # noqa: S603 - an after_stamp entry of this repository
        command,
        cwd=tree,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        check=False,
        encoding="utf-8",
    )


def test_s017_15_the_release_stamp_records_the_interface_changes(
    tmp_path: Path,
) -> None:
    commands = _after_stamp()

    # the lock is checked first, so the changelog is written by the stamped version;
    # then the previous release's snapshot is put back for changelog-add to diff
    # against, since release commits are not on main (#457)
    assert commands[0] == ["uv", "lock", "--check"]
    assert len(commands) == 3
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "restore_interface_snapshot.py").write_text(
        "", encoding="utf-8"
    )
    for command, expected in zip(commands[1:], (RESTORE, CHANGELOG_ADD), strict=True):
        done = _run_with_stub_uv(command, tmp_path)
        # on a tree with the script the step runs, and its failure stops the release
        assert done.returncode == 7, done.stderr
        ran = (tmp_path / "uv-args").read_text(encoding="utf-8").strip()
        assert f"uv {ran}" == expected


# D-29: a hotfix from a line that predates the interface changelog (0.7.x)
def test_d29_a_tree_without_the_restore_script_skips_both_steps_with_a_notice(
    tmp_path: Path,
) -> None:
    for command in _after_stamp()[1:]:
        done = _run_with_stub_uv(command, tmp_path)

        assert done.returncode == 0, done.stderr
        assert "notice: no scripts/restore_interface_snapshot.py" in done.stderr
        assert "(D-29)" in done.stderr
        assert not (tmp_path / "uv-args").exists()


def test_s017_15_cloudfall_changelog_serves_the_file_the_release_writes() -> None:
    assert app.schema_changelog == CHANGELOG


def test_s017_15_the_wheel_carries_the_changelog_and_its_snapshot() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    git = shutil.which("git")
    assert git is not None

    for path in (CHANGELOG, SNAPSHOT):
        assert path.parent.relative_to(ROOT).as_posix() in packages
        # hatchling leaves out what git ignores; exit 1 is "not ignored"
        ignored = subprocess.run(  # noqa: S603 - git, fixed arguments
            [git, "-C", str(ROOT), "check-ignore", "--no-index", "-q", str(path)],
            check=False,
        )
        assert ignored.returncode == 1, path


def test_s017_15_the_releases_section_names_no_manual_changelog_step() -> None:
    section = _releases_section()

    assert "treaty changelog-add cloudfall.app:app" in section
    assert "after_stamp" in section
    assert "scripts/restore_interface_snapshot.py" in section
    assert "did not change adds no entry" in section
    assert "```" not in section
