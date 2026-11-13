"""Test projects copied from the examples leave the examples' tmp/ behind (#226)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from examples_project import ROOT, copy_examples

if TYPE_CHECKING:
    from pathlib import Path

TEST_FOLDERS = (ROOT / "sdk" / "tests", ROOT / "engine" / "tests")
DIRECT_COPY = re.compile(
    r'copytree\(\s*(?:EXAMPLES|ROOT\s*/\s*"config"\s*/\s*"examples")\s*,'
)


def test_copy_leaves_the_examples_output_folder_behind(tmp_path: Path) -> None:
    source = tmp_path / "examples"
    (source / "tmp" / "observed").mkdir(parents=True)
    (source / "tmp" / "ansible-inventory.json").write_text("{}", encoding="utf-8")
    (source / "servers" / "tmp").mkdir(parents=True)
    (source / "servers" / "h1.yaml").write_text("kind: Server\n", encoding="utf-8")

    project = copy_examples(tmp_path / "project", source)

    assert project == tmp_path / "project"
    assert not (project / "tmp").exists()
    assert (project / "servers" / "h1.yaml").is_file()
    # Only the examples' own output folder is skipped, not a nested tmp/.
    assert (project / "servers" / "tmp").is_dir()


def test_no_test_copies_the_examples_around_the_helper() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for folder in TEST_FOLDERS
        for path in sorted(folder.glob("*.py"))
        if DIRECT_COPY.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], (
        "copy the examples with examples_project.copy_examples so their "
        f"tmp/ stays behind: {offenders}"
    )
