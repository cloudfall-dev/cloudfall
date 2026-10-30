"""Secret reference resolution tests with an injected decryptor."""

from __future__ import annotations

import base64
import hashlib
import hmac
import stat
from pathlib import Path

import pytest
from cloudfall.domain import ResourceId
from cloudfall.lifecycle import EngineContext
from cloudfall.secrets import (
    SecretsError,
    SopsSecretProvider,
    render_environment,
    scram_sha256_verifier,
)

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
ENGINE = ROOT / "engine"


def _context(tmp_path: Path) -> EngineContext:
    return EngineContext(
        project_directory=EXAMPLES,
        schema_directory=SCHEMAS,
        engine_directory=ENGINE,
        inventory_file=tmp_path / "inventory.json",
    )


def _provider(tmp_path: Path) -> SopsSecretProvider:
    secrets = tmp_path / "secrets"
    (secrets / "production").mkdir(parents=True)
    (secrets / "production" / "shared.env").write_text(
        "SENTRY_DSN=https://shared.example/1\nLOG_LEVEL=warning\n",
        encoding="utf-8",
    )
    (secrets / "production" / "crm.env").write_text(
        "# application-wide\nLOG_LEVEL=info\n", encoding="utf-8"
    )
    (secrets / "production" / "crm" / "backend.env").parent.mkdir(
        parents=True, exist_ok=True
    )
    (secrets / "production" / "crm" / "backend.env").write_text(
        "STRIPE_KEY=sk_test_123\n", encoding="utf-8"
    )
    return SopsSecretProvider(
        secrets_directory=secrets,
        decrypt=lambda path: path.read_text(encoding="utf-8"),
    )


def test_render_environment_merges_refs_in_override_order(
    tmp_path: Path,
) -> None:
    output = tmp_path / "env" / "crm-backend.env"

    result = render_environment(
        _context(tmp_path),
        ResourceId.from_boundary("crm-backend"),
        _provider(tmp_path),
        output,
    )

    content = output.read_text(encoding="utf-8")
    assert "SENTRY_DSN=https://shared.example/1" in content
    assert "LOG_LEVEL=info" in content
    assert "LOG_LEVEL=warning" not in content
    assert "STRIPE_KEY=sk_test_123" in content
    assert "FEATURE_SIGNUPS=true" in content
    assert "REDIS_URL=redis://127.0.0.1:6379/0" in content
    mode = stat.S_IMODE(output.stat().st_mode)
    assert mode == 0o600
    assert result["status"] == "ok"
    assert result["keys"] == [
        "FEATURE_SIGNUPS",
        "LOG_LEVEL",
        "REDIS_URL",
        "SENTRY_DSN",
        "STRIPE_KEY",
    ]
    assert result["declaredKeys"] == ["FEATURE_SIGNUPS", "REDIS_URL"]
    serialized = str(result)
    assert "sk_test_123" not in serialized
    assert "https://shared.example/1" not in serialized


def test_missing_secret_source_fails_with_the_expected_path(
    tmp_path: Path,
) -> None:
    provider = SopsSecretProvider(
        secrets_directory=tmp_path / "secrets",
        decrypt=lambda path: path.read_text(encoding="utf-8"),
    )

    with pytest.raises(SecretsError) as caught:
        render_environment(
            _context(tmp_path),
            ResourceId.from_boundary("crm-backend"),
            provider,
            tmp_path / "out.env",
        )

    assert caught.value.code == "secrets_source_missing"
    assert "production/shared.env" in caught.value.message


def test_malformed_source_lines_fail_fast(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    shared = tmp_path / "secrets" / "production" / "shared.env"
    shared.write_text("not a pair\n", encoding="utf-8")

    with pytest.raises(SecretsError) as caught:
        render_environment(
            _context(tmp_path),
            ResourceId.from_boundary("crm-backend"),
            provider,
            tmp_path / "out.env",
        )

    assert caught.value.code == "secrets_source_invalid"
    assert "line 1" in caught.value.message


def test_unknown_component_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SecretsError) as caught:
        render_environment(
            _context(tmp_path),
            ResourceId.from_boundary("mystery"),
            _provider(tmp_path),
            tmp_path / "out.env",
        )

    assert caught.value.code == "secrets_component_unknown"


def test_declared_and_secret_key_conflicts_fail_fast(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    backend = tmp_path / "secrets" / "production" / "crm" / "backend.env"
    backend.write_text(
        "STRIPE_KEY=sk_test_123\nFEATURE_SIGNUPS=false\n", encoding="utf-8"
    )

    with pytest.raises(SecretsError) as caught:
        render_environment(
            _context(tmp_path),
            ResourceId.from_boundary("crm-backend"),
            provider,
            tmp_path / "out.env",
        )

    assert caught.value.code == "secrets_key_conflict"
    assert "FEATURE_SIGNUPS" in caught.value.message
    assert "exactly one place" in caught.value.message


def _verifier_parts(verifier: str) -> tuple[int, bytes, bytes, bytes]:
    """Split PostgreSQL's stored SCRAM-SHA-256 format, refusing any other shape."""
    scheme, _, rest = verifier.partition("$")
    assert scheme == "SCRAM-SHA-256"
    iterations_and_salt, _, keys = rest.partition("$")
    iterations, _, salt = iterations_and_salt.partition(":")
    stored_key, _, server_key = keys.partition(":")
    decoded = [
        base64.b64decode(part, validate=True)
        for part in (salt, stored_key, server_key)
    ]
    assert all(
        base64.b64encode(raw).decode() == part
        for raw, part in zip(decoded, (salt, stored_key, server_key), strict=True)
    )
    return int(iterations), decoded[0], decoded[1], decoded[2]


def verifies(verifier: str, password: str) -> bool:
    """Recompute StoredKey and ServerKey from the verifier's salt and iterations."""
    iterations, salt, stored_key, server_key = _verifier_parts(verifier)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.digest(salted, b"Client Key", "sha256")
    return (
        hashlib.sha256(client_key).digest() == stored_key
        and hmac.digest(salted, b"Server Key", "sha256") == server_key
    )


# RFC 7677 section 3: the SCRAM-SHA-256 exchange for user "user", password "pencil".
_RFC7677_SALT = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
_RFC7677_AUTH_MESSAGE = (
    b"n=user,r=rOprNGfwEbeRWgbNEkqO,"
    b"r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
    b"s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
    b"c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
)
_RFC7677_CLIENT_PROOF = base64.b64decode(
    "dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ="
)
_RFC7677_SERVER_SIGNATURE = base64.b64decode(
    "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
)


def test_the_verifier_authenticates_the_rfc_7677_exchange() -> None:
    verifier = scram_sha256_verifier("pencil", salt=_RFC7677_SALT)

    iterations, salt, stored_key, server_key = _verifier_parts(verifier)
    assert verifier.startswith("SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ==$")
    assert (iterations, salt) == (4096, _RFC7677_SALT)
    # What the server checks: the proof recovers a ClientKey hashing to StoredKey,
    # and ServerKey signs the same exchange the RFC's server signed.
    signature = hmac.digest(stored_key, _RFC7677_AUTH_MESSAGE, "sha256")
    client_key = bytes(
        a ^ b for a, b in zip(_RFC7677_CLIENT_PROOF, signature, strict=True)
    )
    assert hashlib.sha256(client_key).digest() == stored_key
    assert (
        hmac.digest(server_key, _RFC7677_AUTH_MESSAGE, "sha256")
        == _RFC7677_SERVER_SIGNATURE
    )


def test_the_verifier_has_a_fresh_salt_and_never_holds_the_password() -> None:
    password = "s3cr3t-billing-value"  # noqa: S105 - a test value

    first = scram_sha256_verifier(password)
    second = scram_sha256_verifier(password)

    assert first != second
    for verifier in (first, second):
        assert len(_verifier_parts(verifier)[1]) == 16
        assert verifies(verifier, password)
        assert not verifies(verifier, password + "x")
        assert password not in verifier


@pytest.mark.parametrize(
    ("password", "prepared"),
    [
        ("pass\u00a0word", "pass word"),
        ("pass\u200bword", "pass word"),
        ("pass\u00adword", "password"),
        ("\uff21bc", "Abc"),
        ("ab\ue000", "ab\ue000"),
        ("ctl\x07", "ctl\x07"),
    ],
    ids=[
        "space-mapped",
        "zero-width-space-mapped",
        "soft-hyphen-dropped",
        "nfkc",
        "prohibited-kept",
        "ascii-kept",
    ],
)
def test_the_verifier_hashes_the_password_as_postgresql_saslprep_does(
    password: str, prepared: str
) -> None:
    assert verifies(scram_sha256_verifier(password), prepared)


def test_an_empty_salt_is_refused() -> None:
    with pytest.raises(ValueError, match="salt"):
        scram_sha256_verifier("pencil", salt=b"")
