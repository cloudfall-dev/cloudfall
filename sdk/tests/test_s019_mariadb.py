"""Declaring and validating a MariaDB service (S-019, #419)."""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from cloudfall.cli import main
from cloudfall.inventory import PlatformInventory
from cloudfall.mariadb import (
    SUPPORTED_SERIES,
    MariadbSeries,
    MariadbVersion,
    supported_series,
)
from cloudfall.requirements import RequirementType, requirements_of
from cloudfall.validation import mariadb_versions_ending, validate_config
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from mariadb_project import (
    BILLING_PASSWORD_REF,
    EXAMPLES,
    SCHEMAS,
    SOURCE_CREDENTIALS_REF,
    edit_baseline,
    mariadb_service,
    primary_replication,
    replica_service,
    write_project,
)

if TYPE_CHECKING:
    from pathlib import Path

PAIR = ("h1", "h2")


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


def _pair(tmp_path: Path, replica: dict[str, object] | None = None) -> Path:
    """Write the primary on h1 and ``replica``, by default ``replica_service()``."""
    return write_project(
        tmp_path / "project",
        mariadb_service(replication=primary_replication()),
        replica_service() if replica is None else replica,
        servers=PAIR,
    )


def _source_replica(**source: object) -> dict[str, object]:
    return mariadb_service(
        databases=None,
        replication={
            "role": "replica",
            "serverId": 12,
            "source": {
                "host": "10.0.0.11",
                "port": 3306,
                "credentialsRef": SOURCE_CREDENTIALS_REF,
                **source,
            },
        },
    )


def _without_databases(service: dict[str, object]) -> dict[str, object]:
    spec = service["spec"]
    assert isinstance(spec, dict)
    mariadb = spec["mariadb"]
    assert isinstance(mariadb, dict)
    mariadb.pop("databases", None)
    return service


def _ca_certificate(*, ca: bool) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "old-db-ca")])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


# S-019-1


def test_s019_1_declaration_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", mariadb_service())

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document
    assert document["data"]["by_kind"]["Service"] == 1
    assert document["data"]["by_kind"]["Server"] == 1
    assert not [
        warning
        for warning in document["warnings"]
        if warning["code"].startswith("MARIADB_")
    ]


def test_s019_1_inventory_carries_databases_and_references_never_passwords(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", mariadb_service())

    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (service,) = inventory.services
    rendered = service.as_dict()
    assert rendered["serviceKind"] == "mariadb"
    assert rendered["mariadb"] == {
        "version": "11.8.9",
        "innodbBufferPoolMb": 4096,
        "databases": [
            {"name": "crm", "application": "crm", "owner": "crm"},
            {
                "name": "billing",
                "owner": "billing",
                "passwordRef": BILLING_PASSWORD_REF,
                "authentication": "mysql_native_password",
            },
        ],
    }
    # Only declared keys, the resolved user and plugin: no password, no hash
    assert _keys(rendered) <= _keys(mariadb_service()) | {"id", "authentication"}


def _keys(value: object) -> set[str]:
    """Return every mapping key in ``value``, at any depth."""
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            key for item in value.values() for key in _keys(item)
        }
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def test_s019_1_ed25519_owner_renders_its_plugin(tmp_path: Path) -> None:
    databases = [
        {
            "name": "billing",
            "owner": "billing",
            "passwordRef": BILLING_PASSWORD_REF,
            "authentication": "ed25519",
        }
    ]
    project = write_project(
        tmp_path / "project", mariadb_service(databases=databases)
    )

    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (service,) = inventory.services
    mariadb = service.as_dict()["mariadb"]
    assert isinstance(mariadb, dict)
    assert mariadb["databases"][0]["authentication"] == "ed25519"


def test_s019_1_a_pair_renders_its_replication(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _pair(tmp_path)

    exit_code, document = _validate(project, capsys)
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    assert exit_code == 0, document
    primary, replica = (service.as_dict()["mariadb"] for service in inventory.services)
    assert isinstance(primary, dict)
    assert isinstance(replica, dict)
    assert primary["replication"] == primary_replication()
    assert replica["replication"] == {
        "role": "replica",
        "serverId": 2,
        "primary": "mariadb-main",
    }
    assert replica["databases"] == []


def test_s019_1_source_renders_its_references_and_default_port(
    tmp_path: Path,
) -> None:
    service = _source_replica()
    spec = service["spec"]
    assert isinstance(spec, dict)
    del spec["mariadb"]["replication"]["source"]["port"]
    project = write_project(tmp_path / "project", _without_databases(service))

    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (rendered,) = (item.as_dict()["mariadb"] for item in inventory.services)
    assert isinstance(rendered, dict)
    assert rendered["replication"]["source"] == {
        "host": "10.0.0.11",
        "port": 3306,
        "credentialsRef": SOURCE_CREDENTIALS_REF,
    }


def test_s019_1_standalone_server_needs_its_volume_and_package(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", mariadb_service())

    (server,) = requirements_of(
        PlatformInventory.from_state(validate_config(project, SCHEMAS))
    )

    placed = {
        (item.spec.requirement_type, item.spec.name.value): item.spec.as_dict()
        for item in server.requirements
        if item.owner.resource_id.value == "mariadb-main"
    }
    assert placed == {
        (RequirementType.VOLUME, "mariadb"): {
            "path": "/var/lib/mysql",
            "minimumBytes": 10 * 1024**3,
            "placement": "mirrored",
            "units": ["mariadb.service"],
            "pool": "data",
            "volumeGroup": "vg_data",
            "logicalVolume": "lv_mariadb",
            "type": "raid1",
            "physicalVolumes": ["/dev/nvme0n1p5", "/dev/nvme1n1p5"],
        },
        (RequirementType.PACKAGE, "mariadb"): {
            "package": "mariadb-server",
            "version": "11.8.9",
        },
    }


def test_s019_1_primary_needs_its_port_and_certificate(
    tmp_path: Path,
) -> None:
    project = _pair(tmp_path)

    h1, h2 = requirements_of(
        PlatformInventory.from_state(validate_config(project, SCHEMAS))
    )

    primary = {
        (item.spec.requirement_type, item.spec.name.value): item.spec.as_dict()
        for item in h1.requirements
    }
    assert primary[(RequirementType.PORT, "mariadb-replication")] == {
        "port": 3306,
        "protocol": "tcp",
        "sources": ["h2"],
    }
    certificate = primary[(RequirementType.CERTIFICATE, "mariadb")]
    assert certificate["usage"] == "server"
    assert certificate["subjectNames"] == ["h1.example.internal", "203.0.113.11"]
    # The replication password lands on no host: the primary gets its hash,
    # a replica the value for one run (#423), so it is no requirement;
    # `secrets generate` makes it all the same
    assert (RequirementType.SECRET, "mariadb-replication-password") not in primary
    replica = {
        (item.spec.requirement_type, item.spec.name.value) for item in h2.requirements
    }
    assert (RequirementType.CERTIFICATE, "mariadb") in replica
    assert (RequirementType.SECRET, "mariadb-replication-password") not in replica
    assert (RequirementType.PORT, "mariadb-replication") not in replica


# S-019-10: each refusal under Validation


@pytest.mark.parametrize("kind", ["postgresql", "redis", "elasticsearch"])
def test_s019_10_mariadb_block_on_another_kind_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    service = mariadb_service()
    spec = service["spec"]
    assert isinstance(spec, dict)
    spec["serviceKind"] = kind
    spec[kind] = {
        "postgresql": {"majorVersion": "17", "databases": [{"name": "crm",
                                                             "application": "crm"}]},
        "redis": {"maxmemoryMb": 256, "appendOnly": True},
        "elasticsearch": {"version": "9.2.4", "heapMb": 4096},
    }[kind]
    project = write_project(tmp_path / "project", service)

    _refused_with(project, capsys, "schema_validation_failed")


def test_s019_10_mariadb_kind_without_its_block_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = mariadb_service()
    spec = service["spec"]
    assert isinstance(spec, dict)
    del spec["mariadb"]
    project = write_project(tmp_path / "project", service)

    _refused_with(project, capsys, "schema_validation_failed")


@pytest.mark.parametrize("version", ["11.8", "11.8.9-1", "v11.8.9", "011.8.9"])
def test_s019_10_version_that_is_not_x_y_z_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    project = write_project(tmp_path / "project", mariadb_service(version=version))

    context = _refused_with(project, capsys, "service_mariadb_version")
    assert context["field"] == "spec.mariadb.version"


@pytest.mark.parametrize("version", ["10.6.20", "11.7.2", "12.0.2", "12.2.1"])
def test_s019_10_version_of_an_unsupported_series_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    project = write_project(tmp_path / "project", mariadb_service(version=version))

    context = _refused_with(project, capsys, "service_mariadb_version")
    assert version in context["message"]


@pytest.mark.parametrize("version", ["10.11.15", "11.4.9", "11.8.9", "12.3.1"])
def test_s019_10_supported_series_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    project = write_project(tmp_path / "project", mariadb_service(version=version))

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


def test_s019_10_server_holding_two_mariadb_services_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        mariadb_service(),
        mariadb_service("mariadb-other", port=3307, databases=None),
    )

    context = _refused_with(project, capsys, "service_mariadb_server_shared")
    assert context["field"] == "spec.server"


@pytest.mark.parametrize(
    "database",
    [
        {"name": "crm", "application": "crm", "owner": "crm2",
         "passwordRef": BILLING_PASSWORD_REF},
        {"name": "crm"},
        {"name": "billing", "owner": "billing"},
        {"name": "crm", "application": "crm", "passwordRef": BILLING_PASSWORD_REF},
        {"name": "crm", "application": "crm", "authentication": "ed25519"},
        {"name": "billing", "owner": "billing", "passwordRef": BILLING_PASSWORD_REF,
         "password": "hunter2"},
    ],
    ids=[
        "both",
        "neither",
        "owner-without-password-ref",
        "password-ref-next-to-application",
        "authentication-next-to-application",
        "plain-password",
    ],
)
def test_s019_10_database_entry_outside_the_s006_rules_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], database: dict[str, object]
) -> None:
    project = write_project(
        tmp_path / "project", mariadb_service(databases=[database])
    )

    _refused_with(project, capsys, "schema_validation_failed")


@pytest.mark.parametrize(
    "owner", ["root", "mysql", "mariadb.sys", "cloudfall_repl", "alloy", "sys", "crm"]
)
def test_s019_10_reserved_owner_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], owner: str
) -> None:
    databases = [
        {"name": "crm", "application": "crm"},
        {"name": "billing", "owner": owner, "passwordRef": BILLING_PASSWORD_REF},
    ]
    project = write_project(
        tmp_path / "project", mariadb_service(databases=databases)
    )

    context = _refused_with(project, capsys, "service_database_owner_reserved")
    assert context["field"] == "spec.mariadb.databases.1.owner"


@pytest.mark.parametrize(
    "name",
    ["root", "mysql", "information_schema", "performance_schema", "sys", "alloy"],
)
def test_s019_10_reserved_database_name_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    databases = [
        {"name": name, "owner": "billing", "passwordRef": BILLING_PASSWORD_REF}
    ]
    project = write_project(
        tmp_path / "project", mariadb_service(databases=databases)
    )

    context = _refused_with(project, capsys, "service_database_owner_reserved")
    assert context["field"] == "spec.mariadb.databases.0.name"


def test_s019_10_application_whose_linux_user_is_reserved_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", mariadb_service())
    path = project / "applications" / "crm.yaml"
    application = yaml.safe_load(path.read_text(encoding="utf-8"))
    application["spec"]["linuxUser"] = "mysql"
    path.write_text(yaml.safe_dump(application), encoding="utf-8")

    context = _refused_with(project, capsys, "service_database_owner_reserved")
    assert context["field"] == "spec.mariadb.databases.0.application"


def test_s019_10_duplicate_database_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    databases = [
        {"name": "crm", "application": "crm"},
        {"name": "crm", "owner": "billing", "passwordRef": BILLING_PASSWORD_REF},
    ]
    project = write_project(
        tmp_path / "project", mariadb_service(databases=databases)
    )

    _refused_with(project, capsys, "service_database_duplicate")


def test_s019_10_two_services_with_one_server_id_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _pair(tmp_path, replica_service(server_id=1))

    context = _refused_with(project, capsys, "service_mariadb_server_id_duplicate")
    assert context["field"] == "spec.mariadb.replication.serverId"


def test_s019_10_primary_without_listen_address_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replication = primary_replication()
    del replication["listenAddress"]
    project = write_project(
        tmp_path / "project", mariadb_service(replication=replication)
    )

    _refused_with(project, capsys, "service_mariadb_replication_listen")


@pytest.mark.parametrize(
    "address",
    ["0.0.0.0", "::", "10.0.0.02", "h1"],  # noqa: S104 - refused, never bound
)
def test_s019_10_primary_listen_address_that_is_no_one_address_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], address: str
) -> None:
    project = write_project(
        tmp_path / "project",
        mariadb_service(replication=primary_replication(listenAddress=address)),
    )

    context = _refused_with(project, capsys, "service_mariadb_replication_listen")
    assert context["field"] == "spec.mariadb.replication.listenAddress"


def test_s019_10_replica_with_listen_address_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        mariadb_service(replication=primary_replication()),
        replica_service(),
        servers=PAIR,
    )
    path = project / "services" / "mariadb-replica.yaml"
    service = yaml.safe_load(path.read_text(encoding="utf-8"))
    service["spec"]["mariadb"]["replication"]["listenAddress"] = "203.0.113.12"
    path.write_text(yaml.safe_dump(service), encoding="utf-8")

    _refused_with(project, capsys, "service_mariadb_replication_listen")


def test_s019_10_replica_with_both_primary_and_source_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replica = replica_service()
    spec = replica["spec"]
    assert isinstance(spec, dict)
    spec["mariadb"]["replication"]["source"] = {
        "host": "10.0.0.11",
        "credentialsRef": SOURCE_CREDENTIALS_REF,
    }
    project = write_project(
        tmp_path / "project",
        mariadb_service(replication=primary_replication()),
        replica,
        servers=PAIR,
    )

    _refused_with(project, capsys, "service_mariadb_replication_primary")


def test_s019_10_replica_with_neither_primary_nor_source_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replica = replica_service()
    spec = replica["spec"]
    assert isinstance(spec, dict)
    del spec["mariadb"]["replication"]["primary"]
    project = write_project(tmp_path / "project", replica, servers=PAIR)

    _refused_with(project, capsys, "service_mariadb_replication_primary")


@pytest.mark.parametrize("primary", ["mariadb-unknown", "mariadb-replica"])
def test_s019_10_replica_of_an_unknown_service_or_itself_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], primary: str
) -> None:
    project = _pair(tmp_path, replica_service(primary=primary))

    context = _refused_with(project, capsys, "service_mariadb_replication_primary")
    assert context["field"] == "spec.mariadb.replication.primary"


def test_s019_10_replica_of_a_service_of_another_kind_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project", replica_service(primary="postgresql-main"),
        servers=PAIR,
    )
    (project / "services" / "postgresql-main.yaml").write_text(
        (EXAMPLES / "services" / "postgresql-main.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    _refused_with(project, capsys, "service_mariadb_replication_primary")


def test_s019_10_replica_of_a_standalone_server_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project", mariadb_service(), replica_service(), servers=PAIR
    )

    context = _refused_with(project, capsys, "service_mariadb_replication_primary")
    assert "role: primary" in context["message"]


def test_s019_10_replica_of_a_primary_of_another_environment_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _pair(tmp_path)
    for path in (
        project / "servers" / "h2.yaml",
        project / "services" / "mariadb-replica.yaml",
    ):
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
        content["spec"]["environment"] = "staging"
        path.write_text(yaml.safe_dump(content), encoding="utf-8")

    context = _refused_with(project, capsys, "service_mariadb_replication_primary")
    assert "staging" not in context["message"]
    assert "production" in context["message"]


def test_s019_10_replica_on_its_primarys_server_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Refused before the two-services rule, which would also refuse it."""
    project = _pair(tmp_path, replica_service(server="h1", port=3307))

    _refused_with(project, capsys, "service_mariadb_replication_primary")


def test_s019_10_replica_with_databases_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replica = replica_service()
    spec = replica["spec"]
    assert isinstance(spec, dict)
    spec["mariadb"]["databases"] = [{"name": "crm", "application": "crm"}]
    project = write_project(
        tmp_path / "project",
        mariadb_service(replication=primary_replication()),
        replica,
        servers=PAIR,
    )

    context = _refused_with(project, capsys, "service_mariadb_replica_databases")
    assert context["field"] == "spec.mariadb.databases"


@pytest.mark.parametrize("version", ["11.8.8", "11.4.9", "12.3.1"])
def test_s019_10_replica_of_another_series_or_older_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    project = _pair(tmp_path, replica_service(version=version))

    context = _refused_with(project, capsys, "service_mariadb_replica_version")
    assert context["field"] == "spec.mariadb.version"


def test_s019_10_replica_of_a_newer_patch_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _pair(tmp_path, replica_service(version="11.8.10"))

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


@pytest.mark.parametrize("version", ["11.8", "10.6.1"])
def test_s019_10_replica_read_first_refuses_its_primarys_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    """A replica sorted before its primary names the primary's own refusal."""
    project = write_project(
        tmp_path / "project",
        mariadb_service(version=version, replication=primary_replication()),
        replica_service("a-replica"),
        servers=PAIR,
    )

    context = _refused_with(project, capsys, "service_mariadb_version")
    assert context["source"].endswith("mariadb-main.yaml#1")


def test_s019_10_pair_without_a_firewall_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _pair(tmp_path)
    edit_baseline(project, firewall=None)

    _refused_with(project, capsys, "service_mariadb_replication_unfirewalled")


def test_s019_10_primary_with_a_replica_needs_a_firewall_of_its_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _pair(tmp_path)
    baseline_path = project / "baselines" / "debian-application.yaml"
    open_baseline = yaml.safe_load(baseline_path.read_text(encoding="utf-8"))
    open_baseline["metadata"]["id"] = "debian-open"
    del open_baseline["spec"]["firewall"]
    (project / "baselines" / "debian-open.yaml").write_text(
        yaml.safe_dump(open_baseline), encoding="utf-8"
    )
    server_path = project / "servers" / "h1.yaml"
    server = yaml.safe_load(server_path.read_text(encoding="utf-8"))
    server["spec"]["baseline"] = "debian-open"
    server_path.write_text(yaml.safe_dump(server), encoding="utf-8")

    context = _refused_with(project, capsys, "service_mariadb_replication_unfirewalled")
    assert "primary" in context["message"]


def test_s019_10_primary_without_replicas_needs_no_firewall(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project", mariadb_service(replication=primary_replication())
    )
    edit_baseline(project, firewall=None)

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


def test_s019_10_replica_server_without_an_address_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _pair(tmp_path)
    path = project / "servers" / "h2.yaml"
    server = yaml.safe_load(path.read_text(encoding="utf-8"))
    del server["spec"]["network"]
    path.write_text(yaml.safe_dump(server), encoding="utf-8")

    _refused_with(project, capsys, "service_mariadb_replica_unaddressed")


def test_s019_10_source_with_a_ca_certificate_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        _without_databases(_source_replica(caCertificateFile="pki/old-db-ca.pem")),
    )
    (project / "pki").mkdir()
    (project / "pki" / "old-db-ca.pem").write_bytes(_ca_certificate(ca=True))

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document
    assert not [
        warning
        for warning in document["warnings"]
        if warning["code"] == "MARIADB_REPLICATION_UNENCRYPTED"
    ]


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("pki/missing.pem", None),
        ("../outside.pem", b""),
        ("pki/leaf.pem", "leaf"),
        ("pki/garbage.pem", b"not a certificate"),
    ],
)
def test_s019_10_source_ca_file_missing_outside_or_not_a_ca_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    path: str,
    content: bytes | str | None,
) -> None:
    project = write_project(
        tmp_path / "project",
        _without_databases(_source_replica(caCertificateFile=path)),
    )
    (project / "pki").mkdir()
    if content is not None:
        data = _ca_certificate(ca=False) if content == "leaf" else content
        assert isinstance(data, bytes)
        (project / path).write_bytes(data)

    context = _refused_with(project, capsys, "service_mariadb_replication_source")
    assert context["field"] == "spec.mariadb.replication.source.caCertificateFile"


@pytest.mark.parametrize(
    ("pools", "code"),
    [
        (None, "volume_requirement_pool_missing"),
        (
            [
                {"id": "data", "volumeGroup": "vg_data", "primary": "/dev/sda5",
                 "secondary": "/dev/sdb5"},
                {"id": "more", "volumeGroup": "vg_more", "primary": "/dev/sdc5",
                 "secondary": "/dev/sdd5"},
            ],
            "volume_requirement_pool_ambiguous",
        ),
        (
            [{"id": "data", "volumeGroup": "vg_data", "primary": "/dev/sda5"}],
            "volume_requirement_pool_unmirrored",
        ),
    ],
)
def test_s019_10_server_without_one_mirrored_pool_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    pools: list[dict[str, object]] | None,
    code: str,
) -> None:
    project = write_project(tmp_path / "project", mariadb_service())
    path = project / "baselines" / "debian-application.yaml"
    baseline = yaml.safe_load(path.read_text(encoding="utf-8"))
    if pools is None:
        del baseline["spec"]["storage"]["pools"]
    else:
        baseline["spec"]["storage"]["pools"] = pools
    path.write_text(yaml.safe_dump(baseline), encoding="utf-8")

    _refused_with(project, capsys, code)


REMOTE_ACCESS = {"listenAddress": "10.0.0.2", "allowedSources": ["10.0.0.0/24"]}


def test_s019_10_remote_access_validates_with_an_owner_and_a_firewall(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project", mariadb_service(remoteAccess=REMOTE_ACCESS)
    )

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


def test_s019_10_remote_access_without_an_owner_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        mariadb_service(
            remoteAccess=REMOTE_ACCESS,
            databases=[{"name": "crm", "application": "crm"}],
        ),
    )

    _refused_with(project, capsys, "service_remote_access_unused")


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"firewall": None}, "service_remote_access_unfirewalled"),
        (
            {"firewall": {"policy": "default-deny", "allowedInbound": [
                {"port": 3306, "protocol": "tcp", "description": "mariadb"}]}},
            "service_remote_access_port_open",
        ),
    ],
)
def test_s019_10_remote_access_under_the_s006_rules_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, object],
    code: str,
) -> None:
    project = write_project(
        tmp_path / "project", mariadb_service(remoteAccess=REMOTE_ACCESS)
    )
    edit_baseline(project, **change)

    _refused_with(project, capsys, code)


def test_s019_10_remote_access_from_every_address_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    remote_access = {"listenAddress": "10.0.0.2", "allowedSources": ["0.0.0.0/0"]}
    project = write_project(
        tmp_path / "project", mariadb_service(remoteAccess=remote_access)
    )

    _refused_with(project, capsys, "service_remote_access_open")


def test_s019_10_remote_access_from_an_ipv6_network_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    remote_access = {
        "listenAddress": "10.0.0.2",
        "allowedSources": ["10.0.0.0/24", "2001:db8::/64"],
    }
    project = write_project(
        tmp_path / "project", mariadb_service(remoteAccess=remote_access)
    )

    context = _refused_with(project, capsys, "service_mariadb_remote_access_ipv6")

    assert "2001:db8::/64" in context["message"]
    assert "single IPv6 address" in context["message"]
    assert context["field"] == "spec.mariadb.remoteAccess.allowedSources.1"


@pytest.mark.parametrize("source", ["2001:db8::7/128", "10.0.0.0/24", "10.0.0.7/32"])
def test_s019_10_remote_access_from_one_ipv6_address_or_ipv4_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    remote_access = {"listenAddress": "10.0.0.2", "allowedSources": [source]}
    project = write_project(
        tmp_path / "project", mariadb_service(remoteAccess=remote_access)
    )

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


def test_s019_10_remote_access_on_a_replica_needs_no_owner_of_its_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _pair(tmp_path, replica_service(remoteAccess=REMOTE_ACCESS))

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


def test_s019_10_source_without_a_ca_file_warns_unencrypted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", _without_databases(_source_replica()))

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document
    (warning,) = [
        warning
        for warning in document["warnings"]
        if warning["code"] == "MARIADB_REPLICATION_UNENCRYPTED"
    ]
    assert warning["context"]["service"] == "mariadb-main"


def test_s019_10_source_host_that_is_no_host_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        _without_databases(_source_replica(host="not a host")),
    )

    _refused_with(project, capsys, "service_mariadb_replication_source")


def test_s019_10_series_within_90_days_of_its_date_warns_naming_it(
    tmp_path: Path,
) -> None:
    project = write_project(
        tmp_path / "project", mariadb_service(version="10.11.15")
    )
    state = validate_config(project, SCHEMAS)
    series = supported_series(MariadbVersion("10.11.15"))
    assert series is not None
    end = series.end_of_life

    long_before = mariadb_versions_ending(state, end - dt.timedelta(91))
    window_opens = mariadb_versions_ending(state, end - dt.timedelta(90))
    after = mariadb_versions_ending(state, end + dt.timedelta(1))

    assert long_before == ()
    (ending,) = window_opens
    assert ending.service.value == "mariadb-main"
    assert ending.series is series
    message = ending.message(end - dt.timedelta(90))
    assert "2028-02-16" in message
    assert "ends on" in message
    (ended,) = after
    assert "ended on" in ended.message(end + dt.timedelta(1))


def test_s019_10_warning_reads_the_injected_table_not_the_clock(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", mariadb_service())
    state = validate_config(project, SCHEMAS)
    table = (MariadbSeries(major=11, minor=8, end_of_life=dt.date(2030, 3, 1)),)

    assert mariadb_versions_ending(state, dt.date(2029, 1, 1), table) == ()
    (ending,) = mariadb_versions_ending(state, dt.date(2030, 1, 1), table)
    assert "2030-03-01" in ending.message(dt.date(2030, 1, 1))


def test_supported_series_are_maintained_long_term_series() -> None:
    assert [(series.label, series.end_of_life.isoformat()) for series in
            SUPPORTED_SERIES] == [
        ("10.11", "2028-02-16"),
        ("11.4", "2029-05-29"),
        ("11.8", "2028-06-04"),
        ("12.3", "2029-06-12"),
    ]
    assert supported_series(MariadbVersion("11.7.2")) is None
    assert MariadbVersion("11.8.10").series == "11.8"
    assert MariadbVersion("11.8.9").is_older_than(MariadbVersion("11.8.10"))
