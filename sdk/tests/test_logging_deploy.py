"""S-011: deploy the LoggingStack with what Cloudfall provides for it."""

from __future__ import annotations

import datetime
import grp
import json
import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
CLOUDFALL = Path(sys.executable).with_name("cloudfall")
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
DELIVER_TASKS = (
    ROOT / "engine/ansible/roles/cloudfall_logging_backend/tasks/deliver.yml"
)
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
'''Stand in for ansible-playbook.

logging-requests.yml: answer each certificate request with a fresh CSR, as a
host would. logging.yml: record each staged file as the run sees it.
'''
import json
import os
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("ansible-playbook [core 2.21.0]")
    sys.exit(0)
extra = json.loads(args[args.index("--extra-vars") + 1])
if args[-1].endswith("logging-volumes.yml"):
    root = extra["cloudfall_volume_report_directory"]
    for server, items in extra["cloudfall_volume_requests"].items():
        def report(rows):
            return json.dumps({{"report": [rows]}})
        pvs = [
            {{"pv_name": device, "vg_name": "vg_data", "pv_free": str(100 * 1024**3)}}
            for device in ("/dev/nvme0n1p5", "/dev/nvme1n1p5")
        ]
        group = os.environ.get("CLOUDFALL_TEST_VG", "vg_data")
        content = {{
            "vgs": report({{"vg": [{{"vg_name": group}}]}}),
            "pvs": report({{"pv": pvs}}),
            "lvs": report({{"lv": []}}),
            "filesystems": {{item["logicalVolume"]: None for item in items}},
            "paths": {{
                item["path"]: {{"mountedFrom": None, "empty": True}} for item in items
            }},
        }}
        with open(os.path.join(root, server + ".json"), "w") as out:
            json.dump(content, out)
    sys.exit(0)
if args[-1].endswith("logging-requests.yml"):
    open(os.environ["CLOUDFALL_TEST_RECORD"] + ".requests", "w").close()
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    root = extra["cloudfall_logging_request_directory"]
    for server, requests in extra["cloudfall_logging_certificate_requests"].items():
        os.makedirs(os.path.join(root, server), exist_ok=True)
        for item in requests:
            key = ec.generate_private_key(ec.SECP256R1())
            common = x509.NameAttribute(NameOID.COMMON_NAME, item["commonName"])
            name = x509.Name([common])
            csr = x509.CertificateSigningRequestBuilder().subject_name(name).sign(
                key, hashes.SHA256()
            )
            report = {{
                "request": csr.public_bytes(serialization.Encoding.PEM).decode(),
                "certificate": None,
            }}
            with open(os.path.join(root, server, item["name"] + ".json"), "w") as out:
                json.dump(report, out)
    sys.exit(0)
seen = {{}}
for server, files in extra.get("cloudfall_logging_delivered_files", {{}}).items():
    for item in files:
        with open(item["source"], encoding="utf-8") as handle:
            seen[server + ":" + item["path"]] = {{
                **item,
                "value": handle.read(),
                "staged_mode": oct(os.stat(item["source"]).st_mode & 0o777),
            }}
with open(os.environ["CLOUDFALL_TEST_RECORD"], "w", encoding="utf-8") as handle:
    json.dump(
        {{
            "playbook": args[-1],
            "files": seen,
            "volumes": extra.get("cloudfall_volume_actions", {{}}),
        }},
        handle,
    )
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
    secrets_dir = "/etc/cloudfall/logging/"
    assert set(files) == {
        f"h1:{secrets_dir}grafana-admin-password",
        f"h1:{secrets_dir}smtp-password",
        f"h1:{secrets_dir}server.crt",
        f"h1:{secrets_dir}client-ca.crt",
        f"h1:{secrets_dir}client.crt",
        f"h1:{secrets_dir}ca.crt",
        f"h2:{secrets_dir}client.crt",
        f"h2:{secrets_dir}ca.crt",
    }
    smtp = files[f"h1:{secrets_dir}smtp-password"]
    assert (smtp["value"], smtp["mode"], smtp["force"], smtp["role"]) == (
        SMTP_VALUE,
        "0600",
        False,
        "backend",
    )
    grafana = files[f"h1:{secrets_dir}grafana-admin-password"]
    assert len(grafana["value"]) == 43
    gateway = files[f"h1:{secrets_dir}server.crt"]
    assert (gateway["role"], gateway["mode"], gateway["force"]) == (
        "backend",
        "0644",
        True,
    )
    assert files[f"h2:{secrets_dir}client.crt"]["role"] == "collector"
    ca = files[f"h2:{secrets_dir}ca.crt"]["value"]
    assert files[f"h1:{secrets_dir}client-ca.crt"]["value"] == ca
    certificate = x509.load_pem_x509_certificate(gateway["value"].encode())
    certificate.verify_directly_issued_by(x509.load_pem_x509_certificate(ca.encode()))
    assert all(item["staged_mode"] == "0o600" for item in files.values())
    assert all(not Path(item["source"]).exists() for item in files.values())
    assert data["ca_created"] is True
    volumes = record["volumes"]["h1"]
    assert [(item["logicalVolume"], item["action"]) for item in volumes] == [
        ("lv_loki", "create"),
        ("lv_prometheus", "create"),
    ]
    planned = data["volumes"]
    assert isinstance(planned, list)
    assert [item["logical_volume"] for item in planned] == [
        "lv_loki",
        "lv_prometheus",
    ]
    states = data["certificates"]
    assert isinstance(states, list)
    assert {(item["server"], item["name"], item["state"]) for item in states} == {
        ("h1", "logging-gateway", "will-create"),
        ("h1", "logging-collector", "will-create"),
        ("h2", "logging-collector", "will-create"),
    }
    delivered = data["delivered"]
    assert isinstance(delivered, list)
    assert len(delivered) == len(files)
    assert grafana["value"] not in json.dumps(envelope)
    assert "PRIVATE KEY" not in json.dumps(envelope)


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
        {
            "role": "backend",
            "path": str(host / name / "secret"),
            "source": str(sources[name]),
            "mode": "0600",
            "force": False,
        }
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
                        "cloudfall_logging_delivered_files": {"h1": files},
                        "cloudfall_logging_backend_file_owner": pwd.getpwuid(
                            os.getuid()
                        ).pw_name,
                        "cloudfall_logging_backend_file_group": grp.getgrgid(
                            os.getgid()
                        ).gr_name,
                    },
                    "tasks": [
                        {"ansible.builtin.include_tasks": str(DELIVER_TASKS)},
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


def test_the_role_replaces_a_certificate_signed_in_this_run(tmp_path: Path) -> None:
    held = tmp_path / "host" / "server.crt"
    held.parent.mkdir()
    held.write_text("old certificate", encoding="utf-8")
    source = tmp_path / "signed"
    source.write_text("new certificate", encoding="utf-8")
    source.chmod(0o600)
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        json.dumps(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "cloudfall_server_id": "h1",
                        "cloudfall_logging_delivered_files": {
                            "h1": [
                                {
                                    "role": "backend",
                                    "path": str(held),
                                    "source": str(source),
                                    "mode": "0644",
                                    "force": True,
                                },
                                {
                                    "role": "collector",
                                    "path": str(tmp_path / "host" / "other.crt"),
                                    "source": str(source),
                                    "mode": "0644",
                                    "force": True,
                                },
                            ]
                        },
                        "cloudfall_logging_backend_file_owner": pwd.getpwuid(
                            os.getuid()
                        ).pw_name,
                        "cloudfall_logging_backend_file_group": grp.getgrgid(
                            os.getgid()
                        ).gr_name,
                    },
                    "tasks": [{"ansible.builtin.include_tasks": str(DELIVER_TASKS)}],
                }
            ]
        ),
        encoding="utf-8",
    )

    subprocess.run(  # noqa: S603 - the venv's ansible-playbook
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

    assert held.read_text(encoding="utf-8") == "new certificate"
    assert held.stat().st_mode & 0o777 == 0o644
    assert not (tmp_path / "host" / "other.crt").exists()


def test_hosts_make_their_own_keys_and_hand_over_requests_only(
    tmp_path: Path,
) -> None:
    host = tmp_path / "host"
    host.mkdir()
    reports = tmp_path / "reports"
    held_key = host / "held.key"
    key = ec.generate_private_key(ec.SECP256R1())
    held_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    held_key.chmod(0o600)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "held")])
    now = datetime.datetime.now(datetime.UTC)
    held_certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    (host / "held.crt").write_bytes(
        held_certificate.public_bytes(serialization.Encoding.PEM)
    )
    before = held_key.read_bytes()
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "all": {
                    "children": {
                        "cloudfall_logging_backends": {
                            "hosts": {
                                "h1": {
                                    "ansible_connection": "local",
                                    "ansible_become": False,
                                    "ansible_python_interpreter": sys.executable,
                                    "cloudfall_server_id": "h1",
                                }
                            }
                        },
                        "cloudfall_logging_collectors": {"hosts": {"h1": {}}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    extra = {
        "cloudfall_logging_key_owner": pwd.getpwuid(os.getuid()).pw_name,
        "cloudfall_logging_key_group": grp.getgrgid(os.getgid()).gr_name,
        "cloudfall_logging_request_directory": str(reports),
        "cloudfall_logging_certificate_requests": {
            "h1": [
                {
                    "name": "fresh",
                    "keyPath": str(host / "keys" / "fresh.key"),
                    "certificatePath": str(host / "fresh.crt"),
                    "commonName": "logs.example.internal",
                },
                {
                    "name": "held",
                    "keyPath": str(held_key),
                    "certificatePath": str(host / "held.crt"),
                    "commonName": "held",
                },
            ]
        },
    }
    done = subprocess.run(  # noqa: S603 - the venv's ansible-playbook
        [
            str(ANSIBLE_PLAYBOOK),
            "--inventory",
            str(inventory),
            "--extra-vars",
            json.dumps(extra),
            "--skip-tags",
            "packages",
            str(ROOT / "engine/ansible/playbooks/logging-requests.yml"),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )

    fresh_key = host / "keys" / "fresh.key"
    assert fresh_key.stat().st_mode & 0o777 == 0o600
    assert held_key.read_bytes() == before
    fresh = json.loads((reports / "h1" / "fresh.json").read_text(encoding="utf-8"))
    held = json.loads((reports / "h1" / "held.json").read_text(encoding="utf-8"))
    request = x509.load_pem_x509_csr(fresh["request"].encode())
    assert request.is_signature_valid
    assert fresh["certificate"] is None
    assert held["certificate"].startswith("-----BEGIN CERTIFICATE-----")
    reported = "".join(
        path.read_text(encoding="utf-8") for path in (reports / "h1").iterdir()
    )
    assert "PRIVATE KEY" not in reported + done.stdout


def test_a_refused_volume_stops_the_deploy_before_any_host_change(
    tmp_path: Path,
) -> None:
    project, environment = _project(tmp_path)
    environment["CLOUDFALL_TEST_VG"] = "vg_other"
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

    assert code == 4
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["context"]["code"] == "volume_refused"
    assert error["context"]["server"] == "h1"
    assert error["context"]["pool"] == "data"
    assert "vg_data does not exist" in error["context"]["reason"]
    assert not (tmp_path / "record.json.requests").exists()
    assert not (tmp_path / "record.json").exists()
