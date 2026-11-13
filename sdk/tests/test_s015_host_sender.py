"""S-015: the host's sender, and the role that installs and wires it."""

from __future__ import annotations

import grp
import json
import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_alerting"
TOKEN = "123456:synthetic-bot-token"  # noqa: S105 - a synthetic test value
FAKE_CURL = """#!/bin/sh
printf '%s\\n' "$@" > "$RECORD/argv"
cat > "$RECORD/stdin"
"""
FAKE_LOGGER = """#!/bin/sh
printf '%s\\n' "$*" >> "$RECORD/log"
"""
FAKE_HOSTNAME = """#!/bin/sh
echo host.example.internal
"""


def _play(tmp_path: Path, *, token: bool = True) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "host"
    (root / "etc").mkdir(parents=True)
    mdadm = root / "etc" / "mdadm.conf"
    mdadm.write_text("ARRAY /dev/md0 metadata=1.2\nMAILADDR root\n", encoding="utf-8")
    smartd = root / "etc" / "smartd.conf"
    smartd.write_text(
        "# default\nDEVICESCAN -d removable -n standby -m root "
        "-M exec /usr/share/smartmontools/smartd-runner\n",
        encoding="utf-8",
    )
    source = tmp_path / "staged-token"
    source.write_text(TOKEN, encoding="utf-8")
    source.chmod(0o600)
    paths = {
        "cloudfall_alerting_root": str(root / "alerting"),
        "cloudfall_alerting_sender": str(root / "cloudfall-alert"),
        "cloudfall_alerting_mdadm_conf": str(mdadm),
        "cloudfall_alerting_smartd_conf": str(smartd),
        "cloudfall_alerting_owner": pwd.getpwuid(os.getuid()).pw_name,
        "cloudfall_alerting_group": grp.getgrgid(os.getgid()).gr_name,
    }
    channel: dict[str, str] = {"id": "operations", "chatId": "145629422"}
    if token:
        channel["tokenSource"] = str(source)
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": paths,
                    "roles": [
                        {
                            "role": str(ROLE),
                            "cloudfall_alerting_channel": channel,
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    return playbook, paths


def _run(playbook: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
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
        check=check,
        timeout=300,
    )


def test_s015_3_the_role_installs_the_sender_and_wires_both_sources(
    tmp_path: Path,
) -> None:
    playbook, paths = _play(tmp_path)

    first = _run(playbook)
    second = _run(playbook)

    channel = Path(paths["cloudfall_alerting_root"]) / "operations"
    token = channel / "bot-token"
    assert token.read_text(encoding="utf-8") == TOKEN
    assert token.stat().st_mode & 0o777 == 0o640
    assert (channel / "chat-id").read_text(encoding="utf-8") == "145629422\n"
    sender = Path(paths["cloudfall_alerting_sender"])
    assert sender.stat().st_mode & 0o777 == 0o755
    mdadm = Path(paths["cloudfall_alerting_mdadm_conf"]).read_text(encoding="utf-8")
    assert f"PROGRAM {sender}\n" in mdadm
    smartd = Path(paths["cloudfall_alerting_smartd_conf"]).read_text(encoding="utf-8")
    assert f"-M exec {sender}\n" in smartd
    assert "smartd-runner" not in smartd
    assert TOKEN not in first.stdout + second.stdout
    assert "changed=0" in second.stdout


def test_s015_3_without_a_token_the_role_stops(tmp_path: Path) -> None:
    playbook, paths = _play(tmp_path, token=False)

    done = _run(playbook, check=False)

    assert done.returncode != 0
    assert "converge through cloudfall" in done.stdout
    assert not Path(paths["cloudfall_alerting_sender"]).exists()


@pytest.mark.parametrize(
    ("argv", "environment", "expected"),
    [
        (["disk check"], {}, "[host.example.internal] disk check"),
        (
            ["Fail", "/dev/md1", "/dev/nvme0n1p2"],
            {},
            "[host.example.internal] mdadm Fail on /dev/md1 (/dev/nvme0n1p2)",
        ),
        (
            [],
            {
                "SMARTD_DEVICE": "/dev/nvme0",
                "SMARTD_FAILTYPE": "Health",
                "SMARTD_MESSAGE": "worn out",
            },
            "[host.example.internal] smartd Health on /dev/nvme0: worn out",
        ),
    ],
)
def test_s015_6_the_sender_keeps_the_token_out_of_argv(
    tmp_path: Path, argv: list[str], environment: dict[str, str], expected: str
) -> None:
    playbook, paths = _play(tmp_path)
    _run(playbook)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, script in (
        ("curl", FAKE_CURL),
        ("logger", FAKE_LOGGER),
        ("hostname", FAKE_HOSTNAME),
    ):
        (tools / name).write_text(script, encoding="utf-8")
        (tools / name).chmod(0o755)
    record = tmp_path / "record"
    record.mkdir()

    subprocess.run(  # noqa: S603 - the sender the role installed
        [paths["cloudfall_alerting_sender"], *argv],
        check=True,
        timeout=60,
        env={
            "PATH": f"{tools}{os.pathsep}/usr/bin:/bin",
            "RECORD": str(record),
            **environment,
        },
    )

    sent = (record / "argv").read_text(encoding="utf-8")
    assert TOKEN not in sent
    assert f"text={expected}" in sent
    assert "chat_id=145629422" in sent
    assert TOKEN in (record / "stdin").read_text(encoding="utf-8")
    assert TOKEN not in (record / "log").read_text(encoding="utf-8")
