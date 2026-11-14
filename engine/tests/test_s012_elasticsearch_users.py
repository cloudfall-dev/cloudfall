"""Declared Elasticsearch users, limited to their index patterns (S-012, #311).

The role's user tasks run for real with ansible-playbook on localhost against
a stand-in for Elasticsearch's security API, which records every request and
keeps the users and roles they leave. Searching as a user and getting 403
needs a real node: these tests prove the role and user each request puts,
and the selection of what a converge deletes and what audit lists.
"""

from __future__ import annotations

import base64
import hashlib
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
from cloudfall.ansible_data import as_ansible_data
from cloudfall.audit import AuditStatus, audit_inventory
from cloudfall.domain import ResourceId
from cloudfall.elasticsearch import ElasticsearchUserName
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.secrets import elasticsearch_user_bcrypt_hash
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from elasticsearch_project import (
    PASSWORD_REF,
    SCHEMAS,
    elasticsearch_service,
    write_project,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).parents[2]
ROLES = ROOT / "engine" / "ansible" / "roles"
ROLE = ROLES / "cloudfall_elasticsearch"
INSPECT = ROLES / "cloudfall_inspect"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"
CLOUDFALL_PASSWORD = "synthetic-cloudfall-value"  # noqa: S105 - grepped for
ELASTIC_PASSWORD = "synthetic-elastic-value"  # noqa: S105 - grepped for
CRM_PASSWORD = "synthetic-crm-value"  # noqa: S105 - grepped for
BILLING_PASSWORD = "synthetic-billing-value"  # noqa: S105 - grepped for
SERVICE = "search-main"
READ = ["read", "view_index_metadata"]
WRITE = ["read", "view_index_metadata", "write", "create_index", "manage"]
RESERVED_USERS = {
    "elastic": {
        "username": "elastic",
        "roles": ["superuser"],
        "full_name": None,
        "email": None,
        "metadata": {"_reserved": True},
        "enabled": True,
    },
    "kibana_system": {
        "username": "kibana_system",
        "roles": ["kibana_system"],
        "full_name": None,
        "email": None,
        "metadata": {"_reserved": True},
        "enabled": True,
    },
}
RESERVED_ROLES = {
    "superuser": {
        "cluster": ["all"],
        "indices": [{"names": ["*"], "privileges": ["all"]}],
        "applications": [],
        "run_as": ["*"],
        "metadata": {"_reserved": True},
        "transient_metadata": {},
    },
}


def _user(name: str, access: str, *patterns: str, key: str) -> dict[str, object]:
    return {
        "name": name,
        "access": access,
        "indexPatterns": list(patterns),
        "passwordRef": {**PASSWORD_REF, "key": key},
    }


CRM = _user("crm", "read", "crm-*", key="CRM_PASSWORD")
BILLING = _user("billing", "write", "billing-*", "crm-2", key="BILLING_PASSWORD")


@dataclass
class FakeCluster:
    """Elasticsearch's security API as the role's tasks use it, in memory."""

    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)
    requests: list[tuple[str, str, object]] = field(default_factory=list)
    port: int = 0

    def handle(self, method: str, path: str, body: object) -> tuple[int, object]:
        """Answer one authenticated request, as Elasticsearch would."""
        self.requests.append((method, path, body))
        match = re.fullmatch(r"/_security/(user|role)(?:/([a-z][a-z0-9_-]*))?", path)
        if match is None:
            return 404, {"error": path}
        kind, name = match.groups()
        store = self.users if kind == "user" else self.roles
        if method == "GET" and name is None:
            return 200, store
        assert name is not None
        if method == "DELETE":
            found = store.pop(name, None) is not None
            self.hashes.pop(name, None) if kind == "user" else None
            return (200 if found else 404), {"found": found}
        assert method == "PUT"
        assert isinstance(body, dict)
        created = name not in store
        if kind == "role":
            store[name] = {
                "cluster": body["cluster"],
                "indices": [
                    {**index, "allow_restricted_indices": False}
                    for index in body["indices"]
                ],
                "applications": [],
                "run_as": [],
                "metadata": body["metadata"],
                "transient_metadata": {"enabled": True},
            }
            return 200, {"role": {"created": created}}
        self.hashes[name] = body["password_hash"]
        store[name] = {
            "username": name,
            "roles": body["roles"],
            "full_name": None,
            "email": None,
            "metadata": body["metadata"],
            "enabled": body["enabled"],
        }
        return 200, {"created": created}

    def writes(self) -> list[tuple[str, str, object]]:
        """Return the requests that change the cluster."""
        return [request for request in self.requests if request[0] != "GET"]


def _handler(cluster: FakeCluster) -> type[BaseHTTPRequestHandler]:
    expected = "Basic " + base64.b64encode(
        f"cloudfall:{CLOUDFALL_PASSWORD}".encode()
    ).decode("ascii")

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method: str) -> None:
            if self.headers.get("Authorization") != expected:
                self._send(401, {"error": "unauthenticated"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            status, answer = cluster.handle(
                method, self.path, json.loads(raw) if raw else None
            )
            self._send(status, answer)

        def _send(self, status: int, answer: object) -> None:
            content = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            self._answer("GET")

        def do_PUT(self) -> None:
            self._answer("PUT")

        def do_DELETE(self) -> None:
            self._answer("DELETE")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    return Handler


@pytest.fixture
def cluster() -> Iterator[FakeCluster]:
    """Serve a stand-in cluster with its built-in users and a user made by hand."""
    fake = FakeCluster(
        users={
            **json.loads(json.dumps(RESERVED_USERS)),
            "analyst": {
                "username": "analyst",
                "roles": ["analyst"],
                "full_name": "Made by hand",
                "email": None,
                "metadata": {},
                "enabled": True,
            },
        },
        roles={
            **json.loads(json.dumps(RESERVED_ROLES)),
            "analyst": {
                "cluster": [],
                "indices": [{"names": ["logs-*"], "privileges": ["read"]}],
                "applications": [],
                "run_as": [],
                "metadata": {},
                "transient_metadata": {"enabled": True},
            },
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    fake.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


def _service(port: int, *users: dict[str, object]) -> dict[str, object]:
    return {
        "id": SERVICE,
        "serviceKind": "elasticsearch",
        "environment": "production",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": port},
        "elasticsearch": {
            "version": "9.2.4",
            "heapMb": 4096,
            "nodes": ["h1"],
            "clusterName": SERVICE,
            "users": list(users),
        },
        "backup": {
            "directory": f"/var/backups/cloudfall/{SERVICE}",
            "onCalendar": "*-*-* 03:20:00 UTC",
            "retentionDays": 14,
        },
    }


def _hash(user: str, password: str) -> str:
    return elasticsearch_user_bcrypt_hash(
        password,
        service=ResourceId(SERVICE),
        user=ElasticsearchUserName(user),
        salt_key=CLOUDFALL_PASSWORD,
    )


def _staged(directory: Path, passwords: dict[str, str]) -> dict[str, object]:
    """Stage the files as the controller does: the users' hashes only."""
    directory.mkdir(exist_ok=True)
    plain = directory / "cloudfall.password"
    plain.write_text(CLOUDFALL_PASSWORD, encoding="utf-8")
    users: dict[str, str] = {}
    for name, password in passwords.items():
        path = directory / f"user-{name}.bcrypt"
        path.write_text(_hash(name, password), encoding="utf-8")
        users[name] = str(path)
    return {"cloudfallPassword": str(plain), "users": users}


@dataclass(frozen=True)
class Converged:
    """How one run of the user tasks ended."""

    returncode: int
    output: str

    @property
    def changed(self) -> int:
        """Return the play recap's changed count."""
        found = re.search(r"localhost\s+: ok=\d+\s+changed=(\d+)", self.output)
        assert found is not None, self.output
        return int(found.group(1))


def _converge(
    tmp_path: Path,
    cluster: FakeCluster,
    passwords: dict[str, str],
    *users: dict[str, object],
) -> Converged:
    """Run the role's user tasks on localhost against the stand-in cluster."""
    play = [
        {
            "name": "Converge the declared Elasticsearch users",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {
                "cloudfall_server_id": "h1",
                "ansible_python_interpreter": sys.executable,
            },
            "tasks": [
                {
                    "name": "Run the user tasks of the Elasticsearch role",
                    "ansible.builtin.include_role": {
                        "name": "cloudfall_elasticsearch",
                        "tasks_from": "users",
                    },
                    "vars": {
                        "cloudfall_elasticsearch_service": _service(
                            cluster.port, *users
                        ),
                        "cloudfall_elasticsearch_account_files": _staged(
                            tmp_path / "staged", passwords
                        ),
                    },
                }
            ],
        }
    ]
    playbook = tmp_path / "users.yml"
    playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
    binary = shutil.which("ansible-playbook")
    assert binary is not None
    done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [binary, "--inventory", "localhost,", "-vvv", str(playbook)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "ANSIBLE_ROLES_PATH": str(ROLES)},
    )
    return Converged(done.returncode, done.stdout + done.stderr)


def _role(patterns: list[str], privileges: list[str]) -> dict[str, object]:
    return {
        "cluster": [],
        "indices": [{"names": patterns, "privileges": privileges}],
        "metadata": {"cloudfall_managed_by": SERVICE},
    }


PASSWORDS = {"crm": CRM_PASSWORD, "billing": BILLING_PASSWORD}


# proves: S-012-7
def test_s012_7_each_user_gets_a_role_limited_to_its_patterns_and_access(
    tmp_path: Path, cluster: FakeCluster
) -> None:
    done = _converge(tmp_path, cluster, PASSWORDS, CRM, BILLING)

    assert done.returncode == 0, done.output
    puts = {path: body for method, path, body in cluster.writes() if method == "PUT"}
    # read: search and see the mappings of crm-*, nothing else; write adds
    # indexing into, creating and managing its own patterns.
    assert puts["/_security/role/crm"] == _role(["crm-*"], READ)
    assert puts["/_security/role/billing"] == _role(["billing-*", "crm-2"], WRITE)
    for name in ("crm", "billing"):
        body = cast("dict[str, object]", puts[f"/_security/user/{name}"])
        # The user holds its own role alone: no other index is reachable.
        assert body["roles"] == [name]
        assert body["enabled"] is True
    # A role is put before the user that holds it.
    paths = [path for _, path, _ in cluster.writes()]
    assert paths.index("/_security/role/crm") < paths.index("/_security/user/crm")


# proves: S-012-8
def test_s012_8_a_user_reaches_the_cluster_as_its_bcrypt_hash_only(
    tmp_path: Path, cluster: FakeCluster
) -> None:
    done = _converge(tmp_path, cluster, PASSWORDS, CRM, BILLING)

    assert done.returncode == 0, done.output
    for name, password in PASSWORDS.items():
        staged = (tmp_path / "staged" / f"user-{name}.bcrypt").read_text("utf-8")
        assert staged.startswith("$2a$10$")
        assert password not in staged
        assert cluster.hashes[name] == staged
        metadata = cluster.users[name]["metadata"]
        assert metadata == {
            "cloudfall_managed_by": SERVICE,
            "cloudfall_password_digest": hashlib.sha256(staged.encode()).hexdigest(),
        }
    sent = json.dumps(cluster.requests)
    # -vvv shows every task's arguments that are not no_log.
    for secret in (CLOUDFALL_PASSWORD, CRM_PASSWORD, BILLING_PASSWORD):
        assert secret not in done.output
        assert secret not in sent
    for staged_hash in cluster.hashes.values():
        assert staged_hash not in done.output


# proves: S-012-4
def test_a_second_converge_of_unchanged_users_changes_nothing(
    tmp_path: Path, cluster: FakeCluster
) -> None:
    first = _converge(tmp_path, cluster, PASSWORDS, CRM, BILLING)
    writes = len(cluster.writes())
    second = _converge(tmp_path, cluster, PASSWORDS, CRM, BILLING)

    assert first.returncode == second.returncode == 0, second.output
    # Two roles and two users the first time (the recap counts the two
    # looped tasks), nothing the second.
    assert writes == 4
    assert first.changed == 2
    assert second.changed == 0
    assert len(cluster.writes()) == writes


def test_a_new_password_puts_the_user_again_and_nothing_else(
    tmp_path: Path, cluster: FakeCluster
) -> None:
    _converge(tmp_path, cluster, PASSWORDS, CRM, BILLING)
    before = len(cluster.writes())

    rotated = {**PASSWORDS, "crm": "synthetic-crm-rotated"}
    done = _converge(tmp_path, cluster, rotated, CRM, BILLING)

    assert done.returncode == 0, done.output
    assert [path for _, path, _ in cluster.writes()[before:]] == [
        "/_security/user/crm"
    ]
    assert cluster.hashes["crm"] == _hash("crm", "synthetic-crm-rotated")


def test_a_changed_pattern_or_access_puts_the_role_again(
    tmp_path: Path, cluster: FakeCluster
) -> None:
    _converge(tmp_path, cluster, PASSWORDS, CRM, BILLING)
    before = len(cluster.writes())

    widened = _user("crm", "write", "crm-*", key="CRM_PASSWORD")
    done = _converge(tmp_path, cluster, PASSWORDS, widened, BILLING)

    assert done.returncode == 0, done.output
    assert cluster.writes()[before:] == [
        ("PUT", "/_security/role/crm", _role(["crm-*"], WRITE))
    ]


# proves: S-012-9
def test_s012_9_a_removed_user_and_its_role_leave_and_one_made_by_hand_stays(
    tmp_path: Path, cluster: FakeCluster
) -> None:
    _converge(tmp_path, cluster, PASSWORDS, CRM, BILLING)
    before = len(cluster.writes())

    done = _converge(tmp_path, cluster, {"crm": CRM_PASSWORD}, CRM)

    assert done.returncode == 0, done.output
    assert cluster.writes()[before:] == [
        ("DELETE", "/_security/user/billing", None),
        ("DELETE", "/_security/role/billing", None),
    ]
    assert set(cluster.users) == {"elastic", "kibana_system", "analyst", "crm"}
    assert set(cluster.roles) == {"superuser", "analyst", "crm"}
    assert cluster.users["analyst"]["full_name"] == "Made by hand"


# proves: S-012-9
def test_s012_9_no_declared_user_leaves_only_what_cloudfall_made(
    tmp_path: Path, cluster: FakeCluster
) -> None:
    """A user named like a declared one but unmarked is never deleted."""
    cluster.users["billing"] = {**cluster.users["analyst"], "username": "billing"}
    _converge(tmp_path, cluster, {"crm": CRM_PASSWORD}, CRM)
    before = len(cluster.writes())

    done = _converge(tmp_path, cluster, {})

    assert done.returncode == 0, done.output
    assert cluster.writes()[before:] == [
        ("DELETE", "/_security/user/crm", None),
        ("DELETE", "/_security/role/crm", None),
    ]
    assert set(cluster.users) == {"elastic", "kibana_system", "analyst", "billing"}


def test_a_declared_name_held_by_a_user_not_made_by_cloudfall_is_refused(
    tmp_path: Path, cluster: FakeCluster
) -> None:
    analyst = _user("analyst", "read", "logs-*", key="ANALYST_PASSWORD")

    done = _converge(tmp_path, cluster, {"analyst": "synthetic-analyst"}, analyst)

    assert done.returncode != 0
    assert "declares analyst, which a user or role not made by Cloudfall" in (
        done.output
    )
    assert cluster.writes() == []


def test_every_user_task_that_sees_a_password_or_its_hash_is_no_log() -> None:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "users.yml"))
    assert isinstance(tasks, list)
    seeing = [
        task for task in tasks if "cloudfall_elasticsearch_account_files" in str(task)
    ]

    assert [task["name"] for task in seeing] == [
        "Read the users of the cluster",
        "Read the roles of the cluster",
        "Put the role of each declared user",
        "Put each declared user with its password hash",
        "Delete the users Cloudfall made that are no longer declared",
        "Delete the roles Cloudfall made that are no longer declared",
    ]
    assert all(task.get("no_log") is True for task in seeing)


def test_the_role_converges_users_on_the_home_server_only_and_not_in_check_mode() -> (
    None
):
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "main.yml"))
    assert isinstance(tasks, list)
    include = next(
        task
        for task in tasks
        if task.get("ansible.builtin.include_tasks") == "users.yml"
    )
    names = [task["name"] for task in tasks]

    assert include["when"] == [
        "not ansible_check_mode",
        "cloudfall_elasticsearch_service.server == cloudfall_server_id",
    ]
    # After the node serves the cluster and elastic has its password.
    assert names.index(include["name"]) > names.index("Set the elastic password once")
    staged = next(
        task
        for task in tasks
        if task["name"] == "Require the staged account files of the service"
    )
    variables: dict[str, object] = {
        "cloudfall_elasticsearch_service": _service(9200, CRM),
        "cloudfall_elasticsearch_account_files": {
            "cloudfallPassword": "/tmp/p",  # noqa: S108
            "cloudfallHash": "/tmp/h",  # noqa: S108
            "elasticHash": "/tmp/e",  # noqa: S108
        },
    }
    templar = Templar(loader=DataLoader(), variables=variables)

    def holds() -> bool:
        return all(
            templar.evaluate_conditional(trust_as_template(str(condition)))
            for condition in staged["ansible.builtin.assert"]["that"]
        )

    assert not holds()
    variables["cloudfall_elasticsearch_account_files"] = {
        **cast("dict[str, object]", variables["cloudfall_elasticsearch_account_files"]),
        "users": {"crm": "/tmp/crm"},  # noqa: S108
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    assert holds()


def _evidence(answers: list[dict[str, Any]], *, available: bool = True) -> object:
    """Evaluate the inspect role's evidence of one service on API answers."""
    tasks = DataLoader().load_from_file(
        str(INSPECT / "tasks" / "elasticsearch_users.yml")
    )
    assert isinstance(tasks, list)
    attach = next(
        task for task in tasks if task["name"] == "Attach the cluster's users and roles"
    )
    task_vars = {
        name: trust_as_template(value) if isinstance(value, str) else value
        for name, value in cast("dict[str, object]", attach["vars"]).items()
    }
    results = [
        {"status": 200, "json": answer} if available else {"status": -1}
        for answer in answers
    ]
    variables = {
        **task_vars,
        "cloudfall_inspect_elasticsearch_service": _service(9200),
        "cloudfall_inspect_elasticsearch_security": {"results": results},
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(
        trust_as_template("{{ cloudfall_inspect_elasticsearch_evidence }}")
    )


# proves: S-012-9
def test_s012_9_audit_lists_a_user_made_by_hand_as_unmanaged(
    tmp_path: Path, cluster: FakeCluster
) -> None:
    """Converge, inspect what the cluster holds, then audit it."""
    _converge(tmp_path, cluster, {"crm": CRM_PASSWORD}, CRM)
    evidence = _evidence([cluster.users, cluster.roles])

    assert evidence == {
        "service": SERVICE,
        "available": True,
        "users": {"managed": ["crm"], "unmanaged": ["analyst"]},
        "roles": {"managed": ["crm"], "unmanaged": ["analyst"]},
    }
    check = _audit(tmp_path, evidence, CRM)
    assert check["status"] == AuditStatus.COMPLIANT.value
    assert check["observed"] == {
        "users": ["crm"],
        "roles": ["crm"],
        "unmanagedUsers": ["analyst"],
        "unmanagedRoles": ["analyst"],
    }
    assert "unmanaged (not made by Cloudfall, left as they are): users analyst" in str(
        check["message"]
    )
    # A declared user the cluster lacks is drift; so is a marked one left over.
    assert _audit(tmp_path, evidence, CRM, BILLING)["status"] == "drift"
    assert _audit(tmp_path, evidence)["status"] == "drift"
    # A cluster that did not answer is unknown, never compliant.
    silent = _evidence([cluster.users, cluster.roles], available=False)
    assert _audit(tmp_path, silent, CRM)["status"] == "unknown"


def _audit(
    tmp_path: Path, evidence: object, *users: dict[str, object]
) -> dict[str, object]:
    project = write_project(
        tmp_path / f"project-{len(list(tmp_path.glob('project-*')))}",
        elasticsearch_service(users=list(users))
        if users
        else elasticsearch_service(),
    )
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    observations = project / "observed"
    observations.mkdir()
    document = json.loads((COMPLIANT / "h1.json").read_text(encoding="utf-8"))
    document["spec"]["elasticsearchUsers"] = [evidence]
    (observations / "h1.json").write_text(json.dumps(document), encoding="utf-8")
    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))
    (server,) = (item for item in report.servers if item.server_id == "h1")
    return next(
        check.as_dict()
        for check in server.checks
        if check.check == f"elasticsearch.users[{SERVICE}]"
    )


def test_inspect_reads_the_users_of_home_clusters_only() -> None:
    (play,) = yaml.safe_load(
        (ROOT / "engine" / "ansible" / "playbooks" / "inspect.yml").read_text("utf-8")
    )
    expression = play["roles"][0]["cloudfall_inspect_elasticsearch_services"]
    home = _service(9200)
    elsewhere = {**_service(9201), "id": "search-other", "server": "h2"}
    variables = {
        "cloudfall_server_id": "h1",
        "cloudfall_services": [
            home,
            elsewhere,
            {"serviceKind": "redis", "server": "h1"},
        ],
    }

    templar = Templar(loader=DataLoader(), variables=variables)
    selected = templar.template(trust_as_template(expression))

    assert [service["id"] for service in selected] == [SERVICE]


FAKE_SOPS = """\
#!{python}
import pathlib, sys
assert sys.argv[1] == "--decrypt"
sys.stdout.write(pathlib.Path(sys.argv[2]).read_text())
"""


# proves: S-012-8
def test_s012_8_no_password_in_the_inventory_the_engine_output_or_its_answer(
    tmp_path: Path,
) -> None:
    """``cloudfall-engine playbook run services`` stages users' hashes only.

    A tag no task carries keeps Ansible from changing anything; -vvv prints
    the extra vars, so the staged paths and every argument show.
    """
    project = write_project(tmp_path / "project", elasticsearch_service(users=[CRM]))
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
    source = tmp_path / "secrets" / "production" / "services" / "search-main.env"
    source.parent.mkdir(parents=True)
    source.write_text(
        f"CLOUDFALL_PASSWORD={CLOUDFALL_PASSWORD}\n"
        f"ELASTIC_PASSWORD={ELASTIC_PASSWORD}\n"
        f"CRM_PASSWORD={CRM_PASSWORD}\n",
        encoding="utf-8",
    )
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "sops").write_text(FAKE_SOPS.format(python=sys.executable), "utf-8")
    (tools / "sops").chmod(0o700)

    done = subprocess.run(  # noqa: S603 - fixed interpreter and arguments.
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
            str(tmp_path / "secrets"),
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

    assert done.returncode == 0, done.stdout + done.stderr
    assert re.search(
        r"cloudfall-passwords-[^/\"']+/elasticsearch/search-main-user-crm\.bcrypt",
        done.stderr,
    )
    text = inventory.read_text(encoding="utf-8")
    for secret in (CLOUDFALL_PASSWORD, ELASTIC_PASSWORD, CRM_PASSWORD):
        assert secret not in text
        assert secret not in done.stdout
        assert secret not in done.stderr
    assert json.loads(done.stdout)["data"]["owned_roles"] == []
