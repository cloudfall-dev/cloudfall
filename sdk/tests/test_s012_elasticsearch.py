"""Declaring and validating an Elasticsearch service (S-012, #309)."""

from __future__ import annotations

import datetime
import json
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from cloudfall.cli import main
from cloudfall.elasticsearch import (
    SUPPORTED_LINES,
    ElasticsearchLine,
    ElasticsearchVersion,
    supported_line,
)
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import (
    ConfigValidationError,
    elasticsearch_versions_ending,
    validate_config,
)
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from elasticsearch_project import (
    PASSWORD_REF,
    SCHEMAS,
    SNAPSHOT_REPOSITORY,
    elasticsearch_service,
    write_project,
)

if TYPE_CHECKING:
    from pathlib import Path

EXISTING_CLUSTER: dict[str, object] = {
    "seedHosts": ["10.0.0.11:9300", "10.0.0.12:9300"],
    "caCertificateFile": "pki/search-old-ca.pem",
}


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


def _certificate(*, ca: bool) -> tuple[bytes, bytes]:
    """Return a self-signed certificate and its key, both PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "search-old-ca")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def _write_ca(project: Path, content: bytes) -> None:
    (project / "pki").mkdir(exist_ok=True)
    (project / "pki" / "search-old-ca.pem").write_bytes(content)


def _joining_service(
    existing_cluster: dict[str, object] = EXISTING_CLUSTER,
) -> dict[str, object]:
    return elasticsearch_service(
        clusterName="search",
        existingCluster=existing_cluster,
        snapshotRepository=SNAPSHOT_REPOSITORY,
    )


def _edit_baseline(project: Path, firewall: dict[str, object] | None) -> None:
    path = project / "baselines" / "debian-application.yaml"
    baseline = yaml.safe_load(path.read_text(encoding="utf-8"))
    if firewall is None:
        del baseline["spec"]["firewall"]
    else:
        baseline["spec"]["firewall"] = firewall
    path.write_text(yaml.safe_dump(baseline, sort_keys=False), encoding="utf-8")


# S-012-1


def test_s012_1_one_node_service_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", elasticsearch_service())

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document
    assert document["data"]["by_kind"]["Service"] == 1


def test_s012_1_absent_nodes_and_cluster_name_resolve_to_the_server_and_id(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", elasticsearch_service())

    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (service,) = inventory.services
    assert service.as_dict()["elasticsearch"] == {
        "version": "9.2.4",
        "heapMb": 4096,
        "nodes": ["h1"],
        "clusterName": "search-main",
        "users": [],
    }
    assert service.postgresql is None
    assert service.redis is None


def test_s012_1_full_declaration_renders_references_never_passwords(
    tmp_path: Path,
) -> None:
    user = {
        "name": "crm",
        "access": "write",
        "indexPatterns": ["crm-*"],
        "passwordRef": PASSWORD_REF,
    }
    project = write_project(
        tmp_path / "project",
        elasticsearch_service(
            nodes=["h1", "h2"],
            clusterName="search",
            users=[user],
            snapshotRepository=SNAPSHOT_REPOSITORY,
            restoreCheckIndices=["crm-*"],
        ),
        servers=("h1", "h2"),
    )

    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    (service,) = inventory.services
    rendered = service.as_dict()["elasticsearch"]
    assert rendered == {
        "version": "9.2.4",
        "heapMb": 4096,
        "nodes": ["h1", "h2"],
        "clusterName": "search",
        "users": [user],
        "snapshotRepository": SNAPSHOT_REPOSITORY,
        "restoreCheckIndices": ["crm-*"],
    }


def test_users_take_no_plain_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D-9: a password reaches the service only as a hash; none is declared."""
    user = {
        "name": "crm",
        "access": "read",
        "indexPatterns": ["crm-*"],
        "passwordRef": PASSWORD_REF,
        "password": "hunter2",
    }
    project = write_project(
        tmp_path / "project", elasticsearch_service(users=[user])
    )

    _refused_with(project, capsys, "schema_validation_failed")


# S-012-12: each refusal under Validation


@pytest.mark.parametrize("kind", ["postgresql", "redis"])
def test_s012_12_elasticsearch_block_on_another_kind_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    service = elasticsearch_service()
    spec = service["spec"]
    assert isinstance(spec, dict)
    spec["serviceKind"] = kind
    spec[kind] = (
        {"majorVersion": "17", "databases": [{"name": "crm", "owner": "crm",
                                              "passwordRef": PASSWORD_REF}]}
        if kind == "postgresql"
        else {"maxmemoryMb": 256, "appendOnly": True}
    )
    project = write_project(tmp_path / "project", service)

    _refused_with(project, capsys, "schema_validation_failed")


def test_s012_12_elasticsearch_kind_without_its_block_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = elasticsearch_service()
    spec = service["spec"]
    assert isinstance(spec, dict)
    del spec["elasticsearch"]
    project = write_project(tmp_path / "project", service)

    _refused_with(project, capsys, "schema_validation_failed")


@pytest.mark.parametrize("version", ["9.2", "9.2.4-SNAPSHOT", "v9.2.4", "09.2.4"])
def test_s012_12_version_that_is_not_x_y_z_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    project = write_project(
        tmp_path / "project", elasticsearch_service(version=version)
    )

    context = _refused_with(project, capsys, "service_elasticsearch_version")
    assert context["field"] == "spec.elasticsearch.version"


@pytest.mark.parametrize(
    ("nodes", "field"),
    [
        (["h2"], "spec.elasticsearch.nodes"),
        (["h1", "h9"], "spec.elasticsearch.nodes.1"),
        (["h1", "h2", "h1"], "spec.elasticsearch.nodes.2"),
    ],
    ids=["without-server", "undeclared", "duplicate"],
)
def test_s012_12_nodes_are_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    nodes: list[str],
    field: str,
) -> None:
    project = write_project(
        tmp_path / "project",
        elasticsearch_service(nodes=nodes, snapshotRepository=SNAPSHOT_REPOSITORY),
        servers=("h1", "h2"),
    )

    context = _refused_with(project, capsys, "service_elasticsearch_nodes")
    assert context["field"] == field


def test_s012_12_existing_cluster_joins_with_a_ca_certificate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", _joining_service())
    _write_ca(project, _certificate(ca=True)[0])

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


def test_s012_12_existing_cluster_without_cluster_name_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        elasticsearch_service(
            existingCluster=EXISTING_CLUSTER, snapshotRepository=SNAPSHOT_REPOSITORY
        ),
    )
    _write_ca(project, _certificate(ca=True)[0])

    _refused_with(project, capsys, "service_elasticsearch_existing_cluster")


@pytest.mark.parametrize(
    "ca_file",
    ["pki/missing.pem", "../search-old-ca.pem", "/etc/ssl/certs/search-old-ca.pem"],
    ids=["missing", "climbs-out", "absolute"],
)
def test_s012_12_existing_cluster_ca_file_outside_or_missing_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], ca_file: str
) -> None:
    project = write_project(
        tmp_path / "project",
        _joining_service(
            {**EXISTING_CLUSTER, "caCertificateFile": ca_file}
        ),
    )
    (tmp_path / "search-old-ca.pem").write_bytes(_certificate(ca=True)[0])

    context = _refused_with(project, capsys, "service_elasticsearch_existing_cluster")
    assert context["field"] == "spec.elasticsearch.existingCluster.caCertificateFile"


def test_s012_12_existing_cluster_ca_file_that_is_not_a_ca_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", _joining_service())
    _write_ca(project, _certificate(ca=False)[0])

    _refused_with(project, capsys, "service_elasticsearch_existing_cluster")


def test_s012_12_existing_cluster_ca_file_with_its_key_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", _joining_service())
    certificate, key = _certificate(ca=True)
    _write_ca(project, certificate + key)

    context = _refused_with(project, capsys, "service_elasticsearch_existing_cluster")
    assert "private key" in context["message"]


@pytest.mark.parametrize(
    "seed_host", ["203.0.113.11:9300", "h1.example.internal:9300"]
)
def test_s012_12_existing_cluster_seed_host_that_is_a_node_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], seed_host: str
) -> None:
    project = write_project(
        tmp_path / "project",
        _joining_service(
            {**EXISTING_CLUSTER, "seedHosts": [seed_host]}
        ),
    )
    _write_ca(project, _certificate(ca=True)[0])

    context = _refused_with(project, capsys, "service_elasticsearch_existing_cluster")
    assert context["field"] == "spec.elasticsearch.existingCluster.seedHosts.0"


def test_s012_12_server_in_two_elasticsearch_services_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        elasticsearch_service(),
        elasticsearch_service(
            "search-logs",
            server="h2",
            port=9201,
            nodes=["h2", "h1"],
            snapshotRepository=SNAPSHOT_REPOSITORY,
        ),
        servers=("h1", "h2"),
    )

    _refused_with(project, capsys, "service_elasticsearch_server_shared")


def test_s012_12_two_nodes_without_snapshot_repository_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        elasticsearch_service(nodes=["h1", "h2"]),
        servers=("h1", "h2"),
    )

    _refused_with(project, capsys, "service_elasticsearch_snapshot_repository_local")


def test_s012_12_existing_cluster_without_snapshot_repository_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        elasticsearch_service(clusterName="search", existingCluster=EXISTING_CLUSTER),
    )
    _write_ca(project, _certificate(ca=True)[0])

    _refused_with(project, capsys, "service_elasticsearch_snapshot_repository_local")


@pytest.mark.parametrize(
    "name", ["elastic", "cloudfall", "kibana_system", "superuser", "viewer"]
)
def test_s012_12_reserved_user_name_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    user = {
        "name": name,
        "access": "read",
        "indexPatterns": ["crm-*"],
        "passwordRef": PASSWORD_REF,
    }
    project = write_project(
        tmp_path / "project", elasticsearch_service(users=[user])
    )

    context = _refused_with(project, capsys, "service_elasticsearch_user_reserved")
    assert context["field"] == "spec.elasticsearch.users.0.name"


@pytest.mark.parametrize("pattern", ["*", "**", ".security", ".kibana*"])
def test_s012_12_pattern_of_every_or_a_hidden_index_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], pattern: str
) -> None:
    user = {
        "name": "crm",
        "access": "read",
        "indexPatterns": ["crm-*", pattern],
        "passwordRef": PASSWORD_REF,
    }
    project = write_project(
        tmp_path / "project", elasticsearch_service(users=[user])
    )

    context = _refused_with(project, capsys, "service_elasticsearch_pattern")
    assert context["field"] == "spec.elasticsearch.users.0.indexPatterns.1"


def test_restore_check_pattern_of_every_index_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project", elasticsearch_service(restoreCheckIndices=["*"])
    )

    _refused_with(project, capsys, "service_elasticsearch_pattern")


def test_user_declared_twice_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user = {
        "name": "crm",
        "access": "read",
        "indexPatterns": ["crm-*"],
        "passwordRef": PASSWORD_REF,
    }
    project = write_project(
        tmp_path / "project", elasticsearch_service(users=[user, user])
    )

    _refused_with(project, capsys, "service_elasticsearch_user_duplicate")


REMOTE_ACCESS = {"listenAddress": "10.0.0.2", "allowedSources": ["10.0.0.0/24"]}


def test_s012_12_remote_access_validates_with_a_firewall(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project", elasticsearch_service(remoteAccess=REMOTE_ACCESS)
    )

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


def test_s012_12_remote_access_without_a_firewall_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project", elasticsearch_service(remoteAccess=REMOTE_ACCESS)
    )
    _edit_baseline(project, None)

    context = _refused_with(project, capsys, "service_remote_access_unfirewalled")
    assert context["field"] == "spec.elasticsearch.remoteAccess"


def test_s012_12_remote_access_on_a_port_open_to_all_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project", elasticsearch_service(remoteAccess=REMOTE_ACCESS)
    )
    _edit_baseline(
        project,
        {
            "policy": "default-deny",
            "allowedInbound": [
                {"port": 9200, "protocol": "tcp", "description": "search"}
            ],
        },
    )

    _refused_with(project, capsys, "service_remote_access_port_open")


def test_s012_12_remote_access_from_every_address_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        elasticsearch_service(
            remoteAccess={"listenAddress": "10.0.0.2", "allowedSources": ["0.0.0.0/0"]}
        ),
    )

    _refused_with(project, capsys, "service_remote_access_open")


def test_elasticsearch_node_port_taken_by_another_service_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    redis: dict[str, object] = {
        "apiVersion": "cloudfall/v1",
        "kind": "Service",
        "metadata": {"id": "redis-cache"},
        "spec": {
            "serviceKind": "redis",
            "environment": "production",
            "server": "h2",
            "bind": {"address": "127.0.0.1", "port": 9200},
            "redis": {"maxmemoryMb": 256, "appendOnly": True},
            "backup": {
                "directory": "/var/backups/cloudfall/redis-cache",
                "onCalendar": "*-*-* 03:00:00 UTC",
                "retentionDays": 7,
            },
        },
    }
    project = write_project(
        tmp_path / "project",
        elasticsearch_service(
            nodes=["h1", "h2"], snapshotRepository=SNAPSHOT_REPOSITORY
        ),
        redis,
        servers=("h1", "h2"),
    )

    _refused_with(project, capsys, "service_port_conflict")


# S-012-16: supported lines and their end-of-life dates


@pytest.mark.parametrize("version", ["7.17.26", "8.18.0"])
def test_s012_16_unsupported_line_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    project = write_project(
        tmp_path / "project", elasticsearch_service(version=version)
    )

    context = _refused_with(project, capsys, "service_elasticsearch_version")
    assert version in context["message"]


@pytest.mark.parametrize("version", ["8.19.6", "9.0.0", "9.2.4"])
def test_s012_16_supported_lines_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    project = write_project(
        tmp_path / "project", elasticsearch_service(version=version)
    )

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document


def test_s012_16_line_within_90_days_of_its_date_warns_naming_it(
    tmp_path: Path,
) -> None:
    project = write_project(
        tmp_path / "project", elasticsearch_service(version="8.19.6")
    )
    state = validate_config(project, SCHEMAS)
    line = supported_line(ElasticsearchVersion("8.19.6"))
    assert line is not None
    end = line.end_of_life

    long_before = elasticsearch_versions_ending(state, end - datetime.timedelta(91))
    window_opens = elasticsearch_versions_ending(state, end - datetime.timedelta(90))
    after = elasticsearch_versions_ending(state, end + datetime.timedelta(1))

    assert long_before == ()
    (ending,) = window_opens
    assert ending.service.value == "search-main"
    assert ending.line is line
    message = ending.message(end - datetime.timedelta(90))
    assert end.isoformat() in message
    assert "ends on" in message
    (ended,) = after
    assert "ended on" in ended.message(end + datetime.timedelta(1))


def test_s012_16_config_validate_warns_for_8_19(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """8.19's window opened on 2026-10-17, so this holds on any later clock."""
    project = write_project(
        tmp_path / "project", elasticsearch_service(version="8.19.6")
    )

    exit_code, document = _validate(project, capsys)

    assert exit_code == 0, document
    (warning,) = [
        warning
        for warning in document["warnings"]
        if warning["code"] == "ELASTICSEARCH_VERSION_ENDING"
    ]
    assert warning["context"]["endOfLife"] == "2027-01-15"
    assert "2027-01-15" in warning["message"]


def test_s012_16_warning_reads_the_injected_table_not_the_clock(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", elasticsearch_service())
    state = validate_config(project, SCHEMAS)
    lines = (
        ElasticsearchLine(major=9, minor=None, end_of_life=datetime.date(2030, 3, 1)),
    )

    assert elasticsearch_versions_ending(state, datetime.date(2029, 1, 1), lines) == ()
    (ending,) = elasticsearch_versions_ending(state, datetime.date(2030, 1, 1), lines)
    assert "2030-03-01" in ending.message(datetime.date(2030, 1, 1))


def test_supported_lines_are_8_19_and_9() -> None:
    assert [line.label for line in SUPPORTED_LINES] == ["8.19", "9.x"]
    assert supported_line(ElasticsearchVersion("8.18.0")) is None
    assert supported_line(ElasticsearchVersion("10.0.0")) is None


def test_validate_documents_cannot_resolve_a_ca_file(tmp_path: Path) -> None:
    """Resources not read from a project have no root to resolve the CA file in."""
    from cloudfall.domain import RawDocument, SourceLocation  # noqa: PLC0415
    from cloudfall.validation import SchemaCatalog, StateValidator  # noqa: PLC0415

    project = write_project(tmp_path / "project", _joining_service())
    raw = [
        RawDocument(
            content=yaml.safe_load(path.read_text(encoding="utf-8")),
            source=SourceLocation(path=path, document_number=1),
        )
        for path in sorted(project.rglob("*.yaml"))
    ]

    with pytest.raises(ConfigValidationError) as error:
        StateValidator(SchemaCatalog(SCHEMAS)).validate_documents(raw)

    assert error.value.issue.code == "service_elasticsearch_existing_cluster"
