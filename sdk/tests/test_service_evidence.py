"""Domain route evidence collection tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from cloudfall.domain import Hostname, IpAddress, TcpPort
from cloudfall.inventory import PlatformInventory
from cloudfall.service_evidence import (
    EndpointEvidence,
    EvidenceTimestamp,
    NetworkResponse,
    ProbeTarget,
    TlsEvidence,
    inspect_domains,
    load_domain_observations,
)
from cloudfall.validation import validate_config
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
OBSERVED_AT = EvidenceTimestamp(datetime(2026, 9, 8, 12, tzinfo=UTC))


class _RecordingClient:
    """Resolves everything locally and records every endpoint probe."""

    def __init__(self) -> None:
        self.probed: list[ProbeTarget] = []

    def resolve(
        self,
        hostname: Hostname,  # noqa: ARG002 - protocol shape.
        port: TcpPort,  # noqa: ARG002 - protocol shape.
    ) -> tuple[IpAddress, ...]:
        return (IpAddress("192.0.2.10"),)

    def request(self, target: ProbeTarget) -> NetworkResponse:
        self.probed.append(target)
        return NetworkResponse(
            endpoint=EndpointEvidence(reachable=True, status=None, error=None),
            tls=TlsEvidence(
                available=False, valid=False, expires_at=None, error=None
            ),
            headers={},
        )


def _single_host_inventory(tmp_path: Path) -> PlatformInventory:
    project_directory = tmp_path / "config"
    copy_examples(project_directory)
    domain_path = project_directory / "domains" / "crm-site.yaml"
    single_host = domain_path.read_text(encoding="utf-8").replace(
        "    server: h1", "    server: h2"
    )
    domain_path.write_text(single_host, encoding="utf-8")
    return PlatformInventory.from_state(validate_config(project_directory, SCHEMAS))


def test_single_host_route_skips_the_origin_probe(tmp_path: Path) -> None:
    """When origin and proxy share a host there is no origin hop to probe.

    The 2026-09-08 M2/M3 proving run showed the origin probe timing out
    against the firewall on single-host routes, wrongly degrading the route
    to a warning and blocking the migrate orchestrator's evidence pass.
    """
    inventory = _single_host_inventory(tmp_path)
    client = _RecordingClient()

    paths = inspect_domains(
        inventory,
        tmp_path / "observed-domains",
        client,
        observed_at=OBSERVED_AT,
    )

    snapshot = json.loads(paths[0].read_text(encoding="utf-8"))
    origin = snapshot["spec"]["origin"]
    assert origin["skipped"] is True
    assert "public probe" in origin["skipReason"]
    assert len(client.probed) == 1  # public only, no origin probe

    observations = load_domain_observations(
        tmp_path / "observed-domains", SCHEMAS
    )
    evidence = observations.snapshots[0].origin
    assert evidence.skipped is True
    assert evidence.skip_reason is not None
