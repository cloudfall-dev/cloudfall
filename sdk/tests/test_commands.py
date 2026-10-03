"""The command catalog matches the commands the treaty apps register."""

from __future__ import annotations

import dataclasses

from cloudfall.app import app
from cloudfall.commands import (
    CLI_COMMANDS,
    COMMANDS,
    ENGINE_COMMANDS,
    CommandEffect,
    commands_with_effect,
)
from cloudfall_engine.cli import app as engine_app


def _leaves() -> dict[str, set[str]]:
    """Map every ``cloudfall`` command path to the fields its arguments take."""
    return {
        str(path).replace(".", " "): {
            field.name for field in dataclasses.fields(command.args_type)
        }
        for path, command in app.commands.items()
        if path not in app.builtins
    }


def test_catalog_names_every_cli_leaf_command_once() -> None:
    cataloged = [command.name for command in CLI_COMMANDS]

    assert sorted(cataloged) == sorted(_leaves())
    assert len(cataloged) == len(set(cataloged))
    assert all(command.program == "cloudfall" for command in CLI_COMMANDS)


def test_catalog_names_every_engine_leaf_command_once() -> None:
    cataloged = [command.name for command in ENGINE_COMMANDS]

    engine_leaves = [
        str(path).replace(".", " ")
        for path in engine_app.commands
        if path not in engine_app.builtins
    ]
    assert sorted(cataloged) == sorted(engine_leaves)
    assert len(cataloged) == len(set(cataloged))
    assert all(command.program == "cloudfall-engine" for command in ENGINE_COMMANDS)


def test_every_yes_gated_command_is_classified_as_changing_servers() -> None:
    gated = {
        name
        for name, fields in _leaves().items()
        if "yes" in fields
    }
    changing = {
        command.name for command in commands_with_effect(CommandEffect.SERVERS)
    }

    assert gated == {
        "deploy",
        "rollback",
        "restart",
        "data copy",
        "migrate",
        "decisions approve",
    }
    assert gated <= changing
    for command in commands_with_effect(CommandEffect.SERVERS):
        assert command.gate is not None, command.name
        if command.name in gated:
            assert "--yes" in command.gate


def test_only_server_changing_commands_carry_a_gate() -> None:
    for command in COMMANDS:
        assert (command.gate is not None) is (
            command.effect is CommandEffect.SERVERS
        ), command.name


def test_invocation_is_the_canonical_project_command() -> None:
    by_name = {command.name: command for command in COMMANDS}

    assert by_name["data copy"].invocation == "uv run cloudfall data copy"
    assert by_name["playbook run"].invocation == "uv run cloudfall-engine playbook run"
