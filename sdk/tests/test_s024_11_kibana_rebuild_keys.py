"""A rebuilt Kibana server gets the store's keys, not new ones (S-024-11, #580).

The controller side: every services converge stages the three keys the
project's store holds, and the store never replaces a held key, so a server
converged empty after a rebuild gets the keys its saved objects were
encrypted with. The host side is tested in
``engine/tests/test_s024_10_11_kibana_dashboards.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from cloudfall.inventory import PlatformInventory
from cloudfall.lifecycle import converge_services
from cloudfall.requirements import requirements_of
from cloudfall.secret_store import (
    ERROR_KEY_EXISTS,
    SecretStore,
    secret_requirements,
)
from cloudfall.secrets import SecretsError
from cloudfall.validation import validate_config
from kibana_project import SCHEMAS
from test_s024_kibana_converge import KEYS, Recorder, _context, _project, _provider

if TYPE_CHECKING:
    from pathlib import Path

STAGED = {
    "securityKey": KEYS["SECURITY_ENCRYPTION_KEY"],
    "savedObjectsKey": KEYS["SAVED_OBJECTS_ENCRYPTION_KEY"],
    "reportingKey": KEYS["REPORTING_ENCRYPTION_KEY"],
}


def _staged(recorder: Recorder) -> dict[str, str]:
    return {
        name: recorder.staged[f"search-kibana.{name}"][1]
        for name in ("securityKey", "savedObjectsKey", "reportingKey")
    }


# proves: S-024-11
def test_s024_11_every_converge_stages_the_keys_the_store_holds(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    store_file = project / "secrets" / "production" / "services" / "search-kibana.env"
    stored = store_file.read_bytes()

    first, rebuilt = Recorder(), Recorder()
    converge_services(_context(project, first), _provider(project))
    # The server is rebuilt empty and converged again: the same keys
    converge_services(_context(project, rebuilt), _provider(project))

    assert _staged(first) == STAGED
    assert _staged(rebuilt) == STAGED
    assert store_file.read_bytes() == stored


# proves: S-024-11
def test_s024_11_the_store_never_replaces_a_held_key(tmp_path: Path) -> None:
    project = _project(tmp_path)
    store = SecretStore(
        project / "secrets",
        decrypt=lambda path: path.read_text(encoding="utf-8"),
        encrypt=lambda _path, text: text,
    )
    inventory = PlatformInventory.from_state(validate_config(project, SCHEMAS))
    wanted = [
        spec
        for spec in secret_requirements(requirements_of(inventory))
        if spec.reference.path.value == "/services/search-kibana"
    ]

    assert len(wanted) == len(KEYS)
    for spec in wanted:
        assert store.holds(spec.reference)
        with pytest.raises(SecretsError) as error:
            store.generate(spec.reference)
        assert error.value.code == ERROR_KEY_EXISTS
        assert store.value(spec.reference) == KEYS[spec.reference.key.value]
