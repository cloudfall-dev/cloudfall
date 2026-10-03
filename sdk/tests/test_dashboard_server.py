"""Live dashboard server tests."""

from __future__ import annotations

import http.client
import json
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cloudfall.dashboard import (
    RefreshInterval,
    operations_fingerprint,
    render_dashboard_html,
)
from cloudfall.dashboard_server import (
    DashboardHTTPServer,
    EvidenceSources,
    ListenEndpoint,
    SnapshotCache,
    create_dashboard_server,
)
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.operations import FleetOperations, UtcTimestamp, build_operations_view
from cloudfall.service_evidence import DeploymentReceiptSet, DomainObservationSet
from cloudfall.validation import ConfigValidationError, validate_config

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"


class ManualClock:
    """Deterministic monotonic clock injected instead of time.monotonic."""

    def __init__(self) -> None:
        """Start at zero."""
        self.value = 0.0

    def __call__(self) -> float:
        """Return the manually advanced time."""
        return self.value


def _view(generated_at: UtcTimestamp) -> FleetOperations:
    inventory = PlatformInventory.from_state(validate_config(EXAMPLES, SCHEMAS))
    observations = load_observations(COMPLIANT, SCHEMAS)
    return build_operations_view(
        inventory,
        observations,
        DeploymentReceiptSet.empty(),
        DomainObservationSet.empty(),
        generated_at=generated_at,
    )


def _sources(tmp_path: Path) -> EvidenceSources:
    observed = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observed)
    return EvidenceSources(
        project_directory=EXAMPLES,
        schema_directory=SCHEMAS,
        observed_directory=observed,
        service_observed_directory=tmp_path / "observed-services",
        deployments_directory=tmp_path / "deployments",
    )


def test_fingerprint_ignores_generation_time() -> None:
    first = _view(UtcTimestamp(datetime(2026, 7, 15, 11, tzinfo=UTC)))
    second = _view(UtcTimestamp(datetime(2026, 7, 16, 9, tzinfo=UTC)))

    assert first.generated_at != second.generated_at
    assert operations_fingerprint(first) == operations_fingerprint(second)


def test_live_html_embeds_refresh_script_and_fingerprint() -> None:
    view = _view(UtcTimestamp(datetime(2026, 7, 15, 11, tzinfo=UTC)))

    live = render_dashboard_html(view, live=RefreshInterval(7))
    static = render_dashboard_html(view)

    assert f'data-fingerprint="{operations_fingerprint(view)}"' in live
    assert "const REFRESH_MS = 7000;" in live
    assert 'id="live-status"' in live
    assert "REFRESH_MS" not in static
    assert f'data-fingerprint="{operations_fingerprint(view)}"' in static


def test_refresh_interval_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError, match="refresh interval"):
        RefreshInterval(0)
    with pytest.raises(TypeError, match="refresh interval"):
        RefreshInterval.from_boundary("10")


def test_snapshot_cache_rebuilds_only_after_interval(tmp_path: Path) -> None:
    clock = ManualClock()
    cache = SnapshotCache(
        sources=_sources(tmp_path), refresh=RefreshInterval(10), clock=clock
    )

    first = cache.current()
    clock.value = 5.0
    assert cache.current() is first
    clock.value = 15.0
    assert cache.current() is not first


def _serve(server: DashboardHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _get(port: int, path: str) -> tuple[int, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        connection.close()


def test_server_serves_live_html_json_and_404(tmp_path: Path) -> None:
    server = create_dashboard_server(
        _sources(tmp_path),
        ListenEndpoint(host="127.0.0.1", port=0),
        RefreshInterval(60),
    )
    thread = _serve(server)
    port = int(server.server_address[1])
    try:
        html_status, html = _get(port, "/")
        json_status, payload_text = _get(port, "/operations.json")
        missing_status, missing = _get(port, "/missing")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    payload = json.loads(payload_text)
    assert html_status == 200
    assert "Cloudfall Operations" in html
    assert "REFRESH_MS = 60000" in html
    assert json_status == 200
    assert payload["health"] in {"healthy", "warning", "critical", "unknown"}
    assert missing_status == 404
    assert json.loads(missing)["path"] == "/missing"


def test_server_surfaces_evidence_errors_as_500(tmp_path: Path) -> None:
    clock = ManualClock()
    sources = _sources(tmp_path)
    server = create_dashboard_server(
        sources,
        ListenEndpoint(host="127.0.0.1", port=0),
        RefreshInterval(1),
        clock=clock,
    )
    thread = _serve(server)
    port = int(server.server_address[1])
    try:
        shutil.rmtree(sources.observed_directory)
        clock.value = 2.0
        status, body = _get(port, "/")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 500
    error = json.loads(body)
    assert error["status"] == "error"
    assert error["error"]["code"] == "observation_directory_missing"


def test_eager_first_snapshot_fails_fast(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    shutil.rmtree(sources.observed_directory)

    with pytest.raises(ConfigValidationError):
        create_dashboard_server(
            sources,
            ListenEndpoint(host="127.0.0.1", port=0),
            RefreshInterval(10),
        )


def test_request_lines_go_to_the_request_log(tmp_path: Path) -> None:
    lines: list[str] = []
    server = create_dashboard_server(
        _sources(tmp_path),
        ListenEndpoint(host="127.0.0.1", port=0),
        RefreshInterval(60),
        request_log=lines.append,
    )
    thread = _serve(server)
    try:
        status, _ = _get(int(server.server_address[1]), "/missing")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 404
    assert any("GET /missing" in line for line in lines)
