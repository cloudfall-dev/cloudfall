"""Reading a fleet from the team's own Ansible inventory."""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest
from cloudfall.ansible_api import (
    ANSIBLE_CONFIG_FILE,
    AnsibleReadError,
    InventorySource,
    read_inventory,
)
from cloudfall.ansible_reader import read_fleet
from cloudfall.cli import main
from cloudfall.inventory import PlatformInventory
from cloudfall.resources import default_schema_directory
from cloudfall.validation import ConfigValidationError

SCHEMAS = default_schema_directory()

HOSTS = """\
---
all:
  children:
    app_servers:
      hosts:
        web-1:
          ansible_host: 10.0.0.1
        web-2:
          ansible_host: 10.0.0.2
    builders:
      hosts:
        build-1:
          ansible_host: 10.0.0.9
"""

GROUP_VARS = """\
---
ansible_user: ansible
cloudfall_server_types:
  web:
    description: Debian web baseline
    os:
      distribution: Debian
      versions:
        - '13'
      service_manager: systemd
    storage:
      mounts:
        - path: /
          filesystem: ext4
          minimum_bytes: 20000000000
    packages:
      required:
        - name: curl
      forbidden:
        - telnetd
    services:
      required:
        - name: ssh.service
          state: running
          status: enabled
    configuration:
      files:
        - path: /etc/ssh/sshd_config
          capture: hash
"""

WEB_1 = """\
---
cloudfall:
  description: First web host
  environment: production
  server_type: web
  lifecycle: active
  provider:
    name: hetzner
    server_id: '1056391'
  network:
    ipv4: 10.0.0.1
    ipv6_cidr: 2a01:4f8:121:50f2::/64
    reverse_dns: web-1.example.test
  labels:
    workload: web
"""

WEB_2 = """\
---
cloudfall:
  environment: production
  server_type: web
  lifecycle: active
"""


def _inventory(
    tmp_path: Path,
    *,
    group_vars: str = GROUP_VARS,
    web_1: str = WEB_1,
    web_2: str | None = WEB_2,
    build_1: str | None = None,
) -> InventorySource:
    root = tmp_path / "inventory"
    (root / "group_vars" / "all").mkdir(parents=True)
    (root / "host_vars").mkdir(parents=True)
    (root / "hosts.yml").write_text(HOSTS, encoding="utf-8")
    (root / "group_vars" / "all" / "main.yml").write_text(group_vars, encoding="utf-8")
    (root / "host_vars" / "web-1.yml").write_text(web_1, encoding="utf-8")
    if web_2 is not None:
        (root / "host_vars" / "web-2.yml").write_text(web_2, encoding="utf-8")
    if build_1 is not None:
        (root / "host_vars" / "build-1.yml").write_text(build_1, encoding="utf-8")
    return InventorySource(root)


def test_the_wrapper_reads_hosts_groups_and_merged_variables(tmp_path: Path) -> None:
    inventory = read_inventory(_inventory(tmp_path))

    assert inventory.in_process is True
    assert [str(host.name) for host in inventory.hosts] == [
        "build-1",
        "web-1",
        "web-2",
    ]
    web_1 = inventory.hosts[1]
    assert [str(group) for group in web_1.groups] == ["all", "app_servers"]
    assert web_1.variable("ansible_host") == "10.0.0.1"
    assert web_1.variable("ansible_user") == "ansible"


def test_the_command_fallback_reads_the_same_fleet(tmp_path: Path) -> None:
    """The fallback exists for a release that moves the Python API."""
    source = _inventory(tmp_path)

    in_process = read_inventory(source)
    fallback = read_inventory(source, in_process=False)

    assert fallback.in_process is False
    assert [str(host.name) for host in fallback.hosts] == [
        str(host.name) for host in in_process.hosts
    ]
    assert [
        [str(group) for group in host.groups] for host in fallback.hosts
    ] == [[str(group) for group in host.groups] for host in in_process.hosts]


def test_a_missing_inventory_fails_at_construction(tmp_path: Path) -> None:
    with pytest.raises(AnsibleReadError) as error:
        InventorySource(tmp_path / "absent")

    assert error.value.code == "ansible_inventory_missing"


def test_the_reader_builds_servers_from_ansible_and_the_declaration(
    tmp_path: Path,
) -> None:
    read = read_fleet(read_inventory(_inventory(tmp_path)), SCHEMAS)

    assert read.managed == ("web-1", "web-2")
    assert read.unmanaged == ("build-1",)
    inventory = PlatformInventory.from_state(read.config)
    web_1 = inventory.servers[0].as_dict()
    assert web_1 == {
        "id": "web-1",
        "hostname": "web-1",
        "address": "10.0.0.1",
        "environment": "production",
        "serverType": "web",
        "lifecycle": "active",
        "ssh": {"user": "ansible", "port": 22},
        "labels": {"workload": "web"},
        "provider": {"name": "hetzner", "serverId": "1056391"},
        "network": {
            "ipv4": "10.0.0.1",
            "ipv6Cidr": "2a01:4f8:121:50f2::/64",
            "reverseDns": "web-1.example.test",
        },
    }


def test_the_reader_rewrites_ansible_case_into_the_schemas_case(
    tmp_path: Path,
) -> None:
    """`server_id` and `minimum_bytes` are Ansible style; the schemas are not."""
    read = read_fleet(read_inventory(_inventory(tmp_path)), SCHEMAS)

    inventory = PlatformInventory.from_state(read.config)
    server_type = inventory.server_types[0]
    assert server_type.resource_id.value == "web"
    assert server_type.os.service_manager.value == "systemd"
    assert server_type.mounts[0].minimum_bytes.value == 20000000000


def test_the_declaration_beats_the_group_defaults(tmp_path: Path) -> None:
    group_vars = GROUP_VARS + textwrap.dedent("""\
        cloudfall_defaults:
          environment: staging
          lifecycle: retired
        """)
    web_2 = textwrap.dedent("""\
        ---
        cloudfall:
          environment: production
          server_type: web
        """)

    read = read_fleet(
        read_inventory(_inventory(tmp_path, group_vars=group_vars, web_2=web_2)),
        SCHEMAS,
    )

    inventory = PlatformInventory.from_state(read.config)
    server = next(s for s in inventory.servers if s.resource_id.value == "web-2")
    assert server.environment.value == "production"
    assert server.lifecycle.value == "retired"


def test_a_host_without_a_declaration_is_left_alone(tmp_path: Path) -> None:
    """An inventory holds hosts Cloudfall was never asked to manage."""
    read = read_fleet(read_inventory(_inventory(tmp_path, web_2=None)), SCHEMAS)

    assert read.managed == ("web-1",)
    assert read.unmanaged == ("build-1", "web-2")


def test_an_inventory_declaring_nothing_is_refused(tmp_path: Path) -> None:
    source = _inventory(tmp_path, web_1="---\n{}\n", web_2=None)

    with pytest.raises(AnsibleReadError) as error:
        read_fleet(read_inventory(source), SCHEMAS)

    assert error.value.code == "ansible_no_declarations"


def test_a_declaration_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    source = _inventory(tmp_path, web_1="---\ncloudfall: production\n")

    with pytest.raises(AnsibleReadError) as error:
        read_fleet(read_inventory(source), SCHEMAS)

    assert error.value.code == "ansible_declaration_invalid"


def test_two_hosts_may_not_see_different_catalogs(tmp_path: Path) -> None:
    conflicting = WEB_2 + textwrap.dedent("""\
        cloudfall_server_types:
          web:
            description: A different web
        """)
    source = _inventory(tmp_path, web_2=conflicting)

    with pytest.raises(AnsibleReadError) as error:
        read_fleet(read_inventory(source), SCHEMAS)

    assert error.value.code == "ansible_catalog_conflict"


def test_a_bad_declaration_reports_the_host_vars_file(tmp_path: Path) -> None:
    """The error must name the file the team has to edit."""
    broken = textwrap.dedent("""\
        ---
        cloudfall:
          environment: production
          server_type: web
          lifecycle: sleeping
        """)
    source = _inventory(tmp_path, web_1=broken)

    with pytest.raises(ConfigValidationError) as error:
        read_fleet(read_inventory(source), SCHEMAS)

    issue = error.value.issue
    assert issue.code == "schema_validation_failed"
    assert issue.source is not None
    assert issue.source.path.name == "web-1.yml"


def test_an_unknown_server_type_fails_the_reference_check(tmp_path: Path) -> None:
    """The same cross-resource check a project directory gets."""
    unknown = WEB_1.replace("server_type: web", "server_type: absent")
    source = _inventory(tmp_path, web_1=unknown, web_2=None)

    with pytest.raises(ConfigValidationError) as error:
        read_fleet(read_inventory(source), SCHEMAS)

    assert error.value.issue.code == "resource_reference_missing"


def test_the_cli_reads_the_fleet_from_a_named_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _inventory(tmp_path)

    exit_code = main(["inventory", "show", "--inventory", str(source)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert [server["id"] for server in payload["data"]["inventory"]["servers"]] == [
        "web-1",
        "web-2",
    ]
    assert payload["data"]["ansible"]["managed"] == ["web-1", "web-2"]
    assert payload["data"]["ansible"]["unmanaged"] == ["build-1"]


def test_the_cli_finds_the_inventory_an_ansible_cfg_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An Ansible control repository needs no Cloudfall-side configuration."""
    source = _inventory(tmp_path)
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ANSIBLE_CONFIG_FILE).write_text(
        f"[defaults]\ninventory = {source}\n", encoding="utf-8"
    )

    before = Path.cwd()
    os.chdir(repository)
    try:
        exit_code = main(["inventory", "show"])
    finally:
        os.chdir(before)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["data"]["ansible"]["inProcess"] is True
    assert [server["id"] for server in payload["data"]["inventory"]["servers"]] == [
        "web-1",
        "web-2",
    ]


def test_the_cli_refuses_two_fleets_at_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _inventory(tmp_path)

    exit_code = main(
        [
            "inventory",
            "show",
            "--inventory",
            str(source),
            "--project",
            str(tmp_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"]["code"] == "ARG_ERROR"
    assert "--project" in json.dumps(payload["error"])


def test_the_cli_reports_a_missing_inventory_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["inventory", "show", "--inventory", str(tmp_path / "absent")])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 85
    assert payload["error"]["code"] == "INVENTORY_UNREADABLE"
    assert payload["error"]["context"]["code"] == "ansible_inventory_missing"
