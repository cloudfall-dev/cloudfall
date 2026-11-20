"""Every role's handlers load as handlers, the task files they include too.

#495. Ansible loads a handler, and every task file a handler includes, with
its handler loader, which refuses a block anywhere below a file's top level
("Using a block as a handler is not supported."). The error surfaces only
when a converge notifies the handler, so the files are loaded here with
Ansible's own loader, as a notified handler loads them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.playbook.helpers import load_list_of_blocks
from ansible.playbook.play import Play
from ansible.plugins.loader import init_plugin_loader
from ansible.vars.manager import VariableManager

ROLES = Path(__file__).parents[2] / "engine" / "ansible" / "roles"
INCLUDES = (
    "ansible.builtin.include_tasks",
    "ansible.builtin.import_tasks",
    "include_tasks",
    "import_tasks",
)

init_plugin_loader()


def _included(task: dict[str, Any]) -> list[str]:
    """Return the task files a task includes, those of its blocks too."""
    found: list[str] = []
    for key in INCLUDES:
        if key in task:
            target = task[key]
            found.append(target["file"] if isinstance(target, dict) else target)
    for section in ("block", "rescue", "always"):
        for nested in task.get(section, []):
            found.extend(_included(nested))
    return found


def _handler_files(role: Path) -> list[Path]:
    """Return the role's handler file and every task file it includes."""
    pending = [role / "handlers" / "main.yml"]
    seen: list[Path] = []
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.append(path)
        for task in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            for target in _included(task):
                resolved = target.replace("{{ role_path }}", str(role))
                assert "{{" not in resolved, f"{path}: can't resolve {target}"
                candidate = Path(resolved)
                if not candidate.is_absolute():
                    candidate = role / "tasks" / candidate
                assert candidate.is_file(), f"{path}: {target} is not a file"
                pending.append(candidate)
    return seen


ROLES_WITH_HANDLERS = sorted(
    role.name for role in ROLES.iterdir() if (role / "handlers" / "main.yml").is_file()
)


@pytest.mark.parametrize("role", ROLES_WITH_HANDLERS)
def test_a_notified_handler_loads_every_file_it_runs(role: str) -> None:
    loader = DataLoader()
    play = Play.load({"hosts": "all", "gather_facts": False}, loader=loader)

    for path in _handler_files(ROLES / role):
        load_list_of_blocks(
            loader.load_from_file(str(path)),
            play=play,
            use_handlers=True,
            loader=loader,
            variable_manager=VariableManager(loader=loader),
        )


def test_the_elasticsearch_restart_handler_runs_its_node_start_file() -> None:
    """The check above covers the files a converge loads on a restart."""
    names = {path.name for path in _handler_files(ROLES / "cloudfall_elasticsearch")}

    assert {"main.yml", "restart.yml", "node-stop.yml", "node-start.yml"} <= names
