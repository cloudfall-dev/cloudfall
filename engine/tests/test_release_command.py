"""A component's release command, run by the deploy role before it switches.

The role needs a Debian host with systemd, so these tests take three routes
that run without one. The release command tasks run for real under
``ansible-playbook`` on the controller, with ``systemd-run`` swapped through the
role's ``cloudfall_deploy_systemd_run`` variable for a stand-in that honours
``--working-directory`` and runs the argv after ``--``. ``deploy.yml`` runs for
real against two local hosts with the deploy role replaced, through
``--roles``, by one that records what each host did. The rest replays the
role's own conditions and receipt template with Ansible's templating.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import SchemaCatalog, validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cloudfall_engine.cli import main
from jsonschema.exceptions import ValidationError

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"
ROLE = ENGINE / "ansible" / "roles" / "cloudfall_deploy"
MATERIALIZE = "Materialize locked dependencies for the release"
RUN = "Run the release command on the release server"
REQUIRE = "Require the release command to have passed before switching"
INSTALL_UNIT = "Install the managed service unit"
ACTIVATE = "Activate the release behind the health gate"
SWITCH = "Switch the current release symlink"
RECEIPT = "Write the release receipt on the controller"
TIMEOUT_MARKER = "cloudfall-test-timeout"
REFUSED_MARKER = "cloudfall-test-refused"

FAKE_SYSTEMD_RUN = f"""#!/bin/sh
# Stand-in for systemd-run --wait --pipe: chdir like --working-directory,
# then run the argv after "--" and pass on its exit code.
printf '%s\\n' "$@" > "$0.argv"
directory=
while [ "$1" != "--" ]; do
  case "$1" in
    --working-directory=*) directory="${{1#--working-directory=}}" ;;
  esac
  shift
done
shift
cd "$directory" || exit 200
if [ -f "$directory/{REFUSED_MARKER}" ]; then
  echo "Failed to start transient service unit: Unit already exists." >&2
  exit 1
fi
"$@"
status=$?
if [ -f "$directory/{TIMEOUT_MARKER}" ]; then
  echo "Finished with result: timeout" >&2
  exit 1
fi
exit $status
"""

MIGRATE = """#!/bin/sh
# Creates the "table", reports where and as what it ran, then exits as asked.
echo "created" > "$PWD/table"
echo "cwd=$PWD"
echo "to-stderr" >&2
if [ -n "$2" ]; then
  i=0
  while [ "$i" -lt "$2" ]; do printf 'x'; i=$((i + 1)); done
  echo
fi
echo "last-line"
exit "$1"
"""


def _component(
    release: dict[str, object] | None, *, server: str = "b"
) -> dict[str, object]:
    component: dict[str, object] = {
        "id": "crm-backend",
        "application_user": "crm",
        "install_root": "/srv/apps/crm/backend",
        "service": {"name": "crm-backend", "command": [".venv/bin/app"]},
        "healthCheck": {"type": "none"},
    }
    if release is not None:
        component["release"] = {"timeoutSeconds": 300, "server": server, **release}
    return component


def _release_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    release_directory = tmp_path / "releases" / "20261027T000000Z-abcdef1"
    working_directory = release_directory / "services" / "api"
    working_directory.mkdir(parents=True)
    migrate = release_directory / "bin" / "migrate"
    migrate.parent.mkdir()
    migrate.write_text(MIGRATE, encoding="utf-8")
    migrate.chmod(0o755)
    systemd_run = tmp_path / "systemd-run"
    systemd_run.write_text(FAKE_SYSTEMD_RUN, encoding="utf-8")
    systemd_run.chmod(0o755)
    return release_directory, working_directory, systemd_run


def _local_inventory(tmp_path: Path) -> Path:
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "all": {
                    "hosts": {
                        "localhost": {
                            "ansible_connection": "local",
                            "ansible_python_interpreter": sys.executable,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return inventory


def _run_release_command(
    tmp_path: Path,
    arguments: list[str],
    *,
    timed_out: bool = False,
    refused: bool = False,
) -> tuple[int, Path]:
    """Run the role's release command tasks; answer the exit code and result."""
    release_directory, working_directory, systemd_run = _release_tree(tmp_path)
    if timed_out:
        (working_directory / TIMEOUT_MARKER).write_text("", encoding="utf-8")
    if refused:
        (working_directory / REFUSED_MARKER).write_text("", encoding="utf-8")
    result_file = tmp_path / "result.json"
    variables = {
        "cloudfall_server_id": "b",
        "cloudfall_deploy_release": release_directory.name,
        "cloudfall_deploy_release_directory": str(release_directory),
        "cloudfall_deploy_shared_directory": str(tmp_path / "shared"),
        "cloudfall_deploy_systemd_run": str(systemd_run),
        "cloudfall_deploy_component": _component(
            {
                "command": ["bin/migrate", *arguments],
                "workingDirectory": "services/api",
            }
        ),
    }
    playbook = tmp_path / "release.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Run a release command",
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": variables,
                    "tasks": [
                        {
                            "name": "Run the role's release command tasks",
                            "ansible.builtin.include_role": {
                                "name": "cloudfall_deploy",
                                "tasks_from": "release_command",
                            },
                        },
                        {
                            "name": "Keep the recorded result",
                            "ansible.builtin.copy": {
                                "content": (
                                    "{{ cloudfall_deploy_release_command_result"
                                    " | to_json }}"
                                ),
                                "dest": str(result_file),
                                "mode": "0644",
                            },
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    exit_code = main(
        [
            "playbook",
            "run",
            str(playbook),
            "--engine",
            str(ENGINE),
            "--inventory",
            str(_local_inventory(tmp_path)),
        ]
    )
    return exit_code, result_file


def _argv(tmp_path: Path) -> list[str]:
    return (tmp_path / "systemd-run.argv").read_text(encoding="utf-8").splitlines()


def test_s005_1_a_passing_release_command_records_its_run(tmp_path: Path) -> None:
    """The command's effect stays and its result names it, exit 0 and output."""
    exit_code, result_file = _run_release_command(tmp_path, ["0"])

    assert exit_code == 0
    working_directory = tmp_path / "releases" / "20261027T000000Z-abcdef1"
    assert (working_directory / "services" / "api" / "table").is_file()
    result = json.loads(result_file.read_text(encoding="utf-8"))
    assert result["server"] == "b"
    assert result["command"] == ["bin/migrate", "0"]
    assert result["workingDirectory"] == "services/api"
    assert result["exitCode"] == 0
    assert "last-line" in result["outputTail"]


def test_s005_2_the_command_runs_in_its_working_directory_as_the_user(
    tmp_path: Path,
) -> None:
    """A relative executable resolves against the release, not the work dir."""
    exit_code, result_file = _run_release_command(tmp_path, ["0"])

    assert exit_code == 0
    release_directory = tmp_path / "releases" / "20261027T000000Z-abcdef1"
    argv = _argv(tmp_path)
    assert f"--working-directory={release_directory}/services/api" in argv
    assert "--uid=crm" in argv
    assert "--gid=crm" in argv
    after = argv[argv.index("--") + 1 :]
    assert after == [
        "/bin/sh",
        "-c",
        'exec "$@" 2>&1',
        "cloudfall-release",
        f"{release_directory}/bin/migrate",
        "0",
    ]
    result = json.loads(result_file.read_text(encoding="utf-8"))
    assert f"cwd={release_directory}/services/api" in result["outputTail"]


def test_s005_3_the_command_loads_the_unit_environment_file(
    tmp_path: Path,
) -> None:
    """The transient unit gets the ``EnvironmentFile=-`` line the unit names."""
    exit_code, _ = _run_release_command(tmp_path, ["0"])

    assert exit_code == 0
    unit = Templar(
        loader=DataLoader(),
        variables={
            "cloudfall_deploy_component": {
                **_component(None),
                "install_root": str(tmp_path),
            }
        },
    ).template(
        trust_as_template(
            (ROLE / "templates" / "component.service.j2").read_text(encoding="utf-8")
        )
    )
    unit_line = next(
        line for line in str(unit).splitlines() if line.startswith("EnvironmentFile=")
    )
    assert f"--property={unit_line}" in _argv(tmp_path)


def test_s005_1_stderr_is_part_of_the_recorded_output(tmp_path: Path) -> None:
    _, result_file = _run_release_command(tmp_path, ["0"])

    result = json.loads(result_file.read_text(encoding="utf-8"))
    assert "to-stderr" in result["outputTail"]


def test_s005_4_a_failing_release_command_fails_the_play_naming_its_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, result_file = _run_release_command(tmp_path, ["1"])

    assert exit_code == 82
    output = _captured_output(capsys)
    assert "Release command bin/migrate 1 exited with code 1" in output
    assert "no server switched" in output
    assert "last-line" in output
    assert not result_file.exists()


def test_s005_5_a_timed_out_release_command_fails_naming_the_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, _ = _run_release_command(tmp_path, ["0"], timed_out=True)

    assert exit_code == 82
    output = _captured_output(capsys)
    assert "timed out after 300 seconds and was stopped" in output


def test_s005_4_a_unit_that_never_starts_names_systemd_run_s_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A unit that never starts fails with systemd-run's own message."""
    exit_code, result_file = _run_release_command(tmp_path, ["0"], refused=True)

    assert exit_code == 82
    output = _captured_output(capsys)
    assert "exited with code 1" in output
    assert "Failed to start transient service unit" in output
    assert not (tmp_path / "releases" / "20261027T000000Z-abcdef1" / "table").exists()
    assert not result_file.exists()


def test_s005_5_the_timeout_is_the_unit_runtime_limit(tmp_path: Path) -> None:
    _run_release_command(tmp_path, ["0"])

    argv = _argv(tmp_path)
    assert "--property=RuntimeMaxSec=300" in argv
    assert "--wait" in argv
    assert "--quiet" not in argv


def test_s005_7_the_output_tail_keeps_the_last_2000_characters(
    tmp_path: Path,
) -> None:
    exit_code, result_file = _run_release_command(tmp_path, ["0", "5000"])

    assert exit_code == 0
    tail = json.loads(result_file.read_text(encoding="utf-8"))["outputTail"]
    assert len(tail) == 2000
    assert tail.endswith("last-line")


def _captured_output(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return f"{captured.out}\n{captured.err}"


# Role order and conditions.


def _main_tasks() -> list[dict[str, object]]:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "main.yml"))
    assert isinstance(tasks, list)
    return tasks


def _names(tasks: list[dict[str, object]]) -> list[str]:
    names: list[str] = []
    for task in tasks:
        names.append(str(task["name"]))
        block = task.get("block")
        if isinstance(block, list):
            names.extend(str(inner["name"]) for inner in block)
    return names


def _holds(expression: object, variables: dict[str, object]) -> bool:
    templar = Templar(loader=DataLoader(), variables=variables)
    return bool(templar.evaluate_conditional(trust_as_template(str(expression))))


def _runs(name: str, variables: dict[str, object]) -> bool:
    task = next(t for t in _main_tasks() if t["name"] == name)
    conditions = task.get("when", [])
    assert isinstance(conditions, list)
    return all(_holds(c, variables) for c in conditions)


def test_s005_2_the_release_command_runs_between_materialize_and_the_switch() -> None:
    names = _names(_main_tasks())

    assert names.index(MATERIALIZE) < names.index(RUN)
    assert names.index(RUN) < names.index(REQUIRE)
    assert names.index(REQUIRE) < names.index(INSTALL_UNIT)
    assert names.index(INSTALL_UNIT) < names.index(SWITCH)
    run = next(t for t in _main_tasks() if t["name"] == RUN)
    assert run["ansible.builtin.include_tasks"] == "release_command.yml"


def _variables(
    component: dict[str, object], server: str, *, check_mode: bool = False
) -> dict[str, object]:
    return {
        "ansible_check_mode": check_mode,
        "cloudfall_server_id": server,
        "cloudfall_deploy_component": component,
    }


def test_s005_6_only_the_release_server_runs_the_command() -> None:
    component = _component({"command": ["bin/migrate"]}, server="b")

    assert _runs(RUN, _variables(component, "b"))
    assert not _runs(RUN, _variables(component, "a"))
    assert _runs(REQUIRE, _variables(component, "a"))
    assert _runs(REQUIRE, _variables(component, "b"))


def test_s005_10_a_component_without_release_runs_no_new_task() -> None:
    component = _component(None)

    for server in ("a", "b"):
        assert not _runs(RUN, _variables(component, server))
        assert not _runs(REQUIRE, _variables(component, server))


def test_s005_6_a_server_whose_release_server_has_not_passed_does_not_switch() -> None:
    task = next(t for t in _main_tasks() if t["name"] == REQUIRE)
    assertion = task["ansible.builtin.assert"]
    assert isinstance(assertion, dict)
    task_vars = task["vars"]
    assert isinstance(task_vars, dict)

    def passes(hostvars: dict[str, object]) -> bool:
        variables: dict[str, object] = {
            "hostvars": hostvars,
            "cloudfall_deploy_component": _component(
                {"command": ["bin/migrate"]}, server="b"
            ),
        }
        evidence = Templar(loader=DataLoader(), variables=variables).template(
            trust_as_template(task_vars["cloudfall_deploy_release_evidence"])
        )
        variables["cloudfall_deploy_release_evidence"] = evidence
        return all(_holds(c, variables) for c in assertion["that"])

    assert passes({"b": {"cloudfall_deploy_release_command_result": {"exitCode": 0}}})
    assert not passes({"b": {}})
    assert not passes(
        {"b": {"cloudfall_deploy_release_command_result": {"exitCode": 1}}}
    )


# The release receipt.


def _receipt(
    component: dict[str, object], server: str, hostvars: dict[str, object]
) -> dict[str, object]:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "release_receipt.yml"))
    assert isinstance(tasks, list)
    task = next(t for t in tasks if t["name"] == RECEIPT)
    copy = task["ansible.builtin.copy"]
    assert isinstance(copy, dict)
    variables: dict[str, object] = {
        "cloudfall_deploy_receipt_passed": True,
        "hostvars": hostvars,
        "cloudfall_server_id": server,
        "cloudfall_deploy_component": component,
        "cloudfall_deploy_release": "20261027T000000Z-abcdef1",
        "cloudfall_deploy_artifact_sha256": "a" * 64,
        "cloudfall_deploy_previous_directory": "",
        "ansible_facts": {"date_time": {"iso8601": "2026-10-27T00:00:00Z"}},
    }
    rendered = Templar(loader=DataLoader(), variables=variables).template(
        trust_as_template(copy["content"])
    )
    receipt = cast("dict[str, object]", json.loads(str(rendered)))
    _validate_receipt(receipt)
    return receipt


def _validate_receipt(receipt: dict[str, object]) -> None:
    SchemaCatalog(SCHEMAS).validate_named("release-receipt.schema.json", receipt)


RESULT_ON_B: dict[str, object] = {
    "server": "b",
    "command": ["bin/migrate"],
    "workingDirectory": None,
    "exitCode": 0,
    "outputTail": "migrated",
}


def test_s005_6_every_receipt_names_the_release_server() -> None:
    component = _component({"command": ["bin/migrate"]}, server="b")
    hostvars: dict[str, object] = {
        "a": {},
        "b": {"cloudfall_deploy_release_command_result": RESULT_ON_B},
    }

    for server in ("b", "a"):
        spec = _receipt(component, server, hostvars)["spec"]
        assert isinstance(spec, dict)
        assert spec["server"] == server
        assert spec["releaseCommand"] == RESULT_ON_B


def test_s005_10_a_receipt_without_release_has_no_release_command() -> None:
    spec = _receipt(_component(None), "a", {"a": {}})["spec"]

    assert isinstance(spec, dict)
    assert "releaseCommand" not in spec
    assert set(spec) == {
        "component",
        "server",
        "release",
        "artifactSha256",
        "deployedAt",
        "previousRelease",
        "healthCheckPassed",
    }


def test_s005_7_a_receipt_refuses_an_output_tail_over_2000_characters() -> None:
    component = _component({"command": ["bin/migrate"]}, server="b")
    hostvars: dict[str, object] = {
        "b": {
            "cloudfall_deploy_release_command_result": {
                **RESULT_ON_B,
                "outputTail": "x" * 2001,
            }
        }
    }

    with pytest.raises(ValidationError, match="too long"):
        _receipt(component, "b", hostvars)


# deploy.yml's host order, run for real against two local hosts.

STUB_ROLE = """---
- name: Record what this host did
  ansible.builtin.lineinfile:
    path: "{{ stub_log }}"
    line: >-
      {{ cloudfall_server_id }}:{{
        'release-command'
        if cloudfall_deploy_component.release is defined
        and cloudfall_deploy_component.release.server == cloudfall_server_id
        else 'switch'
      }}
    create: true
    mode: "0644"
  delegate_to: localhost

- name: Fail where the test asks
  ansible.builtin.fail:
    msg: stub failure on {{ cloudfall_server_id }}
  when: cloudfall_server_id == stub_fail_on | default('')
"""


def _two_host_project(tmp_path: Path, *, release: bool) -> Path:
    """Copy the example project with ``deployment.servers: [h2, h1]``."""
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    path = project / "components" / "crm-backend.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["spec"]["deployment"]["servers"] = ["h2", "h1"]
    if release:
        document["spec"]["release"] = {"command": ["bin/migrate"]}
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return project


def _deploy(
    tmp_path: Path, *, release: bool, fail_on: str = ""
) -> tuple[int, list[str]]:
    state = validate_config(_two_host_project(tmp_path, release=release), SCHEMAS)
    rendered = render_ansible_inventory(PlatformInventory.from_state(state))
    hosts = rendered["all"]["children"]["cloudfall_servers"]["hosts"]  # type: ignore[index]
    assert list(hosts) == ["h1", "h2"]
    for host in hosts.values():
        host.update(
            {
                "ansible_connection": "local",
                "ansible_python_interpreter": sys.executable,
                "ansible_become": False,
            }
        )
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(rendered), encoding="utf-8")
    roles = tmp_path / "roles"
    (roles / "cloudfall_deploy" / "tasks").mkdir(parents=True)
    (roles / "cloudfall_deploy" / "tasks" / "main.yml").write_text(
        STUB_ROLE, encoding="utf-8"
    )
    log = tmp_path / "hosts.log"
    exit_code = main(
        [
            "playbook",
            "run",
            "deploy",
            "--engine",
            str(ENGINE),
            "--inventory",
            str(inventory),
            "--roles",
            str(roles),
            "--extra-vars",
            json.dumps(
                {
                    "cloudfall_deploy_component_id": "crm-backend",
                    "cloudfall_deploy_release": "20261027T000000Z-abcdef1",
                    "cloudfall_deploy_artifact_archive": "/dev/null",
                    "cloudfall_deploy_artifact_sha256": "a" * 64,
                    "stub_log": str(log),
                    "stub_fail_on": fail_on,
                    "ansible_become": False,
                }
            ),
        ]
    )
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return exit_code, lines


def test_s005_6_the_release_server_deploys_first_and_runs_the_command_once(
    tmp_path: Path,
) -> None:
    """``deployment.servers: [h2, h1]`` against inventory order h1, h2."""
    exit_code, lines = _deploy(tmp_path, release=True)

    assert exit_code == 0
    assert lines == ["h2:release-command", "h1:switch"]


def test_s005_4_a_failed_release_server_stops_every_other_server(
    tmp_path: Path,
) -> None:
    exit_code, lines = _deploy(tmp_path, release=True, fail_on="h2")

    assert exit_code == 82
    assert lines == ["h2:release-command"]


def test_s005_10_a_component_without_release_deploys_in_inventory_order(
    tmp_path: Path,
) -> None:
    exit_code, lines = _deploy(tmp_path, release=False)

    assert exit_code == 0
    assert lines == ["h1:switch", "h2:switch"]
