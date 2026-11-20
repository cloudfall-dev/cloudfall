"""The rendered inventory carries a ClickHouse service (S-022, #566)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from clickhouse_project import (
    EVENTS_PASSWORD_REF,
    EXAMPLES,
    REPLICAS,
    SCHEMAS,
    VOTERS,
    clickhouse_service,
    write_project,
)
from cloudfall.ansible_data import unmarked
from cloudfall_engine.cli import main

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


def test_s022_1_render_carries_replicas_voters_the_database_and_no_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", clickhouse_service())
    secret = "never-rendered-events-password"  # noqa: S105 - a test value
    secrets = project / "secrets" / "production" / "services"
    secrets.mkdir(parents=True)
    (secrets / "clickhouse-main.env").write_text(
        f"EVENTS_PASSWORD={secret}\n", encoding="utf-8"
    )

    text = _render(project, tmp_path / "inventory.json")
    capsys.readouterr()

    services = _services_by_host(text)
    (service,) = services["h1"]
    assert service["serviceKind"] == "clickhouse"
    assert service["bind"] == {"address": "127.0.0.1", "port": 9000}
    assert service["clickhouse"] == {
        "version": "26.8.37.168",
        "maxServerMemoryMb": 16384,
        "replicas": list(REPLICAS),
        "keeper": {"servers": list(VOTERS)},
        "databases": [
            {"name": "events", "owner": "events", "passwordRef": EVENTS_PASSWORD_REF}
        ],
        "clientAccess": {"allowedSources": ["10.0.0.0/24"]},
    }
    # The service's server is its home; the other replica and the third
    # voter converge it too
    assert services["h2"] == [service]
    assert services["h3"] == [service]
    assert secret not in text


# proves: S-022-2
def test_s022_2_examples_without_clickhouse_render_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rendered = _render(EXAMPLES, tmp_path / "ansible-inventory.json")
    capsys.readouterr()

    assert rendered == GOLDEN.read_text(encoding="utf-8")
    assert "clickhouse" not in rendered
