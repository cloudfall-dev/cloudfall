"""What the nodes of an Elasticsearch cluster need, and how a converge meets it.

S-012, #312.

A second node needs the transport port open to the first node only and a
transport certificate from the project CA; every node needs its data
volume. The converge runs here with a recording step runner that answers
the provider playbooks as fresh nodes would (``elasticsearch_hosts``).
"""

from __future__ import annotations

import datetime
import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from cloudfall.cli import main
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import (
    EngineContext,
    ExecutionStep,
    LifecycleError,
    converge_services,
)
from cloudfall.pki import ProjectCa, certificate_names, load_or_create_ca
from cloudfall.requirements import (
    CertificateRequirement,
    PortRequirement,
    Requirement,
    VolumeRequirement,
    requirements_of,
)
from cloudfall.secret_store import SecretStore
from cloudfall.secrets import SopsSecretProvider
from cloudfall.validation import validate_config
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
from elasticsearch_hosts import FakeNodes
from elasticsearch_project import (
    SCHEMAS,
    SNAPSHOT_REPOSITORY,
    elasticsearch_service,
    write_project,
)

ROOT = Path(__file__).parents[2]
ENGINE = ROOT / "engine"
ACCOUNTS = (
    "CLOUDFALL_PASSWORD=synthetic-cloudfall\nELASTIC_PASSWORD=synthetic-elastic\n"
)
GIBIBYTE = 1024**3


def _two_nodes(tmp_path: Path) -> Path:
    """Write S-012-5's project: the service grown to h1 and h2."""
    return write_project(
        tmp_path / "project",
        elasticsearch_service(
            nodes=["h1", "h2"], snapshotRepository=SNAPSHOT_REPOSITORY
        ),
        servers=("h1", "h2"),
    )


def _inventory(project: Path) -> PlatformInventory:
    return PlatformInventory.from_state(validate_config(project, SCHEMAS))


def _placed(project: Path) -> dict[str, dict[type, Requirement]]:
    return {
        server.server_id.value: {type(item.spec): item for item in server.requirements}
        for server in requirements_of(_inventory(project))
    }


# proves: S-012-5
def test_s012_5_each_node_admits_the_transport_from_the_other_node_only(
    tmp_path: Path,
) -> None:
    placed = _placed(_two_nodes(tmp_path))

    assert set(placed) == {"h1", "h2"}
    for node, other in (("h1", "h2"), ("h2", "h1")):
        port = cast("PortRequirement", placed[node][PortRequirement].spec)
        assert port.as_dict() == {"port": 9300, "protocol": "tcp", "sources": [other]}
        assert placed[node][PortRequirement].owner.as_dict() == {
            "kind": "Service",
            "id": "search-main",
        }


# proves: S-012-5
def test_s012_5_each_node_needs_a_transport_certificate_for_its_names(
    tmp_path: Path,
) -> None:
    placed = _placed(_two_nodes(tmp_path))

    for node, address in (("h1", "203.0.113.11"), ("h2", "203.0.113.12")):
        certificate = cast(
            "CertificateRequirement", placed[node][CertificateRequirement].spec
        )
        assert certificate.name.value == "elasticsearch-transport"
        assert certificate.as_dict() == {
            "usage": "server-and-client",
            "subjectNames": [f"{node}.example.internal", address],
            "certificatePath": "/etc/elasticsearch/certs/transport.crt",
            "keyPath": "/etc/elasticsearch/certs/transport.key",
            "caPath": "/etc/elasticsearch/certs/ca.crt",
        }


def test_each_node_has_its_data_volume_mirrored_only_as_a_cluster_of_one(
    tmp_path: Path,
) -> None:
    one = _placed(write_project(tmp_path / "one", elasticsearch_service()))
    two = _placed(_two_nodes(tmp_path))

    volume = cast("VolumeRequirement", one["h1"][VolumeRequirement].spec)
    assert volume.as_dict() == {
        "path": "/var/lib/elasticsearch",
        "minimumBytes": 10 * GIBIBYTE,
        "placement": "mirrored",
        "units": ["elasticsearch.service"],
        "pool": "data",
        "volumeGroup": "vg_data",
        "logicalVolume": "lv_elasticsearch",
        "type": "raid1",
        "physicalVolumes": ["/dev/nvme0n1p5", "/dev/nvme1n1p5"],
    }
    assert PortRequirement not in one["h1"]
    assert CertificateRequirement not in one["h1"]
    for node in ("h1", "h2"):
        grown = cast("VolumeRequirement", two[node][VolumeRequirement].spec)
        assert (grown.placement.value, grown.volume_type) == ("expendable", "linear")
        assert grown.physical_volumes[0].value == "/dev/nvme0n1p5"


def _validate(project: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    exit_code = main(
        ["config", "validate", "--project", str(project), "--schemas", str(SCHEMAS)]
    )
    document: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert exit_code == 80, document
    context: dict[str, Any] = document["error"]["context"]
    return context


def _edit(path: Path, change: dict[str, object]) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["spec"].update(change)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _pools(project: Path, pools: list[dict[str, str]]) -> None:
    path = project / "baselines" / "debian-application.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if pools:
        document["spec"]["storage"]["pools"] = pools
    else:
        del document["spec"]["storage"]["pools"]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("pools", "code"),
    [
        ([], "volume_requirement_pool_missing"),
        (
            [
                {"id": "data", "volumeGroup": "vg_data", "primary": "/dev/sda5"},
                {"id": "more", "volumeGroup": "vg_more", "primary": "/dev/sdb5"},
            ],
            "volume_requirement_pool_ambiguous",
        ),
        (
            [{"id": "data", "volumeGroup": "vg_data", "primary": "/dev/sda5"}],
            "volume_requirement_pool_unmirrored",
        ),
    ],
    ids=["no-pool", "two-pools", "one-disk-for-one-node"],
)
def test_a_node_whose_volume_has_nowhere_to_live_fails_validation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    pools: list[dict[str, str]],
    code: str,
) -> None:
    project = write_project(tmp_path / "project", elasticsearch_service())
    _pools(project, pools)

    context = _validate(project, capsys)

    assert context["code"] == code
    assert "Server/h1" in context["message"]


def test_a_cluster_of_two_needs_no_second_disk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _two_nodes(tmp_path)
    _pools(project, [{"id": "data", "volumeGroup": "vg_data", "primary": "/dev/sda5"}])

    assert main(
        ["config", "validate", "--project", str(project), "--schemas", str(SCHEMAS)]
    ) == 0, capsys.readouterr().out


def test_a_node_without_an_ipv4_fails_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _two_nodes(tmp_path)
    path = project / "servers" / "h2.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    del document["spec"]["network"]
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    context = _validate(project, capsys)

    assert context["code"] == "service_elasticsearch_node_unaddressed"
    assert "node Server/h2 has a name for its address" in context["message"]


@dataclass(frozen=True)
class Finished:
    """A step that exited 0 and printed nothing."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Recorder:
    """Run each step against fake nodes; keep what services.yml was handed."""

    nodes: FakeNodes = field(default_factory=FakeNodes)
    services: dict[str, Any] = field(default_factory=dict)
    delivered: dict[str, str] = field(default_factory=dict)
    steps: list[ExecutionStep] = field(default_factory=list)

    def __call__(self, step: ExecutionStep) -> Finished:
        """Answer a provider playbook, or read the files services.yml copies."""
        self.steps.append(step)
        if "--extra-vars" not in step.argv:
            return Finished()
        extra = json.loads(step.argv[step.argv.index("--extra-vars") + 1])
        self.nodes.answer(Path(step.argv[-1]).name, extra)
        if step.argv[-1].endswith("services.yml"):
            self.services = extra
            for server, files in extra.get(
                "cloudfall_services_delivered_files", {}
            ).items():
                for item in files:
                    source = Path(item["source"])
                    assert stat.S_IMODE(source.stat().st_mode) == 0o600
                    self.delivered[f"{server}:{item['path']}"] = source.read_text(
                        encoding="utf-8"
                    )
        return Finished()


def _plaintext(source: Path) -> str:
    """Stand in for sops: the test's secret files are not encrypted."""
    return source.read_text(encoding="utf-8")


def _store(project: Path) -> SecretStore:
    return SecretStore(
        project / "secrets", decrypt=_plaintext, encrypt=lambda _, text: text
    )


def _converge(project: Path) -> tuple[Recorder, ProjectCa]:
    accounts = project / "secrets" / "production" / "services" / "search-main.env"
    accounts.parent.mkdir(parents=True)
    accounts.write_text(ACCOUNTS, encoding="utf-8")
    from cloudfall.domain import ResourceId  # noqa: PLC0415 - one use

    ca, created = load_or_create_ca(
        _store(project), ResourceId("production"), datetime.datetime.now(datetime.UTC)
    )
    assert created
    recorder = Recorder()
    converge_services(
        EngineContext(
            project_directory=project,
            schema_directory=SCHEMAS,
            engine_directory=ENGINE,
            inventory_file=project / "tmp" / "ansible-inventory.json",
            run=recorder,
        ),
        SopsSecretProvider(secrets_directory=project / "secrets", decrypt=_plaintext),
    )
    return recorder, ca


# proves: S-012-5
def test_s012_5_each_transport_certificate_chains_to_the_project_ca(
    tmp_path: Path,
) -> None:
    """The nodes make their keys; the project CA signs both names and both usages."""
    recorder, ca = _converge(_two_nodes(tmp_path))

    assert recorder.nodes.playbooks == [
        "logging-volumes.yml",
        "logging-requests.yml",
        "services.yml",
    ]
    assert recorder.nodes.hosts == {
        "logging-volumes.yml": "h1:h2",
        "logging-requests.yml": "h1:h2",
    }
    for node, address in (("h1", "203.0.113.11"), ("h2", "203.0.113.12")):
        signed = x509.load_pem_x509_certificate(
            recorder.delivered[f"{node}:/etc/elasticsearch/certs/transport.crt"].encode()
        )
        signed.verify_directly_issued_by(ca.certificate)
        assert recorder.delivered[f"{node}:/etc/elasticsearch/certs/ca.crt"] == (
            ca.certificate_pem
        )
        assert certificate_names(signed) == {f"{node}.example.internal", address}
        usages = signed.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        assert set(usages.value) == {
            ExtendedKeyUsageOID.SERVER_AUTH,
            ExtendedKeyUsageOID.CLIENT_AUTH,
        }
        # Signed for the key the node made, which never left it.
        key = recorder.nodes.keys[node, "elasticsearch-transport"]
        assert signed.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ) == key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    rendered = json.dumps(recorder.services)
    assert "PRIVATE KEY" not in rendered
    assert all(
        not Path(item["source"]).exists()
        for files in recorder.services["cloudfall_services_delivered_files"].values()
        for item in files
    )


def test_the_converge_creates_each_node_volume_it_decided(tmp_path: Path) -> None:
    recorder, _ = _converge(_two_nodes(tmp_path))

    actions = recorder.services["cloudfall_volume_actions"]
    assert set(actions) == {"h1", "h2"}
    for node in ("h1", "h2"):
        (action,) = actions[node]
        assert (action["name"], action["action"], action["type"], action["path"]) == (
            "elasticsearch",
            "create",
            "linear",
            "/var/lib/elasticsearch",
        )
        assert action["units"] == ["elasticsearch.service"]


def test_a_volume_the_provider_refuses_stops_before_services(tmp_path: Path) -> None:
    project = write_project(tmp_path / "project", elasticsearch_service())
    accounts = project / "secrets" / "production" / "services" / "search-main.env"
    accounts.parent.mkdir(parents=True)
    accounts.write_text(ACCOUNTS, encoding="utf-8")

    def refused(step: ExecutionStep) -> Finished:
        if "--extra-vars" in step.argv and step.argv[-1].endswith("services.yml"):
            pytest.fail("services.yml ran after a refused volume")
        return Finished()

    with pytest.raises(LifecycleError) as error:
        converge_services(
            EngineContext(
                project_directory=project,
                schema_directory=SCHEMAS,
                engine_directory=ENGINE,
                inventory_file=project / "tmp" / "ansible-inventory.json",
                run=refused,
            ),
            SopsSecretProvider(
                secrets_directory=project / "secrets", decrypt=_plaintext
            ),
        )

    assert error.value.code == "volume_refused"
    assert "h1 wrote no volume report" in error.value.detail
