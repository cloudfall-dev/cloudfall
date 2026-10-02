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
from dataclasses import dataclass
from functools import partial
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
    Out,
    ParseError,
    Subprocess,
    Timeout,
)

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
from cloudfall.decision import DECISION_DIRECTORY, DecisionError, DecisionStore
from cloudfall.domain import (
    ConnectionAddress,
    Hostname,
    LinuxUser,
    ResourceId,
    TcpPort,
)
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.operations import FleetOperations, UtcTimestamp, build_operations_view
from cloudfall.operator import ERROR_PROPOSAL_MISSING, OperatorError, ProposalStore
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
from cloudfall.resources import default_schema_directory
from cloudfall.secrets import load_environment_receipts
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
]


@add.command(
    "ssh-key",
    description="Declare an SSH public key read from a file",
    danger_level="mutating",
    timeout=30,
    supports_raw_payload=True,
    exit_codes=[*_ADD_EXIT_CODES, "NOT_FOUND"],
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


@dataclass(frozen=True, slots=True)
class DashboardBuilt:
    """The static dashboard written, and the fleet health it shows."""

    effect: str
    status: str
    health: str
    tasks: int
    dashboard: dict[str, str]


@dashboard.command(
    "build",
    description="Build a static read-only operations dashboard",
    danger_level="mutating",
    timeout=120,
    supports_raw_payload=True,
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
) -> DashboardBuilt:
    """Write the dashboard pages for the fleet's current evidence."""
    view = args.operations_view(fleet)
    output = fleet.path(args.output_dir)
    effect = "updated" if output.exists() else "created"
    try:
        artifacts = build_dashboard(view, output)
    except OSError as error:
        if error.errno not in _NOT_WRITABLE:
            raise
        raise _not_writable(error) from error
    return DashboardBuilt(
        effect=effect,
        status="ok",
        health=view.health.value,
        tasks=len(view.tasks),
        dashboard=artifacts.as_dict(),
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


@dataclass(frozen=True, slots=True)
class ServicesInspected:
    """The domain observation files written."""

    effect: str
    status: str
    observations: list[str] = Out(ordered=True)


@services.command(
    "inspect",
    description="Collect DNS, TLS, origin, and public route evidence",
    danger_level="mutating",
    # DNS, TLS, and HTTP probes of every declared domain, one after another.
    timeout=600,
    exit_codes=["PROJECT_INVALID", "CONFIG_INVALID", "PERMISSION_DENIED"],
    examples=[("Probe every declared domain", "cloudfall services inspect")],
)
def services_inspect(
    args: ServicesInspectArgs, _ctx: Ctx, fleet: Fleet
) -> ServicesInspected:
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
    return ServicesInspected(
        effect="created",
        status="ok",
        observations=[str(path) for path in paths],
    )
