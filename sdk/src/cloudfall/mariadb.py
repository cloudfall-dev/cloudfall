"""MariaDB service values: versions, the supported series, names (S-019).

The supported series live in one table, each with the date MariaDB's
maintenance policy gives for the end of its community binaries, taken when a
Cloudfall release is cut. A version outside the table fails validation; a
version of a series within ``VERSION_ENDING_WINDOW`` of its date, or past
it, validates with a warning. A series leaves the table in the next minor
release of Cloudfall.
"""

from __future__ import annotations

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


def _string(value: object, description: str) -> str:
    if not isinstance(value, str):
        message = f"{description} must be a string, got {type(value).__name__}"
        raise TypeError(message)
    return value
