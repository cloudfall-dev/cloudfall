"""The Nginx site role's decisions, evaluated with Ansible's own templating.

The role needs a Debian host with Nginx, so these tests replay what it
decides rather than running it: the version it reads from ``nginx -v``, and
which rollback steps run when ``nginx -t`` rejects a render.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).parents[2]
TASKS = ROOT / "engine" / "ansible" / "roles" / "cloudfall_nginx_site" / "tasks"
RESTORE = "Restore the previous virtual host"
REMOVE_FILE = "Remove the rejected new virtual host"
REMOVE_LINK = "Remove the link this run created"
FAIL = "Fail after rolling back the rejected virtual host"
SITES_AVAILABLE = "/etc/nginx/sites-available/crm.example.test.conf"
SITES_ENABLED = "/etc/nginx/sites-enabled/crm.example.test.conf"


def _tasks(name: str) -> list[dict[str, object]]:
    tasks = DataLoader().load_from_file(str(TASKS / name))
    assert isinstance(tasks, list)
    return tasks


def _task(name: str, tasks: list[dict[str, object]]) -> dict[str, object]:
    return next(t for t in tasks if t["name"] == name)


def _templar(variables: dict[str, object]) -> Templar:
    return Templar(loader=DataLoader(), variables=variables)


def _holds(expression: object, variables: dict[str, object]) -> bool:
    return bool(
        _templar(variables).evaluate_conditional(trust_as_template(str(expression)))
    )


def _conditions(task: dict[str, object]) -> list[object]:
    value = task.get("when", [])
    return value if isinstance(value, list) else [value]


def _probe(stderr: str, rc: int = 0) -> dict[str, object]:
    return {"cloudfall_nginx_site_version_probe": {"rc": rc, "stderr": stderr}}


def _version_readable(probe: dict[str, object]) -> bool:
    task = _task("Require a readable Nginx version", _tasks("main.yml"))
    assertion = task["ansible.builtin.assert"]
    assert isinstance(assertion, dict)
    return all(_holds(c, probe) for c in assertion["that"])


def _recorded_version(probe: dict[str, object]) -> object:
    task = _task("Record the installed Nginx version", _tasks("main.yml"))
    facts = task["ansible.builtin.set_fact"]
    assert isinstance(facts, dict)
    expression = facts["cloudfall_nginx_site_version"]
    return _templar(probe).template(trust_as_template(expression))


@pytest.mark.parametrize(
    ("stderr", "version"),
    [
        ("nginx version: nginx/1.22.1\n", "1.22.1"),
        ("nginx version: nginx/1.24.0 (Ubuntu)\n", "1.24.0"),
        ("nginx version: nginx/1.26.3\n", "1.26.3"),
    ],
)
def test_the_installed_version_is_read_from_nginx_v(
    stderr: str, version: str
) -> None:
    probe = _probe(stderr)

    assert _version_readable(probe)
    assert _recorded_version(probe) == version


@pytest.mark.parametrize(
    ("stderr", "rc"),
    [
        ("", 2),
        ("nginx: [emerg] something else\n", 1),
        ("nginx version: openresty/1.25.3.1\n", 0),
        ("nginx version: nginx/1.25\n", 0),
    ],
)
def test_an_unreadable_version_fails_the_converge(stderr: str, rc: int) -> None:
    assert not _version_readable(_probe(stderr, rc))


def _rescue_tasks() -> list[dict[str, object]]:
    block = _task(
        "Apply the virtual host behind the configuration test",
        _tasks("apply_virtual_host.yml"),
    )
    rescue = block["rescue"]
    assert isinstance(rescue, list)
    return rescue


def _rollback(
    *,
    file_existed: bool,
    link_existed: bool,
    link: str = SITES_ENABLED,
    check_mode: bool = False,
) -> set[str]:
    """Name every rescue step whose ``when`` holds for this host."""
    variables: dict[str, object] = {
        "ansible_check_mode": check_mode,
        "cloudfall_nginx_site_link": link,
        "cloudfall_nginx_site_previous_file": {"stat": {"exists": file_existed}},
        # A skipped stat registers no `stat`: the link condition guards it.
        "cloudfall_nginx_site_previous_link": (
            {"stat": {"exists": link_existed}} if link else {"skipped": True}
        ),
    }
    return {
        str(task["name"])
        for task in _rescue_tasks()
        if all(_holds(c, variables) for c in _conditions(task))
    }


def test_a_rejected_first_render_removes_the_file_and_its_new_link() -> None:
    assert _rollback(file_existed=False, link_existed=False) == {
        REMOVE_FILE,
        REMOVE_LINK,
        FAIL,
    }


def test_a_rejected_rerender_restores_the_file_and_keeps_its_link() -> None:
    assert _rollback(file_existed=True, link_existed=True) == {RESTORE, FAIL}


def test_a_link_this_run_created_is_removed_beside_a_restored_file() -> None:
    assert _rollback(file_existed=True, link_existed=False) == {
        RESTORE,
        REMOVE_LINK,
        FAIL,
    }


def test_a_vhost_outside_sites_available_has_no_link_to_roll_back() -> None:
    assert _rollback(file_existed=False, link_existed=False, link="") == {
        REMOVE_FILE,
        FAIL,
    }


@pytest.mark.parametrize("file_existed", [True, False])
def test_check_mode_rolls_nothing_back_and_still_fails(*, file_existed: bool) -> None:
    """Check mode changed nothing on disk, so there is nothing to restore."""
    assert _rollback(
        file_existed=file_existed, link_existed=False, check_mode=True
    ) == {FAIL}


def test_the_rollback_failure_carries_the_nginx_error() -> None:
    task = _task(FAIL, _rescue_tasks())
    failure = task["ansible.builtin.fail"]
    assert isinstance(failure, dict)
    error = 'nginx: [emerg] unknown directive "http2" in crm.example.test.conf:41'
    variables = {
        "cloudfall_nginx_site_domain": {
            "proxy": {"configurationPath": SITES_AVAILABLE}
        },
        "ansible_failed_result": {"rc": 1, "stderr": error, "msg": "non-zero"},
    }

    message = _templar(variables).template(trust_as_template(failure["msg"]))

    assert isinstance(message, str)
    assert message.startswith(SITES_AVAILABLE)
    assert "Nginx was not reloaded" in message
    assert message.endswith(error)


def test_the_link_is_named_after_a_sites_available_vhost() -> None:
    task = _task("Locate the managed virtual host link", _tasks("main.yml"))
    facts = task["ansible.builtin.set_fact"]
    assert isinstance(facts, dict)

    def link(path: str) -> object:
        variables: dict[str, object] = {
            "cloudfall_nginx_site_domain": {"proxy": {"configurationPath": path}}
        }
        return _templar(variables).template(
            trust_as_template(facts["cloudfall_nginx_site_link"])
        )

    assert link(SITES_AVAILABLE) == SITES_ENABLED
    assert link("/etc/nginx/conf.d/crm.example.test.conf") == ""
