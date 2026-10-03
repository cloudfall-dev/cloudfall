"""Serve a live-refreshing read-only operations dashboard over HTTP."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from cloudfall.dashboard import RefreshInterval, render_dashboard_html
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.operations import UtcTimestamp, build_operations_view
from cloudfall.service_evidence import (
    DeploymentReceiptSet,
    DomainObservationSet,
    EvidenceTimestamp,
    SocketDomainNetworkClient,
    inspect_domains,
    load_deployment_receipts,
    load_domain_observations,
)
from cloudfall.validation import ConfigValidationError, validate_config

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from cloudfall.operations import FleetOperations

_PORT_MAXIMUM = 65_535


@dataclass(frozen=True, slots=True)
class ListenEndpoint:
    """Network address the dashboard server binds to."""

    host: str
    port: int

    def __post_init__(self) -> None:
        """Reject empty hosts and out-of-range ports."""
        if not self.host:
            message = "listen host must not be empty"
            raise ValueError(message)
        if not 0 <= self.port <= _PORT_MAXIMUM:
            message = f"listen port must be within 0..{_PORT_MAXIMUM}, got {self.port}"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class EvidenceSources:
    """Filesystem locations the dashboard re-derives its view from."""

    project_directory: Path
    schema_directory: Path
    observed_directory: Path
    service_observed_directory: Path
    deployments_directory: Path
    inspect_services: bool = False

    def operations_view(self) -> FleetOperations:
        """Re-validate state and evidence, then build a fresh projection."""
        state = validate_config(self.project_directory, self.schema_directory)
        inventory = PlatformInventory.from_state(state)
        if self.inspect_services:
            inspect_domains(
                inventory,
                self.service_observed_directory,
                SocketDomainNetworkClient(),
                observed_at=EvidenceTimestamp.now(),
            )
        observations = load_observations(
            self.observed_directory, self.schema_directory
        )
        deployment_receipts = (
            load_deployment_receipts(
                self.deployments_directory, self.schema_directory
            )
            if self.deployments_directory.is_dir()
            else DeploymentReceiptSet.empty()
        )
        domain_observations = (
            load_domain_observations(
                self.service_observed_directory, self.schema_directory
            )
            if self.service_observed_directory.is_dir()
            else DomainObservationSet.empty()
        )
        return build_operations_view(
            inventory,
            observations,
            deployment_receipts,
            domain_observations,
            generated_at=UtcTimestamp.now(),
        )


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """One rendered dashboard build held in memory by the server."""

    html: str
    operations_json: str
    built_at: float


@dataclass(slots=True)
class SnapshotCache:
    """Rebuild dashboard artifacts from evidence at most once per interval."""

    sources: EvidenceSources
    refresh: RefreshInterval
    clock: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _snapshot: DashboardSnapshot | None = field(default=None, init=False, repr=False)

    def current(self) -> DashboardSnapshot:
        """Return the cached snapshot, rebuilding once it has expired."""
        with self._lock:
            now = self.clock()
            snapshot = self._snapshot
            if snapshot is None or now - snapshot.built_at >= self.refresh.seconds:
                operations = self.sources.operations_view()
                payload = json.dumps(
                    operations.as_dict(), indent=2, sort_keys=True
                )
                snapshot = DashboardSnapshot(
                    html=render_dashboard_html(operations, live=self.refresh),
                    operations_json=f"{payload}\n",
                    built_at=now,
                )
                self._snapshot = snapshot
            return snapshot


class DashboardHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the shared snapshot cache."""

    daemon_threads = True

    def __init__(
        self,
        endpoint: ListenEndpoint,
        cache: SnapshotCache,
        *,
        request_log: Callable[[str], None] | None = None,
    ) -> None:
        """Bind the endpoint and expose the cache to request handlers."""
        self.cache = cache
        self.request_log = request_log
        """Receives each request line; ``None`` logs none."""
        super().__init__((endpoint.host, endpoint.port), _DashboardRequestHandler)


class _DashboardRequestHandler(BaseHTTPRequestHandler):
    """Serve the rendered dashboard page and its machine-readable payload."""

    def do_GET(self) -> None:
        """Route dashboard requests to the current evidence snapshot."""
        if self.path not in {"/", "/index.html", "/operations.json"}:
            body = json.dumps({"error": "not found", "path": self.path}, sort_keys=True)
            self._respond(HTTPStatus.NOT_FOUND, "application/json", f"{body}\n")
            return
        try:
            snapshot = self._cache().current()
        except ConfigValidationError as error:
            body = json.dumps(error.as_dict(), sort_keys=True)
            self._respond(
                HTTPStatus.INTERNAL_SERVER_ERROR, "application/json", f"{body}\n"
            )
            return
        if self.path == "/operations.json":
            self._respond(HTTPStatus.OK, "application/json", snapshot.operations_json)
            return
        self._respond(HTTPStatus.OK, "text/html; charset=utf-8", snapshot.html)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base signature
        """Hand the request line to the server's request log, if it has one."""
        server = self.server
        if isinstance(server, DashboardHTTPServer) and server.request_log is not None:
            server.request_log(format % args)

    def _cache(self) -> SnapshotCache:
        server = self.server
        if not isinstance(server, DashboardHTTPServer):
            message = f"handler bound to unexpected server: {type(server).__name__}"
            raise TypeError(message)
        return server.cache

    def _respond(self, status: HTTPStatus, content_type: str, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def create_dashboard_server(
    sources: EvidenceSources,
    endpoint: ListenEndpoint,
    refresh: RefreshInterval,
    clock: Callable[[], float] = time.monotonic,
    *,
    request_log: Callable[[str], None] | None = None,
) -> DashboardHTTPServer:
    """Build one snapshot eagerly (fail fast), then bind the HTTP server."""
    cache = warm_snapshot_cache(sources, refresh, clock)
    return DashboardHTTPServer(endpoint, cache, request_log=request_log)


def warm_snapshot_cache(
    sources: EvidenceSources,
    refresh: RefreshInterval,
    clock: Callable[[], float] = time.monotonic,
) -> SnapshotCache:
    """Return a cache holding its first snapshot, so bad evidence fails first."""
    cache = SnapshotCache(sources=sources, refresh=refresh, clock=clock)
    cache.current()
    return cache
