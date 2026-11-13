"""S-015: the host's sender, and the role that installs and wires it."""

from __future__ import annotations

import grp
import importlib.util
import json
import os
import pwd
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable

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
NO_FQDN_HOSTNAME = """#!/bin/sh
if [ "$1" = -f ]; then
  echo "hostname: Name or service not known" >&2
  exit 1
fi
echo host
"""


DEBIAN_SMARTD = (
    "# default\nDEVICESCAN -d removable -n standby -m root "
    "-M exec /usr/share/smartmontools/smartd-runner\n"
)


def _play(
    tmp_path: Path, *, token: bool = True, smartd_conf: str = DEBIAN_SMARTD
) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "host"
    (root / "etc").mkdir(parents=True)
    mdadm = root / "etc" / "mdadm.conf"
    mdadm.write_text("ARRAY /dev/md0 metadata=1.2\nMAILADDR root\n", encoding="utf-8")
    smartd = root / "etc" / "smartd.conf"
    smartd.write_text(smartd_conf, encoding="utf-8")
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
        # Units no test host has: the handlers skip them rather than restart
        # the runner's own mdmonitor or smartd.
        "cloudfall_alerting_mdadm_unit": "cloudfall-test-absent-mdmonitor.service",
        "cloudfall_alerting_smartd_unit": "cloudfall-test-absent-smartd.service",
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
    ("scan", "expected"),
    [
        (
            "DEVICESCAN -a -s (S/../.././02|L/../../6/03) -W 4,45,55 -m root "
            "-M exec /usr/share/smartmontools/smartd-runner",
            "DEVICESCAN -a -s (S/../.././02|L/../../6/03) -W 4,45,55 -m root "
            "-M exec {sender}",
        ),
        (
            "DEVICESCAN -a -M exec /opt/elsewhere/notify -M daily -m ops@example.com",
            "DEVICESCAN -a -M daily -m ops@example.com -M exec {sender}",
        ),
        (
            "DEVICESCAN -a -s L/../../7/04 -W 0,0,60",
            "DEVICESCAN -a -s L/../../7/04 -W 0,0,60 -m root -M exec {sender}",
        ),
        (
            "",
            "DEVICESCAN -d removable -n standby -m root -M exec {sender}",
        ),
    ],
    ids=["keeps-s-and-W", "replaces-foreign-exec", "adds-m-root", "no-scan-line"],
)
def test_s015_3_the_role_keeps_the_hosts_smartd_options(
    tmp_path: Path, scan: str, expected: str
) -> None:
    conf = f"# host's own\n{scan}\n" if scan else "# host's own\n"
    playbook, paths = _play(tmp_path, smartd_conf=conf)

    _run(playbook)
    second = _run(playbook)

    smartd = Path(paths["cloudfall_alerting_smartd_conf"]).read_text(encoding="utf-8")
    sender = paths["cloudfall_alerting_sender"]
    assert smartd == f"# host's own\n{expected.format(sender=sender)}\n"
    assert "changed=0" in second.stdout


def _smartd_scan() -> Callable[[str, str], str]:
    path = ROLE / "filter_plugins" / "smartd_scan.py"
    spec = importlib.util.spec_from_file_location("smartd_scan", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scan: Callable[[str, str], str] = module.smartd_scan
    return scan


def test_s015_3_the_scan_line_keeps_a_trailing_comment() -> None:
    line = _smartd_scan()("DEVICESCAN -a -m root # weekly tests\n", "/s")

    assert line == "DEVICESCAN -a -m root -M exec /s # weekly tests"
    assert _smartd_scan()(f"{line}\n", "/s") == line


@pytest.mark.parametrize(
    ("conf", "message"),
    [
        ("DEVICESCAN -a\nDEVICESCAN -m root\n", "more than one DEVICESCAN"),
        ("DEVICESCAN -a \\\n  -m root\n", "continued"),
        ("DEVICESCAN -a -M exec\n", "-M exec without a program"),
    ],
)
def test_s015_3_a_scan_line_the_role_cannot_read_stops_it(
    conf: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _smartd_scan()(conf, "/s")


def test_s015_3_a_failed_restart_fails_the_run() -> None:
    handlers = yaml.safe_load((ROLE / "handlers" / "main.yml").read_text("utf-8"))

    for handler in handlers:
        assert "failed_when" not in handler
        assert "ignore_errors" not in handler
        assert "in ansible_facts.services" in handler["when"]


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


def test_the_sender_still_sends_when_the_host_has_no_fqdn(
    tmp_path: Path,
) -> None:
    playbook, paths = _play(tmp_path)
    _run(playbook)
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, script in (
        ("curl", FAKE_CURL),
        ("logger", FAKE_LOGGER),
        ("hostname", NO_FQDN_HOSTNAME),
    ):
        (tools / name).write_text(script, encoding="utf-8")
        (tools / name).chmod(0o755)
    record = tmp_path / "record"
    record.mkdir()

    subprocess.run(  # noqa: S603 - the sender the role installed
        [paths["cloudfall_alerting_sender"], "Fail", "/dev/md1"],
        check=True,
        timeout=60,
        env={"PATH": f"{tools}{os.pathsep}/usr/bin:/bin", "RECORD": str(record)},
    )

    sent = (record / "argv").read_text(encoding="utf-8")
    assert "text=[host] mdadm Fail on /dev/md1" in sent
