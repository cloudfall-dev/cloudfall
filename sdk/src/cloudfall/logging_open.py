"""Open a LoggingStack's Grafana through an SSH forward (S-016).

Grafana listens on the backend's loopback only. ``logging open`` builds the
``ssh -L`` forward from what the project declares (the backend server's
address, SSH user and port, Grafana's listen address and port) so a person
never writes it by hand. The admin password is never read here: the answer
names where the project's secret store keeps it.
"""

from __future__ import annotations

import socket
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cloudfall.logging_deploy import stack_requirements
from cloudfall.requirements import SecretRequirement

if TYPE_CHECKING:
    from cloudfall.domain import ResourceId
    from cloudfall.inventory import LoggingStackInventory, PlatformInventory

DEFAULT_LOCAL_PORT = 3000
DASHBOARDS_PATH = "/dashboards/f/cloudfall"
ERROR_STACK_UNDECLARED = "logging_stack_undeclared"
ERROR_STACK_UNKNOWN = "logging_stack_unknown"
ERROR_STACK_AMBIGUOUS = "logging_stack_ambiguous"
ERROR_PORT_IN_USE = "local_port_in_use"
ERROR_SSH_MISSING = "ssh_missing"
ERROR_FORWARD_FAILED = "ssh_forward_failed"
READY_TIMEOUT_SECONDS = 30.0
_LOOPBACK = "127.0.0.1"


class LoggingOpenError(RuntimeError):
    """The forward cannot be built or started; nothing ran."""

    def __init__(self, code: str, message: str) -> None:
        """Keep the machine-readable code next to the message."""
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class GrafanaForward:
    """The ssh command that forwards a local port to a stack's Grafana."""

    stack: ResourceId
    server: ResourceId
    local_port: int
    argv: tuple[str, ...]
    url: str
    password_file: str
    password_key: str

    def as_dict(self) -> dict[str, object]:
        """Serialize for the command answer; it carries no secret value."""
        return {
            "loggingStack": self.stack.value,
            "server": self.server.value,
            "localPort": self.local_port,
            "command": list(self.argv),
            "url": self.url,
            "adminUser": "admin",
            "adminPassword": {"file": self.password_file, "key": self.password_key},
        }


def chosen_stack(
    inventory: PlatformInventory, stack_id: ResourceId | None
) -> LoggingStackInventory:
    """Return the stack ``--stack`` names, or the only one declared."""
    stacks = inventory.logging_stacks
    if not stacks:
        message = "no LoggingStack is declared; declare one under logging-stacks/"
        raise LoggingOpenError(ERROR_STACK_UNDECLARED, message)
    if stack_id is None:
        if len(stacks) > 1:
            names = ", ".join(stack.resource_id.value for stack in stacks)
            message = f"more than one LoggingStack is declared ({names}); pass --stack"
            raise LoggingOpenError(ERROR_STACK_AMBIGUOUS, message)
        return stacks[0]
    for stack in stacks:
        if stack.resource_id == stack_id:
            return stack
    message = f"no LoggingStack {stack_id.value} is declared"
    raise LoggingOpenError(ERROR_STACK_UNKNOWN, message)


def grafana_forward(
    inventory: PlatformInventory, stack: LoggingStackInventory, local_port: int
) -> GrafanaForward:
    """Build the forward from the stack's backend server and Grafana endpoint."""
    server = next(
        item
        for item in inventory.servers
        if item.resource_id == stack.backend.server_id
    )
    password = next(
        requirement.spec
        for owned in stack_requirements(inventory, stack)
        for requirement in owned.requirements
        if isinstance(requirement.spec, SecretRequirement)
        and requirement.spec.path == stack.grafana.admin_password_path
    )
    reference = password.reference
    forward = (
        f"{_LOOPBACK}:{local_port}:"
        f"{stack.grafana.listen_address.value}:{stack.grafana.port.value}"
    )
    argv = (
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-p",
        str(server.ssh_port.value),
        "-L",
        forward,
        f"{server.ssh_user.value}@{server.address.value}",
    )
    return GrafanaForward(
        stack=stack.resource_id,
        server=server.resource_id,
        local_port=local_port,
        argv=argv,
        url=f"http://{_LOOPBACK}:{local_port}{DASHBOARDS_PATH}",
        password_file=(
            f"secrets/{reference.environment.value}{reference.path.value}.env"
        ),
        password_key=reference.key.value,
    )


def require_free_port(port: int) -> None:
    """Stop before ssh starts when the local port is taken."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((_LOOPBACK, port))
        except OSError as error:
            message = f"local port {port} is in use; pass --port with a free one"
            raise LoggingOpenError(ERROR_PORT_IN_USE, message) from error


def forward_ready(port: int) -> bool:
    """Return whether something accepts connections on the local port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((_LOOPBACK, port)) == 0


def start_forward(
    forward: GrafanaForward, *, timeout: float = READY_TIMEOUT_SECONDS
) -> subprocess.Popen[bytes]:
    """Start ssh and return it once the local port accepts connections."""
    try:
        process = subprocess.Popen(forward.argv, stdin=subprocess.DEVNULL)  # noqa: S603
    except FileNotFoundError as error:
        message = "ssh is not installed or not on PATH"
        raise LoggingOpenError(ERROR_SSH_MISSING, message) from error
    deadline = time.monotonic() + timeout
    while not forward_ready(forward.local_port):
        code = process.poll()
        if code is not None:
            message = f"ssh exited with {code} before the forward was up"
            raise LoggingOpenError(ERROR_FORWARD_FAILED, message)
        if time.monotonic() > deadline:
            process.terminate()
            process.wait()
            message = f"the forward was not up after {timeout:.0f}s"
            raise LoggingOpenError(ERROR_FORWARD_FAILED, message)
        time.sleep(0.2)
    return process
