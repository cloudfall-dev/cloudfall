"""Render deterministic static Ansible inventory from typed Cloudfall inventory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloudfall.inventory import (
    firewall_rules_for_server,
    remote_access_rules_for_server,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from cloudfall.domain import ResourceId
    from cloudfall.inventory import (
        AlertRuleInventory,
        ApplicationInventory,
        ComponentInventory,
        LoggingStackInventory,
        PlatformInventory,
        ServerInventory,
        ServerTypeInventory,
        ServiceInventory,
    )


def render_ansible_inventory(inventory: PlatformInventory) -> dict[str, object]:
    """Render static JSON accepted by Ansible's YAML inventory plugin."""
    environment_groups: dict[str, object] = {}
    environments = sorted(
        {server.environment for server in inventory.servers},
        key=lambda environment: environment.value,
    )
    for environment in environments:
        environment_groups[_group_name("environment", environment)] = {
            "hosts": _empty_host_entries(
                server.resource_id.value
                for server in inventory.servers
                if server.environment == environment
            )
        }

    application_groups: dict[str, object] = {}
    for application in inventory.applications:
        application_components = frozenset(application.component_ids)
        application_hosts = sorted(
            {
                server_id.value
                for component in inventory.components
                if component.resource_id in application_components
                for server_id in component.server_ids
            }
        )
        application_groups[_group_name("application", application.resource_id)] = {
            "hosts": _empty_host_entries(application_hosts)
        }

    component_groups = {
        _group_name("component", component.resource_id): {
            "hosts": _empty_host_entries(
                server.value for server in component.server_ids
            )
        }
        for component in inventory.components
    }

    logging_backend_hosts = _empty_host_entries(
        stack.backend.server_id.value for stack in inventory.logging_stacks
    )
    logging_collector_hosts = _empty_host_entries(
        server_id.value
        for stack in inventory.logging_stacks
        for server_id in stack.collectors.server_ids
    )
    domain_origin_hosts = _empty_host_entries(
        domain.origin.server_id.value for domain in inventory.domains
    )

    return {
        "all": {
            "children": {
                "ungrouped": {"hosts": {}},
                "cloudfall_servers": {"hosts": _host_variables(inventory)},
                "cloudfall_environments": {"children": environment_groups},
                "cloudfall_applications": {"children": application_groups},
                "cloudfall_components": {"children": component_groups},
                "cloudfall_logging_backends": {"hosts": logging_backend_hosts},
                "cloudfall_logging_collectors": {"hosts": logging_collector_hosts},
                "cloudfall_domain_origins": {"hosts": domain_origin_hosts},
                # deploy.yml adds the deployed component's release server here,
                # so the server that runs its release command deploys first.
                "cloudfall_release_servers": {"hosts": {}},
            }
        }
    }


def _host_variables(inventory: PlatformInventory) -> dict[str, object]:
    applications_by_id = {
        application.resource_id: application for application in inventory.applications
    }
    server_types_by_id = {
        server_type.resource_id: server_type for server_type in inventory.server_types
    }
    return {
        server.resource_id.value: {
            "ansible_host": server.address.value,
            "ansible_port": server.ssh_port.value,
            "ansible_user": server.ssh_user.value,
            "cloudfall_server_id": server.resource_id.value,
            "cloudfall_hostname": server.hostname.value,
            "cloudfall_environment": server.environment.value,
            "cloudfall_lifecycle": server.lifecycle.value,
            "cloudfall_labels": {
                label.key.value: label.value for label in server.labels
            },
            "cloudfall_ssh_public_keys": [
                key.as_dict()
                for key in inventory.ssh_public_keys
                if key.environment == server.environment and key.is_active
            ],
            **_host_infrastructure(server),
            **_host_firewall(
                server,
                server_types_by_id[server.server_type_id],
                inventory.services,
            ),
            **_host_logging(server, inventory.logging_stacks, inventory.alert_rules),
            "cloudfall_server_type": server_types_by_id[
                server.server_type_id
            ].as_dict(),
            "cloudfall_applications": [
                _host_application(application)
                for application in inventory.applications
                if _application_is_on_server(application, server, inventory)
            ],
            "cloudfall_components": [
                _host_component(component, applications_by_id[component.application_id])
                for component in inventory.components
                if server.resource_id in component.server_ids
            ],
            "cloudfall_services": [
                service.as_dict()
                for service in inventory.services
                if service.server_id == server.resource_id
            ],
            "cloudfall_domains": [
                domain.as_dict()
                for domain in inventory.domains
                if domain.proxy.server_id == server.resource_id
            ],
            "cloudfall_origin_domains": [
                domain.as_dict()
                for domain in inventory.domains
                if domain.origin.server_id == server.resource_id
            ],
        }
        for server in inventory.servers
    }


def _host_infrastructure(server: ServerInventory) -> dict[str, object]:
    variables: dict[str, object] = {}
    if server.provider is not None:
        variables["cloudfall_provider"] = server.provider.as_dict()
    if server.network is not None:
        variables["cloudfall_network"] = server.network.as_dict()
    return variables


def _host_firewall(
    server: ServerInventory,
    server_type: ServerTypeInventory,
    services: tuple[ServiceInventory, ...],
) -> dict[str, object]:
    remote_access = remote_access_rules_for_server(server, services)
    if server_type.firewall is None:
        if remote_access:
            message = (
                "PostgreSQL remoteAccess needs a firewall on its server: "
                f"{server.resource_id.value}"
            )
            raise ValueError(message)
        return {}
    rules = firewall_rules_for_server(server_type.firewall, server.ssh_port)
    return {
        "cloudfall_firewall": {
            "policy": server_type.firewall.policy.value,
            "allowedInbound": [
                *(rule.as_dict() for rule in rules),
                *(rule.as_dict() for rule in remote_access),
            ],
        }
    }


def _host_logging(
    server: ServerInventory,
    logging_stacks: tuple[LoggingStackInventory, ...],
    alert_rules: tuple[AlertRuleInventory, ...],
) -> dict[str, object]:
    variables: dict[str, object] = {}
    backend = next(
        (
            stack
            for stack in logging_stacks
            if stack.backend.server_id == server.resource_id
        ),
        None,
    )
    collector = next(
        (
            stack
            for stack in logging_stacks
            if server.resource_id in stack.collectors.server_ids
        ),
        None,
    )
    if backend is not None:
        variables["cloudfall_logging_backend"] = backend.as_dict()
        variables["cloudfall_alert_rules"] = [
            rule.as_dict()
            for rule in sorted(
                alert_rules, key=lambda rule: rule.resource_id.value
            )
            if rule.environment == backend.environment
        ]
    if collector is not None:
        variables["cloudfall_logging_collector"] = collector.as_dict()
    return variables


def _host_application(application: ApplicationInventory) -> dict[str, object]:
    return {
        "id": application.resource_id.value,
        "user": application.linux_user.value,
        "approval": application.approval.value,
    }


def _host_component(
    component: ComponentInventory, application: ApplicationInventory
) -> dict[str, object]:
    variables: dict[str, object] = {
        "id": component.resource_id.value,
        "application": component.application_id.value,
        "application_user": application.linux_user.value,
        "install_root": component.install_root.value,
        "repository": component.repository.as_dict(),
        "runtime": component.runtime.as_dict(),
        "service": component.service.as_dict(),
        "healthCheck": component.health_check.as_dict(),
        "retainUntilCleanup": component.retain_until_cleanup,
    }
    if component.release is not None:
        variables["release"] = {
            **component.release.as_dict(),
            "server": component.release_server_id.value,
        }
    return variables


def _application_is_on_server(
    application: ApplicationInventory,
    server: ServerInventory,
    inventory: PlatformInventory,
) -> bool:
    application_components = frozenset(application.component_ids)
    return any(
        component.resource_id in application_components
        and server.resource_id in component.server_ids
        for component in inventory.components
    )


def _empty_host_entries(hosts: Iterable[str]) -> dict[str, object]:
    return {host: {} for host in hosts}


def _group_name(prefix: str, resource_id: ResourceId) -> str:
    return f"{prefix}_{resource_id.value.replace('-', '_')}"
