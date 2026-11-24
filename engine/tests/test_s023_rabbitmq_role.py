"""The ``cloudfall_rabbitmq`` role for one node (S-023, #506).

The role needs a Debian 13 host with RabbitMQ, so its decisions are evaluated
with Ansible's own templating, its settings rendered from its templates, and
its virtual host and owner tasks run for real on localhost against a
stand-in ``rabbitmqctl`` that keeps the cluster's definitions in a file: a
second converge must change nothing. What a live host alone can prove (the
package and its hold, the listeners RabbitMQ opens, a client's login and the
type of the queue it declares) is listed in the pull request as unverified.
"""

from __future__ import annotations

import base64
import hashlib
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
from cloudfall.rabbitmq import (
    MANAGED_TAG,
    OWNER_TAG_PREFIX,
    SUPPORTED_SERIES,
    password_hash,
)
from rabbitmq_node import fresh_state, write_ctl

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import ModuleType

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_rabbitmq"
PLAYBOOKS = ROOT / "engine" / "ansible" / "playbooks"
SERVICES = PLAYBOOKS / "services.yml"
PASSWORD = "s3cr3t-jobs-value"  # noqa: S105 - the value every output is grepped for
COOKIE = "s3cr3t-cookie-value"
RECAP = re.compile(r"localhost\s+:\s+ok=\d+\s+changed=(\d+)")
JOBS: dict[str, object] = {
    "name": "jobs",
    "owner": "jobs",
    "passwordRef": {
        "environment": "production",
        "path": "/services/rabbitmq-main",
        "key": "JOBS_PASSWORD",
    },
}
# The tasks of main.yml that only read or refuse; every one before the first
# task that changes the host
REFUSALS = (
    "Validate RabbitMQ service target and declaration",
    "Require the node's rendered client access",
    "Require the node's rendered cluster",
    "Refuse a memory watermark above six tenths of the server's memory",
    "Refuse a series Cloudfall does not install",
    "Refuse a host whose hostname is not the declared one",
    "Require the staged Erlang cookie and every owner's password",
    "Read the installed RabbitMQ server package",
    "Refuse a version other than the installed one",
    "Validate the declared backup and restore-drill calendar expressions",
    # A cluster's majority is read, and its loss refused, before any change
    "Refuse a converge while the cluster has no majority",
    # A version no running member can move to, as well (S-023-13)
    "Refuse a version change a running member can't make",
)


def _service(*vhosts: dict[str, object], **rabbitmq: object) -> dict[str, object]:
    return {
        "id": "rabbitmq-main",
        "serviceKind": "rabbitmq",
        "environment": "production",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": 5672},
        "rabbitmq": {
            "version": "4.4.1",
            "memoryHighWatermarkMb": 2048,
            "nodes": ["h1"],
            "vhosts": list(vhosts),
            **rabbitmq,
        },
        "backup": {
            "directory": "/var/backups/cloudfall/rabbitmq-main",
            "onCalendar": "*-*-* 02:50:00 UTC",
            "retentionDays": 14,
        },
        "metrics": {"enabled": True},
    }


def _filters() -> ModuleType:
    path = ROLE / "filter_plugins" / "rabbitmq_accounts.py"
    spec = importlib.util.spec_from_file_location("rabbitmq_accounts", path)
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
        "cloudfall_server_id": "h1",
        "cloudfall_hostname": "h1.example.internal",
        "hostvars": {"h1": {"cloudfall_hostname": "h1.example.internal"}},
        "ansible_facts": {
            "distribution": "Debian",
            "distribution_major_version": "13",
            "distribution_release": "trixie",
            "service_mgr": "systemd",
            "memtotal_mb": 8192,
            "hostname": "h1",
        },
        "cloudfall_rabbitmq_service": _service(JOBS),
        "cloudfall_rabbitmq_files": {
            "cookie": "/tmp/staged/rabbitmq-main.cookie",  # noqa: S108
            "owners": {"jobs": "/tmp/staged/rabbitmq-main-jobs.password"},  # noqa: S108
        },
        **extra,
    }


def _holds(task: dict[str, Any], variables: dict[str, object]) -> bool:
    that = cast("dict[str, list[str]]", task["ansible.builtin.assert"])["that"]
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(
        templar.evaluate_conditional(trust_as_template(str(condition)))
        for condition in that
    )


def _template(expression: str, variables: dict[str, object]) -> object:
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(expression))


def _render(name: str, variables: dict[str, object]) -> str:
    text = (ROLE / "templates" / name).read_text(encoding="utf-8")
    return str(_template(text, variables))


def _settings(variables: dict[str, object]) -> dict[str, str]:
    """Read every managed conf.d file as RabbitMQ does: later files win."""
    found: dict[str, str] = {}
    for name in cast("list[str]", _defaults()["cloudfall_rabbitmq_managed_files"]):
        for line in _render(f"{name}.j2", variables).splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                found[key.strip()] = value.strip()
    return found


# proves: S-023-3
def test_s023_3_the_managed_settings_are_the_declared_ones() -> None:
    settings = _settings(_variables())

    assert settings["vm_memory_high_watermark.absolute"] == "2048MiB"
    assert settings["disk_free_limit.absolute"] == "2GB"
    # Nothing outside loopback: clients, the inter-node port and metrics
    assert settings["listeners.tcp.default"] == "127.0.0.1:5672"
    assert settings["listeners.ssl"] == "none"
    assert settings["distribution.listener.interface"] == "127.0.0.1"
    assert settings["prometheus.tcp.ip"] == "127.0.0.1"
    # One node is a cluster that waits for two more
    assert settings["cluster_formation.peer_discovery_backend"] == "classic_config"
    assert settings["cluster_formation.classic_config.nodes.1"] == "rabbit@h1"
    assert (
        settings["quorum_queue.continuous_membership_reconciliation.enabled"] == "true"
    )
    assert (
        settings["quorum_queue.continuous_membership_reconciliation.target_group_size"]
        == "3"
    )
    # The managed files sort after an operator's own
    names = cast("list[str]", _defaults()["cloudfall_rabbitmq_managed_files"])
    assert all(name.startswith(("9",)) for name in names)


# proves: S-023-3
def test_s023_3_the_only_enabled_plugin_is_the_prometheus_one() -> None:
    assert _render("enabled_plugins.j2", _variables()).strip() == (
        "[rabbitmq_prometheus]."
    )
    # And epmd, which the node starts, binds to loopback
    env = _task("Keep the node's name and epmd on loopback", _tasks("main.yml"))
    block = _template(env["ansible.builtin.blockinfile"]["block"], _variables())
    assert "ERL_EPMD_ADDRESS=127.0.0.1" in str(block)
    assert "NODENAME=rabbit@h1" in str(block)
    mask = _task("Mask the distribution's epmd socket", _tasks("package.yml"))
    assert mask["ansible.builtin.systemd_service"] == {
        "name": "epmd.socket",
        "masked": True,
    }


# proves: S-023-3
def test_s023_3_rabbitmq_and_the_erlang_packages_are_pinned_and_held() -> None:
    variables = _variables()
    main = _tasks("main.yml")
    package = _task("Install the pinned RabbitMQ server", main)
    assert _template(package["ansible.builtin.apt"]["name"], variables) == (
        "rabbitmq-server=4.4.1-1"
    )
    assert package["ansible.builtin.apt"]["policy_rc_d"] == 101
    hold = _task("Hold the RabbitMQ server package", main)
    assert hold["ansible.builtin.dpkg_selections"] == {
        "name": "rabbitmq-server",
        "selection": "hold",
    }
    erlang = _task("Hold the Erlang packages", _tasks("package.yml"))
    assert erlang["ansible.builtin.dpkg_selections"]["selection"] == "hold"
    assert erlang["loop"] == "{{ cloudfall_rabbitmq_erlang_packages }}"
    assert "erlang-base" in cast(
        "list[str]", _defaults()["cloudfall_rabbitmq_erlang_packages"]
    )
    # Erlang comes from the Debian release: no second repository
    repository = _task("Configure Team RabbitMQ's repository", _tasks("package.yml"))
    uris = _template(repository["ansible.builtin.deb822_repository"]["uris"], variables)
    assert all(
        "rabbitmq-server/debian/trixie" in str(uri) for uri in cast("list[str]", uris)
    )


# proves: S-023-3
def test_s023_3_the_data_directory_must_be_the_rabbitmq_volume() -> None:
    storage = _tasks("storage.yml")
    actions = _template(
        cast("str", _defaults()["cloudfall_rabbitmq_volume_actions"]),
        _variables(
            cloudfall_volume_actions={
                "h1": [{"name": "rabbitmq"}, {"name": "mariadb"}],
            }
        ),
    )
    assert actions == [{"name": "rabbitmq"}]
    refuse = _task("Refuse a data directory that is not its volume", storage)
    assert refuse["ansible.builtin.assert"]["that"] == [
        "cloudfall_rabbitmq_data_mount.rc == 0"
    ]
    assert _defaults()["cloudfall_rabbitmq_data_path"] == "/var/lib/rabbitmq"
    names = [task["name"] for task in _tasks("main.yml")]
    # The volume is mounted before the package writes its first node there
    assert names.index("Prepare the data volume") < names.index(
        "Install the pinned RabbitMQ server"
    )


# proves: S-023-3
def test_s023_3_a_listener_outside_loopback_is_refused() -> None:
    sockets = "\n".join(  # noqa: FLY002 - one socket per line
        (
            'LISTEN 0 128 127.0.0.1:5672 0.0.0.0:* users:(("beam.smp",pid=9,fd=1))',
            'LISTEN 0 128 127.0.0.1:25672 0.0.0.0:* users:(("beam.smp",pid=9,fd=2))',
            'LISTEN 0 128 127.0.0.1:4369 0.0.0.0:* users:(("epmd",pid=8,fd=3))',
            'LISTEN 0 128 [::1]:15692 [::]:* users:(("beam.smp",pid=9,fd=4))',
            'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))',
        )
    )
    assert FILTERS.public_listeners(sockets) == []
    exposed = sockets + '\nLISTEN 0 128 [::]:4369 [::]:* users:(("epmd",pid=8,fd=4))'
    assert FILTERS.public_listeners(exposed) == ["[::]:4369 epmd"]
    refuse = _task(
        "Refuse a listener of RabbitMQ or epmd outside loopback", _tasks("main.yml")
    )
    assert refuse["ansible.builtin.assert"]["that"] == [
        "cloudfall_rabbitmq_public | length == 0"
    ]


def test_the_erlang_table_is_the_sdk_one() -> None:
    table = cast("dict[str, str]", _defaults()["cloudfall_rabbitmq_erlang_minimum"])
    assert table == {
        series.label: series.erlang.minimum.label for series in SUPPORTED_SERIES
    }
    assert (
        FILTERS.erlang_release(
            "erlang-base:\n  Installed: (none)\n  Candidate: 1:27.3.4.1+dfsg-1\n"
        )
        == "27.3"
    )
    assert (
        FILTERS.erlang_release(
            "erlang-base:\n  Installed: 1:26.2.5-1\n  Candidate: 1:27.3-1\n"
        )
        == "26.2"
    )
    assert (
        FILTERS.erlang_release(
            "erlang-base:\n  Installed: (none)\n  Candidate: (none)\n"
        )
        == ""
    )
    assert FILTERS.erlang_supported("27.3", "27.0")
    assert not FILTERS.erlang_supported("26.2", "27.0")
    assert not FILTERS.erlang_supported("", "27.0")


def test_the_managed_tags_are_the_sdk_ones() -> None:
    defaults = _defaults()
    assert defaults["cloudfall_rabbitmq_managed_tag"] == MANAGED_TAG
    assert defaults["cloudfall_rabbitmq_owner_tag_prefix"] == OWNER_TAG_PREFIX


# proves: S-023-4
def test_s023_4_a_watermark_above_six_tenths_of_memory_is_refused() -> None:
    refuse = _task(
        "Refuse a memory watermark above six tenths of the server's memory",
        _tasks("main.yml"),
    )
    # 8192 MB: 4915 is the most six tenths allow
    at_limit = _service(JOBS, memoryHighWatermarkMb=4915)
    assert _holds(refuse, _variables(cloudfall_rabbitmq_service=at_limit))
    above = _service(JOBS, memoryHighWatermarkMb=4916)
    assert not _holds(refuse, _variables(cloudfall_rabbitmq_service=above))


# proves: S-023-4
def test_s023_4_every_refusal_comes_before_any_task_that_changes_the_host() -> None:
    names = [task["name"] for task in _tasks("main.yml")]
    assert names[: len(REFUSALS)] == list(REFUSALS)
    read = _task("Read the installed RabbitMQ server package", _tasks("main.yml"))
    assert read["changed_when"] is False
    assert read["check_mode"] is False
    # services.yml refuses it in pre_tasks, before any service role runs
    play = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))[0]
    pre_task = _task(
        "Refuse a RabbitMQ memory watermark above six tenths of the server's memory",
        play["pre_tasks"],
    )
    service = _service(JOBS, memoryHighWatermarkMb=4916)
    variables = {
        **_variables(),
        "cloudfall_services": [service],
        "cloudfall_service_item": service,
    }
    assert not _holds(pre_task, variables)
    assert _template(pre_task["loop"], variables) == [service]
    deploy = _task("Deploy declared RabbitMQ services", play["tasks"])
    assert deploy["ansible.builtin.include_role"]["name"] == "cloudfall_rabbitmq"


def test_three_nodes_and_client_access_are_no_longer_refused() -> None:
    names = [task["name"] for task in _tasks("main.yml")]
    assert "Refuse a cluster this release does not converge" not in names
    assert "Refuse client access this release does not converge" not in names


def test_a_renamed_host_is_refused() -> None:
    # A version change is a rolling change now (#509), and its refusals are
    # proven in test_s023_rabbitmq_version_role.py
    main = _tasks("main.yml")
    variables = _variables()
    hostname = _task("Refuse a host whose hostname is not the declared one", main)
    assert _holds(hostname, variables)
    renamed = {
        **cast("dict[str, object]", variables["ansible_facts"]),
        "hostname": "other",
    }
    assert not _holds(hostname, _variables(ansible_facts=renamed))


# proves: S-023-6
def test_s023_6_the_hash_is_rabbitmqs_and_the_sdks() -> None:
    salt = b"\x90\x8d\xa4\x1d"
    stored = FILTERS.password_hash("test12", salt)
    raw = base64.b64decode(stored)
    assert raw[:4] == salt
    assert raw[4:] == hashlib.sha256(salt + b"test12").digest()
    assert stored == password_hash("test12", salt)
    assert "test12" not in stored


# proves: S-023-6
def test_s023_6_no_secret_reaches_ansible_output() -> None:
    main = _tasks("main.yml")
    assert _task("Install the Erlang cookie the node and its tools share", main)[
        "no_log"
    ]
    accounts = _tasks("accounts.yml")
    for name in ("Read the cluster's definitions", "Plan the virtual hosts and owners"):
        assert _task(name, accounts)["no_log"] is True
    hashes = _task("Set the owners' password hashes", accounts)
    for task in hashes["block"]:
        if task["name"] != "Create the state directory":
            assert task["no_log"] is True
    (remove,) = hashes["always"]
    assert remove["ansible.builtin.file"]["state"] == "absent"


class Node:
    """A replay of the role's virtual host and owner tasks against the stand-in."""

    def __init__(self, tmp_path: Path, state: dict[str, object] | None = None) -> None:
        """Write the stand-in, the controller's password file and the tasks."""
        self.tmp_path = tmp_path
        self.ctl, self.state_path, self.log = write_ctl(
            tmp_path / "node", state if state is not None else fresh_state()
        )
        tasks = yaml.safe_load((ROLE / "tasks" / "accounts.yml").read_text("utf-8"))
        _strip_ownership(tasks)
        self.tasks = tmp_path / "accounts.yml"
        self.tasks.write_text(yaml.safe_dump(tasks), encoding="utf-8")
        self.host_state = tmp_path / "host-state"
        self.controller = tmp_path / "controller"
        self.controller.mkdir()

    def converge(
        self, service: dict[str, object], passwords: dict[str, str]
    ) -> tuple[subprocess.CompletedProcess[str], int]:
        """Run the tasks at -vvv with --diff; return the run and its change count."""
        staged: dict[str, str] = {}
        for owner, value in passwords.items():
            path = self.controller / f"rabbitmq-main-{owner}.password"
            path.write_text(value, encoding="utf-8")
            staged[owner] = str(path)
        cookie = self.controller / "rabbitmq-main.cookie"
        cookie.write_text(COOKIE, encoding="utf-8")
        play = [
            {
                "name": "Replay the virtual host and owner tasks of cloudfall_rabbitmq",
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "vars": {
                    **_defaults(),
                    "cloudfall_server_id": "h1",
                    "cloudfall_rabbitmq_service": service,
                    "cloudfall_rabbitmq_files": {
                        "cookie": str(cookie),
                        "owners": staged,
                    },
                    "cloudfall_rabbitmq_ctl": str(self.ctl),
                    "cloudfall_rabbitmq_state_directory": str(self.host_state),
                },
                "tasks": [
                    {
                        "name": "Converge the virtual hosts and owners",
                        "ansible.builtin.include_tasks": str(self.tasks),
                    }
                ],
            }
        ]
        playbook = self.tmp_path / "accounts-play.yml"
        playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
        binary = shutil.which("ansible-playbook")
        assert binary is not None
        done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
            [binary, "-vvv", "--diff", "--inventory", "localhost,", str(playbook)],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            env={
                **os.environ,
                "ANSIBLE_FILTER_PLUGINS": str(ROLE / "filter_plugins"),
            },
        )
        recap = RECAP.search(done.stdout)
        assert recap is not None, done.stdout + done.stderr
        assert done.returncode == 0, done.stdout + done.stderr
        return done, int(recap[1])

    @property
    def state(self) -> dict[str, Any]:
        """Return the definitions the node holds now."""
        return cast("dict[str, Any]", json.loads(self.state_path.read_text("utf-8")))

    def calls(self) -> list[list[str]]:
        """Return every argv the stand-in was given, and each imported file."""
        return [
            cast("list[str]", json.loads(line))
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]

    def writes(self) -> list[list[str]]:
        """Return the calls that change the node, not the reads."""
        return [
            call
            for call in self.calls()
            if call[0] not in ("--quiet", "export_definitions", "list_queues")
            and call[1:2] != ["export_definitions"]
            and call[1:2] != ["list_queues"]
        ]

    def user(self, name: str) -> dict[str, Any] | None:
        """Return one user of the node, or None."""
        return next(
            (item for item in self.state["users"] if item["name"] == name), None
        )

    def vhost(self, name: str) -> dict[str, Any] | None:
        """Return one virtual host of the node, or None."""
        return next(
            (item for item in self.state["vhosts"] if item["name"] == name), None
        )


def _strip_ownership(tasks: list[dict[str, Any]]) -> None:
    """Drop owner and group, which only root may set, from file tasks."""
    for task in tasks:
        for module in ("ansible.builtin.file", "ansible.builtin.copy"):
            if module in task:
                task[module].pop("owner", None)
                task[module].pop("group", None)
        for key in ("block", "always"):
            if key in task:
                _strip_ownership(task[key])


requires_ansible = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook is not on PATH"
)


def _login(node: Node, user: str, password: str) -> bool:
    """Return whether RabbitMQ would accept ``password`` for ``user``."""
    found = node.user(user)
    if found is None or not found["password_hash"]:
        return False
    raw = base64.b64decode(found["password_hash"])
    return raw[4:] == hashlib.sha256(raw[:4] + password.encode()).digest()


# proves: S-023-3
# proves: S-023-5
@requires_ansible
def test_s023_5_a_fresh_node_gets_its_vhost_and_owner_and_loses_guest(
    tmp_path: Path,
) -> None:
    node = Node(tmp_path)

    _, changed = node.converge(_service(JOBS), {"jobs": PASSWORD})

    assert changed > 0
    # guest and / are gone (S-023-3)
    assert node.user("guest") is None
    assert node.vhost("/") is None
    # jobs logs in with its password, and its vhost defaults to quorum queues
    assert _login(node, "jobs", PASSWORD)
    assert not _login(node, "guest", "guest")
    jobs = node.vhost("jobs")
    assert jobs is not None
    assert jobs["metadata"]["default_queue_type"] == "quorum"
    assert set(jobs["metadata"]["tags"]) == {MANAGED_TAG, f"{OWNER_TAG_PREFIX}jobs"}
    # Everything in its own vhost, nothing elsewhere, and no tag
    assert node.state["permissions"] == [
        {
            "user": "jobs",
            "vhost": "jobs",
            "configure": ".*",
            "write": ".*",
            "read": ".*",
        }
    ]
    user = node.user("jobs")
    assert user is not None
    assert user["tags"] == []


# proves: S-023-4
@requires_ansible
def test_s023_4_a_second_converge_changes_nothing(tmp_path: Path) -> None:
    node = Node(tmp_path)
    node.converge(_service(JOBS), {"jobs": PASSWORD})
    before = node.state
    writes = len(node.writes())

    _, changed = node.converge(_service(JOBS), {"jobs": PASSWORD})

    assert changed == 0
    assert node.state == before
    assert len(node.writes()) == writes


@requires_ansible
def test_a_rotated_password_keeps_the_salt_and_changes_the_hash(
    tmp_path: Path,
) -> None:
    node = Node(tmp_path)
    node.converge(_service(JOBS), {"jobs": PASSWORD})
    first = cast("dict[str, Any]", node.user("jobs"))["password_hash"]

    _, changed = node.converge(_service(JOBS), {"jobs": "rotated-value"})

    second = cast("dict[str, Any]", node.user("jobs"))["password_hash"]
    assert changed > 0
    assert second != first
    assert base64.b64decode(second)[:4] == base64.b64decode(first)[:4]
    assert _login(node, "jobs", "rotated-value")


# proves: S-023-5
@requires_ansible
def test_s023_5_owner_permissions_elsewhere_are_cleared(tmp_path: Path) -> None:
    state = fresh_state()
    state["vhosts"] = [
        *cast("list[dict[str, object]]", state["vhosts"]),
        {"name": "other", "metadata": {"tags": [], "default_queue_type": "classic"}},
    ]
    state["permissions"] = [
        *cast("list[dict[str, object]]", state["permissions"]),
        {
            "user": "jobs",
            "vhost": "other",
            "configure": ".*",
            "write": ".*",
            "read": ".*",
        },
    ]
    node = Node(tmp_path, state)

    node.converge(_service(JOBS), {"jobs": PASSWORD})

    assert [(item["user"], item["vhost"]) for item in node.state["permissions"]] == [
        ("jobs", "jobs")
    ]
    # A vhost Cloudfall did not make is left in place
    assert node.vhost("other") is not None


@requires_ansible
def test_the_default_vhost_holding_queues_is_left_alone(tmp_path: Path) -> None:
    state = fresh_state()
    state["queues"] = {"/": ["orders"]}
    node = Node(tmp_path, state)

    done, _ = node.converge(_service(JOBS), {"jobs": PASSWORD})

    assert node.vhost("/") is not None
    assert node.user("guest") is None
    assert "unmanaged (left as they are)" in done.stdout


# proves: S-023-6
@requires_ansible
def test_s023_6_only_a_hash_reaches_the_node_and_the_file_is_gone(
    tmp_path: Path,
) -> None:
    node = Node(tmp_path)

    done, _ = node.converge(_service(JOBS), {"jobs": PASSWORD})

    imported = [call[1] for call in node.calls() if call[0] == "imported"]
    (definitions,) = imported
    assert PASSWORD not in definitions
    (user,) = json.loads(definitions)["users"]
    assert user["hashing_algorithm"] == "rabbit_password_hashing_sha256"
    assert _login(node, "jobs", PASSWORD)
    # The definitions file is removed after the run
    assert list(node.host_state.iterdir()) == []
    for secret in (PASSWORD, COOKIE, user["password_hash"]):
        assert secret not in done.stdout
        assert secret not in done.stderr
        assert all(
            secret not in " ".join(call)
            for call in node.writes()
            if call[0] != "imported"
        )


# proves: S-023-7
@requires_ansible
def test_s023_7_a_removed_vhost_is_kept_and_its_owner_locked_out(
    tmp_path: Path,
) -> None:
    reports: dict[str, object] = {
        "name": "reports",
        "owner": "reports",
        "passwordRef": {
            "environment": "production",
            "path": "/services/rabbitmq-main",
            "key": "REPORTS_PASSWORD",
        },
    }
    node = Node(tmp_path)
    node.converge(_service(JOBS, reports), {"jobs": PASSWORD, "reports": "r-value"})
    state = node.state
    state["queues"] = {"reports": ["daily"]}
    state["users"].append(
        {
            "name": "ops",
            "password_hash": password_hash("ops-value", b"\x05\x06\x07\x08"),
            "hashing_algorithm": "rabbit_password_hashing_sha256",
            "tags": [],
            "limits": {},
        }
    )
    node.state_path.write_text(json.dumps(state), encoding="utf-8")

    done, changed = node.converge(_service(JOBS), {"jobs": PASSWORD})

    assert changed > 0
    # The vhost and its queues stay; deleting is never a converge's side effect
    assert node.vhost("reports") is not None
    assert node.state["queues"] == {"reports": ["daily"]}
    assert ["delete_vhost", "reports"] not in [call[:2] for call in node.writes()]
    # The owner can no longer log in, and is not deleted
    assert node.user("reports") is not None
    assert not _login(node, "reports", "r-value")
    # A user made by hand is left alone
    assert _login(node, "ops", "ops-value")
    assert "virtual hosts reports" in done.stdout
    assert "users ops" in done.stdout
    # And the next converge changes nothing
    _, again = node.converge(_service(JOBS), {"jobs": PASSWORD})
    assert again == 0


def test_backup_runs_on_the_services_home_as_root() -> None:
    (select, play) = yaml.safe_load((PLAYBOOKS / "backup.yml").read_text("utf-8"))
    run = _task(
        "Execute the requested backup action as the service user", play["tasks"]
    )
    assert "'rabbitmq': 'root'" in run["become_user"]
    add = _task("Back up on the service's server only", select["tasks"])
    service = _service(JOBS, nodes=["h1", "h2", "h3"])
    hostvars = {
        server: {"cloudfall_server_id": server, "cloudfall_services": [service]}
        for server in ("h1", "h2", "h3")
    }
    templar = Templar(
        loader=DataLoader(),
        variables={"hostvars": hostvars, "cloudfall_backup_service": "rabbitmq-main"},
    )
    selected = [
        server
        for server in ("h1", "h2", "h3")
        if templar.evaluate_conditional(
            trust_as_template(
                add["when"].replace("cloudfall_backup_server_item", f"'{server}'")
            )
        )
    ]
    assert selected == ["h1"]


def test_services_yml_refuses_a_kind_it_has_no_role_for() -> None:
    plays = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))
    play = plays[0]
    refuse = _task(
        "Refuse a service of a kind this playbook does not converge", play["pre_tasks"]
    )
    known = {"cloudfall_services": [_service(JOBS), {"serviceKind": "postgresql"}]}
    assert _holds(refuse, {**_variables(), **known})
    unknown = {"cloudfall_services": [{"serviceKind": "kafka"}]}
    assert not _holds(refuse, {**_variables(), **unknown})
    # Kibana's role runs in a block of the play after the nodes' (S-024)
    roles = {
        task["ansible.builtin.include_role"]["name"]
        for each in plays
        for listed in each["tasks"]
        for task in (listed, *listed.get("block", []))
        if "ansible.builtin.include_role" in task
    }
    kinds = set(re.findall(r"'(\w+)'", refuse["ansible.builtin.assert"]["that"][0])) - {
        "serviceKind"
    }
    assert "rabbitmq" in kinds
    assert {f"cloudfall_{kind}" for kind in kinds} <= roles
