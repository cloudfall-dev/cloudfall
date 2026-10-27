"""Collect and load evidence for public domain service lifecycles."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, cast

from jsonschema.exceptions import ValidationError

from cloudfall.domain import (
    AbsolutePath,
    ConnectionAddress,
    Hostname,
    HttpScheme,
    HttpStatusCode,
    IpAddress,
    PositiveCount,
    ResourceId,
    Sha256Digest,
    SourceLocation,
    TcpPort,
)
from cloudfall.validation import ConfigValidationError, SchemaCatalog, ValidationIssue

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from cloudfall.inventory import DomainInventory, PlatformInventory, ServerInventory

_DEPLOYMENT_RECEIPT_SCHEMA = "deployment-receipt.schema.json"
_OBSERVED_DOMAIN_SCHEMA = "observed-domain.schema.json"
_MAX_ERROR_LENGTH = 500


@dataclass(frozen=True, slots=True)
class EvidenceTimestamp:
    """UTC timestamp carried by deployment and service evidence."""

    value: datetime

    def __post_init__(self) -> None:
        """Require timezone-aware UTC evidence timestamps."""
        if self.value.tzinfo is None or self.value.utcoffset() is None:
            message = "evidence timestamp must include a timezone"
            raise ValueError(message)

    @classmethod
    def now(cls) -> EvidenceTimestamp:
        """Return the current UTC time."""
        return cls(datetime.now(UTC))

    @classmethod
    def from_boundary(cls, value: object) -> EvidenceTimestamp:
        """Parse a schema-validated RFC 3339 timestamp."""
        if not isinstance(value, str):
            message = "evidence timestamp must be a string"
            raise TypeError(message)
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            message = "evidence timestamp must include a timezone"
            raise ValueError(message)
        return cls(parsed.astimezone(UTC))

    def as_string(self) -> str:
        """Serialize canonical UTC RFC 3339."""
        return self.value.astimezone(UTC).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )


@dataclass(frozen=True, slots=True)
class DeploymentReceipt:
    """Evidence that one domain configuration was applied to a proxy server."""

    domain_id: ResourceId
    server_id: ResourceId
    deployed_at: EvidenceTimestamp
    configuration_path: AbsolutePath
    configuration_sha256: Sha256Digest
    source: Path


@dataclass(frozen=True, slots=True)
class DeploymentReceiptSet:
    """Deployment receipts indexed by domain identifier."""

    receipts: tuple[DeploymentReceipt, ...]
    _index: Mapping[ResourceId, DeploymentReceipt]

    @classmethod
    def empty(cls) -> DeploymentReceiptSet:
        """Return an empty receipt set."""
        return cls(receipts=(), _index={})

    def for_domain(self, domain_id: ResourceId) -> DeploymentReceipt | None:
        """Return deployment evidence for a domain, if supplied."""
        return self._index.get(domain_id)


@dataclass(frozen=True, slots=True)
class EndpointEvidence:
    """One HTTP endpoint probe result."""

    reachable: bool
    status: HttpStatusCode | None
    error: str | None
    skipped: bool = False
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TlsEvidence:
    """TLS handshake and certificate evidence."""

    available: bool
    valid: bool
    expires_at: EvidenceTimestamp | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ObservedDomainSnapshot:
    """Validated DNS, TLS, origin, and public route evidence."""

    domain_id: ResourceId
    observed_at: EvidenceTimestamp
    dns_addresses: tuple[IpAddress, ...]
    proxy_address_observed: bool
    edge_detected: bool
    dns_error: str | None
    tls: TlsEvidence
    origin: EndpointEvidence
    public: EndpointEvidence
    source: Path


@dataclass(frozen=True, slots=True)
class DomainObservationSet:
    """Domain observations indexed by desired domain identifier."""

    snapshots: tuple[ObservedDomainSnapshot, ...]
    _index: Mapping[ResourceId, ObservedDomainSnapshot]

    @classmethod
    def empty(cls) -> DomainObservationSet:
        """Return an empty domain observation set."""
        return cls(snapshots=(), _index={})

    def for_domain(self, domain_id: ResourceId) -> ObservedDomainSnapshot | None:
        """Return route evidence for a domain, if supplied."""
        return self._index.get(domain_id)


@dataclass(frozen=True, slots=True)
class NetworkResponse:
    """Raw network response used to build persisted route evidence."""

    endpoint: EndpointEvidence
    tls: TlsEvidence
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    """Strict connection and HTTP request inputs for one endpoint probe."""

    address: ConnectionAddress
    port: TcpPort
    server_name: Hostname
    scheme: HttpScheme
    path: str
    timeout: PositiveCount


class DomainNetworkClient(Protocol):
    """Network boundary used by domain inspection."""

    def resolve(self, hostname: Hostname, port: TcpPort) -> tuple[IpAddress, ...]:
        """Resolve every canonical address for a hostname."""

    def request(self, target: ProbeTarget) -> NetworkResponse:
        """Probe an endpoint with explicit connect address, Host, and TLS SNI."""
        ...


class SocketDomainNetworkClient:
    """Standard-library DNS, TLS, and HTTP network client."""

    def resolve(self, hostname: Hostname, port: TcpPort) -> tuple[IpAddress, ...]:
        """Resolve deterministic unique IPv4 and IPv6 addresses."""
        records = socket.getaddrinfo(
            hostname.value,
            port.value,
            type=socket.SOCK_STREAM,
        )
        addresses = {
            IpAddress.from_boundary(record[4][0])
            for record in records
        }
        return tuple(sorted(addresses, key=lambda address: address.value))

    def request(self, target: ProbeTarget) -> NetworkResponse:
        """Perform one certificate-verifying HTTP request."""
        tls = TlsEvidence(available=False, valid=False, expires_at=None, error=None)
        stream: socket.socket | ssl.SSLSocket | None = None
        try:
            stream = socket.create_connection(
                (target.address.value, target.port.value),
                timeout=target.timeout.value,
            )
            if target.scheme is HttpScheme.HTTPS:
                context = ssl.create_default_context()
                stream = context.wrap_socket(
                    stream,
                    server_hostname=target.server_name.value,
                )
                expires_at = _certificate_expiry(stream.getpeercert())
                tls = TlsEvidence(
                    available=True,
                    valid=True,
                    expires_at=expires_at,
                    error=None,
                )
            request = (
                f"GET {target.path} HTTP/1.1\r\n"
                f"Host: {target.server_name.value}\r\n"
                "User-Agent: cloudfall-service-inspect/0.1\r\n"
                "Accept: */*\r\n"
                "Connection: close\r\n\r\n"
            )
            stream.sendall(request.encode("ascii"))
            response = http.client.HTTPResponse(stream)
            response.begin()
            headers = {key.lower(): value for key, value in response.getheaders()}
            endpoint = EndpointEvidence(
                reachable=True,
                status=HttpStatusCode(response.status),
                error=None,
            )
            return NetworkResponse(endpoint=endpoint, tls=tls, headers=headers)
        except (OSError, ValueError, ssl.SSLError, http.client.HTTPException) as error:
            message = _error_message(error)
            return NetworkResponse(
                endpoint=EndpointEvidence(
                    reachable=False,
                    status=None,
                    error=message,
                ),
                tls=TlsEvidence(
                    available=tls.available,
                    valid=False,
                    expires_at=tls.expires_at,
                    error=message if target.scheme is HttpScheme.HTTPS else None,
                ),
                headers={},
            )
        finally:
            if stream is not None:
                stream.close()


def inspect_domains(
    inventory: PlatformInventory,
    output_directory: Path,
    client: DomainNetworkClient,
    *,
    observed_at: EvidenceTimestamp,
) -> tuple[Path, ...]:
    """Probe every desired domain and write normalized evidence snapshots."""
    output_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths: list[Path] = []
    for domain in inventory.domains:
        content = _inspect_domain(inventory, domain, client, observed_at)
        path = output_directory / f"{domain.resource_id.value}.json"
        _atomic_json_write(path, content)
        paths.append(path)
    return tuple(paths)


def _inspect_domain(
    inventory: PlatformInventory,
    domain: DomainInventory,
    client: DomainNetworkClient,
    observed_at: EvidenceTimestamp,
) -> dict[str, object]:
    proxy = _server(inventory, domain.proxy.server_id)
    origin = _server(inventory, domain.origin.server_id)
    dns_error: str | None = None
    try:
        addresses = client.resolve(
            domain.primary_name,
            _public_port(domain.health_check.scheme),
        )
    except OSError as error:
        addresses = ()
        dns_error = _error_message(error)
    proxy_addresses = _server_addresses(proxy, client)
    if domain.origin.server_id == domain.proxy.server_id:
        # A single-host route has no separate origin hop to probe: the
        # public probe already traverses the same proxy and upstream, and
        # the origin port is rightly firewalled from the outside.
        origin_evidence: dict[str, object] = {
            "reachable": False,
            "status": None,
            "error": None,
            "skipped": True,
            "skipReason": (
                "origin shares the proxy host; covered by the public probe"
            ),
        }
    else:
        origin_response = client.request(
            ProbeTarget(
                address=origin.address,
                port=domain.origin.port,
                server_name=domain.origin.server_name,
                scheme=domain.origin.scheme,
                path=domain.health_check.path,
                timeout=domain.health_check.timeout_seconds,
            )
        )
        origin_evidence = _endpoint_dict(origin_response.endpoint)
    public_response = client.request(
        ProbeTarget(
            address=ConnectionAddress(domain.primary_name.value),
            port=_public_port(domain.health_check.scheme),
            server_name=domain.primary_name,
            scheme=domain.health_check.scheme,
            path=domain.health_check.path,
            timeout=domain.health_check.timeout_seconds,
        )
    )
    edge_detected = _edge_detected(domain, public_response.headers)
    return {
        "apiVersion": "cloudfall/v1",
        "kind": "ObservedDomain",
        "metadata": {
            "id": domain.resource_id.value,
            "description": "Read-only domain route evidence collected by Cloudfall",
        },
        "spec": {
            "domain": domain.resource_id.value,
            "observedAt": observed_at.as_string(),
            "dns": {
                "addresses": [address.value for address in addresses],
                "proxyAddressObserved": bool(set(addresses) & set(proxy_addresses)),
                "edgeDetected": edge_detected,
                "error": dns_error,
            },
            "tls": _tls_dict(public_response.tls),
            "origin": origin_evidence,
            "public": _endpoint_dict(public_response.endpoint),
        },
    }


def load_deployment_receipts(
    receipt_directory: Path, schema_directory: Path
) -> DeploymentReceiptSet:
    """Load strictly validated deployment receipts."""
    contents = _validated_json_documents(
        receipt_directory,
        schema_directory,
        _DEPLOYMENT_RECEIPT_SCHEMA,
        "deployment receipt",
    )
    receipts: list[DeploymentReceipt] = []
    index: dict[ResourceId, DeploymentReceipt] = {}
    for path, content in contents:
        receipt = _deployment_receipt(path, content)
        if receipt.domain_id in index:
            _raise_duplicate(
                receipt.domain_id, index[receipt.domain_id].source, path, "deployment"
            )
        receipts.append(receipt)
        index[receipt.domain_id] = receipt
    return DeploymentReceiptSet(receipts=tuple(receipts), _index=index)


def load_deployment_receipt(path: Path, schema_directory: Path) -> DeploymentReceipt:
    """Load one strictly validated deployment receipt file."""
    content = _load_json(path)
    _validate_document(
        SchemaCatalog(schema_directory),
        path,
        content,
        _DEPLOYMENT_RECEIPT_SCHEMA,
        "deployment receipt",
    )
    return _deployment_receipt(path, content)


def _deployment_receipt(path: Path, content: Mapping[str, object]) -> DeploymentReceipt:
    metadata = _mapping(content, "metadata")
    spec = _mapping(content, "spec")
    domain_id = ResourceId.from_boundary(spec.get("domain"))
    _require_matching_identity(metadata, domain_id, path, "deployment")
    return DeploymentReceipt(
        domain_id=domain_id,
        server_id=ResourceId.from_boundary(spec.get("server")),
        deployed_at=EvidenceTimestamp.from_boundary(spec.get("deployedAt")),
        configuration_path=AbsolutePath.from_boundary(spec.get("configurationPath")),
        configuration_sha256=Sha256Digest.from_boundary(
            spec.get("configurationSha256")
        ),
        source=path,
    )


def load_domain_observations(
    observation_directory: Path, schema_directory: Path
) -> DomainObservationSet:
    """Load strictly validated public domain observations."""
    contents = _validated_json_documents(
        observation_directory,
        schema_directory,
        _OBSERVED_DOMAIN_SCHEMA,
        "domain observation",
    )
    snapshots: list[ObservedDomainSnapshot] = []
    index: dict[ResourceId, ObservedDomainSnapshot] = {}
    for path, content in contents:
        metadata = _mapping(content, "metadata")
        spec = _mapping(content, "spec")
        domain_id = ResourceId.from_boundary(spec.get("domain"))
        _require_matching_identity(metadata, domain_id, path, "domain observation")
        if domain_id in index:
            _raise_duplicate(domain_id, index[domain_id].source, path, "observation")
        dns = _mapping(spec, "dns")
        tls = _mapping(spec, "tls")
        snapshot = ObservedDomainSnapshot(
            domain_id=domain_id,
            observed_at=EvidenceTimestamp.from_boundary(spec.get("observedAt")),
            dns_addresses=tuple(
                IpAddress.from_boundary(item)
                for item in _sequence(dns.get("addresses"), "DNS addresses")
            ),
            proxy_address_observed=_boolean(dns, "proxyAddressObserved"),
            edge_detected=_boolean(dns, "edgeDetected"),
            dns_error=_optional_string(dns, "error"),
            tls=TlsEvidence(
                available=_boolean(tls, "available"),
                valid=_boolean(tls, "valid"),
                expires_at=_optional_timestamp(tls.get("expiresAt")),
                error=_optional_string(tls, "error"),
            ),
            origin=_endpoint(_mapping(spec, "origin")),
            public=_endpoint(_mapping(spec, "public")),
            source=path,
        )
        snapshots.append(snapshot)
        index[domain_id] = snapshot
    return DomainObservationSet(snapshots=tuple(snapshots), _index=index)


def _validated_json_documents(
    directory: Path,
    schema_directory: Path,
    schema_name: str,
    concept: str,
) -> tuple[tuple[Path, Mapping[str, object]], ...]:
    if not directory.is_dir():
        issue = ValidationIssue(
            code=f"{concept.replace(' ', '_')}_directory_missing",
            message=f"{concept} directory does not exist: {directory}",
        )
        raise ConfigValidationError(issue)
    catalog = SchemaCatalog(schema_directory)
    documents: list[tuple[Path, Mapping[str, object]]] = []
    for path in sorted(directory.rglob("*.json")):
        content = _load_json(path)
        _validate_document(catalog, path, content, schema_name, concept)
        documents.append((path, content))
    return tuple(documents)


def _validate_document(
    catalog: SchemaCatalog,
    path: Path,
    content: Mapping[str, object],
    schema_name: str,
    concept: str,
) -> None:
    try:
        catalog.validate_named(schema_name, content)
    except ValidationError as error:
        issue = ValidationIssue(
            code=f"{concept.replace(' ', '_')}_schema_validation_failed",
            message=error.message,
            source=SourceLocation(path=path, document_number=1),
            field_path=tuple(error.absolute_path),
        )
        raise ConfigValidationError(issue) from error


def _load_json(path: Path) -> Mapping[str, object]:
    try:
        raw = cast("object", json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as error:
        issue = ValidationIssue(
            code="service_evidence_json_invalid",
            message=error.msg,
            source=SourceLocation(path=path, document_number=1),
        )
        raise ConfigValidationError(issue) from error
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        issue = ValidationIssue(
            code="service_evidence_shape_invalid",
            message="service evidence root must be an object",
            source=SourceLocation(path=path, document_number=1),
        )
        raise ConfigValidationError(issue)
    return cast("Mapping[str, object]", raw)


def _server(inventory: PlatformInventory, server_id: ResourceId) -> ServerInventory:
    server = next(
        (item for item in inventory.servers if item.resource_id == server_id),
        None,
    )
    if server is None:
        message = f"validated domain server does not exist: {server_id.value}"
        raise KeyError(message)
    return server


def _server_addresses(
    server: ServerInventory, client: DomainNetworkClient
) -> tuple[IpAddress, ...]:
    if server.network is not None:
        return (IpAddress(server.network.ipv4.value),)
    try:
        return (IpAddress(server.address.value),)
    except ValueError:
        return client.resolve(Hostname(server.address.value), TcpPort(443))


def _public_port(scheme: HttpScheme) -> TcpPort:
    return TcpPort(443 if scheme is HttpScheme.HTTPS else 80)


def _edge_detected(
    domain: DomainInventory, headers: Mapping[str, str]
) -> bool:
    if domain.edge.provider.value != "cloudflare":
        return False
    server = headers.get("server", "").lower()
    return "cf-ray" in headers or server == "cloudflare"


def _endpoint(content: Mapping[str, object]) -> EndpointEvidence:
    raw_status = content.get("status")
    return EndpointEvidence(
        reachable=_boolean(content, "reachable"),
        status=(
            HttpStatusCode.from_boundary(raw_status)
            if raw_status is not None
            else None
        ),
        error=_optional_string(content, "error"),
        skipped=bool(content.get("skipped", False)),
        skip_reason=_optional_string(content, "skipReason"),
    )


def _endpoint_dict(evidence: EndpointEvidence) -> dict[str, object]:
    return {
        "reachable": evidence.reachable,
        "status": evidence.status.value if evidence.status is not None else None,
        "error": evidence.error,
    }


def _tls_dict(evidence: TlsEvidence) -> dict[str, object]:
    return {
        "available": evidence.available,
        "valid": evidence.valid,
        "expiresAt": (
            evidence.expires_at.as_string()
            if evidence.expires_at is not None
            else None
        ),
        "error": evidence.error,
    }


def _mapping(content: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = content.get(key)
    if not isinstance(value, dict) or not all(
        isinstance(child_key, str) for child_key in value
    ):
        message = f"validated field {key!r} is not an object"
        raise TypeError(message)
    return cast("Mapping[str, object]", value)


def _sequence(value: object, concept: str) -> tuple[object, ...]:
    if not isinstance(value, list):
        message = f"validated {concept} is not a list"
        raise TypeError(message)
    return tuple(value)


def _boolean(content: Mapping[str, object], key: str) -> bool:
    value = content.get(key)
    if not isinstance(value, bool):
        message = f"validated field {key!r} is not a boolean"
        raise TypeError(message)
    return value


def _optional_string(content: Mapping[str, object], key: str) -> str | None:
    value = content.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        message = f"validated field {key!r} is not a string or null"
        raise TypeError(message)
    return value


def _optional_timestamp(value: object) -> EvidenceTimestamp | None:
    return EvidenceTimestamp.from_boundary(value) if value is not None else None


def _require_matching_identity(
    metadata: Mapping[str, object],
    domain_id: ResourceId,
    path: Path,
    concept: str,
) -> None:
    metadata_id = ResourceId.from_boundary(metadata.get("id"))
    if metadata_id != domain_id:
        issue = ValidationIssue(
            code=f"{concept.replace(' ', '_')}_identity_mismatch",
            message=f"metadata.id {metadata_id} does not match domain {domain_id}",
            source=SourceLocation(path=path, document_number=1),
        )
        raise ConfigValidationError(issue)


def _raise_duplicate(
    domain_id: ResourceId,
    first_path: Path,
    path: Path,
    concept: str,
) -> None:
    issue = ValidationIssue(
        code=f"{concept}_duplicate",
        message=f"domain {domain_id} already supplied at {first_path}",
        source=SourceLocation(path=path, document_number=1),
    )
    raise ConfigValidationError(issue)


def _error_message(error: BaseException) -> str:
    message = str(error) or type(error).__name__
    return message[:_MAX_ERROR_LENGTH]


def _certificate_expiry(certificate: object) -> EvidenceTimestamp | None:
    if not isinstance(certificate, dict):
        message = "TLS peer did not provide a certificate"
        raise TypeError(message)
    not_after = certificate.get("notAfter")
    if not isinstance(not_after, str):
        return None
    return EvidenceTimestamp(
        datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=UTC)
    )


def _atomic_json_write(path: Path, content: Mapping[str, object]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        f"{json.dumps(content, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)
