"""Which MCP tools change a project's servers, and whether a policy grants their run.

Over ``cloudfall mcp serve`` in project mode an agent confirms its own server
changes; spec 018 gates each such run on an ``OperatorPolicy`` grant per
environment. This module holds what that gate reads: the class each gated
tool runs (the ``kind`` a grant names), the servers a run changes, and the
grant check of every environment those servers are in, which applies the
operator's own grant rules. A run is self-confirmed only when every
environment it touches grants it.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from cloudfall.operator import (
    OperationKind,
    grant_decision,
    incomplete_history,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime

    from cloudfall.domain import ResourceId
    from cloudfall.inventory import PlatformInventory
    from cloudfall.operator import ProposalStore

GATED_TOOLS: Mapping[str, OperationKind] = MappingProxyType(
    {
        "deploy": OperationKind.DEPLOY,
        "rollback": OperationKind.ROLLBACK,
        "restart": OperationKind.RESTART,
        "migrate": OperationKind.MIGRATE,
        "data_copy": OperationKind.DATA_COPY,
        "domains_apply": OperationKind.DOMAINS_APPLY,
        "converge_baseline": OperationKind.CONVERGE_BASELINE,
        "converge_services": OperationKind.CONVERGE_SERVICES,
        "converge_domains": OperationKind.CONVERGE_DOMAINS,
    }
)
"""The project's MCP tools that change servers, and the class each runs (S-018).

The first six run with ``yes: true``, the ``converge_*`` tools with
``confirm_destructive: true``.
"""


def tool_class(tool: str) -> OperationKind:
    """Return the operation class a gated MCP tool runs."""
    try:
        return GATED_TOOLS[tool]
    except KeyError as error:
        message = f"tool {tool} is not gated: it changes no server"
        raise ValueError(message) from error


_ALL_SERVER_STEPS = frozenset({"baseline", "services"})
"""``migrate`` steps that converge every declared server."""
_DOMAIN_STEPS = frozenset({"domains-http", "domains-tls"})
"""``migrate`` steps that converge every Domain on its proxy server."""
_CONTROLLER_STEPS = frozenset(
    {
        "ttl-lower",
        "parallel-run",
        "dns-verify",
        "inspect",
        "audit",
        "verify-routes",
        "rollback-window",
    }
)
"""``migrate`` steps that change no server: they read, measure, or record."""


def declared_servers(inventory: PlatformInventory) -> tuple[ResourceId, ...]:
    """Every declared server: what a ``converge_*`` run over the fleet changes."""
    return tuple(server.resource_id for server in inventory.servers)


def domain_servers(
    inventory: PlatformInventory, domain_id: ResourceId | None
) -> tuple[ResourceId, ...]:
    """Return the proxy server of one Domain, or every server when none is named.

    ``converge_domains`` with ``domain`` runs on that Domain's proxy server
    alone; without it, on every declared server.
    """
    if domain_id is None:
        return declared_servers(inventory)
    for domain in inventory.domains:
        if domain.resource_id == domain_id:
            return (domain.proxy.server_id,)
    message = f"domain {domain_id.value} is not declared"
    raise ValueError(message)


def migrate_servers(
    inventory: PlatformInventory, pending_steps: Iterable[str]
) -> tuple[ResourceId, ...]:
    """Return the servers the pending steps of a ``migrate`` plan change.

    ``baseline`` and ``services`` change every declared server, a
    ``data:<database>`` step the server of the service declaring it, a
    ``deploy:<component>`` step the component's servers, and the Domain steps
    every Domain's proxy server; builds and checks change none.
    """
    servers: list[ResourceId] = []
    for step in pending_steps:
        kind, _, subject = step.partition(":")
        if step in _ALL_SERVER_STEPS:
            servers.extend(declared_servers(inventory))
        elif step in _DOMAIN_STEPS:
            servers.extend(domain.proxy.server_id for domain in inventory.domains)
        elif kind == "data" and subject:
            servers.extend(
                service.server_id
                for service in inventory.services
                if service.postgresql is not None
                and any(
                    database.name.value == subject
                    for database in service.postgresql.databases
                )
            )
        elif kind == "deploy" and subject:
            servers.extend(
                server
                for component in inventory.components
                if component.resource_id.value == subject
                for server in component.server_ids
            )
        elif not (kind == "build" and subject) and step not in _CONTROLLER_STEPS:
            message = f"migrate step {step} is not one this release plans"
            raise ValueError(message)
    return _unique(servers)


@dataclass(frozen=True, slots=True)
class EnvironmentGrant:
    """Whether one environment a run touches grants it, and why."""

    environment: ResourceId
    policy: ResourceId | None
    """The ``OperatorPolicy`` of the environment; none when it declares none."""
    granted: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        """Serialize for a plan's ``approval``."""
        return {
            "environment": self.environment.value,
            "policy": None if self.policy is None else self.policy.value,
            "granted": self.granted,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class GrantCheck:
    """What a confirmed call of a gated tool would do: run, or only propose."""

    operation_kind: OperationKind
    servers: tuple[ResourceId, ...]
    environments: tuple[EnvironmentGrant, ...]

    @property
    def self_confirm(self) -> bool:
        """The run is granted: it touches servers, and every environment grants it.

        A run that changes no declared server touches no environment that
        could grant it, so it is not self-confirmed.
        """
        return bool(self.environments) and all(
            environment.granted for environment in self.environments
        )

    def as_dict(self) -> dict[str, object]:
        """Serialize the fields a plan's ``approval`` gains (S-018-10)."""
        return {
            "selfConfirm": self.self_confirm,
            "environments": [
                environment.as_dict() for environment in self.environments
            ],
        }


def check_grant(
    operation_kind: OperationKind,
    servers: Iterable[ResourceId],
    inventory: PlatformInventory,
    store: ProposalStore,
    now: datetime,
) -> GrantCheck:
    """Check each environment of ``servers`` for a grant of ``operation_kind``.

    A server's environment is its ``Server.spec.environment``, and the policy
    is the one whose ``spec.environment`` names it. Each grant is judged by
    the operator's rules on the proposal store's settled records of that
    environment (D-19); a record the store cannot read withholds every
    grant, since it could be the failure that suspends one.
    """
    changed = _unique(servers)
    environments_by_server = {
        server.resource_id: server.environment for server in inventory.servers
    }
    undeclared = [server for server in changed if server not in environments_by_server]
    if undeclared:
        message = (
            "servers are not declared: "
            f"{', '.join(server.value for server in undeclared)}"
        )
        raise ValueError(message)
    policies = {policy.environment: policy for policy in inventory.operator_policies}
    listing = store.list()
    grants: list[EnvironmentGrant] = []
    for environment in _unique(environments_by_server[server] for server in changed):
        policy = policies.get(environment)
        if policy is None:
            grants.append(
                EnvironmentGrant(
                    environment=environment,
                    policy=None,
                    granted=False,
                    reason=(
                        "no operator policy declared for environment "
                        f"{environment.value}"
                    ),
                )
            )
            continue
        decision = (
            incomplete_history(listing.unreadable)
            if listing.unreadable
            else grant_decision(
                operation_kind,
                policy,
                listing.records,
                now,
                environments_by_server,
            )
        )
        grants.append(
            EnvironmentGrant(
                environment=environment,
                policy=policy.resource_id,
                granted=decision.granted,
                reason=decision.reason,
            )
        )
    return GrantCheck(
        operation_kind=operation_kind,
        servers=changed,
        environments=tuple(grants),
    )


def _unique(values: Iterable[ResourceId]) -> tuple[ResourceId, ...]:
    """Keep each value once, in the order first seen."""
    return tuple(dict.fromkeys(values))
