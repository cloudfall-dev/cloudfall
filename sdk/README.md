# SDK

This module provides the stable Python API used by AI agents, future CLI and MCP
entry points, and other operational tooling.

Most of the SDK is read-only: load a project, validate it strictly, resolve
references, and expose inventory queries without depending on Ansible internals.

`cloudfall init [directory]` is the first command: it lays out a new project
(one directory per resource kind, an empty `secrets/` with a `.sops.yaml`
template, a README whose guide and example links point at the pinned
commit, an `AGENTS.md` operating contract for AI agents with a `CLAUDE.md`
pointing at it, a `.gitignore` for `tmp/`, and a `pyproject.toml` pinning
Cloudfall to the commit the running `cloudfall` was installed from, or to
`--rev`),
runs `git init` unless the directory already lies inside a repository, and
reports the files it wrote as JSON. `--description` writes one line saying
what the project manages into the README and `pyproject.toml`. When
`cloudfall` runs from a source checkout, the pin is refused unless the
checkout is clean (else `HEAD` is not the running code) and `HEAD` is on a
remote branch (else `uv sync` could not fetch it). It initializes the
current directory when none is named and refuses a non-empty one. Every
other command runs inside a project: the current directory when it is one,
else `--project`, else `CLOUDFALL_PROJECT`; relative paths such as the
`tmp/` defaults resolve against the project.

The agent contract is rendered from `cloudfall.commands`, the catalog of
every `cloudfall` and `cloudfall-engine` leaf command with its effect
(`read`: reads the project, evidence, or servers and writes only under
`tmp/`; `project`: writes project files on the controller; `servers`:
changes servers) and, for the last class, the gate that guards it (`--yes`,
a reviewed proposal, a declared backup). The tests walk both argparse trees
and fail when a command is missing from the catalog or a `--yes`-gated
command is not classed as changing servers, so `AGENTS.md` names every
server-changing command and no other.

`cloudfall add ssh-key|server-type|server` writes fleet resources into the
project: an `SshPublicKey` read from a key file, a `ServerType` from the
bundled Debian 13 baseline (no software RAID, the six baseline packages,
default-deny firewall with 22/80/443), and a `Server` that creates its type
on first use. Each document is schema-validated before it is written, the
project is validated afterwards, and a failed validation removes what was
written. Existing resources are never overwritten.

The lifecycle module adds the first mutating operations: `deploy()`,
`rollback()`, `restart()`, and `health()` with structured JSON results. They
verify artifacts (schema, identity, and recomputed digest) before anything
runs, then execute through the engine's process boundaries only — the
`cloudfall_engine` command-line contract for inventory rendering and the
engine's playbook contract for execution — never Ansible internals. Each
operation is split into a pure, testable execution plan and a thin executor:

```console
uv run cloudfall deploy --project config/examples crm-backend --release <release-id> --yes
uv run cloudfall rollback --project config/examples crm-backend --release <release-id> --yes
uv run cloudfall restart --project config/examples crm-backend --yes
uv run cloudfall health --project config/examples crm-backend
```

`deploy`, `rollback`, `restart`, and `data migrate` change servers only with
`--yes`. Without it they run every controller-side check (declared component,
verified artifact, declared database, source URL file) and print a `plan`
naming the target servers, exit `0`, so a request can be reviewed before it
runs.

`deploy` refuses to run when the built artifact is missing, misidentified, or
fails digest verification, and refuses to report success if the engine wrote
no release receipt. `health` exits `0` when every declared server passes the
component's declared health check and `1` otherwise.

`cloudfall import render` maps a Render blueprint onto schema-validated state
fragments plus environment files outside state, and returns a structured gap
report of unsupported services, assumptions, and required actions instead of
guessing silently.

## MCP server

`cloudfall mcp serve` (requires the `cloudfall[mcp]` extra) serves one
fleet over stdio, and its tools are Cloudfall's own commands. The fleet,
the schema and engine directories, and the evidence directories commands
share are fixed when the server starts, so a tool call cannot point at
another project, schema directory or engine. Read-only commands (`config_validate`, `inventory_show`, `audit`,
`services_status`, `health`, `observe`, `services_inspect`) carry read-only
annotations. `deploy`, `rollback`, `restart`, `migrate` and `data_migrate`
return their plan until called again with `yes: true`. Four tools have no
command of their own: `build_artifact`, and `converge_baseline`,
`converge_services` and `converge_domains`, which are destructive and run
only with `confirm_destructive: true`.

The fleet-declaring tools `add_ssh-key`, `add_server-type` and `add_server`
are `cloudfall add`: they write schema-validated resource files into the
project and never overwrite an existing resource. `cloudfall init`,
`operations approve`, `operator run` and `dashboard serve` are never tools:
the server starts inside an existing project, approving a recorded
operation is a person's command, and the loops run until stopped.

```console
uv run cloudfall mcp serve --project config/examples
uv run cloudfall mcp serve --repository ../fleet-ansible
uv run cloudfall mcp serve --project config/examples --list-tools
```

With `--repository` the server serves a team's own Ansible repository
instead: the read-only fleet commands, and one tool per declared operation,
which runs check mode and records a proposal for a person to approve.
Every tool answers with the same JSON envelope as the CLI, errors included,
so agents never need to parse free-form failures. An agent connected to this server
can drive the full migration path — import a blueprint, build artifacts,
converge the baseline, services, and domains, deploy with automatic
rollback, and audit the result — without shell access to any server.

The `migrate` tool (also `cloudfall migrate` on the CLI) chains all of that
as one resumable plan with persisted step progress: baseline, services,
builds, deployments, HTTP routes, a DNS-verification pause at the cutover
moment, TLS issuance, and a final evidence pass that requires a compliant
audit and healthy routes before declaring success. Without confirmation it
returns the plan preview; interrupted or paused runs resume at the first
incomplete step.

The SDK validates v1 Server, ServerType, Application, and Component resources and
builds a read-only typed index. The `cloudfall config validate` command is its first
system boundary.

`LoggingStack` validation additionally resolves backend, collector, application,
and component placement; enforces one stack per environment; and prevents the
first migration slice from disabling legacy log agents. Inventory output
contains certificate paths and package pins but never secret values.

`cloudfall inventory show` applications validated state into typed server, application, and
component records. It supports component-to-server placement queries and omits
secret references from serialized output.

`cloudfall audit` first validates normalized `ObservedServer` JSON snapshots, then
compares every server with its referenced `ServerType`. Checks cover OS and
service manager, RAID level/health/capacity, mounted filesystems, required and
forbidden packages, service state, and allowlisted configuration evidence.

```console
uv run cloudfall audit --project config/examples --observed tmp/observed
```

`cloudfall dashboard build` combines the same validated audit data with operational
signals such as disk pressure, failed services, and stale evidence. It emits a
dependency-free static dashboard plus `operations.json` for other clients:

```console
uv run cloudfall dashboard build --project config/examples \
  --observed tmp/observed --output-dir tmp/dashboard
```

The report and exit code distinguish compliant state, detected drift, malformed
inputs, and missing observations. This gives agents a stable decision boundary
without requiring them to infer server state from ad hoc SSH commands.
