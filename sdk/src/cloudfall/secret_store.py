"""The secrets provider of S-011: keep requirement secrets in the project, encrypted.

A secret requirement names a key in a sops-encrypted dotenv file under
``secrets/``. sops leaves dotenv key names in clear and encrypts each value,
so whether a key is stored is read from the file without decrypting it, and
``requirements show`` needs no key. Writing decrypts the file when it exists,
adds one key, and encrypts the whole file again through sops and the
project's ``.sops.yaml``. A stored key is never overwritten.

Values live in memory only while they are written; what leaves this module
is the reference and the value's SHA-256.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cloudfall.requirements import (
    RequirementState,
    RequirementType,
    SecretRequirement,
    SecretSource,
    ServerRequirements,
)
from cloudfall.secrets import (
    ERROR_SOPS_MISSING,
    SecretsError,
    SopsSecretProvider,
    parse_dotenv,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from cloudfall.inventory import SecretValueReference

ERROR_ENCRYPT_FAILED = "secrets_encrypt_failed"
ERROR_KEY_EXISTS = "secrets_key_exists"
ERROR_VALUE_INVALID = "secrets_value_invalid"
ERROR_STORE_INVALID = "secrets_store_invalid"
GENERATED_BYTES = 32
"""Random bytes in a generated value, written URL-safe base64 (43 characters)."""
_SOPS_METADATA_PREFIX = "sops_"
_STORE_MODE = 0o644
"""Encrypted files are committed, like every other fragment under ``secrets/``."""


@dataclass(frozen=True, slots=True)
class StoredSecret:
    """What may be said about a secret just written: never the value."""

    reference: SecretValueReference
    sha256: str

    def as_dict(self) -> dict[str, object]:
        """Serialize the reference, its key and the value's SHA-256."""
        return {
            "reference": self.reference.as_dict(),
            "key": self.reference.key.value,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class SecretStore:
    """Reads and writes requirement secrets in a sops-encrypted secrets directory."""

    secrets_directory: Path
    decrypt: Callable[[Path], str]
    """Decrypts one file; ``secrets.sops_decrypt`` outside a command."""
    encrypt: Callable[[Path, str], str]
    """Encrypts dotenv text for one target file; ``sops_encrypt`` outside a command."""

    def source_path(self, reference: SecretValueReference) -> Path:
        """Return the encrypted file that holds ``reference``."""
        return SopsSecretProvider(self.secrets_directory).source_path(reference)

    def holds(self, reference: SecretValueReference) -> bool:
        """Return whether the store has the key, reading key names in clear."""
        source = self.source_path(reference)
        if not source.is_file():
            return False
        return reference.key.value in _stored_keys(source)

    def state(self, requirement: SecretRequirement) -> RequirementState:
        """Return ``met``, or what meeting the requirement takes."""
        if self.holds(requirement.reference):
            return RequirementState.MET
        if requirement.source is SecretSource.GENERATE:
            return RequirementState.WILL_CREATE
        return RequirementState.NEEDS_INPUT

    def write(self, reference: SecretValueReference, value: str) -> StoredSecret:
        """Add one key to its encrypted file; refuse a key already stored."""
        _check_value(reference, value)
        source = self.source_path(reference)
        pairs: tuple[tuple[str, str], ...] = ()
        if source.is_file():
            pairs = parse_dotenv(self.decrypt(source), source)
        if any(name == reference.key.value for name, _ in pairs):
            message = (
                f"secret key {reference.key.value} is already stored in {source}; "
                "nothing was overwritten"
            )
            raise SecretsError(ERROR_KEY_EXISTS, message)
        plaintext = "".join(
            f"{name}={item}\n" for name, item in (*pairs, (reference.key.value, value))
        )
        _replace(source, self.encrypt(source, plaintext))
        return StoredSecret(
            reference, hashlib.sha256(value.encode("utf-8")).hexdigest()
        )

    def generate(self, reference: SecretValueReference) -> StoredSecret:
        """Write a fresh random value for ``reference``."""
        return self.write(reference, secrets.token_urlsafe(GENERATED_BYTES))


def secret_requirements(
    servers: Iterable[ServerRequirements],
) -> tuple[SecretRequirement, ...]:
    """Return every secret requirement once, in server and requirement order."""
    seen: dict[tuple[str, str, str], SecretRequirement] = {}
    for server in servers:
        for requirement in server.requirements:
            spec = requirement.spec
            if not isinstance(spec, SecretRequirement):
                continue
            reference = spec.reference
            seen.setdefault(
                (
                    reference.environment.value,
                    reference.path.value,
                    reference.key.value,
                ),
                spec,
            )
    return tuple(seen.values())


def requirement_states(
    servers: Iterable[ServerRequirements], store: SecretStore
) -> list[dict[str, object]]:
    """Serialize every server's requirements with the state its provider sees."""
    answered: list[dict[str, object]] = []
    for server in servers:
        items: list[dict[str, object]] = []
        for requirement in server.requirements:
            spec = requirement.spec
            state = (
                store.state(spec)
                if isinstance(spec, SecretRequirement)
                else RequirementState.UNCHECKED
            )
            items.append({**requirement.as_dict(), "state": state.value})
        answered.append({"server": server.server_id.value, "requirements": items})
    return answered


def needs_input(answered: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    """Return the requirements a person must supply, from ``requirement_states``."""
    missing: list[dict[str, object]] = []
    for server in answered:
        requirements = server["requirements"]
        if not isinstance(requirements, list):
            message = "requirement states carry no requirements list"
            raise TypeError(message)
        missing.extend(
            {"server": server["server"], **item}
            for item in requirements
            if item["state"] == RequirementState.NEEDS_INPUT.value
            and item["type"] == RequirementType.SECRET.value
        )
    return missing


def sops_encrypt_argv(binary: str, target: Path) -> list[str]:
    """Return the sops call that encrypts dotenv on stdin by ``target``'s creation rule.

    The plaintext travels on stdin, never on a command line or in a file;
    run it from ``sops_root(target)`` so sops finds ``.sops.yaml``.
    """
    return [
        binary,
        "--encrypt",
        "--input-type",
        "dotenv",
        "--output-type",
        "dotenv",
        "--filename-override",
        str(target.resolve()),
        "/dev/stdin",
    ]


def sops_binary() -> str:
    """Return the sops binary, or fail naming what to install."""
    binary = shutil.which("sops")
    if binary is None:
        message = (
            "the sops binary is not installed; install sops and age, and "
            "set SOPS_AGE_KEY_FILE to your age key"
        )
        raise SecretsError(ERROR_SOPS_MISSING, message)
    return binary


def sops_root(target: Path) -> Path:
    """Return the nearest directory above ``target`` that has a ``.sops.yaml``."""
    for directory in target.resolve().parents:
        if (directory / ".sops.yaml").is_file():
            return directory
    message = f"no .sops.yaml above {target}; sops has no rule to encrypt it with"
    raise SecretsError(ERROR_STORE_INVALID, message)


def encrypt_failed(target: Path, stderr: str) -> SecretsError:
    """Return the failure of one sops encryption, naming the file, never a value."""
    message = f"sops could not encrypt {target}: {stderr.strip()[:300]}"
    return SecretsError(ERROR_ENCRYPT_FAILED, message)


def sops_encrypt(target: Path, plaintext: str) -> str:
    """Encrypt dotenv ``plaintext`` by ``target``'s rule in ``.sops.yaml``."""
    completed = subprocess.run(  # noqa: S603 - fixed binary and arguments
        sops_encrypt_argv(sops_binary(), target),
        input=plaintext,
        capture_output=True,
        text=True,
        check=False,
        cwd=sops_root(target),
    )
    if completed.returncode != 0:
        raise encrypt_failed(target, completed.stderr)
    return completed.stdout


def _stored_keys(source: Path) -> frozenset[str]:
    names = {
        name
        for name, _ in parse_dotenv(source.read_text(encoding="utf-8"), source)
        if not name.startswith(_SOPS_METADATA_PREFIX)
    }
    return frozenset(names)


def _check_value(reference: SecretValueReference, value: str) -> None:
    if not value or value != value.strip() or "\n" in value or "\r" in value:
        message = (
            f"the value for {reference.key.value} must be one line with no "
            "leading or trailing whitespace"
        )
        raise SecretsError(ERROR_VALUE_INVALID, message)


def _replace(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically, creating its directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    Path(temporary).chmod(_STORE_MODE)
    Path(temporary).replace(target)
