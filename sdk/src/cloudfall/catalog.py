"""The operations catalog: what an agent may run, and how it is verified.

Every playbook the team already runs becomes an operation declared in
``operations/`` beside it. An operation carries a risk level, a target
scope, a typed input schema, its preconditions and its verify step, and
the catalog those documents form is the agent's tool list. A playbook the
team has not declared is not an operation, so it does not exist to the
agent: the catalog is the boundary, not the filesystem.

The verify step is the field no generic harness fills in. "The playbook
ran" is what a job history records; an operation says which playbook
proves the fleet is healthy afterwards, which is what turns a run into an
outcome an audit entry can cite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from jsonschema.exceptions import ValidationError

from cloudfall.domain import ResourceId, SourceLocation
from cloudfall.validation import ConfigValidationError, SchemaCatalog, ValidationIssue

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

ERROR_OPERATION_UNDECLARED = "operation_undeclared"
"""Code of the issue ``OperationCatalog.get`` raises for an id it does not declare."""
CATALOG_DIRECTORY = "operations"
"""Directory holding one document per declared operation."""

OPERATION_SCHEMA = "operation.schema.json"
"""Schema every operation document is validated against."""

_INPUT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DOCUMENT_SUFFIXES = (".yml", ".yaml")
_HEALTH_PRECONDITION = "health"


class RiskLevel(StrEnum):
    """How far an operation's effects reach, as the gate reads it."""

    READ = "read"
    """Reads the fleet; runs freely."""

    MUTATING = "mutating"
    """Changes the fleet; check mode and approval first unless policy allows."""

    DESTRUCTIVE = "destructive"
    """Cannot be undone; always waits for a human."""


class TargetScope(StrEnum):
    """What an operation runs against."""

    FLEET = "fleet"
    """Every host the inventory declares to Cloudfall."""

    GROUP = "group"
    """One inventory group, named per call."""

    HOST = "host"
    """One host, named per call."""


class InputType(StrEnum):
    """Type of one declared operation input."""

    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"


@dataclass(frozen=True, slots=True)
class InputName:
    """Name of one operation input, as the caller passes it."""

    value: str

    def __post_init__(self) -> None:
        """Enforce a lowercase identifier an extra var can carry."""
        if not _INPUT_NAME_PATTERN.fullmatch(self.value):
            message = (
                "input name must be lowercase letters, digits and underscores "
                f"starting with a letter: {self.value!r}"
            )
            raise ValueError(message)

    def __str__(self) -> str:
        """Return the serialized name."""
        return self.value


@dataclass(frozen=True, slots=True)
class OperationInput:
    """One typed input an operation takes."""

    name: InputName
    type: InputType
    required: bool = True
    description: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Serialize the input for system boundaries."""
        result: dict[str, object] = {
            "name": str(self.name),
            "type": self.type.value,
            "required": self.required,
        }
        if self.description is not None:
            result["description"] = self.description
        return result


class Precondition(StrEnum):
    """A condition that must hold before an operation runs."""

    HEALTH_OK = "health:ok"
    """Every target's declared health check passes."""


@dataclass(frozen=True, slots=True)
class VerifyStep:
    """The playbook that proves an operation did what it claimed."""

    playbook: Path

    def as_dict(self) -> dict[str, object]:
        """Serialize the verify step for system boundaries."""
        return {"playbook": str(self.playbook)}


@dataclass(frozen=True, slots=True)
class Operation:
    """One declared operation: a playbook an agent may ask for by name."""

    operation_id: ResourceId
    playbook: Path
    risk: RiskLevel
    targets: TargetScope
    inputs: tuple[OperationInput, ...] = ()
    preconditions: tuple[Precondition, ...] = ()
    verify: VerifyStep | None = None
    description: str | None = None
    source: Path | None = None

    def as_dict(self) -> dict[str, object]:
        """Serialize the operation for system boundaries."""
        result: dict[str, object] = {
            "id": self.operation_id.value,
            "playbook": str(self.playbook),
            "risk": self.risk.value,
            "targets": self.targets.value,
            "inputs": [declared.as_dict() for declared in self.inputs],
            "preconditions": [
                condition.value for condition in self.preconditions
            ],
        }
        if self.verify is not None:
            result["verify"] = self.verify.as_dict()
        if self.description is not None:
            result["description"] = self.description
        if self.source is not None:
            result["source"] = str(self.source)
        return result


@dataclass(frozen=True, slots=True)
class OperationCatalog:
    """Every operation one repository declares, in identifier order."""

    operations: tuple[Operation, ...]
    directory: Path

    def get(self, operation_id: ResourceId) -> Operation:
        """Return one operation, or fail because the agent may not call it."""
        for operation in self.operations:
            if operation.operation_id == operation_id:
                return operation
        declared = ", ".join(
            operation.operation_id.value for operation in self.operations
        )
        issue = ValidationIssue(
            code=ERROR_OPERATION_UNDECLARED,
            message=(
                f"no operation {operation_id} is declared in {self.directory}; "
                f"declared operations: {declared or 'none'}"
            ),
        )
        raise ConfigValidationError(issue)

    def by_risk(self, risk: RiskLevel) -> tuple[Operation, ...]:
        """Return the operations at one risk level."""
        return tuple(
            operation for operation in self.operations if operation.risk is risk
        )

    def as_dict(self) -> dict[str, object]:
        """Serialize the catalog for system boundaries."""
        return {
            "status": "ok",
            "directory": str(self.directory),
            "operations": [operation.as_dict() for operation in self.operations],
            "byRisk": {
                risk.value: len(self.by_risk(risk)) for risk in RiskLevel
            },
        }


def load_catalog(
    repository: Path, schema_directory: Path, directory: Path | None = None
) -> OperationCatalog:
    """Read and validate every operation the repository declares.

    ``repository`` is the root the declared playbook paths are relative to:
    the Ansible repository in a brownfield checkout, the project directory
    otherwise. A declared playbook that does not exist is an error, because
    a catalog entry an agent cannot run is worse than no entry at all.
    """
    catalog_directory = (
        directory if directory is not None else repository / CATALOG_DIRECTORY
    )
    if not catalog_directory.is_dir():
        issue = ValidationIssue(
            code="operations_directory_missing",
            message=(
                f"no operations directory at {catalog_directory}: declare the "
                "playbooks an agent may run, one document each"
            ),
        )
        raise ConfigValidationError(issue)
    schemas = SchemaCatalog(schema_directory)
    operations = tuple(
        sorted(
            (
                _operation(path, schemas, repository)
                for path in _documents(catalog_directory)
            ),
            key=lambda operation: operation.operation_id.value,
        )
    )
    _reject_duplicates(operations, catalog_directory)
    return OperationCatalog(operations=operations, directory=catalog_directory)


def _documents(directory: Path) -> Iterable[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix in _DOCUMENT_SUFFIXES
    )


def _operation(path: Path, schemas: SchemaCatalog, repository: Path) -> Operation:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        issue = ValidationIssue(
            code="operation_shape_invalid",
            message=f"operation document must be an object with string keys: {path}",
        )
        raise ConfigValidationError(issue)
    document: Mapping[str, object] = raw
    try:
        schemas.validate_named(OPERATION_SCHEMA, document)
    except ValidationError as error:
        issue = ValidationIssue(
            code="schema_validation_failed",
            message=error.message,
            source=SourceLocation(path=path, document_number=1),
            field_path=tuple(str(part) for part in error.absolute_path),
        )
        raise ConfigValidationError(issue) from error
    playbook = _playbook(document, "playbook", path, repository)
    verify = document.get("verify")
    return Operation(
        operation_id=ResourceId.from_boundary(document["id"]),
        playbook=playbook,
        risk=RiskLevel(str(document["risk"])),
        targets=TargetScope(str(document["targets"])),
        inputs=_inputs(document.get("inputs")),
        preconditions=_preconditions(document.get("preconditions")),
        verify=(
            VerifyStep(_playbook(verify, "playbook", path, repository))
            if isinstance(verify, dict)
            else None
        ),
        description=(
            str(document["description"]) if "description" in document else None
        ),
        source=path,
    )


def _playbook(
    document: Mapping[str, object], field: str, source: Path, repository: Path
) -> Path:
    declared = Path(str(document[field]))
    if not (repository / declared).is_file():
        issue = ValidationIssue(
            code="operation_playbook_missing",
            message=(
                f"{source.name} declares {field} {declared}, which does not "
                f"exist under {repository}"
            ),
        )
        raise ConfigValidationError(issue)
    return declared


def _inputs(declared: object) -> tuple[OperationInput, ...]:
    if declared is None:
        return ()
    if not isinstance(declared, dict):  # pragma: no cover - schema rejects it
        return ()
    return tuple(
        _input(str(name), value) for name, value in sorted(declared.items())
    )


def _input(name: str, declared: object) -> OperationInput:
    if isinstance(declared, str):
        return OperationInput(name=InputName(name), type=InputType(declared))
    if not isinstance(declared, dict):  # pragma: no cover - schema rejects it
        message = f"input {name} must be a type name or an object"
        raise TypeError(message)
    description = declared.get("description")
    return OperationInput(
        name=InputName(name),
        type=InputType(str(declared["type"])),
        required=bool(declared.get("required", True)),
        description=str(description) if description is not None else None,
    )


def _preconditions(declared: object) -> tuple[Precondition, ...]:
    if declared is None:
        return ()
    if not isinstance(declared, list):  # pragma: no cover - schema rejects it
        return ()
    return tuple(
        Precondition.HEALTH_OK
        for entry in declared
        if isinstance(entry, dict) and _HEALTH_PRECONDITION in entry
    )


def _reject_duplicates(
    operations: tuple[Operation, ...], directory: Path
) -> None:
    seen: dict[str, Path] = {}
    for operation in operations:
        identifier = operation.operation_id.value
        first = seen.get(identifier)
        if first is not None:
            issue = ValidationIssue(
                code="operation_duplicate",
                message=(
                    f"operation {identifier} is declared twice in {directory}: "
                    f"{first.name} and {operation.source}"
                ),
            )
            raise ConfigValidationError(issue)
        if operation.source is not None:
            seen[identifier] = operation.source
