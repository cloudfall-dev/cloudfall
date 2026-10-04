# Brownfield design: Ansible is the hand, the agent is the brain, Cloudfall is the conscience

Status: proposed concept. Nothing in this document is implemented. It
re-roots Cloudfall on an existing Ansible inventory instead of Cloudfall's
own resource documents. It builds on the decision in
[no Ansible replacement](decisions/2026-09-14-no-ansible-replacement.md)
and changes the [project vocabulary](../ARCHITECTURE.md): the fleet is no
longer declared by Cloudfall, it is read from Ansible.

## The product

Cloudfall is the operator's record for self-hosted servers run by an AI agent.

Point your agent at the Ansible repository you already have. Cloudfall
turns your playbooks into a tool list, checks every result, and keeps the
evidence.

What it is:

- **A thin MCP server over your playbooks.** Each playbook becomes a tool
  with a risk annotation; any client gates it
- **A verify step per operation.** Not "the playbook ran" but "the service
  is healthy on 3 of 3 hosts"
- **An audit entry per decision.** What the fleet looked like, what the
  agent proposed, who approved, what verify said
- **Receipted operations that ship with it.** Backups, restore drills,
  alerting to a second host; proven and optional

Who it is for: a team with one to twenty Debian hosts, an Ansible
repository, and Claude Code or Codex already in the loop. No AAP, no
Kubernetes, no SRE.

What it is not:

- **Not a new config format.** Your inventory is the config; Cloudfall adds
  one key
- **Not the brain.** The agent decides; Cloudfall says what it may do and
  records what it did
- **Not a gate product.** The client gates; Cloudfall supplies the
  annotations and the evidence
- **Not a dashboard.** One read-only view of the record; approvals stay in
  the CLI

The promise: after a month you can answer "why did the agent do that" for
every change on every host. After six months you can let it run some
operations unattended, because the record shows they always succeed.

For contrast, Cloudfall was a typed fleet description with an Ansible
engine behind it and an operator on top. The typed description is gone as
the entry point. The engine roles stay as optional operations. The operator
and its receipts are the product.

## Problem

Most teams that would benefit from Cloudfall already run Ansible. They have
an inventory, roles and playbooks that work. In 2026 they also have an
agent: Claude Code, Codex or any MCP client pointed at the repository. The
agent can already call `ansible-playbook`. What nobody has given it is a
reason to stop.

The agent decides well enough. What it lacks is a contract: a list of what
it may run, a gate it cannot bypass, and a record of why it did what it
did. Today that contract is a human reading over its shoulder, and the job
is manual.

Two obvious paths are wrong:

- **Import and leave.** Reading their inventory once into Cloudfall
  resources creates a second source of truth that drifts on the first edit
- **Two inventories side by side.** Cloudfall's rendered inventory next to
  theirs works technically (Ansible merges sources) but doubles the config
  and splits ownership of every host

The rule for this design: one config, which is theirs. Ansible executes,
the agent decides, Cloudfall constrains and records.

## Positioning

Cloudfall is not "Ansible for agents". Ansible is already callable by an
agent, and the [landscape](landscape.md) lists three Red Hat surfaces that
do it: the AAP MCP server launches jobs, the ADT MCP server runs playbooks
through ansible-navigator, Lightspeed writes them. None attach a risk
level, a precondition, a check-mode preview or a verify step to a run.
Cloudfall is the harness an agent is missing:

| Agent needs | Ansible gives | Cloudfall adds |
| --- | --- | --- |
| Know what it may do | Any playbook, any host | A catalog of declared operations with risk levels |
| Be stopped before harm | `--check` if remembered | A gate it cannot bypass |
| Explain itself | Job history: a job ran | Audit log with the snapshot, diff and approver behind each decision |
| Know the fleet | Inventory, facts | One structured snapshot per host |
| See the result | Verbose text, JSON callback | Per-host outcome, diff, evidence |
| Change config safely | Nothing; raw file writes | One edit primitive: round-trip, schema-checked, diffed, reversible |

The defensible part is the contract: an agent acts only through declared
operations and safe edits, on a fleet it can observe, behind a gate it does
not control. The intelligence is the model's. Cloudfall does not compete
with it and should not pretend to.

## Priority

The parts below are in build order. The order follows one question: what
does a team with one to twenty hosts, an Ansible repository and an agent
have no answer for today?

| Order | Part | Why this position |
| --- | --- | --- |
| 1 | Operations catalog | Turns "any playbook" into a tool list with a verify step; the verify step is the part no client harness supplies |
| 2 | Approval and audit | The evidence: what the agent saw, proposed, who let it, what verify said; the gate itself is thin |
| 3 | Agent surface | Catalog and audit are worthless unless the agent reaches them through one CLI and one MCP server |
| 4 | Fleet reader | The catalog needs targets; reading their inventory is the entry point to brownfield |
| 5 | Observation | Snapshots improve decisions; facts and callbacks already get most of the way |
| 6 | Safe edits | Real problem, but git diff plus ansible-lint plus review already approximates the gate |
| 7 | Starter roles | Optional; only for teams with no baseline |

The window is real and not permanent. Event-Driven Ansible already has the
loop, and risk levels on top of the AAP MCP server are one Red Hat release
away for enterprises. Parts 1 to 3 are the product. Build them first, and
build them thin, because the wedge moves within a year.

## The wedge in 2026 and in 2027

In 2026 the pitch is "the agent needs a gate". That pitch weakens fast:

- **MCP tool annotations** already mark a tool read-only or destructive,
  and Claude Code, Codex and other clients already gate on them with their
  permission modes. Any MCP server that wraps playbooks with those
  annotations gets a gate from the client for free. Cloudfall's own risk
  vocabulary buys nothing over that and should reuse it where the two
  overlap: `read` maps to `readOnlyHint`, `destructive` to
  `destructiveHint`, `mutating` is the default
- **Enterprise closes.** Assume the AAP MCP server has risk levels and
  approvals by 2027

In 2027 the gate is table stakes and the pitch is "the agent needs
evidence". What no client harness and no Red Hat surface does, because it
needs Ansible knowledge rather than a generic tool loop:

- **The audit entry with the fleet in it.** A client logs that a tool was
  called. Cloudfall logs what the fleet looked like when it was called, the
  check-mode diff it proposed, who approved, and what verify reported. That
  is what a team asks for after its first incident
- **The verify step.** An operation that checks its own result and reports
  a per-host outcome, so "deploy ran" becomes "deploy ran and health is ok
  on 3 of 3"
- **Receipts for the operations Cloudfall already ships.** Backups, restore
  drills, alerts delivered to a second host. These exist and are proven;
  nobody else has them at the one-to-twenty-host scale

Consequences for the build:

- Part 1 is a thin MCP server over the team's playbooks, with client
  annotations for risk and Cloudfall's own effort going into the verify
  step
- Part 2 is mostly the audit entry, not the gate. The gate is a few lines
  once the client enforces annotations
- Part 6 waits until a workload needs it
- If the market for the harness closes entirely, the operator with receipts
  is still worth having for Cloudfall's own hosts. That is the floor

## The moat

The moat is not the code. It is the record.

- **Receipt history that compounds.** Every run adds an audit entry with
  the fleet snapshot, the diff, the approver and the verify result. After
  six months a team holds evidence nobody else has: which operations always
  succeed, on which hosts, under what conditions. That history is what lets
  autonomy be granted per operation class. The schema can be copied in a
  week; the six months cannot
- **Verify as a first-class field.** Ansible reports that tasks changed.
  Cloudfall reports that the thing is now true, per host. Verify playbooks
  for real operations (backup restored, service healthy, cert renewed) are
  slow domain work and accumulate as a library the same way receipts do
- **Brownfield with zero migration.** Their inventory, their playbooks, one
  `cloudfall` key. Trying costs an afternoon; leaving costs deleting a key
- **The scale nobody serves.** One to twenty hosts, no AAP, no Kubernetes.
  Red Hat will not price for it, Coolify and Kamal do not audit, the AI SRE
  startups target Datadog-sized fleets
- **Open source with typed state the user owns.** Devopness is the closest
  twin and is closed SaaS. A tool that gates what an agent does to your
  servers should be readable

What the moat is not:

- Not a technology moat; anyone can wrap playbooks in MCP
- Not a gate moat; clients supply the gate now
- Not a defence against Red Hat at enterprise scale, and it does not need
  to be

Why it is still worth building:

- **Cloudfall is its own first user.** The operator with receipts pays for
  itself on Cloudfall's own hosts whether or not anyone else adopts it.
  That is the floor and it is already above zero
- **The parts that matter are small.** A thin MCP server over playbooks, an
  audit entry, a verify field. The expensive parts (safe edits, snapshot
  cache, in-process reader) are explicitly last
- **The compounding starts on day one.** The moat is time-based, so
  starting late is the only way to lose it
- **The proven operations are already the hardest part.** Backups, restore
  drills and cross-host alerting are done and exercised. The harness is
  the cheap layer on top of the expensive layer that exists

A year from now, a schema and a CLI is no moat. A year of receipts on real
hosts and a library of verify steps is the product.

## The seven parts

### 1. Operations catalog

Every playbook or role the team already runs becomes an operation:

```yaml
# operations/deploy.yml
id: deploy
playbook: playbooks/deploy.yml
risk: mutating
targets: component
inputs:
  version: string
preconditions:
  - health: ok
verify:
  playbook: playbooks/health.yml
```

An operation has a typed input schema, a risk level (`read`, `mutating`,
`destructive`), a target scope, preconditions and a verify step. The
catalog is the agent's tool list, generated from what the team runs today.
An operation the team has not declared does not exist to the agent.

The risk level is exported as MCP tool annotations so any client gates on
it without Cloudfall's help. The verify step is where Cloudfall's own work
goes: it is what turns a run into a per-host outcome the audit entry can
cite, and it is the field no generic harness can fill in.

Cloudfall's own roles (`cloudfall_bootstrap`, `cloudfall_firewall`,
`cloudfall_postgresql`, ...) ship as a starter pack of operations for teams
that have no baseline yet. They are optional (part 7).

### 2. Approval and audit

| Risk | Behaviour |
| --- | --- |
| `read` | Runs freely |
| `mutating` | Check mode first, diff shown, then waits for approval unless policy allows |
| `destructive` | Always waits for a human |

Every decision records the operation, the targets, the snapshot it was
based on, the check-mode diff, who approved and the outcome. The audit
log is the evidence the agent shows when asked why. AAP job history says a
job ran; a client's tool log says a tool was called. This says what the
agent saw, what it proposed, who let it, and what verify reported. By 2027
this entry is the product; the gate around it is a formality the client
mostly supplies.

The gate lives in Cloudfall, not in the agent's prompt. An agent can be
talked out of a rule in its instructions. It cannot be talked out of a
process that refuses to run a `destructive` operation without a human.

### 3. Agent surface

One CLI and one MCP server, same commands for humans and agents, JSON
output everywhere. The loop is fixed:

1. Observe: read snapshots
2. Decide: pick an operation and targets, state why
3. Preview: check mode and diff
4. Approve: gate by risk
5. Run: execute through Ansible
6. Verify: run the operation's verify step, update snapshots

The agent's tool list is the catalog from part 1 plus the safe-edit verbs
from part 6. Nothing else. A tool that is not on the list is a raw shell
call, and raw shell calls are what the harness exists to make unnecessary.

### 4. Fleet reader

Cloudfall declares no servers. It reads the team's inventory (static INI
or YAML, or a dynamic plugin) through ansible-core's own loader and gets
hosts, groups and merged variables with Ansible's precedence applied. The
typed model in `sdk/src/cloudfall/inventory.py` is populated from that
instead of from resource documents.

Everything Ansible does not model lives in a `cloudfall` variable block in
the team's own `group_vars` and `host_vars`:

```yaml
# host_vars/web-3.yml
cloudfall:
  environment: production
  server_type: web
  owns: [firewall, nginx]
  components: [ledgrai-backend]
```

This is ordinary Ansible. Their playbooks can read the same variables.
There is nothing to sync. Cloudfall's own state is confined to this key;
removing Cloudfall means deleting the `cloudfall` blocks. Any other change
to their files goes through the safe edit primitive in part 6, never
through a whole-file rewrite.

The `owns` list says which subsystems Cloudfall's own roles may touch on
that host. Anything not listed belongs to the team's roles. This is the
answer to the real conflict, which is not inventory but two role sets
managing the same file.

How the read is done in process, and the rules for depending on
ansible-core's internal API, are an implementation note at the end of this
document. They do not change what the reader is for.

### 5. Observation

Facts, service state, disk, recent logs and the last run result are
gathered into one structured snapshot per host. The agent reads the
snapshot, never raw SSH output. Snapshots are cached with a timestamp so
the agent can tell how stale its view is and refresh only what it needs.

The snapshot is what an audit entry (part 2) points at when it says what
the agent saw. That link is the reason snapshots exist as a Cloudfall
concept rather than as Ansible fact caching.

### 6. Safe edits

Ansible ships nothing that edits its own files: `ansible-inventory` exports,
`ansible-config init` generates, ansible-lint validates. An agent editing
Ansible today rewrites raw YAML with no schema and no gate, and loses
comments and anchors on the way.

The value is one function: parse round-trip, validate by file kind, diff,
gate, write, re-validate, record. The verbs on top of it are a thin naming
layer so the agent's tool list reads as intents. The shape is `verb target
name path value`:

```
cloudfall get    host  web-4
cloudfall set    host  web-4 ansible_host 10.0.0.14
cloudfall set    host  web-4 cloudfall.owns '[firewall, nginx]' --check
cloudfall unset  host  web-4 cloudfall.owns
cloudfall set    group production cloudfall.environment production
cloudfall set    play  deploy tasks[3].retries 5
cloudfall add    host  web-4 --address 10.0.0.14 --group production
cloudfall remove host  web-4
cloudfall set    file  hosts.yml all.children.production.hosts.web-4 '{ansible_host: 10.0.0.14}'
```

| Target | Addresses | Resolved to |
| --- | --- | --- |
| `host` | An inventory host by name | Its entry in the inventory file, or its `host_vars` file for variables |
| `group` | An inventory group by name | Its entry in the inventory file, or its `group_vars` file for variables |
| `play` | A playbook by name | The playbook file, path relative to the first play |
| `file` | Any YAML file by path | The file itself; the escape hatch for everything else |

Cloudfall resolves the file, picks the most specific existing one
(`host_vars/web-4.yml` over `host_vars/web-4/main.yml` over the inventory
entry), and names it in the output. `get` shows the merged value and which
file wins, so precedence is visible before a write.

Every mutating verb runs the same pipeline:

1. **Parse round-trip** with ruamel so comments, anchors, ordering and
   quoting survive. Only the addressed path changes
2. **Validate by file kind** before writing. Inventory and vars files
   against the ansible-lint inventory and vars schemas; playbooks against
   the ansible-lint playbook schema and `ansible-playbook --syntax-check`;
   task module arguments against `ansible-doc --json` argument specs; the
   `cloudfall` key against Cloudfall's own schema. Malformed input fails
   with a structured error and nothing is written
3. **Diff and gate.** Without `--yes`, print the unified diff and stop.
   Edits are `mutating` and go through the same gate as part 2
4. **Write, then re-validate** on disk with the same checks, plus a fresh
   inventory load for inventory and vars files. Any failure restores the
   pre-write copy, which is kept until the post-write check passes
5. **Record** the path, the diff and the approver in the audit log

`add host` and `remove host` add semantic checks on top of `set` and
`unset`: `add host` refuses a host that already exists in the merged
inventory or a group that does not, and writes both the inventory entry and
an empty `cloudfall` block. `set` and `unset` on the `cloudfall` key are
what `cloudfall add server-type` and the other declaration commands become.

Limits, stated to the user rather than worked around:

- **INI inventories and dynamic sources** are not editable. The command
  fails and names the file or plugin to change by hand
- **Semantic correctness** of a playbook is not checked. Syntax check and
  argument specs catch a wrong parameter, not a task that stops the wrong
  service. Check mode on the next run is the gate for that
- **Jinja templates** are not YAML and are out of scope
- **Cloudfall never rewrites a whole file** or restructures groups, roles or
  plays. Larger changes are drafted on a branch and merged by a human
- **No bare `edit` verb.** Every command names what it changes

A team with a review habit gets most of this from git diff, ansible-lint
and a pull request. Safe edits are for the agent that works without one,
and for the round-trip write that no reviewer can add after the fact.

### 7. Starter roles

The existing engine roles stay, packaged as optional operations. A
greenfield team gets a plain Ansible inventory generated by `cloudfall init`
so that greenfield and brownfield share one code path.

## What changes in the codebase

| Keep | Drop | Re-root |
| --- | --- | --- |
| Operator, approval and audit logic | Resource documents as the fleet source | Approval and audit become the gate for every operation and edit |
| MCP server and CLI | Outward inventory renderer | Tool list is generated from the catalog |
| Typed domain and JSON schemas | `cloudfall add *` as resource writers | Schemas validate the `cloudfall` var block instead of standalone files |
| Engine roles and playbooks | | Registered as starter operations |
| Read-only dashboard | | Dashboard reads snapshots and the audit log |
| | | `cloudfall add *` become wrappers over the safe edit primitive |
| | | `cloudfall init` emits a plain Ansible inventory |

## Open questions

- **Policy for auto-approval.** Which mutating operations may run without a
  human, and under what conditions (environment, time window, blast radius).
  This is the first question because it decides how much of part 2 a team
  will switch on
- **Hostname mapping.** The team's inventory name may differ from the
  machine hostname and from any Cloudfall id. The inventory name is the
  key; everything else is a variable
- **Schema strictness of the var block.** Reuse the existing JSON schemas,
  but decide whether unknown keys under `cloudfall` fail or warn
- **Group and host `cloudfall` keys do not merge.** Ansible replaces a
  dictionary defined at both group and host level unless `hash_behaviour`
  is set to `merge`, which is deprecated and fleet-wide. A `cloudfall`
  block in `group_vars/production.yml` is therefore lost on any host that
  also has one in `host_vars`. Found on the fleet-ansible reference repo
  on 2026-09-17, where the server-type catalog had to become a separate
  `cloudfall_server_types` variable. Options: one flat key per concern
  (`cloudfall_environment`, `cloudfall_owns`, ...) so each merges by
  precedence on its own; or a `cloudfall` block at host level only with
  group-level defaults read by Cloudfall itself, not by Ansible. The
  reader (part 4) and the `set` verb (part 6) both depend on the answer
- **Dynamic inventories.** Where do `host_vars` go when hosts come from a
  cloud plugin? Likely `group_vars` keyed by tag, plus a Cloudfall-side
  overlay file only for hosts that need one

## Not in scope

- Replacing Ansible or owning the team's inventory and playbook files;
  Cloudfall edits them only through the safe edit primitive, path by path
- Being the brain. Cloudfall proposes nothing and plans nothing; the agent
  does, and Cloudfall decides whether it may
- A write UI; approvals stay in the CLI (see the UI scope decision)
- Multi-tool execution (Terraform, Kubernetes); Ansible is the only hand
  for now

## Implementation note: in-process Ansible

This section is engineering, not product. It affects latency on the agent's
hot path and nothing a user chooses Cloudfall for.

Cloudfall stays Python. The [no Rust rewrite](decisions/2026-09-14-no-rust-rewrite.md)
decision holds. `get` and `set` usually run with no playbook in flight, so
a subprocess per call (`ansible-inventory --list`, several hundred
milliseconds) would dominate. Importing ansible-core costs about 150 ms
once per CLI invocation; every read after that is in memory, and dynamic
plugins run inside the same call.

| Need | How | Process cost |
| --- | --- | --- |
| Merged inventory, host and group vars, precedence, vault | `InventoryManager` and `VariableManager` in process | Import once, then in memory |
| Module argument specs for validation | `module_loader` and plugin docs in process, cached to disk | Import once |
| Playbook syntax check | `ansible-playbook --syntax-check` subprocess | Only at write time |
| Running an operation | ansible-runner, structured events, check mode | Ansible forks per host anyway |

Rules, because the internal API carries no stability promise:

- **One wrapper module** (`cloudfall.ansible_api`) owns every import from
  `ansible.*`. Nothing else in the SDK touches ansible-core directly
- **Pin ansible-core exactly.** A release bump is a change to one module
  with its own tests
- **Subprocess fallback behind the same functions.** If the in-process
  loader fails on a release, the wrapper falls back to
  `ansible-inventory --list` and reports that it did
- **One project per process.** ansible-core reads `ansible.cfg` and the
  environment into module-level state at import; `context.CLIARGS` is set
  once before any other call
- **Lazy imports.** ansible-core, the MCP server and the dashboard are
  imported only by the commands that need them

Target for the hot path: `cloudfall get host web-4` under 250 ms end to
end on a static inventory. Measure before optimising further; if it is
over budget after lazy imports, the next step is caching the merged
inventory keyed on file mtimes, not a language change.
