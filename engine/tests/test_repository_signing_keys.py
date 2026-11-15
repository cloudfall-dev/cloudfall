"""A package repository's signing key is fetched only when it is missing (#388).

``get_url`` without ``force`` still asks the server whether the key changed, so
a re-converge failed whenever the key's server was slow. The roles' own key
tasks run for real on localhost, in check mode, against a URL nothing serves.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROLES = Path(__file__).parents[2] / "engine" / "ansible" / "roles"
# Port 9 (discard) on loopback: a request there fails at once.
UNSERVED = "http://127.0.0.1:9/signing-key.asc"
KEYS = [
    pytest.param(
        "cloudfall_postgresql",
        "cloudfall_postgresql_repository_key_path",
        "cloudfall_postgresql_repository_key_url",
        id="postgresql",
    ),
    pytest.param(
        "cloudfall_elasticsearch",
        "cloudfall_elasticsearch_repository_key_path",
        "cloudfall_elasticsearch_repository_key_url",
        id="elasticsearch",
    ),
    pytest.param(
        "cloudfall_logging_collector",
        "cloudfall_logging_collector_grafana_repository_key_path",
        "cloudfall_logging_collector_grafana_repository_key_url",
        id="logging-collector",
    ),
    pytest.param(
        "cloudfall_logging_backend",
        "cloudfall_logging_backend_grafana_repository_key_path",
        "cloudfall_logging_backend_grafana_repository_key_url",
        id="logging-backend",
    ),
]


def _key_tasks(role: str, path_var: str) -> list[dict[str, object]]:
    """Return the role's tasks from the key's check to its owner-and-mode task."""
    tasks = yaml.safe_load(
        (ROLES / role / "tasks" / "main.yml").read_text(encoding="utf-8")
    )
    assert isinstance(tasks, list)
    download = next(
        index
        for index, task in enumerate(tasks)
        if cast("dict[str, object]", task.get("ansible.builtin.get_url", {})).get(
            "dest"
        )
        == f"{{{{ {path_var} }}}}"
    )
    return cast("list[dict[str, object]]", tasks[download - 1 : download + 2])


def _fetches(download: dict[str, object], registered: str, *, exists: bool) -> bool:
    templar = Templar(
        loader=DataLoader(), variables={registered: {"stat": {"exists": exists}}}
    )
    return bool(templar.evaluate_conditional(trust_as_template(str(download["when"]))))


@pytest.mark.parametrize(("role", "path_var", "url_var"), KEYS)
def test_the_key_is_downloaded_only_when_missing_and_retried(
    role: str, path_var: str, url_var: str
) -> None:
    check, download, keep = _key_tasks(role, path_var)
    registered = cast("str", check["register"])

    assert cast("dict[str, str]", check["ansible.builtin.stat"])["path"] == (
        f"{{{{ {path_var} }}}}"
    )
    assert download["when"] == f"not {registered}.stat.exists"
    assert _fetches(download, registered, exists=False)
    assert not _fetches(download, registered, exists=True)
    assert download["until"] == f"{download['register']} is succeeded"
    assert cast("int", download["retries"]) >= 3
    assert cast("int", download["delay"]) > 0
    get_url = cast("dict[str, str]", download["ansible.builtin.get_url"])
    assert get_url["url"] == f"{{{{ {url_var} }}}}"
    kept = cast("dict[str, str]", keep["ansible.builtin.file"])
    assert keep["when"] == f"{registered}.stat.exists"
    assert kept["path"] == get_url["dest"]
    assert kept["state"] == "file"
    for name in ("owner", "group", "mode"):
        assert kept[name] == get_url[name]


@pytest.mark.parametrize(("role", "path_var", "url_var"), KEYS)
def test_an_installed_key_needs_no_request(
    tmp_path: Path, role: str, path_var: str, url_var: str
) -> None:
    """The key is on disk and its URL serves nothing: the run still passes.

    The owner-and-mode task is left out: it needs a ``root`` group, which a
    developer's macOS doesn't have; the test above pins what it sets.
    """
    check, download, _keep = _key_tasks(role, path_var)
    key = tmp_path / "signing-key.asc"
    key.write_text("-----BEGIN PGP PUBLIC KEY BLOCK-----\n", encoding="utf-8")
    play = [
        {
            "name": "Install a repository signing key that is already there",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {path_var: str(key), url_var: UNSERVED},
            "tasks": [check, download],
        }
    ]
    playbook = tmp_path / "signing-key.yml"
    playbook.write_text(yaml.safe_dump(play), encoding="utf-8")
    binary = shutil.which("ansible-playbook")
    assert binary is not None

    done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [binary, "--check", "--inventory", "localhost,", str(playbook)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    output = done.stdout + done.stderr
    assert done.returncode == 0, output
    assert UNSERVED not in output
    assert re.search(r"skipping: \[localhost\]", output), output
    assert re.search(
        r"localhost\s+: ok=\d+\s+changed=\d+\s+unreachable=0\s+failed=0", output
    )
