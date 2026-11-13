"""S-015: alert channels, the resources that name them, and the bot token."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cloudfall.cli import main
from cloudfall.validation import ConfigValidationError, validate_config
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
CHANNEL = """---
apiVersion: cloudfall/v1
kind: AlertChannel
metadata:
  id: operations
  description: The operators' Telegram chat
spec:
  telegram:
    chatId: "{chat}"
"""


def _project(
    tmp_path: Path,
    *,
    chat: str = "145629422",
    baseline_channel: str | None = "operations",
    receiver_channel: str | None = None,
    declare: bool = True,
) -> Path:
    project = copy_examples(tmp_path / "config")
    if declare:
        channels = project / "alert-channels"
        channels.mkdir()
        (channels / "operations.yaml").write_text(
            CHANNEL.format(chat=chat), encoding="utf-8"
        )
    if baseline_channel is not None:
        baseline = project / "baselines" / "debian-application.yaml"
        baseline.write_text(
            baseline.read_text(encoding="utf-8")
            + f"  alerting:\n    channel: {baseline_channel}\n",
            encoding="utf-8",
        )
    if receiver_channel is not None:
        stack = project / "logging-stacks" / "operations.yaml"
        text = stack.read_text(encoding="utf-8")
        old = "    receivers:\n"
        assert old in text
        stack.write_text(
            text.replace(
                old, old + f"      - id: chat\n        channel: {receiver_channel}\n"
            ),
            encoding="utf-8",
        )
    return project


def _code(project: Path) -> str:
    with pytest.raises(ConfigValidationError) as error:
        validate_config(project, SCHEMAS)
    return error.value.issue.code


def test_s015_1_a_channel_and_its_references_validate(tmp_path: Path) -> None:
    validate_config(_project(tmp_path, receiver_channel="operations"), SCHEMAS)


@pytest.mark.parametrize("chat", ["-100123", "145629422"])
def test_s015_1_chat_ids_are_digits(tmp_path: Path, chat: str) -> None:
    validate_config(_project(tmp_path, chat=chat), SCHEMAS)


@pytest.mark.parametrize("chat", ["@operators", "12 34", "", "1-2"])
def test_s015_1_other_chat_ids_are_refused(tmp_path: Path, chat: str) -> None:
    assert _code(_project(tmp_path, chat=chat)) == "schema_validation_failed"


def test_s015_1_a_baseline_naming_an_undeclared_channel_is_refused(
    tmp_path: Path,
) -> None:
    assert _code(_project(tmp_path, declare=False)) == "alert_channel_missing"


def test_s015_1_a_receiver_naming_an_undeclared_channel_is_refused(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, baseline_channel=None, receiver_channel="nowhere")
    assert _code(project) == "alert_channel_missing"


def _token_states(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> dict[str, list[tuple[str, str, str]]]:
    assert main(["requirements", "show", "--project", str(project)]) == 0
    payload = json.loads(capsys.readouterr().out)
    return {
        server["server"]: [
            (item["owner"]["kind"], item["owner"]["id"], item["state"])
            for item in server["requirements"]
            if item["name"] == "telegram-bot-token"
        ]
        for server in payload["data"]["servers"]
    }


def test_s015_2_every_server_on_the_baseline_needs_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    states = _token_states(_project(tmp_path), capsys)

    assert states == {
        "h1": [("AlertChannel", "operations", "needs-input")],
        "h2": [("AlertChannel", "operations", "needs-input")],
    }


def test_s015_2_a_receiver_on_the_channel_adds_no_second_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    states = _token_states(_project(tmp_path, receiver_channel="operations"), capsys)

    assert states["h1"] == [("AlertChannel", "operations", "needs-input")]


def test_s015_2_a_receiver_alone_puts_the_token_on_the_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, baseline_channel=None, receiver_channel="operations")

    states = _token_states(project, capsys)

    assert states["h1"] == [("AlertChannel", "operations", "needs-input")]
    assert states["h2"] == []
