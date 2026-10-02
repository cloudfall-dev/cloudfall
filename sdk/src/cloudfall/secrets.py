"""Resolve declared secret references into component environment files.

Values live sops-encrypted in a private repository and are decrypted only
on the controller, at render time, into a single 0600 environment file per
component. State carries references, receipts carry hashes and key names;
a secret value never appears in state, output envelopes, or the agent
surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from cloudfall.inventory import PlatformInventory
from cloudfall.validation import SchemaCatalog, validate_config

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cloudfall.domain import ResourceId
    from cloudfall.inventory import ComponentInventory, SecretReference
    from cloudfall.lifecycle import EngineContext

ERROR_SOPS_MISSING = "secrets_sops_missing"
_ERROR_SOURCE_MISSING = "secrets_source_missing"
_ERROR_SOURCE_INVALID = "secrets_source_invalid"
_ERROR_COMPONENT_UNKNOWN = "secrets_component_unknown"
ERROR_DECRYPT_FAILED = "secrets_decrypt_failed"
_ERROR_KEY_CONFLICT = "secrets_key_conflict"
_RECEIPT_SCHEMA = "environment-receipt.schema.json"
_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FILE_MODE = 0o600


class SecretsError(RuntimeError):
    """Structured secrets failure that never carries a value."""

    def __init__(self, code: str, message: str) -> None:
        """Capture a stable error code alongside the human message."""
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, object]:
        """Serialize the failure for structured output."""
        return {
            "status": "error",
            "error": {"code": self.code, "message": self.message},
        }


@dataclass(frozen=True, slots=True)
class SopsSecretProvider:
    """Reads dotenv fragments sops-encrypted in a secrets directory.

    The on-disk layout mirrors the reference itself:
    ``<secrets_directory>/<environment><path>.env`` — for example the
    reference ``{environment: production, path: /crm/backend}`` resolves
    from ``secrets/production/crm/backend.env``.
    """

    secrets_directory: Path
    decrypt: Callable[[Path], str] | None = None

    def fetch(
        self, reference: SecretReference
    ) -> tuple[tuple[str, str], ...]:
        """Decrypt one reference's fragment into ordered key pairs."""
        source = self.source_path(reference)
        if not source.is_file():
            message = (
                f"secret source does not exist: {source} (referenced as "
                f"scope={reference.scope.value} path={reference.path.value})"
            )
            raise SecretsError(_ERROR_SOURCE_MISSING, message)
        text = (
            self.decrypt(source)
            if self.decrypt is not None
            else _sops_decrypt(source)
        )
        return _parse_dotenv(text, source)

    def source_path(self, reference: SecretReference) -> Path:
        """Return the encrypted file backing one reference."""
        relative = reference.path.value.lstrip("/")
        return (
            self.secrets_directory
            / reference.environment.value
            / f"{relative}.env"
        )


def render_environment(
    context: EngineContext,
    component_id: ResourceId,
    provider: SopsSecretProvider,
    output_path: Path,
    receipt_directory: Path | None = None,
) -> dict[str, object]:
    """Resolve a component's references into one 0600 environment file."""
    state = validate_config(context.project_directory, context.schema_directory)
    inventory = PlatformInventory.from_state(state)
    component = _declared_component(inventory, component_id)
    application = next(
        application
        for application in inventory.applications
        if application.resource_id == component.application_id
    )
    references = (*application.secret_refs, *component.secret_refs)
    declared = dict(component.environment)
    merged: dict[str, str] = dict(declared)
    for reference in references:
        for key, value in provider.fetch(reference):
            if key in declared:
                message = (
                    f"{key} is declared in the component's non-secret "
                    "environment and also provided by the secret source "
                    f"{provider.source_path(reference)}; a key must live "
                    "in exactly one place"
                )
                raise SecretsError(_ERROR_KEY_CONFLICT, message)
            merged[key] = value
    content = "".join(f"{key}={value}\n" for key, value in merged.items())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    output_path.chmod(_FILE_MODE)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    envelope: dict[str, object] = {
        "status": "ok",
        "component": component_id.value,
        "environmentFile": str(output_path),
        "sha256": digest,
        "keys": sorted(merged),
        "declaredKeys": sorted(declared),
        "references": [reference.as_dict() for reference in references],
    }
    if receipt_directory is not None:
        envelope["receipt"] = str(
            _write_receipt(
                context, component_id, digest, sorted(merged),
                receipt_directory,
            )
        )
    return envelope


def _write_receipt(
    context: EngineContext,
    component_id: ResourceId,
    digest: str,
    keys: list[str],
    receipt_directory: Path,
) -> Path:
    document: dict[str, object] = {
        "apiVersion": "cloudfall/v1",
        "kind": "EnvironmentReceipt",
        "metadata": {
            "id": component_id.value,
            "description": "Rendered environment receipt",
        },
        "spec": {
            "component": component_id.value,
            "sha256": digest,
            "renderedAt": datetime.now(UTC).isoformat(timespec="seconds"),
            "keys": keys,
        },
    }
    SchemaCatalog(context.schema_directory).validate_named(
        _RECEIPT_SCHEMA, document
    )
    receipt_directory.mkdir(parents=True, exist_ok=True)
    path = receipt_directory / f"{component_id.value}.json"
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def load_environment_receipts(
    receipt_directory: Path, schema_directory: Path
) -> dict[str, str]:
    """Load rendered-environment hashes by component id."""
    if not receipt_directory.is_dir():
        return {}
    catalog = SchemaCatalog(schema_directory)
    hashes: dict[str, str] = {}
    for path in sorted(receipt_directory.glob("*.json")):
        document = cast(
            "dict[str, object]",
            json.loads(path.read_text(encoding="utf-8")),
        )
        catalog.validate_named(_RECEIPT_SCHEMA, document)
        spec = cast("dict[str, object]", document["spec"])
        hashes[cast("str", spec["component"])] = cast("str", spec["sha256"])
    return hashes


def _declared_component(
    inventory: PlatformInventory, component_id: ResourceId
) -> ComponentInventory:
    component = next(
        (
            candidate
            for candidate in inventory.components
            if candidate.resource_id == component_id
        ),
        None,
    )
    if component is None:
        message = f"declared component does not exist: {component_id.value}"
        raise SecretsError(_ERROR_COMPONENT_UNKNOWN, message)
    return component


def _sops_decrypt(source: Path) -> str:
    binary = shutil.which("sops")
    if binary is None:
        message = (
            "the sops binary is not installed; install sops and age, and "
            "set SOPS_AGE_KEY_FILE to your age key"
        )
        raise SecretsError(ERROR_SOPS_MISSING, message)
    completed = subprocess.run(  # noqa: S603 - fixed binary, declared file
        [binary, "--decrypt", str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        message = (
            f"sops could not decrypt {source}: "
            f"{completed.stderr.strip()[:300]}"
        )
        raise SecretsError(ERROR_DECRYPT_FAILED, message)
    return completed.stdout


def _parse_dotenv(text: str, source: Path) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not _KEY_PATTERN.fullmatch(key):
            message = (
                f"{source} line {line_number} is not a KEY=VALUE entry"
            )
            raise SecretsError(_ERROR_SOURCE_INVALID, message)
        pairs.append((key, value))
    return tuple(pairs)
