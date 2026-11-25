"""The Kibana evidence inspect collects for audit (S-024-13).

Inspect needs a host with Kibana and a node to answer, so the evidence it
attaches is evaluated with Ansible's own templating from what Kibana, the
node and ``kibana-keystore`` answer; the tasks that send the cloudfall
account's password are read for ``no_log``. A live inspect of a Kibana
server is listed in the pull request as unverified.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).parents[2]
INSPECT = ROOT / "engine" / "ansible" / "roles" / "cloudfall_inspect"
PLAYBOOK = ROOT / "engine" / "ansible" / "playbooks" / "inspect.yml"
VIEW = "cloudfall-kibana-view"
EDIT = "cloudfall-kibana-edit"
PASSWORD = "synthetic-cloudfall-value"  # noqa: S105 - grepped for
DIGEST = "synthetic-password-digest-77ab"
KIBANA_YML = 'server.host: "127.0.0.1"\nserver.port: 5601\n'
NODE_OPTIONS = "--max-old-space-size=1024\n"


def _yaml(path: Path) -> Any:  # noqa: ANN401 - a YAML document of any shape.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _trusted(value: object) -> object:
    """Trust the role's own files as templates, as Ansible trusts them."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return {key: _trusted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_trusted(item) for item in value]
    return value


def _tasks() -> list[dict[str, Any]]:
    tasks = DataLoader().load_from_file(str(INSPECT / "tasks" / "kibana.yml"))
    assert isinstance(tasks, list)
    return cast("list[dict[str, Any]]", tasks)


def _task(name: str) -> dict[str, Any]:
    return next(task for task in _tasks() if task["name"] == name)


def _kibana(**bind: object) -> dict[str, object]:
    return {
        "id": "search-kibana",
        "serviceKind": "kibana",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": 5601, **bind},
        "kibana": {"elasticsearch": "search-main", "heapMb": 1024, "users": []},
    }


def _search_main(**elasticsearch: object) -> dict[str, object]:
    return {
        "id": "search-main",
        "serviceKind": "elasticsearch",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": 9200},
        "elasticsearch": {"version": "9.2.4", "nodes": ["h1"], **elasticsearch},
    }


def _encoded(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _role_answer(name: str, base: str) -> dict[str, object]:
    return {
        "item": name,
        "status": 200,
        "json": {
            "name": name,
            "elasticsearch": {"cluster": [], "indices": [], "run_as": []},
            "kibana": [{"base": [base], "feature": {}, "spaces": ["*"]}],
            "metadata": {"cloudfall_managed_by": "search-kibana"},
            "transient_metadata": {"enabled": True},
        },
    }


def _user(*roles: str, reserved: bool = False) -> dict[str, object]:
    metadata: dict[str, object] = (
        {"_reserved": True}
        if reserved
        else {
            "cloudfall_managed_by": "search-main",
            "cloudfall_password_digest": DIGEST,
        }
    )
    return {"roles": list(roles), "enabled": True, "metadata": metadata}


ANSWERS: dict[str, object] = {
    "cloudfall_inspect_kibana_file": {"stat": {"isreg": True}},
    "cloudfall_inspect_kibana_content": {"content": _encoded(KIBANA_YML)},
    "cloudfall_inspect_kibana_options_file": {"stat": {"isreg": True}},
    "cloudfall_inspect_kibana_options": {"content": _encoded(NODE_OPTIONS)},
    "cloudfall_inspect_kibana_keystore": {
        "rc": 0,
        "stdout_lines": [
            "elasticsearch.serviceAccountToken",
            "xpack.security.encryptionKey",
        ],
    },
    "cloudfall_inspect_kibana_password_file": {"stat": {"exists": True}},
    "cloudfall_inspect_kibana_password": {"content": _encoded(PASSWORD)},
    "cloudfall_inspect_kibana_status": {
        "status": 200,
        "json": {"status": {"overall": {"level": "available"}}},
    },
    "cloudfall_inspect_kibana_roles": {
        "results": [_role_answer(VIEW, "read"), _role_answer(EDIT, "all")]
    },
    "cloudfall_inspect_kibana_users": {
        "status": 200,
        "json": {
            "analyst": _user("analyst", EDIT),
            "support": _user("support", VIEW, EDIT),
            "crm": _user("crm"),
            "elastic": _user("superuser", reserved=True),
        },
    },
}


def _variables(**answers: object) -> dict[str, object]:
    attach = _task("Attach Kibana evidence")
    return {
        **cast("dict[str, object]", _trusted(_yaml(INSPECT / "defaults" / "main.yml"))),
        **cast("dict[str, object]", _trusted(attach["vars"])),
        "omit": "__omit__",
        "cloudfall_inspect_kibana_service": _kibana(),
        "cloudfall_inspect_kibana_elasticsearch_services": [_search_main()],
        **ANSWERS,
        **answers,
    }


def _template(expression: str, variables: dict[str, object]) -> object:
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(expression))


def _evidence(**answers: object) -> dict[str, object]:
    evidence = _template(
        "{{ cloudfall_inspect_kibana_evidence }}", _variables(**answers)
    )
    assert isinstance(evidence, dict)
    return cast("dict[str, object]", evidence)


# proves: S-024-13
def test_s024_13_inspect_reads_kibanas_files_keystore_status_roles_and_users() -> None:
    evidence = _evidence()

    assert evidence == {
        "service": "search-kibana",
        "configurationPath": "/etc/kibana/kibana.yml",
        "configuration": KIBANA_YML,
        "nodeOptions": NODE_OPTIONS,
        "keystore": [
            "elasticsearch.serviceAccountToken",
            "xpack.security.encryptionKey",
        ],
        "status": {"read": True, "level": "available"},
        "roles": [
            {
                "name": VIEW,
                "found": True,
                "elasticsearch": {"cluster": [], "indices": [], "run_as": []},
                "kibana": [{"base": ["read"], "feature": {}, "spaces": ["*"]}],
                "metadata": {"cloudfall_managed_by": "search-kibana"},
            },
            {
                "name": EDIT,
                "found": True,
                "elasticsearch": {"cluster": [], "indices": [], "run_as": []},
                "kibana": [{"base": ["all"], "feature": {}, "spaces": ["*"]}],
                "metadata": {"cloudfall_managed_by": "search-kibana"},
            },
        ],
        # support was given the edit role by hand; elastic is built in
        "userRoles": {"analyst": [EDIT], "support": [VIEW, EDIT], "crm": []},
    }


# proves: S-024-5
def test_s024_5_inspect_keeps_no_password_or_digest_in_the_evidence() -> None:
    evidence = json.dumps(_evidence())

    assert PASSWORD not in evidence
    assert DIGEST not in evidence
    # Every task that sends the cloudfall password, or reads it, logs nothing
    for task in _tasks():
        module = next(
            (
                value
                for key, value in task.items()
                if key in {"ansible.builtin.uri", "ansible.builtin.slurp"}
            ),
            None,
        )
        if module is None:
            continue
        sends = "url_password" in module
        reads = "elasticsearch_password_path" in str(module.get("src", ""))
        if sends or reads:
            assert task.get("no_log") is True, task["name"]


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        pytest.param(
            {
                "cloudfall_inspect_kibana_password_file": {"stat": {"exists": False}},
                "cloudfall_inspect_kibana_status": {"skipped": True},
                "cloudfall_inspect_kibana_roles": {"skipped": True},
                "cloudfall_inspect_kibana_users": {"skipped": True},
            },
            {
                "status": {"read": False, "level": None},
                "roles": None,
                "userRoles": None,
            },
            id="no cloudfall password on the server",
        ),
        pytest.param(
            {
                "cloudfall_inspect_kibana_status": {"status": -1, "msg": "refused"},
                "cloudfall_inspect_kibana_roles": {
                    "results": [
                        {"item": VIEW, "status": -1},
                        {"item": EDIT, "status": -1},
                    ]
                },
            },
            {"status": {"read": True, "level": None}, "roles": None},
            id="Kibana does not answer",
        ),
        pytest.param(
            {
                "cloudfall_inspect_kibana_status": {
                    "status": 503,
                    "json": {"status": {"overall": {"level": "unavailable"}}},
                }
            },
            {"status": {"read": True, "level": "unavailable"}},
            id="Kibana reports itself unavailable",
        ),
        pytest.param(
            {
                "cloudfall_inspect_kibana_roles": {
                    "results": [
                        {"item": VIEW, "status": 404, "json": {}},
                        _role_answer(EDIT, "all"),
                    ]
                }
            },
            {
                "roles": [
                    {
                        "name": VIEW,
                        "found": False,
                        "elasticsearch": None,
                        "kibana": None,
                        "metadata": None,
                    },
                    {
                        "name": EDIT,
                        "found": True,
                        "elasticsearch": {"cluster": [], "indices": [], "run_as": []},
                        "kibana": [{"base": ["all"], "feature": {}, "spaces": ["*"]}],
                        "metadata": {"cloudfall_managed_by": "search-kibana"},
                    },
                ]
            },
            id="a deleted role",
        ),
        pytest.param(
            {
                "cloudfall_inspect_kibana_file": {"stat": {"exists": False}},
                "cloudfall_inspect_kibana_content": {"skipped": True},
                "cloudfall_inspect_kibana_options_file": {"stat": {"exists": False}},
                "cloudfall_inspect_kibana_options": {"skipped": True},
                "cloudfall_inspect_kibana_keystore": {
                    "rc": 1,
                    "stdout_lines": [],
                },
                "cloudfall_inspect_kibana_users": {"status": 401, "json": {}},
            },
            {
                "configuration": None,
                "nodeOptions": None,
                "keystore": None,
                "userRoles": None,
            },
            id="no files, no keystore, and the node refusing",
        ),
    ],
)
def test_inspect_reads_a_kibana_it_cannot_fully_read(
    answers: dict[str, object], expected: dict[str, object]
) -> None:
    evidence = _evidence(**answers)

    assert {key: evidence[key] for key in expected} == expected


@pytest.mark.parametrize(
    ("kibana", "elasticsearch", "urls"),
    [
        pytest.param(
            {},
            {},
            ("http://127.0.0.1:5601", "http://127.0.0.1:9200"),
            id="loopback",
        ),
        pytest.param(
            {"address": "::1"},
            {"remoteAccess": {"listenAddress": "10.0.0.11"}},
            ("http://[::1]:5601", "https://10.0.0.11:9200"),
            id="IPv6 loopback and a node with remote access",
        ),
    ],
)
def test_inspect_reaches_kibana_and_the_node_as_the_kibana_role_does(
    kibana: dict[str, object],
    elasticsearch: dict[str, object],
    urls: tuple[str, str],
) -> None:
    variables = _variables(
        cloudfall_inspect_kibana_service=_kibana(**kibana),
        cloudfall_inspect_kibana_elasticsearch_services=[
            _search_main(nodes=["h1"], **elasticsearch)
        ],
    )

    assert (
        _template("{{ cloudfall_inspect_kibana_url }}", variables),
        _template("{{ cloudfall_inspect_kibana_elasticsearch_url }}", variables),
    ) == urls


def test_inspect_reads_the_kibana_service_of_the_server_only() -> None:
    (play,) = _yaml(PLAYBOOK)
    (role,) = play["roles"]
    services = [
        _kibana(),
        {**_kibana(), "id": "other-kibana", "server": "h2"},
        _search_main(),
        {"id": "jobs", "serviceKind": "rabbitmq", "server": "h1"},
    ]
    variables = {"cloudfall_services": services, "cloudfall_server_id": "h1"}

    kibana = _template(role["cloudfall_inspect_kibana_services"], variables)
    clusters = _template(
        role["cloudfall_inspect_kibana_elasticsearch_services"], variables
    )

    assert [service["id"] for service in cast("list[dict[str, str]]", kibana)] == [
        "search-kibana"
    ]
    assert [service["id"] for service in cast("list[dict[str, str]]", clusters)] == [
        "search-main"
    ]
    # A project without Kibana reads nothing more
    assert _template(
        role["cloudfall_inspect_kibana_services"],
        {"cloudfall_services": [_search_main()], "cloudfall_server_id": "h1"},
    ) == []
