"""RabbitMQ definitions exports and the restore drill (S-023-16, S-023-17, #511).

The rendered ``cloudfall-rabbitmq-backup`` and ``cloudfall-rabbitmq-restore-check``
run for real against stand-ins: a ``rabbitmqctl`` of the cluster that keeps
its definitions in a file, and a scratch ``rabbitmq-server`` that imports the
file its configuration names at boot, writes its pid file, listens (in the
stand-in ``ss``) on the ports it was given, and runs until it is stopped.
``backup.yml`` runs on localhost with the scripts rendered for them, so the
receipts are the playbook's own. What only a live host proves (that
RabbitMQ 4.x imports an export at boot without a default user, the real
listeners of the scratch node and its epmd, the Debian paths of the tools)
is listed in the pull request as unverified.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from cloudfall.rabbitmq import password_hash
from cloudfall.validation import SchemaCatalog
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_rabbitmq"
PLAYBOOKS = ROOT / "engine" / "ansible" / "playbooks"
SCHEMAS = ROOT / "config" / "schemas" / "v1"
SCRATCH_NODE = "cloudfall_restore_check@localhost"
DAY = 24 * 60 * 60
JOBS_HASH = password_hash("s3cr3t-jobs-value", b"\x11\x22\x33\x44")
OPS_HASH = password_hash("s3cr3t-ops-value", b"\x55\x66\x77\x88")

STAND_IN = r"""#!__PYTHON__
import json
import os
import signal
import sys
import time
from pathlib import Path

WORLD = Path(__WORLD__)
CONTROL = json.loads((WORLD / "control.json").read_text(encoding="utf-8"))
LISTENERS = WORLD / "listeners"
tool = Path(sys.argv[0]).name
scratch = Path(sys.argv[0]).parent.name == "lib"
argv = [item for item in sys.argv[1:] if item != "--quiet"]
with (WORLD / "calls.log").open("a", encoding="utf-8") as log:
    log.write(json.dumps({
        "tool": tool,
        "scratch": scratch,
        "argv": argv,
        "node": os.environ.get("RABBITMQ_NODENAME"),
        "home": os.environ.get("HOME"),
        "epmd": os.environ.get("ERL_EPMD_PORT"),
    }) + "\n")


def lines():
    if not LISTENERS.exists():
        return []
    return LISTENERS.read_text(encoding="utf-8").splitlines()


def keep(predicate):
    LISTENERS.write_text(
        "".join(line + "\n" for line in lines() if predicate(line)), encoding="utf-8"
    )


def data():
    return Path(os.environ["RABBITMQ_MNESIA_BASE"])


def server():
    config = Path(os.environ["RABBITMQ_CONFIG_FILE"]).read_text(encoding="utf-8")
    settings = dict(
        line.split(" = ", 1) for line in config.splitlines() if " = " in line
    )
    cookie = Path(os.environ["HOME"]) / ".erlang.cookie"
    (WORLD / "scratch-start.json").write_text(json.dumps({
        "settings": settings,
        "environment": {
            key: value
            for key, value in os.environ.items()
            if key.startswith(("RABBITMQ_", "ERL_", "HOME"))
        },
        "cookie": cookie.read_text(encoding="utf-8"),
        "cookie_mode": oct(cookie.stat().st_mode & 0o777),
        "pid": os.getpid(),
    }), encoding="utf-8")
    if CONTROL.get("boot_fails"):
        print("error during startup: definitions import failed", file=sys.stderr)
        return 1
    definitions = json.loads(
        Path(settings["definitions.local.path"]).read_text(encoding="utf-8")
    )
    drop = CONTROL.get("drop")
    if drop:
        definitions[drop].pop(0)
    if CONTROL.get("rehash"):
        definitions["users"][0]["password_hash"] = "rehashed"
    data().mkdir(exist_ok=True)
    plant = CONTROL.get("plant")
    if plant:
        # The rabbitmq user owns the scratch directory: it can put a link
        # where root then writes the export the drill reads back
        (data().parent / "restored.json").symlink_to(plant)
    (data() / "state.json").write_text(json.dumps(definitions), encoding="utf-8")
    pid = os.getpid()
    Path(os.environ["RABBITMQ_PID_FILE"]).write_text(f"{pid}\n", encoding="utf-8")
    amqp = settings["listeners.tcp.default"]
    distribution = os.environ["RABBITMQ_DIST_PORT"]
    epmd = os.environ["ERL_EPMD_PORT"]
    with LISTENERS.open("a", encoding="utf-8") as handle:
        handle.write(
            f'LISTEN 0 128 {amqp} 0.0.0.0:* users:(("beam.smp",pid={pid},fd=1))\n'
            f'LISTEN 0 128 127.0.0.1:{distribution} 0.0.0.0:* '
            f'users:(("beam.smp",pid={pid},fd=2))\n'
            f'LISTEN 0 128 127.0.0.1:{epmd} 0.0.0.0:* users:(("epmd",fd=3))\n'
        )
    if CONTROL.get("join"):
        CONTROL["members"].append(os.environ["RABBITMQ_NODENAME"])
        (WORLD / "control.json").write_text(json.dumps(CONTROL), encoding="utf-8")

    def stop(*_):
        keep(lambda line: f"pid={pid}," not in line)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    while not (data() / "stop").exists():
        time.sleep(0.05)
    stop()


def scratch_ctl():
    command = argv[0]
    state = data() / "state.json"
    if command == "await_startup":
        deadline = time.time() + float(argv[argv.index("--timeout") + 1])
        while time.time() < deadline:
            if state.exists():
                return 0
            time.sleep(0.05)
        return 69
    if not state.exists():
        print("Error: unable to perform an operation on node", file=sys.stderr)
        return 69
    if command == "export_definitions":
        print(state.read_text(encoding="utf-8"))
        return 0
    if command == "stop":
        (data() / "stop").touch()
        if len(argv) > 1:
            pid = int(Path(argv[1]).read_text(encoding="utf-8"))
            for _ in range(200):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return 0
                time.sleep(0.05)
        return 0
    return 64


def ctl():
    command = argv[0]
    if command == "export_definitions":
        if CONTROL.get("export_garbage"):
            print("Error: not a node")
            return 0
        print((WORLD / "cluster.json").read_text(encoding="utf-8"))
        return 0
    if command == "cluster_status":
        members = CONTROL["members"]
        print(json.dumps({"disk_nodes": members, "running_nodes": members}))
        return 0
    return 64


if tool == "rabbitmq-server":
    sys.exit(server())
if tool == "rabbitmqctl":
    sys.exit(scratch_ctl() if scratch else ctl())
if tool == "epmd":
    if not CONTROL.get("epmd_stays"):
        port = os.environ["ERL_EPMD_PORT"]
        keep(lambda line: f":{port} " not in line)
    sys.exit(0)
if tool == "ss":
    for line in lines():
        print(line)
    sys.exit(0)
sys.exit(64)
"""

RUNUSER = r"""#!__PYTHON__
# runuser -u <user> -- <command>...: the rabbitmq user, who can't write in a
# directory the world's "protected" file lists, as the kernel would refuse it.
# The entry a path argument names is the parent's, so a link the command
# would follow at its parent is resolved and a link it names is not.
import os
import sys
from pathlib import Path

WORLD = Path(__WORLD__)
user, command = sys.argv[2], sys.argv[4:]
with (WORLD / "runuser.log").open("a", encoding="utf-8") as log:
    log.write(user + "\n")
protected = WORLD / "protected"
roots = protected.read_text(encoding="utf-8").split() if protected.exists() else []
for argument in command[1:]:
    if not argument.startswith("/"):
        continue
    parent = os.path.realpath(os.path.dirname(argument))
    if any(parent == root or parent.startswith(root + os.sep) for root in roots):
        print(f"{command[0]}: {argument}: Permission denied", file=sys.stderr)
        sys.exit(1)
os.execvp(command[0], command)
"""

# head, with the rabbitmq user's move in the window after the scratch tree is
# made: the directory is renamed and a link to "target" put in its place.
SWAP_HEAD = """#!/bin/sh
if [ -f "__WORLD__/swap" ] && [ -d "__SCRATCH__" ] && [ ! -L "__SCRATCH__" ]; then
  target="$(cat "__WORLD__/swap")"
  rm "__WORLD__/swap"
  mv "__SCRATCH__" "__WORLD__/moved"
  ln -s "${target}" "__SCRATCH__"
fi
exec /usr/bin/head "$@"
"""


def _cluster() -> dict[str, Any]:
    """Return what the cluster holds, in the shape of ``export_definitions``."""
    return {
        "rabbit_version": "4.4.1",
        "users": [
            {
                "name": "jobs",
                "password_hash": JOBS_HASH,
                "hashing_algorithm": "rabbit_password_hashing_sha256",
                "tags": [],
                "limits": {},
            },
            {
                "name": "ops",
                "password_hash": OPS_HASH,
                "hashing_algorithm": "rabbit_password_hashing_sha256",
                "tags": [],
                "limits": {},
            },
        ],
        "vhosts": [
            {"name": "jobs", "metadata": {"default_queue_type": "quorum"}},
            {"name": "reports", "metadata": {"default_queue_type": "quorum"}},
        ],
        "permissions": [
            {
                "user": "jobs",
                "vhost": "jobs",
                "configure": ".*",
                "write": ".*",
                "read": ".*",
            }
        ],
        "parameters": [],
        "global_parameters": [{"name": "cluster_name", "value": "rabbit@h1"}],
        "policies": [
            {
                "vhost": "jobs",
                "name": "ttl",
                "pattern": "^orders$",
                "apply-to": "queues",
                "definition": {"message-ttl": 60000},
                "priority": 0,
            }
        ],
        "queues": [
            {
                "name": "orders",
                "vhost": "jobs",
                "durable": True,
                "auto_delete": False,
                "arguments": {"x-queue-type": "quorum"},
            },
            {
                "name": "mail",
                "vhost": "jobs",
                "durable": True,
                "auto_delete": False,
                "arguments": {"x-queue-type": "quorum"},
            },
            {
                "name": "daily",
                "vhost": "reports",
                "durable": True,
                "auto_delete": False,
                "arguments": {"x-queue-type": "quorum"},
            },
        ],
        "exchanges": [
            {
                "name": "events",
                "vhost": "jobs",
                "type": "topic",
                "durable": True,
                "auto_delete": False,
                "internal": False,
                "arguments": {},
            }
        ],
        "bindings": [
            {
                "source": "events",
                "vhost": "jobs",
                "destination": "orders",
                "destination_type": "queue",
                "routing_key": "order.*",
                "arguments": {},
            },
            {
                "source": "events",
                "vhost": "jobs",
                "destination": "mail",
                "destination_type": "queue",
                "routing_key": "mail.#",
                "arguments": {},
            },
        ],
    }


def _service(**backup: object) -> dict[str, Any]:
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
            "vhosts": [
                {
                    "name": "jobs",
                    "owner": "jobs",
                    "passwordRef": {
                        "environment": "production",
                        "path": "/services/rabbitmq-main",
                        "key": "JOBS_PASSWORD",
                    },
                }
            ],
        },
        "backup": {
            "directory": "/var/backups/cloudfall/rabbitmq-main",
            "onCalendar": "*-*-* 02:50:00 UTC",
            "restoreCheckOnCalendar": "Sun *-*-* 05:40:00 UTC",
            "retentionDays": 14,
            **backup,
        },
        "metrics": {"enabled": True},
    }


def _defaults() -> dict[str, Any]:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    assert isinstance(defaults, dict)
    return cast("dict[str, Any]", defaults)


def _render(template: str, **variables: object) -> str:
    environment = Environment(
        loader=FileSystemLoader(ROLE / "templates"),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - shell scripts are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    return environment.get_template(template).render(
        {**_defaults(), "cloudfall_rabbitmq_service": _service(), **variables}
    )


class Host:
    """A backup directory, the stand-in cluster and scratch node, the scripts."""

    def __init__(self, tmp_path: Path, free_mb: int = 4096) -> None:
        """Write the stand-ins and render both scripts for them."""
        self.tmp_path = tmp_path
        self.world = tmp_path / "world"
        self.world.mkdir()
        self.backups = tmp_path / "backups"
        self.backups.mkdir()
        self.backups.chmod(0o700)
        self.scratch = tmp_path / "scratch"
        self.tools = tmp_path / "bin"
        self.lib = self.tools / "lib"
        self.lib.mkdir(parents=True)
        self.control(members=["rabbit@h1"])
        (self.world / "cluster.json").write_text(json.dumps(_cluster()), "utf-8")
        (self.world / "listeners").write_text(
            'LISTEN 0 128 127.0.0.1:5672 0.0.0.0:* users:(("beam.smp",pid=1,fd=1))\n'
            'LISTEN 0 128 127.0.0.1:4369 0.0.0.0:* users:(("epmd",pid=2,fd=3))\n',
            encoding="utf-8",
        )
        (self.world / "calls.log").touch()
        stand_in = STAND_IN.replace("__PYTHON__", sys.executable).replace(
            "__WORLD__", repr(str(self.world))
        )
        for path in (
            self.tools / "rabbitmqctl",
            self.tools / "epmd",
            self.tools / "ss",
            self.lib / "rabbitmqctl",
            self.lib / "rabbitmq-server",
        ):
            path.write_text(stand_in, encoding="utf-8")
            path.chmod(0o700)
        runuser = self.tools / "runuser"
        runuser.write_text(
            RUNUSER.replace("__PYTHON__", sys.executable).replace(
                "__WORLD__", repr(str(self.world))
            ),
            "utf-8",
        )
        runuser.chmod(0o700)
        (self.tools / "python3").symlink_to(sys.executable)
        self.meminfo = tmp_path / "meminfo"
        self.meminfo.write_text(
            f"MemTotal: 8000000 kB\nMemAvailable: {free_mb * 1024} kB\n", "utf-8"
        )
        variables: dict[str, object] = {
            "cloudfall_rabbitmq_service": _service(directory=str(self.backups)),
            "cloudfall_rabbitmq_ctl": str(self.tools / "rabbitmqctl"),
            "cloudfall_rabbitmq_bin_directory": str(self.lib),
            "cloudfall_rabbitmq_scratch_directory": str(self.scratch),
            "cloudfall_rabbitmq_user": getpass.getuser(),
            "cloudfall_rabbitmq_meminfo_path": str(self.meminfo),
        }
        self.scripts = tmp_path / "sbin"
        self.scripts.mkdir()
        for name in ("backup", "restore-check"):
            script = self.scripts / f"cloudfall-rabbitmq-{name}"
            script.write_text(_render(f"{name}.sh.j2", **variables), "utf-8")
            script.chmod(0o700)

    def swap_scratch_for_link(self, target: Path) -> None:
        """Have the rabbitmq user swap the fresh scratch tree for a link.

        ``target`` is a directory root can write and that user can't.
        """
        (self.world / "swap").write_text(str(target), "utf-8")
        (self.world / "protected").write_text(os.path.realpath(target), "utf-8")
        head = self.tools / "head"
        head.write_text(
            SWAP_HEAD.replace("__WORLD__", str(self.world)).replace(
                "__SCRATCH__", str(self.scratch)
            ),
            "utf-8",
        )
        head.chmod(0o700)

    def control(self, **values: object) -> None:
        """Set how the stand-ins behave."""
        path = self.world / "control.json"
        current = json.loads(path.read_text("utf-8")) if path.exists() else {}
        path.write_text(json.dumps({**current, **values}), encoding="utf-8")

    @property
    def path(self) -> str:
        """Return the PATH the scripts run with: the stand-ins first."""
        return f"{self.tools}:/usr/bin:/bin:/usr/sbin:/sbin"

    def run(self, name: str) -> subprocess.CompletedProcess[str]:
        """Run one rendered script with the stand-ins first on PATH."""
        return subprocess.run(  # noqa: S603 - the rendered script under test.
            [
                "/usr/bin/env",
                f"PATH={self.path}",
                "bash",
                str(self.scripts / f"cloudfall-rabbitmq-{name}"),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def backup(self) -> subprocess.CompletedProcess[str]:
        """Run cloudfall-rabbitmq-backup and require it to pass."""
        done = self.run("backup")
        assert done.returncode == 0, done.stdout + done.stderr
        return done

    def exports(self) -> list[Path]:
        """Return the exports in the backup directory, oldest first."""
        return sorted(self.backups.glob("definitions-*.json"))

    def calls(self) -> list[dict[str, Any]]:
        """Return every stand-in invocation."""
        return [
            cast("dict[str, Any]", json.loads(line))
            for line in (self.world / "calls.log").read_text("utf-8").splitlines()
        ]

    def listeners(self) -> str:
        """Return what the stand-in ss reports."""
        return (self.world / "listeners").read_text(encoding="utf-8")

    def scratch_listeners(self) -> list[str]:
        """Return the stand-in listeners on the scratch node's ports."""
        ports = cast("dict[str, int]", _defaults()["cloudfall_rabbitmq_scratch_ports"])
        return [
            line
            for line in self.listeners().splitlines()
            if any(f":{port} " in line for port in ports.values())
        ]

    def started(self) -> dict[str, Any]:
        """Return what the scratch node was started with."""
        return cast(
            "dict[str, Any]",
            json.loads((self.world / "scratch-start.json").read_text("utf-8")),
        )


def _age(path: Path, days: int) -> None:
    then = time.time() - days * DAY
    os.utime(path, (then, then))


def _secrets_absent(*texts: str) -> None:
    for text in texts:
        for secret in (JOBS_HASH, OPS_HASH):
            assert secret not in text


# proves: S-023-16
def test_s023_16_backup_writes_one_export_0600_and_names_no_message(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)

    done = host.backup()

    (export,) = host.exports()
    assert stat.S_IMODE(export.stat().st_mode) == 0o600
    # The script runs as root on the server (backup.yml, the unit): the file
    # is its runner's, whoever that is here
    assert export.stat().st_uid == os.getuid()
    assert json.loads(export.read_text("utf-8")) == _cluster()
    assert not list(host.backups.glob("*.part"))
    lines = done.stdout.splitlines()
    assert lines[-1] == (
        f"backed up the definitions of rabbitmq-main: {export} (2 virtual hosts, "
        "2 users, 3 queues, 1 exchanges, 2 bindings, 1 policies)"
    )
    assert lines[-2].startswith("no message is in this backup")
    _secrets_absent(done.stdout, done.stderr)
    (export_call,) = [call for call in host.calls() if call["tool"] == "rabbitmqctl"]
    assert export_call["argv"] == ["export_definitions", "-", "--format", "json"]
    assert not export_call["scratch"]


# proves: S-023-16
def test_s023_16_an_export_without_a_declared_vhost_is_refused(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    cluster = _cluster()
    cluster["vhosts"] = [{"name": "reports", "metadata": {}}]
    (host.world / "cluster.json").write_text(json.dumps(cluster), "utf-8")

    done = host.run("backup")

    assert done.returncode != 0
    assert "names no virtual host jobs" in done.stderr
    assert host.exports() == []
    assert list(host.backups.iterdir()) == []


def test_an_export_that_is_not_json_is_refused(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.control(export_garbage=True)

    done = host.run("backup")

    assert done.returncode != 0
    assert "the export of the definitions is not JSON" in done.stderr
    assert list(host.backups.iterdir()) == []


def _old_export(host: Host, stamp: str, days: int) -> Path:
    path = host.backups / f"definitions-{stamp}.json"
    path.write_text("{}", encoding="utf-8")
    _age(path, days)
    return path


# proves: S-023-17
def test_s023_17_exports_older_than_retention_days_are_gone_after_the_next_backup(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    old = _old_export(host, "20261001T025000Z", 20)
    recent = _old_export(host, "20261110T025000Z", 5)
    # Files the backup did not write stay, however old
    foreign = [
        host.backups / "definitions-manual.json",
        host.backups / "definitions-20261001T025000Z.json.keep",
        host.backups / "notes.json",
    ]
    for path in foreign:
        path.write_text("{}", encoding="utf-8")
        _age(path, 30)

    done = host.backup()

    assert not old.exists()
    assert recent.exists()
    assert all(path.exists() for path in foreign)
    assert len([p for p in host.exports() if p.name[12:13].isdigit()]) == 2
    assert "removed definitions-20261001T025000Z.json, older than 14 days" in (
        done.stdout
    )
    # The receipt keeps the last lines: they name the export
    assert done.stdout.splitlines()[-1].startswith("backed up the definitions")


# proves: S-023-17
def test_s023_17_the_newest_export_is_kept_even_when_older_than_retention_days(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    # A newest export, by its name, older than retentionDays (the clock moved)
    newest = _old_export(host, "20991231T025000Z", 30)
    older = _old_export(host, "20261001T025000Z", 40)

    host.backup()

    assert newest.exists()
    assert not older.exists()


# proves: S-023-17
def test_s023_17_a_failed_backup_deletes_no_export(tmp_path: Path) -> None:
    host = Host(tmp_path)
    old = _old_export(host, "20261001T025000Z", 40)
    host.control(export_garbage=True)

    done = host.run("backup")

    assert done.returncode != 0
    assert old.exists()


# proves: S-023-16
def test_s023_16_the_drill_imports_the_newest_export_into_a_scratch_node(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    _old_export(host, "20261001T025000Z", 3)
    host.backup()
    newest = host.exports()[-1]

    done = host.run("restore-check")

    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.splitlines()[-1] == (
        f"restore check passed for rabbitmq-main: {newest} (2 virtual hosts, "
        "2 users, 3 queues, 1 exchanges, 2 bindings, 1 policies, as in the export)"
    )
    _secrets_absent(done.stdout, done.stderr)
    started = host.started()
    environment = started["environment"]
    settings = started["settings"]
    # A node of its own: name, cookie, data directory, configuration, ports
    assert environment["RABBITMQ_NODENAME"] == SCRATCH_NODE
    assert environment["HOME"] == f"{host.scratch}/home"
    assert environment["RABBITMQ_MNESIA_BASE"] == f"{host.scratch}/data"
    assert environment["RABBITMQ_CONF_ENV_FILE"] == f"{host.scratch}/rabbitmq-env.conf"
    assert environment["RABBITMQ_CONFIG_FILES"] == f"{host.scratch}/conf.d"
    assert environment["RABBITMQ_ENABLED_PLUGINS_FILE"] == (
        f"{host.scratch}/enabled_plugins"
    )
    assert (environment["RABBITMQ_DIST_PORT"], environment["ERL_EPMD_PORT"]) == (
        "45673",
        "45674",
    )
    assert environment["ERL_EPMD_ADDRESS"] == "127.0.0.1"
    assert started["cookie_mode"] == "0o400"
    assert len(started["cookie"]) == 40
    # Loopback only, no node list, a small watermark, the export imported at boot
    assert settings == {
        "listeners.tcp.default": "127.0.0.1:45672",
        "listeners.ssl": "none",
        "distribution.listener.interface": "127.0.0.1",
        "vm_memory_high_watermark.absolute": "256MiB",
        "disk_free_limit.absolute": "50MB",
        "definitions.import_backend": "local_filesystem",
        "definitions.local.path": f"{host.scratch}/definitions.json",
    }
    # Every tool of the scratch node ran as the rabbitmq user, with its env
    scratch_calls = [call for call in host.calls() if call["scratch"]]
    assert scratch_calls
    assert all(call["node"] == SCRATCH_NODE for call in scratch_calls)
    assert all(call["epmd"] == "45674" for call in scratch_calls)
    users = (host.world / "runuser.log").read_text("utf-8").split()
    assert set(users) == {getpass.getuser()}
    # The cluster was asked, and only read
    cluster_calls = [
        call["argv"][0]
        for call in host.calls()
        if call["tool"] == "rabbitmqctl" and not call["scratch"]
    ]
    assert cluster_calls == ["export_definitions", "cluster_status"]
    # Nothing of the scratch node is left: no directory, no listener, no process
    assert not host.scratch.exists()
    assert host.scratch_listeners() == []
    with pytest.raises(ProcessLookupError):
        os.kill(started["pid"], 0)
    assert f"the scratch node {SCRATCH_NODE} is stopped and {host.scratch} removed" in (
        done.stdout
    )


def _drill_fails(host: Host, message: str) -> subprocess.CompletedProcess[str]:
    done = host.run("restore-check")
    assert done.returncode != 0, done.stdout
    assert message in done.stderr, done.stderr
    assert "restore check passed" not in done.stdout
    assert not host.scratch.exists()
    assert host.scratch_listeners() == []
    _secrets_absent(done.stdout, done.stderr)
    return done


# proves: S-023-16
def test_s023_16_with_no_export_the_drill_fails_and_says_so(tmp_path: Path) -> None:
    host = Host(tmp_path)
    (host.backups / "definitions-manual.json").write_text("{}", "utf-8")

    _drill_fails(
        host,
        f"no export found for rabbitmq-main in {host.backups}: run cloudfall "
        "backup run rabbitmq-main first",
    )

    # No scratch node was started
    assert not (host.world / "scratch-start.json").exists()


# proves: S-023-16
@pytest.mark.parametrize(
    ("drop", "named"),
    [
        ("vhosts", "virtual hosts in the export, not in the scratch node: jobs"),
        ("users", "users in the export, not in the scratch node: jobs"),
        ("queues", "queues in the export, not in the scratch node: jobs/orders"),
        ("exchanges", "exchanges in the export, not in the scratch node: jobs/events"),
        (
            "bindings",
            (
                "bindings in the export, not in the scratch node: jobs/events -> "
                "queue orders (order.*)"
            ),
        ),
        ("policies", "policies in the export, not in the scratch node: jobs/ttl"),
    ],
)
def test_s023_16_a_difference_fails_the_drill_and_removes_the_scratch_node(
    tmp_path: Path, drop: str, named: str
) -> None:
    host = Host(tmp_path)
    host.backup()
    host.control(drop=drop)

    done = _drill_fails(host, named)

    assert "restore check failed for rabbitmq-main" in done.stderr


def test_a_password_hash_that_differs_fails_and_is_not_printed(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    host.backup()
    host.control(rehash=True)

    _drill_fails(host, "users whose password hash differs: jobs")


# proves: S-023-16
def test_s023_16_a_node_that_does_not_boot_fails_and_leaves_nothing(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    host.backup()
    host.control(boot_fails=True)

    _drill_fails(host, f"the scratch node {SCRATCH_NODE} did not start")


# proves: S-023-16
def test_s023_16_a_scratch_node_in_the_cluster_fails_the_drill(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    host.backup()
    host.control(join=True)

    _drill_fails(host, "is a member of the cluster")


# proves: S-023-16
def test_s023_16_a_listener_left_by_the_scratch_node_fails_the_drill(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    host.backup()
    host.control(epmd_stays=True)

    done = host.run("restore-check")

    assert done.returncode != 0
    assert "left a listener" in done.stderr
    assert ":45674" in done.stderr
    assert "restore check passed" not in done.stdout


def test_root_writes_nothing_through_a_link_in_the_scratch_directory(
    tmp_path: Path,
) -> None:
    """The scratch directory is the rabbitmq user's; root writes none of it."""
    host = Host(tmp_path)
    host.backup()
    victim = tmp_path / "victim"
    victim.write_text("untouched", "utf-8")
    host.control(plant=str(victim))

    done = host.run("restore-check")

    assert done.returncode == 0, done.stdout + done.stderr
    assert victim.read_text("utf-8") == "untouched"
    assert not host.scratch.exists()


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_root_writes_nothing_through_a_scratch_directory_swapped_for_a_link(
    tmp_path: Path,
) -> None:
    """The rabbitmq user owns the scratch directory's parent (#544).

    It renames the fresh tree and links its name to a directory only root can
    write, which holds the tree's subdirectories so a write by path lands.
    """
    host = Host(tmp_path)
    host.backup()
    elsewhere = tmp_path / "elsewhere"
    for name in ("home", "conf.d", "data", "log"):
        (elsewhere / name).mkdir(parents=True)
    before = _tree(elsewhere)
    host.swap_scratch_for_link(elsewhere)

    done = host.run("restore-check")

    assert done.returncode != 0, done.stdout
    assert "Permission denied" in done.stderr, done.stderr
    assert "restore check passed" not in done.stdout
    assert _tree(elsewhere) == before
    # The link is removed, not what it points at
    assert not host.scratch.exists()
    assert not host.scratch.is_symlink()
    assert not (host.world / "scratch-start.json").exists()
    _secrets_absent(done.stdout, done.stderr)


def test_a_dangling_link_left_at_the_scratch_path_is_removed_first(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    host.backup()
    host.scratch.symlink_to(tmp_path / "gone")

    done = host.run("restore-check")

    assert done.returncode == 0, done.stdout + done.stderr
    assert not host.scratch.is_symlink()
    assert not host.scratch.exists()
    assert not (tmp_path / "gone").exists()


def test_the_drill_refuses_a_server_short_of_memory(tmp_path: Path) -> None:
    host = Host(tmp_path, free_mb=200)
    host.backup()

    _drill_fails(
        host,
        "the server has 200 MB of free memory, less than the 256 MB the scratch "
        "node takes",
    )
    assert not (host.world / "scratch-start.json").exists()


def test_the_drill_refuses_when_a_scratch_port_is_taken(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.backup()
    with (host.world / "listeners").open("a", encoding="utf-8") as handle:
        handle.write(
            'LISTEN 0 128 127.0.0.1:45672 0.0.0.0:* users:(("nc",pid=7,fd=3))\n'
        )

    done = host.run("restore-check")

    assert done.returncode != 0
    assert "the scratch node's ports are taken" in done.stderr
    assert not (host.world / "scratch-start.json").exists()


def test_a_scratch_directory_a_stopped_drill_left_is_removed_first(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    host.backup()
    (host.scratch / "data").mkdir(parents=True)
    (host.scratch / "data" / "leftover").write_text("x", "utf-8")

    done = host.run("restore-check")

    assert done.returncode == 0, done.stderr
    assert "removing the scratch node a stopped restore drill left" in done.stdout
    assert not host.scratch.exists()


def _playbook(
    host: Host, action: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Run ``backup.yml`` on localhost with the scripts rendered for the host."""
    playbooks = host.tmp_path / "playbooks"
    if not playbooks.exists():
        shutil.copytree(PLAYBOOKS, playbooks)
        backup = playbooks / "backup.yml"
        text = backup.read_text(encoding="utf-8")
        assert text.count("/usr/local/sbin/") == 1
        backup.write_text(text.replace("/usr/local/sbin/", f"{host.scripts}/"), "utf-8")
    inventory = host.tmp_path / "inventory.json"
    service = _service(directory=str(host.backups))
    inventory.write_text(
        json.dumps(
            {
                "all": {
                    "children": {
                        "cloudfall_servers": {
                            "hosts": {
                                "h1": {
                                    "ansible_connection": "local",
                                    "ansible_become": False,
                                    "ansible_host": "localhost",
                                    "ansible_python_interpreter": sys.executable,
                                    "cloudfall_server_id": "h1",
                                    "cloudfall_services": [service],
                                }
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    receipts = host.tmp_path / "receipts"
    binary = shutil.which("ansible-playbook")
    assert binary is not None
    done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [
            binary,
            "-v",
            "--inventory",
            str(inventory),
            "--extra-vars",
            json.dumps(
                {
                    "cloudfall_backup_service": "rabbitmq-main",
                    "cloudfall_backup_action": action,
                    "cloudfall_backup_receipt_directory": str(receipts),
                }
            ),
            str(playbooks / "backup.yml"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={
            **os.environ,
            "ANSIBLE_STDOUT_CALLBACK": "default",
            "PATH": f"{host.path}:{os.environ['PATH']}",
        },
    )
    assert done.returncode == 0, done.stdout + done.stderr
    receipt = cast(
        "dict[str, Any]",
        json.loads(
            (receipts / f"rabbitmq-main-{action}.json").read_text(encoding="utf-8")
        ),
    )
    SchemaCatalog(SCHEMAS).validate_named("backup-receipt.schema.json", receipt)
    return done, receipt


requires_ansible = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook is not on PATH"
)


# proves: S-023-16
@requires_ansible
def test_s023_16_backup_run_and_verify_write_receipts_that_hold_no_message(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)

    done, receipt = _playbook(host, "backup")

    spec = receipt["spec"]
    assert (spec["service"], spec["server"], spec["action"]) == (
        "rabbitmq-main",
        "h1",
        "backup",
    )
    assert spec["holdsMessages"] is False
    assert "backed up the definitions of rabbitmq-main" in spec["summary"]
    assert "no message is in this backup" in spec["summary"]
    (export,) = host.exports()
    assert stat.S_IMODE(export.stat().st_mode) == 0o600

    verified, check = _playbook(host, "restore-check")

    assert check["spec"]["action"] == "restore-check"
    assert check["spec"]["holdsMessages"] is False
    assert "restore check passed for rabbitmq-main" in check["spec"]["summary"]
    assert not host.scratch.exists()
    for text in (
        done.stdout,
        done.stderr,
        verified.stdout,
        verified.stderr,
        json.dumps(receipt),
        json.dumps(check),
    ):
        _secrets_absent(text)


def test_the_receipt_of_another_kind_has_no_messages_field() -> None:
    """Only a RabbitMQ receipt carries ``holdsMessages``; the others are as before."""
    plays = yaml.safe_load((PLAYBOOKS / "backup.yml").read_text("utf-8"))
    write = next(
        task
        for task in plays[1]["tasks"]
        if task["name"] == "Write the backup receipt on the controller"
    )
    content = write["ansible.builtin.copy"]["content"]
    assert "'holdsMessages': false" in content
    assert "== 'rabbitmq'" in content
    catalog = SchemaCatalog(SCHEMAS)
    receipt: dict[str, Any] = {
        "apiVersion": "cloudfall/v1",
        "kind": "BackupReceipt",
        "metadata": {"id": "mariadb-main-backup", "description": "receipt"},
        "spec": {
            "service": "mariadb-main",
            "server": "h1",
            "action": "backup",
            "executedAt": "2026-11-18T02:50:00Z",
            "summary": "backed up crm",
        },
    }
    catalog.validate_named("backup-receipt.schema.json", receipt)
    receipt["spec"]["holdsMessages"] = False
    catalog.validate_named("backup-receipt.schema.json", receipt)


def _tasks(name: str) -> list[dict[str, Any]]:
    tasks = yaml.safe_load((ROLE / "tasks" / name).read_text("utf-8"))
    assert isinstance(tasks, list)
    return cast("list[dict[str, Any]]", tasks)


# proves: S-023-16
def test_s023_16_the_role_installs_the_scripts_and_timers_on_the_home_only() -> None:
    tasks = _tasks("backup.yml")
    directory = next(
        task for task in tasks if task["name"] == "Create the backup directory"
    )["ansible.builtin.file"]
    assert (directory["owner"], directory["group"], directory["mode"]) == (
        "root",
        "root",
        "0700",
    )
    installed = {
        task["ansible.builtin.template"]["dest"]: task
        for task in tasks
        if "ansible.builtin.template" in task
    }
    assert set(installed) == {
        "/usr/local/sbin/cloudfall-rabbitmq-backup",
        "/usr/local/sbin/cloudfall-rabbitmq-restore-check",
        "/etc/systemd/system/cloudfall-rabbitmq-backup.service",
        "/etc/systemd/system/cloudfall-rabbitmq-backup.timer",
        "/etc/systemd/system/cloudfall-rabbitmq-restore-check.service",
        "/etc/systemd/system/cloudfall-rabbitmq-restore-check.timer",
    }
    for unit in ("backup.service.j2", "restore-check.service.j2"):
        text = _render(unit)
        assert "User=root\n" in text
        assert "Requires=rabbitmq-server.service\n" in text
    assert "OnCalendar=Sun *-*-* 05:40:00 UTC\n" in _render("restore-check.timer.j2")
    assert "OnCalendar=*-*-* 02:50:00 UTC\n" in _render("backup.timer.j2")
    main = _tasks("main.yml")
    backup = next(
        task
        for task in main
        if task["name"]
        == "Install the definitions export and the restore drill on the service's home"
    )
    assert backup["ansible.builtin.import_tasks"] == "backup.yml"
    assert backup["when"] == "cloudfall_rabbitmq_home | bool"
    assert main[-1] is backup
