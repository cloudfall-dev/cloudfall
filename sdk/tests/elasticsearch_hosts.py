"""Stand in for the nodes the services converge asks before ``services.yml`` (S-012).

``logging-volumes.yml`` gets a report of an empty pool on every node, and
``logging-requests.yml`` a fresh key and signing request per certificate, as
a host makes them; the keys stay here so a test can check what was signed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

PHYSICAL_VOLUMES = ("/dev/nvme0n1p5", "/dev/nvme1n1p5")


def _report(rows: dict[str, object]) -> str:
    return json.dumps({"report": [rows]})


@dataclass
class FakeNodes:
    """Answer the provider playbooks the way fresh Debian nodes would."""

    playbooks: list[str] = field(default_factory=list)
    hosts: dict[str, str] = field(default_factory=dict)
    keys: dict[tuple[str, str], ec.EllipticCurvePrivateKey] = field(
        default_factory=dict
    )

    def answer(self, playbook: str, extra: dict[str, object]) -> None:
        """Write what ``playbook`` would hand the controller, if it is a provider's."""
        self.playbooks.append(playbook)
        if playbook == "logging-volumes.yml":
            self.hosts[playbook] = str(extra["cloudfall_provider_hosts"])
            self._volumes(extra)
        elif playbook == "logging-requests.yml":
            self.hosts[playbook] = str(extra["cloudfall_provider_hosts"])
            self._requests(extra)

    def _volumes(self, extra: dict[str, object]) -> None:
        root = Path(str(extra["cloudfall_volume_report_directory"]))
        requests = cast(
            "dict[str, list[dict[str, object]]]", extra["cloudfall_volume_requests"]
        )
        pvs = [
            {"pv_name": device, "vg_name": "vg_data", "pv_free": str(100 * 1024**3)}
            for device in PHYSICAL_VOLUMES
        ]
        for server, items in requests.items():
            content = {
                "vgs": _report({"vg": [{"vg_name": "vg_data"}]}),
                "pvs": _report({"pv": pvs}),
                "lvs": _report({"lv": []}),
                "filesystems": {str(item["logicalVolume"]): None for item in items},
                "paths": {
                    str(item["path"]): {"mountedFrom": None, "empty": True}
                    for item in items
                },
                "devices": {
                    path: {"device": path, "error": None} for path in PHYSICAL_VOLUMES
                },
            }
            (root / f"{server}.json").write_text(json.dumps(content), "utf-8")

    def _requests(self, extra: dict[str, object]) -> None:
        root = Path(str(extra["cloudfall_logging_request_directory"]))
        requests = cast(
            "dict[str, list[dict[str, str]]]",
            extra["cloudfall_logging_certificate_requests"],
        )
        for server, items in requests.items():
            (root / server).mkdir(parents=True, exist_ok=True)
            for item in items:
                key = ec.generate_private_key(ec.SECP256R1())
                self.keys[server, item["name"]] = key
                name = x509.Name(
                    [x509.NameAttribute(NameOID.COMMON_NAME, item["commonName"])]
                )
                csr = x509.CertificateSigningRequestBuilder().subject_name(name)
                report = {
                    "request": csr.sign(key, hashes.SHA256())
                    .public_bytes(serialization.Encoding.PEM)
                    .decode(),
                    "certificate": None,
                }
                (root / server / f"{item['name']}.json").write_text(
                    json.dumps(report), "utf-8"
                )
