"""The certificate provider of S-011, controller side: a project CA that signs CSRs.

Each environment has one CA. Its private key and certificate live in the
secret store, sops-encrypted, at ``secrets/<environment>/pki/ca.env`` (keys
``CA_KEY`` and ``CA_CERTIFICATE``, each PEM in base64 on one line). A host
makes its own key and a certificate signing request; the CA signs only the
public key of that request, and every name, usage and lifetime comes from
the requirement, never from the request. So a host's private key never
leaves it, and a request cannot widen what its certificate is good for.
"""

from __future__ import annotations

import base64
import datetime
import ipaddress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from cloudfall.domain import AbsolutePath, SecretKeyName
from cloudfall.inventory import SecretValueReference
from cloudfall.requirements import CertificateRequirement, CertificateUsage
from cloudfall.secrets import SecretsError

if TYPE_CHECKING:
    from cloudfall.domain import ResourceId
    from cloudfall.secret_store import SecretStore

ERROR_CSR_INVALID = "pki_csr_invalid"
ERROR_CA_INVALID = "pki_ca_invalid"
CA_LIFETIME = datetime.timedelta(days=3650)
CERTIFICATE_LIFETIME = datetime.timedelta(days=90)
RENEW_BEFORE = datetime.timedelta(days=30)
"""A certificate this close to its expiry is renewed on the next deploy."""
_CA_PATH = AbsolutePath("/pki/ca")
_CA_KEY = SecretKeyName("CA_KEY")
_CA_CERTIFICATE = SecretKeyName("CA_CERTIFICATE")
_CLOCK_SKEW = datetime.timedelta(minutes=5)


class PkiError(RuntimeError):
    """A certificate step that cannot go on, named by a stable code."""

    def __init__(self, code: str, message: str) -> None:
        """Capture the code beside the message."""
        super().__init__(message)
        self.code = code
        self.message = message


class CertificateState(StrEnum):
    """What a certificate already on a host needs."""

    MET = "met"
    WILL_CREATE = "will-create"
    """No certificate at the path yet."""
    WILL_RENEW = "will-renew"
    """Signed by the project CA, but expiring, or for other names or usage."""
    FOREIGN = "foreign"
    """Valid, but not signed by the project CA: provided by hand, kept as it is."""


@dataclass(frozen=True, slots=True)
class ProjectCa:
    """One environment's CA: its key stays in memory while it signs."""

    environment: ResourceId
    certificate: x509.Certificate
    key: ec.EllipticCurvePrivateKey = field(repr=False)

    @property
    def certificate_pem(self) -> str:
        """Return the CA certificate, the file every host trusts."""
        return self.certificate.public_bytes(serialization.Encoding.PEM).decode()

    def sign(
        self,
        csr_pem: str,
        requirement: CertificateRequirement,
        now: datetime.datetime,
    ) -> str:
        """Sign the public key of ``csr_pem`` for exactly what ``requirement`` says."""
        try:
            request = x509.load_pem_x509_csr(csr_pem.encode())
        except ValueError as error:
            message = f"{requirement.name.value}: not a PEM certificate request"
            raise PkiError(ERROR_CSR_INVALID, message) from error
        if not request.is_signature_valid:
            message = (
                f"{requirement.name.value}: the request's signature does not match "
                "its key"
            )
            raise PkiError(ERROR_CSR_INVALID, message)
        public_key = request.public_key()
        if not isinstance(public_key, ec.EllipticCurvePublicKey | rsa.RSAPublicKey):
            message = f"{requirement.name.value}: only EC and RSA keys are signed"
            raise PkiError(ERROR_CSR_INVALID, message)
        names = [name.value.removesuffix(".") for name in requirement.subject_names]
        usage = (
            ExtendedKeyUsageOID.SERVER_AUTH
            if requirement.usage is CertificateUsage.SERVER
            else ExtendedKeyUsageOID.CLIENT_AUTH
        )
        certificate = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
            )
            .issuer_name(self.certificate.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _CLOCK_SKEW)
            .not_valid_after(now + CERTIFICATE_LIFETIME)
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=isinstance(public_key, rsa.RSAPublicKey),
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
            .add_extension(
                x509.SubjectAlternativeName([_general_name(name) for name in names]),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    self.key.public_key()
                ),
                critical=False,
            )
            .sign(self.key, hashes.SHA256())
        )
        return certificate.public_bytes(serialization.Encoding.PEM).decode()

    def state(
        self,
        certificate_pem: str | None,
        requirement: CertificateRequirement,
        now: datetime.datetime,
    ) -> CertificateState:
        """Return what the certificate a host holds at the requirement's path needs."""
        if certificate_pem is None:
            return CertificateState.WILL_CREATE
        try:
            held = x509.load_pem_x509_certificate(certificate_pem.encode())
        except ValueError:
            return CertificateState.WILL_RENEW
        if not self._issued(held):
            return CertificateState.FOREIGN
        names = {
            name.value.removesuffix(".").lower() for name in requirement.subject_names
        }
        current = (
            held.not_valid_after_utc - now > RENEW_BEFORE
            and certificate_names(held) == names
            and _usage(held) is requirement.usage
        )
        return CertificateState.MET if current else CertificateState.WILL_RENEW

    def _issued(self, held: x509.Certificate) -> bool:
        if held.issuer != self.certificate.subject:
            return False
        try:
            held.verify_directly_issued_by(self.certificate)
        except ValueError, TypeError, InvalidSignature:
            return False
        return True


def ca_reference(environment: ResourceId) -> SecretValueReference:
    """Return where the store keeps the environment CA's key."""
    return SecretValueReference(environment, _CA_PATH, _CA_KEY)


def load_or_create_ca(
    store: SecretStore, environment: ResourceId, now: datetime.datetime
) -> tuple[ProjectCa, bool]:
    """Return the environment's CA, creating and storing it on first use.

    The second item says whether it was created now.
    """
    key_reference = ca_reference(environment)
    certificate_reference = SecretValueReference(environment, _CA_PATH, _CA_CERTIFICATE)
    held_key = store.holds(key_reference)
    held_certificate = store.holds(certificate_reference)
    if held_key != held_certificate:
        message = (
            f"{store.source_path(key_reference)} holds only one of CA_KEY and "
            "CA_CERTIFICATE; restore the other or remove both"
        )
        raise PkiError(ERROR_CA_INVALID, message)
    if held_key:
        return _loaded_ca(
            store, environment, key_reference, certificate_reference
        ), False
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Cloudfall"),
            x509.NameAttribute(
                NameOID.COMMON_NAME, f"Cloudfall {environment.value} CA"
            ),
        ]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _CLOCK_SKEW)
        .not_valid_after(now + CA_LIFETIME)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
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
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    store.write(key_reference, base64.b64encode(key_pem).decode())
    store.write(
        certificate_reference,
        base64.b64encode(certificate.public_bytes(serialization.Encoding.PEM)).decode(),
    )
    return ProjectCa(environment, certificate, key), True


def _loaded_ca(
    store: SecretStore,
    environment: ResourceId,
    key_reference: SecretValueReference,
    certificate_reference: SecretValueReference,
) -> ProjectCa:
    try:
        key = serialization.load_pem_private_key(
            base64.b64decode(store.value(key_reference), validate=True), password=None
        )
        certificate = x509.load_pem_x509_certificate(
            base64.b64decode(store.value(certificate_reference), validate=True)
        )
    except (ValueError, TypeError, SecretsError) as error:
        message = (
            f"the {environment.value} CA in the secret store is unreadable: {error}"
        )
        raise PkiError(ERROR_CA_INVALID, message) from error
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        message = f"the {environment.value} CA key is not an EC key"
        raise PkiError(ERROR_CA_INVALID, message)
    if certificate.public_key() != key.public_key():
        message = f"the {environment.value} CA certificate does not match its key"
        raise PkiError(ERROR_CA_INVALID, message)
    return ProjectCa(environment, certificate, key)


def _general_name(name: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)


def certificate_names(certificate: x509.Certificate) -> set[str]:
    """Return a certificate's DNS and IP subject alternative names, lowercased."""
    try:
        extension = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
    except x509.ExtensionNotFound:
        return set()
    return {
        str(value).lower()
        for value in (
            *extension.value.get_values_for_type(x509.DNSName),
            *extension.value.get_values_for_type(x509.IPAddress),
        )
    }


def _usage(certificate: x509.Certificate) -> CertificateUsage | None:
    try:
        extension = certificate.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage
        )
    except x509.ExtensionNotFound:
        return None
    usages = set(extension.value)
    if usages == {ExtendedKeyUsageOID.SERVER_AUTH}:
        return CertificateUsage.SERVER
    if usages == {ExtendedKeyUsageOID.CLIENT_AUTH}:
        return CertificateUsage.CLIENT
    return None
