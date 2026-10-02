"""treaty boundary for the ``cloudfall`` commands moved off argparse so far.

The CLI moves to treaty a command at a time. ``cloudfall.cli.main`` sends a
command line here when ``app.resolves`` says one of these commands owns it
and to argparse otherwise, so both run side by side until the last command
moves.

Every answer keeps the keys the argparse CLI wrote under ``data``,
``status`` included: a ``Payload`` subclass names its command in the
operating contract (``cloudfall.commands``), and that contract's output
shape is the schema treaty checks the answer against. A negative verdict,
such as drift, exits with its own code and keeps the report in ``data``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self

from treaty import App, Arg, Ctx, Excludes, Exit, Flag, ParseError

from cloudfall.ansible_api import (
    AnsibleReadError,
    InventorySource,
    inventory_from_config,
    read_inventory,
)
from cloudfall.ansible_reader import FleetRead, read_fleet
from cloudfall.audit import AuditStatus, audit_inventory
from cloudfall.catalog import (
    CATALOG_DIRECTORY,
    ERROR_OPERATION_UNDECLARED,
    OperationCatalog,
    load_catalog,
)
from cloudfall.commands import CLI_COMMANDS
from cloudfall.decision import DECISION_DIRECTORY, DecisionError, DecisionStore
from cloudfall.domain import ResourceId
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.operations import UtcTimestamp, build_operations_view
from cloudfall.operator import ERROR_PROPOSAL_MISSING, OperatorError, ProposalStore
from cloudfall.project import (
    PROJECT_DIRECTORY_VARIABLE,
    ProjectError,
    is_project,
    project_path,
    resolve_project_directory,
)
from cloudfall.resources import default_schema_directory
from cloudfall.secrets import load_environment_receipts
from cloudfall.service_evidence import (
    DeploymentReceiptSet,
    DomainObservationSet,
    load_deployment_receipts,
    load_domain_observations,
)
from cloudfall.validation import (
    ConfigValidationError,
    SchemaCatalog,
    ValidatedConfig,
    validate_config,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

app = App(
    "cloudfall",
    version=version("cloudfall"),
    description=(
        "Run a fleet from validated config, and keep the record of what an "
        "agent did to it"
    ),
)
app.scalar(
    ResourceId,
    parse=ResourceId.from_boundary,
    pattern=r"[a-z][a-z0-9]*(-[a-z0-9]+)*",
)
# 79 and 80 mean what they mean on cloudfall-engine; 81 and 82 stay the
# engine's (artifact build, playbook run) for the commands that wrap it.
app.exit_code(
    "PROJECT_INVALID",
    79,
    description="No project directory could be resolved",
    retryable=False,
    side_effects="none",
    suggestion=(
        f"pass --project, set {PROJECT_DIRECTORY_VARIABLE}, "
        "or run inside a project"
    ),
)
app.exit_code(
    "CONFIG_INVALID",
    80,
    description="The project's resources or evidence files failed validation",
    retryable=False,
    side_effects="none",
    suggestion="fix the file error.context names, then run the command again",
)
app.exit_code(
    "DRIFT",
    83,
    description="The fleet differs from its declared config; data holds the report",
    retryable=False,
    side_effects="none",
    suggestion="read data.servers for the checks that drifted",
)
app.exit_code(
    "UNKNOWN",
    84,
    description=(
        "The evidence is missing or stale, so the result is not known; "
        "data holds the report"
    ),
    retryable=True,
    side_effects="none",
    suggestion="collect fresh snapshots with `cloudfall observe`, then run again",
)
app.exit_code(
    "INVENTORY_UNREADABLE",
    85,
    description="The Ansible inventory could not be read as a fleet",
    retryable=False,
    side_effects="none",
)
app.exit_code(
    "RECORD_INVALID",
    86,
    description="A decision record or proposal receipt could not be read",
    retryable=False,
    side_effects="none",
)


class Payload:
    """A command's flat payload, written as ``data`` under its declared keys.

    ``command`` names the command in the operating contract; its output
    shape is this payload's schema. A key the shape marks optional is
    written as an empty object when the command has nothing for it, since
    treaty writes every key of a schema on every answer.
    """

    command: ClassVar[str]

    def __init__(self, body: Mapping[str, object]) -> None:
        """Keep the payload as the domain serialized it."""
        self.body = dict(body)


def _shape_keys(command: str) -> tuple[tuple[str, bool], ...]:
    """Return each data key of the command's output shape and if it is optional."""
    contract = next(
        entry
        for entry in CLI_COMMANDS
        if entry.program == "cloudfall" and entry.name == command
    )
    (shape,) = contract.output
    return tuple((key.name, key.optional) for key in shape.keys if key.name != "error")


def _payload_schema(cls: type[Payload]) -> dict[str, object]:
    names = [name for name, _ in _shape_keys(cls.command)]
    return {
        "type": "object",
        # The domain writes each array in the order it means: a ranking, a
        # timeline, the order checks ran in. treaty would sort them.
        "properties": {name: {"x-ordered": True} for name in names},
        "required": names,
        "additionalProperties": False,
    }


def _payload_document(payload: Payload) -> dict[str, object]:
    document = dict(payload.body)
    for name, optional in _shape_keys(payload.command):
        if optional and name not in document:
            document[name] = {}
    return document


app.output_adapter(Payload, schema=_payload_schema, dump=_payload_document)


@dataclass(frozen=True, slots=True, kw_only=True)
class SchemaArgs:
    """The option every command that validates documents shares."""

    schemas: Path = Flag(
        default=default_schema_directory(),
        description="Versioned schema directory (default: bundled schemas)",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectArgs(SchemaArgs):
    """Options every command that reads a project shares."""

    project: Path | None = Flag(
        default=None,
        description=(
            f"Project directory to run in (default: ${PROJECT_DIRECTORY_VARIABLE}, "
            "else the current directory when it is a project)"
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class FleetArgs(ProjectArgs):
    """Options of a command that also reads a team's own Ansible inventory."""

    inventory: Path | None = Flag(
        default=None,
        description=(
            "Ansible inventory to read the fleet from instead of a project "
            "(default: the inventory an ansible.cfg in the current directory names)"
        ),
    )


ONE_FLEET = Excludes("inventory", prohibited=("project",))
"""``--inventory`` and ``--project`` name two fleets: a command reads one."""


def _inside_project(value: Path, flag: str) -> None:
    """Refuse a relative path that climbs out of the project directory."""
    try:
        project_path(str(value))
    except ValueError as error:
        raise ParseError(str(error), context={"flag": flag}) from error


@dataclass(frozen=True, slots=True)
class Fleet:
    """The fleet a command reads: a validated project, or a team's inventory.

    Relative paths resolve against ``directory``: the project, or for an
    inventory the directory the command was started in, beside its
    playbooks. Nothing changes the working directory.
    """

    directory: Path
    state: ValidatedConfig
    read: FleetRead | None = None

    @classmethod
    def acquire(cls, args: ProjectArgs, _ctx: Ctx) -> Self:
        """Resolve and validate the fleet before the handler runs."""
        try:
            source = _inventory_source(args)
            if source is not None:
                read = read_fleet(read_inventory(source), args.schemas)
                return cls(Path.cwd(), read.config, read)
            directory = resolve_project_directory(
                args.project, os.environ, Path.cwd()
            )
            return cls(directory, validate_config(directory, args.schemas))
        except ProjectError as error:
            raise Exit.PROJECT_INVALID(
                error.detail, context={"code": error.code}
            ) from error
        except AnsibleReadError as error:
            raise Exit.INVENTORY_UNREADABLE(
                error.detail, context={"code": error.code}
            ) from error
        except ConfigValidationError as error:
            raise _config_invalid(error) from error

    def path(self, value: Path) -> Path:
        """Resolve a relative runtime path against the fleet's directory."""
        return value if value.is_absolute() else self.directory / value

    @property
    def inventory(self) -> PlatformInventory:
        """The typed, secret-free inventory the config declares."""
        return PlatformInventory.from_state(self.state)


def _inventory_source(args: ProjectArgs) -> InventorySource | None:
    """Return the Ansible inventory this run reads, if any.

    Precedence: ``--inventory``, then an ``ansible.cfg`` in the current
    directory when no project was asked for and the directory is no project.
    Only a command that declares ``--inventory`` reads one.
    """
    if not isinstance(args, FleetArgs):
        return None
    if args.inventory is not None:
        return InventorySource.from_boundary(args.inventory)
    if args.project is not None or os.environ.get(PROJECT_DIRECTORY_VARIABLE):
        return None
    if is_project(Path.cwd()):
        return None
    return inventory_from_config(Path.cwd())


def _config_invalid(error: ConfigValidationError) -> Exception:
    return Exit.CONFIG_INVALID(error.issue.message, context=error.issue.as_dict())


def _record_invalid(error: DecisionError | OperatorError) -> Exception:
    message = error.detail if isinstance(error, DecisionError) else error.message
    if isinstance(error, OperatorError) and error.code == ERROR_PROPOSAL_MISSING:
        return Exit.NOT_FOUND(message, context={"code": error.code})
    return Exit.RECORD_INVALID(message, context={"code": error.code})


# config validate, inventory show


config = app.group("config", description="Operate on the config")
inventory = app.group("inventory", description="Query the validated platform inventory")


class ValidatedPayload(Payload):
    """``config validate``: the resources the config declares, by kind."""

    command = "config validate"


@config.command(
    "validate",
    description="Validate every resource against the schemas and cross-references",
    danger_level="safe",
    exit_codes=["PROJECT_INVALID", "CONFIG_INVALID"],
    examples=[("Validate the project in this directory", "cloudfall config validate")],
)
def config_validate(_args: ProjectArgs, _ctx: Ctx, fleet: Fleet) -> ValidatedPayload:
    """Answer the validation summary of the resolved project."""
    return ValidatedPayload(fleet.state.as_dict())


class InventoryPayload(Payload):
    """``inventory show``: the inventory, and how it was read from Ansible."""

    command = "inventory show"


@inventory.command(
    "show",
    description=(
        "Show the typed, secret-free platform inventory; data.ansible says "
        "how it was read from an Ansible inventory, empty for a project"
    ),
    danger_level="safe",
    exit_codes=[
        "PROJECT_INVALID",
        "CONFIG_INVALID",
        "INVENTORY_UNREADABLE",
    ],
    requires=[ONE_FLEET],
    examples=[
        ("Show the project's inventory", "cloudfall inventory show"),
        (
            "Read the fleet from a team's Ansible inventory",
            "cloudfall inventory show --inventory hosts.yml",
        ),
    ],
)
def inventory_show(_args: FleetArgs, _ctx: Ctx, fleet: Fleet) -> InventoryPayload:
    """Answer the inventory the fleet declares."""
    body: dict[str, object] = {
        "status": "ok",
        "inventory": fleet.inventory.as_dict(),
    }
    if fleet.read is not None:
        body["ansible"] = fleet.read.as_dict()
    return InventoryPayload(body)


# operations list, show, decisions


operations = app.group(
    "operations", description="Read the catalog of operations an agent may run"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RepositoryArgs(SchemaArgs):
    """Options of a command that reads the repository and nothing else."""

    repository: Path | None = Flag(
        default=None,
        description=(
            "Repository holding the operations directory and the playbooks it "
            "declares (default: the current directory)"
        ),
    )

    @property
    def root(self) -> Path:
        """The repository, the current directory by default."""
        return self.repository if self.repository is not None else Path.cwd()


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogArgs(RepositoryArgs):
    """Options of a command that reads the operation catalog."""

    operations: Path = Flag(
        default=Path(CATALOG_DIRECTORY),
        description=(
            "Directory holding the operation documents "
            f"(default: {CATALOG_DIRECTORY})"
        ),
    )

    def __post_init__(self) -> None:
        """Keep a relative catalog directory inside the repository."""
        _inside_project(self.operations, "operations")

    def catalog(self) -> OperationCatalog:
        """Load and validate the catalog."""
        try:
            return load_catalog(self.root, self.schemas, self.root / self.operations)
        except ConfigValidationError as error:
            raise _config_invalid(error) from error


@dataclass(frozen=True, slots=True, kw_only=True)
class ShowOperationArgs(CatalogArgs):
    """Arguments of ``operations show``."""

    operation: ResourceId = Arg(description="Operation id")


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionsArgs(RepositoryArgs):
    """Arguments of ``operations decisions``."""

    decisions: Path = Flag(
        default=Path(DECISION_DIRECTORY),
        description=(
            f"Directory holding the decision records (default: {DECISION_DIRECTORY})"
        ),
    )

    def __post_init__(self) -> None:
        """Keep a relative record directory inside the repository."""
        _inside_project(self.decisions, "decisions")


class CatalogPayload(Payload):
    """``operations list``: every declared operation and the risk counts."""

    command = "operations list"


class OperationPayload(Payload):
    """``operations show``: one declared operation in full."""

    command = "operations show"


class DecisionsPayload(Payload):
    """``operations decisions``: every decision record, oldest first."""

    command = "operations decisions"


@operations.command(
    "list",
    description="List every declared operation with its risk level",
    danger_level="safe",
    exit_codes=["CONFIG_INVALID"],
    examples=[("List the operations an agent may run", "cloudfall operations list")],
)
def operations_list(args: CatalogArgs, _ctx: Ctx) -> CatalogPayload:
    """Answer the catalog, which needs no fleet and no project."""
    return CatalogPayload(args.catalog().as_dict())


@operations.command(
    "show",
    description="Show one declared operation in full",
    danger_level="safe",
    exit_codes=["CONFIG_INVALID", "NOT_FOUND"],
    examples=[("Show one operation", "cloudfall operations show restart-nginx")],
)
def operations_show(args: ShowOperationArgs, _ctx: Ctx) -> OperationPayload:
    """Answer one operation, or say it is not declared."""
    try:
        operation = args.catalog().get(args.operation)
    except ConfigValidationError as error:
        if error.issue.code == ERROR_OPERATION_UNDECLARED:
            raise Exit.NOT_FOUND(
                error.issue.message, context=error.issue.as_dict()
            ) from error
        raise _config_invalid(error) from error
    return OperationPayload({"status": "ok", "operation": operation.as_dict()})


@operations.command(
    "decisions",
    description="List the decision records this repository holds",
    danger_level="safe",
    exit_codes=["RECORD_INVALID"],
    examples=[("List the decision records", "cloudfall operations decisions")],
)
def operations_decisions(args: DecisionsArgs, _ctx: Ctx) -> DecisionsPayload:
    """Answer every decision record: the record outlives the catalog."""
    store = DecisionStore(
        directory=args.root / args.decisions, catalog=SchemaCatalog(args.schemas)
    )
    try:
        decisions = [decision.as_document() for decision in store.list()]
    except DecisionError as error:
        raise _record_invalid(error) from error
    return DecisionsPayload(
        {"status": "ok", "directory": str(store.directory), "decisions": decisions}
    )


# operator list, show


operator = app.group(
    "operator", description="Read and act on the operator's proposal receipts"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposalArgs(ProjectArgs):
    """Options of a command that reads the operator's proposal receipts."""

    proposals: Path = Flag(
        default=Path("tmp/operator/proposals"),
        description="Proposal receipt directory (default: tmp/operator/proposals)",
    )

    def __post_init__(self) -> None:
        """Keep a relative receipt directory inside the project."""
        _inside_project(self.proposals, "proposals")

    def store(self, fleet: Fleet) -> ProposalStore:
        """Open the receipt store under the fleet's directory."""
        return ProposalStore(
            directory=fleet.path(self.proposals),
            catalog=SchemaCatalog(self.schemas),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ShowProposalArgs(ProposalArgs):
    """Arguments of ``operator show``."""

    proposal: ResourceId = Arg(description="Proposal id")


class ProposalsPayload(Payload):
    """``operator list``: every proposal receipt, oldest first."""

    command = "operator list"


class ProposalPayload(Payload):
    """``operator show``: one proposal receipt."""

    command = "operator show"


@operator.command(
    "list",
    description="List proposal receipts, oldest first",
    danger_level="safe",
    exit_codes=["PROJECT_INVALID", "CONFIG_INVALID", "RECORD_INVALID"],
    examples=[("List the operator's proposals", "cloudfall operator list")],
)
def operator_list(args: ProposalArgs, _ctx: Ctx, fleet: Fleet) -> ProposalsPayload:
    """Answer every proposal receipt the operator wrote."""
    try:
        proposals = [proposal.as_document() for proposal in args.store(fleet).list()]
    except OperatorError as error:
        raise _record_invalid(error) from error
    return ProposalsPayload({"status": "ok", "proposals": proposals})


@operator.command(
    "show",
    description="Show one proposal receipt",
    danger_level="safe",
    exit_codes=["PROJECT_INVALID", "CONFIG_INVALID", "RECORD_INVALID", "NOT_FOUND"],
    examples=[("Show one proposal", "cloudfall operator show nginx-down-20260101")],
)
def operator_show(args: ShowProposalArgs, _ctx: Ctx, fleet: Fleet) -> ProposalPayload:
    """Answer one proposal receipt, or say there is none."""
    try:
        proposal = args.store(fleet).load(args.proposal)
    except OperatorError as error:
        raise _record_invalid(error) from error
    return ProposalPayload({"status": "ok", "proposal": proposal.as_document()})


# audit, services status


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditArgs(FleetArgs):
    """Arguments of ``audit``."""

    observed: Path = Flag(
        description="Directory containing observed-server JSON snapshots"
    )
    env_receipts: Path = Flag(
        default=Path("tmp/env-receipts"),
        description=(
            "Rendered environment receipt directory used for env-file drift "
            "checks (default: tmp/env-receipts)"
        ),
    )

    def __post_init__(self) -> None:
        """Keep relative evidence paths inside the project."""
        _inside_project(self.observed, "observed")
        _inside_project(self.env_receipts, "env-receipts")


class AuditPayload(Payload):
    """``audit``: the desired-versus-observed report."""

    command = "audit"


@app.command(
    "audit",
    description=(
        "Compare the declared config with observed server snapshots; drift and "
        "unknown evidence exit non-zero with the report in data"
    ),
    danger_level="safe",
    exit_codes=[
        "PROJECT_INVALID",
        "CONFIG_INVALID",
        "INVENTORY_UNREADABLE",
        "DRIFT",
        "UNKNOWN",
    ],
    requires=[ONE_FLEET],
    examples=[
        (
            "Audit the fleet against fresh snapshots",
            "cloudfall audit --observed tmp/observed",
        ),
    ],
)
def audit(args: AuditArgs, _ctx: Ctx, fleet: Fleet) -> AuditPayload:
    """Answer the audit report; a negative verdict is its own exit code."""
    try:
        observations = load_observations(fleet.path(args.observed), args.schemas)
        receipts = load_environment_receipts(
            fleet.path(args.env_receipts), args.schemas
        )
    except ConfigValidationError as error:
        raise _config_invalid(error) from error
    report = audit_inventory(fleet.inventory, observations, receipts)
    payload = AuditPayload(report.as_dict())
    if report.status is AuditStatus.DRIFT:
        message = "the fleet differs from its declared config"
        raise Exit.DRIFT(message, data=payload)
    if report.status is AuditStatus.UNKNOWN:
        message = "the evidence is missing or stale, so drift is not known"
        raise Exit.UNKNOWN(message, data=payload)
    return payload


services = app.group(
    "services", description="Inspect and report public service lifecycles"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ServicesStatusArgs(ProjectArgs):
    """Arguments of ``services status``."""

    observed: Path = Flag(
        description="Directory containing observed-server JSON snapshots"
    )
    service_observed: Path = Flag(
        default=Path("tmp/observed-services"),
        description="Domain observation directory (default: tmp/observed-services)",
    )
    deployments: Path = Flag(
        default=Path("tmp/deployments"),
        description="Deployment receipt directory (default: tmp/deployments)",
    )

    def __post_init__(self) -> None:
        """Keep relative evidence paths inside the project."""
        _inside_project(self.observed, "observed")
        _inside_project(self.service_observed, "service-observed")
        _inside_project(self.deployments, "deployments")


class ServicesPayload(Payload):
    """``services status``: each public service's lifecycle."""

    command = "services status"


@services.command(
    "status",
    description="Derive service lifecycle status from current evidence",
    danger_level="safe",
    exit_codes=["PROJECT_INVALID", "CONFIG_INVALID"],
    examples=[
        (
            "Report each service from fresh snapshots",
            "cloudfall services status --observed tmp/observed",
        ),
    ],
)
def services_status(
    args: ServicesStatusArgs, _ctx: Ctx, fleet: Fleet
) -> ServicesPayload:
    """Answer each declared service's lifecycle from the evidence on disk."""
    deployments = fleet.path(args.deployments)
    domains = fleet.path(args.service_observed)
    try:
        view = build_operations_view(
            fleet.inventory,
            load_observations(fleet.path(args.observed), args.schemas),
            load_deployment_receipts(deployments, args.schemas)
            if deployments.is_dir()
            else DeploymentReceiptSet.empty(),
            load_domain_observations(domains, args.schemas)
            if domains.is_dir()
            else DomainObservationSet.empty(),
            generated_at=UtcTimestamp.now(),
        )
    except ConfigValidationError as error:
        raise _config_invalid(error) from error
    return ServicesPayload(
        {"status": "ok", "services": [domain.as_dict() for domain in view.domains]}
    )
