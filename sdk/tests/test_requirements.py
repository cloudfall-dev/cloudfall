"""S-011: host requirements derived from the validated inventory."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from cloudfall.cli import main
from cloudfall.domain import (
    AbsolutePath,
    Hostname,
    ResourceId,
    ResourceKind,
)
from cloudfall.inventory import PlatformInventory
from cloudfall.requirements import (
    CertificateRequirement,
    CertificateUsage,
    PortRequirement,
    Requirement,
    RequirementOwner,
    RequirementType,
    SecretRequirement,
    SecretSource,
    ServerRequirements,
    requirements_of,
)
from cloudfall.validation import validate_config

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"


def _requirements(project: Path = EXAMPLES) -> dict[str, ServerRequirements]:
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    return {item.server_id.value: item for item in requirements_of(inventory)}


def _by_name(
    server: ServerRequirements, requirement_type: RequirementType
) -> dict[str, Requirement]:
    return {
        item.spec.name.value: item
        for item in server.requirements
        if item.spec.requirement_type is requirement_type
    }


def _single_host_project(tmp_path: Path) -> Path:
    project = tmp_path / "config"
    shutil.copytree(EXAMPLES, project)
    stack = project / "logging-stacks" / "operations.yaml"
    original = stack.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    kept = [line for line in lines if line.strip() != "- h2"]
    assert len(lines) - len(kept) == 2
    stack.write_text("".join(kept), encoding="utf-8")
    return project


def test_the_backend_carries_every_requirement_of_the_stack() -> None:
    backend = _requirements()["h1"]

    types = [item.spec.requirement_type for item in backend.requirements]
    assert types == sorted(types, key=list(RequirementType).index)
    assert set(_by_name(backend, RequirementType.VOLUME)) == {"loki", "prometheus"}
    assert all(
        item.owner
        == RequirementOwner(ResourceKind.LOGGING_STACK, ResourceId("operations"))
        for item in backend.requirements
    )
    secrets = _by_name(backend, RequirementType.SECRET)
    grafana = secrets["grafana-admin-password"].spec
    smtp = secrets["smtp-password-operations-email"].spec
    assert isinstance(grafana, SecretRequirement)
    assert isinstance(smtp, SecretRequirement)
    assert grafana.source is SecretSource.GENERATE
    assert smtp.source is SecretSource.INPUT
    assert smtp.path == AbsolutePath("/etc/cloudfall/logging/smtp-password")
    gateway = _by_name(backend, RequirementType.CERTIFICATE)["logging-gateway"].spec
    assert isinstance(gateway, CertificateRequirement)
    assert gateway.usage is CertificateUsage.SERVER
    assert gateway.subject_names == (Hostname("logs.example.internal"),)
    assert gateway.ca_path == AbsolutePath("/etc/cloudfall/logging/client-ca.crt")
    assert set(_by_name(backend, RequirementType.PACKAGE)) == {
        "loki",
        "grafana",
        "alloy",
    }
    assert set(_by_name(backend, RequirementType.DNS_NAME)) == {"logging-gateway"}


def test_the_gateway_port_opens_to_remote_collectors_only() -> None:
    port = _by_name(_requirements()["h1"], RequirementType.PORT)["logging-gateway"]

    assert isinstance(port.spec, PortRequirement)
    assert port.spec.sources == (ResourceId("h2"),)


def test_each_collector_gets_a_client_certificate_for_its_own_hostname() -> None:
    collector = _requirements()["h2"]

    certificate = _by_name(collector, RequirementType.CERTIFICATE)
    assert set(certificate) == {"logging-collector"}
    spec = certificate["logging-collector"].spec
    assert isinstance(spec, CertificateRequirement)
    assert spec.usage is CertificateUsage.CLIENT
    assert spec.subject_names == (Hostname("h2.example.internal"),)
    assert set(_by_name(collector, RequirementType.PACKAGE)) == {"alloy"}
    assert not _by_name(collector, RequirementType.VOLUME)


def test_a_single_host_stack_opens_no_port(tmp_path: Path) -> None:
    servers = _requirements(_single_host_project(tmp_path))

    assert set(servers) == {"h1"}
    assert not _by_name(servers["h1"], RequirementType.PORT)


def test_a_server_rejects_two_requirements_of_one_type_and_name() -> None:
    owner = RequirementOwner(ResourceKind.LOGGING_STACK, ResourceId("operations"))
    secret = Requirement(
        owner,
        SecretRequirement(
            ResourceId("grafana-admin-password"),
            SecretSource.GENERATE,
            AbsolutePath("/etc/cloudfall/logging/grafana-admin-password"),
        ),
    )

    with pytest.raises(ValueError, match="duplicate secret requirement"):
        ServerRequirements(ResourceId("h1"), (secret, secret))


def test_requirements_show_answers_the_requirements_by_server(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["requirements", "show", "--project", str(EXAMPLES)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["data"]["status"] == "ok"
    servers = payload["data"]["servers"]
    assert [server["server"] for server in servers] == ["h1", "h2"]
    secret = next(
        item
        for item in servers[0]["requirements"]
        if item["name"] == "smtp-password-operations-email"
    )
    assert secret == {
        "type": "secret",
        "name": "smtp-password-operations-email",
        "owner": {"kind": "LoggingStack", "id": "operations"},
        "spec": {"source": "input", "path": "/etc/cloudfall/logging/smtp-password"},
    }
