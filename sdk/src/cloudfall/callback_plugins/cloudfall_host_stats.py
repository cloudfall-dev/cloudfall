"""Each host's play recap, one JSON file per host, for the decision record.

Whether a run changed a host is a fact of the whole play, not of its last
task, so it is read from the play stats the recap prints rather than from
per-task results. Ansible loads this file itself, into ``ansible-playbook``;
it imports nothing from the SDK, so it works whichever interpreter runs it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ansible.plugins.callback import CallbackBase

if TYPE_CHECKING:
    from ansible.executor.stats import AggregateStats

DOCUMENTATION = """
name: cloudfall_host_stats
type: aggregate
short_description: Write each host's play recap counts as JSON
description:
  - When the playbook ends, writes one JSON file per host, named after the
    host, holding its ok, changed, failed and unreachable counts.
requirements:
  - enabled in callbacks_enabled
options:
  directory:
    description: The directory the per-host files are written to.
    required: true
    type: path
    env:
      - name: CLOUDFALL_HOST_STATS_DIR
"""

# What each written key is called in the stats' own summary.
_COUNTS = {
    "ok": "ok",
    "changed": "changed",
    "failed": "failures",
    "unreachable": "unreachable",
}


# ansible-core ships no type information, so its base class is Any to mypy.
class CallbackModule(CallbackBase):  # type: ignore[misc]
    """Write the recap counts of every host the play touched."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "cloudfall_host_stats"
    CALLBACK_NEEDS_ENABLED = True

    def v2_playbook_on_stats(self, stats: AggregateStats) -> None:
        """Write one file per host once the play's stats are final."""
        directory = Path(str(self.get_option("directory")))
        directory.mkdir(parents=True, exist_ok=True)
        for host in sorted(stats.processed):
            summary = stats.summarize(host)
            counts = {key: int(summary[name]) for key, name in _COUNTS.items()}
            (directory / host).write_text(
                json.dumps(counts, sort_keys=True), encoding="utf-8"
            )
