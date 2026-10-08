# Architecture

Cloudfall is the operator's record for self-hosted servers run by an AI agent.
The agent decides, Ansible executes, and Cloudfall sits between them: it
declares what the agent may do, checks every result, and keeps the evidence
behind every decision. Ansible is the hand, the agent is the brain,
Cloudfall is the conscience.

It was built wedge-first: take a SaaS off a cloud PaaS such as Render and
run it on one fresh Debian host with a hardened baseline, monitoring,
deploys, and rollback, then let an always-on operator run it there. The
architecture is designed for the goal behind the wedge: an agent safely
operating 10–100 standalone SaaS applications across a fleet of dedicated
Debian servers without Kubernetes (see the [fleet goals](docs/fleet-goals.md)),
on a config the team already owns. This document describes the durable
design; [`ROADMAP.md`](ROADMAP.md) is the authoritative source for what is
implemented today, and the [brownfield design](docs/brownfield-design.md)
describes the next step, whose operations catalog, gate and record are
built while safe edits and autonomy from the record are not.

Every layer keeps the same invariants: no action without declared config, no
status without evidence, no compliance without audit, no decision without a
record.

## Control plane stack

```text
Human / AI agent ──edits──► Config (YAML)
Human / AI agent ──runs───► CLI / MCP / Python API ──► Gate ──► Execution engine ──► Servers
                                     │                                   │
                                     └──────────── Record ◄── Verify ◄───┘
```

The config is a store, not a pipeline stage: humans and agents edit it
directly, the CLI validates and reads it, and the engine converges servers
to it. The agent operates through a stable CLI, the `cloudfall mcp serve` server
and a Python API instead of inventing shell commands or discovering
infrastructure over SSH. Every mutating operation goes through the engine's
explicit playbook contracts, waits behind a gate the agent does not control
(a confirmation handshake, or an `OperatorPolicy` that licenses the
operation class), and produces receipts. Status is derived from validated
observations, never inferred.

The record is the durable output. Every operator decision is written as a
schema-validated receipt that holds the evidence it was based on, the
diagnosis, the exact operation, who approved it and the outcome; deploys,
backups, restore drills and data migrations write receipts of their own.
The agent's own log says a tool was called. The record says what the fleet
looked like when it was, and it is what licenses autonomy later.

The loop the agent runs is fixed: observe (read evidence), decide (pick an
operation and targets, state why), preview (check mode and diff), approve
(gate by risk), run (execute through the engine), verify (check the result
and update the evidence).

## Project

A project is the unit Cloudfall operates: one directory, usually a private
repository, holding everything needed to run one user's stack.

- **Fleet** — the machines: `Server`, `ServerType`, and `SshPublicKey`
  resources
- **Applications** — the workloads: `Application`, `Component`, `Service`,
  and `Domain` resources
- **Operations** — the declarations that run them: `AlertRule`,
  `OperatorPolicy`, and `LoggingStack` resources
- **Pin** — a `pyproject.toml` that installs Cloudfall as a package at one
  exact commit, so the project and the platform version move together

Every `cloudfall` command runs inside a project: the current directory when
it is one, or the directory named by `--project` or by the
`CLOUDFALL_PROJECT` environment variable. Relative paths, including the
`tmp/` defaults, resolve against the project, as if the command had been
started there.
Resources live in one directory per kind (`servers/`, `components/`, ...),
and only those directories are read as resources; a resource in the wrong
directory is a validation error. Everything else in the project is the
user's: playbooks and roles of their own, encrypted secrets, docs, tooling.
Runtime state (observations, receipts, rendered inventory, built artifacts,
rendered environment files, the persisted migration plan) lands under `tmp/`
and is never committed. `cloudfall init` lays out a new project and
`cloudfall add` declares its first fleet resources.

Proposed, not built: for a team that already runs Ansible, the project is
their existing repository. The fleet is read from their inventory instead of
`Server` resources, Cloudfall's declarations live under one `cloudfall` key
in their `group_vars` and `host_vars`, and their playbooks are registered as
operations. One config, theirs; no import and no second copy. See the
[brownfield design](docs/brownfield-design.md).

## Module boundaries

Cloudfall is a monorepo with three architectural modules:

- [`config/`](config/README.md) — the config: declarative YAML resources and
  their JSON Schemas
- [`sdk/`](sdk/README.md) — the `cloudfall` CLI and Python API consumed by
  agents and tooling
- [`engine/`](engine/README.md) — internal execution machinery: Ansible-based
  execution of explicit plans

The boundaries are strict even inside one repository: the config contains no
execution logic, the CLI reads and validates the config without Ansible
internals, the engine never silently rewrites the config, and external entry
points call the CLI rather than Ansible directly. Mutating operations execute
through the engine's command-line and playbook contracts, never its
internals. The modules may split into separate repositories later if
independent release cycles or access control require it.

## Config

The config is declarative only, organized as typed resources validated
against versioned JSON Schemas: servers, server types, applications,
components, services, domains, logging stacks, alert rules, operator
policy, and SSH public keys.

The config never contains secret values; schemas and validation reject them.
Secrets exist as references, rendered from a sops/age-encrypted secrets
directory into environment files under the project's `tmp/`, never into a
resource, and consumed by systemd via `EnvironmentFile`.

The typed resources are today's config, not the point. The point is that
there is one description of the fleet, the team owns it, it is plain files
in a repository, and nothing rewrites it silently. The brownfield design
keeps every one of those properties while moving the description into the
team's own Ansible inventory; the schemas then validate the `cloudfall`
block instead of standalone documents.

## Application and component model

An application is an independent SaaS
product that composes components; a component (frontend, backend, worker,
scheduler) owns its own deployment. Components are named `crm-backend` style
rather than by path, and each may deploy to one or more servers.

On disk, each application owns a Linux user and a `/srv/apps/<application>/`
directory with one subdirectory per component. Releases unpack into
`releases/` and a `current` symlink points at the active release, which is
what makes rollback a symlink switch.

Components come from one repository each by preference, with monorepos also
supported.

## Deployment strategy

Artifacts are built on the management host — the machine running Cloudfall,
which is a workstation for one-off migrations or a small always-on host
once the operator runs persistently — never on production servers:

```text
Git ref → build artifact (hashed tarball + release metadata) → digest-verified
transfer → unpack into releases/ → locked dependency materialization →
environment file → systemd unit → health-check gate → symlink switch
```

A failed health check triggers automatic rollback to the previous release.
Releases are retained until a cleanup policy removes them, so explicit
rollback to any retained release stays available. Deployment approval is
automatic or manual per application.

## Supported workloads

Applications run as native systemd units on long-lived Debian hosts, not
in containers. The primary stacks are Python (uv, Gunicorn/Uvicorn, Celery)
and Node.js; anything that runs as a systemd service with a health endpoint
fits the model.

## Infrastructure model

Servers are traditional long-lived Debian hosts (not immutable), described
by reusable `ServerType` resources; bare-metal server types
add software RAID1 and the hybrid storage layout, while cloud VPS types do
not. Any server can run any declared service or component:

- Infrastructure services (PostgreSQL and Redis today; MySQL, Elasticsearch,
  and friends as the catalog grows) stay pinned to declared servers
- Application components are movable: reassigning `crm-backend` from `h1,h2`
  to `h3` is a config change followed by convergence
- Failover is manual with easy reassignment rather than automated
  orchestration; the planned PostgreSQL high-availability formation (M14 on
  the roadmap) amends this at the database layer only, where a standby may
  be promoted automatically but an operator never initiates a promotion
  without explicit confirmation

## Networking

Nginx terminates TLS with virtual hosts rendered from `Domain` resources and
Let's Encrypt certificates. Cloudflare proxying is used where appropriate,
and future load balancing is Cloudflare plus Nginx, with HAProxy optional
later.

## Operations layer

Operating servers is the same evidence discipline running continuously:

- **Alerting** — alert rules and notification channels are typed config
  rendered into the Loki/Grafana/Alloy stack; a firing alert is evidence,
  exposed over a read-only mTLS route on the same gateway that receives
  logs and metrics
- **Operator** — an always-on management-host process watches alerts and
  scheduled drift audits, writes every diagnosis as a schema-validated
  proposal receipt (trigger evidence, diagnosis, exact operation, outcome),
  and executes only existing engine entry points
- **Graduated autonomy** — autonomy is granted per operation class by a
  declared `OperatorPolicy` and earned by verified receipt history, never
  globally; DNS cutover, data deletion, and database promotion stay behind
  explicit confirmation regardless of autonomy level
- **Record** — receipts are the audit log: schema-validated, written under
  the project's `tmp/`, one per decision or operation, holding the evidence,
  the action, the approver and the outcome. "Why did the agent do that" is
  answered from the record, not from the agent's memory
- **Verify** — a run is not an outcome. Deploys are gated on health, backups
  on a timer-driven restore drill, migrations on row counts, audits on
  observed drift. The brownfield design generalises this into a verify step
  declared on every operation

## Direction: the team's Ansible as the config

The wedge proves the model on one host that Cloudfall set up. Most teams
that would use the record already have a fleet and an Ansible repository.
The [brownfield design](docs/brownfield-design.md) re-roots the same
machinery on that repository, in this build order:

- **Operations catalog** — each of the team's playbooks becomes a declared
  tool with typed inputs, a risk level exported as MCP tool annotations,
  preconditions and a verify step
- **Approval and audit** — the gate and the audit entry, unchanged in
  principle from the operator above, applied to every declared operation
- **Agent surface** — the same CLI and MCP server, with the tool list
  generated from the catalog
- **Fleet reader** — hosts, groups and merged variables read through
  ansible-core in process, with the `cloudfall` block as the only addition
- **Observation, safe edits, starter roles** — last; the existing engine
  roles become optional operations for teams with no baseline yet

None of this is implemented. The module boundaries above hold: the reader
is one wrapper module around ansible-core, and mutating operations still
execute through the engine's contracts.

## Long-term vision: fleet operation

The same config, record, and deploy machinery is designed to extend to
fleet operation without architectural change; the
[fleet goals](docs/fleet-goals.md) state the requirements this must deliver
and their current status:

- **Catalog breadth** — MySQL, Elasticsearch, RabbitMQ, and Node runtime
  services following the PostgreSQL and Redis pattern: pinned installs,
  loopback-only binds, backup policies, audited evidence
- **Multi-server assignment at scale** — components spread across many
  servers and moved by editing the config; the model exists, fleet-scale
  operation is the unproven part
- **Managed secrets backends** — the sops/age provider boundary admits a
  central secrets manager later, keeping the platform/application/component
  scope hierarchy
- **Fleet observability** — per-service exporters beyond PostgreSQL and
  Redis, and alert rules spanning more than one host
- **PostgreSQL high availability** — a declared replication, failover, and
  point-in-time recovery (M13) and failover formation (M14 on the roadmap)

## Status

Implementation status, milestone exit criteria, and what remains before the
wedge is proven live are tracked in [`ROADMAP.md`](ROADMAP.md).
