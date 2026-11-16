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


def test_s017_15_the_release_stamp_records_the_interface_changes() -> None:
    policy = tomllib.loads(
        (ROOT / ".github" / "shipmill.toml").read_text(encoding="utf-8")
    )
    commands = [shlex.split(command) for command in policy["after_stamp"]]

    # the lock is checked first, so the changelog is written by the stamped version
    assert commands[0] == ["uv", "lock", "--check"]
    assert commands[1:] == [
        [
            "uv",
            "run",
            "--frozen",
            "--no-dev",
            "treaty",
            "changelog-add",
            "cloudfall.app:app",
        ]
    ]


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
    assert "```" not in section
