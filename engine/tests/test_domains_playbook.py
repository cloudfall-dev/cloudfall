"""``domains.yml`` refuses a Domain it cannot render before it touches a server.

The hosts sit on TEST-NET-1 (192.0.2.0/24), which never answers, so a run
that reached any of them would end UNREACHABLE. A refused run never gets that
far: the check needs no connection.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from cloudfall_engine.playbook import PlaybookRun, playbook_command, resolve_playbook

ENGINE = Path(__file__).parents[2] / "engine"
REFUSAL = "have no proxy.upstream"


def _domain(domain_id: str, server: str, *, upstream: bool) -> dict[str, object]:
    proxy: dict[str, object] = {
        "server": server,
        "configurationPath": f"/etc/nginx/sites-available/{domain_id}.conf",
        "service": "nginx.service",
    }
    if upstream:
        proxy["upstream"] = {"address": "127.0.0.1", "port": 8080}
    return {
        "id": domain_id,
        "primaryName": f"{domain_id}.example.test",
        "aliases": [],
        "proxy": proxy,
        "tls": {"mode": "disabled"},
    }


def _host(server: str, address: str, domains: list[dict[str, object]]) -> object:
    return {
        "ansible_host": address,
        "ansible_user": "root",
        "ansible_timeout": 1,
        "cloudfall_server_id": server,
        "cloudfall_domains": domains,
    }


def _run(tmp_path: Path, hosts: dict[str, object]) -> str:
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps({"all": {"children": {"cloudfall_servers": {"hosts": hosts}}}}),
        encoding="utf-8",
    )
    run = PlaybookRun(
        playbook=resolve_playbook("domains", ENGINE), inventory_file=inventory
    )
    command = playbook_command(run, ENGINE)
    completed = subprocess.run(  # noqa: S603 - argv built by the engine
        command.argv,
        env={
            **os.environ,
            **command.environment,
            "ANSIBLE_HOST_KEY_CHECKING": "False",
            "ANSIBLE_NOCOLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode != 0
    return completed.stdout + completed.stderr


def test_a_domain_without_upstream_stops_the_run_before_any_server(
    tmp_path: Path,
) -> None:
    """The offending Domains sit on the second server; the first is not touched."""
    output = _run(
        tmp_path,
        {
            "edge-a": _host(
                "edge-a", "192.0.2.10", [_domain("site-a", "edge-a", upstream=True)]
            ),
            "edge-b": _host(
                "edge-b",
                "192.0.2.11",
                [
                    _domain("site-b", "edge-b", upstream=True),
                    _domain("bare-one", "edge-b", upstream=False),
                    _domain("bare-two", "edge-b", upstream=False),
                ],
            ),
        },
    )

    refusal = next(line for line in output.splitlines() if REFUSAL in line)
    assert "Domains bare-one, bare-two have no proxy.upstream" in refusal
    assert "site-" not in refusal
    assert "UNREACHABLE" not in output
    assert "Gathering Facts" not in output


def test_domains_that_render_pass_the_check_and_reach_their_servers(
    tmp_path: Path,
) -> None:
    output = _run(
        tmp_path,
        {
            "edge-a": _host(
                "edge-a", "192.0.2.10", [_domain("site-a", "edge-a", upstream=True)]
            ),
            "edge-b": _host("edge-b", "192.0.2.11", []),
        },
    )

    assert REFUSAL not in output
    assert "UNREACHABLE" in output
