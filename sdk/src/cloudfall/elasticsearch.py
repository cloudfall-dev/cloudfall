"""Elasticsearch service values: versions, the supported lines, names (S-012).

The supported lines live in one table, each with the end-of-life date taken
from Elastic's end-of-life page when a Cloudfall release is cut. A version
outside the table fails validation; a version of a line within
``VERSION_ENDING_WINDOW`` of its date, or past it, validates with a warning.
A line leaves the table in the next minor release of Cloudfall.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

_VERSION_PART = r"(0|[1-9][0-9]{0,3})"
_VERSION_PATTERN = re.compile(rf"{_VERSION_PART}\.{_VERSION_PART}\.{_VERSION_PART}")
_MAX_TCP_PORT = 65535
_USER_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,62}")
_INDEX_PATTERN_PATTERN = re.compile(r"[a-z0-9._*+-]{1,255}")

VERSION_ENDING_WINDOW = timedelta(days=90)
"""How long before a line's end-of-life date ``config validate`` warns."""
WARNING_VERSION_ENDING = "ELASTICSEARCH_VERSION_ENDING"
"""Warning code of a service whose line ends within the window, or has ended."""


@dataclass(frozen=True, slots=True)
class ElasticsearchVersion:
    """Exact ``X.Y.Z`` Elasticsearch release, installed as ``elasticsearch=X.Y.Z``."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the exact three-part release syntax."""
        if not _VERSION_PATTERN.fullmatch(self.value):
            message = f"Elasticsearch version is not X.Y.Z: {self.value!r}"
            raise ValueError(message)

    @property
    def major(self) -> int:
        """Return the major release number."""
        return int(self.value.split(".")[0])

    @property
    def minor(self) -> int:
        """Return the minor release number."""
        return int(self.value.split(".")[1])

    @classmethod
    def from_boundary(cls, value: object) -> ElasticsearchVersion:
        """Coerce a boundary value into an Elasticsearch version."""
        if not isinstance(value, str):
            message = (
                f"Elasticsearch version must be a string, got {type(value).__name__}"
            )
            raise TypeError(message)
        return cls(value)


@dataclass(frozen=True, slots=True)
class ElasticsearchLine:
    """One release line Cloudfall installs, with Elastic's end-of-life date.

    ``minor`` is ``None`` when Elastic maintains the whole major as one line.
    """

    major: int
    minor: int | None
    end_of_life: date

    @property
    def label(self) -> str:
        """Return the line as written in messages: ``8.19`` or ``9.x``."""
        return f"{self.major}.{'x' if self.minor is None else self.minor}"

    def covers(self, version: ElasticsearchVersion) -> bool:
        """Return whether ``version`` belongs to this line."""
        return version.major == self.major and (
            self.minor is None or version.minor == self.minor
        )

    def is_ending(self, today: date) -> bool:
        """Return whether ``today`` is within the window of, or past, the date."""
        return today >= self.end_of_life - VERSION_ENDING_WINDOW


# Elastic's end-of-life page (https://www.elastic.co/support/eol), read
# 2026-11-14: the End of Maintenance Term of the 8.x row, whose final minor
# 8.19 is the only 8.x line still maintained, and of the 9.x row, which
# Elastic maintains as one major.
SUPPORTED_LINES: tuple[ElasticsearchLine, ...] = (
    ElasticsearchLine(major=8, minor=19, end_of_life=date(2027, 1, 15)),
    ElasticsearchLine(major=9, minor=None, end_of_life=date(2027, 10, 15)),
)


def supported_line(
    version: ElasticsearchVersion,
    lines: tuple[ElasticsearchLine, ...] = SUPPORTED_LINES,
) -> ElasticsearchLine | None:
    """Return the supported line ``version`` belongs to, if any."""
    return next((line for line in lines if line.covers(version)), None)


def supported_lines_text(lines: tuple[ElasticsearchLine, ...] = SUPPORTED_LINES) -> str:
    """Return the supported lines as one human-readable list."""
    return ", ".join(line.label for line in lines)


class ElasticsearchAccess(StrEnum):
    """What a declared user may do on its index patterns."""

    READ = "read"
    WRITE = "write"

    @classmethod
    def from_boundary(cls, value: object) -> ElasticsearchAccess:
        """Coerce a boundary value into an access level."""
        if not isinstance(value, str):
            message = f"access must be a string, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value)


@dataclass(frozen=True, slots=True)
class ElasticsearchUserName:
    """Name of a declared user, also the name of its role."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the conservative naming the schema accepts."""
        if not _USER_NAME_PATTERN.fullmatch(self.value):
            message = f"invalid Elasticsearch user name: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ElasticsearchUserName:
        """Coerce a boundary value into a user name."""
        if not isinstance(value, str):
            message = f"user name must be a string, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value)


@dataclass(frozen=True, slots=True)
class ElasticsearchIndexPattern:
    """Index name or wildcard pattern a user's role is limited to."""

    value: str

    def __post_init__(self) -> None:
        """Enforce index-name characters; never every index, never a hidden one."""
        if not _INDEX_PATTERN_PATTERN.fullmatch(self.value):
            message = f"invalid Elasticsearch index pattern: {self.value!r}"
            raise ValueError(message)
        if not self.value.strip("*"):
            message = f"index pattern matches every index: {self.value!r}"
            raise ValueError(message)
        if self.value.startswith("."):
            message = (
                f"index pattern names hidden or system indices: {self.value!r}"
            )
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ElasticsearchIndexPattern:
        """Coerce a boundary value into an index pattern."""
        if not isinstance(value, str):
            message = f"index pattern must be a string, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value)


@dataclass(frozen=True, slots=True)
class ElasticsearchClusterName:
    """``cluster.name`` of the cluster the nodes form or join."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the cluster naming the schema accepts."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", self.value):
            message = f"invalid Elasticsearch cluster name: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ElasticsearchClusterName:
        """Coerce a boundary value into a cluster name."""
        if not isinstance(value, str):
            message = f"cluster name must be a string, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value)


@dataclass(frozen=True, slots=True)
class ElasticsearchSeedHost:
    """``host:port`` of a transport address of an existing cluster's node."""

    value: str

    def __post_init__(self) -> None:
        """Enforce a host and a TCP port, an IPv6 host in brackets."""
        match = re.fullmatch(
            r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)"
            r":([0-9]{1,5})",
            self.value,
        )
        if match is None or not 1 <= int(match.group(2)) <= _MAX_TCP_PORT:
            message = f"seed host is not host:port: {self.value!r}"
            raise ValueError(message)

    @property
    def host(self) -> str:
        """Return the host part, without IPv6 brackets."""
        host = self.value.rsplit(":", 1)[0]
        return host.removeprefix("[").removesuffix("]")

    @classmethod
    def from_boundary(cls, value: object) -> ElasticsearchSeedHost:
        """Coerce a boundary value into a seed host."""
        if not isinstance(value, str):
            message = f"seed host must be a string, got {type(value).__name__}"
            raise TypeError(message)
        return cls(value)


def _string(value: object, description: str) -> str:
    if not isinstance(value, str):
        message = f"{description} must be a string, got {type(value).__name__}"
        raise TypeError(message)
    return value


@dataclass(frozen=True, slots=True)
class ProjectFilePath:
    """Path of a file inside the project, relative to its root."""

    value: str

    def __post_init__(self) -> None:
        """Reject absolute paths, ``..`` segments, and control characters."""
        if (
            not self.value
            or self.value.startswith("/")
            or ".." in self.value.split("/")
            or any(not character.isprintable() for character in self.value)
        ):
            message = f"not a path inside the project: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> ProjectFilePath:
        """Coerce a boundary value into a project file path."""
        return cls(_string(value, "project file path"))


@dataclass(frozen=True, slots=True)
class S3Bucket:
    """Name of an S3-compatible bucket."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the S3 bucket naming the schema accepts."""
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.value):
            message = f"invalid S3 bucket name: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> S3Bucket:
        """Coerce a boundary value into a bucket name."""
        return cls(_string(value, "S3 bucket"))


@dataclass(frozen=True, slots=True)
class S3Endpoint:
    """Host, and optionally port, of an S3-compatible endpoint."""

    value: str

    def __post_init__(self) -> None:
        """Enforce a host name or address with an optional port."""
        if not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?",
            self.value,
        ):
            message = f"invalid S3 endpoint: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> S3Endpoint:
        """Coerce a boundary value into an endpoint."""
        return cls(_string(value, "S3 endpoint"))


@dataclass(frozen=True, slots=True)
class S3BasePath:
    """Key prefix of the snapshots inside the bucket."""

    value: str

    def __post_init__(self) -> None:
        """Enforce slash-separated segments of safe characters."""
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,255}(?:/[A-Za-z0-9._-]+)*", self.value):
            message = f"invalid S3 base path: {self.value!r}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> S3BasePath:
        """Coerce a boundary value into a base path."""
        return cls(_string(value, "S3 base path"))


# Users and roles Elasticsearch ships with; a declared user is also a role of
# the same name, so it may be named after neither.
RESERVED_USER_NAMES = frozenset(
    {
        "cloudfall",
        "elastic",
        "kibana",
        "kibana_system",
        "logstash_system",
        "beats_system",
        "apm_system",
        "remote_monitoring_user",
        "superuser",
        "kibana_admin",
        "kibana_user",
        "logstash_admin",
        "beats_admin",
        "apm_user",
        "remote_monitoring_agent",
        "remote_monitoring_collector",
        "monitoring_user",
        "reporting_user",
        "ingest_admin",
        "machine_learning_admin",
        "machine_learning_user",
        "transform_admin",
        "transform_user",
        "data_frame_transforms_admin",
        "data_frame_transforms_user",
        "watcher_admin",
        "watcher_user",
        "viewer",
        "editor",
        "enrich_user",
        "snapshot_user",
        "transport_client",
        "rollup_admin",
        "rollup_user",
        "inference_admin",
        "inference_user",
    }
)
