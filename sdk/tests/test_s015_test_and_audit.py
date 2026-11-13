"""S-015: alerts test is a channel's verify step, and audit checks the channel."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cloudfall.audit import audit_inventory
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.validation import validate_config
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"
CLOUDFALL = Path(sys.executable).with_name("cloudfall")
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
SENDER = "/usr/local/sbin/cloudfall-alert"
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
FAKE_PLAYBOOK = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("ansible-playbook [core 2.21.0]")
    sys.exit(0)
extra = json.loads(args[args.index("--extra-vars") + 1])
limit = args[args.index("--limit") + 1] if "--limit" in args else None
json.dump({{"playbook": args[-1], "limit": limit, "extra": extra}},
          open(os.environ["CLOUDFALL_TEST_RECORD"], "w"))
"""


def _project(tmp_path: Path) -> Path:
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
    return project


def _cloudfall(tmp_path: Path, project: Path, *argv: str) -> tuple[int, dict]:  # type: ignore[type-arg]
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    (tools / "ansible-playbook").write_text(FAKE_PLAYBOOK, encoding="utf-8")
    (tools / "ansible-playbook").chmod(0o755)
    done = subprocess.run(  # noqa: S603 - the installed CLI, fixed arguments
        [str(CLOUDFALL), *argv, "--project", str(project)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "CLOUDFALL_TEST_RECORD": str(tmp_path / "record.json"),
        },
        timeout=300,
    )
    return done.returncode, json.loads(done.stdout)


def test_s015_4_the_plan_names_the_channel_and_sends_nothing(tmp_path: Path) -> None:
    code, envelope = _cloudfall(tmp_path, _project(tmp_path), "alerts", "test", "h2")

    assert code == 0
    assert envelope["data"]["status"] == "plan"
    assert envelope["data"]["channel"] == "operations"
    assert not (tmp_path / "record.json").exists()


def test_s015_4_the_message_goes_to_that_server_only(tmp_path: Path) -> None:
    code, envelope = _cloudfall(
        tmp_path, _project(tmp_path), "alerts", "test", "h2", "--yes"
    )

    assert code == 0, envelope
    record = json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))
    assert record["playbook"].endswith("alerts-test.yml")
    assert record["limit"] == "h2"
    message = record["extra"]["cloudfall_alert_test_message"]
    assert message.startswith("Test alert from cloudfall for h2 on channel operations")
    assert envelope["data"]["message"] == message


@pytest.mark.parametrize("server", ["all", "ungrouped"])
def test_a_server_named_as_an_ansible_implicit_group_is_refused(
    tmp_path: Path, server: str
) -> None:
    """#283: ``--limit all`` would reach every host, ``ungrouped`` none."""
    project = _project(tmp_path)
    h2 = (project / "servers" / "h2.yaml").read_text(encoding="utf-8")
    (project / "servers" / f"{server}.yaml").write_text(
        h2.replace("id: h2", f"id: {server}")
        .replace("h2.example.internal", f"{server}.example.internal")
        .replace("203.0.113.12", "203.0.113.99")
        .replace("2001:db8:12::/64", "2001:db8:99::/64"),
        encoding="utf-8",
    )

    code, envelope = _cloudfall(tmp_path, project, "alerts", "test", server, "--yes")

    assert code == 4, envelope
    assert envelope["error"]["context"]["code"] == "lifecycle_limit_reserved_group"
    assert not (tmp_path / "record.json").exists()


def test_s015_4_a_server_without_a_channel_is_refused(tmp_path: Path) -> None:
    project = copy_examples(tmp_path / "project")

    code, envelope = _cloudfall(tmp_path, project, "alerts", "test", "h1", "--yes")

    assert code == 4
    assert envelope["error"]["context"]["code"] == "alert_channel_undeclared"


def _evidence(**changes: object) -> dict[str, object]:
    return {
        "channel": "operations",
        "sender": SENDER,
        "senderInstalled": True,
        "tokenPresent": True,
        "chatIdPresent": True,
        "mdadmProgram": f"PROGRAM {SENDER}",
        "mdadmConfigured": True,
        "smartdScan": f"DEVICESCAN -d removable -n standby -m root -M exec {SENDER}",
        "smartdConfigured": True,
        **changes,
    }


def _channel_status(tmp_path: Path, evidence: dict[str, object] | None) -> str:
    project = _project(tmp_path)
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    document = json.loads(h1.read_text(encoding="utf-8"))
    if evidence is not None:
        document["spec"]["alertChannel"] = evidence
    h1.write_text(json.dumps(document), encoding="utf-8")
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))
    server = next(item for item in report.servers if item.server_id == "h1")
    check = next(item for item in server.checks if item.check == "alerting.channel")
    return check.status.value


def test_s015_7_a_wired_host_audits_compliant(tmp_path: Path) -> None:
    assert _channel_status(tmp_path, _evidence()) == "compliant"


@pytest.mark.parametrize(
    "changes",
    [
        {"senderInstalled": False},
        {"tokenPresent": False},
        {"chatIdPresent": False},
        {"mdadmProgram": None},
        {"mdadmProgram": "PROGRAM /usr/share/mdadm/other"},
        {"smartdScan": "DEVICESCAN -d removable -n standby -m root"},
        {"smartdScan": f"DEVICESCAN -m root -M exec {SENDER}-old"},
    ],
)
def test_s015_7_missing_pieces_are_drift(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    assert _channel_status(tmp_path, _evidence(**changes)) == "drift"


def test_s015_7_a_host_without_mdadm_or_smartd_needs_neither(tmp_path: Path) -> None:
    evidence = _evidence(
        mdadmProgram=None,
        mdadmConfigured=False,
        smartdScan=None,
        smartdConfigured=False,
    )
    assert _channel_status(tmp_path, evidence) == "compliant"


def test_s015_7_no_evidence_is_unknown(tmp_path: Path) -> None:
    assert _channel_status(tmp_path, None) == "unknown"


def test_inspect_reads_the_channel_files_but_not_the_token(tmp_path: Path) -> None:
    host = tmp_path / "host"
    channel = host / "alerting" / "operations"
    channel.mkdir(parents=True)
    (channel / "bot-token").write_text("123456:synthetic", encoding="utf-8")
    sender = host / "cloudfall-alert"
    sender.write_text("#!/bin/sh\n", encoding="utf-8")
    mdadm = host / "mdadm.conf"
    mdadm.write_text(f"ARRAY /dev/md0\nPROGRAM {sender}\n", encoding="utf-8")
    output = tmp_path / "out.json"
    playbook = tmp_path / "play.yml"
    tasks = ROOT / "engine/ansible/roles/cloudfall_inspect/tasks/alert_channel.yml"
    playbook.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "cloudfall_inspect_snapshot": {"spec": {"server": "h1"}},
                        "cloudfall_inspect_alert_channel": "operations",
                        "cloudfall_inspect_alerting_root": str(host / "alerting"),
                        "cloudfall_inspect_alerting_sender": str(sender),
                        "cloudfall_inspect_mdadm_conf": str(mdadm),
                        "cloudfall_inspect_smartd_conf": str(host / "smartd.conf"),
                    },
                    "tasks": [
                        {"ansible.builtin.include_tasks": str(tasks)},
                        {
                            "ansible.builtin.copy": {
                                "content": "{{ cloudfall_inspect_snapshot | to_json }}",
                                "dest": str(output),
                            }
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    done = subprocess.run(  # noqa: S603 - the venv's ansible-playbook
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
        check=True,
        timeout=300,
    )

    evidence = json.loads(output.read_text(encoding="utf-8"))["spec"]["alertChannel"]
    assert evidence == {
        "channel": "operations",
        "sender": str(sender),
        "senderInstalled": True,
        "tokenPresent": True,
        "chatIdPresent": False,
        "mdadmProgram": f"PROGRAM {sender}",
        "mdadmConfigured": True,
        "smartdScan": None,
        "smartdConfigured": False,
    }
    assert "123456:synthetic" not in done.stdout + output.read_text(encoding="utf-8")
