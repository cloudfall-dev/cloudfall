"""S-011: deploy the LoggingStack with the secrets Cloudfall keeps for it."""

from __future__ import annotations

import grp
import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
CLOUDFALL = Path(sys.executable).with_name("cloudfall")
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
SECRET_TASKS = ROOT / "engine/ansible/roles/cloudfall_logging_backend/tasks/secrets.yml"
SMTP_VALUE = "synthetic-smtp-value"
FAKE_SOPS = f"""#!{sys.executable}
'''Stand in for sops: "encrypts" by appending sops metadata, values stay in clear.'''
import sys

args = sys.argv[1:]
if args[0] == "--decrypt":
    with open(args[1], encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    kept = [line for line in lines if not line.startswith("sops_")]
    sys.stdout.write("".join(f"{{line}}\\n" for line in kept))
elif args[0] == "--encrypt":
    sys.stdout.write(sys.stdin.read() + "sops_mac=fake\\n")
else:
    sys.exit(f"unexpected sops call: {{args}}")
"""
FAKE_PLAYBOOK = f"""#!{sys.executable}
'''Stand in for ansible-playbook: record each staged secret file as the run sees it.'''
import json
import os
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("ansible-playbook [core 2.21.0]")
    sys.exit(0)
extra = json.loads(args[args.index("--extra-vars") + 1])
seen = {{}}
for server, files in extra.get("cloudfall_logging_secret_files", {{}}).items():
    for item in files:
        with open(item["source"], encoding="utf-8") as handle:
            seen[item["path"]] = {{
                "server": server,
                "source": item["source"],
                "value": handle.read(),
                "mode": oct(os.stat(item["source"]).st_mode & 0o777),
            }}
with open(os.environ["CLOUDFALL_TEST_RECORD"], "w", encoding="utf-8") as handle:
    json.dump({{"playbook": args[-1], "files": seen}}, handle)
"""


def _tools(tmp_path: Path) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, script in (("sops", FAKE_SOPS), ("ansible-playbook", FAKE_PLAYBOOK)):
        path = tools / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    return tools


def _project(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project, ignore=shutil.ignore_patterns("tmp"))
    (project / ".sops.yaml").write_text(
        "creation_rules:\n  - path_regex: secrets/.*\\.env$\n    age: age1example\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PATH": f"{_tools(tmp_path)}{os.pathsep}{os.environ['PATH']}",
        "CLOUDFALL_TEST_SMTP": SMTP_VALUE,
        "CLOUDFALL_TEST_RECORD": str(tmp_path / "record.json"),
    }
    return project, environment


def _cloudfall(
    project: Path, environment: dict[str, str], *argv: str
) -> tuple[int, dict[str, object]]:
    done = subprocess.run(  # noqa: S603 - the installed CLI, fixed arguments
        [str(CLOUDFALL), *argv, "--project", str(project)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        stdin=subprocess.DEVNULL,
        timeout=300,
    )
    assert SMTP_VALUE not in done.stdout + done.stderr
    return done.returncode, json.loads(done.stdout)


def _data(envelope: dict[str, object]) -> dict[str, object]:
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


def test_the_plan_names_what_it_would_generate_and_what_is_missing(
    tmp_path: Path,
) -> None:
    project, environment = _project(tmp_path)

    code, envelope = _cloudfall(project, environment, "logging", "deploy")

    assert code == 0
    data = _data(envelope)
    assert data["status"] == "plan"
    assert data["logging_stack"] == "operations"
    assert "first generate grafana-admin-password" in str(data["would_run"])
    missing = data["missing"]
    assert isinstance(missing, list)
    assert [item["name"] for item in missing] == ["smtp-password-operations-email"]
    assert not (project / "secrets" / "production" / "logging-stacks").exists()
    assert not (tmp_path / "record.json").exists()


def test_a_missing_input_stops_the_deploy_before_anything_changes(
    tmp_path: Path,
) -> None:
    project, environment = _project(tmp_path)

    code, envelope = _cloudfall(project, environment, "logging", "deploy", "--yes")

    assert code == 4
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["context"]["code"] == "INPUT_REQUIRED"
    assert error["context"]["missing"] == ["smtp-password-operations-email"]
    assert not (project / "secrets" / "production" / "logging-stacks").exists()
    assert not (tmp_path / "record.json").exists()


def test_the_deploy_generates_stages_and_removes_each_secret(
    tmp_path: Path,
) -> None:
    project, environment = _project(tmp_path)
    code, _ = _cloudfall(
        project,
        environment,
        "secrets",
        "input",
        "smtp-password-operations-email",
        "--owner",
        "operations",
        "--value-from-env",
        "CLOUDFALL_TEST_SMTP",
    )
    assert code == 0

    code, envelope = _cloudfall(project, environment, "logging", "deploy", "--yes")

    assert code == 0, envelope
    data = _data(envelope)
    generated = data["generated"]
    assert isinstance(generated, list)
    assert [item["key"] for item in generated] == ["GRAFANA_ADMIN_PASSWORD"]
    record = json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))
    assert record["playbook"].endswith("logging.yml")
    files = record["files"]
    assert set(files) == {
        "/etc/cloudfall/logging/grafana-admin-password",
        "/etc/cloudfall/logging/smtp-password",
    }
    smtp = files["/etc/cloudfall/logging/smtp-password"]
    assert (smtp["server"], smtp["value"], smtp["mode"]) == ("h1", SMTP_VALUE, "0o600")
    grafana = files["/etc/cloudfall/logging/grafana-admin-password"]
    assert len(grafana["value"]) == 43
    assert all(not Path(item["source"]).exists() for item in files.values())
    delivered = data["delivered"]
    assert isinstance(delivered, list)
    assert {item["path"] for item in delivered} == set(files)
    assert grafana["value"] not in json.dumps(envelope)


def test_the_role_delivers_a_missing_file_and_keeps_an_existing_one(
    tmp_path: Path,
) -> None:
    host = tmp_path / "host"
    (host / "kept").mkdir(parents=True)
    (host / "kept" / "secret").write_text("by hand", encoding="utf-8")
    sources = {}
    for name in ("kept", "fresh"):
        source = tmp_path / f"source-{name}"
        source.write_text(f"from cloudfall {name}", encoding="utf-8")
        source.chmod(0o600)
        sources[name] = source
    files = [
        {"path": str(host / name / "secret"), "source": str(sources[name])}
        for name in ("kept", "fresh")
    ]
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "cloudfall_server_id": "h1",
                        "cloudfall_logging_secret_files": {"h1": files},
                        "cloudfall_logging_backend_secret_owner": pwd.getpwuid(
                            os.getuid()
                        ).pw_name,
                        "cloudfall_logging_backend_secret_group": grp.getgrgid(
                            os.getgid()
                        ).gr_name,
                    },
                    "tasks": [
                        {"ansible.builtin.include_tasks": str(SECRET_TASKS)},
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    def run() -> str:
        done = subprocess.run(  # noqa: S603 - the venv's ansible-playbook
            [
                str(ANSIBLE_PLAYBOOK),
                "--inventory",
                "localhost,",
                "--connection",
                "local",
                str(playbook),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
        return done.stdout

    first = run()
    second = run()

    assert (host / "kept" / "secret").read_text(encoding="utf-8") == "by hand"
    fresh = host / "fresh" / "secret"
    assert fresh.read_text(encoding="utf-8") == "from cloudfall fresh"
    assert fresh.stat().st_mode & 0o777 == 0o600
    assert "from cloudfall" not in first
    assert "changed=0" not in first
    assert "changed=0" in second
