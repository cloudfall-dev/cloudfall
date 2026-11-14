"""Meet what the nodes of each Elasticsearch service need before ``services.yml``.

S-012 yields a data volume on every node, and on a cluster of two or more a
transport certificate from the project CA. The services converge meets them
the way ``logging deploy`` meets the logging stack's (S-011, S-014): the
nodes report their volume facts and the controller decides every volume's
action, or refuses before any host changes; each node makes its own key and
a signing request, and the CA of the service's environment signs those whose
certificate is missing or due, so a private key never leaves its host. The
signed certificates and the CA are staged in a private temp dir, removed
when the run ends, and the role copies them.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cloudfall.domain import ResourceKind
from cloudfall.lifecycle_error import LifecycleError
from cloudfall.logging_deploy import (
    DELIVERED_FILES_VARIABLE,
    REQUESTS_DIRECTORY_VARIABLE,
    REQUESTS_PLAYBOOK,
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
from cloudfall.requirements import ServerRequirements, requirements_of
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


@contextmanager
def prepared_nodes(
    run: PlaybookRunner, inventory: PlatformInventory, store: SecretStore
) -> Iterator[dict[str, object]]:
    """Meet each node's volume and certificate; yield ``services.yml``'s extra vars.

    Nothing to meet yields no variable, leaving the run as it was.
    """
    servers = elasticsearch_requirements(inventory)
    if not servers:
        yield {}
        return
    try:
        actions = _volume_actions(run, servers)
        files = _certificates(run, servers, inventory, store)
    except VolumeRefusedError as error:
        raise LifecycleError(
            error.code,
            str(error),
            {"server": error.server, "pool": error.pool, "reason": error.reason},
        ) from error
    except (LoggingDeployError, PkiError, SecretsError) as error:
        raise LifecycleError(ERROR_PROVIDER, error.message) from error
    with staged_files(files) as (staged, _):
        extra: dict[str, object] = {ACTIONS_VARIABLE: actions}
        if files:
            extra[DELIVERED_VARIABLE] = staged[DELIVERED_FILES_VARIABLE]
        yield extra
