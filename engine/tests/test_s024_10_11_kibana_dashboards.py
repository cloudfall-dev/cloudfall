"""Kibana's dashboards survive a snapshot restore and a rebuilt server (S-024, #580).

S-024-10: the snapshot script of the Elasticsearch role, rendered with
Ansible's own templating, runs for real against the fake node of
``test_s012_elasticsearch_snapshots``; the guide's restore steps are read from
``docs/kibana-service-guide.md``. S-024-11: the Kibana role's keystore tasks run
for real twice, on a converged host and on a rebuilt empty one, against the
stand-ins of ``kibana_node``. Restoring the feature state on a live cluster,
Kibana starting on it, and ``analyst`` opening the dashboard need a live host
and are listed in the pull request as unverified.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from test_s012_elasticsearch_snapshots import (
    PASSWORD,
    FakeElasticsearch,
    _render,
    _service,
    _variables,
)
from test_s012_elasticsearch_snapshots import (
    fake as fake,  # noqa: PLC0414 - the fixture, used by name
)
from test_s024_kibana_role import KEYS, SETTINGS, TOKEN_SETTING, Node, requires_ansible

ROOT = Path(__file__).parents[2]
SERVICES = ROOT / "engine" / "ansible" / "playbooks" / "services.yml"
GUIDE = ROOT / "docs" / "kibana-service-guide.md"


def _script(tmp_path: Path, port: int, kibana: list[str]) -> Path:
    """Render the backup script as the converge does with ``kibana`` reading it."""
    password = tmp_path / "cloudfall-password"
    password.write_text(PASSWORD + "\n", encoding="utf-8")
    rendered = _render(
        "snapshots.py.j2",
        _variables(
            _service(port),
            cloudfall_elasticsearch_snapshot_action="backup",
            cloudfall_elasticsearch_password_path=str(password),
            cloudfall_elasticsearch_kibana_services=kibana,
        ),
    )
    script = tmp_path / "cloudfall-elasticsearch-backup"
    script.write_text(
        rendered.replace("#!/usr/bin/python3", f"#!{sys.executable}", 1),
        encoding="utf-8",
    )
    return script


def _backup(
    tmp_path: Path, port: int, kibana: list[str]
) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(  # noqa: S603 - the rendered script under test.
        [sys.executable, str(_script(tmp_path, port, kibana))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert PASSWORD not in done.stdout + done.stderr
    return done


# proves: S-024-10
def test_s024_10_the_snapshot_lists_the_feature_state_kibana(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    state.feature_states = ["kibana", "security"]

    done = _backup(tmp_path, port, ["search-kibana"])

    assert done.returncode == 0, done.stderr
    (taken,) = state.snapshots
    features = cast("list[dict[str, object]]", taken["feature_states"])
    assert "kibana" in [feature["feature_name"] for feature in features]
    assert f"snapshot {taken['snapshot']} holds the feature state kibana" in done.stdout
    # The request is S-012's: the cluster state and every feature state, not
    # the feature state kibana alone
    assert state.body("PUT", "/_snapshot/") == {
        "include_global_state": True,
        "metadata": {"takenBy": "cloudfall", "service": "search-main"},
    }


# proves: S-024-10
def test_s024_10_a_snapshot_without_the_feature_state_kibana_fails_the_backup(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    state.feature_states = ["security"]
    state.snapshot("cloudfall-20260101-000000", age_days=60)

    done = _backup(tmp_path, port, ["search-kibana"])

    assert done.returncode == 1
    assert "holds no feature state kibana" in done.stderr
    assert "Kibana service search-kibana" in done.stderr
    # No retention after a failed run: the old snapshot stays
    assert state.requests("DELETE") == []
    assert "cloudfall-20260101-000000" in state.names()


def test_a_cluster_without_kibana_backs_up_and_renders_as_before(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    _, port = fake

    done = _backup(tmp_path, port, [])

    assert done.returncode == 0, done.stderr
    assert "feature state" not in done.stdout
    text = _script(tmp_path, port, []).read_text(encoding="utf-8")
    assert "KIBANA" not in text
    assert "feature_state" not in text
    # With Kibana, the script gains its check and nothing else changes
    with_kibana = _script(tmp_path, port, ["search-kibana"]).read_text("utf-8")
    without = text.splitlines()
    added = [line for line in with_kibana.splitlines() if line not in without]
    assert [line for line in without if line not in with_kibana.splitlines()] == []
    assert any("KIBANA_FEATURE_STATE" in line for line in added)


# proves: S-024-10
def test_s024_10_services_yml_tells_each_cluster_which_kibana_reads_it() -> None:
    nodes, _ = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))
    deploy = next(
        task
        for task in nodes["tasks"]
        if task.get("ansible.builtin.include_role", {}).get("name")
        == "cloudfall_elasticsearch"
    )
    expression = deploy["vars"]["cloudfall_elasticsearch_kibana_services"]
    kibana = {
        "id": "search-kibana",
        "serviceKind": "kibana",
        "server": "h2",
        "kibana": {"elasticsearch": "search-main", "users": []},
    }
    other = {**kibana, "id": "logs-kibana", "kibana": {"elasticsearch": "logs-main"}}
    hostvars = {
        "h1": {"cloudfall_services": [{"id": "search-main", "serviceKind": "x"}]},
        # The Kibana of the cluster runs on another node than the home
        "h2": {"cloudfall_services": [kibana, other]},
        "h3": {},
    }

    def found(cluster: str) -> object:
        templar = Templar(
            loader=DataLoader(),
            variables={
                "groups": {"cloudfall_servers": ["h1", "h2", "h3"]},
                "hostvars": hostvars,
                "cloudfall_service_item": {"id": cluster},
            },
        )
        return templar.template(trust_as_template(expression))

    assert found("search-main") == ["search-kibana"]
    assert found("crm-main") == []


def _guide_commands() -> list[str]:
    """Return the console blocks of the guide's restore section, in order."""
    text = GUIDE.read_text(encoding="utf-8")
    section = text.split("## Restore the dashboards", 1)[1].split("\n## ", 1)[0]
    return [
        " ".join(
            line.strip().rstrip("\\").strip() for line in block.splitlines()
        ).strip()
        for block in re.findall(r"```console\n(.*?)```", section, flags=re.DOTALL)
    ]


# proves: S-024-10
def test_s024_10_the_guide_stops_kibana_restores_the_feature_state_and_starts_it() -> (
    None
):
    stop, restore, start = _guide_commands()

    assert stop == "sudo systemctl stop kibana.service"
    assert start == "sudo systemctl start kibana.service"
    request = re.search(r"'(http://[^']+)'", restore)
    assert request is not None
    assert re.fullmatch(
        r"http://127\.0\.0\.1:9200/_snapshot/cloudfall/cloudfall-[0-9]{8}-[0-9]{6}"
        r"/_restore\?wait_for_completion=true",
        request[1],
    )
    assert "-X POST" in restore
    body = re.search(r"-d '(\{.*\})'", restore)
    assert body is not None
    # Only the feature state: no data index and not the cluster state
    assert json.loads(body[1]) == {
        "indices": "-*",
        "include_global_state": False,
        "feature_states": ["kibana"],
    }
    # Cloudfall's account, its password asked for, never on the command line
    assert "--user cloudfall " in restore
    assert PASSWORD not in restore


@pytest.fixture
def rebuilt(tmp_path: Path) -> tuple[Node, Node]:
    """Two hosts of the same Kibana service: the first, and the rebuilt one."""
    first, second = tmp_path / "first", tmp_path / "rebuilt"
    first.mkdir()
    second.mkdir()
    return Node(first), Node(second)


# proves: S-024-11
@requires_ansible
def test_s024_11_a_rebuilt_server_gets_the_stores_keys_and_a_new_token(
    rebuilt: tuple[Node, Node],
) -> None:
    before, after = rebuilt
    before.converge()
    old_token = before.keystore[TOKEN_SETTING]

    # The rebuilt server is empty: no keystore, no digest, no service_tokens
    assert not (after.kibana_conf / "kibana.keystore").exists()
    assert not (after.elasticsearch_conf / "service_tokens").exists()
    done, changed = after.converge()

    assert changed > 0
    # The three keys in the keystore are the store's, the ones the old
    # server had, so saved objects encrypted before still decrypt
    stored = {SETTINGS[name]: value for name, value in KEYS.items()}
    assert {setting: after.keystore[setting] for setting in SETTINGS.values()} == (
        stored
    )
    assert {setting: before.keystore[setting] for setting in SETTINGS.values()} == (
        stored
    )
    # No task makes a key on the server: each comes from the staged store
    assert all(
        call[:2] != ["kibana-keystore", "add"] or call[-1] != "--generate"
        for call in after.writes()
    )
    # A new token of elastic/kibana in the rebuilt node's own file
    (line,) = after.service_tokens.splitlines()
    assert line.startswith("elastic/kibana/search-kibana:")
    assert after.keystore[TOKEN_SETTING] != old_token
    for secret in (after.keystore[TOKEN_SETTING], *KEYS.values()):
        assert secret not in done.stdout
        assert secret not in done.stderr


# proves: S-024-11
@requires_ansible
def test_s024_11_a_second_converge_of_the_rebuilt_server_keeps_its_keys(
    rebuilt: tuple[Node, Node],
) -> None:
    _, after = rebuilt
    after.converge()
    keystore = after.keystore

    _, changed = after.converge()

    assert changed == 0
    assert after.keystore == keystore
