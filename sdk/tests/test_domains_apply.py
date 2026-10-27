"""``cloudfall domains apply``: one Domain on its proxy server (spec S-004).

DNS answers come from a stand-in resolver passed to the lifecycle calls, so
no test asks a real resolver. The CLI tests take only the paths that resolve
nothing: the plain plan, a proxied Domain, and the refusals before DNS.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from cloudfall.cli import main
from cloudfall.domain import Hostname, IpAddress, ResourceId
from cloudfall.lifecycle import (
    DnsCheck,
    DomainApply,
    EngineContext,
    ExecutionStep,
    LifecycleError,
    StepRun,
    StepRunner,
    apply_domain,
    preview_domain_apply,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"
PLAYBOOKS = ENGINE / "ansible" / "playbooks"
NGINX_SITE = ENGINE / "ansible" / "roles" / "cloudfall_nginx_site"
# crm-site in the examples: proxy server h2 (address h2.example.internal),
# primaryName crm.example.test, one alias www.crm.example.test.
PROXY = "203.0.113.20"
ELSEWHERE = "198.51.100.7"


@dataclass(slots=True)
class _Resolver:
    """Answer from a fixed table, keeping every name asked."""

    answers: dict[str, frozenset[IpAddress]]
    asked: list[str] = field(default_factory=list)

    def resolve(self, name: Hostname) -> frozenset[IpAddress]:
        self.asked.append(name.value)
        return self.answers.get(name.value, frozenset())


def _resolver(**overrides: str) -> _Resolver:
    """Every name at the proxy server, unless overridden by name."""
    answers = {
        "h2.example.internal": PROXY,
        "crm.example.test": PROXY,
        "www.crm.example.test": PROXY,
    }
    answers.update({name.replace("_", "."): value for name, value in overrides.items()})
    return _Resolver(
        {name: frozenset({IpAddress(value)}) for name, value in answers.items()}
    )


@dataclass(frozen=True, slots=True)
class _Done:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(slots=True)
class _Recorder:
    """Answer each step, writing the receipt a playbook run would write."""

    receipts: Path
    steps: list[ExecutionStep] = field(default_factory=list)

    def __call__(self, step: ExecutionStep) -> _Done:
        self.steps.append(step)
        if step.description == "run domains.yml":
            self.receipts.mkdir(parents=True, exist_ok=True)
            (self.receipts / "crm-site.json").write_text("{}", encoding="utf-8")
        return _Done(0)


def _project(tmp_path: Path, **spec: object) -> Path:
    """Copy the examples, with crm-site's spec keys replaced by ``spec``."""
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    path = project / "domains" / "crm-site.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["spec"].update(spec)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return project


def _context(project: Path, run: StepRunner) -> EngineContext:
    return EngineContext(
        project_directory=project,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=project / "tmp" / "inventory.json",
        run=run,
    )


def _request(project: Path, *, issue_certificate: bool) -> DomainApply:
    return DomainApply(
        domain_id=ResourceId("crm-site"),
        issue_certificate=issue_certificate,
        receipt_directory=project / "tmp" / "deployments",
    )


def _extra_vars(argv: Sequence[str]) -> dict[str, object]:
    extra_vars = json.loads(argv[list(argv).index("--extra-vars") + 1])
    assert isinstance(extra_vars, dict)
    return extra_vars


def _cli(
    project: Path, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, dict[str, Any]]:
    exit_code = main(
        [
            "domains",
            "apply",
            *argv,
            "--project",
            str(project),
            "--schemas",
            str(SCHEMAS),
            "--engine",
            str(ENGINE),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    return exit_code, payload


def _untouched(project: Path) -> None:
    """Neither the inventory render nor a playbook ran, and no receipt exists."""
    assert not (project / "tmp" / "ansible-inventory.json").exists()
    assert not (project / "tmp" / "inventory.json").exists()
    assert not (project / "tmp" / "deployments").exists()


# S-004-3: the plan


def test_s004_3_without_yes_the_plan_runs_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)

    exit_code, payload = _cli(project, capsys, "crm-site")

    assert exit_code == 0, payload
    data = payload["data"]
    assert data["status"] == "plan"
    assert data["effect"] == "would_update"
    assert data["action"] == "domains-apply"
    assert data["domain"] == "crm-site"
    assert data["servers"] == ["h2"]
    assert "h2" in data["would_run"]
    assert "--yes" in data["instruction"]
    assert data["dns_check"] is None
    assert payload["meta"]["dry_run"] is True
    _untouched(project)


def test_s004_3_the_plan_starts_no_engine_step(tmp_path: Path) -> None:
    project = _project(tmp_path)
    recorder = _Recorder(project / "tmp" / "deployments")
    resolver = _resolver()

    planned = preview_domain_apply(
        _context(project, recorder),
        _request(project, issue_certificate=True),
        resolver,
    )

    assert recorder.steps == []
    assert planned.as_dict()["status"] == "plan"
    assert planned.dns_check is DnsCheck.VERIFIED
    _untouched(project)


@pytest.mark.parametrize("yes", [[], ["--yes"]], ids=["plan", "yes"])
def test_an_unknown_domain_is_refused_before_any_engine_step(
    yes: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)

    exit_code, payload = _cli(project, capsys, "nope", *yes)

    assert exit_code == 4, payload
    assert payload["error"]["code"] == "PRECONDITION"
    assert payload["error"]["context"]["code"] == "lifecycle_domain_missing"
    _untouched(project)


# S-004-5: an invalid config


@pytest.mark.parametrize("yes", [[], ["--yes"]], ids=["plan", "yes"])
def test_s004_5_an_invalid_config_exits_before_any_engine_step(
    yes: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, proxy={"server": "ghost"})

    exit_code, payload = _cli(project, capsys, "crm-site", *yes)

    assert exit_code != 0
    assert payload["error"]["code"] == "CONFIG_INVALID"
    _untouched(project)


# S-004-6: names that do not reach the proxy server


@pytest.mark.parametrize("yes", [False, True], ids=["plan", "yes"])
def test_s004_6_an_alias_elsewhere_is_refused_naming_it(
    yes: bool,  # noqa: FBT001 - a pytest parameter
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    recorder = _Recorder(project / "tmp" / "deployments")
    resolver = _resolver(www_crm_example_test=ELSEWHERE)
    call = apply_domain if yes else preview_domain_apply

    with pytest.raises(LifecycleError) as error:
        call(
            _context(project, recorder),
            _request(project, issue_certificate=True),
            resolver,
        )

    assert error.value.code == "domains_dns_unverified"
    assert f"www.crm.example.test resolves to {ELSEWHERE}" in error.value.detail
    # The primary name reaches the proxy, so only the alias is named.
    assert error.value.detail.count("crm.example.test resolves") == 1
    assert PROXY in error.value.detail
    assert recorder.steps == []
    _untouched(project)


def test_s004_6_a_name_that_resolves_to_nothing_is_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    recorder = _Recorder(project / "tmp" / "deployments")
    resolver = _resolver()
    del resolver.answers["crm.example.test"]

    with pytest.raises(LifecycleError) as error:
        apply_domain(
            _context(project, recorder),
            _request(project, issue_certificate=True),
            resolver,
        )

    assert error.value.code == "domains_dns_unverified"
    assert "crm.example.test resolves to nothing" in error.value.detail
    assert recorder.steps == []


def test_a_proxy_server_address_that_is_an_ip_is_taken_as_it_is(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    server = project / "servers" / "h2.yaml"
    document = yaml.safe_load(server.read_text(encoding="utf-8"))
    document["spec"]["address"] = "192.0.2.10"
    server.write_text(yaml.safe_dump(document), encoding="utf-8")
    resolver = _resolver(
        crm_example_test="192.0.2.10", www_crm_example_test="192.0.2.10"
    )

    planned = preview_domain_apply(
        _context(project, _Recorder(tmp_path / "unused")),
        _request(project, issue_certificate=True),
        resolver,
    )

    assert planned.dns_check is DnsCheck.VERIFIED
    assert resolver.asked == ["crm.example.test", "www.crm.example.test"]


def test_without_issue_certificate_dns_is_not_checked(tmp_path: Path) -> None:
    project = _project(tmp_path)
    resolver = _resolver(www_crm_example_test=ELSEWHERE)

    planned = preview_domain_apply(
        _context(project, _Recorder(tmp_path / "unused")),
        _request(project, issue_certificate=False),
        resolver,
    )

    assert planned.dns_check is None
    assert "dnsCheck" not in planned.as_dict()
    assert resolver.asked == []


# S-004-7: a proxied Domain


def test_s004_7_a_proxied_domain_skips_the_dns_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, edge={"provider": "cloudflare", "mode": "proxied"})

    exit_code, payload = _cli(project, capsys, "crm-site", "--issue-certificate")

    assert exit_code == 0, payload
    assert payload["data"]["dns_check"] == "skipped-proxied"
    assert payload["data"]["status"] == "plan"
    _untouched(project)


def test_s004_7_a_proxied_domain_asks_no_resolver(tmp_path: Path) -> None:
    project = _project(tmp_path, edge={"provider": "cloudflare", "mode": "proxied"})
    recorder = _Recorder(project / "tmp" / "deployments")
    resolver = _resolver(crm_example_test=ELSEWHERE)

    applied = apply_domain(
        _context(project, recorder),
        _request(project, issue_certificate=True),
        resolver,
    )

    assert resolver.asked == []
    assert applied.as_dict()["dnsCheck"] == "skipped-proxied"


# S-004-8: no certificate for tls.mode disabled


@pytest.mark.parametrize("yes", [[], ["--yes"]], ids=["plan", "yes"])
def test_s004_8_tls_disabled_refuses_issue_certificate(
    yes: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, tls={"mode": "disabled"})

    exit_code, payload = _cli(project, capsys, "crm-site", "--issue-certificate", *yes)

    assert exit_code == 4, payload
    assert payload["error"]["code"] == "PRECONDITION"
    assert payload["error"]["context"]["code"] == "domains_tls_disabled"
    _untouched(project)


def test_s004_8_tls_disabled_is_refused_before_dns(tmp_path: Path) -> None:
    project = _project(tmp_path, tls={"mode": "disabled"})
    recorder = _Recorder(project / "tmp" / "deployments")
    resolver = _resolver()

    with pytest.raises(LifecycleError) as error:
        apply_domain(
            _context(project, recorder),
            _request(project, issue_certificate=True),
            resolver,
        )

    assert error.value.code == "domains_tls_disabled"
    assert resolver.asked == []
    assert recorder.steps == []


# S-004-9: the certificate run


def test_s004_9_issue_certificate_passes_the_flag_to_domains_yml(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    receipts = project / "tmp" / "deployments"
    recorder = _Recorder(receipts)

    applied = apply_domain(
        _context(project, recorder),
        _request(project, issue_certificate=True),
        _resolver(),
    )

    render, playbook = recorder.steps
    assert render.description == "render Ansible inventory"
    assert playbook.argv[-1] == str(PLAYBOOKS / "domains.yml")
    assert playbook.argv[playbook.argv.index("--limit") + 1] == "h2"
    assert _extra_vars(playbook.argv) == {
        "cloudfall_domains_issue_certificates": True,
        "cloudfall_domains_receipt_directory": str(receipts),
        "cloudfall_domains_domain_id": "crm-site",
    }
    assert applied.as_dict() == {
        "status": "ok",
        "action": "domains-apply",
        "domain": "crm-site",
        "servers": ["h2"],
        "receipt": str(receipts / "crm-site.json"),
        "dnsCheck": "verified",
    }


def test_a_run_that_writes_no_receipt_is_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)

    def no_receipt(_step: ExecutionStep) -> StepRun:
        return _Done(0)

    with pytest.raises(LifecycleError) as error:
        apply_domain(
            _context(project, no_receipt),
            _request(project, issue_certificate=False),
            _resolver(),
        )

    assert error.value.code == "lifecycle_receipt_missing"


# S-004-1: domains.yml for real, on the proxy server only


STUB_ROLE = """---
- name: Record what this host rendered
  ansible.builtin.lineinfile:
    path: {log}
    line: >-
      {{{{ cloudfall_server_id }}}}:{{{{ cloudfall_nginx_site_domain.id }}}}:{{{{
      cloudfall_nginx_site_issue_certificate | bool }}}}
    create: true
    mode: "0644"
  delegate_to: localhost
  become: false

- name: Create the receipt directory
  ansible.builtin.file:
    path: "{{{{ cloudfall_nginx_site_receipt_directory }}}}"
    state: directory
    mode: "0755"
  delegate_to: localhost
  become: false

- name: Write a stand-in receipt
  ansible.builtin.copy:
    content: "{{}}"
    dest: >-
      {{{{ cloudfall_nginx_site_receipt_directory }}}}/{{{{
      cloudfall_nginx_site_domain.id }}}}.json
    mode: "0644"
  delegate_to: localhost
  become: false
"""


@dataclass(slots=True)
class _LocalRun:
    """Run each step for real, with every server a local connection.

    The rendered inventory keeps every host and group; only how Ansible
    reaches a host changes. ``cloudfall_nginx_site`` is swapped for a stub
    through ``ANSIBLE_ROLES_PATH``, since the real role needs Debian.
    """

    inventory_file: Path
    roles: Path
    playbook_output: list[str] = field(default_factory=list)

    def __call__(self, step: ExecutionStep) -> StepRun:
        environment = {**os.environ, **step.environment}
        if step.description.startswith("run "):
            self._connect_locally()
            environment["ANSIBLE_ROLES_PATH"] = str(self.roles)
        done = subprocess.run(  # noqa: S603 - argv built by the SDK.
            list(step.argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=environment,
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
                }
            )
        self.inventory_file.write_text(json.dumps(rendered), encoding="utf-8")


def _local_run(tmp_path: Path, project: Path) -> tuple[_LocalRun, Path]:
    log = tmp_path / "hosts.log"
    tasks = tmp_path / "roles" / "cloudfall_nginx_site" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text(
        STUB_ROLE.format(log=json.dumps(str(log))), encoding="utf-8"
    )
    run = _LocalRun(project / "tmp" / "inventory.json", tmp_path / "roles")
    return run, log


def _recap_hosts(output: str) -> list[str]:
    recap = output.split("PLAY RECAP", 1)[1]
    return re.findall(r"^(\S+)\s+:\s+ok=", recap, flags=re.MULTILINE)


DOMAINS_TASKS = {
    "Gathering Facts",
    "Require an upstream on every Domain this run renders",
    "Render declared domains as managed virtual hosts",
    "cloudfall_nginx_site : Record what this host rendered",
    "cloudfall_nginx_site : Create the receipt directory",
    "cloudfall_nginx_site : Write a stand-in receipt",
}


# proves: S-004-9 (the flag reaches the role, and the receipt is written)
@pytest.mark.parametrize("issue_certificate", [False, True], ids=["http", "tls"])
def test_s004_1_only_the_proxy_server_runs_domains_yml(
    issue_certificate: bool,  # noqa: FBT001 - a pytest parameter
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    run, log = _local_run(tmp_path, project)
    receipts = project / "tmp" / "deployments"

    applied = apply_domain(
        _context(project, run),
        _request(project, issue_certificate=issue_certificate),
        _resolver(),
    )

    (output,) = run.playbook_output
    assert _recap_hosts(output) == ["h2"], output
    ran = set(re.findall(r"^TASK \[(.+)\]", output, flags=re.MULTILINE))
    assert ran <= DOMAINS_TASKS, output
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"h2:crm-site:{issue_certificate}"
    ]
    assert (receipts / "crm-site.json").is_file()
    assert applied.receipt == receipts / "crm-site.json"


def test_s004_1_domains_yml_runs_no_baseline_firewall_or_service_role() -> None:
    """The stub above stands in for this role; nothing else can run."""
    plays = yaml.safe_load((PLAYBOOKS / "domains.yml").read_text(encoding="utf-8"))
    included: list[str] = []
    for play in plays:
        assert set(play) & {"roles", "import_playbook", "pre_tasks", "post_tasks"} == (
            set()
        )
        for task in play["tasks"]:
            role = task.get("ansible.builtin.include_role")
            if role is not None:
                included.append(role["name"])
    assert included == ["cloudfall_nginx_site"]
    meta = yaml.safe_load((NGINX_SITE / "meta" / "main.yml").read_text("utf-8"))
    assert meta["dependencies"] == []
    for tasks in (NGINX_SITE / "tasks").glob("*.yml"):
        text = tasks.read_text(encoding="utf-8")
        for keyword in ("include_role", "import_role", "import_playbook"):
            assert keyword not in text, tasks
