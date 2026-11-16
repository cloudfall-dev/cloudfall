"""A project format this release does not read stops every command at start.

Each command runs as the installed console script, with an
``ansible-playbook`` on ``PATH`` that records every start, so a refusal
that came after an Ansible run, or after a write under the project's
``tmp/``, shows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cloudfall.project_format import (
    PROJECT_FORMAT_UNSUPPORTED_EXIT,
    SUPPORTED_FORMATS,
)
from examples_project import EXAMPLES, copy_examples

SCHEMAS = EXAMPLES.parents[1] / "config" / "schemas" / "v1"
OLDEST = SUPPORTED_FORMATS.oldest.value
CURRENT = SUPPORTED_FORMATS.current.value
BIN = Path(sys.executable).parent
CLOUDFALL = str(BIN / "cloudfall")
ENGINE = str(BIN / "cloudfall-engine")
RECORD = "CLOUDFALL_TEST_RECORD"
RECORDING_PLAYBOOK = f"""#!{sys.executable}
import os, sys
with open(os.environ["{RECORD}"], "a", encoding="utf-8") as record:
    record.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--version"]:
    print("ansible-playbook [core 2.21.0]")
"""
PUBLIC_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
    " someone@example\n"
)
TOO_NEW_CONTEXT = {
    "code": "project_format_too_new",
    "projectFormat": CURRENT + 1,
    "oldest": OLDEST,
    "current": CURRENT,
}


def _too_new_project(tmp_path: Path) -> Path:
    project = copy_examples(tmp_path / "project")
    (project / "pyproject.toml").write_text(
        f"[tool.cloudfall]\nformat = {CURRENT + 1}\n", encoding="utf-8"
    )
    return project


def _run(
    tmp_path: Path, project: Path, argv: tuple[str, ...]
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run ``argv`` inside ``project``; return the run and the playbook record."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    playbook = tools / "ansible-playbook"
    playbook.write_text(RECORDING_PLAYBOOK, encoding="utf-8")
    playbook.chmod(0o755)
    record = tmp_path / "ansible-playbook-starts.log"
    done = subprocess.run(  # noqa: S603 - the venv's own console scripts.
        argv,
        cwd=project,
        env={
            **os.environ,
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "CLOUDFALL_PROJECT": str(project),
            RECORD: str(record),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return done, record


def _assert_refused_too_new(
    done: subprocess.CompletedProcess[str], *, answer: str = "stdout"
) -> None:
    assert done.returncode == PROJECT_FORMAT_UNSUPPORTED_EXIT, (
        done.stdout + done.stderr
    )
    envelope = json.loads(getattr(done, answer))
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "PROJECT_FORMAT_UNSUPPORTED"
    assert envelope["error"]["context"] == TOO_NEW_CONTEXT


@pytest.mark.parametrize(
    "argv",
    [
        (CLOUDFALL, "config", "validate", "--schemas", str(SCHEMAS)),
        (CLOUDFALL, "audit", "--schemas", str(SCHEMAS)),
        (
            CLOUDFALL,
            *("deploy", "crm-backend", "--release", "20260101T000000Z-abcdef0"),
            *("--schemas", str(SCHEMAS)),
        ),
        (
            ENGINE,
            *("inventory", "render", "--schemas", str(SCHEMAS)),
            *("--output", "tmp/ansible-inventory.json"),
        ),
    ],
    ids=["config-validate", "audit", "deploy-plan", "engine-inventory-render"],
)
def test_s017_4_a_too_new_project_stops_each_command_before_tmp_or_ansible(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    project = _too_new_project(tmp_path)

    done, record = _run(tmp_path, project, argv)

    _assert_refused_too_new(done)
    assert not (project / "tmp").exists()
    assert not record.exists(), record.read_text(encoding="utf-8")


def test_s017_5_mcp_serve_refuses_a_too_new_project_before_a_client(
    tmp_path: Path,
) -> None:
    project = _too_new_project(tmp_path)

    # stdin is empty, so a server that went on to serve would not hang the
    # test. Stdout is the protocol's, so the refusal is on stderr.
    done, record = _run(tmp_path, project, (CLOUDFALL, "mcp", "serve"))

    _assert_refused_too_new(done, answer="stderr")
    assert not (project / "tmp").exists()
    assert not record.exists(), record.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "argv",
    [
        (CLOUDFALL, "add", "ssh-key", "id.pub", "--owner", "someone"),
        (CLOUDFALL, "inventory", "show", "--schemas", str(SCHEMAS)),
        (CLOUDFALL, "health", "crm-backend", "--schemas", str(SCHEMAS)),
    ],
    ids=["add-ssh-key", "inventory-show", "health"],
)
def test_other_commands_that_read_the_project_refuse_it_too(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    project = _too_new_project(tmp_path)
    (project / "id.pub").write_text(PUBLIC_KEY, encoding="utf-8")

    done, record = _run(tmp_path, project, argv)

    _assert_refused_too_new(done)
    assert not (project / "ssh-public-keys" / "someone.yaml").exists()
    assert not record.exists(), record.read_text(encoding="utf-8")


def test_a_format_0_project_runs_other_commands_with_one_old_format_warning(
    tmp_path: Path,
) -> None:
    project = copy_examples(tmp_path / "project")

    done, _record = _run(
        tmp_path, project, (CLOUDFALL, "inventory", "show", "--schemas", str(SCHEMAS))
    )

    assert done.returncode == 0, done.stdout + done.stderr
    warnings = json.loads(done.stdout)["warnings"]
    assert [warning["code"] for warning in warnings] == ["PROJECT_FORMAT_OLD"]
