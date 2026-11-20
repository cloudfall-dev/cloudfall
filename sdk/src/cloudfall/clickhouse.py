"""ClickHouse service values: versions, the supported series, names (S-022).

Cloudfall installs only the long-term support (LTS) series ClickHouse still
maintains. They live in one table, each with its end-of-support date: the
first release of the series plus one year, since ClickHouse publishes the rule
and no date, checked against the supported list in ClickHouse's
``SECURITY.md`` when a Cloudfall release is cut. A version outside the table
fails validation; a version of a series within ``VERSION_ENDING_WINDOW`` of
its date, or past it, validates with a warning. A series leaves the table in
the next minor release of Cloudfall.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

_VERSION_PART = r"(0|[1-9][0-9]{0,4})"
_VERSION_PATTERN = re.compile(
    rf"{_VERSION_PART}\.{_VERSION_PART}\.{_VERSION_PART}\.{_VERSION_PART}"
)
_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,62}")

VERSION_ENDING_WINDOW = timedelta(days=90)
"""How long before a series' end of support ``config validate`` warns."""
WARNING_VERSION_ENDING = "CLICKHOUSE_VERSION_ENDING"
"""Warning code of a service whose series ends within the window, or has ended."""
WARNING_SINGLE_REPLICA = "CLICKHOUSE_SINGLE_REPLICA"
"""Warning code of a production service of one replica: one copy of the data."""

MAX_REPLICAS = 2
"""A service holds its data on one server or two; more is out of scope."""
VOTERS_PER_REPLICAS: dict[int, int] = {1: 1, 2: 3}
"""The Keeper voters each number of replicas needs: two voters stop on either loss."""
PRODUCTION_ENVIRONMENT = "production"
"""The environment whose single-replica services ``config validate`` warns about."""
RESERVED_NAMES = frozenset(
    {"default", "cloudfall", "system", "information_schema", "INFORMATION_SCHEMA"}
)
"""Names ClickHouse or Cloudfall holds: an owner or database never takes one."""
SCRATCH_PREFIX = "cloudfall_"
"""The prefix of the scratch databases the restore drill creates."""


@dataclass(frozen=True, slots=True)
class ClickhouseVersion:
    """Exact ``X.Y.Z.N`` ClickHouse build, installed at that version and held."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the exact four-part build syntax."""
        if not _VERSION_PATTERN.fullmatch(self.value):
            message = f"ClickHouse version is not X.Y.Z.N: {self.value!r}"
            raise ValueError(message)

    @property
    def series(self) -> tuple[int, int]:
        """Return the series the build belongs to, as numbers: ``(26, 8)``."""
        year, release, _, _ = (int(part) for part in self.value.split("."))
        return year, release

    @classmethod
    def from_boundary(cls, value: object) -> ClickhouseVersion:
        """Coerce a boundary value into a ClickHouse version."""
        return cls(_string(value, "ClickHouse version"))


@dataclass(frozen=True, slots=True)
class ClickhouseSeries:
    """One LTS series Cloudfall installs, with its end-of-support date."""

    year: int
    release: int
    end_of_support: date

    @property
    def label(self) -> str:
        """Return the series as written in messages: ``26.8``."""
        return f"{self.year}.{self.release}"

    def covers(self, version: ClickhouseVersion) -> bool:
        """Return whether ``version`` belongs to this series."""
        return version.series == (self.year, self.release)

    def is_ending(self, today: date) -> bool:
        """Return whether ``today`` is within the window of, or past, the date."""
        return today >= self.end_of_support - VERSION_ENDING_WINDOW


# Spec 022, Supported versions: the first release of each LTS series plus one
# year, checked against ClickHouse's SECURITY.md when a release is cut.
SUPPORTED_SERIES: tuple[ClickhouseSeries, ...] = (
    ClickhouseSeries(year=26, release=3, end_of_support=date(2027, 3, 27)),
    ClickhouseSeries(year=26, release=8, end_of_support=date(2027, 8, 30)),
)


def supported_series(
    version: ClickhouseVersion,
    table: tuple[ClickhouseSeries, ...] = SUPPORTED_SERIES,
) -> ClickhouseSeries | None:
    """Return the supported series ``version`` belongs to, if any."""
    return next((series for series in table if series.covers(version)), None)


def supported_series_text(
    table: tuple[ClickhouseSeries, ...] = SUPPORTED_SERIES,
) -> str:
    """Return the supported series as one human-readable list."""
    return ", ".join(series.label for series in table)


@dataclass(frozen=True, slots=True)
class ClickhouseDatabaseName:
    """Name of a declared ClickHouse database."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the conservative naming the schema accepts."""
        if not _NAME_PATTERN.fullmatch(self.value):
            message = f"invalid ClickHouse database name: {self.value!r}"
            raise ValueError(message)

    @property
    def is_reserved(self) -> bool:
        """Return whether ClickHouse, Cloudfall or the restore drill holds it."""
        return _is_reserved(self.value)

    @classmethod
    def from_boundary(cls, value: object) -> ClickhouseDatabaseName:
        """Coerce a boundary value into a database name."""
        return cls(_string(value, "ClickHouse database name"))


@dataclass(frozen=True, slots=True)
class ClickhouseUserName:
    """Name of the user that owns one declared database."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the conservative naming the schema accepts."""
        if not _NAME_PATTERN.fullmatch(self.value):
            message = f"invalid ClickHouse user name: {self.value!r}"
            raise ValueError(message)

    @property
    def is_reserved(self) -> bool:
        """Return whether ClickHouse, Cloudfall or the restore drill holds it."""
        return _is_reserved(self.value)

    @classmethod
    def from_boundary(cls, value: object) -> ClickhouseUserName:
        """Coerce a boundary value into a user name."""
        return cls(_string(value, "ClickHouse user name"))


def _is_reserved(value: str) -> bool:
    return value in RESERVED_NAMES or value.startswith(SCRATCH_PREFIX)


def _string(value: object, description: str) -> str:
    if not isinstance(value, str):
        message = f"{description} must be a string, got {type(value).__name__}"
        raise TypeError(message)
    return value
