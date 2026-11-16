"""Compute an owner role's SCRAM-SHA-256 verifier on the controller (D-9, D-25).

The controller keeps the owner's password in a 0600 file of its own, and the
play reads the role's stored password from ``pg_authid`` as psql prints it,
``<rolcanlogin>|<rolpassword>``. The verifier is computed with the stored
verifier's salt and iteration count, so the same password gives the same
verifier and a converge that changes nothing sends nothing; a role with no
SCRAM-SHA-256 verifier (none, or md5) gets a fresh salt. A stored password of
any other shape is refused, never silently replaced. No message quotes the
password or either verifier.

Ansible loads this file itself, into ``ansible-playbook``; it imports nothing
from the SDK, so it works whichever interpreter runs it. Its verifier is the
one ``cloudfall.secrets.scram_sha256_verifier`` computes, as a test checks.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import stringprep
import unicodedata
from pathlib import Path
from secrets import token_bytes
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

ITERATIONS = 4096
SALT_BYTES = 16
KEY_BYTES = 32
_BASE64 = r"[A-Za-z0-9+/]+={0,2}"
SCRAM = re.compile(
    rf"SCRAM-SHA-256\$([1-9][0-9]{{0,9}}):({_BASE64})\$({_BASE64}):({_BASE64})"
)
MD5 = re.compile(r"md5[0-9a-f]{32}")
LOGIN = {"t": True, "f": False}


class StoredPasswordError(ValueError):
    """The role's stored password is not one Cloudfall can compare."""


def _decode(text: str, part: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except binascii.Error as error:
        message = f"the role's stored SCRAM-SHA-256 {part} is not base64"
        raise StoredPasswordError(message) from error


def stored_salt(stored: str) -> tuple[int, bytes] | None:
    """Return the iterations and salt of a stored line, or None for a fresh salt.

    ``stored`` is psql's ``<rolcanlogin>|<rolpassword>`` line for the role.
    """
    login, separator, password = stored.partition("|")
    if not separator or login not in LOGIN:
        message = (
            "the role's stored password was not read as "
            "<rolcanlogin>|<rolpassword>"
        )
        raise StoredPasswordError(message)
    if not password or MD5.fullmatch(password):
        return None
    match = SCRAM.fullmatch(password)
    if match is None:
        message = (
            "the role's stored password is neither a SCRAM-SHA-256 nor an md5 "
            "verifier; set it once with ALTER ROLE ... PASSWORD NULL and converge"
        )
        raise StoredPasswordError(message)
    salt = _decode(match[2], "salt")
    stored_key = _decode(match[3], "StoredKey")
    server_key = _decode(match[4], "ServerKey")
    if not salt or len(stored_key) != KEY_BYTES or len(server_key) != KEY_BYTES:
        message = "the role's stored SCRAM-SHA-256 verifier has a key of a wrong size"
        raise StoredPasswordError(message)
    return int(match[1]), salt


def stored_password_problem(stored: str) -> str:
    """Return why the stored line can't be compared, or an empty string."""
    try:
        stored_salt(stored)
    except StoredPasswordError as error:
        return str(error)
    return ""


def scram_verifier(password_file: str, stored: str) -> str:
    """Return the verifier of the password in ``password_file``.

    It reuses the salt and iteration count of the role's stored SCRAM-SHA-256
    verifier in ``stored`` (psql's ``<rolcanlogin>|<rolpassword>`` line), or
    takes a fresh 16-byte salt and 4096 iterations, PostgreSQL's defaults.
    """
    password = Path(password_file).read_bytes().decode("utf-8")
    if not password:
        message = "the staged owner role password is empty"
        raise ValueError(message)
    found = stored_salt(stored)
    iterations, salt = (
        (ITERATIONS, token_bytes(SALT_BYTES)) if found is None else found
    )
    return verifier(password, salt, iterations)


def verifier(password: str, salt: bytes, iterations: int) -> str:
    """Return PostgreSQL's stored SCRAM-SHA-256 form of ``password``.

    ``SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>`` in base64,
    the password prepared with SASLprep as PostgreSQL prepares it.
    """
    if not salt:
        message = "a SCRAM-SHA-256 salt must not be empty"
        raise ValueError(message)
    salted = hashlib.pbkdf2_hmac(
        "sha256", _saslprep(password).encode("utf-8"), salt, iterations
    )
    stored_key = hashlib.sha256(hmac.digest(salted, b"Client Key", "sha256")).digest()
    server_key = hmac.digest(salted, b"Server Key", "sha256")
    return (
        f"SCRAM-SHA-256${iterations}:{_b64(salt)}"
        f"${_b64(stored_key)}:{_b64(server_key)}"
    )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


_PROHIBITED = (
    stringprep.in_table_c12,
    stringprep.in_table_c21,
    stringprep.in_table_c22,
    stringprep.in_table_c3,
    stringprep.in_table_c4,
    stringprep.in_table_c5,
    stringprep.in_table_c6,
    stringprep.in_table_c7,
    stringprep.in_table_c8,
    stringprep.in_table_c9,
    stringprep.in_table_a1,
)


def _saslprep(password: str) -> str:
    """Prepare a password as PostgreSQL's ``pg_saslprep`` does (RFC 4013).

    The same steps as ``cloudfall.secrets``: PostgreSQL falls back to the
    password as given when the prepared form is not usable, so this does too.
    """
    if password.isascii():
        return password
    mapped = "".join(
        " " if stringprep.in_table_c12(char) else char
        for char in password
        if stringprep.in_table_c12(char) or not stringprep.in_table_b1(char)
    )
    prepared = unicodedata.normalize("NFKC", mapped)
    if not prepared or any(
        prohibited(char) for char in prepared for prohibited in _PROHIBITED
    ):
        return password
    if any(stringprep.in_table_d1(char) for char in prepared) and (
        any(stringprep.in_table_d2(char) for char in prepared)
        or not stringprep.in_table_d1(prepared[0])
        or not stringprep.in_table_d1(prepared[-1])
    ):
        return password
    return prepared


class FilterModule:
    """The role's filters."""

    def filters(self) -> dict[str, Callable[..., str]]:
        """Name the filters for Ansible."""
        return {
            "cloudfall_scram_verifier": scram_verifier,
            "cloudfall_pg_stored_password_problem": stored_password_problem,
        }
