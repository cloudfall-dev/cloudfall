"""Load and strictly validate declarative Cloudfall state."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Network
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource

from cloudfall.domain import (
    IpAddress,
    IpNetworkCidr,
    OpenSshPublicKey,
    RawDocument,
    ResourceDocument,
    ResourceId,
    ResourceKey,
    ResourceKind,
    SourceLocation,
)

_SCHEMA_FILES: Mapping[ResourceKind, str] = {
    ResourceKind.SERVER: "server.schema.json",
    ResourceKind.HOST_PROFILE: "server-type.schema.json",
    ResourceKind.APPLICATION: "application.schema.json",
    ResourceKind.COMPONENT: "component.schema.json",
    ResourceKind.DOMAIN: "domain.schema.json",
    ResourceKind.SSH_PUBLIC_KEY: "ssh-public-key.schema.json",
    ResourceKind.LOGGING_STACK: "logging-stack.schema.json",
    ResourceKind.SERVICE: "service.schema.json",
    ResourceKind.ALERT_RULE: "alert-rule.schema.json",
    ResourceKind.OPERATOR_POLICY: "operator-policy.schema.json",
}
_YAML_SUFFIXES = frozenset({".yaml", ".yml"})


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """Structured validation failure suitable for CLI and agent consumers."""

    code: str
    message: str
    source: SourceLocation | None = None
    field_path: tuple[str | int, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """Serialize the issue for agent and CLI consumers."""
        result: dict[str, object] = {
            "code": self.code,
            "message": self.message,
        }
        if self.source is not None:
            result["source"] = self.source.display()
        if self.field_path:
            result["field"] = ".".join(str(part) for part in self.field_path)
        return result


class ConfigValidationError(ValueError):
    """Fail-fast state validation error with a stable machine-readable code."""

    def __init__(self, issue: ValidationIssue) -> None:
        """Build an exception around one actionable validation issue."""
        self.issue = issue
        location = f" at {issue.source.display()}" if issue.source else ""
        field = (
            f" ({'.'.join(str(part) for part in issue.field_path)})"
            if issue.field_path
            else ""
        )
        super().__init__(f"{issue.code}{location}{field}: {issue.message}")

    def as_dict(self) -> dict[str, object]:
        """Serialize the error envelope for system boundaries."""
        return {"status": "error", "error": self.issue.as_dict()}


@dataclass(frozen=True, slots=True)
class ValidatedConfig:
    """Read-only, indexed config."""

    documents: tuple[ResourceDocument, ...]
    _index: Mapping[ResourceKey, ResourceDocument]

    @classmethod
    def empty(cls) -> ValidatedConfig:
        """Return a config holding no resources.

        Some commands read the repository without reading the fleet: the
        operations catalog is declared and validated on its own.
        """
        return cls(documents=(), _index={})

    @property
    def resource_count(self) -> int:
        """Return the number of validated resource documents."""
        return len(self.documents)

    def counts_by_kind(self) -> dict[str, int]:
        """Return deterministic resource counts grouped by kind."""
        counts = Counter(document.key.kind.value for document in self.documents)
        return dict(sorted(counts.items()))

    def get(self, kind: ResourceKind, resource_id: ResourceId) -> ResourceDocument:
        """Return a resource from the typed index."""
        key = ResourceKey(kind=kind, resource_id=resource_id)
        try:
            return self._index[key]
        except KeyError as error:
            message = f"resource does not exist: {kind.value}/{resource_id}"
            raise KeyError(message) from error

    def resources(self, kind: ResourceKind) -> tuple[ResourceDocument, ...]:
        """Return all resources of one kind sorted by identifier."""
        return tuple(
            sorted(
                (document for document in self.documents if document.key.kind is kind),
                key=lambda document: document.key.resource_id.value,
            )
        )

    def as_dict(self) -> dict[str, object]:
        """Serialize the successful validation summary."""
        return {
            "status": "ok",
            "resources": self.resource_count,
            "byKind": self.counts_by_kind(),
        }


class SchemaCatalog:
    """Validated v1 schemas and their reference registry."""

    def __init__(self, schema_directory: Path) -> None:
        """Load and validate all schemas needed by the v1 contract."""
        if not schema_directory.is_dir():
            issue = ValidationIssue(
                code="schema_directory_missing",
                message=f"schema directory does not exist: {schema_directory}",
            )
            raise ConfigValidationError(issue)

        schemas = self._load_schemas(schema_directory)
        registry = Registry().with_resources(
            (self._schema_id(schema, path), Resource.from_contents(schema))
            for path, schema in schemas.items()
        )

        named_validators: dict[str, Draft202012Validator] = {}
        for path, schema in schemas.items():
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as error:
                issue = ValidationIssue(
                    code="schema_invalid",
                    message=f"{path}: {error.message}",
                )
                raise ConfigValidationError(issue) from error
            named_validators[path.name] = Draft202012Validator(
                schema,
                registry=registry,
                format_checker=FormatChecker(),
            )

        validators: dict[ResourceKind, Draft202012Validator] = {}
        for kind, filename in _SCHEMA_FILES.items():
            path = schema_directory / filename
            try:
                validators[kind] = named_validators[path.name]
            except KeyError as error:
                issue = ValidationIssue(
                    code="schema_missing",
                    message=f"required schema does not exist: {path}",
                )
                raise ConfigValidationError(issue) from error
        self._validators = validators
        self._named_validators = named_validators

    @staticmethod
    def _load_schemas(schema_directory: Path) -> dict[Path, Mapping[str, object]]:
        schemas: dict[Path, Mapping[str, object]] = {}
        for path in sorted(schema_directory.glob("*.schema.json")):
            try:
                raw = cast("object", json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError as error:
                issue = ValidationIssue(
                    code="schema_json_invalid",
                    message=f"{path}: {error.msg}",
                )
                raise ConfigValidationError(issue) from error
            if not isinstance(raw, dict) or not all(
                isinstance(key, str) for key in raw
            ):
                issue = ValidationIssue(
                    code="schema_shape_invalid",
                    message=f"schema root must be an object: {path}",
                )
                raise ConfigValidationError(issue)
            schemas[path] = cast("Mapping[str, object]", raw)
        return schemas

    @staticmethod
    def _schema_id(schema: Mapping[str, object], path: Path) -> str:
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str):
            issue = ValidationIssue(
                code="schema_id_missing",
                message=f"schema must define a string $id: {path}",
            )
            raise ConfigValidationError(issue)
        return schema_id

    def validate(self, kind: ResourceKind, content: Mapping[str, object]) -> None:
        """Validate one resource against the schema for its kind."""
        self._validators[kind].validate(content)

    def validate_named(self, filename: str, content: Mapping[str, object]) -> None:
        """Validate one document against a named schema in the catalog."""
        try:
            validator = self._named_validators[filename]
        except KeyError as error:
            issue = ValidationIssue(
                code="schema_missing",
                message=f"schema does not exist in catalog: {filename}",
            )
            raise ConfigValidationError(issue) from error
        validator.validate(content)


def _resource_files(project_directory: Path) -> Iterable[tuple[ResourceKind, Path]]:
    """Yield each kind directory's YAML files in deterministic order.

    Resources live in their kind's directory (``servers/``, ``components/``,
    ...). Nothing else in a project is read as a resource: not ``tmp/``, not
    playbooks, not secrets, not tooling files.
    """
    for kind in ResourceKind:
        kind_directory = project_directory / kind.directory
        if not kind_directory.is_dir():
            continue
        for path in sorted(kind_directory.rglob("*")):
            if path.is_file() and path.suffix.lower() in _YAML_SUFFIXES:
                yield kind, path


class StateValidator:
    """Schema and semantic validator for a project directory."""

    def __init__(self, schemas: SchemaCatalog) -> None:
        """Create a state validator backed by a validated schema catalog."""
        self._schemas = schemas

    def validate_directory(self, project_directory: Path) -> ValidatedConfig:
        """Validate every YAML resource in a project's kind directories."""
        if not project_directory.is_dir():
            issue = ValidationIssue(
                code="project_directory_missing",
                message=f"project directory does not exist: {project_directory}",
            )
            raise ConfigValidationError(issue)

        paths = tuple(_resource_files(project_directory))
        if not paths:
            issue = ValidationIssue(
                code="project_empty",
                message=f"project contains no YAML resources: {project_directory}",
            )
            raise ConfigValidationError(issue)

        documents: list[ResourceDocument] = []
        index: dict[ResourceKey, ResourceDocument] = {}
        for kind, path in paths:
            for document in self._load_file(path):
                if document.key.kind is not kind:
                    issue = ValidationIssue(
                        code="resource_misplaced",
                        message=(
                            f"{document.key.kind.value}/{document.key.resource_id} "
                            f"belongs in {document.key.kind.directory}/, "
                            f"not {kind.directory}/"
                        ),
                        source=document.source,
                    )
                    raise ConfigValidationError(issue)
                if document.key in index:
                    first = index[document.key]
                    issue = ValidationIssue(
                        code="resource_duplicate",
                        message=(
                            f"{document.key.kind.value}/{document.key.resource_id} "
                            f"already defined at {first.source.display()}"
                        ),
                        source=document.source,
                    )
                    raise ConfigValidationError(issue)
                index[document.key] = document
                documents.append(document)

        self._validate_references(documents, index)
        return ValidatedConfig(documents=tuple(documents), _index=index)

    def validate_documents(
        self, raw_documents: Iterable[RawDocument]
    ) -> ValidatedConfig:
        """Validate documents already in memory, whatever assembled them.

        A reader that derives resources from somewhere other than the
        project directory gets the same schemas, the same reference checks
        and the same error codes as a file on disk.
        """
        documents: list[ResourceDocument] = []
        index: dict[ResourceKey, ResourceDocument] = {}
        for raw in raw_documents:
            document = self._validate_document(raw.content, raw.source)
            if document.key in index:
                first = index[document.key]
                issue = ValidationIssue(
                    code="resource_duplicate",
                    message=(
                        f"{document.key.kind.value}/{document.key.resource_id} "
                        f"already defined at {first.source.display()}"
                    ),
                    source=document.source,
                )
                raise ConfigValidationError(issue)
            index[document.key] = document
            documents.append(document)

        self._validate_references(documents, index)
        return ValidatedConfig(documents=tuple(documents), _index=index)

    def _load_file(self, path: Path) -> Iterable[ResourceDocument]:
        try:
            with path.open(encoding="utf-8") as stream:
                raw_documents = tuple(
                    cast("object", document) for document in yaml.safe_load_all(stream)
                )
        except yaml.YAMLError as error:
            issue = ValidationIssue(
                code="yaml_invalid",
                message=str(error),
                source=SourceLocation(path=path, document_number=1),
            )
            raise ConfigValidationError(issue) from error

        for number, raw in enumerate(raw_documents, start=1):
            source = SourceLocation(path=path, document_number=number)
            if raw is None:
                issue = ValidationIssue(
                    code="document_empty",
                    message="YAML document is empty",
                    source=source,
                )
                raise ConfigValidationError(issue)
            if not isinstance(raw, dict) or not all(
                isinstance(key, str) for key in raw
            ):
                issue = ValidationIssue(
                    code="document_shape_invalid",
                    message="resource document must be an object with string keys",
                    source=source,
                )
                raise ConfigValidationError(issue)
            content = cast("Mapping[str, object]", raw)
            yield self._validate_document(content, source)

    def _validate_document(
        self, content: Mapping[str, object], source: SourceLocation
    ) -> ResourceDocument:
        kind = self._resource_kind(content, source)
        try:
            self._schemas.validate(kind, content)
        except ValidationError as error:
            issue = ValidationIssue(
                code="schema_validation_failed",
                message=error.message,
                source=source,
                field_path=tuple(error.absolute_path),
            )
            raise ConfigValidationError(issue) from error
        resource_id = self._resource_id(content, source)
        return ResourceDocument(
            key=ResourceKey(kind=kind, resource_id=resource_id),
            content=content,
            source=source,
        )

    @staticmethod
    def _resource_kind(
        content: Mapping[str, object], source: SourceLocation
    ) -> ResourceKind:
        try:
            return ResourceKind.from_boundary(content.get("kind"))
        except (TypeError, ValueError) as error:
            issue = ValidationIssue(
                code="resource_kind_invalid",
                message=str(error),
                source=source,
                field_path=("kind",),
            )
            raise ConfigValidationError(issue) from error

    @staticmethod
    def _resource_id(
        content: Mapping[str, object], source: SourceLocation
    ) -> ResourceId:
        metadata = _required_mapping(content, "metadata", source)
        try:
            return ResourceId.from_boundary(metadata.get("id"))
        except (TypeError, ValueError) as error:
            issue = ValidationIssue(
                code="resource_id_invalid",
                message=str(error),
                source=source,
                field_path=("metadata", "id"),
            )
            raise ConfigValidationError(issue) from error

    @staticmethod
    def _validate_references(
        documents: Sequence[ResourceDocument],
        index: Mapping[ResourceKey, ResourceDocument],
    ) -> None:
        for document in documents:
            spec = _required_mapping(document.content, "spec", document.source)
            _REFERENCE_VALIDATORS[document.key.kind](document, spec, index)
        _validate_logging_uniqueness(documents)
        _validate_service_uniqueness(documents)


def _validate_server_references(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    server_type_id = _resource_id_value(spec, "serverType", document.source)
    _require_resource(
        index,
        ResourceKind.HOST_PROFILE,
        server_type_id,
        document.source,
        ("spec", "serverType"),
    )
    _validate_server_network(spec, document.source)


def _validate_server_type_references(
    document: ResourceDocument,
    spec: Mapping[str, object],
    _index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    _validate_server_type(spec, document.source)


def _validate_application_references(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    component_ids = _resource_id_list(spec, "components", document.source)
    for component_id in component_ids:
        _require_resource(
            index,
            ResourceKind.COMPONENT,
            component_id,
            document.source,
            ("spec", "components"),
        )


def _validate_ssh_public_key_references(
    document: ResourceDocument,
    spec: Mapping[str, object],
    _index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    _validate_ssh_public_key(document, spec)


def _validate_ssh_public_key(
    document: ResourceDocument, spec: Mapping[str, object]
) -> None:
    try:
        OpenSshPublicKey.from_boundary(spec.get("publicKey"))
    except (TypeError, ValueError) as error:
        issue = ValidationIssue(
            code="ssh_public_key_invalid",
            message=str(error),
            source=document.source,
            field_path=("spec", "publicKey"),
        )
        raise ConfigValidationError(issue) from error


def _validate_component_references(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    application_id = _resource_id_value(spec, "application", document.source)
    application = _require_resource(
        index,
        ResourceKind.APPLICATION,
        application_id,
        document.source,
        ("spec", "application"),
    )
    deployment = _required_mapping(spec, "deployment", document.source)
    _validate_install_root(deployment, application_id, document.source)
    server_ids = _resource_id_list(deployment, "servers", document.source)
    for server_id in server_ids:
        _require_resource(
            index,
            ResourceKind.SERVER,
            server_id,
            document.source,
            ("spec", "deployment", "servers"),
        )
    application_spec = _required_mapping(
        application.content, "spec", application.source
    )
    application_components = _resource_id_list(
        application_spec, "components", application.source
    )
    if document.key.resource_id not in application_components:
        issue = ValidationIssue(
            code="application_component_mismatch",
            message=(
                f"Application/{application_id} does not list "
                f"Component/{document.key.resource_id}"
            ),
            source=document.source,
            field_path=("spec", "application"),
        )
        raise ConfigValidationError(issue)


def _validate_domain_references(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    proxy = _required_mapping(spec, "proxy", document.source)
    origin = _required_mapping(spec, "origin", document.source)
    for field_name, content in (("proxy", proxy), ("origin", origin)):
        server_id = _resource_id_value(content, "server", document.source)
        _require_resource(
            index,
            ResourceKind.SERVER,
            server_id,
            document.source,
            ("spec", field_name, "server"),
        )
    primary_name = spec.get("primaryName")
    aliases = spec.get("aliases")
    if isinstance(aliases, list) and primary_name in aliases:
        issue = ValidationIssue(
            code="domain_alias_invalid",
            message="primaryName must not also appear in aliases",
            source=document.source,
            field_path=("spec", "aliases"),
        )
        raise ConfigValidationError(issue)
    tls = _required_mapping(spec, "tls", document.source)
    health = _required_mapping(spec, "healthCheck", document.source)
    if tls.get("mode") == "required" and health.get("scheme") != "https":
        issue = ValidationIssue(
            code="domain_health_scheme_invalid",
            message="TLS-required domains must use an HTTPS health check",
            source=document.source,
            field_path=("spec", "healthCheck", "scheme"),
        )
        raise ConfigValidationError(issue)


def _validate_service_references(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    environment = _resource_id_value(spec, "environment", document.source)
    server_id = _resource_id_value(spec, "server", document.source)
    server = _require_resource(
        index,
        ResourceKind.SERVER,
        server_id,
        document.source,
        ("spec", "server"),
    )
    server_spec = _required_mapping(server.content, "spec", server.source)
    server_environment = _resource_id_value(
        server_spec, "environment", server.source
    )
    if server_environment != environment:
        issue = ValidationIssue(
            code="service_environment_mismatch",
            message=(
                f"Server/{server_id} belongs to {server_environment}, "
                f"not {environment}"
            ),
            source=document.source,
            field_path=("spec", "environment"),
        )
        raise ConfigValidationError(issue)

    if spec.get("serviceKind") == "postgresql":
        _validate_postgresql_service(document, spec, server, index)


def _validate_postgresql_service(
    document: ResourceDocument,
    spec: Mapping[str, object],
    server: ResourceDocument,
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    postgresql = _required_mapping(spec, "postgresql", document.source)
    raw_databases = postgresql.get("databases")
    if not isinstance(raw_databases, list):
        issue = ValidationIssue(
            code="internal_state_shape_invalid",
            message="validated service databases are not a list",
            source=document.source,
            field_path=("spec", "postgresql", "databases"),
        )
        raise ConfigValidationError(issue)
    seen_names: set[object] = set()
    databases: list[tuple[int, Mapping[str, object]]] = []
    for position, raw_database in enumerate(raw_databases):
        if not isinstance(raw_database, dict) or not all(
            isinstance(key, str) for key in raw_database
        ):
            issue = ValidationIssue(
                code="internal_state_shape_invalid",
                message="validated service database is not an object",
                source=document.source,
                field_path=("spec", "postgresql", "databases", position),
            )
            raise ConfigValidationError(issue)
        database = cast("Mapping[str, object]", raw_database)
        name = database.get("name")
        if name in seen_names:
            issue = ValidationIssue(
                code="service_database_duplicate",
                message=f"duplicate service database name: {name}",
                source=document.source,
                field_path=("spec", "postgresql", "databases", position, "name"),
            )
            raise ConfigValidationError(issue)
        seen_names.add(name)
        databases.append((position, database))
    application_users: set[str] = set()
    for position, database in databases:
        if "application" not in database:
            continue
        application_id = _resource_id_value(database, "application", document.source)
        application = _require_resource(
            index,
            ResourceKind.APPLICATION,
            application_id,
            document.source,
            ("spec", "postgresql", "databases", position, "application"),
        )
        application_spec = _required_mapping(
            application.content, "spec", application.source
        )
        linux_user = application_spec.get("linuxUser")
        if isinstance(linux_user, str):
            application_users.add(linux_user)
    owners = _validate_service_database_owners(
        document, databases, frozenset(application_users)
    )
    if "remoteAccess" in postgresql:
        _validate_service_remote_access(
            document,
            spec,
            _required_mapping(postgresql, "remoteAccess", document.source),
            server,
            index,
            has_owner=bool(owners),
        )


_RESERVED_POSTGRES_ROLES = frozenset({"postgres", "alloy"})


def _validate_service_database_owners(
    document: ResourceDocument,
    databases: Sequence[tuple[int, Mapping[str, object]]],
    application_users: frozenset[str],
) -> tuple[str, ...]:
    """Refuse an owner role Cloudfall or an application on the service holds."""
    owners: list[str] = []
    for position, database in databases:
        owner = database.get("owner")
        if owner is None:
            continue
        if not isinstance(owner, str):
            issue = ValidationIssue(
                code="internal_state_shape_invalid",
                message="validated service database owner is not a string",
                source=document.source,
                field_path=("spec", "postgresql", "databases", position, "owner"),
            )
            raise ConfigValidationError(issue)
        if owner in _RESERVED_POSTGRES_ROLES or owner in application_users:
            holder = (
                "a role Cloudfall reserves"
                if owner in _RESERVED_POSTGRES_ROLES
                else "the Linux user of an Application with a database on "
                "this service"
            )
            issue = ValidationIssue(
                code="service_database_owner_reserved",
                message=f"database owner {owner!r} is {holder}",
                source=document.source,
                field_path=("spec", "postgresql", "databases", position, "owner"),
            )
            raise ConfigValidationError(issue)
        owners.append(owner)
    return tuple(owners)


def _validate_service_remote_access(  # noqa: PLR0913
    document: ResourceDocument,
    spec: Mapping[str, object],
    remote_access: Mapping[str, object],
    server: ResourceDocument,
    index: Mapping[ResourceKey, ResourceDocument],
    *,
    has_owner: bool,
) -> None:
    """Refuse a way in that serves no owner role or opens the port to all."""
    field_path: tuple[str | int, ...] = ("spec", "postgresql", "remoteAccess")
    if not has_owner:
        issue = ValidationIssue(
            code="service_remote_access_unused",
            message=(
                "remoteAccess needs a database with an owner: databases named "
                "by an application stay socket-only"
            ),
            source=document.source,
            field_path=field_path,
        )
        raise ConfigValidationError(issue)
    _validate_remote_access_listen_address(document, remote_access, field_path)
    server_spec = _required_mapping(server.content, "spec", server.source)
    server_type_id = _resource_id_value(server_spec, "serverType", server.source)
    server_type = _require_resource(
        index,
        ResourceKind.HOST_PROFILE,
        server_type_id,
        server.source,
        ("spec", "serverType"),
    )
    server_type_spec = _required_mapping(
        server_type.content, "spec", server_type.source
    )
    if "firewall" not in server_type_spec:
        issue = ValidationIssue(
            code="service_remote_access_unfirewalled",
            message=(
                f"ServerType/{server_type_id} of Server/{server.key.resource_id} "
                "declares no firewall to restrict remoteAccess to its sources"
            ),
            source=document.source,
            field_path=field_path,
        )
        raise ConfigValidationError(issue)
    bind = _required_mapping(spec, "bind", document.source)
    port = bind.get("port")
    firewall = _required_mapping(server_type_spec, "firewall", server_type.source)
    raw_rules = firewall.get("allowedInbound")
    rules = raw_rules if isinstance(raw_rules, list) else []
    for rule in rules:
        if (
            isinstance(rule, dict)
            and rule.get("port") == port
            and rule.get("protocol") == "tcp"
        ):
            issue = ValidationIssue(
                code="service_remote_access_port_open",
                message=(
                    f"ServerType/{server_type_id} already opens tcp port {port} "
                    "to every address; remoteAccess would not restrict it"
                ),
                source=document.source,
                field_path=field_path,
            )
            raise ConfigValidationError(issue)
    raw_sources = remote_access.get("allowedSources")
    sources = raw_sources if isinstance(raw_sources, list) else []
    for position, raw_source in enumerate(sources):
        source_path = (*field_path, "allowedSources", position)
        try:
            source = IpNetworkCidr.from_boundary(raw_source)
        except (TypeError, ValueError) as error:
            issue = ValidationIssue(
                code="service_remote_access_source_invalid",
                message=f"allowed source is not a canonical CIDR: {error}",
                source=document.source,
                field_path=source_path,
            )
            raise ConfigValidationError(issue) from error
        if source.is_unrestricted:
            issue = ValidationIssue(
                code="service_remote_access_open",
                message=(
                    f"allowed source {source.value} matches every address; "
                    "name the networks the outside app connects from"
                ),
                source=document.source,
                field_path=source_path,
            )
            raise ConfigValidationError(issue)


def _validate_remote_access_listen_address(
    document: ResourceDocument,
    remote_access: Mapping[str, object],
    field_path: tuple[str | int, ...],
) -> None:
    """Refuse a listen address that is a wildcard or not canonical."""
    listen_path = (*field_path, "listenAddress")
    try:
        listen_address = IpAddress.from_boundary(remote_access.get("listenAddress"))
    except (TypeError, ValueError) as error:
        issue = ValidationIssue(
            code="service_remote_access_listen_invalid",
            message=f"listen address is not one canonical IP address: {error}",
            source=document.source,
            field_path=listen_path,
        )
        raise ConfigValidationError(issue) from error
    if listen_address.is_unspecified:
        issue = ValidationIssue(
            code="service_remote_access_listen_invalid",
            message=(
                f"listen address {listen_address.value} is a wildcard; name one "
                "address of the server"
            ),
            source=document.source,
            field_path=listen_path,
        )
        raise ConfigValidationError(issue)


def _validate_service_uniqueness(documents: Sequence[ResourceDocument]) -> None:
    placements: dict[tuple[ResourceId, str], ResourceDocument] = {}
    ports: dict[tuple[ResourceId, int], ResourceDocument] = {}
    for document in documents:
        if document.key.kind is not ResourceKind.SERVICE:
            continue
        spec = _required_mapping(document.content, "spec", document.source)
        bind = _required_mapping(spec, "bind", document.source)
        server_id = _resource_id_value(spec, "server", document.source)
        raw_kind = spec.get("serviceKind")
        raw_port = bind.get("port")
        if not isinstance(raw_kind, str) or not isinstance(raw_port, int):
            issue = ValidationIssue(
                code="internal_state_shape_invalid",
                message="validated service kind or port has an invalid shape",
                source=document.source,
                field_path=("spec",),
            )
            raise ConfigValidationError(issue)
        placement = (server_id, raw_kind)
        if placement in placements:
            issue = ValidationIssue(
                code="service_placement_conflict",
                message=(
                    f"Server/{server_id} already runs a {raw_kind} service "
                    f"declared at {placements[placement].source.display()}"
                ),
                source=document.source,
                field_path=("spec", "server"),
            )
            raise ConfigValidationError(issue)
        placements[placement] = document
        listener = (server_id, raw_port)
        if listener in ports:
            issue = ValidationIssue(
                code="service_port_conflict",
                message=(
                    f"Server/{server_id} port {raw_port} already used by the "
                    f"service declared at {ports[listener].source.display()}"
                ),
                source=document.source,
                field_path=("spec", "bind", "port"),
            )
            raise ConfigValidationError(issue)
        ports[listener] = document


def _validate_logging_references(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    environment = _resource_id_value(spec, "environment", document.source)
    backend = _required_mapping(spec, "backend", document.source)
    backend_id = _resource_id_value(backend, "server", document.source)
    backend_server = _require_resource(
        index,
        ResourceKind.SERVER,
        backend_id,
        document.source,
        ("spec", "backend", "server"),
    )
    _require_server_environment(backend_server, environment, document.source)

    _validate_logging_secret_paths(spec, document.source)
    _validate_logging_ports(spec, document.source)

    collectors = _required_mapping(spec, "collectors", document.source)
    collector_ids = _resource_id_list(collectors, "servers", document.source)
    collector_set = frozenset(collector_ids)
    for server_id in collector_ids:
        server = _require_resource(
            index,
            ResourceKind.SERVER,
            server_id,
            document.source,
            ("spec", "collectors", "servers"),
        )
        _require_server_environment(server, environment, document.source)

    raw_files = collectors.get("files")
    if not isinstance(raw_files, list):
        issue = ValidationIssue(
            code="internal_state_shape_invalid",
            message="validated logging file sources are not a list",
            source=document.source,
            field_path=("spec", "collectors", "files"),
        )
        raise ConfigValidationError(issue)
    seen_file_ids: set[ResourceId] = set()
    for position, raw_file in enumerate(raw_files):
        if not isinstance(raw_file, dict) or not all(
            isinstance(key, str) for key in raw_file
        ):
            issue = ValidationIssue(
                code="internal_state_shape_invalid",
                message="validated logging file source is not an object",
                source=document.source,
                field_path=("spec", "collectors", "files", position),
            )
            raise ConfigValidationError(issue)
        file_source = cast("Mapping[str, object]", raw_file)
        file_id = _resource_id_value(file_source, "id", document.source)
        if file_id in seen_file_ids:
            issue = ValidationIssue(
                code="logging_file_source_duplicate",
                message=f"duplicate logging file source id: {file_id}",
                source=document.source,
                field_path=("spec", "collectors", "files", position, "id"),
            )
            raise ConfigValidationError(issue)
        seen_file_ids.add(file_id)
        source_servers = _resource_id_list(file_source, "servers", document.source)
        if not frozenset(source_servers).issubset(collector_set):
            issue = ValidationIssue(
                code="logging_file_server_invalid",
                message="file source servers must be declared collector servers",
                source=document.source,
                field_path=(
                    "spec",
                    "collectors",
                    "files",
                    position,
                    "servers",
                ),
            )
            raise ConfigValidationError(issue)
        _validate_logging_file_owner(
            file_source,
            source_servers,
            document.source,
            position,
            index,
        )


def _validate_logging_secret_paths(
    spec: Mapping[str, object], source: SourceLocation
) -> None:
    gateway = _required_mapping(spec, "gateway", source)
    gateway_tls = _required_mapping(gateway, "tls", source)
    grafana = _required_mapping(spec, "grafana", source)
    collectors = _required_mapping(spec, "collectors", source)
    client_tls = _required_mapping(collectors, "clientTls", source)
    fields = (
        (gateway_tls, "certificatePath"),
        (gateway_tls, "keyPath"),
        (gateway_tls, "clientCaPath"),
        (grafana, "adminPasswordPath"),
        (client_tls, "caPath"),
        (client_tls, "certificatePath"),
        (client_tls, "keyPath"),
    )
    secret_root = PurePosixPath("/etc/cloudfall/logging")
    seen: set[PurePosixPath] = set()
    for content, field_name in fields:
        raw_path = content.get(field_name)
        if not isinstance(raw_path, str):
            issue = ValidationIssue(
                code="internal_state_shape_invalid",
                message=f"validated logging path {field_name!r} is not a string",
                source=source,
                field_path=("spec", field_name),
            )
            raise ConfigValidationError(issue)
        path = PurePosixPath(raw_path)
        if path == secret_root or not path.is_relative_to(secret_root):
            issue = ValidationIssue(
                code="logging_secret_path_invalid",
                message=f"logging secret paths must be below {secret_root}: {path}",
                source=source,
                field_path=("spec", field_name),
            )
            raise ConfigValidationError(issue)
        if path in seen:
            issue = ValidationIssue(
                code="logging_secret_path_duplicate",
                message=f"logging secret paths must be distinct: {path}",
                source=source,
                field_path=("spec", field_name),
            )
            raise ConfigValidationError(issue)
        seen.add(path)


def _validate_logging_ports(
    spec: Mapping[str, object], source: SourceLocation
) -> None:
    backend = _required_mapping(spec, "backend", source)
    gateway = _required_mapping(spec, "gateway", source)
    grafana = _required_mapping(spec, "grafana", source)
    metrics = _required_mapping(spec, "metrics", source)
    metrics_backend = _required_mapping(metrics, "backend", source)
    listeners = (
        ("backend", backend.get("port")),
        ("gateway", gateway.get("port")),
        ("grafana", grafana.get("port")),
        ("metrics.backend", metrics_backend.get("port")),
    )
    seen: dict[int, str] = {}
    for name, raw_port in listeners:
        if isinstance(raw_port, bool) or not isinstance(raw_port, int):
            issue = ValidationIssue(
                code="internal_state_shape_invalid",
                message=f"validated logging {name} port is not an integer",
                source=source,
                field_path=("spec", name, "port"),
            )
            raise ConfigValidationError(issue)
        if raw_port in seen:
            issue = ValidationIssue(
                code="logging_port_conflict",
                message=(
                    f"logging {name} port {raw_port} conflicts with "
                    f"{seen[raw_port]}"
                ),
                source=source,
                field_path=("spec", name, "port"),
            )
            raise ConfigValidationError(issue)
        seen[raw_port] = name


def _require_server_environment(
    server: ResourceDocument,
    environment: ResourceId,
    source: SourceLocation,
) -> None:
    server_spec = _required_mapping(server.content, "spec", server.source)
    server_environment = _resource_id_value(server_spec, "environment", server.source)
    if server_environment != environment:
        issue = ValidationIssue(
            code="logging_server_environment_mismatch",
            message=(
                f"Server/{server.key.resource_id} belongs to {server_environment}, "
                f"not {environment}"
            ),
            source=source,
            field_path=("spec", "environment"),
        )
        raise ConfigValidationError(issue)


def _validate_logging_file_owner(
    file_source: Mapping[str, object],
    source_servers: tuple[ResourceId, ...],
    source: SourceLocation,
    position: int,
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    raw_application = file_source.get("application")
    raw_component = file_source.get("component")
    if raw_application is None:
        return
    application_id = ResourceId.from_boundary(raw_application)
    _require_resource(
        index,
        ResourceKind.APPLICATION,
        application_id,
        source,
        ("spec", "collectors", "files", position, "application"),
    )
    if raw_component is None:
        return
    component_id = ResourceId.from_boundary(raw_component)
    component = _require_resource(
        index,
        ResourceKind.COMPONENT,
        component_id,
        source,
        ("spec", "collectors", "files", position, "component"),
    )
    component_spec = _required_mapping(component.content, "spec", component.source)
    owner_id = _resource_id_value(component_spec, "application", component.source)
    if owner_id != application_id:
        issue = ValidationIssue(
            code="logging_file_owner_mismatch",
            message=(
                f"Component/{component_id} belongs to Application/{owner_id}, "
                f"not Application/{application_id}"
            ),
            source=source,
            field_path=("spec", "collectors", "files", position),
        )
        raise ConfigValidationError(issue)
    deployment = _required_mapping(component_spec, "deployment", component.source)
    component_servers = frozenset(
        _resource_id_list(deployment, "servers", component.source)
    )
    if not frozenset(source_servers).issubset(component_servers):
        issue = ValidationIssue(
            code="logging_file_component_placement_mismatch",
            message=(
                f"file source servers must be deployment targets of "
                f"Component/{component_id}"
            ),
            source=source,
            field_path=("spec", "collectors", "files", position, "servers"),
        )
        raise ConfigValidationError(issue)


def _validate_logging_uniqueness(
    documents: Sequence[ResourceDocument],
) -> None:
    environments: dict[ResourceId, ResourceDocument] = {}
    backend_servers: dict[ResourceId, ResourceDocument] = {}
    for document in documents:
        if document.key.kind is not ResourceKind.LOGGING_STACK:
            continue
        spec = _required_mapping(document.content, "spec", document.source)
        environment = _resource_id_value(spec, "environment", document.source)
        backend = _required_mapping(spec, "backend", document.source)
        backend_id = _resource_id_value(backend, "server", document.source)
        for identity, seen, code, concept in (
            (
                environment,
                environments,
                "logging_environment_duplicate",
                "environment",
            ),
            (
                backend_id,
                backend_servers,
                "logging_backend_duplicate",
                "backend server",
            ),
        ):
            previous = seen.get(identity)
            if previous is not None:
                issue = ValidationIssue(
                    code=code,
                    message=(
                        f"LoggingStack/{previous.key.resource_id} already owns "
                        f"{concept} {identity}"
                    ),
                    source=document.source,
                    field_path=("spec",),
                )
                raise ConfigValidationError(issue)
            seen[identity] = document


def _validate_server_type(spec: Mapping[str, object], source: SourceLocation) -> None:
    storage = _required_mapping(spec, "storage", source)
    packages = _required_mapping(spec, "packages", source)
    services = _required_mapping(spec, "services", source)
    configuration = _required_mapping(spec, "configuration", source)
    _validate_unique_entry_keys(
        storage,
        "mounts",
        "path",
        source,
        ("spec", "storage", "mounts"),
    )
    _validate_unique_entry_keys(
        packages,
        "required",
        "name",
        source,
        ("spec", "packages", "required"),
    )
    _validate_unique_entry_keys(
        services,
        "required",
        "name",
        source,
        ("spec", "services", "required"),
    )
    configuration_items = _validate_unique_entry_keys(
        configuration,
        "files",
        "path",
        source,
        ("spec", "configuration", "files"),
    )
    for index, item in enumerate(configuration_items):
        if item.get("sha256") is not None and item.get("capture") != "hash":
            issue = ValidationIssue(
                code="configuration_capture_invalid",
                message="sha256 requires configuration capture mode 'hash'",
                source=source,
                field_path=("spec", "configuration", "files", index),
            )
            raise ConfigValidationError(issue)
    _validate_server_type_firewall(spec, source)


def _validate_server_type_firewall(
    spec: Mapping[str, object], source: SourceLocation
) -> None:
    if spec.get("firewall") is None:
        return
    firewall = _required_mapping(spec, "firewall", source)
    raw_rules = firewall.get("allowedInbound")
    if not isinstance(raw_rules, list):
        issue = ValidationIssue(
            code="internal_state_shape_invalid",
            message="validated firewall allowedInbound is not a list",
            source=source,
            field_path=("spec", "firewall", "allowedInbound"),
        )
        raise ConfigValidationError(issue)
    seen: set[tuple[object, object]] = set()
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict) or not all(
            isinstance(key, str) for key in raw_rule
        ):
            issue = ValidationIssue(
                code="internal_state_shape_invalid",
                message="validated firewall rule is not an object",
                source=source,
                field_path=("spec", "firewall", "allowedInbound", index),
            )
            raise ConfigValidationError(issue)
        rule = cast("Mapping[str, object]", raw_rule)
        identity = (rule.get("port"), rule.get("protocol"))
        if identity in seen:
            issue = ValidationIssue(
                code="firewall_rule_duplicate",
                message=(
                    "duplicate firewall rule: "
                    f"{rule.get('port')}/{rule.get('protocol')}"
                ),
                source=source,
                field_path=("spec", "firewall", "allowedInbound", index),
            )
            raise ConfigValidationError(issue)
        seen.add(identity)


def _validate_server_network(
    spec: Mapping[str, object], source: SourceLocation
) -> None:
    raw_network = spec.get("network")
    if raw_network is None:
        return
    network = _required_mapping(spec, "network", source)
    raw_ipv4 = network.get("ipv4")
    raw_ipv6 = network.get("ipv6Cidr")
    if not isinstance(raw_ipv4, str) or not isinstance(raw_ipv6, str):
        issue = ValidationIssue(
            code="internal_state_shape_invalid",
            message="validated server network addresses are not strings",
            source=source,
            field_path=("spec", "network"),
        )
        raise ConfigValidationError(issue)
    try:
        ipv4 = IPv4Address(raw_ipv4)
        ipv6 = IPv6Network(raw_ipv6, strict=True)
    except ValueError as error:
        issue = ValidationIssue(
            code="server_network_invalid",
            message=str(error),
            source=source,
            field_path=("spec", "network"),
        )
        raise ConfigValidationError(issue) from error
    if str(ipv4) != raw_ipv4 or str(ipv6) != raw_ipv6:
        issue = ValidationIssue(
            code="server_network_not_canonical",
            message="server IPv4 and IPv6 network must use canonical notation",
            source=source,
            field_path=("spec", "network"),
        )
        raise ConfigValidationError(issue)


def _validate_unique_entry_keys(
    content: Mapping[str, object],
    list_key: str,
    entry_key: str,
    source: SourceLocation,
    field_path: tuple[str | int, ...],
) -> tuple[Mapping[str, object], ...]:
    value = content.get(list_key)
    if not isinstance(value, list):
        issue = ValidationIssue(
            code="internal_state_shape_invalid",
            message=f"validated field {list_key!r} is not a list",
            source=source,
            field_path=field_path,
        )
        raise ConfigValidationError(issue)
    items: list[Mapping[str, object]] = []
    seen: set[object] = set()
    for index, raw_item in enumerate(value):
        if not isinstance(raw_item, dict) or not all(
            isinstance(key, str) for key in raw_item
        ):
            issue = ValidationIssue(
                code="internal_state_shape_invalid",
                message=f"validated {list_key!r} entry is not an object",
                source=source,
                field_path=(*field_path, index),
            )
            raise ConfigValidationError(issue)
        item = cast("Mapping[str, object]", raw_item)
        identity = item.get(entry_key)
        if identity in seen:
            issue = ValidationIssue(
                code="server_type_entry_duplicate",
                message=f"duplicate {entry_key} in server type: {identity}",
                source=source,
                field_path=(*field_path, index, entry_key),
            )
            raise ConfigValidationError(issue)
        seen.add(identity)
        items.append(item)
    return tuple(items)


def _required_mapping(
    content: Mapping[str, object], key: str, source: SourceLocation
) -> Mapping[str, object]:
    value = content.get(key)
    if not isinstance(value, dict) or not all(
        isinstance(child_key, str) for child_key in value
    ):
        issue = ValidationIssue(
            code="internal_state_shape_invalid",
            message=f"validated field {key!r} is not an object",
            source=source,
            field_path=(key,),
        )
        raise ConfigValidationError(issue)
    return cast("Mapping[str, object]", value)


def _resource_id_value(
    content: Mapping[str, object], key: str, source: SourceLocation
) -> ResourceId:
    try:
        return ResourceId.from_boundary(content.get(key))
    except (TypeError, ValueError) as error:
        issue = ValidationIssue(
            code="internal_resource_id_invalid",
            message=str(error),
            source=source,
            field_path=(key,),
        )
        raise ConfigValidationError(issue) from error


def _resource_id_list(
    content: Mapping[str, object], key: str, source: SourceLocation
) -> tuple[ResourceId, ...]:
    value = content.get(key)
    if not isinstance(value, list):
        issue = ValidationIssue(
            code="internal_state_shape_invalid",
            message=f"validated field {key!r} is not a list",
            source=source,
            field_path=(key,),
        )
        raise ConfigValidationError(issue)
    try:
        return tuple(ResourceId.from_boundary(item) for item in value)
    except (TypeError, ValueError) as error:
        issue = ValidationIssue(
            code="internal_resource_id_invalid",
            message=str(error),
            source=source,
            field_path=(key,),
        )
        raise ConfigValidationError(issue) from error


def _require_resource(
    index: Mapping[ResourceKey, ResourceDocument],
    kind: ResourceKind,
    resource_id: ResourceId,
    source: SourceLocation,
    field_path: tuple[str | int, ...],
) -> ResourceDocument:
    key = ResourceKey(kind=kind, resource_id=resource_id)
    try:
        return index[key]
    except KeyError as error:
        issue = ValidationIssue(
            code="resource_reference_missing",
            message=f"referenced resource does not exist: {kind.value}/{resource_id}",
            source=source,
            field_path=field_path,
        )
        raise ConfigValidationError(issue) from error


def _validate_install_root(
    deployment: Mapping[str, object],
    application_id: ResourceId,
    source: SourceLocation,
) -> None:
    raw_install_root = deployment.get("installRoot")
    if not isinstance(raw_install_root, str):
        issue = ValidationIssue(
            code="internal_state_shape_invalid",
            message="validated installRoot is not a string",
            source=source,
            field_path=("spec", "deployment", "installRoot"),
        )
        raise ConfigValidationError(issue)
    install_root = PurePosixPath(raw_install_root)
    application_root = PurePosixPath("/srv/apps") / application_id.value
    if install_root == application_root or not install_root.is_relative_to(
        application_root
    ):
        issue = ValidationIssue(
            code="component_install_root_invalid",
            message=(
                f"component installRoot must be below "
                f"{application_root}: {install_root}"
            ),
            source=source,
            field_path=("spec", "deployment", "installRoot"),
        )
        raise ConfigValidationError(issue)


def _validate_alert_rule_references(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    environment = _resource_id_value(spec, "environment", document.source)
    stack_environments = set()
    for key, stack in index.items():
        if key.kind is not ResourceKind.LOGGING_STACK:
            continue
        stack_spec = _required_mapping(stack.content, "spec", stack.source)
        stack_environments.add(
            _resource_id_value(stack_spec, "environment", stack.source)
        )
    if environment not in stack_environments:
        issue = ValidationIssue(
            code="alert_rule_environment_unmonitored",
            message=(
                f"no LoggingStack monitors environment {environment}; "
                "an alert rule without a metrics backend can never fire"
            ),
            source=document.source,
            field_path=("spec", "environment"),
        )
        raise ConfigValidationError(issue)


def _validate_operator_policy_references(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    environment = _resource_id_value(spec, "environment", document.source)
    stack_environments = set()
    for key, stack in index.items():
        if key.kind is not ResourceKind.LOGGING_STACK:
            continue
        stack_spec = _required_mapping(stack.content, "spec", stack.source)
        stack_environments.add(
            _resource_id_value(stack_spec, "environment", stack.source)
        )
    if environment not in stack_environments:
        issue = ValidationIssue(
            code="operator_policy_environment_unmonitored",
            message=(
                f"no LoggingStack monitors environment {environment}; "
                "autonomy without alert evidence is not grantable"
            ),
            source=document.source,
            field_path=("spec", "environment"),
        )
        raise ConfigValidationError(issue)
    duplicate = next(
        (
            other
            for key, other in index.items()
            if key.kind is ResourceKind.OPERATOR_POLICY
            and other is not document
            and _resource_id_value(
                _required_mapping(other.content, "spec", other.source),
                "environment",
                other.source,
            )
            == environment
        ),
        None,
    )
    if duplicate is not None:
        issue = ValidationIssue(
            code="operator_policy_environment_duplicate",
            message=(
                f"environment {environment} already has an operator policy "
                f"at {duplicate.source.display()}"
            ),
            source=document.source,
            field_path=("spec", "environment"),
        )
        raise ConfigValidationError(issue)


def validate_config(project_directory: Path, schema_directory: Path) -> ValidatedConfig:
    """Load and validate a directory of Cloudfall YAML resources."""
    catalog = SchemaCatalog(schema_directory)
    return StateValidator(catalog).validate_directory(project_directory)

_REFERENCE_VALIDATORS: Mapping[
    ResourceKind,
    Callable[
        [
            ResourceDocument,
            Mapping[str, object],
            Mapping[ResourceKey, ResourceDocument],
        ],
        None,
    ],
] = {
    ResourceKind.SERVER: _validate_server_references,
    ResourceKind.HOST_PROFILE: _validate_server_type_references,
    ResourceKind.APPLICATION: _validate_application_references,
    ResourceKind.COMPONENT: _validate_component_references,
    ResourceKind.DOMAIN: _validate_domain_references,
    ResourceKind.LOGGING_STACK: _validate_logging_references,
    ResourceKind.SSH_PUBLIC_KEY: _validate_ssh_public_key_references,
    ResourceKind.SERVICE: _validate_service_references,
    ResourceKind.ALERT_RULE: _validate_alert_rule_references,
    ResourceKind.OPERATOR_POLICY: _validate_operator_policy_references,
}
