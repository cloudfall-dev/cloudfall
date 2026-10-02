"""``cloudfall add`` acceptance tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from cloudfall.authoring import (
    AuthoringError,
    ServerOptions,
    SshKeyOptions,
    add_server,
    add_ssh_key,
)
from cloudfall.cli import main
from cloudfall.domain import ConnectionAddress, LinuxUser, ResourceId, TcpPort
from cloudfall.project import (
    InitOptions,
    ProjectName,
    ReleaseVersion,
    init_project,
)
from cloudfall.validation import ConfigValidationError, validate_config

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
VERSION = "0.3.0"


def _example_key() -> str:
    document = yaml.safe_load(
        (EXAMPLES / "ssh-public-keys" / "example-admin.yaml").read_text(
            encoding="utf-8"
        )
    )
    return str(document["spec"]["publicKey"])


def _spec(project: Path, relative: str) -> dict[str, Any]:
    document = yaml.safe_load((project / relative).read_text(encoding="utf-8"))
    return cast("dict[str, Any]", document["spec"])


def _project(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    init_project(
        InitOptions(
            directory=directory,
            name=ProjectName("project"),
            version=ReleaseVersion(VERSION),
        )
    )
    return directory


def _server_options(
    resource_id: str = "h1", address: str = "203.0.113.10"
) -> ServerOptions:
    return ServerOptions(
        resource_id=ResourceId(resource_id),
        address=ConnectionAddress(address),
        server_type=ResourceId("debian-application"),
        environment=ResourceId("production"),
        ssh_user=LinuxUser("root"),
        ssh_port=TcpPort(22),
    )


def test_add_ssh_key_reads_the_key_file(tmp_path: Path) -> None:
    project = _project(tmp_path)
    key_file = tmp_path / "id_rsa.pub"
    key_file.write_text(f"{_example_key()}\n", encoding="utf-8")

    result = add_ssh_key(
        project,
        SshKeyOptions(
            key_path=key_file,
            owner=ResourceId("roman"),
            environment=ResourceId("production"),
        ),
        SCHEMAS,
    )

    assert [added.path for added in result.added] == ["ssh-public-keys/roman.yaml"]
    assert validate_config(project, SCHEMAS).resource_count == 1
    key = _spec(project, "ssh-public-keys/roman.yaml")
    assert key["owner"] == "roman"
    assert key["publicKey"] == _example_key()


def test_add_ssh_key_rejects_a_bad_file(tmp_path: Path) -> None:
    project = _project(tmp_path)
    key_file = tmp_path / "junk.pub"
    key_file.write_text("ssh-ed25519 not-base64!\n", encoding="utf-8")
    options = SshKeyOptions(
        key_path=key_file,
        owner=ResourceId("roman"),
        environment=ResourceId("production"),
    )

    with pytest.raises(AuthoringError) as error:
        add_ssh_key(project, options, SCHEMAS)
    assert error.value.code == "ssh_key_file_invalid"

    options = SshKeyOptions(
        key_path=tmp_path / "absent.pub",
        owner=ResourceId("roman"),
        environment=ResourceId("production"),
    )
    with pytest.raises(AuthoringError) as error:
        add_ssh_key(project, options, SCHEMAS)
    assert error.value.code == "ssh_key_file_missing"


def test_add_server_creates_the_baseline_type_once(tmp_path: Path) -> None:
    project = _project(tmp_path)

    first = add_server(project, _server_options(), SCHEMAS)
    second = add_server(project, _server_options("h2", "h2.example.internal"), SCHEMAS)

    assert [added.path for added in first.added] == [
        "server-types/debian-application.yaml",
        "servers/h1.yaml",
    ]
    assert [added.path for added in second.added] == ["servers/h2.yaml"]
    assert validate_config(project, SCHEMAS).resource_count == 3
    h1 = _spec(project, "servers/h1.yaml")
    h2 = _spec(project, "servers/h2.yaml")
    assert h1["hostname"] == "h1"
    assert h1["address"] == "203.0.113.10"
    assert h2["hostname"] == "h2.example.internal"
    server_type = _spec(project, "server-types/debian-application.yaml")
    assert server_type["os"]["versions"] == ["13"]
    assert "softwareRaid" not in server_type["storage"]
    assert {rule["port"] for rule in server_type["firewall"]["allowedInbound"]} == {
        22,
        80,
        443,
    }


def test_add_refuses_an_existing_resource_without_writing(tmp_path: Path) -> None:
    project = _project(tmp_path)
    add_server(project, _server_options(), SCHEMAS)
    before = (project / "servers" / "h1.yaml").read_text(encoding="utf-8")

    with pytest.raises(AuthoringError) as error:
        add_server(project, _server_options(address="198.51.100.1"), SCHEMAS)

    assert error.value.code == "resource_exists"
    assert (project / "servers" / "h1.yaml").read_text(encoding="utf-8") == before


def test_add_rolls_back_when_the_project_stops_validating(tmp_path: Path) -> None:
    project = _project(tmp_path)
    add_server(project, _server_options(), SCHEMAS)
    duplicate = (project / "servers" / "h1.yaml").read_text(encoding="utf-8")
    (project / "servers" / "elsewhere.yaml").write_text(
        duplicate.replace("address: 203.0.113.10", "address: 198.51.100.1"),
        encoding="utf-8",
    )
    (project / "servers" / "h1.yaml").unlink()

    with pytest.raises(ConfigValidationError) as error:
        add_server(project, _server_options(), SCHEMAS)

    assert error.value.issue.code == "resource_duplicate"
    assert not (project / "servers" / "h1.yaml").exists()


def test_cli_add_flow_from_init_to_a_valid_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    key_file = tmp_path / "id_rsa.pub"
    key_file.write_text(f"{_example_key()}\n", encoding="utf-8")
    common = ["--project", str(project), "--schemas", str(SCHEMAS)]

    assert main(["add", "ssh-key", str(key_file), "--owner", "roman", *common]) == 0
    key_payload = json.loads(capsys.readouterr().out)
    assert key_payload["data"]["added"][0]["path"] == "ssh-public-keys/roman.yaml"

    assert main(["add", "server", "h1", "--address", "203.0.113.10", *common]) == 0
    server_payload = json.loads(capsys.readouterr().out)
    assert [added["kind"] for added in server_payload["data"]["added"]] == [
        "ServerType",
        "Server",
    ]

    assert main(["config", "validate", *common]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["resources"] == 3


def test_cli_add_reports_bad_arguments_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    common = ["--project", str(project), "--schemas", str(SCHEMAS)]

    exit_code = main(["add", "server", "H1", "--address", "203.0.113.10", *common])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"]["code"] == "ARG_ERROR"
    assert not (project / "servers" / "H1.yaml").exists()


def _files(directory: Path) -> set[str]:
    return {path.relative_to(directory).as_posix() for path in directory.rglob("*")}


def test_cli_add_into_a_read_only_project_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    before = _files(project)
    for directory in (project, *(p for p in project.rglob("*") if p.is_dir())):
        directory.chmod(0o555)
    try:
        code = main(
            [
                *("add", "server", "h1", "--address", "203.0.113.10"),
                *("--project", str(project), "--schemas", str(SCHEMAS)),
            ]
        )
    finally:
        for directory in (project, *(p for p in project.rglob("*") if p.is_dir())):
            directory.chmod(0o755)

    error = json.loads(capsys.readouterr().out)["error"]
    assert code == 7
    assert error["code"] == "PERMISSION_DENIED"
    assert error["context"]["code"] == "project_write_failed"
    assert "Permission denied" in error["message"]
    assert _files(project) == before


def test_add_removes_what_it_wrote_when_a_later_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A new server type is written first; if the server fails, it goes too."""
    project = _project(tmp_path)
    servers = project / "servers"
    servers.mkdir(exist_ok=True)
    before = _files(project)
    servers.chmod(0o555)
    try:
        code = main(
            [
                *("add", "server", "h1", "--address", "203.0.113.10"),
                *("--project", str(project), "--schemas", str(SCHEMAS)),
            ]
        )
    finally:
        servers.chmod(0o755)

    assert code == 7
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["context"]["code"] == "project_write_failed"
    assert _files(project) == before


def test_init_reports_a_read_only_path_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = tmp_path / "read-only"
    parent.mkdir()
    parent.chmod(0o555)
    try:
        code = main(["init", str(parent / "project")])
    finally:
        parent.chmod(0o755)

    document = json.loads(capsys.readouterr().out)
    assert code == 7
    assert document["ok"] is False
    assert document["error"]["code"] == "PERMISSION_DENIED"
    assert document["error"]["context"]["code"] == "path_not_writable"
    assert str(parent / "project") in document["error"]["message"]
