"""S-019: audit drift of a MariaDB service's server (S-019-19).

The installed version, the unit, the managed file, and the server's role
against the declaration, from the evidence inspect collects.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from cloudfall.audit import AuditCheck, AuditStatus, audit_inventory
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.validation import validate_config
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from mariadb_project import (
    SCHEMAS,
    mariadb_service,
    primary_replication,
    replica_service,
    write_project,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloudfall.inventory import ServiceInventory

ROOT = Path(__file__).parents[2]
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_mariadb"
SETTINGS_PATH = "/etc/mysql/mariadb.conf.d/90-cloudfall.cnf"
RUNNING = {"ioRunning": "Yes", "sqlRunning": "Yes", "sourceServerId": 1}


def _rendered_settings(service: ServiceInventory) -> str:
    """Render the managed file as the role writes it for ``service``."""
    environment = Environment(
        loader=FileSystemLoader(ROLE / "templates"),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - an option file is not HTML.
        keep_trailing_newline=True,
    )
    replication = service.mariadb.replication if service.mariadb else None
    return environment.get_template("90-cloudfall.cnf.j2").render(
        cloudfall_mariadb_service=service.as_dict(),
        cloudfall_mariadb_data_path="/var/lib/mysql",
        cloudfall_mariadb_certificate_path="/etc/mysql/cloudfall/server.crt",
        cloudfall_mariadb_key_path="/etc/mysql/cloudfall/server.key",
        cloudfall_mariadb_ca_path="/etc/mysql/cloudfall/ca.crt",
        cloudfall_mariadb_read_only=replication is not None
        and replication.role.value == "replica",
    )


def _pair(tmp_path: Path) -> PlatformInventory:
    project = write_project(
        tmp_path / "project",
        mariadb_service(replication=primary_replication()),
        replica_service(),
        servers=("h1", "h2"),
    )
    return PlatformInventory.from_state(validate_config(project, SCHEMAS))


def _standalone(tmp_path: Path) -> PlatformInventory:
    project = write_project(tmp_path / "project", mariadb_service())
    return PlatformInventory.from_state(validate_config(project, SCHEMAS))


def _service(inventory: PlatformInventory, service_id: str) -> ServiceInventory:
    return next(
        service
        for service in inventory.services
        if service.resource_id.value == service_id
    )


def _evidence(
    inventory: PlatformInventory,
    service_id: str,
    *,
    read_only: bool,
    replication: dict[str, object] | None,
) -> dict[str, object]:
    """Return what inspect reads from a server converged as declared."""
    return {
        "service": service_id,
        "configurationPath": SETTINGS_PATH,
        "configuration": _rendered_settings(_service(inventory, service_id)),
        "available": True,
        "readOnly": read_only,
        "replication": replication,
    }


def _checks(
    tmp_path: Path,
    inventory: PlatformInventory,
    server: str,
    evidence: dict[str, object] | None,
    host: dict[str, object] | None = None,
) -> dict[str, AuditCheck]:
    """Audit ``server`` with the compliant fixture and the MariaDB evidence.

    ``host`` replaces the installed package's version (``package``) or
    fields of the unit (``unit``).
    """
    host = host or {}
    document = json.loads((COMPLIANT / f"{server}.json").read_text(encoding="utf-8"))
    spec = document["spec"]
    spec["packages"].append(
        {
            "name": "mariadb-server",
            "version": host.get("package", "1:11.8.9+maria~deb13"),
            "source": "apt",
            "arch": "amd64",
        }
    )
    spec["services"]["mariadb.service"] = {
        "name": "mariadb.service",
        "state": "running",
        "status": "enabled",
        "source": "systemd",
        **cast("dict[str, str]", host.get("unit", {})),
    }
    if evidence is not None:
        spec["mariadb"] = evidence
    observations = tmp_path / "observed"
    observations.mkdir(parents=True)
    (observations / f"{server}.json").write_text(json.dumps(document), encoding="utf-8")
    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))
    (audit,) = (item for item in report.servers if item.server_id == server)
    return {
        check.check: check
        for check in audit.checks
        if check.check.startswith("mariadb.") and not check.check.startswith(
            "mariadb.accounts"
        )
    }


def _statuses(checks: dict[str, AuditCheck]) -> dict[str, AuditStatus]:
    return {name: check.status for name, check in checks.items()}


def test_a_standalone_server_converged_as_declared_is_compliant(
    tmp_path: Path,
) -> None:
    inventory = _standalone(tmp_path)
    evidence = _evidence(inventory, "mariadb-main", read_only=False, replication=None)

    checks = _checks(tmp_path, inventory, "h1", evidence)

    assert _statuses(checks) == {
        "mariadb.version[mariadb-main]": AuditStatus.COMPLIANT,
        "mariadb.series[mariadb-main]": AuditStatus.COMPLIANT,
        "mariadb.unit[mariadb-main]": AuditStatus.COMPLIANT,
        "mariadb.configuration[mariadb-main]": AuditStatus.COMPLIANT,
        "mariadb.role[mariadb-main]": AuditStatus.COMPLIANT,
    }


def test_a_replica_converged_as_declared_is_compliant_and_reports_its_lag(
    tmp_path: Path,
) -> None:
    inventory = _pair(tmp_path)
    evidence = _evidence(
        inventory,
        "mariadb-replica",
        read_only=True,
        replication={**RUNNING, "secondsBehindMaster": 4},
    )

    checks = _checks(tmp_path, inventory, "h2", evidence)

    assert set(_statuses(checks).values()) == {AuditStatus.COMPLIANT}
    lag = checks["mariadb.lag[mariadb-replica]"]
    assert lag.observed == {"seconds": 4}
    assert lag.message == "the replica is 4 seconds behind its primary"


# proves: S-019-19
def test_s019_19_an_installed_version_other_than_the_declared_one_is_drift(
    tmp_path: Path,
) -> None:
    inventory = _standalone(tmp_path)
    evidence = _evidence(inventory, "mariadb-main", read_only=False, replication=None)

    checks = _checks(
        tmp_path, inventory, "h1", evidence, {"package": "1:11.8.8+maria~deb13"}
    )

    version = checks["mariadb.version[mariadb-main]"]
    assert version.status is AuditStatus.DRIFT
    assert version.desired == {"version": "11.8.9"}
    assert version.observed == {"versions": ["1:11.8.8+maria~deb13"]}


# proves: S-019-19
@pytest.mark.parametrize(
    "unit",
    [
        pytest.param({"state": "stopped"}, id="stopped"),
        pytest.param({"status": "disabled"}, id="disabled"),
    ],
)
def test_s019_19_a_stopped_or_disabled_unit_is_drift(
    tmp_path: Path, unit: dict[str, str]
) -> None:
    inventory = _standalone(tmp_path)
    evidence = _evidence(inventory, "mariadb-main", read_only=False, replication=None)

    checks = _checks(tmp_path, inventory, "h1", evidence, {"unit": unit})

    assert checks["mariadb.unit[mariadb-main]"].status is AuditStatus.DRIFT


# proves: S-019-19
@pytest.mark.parametrize(
    ("edit", "found"),
    [
        pytest.param(
            lambda text: text.replace(
                "innodb_buffer_pool_size = 4096M", "innodb_buffer_pool_size = 8192M"
            ),
            {"innodb_buffer_pool_size": "8192M"},
            id="a changed setting",
        ),
        pytest.param(
            lambda text: text + "max_connections = 500\n",
            {"max_connections": "500"},
            id="an added setting",
        ),
        pytest.param(
            lambda text: text.replace("ignore-db-dirs = lost+found\n", ""),
            {},
            id="a removed setting",
        ),
    ],
)
def test_s019_19_a_managed_file_changed_by_hand_is_drift(
    tmp_path: Path, edit: Callable[[str], str], found: dict[str, str]
) -> None:
    inventory = _standalone(tmp_path)
    evidence = _evidence(inventory, "mariadb-main", read_only=False, replication=None)
    evidence["configuration"] = edit(cast("str", evidence["configuration"]))

    checks = _checks(tmp_path, inventory, "h1", evidence)

    configuration = checks["mariadb.configuration[mariadb-main]"]
    assert configuration.status is AuditStatus.DRIFT
    observed = cast("dict[str, dict[str, str]]", configuration.observed)["mariadbd"]
    for key, value in found.items():
        assert observed[key] == value
    if not found:
        assert "ignore_db_dirs" not in observed


def test_a_comment_changed_in_the_managed_file_is_no_drift(tmp_path: Path) -> None:
    inventory = _standalone(tmp_path)
    evidence = _evidence(inventory, "mariadb-main", read_only=False, replication=None)
    evidence["configuration"] = "# a note\n" + cast("str", evidence["configuration"])

    checks = _checks(tmp_path, inventory, "h1", evidence)

    assert checks["mariadb.configuration[mariadb-main]"].status is (
        AuditStatus.COMPLIANT
    )


def test_a_missing_managed_file_is_drift(tmp_path: Path) -> None:
    inventory = _standalone(tmp_path)
    evidence = _evidence(inventory, "mariadb-main", read_only=False, replication=None)
    evidence["configuration"] = None

    checks = _checks(tmp_path, inventory, "h1", evidence)

    configuration = checks["mariadb.configuration[mariadb-main]"]
    assert configuration.status is AuditStatus.DRIFT
    assert configuration.observed == {"present": False}


# proves: S-019-19
def test_s019_19_a_replica_made_writable_by_hand_is_drift(tmp_path: Path) -> None:
    inventory = _pair(tmp_path)
    evidence = _evidence(
        inventory,
        "mariadb-replica",
        read_only=False,
        replication={**RUNNING, "secondsBehindMaster": 0},
    )

    checks = _checks(tmp_path, inventory, "h2", evidence)

    role = checks["mariadb.role[mariadb-replica]"]
    assert role.status is AuditStatus.DRIFT
    assert role.message == "the replica is writable: read_only is off"
    # The lag is read as it is: only the role is drift
    assert checks["mariadb.lag[mariadb-replica]"].status is AuditStatus.COMPLIANT


# proves: S-019-19
@pytest.mark.parametrize(
    ("replication", "message"),
    [
        pytest.param(
            {**RUNNING, "ioRunning": "No", "sqlRunning": "No"},
            "replication is stopped: IO thread No, SQL thread No",
            id="both threads stopped",
        ),
        pytest.param(
            {**RUNNING, "sqlRunning": "No"},
            "replication is stopped: SQL thread No",
            id="the SQL thread stopped",
        ),
        pytest.param(
            {**RUNNING, "ioRunning": "Connecting"},
            "replication is stopped: IO thread Connecting",
            id="the IO thread connecting",
        ),
        pytest.param(None, "the replica replicates from nowhere", id="no link"),
    ],
)
def test_s019_19_a_replica_whose_replication_is_stopped_is_drift(
    tmp_path: Path, replication: dict[str, object] | None, message: str
) -> None:
    inventory = _pair(tmp_path)
    link = (
        None if replication is None else {**replication, "secondsBehindMaster": None}
    )
    evidence = _evidence(inventory, "mariadb-replica", read_only=True, replication=link)

    checks = _checks(tmp_path, inventory, "h2", evidence)

    role = checks["mariadb.role[mariadb-replica]"]
    assert role.status is AuditStatus.DRIFT
    assert role.message == message
    # A stopped link has no lag: unknown, not a number
    assert checks["mariadb.lag[mariadb-replica]"].status is AuditStatus.UNKNOWN


def test_a_replica_more_than_five_minutes_behind_is_a_finding(
    tmp_path: Path,
) -> None:
    inventory = _pair(tmp_path)
    behind = _evidence(
        inventory,
        "mariadb-replica",
        read_only=True,
        replication={**RUNNING, "secondsBehindMaster": 301},
    )
    at_limit = copy.deepcopy(behind)
    cast("dict[str, object]", at_limit["replication"])["secondsBehindMaster"] = 300

    late = _checks(tmp_path / "late", inventory, "h2", behind)
    limit = _checks(tmp_path / "limit", inventory, "h2", at_limit)

    assert late["mariadb.lag[mariadb-replica]"].status is AuditStatus.DRIFT
    assert late["mariadb.role[mariadb-replica]"].status is AuditStatus.COMPLIANT
    assert limit["mariadb.lag[mariadb-replica]"].status is AuditStatus.COMPLIANT


def test_a_primary_that_replicates_from_somewhere_is_drift(tmp_path: Path) -> None:
    inventory = _pair(tmp_path)
    evidence = _evidence(
        inventory,
        "mariadb-main",
        read_only=False,
        replication={**RUNNING, "sourceServerId": 7, "secondsBehindMaster": 0},
    )

    checks = _checks(tmp_path, inventory, "h1", evidence)

    role = checks["mariadb.role[mariadb-main]"]
    assert role.status is AuditStatus.DRIFT
    assert role.message == "the primary server replicates from server id 7"
    # A primary has no lag finding
    assert "mariadb.lag[mariadb-main]" not in checks


def test_the_role_is_unknown_when_the_server_was_not_read(tmp_path: Path) -> None:
    inventory = _pair(tmp_path)
    evidence = _evidence(inventory, "mariadb-replica", read_only=True, replication=None)
    evidence |= {"available": False, "readOnly": None}

    checks = _checks(tmp_path, inventory, "h2", evidence)

    assert checks["mariadb.role[mariadb-replica]"].status is AuditStatus.UNKNOWN
    assert checks["mariadb.lag[mariadb-replica]"].status is AuditStatus.UNKNOWN
    # The file is read without the server
    assert checks["mariadb.configuration[mariadb-replica]"].status is (
        AuditStatus.COMPLIANT
    )


def _bind(
    tmp_path: Path, inventory: PlatformInventory, server: str, *listeners: str
) -> AuditCheck:
    """Audit ``server`` listening on ``listeners`` and return its MariaDB bind."""
    document = json.loads((COMPLIANT / f"{server}.json").read_text(encoding="utf-8"))
    sockets = "".join(
        f"\ntcp   LISTEN 0 80     {listener}  0.0.0.0:*" for listener in listeners
    )
    document["spec"]["network"]["listeningSockets"] += sockets
    observations = tmp_path / "observed"
    observations.mkdir(parents=True)
    (observations / f"{server}.json").write_text(json.dumps(document), encoding="utf-8")
    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))
    (audit,) = (item for item in report.servers if item.server_id == server)
    (bind,) = (
        check for check in audit.checks if check.check.startswith("services.bind[maria")
    )
    return bind


# proves: S-019-12
def test_s019_12_a_pair_listening_on_its_replication_address_is_compliant(
    tmp_path: Path,
) -> None:
    inventory = _pair(tmp_path)

    primary = _bind(
        tmp_path / "h1", inventory, "h1", "127.0.0.1:3306", "203.0.113.11:3306"
    )
    replica = _bind(tmp_path / "h2", inventory, "h2", "127.0.0.1:3306")

    assert primary.check == "services.bind[mariadb-main]"
    assert primary.status is AuditStatus.COMPLIANT
    assert primary.desired == {
        "protocol": "tcp",
        "address": "127.0.0.1",
        "port": 3306,
        "loopbackOnly": False,
        "replication": {"listenAddress": "203.0.113.11"},
    }
    assert replica.check == "services.bind[mariadb-replica]"
    assert replica.status is AuditStatus.COMPLIANT


# proves: S-019-3
@pytest.mark.parametrize(
    "stray",
    [
        pytest.param("0.0.0.0:3306", id="IPv4 wildcard"),
        pytest.param("[::]:3306", id="IPv6 wildcard"),
        pytest.param("[2001:db8::11]:3306", id="an IPv6 address"),
        pytest.param("10.0.0.5:3306", id="another IPv4 address"),
    ],
)
def test_s019_3_a_primary_listening_beyond_its_declared_addresses_is_drift(
    tmp_path: Path, stray: str
) -> None:
    inventory = _pair(tmp_path)

    bind = _bind(
        tmp_path, inventory, "h1", "127.0.0.1:3306", "203.0.113.11:3306", stray
    )

    assert bind.status is AuditStatus.DRIFT


def test_a_standalone_server_listening_on_a_public_address_is_drift(
    tmp_path: Path,
) -> None:
    inventory = _standalone(tmp_path)

    bind = _bind(tmp_path, inventory, "h1", "127.0.0.1:3306", "203.0.113.11:3306")

    assert bind.status is AuditStatus.DRIFT


def test_an_observation_without_server_evidence_is_unknown(tmp_path: Path) -> None:
    inventory = _standalone(tmp_path)

    checks = _checks(tmp_path, inventory, "h1", None)

    assert checks["mariadb.version[mariadb-main]"].status is AuditStatus.COMPLIANT
    assert checks["mariadb.configuration[mariadb-main]"].status is (
        AuditStatus.UNKNOWN
    )
    assert checks["mariadb.role[mariadb-main]"].status is AuditStatus.UNKNOWN
