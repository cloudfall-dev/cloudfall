"""Resolve declared secret references into component environment files.

Values live sops-encrypted in a private repository and are decrypted only
on the controller, at render time, into a single 0600 environment file per
component. State carries references, receipts carry hashes and key names;
a secret value never appears in state, output envelopes, or the agent
surface.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import stringprep
import subprocess
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_bytes
from typing import TYPE_CHECKING, cast

from cloudfall.domain import (
    AbsolutePath,
    PostgresRoleName,
    ResourceId,
    SecretKeyName,
)
from cloudfall.inventory import (
    OwnedPostgresDatabase,
    PlatformInventory,
    SecretValueReference,
)
from cloudfall.validation import SchemaCatalog, validate_config

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from cloudfall.inventory import ComponentInventory, SecretReference
    from cloudfall.lifecycle import EngineContext

ERROR_SOPS_MISSING = "secrets_sops_missing"
ERROR_KEY_MISSING = "secrets_key_missing"
_ERROR_OWNER_CONFLICT = "secrets_owner_conflict"
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

    def source_path(
        self, reference: SecretReference | SecretValueReference
    ) -> Path:
        """Return the encrypted file backing one reference."""
        relative = reference.path.value.lstrip("/")
        return (
            self.secrets_directory
            / reference.environment.value
            / f"{relative}.env"
        )

    def fetch_value(self, reference: SecretValueReference) -> str:
        """Decrypt one reference's fragment and return its one key's value.

        A missing file, a missing key, or an empty value fails naming the
        expected path and key, never a value.
        """
        source = self.source_path(reference)
        key = reference.key.value
        if not source.is_file():
            message = (
                f"secret source does not exist: expected {source} holding key "
                f"{key}"
            )
            raise SecretsError(_ERROR_SOURCE_MISSING, message)
        text = (
            self.decrypt(source)
            if self.decrypt is not None
            else _sops_decrypt(source)
        )
        values = [value for name, value in _parse_dotenv(text, source) if name == key]
        if not values:
            message = f"secret key {key} is missing from {source}"
            raise SecretsError(ERROR_KEY_MISSING, message)
        if len(values) > 1:
            message = f"secret key {key} appears more than once in {source}"
            raise SecretsError(_ERROR_SOURCE_INVALID, message)
        if not values[0]:
            message = f"secret key {key} in {source} holds an empty value"
            raise SecretsError(ERROR_KEY_MISSING, message)
        return values[0]


@dataclass(frozen=True, slots=True)
class OwnedRolePassword:
    """The decrypted password of one owner role, held only in memory.

    ``value`` stays out of ``repr`` and out of ``as_dict``: what leaves this
    object is the reference and the value's SHA-256.
    """

    service: ResourceId
    owner: PostgresRoleName
    reference: SecretValueReference
    value: str = field(repr=False)

    @property
    def sha256(self) -> str:
        """Return the hex SHA-256 of the value, for receipts and answers."""
        return hashlib.sha256(self.value.encode("utf-8")).hexdigest()

    def record(self) -> OwnedRoleRecord:
        """Return what may be recorded of this password: never the value."""
        return OwnedRoleRecord(
            service=self.service,
            owner=self.owner,
            reference=self.reference,
            sha256=self.sha256,
        )

    def as_dict(self) -> dict[str, object]:
        """Serialize the reference, the key, and the hash; never the value."""
        return self.record().as_dict()


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class OwnedRoleRecord:
    """One owner role password a converge set, as answers and receipts name it."""

    service: ResourceId
    owner: PostgresRoleName
    reference: SecretValueReference
    sha256: str

    def __post_init__(self) -> None:
        """Hold a hex SHA-256 digest only."""
        if not _SHA256_PATTERN.fullmatch(self.sha256):
            message = f"not a hex SHA-256 digest: {self.sha256!r}"
            raise ValueError(message)

    def as_dict(self) -> dict[str, object]:
        """Serialize the reference, the key, and the hash."""
        return {
            "service": self.service.value,
            "owner": self.owner.value,
            "passwordRef": self.reference.as_dict(),
            "key": self.reference.key.value,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, content: Mapping[str, object]) -> OwnedRoleRecord:
        """Read one record back from a schema-validated receipt."""
        reference = content.get("passwordRef")
        if not isinstance(reference, dict):
            message = "owned role record has no passwordRef mapping"
            raise TypeError(message)
        return cls(
            service=ResourceId.from_boundary(content.get("service")),
            owner=PostgresRoleName.from_boundary(content.get("owner")),
            reference=SecretValueReference(
                environment=ResourceId.from_boundary(reference.get("environment")),
                path=AbsolutePath.from_boundary(reference.get("path")),
                key=SecretKeyName.from_boundary(reference.get("key")),
            ),
            sha256=str(content.get("sha256")),
        )


def owned_databases(
    inventory: PlatformInventory,
) -> tuple[tuple[ResourceId, OwnedPostgresDatabase], ...]:
    """Return every database owned by a declared role, with its service."""
    return tuple(
        (service.resource_id, database)
        for service in inventory.services
        if service.postgresql is not None
        for database in service.postgresql.databases
        if isinstance(database, OwnedPostgresDatabase)
    )


def owned_role_passwords(
    databases: Iterable[tuple[ResourceId, OwnedPostgresDatabase]],
    provider: SopsSecretProvider,
) -> tuple[OwnedRolePassword, ...]:
    """Decrypt the password of every owner role, once per service and role.

    One role named by two databases of a service must name one reference.
    """
    passwords: dict[tuple[ResourceId, PostgresRoleName], OwnedRolePassword] = {}
    for service_id, database in databases:
        slot = (service_id, database.owner)
        known = passwords.get(slot)
        if known is not None:
            if known.reference != database.password_ref:
                message = (
                    f"role {database.owner.value} of service {service_id.value} "
                    "names two passwordRefs; every database it owns must name "
                    "the same one"
                )
                raise SecretsError(_ERROR_OWNER_CONFLICT, message)
            continue
        passwords[slot] = OwnedRolePassword(
            service=service_id,
            owner=database.owner,
            reference=database.password_ref,
            value=provider.fetch_value(database.password_ref),
        )
    return tuple(passwords.values())


_SCRAM_ITERATIONS = 4096
_SCRAM_SALT_BYTES = 16


def scram_sha256_verifier(password: str, *, salt: bytes | None = None) -> str:
    """Return the SCRAM-SHA-256 verifier PostgreSQL would store for a password.

    The format is PostgreSQL's own,
    ``SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>`` in base64,
    with 4096 iterations and a fresh 16-byte salt, as PostgreSQL's defaults
    are. PostgreSQL stores a value of this shape as it is, so the password
    itself never reaches the server (D-9). The password is prepared with
    SASLprep as PostgreSQL prepares it, so a client logs in with it.
    """
    salt = token_bytes(_SCRAM_SALT_BYTES) if salt is None else salt
    if not salt:
        message = "a SCRAM-SHA-256 salt must not be empty"
        raise ValueError(message)
    salted = hashlib.pbkdf2_hmac(
        "sha256", _saslprep(password).encode("utf-8"), salt, _SCRAM_ITERATIONS
    )
    stored_key = hashlib.sha256(hmac.digest(salted, b"Client Key", "sha256")).digest()
    server_key = hmac.digest(salted, b"Server Key", "sha256")
    return (
        f"SCRAM-SHA-256${_SCRAM_ITERATIONS}:{_b64(salt)}"
        f"${_b64(stored_key)}:{_b64(server_key)}"
    )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


_PROHIBITED = (
    stringprep.in_table_c12,
    stringprep.in_table_c21,
    stringprep.in_table_c22,
    stringprep.in_table_c3,
    stringprep.in_table_c4,
    stringprep.in_table_c5,
    stringprep.in_table_c6,
    stringprep.in_table_c7,
    stringprep.in_table_c8,
    stringprep.in_table_c9,
    stringprep.in_table_a1,
)


def _saslprep(password: str) -> str:
    """Prepare a password as PostgreSQL's ``pg_saslprep`` does (RFC 4013).

    An ASCII password stays as it is. Otherwise non-ASCII spaces map to a
    space, characters mapped to nothing drop, and the result is NFKC
    normalized; a result that is empty, holds a prohibited or unassigned
    character, or breaks the bidirectional rules is not used, and PostgreSQL
    falls back to the password as given, so this does too.
    """
    if password.isascii():
        return password
    # A space first, as PostgreSQL maps it: U+200B is in both tables.
    mapped = "".join(
        " " if stringprep.in_table_c12(char) else char
        for char in password
        if stringprep.in_table_c12(char) or not stringprep.in_table_b1(char)
    )
    prepared = unicodedata.normalize("NFKC", mapped)
    if not prepared or any(
        prohibited(char) for char in prepared for prohibited in _PROHIBITED
    ):
        return password
    if any(stringprep.in_table_d1(char) for char in prepared) and (
        any(stringprep.in_table_d2(char) for char in prepared)
        or not stringprep.in_table_d1(prepared[0])
        or not stringprep.in_table_d1(prepared[-1])
    ):
        return password
    return prepared


PASSWORD_FILES_VARIABLE = "cloudfall_services_password_files"  # noqa: S105 - a name
"""The ``services.yml`` variable mapping service, then owner, to a staged file."""


@contextmanager
def staged_password_files(
    passwords: tuple[OwnedRolePassword, ...],
) -> Iterator[dict[str, object]]:
    """Write each password's SCRAM verifier to a 0600 file, then remove it.

    The files sit in a private temp dir and hold the verifier only, never the
    password (D-9). Yields the extra vars ``services.yml`` reads: the
    controller paths only, so neither crosses a command line, the inventory,
    or the project. No passwords yields no variable, leaving the run as it was.
    """
    if not passwords:
        yield {}
        return
    with tempfile.TemporaryDirectory(prefix="cloudfall-passwords-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        files: dict[str, dict[str, str]] = {}
        for password in passwords:
            path = root / f"{password.service.value}-{password.owner.value}"
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(scram_sha256_verifier(password.value))
            files.setdefault(password.service.value, {})[password.owner.value] = str(
                path
            )
        yield {PASSWORD_FILES_VARIABLE: files}


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
