"""A stand-in ``rabbitmqctl`` that keeps a node's definitions in a JSON file.

It answers the subcommands the ``cloudfall_rabbitmq`` role and inspect use,
changes the definitions the way RabbitMQ would, and logs every argv, and the
content of every file it imports, so a test can grep them for a password.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from cloudfall.rabbitmq import password_hash

if TYPE_CHECKING:
    from pathlib import Path

GUEST_HASH = password_hash("guest", b"\x01\x02\x03\x04")
"""A stored hash of the shape RabbitMQ gives guest on a fresh node."""

_SCRIPT = r"""
import json
import sys
from pathlib import Path

STATE = Path(__STATE__)
LOG = Path(__LOG__)


def load():
    return json.loads(STATE.read_text(encoding="utf-8"))


def save(state):
    STATE.write_text(json.dumps(state), encoding="utf-8")


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def positional(argv):
    found = []
    skip = False
    for item in argv:
        if skip:
            skip = False
            continue
        if item in ("--vhost", "-p", "--default-queue-type", "--tags", "--format"):
            skip = True
            continue
        if item.startswith("--"):
            continue
        found.append(item)
    return found


def main(argv):
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")
    state = load()
    command, args = argv[0], positional(argv[1:])
    if command == "export_definitions":
        print(json.dumps(state))
        return 0
    if command == "list_queues":
        vhost = flag(argv, "--vhost") or "/"
        for queue in state.get("queues", {}).get(vhost, []):
            print(queue)
        return 0
    if command in ("add_vhost", "update_vhost_metadata"):
        name = args[0]
        tags = [tag for tag in (flag(argv, "--tags") or "").split(",") if tag]
        metadata = {
            "description": "",
            "tags": tags,
            "default_queue_type": flag(argv, "--default-queue-type") or "classic",
        }
        found = [item for item in state["vhosts"] if item["name"] == name]
        if command == "add_vhost" and found:
            print("vhost exists", file=sys.stderr)
            return 1
        if command == "update_vhost_metadata" and not found:
            return 1
        state["vhosts"] = [item for item in state["vhosts"] if item["name"] != name]
        state["vhosts"].append({"name": name, "metadata": metadata})
    elif command == "set_permissions":
        vhost = flag(argv, "--vhost")
        user, configure, write, read = args
        # RabbitMQ refuses permissions of a user or on a vhost it does not have
        if user not in [item["name"] for item in state["users"]]:
            print(f"Error: no_such_user: {user}", file=sys.stderr)
            return 1
        if vhost not in [item["name"] for item in state["vhosts"]]:
            print(f"Error: no_such_vhost: {vhost}", file=sys.stderr)
            return 1
        state["permissions"] = [
            item
            for item in state["permissions"]
            if (item["user"], item["vhost"]) != (user, vhost)
        ]
        state["permissions"].append(
            {
                "user": user,
                "vhost": vhost,
                "configure": configure,
                "write": write,
                "read": read,
            }
        )
    elif command == "clear_permissions":
        vhost = flag(argv, "--vhost")
        (user,) = args
        state["permissions"] = [
            item
            for item in state["permissions"]
            if (item["user"], item["vhost"]) != (user, vhost)
        ]
    elif command == "clear_password":
        (user,) = args
        for item in state["users"]:
            if item["name"] == user:
                item["password_hash"] = ""
    elif command == "delete_user":
        (user,) = args
        state["users"] = [item for item in state["users"] if item["name"] != user]
        state["permissions"] = [
            item for item in state["permissions"] if item["user"] != user
        ]
    elif command == "delete_vhost":
        (vhost,) = args
        state["vhosts"] = [item for item in state["vhosts"] if item["name"] != vhost]
        state["permissions"] = [
            item for item in state["permissions"] if item["vhost"] != vhost
        ]
        state.get("queues", {}).pop(vhost, None)
    elif command == "import_definitions":
        (path,) = args
        content = Path(path).read_text(encoding="utf-8")
        with LOG.open("a", encoding="utf-8") as log:
            log.write(json.dumps(["imported", content]) + "\n")
        imported = json.loads(content)
        for user in imported.get("users", []):
            state["users"] = [
                item for item in state["users"] if item["name"] != user["name"]
            ]
            state["users"].append(user)
    else:
        print(f"unknown command {command}", file=sys.stderr)
        return 64
    save(state)
    return 0


sys.exit(main([item for item in sys.argv[1:] if item != "--quiet"]))
"""


def fresh_state() -> dict[str, object]:
    """Return what a node RabbitMQ just installed holds: ``guest`` and ``/``."""
    return {
        "users": [
            {
                "name": "guest",
                "password_hash": GUEST_HASH,
                "hashing_algorithm": "rabbit_password_hashing_sha256",
                "tags": ["administrator"],
                "limits": {},
            }
        ],
        "vhosts": [
            {
                "name": "/",
                "metadata": {
                    "description": "Default virtual host",
                    "tags": [],
                    "default_queue_type": "classic",
                },
            }
        ],
        "permissions": [
            {
                "user": "guest",
                "vhost": "/",
                "configure": ".*",
                "write": ".*",
                "read": ".*",
            }
        ],
        "queues": {},
    }


def write_ctl(directory: Path, state: dict[str, object]) -> tuple[Path, Path, Path]:
    """Write the stand-in, its state and its log; return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "definitions.json"
    log = directory / "rabbitmqctl.log"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    log.write_text("", encoding="utf-8")
    ctl = directory / "rabbitmqctl"
    ctl.write_text(
        f"#!{sys.executable}\n"
        + _SCRIPT.replace("__STATE__", repr(str(state_path))).replace(
            "__LOG__", repr(str(log))
        ),
        encoding="utf-8",
    )
    ctl.chmod(0o755)
    return ctl, state_path, log
