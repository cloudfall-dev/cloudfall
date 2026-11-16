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
- Enforced by: a regression test that the file staged on the host holds a verifier and never the password (since D-25 the controller's own 0600 file holds the password, which never leaves the controller)

## D-10: Components are isolated by the systemd sandbox, and separate servers are recommended, not required

- Decided: 2026-11-02, in cloudfall-dev/cloudfall#191
- Rule: On every server, components are kept apart by each unit's systemd sandbox (own user, ProtectSystem, ProtectHome, PrivateTmp, ProtectProc, SystemCallFilter, capability and address-family limits, cgroup limits), a database role per component, secrets through systemd credentials, keys scoped by environment and a non-root deploy user; runners are ephemeral sandboxed units, denied production directories, network peers and keys when they share a server with production; separate servers for different trust levels (production and staging, build or CI and production, different owners) are recommended, and a team may place everything on one server, several independent projects included, by declaring the accepted risk in the plan; Cloudfall warns about an undeclared placement of two trust levels on one server, and warns more strongly when they belong to different owners; Cloudfall adds no container runtime and no hypervisor of its own
- Why: A Unix user alone keeps only files and signals apart; containers use the same kernel mechanisms systemd exposes and add no boundary, only a runtime to operate; a separate kernel is the one stronger boundary and comes from a separate server, but requiring it would rule out the one-server teams Cloudfall serves, and a team can accept that risk for its own projects, not for other owners; the full reasoning is in docs/decisions/2026-11-02-isolation-without-containers.md
- Applies to: engine/ansible/roles/cloudfall_deploy/templates/component.service.j2, engine/ansible/roles/cloudfall_access, server placement in plans, runner and CI setup
- Enforced by: review

Status on 2026-11-15: partly built. component.service.j2 sets only User, Group, NoNewPrivileges=true and PrivateTmp=true, secrets reach the unit through the per-release EnvironmentFile (D-21) rather than systemd credentials, and deploy tasks run as root through become; the rest of the sandbox, the directives listed in docs/decisions/2026-11-02-isolation-without-containers.md, is not built yet and is tracked in cloudfall-dev/cloudfall#362.

## D-11: The resource every server runs is a Baseline

- Decided: 2026-11-13, in cloudfall-dev/cloudfall#198
- Rule: The kind is Baseline, stored under baselines/, referenced as Server.spec.baseline, checked by baseline.schema.json and declared with cloudfall add baseline; its storage part stays in it; the old kind ServerType, folder server-types/ and field serverType keep validating for one minor release with a deprecation warning from config validate and a command that rewrites a project to the new names, and a server that sets both serverType and baseline is an error
- Why: On Hetzner and most clouds a server type is a hardware model (CX22, AX41-NVMe), and the docs and CLI already call this resource the baseline; splitting storage out is new behaviour that needs its own spec
- Applies to: sdk/src/cloudfall/*, engine/*, config/schemas/v1/server-type.schema.json, config/schemas/v1/baseline.schema.json, config/examples/*, docs/*, cloudfall add, config validate
- Enforced by: review

## D-12: An Elasticsearch line ends on Elastic's End of Maintenance date

- Decided: 2026-11-14, in cloudfall-dev/cloudfall#318
- Rule: The end-of-life date in the SDK's table of supported Elasticsearch lines (S-012, Supported versions) is the End of Maintenance date Elastic publishes for the line; the 90-day ELASTICSEARCH_VERSION_ENDING warning and audit's unsupported-version finding both read that date
- Why: Elastic's end-of-life page has no single End of Life column and publishes no End of Support date for 9.x; End of Maintenance is published for every line, and it is the earlier date, so the warning comes before fixes stop
- Applies to: sdk/src/cloudfall/elasticsearch.py; elasticsearch; audit
- Enforced by: sdk/tests/test_s012_elasticsearch.py
- Zone: interpretation (notify, veto until 2026-11-15T12:29:00Z)

## D-13: A component's deployment receipt has one entry per server

- Decided: 2026-11-15, in cloudfall-dev/cloudfall#222
- Rule: A deploy writes one receipt per component, <receipts>/<component>.json, holding one entry per server with the release that server ended on; readers (cloudfall why, the operator) read that one file per component, and an older receipt that holds a single server reads as a one-entry list
- Why: With serial deploys each server overwrote the previous server's receipt, so a partial failure left no record of which servers run which release; one file per component keeps every reader on one path, while a file per server would change the file names and make every reader collect several files
- Applies to: engine/ansible/roles/cloudfall_deploy/tasks/release_receipt.yml, the receipt readers in sdk/src/cloudfall/*, cloudfall why, the operator
- Enforced by: review

## D-14: A change that makes a validating config stop validating is Breaking in the changelog

- Decided: 2026-11-15, in cloudfall-dev/cloudfall#257
- Rule: A changelog entry for a change after which a config that validated before stops validating or renders differently goes under ### Breaking, with the step a user takes before upgrading; the LoggingStack storage-pool requirement (S-014-2) is such a change
- Why: Quality bar Q-3: configs that validate today keep validating, or the change is a breaking one; [bump] reads ### Breaking as a major bump and the release notes then tell LoggingStack users to add storage.pools before upgrading
- Applies to: changelog.d/*, CHANGELOG.md, config validate
- Enforced by: review

## D-15: A component sets how many releases a deploy keeps with deployment.retainReleases

- Decided: 2026-11-15, in cloudfall-dev/cloudfall#224
- Rule: A Component's optional deployment.retainReleases (integer, at least 2, default 5) is how many releases a deploy keeps on each server; once a deploy passes its gate it removes the older ones, always keeping current and the release before it, and records what it removed in the receipt; retainUntilCleanup: true keeps validating and means cleanup is on
- Why: retainUntilCleanup is declared const true and read as a bool, so there was no number to clean up to; a per-component count lets a team with large releases or tight disks tune it, while existing configs keep validating
- Applies to: config/schemas/v1/component.schema.json, sdk/src/cloudfall/* inventory, engine/ansible/roles/cloudfall_deploy
- Enforced by: review

## D-16: Receipts and operator proposals live in a committed record/ directory

- Decided: 2026-11-15, in cloudfall-dev/cloudfall#219
- Rule: By default a project keeps its deployment receipts and operator proposals in a committed record/ directory, as decisions live in decisions/; tmp/ keeps only what can be rebuilt; cloudfall config upgrade moves an existing project's receipts and proposals from tmp/ into record/; no record written there holds a secret value
- Why: The audit trail must survive losing the management host; tmp/ is not committed, and one upgrade command moves existing projects so there is one layout in the field
- Applies to: sdk/src/cloudfall/project.py, the receipt and proposal stores, cloudfall init, cloudfall config upgrade, engine receipt paths, docs/operator-guide.md
- Enforced by: review

## D-17: Agents may change the CI workflows for a Debian converge job and hardening

- Decided: 2026-11-15, in cloudfall-dev/cloudfall#227
- Rule: CI converges a Debian container with systemd through bootstrap, baseline, services, a deploy, a rolled-back failing deploy, a backup and its restore drill, then reruns asserting zero changes; workflows sync with uv sync --locked, pin actions by commit SHA, set a least-privilege permissions block per job, syntax-check every playbook by glob, and Dependabot updates actions and dependencies
- Why: No job converged a role on a real Debian, so deploy and rollback fixes were proven only by unit tests; loose pins and broad token permissions are the workflows' attack surface; the repo is public, so GitHub-hosted runners are allowed
- Applies to: .github/workflows/*, .github/dependabot.yml
- Enforced by: review

## D-18: A mirrored volume meets an expendable requirement

- Decided: 2026-11-15, in cloudfall-dev/cloudfall#323
- Rule: An existing raid1 volume over its pool's primary and secondary satisfies a volume requirement of placement expendable: the volume provider keeps it, grows it on both physical volumes, and audit checks it as raid1; a linear volume never satisfies a mirrored requirement, and a raid1 volume on other disks is still refused
- Why: S-012 makes an Elasticsearch node's volume mirrored for a cluster of one and expendable for two or more, while S-014 refused any change of an existing volume's type, so growing a one-node service refused its first node; a mirror is stronger than what an expendable volume asks, nothing on the host has to change when the cluster grows, and converting a raid1 volume to linear in place would mean an lvconvert the provider never runs
- Applies to: sdk/src/cloudfall/requirements.py (VolumeRequirement.held_as), sdk/src/cloudfall/volumes.py, sdk/src/cloudfall/audit.py; S-012 What each node needs, S-014 Refusals
- Enforced by: sdk/tests/test_s014_volumes.py

## D-19: Operator autonomy is earned per environment

- Decided: 2026-11-15, in cloudfall-dev/cloudfall#351
- Rule: The history an autonomy grant is judged on, and the suspension rule, count only settled receipts whose operation_server is in the same environment as the proposal's server; a receipt of an undeclared server counts for no environment
- Why: The grant lives in the policy chosen by the server's environment; servers of one environment run the same declarations, so per-server history would make each new server start from zero
- Applies to: sdk/src/cloudfall/operator.py, operator autonomy
- Enforced by: review
- Zone: interpretation (notify, veto until 2026-11-16T16:10:00Z)

## D-20: An approval is bound to what was proposed

- Decided: 2026-11-15, in cloudfall-dev/cloudfall#352
- Rule: A decision record binds the sha256 of its playbook and verify playbook and, in a git project, the HEAD commit and a digest of uncommitted changes outside the decisions, record/ and tmp/ directories; approve recomputes them and the diff's sha256 before anything runs and refuses any difference, and refuses a record with no binding, asking for a new proposal; a dirty tree at propose time is allowed
- Why: The approval must be of what the approver read; only a change after the proposal breaks that, and an agent usually proposes right after editing
- Applies to: sdk/src/cloudfall/decision.py, decisions approve, decision record
- Enforced by: review
- Zone: contract-change (notify, veto until 2026-11-16T16:10:00Z)

## D-21: Each release has its own environment file

- Decided: 2026-11-15, in cloudfall-dev/cloudfall#353
- Rule: A deploy writes the component's environment to shared/env/<component>.<release>.env and the release's unit, release command and health check read that file, so a rescue or rollback that restores the previous unit restores the previous environment; a redeploy copy of the same release gets its own file, shared/env/<component>.<release>.redeploy-<time>.env, and a deploy without --env-file copies in the file the previous release reads; the legacy shared/env/<component>.env is never written or removed by a deploy, and retention removes a removed release's file
- Why: Code, unit and environment must move together, or a rollback after a bad environment starts the previous release on the bad one
- Applies to: engine/ansible/roles/cloudfall_deploy/*, deploy, rollback
- Enforced by: review
- Zone: contract-change (notify, veto until 2026-11-16T16:10:00Z)

## D-22: Root's authorized_keys is shared, not exclusive

- Decided: 2026-11-15, in cloudfall-dev/cloudfall#354
- Rule: The baseline adds declared active keys and removes declared revoked keys from root's authorized_keys, and leaves undeclared keys in place
- Why: Brownfield servers carry keys Cloudfall never declared (a provider's rescue key, a team member's); an exclusive file would lock them out on adoption
- Applies to: engine/ansible/roles/cloudfall_access/*, SshPublicKey lifecycle
- Enforced by: review
- Zone: interpretation (notify, veto until 2026-11-16T16:10:00Z)

## D-23: Releases wait for converge

- Decided: 2026-11-16, in cloudfall-dev/cloudfall#361
- Rule: release.yml and publish.yml run or require converge.yml on the same commit, and no release candidate or release is cut or published unless that converge run passed
- Why: converge is the only check that runs the roles on a real Debian; a candidate cut from a commit whose converge failed ships broken roles, and the extra ~10 minutes per release is accepted
- Applies to: .github/workflows/release.yml, .github/workflows/publish.yml, .github/workflows/converge.yml
- Enforced by: review

## D-24: Raw diffs and run logs of declared operations stay out of git

- Decided: 2026-11-16, in cloudfall-dev/cloudfall#357
- Rule: A declared operation's raw diff, execution log and verification log are written under the project's ignored tmp/ directory, and the committed decision record keeps only each file's sha256 and size; approve refuses when the raw diff is missing or its sha256 differs (D-20)
- Why: a team playbook can print a secret Cloudfall doesn't know about (no no_log, diff: true), so only keeping its output out of the commit guarantees no record holds a secret value; the cost is that another clone sees hashes, not the diff text, and can't approve without the raw diff
- Applies to: sdk/src/cloudfall/decision.py, decisions/, decision records
- Enforced by: review

## D-25: A PostgreSQL owner password converges by reusing the stored salt

- Decided: 2026-11-16, in cloudfall-dev/cloudfall#398
- Rule: To set a PostgreSQL role's password, the play reads the role's stored SCRAM-SHA-256 salt and iteration count from the host, the controller computes the verifier with them, and the ALTER ROLE runs and reports a change only when that verifier differs from the stored one; when the role has no SCRAM verifier a fresh salt is used; the plaintext stays on the controller as D-9 requires
- Why: a fresh random salt on every run made every converge of an owner with a passwordRef report a change, so the second converge could never change nothing (D-17); salts aren't secret, and this needs no new secret and no weaker hash
- Applies to: engine/ansible/roles/cloudfall_postgresql/*, sdk/src/cloudfall/secrets.py, PostgreSQL passwords
- Enforced by: review
