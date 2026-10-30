"""``release approve``: a person's record that one release may be deployed (S-008)."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cloudfall.agent_server import PROJECT_COMMANDS
from cloudfall.app import app
from cloudfall.cli import main
from cloudfall.decision import ApprovalChannel
from cloudfall.domain import ReleaseId, ResourceId, Sha256Digest
from cloudfall.release_approval import (
    Approver,
    GitCommit,
    ReleaseApprovalError,
    ReleaseCandidate,
    ValidityHours,
    approve_release,
)
from cloudfall.validation import SchemaCatalog
from treaty import CommandPath

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
RELEASE = "20260101T000000Z-abcdef0"
COMMIT = "a" * 40
MOMENT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


class _Terminal(io.StringIO):
    """A stream that says it is a terminal, as a person's shell is."""

    def isatty(self) -> bool:
        return True


def _project(tmp_path: Path, approval: str = "manual") -> Path:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    application = project / "applications" / "crm.yaml"
    text = application.read_text(encoding="utf-8")
    application.write_text(
        text.replace("approval: manual", f"approval: {approval}"), encoding="utf-8"
    )
    return project


def _write_artifact(project: Path) -> str:
    """Build a consistent artifact under the project's default folder."""
    directory = project / "tmp" / "artifacts" / "crm-backend"
    directory.mkdir(parents=True)
    source = project / "tmp" / "source"
    source.mkdir()
    (source / "app.py").write_text("print('crm')\n", encoding="utf-8")
    archive = directory / f"{RELEASE}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname=".")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    metadata = {
        "apiVersion": "cloudfall/v1",
        "kind": "Artifact",
        "metadata": {"id": "crm-backend"},
        "spec": {
            "component": "crm-backend",
            "release": RELEASE,
            "gitRef": "main",
            "gitCommit": COMMIT,
            "builtAt": "2026-01-01T00:00:00Z",
            "archive": archive.name,
            "archiveSha256": digest,
            "sizeBytes": archive.stat().st_size,
        },
    }
    (directory / f"{RELEASE}.json").write_text(json.dumps(metadata), encoding="utf-8")
    return digest


def _argv(project: Path, *flags: str) -> list[str]:
    return [
        *("release", "approve", "crm-backend", "--release", RELEASE),
        *("--project", str(project), "--schemas", str(SCHEMAS)),
        *("--approver", "roman"),
        *flags,
    ]


def _at_terminal(
    project: Path, typed: str, *flags: str
) -> tuple[int, dict[str, object], str]:
    """Run ``release approve`` as a person at a terminal who types ``typed``."""
    stdout = _Terminal()
    stderr = io.StringIO()
    exit_code = app.run(
        [*_argv(project, *flags), "--format", "json"],
        stdin=_Terminal(f"{typed}\n"),
        stdout=stdout,
        stderr=stderr,
    )
    return exit_code, json.loads(stdout.getvalue()), stderr.getvalue()


def _records(project: Path) -> list[Path]:
    approvals = project / "decisions" / "releases"
    return sorted(approvals.glob("*.json")) if approvals.is_dir() else []


def test_s008_2_a_person_approves_a_release_by_typing_its_id(tmp_path: Path) -> None:
    project = _project(tmp_path)
    digest = _write_artifact(project)

    exit_code, payload, prompt = _at_terminal(project, RELEASE)

    assert exit_code == 0, payload
    (path,) = _records(project)
    record = json.loads(path.read_text(encoding="utf-8"))
    SchemaCatalog(SCHEMAS).validate_named("release-approval.schema.json", record)
    assert record["kind"] == "ReleaseApproval"
    assert path.name == f"{record['metadata']['id']}.json"
    assert record["metadata"]["id"].startswith("approval-")
    spec = record["spec"]
    assert spec["application"] == "crm"
    assert spec["component"] == "crm-backend"
    assert spec["release"] == RELEASE
    assert spec["artifactSha256"] == digest
    assert spec["gitCommit"] == COMMIT
    assert spec["servers"] == ["h1", "h2"]
    assert spec["approver"] == "roman"
    assert spec["via"] == "terminal"
    approved = datetime.fromisoformat(spec["approvedAt"])
    expires = datetime.fromisoformat(spec["expiresAt"])
    assert expires - approved == timedelta(hours=24)
    data = payload["data"]
    assert isinstance(data, dict)
    assert data["approval"] == record
    assert data["path"] == str(path)
    for named in ("crm", "crm-backend", RELEASE, COMMIT, digest, "h1, h2"):
        assert named in prompt


def test_s008_2_the_record_id_is_an_approval_time_and_16_hex() -> None:
    candidate = ReleaseCandidate(
        application_id=ResourceId("crm"),
        component_id=ResourceId("crm-backend"),
        release=ReleaseId(RELEASE),
        artifact_sha256=Sha256Digest("b" * 64),
        git_commit=GitCommit(COMMIT),
        server_ids=(ResourceId("h1"),),
    )

    approval = approve_release(
        candidate,
        Approver("roman"),
        ApprovalChannel.TERMINAL,
        ValidityHours(24),
        MOMENT,
    )

    document = approval.as_document()
    assert approval.approval_id.value.startswith("approval-20260101t120000-")
    assert len(approval.approval_id.value.rsplit("-", 1)[1]) == 16
    assert document["spec"] == {
        "application": "crm",
        "component": "crm-backend",
        "release": RELEASE,
        "artifactSha256": "b" * 64,
        "gitCommit": COMMIT,
        "servers": ["h1"],
        "approver": "roman",
        "approvedAt": "2026-01-01T12:00:00Z",
        "expiresAt": "2026-01-02T12:00:00Z",
        "via": "terminal",
    }


@pytest.mark.parametrize("flags", [["--yes"], []], ids=["yes", "bare"])
def test_s008_4_off_a_terminal_nothing_is_approved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    project = _project(tmp_path)
    _write_artifact(project)

    exit_code = main(_argv(project, *flags))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 4
    assert payload["error"]["code"] == "PERSON_REQUIRED"
    assert _records(project) == []


@pytest.mark.parametrize("flags", [["--yes"], []], ids=["yes", "bare"])
def test_s008_4_a_mistyped_release_id_approves_nothing(
    tmp_path: Path, flags: list[str]
) -> None:
    project = _project(tmp_path)
    _write_artifact(project)

    exit_code, payload, _prompt = _at_terminal(project, "yes", *flags)

    error = payload["error"]
    assert isinstance(error, dict)
    assert exit_code == 4
    assert error["code"] == "ATTESTATION_MISMATCH"
    assert _records(project) == []


def test_s008_11_an_automatic_application_needs_no_approval(tmp_path: Path) -> None:
    project = _project(tmp_path, approval="automatic")
    _write_artifact(project)

    exit_code, payload, prompt = _at_terminal(project, RELEASE)

    error = payload["error"]
    assert isinstance(error, dict)
    assert exit_code == 4
    assert error["code"] == "PRECONDITION"
    assert error["context"]["code"] == "release_approval_not_required"
    assert prompt == ""
    assert _records(project) == []


def test_s008_11_a_missing_artifact_is_refused_before_asking(tmp_path: Path) -> None:
    project = _project(tmp_path)

    exit_code, payload, prompt = _at_terminal(project, RELEASE)

    error = payload["error"]
    assert isinstance(error, dict)
    assert exit_code == 4
    assert error["code"] == "PRECONDITION"
    assert error["context"]["code"] == "lifecycle_artifact_missing"
    assert prompt == ""
    assert _records(project) == []


def test_s008_11_a_tampered_artifact_is_refused_before_asking(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_artifact(project)
    archive = project / "tmp" / "artifacts" / "crm-backend" / f"{RELEASE}.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"tampered")

    exit_code, payload, prompt = _at_terminal(project, RELEASE)

    error = payload["error"]
    assert isinstance(error, dict)
    assert exit_code == 4
    assert error["code"] == "PRECONDITION"
    assert error["context"]["code"] == "lifecycle_artifact_digest_mismatch"
    assert prompt == ""
    assert _records(project) == []


def test_an_undeclared_component_is_refused_before_asking(tmp_path: Path) -> None:
    project = _project(tmp_path)
    stdout = _Terminal()
    stderr = io.StringIO()

    exit_code = app.run(
        [
            *("release", "approve", "ghost", "--release", RELEASE),
            *("--project", str(project), "--schemas", str(SCHEMAS)),
            *("--approver", "roman", "--format", "json"),
        ],
        stdin=_Terminal(f"{RELEASE}\n"),
        stdout=stdout,
        stderr=stderr,
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == 4
    assert payload["error"]["context"]["code"] == "lifecycle_component_missing"
    assert stderr.getvalue() == ""


@pytest.mark.parametrize("hours", ["0", "169"])
def test_s008_12_a_validity_out_of_range_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hours: str
) -> None:
    project = _project(tmp_path)
    _write_artifact(project)

    exit_code = main(_argv(project, "--valid-for", hours))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"]["code"] == "ARG_ERROR"
    assert "--valid-for" in payload["error"]["message"]
    assert _records(project) == []


def test_s008_12_a_validity_of_two_hours_expires_two_hours_later(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _write_artifact(project)

    exit_code, payload, _prompt = _at_terminal(project, RELEASE, "--valid-for", "2")

    assert exit_code == 0, payload
    (path,) = _records(project)
    spec = json.loads(path.read_text(encoding="utf-8"))["spec"]
    approved = datetime.fromisoformat(spec["approvedAt"])
    expires = datetime.fromisoformat(spec["expiresAt"])
    assert expires - approved == timedelta(hours=2)


@pytest.mark.parametrize("hours", [0, 169, True])
def test_the_validity_is_whole_hours_from_one_to_a_week(hours: int) -> None:
    with pytest.raises(ReleaseApprovalError):
        ValidityHours(hours)


def test_an_approval_needs_an_approver() -> None:
    with pytest.raises(ReleaseApprovalError) as error:
        Approver(" ")

    assert error.value.code == "release_approver_missing"


def test_a_blank_approver_is_refused_before_asking(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_artifact(project)
    stdout = _Terminal()
    stderr = io.StringIO()

    exit_code = app.run(
        [
            *("release", "approve", "crm-backend", "--release", RELEASE),
            *("--project", str(project), "--schemas", str(SCHEMAS)),
            *("--approver", " ", "--format", "json"),
        ],
        stdin=_Terminal(f"{RELEASE}\n"),
        stdout=stdout,
        stderr=stderr,
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == 4
    assert payload["error"]["context"]["code"] == "release_approver_missing"
    assert stderr.getvalue() == ""
    assert _records(project) == []


def test_the_release_approval_command_is_a_persons_and_never_a_tool() -> None:
    command = app.commands[CommandPath("release.approve")]

    assert command.requires_person is True
    assert command.mcp is False
    assert "release.approve" not in PROJECT_COMMANDS
