"""Deploy the declared LoggingStack with the secrets Cloudfall keeps for it (S-011).

The plan resolves every requirement and its state, and changes nothing. The
deploy first meets what the secrets provider can meet on the controller:
missing generated secrets are written to the store, and a secret only a
person can supply stops the run before any server is touched. Then each
stored value is staged in a 0600 file in a private temp dir and
``logging.yml`` copies it to the host path the stack declares, keeping any
file a person already put there. The staged files are removed when the run
ends, whether it passed or failed.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cloudfall.requirements import (
    RequirementState,
    SecretRequirement,
    SecretSource,
    ServerRequirements,
    requirements_of,
)
from cloudfall.secret_store import (
    SecretStore,
    StoredSecret,
    needs_input,
    requirement_states,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cloudfall.domain import ResourceId
    from cloudfall.inventory import LoggingStackInventory, PlatformInventory

SECRET_FILES_VARIABLE = "cloudfall_logging_secret_files"  # noqa: S105 - a name
"""The ``logging.yml`` variable: server id, then ``[{path, source}]``."""
PLAYBOOK = "logging.yml"
ERROR_STACK_UNDECLARED = "logging_stack_undeclared"
_FILE_MODE = 0o600


class LoggingDeployError(RuntimeError):
    """A deploy that cannot start, named by a stable code."""

    def __init__(self, code: str, message: str) -> None:
        """Capture the code beside the message."""
        super().__init__(message)
        self.code = code
        self.message = message


class InputMissingError(RuntimeError):
    """Secrets only a person can supply are missing; nothing ran."""

    def __init__(self, missing: tuple[dict[str, object], ...]) -> None:
        """Name each missing secret."""
        names = ", ".join(str(item["name"]) for item in missing)
        super().__init__(f"secrets only a person can supply are missing: {names}")
        self.missing = missing


@dataclass(frozen=True, slots=True)
class DeliveredSecret:
    """One secret staged for a server: where it lands and its hash, never the value."""

    server: ResourceId
    name: str
    path: str
    sha256: str

    def as_dict(self) -> dict[str, object]:
        """Serialize the delivery; the host keeps a file already there."""
        return {
            "server": self.server.value,
            "name": self.name,
            "path": self.path,
            "sha256": self.sha256,
        }


def declared_stack(inventory: PlatformInventory) -> LoggingStackInventory:
    """Return the one declared LoggingStack, or fail naming why there is none."""
    if not inventory.logging_stacks:
        message = "no LoggingStack is declared; declare one under logging-stacks/"
        raise LoggingDeployError(ERROR_STACK_UNDECLARED, message)
    return inventory.logging_stacks[0]


def stack_requirements(
    inventory: PlatformInventory, stack: LoggingStackInventory
) -> tuple[ServerRequirements, ...]:
    """Return only the requirements ``stack`` places on its servers."""
    return tuple(
        ServerRequirements(
            server.server_id,
            tuple(
                item
                for item in server.requirements
                if item.owner.resource_id == stack.resource_id
            ),
        )
        for server in requirements_of(inventory)
    )


def preview_logging_deploy(
    inventory: PlatformInventory, store: SecretStore
) -> dict[str, object]:
    """Return the plan: the stack, its servers, and each requirement's state."""
    stack = declared_stack(inventory)
    servers = stack_requirements(inventory, stack)
    answered = requirement_states(servers, store)
    to_generate = [
        spec.name.value
        for spec in _secrets(servers)
        if store.state(spec) is RequirementState.WILL_CREATE
    ]
    effect = (
        f"deploy LoggingStack {stack.resource_id.value} with {PLAYBOOK}: backend "
        f"{stack.backend.server_id.value}, collectors "
        f"{', '.join(server.value for server in stack.collectors.server_ids)}"
    )
    if to_generate:
        effect += f"; first generate {', '.join(to_generate)} into the secret store"
    effect += "; copy each stored secret to its host path, keeping a file already there"
    return {
        "status": "plan",
        "action": "logging-deploy",
        "loggingStack": stack.resource_id.value,
        "servers": answered,
        "missing": needs_input(answered),
        "wouldRun": effect,
    }


def prepare_secrets(
    inventory: PlatformInventory, store: SecretStore
) -> tuple[tuple[StoredSecret, ...], tuple[ServerRequirements, ...]]:
    """Meet the secret requirements the controller can meet, before any server.

    Raises ``InputMissingError`` when a secret only a person can supply is not
    stored; nothing is generated then, so a refused run changes nothing.
    """
    stack = declared_stack(inventory)
    servers = stack_requirements(inventory, stack)
    missing = needs_input(requirement_states(servers, store))
    if missing:
        raise InputMissingError(tuple(missing))
    generated = tuple(
        store.generate(spec.reference)
        for spec in _secrets(servers)
        if spec.source is SecretSource.GENERATE and not store.holds(spec.reference)
    )
    return generated, servers


@contextmanager
def staged_secret_files(
    servers: tuple[ServerRequirements, ...], store: SecretStore
) -> Iterator[tuple[dict[str, object], tuple[DeliveredSecret, ...]]]:
    """Stage every server's secrets in 0600 files; yield the extra vars and hashes.

    The values pass through memory and the private temp dir only; the
    directory is removed when the block ends.
    """
    with tempfile.TemporaryDirectory(prefix="cloudfall-logging-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        files: dict[str, list[dict[str, str]]] = {}
        delivered: list[DeliveredSecret] = []
        for server in servers:
            for requirement in server.requirements:
                spec = requirement.spec
                if not isinstance(spec, SecretRequirement):
                    continue
                value = store.value(spec.reference)
                source = root / f"{server.server_id.value}-{spec.name.value}"
                descriptor = os.open(
                    source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(value)
                files.setdefault(server.server_id.value, []).append(
                    {"path": spec.path.value, "source": str(source)}
                )
                delivered.append(
                    DeliveredSecret(
                        server.server_id,
                        spec.name.value,
                        spec.path.value,
                        hashlib.sha256(value.encode("utf-8")).hexdigest(),
                    )
                )
        yield {SECRET_FILES_VARIABLE: files}, tuple(delivered)


def _secrets(servers: tuple[ServerRequirements, ...]) -> list[SecretRequirement]:
    return [
        requirement.spec
        for server in servers
        for requirement in server.requirements
        if isinstance(requirement.spec, SecretRequirement)
    ]
