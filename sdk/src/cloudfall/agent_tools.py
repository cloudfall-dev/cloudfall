"""Typed agent-facing operations behind the MCP boundary.

Every method returns a JSON-serializable envelope instead of raising, so
agents always receive structured results. Read-only evidence tools are
exposed freely; anything that changes servers demands an explicit
confirmation handshake and always produces receipts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cloudfall.audit import audit_inventory
from cloudfall.authoring import (
    AuthoringError,
    ServerOptions,
    ServerTypeOptions,
    SshKeyOptions,
    add_server,
    add_server_type,
    add_ssh_key,
)
from cloudfall.domain import (
    ConnectionAddress,
    Hostname,
    LinuxUser,
    ReleaseId,
    ResourceId,
    TcpPort,
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
    StepRunner,
    backup_service,
    build_release_artifact,
    deploy,
    health,
    migrate_data,
    restart,
    rollback,
    run_engine_playbook,
    subprocess_step,
    verify_backup,
)
from cloudfall.migrate import MigrateError, MigrateOptions, execute_migration
from cloudfall.observation import load_observations
from cloudfall.operations import UtcTimestamp, build_operations_view
from cloudfall.operator import (
    ApproveOptions,
    OperatorError,
    ProposalStatus,
    ProposalStore,
    TriggerKind,
    alert_resolution_verifier,
    drift_pass,
    drift_resolution_verifier,
    engine_auditor,
    engine_executor,
    gateway_feed,
)
from cloudfall.operator import (
    approve as operator_approve_proposal,
)
from cloudfall.operator import (
    run_once as operator_run_once,
)
from cloudfall.project import project_path
from cloudfall.render_api import HttpRenderApiClient, read_api_key
from cloudfall.render_api import (
    import_render_api as render_api_import,
)
from cloudfall.secrets import (
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
from cloudfall.validation import ConfigValidationError, SchemaCatalog, validate_config

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloudfall.operator import AlertFeed
    from cloudfall.validation import ValidatedConfig

_CODE_INVALID_ARGUMENT = "invalid_argument"


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Filesystem contract for one agent-facing server instance."""

    project_directory: Path
    schema_directory: Path
    engine_directory: Path
    inventory_file: Path
    observed_directory: Path
    service_observed_directory: Path
    deployments_directory: Path
    releases_directory: Path
    artifacts_directory: Path
    data_migrations_directory: Path = Path("tmp/data-migrations")
    backups_directory: Path = Path("tmp/backups")
    secrets_directory: Path = Path("secrets")
    environment_directory: Path = Path("tmp/env")
    environment_receipts_directory: Path = Path("tmp/env-receipts")
    proposals_directory: Path = Path("tmp/operator/proposals")
    gateway_ca_path: Path | None = None
    gateway_certificate_path: Path | None = None
    gateway_key_path: Path | None = None
    run: StepRunner = subprocess_step
    """Starts each engine step: ``subprocess`` here, ``ctx.run`` under treaty."""

    def context(self) -> EngineContext:
        """Return the engine execution context shared by mutating tools."""
        return EngineContext(
            project_directory=self.project_directory,
            schema_directory=self.schema_directory,
            engine_directory=self.engine_directory,
            inventory_file=self.inventory_file,
            run=self.run,
        )


class AgentToolset:
    """Structured operations offered to AI agents."""

    def __init__(self, config: AgentConfig) -> None:
        """Bind the toolset to one filesystem contract."""
        self._config = config

    def validate(self) -> dict[str, object]:
        """Validate declared state and return the structured summary."""
        try:
            return self._state().as_dict()
        except ConfigValidationError as error:
            return error.as_dict()

    def inventory(self) -> dict[str, object]:
        """Return the non-secret platform inventory."""
        try:
            state = self._state()
        except ConfigValidationError as error:
            return error.as_dict()
        return {
            "status": "ok",
            "inventory": PlatformInventory.from_state(state).as_dict(),
        }

    def audit(self) -> dict[str, object]:
        """Compare desired state with collected server observations."""
        try:
            state = self._state()
            observations = load_observations(
                self._config.observed_directory, self._config.schema_directory
            )
        except ConfigValidationError as error:
            return error.as_dict()
        report = audit_inventory(
            PlatformInventory.from_state(state),
            observations,
            load_environment_receipts(
                self._config.environment_receipts_directory,
                self._config.schema_directory,
            ),
        )
        return report.as_dict()

    def services_status(self) -> dict[str, object]:
        """Derive the evidence-based public service lifecycle."""
        try:
            state = self._state()
            observations = load_observations(
                self._config.observed_directory, self._config.schema_directory
            )
        except ConfigValidationError as error:
            return error.as_dict()
        receipts = (
            load_deployment_receipts(
                self._config.deployments_directory,
                self._config.schema_directory,
            )
            if self._config.deployments_directory.is_dir()
            else DeploymentReceiptSet.empty()
        )
        domain_observations = (
            load_domain_observations(
                self._config.service_observed_directory,
                self._config.schema_directory,
            )
            if self._config.service_observed_directory.is_dir()
            else DomainObservationSet.empty()
        )
        view = build_operations_view(
            PlatformInventory.from_state(state),
            observations,
            receipts,
            domain_observations,
            generated_at=UtcTimestamp.now(),
        )
        return {
            "status": "ok",
            "health": view.health.value,
            "services": [domain.as_dict() for domain in view.domains],
        }

    def component_health(self, component: str) -> dict[str, object]:
        """Probe one component's declared health check on its servers."""
        try:
            component_id = ResourceId.from_boundary(component)
        except (TypeError, ValueError) as error:
            return _invalid_argument(error)
        try:
            return health(self._config.context(), component_id).as_dict()
        except LifecycleError as error:
            return error.as_dict()

    def inspect_servers(self) -> dict[str, object]:
        """Collect read-only server evidence into the observation directory."""
        observed = self._config.observed_directory.resolve()
        try:
            run_engine_playbook(
                self._config.context(),
                "inspect.yml",
                {"cloudfall_inspect_output_directory": str(observed)},
            )
        except LifecycleError as error:
            return error.as_dict()
        return {"status": "ok", "observed": str(observed)}

    def inspect_services(self) -> dict[str, object]:
        """Collect DNS, TLS, origin, and public route evidence."""
        try:
            state = self._state()
        except ConfigValidationError as error:
            return error.as_dict()
        paths = inspect_domains(
            PlatformInventory.from_state(state),
            self._config.service_observed_directory,
            SocketDomainNetworkClient(),
            observed_at=EvidenceTimestamp.now(),
        )
        return {
            "status": "ok",
            "observations": [str(path) for path in paths],
        }

    def add_ssh_key(
        self,
        key_file: str,
        owner: str,
        resource_id: str | None = None,
        environment: str = "production",
        description: str | None = None,
    ) -> dict[str, object]:
        """Declare one SSH public key read from a controller-side key file."""
        try:
            options = SshKeyOptions(
                key_path=self._path(key_file).expanduser(),
                owner=ResourceId.from_boundary(owner),
                environment=ResourceId.from_boundary(environment),
                resource_id=(
                    ResourceId.from_boundary(resource_id)
                    if resource_id is not None
                    else None
                ),
                description=description,
            )
        except (TypeError, ValueError) as error:
            return _invalid_argument(error)
        try:
            result = add_ssh_key(
                self._config.project_directory, options, self._config.schema_directory
            )
        except (AuthoringError, ConfigValidationError) as error:
            return error.as_dict()
        return result.as_dict()

    def add_server_type(
        self, resource_id: str, description: str | None = None
    ) -> dict[str, object]:
        """Declare one server type from the bundled Debian 13 baseline."""
        try:
            options = ServerTypeOptions(
                resource_id=ResourceId.from_boundary(resource_id),
                description=description,
            )
        except (TypeError, ValueError) as error:
            return _invalid_argument(error)
        try:
            result = add_server_type(
                self._config.project_directory, options, self._config.schema_directory
            )
        except (AuthoringError, ConfigValidationError) as error:
            return error.as_dict()
        return result.as_dict()

    def add_server(  # noqa: PLR0913 - boundary signature mirrors the CLI.
        self,
        resource_id: str,
        address: str,
        server_type: str = "debian-application",
        environment: str = "production",
        hostname: str | None = None,
        ssh_user: str = "root",
        ssh_port: int = 22,
        description: str | None = None,
    ) -> dict[str, object]:
        """Declare one server, creating its server type when the project lacks it."""
        try:
            options = ServerOptions(
                resource_id=ResourceId.from_boundary(resource_id),
                address=ConnectionAddress.from_boundary(address),
                server_type=ResourceId.from_boundary(server_type),
                environment=ResourceId.from_boundary(environment),
                ssh_user=LinuxUser.from_boundary(ssh_user),
                ssh_port=TcpPort.from_boundary(ssh_port),
                hostname=(
                    Hostname.from_boundary(hostname) if hostname is not None else None
                ),
                description=description,
            )
        except (TypeError, ValueError) as error:
            return _invalid_argument(error)
        try:
            result = add_server(
                self._config.project_directory, options, self._config.schema_directory
            )
        except (AuthoringError, ConfigValidationError) as error:
            return error.as_dict()
        return result.as_dict()

    def import_render(
        self,
        blueprint: str,
        application: str,
        server: str,
        output_directory: str,
        environment_directory: str,
    ) -> dict[str, object]:
        """Map a Render blueprint onto Cloudfall config fragments."""
        try:
            targets = ImportTargets(
                application_id=ResourceId.from_boundary(application),
                server_id=ResourceId.from_boundary(server),
                project_directory=project_path(output_directory),
                environment_directory=project_path(environment_directory),
            )
        except (TypeError, ValueError) as error:
            return _invalid_argument(error)
        try:
            result = import_render_blueprint(
                self._path(blueprint), targets, self._config.schema_directory
            )
        except (RenderImportError, ConfigValidationError) as error:
            return error.as_dict()
        return result.as_dict()

    def import_render_api(
        self,
        api_key_file: str,
        application: str,
        server: str,
        output_directory: str,
        environment_directory: str,
    ) -> dict[str, object]:
        """Map a live Render workspace onto Cloudfall config fragments."""
        try:
            targets = ImportTargets(
                application_id=ResourceId.from_boundary(application),
                server_id=ResourceId.from_boundary(server),
                project_directory=project_path(output_directory),
                environment_directory=project_path(environment_directory),
            )
        except (TypeError, ValueError) as error:
            return _invalid_argument(error)
        try:
            client = HttpRenderApiClient(
                api_key=read_api_key(self._path(api_key_file))
            )
            result = render_api_import(
                client, targets, self._config.schema_directory
            )
        except (RenderImportError, ConfigValidationError) as error:
            return error.as_dict()
        return result.as_dict()

    def build_artifact(self, component: str, git_ref: str) -> dict[str, object]:
        """Build one hashed release artifact from the component repository."""
        try:
            component_id = ResourceId.from_boundary(component)
        except (TypeError, ValueError) as error:
            return _invalid_argument(error)
        try:
            return build_release_artifact(
                self._config.context(),
                component_id,
                git_ref,
                self._config.artifacts_directory,
            )
        except LifecycleError as error:
            return error.as_dict()

    def deploy_component(
        self,
        component: str,
        release: str,
        environment_file: str | None = None,
        *,
        confirm: bool = False,
    ) -> dict[str, object]:
        """Deploy one built release behind the engine's health gate."""
        gate = _confirmation_gate(
            confirm,
            "deploy",
            f"deploy release {release} of component {component} to its "
            "declared servers with automatic rollback on a failed health "
            "check",
        )
        if gate is not None:
            return gate
        try:
            component_id = ResourceId.from_boundary(component)
            release_id = ReleaseId.from_boundary(release)
        except (TypeError, ValueError) as error:
            return _invalid_argument(error)
        try:
            result = deploy(
                self._config.context(),
                component_id,
                release_id,
                self._config.artifacts_directory,
                DeployOptions(
                    environment_file=(
                        self._path(environment_file)
                        if environment_file is not None
                        else None
                    ),
                    receipt_directory=self._config.releases_directory,
                ),
            )
        except LifecycleError as error:
            return error.as_dict()
        return result.as_dict()

    def rollback_component(
        self, component: str, release: str, *, confirm: bool = False
    ) -> dict[str, object]:
        """Activate an already-retained release behind the health gate."""
        gate = _confirmation_gate(
            confirm,
            "rollback",
            f"switch component {component} back to retained release "
            f"{release} and restart it",
        )
        if gate is not None:
            return gate
        try:
            component_id = ResourceId.from_boundary(component)
            release_id = ReleaseId.from_boundary(release)
        except (TypeError, ValueError) as error:
            return _invalid_argument(error)
        try:
            result = rollback(self._config.context(), component_id, release_id)
        except LifecycleError as error:
            return error.as_dict()
        return result.as_dict()

    def restart_component(
        self, component: str, *, confirm: bool = False
    ) -> dict[str, object]:
        """Restart one component and require its declared health check."""
        gate = _confirmation_gate(
            confirm,
            "restart",
            f"restart component {component} on its declared servers",
        )
        if gate is not None:
            return gate
        try:
            component_id = ResourceId.from_boundary(component)
        except (TypeError, ValueError) as error:
            return _invalid_argument(error)
        try:
            result = restart(self._config.context(), component_id)
        except LifecycleError as error:
            return error.as_dict()
        return result.as_dict()

    def migrate_database(
        self,
        service: str,
        database: str,
        source_url_file: str,
        *,
        confirm: bool = False,
    ) -> dict[str, object]:
        """Dump an external database and restore it into a declared service."""
        gate = _confirmation_gate(
            confirm,
            "data-migration",
            (
                f"dump the database behind {source_url_file} and restore it "
                f"into declared database {database} on service {service} "
                "with per-table row-count verification"
            ),
        )
        if gate is not None:
            return gate
        try:
            service_id = ResourceId.from_boundary(service)
        except (TypeError, ValueError) as error:
            return _invalid_argument(error)
        try:
            return migrate_data(
                self._config.context(),
                service_id,
                database,
                Path(source_url_file),
                self._config.data_migrations_directory,
            )
        except LifecycleError as error:
            return error.as_dict()

    def converge_baseline(self, *, confirm: bool = False) -> dict[str, object]:
        """Converge every declared server to the managed baseline."""
        return self._converge(
            confirm,
            "converge-baseline",
            "run baseline.yml: bootstrap, UTC, SSH hardening, unattended "
            "upgrades, and the declared firewall on every server",
            "baseline.yml",
            {},
        )

    def converge_services(self, *, confirm: bool = False) -> dict[str, object]:
        """Converge every declared infrastructure service."""
        return self._converge(
            confirm,
            "converge-services",
            "run services.yml: install and configure every declared "
            "infrastructure service",
            "services.yml",
            {},
        )

    def converge_domains(
        self, *, issue_certificates: bool = False, confirm: bool = False
    ) -> dict[str, object]:
        """Converge every declared public domain route."""
        return self._converge(
            confirm,
            "converge-domains",
            "run domains.yml: render every declared domain route"
            + (
                " and issue missing certificates"
                if issue_certificates
                else " without certificate issuance"
            ),
            "domains.yml",
            {
                "cloudfall_domains_issue_certificates": issue_certificates,
                "cloudfall_domains_receipt_directory": str(
                    self._config.deployments_directory.resolve()
                ),
            },
        )

    def migrate(  # noqa: PLR0913 - boundary signature mirrors the CLI.
        self,
        builds: dict[str, str] | None = None,
        releases: dict[str, str] | None = None,
        environment_files: dict[str, str] | None = None,
        data_migrations: dict[str, str] | None = None,
        plan_file: str = "tmp/migrate/plan.json",
        *,
        restart_plan: bool = False,
        confirm: bool = False,
    ) -> dict[str, object]:
        """Preview or execute the resumable end-to-end migration plan."""
        try:
            confined_plan_file = project_path(plan_file)
        except ValueError as error:
            return _invalid_argument(error)
        options = MigrateOptions(
            plan_file=confined_plan_file,
            builds=builds if builds is not None else {},
            releases=releases if releases is not None else {},
            environment_files={
                component: self._path(value)
                for component, value in (
                    environment_files if environment_files is not None else {}
                ).items()
            },
            data_migrations={
                database: self._path(value)
                for database, value in (
                    data_migrations if data_migrations is not None else {}
                ).items()
            },
            execute=confirm,
            restart=restart_plan,
        )
        try:
            result = execute_migration(self._config, options)
        except (MigrateError, ConfigValidationError) as error:
            return error.as_dict()
        if not confirm:
            result["instruction"] = (
                "review the plan and call this tool again with confirm=true "
                "to execute it; interrupted runs resume automatically"
            )
        return result

    def render_secrets(self, component: str) -> dict[str, object]:
        """Render one component's secret references into its env file."""
        try:
            component_id = ResourceId.from_boundary(component)
        except (TypeError, ValueError) as error:
            return _invalid_argument(error)
        try:
            return render_environment(
                self._config.context(),
                component_id,
                SopsSecretProvider(
                    secrets_directory=self._config.secrets_directory
                ),
                self._config.environment_directory
                / f"{component_id.value}.env",
                receipt_directory=(
                    self._config.environment_receipts_directory
                ),
            )
        except (SecretsError, ConfigValidationError) as error:
            return error.as_dict()

    def backup_service(
        self, service: str, *, confirm: bool = False
    ) -> dict[str, object]:
        """Run the declared backup for one service, receipted."""
        return self._backup_operation(
            service,
            confirm=confirm,
            action="backup-service",
            detail_prefix="run the declared backup for",
            operation=backup_service,
        )

    def verify_backup(
        self, service: str, *, confirm: bool = False
    ) -> dict[str, object]:
        """Prove the newest backup of one service restores, receipted."""
        return self._backup_operation(
            service,
            confirm=confirm,
            action="verify-backup",
            detail_prefix="prove the newest backup restores for",
            operation=verify_backup,
        )

    def _backup_operation(
        self,
        service: str,
        *,
        confirm: bool,
        action: str,
        detail_prefix: str,
        operation: Callable[
            [EngineContext, ResourceId, Path], dict[str, object]
        ],
    ) -> dict[str, object]:
        try:
            service_id = ResourceId.from_boundary(service)
        except (TypeError, ValueError) as error:
            return _invalid_argument(error)
        gate = _confirmation_gate(
            confirm,
            action,
            f"{detail_prefix} {service_id.value} on its declared server",
        )
        if gate is not None:
            return gate
        try:
            return operation(
                self._config.context(),
                service_id,
                self._config.backups_directory,
            )
        except (LifecycleError, ConfigValidationError) as error:
            return error.as_dict()

    def operator_proposals(self) -> dict[str, object]:
        """List every operator proposal receipt with its status."""
        try:
            proposals = [
                proposal.as_document()
                for proposal in self._proposal_store().list()
            ]
        except OperatorError as error:
            return error.as_dict()
        return {"status": "ok", "proposals": proposals}

    def operator_watch(self, *, drift: bool = False) -> dict[str, object]:
        """Run one watch pass: fetch alerts, optionally audit for drift."""
        try:
            state = self._state()
        except ConfigValidationError as error:
            return error.as_dict()
        inventory = PlatformInventory.from_state(state)
        try:
            store = self._proposal_store()
            feed = self._alert_feed(inventory)
            report = operator_run_once(feed, inventory, store)
            passes: dict[str, object] = {"alerts": report.as_dict()}
            if drift:
                auditor = engine_auditor(
                    self._config.context(),
                    inventory,
                    self._config.observed_directory,
                )
                passes["drift"] = drift_pass(auditor, store).as_dict()
        except (OperatorError, LifecycleError) as error:
            return error.as_dict()
        return {"status": "ok", **passes}

    def operator_approve(
        self, proposal: str, *, confirm: bool = False
    ) -> dict[str, object]:
        """Execute one proposal after confirmation and verify its trigger."""
        try:
            state = self._state()
        except ConfigValidationError as error:
            return error.as_dict()
        inventory = PlatformInventory.from_state(state)
        try:
            store = self._proposal_store()
            proposal_id = ResourceId.from_boundary(proposal)
            pending = store.load(proposal_id)
        except (OperatorError, ValueError) as error:
            return _invalid_argument(error)
        gate = _confirmation_gate(
            confirm,
            "operator-approve",
            f"execute {' '.join(pending.command)} because "
            f"{pending.diagnosis_summary}",
        )
        if gate is not None:
            return gate
        try:
            if pending.trigger_kind is TriggerKind.ALERT:
                verifier = alert_resolution_verifier(
                    self._alert_feed(inventory)
                )
            else:
                verifier = drift_resolution_verifier(
                    engine_auditor(
                        self._config.context(),
                        inventory,
                        self._config.observed_directory,
                    )
                )
            result = operator_approve_proposal(
                store,
                proposal_id,
                engine_executor(self._config.context()),
                verifier,
                ApproveOptions(),
            )
        except (OperatorError, LifecycleError) as error:
            return error.as_dict()
        return {
            "status": (
                "ok"
                if result.status is ProposalStatus.VERIFIED
                else "failed"
            ),
            "proposal": result.as_document(),
        }

    def _proposal_store(self) -> ProposalStore:
        return ProposalStore(
            directory=self._config.proposals_directory,
            catalog=SchemaCatalog(self._config.schema_directory),
        )

    def _alert_feed(self, inventory: PlatformInventory) -> AlertFeed:
        config = self._config
        if (
            config.gateway_ca_path is None
            or config.gateway_certificate_path is None
            or config.gateway_key_path is None
        ):
            code = "operator_gateway_material_missing"
            message = (
                "the server was started without gateway TLS material; "
                "pass --gateway-ca, --gateway-cert, and --gateway-key"
            )
            raise OperatorError(code, message)
        return gateway_feed(
            inventory,
            ca_path=config.gateway_ca_path,
            certificate_path=config.gateway_certificate_path,
            key_path=config.gateway_key_path,
        )

    def _converge(
        self,
        confirm: bool,  # noqa: FBT001 - explicit gate flag at the boundary.
        action: str,
        detail: str,
        playbook: str,
        extra_vars: dict[str, object],
    ) -> dict[str, object]:
        gate = _confirmation_gate(confirm, action, detail)
        if gate is not None:
            return gate
        try:
            run_engine_playbook(self._config.context(), playbook, extra_vars)
        except LifecycleError as error:
            return error.as_dict()
        return {"status": "ok", "action": action}

    def _state(self) -> ValidatedConfig:
        return validate_config(
            self._config.project_directory, self._config.schema_directory
        )

    def _path(self, value: str) -> Path:
        return Path(value)


def _confirmation_gate(
    confirm: bool,  # noqa: FBT001 - explicit gate flag at the boundary.
    action: str,
    detail: str,
) -> dict[str, object] | None:
    if confirm:
        return None
    return {
        "status": "confirmation-required",
        "action": action,
        "wouldRun": detail,
        "instruction": (
            "review the description and call this tool again with "
            "confirm=true to execute"
        ),
    }


def _invalid_argument(error: Exception) -> dict[str, object]:
    return {
        "status": "error",
        "error": {"code": _CODE_INVALID_ARGUMENT, "message": str(error)},
    }
