"""A source server Cloudfall does not manage, on the MariaDB wire protocol.

The role's preflight logs in to a replica's outside source with its own
client module, ``cloudfall_mariadb_source``, before anything is installed.
This server speaks the protocol's part that module uses: the version 10
handshake, TLS on request with a certificate from a test CA, the
``mysql_native_password`` login checked against the account's stored hash,
and text queries. It answers each query by running it through the source's
``mariadb_pair`` stand-in client, so the preflight, the dump and the link
read one model of the source, and every statement it was sent is in that
stand-in's log.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import os
import socket
import ssl
import struct
import subprocess
import threading
from typing import TYPE_CHECKING, Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

if TYPE_CHECKING:
    from pathlib import Path

_PROTOCOL_41 = 0x200
_SSL = 0x800
_SECURE_CONNECTION = 0x8000
_PLUGIN_AUTH = 0x80000
_SSL_REQUEST_LENGTH = 32


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def write_tls(directory: Path, address: str) -> tuple[str, Path, Path]:
    """Write a CA and a certificate for ``address``; return the CA, cert and key."""
    now = dt.datetime.now(dt.UTC)
    ca_key, key = _key(), _key()
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name("old-db-ca"))
        .issuer_name(_name("old-db-ca"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(address))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(address))]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    certificate_path = directory / "server.crt"
    key_path = directory / "server.key"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    pem = ca.public_bytes(serialization.Encoding.PEM).decode("ascii")
    return pem, certificate_path, key_path


def _packet(sequence: int, payload: bytes) -> bytes:
    return struct.pack("<I", len(payload))[:3] + bytes([sequence % 256]) + payload


def _length_encoded(value: bytes) -> bytes:
    if len(value) < 0xFB:
        return bytes([len(value)]) + value
    return b"\xfc" + struct.pack("<H", len(value)) + value


def _error(sequence: int, code: int, message: str) -> bytes:
    return _packet(
        sequence, b"\xff" + struct.pack("<H", code) + b"#HY000" + message.encode()
    )


class SourceServer:
    """The protocol front of one ``mariadb_pair`` stand-in server."""

    def __init__(
        self,
        state: Path,
        client: Path,
        tls: tuple[Path, Path] | None = None,
    ) -> None:
        """Listen on a free port of 127.0.0.1; ``tls`` is a certificate and its key."""
        self.state = state
        self.client = client
        self.context: ssl.SSLContext | None = None
        if tls is not None:
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.context.load_cert_chain(str(tls[0]), str(tls[1]))
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = int(self.listener.getsockname()[1])
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Close the port: the source is stopped, or out of reach."""
        self.listener.close()
        self.thread.join(timeout=10)

    def _serve(self) -> None:
        while True:
            try:
                stream, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(stream,), daemon=True).start()

    def _session(self, stream: socket.socket) -> None:
        with stream:
            try:
                self._converse(stream)
            except (OSError, ssl.SSLError, ValueError):
                return

    def _converse(self, raw: socket.socket) -> None:
        nonce = bytes(byte % 94 + 33 for byte in os.urandom(20))
        capabilities = _PROTOCOL_41 | _SECURE_CONNECTION | _PLUGIN_AUTH
        if self.context is not None:
            capabilities |= _SSL
        handshake = (
            b"\x0a"
             b"5.5.5-standin-MariaDB\x00"
            + struct.pack("<I", 7)
            + nonce[:8]
            + b"\x00"
            + struct.pack("<H", capabilities & 0xFFFF)
            + bytes([45])
            + struct.pack("<H", 2)
            + struct.pack("<H", capabilities >> 16)
            + bytes([21])
            + b"\x00" * 10
            + nonce[8:]
            + b"\x00"
            + b"mysql_native_password\x00"
        )
        stream: Any = raw
        stream.sendall(_packet(0, handshake))
        sequence, payload = self._read(stream)
        tls = False
        if len(payload) == _SSL_REQUEST_LENGTH and self.context is not None:
            stream = self.context.wrap_socket(raw, server_side=True)
            tls = True
            sequence, payload = self._read(stream)
        user, answer = self._login(payload)
        account = self._account(user)
        if account is None or not self._native_ok(account, answer, nonce):
            stream.sendall(_error(sequence + 1, 1045, f"Access denied for {user}"))
            return
        if account[1].get("ssl") and not tls:
            stream.sendall(_error(sequence + 1, 1045, "TLS required"))
            return
        stream.sendall(_packet(sequence + 1, b"\x00\x00\x00\x02\x00\x00\x00"))
        while True:
            _, command = self._read(stream)
            if not command or command[0] == 0x01:
                return
            sql = command[1:].decode("utf-8")
            stream.sendall(self._answer(sql, account[0]))

    @staticmethod
    def _read(stream: Any) -> tuple[int, bytes]:  # noqa: ANN401 - a socket, TLS or not
        header = b""
        while len(header) < 4:
            chunk = stream.recv(4 - len(header))
            if not chunk:
                message = "closed"
                raise ValueError(message)
            header += chunk
        length = header[0] | header[1] << 8 | header[2] << 16
        payload = b""
        while len(payload) < length:
            chunk = stream.recv(length - len(payload))
            if not chunk:
                message = "closed"
                raise ValueError(message)
            payload += chunk
        return header[3], payload

    @staticmethod
    def _login(payload: bytes) -> tuple[str, bytes]:
        position = 32
        end = payload.index(b"\x00", position)
        user = payload[position:end].decode("utf-8")
        length = payload[end + 1]
        return user, payload[end + 2 : end + 2 + length]

    def _account(self, user: str) -> tuple[str, dict[str, Any]] | None:
        state = json.loads(self.state.read_text(encoding="utf-8"))
        for account, found in state["users"].items():
            if account.split("@", 1)[0] == user and not found.get("locked"):
                return account, found
        return None

    @staticmethod
    def _native_ok(
        account: tuple[str, dict[str, Any]], answer: bytes, nonce: bytes
    ) -> bool:
        stored = str(account[1].get("hash", ""))
        if len(stored) != 41 or len(answer) != 20:
            return False
        second = bytes.fromhex(stored[1:])
        mixed = hashlib.sha1(nonce + second).digest()  # noqa: S324 - the protocol's
        first = bytes(a ^ b for a, b in zip(answer, mixed, strict=True))
        return hashlib.sha1(first).digest() == second  # noqa: S324 - the protocol's

    def _answer(self, sql: str, account: str) -> bytes:
        user, host = account.split("@", 1)
        grants = f"SHOW GRANTS FOR '{user}'@'{host}'"
        statement = grants if sql == "SHOW GRANTS" else sql
        done = subprocess.run(  # noqa: S603 - the stand-in client the test wrote.
            [str(self.client), "--batch", "--raw", "--skip-column-names",
             "--execute", statement],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if done.returncode != 0:
            return _error(1, 1064, done.stderr.strip() or "failed")
        rows = [line.split("\t") for line in done.stdout.splitlines()]
        width = len(rows[0]) if rows else 1
        sequence = 1
        out = _packet(sequence, bytes([width]))
        for _ in range(width):
            sequence += 1
            definition = (
                _length_encoded(b"def") + b"\x00" * 3 + _length_encoded(b"c")
                + b"\x00" + b"\x0c" + struct.pack("<HIBHB", 45, 1024, 0xFD, 0, 0)
                + b"\x00\x00"
            )
            out += _packet(sequence, definition)
        sequence += 1
        out += _packet(sequence, b"\xfe\x00\x00\x02\x00")
        for row in rows:
            sequence += 1
            out += _packet(
                sequence, b"".join(_length_encoded(value.encode()) for value in row)
            )
        sequence += 1
        return out + _packet(sequence, b"\xfe\x00\x00\x02\x00")
