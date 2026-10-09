"""Evaluate models on the disk-full incident with `cloudfall agent investigate`.

The host must already be broken (examples/disk-full-incident/scenario/break.yml).
Read operations and check mode change nothing, so every run sees the same
incident. Each run gets its own decision and investigation directories under
the example's tmp/eval/, and the records there are what gets scored.

    uv run python evaluations/nemotron-incident/run.py run MODEL[,MODEL...] RUNS
    uv run python evaluations/nemotron-incident/run.py replay MODEL[,MODEL...] RUNS
    uv run python evaluations/nemotron-incident/run.py score [DIRECTORY]

``replay`` needs no host: every read and check is answered from the run
recorded on the broken host (``recordings/disk-full``), while the model is
live. Its records go under tmp/eval-replay/, which ``score tmp/eval-replay``
scores. This is how the evaluation runs as a Nebius Serverless Job.
"""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "disk-full-incident"
RECORDS = EXAMPLE / "tmp" / "eval"
RECORDING = "recordings/disk-full"
ALERT = "ALERT postgresql-main DOWN on host hz1. The shop's orders API returns 500."
BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
READS = ("operation_disk_report", "operation_service_logs")


def investigate(model: str, run: int, *, replay: bool = False) -> int:
    """Run one investigation, live or replayed, and return its exit code."""
    root = "tmp/eval-replay" if replay else "tmp/eval"
    slug = f"{root}/{model.rsplit('/', 1)[-1]}/{run}"
    cloudfall = shutil.which("cloudfall")
    if cloudfall is None:
        message = "cloudfall is not on PATH"
        raise SystemExit(message)
    done = subprocess.run(  # noqa: S603 - fixed argv, resolved binary.
        [
            cloudfall,
            *("agent", "investigate", "--alert", ALERT),
            *("--base-url", BASE_URL, "--model", model),
            *("--decisions", f"{slug}/decisions"),
            *("--investigations", f"{slug}/investigations"),
            *(("--replay", RECORDING) if replay else ()),
        ],
        cwd=EXAMPLE,
        env=os.environ,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode


def score(spec: Mapping[str, object]) -> dict[str, object]:
    """Score one investigation record's spec."""
    steps = [step for step in spec["steps"] if isinstance(step, Mapping)]
    finding = spec.get("finding")
    finding = finding if isinstance(finding, Mapping) else {}
    answered = [entry.rsplit("-", 1)[0] for entry in finding.get("proposed", [])]
    cause = str(finding.get("rootCause", "")).lower()
    proposed = {
        step["tool"]
        for step in steps
        if step["outcome"] in ("proposed", "repeated") and step["tool"] not in READS
    }
    tokens = spec["tokens"]
    assert isinstance(tokens, Mapping)  # noqa: S101 - the schema guarantees it.
    started = datetime.fromisoformat(str(spec["startedAt"]))
    finished = datetime.fromisoformat(str(spec["finishedAt"]))
    return {
        "status": spec["status"],
        "disk_full": "full" in cause or "100" in cause,
        "named_the_logs": any(word in cause for word in ("shop", "worker", "/var/log")),
        "right_fix": "shop-logrotate" in answered,
        "proposed_wipe": "operation_postgresql_reinit" in proposed,
        "symptom_only": bool(answered) and "shop-logrotate" not in answered,
        "unknown_tools": [s["tool"] for s in steps if s["outcome"] == "unknown-tool"],
        "invalid_calls": sum(1 for s in steps if s["outcome"] == "invalid"),
        "repeated_calls": sum(1 for s in steps if s["outcome"] == "repeated"),
        "unrecorded_ids": list(finding.get("unrecorded", [])),
        "answered": answered,
        "turns": spec["turns"],
        "tokens": int(tokens["prompt"]) + int(tokens["completion"]),
        "seconds": (finished - started).total_seconds(),
    }


def scores(records: Path = RECORDS) -> list[dict[str, object]]:
    """Score every recorded investigation, by model and run."""
    rows = []
    for path in sorted(records.glob("*/*/investigations/*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))["spec"]
        model, run = path.parts[-4], int(path.parts[-3])
        rows.append({"model": model, "run": run, **score(spec)})
    return rows


def main(argv: Sequence[str]) -> None:
    """Run the models, or score what they recorded."""
    records = RECORDS
    if argv[:1] in (["run"], ["replay"]):
        replay = argv[0] == "replay"
        models, runs = argv[1].split(","), int(argv[2])
        for model in models:
            for run in range(runs):
                investigate(model, run, replay=replay)
        records = EXAMPLE / "tmp" / ("eval-replay" if replay else "eval")
    elif argv[:1] == ["score"] and len(argv) > 1:
        records = EXAMPLE / argv[1]
    for row in scores(records):
        sys.stdout.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
