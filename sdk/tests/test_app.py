"""The commands that moved to treaty, and the shim that routes to them."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from cloudfall.app import app
from cloudfall.cli import main
from cloudfall.commands import CLI_COMMANDS

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"


def test_the_contract_marks_exactly_the_commands_treaty_runs() -> None:
    marked = {
        contract.name
        for contract in CLI_COMMANDS
        if contract.program == "cloudfall" and contract.treaty
    }
    routed = {
        contract.name
        for contract in CLI_COMMANDS
        if contract.program == "cloudfall" and app.resolves(contract.name.split())
    }

    assert marked == routed
    assert "audit" in marked


def test_root_help_still_lists_a_command_that_moved(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
    assert "audit" in capsys.readouterr().err


def test_a_moved_command_keeps_its_keys_with_status_under_data(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["operations", "decisions", "--repository", str(EXAMPLES)])

    document = json.loads(capsys.readouterr().out)
    assert code == 0
    assert "status" not in document
    assert document["data"]["status"] == "ok"
    assert set(document["data"]) == {"status", "directory", "decisions"}


def test_a_half_moved_group_sends_the_rest_to_argparse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["operations", "propose", "restart", "--repository", str(EXAMPLES)])

    error = json.loads(capsys.readouterr().err)
    assert code == 2
    assert error["status"] == "error"


@pytest.mark.xfail(
    strict=True,
    reason="treaty #181: a failure's data loses an output adapter's array order",
)
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
