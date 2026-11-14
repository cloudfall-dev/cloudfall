"""The Grafana dashboards this release bundles (S-016), by file name and sha256.

The backend role copies them to the logging backend; ``audit`` compares what
``inspect`` hashed there with these.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from cloudfall.resources import default_engine_directory

DASHBOARDS_DIRECTORY = Path(
    "ansible", "roles", "cloudfall_logging_backend", "files", "dashboards"
)


def bundled_dashboards(engine: Path | None = None) -> dict[str, str]:
    """Return each bundled dashboard's file name and sha256."""
    root = (engine if engine is not None else default_engine_directory()) / (
        DASHBOARDS_DIRECTORY
    )
    files = sorted(root.glob("*.json"))
    if not files:
        message = f"no bundled dashboards under {root}"
        raise FileNotFoundError(message)
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
