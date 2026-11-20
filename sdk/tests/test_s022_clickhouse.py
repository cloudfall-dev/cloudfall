"""Declaring and validating a ClickHouse service (S-022, #566)."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from clickhouse_project import (
    EVENTS_PASSWORD_REF,
    REPLICAS,
    SCHEMAS,
    VOTERS,
    clickhouse_service,
    edit_baseline,
    edit_server,
    write_project,
)
from cloudfall.cli import main
from cloudfall.clickhouse import (
    SUPPORTED_SERIES,
    ClickhouseSeries,
    ClickhouseVersion,
    supported_series,
)
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import clickhouse_warnings, validate_config

if TYPE_CHECKING:
    from pathlib import Path

ONE_SERVER = ("h1",)


def _validate(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    exit_code = main(
        ["config", "validate", "--project", str(project), "--schemas", str(SCHEMAS)]
    )
    document: dict[str, Any] = json.loads(capsys.readouterr().out)
    return exit_code, document


def _refused_with(
    project: Path, capsys: pytest.CaptureFixture[str], code: str
) -> dict[str, Any]:
    exit_code, document = _validate(project, capsys)
    assert exit_code == 80, document
    assert document["error"]["code"] == "CONFIG_INVALID"
    context: dict[str, Any] = document["error"]["context"]
    assert context["code"] == code, context
    return context


def _one_replica(
    *,
    server: str = "h1",
    port: int = 9000,
    environment: str = "production",
    **clickhouse: object,
) -> dict[str, object]:
    """Return a service of one replica, without clientAccess unless given.

    A field given as ``None`` is left out.
    """
    service = clickhouse_service(
        server=server,
        port=port,
        environment=environment,
        replicas=None,
        keeper=None,
        clientAccess=None,
    )
    spec = service["spec"]
    assert isinstance(spec, dict)
    block = spec["clickhouse"]
    assert isinstance(block, dict)
    for key, value in clickhouse.items():
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    return service


def _codes(document: dict[str, Any]) -> list[str]:
    return [warning["code"] for warning in document["warnings"]]


# S-022-1


def test_s022_1_the_declaration_on_three_servers_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", clickhouse_service())

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document
    assert document["data"]["by_kind"]["Service"] == 1
    assert document["data"]["by_kind"]["Server"] == 3
    assert "CLICKHOUSE_SINGLE_REPLICA" not in _codes(document)


def test_s022_1_inventory_carries_replicas_voters_and_database_never_a_password(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", clickhouse_service())

    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (service,) = inventory.services
    rendered = service.as_dict()
    assert rendered["serviceKind"] == "clickhouse"
    assert rendered["clickhouse"] == {
        "version": "26.8.37.168",
        "maxServerMemoryMb": 16384,
        "replicas": list(REPLICAS),
        "keeper": {"servers": list(VOTERS)},
        "databases": [
            {"name": "events", "owner": "events", "passwordRef": EVENTS_PASSWORD_REF}
        ],
        "clientAccess": {"allowedSources": ["10.0.0.0/24"]},
    }


def test_s022_1_absent_replicas_and_voters_resolve_to_the_server(
    tmp_path: Path,
) -> None:
    project = write_project(
        tmp_path / "project", _one_replica(databases=None), servers=ONE_SERVER
    )

    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (service,) = inventory.services
    assert service.as_dict()["clickhouse"] == {
        "version": "26.8.37.168",
        "maxServerMemoryMb": 16384,
        "replicas": ["h1"],
        "keeper": {"servers": ["h1"]},
        "databases": [],
    }


# S-022-19: each refusal under Validation


@pytest.mark.parametrize(
    "kind", ["postgresql", "redis", "elasticsearch", "mariadb", "rabbitmq"]
)
def test_s022_19_clickhouse_block_on_another_kind_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    service = clickhouse_service()
    spec = service["spec"]
    assert isinstance(spec, dict)
    spec["serviceKind"] = kind
    spec[kind] = {
        "postgresql": {
            "majorVersion": "17",
            "databases": [
                {"name": "crm", "owner": "crm", "passwordRef": EVENTS_PASSWORD_REF}
            ],
        },
        "redis": {"maxmemoryMb": 256, "appendOnly": True},
        "elasticsearch": {"version": "9.2.4", "heapMb": 4096},
        "mariadb": {"version": "11.8.9", "innodbBufferPoolMb": 4096},
        "rabbitmq": {"version": "4.4.1", "memoryHighWatermarkMb": 2048},
    }[kind]
    project = write_project(tmp_path / "project", service)

    _refused_with(project, capsys, "schema_validation_failed")


def test_s022_19_clickhouse_kind_without_its_block_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = clickhouse_service()
    spec = service["spec"]
    assert isinstance(spec, dict)
    del spec["clickhouse"]
    project = write_project(tmp_path / "project", service)

    _refused_with(project, capsys, "schema_validation_failed")


@pytest.mark.parametrize(
    "version", ["25.8.33.6", "26.10.4.207", "26.5.1.1", "24.8.14.39"]
)
def test_s022_19_version_of_an_unsupported_series_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    project = write_project(tmp_path / "project", clickhouse_service(version=version))

    context = _refused_with(project, capsys, "service_clickhouse_version")
    assert context["field"] == "spec.clickhouse.version"
    assert version in context["message"]


@pytest.mark.parametrize(
    "version", ["26.8", "26.8.37", "v26.8.37.168", "26.8.37.168-lts", "026.8.37.168"]
)
def test_s022_19_version_that_is_not_x_y_z_n_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    project = write_project(tmp_path / "project", clickhouse_service(version=version))

    context = _refused_with(project, capsys, "service_clickhouse_version")
    assert context["field"] == "spec.clickhouse.version"


@pytest.mark.parametrize("version", ["26.3.12.1", "26.8.37.168"])
def test_s022_19_supported_series_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    project = write_project(tmp_path / "project", clickhouse_service(version=version))

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


@pytest.mark.parametrize(
    "replicas",
    [["h2", "h3"], ["h1", "h1"], ["h1", "h4"], ["h1", "h2", "h3"], []],
    ids=["without-the-server", "duplicate", "undeclared", "three", "empty"],
)
def test_s022_19_replicas_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], replicas: list[str]
) -> None:
    project = write_project(tmp_path / "project", clickhouse_service(replicas=replicas))

    context = _refused_with(project, capsys, "service_clickhouse_replicas")
    assert context["field"].startswith("spec.clickhouse.replicas")


@pytest.mark.parametrize(
    ("replicas", "voters"),
    [
        (["h1", "h2"], ["h1", "h3", "h2", "h3"]),
        (["h1", "h2"], ["h1", "h2", "h4"]),
        (["h1", "h2"], ["h1", "h3", "h3"]),
        (["h1", "h2"], ["h1", "h3"]),
        (["h1", "h2"], ["h1", "h2"]),
        (["h1", "h2"], None),
        (["h1"], ["h1", "h2", "h3"]),
        (["h1"], ["h2"]),
    ],
    ids=[
        "duplicate",
        "undeclared",
        "misses-a-replica",
        "two-for-two-replicas-without-one",
        "two-for-two-replicas",
        "absent-for-two-replicas",
        "three-for-one-replica",
        "misses-the-only-replica",
    ],
)
def test_s022_19_keeper_servers_are_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    replicas: list[str],
    voters: list[str] | None,
) -> None:
    keeper = None if voters is None else {"servers": voters}
    project = write_project(
        tmp_path / "project", clickhouse_service(replicas=replicas, keeper=keeper)
    )

    context = _refused_with(project, capsys, "service_clickhouse_keeper_servers")
    assert context["field"].startswith("spec.clickhouse.keeper.servers")


@pytest.mark.parametrize(
    "other",
    [
        _one_replica(server="h2", port=9001, databases=None),
        _one_replica(server="h3", port=9001, databases=None),
    ],
    ids=["replica", "voter"],
)
def test_s022_19_server_in_two_clickhouse_services_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], other: dict[str, object]
) -> None:
    other["metadata"] = {"id": "clickhouse-other", "description": "Another"}
    project = write_project(tmp_path / "project", clickhouse_service(), other)

    _refused_with(project, capsys, "service_clickhouse_server_shared")


@pytest.mark.parametrize(
    ("server", "field"),
    [("h2", "spec.clickhouse.replicas.1"), ("h3", "spec.clickhouse.keeper.servers.2")],
)
def test_s022_19_server_in_another_environment_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], server: str, field: str
) -> None:
    project = write_project(tmp_path / "project", clickhouse_service())
    edit_server(project, server, environment="staging")

    context = _refused_with(project, capsys, "service_clickhouse_environment")
    assert context["field"] == field


def test_s022_19_remote_access_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    remote_access = {
        "listenAddress": "203.0.113.11",
        "allowedSources": ["10.0.0.0/24"],
    }
    project = write_project(
        tmp_path / "project", clickhouse_service(remoteAccess=remote_access)
    )

    context = _refused_with(project, capsys, "service_clickhouse_remote_access")
    assert context["field"] == "spec.clickhouse.remoteAccess"


@pytest.mark.parametrize(
    "database",
    [
        {"name": "events", "passwordRef": EVENTS_PASSWORD_REF},
        {"name": "events", "owner": "events"},
        {"name": "events", "application": "crm"},
        {
            "name": "events",
            "owner": "events",
            "passwordRef": EVENTS_PASSWORD_REF,
            "application": "crm",
        },
    ],
    ids=["without-owner", "without-password-ref", "application", "both"],
)
def test_s022_19_database_without_owner_or_password_or_with_application_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], database: dict[str, object]
) -> None:
    project = write_project(
        tmp_path / "project", clickhouse_service(databases=[database])
    )

    _refused_with(project, capsys, "service_clickhouse_database")


def test_one_owner_of_two_databases_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    databases = [
        {"name": "events", "owner": "events", "passwordRef": EVENTS_PASSWORD_REF},
        {"name": "reports", "owner": "events", "passwordRef": EVENTS_PASSWORD_REF},
    ]
    project = write_project(
        tmp_path / "project", clickhouse_service(databases=databases)
    )

    context = _refused_with(project, capsys, "service_clickhouse_database")
    assert context["field"] == "spec.clickhouse.databases.1.owner"


def test_database_declared_twice_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    databases = [
        {"name": "events", "owner": "events", "passwordRef": EVENTS_PASSWORD_REF},
        {"name": "events", "owner": "reports", "passwordRef": EVENTS_PASSWORD_REF},
    ]
    project = write_project(
        tmp_path / "project", clickhouse_service(databases=databases)
    )

    context = _refused_with(project, capsys, "service_database_duplicate")
    assert context["field"] == "spec.clickhouse.databases.1.name"


@pytest.mark.parametrize(
    "name",
    [
        "default",
        "cloudfall",
        "system",
        "information_schema",
        "INFORMATION_SCHEMA",
        "cloudfall_check",
    ],
)
@pytest.mark.parametrize("field", ["name", "owner"])
def test_s022_19_reserved_names_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str, field: str
) -> None:
    database: dict[str, object] = {
        "name": "events",
        "owner": "events",
        "passwordRef": EVENTS_PASSWORD_REF,
        field: name,
    }
    project = write_project(
        tmp_path / "project", clickhouse_service(databases=[database])
    )

    context = _refused_with(project, capsys, "service_database_owner_reserved")
    assert context["field"] == f"spec.clickhouse.databases.0.{field}"


@pytest.mark.parametrize(
    ("service", "servers"),
    [
        (clickhouse_service(clientAccess=None), VOTERS),
        (_one_replica(clientAccess={"allowedSources": ["10.0.0.0/24"]}), ONE_SERVER),
    ],
    ids=["two-replicas", "client-access"],
)
def test_s022_19_reachable_server_without_a_firewall_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    service: dict[str, object],
    servers: tuple[str, ...],
) -> None:
    project = write_project(tmp_path / "project", service, servers=servers)
    edit_baseline(project, firewall=None)

    _refused_with(project, capsys, "service_clickhouse_unfirewalled")


def test_one_replica_without_client_access_needs_no_firewall(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", _one_replica(), servers=ONE_SERVER)
    edit_baseline(project, firewall=None)

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


@pytest.mark.parametrize(
    ("server", "field"),
    [("h2", "spec.clickhouse.replicas.1"), ("h3", "spec.clickhouse.keeper.servers.2")],
)
def test_s022_19_reachable_server_without_an_ipv4_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], server: str, field: str
) -> None:
    project = write_project(tmp_path / "project", clickhouse_service())
    edit_server(project, server, network=None)

    context = _refused_with(project, capsys, "service_clickhouse_server_unaddressed")
    assert context["field"] == field


@pytest.mark.parametrize("source", ["0.0.0.0/0", "::/0"])
def test_s022_19_client_access_from_every_address_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    project = write_project(
        tmp_path / "project",
        clickhouse_service(clientAccess={"allowedSources": [source]}),
    )

    context = _refused_with(project, capsys, "service_remote_access_open")
    assert context["field"] == "spec.clickhouse.clientAccess.allowedSources.0"


def test_client_access_source_that_is_not_canonical_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        clickhouse_service(clientAccess={"allowedSources": ["10.0.0.1/24"]}),
    )

    _refused_with(project, capsys, "service_remote_access_source_invalid")


_POOL = {
    "id": "data",
    "volumeGroup": "vg_data",
    "primary": "/dev/nvme0n1p5",
    "secondary": "/dev/nvme1n1p5",
}


@pytest.mark.parametrize(
    ("pools", "code"),
    [
        ([], "volume_requirement_pool_missing"),
        (
            [_POOL, {**_POOL, "id": "fast", "volumeGroup": "vg_fast"}],
            "volume_requirement_pool_ambiguous",
        ),
    ],
)
def test_s022_19_server_without_one_storage_pool_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    pools: list[dict[str, str]],
    code: str,
) -> None:
    project = write_project(tmp_path / "project", clickhouse_service())
    _set_pools(project, pools)

    _refused_with(project, capsys, code)


def test_s022_19_one_replica_on_a_pool_without_a_secondary_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", _one_replica(), servers=ONE_SERVER)
    _set_pools(project, [{k: v for k, v in _POOL.items() if k != "secondary"}])

    _refused_with(project, capsys, "volume_requirement_pool_unmirrored")


def test_two_replicas_on_pools_without_a_secondary_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With two replicas the copy is on the other server: expendable."""
    project = write_project(tmp_path / "project", clickhouse_service())
    _set_pools(project, [{k: v for k, v in _POOL.items() if k != "secondary"}])

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


def _set_pools(project: Path, pools: list[dict[str, str]]) -> None:
    path = project / "baselines" / "debian-application.yaml"
    baseline = yaml.safe_load(path.read_text(encoding="utf-8"))
    baseline["spec"]["storage"]["pools"] = pools
    path.write_text(yaml.safe_dump(baseline, sort_keys=False), encoding="utf-8")


def test_memory_below_2048_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project", clickhouse_service(maxServerMemoryMb=2047)
    )

    _refused_with(project, capsys, "schema_validation_failed")


def test_replica_port_taken_by_another_service_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    redis: dict[str, object] = {
        "apiVersion": "cloudfall/v1",
        "kind": "Service",
        "metadata": {"id": "redis-cache", "description": "Cache"},
        "spec": {
            "serviceKind": "redis",
            "environment": "production",
            "server": "h2",
            "bind": {"address": "127.0.0.1", "port": 9000},
            "redis": {"maxmemoryMb": 256, "appendOnly": True},
            "backup": {
                "directory": "/var/backups/cloudfall/redis-cache",
                "onCalendar": "*-*-* 03:00:00 UTC",
                "retentionDays": 7,
            },
        },
    }
    project = write_project(tmp_path / "project", clickhouse_service(), redis)

    _refused_with(project, capsys, "service_port_conflict")


# S-022-19: the warnings


def test_s022_19_one_replica_in_production_warns_single_replica(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", _one_replica(), servers=ONE_SERVER)

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document
    (warning,) = [
        warning
        for warning in document["warnings"]
        if warning["code"] == "CLICKHOUSE_SINGLE_REPLICA"
    ]
    assert warning["context"]["service"] == "clickhouse-main"


def test_one_replica_outside_production_does_not_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project", _one_replica(environment="staging"), servers=ONE_SERVER
    )
    edit_server(project, "h1", environment="staging")

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document
    assert "CLICKHOUSE_SINGLE_REPLICA" not in _codes(document)


def test_s022_19_series_within_90_days_of_its_end_warns_naming_the_date(
    tmp_path: Path,
) -> None:
    project = write_project(
        tmp_path / "project", clickhouse_service(version="26.3.12.1")
    )
    state = validate_config(project, SCHEMAS)
    series = supported_series(ClickhouseVersion("26.3.12.1"))
    assert series is not None
    end = series.end_of_support

    long_before = clickhouse_warnings(state, end - dt.timedelta(91))
    window_opens = clickhouse_warnings(state, end - dt.timedelta(90))
    after = clickhouse_warnings(state, end + dt.timedelta(1))

    assert long_before == ()
    (warning,) = window_opens
    assert warning.code == "CLICKHOUSE_VERSION_ENDING"
    assert warning.context == {
        "service": "clickhouse-main",
        "version": "26.3.12.1",
        "series": "26.3",
        "endOfLife": "2027-03-27",
    }
    assert "2027-03-27" in warning.message
    assert "ends support on" in warning.message
    (ended,) = after
    assert "ended support on" in ended.message


def test_s022_19_warning_reads_the_injected_table_not_the_clock(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", clickhouse_service())
    state = validate_config(project, SCHEMAS)
    table = (ClickhouseSeries(year=26, release=8, end_of_support=dt.date(2030, 3, 1)),)

    assert clickhouse_warnings(state, dt.date(2029, 1, 1), table) == ()
    (warning,) = clickhouse_warnings(state, dt.date(2030, 1, 1), table)
    assert warning.context["endOfLife"] == "2030-03-01"
    assert "2030-03-01" in warning.message


def test_supported_series_are_26_3_and_26_8_with_their_dates() -> None:
    assert [(series.label, series.end_of_support) for series in SUPPORTED_SERIES] == [
        ("26.3", dt.date(2027, 3, 27)),
        ("26.8", dt.date(2027, 8, 30)),
    ]
    assert supported_series(ClickhouseVersion("25.8.33.6")) is None
    assert supported_series(ClickhouseVersion("26.10.4.207")) is None
