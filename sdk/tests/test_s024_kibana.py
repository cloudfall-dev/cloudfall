"""Declaring and validating a Kibana service (S-024, #567)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from cloudfall.cli import main
from cloudfall.domain import ResourceId, ResourceKind
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import (
    EngineContext,
    ExecutionStep,
    LifecycleError,
    backup_service,
    verify_backup,
)
from cloudfall.validation import SchemaCatalog, kibana_warnings, validate_config
from elasticsearch_project import SNAPSHOT_REPOSITORY
from jsonschema.exceptions import ValidationError
from kibana_project import (
    ANALYST,
    SCHEMAS,
    kibana_domain,
    kibana_service,
    search_main,
    write_project,
)

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).parents[2]


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


def _codes(document: dict[str, Any]) -> list[str]:
    return [warning["code"] for warning in document["warnings"]]


def _spec(service: dict[str, object]) -> dict[str, Any]:
    spec = service["spec"]
    assert isinstance(spec, dict)
    return spec


def _set_server_environment(project: Path, server: str, environment: str) -> None:
    path = project / "servers" / f"{server}.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["spec"]["environment"] = environment
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


# S-024-1


def test_s024_1_the_declaration_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", search_main(), kibana_service())

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document
    assert document["data"]["by_kind"]["Service"] == 2
    assert "KIBANA_PUBLISHED" not in _codes(document)


def test_s024_1_inventory_carries_kibana_without_a_backup(tmp_path: Path) -> None:
    project = write_project(tmp_path / "project", search_main(), kibana_service())

    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    services = {service.resource_id.value: service for service in inventory.services}
    kibana = services["search-kibana"].as_dict()
    assert kibana["serviceKind"] == "kibana"
    assert kibana["kibana"] == {
        "elasticsearch": "search-main",
        "heapMb": 1024,
        "users": [{"name": "analyst", "access": "edit"}],
    }
    assert "backup" not in kibana
    assert "metrics" not in kibana
    assert services["search-kibana"].backup is None
    # The Elasticsearch service keeps its backup, and analyst its own role
    search = services["search-main"].as_dict()
    assert "backup" in search
    assert search["elasticsearch"]["users"][0]["name"] == "analyst"  # type: ignore[index]


def test_s024_1_inventory_carries_the_domain_when_declared(tmp_path: Path) -> None:
    project = write_project(
        tmp_path / "project",
        search_main(),
        kibana_service(domain="search-kibana-site"),
        domains=(kibana_domain(),),
    )

    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (kibana,) = (
        service for service in inventory.services if service.kibana is not None
    )
    assert kibana.as_dict()["kibana"] == {
        "elasticsearch": "search-main",
        "heapMb": 1024,
        "users": [{"name": "analyst", "access": "edit"}],
        "domain": "search-kibana-site",
    }


# S-024-12: each refusal under Validation, and the warning


@pytest.mark.parametrize(
    ("kind", "block"),
    [
        (
            "postgresql",
            {
                "majorVersion": "17",
                "databases": [{"name": "crm", "application": "crm"}],
            },
        ),
        ("redis", {"maxmemoryMb": 256, "appendOnly": True}),
        ("elasticsearch", {"version": "9.2.4", "heapMb": 4096}),
        ("mariadb", {"version": "11.8.9", "innodbBufferPoolMb": 4096}),
        ("rabbitmq", {"version": "4.4.1", "memoryHighWatermarkMb": 2048}),
    ],
)
def test_s024_12_kibana_block_on_another_kind_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str, block: object
) -> None:
    service = search_main()
    spec = _spec(service)
    del spec["elasticsearch"]
    spec["serviceKind"] = kind
    spec[kind] = block
    spec["kibana"] = {"elasticsearch": "search-main", "heapMb": 1024}
    project = write_project(tmp_path / "project", service)

    _refused_with(project, capsys, "schema_validation_failed")


def test_s024_12_kibana_kind_without_its_block_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = kibana_service()
    del _spec(service)["kibana"]
    project = write_project(tmp_path / "project", search_main(), service)

    _refused_with(project, capsys, "schema_validation_failed")


def test_s024_12_another_kinds_block_on_kibana_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = kibana_service()
    _spec(service)["redis"] = {"maxmemoryMb": 256, "appendOnly": True}
    project = write_project(tmp_path / "project", search_main(), service)

    _refused_with(project, capsys, "schema_validation_failed")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "backup",
            {
                "directory": "/var/backups/cloudfall/search-kibana",
                "onCalendar": "*-*-* 03:20:00 UTC",
                "retentionDays": 14,
            },
        ),
        ("metrics", {"enabled": True}),
    ],
)
def test_s024_12_backup_and_metrics_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], field: str, value: object
) -> None:
    service = kibana_service()
    _spec(service)[field] = value
    project = write_project(tmp_path / "project", search_main(), service)

    context = _refused_with(project, capsys, "service_kibana_field_refused")
    assert context["field"] == f"spec.{field}"


def test_s024_12_remote_access_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = kibana_service(
        remoteAccess={"listenAddress": "10.0.0.11", "allowedSources": ["10.0.0.0/24"]}
    )
    project = write_project(tmp_path / "project", search_main(), service)

    context = _refused_with(project, capsys, "service_kibana_field_refused")
    assert context["field"] == "spec.kibana.remoteAccess"


def test_s024_12_elasticsearch_that_names_no_service_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project", search_main(), kibana_service(elasticsearch="search-x")
    )

    context = _refused_with(project, capsys, "resource_reference_missing")
    assert context["field"] == "spec.kibana.elasticsearch"


def test_s024_12_elasticsearch_that_names_another_kind_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        search_main(),
        kibana_service(elasticsearch="search-kibana"),
    )

    context = _refused_with(project, capsys, "resource_reference_missing")
    assert context["field"] == "spec.kibana.elasticsearch"


def test_s024_12_elasticsearch_of_another_environment_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        search_main(),
        kibana_service(server="h2", environment="staging"),
        servers=("h1", "h2"),
    )
    _set_server_environment(project, "h2", "staging")

    _refused_with(project, capsys, "service_kibana_environment")


def test_s024_12_server_that_is_not_a_node_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        search_main(),
        kibana_service(server="h2"),
        servers=("h1", "h2"),
    )

    context = _refused_with(project, capsys, "service_kibana_server_not_node")
    assert context["field"] == "spec.server"


def test_s024_12_elasticsearch_with_an_existing_cluster_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    search = search_main(
        clusterName="search",
        existingCluster={
            "seedHosts": ["10.0.0.11:9300", "10.0.0.12:9300"],
            "caCertificateFile": "pki/search-old-ca.pem",
        },
    )
    project = write_project(tmp_path / "project", search, kibana_service())

    _refused_with(project, capsys, "service_kibana_existing_cluster")


def test_s024_12_a_second_kibana_for_one_cluster_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        search_main(nodes=["h1", "h2"], snapshotRepository=SNAPSHOT_REPOSITORY),
        kibana_service(),
        kibana_service("search-kibana-2", server="h2"),
        servers=("h1", "h2"),
    )

    context = _refused_with(project, capsys, "service_kibana_duplicate")
    assert context["field"] == "spec.kibana.elasticsearch"


def test_s024_12_a_second_kibana_on_one_server_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        search_main(),
        kibana_service(),
        kibana_service("search-kibana-2", port=5602),
    )

    _refused_with(project, capsys, "service_kibana_duplicate")


def test_s024_12_a_bind_off_loopback_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The schema holds every service's bind to 127.0.0.1, so it refuses first."""
    service = kibana_service()
    _spec(service)["bind"] = {"address": "10.0.0.11", "port": 5601}
    project = write_project(tmp_path / "project", search_main(), service)

    context = _refused_with(project, capsys, "schema_validation_failed")
    assert context["field"] == "spec.bind.address"


@pytest.mark.parametrize("heap_mb", [512, 1023, 8193, 16384])
def test_s024_12_heap_outside_its_range_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], heap_mb: int
) -> None:
    project = write_project(
        tmp_path / "project", search_main(), kibana_service(heapMb=heap_mb)
    )

    context = _refused_with(project, capsys, "service_kibana_heap")
    assert context["field"] == "spec.kibana.heapMb"


@pytest.mark.parametrize("heap_mb", [1024, 8192])
def test_s024_12_heap_at_its_bounds_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], heap_mb: int
) -> None:
    project = write_project(
        tmp_path / "project", search_main(), kibana_service(heapMb=heap_mb)
    )

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


@pytest.mark.parametrize(
    "users",
    [
        [{"name": "support", "access": "view"}],
        [{"name": "analyst", "access": "edit"}, {"name": "analyst", "access": "view"}],
    ],
    ids=["undeclared", "twice"],
)
def test_s024_12_user_unknown_or_twice_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], users: list[object]
) -> None:
    project = write_project(
        tmp_path / "project", search_main(), kibana_service(users=users)
    )

    _refused_with(project, capsys, "service_kibana_user_unknown")


@pytest.mark.parametrize("name", ["cloudfall-kibana-view", "cloudfall-kibana-edit"])
def test_s024_12_elasticsearch_user_named_after_a_kibana_role_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    user = copy.deepcopy(ANALYST)
    user["name"] = name
    project = write_project(tmp_path / "project", search_main(users=[user]))

    context = _refused_with(project, capsys, "service_elasticsearch_user_reserved")
    assert context["field"] == "spec.elasticsearch.users.0.name"


def test_s024_12_domain_that_names_nothing_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        search_main(),
        kibana_service(domain="search-kibana-site"),
    )

    context = _refused_with(project, capsys, "resource_reference_missing")
    assert context["field"] == "spec.kibana.domain"


def _proxy(**fields: object) -> dict[str, object]:
    proxy = copy.deepcopy(_spec(kibana_domain())["proxy"])
    for key, value in fields.items():
        if value is None:
            del proxy[key]
        else:
            proxy[key] = value
    return dict(proxy)


@pytest.mark.parametrize(
    "domain_spec",
    [
        {"proxy": _proxy(server="h2")},
        {"proxy": _proxy(upstream={"address": "127.0.0.1", "port": 5602})},
        {"proxy": _proxy(upstream=None)},
        {"tls": {"mode": "disabled"}},
        {
            "proxy": _proxy(upstream=None),
            "routes": [
                {"path": "/", "upstream": {"address": "127.0.0.1", "port": 5601}}
            ],
        },
    ],
    ids=["proxy-server", "upstream-port", "no-upstream", "tls-disabled", "routes"],
)
def test_s024_12_domain_that_does_not_publish_kibana_alone_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    domain_spec: dict[str, object],
) -> None:
    project = write_project(
        tmp_path / "project",
        search_main(),
        kibana_service(domain="search-kibana-site"),
        servers=("h1", "h2"),
        domains=(kibana_domain(**domain_spec),),
    )

    context = _refused_with(project, capsys, "service_kibana_domain")
    assert context["field"] == "spec.kibana.domain"


def test_s024_12_a_published_kibana_validates_with_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        search_main(),
        kibana_service(domain="search-kibana-site"),
        domains=(kibana_domain(),),
    )

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document
    (warning,) = (
        warning
        for warning in document["warnings"]
        if warning["code"] == "KIBANA_PUBLISHED"
    )
    assert warning["context"] == {
        "service": "search-kibana",
        "domain": "search-kibana-site",
    }
    (found,) = kibana_warnings(validate_config(project, SCHEMAS))
    assert found.code == "KIBANA_PUBLISHED"


# backup is optional for kibana alone


_OTHER_KINDS: dict[str, dict[str, object]] = {
    "postgresql": {
        "majorVersion": "17",
        "databases": [{"name": "crm", "application": "crm"}],
    },
    "redis": {"maxmemoryMb": 256, "appendOnly": True},
    "elasticsearch": {"version": "9.2.4", "heapMb": 4096},
    "mariadb": {"version": "11.8.9", "innodbBufferPoolMb": 4096},
    "rabbitmq": {"version": "4.4.1", "memoryHighWatermarkMb": 2048},
}


@pytest.mark.parametrize("kind", sorted(_OTHER_KINDS))
def test_every_other_kind_still_requires_backup(kind: str) -> None:
    catalog = SchemaCatalog(SCHEMAS)
    service = search_main()
    spec = _spec(service)
    del spec["elasticsearch"]
    spec["serviceKind"] = kind
    spec[kind] = _OTHER_KINDS[kind]
    catalog.validate(ResourceKind.SERVICE, service)

    del spec["backup"]

    with pytest.raises(ValidationError, match="'backup' is a required property"):
        catalog.validate(ResourceKind.SERVICE, service)


def test_kibana_alone_needs_no_backup() -> None:
    SchemaCatalog(SCHEMAS).validate(ResourceKind.SERVICE, kibana_service())


@pytest.mark.parametrize("operation", [backup_service, verify_backup])
def test_backup_of_a_kibana_service_is_refused_before_any_step(
    tmp_path: Path, operation: Callable[..., object]
) -> None:
    """Kibana has no backup of its own: no playbook runs, the cluster is named."""
    project = write_project(tmp_path / "project", search_main(), kibana_service())
    steps: list[ExecutionStep] = []

    def run(step: ExecutionStep) -> object:
        steps.append(step)
        raise AssertionError(step.argv)

    context = EngineContext(
        project_directory=project,
        schema_directory=SCHEMAS,
        engine_directory=ROOT / "engine",
        inventory_file=project / "tmp" / "ansible-inventory.json",
        run=run,  # type: ignore[arg-type]
    )

    with pytest.raises(LifecycleError, match="Service/search-main") as error:
        operation(context, ResourceId("search-kibana"), tmp_path / "receipts")

    assert error.value.code == "lifecycle_service_no_backup"
    assert steps == []
