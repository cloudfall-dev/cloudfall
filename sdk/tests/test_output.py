"""Every JSON document names its output contract and the running tool."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import date
from importlib.metadata import version
from pathlib import Path
from unittest.mock import ANY

import pytest
from cloudfall.cli import main
from cloudfall.commands import EXIT_CODES
from cloudfall.output import (
    RESPONSE_META,
    SCHEMA_CHANGELOG,
    DeprecatedField,
    FieldPath,
    OutputOptions,
    SchemaChange,
    SchemaVersion,
    begin_invocation,
    configure_output,
    envelope,
    exit_code,
    write_error,
    write_result,
)

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
VERSIONS = {"schema_version": "1.0", "tool_version": version("cloudfall")}
META = {**VERSIONS, "request_id": ANY, "duration_ms": ANY}


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    try:
        code = main(argv)
    except SystemExit as exit_info:
        code = int(str(exit_info.code))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_version_flag_prints_the_package_and_schema_versions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _run(["--version"], capsys)

    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "ok": True,
        "status": "ok",
        "error": None,
        "data": {
            "version": version("cloudfall"),
            "schemaVersions": {"current": "1.0", "minimum": "1.0"},
        },
        "meta": META,
        "warnings": [],
    }


def test_results_and_errors_carry_the_same_meta_and_warnings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ok, result, _ = _run(["changelog"], capsys)
    failed, _, error = _run(["observe", "--project", str(ROOT / "missing")], capsys)
    usage_code, _, usage = _run(["deploy"], capsys)

    assert (ok, failed, usage_code) == (0, 2, 2)
    for document in (json.loads(result), json.loads(error), json.loads(usage)):
        assert document["meta"] == META
        assert {
            key: document["meta"][key] for key in VERSIONS
        } == RESPONSE_META.as_dict()
        assert document["warnings"] == []


@pytest.mark.parametrize("owned", ["ok", "data", "meta", "warnings"])
def test_a_payload_cannot_set_what_the_writer_owns(owned: str) -> None:
    with pytest.raises(ValueError, match="writer owns it"):
        write_result({"status": "ok", owned: {}})


def test_a_deprecated_field_warns_while_it_is_still_written() -> None:
    deprecated = DeprecatedField(
        path=FieldPath("data.inventory.servers"),
        replacement=FieldPath("data.inventory.hosts"),
        removed_in=SchemaVersion(2, 0),
    )
    begin_invocation()

    with_field = envelope(
        {"status": "ok", "inventory": {"servers": []}},
        exit_code=0,
        deprecated_fields=(deprecated,),
    )
    without_field = envelope(
        {"status": "ok", "inventory": {"hosts": []}},
        exit_code=0,
        deprecated_fields=(deprecated,),
    )

    assert with_field["warnings"] == [
        {
            "code": "FIELD_DEPRECATED",
            "message": (
                "field 'data.inventory.servers' is deprecated and is removed in "
                "schema 2.0; use 'data.inventory.hosts'"
            ),
            "removed_in": "2.0",
            "context": {
                "field": "data.inventory.servers",
                "replacement": "data.inventory.hosts",
            },
        }
    ]
    assert without_field["warnings"] == []


def test_changelog_lists_contract_changes_newest_first(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = _run(["changelog"], capsys)
    since_code, since_out, _ = _run(["changelog", "--since", "1.0"], capsys)

    assert (code, since_code) == (0, 0)
    entries = json.loads(out)["data"]["entries"]
    assert entries[0] == {
        "version": "1.0",
        "date": "2026-09-23",
        "breaking": False,
        "added": [
            "ok",
            "status",
            "data",
            "error",
            "error.exit_code",
            "meta.schema_version",
            "meta.tool_version",
            "meta.request_id",
            "meta.duration_ms",
            "warnings",
        ],
        "removed": [],
        "changed": [],
    }
    assert [entry["version"] for entry in entries] == [
        str(change.version) for change in reversed(SCHEMA_CHANGELOG)
    ]
    assert json.loads(since_out)["data"]["entries"] == []


def test_a_removal_or_change_is_breaking() -> None:
    def change(**fields: tuple[FieldPath, ...]) -> SchemaChange:
        return SchemaChange(
            version=SchemaVersion(2, 0),
            released=date(2027, 1, 1),
            added=fields.get("added", ()),
            removed=fields.get("removed", ()),
            changed=fields.get("changed", ()),
        )

    assert not change(added=(FieldPath("a"),)).breaking
    assert change(removed=(FieldPath("a"),)).breaking
    assert change(changed=(FieldPath("a"),)).breaking


def test_changelog_since_rejects_a_bare_major(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _, err = _run(["changelog", "--since", "1"], capsys)

    assert code == 2
    assert "expected MAJOR.MINOR" in json.loads(err)["error"]["message"]


def test_schema_version_pin_accepts_the_current_major(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = _run(["--schema-version", "1", "changelog"], capsys)

    assert code == 0
    assert json.loads(out)["meta"]["schema_version"] == "1.0"


@pytest.mark.parametrize(
    ("pin", "reason"),
    [("2", "not supported"), ("0", "expected a MAJOR number"), ("1.0", "MAJOR")],
)
def test_schema_version_pin_fails_before_any_command_runs(
    pin: str, reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(["--schema-version", pin, "changelog"], capsys)

    assert (code, out) == (2, "")
    error = json.loads(err)["error"]
    assert error["code"] == "invalid_argument"
    assert reason in error["message"]


@pytest.mark.parametrize("value", ["1", "1.0.0", "v1.0", "01.0", "1.x", ""])
def test_schema_version_is_major_dot_minor(value: str) -> None:
    with pytest.raises(ValueError, match="invalid schema version"):
        SchemaVersion.parse(value)


def test_schema_versions_order_numerically() -> None:
    assert SchemaVersion.parse("1.10") > SchemaVersion.parse("1.9")


def test_the_payload_moves_under_data_and_status_and_error_stay_on_top() -> None:
    begin_invocation()
    result = envelope({"status": "plan", "steps": []}, exit_code=0)
    failure = envelope(
        {"status": "error", "error": {"code": "x", "message": "y"}}, exit_code=2
    )
    failed_step = envelope(
        {"status": "error", "error": {"code": "x", "message": "y"}, "step": "a"},
        exit_code=1,
    )

    assert result == {
        "ok": True,
        "status": "plan",
        "error": None,
        "data": {"steps": []},
        "meta": META,
        "warnings": [],
    }
    assert failure == {
        "ok": False,
        "status": "error",
        "data": None,
        "error": {"code": "x", "message": "y", "exit_code": 2},
        "meta": META,
        "warnings": [],
    }
    assert failed_step["data"] == {"step": "a"}
    assert failed_step["error"] == {"code": "x", "message": "y", "exit_code": 1}


def test_a_document_with_an_error_cannot_be_ok() -> None:
    with pytest.raises(ValueError, match="cannot be ok"):
        envelope({"status": "error", "error": {"code": "x"}}, exit_code=0)


def test_a_payload_must_name_its_status() -> None:
    with pytest.raises(TypeError, match="names its status"):
        envelope({"inventory": {}}, exit_code=0)


def test_an_error_document_carries_only_its_error() -> None:
    begin_invocation()
    with pytest.raises(ValueError, match="status and error only"):
        write_error({"status": "error", "error": {"code": "x"}, "extra": 1}, 2)


@pytest.mark.parametrize(
    "argv",
    [
        ["--output", "json", "changelog"],
        ["changelog", "--output", "json"],
        ["changelog", "--output=json"],
    ],
)
def test_output_json_is_accepted_before_and_after_the_command(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(argv, capsys)

    assert (code, err) == (0, "")
    assert json.loads(out)["ok"] is True


def test_output_accepts_only_json(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(["changelog", "--output", "text"], capsys)

    assert (code, out) == (2, "")
    assert json.loads(err)["error"]["code"] == "invalid_argument"


def test_one_invocation_has_one_request_id_and_a_growing_duration() -> None:
    invocation = begin_invocation()
    first = envelope({"status": "ok"}, exit_code=0)["meta"]
    second = envelope({"status": "ok"}, exit_code=0)["meta"]

    assert isinstance(first, dict)
    assert isinstance(second, dict)
    assert first["request_id"] == second["request_id"] == str(invocation.request_id)
    assert uuid.UUID(str(first["request_id"])).version == 4
    assert isinstance(first["duration_ms"], int)
    assert 0 <= first["duration_ms"] <= second["duration_ms"]


def test_every_invocation_gets_its_own_request_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, first, _ = _run(["changelog"], capsys)
    _, second, _ = _run(["changelog"], capsys)

    assert json.loads(first)["meta"]["request_id"] != json.loads(second)["meta"][
        "request_id"
    ]


def test_writing_outside_an_invocation_is_a_bug() -> None:
    script = (
        "from cloudfall.output import write_result\n"
        "write_result({'status': 'ok'})\n"
    )
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and script.
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "no invocation has begun" in completed.stderr


def test_help_goes_to_stderr_when_stdout_is_not_a_terminal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _run(["observe", "--help"], capsys)

    assert (code, out) == (0, "")
    assert err.startswith("usage: cloudfall observe")


def test_quiet_silences_help_off_a_terminal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(["--quiet", "--help"], capsys) == (0, "", "")


def test_help_stays_on_stdout_for_a_person_at_a_terminal() -> None:
    import os  # noqa: PLC0415 - test-local.
    import pty  # noqa: PLC0415 - test-local.

    pid, descriptor = pty.fork()
    if pid == 0:
        entry = "from cloudfall.cli import run; run()"
        # Python 3.14's argparse colors help on a terminal; NO_COLOR keeps
        # the text comparable.
        os.execve(  # noqa: S606 - fixed interpreter and entry point.
            sys.executable,
            [sys.executable, "-c", entry, "--help"],
            {**os.environ, "NO_COLOR": "1"},
        )
    printed = b""
    while True:
        try:
            chunk = os.read(descriptor, 65536)
        except OSError:
            break
        if not chunk:
            break
        printed += chunk
    _, status = os.waitpid(pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    assert printed.decode().startswith("usage: cloudfall")


@pytest.mark.parametrize(
    "argv",
    [
        ["--quiet", "observe", "--project", str(ROOT / "missing")],
        ["observe", "--project", str(ROOT / "missing"), "--quiet"],
        ["--quiet", "dashboard", "shoe"],
    ],
)
def test_quiet_leaves_only_the_exit_code(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(argv, capsys) == (2, "", "")


def test_quiet_keeps_the_result_on_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(["changelog", "--quiet"], capsys)

    assert (code, err) == (0, "")
    assert json.loads(out)["ok"] is True


def test_warnings_as_errors_fails_a_result_that_carries_a_warning() -> None:
    deprecated = DeprecatedField(
        path=FieldPath("data.inventory"),
        replacement=FieldPath("data.fleet"),
        removed_in=SchemaVersion(2, 0),
    )
    begin_invocation()
    configure_output(OutputOptions(warnings_as_errors=True))

    document = envelope(
        {"status": "ok", "inventory": {}}, exit_code=0, deprecated_fields=(deprecated,)
    )

    assert (document["ok"], document["status"]) == (False, "ok")
    assert document["data"] == {"inventory": {}}
    assert document["error"] == {
        "code": "warnings_as_errors",
        "message": "--warnings-as-errors: 1 warning(s) (FIELD_DEPRECATED)",
        "exit_code": 1,
    }
    assert (exit_code(0), exit_code(2)) == (1, 2)


def test_warnings_as_errors_leaves_a_clean_result_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = _run(["changelog", "--warnings-as-errors"], capsys)

    assert code == 0
    assert (json.loads(out)["ok"], json.loads(out)["error"]) == (True, None)


@pytest.mark.parametrize("argv", [["--help"], ["observe", "--help"]])
def test_help_ends_with_every_exit_code(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _run(argv, capsys)

    assert code == 0
    table = err[err.index("\nexit codes:\n") :]
    for declared in EXIT_CODES:
        first_words = " ".join(declared.meaning.replace("`", "").split()[:4])
        assert f"  {declared.code}  {first_words}" in table


def test_the_error_carries_the_code_the_process_exits_with(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = [
        (_run(["deploy"], capsys), 2),
        (_run(["observe", "--project", str(tmp_path)], capsys), 2),
    ]

    for (code, _, err), expected in cases:
        assert code == expected
        assert json.loads(err)["error"]["exit_code"] == code


def test_an_error_cannot_exit_zero() -> None:
    begin_invocation()
    with pytest.raises(ValueError, match="cannot exit 0"):
        write_error({"status": "error", "error": {"code": "x", "message": "y"}}, 0)


def test_the_writer_owns_exit_code() -> None:
    begin_invocation()
    with pytest.raises(ValueError, match="exit_code is added"):
        envelope(
            {"status": "error", "error": {"code": "x", "exit_code": 9}}, exit_code=2
        )
