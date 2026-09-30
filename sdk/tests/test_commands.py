"""The command catalog matches the command trees it classifies."""

from __future__ import annotations

import argparse

from cloudfall.cli import _parser as cloudfall_parser
from cloudfall.commands import (
    CLI_COMMANDS,
    COMMANDS,
    ENGINE_COMMANDS,
    CommandEffect,
    commands_with_effect,
)
from cloudfall_engine.cli import app as engine_app


def _leaves(parser: argparse.ArgumentParser, prefix: str = "") -> dict[str, set[str]]:
    """Map every leaf command path to the long options it accepts."""
    subparsers = [
        action
        for action in parser._actions  # noqa: SLF001 - argparse has no public walk.
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    ]
    if not subparsers:
        options = {
            option
            for action in parser._actions  # noqa: SLF001
            for option in action.option_strings
        }
        return {prefix.strip(): options}
    leaves: dict[str, set[str]] = {}
    for action in subparsers:
        for name, child in action.choices.items():
            leaves.update(_leaves(child, f"{prefix} {name}"))
    return leaves


def test_catalog_names_every_cli_leaf_command_once() -> None:
    cataloged = [command.name for command in CLI_COMMANDS]

    assert sorted(cataloged) == sorted(_leaves(cloudfall_parser()))
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
        for name, options in _leaves(cloudfall_parser()).items()
        if "--yes" in options
    }
    changing = {
        command.name for command in commands_with_effect(CommandEffect.SERVERS)
    }

    assert gated == {
        "deploy",
        "rollback",
        "restart",
        "data migrate",
        "migrate",
        "operations approve",
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

    assert by_name["data migrate"].invocation == "uv run cloudfall data migrate"
    assert by_name["playbook run"].invocation == "uv run cloudfall-engine playbook run"
