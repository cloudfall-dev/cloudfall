"""What the controller derives and stages for a RabbitMQ cluster of three (S-023, #508).

The inventory, the firewall rules, each node's certificate from the project
CA and the services converge's answer to a cluster without majority are
checked here; the role's own decisions are in
``engine/tests/test_s023_rabbitmq_cluster_role.py``. The converge runs with
a recording step runner that answers the provider playbooks as fresh hosts
would (``elasticsearch_hosts``).
"""

from __future__ import annotations

import datetime as dt
import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from cloudfall.audit import AuditStatus, audit_inventory
from cloudfall.domain import ResourceId
from cloudfall.elasticsearch_converge import (
    DELIVERED_VARIABLE,
    RABBITMQ_DELIVERED_VARIABLE,
)
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import (
    EngineContext,
    ExecutionStep,
    LifecycleError,
    converge_services,
)
from cloudfall.mariadb_replication import REPORT_DIRECTORY_VARIABLE
from cloudfall.observation import load_observations
from cloudfall.pki import ProjectCa, certificate_names, load_or_create_ca
from cloudfall.rabbitmq_access import CA_PATH, CERTIFICATE_PATH, KEY_PATH
from cloudfall.requirements import (
    CertificateRequirement,
    CertificateUsage,
    PortRequirement,
    VolumePlacement,
    VolumeRequirement,
    requirements_of,
)
from cloudfall.secret_store import SecretStore
from cloudfall.secrets import SopsSecretProvider
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID
from elasticsearch_hosts import FakeNodes
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from rabbitmq_project import SCHEMAS, rabbitmq_service, write_project

ROOT = Path(__file__).parents[2]
ENGINE = ROOT / "engine"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"
FIREWALL = ENGINE / "ansible" / "roles" / "cloudfall_firewall" / "templates"
SECRETS = "JOBS_PASSWORD=synthetic-jobs-value\nERLANG_COOKIE=synthetic-cookie\n"
NAMES = ["rabbit@h1", "rabbit@h2", "rabbit@h3"]
ADDRESSES = {"h1": "203.0.113.11", "h2": "203.0.113.12", "h3": "203.0.113.13"}
V6 = {"h1": "2001:db8:11::/64", "h2": "2001:db8:12::/64", "h3": "2001:db8:13::/64"}


def _three() -> dict[str, object]:
    return rabbitmq_service(clientAccess=None)


def _one() -> dict[str, object]:
    return rabbitmq_service(nodes=["h1"], clientAccess=None)


def _inventory(project: Path) -> PlatformInventory:
    return PlatformInventory.from_state(validate_config(project, SCHEMAS))


def _hosts(project: Path) -> dict[str, dict[str, Any]]:
    rendered = json.loads(json.dumps(render_ansible_inventory(_inventory(project))))
    return cast(
        "dict[str, dict[str, Any]]",
        rendered["all"]["children"]["cloudfall_servers"]["hosts"],
    )


# proves: S-023-9
def test_s023_9_each_node_needs_its_ports_from_the_other_two_and_a_certificate(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", _three())

    placed = {
        item.server_id.value: [requirement.spec for requirement in item.requirements]
        for item in requirements_of(_inventory(project))
    }

    for node in ("h1", "h2", "h3"):
        others = tuple(item for item in ("h1", "h2", "h3") if item != node)
        ports = {
            spec.port.value: [source.value for source in spec.sources]
            for spec in placed[node]
            if isinstance(spec, PortRequirement)
        }
        assert ports == {4369: list(others), 25672: list(others)}
        (certificate,) = (
            spec for spec in placed[node] if isinstance(spec, CertificateRequirement)
        )
        assert certificate.usage is CertificateUsage.SERVER_AND_CLIENT
        # The short hostname is the host part of the node's name, which the
        # connecting node checks against the certificate
        assert [name.value for name in certificate.subject_names] == [
            f"{node}.example.internal",
            ADDRESSES[node],
            node,
        ]
        assert (
            certificate.certificate_path,
            certificate.key_path,
            certificate.ca_path,
        ) == (CERTIFICATE_PATH, KEY_PATH, CA_PATH)
        # The copies are on the other nodes (D-18 keeps a grown node's mirror)
        (volume,) = (
            spec for spec in placed[node] if isinstance(spec, VolumeRequirement)
        )
        assert volume.placement is VolumePlacement.EXPENDABLE


def _nftables(ruleset: dict[str, object]) -> str:
    environment = Environment(
        loader=FileSystemLoader(FIREWALL),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    return environment.get_template("nftables.conf.j2").render(
        cloudfall_firewall_ruleset=ruleset
    )


# proves: S-023-9
def test_s023_9_each_ruleset_admits_4369_and_25672_from_the_other_nodes_only(
    tmp_path: Path,
) -> None:
    hosts = _hosts(write_project(tmp_path / "project", _three()))

    for node in ("h1", "h2", "h3"):
        ruleset = hosts[node]["cloudfall_firewall"]
        rules = _nftables(ruleset).splitlines()
        for port in (4369, 25672):
            admitted = sorted(
                line.strip() for line in rules if f"dport {port} " in line
            )
            expected = []
            for other in ("h1", "h2", "h3"):
                if other == node:
                    continue
                label = "node discovery" if port == 4369 else "inter-node"
                comment = f'comment "rabbitmq-main {label}"'
                expected += [
                    f"ip saddr {ADDRESSES[other]}/32 tcp dport {port} accept {comment}",
                    f"ip6 saddr {V6[other]} tcp dport {port} accept {comment}",
                ]
            assert admitted == sorted(expected)
            # No rule admits a RabbitMQ port from every address
            assert all("saddr" in line for line in admitted)


# proves: S-023-8
def test_s023_8_render_carries_every_node_and_converges_the_home_first(
    tmp_path: Path,
) -> None:
    project = write_project(
        tmp_path / "project", rabbitmq_service(server="h2", clientAccess=None)
    )

    hosts = _hosts(project)

    # The home runs first; the other nodes join it one at a time
    assert next(iter(hosts)) == "h2"
    for node in ("h1", "h2", "h3"):
        cluster = hosts[node]["cloudfall_rabbitmq_cluster"]["rabbitmq-main"]
        assert cluster["address"] == ADDRESSES[node]
        assert cluster["nodes"] == [
            {
                "server": item,
                "name": f"rabbit@{item}",
                "hostname": f"{item}.example.internal",
                "address": ADDRESSES[item],
            }
            for item in ("h1", "h2", "h3")
        ]
        assert cluster["caPath"] == CA_PATH.value
        names = [item["name"] for item in hosts[node]["cloudfall_certificates"]]
        assert names == ["rabbitmq"]


# proves: S-023-9
# proves: S-023-19
def test_s023_19_three_nodes_with_client_access_open_each_port_to_its_sources(
    tmp_path: Path,
) -> None:
    sources = ["10.0.0.0/24", "2001:db8:99::/64"]
    project = write_project(
        tmp_path / "project",
        rabbitmq_service(clientAccess={"allowedSources": sources}),
    )

    hosts = _hosts(project)
    placed = {
        item.server_id.value: [requirement.spec for requirement in item.requirements]
        for item in requirements_of(_inventory(project))
    }

    for node in ("h1", "h2", "h3"):
        rules = _nftables(hosts[node]["cloudfall_firewall"]).splitlines()
        client = sorted(line.strip() for line in rules if "dport 5671 " in line)
        comment = 'comment "rabbitmq-main clientAccess"'
        assert client == sorted(
            [
                f"ip saddr 10.0.0.0/24 tcp dport 5671 accept {comment}",
                f"ip6 saddr 2001:db8:99::/64 tcp dport 5671 accept {comment}",
            ]
        )
        others = [item for item in ("h1", "h2", "h3") if item != node]
        for port in (4369, 25672):
            admitted = sorted(
                line.strip() for line in rules if f"dport {port} " in line
            )
            assert all("saddr" in line for line in admitted)
            assert {line.split()[2] for line in admitted} == {
                address
                for other in others
                for address in (f"{ADDRESSES[other]}/32", V6[other])
            }
        # Neither the plain port nor metrics are opened
        assert not any(
            f"dport {port} " in line for line in rules for port in (5672, 15692)
        )
        # Each node takes clients and its peers on its own IPv4
        assert (
            hosts[node]["cloudfall_rabbitmq_client_access"]["rabbitmq-main"]["address"]
            == ADDRESSES[node]
        )
        assert (
            hosts[node]["cloudfall_rabbitmq_cluster"]["rabbitmq-main"]["address"]
            == ADDRESSES[node]
        )
        # One certificate serves the nodes between them and the clients
        (certificate,) = (
            spec for spec in placed[node] if isinstance(spec, CertificateRequirement)
        )
        assert certificate.usage is CertificateUsage.SERVER_AND_CLIENT
        assert [item["name"] for item in hosts[node]["cloudfall_certificates"]] == [
            "rabbitmq"
        ]


# proves: S-023-3
def test_s023_3_one_node_renders_no_cluster_and_no_port(tmp_path: Path) -> None:
    hosts = _hosts(write_project(tmp_path / "project", _one(), servers=("h1",)))

    assert "cloudfall_rabbitmq_cluster" not in hosts["h1"]
    assert "cloudfall_certificates" not in hosts["h1"]
    ports = {
        rule["port"] for rule in hosts["h1"]["cloudfall_firewall"]["allowedInbound"]
    }
    assert ports.isdisjoint({4369, 25672, 5671, 5672})


@dataclass(frozen=True)
class Finished:
    """A step's exit and output."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Recorder:
    """Run each step against fake hosts; keep what services.yml was handed.

    ``refusal``, when given, is what the RabbitMQ role reports from
    ``services.yml`` before the playbook fails.
    """

    refusal: dict[str, str] | None = None
    nodes: FakeNodes = field(default_factory=FakeNodes)
    services: dict[str, Any] = field(default_factory=dict)
    delivered: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)

    def __call__(self, step: ExecutionStep) -> Finished:
        """Answer a provider playbook, or read what services.yml copies."""
        if "--extra-vars" not in step.argv:
            return Finished()
        extra = json.loads(step.argv[step.argv.index("--extra-vars") + 1])
        self.nodes.answer(Path(step.argv[-1]).name, extra)
        if not step.argv[-1].endswith("services.yml"):
            return Finished()
        self.services = extra
        for variable in (DELIVERED_VARIABLE, RABBITMQ_DELIVERED_VARIABLE):
            found = self.delivered.setdefault(variable, {})
            for server, files in extra.get(variable, {}).items():
                for item in files:
                    source = Path(item["source"])
                    assert stat.S_IMODE(source.stat().st_mode) == 0o600
                    found[f"{server}:{item['path']}"] = (
                        source.read_text(encoding="utf-8"),
                        item["mode"],
                    )
        if self.refusal is None:
            return Finished()
        reports = Path(extra[REPORT_DIRECTORY_VARIABLE])
        (reports / f"{self.refusal['service']}.json").write_text(
            json.dumps(self.refusal), encoding="utf-8"
        )
        return Finished(returncode=2)


def _plaintext(source: Path) -> str:
    """Stand in for sops: the test's secret files are not encrypted."""
    return source.read_text(encoding="utf-8")


def _converge(project: Path, recorder: Recorder) -> ProjectCa:
    secrets = project / "secrets" / "production" / "services"
    secrets.mkdir(parents=True)
    (secrets / "rabbitmq-main.env").write_text(SECRETS, encoding="utf-8")
    store = SecretStore(
        project / "secrets", decrypt=_plaintext, encrypt=lambda _, text: text
    )
    ca, _ = load_or_create_ca(store, ResourceId("production"), dt.datetime.now(dt.UTC))
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
    return ca


# proves: S-023-9
def test_s023_9_the_project_ca_signs_each_node_for_both_directions(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", _three())
    recorder = Recorder()

    ca = _converge(project, recorder)

    assert recorder.nodes.hosts["logging-requests.yml"] == "h1:h2:h3"
    delivered = recorder.delivered[RABBITMQ_DELIVERED_VARIABLE]
    assert recorder.delivered[DELIVERED_VARIABLE] == {}
    for node in ("h1", "h2", "h3"):
        pem, mode = delivered[f"{node}:{CERTIFICATE_PATH.value}"]
        assert mode == "0644"
        assert delivered[f"{node}:{CA_PATH.value}"] == (ca.certificate_pem, "0644")
        signed = x509.load_pem_x509_certificate(pem.encode())
        # Each node verifies the other against the project CA, both ways
        signed.verify_directly_issued_by(ca.certificate)
        usages = signed.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
        assert set(usages.value) == {
            ExtendedKeyUsageOID.SERVER_AUTH,
            ExtendedKeyUsageOID.CLIENT_AUTH,
        }
        # The short hostname is what a connecting node checks: rabbit@h2's host
        assert certificate_names(signed) == {
            f"{node}.example.internal",
            ADDRESSES[node],
            node,
        }
        assert signed.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName) == [f"{node}.example.internal", node]
        # The key the node made never left it
        assert (node, "rabbitmq") in recorder.nodes.keys
    assert "PRIVATE KEY" not in json.dumps(recorder.services)
    # The cookie never reaches the inventory or the extra vars
    assert "synthetic-cookie" not in json.dumps(recorder.services)
    assert "synthetic-cookie" not in json.dumps(
        render_ansible_inventory(_inventory(project))
    )


# proves: S-023-11
def test_s023_11_a_converge_without_majority_answers_precondition(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", _three())
    message = (
        "RabbitMQ service rabbitmq-main has no majority: of its members "
        "rabbit@h1, rabbit@h2, rabbit@h3 only rabbit@h1 run"
    )
    recorder = Recorder(
        refusal={
            "service": "rabbitmq-main",
            "server": "h1",
            "outcome": "refused",
            "code": "service_rabbitmq_no_majority",
            "message": message,
        }
    )

    with pytest.raises(LifecycleError) as error:
        _converge(project, recorder)

    # Not the failed playbook: the role's refusal, which the converge command
    # answers with PRECONDITION
    assert error.value.code == "service_rabbitmq_no_majority"
    assert error.value.detail == message
    assert error.value.context["server"] == "h1"


def test_one_node_runs_with_no_report_directory(tmp_path: Path) -> None:
    project = write_project(tmp_path / "project", _one(), servers=("h1",))
    recorder = Recorder()

    _converge(project, recorder)

    assert REPORT_DIRECTORY_VARIABLE not in recorder.services
    assert RABBITMQ_DELIVERED_VARIABLE not in recorder.services
    assert "logging-requests.yml" not in recorder.nodes.playbooks


def _audit(
    tmp_path: Path, evidence: dict[str, object], service: dict[str, object]
) -> list[dict[str, object]]:
    project = write_project(tmp_path / "project", service)
    observations = tmp_path / "observed"
    observations.mkdir()
    document = json.loads((COMPLIANT / "h1.json").read_text(encoding="utf-8"))
    document["spec"]["rabbitmqCluster"] = [evidence]
    (observations / "h1.json").write_text(json.dumps(document), encoding="utf-8")
    report = audit_inventory(
        _inventory(project), load_observations(observations, SCHEMAS)
    )
    (server,) = (item for item in report.servers if item.server_id == "h1")
    return [
        {
            "status": check.status,
            "observed": check.observed,
            "message": check.message,
        }
        for check in server.checks
        if check.check == "rabbitmq.cluster[rabbitmq-main]"
    ]


def _members(running: list[str]) -> dict[str, object]:
    return {
        "service": "rabbitmq-main",
        "available": True,
        "members": NAMES,
        "running": running,
    }


# proves: S-023-8
def test_s023_8_audit_finds_three_running_members(tmp_path: Path) -> None:
    (found,) = _audit(tmp_path, _members(NAMES), _three())

    assert found["status"] is AuditStatus.COMPLIANT


# proves: S-023-11
def test_s023_11_audit_reports_a_stopped_node_as_missing(tmp_path: Path) -> None:
    (found,) = _audit(tmp_path, _members(NAMES[:1] + NAMES[2:]), _three())

    assert found["status"] is AuditStatus.DRIFT
    assert found["message"] == "missing nodes: rabbit@h2"
    assert cast("dict[str, object]", found["observed"])["majority"] is True


# proves: S-023-11
def test_s023_11_audit_reports_the_cluster_without_majority(tmp_path: Path) -> None:
    (found,) = _audit(tmp_path, _members(NAMES[:1]), _three())

    assert found["status"] is AuditStatus.DRIFT
    message = str(found["message"])
    assert message.startswith("the cluster has no majority: 1 of 3 members run")
    assert "missing nodes: rabbit@h2, rabbit@h3" in message


def test_audit_is_unknown_when_the_members_were_not_read(tmp_path: Path) -> None:
    (found,) = _audit(
        tmp_path, {"service": "rabbitmq-main", "available": False}, _three()
    )

    assert found["status"] is AuditStatus.UNKNOWN


def test_one_node_has_no_cluster_check(tmp_path: Path) -> None:
    project = write_project(tmp_path / "project", _one(), servers=("h1",))
    observations = tmp_path / "observed"
    observations.mkdir()
    document = json.loads((COMPLIANT / "h1.json").read_text(encoding="utf-8"))
    (observations / "h1.json").write_text(json.dumps(document), encoding="utf-8")

    report = audit_inventory(
        _inventory(project), load_observations(observations, SCHEMAS)
    )

    assert not any(
        check.check.startswith("rabbitmq.cluster")
        for server in report.servers
        for check in server.checks
    )
