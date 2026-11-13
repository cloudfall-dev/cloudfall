"""S-015: baseline converge delivers each host's channel token, or stops first."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from examples_project import copy_examples

CLOUDFALL = Path(sys.executable).with_name("cloudfall")
TOKEN = "123456:synthetic-bot-token"  # noqa: S105 - a synthetic test value
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
FAKE_SOPS = f"""#!{sys.executable}
import sys
args = sys.argv[1:]
if args[0] == "--decrypt":
    lines = open(args[1], encoding="utf-8").read().splitlines()
    sys.stdout.write("".join(l + "\\n" for l in lines if not l.startswith("sops_")))
else:
    sys.stdout.write(sys.stdin.read() + "sops_mac=fake\\n")
"""
FAKE_PLAYBOOK = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("ansible-playbook [core 2.21.0]")
    sys.exit(0)
extra = json.loads(args[args.index("--extra-vars") + 1])
seen = {{}}
for server, channel in extra.get("cloudfall_alert_channels", {{}}).items():
    source = channel["tokenSource"]
    seen[server] = {{
        **channel,
        "token": open(source, encoding="utf-8").read(),
        "mode": oct(os.stat(source).st_mode & 0o777),
    }}
json.dump({{"playbook": args[-1], "channels": seen}},
          open(os.environ["CLOUDFALL_TEST_RECORD"], "w"))
"""


def _project(tmp_path: Path) -> tuple[Path, dict[str, str]]:
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
    (project / ".sops.yaml").write_text(
        "creation_rules:\n  - path_regex: secrets/.*\\.env$\n    age: age1example\n",
        encoding="utf-8",
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, script in (("sops", FAKE_SOPS), ("ansible-playbook", FAKE_PLAYBOOK)):
        (tools / name).write_text(script, encoding="utf-8")
        (tools / name).chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "CLOUDFALL_TEST_TOKEN": TOKEN,
        "CLOUDFALL_TEST_RECORD": str(tmp_path / "record.json"),
    }
    return project, environment


def _cloudfall(
    project: Path, environment: dict[str, str], *argv: str
) -> tuple[int, dict[str, object]]:
    done = subprocess.run(  # noqa: S603 - the installed CLI, fixed arguments
        [str(CLOUDFALL), *argv, "--project", str(project)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        stdin=subprocess.DEVNULL,
        timeout=300,
    )
    assert TOKEN not in done.stdout + done.stderr
    return done.returncode, json.loads(done.stdout)


def test_the_plan_names_each_host_and_whether_its_token_is_stored(
    tmp_path: Path,
) -> None:
    project, environment = _project(tmp_path)

    code, envelope = _cloudfall(project, environment, "baseline", "converge")

    assert code == 0
    data = envelope["data"]
    assert isinstance(data, dict)
    assert data["status"] == "plan"
    assert data["channels"] == [
        {"server": "h1", "channel": "operations", "token_stored": False},
        {"server": "h2", "channel": "operations", "token_stored": False},
    ]


def test_s015_3_a_missing_token_stops_before_any_server(tmp_path: Path) -> None:
    project, environment = _project(tmp_path)

    code, envelope = _cloudfall(project, environment, "baseline", "converge", "--yes")

    assert code == 4
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["context"]["code"] == "INPUT_REQUIRED"
    assert error["context"]["missing"] == ["operations"]
    assert not (tmp_path / "record.json").exists()


def test_s015_3_the_token_reaches_each_host_in_a_private_file(
    tmp_path: Path,
) -> None:
    project, environment = _project(tmp_path)
    code, _ = _cloudfall(
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
    assert code == 0

    code, envelope = _cloudfall(project, environment, "baseline", "converge", "--yes")

    assert code == 0, envelope
    record = json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))
    assert record["playbook"].endswith("baseline.yml")
    channels = record["channels"]
    assert set(channels) == {"h1", "h2"}
    for channel in channels.values():
        assert (channel["id"], channel["chatId"]) == ("operations", "145629422")
        assert (channel["token"], channel["mode"]) == (TOKEN, "0o600")
        assert not Path(channel["tokenSource"]).exists()
