"""``cloudfall mcp serve``: which tools an agent gets, and what it cannot change.

The server is started for one fleet: a Cloudfall project, or a team's own
Ansible repository. That choice decides the tool list and is bound into
every tool call, so an agent works on the fleet the person started the
server for and cannot point a call at another one.

On a project, the tools are the commands an agent may run there, with the
server-changing ones previewing until called with ``yes``. On a repository,
they are the read-only fleet commands and one tool per declared operation,
whose call runs check mode and records a proposal; approving it is
``cloudfall decisions approve``, which no tool can stand in for.

Nothing here imports ``cloudfall.app``: ``App(mcp=...)`` needs these at
construction. The tools Cloudfall provides beside its commands, which use
the commands' own helpers, are in ``cloudfall.app``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from treaty import Exit, Flag, ParseError

from cloudfall.ansible_api import (
    AnsibleReadError,
    InventorySource,
    inventory_from_config,
)
from cloudfall.catalog import CATALOG_DIRECTORY, RiskLevel
from cloudfall.decision import DECISION_DIRECTORY
from cloudfall.project import (
    PROJECT_DIRECTORY_VARIABLE,
    ProjectError,
    is_project,
    resolve_project_directory,
)
from cloudfall.render_api import RENDER_API_URL
from cloudfall.resources import default_engine_directory, default_schema_directory

if TYPE_CHECKING:
    from cloudfall.catalog import Operation

APPROVAL_COMMAND = "cloudfall decisions approve"
"""The command a person runs to approve a proposal; no tool stands in for it."""

ERROR_INVENTORY_UNDECLARED = "fleet_inventory_undeclared"


@dataclass(frozen=True, slots=True, kw_only=True)
class McpServeArgs:
    """Startup flags of ``mcp serve``: the fleet it serves, and where its files are."""

    project: Path | None = Flag(
        default=None,
        description=(
            f"Project to serve (default: ${PROJECT_DIRECTORY_VARIABLE}, else the "
            "current directory when it is a project)"
        ),
    )
    repository: Path | None = Flag(
        default=None,
        description=(
            "Ansible repository to serve instead of a project: its declared "
            "operations become tools, and no tool changes a host (default: the "
            "current directory when it holds an ansible.cfg and is no project)"
        ),
    )
    inventory: Path | None = Flag(
        default=None,
        description=(
            "Inventory inside the repository (default: the one its ansible.cfg names)"
        ),
    )
    schemas: Path = Flag(
        default=default_schema_directory(),
        description="Versioned schema directory (default: bundled schemas)",
    )
    engine: Path = Flag(
        default=default_engine_directory(),
        description="Engine directory containing ansible contracts (default: bundled)",
    )
    inventory_file: Path = Flag(
        default=Path("tmp/ansible-inventory.json"),
        description="Rendered inventory path (default: tmp/ansible-inventory.json)",
    )
    observed: Path = Flag(
        default=Path("tmp/observed"),
        description="Server snapshot directory (default: tmp/observed)",
    )
    service_observed: Path = Flag(
        default=Path("tmp/observed-services"),
        description="Domain observation directory (default: tmp/observed-services)",
    )
    deployments: Path = Flag(
        default=Path("tmp/deployments"),
        description="Deployment receipt directory (default: tmp/deployments)",
    )
    artifacts: Path = Flag(
        default=Path("tmp/artifacts"),
        description="Release artifact directory (default: tmp/artifacts)",
    )
    releases: Path = Flag(
        default=Path("tmp/releases"),
        description="Release receipt directory (default: tmp/releases)",
    )
    backups: Path = Flag(
        default=Path("tmp/backups"),
        description="Backup receipt directory (default: tmp/backups)",
    )
    data_migrations: Path = Flag(
        default=Path("tmp/data-migrations"),
        description="Data migration receipt directory (default: tmp/data-migrations)",
    )
    env_receipts: Path = Flag(
        default=Path("tmp/env-receipts"),
        description=(
            "Rendered environment receipt directory (default: tmp/env-receipts)"
        ),
    )
    proposals: Path = Flag(
        default=Path("tmp/operator/proposals"),
        description="Proposal receipt directory (default: tmp/operator/proposals)",
    )
    secrets_dir: Path = Flag(
        default=Path("secrets"),
        description="sops-encrypted secrets directory (default: secrets)",
        # A directory of encrypted files; the name is no secret.
        secret=False,
    )
    operations: Path = Flag(
        default=Path(CATALOG_DIRECTORY),
        description=(
            f"Operation documents in the repository (default: {CATALOG_DIRECTORY})"
        ),
    )
    decisions: Path = Flag(
        default=Path(DECISION_DIRECTORY),
        description=(
            f"Decision records in the repository (default: {DECISION_DIRECTORY})"
        ),
    )
    def __post_init__(self) -> None:
        """Refuse two fleets: a server serves a project or a repository."""
        if self.project is not None and self.repository is not None:
            message = "--project and --repository name two fleets; pass one"
            raise ParseError(message, context={"flag": "repository"})
        if self.project is not None and self.inventory is not None:
            message = "--inventory names a repository's inventory; it has no project"
            raise ParseError(message, context={"flag": "inventory"})


class ServerMode(Enum):
    """What the server was started for."""

    PROJECT = "project"
    FLEET = "fleet"


@dataclass(frozen=True, slots=True)
class ServerRoot:
    """The one fleet a server serves, and how it reads it."""

    mode: ServerMode
    directory: Path
    """The project, or the Ansible repository; absolute."""
    inventory: InventorySource | None = None
    """The team's inventory, for a repository."""

    def path(self, value: Path) -> Path:
        """Resolve a configured path against the served directory."""
        return value if value.is_absolute() else self.directory / value


def resolve_root(args: McpServeArgs) -> ServerRoot:
    """Return the fleet to serve, as the CLI would choose it.

    Precedence: ``--repository``, then a project named by ``--project`` or
    the environment, then the current directory when it is a project, then
    the current directory when an ``ansible.cfg`` there names an inventory.
    """
    current = Path.cwd()
    if args.repository is not None:
        return _repository(args.repository.resolve(), args.inventory)
    named = args.project is not None or bool(
        os.environ.get(PROJECT_DIRECTORY_VARIABLE)
    )
    if not named and not is_project(current) and args.inventory is not None:
        return _repository(current, args.inventory)
    if not named and not is_project(current) and _has_inventory(current):
        return _repository(current, None)
    try:
        directory = resolve_project_directory(args.project, os.environ, current)
    except ProjectError as error:
        raise Exit.PROJECT_INVALID(
            error.detail, context={"code": error.code}
        ) from error
    return ServerRoot(ServerMode.PROJECT, directory.resolve())


def _has_inventory(directory: Path) -> bool:
    try:
        return inventory_from_config(directory) is not None
    except AnsibleReadError:
        # An ansible.cfg that names a missing inventory still marks a
        # repository; reading it again reports the error.
        return True


def _repository(directory: Path, inventory: Path | None) -> ServerRoot:
    try:
        if inventory is not None:
            source = InventorySource.from_boundary(
                inventory if inventory.is_absolute() else directory / inventory
            )
        else:
            declared = inventory_from_config(directory)
            if declared is None:
                message = (
                    f"{directory} names no inventory: add an ansible.cfg with an "
                    "inventory setting, or pass --inventory"
                )
                raise Exit.INVENTORY_UNREADABLE(
                    message, context={"code": ERROR_INVENTORY_UNDECLARED}
                )
            source = declared
    except AnsibleReadError as error:
        raise Exit.INVENTORY_UNREADABLE(
            error.detail, context={"code": error.code}
        ) from error
    return ServerRoot(ServerMode.FLEET, directory, source)


def operation_tool_name(operation: Operation) -> str:
    """Return the MCP tool name one declared operation is exposed under."""
    return f"operation_{operation.operation_id.value.replace('-', '_')}"


def operation_tool_description(operation: Operation) -> str:
    """Describe one operation the way a client's tool list should read it."""
    lines = [
        operation.description
        if operation.description is not None
        else f"Run the {operation.operation_id} operation",
        "",
        f"Risk: {operation.risk.value}. Targets: {operation.targets.value}.",
        "Calling this runs it and returns what the hosts reported; nothing "
        "changes and no approval is needed."
        if operation.risk is RiskLevel.READ
        else "Calling this runs check mode only and records what it would change; "
        f"a person approves the result with `{APPROVAL_COMMAND}`.",
    ]
    if operation.inputs:
        declared = ", ".join(
            f"{declared_input.name}: {declared_input.type.value}"
            + ("" if declared_input.required else " (optional)")
            for declared_input in operation.inputs
        )
        lines.append(f"Inputs: {declared}.")
    if operation.preconditions:
        lines.append(
            "Preconditions: "
            + ", ".join(condition.value for condition in operation.preconditions)
            + "."
        )
    if operation.verify is not None:
        lines.append(f"Verified by: {operation.verify.playbook}.")
    return "\n".join(lines)


PROJECT_COMMANDS = frozenset(
    {
        "config.validate",
        "inventory.show",
        "audit",
        "services.status",
        "services.observe",
        "health",
        "observe",
        "add.ssh-key",
        "add.server-type",
        "add.server",
        "import.render",
        "import.render-api",
        "secrets.render",
        "deploy",
        "rollback",
        "restart",
        "data.copy",
        "migrate",
        "backup.run",
        "backup.verify",
        "operator.list",
        "operator.show",
    }
)
"""The commands an agent runs on a project.

``init``, the loops and ``operator approve`` are not tools: a person approves.
"""

FLEET_COMMANDS = frozenset(
    {
        "inventory.show",
        "observe",
        "audit",
        "operations.list",
        "operations.show",
        "decisions.list",
        "decisions.show",
        "why",
    }
)
"""The read-only commands on a repository; the declared operations join them."""


def served_commands(args: McpServeArgs) -> frozenset[str]:
    """Return the command paths served as tools for this fleet."""
    if resolve_root(args).mode is ServerMode.FLEET:
        return FLEET_COMMANDS
    return PROJECT_COMMANDS


_IMPORT_CONFIG = "tmp/import/config"
_IMPORT_ENV = "tmp/import/env"
_MIGRATE_PLAN = "tmp/migrate/plan.json"


def bound_arguments(args: McpServeArgs) -> dict[str, object]:
    """Return the arguments every tool call runs with, fixed for the run.

    The fleet and every directory a command reads or writes are the
    server's, so a call cannot reach another fleet or another engine.
    """
    root = resolve_root(args)
    shared: dict[str, object] = {
        "schemas": str(args.schemas),
        "engine": str(args.engine),
    }
    if root.mode is ServerMode.FLEET:
        if root.inventory is None:
            message = "a repository is served with its inventory"
            raise TypeError(message)
        observed = str(root.path(args.observed))
        return {
            **shared,
            "repository": str(root.directory),
            "project": None,
            "inventory": str(root.inventory.value),
            "observed": observed,
            "env_receipts": str(root.path(args.env_receipts)),
            "operations": str(args.operations),
            "decisions": str(args.decisions),
        }
    return {
        **shared,
        "project": str(root.directory),
        "inventory": None,
        "inventory_file": str(args.inventory_file),
        "observed": str(args.observed),
        "service_observed": str(args.service_observed),
        "deployments": str(args.deployments),
        "artifacts": str(args.artifacts),
        "releases": str(args.releases),
        "backups": str(args.backups),
        "data_migrations": str(args.data_migrations),
        "env_receipts": str(args.env_receipts),
        # What an import writes, and where migrate keeps its plan, stay in the
        # project: an import's env files hold the application's real values.
        "output_dir": _IMPORT_CONFIG,
        "env_dir": _IMPORT_ENV,
        "plan_file": _MIGRATE_PLAN,
        "proposals": str(args.proposals),
        "secrets_dir": str(args.secrets_dir),
        # A chosen URL would send any file the call names as the key to it.
        "api_url": RENDER_API_URL,
        # Decrypted values are written where the server puts them, in the project.
        "output_file": None,
    }


_FLEET_INSTRUCTIONS = f"""\
Cloudfall is the record for a fleet run through the team's own Ansible
repository. The tool list is the catalog the team declared: a playbook
they have not declared as an operation is not reachable here, and a raw
shell call is what this server exists to make unnecessary.

The loop is fixed: read the fleet (inventory_show), collect snapshots
(observe), compare them with the declared fleet (audit), pick an operation
and say why, then call its operation_* tool to preview it. Calling an
operation tool changes nothing on a host: it runs the playbook in check
mode and records the operation, the targets, the evidence it was based on
and the diff it would produce. Approving that record is `{APPROVAL_COMMAND}`,
a command a person runs; no tool here can do it, whatever reason is given
for asking. When asked why something was done, answer from the record
with the why tool rather than from memory.

Each operation tool carries the risk level the team declared, so read,
mutating and destructive are visible to this client's own gate; a
destructive one asks for confirm_destructive even to preview.
Every result is an envelope: read ok, then data, else error.
"""

_PROJECT_INSTRUCTIONS = """\
Cloudfall manages declarative infrastructure for dedicated Debian servers,
in the one project this server was started for. Read-only tools validate
the project and derive evidence. deploy, rollback, restart, migrate and
data_copy change servers: called without yes they only return the plan;
review it, then call again with yes: true. The converge_* tools converge
every declared server and are destructive: they run only with
confirm_destructive: true. Every change writes receipts; nothing reports
success it cannot prove.

add_ssh-key, add_server-type and add_server declare the fleet: they write
schema-validated resource files into the project, never onto a server, and
never overwrite an existing resource. The project itself is laid out with
`cloudfall init` on the CLI, and the operator's watch loop runs there too.
operator_list and operator_show read the proposals that loop records.
Approving one is `cloudfall operator approve`, a command a person runs
from a shell; no tool here can do it, whatever reason is given for asking.
Every result is an envelope: read ok, then data, else error.
"""


def server_instructions(args: McpServeArgs) -> str:
    """Return what the client is told about this server's loop and gates."""
    if resolve_root(args).mode is ServerMode.FLEET:
        return _FLEET_INSTRUCTIONS
    return _PROJECT_INSTRUCTIONS
