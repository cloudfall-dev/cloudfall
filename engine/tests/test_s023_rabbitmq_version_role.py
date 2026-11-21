"""The ``cloudfall_rabbitmq`` role's rolling change of version (S-023, #509).

A live change needs three Debian 13 hosts with two RabbitMQ releases, so the
role's decisions are evaluated by ``ansible-playbook`` with the role's own
filters, its refusals run for real, and the reads after the last node run
against the stand-in ``rabbitmq-diagnostics`` and ``rabbitmqctl``. What only
live hosts prove (the package going from one release to the next, a publisher
with confirms that reconnects and loses no message) is listed in the pull
request as unverified.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml
from rabbitmq_node import fresh_state, write_ctl
from test_s023_rabbitmq_cluster_role import NAMES, THREE, _play, _rendered_cluster
from test_s023_rabbitmq_role import (
    JOBS,
    ROLE,
    _defaults,
    _service,
    _task,
    _tasks,
)

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

requires_ansible = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook is not on PATH"
)
HOSTVARS = {node: f"{node}.example.internal" for node in THREE}


def _load() -> ModuleType:
    path = ROLE / "filter_plugins" / "rabbitmq_version.py"
    spec = importlib.util.spec_from_file_location("rabbitmq_version", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VERSION = _load()
ACCEPTED = list(
    cast("dict[str, str]", _defaults()["cloudfall_rabbitmq_erlang_minimum"])
)


def _installed(version: str) -> dict[str, object]:
    """Return what the role registers from ``dpkg-query`` for ``version``."""
    if not version:
        return {"rc": 1, "stdout": ""}
    return {"rc": 0, "stdout": f"installed|{version}-1"}


def _status(versions: dict[str, str]) -> dict[str, object]:
    """Return ``cluster_status`` with every member running ``versions``."""
    return {
        "disk_nodes": list(versions),
        "ram_nodes": [],
        "running_nodes": list(versions),
        "versions": {
            node: {"rabbitmq_version": version, "erlang_version": "27.3.4"}
            for node, version in versions.items()
        },
        "alarms": [],
        "partitions": {},
    }


def _node_vars(
    server: str = "h1", version: str = "4.4.1", **extra: object
) -> dict[str, object]:
    """Return the role's variables on ``server`` of three nodes."""
    return {
        "cloudfall_server_id": server,
        "cloudfall_hostname": f"{server}.example.internal",
        "cloudfall_rabbitmq_service": _service(JOBS, nodes=THREE, version=version),
        "cloudfall_rabbitmq_cluster": _rendered_cluster(server),
        **extra,
    }


def _evaluate(
    tmp_path: Path, expressions: dict[str, str], variables: dict[str, object]
) -> dict[str, object]:
    """Evaluate each expression on the node by ``ansible-playbook``; return them."""
    out = tmp_path / "out"
    out.mkdir(parents=True)
    tasks = [
        {
            "name": f"Evaluate {name}",
            "ansible.builtin.copy": {
                "content": "{{ (" + expression + ") | to_json }}",
                "dest": str(out / f"{name}.json"),
                "mode": "0600",
            },
        }
        for name, expression in expressions.items()
    ]
    done, _ = _play(tmp_path, tasks, variables, hostvars=HOSTVARS)
    assert done.returncode == 0, done.stdout + done.stderr
    return {
        name: json.loads((out / f"{name}.json").read_text("utf-8"))
        for name in expressions
    }


def _refusals(tmp_path: Path, variables: dict[str, object]) -> tuple[bool, str]:
    """Run main.yml's two version refusals on the node; return success and output."""
    main = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text("utf-8"))
    tasks = [
        _task("Refuse a version other than the installed one", main),
        _task("Refuse a version change a running member can't make", main),
        # Never reached after a refusal: no task after them changed a host
        {"name": "Change a package", "ansible.builtin.meta": "noop"},
    ]
    done, _ = _play(tmp_path, tasks, variables, hostvars=HOSTVARS)
    return done.returncode == 0, done.stdout + done.stderr


# proves: S-023-12
@requires_ansible
def test_s023_12_every_other_node_changes_first_and_the_home_last(
    tmp_path: Path,
) -> None:
    found = _evaluate(
        tmp_path / "h1",
        {"order": "cloudfall_rabbitmq_version_order"},
        _node_vars("h1"),
    )
    assert found["order"] == ["h2", "h3", "h1"]
    # The home is spec.server wherever it is declared
    home = _service(JOBS, nodes=THREE)
    home["server"] = "h2"
    found = _evaluate(
        tmp_path / "h2",
        {"order": "cloudfall_rabbitmq_version_order"},
        _node_vars("h2", cloudfall_rabbitmq_service=home),
    )
    assert found["order"] == ["h1", "h3", "h2"]
    # One at a time: each node's turn is delegated to it, in that order
    version = _tasks("version.yml")
    loop = _task("Change the version of each node, the service's home last", version)
    assert loop["ansible.builtin.include_tasks"] == "version_node.yml"
    assert loop["loop"] == "{{ cloudfall_rabbitmq_version_order }}"
    turn = _tasks("version_node.yml")
    block = _task("Change the version of {{ cloudfall_rabbitmq_version_node }}", turn)
    assert block["delegate_to"] == "{{ cloudfall_rabbitmq_version_node }}"
    read = turn[0]
    assert read["delegate_to"] == "{{ cloudfall_rabbitmq_version_node }}"
    assert read["changed_when"] is False


# proves: S-023-12
def test_s023_12_a_node_stops_only_once_the_others_can_take_over() -> None:
    block = _task(
        "Change the version of {{ cloudfall_rabbitmq_version_node }}",
        _tasks("version_node.yml"),
    )
    steps = [task["name"] for task in block["block"]]
    assert steps == [
        "Wait until the cluster is healthy before the node stops",
        "Enable every stable feature flag before the first node stops",
        "Remember that the flags were enabled before the first stop",
        "Wait until every quorum queue keeps its majority without the node",
        "Move the clients and queue leaders of the node to the others",
        "Stop the node",
        "Install the declared RabbitMQ server under a released hold",
        "Start the node with the declared version",
        "Wait until the node runs again",
        "Take the node out of maintenance",
        "Wait until the cluster is healthy before the next node",
    ]
    tasks = {task["name"]: task for task in block["block"]}
    quorum = tasks["Wait until every quorum queue keeps its majority without the node"]
    assert quorum["ansible.builtin.command"]["argv"][:3] == [
        "{{ cloudfall_rabbitmq_upgrade }}",
        "--quiet",
        "await_online_quorum_plus_one",
    ]
    # The publishers of the node reconnect to another node
    drain = tasks["Move the clients and queue leaders of the node to the others"]
    assert drain["ansible.builtin.command"]["argv"][-1] == "drain"
    for name in (
        "Wait until every quorum queue keeps its majority without the node",
        "Move the clients and queue leaders of the node to the others",
        "Take the node out of maintenance",
    ):
        assert tasks[name]["when"] == "cloudfall_rabbitmq_has_peers | bool"
    for name in (
        "Wait until the cluster is healthy before the node stops",
        "Wait until the cluster is healthy before the next node",
    ):
        health = tasks[name]["cloudfall_rabbitmq_health"]
        assert health["nodes"] == "{{ cloudfall_rabbitmq_version_members }}"
    assert _defaults()["cloudfall_rabbitmq_upgrade"] == "/usr/sbin/rabbitmq-upgrade"


# proves: S-023-12
def test_s023_12_the_package_is_released_installed_and_held_again() -> None:
    block = _task(
        "Change the version of {{ cloudfall_rabbitmq_version_node }}",
        _tasks("version_node.yml"),
    )
    held = {task["name"]: task for task in block["block"]}[
        "Install the declared RabbitMQ server under a released hold"
    ]
    tasks = {task["name"]: task for task in held["block"]}
    # The package is held again even when its install fails: a node left
    # unheld would follow the next apt upgrade
    assert [task["name"] for task in held["always"]] == [
        "Hold the RabbitMQ server package again"
    ]
    tasks |= {task["name"]: task for task in held["always"]}
    release = tasks["Release the hold of the RabbitMQ server package"]
    assert release["ansible.builtin.dpkg_selections"] == {
        "name": "rabbitmq-server",
        "selection": "install",
    }
    install = tasks["Install the declared RabbitMQ server"]["ansible.builtin.apt"]
    assert install["name"] == (
        "rabbitmq-server={{ cloudfall_rabbitmq_package_version }}"
    )
    # Nothing starts during the install; the role starts it
    assert install["policy_rc_d"] == 101
    hold = tasks["Hold the RabbitMQ server package again"]
    assert hold["ansible.builtin.dpkg_selections"] == {
        "name": "rabbitmq-server",
        "selection": "hold",
    }
    # Only a node whose package is not the declared one changes
    assert block["when"] == [
        "cloudfall_rabbitmq_node_version | length > 0",
        "cloudfall_rabbitmq_node_version != cloudfall_rabbitmq_package_version",
    ]


# proves: S-023-12
@requires_ansible
def test_s023_12_a_node_alone_changes_with_no_quorum_wait(tmp_path: Path) -> None:
    alone = _evaluate(
        tmp_path / "one",
        {"members": "cloudfall_rabbitmq_version_members"},
        {
            **_node_vars("h1"),
            "cloudfall_rabbitmq_service": _service(JOBS, version="4.4.1"),
        },
    )
    # One member: no other node can take over, and no quorum wait deadlocks
    assert alone["members"] == ["rabbit@h1"]
    three = _evaluate(
        tmp_path / "three",
        {"members": "cloudfall_rabbitmq_version_members"},
        _node_vars(
            "h1",
            cloudfall_rabbitmq_membership={
                "members": NAMES,
                "running": NAMES,
                "majority": True,
            },
        ),
    )
    assert three["members"] == NAMES


# proves: S-023-12
@pytest.mark.parametrize(
    ("server", "installed", "running", "due"),
    [
        pytest.param("h1", "4.4.1", {}, False, id="nothing to change"),
        pytest.param("h1", "4.4.0", {}, True, id="the home's package"),
        pytest.param(
            "h1", "4.4.1", {"rabbit@h2": "4.4.0"}, True, id="a member's version"
        ),
        # The other nodes change from the home, which runs first
        pytest.param("h2", "4.4.0", {}, False, id="not the home"),
    ],
)
@requires_ansible
def test_s023_12_the_change_is_due_only_on_the_home_and_only_once(
    tmp_path: Path, server: str, installed: str, running: dict[str, str], *, due: bool
) -> None:
    versions = dict.fromkeys(NAMES, "4.4.1") | running
    found = _evaluate(
        tmp_path,
        {"due": "cloudfall_rabbitmq_version_change | bool"},
        _node_vars(
            server,
            cloudfall_rabbitmq_installed=_installed(installed),
            cloudfall_rabbitmq_membership_read={
                "rc": 0,
                "stdout": json.dumps(_status(versions)),
            },
        ),
    )
    assert found["due"] is due


# proves: S-023-12
def test_s023_12_the_change_comes_before_the_home_converges_its_settings() -> None:
    main = _tasks("main.yml")
    names = [task["name"] for task in main]
    change = names.index("Change the RabbitMQ version of every node one at a time")
    assert (
        names.index(
            "Install the RabbitMQ repository and the Erlang of the Debian release"
        )
        < change
    )
    assert change < names.index("Install the pinned RabbitMQ server")
    task = main[change]
    assert task["ansible.builtin.include_tasks"] == "version.yml"
    assert task["when"] == "cloudfall_rabbitmq_version_change | bool"


# proves: S-023-13
def test_s023_13_a_downgrade_and_a_skipped_series_are_refused() -> None:
    refusal = VERSION.version_refusal
    # Inside a series, and to the next accepted one
    assert refusal(["4.3.2-1"], "4.3.5", ACCEPTED) == ""
    assert refusal(["4.3.2-1", "4.3.2"], "4.4.1", ACCEPTED) == ""
    assert refusal(["", "4.4.1"], "4.4.1", ACCEPTED) == ""
    # An older version, and a series before the installed one
    assert "does not support downgrades" in refusal(["4.4.2-1"], "4.4.1", ACCEPTED)
    assert "does not support downgrades" in refusal(["4.4.1"], "4.3.9", ACCEPTED)
    # A series past the next accepted one
    skipped = refusal(["4.3.2"], "4.5.0", [*ACCEPTED, "4.5"])
    assert "nor of the next accepted series 4.4" in skipped
    with pytest.raises(VERSION.VersionReadError):
        refusal(["4.4"], "4.4.1", ACCEPTED)


# proves: S-023-13
@pytest.mark.parametrize(
    ("server", "installed", "declared", "running", "refused"),
    [
        pytest.param("h1", "4.3.2", "4.4.1", {}, "", id="the next series"),
        pytest.param("h1", "4.4.1", "4.4.2", {}, "", id="a later patch"),
        pytest.param(
            "h1", "4.4.2", "4.4.1", {}, "does not support downgrades", id="older"
        ),
        pytest.param(
            "h1",
            "4.4.1",
            "4.3.9",
            {},
            "does not support downgrades",
            id="the series before",
        ),
        pytest.param(
            "h1",
            "4.4.1",
            "4.4.1",
            {"rabbit@h3": "4.4.3"},
            "does not support downgrades",
            id="a member runs a later one",
        ),
        pytest.param(
            "h2",
            "4.3.2",
            "4.4.1",
            {},
            "changes from the home of the service, h1",
            id="not from the home",
        ),
    ],
)
@requires_ansible
def test_s023_13_the_role_refuses_before_any_package_changes(  # noqa: PLR0913, PLR0917 - one case per row
    tmp_path: Path,
    server: str,
    installed: str,
    declared: str,
    running: dict[str, str],
    refused: str,
) -> None:
    versions = dict.fromkeys(NAMES, installed) | running
    ok, output = _refusals(
        tmp_path,
        _node_vars(
            server,
            declared,
            cloudfall_rabbitmq_installed=_installed(installed),
            cloudfall_rabbitmq_membership_read={
                "rc": 0,
                "stdout": json.dumps(_status(versions)),
            },
        ),
    )

    assert ok is (not refused), output
    if refused:
        assert refused in output
        assert "Nothing changed" in output
        assert "Change a package" not in output


# proves: S-023-12
# proves: S-023-13
@pytest.mark.parametrize(
    ("installed", "declared", "refused"),
    [
        pytest.param("4.3.2", "4.4.1", "", id="the next series"),
        pytest.param("4.4.1", "4.4.2", "", id="a later patch"),
        pytest.param("4.4.2", "4.4.1", "does not support downgrades", id="a downgrade"),
        pytest.param(
            "4.3.2", "4.5.0", "nor of the next accepted series", id="a skipped series"
        ),
    ],
)
@requires_ansible
def test_s023_12_a_dry_run_previews_the_change_on_a_node_other_than_the_home(
    tmp_path: Path, installed: str, declared: str, refused: str
) -> None:
    # The home only previewed h2's turn, so h2 still holds the old package
    main = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text("utf-8"))
    service = _service(JOBS, nodes=THREE, version=declared)
    tasks = [
        _task("Refuse a version other than the installed one", main),
        _task("Refuse a version change a running member can't make", main),
        _task("Install the pinned RabbitMQ server", main),
    ]
    done, _ = _play(
        tmp_path,
        tasks,
        _node_vars(
            "h2",
            cloudfall_rabbitmq_service=service,
            cloudfall_rabbitmq_installed=_installed(installed),
            cloudfall_rabbitmq_erlang_minimum={
                "4.3": "27.0",
                "4.4": "27.0",
                "4.5": "27.0",
            },
        ),
        hostvars=HOSTVARS,
        check=True,
    )
    output = done.stdout + done.stderr

    if refused:
        assert done.returncode != 0, output
        assert refused in output
        assert "Nothing changed" in output
        assert "Install the pinned RabbitMQ server" not in output
    else:
        assert done.returncode == 0, output
        # Still held at the old version: the home's preview stands for it
        install = output.split("Install the pinned RabbitMQ server", 1)[1]
        assert "skipping: [h2]" in install.splitlines()[1], output


# proves: S-023-13
def test_s023_13_both_refusals_come_before_any_task_that_changes_a_host() -> None:
    names = [task["name"] for task in _tasks("main.yml")]
    first_change = names.index(
        "Install the RabbitMQ repository and the Erlang of the Debian release"
    )
    for refusal in (
        "Refuse a version other than the installed one",
        "Refuse a version change a running member can't make",
    ):
        assert names.index(refusal) < first_change
    # The members' versions come from the majority check's read
    assert names.index("Refuse a converge while the cluster has no majority") < (
        names.index("Refuse a version change a running member can't make")
    )


# proves: S-023-13
@requires_ansible
def test_s023_13_flags_are_enabled_once_before_the_first_node_stops(
    tmp_path: Path,
) -> None:
    state = fresh_state()
    state["featureFlags"] = [
        {"name": "khepri_db", "state": "enabled", "stability": "stable"},
        {"name": "new_in_series", "state": "disabled", "stability": "stable"},
    ]
    ctl, _, log = write_ctl(tmp_path / "node", state)
    block = _task(
        "Change the version of {{ cloudfall_rabbitmq_version_node }}",
        yaml.safe_load((ROLE / "tasks" / "version_node.yml").read_text("utf-8")),
    )
    steps = {task["name"]: task for task in block["block"]}
    enable = [
        steps["Enable every stable feature flag before the first node stops"],
        steps["Remember that the flags were enabled before the first stop"],
    ]

    # Two nodes' turns: only the first one enables them
    done, _ = _play(
        tmp_path,
        [*enable, *enable],
        {**_node_vars("h1"), "cloudfall_rabbitmq_ctl": str(ctl)},
        hostvars=HOSTVARS,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    calls = [
        json.loads(line)
        for line in log.read_text("utf-8").splitlines()
        if "enable_feature_flag" in line
    ]
    assert calls == [["enable_feature_flag", "all"]]


def _after_last_node(
    tmp_path: Path, flags: list[dict[str, str]], versions: dict[str, str]
) -> tuple[bool, str, list[dict[str, str]]]:
    """Run version.yml's tasks after the last node against the stand-in."""
    state = fresh_state()
    state["featureFlags"] = flags
    state["cluster"] = _status(versions)
    ctl, state_path, _ = write_ctl(tmp_path / "node", state)
    after = [
        task
        for task in yaml.safe_load((ROLE / "tasks" / "version.yml").read_text("utf-8"))
        if "ansible.builtin.include_tasks" not in task
    ]
    done, _ = _play(
        tmp_path,
        after,
        {
            **_node_vars("h1"),
            "cloudfall_rabbitmq_ctl": str(ctl),
            "cloudfall_rabbitmq_diagnostics": str(ctl),
            "cloudfall_rabbitmq_membership": {
                "members": NAMES,
                "running": NAMES,
                "majority": True,
            },
        },
        hostvars=HOSTVARS,
    )
    final = json.loads(state_path.read_text("utf-8"))
    return (
        done.returncode == 0,
        done.stdout + done.stderr,
        cast("list[dict[str, str]]", final["featureFlags"]),
    )


# proves: S-023-12
# proves: S-023-13
@requires_ansible
def test_s023_13_after_the_last_node_every_stable_flag_is_enabled(
    tmp_path: Path,
) -> None:
    flags = [
        {"name": "khepri_db", "state": "enabled", "stability": "stable"},
        # A flag the new series brings
        {"name": "new_in_series", "state": "disabled", "stability": "stable"},
        {"name": "trial", "state": "disabled", "stability": "experimental"},
    ]

    ok, output, final = _after_last_node(tmp_path, flags, dict.fromkeys(NAMES, "4.4.1"))

    assert ok, output
    assert {flag["name"]: flag["state"] for flag in final} == {
        "khepri_db": "enabled",
        "new_in_series": "enabled",
        # An experimental flag is never enabled by the change
        "trial": "disabled",
    }


# proves: S-023-12
@requires_ansible
def test_s023_12_a_node_that_reports_another_version_stops_the_converge(
    tmp_path: Path,
) -> None:
    flags = [{"name": "khepri_db", "state": "enabled", "stability": "stable"}]

    ok, output, _ = _after_last_node(
        tmp_path, flags, {**dict.fromkeys(NAMES, "4.4.1"), "rabbit@h2": "4.4.0"}
    )

    assert not ok
    assert '"rabbit@h2": "4.4.0"' in output


def test_s023_13_the_version_filters_read_rabbitmqs_output() -> None:
    status = json.dumps(_status({"rabbit@h1": "4.4.1", "rabbit@h2": "4.3.2"}))
    assert VERSION.node_versions(status) == {
        "rabbit@h1": "4.4.1",
        "rabbit@h2": "4.3.2",
    }
    # An output without the versions map is not read as no member running:
    # the refusals of S-023-13 would pass every member unchecked
    with pytest.raises(VERSION.VersionReadError):
        VERSION.node_versions(json.dumps({"running_nodes": []}))
    with pytest.raises(VERSION.VersionReadError):
        VERSION.node_versions(json.dumps({"versions": {"rabbit@h1": {}}}))
    listed = json.dumps(
        [
            {"name": "a", "state": "enabled", "stability": "required"},
            {"name": "b", "state": "disabled", "stability": "stable"},
            {"name": "c", "state": "disabled", "stability": "experimental"},
        ]
    )
    assert VERSION.disabled_stable_flags(listed) == ["b"]
    with pytest.raises(VERSION.VersionReadError):
        VERSION.disabled_stable_flags("Error")
    # A flag without its stability or state is not read as one that passes
    for partial in (
        {"name": "b", "state": "disabled"},
        {"name": "b", "stability": "stable"},
        {"state": "disabled", "stability": "stable"},
    ):
        with pytest.raises(VERSION.VersionReadError):
            VERSION.disabled_stable_flags(json.dumps([partial]))


def test_the_check_mode_install_waits_for_the_previewed_change() -> None:
    install = _task("Install the pinned RabbitMQ server", _tasks("main.yml"))
    assert "cloudfall_rabbitmq_version_change | bool" in install["when"]
    assert "cloudfall_rabbitmq_version_previewed | bool" in install["when"]
    flags: dict[str, Any] = _task(
        "Enable every stable feature flag after the last node", _tasks("version.yml")
    )
    assert flags["ansible.builtin.command"]["argv"][-2:] == [
        "enable_feature_flag",
        "all",
    ]
