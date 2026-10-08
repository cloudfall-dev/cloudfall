# Roadmap

Cloudfall is the operator's record for a small fleet run by an AI agent:
the agent decides, Ansible executes, Cloudfall declares what the agent may
do, checks every result and keeps the evidence behind every decision. The
roadmap reaches that in two stories.

The first story, proven live in M0–M10:

> A SaaS running on a PaaS such as Render gets one fresh Debian bare-metal or
> VPS host, runs Cloudfall, and within an hour has: a hardened baseline, firewall,
> monitoring, logging, PostgreSQL, Nginx with TLS, its application deployed
> from GitHub with health checks and rollback, and a DNS cutover checklist.
> An always-on operator then runs it there, with a receipt for every action.

The second story, under way (M15–M17, see the
[brownfield design](docs/brownfield-design.md)): the operations catalog,
the gate and the decision record are built and have run on the
maintainer's own production fleet; safe edits and autonomy from the record
are not built yet:

> A team that already runs Ansible points its agent at its own repository.
> Cloudfall reads their inventory as the only config, exposes each of their
> playbooks as a declared operation with a risk level and a verify step,
> and writes an audit entry for every decision: what the fleet looked like,
> what was proposed, who approved, what verify reported.

Every milestone keeps the invariants that define Cloudfall: no action without
declared config, no status without evidence, no compliance without audit, no
decision without a record.

Milestone numbers are labels, not order. After M10 the priority is the
brownfield track (M15–M17), because the record only compounds on fleets
that exist; the density and PostgreSQL tracks (M11–M14) are demand-gated
and follow.

## M0 — Publishable base

- Public repository with license, CI, and contribution docs ✔
- Schemas, the validation CLI and Python API, audit pipeline, and generic
  engine roles ✔

## M1 — Layer 1: server baseline

One command turns an installed Debian host into a compliant Cloudfall server.

- Baseline packages, application users, `/srv/apps`, UTC enforcement, SSH
  hardening from declared `SshPublicKey` resources, unattended security
  upgrades ✔ (`task server:baseline`)
- nftables firewall role: default-deny inbound, allowances declared in the
  config, firewall evidence in observations and audit ✔
- Systemd timer units modeled and audited alongside services ✔
- Host metrics through the Loki/Grafana/Alloy stack ✔ — Alloy's embedded
  unix exporter pushes to a loopback Prometheus over the existing mTLS
  gateway, so no standalone `node_exporter` port is exposed

Exit: the baseline run is idempotent (second run reports no changes) and
`cloudfall audit` reports the host compliant, including firewall and timers.
**Met on 2026-09-08** on a disposable Hetzner Cloud Debian 13 server,
including live host metrics through the logging stack; see the
[Hetzner proving-run report](docs/proving-runs/2026-09-08-hetzner-m1.md) and
the earlier [container dry run](docs/proving-runs/2026-09-08-debian-container-m1.md).
Each run caught and fixed a real bug (a firewall-template rendering error and
an observation-schema rejection of escaped systemd unit names), which is the
point of proving runs. Bare-metal specifics (software RAID, storage
provisioning) remain unexercised.

## M2 — Layer 2: service catalog v1

Installable, audited infrastructure services, starting with exactly two.

- A `Service` schema: kind, version, servers, ports, bind policy, backup
  policy ✔
- PostgreSQL role: pinned install, localhost-only bind by default,
  per-application databases and users from the config (peer authentication,
  no database passwords), scheduled dumps, and a restore-proof command ✔
  (`task services:deploy`)
- Nginx site role: virtual hosts rendered from `Domain` resources, certbot
  with timer compliance, canonical client-address forwarding, and
  deployment receipts ✔ (`task domains:deploy`)
- Audit integration: packages, units, config hashes, and listening-socket
  evidence proving each declared service binds only to its loopback
  address ✔

Catalog breadth (Redis, RabbitMQ, Elasticsearch, MySQL, Node runtimes)
follows the same pattern afterward.

Exit: **met on 2026-09-08** on a disposable Hetzner Cloud Debian 13 server —
loopback-only PostgreSQL with a peer-authenticated application database, and
an Nginx route with a real Let's Encrypt certificate issued through the
role's webroot flow; see the
[M2/M3 proving-run report](docs/proving-runs/2026-09-08-hetzner-m2-m3.md).

## M3 — Layer 3: deploy slice

`deploy()` with rollback — the heart of one-command operation.

- Artifact builder on the management host (git ref → hashed tarball with
  schema-valid release metadata) ✔ (`task artifact:build`)
- Deploy role: digest-verified transfer, `releases/` unpacking, locked
  dependency materialization via pinned uv, environment-file installation,
  systemd unit from `Component.service`, health-check gate, symlink switch,
  automatic rollback on failed health checks, and release receipts ✔
  (`task deploy`)
- CLI and Python API operations `deploy()`, `rollback()`, `restart()`,
  `health()` with structured JSON results ✔ (`cloudfall deploy|rollback|restart|health`,
  executing through the engine's process boundaries; explicit rollback to
  any retained release included)
- Secrets v1: environment files generated from secret references (a local
  environment file per component is supported today; reference-driven
  generation shipped as M6's secrets v2)

Exit: the example backend component deploys end to end on a fresh host, and a
bad release rolls back automatically. **Met on 2026-09-08** on the same
disposable Hetzner host as the M2 exit: a uv-locked ASGI component deployed
through `cloudfall deploy` with a passing health gate, and a broken release
failed its gate and rolled back automatically with the service staying
healthy; see the
[M2/M3 proving-run report](docs/proving-runs/2026-09-08-hetzner-m2-m3.md).
The run fixed six engine/CLI bugs. The false origin warning it left open for
single-host topologies was fixed in the M4/M5 run: domain evidence now
records an explicit origin skip when origin and proxy share a host.

## M4 — Migration importer

- `cloudfall import render`: map `render.yaml` resources to Cloudfall
  applications, components, services, and domains, with environment files
  kept outside the config and a structured gap report for anything
  unmappable ✔
  (blueprint files; the Render API and cron jobs are pending)
- Data migration: managed-Postgres dump and restore with verification ✔
  (`cloudfall data copy`, the `--data` step of `cloudfall migrate`, and
  the confirm-gated `migrate_database` MCP tool: per-table row-count
  verification, a non-empty-target refusal guard, secret-file hygiene, and
  a `DataMigrationReceipt`; proven live in the
  [guided data-migration report](docs/proving-runs/2026-09-08-guided-data-migration.md))
- Cutover generator: DNS TTL lowering, parallel-run verification, switch, and
  a rollback window (DNS steps surface in the import report today)
- v1 limitation: native Python and Node builds; container runtimes are a
  tracked decision ✔ (documented and enforced — container services land in
  the gap report)

Exit: a real PaaS application migrated onto one host with its data, TLS,
monitoring, and rollback path. **Met on 2026-09-08** in two stages: a
realistic blueprint migrated end to end
([M4/M5 report](docs/proving-runs/2026-09-08-hetzner-m4-m5.md)), then an
application actually hosted on Render — built by Render from this
repository, serving rows from its managed PostgreSQL — cut over with its
data to a byte-identical response on the Cloudfall host
([cutover report](docs/proving-runs/2026-09-08-render-cutover.md)), and
finally the TTL-lowered DNS record cutover itself on an owned domain, with
the migrate plan pausing at `dns-verify` and resuming after the flip
([DNS cutover report](docs/proving-runs/2026-09-08-dns-cutover.md)). Every
wedge step is now proven live, including the guided data migration.

## M5 — Agent layer

- MCP server over the Python API: read-only tools exposed freely; mutating tools
  gated by explicit confirmation and always writing receipts ✔
  (`cloudfall-mcp` with seventeen annotated tools covering import, artifact
  build, baseline/services/domains convergence, deploy/rollback/restart,
  and the full evidence pipeline)
- A `migrate` orchestrator chaining M1–M4 as a resumable plan ✔
  (`cloudfall migrate` and the `migrate` MCP tool: persisted step progress,
  a DNS-verification pause at the cutover moment, and a final evidence pass
  requiring a compliant audit and healthy routes)

Exit: an AI agent completes the M4 migration through MCP without shell access
to the servers. **Met on 2026-09-08**: an AI agent drove the blueprint import
and the full ten-step migration (baseline through the final evidence pass)
purely through `cloudfall-mcp` with the confirmation handshake, taking a
fresh Debian host to a TLS-served, audit-compliant application in about
seven minutes, and a later session resumed the persisted plan as a no-op —
see the
[M4/M5 proving-run report](docs/proving-runs/2026-09-08-hetzner-m4-m5.md).

## M6 — Next: from proven wedge to routine migrations

M0–M5 proved the wedge end to end. The next milestones make the migration
routine and widen what it can carry, in rough priority order:

- **Catalog breadth** — Redis, MySQL, Elasticsearch, RabbitMQ, and Node
  runtime services following the PostgreSQL pattern: pinned installs,
  loopback-only binds, backup policies, audited evidence (Redis ✔ with RDB
  backups, restore checks, and Alloy metrics; the rest pending)
- **Render API import** ✔ — `cloudfall import render-api` maps a live
  workspace with real environment values and database-URL rewriting; cron
  jobs arrive with their schedules in the gap report, and modeling them as
  systemd timers stays tracked
- **Cutover generator** ✔ — the migrate plan now carries explicit
  `ttl-lower` (authoritative TTLs measured, pause while above 300s),
  `parallel-run` (the origin must serve every domain before DNS changes),
  the existing `dns-verify` switch pause, and `rollback-window` (pre-switch
  answers recorded with an exact 24h revert recipe)
- **Secrets v2** ✔ — `cloudfall secrets render` (and the
  `render_secrets` MCP tool) resolves declared secret references from a
  sops/age-encrypted secrets directory into per-component 0600 environment
  files, merging platform → application → component scopes; envelopes carry key
  names and hashes, never values, and the provider boundary admits managed
  backends later — see the [secrets guide](docs/secrets-guide.md)
- **Bare-metal provisioning** — software RAID and storage layout for
  dedicated servers per the
  [hybrid storage design](docs/hybrid-storage-design.md), the one part of
  the server model still unexercised live
- **Projects** ✔ — `cloudfall init` lays out a project (one directory
  per resource kind, README, `.gitignore`, and a `pyproject.toml` pinning
  Cloudfall to the running commit); only kind directories are read as
  resources, so a project carries its own playbooks, secrets, and docs.
  `cloudfall add ssh-key|server-type|server` declares the fleet without
  hand-writing YAML, creating the Debian 13 baseline type on first use
- **Backup and restore as first-class operations** ✔ — `cloudfall
  backup run|verify` and the confirm-gated `backup_service` /
  `verify_backup` MCP tools execute the declared backup and restore-proof
  scripts on the service's server as its service user, each writing a
  schema-valid `BackupReceipt`

Exit: a second real application migrated by someone other than the author,
using only the public documentation.

## M7 — Full observability: metrics and alerting

The prerequisite for everything after it: an operator acts on signals, and
without alerting there are no signals. Layered on the existing
Loki/Grafana/Alloy stack.

- **Per-service metrics** — exporters for each catalog service (PostgreSQL
  first) declared in config and audited like any other unit, flowing through
  the same mTLS gateway as host metrics
- **Alert rules from declared config** — thresholds and conditions as typed
  resources, rendered into the stack; an alert fires as evidence, not just a
  notification
- **Notification channels** — declared delivery (email/webhook to start),
  with alert delivery itself audited

Exit: an induced service failure on a proving host raises a declared alert
with evidence within one minute, and `cloudfall audit` reports alert-rule
compliance alongside everything else. **Met on 2026-09-10** on a disposable
Hetzner Cloud Debian 13 server: stopping PostgreSQL raised the declared
alert as pending in 28 seconds and firing at 86 seconds per its declared
`for: 1m` dampening, the declared webhook received the payload, the
mid-failure audit separated drift (dead listener) from status (firing rule,
compliant), and recovery returned the host to 20/20 compliant — see the
[M7 proving-run report](docs/proving-runs/2026-09-10-hetzner-m7.md). The run
found and fixed one real latency bug (Prometheus `evaluation_interval` was
stuck at its 1-minute default), which is the point of proving runs.

## M8 — Operator runtime: the always-on agent, propose mode

A persistent agent process that watches the alert stream and drift reports,
diagnoses, and proposes — the human confirms through the existing MCP
handshake. No autonomy yet: this milestone is the loop, not the license.

- **Operator daemon** — long-running process subscribed to alerts and
  scheduled drift checks, maintaining the fleet's observed state
- **Diagnose-and-propose** — for each incident, an evidence-backed diagnosis
  and a concrete proposed operation (existing CLI/MCP verbs only), delivered
  with the receipts that justify it
- **Every proposal receipted** — proposals, confirmations, executions, and
  outcomes all land in the same evidence pipeline as manual operations

Exit: an induced failure is detected, diagnosed, and remediated end to end
with the human contributing only a confirmation — no human diagnosis, no
shell access. **Met on 2026-09-10** on a disposable Hetzner Cloud Debian 13
server, for both trigger kinds: a stopped PostgreSQL went from failure to
receipted, verified remediation in about 2.5 minutes with one approval
command, and induced package drift was detected by the scheduled audit,
proposed, approved through the agent confirmation handshake, and verified
compliant in 53 seconds — see the
[M8 proving-run report](docs/proving-runs/2026-09-10-hetzner-m8.md). The
operator watched remotely over the gateway's new read-only mTLS alerts
route with no shell access during detection, diagnosis, or verification.

## M9 — Graduated autonomy

Autonomy is granted per operation class, earned by evidence, never global.
The order is fixed by the dependency chain: M7 provides the signals, M8 the
loop, M9 the license.

- **Level 2: autonomous for provenly reversible operations** — failed-deploy
  rollback, service restart, certificate renewal; executed without asking,
  receipt delivered after the fact, reversibility backed by the operation
  class's receipt history
- **Level 3: autonomy within declared policy** — bounds live in the same
  typed state as everything else (which operations, which components, quiet
  hours, rate limits); the operator refuses outside them
- **Confirm forever** — DNS cutover, data deletion, and database promotion
  remain behind explicit confirmation regardless of level

Exit: on a proving host, a deliberately broken release is rolled back
autonomously within declared policy — alert to healthy service with zero
human involvement — and the receipt trail shows detection, decision, action,
and verified outcome. **Met on 2026-09-10** on a disposable Hetzner Cloud
Debian 13 server: the first induced PostgreSQL failure was withheld from
autonomy with a recorded reason (insufficient verified history) and needed
one human approval — the receipt that earned the trust — and the second
identical failure went from stopped service to receipted, verified,
policy-licensed autonomous remediation in 2 minutes 6 seconds with zero
human involvement; see the
[M9 proving-run report](docs/proving-runs/2026-09-10-hetzner-m9.md). An
externally killed execution also demonstrated crash consistency: the
receipt stayed truthfully open and the next autonomy pass completed it.

## M10 — Prove the untested promises: restore drill and real alerting

Two capabilities exist in code and receipts but have never run against the
world: the backup restore drill and alert delivery to a real destination.
An unexercised restore path is not a tested one, and an alert that has only
ever reached a loopback listener has not proven it can wake anyone. This
milestone closes both gaps with the same discipline as every other claim.

- **Exercised restore drill** ✔ — `cloudfall backup verify` run on a proving
  host against a real scheduled dump: scratch-database restore, integrity
  check, and a `BackupReceipt` proving the dump is restorable, not merely
  present
- **Scheduled drill** ✔ — the restore-check gains its own systemd timer
  (declared as `backup.restoreCheckOnCalendar`, implemented for PostgreSQL
  and Redis) so restorability is verified continuously and audited like any
  other unit, not only when someone remembers to ask
- **Real alert channel** ✔ — a declared webhook receiver delivering beyond
  loopback, with the delivery captured as evidence; the email config path
  stays covered by template tests only

Exit: on a proving host, a scheduled backup is restore-verified by the
timer-driven drill with a schema-valid `BackupReceipt`, and an induced
failure produces an alert that arrives at a real external destination —
both trails visible in `cloudfall audit` alongside everything else.
**Met on 2026-09-11** on a disposable Hetzner Cloud Debian 13 pair: the
drill timer's own unit restored the newest dump of a seeded database into a
scratch database (420 catalog relations) with a schema-valid
`BackupReceipt`, both timers audited as required units in a 21/21-compliant
baseline, and the induced PostgreSQL failure delivered its full firing
payload to a second machine across the public network at T0+113 s — see the
[M10 proving-run report](docs/proving-runs/2026-09-11-hetzner-m10.md). The
run caught and fixed a real bug (the typed inventory silently dropped the
new drill schedule, so no timer was installed until the audit's
required-unit check exposed it), which is the point of proving runs.

## M15 — Brownfield: operations catalog, gate and record

The first milestone of the second story, and the product in its smallest
form. Nothing here needs the fleet reader: the catalog can be declared
against a Cloudfall project first and re-rooted in M16.

- **Operations catalog** — an `Operation` resource that registers an
  existing playbook with typed inputs, a risk level (`read`, `mutating`,
  `destructive`), a target scope, preconditions and a verify playbook; the
  existing engine roles ship as a starter catalog
- **Risk as MCP annotations** — the MCP tool list is generated from the
  catalog, with `readOnlyHint` and `destructiveHint` set from the risk
  level, so any client gates on it without Cloudfall's help
- **Audit entry per decision** — one schema-validated record per operation
  run holding the evidence snapshot it was based on, the check-mode diff,
  the approver (human, policy, or client handshake) and the verify result;
  the operator's proposal receipts become one kind of audit entry
- **Verify as a first-class field** — a run is not an outcome until its
  verify step reports per host; the existing health gate, restore drill and
  row-count checks are re-expressed as verify steps
- **`cloudfall why`** — the question "why did the agent do that" answered
  from the record for any host, operation or time window, in JSON and as a
  page ✔ (also the `why` tool on `cloudfall mcp serve --repository`)

Exit: an agent drives a declared mutating operation through the MCP server
on a proving host, the client gates it from the annotation alone, the run
is refused until its precondition holds, and afterwards `cloudfall why`
returns the audit entry with snapshot, diff, approver and verify result
without any human having written a note.

## M16 — Brownfield: the team's Ansible as the config

Re-roots M15 on an existing Ansible repository. One config, theirs; no
import, no second copy.

- **Fleet reader** — hosts, groups and merged variables read through
  ansible-core in process behind one wrapper module, with subprocess
  fallback; the typed inventory model is populated from it instead of from
  `Server` resources
- **The `cloudfall` block** — Cloudfall's declarations (environment, server
  type, owned subsystems, components) live under one key in the team's own
  `group_vars` and `host_vars`, validated by the existing schemas; removing
  Cloudfall means deleting the key
- **Operations from their playbooks** — the M15 catalog registers the
  team's playbooks, not only the starter roles; `owns` says which
  subsystems Cloudfall's roles may touch on a host
- **Observation** — facts, service state, disk and last-run result as one
  timestamped snapshot per host, referenced by every audit entry
- **Greenfield on the same path** — `cloudfall init` emits a plain Ansible
  inventory, so both stories share one code path

Exit: on a proving repository with an INI or YAML inventory and playbooks
Cloudfall did not write, `cloudfall get host` returns the merged variables
and the file that wins, an operation declared over one of their playbooks
runs through the M15 gate and record, and no file outside the `cloudfall`
key has changed.

## M17 — Brownfield: safe edits and hosted record

The parts that wait for a workload to need them.

- **Safe edits** — `get`, `set`, `unset`, `add host`, `remove host` over
  inventory, vars and playbook files: ruamel round-trip, schema and
  syntax-check validation before and after the write, diff and gate,
  restore on failure, audit entry; INI and dynamic sources refused with the
  file named
- **Hosted record** — the audit log kept off the management host with
  retention and search across hosts, which is where autonomy-from-history
  is computed; local receipts stay the free path
- **Autonomy from the record** — `OperatorPolicy` may license an operation
  class on evidence ("succeeded N times on these hosts") computed from the
  record, not only on a declared allow-list
- **Per-tenant policy** — policy and approvers scoped per inventory group
  for teams that run many fleets from one repository

Exit: an agent edits a host variable in a proving repository through
`cloudfall set` with comments and ordering preserved, the record shows the
edit with its diff and approver, and an operation class is promoted to
unattended by policy on the strength of the record alone.

## M11 — Fleet density: enforced resource sharing

The [fleet goals](docs/fleet-goals.md) raise the ambition to 10–100
applications on shared nodes, and the first thing that fails at that
density is unenforced sharing: one leaking application takes down its
neighbors. This milestone makes fleet requirement 8 real.

- **Per-component resource envelopes** — memory, CPU, task, and I/O
  limits declared in config and validated against the node's declared
  capacity; placement that oversubscribes a node fails validation
- **systemd enforcement** — the deploy role renders the envelope into the
  unit (`MemoryMax`, `CPUQuota`, `TasksMax`, `IOWeight`), and the audit
  proves the running unit carries the declared limits
- **Capacity accounting** — declared reservations against node capacity
  become fleet headroom evidence in the operations dashboard, so
  placement decisions are computed from config, not guessed

Exit: on a shared proving host, a deliberately leaking component hits its
declared envelope and is contained — its neighbors' health checks stay
green — and `cloudfall audit` reports envelope compliance alongside
everything else.

## M12 — Application mobility: data, routing, drain

Makes "applications and components are movable" true for stateful
applications (fleet requirements 9–11). Moving a component is already a
config edit; this milestone makes its data and routing follow.

- **Data movement as a verb** — moving an application moves its databases
  (the existing verified dump-and-restore path) and its file storage,
  with receipts for both
- **Routing follows placement** — domain routes re-render and reconverge
  as a consequence of a placement change, never by hand-editing the proxy
- **Node drain and decommission** — evacuate every component, dataset,
  and route from a node by config change, prove the fleet healthy without
  it, then retire it

Exit: on a two-node proving fleet, an application with a database and
uploaded files moves between nodes by config edit and convergence,
serving byte-identical responses afterward; a drained node is then
removed with the fleet audit staying compliant.

## M13 — PostgreSQL data safety and scale readiness

The severable single-host half of high availability: near-zero data loss
and formation-ready installs, without paying for a standby.

- **Continuous archiving** — `pgBackRest` WAL archiving to object
  storage, upgrading recovery from last-dump to point-in-time; the M10
  restore drill extends to a receipted PITR drill
- **Availability schema** — the `availability` block from the
  [availability design](docs/availability-design.md) lands, with per-kind
  mode validation; declaring intent becomes possible fleet-wide before
  any formation exists
- **Expansion-ready single** — formation prerequisites installed dormant
  at one node (`wal_level`, replication role, TLS-ready binds, fixed
  identities) and audited, so a future standby is a config edit, not a
  reinstall

Exit: a PITR drill restores a proving database to a declared point in
time with a schema-valid `BackupReceipt`, and a fresh single-node
PostgreSQL passes an audit proving every formation prerequisite.

## M14 — PostgreSQL failover formation (demand-gated)

Built when a service's revenue justifies a standby, not before (fleet
requirement 6). One primary, one hot standby, one witness. This amends
the architecture stance that failover is manual — at the database layer
only, and below the operator: the formation may promote automatically,
but the operator still never initiates a promotion without explicit
confirmation.

- **Streaming replication** — a declared two-node formation with native
  WAL streaming over a private network with TLS, replacing the
  loopback-only binding; synchronous or asynchronous as declared config
- **Automatic failover** — `pg_auto_failover` with the monitor on a
  minimal witness host as the tiebreaker; formation state (primary,
  secondary, single) audited as evidence like any other unit
- **Connection routing** — PgBouncer on each data node with multi-host
  client connection strings (`target_session_attrs=read-write`), so
  applications follow the primary with no extra routing layer
- **Multi-host alerting exercised** — replication lag, formation
  degradation, and archiving failure become declared alert rules, the
  forcing function for alerting across more than one host; backups move
  to the standby

Exit: on a three-host proving formation, killing the primary promotes the
standby and the application recovers with zero human involvement, and the
receipt trail shows detection, promotion, and verified recovery — while
`cloudfall audit` reports formation compliance across all three hosts.
