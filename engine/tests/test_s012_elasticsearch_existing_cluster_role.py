"""The Elasticsearch role joins an existing cluster and lets it go (S-012, #315).

S-012-17, S-012-18 and S-012-19 need a live cluster Cloudfall did not create
for the whole proof; here the rendered inventory of a joining service is
real, each node's configuration and firewall ruleset are rendered from their
templates, the role's decisions are evaluated with Ansible's own templating
against the answers a seed host gives, and the services converge runs
against fake nodes, as the S-012-5 and S-012-6 tests do.
"""

from __future__ import annotations

import datetime
import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar, trust_as_template
from cloudfall.ansible_data import as_ansible_data
from cloudfall.cli import main
from cloudfall.domain import ResourceId
from cloudfall.elasticsearch_cluster import EXISTING_CLUSTER_CA_PATH
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import (
    ERROR_SERVICES_SECRET,
    EngineContext,
    LifecycleError,
    converge_services,
)
from cloudfall.pki import load_or_create_ca
from cloudfall.secret_store import SecretStore
from cloudfall.secrets import SopsSecretProvider
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from elasticsearch_hosts import FakeNodes
from elasticsearch_project import (
    SCHEMAS,
    SNAPSHOT_REPOSITORY,
    elasticsearch_service,
    write_project,
)

if TYPE_CHECKING:
    from cloudfall.lifecycle import ExecutionStep

# The seed check's login is read by the file lookup, a plugin Ansible loads.
init_plugin_loader()

ROOT = Path(__file__).parents[2]
ROLES = ROOT / "engine" / "ansible" / "roles"
ROLE = ROLES / "cloudfall_elasticsearch"
FIREWALL = ROLES / "cloudfall_firewall" / "templates" / "nftables.conf.j2"
EXISTING = {
    "seedHosts": ["10.0.0.11:9300", "10.0.0.12:9300"],
    "caCertificateFile": "pki/search-old-ca.pem",
}
EXISTING_SEEDS = ["10.0.0.11:9300", "10.0.0.12:9300"]
CREDENTIALS_REF = {
    "environment": "production",
    "path": "/services/search-main",
    "key": "OLD_CLUSTER_MONITOR",
}
MONITOR_USER = "team-monitor"
MONITOR_PASSWORD = "synthetic-monitor:value"  # noqa: S105 - grepped for
PROJECT_CA = "/etc/elasticsearch/certs/ca.crt"
FOREIGN_CA = "/etc/elasticsearch/certs/existing-cluster-ca.crt"
FACTS = {
    "distribution": "Debian",
    "distribution_major_version": "13",
    "service_mgr": "systemd",
    "memtotal_mb": 16384,
}


def _old_ca() -> str:
    """Return the PEM of a CA that signed the existing nodes' certificates."""
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
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode()


def _project(
    tmp_path: Path,
    *,
    nodes: tuple[str, ...] = ("h1",),
    joining: bool = True,
    login: bool = False,
) -> Path:
    """Write the spec's joining service on ``nodes``, or the same after it left.

    With ``login``, its ``existingCluster`` names a ``credentialsRef``.
    """
    service = elasticsearch_service(
        clusterName="search", nodes=list(nodes), snapshotRepository=SNAPSHOT_REPOSITORY
    )
    if joining:
        spec = cast("dict[str, dict[str, object]]", service["spec"])
        spec["elasticsearch"]["existingCluster"] = (
            {**EXISTING, "credentialsRef": CREDENTIALS_REF} if login else EXISTING
        )
    project = write_project(tmp_path / "project", service, servers=nodes)
    (project / "pki").mkdir()
    (project / "pki" / "search-old-ca.pem").write_text(_old_ca(), encoding="utf-8")
    return project


def _rendered(project: Path) -> dict[str, Any]:
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    return cast("dict[str, Any]", render_ansible_inventory(inventory))


def _hosts(rendered: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return cast(
        "dict[str, dict[str, Any]]",
        rendered["all"]["children"]["cloudfall_servers"]["hosts"],
    )


def _load(name: str, directory: str = "tasks") -> list[dict[str, Any]]:
    tasks = DataLoader().load_from_file(str(ROLE / directory / name))
    assert isinstance(tasks, list)
    return tasks


def _flat(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for task in tasks:
        found.append(task)
        for section in ("block", "rescue", "always"):
            found.extend(_flat(task.get(section, [])))
    return found


def _task(tasks: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(task for task in _flat(tasks) if task["name"] == name)


def _trusted(values: dict[str, Any]) -> dict[str, Any]:
    return {
        name: trust_as_template(value) if isinstance(value, str) else value
        for name, value in values.items()
    }


def _variables(host: dict[str, Any], **extra: object) -> dict[str, Any]:
    """Return the role's variables on one node, as services.yml passes them."""
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    (service,) = [
        item
        for item in host["cloudfall_services"]
        if item["serviceKind"] == "elasticsearch"
    ]
    return {
        **_trusted(defaults),
        **host,
        "ansible_check_mode": False,
        "ansible_facts": FACTS,
        "cloudfall_elasticsearch_service": service,
        "cloudfall_elasticsearch_account_files": {
            "cloudfallPassword": "/tmp/staged/cloudfall.password",  # noqa: S108
        },
        **extra,
    }


def _templar(variables: dict[str, Any]) -> Templar:
    return Templar(loader=DataLoader(), variables=variables)


def _value(expression: str, variables: dict[str, Any]) -> object:
    return _templar(variables).template(trust_as_template(expression))


def _true(condition: object, variables: dict[str, Any]) -> bool:
    conditions = condition if isinstance(condition, list) else [condition]
    return all(
        _templar(variables).evaluate_conditional(trust_as_template(str(item)))
        for item in conditions
    )


def _holds(task: dict[str, Any], variables: dict[str, Any]) -> bool:
    scoped = {**variables, **_trusted(task.get("vars", {}))}
    return _true(task["ansible.builtin.assert"]["that"], scoped)


def _message(task: dict[str, Any], variables: dict[str, Any]) -> str:
    scoped = {**variables, **_trusted(task.get("vars", {}))}
    text = _value(task["ansible.builtin.assert"]["fail_msg"], scoped)
    return " ".join(str(text).split())


def _configuration(variables: dict[str, Any]) -> dict[str, object]:
    """Render elasticsearch.yml the way the role's install task does."""
    install = _task(
        _load("main.yml"), "Install the managed Elasticsearch configuration"
    )
    bootstrap = _value(install["vars"]["cloudfall_elasticsearch_bootstrap"], variables)
    text = (ROLE / "templates" / "elasticsearch.yml.j2").read_text(encoding="utf-8")
    rendered = _templar(
        {**variables, "cloudfall_elasticsearch_bootstrap": bootstrap}
    ).template(trust_as_template(text))
    return cast("dict[str, object]", yaml.safe_load(str(rendered)))


def _formed(*, exists: bool) -> dict[str, object]:
    return {"cloudfall_elasticsearch_cluster_formed": {"stat": {"exists": exists}}}


def _ruleset_lines(ruleset: dict[str, Any]) -> list[str]:
    text = FIREWALL.read_text(encoding="utf-8")
    rendered = str(
        _templar({"cloudfall_firewall_ruleset": ruleset}).template(
            trust_as_template(text)
        )
    )
    return [line.strip() for line in rendered.splitlines() if "dport 9300" in line]


# S-012-17


# proves: S-012-17
def test_s012_17_a_joining_node_seeds_from_the_existing_cluster_and_never_bootstraps(
    tmp_path: Path,
) -> None:
    hosts = _hosts(_rendered(_project(tmp_path)))
    transport = hosts["h1"]["cloudfall_elasticsearch_transport"]["search-main"]

    assert transport["seedHosts"] == [*EXISTING_SEEDS, "203.0.113.11:9300"]
    assert transport["existingCaPath"] == FOREIGN_CA == EXISTING_CLUSTER_CA_PATH.value
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    assert defaults["cloudfall_elasticsearch_existing_ca_path"] == FOREIGN_CA
    # The home itself joins: neither its first converge nor a later one
    # ever writes cluster.initial_master_nodes.
    for formed in (False, True):
        rendered = _configuration(_variables(hosts["h1"], **_formed(exists=formed)))
        assert rendered["cluster.name"] == "search"
        assert rendered["node.name"] == "h1"
        assert rendered["network.host"] == "127.0.0.1"
        assert rendered["transport.host"] == "203.0.113.11"
        assert rendered["discovery.seed_hosts"] == transport["seedHosts"]
        assert rendered["xpack.security.transport.ssl.verification_mode"] == (
            "certificate"
        )
        assert rendered["xpack.security.transport.ssl.client_authentication"] == (
            "required"
        )
        assert rendered["xpack.security.transport.ssl.certificate_authorities"] == [
            PROJECT_CA,
            FOREIGN_CA,
        ]
        assert "cluster.initial_master_nodes" not in rendered
        assert "discovery.type" not in rendered


# proves: S-012-17
def test_s012_17_the_joining_node_admits_9300_from_the_existing_nodes_only(
    tmp_path: Path,
) -> None:
    hosts = _hosts(_rendered(_project(tmp_path)))
    ruleset = hosts["h1"]["cloudfall_firewall"]

    transport = [rule for rule in ruleset["allowedInbound"] if rule["port"] == 9300]
    assert transport == [
        {
            "port": 9300,
            "protocol": "tcp",
            "sources": ["10.0.0.11/32", "10.0.0.12/32"],
            "description": "search-main transport",
        }
    ]
    assert _ruleset_lines(ruleset) == [
        'ip saddr 10.0.0.11/32 tcp dport 9300 accept comment "search-main transport"',
        'ip saddr 10.0.0.12/32 tcp dport 9300 accept comment "search-main transport"',
    ]


# proves: S-012-17
def test_s012_17_a_second_cloudfall_node_admits_the_first_and_the_existing_nodes(
    tmp_path: Path,
) -> None:
    hosts = _hosts(_rendered(_project(tmp_path, nodes=("h1", "h2"))))

    for node, other in (("h1", "12"), ("h2", "11")):
        (rule,) = [
            rule
            for rule in hosts[node]["cloudfall_firewall"]["allowedInbound"]
            if rule["port"] == 9300
        ]
        assert rule["sources"] == [
            f"203.0.113.{other}/32",
            f"2001:db8:{other}::/64",
            "10.0.0.11/32",
            "10.0.0.12/32",
        ]
        transport = hosts[node]["cloudfall_elasticsearch_transport"]["search-main"]
        assert transport["seedHosts"] == [
            *EXISTING_SEEDS,
            "203.0.113.11:9300",
            "203.0.113.12:9300",
        ]


# proves: S-012-17
def test_s012_17_a_joining_home_neither_looks_for_peers_nor_sets_elastic(
    tmp_path: Path,
) -> None:
    """The existing nodes answer on 9300, and elastic is the existing cluster's."""
    hosts = _hosts(_rendered(_project(tmp_path)))
    fresh = _variables(hosts["h1"], **_formed(exists=False))
    look = _task(
        _load("transport.yml"),
        "Look for a running node before the home bootstraps the cluster",
    )
    main_tasks = _load("main.yml")
    elastic = _task(main_tasks, "Set the elastic password once")
    set_it = _task(main_tasks, "Set the elastic password from its hash")
    record = _task(main_tasks, "Record that the elastic password was set")
    unset = {"cloudfall_elasticsearch_elastic_set": {"stat": {"exists": False}}}

    assert not _true(look["when"], fresh)
    assert _true(elastic["when"], fresh)
    assert not _true(set_it["when"], {**fresh, **unset})
    copy = record["ansible.builtin.copy"]
    assert copy["force"] is False
    assert str(_value(copy["content"], fresh)).strip() == (
        "search-main: elastic password left to the existing cluster"
    )
    # Once existingCluster is gone, the marker it left keeps elastic unset.
    left = _hosts(_rendered(_project(tmp_path / "left", joining=False)))
    later = _variables(left["h1"], **_formed(exists=True))
    assert _true(set_it["when"], {**later, **unset})
    assert not _true(
        set_it["when"],
        {**later, "cloudfall_elasticsearch_elastic_set": {"stat": {"exists": True}}},
    )


# proves: S-012-17
def test_s012_17_the_preflight_only_reads_the_existing_cluster(
    tmp_path: Path,
) -> None:
    """Nothing the role sends a seed host changes it: one GET, no foreign secret."""
    hosts = _hosts(_rendered(_project(tmp_path)))
    variables = _variables(hosts["h1"])
    uris = [
        task
        for task in _flat(_load("existing-cluster.yml"))
        if "ansible.builtin.uri" in task
    ]

    (ask,) = uris
    call = ask["ansible.builtin.uri"]
    assert "method" not in call  # GET
    assert "body" not in call
    assert call["ca_path"] == "{{ cloudfall_elasticsearch_existing_ca_path }}"
    assert ask["no_log"] is True
    assert ask["changed_when"] is False
    seeds = cast("list[str]", _value(ask["loop"], variables))
    urls = [
        "".join(str(_value(call["url"], {**variables, "item": seed})).split())
        for seed in seeds
    ]
    query = "/_nodes?filter_path=cluster_name,nodes.*.name,nodes.*.version"
    assert urls == [
        f"https://10.0.0.11:9200{query}",
        f"https://10.0.0.12:9200{query}",
    ]


def _login(tmp_path: Path, *, login: bool) -> tuple[str, str]:
    """Return the login the seed check sends, as Ansible renders it."""
    hosts = _hosts(_rendered(_project(tmp_path / "project", login=login)))
    staged = tmp_path / "staged"
    staged.mkdir()
    files: dict[str, str] = {}
    for name, value in (
        ("cloudfallPassword", "synthetic-cloudfall"),
        ("existingUsername", MONITOR_USER),
        ("existingPassword", MONITOR_PASSWORD),
    ):
        if name.startswith("existing") and not login:
            continue
        (staged / name).write_text(value, encoding="utf-8")
        files[name] = str(staged / name)
    block, _ = _preflight()
    variables = {
        **_variables(hosts["h1"], cloudfall_elasticsearch_account_files=files),
        **_trusted(block["vars"]),
    }
    call = _task(
        block["block"], "Ask each seed host for the cluster's name and node versions"
    )
    return (
        str(_value(call["ansible.builtin.uri"]["url_username"], variables)),
        str(_value(call["ansible.builtin.uri"]["url_password"], variables)),
    )


# proves: S-012-17
def test_s012_17_the_seed_check_logs_in_with_the_credentials_ref_account(
    tmp_path: Path,
) -> None:
    """The team's own monitor account: nobody adds a cloudfall user there."""
    assert _login(tmp_path / "ref", login=True) == (MONITOR_USER, MONITOR_PASSWORD)


# proves: S-012-17
def test_s012_17_without_credentials_ref_the_seed_check_logs_in_as_cloudfall(
    tmp_path: Path,
) -> None:
    assert _login(tmp_path / "own", login=False) == (
        "cloudfall",
        "synthetic-cloudfall",
    )


# proves: S-012-18
def test_s012_18_a_declared_login_that_is_not_staged_stops_the_converge(
    tmp_path: Path,
) -> None:
    require = _task(
        _load("existing-cluster.yml"),
        "Require the staged login of the existing cluster",
    )
    with_ref = _hosts(_rendered(_project(tmp_path / "ref", login=True)))
    without_ref = _hosts(_rendered(_project(tmp_path / "own")))

    # Only the account files: services.yml was run without cloudfall.
    unstaged = _variables(with_ref["h1"])
    assert _true(require["when"], unstaged)
    assert not _holds(require, unstaged)
    assert "existingCluster's credentialsRef" in " ".join(
        str(_value(require["ansible.builtin.assert"]["fail_msg"], unstaged)).split()
    )
    staged = _variables(
        with_ref["h1"],
        cloudfall_elasticsearch_account_files={
            "cloudfallPassword": "/tmp/staged/cloudfall.password",  # noqa: S108
            "existingUsername": "/tmp/staged/existing.username",  # noqa: S108
            "existingPassword": "/tmp/staged/existing.password",  # noqa: S108
        },
    )
    assert _holds(require, staged)
    assert not _true(require["when"], _variables(without_ref["h1"]))


ACCOUNTS = (
    "CLOUDFALL_PASSWORD=synthetic-cloudfall\nELASTIC_PASSWORD=synthetic-elastic\n"
    "S3=SYNTHETICACCESSKEY:synthetic/secret+key\n"
)
LOGIN = f"OLD_CLUSTER_MONITOR={MONITOR_USER}:{MONITOR_PASSWORD}\n"


def _answers(
    *nodes: tuple[str, str], name: str = "search", status: int = 200
) -> dict[str, object]:
    """Return what the seed hosts answered GET /_nodes, the first one or none."""
    answer: dict[str, object] = {"item": "10.0.0.11:9300", "status": status}
    if status == 200:
        answer["json"] = {
            "cluster_name": name,
            "nodes": {
                f"id-{node}": {"name": node, "version": version}
                for node, version in nodes
            },
        }
    return {
        "cloudfall_elasticsearch_existing_answers": {
            "results": [answer, {"item": "10.0.0.12:9300", "status": -1}]
        }
    }


def _preflight() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the preflight block and its refusals, in order."""
    (block,) = [task for task in _load("existing-cluster.yml") if "block" in task]
    return block, [task for task in block["block"] if "ansible.builtin.assert" in task]


@dataclass
class _Services:
    """Run the converge's steps against fake nodes; check the role at services.yml."""

    rendered: dict[str, Any]
    nodes: FakeNodes = field(default_factory=FakeNodes)
    passed: dict[str, list[str]] = field(default_factory=dict)
    delivered: dict[str, dict[str, str]] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)
    steps: list[ExecutionStep] = field(default_factory=list)
    login: dict[str, tuple[str, int]] = field(default_factory=dict)

    def __call__(self, step: ExecutionStep) -> _Finished:
        self.steps.append(step)
        if "--extra-vars" not in step.argv:
            return _Finished()
        extra = json.loads(step.argv[step.argv.index("--extra-vars") + 1])
        self.nodes.answer(Path(step.argv[-1]).name, extra)
        if step.argv[-1].endswith("services.yml"):
            self.extra = extra
            self._role(extra)
        return _Finished()

    def _role(self, extra: dict[str, Any]) -> None:
        main_tasks = _load("main.yml")
        block, refusals = _preflight()
        before_block = [
            task
            for task in _load("existing-cluster.yml")
            if "ansible.builtin.assert" in task
        ]
        for node, host in _hosts(self.rendered).items():
            files = extra["cloudfall_services_elasticsearch_files"]["search-main"]
            for name in ("existingUsername", "existingPassword"):
                if name in files:
                    path = Path(files[name])
                    self.login[name] = (
                        path.read_text("utf-8"),
                        stat.S_IMODE(path.stat().st_mode),
                    )
            copied = extra["cloudfall_services_delivered_files"][node]
            self.delivered[node] = {
                str(item["path"]): Path(str(item["source"])).read_text("utf-8")
                for item in copied
            }
            variables = {
                **_variables(
                    {**host, **extra},
                    cloudfall_elasticsearch_account_files=files,
                    cloudfall_elasticsearch_installed={"rc": 1, "stdout": ""},
                    cloudfall_elasticsearch_existing_ca={
                        "stat": {"exists": FOREIGN_CA in self.delivered[node]}
                    },
                    **_answers(("old-1", "9.2.4"), ("old-2", "9.2.3")),
                    **_formed(exists=False),
                ),
                **_trusted(block["vars"]),
            }
            passed: list[str] = []
            guards = [
                task
                for task in main_tasks[
                    : [task["name"] for task in main_tasks].index(
                        "Install the declared version, or put a stopped node back"
                    )
                ]
                if "ansible.builtin.assert" in task
            ]
            assert _true(block["when"], variables)
            for guard in [*guards, *before_block, *refusals]:
                if "when" in guard and not _true(guard["when"], variables):
                    continue
                assert _holds(guard, variables), (node, guard["name"])
                passed.append(str(guard["name"]))
            self.passed[node] = passed


@dataclass(frozen=True)
class _Finished:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _plaintext(source: Path) -> str:
    """Stand in for sops: the test's secret files are not encrypted."""
    return source.read_text(encoding="utf-8")


def _converge(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    login: bool = False,
    secrets: str = ACCOUNTS,
) -> tuple[Path, _Services]:
    """Validate the joining project, then run its services converge on fake nodes."""
    project = _project(tmp_path, login=login)
    assert main(
        ["config", "validate", "--project", str(project), "--schemas", str(SCHEMAS)]
    ) == 0, capsys.readouterr().out
    rendered = _rendered(project)
    text = json.dumps(as_ansible_data(rendered))
    for secret in (
        "synthetic-cloudfall",
        "synthetic-elastic",
        "SYNTHETICACCESSKEY",
        MONITOR_PASSWORD,
    ):
        assert secret not in text
    source = project / "secrets" / "production" / "services" / "search-main.env"
    source.parent.mkdir(parents=True)
    source.write_text(secrets, encoding="utf-8")
    store = SecretStore(
        project / "secrets", decrypt=_plaintext, encrypt=lambda _, value: value
    )
    now = datetime.datetime.now(datetime.UTC)
    load_or_create_ca(store, ResourceId("production"), now)
    services = _Services(rendered)

    converge_services(
        EngineContext(
            project_directory=project,
            schema_directory=SCHEMAS,
            engine_directory=ROOT / "engine",
            inventory_file=project / "tmp" / "ansible-inventory.json",
            run=services,
        ),
        SopsSecretProvider(secrets_directory=project / "secrets", decrypt=_plaintext),
    )
    return project, services


# proves: S-012-17
def test_s012_17_the_converge_delivers_the_existing_ca_and_passes_every_guard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, services = _converge(tmp_path, capsys)

    assert services.nodes.playbooks[-1] == "services.yml"
    # The joining node makes its key and gets a transport certificate too.
    assert "logging-requests.yml" in services.nodes.playbooks
    delivered = services.delivered["h1"]
    old_ca = (project / "pki" / "search-old-ca.pem").read_text("utf-8")
    assert delivered[FOREIGN_CA] == old_ca
    assert delivered[PROJECT_CA] != old_ca
    assert "/etc/elasticsearch/certs/transport.crt" in delivered
    assert {
        "Validate Elasticsearch service target and declaration",
        "Require the staged account files of the service",
        "Refuse a joining node without the existing cluster's CA",
        "Refuse an existing cluster that no seed host answered for",
        "Refuse an existing cluster of another name",
        "Refuse an existing cluster with a node of another major",
        "Refuse an existing cluster with a node newer than the declared version",
    } <= set(services.passed["h1"])
    # No answer, no staged file path's content, carries the existing cluster's
    # own credentials: the role asks it as Cloudfall's account.
    staged = services.extra["cloudfall_services_elasticsearch_files"]["search-main"]
    assert set(staged) <= {
        "cloudfallPassword",
        "cloudfallHash",
        "elasticHash",
        "s3AccessKey",
        "s3SecretKey",
    }
    assert services.login == {}


# proves: S-012-17
def test_s012_17_the_converge_stages_the_credentials_ref_login_on_the_controller_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project, services = _converge(
        tmp_path, capsys, login=True, secrets=ACCOUNTS + LOGIN
    )

    assert "Require the staged login of the existing cluster" in services.passed["h1"]
    assert services.login == {
        "existingUsername": (MONITOR_USER, 0o600),
        "existingPassword": (MONITOR_PASSWORD, 0o600),
    }
    staged = services.extra["cloudfall_services_elasticsearch_files"]["search-main"]
    for name in ("existingUsername", "existingPassword"):
        path = Path(staged[name])
        # Removed after the run, never inside the project.
        assert not path.exists()
        assert project not in path.parents
    # Never copied to a node, never on a command line or in an environment.
    for delivered in services.delivered.values():
        assert all(MONITOR_PASSWORD not in text for text in delivered.values())
    for step in services.steps:
        assert MONITOR_PASSWORD not in " ".join(step.argv)
        assert MONITOR_PASSWORD not in json.dumps(dict(step.environment))
    # The reference reaches the rendered inventory, its value does not.
    (service,) = [
        item
        for item in _hosts(_rendered(project))["h1"]["cloudfall_services"]
        if item["serviceKind"] == "elasticsearch"
    ]
    assert service["elasticsearch"]["existingCluster"]["credentialsRef"] == (
        CREDENTIALS_REF
    )


# proves: S-012-18
def test_s012_18_the_seed_check_and_its_login_stay_out_of_the_task_output() -> None:
    block, _ = _preflight()
    ask = _task(
        block["block"], "Ask each seed host for the cluster's name and node versions"
    )
    require = _task(
        _load("existing-cluster.yml"),
        "Require the staged login of the existing cluster",
    )

    assert ask["no_log"] is True
    # The guard names the reference, never a path's content.
    message = str(require["ansible.builtin.assert"]["fail_msg"])
    assert "lookup" not in message
    assert "existingPassword" not in message


# proves: S-012-18
@pytest.mark.parametrize(
    ("secrets", "expected"),
    [
        (ACCOUNTS, "OLD_CLUSTER_MONITOR is missing"),
        (
            ACCOUNTS + "OLD_CLUSTER_MONITOR=synthetic-monitor-only\n",
            "is not <username>:<password> on one line",
        ),
    ],
    ids=["unknown", "malformed"],
)
def test_s012_18_a_credentials_ref_the_store_lacks_stops_before_any_playbook(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    secrets: str,
    expected: str,
) -> None:
    with pytest.raises(LifecycleError) as error:
        _converge(tmp_path, capsys, login=True, secrets=secrets)

    assert error.value.code == ERROR_SERVICES_SECRET
    assert expected in error.value.detail
    assert "existingCluster of Elasticsearch service search-main" in error.value.detail
    assert "synthetic-monitor-only" not in error.value.detail


# S-012-18


# proves: S-012-18
def test_s012_18_the_preflight_runs_before_the_volume_and_the_package() -> None:
    names = [task["name"] for task in _load("main.yml")]
    preflight = "Ask the existing cluster before the node joins it"
    ask = names.index(preflight)

    assert names.index("Install the node certificate and the project CA") < ask
    for later in (
        "Provide the node's data volume",
        "Configure the Elastic package repository of the declared major",
        "Install the declared version, or put a stopped node back",
    ):
        assert ask < names.index(later)
    include = _task(_load("main.yml"), preflight)
    assert include["ansible.builtin.include_tasks"] == "existing-cluster.yml"


def _refusal(
    tmp_path: Path, name: str, answers: dict[str, object], *, login: bool = False
) -> tuple[bool, str]:
    hosts = _hosts(_rendered(_project(tmp_path, login=login)))
    block, refusals = _preflight()
    variables = {
        **_variables(hosts["h1"], **answers),
        **_trusted(block["vars"]),
    }
    task = next(task for task in refusals if task["name"] == name)
    held = _holds(task, variables)
    return held, "" if held else _message(task, variables)


# proves: S-012-18
def test_s012_18_a_cluster_of_another_name_stops_the_converge(tmp_path: Path) -> None:
    name = "Refuse an existing cluster of another name"

    held, _ = _refusal(tmp_path / "a", name, _answers(("old-1", "9.2.4")))
    assert held
    held, message = _refusal(
        tmp_path / "b", name, _answers(("old-1", "9.2.4"), name="logs")
    )
    assert not held
    assert (
        "Seed host 10.0.0.11:9300 belongs to cluster logs, not to clusterName search"
        in message
    )


# proves: S-012-18
def test_s012_18_a_node_of_another_major_stops_the_converge(tmp_path: Path) -> None:
    name = "Refuse an existing cluster with a node of another major"

    held, _ = _refusal(tmp_path / "a", name, _answers(("old-1", "9.0.0")))
    assert held
    held, message = _refusal(
        tmp_path / "b", name, _answers(("old-1", "9.2.4"), ("old-2", "8.19.6"))
    )
    assert not held
    assert message.startswith("Node old-2 runs 8.19.6:")
    assert "another major than the declared 9.2.4" in message
    # A major that only shares a prefix is still another major.
    held, message = _refusal(tmp_path / "c", name, _answers(("old-3", "92.0.0")))
    assert not held
    assert "Node old-3 runs 92.0.0" in message


# proves: S-012-18
def test_s012_18_a_node_newer_than_the_declared_version_stops_the_converge(
    tmp_path: Path,
) -> None:
    name = "Refuse an existing cluster with a node newer than the declared version"

    held, _ = _refusal(
        tmp_path / "a", name, _answers(("old-1", "9.2.4"), ("old-2", "9.1.7"))
    )
    assert held
    held, message = _refusal(
        tmp_path / "b",
        name,
        _answers(("old-1", "9.2.10"), ("old-2", "9.2.4"), ("old-3", "9.3.0")),
    )
    assert not held
    assert message.startswith("Node old-1 runs 9.2.10, Node old-3 runs 9.3.0:")
    assert "newer than the declared 9.2.4 of service search-main" in message


# proves: S-012-18
def test_s012_18_a_cluster_no_seed_host_answers_for_stops_the_converge(
    tmp_path: Path,
) -> None:
    name = "Refuse an existing cluster that no seed host answered for"

    held, message = _refusal(tmp_path / "own", name, _answers(status=401))
    assert not held
    assert "(10.0.0.11:9300: 401, 10.0.0.12:9300: -1)" in message
    assert "no cloudfall account with the monitor privilege" in message
    assert "existingCluster.credentialsRef" in message
    # With a credentialsRef, the message points at that account instead.
    held, message = _refusal(tmp_path / "ref", name, _answers(status=401), login=True)
    assert not held
    assert "the account existingCluster.credentialsRef names" in message
    assert "cloudfall account" not in message


def test_a_dry_run_on_a_node_without_the_existing_ca_skips_the_preflight(
    tmp_path: Path,
) -> None:
    hosts = _hosts(_rendered(_project(tmp_path)))
    block, _ = _preflight()

    def runs(*, check: bool, exists: bool) -> bool:
        return _true(
            block["when"],
            _variables(
                hosts["h1"],
                ansible_check_mode=check,
                cloudfall_elasticsearch_existing_ca={"stat": {"exists": exists}},
            ),
        )

    assert runs(check=False, exists=False)
    assert runs(check=True, exists=True)
    assert not runs(check=True, exists=False)


# S-012-19


# proves: S-012-19
@pytest.mark.parametrize("nodes", [("h1",), ("h1", "h2")], ids=["one", "two"])
def test_s012_19_without_existing_cluster_no_foreign_ca_or_old_seed_host_remains(
    tmp_path: Path, nodes: tuple[str, ...]
) -> None:
    joined = _hosts(_rendered(_project(tmp_path / "joined", nodes=nodes)))
    left = _hosts(_rendered(_project(tmp_path / "left", nodes=nodes, joining=False)))

    for node in nodes:
        before = _configuration(_variables(joined[node], **_formed(exists=True)))
        after = _configuration(_variables(left[node], **_formed(exists=True)))
        text = json.dumps(after)
        for old in ("10.0.0.11", "10.0.0.12", FOREIGN_CA):
            assert old not in text
        assert after["cluster.name"] == before["cluster.name"] == "search"
        assert "cluster.initial_master_nodes" not in after
        assert "existingCaPath" not in json.dumps(left[node])
        assert "10.0.0.1" not in json.dumps(left[node]["cloudfall_firewall"])
        if len(nodes) > 1:
            assert after["discovery.seed_hosts"] == [
                "203.0.113.11:9300",
                "203.0.113.12:9300",
            ]
            assert after["xpack.security.transport.ssl.verification_mode"] == "full"
            assert after["xpack.security.transport.ssl.certificate_authorities"] == [
                PROJECT_CA
            ]
        else:
            # A cluster of one again: its transport is back on loopback.
            assert after["discovery.seed_hosts"] == ["127.0.0.1"]
            assert after["xpack.security.transport.ssl.enabled"] is False


# proves: S-012-19
def test_s012_19_the_foreign_ca_leaves_and_the_cluster_is_green_or_yellow_on_one_node(
    tmp_path: Path,
) -> None:
    """The changed configuration restarts one node at a time; then the CA goes.

    Green with two or more Cloudfall nodes; a single one left has nowhere to
    put its replicas, so yellow is its end state.
    """
    main_tasks = _load("main.yml")
    names = [task["name"] for task in main_tasks]
    remove = _task(
        main_tasks, "Remove the existing cluster's CA once existingCluster is gone"
    )
    wait = _task(
        main_tasks,
        "Wait for the cluster to be green, or yellow on one node, without the "
        "existing cluster",
    )
    install = _task(main_tasks, "Install the managed Elasticsearch configuration")

    assert install["notify"] == "Restart Elasticsearch"
    assert (
        names.index("Apply the managed configuration")
        < names.index(str(remove["name"]))
        < names.index(str(wait["name"]))
    )
    assert remove["ansible.builtin.file"] == {
        "path": "{{ cloudfall_elasticsearch_existing_ca_path }}",
        "state": "absent",
    }
    joined = _hosts(_rendered(_project(tmp_path / "joined")))
    assert not _true(remove["when"], _variables(joined["h1"]))
    for nodes, status in ((("h1", "h2"), "green"), (("h1",), "yellow")):
        left = _hosts(
            _rendered(_project(tmp_path / status, nodes=nodes, joining=False))
        )
        variables = _variables(left["h1"])
        assert _true(remove["when"], variables)
        removed = {"cloudfall_elasticsearch_existing_ca_removed": {"changed": True}}
        assert _true(wait["when"], {**variables, **removed})
        kept = {"cloudfall_elasticsearch_existing_ca_removed": {"changed": False}}
        assert not _true(wait["when"], {**variables, **kept})
        url = str(_value(wait["ansible.builtin.uri"]["url"], variables))
        assert "".join(url.split()).endswith(
            f"/_cluster/health?wait_for_status={status}&timeout=10s"
        )
    assert wait["no_log"] is True
    assert wait["until"] == "cloudfall_elasticsearch_without_existing.status == 200"
