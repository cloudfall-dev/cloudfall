# Render migration guide

This guide walks a Render Blueprint (`render.yaml`) application onto one
self-hosted Debian server with Cloudfall. It covers what maps automatically,
what needs manual work, and the vocabulary differences between the two
platforms.

It is also the greenfield way into Cloudfall. Cloudfall is the operator's
record for a fleet run by an AI agent; a team that already has servers and
Ansible brings them (the [brownfield design](brownfield-design.md), not
built yet), and a team leaving a PaaS gets a fleet this way. Either way the
result is the same: every step below leaves a receipt, the migration ends
with an audit rather than a green light, and the always-on operator takes
over afterwards with a record of everything it does. An agent can drive
the whole guide through `cloudfall mcp serve`; the proving run did, through
its predecessor `cloudfall-mcp`.

> **Status:** proven live. An application actually hosted on Render was
> cut over with its data and a real DNS flip on an owned domain, driven by
> an AI agent through `cloudfall-mcp`; see the
> [proving-run reports](proving-runs/). Read the import report carefully
> all the same; it is where the importer tells you what it did not do.

## Name mapping

Render and Cloudfall describe the same things with different words:

| Render | Cloudfall |
|---|---|
| Blueprint (`render.yaml`) | Config: YAML resources in your project, one directory per kind |
| Web service (`type: web`) | Component with an HTTP health check and an assigned listen port |
| Private service (`type: pserv`) | Component with a listen port and no public domain |
| Background worker (`type: worker`) | Component with a service-active health gate (no fabricated HTTP check) |
| Managed PostgreSQL (`databases:`) | `Service` of kind PostgreSQL: localhost-only bind, application-owned databases, peer authentication, no database passwords |
| Custom domain | `Domain` resource: Nginx virtual host plus Let's Encrypt TLS |
| Environment variables and env groups | Environment files under the project's `tmp/`, never in a resource; the config never contains secret values |
| Instance type / plan | Your server (VPS or bare metal) plus its server type (`ServerType`) |
| Deploy from GitHub | `cloudfall deploy`: artifact built from a git ref, health-check gate, symlink switch, automatic rollback |
| Render dashboard | Local read-only operations dashboard (`task dashboard`), rendered from receipts and observations; it shows the record, it does not act |
| Deploy log | Receipts: every deploy, data migration, backup and operator decision writes a schema-validated record of what it saw, did and verified |
| Service (Render's umbrella term) | Split into two concepts: infrastructure **services** (PostgreSQL, Nginx) and application **components** |

Not imported today; each lands in the gap report instead of being guessed at:

| Render | What to do instead |
|---|---|
| Cron job (`type: cron`) | Recreate the schedule as a systemd timer; timers are modeled and audited |
| Static site (`type: static`) | Serve via an Nginx site manually for now |
| Redis / key-value (`type: redis`, `keyvalue`) | Not mapped by the importer; declare a Redis `Service` by hand (loopback-only, receipted RDB backups) and point the component at it |
| Docker runtime (`runtime: docker` / `image`) | Not supported in v1; native Python and Node builds only |

## What you need

- A fresh Debian 13 server (a Hetzner Cloud VPS is the proven reference)
  with root SSH access
- Your `render.yaml` blueprint and access to your application repositories
- Python 3.14 and [`uv`](https://docs.astral.sh/uv/) on the machine you run
  Cloudfall from (your workstation works; no management server is required)
- A project: `uvx cloudfall init my-project`,
  then `cd my-project && uv sync`. Every command below runs from inside it

## Step 1: import the blueprint

```console
uv run cloudfall import render render.yaml --application acme --server h1
```

No `render.yaml`? Import the live workspace straight from the Render API
instead — put your API key alone into a file and run:

```sh
uv run cloudfall import render-api --api-key-file ~/.render/api-key \
  --application acme --server h1
```

The API path discovers services, workers, cron jobs, and managed PostgreSQL
(including its real major version), reads the actual environment variable
values, and rewrites any value that matches a managed database's connection
string to the local peer-authenticated socket URL. Secret values the API
does not expose are left empty in the environment file with an
action-required gap entry. Both paths share the same mapping and the same
gap report.

This maps every supported Render service onto Cloudfall config: web,
private, and worker services become components, managed PostgreSQL becomes
a PostgreSQL service with application-owned databases, and custom domains
become TLS-required domain routes.

Each web and private component is assigned an explicit listen port (starting
at 8100), written as `PORT` into that component's environment file. Your
application must honor `PORT`, exactly as it does on Render.

## Step 2: read the import report

The importer never guesses silently. `IMPORT-REPORT.md` records:

- everything that was **not** imported (cron, static, key-value, and
  container services), with a reason per entry
- every assumption it made (assigned ports, health checks, runtimes)
- every action required before cutover, including data migration and DNS
  steps

Treat the required-actions list as your migration checklist.

## Step 3: complete the config

Move the emitted fragment into your project's kind directories and declare
what the blueprint cannot know about: your SSH key and the server, whose
`ServerType` is created from the bundled Debian 13 baseline. Then validate:

```console
uv run cloudfall add ssh-key ~/.ssh/id_ed25519.pub --owner roman
uv run cloudfall add server h1 --address 203.0.113.10
uv run cloudfall config validate
```

## Step 4: fill in environment files

The importer creates environment files under `tmp/`, never in a resource,
including any `envVarGroups` from the blueprint. Copy the real secret values
from the Render dashboard into these files; secrets never enter the config,
and validation rejects them if they do.

## Step 5: run the migration plan

```console
uv run cloudfall migrate --build acme-api=main
uv run cloudfall migrate --build acme-api=main --yes
```

Without `--yes` the command shows the plan. With it, the plan executes step
by step: server baseline and firewall, infrastructure services, artifact
builds, health-gated deployments, HTTP routes, a DNS-verification pause at
the cutover moment, TLS issuance, and a final evidence pass requiring a
compliant audit and healthy routes. Progress persists after every step, so
a failure or the DNS pause resumes exactly where it stopped.

## Step 6: migrate the data

Save the Render database's connection URL alone into a local file, then
either run the data migration directly or add it to the plan:

```console
uv run cloudfall data migrate acme-postgresql --database acme --source-url-file ~/.render/acme-db.url
uv run cloudfall migrate --build acme-api=main --data acme=~/.render/acme-db.url --yes
```

Without `--yes` the command shows the plan. The engine dumps on the target
host, restores over the peer-authenticated socket, refuses a non-empty
target database, verifies per-table row counts, and writes a
`DataMigrationReceipt`. The row-count check is the verify step: the
migration is not done because the restore exited zero, it is done because
the counts match.

## Step 7: cut over DNS

The plan carries the cutover as explicit steps rather than a checklist:
`ttl-lower` measures the authoritative TTLs and pauses while they are
above 300 s, `parallel-run` requires the new server to serve every domain
before DNS changes, `dns-verify` pauses until public DNS points at the
declared proxy servers, and `rollback-window` records the pre-switch
answers with an exact revert recipe. Point your domains at the new server
when the plan pauses, then resume it; it finishes with TLS issuance and
the final evidence pass, which requires a compliant audit and healthy
routes. Each step writes to the persisted plan, so the record shows when
DNS moved and what was verified before and after.

## After the migration

- `task audit` proves the server matches the config, with distinct exit
  codes for drift
- `task dashboard` renders the read-only operations dashboard from the
  receipts and observations
- `cloudfall deploy` / `rollback` / `restart` / `health` replace Render's
  deploy buttons, with structured JSON results; the first three show a
  validated plan and change servers only with `--yes`
- The always-on operator replaces the pager: it watches declared alerts
  and audited drift, proposes remediations with the evidence attached,
  runs them after approval or under a declared policy, and verifies the
  result; see the [operator guide](operator-guide.md)
- Backups are restore-drilled on a timer with receipts, so "we have
  backups" is something the record can prove

What you have at the end is not a server that works but a fleet with a
record: who changed what, on which evidence, with what result. That
record is what lets you hand the routine work to the agent later.
