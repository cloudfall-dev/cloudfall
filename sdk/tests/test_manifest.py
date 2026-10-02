"""``cloudfall --schema`` declares what each command writes, and it holds."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from cloudfall.cli import main
from cloudfall.commands import CLI_COMMANDS, CommandContract, Stability
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
RELEASE = "20260923T000000Z-abcdef0"


def _schema(capsys: pytest.CaptureFixture[str], flag: str = "--schema") -> str:
    with pytest.raises(SystemExit) as exit_info:
        main([flag])
    assert exit_info.value.code == 0
    return capsys.readouterr().out


def test_schema_is_json_stable_and_aliased(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _schema(capsys)
    again = _schema(capsys)
    alias = _schema(capsys, "--print-schema")

    manifest = json.loads(first)
    # Only meta, the invocation's own id and duration, may differ.
    documents = [json.loads(output) for output in (first, again, alias)]
    assert documents[0]["data"] == documents[1]["data"] == documents[2]["data"]
    assert manifest["status"] == "ok"
    data = manifest["data"]
    assert data["etag"].startswith("sha256:")
    assert set(data["commands"]) == {command.name for command in CLI_COMMANDS}
    global_flags = set(data["global_flags"])
    assert {"schema", "schema-version", "version", "output"} <= global_flags
    assert "/Users/" not in first
    assert str(ROOT) not in first


def test_flags_are_read_from_the_parser(capsys: pytest.CaptureFixture[str]) -> None:
    deploy = json.loads(_schema(capsys))["data"]["commands"]["deploy"]

    assert deploy["effect"] == "servers"
    assert deploy["flags"]["release"] == {
        "type": "string",
        "required": True,
        "description": "release id produced by cloudfall-engine artifact build",
    }
    assert deploy["flags"]["component"]["positional"] is True
    assert deploy["flags"]["yes"]["type"] == "boolean"


@pytest.mark.parametrize("contract", CLI_COMMANDS, ids=lambda contract: contract.name)
def test_every_command_declares_a_valid_output_schema(
    contract: CommandContract,
) -> None:
    schema = contract.output_schema()

    Draft202012Validator.check_schema(schema)
    shapes = schema.get("oneOf", [schema])
    assert isinstance(shapes, list)
    assert contract.output
    for shape in shapes:
        properties = shape["properties"]
        assert {"meta", "warnings"} <= set(properties)
        assert {entry["x-stability"] for entry in properties.values()} <= {
            tier.value for tier in Stability
        }


def _documents(argv: list[str], capsys: pytest.CaptureFixture[str]) -> list[object]:
    code = main(argv)
    documents = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    # `ok` is the exit code as a boolean, on every document the command wrote.
    assert [document["ok"] for document in documents] == [code == 0] * len(documents)
    for document in documents:
        assert isinstance(document, dict)
        if "status" in document:
            # The argparse envelope: an error on stdout names its exit code.
            top = {"ok", "status", "data", "error", "meta", "warnings"}
            assert set(document) == top
            if document["error"] is not None:
                assert document["error"]["exit_code"] == code
        else:
            # treaty's envelope, for a command that moved; `_conforms` checks
            # which envelope the contract declares.
            assert set(document) == {"ok", "data", "error", "meta", "warnings"}
            assert document["meta"]["exit_code"] == code
    return documents


def _conforms(name: str, documents: list[object]) -> None:
    contract = next(command for command in CLI_COMMANDS if command.name == name)
    validator = Draft202012Validator(contract.output_schema())
    assert documents
    for document in documents:
        assert [error.message for error in validator.iter_errors(document)] == []


@pytest.mark.parametrize(
    ("name", "argv"),
    [
        ("config validate", ["config", "validate"]),
        ("inventory show", ["inventory", "show"]),
        ("operator list", ["operator", "list"]),
        ("restart", ["restart", "crm-backend"]),
        ("rollback", ["rollback", "crm-backend", "--release", RELEASE]),
        (
            "data migrate",
            [
                *("data", "migrate", "postgresql-main", "--database", "crm"),
                *("--source-url-file", "source.url"),
            ],
        ),
        ("migrate", ["migrate", "--build", "crm-backend=main"]),
    ],
)
def test_project_output_matches_its_declared_schema(
    name: str,
    argv: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    (project / "source.url").write_text("postgresql://example.test/crm\n")

    _conforms(name, _documents([*argv, "--project", str(project)], capsys))


def test_projectless_output_matches_its_declared_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "fresh"

    _conforms("changelog", _documents(["changelog"], capsys))
    _conforms("why", _documents(["why", "--repository", str(tmp_path)], capsys))
    _conforms(
        "operations decisions",
        _documents(["operations", "decisions", "--repository", str(tmp_path)], capsys),
    )
    _conforms("init", _documents(["init", str(project)], capsys))
    _conforms(
        "add server",
        _documents(
            [
                *("add", "server", "h1", "--address", "192.0.2.1"),
                *("--project", str(project)),
            ],
            capsys,
        ),
    )
