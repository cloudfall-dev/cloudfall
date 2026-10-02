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

import errno
import os
import shutil
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self, cast

from treaty import (
    App,
    Arg,
    Ctx,
    Excludes,
    Exit,
    Flag,
    FormatRenderer,
    Out,
    ParseError,
    SideEffect,
    Subprocess,
    Timeout,
)

from cloudfall.agent_tools import AgentConfig
from cloudfall.ansible_api import (
    AnsibleReadError,
    InventorySource,
    inventory_from_config,
    read_inventory,
)
from cloudfall.ansible_reader import FleetRead, read_fleet
from cloudfall.audit import AuditStatus, audit_inventory
from cloudfall.authoring import (
    ERROR_KEY_FILE_MISSING,
    ERROR_PROJECT_WRITE_FAILED,
    ERROR_RESOURCE_EXISTS,
    AddResult,
    AuthoringError,
    ServerOptions,
    ServerTypeOptions,
    SshKeyOptions,
    add_server,
    add_server_type,
    add_ssh_key,
)
from cloudfall.catalog import (
    CATALOG_DIRECTORY,
    ERROR_OPERATION_UNDECLARED,
    OperationCatalog,
    load_catalog,
)
from cloudfall.commands import CLI_COMMANDS
from cloudfall.dashboard import build_dashboard
from cloudfall.decision import (
    CHECK_TIMEOUT_SECONDS,
    DECISION_DIRECTORY,
    ERROR_DECISION_MISSING,
    ApprovalRequest,
    CheckRunner,
    DecisionError,
    DecisionStatus,
    DecisionStore,
    ProposalRequest,
    Targets,
    approve,
    propose,
)
from cloudfall.domain import (
    ConnectionAddress,
    Hostname,
    LinuxUser,
    ReleaseId,
    ResourceId,
    ResourceKind,
    TcpPort,
)
from cloudfall.importer import (
    ImportTargets,
    RenderImportError,
    import_render_blueprint,
)
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import (
    ERROR_EXECUTION_FAILED,
    STEP_TIMEOUT_SECONDS,
    DeployOptions,
    EngineContext,
    ExecutionStep,
    LifecycleError,
    LifecyclePreview,
    StepRun,
    StepRunner,
    backup_service,
    deploy,
    health,
    migrate_data,
    preview_data_migration,
    preview_deploy,
    preview_restart,
    preview_rollback,
    restart,
    rollback,
    verify_backup,
)
from cloudfall.migrate import MigrateError, MigrateOptions, execute_migration
from cloudfall.observation import load_observations
from cloudfall.observe import (
    ObservationRequest,
    ObserveError,
    PlaybookRun,
    collect_observations,
    team_configuration,
)
from cloudfall.operations import FleetOperations, UtcTimestamp, build_operations_view
from cloudfall.operator import (
    ERROR_PROPOSAL_MISSING,
    AlertFeed,
    ApproveOptions,
    OperatorError,
    ProposalStatus,
    ProposalStore,
    TriggerKind,
    alert_resolution_verifier,
    drift_resolution_verifier,
    engine_auditor,
    engine_executor,
    gateway_feed,
)
from cloudfall.operator import approve as approve_proposal
from cloudfall.project import (
    PROJECT_DIRECTORY_VARIABLE,
    InitOptions,
    ProjectDescription,
    ProjectError,
    ProjectName,
    init_project,
    initialize_git,
    is_project,
    project_path,
    resolve_installed_version,
    resolve_project_directory,
)
from cloudfall.render_api import (
    ERROR_API_UNREACHABLE,
    HttpRenderApiClient,
    import_render_api,
    read_api_key,
)
from cloudfall.resources import default_engine_directory, default_schema_directory
from cloudfall.secrets import (
    ERROR_DECRYPT_FAILED,
    ERROR_SOPS_MISSING,
    SecretsError,
    SopsSecretProvider,
    load_environment_receipts,
    render_environment,
)
from cloudfall.service_evidence import (
    DeploymentReceiptSet,
    DomainObservationSet,
    EvidenceTimestamp,
    SocketDomainNetworkClient,
    inspect_domains,
    load_deployment_receipts,
    load_domain_observations,
)
from cloudfall.validation import (
    ConfigValidationError,
    SchemaCatalog,
    ValidatedConfig,
    validate_config,
)
from cloudfall.why import WhyError, WhyQuery, answer, render_why_document

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

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
PROJECT_MARKERS = tuple(kind.directory for kind in ResourceKind)
"""A project holds at least one resource kind directory, such as ``servers``."""
GIT_TIMEOUT_SECONDS = 30
"""How long one git step of ``init`` may take."""
_NOT_WRITABLE = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})

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
    written as ``null`` when the command has nothing for it, since
    treaty writes every key of a schema on every answer.
    """

    command: ClassVar[str]
    writes: ClassVar[bool] = False
    """The command changes state, so the payload carries ``effect`` (REQ-C-003)."""

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
    # A command with two shapes (a plan without --yes, a result with it)
    # writes one object: a key not in every shape is optional, so null.
    names: list[str] = []
    for shape in contract.output:
        names.extend(
            key.name
            for key in shape.keys
            if key.name != "error" and key.name not in names
        )
    return tuple(
        (
            name,
            any(
                all(key.name != name for key in shape.keys)
                or any(key.name == name and key.optional for key in shape.keys)
                for shape in contract.output
            ),
        )
        for name in names
    )


def _payload_schema(cls: type[Payload]) -> dict[str, object]:
    names = [name for name, _ in _shape_keys(cls.command)]
    if cls.writes:
        names.append("effect")
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
            document[name] = None
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
    source: InventorySource | None = None
    """The team's Ansible inventory the fleet was read from, if any."""

    @classmethod
    def acquire(cls, args: ProjectArgs, _ctx: Ctx) -> Self:
        """Resolve and validate the project before the handler runs."""
        return cls._project(args)

    @classmethod
    def _project(cls, args: ProjectArgs) -> Self:
        try:
            directory = resolve_project_directory(
                args.project, os.environ, Path.cwd()
            )
            return cls(directory, validate_config(directory, args.schemas))
        except ProjectError as error:
            raise Exit.PROJECT_INVALID(
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


@dataclass(frozen=True, slots=True)
class InventoryFleet(Fleet):
    """A fleet a command may also read from a team's own Ansible inventory.

    Only the commands that declare ``--inventory`` take it, so only they can
    fail on an unreadable inventory.
    """

    @classmethod
    # Narrower than Fleet.acquire on purpose: only --inventory commands take it.
    def acquire(  # type: ignore[override]
        cls, args: FleetArgs, _ctx: Ctx
    ) -> Self:
        """Read the team's inventory when one applies, else the project."""
        try:
            source = _inventory_source(args)
            if source is None:
                return cls._project(args)
            read = read_fleet(read_inventory(source), args.schemas)
        except AnsibleReadError as error:
            raise Exit.INVENTORY_UNREADABLE(
                error.detail, context={"code": error.code}
            ) from error
        except ConfigValidationError as error:
            raise _config_invalid(error) from error
        return cls(Path.cwd(), read.config, read, source)


def _inventory_source(args: FleetArgs) -> InventorySource | None:
    """Return the Ansible inventory this run reads, if any.

    Precedence: ``--inventory``, then an ``ansible.cfg`` in the current
    directory when no project was asked for and the directory is no project.
    """
    if args.inventory is not None:
        return InventorySource.from_boundary(args.inventory)
    if args.project is not None or os.environ.get(PROJECT_DIRECTORY_VARIABLE):
        return None
    if is_project(Path.cwd()):
        return None
    return inventory_from_config(Path.cwd())


def _config_invalid(error: ConfigValidationError) -> Exception:
    return Exit.CONFIG_INVALID(error.issue.message, context=error.issue.as_dict())


def _record_invalid(error: DecisionError) -> Exception:
    return Exit.RECORD_INVALID(error.detail, context={"code": error.code})


def _proposal_failed(error: OperatorError) -> Exception:
    if error.code == ERROR_PROPOSAL_MISSING:
        return Exit.NOT_FOUND(error.message, context={"code": error.code})
    return Exit.RECORD_INVALID(error.message, context={"code": error.code})


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
        "how it was read from an Ansible inventory, null for a project"
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
def inventory_show(
    _args: FleetArgs, _ctx: Ctx, fleet: InventoryFleet
) -> InventoryPayload:
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
    exit_codes=["PROJECT_INVALID", "CONFIG_INVALID", "RECORD_INVALID", "NOT_FOUND"],
    examples=[("List the operator's proposals", "cloudfall operator list")],
)
def operator_list(args: ProposalArgs, _ctx: Ctx, fleet: Fleet) -> ProposalsPayload:
    """Answer every proposal receipt the operator wrote."""
    try:
        proposals = [proposal.as_document() for proposal in args.store(fleet).list()]
    except OperatorError as error:
        raise _proposal_failed(error) from error
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
        raise _proposal_failed(error) from error
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
def audit(args: AuditArgs, _ctx: Ctx, fleet: InventoryFleet) -> AuditPayload:
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
class EvidenceArgs(ProjectArgs):
    """Options of a command that reads the evidence a fleet's view is built from."""

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

    def operations_view(self, fleet: Fleet) -> FleetOperations:
        """Build the fleet's operations view from the evidence on disk."""
        deployments = fleet.path(self.deployments)
        domains = fleet.path(self.service_observed)
        try:
            return build_operations_view(
                fleet.inventory,
                load_observations(fleet.path(self.observed), self.schemas),
                load_deployment_receipts(deployments, self.schemas)
                if deployments.is_dir()
                else DeploymentReceiptSet.empty(),
                load_domain_observations(domains, self.schemas)
                if domains.is_dir()
                else DomainObservationSet.empty(),
                generated_at=UtcTimestamp.now(),
            )
        except ConfigValidationError as error:
            raise _config_invalid(error) from error


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
def services_status(args: EvidenceArgs, _ctx: Ctx, fleet: Fleet) -> ServicesPayload:
    """Answer each declared service's lifecycle from the evidence on disk."""
    view = args.operations_view(fleet)
    return ServicesPayload(
        {"status": "ok", "services": [domain.as_dict() for domain in view.domains]}
    )


def _not_writable(error: OSError) -> Exception:
    """Report a path the command must write as read-only or not ours."""
    message = f"cannot write {error.filename}: {error.strerror}"
    return Exit.PERMISSION_DENIED(message, context={"code": "path_not_writable"})


# init


@dataclass(frozen=True, slots=True, kw_only=True)
class InitArgs:
    """Arguments of ``init``, which runs before any project exists."""

    directory: Path = Arg(
        default=Path(),
        description=(
            "Project directory to create; must be empty or absent (default: .)"
        ),
    )
    name: str | None = Flag(
        default=None,
        description=(
            "Package name written to pyproject.toml (default: the directory name)"
        ),
    )
    description: str | None = Flag(
        default=None,
        description=(
            "One line saying what the project manages, written to the README and "
            "pyproject.toml"
        ),
    )

    def __post_init__(self) -> None:
        """Refuse a name or description the project files cannot hold."""
        try:
            self.project_name()
            self.project_description()
        except ValueError as error:
            raise ParseError(str(error)) from error

    def project_name(self) -> ProjectName:
        """Return the package name, the directory's by default."""
        if self.name is None:
            return ProjectName.from_directory(self.directory)
        return ProjectName.from_boundary(self.name)

    def project_description(self) -> ProjectDescription | None:
        """Return the one-line description, when one was given."""
        if self.description is None:
            return None
        return ProjectDescription.from_boundary(self.description)


@dataclass(frozen=True, slots=True)
class Scaffolded:
    """A new project, the files written into it, and what to run next."""

    effect: str
    status: str
    project: dict[str, object] = Out(ordered=True)
    files: list[str] = Out(ordered=True)
    next: list[str] = Out(ordered=True)


@app.command(
    "init",
    description="Create a new project: fleet, applications, and operations",
    danger_level="mutating",
    # Two git steps of up to GIT_TIMEOUT_SECONDS each, then local files.
    timeout=3 * GIT_TIMEOUT_SECONDS,
    exit_codes=["PRECONDITION", "PERMISSION_DENIED"],
    subprocess=Subprocess("git"),
    required_tools={"git": "2.24.0"},
    examples=[
        ("Create a project in a new directory", "cloudfall init fleet"),
        (
            "Name and describe it",
            "cloudfall init fleet --name acme-fleet --description 'Acme servers'",
        ),
    ],
)
def init(args: InitArgs, ctx: Ctx) -> Scaffolded:
    """Lay out a new project in an empty or absent directory."""
    try:
        options = InitOptions(
            directory=args.directory,
            name=args.project_name(),
            version=resolve_installed_version(),
            description=args.project_description(),
        )
        scaffold = init_project(
            options,
            initialize_git=partial(
                initialize_git,
                run=partial(ctx.run, timeout=Timeout(GIT_TIMEOUT_SECONDS), check=False),
            ),
        )
    except ProjectError as error:
        raise Exit.PRECONDITION(error.detail, context={"code": error.code}) from error
    except OSError as error:
        if error.errno not in _NOT_WRITABLE:
            raise
        raise _not_writable(error) from error
    body = scaffold.as_dict()
    return Scaffolded(
        effect="created",
        status="ok",
        project=dict(cast("Mapping[str, object]", body["project"])),
        files=list(scaffold.files),
        next=list(cast("list[str]", body["next"])),
    )


# add ssh-key, server-type, server


add = app.group("add", description="Write a fleet resource into the project")


@dataclass(frozen=True, slots=True)
class ProjectDirectory:
    """The project a command writes into, resolved but not yet validated."""

    path: Path

    @classmethod
    def acquire(cls, args: ProjectArgs, _ctx: Ctx) -> Self:
        """Resolve the project directory before the handler runs."""
        try:
            return cls(resolve_project_directory(args.project, os.environ, Path.cwd()))
        except ProjectError as error:
            raise Exit.PROJECT_INVALID(
                error.detail, context={"code": error.code}
            ) from error


@dataclass(frozen=True, slots=True, kw_only=True)
class AddArgs(ProjectArgs):
    """Options every ``add`` command shares."""

    description: str | None = Flag(
        default=None, description="One line saying what the resource is for"
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class AddSshKeyArgs(AddArgs):
    """Arguments of ``add ssh-key``."""

    key_file: Path = Arg(
        description="Public key file, such as ~/.ssh/id_ed25519.pub",
        # A path to a public key: nothing in it or in its name is secret.
        secret=False,
    )
    owner: ResourceId = Flag(description="Who the key belongs to")
    id: ResourceId | None = Flag(
        default=None, description="Resource id (default: the owner)"
    )
    environment: ResourceId = Flag(
        default=ResourceId.from_boundary("production"),
        description="Environment the key is for (default: production)",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class AddServerTypeArgs(AddArgs):
    """Arguments of ``add server-type``."""

    id: ResourceId = Arg(
        description="Resource id, such as debian-application"
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class AddServerArgs(AddArgs):
    """Arguments of ``add server``."""

    id: ResourceId = Arg(
        description="Resource id, such as h1"
    )
    address: str = Flag(description="IP address or hostname to connect to")
    type: ResourceId = Flag(
        default=ResourceId.from_boundary("debian-application"),
        description=(
            "Server type id; created when missing (default: debian-application)"
        ),
    )
    environment: ResourceId = Flag(
        default=ResourceId.from_boundary("production"),
        description="Environment the server belongs to (default: production)",
    )
    hostname: str | None = Flag(
        default=None,
        description=(
            "Hostname (default: the address when it is a hostname, else the id)"
        ),
    )
    ssh_user: str = Flag(default="root", description="SSH user (default: root)")
    ssh_port: int = Flag(default=22, description="SSH port (default: 22)")

    def __post_init__(self) -> None:
        """Refuse an address, user, port, or hostname a server cannot have."""
        try:
            self.options()
        except (TypeError, ValueError) as error:
            raise ParseError(str(error)) from error

    def options(self) -> ServerOptions:
        """Return the server to declare, as typed values."""
        return ServerOptions(
            resource_id=self.id,
            address=ConnectionAddress.from_boundary(self.address),
            server_type=self.type,
            environment=self.environment,
            ssh_user=LinuxUser.from_boundary(self.ssh_user),
            ssh_port=TcpPort.from_boundary(self.ssh_port),
            hostname=(
                Hostname.from_boundary(self.hostname)
                if self.hostname is not None
                else None
            ),
            description=self.description,
        )


@dataclass(frozen=True, slots=True)
class Added:
    """The resource files one ``add`` wrote into the project."""

    effect: str
    status: str
    project: str
    added: list[dict[str, object]] = Out(ordered=True)


def _added(result: AddResult) -> Added:
    body = result.as_dict()
    return Added(
        effect="created",
        status="ok",
        project=str(result.project),
        added=list(cast("list[dict[str, object]]", body["added"])),
    )


def _authoring_failed(error: AuthoringError) -> Exception:
    context = {"code": error.code}
    if error.code == ERROR_RESOURCE_EXISTS:
        return Exit.CONFLICT(error.detail, context=context)
    if error.code == ERROR_KEY_FILE_MISSING:
        return Exit.NOT_FOUND(error.detail, context=context)
    if error.code == ERROR_PROJECT_WRITE_FAILED:
        return Exit.PERMISSION_DENIED(error.detail, context=context)
    return Exit.PRECONDITION(error.detail, context=context)


_ADD_EXIT_CODES = [
    "PROJECT_INVALID",
    "CONFIG_INVALID",
    "CONFLICT",
    "PERMISSION_DENIED",
    "PRECONDITION",
    "NOT_FOUND",
]


@add.command(
    "ssh-key",
    description="Declare an SSH public key read from a file",
    danger_level="mutating",
    timeout=30,
    supports_raw_payload=True,
    exit_codes=_ADD_EXIT_CODES,
    examples=[
        (
            "Declare your own key",
            "cloudfall add ssh-key ~/.ssh/id_ed25519.pub --owner alice",
        ),
    ],
)
def add_ssh_key_command(
    args: AddSshKeyArgs, _ctx: Ctx, project: ProjectDirectory
) -> Added:
    """Write one SshPublicKey resource into the project."""
    options = SshKeyOptions(
        key_path=args.key_file.expanduser(),
        owner=args.owner,
        environment=args.environment,
        resource_id=args.id,
        description=args.description,
    )
    try:
        return _added(add_ssh_key(project.path, options, args.schemas))
    except AuthoringError as error:
        raise _authoring_failed(error) from error
    except ConfigValidationError as error:
        raise _config_invalid(error) from error


@add.command(
    "server-type",
    description="Declare a server type from the bundled Debian 13 baseline",
    danger_level="mutating",
    timeout=30,
    supports_raw_payload=True,
    exit_codes=_ADD_EXIT_CODES,
    examples=[
        ("Declare the baseline type", "cloudfall add server-type debian-application"),
    ],
)
def add_server_type_command(
    args: AddServerTypeArgs, _ctx: Ctx, project: ProjectDirectory
) -> Added:
    """Write one ServerType resource into the project."""
    options = ServerTypeOptions(resource_id=args.id, description=args.description)
    try:
        return _added(add_server_type(project.path, options, args.schemas))
    except AuthoringError as error:
        raise _authoring_failed(error) from error
    except ConfigValidationError as error:
        raise _config_invalid(error) from error


@add.command(
    "server",
    description="Declare a server; creates its server type when missing",
    danger_level="mutating",
    timeout=30,
    supports_raw_payload=True,
    exit_codes=_ADD_EXIT_CODES,
    examples=[
        ("Declare a server by address", "cloudfall add server h1 --address 192.0.2.10"),
    ],
)
def add_server_command(
    args: AddServerArgs, _ctx: Ctx, project: ProjectDirectory
) -> Added:
    """Write one Server resource, and its ServerType when the project lacks it."""
    try:
        return _added(add_server(project.path, args.options(), args.schemas))
    except AuthoringError as error:
        raise _authoring_failed(error) from error
    except ConfigValidationError as error:
        raise _config_invalid(error) from error


# dashboard build, services inspect


dashboard = app.group("dashboard", description="Build operations dashboard artifacts")


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardBuildArgs(EvidenceArgs):
    """Arguments of ``dashboard build``."""

    output_dir: Path = Flag(
        default=Path("tmp/dashboard"),
        description="Dashboard output directory (default: tmp/dashboard)",
    )

    def __post_init__(self) -> None:
        """Keep relative evidence and output paths inside the project."""
        EvidenceArgs.__post_init__(self)
        _inside_project(self.output_dir, "output-dir")


class DashboardPayload(Payload):
    """``dashboard build``: the pages written, and the fleet health they show."""

    command = "dashboard build"


@dashboard.command(
    "build",
    description="Build a static read-only operations dashboard",
    danger_level="safe",
    timeout=120,
    supports_raw_payload=True,
    project_root=PROJECT_MARKERS,
    filesystem_side_effects=[SideEffect("{project_root}/tmp/dashboard/", "output")],
    exit_codes=["PROJECT_INVALID", "CONFIG_INVALID", "PERMISSION_DENIED"],
    examples=[
        (
            "Build the dashboard from fresh snapshots",
            "cloudfall dashboard build --observed tmp/observed",
        ),
    ],
)
def dashboard_build(
    args: DashboardBuildArgs, _ctx: Ctx, fleet: Fleet
) -> DashboardPayload:
    """Write the dashboard pages for the fleet's current evidence."""
    view = args.operations_view(fleet)
    try:
        artifacts = build_dashboard(view, fleet.path(args.output_dir))
    except OSError as error:
        if error.errno not in _NOT_WRITABLE:
            raise
        raise _not_writable(error) from error
    return DashboardPayload(
        {
            "status": "ok",
            "health": view.health.value,
            "tasks": len(view.tasks),
            "dashboard": artifacts.as_dict(),
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ServicesInspectArgs(ProjectArgs):
    """Arguments of ``services inspect``."""

    output_dir: Path = Flag(
        default=Path("tmp/observed-services"),
        description="Service observation directory (default: tmp/observed-services)",
    )

    def __post_init__(self) -> None:
        """Keep a relative output path inside the project."""
        _inside_project(self.output_dir, "output-dir")


class InspectedPayload(Payload):
    """``services inspect``: the domain observation files written."""

    command = "services inspect"


@services.command(
    "inspect",
    description="Collect DNS, TLS, origin, and public route evidence",
    danger_level="safe",
    # DNS, TLS, and HTTP probes of every declared domain, one after another.
    timeout=600,
    project_root=PROJECT_MARKERS,
    filesystem_side_effects=[
        SideEffect("{project_root}/tmp/observed-services/", "output")
    ],
    exit_codes=["PROJECT_INVALID", "CONFIG_INVALID", "PERMISSION_DENIED"],
    examples=[("Probe every declared domain", "cloudfall services inspect")],
)
def services_inspect(
    args: ServicesInspectArgs, _ctx: Ctx, fleet: Fleet
) -> InspectedPayload:
    """Probe each declared domain and write one observation per domain."""
    try:
        paths = inspect_domains(
            fleet.inventory,
            fleet.path(args.output_dir),
            SocketDomainNetworkClient(),
            observed_at=EvidenceTimestamp.now(),
        )
    except OSError as error:
        if error.errno not in _NOT_WRITABLE:
            raise
        raise _not_writable(error) from error
    return InspectedPayload(
        {"status": "ok", "observations": [str(path) for path in paths]}
    )


# Commands that change servers: health, backup run/verify, operations
# propose/approve, operator approve, migrate. Ansible runs through ctx.run,
# in the project directory as it did under argparse.


app.exit_code(
    "ENGINE_STEP_FAILED",
    87,
    description=(
        "An engine step (inventory render or playbook run) failed; hosts may be "
        "partly changed"
    ),
    retryable=False,
    side_effects="partial",
    suggestion="read error.message for the end of the step's output, fix it, rerun",
)
app.exit_code(
    "UNHEALTHY",
    88,
    description="A health check failed on a server; data holds the result",
    retryable=True,
    side_effects="none",
    suggestion=(
        "read data.detail for the failing server; restart the component or "
        "redeploy it, then probe again"
    ),
)
app.exit_code(
    "CHECK_FAILED",
    89,
    description=(
        "Check mode failed; the decision is recorded with its diff, in data"
    ),
    retryable=False,
    side_effects="none",
    suggestion="read the diff the decision cites before proposing again",
)
app.exit_code(
    "NOT_VERIFIED",
    90,
    description=(
        "The approved run failed or its verify step did not confirm it; data "
        "holds the record"
    ),
    retryable=False,
    side_effects="partial",
)


def _step_runner(ctx: Ctx, directory: Path) -> StepRunner:
    """Run each engine step through ``ctx.run``, in the project directory."""

    def run(step: ExecutionStep) -> StepRun:
        return ctx.run(
            step.argv,
            env=step.environment,
            cwd=directory,
            timeout=Timeout(STEP_TIMEOUT_SECONDS),
            check=False,
        )

    return run


def _check_runner(ctx: Ctx, directory: Path) -> CheckRunner:
    """Run check mode through ``ctx.run``, keeping its output as the diff."""

    def run(argv: Sequence[str], environment: Mapping[str, str], diff: Path) -> int:
        done = ctx.run(
            list(argv),
            env=environment,
            cwd=directory,
            timeout=Timeout(CHECK_TIMEOUT_SECONDS),
            check=False,
        )
        diff.write_text(done.stdout + done.stderr, encoding="utf-8")
        return done.returncode

    return run


def _lifecycle_failed(error: LifecycleError) -> Exception:
    context = {"code": error.code}
    if error.code == ERROR_EXECUTION_FAILED:
        return Exit.ENGINE_STEP_FAILED(error.detail, context=context)
    return Exit.PRECONDITION(error.detail, context=context)


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineArgs(ProjectArgs):
    """Options of a command that runs engine playbooks against the fleet."""

    engine: Path = Flag(
        default=default_engine_directory(),
        description="Engine directory containing ansible contracts (default: bundled)",
    )
    inventory_file: Path = Flag(
        default=Path("tmp/ansible-inventory.json"),
        description="Rendered inventory path (default: tmp/ansible-inventory.json)",
    )

    def __post_init__(self) -> None:
        """Keep a relative inventory path inside the project."""
        _inside_project(self.inventory_file, "inventory-file")

    def context(self, fleet: Fleet, ctx: Ctx) -> EngineContext:
        """Return the engine contract, with steps run through ``ctx.run``."""
        return EngineContext(
            project_directory=fleet.directory,
            schema_directory=self.schemas,
            engine_directory=self.engine,
            inventory_file=fleet.path(self.inventory_file),
            run=_step_runner(ctx, fleet.directory),
        )


# health


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthArgs(EngineArgs):
    """Arguments of ``health``."""

    component: ResourceId = Arg(description="Component to probe")


class HealthPayload(Payload):
    """``health``: the probe's result on every server of the component."""

    command = "health"


@app.command(
    "health",
    description=(
        "Probe one component's declared health check; an unhealthy result "
        "exits non-zero with the result in data"
    ),
    danger_level="safe",
    exit_codes=[
        "PROJECT_INVALID",
        "CONFIG_INVALID",
        "PRECONDITION",
        "ENGINE_STEP_FAILED",
        "UNHEALTHY",
    ],
    timeout=None,
    subprocess=Subprocess("ansible-playbook"),
    required_tools={"ansible-playbook": "2.21.0"},
    examples=[("Probe one component", "cloudfall health crm-backend")],
)
def health_command(args: HealthArgs, ctx: Ctx, fleet: Fleet) -> HealthPayload:
    """Run the health playbook and answer whether every server passed."""
    try:
        result = health(args.context(fleet, ctx), args.component)
    except LifecycleError as error:
        raise _lifecycle_failed(error) from error
    payload = HealthPayload(result.as_dict())
    if not result.healthy:
        message = f"{args.component} failed its health check"
        raise Exit.UNHEALTHY(message, data=payload)
    return payload


# backup run, verify


backup = app.group("backup", description="Run and prove declared service backups")


@dataclass(frozen=True, slots=True, kw_only=True)
class BackupArgs(EngineArgs):
    """Arguments of ``backup run`` and ``backup verify``."""

    service: ResourceId = Arg(description="Declared service to back up")
    receipts: Path = Flag(
        default=Path("tmp/backups"),
        description="Backup receipt directory (default: tmp/backups)",
    )

    def __post_init__(self) -> None:
        """Keep relative paths inside the project."""
        EngineArgs.__post_init__(self)
        _inside_project(self.receipts, "receipts")


@dataclass(frozen=True, slots=True)
class BackedUp:
    """The receipt one backup or restore check wrote."""

    effect: str
    status: str
    receipt: dict[str, object] = Out(ordered=True)
    path: Path = Path()


def _backed_up(body: Mapping[str, object]) -> BackedUp:
    return BackedUp(
        effect="created",
        status=str(body["status"]),
        receipt=dict(cast("Mapping[str, object]", body["receipt"])),
        path=Path(str(body["path"])),
    )


_BACKUP_EXIT_CODES = [
    "PROJECT_INVALID",
    "CONFIG_INVALID",
    "PRECONDITION",
    "ENGINE_STEP_FAILED",
]


@backup.command(
    "run",
    description="Run the declared backup for one service and keep its receipt",
    danger_level="mutating",
    exit_codes=_BACKUP_EXIT_CODES,
    timeout=None,
    supports_raw_payload=True,
    subprocess=Subprocess("ansible-playbook"),
    required_tools={"ansible-playbook": "2.21.0"},
    examples=[("Back up one database service", "cloudfall backup run postgresql-main")],
)
def backup_run(args: BackupArgs, ctx: Ctx, fleet: Fleet) -> BackedUp:
    """Run the backup playbook for the service."""
    try:
        body = backup_service(
            args.context(fleet, ctx), args.service, fleet.path(args.receipts)
        )
    except LifecycleError as error:
        raise _lifecycle_failed(error) from error
    return _backed_up(body)


@backup.command(
    "verify",
    description="Prove the newest backup restores for one service",
    danger_level="mutating",
    exit_codes=_BACKUP_EXIT_CODES,
    timeout=None,
    supports_raw_payload=True,
    subprocess=Subprocess("ansible-playbook"),
    required_tools={"ansible-playbook": "2.21.0"},
    examples=[
        (
            "Restore-check one database service",
            "cloudfall backup verify postgresql-main",
        ),
    ],
)
def backup_verify(args: BackupArgs, ctx: Ctx, fleet: Fleet) -> BackedUp:
    """Restore the newest backup into scratch and keep the proof."""
    try:
        body = verify_backup(
            args.context(fleet, ctx), args.service, fleet.path(args.receipts)
        )
    except LifecycleError as error:
        raise _lifecycle_failed(error) from error
    return _backed_up(body)


# operations propose, approve


@dataclass(frozen=True, slots=True, kw_only=True)
class ProposeArgs(CatalogArgs):
    """Arguments of ``operations propose``."""

    operation: ResourceId = Arg(description="Operation id")
    target: str | None = Flag(
        default=None,
        description=(
            "Host or group the operation runs against, as its target scope requires"
        ),
    )
    input: tuple[str, ...] = Flag(
        default=(),
        description="Value for one declared input, as NAME=VALUE (repeatable)",
    )
    observed: Path = Flag(
        default=Path("tmp/observed"),
        description=(
            "Snapshot directory the proposal cites as its basis (default: tmp/observed)"
        ),
    )
    decisions: Path = Flag(
        default=Path(DECISION_DIRECTORY),
        description=(
            f"Directory holding the decision records (default: {DECISION_DIRECTORY})"
        ),
    )

    def __post_init__(self) -> None:
        """Refuse a malformed input, and keep relative paths in the repository."""
        CatalogArgs.__post_init__(self)
        _inside_project(self.observed, "observed")
        _inside_project(self.decisions, "decisions")
        self.inputs()

    def inputs(self) -> dict[str, object]:
        """Return the declared inputs, parsed from ``NAME=VALUE``."""
        inputs: dict[str, object] = {}
        for entry in self.input:
            name, separator, value = entry.partition("=")
            if not separator or not name:
                message = f"input must be given as NAME=VALUE, got {entry!r}"
                raise ParseError(message, context={"flag": "input"})
            inputs[name] = value
        return inputs


@dataclass(frozen=True, slots=True, kw_only=True)
class ApproveDecisionArgs(DecisionsArgs):
    """Arguments of ``operations approve``."""

    decision: ResourceId = Arg(description="Decision id from `operations propose`")
    approver: str | None = Flag(
        default=None,
        description="Who is approving (default: the USER environment variable)",
    )
    yes: bool = Flag(
        default=False,
        confirm=True,
        description=(
            "Change the servers; without it the command shows the recorded "
            "proposal and runs nothing"
        ),
    )


@dataclass(frozen=True, slots=True)
class Decided:
    """One decision record, and what to do next when it waits for approval."""

    effect: str
    status: str
    decision: dict[str, object] = Out(ordered=True)
    next: list[str] = Out(ordered=True)


def _decision_failed(error: DecisionError) -> Exception:
    return Exit.PRECONDITION(error.detail, context={"code": error.code})


@operations.command(
    "propose",
    description=(
        "Run one operation in check mode and record what it would do; nothing "
        "on the fleet changes"
    ),
    danger_level="mutating",
    exit_codes=["CONFIG_INVALID", "NOT_FOUND", "PRECONDITION", "CHECK_FAILED"],
    timeout=None,
    subprocess=Subprocess("ansible-playbook"),
    required_tools={"ansible-playbook": "2.21.0"},
    supports_raw_payload=True,
    examples=[
        (
            "Propose a restart of nginx on one host",
            "cloudfall operations propose restart-nginx --target h1",
        ),
    ],
)
def operations_propose(args: ProposeArgs, ctx: Ctx) -> Decided:
    """Record a proposal with the diff check mode produced."""
    catalog = args.catalog()
    try:
        operation = catalog.get(args.operation)
    except ConfigValidationError as error:
        if error.issue.code == ERROR_OPERATION_UNDECLARED:
            raise Exit.NOT_FOUND(
                error.issue.message, context=error.issue.as_dict()
            ) from error
        raise _config_invalid(error) from error
    request = ProposalRequest(
        operation=operation,
        targets=Targets(scope=operation.targets, pattern=args.target),
        inputs=args.inputs(),
        repository=args.root,
        observations=args.root / args.observed,
    )
    store = DecisionStore(
        directory=args.root / args.decisions, catalog=SchemaCatalog(args.schemas)
    )
    try:
        decision = propose(request, store, run=_check_runner(ctx, args.root))
    except DecisionError as error:
        raise _decision_failed(error) from error
    decided = Decided(
        effect="created", status="ok", decision=decision.as_document(), next=[]
    )
    if decision.check.exit_code != 0:
        message = (
            f"check mode exited {decision.check.exit_code}; the decision is "
            "recorded with its diff"
        )
        raise Exit.CHECK_FAILED(message, data=decided)
    return decided


@operations.command(
    "approve",
    description=(
        "Approve one recorded proposal, run it, and verify it; without --yes it "
        "shows the proposal and runs nothing"
    ),
    danger_level="mutating",
    exit_codes=["NOT_FOUND", "PRECONDITION", "RECORD_INVALID", "NOT_VERIFIED"],
    timeout=None,
    supports_raw_payload=True,
    subprocess=Subprocess("ansible-playbook"),
    required_tools={"ansible-playbook": "2.21.0"},
    examples=[
        ("Review a proposal", "cloudfall operations approve restart-nginx-20260101"),
        ("Run it", "cloudfall operations approve restart-nginx-20260101 --yes"),
    ],
)
def operations_approve(args: ApproveDecisionArgs, ctx: Ctx) -> Decided:
    """Run what was proposed, as recorded, and record how it ended."""
    store = DecisionStore(
        directory=args.root / args.decisions, catalog=SchemaCatalog(args.schemas)
    )
    try:
        decision = store.load(args.decision)
    except DecisionError as error:
        if error.code == ERROR_DECISION_MISSING:
            raise Exit.NOT_FOUND(error.detail, context={"code": error.code}) from error
        raise _record_invalid(error) from error
    if not args.yes:
        # Without --yes the run is treaty's dry run (Flag(confirm=True)).
        return Decided(
            effect="would_update",
            status="pending",
            decision=decision.as_document(),
            next=[
                f"review the recorded diff at {decision.check.diff.path}",
                "approve with --yes to run it",
            ],
        )
    approver = args.approver if args.approver is not None else ctx.env.get("USER", "")
    try:
        approved = approve(
            ApprovalRequest(decision=decision, approver=approver, repository=args.root),
            store,
            run=_check_runner(ctx, args.root),
        )
    except DecisionError as error:
        raise _decision_failed(error) from error
    decided = Decided(
        effect="updated", status="ok", decision=approved.as_document(), next=[]
    )
    if approved.status is DecisionStatus.FAILED:
        message = "the approved run failed or its verify step did not confirm it"
        raise Exit.NOT_VERIFIED(message, data=decided)
    return decided


# operator approve


@dataclass(frozen=True, slots=True, kw_only=True)
class OperatorApproveArgs(ProposalArgs):
    """Arguments of ``operator approve``."""

    proposal: ResourceId = Arg(description="Proposal id")
    engine: Path = Flag(
        default=default_engine_directory(),
        description="Engine directory containing ansible contracts (default: bundled)",
    )
    inventory_file: Path = Flag(
        default=Path("tmp/ansible-inventory.json"),
        description="Rendered inventory path (default: tmp/ansible-inventory.json)",
    )
    observed: Path = Flag(
        default=Path("tmp/operator/observed"),
        description=(
            "Observation directory for drift verification "
            "(default: tmp/operator/observed)"
        ),
    )
    verify_timeout: float = Flag(
        default=180.0,
        description="Seconds to wait for the trigger to resolve (default: 180)",
    )
    gateway_url: str | None = Flag(
        default=None,
        description="Alerts endpoint (default: derived from the declared gateway)",
    )
    gateway_ca: Path | None = Flag(default=None, description="Gateway CA file")
    gateway_cert: Path | None = Flag(
        default=None, description="Client certificate for the gateway"
    )
    gateway_key: Path | None = Flag(
        default=None, description="Client key for the gateway", secret=False
    )

    def __post_init__(self) -> None:
        """Keep relative paths inside the project."""
        ProposalArgs.__post_init__(self)
        _inside_project(self.inventory_file, "inventory-file")
        _inside_project(self.observed, "observed")

    def context(self, fleet: Fleet, ctx: Ctx) -> EngineContext:
        """Return the engine contract, with steps run through ``ctx.run``."""
        return EngineContext(
            project_directory=fleet.directory,
            schema_directory=self.schemas,
            engine_directory=self.engine,
            inventory_file=fleet.path(self.inventory_file),
            run=_step_runner(ctx, fleet.directory),
        )

    def feed(self, fleet: Fleet) -> AlertFeed:
        """Return the gateway's alert feed, which an alert proposal needs."""
        if (
            self.gateway_ca is None
            or self.gateway_cert is None
            or self.gateway_key is None
        ):
            message = (
                "approving an alert-triggered proposal requires --gateway-ca, "
                "--gateway-cert, and --gateway-key"
            )
            raise Exit.PRECONDITION(
                message, context={"code": "operator_gateway_material_missing"}
            )
        return gateway_feed(
            fleet.inventory,
            ca_path=self.gateway_ca,
            certificate_path=self.gateway_cert,
            key_path=self.gateway_key,
            url_override=self.gateway_url,
        )


@dataclass(frozen=True, slots=True)
class Approved:
    """One proposal receipt after its approved run."""

    effect: str
    status: str
    proposal: dict[str, object] = Out(ordered=True)


@operator.command(
    "approve",
    description="Execute a proposal and verify its trigger resolves",
    danger_level="mutating",
    exit_codes=[
        "PROJECT_INVALID",
        "CONFIG_INVALID",
        "NOT_FOUND",
        "PRECONDITION",
        "RECORD_INVALID",
        "ENGINE_STEP_FAILED",
        "NOT_VERIFIED",
    ],
    timeout=None,
    supports_raw_payload=True,
    subprocess=Subprocess("ansible-playbook"),
    required_tools={"ansible-playbook": "2.21.0"},
    examples=[
        ("Approve one proposal", "cloudfall operator approve nginx-down-20260101"),
    ],
)
def operator_approve(
    args: OperatorApproveArgs, ctx: Ctx, fleet: Fleet
) -> Approved:
    """Run the proposal's operation, then wait for its trigger to resolve."""
    store = args.store(fleet)
    context = args.context(fleet, ctx)
    try:
        pending = store.load(args.proposal)
        if pending.trigger_kind is TriggerKind.ALERT:
            verifier = alert_resolution_verifier(args.feed(fleet))
        else:
            verifier = drift_resolution_verifier(
                engine_auditor(context, fleet.inventory, fleet.path(args.observed))
            )
        proposal = approve_proposal(
            store,
            args.proposal,
            engine_executor(context),
            verifier,
            ApproveOptions(verify_timeout_seconds=args.verify_timeout),
        )
    except OperatorError as error:
        raise _proposal_failed(error) from error
    except LifecycleError as error:
        raise _lifecycle_failed(error) from error
    verified = proposal.status is ProposalStatus.VERIFIED
    approved = Approved(
        effect="updated",
        status="ok" if verified else "failed",
        proposal=proposal.as_document(),
    )
    if not verified:
        message = "the run finished but its trigger did not resolve"
        raise Exit.NOT_VERIFIED(message, data=approved)
    return approved


# migrate


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrateArgs(EngineArgs):
    """Arguments of ``migrate``."""

    observed: Path = Flag(
        default=Path("tmp/observed"),
        description="Server observation directory (default: tmp/observed)",
    )
    service_observed: Path = Flag(
        default=Path("tmp/observed-services"),
        description="Domain observation directory (default: tmp/observed-services)",
    )
    deployments: Path = Flag(
        default=Path("tmp/deployments"),
        description="Deployment receipt directory (default: tmp/deployments)",
    )
    receipts: Path = Flag(
        default=Path("tmp/releases"),
        description="Release receipt directory (default: tmp/releases)",
    )
    artifacts: Path = Flag(
        default=Path("tmp/artifacts"),
        description="Artifact directory (default: tmp/artifacts)",
    )
    plan_file: Path = Flag(
        default=Path("tmp/migrate/plan.json"),
        description="Persisted migration plan (default: tmp/migrate/plan.json)",
    )
    build: tuple[str, ...] = Flag(
        default=(),
        description="Build a component from a git ref, as COMPONENT=REF (repeatable)",
    )
    release: tuple[str, ...] = Flag(
        default=(),
        description="Deploy an existing release, as COMPONENT=RELEASE (repeatable)",
    )
    env_file: tuple[str, ...] = Flag(
        default=(),
        description="Environment file for a component, as COMPONENT=PATH (repeatable)",
    )
    data: tuple[str, ...] = Flag(
        default=(),
        description=(
            "Source URL file for a database, as DATABASE=PATH (repeatable)"
        ),
    )
    yes: bool = Flag(
        default=False,
        confirm=True,
        description=(
            "Change the servers; without it the command prints the plan and "
            "runs nothing"
        ),
    )
    restart: bool = Flag(
        default=False,
        description="Discard the persisted plan and start again",
    )

    def __post_init__(self) -> None:
        """Refuse a malformed pair, and keep relative paths in the project."""
        EngineArgs.__post_init__(self)
        for flag in (
            "observed",
            "service_observed",
            "deployments",
            "receipts",
            "artifacts",
            "plan_file",
        ):
            _inside_project(getattr(self, flag), flag.replace("_", "-"))
        for option, entries in (
            ("build", self.build),
            ("release", self.release),
            ("env-file", self.env_file),
            ("data", self.data),
        ):
            _pairs(entries, option)

    def options(self, fleet: Fleet) -> MigrateOptions:
        """Return what this run builds, deploys and migrates."""
        return MigrateOptions(
            plan_file=fleet.path(self.plan_file),
            builds=_pairs(self.build, "build"),
            releases=_pairs(self.release, "release"),
            # Relative to the project, as every other path here is.
            environment_files={
                component: fleet.path(Path(value))
                for component, value in _pairs(self.env_file, "env-file").items()
            },
            data_migrations={
                database: fleet.path(Path(value))
                for database, value in _pairs(self.data, "data").items()
            },
            execute=self.yes,
            restart=self.restart,
        )

    def config(self, fleet: Fleet, ctx: Ctx) -> AgentConfig:
        """Return the filesystem contract the migration runs under."""
        return AgentConfig(
            project_directory=fleet.directory,
            schema_directory=self.schemas,
            engine_directory=self.engine,
            inventory_file=fleet.path(self.inventory_file),
            observed_directory=fleet.path(self.observed),
            service_observed_directory=fleet.path(self.service_observed),
            deployments_directory=fleet.path(self.deployments),
            releases_directory=fleet.path(self.receipts),
            artifacts_directory=fleet.path(self.artifacts),
            run=_step_runner(ctx, fleet.directory),
        )


def _pairs(entries: tuple[str, ...], option: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for entry in entries:
        key, separator, value = entry.partition("=")
        if not separator or not key or not value:
            message = f"--{option} expects NAME=VALUE, got {entry!r}"
            raise ParseError(message, context={"flag": option})
        pairs[key] = value
    return pairs


@dataclass(frozen=True, slots=True)
class Migration:
    """The migration plan's steps and how far the run got."""

    effect: str
    status: str
    steps: list[dict[str, object]] = Out(ordered=True)
    completed: int = 0
    next: str | None = None
    step: str | None = None


@app.command(
    "migrate",
    description=(
        "Run the resumable end-to-end migration; without --yes it prints the plan "
        "and runs nothing"
    ),
    danger_level="mutating",
    exit_codes=[
        "PROJECT_INVALID",
        "CONFIG_INVALID",
        "PRECONDITION",
        "PARTIAL_FAILURE",
        "ENGINE_STEP_FAILED",
    ],
    timeout=None,
    supports_raw_payload=True,
    subprocess=Subprocess("ansible-playbook"),
    required_tools={"ansible-playbook": "2.21.0", "git": "2.24.0"},
    examples=[
        ("Print the plan", "cloudfall migrate --build crm-backend=main"),
        ("Run it", "cloudfall migrate --build crm-backend=main --yes"),
    ],
)
def migrate(args: MigrateArgs, ctx: Ctx, fleet: Fleet) -> Migration:
    """Run each pending step, saving progress after every one."""
    try:
        result = execute_migration(args.config(fleet, ctx), args.options(fleet))
    except MigrateError as error:
        raise Exit.PRECONDITION(
            error.detail, context={"code": error.code}
        ) from error
    status = str(result["status"])
    migration = Migration(
        # A plan is the dry run --yes confirms.
        effect="would_update" if status == "plan" else "updated",
        status=status,
        steps=list(cast("list[dict[str, object]]", result["steps"])),
        completed=int(cast("int", result["completed"])),
        next=cast("str | None", result.get("next")),
        step=cast("str | None", result.get("step")),
    )
    # The step's own error goes in error.context, beside the plan in data.
    cause = {
        "step": migration.step,
        **cast("Mapping[str, object]", result.get("error", {})),
    }
    if status == "paused":
        message = f"the migration paused at {migration.step}; resume it with --yes"
        raise Exit.PARTIAL_FAILURE(message, context=cause, data=migration)
    if status == "error":
        message = f"the migration failed at {migration.step}"
        raise Exit.ENGINE_STEP_FAILED(message, context=cause, data=migration)
    return migration


# why


@dataclass(frozen=True, slots=True, kw_only=True)
class WhyArgs(DecisionsArgs):
    """Arguments of ``why``."""

    host: str | None = Flag(
        default=None, description="Only decisions whose record names this host"
    )
    operation: ResourceId | None = Flag(
        default=None, description="Only decisions of this declared operation"
    )
    since: str | None = Flag(
        default=None,
        description=(
            "Only decisions with a moment at or after this ISO 8601 time or date"
        ),
    )
    until: str | None = Flag(
        default=None,
        description=(
            "Only decisions with a moment at or before this ISO 8601 time or date"
        ),
    )

    def __post_init__(self) -> None:
        """Refuse a time the question cannot be asked with."""
        DecisionsArgs.__post_init__(self)
        try:
            self.query()
        except WhyError as error:
            raise ParseError(error.detail, context={"code": error.code}) from error

    def query(self) -> WhyQuery:
        """Return the question, as the record is filtered by it."""
        return WhyQuery.from_boundary(
            host=self.host,
            operation=self.operation.value if self.operation is not None else None,
            since=self.since,
            until=self.until,
        )


class WhyPayload(Payload):
    """``why``: each decision the question is about, told from its record."""

    command = "why"


@app.command(
    "why",
    description=(
        "Answer why the agent did that, from the record, for a host, an operation "
        "or a time window; --format html renders one page"
    ),
    danger_level="safe",
    exit_codes=["RECORD_INVALID"],
    renderers={"html": FormatRenderer(render_why_document, media_type="text/html")},
    examples=[
        ("Ask about one host", "cloudfall why --host h1"),
        ("One page for a person", "cloudfall why --since 2026-10-01 --format html"),
    ],
)
def why(args: WhyArgs, _ctx: Ctx) -> WhyPayload:
    """Answer from the record alone: no catalog, no fleet."""
    store = DecisionStore(
        directory=args.root / args.decisions, catalog=SchemaCatalog(args.schemas)
    )
    try:
        result = answer(store, args.query())
    except (DecisionError, WhyError) as error:
        raise Exit.RECORD_INVALID(error.detail, context={"code": error.code}) from error
    return WhyPayload(result.as_dict())


# observe, secrets render, import render, import render-api: payloads with
# camelCase keys, written through the adapter now that it may carry effect.


app.exit_code(
    "INCOMPLETE",
    91,
    description=(
        "Some servers produced no snapshot or Ansible failed; data says which"
    ),
    retryable=True,
    # observe only reads the hosts; a retry rewrites the snapshots it wrote.
    side_effects="none",
    suggestion="read data.missing and data.detail, fix the hosts, then observe again",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ObserveArgs(FleetArgs):
    """Arguments of ``observe``."""

    output_dir: Path = Flag(
        default=Path("tmp/observed"),
        description="Snapshot directory to write (default: tmp/observed)",
    )
    limit: str | None = Flag(
        default=None, description="Ansible host pattern to inspect a subset"
    )
    engine: Path = Flag(
        default=default_engine_directory(),
        description="Engine directory holding the playbooks (default: bundled)",
    )

    def __post_init__(self) -> None:
        """Keep a relative output path inside the project."""
        _inside_project(self.output_dir, "output-dir")


class ObservedPayload(Payload):
    """``observe``: which servers produced a snapshot."""

    command = "observe"


def _playbook_runner(
    ctx: Ctx, directory: Path
) -> Callable[[Sequence[str], Mapping[str, str]], PlaybookRun]:
    """Run the inspection through ``ctx.run``, keeping its output for a failure."""

    def run(argv: Sequence[str], environment: Mapping[str, str]) -> PlaybookRun:
        done = ctx.run(list(argv), env=environment, cwd=directory, check=False)
        return PlaybookRun(exit_code=done.returncode, output=done.stdout + done.stderr)

    return run


@app.command(
    "observe",
    description=(
        "Collect read-only server snapshots from the fleet; servers without one "
        "exit non-zero with the run in data"
    ),
    # It only reads the hosts. The snapshots are its product, which audit
    # reads, so cleanup keeps them; the inventory overlay is regenerated.
    danger_level="safe",
    project_root=PROJECT_MARKERS,
    filesystem_side_effects=[
        SideEffect("{project_root}/tmp/observed/", "output"),
        SideEffect("{project_root}/tmp/cloudfall/", "cache"),
    ],
    exit_codes=[
        "PROJECT_INVALID",
        "CONFIG_INVALID",
        "INVENTORY_UNREADABLE",
        "PRECONDITION",
        "INCOMPLETE",
    ],
    requires=[ONE_FLEET],
    timeout=None,
    subprocess=Subprocess("ansible-playbook"),
    required_tools={"ansible-playbook": "2.21.0"},
    supports_raw_payload=True,
    examples=[("Snapshot every server", "cloudfall observe")],
)
def observe(args: ObserveArgs, ctx: Ctx, fleet: InventoryFleet) -> ObservedPayload:
    """Inspect every declared server and write one snapshot each."""
    sources = (
        (fleet.source.value,)
        if fleet.source is not None
        else (fleet.directory / "tmp/ansible-inventory.json",)
    )
    output = fleet.path(args.output_dir)
    try:
        request = ObservationRequest(
            inventory_sources=sources,
            output_directory=output.resolve(),
            engine_directory=args.engine,
            limit=args.limit,
            configuration=(
                team_configuration(Path.cwd()) if fleet.source is not None else None
            ),
        )
        result = collect_observations(
            fleet.inventory,
            request,
            fleet.path(Path("tmp/cloudfall")),
            _playbook_runner(ctx, fleet.directory),
        )
    except ObserveError as error:
        raise Exit.PRECONDITION(error.detail, context={"code": error.code}) from error
    payload = ObservedPayload(result.as_dict())
    if not result.complete:
        message = f"{len(result.missing)} server(s) produced no snapshot"
        raise Exit.INCOMPLETE(message, data=payload)
    return payload


# secrets render


secrets = app.group("secrets", description="Resolve declared secret references")


@dataclass(frozen=True, slots=True, kw_only=True)
class SecretsRenderArgs(EngineArgs):
    """Arguments of ``secrets render``."""

    component: ResourceId = Arg(description="Component whose references to render")
    secrets_dir: Path = Flag(
        default=Path("secrets"),
        description="sops-encrypted secrets directory (default: secrets)",
        secret=False,
    )
    output_file: Path | None = Flag(
        default=None,
        description="Environment file to write (default: tmp/env/<component>.env)",
    )
    receipts: Path = Flag(
        default=Path("tmp/env-receipts"),
        description="Environment receipt directory (default: tmp/env-receipts)",
    )

    def __post_init__(self) -> None:
        """Keep relative paths inside the project."""
        EngineArgs.__post_init__(self)
        _inside_project(self.secrets_dir, "secrets-dir")
        _inside_project(self.receipts, "receipts")
        if self.output_file is not None:
            _inside_project(self.output_file, "output-file")


class RenderedSecretsPayload(Payload):
    """``secrets render``: names and hashes of what was written, never values."""

    command = "secrets render"
    writes = True


def _sops_decrypt(ctx: Ctx) -> Callable[[Path], str]:
    """Decrypt one sops file through ``ctx.run``."""

    def decrypt(source: Path) -> str:
        binary = shutil.which("sops")
        if binary is None:
            message = (
                "the sops binary is not installed; install sops and age, and "
                "set SOPS_AGE_KEY_FILE to your age key"
            )
            raise SecretsError(ERROR_SOPS_MISSING, message)
        done = ctx.run([binary, "--decrypt", str(source)], check=False)
        if done.returncode != 0:
            message = f"sops could not decrypt {source}: {done.stderr.strip()[:300]}"
            raise SecretsError(ERROR_DECRYPT_FAILED, message)
        return done.stdout

    return decrypt


@secrets.command(
    "render",
    description=(
        "Render one component's secret references into its 0600 environment "
        "file; the answer names keys and hashes, never values"
    ),
    danger_level="mutating",
    exit_codes=["PROJECT_INVALID", "CONFIG_INVALID", "PRECONDITION"],
    timeout=120,
    subprocess=Subprocess("sops"),
    required_tools={"sops": "3.8.0"},
    supports_raw_payload=True,
    examples=[("Render one component", "cloudfall secrets render crm-backend")],
)
def secrets_render(
    args: SecretsRenderArgs, ctx: Ctx, fleet: Fleet
) -> RenderedSecretsPayload:
    """Decrypt the component's fragments and write its environment file."""
    output = fleet.path(
        args.output_file
        if args.output_file is not None
        else Path("tmp/env") / f"{args.component}.env"
    )
    effect = "updated" if output.exists() else "created"
    provider = SopsSecretProvider(
        secrets_directory=fleet.path(args.secrets_dir), decrypt=_sops_decrypt(ctx)
    )
    try:
        body = render_environment(
            args.context(fleet, ctx),
            args.component,
            provider,
            output,
            receipt_directory=fleet.path(args.receipts),
        )
    except SecretsError as error:
        raise Exit.PRECONDITION(error.message, context={"code": error.code}) from error
    return RenderedSecretsPayload({**body, "effect": effect})


# import render, render-api


imports = app.group("import", description="Import external platform definitions")


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportArgs(ProjectArgs):
    """Options both importers share."""

    application: ResourceId = Flag(
        description="Cloudfall application id (also the application's Linux user)"
    )
    server: ResourceId = Flag(
        description="Declared server id that receives every imported resource"
    )
    output_dir: Path = Flag(
        default=Path("tmp/import/config"),
        description="Config fragment output directory (default: tmp/import/config)",
    )
    env_dir: Path = Flag(
        default=Path("tmp/import/env"),
        description="Environment file output directory (default: tmp/import/env)",
    )

    def __post_init__(self) -> None:
        """Keep relative output paths inside the project."""
        _inside_project(self.output_dir, "output-dir")
        _inside_project(self.env_dir, "env-dir")

    def targets(self, project: ProjectDirectory) -> ImportTargets:
        """Return where the imported resources land."""
        return ImportTargets(
            application_id=self.application,
            server_id=self.server,
            project_directory=project.path / self.output_dir,
            environment_directory=project.path / self.env_dir,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportBlueprintArgs(ImportArgs):
    """Arguments of ``import render``."""

    blueprint: Path = Arg(description="render.yaml blueprint, relative to the project")


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportApiArgs(ImportArgs):
    """Arguments of ``import render-api``."""

    api_key_file: Path = Flag(
        description="File containing only the Render API key",
        # The flag names a file; the key itself never reaches argv.
        secret=False,
    )
    api_url: str = Flag(
        default="https://api.render.com/v1",
        description="Render API base URL (default: https://api.render.com/v1)",
    )


class ImportedPayload(Payload):
    """``import render``: the resources written and what could not be mapped."""

    command = "import render"
    writes = True


class ImportedApiPayload(Payload):
    """``import render-api``: the resources written from the live workspace."""

    command = "import render-api"
    writes = True


_IMPORT_EXIT_CODES = [
    "PROJECT_INVALID",
    "CONFIG_INVALID",
    "PRECONDITION",
    "PERMISSION_DENIED",
]


@imports.command(
    "render",
    description="Map a render.yaml blueprint onto Cloudfall config fragments",
    danger_level="mutating",
    exit_codes=_IMPORT_EXIT_CODES,
    timeout=60,
    supports_raw_payload=True,
    examples=[
        (
            "Import a blueprint for one server",
            "cloudfall import render render.yaml --application crm --server h1",
        ),
    ],
)
def import_render(
    args: ImportBlueprintArgs, _ctx: Ctx, project: ProjectDirectory
) -> ImportedPayload:
    """Write config fragments and environment files from the blueprint."""
    try:
        result = import_render_blueprint(
            project.path / args.blueprint, args.targets(project), args.schemas
        )
    except RenderImportError as error:
        raise Exit.PRECONDITION(error.detail, context={"code": error.code}) from error
    except ConfigValidationError as error:
        raise _config_invalid(error) from error
    except OSError as error:
        if error.errno not in _NOT_WRITABLE:
            raise
        raise _not_writable(error) from error
    return ImportedPayload({**result.as_dict(), "effect": "created"})


def _render_transport(ctx: Ctx) -> Callable[[str, str], object]:
    """Fetch one Render API page through ``ctx.http``."""

    def get(url: str, api_key: str) -> object:
        response = ctx.http.get(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        if response.status != HTTPStatus.OK:
            message = f"Render API answered {response.status} for {url}"
            raise RenderImportError(ERROR_API_UNREACHABLE, message)
        return response.json()

    return get


@imports.command(
    "render-api",
    description="Map a live Render workspace onto Cloudfall config via the API",
    danger_level="mutating",
    exit_codes=_IMPORT_EXIT_CODES,
    timeout=300,
    has_network_io=True,
    # Service names, URLs and settings in the answer come from the workspace.
    external=True,
    supports_raw_payload=True,
    examples=[
        (
            "Import a workspace",
            "cloudfall import render-api --api-key-file render.key "
            "--application crm --server h1",
        ),
    ],
)
def import_render_api_command(
    args: ImportApiArgs, ctx: Ctx, project: ProjectDirectory
) -> ImportedApiPayload:
    """Read the workspace page by page and write config fragments from it."""
    try:
        client = HttpRenderApiClient(
            api_key=read_api_key(project.path / args.api_key_file),
            base_url=args.api_url,
            transport=_render_transport(ctx),
        )
        result = import_render_api(client, args.targets(project), args.schemas)
    except RenderImportError as error:
        raise Exit.PRECONDITION(error.detail, context={"code": error.code}) from error
    except ConfigValidationError as error:
        raise _config_invalid(error) from error
    except OSError as error:
        if error.errno not in _NOT_WRITABLE:
            raise
        raise _not_writable(error) from error
    return ImportedApiPayload({**result.as_dict(), "effect": "created"})


# deploy, rollback, restart, data migrate: --yes runs them; without it they
# validate the request and answer the plan.


app.scalar(
    ReleaseId,
    parse=ReleaseId.from_boundary,
    pattern=r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{7,40}",
)

_PLAN_INSTRUCTION = "review the plan and re-run with --yes to execute it"


@dataclass(frozen=True, slots=True, kw_only=True)
class ComponentArgs(EngineArgs):
    """Options of a command that acts on one component's servers."""

    component: ResourceId = Arg(description="Component to act on")
    yes: bool = Flag(
        default=False,
        confirm=True,
        description=(
            "Change the servers; without it the command validates the request "
            "and only shows what it would do"
        ),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class DeployArgs(ComponentArgs):
    """Arguments of ``deploy``."""

    release: ReleaseId = Flag(
        description="Release id produced by cloudfall-engine artifact build"
    )
    artifacts: Path = Flag(
        default=Path("tmp/artifacts"),
        description="Artifact directory (default: tmp/artifacts)",
    )
    env_file: Path | None = Flag(
        default=None,
        description="Optional controller-side environment file for the component",
    )
    receipts: Path = Flag(
        default=Path("tmp/releases"),
        description="Release receipt directory (default: tmp/releases)",
    )

    def __post_init__(self) -> None:
        """Keep relative paths inside the project."""
        ComponentArgs.__post_init__(self)
        _inside_project(self.artifacts, "artifacts")
        _inside_project(self.receipts, "receipts")


@dataclass(frozen=True, slots=True, kw_only=True)
class RollbackArgs(ComponentArgs):
    """Arguments of ``rollback``."""

    release: ReleaseId = Flag(description="Existing release id to activate")


@dataclass(frozen=True, slots=True, kw_only=True)
class DataMigrateArgs(EngineArgs):
    """Arguments of ``data migrate``."""

    service: ResourceId = Arg(description="Declared service to restore into")
    database: str = Flag(description="Declared database name inside the service")
    source_url_file: Path = Flag(
        description=(
            "Controller-side file whose only content is the source database "
            "connection URL"
        ),
        # The flag names a file; the URL with its password never reaches argv.
        secret=False,
    )
    receipts: Path = Flag(
        default=Path("tmp/data-migrations"),
        description="Migration receipt directory (default: tmp/data-migrations)",
    )
    yes: bool = Flag(
        default=False,
        confirm=True,
        description=(
            "Change the servers; without it the command validates the request "
            "and only shows what it would do"
        ),
    )

    def __post_init__(self) -> None:
        """Keep relative paths inside the project."""
        EngineArgs.__post_init__(self)
        _inside_project(self.receipts, "receipts")


class DeployPayload(Payload):
    """``deploy``: the plan without --yes, the deployed release with it."""

    command = "deploy"
    writes = True


class RollbackPayload(Payload):
    """``rollback``: the plan without --yes, the release switched to with it."""

    command = "rollback"
    writes = True


class RestartPayload(Payload):
    """``restart``: the plan without --yes, the health-gated restart with it."""

    command = "restart"
    writes = True


class DataMigratePayload(Payload):
    """``data migrate``: the plan without --yes, the verified restore with it."""

    command = "data migrate"
    writes = True


def _plan(preview: LifecyclePreview) -> dict[str, object]:
    # Without --yes the run is treaty's dry run (Flag(confirm=True)).
    return {
        **preview.as_dict(),
        "instruction": _PLAN_INSTRUCTION,
        "effect": "would_update",
    }


_LIFECYCLE_EXIT_CODES = [
    "PROJECT_INVALID",
    "CONFIG_INVALID",
    "PRECONDITION",
    "ENGINE_STEP_FAILED",
]


@app.command(
    "deploy",
    description=(
        "Deploy one built component release behind its health gate; without "
        "--yes it shows the plan"
    ),
    danger_level="mutating",
    exit_codes=_LIFECYCLE_EXIT_CODES,
    timeout=None,
    subprocess=Subprocess("ansible-playbook"),
    required_tools={"ansible-playbook": "2.21.0"},
    supports_raw_payload=True,
    examples=[
        (
            "Show the plan",
            "cloudfall deploy crm-backend --release 20260101T000000Z-abcdef0",
        ),
        (
            "Deploy it",
            "cloudfall deploy crm-backend --release 20260101T000000Z-abcdef0 --yes",
        ),
    ],
)
def deploy_command(args: DeployArgs, ctx: Ctx, fleet: Fleet) -> DeployPayload:
    """Activate the release on the component's servers, or show the plan."""
    context = args.context(fleet, ctx)
    artifacts = fleet.path(args.artifacts)
    try:
        if not args.yes:
            return DeployPayload(
                _plan(preview_deploy(context, args.component, args.release, artifacts))
            )
        result = deploy(
            context,
            args.component,
            args.release,
            artifacts,
            DeployOptions(
                environment_file=(
                    fleet.path(args.env_file) if args.env_file is not None else None
                ),
                receipt_directory=fleet.path(args.receipts),
            ),
        )
    except LifecycleError as error:
        raise _lifecycle_failed(error) from error
    return DeployPayload({**result.as_dict(), "effect": "updated"})


@app.command(
    "rollback",
    description=(
        "Switch one component back to an existing release; without --yes it "
        "shows the plan"
    ),
    danger_level="mutating",
    exit_codes=_LIFECYCLE_EXIT_CODES,
    timeout=None,
    subprocess=Subprocess("ansible-playbook"),
    required_tools={"ansible-playbook": "2.21.0"},
    supports_raw_payload=True,
    examples=[
        (
            "Show the plan",
            "cloudfall rollback crm-backend --release 20260101T000000Z-abcdef0",
        ),
    ],
)
def rollback_command(args: RollbackArgs, ctx: Ctx, fleet: Fleet) -> RollbackPayload:
    """Switch the component back to the release, or show the plan."""
    context = args.context(fleet, ctx)
    try:
        if not args.yes:
            return RollbackPayload(
                _plan(preview_rollback(context, args.component, args.release))
            )
        result = rollback(context, args.component, args.release)
    except LifecycleError as error:
        raise _lifecycle_failed(error) from error
    return RollbackPayload({**result.as_dict(), "effect": "updated"})


@app.command(
    "restart",
    description=(
        "Restart one component behind its health check; without --yes it shows "
        "the plan"
    ),
    danger_level="mutating",
    exit_codes=_LIFECYCLE_EXIT_CODES,
    timeout=None,
    supports_raw_payload=True,
    subprocess=Subprocess("ansible-playbook"),
    required_tools={"ansible-playbook": "2.21.0"},
    examples=[
        ("Show the plan", "cloudfall restart crm-backend"),
        ("Restart it", "cloudfall restart crm-backend --yes"),
    ],
)
def restart_command(args: ComponentArgs, ctx: Ctx, fleet: Fleet) -> RestartPayload:
    """Restart the component, or show the plan."""
    context = args.context(fleet, ctx)
    try:
        if not args.yes:
            return RestartPayload(_plan(preview_restart(context, args.component)))
        result = restart(context, args.component)
    except LifecycleError as error:
        raise _lifecycle_failed(error) from error
    return RestartPayload({**result.as_dict(), "effect": "updated"})


data = app.group("data", description="Migrate data into declared services")


@data.command(
    "migrate",
    description=(
        "Dump an external PostgreSQL database and restore it into a declared "
        "service with row-count verification; without --yes it shows the plan"
    ),
    danger_level="mutating",
    exit_codes=_LIFECYCLE_EXIT_CODES,
    timeout=None,
    subprocess=Subprocess("ansible-playbook"),
    required_tools={"ansible-playbook": "2.21.0"},
    supports_raw_payload=True,
    examples=[
        (
            "Show the plan",
            "cloudfall data migrate postgresql-main --database crm "
            "--source-url-file source.url",
        ),
    ],
)
def data_migrate(
    args: DataMigrateArgs, ctx: Ctx, fleet: Fleet
) -> DataMigratePayload:
    """Restore the source database into the service, or show the plan."""
    context = args.context(fleet, ctx)
    source = fleet.path(args.source_url_file)
    try:
        if not args.yes:
            return DataMigratePayload(
                _plan(
                    preview_data_migration(context, args.service, args.database, source)
                )
            )
        body = migrate_data(
            context, args.service, args.database, source, fleet.path(args.receipts)
        )
    except LifecycleError as error:
        raise _lifecycle_failed(error) from error
    return DataMigratePayload({**body, "effect": "updated"})
