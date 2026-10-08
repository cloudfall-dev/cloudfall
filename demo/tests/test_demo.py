"""The hosted demo: its copy of the incident, its config, and its approvals."""

from __future__ import annotations

import filecmp
import json
import shutil
from pathlib import Path

from cloudfall_demo import server
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
CLIENT = TestClient(server.app)


def _differences(comparison: filecmp.dircmp[str]) -> list[str]:
    found = [
        *comparison.left_only,
        *comparison.right_only,
        *comparison.diff_files,
        *comparison.funny_files,
    ]
    for sub in comparison.subdirs.values():
        found.extend(_differences(sub))
    return found


def test_the_demo_incident_is_the_example() -> None:
    comparison = filecmp.dircmp(
        ROOT / "examples" / "disk-full-incident",
        ROOT / "demo" / "incident",
        ignore=["README.md", "tmp", "__pycache__"],
    )

    assert _differences(comparison) == []


def test_the_config_lists_every_model_and_the_catalog() -> None:
    config = CLIENT.get("/api/config").json()

    assert len(config["models"]) == 4
    assert config["catalog"]["postgresql-reinit"]["risk"] == "destructive"
    assert config["catalog"]["disk-report"]["risk"] == "read"
    assert {row["model"] for row in config["evaluation"]} >= {"Nemotron 3 Super"}


def test_an_unknown_model_is_refused() -> None:
    response = CLIENT.post("/api/runs", json={"model": "gpt-anything"})

    assert response.status_code == 400


def _proposed(run: str, operation: str) -> str:
    """Copy one recorded decision of the operation into a run, as a proposal."""
    recorded = sorted(
        (server.INCIDENT / "recordings" / "disk-full").glob(f"{operation}-*.json")
    )[0]
    decisions = server.INCIDENT / server.RUNS / run / "decisions"
    decisions.mkdir(parents=True, exist_ok=True)
    shutil.copy(recorded, decisions)
    return recorded.stem


def test_approving_the_fix_plays_back_the_verified_approval() -> None:
    decision = _proposed("testrun1", "shop-logrotate")
    try:
        result = CLIENT.post(
            "/api/approve", json={"run": "testrun1", "decision": decision}
        ).json()
    finally:
        shutil.rmtree(server.INCIDENT / server.RUNS / "testrun1")

    assert result["outcome"] == "verified"
    assert "verify step changed nothing" in result["verdict"]
    assert " 5% /" in result["after"]


def test_approving_the_wipe_is_refused() -> None:
    decision = _proposed("testrun2", "postgresql-reinit")
    try:
        result = CLIENT.post(
            "/api/approve", json={"run": "testrun2", "decision": decision}
        ).json()
    finally:
        shutil.rmtree(server.INCIDENT / server.RUNS / "testrun2")

    assert result["outcome"] == "refused"


def test_an_operation_without_a_recorded_approval_says_so() -> None:
    decision = _proposed("testrun3", "journal-vacuum")
    try:
        result = CLIENT.post(
            "/api/approve", json={"run": "testrun3", "decision": decision}
        ).json()
    finally:
        shutil.rmtree(server.INCIDENT / server.RUNS / "testrun3")

    assert result["outcome"] == "not-recorded"


def test_a_decision_outside_the_runs_is_refused() -> None:
    response = CLIENT.post(
        "/api/approve", json={"run": "..", "decision": "shop-logrotate-1"}
    )

    assert response.status_code == 400
    assert json.loads(response.text)["error"] == "unknown decision"
