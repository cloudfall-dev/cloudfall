"""Every service role creates the shared backup root before its own directory.

Left to ``ansible.builtin.file`` on the leaf, the missing root takes the
first service's owner and mode, so on ``h1`` of ``config/examples`` (a
PostgreSQL and a Redis service) the Redis backup could not reach its own
directory (#342).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).parents[2]
ROLES = ROOT / "engine" / "ansible" / "roles"
SERVICES = ROOT / "config" / "examples" / "services"
BACKUP_ROOT = "/var/backups/cloudfall"
CREATE_ROOT = "Create the backup root"
# Each role, the task file holding its backup directory, and that task.
ROLE_TASKS = {
    "postgresql": ("main.yml", "Create the backup directory"),
    "redis": ("main.yml", "Create the backup directory"),
    "elasticsearch": ("snapshots.yml", "Create the snapshot repository directory"),
}
# Each service's own directory keeps its owner and mode.
SERVICE_OWNERS = {
    "postgresql": ("postgres", "postgres", "0700"),
    "redis": ("redis", "redis", "0700"),
}


def _load(path: Path) -> object:
    return DataLoader().load_from_file(str(path))


def _tasks(kind: str) -> list[dict[str, object]]:
    tasks = _load(ROLES / f"cloudfall_{kind}" / "tasks" / ROLE_TASKS[kind][0])
    assert isinstance(tasks, list)
    return tasks


def _variables(kind: str, directory: str) -> dict[str, object]:
    defaults = _load(ROLES / f"cloudfall_{kind}" / "defaults" / "main.yml")
    assert isinstance(defaults, dict)
    return {
        f"cloudfall_{kind}_backup_root": defaults[f"cloudfall_{kind}_backup_root"],
        f"cloudfall_{kind}_service": {"backup": {"directory": directory}},
    }


def _render(value: object, variables: dict[str, object]) -> object:
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(str(value)))


def _holds(expression: object, variables: dict[str, object]) -> bool:
    templar = Templar(loader=DataLoader(), variables=variables)
    return bool(templar.evaluate_conditional(trust_as_template(str(expression))))


def _created(kind: str, directory: str) -> list[tuple[str, str, str, str]]:
    """Answer the directories the role's backup tasks create, in order."""
    variables = _variables(kind, directory)
    created: list[tuple[str, str, str, str]] = []
    for task in _tasks(kind):
        module = task.get("ansible.builtin.file")
        if not isinstance(module, dict) or module.get("state") != "directory":
            continue
        if "when" in task and not _holds(task["when"], variables):
            continue
        created.append(
            (
                str(_render(module["path"], variables)),
                str(module["owner"]),
                str(module["group"]),
                str(module["mode"]),
            )
        )
    return created


def _example_host_services() -> dict[str, str]:
    """Map each service kind on h1 of config/examples to its backup directory."""
    services: dict[str, str] = {}
    for path in sorted(SERVICES.glob("*.yaml")):
        spec = yaml.safe_load(path.read_text())["spec"]
        if spec["server"] == "h1":
            services[spec["serviceKind"]] = spec["backup"]["directory"]
    return services


def test_the_example_host_runs_postgresql_and_redis_under_the_backup_root() -> None:
    services = _example_host_services()

    assert {"postgresql", "redis"} <= services.keys()
    assert all(
        directory.startswith(f"{BACKUP_ROOT}/") for directory in services.values()
    )


@pytest.mark.parametrize("kind", ["postgresql", "redis"])
def test_each_service_on_one_host_gets_a_root_owned_root_first(kind: str) -> None:
    directory = _example_host_services()[kind]

    created = _created(kind, directory)

    root = (BACKUP_ROOT, "root", "root", "0755")
    assert root in created
    (own,) = [entry for entry in created if entry[0] == directory]
    assert created.index(root) < created.index(own)
    assert own[1:] == SERVICE_OWNERS[kind]


def test_the_snapshot_repository_gets_a_root_owned_root_first() -> None:
    directory = f"{BACKUP_ROOT}/search-main"

    created = _created("elasticsearch", directory)

    assert created == [
        (BACKUP_ROOT, "root", "root", "0755"),
        (directory, "elasticsearch", "elasticsearch", "0750"),
    ]


@pytest.mark.parametrize("kind", sorted(ROLE_TASKS))
def test_a_directory_outside_the_backup_root_leaves_it_alone(kind: str) -> None:
    created = _created(kind, "/srv/backups/main")

    assert BACKUP_ROOT not in [entry[0] for entry in created]
    assert "/srv/backups/main" in [entry[0] for entry in created]


@pytest.mark.parametrize("kind", sorted(ROLE_TASKS))
def test_the_root_task_comes_right_before_the_service_directory(kind: str) -> None:
    names = [str(task["name"]) for task in _tasks(kind)]

    assert names.index(CREATE_ROOT) + 1 == names.index(ROLE_TASKS[kind][1])
