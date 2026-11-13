"""S-011-7: audit reports drift when what a requirement provided goes away."""

from __future__ import annotations

import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

from cloudfall.audit import AuditCheck, AuditStatus, audit_inventory
from cloudfall.inventory import PlatformInventory
from cloudfall.observation import load_observations
from cloudfall.validation import validate_config
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"
COMPLIANT = ROOT / "config" / "tests" / "observed" / "compliant"
OBSERVED_AT = datetime.datetime(2026, 7, 15, 10, 0, tzinfo=datetime.UTC)


def _observations(tmp_path: Path) -> Path:
    observations = tmp_path / "observed"
    shutil.copytree(COMPLIANT, observations)
    return observations


def _edit(observations: Path, host: str) -> tuple[Path, dict[str, object]]:
    path = observations / f"{host}.json"
    return path, json.loads(path.read_text(encoding="utf-8"))


def _check(observations: Path, host: str, name: str) -> AuditCheck:
    inventory = PlatformInventory.from_state(validate_config(EXAMPLES, SCHEMAS))
    report = audit_inventory(inventory, load_observations(observations, SCHEMAS))
    server = next(item for item in report.servers if item.server_id == host)
    return next(check for check in server.checks if check.check == name)


def _certificate(name: str, not_after: datetime.datetime) -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(OBSERVED_AT - datetime.timedelta(days=60))
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False
        )
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode()
    )


def _replace_certificate(
    observations: Path, host: str, name: str, certificate: str | None
) -> None:
    path, document = _edit(observations, host)
    spec = document["spec"]
    assert isinstance(spec, dict)
    for item in spec["certificates"]:
        if item["name"] == name:
            item["certificate"] = certificate
            item["exists"] = certificate is not None
    path.write_text(json.dumps(document), encoding="utf-8")


def test_the_compliant_fixture_passes_every_requirement_check() -> None:
    gateway = _check(COMPLIANT, "h1", "certificate.logging-gateway")
    collector = _check(COMPLIANT, "h2", "certificate.logging-collector")
    firewall = _check(COMPLIANT, "h1", "firewall.allowedInbound")

    assert {gateway.status, collector.status, firewall.status} == {
        AuditStatus.COMPLIANT
    }


def test_a_removed_gateway_rule_is_drift(tmp_path: Path) -> None:
    observations = _observations(tmp_path)
    path, document = _edit(observations, "h1")
    spec = document["spec"]
    assert isinstance(spec, dict)
    table = json.loads(spec["firewall"]["managedTableJson"])
    table["nftables"] = [
        item
        for item in table["nftables"]
        if item.get("rule", {}).get("comment") != "operations logging gateway"
    ]
    spec["firewall"]["managedTableJson"] = json.dumps(table)
    path.write_text(json.dumps(document), encoding="utf-8")

    check = _check(observations, "h1", "firewall.allowedInbound")

    assert check.status is AuditStatus.DRIFT
    assert any("3101" in str(rule) for rule in check.desired)  # type: ignore[attr-defined]


def test_a_certificate_within_30_days_of_expiry_is_drift(tmp_path: Path) -> None:
    observations = _observations(tmp_path)
    _replace_certificate(
        observations,
        "h1",
        "logging-gateway",
        _certificate(
            "logs.example.internal", OBSERVED_AT + datetime.timedelta(days=29)
        ),
    )

    check = _check(observations, "h1", "certificate.logging-gateway")

    assert check.status is AuditStatus.DRIFT
    assert check.observed["names"] == ["logs.example.internal"]  # type: ignore[index]


def test_a_certificate_for_other_names_is_drift(tmp_path: Path) -> None:
    observations = _observations(tmp_path)
    _replace_certificate(
        observations,
        "h2",
        "logging-collector",
        _certificate("old.example.internal", OBSERVED_AT + datetime.timedelta(days=80)),
    )

    check = _check(observations, "h2", "certificate.logging-collector")

    assert check.status is AuditStatus.DRIFT


def test_a_missing_certificate_is_drift(tmp_path: Path) -> None:
    observations = _observations(tmp_path)
    _replace_certificate(observations, "h2", "logging-collector", None)

    check = _check(observations, "h2", "certificate.logging-collector")

    assert check.status is AuditStatus.DRIFT
    assert check.observed == {"present": False}


def test_an_observation_without_certificate_evidence_is_unknown(
    tmp_path: Path,
) -> None:
    observations = _observations(tmp_path)
    path, document = _edit(observations, "h2")
    spec = document["spec"]
    assert isinstance(spec, dict)
    del spec["certificates"]
    path.write_text(json.dumps(document), encoding="utf-8")

    check = _check(observations, "h2", "certificate.logging-collector")

    assert check.status is AuditStatus.UNKNOWN


def test_inspect_reads_each_named_certificate_and_marks_a_missing_one(
    tmp_path: Path,
) -> None:
    held = tmp_path / "held.crt"
    held.write_text(_certificate("h1.example.internal", OBSERVED_AT), encoding="utf-8")
    output = tmp_path / "out.json"
    tasks = ROOT / "engine/ansible/roles/cloudfall_inspect/tasks/certificates.yml"
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "cloudfall_inspect_snapshot": {"spec": {"server": "h1"}},
                        "cloudfall_inspect_certificates": [
                            {"name": "held", "path": str(held)},
                            {"name": "gone", "path": str(tmp_path / "gone.crt")},
                        ],
                    },
                    "tasks": [
                        {"ansible.builtin.include_tasks": str(tasks)},
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
            str(Path(sys.executable).with_name("ansible-playbook")),
            "--inventory",
            "localhost,",
            "--connection",
            "local",
            str(playbook),
        ],
        capture_output=True,
        check=True,
        timeout=300,
    )

    evidence = json.loads(output.read_text(encoding="utf-8"))["spec"]["certificates"]
    assert [(item["name"], item["exists"]) for item in evidence] == [
        ("held", True),
        ("gone", False),
    ]
    assert evidence[0]["certificate"] == held.read_text(encoding="utf-8")
    assert evidence[1]["certificate"] is None
