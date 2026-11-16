"""MariaDB service values: versions, the supported series, names (S-019).

The supported series live in one table, each with the date MariaDB's
maintenance policy gives for the end of its community binaries, taken when a
Cloudfall release is cut. A version outside the table fails validation; a
version of a series within ``VERSION_ENDING_WINDOW`` of its date, or past
it, validates with a warning. A series leaves the table in the next minor
release of Cloudfall.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

_VERSION_PART = r"(0|[1-9][0-9]{0,3})"
_VERSION_PATTERN = re.compile(rf"{_VERSION_PART}\.{_VERSION_PART}\.{_VERSION_PART}")
_DATABASE_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_USER_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_.]{0,63}")
_MAX_SERVER_ID = 4294967295

VERSION_ENDING_WINDOW = timedelta(days=90)
"""How long before a series' end-of-life date ``config validate`` warns."""
WARNING_VERSION_ENDING = "MARIADB_VERSION_ENDING"
"""Warning code of a service whose series ends within the window, or has ended."""
WARNING_REPLICATION_UNENCRYPTED = "MARIADB_REPLICATION_UNENCRYPTED"
"""Warning code of a replica of an outside source declared without a CA file."""

DEFAULT_SOURCE_PORT = 3306
"""The port of an outside source when its declaration names none."""
REPLICATION_USER = "cloudfall_repl"
"""The one replication account of a primary; its password is generated."""
METRICS_USER = "alloy"
"""The account the collector scrapes the server as, as on PostgreSQL."""
RESERVED_USER_NAMES = frozenset(
    {"root", "mysql", "mariadb.sys", REPLICATION_USER, METRICS_USER}
)
"""Accounts Debian's package, MariaDB or Cloudfall itself holds."""
SYSTEM_DATABASES = frozenset(
    {"information_schema", "mysql", "performance_schema", "sys"}
)
"""Databases every MariaDB server holds; none is declared."""


@dataclass(frozen=True, slots=True)
class MariadbVersion:
    """Exact ``X.Y.Z`` MariaDB release, installed and held at that version."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the exact three-part release syntax."""
        if not _VERSION_PATTERN.fullmatch(self.value):
            message = f"MariaDB version is not X.Y.Z: {self.value!r}"
            raise ValueError(message)

    @property
    def parts(self) -> tuple[int, int, int]:
        """Return the release as numbers, for ordering."""
        major, minor, patch = (int(part) for part in self.value.split("."))
        return major, minor, patch

    @property
    def series(self) -> str:
        """Return the series the release belongs to: ``11.8``."""
        major, minor, _ = self.parts
        return f"{major}.{minor}"

    def is_older_than(self, other: MariadbVersion) -> bool:
        """Return whether this release comes before ``other``."""
        return self.parts < other.parts

    @classmethod
    def from_boundary(cls, value: object) -> MariadbVersion:
        """Coerce a boundary value into a MariaDB version."""
        return cls(_string(value, "MariaDB version"))


@dataclass(frozen=True, slots=True)
class MariadbSeries:
    """One long-term series Cloudfall installs, with its end-of-life date."""

    major: int
    minor: int
    end_of_life: date

    @property
    def label(self) -> str:
        """Return the series as written in messages: ``11.8``."""
        return f"{self.major}.{self.minor}"

    def covers(self, version: MariadbVersion) -> bool:
        """Return whether ``version`` belongs to this series."""
        major, minor, _ = version.parts
        return (major, minor) == (self.major, self.minor)

    def is_ending(self, today: date) -> bool:
        """Return whether ``today`` is within the window of, or past, the date."""
        return today >= self.end_of_life - VERSION_ENDING_WINDOW


# MariaDB's maintenance policy (https://mariadb.org/about/#maintenance-policy),
# read 2026-11-16: the "Community" end-of-life column of the long-term
# release table, for each long-term series MariaDB still maintains.
SUPPORTED_SERIES: tuple[MariadbSeries, ...] = (
    MariadbSeries(major=10, minor=11, end_of_life=date(2028, 2, 16)),
    MariadbSeries(major=11, minor=4, end_of_life=date(2029, 5, 29)),
    MariadbSeries(major=11, minor=8, end_of_life=date(2028, 6, 4)),
    MariadbSeries(major=12, minor=3, end_of_life=date(2029, 6, 12)),
)


def supported_series(
    version: MariadbVersion,
    table: tuple[MariadbSeries, ...] = SUPPORTED_SERIES,
) -> MariadbSeries | None:
    """Return the supported series ``version`` belongs to, if any."""
    return next((series for series in table if series.covers(version)), None)


def supported_series_text(table: tuple[MariadbSeries, ...] = SUPPORTED_SERIES) -> str:
    """Return the supported series as one human-readable list."""
    return ", ".join(series.label for series in table)


class MariadbAuthentication(StrEnum):
    """The plugin an owner's password hash is for."""

    MYSQL_NATIVE_PASSWORD = "mysql_native_password"  # noqa: S105 - a plugin name
    ED25519 = "ed25519"

    def password_hash(self, password: str) -> str:
        """Return the hash this plugin stores for ``password``; never the password."""
        if self is MariadbAuthentication.ED25519:
            return ed25519_password_hash(password)
        return mysql_native_password_hash(password)

    @classmethod
    def from_boundary(cls, value: object) -> MariadbAuthentication:
        """Coerce a boundary value into an authentication plugin."""
        return cls(_string(value, "authentication"))


class MariadbReplicationRole(StrEnum):
    """A server's place in a pair; absent replication means standalone."""

    PRIMARY = "primary"
    REPLICA = "replica"

    @classmethod
    def from_boundary(cls, value: object) -> MariadbReplicationRole:
        """Coerce a boundary value into a replication role."""
        return cls(_string(value, "replication role"))


@dataclass(frozen=True, slots=True)
class MariadbDatabaseName:
    """Name of a declared MariaDB database."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the conservative naming the schema accepts."""
        if not _DATABASE_NAME_PATTERN.fullmatch(self.value):
            message = f"invalid MariaDB database name: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> MariadbDatabaseName:
        """Coerce a boundary value into a database name."""
        return cls(_string(value, "MariaDB database name"))


@dataclass(frozen=True, slots=True)
class MariadbUserName:
    """Name of a declared MariaDB user, the owner of one or more databases."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the conservative naming the schema accepts."""
        if not _USER_NAME_PATTERN.fullmatch(self.value):
            message = f"invalid MariaDB user name: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> MariadbUserName:
        """Coerce a boundary value into a user name."""
        return cls(_string(value, "MariaDB user name"))


@dataclass(frozen=True, slots=True)
class MariadbServerId:
    """``server_id`` of a server in a replication pair, 1 to 4294967295."""

    value: int

    def __post_init__(self) -> None:
        """Reject booleans and values outside MariaDB's range."""
        if isinstance(self.value, bool) or not 1 <= self.value <= _MAX_SERVER_ID:
            message = f"invalid MariaDB server id: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> MariadbServerId:
        """Coerce a boundary value into a server id."""
        if isinstance(value, bool) or not isinstance(value, int):
            message = f"server id must be an integer, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value)


MANAGED_ROLE = "cloudfall-managed"
"""The role Cloudfall grants each user it creates or adopts, as its marker.

It holds no privilege. A role travels with the server's grants, so a replica
promoted later still tells Cloudfall's users from those made by hand, and its
hyphen keeps it out of the names a declaration can use.
"""


def mysql_native_password_hash(password: str) -> str:
    """Return the ``mysql_native_password`` hash MariaDB stores for ``password``.

    ``*`` and the uppercase hex of SHA1(SHA1(password)), as ``PASSWORD()``
    computes it. It has no salt, so the same password gives the same hash and
    the role compares it with the stored one (D-9, D-25).
    """
    _require_password(password)
    inner = hashlib.sha1(password.encode("utf-8")).digest()  # noqa: S324 - MariaDB's format
    return "*" + hashlib.sha1(inner).hexdigest().upper()  # noqa: S324 - MariaDB's format


def ed25519_password_hash(password: str) -> str:
    """Return the ``ed25519`` hash MariaDB stores for ``password``.

    MariaDB's ``auth_ed25519`` keeps the Ed25519 public key whose secret
    scalar is the clamped first half of SHA-512(password), in base64 without
    padding, as ``ed25519_password()`` computes it. It has no salt either, so
    the role compares it with the stored one (D-9, D-25).
    """
    _require_password(password)
    digest = bytearray(hashlib.sha512(password.encode("utf-8")).digest()[:32])
    digest[0] &= 248
    digest[31] &= 63
    digest[31] |= 64
    point = _scalar_multiply(int.from_bytes(digest, "little"), _BASE_POINT)
    return base64.b64encode(_encode_point(point)).decode("ascii").rstrip("=")


def _require_password(password: str) -> None:
    if not password:
        message = "a MariaDB owner password must not be empty"
        raise ValueError(message)


# Ed25519 over edwards25519 in extended coordinates (RFC 8032, section 5.1).
_FIELD = 2**255 - 19
_CURVE_D = -121665 * pow(121666, _FIELD - 2, _FIELD) % _FIELD
type _Point = tuple[int, int, int, int]


def _recover_x(y: int) -> int:
    """Return the even x of the curve point with ``y`` (RFC 8032, 5.1.3)."""
    square = (y * y - 1) * pow(_CURVE_D * y * y + 1, _FIELD - 2, _FIELD) % _FIELD
    x = pow(square, (_FIELD + 3) // 8, _FIELD)
    if (x * x - square) % _FIELD:
        x = x * pow(2, (_FIELD - 1) // 4, _FIELD) % _FIELD
    return _FIELD - x if x % 2 else x


_BASE_Y = 4 * pow(5, _FIELD - 2, _FIELD) % _FIELD
_BASE_X = _recover_x(_BASE_Y)
_BASE_POINT: _Point = (_BASE_X, _BASE_Y, 1, _BASE_X * _BASE_Y % _FIELD)


def _add(first: _Point, second: _Point) -> _Point:
    x1, y1, z1, t1 = first
    x2, y2, z2, t2 = second
    a = (y1 - x1) * (y2 - x2) % _FIELD
    b = (y1 + x1) * (y2 + x2) % _FIELD
    c = t1 * 2 * _CURVE_D * t2 % _FIELD
    d = z1 * 2 * z2 % _FIELD
    e, f, g, h = b - a, d - c, d + c, b + a
    return e * f % _FIELD, g * h % _FIELD, f * g % _FIELD, e * h % _FIELD


def _scalar_multiply(scalar: int, point: _Point) -> _Point:
    result: _Point = (0, 1, 1, 0)
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _encode_point(point: _Point) -> bytes:
    x, y, z, _ = point
    inverse = pow(z, _FIELD - 2, _FIELD)
    x, y = x * inverse % _FIELD, y * inverse % _FIELD
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _string(value: object, description: str) -> str:
    if not isinstance(value, str):
        message = f"{description} must be a string, got {type(value).__name__}"
        raise TypeError(message)
    return value
