"""Elasticsearch snapshots and their restore drill (S-012, #313).

The snapshot and restore-drill script is rendered from the role's template
with Ansible's own templating and runs for real against a fake Elasticsearch:
an HTTP server on loopback that answers the snapshot, restore, count and
health APIs the way a node does and records every request. ``backup.yml``
runs on localhost with the rendered scripts, so the receipt is the one
``cloudfall backup run`` and ``backup verify`` read.
"""

from __future__ import annotations

import base64
import copy
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.validation import SchemaCatalog

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_elasticsearch"
PLAYBOOKS = ROOT / "engine" / "ansible" / "playbooks"
SCHEMAS = ROOT / "config" / "schemas" / "v1"
PASSWORD = "synthetic-cloudfall-snapshot-value"  # noqa: S105 - grepped for
DAY_MS = 86_400_000
GIB = 1024**3
INDEX_MISSING = "index_not_found_exception"
SNAPSHOT_MISSING = "snapshot_missing_exception"
REPOSITORY_MISSING = "repository_missing_exception"
COUNTS = "cloudfall-snapshot-counts"
COPY = "cloudfall-restore-check-"
# Elasticsearch's default http.max_initial_line_length (4kb).
MAX_INITIAL_LINE = 4096
# Live indices: name -> (documents, primary store bytes).
INDICES = {
    "crm-1": (3, 4_000),
    "crm-2": (7, 9_000),
    "billing-1": (40, 80_000),
    ".security-7": (2, 1_000),
}


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class FakeElasticsearch:
    """The state of one fake node and every request it answered."""

    indices: dict[str, int] = field(
        default_factory=lambda: {name: docs for name, (docs, _) in INDICES.items()}
    )
    sizes: dict[str, int] = field(
        default_factory=lambda: {name: size for name, (_, size) in INDICES.items()}
    )
    registered: bool = True
    outcome: str = "SUCCESS"
    # How many times a new snapshot answers IN_PROGRESS before its outcome.
    polls: int = 1
    # The age a new snapshot reports, as if the node's clock were behind.
    new_snapshot_age_days: int = 0
    snapshots: list[dict[str, object]] = field(default_factory=list)
    # Documents a restored copy holds, by source index, instead of the snapshot's.
    restored_counts: dict[str, int] = field(default_factory=dict)
    # Indices whose restore fails a shard.
    failing_restores: set[str] = field(default_factory=set)
    available: int = 50 * GIB
    total: int = 100 * GIB
    watermark: str = "85%"
    # The documents of indices whose documents a test reads by id.
    documents: dict[str, dict[str, object]] = field(default_factory=dict)
    calls: list[tuple[str, str, object]] = field(default_factory=list)
    authorizations: set[str] = field(default_factory=set)
    created: list[tuple[str, object]] = field(default_factory=list)

    def snapshot(
        self,
        name: str,
        *,
        age_days: float,
        state: str = "SUCCESS",
        service: str | None = "search-main",
        taken_by: str = "cloudfall",
    ) -> None:
        """Put a snapshot in the repository, as an earlier run left it."""
        metadata = (
            None if service is None else {"takenBy": taken_by, "service": service}
        )
        self.snapshots.append(
            {
                "snapshot": name,
                "state": state,
                "start_time_in_millis": _now_ms() - int(age_days * DAY_MS),
                "metadata": metadata,
                "indices": [index for index in self.indices if index in INDICES],
                "contents": dict(self.indices),
                "documents": copy.deepcopy(self.documents),
                "polls_left": 0,
            }
        )

    def names(self) -> list[str]:
        """Return the names of the snapshots in the repository, oldest put first."""
        return [str(snapshot["snapshot"]) for snapshot in self.snapshots]

    def requests(self, method: str | None = None) -> list[str]:
        """Return each request as ``METHOD path``, of ``method`` only when given."""
        return [
            f"{verb} {path}"
            for verb, path, _ in self.calls
            if method is None or verb == method
        ]

    def body(self, method: str, prefix: str) -> dict[str, object]:
        """Return the body of the first ``method`` request under ``prefix``."""
        return self.bodies(method, prefix)[0]

    def bodies(self, method: str, prefix: str) -> list[dict[str, object]]:
        """Return the bodies of the ``method`` requests under ``prefix``."""
        return [
            cast("dict[str, object]", body)
            for verb, path, body in self.calls
            if verb == method and path.startswith(prefix)
        ]

    def restored_indices(self) -> list[str]:
        """Return the ``indices`` of each restore request, in order."""
        return [
            str(body["indices"])
            for verb, path, body in self.calls
            if verb == "POST" and path.endswith("/_restore")
            for body in [cast("dict[str, object]", body)]
        ]


class _Answer(Exception):  # noqa: N818 - control flow of the fake, not an error
    def __init__(self, status: int, body: object) -> None:
        super().__init__(status)
        self.status = status
        self.body = body


def _public(snapshot: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in snapshot.items()
        if key not in {"contents", "documents", "polls_left"}
    }


def _missing(kind: str, name: str) -> _Answer:
    return _Answer(404, {"error": {"type": kind, "reason": f"[{name}] missing"}})


def _route(  # noqa: C901, PLR0911 - one fake API, one dispatch
    fake: FakeElasticsearch,
    method: str,
    parts: list[str],
    query: dict[str, list[str]],
    body: object,
) -> object:
    if parts[:2] == ["_cat", "indices"]:
        return [
            {"index": name, "pri.store.size": str(fake.sizes.get(name, 0))}
            for name in fake.indices
        ]
    if parts[:1] == ["_cluster"]:
        if parts[1] == "settings":
            return {
                "persistent": {},
                "transient": {},
                "defaults": {
                    "cluster.routing.allocation.disk.watermark.low": fake.watermark,
                    "cluster.routing.allocation.disk.watermark.low.max_headroom": (
                        "200gb"
                    ),
                },
            }
        return {"status": "green", "timed_out": False}
    if parts[:1] == ["_nodes"]:
        fs = {"total_in_bytes": fake.total, "available_in_bytes": fake.available}
        return {"nodes": {"Qx9": {"name": "h1", "fs": {"total": fs}}}}
    if parts[:1] == ["_snapshot"]:
        return _snapshot_route(fake, method, parts[1:], query, body)
    index = parts[0]
    if parts[1:] == ["_refresh"]:
        return {"_shards": {"failed": 0}}
    if method == "PUT" and len(parts) == 1:
        fake.created.append((index, body))
        fake.indices[index] = 0
        fake.documents[index] = {}
        return {"acknowledged": True}
    if index not in fake.indices:
        raise _missing(INDEX_MISSING, index)
    if parts[1:2] == ["_doc"]:
        return _document(fake, method, index, parts[2], body)
    if method == "HEAD":
        return None
    if method == "DELETE":
        del fake.indices[index]
        fake.documents.pop(index, None)
        return {"acknowledged": True}
    if parts[1:] == ["_count"]:
        return {"count": fake.indices[index]}
    raise _Answer(400, {"error": f"unexpected {method} {'/'.join(parts)}"})


def _document(
    fake: FakeElasticsearch, method: str, index: str, key: str, body: object
) -> object:
    documents = fake.documents.setdefault(index, {})
    if method == "PUT":
        documents[key] = body
    elif key not in documents:
        raise _Answer(404, {"_id": key, "found": False})
    elif method == "DELETE":
        del documents[key]
    else:
        return {"_id": key, "found": True, "_source": documents[key]}
    fake.indices[index] = len(documents)
    return {"_id": key, "result": "ok"}


def _snapshot_route(  # noqa: C901, PLR0911, PLR0912 - the snapshot API of the fake
    fake: FakeElasticsearch,
    method: str,
    parts: list[str],
    query: dict[str, list[str]],
    body: object,
) -> object:
    repository = parts[0]
    if repository != "cloudfall" or not fake.registered:
        raise _missing(REPOSITORY_MISSING, repository)
    if len(parts) == 1:
        return {"cloudfall": {"type": "fs", "settings": {"location": "/x"}}}
    if parts[1] == "_verify":
        return {"nodes": {"Qx9": {"name": "h1"}}}
    names = parts[1].split(",")
    if len(parts) == 3 and parts[2] == "_restore":
        return _restore(fake, names[0], cast("dict[str, object]", body))
    if method == "PUT":
        payload = cast("dict[str, object]", body)
        fake.snapshots.append(
            {
                "snapshot": names[0],
                "state": "IN_PROGRESS",
                "start_time_in_millis": _now_ms()
                - fake.new_snapshot_age_days * DAY_MS,
                "metadata": payload["metadata"],
                "indices": list(fake.indices),
                "contents": dict(fake.indices),
                "documents": copy.deepcopy(fake.documents),
                "polls_left": fake.polls,
                "shards": {"total": 4, "failed": 0, "successful": 4},
            }
        )
        return {"accepted": True}
    if method == "DELETE":
        if not set(names) <= set(fake.names()):
            raise _missing(SNAPSHOT_MISSING, parts[1])
        fake.snapshots = [s for s in fake.snapshots if s["snapshot"] not in names]
        return {"acknowledged": True}
    if "*" in parts[1]:
        found = [
            s for s in fake.snapshots if fnmatch.fnmatch(str(s["snapshot"]), parts[1])
        ]
        return {"snapshots": [_public(s) for s in found], "total": len(found)}
    snapshot = next((s for s in fake.snapshots if s["snapshot"] == names[0]), None)
    if snapshot is None:
        raise _missing(SNAPSHOT_MISSING, names[0])
    if snapshot["state"] == "IN_PROGRESS":
        polls = cast("int", snapshot["polls_left"])
        if polls == 0:
            snapshot["state"] = fake.outcome
            if fake.outcome != "SUCCESS":
                snapshot["shards"] = {"total": 4, "failed": 1, "successful": 3}
                snapshot["failures"] = [{"reason": "shard failed"}]
        snapshot["polls_left"] = polls - 1
    answer = _public(snapshot)
    if query.get("index_details") == ["true"]:
        answer["index_details"] = {
            name: {"size_in_bytes": fake.sizes.get(name, 0)}
            for name in cast("list[str]", snapshot["indices"])
        }
    return {"snapshots": [answer]}


def _restore(
    fake: FakeElasticsearch, name: str, body: dict[str, object]
) -> dict[str, object]:
    snapshot = next(s for s in fake.snapshots if s["snapshot"] == name)
    contents = cast("dict[str, int]", snapshot["contents"])
    documents = cast("dict[str, dict[str, object]]", snapshot["documents"])
    replacement = str(body["rename_replacement"]).replace("$1", r"\1")
    restored = []
    requested = str(body["indices"]).split(",")
    for index in requested:
        target = re.sub(f"^{body['rename_pattern']}$", replacement, index)
        if target in fake.indices:
            raise _Answer(500, {"error": f"an open index [{target}] exists"})
        fake.indices[target] = fake.restored_counts.get(index, contents[index])
        fake.sizes[target] = fake.sizes.get(index, 0)
        if index in documents:
            fake.documents[target] = copy.deepcopy(documents[index])
        restored.append(target)
    total = len(restored)
    failed = len(fake.failing_restores & set(requested))
    return {
        "snapshot": {
            "snapshot": name,
            "indices": restored,
            "shards": {"total": total, "failed": failed, "successful": total - failed},
        }
    }


def _handler(fake: FakeElasticsearch) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            split = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            fake.calls.append((self.command, unquote(split.path), body))
            fake.authorizations.add(self.headers.get("Authorization", ""))
            parts = [unquote(part) for part in split.path.strip("/").split("/")]
            status: int
            answer: object
            # A node refuses a request line over http.max_initial_line_length.
            if len(self.requestline.encode()) > MAX_INITIAL_LINE:
                status, answer = 400, {"error": "too_long_http_line_exception"}
            else:
                try:
                    status, answer = 200, _route(
                        fake, self.command, parts, parse_qs(split.query), body
                    )
                except _Answer as error:
                    status, answer = error.status, error.body
            raw = b"" if answer is None else json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = _serve  # noqa: N815

        def log_message(self, *_: object) -> None:
            """Keep the test output to the test's own."""

    return Handler


@pytest.fixture
def fake() -> Iterator[tuple[FakeElasticsearch, int]]:
    state = FakeElasticsearch()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _service(port: int, **elasticsearch: object) -> dict[str, object]:
    return {
        "id": "search-main",
        "serviceKind": "elasticsearch",
        "environment": "production",
        "server": "h1",
        "bind": {"address": "127.0.0.1", "port": port},
        "elasticsearch": {
            "version": "9.2.4",
            "heapMb": 4096,
            "nodes": ["h1"],
            "clusterName": "search-main",
            "users": [],
            **elasticsearch,
        },
        "backup": {
            "directory": "/var/backups/cloudfall/search-main",
            "onCalendar": "*-*-* 03:20:00 UTC",
            "restoreCheckOnCalendar": "Sun *-*-* 05:00:00 UTC",
            "retentionDays": 14,
        },
        "metrics": {"enabled": True},
    }


def _defaults() -> dict[str, object]:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    assert isinstance(defaults, dict)
    return {
        name: trust_as_template(value) if isinstance(value, str) else value
        for name, value in defaults.items()
    }


def _variables(service: dict[str, object], **extra: object) -> dict[str, object]:
    return {
        **_defaults(),
        "ansible_check_mode": False,
        "cloudfall_server_id": "h1",
        "cloudfall_elasticsearch_service": service,
        "cloudfall_elasticsearch_snapshot_poll_seconds": 0,
        **extra,
    }


def _render(name: str, variables: dict[str, object]) -> str:
    text = (ROLE / "templates" / name).read_text(encoding="utf-8")
    templar = Templar(loader=DataLoader(), variables=variables)
    return str(templar.template(trust_as_template(text)))


def _script(
    tmp_path: Path, port: int, action: str, **elasticsearch: object
) -> Path:
    """Render the script for ``action``; it reads the password from tmp_path."""
    password = tmp_path / "cloudfall-password"
    password.write_text(PASSWORD + "\n", encoding="utf-8")
    rendered = _render(
        "snapshots.py.j2",
        _variables(
            _service(port, **elasticsearch),
            cloudfall_elasticsearch_snapshot_action=action,
            cloudfall_elasticsearch_password_path=str(password),
        ),
    )
    script = tmp_path / f"cloudfall-elasticsearch-{action}"
    script.write_text(
        rendered.replace("#!/usr/bin/python3", f"#!{sys.executable}", 1),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _run(
    tmp_path: Path, port: int, action: str, **elasticsearch: object
) -> subprocess.CompletedProcess[str]:
    script = _script(tmp_path, port, action, **elasticsearch)
    done = subprocess.run(  # noqa: S603 - the rendered script under test.
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert PASSWORD not in done.stdout + done.stderr
    return done


def _basic() -> str:
    return "Basic " + base64.b64encode(f"cloudfall:{PASSWORD}".encode()).decode()


# proves: S-012-10
def test_s012_10_backup_takes_a_snapshot_waits_for_success_and_records_counts(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake

    done = _run(tmp_path, port, "backup")

    assert done.returncode == 0, done.stderr
    (taken,) = state.snapshots
    name = str(taken["snapshot"])
    assert re.fullmatch(r"cloudfall-[0-9]{8}-[0-9]{6}", name)
    assert taken["state"] == "SUCCESS"
    assert state.requests() == [
        "GET /_cat/indices",
        "POST /crm-1/_refresh",
        "GET /crm-1/_count",
        f"HEAD /{COUNTS}",
        f"PUT /{COUNTS}",
        f"PUT /{COUNTS}/_doc/{name}",
        f"PUT /_snapshot/cloudfall/{name}",
        f"GET /_snapshot/cloudfall/{name}",
        f"GET /_snapshot/cloudfall/{name}",
        "GET /_snapshot/cloudfall/cloudfall-*",
    ]
    # Every index and the cluster state; the smallest index is the drill's,
    # its count kept in Cloudfall's own index, which the snapshot holds.
    assert state.body("PUT", "/_snapshot/") == {
        "include_global_state": True,
        "metadata": {"takenBy": "cloudfall", "service": "search-main"},
    }
    assert state.documents[COUNTS] == {
        name: {
            "service": "search-main",
            "snapshot": name,
            "counts": [{"index": "crm-1", "documents": 3}],
        }
    }
    assert COUNTS in cast("list[str]", taken["indices"])
    ((created, body),) = state.created
    mappings = cast("dict[str, object]", cast("dict[str, object]", body)["mappings"])
    assert created == COUNTS
    assert mappings["dynamic"] is False
    assert state.authorizations == {_basic()}
    assert f"snapshot {name} of search-main: SUCCESS, 5 indices" in done.stdout
    assert "restore check will restore 1 indices, 3 documents" in done.stdout


def test_a_second_backup_reuses_the_counts_index(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    assert _run(tmp_path, port, "backup").returncode == 0
    time.sleep(1.1)  # snapshot names are to the second

    assert _run(tmp_path, port, "backup").returncode == 0
    assert len(state.created) == 1
    assert sorted(state.documents[COUNTS]) == sorted(state.names())


# proves: S-012-10
@pytest.mark.parametrize("outcome", ["PARTIAL", "FAILED"])
def test_s012_10_a_snapshot_that_is_not_success_fails_the_run(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int], outcome: str
) -> None:
    state, port = fake
    state.outcome = outcome
    state.snapshot("cloudfall-20260101-000000", age_days=60)

    done = _run(tmp_path, port, "backup")

    assert done.returncode == 1
    assert f"ended {outcome}, not SUCCESS (1 of 4 shards failed)" in done.stderr
    assert "shard failed" in done.stderr
    assert "SUCCESS," not in done.stdout
    # No retention after a failed snapshot: the old one stays.
    assert state.requests("DELETE") == []
    assert "cloudfall-20260101-000000" in state.names()


# proves: S-012-10
def test_s012_10_restore_check_restores_a_renamed_copy_compares_and_deletes_it(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    assert _run(tmp_path, port, "backup").returncode == 0
    (taken,) = state.names()
    live = dict(state.indices)
    state.calls.clear()

    done = _run(tmp_path, port, "restore-check")

    assert done.returncode == 0, done.stderr
    assert state.requests() == [
        "POST /_snapshot/cloudfall/_verify",
        "GET /_snapshot/cloudfall/cloudfall-*",
        f"HEAD /{COPY}{COUNTS}",
        f"POST /_snapshot/cloudfall/{taken}/_restore",
        f"GET /_cluster/health/{COPY}{COUNTS}",
        f"GET /{COPY}{COUNTS}/_doc/{taken}",
        f"GET /_snapshot/cloudfall/{taken}",
        "GET /_nodes/_local/stats/fs",
        "GET /_cluster/settings",
        f"HEAD /{COPY}crm-1",
        f"POST /_snapshot/cloudfall/{taken}/_restore",
        f"GET /_cluster/health/{COPY}crm-1",
        f"GET /{COPY}crm-1/_count",
        f"DELETE /{COPY}{COUNTS}",
        f"DELETE /{COPY}crm-1",
    ]
    options = {
        "include_global_state": False,
        "include_aliases": False,
        "rename_pattern": "(.+)",
        "rename_replacement": "cloudfall-restore-check-$1",
        "index_settings": {"index.number_of_replicas": 0},
    }
    assert state.bodies("POST", f"/_snapshot/cloudfall/{taken}/_restore") == [
        {"indices": COUNTS, **options},
        {"indices": "crm-1", **options},
    ]
    # The live indices are as they were; the copies are gone.
    assert state.indices == live
    assert (
        f"restore check passed for search-main: snapshot {taken} restored "
        "1 indices (crm-1) with 3 documents, as recorded" in done.stdout
    )


# proves: S-012-10
def test_s012_10_restore_check_restores_every_index_the_patterns_match(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    """Many matching indices, long names: every one is restored and compared."""
    state, port = fake
    for number in range(80):
        state.indices[f"crm-{number:03d}-{'x' * 40}"] = number
    crm = sorted(name for name in state.indices if name.startswith("crm-"))
    assert len(crm) == 82
    patterns = {"restoreCheckIndices": ["crm-*"]}
    assert _run(tmp_path, port, "backup", **patterns).returncode == 0
    (recorded,) = state.documents[COUNTS].values()
    assert cast("dict[str, object]", recorded)["counts"] == [
        {"index": name, "documents": state.indices[name]} for name in crm
    ]
    live = dict(state.indices)

    done = _run(tmp_path, port, "restore-check", **patterns)

    assert done.returncode == 0, done.stderr
    assert state.restored_indices() == [COUNTS, ",".join(crm)]
    counted = [call for call in state.requests("GET") if call.endswith("/_count")]
    assert counted[-82:] == [f"GET /{COPY}{name}/_count" for name in crm]
    assert state.requests("DELETE")[-83:] == [
        f"DELETE /{COPY}{name}" for name in [COUNTS, *crm]
    ]
    assert state.indices == live
    assert "restored 82 indices" in done.stdout


# proves: S-012-10
def test_s012_10_restore_check_fails_on_a_mismatch_in_any_matching_index(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    patterns = {"restoreCheckIndices": ["crm-*"]}
    assert _run(tmp_path, port, "backup", **patterns).returncode == 0
    state.restored_counts = {"crm-2": 6}

    done = _run(tmp_path, port, "restore-check", **patterns)

    assert done.returncode == 1
    assert "crm-2: the snapshot recorded 7 documents" in done.stderr
    assert "crm-1:" not in done.stderr
    assert state.requests("DELETE") == [
        f"DELETE /{COPY}{name}" for name in (COUNTS, "crm-1", "crm-2")
    ]
    assert "billing-1" in state.indices


# proves: S-012-10
def test_s012_10_a_count_mismatch_fails_and_still_deletes_the_copy(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    assert _run(tmp_path, port, "backup").returncode == 0
    state.restored_counts = {"crm-1": 2}
    live = dict(state.indices)

    done = _run(tmp_path, port, "restore-check")

    assert done.returncode == 1
    assert (
        "crm-1: the snapshot recorded 3 documents, the restore brought back 2"
        in done.stderr
    )
    assert state.requests("DELETE") == [
        f"DELETE /{COPY}{COUNTS}",
        f"DELETE /{COPY}crm-1",
    ]
    assert state.indices == live


# proves: S-012-10
def test_s012_10_a_failed_restore_deletes_only_the_copies_it_restored(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    assert _run(tmp_path, port, "backup").returncode == 0
    state.failing_restores = {"crm-1"}
    live = dict(state.indices)

    done = _run(tmp_path, port, "restore-check")

    assert done.returncode == 1
    assert "failed 1 of 1 shards" in done.stderr
    assert state.requests("DELETE") == [
        f"DELETE /{COPY}{COUNTS}",
        f"DELETE /{COPY}crm-1",
    ]
    assert state.indices == live


# proves: S-012-10
def test_s012_10_restore_check_never_touches_an_index_named_as_its_copy(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    assert _run(tmp_path, port, "backup").returncode == 0
    state.indices["cloudfall-restore-check-crm-1"] = 99
    before = dict(state.indices)

    done = _run(tmp_path, port, "restore-check")

    assert done.returncode == 1
    assert "cloudfall-restore-check-crm-1 already exist" in done.stderr
    # Only the counts were restored; only their copy is deleted.
    assert state.restored_indices() == [COUNTS]
    assert state.requests("DELETE") == [f"DELETE /{COPY}{COUNTS}"]
    assert state.indices == before


# proves: S-012-10
def test_s012_10_an_empty_repository_fails_and_says_no_snapshot_exists(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    # Only snapshots Cloudfall did not take of this service, or did not finish.
    state.snapshot("nightly-1", age_days=1, service=None)
    state.snapshot("cloudfall-20261101-000000", age_days=1, service="search-other")
    state.snapshot("cloudfall-20261102-000000", age_days=1, state="PARTIAL")

    done = _run(tmp_path, port, "restore-check")

    assert done.returncode == 1
    assert "no snapshot exists in repository cloudfall" in done.stderr
    assert "run cloudfall backup run search-main first" in done.stderr
    assert state.requests() == [
        "POST /_snapshot/cloudfall/_verify",
        "GET /_snapshot/cloudfall/cloudfall-*",
    ]


# proves: S-012-10
def test_s012_10_restore_check_fails_on_an_unregistered_repository(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    state.registered = False

    done = _run(tmp_path, port, "restore-check")

    assert done.returncode == 1
    assert "POST /_snapshot/cloudfall/_verify answered 404" in done.stderr


# proves: S-012-10
def test_s012_10_restore_check_refuses_a_restore_past_the_low_watermark(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    assert _run(tmp_path, port, "backup").returncode == 0
    # 85% of 100 GiB: 15 GiB must stay free; 4 000 bytes would cross it.
    state.available = 15 * GIB + 3_999

    done = _run(tmp_path, port, "restore-check")

    assert done.returncode == 1
    assert "past the low disk watermark (85%)" in done.stderr
    assert state.restored_indices() == [COUNTS]
    assert state.requests("DELETE") == [f"DELETE /{COPY}{COUNTS}"]

    state.available = 15 * GIB + 4_000
    state.watermark = "500mb"
    assert _run(tmp_path, port, "restore-check").returncode == 0


# proves: S-012-10
def test_s012_10_a_snapshot_without_its_counts_fails_the_drill(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    """A snapshot Cloudfall took holds the counts index; without it, no drill."""
    state, port = fake
    state.snapshot("cloudfall-20261113-032000", age_days=1)

    done = _run(tmp_path, port, "restore-check")

    assert done.returncode == 1
    assert f"does not hold {COUNTS}" in done.stderr
    assert state.restored_indices() == []


# proves: S-012-11
def test_s012_11_snapshots_older_than_retention_are_deleted_and_the_newest_kept(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    kept = "cloudfall-20261111-032000"
    old = ["cloudfall-20261001-032000", "cloudfall-20261030-032000"]
    state.indices[COUNTS] = 3
    state.documents[COUNTS] = {name: {"snapshot": name} for name in [*old, kept]}
    state.snapshot(old[0], age_days=44)
    state.snapshot(old[1], age_days=15)
    state.snapshot(kept, age_days=3)
    state.snapshot("cloudfall-20261020-000000", age_days=25, state="IN_PROGRESS")
    # Not Cloudfall's of this service: never touched, however old.
    state.snapshot("manual-before-upgrade", age_days=90, service=None)
    state.snapshot("cloudfall-20260901-000000", age_days=74, service="search-other")
    state.snapshot("cloudfall-20260902-000000", age_days=73, taken_by="someone")

    done = _run(tmp_path, port, "backup")

    assert done.returncode == 0, done.stderr
    assert state.requests("DELETE") == [
        f"DELETE /_snapshot/cloudfall/{old[0]},{old[1]}",
        f"DELETE /{COUNTS}/_doc/{old[0]}",
        f"DELETE /{COUNTS}/_doc/{old[1]}",
    ]
    taken = state.names()[-1]
    # The counts of the deleted snapshots go with them.
    assert sorted(state.documents[COUNTS]) == [kept, taken]
    assert state.names() == [
        "cloudfall-20261111-032000",
        "cloudfall-20261020-000000",
        "manual-before-upgrade",
        "cloudfall-20260901-000000",
        "cloudfall-20260902-000000",
        taken,
    ]
    assert "retention 14 days: deleted 2 older snapshots" in done.stdout


# proves: S-012-11
def test_s012_11_the_newest_snapshot_is_kept_when_every_snapshot_is_older(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    """A node whose clock is behind reports even the new snapshot as old."""
    state, port = fake
    state.new_snapshot_age_days = 30
    state.snapshot("cloudfall-20260901-032000", age_days=60)
    state.snapshot("cloudfall-20260915-032000", age_days=45)

    done = _run(tmp_path, port, "backup")

    assert done.returncode == 0, done.stderr
    (taken,) = state.names()
    assert taken.startswith("cloudfall-2")
    assert "kept 1" in done.stdout


# proves: S-012-11
def test_s012_11_nothing_is_deleted_inside_the_retention(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    state.snapshot("cloudfall-20261113-032000", age_days=1)

    done = _run(tmp_path, port, "backup")

    assert done.returncode == 0, done.stderr
    assert state.requests("DELETE") == []
    assert "no older snapshot to delete, kept 2" in done.stdout


# proves: S-012-11
def test_s012_11_many_old_snapshots_are_deleted_within_the_request_line(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    """Hourly snapshots past a long gap: more names than one request line holds."""
    state, port = fake
    old = [
        f"cloudfall-202609{day:02d}-{hour:02d}0000"
        for day in range(1, 11)
        for hour in range(24)
    ]
    for name in old:
        state.snapshot(name, age_days=40)

    done = _run(tmp_path, port, "backup")

    assert done.returncode == 0, done.stderr
    (taken,) = state.names()
    assert taken not in old
    assert f"deleted {len(old)} older snapshots" in done.stdout


def _backup_playbook(
    tmp_path: Path, port: int, action: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run ``backup.yml`` on localhost, its scripts rendered for the fake node."""
    scripts = tmp_path / "sbin"
    scripts.mkdir(exist_ok=True)
    for name in ("backup", "restore-check"):
        shutil.copy(_script(tmp_path, port, name), scripts)
    playbooks = tmp_path / "playbooks"
    if not playbooks.exists():
        shutil.copytree(PLAYBOOKS, playbooks)
        backup = playbooks / "backup.yml"
        text = backup.read_text(encoding="utf-8")
        assert text.count("/usr/local/sbin/") == 1
        backup.write_text(text.replace("/usr/local/sbin/", f"{scripts}/"), "utf-8")
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "all": {
                    "children": {
                        "cloudfall_servers": {
                            "hosts": {
                                "h1": {
                                    "ansible_connection": "local",
                                    "ansible_become": False,
                                    "ansible_host": "localhost",
                                    "ansible_python_interpreter": sys.executable,
                                    "cloudfall_server_id": "h1",
                                    "cloudfall_services": [_service(port)],
                                }
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    receipts = tmp_path / "receipts"
    binary = shutil.which("ansible-playbook")
    assert binary is not None
    done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [
            binary,
            "--inventory",
            str(inventory),
            "--extra-vars",
            json.dumps(
                {
                    "cloudfall_backup_service": "search-main",
                    "cloudfall_backup_action": action,
                    "cloudfall_backup_receipt_directory": str(receipts),
                }
            ),
            str(playbooks / "backup.yml"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "ANSIBLE_STDOUT_CALLBACK": "default"},
    )
    return done, receipts / f"search-main-{action}.json"


# proves: S-012-10
def test_s012_10_backup_run_and_verify_write_receipts_without_the_password(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake

    for action, summary in (
        ("backup", "of search-main: SUCCESS, 5 indices"),
        ("restore-check", "restore check passed for search-main"),
    ):
        done, path = _backup_playbook(tmp_path, port, action)
        output = done.stdout + done.stderr

        assert done.returncode == 0, output
        receipt = json.loads(path.read_text(encoding="utf-8"))
        SchemaCatalog(SCHEMAS).validate_named("backup-receipt.schema.json", receipt)
        spec = receipt["spec"]
        assert (spec["service"], spec["server"], spec["action"]) == (
            "search-main",
            "h1",
            action,
        )
        assert summary in spec["summary"]
        assert PASSWORD not in output
        assert PASSWORD not in path.read_text(encoding="utf-8")
    assert len(state.snapshots) == 1
    assert "cloudfall-restore-check-crm-1" not in state.indices


# proves: S-012-10
def test_s012_10_a_failed_snapshot_writes_no_receipt(
    tmp_path: Path, fake: tuple[FakeElasticsearch, int]
) -> None:
    state, port = fake
    state.outcome = "PARTIAL"

    done, path = _backup_playbook(tmp_path, port, "backup")

    assert done.returncode != 0
    assert not path.exists()


def test_backup_yml_runs_elasticsearch_scripts_as_the_elasticsearch_user() -> None:
    (_, play) = yaml.safe_load((PLAYBOOKS / "backup.yml").read_text("utf-8"))
    task = next(
        task
        for task in play["tasks"]
        if task["name"] == "Execute the requested backup action as the service user"
    )

    def user(kind: str) -> object:
        variables = {"cloudfall_backup_item": {"serviceKind": kind}}
        templar = Templar(loader=DataLoader(), variables=variables)
        return templar.template(trust_as_template(task["become_user"]))

    assert [user(kind) for kind in ("postgresql", "redis", "elasticsearch")] == [
        "postgres",
        "redis",
        "elasticsearch",
    ]


def _tasks() -> list[dict[str, object]]:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "snapshots.yml"))
    assert isinstance(tasks, list)
    return tasks


def _task(name: str) -> dict[str, object]:
    return next(task for task in _tasks() if task["name"] == name)


def _value(expression: str, variables: dict[str, object]) -> object:
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(expression))


def test_the_converge_imports_the_snapshot_tasks_last() -> None:
    main = DataLoader().load_from_file(str(ROLE / "tasks" / "main.yml"))
    assert isinstance(main, list)

    assert main[-1]["ansible.builtin.import_tasks"] == "snapshots.yml"


def test_the_filesystem_repository_is_in_path_repo_and_registered_once() -> None:
    service = _service(9200)
    variables = _variables(service, cloudfall_elasticsearch_bootstrap=False)
    rendered = yaml.safe_load(_render("elasticsearch.yml.j2", variables))
    s3 = {
        "type": "s3",
        "bucket": "search-snapshots",
        "endpoint": "s3.example.com",
        "credentialsRef": {"environment": "production", "path": "/x", "key": "S3"},
    }
    with_s3 = yaml.safe_load(
        _render(
            "elasticsearch.yml.j2",
            _variables(
                _service(9200, snapshotRepository=s3),
                cloudfall_elasticsearch_bootstrap=False,
            ),
        )
    )

    assert rendered["path.repo"] == ["/var/backups/cloudfall/search-main"]
    assert "path.repo" not in with_s3
    register = _task("Register the filesystem snapshot repository")
    uri = cast("dict[str, object]", register["ansible.builtin.uri"])
    assert _value(str(uri["url"]), variables) == (
        "http://127.0.0.1:9200/_snapshot/cloudfall"
    )
    assert uri["body"] == {
        "type": "fs",
        "settings": {
            "location": "{{ cloudfall_elasticsearch_service.backup.directory }}"
        },
    }
    condition = cast("list[str]", register["when"])[1]

    def registers(answer: dict[str, object]) -> bool:
        scoped = {**variables, "cloudfall_elasticsearch_registered_repository": answer}
        templar = Templar(loader=DataLoader(), variables=scoped)
        return bool(templar.evaluate_conditional(trust_as_template(condition)))

    location = "/var/backups/cloudfall/search-main"
    same = {"type": "fs", "settings": {"location": location}}
    moved = {"type": "fs", "settings": {"location": "/srv/elsewhere"}}
    assert registers({"status": 404})
    assert not registers({"status": 200, "json": {"cloudfall": same}})
    assert registers({"status": 200, "json": {"cloudfall": moved}})
    refuse = _task("Refuse a snapshot repository this release does not converge")
    module = cast("dict[str, object]", refuse["ansible.builtin.assert"])
    assert "S3 repository registration" in str(module["fail_msg"])
    assert "not implemented" in str(module["fail_msg"])
    conditions = cast("list[str]", module["that"])
    declarations = ((service, True), (_service(9200, snapshotRepository=s3), False))
    for declared, holds in declarations:
        scoped = _variables(declared)
        templar = Templar(loader=DataLoader(), variables=scoped)
        assert all(
            templar.evaluate_conditional(trust_as_template(condition))
            for condition in conditions
        ) is holds


def test_every_snapshot_task_that_sees_the_password_is_no_log() -> None:
    seeing = [
        task["name"]
        for task in _tasks()
        if "cloudfall_elasticsearch_account_files." in str(task)
    ]

    assert seeing == [
        "Read the registered snapshot repository",
        "Register the filesystem snapshot repository",
    ]
    assert all(_task(str(name)).get("no_log") is True for name in seeing)


def test_the_timers_run_the_snapshot_and_the_declared_drill() -> None:
    service = _service(9200)
    without_drill = _service(9200)
    backup = cast("dict[str, object]", without_drill["backup"])
    del backup["restoreCheckOnCalendar"]

    jobs = _value("{{ cloudfall_elasticsearch_snapshot_jobs }}", _variables(service))
    assert jobs == [
        {"action": "backup", "calendar": "*-*-* 03:20:00 UTC"},
        {"action": "restore-check", "calendar": "Sun *-*-* 05:00:00 UTC"},
    ]
    assert _value(
        "{{ cloudfall_elasticsearch_snapshot_jobs }}", _variables(without_drill)
    ) == [{"action": "backup", "calendar": "*-*-* 03:20:00 UTC"}]
    timer = _render(
        "snapshots.timer.j2",
        _variables(
            service,
            cloudfall_elasticsearch_snapshot_action="backup",
            cloudfall_elasticsearch_snapshot_calendar="*-*-* 03:20:00 UTC",
        ),
    )
    assert "OnCalendar=*-*-* 03:20:00 UTC" in timer.splitlines()


def test_the_units_run_sandboxed_as_the_elasticsearch_user() -> None:
    unit = _render(
        "snapshots.service.j2",
        _variables(
            _service(9200), cloudfall_elasticsearch_snapshot_action="restore-check"
        ),
    ).splitlines()

    for line in (
        "User=elasticsearch",
        "ExecStart=/usr/local/sbin/cloudfall-elasticsearch-restore-check",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "ProtectProc=invisible",
        "CapabilityBoundingSet=",
        "IPAddressDeny=any",
        "IPAddressAllow=localhost",
        "SystemCallFilter=@system-service",
    ):
        assert line in unit
