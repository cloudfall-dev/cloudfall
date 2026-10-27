"""``cloudfall domains observe``: one Domain read on its proxy server (spec S-004).

The real ``domains-observe.yml`` runs against a local connection: the proxy
server's files sit under ``tmp_path``, and ``openssl`` and ``systemctl`` are
stand-in scripts found first on ``PATH``, since the real ones need a Debian
server with a certificate and systemd.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from cloudfall.cli import main
from cloudfall.domain import ResourceId
from cloudfall.lifecycle import (
    DomainObserve,
    EngineContext,
    ExecutionStep,
    LifecycleError,
    StepRun,
    StepRunner,
    observe_domain,
)

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"
PLAYBOOK = ENGINE / "ansible" / "playbooks" / "domains-observe.yml"
# crm-site in the examples: proxy server h2, origin h1, primaryName crm.example.test.
PRIMARY_NAME = "crm.example.test"
NOT_AFTER = "Jan  5 09:34:43 2027 GMT"


def _project(tmp_path: Path, vhost: Path) -> Path:
    """Copy the examples, with crm-site's vhost at ``vhost``."""
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    path = project / "domains" / "crm-site.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["spec"]["proxy"]["configurationPath"] = str(vhost)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return project


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_receipt(project: Path, vhost: Path) -> Path:
    """Write the receipt a ``domains.yml`` run leaves for the vhost as it is."""
    receipts = project / "tmp" / "deployments"
    receipts.mkdir(parents=True)
    receipt = receipts / "crm-site.json"
    receipt.write_text(
        json.dumps(
            {
                "apiVersion": "cloudfall/v1",
                "kind": "DeploymentReceipt",
                "metadata": {"id": "crm-site", "description": "test receipt"},
                "spec": {
                    "domain": "crm-site",
                    "server": "h2",
                    "deployedAt": "2026-10-27T10:00:00Z",
                    "configurationPath": str(vhost),
                    "configurationSha256": _sha256(vhost),
                    "previousConfigurationSha256": None,
                },
            }
        ),
        encoding="utf-8",
    )
    return receipt


def _snapshot(directory: Path) -> dict[str, tuple[str, int]]:
    """Every file under ``directory``, by content and modification time."""
    return {
        str(path.relative_to(directory)): (_sha256(path), path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _stand_ins(tmp_path: Path, *, enabled: bool, active: bool) -> Path:
    """``openssl`` and ``systemctl`` scripts answering as a server would."""
    tools = tmp_path / "bin"
    tools.mkdir()
    openssl = tools / "openssl"
    openssl.write_text(
        '#!/bin/sh\ntest "$3" = -enddate || exit 64\n'
        f'echo "notAfter={NOT_AFTER}"\n',
        encoding="utf-8",
    )
    is_enabled = "echo enabled; exit 0" if enabled else "echo disabled; exit 1"
    is_active = "echo active; exit 0" if active else "echo inactive; exit 3"
    systemctl = tools / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        'test "$2" = certbot.timer || exit 64\n'
        'case "$1" in\n'
        f"  is-enabled) {is_enabled};;\n"
        f"  is-active) {is_active};;\n"
        "esac\n"
        "exit 64\n",
        encoding="utf-8",
    )
    for script in (openssl, systemctl):
        script.chmod(0o755)
    return tools


@dataclass(slots=True)
class _LocalRun:
    """Run each step for real, with every server a local connection.

    The rendered inventory keeps every host and group; only how Ansible
    reaches a host changes, and where the certificates live.
    """

    inventory_file: Path
    project: Path
    tools: Path
    certificates: Path
    playbook_output: list[str] = field(default_factory=list)

    def __call__(self, step: ExecutionStep) -> StepRun:
        environment = {**os.environ, **step.environment}
        if step.description.startswith("run "):
            self._connect_locally()
            environment["PATH"] = f"{self.tools}{os.pathsep}{environment['PATH']}"
        done = subprocess.run(  # noqa: S603 - argv built by the SDK.
            list(step.argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=environment,
            cwd=self.project,
        )
        if step.description.startswith("run "):
            self.playbook_output.append(done.stdout + done.stderr)
        return done

    def _connect_locally(self) -> None:
        rendered = json.loads(self.inventory_file.read_text(encoding="utf-8"))
        hosts = rendered["all"]["children"]["cloudfall_servers"]["hosts"]
        assert sorted(hosts) == ["h1", "h2"]
        for host in hosts.values():
            host.update(
                {
                    "ansible_connection": "local",
                    "ansible_python_interpreter": sys.executable,
                    "ansible_become": False,
                    "cloudfall_domains_observe_certificate_root": str(
                        self.certificates
                    ),
                }
            )
        self.inventory_file.write_text(json.dumps(rendered), encoding="utf-8")


@dataclass(frozen=True, slots=True)
class _Server:
    """The proxy server's files, under ``tmp_path``."""

    root: Path

    @property
    def vhost(self) -> Path:
        return self.root / "etc" / "nginx" / "crm.example.test.conf"

    @property
    def certificates(self) -> Path:
        return self.root / "etc" / "letsencrypt" / "live"

    @property
    def certificate(self) -> Path:
        return self.certificates / PRIMARY_NAME / "fullchain.pem"


def _setup(
    tmp_path: Path, *, enabled: bool = True, active: bool = True
) -> tuple[Path, _Server, _LocalRun, EngineContext, DomainObserve]:
    server = _Server(tmp_path / "server")
    server.vhost.parent.mkdir(parents=True)
    project = _project(tmp_path, server.vhost)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    run = _LocalRun(
        inventory_file=scratch / "ansible-inventory.json",
        project=project,
        tools=_stand_ins(tmp_path, enabled=enabled, active=active),
        certificates=server.certificates,
    )
    context = EngineContext(
        project_directory=project,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=run.inventory_file,
        run=run,
    )
    request = DomainObserve(
        domain_id=ResourceId("crm-site"),
        receipt_directory=project / "tmp" / "deployments",
        reading_file=scratch / "reading.json",
    )
    return project, server, run, context, request


def _observed(context: EngineContext, request: DomainObserve) -> dict[str, Any]:
    return observe_domain(context, request).as_dict()


def _recap_hosts(output: str) -> list[str]:
    recap = output.split("PLAY RECAP", 1)[1]
    return re.findall(r"^(\S+)\s+:\s+ok=", recap, flags=re.MULTILINE)


# S-004-14: the reading


def test_s004_14_observe_reads_the_proxy_server_and_changes_nothing(
    tmp_path: Path,
) -> None:
    project, server, run, context, request = _setup(tmp_path)
    server.vhost.write_text("server { listen 80; }\n", encoding="utf-8")
    server.certificate.parent.mkdir(parents=True)
    server.certificate.write_text("stand-in certificate\n", encoding="utf-8")
    _write_receipt(project, server.vhost)
    project_before = _snapshot(project)
    server_before = _snapshot(server.root)

    observed = _observed(context, request)

    (output,) = run.playbook_output
    assert _recap_hosts(output) == ["h2"], output
    assert observed["server"] == "h2"
    assert observed["vhost"] == {
        "path": str(server.vhost),
        "present": True,
        "sha256": _sha256(server.vhost),
    }
    assert observed["certificate"] == {
        "path": str(server.certificate),
        "present": True,
        "notAfter": "2027-01-05T09:34:43Z",
    }
    assert observed["certbotTimer"] == {"enabled": True, "active": True}
    assert _snapshot(project) == project_before
    assert _snapshot(server.root) == server_before


def test_s004_14_a_missing_vhost_is_a_reading_not_an_error(tmp_path: Path) -> None:
    project, server, _run, context, request = _setup(
        tmp_path, enabled=False, active=False
    )
    project_before = _snapshot(project)

    observed = _observed(context, request)

    assert observed["status"] == "ok"
    assert observed["vhost"] == {
        "path": str(server.vhost),
        "present": False,
        "sha256": None,
    }
    assert observed["certificate"] == {
        "path": str(server.certificate),
        "present": False,
        "notAfter": None,
    }
    assert observed["certbotTimer"] == {"enabled": False, "active": False}
    assert not server.vhost.exists()
    assert _snapshot(project) == project_before


def test_s004_14_the_playbook_holds_only_read_tasks() -> None:
    """Every server task reads; the one write is the reading, on the controller."""
    (play,) = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    assert set(play) & {"roles", "import_playbook", "pre_tasks", "post_tasks"} == set()
    reads = {
        "ansible.builtin.set_fact",
        "ansible.builtin.assert",
        "ansible.builtin.stat",
    }
    for task in play["tasks"]:
        module = next(key for key in task if key.startswith("ansible.builtin."))
        if module == "ansible.builtin.command":
            assert task["changed_when"] is False, task["name"]
        elif module == "ansible.builtin.copy":
            assert task["delegate_to"] == "localhost", task["name"]
            assert task["become"] is False
            assert task["ansible.builtin.copy"]["dest"] == (
                "{{ cloudfall_domains_observe_reading }}"
            )
        else:
            assert module in reads, task["name"]


# S-004-15: the vhost against the last receipt


def test_s004_15_receipt_matches_until_the_vhost_is_edited(tmp_path: Path) -> None:
    project, server, _run, context, request = _setup(tmp_path)
    server.vhost.write_text("server { listen 80; }\n", encoding="utf-8")
    receipt = _write_receipt(project, server.vhost)
    applied = _sha256(server.vhost)

    unchanged = _observed(context, request)
    server.vhost.write_text("server { listen 8080; }\n", encoding="utf-8")
    edited = _observed(context, request)

    assert unchanged["receipt"] == {
        "configurationSha256": applied,
        "deployedAt": "2026-10-27T10:00:00Z",
        "matches": True,
    }
    assert edited["receipt"] == {**unchanged["receipt"], "matches": False}
    assert edited["vhost"]["sha256"] == _sha256(server.vhost)
    assert json.loads(receipt.read_text(encoding="utf-8"))["spec"][
        "configurationSha256"
    ] == applied


def test_s004_15_no_receipt_reads_as_null(tmp_path: Path) -> None:
    _project_dir, server, _run, context, request = _setup(tmp_path)
    server.vhost.write_text("server { listen 80; }\n", encoding="utf-8")

    observed = _observed(context, request)

    assert observed["receipt"] is None
    assert observed["vhost"]["present"] is True


def test_s004_15_a_removed_vhost_does_not_match_its_receipt(tmp_path: Path) -> None:
    project, server, _run, context, request = _setup(tmp_path)
    server.vhost.write_text("server { listen 80; }\n", encoding="utf-8")
    _write_receipt(project, server.vhost)
    server.vhost.unlink()

    observed = _observed(context, request)

    assert observed["receipt"]["matches"] is False


# S-004-4: an unknown Domain


@dataclass(frozen=True, slots=True)
class _Done:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(slots=True)
class _Recorder:
    """Answer each step, writing ``reading`` where the playbook hands it back."""

    reading: object
    steps: list[ExecutionStep] = field(default_factory=list)

    def __call__(self, step: ExecutionStep) -> _Done:
        self.steps.append(step)
        if step.description == "run domains-observe.yml":
            argv = list(step.argv)
            extra_vars = json.loads(argv[argv.index("--extra-vars") + 1])
            Path(extra_vars["cloudfall_domains_observe_reading"]).write_text(
                json.dumps(self.reading), encoding="utf-8"
            )
        return _Done(0)


def _recorded(tmp_path: Path, run: StepRunner) -> tuple[EngineContext, DomainObserve]:
    project = tmp_path / "project"
    if not project.exists():
        shutil.copytree(EXAMPLES, project)
    context = EngineContext(
        project_directory=project,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=tmp_path / "inventory.json",
        run=run,
    )
    request = DomainObserve(
        domain_id=ResourceId("crm-site"),
        receipt_directory=project / "tmp" / "deployments",
        reading_file=tmp_path / "reading.json",
    )
    return context, request


def _cli(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
    engine: Path = ENGINE,
) -> tuple[int, dict[str, Any]]:
    exit_code = main(
        [
            "domains",
            "observe",
            *argv,
            "--project",
            str(project),
            "--schemas",
            str(SCHEMAS),
            "--engine",
            str(engine),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    return exit_code, payload


def test_s004_4_observe_refuses_an_unknown_domain_before_any_engine_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)

    exit_code, payload = _cli(project, capsys, "nope")

    assert exit_code == 4, payload
    assert payload["error"]["code"] == "PRECONDITION"
    assert payload["error"]["context"]["code"] == "lifecycle_domain_missing"
    assert not (project / "tmp").exists()


def test_s004_4_observe_starts_no_step_for_an_unknown_domain(tmp_path: Path) -> None:
    recorder = _Recorder(reading={})
    context, request = _recorded(tmp_path, recorder)

    with pytest.raises(LifecycleError) as error:
        observe_domain(
            context,
            DomainObserve(
                domain_id=ResourceId("nope"),
                receipt_directory=request.receipt_directory,
                reading_file=request.reading_file,
            ),
        )

    assert error.value.code == "lifecycle_domain_missing"
    assert recorder.steps == []
    assert not context.inventory_file.exists()


# The engine contract


def _reading(**overrides: object) -> dict[str, object]:
    reading: dict[str, object] = {
        "domain": "crm-site",
        "server": "h2",
        "vhost": {"path": "/etc/nginx/x.conf", "present": False, "sha256": None},
        "certificate": {"path": "/etc/x.pem", "present": False, "notAfter": None},
        "certbotTimer": {"enabled": False, "active": False},
    }
    reading.update(overrides)
    return reading


def test_observe_runs_the_read_playbook_on_the_proxy_server_only(
    tmp_path: Path,
) -> None:
    recorder = _Recorder(reading=_reading())
    context, request = _recorded(tmp_path, recorder)

    observe_domain(context, request)

    render, playbook = recorder.steps
    assert render.description == "render Ansible inventory"
    assert playbook.argv[-1] == str(PLAYBOOK)
    assert playbook.argv[playbook.argv.index("--limit") + 1] == "h2"
    extra_vars = json.loads(playbook.argv[playbook.argv.index("--extra-vars") + 1])
    assert extra_vars == {
        "cloudfall_domains_domain_id": "crm-site",
        "cloudfall_domains_observe_reading": str(request.reading_file.resolve()),
    }


@pytest.mark.parametrize(
    "reading",
    [
        _reading(server="h1"),
        _reading(certbotTimer={"enabled": "yes", "active": False}),
        _reading(vhost={"path": "/etc/x.conf", "present": False, "sha256": "ab"}),
        _reading(
            certificate={"path": "/etc/x.pem", "present": True, "notAfter": "soon"}
        ),
        _reading(vhost={"path": "/etc/x.conf", "present": True, "sha256": "ab"}),
    ],
    ids=["other-server", "not-a-boolean", "hash-without-file", "bad-date", "bad-hash"],
)
def test_a_malformed_reading_fails_the_engine_step(
    reading: dict[str, object], tmp_path: Path
) -> None:
    context, request = _recorded(tmp_path, _Recorder(reading=reading))

    with pytest.raises(LifecycleError) as error:
        observe_domain(context, request)

    assert error.value.code == "lifecycle_execution_failed"


STAND_IN_PLAYBOOK = """\
---
- name: Hand back a canned reading without contacting a server
  hosts: cloudfall_servers
  gather_facts: false
  tasks:
    - name: Write the reading
      ansible.builtin.copy:
        content: {reading}
        dest: "{{{{ cloudfall_domains_observe_reading }}}}"
        mode: "0600"
      delegate_to: localhost
"""


def _stand_in_engine(tmp_path: Path, reading: dict[str, object]) -> Path:
    """Lay out an engine whose ``domains-observe.yml`` hands back ``reading``."""
    engine = tmp_path / "engine"
    (engine / "ansible" / "playbooks").mkdir(parents=True)
    shutil.copy(ENGINE / "ansible" / "ansible.cfg", engine / "ansible" / "ansible.cfg")
    (engine / "ansible" / "playbooks" / "domains-observe.yml").write_text(
        STAND_IN_PLAYBOOK.format(reading=json.dumps(json.dumps(reading))),
        encoding="utf-8",
    )
    return engine


def test_s004_14_the_cli_answers_snake_case_and_leaves_the_project_as_it_was(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stand-in engine hands back a reading; the CLI renders outside the project."""
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    engine = _stand_in_engine(
        tmp_path,
        _reading(
            certificate={
                "path": f"/etc/letsencrypt/live/{PRIMARY_NAME}/fullchain.pem",
                "present": True,
                "notAfter": NOT_AFTER,
            },
            certbotTimer={"enabled": True, "active": True},
        ),
    )
    before = _snapshot(project)

    exit_code, payload = _cli(project, capsys, "crm-site", engine=engine)

    assert exit_code == 0, payload
    assert payload["data"] == {
        "status": "ok",
        "action": "domains-observe",
        "domain": "crm-site",
        "server": "h2",
        "vhost": {"path": "/etc/nginx/x.conf", "present": False, "sha256": None},
        "receipt": None,
        "certificate": {
            "path": f"/etc/letsencrypt/live/{PRIMARY_NAME}/fullchain.pem",
            "present": True,
            "not_after": "2027-01-05T09:34:43Z",
        },
        "certbot_timer": {"enabled": True, "active": True},
    }
    assert _snapshot(project) == before


def test_s004_15_the_cli_answers_the_receipt_comparison(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    vhost = tmp_path / "applied.conf"
    vhost.write_text("server { listen 80; }\n", encoding="utf-8")
    _write_receipt(project, vhost)
    engine = _stand_in_engine(
        tmp_path,
        _reading(vhost={"path": str(vhost), "present": True, "sha256": _sha256(vhost)}),
    )

    exit_code, payload = _cli(project, capsys, "crm-site", engine=engine)

    assert exit_code == 0, payload
    assert payload["data"]["receipt"] == {
        "configuration_sha256": _sha256(vhost),
        "deployed_at": "2026-10-27T10:00:00Z",
        "matches": True,
    }


def test_an_unreachable_proxy_server_exits_engine_step_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    server = project / "servers" / "h2.yaml"
    document = yaml.safe_load(server.read_text(encoding="utf-8"))
    # .invalid never resolves (RFC 6761), so the run fails without a network.
    document["spec"]["address"] = "h2.invalid"
    server.write_text(yaml.safe_dump(document), encoding="utf-8")

    exit_code, payload = _cli(project, capsys, "crm-site")

    assert exit_code != 0, payload
    assert payload["error"]["code"] == "ENGINE_STEP_FAILED"
