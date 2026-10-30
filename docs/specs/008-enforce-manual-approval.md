# S-008: Enforce manual approval

status: approved
serves: G-2

## Problem

`Application.spec.approval` accepts `automatic` or `manual`
(`config/schemas/v1/application.schema.json`). `sdk/src/cloudfall/inventory.py` parses it into
`DeploymentApproval` (`sdk/src/cloudfall/domain.py`) and writes it into the rendered inventory,
and nothing reads it: `cloudfall deploy --yes` deploys a `manual` application exactly as it
deploys an `automatic` one, so an agent through the `deploy` MCP tool or a CI job that passes
`--yes` approves its own deploy. The example `config/examples/applications/crm.yaml` declares
`manual`, and the Render importer (`sdk/src/cloudfall/importer.py`) writes `"approval": "manual"`
for every application it imports, so imported projects look gated and aren't. The CI deploy guide
(`docs/ci-deploy-guide.md`) tells teams to use GitHub's required reviewers meanwhile, and that
approval never reaches Cloudfall's record (cloudfall-dev/cloudfall#106).

## Behaviour

**The approval record**

A person approves one release of one component with a new command:

```sh
uv run cloudfall release approve crm-backend --release 20260101T000000Z-abcdef0
```

- `release approve COMPONENT --release RELEASE` takes `--artifacts` (default `tmp/artifacts`, as
  `deploy`), `--approvals` (default `decisions/releases`), `--valid-for` (hours, an integer from
  1 to 168, default 24) and `--approver` (default `USER`, as `decisions approve`)
- Before asking anything it checks what `deploy` without `--yes` checks: the component exists,
  and the artifact's metadata, identity and digest verify (`verify_artifact` in
  `sdk/src/cloudfall/lifecycle.py`). A failure exits `PRECONDITION` with the lifecycle code
  (`lifecycle_artifact_missing`, `lifecycle_artifact_digest_mismatch`, ...) and writes nothing
- It refuses a component whose application declares `automatic` with `PRECONDITION`
  (`release_approval_not_required`): such a record would gate nothing
- It then asks the person to type the release id at the terminal (`ctx.attest`, like
  `decisions approve` and `operator approve`). The prompt names the application, the component,
  the release, the git commit and archive SHA-256 from the artifact metadata, the servers the
  deploy would change, and when the approval expires. Off a terminal it exits 4 with
  `PERSON_REQUIRED`, a mistyped id exits 4 with `ATTESTATION_MISMATCH`, and no flag answers it,
  `--yes` included. Either way nothing is written
- It deploys nothing. It writes one record and exits 0 with the record and its path

The record is a `ReleaseApproval` (`config/schemas/v1/release-approval.schema.json`) at
`<approvals>/<metadata.id>.json`, where `metadata.id` is `approval-<UTC time>-<16 hex>` like an
operator proposal id. Its `spec` holds `application`, `component`, `release`, `artifactSha256`,
`gitCommit`, `servers` (the component's declared servers when approved), `approver`, `approvedAt`,
`expiresAt` and `via` (`terminal`, the `ApprovalChannel` value). The default folder sits beside
the decision records under `decisions/`, so the team commits it with the project; `decisions list`
reads only the top level of `decisions/`, so the subfolder does not disturb it.

`release approve` is a person's command: it is declared `mcp=False` and `requires_person=True`,
is not in `PROJECT_COMMANDS` (`sdk/src/cloudfall/agent_server.py`), and no MCP tool writes into
the approvals folder. As with the other approve commands, this is a speed bump, not a boundary:
a process with the operator's shell and file access can fake a terminal or write the file.

**Which approval counts**

When the component's application declares `manual`, `deploy --yes` looks in `--approvals` for a
record that:

- names the same application, component and release
- names the `artifactSha256` that `verify_artifact` computes for the archive being deployed now
- names exactly the component's declared servers now (a config change that moves the component
  to other servers needs a new approval)
- has an `expiresAt` later than now

Every other record for that component and release is reported with why it doesn't count:
`digest_mismatch`, `servers_changed` or `expired`. When several count, the latest `approvedAt`
is used. A record is not used up by a deploy: a deploy that fails its health gate rolls back on
its own, and the same release may be retried until the approval expires. The spec 001 notion of a
stale proposal (a newer observation of a host in the basis) does not apply: an approval has no
observation basis. The server check above is its counterpart.

**What `deploy` answers**

- Without `--yes`, the plan exits 0 as today and gains an `approval` object. For a `manual`
  application: `policy: manual`, `status` (`approved`, `missing`), the counting record's `path`,
  `approver` and `expiresAt` when there is one, the not-counting records with their reasons, and
  when none counts, `command`: the exact `cloudfall release approve <component> --release
  <release>` to run. For an `automatic` application: `policy: automatic` and nothing else. The
  plan still runs no `ansible-playbook`
- With `--yes` and no counting record, `deploy` exits 4 `PRECONDITION` with
  `context.code: deployment_approval_missing`, before the inventory render or any playbook. The
  message names the application, the component and the release and says a person approves it
  with that command; `context` carries `application`, `component`, `release`, `command` and the
  not-counting records with their reasons. No release receipt is written
- With `--yes` and a counting record, `deploy` runs as today and its result gains `approval` with
  the record's `path`, `approver` and `approvedAt`
- An approvals file that isn't valid JSON or doesn't match its schema stops a `manual` deploy (plan
  or `--yes`) with `RECORD_INVALID`, naming the file, like a broken decision record. `deploy` gains
  `RECORD_INVALID` in its exit codes
- For an `automatic` application `deploy` never reads the approvals folder and deploys as today

The `deploy` MCP tool gets the same answer: `cloudfall mcp serve` binds `approvals` in
`bound_arguments`, so a call can't point `deploy` at a folder it chose, and
`_PROJECT_INSTRUCTIONS` says that deploying a `manual` application needs `cloudfall release
approve`, a command a person runs, as it already says for `operator approve`.

**`migrate`, `rollback` and `restart`**

- `migrate`'s `deploy:<component>` step goes through the same check. A missing approval pauses
  the migration at that step (`deployment_approval_missing` joins `_PAUSE_CODES` in
  `sdk/src/cloudfall/migrate.py`): `migrate --yes` exits `PARTIAL_FAILURE` with
  `context.code: deployment_approval_missing`, the built release and the `release approve`
  command. Once a person approves it, `migrate --yes` resumes at that step with the release the
  completed `build:<component>` step recorded, without building again
- `rollback` needs no approval. It only activates a release still retained on the servers, which
  went live through `deploy`; it is the recovery path the deploy role already takes on its own
  when a health gate fails; and making it wait for a person at a terminal would block recovery
  in an incident, when it matters most. For a `manual` application its plan carries
  `approval: {"policy": "manual", "required": false, "reason": ...}` saying so
- `restart` ships no new code and needs no approval

**Migrating existing projects (Breaking)**

This refuses `deploy --yes` and pauses `migrate --yes` for every project that declares `manual`
and deploys today, the example and every Render import included. The changelog fragment
`changelog.d/106-enforce-manual-approval.md` has a `### Breaking` entry that names both ways out:

- Keep `manual` and approve each release first: build it, run `cloudfall release approve`
  at a terminal, commit the record under `decisions/releases/` when CI deploys, then deploy
- Switch the application to `approval: automatic` when another gate (GitHub's required
  reviewers, say) is the real one, so the record no longer claims a gate Cloudfall doesn't hold

The Render importer keeps writing `manual`, and `crm.yaml` keeps `manual`. Docs change with it:

- `docs/ci-deploy-guide.md`: "Approval: who presses the button" says `deploy` enforces `manual`,
  and shows the approve-then-deploy flow for a `manual` production application: a build job
  uploads `tmp/artifacts/<component>/`, the person downloads it, runs `release approve`, commits
  the record and dispatches the deploy job, which downloads the same artifact and finds the record
  in its checkout. Staging and other `automatic` applications keep the one-job workflow
- `docs/render-migration-guide.md`, `README.md` and `sdk/README.md`: the `migrate --yes` and
  `deploy --yes` examples show the `release approve` step for the `manual` application they use
- `docs/operator-guide.md`: "Through an agent" lists `release approve` with the commands no tool
  stands in for

Touches `sdk/src/cloudfall/app.py` (`deploy`, `rollback`, `migrate`, new `release approve`),
`sdk/src/cloudfall/lifecycle.py`, `sdk/src/cloudfall/migrate.py`, a new
`sdk/src/cloudfall/release_approval.py`, `sdk/src/cloudfall/inventory.py`,
`sdk/src/cloudfall/domain.py`, `sdk/src/cloudfall/agent_server.py`,
`config/schemas/v1/release-approval.schema.json`, `docs/ci-deploy-guide.md`,
`docs/render-migration-guide.md`, `docs/operator-guide.md`, `README.md`, `sdk/README.md`,
`changelog.d/`, and the `deploy`, `rollback` and `migrate` MCP tools.

## Acceptance criteria

- S-008-1: `deploy crm-backend --release R --yes` on a project whose application declares `approval: manual` and whose approvals folder holds no record exits 4 `PRECONDITION` with `context.code: deployment_approval_missing`, a message naming the application, `crm-backend` and `R`, and `context.command` equal to `cloudfall release approve crm-backend --release R`; no `ansible-playbook` runs and no release receipt is written
- S-008-2: `release approve crm-backend --release R` at a terminal, with `R` typed back, exits 0 and writes one file under `decisions/releases/` that validates against `release-approval.schema.json` and holds the application, component, release, the artifact metadata's `archiveSha256` and `gitCommit`, the component's servers, the approver, `approvedAt`, an `expiresAt` 24 hours later, and `via: terminal`
- S-008-3: After S-008-2, `deploy crm-backend --release R --yes` deploys and its result carries `approval` with that record's `path`, `approver` and `approvedAt`
- S-008-4: `release approve` with stdin from a pipe exits 4 `PERSON_REQUIRED`, a mistyped id exits 4 `ATTESTATION_MISMATCH`, both with `--yes` and without, and neither writes a file
- S-008-5: `cloudfall mcp serve` for a project lists no `release_approve` tool, and its `deploy` tool called with `yes: true` for a `manual` application without a record returns `PRECONDITION` with `deployment_approval_missing`
- S-008-6: A record for release `R` doesn't count when the archive at `tmp/artifacts/crm-backend/R.tar.gz` and its metadata now give another SHA-256, when the component's declared servers differ from the record's `servers`, or when `expiresAt` has passed; `deploy --yes` refuses with `deployment_approval_missing` and lists the record with `digest_mismatch`, `servers_changed` or `expired`
- S-008-7: A record naming another release or another component doesn't count for `R`
- S-008-8: `deploy` without `--yes` exits 0, runs no `ansible-playbook`, and shows `approval` with `policy: manual`, `status: missing` and `command` before S-008-2, and `status: approved` with the record's `path`, `approver` and `expiresAt` after it
- S-008-9: For an `automatic` application, `deploy --yes` deploys without any record, its plan shows `approval: {"policy": "automatic"}`, and a file in the approvals folder that isn't valid JSON doesn't stop it
- S-008-10: For a `manual` application, a file in the approvals folder that isn't valid JSON or doesn't match `release-approval.schema.json` makes `deploy` exit `RECORD_INVALID` naming the file
- S-008-11: `release approve` exits `PRECONDITION` with `release_approval_not_required` for a component of an `automatic` application, and with the lifecycle code (`lifecycle_artifact_missing`, `lifecycle_artifact_digest_mismatch`) for a missing or tampered artifact, before asking for the release id and without writing a file
- S-008-12: `release approve --valid-for 0` and `--valid-for 169` are refused as usage errors naming `--valid-for`; `--valid-for 2` writes an `expiresAt` two hours after `approvedAt`
- S-008-13: `rollback --yes` of a `manual` application's component runs without any record, and its plan shows `approval` with `required: false` and a reason
- S-008-14: `migrate --build crm-backend=main --yes` on a `manual` project exits `PARTIAL_FAILURE` paused at `deploy:crm-backend` with `context.code: deployment_approval_missing` and the built release; after `release approve` for that release, `migrate --yes` resumes, runs no `artifact build`, and completes the `deploy:crm-backend` step
- S-008-15: A deploy that fails its health gate leaves its approval record unchanged, and a second `deploy --yes` of the same release before `expiresAt` uses the same record
- S-008-16: `changelog.d/106-enforce-manual-approval.md` has a `### Breaking` entry naming `deploy --yes` and `migrate --yes` on `manual` applications and both migrations (approve with `cloudfall release approve`, or switch to `approval: automatic`), and `docs/ci-deploy-guide.md` no longer says that no command acts on `approval`

## Out of scope

- Listing approvals in `decisions list` or `cloudfall why`: the records are in the repository and the deploy plan shows the one that counts. A later spec can bring every record kind into `why`
- Approval inside GitHub's deployment history: GitHub's required reviewers remain a separate, outer gate. Reading GitHub's approvals into Cloudfall's record needs a GitHub API client and a trust model for it
- Approving by git commit instead of archive digest, so CI could rebuild after the approval: two builds of one commit give different archives (and with S-002 build steps, different contents), so a commit approval would approve bytes nobody checked. Revisit if builds become reproducible
- Single-use approvals: a CI runner can't write the record back to the repository durably, and a health-gate failure would then need a second approval for a retry
- An `OperatorPolicy` that licenses deploys autonomously: the policy schema grants only convergence operations, by design
- Gating `restart`, `data copy`, backups or convergence on `approval`: the field is about deploying new code
- A stronger boundary than the terminal attestation (signed approvals, a second OS user): it applies to every approve command at once and belongs in its own spec
- Changing the importer to write `automatic`: an import has no other gate to point to, so `manual` stays the safe default

## Decisions relied on

- none

## Issues

- cloudfall-dev/cloudfall#160: S-008-2, S-008-4, S-008-11, S-008-12
- cloudfall-dev/cloudfall#163: S-008-1, S-008-3, S-008-5, S-008-6, S-008-7, S-008-8, S-008-9, S-008-10, S-008-15, S-008-16
- cloudfall-dev/cloudfall#165: S-008-13, S-008-14

## Verification
