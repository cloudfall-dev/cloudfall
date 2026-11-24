"""Kibana service values: the heap, a user's access, the roles it keeps (S-024).

Kibana holds no data and no version of its own: it reads one Elasticsearch
service of S-012, runs at that service's version, and its saved objects are
in that cluster's snapshots, so the kind declares no ``backup``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

HEAP_MB_MINIMUM = 1024
"""The smallest ``heapMb`` Kibana's Node.js process is given."""
HEAP_MB_MAXIMUM = 8192
"""The largest ``heapMb`` Kibana's Node.js process is given."""
VIEW_ROLE = "cloudfall-kibana-view"
"""The role a user of ``access: view`` gets: Kibana's base privilege ``read``."""
EDIT_ROLE = "cloudfall-kibana-edit"
"""The role a user of ``access: edit`` gets: Kibana's base privilege ``all``."""
ROLE_NAMES = frozenset({VIEW_ROLE, EDIT_ROLE})
"""The roles the Kibana role keeps; no Elasticsearch user may take their names."""
WARNING_PUBLISHED = "KIBANA_PUBLISHED"
"""Warning code of a Kibana a Domain publishes: its login form faces the internet."""


class KibanaEncryptionKey(StrEnum):
    """One of the three keys Kibana encrypts with, by the setting that names it.

    Each is generated once into the project's store and put into Kibana's
    keystore, never into ``kibana.yml`` (S-024, What the server needs).
    """

    SECURITY = "xpack.security.encryptionKey"
    SAVED_OBJECTS = "xpack.encryptedSavedObjects.encryptionKey"
    REPORTING = "xpack.reporting.encryptionKey"

    @property
    def requirement_name(self) -> str:
        """Return the name of the key's S-011 secret requirement."""
        return _KEY_NAMES[self][0]

    @property
    def store_key(self) -> str:
        """Return the key of ``/services/<id>`` in the store that holds the value."""
        return _KEY_NAMES[self][1]

    @property
    def staged_name(self) -> str:
        """Return the name ``services.yml`` is handed the staged file under."""
        return _KEY_NAMES[self][2]


_KEY_NAMES = {
    KibanaEncryptionKey.SECURITY: (
        "kibana-security-encryption-key",
        "SECURITY_ENCRYPTION_KEY",
        "securityKey",
    ),
    KibanaEncryptionKey.SAVED_OBJECTS: (
        "kibana-saved-objects-encryption-key",
        "SAVED_OBJECTS_ENCRYPTION_KEY",
        "savedObjectsKey",
    ),
    KibanaEncryptionKey.REPORTING: (
        "kibana-reporting-encryption-key",
        "REPORTING_ENCRYPTION_KEY",
        "reportingKey",
    ),
}


class KibanaAccess(StrEnum):
    """What a user of the Elasticsearch service may do in Kibana."""

    VIEW = "view"
    EDIT = "edit"

    @property
    def role(self) -> str:
        """Return the Kibana role this access gives."""
        return VIEW_ROLE if self is KibanaAccess.VIEW else EDIT_ROLE

    @classmethod
    def from_boundary(cls, value: object) -> KibanaAccess:
        """Coerce a boundary value into a Kibana access."""
        return cls(_string(value, "Kibana access"))


@dataclass(frozen=True, slots=True)
class KibanaHeapMb:
    """``--max-old-space-size`` of Kibana's Node.js process, in MiB."""

    value: int

    def __post_init__(self) -> None:
        """Enforce the range spec 024 allows."""
        if isinstance(self.value, bool) or not (
            HEAP_MB_MINIMUM <= self.value <= HEAP_MB_MAXIMUM
        ):
            message = (
                f"Kibana heapMb must be {HEAP_MB_MINIMUM} to {HEAP_MB_MAXIMUM}, "
                f"got {self.value!r}"
            )
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> KibanaHeapMb:
        """Coerce a boundary value into a heap size."""
        if not isinstance(value, int) or isinstance(value, bool):
            message = f"Kibana heapMb must be an integer, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value)


def _string(value: object, description: str) -> str:
    if not isinstance(value, str):
        message = f"{description} must be a string, got {type(value).__name__}"
        raise TypeError(message)
    return value
