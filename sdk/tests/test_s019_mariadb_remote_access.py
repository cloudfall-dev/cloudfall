"""What the controller prepares for a MariaDB server with ``remoteAccess`` (S-019-11).

The converge runs with a recording step runner that answers the provider
playbooks as a fresh host would (``elasticsearch_hosts``): the server makes
its key and a signing request, and the project CA signs the server
certificate for its hostname and addresses, the listen address among them.
The certificate and the CA reach the MariaDB role apart from an
Elasticsearch node's files. A real TLS login from another host needs two
hosts and is left to a live proving run.
"""

from __future__ import annotations

import datetime
import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from cloudfall.audit import AuditCheck, AuditStatus, audit_inventory
from cloudfall.domain import IpNetworkCidr, ResourceId
from cloudfall.elasticsearch_converge import (
    DELIVERED_VARIABLE,
    MARIADB_DELIVERED_VARIABLE,
)
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import EngineContext, ExecutionStep, converge_services
from cloudfall.mariadb import account_host
from cloudfall.observation import load_observations
from cloudfall.pki import ProjectCa, certificate_names, load_or_create_ca
from cloudfall.requirements import (
    MARIADB_CA_PATH,
    MARIADB_CERTIFICATE_PATH,
    MARIADB_KEY_PATH,
)
from cloudfall.secret_store import SecretStore
from cloudfall.secrets import SopsSecretProvider
from cloudfall.validation import validate_config
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
from elasticsearch_hosts import FakeNodes
from elasticsearch_project import elasticsearch_service
from mariadb_project import SCHEMAS, mariadb_service, primary_replication, write_project

ROOT = Path(__file__).parents[2]
ENGINE = ROOT / "engine"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"
REMOTE_ACCESS = {"listenAddress": "10.0.0.2", "allowedSources": ["10.0.0.0/24"]}
SECRETS = (
    "BILLING_PASSWORD=synthetic-billing-value\n"
    "REPLICATION_PASSWORD=synthetic-replication-value-0123\n"
)
SEARCH_SECRETS = (
    "CLOUDFALL_PASSWORD=synthetic-cloudfall\nELASTIC_PASSWORD=synthetic-elastic\n"
)


@dataclass(frozen=True)
class Finished:
    """A step that exited 0 and printed nothing."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Recorder:
    """Run each step against fake hosts; keep what services.yml was handed."""

    nodes: FakeNodes = field(default_factory=FakeNodes)
    services: dict[str, Any] = field(default_factory=dict)
    delivered: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)

    def __call__(self, step: ExecutionStep) -> Finished:
        """Answer a provider playbook, or read the files services.yml copies."""
        if "--extra-vars" not in step.argv:
            return Finished()
        extra = json.loads(step.argv[step.argv.index("--extra-vars") + 1])
        self.nodes.answer(Path(step.argv[-1]).name, extra)
        if step.argv[-1].endswith("services.yml"):
            self.services = extra
            for variable in (DELIVERED_VARIABLE, MARIADB_DELIVERED_VARIABLE):
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


def _converge(project: Path, *, search: bool = False) -> tuple[Recorder, ProjectCa]:
    secrets = project / "secrets" / "production" / "services"
    secrets.mkdir(parents=True)
    (secrets / "mariadb-main.env").write_text(SECRETS, encoding="utf-8")
    if search:
        (secrets / "search-main.env").write_text(SEARCH_SECRETS, encoding="utf-8")
    store = SecretStore(
        project / "secrets", decrypt=_plaintext, encrypt=lambda _, text: text
    )
    ca, created = load_or_create_ca(
        store, ResourceId("production"), datetime.datetime.now(datetime.UTC)
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


# proves: S-019-11
def test_s019_11_the_project_ca_signs_the_server_certificate_for_the_listen_address(
    tmp_path: Path,
) -> None:
    project = write_project(
        tmp_path / "project", mariadb_service(remoteAccess=REMOTE_ACCESS)
    )

    recorder, ca = _converge(project)

    assert recorder.nodes.playbooks == [
        "logging-volumes.yml",
        "logging-requests.yml",
        "services.yml",
    ]
    assert recorder.nodes.hosts["logging-requests.yml"] == "h1"
    delivered = recorder.delivered[MARIADB_DELIVERED_VARIABLE]
    assert set(delivered) == {
        f"h1:{MARIADB_CERTIFICATE_PATH.value}",
        f"h1:{MARIADB_CA_PATH.value}",
    }
    pem, mode = delivered[f"h1:{MARIADB_CERTIFICATE_PATH.value}"]
    assert mode == "0644"
    assert delivered[f"h1:{MARIADB_CA_PATH.value}"] == (ca.certificate_pem, "0644")
    signed = x509.load_pem_x509_certificate(pem.encode())
    # A client from an allowed source verifies the server against the project CA
    signed.verify_directly_issued_by(ca.certificate)
    assert certificate_names(signed) == {
        "h1.example.internal",
        "203.0.113.11",
        "10.0.0.2",
    }
    usages = signed.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert set(usages.value) == {ExtendedKeyUsageOID.SERVER_AUTH}
    # Signed for the key the server made, which never left it
    key = recorder.nodes.keys["h1", "mariadb"]
    assert signed.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ) == key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    requests = recorder.nodes.hosts
    assert requests["logging-volumes.yml"] == "h1"
    rendered = json.dumps(recorder.services)
    assert "PRIVATE KEY" not in rendered
    assert MARIADB_KEY_PATH.value not in rendered
    # Nothing for the Elasticsearch role, and the staged files are gone
    assert DELIVERED_VARIABLE not in recorder.services
    assert all(
        not Path(item["source"]).exists()
        for files in recorder.services[MARIADB_DELIVERED_VARIABLE].values()
        for item in files
    )


def test_an_elasticsearch_node_and_a_mariadb_server_get_their_own_files(
    tmp_path: Path,
) -> None:
    search = elasticsearch_service(
        remoteAccess={"listenAddress": "10.0.0.2", "allowedSources": ["10.0.0.0/24"]}
    )
    project = write_project(
        tmp_path / "project", mariadb_service(remoteAccess=REMOTE_ACCESS), search
    )

    recorder, _ = _converge(project, search=True)

    # One request run for both, then each role is handed its own files only
    assert recorder.nodes.playbooks.count("logging-requests.yml") == 1
    assert {key.split(":", 1)[1] for key in recorder.delivered[DELIVERED_VARIABLE]} == {
        "/etc/elasticsearch/certs/transport.crt",
        "/etc/elasticsearch/certs/ca.crt",
    }
    assert set(recorder.delivered[MARIADB_DELIVERED_VARIABLE]) == {
        f"h1:{MARIADB_CERTIFICATE_PATH.value}",
        f"h1:{MARIADB_CA_PATH.value}",
    }


def test_no_certificate_is_asked_for_without_remote_access_or_replication(
    tmp_path: Path,
) -> None:
    project = write_project(tmp_path / "project", mariadb_service())

    recorder, _ = _converge(project)

    assert "logging-requests.yml" not in recorder.nodes.playbooks
    assert MARIADB_DELIVERED_VARIABLE not in recorder.services


# proves: S-019-12
def test_s019_12_a_primary_with_remote_access_gets_one_certificate_for_both(
    tmp_path: Path,
) -> None:
    service = mariadb_service(
        remoteAccess=REMOTE_ACCESS, replication=primary_replication()
    )
    project = write_project(tmp_path / "project", service)

    recorder, _ = _converge(project)

    # Replication converges now (#423): the server makes its key, and the
    # project CA signs one certificate for its client and its replicas
    assert recorder.nodes.playbooks.count("logging-requests.yml") == 1
    assert set(recorder.delivered[MARIADB_DELIVERED_VARIABLE]) == {
        f"h1:{MARIADB_CERTIFICATE_PATH.value}",
        f"h1:{MARIADB_CA_PATH.value}",
    }


@pytest.mark.parametrize(
    ("source", "host"),
    [
        ("10.0.0.0/24", "10.0.0.0/255.255.255.0"),
        ("10.0.0.0/8", "10.0.0.0/255.0.0.0"),
        ("192.0.2.7/32", "192.0.2.7"),
        ("2001:db8::5/128", "2001:db8::5"),
        ("2001:db8::/64", None),
    ],
)
def test_s019_11_each_allowed_source_is_an_account_host_mariadb_matches(
    source: str, host: str | None
) -> None:
    assert account_host(IpNetworkCidr(source)) == host


def _audit(
    tmp_path: Path, users: list[str], managed: list[str]
) -> dict[str, AuditCheck]:
    project = write_project(
        tmp_path / "project", mariadb_service(remoteAccess=REMOTE_ACCESS)
    )
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    observations = tmp_path / "observed"
    observations.mkdir()
    document = json.loads((COMPLIANT / "h1.json").read_text(encoding="utf-8"))
    document["spec"]["mariadbAccounts"] = [
        {
            "service": "mariadb-main",
            "available": True,
            "users": ["root@localhost", *users],
            "managedUsers": managed,
            "lockedUsers": [],
            "databases": ["mysql", "crm", "billing"],
            "managedDatabases": ["crm", "billing"],
        }
    ]
    (observations / "h1.json").write_text(json.dumps(document), encoding="utf-8")
    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))
    (server,) = (item for item in report.servers if item.server_id == "h1")
    return {item.check: item for item in server.checks}


# proves: S-019-11
def test_s019_11_audit_counts_the_tls_account_and_the_source_rule_as_declared(
    tmp_path: Path,
) -> None:
    accounts = ["crm@localhost", "billing@localhost", "billing@10.0.0.0/255.255.255.0"]

    checks = _audit(tmp_path, accounts, accounts)

    check = checks["mariadb.accounts[mariadb-main]"]
    assert check.status is AuditStatus.COMPLIANT, check.message
    observed = cast("dict[str, object]", check.observed)
    assert observed["undeclaredUsers"] == []
    assert "billing@10.0.0.0/255.255.255.0" in cast("list[str]", observed["users"])
    # The firewall is expected to admit 3306 from the allowed source only
    desired = cast("list[str]", checks["firewall.allowedInbound"].desired)
    assert [rule for rule in desired if "3306" in rule] == ["tcp/3306 from 10.0.0.0/24"]


def test_a_missing_tls_account_is_drift(tmp_path: Path) -> None:
    accounts = ["crm@localhost", "billing@localhost"]

    checks = _audit(tmp_path, accounts, accounts)

    assert checks["mariadb.accounts[mariadb-main]"].status is AuditStatus.DRIFT
