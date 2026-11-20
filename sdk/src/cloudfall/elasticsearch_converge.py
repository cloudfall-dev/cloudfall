"""Meet what the nodes of each Elasticsearch service need before ``services.yml``.

S-012 yields a data volume on every node, and on a cluster of two or more a
transport certificate from the project CA. The services converge meets them
the way ``logging deploy`` meets the logging stack's (S-011, S-014): the
nodes report their volume facts and the controller decides every volume's
action, or refuses before any host changes; each node makes its own key and
a signing request, and the CA of the service's environment signs those whose
certificate is missing or due, so a private key never leaves its host. The
signed certificates and the CA are staged in a private temp dir, removed
when the run ends, and the role copies them. A service that joins an
``existingCluster`` also stages that cluster's CA certificate, read from the
project, for each of its nodes. A MariaDB server with ``remoteAccess`` or
``replication`` gets its server certificate the same way (S-019).
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cloudfall.domain import ResourceKind
from cloudfall.elasticsearch_cluster import EXISTING_CLUSTER_CA_PATH
from cloudfall.lifecycle_error import LifecycleError
from cloudfall.logging_deploy import (
    DELIVERED_FILES_VARIABLE,
    REQUESTS_DIRECTORY_VARIABLE,
    REQUESTS_PLAYBOOK,
    DeliveryRole,
    LoggingDeployError,
    StagedFile,
    certificate_files,
    certificate_requests,
    read_requests,
    staged_files,
)
from cloudfall.logging_deploy import (
    REQUESTS_VARIABLE as CERTIFICATE_REQUESTS_VARIABLE,
)
from cloudfall.pki import PkiError, load_or_create_ca
from cloudfall.rabbitmq_access import CA_PATH as RABBITMQ_CA_PATH
from cloudfall.rabbitmq_access import CERTIFICATE_PATH as RABBITMQ_CERTIFICATE_PATH
from cloudfall.requirements import (
    MARIADB_CA_PATH,
    MARIADB_CERTIFICATE_PATH,
    CertificateRequirement,
    Requirement,
    ServerRequirements,
    VolumeRequirement,
    requirements_of,
)
from cloudfall.secrets import SecretsError
from cloudfall.volumes import (
    ACTIONS_VARIABLE,
    FACTS_PLAYBOOK,
    REPORT_DIRECTORY_VARIABLE,
    VolumeRefusedError,
    actions_by_server,
    normalize_report,
    plan_volumes,
    read_reports,
    volume_requests,
)
from cloudfall.volumes import (
    REQUESTS_VARIABLE as VOLUME_REQUESTS_VARIABLE,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from cloudfall.domain import ResourceId
    from cloudfall.inventory import PlatformInventory
    from cloudfall.secret_store import SecretStore

    type PlaybookRunner = Callable[[str, Mapping[str, object]], None]

PROVIDER_HOSTS_VARIABLE = "cloudfall_provider_hosts"
"""The hosts ``logging-volumes.yml`` and ``logging-requests.yml`` read: the nodes."""
DELIVERED_VARIABLE = "cloudfall_services_delivered_files"
"""``services.yml``'s variable: server id, then the files the role copies."""
MARIADB_DELIVERED_VARIABLE = "cloudfall_services_mariadb_delivered_files"
"""The same for the MariaDB role: the server certificate and the project CA."""
_MARIADB_PATHS = frozenset({MARIADB_CERTIFICATE_PATH.value, MARIADB_CA_PATH.value})
RABBITMQ_DELIVERED_VARIABLE = "cloudfall_services_rabbitmq_delivered_files"
"""The same for the RabbitMQ role: a node's certificate and the project CA."""
_RABBITMQ_PATHS = frozenset({RABBITMQ_CERTIFICATE_PATH.value, RABBITMQ_CA_PATH.value})
ERROR_PROVIDER = "services_requirement_unmet"
"""A node requirement the controller could not meet; no host changed."""


def elasticsearch_requirements(
    inventory: PlatformInventory,
) -> tuple[ServerRequirements, ...]:
    """Return what the Elasticsearch services place on their nodes, and nothing else."""
    services = {
        service.resource_id
        for service in inventory.services
        if service.elasticsearch is not None
    }
    found: list[ServerRequirements] = []
    for server in requirements_of(inventory):
        owned = tuple(
            item
            for item in server.requirements
            if item.owner.kind is ResourceKind.SERVICE
            and item.owner.resource_id in services
        )
        if owned:
            found.append(ServerRequirements(server.server_id, owned))
    return tuple(found)


def mariadb_volume_requirements(
    inventory: PlatformInventory,
) -> tuple[ServerRequirements, ...]:
    """Return the data volume each MariaDB service places on its server (S-019).

    Only the volume: the server certificate comes with
    ``mariadb_certificate_requirements``, and a primary's port with the
    baseline's firewall.
    """
    services = {
        service.resource_id
        for service in inventory.services
        if service.mariadb is not None
    }
    return _owned(inventory, services, VolumeRequirement)


def rabbitmq_volume_requirements(
    inventory: PlatformInventory,
) -> tuple[ServerRequirements, ...]:
    """Return the data volume each RabbitMQ service places on its nodes (S-023).

    Only the volume: the Erlang cookie is staged with the owners' passwords,
    and a node's certificate comes with ``rabbitmq_certificate_requirements``.
    """
    services = {
        service.resource_id
        for service in inventory.services
        if service.rabbitmq is not None
    }
    return _owned(inventory, services, VolumeRequirement)


def rabbitmq_certificate_requirements(
    inventory: PlatformInventory,
) -> tuple[ServerRequirements, ...]:
    """Return the certificate of each node of a RabbitMQ service that needs one.

    The nodes of three verify each other's certificate against the project
    CA in both directions, and the clients of ``clientAccess`` verify the
    node's (S-023); one node without ``clientAccess`` needs none.
    """
    services = {
        service.resource_id
        for service in inventory.services
        if service.rabbitmq is not None
    }
    return _owned(inventory, services, CertificateRequirement)


def mariadb_certificate_requirements(
    inventory: PlatformInventory,
) -> tuple[ServerRequirements, ...]:
    """Return the server certificate of each MariaDB service that needs one.

    A service with ``remoteAccess`` serves TLS to its owners' clients, and one
    with ``replication`` to the other server of its pair, which verifies it
    against the project CA. A replica of a source Cloudfall does not manage
    gets one too: the role's settings name it for every server with
    ``replication``, and once promoted its own replicas verify it (#494).
    The source's CA, ``caCertificateFile``, is staged apart from it. These
    are the certificate requirements ``cloudfall requirements show`` lists.
    """
    services = {
        service.resource_id
        for service in inventory.services
        if service.mariadb is not None
        and (
            service.mariadb.remote_access is not None
            or service.mariadb.replication is not None
        )
    }
    return _owned(inventory, services, CertificateRequirement)


def _owned(
    inventory: PlatformInventory,
    services: set[ResourceId],
    kind: type[VolumeRequirement | CertificateRequirement],
) -> tuple[ServerRequirements, ...]:
    """Return the requirements of ``kind`` that ``services`` place, by server."""
    found: list[ServerRequirements] = []
    for server in requirements_of(inventory):
        owned = tuple(
            item
            for item in server.requirements
            if item.owner.kind is ResourceKind.SERVICE
            and item.owner.resource_id in services
            and isinstance(item.spec, kind)
        )
        if owned:
            found.append(ServerRequirements(server.server_id, owned))
    return tuple(found)


def _merged(
    *groups: tuple[ServerRequirements, ...],
) -> tuple[ServerRequirements, ...]:
    """Join the requirements of each server across groups, in server order."""
    by_server: dict[ResourceId, list[Requirement]] = {}
    for group in groups:
        for server in group:
            by_server.setdefault(server.server_id, []).extend(server.requirements)
    return tuple(
        ServerRequirements(server_id, tuple(requirements))
        for server_id, requirements in by_server.items()
    )


def _hosts(servers: tuple[ServerRequirements, ...]) -> str:
    """Return the Ansible host pattern of the nodes: their ids, joined by ``:``."""
    return ":".join(server.server_id.value for server in servers)


def _volume_actions(
    run: PlaybookRunner, servers: tuple[ServerRequirements, ...]
) -> dict[str, list[dict[str, object]]]:
    """Read the nodes' volume facts and decide each data volume's action."""
    requested = volume_requests(servers)
    if not requested:
        return {}
    # Read only: every refusal is decided before any host changes (S-014).
    with tempfile.TemporaryDirectory(prefix="cloudfall-volumes-") as reports:
        run(
            FACTS_PLAYBOOK,
            {
                PROVIDER_HOSTS_VARIABLE: _hosts(servers),
                VOLUME_REQUESTS_VARIABLE: requested,
                REPORT_DIRECTORY_VARIABLE: reports,
            },
        )
        facts = {
            server: normalize_report(report)
            for server, report in read_reports(Path(reports), servers).items()
        }
    return actions_by_server(plan_volumes(servers, facts))


def _certificates(
    run: PlaybookRunner,
    servers: tuple[ServerRequirements, ...],
    inventory: PlatformInventory,
    store: SecretStore,
) -> list[StagedFile]:
    """Have each node request its transport certificate; sign the missing and due."""
    requests = certificate_requests(servers)
    if not requests:
        return []
    with tempfile.TemporaryDirectory(prefix="cloudfall-requests-") as reports:
        run(
            REQUESTS_PLAYBOOK,
            {
                PROVIDER_HOSTS_VARIABLE: ":".join(requests),
                CERTIFICATE_REQUESTS_VARIABLE: requests,
                REQUESTS_DIRECTORY_VARIABLE: reports,
            },
        )
        held = read_requests(Path(reports), servers)
    environments = {
        service.resource_id: service.environment for service in inventory.services
    }
    by_environment: dict[ResourceId, list[ServerRequirements]] = {}
    for server in servers:
        for requirement in server.requirements:
            environment = environments[requirement.owner.resource_id]
            by_environment.setdefault(environment, []).append(
                ServerRequirements(server.server_id, (requirement,))
            )
    now = datetime.now(UTC)
    files: list[StagedFile] = []
    for environment, placed in by_environment.items():
        ca, _ = load_or_create_ca(store, environment, now)
        signed, _ = certificate_files(tuple(placed), held, ca, now)
        files.extend(signed)
    return files


EXISTING_CLUSTER_CA_NAME = "elasticsearch-existing-cluster-ca"
"""How the staged CA of an existing cluster is named; the role copies it 0644."""


def existing_cluster_ca_files(
    inventory: PlatformInventory, project_directory: Path
) -> list[StagedFile]:
    """Stage the CA of each joined existing cluster for every node of its service.

    Validation checked the file is CA certificates only, inside the project.
    """
    files: list[StagedFile] = []
    for service in inventory.services:
        elasticsearch = service.elasticsearch
        if elasticsearch is None or elasticsearch.existing_cluster is None:
            continue
        relative = elasticsearch.existing_cluster.ca_certificate_file.value
        content = (project_directory / relative).read_text(encoding="utf-8")
        files.extend(
            StagedFile(
                node,
                DeliveryRole.COLLECTOR,
                EXISTING_CLUSTER_CA_NAME,
                EXISTING_CLUSTER_CA_PATH.value,
                content,
                "0644",
                force=True,
            )
            for node in elasticsearch.nodes
        )
    return files


@contextmanager
def prepared_nodes(
    run: PlaybookRunner,
    inventory: PlatformInventory,
    store: SecretStore,
    project_directory: Path,
    server: ResourceId | None = None,
) -> Iterator[dict[str, object]]:
    """Meet each node's volume and certificate; yield ``services.yml``'s extra vars.

    A MariaDB server's data volume is decided with the nodes' (S-019), in the
    same read before any host changes, and the server certificate of one
    with ``remoteAccess`` or ``replication`` is requested and signed with the
    nodes'; it is
    handed to the MariaDB role apart from the nodes' files, so neither role
    copies the other's. A RabbitMQ node's data volume is decided in the same
    read, and the certificate of a node of three or with ``clientAccess`` is
    requested and signed with the others and handed to the RabbitMQ role
    alone (S-023). Nothing to meet yields no variable, leaving the run as it
    was. ``server`` meets that one server's alone, for a run limited to it.
    """
    servers = tuple(
        found
        for found in elasticsearch_requirements(inventory)
        if server is None or found.server_id == server
    )
    databases = tuple(
        found
        for found in _merged(
            mariadb_volume_requirements(inventory),
            rabbitmq_volume_requirements(inventory),
        )
        if server is None or found.server_id == server
    )
    database_certificates = tuple(
        found
        for found in _merged(
            mariadb_certificate_requirements(inventory),
            rabbitmq_certificate_requirements(inventory),
        )
        if server is None or found.server_id == server
    )
    if not servers and not databases:
        yield {}
        return
    try:
        actions = _volume_actions(run, _merged(servers, databases))
        signed = _certificates(
            run, _merged(servers, database_certificates), inventory, store
        )
        files = [
            *(
                item
                for item in signed
                if item.path not in _MARIADB_PATHS | _RABBITMQ_PATHS
            ),
            *(
                staged
                for staged in existing_cluster_ca_files(inventory, project_directory)
                if server is None or staged.server == server
            ),
        ]
        database_files = [item for item in signed if item.path in _MARIADB_PATHS]
        broker_files = [item for item in signed if item.path in _RABBITMQ_PATHS]
    except VolumeRefusedError as error:
        raise LifecycleError(
            error.code,
            str(error),
            {"server": error.server, "pool": error.pool, "reason": error.reason},
        ) from error
    except (LoggingDeployError, PkiError, SecretsError) as error:
        raise LifecycleError(ERROR_PROVIDER, error.message) from error
    with (
        staged_files(files) as (staged, _),
        staged_files(database_files) as (database_staged, _),
        staged_files(broker_files) as (broker_staged, _),
    ):
        extra: dict[str, object] = {ACTIONS_VARIABLE: actions}
        if files:
            extra[DELIVERED_VARIABLE] = staged[DELIVERED_FILES_VARIABLE]
        if database_files:
            extra[MARIADB_DELIVERED_VARIABLE] = database_staged[
                DELIVERED_FILES_VARIABLE
            ]
        if broker_files:
            extra[RABBITMQ_DELIVERED_VARIABLE] = broker_staged[
                DELIVERED_FILES_VARIABLE
            ]
        yield extra
