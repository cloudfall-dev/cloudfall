"""Load and validate read-only server observation snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from jsonschema.exceptions import ValidationError

from cloudfall.domain import ResourceId, SourceLocation
from cloudfall.validation import (
    BASELINE_FIELD,
    LEGACY_BASELINE_FIELD,
    ConfigValidationError,
    SchemaCatalog,
    ValidationIssue,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_OBSERVED_SERVER_SCHEMA = "observed-server.schema.json"
ERROR_OBSERVATION_BASELINE_CONFLICT = "observation_baseline_conflict"


@dataclass(frozen=True, slots=True)
class ObservedServerSnapshot:
    """One schema-validated observation of a managed server."""

    server_id: ResourceId
    baseline_id: ResourceId
    content: Mapping[str, object]
    source: Path

    @property
    def spec(self) -> Mapping[str, object]:
        """Return the validated observation payload."""
        return _mapping(self.content, "spec")


@dataclass(frozen=True, slots=True)
class ObservationSet:
    """Validated observations indexed by server identifier."""

    snapshots: tuple[ObservedServerSnapshot, ...]
    _index: Mapping[ResourceId, ObservedServerSnapshot]

    def for_server(self, server_id: ResourceId) -> ObservedServerSnapshot | None:
        """Return the latest supplied observation for a server, if present."""
        return self._index.get(server_id)


def load_observations(
    observation_directory: Path, schema_directory: Path
) -> ObservationSet:
    """Load and strictly validate JSON snapshots from a directory."""
    if not observation_directory.is_dir():
        issue = ValidationIssue(
            code="observation_directory_missing",
            message=(
                "observation directory does not exist: "
                f"{observation_directory}"
            ),
        )
        raise ConfigValidationError(issue)

    catalog = SchemaCatalog(schema_directory)
    snapshots: list[ObservedServerSnapshot] = []
    index: dict[ResourceId, ObservedServerSnapshot] = {}
    for path in sorted(observation_directory.rglob("*.json")):
        source = SourceLocation(path=path, document_number=1)
        content = _current_names(_load_json(path), source)
        try:
            catalog.validate_named(_OBSERVED_SERVER_SCHEMA, content)
        except ValidationError as error:
            issue = ValidationIssue(
                code="observation_schema_validation_failed",
                message=error.message,
                source=source,
                field_path=tuple(error.absolute_path),
            )
            raise ConfigValidationError(issue) from error

        metadata = _mapping(content, "metadata")
        spec = _mapping(content, "spec")
        server_id = ResourceId.from_boundary(spec.get("server"))
        metadata_id = ResourceId.from_boundary(metadata.get("id"))
        if metadata_id != server_id:
            issue = ValidationIssue(
                code="observation_identity_mismatch",
                message=(
                    f"metadata.id {metadata_id} does not match "
                    f"spec.server {server_id}"
                ),
                source=source,
            )
            raise ConfigValidationError(issue)
        if server_id in index:
            issue = ValidationIssue(
                code="observation_duplicate",
                message=(
                    f"server {server_id} already observed at "
                    f"{index[server_id].source}"
                ),
                source=source,
            )
            raise ConfigValidationError(issue)

        snapshot = ObservedServerSnapshot(
            server_id=server_id,
            baseline_id=ResourceId.from_boundary(spec.get("baseline")),
            content=content,
            source=path,
        )
        snapshots.append(snapshot)
        index[server_id] = snapshot

    return ObservationSet(snapshots=tuple(snapshots), _index=index)


def _current_names(
    content: Mapping[str, object], source: SourceLocation
) -> Mapping[str, object]:
    """Read a snapshot recorded before 0.8.0, whose spec says ``serverType``.

    The inspect role wrote ``spec.serverType`` until the Baseline rename
    (D-11); such a snapshot is read as ``spec.baseline`` until 0.9.0.
    """
    spec = content.get("spec")
    if not isinstance(spec, dict) or LEGACY_BASELINE_FIELD not in spec:
        return content
    if BASELINE_FIELD in spec:
        issue = ValidationIssue(
            code=ERROR_OBSERVATION_BASELINE_CONFLICT,
            message=(
                f"spec sets both {BASELINE_FIELD} and {LEGACY_BASELINE_FIELD}, "
                f"its deprecated name; a snapshot carries {BASELINE_FIELD} only"
            ),
            source=source,
            field_path=("spec", LEGACY_BASELINE_FIELD),
        )
        raise ConfigValidationError(issue)
    renamed = {
        (BASELINE_FIELD if key == LEGACY_BASELINE_FIELD else key): value
        for key, value in cast("Mapping[str, object]", spec).items()
    }
    return {**content, "spec": renamed}


def _load_json(path: Path) -> Mapping[str, object]:
    try:
        raw = cast("object", json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as error:
        issue = ValidationIssue(
            code="observation_json_invalid",
            message=error.msg,
            source=SourceLocation(path=path, document_number=1),
        )
        raise ConfigValidationError(issue) from error
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        issue = ValidationIssue(
            code="observation_shape_invalid",
            message="observation root must be an object",
            source=SourceLocation(path=path, document_number=1),
        )
        raise ConfigValidationError(issue)
    return cast("Mapping[str, object]", raw)


def _mapping(content: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = content.get(key)
    if not isinstance(value, dict) or not all(
        isinstance(child_key, str) for child_key in value
    ):
        message = f"validated field {key!r} is not an object"
        raise TypeError(message)
    return cast("Mapping[str, object]", value)
