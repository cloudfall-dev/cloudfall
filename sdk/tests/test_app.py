"""The commands that moved to treaty, and the shim that routes to them."""

from __future__ import annotations

import json
import select
import shutil
import signal
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
from cloudfall.app import Fleet, MigrateArgs
from cloudfall.cli import main
from cloudfall.commands import CLI_COMMANDS
from cloudfall.resources import default_schema_directory
from cloudfall.validation import validate_config

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"
SCHEMAS = default_schema_directory()


def test_root_help_lists_the_commands(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--help"])

    captured = capsys.readouterr()
    assert code == 0
    assert "audit" in captured.out + captured.err


def test_root_schema_lists_every_cataloged_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["--schema"])

    manifest = json.loads(capsys.readouterr().out)["data"]
    listed = set(manifest["commands"])
    assert code == 0
    assert {
        contract.name.replace(" ", ".")
        for contract in CLI_COMMANDS
    } <= listed


def test_a_command_keeps_its_keys_with_status_under_data(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["decisions", "list", "--repository", str(EXAMPLES)])

    document = json.loads(capsys.readouterr().out)
    assert code == 0
    assert "status" not in document
    assert document["data"]["status"] == "ok"
    assert set(document["data"]) == {"status", "directory", "decisions"}


def test_a_drift_report_keeps_the_order_the_checks_ran_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    h1.write_text(
        h1.read_text(encoding="utf-8").replace("[UU]", "[U_]"), encoding="utf-8"
    )

    code = main(
        ["audit", "--project", str(EXAMPLES), "--observed", str(observations)]
    )

    report = json.loads(capsys.readouterr().out)["data"]
    assert code == 83
    assert report["servers"][0]["checks"][0]["check"] == "server_type.id"


def test_migrate_resolves_env_and_data_files_against_the_project(
    tmp_path: Path,
) -> None:
    """Relative env and data paths name files inside the project."""
    fleet = Fleet(EXAMPLES, validate_config(EXAMPLES, SCHEMAS))
    args = MigrateArgs(
        component_env_file=("crm-backend=tmp/env/crm.env",),
        data=(f"crm={tmp_path / 'source.url'}",),
    )

    options = args.options(fleet)

    assert options.environment_files == {
        "crm-backend": EXAMPLES / "tmp/env/crm.env"
    }
    assert options.data_migrations == {"crm": tmp_path / "source.url"}


def test_dashboard_serve_streams_where_it_listens_until_stopped(
    tmp_path: Path,
) -> None:
    command = [
        sys.executable,
        "-c",
        "from cloudfall.cli import run; run()",
        *("dashboard", "serve", "--project", str(EXAMPLES)),
        *("--observed", str(COMPLIANT), "--port", "0"),
    ]
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and arguments.
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=tmp_path,
    )
    try:
        assert process.stdout is not None
        readable, _, _ = select.select([process.stdout], [], [], 30)
        assert readable, "the listening event never arrived"
        listening = json.loads(process.stdout.readline())
        url = listening["dashboard"]["url"]
        with urllib.request.urlopen(f"{url}operations.json", timeout=10) as response:  # noqa: S310 - local server
            assert response.status == 200
        process.send_signal(signal.SIGTERM)
        closing = json.loads(process.stdout.read().splitlines()[-1])
        assert process.wait(timeout=10) == 143
    finally:
        process.kill()
        process.wait()

    assert listening["_seq"] == 1
    assert listening["status"] == "ok"
    assert closing["error"]["code"] == "CANCELLED"


def test_dashboard_serve_names_a_port_it_cannot_listen_on(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        code = main(
            [
                *("dashboard", "serve", "--project", str(EXAMPLES)),
                *("--observed", str(COMPLIANT), "--port", str(port)),
            ]
        )

    closing = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert code == 4
    assert closing["error"]["context"]["code"] == "dashboard_listen_failed"


def test_decisions_show_says_when_there_is_no_such_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["decisions", "show", "ghost", "--repository", str(EXAMPLES)])

    document = json.loads(capsys.readouterr().out)
    assert code == 5
    assert document["error"]["context"]["code"] == "decision_missing"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (["operations", "decisions"], "cloudfall decisions list"),
        (["services", "inspect"], "cloudfall services observe"),
        (["data", "migrate", "postgresql-main"], "cloudfall data copy postgresql-main"),
    ],
)
def test_a_renamed_command_names_its_new_path(
    old: list[str], new: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(old)

    document = json.loads(capsys.readouterr().out)
    assert code == 13
    assert document["error"]["redirect"]["command"] == new
