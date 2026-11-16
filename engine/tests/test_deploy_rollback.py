"""A deploy's dry run and what a rollback restores (#223).

The role needs a Debian host with systemd, so these tests take the routes the
other deploy tests take. ``deploy.yml --check`` and the role's ``rollback``
tasks run for real under ``ansible-playbook`` against this controller, with
stand-ins: a facts module that reports a Debian x86_64 systemd host,
``systemctl`` first on ``PATH``, and ``systemd-run`` swapped through the
role's ``cloudfall_deploy_systemd_run`` variable. The gate's rescue needs root
and a real systemd, so ``test_deploy_role.py`` replays it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.validation import SchemaCatalog
from cloudfall_engine.playbook import PlaybookRun, playbook_command, resolve_playbook
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ENGINE = ROOT / "engine"
ROLE = ENGINE / "ansible" / "roles" / "cloudfall_deploy"
COMPONENT_ID = "crm-backend"
PREVIOUS = "20261027T000000Z-abcdef1"
RELEASE = "20261028T000000Z-abcdef2"
REDEPLOYED = f"{RELEASE}.redeploy-20261113T101500Z"
RECEIPT = "Write the release receipt on the controller"
RECORD_ENTRY = "Record the server's entry in the run's release receipt"

FACTS_MODULE = """#!/usr/bin/python
# Stand-in fact gathering: the host the role validates against.
import json

print(json.dumps({"changed": False, "ansible_facts": {
    "distribution": "Debian",
    "service_mgr": "systemd",
    "architecture": "x86_64",
}}))
"""

SYSTEMCTL = """#!/bin/sh
# Stand-in for systemctl: logs its arguments and answers for a loaded,
# running unit, or, with no "$0.loaded" file, as systemd answers for a unit
# that has no unit file.
echo "$@" >> "$0.log"
if [ ! -e "$0.loaded" ]; then
  case "$1" in
    show) printf 'ActiveState=inactive\\nLoadState=not-found\\nSubState=dead\\n' ;;
    is-enabled) echo not-found; exit 1 ;;
    list-unit-files) exit 1 ;;
  esac
  exit 0
fi
case "$1" in
  show) printf 'ActiveState=active\\nLoadState=loaded\\nSubState=running\\n' ;;
  is-enabled) echo enabled ;;
esac
exit 0
"""

SYSTEMD_RUN = """#!/bin/sh
# Stand-in for systemd-run --wait: chdir like --working-directory, then run
# the argv after "--" and pass on its exit code.
directory=
while [ "$1" != "--" ]; do
  case "$1" in
    --working-directory=*) directory="${1#--working-directory=}" ;;
  esac
  shift
done
shift
cd "$directory" || exit 200
"$@"
"""

SMOKE = """#!/bin/sh
# Passes in a release directory that holds a "healthy" file.
[ -f healthy ]
"""


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


@dataclass(frozen=True, slots=True)
class _Host:
    """A stand-in server: this controller with fake facts and ``systemctl``.

    ``unit_loaded`` says whether systemd knows the component's unit, as it
    does once a deploy has installed it; a first deploy's dry run writes no
    unit file, so systemd does not.
    """

    tmp_path: Path
    unit_loaded: bool = True
    retain: int | None = 5

    @property
    def install_root(self) -> Path:
        return self.tmp_path / "app"

    @property
    def releases(self) -> Path:
        return self.install_root / "releases"

    @property
    def current(self) -> Path:
        return self.install_root / "current"

    @property
    def environment(self) -> Path:
        return self.install_root / "shared" / "env"

    @property
    def unit_directory(self) -> Path:
        return self.tmp_path / "systemd"

    @property
    def unit(self) -> Path:
        return self.unit_directory / f"{COMPONENT_ID}.service"

    def environment_file(self, release: str | None, text: str) -> Path:
        """Write a release's own environment file, or the legacy one."""
        name = f"{COMPONENT_ID}.{release}.env" if release else f"{COMPONENT_ID}.env"
        path = self.environment / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def install_unit(self, environment: Path) -> None:
        """Install the managed unit a deploy wrote, naming its environment."""
        self.unit_directory.mkdir(exist_ok=True)
        self.unit.write_text(
            "# Managed by Cloudfall. Local changes will be overwritten.\n"
            "[Service]\n"
            f"WorkingDirectory={self.current}\n"
            f"EnvironmentFile=-{environment}\n"
            f"ExecStart={self.current}/.venv/bin/app\n",
            encoding="utf-8",
        )

    @property
    def unit_environment(self) -> Path:
        """The environment file the installed unit names."""
        (line,) = (
            line
            for line in self.unit.read_text(encoding="utf-8").splitlines()
            if line.startswith("EnvironmentFile=")
        )
        return Path(line.removeprefix("EnvironmentFile=-"))

    @property
    def systemctl_calls(self) -> list[str]:
        path = self.tmp_path / "bin" / "systemctl.log"
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    def release(self, name: str, *, healthy: bool = True) -> Path:
        directory = self.releases / name
        _executable(directory / "bin" / "smoke", SMOKE)
        if healthy:
            (directory / "healthy").write_text("", encoding="utf-8")
        return directory

    def component(self) -> dict[str, object]:
        component = self._component()
        if self.retain is None:
            del component["retainReleases"]
        return component

    def _component(self) -> dict[str, object]:
        return {
            "id": COMPONENT_ID,
            "application": "crm",
            # A user with a group of the same name on Linux and macOS; a dry
            # run only compares ownership, so it need not be this user.
            "application_user": "daemon",
            "install_root": str(self.install_root),
            "repository": {"url": "https://github.com/example/crm-backend.git"},
            "runtime": {"type": "python", "version": "3.14", "packageManager": "uv"},
            "service": {
                "manager": "systemd",
                "name": COMPONENT_ID,
                "command": [".venv/bin/app"],
            },
            "healthCheck": {
                "type": "command",
                "command": ["bin/smoke"],
                "timeoutSeconds": 60,
                "attempts": 1,
            },
            "retainUntilCleanup": True,
            "retainReleases": self.retain,
        }

    def run(
        self, playbook: Path, *, check: bool = False, extra: dict[str, object]
    ) -> tuple[int, str]:
        """Run a playbook against this host; answer its exit code and output.

        ``ansible_become: false`` stands in for the root a server's play
        takes; the controller here has no sudo.
        """
        library = self.tmp_path / "library"
        _executable(library / "cloudfall_test_facts.py", FACTS_MODULE)
        _executable(self.tmp_path / "bin" / "systemctl", SYSTEMCTL)
        if self.unit_loaded:
            (self.tmp_path / "bin" / "systemctl.loaded").write_text("", "utf-8")
        systemd_run = self.tmp_path / "bin" / "systemd-run"
        _executable(systemd_run, SYSTEMD_RUN)
        host = {
            "ansible_connection": "local",
            "ansible_python_interpreter": sys.executable,
            "ansible_become": False,
            "ansible_facts_modules": ["cloudfall_test_facts"],
            "cloudfall_server_id": "h1",
            "cloudfall_components": [self.component()],
        }
        # The component's group, as the rendered inventory has it: the deploy
        # playbook targets only the servers in component_<id>.
        component_group = f"component_{COMPONENT_ID.replace('-', '_')}"
        inventory = self.tmp_path / "inventory.json"
        inventory.write_text(
            json.dumps(
                {
                    "all": {
                        "children": {
                            "cloudfall_servers": {"hosts": {"h1": host}},
                            component_group: {"hosts": {"h1": {}}},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        variables = {
            "cloudfall_deploy_systemd_run": str(systemd_run),
            "cloudfall_deploy_unit_directory": str(self.unit_directory),
            **extra,
        }
        command = playbook_command(
            PlaybookRun(
                playbook, inventory, extra_vars=(json.dumps(variables),), check=check
            ),
            ENGINE,
        )
        done = subprocess.run(  # noqa: S603 - the engine's own argv
            command.argv,
            env={
                **os.environ,
                **command.environment,
                "ANSIBLE_LIBRARY": str(library),
                "PATH": f"{self.tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return done.returncode, f"{done.stdout}\n{done.stderr}"


# deploy.yml --check runs to its end and changes nothing.


def _dry_run(host: _Host) -> tuple[int, str]:
    archive = host.tmp_path / "artifact.tar.gz"
    archive.write_bytes(b"never transferred in a dry run")
    return host.run(
        resolve_playbook("deploy", ENGINE),
        check=True,
        extra={
            "cloudfall_deploy_component_id": COMPONENT_ID,
            "cloudfall_deploy_release": RELEASE,
            "cloudfall_deploy_artifact_archive": str(archive),
            "cloudfall_deploy_artifact_sha256": hashlib.sha256(
                archive.read_bytes()
            ).hexdigest(),
        },
    )


def test_a_dry_run_of_a_first_deploy_passes_and_creates_nothing(
    tmp_path: Path,
) -> None:
    host = _Host(tmp_path, unit_loaded=False)

    exit_code, output = _dry_run(host)

    assert exit_code == 0, output
    assert re.search(r"h1 +: ok=\d+ +changed=\d+ +unreachable=0 +failed=0", output)
    assert not host.install_root.exists()
    assert not any(" restart " in f" {call} " for call in host.systemctl_calls)


def test_a_dry_run_of_the_current_release_plans_a_fresh_directory(
    tmp_path: Path,
) -> None:
    host = _Host(tmp_path)
    live = host.release(RELEASE)
    host.current.symlink_to(live)
    before = sorted(path.name for path in live.rglob("*"))

    exit_code, output = _dry_run(host)

    assert exit_code == 0, output
    fresh = rf"\(item=[^)]*/releases/{RELEASE}\.redeploy-[0-9]{{8}}T[0-9]{{12}}Z\)"
    assert re.search(fresh, output), output
    assert f"/releases/{RELEASE})" not in output
    assert host.current.readlink() == live
    assert sorted(path.name for path in live.rglob("*")) == before
    assert [path.name for path in host.releases.iterdir()] == [RELEASE]


# A rollback takes the newest copy of a redeployed release.


def _rollback(host: _Host, release: str) -> tuple[int, str]:
    if not host.unit.exists():
        host.install_unit(host.environment / f"{COMPONENT_ID}.env")
    playbook = host.tmp_path / "rollback.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Roll back as the rollback playbook does",
                    "hosts": "cloudfall_servers",
                    "gather_facts": True,
                    "tasks": [
                        {
                            "name": "Run the role's rollback tasks",
                            "ansible.builtin.include_role": {
                                "name": "cloudfall_deploy",
                                "tasks_from": "rollback",
                            },
                            "vars": {
                                "cloudfall_deploy_component": (
                                    "{{ cloudfall_components | first }}"
                                ),
                            },
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    return host.run(playbook, extra={"cloudfall_deploy_release": release})


def test_a_rollback_to_a_redeployed_release_takes_its_newest_copy(
    tmp_path: Path,
) -> None:
    host = _Host(tmp_path)
    host.release(RELEASE, healthy=False)
    host.release(f"{RELEASE}.redeploy-20261101T000000Z", healthy=False)
    newest = host.release(REDEPLOYED)
    host.current.symlink_to(host.release("20261029T000000Z-abcdef3"))

    exit_code, output = _rollback(host, RELEASE)

    assert exit_code == 0, output
    assert host.current.readlink() == newest


def test_a_rollback_to_a_release_never_redeployed_takes_its_directory(
    tmp_path: Path,
) -> None:
    host = _Host(tmp_path)
    previous = host.release(PREVIOUS)
    host.current.symlink_to(host.release(RELEASE))

    exit_code, output = _rollback(host, PREVIOUS)

    assert exit_code == 0, output
    assert host.current.readlink() == previous


def test_a_rollback_to_a_kept_release_renders_no_refusal(tmp_path: Path) -> None:
    """The refusal's message, which names retainReleases, is only for a refusal.

    The rollback role's arguments require only the component, so a rollback
    to a release still on the server must not template what it would say.
    """
    host = _Host(tmp_path, retain=None)
    previous = host.release(PREVIOUS)
    host.current.symlink_to(host.release(RELEASE))

    exit_code, output = _rollback(host, PREVIOUS)

    assert exit_code == 0, output
    assert host.current.readlink() == previous


def test_a_rollback_to_a_missing_release_is_refused(tmp_path: Path) -> None:
    host = _Host(tmp_path)
    host.release(f"{PREVIOUS}0")
    live = host.release(RELEASE)
    host.current.symlink_to(live)

    exit_code, output = _rollback(host, PREVIOUS)

    assert exit_code != 0
    assert f"Release {PREVIOUS} does not exist under" in output
    # A release a deploy's cleanup removed is refused naming the kept ones.
    assert (
        "or a deploy removed it, keeping 5 releases (deployment.retainReleases);"
        f" the releases kept there are {PREVIOUS}0, {RELEASE}"
    ) in " ".join(output.split())
    assert host.current.readlink() == live


# A rollback restores the environment with the release (#353, D-21).


def test_a_rollback_points_the_unit_at_the_target_releases_environment(
    tmp_path: Path,
) -> None:
    host = _Host(tmp_path)
    previous = host.release(PREVIOUS)
    host.current.symlink_to(host.release(RELEASE))
    host.environment_file(PREVIOUS, "STATE=previous\n")
    host.install_unit(host.environment_file(RELEASE, "STATE=bad\n"))
    before = host.unit.read_text(encoding="utf-8")

    exit_code, output = _rollback(host, PREVIOUS)

    assert exit_code == 0, output
    assert host.current.readlink() == previous
    assert host.unit_environment == host.environment / f"{COMPONENT_ID}.{PREVIOUS}.env"
    assert host.unit_environment.read_text(encoding="utf-8") == "STATE=previous\n"
    # Only the EnvironmentFile= line changed.
    after = host.unit.read_text(encoding="utf-8").splitlines()
    pairs = zip(before.splitlines(), after, strict=True)
    changed = [old for old, new in pairs if old != new]
    assert [old.split("=")[0] for old in changed] == ["EnvironmentFile"]


def test_a_rollback_to_a_release_without_its_own_environment_names_the_legacy_one(
    tmp_path: Path,
) -> None:
    """A release deployed before per-release files reads the legacy file."""
    host = _Host(tmp_path)
    host.release(PREVIOUS)
    host.current.symlink_to(host.release(RELEASE))
    legacy = host.environment_file(None, "STATE=previous\n")
    host.install_unit(host.environment_file(RELEASE, "STATE=bad\n"))

    exit_code, output = _rollback(host, PREVIOUS)

    assert exit_code == 0, output
    assert host.unit_environment == legacy
    assert legacy.read_text(encoding="utf-8") == "STATE=previous\n"


def test_a_rollback_to_a_redeployed_release_reads_its_newest_copys_environment(
    tmp_path: Path,
) -> None:
    host = _Host(tmp_path)
    host.release(RELEASE)
    host.environment_file(RELEASE, "STATE=first\n")
    host.release(REDEPLOYED)
    host.environment_file(REDEPLOYED, "STATE=redeployed\n")
    host.current.symlink_to(host.release(PREVIOUS))
    host.install_unit(host.environment_file(PREVIOUS, "STATE=bad\n"))

    exit_code, output = _rollback(host, RELEASE)

    assert exit_code == 0, output
    assert host.unit_environment.read_text(encoding="utf-8") == "STATE=redeployed\n"


# A deploy whose new environment fails its gate leaves the previous release on
# its previous environment (#353).

BAD_ENVIRONMENT_TASKS = (
    "Inspect the currently active release",
    "Record the rollback candidate",
    "Compute release paths",
    "Compute the release's environment file paths",
    "Create the releases and shared roots",
    "Create release and shared directories",
    "Find the environment file the previous release reads",
    "Choose the environment file the release reads",
    "Install the release's environment file",
    "Carry the previous release's environment over to the release",
    "Remove an environment file left for the release by an earlier deploy",
    "Compute service unit paths",
    "Inspect the existing service unit",
    "Read the existing service unit",
    "Recognize a hand-made service unit",
    "Install the managed service unit",
)
BAD_ENVIRONMENT_RESCUE = (
    "Restore the previous release symlink",
    "Restore the service unit of the previous release",
    "Restart the restored release",
    "Remove the redeploy copy that failed its gate",
)


def _bad_environment_deploy(
    host: _Host, release: str, *, environment_text: str | None = "STATE=bad\n"
) -> tuple[int, str]:
    """Deploy a release, with a new environment by default, and fail its gate.

    The rescue then runs as the role's does.
    """
    environment = host.tmp_path / "controller" / "bad.env"
    if environment_text is not None:
        environment.parent.mkdir()
        environment.write_text(environment_text, encoding="utf-8")
    tasks = _main_tasks(BAD_ENVIRONMENT_TASKS)
    # A play outside the role finds the role's template by its path.
    template = tasks[-1]["ansible.builtin.template"]
    assert isinstance(template, dict)
    template["src"] = str(ROLE / "templates" / "component.service.j2")
    playbook = host.tmp_path / "bad-environment.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Deploy a release whose environment fails its gate",
                    "hosts": "cloudfall_servers",
                    "gather_facts": False,
                    "vars": {
                        "cloudfall_deploy_component": (
                            "{{ cloudfall_components | first }}"
                        ),
                        "cloudfall_deploy_release": release,
                        "cloudfall_deploy_environment_file": (
                            "" if environment_text is None else str(environment)
                        ),
                    },
                    "tasks": [
                        *tasks,
                        {
                            "name": "Activate the release behind the health gate",
                            "block": [
                                *_main_tasks(("Switch the current release symlink",)),
                                {
                                    "name": "Fail the health gate",
                                    "ansible.builtin.fail": {"msg": "unhealthy"},
                                },
                            ],
                            "rescue": _main_tasks(BAD_ENVIRONMENT_RESCUE),
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    return host.run(playbook, extra={})


@pytest.mark.parametrize("own", [True, False], ids=["own-file", "legacy-file"])
def test_a_failed_deploy_restores_the_previous_environment(
    own: bool,  # noqa: FBT001 - a parametrized case
    tmp_path: Path,
) -> None:
    """The previous release was deployed with its own file, or before them."""
    host = _Host(tmp_path)
    previous = host.release(PREVIOUS)
    host.current.symlink_to(previous)
    host.release(RELEASE)
    host.install_unit(
        host.environment_file(PREVIOUS if own else None, "STATE=previous\n")
    )
    before = host.unit.read_text(encoding="utf-8")

    exit_code, output = _bad_environment_deploy(host, RELEASE)

    assert exit_code == 0, output
    assert host.current.readlink() == previous
    assert host.unit.read_text(encoding="utf-8") == before
    assert host.unit_environment.read_text(encoding="utf-8") == "STATE=previous\n"
    # The failed release keeps its own file; the legacy one is never written.
    new = host.environment / f"{COMPONENT_ID}.{RELEASE}.env"
    assert new.read_text(encoding="utf-8") == "STATE=bad\n"
    assert own == (not (host.environment / f"{COMPONENT_ID}.env").exists())


def test_a_failed_redeploy_restores_the_previous_copys_environment(
    tmp_path: Path,
) -> None:
    """A redeploy of current's release gets a file of its own, then loses it."""
    host = _Host(tmp_path)
    live = host.release(RELEASE)
    host.current.symlink_to(live)
    host.install_unit(host.environment_file(RELEASE, "STATE=previous\n"))
    before = host.unit.read_text(encoding="utf-8")

    exit_code, output = _bad_environment_deploy(host, RELEASE)

    assert exit_code == 0, output
    assert host.current.readlink() == live
    assert host.unit.read_text(encoding="utf-8") == before
    assert host.unit_environment.read_text(encoding="utf-8") == "STATE=previous\n"
    assert sorted(path.name for path in host.environment.iterdir()) == [
        f"{COMPONENT_ID}.{RELEASE}.env"
    ]
    assert [path.name for path in host.releases.iterdir()] == [RELEASE]


@pytest.mark.parametrize("own", [True, False], ids=["own-file", "legacy-file"])
def test_a_deploy_without_an_environment_file_keeps_the_previous_one(
    own: bool,  # noqa: FBT001 - a parametrized case
    tmp_path: Path,
) -> None:
    """The release gets a copy of what the previous release reads, as its own."""
    host = _Host(tmp_path)
    previous = host.release(PREVIOUS)
    host.current.symlink_to(previous)
    host.release(RELEASE)
    previous_environment = host.environment_file(
        PREVIOUS if own else None, "STATE=previous\n"
    )
    host.install_unit(previous_environment)

    exit_code, output = _bad_environment_deploy(host, RELEASE, environment_text=None)

    assert exit_code == 0, output
    new = host.environment / f"{COMPONENT_ID}.{RELEASE}.env"
    assert new.read_text(encoding="utf-8") == "STATE=previous\n"
    assert previous_environment.read_text(encoding="utf-8") == "STATE=previous\n"
    # The rescue gave the previous release its unit back, naming its file.
    assert host.unit_environment == previous_environment


def test_a_deploy_without_any_environment_names_the_missing_legacy_file(
    tmp_path: Path,
) -> None:
    """A component that never had an environment file behaves as before."""
    host = _Host(tmp_path)
    host.current.symlink_to(host.release(PREVIOUS))
    host.release(RELEASE)
    stale = host.environment_file(RELEASE, "STATE=stale\n")
    host.environment_file(None, "unused\n").unlink()
    host.unit_directory.mkdir()

    exit_code, output = _bad_environment_deploy(host, RELEASE, environment_text=None)

    assert exit_code == 0, output
    assert not stale.exists()
    assert list(host.environment.iterdir()) == []
    unit = host.unit_directory / f"{COMPONENT_ID}.service"
    # The previous release had no managed unit: the rendered one stays.
    lines = unit.read_text(encoding="utf-8").splitlines()
    assert f"EnvironmentFile=-{host.environment}/{COMPONENT_ID}.env" in lines


# A redeploy copy that failed its gate is gone, so rollback takes the newest
# copy that passed (#245).

FAILED_REDEPLOY_TASKS = (
    "Inspect the currently active release",
    "Record the rollback candidate",
    "Compute release paths",
    "Create the releases and shared roots",
    "Create release and shared directories",
    "Switch the current release symlink",
)
FAILED_REDEPLOY_RESCUE = (
    "Restore the previous release symlink",
    "Restart the restored release",
    "Remove the redeploy copy that failed its gate",
)


def _main_tasks(names: tuple[str, ...]) -> list[dict[str, object]]:
    """Load main.yml's own tasks by name, block and rescue included.

    The role chowns to the application user as root; the controller here
    has no root, so the tasks lose only their owner and group.
    """
    loaded = DataLoader().load_from_file(str(ROLE / "tasks" / "main.yml"))
    assert isinstance(loaded, list)
    flat: list[dict[str, object]] = []
    for task in loaded:
        flat.append(task)
        for section in ("block", "rescue"):
            nested = task.get(section)
            if isinstance(nested, list):
                flat.extend(nested)
    tasks: list[dict[str, object]] = []
    for name in names:
        # A JSON round trip drops Ansible's tagged types, which YAML can't dump.
        task = json.loads(json.dumps(next(t for t in flat if t["name"] == name)))
        for module in task.values():
            if isinstance(module, dict):
                module.pop("owner", None)
                module.pop("group", None)
        tasks.append(task)
    return tasks


def _failed_redeploy(host: _Host) -> tuple[int, str]:
    """Redeploy current's release, fail its gate, and run the role's rescue."""
    playbook = host.tmp_path / "failed-redeploy.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Redeploy the current release and fail its gate",
                    "hosts": "cloudfall_servers",
                    "gather_facts": False,
                    "vars": {
                        "cloudfall_deploy_component": (
                            "{{ cloudfall_components | first }}"
                        ),
                        "cloudfall_deploy_release": RELEASE,
                    },
                    "tasks": [
                        *_main_tasks(FAILED_REDEPLOY_TASKS[:4]),
                        {
                            "name": "Activate the release behind the health gate",
                            "block": [
                                *_main_tasks(FAILED_REDEPLOY_TASKS[4:]),
                                {
                                    "name": "Fail the health gate",
                                    "ansible.builtin.fail": {"msg": "unhealthy"},
                                },
                            ],
                            "rescue": _main_tasks(FAILED_REDEPLOY_RESCUE),
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    return host.run(playbook, extra={})


def test_a_failed_redeploy_copy_is_removed_and_rollback_takes_the_passing_one(
    tmp_path: Path,
) -> None:
    host = _Host(tmp_path)
    host.release(RELEASE)
    passing = host.release(f"{RELEASE}.redeploy-20261101T000000Z")
    host.current.symlink_to(passing)

    exit_code, output = _failed_redeploy(host)

    assert exit_code == 0, output
    assert host.current.readlink() == passing
    assert sorted(path.name for path in host.releases.iterdir()) == [
        RELEASE,
        passing.name,
    ]

    host.current.unlink()
    host.current.symlink_to(host.release(PREVIOUS))
    exit_code, output = _rollback(host, RELEASE)

    assert exit_code == 0, output
    assert host.current.readlink() == passing


# A redeploy copy whose release command failed is gone too, with its
# environment file, so rollback never takes a copy that skipped its release
# command and its gate (#373).

FAILED_RELEASE_COMMAND_TASKS = (
    "Inspect the currently active release",
    "Record the rollback candidate",
    "Compute release paths",
    "Compute the release's environment file paths",
    "Create the releases and shared roots",
    "Create release and shared directories",
    "Find the environment file the previous release reads",
    "Choose the environment file the release reads",
    "Carry the previous release's environment over to the release",
    "Run the release command on the release server",
)


def _absolute_includes(task: object) -> object:
    """Point main.yml's includes at the role's files from a play outside it."""
    if isinstance(task, list):
        return [_absolute_includes(item) for item in task]
    if not isinstance(task, dict):
        return task
    include = task.get("ansible.builtin.include_tasks")
    if isinstance(include, str):
        return {**task, "ansible.builtin.include_tasks": str(ROLE / "tasks" / include)}
    return {key: _absolute_includes(value) for key, value in task.items()}


def _failed_release_command(host: _Host) -> tuple[int, str]:
    """Deploy RELEASE, without a new environment, through a failing release command."""
    playbook = host.tmp_path / "failed-release-command.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Deploy a release whose release command fails",
                    "hosts": "cloudfall_servers",
                    "gather_facts": False,
                    "vars": {
                        "cloudfall_deploy_component": (
                            "{{ cloudfall_components | first | combine({'release':"
                            " {'command': ['/bin/sh', '-c', 'echo migrating; exit 1'],"
                            " 'server': 'h1', 'timeoutSeconds': 60}}) }}"
                        ),
                        "cloudfall_deploy_release": RELEASE,
                        "cloudfall_deploy_environment_file": "",
                        "cloudfall_deploy_release_output_characters": 2000,
                        "cloudfall_deploy_release_failure_characters": 500,
                    },
                    "tasks": _absolute_includes(
                        _main_tasks(FAILED_RELEASE_COMMAND_TASKS)
                    ),
                }
            ]
        ),
        encoding="utf-8",
    )
    return host.run(playbook, extra={})


def test_a_redeploy_whose_release_command_fails_leaves_no_copy_behind(
    tmp_path: Path,
) -> None:
    host = _Host(tmp_path)
    host.release(RELEASE)
    host.environment_file(RELEASE, "STATE=first\n")
    passing = host.release(f"{RELEASE}.redeploy-20261101T000000Z")
    passing_environment = host.environment_file(passing.name, "STATE=passing\n")
    host.current.symlink_to(passing)

    exit_code, output = _failed_release_command(host)

    assert exit_code != 0, output
    assert "exited with code 1" in output
    assert host.current.readlink() == passing
    assert sorted(path.name for path in host.releases.iterdir()) == [
        RELEASE,
        passing.name,
    ]
    # The previous copy's own file stays (D-21).
    assert sorted(path.name for path in host.environment.iterdir()) == sorted(
        [f"{COMPONENT_ID}.{RELEASE}.env", passing_environment.name]
    )

    host.current.unlink()
    host.current.symlink_to(host.release(PREVIOUS))
    host.install_unit(host.environment_file(PREVIOUS, "STATE=previous\n"))
    exit_code, output = _rollback(host, RELEASE)

    assert exit_code == 0, output
    assert host.current.readlink() == passing
    assert host.unit_environment == passing_environment


def test_a_new_release_whose_release_command_fails_keeps_its_directory(
    tmp_path: Path,
) -> None:
    """Only a redeploy copy goes; a new release's directory stays as before."""
    host = _Host(tmp_path)
    previous = host.release(PREVIOUS)
    host.environment_file(PREVIOUS, "STATE=previous\n")
    host.current.symlink_to(previous)

    exit_code, output = _failed_release_command(host)

    assert exit_code != 0, output
    assert "exited with code 1" in output
    assert host.current.readlink() == previous
    assert sorted(path.name for path in host.releases.iterdir()) == [PREVIOUS, RELEASE]
    assert sorted(path.name for path in host.environment.iterdir()) == [
        f"{COMPONENT_ID}.{PREVIOUS}.env",
        f"{COMPONENT_ID}.{RELEASE}.env",
    ]


# The receipt names the release a redeployed directory holds.


def test_a_receipt_names_the_release_of_a_redeployed_previous_directory() -> None:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "release_receipt.yml"))
    assert isinstance(tasks, list)
    record = next(t for t in tasks if t["name"] == RECORD_ENTRY)
    written = DataLoader().load_from_file(str(ROLE / "tasks" / "receipt_write.yml"))
    assert isinstance(written, list)
    copy = next(t for t in written if t["name"] == RECEIPT)["ansible.builtin.copy"]
    controller: dict[str, object] = {}
    variables: dict[str, object] = {
        "cloudfall_deploy_receipt_passed": True,
        "cloudfall_server_id": "h1",
        "hostvars": {"localhost": controller},
        "cloudfall_deploy_component": {
            "id": COMPONENT_ID,
            "healthCheck": {"type": "http"},
        },
        "cloudfall_deploy_release": RELEASE,
        "cloudfall_deploy_artifact_sha256": "a" * 64,
        "cloudfall_deploy_previous_directory": f"/srv/app/releases/{REDEPLOYED}",
        "ansible_facts": {"date_time": {"iso8601": "2026-11-13T10:15:00Z"}},
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    variables["cloudfall_deploy_receipt_previous_release"] = templar.template(
        trust_as_template(record["vars"]["cloudfall_deploy_receipt_previous_release"])
    )
    controller["cloudfall_deploy_run_entries"] = templar.template(
        trust_as_template(
            record["ansible.builtin.set_fact"]["cloudfall_deploy_run_entries"]
        )
    )

    rendered = templar.template(trust_as_template(copy["content"]))

    receipt = cast("dict[str, object]", json.loads(str(rendered)))
    SchemaCatalog(SCHEMAS).validate_named("release-receipt.schema.json", receipt)
    assert _entries(receipt)[0]["previousRelease"] == RELEASE


# A deploy that fails on a later server rolls back the servers that switched
# before it, and its receipt names where every server ended (#222).

SWITCHED_SMOKE = """#!/bin/sh
# Passes in a release directory that holds a "healthy" file, and counts its
# runs there.
echo run >> runs
[ -f healthy ]
"""

# main.yml's own tasks a deploy runs on a server with no unit file yet,
# around the activation block, without the transfer, uv and the unit install,
# which need root and a real Debian server.
DEPLOY_REPLAY_TASKS = (
    "Inspect the currently active release",
    "Record the rollback candidate",
    "Compute release paths",
    "Create the releases and shared roots",
    "Compute service unit paths",
    "Inspect the existing service unit",
    "Read the existing service unit",
    "Recognize a hand-made service unit",
    "Activate the release behind the health gate",
    "Receipt the release that passed its health gate",
    "Remove the releases beyond the declared count",
)


def _strip_ownership(task: object) -> object:
    """Drop owner and group from every module of a task, nested ones too."""
    if isinstance(task, list):
        return [_strip_ownership(item) for item in task]
    if not isinstance(task, dict):
        return task
    stripped: dict[str, object] = {}
    for key, value in task.items():
        if key in {"owner", "group"}:
            continue
        stripped[key] = _strip_ownership(value)
    return stripped


@dataclass(frozen=True, slots=True)
class _Fleet:
    """Stand-in servers on this controller, one install root each.

    The deploy role runs as a replay role: main.yml's tasks of
    ``DEPLOY_REPLAY_TASKS`` with the role's other task files unchanged, so
    the gate, its rescue, the rollback of the servers that switched and the
    receipt are the role's own.
    """

    tmp_path: Path
    servers: tuple[str, ...]
    retain: int = 5

    def install_root(self, server: str) -> Path:
        return self.tmp_path / server / "app"

    def current(self, server: str) -> Path:
        return self.install_root(server) / "current"

    def release(self, server: str, name: str, *, healthy: bool) -> Path:
        directory = self.install_root(server) / "releases" / name
        _executable(directory / "bin" / "smoke", SWITCHED_SMOKE)
        if healthy:
            (directory / "healthy").write_text("", encoding="utf-8")
        return directory

    def runs(self, server: str, name: str) -> int:
        path = self.install_root(server) / "releases" / name / "runs"
        if not path.exists():
            return 0
        return len(path.read_text(encoding="utf-8").splitlines())

    @property
    def receipt(self) -> dict[str, object]:
        path = self.tmp_path / "receipts" / f"{COMPONENT_ID}.json"
        receipt = cast("dict[str, object]", json.loads(path.read_text("utf-8")))
        SchemaCatalog(SCHEMAS).validate_named("release-receipt.schema.json", receipt)
        return receipt

    def _role(self) -> None:
        role = self.tmp_path / "roles" / "cloudfall_deploy_replay"
        (role / "tasks").mkdir(parents=True)
        for directory in ("defaults", "vars"):
            (role / directory).mkdir()
            (role / directory / "main.yml").write_text(
                (ROLE / directory / "main.yml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        for path in (ROLE / "tasks").glob("*.yml"):
            if path.name != "main.yml":
                (role / "tasks" / path.name).write_text(
                    path.read_text(encoding="utf-8"), encoding="utf-8"
                )
        loaded = DataLoader().load_from_file(str(ROLE / "tasks" / "main.yml"))
        assert isinstance(loaded, list)
        # A JSON round trip drops Ansible's tagged types, which YAML can't dump.
        tasks = [
            _strip_ownership(json.loads(json.dumps(task)))
            for task in loaded
            if task["name"] in DEPLOY_REPLAY_TASKS
        ]
        assert len(tasks) == len(DEPLOY_REPLAY_TASKS)
        (role / "tasks" / "main.yml").write_text(
            yaml.safe_dump(tasks), encoding="utf-8"
        )

    def deploy(self) -> tuple[int, str]:
        """Deploy RELEASE one server at a time, as deploy.yml's plays do."""
        self._role()
        _executable(self.tmp_path / "bin" / "systemctl", SYSTEMCTL)
        (self.tmp_path / "bin" / "systemctl.loaded").write_text("", "utf-8")
        systemd_run = self.tmp_path / "bin" / "systemd-run"
        _executable(systemd_run, SYSTEMD_RUN)
        hosts = {
            server: {
                "ansible_connection": "local",
                "ansible_python_interpreter": sys.executable,
                "ansible_become": False,
                "cloudfall_server_id": server,
                "cloudfall_components": [
                    {
                        **_Host(self.tmp_path).component(),
                        "install_root": str(self.install_root(server)),
                        "retainReleases": self.retain,
                    }
                ],
            }
            for server in self.servers
        }
        inventory = self.tmp_path / "inventory.json"
        inventory.write_text(
            json.dumps(
                {"all": {"children": {"cloudfall_servers": {"hosts": hosts}}}}
            ),
            encoding="utf-8",
        )
        playbook = self.tmp_path / "deploy.yml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    {
                        "name": "Deploy one server at a time",
                        "hosts": "cloudfall_servers",
                        "gather_facts": True,
                        "any_errors_fatal": True,
                        "serial": 1,
                        "tasks": [
                            {
                                "name": "Deploy as the role does",
                                "ansible.builtin.include_role": {
                                    "name": "cloudfall_deploy_replay"
                                },
                                "vars": {
                                    "cloudfall_deploy_component": (
                                        "{{ cloudfall_components | first }}"
                                    ),
                                },
                            }
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        variables = {
            "cloudfall_deploy_release": RELEASE,
            "cloudfall_deploy_artifact_sha256": "a" * 64,
            "cloudfall_deploy_receipt_directory": str(self.tmp_path / "receipts"),
            "cloudfall_deploy_systemd_run": str(systemd_run),
        }
        command = playbook_command(
            PlaybookRun(playbook, inventory, extra_vars=(json.dumps(variables),)),
            ENGINE,
        )
        done = subprocess.run(  # noqa: S603 - the engine's own argv
            command.argv,
            env={
                **os.environ,
                **command.environment,
                "PATH": f"{self.tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return done.returncode, f"{done.stdout}\n{done.stderr}"


def _entries(receipt: dict[str, object]) -> list[dict[str, object]]:
    spec = receipt["spec"]
    assert isinstance(spec, dict)
    servers = spec["servers"]
    assert isinstance(servers, list)
    return cast("list[dict[str, object]]", servers)


def test_a_later_server_failing_its_gate_rolls_back_the_earlier_one(
    tmp_path: Path,
) -> None:
    fleet = _Fleet(tmp_path, ("h1", "h2"))
    for server in fleet.servers:
        fleet.current(server).parent.mkdir(parents=True)
        fleet.current(server).symlink_to(
            fleet.release(server, PREVIOUS, healthy=True)
        )
    fleet.release("h1", RELEASE, healthy=True)
    fleet.release("h2", RELEASE, healthy=False)

    exit_code, output = fleet.deploy()

    assert exit_code != 0, output
    for server in fleet.servers:
        assert fleet.current(server).readlink() == (
            fleet.install_root(server) / "releases" / PREVIOUS
        ), output
    # h1's release passed its gate, then its previous release was gated again.
    assert fleet.runs("h1", RELEASE) == 1
    assert fleet.runs("h1", PREVIOUS) == 1
    assert (
        f"Release {RELEASE} failed the command health check of {COMPONENT_ID} on h2"
    ) in output
    assert "h1 had switched to it earlier in this run and was rolled back" in output
    receipt = fleet.receipt
    spec = receipt["spec"]
    assert isinstance(spec, dict)
    assert spec["release"] == RELEASE
    assert [
        (e["server"], e["release"], e["healthCheckPassed"], e["rolledBack"])
        for e in _entries(receipt)
    ] == [("h1", PREVIOUS, True, True), ("h2", PREVIOUS, False, True)]


def test_a_deploy_that_passes_everywhere_receipts_every_server(
    tmp_path: Path,
) -> None:
    fleet = _Fleet(tmp_path, ("h1", "h2"))
    for server in fleet.servers:
        fleet.current(server).parent.mkdir(parents=True)
        fleet.current(server).symlink_to(
            fleet.release(server, PREVIOUS, healthy=True)
        )
        fleet.release(server, RELEASE, healthy=True)

    exit_code, output = fleet.deploy()

    assert exit_code == 0, output
    for server in fleet.servers:
        assert fleet.current(server).readlink() == (
            fleet.install_root(server) / "releases" / RELEASE
        )
    assert [
        (e["server"], e["release"], e["previousRelease"], e["rolledBack"])
        for e in _entries(fleet.receipt)
    ] == [("h1", RELEASE, PREVIOUS, False), ("h2", RELEASE, PREVIOUS, False)]


def test_a_single_server_deploy_receipts_one_entry(tmp_path: Path) -> None:
    fleet = _Fleet(tmp_path, ("h1",))
    fleet.current("h1").parent.mkdir(parents=True)
    fleet.current("h1").symlink_to(fleet.release("h1", PREVIOUS, healthy=True))
    fleet.release("h1", RELEASE, healthy=False)

    exit_code, output = fleet.deploy()

    assert exit_code != 0, output
    assert fleet.current("h1").readlink() == (
        fleet.install_root("h1") / "releases" / PREVIOUS
    )
    assert "had switched to it earlier" not in output
    assert [
        (e["server"], e["release"], e["healthCheckPassed"], e["rolledBack"])
        for e in _entries(fleet.receipt)
    ] == [("h1", PREVIOUS, False, True)]


def test_every_earlier_server_is_rolled_back_a_first_release_is_stopped(
    tmp_path: Path,
) -> None:
    """h1 had no release before this run, h2 had one; h3 fails its gate."""
    fleet = _Fleet(tmp_path, ("h1", "h2", "h3"))
    first = fleet.release("h1", RELEASE, healthy=True)
    for server in ("h2", "h3"):
        fleet.current(server).symlink_to(
            fleet.release(server, PREVIOUS, healthy=True)
        )
    fleet.release("h2", RELEASE, healthy=True)
    fleet.release("h3", RELEASE, healthy=False)

    exit_code, output = fleet.deploy()

    assert exit_code != 0, output
    # A first release has nothing to go back to: it is stopped where it is.
    assert fleet.current("h1").readlink() == first
    for server in ("h2", "h3"):
        assert fleet.current(server).readlink() == (
            fleet.install_root(server) / "releases" / PREVIOUS
        )
    assert "h1, h2 had switched to it earlier in this run and were rolled back" in (
        output
    )
    # The last server that switched goes back first.
    assert output.index("[h3 -> h2]") < output.index("[h3 -> h1]")
    assert [
        (e["server"], e["release"], e["previousRelease"], e["rolledBack"])
        for e in _entries(fleet.receipt)
    ] == [
        ("h1", None, None, True),
        ("h2", PREVIOUS, PREVIOUS, True),
        ("h3", PREVIOUS, PREVIOUS, True),
    ]


def test_an_earlier_server_whose_restored_release_fails_is_named(
    tmp_path: Path,
) -> None:
    fleet = _Fleet(tmp_path, ("h1", "h2"))
    fleet.current("h1").symlink_to(fleet.release("h1", PREVIOUS, healthy=False))
    fleet.current("h2").symlink_to(fleet.release("h2", PREVIOUS, healthy=True))
    fleet.release("h1", RELEASE, healthy=True)
    fleet.release("h2", RELEASE, healthy=False)

    exit_code, output = fleet.deploy()

    assert exit_code != 0, output
    assert fleet.current("h1").readlink() == (
        fleet.install_root("h1") / "releases" / PREVIOUS
    )
    assert (
        "h1 had switched to it earlier in this run and was rolled back, but the"
        " restored release failed its health check again on h1"
    ) in output
    assert [e["release"] for e in _entries(fleet.receipt)] == [PREVIOUS, PREVIOUS]


# What the rollback of a switched server runs, and the failure's last words.

SWITCHED_ROLLBACK = "switched_rollback.yml"


def _switched_tasks() -> list[dict[str, object]]:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / SWITCHED_ROLLBACK))
    assert isinstance(tasks, list)
    return tasks


def _switched_rollback(switched: dict[str, object]) -> list[str]:
    """Name, in order, every top-level step whose ``when`` holds."""
    variables: dict[str, object] = {
        "cloudfall_deploy_release": RELEASE,
        "cloudfall_deploy_switched": {
            "cloudfall_deploy_unit_managed_before": False,
            "cloudfall_deploy_unit_hand_made": False,
            "cloudfall_deploy_release_directory": f"/srv/app/releases/{RELEASE}",
            **switched,
        },
    }

    def runs(task: dict[str, object]) -> bool:
        value = task.get("when", [])
        conditions = value if isinstance(value, list) else [value]
        task_vars = task.get("vars", {})
        assert isinstance(task_vars, dict)
        templar = Templar(
            loader=DataLoader(),
            variables={
                **variables,
                **{name: trust_as_template(v) for name, v in task_vars.items()},
            },
        )
        return all(
            templar.evaluate_conditional(trust_as_template(str(condition)))
            for condition in conditions
        )

    return [str(task["name"]) for task in _switched_tasks() if runs(task)]


def test_a_switched_server_gets_its_previous_release_and_unit_back() -> None:
    assert _switched_rollback(
        {
            "cloudfall_deploy_previous_directory": f"/srv/app/releases/{PREVIOUS}",
            "cloudfall_deploy_unit_managed_before": True,
        }
    ) == [
        "Read the component's declaration on the switched server",
        "Restore the previous release symlink of the switched server",
        "Restore the service unit of the switched server's previous release",
        "Restart the switched server's restored release",
        "Gate the switched server's restored release on its health check",
        "Record the rollback in the run's release receipt",
    ]


def test_a_switched_redeploy_copy_goes_with_the_rollback() -> None:
    names = _switched_rollback(
        {
            "cloudfall_deploy_previous_directory": f"/srv/app/releases/{RELEASE}",
            "cloudfall_deploy_release_directory": f"/srv/app/releases/{REDEPLOYED}",
        }
    )

    assert "Remove the switched server's redeploy copy of the rolled-back release" in (
        names
    )


def test_a_switched_first_release_is_stopped_and_a_kept_unit_put_back() -> None:
    assert _switched_rollback(
        {
            "cloudfall_deploy_previous_directory": "",
            "cloudfall_deploy_unit_hand_made": True,
        }
    ) == [
        "Read the component's declaration on the switched server",
        "Stop the switched server's first release",
        "Restore the switched server's pre-Cloudfall service unit",
        "Record the rollback in the run's release receipt",
    ]


SWITCHED_GATE = "Gate the switched server's restored release on its health check"


def test_every_server_step_of_the_switched_rollback_is_delegated() -> None:
    local = {
        "Read the component's declaration on the switched server",
        SWITCHED_GATE,
        "Record the rollback in the run's release receipt",
    }
    for task in _switched_tasks():
        if task["name"] in local:
            continue
        assert task["delegate_to"] == "{{ cloudfall_deploy_switched_server }}"
    gate = next(t for t in _switched_tasks() if t["name"] == SWITCHED_GATE)
    block = gate["block"]
    assert isinstance(block, list)
    include = block[0]["ansible.builtin.include_tasks"]
    assert include == {
        "file": "health_gate.yml",
        "apply": {"delegate_to": "{{ cloudfall_deploy_switched_server }}"},
    }
    record = next(
        t
        for t in _switched_tasks()
        if t["name"] == "Record the rollback in the run's release receipt"
    )
    assert (record["delegate_to"], record["delegate_facts"]) == ("localhost", True)


def _aftermath(variables: dict[str, object]) -> str:
    role_vars = DataLoader().load_from_file(str(ROLE / "vars" / "main.yml"))
    assert isinstance(role_vars, dict)
    trusted = {name: trust_as_template(value) for name, value in role_vars.items()}
    templar = Templar(loader=DataLoader(), variables={**trusted, **variables})
    return str(
        templar.template(trust_as_template("{{ cloudfall_deploy_failure_aftermath }}"))
    )


def test_a_failure_says_its_release_command_already_ran() -> None:
    component = {
        "id": COMPONENT_ID,
        "release": {"command": ["bin/migrate", "--up"], "server": "h1"},
    }

    message = _aftermath(
        {
            "cloudfall_deploy_component": component,
            "hostvars": {
                "h1": {"cloudfall_deploy_release_command_result": {"exitCode": 0}},
                "localhost": {
                    "cloudfall_deploy_run_entries": {
                        "h1": {**PASSED_ENTRY, "rolledBack": True},
                        "h2": FAILED_ENTRY,
                    }
                },
            },
        }
    )

    assert message == (
        "; h1 had switched to it earlier in this run and was rolled back; its"
        " release command bin/migrate --up already ran on h1 and is not reverted,"
        " so the release that serves again runs against what it changed"
    )


def test_a_failure_without_switched_servers_or_release_command_adds_nothing() -> None:
    assert _aftermath({"cloudfall_deploy_component": {"id": COMPONENT_ID}}) == ""


# The receipt's entries (#222).


def _run_receipt(*entries: dict[str, object]) -> dict[str, object]:
    return {
        "apiVersion": "cloudfall/v1",
        "kind": "ReleaseReceipt",
        "metadata": {
            "id": COMPONENT_ID,
            "description": "Release receipt written by cloudfall_deploy",
        },
        "spec": {
            "component": COMPONENT_ID,
            "release": RELEASE,
            "artifactSha256": "a" * 64,
            "servers": list(entries),
        },
    }


PASSED_ENTRY: dict[str, object] = {
    "server": "h1",
    "release": RELEASE,
    "previousRelease": PREVIOUS,
    "deployedAt": "2026-11-15T00:00:00Z",
    "healthCheckPassed": True,
    "rolledBack": False,
}
FAILED_ENTRY: dict[str, object] = {
    **PASSED_ENTRY,
    "server": "h2",
    "release": PREVIOUS,
    "healthCheckPassed": False,
    "rolledBack": True,
    "healthCheck": {
        "type": "command",
        "command": ["bin/smoke"],
        "attempts": 1,
        "exitCode": 1,
        "timedOut": False,
    },
}


def _validate(receipt: dict[str, object]) -> None:
    SchemaCatalog(SCHEMAS).validate_named("release-receipt.schema.json", receipt)


def test_a_receipt_of_a_run_holds_every_servers_entry() -> None:
    _validate(_run_receipt(PASSED_ENTRY, FAILED_ENTRY))
    _validate(
        _run_receipt({**PASSED_ENTRY, "release": PREVIOUS, "rolledBack": True})
    )


@pytest.mark.parametrize(
    "entry",
    [
        {**FAILED_ENTRY, "healthCheck": None},
        {**FAILED_ENTRY, "rolledBack": False},
        {**PASSED_ENTRY, "release": None},
        {**PASSED_ENTRY, "output": "secret"},
    ],
    ids=[
        "failed-without-a-command-check",
        "failed-but-still-switched",
        "still-switched-to-no-release",
        "another-key",
    ],
)
def test_the_receipt_schema_refuses_an_incoherent_entry(
    entry: dict[str, object],
) -> None:
    if entry.get("healthCheck", ()) is None:
        entry = {key: value for key, value in entry.items() if key != "healthCheck"}

    with pytest.raises(ValidationError):
        _validate(_run_receipt(entry))


def test_the_receipt_schema_refuses_a_run_without_servers() -> None:
    with pytest.raises(ValidationError):
        _validate(_run_receipt())


def test_a_single_server_receipt_from_before_entries_still_validates() -> None:
    """Read as a one-entry list: spec.server and its outcome, no servers."""
    receipt = _run_receipt()
    spec = receipt["spec"]
    assert isinstance(spec, dict)
    del spec["servers"]
    spec.update(
        {
            "server": "h1",
            "deployedAt": "2026-11-15T00:00:00Z",
            "previousRelease": PREVIOUS,
            "healthCheckPassed": True,
        }
    )

    _validate(receipt)
    with pytest.raises(ValidationError):
        _validate({**receipt, "spec": {**spec, "rolledBack": False}})


# A deploy that passed its gate keeps retainReleases releases (#224, D-15).

OLDER = tuple(f"2026101{day}T000000Z-abcdef{day}" for day in range(5))
SELECT = "Select the releases beyond the declared count"


def _selected_for_removal(
    directories: list[str], *, retain: int, previous: str, current: str
) -> list[str]:
    """Evaluate the cleanup's selection on directory names under releases/."""
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "release_cleanup.yml"))
    assert isinstance(tasks, list)
    task = next(t for t in tasks if t["name"] == SELECT)
    releases = "/srv/app/releases"
    variables: dict[str, object] = {
        "cloudfall_deploy_release": RELEASE,
        "cloudfall_deploy_component": {"retainReleases": retain},
        "cloudfall_deploy_previous_directory": (
            f"{releases}/{previous}" if previous else ""
        ),
        "cloudfall_deploy_cleanup_current": {
            "stat": {
                "exists": True,
                "islnk": True,
                "lnk_source": f"{releases}/{current}",
            }
        },
        "cloudfall_deploy_cleanup_found": {
            "files": [{"path": f"{releases}/{name}"} for name in directories]
        },
        **{name: trust_as_template(value) for name, value in task["vars"].items()},
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    selected = templar.template(
        trust_as_template(
            task["ansible.builtin.set_fact"]["cloudfall_deploy_removed_releases"]
        )
    )
    assert isinstance(selected, list)
    return [str(name) for name in selected]


@pytest.mark.parametrize(
    ("directories", "retain", "previous", "current", "removed"),
    [
        # The newest stay: the deployed one, the one before it, then by id.
        ([*OLDER, RELEASE], 3, OLDER[4], RELEASE, list(OLDER[:3])),
        ([*OLDER, RELEASE], 5, OLDER[4], RELEASE, [OLDER[0]]),
        ([*OLDER, RELEASE], 6, OLDER[4], RELEASE, []),
        # After a rollback, the release before the deployed one is the oldest.
        ([*OLDER, RELEASE], 2, OLDER[0], RELEASE, list(OLDER[1:])),
        ([*OLDER, RELEASE], 3, OLDER[0], RELEASE, list(OLDER[1:4])),
        # A redeploy: both copies of the deployed release are one release.
        (
            [*OLDER, RELEASE, REDEPLOYED],
            2,
            RELEASE,
            REDEPLOYED,
            list(OLDER[:4]),
        ),
        # A first deploy has no release before it.
        ([RELEASE], 2, "", RELEASE, []),
        # A copy of a removed release goes with it.
        (
            [OLDER[0], f"{OLDER[0]}.redeploy-20261010T000000Z", OLDER[1], RELEASE],
            2,
            OLDER[1],
            RELEASE,
            [OLDER[0]],
        ),
    ],
    ids=[
        "keep-3",
        "keep-5",
        "fewer-than-the-count",
        "after-a-rollback-keep-2",
        "after-a-rollback-keep-3",
        "redeploy",
        "first-deploy",
        "copies-go-together",
    ],
)
def test_the_cleanup_keeps_current_the_release_before_it_and_the_newest(
    directories: list[str], retain: int, previous: str, current: str, removed: list[str]
) -> None:
    assert (
        _selected_for_removal(
            directories, retain=retain, previous=previous, current=current
        )
        == removed
    )


def _names(fleet: _Fleet, server: str) -> list[str]:
    releases = fleet.install_root(server) / "releases"
    return sorted(path.name for path in releases.iterdir())


def test_a_passed_deploy_removes_the_releases_beyond_the_count(tmp_path: Path) -> None:
    fleet = _Fleet(tmp_path, ("h1",), retain=3)
    for name in OLDER:
        fleet.release("h1", name, healthy=True)
    fleet.current("h1").symlink_to(fleet.release("h1", PREVIOUS, healthy=True))
    fleet.release("h1", RELEASE, healthy=True)
    (fleet.install_root("h1") / "releases" / "hand-made").mkdir()
    environment = fleet.install_root("h1") / "shared" / "env"
    environment.mkdir(parents=True)
    copy = f"{OLDER[0]}.redeploy-20261010T000000Z"
    kept = [f"{COMPONENT_ID}.{name}.env" for name in (OLDER[4], PREVIOUS, RELEASE)]
    for name in [
        *(f"{COMPONENT_ID}.{release}.env" for release in (*OLDER, copy)),
        *kept,
        f"{COMPONENT_ID}.env",
        "other.env",
    ]:
        (environment / name).write_text("KEY=value\n", encoding="utf-8")

    exit_code, output = fleet.deploy()

    assert exit_code == 0, output
    assert _names(fleet, "h1") == sorted([OLDER[4], PREVIOUS, RELEASE, "hand-made"])
    # A removed release's environment files go with it (D-21); the legacy file
    # and any file that names no release stay.
    assert sorted(path.name for path in environment.iterdir()) == sorted(
        [*kept, f"{COMPONENT_ID}.env", "other.env"]
    )
    # The cleanup removes inside releases/, never the 0750 root itself.
    releases = fleet.install_root("h1") / "releases"
    assert stat.S_IMODE(releases.stat().st_mode) == 0o750
    (entry,) = _entries(fleet.receipt)
    assert entry["removedReleases"] == list(OLDER[:4])


def test_a_deploy_after_a_rollback_keeps_the_release_it_rolled_back_to(
    tmp_path: Path,
) -> None:
    """The oldest release is current: it stays with the deployed one."""
    fleet = _Fleet(tmp_path, ("h1",), retain=2)
    for name in OLDER[1:]:
        fleet.release("h1", name, healthy=True)
    fleet.current("h1").parent.mkdir(parents=True, exist_ok=True)
    fleet.current("h1").symlink_to(fleet.release("h1", OLDER[0], healthy=True))
    fleet.release("h1", RELEASE, healthy=True)

    exit_code, output = fleet.deploy()

    assert exit_code == 0, output
    assert _names(fleet, "h1") == [OLDER[0], RELEASE]
    assert _entries(fleet.receipt)[0]["removedReleases"] == list(OLDER[1:])


def test_a_failed_gate_removes_no_release(tmp_path: Path) -> None:
    fleet = _Fleet(tmp_path, ("h1",), retain=2)
    for name in OLDER:
        fleet.release("h1", name, healthy=True)
    fleet.current("h1").symlink_to(fleet.release("h1", PREVIOUS, healthy=True))
    fleet.release("h1", RELEASE, healthy=False)

    exit_code, output = fleet.deploy()

    assert exit_code != 0, output
    assert _names(fleet, "h1") == sorted([*OLDER, PREVIOUS, RELEASE])
    assert "removedReleases" not in _entries(fleet.receipt)[0]


def test_a_server_that_cleaned_up_still_rolls_back_when_a_later_one_fails(
    tmp_path: Path,
) -> None:
    fleet = _Fleet(tmp_path, ("h1", "h2"), retain=2)
    for server in fleet.servers:
        fleet.release(server, OLDER[0], healthy=True)
        fleet.current(server).symlink_to(
            fleet.release(server, PREVIOUS, healthy=True)
        )
    fleet.release("h1", RELEASE, healthy=True)
    fleet.release("h2", RELEASE, healthy=False)

    exit_code, output = fleet.deploy()

    assert exit_code != 0, output
    assert fleet.current("h1").readlink() == (
        fleet.install_root("h1") / "releases" / PREVIOUS
    )
    assert _names(fleet, "h1") == [PREVIOUS, RELEASE]
    assert _names(fleet, "h2") == [OLDER[0], PREVIOUS, RELEASE]
    h1, h2 = _entries(fleet.receipt)
    assert (h1["rolledBack"], h1["removedReleases"]) == (True, [OLDER[0]])
    assert "removedReleases" not in h2


def test_the_receipt_schema_refuses_removed_releases_on_a_failed_gate() -> None:
    _validate(_run_receipt({**PASSED_ENTRY, "removedReleases": [OLDER[0]]}))
    with pytest.raises(ValidationError):
        _validate(_run_receipt({**FAILED_ENTRY, "removedReleases": [OLDER[0]]}))
    with pytest.raises(ValidationError):
        _validate(_run_receipt({**PASSED_ENTRY, "removedReleases": ["old"]}))
