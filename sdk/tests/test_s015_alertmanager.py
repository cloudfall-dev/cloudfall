"""S-015: a LoggingStack receiver on an AlertChannel, via Alertmanager."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from cloudfall.inventory import PlatformInventory
from cloudfall.logging_deploy import InputMissingError, channel_token_files
from cloudfall.requirements import SecretRequirement, requirements_of
from cloudfall.secret_store import SecretStore
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from examples_project import copy_examples
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_logging_backend"
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
TOKEN_FILE = "/etc/cloudfall/logging/alert-channel-operations-bot-token"  # noqa: S105 - a path
CHANNEL = """---
apiVersion: cloudfall/v1
kind: AlertChannel
metadata:
  id: operations
  description: The operators' chat
spec:
  telegram:
    chatId: "-100123"
"""


def _project(tmp_path: Path) -> Path:
    project = copy_examples(tmp_path / "config")
    (project / "alert-channels").mkdir()
    (project / "alert-channels" / "operations.yaml").write_text(
        CHANNEL, encoding="utf-8"
    )
    stack = project / "logging-stacks" / "operations.yaml"
    text = stack.read_text(encoding="utf-8")
    old = "    receivers:\n"
    assert old in text
    stack.write_text(
        text.replace(old, old + "      - id: chat\n        channel: operations\n"),
        encoding="utf-8",
    )
    return project


def _inventory(project: Path) -> PlatformInventory:
    return PlatformInventory.from_state(validate_config(project, SCHEMAS))


def _hostvars(project: Path) -> dict[str, dict[str, object]]:
    rendered = render_ansible_inventory(_inventory(project))
    hosts = rendered["all"]["children"]["cloudfall_servers"]["hosts"]  # type: ignore[index]
    assert isinstance(hosts, dict)
    return hosts


def test_s015_5_the_backend_renders_the_channel_for_alertmanager(
    tmp_path: Path,
) -> None:
    hosts = _hostvars(_project(tmp_path))

    assert hosts["h1"]["cloudfall_logging_alert_channels"] == [
        {
            "receiver": "chat",
            "channel": "operations",
            "chatId": "-100123",
            "botTokenFile": TOKEN_FILE,
        }
    ]
    assert "cloudfall_logging_alert_channels" not in hosts["h2"]


def test_s015_5_alertmanager_gets_a_native_telegram_config(tmp_path: Path) -> None:
    hosts = _hostvars(_project(tmp_path))
    environment = Environment(  # noqa: S701 - renders YAML, not HTML
        loader=FileSystemLoader(ROLE / "templates"),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )

    text = environment.get_template("alertmanager.yaml.j2").render(
        cloudfall_logging_backend_stack=hosts["h1"]["cloudfall_logging_backend"],
        cloudfall_logging_alert_channels=hosts["h1"][
            "cloudfall_logging_alert_channels"
        ],
    )

    receiver = yaml.safe_load(text)["receivers"][0]
    assert receiver["telegram_configs"] == [
        {"bot_token_file": TOKEN_FILE, "chat_id": -100123}
    ]
    assert receiver["webhook_configs"]
    assert receiver["email_configs"]


def _decrypt(source: Path) -> str:
    return "".join(
        f"{line}\n"
        for line in source.read_text(encoding="utf-8").splitlines()
        if not line.startswith("sops_")
    )


def _encrypt(_target: Path, text: str) -> str:
    return text + "sops_mac=fake\n"


def test_s015_5_the_token_is_staged_for_alertmanager_or_the_deploy_stops(
    tmp_path: Path,
) -> None:
    inventory = _inventory(_project(tmp_path))
    store = SecretStore(tmp_path / "secrets", decrypt=_decrypt, encrypt=_encrypt)

    with pytest.raises(InputMissingError):
        channel_token_files(inventory, store)

    channel = next(
        requirement.spec
        for requirement in requirements_of(inventory)[0].requirements
        if isinstance(requirement.spec, SecretRequirement)
        and requirement.spec.name.value == "telegram-bot-token"
    )
    store.write(channel.reference, "123456:synthetic")
    files = channel_token_files(inventory, store)

    assert [(item.path, item.mode, item.group, item.force) for item in files] == [
        (TOKEN_FILE, "0640", "prometheus", True)
    ]


@pytest.mark.parametrize(
    ("candidate", "pinned", "passes"),
    [
        ("0.28.1+ds-1", None, True),
        ("1:0.26.0-1", None, True),
        ("0.25.0-3", None, False),
        ("0.25.0-3", "0.27.0-2", True),
        ("0.31.0", "0.24.0-1", False),
    ],
)
def test_s015_5_alertmanager_older_than_0_26_is_refused(
    tmp_path: Path,
    candidate: str,
    pinned: str | None,
    passes: bool,  # noqa: FBT001 - a parametrized expectation
) -> None:
    tasks = (ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
    start = tasks.index("- name: Require Alertmanager 0.26 or later")
    end = tasks.index("\n\n", start)
    check = yaml.safe_load(tasks[start:end])[0]
    del check["when"]
    alertmanager = {"listenAddress": "127.0.0.1", "port": 9093}
    if pinned is not None:
        alertmanager["packageVersion"] = pinned
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "cloudfall_logging_backend_stack": {
                            "alerting": {"alertmanager": alertmanager}
                        },
                        "cloudfall_logging_backend_alertmanager_policy": {
                            "stdout": (
                                "prometheus-alertmanager:\n"
                                "  Installed: (none)\n"
                                f"  Candidate: {candidate}\n"
                            )
                        },
                    },
                    "tasks": [check],
                }
            ]
        ),
        encoding="utf-8",
    )

    done = subprocess.run(  # noqa: S603 - the venv's ansible-playbook
        [
            str(ANSIBLE_PLAYBOOK),
            "--inventory",
            "localhost,",
            "--connection",
            "local",
            str(playbook),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert (done.returncode == 0) is passes, done.stdout
    if not passes:
        assert "needs Alertmanager 0.26 or later" in done.stdout
