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

## D-5: agent investigate exits PRECONDITION when its record can't be written

- Decided: 2026-10-09, in cloudfall-dev/cloudfall#56
- Rule: When agent investigate cannot write the investigation record, it exits PRECONDITION with no traceback and an error naming the investigations folder; this holds for a stopped run too, whose record is then lost but whose exit stays non-zero
- Why: An unwritable store folder is the same kind of refusal as a bad --decisions folder, and a traceback with exit 0 breaks D-3
- Applies to: sdk/src/cloudfall/investigation.py, sdk/src/cloudfall/app.py, agent investigate
- Enforced by: review

## D-6: The investigation answer and raw tool-call arguments are capped from the start

- Decided: 2026-10-09, in cloudfall-dev/cloudfall#57
- Rule: The investigation record keeps at most 8000 characters of spec.answer and of each trace[].toolCalls[].arguments, keeping the start and marking the cut; the schema bounds both fields
- Why: Both can quote host output like the reasoning D-4 caps, and the finding sits at the start of the answer, so the start is what to keep
- Applies to: sdk/src/cloudfall/investigation.py, config/schemas/v1/agent-investigation.schema.json
- Enforced by: review

## D-7: Replayed and live proposals never supersede each other

- Decided: 2026-10-09, in cloudfall-dev/cloudfall#58
- Rule: A newer proposal supersedes an older proposed record (D-1) only when both have the same replay value, so a replay never closes a live proposal and a live run never closes a replayed one; a replayed read keeps status ran and its verdict says the output was played back, with no schema change
- Why: A replayed record can't be approved (D-2), so letting it supersede a live proposal leaves the operator with nothing to approve
- Applies to: sdk/src/cloudfall/decision.py, sdk/src/cloudfall/app.py, agent investigate --replay, cloudfall why
- Enforced by: review

## D-8: The finding and recorded step arguments are bounded

- Decided: 2026-10-09, in cloudfall-dev/cloudfall#63
- Rule: The investigation record keeps at most 8000 characters of finding.rootCause and of finding.why, keeping the start and marking the cut as D-6 does, and the schema bounds both; a tool call whose parsed arguments serialize to more than 8000 characters is refused before it runs, recorded as an invalid step with empty arguments, so spec.steps[].arguments only ever holds arguments that ran whole
- Why: All three come from the model's output, which can quote host output; structured arguments can't be cut and stay meaningful, so refusing keeps steps[] a true account of what ran
- Applies to: sdk/src/cloudfall/investigation.py, config/schemas/v1/agent-investigation.schema.json
- Enforced by: review

## D-9: Owner passwords reach PostgreSQL only as a SCRAM verifier

- Decided: 2026-10-30, in cloudfall-dev/cloudfall#140
- Rule: Cloudfall sets a PostgreSQL role's password only with a SCRAM-SHA-256 verifier computed on the controller; the plaintext password is never staged on the host and never sent to the server, so it can't reach the PostgreSQL log or pg_stat_activity
- Why: psql substitutes a variable into the statement text, so ALTER ROLE ... PASSWORD with the plaintext puts the password in the server's log under log_statement ddl or all, or on a failing statement; PostgreSQL stores a pre-hashed verifier as it is
- Applies to: engine/ansible/roles/cloudfall_postgresql/tasks/owner_password.yml, sdk/src/cloudfall/secrets.py, engine/src/cloudfall_engine/cli.py
- Enforced by: a regression test that the staged file holds a verifier and never the password

## D-10: Components are isolated by the systemd sandbox, and separate servers are recommended, not required

- Decided: 2026-11-02, in cloudfall-dev/cloudfall#191
- Rule: On every server, components are kept apart by each unit's systemd sandbox (own user, ProtectSystem, ProtectHome, PrivateTmp, ProtectProc, SystemCallFilter, capability and address-family limits, cgroup limits), a database role per component, secrets through systemd credentials, keys scoped by environment and a non-root deploy user; runners are ephemeral sandboxed units, denied production directories, network peers and keys when they share a server with production; separate servers for different trust levels (production and staging, build or CI and production, different owners) are recommended, and a team may place everything on one server, several independent projects included, by declaring the accepted risk in the plan; Cloudfall warns about an undeclared placement of two trust levels on one server, and warns more strongly when they belong to different owners; Cloudfall adds no container runtime and no hypervisor of its own
- Why: A Unix user alone keeps only files and signals apart; containers use the same kernel mechanisms systemd exposes and add no boundary, only a runtime to operate; a separate kernel is the one stronger boundary and comes from a separate server, but requiring it would rule out the one-server teams Cloudfall serves, and a team can accept that risk for its own projects, not for other owners; the full reasoning is in docs/decisions/2026-11-02-isolation-without-containers.md
- Applies to: engine/ansible/roles/cloudfall_deploy/templates/component.service.j2, engine/ansible/roles/cloudfall_access, server placement in plans, runner and CI setup
- Enforced by: review

## D-11: The resource every server runs is a Baseline

- Decided: 2026-11-13, in cloudfall-dev/cloudfall#198
- Rule: The kind is Baseline, stored under baselines/, referenced as Server.spec.baseline, checked by baseline.schema.json and declared with cloudfall add baseline; its storage part stays in it; the old kind ServerType, folder server-types/ and field serverType keep validating for one minor release with a deprecation warning from config validate and a command that rewrites a project to the new names, and a server that sets both serverType and baseline is an error
- Why: On Hetzner and most clouds a server type is a hardware model (CX22, AX41-NVMe), and the docs and CLI already call this resource the baseline; splitting storage out is new behaviour that needs its own spec
- Applies to: sdk/src/cloudfall/*, engine/*, config/schemas/v1/server-type.schema.json, config/schemas/v1/baseline.schema.json, config/examples/*, docs/*, cloudfall add, config validate
- Enforced by: review
