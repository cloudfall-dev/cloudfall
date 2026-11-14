"""The Elasticsearch role converges one node, secured and idempotent (S-012, #310).

The role needs a Debian 13 host, so its decisions are evaluated with
Ansible's own templating, its files are rendered from its templates, and the
heap guard runs for real on localhost in check mode.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.ansible_data import as_ansible_data
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from elasticsearch_project import SCHEMAS, elasticsearch_service, write_project

ROOT = Path(__file__).parents[2]
ROLES = ROOT / "engine" / "ansible" / "roles"
ROLE = ROLES / "cloudfall_elasticsearch"
SERVICES = ROOT / "engine" / "ansible" / "playbooks" / "services.yml"
# Modules that change a host; every task before the first one only reads.
CHANGING = frozenset(
    {
        "ansible.builtin.apt",
        "ansible.builtin.copy",
        "ansible.builtin.deb822_repository",
        "ansible.builtin.dpkg_selections",
        "ansible.builtin.file",
        "ansible.builtin.get_url",
        "ansible.builtin.group",
        "ansible.builtin.lineinfile",
        "ansible.builtin.systemd_service",
        "ansible.builtin.template",
        "ansible.builtin.uri",
        "ansible.builtin.user",
    }
)


def _service(**elasticsearch: object) -> dict[str, object]:
    """Return the service as the rendered inventory carries S-012-1."""
    return {
        "id": "search-main",
        "serviceKind": "elasticsearch",
        "environment": "production",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": 9200},
        "elasticsearch": {
            "version": "9.2.4",
            "heapMb": 4096,
            "nodes": ["h1"],
            "clusterName": "search-main",
            "users": [],
            **elasticsearch,
        },
        "backup": {
            "directory": "/var/backups/cloudfall/search-main",
            "onCalendar": "*-*-* 03:20:00 UTC",
            "restoreCheckOnCalendar": "Sun *-*-* 05:00:00 UTC",
            "retentionDays": 14,
        },
        "metrics": {"enabled": True},
    }


def _tasks() -> list[dict[str, object]]:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "main.yml"))
    assert isinstance(tasks, list)
    return tasks


def _task(name: str) -> dict[str, object]:
    return next(task for task in _tasks() if task["name"] == name)


def _module(task: dict[str, object]) -> str:
    """Return the task's module, or ``block`` for a block of tasks."""
    return next((key for key in task if key.startswith("ansible.builtin.")), "block")


def _defaults() -> dict[str, object]:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    assert isinstance(defaults, dict)
    return defaults


def _variables(**extra: object) -> dict[str, object]:
    return {
        **_defaults(),
        "ansible_check_mode": False,
        "ansible_facts": {
            "distribution": "Debian",
            "distribution_major_version": "13",
            "service_mgr": "systemd",
            "memtotal_mb": 8192,
        },
        "cloudfall_server_id": "h1",
        "cloudfall_elasticsearch_service": _service(),
        "cloudfall_elasticsearch_account_files": {
            "cloudfallPassword": "/tmp/staged/cloudfall.password",  # noqa: S108
            "cloudfallHash": "/tmp/staged/cloudfall.bcrypt",  # noqa: S108
            "elasticHash": "/tmp/staged/elastic.bcrypt",  # noqa: S108
        },
        **extra,
    }


def _templar(variables: dict[str, object]) -> Templar:
    return Templar(loader=DataLoader(), variables=variables)


def _holds(task: dict[str, object], variables: dict[str, object]) -> bool:
    module = cast("dict[str, list[str]]", task["ansible.builtin.assert"])
    # A task's vars are templates of the role, as Ansible trusts them.
    scoped = {
        **variables,
        **{
            name: trust_as_template(value) if isinstance(value, str) else value
            for name, value in cast("dict[str, object]", task.get("vars", {})).items()
        },
    }
    return all(
        _templar(scoped).evaluate_conditional(trust_as_template(str(condition)))
        for condition in module["that"]
    )


def _value(expression: str, variables: dict[str, object]) -> object:
    return _templar(variables).template(trust_as_template(expression))


def _render(name: str, variables: dict[str, object]) -> str:
    text = (ROLE / "templates" / name).read_text(encoding="utf-8")
    return str(_templar(variables).template(trust_as_template(text)))


def _bootstrap(variables: dict[str, object]) -> bool:
    install = _task("Install the managed Elasticsearch configuration")
    expression = cast("dict[str, str]", install["vars"])[
        "cloudfall_elasticsearch_bootstrap"
    ]
    return bool(_value(expression, variables))


def _stat(*, exists: bool) -> dict[str, object]:
    return {"stat": {"exists": exists}}


# proves: S-012-4
def test_s012_4_the_heap_guard_runs_before_any_task_that_changes_the_host() -> None:
    tasks = _tasks()
    first_change = next(
        index for index, task in enumerate(tasks) if _module(task) in CHANGING
    )

    assert tasks[0]["name"] == "Refuse a heap above half of the host's memory"
    assert _module(tasks[0]) == "ansible.builtin.assert"
    assert all(
        _module(task) in {"ansible.builtin.assert", "ansible.builtin.command"}
        for task in tasks[:first_change]
    )
    reads = [task for task in tasks[:first_change] if "ansible.builtin.command" in task]
    assert all(task["changed_when"] is False for task in reads)


# proves: S-012-4
def test_s012_4_a_heap_above_half_of_the_memory_is_refused() -> None:
    guard = _tasks()[0]
    half = _variables()
    above = _variables(cloudfall_elasticsearch_service=_service(heapMb=4097))

    assert _holds(guard, half)
    assert not _holds(guard, above)


# proves: S-012-4
def test_s012_4_the_heap_guard_stops_a_real_run_before_any_other_task(
    tmp_path: Path,
) -> None:
    """The role runs on localhost (in check mode) with a heap no host holds."""
    play = [
        {
            "name": "Converge an Elasticsearch service whose heap is too large",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": True,
            "vars": {"cloudfall_server_id": "h1"},
            "tasks": [
                {
                    "name": "Run the Elasticsearch role",
                    "ansible.builtin.include_role": {"name": "cloudfall_elasticsearch"},
                    "vars": {
                        "cloudfall_elasticsearch_service": _service(heapMb=10**9),
                        "cloudfall_elasticsearch_account_files": {},
                    },
                }
            ],
        }
    ]
    playbook = tmp_path / "heap-guard.yml"
    playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
    binary = shutil.which("ansible-playbook")
    assert binary is not None

    done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [binary, "--check", "--inventory", "localhost,", str(playbook)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "ANSIBLE_ROLES_PATH": str(ROLES)},
    )

    output = done.stdout + done.stderr
    assert done.returncode != 0, output
    assert "is above half of the" in output
    role_tasks = re.findall(r"TASK \[cloudfall_elasticsearch : ([^\]]+)\]", output)
    assert role_tasks[-1] == "Refuse a heap above half of the host's memory"
    assert re.search(
        r"localhost\s+: ok=\d+\s+changed=0\s+unreachable=0\s+failed=1", output
    )


# proves: S-012-3
def test_s012_3_the_rendered_configuration_is_a_secured_loopback_cluster() -> None:
    first_start = yaml.safe_load(
        _render(
            "elasticsearch.yml.j2",
            _variables(cloudfall_elasticsearch_bootstrap=True),
        )
    )
    formed = yaml.safe_load(
        _render(
            "elasticsearch.yml.j2",
            _variables(cloudfall_elasticsearch_bootstrap=False),
        )
    )

    assert formed == {
        "cluster.name": "search-main",
        "node.name": "h1",
        "path.data": "/var/lib/elasticsearch",
        "path.logs": "/var/log/elasticsearch",
        "bootstrap.memory_lock": True,
        "network.host": "127.0.0.1",
        "http.port": 9200,
        "discovery.seed_hosts": ["127.0.0.1"],
        "xpack.security.enabled": True,
        "xpack.security.enrollment.enabled": False,
        "xpack.security.http.ssl.enabled": False,
        "xpack.security.transport.ssl.enabled": False,
    }
    assert first_start == {**formed, "cluster.initial_master_nodes": ["h1"]}
    for rendered in (first_start, formed):
        assert "discovery.type" not in rendered


# proves: S-012-3
def test_s012_3_cluster_name_is_the_declared_one_or_the_service_id() -> None:
    named = _variables(
        cloudfall_elasticsearch_service=_service(clusterName="search"),
        cloudfall_elasticsearch_bootstrap=False,
    )

    assert yaml.safe_load(_render("elasticsearch.yml.j2", named))["cluster.name"] == (
        "search"
    )


# proves: S-012-3
def test_s012_3_initial_master_nodes_only_until_the_data_path_holds_state() -> None:
    fresh = _variables(cloudfall_elasticsearch_cluster_state=_stat(exists=False))
    formed = _variables(cloudfall_elasticsearch_cluster_state=_stat(exists=True))
    joining = _variables(
        cloudfall_elasticsearch_cluster_state=_stat(exists=False),
        cloudfall_elasticsearch_service=_service(
            existingCluster={
                "seedHosts": ["10.0.0.11:9300"],
                "caCertificateFile": "pki/old-ca.pem",
            }
        ),
    )

    assert _bootstrap(fresh) is True
    assert _bootstrap(formed) is False
    assert _bootstrap(joining) is False


# proves: S-012-3
def test_s012_3_the_first_converge_removes_initial_master_nodes_once_formed() -> None:
    """Removed in the run that bootstrapped, so a second run finds no change."""
    remove = _task("Remove cluster.initial_master_nodes once the cluster has formed")
    conditions = cast("list[str]", remove["when"])
    first_run = _variables(
        cloudfall_elasticsearch_cluster_state=_stat(exists=False),
        cloudfall_elasticsearch_formed=_stat(exists=True),
    )
    second_run = _variables(
        cloudfall_elasticsearch_cluster_state=_stat(exists=True),
    )

    def runs(variables: dict[str, object]) -> bool:
        return all(
            _templar(variables).evaluate_conditional(trust_as_template(condition))
            for condition in conditions
        )

    assert runs(first_run)
    assert not runs(second_run)
    assert cast("dict[str, object]", remove["vars"]) == {
        "cloudfall_elasticsearch_bootstrap": False
    }
    template = cast("dict[str, str]", remove["ansible.builtin.template"])
    install = _task("Install the managed Elasticsearch configuration")
    assert template == install["ansible.builtin.template"]
    # Elasticsearch reads the setting only at start: no restart for it.
    assert "notify" not in remove


# proves: S-012-4
def test_s012_4_only_a_package_or_configuration_change_restarts_the_node() -> None:
    notifying = sorted(
        str(task["name"]) for task in _tasks() if task.get("notify")
    )

    assert notifying == [
        "Install the declared Elasticsearch version",
        "Install the declared heap",
        "Install the managed Elasticsearch configuration",
        "Let Elasticsearch lock its heap in memory",
    ]
    reads = [
        task
        for task in _tasks()
        if _module(task) == "ansible.builtin.uri"
        and "method" not in cast("dict[str, object]", task[_module(task)])
    ]
    assert len(reads) == 2
    assert all(task["no_log"] is True for task in reads)


# proves: S-012-4
def test_s012_4_the_cloudfall_line_is_written_only_for_a_new_password() -> None:
    """The staged hash has a random salt, so the role must not rewrite it each run."""
    line = _task("Put the cloudfall account in the file realm")
    password = _task("Install the cloudfall account password")
    read = _task("Read whether the file realm has the cloudfall account")
    condition = str(line["when"])

    def writes(*, password_changed: bool, rc: int) -> bool:
        variables = _variables(
            cloudfall_elasticsearch_password_file={"changed": password_changed},
            cloudfall_elasticsearch_realm_account={"rc": rc},
        )
        return bool(
            _templar(variables).evaluate_conditional(trust_as_template(condition))
        )

    assert password["register"] == "cloudfall_elasticsearch_password_file"
    assert read["register"] == "cloudfall_elasticsearch_realm_account"
    assert read["changed_when"] is False
    tasks = [task["name"] for task in _tasks()]
    assert tasks.index(str(password["name"])) < tasks.index(str(read["name"]))
    assert tasks.index(str(read["name"])) < tasks.index(str(line["name"]))
    # A second converge: same password file, the line already there.
    assert not writes(password_changed=False, rc=0)
    assert writes(password_changed=True, rc=0)
    assert writes(password_changed=False, rc=1)
    assert writes(password_changed=False, rc=2)


# proves: S-012-4
def test_s012_4_the_elastic_password_is_set_once() -> None:
    block = _task("Set the elastic password once")
    tasks = cast("list[dict[str, object]]", block["block"])
    set_password = next(
        task
        for task in tasks
        if task["name"] == "Set the elastic password from its hash"
    )
    condition = str(set_password["when"])
    scoped = {
        **_variables(),
        **cast("dict[str, object]", block["vars"]),
    }

    marked = {**scoped, "cloudfall_elasticsearch_elastic_set": _stat(exists=True)}
    unmarked = {**scoped, "cloudfall_elasticsearch_elastic_set": _stat(exists=False)}
    assert not _templar(marked).evaluate_conditional(trust_as_template(condition))
    assert _templar(unmarked).evaluate_conditional(trust_as_template(condition))
    uri = cast("dict[str, object]", set_password["ansible.builtin.uri"])
    assert uri["body"] == {
        "password_hash": (
            "{{ lookup('ansible.builtin.file',\n"
            "          cloudfall_elasticsearch_account_files.elasticHash) }}"
        )
    }
    assert set_password["no_log"] is True


# proves: S-012-3
def test_s012_3_the_package_is_the_declared_version_from_its_major_and_held() -> None:
    variables = _variables()
    install = _task("Install the declared Elasticsearch version")
    hold = _task("Hold the Elasticsearch package at the declared version")
    repository = _task("Configure the Elastic package repository of the declared major")

    apt = cast("dict[str, object]", install["ansible.builtin.apt"])
    assert _value(str(apt["name"]), variables) == "elasticsearch=9.2.4"
    assert apt["allow_change_held_packages"] is True
    assert hold["ansible.builtin.dpkg_selections"] == {
        "name": "elasticsearch",
        "selection": "hold",
    }
    uris = cast("dict[str, list[str]]", repository["ansible.builtin.deb822_repository"])
    assert _value(uris["uris"][0], variables) == (
        "https://artifacts.elastic.co/packages/9.x/apt"
    )


# proves: S-012-3
def test_s012_3_the_heap_and_the_memory_lock_files() -> None:
    variables = _variables()

    assert _render("heap.options.j2", variables).splitlines()[1:] == [
        "-Xms4096m",
        "-Xmx4096m",
    ]
    assert "LimitMEMLOCK=infinity" in _render("memlock.conf.j2", variables).splitlines()
    locked = _task("Refuse a node that runs without its memory locked")

    def nodes(*, mlockall: bool) -> dict[str, object]:
        answer = {"nodes": {"Qx9": {"process": {"mlockall": mlockall}}}}
        return _variables(cloudfall_elasticsearch_process={"json": answer})

    assert _holds(locked, nodes(mlockall=True))
    assert not _holds(locked, nodes(mlockall=False))


def test_a_declared_installed_version_newer_or_of_another_major_is_refused() -> None:
    refuse = _task("Refuse a version the installed node can't move to")

    def installed(stdout: str, rc: int = 0) -> dict[str, object]:
        return _variables(
            cloudfall_elasticsearch_installed={"rc": rc, "stdout": stdout}
        )

    assert _holds(refuse, installed("", rc=1))
    assert _holds(refuse, installed("installed 9.2.3"))
    assert _holds(refuse, installed("installed 9.2.4"))
    assert not _holds(refuse, installed("installed 9.3.0"))
    assert not _holds(refuse, installed("installed 8.19.6"))


def test_this_release_refuses_what_later_slices_converge() -> None:
    validate = _task("Validate Elasticsearch service target and declaration")
    user = {
        "name": "crm",
        "access": "write",
        "indexPatterns": ["crm-*"],
        "passwordRef": {
            "environment": "production",
            "path": "/services/search-main",
            "key": "CRM_PASSWORD",
        },
    }

    assert _holds(validate, _variables())
    for service in (
        _service(nodes=["h1", "h2"]),
        _service(users=[user]),
        _service(remoteAccess={"listenAddress": "10.0.0.2", "allowedSources": []}),
        _service(existingCluster={"seedHosts": ["10.0.0.11:9300"]}),
    ):
        assert not _holds(validate, _variables(cloudfall_elasticsearch_service=service))
    bookworm = _variables()
    facts = cast("dict[str, object]", bookworm["ansible_facts"])
    bookworm["ansible_facts"] = {**facts, "distribution_major_version": "12"}
    assert not _holds(validate, bookworm)


def test_every_task_that_sees_a_password_or_its_hash_is_no_log() -> None:
    tasks = _tasks()
    block = cast(
        "list[dict[str, object]]", _task("Set the elastic password once")["block"]
    )
    seeing = [
        task
        for task in (*tasks, *block)
        if _module(task) != "block"
        and "cloudfall_elasticsearch_account_files." in str(task)
    ]

    assert [task["name"] for task in seeing] == [
        "Install the cloudfall account password",
        "Put the cloudfall account in the file realm",
        "Wait for the node to serve the cluster",
        "Read whether the node locked its memory",
        "Set the elastic password from its hash",
    ]
    assert all(task.get("no_log") is True for task in seeing)


FAKE_SOPS = """\
#!{python}
import pathlib, sys
assert sys.argv[1] == "--decrypt"
sys.stdout.write(pathlib.Path(sys.argv[2]).read_text())
"""
CLOUDFALL_PASSWORD = "synthetic-cloudfall-value"  # noqa: S105 - grepped for
ELASTIC_PASSWORD = "synthetic-elastic-value"  # noqa: S105 - grepped for


def _engine_run(
    tmp_path: Path, secrets: str | None
) -> subprocess.CompletedProcess[str]:
    """Run ``cloudfall-engine playbook run services`` on the S-012-1 inventory.

    The host is reached locally and a tag no task carries keeps Ansible from
    changing anything; ``--secrets-dir`` holds plain files for a fake sops.
    """
    project = write_project(tmp_path / "project", elasticsearch_service())
    rendered = cast(
        "dict[str, Any]",
        as_ansible_data(
            render_ansible_inventory(
                PlatformInventory.from_state(validate_config(project, SCHEMAS))
            )
        ),
    )
    rendered["all"]["children"]["cloudfall_servers"]["hosts"]["h1"].update(
        {
            "ansible_connection": "local",
            "ansible_become": False,
            "ansible_host": "localhost",
            "ansible_python_interpreter": sys.executable,
        }
    )
    inventory = tmp_path / "ansible-inventory.json"
    inventory.write_text(json.dumps(rendered), encoding="utf-8")
    directory = tmp_path / "secrets"
    if secrets is not None:
        source = directory / "production" / "services" / "search-main.env"
        source.parent.mkdir(parents=True)
        source.write_text(secrets, encoding="utf-8")
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "sops").write_text(FAKE_SOPS.format(python=sys.executable), "utf-8")
    (tools / "sops").chmod(0o700)
    return subprocess.run(  # noqa: S603 - fixed interpreter and arguments.
        [
            sys.executable,
            "-m",
            "cloudfall_engine",
            "playbook",
            "run",
            "services",
            "--engine",
            str(ROOT / "engine"),
            "--inventory",
            str(inventory),
            "--secrets-dir",
            str(directory),
            "--tags",
            "s012-no-task",
            "-vvv",
        ],
        env={**os.environ, "PATH": os.pathsep.join((str(tools), os.environ["PATH"]))},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


# proves: S-012-3
def test_s012_3_the_engine_stages_the_account_files_and_removes_them(
    tmp_path: Path,
) -> None:
    done = _engine_run(
        tmp_path,
        f"CLOUDFALL_PASSWORD={CLOUDFALL_PASSWORD}\nELASTIC_PASSWORD={ELASTIC_PASSWORD}\n",
    )

    assert done.returncode == 0, done.stdout + done.stderr
    staged = re.findall(
        r"cloudfall-passwords-[^/\"']+/elasticsearch/search-main-[a-z]+\.[a-z]+",
        done.stderr,
    )
    assert {path.rsplit("/", 1)[1] for path in staged} == {
        "search-main-cloudfall.password",
        "search-main-cloudfall.bcrypt",
        "search-main-elastic.bcrypt",
    }
    assert not any(Path(tempfile.gettempdir()).glob(staged[0].split("/")[0]))
    for secret in (CLOUDFALL_PASSWORD, ELASTIC_PASSWORD):
        assert secret not in done.stdout + done.stderr
    assert json.loads(done.stdout)["data"]["owned_roles"] == []


def test_the_engine_stops_before_ansible_without_the_account_passwords(
    tmp_path: Path,
) -> None:
    done = _engine_run(tmp_path, f"CLOUDFALL_PASSWORD={CLOUDFALL_PASSWORD}\n")

    payload = json.loads(done.stdout)
    assert payload["error"]["code"] == "PRECONDITION", done.stdout
    assert "ELASTIC_PASSWORD is missing from" in payload["error"]["message"]
    assert "`cloudfall secrets generate`" in payload["error"]["message"]
    assert "PLAY [" not in done.stderr


def test_services_runs_the_role_for_each_elasticsearch_service() -> None:
    (play,) = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))
    task = next(
        task
        for task in play["tasks"]
        if task["name"] == "Deploy declared Elasticsearch services"
    )
    services = [
        _service(),
        {"id": "redis-cache", "serviceKind": "redis"},
    ]
    variables: dict[str, object] = {
        "cloudfall_services": services,
        "cloudfall_services_elasticsearch_files": {
            "search-main": {"cloudfallPassword": "/tmp/x"}  # noqa: S108
        },
    }

    assert task["ansible.builtin.include_role"] == {"name": "cloudfall_elasticsearch"}
    assert _value(task["loop"], variables) == [services[0]]
    files = _value(
        task["vars"]["cloudfall_elasticsearch_account_files"],
        {**variables, "cloudfall_service_item": services[0]},
    )
    assert files == {"cloudfallPassword": "/tmp/x"}  # noqa: S108
