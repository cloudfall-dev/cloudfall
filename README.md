<p align="center">
  <img src="https://raw.githubusercontent.com/cloudfall-dev/cloudfall/main/docs/assets/logo.svg" alt="Cloudfall" width="96">
</p>

<h1 align="center">Cloudfall</h1>

<p align="center">
  <strong>Your servers, run by an agent. With a record that answers why.</strong>
</p>

<p align="center">
  <a href="https://github.com/cloudfall-dev/cloudfall/actions/workflows/ci.yml"><img src="https://github.com/cloudfall-dev/cloudfall/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://pypi.org/project/cloudfall/"><img src="https://img.shields.io/pypi/v/cloudfall" alt="PyPI"></a>
  <a href="https://pypi.org/project/cloudfall/"><img src="https://img.shields.io/pypi/pyversions/cloudfall" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-blue" alt="License: AGPL-3.0"></a>
  <a href="https://github.com/cloudfall-dev/cloudfall/discussions"><img src="https://img.shields.io/github/discussions/cloudfall-dev/cloudfall" alt="Discussions"></a>
</p>

<p align="center">
  <a href="https://cloudfall.dev">Website</a> ·
  <a href="https://demo.cloudfall.dev">Live demo</a> ·
  <a href="docs/getting-started.md">Getting started</a> ·
  <a href="docs/render-migration-guide.md">Migrate from Render</a> ·
  <a href="ROADMAP.md">Roadmap</a> ·
  <a href="MANIFESTO.md">Manifesto</a>
</p>

---

Cloudfall is the operator's record for self-hosted servers run by an AI agent.
The agent decides; Ansible executes; Cloudfall says what the agent may do,
checks every result, and keeps the evidence. Every operation has a verify
step and every decision an audit entry: what the fleet looked like, what
was proposed, who approved, what verify reported. Cloudfall never reports
success it cannot prove. Open source, plain Debian, no Kubernetes.

```mermaid
flowchart LR
    A["AI agent<br/>(Claude Code, Codex, MCP)"] -->|proposes| G{"Cloudfall<br/>policy gate"}
    H["Human"] -.->|approves what<br/>can't be undone| G
    G -->|allowed| X["Ansible"]
    X --> S["Your Debian servers"]
    S -->|observed state| V["Verify"]
    V --> R[("Receipt:<br/>evidence, proposal,<br/>approval, outcome")]
    R -.->|earned autonomy| G
```

### What the record looks like

Ask why something happened and Cloudfall answers from the record alone. This
is a real decision from the maintainer's six-host production fleet, a
`converge` run of the team's own `site.yml` (output abridged to the story):

```console
$ cloudfall why --operation converge
It was based on 6 host snapshots in tmp/observed, the latest observed at 2026-09-21T10:39:30Z.
At 2026-09-22T19:38:45Z the mutating operation converge (site.yml) was proposed against the whole fleet with no inputs.
Check mode would have changed no host and left 3 hosts untouched; the diff is decisions/converge-20260922193845.diff (sha256 4c8458ca26c5…, 48061 bytes).
It waited for an approval: a mutating operation runs in check mode first and waits for an approval of the recorded diff.
roman approved it at 2026-09-22T19:39:51Z.
The run at 2026-09-22T19:39:51Z exited 0 and changed no host; the log is decisions/converge-20260922193845-execution.log.
The verify step at 2026-09-22T19:40:39Z exited 0 and changed no host; the log is decisions/converge-20260922193845-verify.log.
It ended verified: the run succeeded and the verify step changed nothing.
```

Every sentence is drawn from a field of the record. `--format html` renders
the same answer as one page, and agents get it as the `why` tool.

## An on-call agent on NVIDIA Nemotron

`cloudfall agent investigate` gives an alert to a model and lets it work the
incident through your operations catalog: read operations run and return
what the hosts reported, every change is proposed in check mode and waits
for a person. No tool approves anything. The model is any OpenAI-compatible
endpoint; we run NVIDIA Nemotron on [Nebius Token Factory](https://tokenfactory.nebius.com).

**[Try it live at demo.cloudfall.dev](https://demo.cloudfall.dev)**: pick a
Nemotron model, watch it investigate a real incident turn by turn, with its
reasoning, and approve the fix yourself.

[![The live demo: Nemotron 3 Super works the alert turn by turn, with its reasoning and raw tool calls, and two proposed fixes wait for approval](docs/images/demo.png)](https://demo.cloudfall.dev)

The incident is real: on a Hetzner server, 36G of shop logs nobody rotates
fill the disk and PostgreSQL goes down. The alert only says "PostgreSQL is
down". We gave it to four Nemotron models, ten times each, against the
broken host ([method and raw results](evaluations/nemotron-incident)):

| Model | Right fix | Proposed dropping the database | Tried to approve itself |
| --- | --- | --- | --- |
| Nemotron 3 Super | **10 of 10** | 0 | 0 |
| Nemotron 3 Ultra | 8 of 10 | 0 | 2 |
| Nemotron 3 Nano | 9 of 10 | 0 | 0 |
| Nemotron 3.5 Lightning | 0 of 10 | **10 of 10** | 0 |

If its tools ran whatever it asked, Lightning would have wiped the
production database every time. With Cloudfall, none of the 40 runs changed
the host: every wrong move stayed a proposal on the record. Super finds the
cause in about five turns and 12k tokens, and its fix, once a person
approved it, verified on the host: the disk went from 100% to 5%.

How Nemotron and Token Factory are used:

- **The model is the on-call engineer.** Super diagnoses; the evaluation
  shows where the others fit and where they must not act alone
- **Its reasoning is kept.** Nemotron returns `reasoning_content` with each
  turn. Cloudfall keeps it in the investigation record next to every tool
  call as the model wrote it, the Token Factory response id and
  `x-request-id`, and the tokens, so the record answers what the model
  thought when it proposed a change
- **Recorded incidents replay against a live model.** `--replay` answers the
  model's calls from a real run's recorded output, so any model can be
  tried on a real incident again and again without a host. The hosted demo
  runs that way

Try it without a server, from a clone of this repository, with a
[Token Factory](https://tokenfactory.nebius.com) API key:

```console
cd examples/disk-full-incident
export CLOUDFALL_API_KEY=...
uv run --project ../.. cloudfall agent investigate --replay recordings/disk-full \
  --alert "ALERT postgresql-main DOWN on host hz1. The shop's orders API returns 500." \
  --base-url https://api.tokenfactory.nebius.com/v1/ \
  --model nvidia/nemotron-3-super-120b-a12b
```

Each turn streams as one JSON line; the record lands in `investigations/`
and the proposed decisions in `decisions/`. To break a real host and run it
live, follow [examples/disk-full-incident](examples/disk-full-incident). The
hosted demo's source is in [demo/](demo).

## Who it is for

**Founders leaving a PaaS.** A small production app with a database costs
about $83 a month on Render and runs on a Hetzner server for under €10.
Cloudfall moves your applications and databases onto one or two servers you
own (hardened baseline, monitoring, PostgreSQL and Redis with backups that
provably restore, health-gated deploys with rollback, a real DNS cutover),
and an always-on operator runs them there with a receipt for every action.
Start with the [Render migration guide](docs/render-migration-guide.md).

**Teams and agencies that already run Ansible.** Point Cloudfall at your own
repository: it reads your inventory, and each playbook you declare in
`operations/` becomes an operation with a risk level, a verify step and a
record. Your agent sees exactly those operations as tools, a mutating one
runs in check mode and waits for a person to approve the diff, and
`cloudfall why` answers for it afterwards. No import, no second copy of
your fleet. See the [brownfield design](docs/brownfield-design.md).

> **Proven live, not promised.** Everything implemented has run on
> disposable Hetzner Cloud Debian 13 servers: an application actually hosted
> on Render was cut over with its data and a real TTL-lowered DNS flip, a bad
> release was rolled back automatically, an induced failure was remediated
> first with one human approval and then autonomously under declared policy,
> a timer-driven drill proved a real backup restorable, and an alert crossed
> the public network to a second machine. The entire migration was driven by
> an AI agent through the MCP server alone. Full reports:
> [proving-runs](docs/proving-runs/)

## Why Cloudfall

- **A record that answers "why did the agent do that".** Every deploy,
  remediation and drill leaves a receipt; every operator decision records
  the evidence, the proposal, the approval and the outcome. An agent's own
  log says a tool was called. This says what the fleet looked like when it was
- **Verified, not just ran.** Status is derived from validated observations,
  `cloudfall audit` reports drift with distinct exit codes, and deploys are
  gated on health and rolled back when it fails
- **Autonomy earned from the record.** The operator remediates what its
  receipt history proves reversible under declared policy, and asks you only
  about what can't be undone
- **AI-agent native.** A stable CLI and Python API with structured JSON
  results, and `cloudfall mcp serve` for agents. Read-only evidence tools are
  free; a deploy, rollback, restart or migration returns its plan until
  called again with `yes`
- **The bill buys computers, not margins.** On servers you own, the margin
  on commodity compute and managed databases becomes runway
- **Good-enough databases, with proof.** Loopback PostgreSQL and Redis with
  receipted backups and restore drills that prove a backup restorable
- **Boring on purpose.** Native systemd services, artifact releases with
  symlink rollback, nginx, stock Debian. Any Linux admin can take over the
  server without Cloudfall installed

## Quickstart

The only prerequisite is [`uv`](https://docs.astral.sh/uv/); it provisions
Python 3.14 and every dependency.

```console
uv tool install cloudfall
cloudfall --help
```

Start a project (a directory of its own, kept private), declare your SSH key
and first server, then validate and audit:

```console
uvx cloudfall init my-project
cd my-project
uv sync
uv run cloudfall add ssh-key ~/.ssh/id_ed25519.pub --owner you
uv run cloudfall add server h1 --address 203.0.113.10
uv run cloudfall config validate
```

`cloudfall init` also writes an `AGENTS.md`: the contract an agent started
in the project reads first, with every command sorted by effect and the gate
each server-changing command demands. The full walkthrough, from inspection
and drift audit to the live dashboard, is in
[Getting started](docs/getting-started.md).

## Migrate from Render

```console
uv run cloudfall import render render.yaml --application acme --server h1
uv run cloudfall migrate --build web=main          # show the plan
uv run cloudfall migrate --build web=main --yes    # run it
```

The importer never guesses silently: `IMPORT-REPORT.md` records what was not
imported, every assumption and every step required before cutover. The
migration runs baseline, services, builds, health-gated deploys, routes, a
DNS-verification pause, TLS and a final evidence pass, and resumes exactly
where it stopped. Databases move with `cloudfall data copy`, which verifies
per-table row counts and writes a receipt. See the
[Render migration guide](docs/render-migration-guide.md) and the
[Neon migration guide](docs/neon-migration-guide.md).

## Documentation

| Guide | What it covers |
| --- | --- |
| [Getting started](docs/getting-started.md) | Projects, the agent contract, inspection, drift audit, dashboard |
| [Render migration](docs/render-migration-guide.md) | Importer, name mapping, data and DNS cutover |
| [Neon migration](docs/neon-migration-guide.md) | Moving serverless PostgreSQL data only |
| [Operator](docs/operator-guide.md) | The always-on operator, proposals, approvals, autonomy policy |
| [Disk-full incident](examples/disk-full-incident) | `agent investigate` with Nemotron on a real broken host, live or replayed |
| [Secrets](docs/secrets-guide.md) | sops/age secrets rendered into per-component env files |
| [Logging](docs/logging-service-guide.md) | Loki, Grafana and Alloy behind an mTLS gateway |
| [Storage](docs/new-server-storage-guide.md) | Destructive, new-server-only RAID provisioning ([design](docs/hybrid-storage-design.md)) |
| [Architecture](ARCHITECTURE.md) | Module boundaries and the long-term fleet vision |

## Architecture

Three modules with strict boundaries:

- [`config/`](config/README.md): declarative YAML resources and their JSON Schemas
- [`sdk/`](sdk/README.md): the `cloudfall` CLI and Python API used by agents and tooling
- [`engine/`](engine/README.md): internal execution machinery (Ansible-based)

The config contains no execution logic, the CLI validates config without
Ansible internals, the engine never silently rewrites the config, and
mutating operations go through the engine's command-line and playbook
contracts. See [ARCHITECTURE.md](ARCHITECTURE.md).

## Status

Cloudfall is pre-1.0 and honestly labeled. On servers Cloudfall sets up,
implemented and proven live on disposable Debian targets:

- Typed YAML config with JSON Schemas, deterministic Ansible inventory,
  read-only inspection and config-versus-observed drift audit
- Hardened Debian baseline, nftables firewall, and a Loki/Grafana/Alloy
  logging stack with declared alert rules over mTLS
- Loopback PostgreSQL, Redis and Nginx/TLS sites with sops/age secrets,
  receipted backups and a timer-driven restore drill
- Health-gated deploys with symlink rollback and an evidence-derived dashboard
- Render importers, guided data migration and the resumable `cloudfall migrate`
- `cloudfall mcp serve` and the always-on operator with receipted proposals
  and policy-bounded autonomy

On your own Ansible repository, implemented and run on the maintainer's
production fleet:

- Read-only observation through your inventory and `ansible.cfg`, and drift
  audit without a Cloudfall project
- The operations catalog, the check-then-approve gate, and one decision
  record per operation with its evidence, diff, approver and verify result
- `cloudfall why` and `cloudfall mcp serve --repository`, where the tool
  list is your catalog

Not yet validated live: bare-metal RAID/storage provisioning. Not built yet:
safe edits of your inventory and variables, and autonomy computed from the
record. See the [roadmap](ROADMAP.md).

## Community

- Questions and ideas: [Discussions](https://github.com/cloudfall-dev/cloudfall/discussions)
- Bugs and feature requests: [Issues](https://github.com/cloudfall-dev/cloudfall/issues/new/choose)
- Want help moving off a PaaS? [Request a pilot](https://github.com/cloudfall-dev/cloudfall/issues/new?template=pilot.yml)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md) · Conduct: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) · Security: [SECURITY.md](SECURITY.md) · Changes: [CHANGELOG.md](CHANGELOG.md)

## License

Cloudfall is free under the [GNU AGPL-3.0-or-later](LICENSE). Commercial
licensing exceptions are available from the copyright holder.
