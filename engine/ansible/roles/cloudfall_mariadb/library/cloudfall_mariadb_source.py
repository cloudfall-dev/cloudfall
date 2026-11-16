"""Read a MariaDB server Cloudfall does not manage, with no client installed.

A replica of an outside source (S-019) checks its source before anything is
installed on its own server, so no ``mariadb`` client is there yet. This
module speaks the client protocol itself, with the Python every managed
server runs Ansible with: the handshake, TLS verified against the source's
CA when one is given, the ``mysql_native_password`` login, and text queries.

It only reads: a query that does not start with ``SELECT`` or ``SHOW`` is
refused before it is sent, so nothing on the source is changed. The
password is a ``no_log`` parameter and is in no result.
"""

from __future__ import annotations

import hashlib
import re
import socket
import ssl
import struct

from ansible.module_utils.basic import AnsibleModule

DOCUMENTATION = r"""
module: cloudfall_mariadb_source
short_description: Run read-only queries on a MariaDB server Cloudfall does not manage
description:
  - Logs in over the MariaDB client protocol with C(mysql_native_password),
    over TLS verified against C(ca) when given, and returns the rows of each
    query. Only SELECT and SHOW statements are sent.
options:
  host:
    description: The source's host name or address.
    type: str
    required: true
  port:
    description: The source's port.
    type: int
    required: true
  user:
    description: The account the team made on the source.
    type: str
    required: true
  password:
    description: Its password.
    type: str
    required: true
    no_log: true
  ca:
    description: >-
      PEM of the CA that signed the source's certificate; without it the
      connection is not encrypted.
    type: str
    default: ""
  queries:
    description: Each query by name; a SELECT or SHOW statement.
    type: dict
    required: true
  timeout:
    description: Seconds to wait for the source to connect and answer.
    type: int
    default: 10
author: Cloudfall
"""

RETURN = r"""
rows:
  description: >-
    The rows of each query by name, each row a list of its columns, NULL as
    None.
  type: dict
  returned: success
"""

_CLIENT_LONG_PASSWORD = 0x1
_CLIENT_PROTOCOL_41 = 0x200
_CLIENT_SSL = 0x800
_CLIENT_TRANSACTIONS = 0x2000
_CLIENT_SECURE_CONNECTION = 0x8000
_CLIENT_PLUGIN_AUTH = 0x80000
_MAX_PACKET = 16 * 1024 * 1024
_UTF8MB4 = 45
_NATIVE = "mysql_native_password"
_READ_ONLY = re.compile(r"\s*(SELECT|SHOW)\s", re.IGNORECASE)
_HEADER = 4
_OK = 0x00
_EOF = 0xFE
_ERR = 0xFF
_NULL = 0xFB
_EOF_LENGTH = 9


class SourceError(Exception):
    """A failure to reach, log in to or read the source; never holds the password."""


class _Connection:
    """One client session over the MariaDB protocol, packets in sequence."""

    def __init__(self, stream: socket.socket) -> None:
        self.stream: socket.socket | ssl.SSLSocket = stream
        self.sequence = 0

    def _exactly(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.stream.recv(size - len(data))
            if not chunk:
                message = "the source closed the connection"
                raise SourceError(message)
            data += chunk
        return data

    def read(self) -> bytes:
        header = self._exactly(_HEADER)
        length = header[0] | header[1] << 8 | header[2] << 16
        self.sequence = (header[3] + 1) % 256
        payload = self._exactly(length)
        if payload and payload[0] == _ERR:
            raise SourceError(_error_text(payload))
        return payload

    def write(self, payload: bytes) -> None:
        header = struct.pack("<I", len(payload))[:3] + bytes([self.sequence])
        self.stream.sendall(header + payload)
        self.sequence = (self.sequence + 1) % 256


def _error_text(payload: bytes) -> str:
    code = struct.unpack("<H", payload[1:3])[0]
    message = payload[3:]
    if message.startswith(b"#"):
        message = message[6:]
    return f"error {code}: {message.decode('utf-8', 'replace')}"


def _native_scramble(password: str, nonce: bytes) -> bytes:
    """Return the ``mysql_native_password`` answer to ``nonce``."""
    first = hashlib.sha1(password.encode("utf-8")).digest()  # noqa: S324 - the protocol's
    second = hashlib.sha1(first).digest()  # noqa: S324 - the protocol's
    mixed = hashlib.sha1(nonce + second).digest()  # noqa: S324 - the protocol's
    return bytes(a ^ b for a, b in zip(first, mixed, strict=True))


def _null_terminated(data: bytes, start: int) -> tuple[bytes, int]:
    end = data.index(b"\x00", start)
    return data[start:end], end + 1


def _handshake(payload: bytes) -> tuple[int, bytes, str]:
    """Return the server's capabilities, its nonce and its auth plugin."""
    if payload[0] != 10:  # noqa: PLR2004 - protocol version 10
        message = f"the source speaks protocol {payload[0]}, not 10"
        raise SourceError(message)
    _, position = _null_terminated(payload, 1)
    position += 4  # connection id
    nonce = payload[position : position + 8]
    position += 9  # and a filler
    capabilities = struct.unpack("<H", payload[position : position + 2])[0]
    position += 2
    plugin = _NATIVE
    if len(payload) > position:
        position += 3  # character set, status
        capabilities |= struct.unpack("<H", payload[position : position + 2])[0] << 16
        position += 2
        data_length = payload[position]
        position += 11  # the length and ten reserved bytes
        rest = max(13, data_length - 8)
        nonce += payload[position : position + rest].rstrip(b"\x00")
        position += rest
        if capabilities & _CLIENT_PLUGIN_AUTH and position < len(payload):
            name, _ = _null_terminated(payload + b"\x00", position)
            plugin = name.decode("ascii")
    return capabilities, nonce[:20], plugin


def _length_encoded(data: bytes, position: int) -> tuple[int | None, int]:
    first = data[position]
    if first < _NULL:
        return first, position + 1
    if first == _NULL:
        return None, position + 1
    size = {0xFC: 2, 0xFD: 3, 0xFE: 8}[first]
    value = int.from_bytes(data[position + 1 : position + 1 + size], "little")
    return value, position + 1 + size


def _is_eof(payload: bytes) -> bool:
    return payload[0] == _EOF and len(payload) < _EOF_LENGTH


def connect(  # noqa: PLR0913 - the login's parts, named.
    host: str, port: int, *, user: str, password: str, ca: str, timeout: int
) -> _Connection:
    """Open a logged-in session to the source."""
    try:
        stream = socket.create_connection((host, port), timeout=timeout)
    except OSError as error:
        message = f"can not connect to {host}:{port}: {error.strerror or error}"
        raise SourceError(message) from error
    connection = _Connection(stream)
    capabilities, nonce, plugin = _handshake(connection.read())
    wanted = (
        _CLIENT_LONG_PASSWORD
        | _CLIENT_PROTOCOL_41
        | _CLIENT_TRANSACTIONS
        | _CLIENT_SECURE_CONNECTION
        | _CLIENT_PLUGIN_AUTH
    )
    if ca:
        if not capabilities & _CLIENT_SSL:
            message = f"{host}:{port} offers no TLS, and caCertificateFile asks for it"
            raise SourceError(message)
        wanted |= _CLIENT_SSL
        connection.write(
            struct.pack("<IIB", wanted, _MAX_PACKET, _UTF8MB4) + b"\x00" * 23
        )
        context = ssl.create_default_context(cadata=ca)
        # The chain and the host name are verified as the server's own link
        # verifies them; Python's stricter RFC 5280 profile (3.13+) is not
        # MariaDB's, and would refuse a CA the link accepts.
        context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        try:
            connection.stream = context.wrap_socket(stream, server_hostname=host)
        except (ssl.SSLError, ssl.CertificateError) as error:
            message = f"the TLS handshake with {host}:{port} failed: {error}"
            raise SourceError(message) from error
    answer = _native_scramble(password, nonce) if plugin == _NATIVE else b""
    connection.write(
        struct.pack("<IIB", wanted, _MAX_PACKET, _UTF8MB4)
        + b"\x00" * 23
        + user.encode("utf-8")
        + b"\x00"
        + bytes([len(answer)])
        + answer
        + _NATIVE.encode("ascii")
        + b"\x00"
    )
    reply = connection.read()
    if reply[0] == _EOF:
        name, position = _null_terminated(reply, 1)
        if name.decode("ascii") != _NATIVE:
            message = (
                f"the source asks for {name.decode('ascii')}; Cloudfall logs in "
                f"with {_NATIVE}, so give the account that plugin"
            )
            raise SourceError(message)
        connection.write(_native_scramble(password, reply[position:].rstrip(b"\x00")))
        reply = connection.read()
    if reply[0] != _OK:
        message = "the source answered the login with neither OK nor an error"
        raise SourceError(message)
    return connection


def query(connection: _Connection, statement: str) -> list[list[str | None]]:
    """Run one read-only statement; return its rows."""
    if not _READ_ONLY.match(statement + " "):
        message = f"only SELECT and SHOW are sent to the source, not: {statement}"
        raise SourceError(message)
    connection.sequence = 0
    connection.write(b"\x03" + statement.encode("utf-8"))
    first = connection.read()
    if first[0] == _OK:
        return []
    columns, _ = _length_encoded(first, 0)
    for _ in range(columns or 0):
        connection.read()
    if not _is_eof(connection.read()):
        message = "the source sent more column definitions than it named"
        raise SourceError(message)
    rows: list[list[str | None]] = []
    while True:
        payload = connection.read()
        if _is_eof(payload):
            return rows
        row: list[str | None] = []
        position = 0
        for _ in range(columns or 0):
            size, position = _length_encoded(payload, position)
            if size is None:
                row.append(None)
                continue
            row.append(payload[position : position + size].decode("utf-8"))
            position += size
        rows.append(row)


def main() -> None:
    """Run the module."""
    module = AnsibleModule(
        argument_spec={
            "host": {"type": "str", "required": True},
            "port": {"type": "int", "required": True},
            "user": {"type": "str", "required": True},
            "password": {"type": "str", "required": True, "no_log": True},
            "ca": {"type": "str", "default": ""},
            "queries": {"type": "dict", "required": True},
            "timeout": {"type": "int", "default": 10},
        },
        supports_check_mode=True,
    )
    params = module.params
    try:
        connection = connect(
            params["host"],
            params["port"],
            user=params["user"],
            password=params["password"],
            ca=params["ca"],
            timeout=params["timeout"],
        )
        try:
            rows = {
                name: query(connection, str(statement))
                for name, statement in params["queries"].items()
            }
        finally:
            connection.stream.close()
    except (SourceError, OSError) as error:
        module.fail_json(msg=f"source {params['host']}:{params['port']}: {error}")
    module.exit_json(changed=False, rows=rows)


if __name__ == "__main__":
    main()
