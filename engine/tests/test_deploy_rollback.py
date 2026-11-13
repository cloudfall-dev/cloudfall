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
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.validation import SchemaCatalog
from cloudfall_engine.playbook import PlaybookRun, playbook_command, resolve_playbook

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ENGINE = ROOT / "engine"
ROLE = ENGINE / "ansible" / "roles" / "cloudfall_deploy"
COMPONENT_ID = "crm-backend"
PREVIOUS = "20261027T000000Z-abcdef1"
RELEASE = "20261028T000000Z-abcdef2"
REDEPLOYED = f"{RELEASE}.redeploy-20261113T101500Z"
RECEIPT = "Write the release receipt on the controller"

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
        variables = {"cloudfall_deploy_systemd_run": str(systemd_run), **extra}
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
    fresh = rf"\(item=[^)]*/releases/{RELEASE}\.redeploy-[0-9]{{8}}T[0-9]{{6}}Z\)"
    assert re.search(fresh, output), output
    assert f"/releases/{RELEASE})" not in output
    assert host.current.readlink() == live
    assert sorted(path.name for path in live.rglob("*")) == before
    assert [path.name for path in host.releases.iterdir()] == [RELEASE]


# A rollback takes the newest copy of a redeployed release.


def _rollback(host: _Host, release: str) -> tuple[int, str]:
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


def test_a_rollback_to_a_missing_release_is_refused(tmp_path: Path) -> None:
    host = _Host(tmp_path)
    host.release(f"{PREVIOUS}0")
    live = host.release(RELEASE)
    host.current.symlink_to(live)

    exit_code, output = _rollback(host, PREVIOUS)

    assert exit_code != 0
    assert f"Release {PREVIOUS} does not exist under" in output
    assert host.current.readlink() == live


# The receipt names the release a redeployed directory holds.


def test_a_receipt_names_the_release_of_a_redeployed_previous_directory() -> None:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "release_receipt.yml"))
    assert isinstance(tasks, list)
    copy = next(t for t in tasks if t["name"] == RECEIPT)["ansible.builtin.copy"]
    variables: dict[str, object] = {
        "cloudfall_deploy_receipt_passed": True,
        "cloudfall_server_id": "h1",
        "cloudfall_deploy_component": {
            "id": COMPONENT_ID,
            "healthCheck": {"type": "http"},
        },
        "cloudfall_deploy_release": RELEASE,
        "cloudfall_deploy_artifact_sha256": "a" * 64,
        "cloudfall_deploy_previous_directory": f"/srv/app/releases/{REDEPLOYED}",
        "ansible_facts": {"date_time": {"iso8601": "2026-11-13T10:15:00Z"}},
    }

    rendered = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(copy["content"])
    )

    receipt = cast("dict[str, object]", json.loads(str(rendered)))
    SchemaCatalog(SCHEMAS).validate_named("release-receipt.schema.json", receipt)
    spec = receipt["spec"]
    assert isinstance(spec, dict)
    assert spec["previousRelease"] == RELEASE
