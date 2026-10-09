"""An agent investigates an alert through the catalog, and the record it leaves."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from cloudfall.cli import main
from cloudfall.investigation import (
    ERROR_MODEL_REFUSED,
    AgentTool,
    InvestigationError,
    InvestigationStatus,
    InvestigationStore,
    ModelEndpoint,
    ModelReply,
    ModelUnavailableError,
    StepOutcome,
    ToolResult,
    investigate,
)
from cloudfall.resources import default_schema_directory
from cloudfall.validation import SchemaCatalog
from test_catalog import _repository

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

ENDPOINT = ModelEndpoint(base_url="https://models.example/v1/", model="nemotron")
NO_ARGUMENTS: Mapping[str, object] = {"type": "object", "properties": {}}


def _call(
    name: str, arguments: Mapping[str, object], call_id: str = "c1"
) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10},
    }


def _say(text: str) -> dict[str, object]:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5},
    }


class _Script:
    """A model that answers each turn from a list, and keeps what it was sent."""

    def __init__(self, *replies: Mapping[str, object]) -> None:
        self.replies = list(replies)
        self.requests: list[Mapping[str, object]] = []

    def __call__(self, url: str, body: Mapping[str, object]) -> ModelReply:
        assert url == "https://models.example/v1/chat/completions"
        self.requests.append(json.loads(json.dumps(body)))
        return ModelReply(body=self.replies.pop(0), request_id="req-test")


class _Tool:
    """A tool that counts its calls and answers with a fixed result."""

    def __init__(self, name: str, result: ToolResult) -> None:
        self.calls = 0
        self.tool = AgentTool(
            name=name, description=name, input_schema=NO_ARGUMENTS, call=self._call
        )
        self.result = result

    def _call(self, _arguments: Mapping[str, object]) -> ToolResult:
        self.calls += 1
        return self.result


def _tools() -> tuple[_Tool, _Tool]:
    report = _Tool(
        "disk-report",
        ToolResult(StepOutcome.RAN, {"output": "/var/log/shop 36G"}, "disk-report-1"),
    )
    rotate = _Tool(
        "shop-logrotate",
        ToolResult(
            StepOutcome.PROPOSED, {"decision": "x"}, "shop-logrotate-20261008152628"
        ),
    )
    return report, rotate


def _tool_list(*tools: _Tool) -> Sequence[AgentTool]:
    return [tool.tool for tool in tools]


def test_the_model_reads_proposes_and_answers() -> None:
    report, rotate = _tools()
    script = _Script(
        _call("disk-report", {}),
        _call("shop-logrotate", {}),
        _say(
            "**ROOT CAUSE:** /var/log/shop filled the disk\n"
            "PROPOSED: shop-logrotate-20261008152628, "
            "postgresql-reinit-20261008150000\n"
            "WHY: rotating the logs frees the disk."
        ),
    )

    done = investigate("postgresql down", _tool_list(report, rotate), ENDPOINT, script)

    assert done.status is InvestigationStatus.ANSWERED
    assert done.finding is not None
    assert done.finding.root_cause == "/var/log/shop filled the disk"
    assert done.finding.proposed == ("shop-logrotate-20261008152628",)
    assert done.finding.unrecorded == ("postgresql-reinit-20261008150000",)
    assert [step.outcome for step in done.steps] == [
        StepOutcome.RAN,
        StepOutcome.PROPOSED,
    ]
    assert done.turns == 3
    assert (done.usage.prompt, done.usage.completion) == (250, 25)
    tool_reply = script.requests[1]["messages"]
    assert isinstance(tool_reply, list)
    assert "/var/log/shop 36G" in tool_reply[-1]["content"]


def test_the_same_call_twice_runs_once() -> None:
    report, rotate = _tools()
    script = _Script(
        _call("disk-report", {}),
        _call("disk-report", {}, "c2"),
        _say("ROOT CAUSE: disk\nPROPOSED: none\nWHY: nothing to propose."),
    )

    done = investigate("alert", _tool_list(report, rotate), ENDPOINT, script)

    assert report.calls == 1
    assert done.steps[1].outcome is StepOutcome.REPEATED
    assert done.steps[1].decision == "disk-report-1"


def test_a_repeated_read_is_not_taken_for_a_proposal() -> None:
    report = _Tool(
        "disk-report",
        ToolResult(StepOutcome.RAN, {"output": "36G"}, "disk-report-20261008152628"),
    )
    script = _Script(
        _call("disk-report", {}),
        _call("disk-report", {}, "c2"),
        _say("ROOT CAUSE: disk\nPROPOSED: disk-report-20261008152628\nWHY: none."),
    )

    done = investigate("alert", _tool_list(report), ENDPOINT, script)

    assert done.finding is not None
    assert done.finding.proposed == ()


def test_two_investigations_in_the_same_second_are_both_recorded(
    tmp_path: Path,
) -> None:
    report, rotate = _tools()
    done = investigate(
        "alert",
        _tool_list(report, rotate),
        ENDPOINT,
        _Script(_say("ROOT CAUSE: disk\nPROPOSED: none\nWHY: none.")),
    )
    store = InvestigationStore(
        directory=tmp_path / "investigations",
        catalog=SchemaCatalog(default_schema_directory()),
    )

    first = store.save(done)
    second = store.save(done)

    base = done.investigation_id.value
    assert first.investigation_id.value == base
    assert second.investigation_id.value == f"{base}-2"
    recorded = json.loads(
        (tmp_path / "investigations" / f"{base}-2.json").read_text(encoding="utf-8")
    )
    assert recorded["metadata"]["id"] == f"{base}-2"
    assert (tmp_path / "investigations" / f"{base}.json").exists()


def test_an_unknown_tool_or_bad_arguments_never_run_anything() -> None:
    report, rotate = _tools()
    script = _Script(
        _call("decisions-approve", {"decision": "shop-logrotate-1"}),
        _call("disk-report", {"surprise": True}),
        _say("ROOT CAUSE: unknown\nPROPOSED: none\nWHY: none."),
    )
    strict = AgentTool(
        name="disk-report",
        description="report",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        call=report.tool.call,
    )

    done = investigate("alert", [strict, rotate.tool], ENDPOINT, script)

    assert [step.outcome for step in done.steps] == [
        StepOutcome.UNKNOWN_TOOL,
        StepOutcome.INVALID,
    ]
    assert report.calls == 0
    assert rotate.calls == 0


def test_a_model_still_calling_tools_runs_out_of_turns() -> None:
    report, rotate = _tools()
    script = _Script(_call("disk-report", {}), _call("shop-logrotate", {}, "c2"))

    done = investigate(
        "alert", _tool_list(report, rotate), ENDPOINT, script, max_turns=2
    )

    assert done.status is InvestigationStatus.TURN_LIMIT
    assert done.finding is None


def test_an_answer_without_the_three_lines_is_unstructured() -> None:
    report, rotate = _tools()

    done = investigate(
        "alert",
        _tool_list(report, rotate),
        ENDPOINT,
        _Script(_say("The disk is full, rotate the logs.")),
    )

    assert done.status is InvestigationStatus.UNSTRUCTURED
    assert done.answer == "The disk is full, rotate the logs."


def test_a_malformed_model_answer_is_refused() -> None:
    report, rotate = _tools()

    with pytest.raises(InvestigationError) as error:
        investigate("alert", _tool_list(report, rotate), ENDPOINT, _Script({}))

    assert error.value.code == "agent_model_answer_malformed"


class _Failing(_Script):
    """A model that answers from a list, then its endpoint refuses."""

    def __call__(self, url: str, body: Mapping[str, object]) -> ModelReply:
        if not self.replies:
            message = "the model endpoint answered 500: overloaded"
            raise InvestigationError(ERROR_MODEL_REFUSED, message)
        return super().__call__(url, body)


@pytest.mark.parametrize(
    "script",
    [
        _Failing(_call("shop-logrotate", {})),
        _Script(
            _call("shop-logrotate", {}),
            {"choices": [{"message": {"tool_calls": [{"id": "c2"}]}}]},
        ),
    ],
    ids=["refused", "malformed-tool-call"],
)
def test_a_model_failing_mid_run_keeps_the_investigation_so_far(
    tmp_path: Path, script: _Script
) -> None:
    report, rotate = _tools()

    with pytest.raises(ModelUnavailableError) as error:
        investigate("alert", _tool_list(report, rotate), ENDPOINT, script)

    failed = error.value.investigation
    assert failed.status is InvestigationStatus.MODEL_UNAVAILABLE
    assert [step.decision for step in failed.steps] == [
        "shop-logrotate-20261008152628"
    ]
    assert failed.turns == 2
    assert (failed.usage.prompt, failed.usage.completion) == (100, 10)
    assert failed.answer == ""
    assert failed.finding is None
    saved = InvestigationStore(
        directory=tmp_path / "investigations",
        catalog=SchemaCatalog(default_schema_directory()),
    ).save(failed)
    spec = saved.as_document()["spec"]
    assert isinstance(spec, dict)
    assert spec["status"] == "model-unavailable"
    assert "finding" not in spec


@pytest.mark.parametrize(
    "url", ["ftp://models.example/v1", "https://user:secret@models.example/v1"]
)
def test_an_endpoint_must_be_plain_http(url: str) -> None:
    with pytest.raises(InvestigationError) as error:
        ModelEndpoint(base_url=url, model="nemotron")

    assert error.value.code == "agent_endpoint_invalid"


def _streamed(out: str) -> dict[str, Any]:
    """Read a stream: its events, the saved record's event, and how it ended.

    Each event is a bare line with ``_seq``; the stream ends with the
    ``_summary`` line, or with the error envelope when it failed.
    """
    lines = [json.loads(line) for line in out.splitlines() if line.strip()]
    events = [line for line in lines if "_seq" in line]
    record = next(
        (event for event in events if event["kind"] == "investigation"), None
    )
    return {
        "data": record,
        "error": lines[-1].get("error"),
        "events": events,
        "end": lines[-1],
    }


class _Model(BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint answering from a class-level script.

    A reply is JSON sent with 200, or a status and the raw body to send.
    """

    replies: ClassVar[list[object]] = []
    seen: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Model.seen.append(
            {"auth": self.headers["Authorization"], "body": json.loads(body)}
        )
        reply = _Model.replies.pop(0)
        status, answer = (
            reply if isinstance(reply, tuple) else (200, json.dumps(reply).encode())
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def model_url() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


def test_the_cli_investigates_and_records_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_url: str
) -> None:
    repository = _repository(tmp_path)
    (repository / "playbooks" / "facts.yml").write_text(
        "---\n- hosts: localhost\n  connection: local\n  gather_facts: false\n"
        "  tasks:\n    - name: Report\n      ansible.builtin.debug:\n"
        "        msg: /var/log/shop holds 36G\n",
        encoding="utf-8",
    )
    key = tmp_path / "model.key"
    key.write_text("sk-test\n", encoding="utf-8")
    _Model.seen.clear()
    _Model.replies[:] = [
        _call("operation_facts", {}),
        _say("ROOT CAUSE: /var/log/shop\nPROPOSED: none\nWHY: read only."),
    ]

    exit_code = main(
        [
            "agent",
            "investigate",
            "--repository",
            str(repository),
            "--alert",
            "postgresql-main down on web-1",
            "--base-url",
            model_url,
            "--model",
            "nemotron",
            "--api-key-from-file",
            str(key),
        ]
    )

    payload = _streamed(capsys.readouterr().out)
    assert exit_code == 0, payload
    spec = payload["data"]["investigation"]["spec"]
    assert spec["status"] == "answered"
    assert spec["steps"][0]["outcome"] == "ran"
    assert spec["finding"]["rootCause"] == "/var/log/shop"
    assert _Model.seen[0]["auth"] == "Bearer sk-test"
    tool_message = _Model.seen[1]["body"]["messages"][-1]
    assert "/var/log/shop holds 36G" in tool_message["content"]
    recorded = list((repository / "investigations").glob("*.json"))
    assert len(recorded) == 1
    assert "sk-test" not in recorded[0].read_text(encoding="utf-8")


def _investigate_against(repository: Path, key: Path, url: str) -> int:
    return main(
        [
            "agent",
            "investigate",
            "--repository",
            str(repository),
            "--alert",
            "postgresql-main down on web-1",
            "--base-url",
            url,
            "--model",
            "nemotron",
            "--api-key-from-file",
            str(key),
        ]
    )


class _NotJson(BaseHTTPRequestHandler):
    """An endpoint behind a proxy that answers 200 with an HTML page."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        page = b"<html>gateway</html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *_args: object) -> None:
        return


def test_an_answer_that_is_not_json_exits_model_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    key = tmp_path / "model.key"
    key.write_text("sk-test\n", encoding="utf-8")
    server = HTTPServer(("127.0.0.1", 0), _NotJson)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        exit_code = _investigate_against(
            repository, key, f"http://127.0.0.1:{server.server_address[1]}/v1"
        )
    finally:
        server.shutdown()
        server.server_close()

    payload = _streamed(capsys.readouterr().out)
    assert exit_code == 92, payload
    assert payload["error"]["context"]["code"] == "agent_model_answer_malformed"


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        # The refusal echoes the request's key, as a debugging proxy might.
        ((500, b"upstream failed for Bearer sk-test"), "agent_model_refused"),
        ((200, b"<html>gateway</html>"), "agent_model_answer_malformed"),
        ((200, b"[]"), "agent_model_answer_malformed"),
        ({"choices": []}, "agent_model_answer_malformed"),
        (
            {"choices": [{"message": {"tool_calls": [{"id": "c2"}]}}]},
            "agent_model_answer_malformed",
        ),
    ],
    ids=["refused", "not-json", "not-an-object", "no-choices", "bad-tool-call"],
)
def test_a_model_failing_mid_run_records_the_investigation_and_exits_92(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    model_url: str,
    failure: object,
    code: str,
) -> None:
    repository = _repository(tmp_path)
    (repository / "playbooks" / "facts.yml").write_text(
        "---\n- hosts: localhost\n  connection: local\n  gather_facts: false\n"
        "  tasks:\n    - name: Report\n      ansible.builtin.debug:\n"
        "        msg: /var/log/shop holds 36G\n",
        encoding="utf-8",
    )
    key = tmp_path / "model.key"
    key.write_text("sk-test\n", encoding="utf-8")
    _Model.seen.clear()
    _Model.replies[:] = [_call("operation_facts", {}), failure]

    exit_code = _investigate_against(repository, key, model_url)

    out = capsys.readouterr()
    assert "sk-test" not in out.out + out.err
    payload = _streamed(out.out)
    assert exit_code == 92, payload
    context = payload["error"]["context"]
    assert context["code"] == code
    recorded = repository / "investigations" / f"{context['investigation']}.json"
    text = recorded.read_text(encoding="utf-8")
    assert "sk-test" not in text
    spec = json.loads(text)["spec"]
    assert spec["status"] == "model-unavailable"
    assert spec["turns"] == 2
    assert spec["tokens"] == {"prompt": 100, "completion": 10}
    assert spec["answer"] == ""
    assert "finding" not in spec
    [step] = spec["steps"]
    assert step["outcome"] == "ran"
    decisions = list((repository / "decisions").glob(f"{step['decision']}*"))
    assert decisions, "the decision of the first turn is recorded"


@pytest.mark.parametrize(
    "url",
    [
        "https://models.example/v1?api-key=sk-leak",
        "https://models.example/v1#sk-leak",
        "ftp://models.example/v1?api-key=sk-leak",
    ],
)
def test_an_endpoint_with_a_query_or_fragment_is_refused_before_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], url: str
) -> None:
    repository = _repository(tmp_path)
    key = tmp_path / "model.key"
    key.write_text("sk-test\n", encoding="utf-8")

    exit_code = _investigate_against(repository, key, url)

    out = capsys.readouterr()
    payload = _streamed(out.out)
    assert exit_code == 2, payload
    assert "sk-leak" not in out.out + out.err
    assert not (repository / "investigations").exists()


def test_a_made_up_long_tool_name_is_still_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_url: str
) -> None:
    repository = _repository(tmp_path)
    key = tmp_path / "model.key"
    key.write_text("sk-test\n", encoding="utf-8")
    _Model.seen.clear()
    _Model.replies[:] = [
        _call("x" * 200, {}),
        _say("ROOT CAUSE: none\nPROPOSED: none\nWHY: no tool fit."),
    ]

    exit_code = _investigate_against(repository, key, model_url)

    payload = _streamed(capsys.readouterr().out)
    assert exit_code == 0, payload
    step = payload["data"]["investigation"]["spec"]["steps"][0]
    assert step["outcome"] == "unknown-tool"
    assert step["tool"] == "x" * 128


@pytest.mark.parametrize(
    ("flag", "value"), [("--alert", " "), ("--model", "m" * 257)]
)
def test_what_the_record_would_refuse_is_refused_before_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    repository = _repository(tmp_path)
    key = tmp_path / "model.key"
    key.write_text("sk-test\n", encoding="utf-8")
    arguments = {
        "--alert": "postgresql-main down on web-1",
        "--base-url": "http://127.0.0.1:9/v1",
        "--model": "nemotron",
    }
    arguments[flag] = value

    exit_code = main(
        [
            "agent",
            "investigate",
            "--repository",
            str(repository),
            *(part for pair in arguments.items() for part in pair),
            "--api-key-from-file",
            str(key),
        ]
    )

    payload = _streamed(capsys.readouterr().out)
    assert exit_code == 2, payload
    assert not (repository / "investigations").exists()


def test_an_unreachable_endpoint_exits_model_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    key = tmp_path / "model.key"
    key.write_text("sk-test\n", encoding="utf-8")
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()

    exit_code = _investigate_against(repository, key, f"http://127.0.0.1:{port}/v1")

    payload = _streamed(capsys.readouterr().out)
    assert exit_code == 92, payload
    context = payload["error"]["context"]
    assert context["code"] == "agent_model_unreachable"
    # The first request was sent: the run started, so it is recorded, empty.
    recorded = repository / "investigations" / f"{context['investigation']}.json"
    spec = json.loads(recorded.read_text(encoding="utf-8"))["spec"]
    assert spec["status"] == "model-unavailable"
    assert (spec["turns"], spec["steps"]) == (1, [])


def test_a_decision_whose_check_failed_is_not_unrecorded() -> None:
    failed = _Tool(
        "shop-logrotate",
        ToolResult(StepOutcome.FAILED, {"ok": False}, "shop-logrotate-20261008170132"),
    )
    script = _Script(
        _call("shop-logrotate", {}),
        _say(
            "ROOT CAUSE: disk full\nPROPOSED: shop-logrotate-20261008170132\n"
            "WHY: its check failed, a person should look."
        ),
    )

    done = investigate("alert", [failed.tool], ENDPOINT, script)

    assert done.finding is not None
    assert done.finding.proposed == ()
    assert done.finding.unrecorded == ()


def test_a_replay_answers_from_the_recording_and_runs_no_playbook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_url: str
) -> None:
    repository = _repository(tmp_path)
    facts = repository / "playbooks" / "facts.yml"
    facts.write_text(
        "---\n- hosts: localhost\n  connection: local\n  gather_facts: false\n"
        "  tasks:\n    - name: Report\n      ansible.builtin.debug:\n"
        "        msg: /var/log/shop holds 36G\n",
        encoding="utf-8",
    )
    assert (
        main(
            [
                "operations",
                "propose",
                "facts",
                "--repository",
                str(repository),
                "--decisions",
                "recorded",
            ]
        )
        == 0
    )
    capsys.readouterr()
    facts.write_text(
        "---\n- hosts: localhost\n  connection: local\n  gather_facts: false\n"
        "  tasks:\n    - name: Fail\n      ansible.builtin.fail:\n"
        "        msg: a replay must not run this\n",
        encoding="utf-8",
    )
    key = tmp_path / "model.key"
    key.write_text("sk-test\n", encoding="utf-8")
    _Model.seen.clear()
    _Model.replies[:] = [
        _call("operation_facts", {}),
        _call("operation_deploy", {"target": "web-9", "inputs": {"version": "2"}}),
        _say("ROOT CAUSE: /var/log/shop\nPROPOSED: none\nWHY: read only."),
    ]

    exit_code = main(
        [
            "agent",
            "investigate",
            "--repository",
            str(repository),
            "--alert",
            "postgresql-main down on web-1",
            "--base-url",
            model_url,
            "--model",
            "nemotron",
            "--api-key-from-file",
            str(key),
            "--replay",
            "recorded",
        ]
    )

    payload = _streamed(capsys.readouterr().out)
    assert exit_code == 0, payload
    spec = payload["data"]["investigation"]["spec"]
    assert spec["replay"] == "recorded"
    assert [step["outcome"] for step in spec["steps"]] == ["ran", "failed"]
    replies = [seen["body"]["messages"][-1]["content"] for seen in _Model.seen[1:]]
    assert "/var/log/shop holds 36G" in replies[0]
    assert "a replay must not run this" not in replies[0]
    assert "holds no run of playbooks/deploy.yml" in replies[1]


@pytest.mark.parametrize("recording", ["missing", "empty"])
def test_a_replay_of_no_recorded_run_is_refused_before_the_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], recording: str
) -> None:
    repository = _repository(tmp_path)
    (repository / "empty").mkdir()
    key = tmp_path / "model.key"
    key.write_text("sk-test\n", encoding="utf-8")

    exit_code = main(
        [
            "agent",
            "investigate",
            "--repository",
            str(repository),
            "--alert",
            "postgresql-main down on web-1",
            "--base-url",
            "http://127.0.0.1:9/v1",
            "--model",
            "nemotron",
            "--api-key-from-file",
            str(key),
            "--replay",
            recording,
        ]
    )

    payload = _streamed(capsys.readouterr().out)
    assert exit_code != 0, payload
    assert "recording_empty" in json.dumps(payload)
    assert not (repository / "investigations").exists()


def test_the_cli_streams_each_turn_and_call_then_the_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_url: str
) -> None:
    repository = _repository(tmp_path)
    (repository / "playbooks" / "facts.yml").write_text(
        "---\n- hosts: localhost\n  connection: local\n  gather_facts: false\n"
        "  tasks:\n    - name: Report\n      ansible.builtin.debug:\n"
        "        msg: /var/log/shop holds 36G\n",
        encoding="utf-8",
    )
    key = tmp_path / "model.key"
    key.write_text("sk-test\n", encoding="utf-8")
    _Model.replies[:] = [
        _call("operation_facts", {}),
        _say("ROOT CAUSE: /var/log/shop\nPROPOSED: none\nWHY: read only."),
    ]

    assert _investigate_against(repository, key, model_url) == 0

    stream = _streamed(capsys.readouterr().out)
    events = stream["events"]
    assert [event["_seq"] for event in events] == [1, 2, 3, 4]
    assert [event["kind"] for event in events] == [
        "turn",
        "call",
        "turn",
        "investigation",
    ]
    assert events[1]["effect"] == "created"
    assert "/var/log/shop holds 36G" in events[1]["result"]["output"]
    assert events[2]["text"].startswith("ROOT CAUSE")
    # The decision the call wrote, and the saved record.
    assert stream["end"]["_summary"] is True
    assert stream["end"]["effects"] == {"created": 2, "noop": 2}


def test_an_incomplete_investigation_ends_with_the_record_then_93(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model_url: str
) -> None:
    repository = _repository(tmp_path)
    key = tmp_path / "model.key"
    key.write_text("sk-test\n", encoding="utf-8")
    _Model.replies[:] = [_say("I am not sure what happened.")]

    exit_code = _investigate_against(repository, key, model_url)

    payload = _streamed(capsys.readouterr().out)
    assert exit_code == 93, payload
    assert payload["data"]["status"] == "unstructured"
    assert payload["events"][-1]["kind"] == "investigation"
    assert payload["end"]["error"] is not None
    assert len(list((repository / "investigations").glob("*.json"))) == 1
    error = payload["error"]
    assert error["code"] == "INVESTIGATION_INCOMPLETE"
    # The error line holds no data: the record is the event before it.
    assert "data.investigation" not in error["suggestion"]
    assert "kind investigation" in error["suggestion"]


def test_each_turn_keeps_the_raw_trace() -> None:
    report, rotate = _tools()
    thinking = _call("disk-report", {})
    thinking["id"] = "chatcmpl-1"
    first = thinking["choices"]
    assert isinstance(first, list)
    first[0]["message"]["reasoning_content"] = "The disk may be full; read it first."
    thinking["usage"] = {
        "prompt_tokens": 100,
        "completion_tokens": 40,
        "completion_tokens_details": {"reasoning_tokens": 30},
    }
    script = _Script(
        thinking, _say("ROOT CAUSE: disk\nPROPOSED: none\nWHY: read only.")
    )

    done = investigate("alert", _tool_list(report, rotate), ENDPOINT, script)

    turn = done.trace[0]
    assert turn.reasoning == "The disk may be full; read it first."
    assert turn.tool_calls[0].name == "disk-report"
    assert turn.tool_calls[0].arguments == "{}"
    assert (turn.response_id, turn.request_id) == ("chatcmpl-1", "req-test")
    assert (turn.prompt_tokens, turn.completion_tokens, turn.reasoning_tokens) == (
        100,
        40,
        30,
    )
    recorded = done.as_document()["spec"]
    assert isinstance(recorded, dict)
    assert recorded["trace"][0]["reasoning"] == turn.reasoning
    assert recorded["trace"][1]["toolCalls"] == []
