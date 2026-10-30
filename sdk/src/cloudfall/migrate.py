"""Resumable migration orchestrator chaining the platform milestones.

One plan drives a migration end to end: server baseline, infrastructure
services, artifact builds, health-gated deployments, HTTP routes, a DNS
verification pause, TLS issuance, and a final evidence pass that proves
the result with inspection, audit compliance, and healthy routes. Step
progress persists to a plan file, so failed or deliberately paused runs
resume at the first incomplete step.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from ipaddress import ip_address
from typing import TYPE_CHECKING, cast

from cloudfall.audit import AuditStatus, audit_inventory
from cloudfall.cutover import (
    SystemDnsProbe,
    check_ttl,
    parallel_run,
    rollback_instructions,
)
from cloudfall.domain import ReleaseId, ResourceId, TcpPort
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import (
    DataCopySchemas,
    DeployOptions,
    LifecycleError,
    build_release_artifact,
    deploy,
    migrate_data,
    run_engine_playbook,
    run_services_playbook,
    services_passwords,
)
from cloudfall.observation import load_observations
from cloudfall.operations import (
    OperationsHealth,
    UtcTimestamp,
    build_operations_view,
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
from cloudfall.validation import validate_config

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from cloudfall.agent_tools import AgentConfig
    from cloudfall.inventory import DomainInventory, ServerInventory
    from cloudfall.secrets import OwnedRolePassword

    Runner = Callable[[], dict[str, object]]

_PLAN_VERSION = 1
_DNS_PROBE_PORT = 443
_ERROR_COMPONENT_UNKNOWN = "migrate_component_unknown"
_ERROR_COMPONENT_UNBUILT = "migrate_component_unbuilt"
_ERROR_PLAN_STALE = "migrate_plan_stale"
_ERROR_PLAN_INVALID = "migrate_plan_invalid"
_ERROR_RELEASE_MISSING = "migrate_release_missing"
_ERROR_DNS_UNVERIFIED = "migrate_dns_unverified"
_ERROR_TTL_HIGH = "migrate_ttl_high"
_ERROR_PARALLEL_RUN = "migrate_parallel_run_failed"
_MAX_CUTOVER_TTL_SECONDS = 300
_ROLLBACK_WINDOW_HOURS = 24
_PAUSE_CODES = frozenset({_ERROR_DNS_UNVERIFIED, _ERROR_TTL_HIGH})
_ERROR_AUDIT_DRIFT = "migrate_audit_drift"
_ERROR_ROUTES_UNHEALTHY = "migrate_routes_unhealthy"
_ERROR_OBSERVATIONS_MISSING = "migrate_observations_missing"
_ERROR_DATABASE_UNKNOWN = "migrate_database_unknown"
_ERROR_DATA_SCHEMA_WITHOUT_DATA = "migrate_data_schema_without_data"


class MigrateError(RuntimeError):
    """Fail-fast migration error with a stable machine-readable code."""

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


class StepStatus(StrEnum):
    """Persisted status of one migration step."""

    PENDING = "pending"
    COMPLETED = "completed"


@dataclass(slots=True)
class PlanStep:
    """One resumable migration step."""

    step_id: str
    description: str
    status: StepStatus = StepStatus.PENDING
    completed_at: str | None = None
    detail: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        """Serialize the step for the plan file and result envelopes."""
        result: dict[str, object] = {
            "id": self.step_id,
            "description": self.description,
            "status": self.status.value,
        }
        if self.completed_at is not None:
            result["completedAt"] = self.completed_at
        if self.detail is not None:
            result["detail"] = self.detail
        return result


@dataclass(frozen=True, slots=True)
class MigrateOptions:
    """Inputs selecting what one migration run builds and deploys."""

    plan_file: Path
    builds: Mapping[str, str]
    releases: Mapping[str, str]
    environment_files: Mapping[str, Path]
    data_migrations: Mapping[str, Path] = field(default_factory=dict)
    data_schemas: Mapping[str, DataCopySchemas] = field(default_factory=dict)
    """The schemas each ``data:<database>`` step copies; absent copies all."""
    execute: bool = False
    restart: bool = False


def execute_migration(
    config: AgentConfig,
    options: MigrateOptions,
    runners: Mapping[str, Runner] | None = None,
) -> dict[str, object]:
    """Run or preview the migration plan, resuming persisted progress."""
    state = validate_config(config.project_directory, config.schema_directory)
    inventory = PlatformInventory.from_state(state)
    _require_known_components(inventory, options)
    steps = _load_plan(options, _computed_steps(inventory, options))
    if not options.execute:
        return _envelope("plan", steps)

    releases = _recover_releases(steps, options)
    # An owner password that can't be read stops the run before any step,
    # so no playbook starts (S-006-7).
    passwords = (
        _services_passwords(config)
        if any(
            step.step_id == "services" and step.status is StepStatus.PENDING
            for step in steps
        )
        else ()
    )
    resolved_runners = (
        runners
        if runners is not None
        else _build_runners(
            config, options, inventory, releases, steps, passwords
        )
    )
    for step in steps:
        if step.status is StepStatus.COMPLETED:
            continue
        try:
            step.detail = resolved_runners[step.step_id]()
        except (MigrateError, LifecycleError) as error:
            _save_plan(options.plan_file, steps)
            envelope = _envelope(
                "paused"
                if isinstance(error, MigrateError)
                and error.code in _PAUSE_CODES
                else "error",
                steps,
            )
            envelope["step"] = step.step_id
            envelope["error"] = error.as_dict()["error"]
            return envelope
        step.status = StepStatus.COMPLETED
        step.completed_at = datetime.now(tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        _save_plan(options.plan_file, steps)
    return _envelope("ok", steps)


def _envelope(status: str, steps: list[PlanStep]) -> dict[str, object]:
    pending = [
        step.step_id for step in steps if step.status is StepStatus.PENDING
    ]
    envelope: dict[str, object] = {
        "status": status,
        "steps": [step.as_dict() for step in steps],
        "completed": sum(
            1 for step in steps if step.status is StepStatus.COMPLETED
        ),
    }
    if pending:
        envelope["next"] = pending[0]
    return envelope


def _computed_steps(
    inventory: PlatformInventory, options: MigrateOptions
) -> list[PlanStep]:
    steps = [
        PlanStep("baseline", "converge every server to the managed baseline"),
        PlanStep(
            "services", "converge every declared infrastructure service"
        ),
    ]
    if inventory.domains:
        steps.append(
            PlanStep(
                "ttl-lower",
                "measure authoritative DNS TTLs and require them at or "
                f"below {_MAX_CUTOVER_TTL_SECONDS}s before the cutover",
            )
        )
    steps.extend(
        PlanStep(
            f"data:{database}",
            f"migrate {_data_schemas(options, database).describe()} of the "
            f"external data into database {database} with row-count "
            "verification",
        )
        for database in sorted(options.data_migrations)
    )
    for component in inventory.components:
        component_id = component.resource_id.value
        if component_id not in options.releases:
            steps.append(
                PlanStep(
                    f"build:{component_id}",
                    f"build a release artifact for {component_id}",
                )
            )
        steps.append(
            PlanStep(
                f"deploy:{component_id}",
                f"deploy {component_id} behind its health gate",
            )
        )
    if inventory.domains:
        steps.extend(
            (
                PlanStep(
                    "domains-http",
                    "render every declared domain route over HTTP",
                ),
                PlanStep(
                    "parallel-run",
                    "prove the new origin serves every declared domain "
                    "before any DNS record changes",
                ),
                PlanStep(
                    "dns-verify",
                    "verify public DNS points at the declared proxy servers",
                ),
                PlanStep(
                    "domains-tls",
                    "issue certificates and enable HTTPS routes",
                ),
            )
        )
    steps.extend(
        (
            PlanStep("inspect", "collect read-only server evidence"),
            PlanStep("audit", "require a compliant desired-state audit"),
        )
    )
    if inventory.domains:
        steps.extend(
            (
                PlanStep(
                    "verify-routes",
                    "require every declared route to be healthy",
                ),
                PlanStep(
                    "rollback-window",
                    "record the pre-switch DNS answers and the explicit "
                    f"{_ROLLBACK_WINDOW_HOURS}h rollback recipe",
                ),
            )
        )
    return steps


def _require_known_components(
    inventory: PlatformInventory, options: MigrateOptions
) -> None:
    declared = {component.resource_id.value for component in inventory.components}
    for source_name, mapping in (
        ("--build", options.builds),
        ("--release", options.releases),
        ("--env-file", options.environment_files),
    ):
        unknown = sorted(set(mapping) - declared)
        if unknown:
            detail = (
                f"{source_name} references undeclared components: "
                f"{', '.join(unknown)}"
            )
            raise MigrateError(_ERROR_COMPONENT_UNKNOWN, detail)
    unbuilt = sorted(
        declared - set(options.builds) - set(options.releases)
    )
    if unbuilt:
        detail = (
            "declare a git ref (--build component=ref) or a pinned release "
            f"(--release component=id) for: {', '.join(unbuilt)}"
        )
        raise MigrateError(_ERROR_COMPONENT_UNBUILT, detail)
    _require_known_databases(inventory, options)


def _require_known_databases(
    inventory: PlatformInventory, options: MigrateOptions
) -> None:
    declared_databases = {
        database.name.value
        for service in inventory.services
        if service.postgresql is not None
        for database in service.postgresql.databases
    }
    unknown = sorted(set(options.data_migrations) - declared_databases)
    if unknown:
        detail = (
            "--data references databases not declared on any PostgreSQL "
            f"service: {', '.join(unknown)}"
        )
        raise MigrateError(_ERROR_DATABASE_UNKNOWN, detail)
    # A schema for a database nothing is copied into would be silently
    # ignored; refuse it before any step runs (S-007-12).
    unpaired = sorted(set(options.data_schemas) - set(options.data_migrations))
    if unpaired:
        detail = (
            "--data-schema names databases with no --data DATABASE=PATH: "
            f"{', '.join(unpaired)}"
        )
        raise MigrateError(_ERROR_DATA_SCHEMA_WITHOUT_DATA, detail)


def _data_schemas(options: MigrateOptions, database: str) -> DataCopySchemas:
    return options.data_schemas.get(database, DataCopySchemas())


def _load_plan(
    options: MigrateOptions, computed: list[PlanStep]
) -> list[PlanStep]:
    plan_file = options.plan_file
    if options.restart or not plan_file.is_file():
        return computed
    raw = cast("object", json.loads(plan_file.read_text(encoding="utf-8")))
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        detail = f"plan file is not a valid migration plan: {plan_file}"
        raise MigrateError(_ERROR_PLAN_INVALID, detail)
    persisted: list[PlanStep] = []
    for raw_step in cast("list[object]", raw["steps"]):
        if not isinstance(raw_step, dict):
            detail = f"plan file contains a malformed step: {plan_file}"
            raise MigrateError(_ERROR_PLAN_INVALID, detail)
        step = cast("dict[str, object]", raw_step)
        raw_detail = step.get("detail")
        persisted.append(
            PlanStep(
                step_id=str(step.get("id")),
                description=str(step.get("description")),
                status=StepStatus(str(step.get("status"))),
                completed_at=(
                    str(step["completedAt"])
                    if step.get("completedAt") is not None
                    else None
                ),
                detail=(
                    cast("dict[str, object]", raw_detail)
                    if isinstance(raw_detail, dict)
                    else None
                ),
            )
        )
    if [step.step_id for step in persisted] != [
        step.step_id for step in computed
    ]:
        detail = (
            "the persisted plan no longer matches declared state; rerun "
            "with --restart to discard it"
        )
        raise MigrateError(_ERROR_PLAN_STALE, detail)
    # A pending step runs with this run's options (a data step's schemas
    # among them), so it is described by them, not by the saved plan's text.
    for saved, fresh in zip(persisted, computed, strict=True):
        if saved.status is StepStatus.PENDING:
            saved.description = fresh.description
    return persisted


def _save_plan(plan_file: Path, steps: list[PlanStep]) -> None:
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _PLAN_VERSION,
        "steps": [step.as_dict() for step in steps],
    }
    plan_file.write_text(
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )


def _recover_releases(
    steps: list[PlanStep], options: MigrateOptions
) -> dict[str, str]:
    releases = dict(options.releases)
    for step in steps:
        if (
            step.step_id.startswith("build:")
            and step.status is StepStatus.COMPLETED
            and step.detail is not None
            and isinstance(step.detail.get("release"), str)
        ):
            releases[step.step_id.removeprefix("build:")] = str(
                step.detail["release"]
            )
    return releases


def _build_runners(  # noqa: PLR0913 - the run's whole contract.
    config: AgentConfig,
    options: MigrateOptions,
    inventory: PlatformInventory,
    releases: dict[str, str],
    steps: list[PlanStep],
    passwords: tuple[OwnedRolePassword, ...],
) -> dict[str, Runner]:
    context = config.context()
    runners: dict[str, Runner] = {
        "baseline": lambda: _playbook(config, "baseline.yml", {}),
        "services": lambda: _services(config, passwords),
        "ttl-lower": lambda: _lower_ttl(inventory),
        "parallel-run": lambda: _parallel_run(inventory),
        "rollback-window": lambda: _rollback_window(steps),
        "domains-http": lambda: _playbook(
            config, "domains.yml", _domain_vars(config)
        ),
        "dns-verify": lambda: _verify_dns(inventory),
        "domains-tls": lambda: _playbook(
            config,
            "domains.yml",
            {
                **_domain_vars(config),
                "cloudfall_domains_issue_certificates": True,
            },
        ),
        "inspect": lambda: _playbook(
            config,
            "inspect.yml",
            {
                "cloudfall_inspect_output_directory": str(
                    config.observed_directory.resolve()
                )
            },
        ),
        "audit": lambda: _verify_audit(config),
        "verify-routes": lambda: _verify_routes(config),
    }

    def _build_runner(component_id: str, git_ref: str) -> Runner:
        def _run() -> dict[str, object]:
            built = build_release_artifact(
                context,
                ResourceId(component_id),
                git_ref,
                config.artifacts_directory,
            )
            release = built.get("release")
            if not isinstance(release, str):
                detail = "artifact builder returned no release id"
                raise MigrateError(_ERROR_RELEASE_MISSING, detail)
            releases[component_id] = release
            return {"release": release, "gitRef": git_ref}

        return _run

    def _deploy_runner(component_id: str) -> Runner:
        def _run() -> dict[str, object]:
            release = releases.get(component_id)
            if release is None:
                detail = (
                    f"no built or pinned release recorded for {component_id}"
                )
                raise MigrateError(_ERROR_RELEASE_MISSING, detail)
            result = deploy(
                context,
                ResourceId(component_id),
                ReleaseId(release),
                config.artifacts_directory,
                DeployOptions(
                    environment_file=options.environment_files.get(
                        component_id
                    ),
                    receipt_directory=config.releases_directory,
                ),
            )
            return result.as_dict()

        return _run

    for component in inventory.components:
        component_id = component.resource_id.value
        git_ref = options.builds.get(component_id)
        if git_ref is not None:
            runners[f"build:{component_id}"] = _build_runner(
                component_id, git_ref
            )
        runners[f"deploy:{component_id}"] = _deploy_runner(component_id)
    for database, source_url_file in options.data_migrations.items():
        runners[f"data:{database}"] = _data_runner(
            config,
            inventory,
            database,
            source_url_file,
            _data_schemas(options, database),
        )
    return runners


def _data_runner(
    config: AgentConfig,
    inventory: PlatformInventory,
    database: str,
    source_url_file: Path,
    schemas: DataCopySchemas,
) -> Runner:
    def _run() -> dict[str, object]:
        service = next(
            service
            for service in inventory.services
            if service.postgresql is not None
            and any(
                entry.name.value == database
                for entry in service.postgresql.databases
            )
        )
        return migrate_data(
            config.context(),
            service.resource_id,
            database,
            source_url_file,
            config.data_migrations_directory,
            schemas,
        )

    return _run


def _playbook(
    config: AgentConfig,
    playbook_name: str,
    extra_vars: dict[str, object],
) -> dict[str, object]:
    run_engine_playbook(config.context(), playbook_name, extra_vars)
    return {"playbook": playbook_name}


def _services_passwords(config: AgentConfig) -> tuple[OwnedRolePassword, ...]:
    try:
        return services_passwords(config.context(), config.secret_provider())
    except LifecycleError as error:
        raise MigrateError(error.code, error.detail) from error


def _services(
    config: AgentConfig, passwords: tuple[OwnedRolePassword, ...]
) -> dict[str, object]:
    run_services_playbook(config.context(), passwords)
    detail: dict[str, object] = {"playbook": "services.yml"}
    if passwords:
        detail["ownedRoles"] = [password.as_dict() for password in passwords]
    return detail


def _domain_vars(config: AgentConfig) -> dict[str, object]:
    return {
        "cloudfall_domains_receipt_directory": str(
            config.deployments_directory.resolve()
        )
    }


def _lower_ttl(inventory: PlatformInventory) -> dict[str, object]:
    detail, offenders = check_ttl(
        inventory, SystemDnsProbe(), _MAX_CUTOVER_TTL_SECONDS
    )
    if offenders:
        message = (
            "lower these DNS TTLs at your provider, wait one old TTL for "
            "propagation, and rerun the migration: " + "; ".join(offenders)
        )
        raise MigrateError(_ERROR_TTL_HIGH, message)
    return detail


def _parallel_run(inventory: PlatformInventory) -> dict[str, object]:
    detail, failures = parallel_run(inventory, SocketDomainNetworkClient())
    if failures:
        message = (
            "the new origin does not serve every declared domain yet; fix "
            "these before touching DNS: " + "; ".join(failures)
        )
        raise MigrateError(_ERROR_PARALLEL_RUN, message)
    return detail


def _rollback_window(steps: list[PlanStep]) -> dict[str, object]:
    previous = next(
        (
            step.detail
            for step in steps
            if step.step_id == "ttl-lower" and step.detail is not None
        ),
        None,
    )
    switched_at = next(
        (
            step.completed_at
            for step in steps
            if step.step_id == "dns-verify" and step.completed_at is not None
        ),
        None,
    )
    now = datetime.now(tz=UTC)
    switched = switched_at if switched_at is not None else _timestamp(now)
    window_ends = _timestamp(
        now + timedelta(hours=_ROLLBACK_WINDOW_HOURS)
    )
    return rollback_instructions(
        previous if previous is not None else {},
        _ROLLBACK_WINDOW_HOURS,
        switched,
        window_ends,
    )


def _timestamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _verify_dns(inventory: PlatformInventory) -> dict[str, object]:
    servers = {server.resource_id: server for server in inventory.servers}
    client = SocketDomainNetworkClient()
    verified: list[str] = []
    skipped: list[str] = []
    mismatched: list[str] = []
    for domain in inventory.domains:
        if domain.edge.mode.value != "dns-only":
            skipped.append(domain.primary_name.value)
            continue
        expected = _expected_addresses(servers[domain.proxy.server_id])
        observed = _resolved_addresses(client, domain)
        if expected & observed:
            verified.append(domain.primary_name.value)
        else:
            mismatched.append(
                f"{domain.primary_name.value} resolves to "
                f"{sorted(observed) or 'nothing'} instead of "
                f"{sorted(expected)}"
            )
    if mismatched:
        detail = (
            "lower the DNS TTL, point these records at their declared proxy "
            "servers, wait for propagation, and rerun the migration: "
            + "; ".join(mismatched)
        )
        raise MigrateError(_ERROR_DNS_UNVERIFIED, detail)
    return {"verified": verified, "skippedProxied": skipped}


def _expected_addresses(server: ServerInventory) -> set[str]:
    addresses: set[str] = set()
    try:
        ip_address(server.address.value)
    except ValueError:
        addresses.update(_resolve_names(server.address.value))
    else:
        addresses.add(server.address.value)
    if server.network is not None:
        addresses.add(server.network.ipv4.value)
    return addresses


def _resolved_addresses(
    client: SocketDomainNetworkClient, domain: DomainInventory
) -> set[str]:
    try:
        resolved = client.resolve(
            domain.primary_name, TcpPort(_DNS_PROBE_PORT)
        )
    except OSError:
        return set()
    return {address.value for address in resolved}


def _resolve_names(hostname: str) -> set[str]:
    try:
        records = socket.getaddrinfo(
            hostname, _DNS_PROBE_PORT, type=socket.SOCK_STREAM
        )
    except OSError:
        return set()
    return {str(record[4][0]) for record in records}


def _require_observations(config: AgentConfig) -> None:
    """Refuse a verify step whose server snapshots were never recorded.

    Only the missing directory: a malformed snapshot still fails validation,
    and a server without a snapshot is drift the audit reports.
    """
    if not config.observed_directory.is_dir():
        detail = (
            "no server observations at "
            f"{config.observed_directory}; record them with "
            "`cloudfall observe`, then rerun the migration"
        )
        raise MigrateError(_ERROR_OBSERVATIONS_MISSING, detail)


def _verify_audit(config: AgentConfig) -> dict[str, object]:
    _require_observations(config)
    state = validate_config(config.project_directory, config.schema_directory)
    observations = load_observations(
        config.observed_directory, config.schema_directory
    )
    report = audit_inventory(PlatformInventory.from_state(state), observations)
    if report.status is not AuditStatus.COMPLIANT:
        detail = (
            f"the audit reported {report.status.value}; resolve the drift "
            "and rerun the migration"
        )
        raise MigrateError(_ERROR_AUDIT_DRIFT, detail)
    summary = report.as_dict()["summary"]
    return {"summary": cast("dict[str, object]", summary)}


def _verify_routes(config: AgentConfig) -> dict[str, object]:
    # Before any domain is probed: the view needs the server snapshots too.
    _require_observations(config)
    state =validate_config(config.project_directory, config.schema_directory)
    inventory = PlatformInventory.from_state(state)
    inspect_domains(
        inventory,
        config.service_observed_directory,
        SocketDomainNetworkClient(),
        observed_at=EvidenceTimestamp.now(),
    )
    observations = load_observations(
        config.observed_directory, config.schema_directory
    )
    receipts = (
        load_deployment_receipts(
            config.deployments_directory, config.schema_directory
        )
        if config.deployments_directory.is_dir()
        else DeploymentReceiptSet.empty()
    )
    domain_observations = (
        load_domain_observations(
            config.service_observed_directory, config.schema_directory
        )
        if config.service_observed_directory.is_dir()
        else DomainObservationSet.empty()
    )
    view = build_operations_view(
        inventory,
        observations,
        receipts,
        domain_observations,
        generated_at=UtcTimestamp.now(),
    )
    unhealthy = [
        f"{domain.primary_name}: {domain.health.value} "
        f"({domain.public_route.detail})"
        for domain in view.domains
        if domain.health is not OperationsHealth.HEALTHY
    ]
    if unhealthy:
        detail = "routes are not healthy yet: " + "; ".join(unhealthy)
        raise MigrateError(_ERROR_ROUTES_UNHEALTHY, detail)
    return {
        "routes": [domain.primary_name for domain in view.domains],
    }
