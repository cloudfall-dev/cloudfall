"""Command-line boundary for Cloudfall operations."""

from __future__ import annotations

import errno
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    from argparse import Namespace
    from collections.abc import Callable, Mapping, Sequence

    from cloudfall.operations import FleetOperations
    from cloudfall.operator import AlertFeed, OperatorProposal

from cloudfall.ansible_api import (
    ANSIBLE_CONFIG_FILE,
    AnsibleReadError,
    InventorySource,
    inventory_from_config,
    read_inventory,
)
from cloudfall.ansible_reader import read_fleet
from cloudfall.app import app as treaty_app
from cloudfall.arguments import (
    StrictArgumentParser,
    add_output_options,
    parse_arguments,
    project_path_argument,
    release_id_argument,
    resource_id_argument,
    root_parser,
    schema_version_argument,
)
from cloudfall.catalog import CATALOG_DIRECTORY
from cloudfall.dashboard import RefreshInterval
from cloudfall.dashboard_server import (
    EvidenceSources,
    ListenEndpoint,
    create_dashboard_server,
)
from cloudfall.decision import (
    DECISION_DIRECTORY,
    DecisionError,
    DecisionStore,
)
from cloudfall.importer import (
    ImportTargets,
    RenderImportError,
    import_render_blueprint,
)
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import (
    DeployOptions,
    EngineContext,
    LifecycleError,
    LifecyclePreview,
    deploy,
    migrate_data,
    preview_data_migration,
    preview_deploy,
    preview_restart,
    preview_rollback,
    restart,
    rollback,
)
from cloudfall.observation import load_observations
from cloudfall.observe import (
    ObservationRequest,
    ObserveError,
    collect_observations,
    team_configuration,
)
from cloudfall.operations import UtcTimestamp, build_operations_view
from cloudfall.operator import (
    OperatorError,
    ProposalStore,
    TriggerKind,
    alert_resolution_verifier,
    autonomous_pass,
    drift_pass,
    drift_resolution_verifier,
    engine_auditor,
    engine_executor,
    gateway_feed,
)
from cloudfall.operator import (
    run_once as operator_run_once,
)
from cloudfall.output import (
    begin_invocation,
    current_invocation,
    exit_code,
    schema_changes_since,
    schema_versions,
    write_error,
    write_result,
)
from cloudfall.project import (
    PROJECT_DIRECTORY_VARIABLE,
    ProjectError,
    is_project,
    project_context,
)
from cloudfall.render_api import (
    HttpRenderApiClient,
    import_render_api,
    read_api_key,
)
from cloudfall.resources import default_engine_directory, default_schema_directory
from cloudfall.secrets import (
    SecretsError,
    SopsSecretProvider,
    render_environment,
)
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
from cloudfall.why import WhyError, WhyQuery, answer, render_why_html


def _add_config_parsers(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    config_parser = commands.add_parser("config", help="operate on the config")
    config_commands = config_parser.add_subparsers(dest="config_command", required=True)
    validate_parser = config_commands.add_parser(
        "validate", help="validate the YAML config and resource references"
    )
    _add_project_directory_argument(validate_parser)
    validate_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )

    inventory_parser = commands.add_parser(
        "inventory", help="query validated platform inventory"
    )
    inventory_commands = inventory_parser.add_subparsers(
        dest="inventory_command", required=True
    )
    show_parser = inventory_commands.add_parser(
        "show", help="show non-secret platform inventory"
    )
    _add_project_directory_argument(show_parser)
    _add_inventory_argument(show_parser)
    show_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )


def _add_project_directory_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help=(
            f"project directory to run in (default: ${PROJECT_DIRECTORY_VARIABLE}, "
            "else the current directory when it is a project)"
        ),
    )


def _add_projectless_parsers(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    """Add the commands that run before, or without, a project."""
    _add_init_parser(commands)
    _add_changelog_parser(commands)


def _add_changelog_parser(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    changelog_parser = commands.add_parser(
        "changelog",
        help="list changes to the JSON output contract, newest first",
    )
    changelog_parser.add_argument(
        "--since",
        type=schema_version_argument,
        metavar="MAJOR.MINOR",
        help="only changes after this output schema version",
    )


def _add_init_parser(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    init_parser = commands.add_parser(
        "init", help="create a new project: fleet, applications, and operations"
    )
    init_parser.add_argument(
        "directory",
        type=Path,
        nargs="?",
        default=Path(),
        help="project directory to create; must be empty or absent (default: .)",
    )
    init_parser.add_argument(
        "--name",
        help="package name written to pyproject.toml (default: the directory name)",
    )
    init_parser.add_argument(
        "--description",
        help=(
            "one line saying what the project manages, written to the README "
            "and pyproject.toml"
        ),
    )


def _add_operations_parsers(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    """Add the commands that read the declared operations catalog."""
    operations_parser = commands.add_parser(
        "operations", help="read the catalog of operations an agent may run"
    )
    operations_commands = operations_parser.add_subparsers(
        dest="operations_command", required=True
    )
    for name, help_text in (
        ("list", "list every declared operation with its risk level"),
        ("show", "show one declared operation in full"),
        ("propose", "run one operation in check mode and record what it would do"),
        ("approve", "approve one recorded proposal, run it, and verify it"),
        ("decisions", "list the decision records this repository holds"),
    ):
        subparser = operations_commands.add_parser(name, help=help_text)
        subparser.add_argument(
            "--repository",
            type=Path,
            help=(
                "repository holding the operations directory and the "
                "playbooks it declares (default: the current directory)"
            ),
        )
        if name in {"show", "propose"}:
            subparser.add_argument(
                "operation", type=resource_id_argument, help="operation id"
            )
        if name == "approve":
            subparser.add_argument(
                "decision",
                type=resource_id_argument,
                help="decision id from `operations propose`",
            )
            subparser.add_argument(
                "--approver",
                help="who is approving (default: the USER environment variable)",
            )
            subparser.add_argument(
                "--yes",
                action="store_true",
                help=(
                    "change the servers; without this flag the command shows "
                    "the recorded proposal and runs nothing"
                ),
            )
        if name == "propose":
            subparser.add_argument(
                "--target",
                help=(
                    "host or group the operation runs against, as the "
                    "operation's target scope requires"
                ),
            )
            subparser.add_argument(
                "--input",
                action="append",
                default=[],
                metavar="NAME=VALUE",
                help="value for one declared input (repeatable)",
            )
            subparser.add_argument(
                "--observed",
                type=project_path_argument,
                default=Path("tmp/observed"),
                help=(
                    "snapshot directory the proposal cites as its basis "
                    "(default: tmp/observed)"
                ),
            )
            subparser.add_argument(
                "--decisions",
                type=project_path_argument,
                default=Path(DECISION_DIRECTORY),
                help=(
                    "directory holding the decision records "
                    f"(default: {DECISION_DIRECTORY})"
                ),
            )
        if name in {"approve", "decisions"}:
            subparser.add_argument(
                "--decisions",
                type=project_path_argument,
                default=Path(DECISION_DIRECTORY),
                help=(
                    "directory holding the decision records "
                    f"(default: {DECISION_DIRECTORY})"
                ),
            )
        subparser.add_argument(
            "--operations",
            type=project_path_argument,
            default=Path(CATALOG_DIRECTORY),
            help=(
                "directory holding the operation documents "
                f"(default: {CATALOG_DIRECTORY})"
            ),
        )
        subparser.add_argument(
            "--schemas",
            type=Path,
            default=default_schema_directory(),
            help="versioned schema directory (default: bundled schemas)",
        )


def _add_observe_parser(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    """Add the command that collects snapshots from the fleet."""
    observe_parser = commands.add_parser(
        "observe", help="collect read-only server snapshots from the fleet"
    )
    _add_project_directory_argument(observe_parser)
    _add_inventory_argument(observe_parser)
    observe_parser.add_argument(
        "--output-dir",
        type=project_path_argument,
        default=Path("tmp/observed"),
        help="snapshot directory to write (default: tmp/observed)",
    )
    observe_parser.add_argument(
        "--limit",
        help="Ansible host pattern to inspect a subset of the fleet",
    )
    observe_parser.add_argument(
        "--engine",
        type=Path,
        default=default_engine_directory(),
        help="engine directory holding the playbooks (default: bundled engine)",
    )
    observe_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )


def _add_inventory_argument(parser: StrictArgumentParser) -> None:
    """Let a command read the fleet from the team's own Ansible inventory."""
    parser.add_argument(
        "--inventory",
        type=Path,
        help=(
            "Ansible inventory to read the fleet from instead of a project "
            f"(default: the inventory an {ANSIBLE_CONFIG_FILE} in the current "
            "directory names)"
        ),
    )


def _add_add_parsers(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    add_parser = commands.add_parser(
        "add", help="write a fleet resource into the project"
    )
    add_commands = add_parser.add_subparsers(dest="add_command", required=True)

    key_parser = add_commands.add_parser(
        "ssh-key", help="declare an SSH public key read from a file"
    )
    key_parser.add_argument("key_file", type=Path, help="e.g. ~/.ssh/id_ed25519.pub")
    key_parser.add_argument("--owner", required=True, help="who the key belongs to")
    key_parser.add_argument("--id", help="resource id (default: the owner)")
    key_parser.add_argument(
        "--environment", default="production", help="(default: production)"
    )
    key_parser.add_argument("--description")

    type_parser = add_commands.add_parser(
        "server-type",
        help="declare a server type from the bundled Debian 13 baseline",
    )
    type_parser.add_argument("id", help="resource id, e.g. debian-application")
    type_parser.add_argument("--description")

    server_parser = add_commands.add_parser(
        "server", help="declare a server; creates its server type when missing"
    )
    server_parser.add_argument("id", help="resource id, e.g. h1")
    server_parser.add_argument(
        "--address", required=True, help="IP address or hostname to connect to"
    )
    server_parser.add_argument(
        "--type",
        default="debian-application",
        help="server type id (default: debian-application)",
    )
    server_parser.add_argument(
        "--environment", default="production", help="(default: production)"
    )
    server_parser.add_argument(
        "--hostname", help="(default: the address when it is a hostname, else the id)"
    )
    server_parser.add_argument("--ssh-user", default="root", help="(default: root)")
    server_parser.add_argument("--ssh-port", type=int, default=22, help="(default: 22)")
    server_parser.add_argument("--description")

    for subparser in (key_parser, type_parser, server_parser):
        _add_project_directory_argument(subparser)
        subparser.add_argument(
            "--schemas",
            type=Path,
            default=default_schema_directory(),
            help="versioned schema directory (default: bundled schemas)",
        )
    _add_why_parser(commands)


def _add_why_parser(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    """Add the command that answers "why did the agent do that"."""
    why_parser = commands.add_parser(
        "why",
        help=(
            "answer why the agent did that, from the record, for a host, an "
            "operation or a time window"
        ),
    )
    why_parser.add_argument(
        "--repository",
        type=Path,
        help="repository holding the decision records (default: the current directory)",
    )
    why_parser.add_argument(
        "--host", help="only decisions whose record names this host"
    )
    why_parser.add_argument(
        "--operation",
        type=resource_id_argument,
        help="only decisions of this declared operation",
    )
    why_parser.add_argument(
        "--since",
        help="only decisions with a moment at or after this ISO 8601 time or date",
    )
    why_parser.add_argument(
        "--until",
        help="only decisions with a moment at or before this ISO 8601 time or date",
    )
    why_parser.add_argument(
        "--format",
        choices=("json", "html"),
        default="json",
        help="answer as JSON or as one HTML page (default: json)",
    )
    why_parser.add_argument(
        "--decisions",
        type=project_path_argument,
        default=Path(DECISION_DIRECTORY),
        help=f"directory holding the decision records (default: {DECISION_DIRECTORY})",
    )
    why_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )


def _parser() -> StrictArgumentParser:
    parser = _command_parser()
    add_output_options(parser)
    return parser


def _command_parser() -> StrictArgumentParser:
    parser = root_parser("cloudfall")
    commands = parser.add_subparsers(dest="command", required=True)
    _add_projectless_parsers(commands)
    _add_add_parsers(commands)
    _add_config_parsers(commands)

    _add_observe_parser(commands)
    _add_operations_parsers(commands)

    audit_parser = commands.add_parser(
        "audit", help="compare the config with observed server snapshots"
    )
    _add_project_directory_argument(audit_parser)
    _add_inventory_argument(audit_parser)
    audit_parser.add_argument(
        "--observed",
        type=project_path_argument,
        required=True,
        help="directory containing observed-server JSON snapshots",
    )
    audit_parser.add_argument(
        "--env-receipts",
        type=project_path_argument,
        default=Path("tmp/env-receipts"),
        help=(
            "rendered environment receipt directory used for env-file "
            "drift checks (default: tmp/env-receipts)"
        ),
    )

    _add_operator_parsers(commands)
    _add_backup_parsers(commands)
    _add_secrets_parsers(commands)

    services_parser = commands.add_parser(
        "services", help="inspect and report public service lifecycles"
    )
    services_commands = services_parser.add_subparsers(
        dest="services_command", required=True
    )
    services_inspect_parser = services_commands.add_parser(
        "inspect", help="collect DNS, TLS, origin, and public route evidence"
    )
    _add_project_directory_argument(services_inspect_parser)
    services_inspect_parser.add_argument(
        "--output-dir",
        type=project_path_argument,
        default=Path("tmp/observed-services"),
        help="service observation directory (default: tmp/observed-services)",
    )
    services_inspect_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )
    services_status_parser = services_commands.add_parser(
        "status", help="derive service lifecycle status from current evidence"
    )
    _add_project_directory_argument(services_status_parser)
    services_status_parser.add_argument(
        "--observed",
        type=project_path_argument,
        required=True,
        help="directory containing observed-server JSON snapshots",
    )
    services_status_parser.add_argument(
        "--service-observed",
        type=project_path_argument,
        default=Path("tmp/observed-services"),
        help="domain observation directory (default: tmp/observed-services)",
    )
    services_status_parser.add_argument(
        "--deployments",
        type=project_path_argument,
        default=Path("tmp/deployments"),
        help="deployment receipt directory (default: tmp/deployments)",
    )
    services_status_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )
    audit_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )

    dashboard_parser = commands.add_parser(
        "dashboard", help="build operations dashboard artifacts"
    )
    dashboard_commands = dashboard_parser.add_subparsers(
        dest="dashboard_command", required=True
    )
    dashboard_build_parser = dashboard_commands.add_parser(
        "build", help="build a static read-only operations dashboard"
    )
    _add_project_directory_argument(dashboard_build_parser)
    dashboard_build_parser.add_argument(
        "--observed",
        type=project_path_argument,
        required=True,
        help="directory containing observed-server JSON snapshots",
    )
    dashboard_build_parser.add_argument(
        "--service-observed",
        type=project_path_argument,
        default=Path("tmp/observed-services"),
        help="domain observation directory (default: tmp/observed-services)",
    )
    dashboard_build_parser.add_argument(
        "--deployments",
        type=project_path_argument,
        default=Path("tmp/deployments"),
        help="deployment receipt directory (default: tmp/deployments)",
    )
    dashboard_build_parser.add_argument(
        "--output-dir",
        type=project_path_argument,
        default=Path("tmp/dashboard"),
        help="dashboard output directory (default: tmp/dashboard)",
    )
    dashboard_build_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )

    dashboard_serve_parser = dashboard_commands.add_parser(
        "serve", help="serve a live-refreshing read-only dashboard over HTTP"
    )
    _add_project_directory_argument(dashboard_serve_parser)
    dashboard_serve_parser.add_argument(
        "--observed",
        type=project_path_argument,
        required=True,
        help="directory containing observed-server JSON snapshots",
    )
    dashboard_serve_parser.add_argument(
        "--service-observed",
        type=project_path_argument,
        default=Path("tmp/observed-services"),
        help="domain observation directory (default: tmp/observed-services)",
    )
    dashboard_serve_parser.add_argument(
        "--deployments",
        type=project_path_argument,
        default=Path("tmp/deployments"),
        help="deployment receipt directory (default: tmp/deployments)",
    )
    dashboard_serve_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )
    dashboard_serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="listen address; use the VPS address to expose it (default: 127.0.0.1)",
    )
    dashboard_serve_parser.add_argument(
        "--port",
        type=int,
        default=8100,
        help="listen port; 0 picks a free port (default: 8100)",
    )
    dashboard_serve_parser.add_argument(
        "--refresh",
        type=int,
        default=10,
        help="evidence refresh interval in seconds (default: 10)",
    )
    dashboard_serve_parser.add_argument(
        "--inspect-services",
        action="store_true",
        help="probe DNS, TLS, origin, and public routes on every refresh",
    )

    _add_lifecycle_parsers(commands)
    _add_import_parsers(commands)
    _add_migrate_parser(commands)
    return parser


def _add_migrate_parser(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    migrate_parser = commands.add_parser(
        "migrate",
        help="run the resumable end-to-end migration plan",
    )
    _add_project_directory_argument(migrate_parser)
    migrate_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )
    migrate_parser.add_argument(
        "--engine",
        type=Path,
        default=default_engine_directory(),
        help="engine directory containing ansible contracts (default: bundled engine)",
    )
    migrate_parser.add_argument(
        "--inventory-file",
        type=project_path_argument,
        default=Path("tmp/ansible-inventory.json"),
        help="rendered inventory path (default: tmp/ansible-inventory.json)",
    )
    migrate_parser.add_argument(
        "--observed",
        type=project_path_argument,
        default=Path("tmp/observed"),
        help="server observation directory (default: tmp/observed)",
    )
    migrate_parser.add_argument(
        "--service-observed",
        type=project_path_argument,
        default=Path("tmp/observed-services"),
        help="domain observation directory (default: tmp/observed-services)",
    )
    migrate_parser.add_argument(
        "--deployments",
        type=project_path_argument,
        default=Path("tmp/deployments"),
        help="domain receipt directory (default: tmp/deployments)",
    )
    migrate_parser.add_argument(
        "--receipts",
        type=project_path_argument,
        default=Path("tmp/releases"),
        help="release receipt directory (default: tmp/releases)",
    )
    migrate_parser.add_argument(
        "--artifacts",
        type=project_path_argument,
        default=Path("tmp/artifacts"),
        help="artifact directory (default: tmp/artifacts)",
    )
    migrate_parser.add_argument(
        "--plan-file",
        type=project_path_argument,
        default=Path("tmp/migrate/plan.json"),
        help="persisted plan location (default: tmp/migrate/plan.json)",
    )
    migrate_parser.add_argument(
        "--build",
        action="append",
        default=[],
        metavar="COMPONENT=GIT_REF",
        help="build this component from a git ref (repeatable)",
    )
    migrate_parser.add_argument(
        "--release",
        action="append",
        default=[],
        metavar="COMPONENT=RELEASE_ID",
        help="deploy this component from an already-built release",
    )
    migrate_parser.add_argument(
        "--env-file",
        action="append",
        default=[],
        metavar="COMPONENT=PATH",
        help="environment file passed to this component's deployment",
    )
    migrate_parser.add_argument(
        "--data",
        action="append",
        default=[],
        metavar="DATABASE=URL_FILE",
        help=(
            "migrate external data into a declared database before cutover; "
            "URL_FILE is a controller file containing only the source "
            "connection URL"
        ),
    )
    migrate_parser.add_argument(
        "--yes",
        action="store_true",
        help="execute the plan; without this flag only the plan is shown",
    )
    migrate_parser.add_argument(
        "--restart",
        action="store_true",
        help="discard the persisted plan and start over",
    )


def _add_lifecycle_parsers(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    data_parser = commands.add_parser(
        "data", help="migrate data into declared services"
    )
    data_commands = data_parser.add_subparsers(dest="data_command", required=True)
    data_migrate_parser = data_commands.add_parser(
        "migrate",
        help=(
            "dump an external PostgreSQL database and restore it into a "
            "declared service with row-count verification"
        ),
    )
    _add_project_directory_argument(data_migrate_parser)
    data_migrate_parser.add_argument("service", type=resource_id_argument)
    _add_execute_argument(data_migrate_parser)
    data_migrate_parser.add_argument(
        "--database",
        required=True,
        help="declared database name inside the service",
    )
    data_migrate_parser.add_argument(
        "--source-url-file",
        type=Path,
        required=True,
        help=(
            "controller-side file whose only content is the source "
            "database connection URL"
        ),
    )
    data_migrate_parser.add_argument(
        "--receipts",
        type=project_path_argument,
        default=Path("tmp/data-migrations"),
        help="migration receipt directory (default: tmp/data-migrations)",
    )
    data_migrate_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )
    data_migrate_parser.add_argument(
        "--engine",
        type=Path,
        default=default_engine_directory(),
        help="engine directory containing ansible contracts (default: bundled engine)",
    )
    data_migrate_parser.add_argument(
        "--inventory-file",
        type=project_path_argument,
        default=Path("tmp/ansible-inventory.json"),
        help="rendered inventory path (default: tmp/ansible-inventory.json)",
    )

    deploy_parser = commands.add_parser(
        "deploy", help="deploy one built component release"
    )
    _add_lifecycle_arguments(deploy_parser)
    _add_execute_argument(deploy_parser)
    deploy_parser.add_argument(
        "--release",
        type=release_id_argument,
        required=True,
        help="release id produced by cloudfall-engine artifact build",
    )
    deploy_parser.add_argument(
        "--artifacts",
        type=project_path_argument,
        default=Path("tmp/artifacts"),
        help="artifact directory (default: tmp/artifacts)",
    )
    deploy_parser.add_argument(
        "--env-file",
        type=Path,
        help="optional controller-side environment file for the component",
    )
    deploy_parser.add_argument(
        "--receipts",
        type=project_path_argument,
        default=Path("tmp/releases"),
        help="release receipt directory (default: tmp/releases)",
    )

    rollback_parser = commands.add_parser(
        "rollback", help="switch one component back to an existing release"
    )
    _add_lifecycle_arguments(rollback_parser)
    _add_execute_argument(rollback_parser)
    rollback_parser.add_argument(
        "--release",
        type=release_id_argument,
        required=True,
        help="existing release id to activate",
    )

    restart_parser = commands.add_parser(
        "restart", help="restart one component behind its health check"
    )
    _add_lifecycle_arguments(restart_parser)
    _add_execute_argument(restart_parser)

    health_parser = commands.add_parser(
        "health", help="probe one component's declared health check"
    )
    _add_lifecycle_arguments(health_parser)


def _add_import_parsers(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    import_parser = commands.add_parser(
        "import", help="import external platform definitions"
    )
    import_commands = import_parser.add_subparsers(dest="import_command", required=True)
    render_parser = import_commands.add_parser(
        "render", help="map a render.yaml blueprint onto Cloudfall config"
    )
    _add_project_directory_argument(render_parser)
    render_parser.add_argument("blueprint", type=Path)
    render_parser.add_argument(
        "--application",
        type=resource_id_argument,
        required=True,
        help="Cloudfall application id (also the application's Linux user)",
    )
    render_parser.add_argument(
        "--server",
        type=resource_id_argument,
        required=True,
        help="declared server id that receives every imported resource",
    )
    render_parser.add_argument(
        "--output-dir",
        type=project_path_argument,
        default=Path("tmp/import/config"),
        help="config fragment output directory (default: tmp/import/config)",
    )
    render_parser.add_argument(
        "--env-dir",
        type=project_path_argument,
        default=Path("tmp/import/env"),
        help="environment file output directory (default: tmp/import/env)",
    )
    render_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )
    render_api_parser = import_commands.add_parser(
        "render-api",
        help="map a live Render workspace onto Cloudfall config via the API",
    )
    _add_project_directory_argument(render_api_parser)
    render_api_parser.add_argument(
        "--api-key-file",
        type=Path,
        required=True,
        help="file containing only the Render API key",
    )
    render_api_parser.add_argument(
        "--api-url",
        default="https://api.render.com/v1",
        help="Render API base URL (default: https://api.render.com/v1)",
    )
    render_api_parser.add_argument(
        "--application",
        type=resource_id_argument,
        required=True,
        help="Cloudfall application id (also the application's Linux user)",
    )
    render_api_parser.add_argument(
        "--server",
        type=resource_id_argument,
        required=True,
        help="declared server id that receives every imported resource",
    )
    render_api_parser.add_argument(
        "--output-dir",
        type=project_path_argument,
        default=Path("tmp/import/config"),
        help="config fragment output directory (default: tmp/import/config)",
    )
    render_api_parser.add_argument(
        "--env-dir",
        type=project_path_argument,
        default=Path("tmp/import/env"),
        help="environment file output directory (default: tmp/import/env)",
    )
    render_api_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )


def _add_operator_parsers(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    operator_parser = commands.add_parser(
        "operator", help="alert-driven propose-and-approve operation"
    )
    operator_commands = operator_parser.add_subparsers(
        dest="operator_command", required=True
    )
    operator_run_parser = operator_commands.add_parser(
        "run", help="watch declared alerts and drift, write proposals"
    )
    _add_operator_arguments(operator_run_parser)
    _add_operator_feed_arguments(operator_run_parser, required=True)
    _add_operator_engine_arguments(operator_run_parser)
    operator_run_parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="seconds between watch passes (default: one pass, then exit)",
    )
    operator_run_parser.add_argument(
        "--drift-interval",
        type=float,
        default=None,
        help=(
            "seconds between audited drift checks (default: no drift "
            "checks; a single pass runs one when set)"
        ),
    )
    operator_run_parser.add_argument(
        "--observed",
        type=project_path_argument,
        default=Path("tmp/operator/observed"),
        help=(
            "observation directory for drift checks (default: tmp/operator/observed)"
        ),
    )
    operator_list_parser = operator_commands.add_parser(
        "list", help="list proposal receipts"
    )
    _add_operator_arguments(operator_list_parser)
    operator_show_parser = operator_commands.add_parser(
        "show", help="show one proposal receipt"
    )
    _add_operator_arguments(operator_show_parser)
    operator_show_parser.add_argument("proposal", type=resource_id_argument)
    operator_approve_parser = operator_commands.add_parser(
        "approve", help="execute a proposal and verify its trigger resolves"
    )
    _add_operator_arguments(operator_approve_parser)
    _add_operator_feed_arguments(operator_approve_parser, required=False)
    _add_operator_engine_arguments(operator_approve_parser)
    operator_approve_parser.add_argument("proposal", type=resource_id_argument)
    operator_approve_parser.add_argument(
        "--observed",
        type=project_path_argument,
        default=Path("tmp/operator/observed"),
        help=(
            "observation directory for drift verification "
            "(default: tmp/operator/observed)"
        ),
    )
    operator_approve_parser.add_argument(
        "--verify-timeout",
        type=float,
        default=180.0,
        help="seconds to wait for the trigger to resolve (default: 180)",
    )


def _add_secrets_parsers(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    secrets_parser = commands.add_parser(
        "secrets", help="resolve declared secret references"
    )
    secrets_commands = secrets_parser.add_subparsers(
        dest="secrets_command", required=True
    )
    render_parser = secrets_commands.add_parser(
        "render",
        help="render one component's references into its environment file",
    )
    _add_project_directory_argument(render_parser)
    render_parser.add_argument("component", type=resource_id_argument)
    render_parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )
    render_parser.add_argument(
        "--secrets-dir",
        type=Path,
        default=Path("secrets"),
        help="sops-encrypted secrets directory (default: secrets)",
    )
    render_parser.add_argument(
        "--output-file",
        type=project_path_argument,
        default=None,
        help=("environment file to write (default: tmp/env/<component>.env)"),
    )
    render_parser.add_argument(
        "--receipts",
        type=project_path_argument,
        default=Path("tmp/env-receipts"),
        help=("environment receipt directory (default: tmp/env-receipts)"),
    )
    render_parser.add_argument(
        "--engine",
        type=Path,
        default=default_engine_directory(),
        help="engine directory containing ansible contracts (default: bundled engine)",
    )
    render_parser.add_argument(
        "--inventory-file",
        type=project_path_argument,
        default=Path("tmp/ansible-inventory.json"),
        help="rendered inventory path (default: tmp/ansible-inventory.json)",
    )


def _add_backup_parsers(
    commands: argparse._SubParsersAction[StrictArgumentParser],
) -> None:
    backup_parser = commands.add_parser(
        "backup", help="run and prove declared service backups"
    )
    backup_commands = backup_parser.add_subparsers(dest="backup_command", required=True)
    for name, description in (
        ("run", "run the declared backup for one service"),
        ("verify", "prove the newest backup restores for one service"),
    ):
        subparser = backup_commands.add_parser(name, help=description)
        _add_project_directory_argument(subparser)
        subparser.add_argument("service", type=resource_id_argument)
        subparser.add_argument(
            "--schemas",
            type=Path,
            default=default_schema_directory(),
            help="versioned schema directory (default: bundled schemas)",
        )
        subparser.add_argument(
            "--receipts",
            type=project_path_argument,
            default=Path("tmp/backups"),
            help="backup receipt directory (default: tmp/backups)",
        )
        _add_operator_engine_arguments(subparser)


def _add_operator_arguments(parser: argparse.ArgumentParser) -> None:
    _add_project_directory_argument(parser)
    parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )
    parser.add_argument(
        "--proposals",
        type=project_path_argument,
        default=Path("tmp/operator/proposals"),
        help="proposal receipt directory (default: tmp/operator/proposals)",
    )


def _add_operator_feed_arguments(
    parser: argparse.ArgumentParser, *, required: bool
) -> None:
    parser.add_argument(
        "--gateway-url",
        default=None,
        help=("alerts endpoint (default: derived from the declared logging gateway)"),
    )
    parser.add_argument("--gateway-ca", type=Path, required=required)
    parser.add_argument("--gateway-cert", type=Path, required=required)
    parser.add_argument("--gateway-key", type=Path, required=required)


def _add_operator_engine_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--engine",
        type=Path,
        default=default_engine_directory(),
        help="engine directory containing ansible contracts (default: bundled engine)",
    )
    parser.add_argument(
        "--inventory-file",
        type=project_path_argument,
        default=Path("tmp/ansible-inventory.json"),
        help="rendered inventory path (default: tmp/ansible-inventory.json)",
    )


def _add_execute_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "change the servers; without this flag the command validates the "
            "request and only shows what it would do"
        ),
    )


def _add_lifecycle_arguments(parser: argparse.ArgumentParser) -> None:
    _add_project_directory_argument(parser)
    parser.add_argument("component", type=resource_id_argument)
    parser.add_argument(
        "--schemas",
        type=Path,
        default=default_schema_directory(),
        help="versioned schema directory (default: bundled schemas)",
    )
    parser.add_argument(
        "--engine",
        type=Path,
        default=default_engine_directory(),
        help="engine directory containing ansible contracts (default: bundled engine)",
    )
    parser.add_argument(
        "--inventory-file",
        type=project_path_argument,
        default=Path("tmp/ansible-inventory.json"),
        help="rendered inventory path (default: tmp/ansible-inventory.json)",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code.

    A command already moved to treaty runs there; argparse answers every
    other command line, root ``--help`` and ``--version`` included, until the
    last command moves (see ``cloudfall.app``).
    """
    words = list(sys.argv[1:] if argv is None else argv)
    if treaty_app.resolves(words):
        return treaty_app.run(words)
    begin_invocation()
    try:
        code = _main(words)
    except OSError as error:
        # A path the command must write is read-only or not ours: a fact
        # about the environment, reported like any other failure. Every
        # other OS error is a bug and keeps its traceback.
        if error.errno not in _NOT_WRITABLE:
            raise
        message = f"cannot write {error.filename}: {error.strerror}"
        error_body = {"code": "path_not_writable", "message": message}
        # Exit 1, not 2: the command may have done part of its work first.
        code = write_error({"status": "error", "error": error_body}, 1)
    return exit_code(code)


_NOT_WRITABLE = frozenset({errno.EACCES, errno.EPERM, errno.EROFS})


def _main(argv: Sequence[str] | None) -> int:
    arguments = parse_arguments(_parser(), argv)
    projectless_command = _PROJECTLESS_COMMANDS.get(arguments.command)
    if projectless_command is not None:
        return projectless_command(arguments)
    try:
        record_command = _RECORD_COMMANDS.get(arguments.command)
        if record_command is not None:
            return record_command(arguments)
        source = _inventory_source(arguments, os.environ)
        if source is not None:
            return _dispatch_from_inventory(arguments, source)
        with project_context(arguments.project, os.environ) as project_directory:
            arguments.project_directory = project_directory
            without_state = _run_without_validated_fleet(arguments)
            if without_state is not None:
                return without_state
            schema_directory = Path(arguments.schemas)
            state = validate_config(project_directory, schema_directory)
            return _dispatch(arguments, state, schema_directory)
    except (
        ProjectError,
        ConfigValidationError,
        AnsibleReadError,
        DecisionError,
        WhyError,
    ) as error:
        return write_error(error.as_dict(), 2)


_ERROR_SOURCE_AMBIGUOUS = "project_source_ambiguous"
_RENDERED_INVENTORY = Path("tmp/ansible-inventory.json")
_OVERLAY_DIRECTORY = "tmp/cloudfall"


def _run_without_validated_fleet(arguments: Namespace) -> int | None:
    """Run the commands that read no fleet, or return ``None`` for the rest.

    `import` writes the fleet rather than reads it (`add` runs on treaty).
    """
    if arguments.command == "import":
        return _run_import_render(arguments)
    return None


def _inventory_source(
    arguments: Namespace, environment: Mapping[str, str]
) -> InventorySource | None:
    """Return the Ansible inventory this run reads the fleet from, if any.

    Precedence: ``--inventory``, then an ``ansible.cfg`` in the current
    directory when no project was asked for. A command that declares no
    ``--inventory`` never reads one, so brownfield support is stated per
    command rather than implied for all of them.
    """
    if not hasattr(arguments, "inventory"):
        return None
    if arguments.inventory is not None:
        if arguments.project is not None:
            message = "--inventory and --project name two fleets; pass one"
            raise ProjectError(_ERROR_SOURCE_AMBIGUOUS, message)
        return InventorySource.from_boundary(arguments.inventory)
    if arguments.project is not None or environment.get(PROJECT_DIRECTORY_VARIABLE):
        return None
    if is_project(Path.cwd()):
        return None
    return inventory_from_config(Path.cwd())


def _dispatch_from_inventory(arguments: Namespace, source: InventorySource) -> int:
    """Run the command against a fleet read from the team's inventory.

    The Ansible repository is the working directory, so relative paths such
    as the ``tmp/`` defaults land beside their playbooks rather than in a
    Cloudfall project that does not exist here.
    """
    schema_directory = Path(arguments.schemas)
    read = read_fleet(read_inventory(source), schema_directory)
    arguments.project_directory = Path.cwd()
    arguments.fleet_read = read
    arguments.inventory_source = source
    return _dispatch(arguments, read.config, schema_directory)


def _run_import_render(arguments: Namespace) -> int:
    try:
        targets = ImportTargets(
            application_id=arguments.application,
            server_id=arguments.server,
            project_directory=Path(arguments.output_dir),
            environment_directory=Path(arguments.env_dir),
        )
        if arguments.import_command == "render-api":
            api_key = read_api_key(Path(arguments.api_key_file))
            client = HttpRenderApiClient(api_key=api_key, base_url=arguments.api_url)
            result = import_render_api(client, targets, Path(arguments.schemas))
        else:
            result = import_render_blueprint(
                Path(arguments.blueprint), targets, Path(arguments.schemas)
            )
    except (RenderImportError, ConfigValidationError) as error:
        return write_error(error.as_dict(), 2)
    write_result(result.as_dict())
    return 0


def _dispatch(
    arguments: Namespace, state: ValidatedConfig, schema_directory: Path
) -> int:
    handlers: dict[str, Callable[[Namespace, ValidatedConfig, Path], int]] = {
        "secrets:render": _run_secrets_render,
        "data:migrate": _run_data_migrate,
        "dashboard:serve": _run_dashboard_serve,
        "deploy": _run_deploy,
        "observe": _run_observe,
        "operator:run": _run_operator_run,
        "restart": _run_restart,
        "rollback": _run_rollback,
    }
    key = _command_key(arguments)
    try:
        handler = handlers[key]
    except KeyError as error:
        message = f"argparse accepted an unsupported command: {key}"
        raise RuntimeError(message) from error
    return handler(arguments, state, schema_directory)


def _command_key(arguments: Namespace) -> str:
    top_level = {
        "audit",
        "deploy",
        "health",
        "migrate",
        "observe",
        "restart",
        "rollback",
    }
    if arguments.command in top_level:
        return str(arguments.command)
    subcommand = getattr(arguments, f"{arguments.command}_command", None)
    return f"{arguments.command}:{subcommand}"


def _run_why(arguments: Namespace) -> int:
    """Answer the question from the record alone: no catalog, no fleet.

    The record outlives both, so the question must be answerable from a
    checkout that holds nothing but the decisions directory.
    """
    repository = (
        Path(arguments.repository) if arguments.repository is not None else Path.cwd()
    )
    query = WhyQuery.from_boundary(
        host=arguments.host,
        operation=(
            arguments.operation.value if arguments.operation is not None else None
        ),
        since=arguments.since,
        until=arguments.until,
    )
    result = answer(_decision_store(arguments, repository), query)
    if arguments.format == "html":
        sys.stdout.write(render_why_html(result))
        sys.stdout.flush()
        return 0
    write_result(result.as_dict())
    return 0


def _decision_store(arguments: Namespace, repository: Path) -> DecisionStore:
    return DecisionStore(
        directory=repository / Path(arguments.decisions),
        catalog=SchemaCatalog(Path(arguments.schemas)),
    )


def _run_changelog(arguments: Namespace) -> int:
    write_result(
        {
            "status": "ok",
            "entries": schema_changes_since(arguments.since),
            "schemaVersions": schema_versions(),
        }
    )
    return 0


_PROJECTLESS_COMMANDS: Mapping[str, Callable[[Namespace], int]] = {
    "changelog": _run_changelog,
}
"""The commands that must run before a project exists, so none is resolved."""


_RECORD_COMMANDS: Mapping[str, Callable[[Namespace], int]] = {
    "why": _run_why,
}
"""The commands that read the repository and nothing else: no fleet, no project."""


def _run_observe(
    arguments: Namespace, state: ValidatedConfig, _schema_directory: Path
) -> int:
    """Inspect every declared server and write one snapshot each."""
    source = getattr(arguments, "inventory_source", None)
    sources = (
        (source.value,)
        if source is not None
        else (Path(arguments.project_directory) / _RENDERED_INVENTORY,)
    )
    try:
        request = ObservationRequest(
            inventory_sources=sources,
            output_directory=Path(arguments.output_dir).resolve(),
            engine_directory=Path(arguments.engine),
            limit=arguments.limit,
            configuration=(
                team_configuration(Path.cwd()) if source is not None else None
            ),
        )
        result = collect_observations(
            PlatformInventory.from_state(state),
            request,
            Path(_OVERLAY_DIRECTORY),
        )
    except ObserveError as error:
        return write_error(error.as_dict(), 2)
    code = 0 if result.complete else 1
    write_result(result.as_dict(), exit_code=code)
    return code


def _run_secrets_render(
    arguments: Namespace, _state: ValidatedConfig, schema_directory: Path
) -> int:
    output = (
        Path(arguments.output_file)
        if arguments.output_file is not None
        else Path("tmp/env") / f"{arguments.component}.env"
    )
    try:
        result = render_environment(
            _engine_context(arguments, schema_directory),
            arguments.component,
            SopsSecretProvider(secrets_directory=Path(arguments.secrets_dir)),
            output,
            receipt_directory=Path(arguments.receipts),
        )
    except SecretsError as error:
        return write_error(error.as_dict(), 2)
    write_result(result)
    return 0


def _operator_store(arguments: Namespace, schema_directory: Path) -> ProposalStore:
    return ProposalStore(
        directory=Path(arguments.proposals),
        catalog=SchemaCatalog(schema_directory),
    )


def _operator_feed(arguments: Namespace, inventory: PlatformInventory) -> AlertFeed:
    return gateway_feed(
        inventory,
        ca_path=Path(arguments.gateway_ca),
        certificate_path=Path(arguments.gateway_cert),
        key_path=Path(arguments.gateway_key),
        url_override=arguments.gateway_url,
    )


def _operator_exit(error: OperatorError) -> int:
    return write_error(error.as_dict(), 2)


def _run_operator_run(
    arguments: Namespace, state: ValidatedConfig, schema_directory: Path
) -> int:
    inventory = PlatformInventory.from_state(state)
    try:
        store = _operator_store(arguments, schema_directory)
        feed = _operator_feed(arguments, inventory)
        auditor = None
        if arguments.drift_interval is not None:
            auditor = engine_auditor(
                _engine_context(arguments, schema_directory),
                inventory,
                Path(arguments.observed),
            )
        context = _engine_context(arguments, schema_directory)

        def _verifier_for(
            proposal: OperatorProposal,
        ) -> Callable[[OperatorProposal], bool]:
            if proposal.trigger_kind is TriggerKind.ALERT:
                return alert_resolution_verifier(feed)
            return drift_resolution_verifier(
                engine_auditor(context, inventory, Path(arguments.observed))
            )

        drift_due = 0.0
        while True:
            report = operator_run_once(feed, inventory, store)
            write_result({"status": "ok", "pass": "alerts", **report.as_dict()})
            if auditor is not None and time.monotonic() >= drift_due:
                drift_report = drift_pass(auditor, store)
                write_result(
                    {
                        "status": "ok",
                        "pass": "drift",
                        **drift_report.as_dict(),
                    }
                )
                drift_due = time.monotonic() + arguments.drift_interval
            if inventory.operator_policies:
                autonomy_report = autonomous_pass(
                    store,
                    inventory,
                    engine_executor(context),
                    _verifier_for,
                )
                write_result(
                    {
                        "status": "ok",
                        "pass": "autonomy",
                        **autonomy_report.as_dict(),
                    }
                )
            if arguments.interval is None:
                return 0
            time.sleep(arguments.interval)
    except OperatorError as error:
        return _operator_exit(error)


def _run_dashboard_serve(
    arguments: Namespace, _state: ValidatedConfig, schema_directory: Path
) -> int:
    sources = EvidenceSources(
        project_directory=Path(arguments.project_directory),
        schema_directory=schema_directory,
        observed_directory=Path(arguments.observed),
        service_observed_directory=Path(arguments.service_observed),
        deployments_directory=Path(arguments.deployments),
        inspect_services=bool(arguments.inspect_services),
    )
    endpoint = ListenEndpoint(host=str(arguments.host), port=int(arguments.port))
    refresh = RefreshInterval.from_boundary(int(arguments.refresh))
    server = create_dashboard_server(
        sources, endpoint, refresh, quiet=current_invocation().options.quiet
    )
    bound_port = int(server.server_address[1])
    write_result(
        {
            "status": "ok",
            "dashboard": {
                "url": f"http://{endpoint.host}:{bound_port}/",
                "refreshSeconds": refresh.seconds,
                "inspectServices": sources.inspect_services,
            },
        }
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def _service_operations(
    arguments: Namespace, state: ValidatedConfig, schema_directory: Path
) -> FleetOperations:
    observations = load_observations(Path(arguments.observed), schema_directory)
    deployment_receipts = _optional_deployment_receipts(
        Path(arguments.deployments), schema_directory
    )
    domain_observations = _optional_domain_observations(
        Path(arguments.service_observed), schema_directory
    )
    return build_operations_view(
        PlatformInventory.from_state(state),
        observations,
        deployment_receipts,
        domain_observations,
        generated_at=UtcTimestamp.now(),
    )


def _engine_context(arguments: Namespace, schema_directory: Path) -> EngineContext:
    return EngineContext(
        project_directory=Path(arguments.project_directory),
        schema_directory=schema_directory,
        engine_directory=Path(arguments.engine),
        inventory_file=Path(arguments.inventory_file),
    )


def _lifecycle_exit(error: LifecycleError) -> int:
    code = 1 if error.code == "lifecycle_execution_failed" else 2
    return write_error(error.as_dict(), code)


def _run_data_migrate(
    arguments: Namespace, _state: ValidatedConfig, schema_directory: Path
) -> int:
    context = _engine_context(arguments, schema_directory)
    try:
        if not arguments.yes:
            return _write_plan(
                preview_data_migration(
                    context,
                    arguments.service,
                    str(arguments.database),
                    Path(arguments.source_url_file),
                )
            )
        result = migrate_data(
            context,
            arguments.service,
            str(arguments.database),
            Path(arguments.source_url_file),
            Path(arguments.receipts),
        )
    except LifecycleError as error:
        return _lifecycle_exit(error)
    write_result(result)
    return 0


def _run_deploy(
    arguments: Namespace, _state: ValidatedConfig, schema_directory: Path
) -> int:
    context = _engine_context(arguments, schema_directory)
    try:
        if not arguments.yes:
            return _write_plan(
                preview_deploy(
                    context,
                    arguments.component,
                    arguments.release,
                    Path(arguments.artifacts),
                )
            )
        result = deploy(
            context,
            arguments.component,
            arguments.release,
            Path(arguments.artifacts),
            DeployOptions(
                environment_file=(
                    Path(arguments.env_file) if arguments.env_file is not None else None
                ),
                receipt_directory=Path(arguments.receipts),
            ),
        )
    except LifecycleError as error:
        return _lifecycle_exit(error)
    write_result(result.as_dict())
    return 0


def _run_rollback(
    arguments: Namespace, _state: ValidatedConfig, schema_directory: Path
) -> int:
    context = _engine_context(arguments, schema_directory)
    try:
        if not arguments.yes:
            return _write_plan(
                preview_rollback(context, arguments.component, arguments.release)
            )
        result = rollback(
            context,
            arguments.component,
            arguments.release,
        )
    except LifecycleError as error:
        return _lifecycle_exit(error)
    write_result(result.as_dict())
    return 0


def _run_restart(
    arguments: Namespace, _state: ValidatedConfig, schema_directory: Path
) -> int:
    context = _engine_context(arguments, schema_directory)
    try:
        if not arguments.yes:
            return _write_plan(preview_restart(context, arguments.component))
        result = restart(context, arguments.component)
    except LifecycleError as error:
        return _lifecycle_exit(error)
    write_result(result.as_dict())
    return 0


def _write_plan(preview: LifecyclePreview) -> int:
    write_result(
        {
            **preview.as_dict(),
            "instruction": "review the plan and re-run with --yes to execute it",
        }
    )
    return 0


def run() -> None:
    """Installed console-script entry point."""
    raise SystemExit(main())


def _optional_deployment_receipts(
    directory: Path, schema_directory: Path
) -> DeploymentReceiptSet:
    return (
        load_deployment_receipts(directory, schema_directory)
        if directory.is_dir()
        else DeploymentReceiptSet.empty()
    )


def _optional_domain_observations(
    directory: Path, schema_directory: Path
) -> DomainObservationSet:
    return (
        load_domain_observations(directory, schema_directory)
        if directory.is_dir()
        else DomainObservationSet.empty()
    )
