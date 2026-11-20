"""Wait until a RabbitMQ cluster of three is healthy, or say what is missing.

Healthy (S-023, Adding, replacing and losing a node): every declared node is
running and a member of the cluster, no node has a memory or disk alarm, no
partition is reported, and every quorum queue has a leader and as many
members online as the service has nodes. The module reads the node's
command-line tools until that holds or ``timeout`` passes; it only reads, so
it never reports a change, and a later converge resumes the wait.
"""

from __future__ import annotations

import json
import time
from typing import cast

from ansible.module_utils.basic import AnsibleModule

DOCUMENTATION = r"""
module: cloudfall_rabbitmq_health
short_description: Wait until a RabbitMQ cluster is healthy
description:
  - Reads cluster_status, the virtual hosts and each one's queues with the
    node's command-line tools until every declared node runs as a member, no
    alarm or partition is reported, and every quorum queue has a leader and a
    member online on every declared node. Fails with what is missing after
    C(timeout) seconds. Changes nothing.
options:
  ctl:
    description: Path of rabbitmqctl.
    type: path
    required: true
  diagnostics:
    description: Path of rabbitmq-diagnostics.
    type: path
    required: true
  nodes:
    description: The declared node names, as rabbit@h1.
    type: list
    elements: str
    required: true
  timeout:
    description: Seconds to wait before giving up.
    type: int
    default: 3600
  interval:
    description: Seconds between two reads.
    type: int
    default: 10
author: Cloudfall
"""

RETURN = r"""
problems:
  description: >-
    What kept the cluster from being healthy at the last read; empty on
    success.
  type: list
  elements: str
  returned: always
"""


def _list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _describe(item: object) -> str:
    if isinstance(item, dict):
        return ", ".join(f"{key} {value}" for key, value in sorted(item.items()))
    return str(item)


def problems(
    status: dict[str, object],
    queues: dict[str, list[dict[str, object]]],
    nodes: list[str],
) -> list[str]:
    """Return what keeps the cluster from being healthy; empty when it is.

    ``status`` is ``cluster_status``'s JSON, ``queues`` each virtual host's
    ``list_queues name type leader online`` and ``nodes`` the declared names.
    """
    found: list[str] = []
    running = {str(item) for item in _list(status.get("running_nodes"))}
    members = {
        str(item)
        for key in ("disk_nodes", "ram_nodes")
        for item in _list(status.get(key))
    }
    for node in nodes:
        if node not in members:
            found.append(f"{node} is not a member of the cluster")
        elif node not in running:
            found.append(f"{node} is not running")
    found.extend(f"alarm: {_describe(item)}" for item in _list(status.get("alarms")))
    partitions = status.get("partitions")
    if isinstance(partitions, dict):
        found.extend(
            f"partition reported by {node}: {', '.join(map(str, _list(seen)))}"
            for node, seen in sorted(partitions.items())
            if _list(seen)
        )
    else:
        found.extend(f"partition: {_describe(item)}" for item in _list(partitions))
    for vhost, listed in sorted(queues.items()):
        for queue in listed:
            if queue.get("type") != "quorum":
                continue
            name = f"{vhost}/{queue.get('name')}"
            online = [node for node in _list(queue.get("online")) if node in nodes]
            if not queue.get("leader"):
                found.append(f"quorum queue {name} has no leader")
            if len(online) < len(nodes):
                found.append(
                    f"quorum queue {name} has {len(online)} of {len(nodes)} "
                    "members online"
                )
    return found


def _read(module: AnsibleModule, argv: list[str]) -> tuple[object, str | None]:
    """Run one read; return its JSON, or what went wrong."""
    rc, stdout, stderr = module.run_command(argv)
    command = " ".join(argv[1:4])
    if rc != 0:
        return None, f"{command} failed: {(stderr or stdout).strip()[-200:]}"
    try:
        return json.loads(stdout), None
    except json.JSONDecodeError:
        return None, f"{command} did not print JSON"


def _state(
    module: AnsibleModule, ctl: str, diagnostics: str
) -> tuple[dict[str, object], dict[str, list[dict[str, object]]], list[str]]:
    errors: list[str] = []
    status, error = _read(
        module, [diagnostics, "--quiet", "--formatter", "json", "cluster_status"]
    )
    if error:
        errors.append(error)
    vhosts, error = _read(
        module, [ctl, "--quiet", "--formatter", "json", "list_vhosts", "name"]
    )
    if error:
        errors.append(error)
    queues: dict[str, list[dict[str, object]]] = {}
    for vhost in _list(vhosts):
        name = str(vhost.get("name")) if isinstance(vhost, dict) else str(vhost)
        listed, error = _read(
            module,
            [
                ctl,
                "--quiet",
                "--formatter",
                "json",
                "list_queues",
                "--vhost",
                name,
                "name",
                "type",
                "leader",
                "online",
            ],
        )
        if error:
            errors.append(error)
        queues[name] = [item for item in _list(listed) if isinstance(item, dict)]
    return (
        cast("dict[str, object]", status) if isinstance(status, dict) else {},
        queues,
        errors,
    )


def main() -> None:
    """Wait for health; fail with what is missing once the time is up."""
    module = AnsibleModule(
        argument_spec={
            "ctl": {"type": "path", "required": True},
            "diagnostics": {"type": "path", "required": True},
            "nodes": {"type": "list", "elements": "str", "required": True},
            "timeout": {"type": "int", "default": 3600},
            "interval": {"type": "int", "default": 10},
        },
        supports_check_mode=True,
    )
    nodes = cast("list[str]", module.params["nodes"])
    deadline = time.monotonic() + int(module.params["timeout"])
    while True:
        status, queues, errors = _state(
            module, str(module.params["ctl"]), str(module.params["diagnostics"])
        )
        missing = errors or problems(status, queues, nodes)
        if not missing:
            module.exit_json(changed=False, problems=[])
        if time.monotonic() >= deadline:
            module.fail_json(
                msg=(
                    f"the cluster of {', '.join(nodes)} is not healthy after "
                    f"{module.params['timeout']} seconds: {'; '.join(missing)}. "
                    "A later converge resumes the wait"
                ),
                problems=missing,
            )
        time.sleep(int(module.params["interval"]))


if __name__ == "__main__":
    main()
