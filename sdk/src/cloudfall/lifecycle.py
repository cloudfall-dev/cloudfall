"""Agent-facing component lifecycle operations.

The SDK orchestrates deployments through the engine's stable process
boundaries: the ``cloudfall_engine`` command-line contract for inventory
rendering and the engine's playbook contract for execution. It never
imports Ansible or engine internals.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Protocol, cast

from jsonschema.exceptions import ValidationError

from cloudfall.domain import AbsolutePath, DnsMode, Hostname, IpAddress, TlsMode
from cloudfall.inventory import PlatformInventory
from cloudfall.secrets import (
    SecretsError,
    owned_databases,
    owned_role_passwords,
    staged_password_files,
)
from cloudfall.validation import SchemaCatalog, validate_config

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from cloudfall.domain import (
        CommandArgv,
        ReleaseCommandTimeout,
        ReleaseId,
        ResourceId,
    )
    from cloudfall.inventory import (
        ComponentInventory,
        DomainInventory,
        ServerInventory,
        ServiceInventory,
    )
    from cloudfall.secrets import OwnedRolePassword, SopsSecretProvider

_ARTIFACT_SCHEMA = "artifact.schema.json"
_OUTPUT_TAIL_CHARACTERS = 2000
_DNS_PROBE_PORT = 443
_ERROR_COMPONENT_MISSING = "lifecycle_component_missing"
_ERROR_DOMAIN_MISSING = "lifecycle_domain_missing"
_ERROR_DOMAIN_TLS_DISABLED = "domains_tls_disabled"
_ERROR_DOMAIN_DNS_UNVERIFIED = "domains_dns_unverified"
_ERROR_ARTIFACT_MISSING = "lifecycle_artifact_missing"
_ERROR_ARTIFACT_METADATA = "lifecycle_artifact_metadata_invalid"
_ERROR_ARTIFACT_IDENTITY = "lifecycle_artifact_identity_mismatch"
_ERROR_ARTIFACT_DIGEST = "lifecycle_artifact_digest_mismatch"
_ERROR_ANSIBLE_MISSING = "lifecycle_ansible_missing"
_ERROR_ENGINE_MISSING = "lifecycle_engine_missing"
ERROR_EXECUTION_FAILED = "lifecycle_execution_failed"
_ERROR_RECEIPT_MISSING = "lifecycle_receipt_missing"
_ERROR_SERVICE_MISSING = "lifecycle_service_missing"
_ERROR_DATABASE_MISSING = "lifecycle_database_missing"
_ERROR_SOURCE_URL_MISSING = "lifecycle_source_url_missing"


class LifecycleError(RuntimeError):
    """Fail-fast lifecycle error with a stable machine-readable code."""

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
class ExecutionStep:
    """One subprocess invocation with explicit environment additions."""

    description: str
    argv: tuple[str, ...]
    environment: Mapping[str, str]


class StepRun(Protocol):
    """How one finished step reads: ``subprocess`` or treaty's ``ctx.run``."""

    @property
    def returncode(self) -> int:
        """The exit code."""
        ...

    @property
    def stdout(self) -> str:
        """What the step printed on stdout."""
        ...

    @property
    def stderr(self) -> str:
        """What the step printed on stderr."""
        ...


type StepRunner = Callable[[ExecutionStep], StepRun]
"""Runs one step to completion without raising on a non-zero exit."""

STEP_TIMEOUT_SECONDS = 3600
"""How long one step (an inventory render or a playbook run) may take."""


def subprocess_step(step: ExecutionStep) -> StepRun:
    """Run one step as a child process, the runner outside treaty commands."""
    try:
        return subprocess.run(  # noqa: S603 - argv from typed values.
            list(step.argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=STEP_TIMEOUT_SECONDS,
            env={**os.environ, **step.environment},
        )
    except subprocess.TimeoutExpired as error:
        detail = f"{step.description} exceeded {STEP_TIMEOUT_SECONDS} seconds"
        raise LifecycleError(ERROR_EXECUTION_FAILED, detail) from error


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Ordered subprocess steps for one lifecycle action."""

    action: str
    component_id: ResourceId
    release: ReleaseId | None
    steps: tuple[ExecutionStep, ...]


@dataclass(frozen=True, slots=True)
class DeployOptions:
    """Optional deployment inputs."""

    environment_file: Path | None = None
    receipt_directory: Path | None = None


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    """A release artifact whose metadata and digest have been verified."""

    archive_path: Path
    metadata_path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    """Structured outcome of one lifecycle action."""

    action: str
    component_id: ResourceId
    release: ReleaseId | None
    servers: tuple[ResourceId, ...]
    healthy: bool
    detail: str | None
    receipt: Path | None

    def as_dict(self) -> dict[str, object]:
        """Serialize the result for agent consumers."""
        result: dict[str, object] = {
            "status": "ok" if self.healthy else "unhealthy",
            "action": self.action,
            "component": self.component_id.value,
            "servers": [server.value for server in self.servers],
            "healthy": self.healthy,
        }
        if self.release is not None:
            result["release"] = self.release.value
        if self.detail is not None:
            result["detail"] = self.detail
        if self.receipt is not None:
            result["receipt"] = str(self.receipt)
        return result


@dataclass(frozen=True, slots=True)
class ReleaseCommandPlan:
    """The release command a deploy would run, resolved as the engine runs it.

    The working directory is the absolute path on the release server: the
    new release directory, or the declared ``workingDirectory`` under it.
    """

    server: ResourceId
    command: CommandArgv
    working_directory: AbsolutePath
    timeout_seconds: ReleaseCommandTimeout

    @classmethod
    def for_release(
        cls, component: ComponentInventory, release: ReleaseId
    ) -> ReleaseCommandPlan | None:
        """Resolve a component's release command, ``None`` when it has none."""
        declared = component.release
        if declared is None:
            return None
        directory = PurePosixPath(
            component.install_root.value, "releases", release.value
        )
        if declared.working_directory is not None:
            directory /= declared.working_directory.value
        return cls(
            server=component.release_server_id,
            command=declared.command,
            working_directory=AbsolutePath(str(directory)),
            timeout_seconds=declared.timeout_seconds,
        )

    @property
    def described(self) -> str:
        """The command as one line, for a plan's ``wouldRun``."""
        return " ".join(self.command.value)

    def as_dict(self) -> dict[str, object]:
        """Serialize the plan's ``releaseCommand``."""
        return {
            "command": list(self.command.value),
            "workingDirectory": self.working_directory.value,
            "timeoutSeconds": self.timeout_seconds.value,
            "server": self.server.value,
        }


class PreviewSubject(StrEnum):
    """Kind of declared resource a previewed action targets."""

    COMPONENT = "component"
    SERVICE = "service"


@dataclass(frozen=True, slots=True)
class LifecyclePreview:
    """A server-changing action validated against the config, not executed.

    Previews run every controller-side check the action itself runs, so an
    action that previews cleanly fails only on what the servers report.
    """

    action: str
    subject: PreviewSubject
    subject_id: ResourceId
    release: ReleaseId | None
    servers: tuple[ResourceId, ...]
    effect: str
    release_command: ReleaseCommandPlan | None = None
    """The release command a deploy runs; set only on a deploy's plan."""
    release_command_skipped: bool = False
    """A declared release command this action does not run (a rollback)."""

    def as_dict(self) -> dict[str, object]:
        """Serialize the preview for agent consumers."""
        result: dict[str, object] = {
            "status": "plan",
            "action": self.action,
            self.subject.value: self.subject_id.value,
            "servers": [server.value for server in self.servers],
            "wouldRun": self.effect,
        }
        if self.release is not None:
            result["release"] = self.release.value
        if self.release_command is not None:
            result["releaseCommand"] = self.release_command.as_dict()
        if self.release_command_skipped:
            result["releaseCommandSkipped"] = True
        return result


@dataclass(frozen=True, slots=True)
class EngineContext:
    """Filesystem contract shared by every lifecycle action."""

    project_directory: Path
    schema_directory: Path
    engine_directory: Path
    inventory_file: Path
    run: StepRunner = subprocess_step
    """Starts each step: ``subprocess`` by default, ``ctx.run`` under treaty."""

    def playbook(self, name: str) -> Path:
        """Return the path of one engine playbook, requiring it to exist."""
        path = self.engine_directory / "ansible" / "playbooks" / name
        if not path.is_file():
            detail = f"engine playbook does not exist: {path}"
            raise LifecycleError(_ERROR_ENGINE_MISSING, detail)
        return path

    def ansible_environment(self) -> dict[str, str]:
        """Return environment additions for engine playbook execution."""
        return {
            "ANSIBLE_CONFIG": str(
                self.engine_directory / "ansible" / "ansible.cfg"
            )
        }


def verify_artifact(
    artifact_directory: Path,
    component_id: ResourceId,
    release: ReleaseId,
    schema_directory: Path,
) -> VerifiedArtifact:
    """Verify a built artifact's metadata, identity, and digest."""
    component_directory = artifact_directory / component_id.value
    archive_path = component_directory / f"{release.value}.tar.gz"
    metadata_path = component_directory / f"{release.value}.json"
    if not archive_path.is_file() or not metadata_path.is_file():
        detail = (
            "artifact archive or metadata does not exist for "
            f"{component_id}/{release.value} under {artifact_directory}"
        )
        raise LifecycleError(_ERROR_ARTIFACT_MISSING, detail)

    raw = cast(
        "object", json.loads(metadata_path.read_text(encoding="utf-8"))
    )
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) for key in raw
    ):
        detail = f"artifact metadata is not an object: {metadata_path}"
        raise LifecycleError(_ERROR_ARTIFACT_METADATA, detail)
    metadata = cast("Mapping[str, object]", raw)
    try:
        SchemaCatalog(schema_directory).validate_named(
            _ARTIFACT_SCHEMA, metadata
        )
    except ValidationError as error:
        detail = f"artifact metadata is invalid: {error.message}"
        raise LifecycleError(_ERROR_ARTIFACT_METADATA, detail) from error

    spec = cast("Mapping[str, object]", metadata["spec"])
    if (
        spec.get("component") != component_id.value
        or spec.get("release") != release.value
    ):
        detail = (
            "artifact metadata identifies "
            f"{spec.get('component')}/{spec.get('release')}, not "
            f"{component_id}/{release.value}"
        )
        raise LifecycleError(_ERROR_ARTIFACT_IDENTITY, detail)

    declared_digest = spec.get("archiveSha256")
    actual_digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if actual_digest != declared_digest:
        detail = (
            f"artifact digest {actual_digest} does not match declared "
            f"metadata digest {declared_digest}"
        )
        raise LifecycleError(_ERROR_ARTIFACT_DIGEST, detail)
    return VerifiedArtifact(
        archive_path=archive_path,
        metadata_path=metadata_path,
        sha256=actual_digest,
    )


def plan_deploy(
    context: EngineContext,
    component_id: ResourceId,
    release: ReleaseId,
    artifact: VerifiedArtifact,
    options: DeployOptions,
) -> ExecutionPlan:
    """Compose the subprocess steps for one deployment."""
    extra_vars: dict[str, object] = {
        "cloudfall_deploy_component_id": component_id.value,
        "cloudfall_deploy_release": release.value,
        # The engine resolves relative paths against its own tree, so every
        # controller-side path must cross this boundary absolute.
        "cloudfall_deploy_artifact_archive": str(
            artifact.archive_path.resolve()
        ),
        "cloudfall_deploy_artifact_sha256": artifact.sha256,
    }
    if options.environment_file is not None:
        extra_vars["cloudfall_deploy_environment_file"] = str(
            options.environment_file.resolve()
        )
    if options.receipt_directory is not None:
        extra_vars["cloudfall_deploy_receipt_directory"] = str(
            options.receipt_directory.resolve()
        )
    return ExecutionPlan(
        action="deploy",
        component_id=component_id,
        release=release,
        steps=(
            _render_inventory_step(context),
            _playbook_step(context, "deploy.yml", extra_vars),
        ),
    )


def plan_rollback(
    context: EngineContext,
    component_id: ResourceId,
    release: ReleaseId,
) -> ExecutionPlan:
    """Compose the subprocess steps for one explicit rollback."""
    extra_vars: dict[str, object] = {
        "cloudfall_deploy_component_id": component_id.value,
        "cloudfall_deploy_release": release.value,
    }
    return ExecutionPlan(
        action="rollback",
        component_id=component_id,
        release=release,
        steps=(
            _render_inventory_step(context),
            _playbook_step(context, "rollback.yml", extra_vars),
        ),
    )


def plan_restart(
    context: EngineContext, component_id: ResourceId
) -> ExecutionPlan:
    """Compose the subprocess steps for one health-gated restart."""
    extra_vars: dict[str, object] = {
        "cloudfall_deploy_component_id": component_id.value,
    }
    return ExecutionPlan(
        action="restart",
        component_id=component_id,
        release=None,
        steps=(
            _render_inventory_step(context),
            _playbook_step(context, "restart.yml", extra_vars),
        ),
    )


def plan_health(
    context: EngineContext, component_id: ResourceId
) -> ExecutionPlan:
    """Compose the subprocess steps for one read-only health check."""
    extra_vars: dict[str, object] = {
        "cloudfall_deploy_component_id": component_id.value,
    }
    return ExecutionPlan(
        action="health",
        component_id=component_id,
        release=None,
        steps=(
            _render_inventory_step(context),
            _playbook_step(context, "health.yml", extra_vars),
        ),
    )


def execute_plan(plan: ExecutionPlan, run: StepRunner) -> None:
    """Run every step of a plan, failing fast on the first error."""
    _execute_steps(plan.steps, run)


def run_engine_playbook(
    context: EngineContext,
    playbook_name: str,
    extra_vars: Mapping[str, object],
    limit: ResourceId | None = None,
) -> None:
    """Render inventory and run one engine playbook contract.

    ``limit`` narrows the run to one declared server, so no other host is
    contacted.
    """
    _execute_steps(
        (
            _render_inventory_step(context),
            _playbook_step(context, playbook_name, extra_vars, limit),
        ),
        context.run,
    )


@dataclass(frozen=True, slots=True)
class DomainsRun:
    """What one ``domains.yml`` run covers."""

    issue_certificates: bool
    receipt_directory: Path
    domain_id: ResourceId | None = None
    """One Domain to converge on its proxy server alone; ``None`` for all."""


def converge_domains(context: EngineContext, run: DomainsRun) -> None:
    """Run ``domains.yml`` on every server, or for one Domain on its proxy.

    One Domain is looked up in the validated config first, so an unknown id
    is refused before the inventory render and before any SSH connection.
    """
    if run.domain_id is None:
        run_engine_playbook(context, "domains.yml", _domains_vars(run))
        return
    _converge_one_domain(context, _domain(context, run.domain_id), run)


def _domains_vars(run: DomainsRun) -> dict[str, object]:
    extra_vars: dict[str, object] = {
        "cloudfall_domains_issue_certificates": run.issue_certificates,
        "cloudfall_domains_receipt_directory": str(run.receipt_directory),
    }
    if run.domain_id is not None:
        extra_vars["cloudfall_domains_domain_id"] = run.domain_id.value
    return extra_vars


def _converge_one_domain(
    context: EngineContext, domain: DomainInventory, run: DomainsRun
) -> None:
    run_engine_playbook(
        context, "domains.yml", _domains_vars(run), limit=domain.proxy.server_id
    )


class NameResolver(Protocol):
    """Resolves a public name on the controller, as a browser would."""

    def resolve(self, name: Hostname) -> frozenset[IpAddress]:
        """Return every address ``name`` resolves to; empty when none."""
        ...


class SystemNameResolver:
    """The controller's own resolver, through ``getaddrinfo``."""

    def resolve(self, name: Hostname) -> frozenset[IpAddress]:
        """Return the name's addresses; a name that does not resolve has none."""
        try:
            records = socket.getaddrinfo(
                name.value, _DNS_PROBE_PORT, type=socket.SOCK_STREAM
            )
        except socket.gaierror:
            return frozenset()
        # A scoped link-local address (fe80::1%en0) is no public answer.
        return frozenset(
            IpAddress.from_boundary(str(record[4][0]))
            for record in records
            if "%" not in str(record[4][0])
        )


def proxy_addresses(
    server: ServerInventory, resolver: NameResolver
) -> frozenset[IpAddress]:
    """Return the addresses a name pointed at ``server`` may resolve to.

    The server's address (or what its hostname resolves to) and its declared
    ``network.ipv4``, as the migration's ``dns-verify`` step computes them.
    """
    addresses: set[IpAddress] = set()
    try:
        ip_address(server.address.value)
    except ValueError:
        addresses.update(resolver.resolve(Hostname(server.address.value)))
    else:
        addresses.add(IpAddress(server.address.value))
    if server.network is not None:
        addresses.add(IpAddress(server.network.ipv4.value))
    return frozenset(addresses)


class DnsCheck(StrEnum):
    """What the controller-side DNS check of ``domains apply`` found."""

    VERIFIED = "verified"
    SKIPPED_PROXIED = "skipped-proxied"


@dataclass(frozen=True, slots=True)
class DomainApply:
    """One ``domains apply`` request."""

    domain_id: ResourceId
    issue_certificate: bool
    receipt_directory: Path


@dataclass(frozen=True, slots=True)
class DomainApplied:
    """What ``domains apply`` checked, and would run or ran, for one Domain."""

    domain_id: ResourceId
    server: ResourceId
    issue_certificate: bool
    dns_check: DnsCheck | None
    receipt: Path | None = None
    """The receipt the run wrote; ``None`` for a plan."""

    @property
    def would_run(self) -> str:
        """Say what the run changes, and where."""
        certificate = (
            ", issue its certificate by webroot when none exists yet, render it "
            "again with TLS, and enable certbot.timer"
            if self.issue_certificate
            else ""
        )
        return (
            f"render Domain {self.domain_id} as a managed Nginx virtual host on "
            f"{self.server} alone through domains.yml{certificate}; no baseline, "
            "firewall or service task runs"
        )

    def as_dict(self) -> dict[str, object]:
        """Serialize the plan, or with a receipt the run, for agent consumers."""
        result: dict[str, object] = {
            "status": "plan" if self.receipt is None else "ok",
            "action": "domains-apply",
            "domain": self.domain_id.value,
            "servers": [self.server.value],
        }
        if self.receipt is None:
            result["wouldRun"] = self.would_run
        else:
            result["receipt"] = str(self.receipt)
        if self.dns_check is not None:
            result["dnsCheck"] = self.dns_check.value
        return result


def preview_domain_apply(
    context: EngineContext, request: DomainApply, resolver: NameResolver
) -> DomainApplied:
    """Run every controller-side check of ``domains apply``; change nothing.

    The Domain must be declared, ``--issue-certificate`` needs ``tls.mode:
    required``, and a ``dns-only`` Domain's names must resolve to its proxy
    server before a certificate is asked for. Nothing here renders the
    inventory or contacts a server.
    """
    state = validate_config(context.project_directory, context.schema_directory)
    inventory = PlatformInventory.from_state(state)
    domain = _find_domain(inventory, request.domain_id)
    dns_check = None
    if request.issue_certificate:
        if domain.tls_mode is TlsMode.DISABLED:
            detail = (
                f"domain {domain.resource_id} declares tls.mode: disabled, so "
                "there is no certificate to issue; drop --issue-certificate"
            )
            raise LifecycleError(_ERROR_DOMAIN_TLS_DISABLED, detail)
        servers = {server.resource_id: server for server in inventory.servers}
        dns_check = _check_domain_dns(
            domain, servers[domain.proxy.server_id], resolver
        )
    return DomainApplied(
        domain_id=domain.resource_id,
        server=domain.proxy.server_id,
        issue_certificate=request.issue_certificate,
        dns_check=dns_check,
    )


def apply_domain(
    context: EngineContext, request: DomainApply, resolver: NameResolver
) -> DomainApplied:
    """Converge one Domain on its proxy server alone, and return its receipt.

    The checks of :func:`preview_domain_apply` run first, so a refused
    request renders no inventory and runs no playbook.
    """
    checked = preview_domain_apply(context, request, resolver)
    run_engine_playbook(
        context,
        "domains.yml",
        _domains_vars(
            DomainsRun(
                issue_certificates=request.issue_certificate,
                receipt_directory=request.receipt_directory,
                domain_id=request.domain_id,
            )
        ),
        limit=checked.server,
    )
    receipt = request.receipt_directory / f"{request.domain_id.value}.json"
    if not receipt.is_file():
        detail = f"domains.yml reported success but wrote no receipt: {receipt}"
        raise LifecycleError(_ERROR_RECEIPT_MISSING, detail)
    return DomainApplied(
        domain_id=checked.domain_id,
        server=checked.server,
        issue_certificate=checked.issue_certificate,
        dns_check=checked.dns_check,
        receipt=receipt,
    )


def _check_domain_dns(
    domain: DomainInventory, server: ServerInventory, resolver: NameResolver
) -> DnsCheck:
    """Require every name of a ``dns-only`` Domain to reach its proxy server."""
    if domain.edge.mode is DnsMode.PROXIED:
        return DnsCheck.SKIPPED_PROXIED
    expected = proxy_addresses(server, resolver)
    failing: list[str] = []
    for name in (domain.primary_name, *domain.aliases):
        resolved = resolver.resolve(name)
        if not resolved & expected:
            answer = _address_list(resolved) if resolved else "nothing"
            failing.append(f"{name.value} resolves to {answer}")
    if failing:
        detail = (
            f"point these names of domain {domain.resource_id} at proxy server "
            f"{server.resource_id} ({_address_list(expected) or 'no known address'})"
            ", wait for "
            "propagation, and rerun: " + "; ".join(failing)
        )
        raise LifecycleError(_ERROR_DOMAIN_DNS_UNVERIFIED, detail)
    return DnsCheck.VERIFIED


def _address_list(addresses: frozenset[IpAddress]) -> str:
    return ", ".join(sorted(address.value for address in addresses))


ERROR_SERVICES_SECRET = "lifecycle_services_secret_unavailable"  # noqa: S105 - a code
"""An owner role's password could not be read; no playbook ran."""


def services_passwords(
    context: EngineContext, provider: SopsSecretProvider
) -> tuple[OwnedRolePassword, ...]:
    """Decrypt every owner role's password before ``services.yml`` may run.

    A missing file or key fails here, naming the expected path and key, so
    the converge stops before any playbook starts.
    """
    state = validate_config(context.project_directory, context.schema_directory)
    inventory = PlatformInventory.from_state(state)
    try:
        return owned_role_passwords(owned_databases(inventory), provider)
    except SecretsError as error:
        raise LifecycleError(ERROR_SERVICES_SECRET, error.message) from error


def run_services_playbook(
    context: EngineContext, passwords: tuple[OwnedRolePassword, ...]
) -> None:
    """Run ``services.yml`` with each owner password staged in a 0600 file.

    The files live in a private temp dir outside the project, removed when
    the run ends, whether it passed or failed.
    """
    with staged_password_files(passwords) as extra_vars:
        run_engine_playbook(context, "services.yml", extra_vars)


def converge_services(
    context: EngineContext, provider: SopsSecretProvider
) -> tuple[OwnedRolePassword, ...]:
    """Converge every declared service; return the passwords it set, by hash."""
    passwords = services_passwords(context, provider)
    run_services_playbook(context, passwords)
    return passwords


def build_release_artifact(
    context: EngineContext,
    component_id: ResourceId,
    git_ref: str,
    artifact_directory: Path,
) -> dict[str, object]:
    """Build one release artifact through the engine's CLI contract."""
    argv = (
        sys.executable,
        "-m",
        "cloudfall_engine",
        "artifact",
        "build",
        "--project",
        str(context.project_directory),
        component_id.value,
        "--ref",
        git_ref,
        "--schemas",
        str(context.schema_directory),
        "--output-dir",
        str(artifact_directory),
    )
    step = ExecutionStep(
        description="build release artifact",
        argv=argv,
        environment={},
    )
    output = _execute_step_with_output(step, context.run)
    # The engine answers one treaty envelope: the build is under ``data``.
    parsed = cast("object", json.loads(output))
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        detail = "artifact builder returned no successful envelope"
        raise LifecycleError(ERROR_EXECUTION_FAILED, detail)
    data = cast("object", parsed.get("data"))
    if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
        detail = "artifact builder returned a non-object payload"
        raise LifecycleError(ERROR_EXECUTION_FAILED, detail)
    return {"status": "ok", **cast("dict[str, object]", data)}


def _execute_steps(steps: tuple[ExecutionStep, ...], run: StepRunner) -> None:
    for step in steps:
        _execute_step_with_output(step, run)


def _execute_step_with_output(step: ExecutionStep, run: StepRunner) -> str:
    completed = run(step)
    if completed.returncode != 0:
        tail = f"{completed.stdout}\n{completed.stderr}"[-_OUTPUT_TAIL_CHARACTERS:]
        detail = f"{step.description} failed: {tail.strip()}"
        raise LifecycleError(ERROR_EXECUTION_FAILED, detail)
    return completed.stdout


def deploy(
    context: EngineContext,
    component_id: ResourceId,
    release: ReleaseId,
    artifact_directory: Path,
    options: DeployOptions | None = None,
) -> LifecycleResult:
    """Deploy one verified release behind the engine's health gate."""
    resolved_options = options if options is not None else DeployOptions()
    component = _component(context, component_id)
    artifact = verify_artifact(
        artifact_directory, component_id, release, context.schema_directory
    )
    plan = plan_deploy(
        context,
        component_id,
        release,
        artifact,
        resolved_options,
    )
    execute_plan(plan, context.run)
    receipt = None
    if resolved_options.receipt_directory is not None:
        receipt = (
            resolved_options.receipt_directory / f"{component_id.value}.json"
        )
        if not receipt.is_file():
            detail = (
                "deployment reported success but wrote no release receipt: "
                f"{receipt}"
            )
            raise LifecycleError(_ERROR_RECEIPT_MISSING, detail)
    return LifecycleResult(
        action="deploy",
        component_id=component_id,
        release=release,
        servers=component.server_ids,
        healthy=True,
        detail=None,
        receipt=receipt,
    )


def preview_deploy(
    context: EngineContext,
    component_id: ResourceId,
    release: ReleaseId,
    artifact_directory: Path,
) -> LifecyclePreview:
    """Validate a deployment without touching any server."""
    component = _component(context, component_id)
    verify_artifact(
        artifact_directory, component_id, release, context.schema_directory
    )
    release_command = ReleaseCommandPlan.for_release(component, release)
    effect = (
        f"deploy release {release.value} of component {component_id} to "
        f"{_server_list(component.server_ids)} behind its health check, "
        "rolling back automatically if the check fails"
    )
    if release_command is not None:
        effect += (
            f"; first run its release command {release_command.described} "
            f"once on {release_command.server}, before any server switches "
            "to the release"
        )
    return LifecyclePreview(
        action="deploy",
        subject=PreviewSubject.COMPONENT,
        subject_id=component_id,
        release=release,
        servers=component.server_ids,
        effect=effect,
        release_command=release_command,
    )


def preview_rollback(
    context: EngineContext, component_id: ResourceId, release: ReleaseId
) -> LifecyclePreview:
    """Validate a rollback without touching any server.

    Whether the release is still retained is known only to the servers; the
    rollback itself fails if it is not. A rollback never runs the release
    command: a downgrade stays a human decision.
    """
    component = _component(context, component_id)
    effect = (
        f"switch component {component_id} on "
        f"{_server_list(component.server_ids)} back to retained release "
        f"{release.value} and restart it behind its health check"
    )
    if component.release is not None:
        effect += (
            "; its release command "
            f"{' '.join(component.release.command.value)} is not run"
        )
    return LifecyclePreview(
        action="rollback",
        subject=PreviewSubject.COMPONENT,
        subject_id=component_id,
        release=release,
        servers=component.server_ids,
        effect=effect,
        release_command_skipped=component.release is not None,
    )


def preview_restart(
    context: EngineContext, component_id: ResourceId
) -> LifecyclePreview:
    """Validate a restart without touching any server."""
    component = _component(context, component_id)
    return LifecyclePreview(
        action="restart",
        subject=PreviewSubject.COMPONENT,
        subject_id=component_id,
        release=None,
        servers=component.server_ids,
        effect=(
            f"restart component {component_id} on "
            f"{_server_list(component.server_ids)} and require its declared "
            "health check"
        ),
    )


def rollback(
    context: EngineContext,
    component_id: ResourceId,
    release: ReleaseId,
) -> LifecycleResult:
    """Switch one component back to an existing release, health gated."""
    component = _component(context, component_id)
    plan = plan_rollback(context, component_id, release)
    execute_plan(plan, context.run)
    return LifecycleResult(
        action="rollback",
        component_id=component_id,
        release=release,
        servers=component.server_ids,
        healthy=True,
        detail=None,
        receipt=None,
    )


def restart(
    context: EngineContext, component_id: ResourceId
) -> LifecycleResult:
    """Restart one component and require its declared health check."""
    component = _component(context, component_id)
    plan = plan_restart(context, component_id)
    execute_plan(plan, context.run)
    return LifecycleResult(
        action="restart",
        component_id=component_id,
        release=None,
        servers=component.server_ids,
        healthy=True,
        detail=None,
        receipt=None,
    )


def health(
    context: EngineContext, component_id: ResourceId
) -> LifecycleResult:
    """Probe one component's declared health check on every server."""
    component = _component(context, component_id)
    plan = plan_health(context, component_id)
    healthy = True
    detail: str | None = None
    try:
        execute_plan(plan, context.run)
    except LifecycleError as error:
        if error.code != ERROR_EXECUTION_FAILED:
            raise
        healthy = False
        detail = error.detail
    return LifecycleResult(
        action="health",
        component_id=component_id,
        release=None,
        servers=component.server_ids,
        healthy=healthy,
        detail=detail,
        receipt=None,
    )


def plan_data_migration(
    context: EngineContext,
    service_id: ResourceId,
    database: str,
    source_url_file: Path,
    receipt_directory: Path | None,
) -> ExecutionPlan:
    """Compose the subprocess steps for one guided data migration.

    The plan reuses ``ExecutionPlan`` with the service identifier in the
    ``component_id`` slot; the source URL never crosses a process argument,
    only the controller-side file path does.
    """
    extra_vars: dict[str, object] = {
        "cloudfall_data_service_id": service_id.value,
        "cloudfall_data_database": database,
        "cloudfall_data_source_url_file": str(source_url_file.resolve()),
    }
    if receipt_directory is not None:
        extra_vars["cloudfall_data_receipt_directory"] = str(
            receipt_directory.resolve()
        )
    return ExecutionPlan(
        action="data-migration",
        component_id=service_id,
        release=None,
        steps=(
            _render_inventory_step(context),
            _playbook_step(context, "data.yml", extra_vars),
        ),
    )


def migrate_data(
    context: EngineContext,
    service_id: ResourceId,
    database: str,
    source_url_file: Path,
    receipt_directory: Path | None = None,
) -> dict[str, object]:
    """Dump one external database and restore it into a declared service."""
    service = _checked_data_migration(context, service_id, database, source_url_file)
    plan = plan_data_migration(
        context, service_id, database, source_url_file, receipt_directory
    )
    execute_plan(plan, context.run)
    result: dict[str, object] = {
        "status": "ok",
        "action": "data-migration",
        "service": service_id.value,
        "database": database,
        "servers": [service.server_id.value],
        "verification": "per-table row counts matched",
    }
    if receipt_directory is not None:
        result["receipt"] = str(
            receipt_directory / f"{service_id.value}-{database}.json"
        )
    return result


def preview_data_migration(
    context: EngineContext,
    service_id: ResourceId,
    database: str,
    source_url_file: Path,
) -> LifecyclePreview:
    """Validate a data migration without touching any server."""
    service = _checked_data_migration(context, service_id, database, source_url_file)
    return LifecyclePreview(
        action="data-migration",
        subject=PreviewSubject.SERVICE,
        subject_id=service_id,
        release=None,
        servers=(service.server_id,),
        effect=(
            f"dump the database whose URL is in {source_url_file} on "
            f"{service.server_id} and restore it into declared database "
            f"{database} of service {service_id}, refusing a non-empty target "
            "and verifying per-table row counts"
        ),
    )


def _checked_data_migration(
    context: EngineContext,
    service_id: ResourceId,
    database: str,
    source_url_file: Path,
) -> ServiceInventory:
    service = _postgresql_service(context, service_id)
    postgresql = service.postgresql
    if postgresql is None:
        detail = f"service {service_id} has no PostgreSQL contract"
        raise LifecycleError(_ERROR_SERVICE_MISSING, detail)
    declared = {entry.name.value for entry in postgresql.databases}
    if database not in declared:
        detail = (
            f"database {database!r} is not declared on service "
            f"{service_id}; declared: {sorted(declared)}"
        )
        raise LifecycleError(_ERROR_DATABASE_MISSING, detail)
    if not source_url_file.is_file() or not source_url_file.read_text(
        encoding="utf-8"
    ).strip():
        detail = (
            "source URL file does not exist or is empty: "
            f"{source_url_file}"
        )
        raise LifecycleError(_ERROR_SOURCE_URL_MISSING, detail)
    return service


def backup_service(
    context: EngineContext,
    service_id: ResourceId,
    receipt_directory: Path,
) -> dict[str, object]:
    """Run the declared backup for one service and return its receipt."""
    return _backup_operation(context, service_id, receipt_directory, "backup")


def verify_backup(
    context: EngineContext,
    service_id: ResourceId,
    receipt_directory: Path,
) -> dict[str, object]:
    """Run the restore-proof check for one service, receipted."""
    return _backup_operation(
        context, service_id, receipt_directory, "restore-check"
    )


def _backup_operation(
    context: EngineContext,
    service_id: ResourceId,
    receipt_directory: Path,
    action: str,
) -> dict[str, object]:
    state = validate_config(context.project_directory, context.schema_directory)
    inventory = PlatformInventory.from_state(state)
    declared = next(
        (
            service
            for service in inventory.services
            if service.resource_id == service_id
        ),
        None,
    )
    if declared is None:
        detail = f"declared service does not exist: {service_id}"
        raise LifecycleError(_ERROR_SERVICE_MISSING, detail)
    run_engine_playbook(
        context,
        "backup.yml",
        {
            "cloudfall_backup_service": service_id.value,
            "cloudfall_backup_action": action,
            "cloudfall_backup_receipt_directory": str(
                receipt_directory.resolve()
            ),
        },
    )
    receipt_path = receipt_directory / f"{service_id.value}-{action}.json"
    if not receipt_path.is_file():
        detail = (
            f"no {action} receipt was written for {service_id}; the "
            "service's server produced no result"
        )
        raise LifecycleError(_ERROR_SERVICE_MISSING, detail)
    receipt = cast(
        "dict[str, object]",
        json.loads(receipt_path.read_text(encoding="utf-8")),
    )
    SchemaCatalog(context.schema_directory).validate_named(
        "backup-receipt.schema.json", receipt
    )
    return {"status": "ok", "receipt": receipt, "path": str(receipt_path)}


def _postgresql_service(
    context: EngineContext, service_id: ResourceId
) -> ServiceInventory:
    state = validate_config(context.project_directory, context.schema_directory)
    inventory = PlatformInventory.from_state(state)
    service = next(
        (
            candidate
            for candidate in inventory.services
            if candidate.resource_id == service_id
        ),
        None,
    )
    if service is None or service.postgresql is None:
        detail = (
            f"declared PostgreSQL service does not exist: {service_id}"
        )
        raise LifecycleError(_ERROR_SERVICE_MISSING, detail)
    return service


def _component(
    context: EngineContext, component_id: ResourceId
) -> ComponentInventory:
    state = validate_config(context.project_directory, context.schema_directory)
    inventory = PlatformInventory.from_state(state)
    component = next(
        (
            candidate
            for candidate in inventory.components
            if candidate.resource_id == component_id
        ),
        None,
    )
    if component is None:
        detail = f"component does not exist: {component_id}"
        raise LifecycleError(_ERROR_COMPONENT_MISSING, detail)
    return component


def _domain(context: EngineContext, domain_id: ResourceId) -> DomainInventory:
    state = validate_config(context.project_directory, context.schema_directory)
    return _find_domain(PlatformInventory.from_state(state), domain_id)


def _find_domain(
    inventory: PlatformInventory, domain_id: ResourceId
) -> DomainInventory:
    domain = next(
        (
            candidate
            for candidate in inventory.domains
            if candidate.resource_id == domain_id
        ),
        None,
    )
    if domain is None:
        detail = f"domain does not exist: {domain_id}"
        raise LifecycleError(_ERROR_DOMAIN_MISSING, detail)
    return domain


def _server_list(servers: tuple[ResourceId, ...]) -> str:
    return ", ".join(server.value for server in servers)


def _render_inventory_step(context: EngineContext) -> ExecutionStep:
    return ExecutionStep(
        description="render Ansible inventory",
        argv=(
            sys.executable,
            "-m",
            "cloudfall_engine",
            "inventory",
            "render",
            "--project",
            str(context.project_directory),
            "--schemas",
            str(context.schema_directory),
            "--output",
            str(context.inventory_file),
        ),
        environment={},
    )


def _playbook_step(
    context: EngineContext,
    playbook_name: str,
    extra_vars: Mapping[str, object],
    limit: ResourceId | None = None,
) -> ExecutionStep:
    binary = shutil.which("ansible-playbook")
    if binary is None:
        detail = "ansible-playbook is not installed on this controller"
        raise LifecycleError(_ERROR_ANSIBLE_MISSING, detail)
    playbook = context.playbook(playbook_name)
    limited = () if limit is None else ("--limit", limit.value)
    return ExecutionStep(
        description=f"run {playbook_name}",
        argv=(
            binary,
            "--inventory",
            str(context.inventory_file),
            *limited,
            "--extra-vars",
            json.dumps(dict(extra_vars), sort_keys=True),
            str(playbook),
        ),
        environment=context.ansible_environment(),
    )
