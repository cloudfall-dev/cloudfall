"""What the controller derives and stages for RabbitMQ ``clientAccess`` (S-023, #507).

The rendered firewall, each node's certificate from the project CA and the
address its TLS listener takes are checked here; the role's settings are in
``engine/tests/test_s023_rabbitmq_client_access_role.py``. The converge runs
with a recording step runner that answers the provider playbooks as a fresh
host would (``elasticsearch_hosts``). A client's TLS login from another
address needs a live host and is left to a proving run.
"""

from __future__ import annotations

import datetime as dt
import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from cloudfall.audit import audit_inventory
from cloudfall.domain import ResourceId
from cloudfall.elasticsearch_converge import (
    DELIVERED_VARIABLE,
    RABBITMQ_DELIVERED_VARIABLE,
)
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import EngineContext, ExecutionStep, converge_services
from cloudfall.observation import load_observations
from cloudfall.pki import ProjectCa, certificate_names, load_or_create_ca
from cloudfall.rabbitmq_access import CA_PATH, CERTIFICATE_PATH, KEY_PATH
from cloudfall.requirements import (
    CertificateRequirement,
    CertificateUsage,
    PortRequirement,
    requirements_of,
)
from cloudfall.secret_store import SecretStore
from cloudfall.secrets import SopsSecretProvider
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cryptography import x509
from elasticsearch_hosts import FakeNodes
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from rabbitmq_project import SCHEMAS, rabbitmq_service, write_project

ROOT = Path(__file__).parents[2]
ENGINE = ROOT / "engine"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"
FIREWALL = ENGINE / "ansible" / "roles" / "cloudfall_firewall" / "templates"
SECRETS = "JOBS_PASSWORD=synthetic-jobs-value\nERLANG_COOKIE=synthetic-cookie\n"
RABBITMQ_PORTS = (4369, 5671, 5672, 15692, 25672)
COMMENT = 'comment "rabbitmq-main clientAccess"'


def _with_access(*sources: str) -> dict[str, object]:
    return rabbitmq_service(
        nodes=["h1"], clientAccess={"allowedSources": list(sources)}
    )


def _project(tmp_path: Path, service: dict[str, object]) -> Path:
    return write_project(tmp_path / "project", service, servers=("h1",))


def _inventory(project: Path) -> PlatformInventory:
    return PlatformInventory.from_state(validate_config(project, SCHEMAS))


def _host(project: Path) -> dict[str, Any]:
    rendered = json.loads(json.dumps(render_ansible_inventory(_inventory(project))))
    hosts = cast(
        "dict[str, dict[str, Any]]",
        rendered["all"]["children"]["cloudfall_servers"]["hosts"],
    )
    return hosts["h1"]


def _nftables(ruleset: dict[str, object]) -> list[str]:
    environment = Environment(
        loader=FileSystemLoader(FIREWALL),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    rendered = environment.get_template("nftables.conf.j2").render(
        cloudfall_firewall_ruleset=ruleset
    )
    return [line.strip() for line in rendered.splitlines()]


# proves: S-023-19
def test_s023_19_the_ruleset_admits_5671_from_the_allowed_sources_only(
    tmp_path: Path,
) -> None:
    host = _host(_project(tmp_path, _with_access("10.0.0.0/24")))

    rules = _nftables(host["cloudfall_firewall"])

    assert [line for line in rules if "dport 5671" in line] == [
        f"ip saddr 10.0.0.0/24 tcp dport 5671 accept {COMMENT}"
    ]
    # No rule admits a RabbitMQ port from every address, nor the plain port
    for port in RABBITMQ_PORTS:
        for line in rules:
            if f"dport {port} " in line:
                assert "saddr" in line
    assert not any("dport 5672 " in line for line in rules)


# proves: S-023-19
def test_s023_19_an_ipv6_source_is_admitted_as_such(tmp_path: Path) -> None:
    host = _host(_project(tmp_path, _with_access("10.0.0.0/24", "2001:db8:99::/64")))

    rules = [line for line in _nftables(host["cloudfall_firewall"]) if "5671" in line]

    assert rules == [
        f"ip saddr 10.0.0.0/24 tcp dport 5671 accept {COMMENT}",
        f"ip6 saddr 2001:db8:99::/64 tcp dport 5671 accept {COMMENT}",
    ]


# proves: S-023-19
def test_s023_19_the_node_takes_clients_on_its_ipv4_over_tls(tmp_path: Path) -> None:
    host = _host(_project(tmp_path, _with_access("10.0.0.0/24")))

    assert host["cloudfall_rabbitmq_client_access"] == {
        "rabbitmq-main": {
            "address": "203.0.113.11",
            "port": 5671,
            "certificatePath": CERTIFICATE_PATH.value,
            "keyPath": KEY_PATH.value,
            "caPath": CA_PATH.value,
        }
    }
    assert [item["name"] for item in host["cloudfall_certificates"]] == ["rabbitmq"]


# proves: S-023-3
def test_s023_3_without_client_access_nothing_is_rendered_for_it(
    tmp_path: Path,
) -> None:
    host = _host(_project(tmp_path, rabbitmq_service(nodes=["h1"], clientAccess=None)))

    assert "cloudfall_rabbitmq_client_access" not in host
    assert "cloudfall_certificates" not in host
    ports = {rule["port"] for rule in host["cloudfall_firewall"]["allowedInbound"]}
    assert ports.isdisjoint(RABBITMQ_PORTS)


# proves: S-023-19
def test_s023_19_each_node_needs_its_certificate_and_no_port_requirement(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, _with_access("10.0.0.0/24"))

    (server,) = requirements_of(_inventory(project))
    specs = [item.spec for item in server.requirements]

    (certificate,) = (
        spec for spec in specs if isinstance(spec, CertificateRequirement)
    )
    assert certificate.usage is CertificateUsage.SERVER_AND_CLIENT
    assert [name.value for name in certificate.subject_names] == [
        "h1.example.internal",
        "203.0.113.11",
    ]
    assert (
        certificate.certificate_path,
        certificate.key_path,
        certificate.ca_path,
    ) == (CERTIFICATE_PATH, KEY_PATH, CA_PATH)
    # The sources are networks, not servers: the firewall alone opens 5671
    assert not any(isinstance(spec, PortRequirement) for spec in specs)


@dataclass(frozen=True)
class Finished:
    """A step that exited 0 and printed nothing."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Recorder:
    """Run each step against a fake host; keep what services.yml was handed."""

    nodes: FakeNodes = field(default_factory=FakeNodes)
    services: dict[str, Any] = field(default_factory=dict)
    delivered: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)

    def __call__(self, step: ExecutionStep) -> Finished:
        """Answer a provider playbook, or read what services.yml copies."""
        if "--extra-vars" not in step.argv:
            return Finished()
        extra = json.loads(step.argv[step.argv.index("--extra-vars") + 1])
        self.nodes.answer(Path(step.argv[-1]).name, extra)
        if step.argv[-1].endswith("services.yml"):
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
        return Finished()


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


# proves: S-023-19
def test_s023_19_the_project_ca_signs_the_certificate_a_client_verifies(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, _with_access("10.0.0.0/24"))
    recorder = Recorder()

    ca = _converge(project, recorder)

    assert recorder.nodes.hosts["logging-requests.yml"] == "h1"
    delivered = recorder.delivered[RABBITMQ_DELIVERED_VARIABLE]
    # Handed to the RabbitMQ role alone
    assert recorder.delivered[DELIVERED_VARIABLE] == {}
    assert set(delivered) == {
        f"h1:{CERTIFICATE_PATH.value}",
        f"h1:{CA_PATH.value}",
    }
    pem, mode = delivered[f"h1:{CERTIFICATE_PATH.value}"]
    assert mode == "0644"
    assert delivered[f"h1:{CA_PATH.value}"] == (ca.certificate_pem, "0644")
    signed = x509.load_pem_x509_certificate(pem.encode())
    # A client from an allowed source verifies the node at its IPv4
    signed.verify_directly_issued_by(ca.certificate)
    assert certificate_names(signed) == {"h1.example.internal", "203.0.113.11"}
    # The key the node made never left it
    assert ("h1", "rabbitmq") in recorder.nodes.keys
    assert "PRIVATE KEY" not in json.dumps(recorder.services)


def test_one_node_without_client_access_requests_no_certificate(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, rabbitmq_service(nodes=["h1"], clientAccess=None))
    recorder = Recorder()

    _converge(project, recorder)

    assert "logging-requests.yml" not in recorder.nodes.playbooks
    assert RABBITMQ_DELIVERED_VARIABLE not in recorder.services


# proves: S-023-19
def test_s023_19_audit_expects_the_client_access_rule(tmp_path: Path) -> None:
    project = _project(tmp_path, _with_access("10.0.0.0/24"))
    observations = tmp_path / "observed"
    observations.mkdir()
    document = json.loads((COMPLIANT / "h1.json").read_text(encoding="utf-8"))
    (observations / "h1.json").write_text(json.dumps(document), encoding="utf-8")

    report = audit_inventory(
        _inventory(project), load_observations(observations, SCHEMAS)
    )

    (server,) = (item for item in report.servers if item.server_id == "h1")
    (check,) = (
        item for item in server.checks if item.check == "firewall.allowedInbound"
    )
    assert "tcp/5671 from 10.0.0.0/24" in cast("list[str]", check.desired)
