"""The ``cloudfall_kibana`` role on a node of its cluster (S-024, #569).

The role needs a Debian 13 host with Kibana and Elasticsearch, so its
decisions are evaluated with Ansible's own templating, its ``kibana.yml``
rendered from its template, and its token and keystore tasks run for real on
localhost against stand-ins of ``kibana-keystore`` and
``elasticsearch-service-tokens`` that keep the keystore and the node's
``service_tokens`` file: a second converge must change nothing. What a live
host alone can prove (the package and its hold, the Node.js process's heap,
the listener Kibana opens, its status and login page, the journal) is listed
in the pull request as unverified.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from kibana_node import calls, write_tools

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from types import ModuleType

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_kibana"
SERVICES = ROOT / "engine" / "ansible" / "playbooks" / "services.yml"
RECAP = re.compile(r"localhost\s+:\s+ok=\d+\s+changed=(\d+)")
KEYS = {
    "securityKey": "synthetic-security-key-0123456789abcdef",
    "savedObjectsKey": "synthetic-saved-objects-key-0123456789",
    "reportingKey": "synthetic-reporting-key-0123456789abcd",
}
SETTINGS = {
    "securityKey": "xpack.security.encryptionKey",
    "savedObjectsKey": "xpack.encryptedSavedObjects.encryptionKey",
    "reportingKey": "xpack.reporting.encryptionKey",
}
CLOUDFALL_PASSWORD = "synthetic-cloudfall-value"  # noqa: S105 - grepped for
TOKEN_SETTING = "elasticsearch.serviceAccountToken"  # noqa: S105 - a setting name
# The tasks of main.yml that only read or refuse; every one before the first
# task that changes the host
REFUSALS = (
    "Validate Kibana service target and declaration",
    (
        "Refuse a Kibana heap that with the node's is above three quarters of the "
        "host's memory"
    ),
    "Refuse a Domain this server does not hold for Kibana",
    "Require the staged encryption keys and the cloudfall password",
    "Read the installed Kibana version",
    "Refuse a Kibana version the converge can't move to the service's",
    "Read the Elastic repository of the node",
    "Require the Elastic repository of the node",
    "Stop before Kibana's package changes while a node of its cluster is older",
)


def _search_main(**elasticsearch: object) -> dict[str, object]:
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
            "retentionDays": 14,
        },
    }


def _kibana(**kibana: object) -> dict[str, object]:
    return {
        "id": "search-kibana",
        "serviceKind": "kibana",
        "environment": "production",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": 5601},
        "kibana": {
            "elasticsearch": "search-main",
            "heapMb": 1024,
            "users": [{"name": "analyst", "access": "edit"}],
            **kibana,
        },
    }


def _filters() -> ModuleType:
    path = ROLE / "filter_plugins" / "kibana_listeners.py"
    spec = importlib.util.spec_from_file_location("kibana_listeners", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FILTERS = _filters()


def _tasks(name: str) -> list[dict[str, Any]]:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / name))
    assert isinstance(tasks, list)
    return cast("list[dict[str, Any]]", tasks)


def _task(name: str, tasks: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return next(task for task in tasks if task["name"] == name)


def _flat(tasks: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for task in tasks:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _flat(task.get(key, []))


def _defaults() -> dict[str, object]:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    assert isinstance(defaults, dict)
    return cast("dict[str, object]", defaults)


def _trusted(value: object) -> object:
    """Trust the role's own defaults as templates, as Ansible trusts a role's files."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return {key: _trusted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_trusted(item) for item in value]
    return value


def _variables(**extra: object) -> dict[str, object]:
    return {
        **{key: _trusted(value) for key, value in _defaults().items()},
        "ansible_check_mode": False,
        "omit": "__omit__",
        "cloudfall_server_id": "h1",
        "ansible_facts": {
            "distribution": "Debian",
            "distribution_major_version": "13",
            "service_mgr": "systemd",
            "memtotal_mb": 8192,
        },
        "cloudfall_kibana_service": _kibana(),
        "cloudfall_kibana_elasticsearch_service": _search_main(),
        "cloudfall_kibana_files": {
            name: f"/tmp/staged/search-kibana-{name}"  # noqa: S108
            for name in KEYS
        },
        "cloudfall_kibana_elasticsearch_files": {
            "cloudfallPassword": "/tmp/staged/search-main-cloudfall.password",  # noqa: S108
        },
        **extra,
    }


def _holds(task: dict[str, Any], variables: dict[str, object]) -> bool:
    """Evaluate an assert's conditions with its own task vars, as Ansible does."""
    that = cast("dict[str, list[str]]", task["ansible.builtin.assert"])["that"]
    own = cast("dict[str, object]", task.get("vars", {}))
    trusted = cast("dict[str, object]", _trusted(own))
    templar = Templar(loader=DataLoader(), variables={**variables, **trusted})
    return all(
        templar.evaluate_conditional(trust_as_template(str(condition)))
        for condition in that
    )


def _template(expression: str, variables: dict[str, object]) -> object:
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(expression))


def _kibana_yml(variables: dict[str, object]) -> tuple[str, dict[str, object]]:
    text = str(
        _template(
            (ROLE / "templates" / "kibana.yml.j2").read_text(encoding="utf-8"),
            variables,
        )
    )
    settings = yaml.safe_load(text)
    assert isinstance(settings, dict)
    return text, cast("dict[str, object]", settings)


# proves: S-024-3
def test_s024_3_kibana_is_the_clusters_version_and_held() -> None:
    variables = _variables()
    main = _tasks("main.yml")
    install = _task("Install the Elasticsearch service's version of Kibana", main)
    apt = install["ansible.builtin.apt"]
    # The version of search-main: Kibana has none of its own
    assert _template(apt["name"], variables) == "kibana=9.2.4"
    assert apt["allow_change_held_packages"] is True
    # The package does not start Kibana before its configuration is written
    assert apt["policy_rc_d"] == 101
    hold = _task("Hold the Kibana package at the service's version", main)
    assert hold["ansible.builtin.dpkg_selections"] == {
        "name": "kibana",
        "selection": "hold",
    }
    # From the node's repository, which the Elasticsearch role configured
    require = _task("Require the Elastic repository of the node", main)
    assert require["ansible.builtin.assert"]["that"] == [
        "cloudfall_kibana_repository.stat.exists"
    ]
    assert _defaults()["cloudfall_kibana_repository_path"] == (
        "/etc/apt/sources.list.d/elasticsearch.sources"
    )
    refuse = _task(
        "Refuse a Kibana version the converge can't move to the service's", main
    )
    installed = {"rc": 0, "stdout": "installed 9.2.4"}
    assert _holds(refuse, _variables(cloudfall_kibana_installed=installed))
    assert _holds(
        refuse, _variables(cloudfall_kibana_installed={"rc": 1, "stdout": ""})
    )
    # A newer Kibana is never moved back (S-024-9 moves an older one forward)
    newer = {"rc": 0, "stdout": "installed 9.2.5"}
    assert not _holds(refuse, _variables(cloudfall_kibana_installed=newer))


# proves: S-024-3
def test_s024_3_kibana_yml_answers_on_loopback_and_reads_the_node() -> None:
    _, settings = _kibana_yml(_variables())

    assert settings == {
        "server.host": "127.0.0.1",
        "server.port": 5601,
        "server.publicBaseUrl": "http://127.0.0.1:5601",
        "elasticsearch.hosts": ["http://127.0.0.1:9200"],
        "telemetry.optIn": False,
    }


def test_kibana_yml_verifies_the_nodes_https_with_remote_access() -> None:
    remote = _search_main(
        remoteAccess={"listenAddress": "10.0.0.2", "allowedSources": ["10.0.0.0/24"]}
    )

    _, settings = _kibana_yml(_variables(cloudfall_kibana_elasticsearch_service=remote))

    assert settings["elasticsearch.hosts"] == ["https://10.0.0.2:9200"]
    assert settings["elasticsearch.ssl.certificateAuthorities"] == [
        "/etc/kibana/elasticsearch-ca.crt"
    ]
    assert settings["elasticsearch.ssl.verificationMode"] == "full"
    copy = _task(
        "Give Kibana the project CA the node's HTTPS is verified against",
        _tasks("main.yml"),
    )["ansible.builtin.copy"]
    assert copy["src"] == "{{ cloudfall_kibana_elasticsearch_ca_path }}"
    assert copy["remote_src"] is True


# proves: S-024-3
def test_s024_3_the_nodejs_process_gets_the_declared_heap() -> None:
    heap = _task("Give Kibana's Node.js process the declared heap", _tasks("main.yml"))
    line = heap["ansible.builtin.lineinfile"]

    assert line["path"] == "{{ cloudfall_kibana_configuration_directory }}/node.options"
    assert _template(line["line"], _variables()) == "--max-old-space-size=1024"
    # The package ships the option commented out; the line replaces it
    pattern = re.compile(line["regexp"])
    assert pattern.match("#--max-old-space-size=4096")
    assert pattern.match("--max-old-space-size=2048")
    assert not pattern.match("--unhandled-rejections=warn")
    assert heap["notify"] == "Restart Kibana"


# proves: S-024-3
def test_s024_3_a_kibana_listener_outside_loopback_is_refused() -> None:
    sockets = "\n".join(  # noqa: FLY002 - one socket per line
        (
            "LISTEN 0 511 127.0.0.1:5601 0.0.0.0:*",
            "LISTEN 0 4096 127.0.0.1:9200 0.0.0.0:*",
            "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*",
            "LISTEN 0 511 [::1]:5601 [::]:*",
        )
    )
    assert FILTERS.public_listeners(sockets, 5601) == []
    # Port 15601 is not Kibana's; 0.0.0.0 and :: and * are every address
    for exposed in ("0.0.0.0:5601", "*:5601", "[::]:5601", "10.0.0.2:5601"):
        line = f"LISTEN 0 511 {exposed} 0.0.0.0:*"
        assert FILTERS.public_listeners(f"{sockets}\n{line}", 5601) == [exposed]
    assert FILTERS.public_listeners("LISTEN 0 1 0.0.0.0:15601 0.0.0.0:*", 5601) == []
    refuse = _task("Refuse a Kibana listener outside loopback", _tasks("main.yml"))
    assert refuse["ansible.builtin.assert"]["that"] == [
        "cloudfall_kibana_public | length == 0"
    ]


# proves: S-024-3
def test_s024_3_the_converge_waits_until_kibana_is_available_at_the_version() -> None:
    main = _tasks("main.yml")
    wait = _task("Wait until Kibana reports itself available", main)
    (read,) = wait["block"]
    until = read["until"]

    def available(answer: dict[str, object]) -> bool:
        templar = Templar(
            loader=DataLoader(), variables={"cloudfall_kibana_status": answer}
        )
        return bool(templar.evaluate_conditional(trust_as_template(until)))

    migrating = {"status": 503, "json": {"status": {"overall": {"level": "degraded"}}}}
    assert not available(migrating)
    body: dict[str, object] = {
        "version": {"number": "9.2.4"},
        "status": {"overall": {"level": "available"}},
    }
    ready: dict[str, object] = {"status": 200, "json": body}
    assert available(ready)
    assert read["no_log"] is True
    retries = str(_template(str(read["retries"]), _variables()))
    assert int(retries) * read["delay"] >= 300
    # When it never is, the converge stops naming the last level
    (stop,) = wait["rescue"]
    assert "ansible.builtin.fail" in stop
    version = _task(
        "Refuse a Kibana that reports another version than the service's", main
    )
    assert _holds(version, _variables(cloudfall_kibana_status=ready))
    older = {**ready, "json": {**body, "version": {"number": "9.2.3"}}}
    assert not _holds(version, _variables(cloudfall_kibana_status=older))


# proves: S-024-3
def test_s024_3_an_unauthenticated_dashboard_request_goes_to_the_login_form() -> None:
    main = _tasks("main.yml")
    read = _task("Read where an unauthenticated request for a dashboard is sent", main)
    uri = read["ansible.builtin.uri"]

    assert _template(uri["url"], _variables()) == "http://127.0.0.1:5601/app/dashboards"
    assert uri["follow_redirects"] == "none"
    assert uri["status_code"] == 302
    # No credentials: the request is the browser's of a person not logged in
    assert not {"url_username", "url_password", "headers"} & set(uri)
    refuse = _task("Refuse a dashboard that opens without a login", main)
    login = {"location": "http://127.0.0.1:5601/login?next=%2Fapp%2Fdashboards"}
    assert _holds(refuse, _variables(cloudfall_kibana_anonymous=login))
    elsewhere = {"location": "http://127.0.0.1:5601/app/home"}
    assert not _holds(refuse, _variables(cloudfall_kibana_anonymous=elsewhere))


# proves: S-024-4
@pytest.mark.parametrize(
    ("heap", "holds"),
    [
        # 8192 MB: three quarters are 6144, the node takes 4096
        pytest.param(2048, True, id="at three quarters"),
        pytest.param(2049, False, id="above three quarters"),
    ],
)
def test_s024_4_a_heap_above_three_quarters_with_the_nodes_is_refused(
    heap: int, *, holds: bool
) -> None:
    kibana = _kibana(heapMb=heap)
    refuse = _task(
        "Refuse a Kibana heap that with the node's is above three quarters of "
        "the host's memory",
        _tasks("main.yml"),
    )
    assert _holds(refuse, _variables(cloudfall_kibana_service=kibana)) is holds

    # services.yml refuses it in pre_tasks, before any service role runs
    play = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))[0]
    pre_task = _task(
        "Refuse a Kibana heap that with the node's is above three quarters of "
        "the server's memory",
        play["pre_tasks"],
    )
    services = [_search_main(), kibana, {**_kibana(), "server": "h2", "id": "other"}]
    variables = {
        **_variables(),
        "cloudfall_services": services,
        "cloudfall_service_item": kibana,
        **{name: trust_as_template(value) for name, value in pre_task["vars"].items()},
    }
    # Only the Kibana of this server
    assert _template(pre_task["loop"], variables) == [kibana]
    assert _holds(pre_task, variables) is holds
    names = [task["name"] for task in play["pre_tasks"]]
    assert names.index(pre_task["name"]) < names.index(
        "Refuse a service of a kind this playbook does not converge"
    )


# proves: S-024-4
def test_s024_4_every_refusal_comes_before_any_task_that_changes_the_host() -> None:
    main = _tasks("main.yml")
    names = [task["name"] for task in main]

    assert names[: len(REFUSALS)] == list(REFUSALS)
    for name in ("Read the installed Kibana version",):
        read = _task(name, main)
        assert read["changed_when"] is False
        assert read["check_mode"] is False
    assert (
        names[len(REFUSALS)] == "Install the Elasticsearch service's version of Kibana"
    )
    # A domain the server does not hold is refused before any change (#573)
    domain = _task("Refuse a Domain this server does not hold for Kibana", main)
    assert _holds(domain, _variables())
    published = _kibana(domain="search-kibana-site")
    assert not _holds(domain, _variables(cloudfall_kibana_service=published))


def test_a_target_that_is_no_node_of_its_cluster_is_refused() -> None:
    validate = _task(
        "Validate Kibana service target and declaration", _tasks("main.yml")
    )

    assert _holds(validate, _variables())
    elsewhere = _search_main(nodes=["h2"])
    assert not _holds(
        validate, _variables(cloudfall_kibana_elasticsearch_service=elsewhere)
    )
    assert not _holds(validate, _variables(cloudfall_kibana_elasticsearch_service={}))
    public = {**_kibana(), "bind": {"address": "10.0.0.2", "port": 5601}}
    assert not _holds(validate, _variables(cloudfall_kibana_service=public))
    ipv6 = {**_kibana(), "bind": {"address": "::1", "port": 5601}}
    assert _holds(validate, _variables(cloudfall_kibana_service=ipv6))
    assert (
        _template(
            "{{ cloudfall_kibana_url }}", _variables(cloudfall_kibana_service=ipv6)
        )
        == "http://[::1]:5601"
    )


def test_services_yml_runs_kibana_after_every_node_of_its_cluster() -> None:
    first, kibana_play = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))
    assert kibana_play["hosts"] == first["hosts"]
    assert kibana_play["serial"] == 1
    assert kibana_play["any_errors_fatal"] is True
    # No Kibana role in the play of the nodes
    assert all(
        task.get("ansible.builtin.include_role", {}).get("name") != "cloudfall_kibana"
        for task in first["tasks"]
    )
    (mark,) = first["post_tasks"]
    assert mark["ansible.builtin.set_fact"] == {"cloudfall_services_finished": True}
    (block,) = kibana_play["tasks"]
    stop, deploy = block["block"]
    assert deploy["ansible.builtin.include_role"]["name"] == "cloudfall_kibana"

    h1 = {"cloudfall_server_id": "h1", "inventory_hostname": "h1"}
    services = [_search_main(nodes=["h1", "h2", "h3"]), _kibana()]

    def unfinished(finished: set[str], play_hosts: list[str]) -> object:
        hostvars = {
            host: {
                "inventory_hostname": host,
                **({"cloudfall_services_finished": True} if host in finished else {}),
            }
            for host in ("h1", "h2", "h3")
        }
        variables: dict[str, object] = {
            **h1,
            "cloudfall_services": services,
            "hostvars": hostvars,
            "ansible_play_hosts_all": play_hosts,
            "cloudfall_kibana_here": trust_as_template(
                block["vars"]["cloudfall_kibana_here"]
            ),
        }
        return _template(stop["vars"]["cloudfall_kibana_unfinished"], variables)

    assert unfinished({"h1", "h2", "h3"}, ["h1", "h2", "h3"]) == []
    # A node the run could not reach stops Kibana
    assert unfinished({"h1", "h3"}, ["h1", "h2", "h3"]) == ["h2"]
    # --limit h1: the other nodes are not in this run
    assert unfinished({"h1"}, ["h1"]) == []
    # The role gets its cluster's service and staged files
    variables: dict[str, object] = {
        **h1,
        "cloudfall_services": services,
        "cloudfall_service_item": _kibana(),
        "cloudfall_services_kibana_files": {"search-kibana": {"securityKey": "/k"}},
        "cloudfall_services_elasticsearch_files": {
            "search-main": {"cloudfallPassword": "/p"}
        },
    }
    role_vars = deploy["vars"]
    cluster = _template(role_vars["cloudfall_kibana_elasticsearch_service"], variables)
    assert cluster == services[0]
    assert _template(role_vars["cloudfall_kibana_files"], variables) == {
        "securityKey": "/k"
    }
    assert _template(role_vars["cloudfall_kibana_elasticsearch_files"], variables) == {
        "cloudfallPassword": "/p"
    }


def _strip_ownership(tasks: list[dict[str, Any]]) -> None:
    """Drop owner and group, which only root may set, from file tasks."""
    for task in tasks:
        for module in ("ansible.builtin.file", "ansible.builtin.copy"):
            if module in task:
                task[module].pop("owner", None)
                task[module].pop("group", None)
        for key in ("block", "always", "rescue"):
            if key in task:
                _strip_ownership(task[key])


requires_ansible = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook is not on PATH"
)


class Node:
    """A replay of the role's token and keystore tasks against the stand-ins."""

    def __init__(self, tmp_path: Path) -> None:
        """Write the stand-ins, the controller's staged keys and the tasks."""
        self.tmp_path = tmp_path
        self.keystore_tool, self.tokens_tool, self.restart, self.log = write_tools(
            tmp_path / "tools"
        )
        self.kibana_conf = tmp_path / "etc-kibana"
        self.elasticsearch_conf = tmp_path / "etc-elasticsearch"
        self.host_state = tmp_path / "host-state"
        for directory in (self.kibana_conf, self.elasticsearch_conf):
            directory.mkdir()
        tasks = yaml.safe_load((ROLE / "tasks" / "keystore.yml").read_text("utf-8"))
        _strip_ownership(tasks)
        self.tasks = tmp_path / "keystore.yml"
        self.tasks.write_text(yaml.safe_dump(tasks), encoding="utf-8")
        self.controller = tmp_path / "controller"
        self.controller.mkdir()
        self.keys = dict(KEYS)

    def converge(self) -> tuple[subprocess.CompletedProcess[str], int]:
        """Run the tasks at -vvv with --diff; return the run and its change count."""
        staged: dict[str, str] = {}
        for name, value in self.keys.items():
            path = self.controller / f"search-kibana-{name}"
            path.write_text(value, encoding="utf-8")
            staged[name] = str(path)
        play = [
            {
                "name": "Replay the token and keystore tasks of cloudfall_kibana",
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "vars": {
                    **_defaults(),
                    "ansible_python_interpreter": sys.executable,
                    "cloudfall_server_id": "h1",
                    "cloudfall_kibana_service": _kibana(),
                    "cloudfall_kibana_elasticsearch_service": _search_main(),
                    "cloudfall_kibana_files": staged,
                    "cloudfall_kibana_configuration_directory": str(self.kibana_conf),
                    "cloudfall_kibana_elasticsearch_configuration_directory": str(
                        self.elasticsearch_conf
                    ),
                    "cloudfall_kibana_keystore_tool": str(self.keystore_tool),
                    "cloudfall_kibana_service_tokens_tool": str(self.tokens_tool),
                    "cloudfall_kibana_state_directory": str(self.host_state),
                },
                "tasks": [
                    {
                        "name": "Converge the token and the keystore",
                        "ansible.builtin.include_tasks": str(self.tasks),
                    }
                ],
                "handlers": [
                    {
                        "name": "Restart Kibana",
                        "ansible.builtin.command": {"argv": [str(self.restart)]},
                        "changed_when": True,
                    }
                ],
            }
        ]
        playbook = self.tmp_path / "keystore-play.yml"
        playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
        binary = shutil.which("ansible-playbook")
        assert binary is not None
        done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
            [binary, "-vvv", "--diff", "--inventory", "localhost,", str(playbook)],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            env=dict(os.environ),
        )
        recap = RECAP.search(done.stdout)
        assert recap is not None, done.stdout + done.stderr
        assert done.returncode == 0, done.stdout + done.stderr
        return done, int(recap[1])

    @property
    def keystore(self) -> dict[str, str]:
        """Return the settings Kibana's keystore holds now."""
        return cast(
            "dict[str, str]",
            json.loads((self.kibana_conf / "kibana.keystore").read_text("utf-8")),
        )

    @property
    def service_tokens(self) -> str:
        """Return the node's service_tokens file."""
        return (self.elasticsearch_conf / "service_tokens").read_text("utf-8")

    def writes(self) -> list[list[str]]:
        """Return the calls that change the node or Kibana, not the reads."""
        return [call for call in calls(self.log) if call[1:2] != ["list"]]


# proves: S-024-5
@requires_ansible
def test_s024_5_kibana_gets_a_token_of_elastic_kibana_from_the_nodes_file(
    tmp_path: Path,
) -> None:
    node = Node(tmp_path)

    done, changed = node.converge()

    assert changed > 0
    # One token of elastic/kibana, named after the Kibana service, in the
    # node's service_tokens file: only its hash is there
    (line,) = node.service_tokens.splitlines()
    assert line.startswith("elastic/kibana/search-kibana:{PBKDF2_STRETCH}")
    token = node.keystore[TOKEN_SETTING]
    assert token
    assert token not in node.service_tokens
    # The three keys of the store are in the keystore
    assert {SETTINGS[name]: value for name, value in KEYS.items()} == {
        setting: node.keystore[setting] for setting in SETTINGS.values()
    }
    # None crosses a command line, nor the playbook's output at -vvv --diff
    for secret in (token, *KEYS.values()):
        assert secret not in done.stdout
        assert secret not in done.stderr
        assert all(secret not in " ".join(call) for call in calls(node.log))
    assert ["restart"] in calls(node.log)


# proves: S-024-4
@requires_ansible
def test_s024_4_a_second_converge_changes_nothing_and_restarts_nothing(
    tmp_path: Path,
) -> None:
    node = Node(tmp_path)
    node.converge()
    keystore = node.keystore
    tokens = node.service_tokens
    writes = node.writes()

    done, changed = node.converge()

    assert changed == 0, done.stdout
    # The token is not made again, the keys are not put again, and no restart
    assert node.writes() == writes
    assert node.keystore == keystore
    assert node.service_tokens == tokens
    assert [call for call in writes if call == ["restart"]] == [["restart"]]


# proves: S-024-5
@requires_ansible
def test_s024_5_a_token_the_keystore_lost_is_made_again(tmp_path: Path) -> None:
    node = Node(tmp_path)
    node.converge()
    first = node.keystore[TOKEN_SETTING]
    keystore = node.keystore
    del keystore[TOKEN_SETTING]
    (node.kibana_conf / "kibana.keystore").write_text(
        json.dumps(keystore), encoding="utf-8"
    )
    before = len(node.writes())

    _, changed = node.converge()

    assert changed > 0
    tools = [call[:3] for call in node.writes()[before:]]
    assert tools == [
        ["elasticsearch-service-tokens", "delete", "elastic/kibana"],
        ["elasticsearch-service-tokens", "create", "elastic/kibana"],
        ["kibana-keystore", "add", "--stdin"],
        ["restart"],
    ]
    (line,) = node.service_tokens.splitlines()
    assert line.startswith("elastic/kibana/search-kibana:")
    assert node.keystore[TOKEN_SETTING] != first


@requires_ansible
def test_a_rotated_key_is_put_again_and_restarts_kibana(tmp_path: Path) -> None:
    node = Node(tmp_path)
    node.converge()
    before = len(node.writes())
    node.keys["reportingKey"] = "synthetic-rotated-reporting-key-01234567"

    _, changed = node.converge()

    assert changed > 0
    added = [
        call
        for call in node.writes()[before:]
        if call[:2] == ["kibana-keystore", "add"]
    ]
    assert [call[-1] for call in added] == list(SETTINGS.values())
    assert node.keystore["xpack.reporting.encryptionKey"] == node.keys["reportingKey"]
    assert node.writes()[-1] == ["restart"]


# proves: S-024-5
def test_s024_5_every_task_that_sees_a_secret_is_no_log() -> None:
    # A staged key or password, the token the tool printed, or their digest
    secret = re.compile(
        r"cloudfall_kibana_files|cloudfall_kibana_keys(?![_a-z])|"
        r"cloudfall_kibana_keys_digest|cloudfallPassword|cloudfall_kibana_token\.stdout"
    )
    seeing = [
        task
        for name in ("main.yml", "keystore.yml")
        for task in _flat(_tasks(name))
        if "ansible.builtin.assert" not in task
        and "block" not in task
        and secret.search(json.dumps(task))
    ]

    assert sorted(task["name"] for task in seeing) == sorted(
        [
            "Read Kibana's status until it reports itself available",
            "Read the file tokens of elastic/kibana the node holds",
            "Create Kibana's token in the node's service_tokens file",
            "Put the token in Kibana's keystore",
            "Put each encryption key in Kibana's keystore",
            "Record the digest of the encryption keys the keystore holds",
        ]
    )
    assert all(task.get("no_log") is True for task in seeing)


# proves: S-024-5
def test_s024_5_no_secret_is_in_kibana_yml_and_kibana_system_is_never_touched() -> None:
    text, settings = _kibana_yml(_variables())

    # The token and the keys are keystore settings, none in the file
    for setting in (TOKEN_SETTING, *SETTINGS.values()):
        assert setting not in settings
    assert "password" not in text.lower()
    assert "elasticsearch.username" not in settings
    # Nothing in the role, nor in the Kibana play, sets or changes a user of
    # the cluster: its only request to the node reads the elastic/kibana
    # service account's tokens
    files = [*ROLE.rglob("*.yml"), *ROLE.rglob("*.j2"), SERVICES]
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        code = [line for line in lines if not line.lstrip().startswith("#")]
        assert all("kibana_system" not in line for line in code), path
    requests = [
        task["ansible.builtin.uri"]
        for name in ("main.yml", "keystore.yml")
        for task in _flat(_tasks(name))
        if "ansible.builtin.uri" in task
    ]
    assert all(request.get("method", "GET") == "GET" for request in requests)
    node_requests = [
        request["url"] for request in requests if "elasticsearch_url" in request["url"]
    ]
    assert len(node_requests) == 1
    assert (
        node_requests[0]
        .rstrip()
        .endswith("/_security/service/elastic/kibana/credential")
    )
    check = _task(
        "Refuse a Kibana whose token the node does not hold", _tasks("main.yml")
    )
    held = {
        "json": {
            "nodes_credentials": {"file_tokens": {"search-kibana": {"nodes": ["h1"]}}}
        }
    }
    assert _holds(check, _variables(cloudfall_kibana_credentials=held))
    lost: dict[str, object] = {"json": {"nodes_credentials": {"file_tokens": {}}}}
    assert not _holds(check, _variables(cloudfall_kibana_credentials=lost))
