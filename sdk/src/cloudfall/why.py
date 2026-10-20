"""The question the record exists to answer: why did the agent do that.

A month after an agent starts running a fleet, someone asks what happened
to one host, or what one operation has been doing, or what changed last
Tuesday. The decision records hold the answer, one file per decision, but
nobody reads a directory of JSON. This module reads it for them: filter
the record by host, operation and time window, and tell each decision as
a story a person can check against the record it came from.

Nothing here is inferred. A decision is about a host only when the record
names that host, as its target or in the per-host evidence of its check,
run or verify stage. A group pattern is not expanded, because expanding
it would need an inventory the record deliberately does not depend on.

A decision an agent asked for is told with the turn that asked for it:
the investigation records each tool call's decision and turn, and the
turn's trace keeps the model's reasoning and the endpoint's ids.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import TYPE_CHECKING, cast

from jsonschema import ValidationError

from cloudfall.decision import ApprovalChannel, DecisionStatus, Requirement
from cloudfall.domain import Hostname, ResourceId
from cloudfall.investigation import INVESTIGATION_SCHEMA, StepOutcome

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from cloudfall.decision import (
        Approval,
        Decision,
        DecisionStore,
        ObservationClock,
        RunRecord,
        Staleness,
    )
    from cloudfall.validation import SchemaCatalog

_ERROR_INSTANT_INVALID = "why_instant_invalid"
_ERROR_WINDOW_INVERTED = "why_window_inverted"
_ERROR_STATUS_UNKNOWN = "why_status_unknown"
_ERROR_INVESTIGATION_INVALID = "why_investigation_invalid"


class WhyError(RuntimeError):
    """Fail-fast query error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        """Record the failure code and human-readable detail."""
        self.code = code
        self.detail = message
        super().__init__(f"{code}: {message}")

    def as_dict(self) -> dict[str, object]:
        """Serialize the error envelope for system boundaries."""
        return {
            "status": "error",
            "error": {"code": self.code, "message": self.detail},
        }


@dataclass(frozen=True, slots=True, order=True)
class Instant:
    """One moment in UTC, as the record writes it."""

    value: datetime

    def __post_init__(self) -> None:
        """Refuse a naive moment: the record is in UTC and so is a query."""
        if self.value.tzinfo is None:
            message = f"instant must carry a timezone: {self.value.isoformat()}"
            raise ValueError(message)

    @classmethod
    def from_boundary(cls, value: object) -> Instant:
        """Parse an ISO 8601 moment, or a date meaning midnight UTC."""
        if not isinstance(value, str) or not value.strip():
            message = f"instant must be an ISO 8601 string, got {value!r}"
            raise WhyError(_ERROR_INSTANT_INVALID, message)
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as error:
            message = f"instant is not ISO 8601: {text!r}"
            raise WhyError(_ERROR_INSTANT_INVALID, message) from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return cls(parsed.astimezone(UTC))

    def as_string(self) -> str:
        """Render the moment the way the record does."""
        return self.value.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class WhyQuery:
    """What is being asked of the record."""

    host: Hostname | None = None
    operation: ResourceId | None = None
    since: Instant | None = None
    until: Instant | None = None
    statuses: frozenset[DecisionStatus] = frozenset()
    stale: bool = False
    """Only the proposals whose basis hosts have been observed again since."""

    def __post_init__(self) -> None:
        """Refuse a window that ends before it starts."""
        if (
            self.since is not None
            and self.until is not None
            and self.until < self.since
        ):
            message = (
                f"the window ends at {self.until.as_string()}, before it "
                f"starts at {self.since.as_string()}"
            )
            raise WhyError(_ERROR_WINDOW_INVERTED, message)

    @classmethod
    def from_boundary(
        cls,
        host: object = None,
        operation: object = None,
        since: object = None,
        until: object = None,
        status: Iterable[object] = (),
    ) -> WhyQuery:
        """Build a query from the primitives a CLI or tool call carries."""
        return cls(
            host=Hostname.from_boundary(host) if _present(host) else None,
            operation=(
                ResourceId.from_boundary(operation) if _present(operation) else None
            ),
            since=Instant.from_boundary(since) if _present(since) else None,
            until=Instant.from_boundary(until) if _present(until) else None,
            statuses=frozenset(_status(value) for value in status),
        )

    def as_dict(self) -> dict[str, object]:
        """Serialize the query for the answer envelope."""
        result: dict[str, object] = {}
        if self.host is not None:
            result["host"] = self.host.value
        if self.operation is not None:
            result["operation"] = self.operation.value
        if self.since is not None:
            result["since"] = self.since.as_string()
        if self.until is not None:
            result["until"] = self.until.as_string()
        if self.statuses:
            result["status"] = sorted(status.value for status in self.statuses)
        if self.stale:
            result["stale"] = True
        return result

    def matches(
        self, decision: Decision, staleness: Staleness | None = None
    ) -> bool:
        """Return whether one decision is part of the answer."""
        if self.operation is not None and decision.operation_id != self.operation:
            return False
        if self.stale and (staleness is None or not staleness.stale):
            return False
        if self.statuses and decision.status not in self.statuses:
            return False
        if self.host is not None and self.host.value not in hosts_of(decision):
            return False
        if self.since is None and self.until is None:
            return True
        return any(
            (self.since is None or self.since <= moment)
            and (self.until is None or moment <= self.until)
            for moment in moments_of(decision)
        )


@dataclass(frozen=True, slots=True)
class Explanation:
    """One decision, told as what the record can prove about it."""

    decision: Decision
    hosts: tuple[str, ...]
    story: tuple[str, ...]
    proposer: Proposer | None = None
    staleness: Staleness | None = None
    """Whether an open proposal was made on evidence observed again since."""

    def as_dict(self) -> dict[str, object]:
        """Serialize the explanation with the record it is drawn from."""
        record = self.decision.as_document()
        if self.staleness is not None:
            record.update(self.staleness.as_dict())
        document: dict[str, object] = {
            "id": self.decision.decision_id.value,
            "hosts": list(self.hosts),
            "story": list(self.story),
            "decision": record,
        }
        if self.proposer is not None:
            document["agent"] = self.proposer.as_dict()
        return document


@dataclass(frozen=True, slots=True)
class Proposer:
    """The agent turn that asked for a decision, as its investigation recorded it."""

    investigation: str
    model: str
    endpoint: str
    alert: str
    turn: int | None
    """None in a record written before a step kept its turn."""
    reasoning: str | None
    response_id: str | None
    request_id: str | None

    def as_dict(self) -> dict[str, object]:
        """Serialize the turn for the answer, as the investigation has it."""
        document: dict[str, object] = {
            "investigation": self.investigation,
            "model": self.model,
            "endpoint": self.endpoint,
            "alert": self.alert,
        }
        optional = {
            "turn": self.turn,
            "reasoning": self.reasoning,
            "responseId": self.response_id,
            "requestId": self.request_id,
        }
        document.update({key: value for key, value in optional.items() if value})
        return document

    def sentences(self) -> tuple[str, ...]:
        """Tell who asked for the decision, and what it was thinking then."""
        where = f"in turn {self.turn} of" if self.turn is not None else "in"
        # An alert that ends its own sentence is not given a second stop.
        stop = "" if self.alert.rstrip().endswith((".", "!", "?")) else "."
        asked = (
            f"The agent {self.model} at {self.endpoint} asked for it {where} "
            f"investigation {self.investigation}, working the alert "
            f'"{self.alert.strip()}"{stop}'
        )
        if self.turn is None:
            return (
                asked,
                "That investigation predates recording which turn made each "
                "call, so its reasoning is in its trace but not tied to this one.",
            )
        if self.reasoning is None:
            return (asked, "The endpoint returned no reasoning for that turn.")
        served = ", ".join(
            f"{label} {value}"
            for label, value in (
                ("response", self.response_id),
                ("request", self.request_id),
            )
            if value is not None
        )
        return (
            asked,
            "Its reasoning in that turn, as the endpoint returned it: "
            f'"{self.reasoning}"' + (f" ({served})." if served else "."),
        )


@dataclass(frozen=True, slots=True)
class Investigations:
    """Which agent turn asked for each decision, from the investigation records."""

    proposers: Mapping[str, Proposer]

    @classmethod
    def load(cls, directory: Path, catalog: SchemaCatalog) -> Investigations:
        """Read every investigation record in the directory; none is no link.

        A record that is not valid JSON or does not match its schema is
        refused: an answer drawn from it could cite a turn that never was.
        A decision a later call only repeated keeps the turn that asked first.
        """
        proposers: dict[str, Proposer] = {}
        if not directory.is_dir():
            return cls(proposers)
        for path in sorted(directory.glob("*.json")):
            document = _investigation(path, catalog)
            metadata = cast("Mapping[str, str]", document["metadata"])
            spec = cast("Mapping[str, object]", document["spec"])
            for decision, proposer in _proposers(metadata["id"], spec):
                proposers.setdefault(decision, proposer)
        return cls(proposers)


@dataclass(frozen=True, slots=True)
class WhyAnswer:
    """Every decision the record holds that the question is about."""

    query: WhyQuery
    directory: str
    explanations: tuple[Explanation, ...]

    def as_dict(self) -> dict[str, object]:
        """Serialize the answer for system boundaries."""
        return {
            "status": "ok",
            "query": self.query.as_dict(),
            "directory": self.directory,
            "count": len(self.explanations),
            "answers": [entry.as_dict() for entry in self.explanations],
        }


def answer(
    store: DecisionStore,
    query: WhyQuery,
    investigations: Investigations | None = None,
    clock: ObservationClock | None = None,
) -> WhyAnswer:
    """Answer the question from the record, newest decision first.

    With a ``clock``, each open proposal says whether it is stale; without
    one, nothing is, so a ``stale`` question matches nothing.
    """
    matched: list[tuple[Decision, Staleness | None]] = []
    for decision in store.list():
        staleness = clock.staleness(decision) if clock is not None else None
        if query.matches(decision, staleness):
            matched.append((decision, staleness))
    matched.sort(key=lambda entry: entry[0].proposed_at, reverse=True)
    proposers = investigations.proposers if investigations is not None else {}
    return WhyAnswer(
        query=query,
        directory=str(store.directory),
        explanations=tuple(
            explain(decision, proposers.get(decision.decision_id.value), staleness)
            for decision, staleness in matched
        ),
    )


def explain(
    decision: Decision,
    proposer: Proposer | None = None,
    staleness: Staleness | None = None,
) -> Explanation:
    """Tell one decision from its record, and the agent turn that asked for it."""
    return Explanation(
        decision=decision,
        hosts=tuple(sorted(hosts_of(decision))),
        story=tuple(_story(decision, proposer)),
        proposer=proposer,
        staleness=staleness,
    )


def hosts_of(decision: Decision) -> set[str]:
    """Return every host the record names: the target and the evidence."""
    hosts: set[str] = set()
    if decision.targets.pattern is not None:
        hosts.add(decision.targets.pattern)
    hosts.update(decision.check.changed, decision.check.unchanged)
    for run in (decision.execution, decision.verification):
        if run is not None:
            hosts.update(run.changed, run.unchanged)
    return hosts


def moments_of(decision: Decision) -> tuple[Instant, ...]:
    """Return every moment the record dates: proposal, approval, runs."""
    stamps = [decision.proposed_at]
    if decision.approval is not None:
        stamps.append(decision.approval.approved_at)
    stamps.extend(
        run.ran_at
        for run in (decision.execution, decision.verification)
        if run is not None
    )
    return tuple(Instant.from_boundary(stamp) for stamp in stamps)


def render_why_html(result: WhyAnswer) -> str:
    """Render the answer as one page a person reads without the JSON."""
    return render_why_document(result.as_dict())


def render_why_document(document: Mapping[str, object]) -> str:
    """Render the answer's JSON document as one page, as ``--format html`` does.

    The page is drawn from the document alone, so treaty's renderer, which
    receives the answer's ``data``, draws the same page as the object would.
    """
    answers = cast("Sequence[Mapping[str, object]]", document["answers"])
    query = cast("Mapping[str, object]", document["query"])
    cards = "".join(_render_card(entry) for entry in answers)
    if not cards:
        cards = (
            '<p class="empty">The record holds no decision this question is about.</p>'
        )
    filters = ", ".join(
        f"{escape(key)} = {escape(_filter_value(value))}"
        for key, value in query.items()
    )
    asked = escape(filters) if filters else "Every decision the record holds"
    count = len(answers)
    counted = f"{count} decision{'' if count == 1 else 's'}"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Cloudfall Why</title>
  <style>
    :root {{ color-scheme: dark; --bg: #0c1117; --panel: #151c24;
      --line: #293442; --text: #e7edf5; --muted: #91a0b2;
      --healthy: #48c78e; --warning: #ffbd59; --critical: #ff6577;
      --unknown: #91a0b2; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text);
      font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif; }}
    main {{ width: min(920px, calc(100% - 32px)); margin: 40px auto 64px; }}
    h1, h2, h3, p {{ margin-top: 0; }} h1 {{ margin-bottom: 4px; }}
    .muted, code {{ color: var(--muted); }}
    .badge {{ display: inline-flex; align-items: center; border: 1px solid;
      border-radius: 999px; padding: 4px 9px; font-size: 12px;
      font-weight: 700; letter-spacing: .04em; text-transform: uppercase; }}
    .healthy {{ color: var(--healthy); }} .warning {{ color: var(--warning); }}
    .critical {{ color: var(--critical); }} .unknown {{ color: var(--unknown); }}
    .decision {{ background: var(--panel); border: 1px solid var(--line);
      border-radius: 12px; padding: 18px; margin-top: 14px; }}
    .decision-head {{ display: flex; justify-content: space-between; gap: 16px; }}
    .decision h3 {{ margin-bottom: 2px; }}
    ol {{ margin: 12px 0 0; padding-left: 22px; }} li {{ margin-bottom: 6px; }}
    .hosts {{ margin-top: 10px; font-size: 13px; }}
    .empty {{ color: var(--muted); padding: 28px; text-align: center; }}
    footer {{ margin-top: 20px; color: var(--muted); font-size: 13px; }}
    @media (max-width: 720px) {{ .decision-head {{ display: block; }}
      main {{ margin-top: 24px; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <h1>Why did the agent do that</h1>
    <p class="muted">{asked} · {counted}
      · from <code>{escape(str(document["directory"]))}</code></p>
  </header>
  {cards}
  <footer>Read-only projection of the decision records. Nothing here is inferred;
    every sentence cites a field of the record it sits under.</footer>
</main>
</body>
</html>
"""


def _render_card(entry: Mapping[str, object]) -> str:
    decision = cast("Mapping[str, object]", entry["decision"])
    spec = cast("Mapping[str, object]", decision["spec"])
    operation = cast("Mapping[str, object]", spec["operation"])
    status = DecisionStatus(str(spec["status"]))
    ran_exit_code = spec.get("ranExitCode")
    story = cast("Sequence[str]", entry["story"])
    steps = "".join(f"<li>{escape(sentence)}</li>" for sentence in story)
    hosts = (
        ", ".join(escape(host) for host in cast("Sequence[str]", entry["hosts"]))
        or "none named"
    )
    return (
        '<article class="decision">'
        '<div class="decision-head">'
        f"<div><h3>{escape(str(operation['id']))}</h3>"
        f"<code>{escape(str(entry['id']))}</code></div>"
        f'<span class="badge {_tone(status, ran_exit_code)}">'
        f"{escape(status.value)}</span></div>"
        f"<ol>{steps}</ol>"
        f'<div class="hosts muted">Hosts the record names: {hosts}</div>'
        "</article>"
    )


def _tone(status: DecisionStatus, ran_exit_code: object = None) -> str:
    if status is DecisionStatus.RAN:
        return "healthy" if ran_exit_code == 0 else "critical"
    if status is DecisionStatus.VERIFIED:
        return "healthy"
    if status is DecisionStatus.FAILED:
        return "critical"
    if status in {DecisionStatus.REJECTED, DecisionStatus.SUPERSEDED}:
        return "unknown"
    return "warning"


def _filter_value(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(entry) for entry in cast("list[object]", value))
    return str(value)


def _status(value: object) -> DecisionStatus:
    try:
        return DecisionStatus(str(value))
    except ValueError as error:
        known = ", ".join(status.value for status in DecisionStatus)
        message = f"status {value!r} is not one of {known}"
        raise WhyError(_ERROR_STATUS_UNKNOWN, message) from error


def _story(decision: Decision, proposer: Proposer | None) -> Iterable[str]:
    """Yield the record as sentences: saw, proposed, checked, approved, ended."""
    yield _saw(decision)
    yield _proposed(decision)
    if proposer is not None:
        yield from proposer.sentences()
    yield _checked(decision)
    if decision.replay is not None:
        yield (
            f"That check output was played back from the recording "
            f"{decision.replay}, not run on the hosts, so it cannot be approved."
        )
    yield _gated(decision)
    if decision.approval is not None:
        yield _approved(decision.approval)
    if decision.execution is not None:
        yield _ran("The run", decision.execution, decision.execution.log.path)
    if decision.verification is not None:
        yield _ran(
            "The verify step", decision.verification, decision.verification.log.path
        )
    yield _ended(decision)


def _approved(approval: Approval) -> str:
    said = (
        f"{approval.approver} approved it at {approval.approved_at} "
        f"(via {approval.via.value})"
    )
    if approval.via is ApprovalChannel.UNKNOWN:
        return f"{said}: the record predates noting how an approval arrived."
    return f"{said}: a person typed its id at a terminal."


def _saw(decision: Decision) -> str:
    observed = decision.basis.observed_at
    if not observed:
        return (
            "It was proposed against no observation: the record cites "
            f"{decision.basis.observations}, which held no snapshot."
        )
    latest = max(observed.values())
    return (
        f"It was based on {len(observed)} host snapshot"
        f"{'' if len(observed) == 1 else 's'} in {decision.basis.observations}, "
        f"the latest observed at {latest}."
    )


def _proposed(decision: Decision) -> str:
    target = (
        f"{decision.targets.scope.value} {decision.targets.pattern}"
        if decision.targets.pattern is not None
        else "the whole fleet"
    )
    inputs = ", ".join(
        f"{name}={value}" for name, value in sorted(decision.inputs.items())
    )
    return (
        f"At {decision.proposed_at} the {decision.risk.value} operation "
        f"{decision.operation_id.value} ({decision.playbook}) was proposed "
        f"against {target}" + (f" with {inputs}." if inputs else " with no inputs.")
    )


def _checked(decision: Decision) -> str:
    check = decision.check
    if check.exit_code != 0:
        return (
            f"Check mode exited {check.exit_code}; its output is "
            f"{check.diff.path} (sha256 {check.diff.sha256[:12]}…)."
        )
    changed = ", ".join(check.changed) or "no host"
    return (
        f"Check mode would have changed {changed} and left "
        f"{len(check.unchanged)} host{'' if len(check.unchanged) == 1 else 's'} "
        f"untouched; the diff is {check.diff.path} "
        f"(sha256 {check.diff.sha256[:12]}…, {check.diff.size} bytes)."
    )


def _gated(decision: Decision) -> str:
    if decision.requirement is Requirement.RUNS_FREELY:
        return f"Nothing gated it: {decision.reason}."
    if decision.requirement is Requirement.HUMAN_REQUIRED:
        return f"Only a person could let it through: {decision.reason}."
    return f"It waited for an approval: {decision.reason}."


def _ran(subject: str, run: RunRecord, log: object) -> str:
    changed = ", ".join(run.changed) or "no host"
    return (
        f"{subject} at {run.ran_at} exited {run.exit_code} and changed "
        f"{changed}; the log is {log}."
    )


def _ended(decision: Decision) -> str:
    if decision.status is DecisionStatus.PROPOSED:
        return "It is still proposed: nobody has approved or rejected it."
    if decision.status is DecisionStatus.RAN and decision.replay is not None:
        return (
            f"Its played-back output exited {decision.ran_exit_code}: "
            "a read operation waits for no approval."
        )
    if decision.status is DecisionStatus.RAN:
        return (
            f"It ran when it was proposed and exited {decision.ran_exit_code}: "
            "a read operation waits for no approval."
        )
    if decision.status is DecisionStatus.SUPERSEDED:
        return (
            f"It was superseded by {decision.superseded_by}, a newer proposal "
            "of the same operation, targets and inputs; nobody approved it."
        )
    if decision.verdict is not None:
        return f"It ended {decision.status.value}: {decision.verdict}."
    return f"It ended {decision.status.value}."


def _present(value: object) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _investigation(path: Path, catalog: SchemaCatalog) -> Mapping[str, object]:
    try:
        document = cast(
            "Mapping[str, object]", json.loads(path.read_text(encoding="utf-8"))
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        message = f"investigation record is not valid JSON: {path}: {error}"
        raise WhyError(_ERROR_INVESTIGATION_INVALID, message) from error
    try:
        catalog.validate_named(INVESTIGATION_SCHEMA, document)
    except ValidationError as error:
        field = ".".join(str(part) for part in error.absolute_path) or "(root)"
        message = (
            f"investigation record does not match its schema: {path}: "
            f"{field}: {error.message}"
        )
        raise WhyError(_ERROR_INVESTIGATION_INVALID, message) from error
    return document


def _proposers(
    identifier: str, spec: Mapping[str, object]
) -> Iterable[tuple[str, Proposer]]:
    """Yield each decision the investigation's own calls recorded, with its turn."""
    model = cast("Mapping[str, str]", spec["model"])
    trace = {
        int(cast("int", turn["turn"])): turn
        for turn in cast("Sequence[Mapping[str, object]]", spec.get("trace", ()))
    }
    for step in cast("Sequence[Mapping[str, object]]", spec["steps"]):
        decision = step.get("decision")
        if decision is None or step["outcome"] == StepOutcome.REPEATED.value:
            continue
        number = cast("int | None", step.get("turn"))
        turn = trace.get(number, {}) if number is not None else {}
        yield (
            str(decision),
            Proposer(
                investigation=identifier,
                model=model["name"],
                endpoint=model["endpoint"],
                alert=str(spec["alert"]),
                turn=number,
                reasoning=_text(turn.get("reasoning")),
                response_id=_text(turn.get("responseId")),
                request_id=_text(turn.get("requestId")),
            ),
        )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
