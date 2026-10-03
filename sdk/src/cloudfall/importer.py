"""Import a Render blueprint into declarative Cloudfall state.

The importer maps ``render.yaml`` services onto Cloudfall resources, writes
environment files outside the project directory (the config never contains secret
values), and records every assumption, unsupported feature, and required
follow-up action in a structured gap report instead of guessing silently.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import yaml

from cloudfall.domain import LinuxUser, ResourceId, ResourceKind
from cloudfall.validation import SchemaCatalog

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

_FIRST_COMPONENT_PORT = 8100
_MAX_TCP_PORT = 65535
_DEFAULT_PYTHON_VERSION = "3.14"
_DEFAULT_NODE_VERSION = "22"
_DEFAULT_POSTGRES_MAJOR = "16"
_POSTGRES_SERVICE_ID = "postgresql-main"
_RESOURCE_SLUG_MAX = 63
_ERROR_BLUEPRINT_INVALID = "import_blueprint_invalid"
_ERROR_NAME_INVALID = "import_name_invalid"
_ERROR_NAME_COLLISION = "import_name_collision"
_ERROR_PROJECT_INVALID = "import_application_invalid"
_CATEGORY_UNSUPPORTED = "unsupported"
_CATEGORY_ASSUMPTION = "assumption"
_CATEGORY_ACTION = "action"


class RenderImportError(RuntimeError):
    """Fail-fast import error with a stable machine-readable code."""

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
class ImportGap:
    """One assumption, unsupported feature, or required follow-up."""

    category: str
    subject: str
    detail: str

    def as_dict(self) -> dict[str, object]:
        """Serialize the gap for report consumers."""
        return {
            "category": self.category,
            "subject": self.subject,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ImportTargets:
    """Where imported resources are placed."""

    application_id: ResourceId
    server_id: ResourceId
    project_directory: Path
    environment_directory: Path


@dataclass(frozen=True, slots=True)
class RenderImportResult:
    """Structured outcome of one blueprint import."""

    application_id: ResourceId
    components: tuple[str, ...]
    services: tuple[str, ...]
    domains: tuple[str, ...]
    written: tuple[Path, ...]
    environment_files: tuple[Path, ...]
    report: Path
    gaps: tuple[ImportGap, ...]

    def as_dict(self) -> dict[str, object]:
        """Serialize the import result for agent consumers."""
        return {
            "status": "ok",
            "application": self.application_id.value,
            "components": list(self.components),
            "services": list(self.services),
            "domains": list(self.domains),
            "written": [str(path) for path in self.written],
            "environmentFiles": [
                str(path) for path in self.environment_files
            ],
            "report": str(self.report),
            "gaps": [gap.as_dict() for gap in self.gaps],
        }


@dataclass(slots=True)
class _ImportState:
    """Mutable working state accumulated while mapping a blueprint."""

    gaps: list[ImportGap] = field(default_factory=list)
    component_documents: list[dict[str, object]] = field(default_factory=list)
    environment_lines: dict[str, list[str]] = field(default_factory=dict)
    domain_documents: list[dict[str, object]] = field(default_factory=list)
    used_ids: set[str] = field(default_factory=set)
    database_names: dict[str, str] = field(default_factory=dict)
    next_port: int = _FIRST_COMPONENT_PORT

    def gap(self, category: str, subject: str, detail: str) -> None:
        self.gaps.append(
            ImportGap(category=category, subject=subject, detail=detail)
        )

    def claim_id(self, slug: str, subject: str) -> None:
        if slug in self.used_ids:
            detail = f"{subject} maps to an already-used resource id: {slug}"
            raise RenderImportError(_ERROR_NAME_COLLISION, detail)
        self.used_ids.add(slug)

    def claim_port(self, declared: object, subject: str) -> int:
        if isinstance(declared, str) and declared.isdigit():
            declared = int(declared)
        if (
            isinstance(declared, int)
            and not isinstance(declared, bool)
            and 1 <= declared <= _MAX_TCP_PORT
        ):
            return declared
        port = self.next_port
        self.next_port += 1
        self.gap(
            _CATEGORY_ASSUMPTION,
            subject,
            f"assigned listen port {port}; PORT is written to the component "
            "environment file and the application must honor it",
        )
        return port


def import_render_blueprint(
    blueprint_path: Path,
    targets: ImportTargets,
    schema_directory: Path,
) -> RenderImportResult:
    """Map one Render blueprint onto validated Cloudfall state files."""
    blueprint = _load_blueprint(blueprint_path)
    return import_render_mapping(blueprint, targets, schema_directory)


def import_render_mapping(
    blueprint: Mapping[str, object],
    targets: ImportTargets,
    schema_directory: Path,
    initial_gaps: tuple[ImportGap, ...] = (),
) -> RenderImportResult:
    """Map one blueprint-shaped Render description onto state files."""
    _require_application_user(targets.application_id)
    catalog = SchemaCatalog(schema_directory)
    state = _ImportState()
    state.gaps.extend(initial_gaps)
    state.database_names = _database_name_map(blueprint)
    groups = _environment_groups(blueprint, state)

    for raw_service in _sequence(blueprint.get("services"), "services"):
        _import_service(_mapping(raw_service, "service"), targets, groups, state)

    if not state.component_documents:
        code = "render_import_empty"
        reasons = "; ".join(
            f"{gap.subject}: {gap.detail}" for gap in state.gaps
        )
        message = (
            "no importable services were found, so there is nothing to "
            f"migrate; every exclusion is explained by a gap: {reasons}"
            if reasons
            else "no services were found to import"
        )
        raise RenderImportError(code, message)

    service_documents = _import_databases(blueprint, targets, state)
    application_document = _application_document(targets, state)

    written: list[Path] = [
        _write_resource(
            catalog,
            ResourceKind.APPLICATION,
            application_document,
            targets.project_directory / "applications",
        )
    ]
    written.extend(
        _write_resource(
            catalog,
            ResourceKind.COMPONENT,
            document,
            targets.project_directory / "components",
        )
        for document in state.component_documents
    )
    written.extend(
        _write_resource(
            catalog,
            ResourceKind.SERVICE,
            document,
            targets.project_directory / "services",
        )
        for document in service_documents
    )
    written.extend(
        _write_resource(
            catalog,
            ResourceKind.DOMAIN,
            document,
            targets.project_directory / "domains",
        )
        for document in state.domain_documents
    )

    environment_files = _write_environment_files(targets, state)
    report = _write_report(targets, state)
    return RenderImportResult(
        application_id=targets.application_id,
        components=tuple(
            _document_id(document) for document in state.component_documents
        ),
        services=tuple(
            _document_id(document) for document in service_documents
        ),
        domains=tuple(
            _document_id(document) for document in state.domain_documents
        ),
        written=tuple(written),
        environment_files=environment_files,
        report=report,
        gaps=tuple(state.gaps),
    )


def _import_service(
    raw_service: Mapping[str, object],
    targets: ImportTargets,
    groups: Mapping[str, Sequence[Mapping[str, object]]],
    state: _ImportState,
) -> None:
    name = str(raw_service.get("name", "unnamed"))
    service_type = str(raw_service.get("type", ""))
    runtime = str(
        raw_service.get("runtime", raw_service.get("env", ""))
    )
    if service_type == "cron":
        state.gap(
            _CATEGORY_UNSUPPORTED,
            name,
            "cron services are not modeled yet; recreate the schedule "
            f"({raw_service.get('schedule')!r}) as a systemd timer manually",
        )
        return
    if service_type in {"static", "redis", "keyvalue"}:
        state.gap(
            _CATEGORY_UNSUPPORTED,
            name,
            f"{service_type} services are not part of the v1 catalog",
        )
        return
    if runtime in {"docker", "image"}:
        state.gap(
            _CATEGORY_UNSUPPORTED,
            name,
            "container runtimes are a tracked decision; only native python "
            "and node services import today",
        )
        return
    if service_type not in {"web", "worker", "pserv"}:
        state.gap(
            _CATEGORY_UNSUPPORTED,
            name,
            f"service type {service_type!r} is not supported by the importer",
        )
        return
    if runtime not in {"python", "node"}:
        state.gap(
            _CATEGORY_UNSUPPORTED,
            name,
            f"runtime {runtime!r} is not supported by the importer",
        )
        return
    _import_component(
        raw_service, name, service_type, runtime, targets, groups, state
    )


def _import_component(  # noqa: PLR0913 - one boundary mapping, many inputs.
    raw_service: Mapping[str, object],
    name: str,
    service_type: str,
    runtime: str,
    targets: ImportTargets,
    groups: Mapping[str, Sequence[Mapping[str, object]]],
    state: _ImportState,
) -> None:
    repo = raw_service.get("repo")
    if not isinstance(repo, str) or not repo:
        state.gap(
            _CATEGORY_ACTION,
            name,
            "declares no repository; add repo to the blueprint or create "
            "the Component manually",
        )
        return
    raw_start = raw_service.get("startCommand")
    if not isinstance(raw_start, str) or not raw_start.strip():
        state.gap(
            _CATEGORY_ACTION,
            name,
            "declares no startCommand; Cloudfall needs an explicit argv "
            "service command",
        )
        return

    component_id = _resource_slug(name)
    state.claim_id(component_id, f"service {name}")
    environment, resolved = _resolve_environment(raw_service, groups, name, state)
    port: int | None = None
    if service_type in {"web", "pserv"}:
        port = state.claim_port(resolved.get("PORT"), name)
        environment = [
            f"PORT={port}",
            *(
                line
                for line in environment
                if not line.startswith("PORT=")
            ),
        ]

    if raw_service.get("buildCommand"):
        state.gap(
            _CATEGORY_ASSUMPTION,
            name,
            "buildCommand is not executed; Cloudfall materializes locked "
            "dependencies at deploy time instead",
        )
    branch = raw_service.get("branch")
    if isinstance(branch, str) and branch:
        state.gap(
            _CATEGORY_ACTION,
            name,
            f"build releases from the declared branch: cloudfall-engine "
            f"artifact build ... --ref {branch}",
        )

    document: dict[str, object] = {
        "apiVersion": "cloudfall/v1",
        "kind": "Component",
        "metadata": {
            "id": component_id,
            "description": f"Imported from Render service {name}",
        },
        "spec": {
            "application": targets.application_id.value,
            "repository": _repository(raw_service, repo),
            "runtime": _runtime(runtime, resolved, name, state),
            "deployment": {
                "strategy": "artifact-symlink",
                "servers": [targets.server_id.value],
                "installRoot": (
                    f"/srv/apps/{targets.application_id.value}/{component_id}"
                ),
                "retainUntilCleanup": True,
            },
            "service": {
                "manager": "systemd",
                "name": component_id,
                "command": shlex.split(raw_start),
            },
            "healthCheck": _health_check(raw_service, port, name, state),
        },
    }
    state.component_documents.append(document)
    if environment:
        state.environment_lines[component_id] = environment
    _import_domains(raw_service, component_id, port, targets, state)


def _repository(
    raw_service: Mapping[str, object], repo: str
) -> dict[str, object]:
    repository: dict[str, object] = {"url": repo}
    root_directory = raw_service.get("rootDir")
    if isinstance(root_directory, str) and root_directory:
        repository["subdirectory"] = root_directory
    return repository


def _runtime(
    runtime: str,
    resolved: Mapping[str, str],
    name: str,
    state: _ImportState,
) -> dict[str, object]:
    if runtime == "python":
        version = resolved.get("PYTHON_VERSION", _DEFAULT_PYTHON_VERSION)
        if "PYTHON_VERSION" not in resolved:
            state.gap(
                _CATEGORY_ASSUMPTION,
                name,
                f"no PYTHON_VERSION declared; assuming "
                f"{_DEFAULT_PYTHON_VERSION}",
            )
        state.gap(
            _CATEGORY_ACTION,
            name,
            "Cloudfall deploys python components with uv sync --frozen; the "
            "repository needs pyproject.toml and uv.lock",
        )
        return {
            "type": "python",
            "version": version,
            "packageManager": "uv",
        }
    version = resolved.get("NODE_VERSION", _DEFAULT_NODE_VERSION)
    if "NODE_VERSION" not in resolved:
        state.gap(
            _CATEGORY_ASSUMPTION,
            name,
            f"no NODE_VERSION declared; assuming {_DEFAULT_NODE_VERSION}",
        )
    state.gap(
        _CATEGORY_ACTION,
        name,
        "node components are modeled in state but the deploy role supports "
        "uv-managed python only in this slice",
    )
    return {"type": "node", "version": version, "packageManager": "npm"}


def _health_check(
    raw_service: Mapping[str, object],
    port: int | None,
    name: str,
    state: _ImportState,
) -> dict[str, object]:
    if port is None:
        return {"type": "none"}
    raw_path = raw_service.get("healthCheckPath")
    path = raw_path if isinstance(raw_path, str) and raw_path else "/"
    if path == "/":
        state.gap(
            _CATEGORY_ASSUMPTION,
            name,
            "no healthCheckPath declared; probing / instead",
        )
    return {
        "type": "http",
        "scheme": "http",
        "port": port,
        "path": path,
        "expectedStatuses": [200],
        "timeoutSeconds": 5,
        "attempts": 5,
    }


def _import_domains(
    raw_service: Mapping[str, object],
    component_id: str,
    port: int | None,
    targets: ImportTargets,
    state: _ImportState,
) -> None:
    raw_domains = raw_service.get("domains")
    if raw_domains is None:
        return
    if port is None:
        state.gap(
            _CATEGORY_ACTION,
            component_id,
            "declares domains but exposes no HTTP port; route it manually",
        )
        return
    for raw_domain in _sequence(raw_domains, "domains"):
        domain_name = str(raw_domain)
        domain_id = _resource_slug(domain_name)
        state.claim_id(domain_id, f"domain {domain_name}")
        state.domain_documents.append(
            {
                "apiVersion": "cloudfall/v1",
                "kind": "Domain",
                "metadata": {
                    "id": domain_id,
                    "description": (
                        f"Imported Render domain for {component_id}"
                    ),
                },
                "spec": {
                    "primaryName": domain_name,
                    "aliases": [],
                    "proxy": {
                        "server": targets.server_id.value,
                        "configurationPath": (
                            f"/etc/nginx/sites-available/{domain_name}.conf"
                        ),
                        "service": "nginx.service",
                        "upstream": {"address": "127.0.0.1", "port": port},
                    },
                    "origin": {
                        "server": targets.server_id.value,
                        "configurationPath": (
                            f"/etc/nginx/sites-available/{domain_name}.conf"
                        ),
                        "service": "nginx.service",
                        "scheme": "http",
                        "port": port,
                        "serverName": domain_name,
                    },
                    "edge": {"provider": "external", "mode": "dns-only"},
                    "tls": {"mode": "required"},
                    "healthCheck": {
                        "scheme": "https",
                        "path": "/",
                        "expectedStatuses": [200],
                        "timeoutSeconds": 10,
                    },
                },
            }
        )
        state.gap(
            _CATEGORY_ACTION,
            domain_name,
            "lower the DNS TTL, point the record at the target server, and "
            "deploy the route with task domains:deploy",
        )


def _import_databases(
    blueprint: Mapping[str, object],
    targets: ImportTargets,
    state: _ImportState,
) -> list[dict[str, object]]:
    raw_databases = blueprint.get("databases")
    if raw_databases is None:
        return []
    databases: list[dict[str, object]] = []
    major_version: str | None = None
    for raw_database in _sequence(raw_databases, "databases"):
        entry = _mapping(raw_database, "database")
        name = str(entry.get("name", "database"))
        databases.append(
            {
                "name": state.database_names[name],
                "application": targets.application_id.value,
            }
        )
        declared_major = entry.get("postgresMajorVersion")
        if isinstance(declared_major, str) and major_version is None:
            major_version = declared_major
        if entry.get("user") is not None:
            state.gap(
                _CATEGORY_ACTION,
                name,
                "database roles use peer authentication as the application user; "
                "update connection strings that referenced the Render user",
            )
    if major_version is None:
        major_version = _DEFAULT_POSTGRES_MAJOR
        state.gap(
            _CATEGORY_ASSUMPTION,
            _POSTGRES_SERVICE_ID,
            "no postgresMajorVersion declared; assuming "
            f"{_DEFAULT_POSTGRES_MAJOR}",
        )
    state.gap(
        _CATEGORY_ACTION,
        _POSTGRES_SERVICE_ID,
        "before cutover, save the Render database connection URL into a "
        "local file and migrate the data with: cloudfall data copy "
        f"{_POSTGRES_SERVICE_ID} --database <name> --source-url-file <file> "
        "--yes (or pass --data <name>=<file> to cloudfall migrate)",
    )
    return [
        {
            "apiVersion": "cloudfall/v1",
            "kind": "Service",
            "metadata": {
                "id": _POSTGRES_SERVICE_ID,
                "description": "Imported from Render managed PostgreSQL",
            },
            "spec": {
                "serviceKind": "postgresql",
                "environment": "production",
                "server": targets.server_id.value,
                "bind": {"address": "127.0.0.1", "port": 5432},
                "postgresql": {
                    "majorVersion": major_version,
                    "databases": databases,
                },
                "backup": {
                    "directory": (
                        f"/var/backups/cloudfall/{_POSTGRES_SERVICE_ID}"
                    ),
                    "onCalendar": "*-*-* 02:00:00 UTC",
                    "retentionDays": 14,
                },
            },
        }
    ]


def _resolve_environment(
    raw_service: Mapping[str, object],
    groups: Mapping[str, Sequence[Mapping[str, object]]],
    name: str,
    state: _ImportState,
) -> tuple[list[str], dict[str, str]]:
    lines: list[str] = []
    resolved: dict[str, str] = {}
    raw_env = raw_service.get("envVars")
    if raw_env is None:
        return lines, resolved
    entries: list[Mapping[str, object]] = []
    for raw_entry in _sequence(raw_env, "envVars"):
        entry = _mapping(raw_entry, "environment variable")
        group_name = entry.get("fromGroup")
        if isinstance(group_name, str):
            group = groups.get(group_name)
            if group is None:
                state.gap(
                    _CATEGORY_ACTION,
                    name,
                    f"references undeclared env group {group_name!r}",
                )
                continue
            entries.extend(group)
            continue
        entries.append(entry)
    for entry in entries:
        _resolve_environment_entry(entry, name, lines, resolved, state)
    return lines, resolved


def _resolve_environment_entry(
    entry: Mapping[str, object],
    name: str,
    lines: list[str],
    resolved: dict[str, str],
    state: _ImportState,
) -> None:
    key = entry.get("key")
    if not isinstance(key, str) or not key:
        state.gap(
            _CATEGORY_ACTION, name, "contains an environment entry without a key"
        )
        return
    value = entry.get("value")
    from_database = entry.get("fromDatabase")
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        resolved[key] = str(value)
        lines.append(f"{key}={value}")
        return
    if entry.get("generateValue") is True or entry.get("sync") is False:
        lines.append(f"{key}=")
        state.gap(
            _CATEGORY_ACTION,
            name,
            f"provide a value for {key} in the component environment file",
        )
        return
    if isinstance(from_database, dict):
        database = cast("Mapping[str, object]", from_database)
        if database.get("property") == "connectionString":
            render_name = str(database.get("name", "database"))
            slug = state.database_names.get(
                render_name, _postgres_slug(render_name)
            )
            lines.append(
                f"{key}=postgresql:///{slug}?host=/var/run/postgresql"
            )
            state.gap(
                _CATEGORY_ASSUMPTION,
                name,
                f"{key} now uses peer authentication over the local socket",
            )
            return
        lines.append(f"{key}=")
        state.gap(
            _CATEGORY_ACTION,
            name,
            f"{key} referenced database property "
            f"{database.get('property')!r}; resolve it manually",
        )
        return
    lines.append(f"{key}=")
    state.gap(
        _CATEGORY_ACTION,
        name,
        f"{key} uses an unsupported source; resolve it manually",
    )


def _database_name_map(blueprint: Mapping[str, object]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    raw_databases = blueprint.get("databases")
    if raw_databases is None:
        return mapping
    for raw_database in _sequence(raw_databases, "databases"):
        entry = _mapping(raw_database, "database")
        name = str(entry.get("name", "database"))
        declared = entry.get("databaseName")
        mapping[name] = _postgres_slug(
            declared if isinstance(declared, str) else name
        )
    return mapping


def _environment_groups(
    blueprint: Mapping[str, object], state: _ImportState
) -> dict[str, list[Mapping[str, object]]]:
    groups: dict[str, list[Mapping[str, object]]] = {}
    raw_groups = blueprint.get("envVarGroups")
    if raw_groups is None:
        return groups
    for raw_group in _sequence(raw_groups, "envVarGroups"):
        group = _mapping(raw_group, "environment group")
        group_name = str(group.get("name", ""))
        entries = [
            _mapping(item, "environment variable")
            for item in _sequence(group.get("envVars", []), "group envVars")
        ]
        groups[group_name] = entries
        state.gap(
            _CATEGORY_ASSUMPTION,
            group_name,
            "env group entries were inlined into each referencing component",
        )
    return groups


def _application_document(
    targets: ImportTargets, state: _ImportState
) -> dict[str, object]:
    return {
        "apiVersion": "cloudfall/v1",
        "kind": "Application",
        "metadata": {
            "id": targets.application_id.value,
            "description": "Imported from a Render blueprint",
        },
        "spec": {
            "linuxUser": targets.application_id.value,
            "approval": "manual",
            "components": [
                _document_id(document)
                for document in state.component_documents
            ],
        },
    }


def _write_resource(
    catalog: SchemaCatalog,
    kind: ResourceKind,
    document: dict[str, object],
    directory: Path,
) -> Path:
    catalog.validate(kind, document)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_document_id(document)}.yaml"
    path.write_text(
        f"---\n{yaml.safe_dump(document, sort_keys=False)}",
        encoding="utf-8",
    )
    return path


def _write_environment_files(
    targets: ImportTargets, state: _ImportState
) -> tuple[Path, ...]:
    paths: list[Path] = []
    for component_id, lines in sorted(state.environment_lines.items()):
        targets.environment_directory.mkdir(parents=True, exist_ok=True)
        path = targets.environment_directory / f"{component_id}.env"
        content = "\n".join(lines)
        path.write_text(f"{content}\n", encoding="utf-8")
        path.chmod(0o600)
        paths.append(path)
        state.gap(
            _CATEGORY_ACTION,
            component_id,
            f"review {path} and pass it to deployment with --env-file",
        )
    return tuple(paths)


def _write_report(targets: ImportTargets, state: _ImportState) -> Path:
    lines = [
        "# Render import report",
        "",
        "Imported resources are declarative state only. Merge this",
        "directory with your servers and server types, then run",
        "`cloudfall config validate` before deploying anything.",
        "",
    ]
    for category, title in (
        (_CATEGORY_UNSUPPORTED, "Not imported"),
        (_CATEGORY_ASSUMPTION, "Assumptions"),
        (_CATEGORY_ACTION, "Required actions"),
    ):
        entries = [gap for gap in state.gaps if gap.category == category]
        if not entries:
            continue
        lines.append(f"## {title}")
        lines.append("")
        lines.extend(
            f"- **{gap.subject}**: {gap.detail}" for gap in entries
        )
        lines.append("")
    path = targets.project_directory / "IMPORT-REPORT.md"
    targets.project_directory.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _require_application_user(application_id: ResourceId) -> None:
    try:
        LinuxUser(application_id.value)
    except ValueError as error:
        detail = (
            f"application id {application_id.value!r} is not usable as a Linux user"
        )
        raise RenderImportError(_ERROR_PROJECT_INVALID, detail) from error


def _load_blueprint(blueprint_path: Path) -> Mapping[str, object]:
    if not blueprint_path.is_file():
        detail = f"blueprint does not exist: {blueprint_path}"
        raise RenderImportError(_ERROR_BLUEPRINT_INVALID, detail)
    try:
        raw = cast(
            "object",
            yaml.safe_load(blueprint_path.read_text(encoding="utf-8")),
        )
    except yaml.YAMLError as error:
        detail = f"blueprint is not valid YAML: {error}"
        raise RenderImportError(_ERROR_BLUEPRINT_INVALID, detail) from error
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) for key in raw
    ):
        detail = "blueprint root must be a mapping"
        raise RenderImportError(_ERROR_BLUEPRINT_INVALID, detail)
    return cast("Mapping[str, object]", raw)


def _resource_slug(name: str) -> str:
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", name.lower()))
    slug = slug.strip("-")[:_RESOURCE_SLUG_MAX].strip("-")
    if not slug or not slug[0].isalpha():
        detail = f"cannot derive a resource id from {name!r}"
        raise RenderImportError(_ERROR_NAME_INVALID, detail)
    return slug


def _postgres_slug(name: str) -> str:
    slug = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", name.lower()))
    slug = slug.strip("_")[:_RESOURCE_SLUG_MAX].strip("_")
    if not slug or not slug[0].isalpha():
        detail = f"cannot derive a database name from {name!r}"
        raise RenderImportError(_ERROR_NAME_INVALID, detail)
    return slug


def _document_id(document: Mapping[str, object]) -> str:
    metadata = _mapping(document.get("metadata"), "metadata")
    return str(metadata.get("id"))


def _sequence(value: object, concept: str) -> tuple[object, ...]:
    if not isinstance(value, list):
        detail = f"blueprint {concept} must be a list"
        raise RenderImportError(_ERROR_BLUEPRINT_INVALID, detail)
    return tuple(value)


def _mapping(value: object, concept: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        detail = f"blueprint {concept} must be a mapping"
        raise RenderImportError(_ERROR_BLUEPRINT_INVALID, detail)
    return cast("Mapping[str, object]", value)
