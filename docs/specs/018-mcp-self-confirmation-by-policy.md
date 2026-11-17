# S-018: MCP self-confirmation by policy

status: approved
serves: G-2

## Problem

Over `cloudfall mcp serve` in project mode the agent confirms its own changes
(cloudfall-dev/cloudfall#391). The server's instructions (`_PROJECT_INSTRUCTIONS` in
`sdk/src/cloudfall/agent_server.py`) tell it to call `deploy`, `rollback`, `restart`,
`migrate`, `data_copy` and `domains_apply` again with `yes: true`, and the `converge_*` tools
with `confirm_destructive: true`. Nothing but the agent's own second call stands between the
plan and the run. The `converge_*` handlers in `sdk/src/cloudfall/app.py`
(`_converge_baseline_handler`, `_converge_services_handler`, `converge_domain_routes`) write
no record of who confirmed, while the same playbook proposed by the operator needs a person
or an `OperatorPolicy` grant. G-2 asks for a verify step for every operation and an audit
entry for every decision; Q-2 promises a gate the agent does not control.

The maintainer chose option 2 of #391: self-confirmation stays only for operation classes an
`OperatorPolicy` grants, per environment; everything else only proposes, and each confirmed
run writes a record naming the agent as approver. Until this is built, the docs say the
confirmation is the agent's own (#391, option 3's wording).

## Behaviour

### Which tools are gated

In project mode, these MCP tools change servers and are gated. Each maps to one operation
class, the `kind` an `OperatorPolicy` grant names:

| Tool | Confirmed by | Operation class |
|---|---|---|
| `deploy` | `yes: true` | `deploy` |
| `rollback` | `yes: true` | `rollback` |
| `restart` | `yes: true` | `restart` |
| `migrate` | `yes: true` | `migrate` |
| `data_copy` | `yes: true` | `data-copy` |
| `domains_apply` | `yes: true` | `domains-apply` |
| `converge_baseline` | `confirm_destructive: true` | `converge-baseline` (exists) |
| `converge_services` | `confirm_destructive: true` | `converge-services` (exists) |
| `converge_domains` | `confirm_destructive: true` | `converge-domains` |

`converge-baseline` and `converge-services` are the operator's existing `OperationKind`
values (`sdk/src/cloudfall/operator.py`); the other seven are new values of the same enum, of
`spec.autonomy.operations[].kind` in `config/schemas/v1/operator-policy.schema.json`, and of
`spec.operation.kind` in `config/schemas/v1/operator-proposal.schema.json`.

Not gated, because they change no server: the read-only tools, `add_*`, `config_upgrade`,
`import_*`, `secrets_render`, `build_artifact` (controller only), `backup_run` and
`backup_verify` (they add a backup or restore into scratch, and change no running service),
`operator_list` and `operator_show`. Fleet mode is unchanged: its operation tools already
only propose. The CLI is unchanged: `cloudfall deploy --yes` typed in a shell runs as today.

A call without its confirmation (no `yes`, or a `converge_*` preview) answers the plan as
today and writes nothing. Its plan gains an `approval` object saying in advance what a
confirmed call would do: `selfConfirm` (true or false), `environments` (the environments of
the servers the run changes), and, per environment, the `policy` that applies or none, and the
`reason` from the grant check below.

### Which environment, and what a grant is

The servers a run changes are the ones the command already targets: a component's declared
servers for `deploy`, `rollback` and `restart`; every server `migrate`'s plan touches; the
service's server for `data_copy`; the Domain's proxy server for `domains_apply` and for
`converge_domains` with `domain`; every declared server for `converge_baseline`,
`converge_services` and `converge_domains` without `domain`. A server's environment is its
`Server.spec.environment`, and the policy is the one whose `spec.environment` names it, as
`autonomous_pass` already matches them.

A grant is an entry of that policy's `spec.autonomy.operations` with the run's operation
class. The check is the operator's own `autonomy_decision`, unchanged in its rules:

- the history is the settled records of that class whose server is in the policy's
  environment (D-19), agent runs and person-approved agent proposals included, and must hold
  at least `requiredVerifiedRuns` verified ones
- the latest settled record of that class in that environment being `failed` or
  `interrupted` suspends the grant until a person-approved run verifies
- `quietHours` withhold it
- `maxAutonomousPerHour` withholds it, counting runs with `approval.mode` `autonomous` or
  `agent` in the last hour
- a proposal record that cannot be read withholds every grant (incomplete history)

A run is self-confirmed only when every environment it touches grants it. One environment
without a policy, without the class, or withheld, and the whole run proposes.

**No `OperatorPolicy` declared: everything proposes.** That is the safe default the
maintainer chose. A project that wants today's one-call flow in staging declares a policy for
staging that grants the classes it wants.

### What a confirmed call returns

**Granted.** The command runs as today, and before its first playbook one record is written
to the proposal store (`--proposals`, bound by `bound_arguments`, `record/operator/proposals`
by default per D-16), status `running`, then updated to the outcome. The tool's result gains
`approval`: `mode: agent`, `policy`, `grant` (the class), and `record` (the record's path).

**Not granted.** Nothing runs on any server. One record is written with status `proposed`,
and the tool answers `PRECONDITION` with `error.context.code: operator_approval_required`,
`context.proposal` (its id), `context.command` (`cloudfall operator approve <id>`) and
`context.reasons` (one line per environment, the reason from the grant check). The message
says that a person approves it with that command. A call repeating the same tool, arguments
and servers while a proposal for it is `proposed` returns that proposal's id and writes no
second record.

The checks that come before a run still come first, unchanged: argument and config
validation, S-008's release approval for a `manual` application
(`deployment_approval_missing`), locks. A call they refuse writes no record.

### The record

A self-confirmed run and an agent proposal are both an `OperatorProposal`, so the operator's
store, `operator list`, `operator show`, `operator approve` and the D-19 history read them
without a new kind. The schema gains, additively:

- `spec.trigger.kind: agent`, with `spec.trigger.request`: `tool` (the MCP tool name),
  `arguments` (the call's arguments, the bound ones left out) and `reason` (below). The
  fingerprint is the sha256 prefix of tool, arguments and servers
- `spec.operation.servers` and `spec.operation.environments`: every server the run changes and
  their environments. `spec.operation.server` stays required for `alert` and `drift`
  proposals only
- `spec.approval.mode: agent`, with `policy`, `grant` (the class) and `client` (the
  `clientInfo.name` and `version` the MCP client sent at initialize, or `unknown`)
- `spec.binding`: `planSha256` (the sha256 of the plan the command computed for this run, the
  same plan an unconfirmed call returns), and in a git project `gitCommit` and
  `worktreeDigest`, computed as D-20 computes them for a decision record

Each gated tool gains an optional `reason` input (a string of at most 1000 characters): why
the agent asks. It becomes `spec.trigger.request.reason` and `spec.diagnosis.summary`; without
it the summary says the agent gave no reason. `spec.diagnosis.evidence` lists the plan's
summary lines.

**The verify step.** The outcome is `verified` only when the run's own check passed, and
`failed` otherwise:

- `deploy`, `rollback`, `restart`: the health gate passed
- `migrate`: every step completed
- `data_copy`: the copy's receipt reports it verified
- `domains_apply`, `converge_domains`: a `domains observe` of the changed Domains matches
  what was declared
- `converge_baseline`, `converge_services`: an `audit` of the changed servers reports no
  failing check

### Approving a proposal

`cloudfall operator approve <id>` on an agent proposal does what it does for the operator's:
the person types the id at a terminal (`PERSON_REQUIRED` off one), and the record gets
`approval.mode: human`, `via: terminal`. It then recomputes the plan from the stored tool and
arguments, and `gitCommit` and `worktreeDigest`, and refuses any difference before anything
runs, with `PRECONDITION` and `context.code: proposal_binding_changed`, naming what changed
and asking for a new proposal (the agent calls the tool again). Otherwise it runs the stored
request as the confirmed command would, through the same executor, with the verify step
above, and S-008's release approval still applies. A verified person-approved run counts in
the history that earns the grant, so a class is earned in an environment the way the operator
earns it.

The operator's `autonomous_pass` leaves `agent` proposals alone: only a person approves them,
or the agent calls again once a grant covers the run.

### What `why` shows

`cloudfall why` reads the proposal store as well as `decisions/` (a new `--proposals` flag,
default the store's directory) and answers each agent record and agent proposal like a
decision: `--host` matches any of its `operation.servers`, `--status` its status, and each
answer names the tool, the class, the approver (`agent` with its `client`, or the person),
the policy and grant, the reason, `planSha256` and the outcome. An unreadable record is
skipped with the `RECORD_UNREADABLE` warning, as for decisions.

### Compatibility (S-017)

No MCP tool name, exit code or record field changes or goes. The new operation classes,
trigger kind, approval mode, record fields, plan key and `reason` input are additive. The
change in behaviour is not: a call that ran with `yes: true` or `confirm_destructive: true`
now proposes unless a grant covers it, so existing agents and scripts see `PRECONDITION`
where they saw a run. It therefore:

- ships in a minor release, never a patch
- has a `### Breaking` changelog entry in `changelog.d/` naming the gated tools, the
  `operator_approval_required` refusal, `cloudfall operator approve`, and how to keep the
  one-call flow (declare an `OperatorPolicy` for that environment granting those classes)
- has no deprecation window: a window would keep open the gap the change closes
- updates `_PROJECT_INSTRUCTIONS`, so an agent learns at initialize that a confirmed call
  may answer with a proposal id, and that reading the plan's `approval` tells it in advance
- updates `docs/operator-guide.md`, `ARCHITECTURE.md`, `docs/brownfield-design.md` and
  `docs/compatibility.md` once built, replacing #391's interim wording

Touches `sdk/src/cloudfall/agent_server.py`, `sdk/src/cloudfall/app.py` (the gated commands
and `converge_*` handlers, `why`), `sdk/src/cloudfall/operator.py` (`OperationKind`,
`ApprovalRecord`, `OperatorProposal`, `autonomy_decision`, `autonomous_pass`, the executor),
`sdk/src/cloudfall/inventory.py` (`OperatorPolicyInventory`), `sdk/src/cloudfall/why.py`,
`sdk/src/cloudfall/decision.py` (the D-20 binding, reused),
`config/schemas/v1/operator-policy.schema.json`,
`config/schemas/v1/operator-proposal.schema.json`, `changelog.d/` and the docs above.

## Acceptance criteria

- S-018-1: In a project with no `OperatorPolicy`, each of `deploy`, `rollback`, `restart`, `migrate`, `data_copy` and `domains_apply` called over `cloudfall mcp serve` with `yes: true` returns `PRECONDITION` with `error.context.code: operator_approval_required`, `context.proposal`, `context.command` equal to `cloudfall operator approve <that id>` and a `context.reasons` line naming the environment and "no operator policy"; no `ansible-playbook` process starts and no release, deployment or migration receipt is written
- S-018-2: In the same project, `converge_baseline`, `converge_services` and `converge_domains` called with `confirm_destructive: true` return the same refusal, and no `ansible-playbook` process starts
- S-018-3: Each refusal of S-018-1 and S-018-2 writes exactly one file to the proposal store that validates against `operator-proposal.schema.json`, with `status: proposed`, `trigger.kind: agent`, `trigger.request.tool` and `trigger.request.arguments` of the call, `operation.kind` the tool's class from the table, `operation.servers` and `operation.environments`, and `binding.planSha256`
- S-018-4: The same refused call made twice while its proposal is `proposed` returns the same `context.proposal` and leaves one file in the store
- S-018-5: With an `OperatorPolicy` for `staging` granting `deploy` with `requiredVerifiedRuns` met, `deploy` with `yes: true` for a component whose servers are all in `staging` runs the deploy, exits 0, and its result carries `approval` with `mode: agent`, the policy id, `grant: deploy` and `record`, the path of a file in the proposal store
- S-018-6: The record of S-018-5 validates against the schema and holds `trigger.kind: agent` with the tool, its arguments and the given `reason`, `approval.mode: agent` with `policy`, `grant` and `client`, `binding.planSha256` equal to the sha256 of the plan the same call without `yes` returns, `gitCommit` and `worktreeDigest` in a git project, `operation.servers`, `operation.environments`, and `status: verified` with an `outcome` once the health gate passed
- S-018-7: The record of a granted run is on disk with `status: running` before the first `ansible-playbook` starts, and a run whose health gate fails ends with `status: failed`
- S-018-8: With the `staging` grant of S-018-5 and no policy for `production`, `deploy` with `yes: true` for a component with one server in each environment refuses with `operator_approval_required` and a `context.reasons` line naming `production`, and nothing runs on either server
- S-018-9: The grant is withheld, and the call refuses with `operator_approval_required` and a proposal, when the policy does not list the class, when fewer than `requiredVerifiedRuns` verified records of that class exist in that environment (records of another environment do not count), when the latest settled record of that class in that environment is `failed` or `interrupted`, during `quietHours`, when `maxAutonomousPerHour` runs with `approval.mode` `autonomous` or `agent` ran in the last hour, and when a file in the proposal store cannot be read; each `context.reasons` line names its cause
- S-018-10: A call without `yes` (or a `converge_*` call without `confirm_destructive`) writes no file and its plan carries `approval` with `selfConfirm`, `environments`, and per environment the `policy` or none and the `reason`; `selfConfirm` is true exactly when the confirmed call would run
- S-018-11: `cloudfall operator approve <id>` at a terminal, the id typed back, on a proposal of S-018-1 for `deploy` runs that deploy with the stored arguments, records `approval.mode: human` with `via: terminal`, and ends `verified` when the health gate passes; off a terminal it exits 4 `PERSON_REQUIRED` and runs nothing
- S-018-12: After a proposal of S-018-1, a change to the component's config, a new commit, or a change to an uncommitted file outside `decisions/`, `record/` and `tmp/` makes `operator approve <id>` refuse with `PRECONDITION` and `context.code: proposal_binding_changed` naming what changed, before any `ansible-playbook` starts, and the record stays `proposed`
- S-018-13: A verified person-approved agent proposal of class `deploy` in `staging` counts toward `requiredVerifiedRuns` of the `staging` grant, so with `requiredVerifiedRuns: 1` the next `deploy` with `yes: true` in `staging` runs self-confirmed
- S-018-14: `cloudfall operator run` (its autonomous pass) with a policy granting `deploy` leaves an agent proposal `proposed` and lists it as withheld with a reason saying only a person approves it
- S-018-15: For an application declaring `approval: manual` with no release approval, a granted `deploy` with `yes: true` refuses with `deployment_approval_missing` as S-008 says and writes no proposal record
- S-018-16: `cloudfall why --host <server>` lists the records of S-018-5 and S-018-1 for that server, each naming the tool, the class, the approver (`agent` with its client, or `human`), the policy and grant when there is one, the reason, `planSha256` and the status; `--status verified` keeps only the first
- S-018-17: Each tool's own check decides `verified`: a `converge_services` run whose following `audit` of the changed servers reports a failing check ends `failed`, and a `domains_apply` run whose `domains observe` matches ends `verified`
- S-018-18: `cloudfall deploy <component> --release R --yes` from the CLI in a project with no `OperatorPolicy` runs as today and writes no agent record; `cloudfall mcp serve` on a repository (fleet mode) lists the same tools as before
- S-018-19: `operator-policy.schema.json` accepts each of the nine classes as `autonomy.operations[].kind`, every `OperatorPolicy` and `OperatorProposal` in `config/examples` and in the S-017-11 fixtures still validates, and the MCP tool names and exit codes checked by S-017-12 are unchanged
- S-018-20: `_PROJECT_INSTRUCTIONS` names `operator_approval_required`, `cloudfall operator approve` and the plan's `approval.selfConfirm`, and no longer says that a second call with `yes: true` runs the change; `changelog.d/` holds a `### Breaking` entry naming the gated tools, `operator_approval_required`, `cloudfall operator approve` and declaring an `OperatorPolicy` to keep the one-call flow

## Out of scope

- Gating the CLI: `cloudfall deploy --yes` in a shell is a person's command; an agent with
  shell access can run it, and `docs/operator-guide.md` already calls the terminal check a
  speed bump, not a boundary. A stronger boundary (signed approvals, a second OS user) is the
  separate spec S-008 already left out
- `cloudfall-engine playbook run`, a mutating entry point with no gate (#391): it is the
  engine's, not the MCP server's, and needs its own issue
- Limiting a `converge_*` run to one environment with a new input, so a run over a mixed
  fleet could be granted in staging alone: a later contract addition if mixed fleets need it
- An `operator approve` channel other than the terminal (a chat button, the dashboard): the
  dashboard stays read-only (the UI scope decision), and other channels are their own spec
- Rewriting the operator loop so it runs agent proposals: they wait for a person or a grant
- The interim wording of #391's option 3 in `ARCHITECTURE.md`, `docs/product.md` and
  `docs/brownfield-design.md`: its own pull requests, before this is built

## Decisions relied on

- D-16: agent records and proposals are written to the committed proposal store under
  `record/`
- D-19: a grant's history and its suspension count only records of servers in the policy's
  environment; this spec reuses that rule for agent runs
- D-20: the binding of a proposal to what was proposed (HEAD commit and the digest of
  uncommitted changes outside `decisions/`, `record/` and `tmp/`), recomputed at approve and
  refused on any difference; this spec applies it to agent proposals, with the plan's sha256
  in place of the playbook's

## Issues

## Verification

No criterion is verified yet.
