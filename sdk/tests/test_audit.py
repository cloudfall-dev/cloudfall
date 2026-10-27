"""Desired-versus-observed server audit tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from cloudfall.audit import AuditCheck, AuditReport, AuditStatus, audit_inventory
from cloudfall.cli import main
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.validation import ConfigValidationError, validate_config

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"


def _audit(observation_directory: Path) -> AuditReport:
    inventory = PlatformInventory.from_state(validate_config(EXAMPLES, SCHEMAS))
    observations = load_observations(observation_directory, SCHEMAS)
    return audit_inventory(inventory, observations)


def test_compliant_observations_match_desired_state() -> None:
    report = _audit(COMPLIANT)

    assert report.status is AuditStatus.COMPLIANT
    assert all(server.status is AuditStatus.COMPLIANT for server in report.servers)
    assert report.unmatched_observations == ()


def test_one_healthy_array_does_not_hide_a_degraded_one(tmp_path: Path) -> None:
    """A host with a degraded mirror is running unprotected.

    Found on the live fleet on 2026-09-22: h3 carried two arrays at
    ``[2/1] [U_]`` and the audit called the host compliant on RAID,
    because it read the healthiest array instead of the worst one.
    """
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    document = json.loads(h1.read_text(encoding="utf-8"))
    document["spec"]["storage"]["softwareRaid"]["mdstat"] = (
        "md0 : active raid1 sda1[0] sdb1[1]\n"
        "      976630336 blocks [2/2] [UU]\n"
        "\n"
        "md1 : active raid1 sda2[0] sdb2[1]\n"
        "      976630336 blocks [2/1] [U_]\n"
    )
    h1.write_text(json.dumps(document), encoding="utf-8")

    report = _audit(observations)

    degraded = next(
        server for server in report.servers if server.server_id == "h1"
    )
    active = next(
        check
        for check in degraded.checks
        if check.check == "storage.softwareRaid.activeDevices"
    )
    assert active.status is AuditStatus.DRIFT
    assert active.observed == 1
    assert report.status is AuditStatus.DRIFT


def test_observed_systemd_escaped_unit_names_are_accepted(tmp_path: Path) -> None:
    r"""Observed units may carry systemd path escapes such as ``\x2d``.

    The 2026-09-08 Hetzner proving run failed observation validation because
    a real host reported ``systemd-fsck@dev-disk-by\x2duuid-....service``,
    which the declared-state unit-name pattern rejects.
    """
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    snapshot = json.loads(h1.read_text(encoding="utf-8"))
    escaped = "systemd-fsck@dev-disk-by\\x2duuid-E079\\x2d7D41.service"
    snapshot["spec"]["services"][escaped] = {
        "name": escaped,
        "state": "stopped",
        "status": "static",
        "source": "systemd",
    }
    h1.write_text(json.dumps(snapshot), encoding="utf-8")

    report = _audit(observations)

    assert report.status is AuditStatus.COMPLIANT


def test_audit_reports_raid_package_service_and_config_drift(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    drifted = (
        h1.read_text(encoding="utf-8")
        .replace("[UU]", "[U_]")
        .replace('"name": "curl"', '"name": "curl-missing"')
        .replace('"state": "running"', '"state": "stopped"')
        .replace('"mode": "0644"', '"mode": "0600"')
    )
    h1.write_text(drifted, encoding="utf-8")

    report = _audit(observations)
    h1_report = next(server for server in report.servers if server.server_id == "h1")
    drift_checks = {
        check.check
        for check in h1_report.checks
        if check.status is AuditStatus.DRIFT
    }

    assert report.status is AuditStatus.DRIFT
    assert {
        "storage.softwareRaid.activeDevices",
        "packages.required[curl]",
        "services.required[ssh.service]",
        "configuration.files[/etc/ssh/sshd_config]",
    } <= drift_checks

    exit_code = main(
        [
            "audit",
            "--project",
            str(EXAMPLES),
            "--schemas",
            str(SCHEMAS),
            "--observed",
            str(observations),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 83
    document = json.loads(captured.out)
    assert (document["ok"], document["error"]["code"]) == (False, "DRIFT")
    assert document["data"]["status"] == "drift"
    assert document["data"]["summary"]["servers"]["drift"] == 1
    assert captured.err == ""


def test_audit_reports_firewall_and_timer_drift(tmp_path: Path) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    content = json.loads(h1.read_text(encoding="utf-8"))
    spec = content["spec"]
    table = json.loads(spec["firewall"]["managedTableJson"])
    table["nftables"] = [
        entry
        for entry in table["nftables"]
        if entry.get("rule", {}).get("handle") != 9
    ]
    for entry in table["nftables"]:
        chain = entry.get("chain")
        if chain is not None and chain["name"] == "input":
            chain["policy"] = "accept"
    spec["firewall"]["managedTableJson"] = json.dumps(table)
    spec["timers"]["apt-daily.timer"]["status"] = "disabled"
    h1.write_text(json.dumps(content), encoding="utf-8")

    report = _audit(observations)
    h1_report = next(server for server in report.servers if server.server_id == "h1")
    drift_checks = {
        check.check
        for check in h1_report.checks
        if check.status is AuditStatus.DRIFT
    }

    assert report.status is AuditStatus.DRIFT
    assert {
        "firewall.inputPolicy",
        "firewall.allowedInbound",
        "services.required[apt-daily.timer]",
    } <= drift_checks
    allowed = next(
        check
        for check in h1_report.checks
        if check.check == "firewall.allowedInbound"
    )
    assert allowed.desired == ["tcp/22", "tcp/80", "tcp/443"]
    assert allowed.observed == ["tcp/22", "tcp/80"]


def test_audit_reports_missing_firewall_table_as_drift(tmp_path: Path) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    content = json.loads(h1.read_text(encoding="utf-8"))
    content["spec"]["firewall"] = {
        "nftAvailable": False,
        "managedTableJson": None,
    }
    h1.write_text(json.dumps(content), encoding="utf-8")

    report = _audit(observations)
    h1_report = next(server for server in report.servers if server.server_id == "h1")
    drift_checks = {
        check.check
        for check in h1_report.checks
        if check.status is AuditStatus.DRIFT
    }

    assert {
        "firewall.managedTable",
        "firewall.inputPolicy",
        "firewall.allowedInbound",
    } <= drift_checks


def test_audit_reports_missing_alert_evidence_as_drift(tmp_path: Path) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    content = json.loads(h1.read_text(encoding="utf-8"))
    del content["spec"]["alerting"]
    h1.write_text(json.dumps(content), encoding="utf-8")

    report = _audit(observations)
    h1_report = next(server for server in report.servers if server.server_id == "h1")
    check = next(
        check
        for check in h1_report.checks
        if check.check == "alerting.rules[postgresql-down]"
    )

    assert check.status is AuditStatus.DRIFT
    assert check.observed is None


def test_audit_reports_unloaded_alert_rule_as_drift(tmp_path: Path) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    content = json.loads(h1.read_text(encoding="utf-8"))
    content["spec"]["alerting"]["rulesJson"] = json.dumps(
        {"status": "success", "data": {"groups": []}}
    )
    h1.write_text(json.dumps(content), encoding="utf-8")

    report = _audit(observations)
    h1_report = next(server for server in report.servers if server.server_id == "h1")
    check = next(
        check
        for check in h1_report.checks
        if check.check == "alerting.rules[postgresql-down]"
    )

    assert check.status is AuditStatus.DRIFT


def test_audit_reports_unhealthy_alert_rule_as_drift(tmp_path: Path) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    content = json.loads(h1.read_text(encoding="utf-8"))
    content["spec"]["alerting"]["rulesJson"] = json.dumps(
        {
            "status": "success",
            "data": {
                "groups": [
                    {
                        "name": "cloudfall",
                        "rules": [
                            {
                                "name": "postgresql_down",
                                "type": "alerting",
                                "state": "inactive",
                                "health": "err",
                            }
                        ],
                    }
                ]
            },
        }
    )
    h1.write_text(json.dumps(content), encoding="utf-8")

    report = _audit(observations)
    h1_report = next(server for server in report.servers if server.server_id == "h1")
    check = next(
        check
        for check in h1_report.checks
        if check.check == "alerting.rules[postgresql-down]"
    )

    assert check.status is AuditStatus.DRIFT
    assert isinstance(check.observed, dict)
    assert check.observed["health"] == "err"


def test_audit_accepts_firing_declared_alert_as_compliant(
    tmp_path: Path,
) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    content = json.loads(h1.read_text(encoding="utf-8"))
    content["spec"]["alerting"]["rulesJson"] = json.dumps(
        {
            "status": "success",
            "data": {
                "groups": [
                    {
                        "name": "cloudfall",
                        "rules": [
                            {
                                "name": "postgresql_down",
                                "type": "alerting",
                                "state": "firing",
                                "health": "ok",
                            }
                        ],
                    }
                ]
            },
        }
    )
    h1.write_text(json.dumps(content), encoding="utf-8")

    report = _audit(observations)
    h1_report = next(server for server in report.servers if server.server_id == "h1")
    check = next(
        check
        for check in h1_report.checks
        if check.check == "alerting.rules[postgresql-down]"
    )

    assert check.status is AuditStatus.COMPLIANT
    assert isinstance(check.observed, dict)
    assert check.observed["state"] == "firing"


def test_audit_reports_wildcard_service_exposure_as_drift(
    tmp_path: Path,
) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    content = json.loads(h1.read_text(encoding="utf-8"))
    network = content["spec"]["network"]
    network["listeningSockets"] = network["listeningSockets"].replace(
        "127.0.0.1:5432", "0.0.0.0:5432"
    )
    h1.write_text(json.dumps(content), encoding="utf-8")

    report = _audit(observations)
    h1_report = next(server for server in report.servers if server.server_id == "h1")
    bind_check = next(
        check
        for check in h1_report.checks
        if check.check == "services.bind[postgresql-main]"
    )

    assert report.status is AuditStatus.DRIFT
    assert bind_check.status is AuditStatus.DRIFT
    assert bind_check.observed == {"listeners": ["0.0.0.0:5432"]}


def test_audit_reports_missing_service_listener_as_drift(
    tmp_path: Path,
) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    content = json.loads(h1.read_text(encoding="utf-8"))
    network = content["spec"]["network"]
    network["listeningSockets"] = "\n".join(
        line
        for line in network["listeningSockets"].splitlines()
        if ":5432" not in line
    )
    h1.write_text(json.dumps(content), encoding="utf-8")

    report = _audit(observations)
    h1_report = next(server for server in report.servers if server.server_id == "h1")
    bind_check = next(
        check
        for check in h1_report.checks
        if check.check == "services.bind[postgresql-main]"
    )

    assert bind_check.status is AuditStatus.DRIFT
    assert bind_check.observed is None


REMOTE_SOURCES = ["10.0.0.0/24", "10.1.0.7/32", "2001:db8::/64"]
REMOTE_LISTENER = "tcp   LISTEN 0 244    10.0.0.2:5432  0.0.0.0:*"


def _remote_access_project(tmp_path: Path) -> Path:
    """Copy the examples and open postgresql-main to declared sources."""
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    path = project / "services" / "postgresql-main.yaml"
    service = yaml.safe_load(path.read_text(encoding="utf-8"))
    postgresql = service["spec"]["postgresql"]
    postgresql["databases"].append(
        {
            "name": "billing",
            "owner": "billing",
            "passwordRef": {
                "environment": "production",
                "path": "/billing/postgresql",
                "key": "BILLING_PASSWORD",
            },
        }
    )
    postgresql["remoteAccess"] = {
        "listenAddress": "10.0.0.2",
        "allowedSources": REMOTE_SOURCES,
    }
    path.write_text(yaml.safe_dump(service, sort_keys=False), encoding="utf-8")
    return project


def _saddr_rule(
    handle: int, source: object | None, family: str = "ip"
) -> dict[str, object]:
    """One accepted rule as ``nft -j`` prints ``ip saddr <source> tcp dport 5432``.

    Without a source it is the rule ``tcp dport 5432 accept``.
    """
    saddr: list[dict[str, object]] = (
        []
        if source is None
        else [
            {
                "match": {
                    "op": "==",
                    "left": {"payload": {"protocol": family, "field": "saddr"}},
                    "right": source,
                }
            }
        ]
    )
    return {
        "rule": {
            "family": "inet",
            "table": "cloudfall",
            "chain": "input",
            "handle": handle,
            "comment": "postgresql-main remoteAccess",
            "expr": [
                *saddr,
                {
                    "match": {
                        "op": "==",
                        "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                        "right": 5432,
                    }
                },
                {"accept": None},
            ],
        }
    }


def _rendered_remote_access_rules() -> list[dict[str, object]]:
    """Return the rules nftables holds for ``REMOTE_SOURCES``, a /32 bare."""
    return [
        _saddr_rule(10, {"prefix": {"addr": "10.0.0.0", "len": 24}}),
        _saddr_rule(11, "10.1.0.7"),
        _saddr_rule(12, {"prefix": {"addr": "2001:db8::", "len": 64}}, "ip6"),
    ]


def _remote_access_observations(
    tmp_path: Path,
    rules: list[dict[str, object]],
    listeners: tuple[str, ...] = (REMOTE_LISTENER,),
) -> Path:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    content = json.loads(h1.read_text(encoding="utf-8"))
    spec = content["spec"]
    table = json.loads(spec["firewall"]["managedTableJson"])
    table["nftables"].extend(rules)
    spec["firewall"]["managedTableJson"] = json.dumps(table)
    for listener in listeners:
        spec["network"]["listeningSockets"] += f"\n{listener}"
    h1.write_text(json.dumps(content), encoding="utf-8")
    return observations


def _h1_check(project: Path, observations: Path, name: str) -> AuditCheck:
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))
    h1_report = next(server for server in report.servers if server.server_id == "h1")
    return next(check for check in h1_report.checks if check.check == name)


def test_remote_access_rules_and_listener_audit_clean(tmp_path: Path) -> None:
    """Regression for #143: the declared remoteAccess was reported as drift."""
    project = _remote_access_project(tmp_path)
    observations = _remote_access_observations(
        tmp_path, _rendered_remote_access_rules()
    )
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))

    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))

    assert report.status is AuditStatus.COMPLIANT
    allowed = _h1_check(project, observations, "firewall.allowedInbound")
    assert allowed.desired == [
        "tcp/22",
        "tcp/80",
        "tcp/443",
        "tcp/5432 from 10.0.0.0/24",
        "tcp/5432 from 10.1.0.7/32",
        "tcp/5432 from 2001:db8::/64",
    ]
    assert allowed.observed == allowed.desired
    bind = _h1_check(project, observations, "services.bind[postgresql-main]")
    assert bind.desired == {
        "protocol": "tcp",
        "address": "127.0.0.1",
        "port": 5432,
        "loopbackOnly": False,
        "remoteAccess": {"listenAddress": "10.0.0.2"},
    }
    assert bind.observed == {"listeners": ["127.0.0.1:5432", "10.0.0.2:5432"]}


def test_remote_access_port_open_to_every_address_is_drift(tmp_path: Path) -> None:
    project = _remote_access_project(tmp_path)
    observations = _remote_access_observations(
        tmp_path, [*_rendered_remote_access_rules(), _saddr_rule(13, None)]
    )

    allowed = _h1_check(project, observations, "firewall.allowedInbound")

    assert allowed.status is AuditStatus.DRIFT
    assert isinstance(allowed.observed, list)
    assert "tcp/5432" in allowed.observed


def test_remote_access_from_a_wrong_source_is_drift(tmp_path: Path) -> None:
    project = _remote_access_project(tmp_path)
    rules = _rendered_remote_access_rules()
    rules[0] = _saddr_rule(10, {"prefix": {"addr": "10.0.1.0", "len": 24}})
    observations = _remote_access_observations(tmp_path, rules)

    allowed = _h1_check(project, observations, "firewall.allowedInbound")

    assert allowed.status is AuditStatus.DRIFT
    assert isinstance(allowed.observed, list)
    assert "tcp/5432 from 10.0.1.0/24" in allowed.observed
    assert "tcp/5432 from 10.0.0.0/24" not in allowed.observed


@pytest.mark.parametrize(
    "listener",
    [
        "tcp   LISTEN 0 244    10.0.0.3:5432  0.0.0.0:*",
        "tcp   LISTEN 0 244    0.0.0.0:5432  0.0.0.0:*",
    ],
)
def test_remote_access_listener_on_an_undeclared_address_is_drift(
    tmp_path: Path, listener: str
) -> None:
    project = _remote_access_project(tmp_path)
    observations = _remote_access_observations(
        tmp_path, _rendered_remote_access_rules(), (REMOTE_LISTENER, listener)
    )

    bind = _h1_check(project, observations, "services.bind[postgresql-main]")

    assert bind.status is AuditStatus.DRIFT


def test_remote_access_without_its_listener_is_drift(tmp_path: Path) -> None:
    """A PostgreSQL that failed to bind listenAddress has remote access down."""
    project = _remote_access_project(tmp_path)
    observations = _remote_access_observations(
        tmp_path, _rendered_remote_access_rules(), ()
    )

    bind = _h1_check(project, observations, "services.bind[postgresql-main]")

    assert bind.status is AuditStatus.DRIFT
    assert bind.observed == {"listeners": ["127.0.0.1:5432"]}


def test_a_server_without_remote_access_audits_as_before() -> None:
    report = _audit(COMPLIANT)
    h1_report = next(server for server in report.servers if server.server_id == "h1")
    checks = {check.check: check for check in h1_report.checks}

    assert checks["firewall.allowedInbound"].desired == [
        "tcp/22",
        "tcp/80",
        "tcp/443",
    ]
    assert checks["firewall.allowedInbound"].observed == [
        "tcp/22",
        "tcp/80",
        "tcp/443",
    ]
    assert checks["services.bind[postgresql-main]"].desired == {
        "protocol": "tcp",
        "address": "127.0.0.1",
        "port": 5432,
        "loopbackOnly": True,
    }
    assert checks["services.bind[postgresql-main]"].observed == {
        "listeners": ["127.0.0.1:5432"]
    }


def test_missing_observation_is_unknown(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    observations = tmp_path / "observed"
    observations.mkdir()
    shutil.copy2(COMPLIANT / "h1.json", observations / "h1.json")

    report = _audit(observations)

    assert report.status is AuditStatus.UNKNOWN
    h2_report = next(server for server in report.servers if server.server_id == "h2")
    assert h2_report.status is AuditStatus.UNKNOWN

    exit_code = main(
        [
            "audit",
            "--project",
            str(EXAMPLES),
            "--schemas",
            str(SCHEMAS),
            "--observed",
            str(observations),
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 84
    document = json.loads(captured.out)
    assert (document["ok"], document["error"]["code"]) == (False, "UNKNOWN")
    assert document["data"]["status"] == "unknown"
    assert captured.err == ""


def test_invalid_observation_fails_before_audit(tmp_path: Path) -> None:
    observations = tmp_path / "observed"
    observations.mkdir()
    invalid = (COMPLIANT / "h1.json").read_text(encoding="utf-8").replace(
        '"processorCount": 1', '"processorCount": -1'
    )
    (observations / "h1.json").write_text(invalid, encoding="utf-8")

    with pytest.raises(ConfigValidationError) as error:
        load_observations(observations, SCHEMAS)

    assert error.value.issue.code == "observation_schema_validation_failed"


def test_audit_cli_emits_json_and_compliance_exit_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "audit",
            "--project",
            str(EXAMPLES),
            "--schemas",
            str(SCHEMAS),
            "--observed",
            str(COMPLIANT),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out)["data"]["status"] == "compliant"
    assert captured.err == ""


def _environment_evidence(
    sha256: str | None, *, exists: bool = True
) -> dict[str, object]:
    return {
        "component": "crm-backend",
        "path": "/srv/apps/crm/backend/shared/env/crm-backend.env",
        "exists": exists,
        "sha256": sha256,
    }


def _audit_with_receipts(
    observation_directory: Path, receipts: dict[str, str]
) -> AuditReport:
    inventory = PlatformInventory.from_state(
        validate_config(EXAMPLES, SCHEMAS)
    )
    observations = load_observations(observation_directory, SCHEMAS)
    return audit_inventory(inventory, observations, receipts)


def test_environment_file_matching_hash_is_compliant(tmp_path: Path) -> None:
    expected = "a" * 64
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    content = json.loads(h1.read_text(encoding="utf-8"))
    content["spec"]["componentEnvironment"] = [_environment_evidence(expected)]
    h1.write_text(json.dumps(content), encoding="utf-8")

    report = _audit_with_receipts(observations, {"crm-backend": expected})
    h1_report = next(
        server for server in report.servers if server.server_id == "h1"
    )
    check = next(
        check
        for check in h1_report.checks
        if check.check == "environment.file[crm-backend]"
    )

    assert check.status is AuditStatus.COMPLIANT


def test_environment_file_hash_mismatch_is_drift(tmp_path: Path) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    content = json.loads(h1.read_text(encoding="utf-8"))
    content["spec"]["componentEnvironment"] = [
        _environment_evidence("b" * 64)
    ]
    h1.write_text(json.dumps(content), encoding="utf-8")

    report = _audit_with_receipts(observations, {"crm-backend": "a" * 64})
    h1_report = next(
        server for server in report.servers if server.server_id == "h1"
    )
    check = next(
        check
        for check in h1_report.checks
        if check.check == "environment.file[crm-backend]"
    )

    assert check.status is AuditStatus.DRIFT


def test_environment_file_missing_evidence_is_drift(tmp_path: Path) -> None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)

    report = _audit_with_receipts(observations, {"crm-backend": "a" * 64})
    h1_report = next(
        server for server in report.servers if server.server_id == "h1"
    )
    check = next(
        check
        for check in h1_report.checks
        if check.check == "environment.file[crm-backend]"
    )

    assert check.status is AuditStatus.DRIFT
    assert check.observed is None


def test_no_environment_receipts_means_no_environment_checks() -> None:
    report = _audit(COMPLIANT)

    for server in report.servers:
        assert not any(
            check.check.startswith("environment.file")
            for check in server.checks
        )


def test_audit_reads_the_snapshots_observe_writes_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    shutil.copytree(COMPLIANT, project / "tmp" / "observed")

    code = main(["audit", "--project", str(project)])

    report = json.loads(capsys.readouterr().out)["data"]
    assert code == 0
    assert report["status"] == "compliant"
