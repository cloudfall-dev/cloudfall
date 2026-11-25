"""Compare desired baselines with validated server observations."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import TYPE_CHECKING, cast

import yaml
from cryptography import x509

from cloudfall.dashboards import bundled_dashboards
from cloudfall.domain import ResourceKind
from cloudfall.elasticsearch import supported_line
from cloudfall.inventory import (
    MariadbOwnedDatabase,
    firewall_rules_for_server,
    logging_gateway_rules_for_server,
    remote_access_rules_for_server,
    service_remote_access,
)
from cloudfall.kibana import EDIT_ROLE as KIBANA_EDIT_ROLE
from cloudfall.kibana import ROLE_NAMES as KIBANA_ROLE_NAMES
from cloudfall.kibana import VIEW_ROLE as KIBANA_VIEW_ROLE
from cloudfall.kibana import KibanaEncryptionKey
from cloudfall.mariadb import RESERVED_USER_NAMES as RESERVED_MARIADB_USER_NAMES
from cloudfall.mariadb import SYSTEM_DATABASES as MARIADB_SYSTEM_DATABASES
from cloudfall.mariadb import MariadbReplicationRole
from cloudfall.mariadb import account_host as mariadb_account_host
from cloudfall.mariadb import supported_series as mariadb_supported_series
from cloudfall.mariadb_replication import replication_rules_for_server
from cloudfall.pki import RENEW_BEFORE, certificate_names
from cloudfall.rabbitmq import GUEST_USER as RABBITMQ_GUEST_USER
from cloudfall.rabbitmq import MANAGED_TAG as RABBITMQ_MANAGED_TAG
from cloudfall.rabbitmq import OWNER_TAG_PREFIX as RABBITMQ_OWNER_TAG_PREFIX
from cloudfall.rabbitmq_access import (
    client_access_rules_for_server as rabbitmq_client_access_rules_for_server,
)
from cloudfall.rabbitmq_cluster import (
    cluster_rules_for_server as rabbitmq_cluster_rules_for_server,
)
from cloudfall.rabbitmq_cluster import is_cluster as is_rabbitmq_cluster
from cloudfall.rabbitmq_cluster import node_name as rabbitmq_node_name
from cloudfall.requirements import (
    ELASTICSEARCH_PACKAGE,
    KIBANA_PACKAGE,
    MARIADB_CA_PATH,
    MARIADB_CERTIFICATE_PATH,
    MARIADB_KEY_PATH,
    MARIADB_PACKAGE,
    MARIADB_VOLUME_PATH,
    CertificateRequirement,
    PackageRequirement,
    VolumeRequirement,
    requirements_of,
)
from cloudfall.volumes import (
    DeviceResolution,
    parse_devices,
    parse_logical_volumes,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from cloudfall.domain import ResourceId
    from cloudfall.inventory import (
        AlertRuleInventory,
        BaselineInventory,
        ComponentInventory,
        ConfigurationFileRequirement,
        DomainInventory,
        ElasticsearchService,
        FirewallRequirement,
        LoggingStackInventory,
        MariadbService,
        PlatformInventory,
        RaidRequirement,
        ServerInventory,
        ServiceInventory,
        SourceRestrictedFirewallRule,
    )
    from cloudfall.observation import ObservationSet, ObservedServerSnapshot

_RAID_HEALTH_PATTERN = re.compile(r"\[(\d+)/(\d+)\]\s+\[([U_]+)\]")
_MANAGED_FIREWALL_TABLE = "cloudfall"
_MANAGED_FIREWALL_INPUT_CHAIN = "input"
_MANAGED_FIREWALL_INPUT_POLICY = "drop"
_LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})
# An inbound rule without a saddr match accepts every source.
_ANY_SOURCE = ""
_MINIMUM_SOCKET_FIELDS = 5
ELASTICSEARCH_UNIT = "elasticsearch.service"
MARIADB_UNIT = "mariadb.service"
MARIADB_SETTINGS_PATH = "/etc/mysql/mariadb.conf.d/90-cloudfall.cnf"
"""The one file the MariaDB role writes its managed settings to (S-019)."""
MARIADB_LAG_LIMIT_SECONDS = 300
"""A replica further behind its primary than this is a finding (S-019)."""
_MARIADB_UPSTREAM_VERSION = re.compile(r"^(?:\d+:)?(\d+\.\d+\.\d+)")
_MARIADB_SETTINGS_SECTION = "mariadbd"
_MARIADB_THREAD_RUNNING = "Yes"
KIBANA_UNIT = "kibana.service"
KIBANA_CONFIGURATION_DIRECTORY = "/etc/kibana"
KIBANA_CA_PATH = f"{KIBANA_CONFIGURATION_DIRECTORY}/elasticsearch-ca.crt"
"""Where the Kibana role copies the project CA the node's HTTPS is verified against."""
KIBANA_AVAILABLE_LEVEL = "available"
_KIBANA_HEAP_OPTION = "--max-old-space-size"
_KIBANA_ROLE_BASES = {KIBANA_VIEW_ROLE: "read", KIBANA_EDIT_ROLE: "all"}
"""Kibana's base privilege each role the Kibana role keeps holds on every space."""
_KIBANA_ELASTICSEARCH_PRIVILEGES = ("cluster", "indices", "run_as", "remote_indices")


class AuditStatus(StrEnum):
    """Compliance status of an audit check or report."""

    COMPLIANT = "compliant"
    DRIFT = "drift"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AuditCheck:
    """One desired-versus-observed comparison."""

    check: str
    status: AuditStatus
    desired: object
    observed: object
    message: str

    def as_dict(self) -> dict[str, object]:
        """Serialize a stable machine-readable check."""
        return {
            "check": self.check,
            "status": self.status.value,
            "desired": self.desired,
            "observed": self.observed,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ServerAudit:
    """Audit result for one desired server."""

    server_id: str
    baseline_id: str
    status: AuditStatus
    observation: str | None
    observed_at: str | None
    checks: tuple[AuditCheck, ...]

    def as_dict(self) -> dict[str, object]:
        """Serialize a server audit."""
        return {
            "server": self.server_id,
            "baseline": self.baseline_id,
            # Deprecated name of baseline, the same value, until 0.9.0 (D-11).
            "serverType": self.baseline_id,
            "status": self.status.value,
            "observation": self.observation,
            "observedAt": self.observed_at,
            "checks": [check.as_dict() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class AuditReport:
    """Fleet-wide desired-versus-observed audit report."""

    status: AuditStatus
    servers: tuple[ServerAudit, ...]
    unmatched_observations: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Serialize the report with deterministic summary counts."""
        server_counts = Counter(server.status.value for server in self.servers)
        check_counts = Counter(
            check.status.value
            for server in self.servers
            for check in server.checks
        )
        return {
            "status": self.status.value,
            "summary": {
                "servers": {
                    status.value: server_counts[status.value]
                    for status in AuditStatus
                },
                "checks": {
                    status.value: check_counts[status.value]
                    for status in AuditStatus
                },
            },
            "unmatchedObservations": list(self.unmatched_observations),
            "servers": [server.as_dict() for server in self.servers],
        }


def audit_inventory(
    inventory: PlatformInventory,
    observations: ObservationSet,
    environment_receipts: Mapping[str, str] | None = None,
    dashboards: Mapping[str, str] | None = None,
) -> AuditReport:
    """Audit every desired server against its validated observation.

    ``dashboards`` maps each bundled Grafana dashboard's file name to its
    sha256 (S-016); by default the installed release's own.
    """
    receipts = environment_receipts if environment_receipts is not None else {}
    if dashboards is None and inventory.logging_stacks:
        dashboards = bundled_dashboards()
    placed = requirements_of(inventory)
    certificates = {
        server.server_id.value: tuple(
            requirement.spec
            for requirement in server.requirements
            if isinstance(requirement.spec, CertificateRequirement)
        )
        for server in placed
    }
    volumes = {
        server.server_id.value: tuple(
            requirement.spec
            for requirement in server.requirements
            if isinstance(requirement.spec, VolumeRequirement)
        )
        for server in placed
    }
    logging_packages = {
        server.server_id.value: tuple(
            requirement.spec
            for requirement in server.requirements
            if isinstance(requirement.spec, PackageRequirement)
            and requirement.owner.kind is ResourceKind.LOGGING_STACK
        )
        for server in placed
    }
    stacks_by_backend = {
        stack.backend.server_id: stack for stack in inventory.logging_stacks
    }
    servers_by_id = {server.resource_id: server for server in inventory.servers}
    rabbitmq_nodes = {
        service.resource_id: tuple(
            rabbitmq_node_name(servers_by_id[node]) for node in service.rabbitmq.nodes
        )
        for service in inventory.services
        if service.rabbitmq is not None and is_rabbitmq_cluster(service)
    }
    kibana = _kibana_contexts(inventory)
    audits = tuple(
        _audit_server(
            server,
            inventory.baseline(server.baseline_id),
            services=tuple(
                service
                for service in inventory.services
                if service.server_id == server.resource_id
            ),
            alert_rules=_alert_rules_for_backend(server, stacks_by_backend, inventory),
            components=tuple(
                component
                for component in inventory.components
                if server.resource_id in component.server_ids
            ),
            environment_receipts=receipts,
            snapshot=observations.for_server(server.resource_id),
            gateway_rules=_gateway_rules(server, inventory),
            certificates=certificates.get(server.resource_id.value, ()),
            volumes=volumes.get(server.resource_id.value, ()),
            logging_packages=logging_packages.get(server.resource_id.value, ()),
            dashboards=dashboards if server.resource_id in stacks_by_backend else None,
            elasticsearch_nodes=tuple(
                service
                for service in inventory.services
                if service.elasticsearch is not None
                and server.resource_id in service.elasticsearch.nodes
            ),
            rabbitmq_nodes=rabbitmq_nodes,
            kibana=tuple(
                context
                for context in kibana
                if context.service.server_id == server.resource_id
            ),
        )
        for server in inventory.servers
    )
    desired_ids = frozenset(server.resource_id for server in inventory.servers)
    unmatched = tuple(
        sorted(
            snapshot.server_id.value
            for snapshot in observations.snapshots
            if snapshot.server_id not in desired_ids
        )
    )
    return AuditReport(
        status=_aggregate_status(audit.status for audit in audits),
        servers=audits,
        unmatched_observations=unmatched,
    )


def _audit_server(  # noqa: PLR0913 - one audit, many evidence sources.
    server: ServerInventory,
    baseline: BaselineInventory,
    *,
    services: tuple[ServiceInventory, ...],
    alert_rules: tuple[AlertRuleInventory, ...],
    components: tuple[ComponentInventory, ...],
    environment_receipts: Mapping[str, str],
    snapshot: ObservedServerSnapshot | None,
    gateway_rules: tuple[SourceRestrictedFirewallRule, ...] = (),
    certificates: tuple[CertificateRequirement, ...] = (),
    volumes: tuple[VolumeRequirement, ...] = (),
    logging_packages: tuple[PackageRequirement, ...] = (),
    dashboards: Mapping[str, str] | None = None,
    elasticsearch_nodes: tuple[ServiceInventory, ...] = (),
    rabbitmq_nodes: Mapping[ResourceId, tuple[str, ...]] | None = None,
    kibana: tuple[_KibanaContext, ...] = (),
) -> ServerAudit:
    if snapshot is None:
        check = AuditCheck(
            check="observation.available",
            status=AuditStatus.UNKNOWN,
            desired="validated observation",
            observed=None,
            message="no observation was supplied for this server",
        )
        return ServerAudit(
            server_id=server.resource_id.value,
            baseline_id=baseline.resource_id.value,
            status=AuditStatus.UNKNOWN,
            observation=None,
            observed_at=None,
            checks=(check,),
        )

    checks: list[AuditCheck] = []
    checks.append(
        _comparison(
            "baseline.id",
            baseline.resource_id.value,
            snapshot.baseline_id.value,
            matches=baseline.resource_id == snapshot.baseline_id,
        )
    )
    checks.extend(_audit_os(baseline, snapshot))
    if baseline.raid is not None:
        checks.extend(_audit_raid(baseline.raid, snapshot))
    checks.extend(_audit_mounts(baseline, snapshot))
    checks.extend(_audit_packages(baseline, snapshot))
    checks.extend(_audit_logging_packages(logging_packages, snapshot))
    checks.extend(_audit_services(baseline, snapshot))
    if baseline.firewall is not None:
        checks.extend(
            _audit_firewall(
                server, baseline.firewall, services, gateway_rules, snapshot
            )
        )
    checks.extend(_audit_certificates(certificates, snapshot))
    checks.extend(_audit_volumes(volumes, snapshot))
    if baseline.alert_channel is not None:
        checks.append(_audit_alert_channel(baseline.alert_channel, snapshot))
    if dashboards is not None:
        checks.append(_audit_dashboards(dashboards, snapshot))
    checks.extend(_audit_service_binds(services, snapshot))
    for service in elasticsearch_nodes:
        checks.extend(_audit_elasticsearch_node(service, snapshot))
    checks.extend(_audit_elasticsearch_users(services, snapshot))
    for service in services:
        checks.extend(_audit_mariadb_server(service, snapshot))
    checks.extend(_audit_mariadb_accounts(services, snapshot))
    for context in kibana:
        checks.extend(_audit_kibana(context, snapshot))
    checks.extend(_audit_rabbitmq_accounts(services, snapshot))
    checks.extend(_audit_rabbitmq_cluster(services, rabbitmq_nodes, snapshot))
    checks.extend(_audit_alert_rules(alert_rules, snapshot))
    checks.extend(
        _audit_component_environment(
            components, environment_receipts, snapshot
        )
    )
    checks.extend(_audit_configuration(baseline, snapshot))
    return ServerAudit(
        server_id=server.resource_id.value,
        baseline_id=baseline.resource_id.value,
        status=_aggregate_status(check.status for check in checks),
        observation=str(snapshot.source),
        observed_at=_string(snapshot.spec, "observedAt"),
        checks=tuple(checks),
    )


def _audit_os(
    baseline: BaselineInventory, snapshot: ObservedServerSnapshot
) -> tuple[AuditCheck, ...]:
    observed = _mapping(snapshot.spec, "os")
    distribution = _string(observed, "distribution")
    major_version = _string(observed, "majorVersion")
    service_manager = _string(observed, "serviceManager")
    versions = [version.value for version in baseline.os.versions]
    return (
        _comparison(
            "os.distribution",
            baseline.os.distribution.value,
            distribution,
            matches=distribution == baseline.os.distribution.value,
        ),
        _comparison(
            "os.majorVersion",
            versions,
            major_version,
            matches=major_version in versions,
        ),
        _comparison(
            "os.serviceManager",
            baseline.os.service_manager.value,
            service_manager,
            matches=service_manager == baseline.os.service_manager.value,
        ),
    )


def _audit_raid(
    requirement: RaidRequirement, snapshot: ObservedServerSnapshot
) -> tuple[AuditCheck, ...]:
    storage = _mapping(snapshot.spec, "storage")
    software_raid = _mapping(storage, "softwareRaid")
    mdstat = _string(software_raid, "mdstat")
    raid_level = requirement.level.value
    health_matches = tuple(_RAID_HEALTH_PATTERN.finditer(mdstat))
    # The worst array decides: a host with one healthy mirror and one
    # degraded mirror is a host running unprotected, whatever the healthy
    # one reports.
    active_devices = min(
        (match.group(3).count("U") for match in health_matches),
        default=0,
    )
    raid_sizes = [
        _integer(device, "size")
        for device in _flatten_block_devices(
            _mapping_sequence(storage, "blockDevices")
        )
        if _string(device, "type") == raid_level
    ]
    usable_bytes = max(raid_sizes, default=0)
    return (
        _comparison(
            "storage.softwareRaid.level",
            raid_level,
            _observed_raid_levels(mdstat),
            matches=f"active {raid_level}" in mdstat,
        ),
        _comparison(
            "storage.softwareRaid.activeDevices",
            {"minimum": requirement.minimum_active_devices.value},
            active_devices,
            matches=active_devices >= requirement.minimum_active_devices.value,
        ),
        _comparison(
            "storage.softwareRaid.usableBytes",
            {"minimum": requirement.minimum_usable_bytes.value},
            usable_bytes,
            matches=usable_bytes >= requirement.minimum_usable_bytes.value,
        ),
    )


def _audit_mounts(
    baseline: BaselineInventory, snapshot: ObservedServerSnapshot
) -> tuple[AuditCheck, ...]:
    storage = _mapping(snapshot.spec, "storage")
    filesystems = {
        _string(item, "target"): item
        for item in _mapping_sequence(storage, "filesystems")
    }
    checks: list[AuditCheck] = []
    for requirement in baseline.mounts:
        actual = filesystems.get(requirement.path.value)
        desired = {
            "filesystem": requirement.filesystem.value,
            "minimumBytes": requirement.minimum_bytes.value,
        }
        if actual is None:
            checks.append(
                _comparison(
                    f"storage.mounts[{requirement.path.value}]",
                    desired,
                    None,
                    matches=False,
                )
            )
            continue
        observed_size = actual.get("size")
        size_matches = (
            isinstance(observed_size, int)
            and not isinstance(observed_size, bool)
            and observed_size >= requirement.minimum_bytes.value
        )
        observed = {
            "filesystem": _string(actual, "fstype"),
            "size": observed_size,
            "source": _string(actual, "source"),
        }
        checks.append(
            _comparison(
                f"storage.mounts[{requirement.path.value}]",
                desired,
                observed,
                matches=(
                    observed["filesystem"] == requirement.filesystem.value
                    and size_matches
                ),
            )
        )
    return tuple(checks)


def _audit_packages(
    baseline: BaselineInventory, snapshot: ObservedServerSnapshot
) -> tuple[AuditCheck, ...]:
    packages: dict[str, list[Mapping[str, object]]] = {}
    for entry in _mapping_sequence(snapshot.spec, "packages"):
        packages.setdefault(_string(entry, "name"), []).append(entry)
    checks: list[AuditCheck] = []
    for requirement in baseline.required_packages:
        entries = tuple(packages.get(requirement.name.value, []))
        versions = [_string(entry, "version") for entry in entries]
        desired: object = (
            {"installed": True}
            if requirement.version is None
            else {"version": requirement.version.value}
        )
        matches = bool(entries) and (
            requirement.version is None
            or requirement.version.value in versions
        )
        checks.append(
            _comparison(
                f"packages.required[{requirement.name.value}]",
                desired,
                {"versions": versions} if entries else None,
                matches=matches,
            )
        )
    for package in baseline.forbidden_packages:
        present = package.value in packages
        checks.append(
            _comparison(
                f"packages.forbidden[{package.value}]",
                {"installed": False},
                {"installed": present},
                matches=not present,
            )
        )
    return tuple(checks)


def _audit_logging_packages(
    requirements: tuple[PackageRequirement, ...], snapshot: ObservedServerSnapshot
) -> tuple[AuditCheck, ...]:
    """Compare each package a LoggingStack pins with the installed one (#294)."""
    installed: dict[str, list[str]] = {}
    for entry in _mapping_sequence(snapshot.spec, "packages"):
        installed.setdefault(_string(entry, "name"), []).append(
            _string(entry, "version")
        )
    checks: list[AuditCheck] = []
    for requirement in requirements:
        versions = installed.get(requirement.package.value)
        checks.append(
            _comparison(
                f"logging.packages[{requirement.package.value}]",
                {"version": requirement.version.value},
                {"versions": versions} if versions is not None else None,
                matches=versions is not None
                and requirement.version.value in versions,
            )
        )
    return tuple(checks)


def _audit_services(
    baseline: BaselineInventory, snapshot: ObservedServerSnapshot
) -> tuple[AuditCheck, ...]:
    services = _mapping(snapshot.spec, "services")
    timers = _mapping(snapshot.spec, "timers")
    checks: list[AuditCheck] = []
    for requirement in baseline.required_services:
        evidence = timers if requirement.name.is_timer else services
        raw_actual = evidence.get(requirement.name.value)
        actual = _optional_mapping(raw_actual)
        desired = {
            "state": requirement.state.value,
            "status": requirement.status.value,
        }
        observed = (
            {
                "state": _string(actual, "state"),
                "status": _string(actual, "status"),
            }
            if actual is not None
            else None
        )
        checks.append(
            _comparison(
                f"services.required[{requirement.name.value}]",
                desired,
                observed,
                matches=observed == desired,
            )
        )
    return tuple(checks)


@dataclass(frozen=True, slots=True)
class _ObservedFirewallTable:
    """Managed nftables table facts parsed from observation evidence."""

    input_policy: str | None
    allowed_inbound: tuple[tuple[str, int, str], ...]


def _audit_firewall(
    server: ServerInventory,
    firewall: FirewallRequirement,
    services: tuple[ServiceInventory, ...],
    gateway_rules: tuple[SourceRestrictedFirewallRule, ...],
    snapshot: ObservedServerSnapshot,
) -> tuple[AuditCheck, ...]:
    evidence = _mapping(snapshot.spec, "firewall")
    nft_available = evidence.get("nftAvailable") is True
    raw_table = evidence.get("managedTableJson")
    table = (
        _parse_managed_firewall_table(raw_table)
        if nft_available and isinstance(raw_table, str)
        else None
    )
    expected_rules = sorted(
        [
            (rule.protocol.value, rule.port.value, _ANY_SOURCE)
            for rule in firewall_rules_for_server(firewall, server.ssh_port)
        ]
        + [
            (rule.protocol.value, rule.port.value, source.value)
            for rule in (
                *remote_access_rules_for_server(server, services),
                *gateway_rules,
            )
            for source in rule.sources
        ]
    )
    observed_rules = None if table is None else sorted(table.allowed_inbound)
    return (
        _comparison(
            "firewall.managedTable",
            {"table": f"inet {_MANAGED_FIREWALL_TABLE}", "present": True},
            {"nftAvailable": nft_available, "present": table is not None},
            matches=table is not None,
        ),
        _comparison(
            "firewall.inputPolicy",
            _MANAGED_FIREWALL_INPUT_POLICY,
            None if table is None else table.input_policy,
            matches=(
                table is not None
                and table.input_policy == _MANAGED_FIREWALL_INPUT_POLICY
            ),
        ),
        _comparison(
            "firewall.allowedInbound",
            [_inbound_label(rule) for rule in expected_rules],
            (
                None
                if observed_rules is None
                else [_inbound_label(rule) for rule in observed_rules]
            ),
            matches=observed_rules == expected_rules,
        ),
    )


def _gateway_rules(
    server: ServerInventory, inventory: PlatformInventory
) -> tuple[SourceRestrictedFirewallRule, ...]:
    """Return the source-restricted rules the engine renders for ``server``.

    Those are the logging gateway's (S-011), a MariaDB primary's port,
    opened to its replicas' servers (S-019), and a RabbitMQ node's TLS
    client port, opened to its ``clientAccess`` sources (S-023).
    """
    baseline = inventory.baseline(server.baseline_id)
    if baseline.firewall is None:
        return ()
    open_ports = frozenset(
        (rule.port.value, rule.protocol)
        for rule in firewall_rules_for_server(baseline.firewall, server.ssh_port)
    )
    return (
        *logging_gateway_rules_for_server(
            server, inventory.servers, inventory.logging_stacks, open_ports
        ),
        *replication_rules_for_server(server, inventory.servers, inventory.services),
        *rabbitmq_client_access_rules_for_server(server, inventory.services),
        *rabbitmq_cluster_rules_for_server(
            server, inventory.servers, inventory.services
        ),
    )


def _audit_certificates(
    certificates: tuple[CertificateRequirement, ...],
    snapshot: ObservedServerSnapshot,
) -> tuple[AuditCheck, ...]:
    """Check each required certificate is there, for its names, and not due (S-011).

    Expiry is judged at the observation's own time, so the same snapshot
    always audits the same way.
    """
    if not certificates:
        return ()
    observed_at = datetime.fromisoformat(_string(snapshot.spec, "observedAt"))
    raw = snapshot.spec.get("certificates")
    evidence = {
        str(item["name"]): item
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, dict)
    }
    return tuple(
        _audit_certificate(
            requirement, evidence.get(requirement.name.value), observed_at
        )
        for requirement in certificates
    )


def _audit_dashboards(
    bundled: Mapping[str, str], snapshot: ObservedServerSnapshot
) -> AuditCheck:
    """Check the backend carries exactly the bundled dashboards (S-016-6)."""
    desired = dict(sorted(bundled.items()))
    evidence = snapshot.spec.get("loggingDashboards")
    if not isinstance(evidence, dict):
        return AuditCheck(
            check="logging.dashboards",
            status=AuditStatus.UNKNOWN,
            desired=desired,
            observed=None,
            message="the observation carries no dashboard evidence",
        )
    files = evidence.get("files")
    observed = {
        str(item.get("name")): item.get("sha256")
        for item in (files if isinstance(files, list) else [])
        if isinstance(item, dict)
    }
    missing = sorted(set(desired) - set(observed))
    extra = sorted(set(observed) - set(desired))
    changed = sorted(
        name for name in set(desired) & set(observed) if observed[name] != desired[name]
    )
    problems = [
        f"{label}: {', '.join(names)}"
        for label, names in (
            ("missing", missing),
            ("extra", extra),
            ("changed", changed),
        )
        if names
    ]
    return AuditCheck(
        check="logging.dashboards",
        status=AuditStatus.DRIFT if problems else AuditStatus.COMPLIANT,
        desired=desired,
        observed=dict(sorted(observed.items())),
        message=(
            "; ".join(problems)
            if problems
            else "the backend carries exactly the bundled dashboards"
        ),
    )


def _audit_alert_channel(
    channel: ResourceId, snapshot: ObservedServerSnapshot
) -> AuditCheck:
    """Check the host can send its events to its channel (S-015-7).

    Compliant when the sender, the token and the chat id are there, and mdadm
    and smartd, when installed, point at the sender.
    """
    desired = {
        "channel": channel.value,
        "senderInstalled": True,
        "tokenPresent": True,
        "chatIdPresent": True,
        "mdadmProgram": "the sender, when mdadm is installed",
        "smartdScan": "-M exec the sender, when smartd is installed",
    }
    evidence = snapshot.spec.get("alertChannel")
    if not isinstance(evidence, dict) or evidence.get("channel") != channel.value:
        return AuditCheck(
            check="alerting.channel",
            status=AuditStatus.UNKNOWN,
            desired=desired,
            observed=evidence if isinstance(evidence, dict) else None,
            message="the observation carries no evidence for this channel",
        )
    sender = str(evidence.get("sender"))
    program = evidence.get("mdadmProgram")
    scan = evidence.get("smartdScan")
    mdadm_ok = not evidence.get("mdadmConfigured") or (
        isinstance(program, str) and program.split()[1:] == [sender]
    )
    words = scan.split() if isinstance(scan, str) else []
    smartd_ok = not evidence.get("smartdConfigured") or any(
        words[index : index + 3] == ["-M", "exec", sender]
        for index in range(len(words))
    )
    matches = (
        evidence.get("senderInstalled") is True
        and evidence.get("tokenPresent") is True
        and evidence.get("chatIdPresent") is True
        and mdadm_ok
        and smartd_ok
    )
    return _comparison("alerting.channel", desired, dict(evidence), matches=matches)


def _audit_volumes(
    volumes: tuple[VolumeRequirement, ...],
    snapshot: ObservedServerSnapshot,
) -> tuple[AuditCheck, ...]:
    """Check each required volume: type, disks, size, health and mount (S-014-8)."""
    if not volumes:
        return ()
    evidence = snapshot.spec.get("volumes")
    report = (
        evidence.get("lvsJson")
        if isinstance(evidence, dict)
        else None
    )
    mounts = evidence.get("mounts") if isinstance(evidence, dict) else None
    if not isinstance(report, str) or not isinstance(mounts, dict):
        return tuple(
            AuditCheck(
                check=f"volume.{volume.name.value}",
                status=AuditStatus.UNKNOWN,
                desired=_volume_desired(volume),
                observed=None,
                message="the observation carries no volume evidence",
            )
            for volume in volumes
        )
    logical = parse_logical_volumes(report)
    # An observation from before #293 has no resolved devices: it compares
    # the declared paths literally, as it always did
    raw_devices = evidence.get("devices") if isinstance(evidence, dict) else None
    devices = None if raw_devices is None else parse_devices(raw_devices)
    return tuple(
        _audit_volume(volume, logical, mounts, devices) for volume in volumes
    )


def _volume_desired(volume: VolumeRequirement) -> dict[str, object]:
    return {
        "logicalVolume": f"{volume.pool.volume_group.value}/{volume.logical_volume}",
        "type": volume.volume_type,
        "physicalVolumes": sorted(item.value for item in volume.physical_volumes),
        "minimumBytes": volume.minimum_size.value,
        "healthy": True,
        "mountedAt": volume.path.value,
    }


def _volume_devices_match(
    volume: VolumeRequirement,
    sits_on: object,
    devices: Mapping[str, DeviceResolution] | None,
) -> bool:
    """Compare an LV's devices with the declared paths, on resolved devices (#293)."""
    if not isinstance(sits_on, list):
        return False
    declared = [item.value for item in volume.physical_volumes]
    if devices is None:
        return sorted(str(item) for item in sits_on) == sorted(declared)
    wanted = [devices[path].device if path in devices else None for path in declared]
    if None in wanted:
        return False

    def canonical(name: str) -> str:
        found = devices.get(name)
        return found.device if found is not None and found.device else name

    return sorted(canonical(str(item)) for item in sits_on) == sorted(
        str(item) for item in wanted
    )


def _audit_volume(
    volume: VolumeRequirement,
    logical: Mapping[str, Mapping[str, object]],
    mounts: Mapping[str, object],
    devices: Mapping[str, DeviceResolution] | None,
) -> AuditCheck:
    desired = _volume_desired(volume)
    check = f"volume.{volume.name.value}"
    held = logical.get(
        f"{volume.pool.volume_group.value}/{volume.logical_volume}"
    )
    if held is None:
        return _comparison(check, desired, {"present": False}, matches=False)
    # D-18: a mirror meets an expendable requirement, as the provider keeps it
    volume = volume.held_as(held.get("type"))
    desired = _volume_desired(volume)
    source = mounts.get(volume.path.value)
    lv_path = f"/dev/{volume.pool.volume_group.value}/{volume.logical_volume}"
    size = held.get("sizeBytes")
    observed: dict[str, object] = {
        "logicalVolume": f"{volume.pool.volume_group.value}/{volume.logical_volume}",
        "type": held.get("type"),
        "physicalVolumes": held.get("devices"),
        "sizeBytes": size,
        "health": held.get("health") or None,
        "mountedFrom": source,
    }
    if devices is not None:
        # The declared path next to the device it resolved to, None when the
        # host could not resolve it
        observed["resolvedPhysicalVolumes"] = {
            item.value: (
                devices[item.value].device if item.value in devices else None
            )
            for item in volume.physical_volumes
        }
    held_matches = (
        held.get("type") == desired["type"]
        and _volume_devices_match(volume, held.get("devices"), devices)
        and isinstance(size, int)
        and size >= volume.minimum_size.value
        and not held.get("health")
    )
    if held_matches and volume.path.value not in mounts:
        return AuditCheck(
            check=check,
            status=AuditStatus.UNKNOWN,
            desired=desired,
            observed=observed,
            message="the observation did not read what is mounted at this path",
        )
    matches = (
        held_matches
        and isinstance(source, str)
        and source in {lv_path, held.get("mapper")}
    )
    return _comparison(check, desired, observed, matches=bool(matches))


def _audit_certificate(
    requirement: CertificateRequirement,
    evidence: Mapping[str, object] | None,
    observed_at: datetime,
) -> AuditCheck:
    names = sorted(
        name.value.removesuffix(".").lower() for name in requirement.subject_names
    )
    desired = {
        "path": requirement.certificate_path.value,
        "names": names,
        "validForMoreThanDays": RENEW_BEFORE.days,
    }
    check = f"certificate.{requirement.name.value}"
    if evidence is None:
        return AuditCheck(
            check=check,
            status=AuditStatus.UNKNOWN,
            desired=desired,
            observed=None,
            message="the observation carries no evidence for this certificate",
        )
    pem = evidence.get("certificate")
    if not isinstance(pem, str):
        return _comparison(check, desired, {"present": False}, matches=False)
    try:
        held = x509.load_pem_x509_certificate(pem.encode())
    except ValueError:
        unreadable = {"present": True, "readable": False}
        return _comparison(check, desired, unreadable, matches=False)
    held_names = sorted(certificate_names(held))
    remaining = held.not_valid_after_utc - observed_at
    observed = {
        "present": True,
        "names": held_names,
        "notAfter": held.not_valid_after_utc.isoformat(),
        "issuer": held.issuer.rfc4514_string(),
    }
    return _comparison(
        check,
        desired,
        observed,
        matches=held_names == names and remaining > RENEW_BEFORE,
    )


def _inbound_label(rule: tuple[str, int, str]) -> str:
    """Render ``tcp/22``, or ``tcp/5432 from 10.0.0.0/24`` for a source rule."""
    protocol, port, source = rule
    if source == _ANY_SOURCE:
        return f"{protocol}/{port}"
    return f"{protocol}/{port} from {source}"


def _parse_managed_firewall_table(raw: str) -> _ObservedFirewallTable | None:
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    document = _optional_mapping(parsed)
    if document is None:
        return None
    entries = _mapping_values_sequence(document.get("nftables"))
    input_policy: str | None = None
    allowed: list[tuple[str, int, str]] = []
    table_present = False
    for entry in entries:
        table = _optional_mapping(entry.get("table"))
        if table is not None and table.get("name") == _MANAGED_FIREWALL_TABLE:
            table_present = True
        chain = _optional_mapping(entry.get("chain"))
        if (
            chain is not None
            and chain.get("table") == _MANAGED_FIREWALL_TABLE
            and chain.get("name") == _MANAGED_FIREWALL_INPUT_CHAIN
            and isinstance(chain.get("policy"), str)
        ):
            input_policy = cast("str", chain.get("policy"))
        rule = _optional_mapping(entry.get("rule"))
        if (
            rule is not None
            and rule.get("table") == _MANAGED_FIREWALL_TABLE
            and rule.get("chain") == _MANAGED_FIREWALL_INPUT_CHAIN
        ):
            endpoint = _accepted_inbound_endpoint(rule)
            if endpoint is not None:
                allowed.append(endpoint)
    if not table_present:
        return None
    return _ObservedFirewallTable(
        input_policy=input_policy,
        allowed_inbound=tuple(allowed),
    )


def _accepted_inbound_endpoint(
    rule: Mapping[str, object],
) -> tuple[str, int, str] | None:
    expressions = _mapping_values_sequence(rule.get("expr"))
    endpoint: tuple[str, int] | None = None
    source = _ANY_SOURCE
    accepts = False
    for expression in expressions:
        if "accept" in expression:
            accepts = True
            continue
        match = _optional_mapping(expression.get("match"))
        if match is None:
            continue
        left = _optional_mapping(match.get("left"))
        payload = None if left is None else _optional_mapping(left.get("payload"))
        if payload is None:
            continue
        port = match.get("right")
        if (
            payload.get("field") == "dport"
            and payload.get("protocol") in ("tcp", "udp")
            and isinstance(port, int)
            and not isinstance(port, bool)
        ):
            endpoint = (cast("str", payload.get("protocol")), port)
        elif payload.get("field") == "saddr" and payload.get("protocol") in (
            "ip",
            "ip6",
        ):
            source = _observed_source(match)
    if not accepts or endpoint is None:
        return None
    return (*endpoint, source)


def _observed_source(match: Mapping[str, object]) -> str:
    """Return a saddr match as a canonical network, as the declaration holds it.

    nftables prints a single address (a /32 or /128) bare and a wider network
    as ``{"prefix": {"addr": ..., "len": ...}}``. Any other shape (a set, a
    negation, a range) is kept as its raw JSON so it can never match a
    declared source and shows up as drift.
    """
    right = match.get("right")
    prefix = _optional_mapping(right)
    inner = None if prefix is None else _optional_mapping(prefix.get("prefix"))
    candidate: str | None = None
    if isinstance(right, str):
        candidate = right
    elif inner is not None:
        address = inner.get("addr")
        length = inner.get("len")
        if (
            isinstance(address, str)
            and isinstance(length, int)
            and not isinstance(length, bool)
        ):
            candidate = f"{address}/{length}"
    raw = json.dumps(dict(match), sort_keys=True)
    if match.get("op") != "==" or candidate is None:
        return raw
    try:
        network = ip_network(candidate, strict=True)
    except ValueError:
        return raw
    return str(network)


@dataclass(frozen=True, slots=True)
class _SocketListener:
    """One listening socket parsed from collected evidence."""

    protocol: str
    address: str
    port: int


def _audit_service_binds(
    services: tuple[ServiceInventory, ...],
    snapshot: ObservedServerSnapshot,
) -> tuple[AuditCheck, ...]:
    if not services:
        return ()
    network = _mapping(snapshot.spec, "network")
    listeners = _parse_listening_sockets(_string(network, "listeningSockets"))
    checks: list[AuditCheck] = []
    for service in services:
        port = service.bind.port.value
        address = service.bind.address.value
        on_port = tuple(
            listener
            for listener in listeners
            if listener.protocol == "tcp" and listener.port == port
        )
        declared_present = any(
            listener.address == address for listener in on_port
        )
        remote_access = service_remote_access(service, service.server_id)
        replication = (
            None if service.mariadb is None else service.mariadb.replication
        )
        replication_address = (
            None if replication is None else replication.listen_address
        )
        # The remoteAccess listener, and a MariaDB primary's replication
        # listener, are narrowed to their declared sources by the firewall,
        # which firewall.allowedInbound verifies; any other non-loopback
        # listener is exposed.
        accepted = {ip_address(loopback) for loopback in _LOOPBACK_ADDRESSES}
        if remote_access is not None:
            accepted.add(ip_address(remote_access.listen_address.value))
        if replication_address is not None:
            accepted.add(ip_address(replication_address.value))
        exposed = tuple(
            listener
            for listener in on_port
            if _listener_ip(listener.address) not in accepted
        )
        # A remoteAccess service must also listen on its listenAddress: one
        # that failed to bind it has remote access down while it is declared.
        if remote_access is not None:
            declared_present = declared_present and any(
                listener.address == remote_access.listen_address.value
                for listener in on_port
            )
        # Likewise a primary must listen on its replication listenAddress: its
        # replicas cannot connect to one that failed to bind it.
        if replication_address is not None:
            replication_ip = ip_address(replication_address.value)
            declared_present = declared_present and any(
                _listener_ip(listener.address) == replication_ip
                for listener in on_port
            )
        desired: dict[str, object] = {
            "protocol": "tcp",
            "address": address,
            "port": port,
            "loopbackOnly": remote_access is None and replication_address is None,
        }
        if remote_access is not None:
            desired["remoteAccess"] = {
                "listenAddress": remote_access.listen_address.value
            }
        if replication_address is not None:
            desired["replication"] = {"listenAddress": replication_address.value}
        observed = (
            {
                "listeners": [
                    f"{listener.address}:{listener.port}"
                    for listener in on_port
                ]
            }
            if on_port
            else None
        )
        checks.append(
            _comparison(
                f"services.bind[{service.resource_id.value}]",
                desired,
                observed,
                matches=declared_present and not exposed,
            )
        )
    return tuple(checks)


def _listener_ip(address: str) -> IPv4Address | IPv6Address | None:
    """Return a listener's address as an IP, or ``None`` when it is none.

    ``ss`` prints ``*`` for a socket bound to every address; an address that
    does not parse can never match a declared one, so it shows up as drift.
    """
    try:
        return ip_address(address)
    except ValueError:
        return None


def elasticsearch_managed_settings(service: ServiceInventory) -> dict[str, object]:
    """Return the HTTP and security settings the role writes for a node (S-012).

    ``http.host`` is ``None`` without ``remoteAccess``: the node then listens
    where ``network.host`` says, and a hand-written ``http.host`` is drift.
    """
    elasticsearch = service.elasticsearch
    if elasticsearch is None:
        message = f"not an Elasticsearch service: {service.resource_id.value}"
        raise ValueError(message)
    remote_access = elasticsearch.remote_access
    return {
        "network.host": service.bind.address.value,
        "http.port": service.bind.port.value,
        "http.host": (
            None
            if remote_access is None
            else [service.bind.address.value, remote_access.listen_address.value]
        ),
        "xpack.security.enabled": True,
        "xpack.security.http.ssl.enabled": remote_access is not None,
    }


def _audit_elasticsearch_node(
    service: ServiceInventory, snapshot: ObservedServerSnapshot
) -> tuple[AuditCheck, ...]:
    """Check one node of an Elasticsearch service; every check only reads (S-012).

    The installed package against ``version``, the line against its end of
    maintenance at the observation's time (D-12), the unit, and the managed
    settings of ``elasticsearch.yml`` as the file holds them: a hand edit
    is drift before the node restarts with it.
    """
    elasticsearch = service.elasticsearch
    if elasticsearch is None:
        return ()
    service_id = service.resource_id.value
    version = elasticsearch.version.value
    installed = [
        _string(entry, "version")
        for entry in _mapping_sequence(snapshot.spec, "packages")
        if _string(entry, "name") == ELASTICSEARCH_PACKAGE.value
    ]
    unit = _optional_mapping(
        _mapping(snapshot.spec, "services").get(ELASTICSEARCH_UNIT)
    )
    unit_observed = (
        None
        if unit is None
        else {"state": _string(unit, "state"), "status": _string(unit, "status")}
    )
    unit_desired = {"state": "running", "status": "enabled"}
    return (
        _comparison(
            f"elasticsearch.version[{service_id}]",
            {"version": version},
            {"versions": installed} if installed else None,
            matches=installed == [version],
        ),
        _audit_elasticsearch_line(service, snapshot),
        _comparison(
            f"elasticsearch.unit[{service_id}]",
            {"unit": ELASTICSEARCH_UNIT, **unit_desired},
            unit_observed,
            matches=unit_observed == unit_desired,
        ),
        _audit_elasticsearch_settings(service, snapshot),
    )


def _audit_elasticsearch_line(
    service: ServiceInventory, snapshot: ObservedServerSnapshot
) -> AuditCheck:
    """Report a node whose line is past its end of maintenance (D-12)."""
    elasticsearch = cast("ElasticsearchService", service.elasticsearch)
    check = f"elasticsearch.line[{service.resource_id.value}]"
    line = supported_line(elasticsearch.version)
    observed_on = datetime.fromisoformat(_string(snapshot.spec, "observedAt")).date()
    if line is None:
        return AuditCheck(
            check=check,
            status=AuditStatus.DRIFT,
            desired={"supported": True},
            observed={"version": elasticsearch.version.value, "line": None},
            message=(
                f"Elasticsearch {elasticsearch.version.value} is of no supported "
                "line; Cloudfall installs only lines Elastic still maintains"
            ),
        )
    ended = observed_on >= line.end_of_life
    return AuditCheck(
        check=check,
        status=AuditStatus.DRIFT if ended else AuditStatus.COMPLIANT,
        desired={
            "line": line.label,
            "endOfMaintenance": line.end_of_life.isoformat(),
        },
        observed={
            "version": elasticsearch.version.value,
            "on": observed_on.isoformat(),
        },
        message=(
            f"the node runs an unsupported version: line {line.label} ended its "
            f"maintenance on {line.end_of_life.isoformat()}"
            if ended
            else f"line {line.label} is maintained until "
            f"{line.end_of_life.isoformat()}"
        ),
    )


def _audit_elasticsearch_settings(
    service: ServiceInventory, snapshot: ObservedServerSnapshot
) -> AuditCheck:
    desired = elasticsearch_managed_settings(service)
    check = f"elasticsearch.configuration[{service.resource_id.value}]"
    evidence = _optional_mapping(snapshot.spec.get("elasticsearch"))
    if evidence is None:
        return AuditCheck(
            check=check,
            status=AuditStatus.UNKNOWN,
            desired=desired,
            observed=None,
            message="the observation carries no Elasticsearch configuration evidence",
        )
    content = evidence.get("configuration")
    if not isinstance(content, str):
        return _comparison(check, desired, {"present": False}, matches=False)
    settings = _elasticsearch_settings(content)
    if settings is None:
        return _comparison(
            check, desired, {"present": True, "readable": False}, matches=False
        )
    observed = {key: settings.get(key) for key in desired}
    return _comparison(check, desired, observed, matches=observed == desired)


def _elasticsearch_settings(content: str) -> dict[str, object] | None:
    """Flatten ``elasticsearch.yml`` into dotted keys, as the node reads it.

    ``network: {host: ...}`` and ``network.host: ...`` are the same setting.
    ``None`` when the file is not a YAML mapping.
    """
    try:
        document: object = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    if document is None:
        return {}
    if not isinstance(document, dict):
        return None
    flat: dict[str, object] = {}

    def walk(prefix: str, value: object) -> None:
        if isinstance(value, dict) and value:
            for key, item in value.items():
                walk(f"{prefix}.{key}" if prefix else str(key), item)
        else:
            flat[prefix] = value

    walk("", document)
    return flat


def _audit_elasticsearch_users(
    services: tuple[ServiceInventory, ...],
    snapshot: ObservedServerSnapshot,
) -> tuple[AuditCheck, ...]:
    """Compare each home cluster's managed users and roles with the declared ones.

    A user or role without Cloudfall's marker was not made by it: the
    converge leaves it alone, and the check lists it as unmanaged without
    counting it as drift (S-012). A declared user or role that is missing, or
    a marked one that is no longer declared, is drift.
    """
    evidence = {
        _string(entry, "service"): entry
        for entry in _mapping_sequence(snapshot.spec, "elasticsearchUsers")
    }
    checks: list[AuditCheck] = []
    for service in services:
        if service.elasticsearch is None:
            continue
        name = f"elasticsearch.users[{service.resource_id.value}]"
        declared = sorted(user.name.value for user in service.elasticsearch.users)
        desired = {"users": declared, "roles": declared}
        entry = evidence.get(service.resource_id.value)
        if entry is None or entry.get("available") is not True:
            checks.append(
                AuditCheck(
                    check=name,
                    status=AuditStatus.UNKNOWN,
                    desired=desired,
                    observed=None,
                    message=(
                        "the cluster's users and roles were not read; inspect "
                        "again once the node answers as the cloudfall account"
                    ),
                )
            )
            continue
        users = _mapping(entry, "users")
        roles = _mapping(entry, "roles")
        observed = {
            "users": sorted(_strings(users, "managed")),
            "roles": sorted(_strings(roles, "managed")),
            "unmanagedUsers": sorted(_strings(users, "unmanaged")),
            "unmanagedRoles": sorted(_strings(roles, "unmanaged")),
        }
        matches = observed["users"] == declared and observed["roles"] == declared
        unmanaged = [
            f"{kind} {', '.join(names)}"
            for kind, names in (
                ("users", observed["unmanagedUsers"]),
                ("roles", observed["unmanagedRoles"]),
            )
            if names
        ]
        message = "desired state matches" if matches else "desired state does not match"
        if unmanaged:
            message += (
                "; unmanaged (not made by Cloudfall, left as they are): "
                + "; ".join(unmanaged)
            )
        checks.append(
            AuditCheck(
                check=name,
                status=AuditStatus.COMPLIANT if matches else AuditStatus.DRIFT,
                desired=desired,
                observed=observed,
                message=message,
            )
        )
    return tuple(checks)


@dataclass(frozen=True, slots=True)
class _KibanaContext:
    """A Kibana service, the Elasticsearch service it reads, and its Domain."""

    service: ServiceInventory
    cluster: ServiceInventory
    domain: DomainInventory | None


def _kibana_contexts(inventory: PlatformInventory) -> tuple[_KibanaContext, ...]:
    """Pair each Kibana service with its cluster and Domain, which validation found."""
    services = {service.resource_id: service for service in inventory.services}
    domains = {domain.resource_id: domain for domain in inventory.domains}
    contexts: list[_KibanaContext] = []
    for service in inventory.services:
        kibana = service.kibana
        if kibana is None:
            continue
        contexts.append(
            _KibanaContext(
                service=service,
                cluster=services[kibana.elasticsearch],
                domain=None if kibana.domain is None else domains[kibana.domain],
            )
        )
    return tuple(contexts)


def _url_host(address: str) -> str:
    """Return ``address`` as a URL's host: an IPv6 address in brackets."""
    return f"[{address}]" if ":" in address else address


def kibana_managed_settings(
    service: ServiceInventory,
    cluster: ServiceInventory,
    domain: DomainInventory | None,
) -> dict[str, object]:
    """Return the settings the Kibana role writes to ``kibana.yml`` (S-024).

    The file holds these and nothing else: the token and the encryption keys
    are in Kibana's keystore. Kibana reaches the node as the role's own calls
    do, over plain HTTP on loopback, or over HTTPS verified against the
    project CA when the cluster declares ``remoteAccess``.
    """
    elasticsearch = cluster.elasticsearch
    if service.kibana is None or elasticsearch is None:
        message = (
            "not a Kibana service and its Elasticsearch service: "
            f"{service.resource_id.value}, {cluster.resource_id.value}"
        )
        raise ValueError(message)
    address = service.bind.address.value
    port = service.bind.port.value
    remote_access = elasticsearch.remote_access
    node = (
        f"http://{cluster.bind.address.value}:{cluster.bind.port.value}"
        if remote_access is None
        else f"https://{_url_host(remote_access.listen_address.value)}:"
        f"{cluster.bind.port.value}"
    )
    settings: dict[str, object] = {
        "server.host": address,
        "server.port": port,
        "server.publicBaseUrl": (
            f"http://{_url_host(address)}:{port}"
            if domain is None
            else f"https://{domain.primary_name.value}"
        ),
    }
    if domain is not None:
        settings["xpack.security.secureCookies"] = True
    settings["elasticsearch.hosts"] = [node]
    if remote_access is not None:
        settings["elasticsearch.ssl.certificateAuthorities"] = [KIBANA_CA_PATH]
        settings["elasticsearch.ssl.verificationMode"] = "full"
    settings["telemetry.optIn"] = False
    return settings


def _audit_kibana(
    context: _KibanaContext, snapshot: ObservedServerSnapshot
) -> tuple[AuditCheck, ...]:
    """Check one Kibana service; every check only reads (S-024, Audit).

    The installed package against the Elasticsearch service's ``version``,
    the unit, the managed files, the level Kibana reports, the two roles it
    keeps, the Kibana roles of the cluster's declared users, and its S-011
    secret requirements: the three keys in its keystore. A listener off
    loopback is the ``services.bind`` check every service has.
    """
    service = context.service
    elasticsearch = cast("ElasticsearchService", context.cluster.elasticsearch)
    service_id = service.resource_id.value
    version = elasticsearch.version.value
    installed = [
        _string(entry, "version")
        for entry in _mapping_sequence(snapshot.spec, "packages")
        if _string(entry, "name") == KIBANA_PACKAGE.value
    ]
    unit = _optional_mapping(_mapping(snapshot.spec, "services").get(KIBANA_UNIT))
    unit_observed = (
        None
        if unit is None
        else {"state": _string(unit, "state"), "status": _string(unit, "status")}
    )
    unit_desired = {"state": "running", "status": "enabled"}
    evidence = _optional_mapping(snapshot.spec.get("kibana"))
    if evidence is not None and _string(evidence, "service") != service_id:
        evidence = None
    return (
        _comparison(
            f"kibana.version[{service_id}]",
            {
                "version": version,
                "elasticsearch": context.cluster.resource_id.value,
            },
            {"versions": installed} if installed else None,
            matches=installed == [version],
        ),
        _comparison(
            f"kibana.unit[{service_id}]",
            {"unit": KIBANA_UNIT, **unit_desired},
            unit_observed,
            matches=unit_observed == unit_desired,
        ),
        _audit_kibana_configuration(context, evidence),
        _audit_kibana_status(service, evidence),
        _audit_kibana_roles(service, evidence),
        _audit_kibana_users(context, evidence),
        _audit_kibana_keystore(service, evidence),
    )


def _kibana_unknown(check: str, desired: object, reason: str) -> AuditCheck:
    return AuditCheck(
        check=check,
        status=AuditStatus.UNKNOWN,
        desired=desired,
        observed=None,
        message=reason,
    )


_KIBANA_NO_EVIDENCE = (
    "the observation carries no Kibana evidence; inspect again with this release"
)


def _audit_kibana_configuration(
    context: _KibanaContext, evidence: Mapping[str, object] | None
) -> AuditCheck:
    """Compare ``kibana.yml`` and the heap in ``node.options`` with the rendered.

    ``kibana.yml`` is the role's alone: a setting it does not write is drift,
    listed by name only, since a hand-written value may be a secret.
    """
    service = context.service
    kibana = service.kibana
    if kibana is None:
        message = f"not a Kibana service: {service.resource_id.value}"
        raise ValueError(message)
    settings = kibana_managed_settings(service, context.cluster, context.domain)
    heap = {_KIBANA_HEAP_OPTION: kibana.heap_mb.value}
    desired = {"kibana.yml": settings, "node.options": heap}
    check = f"kibana.configuration[{service.resource_id.value}]"
    if evidence is None:
        return _kibana_unknown(check, desired, _KIBANA_NO_EVIDENCE)
    observed: dict[str, object] = {}
    problems: list[str] = []
    content = evidence.get("configuration")
    found = _elasticsearch_settings(content) if isinstance(content, str) else None
    if not isinstance(content, str):
        observed["kibana.yml"] = {"present": False}
        problems.append("kibana.yml is missing")
    elif found is None:
        observed["kibana.yml"] = {"present": True, "readable": False}
        problems.append("kibana.yml is no YAML mapping")
    else:
        observed["kibana.yml"] = {key: found.get(key) for key in settings}
        changed = sorted(key for key in settings if found.get(key) != settings[key])
        unmanaged = sorted(key for key in found if key not in settings)
        if unmanaged:
            observed["unmanagedSettings"] = unmanaged
        problems.extend(f"{key} differs" for key in changed)
        if unmanaged:
            problems.append(
                "kibana.yml holds settings the role does not write: "
                + ", ".join(unmanaged)
            )
    options = evidence.get("nodeOptions")
    heaps = _kibana_heaps(options) if isinstance(options, str) else None
    observed["node.options"] = (
        {"present": False}
        if heaps is None
        else {_KIBANA_HEAP_OPTION: heaps[0] if len(heaps) == 1 else heaps}
    )
    if heaps != [kibana.heap_mb.value]:
        problems.append(
            "node.options is missing"
            if heaps is None
            else f"node.options gives {_KIBANA_HEAP_OPTION} "
            f"{', '.join(str(item) for item in heaps) or 'nowhere'}"
        )
    return AuditCheck(
        check=check,
        status=AuditStatus.DRIFT if problems else AuditStatus.COMPLIANT,
        desired=desired,
        observed=observed,
        message="; ".join(problems) if problems else "desired state matches",
    )


def _kibana_heaps(content: str) -> list[int | str]:
    """Return each ``--max-old-space-size`` an active line of ``node.options`` sets."""
    found: list[int | str] = []
    prefix = f"{_KIBANA_HEAP_OPTION}="
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith(prefix):
            continue
        value = line.removeprefix(prefix).strip()
        found.append(int(value) if value.isdigit() else value)
    return found


def _audit_kibana_status(
    service: ServiceInventory, evidence: Mapping[str, object] | None
) -> AuditCheck:
    """Report a Kibana whose ``GET /api/status`` overall level is not available."""
    check = f"kibana.status[{service.resource_id.value}]"
    desired = {"level": KIBANA_AVAILABLE_LEVEL}
    if evidence is None:
        return _kibana_unknown(check, desired, _KIBANA_NO_EVIDENCE)
    status = _mapping(evidence, "status")
    if status.get("read") is not True:
        return _kibana_unknown(
            check,
            desired,
            "Kibana was not asked: the cloudfall account's password is not on "
            "the server",
        )
    level = status.get("level")
    available = level == KIBANA_AVAILABLE_LEVEL
    return AuditCheck(
        check=check,
        status=AuditStatus.COMPLIANT if available else AuditStatus.DRIFT,
        desired=desired,
        observed={"level": level},
        message=(
            "Kibana reports itself available"
            if available
            else "Kibana did not answer GET /api/status"
            if level is None
            else f"Kibana reports itself {level}"
        ),
    )


def _kibana_role_desired(service_id: str, role: str) -> dict[str, object]:
    """Return a role the Kibana role keeps, as its PUT body defines it."""
    return {
        "elasticsearchPrivileges": [],
        "kibana": [
            {"base": [_KIBANA_ROLE_BASES[role]], "features": [], "spaces": ["*"]}
        ],
        "metadata": {"cloudfall_managed_by": service_id},
    }


def _kibana_role_observed(entry: Mapping[str, object]) -> dict[str, object] | None:
    """Reduce Kibana's answer for a role to what the role compares; None if absent."""
    if entry.get("found") is not True:
        return None
    elasticsearch = _optional_mapping(entry.get("elasticsearch")) or {}
    privileges = sorted(
        key
        for key in _KIBANA_ELASTICSEARCH_PRIVILEGES
        if elasticsearch.get(key) not in (None, [])
    )
    kibana = [
        {
            "base": item.get("base"),
            "features": sorted(_optional_mapping(item.get("feature")) or {}),
            "spaces": item.get("spaces"),
        }
        for item in _mapping_values_sequence(entry.get("kibana"))
    ]
    return {
        "elasticsearchPrivileges": privileges,
        "kibana": kibana,
        "metadata": dict(_optional_mapping(entry.get("metadata")) or {}),
    }


def _audit_kibana_roles(
    service: ServiceInventory, evidence: Mapping[str, object] | None
) -> AuditCheck:
    """Compare the two Kibana roles with what the Kibana role writes (S-024).

    Each holds Kibana's base privilege ``read`` or ``all`` on every space,
    no feature privilege, no privilege in Elasticsearch, and the service's
    marker.
    """
    service_id = service.resource_id.value
    check = f"kibana.roles[{service_id}]"
    roles = sorted(KIBANA_ROLE_NAMES)
    desired = {role: _kibana_role_desired(service_id, role) for role in roles}
    if evidence is None:
        return _kibana_unknown(check, desired, _KIBANA_NO_EVIDENCE)
    entries = evidence.get("roles")
    if entries is None:
        return _kibana_unknown(
            check,
            desired,
            "the roles were not read; inspect again once Kibana answers as the "
            "cloudfall account",
        )
    answered = {
        _string(entry, "name"): entry for entry in _mapping_values_sequence(entries)
    }
    observed = {
        role: None if role not in answered else _kibana_role_observed(answered[role])
        for role in roles
    }
    differing = [role for role in roles if observed[role] != desired[role]]
    return AuditCheck(
        check=check,
        status=AuditStatus.DRIFT if differing else AuditStatus.COMPLIANT,
        desired=desired,
        observed=observed,
        message=(
            "; ".join(
                f"{role} is missing"
                if observed[role] is None
                else f"{role} differs from what the role writes"
                for role in differing
            )
            if differing
            else "desired state matches"
        ),
    )


def _audit_kibana_users(
    context: _KibanaContext, evidence: Mapping[str, object] | None
) -> AuditCheck:
    """Compare the Kibana roles each declared user holds with its ``access``.

    A user of the Elasticsearch service that ``kibana.users`` names holds the
    role of its access and no other; every other declared user holds none. A
    user the service does not declare is not Cloudfall's: one holding a
    Kibana role is listed as unmanaged without counting as drift.
    """
    service = context.service
    kibana = service.kibana
    elasticsearch = context.cluster.elasticsearch
    if kibana is None or elasticsearch is None:
        message = f"not a Kibana service: {service.resource_id.value}"
        raise ValueError(message)
    check = f"kibana.users[{service.resource_id.value}]"
    access = {user.name.value: user.access.role for user in kibana.users}
    desired = {
        name: [access[name]] if name in access else []
        for name in sorted(user.name.value for user in elasticsearch.users)
    }
    if evidence is None:
        return _kibana_unknown(check, desired, _KIBANA_NO_EVIDENCE)
    raw = evidence.get("userRoles")
    held = _optional_mapping(raw)
    if held is None:
        return _kibana_unknown(
            check,
            desired,
            "the cluster's users were not read; inspect again once the node "
            "answers as the cloudfall account",
        )
    holding = {name: sorted(_strings(held, name)) for name in held}
    observed = {name: holding.get(name, []) for name in desired}
    differing = [name for name in desired if observed[name] != desired[name]]
    unmanaged = sorted(
        name for name, roles in holding.items() if name not in desired and roles
    )
    problems = [
        f"{name} holds {', '.join(observed[name]) or 'no Kibana role'} where its "
        f"access gives {', '.join(desired[name]) or 'none'}"
        for name in differing
    ]
    message = "; ".join(problems) if problems else "desired state matches"
    if unmanaged:
        message += (
            "; unmanaged (not declared, left as they are): "
            + "; ".join(
                f"{name} holds {', '.join(holding[name])}" for name in unmanaged
            )
        )
    return AuditCheck(
        check=check,
        status=AuditStatus.DRIFT if differing else AuditStatus.COMPLIANT,
        desired=desired,
        observed=observed,
        message=message,
    )


def _audit_kibana_keystore(
    service: ServiceInventory, evidence: Mapping[str, object] | None
) -> AuditCheck:
    """Check the S-011 secret requirements: the three keys in Kibana's keystore.

    Read by setting name only; the package requirement is ``kibana.version``.
    """
    check = f"kibana.keystore[{service.resource_id.value}]"
    keys = sorted(key.value for key in KibanaEncryptionKey)
    desired = {"settings": keys}
    if evidence is None:
        return _kibana_unknown(check, desired, _KIBANA_NO_EVIDENCE)
    listed = evidence.get("keystore")
    if listed is None:
        return AuditCheck(
            check=check,
            status=AuditStatus.DRIFT,
            desired=desired,
            observed=None,
            message="Kibana's keystore could not be listed",
        )
    held = set(_strings(evidence, "keystore"))
    present = [key for key in keys if key in held]
    missing = [key for key in keys if key not in held]
    return AuditCheck(
        check=check,
        status=AuditStatus.DRIFT if missing else AuditStatus.COMPLIANT,
        desired=desired,
        observed={"settings": present},
        message=(
            "the keystore lacks " + ", ".join(missing)
            if missing
            else "desired state matches"
        ),
    )


def _audit_mariadb_accounts(
    services: tuple[ServiceInventory, ...],
    snapshot: ObservedServerSnapshot,
) -> tuple[AuditCheck, ...]:
    """Compare each MariaDB service's databases and users with the declared ones.

    A user holding Cloudfall's marker role was made or adopted by it, and a
    database such a user has privileges on is Cloudfall's. One of them no
    longer declared is listed as undeclared: the converge locked the user and
    left both in place, which is the declared outcome, not drift, as long as
    the user is locked (S-019-7). A user or database without the marker is
    listed as unmanaged and left alone. A declared database or user that is
    missing, or locked, is drift.
    """
    evidence = {
        _string(entry, "service"): entry
        for entry in _mapping_sequence(snapshot.spec, "mariadbAccounts")
    }
    checks: list[AuditCheck] = []
    for service in services:
        mariadb = service.mariadb
        if mariadb is None:
            continue
        name = f"mariadb.accounts[{service.resource_id.value}]"
        declared_databases = sorted({item.name.value for item in mariadb.databases})
        # With remoteAccess, each owner with a password also has one account
        # per allowed source (S-019, Access).
        remote_hosts = (
            ()
            if mariadb.remote_access is None
            else tuple(
                host
                for source in mariadb.remote_access.allowed_sources
                if (host := mariadb_account_host(source)) is not None
            )
        )
        declared_users = sorted(
            {f"{item.owner.value}@localhost" for item in mariadb.databases}
            | {
                f"{item.owner.value}@{host}"
                for item in mariadb.databases
                if isinstance(item, MariadbOwnedDatabase)
                for host in remote_hosts
            }
        )
        desired = {"databases": declared_databases, "users": declared_users}
        entry = evidence.get(service.resource_id.value)
        if entry is None or entry.get("available") is not True:
            checks.append(
                AuditCheck(
                    check=name,
                    status=AuditStatus.UNKNOWN,
                    desired=desired,
                    observed=None,
                    message=(
                        "the server's databases and users were not read; inspect "
                        "again once MariaDB answers root through its socket"
                    ),
                )
            )
            continue
        checks.append(_mariadb_accounts_check(name, desired, entry))
    return tuple(checks)


def _audit_rabbitmq_accounts(
    services: tuple[ServiceInventory, ...],
    snapshot: ObservedServerSnapshot,
) -> tuple[AuditCheck, ...]:
    """Compare each RabbitMQ service's virtual hosts and owners with the declared.

    Read on the service's home. A virtual host tagged ``cloudfall-managed``
    was made by Cloudfall, and its ``cloudfall-owner:`` tag names its owner.
    One no longer declared is listed as undeclared with its owner: the
    converge kept both and cleared the owner's password, which is the
    declared outcome, not drift, as long as the owner has none (S-023-7).
    Anything without the tag is listed as unmanaged and left alone. A
    declared virtual host or owner that is missing, an owner without a
    password, and a ``guest`` user are drift.
    """
    evidence = {
        _string(entry, "service"): entry
        for entry in _mapping_sequence(snapshot.spec, "rabbitmqAccounts")
    }
    checks: list[AuditCheck] = []
    for service in services:
        rabbitmq = service.rabbitmq
        if rabbitmq is None:
            continue
        name = f"rabbitmq.accounts[{service.resource_id.value}]"
        desired: dict[str, list[str]] = {
            "vhosts": sorted(vhost.name.value for vhost in rabbitmq.vhosts),
            "users": sorted(vhost.owner.value for vhost in rabbitmq.vhosts),
        }
        entry = evidence.get(service.resource_id.value)
        if entry is None or entry.get("available") is not True:
            checks.append(
                AuditCheck(
                    check=name,
                    status=AuditStatus.UNKNOWN,
                    desired=desired,
                    observed=None,
                    message=(
                        "the cluster's virtual hosts and users were not read; "
                        "inspect again once rabbitmqctl answers on the home server"
                    ),
                )
            )
            continue
        checks.append(_rabbitmq_accounts_check(name, desired, entry))
    return tuple(checks)


def _audit_rabbitmq_cluster(
    services: tuple[ServiceInventory, ...],
    nodes: Mapping[ResourceId, tuple[str, ...]] | None,
    snapshot: ObservedServerSnapshot,
) -> tuple[AuditCheck, ...]:
    """Compare each RabbitMQ cluster of three's members with its declared nodes.

    Read on the service's home. A declared node that is not a running member
    is missing; a cluster in which no more than half of the members run has
    no majority, and takes no publish to a quorum queue and no change of a
    definition (S-023-11). A service of one node has no such check.
    """
    evidence = {
        _string(entry, "service"): entry
        for entry in _mapping_sequence(snapshot.spec, "rabbitmqCluster")
    }
    checks: list[AuditCheck] = []
    for service in services:
        declared = (nodes or {}).get(service.resource_id)
        if declared is None:
            continue
        name = f"rabbitmq.cluster[{service.resource_id.value}]"
        desired = {"members": sorted(declared), "running": sorted(declared)}
        entry = evidence.get(service.resource_id.value)
        if entry is None or entry.get("available") is not True:
            checks.append(
                AuditCheck(
                    check=name,
                    status=AuditStatus.UNKNOWN,
                    desired=desired,
                    observed=None,
                    message=(
                        "the cluster's members were not read; inspect again "
                        "once rabbitmq-diagnostics answers on the home server"
                    ),
                )
            )
            continue
        members = sorted(set(_strings(entry, "members")))
        running = sorted(set(_strings(entry, "running")) & set(members))
        missing = [node for node in sorted(declared) if node not in running]
        undeclared = [node for node in members if node not in declared]
        majority = len(members) <= 1 or 2 * len(running) > len(members)
        problems = []
        if not majority:
            problems.append(
                f"the cluster has no majority: {len(running)} of {len(members)} "
                "members run, so it takes no publish to a quorum queue and no "
                "change of a definition"
            )
        if missing:
            problems.append(f"missing nodes: {', '.join(missing)}")
        if undeclared:
            problems.append(f"undeclared members: {', '.join(undeclared)}")
        checks.append(
            AuditCheck(
                check=name,
                status=AuditStatus.DRIFT if problems else AuditStatus.COMPLIANT,
                desired=desired,
                observed={
                    "members": members,
                    "running": running,
                    "missing": missing,
                    "majority": majority,
                },
                message="; ".join(problems) or "desired state matches",
            )
        )
    return tuple(checks)


def _rabbitmq_accounts_check(
    name: str, desired: dict[str, list[str]], entry: Mapping[str, object]
) -> AuditCheck:
    users = set(_strings(entry, "users"))
    without_password = set(_strings(entry, "usersWithoutPassword"))
    vhosts = set(_strings(entry, "vhosts"))
    tags = {
        vhost: {tag for tag in raw.split(",") if tag}
        for vhost, _, raw in (
            item.rpartition("|") for item in _strings(entry, "vhostTags")
        )
    }
    declared_vhosts = set(desired["vhosts"])
    declared_users = set(desired["users"])
    managed = {vhost for vhost, found in tags.items() if RABBITMQ_MANAGED_TAG in found}
    undeclared_vhosts = sorted(managed - declared_vhosts)
    undeclared_users = sorted(
        {
            tag.removeprefix(RABBITMQ_OWNER_TAG_PREFIX)
            for vhost in undeclared_vhosts
            for tag in tags[vhost]
            if tag.startswith(RABBITMQ_OWNER_TAG_PREFIX)
        }
        & users - declared_users
    )
    observed: dict[str, object] = {
        "vhosts": sorted(vhosts & declared_vhosts),
        "users": sorted(users & declared_users),
        "usersWithoutPassword": sorted(without_password & declared_users),
        "guest": RABBITMQ_GUEST_USER in users,
        "undeclaredVhosts": undeclared_vhosts,
        "undeclaredUsers": undeclared_users,
        "undeclaredUsersWithPassword": sorted(set(undeclared_users) - without_password),
        "unmanagedVhosts": sorted(vhosts - managed - declared_vhosts),
        "unmanagedUsers": sorted(
            users - declared_users - set(undeclared_users) - {RABBITMQ_GUEST_USER}
        ),
    }
    matches = (
        observed["vhosts"] == desired["vhosts"]
        and observed["users"] == desired["users"]
        and not observed["usersWithoutPassword"]
        and not observed["guest"]
        and not observed["undeclaredUsersWithPassword"]
    )
    message = "desired state matches" if matches else "desired state does not match"
    if observed["guest"]:
        message += "; the guest user exists"
    for label, key in (
        ("undeclared (made by Cloudfall, kept, owner can't log in)", "undeclared"),
        ("unmanaged (not made by Cloudfall, left as they are)", "unmanaged"),
    ):
        parts = [
            f"{kind} {', '.join(cast('list[str]', observed[f'{key}{title}']))}"
            for kind, title in (("virtual hosts", "Vhosts"), ("users", "Users"))
            if observed[f"{key}{title}"]
        ]
        if parts:
            message += f"; {label}: " + "; ".join(parts)
    return AuditCheck(
        check=name,
        status=AuditStatus.COMPLIANT if matches else AuditStatus.DRIFT,
        desired=desired,
        observed=observed,
        message=message,
    )


def _mariadb_accounts_check(
    name: str, desired: dict[str, list[str]], entry: Mapping[str, object]
) -> AuditCheck:
    users = set(_strings(entry, "users"))
    managed_users = set(_strings(entry, "managedUsers"))
    locked = set(_strings(entry, "lockedUsers"))
    databases = set(_strings(entry, "databases"))
    managed_databases = set(_strings(entry, "managedDatabases"))
    declared_users = set(desired["users"])
    declared_databases = set(desired["databases"])
    system_users = {
        user
        for user in users
        if user.rpartition("@")[0] in RESERVED_MARIADB_USER_NAMES
    }
    undeclared_users = sorted(managed_users - declared_users - system_users)
    observed: dict[str, object] = {
        "databases": sorted(databases & declared_databases),
        "users": sorted(users & declared_users),
        "lockedUsers": sorted(locked & declared_users),
        "undeclaredDatabases": sorted(managed_databases - declared_databases),
        "undeclaredUsers": undeclared_users,
        "unlockedUndeclaredUsers": sorted(set(undeclared_users) - locked),
        "unmanagedDatabases": sorted(
            databases
            - managed_databases
            - declared_databases
            - MARIADB_SYSTEM_DATABASES
        ),
        "unmanagedUsers": sorted(
            users - managed_users - declared_users - system_users
        ),
    }
    # Each remote owner account requires TLS: converge sets REQUIRE SSL, which
    # MariaDB keeps as ssl_type ANY, and nothing else (S-019-11, #435). A record
    # an older inspect wrote holds no sslTypes, so the requirement is unread.
    remote_users = sorted(
        user
        for user in users & declared_users
        if user.rpartition("@")[2] != "localhost"
    )
    tls_unread = bool(remote_users) and "sslTypes" not in entry
    if remote_users:
        ssl_types = (
            {}
            if tls_unread
            else {
                user: kind
                for user, _, kind in (
                    item.rpartition(" ") for item in _strings(entry, "sslTypes")
                )
            }
        )
        observed["remoteUsersWithoutRequireSsl"] = (
            None
            if tls_unread
            else [
                f"{user} {ssl_types.get(user, 'unread')}"
                for user in remote_users
                if ssl_types.get(user) != "ANY"
            ]
        )
    matches = (
        observed["databases"] == desired["databases"]
        and observed["users"] == desired["users"]
        and not observed["lockedUsers"]
        and not observed["unlockedUndeclaredUsers"]
        and not observed.get("remoteUsersWithoutRequireSsl")
    )
    status = (
        AuditStatus.DRIFT
        if not matches
        else AuditStatus.UNKNOWN
        if tls_unread
        else AuditStatus.COMPLIANT
    )
    message = "desired state matches" if matches else "desired state does not match"
    if observed.get("remoteUsersWithoutRequireSsl"):
        message += "; remote users without REQUIRE SSL (ssl_type ANY): " + ", ".join(
            cast("list[str]", observed["remoteUsersWithoutRequireSsl"])
        )
    if tls_unread:
        message += (
            "; whether the remote users require TLS was not read; inspect again "
            "with this release"
        )
    for label, key in (
        ("undeclared (made by Cloudfall, kept, user locked)", "undeclared"),
        ("unmanaged (not made by Cloudfall, left as they are)", "unmanaged"),
    ):
        parts = [
            f"{kind} {', '.join(cast('list[str]', observed[f'{key}{title}']))}"
            for kind, title in (("databases", "Databases"), ("users", "Users"))
            if observed[f"{key}{title}"]
        ]
        if parts:
            message += f"; {label}: " + "; ".join(parts)
    return AuditCheck(
        check=name,
        status=status,
        desired=desired,
        observed=observed,
        message=message,
    )


def mariadb_managed_settings(service: ServiceInventory) -> dict[str, str]:
    """Return the settings the role writes to the managed file (S-019).

    The same keys and values as ``90-cloudfall.cnf.j2``, each name with
    ``_`` for ``-``, as MariaDB reads both alike. ``read_only`` is the
    declared role's: a replica is read-only. A replica of a ``source``
    Cloudfall does not manage ignores ``mysql.%``.
    """
    mariadb = service.mariadb
    if mariadb is None:
        message = f"not a MariaDB service: {service.resource_id.value}"
        raise ValueError(message)
    replication = mariadb.replication
    listeners = [service.bind.address.value]
    if mariadb.remote_access is not None:
        listeners.append(mariadb.remote_access.listen_address.value)
    if replication is not None and replication.listen_address is not None:
        listeners.append(replication.listen_address.value)
    settings = {
        "bind_address": ",".join(dict.fromkeys(listeners)),
        "port": str(service.bind.port.value),
        "datadir": MARIADB_VOLUME_PATH.value,
        "innodb_buffer_pool_size": f"{mariadb.innodb_buffer_pool_mb.value}M",
        "character_set_server": "utf8mb4",
        "ignore_db_dirs": "lost+found",
    }
    if mariadb.remote_access is not None or replication is not None:
        settings |= {
            "ssl_cert": MARIADB_CERTIFICATE_PATH.value,
            "ssl_key": MARIADB_KEY_PATH.value,
            "ssl_ca": MARIADB_CA_PATH.value,
        }
    if replication is not None:
        settings |= {
            "server_id": str(replication.server_id.value),
            "log_bin": "mariadb-bin",
            "log_slave_updates": "ON",
            "binlog_format": "ROW",
            "gtid_strict_mode": "ON",
            "binlog_expire_logs_seconds": "604800",
            "relay_log": "mariadb-relay-bin",
        }
        if replication.role is MariadbReplicationRole.REPLICA:
            settings["read_only"] = "ON"
        if replication.source is not None:
            # A source Cloudfall does not manage: its users are not applied.
            settings["replicate_wild_ignore_table"] = "mysql.%"
    return settings


def _audit_mariadb_server(
    service: ServiceInventory, snapshot: ObservedServerSnapshot
) -> tuple[AuditCheck, ...]:
    """Check one MariaDB service's server; every check only reads (S-019).

    The installed package against ``version``, its series against the end of
    its community binaries, the unit, the managed file as it holds its
    settings, and the server's role: whether it is read-only and whom it
    replicates from. A replica's lag is a finding above five minutes.
    """
    mariadb = service.mariadb
    if mariadb is None:
        return ()
    service_id = service.resource_id.value
    installed = [
        _string(entry, "version")
        for entry in _mapping_sequence(snapshot.spec, "packages")
        if _string(entry, "name") == MARIADB_PACKAGE.value
    ]
    upstream = [
        match[1] if (match := _MARIADB_UPSTREAM_VERSION.match(version)) else version
        for version in installed
    ]
    unit = _optional_mapping(_mapping(snapshot.spec, "services").get(MARIADB_UNIT))
    unit_observed = (
        None
        if unit is None
        else {"state": _string(unit, "state"), "status": _string(unit, "status")}
    )
    unit_desired = {"state": "running", "status": "enabled"}
    evidence = _optional_mapping(snapshot.spec.get("mariadb"))
    if evidence is not None and _string(evidence, "service") != service_id:
        evidence = None
    checks = [
        _comparison(
            f"mariadb.version[{service_id}]",
            {"version": mariadb.version.value},
            {"versions": installed} if installed else None,
            matches=upstream == [mariadb.version.value],
        ),
        _audit_mariadb_series(service, snapshot),
        _comparison(
            f"mariadb.unit[{service_id}]",
            {"unit": MARIADB_UNIT, **unit_desired},
            unit_observed,
            matches=unit_observed == unit_desired,
        ),
        _audit_mariadb_settings(service, evidence),
        _audit_mariadb_role(service, evidence),
    ]
    replication = mariadb.replication
    if replication is not None and replication.role is MariadbReplicationRole.REPLICA:
        checks.append(_audit_mariadb_lag(service, evidence))
    return tuple(checks)


def _audit_mariadb_series(
    service: ServiceInventory, snapshot: ObservedServerSnapshot
) -> AuditCheck:
    """Report a service whose series is past its end of life (S-019)."""
    mariadb = cast("MariadbService", service.mariadb)
    check = f"mariadb.series[{service.resource_id.value}]"
    series = mariadb_supported_series(mariadb.version)
    observed_on = datetime.fromisoformat(_string(snapshot.spec, "observedAt")).date()
    if series is None:
        return AuditCheck(
            check=check,
            status=AuditStatus.DRIFT,
            desired={"supported": True},
            observed={"version": mariadb.version.value, "series": None},
            message=(
                f"MariaDB {mariadb.version.value} is of no supported series; "
                "Cloudfall installs only long-term series MariaDB still maintains"
            ),
        )
    ended = observed_on >= series.end_of_life
    return AuditCheck(
        check=check,
        status=AuditStatus.DRIFT if ended else AuditStatus.COMPLIANT,
        desired={"series": series.label, "endOfLife": series.end_of_life.isoformat()},
        observed={"version": mariadb.version.value, "on": observed_on.isoformat()},
        message=(
            f"the server runs an unsupported version: series {series.label} "
            f"reached its end of life on {series.end_of_life.isoformat()}"
            if ended
            else f"series {series.label} is maintained until "
            f"{series.end_of_life.isoformat()}"
        ),
    )


def _audit_mariadb_settings(
    service: ServiceInventory, evidence: Mapping[str, object] | None
) -> AuditCheck:
    desired = {_MARIADB_SETTINGS_SECTION: mariadb_managed_settings(service)}
    check = f"mariadb.configuration[{service.resource_id.value}]"
    if evidence is None:
        return AuditCheck(
            check=check,
            status=AuditStatus.UNKNOWN,
            desired=desired,
            observed=None,
            message=(
                "the observation carries no MariaDB server evidence; inspect "
                "again with this release"
            ),
        )
    content = evidence.get("configuration")
    if not isinstance(content, str):
        return _comparison(check, desired, {"present": False}, matches=False)
    settings = _mariadb_settings(content)
    if settings is None:
        return _comparison(
            check, desired, {"present": True, "readable": False}, matches=False
        )
    return _comparison(check, desired, settings, matches=settings == desired)


def _mariadb_settings(content: str) -> dict[str, dict[str, str]] | None:
    """Read an option file into its sections, each name with ``_`` for ``-``.

    An option without ``=`` is a switch, read as ``""``. ``None`` when a
    setting comes before any section, or the file includes another.
    """
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip(), {})
            continue
        if current is None or line.startswith("!"):
            return None
        name, _, value = line.partition("=")
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        current[name.strip().replace("-", "_")] = value
    return sections


def _mariadb_link(evidence: Mapping[str, object]) -> Mapping[str, object] | None:
    return _optional_mapping(evidence.get("replication"))


def _audit_mariadb_role(
    service: ServiceInventory, evidence: Mapping[str, object] | None
) -> AuditCheck:
    """Compare the server's role with the declared one (S-019, Audit and metrics).

    A replica is read-only and both its replication threads run; a
    standalone server or a primary replicates from nowhere.
    """
    mariadb = cast("MariadbService", service.mariadb)
    check = f"mariadb.role[{service.resource_id.value}]"
    role = (
        "standalone" if mariadb.replication is None else mariadb.replication.role.value
    )
    replica = role == MariadbReplicationRole.REPLICA.value
    desired: dict[str, object] = (
        {"role": role, "readOnly": True, "replicating": True}
        if replica
        else {"role": role, "replicating": False}
    )
    if evidence is None or evidence.get("available") is not True:
        return AuditCheck(
            check=check,
            status=AuditStatus.UNKNOWN,
            desired=desired,
            observed=None,
            message=(
                "the server's role was not read; inspect again once MariaDB "
                "answers root through its socket"
            ),
        )
    read_only = evidence.get("readOnly")
    link = _mariadb_link(evidence)
    observed: dict[str, object] = {
        "readOnly": read_only,
        "replication": None
        if link is None
        else {
            "ioRunning": _string(link, "ioRunning"),
            "sqlRunning": _string(link, "sqlRunning"),
            "sourceServerId": link.get("sourceServerId"),
        },
    }
    problems: list[str] = []
    if replica:
        if read_only is not True:
            problems.append("the replica is writable: read_only is off")
        if link is None:
            problems.append("the replica replicates from nowhere")
        else:
            stopped = [
                f"{thread} thread {_string(link, key)}"
                for thread, key in (("IO", "ioRunning"), ("SQL", "sqlRunning"))
                if _string(link, key) != _MARIADB_THREAD_RUNNING
            ]
            if stopped:
                problems.append("replication is stopped: " + ", ".join(stopped))
    elif link is not None:
        problems.append(
            f"the {role} server replicates from server id "
            f"{link.get('sourceServerId')}"
        )
    return AuditCheck(
        check=check,
        status=AuditStatus.DRIFT if problems else AuditStatus.COMPLIANT,
        desired=desired,
        observed=observed,
        message="; ".join(problems) if problems else "desired state matches",
    )


def _audit_mariadb_lag(
    service: ServiceInventory, evidence: Mapping[str, object] | None
) -> AuditCheck:
    """Report a replica's lag, as a finding above five minutes (S-019)."""
    check = f"mariadb.lag[{service.resource_id.value}]"
    desired = {"maxSeconds": MARIADB_LAG_LIMIT_SECONDS}
    link = None if evidence is None else _mariadb_link(evidence)
    seconds = None if link is None else link.get("secondsBehindMaster")
    if (
        evidence is None
        or evidence.get("available") is not True
        or not isinstance(seconds, int)
        or isinstance(seconds, bool)
    ):
        return AuditCheck(
            check=check,
            status=AuditStatus.UNKNOWN,
            desired=desired,
            observed=None,
            message=(
                "the replica's lag is unknown: its replication was not read "
                "or is not running"
            ),
        )
    behind = seconds > MARIADB_LAG_LIMIT_SECONDS
    return AuditCheck(
        check=check,
        status=AuditStatus.DRIFT if behind else AuditStatus.COMPLIANT,
        desired=desired,
        observed={"seconds": seconds},
        message=f"the replica is {seconds} seconds behind its primary",
    )


def _strings(content: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = content.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        message = f"validated field {key!r} is not a list of strings"
        raise TypeError(message)
    return tuple(cast("list[str]", value))


def _parse_listening_sockets(raw: str) -> tuple[_SocketListener, ...]:
    listeners: list[_SocketListener] = []
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < _MINIMUM_SOCKET_FIELDS:
            continue
        protocol = fields[0]
        local = fields[4]
        address, separator, raw_port = local.rpartition(":")
        if not separator or not raw_port.isdigit():
            continue
        address = address.strip("[]").split("%", maxsplit=1)[0]
        listeners.append(
            _SocketListener(
                protocol=protocol,
                address=address,
                port=int(raw_port),
            )
        )
    return tuple(listeners)


def _alert_rules_for_backend(
    server: ServerInventory,
    stacks_by_backend: Mapping[ResourceId, LoggingStackInventory],
    inventory: PlatformInventory,
) -> tuple[AlertRuleInventory, ...]:
    stack = stacks_by_backend.get(server.resource_id)
    if stack is None:
        return ()
    return tuple(
        rule
        for rule in inventory.alert_rules
        if rule.environment == stack.environment
    )


def _audit_alert_rules(
    alert_rules: tuple[AlertRuleInventory, ...],
    snapshot: ObservedServerSnapshot,
) -> tuple[AuditCheck, ...]:
    if not alert_rules:
        return ()
    raw_alerting = _optional_mapping(snapshot.spec.get("alerting"))
    observed_rules = (
        _observed_alert_rules(raw_alerting)
        if raw_alerting is not None
        and raw_alerting.get("prometheusAvailable") is True
        else None
    )
    checks: list[AuditCheck] = []
    for rule in alert_rules:
        rule_name = rule.resource_id.value.replace("-", "_")
        desired = {"name": rule_name, "health": "ok", "loaded": True}
        observed = (
            observed_rules.get(rule_name) if observed_rules is not None else None
        )
        checks.append(
            _comparison(
                f"alerting.rules[{rule.resource_id.value}]",
                desired,
                observed,
                matches=(
                    observed is not None and observed.get("health") == "ok"
                ),
            )
        )
    return tuple(checks)


def _observed_alert_rules(
    alerting: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    raw_rules_json = alerting.get("rulesJson")
    if not isinstance(raw_rules_json, str):
        return {}
    try:
        payload = cast("object", json.loads(raw_rules_json))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    observed: dict[str, dict[str, object]] = {}
    for group in _mapping_values_sequence(data.get("groups")):
        for raw_rule in _mapping_values_sequence(group.get("rules")):
            if raw_rule.get("type") != "alerting":
                continue
            name = raw_rule.get("name")
            if not isinstance(name, str):
                continue
            observed[name] = {
                "name": name,
                "health": raw_rule.get("health"),
                "state": raw_rule.get("state"),
                "loaded": True,
            }
    return observed


def _audit_component_environment(
    components: tuple[ComponentInventory, ...],
    environment_receipts: Mapping[str, str],
    snapshot: ObservedServerSnapshot,
) -> tuple[AuditCheck, ...]:
    audited = tuple(
        component
        for component in components
        if component.resource_id.value in environment_receipts
    )
    if not audited:
        return ()
    observed_entries = {
        str(entry.get("component")): entry
        for entry in _mapping_values_sequence(
            snapshot.spec.get("componentEnvironment")
        )
    }
    checks: list[AuditCheck] = []
    for component in audited:
        expected = environment_receipts[component.resource_id.value]
        entry = observed_entries.get(component.resource_id.value)
        observed = (
            {
                "exists": entry.get("exists"),
                "sha256": entry.get("sha256"),
            }
            if entry is not None
            else None
        )
        matches = (
            entry is not None
            and entry.get("exists") is True
            and entry.get("sha256") == expected
        )
        checks.append(
            _comparison(
                f"environment.file[{component.resource_id.value}]",
                {"exists": True, "sha256": expected},
                observed,
                matches=matches,
            )
        )
    return tuple(checks)


def _audit_configuration(
    baseline: BaselineInventory, snapshot: ObservedServerSnapshot
) -> tuple[AuditCheck, ...]:
    evidence = {
        _string(item, "path"): item
        for item in _mapping_sequence(snapshot.spec, "configuration")
    }
    return tuple(
        _audit_configuration_file(requirement, evidence.get(requirement.path.value))
        for requirement in baseline.configuration_files
    )


def _audit_configuration_file(
    requirement: ConfigurationFileRequirement,
    actual: Mapping[str, object] | None,
) -> AuditCheck:
    desired: dict[str, object] = {
        "exists": True,
        "capture": requirement.capture.value,
    }
    if requirement.owner is not None:
        desired["owner"] = requirement.owner.value
    if requirement.group is not None:
        desired["group"] = requirement.group.value
    if requirement.mode is not None:
        desired["mode"] = requirement.mode.value
    if requirement.sha256 is not None:
        desired["sha256"] = requirement.sha256.value

    if actual is None:
        return _comparison(
            f"configuration.files[{requirement.path.value}]",
            desired,
            None,
            matches=False,
        )
    observed = {
        key: actual.get(key)
        for key in ("exists", "capture", "owner", "group", "mode", "sha256")
    }
    matches = actual.get("exists") is True
    if requirement.capture.value == "hash":
        matches = matches and actual.get("sha256") is not None
    for key, expected in desired.items():
        matches = matches and observed.get(key) == expected
    return _comparison(
        f"configuration.files[{requirement.path.value}]",
        desired,
        observed,
        matches=matches,
    )


def _comparison(
    check: str, desired: object, observed: object, *, matches: bool
) -> AuditCheck:
    status = AuditStatus.COMPLIANT if matches else AuditStatus.DRIFT
    message = "desired state matches" if matches else "desired state does not match"
    return AuditCheck(
        check=check,
        status=status,
        desired=desired,
        observed=observed,
        message=message,
    )


def _aggregate_status(statuses: Iterable[AuditStatus]) -> AuditStatus:
    values = tuple(statuses)
    if AuditStatus.DRIFT in values:
        return AuditStatus.DRIFT
    if AuditStatus.UNKNOWN in values:
        return AuditStatus.UNKNOWN
    return AuditStatus.COMPLIANT


def _observed_raid_levels(mdstat: str) -> list[str]:
    return sorted(
        {
            level
            for level in ("raid0", "raid1", "raid5", "raid6", "raid10")
            if f"active {level}" in mdstat
        }
    )


def _flatten_block_devices(
    devices: tuple[Mapping[str, object], ...],
) -> tuple[Mapping[str, object], ...]:
    flattened: list[Mapping[str, object]] = []
    for device in devices:
        flattened.append(device)
        raw_children = device.get("children", [])
        flattened.extend(_flatten_block_devices(_mapping_values_sequence(raw_children)))
    return tuple(flattened)


def _mapping(content: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = content.get(key)
    result = _optional_mapping(value)
    if result is None:
        message = f"validated field {key!r} is not an object"
        raise TypeError(message)
    return result


def _optional_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in value
    ):
        return None
    return cast("Mapping[str, object]", value)


def _mapping_sequence(
    content: Mapping[str, object], key: str
) -> tuple[Mapping[str, object], ...]:
    return _mapping_values_sequence(content.get(key))


def _mapping_values_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        return ()
    result: list[Mapping[str, object]] = []
    for item in value:
        mapping = _optional_mapping(item)
        if mapping is None:
            return ()
        result.append(mapping)
    return tuple(result)


def _string(content: Mapping[str, object], key: str) -> str:
    value = content.get(key)
    if not isinstance(value, str):
        message = f"validated field {key!r} is not a string"
        raise TypeError(message)
    return value


def _integer(content: Mapping[str, object], key: str) -> int:
    value = content.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        message = f"validated field {key!r} is not an integer"
        raise TypeError(message)
    return value
