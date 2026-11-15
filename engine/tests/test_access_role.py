"""Root's authorized_keys gets the active keys and loses the revoked ones (#354).

The role's key tasks run for real on localhost against a scratch
authorized_keys; a key the project never declared stays (D-22).
"""

from __future__ import annotations

import base64
import shutil
import struct
import subprocess
from pathlib import Path

import yaml
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from examples_project import copy_examples

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ACCESS = ROOT / "engine" / "ansible" / "roles" / "cloudfall_access"
KEY_TASKS = (
    "Select active declared SSH keys",
    "Require at least one active declared SSH key",
    "Refuse a key declared both active and revoked",
    "Install active root SSH keys",
    "Remove revoked root SSH keys",
)


def _ed25519(fill: int) -> str:
    """Return a well-formed ed25519 public key, without a comment."""
    algorithm = b"ssh-ed25519"
    wire = (
        struct.pack(">I", len(algorithm))
        + algorithm
        + struct.pack(">I", 32)
        + bytes([fill]) * 32
    )
    return f"ssh-ed25519 {base64.b64encode(wire).decode('ascii')}"


ACTIVE = _ed25519(0x01)
# Its body holds "+" and "/", which a regular expression must escape.
REVOKED = _ed25519(0xFB)
UNDECLARED = _ed25519(0x03)


def _key(key_id: str, public_key: str, lifecycle: str) -> dict[str, str]:
    return {
        "id": key_id,
        "owner": key_id,
        "environment": "production",
        "algorithm": "ssh-ed25519",
        "publicKey": public_key,
        "lifecycle": lifecycle,
    }


def _run(
    tmp_path: Path, keys: list[dict[str, str]]
) -> subprocess.CompletedProcess[str]:
    """Replay the role's key tasks with root's file swapped for a scratch one."""
    authorized_keys = tmp_path / "authorized_keys"
    tasks = yaml.safe_load((ACCESS / "tasks" / "main.yml").read_text(encoding="utf-8"))
    replay = [task for task in tasks if task["name"] in KEY_TASKS]
    assert [task["name"] for task in replay] == list(KEY_TASKS)
    for task in replay:
        module = task.get("ansible.builtin.lineinfile")
        if module is not None:
            module["path"] = str(authorized_keys)
            module.pop("owner", None)
            module.pop("group", None)
    play = [
        {
            "name": "Replay the key tasks of cloudfall_access",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {"cloudfall_access_ssh_public_keys": keys},
            "tasks": replay,
        }
    ]
    playbook = tmp_path / "access.yml"
    playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
    binary = shutil.which("ansible-playbook")
    assert binary is not None
    return subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [binary, "--inventory", "localhost,", str(playbook)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_a_revoked_key_is_removed_and_the_others_stay(tmp_path: Path) -> None:
    authorized_keys = tmp_path / "authorized_keys"
    authorized_keys.write_text(
        f"{UNDECLARED} rescue\n"
        f"{REVOKED} laptop-old-comment\n"
        f'from="10.0.0.0/8" {REVOKED}\n'
        f"{ACTIVE} ci\n",
        encoding="utf-8",
    )

    done = _run(
        tmp_path,
        [_key("ci", f"{ACTIVE} ci", "active"), _key("laptop", REVOKED, "revoked")],
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert authorized_keys.read_text(encoding="utf-8").splitlines() == [
        f"{UNDECLARED} rescue",
        f"{ACTIVE} ci",
    ]


def test_a_key_declared_active_and_revoked_stops_before_any_change(
    tmp_path: Path,
) -> None:
    authorized_keys = tmp_path / "authorized_keys"
    authorized_keys.write_text(f"{UNDECLARED} rescue\n", encoding="utf-8")

    done = _run(
        tmp_path,
        [
            _key("ci", f"{ACTIVE} ci", "active"),
            _key("ci-old", f"{ACTIVE} another-comment", "revoked"),
        ],
    )

    assert done.returncode != 0
    assert "declared both active and revoked" in done.stdout
    assert authorized_keys.read_text(encoding="utf-8") == f"{UNDECLARED} rescue\n"


def test_the_inventory_carries_revoked_keys_to_the_role(tmp_path: Path) -> None:
    project = tmp_path / "config"
    copy_examples(project)
    document = {
        "apiVersion": "cloudfall/v1",
        "kind": "SshPublicKey",
        "metadata": {"id": "example-old"},
        "spec": {
            "owner": "example-old",
            "environment": "production",
            "publicKey": f"{REVOKED} old",
            "lifecycle": "revoked",
        },
    }
    (project / "ssh-public-keys" / "example-old.yaml").write_text(
        yaml.safe_dump(document), encoding="utf-8"
    )

    rendered = render_ansible_inventory(
        PlatformInventory.from_state(validate_config(project, SCHEMAS))
    )

    children = rendered["all"]["children"]  # type: ignore[index]
    h1 = children["cloudfall_servers"]["hosts"]["h1"]
    keys = {key["id"]: key["lifecycle"] for key in h1["cloudfall_ssh_public_keys"]}
    assert keys == {"example-admin": "active", "example-old": "revoked"}
