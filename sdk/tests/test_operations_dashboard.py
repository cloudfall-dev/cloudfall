"""Operations projection and static dashboard tests."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from cloudfall.dashboard import build_dashboard
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.operations import (
    FleetOperations,
    OperationsHealth,
    TaskKind,
    TaskSeverity,
    UtcTimestamp,
    build_operations_view,
)
from cloudfall.service_evidence import DeploymentReceiptSet, DomainObservationSet
from cloudfall.validation import validate_config
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"
GENERATED_AT = UtcTimestamp(datetime(2026, 7, 15, 11, tzinfo=UTC))


def _view(
    observation_directory: Path, project_directory: Path = EXAMPLES
) -> FleetOperations:
    inventory = PlatformInventory.from_state(
        validate_config(project_directory, SCHEMAS)
    )
    observations = load_observations(observation_directory, SCHEMAS)
    return build_operations_view(
        inventory,
        observations,
        DeploymentReceiptSet.empty(),
        DomainObservationSet.empty(),
        generated_at=GENERATED_AT,
    )


def _domainless_examples(tmp_path: Path) -> Path:
    project_directory = tmp_path / "config"
    copy_examples(project_directory)
    shutil.rmtree(project_directory / "domains")
    return project_directory


def test_compliant_evidence_produces_healthy_operations_view(
    tmp_path: Path,
) -> None:
    view = _view(COMPLIANT, _domainless_examples(tmp_path))

    assert view.health is OperationsHealth.HEALTHY
    assert view.tasks == ()
    assert all(server.health is OperationsHealth.HEALTHY for server in view.servers)


def test_declared_domain_without_evidence_degrades_to_warning() -> None:
    view = _view(COMPLIANT)

    assert view.health is OperationsHealth.WARNING
    assert all(server.health is OperationsHealth.HEALTHY for server in view.servers)
    domain = next(item for item in view.domains if item.domain_id.value == "crm-site")
    assert "no public observation" in domain.public_route.detail


def test_virtual_disks_do_not_require_nvme_smartctl(
    tmp_path: Path,
) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    for path in observations.glob("*.json"):
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        snapshot["spec"]["storage"]["smart"] = {
            "available": False,
            "smartctlVersion": None,
            "devices": [],
        }
        path.write_text(json.dumps(snapshot), encoding="utf-8")

    view = _view(observations, _domainless_examples(tmp_path))

    assert view.health is OperationsHealth.HEALTHY
    assert all(task.kind is not TaskKind.SMART for task in view.tasks)


def test_operations_view_prioritizes_disk_service_and_stale_evidence(
    tmp_path: Path,
) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1_path = observations / "h1.json"
    h1 = json.loads(h1_path.read_text(encoding="utf-8"))
    h1["spec"]["observedAt"] = "2026-07-01T10:00:00Z"
    filesystem = h1["spec"]["storage"]["filesystems"][0]
    filesystem["used"] = 99_000
    filesystem["avail"] = 1_000
    h1["spec"]["services"]["failed-example.service"] = {
        "name": "failed-example.service",
        "state": "failed",
        "status": "enabled",
        "source": "systemd",
    }
    h1["spec"]["storage"]["smart"]["devices"] = [
        {
            "path": "/dev/nvme0n1",
            "model": "Example NVMe",
            "serial": "EXAMPLE001",
            "firmware": "1.0",
            "smartctlExitCode": 8,
            "overallHealth": "failed",
            "criticalWarning": "0x04",
            "reliabilityDegraded": True,
            "temperatureCelsius": 44,
            "availableSparePercent": 94,
            "availableSpareThresholdPercent": 10,
            "percentageUsed": 255,
            "dataUnitsRead": 426_741_528,
            "dataUnitsWritten": 1_470_402_058,
            "powerCycles": 34,
            "powerOnHours": 68_065,
            "unsafeShutdowns": 21,
            "mediaAndDataIntegrityErrors": 0,
            "errorInformationLogEntries": 0,
        }
    ]
    h1_path.write_text(json.dumps(h1), encoding="utf-8")

    view = _view(observations)
    h1_view = next(server for server in view.servers if server.server_id.value == "h1")

    assert view.health is OperationsHealth.CRITICAL
    assert h1_view.health is OperationsHealth.CRITICAL
    assert {task.kind for task in h1_view.tasks} == {
        TaskKind.FILESYSTEM,
        TaskKind.OBSERVATION,
        TaskKind.SERVICE,
        TaskKind.SMART,
    }
    assert h1_view.tasks[0].severity is TaskSeverity.CRITICAL
    assert h1_view.smart_devices[0].percentage_used == 255
    assert len({task.task_id for task in h1_view.tasks}) == len(h1_view.tasks)


def test_dashboard_build_writes_html_and_machine_readable_json(
    tmp_path: Path,
) -> None:
    view = _view(COMPLIANT, _domainless_examples(tmp_path))

    artifacts = build_dashboard(view, tmp_path / "dashboard")

    html = artifacts.index.read_text(encoding="utf-8")
    payload = json.loads(artifacts.operations.read_text(encoding="utf-8"))
    assert "Cloudfall Operations" in html
    assert "No open tasks from current evidence" in html
    assert payload["health"] == "healthy"
    assert payload["summary"]["tasks"]["open"] == 0
