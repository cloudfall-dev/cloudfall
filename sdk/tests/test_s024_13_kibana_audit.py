"""S-024: audit drift of a Kibana service (S-024-13).

The installed version against the Elasticsearch service's ``version``, the
unit, the managed files, the level Kibana reports, the two Kibana roles, the
Kibana roles of the declared users, and the keys in Kibana's keystore, from
the evidence inspect collects. A listener off loopback is the
``services.bind`` check every service has. The compliant ``kibana.yml`` is
the role's own template rendered with Ansible's templating, so the audit
compares against what a converge writes.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.audit import AuditCheck, AuditReport, AuditStatus, audit_inventory
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.validation import validate_config
from elasticsearch_project import elasticsearch_service
from elasticsearch_project import write_project as write_elasticsearch_project
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
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_kibana"
VIEW = "cloudfall-kibana-view"
EDIT = "cloudfall-kibana-edit"
KEYS = [
    "xpack.encryptedSavedObjects.encryptionKey",
    "xpack.reporting.encryptionKey",
    "xpack.security.encryptionKey",
]
TOKEN_SETTING = "elasticsearch.serviceAccountToken"  # noqa: S105 - a setting name
SUPPORT: dict[str, object] = {
    **ANALYST,
    "name": "support",
    "passwordRef": {
        "environment": "production",
        "path": "/services/search-main",
        "key": "SUPPORT_PASSWORD",
    },
}
# A declared user of the cluster with no Kibana access: an application's
APPLICATION: dict[str, object] = {
    **ANALYST,
    "name": "crm",
    "passwordRef": {
        "environment": "production",
        "path": "/services/search-main",
        "key": "CRM_PASSWORD",
    },
}
LISTENERS = (
    "tcp   LISTEN 0 511    127.0.0.1:9200  0.0.0.0:*\n"
    "tcp   LISTEN 0 511    127.0.0.1:5601  0.0.0.0:*"
)
SECRET = "synthetic-hand-written-secret-4f1c9a"  # noqa: S105 - grepped for


def _inventory(
    tmp_path: Path, *, domain: bool = False, **elasticsearch: object
) -> PlatformInventory:
    """Return the spec's declaration: analyst edits, support views, crm has none."""
    users = [copy.deepcopy(ANALYST), copy.deepcopy(SUPPORT), copy.deepcopy(APPLICATION)]
    kibana = kibana_service(
        users=[
            {"name": "analyst", "access": "edit"},
            {"name": "support", "access": "view"},
        ],
        domain="search-kibana-site" if domain else None,
    )
    project = write_project(
        tmp_path / "project",
        search_main(users=users, **elasticsearch),
        kibana,
        domains=(kibana_domain(),) if domain else (),
    )
    return PlatformInventory.from_state(validate_config(project, SCHEMAS))


def _trusted(value: object) -> object:
    """Trust the role's own files as templates, as Ansible trusts a role's files."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return {key: _trusted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_trusted(item) for item in value]
    return value


def _rendered(inventory: PlatformInventory) -> str:
    """Render ``kibana.yml`` as the Kibana role writes it for the inventory."""
    services = {service.resource_id.value: service for service in inventory.services}
    kibana = services["search-kibana"]
    assert kibana.kibana is not None
    domains = {domain.resource_id.value: domain for domain in inventory.domains}
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    variables: dict[str, object] = {
        **cast("dict[str, object]", _trusted(defaults)),
        "cloudfall_kibana_service": kibana.as_dict(),
        "cloudfall_kibana_elasticsearch_service": services["search-main"].as_dict(),
        "cloudfall_kibana_domain": (
            {}
            if kibana.kibana.domain is None
            else domains[kibana.kibana.domain.value].as_dict()
        ),
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    text = (ROLE / "templates" / "kibana.yml.j2").read_text(encoding="utf-8")
    return str(templar.template(trust_as_template(text)))


def _role(base: str) -> dict[str, object]:
    """Return a Kibana role as Kibana's role API answers it after a converge."""
    return {
        "elasticsearch": {"cluster": [], "indices": [], "run_as": []},
        "kibana": [{"base": [base], "feature": {}, "spaces": ["*"]}],
        "metadata": {"cloudfall_managed_by": "search-kibana"},
    }


def _evidence(inventory: PlatformInventory) -> dict[str, object]:
    """Return what inspect reads from a Kibana server converged as declared."""
    return {
        "service": "search-kibana",
        "configurationPath": "/etc/kibana/kibana.yml",
        "configuration": _rendered(inventory),
        "nodeOptions": (
            "## Kibana's own options\n--unhandled-rejections=warn\n"
            "--max-old-space-size=1024\n"
        ),
        "keystore": [TOKEN_SETTING, *KEYS],
        "status": {"read": True, "level": "available"},
        "roles": [
            {"name": VIEW, "found": True, **_role("read")},
            {"name": EDIT, "found": True, **_role("all")},
        ],
        "userRoles": {
            "analyst": [EDIT],
            "support": [VIEW],
            "crm": [],
            "cloudfall": [],
        },
    }


def _report(
    tmp_path: Path,
    inventory: PlatformInventory,
    evidence: dict[str, object] | None,
    host: dict[str, object] | None = None,
) -> AuditReport:
    """Audit h1 with the compliant fixture, the node, Kibana and its evidence.

    ``host`` replaces the installed versions (``kibana``, ``elasticsearch``),
    fields of Kibana's unit (``unit``), or the listeners (``listeners``).
    """
    host = host or {}
    document = json.loads((COMPLIANT / "h1.json").read_text(encoding="utf-8"))
    spec = document["spec"]
    for name in ("kibana", "elasticsearch"):
        spec["packages"].append(
            {
                "name": name,
                "version": host.get(name, "9.2.4"),
                "source": "apt",
                "arch": "amd64",
            }
        )
    for unit, fields in (
        ("kibana.service", host.get("unit", {})),
        ("elasticsearch.service", {}),
    ):
        spec["services"][unit] = {
            "name": unit,
            "state": "running",
            "status": "enabled",
            "source": "systemd",
            **cast("dict[str, str]", fields),
        }
    network = spec["network"]
    network["listeningSockets"] += "\n" + cast(
        "str", host.get("listeners", LISTENERS)
    )
    if evidence is not None:
        spec["kibana"] = evidence
    observations = tmp_path / "observed"
    observations.mkdir(parents=True)
    (observations / "h1.json").write_text(json.dumps(document), encoding="utf-8")
    return audit_inventory(inventory, load_observations(observations, SCHEMAS))


def _checks(
    tmp_path: Path,
    inventory: PlatformInventory,
    evidence: dict[str, object] | None,
    host: dict[str, object] | None = None,
) -> dict[str, AuditCheck]:
    """Return the Kibana checks of h1, and its listener check."""
    report = _report(tmp_path, inventory, evidence, host)
    (audit,) = (item for item in report.servers if item.server_id == "h1")
    return {
        check.check: check
        for check in audit.checks
        if check.check.startswith("kibana.")
        or check.check == "services.bind[search-kibana]"
    }


def _statuses(checks: dict[str, AuditCheck]) -> dict[str, AuditStatus]:
    return {name: check.status for name, check in checks.items()}


ALL_COMPLIANT = {
    "kibana.version[search-kibana]": AuditStatus.COMPLIANT,
    "kibana.unit[search-kibana]": AuditStatus.COMPLIANT,
    "kibana.configuration[search-kibana]": AuditStatus.COMPLIANT,
    "kibana.status[search-kibana]": AuditStatus.COMPLIANT,
    "kibana.roles[search-kibana]": AuditStatus.COMPLIANT,
    "kibana.users[search-kibana]": AuditStatus.COMPLIANT,
    "kibana.keystore[search-kibana]": AuditStatus.COMPLIANT,
    "services.bind[search-kibana]": AuditStatus.COMPLIANT,
}


# proves: S-024-13
def test_s024_13_a_kibana_converged_as_declared_is_compliant(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)

    checks = _checks(tmp_path, inventory, _evidence(inventory))

    assert _statuses(checks) == ALL_COMPLIANT
    users = checks["kibana.users[search-kibana]"]
    # crm has no Kibana access and holds no Kibana role: nothing to report
    assert users.desired == {"analyst": [EDIT], "crm": [], "support": [VIEW]}


# proves: S-024-13
@pytest.mark.parametrize(
    "elasticsearch",
    [
        pytest.param({}, id="loopback"),
        pytest.param(
            {
                "remoteAccess": {
                    "listenAddress": "10.0.0.11",
                    "allowedSources": ["10.0.0.0/24"],
                }
            },
            id="remote access",
        ),
    ],
)
@pytest.mark.parametrize("domain", [False, True], ids=["unpublished", "published"])
def test_s024_13_the_rendered_kibana_yml_of_every_shape_is_compliant(
    tmp_path: Path,
    elasticsearch: dict[str, object],
    domain: bool,  # noqa: FBT001 - the parametrized shape
) -> None:
    inventory = _inventory(tmp_path, domain=domain, **elasticsearch)

    checks = _checks(tmp_path, inventory, _evidence(inventory))

    configuration = checks["kibana.configuration[search-kibana]"]
    assert configuration.status is AuditStatus.COMPLIANT, configuration.message
    settings = cast("dict[str, object]", configuration.desired)["kibana.yml"]
    assert isinstance(settings, dict)
    assert ("xpack.security.secureCookies" in settings) is domain
    assert ("elasticsearch.ssl.verificationMode" in settings) is bool(elasticsearch)


# proves: S-024-13
def test_s024_13_a_kibana_version_other_than_the_elasticsearch_services_is_drift(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)

    checks = _checks(tmp_path, inventory, _evidence(inventory), {"kibana": "9.2.3"})

    version = checks["kibana.version[search-kibana]"]
    assert version.status is AuditStatus.DRIFT
    assert version.desired == {"version": "9.2.4", "elasticsearch": "search-main"}
    assert version.observed == {"versions": ["9.2.3"]}


def test_kibana_is_compared_with_the_declared_version_not_the_nodes(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)

    checks = _checks(
        tmp_path, inventory, _evidence(inventory), {"elasticsearch": "9.2.5"}
    )

    # The node drifted, not Kibana: Kibana runs the service's declared version
    assert checks["kibana.version[search-kibana]"].status is AuditStatus.COMPLIANT


def test_a_missing_kibana_package_is_drift(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    _report(tmp_path, inventory, _evidence(inventory))
    document = json.loads((tmp_path / "observed" / "h1.json").read_text("utf-8"))
    document["spec"]["packages"] = [
        item for item in document["spec"]["packages"] if item["name"] != "kibana"
    ]
    again = tmp_path / "again"
    again.mkdir()
    (again / "h1.json").write_text(json.dumps(document), encoding="utf-8")

    report = audit_inventory(inventory, load_observations(again, SCHEMAS))

    (audit,) = report.servers
    (version,) = (c for c in audit.checks if c.check.startswith("kibana.version"))
    assert version.status is AuditStatus.DRIFT
    assert version.observed is None


# proves: S-024-13
@pytest.mark.parametrize(
    "unit",
    [
        pytest.param({"state": "stopped"}, id="stopped"),
        pytest.param({"status": "disabled"}, id="disabled"),
    ],
)
def test_s024_13_a_stopped_or_disabled_kibana_unit_is_drift(
    tmp_path: Path, unit: dict[str, str]
) -> None:
    inventory = _inventory(tmp_path)

    checks = _checks(tmp_path, inventory, _evidence(inventory), {"unit": unit})

    assert checks["kibana.unit[search-kibana]"].status is AuditStatus.DRIFT


# proves: S-024-13
def test_s024_13_server_host_changed_by_hand_to_a_non_loopback_address_is_drift(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)
    evidence = _evidence(inventory)
    evidence["configuration"] = cast("str", evidence["configuration"]).replace(
        'server.host: "127.0.0.1"', 'server.host: "0.0.0.0"'
    )
    assert "0.0.0.0" in cast("str", evidence["configuration"])  # noqa: S104
    listeners = LISTENERS.replace("127.0.0.1:5601", "0.0.0.0:5601")

    # Before Kibana restarts: the file is drift
    before = _checks(tmp_path / "before", inventory, evidence)
    # After it restarts with it: the listener is drift too
    after = _checks(
        tmp_path / "after", inventory, evidence, {"listeners": listeners}
    )

    configuration = before["kibana.configuration[search-kibana]"]
    assert configuration.status is AuditStatus.DRIFT
    assert configuration.observed is not None
    kibana_yml = cast("dict[str, dict[str, object]]", configuration.observed)
    assert kibana_yml["kibana.yml"]["server.host"] == "0.0.0.0"  # noqa: S104
    assert "server.host differs" in configuration.message
    assert before["services.bind[search-kibana]"].status is AuditStatus.COMPLIANT
    bind = after["services.bind[search-kibana]"]
    assert bind.status is AuditStatus.DRIFT
    assert bind.observed == {"listeners": ["0.0.0.0:5601"]}
    assert cast("dict[str, object]", bind.desired)["loopbackOnly"] is True


# proves: S-024-13
@pytest.mark.parametrize(
    "held",
    [
        pytest.param([VIEW, EDIT], id="next to its view role"),
        pytest.param([EDIT], id="instead of its view role"),
    ],
)
def test_s024_13_support_given_the_edit_role_by_hand_is_drift(
    tmp_path: Path, held: list[str]
) -> None:
    inventory = _inventory(tmp_path)
    evidence = _evidence(inventory)
    cast("dict[str, list[str]]", evidence["userRoles"])["support"] = held

    checks = _checks(tmp_path, inventory, evidence)

    users = checks["kibana.users[search-kibana]"]
    assert users.status is AuditStatus.DRIFT
    assert cast("dict[str, object]", users.observed)["support"] == sorted(held)
    assert users.message == (
        f"support holds {', '.join(sorted(held))} where its access gives {VIEW}"
    )
    # Every other check stays as it was
    assert {
        name: status
        for name, status in _statuses(checks).items()
        if name != "kibana.users[search-kibana]"
    } == {
        name: status
        for name, status in ALL_COMPLIANT.items()
        if name != "kibana.users[search-kibana]"
    }


@pytest.mark.parametrize(
    ("user", "held", "message"),
    [
        pytest.param(
            "analyst",
            [],
            f"analyst holds no Kibana role where its access gives {EDIT}",
            id="a user that lost its role",
        ),
        pytest.param(
            "crm",
            [VIEW],
            f"crm holds {VIEW} where its access gives none",
            id="a user without access given a role",
        ),
    ],
)
def test_a_declared_users_kibana_roles_other_than_its_access_are_drift(
    tmp_path: Path, user: str, held: list[str], message: str
) -> None:
    inventory = _inventory(tmp_path)
    evidence = _evidence(inventory)
    cast("dict[str, list[str]]", evidence["userRoles"])[user] = held

    checks = _checks(tmp_path, inventory, evidence)

    users = checks["kibana.users[search-kibana]"]
    assert users.status is AuditStatus.DRIFT
    assert users.message == message


def test_an_undeclared_user_with_a_kibana_role_is_listed_not_drift(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)
    evidence = _evidence(inventory)
    cast("dict[str, list[str]]", evidence["userRoles"])["hand-made"] = [EDIT]

    checks = _checks(tmp_path, inventory, evidence)

    users = checks["kibana.users[search-kibana]"]
    assert users.status is AuditStatus.COMPLIANT
    assert users.message == (
        "desired state matches; unmanaged (not declared, left as they are): "
        f"hand-made holds {EDIT}"
    )
    assert "hand-made" not in cast("dict[str, object]", users.observed)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        pytest.param(
            lambda text: text.replace(
                "telemetry.optIn: false", "telemetry.optIn: true"
            ),
            "telemetry.optIn differs",
            id="a changed setting",
        ),
        pytest.param(
            lambda text: text.replace(
                'server.publicBaseUrl: "http://127.0.0.1:5601"\n', ""
            ),
            "server.publicBaseUrl differs",
            id="a removed setting",
        ),
        pytest.param(
            lambda text: text + f"elasticsearch.password: {SECRET}\n",
            "kibana.yml holds settings the role does not write: elasticsearch.password",
            id="an added setting",
        ),
    ],
)
def test_kibana_yml_changed_by_hand_is_drift(
    tmp_path: Path, edit: Callable[[str], str], message: str
) -> None:
    inventory = _inventory(tmp_path)
    evidence = _evidence(inventory)
    evidence["configuration"] = edit(cast("str", evidence["configuration"]))

    report = _report(tmp_path, inventory, evidence)

    (audit,) = report.servers
    (configuration,) = (
        c for c in audit.checks if c.check == "kibana.configuration[search-kibana]"
    )
    assert configuration.status is AuditStatus.DRIFT
    assert configuration.message == message
    # A hand-written setting is named, its value is in no finding (S-024-5)
    assert SECRET not in json.dumps(report.as_dict())


@pytest.mark.parametrize(
    ("options", "observed", "message"),
    [
        pytest.param(
            "--max-old-space-size=2048\n",
            {"--max-old-space-size": 2048},
            "node.options gives --max-old-space-size 2048",
            id="another heap",
        ),
        pytest.param(
            "#--max-old-space-size=1024\n",
            {"--max-old-space-size": []},
            "node.options gives --max-old-space-size nowhere",
            id="the heap commented out",
        ),
        pytest.param(
            None,
            {"present": False},
            "node.options is missing",
            id="no node.options",
        ),
    ],
)
def test_a_heap_changed_by_hand_is_drift(
    tmp_path: Path,
    options: str | None,
    observed: dict[str, object],
    message: str,
) -> None:
    inventory = _inventory(tmp_path)
    evidence = _evidence(inventory)
    evidence["nodeOptions"] = options

    checks = _checks(tmp_path, inventory, evidence)

    configuration = checks["kibana.configuration[search-kibana]"]
    assert configuration.status is AuditStatus.DRIFT
    assert cast("dict[str, object]", configuration.observed)["node.options"] == (
        observed
    )
    assert configuration.message == message


def test_a_missing_kibana_yml_is_drift(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    evidence = _evidence(inventory)
    evidence["configuration"] = None

    checks = _checks(tmp_path, inventory, evidence)

    configuration = checks["kibana.configuration[search-kibana]"]
    assert configuration.status is AuditStatus.DRIFT
    assert configuration.message == "kibana.yml is missing"


@pytest.mark.parametrize(
    ("level", "message"),
    [
        pytest.param("degraded", "Kibana reports itself degraded", id="degraded"),
        pytest.param(None, "Kibana did not answer GET /api/status", id="no answer"),
    ],
)
def test_a_kibana_that_does_not_report_itself_available_is_drift(
    tmp_path: Path, level: str | None, message: str
) -> None:
    inventory = _inventory(tmp_path)
    evidence = _evidence(inventory)
    evidence["status"] = {"read": True, "level": level}

    checks = _checks(tmp_path, inventory, evidence)

    status = checks["kibana.status[search-kibana]"]
    assert status.status is AuditStatus.DRIFT
    assert status.message == message


def test_a_kibana_inspect_could_not_ask_is_unknown(tmp_path: Path) -> None:
    inventory = _inventory(tmp_path)
    evidence = _evidence(inventory)
    evidence["status"] = {"read": False, "level": None}
    evidence["roles"] = None
    evidence["userRoles"] = None

    checks = _checks(tmp_path, inventory, evidence)

    for name in ("status", "roles", "users"):
        assert checks[f"kibana.{name}[search-kibana]"].status is AuditStatus.UNKNOWN


def _feature_privilege(role: dict[str, object]) -> None:
    kibana = cast("list[dict[str, object]]", role["kibana"])
    kibana[0]["base"] = []
    kibana[0]["feature"] = {"dashboard": ["all"]}


def _index_privilege(role: dict[str, object]) -> None:
    elasticsearch = cast("dict[str, object]", role["elasticsearch"])
    elasticsearch["indices"] = [{"names": ["*"], "privileges": ["read"]}]


def _one_space(role: dict[str, object]) -> None:
    cast("list[dict[str, object]]", role["kibana"])[0]["spaces"] = ["default"]


def _missing(role: dict[str, object]) -> None:
    role.update(found=False, elasticsearch=None, kibana=None, metadata=None)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(
            _feature_privilege,
            f"{VIEW} differs from what the role writes",
            id="a feature privilege",
        ),
        pytest.param(
            _index_privilege,
            f"{VIEW} differs from what the role writes",
            id="an index privilege",
        ),
        pytest.param(
            _one_space, f"{VIEW} differs from what the role writes", id="one space"
        ),
        pytest.param(_missing, f"{VIEW} is missing", id="deleted"),
    ],
)
def test_a_kibana_role_changed_by_hand_is_drift(
    tmp_path: Path, change: Callable[[dict[str, object]], None], message: str
) -> None:
    inventory = _inventory(tmp_path)
    evidence = _evidence(inventory)
    roles = cast("list[dict[str, object]]", evidence["roles"])
    change(roles[0])

    checks = _checks(tmp_path, inventory, evidence)

    check = checks["kibana.roles[search-kibana]"]
    assert check.status is AuditStatus.DRIFT
    assert check.message == message


@pytest.mark.parametrize(
    ("keystore", "message"),
    [
        pytest.param(
            [TOKEN_SETTING, *KEYS[1:]],
            "the keystore lacks xpack.encryptedSavedObjects.encryptionKey",
            id="a key missing",
        ),
        pytest.param(None, "Kibana's keystore could not be listed", id="no keystore"),
    ],
)
def test_a_key_missing_from_the_keystore_is_an_unmet_requirement(
    tmp_path: Path, keystore: list[str] | None, message: str
) -> None:
    inventory = _inventory(tmp_path)
    evidence = _evidence(inventory)
    evidence["keystore"] = keystore

    checks = _checks(tmp_path, inventory, evidence)

    check = checks["kibana.keystore[search-kibana]"]
    assert check.status is AuditStatus.DRIFT
    assert check.message == message


def test_an_observation_without_kibana_evidence_is_unknown_not_drift(
    tmp_path: Path,
) -> None:
    inventory = _inventory(tmp_path)

    checks = _checks(tmp_path, inventory, None)

    assert {
        name: status
        for name, status in _statuses(checks).items()
        if status is not AuditStatus.COMPLIANT
    } == {
        f"kibana.{name}[search-kibana]": AuditStatus.UNKNOWN
        for name in ("configuration", "status", "roles", "users", "keystore")
    }


def test_an_elasticsearch_only_project_has_no_kibana_check(tmp_path: Path) -> None:
    project = write_elasticsearch_project(tmp_path / "project", elasticsearch_service())
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    report = _report(tmp_path, inventory, None)

    (audit,) = report.servers
    assert not [check for check in audit.checks if check.check.startswith("kibana.")]
