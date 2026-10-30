"""``deploy`` enforces an application's ``approval: manual`` (S-008).

The engine the CLI is pointed at is a stand-in whose deploy playbook only
leaves a marker and a release receipt on the controller, so a test sees
whether ``ansible-playbook`` ran without contacting any host.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cloudfall.app import app
from cloudfall.cli import main
from cloudfall.decision import ApprovalChannel
from cloudfall.domain import ReleaseId, ResourceId, Sha256Digest
from cloudfall.lifecycle import DeployOptions, EngineContext, LifecycleError, deploy
from cloudfall.release_approval import (
    Approver,
    GitCommit,
    ReleaseCandidate,
    ValidityHours,
    approve_release,
    write_approval,
)
from cloudfall.validation import SchemaCatalog

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"
RELEASE = "20260101T000000Z-abcdef0"
OTHER_RELEASE = "20260102T000000Z-bcdef01"
COMMIT = "a" * 40
COMMAND = f"cloudfall release approve crm-backend --release {RELEASE}"

_RECEIPT_PLAY = """\
---
- name: Stand in for a deploy
  hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - name: Write the marker
      ansible.builtin.copy:
        dest: {marker}
        content: ran
        mode: '0600'
{outcome}
"""

_SUCCEED = """\
    - name: Make the receipt directory
      ansible.builtin.file:
        path: "{{ cloudfall_deploy_receipt_directory }}"
        state: directory
        mode: '0700'
    - name: Write the release receipt
      ansible.builtin.copy:
        dest: "{{ cloudfall_deploy_receipt_directory }}/crm-backend.json"
        content: '{}'
        mode: '0600'
"""

_FAIL_HEALTH_GATE = """\
    - name: Fail the health gate
      ansible.builtin.fail:
        msg: health check failed; rolled back to the previous release
"""


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


def _write_artifact(project: Path, release: str = RELEASE) -> str:
    """Build a consistent artifact under the project's default folder."""
    directory = project / "tmp" / "artifacts" / "crm-backend"
    directory.mkdir(parents=True, exist_ok=True)
    source = project / "tmp" / "source"
    source.mkdir(exist_ok=True)
    (source / "app.py").write_text(f"print('{release}')\n", encoding="utf-8")
    archive = directory / f"{release}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname=".")
    _write_metadata(archive, release)
    return hashlib.sha256(archive.read_bytes()).hexdigest()


def _write_metadata(archive: Path, release: str = RELEASE) -> None:
    metadata = {
        "apiVersion": "cloudfall/v1",
        "kind": "Artifact",
        "metadata": {"id": "crm-backend"},
        "spec": {
            "component": "crm-backend",
            "release": release,
            "gitRef": "main",
            "gitCommit": COMMIT,
            "builtAt": "2026-01-01T00:00:00Z",
            "archive": archive.name,
            "archiveSha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "sizeBytes": archive.stat().st_size,
        },
    }
    archive.with_name(f"{release}.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )


def _engine(tmp_path: Path, *, healthy: bool = True) -> Path:
    """Lay out an engine whose deploy playbook only writes a marker and a receipt."""
    engine = tmp_path / ("engine" if healthy else "failing-engine")
    playbooks = engine / "ansible" / "playbooks"
    playbooks.mkdir(parents=True, exist_ok=True)
    shutil.copy(ENGINE / "ansible" / "ansible.cfg", engine / "ansible")
    (playbooks / "deploy.yml").write_text(
        _RECEIPT_PLAY.format(
            marker=tmp_path / "ran-deploy.yml",
            outcome=_SUCCEED if healthy else _FAIL_HEALTH_GATE,
        ),
        encoding="utf-8",
    )
    return engine


def _deploy(
    project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *flags: str,
    healthy: bool = True,
    release: str = RELEASE,
) -> tuple[int, dict[str, Any]]:
    exit_code = main(
        [
            *("deploy", "crm-backend", "--release", release),
            *("--project", str(project), "--schemas", str(SCHEMAS)),
            *("--engine", str(_engine(tmp_path, healthy=healthy))),
            *("--inventory-file", str(tmp_path / "inventory.json")),
            *flags,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    return exit_code, payload


def _approve_at_terminal(project: Path, release: str = RELEASE) -> Path:
    """Run ``release approve`` as a person at a terminal (S-008-2)."""
    stdout = _Terminal()
    exit_code = app.run(
        [
            *("release", "approve", "crm-backend", "--release", release),
            *("--project", str(project), "--schemas", str(SCHEMAS)),
            *("--approver", "roman", "--format", "json"),
        ],
        stdin=_Terminal(f"{release}\n"),
        stdout=stdout,
        stderr=io.StringIO(),
    )
    payload = json.loads(stdout.getvalue())
    assert exit_code == 0, payload
    return Path(payload["data"]["path"])


def _record(  # noqa: PLR0913 - one record's every counted field.
    project: Path,
    *,
    digest: str,
    servers: tuple[str, ...] = ("h1", "h2"),
    approved_at: datetime | None = None,
    valid_for: int = 24,
    component: str = "crm-backend",
    release: str = RELEASE,
    approver: str = "roman",
) -> Path:
    """Write one approval record straight into the approvals folder."""
    approval = approve_release(
        ReleaseCandidate(
            application_id=ResourceId("crm"),
            component_id=ResourceId(component),
            release=ReleaseId(release),
            artifact_sha256=Sha256Digest(digest),
            git_commit=GitCommit(COMMIT),
            server_ids=tuple(ResourceId(server) for server in servers),
        ),
        Approver(approver),
        ApprovalChannel.TERMINAL,
        ValidityHours(valid_for),
        approved_at if approved_at is not None else datetime.now(UTC),
    )
    return write_approval(
        approval, project / "decisions" / "releases", SchemaCatalog(SCHEMAS)
    )


def _ran_nothing(project: Path, tmp_path: Path) -> bool:
    return (
        not (tmp_path / "ran-deploy.yml").exists()
        and not (tmp_path / "inventory.json").exists()
        and not (project / "tmp" / "releases").exists()
    )


def _refusal(payload: dict[str, Any]) -> dict[str, Any]:
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == "PRECONDITION", error
    assert error["context"]["code"] == "deployment_approval_missing"
    return error


def test_s008_1_a_manual_application_without_approval_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    _write_artifact(project)

    exit_code, payload = _deploy(project, tmp_path, capsys, "--yes")

    assert exit_code == 4
    error = _refusal(payload)
    for named in ("crm", "crm-backend", RELEASE, COMMAND):
        assert named in error["message"]
    context = error["context"]
    assert context["command"] == COMMAND
    assert context["application"] == "crm"
    assert context["component"] == "crm-backend"
    assert context["release"] == RELEASE
    assert context["notCounting"] == []
    assert _ran_nothing(project, tmp_path)


def test_s008_3_an_approved_release_deploys_and_names_its_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    _write_artifact(project)
    path = _approve_at_terminal(project)
    record = json.loads(path.read_text(encoding="utf-8"))["spec"]

    exit_code, payload = _deploy(project, tmp_path, capsys, "--yes")

    assert exit_code == 0, payload
    data = payload["data"]
    assert data["status"] == "ok"
    assert data["effect"] == "updated"
    assert data["approval"] == {
        "policy": "manual",
        "path": str(path),
        "approver": record["approver"],
        "approved_at": record["approvedAt"],
    }
    assert (tmp_path / "ran-deploy.yml").read_text(encoding="utf-8") == "ran"
    assert (project / "tmp" / "releases" / "crm-backend.json").is_file()


def test_s008_6_a_record_for_another_archive_does_not_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    _write_artifact(project)
    path = _approve_at_terminal(project)
    # The release is rebuilt: the archive and its metadata agree, on new bytes.
    archive = project / "tmp" / "artifacts" / "crm-backend" / f"{RELEASE}.tar.gz"
    archive.write_bytes(archive.read_bytes() + b"rebuilt")
    _write_metadata(archive)

    exit_code, payload = _deploy(project, tmp_path, capsys, "--yes")

    assert exit_code == 4
    error = _refusal(payload)
    assert error["context"]["notCounting"] == [
        {"path": str(path), "reason": "digest_mismatch"}
    ]
    assert str(path) in error["message"]
    assert _ran_nothing(project, tmp_path)


def test_s008_6_a_record_for_other_servers_does_not_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    digest = _write_artifact(project)
    # Approved while the component ran on h1 alone; it is declared on h1, h2 now.
    path = _record(project, digest=digest, servers=("h1",))

    exit_code, payload = _deploy(project, tmp_path, capsys, "--yes")

    assert exit_code == 4
    assert _refusal(payload)["context"]["notCounting"] == [
        {"path": str(path), "reason": "servers_changed"}
    ]
    assert _ran_nothing(project, tmp_path)


def test_s008_6_an_expired_record_does_not_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    digest = _write_artifact(project)
    path = _record(
        project,
        digest=digest,
        approved_at=datetime.now(UTC) - timedelta(hours=2),
        valid_for=1,
    )

    exit_code, payload = _deploy(project, tmp_path, capsys, "--yes")

    assert exit_code == 4
    assert _refusal(payload)["context"]["notCounting"] == [
        {"path": str(path), "reason": "expired"}
    ]
    assert _ran_nothing(project, tmp_path)


def test_s008_6_the_servers_are_compared_as_a_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    digest = _write_artifact(project)
    path = _record(project, digest=digest, servers=("h2", "h1"))

    exit_code, payload = _deploy(project, tmp_path, capsys)

    assert exit_code == 0, payload
    assert payload["data"]["approval"]["path"] == str(path)


def test_s008_7_a_record_for_another_release_or_component_does_not_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    digest = _write_artifact(project)
    _record(project, digest=digest, release=OTHER_RELEASE)
    _record(project, digest=digest, component="billing-backend")

    exit_code, payload = _deploy(project, tmp_path, capsys, "--yes")

    assert exit_code == 4
    # Neither is about this release, so neither is listed as not counting.
    assert _refusal(payload)["context"]["notCounting"] == []
    assert _ran_nothing(project, tmp_path)


def test_s008_8_the_plan_shows_the_approval_before_and_after_it_is_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    _write_artifact(project)

    exit_code, before = _deploy(project, tmp_path, capsys)

    assert exit_code == 0, before
    assert before["meta"]["dry_run"] is True
    assert before["data"]["status"] == "plan"
    assert before["data"]["approval"] == {
        "policy": "manual",
        "status": "missing",
        "not_counting": [],
        "command": COMMAND,
    }
    assert _ran_nothing(project, tmp_path)

    path = _approve_at_terminal(project)
    record = json.loads(path.read_text(encoding="utf-8"))["spec"]
    exit_code, after = _deploy(project, tmp_path, capsys)

    assert exit_code == 0, after
    assert after["data"]["approval"] == {
        "policy": "manual",
        "status": "approved",
        "path": str(path),
        "approver": record["approver"],
        "expires_at": record["expiresAt"],
        "not_counting": [],
    }
    assert _ran_nothing(project, tmp_path)


def test_s008_8_the_latest_counting_approval_is_shown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    digest = _write_artifact(project)
    now = datetime.now(UTC)
    _record(project, digest=digest, approved_at=now - timedelta(hours=2))
    latest = _record(project, digest=digest, approved_at=now, approver="amy")
    stale = _record(project, digest="b" * 64, approved_at=now)

    exit_code, payload = _deploy(project, tmp_path, capsys)

    assert exit_code == 0, payload
    approval = payload["data"]["approval"]
    assert approval["path"] == str(latest)
    assert approval["approver"] == "amy"
    assert approval["not_counting"] == [
        {"path": str(stale), "reason": "digest_mismatch"}
    ]


@pytest.mark.parametrize("yes", [False, True], ids=["plan", "yes"])
def test_s008_9_an_automatic_application_needs_no_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *, yes: bool
) -> None:
    project = _project(tmp_path, approval="automatic")
    _write_artifact(project)
    approvals = project / "decisions" / "releases"
    approvals.mkdir(parents=True)
    (approvals / "junk.json").write_text("{not json", encoding="utf-8")

    exit_code, payload = _deploy(project, tmp_path, capsys, *(["--yes"] * yes))

    assert exit_code == 0, payload
    assert payload["data"]["approval"] == {"policy": "automatic"}
    if yes:
        assert payload["data"]["status"] == "ok"
        assert (tmp_path / "ran-deploy.yml").is_file()
    else:
        assert payload["data"]["status"] == "plan"
        assert _ran_nothing(project, tmp_path)


@pytest.mark.parametrize(
    ("content", "says"),
    [
        ("{not json", "not valid JSON"),
        ('{"kind": "ReleaseApproval"}', "does not match its schema"),
        ("[]", "not a JSON object"),
    ],
    ids=["not-json", "not-the-schema", "not-an-object"],
)
@pytest.mark.parametrize("yes", [False, True], ids=["plan", "yes"])
def test_s008_10_a_broken_record_stops_a_manual_deploy(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str,
    says: str,
    *,
    yes: bool,
) -> None:
    project = _project(tmp_path)
    _write_artifact(project)
    _approve_at_terminal(project)
    broken = project / "decisions" / "releases" / "broken.json"
    broken.write_text(content, encoding="utf-8")

    exit_code, payload = _deploy(project, tmp_path, capsys, *(["--yes"] * yes))

    error = payload["error"]
    assert error["code"] == "RECORD_INVALID", error
    assert exit_code == 86
    assert error["context"]["code"] == "release_approval_record_invalid"
    assert str(broken) in error["message"]
    assert says in error["message"]
    assert _ran_nothing(project, tmp_path)


def test_s008_15_a_failed_health_gate_keeps_the_approval_for_a_retry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    _write_artifact(project)
    path = _approve_at_terminal(project)
    recorded = path.read_bytes()

    exit_code, failed = _deploy(project, tmp_path, capsys, "--yes", healthy=False)

    assert exit_code != 0
    assert failed["error"]["code"] == "ENGINE_STEP_FAILED", failed
    assert path.read_bytes() == recorded
    assert sorted((project / "decisions" / "releases").iterdir()) == [path]

    exit_code, retried = _deploy(project, tmp_path, capsys, "--yes")

    assert exit_code == 0, retried
    assert retried["data"]["approval"]["path"] == str(path)
    assert path.read_bytes() == recorded


def test_the_deploy_refuses_an_archive_swapped_after_the_check(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _write_artifact(project)

    with pytest.raises(LifecycleError) as error:
        deploy(
            EngineContext(
                project_directory=project,
                schema_directory=SCHEMAS,
                engine_directory=_engine(tmp_path),
                inventory_file=tmp_path / "inventory.json",
            ),
            ResourceId("crm-backend"),
            ReleaseId(RELEASE),
            project / "tmp" / "artifacts",
            DeployOptions(approved_sha256=Sha256Digest("b" * 64)),
        )

    assert error.value.code == "lifecycle_artifact_digest_mismatch"
    assert _ran_nothing(project, tmp_path)


def test_s008_10_deploy_declares_record_invalid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["deploy", "--schema"]) == 0
    described = json.loads(capsys.readouterr().out)["data"]

    assert "RECORD_INVALID" in json.dumps(described["exit_codes"])
    output = described["output_schema"]
    assert output["properties"]["approval"]["type"] == "object"
    assert "approval" in output["required"]


def test_s008_16_the_changelog_and_ci_guide_say_deploy_enforces_manual() -> None:
    fragment = (ROOT / "changelog.d" / "106-enforce-manual-approval.md").read_text(
        encoding="utf-8"
    )
    guide = (ROOT / "docs" / "ci-deploy-guide.md").read_text(encoding="utf-8")

    breaking = fragment.split("### Breaking", 1)[1]
    for named in (
        "`deploy --yes`",
        "`migrate --yes`",
        "`approval: manual`",
        "`cloudfall release approve`",
        "`approval: automatic`",
    ):
        assert named in breaking, named
    assert "no command acts on" not in " ".join(guide.split())
    assert "cloudfall release approve" in guide
