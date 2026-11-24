"""Kibana follows a change of its cluster's version, after every node (S-024-9, #579).

The Kibana role's ``version.yml`` runs for real with ansible-playbook on
localhost against a stand-in node that answers ``GET /_nodes`` for a cluster of
two; the order of the role's tasks and of ``services.yml``'s plays is read from
the files, and the role's conditions are evaluated with Ansible's own
templating. Upgrading the package on a Debian 13 host, Kibana's saved-object
migration and the dashboard opening afterwards need a live host and are listed
in the pull request as unverified.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

ROOT = Path(__file__).parents[2]
ROLES = ROOT / "engine" / "ansible" / "roles"
ROLE = ROLES / "cloudfall_kibana"
SERVICES = ROOT / "engine" / "ansible" / "playbooks" / "services.yml"
CLOUDFALL_PASSWORD = "synthetic-cloudfall-value"  # noqa: S105 - grepped for
RECAP = re.compile(r"localhost\s+:\s+ok=\d+\s+changed=(\d+)")
DECLARED = "9.2.5"
CHECK = "Stop before Kibana's package changes while a node of its cluster is older"
INSTALL = "Install the Elasticsearch service's version of Kibana"


def _filters() -> ModuleType:
    path = ROLE / "filter_plugins" / "kibana_nodes.py"
    spec = importlib.util.spec_from_file_location("kibana_nodes", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FILTERS = _filters()


def _answer(**versions: str) -> dict[str, object]:
    """Return GET /_nodes as a cluster answers it, filtered to name and version."""
    return {
        "nodes": {
            f"id-{name}": {"name": name, "version": version}
            for name, version in versions.items()
        }
    }


def _search_main(port: int = 9200, version: str = DECLARED) -> dict[str, object]:
    return {
        "id": "search-main",
        "serviceKind": "elasticsearch",
        "environment": "production",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": port},
        "elasticsearch": {
            "version": version,
            "heapMb": 2048,
            "nodes": ["h1", "h2"],
            "clusterName": "search-main",
            "users": [],
        },
        "backup": {
            "directory": "/var/backups/cloudfall/search-main",
            "onCalendar": "*-*-* 03:20:00 UTC",
            "retentionDays": 14,
        },
    }


def _kibana() -> dict[str, object]:
    return {
        "id": "search-kibana",
        "serviceKind": "kibana",
        "environment": "production",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": 5601},
        "kibana": {"elasticsearch": "search-main", "heapMb": 1024, "users": []},
    }


def _tasks(name: str) -> list[dict[str, Any]]:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / name))
    assert isinstance(tasks, list)
    return cast("list[dict[str, Any]]", tasks)


def _task(name: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return next(task for task in tasks if task["name"] == name)


def _defaults() -> dict[str, object]:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    assert isinstance(defaults, dict)
    return cast("dict[str, object]", defaults)


def _conditions(task: dict[str, Any], **variables: object) -> bool:
    """Evaluate a task's ``when`` with the role's defaults, as Ansible does."""
    defaults = {
        key: trust_as_template(value) if isinstance(value, str) else value
        for key, value in _defaults().items()
    }
    templar = Templar(loader=DataLoader(), variables={**defaults, **variables})
    return all(
        templar.evaluate_conditional(trust_as_template(str(condition)))
        for condition in task["when"]
    )


# proves: S-024-9
@pytest.mark.parametrize(
    ("versions", "older"),
    [
        pytest.param({"h1": "9.2.5", "h2": "9.2.5"}, [], id="every node moved"),
        pytest.param({"h1": "9.2.5", "h2": "9.2.4"}, ["h2 9.2.4"], id="one old"),
        pytest.param(
            {"h1": "9.2.4", "h2": "9.1.9"}, ["h1 9.2.4", "h2 9.1.9"], id="both old"
        ),
        # A node ahead of the declared version does not hold Kibana back
        pytest.param({"h1": "9.2.10", "h2": "9.3.0"}, [], id="newer"),
    ],
)
def test_s024_9_the_nodes_older_than_the_declared_version_are_named(
    versions: dict[str, str], older: list[str]
) -> None:
    assert FILTERS.older_nodes(_answer(**versions), DECLARED) == older


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"nodes": {}}, id="no node"),
        pytest.param({}, id="no nodes key"),
        pytest.param(_answer(h1="9.2"), id="short version"),
        pytest.param(_answer(h1="9.2.5-SNAPSHOT"), id="suffixed version"),
        pytest.param({"nodes": {"a": {"version": "9.2.5"}}}, id="no name"),
        pytest.param({"nodes": {"a": {"name": "h1"}}}, id="no version"),
    ],
)
def test_a_cluster_answer_the_role_cannot_read_is_refused(answer: object) -> None:
    with pytest.raises(ValueError, match=r"GET /_nodes|not a version"):
        FILTERS.older_nodes(answer, DECLARED)


# proves: S-024-9
def test_s024_9_every_node_is_read_before_the_package_changes() -> None:
    main = _tasks("main.yml")
    names = [task["name"] for task in main]
    check = _task(CHECK, main)

    assert check["ansible.builtin.include_tasks"] == "version.yml"
    assert names.index(CHECK) < names.index(INSTALL)
    # Every task before the check only reads or refuses
    assert names.index(CHECK) + 1 == names.index(INSTALL)
    cluster = _search_main()

    def reads(installed: dict[str, object], *, check_mode: bool = False) -> bool:
        return _conditions(
            check,
            ansible_check_mode=check_mode,
            cloudfall_kibana_installed=installed,
            cloudfall_kibana_elasticsearch_service=cluster,
        )

    # A new declared version, and a first install, read the nodes first
    assert reads({"rc": 0, "stdout": "installed 9.2.4"})
    assert reads({"rc": 1, "stdout": ""})
    # Kibana at the declared version: nothing more is read (S-024-4)
    assert not reads({"rc": 0, "stdout": f"installed {DECLARED}"})
    # A dry run converges no node, so it reads none
    assert not reads({"rc": 0, "stdout": "installed 9.2.4"}, check_mode=True)


# proves: S-024-9
def test_s024_9_the_held_package_moves_and_kibana_waits_for_its_migration() -> None:
    main = _tasks("main.yml")
    names = [task["name"] for task in main]
    install = _task(INSTALL, main)["ansible.builtin.apt"]

    # Held at the old version, the pinned install still moves it, and the
    # hold comes back at the new one
    assert install["allow_change_held_packages"] is True
    assert _task(INSTALL, main)["notify"] == "Restart Kibana"
    hold = _task("Hold the Kibana package at the service's version", main)
    assert hold["ansible.builtin.dpkg_selections"]["selection"] == "hold"
    # The restart is flushed before the wait, and the version is checked
    # once Kibana, its saved objects migrated, reports itself available
    order = [
        INSTALL,
        "Hold the Kibana package at the service's version",
        "Apply the managed Kibana configuration",
        "Wait until Kibana reports itself available",
        "Refuse a Kibana that reports another version than the service's",
    ]
    assert [names.index(name) for name in order] == sorted(
        names.index(name) for name in order
    )


# proves: S-024-9
def test_s024_9_kibana_converges_after_every_node_and_reports_its_refusal() -> None:
    nodes, kibana = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))

    # The nodes' play, every server's, comes first; Kibana's play after it
    assert any(
        task.get("ansible.builtin.include_role", {}).get("name")
        == "cloudfall_elasticsearch"
        for task in nodes["tasks"]
    )
    (block,) = kibana["tasks"]
    _, deploy = block["block"]
    assert deploy["ansible.builtin.include_role"]["name"] == "cloudfall_kibana"
    # The refusal reaches the directory the services converge reads
    report = deploy["vars"]["cloudfall_kibana_report_directory"]
    templar = Templar(
        loader=DataLoader(),
        variables={"cloudfall_services_mariadb_report_directory": "/tmp/reports"},  # noqa: S108
    )
    assert templar.template(trust_as_template(report)) == "/tmp/reports"  # noqa: S108
    assert (
        Templar(loader=DataLoader(), variables={}).template(trust_as_template(report))
        == ""
    )


def test_a_newer_or_another_major_kibana_is_refused_before_anything_changes() -> None:
    refuse = _task(
        "Refuse a Kibana version the converge can't move to the service's",
        _tasks("main.yml"),
    )

    def holds(installed: str) -> bool:
        defaults = {
            key: trust_as_template(value) if isinstance(value, str) else value
            for key, value in _defaults().items()
        }
        templar = Templar(
            loader=DataLoader(),
            variables={
                **defaults,
                "cloudfall_kibana_installed": {
                    "rc": 0,
                    "stdout": f"installed {installed}",
                },
                "cloudfall_kibana_elasticsearch_service": _search_main(),
            },
        )
        return all(
            templar.evaluate_conditional(trust_as_template(str(condition)))
            for condition in refuse["ansible.builtin.assert"]["that"]
        )

    assert holds("9.2.4")
    assert holds("9.1.0")
    assert holds(DECLARED)
    assert not holds("9.2.6")
    assert not holds("8.19.4")


@dataclass
class FakeCluster:
    """A node that answers GET /_nodes for its cluster, and records each request."""

    versions: dict[str, str]
    port: int = 0
    requests: list[tuple[str, str]] = field(default_factory=list)


def _handler(cluster: FakeCluster) -> type[BaseHTTPRequestHandler]:
    expected = "Basic " + base64.b64encode(
        f"cloudfall:{CLOUDFALL_PASSWORD}".encode()
    ).decode("ascii")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            cluster.requests.append(("GET", self.path))
            if self.headers.get("Authorization") != expected:
                self._send(401, {"error": "unauthenticated"})
                return
            if self.path != "/_nodes?filter_path=nodes.*.name,nodes.*.version":
                self._send(404, {"error": self.path})
                return
            self._send(200, _answer(**cluster.versions))

        def _send(self, status: int, answer: object) -> None:
            content = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    return Handler


@pytest.fixture
def cluster() -> Iterator[FakeCluster]:
    """Serve a stand-in node of a cluster of two."""
    fake = FakeCluster(versions={"h1": DECLARED, "h2": DECLARED})
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    fake.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


@dataclass(frozen=True)
class Converged:
    """How one run of the version check ended."""

    returncode: int
    output: str

    @property
    def changed(self) -> int:
        """Return the play recap's changed count."""
        found = RECAP.search(self.output)
        assert found is not None, self.output
        return int(found.group(1))


def _check(tmp_path: Path, cluster: FakeCluster, reports: Path) -> Converged:
    """Run the role's version check as the Kibana play does, Kibana at 9.2.4."""
    password = tmp_path / "cloudfall.password"
    password.write_text(CLOUDFALL_PASSWORD, encoding="utf-8")
    play = [
        {
            "name": "Read the nodes before Kibana's package changes",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {
                "cloudfall_server_id": "h1",
                "ansible_python_interpreter": sys.executable,
            },
            "tasks": [
                {
                    "name": "Run the version check of the Kibana role",
                    "ansible.builtin.include_role": {
                        "name": "cloudfall_kibana",
                        "tasks_from": "version",
                    },
                    "vars": {
                        "cloudfall_kibana_service": _kibana(),
                        "cloudfall_kibana_elasticsearch_service": _search_main(
                            cluster.port
                        ),
                        "cloudfall_kibana_elasticsearch_files": {
                            "cloudfallPassword": str(password)
                        },
                        "cloudfall_kibana_installed": {
                            "rc": 0,
                            "stdout": "installed 9.2.4",
                        },
                        "cloudfall_kibana_report_directory": str(reports),
                    },
                },
            ],
        }
    ]
    playbook = tmp_path / "version.yml"
    playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
    binary = shutil.which("ansible-playbook")
    assert binary is not None
    done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [binary, "--inventory", "localhost,", "-vvv", "--diff", str(playbook)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "ANSIBLE_ROLES_PATH": str(ROLES)},
    )
    return Converged(done.returncode, done.stdout + done.stderr)


requires_ansible = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook is not on PATH"
)


# proves: S-024-9
@requires_ansible
def test_s024_9_a_node_still_on_the_old_version_stops_with_precondition(
    tmp_path: Path, cluster: FakeCluster
) -> None:
    cluster.versions["h2"] = "9.2.4"
    reports = tmp_path / "reports"
    reports.mkdir()

    done = _check(tmp_path, cluster, reports)

    assert done.returncode != 0, done.output
    assert "PRECONDITION service_kibana_node_older" in done.output
    report = json.loads((reports / "search-kibana.json").read_text(encoding="utf-8"))
    assert report["outcome"] == "refused"
    assert report["code"] == "service_kibana_node_older"
    assert (report["service"], report["server"]) == ("search-kibana", "h1")
    assert "h2 9.2.4" in report["message"]
    assert "left at 9.2.4" in report["message"]
    assert report["message"].endswith(
        "9.2.5: h2 9.2.4. Kibana never runs ahead of a node; converge every node "
        "to that version, then run the command again"
    )
    # The cluster was only read, once, as the cloudfall account
    assert cluster.requests == [
        ("GET", "/_nodes?filter_path=nodes.*.name,nodes.*.version")
    ]
    assert CLOUDFALL_PASSWORD not in done.output


# proves: S-024-9
@requires_ansible
def test_s024_9_every_node_on_the_new_version_lets_kibana_move(
    tmp_path: Path, cluster: FakeCluster
) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()

    done = _check(tmp_path, cluster, reports)

    assert done.returncode == 0, done.output
    # Only a read: the check itself changes nothing
    assert done.changed == 0
    assert list(reports.iterdir()) == []
    assert CLOUDFALL_PASSWORD not in done.output


# proves: S-024-5
def test_s024_5_the_node_read_is_no_log() -> None:
    (read,) = (task for task in _tasks("version.yml") if "ansible.builtin.uri" in task)

    assert read["no_log"] is True
    assert read["ansible.builtin.uri"].get("method", "GET") == "GET"
