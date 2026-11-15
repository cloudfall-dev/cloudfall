"""Turn the source database URL into a libpq service file (#355).

``psql`` and ``pg_dump`` read the connection from the 0600 file through
``PGSERVICEFILE`` and ``PGSERVICE``, so the password never lands on their
command line, where every local user reads it in ``/proc/<pid>/cmdline``.

The URL is parsed as libpq parses a connection URI: percent-encoded user,
password, host and database, a comma-separated host list, bracketed IPv6
hosts and query parameters such as ``sslmode``. No message quotes any part
of the URL, since it holds the password.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import unquote

if TYPE_CHECKING:
    from collections.abc import Callable

SCHEMES = ("postgresql://", "postgres://")
SERVICE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
KEYWORD = re.compile(r"^[a-z_]+$")
PORT = re.compile(r"^[0-9]*$")
BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
# A service file line is key=value up to the line's end, with trailing
# whitespace dropped; a nested service or service file is refused by libpq.
REFUSED_KEYWORDS = frozenset({"service", "servicefile"})


class SourceUrlError(ValueError):
    """The source URL is not a connection URI the service file can hold."""


def _decode(text: str, part: str) -> str:
    if BAD_PERCENT.search(text):
        message = f"the source URL's {part} has an invalid percent-encoding"
        raise SourceUrlError(message)
    try:
        value = unquote(text, errors="strict")
    except UnicodeDecodeError as error:
        message = f"the source URL's {part} is not UTF-8 once decoded"
        raise SourceUrlError(message) from error
    if any(character in value for character in "\r\n\0"):
        message = f"the source URL's {part} holds a line break or a NUL"
        raise SourceUrlError(message)
    if value != value.rstrip():
        message = (
            f"the source URL's {part} ends with whitespace, which a service "
            "file can't hold"
        )
        raise SourceUrlError(message)
    return value


def _hosts(host_list: str) -> tuple[list[str], list[str]]:
    hosts: list[str] = []
    ports: list[str] = []
    for entry in host_list.split(","):
        if entry.startswith("["):
            close = entry.find("]")
            if close < 0:
                message = "the source URL has an IPv6 host without its ]"
                raise SourceUrlError(message)
            host, after = entry[1:close], entry[close + 1 :]
            if after and not after.startswith(":"):
                message = "the source URL has text after an IPv6 host's ]"
                raise SourceUrlError(message)
            port = after[1:]
        else:
            host, _, port = entry.partition(":")
        host = _decode(host, "host")
        port = _decode(port, "port")
        if "," in host or any(character.isspace() for character in host):
            message = "the source URL has a host with a comma or whitespace"
            raise SourceUrlError(message)
        if not PORT.fullmatch(port):
            message = "the source URL has a port that is not a number"
            raise SourceUrlError(message)
        hosts.append(host)
        ports.append(port)
    return hosts, ports


def _set(parameters: dict[str, str], keyword: str, value: str) -> None:
    if not KEYWORD.fullmatch(keyword) or keyword in REFUSED_KEYWORDS:
        message = "the source URL names a connection parameter libpq doesn't take here"
        raise SourceUrlError(message)
    if keyword in parameters:
        message = f"the source URL sets {keyword} twice"
        raise SourceUrlError(message)
    parameters[keyword] = value


def _userinfo(parameters: dict[str, str], userinfo: str) -> None:
    user, colon, password = userinfo.partition(":")
    if user:
        _set(parameters, "user", _decode(user, "user"))
    if colon:
        _set(parameters, "password", _decode(password, "password"))


def _host_list(parameters: dict[str, str], host_list: str) -> None:
    if host_list:
        hosts, ports = _hosts(host_list)
        if any(hosts):
            _set(parameters, "host", ",".join(hosts))
        if any(ports):
            _set(parameters, "port", ",".join(ports))


def _query(parameters: dict[str, str], query: str) -> None:
    for pair in query.split("&"):
        keyword, equals, value = pair.partition("=")
        if not equals:
            message = "the source URL has a query parameter without a value"
            raise SourceUrlError(message)
        keyword = _decode(keyword, "query parameter name")
        value = _decode(value, "query parameter value")
        # libpq reads the JDBC spelling as sslmode=require
        if keyword == "ssl" and value == "true":
            keyword, value = "sslmode", "require"
        _set(parameters, keyword, value)


def connection_parameters(url: str) -> dict[str, str]:
    """Return the libpq connection parameters a ``postgresql://`` URL names."""
    text = url.strip()
    scheme = next((prefix for prefix in SCHEMES if text.startswith(prefix)), None)
    if scheme is None:
        message = "the source URL must start with postgresql:// or postgres://"
        raise SourceUrlError(message)
    rest = text[len(scheme) :]
    parameters: dict[str, str] = {}
    # As libpq does, the user info runs to the first @ before any /, so an
    # unencoded ? or # in the password stays part of it.
    end = next((i for i, c in enumerate(rest) if c in "@/"), len(rest))
    if rest[end:].startswith("@"):
        _userinfo(parameters, rest[:end])
        rest = rest[end + 1 :]
    if "#" in rest:
        message = "the source URL holds a #; percent-encode it as %23"
        raise SourceUrlError(message)
    location, _, query = rest.partition("?")
    host_list, _, path = location.partition("/")
    _host_list(parameters, host_list)
    if path:
        _set(parameters, "dbname", _decode(path, "database"))
    if query:
        _query(parameters, query)
    return parameters


def pg_service(url: str, name: str) -> str:
    """Return a libpq service file holding the URL's connection as ``name``."""
    if not SERVICE_NAME.fullmatch(name):
        message = "the service name must be letters, digits, _ or -"
        raise SourceUrlError(message)
    lines = [f"[{name}]"]
    lines.extend(
        f"{keyword}={value}"
        for keyword, value in connection_parameters(url).items()
    )
    return "\n".join(lines) + "\n"


def pg_service_problem(url: str) -> str:
    """Return why the URL can't become a service file, or an empty string."""
    try:
        connection_parameters(url)
    except SourceUrlError as error:
        return str(error)
    return ""


class FilterModule:
    """The role's filters."""

    def filters(self) -> dict[str, Callable[..., str]]:
        """Name the filters for Ansible."""
        return {
            "cloudfall_pg_service": pg_service,
            "cloudfall_pg_service_problem": pg_service_problem,
        }
