# Where Cloudfall sits: the tool landscape

Status: reference positioning, written 2026-09-14, Ansible Automation
Platform, the Ansible MCP servers, and Ansible Lightspeed added
2026-09-16, re-centred on the operator's record 2026-09-17. This document
places every neighboring tool at once. Each tool is classified by the
layer it occupies and by its relationship to Cloudfall: **built on**
(Cloudfall uses it), **replaces** (Cloudfall does the same job a different
way), **complements** (runs next to Cloudfall, no overlap), **alternative**
(a different approach to the same problem that Cloudfall rejects), or
**migrates from** (the place a workload leaves).

## The stack, top to bottom

```text
  who decides        Claude Code, Codex, any MCP client   (the agent's brain)
  what it may do,    Cloudfall record: catalog, gate, verify, audit entry
  and the proof      |  AAP MCP server, ADT MCP server (run, no record)
  who runs it        Cloudfall operator, PagerDuty, Devopness, AI-SRE tools
  how you deploy     Cloudfall deploy, Coolify, Dokku, Kamal, Compose, k8s
  what it runs on    systemd + nginx + PostgreSQL   (Cloudfall's choice)
  how the OS is      Ansible, Puppet, Salt   |   NixOS (OS is the config)
  configured
  which OS           Debian (Cloudfall requires)  |  NixOS, Ubuntu, Alpine
  how the server     Terraform / OpenTofu, Pulumi, hcloud CLI, cloud-init
  is created
  where              Hetzner, OVH, any VPS   |   Render, Heroku, Fly (PaaS)
```

Cloudfall occupies four of these rows itself: what the agent may do and
the proof (the record, which is the product), who runs it (the operator),
how you deploy, and how the OS is configured (through its embedded Ansible
engine today, through the team's own Ansible under the
[brownfield design](brownfield-design.md) next). It deliberately does not
occupy the top row: the agent decides, Cloudfall declares what it may
decide between, gates it, verifies it and records it. It requires a
specific answer for one row (Debian), has no opinion on server creation,
and treats the PaaS column as the place workloads come from.

## Tool by tool

| Tool | Layer | Relationship | In one sentence |
|---|---|---|---|
| Ansible | OS configuration | Built on | Cloudfall's engine is Ansible behind a playbook contract; today you write no playbooks unless you add a role of your own, and under the brownfield design your existing playbooks become the agent's declared operations |
| Red Hat Ansible Automation Platform (AAP), AWX | Automation runner | Complements | Runs your playbooks at enterprise scale with RBAC, schedules, approvals, and event-driven rules; it brings no playbooks, no declared model, and no proof of results (see below) |
| Ansible Development Tools (ADT) MCP server | Authoring aid | Complements | Helps an agent write Ansible: `ansible-lint` with auto-fix, `ansible-creator` scaffolding, `ansible-navigator` playbook runs, execution environment builds, best-practice guidance; it can run a playbook but attaches no risk level, verify step or audit entry to the run, holds no model of servers, and can serve as the lint step for brownfield edits |
| Red Hat Ansible Lightspeed | Authoring aid and AAP help | Complements | Two AI services sold with AAP: a coding assistant in the Ansible VS Code extension that generates tasks, playbooks, and roles from plain English, explains existing content, and matches suggestions to their Galaxy sources; and an intelligent assistant, a chat in the AAP web UI that answers AAP install, configure, and troubleshooting questions from Red Hat docs (and, as a technology preview, from live AAP data through the AAP MCP server); it writes code and answers questions, it holds no model of servers, runs no audit, and decides nothing about a fleet |
| Terraform / OpenTofu, Pulumi | Server creation | Complements | Creates the Hetzner server, DNS zone, and network; hands a Debian host to Cloudfall and stops |
| hcloud CLI, cloud-init | Server creation | Complements | The manual or scripted way to get the same Debian host; the proving runs use hcloud directly |
| Debian | Operating system | Required | Stock Debian is a principle (any Linux admin can take over), not a placeholder |
| NixOS | Operating system and its configuration | Alternative | Makes the machine a build output; Cloudfall makes the operation provable on a mutable host |
| Puppet, Chef, Salt | OS configuration | Alternative | Same layer as Ansible; Cloudfall picked Ansible and hides it |
| pyinfra | OS configuration | Alternative | Same layer as Ansible, with Python files instead of YAML and a fact-then-diff run that is faster; the candidate engine if Cloudfall ever swaps out Ansible, and hidden behind the engine contract either way |
| Coolify, Dokploy, CapRover | Self-hosted PaaS | Replaces | Same job (apps behind a proxy with TLS on your server); Docker containers driven from a dashboard versus native systemd driven from typed config with receipts |
| Dokku | Self-hosted PaaS | Replaces | Heroku-style git push to containers on one host; no typed state, no evidence, no operator |
| Kamal | Deployment | Replaces | Imperative container deploys over SSH with health checks; closest in spirit on deploys, but containers, no config-versus-observed audit, no databases, no operator |
| Docker Compose | Deployment | Alternative | Cloudfall runs applications as native units, not containers; container services land in the import gap report today |
| Kubernetes, k3s, Nomad, Swarm | Orchestration | Alternative | The thing Cloudfall exists to avoid at the 1 to 100 application scale; movability is a config change plus convergence, not a scheduler |
| ArgoCD, Flux | GitOps | Alternative | Kubernetes-only reconcilers; Cloudfall's config repository plus audit is the same idea without a cluster |
| Render, Heroku, Fly.io, Railway | Managed PaaS | Migrates from | The bill Cloudfall replaces; the Render importer, guided data migration, and DNS-paused cutover exist for this |
| Neon, Supabase, RDS | Managed database | Replaces or complements | Loopback PostgreSQL with receipted backups and restore drills replaces the compute bill; a team may keep a managed database and move only the backend |
| Grafana, Loki, Prometheus, Alloy | Observability | Built on | Bundled as one guarded logging stack over mTLS with typed alert rules |
| Datadog, New Relic, Better Stack | Observability SaaS | Replaces | Per-host pricing at this scale is another margin; the bundled stack covers logs, metrics, and alerts |
| sops, age | Secrets | Built on | Secrets encrypted in the repository and rendered to environment files outside the config |
| Vault, Doppler, Infisical | Secrets | Alternative | Central secret servers; Cloudfall keeps the sops file model for one to two servers |
| certbot, Let's Encrypt | TLS | Built on | Issued through the nginx role's webroot flow with timer compliance |
| pgBackRest, Barman, restic, Borg | Backups | Alternative today | Cloudfall runs its own receipted dumps and restore drills; point-in-time recovery on the roadmap may adopt one of these |
| PagerDuty, Opsgenie | Operations | Replaces at this scale | The operator remediates what its receipts prove reversible and pages you only for what it cannot undo |
| Devopness | Operations SaaS | Competes | The closest operational twin: closed SaaS, imperative dashboard; Cloudfall is AGPL with typed state the user owns |
| AI SRE agents (generic) | Operations | Alternative | Agents bolted onto imperative tools; their log says a tool was called. Cloudfall keeps the record of what the fleet looked like, what was proposed, who approved and what verify reported, and grants autonomy per operation class from it |
| Claude Code, Codex, any MCP client | Agent surface | Complements | The brain. They drive Cloudfall through `cloudfall mcp serve`, gate on its tool annotations with their own permission modes, and leave the record to Cloudfall; read-only tools free, server changes previewed until confirmed |

## What Cloudfall adds and what it replaces

Cloudfall **adds** something no tool on the list has: the record. A layer
of evidence between "the command exited zero" and "the platform is
healthy", and between "the agent called a tool" and "the agent can say
why". Receipts for every mutation, a verify step that turns a run into a
per-host outcome, an audit that reports drift with exit codes, restore
drills that prove a backup, one audit entry per decision holding the
snapshot, the diff, the approver and the verify result, and an operator
whose autonomy is derived from that record. Every other tool here treats
success as the absence of an error, and every agent surface here treats a
tool call as its own explanation.

Cloudfall **replaces** the self-hosted PaaS category (Coolify, Dokku,
Kamal), the managed PaaS bill (Render, Heroku), hand-rolled Ansible, and,
at one-to-two-server scale, observability and paging SaaS.

Cloudfall **does not replace** server provisioning (Terraform, hcloud),
the operating system (Debian), the agent that decides (any MCP client), or
the gate that client already applies from tool annotations. It is not the
brain and does not want to be. It uses Ansible, Grafana/Loki, sops/age,
and certbot underneath and does not pretend otherwise.

Cloudfall **rejects** orchestration (Kubernetes and friends), containers
as the application unit, and NixOS as the base, each as a deliberate
trade: the exit test is that any Linux admin can take over the server with
nothing but Debian knowledge.

## The two axes that separate Cloudfall from its closest rivals

The self-hosted PaaS tools are the category rivals on the deploy row, and
the Ansible MCP servers are the rivals on the agent row. Three axes split
them from Cloudfall:

1. **Where the truth lives.** Coolify, Dokploy, and CapRover hold state in
   their own database behind a dashboard; Kamal holds it in a YAML file plus
   whatever the containers are doing. Cloudfall holds it in schema-validated
   YAML the user owns, and audits the servers against it
2. **What counts as success.** All of them report a deploy as successful
   when the container starts or the health check passes once. Cloudfall
   writes a receipt, keeps observing, and the operator acts on drift and
   alerts afterwards
3. **Whether the agent can explain itself.** The AAP and ADT MCP servers
   let an agent run a playbook and log that it did. Cloudfall lets it run
   only a declared operation, and records what the fleet looked like, what
   the agent proposed, who approved and what verify reported. After an
   incident, one of these has a page to open

The migration axis is unclaimed by any of them: every self-hosted PaaS
starts from an empty server, and the managed PaaS providers are
structurally disincentivized to help you leave.

## Ansible Automation Platform: a bigger hand, not a conscience

Red Hat Ansible Automation Platform (AAP), and AWX, its free upstream, is
the one tool on the list that sits on Cloudfall's own engine. It overlaps
with Cloudfall on running and reacting, not on knowing what to run:

| Cloudfall | AAP equivalent |
|---|---|
| Always-on operator remediating alerts | Event-Driven Ansible rulebooks: "if this event arrives, run this playbook" |
| `cloudfall operator approve` | Approval nodes in Automation Controller workflows |
| Timer-driven jobs such as restore drills | Scheduled job templates |
| Receipts | Job history and logs: a record that a job ran, not proof of its result |
| Agent surface through `cloudfall mcp serve` | The AAP MCP server (jobs, inventory queries, monitoring through the controller API; a read-only mode, and a read-write mode that launches jobs; technology preview in AAP 2.6.4); the Lightspeed coding assistant and the ADT MCP server help write playbooks |
| `cloudfall mcp serve` as the agent's view of the fleet | The Lightspeed intelligent assistant, a chat in the controller UI; it answers from Red Hat docs and, as a technology preview, can query live AAP data through the AAP MCP server |
| Read-only operations dashboard | Controller web UI, which also launches jobs |

What AAP does not provide, and a team would have to write themselves:

- Health-gated deploys, symlink rollback, and the nginx, PostgreSQL, and
  Redis catalog
- A declared model of the servers and a drift audit against it; check mode
  is not an audit
- Receipted backups and restore drills that prove a backup restorable
- Any migration path off a PaaS: importers, guided data migration, cutover
- A logging and alerting stack; AAP integrates with one, it does not
  install one

AAP is sold as a per-managed-node enterprise subscription and AWX is heavy
to operate, so neither fits the one-to-two-server team Cloudfall targets.
For a larger brownfield team already on AAP the two combine: AAP remains
the hand that executes, the agent decides, and Cloudfall is the conscience
that gates what it may do and proves what it did. One catch for the
[brownfield design](brownfield-design.md):
AAP teams often keep their inventory in the controller database rather than
in files, so Cloudfall cannot read `group_vars`/`host_vars` there and would
need either an inventory kept in git or the AAP API.

## A typical complete stack

For the wedge use case, the full toolchain is:

- Terraform or the hcloud CLI creates a Debian 13 server on Hetzner
- Cloudfall baselines it, installs PostgreSQL, Redis, nginx, and the
  logging stack, deploys the application from a git ref with a health gate,
  and pauses for DNS verification
- Cloudflare or your registrar holds DNS; Cloudfall verifies the flip, it
  does not perform it
- An MCP client such as Claude Code drives the whole migration through
  `cloudfall mcp serve`, confirming each server-changing step
- The Cloudfall operator runs afterwards on a small always-on management
  host, watching alerts and audits

Nothing on that list is NixOS, Kubernetes, Docker, or a PaaS.

For a team that already runs Ansible, the intended stack (designed, not
built) is shorter:

- Their inventory and playbooks stay where they are; Cloudfall adds one
  `cloudfall` key per host and reads the rest in process
- Each playbook they choose to expose is declared as an operation with a
  risk level and a verify step; the MCP tool list is generated from that
- Claude Code or Codex drives it, gating on the tool annotations
- Cloudfall writes the audit entry for every run and the operator watches
  their existing alerts

Nothing on that list replaces Ansible, and nothing on it is Cloudfall's
own config format.
