"""Who may work in Kibana: the view and edit roles and the users holding them.

S-024, People and what they may do (#572). The Kibana role's ``roles.yml``
and the Elasticsearch role's ``users.yml`` run for real with ansible-playbook
on localhost against one stand-in that answers Elasticsearch's security API
and Kibana's role API, records every request, and keeps what they leave.
Logging in, building a data view and a dashboard, and being refused a save
need a live Kibana: these tests prove the roles and users each request puts,
which is what decides them.
"""

from __future__ import annotations

import base64
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
from cloudfall.domain import ResourceId
from cloudfall.elasticsearch import ElasticsearchUserName
from cloudfall.kibana import EDIT_ROLE, VIEW_ROLE
from cloudfall.secrets import elasticsearch_user_bcrypt_hash

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).parents[2]
ROLES = ROOT / "engine" / "ansible" / "roles"
KIBANA_ROLE = ROLES / "cloudfall_kibana"
SERVICES = ROOT / "engine" / "ansible" / "playbooks" / "services.yml"
CLOUDFALL_PASSWORD = "synthetic-cloudfall-value"  # noqa: S105 - grepped for
PASSWORDS = {
    "analyst": "synthetic-analyst-value",
    "support": "synthetic-support-value",
    "crm": "synthetic-crm-value",
}
SERVICE = "search-main"
KIBANA = "search-kibana"
READ = ["read", "view_index_metadata"]
KIBANA_APPLICATION = "kibana-.kibana"
RECAP = re.compile(r"localhost\s+:\s+ok=\d+\s+changed=(\d+)")
ROLE_NAME = r"[a-z][a-z0-9_-]*"


def _user(name: str, *patterns: str) -> dict[str, object]:
    return {
        "name": name,
        "access": "read",
        "indexPatterns": list(patterns),
        "passwordRef": {
            "environment": "production",
            "path": f"/services/{SERVICE}",
            "key": f"{name.upper()}_PASSWORD",
        },
    }


# analyst reads crm-* only: billing-1 is in no pattern of its own role
ANALYST = _user("analyst", "crm-*")
SUPPORT = _user("support", "crm-*")
CRM = _user("crm", "crm-*")
USERS = (ANALYST, SUPPORT, CRM)
ACCESS = {"analyst": "edit", "support": "view"}


@dataclass
class FakeStack:
    """Elasticsearch's security API and Kibana's role API, in memory.

    A role put through Kibana is kept the way Kibana keeps it: as a role of
    the cluster holding privileges of Kibana's application.
    """

    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)
    requests: list[tuple[str, str, object]] = field(default_factory=list)
    port: int = 0

    def handle(
        self, method: str, path: str, body: object, *, xsrf: bool
    ) -> tuple[int, object]:
        """Answer one authenticated request, as the node or Kibana would."""
        self.requests.append((method, path, body))
        kibana = re.fullmatch(rf"/api/security/role/({ROLE_NAME})", path)
        if kibana is not None:
            return self._kibana_role(method, kibana.group(1), body, xsrf=xsrf)
        match = re.fullmatch(rf"/_security/(user|role)(?:/({ROLE_NAME}))?", path)
        if match is None:
            return 404, {"error": path}
        kind, name = match.groups()
        store = self.users if kind == "user" else self.roles
        if method == "GET" and name is None:
            return 200, store
        assert name is not None
        if method == "DELETE":
            found = store.pop(name, None) is not None
            return (200 if found else 404), {"found": found}
        assert method == "PUT"
        assert isinstance(body, dict)
        created = name not in store
        if kind == "role":
            store[name] = {
                "cluster": body["cluster"],
                "indices": body["indices"],
                "applications": body.get("applications", []),
                "run_as": [],
                "metadata": body["metadata"],
                "transient_metadata": {"enabled": True},
            }
            return 200, {"role": {"created": created}}
        if "password_hash" in body:
            self.hashes[name] = body["password_hash"]
        else:
            # Elasticsearch keeps the password of a user put without one
            assert not created, "a new user needs a password"
        store[name] = {
            "username": name,
            "roles": body["roles"],
            "full_name": None,
            "email": None,
            "metadata": body["metadata"],
            "enabled": body["enabled"],
        }
        return 200, {"created": created}

    def _kibana_role(
        self, method: str, name: str, body: object, *, xsrf: bool
    ) -> tuple[int, object]:
        if method == "GET":
            role = self.roles.get(name)
            if role is None:
                return 404, {"statusCode": 404, "error": "Not Found"}
            return 200, {
                "name": name,
                "metadata": role["metadata"],
                "transient_metadata": role["transient_metadata"],
                "elasticsearch": {
                    "cluster": role["cluster"],
                    "indices": role["indices"],
                    "run_as": role["run_as"],
                },
                "kibana": [
                    {
                        "base": application["privileges"],
                        "feature": {},
                        "spaces": ["*"],
                    }
                    for application in role["applications"]
                ],
                "_transform_error": [],
                "_unrecognized_applications": [],
            }
        assert method == "PUT"
        if not xsrf:
            return 400, {"message": "Request must contain a kbn-xsrf header."}
        assert isinstance(body, dict)
        (privileges,) = body["kibana"]
        assert privileges["spaces"] == ["*"]
        self.roles[name] = {
            "cluster": body["elasticsearch"]["cluster"],
            "indices": body["elasticsearch"]["indices"],
            "applications": [
                {
                    "application": KIBANA_APPLICATION,
                    "privileges": privileges["base"],
                    "resources": ["*"],
                }
            ],
            "run_as": body["elasticsearch"]["run_as"],
            "metadata": body["metadata"],
            "transient_metadata": {"enabled": True},
        }
        return 204, None

    def writes(self) -> list[tuple[str, str, object]]:
        """Return the requests that change the cluster or Kibana."""
        return [request for request in self.requests if request[0] != "GET"]


def _handler(stack: FakeStack) -> type[BaseHTTPRequestHandler]:
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
            status, answer = stack.handle(
                method,
                self.path,
                json.loads(raw) if raw else None,
                xsrf=self.headers.get("kbn-xsrf") is not None,
            )
            self._send(status, answer)

        def _send(self, status: int, answer: object) -> None:
            self.send_response(status)
            if answer is None:
                self.end_headers()
                return
            content = json.dumps(answer).encode()
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
def stack() -> Iterator[FakeStack]:
    """Serve a stand-in node and Kibana with the cluster's built-in superuser."""
    fake = FakeStack(
        roles={
            "superuser": {
                "cluster": ["all"],
                "indices": [{"names": ["*"], "privileges": ["all"]}],
                "applications": [
                    {"application": "*", "privileges": ["*"], "resources": ["*"]}
                ],
                "run_as": ["*"],
                "metadata": {"_reserved": True},
                "transient_metadata": {},
            }
        }
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


def _hash(user: str, password: str) -> str:
    return elasticsearch_user_bcrypt_hash(
        password,
        service=ResourceId(SERVICE),
        user=ElasticsearchUserName(user),
        salt_key=CLOUDFALL_PASSWORD,
    )


def _staged(
    directory: Path,
    users: tuple[dict[str, object], ...],
    passwords: dict[str, str],
) -> dict[str, object]:
    """Stage the files as the controller does: the users' hashes only."""
    directory.mkdir(exist_ok=True)
    plain = directory / "cloudfall.password"
    plain.write_text(CLOUDFALL_PASSWORD, encoding="utf-8")
    staged: dict[str, str] = {}
    for user in users:
        name = cast("str", user["name"])
        path = directory / f"user-{name}.bcrypt"
        path.write_text(_hash(name, passwords[name]), encoding="utf-8")
        staged[name] = str(path)
    return {"cloudfallPassword": str(plain), "users": staged}


def _search_main(port: int, users: tuple[dict[str, object], ...]) -> dict[str, object]:
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


def _search_kibana(port: int, access: dict[str, str]) -> dict[str, object]:
    return {
        "id": KIBANA,
        "serviceKind": "kibana",
        "environment": "production",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": port},
        "kibana": {
            "elasticsearch": SERVICE,
            "heapMb": 1024,
            "users": [
                {"name": name, "access": level} for name, level in access.items()
            ],
        },
    }


@dataclass(frozen=True)
class Converged:
    """How one run of the roles' tasks ended."""

    returncode: int
    output: str

    @property
    def changed(self) -> int:
        """Return the play recap's changed count."""
        found = RECAP.search(self.output)
        assert found is not None, self.output
        return int(found.group(1))


def _converge(
    tmp_path: Path,
    stack: FakeStack,
    access: dict[str, str],
    users: tuple[dict[str, object], ...] = USERS,
    passwords: dict[str, str] | None = None,
) -> Converged:
    """Converge the users from the home, then Kibana's roles, as services.yml does."""
    staged = _staged(tmp_path / "staged", users, passwords or PASSWORDS)
    play = [
        {
            "name": "Converge the users of the cluster and the roles of its Kibana",
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
                        "cloudfall_elasticsearch_service": _search_main(
                            stack.port, users
                        ),
                        "cloudfall_elasticsearch_account_files": staged,
                        "cloudfall_elasticsearch_kibana_access": access,
                    },
                },
                {
                    "name": "Run the role tasks of the Kibana role",
                    "ansible.builtin.include_role": {
                        "name": "cloudfall_kibana",
                        "tasks_from": "roles",
                    },
                    "vars": {
                        "cloudfall_kibana_service": _search_kibana(stack.port, access),
                        "cloudfall_kibana_elasticsearch_files": staged,
                    },
                },
            ],
        }
    ]
    playbook = tmp_path / "access.yml"
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


def _kibana_role(base: str) -> dict[str, object]:
    return {
        "metadata": {"cloudfall_managed_by": KIBANA},
        "elasticsearch": {"cluster": [], "indices": [], "run_as": []},
        "kibana": [{"base": [base], "feature": {}, "spaces": ["*"]}],
    }


requires_ansible = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook is not on PATH"
)


# proves: S-024-6
@requires_ansible
def test_s024_6_edit_and_view_roles_are_written_through_kibana_with_no_index(
    tmp_path: Path, stack: FakeStack
) -> None:
    done = _converge(tmp_path, stack, ACCESS)

    assert done.returncode == 0, done.output
    puts = {path: body for method, path, body in stack.writes() if method == "PUT"}
    # edit is Kibana's base privilege all, view its base read, on every
    # space; neither holds a privilege in Elasticsearch, so neither reaches
    # an index nor manages users and roles
    assert puts[f"/api/security/role/{EDIT_ROLE}"] == _kibana_role("all")
    assert puts[f"/api/security/role/{VIEW_ROLE}"] == _kibana_role("read")
    assert f"/_security/role/{EDIT_ROLE}" not in puts
    assert f"/_security/role/{VIEW_ROLE}" not in puts
    for role in (EDIT_ROLE, VIEW_ROLE):
        assert stack.roles[role]["cluster"] == []
        assert stack.roles[role]["indices"] == []
        assert stack.roles[role]["run_as"] == []


# proves: S-024-6
@requires_ansible
def test_s024_6_analyst_edits_and_support_views_and_each_reads_its_own_indices(
    tmp_path: Path, stack: FakeStack
) -> None:
    done = _converge(tmp_path, stack, ACCESS)

    assert done.returncode == 0, done.output
    # analyst builds data views and dashboards, support opens them and is
    # refused a save: the Kibana role of each access, next to its own role
    assert stack.users["analyst"]["roles"] == ["analyst", EDIT_ROLE]
    assert stack.users["support"]["roles"] == ["support", VIEW_ROLE]
    # What a dashboard shows each is what its own role reads: crm-*, so no
    # document of billing-1
    for name in ("analyst", "support"):
        own = stack.roles[name]
        assert own["indices"] == [{"names": ["crm-*"], "privileges": READ}]
        assert own["cluster"] == []
        assert own["applications"] == []
    held = [
        index
        for name in ("analyst", EDIT_ROLE)
        for index in stack.roles[name]["indices"]
    ]
    assert all("billing-1" not in index["names"] for index in held)
    assert all(
        not re.fullmatch(pattern.replace("*", ".*"), "billing-1")
        for index in held
        for pattern in index["names"]
    )


# proves: S-024-6
@requires_ansible
def test_s024_6_a_second_converge_puts_no_role_and_no_user(
    tmp_path: Path, stack: FakeStack
) -> None:
    first = _converge(tmp_path, stack, ACCESS)
    writes = len(stack.writes())

    second = _converge(tmp_path, stack, ACCESS)

    assert first.returncode == second.returncode == 0, second.output
    assert first.changed > 0
    assert second.changed == 0, second.output
    assert len(stack.writes()) == writes


# proves: S-024-7
@requires_ansible
def test_s024_7_a_user_kibana_users_does_not_name_holds_its_own_role_alone(
    tmp_path: Path, stack: FakeStack
) -> None:
    done = _converge(tmp_path, stack, ACCESS)

    assert done.returncode == 0, done.output
    # crm logs in and holds no privilege of Kibana's application: Kibana
    # opens no application for it
    assert stack.users["crm"]["roles"] == ["crm"]
    # The role of each declared user holds no application privilege, as
    # S-012 leaves it
    for user in USERS:
        role = stack.roles[cast("str", user["name"])]
        assert role["applications"] == []
    role_puts = [
        cast("dict[str, object]", body)
        for method, path, body in stack.writes()
        if method == "PUT" and path.startswith("/_security/role/")
    ]
    assert len(role_puts) == len(USERS)
    assert all("applications" not in body for body in role_puts)


# proves: S-024-7
@requires_ansible
def test_s024_7_users_not_named_keep_exactly_their_roles_when_access_is_given(
    tmp_path: Path, stack: FakeStack
) -> None:
    """A cluster S-012 converged before Kibana: only the named users change."""
    _converge(tmp_path, stack, {})
    assert all(user["roles"] == [name] for name, user in stack.users.items())
    before = len(stack.writes())
    hashes = dict(stack.hashes)

    done = _converge(tmp_path, stack, ACCESS)

    assert done.returncode == 0, done.output
    users = [
        (path, cast("dict[str, object]", body))
        for method, path, body in stack.writes()[before:]
        if path.startswith("/_security/")
    ]
    assert [path for path, _ in users] == [
        "/_security/user/analyst",
        "/_security/user/support",
    ]
    # Giving access sends no password: one the person changed stays
    assert all("password_hash" not in body for _, body in users)
    assert stack.hashes == hashes
    assert stack.users["crm"]["roles"] == ["crm"]


# proves: S-024-7
@requires_ansible
def test_s024_7_removing_analyst_takes_the_kibana_role_alone(
    tmp_path: Path, stack: FakeStack
) -> None:
    _converge(tmp_path, stack, ACCESS)
    own_role = dict(stack.roles["analyst"])
    hashes = dict(stack.hashes)
    before = len(stack.writes())

    done = _converge(tmp_path, stack, {"support": "view"})

    assert done.returncode == 0, done.output
    # One request: analyst put again with its own role alone, no password
    (write,) = stack.writes()[before:]
    method, path, body = write
    assert (method, path) == ("PUT", "/_security/user/analyst")
    assert cast("dict[str, object]", body)["roles"] == ["analyst"]
    assert "password_hash" not in cast("dict[str, object]", body)
    # analyst still logs in with its password and searches crm-1 through
    # Elasticsearch: the user, its own role and its password are as they were
    assert stack.users["analyst"]["enabled"] is True
    assert stack.roles["analyst"] == own_role
    assert stack.hashes == hashes
    assert stack.users["support"]["roles"] == ["support", VIEW_ROLE]
    # The two roles stay for the users that are still named
    assert {EDIT_ROLE, VIEW_ROLE} <= set(stack.roles)


@requires_ansible
def test_a_role_changed_by_hand_is_put_again(tmp_path: Path, stack: FakeStack) -> None:
    _converge(tmp_path, stack, ACCESS)
    stack.roles[VIEW_ROLE]["applications"][0]["privileges"] = ["all"]
    stack.roles[EDIT_ROLE]["indices"] = [{"names": ["*"], "privileges": ["read"]}]
    before = len(stack.writes())

    done = _converge(tmp_path, stack, ACCESS)

    assert done.returncode == 0, done.output
    assert stack.writes()[before:] == [
        ("PUT", f"/api/security/role/{VIEW_ROLE}", _kibana_role("read")),
        ("PUT", f"/api/security/role/{EDIT_ROLE}", _kibana_role("all")),
    ]


@requires_ansible
def test_a_new_password_is_still_sent_with_kibana_access(
    tmp_path: Path, stack: FakeStack
) -> None:
    _converge(tmp_path, stack, ACCESS)
    before = len(stack.writes())
    rotated = {**PASSWORDS, "analyst": "synthetic-analyst-rotated"}

    done = _converge(tmp_path, stack, ACCESS, passwords=rotated)

    assert done.returncode == 0, done.output
    (write,) = stack.writes()[before:]
    body = cast("dict[str, object]", write[2])
    assert write[1] == "/_security/user/analyst"
    assert body["roles"] == ["analyst", EDIT_ROLE]
    assert stack.hashes["analyst"] == body["password_hash"]


@requires_ansible
def test_no_password_or_hash_reaches_the_output(
    tmp_path: Path, stack: FakeStack
) -> None:
    done = _converge(tmp_path, stack, ACCESS)

    assert done.returncode == 0, done.output
    # -vvv --diff shows every argument of a task that is not no_log
    for secret in (CLOUDFALL_PASSWORD, *PASSWORDS.values(), *stack.hashes.values()):
        assert secret not in done.output
    assert CLOUDFALL_PASSWORD not in json.dumps(stack.requests)


def test_every_role_task_that_sees_the_password_is_no_log() -> None:
    tasks = DataLoader().load_from_file(str(KIBANA_ROLE / "tasks" / "roles.yml"))
    assert isinstance(tasks, list)
    seeing = [task for task in tasks if "cloudfallPassword" in json.dumps(task)]

    assert [task["name"] for task in seeing] == [
        "Read the roles Kibana keeps",
        "Put each role Kibana keeps",
    ]
    assert all(task.get("no_log") is True for task in seeing)


def test_the_roles_are_kept_once_kibana_is_available_and_not_in_check_mode() -> None:
    tasks = DataLoader().load_from_file(str(KIBANA_ROLE / "tasks" / "main.yml"))
    assert isinstance(tasks, list)
    names = [task["name"] for task in tasks]
    include = next(
        task
        for task in tasks
        if task.get("ansible.builtin.include_tasks") == "roles.yml"
    )

    assert include["when"] == "not ansible_check_mode"
    assert names.index(include["name"]) > names.index(
        "Wait until Kibana reports itself available"
    )


def _kibana_access(hosts: dict[str, list[dict[str, object]]]) -> object:
    (first, _) = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))
    deploy = next(
        task
        for task in first["tasks"]
        if task.get("ansible.builtin.include_role", {}).get("name")
        == "cloudfall_elasticsearch"
    )
    expression = deploy["vars"]["cloudfall_elasticsearch_kibana_access"]
    variables: dict[str, object] = {
        "groups": {"cloudfall_servers": [*hosts, "bare"]},
        # A server with no service has no cloudfall_services at all
        "hostvars": {
            "bare": {},
            **{
                host: {"cloudfall_services": services}
                for host, services in hosts.items()
            },
        },
        "cloudfall_service_item": _search_main(9200, USERS),
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(expression))


# proves: S-024-7
def test_s024_7_services_yml_hands_the_home_the_access_its_kibana_names() -> None:
    search = _search_main(9200, USERS)
    kibana = {**_search_kibana(5601, ACCESS), "server": "h2"}
    other = {
        **_search_kibana(5601, {"crm": "edit"}),
        "id": "other-kibana",
        "server": "h3",
        "kibana": {
            "elasticsearch": "search-other",
            "heapMb": 1024,
            "users": [{"name": "crm", "access": "edit"}],
        },
    }

    # The home h1 converges the users; Kibana runs on the node h2
    assert _kibana_access({"h1": [search], "h2": [search, kibana], "h3": [other]}) == (
        ACCESS
    )
    # Without a Kibana for the cluster, no user gets a Kibana role
    assert _kibana_access({"h1": [search], "h3": [other]}) == {}
    assert _kibana_access({"h1": [search], "h4": []}) == {}
