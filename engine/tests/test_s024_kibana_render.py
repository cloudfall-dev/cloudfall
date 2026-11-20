"""The rendered inventory carries a Kibana service (S-024, #567)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cloudfall.ansible_data import unmarked
from cloudfall_engine.cli import main
from kibana_project import (
    EXAMPLES,
    SCHEMAS,
    kibana_service,
    search_main,
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


def test_s024_1_render_carries_kibana_the_cluster_and_analyst(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", search_main(), kibana_service())
    secret = "never-rendered-analyst-password"  # noqa: S105 - a test value
    secrets = project / "secrets" / "production" / "services"
    secrets.mkdir(parents=True)
    (secrets / "search-main.env").write_text(
        f"ANALYST_PASSWORD={secret}\n", encoding="utf-8"
    )

    text = _render(project, tmp_path / "inventory.json")
    capsys.readouterr()

    services = {
        cast("str", service["id"]): service
        for service in _services_by_host(text)["h1"]
    }
    assert sorted(services) == ["search-kibana", "search-main"]
    kibana = services["search-kibana"]
    assert kibana["serviceKind"] == "kibana"
    assert kibana["bind"] == {"address": "127.0.0.1", "port": 5601}
    assert kibana["kibana"] == {
        "elasticsearch": "search-main",
        "heapMb": 1024,
        "users": [{"name": "analyst", "access": "edit"}],
    }
    assert "backup" not in kibana
    # The Elasticsearch service Kibana reads, with analyst and its own role
    search = services["search-main"]
    assert search["serviceKind"] == "elasticsearch"
    users = cast("dict[str, list[dict[str, object]]]", search["elasticsearch"])
    assert [user["name"] for user in users["users"]] == ["analyst"]
    assert secret not in text


# proves: S-024-2
def test_s024_2_examples_without_kibana_render_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rendered = _render(EXAMPLES, tmp_path / "ansible-inventory.json")
    capsys.readouterr()

    assert rendered == GOLDEN.read_text(encoding="utf-8")
    assert "kibana" not in rendered
