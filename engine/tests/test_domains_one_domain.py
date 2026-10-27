"""``domains.yml`` for one Domain, and the hash of the vhost a run replaces.

The role needs a Debian host with Nginx, so the loop is evaluated with
Ansible's own templating, and the receipt tasks run for real on localhost
against a vhost file under ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.validation import SchemaCatalog

ROOT = Path(__file__).parents[2]
ANSIBLE = ROOT / "engine" / "ansible"
TASKS = ANSIBLE / "roles" / "cloudfall_nginx_site" / "tasks"
SCHEMAS = ROOT / "config" / "schemas" / "v1"
RECEIPT_TASKS = (
    "Hash the virtual host this run replaces",
    "Hash the applied configuration for the receipt",
    "Ensure the controller receipt directory exists",
    "Write the deployment receipt on the controller",
)
RENDERED = "server { listen 80; }\n"
RENDER_PLAY = "Deploy declared Cloudfall public domain routes"


def _loop(variables: dict[str, object]) -> object:
    plays = DataLoader().load_from_file(str(ANSIBLE / "playbooks" / "domains.yml"))
    assert isinstance(plays, list)
    (render,) = (play for play in plays if play["name"] == RENDER_PLAY)
    (task,) = render["tasks"]
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(task["loop"]))


def _domain(domain_id: str) -> dict[str, object]:
    return {
        "id": domain_id,
        "proxy": {"configurationPath": f"/etc/nginx/sites-available/{domain_id}.conf"},
    }


def test_s004_2_one_domain_id_renders_only_that_domain() -> None:
    domains = [_domain("d1"), _domain("d2")]

    only_d1 = _loop({"cloudfall_domains": domains, "cloudfall_domains_domain_id": "d1"})

    assert only_d1 == [_domain("d1")]


def test_s004_17_without_a_domain_id_every_domain_on_the_host_renders() -> None:
    domains = [_domain("d1"), _domain("d2")]

    assert _loop({"cloudfall_domains": domains}) == domains
    assert _loop({}) == []


def test_s004_2_a_host_without_the_domain_renders_nothing() -> None:
    variables: dict[str, object] = {
        "cloudfall_domains": [_domain("d2")],
        "cloudfall_domains_domain_id": "d1",
    }

    assert _loop(variables) == []


def _receipt_play(vhost: Path, receipts: Path) -> list[dict[str, object]]:
    """Replay the role's receipt tasks, with a stand-in for the render."""
    # Plain YAML, not Ansible's loader: the tasks are written out again.
    tasks = yaml.safe_load((TASKS / "main.yml").read_text(encoding="utf-8"))
    assert isinstance(tasks, list)
    named = {task["name"]: task for task in tasks}
    render = {
        "name": "Stand in for the render",
        "ansible.builtin.copy": {
            "content": RENDERED,
            "dest": str(vhost),
            "mode": "0644",
        },
    }
    hash_replaced, *receipt = (named[name] for name in RECEIPT_TASKS)
    return [
        {
            "name": "Replay the receipt tasks of cloudfall_nginx_site",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": True,
            "gather_subset": ["min"],
            "vars": {
                "cloudfall_server_id": "h2",
                "cloudfall_nginx_site_receipt_directory": str(receipts),
                "cloudfall_nginx_site_domain": {
                    "id": "crm-site",
                    "proxy": {"configurationPath": str(vhost)},
                },
            },
            "tasks": [hash_replaced, render, *receipt],
        }
    ]


def _run(tmp_path: Path, vhost: Path) -> dict[str, object]:
    binary = shutil.which("ansible-playbook")
    assert binary is not None
    receipts = tmp_path / "deployments"
    playbook = tmp_path / "receipt.yml"
    play = _receipt_play(vhost, receipts)
    playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
    done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [binary, "--inventory", "localhost,", str(playbook)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    receipt = json.loads((receipts / "crm-site.json").read_text(encoding="utf-8"))
    SchemaCatalog(SCHEMAS).validate_named("deployment-receipt.schema.json", receipt)
    spec = receipt["spec"]
    assert isinstance(spec, dict)
    return spec


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_s004_11_the_receipt_keeps_the_hash_of_a_hand_written_vhost(
    tmp_path: Path,
) -> None:
    vhost = tmp_path / "crm.example.test.conf"
    hand_written = "server { listen 80; server_name crm.example.test; }\n"
    vhost.write_text(hand_written, encoding="utf-8")

    spec = _run(tmp_path, vhost)

    assert spec["previousConfigurationSha256"] == _sha256(hand_written)
    assert spec["configurationSha256"] == _sha256(RENDERED)


def test_s004_11_a_run_with_no_vhost_there_records_null(tmp_path: Path) -> None:
    spec = _run(tmp_path, tmp_path / "crm.example.test.conf")

    assert spec["previousConfigurationSha256"] is None
    assert spec["configurationSha256"] == _sha256(RENDERED)


def test_s004_11_a_vhost_linked_into_place_is_hashed_through_the_link(
    tmp_path: Path,
) -> None:
    """Debian keeps vhosts as links into sites-available; hash what it reads."""
    hand_written = "server { listen 80; server_name crm.example.test; }\n"
    target = tmp_path / "sites-available.conf"
    target.write_text(hand_written, encoding="utf-8")
    vhost = tmp_path / "crm.example.test.conf"
    vhost.symlink_to(target)

    spec = _run(tmp_path, vhost)

    assert spec["previousConfigurationSha256"] == _sha256(hand_written)


def test_s004_11_the_replaced_vhost_is_hashed_before_the_first_render() -> None:
    """The second render would see only this run's own file."""
    tasks = DataLoader().load_from_file(str(TASKS / "main.yml"))
    assert isinstance(tasks, list)
    names = [task["name"] for task in tasks]

    assert names.index(RECEIPT_TASKS[0]) < names.index(
        "Apply the virtual host before issuance"
    )
