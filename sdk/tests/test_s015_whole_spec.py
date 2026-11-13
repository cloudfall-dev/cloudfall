"""S-015: the whole-spec checks, the token's surfaces and a host with no channel."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from cloudfall.audit import audit_inventory
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from examples_project import copy_examples
from test_logging_deploy import FAKE_PLAYBOOK as LOGGING_PLAYBOOK
from test_logging_deploy import FAKE_SOPS

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"
ENGINE = ROOT / "engine" / "ansible"
CLOUDFALL = Path(sys.executable).with_name("cloudfall")
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
TOKEN = "987654:sentinel-bot-token-s015-6"  # noqa: S105 - a synthetic sentinel
SMTP = "synthetic-smtp-value"
CHANNEL = """---
apiVersion: cloudfall/v1
kind: AlertChannel
metadata:
  id: operations
  description: The operators' chat
spec:
  telegram:
    chatId: "145629422"
"""
RECEIVER = "      - id: chat\n        channel: operations\n"
# Records each call's argv and the inventory it was handed, then answers as
# test_logging_deploy's stand-in does
RECORDING_PLAYBOOK = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
call = {{"argv": sys.argv, "inventory": None}}
if "--inventory" in args:
    with open(args[args.index("--inventory") + 1], encoding="utf-8") as handle:
        call["inventory"] = handle.read()
with open(os.environ["CLOUDFALL_TEST_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(call) + "\\n")
answer = os.path.join(os.path.dirname(sys.argv[0]), "answering-playbook")
os.execv(answer, [answer, *args])
"""


def _channel_project(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    project = copy_examples(tmp_path / "project")
    (project / "alert-channels").mkdir()
    (project / "alert-channels" / "operations.yaml").write_text(
        CHANNEL, encoding="utf-8"
    )
    baseline = project / "baselines" / "debian-application.yaml"
    baseline.write_text(
        baseline.read_text(encoding="utf-8") + "  alerting:\n    channel: operations\n",
        encoding="utf-8",
    )
    stack = project / "logging-stacks" / "operations.yaml"
    text = stack.read_text(encoding="utf-8")
    receivers = "    receivers:\n"
    assert receivers in text
    stack.write_text(
        text.replace(receivers, receivers + RECEIVER),
        encoding="utf-8",
    )
    (project / ".sops.yaml").write_text(
        "creation_rules:\n  - path_regex: secrets/.*\\.env$\n    age: age1example\n",
        encoding="utf-8",
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, script in (
        ("sops", FAKE_SOPS),
        ("ansible-playbook", RECORDING_PLAYBOOK),
        ("answering-playbook", LOGGING_PLAYBOOK),
    ):
        (tools / name).write_text(script, encoding="utf-8")
        (tools / name).chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "CLOUDFALL_TEST_TOKEN": TOKEN,
        "CLOUDFALL_TEST_SMTP": SMTP,
        "CLOUDFALL_TEST_RECORD": str(tmp_path / "record.json"),
        "CLOUDFALL_TEST_CALLS": str(tmp_path / "calls.jsonl"),
    }
    return project, environment


def _answer(project: Path, environment: dict[str, str], *argv: str) -> tuple[int, str]:
    done = subprocess.run(  # noqa: S603 - the installed CLI, fixed arguments
        [str(CLOUDFALL), *argv, "--project", str(project)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        stdin=subprocess.DEVNULL,
        timeout=300,
    )
    return done.returncode, done.stdout + done.stderr


def _token_states(project: Path, environment: dict[str, str]) -> dict[str, set[str]]:
    code, answer = _answer(project, environment, "requirements", "show")
    assert code == 0, answer
    return {
        server["server"]: {
            item["state"]
            for item in server["requirements"]
            if item["name"] == "telegram-bot-token"
        }
        for server in json.loads(answer)["data"]["servers"]
    }


def test_s015_2_after_secrets_input_the_token_is_met(tmp_path: Path) -> None:
    project, environment = _channel_project(tmp_path)
    assert _token_states(project, environment) == {
        "h1": {"needs-input"},
        "h2": {"needs-input"},
    }
    code, answer = _answer(
        project,
        environment,
        "secrets",
        "input",
        "telegram-bot-token",
        "--owner",
        "operations",
        "--value-from-env",
        "CLOUDFALL_TEST_TOKEN",
    )
    assert code == 0, answer

    assert _token_states(project, environment) == {"h1": {"met"}, "h2": {"met"}}


def test_s015_6_the_token_reaches_no_argv_inventory_answer_or_record(
    tmp_path: Path,
) -> None:
    project, environment = _channel_project(tmp_path)
    answers: list[str] = []
    for owner, name, variable in (
        ("operations", "telegram-bot-token", "CLOUDFALL_TEST_TOKEN"),
        ("operations", "smtp-password-operations-email", "CLOUDFALL_TEST_SMTP"),
    ):
        code, answer = _answer(
            project,
            environment,
            "secrets",
            "input",
            name,
            "--owner",
            owner,
            "--value-from-env",
            variable,
        )
        assert code == 0, answer
        answers.append(answer)

    for argv in (
        ("requirements", "show"),
        ("baseline", "converge"),
        ("baseline", "converge", "--yes"),
        ("logging", "deploy"),
        ("logging", "deploy", "--yes"),
        ("alerts", "test", "h1"),
        ("alerts", "test", "h1", "--yes"),
    ):
        code, answer = _answer(project, environment, *argv)
        assert code == 0, (argv, answer)
        answers.append(answer)

    calls = [
        json.loads(line)
        for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    playbooks = [Path(call["argv"][-1]).name for call in calls]
    assert {"baseline.yml", "logging.yml", "alerts-test.yml"} <= set(playbooks)
    # The token did reach the run, through a staged file only
    record = json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))
    assert record["playbook"].endswith("alerts-test.yml")
    for call in calls:
        assert TOKEN not in " ".join(call["argv"]), call["argv"][-1]
        assert call["inventory"] is not None
        assert TOKEN not in call["inventory"], call["argv"][-1]
    for answer in answers:
        assert TOKEN not in answer
    # The stand-in sops keeps values in clear, so the store is the one place
    store = project / "secrets" / "production" / "alert-channels" / "operations.env"
    assert TOKEN in store.read_text(encoding="utf-8")
    written = [
        path
        for path in project.rglob("*")
        if path.is_file() and "secrets" not in path.relative_to(project).parts
    ]
    assert project / "tmp" / "ansible-inventory.json" in written
    for path in written:
        assert TOKEN not in path.read_text(encoding="utf-8", errors="replace"), path


def test_s015_6_the_token_is_handed_over_staged_and_then_removed(
    tmp_path: Path,
) -> None:
    project, environment = _channel_project(tmp_path)
    code, answer = _answer(
        project,
        environment,
        "secrets",
        "input",
        "telegram-bot-token",
        "--owner",
        "operations",
        "--value-from-env",
        "CLOUDFALL_TEST_TOKEN",
    )
    assert code == 0, answer

    code, answer = _answer(project, environment, "baseline", "converge", "--yes")

    assert code == 0, answer
    call = json.loads((tmp_path / "calls.jsonl").read_text(encoding="utf-8"))
    extra = json.loads(call["argv"][call["argv"].index("--extra-vars") + 1])
    channels = extra["cloudfall_alert_channels"].values()
    staged = [item["tokenSource"] for item in channels]
    assert len(staged) == 2
    assert not any(Path(source).exists() for source in staged)


def _plain_project(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    project = copy_examples(tmp_path / "project")
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, script in (
        ("ansible-playbook", RECORDING_PLAYBOOK),
        ("answering-playbook", LOGGING_PLAYBOOK),
    ):
        (tools / name).write_text(script, encoding="utf-8")
        (tools / name).chmod(0o755)
    environment = {
        **os.environ,
        # No sops on the path: a host without a channel reads no secret
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "CLOUDFALL_TEST_RECORD": str(tmp_path / "record.json"),
        "CLOUDFALL_TEST_CALLS": str(tmp_path / "calls.jsonl"),
    }
    return project, environment


def test_s015_8_a_host_without_a_channel_converges_with_no_channel(
    tmp_path: Path,
) -> None:
    project, environment = _plain_project(tmp_path)

    code, plan = _answer(project, environment, "baseline", "converge")
    assert code == 0, plan
    assert json.loads(plan)["data"]["channels"] == []
    code, answer = _answer(project, environment, "baseline", "converge", "--yes")

    assert code == 0, answer
    (call,) = [
        json.loads(line)
        for line in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert Path(call["argv"][-1]).name == "baseline.yml"
    extra = json.loads(call["argv"][call["argv"].index("--extra-vars") + 1])
    assert extra == {"cloudfall_alert_channels": {}}
    assert "alert_channel" not in call["inventory"]


def test_s015_8_a_channel_adds_one_host_variable_and_nothing_else(
    tmp_path: Path,
) -> None:
    plain = copy_examples(tmp_path / "plain")
    channelled, _ = _channel_project(tmp_path)
    stack = channelled / "logging-stacks" / "operations.yaml"
    stack.write_text(
        stack.read_text(encoding="utf-8").replace(RECEIVER, ""), encoding="utf-8"
    )

    def hosts(project: Path) -> dict[str, dict[str, object]]:
        inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
        rendered = render_ansible_inventory(inventory)
        found = rendered["all"]["children"]["cloudfall_servers"]["hosts"]  # type: ignore[index]
        assert isinstance(found, dict)
        return found

    before = hosts(plain)
    after = hosts(channelled)

    for variables in before.values():
        assert "cloudfall_alert_channel" not in variables
        assert "alerting" not in variables["cloudfall_baseline"]  # type: ignore[operator]
        assert "alerting" not in variables["cloudfall_server_type"]  # type: ignore[operator]
    for server, variables in after.items():
        assert variables.pop("cloudfall_alert_channel") == "operations"
        for key in ("cloudfall_baseline", "cloudfall_server_type"):
            declared = variables[key]
            assert isinstance(declared, dict)
            assert declared.pop("alerting") == {"channel": "operations"}
        assert variables == before[server]


def test_s015_8_baseline_skips_the_alerting_role_without_a_channel(
    tmp_path: Path,
) -> None:
    tasks = yaml.safe_load(
        (ENGINE / "playbooks" / "baseline.yml").read_text(encoding="utf-8")
    )[0]["tasks"]
    (alerting,) = [
        task
        for task in tasks
        if task.get("ansible.builtin.include_role", {}).get("name")
        == "cloudfall_alerting"
    ]

    def run(*variables: dict[str, object]) -> subprocess.CompletedProcess[str]:
        playbook = tmp_path / "play.yml"
        playbook.write_text(
            json.dumps(
                [
                    {
                        "hosts": "localhost",
                        "gather_facts": False,
                        "vars": {"cloudfall_server_id": "h1", **item},
                        "tasks": [alerting],
                    }
                    for item in variables
                ]
            ),
            encoding="utf-8",
        )
        return subprocess.run(  # noqa: S603 - the venv's ansible-playbook
            [
                str(ANSIBLE_PLAYBOOK),
                "--inventory",
                "localhost,",
                "--connection",
                "local",
                str(playbook),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
            env={**os.environ, "ANSIBLE_ROLES_PATH": str(ENGINE / "roles")},
        )

    skipped = run(
        {"cloudfall_alert_channels": {"h2": {"id": "operations"}}},
        {"cloudfall_alert_channels": {}},
        {},
    )
    # The same task does run the role for a host that has a channel
    ran = run({"cloudfall_alert_channels": {"h1": {"id": "operations"}}})

    assert skipped.returncode == 0, skipped.stdout
    assert skipped.stdout.count("skipping: [localhost]") == 3
    assert re.search(r"ok=0\s+changed=0\s+unreachable=0\s+failed=0", skipped.stdout)
    assert "TASK [cloudfall_alerting :" not in skipped.stdout
    assert "TASK [cloudfall_alerting :" in ran.stdout


def test_s015_8_a_host_without_a_channel_audits_as_before(tmp_path: Path) -> None:
    project = copy_examples(tmp_path / "project")
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))

    checks = [check for server in report.servers for check in server.checks]
    assert checks
    assert not [check for check in checks if check.check == "alerting.channel"]
    assert {check.status.value for check in checks} == {"compliant"}
