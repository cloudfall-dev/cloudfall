"""RabbitMQ service values: versions, the supported series, names (S-023).

The supported series live in one table, each with the end of community
support RabbitMQ publishes for its open-source edition and the Erlang
releases it runs on, checked against RabbitMQ's release information page when
a Cloudfall release is cut. A version outside the table fails validation; a
version of a series within ``VERSION_ENDING_WINDOW`` of its date, or past it,
validates with a warning. A series leaves the table in the next minor release
of Cloudfall.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta

_VERSION_PART = r"(0|[1-9][0-9]{0,3})"
_VERSION_PATTERN = re.compile(rf"{_VERSION_PART}\.{_VERSION_PART}\.{_VERSION_PART}")
_ERLANG_PATTERN = re.compile(r"(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})")
_VHOST_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,62}")
_USER_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,62}")

VERSION_ENDING_WINDOW = timedelta(days=90)
"""How long before a series' end of community support ``config validate`` warns."""
WARNING_VERSION_ENDING = "RABBITMQ_VERSION_ENDING"
"""Warning code of a service whose series ends within the window, or has ended."""
WARNING_SINGLE_NODE = "RABBITMQ_SINGLE_NODE"
"""Warning code of a production service of one node: one copy of each message."""

NODE_COUNTS = frozenset({1, 3})
"""The sizes a cluster may have: two nodes stop on the loss of either."""
PRODUCTION_ENVIRONMENT = "production"
"""The environment whose single-node services ``config validate`` warns about."""
GUEST_USER = "guest"
"""The account RabbitMQ ships with a known password; Cloudfall removes it."""
DEFAULT_VHOST = "/"
"""The virtual host RabbitMQ ships with; Cloudfall removes it with ``guest``."""
MANAGED_TAG = "cloudfall-managed"
"""The tag of every virtual host Cloudfall created; a converge and audit read it."""
OWNER_TAG_PREFIX = "cloudfall-owner:"
"""The prefix of the tag that names the owner of a virtual host Cloudfall created."""
SCRATCH_PREFIX = "cloudfall_"
"""The prefix of the names the restore drill's scratch node uses."""


@dataclass(frozen=True, slots=True)
class RabbitmqVersion:
    """Exact ``X.Y.Z`` RabbitMQ release, installed as ``rabbitmq-server=X.Y.Z-1``."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the exact three-part release syntax."""
        if not _VERSION_PATTERN.fullmatch(self.value):
            message = f"RabbitMQ version is not X.Y.Z: {self.value!r}"
            raise ValueError(message)

    @property
    def parts(self) -> tuple[int, int, int]:
        """Return the release as numbers, for ordering."""
        major, minor, patch = (int(part) for part in self.value.split("."))
        return major, minor, patch

    @property
    def series(self) -> str:
        """Return the series the release belongs to: ``4.4``."""
        major, minor, _ = self.parts
        return f"{major}.{minor}"

    @classmethod
    def from_boundary(cls, value: object) -> RabbitmqVersion:
        """Coerce a boundary value into a RabbitMQ version."""
        return cls(_string(value, "RabbitMQ version"))


@dataclass(frozen=True, slots=True, order=True)
class ErlangRelease:
    """An Erlang/OTP release as ``major.minor``: ``27.0``."""

    major: int
    minor: int

    @property
    def label(self) -> str:
        """Return the release as written in messages."""
        return f"{self.major}.{self.minor}"

    @classmethod
    def parse(cls, value: str) -> ErlangRelease:
        """Read ``major.minor``."""
        match = _ERLANG_PATTERN.fullmatch(value)
        if match is None:
            message = f"Erlang release is not major.minor: {value!r}"
            raise ValueError(message)
        return cls(int(match.group(1)), int(match.group(2)))


@dataclass(frozen=True, slots=True)
class ErlangRange:
    """The Erlang releases a RabbitMQ series runs on.

    ``maximum`` is ``None`` while no newest supported Erlang is recorded for
    the series: every release from ``minimum`` on is accepted.
    """

    minimum: ErlangRelease
    maximum: ErlangRelease | None = None

    def contains(self, release: ErlangRelease) -> bool:
        """Return whether ``release`` is inside the range."""
        return release >= self.minimum and (
            self.maximum is None or release <= self.maximum
        )

    @property
    def label(self) -> str:
        """Return the range as written in messages: ``27.0 or later``."""
        if self.maximum is None:
            return f"{self.minimum.label} or later"
        return f"{self.minimum.label} to {self.maximum.label}"


@dataclass(frozen=True, slots=True)
class RabbitmqSeries:
    """One series Cloudfall installs, with its end of community support."""

    major: int
    minor: int
    end_of_support: date
    erlang: ErlangRange

    @property
    def label(self) -> str:
        """Return the series as written in messages: ``4.4``."""
        return f"{self.major}.{self.minor}"

    def covers(self, version: RabbitmqVersion) -> bool:
        """Return whether ``version`` belongs to this series."""
        major, minor, _ = version.parts
        return (major, minor) == (self.major, self.minor)

    def is_ending(self, today: date) -> bool:
        """Return whether ``today`` is within the window of, or past, the date."""
        return today >= self.end_of_support - VERSION_ENDING_WINDOW


# Debian 13 ships Erlang 27, and both series need 27.0 or later (S-023,
# Supported versions). Spec 023 gives the end of community support of each
# series from RabbitMQ's release information page
# (https://www.rabbitmq.com/release-information).
_ERLANG_27 = ErlangRange(minimum=ErlangRelease.parse("27.0"))
SUPPORTED_SERIES: tuple[RabbitmqSeries, ...] = (
    RabbitmqSeries(
        major=4, minor=3, end_of_support=date(2027, 1, 31), erlang=_ERLANG_27
    ),
    RabbitmqSeries(
        major=4, minor=4, end_of_support=date(2027, 7, 31), erlang=_ERLANG_27
    ),
)


def supported_series(
    version: RabbitmqVersion,
    table: tuple[RabbitmqSeries, ...] = SUPPORTED_SERIES,
) -> RabbitmqSeries | None:
    """Return the supported series ``version`` belongs to, if any."""
    return next((series for series in table if series.covers(version)), None)


def supported_series_text(table: tuple[RabbitmqSeries, ...] = SUPPORTED_SERIES) -> str:
    """Return the supported series as one human-readable list."""
    return ", ".join(series.label for series in table)


@dataclass(frozen=True, slots=True)
class RabbitmqVhostName:
    """Name of a declared virtual host.

    ``/`` is RabbitMQ's default virtual host and a declaration never names it;
    validation refuses it with its own code before this type reads it.
    """

    value: str

    def __post_init__(self) -> None:
        """Enforce the conservative naming the schema accepts."""
        if not _VHOST_NAME_PATTERN.fullmatch(self.value):
            message = f"invalid RabbitMQ virtual host name: {self.value!r}"
            raise ValueError(message)

    @property
    def is_reserved(self) -> bool:
        """Return whether the restore drill's scratch node holds the name."""
        return self.value.startswith(SCRATCH_PREFIX)

    @classmethod
    def from_boundary(cls, value: object) -> RabbitmqVhostName:
        """Coerce a boundary value into a virtual host name."""
        return cls(_string(value, "RabbitMQ virtual host name"))


@dataclass(frozen=True, slots=True)
class RabbitmqUserName:
    """Name of the user that owns one virtual host."""

    value: str

    def __post_init__(self) -> None:
        """Enforce the conservative naming the schema accepts."""
        if not _USER_NAME_PATTERN.fullmatch(self.value):
            message = f"invalid RabbitMQ user name: {self.value!r}"
            raise ValueError(message)

    @property
    def is_reserved(self) -> bool:
        """Return whether RabbitMQ or the restore drill holds the name."""
        return self.value == GUEST_USER or self.value.startswith(SCRATCH_PREFIX)

    @classmethod
    def from_boundary(cls, value: object) -> RabbitmqUserName:
        """Coerce a boundary value into a user name."""
        return cls(_string(value, "RabbitMQ user name"))


PASSWORD_SALT_BYTES = 4
"""The salt RabbitMQ's ``rabbit_password_hashing_sha256`` puts before the digest."""


def password_hash(password: str, salt: bytes) -> str:
    """Return RabbitMQ's stored SHA-256 form of ``password`` (D-9).

    Base64 of the four-byte salt followed by SHA-256 of the salt and the
    password's UTF-8 bytes, as ``rabbit_password_hashing_sha256`` stores it.
    The role computes the same on the controller, reusing the salt a user
    already has (D-25).
    """
    if len(salt) != PASSWORD_SALT_BYTES:
        message = f"a RabbitMQ salt is {PASSWORD_SALT_BYTES} bytes"
        raise ValueError(message)
    digest = hashlib.sha256(salt + password.encode("utf-8")).digest()
    return base64.b64encode(salt + digest).decode("ascii")


def _string(value: object, description: str) -> str:
    if not isinstance(value, str):
        message = f"{description} must be a string, got {type(value).__name__}"
        raise TypeError(message)
    return value
