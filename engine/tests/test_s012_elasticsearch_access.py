"""Audit, remote access and metrics of an Elasticsearch node (S-012, #314).

Audit runs against recorded observations of a node: the rendered
``elasticsearch.yml`` of the role stands for a converged, untouched node, and
each drift is one hand change to that evidence. The node's configuration,
the host's nftables ruleset and the Alloy collector configuration are
rendered from a validated project with the templates Ansible uses. A client
connecting over HTTPS and plain HTTP being refused need a live node: they
are left for the proving run.
"""

from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.audit import AuditCheck, audit_inventory, elasticsearch_managed_settings
from cloudfall.domain import IpAddress, ResourceId
from cloudfall.elasticsearch_cluster import (
    TRANSPORT_CA_PATH,
    TRANSPORT_CERTIFICATE_PATH,
    TRANSPORT_KEY_PATH,
)
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.pki import CertificateState, certificate_names, load_or_create_ca
from cloudfall.requirements import (
    ELASTICSEARCH_PASSWORD_PATH,
    CertificateRequirement,
    elasticsearch_node_names,
    requirements_of,
)
from cloudfall.secret_store import SecretStore
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from elasticsearch_project import SCHEMAS, elasticsearch_service, write_project
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant" / "h1.json"
ROLES = ROOT / "engine" / "ansible" / "roles"
ROLE = ROLES / "cloudfall_elasticsearch"
INSPECT = ROLES / "cloudfall_inspect"
COLLECTOR = ROLES / "cloudfall_logging_collector"
FIREWALL = ROLES / "cloudfall_firewall"
SERVICES = ROOT / "engine" / "ansible" / "playbooks" / "services.yml"
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
REMOTE_ACCESS: dict[str, object] = {
    "listenAddress": "10.0.0.2",
    "allowedSources": ["10.0.0.0/24"],
}
LOOPBACK_LISTENERS = (
    "tcp   LISTEN 0 128    0.0.0.0:22      0.0.0.0:*\n"
    "tcp   LISTEN 0 4096   127.0.0.1:9200  0.0.0.0:*\n"
    "tcp   LISTEN 0 4096   127.0.0.1:9300  0.0.0.0:*"
)


def _project(
    tmp_path: Path,
    version: str = "9.2.4",
    remote_access: dict[str, object] | None = None,
) -> Path:
    declaration = elasticsearch_service(version=version)
    if remote_access is not None:
        spec = cast("dict[str, dict[str, object]]", declaration["spec"])
        spec["elasticsearch"]["remoteAccess"] = remote_access
    return write_project(tmp_path / "project", declaration)


def _inventory(project: Path) -> PlatformInventory:
    return PlatformInventory.from_state(validate_config(project, SCHEMAS))


def _host(inventory: PlatformInventory) -> dict[str, object]:
    document = render_ansible_inventory(inventory)
    children = cast("dict[str, dict[str, object]]", document["all"])["children"]
    servers = cast("dict[str, dict[str, dict[str, object]]]", children)
    return cast("dict[str, object]", servers["cloudfall_servers"]["hosts"]["h1"])


def _service(host: dict[str, object]) -> dict[str, object]:
    services = cast("list[dict[str, object]]", host["cloudfall_services"])
    return next(item for item in services if item["serviceKind"] == "elasticsearch")


# The role's own files, templated as Ansible does


def _role_defaults() -> dict[str, object]:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    assert isinstance(defaults, dict)
    return {
        name: trust_as_template(value) if isinstance(value, str) else value
        for name, value in defaults.items()
    }


def _role_variables(service: dict[str, object]) -> dict[str, object]:
    return {
        **_role_defaults(),
        "ansible_check_mode": False,
        "cloudfall_server_id": "h1",
        "cloudfall_elasticsearch_service": service,
        "cloudfall_elasticsearch_bootstrap": False,
    }


def _templar(variables: dict[str, object]) -> Templar:
    return Templar(loader=DataLoader(), variables=variables)


def _node_configuration(service: dict[str, object]) -> str:
    text = (ROLE / "templates" / "elasticsearch.yml.j2").read_text(encoding="utf-8")
    return str(_templar(_role_variables(service)).template(trust_as_template(text)))


def _role_value(expression: str, service: dict[str, object]) -> object:
    return _templar(_role_variables(service)).template(trust_as_template(expression))


def _role_tasks(name: str = "main.yml") -> list[dict[str, object]]:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / name))
    assert isinstance(tasks, list)
    return tasks


def _holds(task: dict[str, object], variables: dict[str, object]) -> bool:
    module = cast("dict[str, list[str]]", task["ansible.builtin.assert"])
    scoped = {
        **variables,
        **{
            name: trust_as_template(value) if isinstance(value, str) else value
            for name, value in cast("dict[str, object]", task.get("vars", {})).items()
        },
    }
    return all(
        _templar(scoped).evaluate_conditional(trust_as_template(str(condition)))
        for condition in module["that"]
    )


def _jinja(directory: Path) -> Environment:
    """Jinja with the whitespace rules Ansible's template module applies."""
    return Environment(
        loader=FileSystemLoader(directory),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )


# Audit against recorded observations


def _snapshot(  # noqa: PLR0913 - one observation, each hand change a keyword.
    service: dict[str, object],
    *,
    installed: str = "9.2.4",
    unit: tuple[str, str] = ("running", "enabled"),
    listeners: str = LOOPBACK_LISTENERS,
    configuration: str | None = None,
    observed_at: str = "2026-11-14T10:00:00Z",
    firewall: str | None = None,
) -> dict[str, object]:
    """Return h1's observation as a converged, untouched node, changed as asked."""
    document = json.loads(COMPLIANT.read_text(encoding="utf-8"))
    spec = document["spec"]
    spec["observedAt"] = observed_at
    spec["packages"].append(
        {
            "name": "elasticsearch",
            "version": installed,
            "source": "dpkg-query",
            "arch": "amd64",
        }
    )
    spec["services"]["elasticsearch.service"] = {
        "name": "elasticsearch.service",
        "state": unit[0],
        "status": unit[1],
        "source": "systemd",
    }
    spec["network"]["listeningSockets"] = listeners
    spec["elasticsearch"] = {
        "configurationPath": "/etc/elasticsearch/elasticsearch.yml",
        "configuration": (
            _node_configuration(service) if configuration is None else configuration
        ),
    }
    if firewall is not None:
        spec["firewall"]["managedTableJson"] = firewall
    return cast("dict[str, object]", document)


def _audit(
    tmp_path: Path, project: Path, snapshot: dict[str, object]
) -> dict[str, AuditCheck]:
    observations = tmp_path / "observed"
    observations.mkdir(exist_ok=True)
    (observations / "h1.json").write_text(json.dumps(snapshot), encoding="utf-8")
    report = audit_inventory(
        _inventory(project), load_observations(observations, SCHEMAS)
    )
    (server,) = report.servers
    return {check.check: check for check in server.checks}


def _node_checks(checks: dict[str, AuditCheck]) -> dict[str, str]:
    return {
        name: check.status.value
        for name, check in checks.items()
        if name.startswith("elasticsearch.") or name == "services.bind[search-main]"
    }


COMPLIANT_NODE = {
    "elasticsearch.version[search-main]": "compliant",
    "elasticsearch.line[search-main]": "compliant",
    "elasticsearch.unit[search-main]": "compliant",
    "elasticsearch.configuration[search-main]": "compliant",
    "services.bind[search-main]": "compliant",
}


# proves: S-012-13
def test_s012_13_a_converged_untouched_node_audits_compliant(tmp_path: Path) -> None:
    project = _project(tmp_path)
    service = _service(_host(_inventory(project)))

    checks = _audit(tmp_path, project, _snapshot(service))

    assert _node_checks(checks) == COMPLIANT_NODE


# proves: S-012-13
def test_s012_13_an_installed_version_other_than_the_declared_one_is_drift(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    service = _service(_host(_inventory(project)))

    checks = _audit(tmp_path, project, _snapshot(service, installed="9.2.3"))

    version = checks["elasticsearch.version[search-main]"]
    assert version.status.value == "drift"
    assert version.desired == {"version": "9.2.4"}
    assert version.observed == {"versions": ["9.2.3"]}
    assert _node_checks(checks) == {
        **COMPLIANT_NODE,
        "elasticsearch.version[search-main]": "drift",
    }


# proves: S-012-13
@pytest.mark.parametrize(
    "unit", [("stopped", "enabled"), ("running", "disabled"), ("stopped", "disabled")]
)
def test_s012_13_a_stopped_or_disabled_unit_is_drift(
    tmp_path: Path, unit: tuple[str, str]
) -> None:
    project = _project(tmp_path)
    service = _service(_host(_inventory(project)))

    checks = _audit(tmp_path, project, _snapshot(service, unit=unit))

    assert checks["elasticsearch.unit[search-main]"].status.value == "drift"
    assert checks["elasticsearch.unit[search-main]"].observed == {
        "state": unit[0],
        "status": unit[1],
    }


# proves: S-012-13
@pytest.mark.parametrize(
    ("edit", "listening"),
    [
        ('network.host: "127.0.0.1"', '"0.0.0.0"'),
        ('network.host: "127.0.0.1"', '"10.0.0.9"'),
        ('network.host: "127.0.0.1"', '\nnetwork:\n  host: "10.0.0.9"'),
    ],
)
def test_s012_13_network_host_edited_by_hand_to_a_non_loopback_address_is_drift(
    tmp_path: Path, edit: str, listening: str
) -> None:
    """The file is drift before a restart; the listener is drift after one."""
    project = _project(tmp_path)
    service = _service(_host(_inventory(project)))
    rendered = _node_configuration(service)
    assert edit in rendered
    replacement = (
        listening.lstrip("\n")
        if listening.startswith("\n")
        else f"network.host: {listening}"
    )
    edited = rendered.replace(edit, replacement)
    address = yaml.safe_load(edited)
    host = (
        address["network.host"]
        if "network.host" in address
        else address["network"]["host"]
    )

    before_restart = _audit(tmp_path, project, _snapshot(service, configuration=edited))
    after_restart = _audit(
        tmp_path,
        project,
        _snapshot(
            service,
            configuration=edited,
            listeners=LOOPBACK_LISTENERS.replace("127.0.0.1", host),
        ),
    )

    configuration = before_restart["elasticsearch.configuration[search-main]"]
    assert configuration.status.value == "drift"
    assert cast("dict[str, object]", configuration.observed)["network.host"] == host
    assert before_restart["services.bind[search-main]"].status.value == "compliant"
    assert after_restart["services.bind[search-main]"].status.value == "drift"


def test_a_hand_written_http_host_is_drift(tmp_path: Path) -> None:
    project = _project(tmp_path)
    service = _service(_host(_inventory(project)))
    edited = _node_configuration(service) + 'http.host: "0.0.0.0"\n'

    checks = _audit(tmp_path, project, _snapshot(service, configuration=edited))

    assert checks["elasticsearch.configuration[search-main]"].status.value == "drift"


def test_a_missing_or_unreadable_configuration_is_drift_and_no_evidence_unknown(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    service = _service(_host(_inventory(project)))
    missing = _snapshot(service)
    evidence = cast("dict[str, dict[str, object]]", missing["spec"])
    evidence["elasticsearch"]["configuration"] = None
    absent = _snapshot(service)
    del cast("dict[str, object]", absent["spec"])["elasticsearch"]

    def status(snapshot: dict[str, object]) -> str:
        checks = _audit(tmp_path, project, snapshot)
        return checks["elasticsearch.configuration[search-main]"].status.value

    assert status(missing) == "drift"
    assert status(_snapshot(service, configuration="network.host: [\n")) == "drift"
    assert status(absent) == "unknown"


@pytest.mark.parametrize(
    ("version", "observed_at", "status"),
    [
        ("9.2.4", "2027-10-14T23:59:59Z", "compliant"),
        ("9.2.4", "2027-10-15T00:00:00Z", "drift"),
        ("8.19.6", "2027-01-15T08:00:00Z", "drift"),
    ],
)
def test_d12_a_line_past_its_end_of_maintenance_runs_an_unsupported_version(
    tmp_path: Path, version: str, observed_at: str, status: str
) -> None:
    project = _project(tmp_path, version=version)
    service = _service(_host(_inventory(project)))

    checks = _audit(
        tmp_path,
        project,
        _snapshot(service, installed=version, observed_at=observed_at),
    )

    line = checks["elasticsearch.line[search-main]"]
    assert line.status.value == status
    if status == "drift":
        assert "unsupported version" in line.message


def test_a_server_that_is_no_node_has_no_elasticsearch_check(tmp_path: Path) -> None:
    project = write_project(tmp_path / "project")
    observations = tmp_path / "observed"
    observations.mkdir()
    shutil.copy(COMPLIANT, observations / "h1.json")

    report = audit_inventory(
        _inventory(project), load_observations(observations, SCHEMAS)
    )

    assert not [
        check
        for server in report.servers
        for check in server.checks
        if check.check.startswith("elasticsearch.")
    ]


@pytest.mark.parametrize("remote_access", [None, REMOTE_ACCESS])
def test_the_audited_settings_are_the_ones_the_role_writes(
    tmp_path: Path, remote_access: dict[str, object] | None
) -> None:
    """No drift on an untouched node: the SDK's settings match the template's."""
    inventory = _inventory(_project(tmp_path, remote_access=remote_access))
    rendered = yaml.safe_load(_node_configuration(_service(_host(inventory))))
    desired = elasticsearch_managed_settings(inventory.services[0])

    assert {key: rendered.get(key) for key in desired} == desired


def _inspect(tmp_path: Path, path: Path) -> dict[str, object]:
    output = tmp_path / "out.json"
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "cloudfall_inspect_snapshot": {"spec": {"server": "h1"}},
                        "cloudfall_inspect_elasticsearch_configuration": str(path),
                    },
                    "tasks": [
                        {
                            "ansible.builtin.include_tasks": str(
                                INSPECT / "tasks" / "elasticsearch.yml"
                            )
                        },
                        {
                            "ansible.builtin.copy": {
                                "content": "{{ cloudfall_inspect_snapshot | to_json }}",
                                "dest": str(output),
                            }
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(  # noqa: S603 - the venv's ansible-playbook
        [
            str(ANSIBLE_PLAYBOOK),
            "--inventory",
            "localhost,",
            "--connection",
            "local",
            str(playbook),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    evidence = snapshot["spec"]["elasticsearch"]
    assert isinstance(evidence, dict)
    return evidence


# proves: S-012-13
def test_s012_13_inspect_reads_the_node_configuration_as_text(tmp_path: Path) -> None:
    configuration = tmp_path / "elasticsearch.yml"
    configuration.write_text('network.host: "0.0.0.0"\n', encoding="utf-8")

    assert _inspect(tmp_path, configuration) == {
        "configurationPath": str(configuration),
        "configuration": 'network.host: "0.0.0.0"\n',
    }
    assert _inspect(tmp_path, tmp_path / "none.yml")["configuration"] is None


def test_inspect_only_reads_and_looks_on_elasticsearch_nodes_only() -> None:
    tasks = DataLoader().load_from_file(str(INSPECT / "tasks" / "elasticsearch.yml"))
    modules = {
        next(key for key in task if key.startswith("ansible.builtin."))
        for task in tasks
    }
    (play,) = yaml.safe_load(
        (ROOT / "engine" / "ansible" / "playbooks" / "inspect.yml").read_text("utf-8")
    )
    expression = play["roles"][0]["cloudfall_inspect_elasticsearch_configuration"]

    def path(services: list[dict[str, object]]) -> object:
        return Templar(
            loader=DataLoader(), variables={"cloudfall_services": services}
        ).template(trust_as_template(expression))

    assert modules == {
        "ansible.builtin.stat",
        "ansible.builtin.slurp",
        "ansible.builtin.set_fact",
    }
    assert path([{"serviceKind": "elasticsearch"}]) == (
        "/etc/elasticsearch/elasticsearch.yml"
    )
    assert path([{"serviceKind": "postgresql"}]) == ""
    assert path([]) == ""


# remoteAccess: HTTPS on both addresses, a source-restricted 9200 rule


def _nftables(host: dict[str, object]) -> list[str]:
    rendered = (
        _jinja(FIREWALL / "templates")
        .get_template("nftables.conf.j2")
        .render(cloudfall_firewall_ruleset=host["cloudfall_firewall"])
    )
    return [line.strip() for line in rendered.splitlines()]


def _port_rules(lines: list[str], port: int) -> list[str]:
    return [line for line in lines if f"dport {port} " in line]


# proves: S-012-14
def test_s012_14_the_ruleset_opens_9200_to_the_declared_sources_only(
    tmp_path: Path,
) -> None:
    opened = _host(
        _inventory(_project(tmp_path / "opened", remote_access=REMOTE_ACCESS))
    )
    closed = _host(_inventory(_project(tmp_path / "closed")))

    rules = _port_rules(_nftables(opened), 9200)

    assert rules == [
        'ip saddr 10.0.0.0/24 tcp dport 9200 accept comment "search-main remoteAccess"'
    ]
    assert all(rule.startswith("ip saddr ") for rule in rules)
    assert _port_rules(_nftables(closed), 9200) == []
    assert _port_rules(_nftables(closed), 9300) == []


# proves: S-012-14
def test_s012_14_http_is_tls_only_on_loopback_and_the_listen_address(
    tmp_path: Path,
) -> None:
    opened = _service(
        _host(_inventory(_project(tmp_path / "opened", remote_access=REMOTE_ACCESS)))
    )
    closed = _service(_host(_inventory(_project(tmp_path / "closed"))))

    rendered = yaml.safe_load(_node_configuration(opened))
    loopback = yaml.safe_load(_node_configuration(closed))

    # One HTTP TLS setting per node: every HTTP listener speaks TLS only.
    assert rendered["http.host"] == ["127.0.0.1", "10.0.0.2"]
    assert rendered["xpack.security.http.ssl.enabled"] is True
    assert rendered["xpack.security.http.ssl.certificate"] == (
        TRANSPORT_CERTIFICATE_PATH.value
    )
    assert rendered["xpack.security.http.ssl.key"] == TRANSPORT_KEY_PATH.value
    assert rendered["xpack.security.http.ssl.certificate_authorities"] == [
        TRANSPORT_CA_PATH.value
    ]
    # The transport stays on loopback; only HTTP opens.
    assert rendered["network.host"] == "127.0.0.1"
    assert rendered["xpack.security.enabled"] is True
    # Without remoteAccess the node is unchanged: loopback, plain HTTP.
    assert "http.host" not in loopback
    assert loopback["xpack.security.http.ssl.enabled"] is False
    assert [key for key in loopback if key.startswith("xpack.security.http.")] == [
        "xpack.security.http.ssl.enabled"
    ]


# proves: S-012-14
def test_s012_14_the_node_certificate_comes_from_the_project_ca(
    tmp_path: Path,
) -> None:
    opened = _inventory(_project(tmp_path / "opened", remote_access=REMOTE_ACCESS))
    closed = _inventory(_project(tmp_path / "closed"))

    def certificates(inventory: PlatformInventory) -> list[CertificateRequirement]:
        return [
            requirement.spec
            for server in requirements_of(inventory)
            for requirement in server.requirements
            if isinstance(requirement.spec, CertificateRequirement)
        ]

    # The node's one certificate (S-012, What each node needs): server and
    # client, for its hostname and every address, the listen address included,
    # which a client verifies when it connects there.
    (certificate,) = certificates(opened)
    assert certificate.name.value == "elasticsearch-transport"
    assert certificate.as_dict() == {
        "usage": "server-and-client",
        "subjectNames": ["h1.example.internal", "203.0.113.11", "10.0.0.2"],
        "certificatePath": "/etc/elasticsearch/certs/transport.crt",
        "keyPath": "/etc/elasticsearch/certs/transport.key",
        "caPath": "/etc/elasticsearch/certs/ca.crt",
    }
    assert _host(opened)["cloudfall_certificates"] == [
        {"name": "elasticsearch-transport", "path": TRANSPORT_CERTIFICATE_PATH.value}
    ]
    assert certificates(closed) == []
    assert "cloudfall_certificates" not in _host(closed)
    # The role reads them where the requirement puts them.
    defaults = _role_defaults()
    assert defaults["cloudfall_elasticsearch_certificate_path"] == (
        TRANSPORT_CERTIFICATE_PATH.value
    )
    assert defaults["cloudfall_elasticsearch_key_path"] == TRANSPORT_KEY_PATH.value
    assert defaults["cloudfall_elasticsearch_ca_path"] == TRANSPORT_CA_PATH.value


def test_the_node_certificate_names_each_address_once(tmp_path: Path) -> None:
    project = _project(tmp_path, remote_access=REMOTE_ACCESS)
    server = project / "servers" / "h1.yaml"
    document = yaml.safe_load(server.read_text(encoding="utf-8"))
    del document["spec"]["network"]
    document["spec"]["address"] = "10.0.0.2"
    server.write_text(yaml.safe_dump(document), encoding="utf-8")
    (node,) = _inventory(project).servers

    assert [name.value for name in elasticsearch_node_names(node)] == [
        "h1.example.internal",
        "10.0.0.2",
    ]
    assert [
        name.value for name in elasticsearch_node_names(node, IpAddress("2001:db8::2"))
    ] == ["h1.example.internal", "10.0.0.2", "2001:db8::2"]


def test_the_project_ca_signs_the_node_certificate_for_both_sides(
    tmp_path: Path,
) -> None:
    """Signed for server and client auth, it is ``met`` on the next converge."""
    now = datetime.datetime(2026, 11, 14, 12, 0, tzinfo=datetime.UTC)
    inventory = _inventory(_project(tmp_path, remote_access=REMOTE_ACCESS))
    (requirement,) = [
        item.spec
        for server in requirements_of(inventory)
        for item in server.requirements
        if isinstance(item.spec, CertificateRequirement)
    ]
    key = ec.generate_private_key(ec.SECP256R1())
    request = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "h1")]))
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )
    store = SecretStore(
        tmp_path / "secrets",
        decrypt=lambda source: "".join(
            f"{line}\n"
            for line in source.read_text(encoding="utf-8").splitlines()
            if not line.startswith("sops_")
        ),
        encrypt=lambda _target, plaintext: plaintext + "sops_mac=fake\n",
    )
    ca, _ = load_or_create_ca(store, ResourceId("production"), now)

    signed = ca.sign(request, requirement, now)

    certificate = x509.load_pem_x509_certificate(signed.encode())
    usages = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert list(usages.value) == [
        ExtendedKeyUsageOID.SERVER_AUTH,
        ExtendedKeyUsageOID.CLIENT_AUTH,
    ]
    assert certificate_names(certificate) == {
        "h1.example.internal",
        "203.0.113.11",
        "10.0.0.2",
    }
    assert ca.state(signed, requirement, now) is CertificateState.MET


# proves: S-012-14
def test_s012_14_the_role_calls_its_api_over_https_verifying_the_project_ca(
    tmp_path: Path,
) -> None:
    opened = _service(
        _host(_inventory(_project(tmp_path / "opened", remote_access=REMOTE_ACCESS)))
    )
    closed = _service(_host(_inventory(_project(tmp_path / "closed"))))
    calls = [
        task
        for task in (
            *_role_tasks(),
            *cast(
                "list[dict[str, object]]",
                next(
                    task
                    for task in _role_tasks()
                    if task["name"] == "Set the elastic password once"
                )["block"],
            ),
        )
        if "ansible.builtin.uri" in task
    ]

    assert len(calls) == 3
    for task in calls:
        uri = cast("dict[str, str]", task["ansible.builtin.uri"])
        assert uri["url"].strip().startswith("{{ cloudfall_elasticsearch_api_url")
        assert uri["ca_path"] == "{{ cloudfall_elasticsearch_api_ca or omit }}"
    assert _role_value("{{ cloudfall_elasticsearch_api_url }}", opened) == (
        "https://10.0.0.2:9200"
    )
    assert _role_value("{{ cloudfall_elasticsearch_api_ca }}", opened) == (
        TRANSPORT_CA_PATH.value
    )
    assert _role_value("{{ cloudfall_elasticsearch_api_url }}", closed) == (
        "http://127.0.0.1:9200"
    )
    assert _role_value("{{ cloudfall_elasticsearch_api_ca }}", closed) == ""


def test_an_ipv6_listen_address_is_bracketed_in_the_api_url(tmp_path: Path) -> None:
    access: dict[str, object] = {
        "listenAddress": "2001:db8::2",
        "allowedSources": ["2001:db8::/64"],
    }
    service = _service(_host(_inventory(_project(tmp_path, remote_access=access))))

    assert _role_value("{{ cloudfall_elasticsearch_api_url }}", service) == (
        "https://[2001:db8::2]:9200"
    )


# proves: S-012-14
def test_s012_14_the_role_accepts_remote_access_narrowed_to_sources() -> None:
    validate = next(
        task
        for task in _role_tasks()
        if task["name"] == "Validate Elasticsearch service target and declaration"
    )
    refuse = next(
        task
        for task in _role_tasks()
        if task["name"]
        == "Refuse remote access that is not narrowed to declared sources"
    )
    service = {
        "id": "search-main",
        "serviceKind": "elasticsearch",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": 9200},
        "elasticsearch": {
            "nodes": ["h1"],
            "users": [],
            "remoteAccess": dict(REMOTE_ACCESS),
        },
    }
    facts = {
        "distribution": "Debian",
        "distribution_major_version": "13",
        "service_mgr": "systemd",
    }

    def variables(**remote_access: object) -> dict[str, object]:
        elasticsearch = cast("dict[str, object]", service["elasticsearch"])
        return {
            "ansible_facts": facts,
            "cloudfall_server_id": "h1",
            "cloudfall_elasticsearch_service": {
                **service,
                "elasticsearch": {
                    **elasticsearch,
                    "remoteAccess": {**REMOTE_ACCESS, **remote_access},
                },
            },
        }

    assert _holds(validate, variables())
    assert _holds(refuse, variables())
    assert not _holds(refuse, variables(allowedSources=["0.0.0.0/0"]))
    assert not _holds(refuse, variables(allowedSources=[]))
    assert not _holds(refuse, variables(listenAddress="0.0.0.0"))  # noqa: S104


def test_the_certificate_is_checked_before_a_configuration_names_it() -> None:
    names = [task["name"] for task in _role_tasks()]
    include = next(
        task
        for task in _role_tasks()
        if task["name"] == "Prepare the node certificate for HTTP over TLS"
    )
    tls = _role_tasks("http_tls.yml")

    assert include["ansible.builtin.include_tasks"] == "http_tls.yml"
    assert names.index("Prepare the node certificate for HTTP over TLS") < (
        names.index("Install the managed Elasticsearch configuration")
    )
    modules = [next(key for key in task if key.startswith("ansible.")) for task in tls]
    assert modules == [
        "ansible.builtin.stat",
        "ansible.builtin.assert",
        "ansible.builtin.file",
    ]
    readable = tls[2]["loop"]
    assert {"path": "{{ cloudfall_elasticsearch_key_path }}", "mode": "0640"} in cast(
        "list[dict[str, str]]", readable
    )


def _nft_rule(port: int, source: str | None = None) -> dict[str, object]:
    expression: list[dict[str, object]] = []
    if source is not None:
        address, length = source.split("/")
        expression.append(
            {
                "match": {
                    "op": "==",
                    "left": {"payload": {"protocol": "ip", "field": "saddr"}},
                    "right": {"prefix": {"addr": address, "len": int(length)}},
                }
            }
        )
    expression.extend(
        [
            {
                "match": {
                    "op": "==",
                    "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                    "right": port,
                }
            },
            {"accept": None},
        ]
    )
    return {
        "rule": {
            "family": "inet",
            "table": "cloudfall",
            "chain": "input",
            "expr": expression,
        }
    }


def _nft_table(*rules: dict[str, object]) -> str:
    return json.dumps(
        {
            "nftables": [
                {"table": {"family": "inet", "name": "cloudfall"}},
                {
                    "chain": {
                        "family": "inet",
                        "table": "cloudfall",
                        "name": "input",
                        "policy": "drop",
                    }
                },
                *rules,
            ]
        }
    )


# proves: S-012-14
def test_s012_14_audit_holds_the_listen_address_and_no_unrestricted_9200_rule(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path, remote_access=REMOTE_ACCESS)
    service = _service(_host(_inventory(project)))
    listeners = LOOPBACK_LISTENERS + "\ntcp   LISTEN 0 4096   10.0.0.2:9200  0.0.0.0:*"
    base = [_nft_rule(22), _nft_rule(80), _nft_rule(443)]
    restricted = _nft_table(*base, _nft_rule(9200, "10.0.0.0/24"))
    unrestricted = _nft_table(*base, _nft_rule(9200, "10.0.0.0/24"), _nft_rule(9200))

    converged = _audit(
        tmp_path,
        project,
        _snapshot(service, listeners=listeners, firewall=restricted),
    )
    opened_to_all = _audit(
        tmp_path,
        project,
        _snapshot(service, listeners=listeners, firewall=unrestricted),
    )
    listen_down = _audit(tmp_path, project, _snapshot(service, firewall=restricted))

    assert _node_checks(converged) == COMPLIANT_NODE
    assert converged["firewall.allowedInbound"].status.value == "compliant"
    assert opened_to_all["firewall.allowedInbound"].status.value == "drift"
    assert listen_down["services.bind[search-main]"].status.value == "drift"


# Metrics: one Elasticsearch exporter per node, labelled with the service id


def _logging_stack() -> dict[str, object]:
    inventory = PlatformInventory.from_state(validate_config(EXAMPLES, SCHEMAS))
    return inventory.logging_stacks[0].as_dict()


def _collector_defaults() -> dict[str, object]:
    defaults = yaml.safe_load(
        (COLLECTOR / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    assert isinstance(defaults, dict)
    return defaults


def _collector(name: str, services: list[dict[str, object]]) -> str:
    return (
        _jinja(COLLECTOR / "templates")
        .get_template(name)
        .render(
            {
                **_collector_defaults(),
                "cloudfall_logging_collector_stack": _logging_stack(),
                "cloudfall_server_id": "h1",
                "cloudfall_services": services,
            }
        )
    )


def _without_metrics(service: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in service.items() if key != "metrics"}


# proves: S-012-15
def test_s012_15_metrics_render_one_elasticsearch_exporter_labelled_with_the_id(
    tmp_path: Path,
) -> None:
    service = _service(_host(_inventory(_project(tmp_path))))

    rendered = _collector("config.alloy.j2", [service])
    without = _collector("config.alloy.j2", [_without_metrics(service)])

    assert rendered.count("prometheus.exporter.elasticsearch ") == 1
    assert 'prometheus.exporter.elasticsearch "search_main" {' in rendered
    assert 'address = "http://127.0.0.1:9200"' in rendered
    assert 'node    = "_local"' in rendered
    assert 'username      = "cloudfall"' in rendered
    assert (
        'password_file = "/run/credentials/alloy.service/'
        'elasticsearch-search-main-password"'
    ) in rendered
    assert 'replacement  = "search-main"' in rendered
    assert (
        "targets         = prometheus.exporter.elasticsearch.search_main.targets"
    ) in rendered
    assert "prometheus.exporter.elasticsearch" not in without


# proves: S-012-15
def test_s012_15_alloy_gets_the_password_as_a_credential_never_the_value(
    tmp_path: Path,
) -> None:
    service = _service(_host(_inventory(_project(tmp_path))))

    override = _collector("alloy-systemd-override.conf.j2", [service])
    rendered = _collector("config.alloy.j2", [service])
    without = _collector("alloy-systemd-override.conf.j2", [_without_metrics(service)])

    assert (
        "LoadCredential=elasticsearch-search-main-password:"
        f"{ELASTICSEARCH_PASSWORD_PATH.value}"
    ) in override.splitlines()
    assert "LoadCredential" not in without
    # S-012-8: only the file's path, never a password, in the configuration.
    assert "password " not in rendered.replace("password_file", "")
    assert "password=" not in rendered


def test_metrics_with_remote_access_verify_the_project_ca(tmp_path: Path) -> None:
    service = _service(
        _host(_inventory(_project(tmp_path, remote_access=REMOTE_ACCESS)))
    )

    rendered = _collector("config.alloy.j2", [service])
    override = _collector("alloy-systemd-override.conf.j2", [service])

    assert 'address = "https://10.0.0.2:9200"' in rendered
    assert (
        'ca      = "/run/credentials/alloy.service/elasticsearch-search-main-ca"'
    ) in rendered
    assert (
        f"LoadCredential=elasticsearch-search-main-ca:{TRANSPORT_CA_PATH.value}"
    ) in override.splitlines()


def _credential_ids(override: str, setting: str) -> list[str]:
    return [
        line.removeprefix(f"{setting}=").split(":", 1)[0]
        for line in override.splitlines()
        if line.startswith(f"{setting}=")
    ]


def test_a_missing_exporter_credential_falls_back_instead_of_stopping_alloy(
    tmp_path: Path,
) -> None:
    # #326: systemd refuses to start a unit whose LoadCredential= file is
    # missing unless a SetCredential= of the same ID gives it a default; an
    # empty SetCredential= value is dropped by the parser, so the fallback is
    # one newline, which Alloy reads as an empty password and an empty CA pool.
    loopback = _service(_host(_inventory(_project(tmp_path / "closed"))))
    opened = _service(
        _host(_inventory(_project(tmp_path / "opened", remote_access=REMOTE_ACCESS)))
    )

    for service, ids in (
        (loopback, ["elasticsearch-search-main-password"]),
        (
            opened,
            ["elasticsearch-search-main-password", "elasticsearch-search-main-ca"],
        ),
    ):
        override = _collector("alloy-systemd-override.conf.j2", [service])
        assert _credential_ids(override, "LoadCredential") == ids
        assert _credential_ids(override, "SetCredential") == ids
        for credential in ids:
            assert f"SetCredential={credential}:\\n" in override.splitlines()

    # Taking the service out of the project takes its exporter and its
    # credential lines out with it on the next logging deploy.
    removed_override = _collector("alloy-systemd-override.conf.j2", [])
    removed_config = _collector("config.alloy.j2", [])
    assert "Credential" not in removed_override
    assert "search-main" not in removed_override
    assert "prometheus.exporter.elasticsearch" not in removed_config
    assert "search_main" not in removed_config


def test_the_collector_requires_the_exporter_files_before_it_changes_the_host(
    tmp_path: Path,
) -> None:
    tasks = DataLoader().load_from_file(str(COLLECTOR / "tasks" / "main.yml"))
    names = [task["name"] for task in tasks]
    inspect = tasks[names.index("Inspect the files the Elasticsearch exporters read")]
    opened = _service(
        _host(_inventory(_project(tmp_path / "opened", remote_access=REMOTE_ACCESS)))
    )
    loopback = _service(_host(_inventory(_project(tmp_path / "closed"))))

    def files(services: list[dict[str, object]]) -> object:
        variables: dict[str, object] = {
            **_collector_defaults(),
            "cloudfall_services": services,
        }
        for name, value in inspect["vars"].items():
            variables[name] = _templar(variables).template(trust_as_template(value))
        return _templar(variables).template(trust_as_template(inspect["loop"]))

    assert names.index("Require the Elasticsearch node before its exporter") < (
        names.index("Install safe Alloy configuration before package configuration")
    )
    assert files([loopback]) == [ELASTICSEARCH_PASSWORD_PATH.value]
    assert files([opened]) == [
        ELASTICSEARCH_PASSWORD_PATH.value,
        TRANSPORT_CA_PATH.value,
    ]
    assert files([_without_metrics(loopback)]) == []
    assert files([]) == []


# #329: a services converge that changes how the node is reached takes the
# collector's exporter with it, without waiting for the next logging deploy


def _collector_step() -> dict[str, object]:
    (play,) = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))
    return next(
        task
        for task in play["tasks"]
        if task.get("ansible.builtin.include_role", {}).get("name")
        == "cloudfall_logging_collector"
    )


def _flat(tasks: list[dict[str, object]]) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for task in tasks:
        if "block" in task:
            found.extend(_flat(cast("list[dict[str, object]]", task["block"])))
        else:
            found.append(task)
    return found


def _module_arguments(task: dict[str, object], module: str) -> dict[str, object]:
    return cast("dict[str, object]", task[module])


def _exporter_tasks() -> list[dict[str, object]]:
    include = _module_arguments(_collector_step(), "ansible.builtin.include_role")
    return cast(
        "list[dict[str, object]]",
        DataLoader().load_from_file(
            str(COLLECTOR / "tasks" / f"{include['tasks_from']}.yml")
        ),
    )


def _converged_collector(
    host: dict[str, object], *, alloy_set_up: bool = True
) -> dict[str, str]:
    """Each file services.yml writes for Alloy on ``host``, by destination.

    Empty when the step leaves the host alone. The guard of the collector's
    ``exporters.yml`` is evaluated with the stat results the host would give.
    """
    step = _collector_step()
    variables: dict[str, object] = {**_collector_defaults(), **host}
    if not _templar(variables).evaluate_conditional(
        trust_as_template(str(step["when"]))
    ):
        return {}
    for name, value in cast("dict[str, str]", step["vars"]).items():
        variables[name] = _templar(variables).template(trust_as_template(value))
    tasks = _exporter_tasks()
    (inspect,) = [task for task in tasks if "ansible.builtin.stat" in task]
    (guarded,) = [task for task in tasks if "block" in task]
    variables[str(inspect["register"])] = {
        "results": [
            {"item": path, "stat": {"exists": alloy_set_up}}
            for path in cast("list[str]", inspect["loop"])
        ]
    }
    if not _templar(variables).evaluate_conditional(
        trust_as_template(str(guarded["when"]))
    ):
        return {}
    templates = [
        _module_arguments(task, "ansible.builtin.template")
        for task in _flat([guarded])
        if "ansible.builtin.template" in task
    ]
    return {
        str(template["dest"]): _jinja(COLLECTOR / "templates")
        .get_template(str(template["src"]))
        .render(variables)
        for template in templates
    }


def _collector_host(project: Path) -> dict[str, object]:
    return {
        **_host(_inventory(project)),
        "cloudfall_logging_collector": _logging_stack(),
    }


def test_a_services_converge_takes_the_exporter_to_https_with_remote_access(
    tmp_path: Path,
) -> None:
    closed = _collector_host(_project(tmp_path / "closed"))
    opened = _collector_host(_project(tmp_path / "opened", remote_access=REMOTE_ACCESS))

    before = _converged_collector(closed)
    after = _converged_collector(opened)

    config = "/etc/alloy/cloudfall.alloy"
    override = "/etc/systemd/system/alloy.service.d/cloudfall.conf"
    assert set(after) == {config, override}
    assert 'address = "http://127.0.0.1:9200"' in before[config]
    assert 'address = "https://10.0.0.2:9200"' in after[config]
    assert (
        'ca      = "/run/credentials/alloy.service/elasticsearch-search-main-ca"'
    ) in after[config]
    assert _credential_ids(after[override], "LoadCredential") == [
        "elasticsearch-search-main-password",
        "elasticsearch-search-main-ca",
    ]
    assert _credential_ids(before[override], "LoadCredential") == [
        "elasticsearch-search-main-password",
    ]
    # The same files logging deploy writes, so the next one changes nothing.
    for files, project_host in ((before, closed), (after, opened)):
        services = [_service(project_host)]
        assert files[config] == _collector("config.alloy.j2", services)
        assert files[override] == _collector("alloy-systemd-override.conf.j2", services)
    # Taking remoteAccess away again: the next converge rewrites both files
    # (so the template tasks change and Alloy restarts) back to plain HTTP.
    removed = _converged_collector(closed)
    assert removed[config] != after[config]
    assert removed[override] != after[override]
    assert removed == before
    assert "-ca" not in removed[override]
    assert (
        "https://" not in removed[config].split("prometheus.exporter.elasticsearch")[1]
    )


def test_the_services_converge_reloads_alloy_only_through_its_validated_files() -> None:
    main = DataLoader().load_from_file(str(COLLECTOR / "tasks" / "main.yml"))
    deployed = {
        _module_arguments(task, "ansible.builtin.template")["dest"]: task
        for task in main
        if "ansible.builtin.template" in task
    }
    tasks = _flat(_exporter_tasks())
    templates = [task for task in tasks if "ansible.builtin.template" in task]
    names = [task["name"] for task in tasks]

    # The files, owners, modes and the restart of logging deploy, no other.
    assert len(templates) == len(deployed) == 2
    for task in templates:
        arguments = _module_arguments(task, "ansible.builtin.template")
        assert arguments == _module_arguments(
            deployed[arguments["dest"]], "ansible.builtin.template"
        )
        assert task["notify"] == "Restart Alloy"
    validate = next(task for task in tasks if "ansible.builtin.command" in task)
    assert _module_arguments(validate, "ansible.builtin.command")["argv"] == [
        "/usr/bin/alloy",
        "validate",
        "/etc/alloy/cloudfall.alloy",
    ]
    flush = names.index("Apply the validated Alloy configuration")
    assert names.index(validate["name"]) < flush == len(names) - 1


def test_a_services_converge_leaves_a_host_without_the_collector_alone(
    tmp_path: Path,
) -> None:
    opened = _project(tmp_path / "opened", remote_access=REMOTE_ACCESS)

    # Not a collector of any logging stack: the role is not even included.
    assert _converged_collector(_host(_inventory(opened))) == {}
    # A collector logging deploy has not set up yet: nothing is installed.
    assert _converged_collector(_collector_host(opened), alloy_set_up=False) == {}
    step = _collector_step()
    assert _module_arguments(step, "ansible.builtin.include_role") == {
        "name": "cloudfall_logging_collector",
        "tasks_from": "exporters",
    }
    (play,) = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))
    assert play["tasks"][-1] == step
    for task in _flat(_exporter_tasks()):
        assert "ansible.builtin.apt" not in task
        assert "ansible.builtin.user" not in task
