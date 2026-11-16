"""The rendered inventory carries a MariaDB service (S-019, #419)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cloudfall.ansible_data import unmarked
from cloudfall_engine.cli import main
from mariadb_project import (
    BILLING_PASSWORD_REF,
    EXAMPLES,
    SCHEMAS,
    mariadb_service,
    primary_replication,
    replica_service,
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


def _hosts(text: str) -> dict[str, dict[str, object]]:
    document = cast(
        "dict[str, dict[str, object]]", json.loads(text, object_hook=unmarked)
    )
    return cast(
        "dict[str, dict[str, dict[str, dict[str, object]]]]",
        document["all"]["children"],
    )["cloudfall_servers"]["hosts"]


def test_s019_1_render_carries_the_service_with_its_databases_and_no_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", mariadb_service())
    secret = "never-rendered-billing-password"  # noqa: S105 - a test value
    secrets = project / "secrets" / "production" / "services"
    secrets.mkdir(parents=True)
    (secrets / "mariadb-main.env").write_text(
        f"BILLING_PASSWORD={secret}\n", encoding="utf-8"
    )

    text = _render(project, tmp_path / "inventory.json")
    capsys.readouterr()

    services = _hosts(text)["h1"]["cloudfall_services"]
    (service,) = cast("list[dict[str, object]]", services)
    assert service["serviceKind"] == "mariadb"
    assert service["bind"] == {"address": "127.0.0.1", "port": 3306}
    assert service["mariadb"] == {
        "version": "11.8.9",
        "innodbBufferPoolMb": 4096,
        "databases": [
            {"name": "crm", "application": "crm", "owner": "crm"},
            {
                "name": "billing",
                "owner": "billing",
                "passwordRef": BILLING_PASSWORD_REF,
                "authentication": "mysql_native_password",
            },
        ],
    }
    assert secret not in text


def test_s019_1_render_places_the_mariadb_volume_on_its_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(tmp_path / "project", mariadb_service())

    hosts = _hosts(_render(project, tmp_path / "inventory.json"))
    capsys.readouterr()

    volumes = cast("list[dict[str, object]]", hosts["h1"]["cloudfall_volumes"])
    assert {"name": "mariadb", "path": "/var/lib/mysql"}.items() <= volumes[0].items()


def test_s019_1_a_pair_renders_each_service_on_its_own_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_project(
        tmp_path / "project",
        mariadb_service(replication=primary_replication()),
        replica_service(),
        servers=("h1", "h2"),
    )

    hosts = _hosts(_render(project, tmp_path / "inventory.json"))
    capsys.readouterr()

    (primary,) = cast("list[dict[str, object]]", hosts["h1"]["cloudfall_services"])
    (replica,) = cast("list[dict[str, object]]", hosts["h2"]["cloudfall_services"])
    assert primary["id"] == "mariadb-main"
    assert replica["id"] == "mariadb-replica"
    replica_block = cast("dict[str, object]", replica["mariadb"])
    assert replica_block["replication"] == {
        "role": "replica",
        "serverId": 2,
        "primary": "mariadb-main",
    }


# proves: S-019-2
def test_s019_2_examples_without_mariadb_render_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rendered = _render(EXAMPLES, tmp_path / "ansible-inventory.json")
    capsys.readouterr()

    assert rendered == GOLDEN.read_text(encoding="utf-8")
    assert "mariadb" not in rendered
