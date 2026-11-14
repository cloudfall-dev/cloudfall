"""S-016: inspect hashes the backend's dashboards, and audit checks them."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from cloudfall.audit import AuditCheck, audit_inventory
from cloudfall.dashboards import bundled_dashboards
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.validation import validate_config

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
INSPECT = ROOT / "engine" / "ansible" / "roles" / "cloudfall_inspect"
BACKEND = ROOT / "engine" / "ansible" / "roles" / "cloudfall_logging_backend"


def _bundled() -> dict[str, str]:
    return bundled_dashboards(ROOT / "engine")


def _check(
    tmp_path: Path, files: list[dict[str, object]] | None, server: str = "h1"
) -> AuditCheck | None:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    h1 = observations / "h1.json"
    document = json.loads(h1.read_text(encoding="utf-8"))
    if files is None:
        del document["spec"]["loggingDashboards"]
    else:
        document["spec"]["loggingDashboards"]["files"] = files
    h1.write_text(json.dumps(document), encoding="utf-8")
    inventory = PlatformInventory.from_state(validate_config(EXAMPLES, SCHEMAS))
    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))
    audited = next(item for item in report.servers if item.server_id == server)
    return next(
        (item for item in audited.checks if item.check == "logging.dashboards"), None
    )


def _files(**changes: str | None) -> list[dict[str, object]]:
    found: dict[str, str | None] = {**_bundled()}
    for name, sha256 in changes.items():
        if sha256 is None:
            del found[name.replace("_", "-") + ".json"]
        else:
            found[name.replace("_", "-") + ".json"] = sha256
    return [{"name": name, "sha256": value} for name, value in sorted(found.items())]


def test_the_compliant_fixture_carries_this_release_s_dashboards() -> None:
    document = json.loads((COMPLIANT / "h1.json").read_text(encoding="utf-8"))
    observed = {
        item["name"]: item["sha256"]
        for item in document["spec"]["loggingDashboards"]["files"]
    }

    # A changed dashboard changes its hash: refresh the fixture with it
    assert observed == _bundled()


def test_s016_6_the_bundled_dashboards_audit_compliant(tmp_path: Path) -> None:
    check = _check(tmp_path, _files())

    assert check is not None
    assert check.status.value == "compliant"


@pytest.mark.parametrize(
    ("files", "named"),
    [
        (_files(cloudfall_logs=None), "missing: cloudfall-logs.json"),
        (_files(cloudfall_fleet="0" * 64), "changed: cloudfall-fleet.json"),
        (_files(cloudfall_extra="1" * 64), "extra: cloudfall-extra.json"),
    ],
)
def test_s016_6_a_removed_edited_or_extra_file_is_drift(
    tmp_path: Path, files: list[dict[str, object]], named: str
) -> None:
    check = _check(tmp_path, files)

    assert check is not None
    assert check.status.value == "drift"
    assert named in check.message


def test_no_dashboard_evidence_is_unknown(tmp_path: Path) -> None:
    check = _check(tmp_path, None)

    assert check is not None
    assert check.status.value == "unknown"


def test_a_server_that_is_no_backend_has_no_dashboard_check(tmp_path: Path) -> None:
    assert _check(tmp_path, _files(), server="h2") is None


def test_inspect_looks_where_the_backend_role_writes() -> None:
    inspect = yaml.safe_load(
        (INSPECT / "defaults" / "main.yml").read_text(encoding="utf-8")
    )
    backend = yaml.safe_load(
        (BACKEND / "defaults" / "main.yml").read_text(encoding="utf-8")
    )

    assert (
        inspect["cloudfall_inspect_grafana_dashboards"]
        == backend["cloudfall_logging_backend_dashboards_path"]
    )


def _inspect(tmp_path: Path, directory: Path) -> dict[str, object]:
    output = tmp_path / "out.json"
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "cloudfall_inspect_snapshot": {"spec": {"server": "h1"}},
                        "cloudfall_inspect_grafana_dashboards": str(directory),
                    },
                    "tasks": [
                        {
                            "ansible.builtin.include_tasks": str(
                                INSPECT / "tasks" / "dashboards.yml"
                            )
                        },
                        {
                            "ansible.builtin.copy": {
                                "content": "{{ cloudfall_inspect_snapshot | to_json }}",
                                "dest": str(output),
                            }
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(  # noqa: S603 - the venv's ansible-playbook
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
        check=True,
        timeout=300,
    )
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    return snapshot["spec"]["loggingDashboards"]


def test_s016_6_inspect_hashes_each_dashboard_file(tmp_path: Path) -> None:
    directory = tmp_path / "dashboards"
    directory.mkdir()
    (directory / "b.json").write_text('{"uid": "b"}', encoding="utf-8")
    (directory / "a.json").write_text('{"uid": "a"}', encoding="utf-8")

    evidence = _inspect(tmp_path, directory)

    assert evidence == {
        "directory": str(directory),
        "present": True,
        "files": [
            {
                "name": name,
                "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(),
            }
            for name in ("a.json", "b.json")
        ],
    }


def test_inspect_on_a_backend_without_the_directory_says_so(tmp_path: Path) -> None:
    evidence = _inspect(tmp_path, tmp_path / "none")

    assert evidence["present"] is False
    assert evidence["files"] == []
