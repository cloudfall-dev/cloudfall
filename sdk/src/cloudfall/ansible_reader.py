"""Read a fleet from the team's own Ansible inventory.

Cloudfall declares no servers in a brownfield repository. The inventory is
the config: hosts, groups and connection settings come from Ansible, and
everything Ansible does not model comes from a ``cloudfall`` block in the
team's own ``host_vars``, with shared defaults in ``cloudfall_defaults``
and the baseline catalog in ``cloudfall_baselines``.

Ansible replaces rather than merges a variable of the same name, so a
``cloudfall`` block in ``group_vars`` would silently lose to one in
``host_vars``. The two names keep that from happening: ``cloudfall`` is
per host, ``cloudfall_defaults`` is per group, and Cloudfall merges them
itself with the host winning.

The documents this builds are the same shape as the YAML a greenfield
project holds, so they go through the same schemas and the same reference
checks, and every consumer of ``PlatformInventory`` works unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cloudfall.ansible_api import AnsibleReadError
from cloudfall.domain import RawDocument, ResourceKind, SourceLocation
from cloudfall.validation import SchemaCatalog, StateValidator

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from cloudfall.ansible_api import AnsibleHost, AnsibleInventory
    from cloudfall.validation import ValidatedConfig

API_VERSION = "cloudfall/v1"
"""Envelope version of the documents the reader builds."""

DECLARATION_VARIABLE = "cloudfall"
"""Host variable holding one host's Cloudfall declaration."""

DEFAULTS_VARIABLE = "cloudfall_defaults"
"""Group variable holding declaration defaults a host may override."""

CATALOG_VARIABLE = "cloudfall_baselines"
"""Group variable holding the baseline catalog the hosts point into."""

LEGACY_CATALOG_VARIABLE = "cloudfall_server_types"
"""Deprecated name of ``cloudfall_baselines``, read until 0.9.0 (D-11).

A baseline both variables hold must be defined the same way in each."""

_ADDRESS_VARIABLE = "ansible_host"
_SSH_USER_VARIABLE = "ansible_user"
_SSH_PORT_VARIABLE = "ansible_port"
_DEFAULT_SSH_PORT = 22
_DESCRIPTION_FIELD = "description"
_LABELS_FIELD = "labels"
_HOSTNAME_FIELD = "hostname"
_SNAKE_BOUNDARY = re.compile(r"_([a-z0-9])")
_HOST_VARS_SUFFIXES = (".yml", ".yaml")

_ERROR_DECLARATION_INVALID = "ansible_declaration_invalid"
_ERROR_CATALOG_INVALID = "ansible_catalog_invalid"
_ERROR_CATALOG_CONFLICT = "ansible_catalog_conflict"
_ERROR_NO_DECLARATIONS = "ansible_no_declarations"

# `labels` holds the team's own keys, which are data rather than field
# names, so the snake-to-camel rewrite must not touch inside it.
_OPAQUE_FIELDS = frozenset({_LABELS_FIELD})


@dataclass(frozen=True, slots=True)
class FleetRead:
    """What one inventory yielded: a validated fleet, and what it skipped."""

    config: ValidatedConfig
    managed: tuple[str, ...]
    """Hosts that carry a ``cloudfall`` declaration, in inventory order."""

    unmanaged: tuple[str, ...]
    """Hosts the inventory holds that Cloudfall was not told to manage."""

    in_process: bool
    """ansible-core's Python API served the read rather than the command."""

    def as_dict(self) -> dict[str, object]:
        """Serialize the read for system boundaries."""
        return {
            "status": "ok",
            "inProcess": self.in_process,
            "managed": list(self.managed),
            "unmanaged": list(self.unmanaged),
            "byKind": self.config.reported_counts_by_kind(),
            "resources": self.config.resource_count,
        }


def read_fleet(inventory: AnsibleInventory, schema_directory: Path) -> FleetRead:
    """Turn an Ansible inventory into validated Cloudfall resources."""
    managed = tuple(
        host for host in inventory.hosts if _declaration(host) is not None
    )
    unmanaged = tuple(
        str(host.name) for host in inventory.hosts if _declaration(host) is None
    )
    if not managed:
        message = (
            f"no host in {inventory.source} declares a `{DECLARATION_VARIABLE}` "
            "block, so there is no fleet to read"
        )
        raise AnsibleReadError(_ERROR_NO_DECLARATIONS, message)

    documents = [
        *_baseline_documents(managed, inventory.source.value),
        *(_server_document(host, inventory.source.value) for host in managed),
    ]
    validator = StateValidator(SchemaCatalog(schema_directory))
    return FleetRead(
        config=validator.validate_documents(documents),
        managed=tuple(str(host.name) for host in managed),
        unmanaged=unmanaged,
        in_process=inventory.in_process,
    )


def _server_document(host: AnsibleHost, source: Path) -> RawDocument:
    declaration = _declaration(host)
    if declaration is None:  # pragma: no cover - callers filter first
        message = f"host {host.name} declares no `{DECLARATION_VARIABLE}` block"
        raise AnsibleReadError(_ERROR_DECLARATION_INVALID, message)
    declared = _camelized(_merged_declaration(host, declaration))
    spec: dict[str, object] = {
        key: value
        for key, value in declared.items()
        if key not in {_DESCRIPTION_FIELD, _HOSTNAME_FIELD}
    }
    spec[_HOSTNAME_FIELD] = declared.get(_HOSTNAME_FIELD, str(host.name))
    spec["address"] = _address(host)
    spec["ssh"] = _ssh(host)
    metadata: dict[str, object] = {"id": str(host.name)}
    description = declared.get(_DESCRIPTION_FIELD)
    if description is not None:
        metadata[_DESCRIPTION_FIELD] = description
    content = {
        "apiVersion": API_VERSION,
        "kind": ResourceKind.SERVER.value,
        "metadata": metadata,
        "spec": spec,
    }
    return RawDocument(content=content, source=_host_source(host, source))


def _baseline_documents(
    hosts: tuple[AnsibleHost, ...], source: Path
) -> tuple[RawDocument, ...]:
    catalog: dict[str, Mapping[str, object]] = {}
    for host in hosts:
        for variable in (CATALOG_VARIABLE, LEGACY_CATALOG_VARIABLE):
            _read_catalog(host, variable, catalog)
    return tuple(
        RawDocument(
            content=_baseline_content(identifier, catalog[identifier]),
            source=SourceLocation(path=source, document_number=0),
        )
        for identifier in sorted(catalog)
    )


def _read_catalog(
    host: AnsibleHost, variable: str, catalog: dict[str, Mapping[str, object]]
) -> None:
    raw = host.variable(variable)
    if raw is None:
        return
    if not isinstance(raw, dict):
        message = (
            f"`{variable}` on host {host.name} must be a mapping of "
            f"baseline name to definition, got {type(raw).__name__}"
        )
        raise AnsibleReadError(_ERROR_CATALOG_INVALID, message)
    for name, definition in raw.items():
        if not isinstance(definition, dict):
            message = (
                f"`{variable}.{name}` must be a mapping, got "
                f"{type(definition).__name__}"
            )
            raise AnsibleReadError(_ERROR_CATALOG_INVALID, message)
        identifier = str(name)
        existing = catalog.get(identifier)
        if existing is not None and existing != definition:
            message = (
                f"baseline {identifier} is defined differently for "
                f"host {host.name} than for an earlier host or catalog "
                "variable; one catalog must describe the fleet"
            )
            raise AnsibleReadError(_ERROR_CATALOG_CONFLICT, message)
        catalog[identifier] = definition


def _baseline_content(
    identifier: str, definition: Mapping[str, object]
) -> Mapping[str, object]:
    declared = _camelized(definition)
    spec = {
        key: value for key, value in declared.items() if key != _DESCRIPTION_FIELD
    }
    metadata: dict[str, object] = {"id": identifier}
    description = declared.get(_DESCRIPTION_FIELD)
    if description is not None:
        metadata[_DESCRIPTION_FIELD] = description
    return {
        "apiVersion": API_VERSION,
        "kind": ResourceKind.BASELINE.value,
        "metadata": metadata,
        "spec": spec,
    }


def _declaration(host: AnsibleHost) -> Mapping[str, object] | None:
    raw = host.variable(DECLARATION_VARIABLE)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        message = (
            f"`{DECLARATION_VARIABLE}` on host {host.name} must be a mapping, "
            f"got {type(raw).__name__}"
        )
        raise AnsibleReadError(_ERROR_DECLARATION_INVALID, message)
    return {str(key): value for key, value in raw.items()}


def _merged_declaration(
    host: AnsibleHost, declaration: Mapping[str, object]
) -> Mapping[str, object]:
    defaults = host.variable(DEFAULTS_VARIABLE)
    if defaults is None:
        return declaration
    if not isinstance(defaults, dict):
        message = (
            f"`{DEFAULTS_VARIABLE}` reaching host {host.name} must be a "
            f"mapping, got {type(defaults).__name__}"
        )
        raise AnsibleReadError(_ERROR_DECLARATION_INVALID, message)
    merged = {str(key): value for key, value in defaults.items()}
    merged.update(declaration)
    return merged


def _address(host: AnsibleHost) -> object:
    declared = host.variable(_ADDRESS_VARIABLE)
    return declared if declared is not None else str(host.name)


def _ssh(host: AnsibleHost) -> Mapping[str, object]:
    user = host.variable(_SSH_USER_VARIABLE)
    port = host.variable(_SSH_PORT_VARIABLE)
    ssh: dict[str, object] = {"port": port if port is not None else _DEFAULT_SSH_PORT}
    if user is not None:
        ssh["user"] = user
    return ssh


def _host_source(host: AnsibleHost, source: Path) -> SourceLocation:
    """Point errors at the host's own file when the inventory has one."""
    for suffix in _HOST_VARS_SUFFIXES:
        candidate = source / "host_vars" / f"{host.name}{suffix}"
        if candidate.is_file():
            return SourceLocation(path=candidate, document_number=1)
    return SourceLocation(path=source, document_number=0)


def _camelized(value: Mapping[str, object]) -> Mapping[str, object]:
    """Rewrite Ansible's snake_case field names as the schemas' camelCase."""
    return {
        _camel(str(key)): _camelized_value(str(key), item)
        for key, item in value.items()
    }


def _camelized_value(key: str, value: object) -> object:
    if key in _OPAQUE_FIELDS:
        return value
    if isinstance(value, dict):
        return _camelized({str(item): entry for item, entry in value.items()})
    if isinstance(value, list):
        return [_camelized_value(key, item) for item in value]
    return value


def _camel(name: str) -> str:
    return _SNAKE_BOUNDARY.sub(lambda match: match.group(1).upper(), name)
