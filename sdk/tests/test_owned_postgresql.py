"""Validation of PostgreSQL databases owned by a declared role (S-006)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from cloudfall.cli import main
from cloudfall.domain import ResourceId
from cloudfall.inventory import OwnedPostgresDatabase, PlatformInventory
from cloudfall.validation import ConfigValidationError, validate_config

if TYPE_CHECKING:
    from collections.abc import Mapping

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
SCHEMAS = ROOT / "config" / "schemas" / "v1"
PASSWORD_REF = {
    "environment": "production",
    "path": "/billing/postgresql",
    "key": "BILLING_PASSWORD",
}


def _owned_service(
    databases: list[dict[str, object]],
    remote_access: Mapping[str, object] | None = None,
) -> dict[str, object]:
    postgresql: dict[str, object] = {"majorVersion": "17", "databases": databases}
    if remote_access is not None:
        postgresql["remoteAccess"] = remote_access
    return {
        "apiVersion": "cloudfall/v1",
        "kind": "Service",
        "metadata": {"id": "postgresql-billing"},
        "spec": {
            "serviceKind": "postgresql",
            "environment": "production",
            "server": "h1",
            "bind": {"address": "127.0.0.1", "port": 5432},
            "postgresql": postgresql,
            "backup": {
                "directory": "/var/backups/cloudfall/postgresql-billing",
                "onCalendar": "*-*-* 02:00:00 UTC",
                "retentionDays": 14,
            },
        },
    }


def _billing(**extra: object) -> dict[str, object]:
    return {"name": "billing", "owner": "billing", "passwordRef": PASSWORD_REF, **extra}


def _standalone_project(tmp_path: Path, service: dict[str, object]) -> Path:
    """Write a project of one Server, its server type, and one Service."""
    project = tmp_path / "project"
    (project / "servers").mkdir(parents=True)
    (project / "server-types").mkdir()
    (project / "services").mkdir()
    shutil.copy(EXAMPLES / "servers" / "h1.yaml", project / "servers" / "h1.yaml")
    shutil.copy(
        EXAMPLES / "server-types" / "debian-application.yaml",
        project / "server-types" / "debian-application.yaml",
    )
    _write_service(project, service)
    return project


def _write_service(project: Path, service: dict[str, object]) -> None:
    (project / "services" / "postgresql-billing.yaml").write_text(
        yaml.safe_dump(service, sort_keys=False), encoding="utf-8"
    )


def _validate(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, dict[str, Any]]]:
    exit_code = main(
        ["config", "validate", "--project", str(project), "--schemas", str(SCHEMAS)]
    )
    document: dict[str, dict[str, Any]] = json.loads(capsys.readouterr().out)
    return exit_code, document


def _refusal(project: Path) -> ConfigValidationError:
    with pytest.raises(ConfigValidationError) as error:
        validate_config(project, SCHEMAS)
    return error.value


def test_s006_1_owner_database_validates_without_application(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _standalone_project(tmp_path, _owned_service([_billing()]))

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0
    assert document["data"]["by_kind"] == {
        "Server": 1,
        "ServerType": 1,
        "Service": 1,
    }


def test_s006_1_owner_database_renders_reference_without_application(
    tmp_path: Path,
) -> None:
    project = _standalone_project(tmp_path, _owned_service([_billing()]))

    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (service,) = inventory.services
    assert service.postgresql is not None
    (database,) = service.postgresql.databases
    assert isinstance(database, OwnedPostgresDatabase)
    assert service.as_dict()["postgresql"] == {
        "majorVersion": "17",
        "databases": [
            {"name": "billing", "owner": "billing", "passwordRef": PASSWORD_REF}
        ],
    }


@pytest.mark.parametrize(
    "database",
    [
        _billing(application="crm"),
        {"name": "billing"},
    ],
    ids=["both", "neither"],
)
def test_s006_2_database_names_exactly_one_of_application_or_owner(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    database: dict[str, object],
) -> None:
    project = _standalone_project(tmp_path, _owned_service([database]))

    exit_code, document = _validate(project, capsys)

    assert exit_code == 80
    assert document["error"]["code"] == "CONFIG_INVALID"
    assert document["error"]["context"]["field"] == "spec.postgresql.databases.0"


@pytest.mark.parametrize(
    "database",
    [
        {"name": "billing", "owner": "billing"},
        {"name": "crm", "application": "crm", "passwordRef": PASSWORD_REF},
    ],
    ids=["owner-without-ref", "ref-next-to-application"],
)
def test_s006_3_password_ref_goes_with_owner_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    database: dict[str, object],
) -> None:
    project = _standalone_project(tmp_path, _owned_service([database]))

    exit_code, document = _validate(project, capsys)

    assert exit_code == 80
    assert document["error"]["code"] == "CONFIG_INVALID"
    assert document["error"]["context"]["field"] == "spec.postgresql.databases.0"
    assert "passwordRef" in document["error"]["message"]


@pytest.mark.parametrize("owner", ["postgres", "alloy"])
def test_s006_4_owner_refuses_cloudfall_roles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], owner: str
) -> None:
    project = _standalone_project(
        tmp_path, _owned_service([_billing(owner=owner)])
    )

    exit_code, document = _validate(project, capsys)

    assert exit_code == 80
    assert document["error"]["code"] == "CONFIG_INVALID"
    assert document["error"]["context"]["code"] == "service_database_owner_reserved"


def test_s006_4_owner_refuses_an_application_user_on_the_service(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    service_path = project / "services" / "postgresql-main.yaml"
    service = yaml.safe_load(service_path.read_text(encoding="utf-8"))
    service["spec"]["postgresql"]["databases"].append(_billing(owner="crm"))
    service_path.write_text(yaml.safe_dump(service), encoding="utf-8")

    error = _refusal(project)

    assert error.issue.code == "service_database_owner_reserved"
    assert error.issue.field_path == ("spec", "postgresql", "databases", 1, "owner")


def test_owner_may_share_a_name_with_an_application_elsewhere(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    service = _owned_service([_billing(owner="crm")])
    service["spec"]["server"] = "h2"  # type: ignore[index]
    _write_service(project, service)

    config = validate_config(project, SCHEMAS)

    assert config.resource_count == 13


REMOTE_ACCESS = {"listenAddress": "10.0.0.2", "allowedSources": ["10.0.0.0/24"]}


def test_remote_access_with_an_owner_validates_and_renders(tmp_path: Path) -> None:
    project = _standalone_project(
        tmp_path, _owned_service([_billing()], remote_access=REMOTE_ACCESS)
    )

    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (service,) = inventory.services
    assert service.resource_id == ResourceId("postgresql-billing")
    postgresql = service.as_dict()["postgresql"]
    assert isinstance(postgresql, dict)
    assert postgresql["remoteAccess"] == REMOTE_ACCESS


def test_s006_11_remote_access_needs_an_owner_database(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    service_path = project / "services" / "postgresql-main.yaml"
    service = yaml.safe_load(service_path.read_text(encoding="utf-8"))
    service["spec"]["postgresql"]["remoteAccess"] = REMOTE_ACCESS
    service_path.write_text(yaml.safe_dump(service), encoding="utf-8")

    assert _refusal(project).issue.code == "service_remote_access_unused"


def test_s006_11_remote_access_needs_a_firewall(tmp_path: Path) -> None:
    project = _standalone_project(
        tmp_path, _owned_service([_billing()], remote_access=REMOTE_ACCESS)
    )
    server_type_path = project / "server-types" / "debian-application.yaml"
    server_type = yaml.safe_load(server_type_path.read_text(encoding="utf-8"))
    del server_type["spec"]["firewall"]
    server_type_path.write_text(yaml.safe_dump(server_type), encoding="utf-8")

    assert _refusal(project).issue.code == "service_remote_access_unfirewalled"


def test_s006_11_remote_access_refuses_a_port_already_open(tmp_path: Path) -> None:
    project = _standalone_project(
        tmp_path, _owned_service([_billing()], remote_access=REMOTE_ACCESS)
    )
    server_type_path = project / "server-types" / "debian-application.yaml"
    server_type = yaml.safe_load(server_type_path.read_text(encoding="utf-8"))
    server_type["spec"]["firewall"]["allowedInbound"].append(
        {"port": 5432, "protocol": "tcp", "description": "postgresql"}
    )
    server_type_path.write_text(yaml.safe_dump(server_type), encoding="utf-8")

    assert _refusal(project).issue.code == "service_remote_access_port_open"


@pytest.mark.parametrize("source", ["0.0.0.0/0", "::/0"])
def test_s006_11_remote_access_refuses_every_address(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    remote_access = {"listenAddress": "10.0.0.2", "allowedSources": [source]}
    project = _standalone_project(
        tmp_path, _owned_service([_billing()], remote_access=remote_access)
    )

    exit_code, document = _validate(project, capsys)

    assert exit_code == 80
    assert document["error"]["code"] == "CONFIG_INVALID"
    assert document["error"]["context"]["code"] == "service_remote_access_open"


def test_remote_access_refuses_a_non_canonical_source(tmp_path: Path) -> None:
    remote_access = {"listenAddress": "10.0.0.2", "allowedSources": ["10.0.0.1/24"]}
    project = _standalone_project(
        tmp_path, _owned_service([_billing()], remote_access=remote_access)
    )

    assert _refusal(project).issue.code == "service_remote_access_source_invalid"


@pytest.mark.parametrize(
    "address",
    ["0.0.0.0", "::", "not-an-address"],  # noqa: S104 - refused wildcards
)
def test_remote_access_listen_address_is_one_address(
    tmp_path: Path, address: str
) -> None:
    remote_access = {"listenAddress": address, "allowedSources": ["10.0.0.0/24"]}
    project = _standalone_project(
        tmp_path, _owned_service([_billing()], remote_access=remote_access)
    )

    assert _refusal(project).issue.code == "schema_validation_failed"


@pytest.mark.parametrize(
    "address",
    ["::0", "0:0:0:0:0:0:0:0", "fd00::0002"],
    ids=["wildcard-short", "wildcard-long", "non-canonical"],
)
def test_remote_access_listen_address_is_one_canonical_address(
    tmp_path: Path, address: str
) -> None:
    remote_access = {"listenAddress": address, "allowedSources": ["10.0.0.0/24"]}
    project = _standalone_project(
        tmp_path, _owned_service([_billing()], remote_access=remote_access)
    )

    error = _refusal(project)

    assert error.issue.code == "service_remote_access_listen_invalid"
    assert error.issue.field_path == (
        "spec",
        "postgresql",
        "remoteAccess",
        "listenAddress",
    )


PG_HBA_KEYWORDS = ["all", "sameuser", "samerole", "samegroup", "replication"]


@pytest.mark.parametrize("keyword", PG_HBA_KEYWORDS)
@pytest.mark.parametrize("field_name", ["name", "owner"])
def test_remote_access_refuses_a_name_pg_hba_reads_as_a_keyword(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    keyword: str,
    field_name: str,
) -> None:
    database = _billing(**{field_name: keyword})
    project = _standalone_project(
        tmp_path, _owned_service([database], remote_access=REMOTE_ACCESS)
    )

    exit_code, document = _validate(project, capsys)

    assert exit_code == 80
    assert document["error"]["code"] == "CONFIG_INVALID"
    context = document["error"]["context"]
    assert context["code"] == "service_remote_access_name_reserved"
    assert context["field"] == f"spec.postgresql.databases.0.{field_name}"


@pytest.mark.parametrize("keyword", PG_HBA_KEYWORDS)
@pytest.mark.parametrize("field_name", ["name", "owner"])
def test_a_keyword_name_validates_without_remote_access(
    tmp_path: Path, keyword: str, field_name: str
) -> None:
    database = _billing(**{field_name: keyword})
    project = _standalone_project(tmp_path, _owned_service([database]))

    assert validate_config(project, SCHEMAS).resource_count == 3
