"""Declaring and validating a RabbitMQ service (S-023, #505)."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from cloudfall.cli import main
from cloudfall.inventory import PlatformInventory
from cloudfall.rabbitmq import (
    SUPPORTED_SERIES,
    ErlangRange,
    ErlangRelease,
    RabbitmqSeries,
    RabbitmqVersion,
    supported_series,
)
from cloudfall.validation import rabbitmq_warnings, validate_config
from rabbitmq_project import (
    CLUSTER,
    JOBS_PASSWORD_REF,
    SCHEMAS,
    edit_baseline,
    edit_server,
    rabbitmq_service,
    write_project,
)

if TYPE_CHECKING:
    from pathlib import Path

ONE_NODE = ("h1",)


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


def _one_node(
    *,
    server: str = "h1",
    port: int = 5672,
    environment: str = "production",
    **rabbitmq: object,
) -> dict[str, object]:
    """Return a service of one node, without clientAccess unless given.

    A field given as ``None`` is left out.
    """
    service = rabbitmq_service(
        server=server,
        port=port,
        environment=environment,
        nodes=None,
        clientAccess=None,
    )
    spec = service["spec"]
    assert isinstance(spec, dict)
    block = spec["rabbitmq"]
    assert isinstance(block, dict)
    for key, value in rabbitmq.items():
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    return service


def _codes(document: dict[str, Any]) -> list[str]:
    return [warning["code"] for warning in document["warnings"]]


# S-023-1


def test_s023_1_the_declaration_on_three_servers_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service())

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document
    assert document["data"]["by_kind"]["Service"] == 1
    assert document["data"]["by_kind"]["Server"] == 3
    assert "RABBITMQ_SINGLE_NODE" not in _codes(document)


def test_s023_1_inventory_carries_nodes_and_vhost_never_a_password(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service())

    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (service,) = inventory.services
    rendered = service.as_dict()
    assert rendered["serviceKind"] == "rabbitmq"
    assert rendered["rabbitmq"] == {
        "version": "4.4.1",
        "memoryHighWatermarkMb": 2048,
        "nodes": list(CLUSTER),
        "vhosts": [{"name": "jobs", "owner": "jobs", "passwordRef": JOBS_PASSWORD_REF}],
        "clientAccess": {"allowedSources": ["10.0.0.0/24"]},
    }


def test_s023_1_absent_nodes_resolve_to_the_server(tmp_path: Path) -> None:
    project = write_project(
        tmp_path / "project", _one_node(vhosts=None), servers=ONE_NODE
    )

    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (service,) = inventory.services
    assert service.as_dict()["rabbitmq"] == {
        "version": "4.4.1",
        "memoryHighWatermarkMb": 2048,
        "nodes": ["h1"],
        "vhosts": [],
    }


# S-023-18: each refusal under Validation


@pytest.mark.parametrize("kind", ["postgresql", "redis", "elasticsearch", "mariadb"])
def test_s023_18_rabbitmq_block_on_another_kind_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    service = rabbitmq_service()
    spec = service["spec"]
    assert isinstance(spec, dict)
    spec["serviceKind"] = kind
    spec[kind] = {
        "postgresql": {
            "majorVersion": "17",
            "databases": [
                {"name": "crm", "owner": "crm", "passwordRef": JOBS_PASSWORD_REF}
            ],
        },
        "redis": {"maxmemoryMb": 256, "appendOnly": True},
        "elasticsearch": {"version": "9.2.4", "heapMb": 4096},
        "mariadb": {"version": "11.8.9", "innodbBufferPoolMb": 4096},
    }[kind]
    project = write_project(tmp_path / "project", service)

    _refused_with(project, capsys, "schema_validation_failed")


def test_s023_18_rabbitmq_kind_without_its_block_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = rabbitmq_service()
    spec = service["spec"]
    assert isinstance(spec, dict)
    del spec["rabbitmq"]
    project = write_project(tmp_path / "project", service)

    _refused_with(project, capsys, "schema_validation_failed")


@pytest.mark.parametrize("version", ["4.4", "4.4.1-1", "v4.4.1", "04.4.1"])
def test_s023_18_version_that_is_not_x_y_z_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service(version=version))

    context = _refused_with(project, capsys, "service_rabbitmq_version")
    assert context["field"] == "spec.rabbitmq.version"


@pytest.mark.parametrize("version", ["4.2.9", "4.5.0", "3.13.7"])
def test_s023_18_version_of_an_unsupported_series_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service(version=version))

    context = _refused_with(project, capsys, "service_rabbitmq_version")
    assert version in context["message"]


@pytest.mark.parametrize("version", ["4.3.0", "4.4.1"])
def test_s023_18_supported_series_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service(version=version))

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


@pytest.mark.parametrize(
    "nodes",
    [
        ["h1", "h2"],
        ["h2", "h3", "h1", "h2"],
        ["h2", "h3", "h4"],
        ["h2"],
    ],
    ids=["two", "duplicate", "undeclared", "without-the-server"],
)
def test_s023_18_nodes_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], nodes: list[str]
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service(nodes=nodes))

    context = _refused_with(project, capsys, "service_rabbitmq_nodes")
    assert context["field"].startswith("spec.rabbitmq.nodes")


def test_s023_18_server_in_two_rabbitmq_services_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        rabbitmq_service(),
        _one_node(server="h2", port=5673, vhosts=None)
        | {"metadata": {"id": "rabbitmq-other", "description": "Another"}},
    )

    _refused_with(project, capsys, "service_rabbitmq_server_shared")


def test_s023_18_node_in_another_environment_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service())
    edit_server(project, "h3", environment="staging")

    context = _refused_with(project, capsys, "service_rabbitmq_environment")
    assert context["field"] == "spec.rabbitmq.nodes.2"


def test_s023_18_two_nodes_with_one_hostname_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service())
    edit_server(project, "h3", hostname="H2.example.internal.")

    context = _refused_with(project, capsys, "service_rabbitmq_hostname")
    assert "h2" in context["message"]


def test_s023_18_remote_access_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    remote_access = {
        "listenAddress": "203.0.113.11",
        "allowedSources": ["10.0.0.0/24"],
    }
    project = write_project(
        tmp_path / "project", rabbitmq_service(remoteAccess=remote_access)
    )

    context = _refused_with(project, capsys, "service_rabbitmq_remote_access")
    assert context["field"] == "spec.rabbitmq.remoteAccess"


@pytest.mark.parametrize(
    "vhost",
    [
        {"name": "jobs", "passwordRef": JOBS_PASSWORD_REF},
        {"name": "jobs", "owner": "jobs"},
        {"name": "jobs", "application": "crm"},
        {
            "name": "jobs",
            "owner": "jobs",
            "passwordRef": JOBS_PASSWORD_REF,
            "application": "crm",
        },
    ],
    ids=["without-owner", "without-password-ref", "application", "both"],
)
def test_s023_18_vhost_without_owner_or_password_or_with_application_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], vhost: dict[str, object]
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service(vhosts=[vhost]))

    _refused_with(project, capsys, "service_rabbitmq_vhost")


def test_vhost_declared_twice_or_one_owner_of_two_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for position, vhosts in enumerate(
        (
            [
                {"name": "jobs", "owner": "jobs", "passwordRef": JOBS_PASSWORD_REF},
                {"name": "jobs", "owner": "mail", "passwordRef": JOBS_PASSWORD_REF},
            ],
            [
                {"name": "jobs", "owner": "jobs", "passwordRef": JOBS_PASSWORD_REF},
                {"name": "mail", "owner": "jobs", "passwordRef": JOBS_PASSWORD_REF},
            ],
        )
    ):
        project = write_project(
            tmp_path / f"project-{position}", rabbitmq_service(vhosts=vhosts)
        )

        context = _refused_with(project, capsys, "service_rabbitmq_vhost")
        assert context["field"].startswith("spec.rabbitmq.vhosts.1.")


@pytest.mark.parametrize(
    ("name", "owner", "field"),
    [
        ("jobs", "guest", "owner"),
        ("/", "jobs", "name"),
        ("cloudfall_check", "jobs", "name"),
        ("jobs", "cloudfall_check", "owner"),
    ],
)
def test_s023_18_reserved_names_are_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    owner: str,
    field: str,
) -> None:
    vhosts = [{"name": name, "owner": owner, "passwordRef": JOBS_PASSWORD_REF}]
    project = write_project(tmp_path / "project", rabbitmq_service(vhosts=vhosts))

    context = _refused_with(project, capsys, "service_rabbitmq_reserved")
    assert context["field"] == f"spec.rabbitmq.vhosts.0.{field}"


@pytest.mark.parametrize(
    ("service", "servers"),
    [
        (rabbitmq_service(clientAccess=None), CLUSTER),
        (_one_node(clientAccess={"allowedSources": ["10.0.0.0/24"]}), ONE_NODE),
    ],
    ids=["three-nodes", "client-access"],
)
def test_s023_18_reachable_node_without_a_firewall_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    service: dict[str, object],
    servers: tuple[str, ...],
) -> None:
    project = write_project(tmp_path / "project", service, servers=servers)
    edit_baseline(project, firewall=None)

    _refused_with(project, capsys, "service_rabbitmq_unfirewalled")


def test_one_node_without_client_access_needs_no_firewall(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", _one_node(), servers=ONE_NODE)
    edit_baseline(project, firewall=None)

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


def test_s023_18_reachable_node_without_an_ipv4_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service())
    edit_server(project, "h2", network=None)

    context = _refused_with(project, capsys, "service_rabbitmq_server_unaddressed")
    assert context["field"] == "spec.rabbitmq.nodes.1"


@pytest.mark.parametrize("source", ["0.0.0.0/0", "::/0"])
def test_s023_18_client_access_from_every_address_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    project = write_project(
        tmp_path / "project",
        rabbitmq_service(clientAccess={"allowedSources": [source]}),
    )

    context = _refused_with(project, capsys, "service_remote_access_open")
    assert context["field"] == "spec.rabbitmq.clientAccess.allowedSources.0"


def test_client_access_source_that_is_not_canonical_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        rabbitmq_service(clientAccess={"allowedSources": ["10.0.0.1/24"]}),
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
def test_s023_18_server_without_one_storage_pool_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    pools: list[dict[str, str]],
    code: str,
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service())
    _set_pools(project, pools)

    _refused_with(project, capsys, code)


def test_s023_18_one_node_on_a_pool_without_a_secondary_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", _one_node(), servers=ONE_NODE)
    _set_pools(project, [{k: v for k, v in _POOL.items() if k != "secondary"}])

    _refused_with(project, capsys, "volume_requirement_pool_unmirrored")


def test_three_nodes_on_pools_without_a_secondary_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With three nodes the copies are on the other servers: expendable."""
    project = write_project(tmp_path / "project", rabbitmq_service())
    _set_pools(project, [{k: v for k, v in _POOL.items() if k != "secondary"}])

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


def _set_pools(project: Path, pools: list[dict[str, str]]) -> None:
    path = project / "baselines" / "debian-application.yaml"
    baseline = yaml.safe_load(path.read_text(encoding="utf-8"))
    baseline["spec"]["storage"]["pools"] = pools
    path.write_text(yaml.safe_dump(baseline, sort_keys=False), encoding="utf-8")


def test_node_port_taken_by_another_service_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    redis: dict[str, object] = {
        "apiVersion": "cloudfall/v1",
        "kind": "Service",
        "metadata": {"id": "redis-cache", "description": "Cache"},
        "spec": {
            "serviceKind": "redis",
            "environment": "production",
            "server": "h3",
            "bind": {"address": "127.0.0.1", "port": 5672},
            "redis": {"maxmemoryMb": 256, "appendOnly": True},
            "backup": {
                "directory": "/var/backups/cloudfall/redis-cache",
                "onCalendar": "*-*-* 03:00:00 UTC",
                "retentionDays": 7,
            },
        },
    }
    project = write_project(tmp_path / "project", rabbitmq_service(), redis)

    _refused_with(project, capsys, "service_port_conflict")


# S-023-18: the warnings


def test_s023_18_one_node_in_production_warns_single_node(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", _one_node(), servers=ONE_NODE)

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document
    (warning,) = [
        warning
        for warning in document["warnings"]
        if warning["code"] == "RABBITMQ_SINGLE_NODE"
    ]
    assert warning["context"]["service"] == "rabbitmq-main"


def test_one_node_outside_production_does_not_warn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project", _one_node(environment="staging"), servers=ONE_NODE
    )
    for server in ONE_NODE:
        edit_server(project, server, environment="staging")

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document
    assert "RABBITMQ_SINGLE_NODE" not in _codes(document)


def test_s023_18_series_within_90_days_of_its_end_warns_naming_the_date(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service(version="4.3.2"))
    state = validate_config(project, SCHEMAS)
    series = supported_series(RabbitmqVersion("4.3.2"))
    assert series is not None
    end = series.end_of_support

    long_before = rabbitmq_warnings(state, end - dt.timedelta(91))
    window_opens = rabbitmq_warnings(state, end - dt.timedelta(90))
    after = rabbitmq_warnings(state, end + dt.timedelta(1))

    assert long_before == ()
    (warning,) = window_opens
    assert warning.code == "RABBITMQ_VERSION_ENDING"
    assert warning.context == {
        "service": "rabbitmq-main",
        "version": "4.3.2",
        "series": "4.3",
        "endOfLife": "2027-01-31",
    }
    assert "2027-01-31" in warning.message
    assert "ends community support on" in warning.message
    (ended,) = after
    assert "ended community support on" in ended.message


def test_s023_18_warning_reads_the_injected_table_not_the_clock(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service())
    state = validate_config(project, SCHEMAS)
    table = (
        RabbitmqSeries(
            major=4,
            minor=4,
            end_of_support=dt.date(2030, 3, 1),
            erlang=ErlangRange(minimum=ErlangRelease(27, 0)),
        ),
    )

    assert rabbitmq_warnings(state, dt.date(2029, 1, 1), table) == ()
    (warning,) = rabbitmq_warnings(state, dt.date(2030, 1, 1), table)
    assert warning.context["endOfLife"] == "2030-03-01"
    assert "2030-03-01" in warning.message


def test_supported_series_are_4_3_and_4_4_with_their_dates_and_erlang() -> None:
    assert [
        (series.label, series.end_of_support, series.erlang.label)
        for series in SUPPORTED_SERIES
    ] == [
        ("4.3", dt.date(2027, 1, 31), "27.0 or later"),
        ("4.4", dt.date(2027, 7, 31), "27.0 or later"),
    ]
    assert supported_series(RabbitmqVersion("4.2.9")) is None
    assert supported_series(RabbitmqVersion("4.5.0")) is None
    erlang = SUPPORTED_SERIES[0].erlang
    assert erlang.contains(ErlangRelease(27, 0))
    assert erlang.contains(ErlangRelease(27, 3))
    assert not erlang.contains(ErlangRelease(26, 2))
