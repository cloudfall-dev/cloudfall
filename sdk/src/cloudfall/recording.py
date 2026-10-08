"""Recorded playbook runs, played back in place of Ansible.

A recording is a decisions directory from a real run: each decision record
names the playbook, the target pattern and the inputs it ran with, and its
``.diff`` holds what the hosts printed. Played back, a check-mode run
writes that output and those per-host results instead of touching a host,
so everything around it (validation, the decision record, the gate) works
exactly as it does live. An agent can then be tried on a real incident
again and again, with no host at all.

A call the recording does not hold fails, saying so; nothing is invented.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from cloudfall.decision import CheckRunner

NOT_RECORDED_EXIT = 3
"""The exit code of a call the recording does not hold."""

ERROR_RECORDING_EMPTY = "recording_empty"
"""The code of a recording that is not a directory or holds no runs."""

_TREE_DIR_VARIABLE = "ANSIBLE_CALLBACK_TREE_DIR"


class RecordingError(ValueError):
    """A recording that cannot be played back."""

    def __init__(self, code: str, detail: str) -> None:
        """Keep the machine-readable code beside the message."""
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RecordedRun:
    """What one check-mode run printed and reported per host."""

    exit_code: int
    output: str
    changed: tuple[str, ...]
    unchanged: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Recording:
    """Recorded check-mode runs, by playbook, target pattern and inputs."""

    directory: Path
    runs: Mapping[str, RecordedRun]

    @classmethod
    def load(cls, directory: Path) -> Recording:
        """Read every decision record in the directory; the first of a call wins.

        A path that is not a directory, or a directory holding no decision
        record, is refused: played back, it would fail every call.
        """
        if not directory.is_dir():
            message = f"the recording {directory} is not a directory"
            raise RecordingError(ERROR_RECORDING_EMPTY, message)
        runs: dict[str, RecordedRun] = {}
        for path in sorted(directory.glob("*.json")):
            document = cast(
                "Mapping[str, object]", json.loads(path.read_text(encoding="utf-8"))
            )
            if document.get("kind") != "OperationDecision":
                continue
            spec = cast("Mapping[str, Mapping[str, object]]", document["spec"])
            check = spec["check"]
            diff = cast("Mapping[str, object]", check["diff"])
            output = directory / f"{path.stem}.diff"
            key = _key(
                str(spec["operation"]["playbook"]),
                cast("str | None", spec["targets"].get("pattern")),
                spec["inputs"],
            )
            runs.setdefault(
                key,
                RecordedRun(
                    exit_code=int(cast("int", check["exitCode"])),
                    output=(
                        output.read_text(encoding="utf-8")
                        if output.is_file()
                        else f"(the recorded output {diff['path']} is missing)\n"
                    ),
                    changed=tuple(cast("Sequence[str]", check["changed"])),
                    unchanged=tuple(cast("Sequence[str]", check["unchanged"])),
                ),
            )
        if not runs:
            message = f"the recording {directory} holds no decision record"
            raise RecordingError(ERROR_RECORDING_EMPTY, message)
        return cls(directory=directory, runs=runs)

    def runner(self, repository: Path) -> CheckRunner:
        """Return a check runner that plays this recording back."""

        def run(argv: Sequence[str], environment: Mapping[str, str], diff: Path) -> int:
            playbook, pattern, inputs = _invocation(argv, repository)
            recorded = self.runs.get(_key(playbook, pattern, inputs))
            if recorded is None:
                diff.write_text(
                    f"replay: {self.directory.name} holds no run of {playbook} "
                    f"with --limit {pattern} and inputs "
                    f"{json.dumps(dict(inputs), sort_keys=True)}\n",
                    encoding="utf-8",
                )
                return NOT_RECORDED_EXIT
            diff.write_text(recorded.output, encoding="utf-8")
            _write_tree(environment, recorded)
            return recorded.exit_code

        return run


def _key(playbook: str, pattern: str | None, inputs: Mapping[str, object]) -> str:
    return json.dumps(
        {"playbook": playbook, "pattern": pattern, "inputs": dict(inputs)},
        sort_keys=True,
    )


def _invocation(
    argv: Sequence[str], repository: Path
) -> tuple[str, str | None, Mapping[str, object]]:
    """Read the playbook, the target pattern and the inputs back out of argv."""
    arguments = list(argv)
    pattern = _value(arguments, "--limit")
    extra = _value(arguments, "--extra-vars")
    inputs = cast("Mapping[str, object]", json.loads(extra)) if extra else {}
    playbook = arguments[-1]
    root = f"{repository}/"
    return playbook.removeprefix(root), pattern, inputs


def _value(arguments: Sequence[str], flag: str) -> str | None:
    if flag not in arguments:
        return None
    return arguments[arguments.index(flag) + 1]


def _write_tree(environment: Mapping[str, str], recorded: RecordedRun) -> None:
    """Leave the per-host results the tree callback would have written."""
    tree = environment.get(_TREE_DIR_VARIABLE)
    if tree is None:
        return
    directory = Path(tree)
    directory.mkdir(parents=True, exist_ok=True)
    for host in recorded.changed:
        (directory / host).write_text('{"changed": true}', encoding="utf-8")
    for host in recorded.unchanged:
        (directory / host).write_text('{"changed": false}', encoding="utf-8")
