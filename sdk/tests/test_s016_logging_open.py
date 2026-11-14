"""S-016: ``logging open`` forwards a local port to the stack's loopback Grafana."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cloudfall.domain import ResourceId
from cloudfall.inventory import PlatformInventory
from cloudfall.logging_open import (
    ERROR_FORWARD_FAILED,
    ERROR_PORT_IN_USE,
    ERROR_SSH_MISSING,
    ERROR_STACK_UNDECLARED,
    ERROR_STACK_UNKNOWN,
    GrafanaForward,
    LoggingOpenError,
    chosen_stack,
    grafana_forward,
    require_free_port,
    start_forward,
)
from cloudfall.validation import validate_config
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
CLOUDFALL = Path(sys.executable).with_name("cloudfall")

# Stands in for ssh: listens on the -L local port until stopped, records its pid
FAKE_SSH = f"""#!{sys.executable}
import os, signal, socket, sys, time
forward = sys.argv[sys.argv.index("-L") + 1]
host, port = forward.split(":")[0], int(forward.split(":")[1])
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind((host, port))
listener.listen()
with open(os.environ["CLOUDFALL_TEST_SSH_PID"], "w") as record:
    record.write(str(os.getpid()))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(0.1)
"""


def _inventory() -> PlatformInventory:
    return PlatformInventory.from_state(validate_config(EXAMPLES, SCHEMAS))


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _forward(port: int) -> GrafanaForward:
    inventory = _inventory()
    return grafana_forward(inventory, chosen_stack(inventory, None), port)


def test_s016_5_the_command_uses_the_declared_backend_and_grafana() -> None:
    inventory = _inventory()
    stack = chosen_stack(inventory, None)
    server = next(
        item
        for item in inventory.servers
        if item.resource_id == stack.backend.server_id
    )

    forward = grafana_forward(inventory, stack, 3300)

    assert forward.argv == (
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-p",
        str(server.ssh_port.value),
        "-L",
        f"127.0.0.1:3300:{stack.grafana.listen_address.value}:{stack.grafana.port.value}",
        f"{server.ssh_user.value}@{server.address.value}",
    )
    assert forward.url == "http://127.0.0.1:3300/dashboards/f/cloudfall"


def test_s016_5_the_answer_names_the_password_file_never_the_password() -> None:
    inventory = _inventory()
    stack = chosen_stack(inventory, None)

    answer = grafana_forward(inventory, stack, 3000).as_dict()

    assert answer["adminPassword"] == {
        "file": (
            f"secrets/{stack.environment.value}/logging-stacks/"
            f"{stack.resource_id.value}.env"
        ),
        "key": "GRAFANA_ADMIN_PASSWORD",
    }
    assert set(answer) == {
        "loggingStack",
        "server",
        "localPort",
        "command",
        "url",
        "adminUser",
        "adminPassword",
    }


def test_a_stack_not_declared_is_not_found() -> None:
    with pytest.raises(LoggingOpenError) as raised:
        chosen_stack(_inventory(), ResourceId("nope"))

    assert raised.value.code == ERROR_STACK_UNKNOWN


def test_a_project_without_a_stack_is_refused(tmp_path: Path) -> None:
    project = copy_examples(tmp_path / "project")
    for directory in ("logging-stacks", "alert-rules", "operator-policies"):
        for path in (project / directory).iterdir():
            path.unlink()
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    with pytest.raises(LoggingOpenError) as raised:
        chosen_stack(inventory, None)

    assert raised.value.code == ERROR_STACK_UNDECLARED


def test_s016_5_a_local_port_in_use_is_refused() -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = int(holder.getsockname()[1])

        with pytest.raises(LoggingOpenError) as raised:
            require_free_port(port)

    assert raised.value.code == ERROR_PORT_IN_USE


def test_ssh_that_exits_before_the_forward_is_up_fails() -> None:
    forward = _forward(_free_port())
    failing = GrafanaForward(
        **{
            **{name: getattr(forward, name) for name in GrafanaForward.__slots__},
            "argv": (sys.executable, "-c", "raise SystemExit(255)"),
        }
    )

    with pytest.raises(LoggingOpenError) as raised:
        start_forward(failing, timeout=5)

    assert raised.value.code == ERROR_FORWARD_FAILED
    assert "255" in raised.value.message


def test_missing_ssh_is_named() -> None:
    forward = _forward(_free_port())
    missing = GrafanaForward(
        **{
            **{name: getattr(forward, name) for name in GrafanaForward.__slots__},
            "argv": ("/nonexistent/ssh",),
        }
    )

    with pytest.raises(LoggingOpenError) as raised:
        start_forward(missing, timeout=1)

    assert raised.value.code == ERROR_SSH_MISSING


def _run(tmp_path: Path, *argv: str) -> subprocess.Popen[str]:
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    (tools / "ssh").write_text(FAKE_SSH, encoding="utf-8")
    (tools / "ssh").chmod(0o755)
    project = copy_examples(tmp_path / "project")
    return subprocess.Popen(  # noqa: S603 - the installed CLI, fixed arguments
        [str(CLOUDFALL), "logging", "open", *argv, "--project", str(project)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "CLOUDFALL_TEST_SSH_PID": str(tmp_path / "ssh.pid"),
        },
    )


def test_s016_5_print_command_runs_nothing(tmp_path: Path) -> None:
    process = _run(tmp_path, "--print-command", "--format", "json", "--no-stream")
    stdout, _ = process.communicate(timeout=120)

    envelope = json.loads(stdout)
    assert process.returncode == 0, envelope
    (event,) = envelope["data"]
    assert event["status"] == "ok"
    assert event["command"][0] == "ssh"
    assert not (tmp_path / "ssh.pid").exists()


def test_s016_5_open_holds_the_forward_until_stopped(tmp_path: Path) -> None:
    port = _free_port()
    process = _run(tmp_path, "--port", str(port), "--format", "jsonl")
    try:
        assert process.stdout is not None
        event = json.loads(process.stdout.readline())
        assert event["status"] == "open"
        assert event["url"] == f"http://127.0.0.1:{port}/dashboards/f/cloudfall"
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
        ssh_pid = int((tmp_path / "ssh.pid").read_text(encoding="utf-8"))
    finally:
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=60)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(ssh_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("ssh outlived logging open")
