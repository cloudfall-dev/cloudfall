"""Copy the example project into a test's temporary directory.

The Taskfile and CI render inventory and observations into the examples'
own ``tmp/`` folder, so a plain ``shutil.copytree`` of the examples carries
that output into every test project and breaks the tests that check no
engine step ran (#226). Every test that copies the examples goes through
``copy_examples``, and ``test_examples_project`` holds them to it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "config" / "examples"
OUTPUT = "tmp"


def copy_examples(destination: Path, source: Path = EXAMPLES) -> Path:
    """Copy ``source`` to ``destination`` without its top-level ``tmp/``."""

    def _skip_output(directory: str, names: list[str]) -> set[str]:
        if Path(directory) == source and OUTPUT in names:
            return {OUTPUT}
        return set()

    shutil.copytree(source, destination, ignore=_skip_output)
    return destination
