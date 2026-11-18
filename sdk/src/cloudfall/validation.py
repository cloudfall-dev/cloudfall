"""Load and strictly validate declarative Cloudfall state."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Network, ip_address, ip_network
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from datetime import date
    from pathlib import Path

import yaml
from cryptography import x509
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry, Resource

from cloudfall.domain import (
    ConnectionAddress,
    DomainRoutePath,
    IpAddress,
    IpNetworkCidr,
    OpenSshPublicKey,
    RawDocument,
    ResourceDocument,
    ResourceId,
    ResourceKey,
    ResourceKind,
    RuntimePackageManager,
    RuntimeType,
    ServiceKind,
    SourceLocation,
    SshPublicKeyLifecycle,
    TelegramChatId,
)
from cloudfall.elasticsearch import (
    RESERVED_USER_NAMES,
    SUPPORTED_LINES,
    ElasticsearchIndexPattern,
    ElasticsearchLine,
    ElasticsearchSeedHost,
    ElasticsearchVersion,
    ProjectFilePath,
    supported_line,
    supported_lines_text,
)
from cloudfall.mariadb import RESERVED_USER_NAMES as RESERVED_MARIADB_USERS
from cloudfall.mariadb import SUPPORTED_SERIES as MARIADB_SUPPORTED_SERIES
from cloudfall.mariadb import SYSTEM_DATABASES as MARIADB_SYSTEM_DATABASES
from cloudfall.mariadb import (
    MariadbReplicationRole,
    MariadbSeries,
    MariadbVersion,
)
from cloudfall.mariadb import account_host as mariadb_account_host
from cloudfall.mariadb import supported_series as mariadb_series
from cloudfall.mariadb import supported_series_text as mariadb_series_text
from cloudfall.rabbitmq import DEFAULT_VHOST as RABBITMQ_DEFAULT_VHOST
from cloudfall.rabbitmq import NODE_COUNTS as RABBITMQ_NODE_COUNTS
from cloudfall.rabbitmq import PRODUCTION_ENVIRONMENT as RABBITMQ_PRODUCTION
from cloudfall.rabbitmq import SUPPORTED_SERIES as RABBITMQ_SUPPORTED_SERIES
from cloudfall.rabbitmq import WARNING_SINGLE_NODE as RABBITMQ_WARNING_SINGLE_NODE
from cloudfall.rabbitmq import (
    WARNING_VERSION_ENDING as RABBITMQ_WARNING_VERSION_ENDING,
)
from cloudfall.rabbitmq import (
    RabbitmqSeries,
    RabbitmqUserName,
    RabbitmqVersion,
    RabbitmqVhostName,
)
from cloudfall.rabbitmq import supported_series as rabbitmq_series
from cloudfall.rabbitmq import supported_series_text as rabbitmq_series_text

_SCHEMA_FILES: Mapping[ResourceKind, str] = {
    ResourceKind.SERVER: "server.schema.json",
    ResourceKind.BASELINE: "baseline.schema.json",
    ResourceKind.APPLICATION: "application.schema.json",
    ResourceKind.COMPONENT: "component.schema.json",
    ResourceKind.DOMAIN: "domain.schema.json",
    ResourceKind.SSH_PUBLIC_KEY: "ssh-public-key.schema.json",
    ResourceKind.LOGGING_STACK: "logging-stack.schema.json",
    ResourceKind.SERVICE: "service.schema.json",
    ResourceKind.ALERT_RULE: "alert-rule.schema.json",
    ResourceKind.ALERT_CHANNEL: "alert-channel.schema.json",
    ResourceKind.OPERATOR_POLICY: "operator-policy.schema.json",
}
_YAML_SUFFIXES = frozenset({".yaml", ".yml"})

# A Baseline was a ServerType before 0.8.0 (D-11). The old kind, folder and
# Server field are read for one minor release, each use recorded as a
# DeprecatedName, and `cloudfall config upgrade` rewrites them.
LEGACY_BASELINE_KIND = "ServerType"
"""Deprecated spelling of the ``Baseline`` kind."""
LEGACY_BASELINE_DIRECTORY = "server-types"
"""Deprecated folder of ``Baseline`` resources."""
LEGACY_BASELINE_FIELD = "serverType"
"""Deprecated name of ``Server.spec.baseline``."""
DEPRECATED_NAMES_REMOVED_IN: Mapping[str, str] = {
    LEGACY_BASELINE_KIND: "0.9.0",
    LEGACY_BASELINE_DIRECTORY: "0.9.0",
    LEGACY_BASELINE_FIELD: "0.9.0",
}
"""The release that stops reading each deprecated name above (S-017-13)."""
BASELINE_FIELD = "baseline"
"""The ``Server`` spec field naming the server's ``Baseline``."""
UPGRADE_COMMAND = "cloudfall config upgrade"
"""The command that rewrites a project's deprecated names."""
ERROR_BASELINE_CONFLICT = "server_baseline_conflict"
_LEGACY_DIRECTORIES: Mapping[str, ResourceKind] = {
    LEGACY_BASELINE_DIRECTORY: ResourceKind.BASELINE,
}


@dataclass(frozen=True, slots=True)
class DeprecatedName:
    """One deprecated name a resource file still uses, and its replacement."""

    path: Path
    old: str
    new: str
# The schema enums keep node, pm2, npm, pnpm and yarn as reserved values; the
# deploy role runs only these (S-002-14).
_DEPLOYABLE_RUNTIME: Mapping[str, str] = {
    "type": RuntimeType.PYTHON.value,
    "packageManager": RuntimePackageManager.UV.value,
}


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
    deprecations: tuple[DeprecatedName, ...] = ()
    """Deprecated names the documents were read under, in reading order."""

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

    def reported_counts_by_kind(self) -> dict[str, int]:
        """Return the counts a command answers with, deprecated kinds included.

        ``ServerType`` repeats the ``Baseline`` count until 0.9.0 (D-11), so
        a caller reading ``by_kind.ServerType`` keeps working.
        """
        counts = self.counts_by_kind()
        if ResourceKind.BASELINE.value in counts:
            counts[LEGACY_BASELINE_KIND] = counts[ResourceKind.BASELINE.value]
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
            "byKind": self.reported_counts_by_kind(),
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


def _resource_files(
    project_directory: Path,
) -> Iterable[tuple[ResourceKind, str, Path]]:
    """Yield each kind directory's YAML files in deterministic order.

    Resources live in their kind's directory (``servers/``, ``components/``,
    ...), or in a kind's deprecated directory (``server-types/``). Nothing
    else in a project is read as a resource: not ``tmp/``, not playbooks,
    not secrets, not tooling files.
    """
    directories = [(kind, kind.directory) for kind in ResourceKind]
    directories += [(kind, name) for name, kind in _LEGACY_DIRECTORIES.items()]
    for kind, name in directories:
        kind_directory = project_directory / name
        if not kind_directory.is_dir():
            continue
        for path in sorted(kind_directory.rglob("*")):
            if path.is_file() and path.suffix.lower() in _YAML_SUFFIXES:
                yield kind, name, path


def _current_names(
    content: Mapping[str, object],
    source: SourceLocation,
    deprecations: list[DeprecatedName],
) -> Mapping[str, object]:
    """Return ``content`` under the current names, recording each old one."""
    if content.get("kind") != LEGACY_BASELINE_KIND and not (
        content.get("kind") == ResourceKind.SERVER.value
        and isinstance(content.get("spec"), dict)
        and LEGACY_BASELINE_FIELD in cast("Mapping[str, object]", content["spec"])
    ):
        return content
    current = dict(content)
    if current.get("kind") == LEGACY_BASELINE_KIND:
        current["kind"] = ResourceKind.BASELINE.value
        deprecations.append(
            DeprecatedName(
                source.path,
                f"kind: {LEGACY_BASELINE_KIND}",
                f"kind: {ResourceKind.BASELINE.value}",
            )
        )
        return current
    spec = cast("Mapping[str, object]", current["spec"])
    if BASELINE_FIELD in spec:
        issue = ValidationIssue(
            code=ERROR_BASELINE_CONFLICT,
            message=(
                f"spec sets both {BASELINE_FIELD} and {LEGACY_BASELINE_FIELD}, "
                f"its deprecated name; keep {BASELINE_FIELD} only"
            ),
            source=source,
            field_path=("spec", LEGACY_BASELINE_FIELD),
        )
        raise ConfigValidationError(issue)
    current["spec"] = {
        (BASELINE_FIELD if key == LEGACY_BASELINE_FIELD else key): value
        for key, value in spec.items()
    }
    deprecations.append(
        DeprecatedName(
            source.path,
            f"spec.{LEGACY_BASELINE_FIELD}",
            f"spec.{BASELINE_FIELD}",
        )
    )
    return current


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
        deprecations: list[DeprecatedName] = []
        for kind, directory, path in paths:
            if directory != kind.directory:
                deprecations.append(
                    DeprecatedName(path, f"{directory}/", f"{kind.directory}/")
                )
            for document in self._load_file(path, deprecations):
                if document.key.kind is not kind:
                    issue = ValidationIssue(
                        code="resource_misplaced",
                        message=(
                            f"{document.key.kind.value}/{document.key.resource_id} "
                            f"belongs in {document.key.kind.directory}/, "
                            f"not {directory}/"
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

        self._validate_references(documents, index, project_directory)
        return ValidatedConfig(
            documents=tuple(documents),
            _index=index,
            deprecations=tuple(deprecations),
        )

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
        deprecations: list[DeprecatedName] = []
        for raw in raw_documents:
            document = self._validate_document(raw.content, raw.source, deprecations)
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

        self._validate_references(documents, index, None)
        return ValidatedConfig(
            documents=tuple(documents),
            _index=index,
            deprecations=tuple(deprecations),
        )

    def _load_file(
        self, path: Path, deprecations: list[DeprecatedName]
    ) -> Iterable[ResourceDocument]:
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
            yield self._validate_document(content, source, deprecations)

    def _validate_document(
        self,
        content: Mapping[str, object],
        source: SourceLocation,
        deprecations: list[DeprecatedName],
    ) -> ResourceDocument:
        content = _current_names(content, source, deprecations)
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
        project_directory: Path | None,
    ) -> None:
        for document in documents:
            spec = _required_mapping(document.content, "spec", document.source)
            _REFERENCE_VALIDATORS[document.key.kind](document, spec, index)
        _validate_logging_uniqueness(documents)
        _validate_service_uniqueness(documents)
        _validate_elasticsearch_files(documents, project_directory)
        _validate_mariadb_project(documents, project_directory)


def _validate_server_references(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    baseline_id = _resource_id_value(spec, "baseline", document.source)
    _require_resource(
        index,
        ResourceKind.BASELINE,
        baseline_id,
        document.source,
        ("spec", "baseline"),
    )
    _validate_server_network(spec, document.source)


def _validate_baseline_references(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    _validate_baseline(spec, document.source)
    alerting = spec.get("alerting")
    if isinstance(alerting, dict):
        _require_alert_channel(
            index,
            alerting.get("channel"),
            document.source,
            ("spec", "alerting", "channel"),
        )


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
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    _validate_ssh_public_key(document, spec)
    _validate_ssh_public_key_lifecycle(document, index)


def _ssh_key_declaration(
    document: ResourceDocument,
) -> tuple[tuple[str, str, str], str]:
    """Return a key's identity (environment, type, base64 body) and lifecycle.

    The comment after the body is not part of the key.
    """
    spec = _required_mapping(document.content, "spec", document.source)
    environment = spec.get("environment")
    public_key = spec.get("publicKey")
    lifecycle = spec.get("lifecycle")
    if (
        not isinstance(environment, str)
        or not isinstance(public_key, str)
        or not isinstance(lifecycle, str)
    ):
        issue = ValidationIssue(
            code="internal_state_shape_invalid",
            message="validated SSH public key has an invalid shape",
            source=document.source,
            field_path=("spec",),
        )
        raise ConfigValidationError(issue)
    algorithm, body = public_key.split()[:2]
    return (environment, algorithm, body), lifecycle


def _validate_ssh_public_key_lifecycle(
    document: ResourceDocument,
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    """Refuse one key declared active and revoked in the same environment.

    The baseline would install it and remove it on every run (D-22).
    """
    identity, lifecycle = _ssh_key_declaration(document)
    conflict = next(
        (
            other
            for key, other in index.items()
            if key.kind is ResourceKind.SSH_PUBLIC_KEY
            and other is not document
            and _ssh_key_declaration(other) == (identity, _other_lifecycle(lifecycle))
        ),
        None,
    )
    if conflict is not None:
        issue = ValidationIssue(
            code="ssh_public_key_lifecycle_conflict",
            message=(
                f"this SSH public key is declared {lifecycle} here and "
                f"{_other_lifecycle(lifecycle)} at {conflict.source.display()}; "
                "declare it once"
            ),
            source=document.source,
            field_path=("spec", "lifecycle"),
        )
        raise ConfigValidationError(issue)


def _other_lifecycle(lifecycle: str) -> str:
    other = {
        SshPublicKeyLifecycle.ACTIVE.value: SshPublicKeyLifecycle.REVOKED.value,
        SshPublicKeyLifecycle.REVOKED.value: SshPublicKeyLifecycle.ACTIVE.value,
    }
    return other[lifecycle]


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
    _validate_component_runtime(spec, document.source)
    deployment = _required_mapping(spec, "deployment", document.source)
    _validate_install_root(deployment, application_id, document.source)
    _validate_component_build(spec, document.source)
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
    _validate_domain_routes(document, spec, proxy, index)
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


def _validate_domain_routes(
    document: ResourceDocument,
    spec: Mapping[str, object],
    proxy: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    """Check ``spec.routes``: one route source, safe unique paths, served components.

    The schema holds each route's shape, ``directory`` and ``spaFallback``; the
    path rules live here so each refusal names its own code.
    """
    if "routes" not in spec:
        return
    if "upstream" in proxy:
        issue = ValidationIssue(
            code="domain_route_source_invalid",
            message="a Domain declares proxy.upstream or routes, not both",
            source=document.source,
            field_path=("spec", "routes"),
        )
        raise ConfigValidationError(issue)
    proxy_server_id = _resource_id_value(proxy, "server", document.source)
    raw_routes = spec.get("routes")
    if not isinstance(raw_routes, list):
        issue = ValidationIssue(
            code="internal_state_shape_invalid",
            message="validated field 'routes' is not a list",
            source=document.source,
            field_path=("spec", "routes"),
        )
        raise ConfigValidationError(issue)
    seen: set[DomainRoutePath] = set()
    for position, raw_route in enumerate(raw_routes):
        route_field: tuple[str | int, ...] = ("spec", "routes", position)
        if not isinstance(raw_route, dict):
            issue = ValidationIssue(
                code="internal_state_shape_invalid",
                message="validated route is not an object",
                source=document.source,
                field_path=route_field,
            )
            raise ConfigValidationError(issue)
        route = cast("Mapping[str, object]", raw_route)
        path = _domain_route_path(route, document.source, (*route_field, "path"))
        if path in seen:
            issue = ValidationIssue(
                code="domain_route_path_duplicate",
                message=f"two routes declare the path {path.value!r}",
                source=document.source,
                field_path=(*route_field, "path"),
            )
            raise ConfigValidationError(issue)
        seen.add(path)
        if "static" in route:
            _validate_static_route(
                document,
                path,
                _required_mapping(route, "static", document.source),
                proxy_server_id,
                index,
                route_field=route_field,
            )


def _domain_route_path(
    route: Mapping[str, object],
    source: SourceLocation,
    field_path: tuple[str | int, ...],
) -> DomainRoutePath:
    try:
        path = DomainRoutePath.from_boundary(route.get("path"))
    except (TypeError, ValueError) as error:
        issue = ValidationIssue(
            code="domain_route_path_invalid",
            message=str(error),
            source=source,
            field_path=field_path,
        )
        raise ConfigValidationError(issue) from error
    if path.is_reserved:
        issue = ValidationIssue(
            code="domain_route_path_reserved",
            message=(
                f"route path {path.value!r} is under /.well-known/acme-challenge, "
                "which certificate issuance owns"
            ),
            source=source,
            field_path=field_path,
        )
        raise ConfigValidationError(issue)
    return path


def _validate_static_route(  # noqa: PLR0913
    document: ResourceDocument,
    path: DomainRoutePath,
    static: Mapping[str, object],
    proxy_server_id: ResourceId,
    index: Mapping[ResourceKey, ResourceDocument],
    *,
    route_field: tuple[str | int, ...],
) -> None:
    if not path.is_directory:
        issue = ValidationIssue(
            code="domain_route_path_invalid",
            message=(
                f"static route path {path.value!r} must end with '/', so nginx "
                "can't serve files from outside its directory"
            ),
            source=document.source,
            field_path=(*route_field, "path"),
        )
        raise ConfigValidationError(issue)
    component_field = (*route_field, "static", "component")
    component_id = _resource_id_value(static, "component", document.source)
    component = _require_resource(
        index,
        ResourceKind.COMPONENT,
        component_id,
        document.source,
        component_field,
    )
    component_spec = _required_mapping(component.content, "spec", component.source)
    deployment = _required_mapping(component_spec, "deployment", component.source)
    if proxy_server_id not in _resource_id_list(
        deployment, "servers", component.source
    ):
        issue = ValidationIssue(
            code="domain_route_component_not_on_proxy",
            message=(
                f"Component/{component_id} is not deployed on the proxy server "
                f"{proxy_server_id}, where nginx reads its files"
            ),
            source=document.source,
            field_path=component_field,
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
    elif spec.get("serviceKind") == "elasticsearch":
        _validate_elasticsearch_service(document, spec, index)
    elif spec.get("serviceKind") == ServiceKind.MARIADB.value:
        _validate_mariadb_service(document, spec, server, index)
    elif spec.get("serviceKind") == ServiceKind.RABBITMQ.value:
        _validate_rabbitmq_service(document, spec, index)


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
    _validate_restore_check_names(document, databases)
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
        _validate_remote_access_names(document, databases)


# PostgreSQL cuts a name at 63 bytes, so the restore drill keeps the first 49
# of a database name before the 14 of its scratch suffix.
_RESTORE_CHECK_SUFFIX = "_restore_check"
_RESTORE_CHECK_PREFIX_LENGTH = 63 - len(_RESTORE_CHECK_SUFFIX)


def _restore_check_scratch_name(database: str) -> str:
    """Name the scratch database the restore drill restores ``database`` into."""
    return database[:_RESTORE_CHECK_PREFIX_LENGTH] + _RESTORE_CHECK_SUFFIX


def _validate_restore_check_names(
    document: ResourceDocument,
    databases: Sequence[tuple[int, Mapping[str, object]]],
) -> None:
    """Refuse a database the restore drill would drop as its scratch database."""
    scratch_names = {
        _restore_check_scratch_name(name): name
        for _, database in databases
        if isinstance(name := database.get("name"), str)
    }
    for position, database in databases:
        name = database.get("name")
        if isinstance(name, str) and name in scratch_names:
            issue = ValidationIssue(
                code="service_database_restore_check_name",
                message=(
                    f"database name {name!r} is the scratch database the "
                    f"restore drill of {scratch_names[name]!r} drops"
                ),
                source=document.source,
                field_path=("spec", "postgresql", "databases", position, "name"),
            )
            raise ConfigValidationError(issue)


_RESERVED_POSTGRES_ROLES = frozenset({"postgres", "alloy"})
# pg_hba.conf reads these bare words as keywords, not names: a database named
# all would open every database to its owner, an owner named all every role.
_PG_HBA_KEYWORDS = frozenset(
    {"all", "sameuser", "samerole", "samegroup", "replication"}
)


def _validate_remote_access_names(
    document: ResourceDocument,
    databases: Sequence[tuple[int, Mapping[str, object]]],
) -> None:
    """Refuse an owned database or owner pg_hba.conf would read as a keyword."""
    for position, database in databases:
        if "owner" not in database:
            continue
        for field_name in ("name", "owner"):
            value = database.get(field_name)
            if value in _PG_HBA_KEYWORDS:
                issue = ValidationIssue(
                    code="service_remote_access_name_reserved",
                    message=(
                        f"database {field_name} {value!r} is a pg_hba.conf "
                        "keyword; remoteAccess cannot name it in a hostssl line"
                    ),
                    source=document.source,
                    field_path=(
                        "spec",
                        "postgresql",
                        "databases",
                        position,
                        field_name,
                    ),
                )
                raise ConfigValidationError(issue)


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
    _validate_remote_access_rules(
        document, spec, remote_access, server, index, field_path=field_path
    )


def _validate_remote_access_rules(  # noqa: PLR0913
    document: ResourceDocument,
    spec: Mapping[str, object],
    remote_access: Mapping[str, object],
    server: ResourceDocument,
    index: Mapping[ResourceKey, ResourceDocument],
    *,
    field_path: tuple[str | int, ...],
) -> None:
    """Refuse a way in on ``server`` that its firewall cannot narrow (S-006)."""
    _validate_remote_access_listen_address(document, remote_access, field_path)
    server_spec = _required_mapping(server.content, "spec", server.source)
    baseline_id = _resource_id_value(server_spec, "baseline", server.source)
    baseline = _require_resource(
        index,
        ResourceKind.BASELINE,
        baseline_id,
        server.source,
        ("spec", "baseline"),
    )
    baseline_spec = _required_mapping(
        baseline.content, "spec", baseline.source
    )
    if "firewall" not in baseline_spec:
        issue = ValidationIssue(
            code="service_remote_access_unfirewalled",
            message=(
                f"Baseline/{baseline_id} of Server/{server.key.resource_id} "
                "declares no firewall to restrict remoteAccess to its sources"
            ),
            source=document.source,
            field_path=field_path,
        )
        raise ConfigValidationError(issue)
    bind = _required_mapping(spec, "bind", document.source)
    port = bind.get("port")
    firewall = _required_mapping(baseline_spec, "firewall", baseline.source)
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
                    f"Baseline/{baseline_id} already opens tcp port {port} "
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


ERROR_ELASTICSEARCH_VERSION = "service_elasticsearch_version"
ERROR_ELASTICSEARCH_NODES = "service_elasticsearch_nodes"
ERROR_ELASTICSEARCH_EXISTING_CLUSTER = "service_elasticsearch_existing_cluster"
ERROR_ELASTICSEARCH_SERVER_SHARED = "service_elasticsearch_server_shared"
ERROR_ELASTICSEARCH_SNAPSHOT_LOCAL = "service_elasticsearch_snapshot_repository_local"
ERROR_ELASTICSEARCH_USER_RESERVED = "service_elasticsearch_user_reserved"
ERROR_ELASTICSEARCH_USER_DUPLICATE = "service_elasticsearch_user_duplicate"
ERROR_ELASTICSEARCH_PATTERN = "service_elasticsearch_pattern"
ERROR_ELASTICSEARCH_NODE_UNADDRESSED = "service_elasticsearch_node_unaddressed"
ERROR_ELASTICSEARCH_CLUSTER_FIREWALL_MISSING = (
    "service_elasticsearch_cluster_firewall_missing"
)
_ELASTICSEARCH_PATH = ("spec", "elasticsearch")
_IPV4 = 4


def _elasticsearch_refusal(
    document: ResourceDocument,
    code: str,
    message: str,
    *field: str | int,
) -> ConfigValidationError:
    return ConfigValidationError(
        ValidationIssue(
            code=code,
            message=message,
            source=document.source,
            field_path=(*_ELASTICSEARCH_PATH, *field),
        )
    )


def _elasticsearch_nodes(
    document: ResourceDocument, spec: Mapping[str, object]
) -> tuple[ResourceId, ...]:
    """Return the declared nodes, or the service's server when none are."""
    elasticsearch = _required_mapping(spec, "elasticsearch", document.source)
    server_id = _resource_id_value(spec, "server", document.source)
    if "nodes" not in elasticsearch:
        return (server_id,)
    raw_nodes = elasticsearch.get("nodes")
    if not isinstance(raw_nodes, list):
        issue = ValidationIssue(
            code="internal_state_shape_invalid",
            message="validated Elasticsearch nodes are not a list",
            source=document.source,
            field_path=(*_ELASTICSEARCH_PATH, "nodes"),
        )
        raise ConfigValidationError(issue)
    return tuple(ResourceId.from_boundary(node) for node in raw_nodes)


def _validate_elasticsearch_service(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    """Apply the S-012 refusals that read one service and the servers it names."""
    elasticsearch = _required_mapping(spec, "elasticsearch", document.source)
    _validate_elasticsearch_version(document, elasticsearch)
    nodes = _validate_elasticsearch_node_list(document, spec, index)
    _validate_elasticsearch_users(document, elasticsearch)
    if "existingCluster" in elasticsearch:
        _validate_elasticsearch_existing_cluster(
            document,
            elasticsearch,
            _required_mapping(elasticsearch, "existingCluster", document.source),
            nodes,
            index,
        )
    if "snapshotRepository" not in elasticsearch and (
        len(nodes) > 1 or "existingCluster" in elasticsearch
    ):
        reason = (
            "existingCluster" if "existingCluster" in elasticsearch else "two nodes"
        )
        raise _elasticsearch_refusal(
            document,
            ERROR_ELASTICSEARCH_SNAPSHOT_LOCAL,
            f"a cluster with {reason} needs a snapshotRepository: a filesystem "
            "repository would have to be one shared mount on every node, which "
            "Cloudfall does not provide",
        )
    _validate_elasticsearch_node_hosts(
        document, nodes, index, joins="existingCluster" in elasticsearch
    )
    if "remoteAccess" in elasticsearch:
        remote_access = _required_mapping(
            elasticsearch, "remoteAccess", document.source
        )
        for node in nodes:
            _validate_remote_access_rules(
                document,
                spec,
                remote_access,
                index[ResourceKey(kind=ResourceKind.SERVER, resource_id=node)],
                index,
                field_path=(*_ELASTICSEARCH_PATH, "remoteAccess"),
            )


def _validate_elasticsearch_node_hosts(
    document: ResourceDocument,
    nodes: tuple[ResourceId, ...],
    index: Mapping[ResourceKey, ResourceDocument],
    *,
    joins: bool,
) -> None:
    """Refuse a node whose server can't hold what the node needs (S-012).

    Each node's data volume goes into the only storage pool of its server's
    baseline, on two disks for a cluster of one, the only copy of its data.
    A node of a cluster of two or more, which a node that ``joins`` an
    existing cluster is, binds its transport to an IPv4 the other nodes reach
    and their firewalls admit.
    """
    single = len(nodes) == 1
    transported = not single or joins
    for position, node in enumerate(nodes):
        server = index[ResourceKey(kind=ResourceKind.SERVER, resource_id=node)]
        server_spec = _required_mapping(server.content, "spec", server.source)
        baseline_id = _resource_id_value(server_spec, "baseline", server.source)
        baseline = _require_resource(
            index,
            ResourceKind.BASELINE,
            baseline_id,
            server.source,
            ("spec", "baseline"),
        )
        baseline_spec = _required_mapping(baseline.content, "spec", baseline.source)
        storage = _required_mapping(baseline_spec, "storage", baseline.source)
        raw_pools = storage.get("pools")
        pools = [
            pool
            for pool in (raw_pools if isinstance(raw_pools, list) else [])
            if isinstance(pool, dict)
        ]
        if len(pools) != 1:
            code = (
                "volume_requirement_pool_missing"
                if not pools
                else "volume_requirement_pool_ambiguous"
            )
            reason = (
                f"Baseline/{baseline_id} declares no storage pool"
                if not pools
                else f"Baseline/{baseline_id} declares {len(pools)} pools, and the "
                "node's volume goes into the only one"
            )
            raise _elasticsearch_refusal(
                document,
                code,
                f"the elasticsearch volume of node Server/{node} has nowhere to "
                f"live: {reason}",
                "nodes",
                position,
            )
        if single and "secondary" not in pools[0]:
            raise _elasticsearch_refusal(
                document,
                "volume_requirement_pool_unmirrored",
                f"the elasticsearch volume of Server/{node} is mirrored, the only "
                f"copy of a cluster of one, but pool {pools[0].get('id')} of "
                f"Baseline/{baseline_id} has no secondary physical volume; add "
                "one, or a second node",
                "nodes",
                position,
            )
        if transported and not (
            "network" in server_spec or _is_ipv4(server_spec.get("address"))
        ):
            raise _elasticsearch_refusal(
                document,
                ERROR_ELASTICSEARCH_NODE_UNADDRESSED,
                f"node Server/{node} has a name for its address and declares no "
                "network, so the other nodes have no IPv4 to reach its transport "
                "at and their firewalls none to admit; give it an IPv4 address "
                "or a network block",
                "nodes",
                position,
            )
        if transported and "firewall" not in baseline_spec:
            # The S-006 rule remoteAccess follows: a port opened to some
            # sources needs a firewall to keep everyone else out
            raise _elasticsearch_refusal(
                document,
                ERROR_ELASTICSEARCH_CLUSTER_FIREWALL_MISSING,
                f"node Server/{node} opens its transport (tcp 9300) to the "
                f"other nodes only, but Baseline/{baseline_id} declares no "
                "firewall to keep every other address out; declare a firewall "
                "on the baseline",
                "nodes",
                position,
            )


def _is_ipv4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return ip_address(value).version == _IPV4
    except ValueError:
        return False


def _validate_elasticsearch_version(
    document: ResourceDocument, elasticsearch: Mapping[str, object]
) -> None:
    raw_version = elasticsearch.get("version")
    try:
        version = ElasticsearchVersion.from_boundary(raw_version)
    except (TypeError, ValueError) as error:
        raise _elasticsearch_refusal(
            document,
            ERROR_ELASTICSEARCH_VERSION,
            f"{error}; Cloudfall installs an exact release of a supported line: "
            f"{supported_lines_text()}",
            "version",
        ) from error
    if supported_line(version) is None:
        raise _elasticsearch_refusal(
            document,
            ERROR_ELASTICSEARCH_VERSION,
            f"Elasticsearch {version.value} is not of a line Elastic still "
            f"supports; the supported lines are {supported_lines_text()}",
            "version",
        )


def _validate_elasticsearch_node_list(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> tuple[ResourceId, ...]:
    nodes = _elasticsearch_nodes(document, spec)
    server_id = _resource_id_value(spec, "server", document.source)
    seen: set[ResourceId] = set()
    for position, node in enumerate(nodes):
        if node in seen:
            raise _elasticsearch_refusal(
                document,
                ERROR_ELASTICSEARCH_NODES,
                f"Server/{node} is listed twice in nodes",
                "nodes",
                position,
            )
        seen.add(node)
        if ResourceKey(kind=ResourceKind.SERVER, resource_id=node) not in index:
            raise _elasticsearch_refusal(
                document,
                ERROR_ELASTICSEARCH_NODES,
                f"node Server/{node} is not declared",
                "nodes",
                position,
            )
    if server_id not in seen:
        raise _elasticsearch_refusal(
            document,
            ERROR_ELASTICSEARCH_NODES,
            f"nodes must contain the service's server, Server/{server_id}",
            "nodes",
        )
    return nodes


def _validate_elasticsearch_users(
    document: ResourceDocument, elasticsearch: Mapping[str, object]
) -> None:
    raw_users = elasticsearch.get("users", [])
    users = raw_users if isinstance(raw_users, list) else []
    names: set[object] = set()
    for position, raw_user in enumerate(users):
        user = cast("Mapping[str, object]", raw_user)
        name = user.get("name")
        if name in RESERVED_USER_NAMES:
            raise _elasticsearch_refusal(
                document,
                ERROR_ELASTICSEARCH_USER_RESERVED,
                f"user name {name!r} is Cloudfall's own account or a built-in "
                "Elasticsearch user or role",
                "users",
                position,
                "name",
            )
        if name in names:
            raise _elasticsearch_refusal(
                document,
                ERROR_ELASTICSEARCH_USER_DUPLICATE,
                f"user {name!r} is declared twice",
                "users",
                position,
                "name",
            )
        names.add(name)
        _validate_elasticsearch_patterns(
            document, user.get("indexPatterns"), ("users", position, "indexPatterns")
        )
    if "restoreCheckIndices" in elasticsearch:
        _validate_elasticsearch_patterns(
            document,
            elasticsearch.get("restoreCheckIndices"),
            ("restoreCheckIndices",),
        )


def _validate_elasticsearch_patterns(
    document: ResourceDocument,
    raw_patterns: object,
    field: tuple[str | int, ...],
) -> None:
    patterns = raw_patterns if isinstance(raw_patterns, list) else []
    for position, pattern in enumerate(patterns):
        try:
            ElasticsearchIndexPattern.from_boundary(pattern)
        except (TypeError, ValueError) as error:
            raise _elasticsearch_refusal(
                document,
                ERROR_ELASTICSEARCH_PATTERN,
                f"{error}; name the application's own indices",
                *field,
                position,
            ) from error


def _seed_host_is_server(
    seed_host: ElasticsearchSeedHost, server: ResourceDocument
) -> bool:
    """Return whether ``seed_host`` names ``server`` or an address of its prefix."""
    server_spec = _required_mapping(server.content, "spec", server.source)
    raw_network = server_spec.get("network")
    network = (
        cast("Mapping[str, object]", raw_network)
        if isinstance(raw_network, dict)
        else {}
    )
    names = {
        value.lower().removesuffix(".")
        for value in (
            server_spec.get("hostname"),
            server_spec.get("address"),
            network.get("ipv4"),
            network.get("reverseDns"),
        )
        if isinstance(value, str)
    }
    if seed_host.host.lower().removesuffix(".") in names:
        return True
    ipv6_cidr = network.get("ipv6Cidr")
    return (
        seed_host.ipv6 is not None
        and isinstance(ipv6_cidr, str)
        and seed_host.ipv6 in ip_network(ipv6_cidr)
    )


def _validate_elasticsearch_existing_cluster(
    document: ResourceDocument,
    elasticsearch: Mapping[str, object],
    existing: Mapping[str, object],
    nodes: tuple[ResourceId, ...],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    if "clusterName" not in elasticsearch:
        raise _elasticsearch_refusal(
            document,
            ERROR_ELASTICSEARCH_EXISTING_CLUSTER,
            "existingCluster needs clusterName: the nodes join the cluster of "
            "that name, and the converge refuses a seed host of another",
            "existingCluster",
        )
    raw_seed_hosts = existing.get("seedHosts")
    seed_hosts = raw_seed_hosts if isinstance(raw_seed_hosts, list) else []
    for position, raw_seed_host in enumerate(seed_hosts):
        try:
            seed_host = ElasticsearchSeedHost.from_boundary(raw_seed_host)
        except (TypeError, ValueError) as error:
            raise _elasticsearch_refusal(
                document,
                ERROR_ELASTICSEARCH_EXISTING_CLUSTER,
                str(error),
                "existingCluster",
                "seedHosts",
                position,
            ) from error
        for node in nodes:
            server = index[ResourceKey(kind=ResourceKind.SERVER, resource_id=node)]
            if _seed_host_is_server(seed_host, server):
                raise _elasticsearch_refusal(
                    document,
                    ERROR_ELASTICSEARCH_EXISTING_CLUSTER,
                    f"seed host {seed_host.value} is Server/{node}, a declared "
                    "node; seed hosts are the existing cluster's own nodes",
                    "existingCluster",
                    "seedHosts",
                    position,
                )
        try:
            ip_address(seed_host.host)
        except ValueError as error:
            raise _elasticsearch_refusal(
                document,
                ERROR_ELASTICSEARCH_EXISTING_CLUSTER,
                f"seed host {seed_host.value} is a name: a joining node's "
                "firewall admits the existing nodes' transport by address, so "
                "give each seed host as IP:port",
                "existingCluster",
                "seedHosts",
                position,
            ) from error


def _validate_service_uniqueness(documents: Sequence[ResourceDocument]) -> None:
    placements: dict[tuple[ResourceId, str], ResourceDocument] = {}
    ports: dict[tuple[ResourceId, int], ResourceDocument] = {}
    nodes_taken: dict[ResourceId, ResourceDocument] = {}
    rabbitmq_nodes: dict[ResourceId, ResourceDocument] = {}
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
        if raw_kind == "elasticsearch":
            _claim_elasticsearch_nodes(document, spec, raw_port, nodes_taken, ports)
            continue
        if raw_kind == ServiceKind.RABBITMQ.value:
            _claim_rabbitmq_nodes(document, spec, raw_port, rabbitmq_nodes, ports)
            continue
        placement = (server_id, raw_kind)
        if placement in placements and raw_kind == ServiceKind.MARIADB.value:
            issue = ValidationIssue(
                code=ERROR_MARIADB_SERVER_SHARED,
                message=(
                    f"Server/{server_id} already runs the MariaDB service "
                    f"declared at {placements[placement].source.display()}; the "
                    "Debian package runs one instance per host"
                ),
                source=document.source,
                field_path=("spec", "server"),
            )
            raise ConfigValidationError(issue)
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


def _claim_elasticsearch_nodes(
    document: ResourceDocument,
    spec: Mapping[str, object],
    port: int,
    nodes_taken: dict[ResourceId, ResourceDocument],
    ports: dict[tuple[ResourceId, int], ResourceDocument],
) -> None:
    """Refuse a node already in another cluster, or whose port is taken.

    The Debian package runs one Elasticsearch instance per host, and every
    node listens on the service's port.
    """
    for position, node in enumerate(_elasticsearch_nodes(document, spec)):
        if node in nodes_taken:
            raise _elasticsearch_refusal(
                document,
                ERROR_ELASTICSEARCH_SERVER_SHARED,
                f"Server/{node} is already a node of the Elasticsearch service "
                f"declared at {nodes_taken[node].source.display()}; a server "
                "holds at most one Elasticsearch node",
                "nodes",
                position,
            )
        nodes_taken[node] = document
        listener = (node, port)
        if listener in ports:
            issue = ValidationIssue(
                code="service_port_conflict",
                message=(
                    f"Server/{node} port {port} already used by the "
                    f"service declared at {ports[listener].source.display()}"
                ),
                source=document.source,
                field_path=("spec", "bind", "port"),
            )
            raise ConfigValidationError(issue)
        ports[listener] = document


def _validate_elasticsearch_files(
    documents: Sequence[ResourceDocument], project_directory: Path | None
) -> None:
    """Refuse an existing cluster's CA file that is missing, foreign or not a CA."""
    for document in documents:
        if document.key.kind is not ResourceKind.SERVICE:
            continue
        spec = _required_mapping(document.content, "spec", document.source)
        if spec.get("serviceKind") != "elasticsearch":
            continue
        elasticsearch = _required_mapping(spec, "elasticsearch", document.source)
        if "existingCluster" not in elasticsearch:
            continue
        existing = _required_mapping(
            elasticsearch, "existingCluster", document.source
        )
        _validate_ca_certificate_file(
            document, existing.get("caCertificateFile"), project_directory
        )


def _validate_ca_certificate_file(
    document: ResourceDocument,
    raw_path: object,
    project_directory: Path | None,
) -> None:
    field = ("existingCluster", "caCertificateFile")

    def refuse(message: str) -> ConfigValidationError:
        return _elasticsearch_refusal(
            document, ERROR_ELASTICSEARCH_EXISTING_CLUSTER, message, *field
        )

    if project_directory is None:
        message = (
            "caCertificateFile is a path inside the project, and these "
            "resources were not read from a project directory"
        )
        raise refuse(message)
    try:
        relative = ProjectFilePath.from_boundary(raw_path)
    except (TypeError, ValueError) as error:
        raise refuse(str(error)) from error
    root = project_directory.resolve()
    path = (root / relative.value).resolve()
    if not path.is_relative_to(root):
        message = f"caCertificateFile {relative.value} resolves outside the project"
        raise refuse(message)
    if not path.is_file():
        message = f"caCertificateFile {relative.value} does not exist"
        raise refuse(message)
    problem = _ca_certificate_problem(path.read_bytes())
    if problem is not None:
        message = f"caCertificateFile {relative.value} {problem}"
        raise refuse(message)


def _ca_certificate_problem(content: bytes) -> str | None:
    """Say what keeps ``content`` from being CA certificates only, if anything."""
    if b"PRIVATE KEY" in content:
        return "holds a private key; give the CA's certificate only"
    try:
        certificates = x509.load_pem_x509_certificates(content)
    except ValueError:
        return "is not a PEM certificate"
    for certificate in certificates:
        try:
            constraints = certificate.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value
        except x509.ExtensionNotFound:
            return "is not a CA certificate"
        if not constraints.ca:
            return "is not a CA certificate"
    return None


@dataclass(frozen=True, slots=True)
class ElasticsearchVersionEnding:
    """A service whose Elasticsearch line ends within the window, or has ended."""

    service: ResourceId
    version: ElasticsearchVersion
    line: ElasticsearchLine

    def message(self, today: date) -> str:
        """Say when the line ends, or that it has ended."""
        tense = "ended" if today > self.line.end_of_life else "ends"
        return (
            f"Service/{self.service} runs Elasticsearch {self.version.value}, "
            f"whose line {self.line.label} {tense} on "
            f"{self.line.end_of_life.isoformat()}; move it to a later "
            "supported line"
        )


def elasticsearch_versions_ending(
    config: ValidatedConfig,
    today: date,
    lines: tuple[ElasticsearchLine, ...] = SUPPORTED_LINES,
) -> tuple[ElasticsearchVersionEnding, ...]:
    """Return each Elasticsearch service whose line is within its window on ``today``.

    ``today`` is passed in, so the answer depends on the caller's clock only.
    """
    endings: list[ElasticsearchVersionEnding] = []
    for document in config.resources(ResourceKind.SERVICE):
        spec = _required_mapping(document.content, "spec", document.source)
        if spec.get("serviceKind") != "elasticsearch":
            continue
        elasticsearch = _required_mapping(spec, "elasticsearch", document.source)
        version = ElasticsearchVersion.from_boundary(elasticsearch.get("version"))
        line = supported_line(version, lines)
        if line is not None and line.is_ending(today):
            endings.append(
                ElasticsearchVersionEnding(document.key.resource_id, version, line)
            )
    return tuple(endings)


ERROR_MARIADB_VERSION = "service_mariadb_version"
ERROR_MARIADB_SERVER_SHARED = "service_mariadb_server_shared"
ERROR_MARIADB_SERVER_ID_DUPLICATE = "service_mariadb_server_id_duplicate"
ERROR_MARIADB_REPLICATION_LISTEN = "service_mariadb_replication_listen"
ERROR_MARIADB_REPLICATION_PRIMARY = "service_mariadb_replication_primary"
ERROR_MARIADB_REPLICA_DATABASES = "service_mariadb_replica_databases"
ERROR_MARIADB_REPLICA_VERSION = "service_mariadb_replica_version"
ERROR_MARIADB_REPLICATION_UNFIREWALLED = "service_mariadb_replication_unfirewalled"
ERROR_MARIADB_REPLICA_UNADDRESSED = "service_mariadb_replica_unaddressed"
ERROR_MARIADB_REPLICATION_SOURCE = "service_mariadb_replication_source"
ERROR_MARIADB_REMOTE_ACCESS_IPV6 = "service_mariadb_remote_access_ipv6"
ERROR_DATABASE_OWNER_RESERVED = "service_database_owner_reserved"
_MARIADB_PATH = ("spec", "mariadb")
_REPLICATION_PATH = ("replication",)


def _mariadb_refusal(
    document: ResourceDocument,
    code: str,
    message: str,
    *field: str | int,
) -> ConfigValidationError:
    return ConfigValidationError(
        ValidationIssue(
            code=code,
            message=message,
            source=document.source,
            field_path=(*_MARIADB_PATH, *field),
        )
    )


def _mariadb_replication(
    document: ResourceDocument,
) -> Mapping[str, object] | None:
    """Return a MariaDB service's ``replication`` block, or ``None`` when standalone."""
    spec = _required_mapping(document.content, "spec", document.source)
    mariadb = _required_mapping(spec, "mariadb", document.source)
    if "replication" not in mariadb:
        return None
    return _required_mapping(mariadb, "replication", document.source)


def _mariadb_role(document: ResourceDocument) -> MariadbReplicationRole | None:
    replication = _mariadb_replication(document)
    if replication is None:
        return None
    return MariadbReplicationRole.from_boundary(replication.get("role"))


def _is_mariadb_service(document: ResourceDocument) -> bool:
    if document.key.kind is not ResourceKind.SERVICE:
        return False
    spec = _required_mapping(document.content, "spec", document.source)
    return spec.get("serviceKind") == ServiceKind.MARIADB.value


def _baseline_of(
    server: ResourceDocument, index: Mapping[ResourceKey, ResourceDocument]
) -> tuple[ResourceId, Mapping[str, object]]:
    server_spec = _required_mapping(server.content, "spec", server.source)
    baseline_id = _resource_id_value(server_spec, "baseline", server.source)
    baseline = _require_resource(
        index, ResourceKind.BASELINE, baseline_id, server.source, ("spec", "baseline")
    )
    return baseline_id, _required_mapping(baseline.content, "spec", baseline.source)


def _validate_mariadb_service(
    document: ResourceDocument,
    spec: Mapping[str, object],
    server: ResourceDocument,
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    """Apply the S-019 refusals that read one service and what it names."""
    mariadb = _required_mapping(spec, "mariadb", document.source)
    version = _validate_mariadb_version(document, mariadb)
    _validate_mariadb_volume_pool(document, server, index)
    role = _mariadb_role(document)
    databases = _mariadb_databases(document, mariadb)
    owners = _validate_mariadb_database_names(document, databases, index)
    if role is MariadbReplicationRole.PRIMARY:
        _validate_mariadb_primary(document, server, index)
    elif role is MariadbReplicationRole.REPLICA:
        _validate_mariadb_replica(
            document, version, server, index, has_databases=bool(databases)
        )
    if "remoteAccess" in mariadb:
        field_path = (*_MARIADB_PATH, "remoteAccess")
        if role is not MariadbReplicationRole.REPLICA and not owners:
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
        remote_access = _required_mapping(mariadb, "remoteAccess", document.source)
        _validate_remote_access_rules(
            document, spec, remote_access, server, index, field_path=field_path
        )
        _validate_mariadb_remote_sources(document, remote_access)


def _validate_mariadb_remote_sources(
    document: ResourceDocument, remote_access: Mapping[str, object]
) -> None:
    """Refuse a source that is no MariaDB account host: an IPv6 network.

    Runs after the S-006 rules, so each source is already a canonical network.
    One IPv6 address (``/128``) and every IPv4 network stay accepted (D-26);
    the role refuses the same sources at converge, as a second line.
    """
    raw_sources = remote_access.get("allowedSources")
    sources = raw_sources if isinstance(raw_sources, list) else []
    for position, raw_source in enumerate(sources):
        source = IpNetworkCidr.from_boundary(raw_source)
        if mariadb_account_host(source) is None:
            raise _mariadb_refusal(
                document,
                ERROR_MARIADB_REMOTE_ACCESS_IPV6,
                f"allowed source {source.value} is an IPv6 network, which MariaDB "
                "cannot match as an account host; a single IPv6 address "
                "(/128) or an IPv4 network is accepted",
                "remoteAccess",
                "allowedSources",
                position,
            )


def _validate_mariadb_version(
    document: ResourceDocument, mariadb: Mapping[str, object]
) -> MariadbVersion:
    try:
        version = MariadbVersion.from_boundary(mariadb.get("version"))
    except (TypeError, ValueError) as error:
        raise _mariadb_refusal(
            document,
            ERROR_MARIADB_VERSION,
            f"{error}; Cloudfall installs an exact release of a supported "
            f"long-term series: {mariadb_series_text()}",
            "version",
        ) from error
    if mariadb_series(version) is None:
        raise _mariadb_refusal(
            document,
            ERROR_MARIADB_VERSION,
            f"MariaDB {version.value} is not of a long-term series MariaDB still "
            f"maintains; the supported series are {mariadb_series_text()}",
            "version",
        )
    return version


def _validate_mariadb_volume_pool(
    document: ResourceDocument,
    server: ResourceDocument,
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    """Refuse a server with nowhere to keep the mirrored ``mariadb`` volume (S-014)."""
    baseline_id, baseline_spec = _baseline_of(server, index)
    storage = _required_mapping(baseline_spec, "storage", server.source)
    raw_pools = storage.get("pools")
    pools = [
        pool
        for pool in (raw_pools if isinstance(raw_pools, list) else [])
        if isinstance(pool, dict)
    ]
    server_id = server.key.resource_id
    if len(pools) != 1:
        code = (
            "volume_requirement_pool_missing"
            if not pools
            else "volume_requirement_pool_ambiguous"
        )
        reason = (
            f"Baseline/{baseline_id} declares no storage pool"
            if not pools
            else f"Baseline/{baseline_id} declares {len(pools)} pools, and the "
            "volume goes into the only one"
        )
        issue = ValidationIssue(
            code=code,
            message=f"the mariadb volume of Server/{server_id} has nowhere to "
            f"live: {reason}",
            source=document.source,
            field_path=("spec", "server"),
        )
        raise ConfigValidationError(issue)
    if "secondary" not in pools[0]:
        issue = ValidationIssue(
            code="volume_requirement_pool_unmirrored",
            message=(
                f"the mariadb volume of Server/{server_id} is mirrored, but pool "
                f"{pools[0].get('id')} of Baseline/{baseline_id} has no secondary "
                "physical volume; add one"
            ),
            source=document.source,
            field_path=("spec", "server"),
        )
        raise ConfigValidationError(issue)


def _mariadb_databases(
    document: ResourceDocument, mariadb: Mapping[str, object]
) -> tuple[tuple[int, Mapping[str, object]], ...]:
    raw_databases = mariadb.get("databases", [])
    if not isinstance(raw_databases, list):
        raise _mariadb_refusal(
            document,
            "internal_state_shape_invalid",
            "validated service databases are not a list",
            "databases",
        )
    return tuple(
        (position, cast("Mapping[str, object]", database))
        for position, database in enumerate(raw_databases)
        if isinstance(database, dict)
    )


def _validate_mariadb_database_names(
    document: ResourceDocument,
    databases: Sequence[tuple[int, Mapping[str, object]]],
    index: Mapping[ResourceKey, ResourceDocument],
) -> tuple[str, ...]:
    """Refuse a repeated database, or a name MariaDB, Cloudfall or an app holds.

    Return the declared owners.
    """
    application_users = _validate_mariadb_database_entries(document, databases, index)
    owners: list[str] = []
    for position, database in databases:
        owner = database.get("owner")
        if not isinstance(owner, str):
            continue
        reserved = owner in RESERVED_MARIADB_USERS or owner in MARIADB_SYSTEM_DATABASES
        if reserved or owner in application_users:
            holder = (
                "an account MariaDB or Cloudfall holds, or a system database"
                if reserved
                else "the Linux user of an Application with a database on "
                "this service"
            )
            raise _mariadb_refusal(
                document,
                ERROR_DATABASE_OWNER_RESERVED,
                f"database owner {owner!r} is {holder}",
                "databases",
                position,
                "owner",
            )
        owners.append(owner)
    return tuple(owners)


def _validate_mariadb_database_entries(
    document: ResourceDocument,
    databases: Sequence[tuple[int, Mapping[str, object]]],
    index: Mapping[ResourceKey, ResourceDocument],
) -> frozenset[str]:
    """Refuse a repeated or reserved database name, or a reserved app user.

    Return the Linux users of the applications with a database here.
    """
    names: set[object] = set()
    application_users: dict[str, int] = {}
    for position, database in databases:
        name = database.get("name")
        if name in names:
            raise _mariadb_refusal(
                document,
                "service_database_duplicate",
                f"duplicate service database name: {name}",
                "databases",
                position,
                "name",
            )
        names.add(name)
        if name in RESERVED_MARIADB_USERS or name in MARIADB_SYSTEM_DATABASES:
            raise _mariadb_refusal(
                document,
                ERROR_DATABASE_OWNER_RESERVED,
                f"database name {name!r} is a system database or an account "
                "MariaDB or Cloudfall holds",
                "databases",
                position,
                "name",
            )
        if "application" in database:
            application_id = _resource_id_value(
                database, "application", document.source
            )
            application = _require_resource(
                index,
                ResourceKind.APPLICATION,
                application_id,
                document.source,
                (*_MARIADB_PATH, "databases", position, "application"),
            )
            application_spec = _required_mapping(
                application.content, "spec", application.source
            )
            linux_user = application_spec.get("linuxUser")
            if isinstance(linux_user, str):
                application_users.setdefault(linux_user, position)
    for linux_user, position in application_users.items():
        if linux_user in RESERVED_MARIADB_USERS:
            raise _mariadb_refusal(
                document,
                ERROR_DATABASE_OWNER_RESERVED,
                f"the application's Linux user {linux_user!r} would be its "
                "database user, and that name is an account MariaDB or "
                "Cloudfall holds",
                "databases",
                position,
                "application",
            )
    return frozenset(application_users)


def _replication_listen_address(
    document: ResourceDocument, replication: Mapping[str, object]
) -> None:
    field = (*_REPLICATION_PATH, "listenAddress")
    try:
        address = IpAddress.from_boundary(replication.get("listenAddress"))
    except (TypeError, ValueError) as error:
        raise _mariadb_refusal(
            document,
            ERROR_MARIADB_REPLICATION_LISTEN,
            f"a primary's listenAddress is one canonical IP address of its "
            f"server: {error}",
            *field,
        ) from error
    if address.is_unspecified:
        raise _mariadb_refusal(
            document,
            ERROR_MARIADB_REPLICATION_LISTEN,
            f"listenAddress {address.value} is a wildcard; name the one address "
            "of the server its replicas connect to",
            *field,
        )


def _replicas_of(
    document: ResourceDocument, index: Mapping[ResourceKey, ResourceDocument]
) -> tuple[ResourceDocument, ...]:
    """Return the MariaDB services whose ``replication.primary`` names ``document``."""
    replicas: list[ResourceDocument] = []
    for other in index.values():
        if other is document or not _is_mariadb_service(other):
            continue
        replication = _mariadb_replication(other)
        if (
            replication is not None
            and replication.get("role") == MariadbReplicationRole.REPLICA.value
            and replication.get("primary") == document.key.resource_id.value
        ):
            replicas.append(other)
    return tuple(replicas)


def _validate_mariadb_primary(
    document: ResourceDocument,
    server: ResourceDocument,
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    replication = _mariadb_replication(document)
    if replication is None:
        return
    for field in ("primary", "source"):
        if field in replication:
            raise _mariadb_refusal(
                document,
                ERROR_MARIADB_REPLICATION_PRIMARY,
                f"a primary declares no {field}: only a replica follows one",
                *_REPLICATION_PATH,
                field,
            )
    if "listenAddress" not in replication:
        raise _mariadb_refusal(
            document,
            ERROR_MARIADB_REPLICATION_LISTEN,
            "a primary needs listenAddress, the address of its server its "
            "replicas connect to",
            *_REPLICATION_PATH,
        )
    _replication_listen_address(document, replication)
    baseline_id, baseline_spec = _baseline_of(server, index)
    if _replicas_of(document, index) and "firewall" not in baseline_spec:
        raise _mariadb_refusal(
            document,
            ERROR_MARIADB_REPLICATION_UNFIREWALLED,
            f"this primary admits its replicas on its port only, but "
            f"Baseline/{baseline_id} of Server/{server.key.resource_id} declares "
            "no firewall to keep every other address out",
            *_REPLICATION_PATH,
        )


def _validate_mariadb_replica(
    document: ResourceDocument,
    version: MariadbVersion,
    server: ResourceDocument,
    index: Mapping[ResourceKey, ResourceDocument],
    *,
    has_databases: bool,
) -> None:
    replication = _mariadb_replication(document)
    if replication is None:
        return
    if "listenAddress" in replication:
        raise _mariadb_refusal(
            document,
            ERROR_MARIADB_REPLICATION_LISTEN,
            "a replica declares no listenAddress: it connects to its primary",
            *_REPLICATION_PATH,
            "listenAddress",
        )
    if ("primary" in replication) == ("source" in replication):
        raise _mariadb_refusal(
            document,
            ERROR_MARIADB_REPLICATION_PRIMARY,
            "a replica follows exactly one of primary, a MariaDB service of the "
            "project, or source, a server Cloudfall does not manage",
            *_REPLICATION_PATH,
        )
    if has_databases:
        raise _mariadb_refusal(
            document,
            ERROR_MARIADB_REPLICA_DATABASES,
            "a replica declares no databases: its databases and users are its "
            "primary's and arrive by replication",
            "databases",
        )
    baseline_id, baseline_spec = _baseline_of(server, index)
    if "firewall" not in baseline_spec:
        raise _mariadb_refusal(
            document,
            ERROR_MARIADB_REPLICATION_UNFIREWALLED,
            f"Baseline/{baseline_id} of Server/{server.key.resource_id} declares "
            "no firewall, and a replica's server must keep every address but "
            "the declared ones out",
            *_REPLICATION_PATH,
        )
    if "source" in replication:
        source = _required_mapping(replication, "source", document.source)
        try:
            ConnectionAddress.from_boundary(source.get("host"))
        except (TypeError, ValueError) as error:
            raise _mariadb_refusal(
                document,
                ERROR_MARIADB_REPLICATION_SOURCE,
                f"source host is not a hostname or an IP address: {error}",
                *_REPLICATION_PATH,
                "source",
                "host",
            ) from error
        return
    primary = _replication_primary(document, replication, server, index)
    primary_spec = _required_mapping(primary.content, "spec", primary.source)
    # The primary may be read after its replica: refuse its own version first.
    primary_version = _validate_mariadb_version(
        primary, _required_mapping(primary_spec, "mariadb", primary.source)
    )
    if version.series != primary_version.series or version.is_older_than(
        primary_version
    ):
        raise _mariadb_refusal(
            document,
            ERROR_MARIADB_REPLICA_VERSION,
            f"MariaDB {version.value} is not of the series of its primary "
            f"Service/{primary.key.resource_id} ({primary_version.value}) or is "
            "older; a replica runs the primary's series, at its version or "
            "newer, so a patch reaches the replica first",
            "version",
        )
    server_spec = _required_mapping(server.content, "spec", server.source)
    if not ("network" in server_spec or _is_ipv4(server_spec.get("address"))):
        raise _mariadb_refusal(
            document,
            ERROR_MARIADB_REPLICA_UNADDRESSED,
            f"Server/{server.key.resource_id} has a name for its address and "
            "declares no network, so the primary's firewall has no address to "
            "admit it from; give it an IPv4 address or a network block",
            *_REPLICATION_PATH,
        )


def _replication_primary(
    document: ResourceDocument,
    replication: Mapping[str, object],
    server: ResourceDocument,
    index: Mapping[ResourceKey, ResourceDocument],
) -> ResourceDocument:
    """Return the primary a replica names, refusing one it can't follow."""
    primary_id = _resource_id_value(replication, "primary", document.source)

    def refuse(reason: str) -> ConfigValidationError:
        return _mariadb_refusal(
            document,
            ERROR_MARIADB_REPLICATION_PRIMARY,
            f"primary Service/{primary_id} {reason}",
            *_REPLICATION_PATH,
            "primary",
        )

    if primary_id == document.key.resource_id:
        message = "is this service itself"
        raise refuse(message)
    primary = index.get(ResourceKey(kind=ResourceKind.SERVICE, resource_id=primary_id))
    if primary is None or not _is_mariadb_service(primary):
        message = "is not a declared MariaDB service"
        raise refuse(message)
    spec = _required_mapping(document.content, "spec", document.source)
    primary_spec = _required_mapping(primary.content, "spec", primary.source)
    if primary_spec.get("environment") != spec.get("environment"):
        message = f"belongs to {primary_spec.get('environment')}, not this environment"
        raise refuse(message)
    if _mariadb_role(primary) is not MariadbReplicationRole.PRIMARY:
        message = "does not declare replication role: primary"
        raise refuse(message)
    if primary_spec.get("server") == server.key.resource_id.value:
        message = (
            f"runs on Server/{server.key.resource_id} too; a server holds one "
            "MariaDB server, so a replica lives on another"
        )
        raise refuse(message)
    return primary


def _validate_mariadb_project(
    documents: Sequence[ResourceDocument], project_directory: Path | None
) -> None:
    """Refuse a ``serverId`` used twice, and an outside source's CA file (S-019)."""
    server_ids: dict[object, ResourceDocument] = {}
    for document in documents:
        if not _is_mariadb_service(document):
            continue
        replication = _mariadb_replication(document)
        if replication is None:
            continue
        server_id = replication.get("serverId")
        if server_id in server_ids:
            raise _mariadb_refusal(
                document,
                ERROR_MARIADB_SERVER_ID_DUPLICATE,
                f"serverId {server_id} is already the one of the MariaDB service "
                f"declared at {server_ids[server_id].source.display()}; each "
                "server of the project's MariaDB services has its own",
                *_REPLICATION_PATH,
                "serverId",
            )
        server_ids[server_id] = document
        source = replication.get("source")
        if isinstance(source, dict) and "caCertificateFile" in source:
            _validate_mariadb_source_ca(
                document, source.get("caCertificateFile"), project_directory
            )


def _validate_mariadb_source_ca(
    document: ResourceDocument, raw_path: object, project_directory: Path | None
) -> None:
    field = (*_REPLICATION_PATH, "source", "caCertificateFile")

    def refuse(message: str) -> ConfigValidationError:
        return _mariadb_refusal(
            document, ERROR_MARIADB_REPLICATION_SOURCE, message, *field
        )

    if project_directory is None:
        message = (
            "caCertificateFile is a path inside the project, and these "
            "resources were not read from a project directory"
        )
        raise refuse(message)
    try:
        relative = ProjectFilePath.from_boundary(raw_path)
    except (TypeError, ValueError) as error:
        raise refuse(str(error)) from error
    root = project_directory.resolve()
    path = (root / relative.value).resolve()
    if not path.is_relative_to(root):
        message = f"caCertificateFile {relative.value} resolves outside the project"
        raise refuse(message)
    if not path.is_file():
        message = f"caCertificateFile {relative.value} does not exist"
        raise refuse(message)
    problem = _ca_certificate_problem(path.read_bytes())
    if problem is not None:
        message = f"caCertificateFile {relative.value} {problem}"
        raise refuse(message)


@dataclass(frozen=True, slots=True)
class MariadbVersionEnding:
    """A service whose MariaDB series ends within the window, or has ended."""

    service: ResourceId
    version: MariadbVersion
    series: MariadbSeries

    def message(self, today: date) -> str:
        """Say when the series ends, or that it has ended."""
        tense = "ended" if today > self.series.end_of_life else "ends"
        return (
            f"Service/{self.service} runs MariaDB {self.version.value}, whose "
            f"series {self.series.label} {tense} on "
            f"{self.series.end_of_life.isoformat()}; move it to a later "
            "supported series"
        )


def mariadb_versions_ending(
    config: ValidatedConfig,
    today: date,
    table: tuple[MariadbSeries, ...] = MARIADB_SUPPORTED_SERIES,
) -> tuple[MariadbVersionEnding, ...]:
    """Return each MariaDB service whose series is within its window on ``today``.

    ``today`` is passed in, so the answer depends on the caller's clock only.
    """
    endings: list[MariadbVersionEnding] = []
    for document in config.resources(ResourceKind.SERVICE):
        if not _is_mariadb_service(document):
            continue
        spec = _required_mapping(document.content, "spec", document.source)
        mariadb = _required_mapping(spec, "mariadb", document.source)
        version = MariadbVersion.from_boundary(mariadb.get("version"))
        series = mariadb_series(version, table)
        if series is not None and series.is_ending(today):
            endings.append(
                MariadbVersionEnding(document.key.resource_id, version, series)
            )
    return tuple(endings)


def mariadb_unencrypted_sources(config: ValidatedConfig) -> tuple[ResourceId, ...]:
    """Return each replica of an outside source declared without a CA file."""
    found: list[ResourceId] = []
    for document in config.resources(ResourceKind.SERVICE):
        if not _is_mariadb_service(document):
            continue
        replication = _mariadb_replication(document)
        source = None if replication is None else replication.get("source")
        if isinstance(source, dict) and "caCertificateFile" not in source:
            found.append(document.key.resource_id)
    return tuple(found)


ERROR_RABBITMQ_VERSION = "service_rabbitmq_version"
ERROR_RABBITMQ_NODES = "service_rabbitmq_nodes"
ERROR_RABBITMQ_SERVER_SHARED = "service_rabbitmq_server_shared"
ERROR_RABBITMQ_ENVIRONMENT = "service_rabbitmq_environment"
ERROR_RABBITMQ_HOSTNAME = "service_rabbitmq_hostname"
ERROR_RABBITMQ_REMOTE_ACCESS = "service_rabbitmq_remote_access"
ERROR_RABBITMQ_VHOST = "service_rabbitmq_vhost"
ERROR_RABBITMQ_RESERVED = "service_rabbitmq_reserved"
ERROR_RABBITMQ_UNFIREWALLED = "service_rabbitmq_unfirewalled"
ERROR_RABBITMQ_SERVER_UNADDRESSED = "service_rabbitmq_server_unaddressed"
_RABBITMQ_PATH = ("spec", "rabbitmq")


def _rabbitmq_refusal(
    document: ResourceDocument,
    code: str,
    message: str,
    *field: str | int,
) -> ConfigValidationError:
    return ConfigValidationError(
        ValidationIssue(
            code=code,
            message=message,
            source=document.source,
            field_path=(*_RABBITMQ_PATH, *field),
        )
    )


def _is_rabbitmq_service(document: ResourceDocument) -> bool:
    if document.key.kind is not ResourceKind.SERVICE:
        return False
    spec = _required_mapping(document.content, "spec", document.source)
    return spec.get("serviceKind") == ServiceKind.RABBITMQ.value


def _rabbitmq_nodes(
    document: ResourceDocument, spec: Mapping[str, object]
) -> tuple[ResourceId, ...]:
    """Return the declared nodes, or the service's server when none are."""
    rabbitmq = _required_mapping(spec, "rabbitmq", document.source)
    if "nodes" not in rabbitmq:
        return (_resource_id_value(spec, "server", document.source),)
    raw_nodes = rabbitmq.get("nodes")
    if not isinstance(raw_nodes, list):
        raise _rabbitmq_refusal(
            document,
            "internal_state_shape_invalid",
            "validated RabbitMQ nodes are not a list",
            "nodes",
        )
    return tuple(ResourceId.from_boundary(node) for node in raw_nodes)


def _validate_rabbitmq_service(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    """Apply the S-023 refusals that read one service and the servers it names."""
    rabbitmq = _required_mapping(spec, "rabbitmq", document.source)
    if "remoteAccess" in rabbitmq:
        raise _rabbitmq_refusal(
            document,
            ERROR_RABBITMQ_REMOTE_ACCESS,
            "remoteAccess names one listen address, and every RabbitMQ node "
            "listens on its own; declare clientAccess with its allowedSources",
            "remoteAccess",
        )
    _validate_rabbitmq_version(document, rabbitmq)
    nodes = _validate_rabbitmq_node_list(document, spec, index)
    _validate_rabbitmq_node_servers(document, spec, nodes, index)
    _validate_rabbitmq_vhosts(document, rabbitmq)
    if "clientAccess" in rabbitmq:
        _validate_rabbitmq_client_sources(
            document, _required_mapping(rabbitmq, "clientAccess", document.source)
        )
    _validate_rabbitmq_node_hosts(
        document,
        nodes,
        index,
        reachable=len(nodes) > 1 or "clientAccess" in rabbitmq,
    )


def _validate_rabbitmq_version(
    document: ResourceDocument, rabbitmq: Mapping[str, object]
) -> None:
    try:
        version = RabbitmqVersion.from_boundary(rabbitmq.get("version"))
    except (TypeError, ValueError) as error:
        raise _rabbitmq_refusal(
            document,
            ERROR_RABBITMQ_VERSION,
            f"{error}; Cloudfall installs an exact release of a supported "
            f"series: {rabbitmq_series_text()}",
            "version",
        ) from error
    if rabbitmq_series(version) is None:
        raise _rabbitmq_refusal(
            document,
            ERROR_RABBITMQ_VERSION,
            f"RabbitMQ {version.value} is not of a series Team RabbitMQ still "
            "supports for the open-source edition; the supported series are "
            f"{rabbitmq_series_text()}",
            "version",
        )


def _validate_rabbitmq_node_list(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> tuple[ResourceId, ...]:
    nodes = _rabbitmq_nodes(document, spec)
    server_id = _resource_id_value(spec, "server", document.source)
    seen: set[ResourceId] = set()
    for position, node in enumerate(nodes):
        if node in seen:
            raise _rabbitmq_refusal(
                document,
                ERROR_RABBITMQ_NODES,
                f"Server/{node} is listed twice in nodes",
                "nodes",
                position,
            )
        seen.add(node)
        if ResourceKey(kind=ResourceKind.SERVER, resource_id=node) not in index:
            raise _rabbitmq_refusal(
                document,
                ERROR_RABBITMQ_NODES,
                f"node Server/{node} is not declared",
                "nodes",
                position,
            )
    if server_id not in seen:
        raise _rabbitmq_refusal(
            document,
            ERROR_RABBITMQ_NODES,
            f"nodes must contain the service's server, Server/{server_id}",
            "nodes",
        )
    if len(nodes) not in RABBITMQ_NODE_COUNTS:
        raise _rabbitmq_refusal(
            document,
            ERROR_RABBITMQ_NODES,
            f"nodes names {len(nodes)} servers; a RabbitMQ service has one node "
            "or three, since with two the loss of either stops the cluster",
            "nodes",
        )
    return nodes


def _validate_rabbitmq_node_servers(
    document: ResourceDocument,
    spec: Mapping[str, object],
    nodes: tuple[ResourceId, ...],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    """Refuse a node of another environment, or two nodes of one hostname."""
    environment = _resource_id_value(spec, "environment", document.source)
    hostnames: dict[str, ResourceId] = {}
    for position, node in enumerate(nodes):
        server = index[ResourceKey(kind=ResourceKind.SERVER, resource_id=node)]
        server_spec = _required_mapping(server.content, "spec", server.source)
        server_environment = _resource_id_value(
            server_spec, "environment", server.source
        )
        if server_environment != environment:
            raise _rabbitmq_refusal(
                document,
                ERROR_RABBITMQ_ENVIRONMENT,
                f"node Server/{node} belongs to {server_environment}, not "
                f"{environment}",
                "nodes",
                position,
            )
        hostname = server_spec.get("hostname")
        if not isinstance(hostname, str):
            raise _rabbitmq_refusal(
                document,
                "internal_state_shape_invalid",
                f"validated hostname of Server/{node} is not a string",
                "nodes",
                position,
            )
        # RabbitMQ names a node rabbit@ and the short hostname, its first
        # label, so h2.a and h2.b would be one node name
        key = hostname.lower().removesuffix(".").split(".")[0]
        if key in hostnames:
            raise _rabbitmq_refusal(
                document,
                ERROR_RABBITMQ_HOSTNAME,
                f"nodes Server/{hostnames[key]} and Server/{node} have the same "
                f"short hostname {key}; a node is named rabbit@ and its short "
                "hostname, so each node needs its own",
                "nodes",
                position,
            )
        hostnames[key] = node


def _validate_rabbitmq_vhosts(
    document: ResourceDocument, rabbitmq: Mapping[str, object]
) -> None:
    """Refuse a virtual host without an owner, or a name RabbitMQ or Cloudfall holds."""
    raw_vhosts = rabbitmq.get("vhosts", [])
    if not isinstance(raw_vhosts, list):
        raise _rabbitmq_refusal(
            document,
            "internal_state_shape_invalid",
            "validated RabbitMQ virtual hosts are not a list",
            "vhosts",
        )
    names: dict[str, int] = {}
    owners: dict[str, str] = {}
    for position, raw_vhost in enumerate(raw_vhosts):
        vhost = cast("Mapping[str, object]", raw_vhost)
        field = ("vhosts", position)
        name = _rabbitmq_vhost_name(document, vhost, field)
        if "application" in vhost:
            raise _rabbitmq_refusal(
                document,
                ERROR_RABBITMQ_VHOST,
                f"virtual host {name.value!r} names an application: RabbitMQ has "
                "no login by unix socket, so every account has a password; "
                "declare an owner and its passwordRef",
                *field,
                "application",
            )
        for required in ("owner", "passwordRef"):
            if required not in vhost:
                raise _rabbitmq_refusal(
                    document,
                    ERROR_RABBITMQ_VHOST,
                    f"virtual host {name.value!r} needs {required}: it belongs "
                    "to a user whose password comes from passwordRef",
                    *field,
                )
        if name.value in names:
            raise _rabbitmq_refusal(
                document,
                ERROR_RABBITMQ_VHOST,
                f"virtual host {name.value!r} is declared twice",
                *field,
                "name",
            )
        names[name.value] = position
        owner = _rabbitmq_owner(document, vhost, field)
        if owner.value in owners:
            raise _rabbitmq_refusal(
                document,
                ERROR_RABBITMQ_VHOST,
                f"owner {owner.value!r} already owns virtual host "
                f"{owners[owner.value]!r}; an owner has permissions on its own "
                "virtual host only",
                *field,
                "owner",
            )
        owners[owner.value] = name.value


def _rabbitmq_vhost_name(
    document: ResourceDocument,
    vhost: Mapping[str, object],
    field: tuple[str | int, ...],
) -> RabbitmqVhostName:
    raw_name = vhost.get("name")
    if raw_name == RABBITMQ_DEFAULT_VHOST:
        raise _rabbitmq_refusal(
            document,
            ERROR_RABBITMQ_RESERVED,
            "virtual host '/' is RabbitMQ's default, which Cloudfall removes "
            "with guest; name the application's own",
            *field,
            "name",
        )
    try:
        name = RabbitmqVhostName.from_boundary(raw_name)
    except (TypeError, ValueError) as error:
        raise _rabbitmq_refusal(
            document, ERROR_RABBITMQ_VHOST, str(error), *field, "name"
        ) from error
    if name.is_reserved:
        raise _rabbitmq_refusal(
            document,
            ERROR_RABBITMQ_RESERVED,
            f"virtual host {name.value!r} starts with cloudfall_, the prefix of "
            "the names the restore drill uses",
            *field,
            "name",
        )
    return name


def _rabbitmq_owner(
    document: ResourceDocument,
    vhost: Mapping[str, object],
    field: tuple[str | int, ...],
) -> RabbitmqUserName:
    try:
        owner = RabbitmqUserName.from_boundary(vhost.get("owner"))
    except (TypeError, ValueError) as error:
        raise _rabbitmq_refusal(
            document, ERROR_RABBITMQ_VHOST, str(error), *field, "owner"
        ) from error
    if owner.is_reserved:
        raise _rabbitmq_refusal(
            document,
            ERROR_RABBITMQ_RESERVED,
            f"owner {owner.value!r} is RabbitMQ's guest account, which Cloudfall "
            "removes, or starts with cloudfall_, the prefix of the names the "
            "restore drill uses",
            *field,
            "owner",
        )
    return owner


def _validate_rabbitmq_client_sources(
    document: ResourceDocument, client_access: Mapping[str, object]
) -> None:
    """Refuse a client source that is no canonical network, or every address (S-006)."""
    raw_sources = client_access.get("allowedSources")
    sources = raw_sources if isinstance(raw_sources, list) else []
    for position, raw_source in enumerate(sources):
        field = ("clientAccess", "allowedSources", position)
        try:
            source = IpNetworkCidr.from_boundary(raw_source)
        except (TypeError, ValueError) as error:
            raise _rabbitmq_refusal(
                document,
                "service_remote_access_source_invalid",
                f"allowed source is not a canonical CIDR: {error}",
                *field,
            ) from error
        if source.is_unrestricted:
            raise _rabbitmq_refusal(
                document,
                "service_remote_access_open",
                f"allowed source {source.value} matches every address; name the "
                "networks the clients connect from",
                *field,
            )


def _validate_rabbitmq_node_hosts(
    document: ResourceDocument,
    nodes: tuple[ResourceId, ...],
    index: Mapping[ResourceKey, ResourceDocument],
    *,
    reachable: bool,
) -> None:
    """Refuse a node whose server can't hold what the node needs (S-023).

    Each node's ``rabbitmq`` volume goes into the only storage pool of its
    server's baseline, on two disks for a service of one node, the only copy
    of its messages. A node that others reach, a node of three or one with
    ``clientAccess``, needs a firewall to admit them alone and an IPv4 to be
    reached and admitted at.
    """
    single = len(nodes) == 1
    for position, node in enumerate(nodes):
        server = index[ResourceKey(kind=ResourceKind.SERVER, resource_id=node)]
        server_spec = _required_mapping(server.content, "spec", server.source)
        baseline_id, baseline_spec = _baseline_of(server, index)
        storage = _required_mapping(baseline_spec, "storage", server.source)
        raw_pools = storage.get("pools")
        pools = [
            pool
            for pool in (raw_pools if isinstance(raw_pools, list) else [])
            if isinstance(pool, dict)
        ]
        if len(pools) != 1:
            code = (
                "volume_requirement_pool_missing"
                if not pools
                else "volume_requirement_pool_ambiguous"
            )
            reason = (
                f"Baseline/{baseline_id} declares no storage pool"
                if not pools
                else f"Baseline/{baseline_id} declares {len(pools)} pools, and the "
                "node's volume goes into the only one"
            )
            raise _rabbitmq_refusal(
                document,
                code,
                f"the rabbitmq volume of node Server/{node} has nowhere to live: "
                f"{reason}",
                "nodes",
                position,
            )
        if single and "secondary" not in pools[0]:
            raise _rabbitmq_refusal(
                document,
                "volume_requirement_pool_unmirrored",
                f"the rabbitmq volume of Server/{node} is mirrored, the only copy "
                f"of a service of one node, but pool {pools[0].get('id')} of "
                f"Baseline/{baseline_id} has no secondary physical volume; add one",
                "nodes",
                position,
            )
        if reachable and "firewall" not in baseline_spec:
            raise _rabbitmq_refusal(
                document,
                ERROR_RABBITMQ_UNFIREWALLED,
                f"node Server/{node} opens its RabbitMQ ports to the other nodes "
                f"or the clientAccess sources only, but Baseline/{baseline_id} "
                "declares no firewall to keep every other address out",
                "nodes",
                position,
            )
        if reachable and not (
            "network" in server_spec or _is_ipv4(server_spec.get("address"))
        ):
            raise _rabbitmq_refusal(
                document,
                ERROR_RABBITMQ_SERVER_UNADDRESSED,
                f"node Server/{node} has a name for its address and declares no "
                "network, so the other nodes and the clients have no IPv4 to "
                "reach it at and the firewalls none to admit; give it an IPv4 "
                "address or a network block",
                "nodes",
                position,
            )


def _claim_rabbitmq_nodes(
    document: ResourceDocument,
    spec: Mapping[str, object],
    port: int,
    nodes_taken: dict[ResourceId, ResourceDocument],
    ports: dict[tuple[ResourceId, int], ResourceDocument],
) -> None:
    """Refuse a node already in another RabbitMQ service, or whose port is taken.

    The Debian package runs one RabbitMQ node per host, and every node
    listens on the service's port.
    """
    for position, node in enumerate(_rabbitmq_nodes(document, spec)):
        if node in nodes_taken:
            raise _rabbitmq_refusal(
                document,
                ERROR_RABBITMQ_SERVER_SHARED,
                f"Server/{node} is already a node of the RabbitMQ service "
                f"declared at {nodes_taken[node].source.display()}; a server "
                "holds at most one RabbitMQ node",
                "nodes",
                position,
            )
        nodes_taken[node] = document
        listener = (node, port)
        if listener in ports:
            issue = ValidationIssue(
                code="service_port_conflict",
                message=(
                    f"Server/{node} port {port} already used by the "
                    f"service declared at {ports[listener].source.display()}"
                ),
                source=document.source,
                field_path=("spec", "bind", "port"),
            )
            raise ConfigValidationError(issue)
        ports[listener] = document


@dataclass(frozen=True, slots=True)
class RabbitmqVersionEnding:
    """A service whose RabbitMQ series ends within the window, or has ended."""

    service: ResourceId
    version: RabbitmqVersion
    series: RabbitmqSeries

    def message(self, today: date) -> str:
        """Say when community support of the series ends, or that it has ended."""
        tense = "ended" if today > self.series.end_of_support else "ends"
        return (
            f"Service/{self.service} runs RabbitMQ {self.version.value}, whose "
            f"series {self.series.label} {tense} community support on "
            f"{self.series.end_of_support.isoformat()}; move it to the next "
            "supported series"
        )


def rabbitmq_versions_ending(
    config: ValidatedConfig,
    today: date,
    table: tuple[RabbitmqSeries, ...] = RABBITMQ_SUPPORTED_SERIES,
) -> tuple[RabbitmqVersionEnding, ...]:
    """Return each RabbitMQ service whose series is within its window on ``today``.

    ``today`` is passed in, so the answer depends on the caller's clock only.
    """
    endings: list[RabbitmqVersionEnding] = []
    for document in config.resources(ResourceKind.SERVICE):
        if not _is_rabbitmq_service(document):
            continue
        spec = _required_mapping(document.content, "spec", document.source)
        rabbitmq = _required_mapping(spec, "rabbitmq", document.source)
        version = RabbitmqVersion.from_boundary(rabbitmq.get("version"))
        series = rabbitmq_series(version, table)
        if series is not None and series.is_ending(today):
            endings.append(
                RabbitmqVersionEnding(document.key.resource_id, version, series)
            )
    return tuple(endings)


def rabbitmq_single_nodes(config: ValidatedConfig) -> tuple[ResourceId, ...]:
    """Return each RabbitMQ service of the production environment with one node."""
    found: list[ResourceId] = []
    for document in config.resources(ResourceKind.SERVICE):
        if not _is_rabbitmq_service(document):
            continue
        spec = _required_mapping(document.content, "spec", document.source)
        environment = _resource_id_value(spec, "environment", document.source)
        if (
            environment.value == RABBITMQ_PRODUCTION
            and len(_rabbitmq_nodes(document, spec)) == 1
        ):
            found.append(document.key.resource_id)
    return tuple(found)


@dataclass(frozen=True, slots=True)
class ServiceWarning:
    """A warning ``config validate`` gives about one service, with its context."""

    code: str
    message: str
    context: Mapping[str, str]


def rabbitmq_warnings(
    config: ValidatedConfig,
    today: date,
    table: tuple[RabbitmqSeries, ...] = RABBITMQ_SUPPORTED_SERIES,
) -> tuple[ServiceWarning, ...]:
    """Return the S-023 warnings of ``config`` on ``today``.

    ``RABBITMQ_VERSION_ENDING`` per service whose series ends within the
    window, or has ended, naming the date; ``RABBITMQ_SINGLE_NODE`` per
    production service of one node. ``today`` and ``table`` are passed in,
    so the answer depends on the caller's clock only.
    """
    endings = tuple(
        ServiceWarning(
            code=RABBITMQ_WARNING_VERSION_ENDING,
            message=ending.message(today),
            context={
                "service": ending.service.value,
                "version": ending.version.value,
                "series": ending.series.label,
                "endOfLife": ending.series.end_of_support.isoformat(),
            },
        )
        for ending in rabbitmq_versions_ending(config, today, table)
    )
    single_nodes = tuple(
        ServiceWarning(
            code=RABBITMQ_WARNING_SINGLE_NODE,
            message=(
                f"Service/{service} is one RabbitMQ node in production: every "
                "message has one copy, and every restart stops the service; "
                "add two nodes"
            ),
            context={"service": service.value},
        )
        for service in rabbitmq_single_nodes(config)
    )
    return endings + single_nodes


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
    remote_collectors: list[ResourceDocument] = []
    for server_id in collector_ids:
        server = _require_resource(
            index,
            ResourceKind.SERVER,
            server_id,
            document.source,
            ("spec", "collectors", "servers"),
        )
        _require_server_environment(server, environment, document.source)
        if server_id != backend_id:
            remote_collectors.append(server)
    _validate_logging_gateway_sources(
        document, spec, backend_server, tuple(remote_collectors), index
    )
    _validate_logging_volume_pools(document, spec, backend_server, index)
    alerting = spec.get("alerting")
    receivers = alerting.get("receivers") if isinstance(alerting, dict) else None
    listed = receivers if isinstance(receivers, list) else []
    for position, receiver in enumerate(listed):
        if isinstance(receiver, dict) and "channel" in receiver:
            _require_alert_channel(
                index,
                receiver["channel"],
                document.source,
                ("spec", "alerting", "receivers", position, "channel"),
            )

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


def _validate_logging_gateway_sources(
    document: ResourceDocument,
    spec: Mapping[str, object],
    backend_server: ResourceDocument,
    remote_collectors: tuple[ResourceDocument, ...],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    """Refuse a remote collector the backend's firewall cannot admit (S-011).

    The gateway port opens to each remote collector's addresses only, so a
    collector behind a default-deny firewall needs an IP: its ``address``, or
    ``network``. A firewall that already opens the port to everyone, or none,
    needs nothing.
    """
    if not remote_collectors:
        return
    server_spec = _required_mapping(
        backend_server.content, "spec", backend_server.source
    )
    baseline_id = _resource_id_value(server_spec, "baseline", backend_server.source)
    baseline = _require_resource(
        index,
        ResourceKind.BASELINE,
        baseline_id,
        backend_server.source,
        ("spec", "baseline"),
    )
    baseline_spec = _required_mapping(baseline.content, "spec", baseline.source)
    if "firewall" not in baseline_spec:
        return
    gateway = _required_mapping(spec, "gateway", document.source)
    port = gateway.get("port")
    firewall = _required_mapping(baseline_spec, "firewall", baseline.source)
    raw_rules = firewall.get("allowedInbound")
    rules = raw_rules if isinstance(raw_rules, list) else []
    if any(
        isinstance(rule, dict)
        and rule.get("port") == port
        and rule.get("protocol") == "tcp"
        for rule in rules
    ):
        return
    for collector in remote_collectors:
        collector_spec = _required_mapping(
            collector.content, "spec", collector.source
        )
        if "network" in collector_spec or _is_ip(collector_spec.get("address")):
            continue
        issue = ValidationIssue(
            code="logging_gateway_collector_unaddressed",
            message=(
                f"Server/{collector.key.resource_id} collects for this stack, but "
                "its address is a name and it declares no network, so the "
                f"firewall of Baseline/{baseline_id} cannot open tcp port {port} "
                "to it; give it an IP address or a network block"
            ),
            source=document.source,
            field_path=("spec", "collectors", "servers"),
        )
        raise ConfigValidationError(issue)


def _validate_logging_volume_pools(
    document: ResourceDocument,
    spec: Mapping[str, object],
    backend_server: ResourceDocument,
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    """Refuse a logging volume with no pool to live in (S-014-2).

    Each volume names a pool of the backend's baseline, or leaves it out when
    the baseline has exactly one.
    """
    server_spec = _required_mapping(
        backend_server.content, "spec", backend_server.source
    )
    baseline_id = _resource_id_value(server_spec, "baseline", backend_server.source)
    baseline = _require_resource(
        index,
        ResourceKind.BASELINE,
        baseline_id,
        backend_server.source,
        ("spec", "baseline"),
    )
    baseline_spec = _required_mapping(baseline.content, "spec", baseline.source)
    storage = _required_mapping(baseline_spec, "storage", baseline.source)
    raw_pools = storage.get("pools")
    pools = [
        str(pool.get("id"))
        for pool in (raw_pools if isinstance(raw_pools, list) else [])
        if isinstance(pool, dict)
    ]
    backend = _required_mapping(spec, "backend", document.source)
    metrics = _required_mapping(spec, "metrics", document.source)
    volumes = (
        (
            "loki",
            _required_mapping(backend, "storage", document.source).get("pool"),
            ("spec", "backend", "storage", "pool"),
        ),
        (
            "prometheus",
            _required_mapping(metrics, "backend", document.source).get("storagePool"),
            ("spec", "metrics", "backend", "storagePool"),
        ),
    )
    for volume, named, field_path in volumes:
        if named is None and len(pools) == 1:
            continue
        if named is not None and named in pools:
            continue
        if not pools:
            code = "volume_requirement_pool_missing"
            reason = f"Baseline/{baseline_id} declares no storage pool"
        elif named is None:
            code = "volume_requirement_pool_ambiguous"
            reason = (
                f"Baseline/{baseline_id} declares {len(pools)} pools; name one"
            )
        else:
            code = "volume_requirement_pool_unknown"
            reason = f"Baseline/{baseline_id} declares no pool {named}"
        issue = ValidationIssue(
            code=code,
            message=f"the {volume} volume has nowhere to live: {reason}",
            source=document.source,
            field_path=field_path,
        )
        raise ConfigValidationError(issue)


def _is_ip(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        ip_address(value)
    except ValueError:
        return False
    return True


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


def _validate_baseline(spec: Mapping[str, object], source: SourceLocation) -> None:
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
    if "pools" in storage:
        pools = _validate_unique_entry_keys(
            storage, "pools", "id", source, ("spec", "storage", "pools")
        )
        _validate_unique_entry_keys(
            storage, "pools", "volumeGroup", source, ("spec", "storage", "pools")
        )
        for index, pool in enumerate(pools):
            if pool.get("secondary") == pool.get("primary"):
                issue = ValidationIssue(
                    code="storage_pool_same_physical_volume",
                    message=(
                        f"storage pool {pool.get('id')} names "
                        f"{pool.get('primary')} as both primary and secondary"
                    ),
                    source=source,
                    field_path=("spec", "storage", "pools", index, "secondary"),
                )
                raise ConfigValidationError(issue)
    _validate_baseline_firewall(spec, source)


def _validate_baseline_firewall(
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
                code="baseline_entry_duplicate",
                message=f"duplicate {entry_key} in baseline: {identity}",
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


def _validate_component_build(
    spec: Mapping[str, object], source: SourceLocation
) -> None:
    raw_steps = spec.get("build")
    if raw_steps is None:
        return
    raw_tools = spec.get("buildTools")
    tools = {} if raw_tools is None else _required_mapping(spec, "buildTools", source)
    if not isinstance(raw_steps, list):
        issue = ValidationIssue(
            code="internal_state_shape_invalid",
            message="validated build is not a list",
            source=source,
            field_path=("spec", "build"),
        )
        raise ConfigValidationError(issue)
    for index, step in enumerate(raw_steps):
        command = step.get("command") if isinstance(step, dict) else None
        if not isinstance(command, list) or not command:
            issue = ValidationIssue(
                code="internal_state_shape_invalid",
                message="validated build step command is not a non-empty list",
                source=source,
                field_path=("spec", "build", index, "command"),
            )
            raise ConfigValidationError(issue)
        executable = command[0]
        if executable not in tools:
            issue = ValidationIssue(
                code="component_build_tool_undeclared",
                message=(
                    f"build step {index} runs {executable!r}, which "
                    "spec.buildTools does not declare; add it with its "
                    "minimum version"
                ),
                source=source,
                field_path=("spec", "build", index, "command", 0),
            )
            raise ConfigValidationError(issue)


def _validate_component_runtime(
    spec: Mapping[str, object], source: SourceLocation
) -> None:
    runtime = _required_mapping(spec, "runtime", source)
    for field, deployable in _DEPLOYABLE_RUNTIME.items():
        value = runtime.get(field)
        if value != deployable:
            issue = ValidationIssue(
                code="component_runtime_unsupported",
                message=(
                    f"runtime.{field} {value!r} is reserved and cannot deploy: "
                    "only runtime.type 'python' with runtime.packageManager "
                    "'uv' deploys today"
                ),
                source=source,
                field_path=("spec", "runtime", field),
            )
            raise ConfigValidationError(issue)


def _validate_alert_channel_references(
    document: ResourceDocument,
    spec: Mapping[str, object],
    index: Mapping[ResourceKey, ResourceDocument],
) -> None:
    """Check the chat id as the inventory reads it; references are checked there.

    The schema's ``$`` lets a trailing newline through, which the inventory's
    ``TelegramChatId`` refuses.
    """
    del index
    telegram = spec.get("telegram")
    chat_id = telegram.get("chatId") if isinstance(telegram, dict) else None
    try:
        TelegramChatId.from_boundary(chat_id)
    except (TypeError, ValueError) as error:
        issue = ValidationIssue(
            code="alert_channel_chat_id_invalid",
            message=str(error),
            source=document.source,
            field_path=("spec", "telegram", "chatId"),
        )
        raise ConfigValidationError(issue) from error


def _require_alert_channel(
    index: Mapping[ResourceKey, ResourceDocument],
    channel: object,
    source: SourceLocation,
    field_path: tuple[str | int, ...],
) -> None:
    """Refuse a reference to an AlertChannel the project does not declare (S-015)."""
    if not isinstance(channel, str) or not any(
        key.kind is ResourceKind.ALERT_CHANNEL and key.resource_id.value == channel
        for key in index
    ):
        issue = ValidationIssue(
            code="alert_channel_missing",
            message=f"no AlertChannel {channel} is declared",
            source=source,
            field_path=field_path,
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
    ResourceKind.BASELINE: _validate_baseline_references,
    ResourceKind.APPLICATION: _validate_application_references,
    ResourceKind.COMPONENT: _validate_component_references,
    ResourceKind.DOMAIN: _validate_domain_references,
    ResourceKind.LOGGING_STACK: _validate_logging_references,
    ResourceKind.SSH_PUBLIC_KEY: _validate_ssh_public_key_references,
    ResourceKind.SERVICE: _validate_service_references,
    ResourceKind.ALERT_RULE: _validate_alert_rule_references,
    ResourceKind.ALERT_CHANNEL: _validate_alert_channel_references,
    ResourceKind.OPERATOR_POLICY: _validate_operator_policy_references,
}
