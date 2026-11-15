"""Every remote download in the engine retries a bounded number of times (#372).

One slow TLS handshake failed a whole converge or deploy: a ``get_url`` task
must retry until it succeeds, a few times and with a pause, and still fail
once the retries run out.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator

ANSIBLE = Path(__file__).parents[2] / "engine" / "ansible"
GET_URL = ("ansible.builtin.get_url", "get_url")
NESTED = ("block", "rescue", "always")


def _task_files() -> list[Path]:
    return sorted(
        path
        for pattern in ("roles/*/tasks/*.yml", "roles/*/handlers/*.yml", "*.yml")
        for path in ANSIBLE.glob(pattern)
    )


def _tasks(entries: object) -> Iterator[dict[str, object]]:
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        yield entry
        for key in (*NESTED, "tasks", "pre_tasks", "post_tasks", "handlers"):
            yield from _tasks(entry.get(key))


def _downloads() -> list[tuple[str, dict[str, object]]]:
    return [
        (f"{path.relative_to(ANSIBLE)}: {task.get('name')}", task)
        for path in _task_files()
        for task in _tasks(yaml.safe_load(path.read_text(encoding="utf-8")))
        if any(module in task for module in GET_URL)
    ]


DOWNLOADS = _downloads()


def test_the_engine_has_downloads_to_check() -> None:
    names = [name for name, _ in DOWNLOADS]

    assert any("Download the pinned uv archive" in name for name in names), names
    assert len(DOWNLOADS) >= 5


@pytest.mark.parametrize(
    "task", [task for _, task in DOWNLOADS], ids=[name for name, _ in DOWNLOADS]
)
def test_every_get_url_retries_a_bounded_number_of_times(
    task: dict[str, object],
) -> None:
    registered = task.get("register")

    assert isinstance(registered, str)
    assert task.get("until") == f"{registered} is succeeded"
    assert cast("int", task.get("retries", 0)) >= 3
    assert cast("int", task.get("delay", 0)) > 0
