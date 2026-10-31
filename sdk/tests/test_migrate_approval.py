"""``migrate`` waits for a person's approval; ``rollback`` never does (S-008).

The engine the CLI is pointed at is a stand-in whose playbooks only leave a
marker (and, for a deploy, a release receipt) on the controller, so a test
sees which ``ansible-playbook`` ran without contacting any host. The artifact
is built for real, by the engine's ``artifact build``, from a local git
repository.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from cloudfall.app import app
from cloudfall.cli import main

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"
RELEASE = "20260101T000000Z-abcdef0"

_STAND_IN_PLAY = """\
---
- name: Stand in for {playbook}
  hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - name: Write the marker
      ansible.builtin.copy:
        dest: {marker}
        content: ran
        mode: '0600'
{receipt}
"""

_RECEIPT = """\
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

_PLAYBOOKS = (
    "baseline.yml",
    "services.yml",
    "deploy.yml",
    "inspect.yml",
    "rollback.yml",
)


class _Terminal(io.StringIO):
    """A stream that says it is a terminal, as a person's shell is."""

    def isatty(self) -> bool:
        return True


def _git(*arguments: str) -> None:
    subprocess.run(  # noqa: S603 - fixture repository setup.
        [
            shutil.which("git") or "git",
            "-c",
            "user.email=test@example.test",
            "-c",
            "user.name=Cloudfall Test",
            *arguments,
        ],
        capture_output=True,
        check=True,
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("-C", str(repository), "-c", "init.defaultBranch=main", "init", "--quiet")
    (repository / "app.py").write_text("print('crm')\n", encoding="utf-8")
    _git("-C", str(repository), "add", ".")
    _git("-C", str(repository), "commit", "--quiet", "--message", "initial")
    return repository


def _project(tmp_path: Path, repository: Path | None = None) -> Path:
    """Copy the example (``approval: manual``) without its domains.

    Without domains the plan has no DNS steps, so nothing here leaves the
    machine; the component builds from the local repository.
    """
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    shutil.rmtree(project / "domains")
    if repository is not None:
        component = project / "components" / "crm-backend.yaml"
        component.write_text(
            component.read_text(encoding="utf-8").replace(
                "https://github.com/example/crm-backend.git", f"file://{repository}"
            ),
            encoding="utf-8",
        )
    return project


def _engine(tmp_path: Path) -> Path:
    """Lay out an engine whose playbooks only write a marker each."""
    engine = tmp_path / "engine"
    playbooks = engine / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    shutil.copy(ENGINE / "ansible" / "ansible.cfg", engine / "ansible")
    for playbook in _PLAYBOOKS:
        (playbooks / playbook).write_text(
            _STAND_IN_PLAY.format(
                playbook=playbook,
                marker=_marker(tmp_path, playbook),
                receipt=_RECEIPT if playbook == "deploy.yml" else "",
            ),
            encoding="utf-8",
        )
    return engine


def _marker(tmp_path: Path, playbook: str) -> Path:
    return tmp_path / f"ran-{playbook}"


def _run(
    project: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *arguments: str,
) -> tuple[int, dict[str, Any]]:
    exit_code = main(
        [
            *arguments,
            *("--project", str(project), "--schemas", str(SCHEMAS)),
            *("--engine", str(_engine_once(tmp_path))),
            *("--inventory-file", str(tmp_path / "inventory.json")),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    return exit_code, payload


def _engine_once(tmp_path: Path) -> Path:
    engine = tmp_path / "engine"
    return engine if engine.is_dir() else _engine(tmp_path)


def _approve_at_terminal(project: Path, release: str) -> Path:
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


def _saved_step(project: Path, step_id: str) -> dict[str, Any]:
    """Return one step as the plan file saved it.

    The run goes on past the deploy step to ``inspect`` and ``audit``, which
    the stand-in engine leaves without observations, so the saved plan, not
    the run's answer, says how the deploy step ended.
    """
    plan = json.loads(
        (project / "tmp" / "migrate" / "plan.json").read_text(encoding="utf-8")
    )
    return next(step for step in plan["steps"] if step["id"] == step_id)


def _step(payload: dict[str, Any], step_id: str) -> dict[str, Any]:
    """Return one step of the plan, from a result or beside an error."""
    assert payload["data"] is not None, payload["error"]
    return next(step for step in payload["data"]["steps"] if step["id"] == step_id)


def test_s008_14_migrate_pauses_for_an_approval_and_resumes_without_a_rebuild(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    project = _project(tmp_path, repository)

    exit_code, paused = _run(
        project, tmp_path, capsys, "migrate", "--build", "crm-backend=main", "--yes"
    )

    error = paused["error"]
    assert error["code"] == "PARTIAL_FAILURE", error
    assert exit_code == 3
    context = error["context"]
    assert context["step"] == "deploy:crm-backend"
    assert context["code"] == "deployment_approval_missing"
    built = _step(paused, "build:crm-backend")
    assert built["status"] == "completed"
    release = built["detail"]["release"]
    assert context["release"] == release
    command = f"cloudfall release approve crm-backend --release {release}"
    assert context["command"] == command
    assert command in error["message"]
    assert paused["data"]["status"] == "paused"
    assert _step(paused, "deploy:crm-backend")["status"] == "pending"
    assert not _marker(tmp_path, "deploy.yml").exists()
    assert not (project / "tmp" / "releases").exists()

    path = _approve_at_terminal(project, release)
    # A second build has nothing to clone from: the resumed run must reuse
    # the release the completed build step recorded.
    shutil.rmtree(repository)
    archives = sorted((project / "tmp" / "artifacts" / "crm-backend").iterdir())

    _run(project, tmp_path, capsys, "migrate", "--yes")

    deployed = _saved_step(project, "deploy:crm-backend")
    assert deployed["status"] == "completed", deployed
    assert deployed["detail"]["release"] == release
    assert deployed["detail"]["approval"]["path"] == str(path)
    assert _marker(tmp_path, "deploy.yml").read_text(encoding="utf-8") == "ran"
    assert (project / "tmp" / "releases" / "crm-backend.json").is_file()
    assert sorted((project / "tmp" / "artifacts" / "crm-backend").iterdir()) == archives


def test_s008_14_a_broken_approval_record_stops_the_deploy_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    project = _project(tmp_path, repository)
    approvals = project / "decisions" / "releases"
    approvals.mkdir(parents=True)
    broken = approvals / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    exit_code, payload = _run(
        project, tmp_path, capsys, "migrate", "--build", "crm-backend=main", "--yes"
    )

    error = payload["error"]
    assert error["code"] == "RECORD_INVALID", error
    assert exit_code == 86
    assert error["context"]["code"] == "release_approval_record_invalid"
    assert str(broken) in error["message"]
    assert not _marker(tmp_path, "deploy.yml").exists()


def test_s008_14_an_automatic_application_migrates_without_a_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    project = _project(tmp_path, repository)
    application = project / "applications" / "crm.yaml"
    application.write_text(
        application.read_text(encoding="utf-8").replace(
            "approval: manual", "approval: automatic"
        ),
        encoding="utf-8",
    )

    _run(project, tmp_path, capsys, "migrate", "--build", "crm-backend=main", "--yes")

    deployed = _saved_step(project, "deploy:crm-backend")
    assert deployed["status"] == "completed", deployed
    assert "approval" not in deployed["detail"]
    assert _marker(tmp_path, "deploy.yml").read_text(encoding="utf-8") == "ran"


@pytest.mark.parametrize("approval", ["manual", "automatic"])
def test_s008_13_rollback_needs_no_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], approval: str
) -> None:
    project = _project(tmp_path)
    application = project / "applications" / "crm.yaml"
    application.write_text(
        application.read_text(encoding="utf-8").replace(
            "approval: manual", f"approval: {approval}"
        ),
        encoding="utf-8",
    )
    rollback = ("rollback", "crm-backend", "--release", RELEASE)

    exit_code, plan = _run(project, tmp_path, capsys, *rollback)

    assert exit_code == 0, plan
    assert plan["data"]["status"] == "plan"
    if approval == "manual":
        assert plan["data"]["approval"]["policy"] == "manual"
        assert plan["data"]["approval"]["required"] is False
        assert "recovery" in plan["data"]["approval"]["reason"]
    else:
        assert plan["data"]["approval"] == {"policy": "automatic"}
    assert not _marker(tmp_path, "rollback.yml").exists()

    exit_code, done = _run(project, tmp_path, capsys, *rollback, "--yes")

    assert exit_code == 0, done
    assert done["data"]["status"] == "ok"
    assert _marker(tmp_path, "rollback.yml").read_text(encoding="utf-8") == "ran"
    assert not (project / "decisions" / "releases").exists()
