"""S-011 secrets provider: requirement secrets kept sops-encrypted in the project."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from cloudfall.domain import AbsolutePath, ResourceId, SecretKeyName
from cloudfall.inventory import SecretValueReference
from cloudfall.requirements import (
    RequirementName,
    RequirementState,
    SecretRequirement,
    SecretSource,
)
from cloudfall.secret_store import (
    ERROR_KEY_EXISTS,
    ERROR_VALUE_INVALID,
    GENERATED_BYTES,
    SecretStore,
)
from cloudfall.secrets import SecretsError, parse_dotenv

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
CLOUDFALL = Path(sys.executable).with_name("cloudfall")
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
    sys.stdout.write(sys.stdin.read() + "sops_mac=fake\\nsops_version=3.13.3\\n")
else:
    sys.exit(f"unexpected sops call: {{args}}")
"""
REFERENCE = SecretValueReference(
    environment=ResourceId("production"),
    path=AbsolutePath("/logging-stacks/operations"),
    key=SecretKeyName("GRAFANA_ADMIN_PASSWORD"),
)


def _decrypt(source: Path) -> str:
    """Read the fake format: every line that is not sops metadata."""
    return "".join(
        f"{line}\n"
        for line in source.read_text(encoding="utf-8").splitlines()
        if not line.startswith("sops_")
    )


def _encrypt(_target: Path, plaintext: str) -> str:
    return plaintext + "sops_mac=fake\n"


def _refuse_decrypt(source: Path) -> str:
    message = f"decrypted {source}"
    raise AssertionError(message)


def _store(tmp_path: Path) -> SecretStore:
    return SecretStore(tmp_path / "secrets", decrypt=_decrypt, encrypt=_encrypt)


def _secret(source: SecretSource) -> SecretRequirement:
    return SecretRequirement(
        RequirementName("grafana-admin-password"),
        source,
        AbsolutePath("/etc/cloudfall/logging/grafana-admin-password"),
        REFERENCE,
    )


def _stored(store: SecretStore) -> dict[str, str]:
    source = store.source_path(REFERENCE)
    return dict(parse_dotenv(_decrypt(source), source))


def test_a_missing_secret_is_created_or_asked_for_by_its_source(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    assert store.state(_secret(SecretSource.GENERATE)) is RequirementState.WILL_CREATE
    assert store.state(_secret(SecretSource.INPUT)) is RequirementState.NEEDS_INPUT


def test_a_stored_secret_is_met_from_key_names_alone(tmp_path: Path) -> None:
    _store(tmp_path).write(REFERENCE, "synthetic-value")
    reader = SecretStore(
        tmp_path / "secrets", decrypt=_refuse_decrypt, encrypt=_encrypt
    )

    assert reader.state(_secret(SecretSource.INPUT)) is RequirementState.MET


def test_writing_adds_a_key_beside_the_stored_ones(tmp_path: Path) -> None:
    store = _store(tmp_path)
    other = SecretValueReference(
        REFERENCE.environment, REFERENCE.path, SecretKeyName("SMTP_PASSWORD")
    )

    store.write(other, "first")
    stored = store.write(REFERENCE, "synthetic-value")

    assert _stored(store) == {
        "SMTP_PASSWORD": "first",
        "GRAFANA_ADMIN_PASSWORD": "synthetic-value",
    }
    assert store.source_path(REFERENCE) == (
        tmp_path / "secrets" / "production" / "logging-stacks" / "operations.env"
    )
    assert stored.as_dict() == {
        "reference": REFERENCE.as_dict(),
        "key": "GRAFANA_ADMIN_PASSWORD",
        "sha256": hashlib.sha256(b"synthetic-value").hexdigest(),
    }
    assert "synthetic-value" not in json.dumps(stored.as_dict())


def test_a_stored_key_is_never_overwritten(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(REFERENCE, "synthetic-value")
    before = store.source_path(REFERENCE).read_bytes()

    with pytest.raises(SecretsError) as error:
        store.write(REFERENCE, "another-value")

    assert error.value.code == ERROR_KEY_EXISTS
    assert "another-value" not in error.value.message
    assert store.source_path(REFERENCE).read_bytes() == before


@pytest.mark.parametrize("value", ["", " padded", "two\nlines", "tail\r"])
def test_a_value_must_be_one_clean_line(tmp_path: Path, value: str) -> None:
    store = _store(tmp_path)

    with pytest.raises(SecretsError) as error:
        store.write(REFERENCE, value)

    assert error.value.code == ERROR_VALUE_INVALID
    assert not store.source_path(REFERENCE).exists()


def test_a_generated_value_is_random_and_url_safe(tmp_path: Path) -> None:
    first = _store(tmp_path / "a")
    second = _store(tmp_path / "b")

    first.generate(REFERENCE)
    second.generate(REFERENCE)

    one = _stored(first)["GRAFANA_ADMIN_PASSWORD"]
    two = _stored(second)["GRAFANA_ADMIN_PASSWORD"]
    assert one != two
    assert len(one) == len(two) == 43
    assert GENERATED_BYTES == 32
    assert set(one) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )


def _project(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    project = tmp_path / "project"
    shutil.copytree(EXAMPLES, project)
    (project / ".sops.yaml").write_text(
        "creation_rules:\n  - path_regex: secrets/.*\\.env$\n    age: age1example\n",
        encoding="utf-8",
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    sops = tools / "sops"
    sops.write_text(FAKE_SOPS, encoding="utf-8")
    sops.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "CLOUDFALL_TEST_SMTP": "synthetic-smtp-value",
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
        timeout=120,
    )
    assert "synthetic-smtp-value" not in done.stdout + done.stderr
    return done.returncode, json.loads(done.stdout)


def _data(envelope: dict[str, object]) -> dict[str, object]:
    data = envelope["data"]
    assert isinstance(data, dict)
    return data


def test_the_cli_generates_then_takes_input_until_nothing_is_missing(
    tmp_path: Path,
) -> None:
    project, environment = _project(tmp_path)

    code, before = _cloudfall(project, environment, "requirements", "show")
    assert code == 0
    assert [item["name"] for item in _data(before)["missing"]] == [  # type: ignore[attr-defined]
        "smtp-password-operations-email"
    ]

    code, generated = _cloudfall(project, environment, "secrets", "generate")
    assert code == 0
    assert [item["key"] for item in _data(generated)["written"]] == [  # type: ignore[attr-defined]
        "GRAFANA_ADMIN_PASSWORD"
    ]
    code, again = _cloudfall(project, environment, "secrets", "generate")
    assert code == 0
    assert _data(again)["written"] == []
    assert _data(again)["effect"] == "noop"

    code, typed = _cloudfall(
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
    written = _data(typed)["written"]
    assert isinstance(written, list)
    assert written[0]["key"] == "SMTP_PASSWORD_OPERATIONS_EMAIL"

    code, after = _cloudfall(project, environment, "requirements", "show")
    assert code == 0
    assert _data(after)["missing"] == []
    stored = (
        project / "secrets" / "production" / "logging-stacks" / "operations.env"
    ).read_text(encoding="utf-8")
    assert "SMTP_PASSWORD_OPERATIONS_EMAIL=synthetic-smtp-value\n" in stored


def test_input_off_a_terminal_without_a_value_needs_a_person(
    tmp_path: Path,
) -> None:
    project, environment = _project(tmp_path)

    code, envelope = _cloudfall(
        project,
        environment,
        "secrets",
        "input",
        "smtp-password-operations-email",
        "--owner",
        "operations",
    )

    assert code == 4
    error = envelope["error"]
    assert isinstance(error, dict)
    assert error["code"] == "INPUT_REQUIRED"
    assert not (project / "secrets" / "production").exists()


@pytest.mark.parametrize(
    ("name", "code", "error_code"),
    [
        ("smtp-password-nobody", 5, "secrets_requirement_unknown"),
        ("grafana-admin-password", 4, "secrets_not_input"),
    ],
)
def test_input_names_one_declared_input_secret(
    tmp_path: Path, name: str, code: int, error_code: str
) -> None:
    project, environment = _project(tmp_path)

    exit_code, envelope = _cloudfall(
        project,
        environment,
        "secrets",
        "input",
        name,
        "--owner",
        "operations",
        "--value-from-env",
        "CLOUDFALL_TEST_SMTP",
    )

    error = envelope["error"]
    assert isinstance(error, dict)
    assert (exit_code, error["context"]["code"]) == (code, error_code)
