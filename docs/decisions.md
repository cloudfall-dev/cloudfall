# Decisions

Rules the maintainers settled, numbered in order. Triage designs and code reviews check
every change against the entries whose "Applies to" it touches. Change a rule by adding an
entry that supersedes it, never by editing an old one.

## D-1: Read runs and replaced proposals have their own decision status

- Decided: 2026-10-09, in cloudfall-dev/cloudfall#26
- Rule: A decision for a risk: read operation is recorded as status ran with the check run's exit code, and a newer proposal of the same operation, targets and inputs marks the older proposed record superseded with superseded_by naming the new decision; the existing status values keep their single meaning
- Why: A read run is finished when recorded and a repeated proposal replaces the old one; reusing executed or rejected would give one status two meanings in the record
- Applies to: sdk/src/cloudfall/decision.py, config/schemas/v1/operation-decision.schema.json, decisions list, cloudfall why
- Enforced by: review

## D-2: A replayed decision is marked and cannot be approved

- Decided: 2026-10-09, in cloudfall-dev/cloudfall#36
- Rule: A decision proposed during agent investigate --replay carries a replay field naming the recording, decisions approve refuses it with PRECONDITION, and --replay refuses a --decisions folder that is the recording itself
- Why: Its check output was played back, not run on the host, so approving it would let the gate trust evidence that never came from the host
- Applies to: sdk/src/cloudfall/app.py, sdk/src/cloudfall/decision.py, sdk/src/cloudfall/recording.py, config/schemas/v1/operation-decision.schema.json, decisions approve
- Enforced by: review

## D-3: A run cut short is recorded as stopped

- Decided: 2026-10-09, in cloudfall-dev/cloudfall#44
- Rule: When agent investigate cannot finish because its reader went away, the investigation record is saved with status stopped and the command exits non-zero
- Why: A record saved with the status it had mid-run looks like a run still going or one that ended normally
- Applies to: sdk/src/cloudfall/investigation.py, sdk/src/cloudfall/app.py, config/schemas/v1/agent-investigation.schema.json, agent investigate
- Enforced by: review

## D-4: Model reasoning in the investigation record is capped

- Decided: 2026-10-09, in cloudfall-dev/cloudfall#50
- Rule: The investigation record keeps at most 8000 characters of a turn's reasoning in spec.trace, like TOOL_TEXT_LIMIT, and marks the cut; the schema bounds the field
- Why: The model's reasoning quotes host output it read, so unbounded reasoning makes the record unbounded and stores host output in it
- Applies to: sdk/src/cloudfall/investigation.py, config/schemas/v1/agent-investigation.schema.json
- Enforced by: review
