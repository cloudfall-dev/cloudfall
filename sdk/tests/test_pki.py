"""S-011 certificate provider, controller side: the project CA signs CSRs."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

import pytest
from cloudfall.domain import AbsolutePath, Hostname, ResourceId
from cloudfall.pki import (
    CERTIFICATE_LIFETIME,
    ERROR_CA_INVALID,
    ERROR_CSR_INVALID,
    CertificateState,
    PkiError,
    ca_reference,
    load_or_create_ca,
)
from cloudfall.requirements import (
    CertificateRequirement,
    CertificateUsage,
    RequirementName,
)
from cloudfall.secret_store import SecretStore
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime.datetime(2026, 11, 13, 12, 0, tzinfo=datetime.UTC)
PRODUCTION = ResourceId("production")


def _decrypt(source: Path) -> str:
    return "".join(
        f"{line}\n"
        for line in source.read_text(encoding="utf-8").splitlines()
        if not line.startswith("sops_")
    )


def _encrypt(_target: Path, plaintext: str) -> str:
    return plaintext + "sops_mac=fake\n"


def _store(tmp_path: Path) -> SecretStore:
    return SecretStore(tmp_path / "secrets", decrypt=_decrypt, encrypt=_encrypt)


def _requirement(
    usage: CertificateUsage = CertificateUsage.SERVER,
    names: tuple[str, ...] = ("logs.example.internal",),
) -> CertificateRequirement:
    return CertificateRequirement(
        RequirementName("logging-gateway"),
        usage,
        tuple(Hostname(name) for name in names),
        AbsolutePath("/etc/cloudfall/logging/server.crt"),
        AbsolutePath("/etc/cloudfall/logging/server.key"),
        AbsolutePath("/etc/cloudfall/logging/client-ca.crt"),
    )


def _csr(names: tuple[str, ...] = ("evil.example",)) -> str:
    """Ask for more than any requirement grants: a CA, other names."""
    key = ec.generate_private_key(ec.SECP256R1())
    request = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in names]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return request.public_bytes(serialization.Encoding.PEM).decode()


def _certificate(pem: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode())


def test_the_ca_is_created_once_and_loaded_after(tmp_path: Path) -> None:
    store = _store(tmp_path)

    created, was_created = load_or_create_ca(store, PRODUCTION, NOW)
    loaded, created_again = load_or_create_ca(store, PRODUCTION, NOW)

    assert (was_created, created_again) == (True, False)
    assert loaded.certificate == created.certificate
    assert store.source_path(ca_reference(PRODUCTION)) == (
        tmp_path / "secrets" / "production" / "pki" / "ca.env"
    )
    assert created.certificate.extensions.get_extension_for_class(
        x509.BasicConstraints
    ).value.ca
    assert "PrivateKey" not in repr(created)


def test_a_half_stored_ca_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    load_or_create_ca(store, PRODUCTION, NOW)
    source = store.source_path(ca_reference(PRODUCTION))
    kept = [
        line
        for line in source.read_text(encoding="utf-8").splitlines()
        if not line.startswith("CA_CERTIFICATE=")
    ]
    source.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")

    with pytest.raises(PkiError) as error:
        load_or_create_ca(store, PRODUCTION, NOW)

    assert error.value.code == ERROR_CA_INVALID


def test_a_signed_certificate_carries_only_what_the_requirement_grants(
    tmp_path: Path,
) -> None:
    ca, _ = load_or_create_ca(_store(tmp_path), PRODUCTION, NOW)

    signed = _certificate(ca.sign(_csr(), _requirement(), NOW))

    signed.verify_directly_issued_by(ca.certificate)
    names = signed.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert names.value.get_values_for_type(x509.DNSName) == ["logs.example.internal"]
    assert not signed.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    usages = signed.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(usages) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert signed.not_valid_after_utc == NOW + CERTIFICATE_LIFETIME


def test_a_client_certificate_is_good_for_client_auth_only(tmp_path: Path) -> None:
    ca, _ = load_or_create_ca(_store(tmp_path), PRODUCTION, NOW)
    requirement = _requirement(CertificateUsage.CLIENT, ("h2.example.internal",))

    signed = _certificate(ca.sign(_csr(), requirement, NOW))

    usages = signed.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(usages) == [ExtendedKeyUsageOID.CLIENT_AUTH]


def test_a_request_that_is_not_a_csr_is_refused(tmp_path: Path) -> None:
    ca, _ = load_or_create_ca(_store(tmp_path), PRODUCTION, NOW)

    with pytest.raises(PkiError) as error:
        ca.sign("-----BEGIN CERTIFICATE REQUEST-----\nAAAA\n", _requirement(), NOW)

    assert error.value.code == ERROR_CSR_INVALID


def test_the_state_of_a_held_certificate(tmp_path: Path) -> None:
    ca, _ = load_or_create_ca(_store(tmp_path), PRODUCTION, NOW)
    other_ca, _ = load_or_create_ca(_store(tmp_path / "other"), PRODUCTION, NOW)
    requirement = _requirement()
    fresh = ca.sign(_csr(), requirement, NOW)
    expiring = ca.sign(_csr(), requirement, NOW - datetime.timedelta(days=70))
    renamed = ca.sign(_csr(), _requirement(names=("old.example.internal",)), NOW)
    client = ca.sign(_csr(), _requirement(CertificateUsage.CLIENT), NOW)
    foreign = other_ca.sign(_csr(), requirement, NOW)

    states = {
        label: ca.state(pem, requirement, NOW)
        for label, pem in (
            ("missing", None),
            ("fresh", fresh),
            ("expiring", expiring),
            ("renamed", renamed),
            ("client", client),
            ("foreign", foreign),
            ("garbage", "not a certificate"),
        )
    }

    assert states == {
        "missing": CertificateState.WILL_CREATE,
        "fresh": CertificateState.MET,
        "expiring": CertificateState.WILL_RENEW,
        "renamed": CertificateState.WILL_RENEW,
        "client": CertificateState.WILL_RENEW,
        "foreign": CertificateState.FOREIGN,
        "garbage": CertificateState.WILL_RENEW,
    }
