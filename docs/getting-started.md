# Getting started

The longer walkthrough behind the [README](../README.md) quickstart: starting a
project of your own, the contract an agent operates it under, drift audit, and
the operations dashboard.

## Start your own project

Your servers, applications, evidence, and any playbooks of your own belong in
a project: a directory of its own, kept private. Cloudfall's wheel bundles the
schema catalog and the Ansible engine, so the project needs no checkout of
this repository. `cloudfall init` is the first command, and it pins the
project to the released Cloudfall that ran it. Without a directory argument
it initializes the current directory, which must be empty:

```console
uvx cloudfall init my-project
cd my-project
uv sync
```

The project holds one directory per resource kind (`servers/`,
`server-types/`, `ssh-public-keys/`, `applications/`, `components/`,
`services/`, `domains/`, `alert-rules/`, `operator-policies/`,
`logging-stacks/`), a `secrets/` directory with a `.sops.yaml` template for
its encrypted fragments, a README with the next steps (pass `--description`
to say what the project manages), an `AGENTS.md` stating the terms on which
an AI agent operates the project (see below), a `.gitignore` for the
runtime `tmp/` directory where evidence and receipts land, a fresh git
repository, and a `pyproject.toml`:

```toml
[project]
name = "my-project"
version = "0"
requires-python = ">=3.14"
dependencies = ["cloudfall==<version>"]

[tool.uv]
package = false
```

Bump that version and run `uv sync` to move the project to a newer
Cloudfall. To run an unreleased Cloudfall in a project, point the dependency
at a checkout or a commit with uv's own
[`[tool.uv.sources]`](https://docs.astral.sh/uv/concepts/projects/dependencies/#dependency-sources);
Cloudfall itself only ever pins a release.

Only the kind directories are read as resources, so playbooks, roles, docs,
and tooling files may live anywhere else in the project. Commands find the
project on their own: the current directory when it is one, else
`--project`, else `CLOUDFALL_PROJECT`. Declare your SSH key and your first
server with `cloudfall add`; the server's type is created from the bundled
Debian 13 baseline when it does not exist yet, and every written file is
plain YAML you can edit (the reference set under `config/examples/` shows
every kind). Then validate, render, inspect, and audit from inside the
project:

```console
uv run cloudfall add ssh-key ~/.ssh/id_ed25519.pub --owner roman
uv run cloudfall add server h1 --address 203.0.113.10
uv run cloudfall config validate
uv run cloudfall-engine inventory render --output tmp/ansible-inventory.json
uv run cloudfall-engine playbook run inspect --inventory tmp/ansible-inventory.json --extra-vars cloudfall_inspect_output_directory=$PWD/tmp/observed
uv run cloudfall audit --observed tmp/observed
uv run cloudfall-engine playbook run playbooks/proxy.yml --roles roles
```

Bundled playbooks are addressed by name (`cloudfall-engine playbook list`
shows them); project playbooks by path, with `--roles` directories searched
before the bundled roles. To move a project to a newer Cloudfall commit, bump
`rev` and run `uv sync`.

## The agent contract

Humans and AI agents both edit the resources and both run the CLI, with a
human approving anything that changes a server. `cloudfall init` writes
that arrangement down as the project's `AGENTS.md`, the operating contract
an agent started in the directory (Claude Code, Codex, or `cloudfall mcp serve`)
reads first: the canonical `uv run cloudfall` invocation and project
resolution, every command sorted by effect (changes nothing on servers,
writes project files, changes servers) with the gate each server-changing
command demands, the JSON-on-stdout, JSON-error-on-stderr output contract
and exit codes, what the agent may read under `tmp/`, the rule that secret
values are never read or written by an agent, and what is committed. A
one-line `CLAUDE.md` points Claude Code at it. The classification is
generated from the same catalog the tests check against the CLI, so a new
command cannot ship unclassified.

## Inspect servers and audit drift

Each `Server` references a reusable `ServerType`
describing its required Debian version, software RAID, filesystem capacity,
packages, systemd services, and allowlisted configuration evidence.

Collect a read-only snapshot from every reachable server, then compare it
with the config. Workflow commands use [Task](https://taskfile.dev)
(`brew install go-task` / `apt install task`); every task is a thin wrapper
over a `uv run` command, so if you prefer not to install Task, copy the
underlying command from [`Taskfile.yml`](../Taskfile.yml):

```console
task inspect
task audit
```

The direct equivalent of `task audit`, for example, is:

```console
uv run cloudfall audit --project config/examples --observed tmp/observed
```

Snapshots are written to `tmp/observed/<server>.json` with mode `0600` and
never include configuration-file contents. The audit emits one JSON report:
exit code `0` compliant, `1` drift, `2` invalid input, `3` compliance unknown.

## Operations dashboard

Cloudfall renders validated config, host observations, and audit results into
a local read-only dashboard with an evidence-derived task queue and a public-service
lifecycle (planned → ready → deployed → configured → healthy):

```console
task dashboard
```

The build writes `tmp/dashboard/index.html` and machine-readable
`tmp/dashboard/operations.json`. Missing receipts or observations remain
visible as `no` or `unknown`; Cloudfall does not infer deployment merely because
a playbook exists.

For a live view, run the dashboard as a server on the management host (a
VPS or your local machine):

```console
task dashboard:serve
```

`cloudfall dashboard serve` re-derives the projection from state and evidence
on a refresh interval (default 10 seconds) and serves a page that updates
itself in place; `--inspect-services` additionally probes DNS, TLS, origin,
and public routes on every refresh, so service health is live. Server
hardware evidence still comes from observation snapshots: schedule
`task inspect` (cron or a timer) on the management host to keep it fresh.
The server binds `127.0.0.1:8100` by default; set `DASHBOARD_HOST`,
`DASHBOARD_PORT`, and `DASHBOARD_REFRESH` to override. Once it listens it
prints one JSON line with its URL, then serves until stopped. The projection stays
strictly read-only either way.

