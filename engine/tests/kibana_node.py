"""Stand-ins for the two tools the ``cloudfall_kibana`` role runs on a node.

``kibana-keystore`` keeps Kibana's keystore as a JSON file in
``$KBN_PATH_CONF``; ``elasticsearch-service-tokens`` keeps the node's
``service_tokens`` file in ``$ES_PATH_CONF`` the way Elasticsearch does: one
``elastic/kibana/<name>:<hash>`` line a token, the token itself printed once.
Both log every argv, never what crosses their stdin, so a test can count the
calls that change the node and grep them for a secret.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_KEYSTORE = r"""
import json
import os
import sys
from pathlib import Path

LOG = Path(__LOG__)


def main(argv):
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(["kibana-keystore", *argv]) + "\n")
    path = Path(os.environ["KBN_PATH_CONF"]) / "kibana.keystore"
    command, *rest = argv
    if command == "create":
        path.write_text("{}", encoding="utf-8")
        return 0
    if not path.is_file():
        print("ERROR: Kibana keystore not found. Use 'create' command", file=sys.stderr)
        return 1
    settings = json.loads(path.read_text(encoding="utf-8"))
    if command == "list":
        for name in settings:
            print(name)
        return 0
    if command == "add":
        (name,) = [item for item in rest if not item.startswith("--")]
        if name in settings and "--force" not in rest:
            print(f"Setting {name} already exists", file=sys.stderr)
            return 1
        assert "--stdin" in rest
        value = sys.stdin.read()
        if not value:
            return 1
        settings[name] = value
        path.write_text(json.dumps(settings), encoding="utf-8")
        return 0
    if command == "remove":
        (name,) = rest
        settings.pop(name)
        path.write_text(json.dumps(settings), encoding="utf-8")
        return 0
    print(f"unknown command {command}", file=sys.stderr)
    return 64


sys.exit(main(sys.argv[1:]))
"""

_SERVICE_TOKENS = r"""
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

LOG = Path(__LOG__)


def main(argv):
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(["elasticsearch-service-tokens", *argv]) + "\n")
    path = Path(os.environ["ES_PATH_CONF"]) / "service_tokens"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    command, *rest = argv
    if command == "list":
        for line in lines:
            print(line.split(":", 1)[0])
        return 0
    account, name = rest
    prefix = f"{account}/{name}:"
    held = [line for line in lines if line.startswith(prefix)]
    if command == "create":
        if held:
            print(f"Service token [{account}/{name}] already exists", file=sys.stderr)
            return 1
        token = secrets.token_urlsafe(48)
        digest = hashlib.sha256(token.encode()).hexdigest()
        lines.append(f"{prefix}{{PBKDF2_STRETCH}}10000${digest}")
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        print(f"SERVICE_TOKEN {account}/{name} = {token}")
        return 0
    if command == "delete":
        if not held:
            print(f"Service token [{account}/{name}] does not exist", file=sys.stderr)
            return 1
        kept = [line for line in lines if not line.startswith(prefix)]
        path.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")
        return 0
    print(f"unknown command {command}", file=sys.stderr)
    return 64


sys.exit(main(sys.argv[1:]))
"""

_RESTART = r"""
import json
import sys
from pathlib import Path

with Path(__LOG__).open("a", encoding="utf-8") as log:
    log.write(json.dumps(["restart", *sys.argv[1:]]) + "\n")
"""


def write_tools(directory: Path) -> tuple[Path, Path, Path, Path]:
    """Write the keystore, token and restart stand-ins; return them and the log."""
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "calls.log"
    log.write_text("", encoding="utf-8")
    tools: list[Path] = []
    for name, script in (
        ("kibana-keystore", _KEYSTORE),
        ("elasticsearch-service-tokens", _SERVICE_TOKENS),
        ("restart-kibana", _RESTART),
    ):
        tool = directory / name
        tool.write_text(
            f"#!{sys.executable}\n" + script.replace("__LOG__", repr(str(log))),
            encoding="utf-8",
        )
        tool.chmod(0o755)
        tools.append(tool)
    keystore, service_tokens, restart = tools
    return keystore, service_tokens, restart, log


def calls(log: Path) -> list[list[str]]:
    """Return every argv the stand-ins were given, in order."""
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
