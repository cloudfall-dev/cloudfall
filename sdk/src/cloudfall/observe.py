"""Collect read-only observations of the fleet the inventory describes.

The inspect role needs two things Ansible does not know: which hosts
Cloudfall observes, and the server type each one is held to. In a brownfield
repository neither belongs in the team's files, because both are derived
from what they already declare.

So the run takes their inventory as it is and adds one ephemeral overlay
source holding nothing but the ``cloudfall_servers`` group and those two
derived variables. Ansible merges sources, so connection settings, group
membership and every other variable still come from their inventory; the
overlay is regenerated per run and owns nothing. Removing Cloudfall leaves
their inventory exactly as it was.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cloudfall.ansible_api import InventorySource, read_inventory

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from cloudfall.inventory import PlatformInventory

OBSERVED_GROUP = "cloudfall_servers"
"""Inventory group the inspect playbook targets."""

OVERLAY_FILE = "inventory-overlay.json"
"""Generated inventory source holding the group and the derived variables."""

INSPECT_PLAYBOOK = "inspect.yml"
"""Engine playbook that collects one snapshot per host."""

_SERVER_ID_VARIABLE = "cloudfall_server_id"
_SERVER_TYPE_VARIABLE = "cloudfall_server_type"
_OUTPUT_DIRECTORY_VARIABLE = "cloudfall_inspect_output_directory"
_ANSIBLE_CONFIG_FILE = "ansible.cfg"
_SNAPSHOT_SUFFIX = ".json"
_OUTPUT_TAIL_CHARACTERS = 2000
"""As much of a failed run's output as a lifecycle error keeps."""

_ERROR_ARGUMENT = "observe_invalid_argument"
_ERROR_ANSIBLE_MISSING = "observe_ansible_missing"
_ERROR_ENGINE_MISSING = "observe_engine_missing"
_ERROR_SOURCE_MISSING = "observe_inventory_missing"
_ERROR_NO_TARGETS = "observe_no_targets"


class ObserveError(RuntimeError):
    """Fail-fast observation error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        """Record the failure code and human-readable detail."""
        self.code = code
        self.detail = message
        super().__init__(f"{code}: {message}")

    def as_dict(self) -> dict[str, object]:
        """Serialize the error envelope for system boundaries."""
        return {
            "status": "error",
            "error": {"code": self.code, "message": self.detail},
        }


@dataclass(frozen=True, slots=True)
class ObservationRequest:
    """Everything one inspection run needs."""

    inventory_sources: tuple[Path, ...]
    output_directory: Path
    engine_directory: Path
    limit: str | None = None
    configuration: Path | None = None
    """``ansible.cfg`` to honour; the team's own when they have one."""

    def __post_init__(self) -> None:
        """Reject inputs Ansible would only fail on later."""
        if not self.inventory_sources:
            message = "an inspection needs at least one inventory source"
            raise ObserveError(_ERROR_SOURCE_MISSING, message)
        for source in self.inventory_sources:
            if not source.exists():
                message = f"inventory source does not exist: {source}"
                raise ObserveError(_ERROR_SOURCE_MISSING, message)
        if not self.output_directory.is_absolute():
            message = (
                "the inspect role writes snapshots to an absolute path, got "
                f"{self.output_directory}"
            )
            raise ObserveError(_ERROR_ARGUMENT, message)
        if self.limit is not None and not self.limit:
            message = "limit must not be empty"
            raise ObserveError(_ERROR_ARGUMENT, message)


@dataclass(frozen=True, slots=True)
class PlaybookRun:
    """How an inspection playbook ended, and what it printed."""

    exit_code: int
    output: str
    """Ansible's stdout and stderr, interleaved as it wrote them."""


@dataclass(frozen=True, slots=True)
class ObservationResult:
    """What one inspection run produced."""

    output_directory: Path
    requested: tuple[str, ...]
    """Servers this run was asked to observe, after any ``--limit``."""

    observed: tuple[str, ...]
    """Requested servers whose snapshot exists after the run."""

    missing: tuple[str, ...]
    """Requested servers the run produced no snapshot for."""

    exit_code: int
    detail: str | None = None
    """The end of Ansible's output when the playbook failed; it says why."""

    @property
    def complete(self) -> bool:
        """Every declared server was observed and Ansible was happy."""
        return self.exit_code == 0 and not self.missing

    def as_dict(self) -> dict[str, object]:
        """Serialize the run for system boundaries."""
        result: dict[str, object] = {
            "status": "ok" if self.complete else "incomplete",
            "requested": list(self.requested),
            "observed": list(self.observed),
            "missing": list(self.missing),
            "output": str(self.output_directory),
            "exitCode": self.exit_code,
        }
        if self.detail is not None:
            result["detail"] = self.detail
        return result


def overlay_document(inventory: PlatformInventory) -> dict[str, object]:
    """Return the inventory source that says what Cloudfall observes.

    Only the group and the two derived variables: every connection setting
    stays in the team's inventory, where it already is.
    """
    server_types = {
        server_type.resource_id: server_type
        for server_type in inventory.server_types
    }
    hosts = {
        server.resource_id.value: {
            _SERVER_ID_VARIABLE: server.resource_id.value,
            _SERVER_TYPE_VARIABLE: server_types[server.server_type_id].as_dict(),
        }
        for server in inventory.servers
    }
    return {"all": {"children": {OBSERVED_GROUP: {"hosts": hosts}}}}


def write_overlay(inventory: PlatformInventory, directory: Path) -> Path:
    """Write the overlay source and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / OVERLAY_FILE
    document = json.dumps(overlay_document(inventory), indent=2, sort_keys=True)
    path.write_text(f"{document}\n", encoding="utf-8")
    return path


def observation_command(
    request: ObservationRequest, overlay: Path
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Return the ``ansible-playbook`` invocation and its environment."""
    binary = shutil.which("ansible-playbook")
    if binary is None:
        message = "ansible-playbook is not installed on this controller"
        raise ObserveError(_ERROR_ANSIBLE_MISSING, message)
    ansible_directory = request.engine_directory / "ansible"
    playbook = ansible_directory / "playbooks" / INSPECT_PLAYBOOK
    roles = ansible_directory / "roles"
    for required in (playbook, roles):
        if not required.exists():
            message = f"engine is incomplete, missing {required}"
            raise ObserveError(_ERROR_ENGINE_MISSING, message)

    argv: list[str] = [binary]
    for source in (*request.inventory_sources, overlay):
        argv.extend(("--inventory", str(source)))
    if request.limit is not None:
        argv.extend(("--limit", request.limit))
    argv.extend(
        (
            "--extra-vars",
            json.dumps(
                {_OUTPUT_DIRECTORY_VARIABLE: str(request.output_directory)},
                sort_keys=True,
            ),
            str(playbook),
        )
    )
    # The engine's roles are found through the environment, which outranks
    # the `roles_path` in whichever ansible.cfg applies, so the team's own
    # configuration keeps deciding how Ansible connects to their hosts.
    environment = {"ANSIBLE_ROLES_PATH": str(roles.resolve())}
    configuration = _configuration(request)
    if configuration is not None:
        environment["ANSIBLE_CONFIG"] = str(configuration.resolve())
    return tuple(argv), environment


def collect_observations(
    inventory: PlatformInventory,
    request: ObservationRequest,
    overlay_directory: Path,
    run: Callable[[Sequence[str], Mapping[str, str]], PlaybookRun] | None = None,
) -> ObservationResult:
    """Inspect every declared server and report which snapshots exist."""
    requested = requested_servers(inventory, request)
    if not requested:
        message = (
            "the fleet declares no servers to observe"
            if request.limit is None
            else f"no declared server matches the limit {request.limit!r}"
        )
        raise ObserveError(_ERROR_NO_TARGETS, message)
    overlay = write_overlay(inventory, overlay_directory)
    argv, environment = observation_command(request, overlay)
    playbook_run = (run or _run_playbook)(argv, environment)
    observed = tuple(
        server
        for server in requested
        if (request.output_directory / f"{server}{_SNAPSHOT_SUFFIX}").is_file()
    )
    return ObservationResult(
        output_directory=request.output_directory,
        requested=requested,
        observed=observed,
        missing=tuple(server for server in requested if server not in set(observed)),
        exit_code=playbook_run.exit_code,
        detail=(
            playbook_run.output[-_OUTPUT_TAIL_CHARACTERS:].strip() or None
            if playbook_run.exit_code != 0
            else None
        ),
    )


def requested_servers(
    inventory: PlatformInventory, request: ObservationRequest
) -> tuple[str, ...]:
    """Return the declared servers this run is asked to observe.

    A ``--limit`` is Ansible's own host pattern, so Ansible resolves it:
    guessing at one here would report a run as incomplete whenever the
    operator narrowed it on purpose.
    """
    declared = tuple(server.resource_id.value for server in inventory.servers)
    if request.limit is None:
        return declared
    matched = {
        str(host.name)
        for host in read_inventory(
            InventorySource(request.inventory_sources[0]), limit=request.limit
        ).hosts
    }
    return tuple(server for server in declared if server in matched)


def _run_playbook(argv: Sequence[str], environment: Mapping[str, str]) -> PlaybookRun:
    # Captured, never inherited: the CLI's stdout carries one JSON document
    # and `mcp serve`'s carries the protocol, so Ansible may write to neither.
    completed = subprocess.run(  # noqa: S603 - resolved binary, built argv.
        tuple(argv),
        env={**os.environ, **environment},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return PlaybookRun(exit_code=completed.returncode, output=completed.stdout)


def _configuration(request: ObservationRequest) -> Path | None:
    if request.configuration is not None:
        return request.configuration
    engine_configuration = (
        request.engine_directory / "ansible" / _ANSIBLE_CONFIG_FILE
    )
    if engine_configuration.is_file():
        return engine_configuration
    return None


def team_configuration(directory: Path) -> Path | None:
    """Return the team's own ``ansible.cfg`` when this is their repository."""
    candidate = directory / _ANSIBLE_CONFIG_FILE
    return candidate if candidate.is_file() else None
