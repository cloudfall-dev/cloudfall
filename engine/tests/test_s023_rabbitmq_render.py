"""The rendered inventory carries a RabbitMQ service (S-023, #505)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cloudfall.ansible_data import unmarked
from cloudfall_engine.cli import main
from rabbitmq_project import (
    CLUSTER,
    EXAMPLES,
    JOBS_PASSWORD_REF,
    SCHEMAS,
    rabbitmq_service,
    write_project,
)

if TYPE_CHECKING:
    import pytest

# test_examples_render_unchanged pins the same file.
GOLDEN = Path(__file__).parent / "golden" / "examples" / "ansible-inventory.json"


def _render(project: Path, output: Path) -> str:
    exit_code = main(
        [
            "inventory",
            "render",
            "--project",
            str(project),
            "--schemas",
            str(SCHEMAS),
            "--output",
            str(output),
        ]
    )
    assert exit_code == 0
    return output.read_text(encoding="utf-8")


def _services_by_host(text: str) -> dict[str, list[dict[str, object]]]:
    document = cast(
        "dict[str, dict[str, object]]", json.loads(text, object_hook=unmarked)
    )
    hosts = cast(
        "dict[str, dict[str, dict[str, dict[str, object]]]]",
        document["all"]["children"],
    )["cloudfall_servers"]["hosts"]
    return {
        host: cast("list[dict[str, object]]", variables["cloudfall_services"])
        for host, variables in hosts.items()
    }


def test_s023_1_render_carries_three_nodes_the_vhost_and_no_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", rabbitmq_service())
    secret = "never-rendered-jobs-password"  # noqa: S105 - a test value
    secrets = project / "secrets" / "production" / "services"
    secrets.mkdir(parents=True)
    (secrets / "rabbitmq-main.env").write_text(
        f"JOBS_PASSWORD={secret}\n", encoding="utf-8"
    )

    text = _render(project, tmp_path / "inventory.json")
    capsys.readouterr()

    services = _services_by_host(text)
    (service,) = services["h1"]
    assert service["serviceKind"] == "rabbitmq"
    assert service["bind"] == {"address": "127.0.0.1", "port": 5672}
    assert service["rabbitmq"] == {
        "version": "4.4.1",
        "memoryHighWatermarkMb": 2048,
        "nodes": list(CLUSTER),
        "vhosts": [{"name": "jobs", "owner": "jobs", "passwordRef": JOBS_PASSWORD_REF}],
        "clientAccess": {"allowedSources": ["10.0.0.0/24"]},
    }
    # The service's server is its home; each node converges it too
    assert services["h2"] == [service]
    assert services["h3"] == [service]
    assert secret not in text


# proves: S-023-2
def test_s023_2_examples_without_rabbitmq_render_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rendered = _render(EXAMPLES, tmp_path / "ansible-inventory.json")
    capsys.readouterr()

    assert rendered == GOLDEN.read_text(encoding="utf-8")
    assert "rabbitmq" not in rendered
