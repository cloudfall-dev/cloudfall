"""The rendered inventory carries an Elasticsearch service (S-012, #309)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cloudfall.ansible_data import unmarked
from cloudfall_engine.cli import main
from elasticsearch_project import (
    EXAMPLES,
    SCHEMAS,
    SNAPSHOT_REPOSITORY,
    elasticsearch_service,
    write_project,
)

if TYPE_CHECKING:
    import pytest

# Captured before S-012; test_examples_render_unchanged pins the same file.
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


def test_s012_1_render_carries_the_service_with_its_one_node(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", elasticsearch_service())

    services = _services_by_host(_render(project, tmp_path / "inventory.json"))
    capsys.readouterr()

    (service,) = services["h1"]
    assert service["serviceKind"] == "elasticsearch"
    assert service["elasticsearch"] == {
        "version": "9.2.4",
        "heapMb": 4096,
        "nodes": ["h1"],
        "clusterName": "search-main",
        "users": [],
    }
    assert service["bind"] == {"address": "127.0.0.1", "port": 9200}
    assert service["metrics"] == {"enabled": True}


def test_s012_1_a_cluster_renders_on_its_home_server_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The service's server is its home; the other nodes are read from ``nodes``."""
    project = write_project(
        tmp_path / "project",
        elasticsearch_service(
            nodes=["h1", "h2"], snapshotRepository=SNAPSHOT_REPOSITORY
        ),
        servers=("h1", "h2"),
    )

    services = _services_by_host(_render(project, tmp_path / "inventory.json"))
    capsys.readouterr()

    (service,) = services["h1"]
    elasticsearch = cast("dict[str, object]", service["elasticsearch"])
    assert elasticsearch["nodes"] == ["h1", "h2"]
    assert services["h2"] == []


# proves: S-012-2
def test_s012_2_examples_without_elasticsearch_render_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rendered = _render(EXAMPLES, tmp_path / "ansible-inventory.json")
    capsys.readouterr()

    golden = GOLDEN.read_text(encoding="utf-8")
    assert rendered == golden
    assert "elasticsearch" not in rendered
