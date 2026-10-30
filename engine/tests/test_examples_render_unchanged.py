"""The example project renders byte-identical to the committed golden files.

The golden files under ``golden/examples`` were captured from main before
S-006 changed the PostgreSQL role, so any drift in what a project without an
owned database renders shows up here as a diff.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cloudfall.ansible_data import UNSAFE_KEY
from cloudfall_engine.cli import main
from jinja2 import Environment, FileSystemLoader, StrictUndefined

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ROLES = ROOT / "engine" / "ansible" / "roles"
GOLDEN = Path(__file__).parent / "golden" / "examples"
POSTGRESQL_TEMPLATES = {
    "cloudfall.conf": "cloudfall.conf.j2",
    "backup.sh": "backup.sh.j2",
    "restore-check.sh": "restore-check.sh.j2",
}


def _environment(role: str) -> Environment:
    """Jinja with the whitespace rules Ansible's template module applies."""
    return Environment(
        loader=FileSystemLoader(ROLES / role / "templates"),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )


def _unsafe_as_text(value: dict[str, object]) -> object:
    if list(value) == [UNSAFE_KEY]:
        return value[UNSAFE_KEY]
    return value


def render_examples(output: Path) -> dict[str, str]:
    """Render the example inventory and the files S-006-12 names, by name."""
    inventory_path = output / "ansible-inventory.json"
    exit_code = main(
        [
            "inventory",
            "render",
            "--project",
            str(EXAMPLES),
            "--schemas",
            str(SCHEMAS),
            "--output",
            str(inventory_path),
        ]
    )
    assert exit_code == 0
    inventory_text = inventory_path.read_text(encoding="utf-8")
    rendered = {"ansible-inventory.json": inventory_text}
    # Read the values back as Ansible's loader does: an unsafe string is text.
    document = cast(
        "dict[str, dict[str, object]]",
        json.loads(inventory_text, object_hook=_unsafe_as_text),
    )
    servers = cast(
        "dict[str, dict[str, dict[str, dict[str, object]]]]",
        document["all"]["children"],
    )["cloudfall_servers"]["hosts"]
    firewall = _environment("cloudfall_firewall").get_template("nftables.conf.j2")
    postgresql = _environment("cloudfall_postgresql")
    for host, variables in sorted(servers.items()):
        if "cloudfall_firewall" in variables:
            rendered[f"{host}/nftables.conf"] = firewall.render(
                cloudfall_firewall_ruleset=variables["cloudfall_firewall"]
            )
        services = cast("list[dict[str, object]]", variables["cloudfall_services"])
        for service in services:
            if service["serviceKind"] != "postgresql":
                continue
            for name, template in POSTGRESQL_TEMPLATES.items():
                rendered[f"{host}/{service['id']}/{name}"] = postgresql.get_template(
                    template
                ).render(cloudfall_postgresql_service=service)
    return rendered


def _golden() -> dict[str, str]:
    return {
        path.relative_to(GOLDEN).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(GOLDEN.rglob("*"))
        if path.is_file()
    }


# proves: S-006-12
def test_s006_12_examples_render_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rendered = render_examples(tmp_path)
    capsys.readouterr()

    golden = _golden()
    assert sorted(rendered) == sorted(golden)
    assert {"h1/nftables.conf", "h1/postgresql-main/cloudfall.conf"} <= set(golden)
    for name, text in rendered.items():
        assert text == golden[name], name
