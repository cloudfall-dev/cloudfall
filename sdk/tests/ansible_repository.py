"""A team's own Ansible control repository, as ``cloudfall mcp serve`` reads it.

One host, ``web-1``, declares a ``cloudfall`` block on the baseline ``web`` the
``all`` group's ``cloudfall_baselines`` holds; ``operations/`` declares the
operations the caller names (``write-marker`` when none).
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

HOSTS = """\
---
all:
  children:
    app_servers:
      hosts:
        web-1:
          ansible_host: 127.0.0.1
"""

GROUP_VARS = """\
---
ansible_user: ansible
cloudfall_baselines:
  web:
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
  environment: production
  baseline: web
  lifecycle: active
"""

MARKER = """\
id: write-marker
description: Write the release marker
playbook: playbooks/marker.yml
risk: mutating
targets: host
inputs:
  version: string
verify:
  playbook: playbooks/marker.yml
"""

PURGE = """\
id: purge
description: Remove what nothing points at
playbook: playbooks/marker.yml
risk: destructive
targets: fleet
verify:
  playbook: playbooks/marker.yml
"""

PLAYBOOK = """\
---
- name: Write the marker
  hosts: all
  connection: local
  gather_facts: false
  tasks:
    - name: Place the marker
      ansible.builtin.copy:
        content: "release {{ version | default('none') }}\\n"
        dest: "{{ playbook_dir }}/../marker.txt"
        mode: "0600"
"""


def ansible_repository(tmp_path: Path, **operations: str) -> Path:
    """Write the repository under ``tmp_path`` and return its directory."""
    repository = tmp_path / "fleet-ansible"
    inventory = repository / "inventories" / "production"
    (inventory / "group_vars" / "all").mkdir(parents=True)
    (inventory / "host_vars").mkdir(parents=True)
    (inventory / "hosts.yml").write_text(HOSTS, encoding="utf-8")
    (inventory / "group_vars" / "all" / "main.yml").write_text(
        GROUP_VARS, encoding="utf-8"
    )
    (inventory / "host_vars" / "web-1.yml").write_text(WEB_1, encoding="utf-8")
    (repository / "ansible.cfg").write_text(
        textwrap.dedent("""\
            [defaults]
            inventory = inventories/production
            retry_files_enabled = False
            interpreter_python = auto_silent
            """),
        encoding="utf-8",
    )
    (repository / "playbooks").mkdir()
    (repository / "playbooks" / "marker.yml").write_text(PLAYBOOK, encoding="utf-8")
    declared = operations or {"write-marker": MARKER}
    (repository / "operations").mkdir()
    for name, body in declared.items():
        (repository / "operations" / f"{name}.yml").write_text(body, encoding="utf-8")
    return repository
