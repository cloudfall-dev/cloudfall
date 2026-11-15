"""The inspection's environment evidence is the current release's file (#353).

Each release reads its own ``shared/env/<component>.<release directory>.env``
(D-21), or the legacy ``shared/env/<component>.env`` when its deploy wrote
none, so the evidence the audit compares is the file current's release reads.
The role's own normalizing task is replayed with Ansible's templating on the
stat results its capture tasks would register.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROLE = Path(__file__).parents[2] / "engine" / "ansible" / "roles" / "cloudfall_inspect"
NORMALIZE = "Normalize component environment evidence"
ROOT = "/srv/apps/crm/backend"
RELEASE = "20261028T000000Z-abcdef2"


def _evidence(*, islnk: bool, own: bool) -> dict[str, object]:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "main.yml"))
    assert isinstance(tasks, list)
    task = next(t for t in tasks if t["name"] == NORMALIZE)
    component = {"id": "crm-backend", "install_root": ROOT}
    current = {
        "cloudfall_inspect_component_item": component,
        "stat": (
            {"exists": True, "islnk": True, "lnk_source": f"{ROOT}/releases/{RELEASE}"}
            if islnk
            else {"exists": False}
        ),
    }
    release_env = {
        "cloudfall_inspect_current_result": current,
        "stat": {"exists": True, "checksum": "a" * 64} if own else {"exists": False},
    }
    legacy_env = {
        "cloudfall_inspect_component_item": component,
        "stat": {"exists": True, "checksum": "b" * 64},
    }
    variables: dict[str, object] = {
        "cloudfall_inspect_env_pair": [release_env, legacy_env],
        **{name: trust_as_template(value) for name, value in task["vars"].items()},
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    evidence = templar.template(
        trust_as_template(
            task["ansible.builtin.set_fact"]["cloudfall_inspect_component_environment"]
        )
    )
    assert isinstance(evidence, list)
    (entry,) = evidence
    assert isinstance(entry, dict)
    return entry


@pytest.mark.parametrize(
    ("islnk", "own", "path", "sha256"),
    [
        (True, True, f"{ROOT}/shared/env/crm-backend.{RELEASE}.env", "a" * 64),
        (True, False, f"{ROOT}/shared/env/crm-backend.env", "b" * 64),
        (False, False, f"{ROOT}/shared/env/crm-backend.env", "b" * 64),
    ],
    ids=["own-file", "deployed-before-or-without", "no-current"],
)
def test_the_evidence_is_the_file_currents_release_reads(
    islnk: bool,  # noqa: FBT001 - a parametrized case
    own: bool,  # noqa: FBT001 - a parametrized case
    path: str,
    sha256: str,
) -> None:
    assert _evidence(islnk=islnk, own=own) == {
        "component": "crm-backend",
        "path": path,
        "exists": True,
        "sha256": sha256,
    }
