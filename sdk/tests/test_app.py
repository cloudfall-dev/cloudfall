"""The commands that moved to treaty, and the shim that routes to them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cloudfall.app import app
from cloudfall.cli import main
from cloudfall.commands import CLI_COMMANDS

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"


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
