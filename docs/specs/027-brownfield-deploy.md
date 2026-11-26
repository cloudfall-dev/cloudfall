# S-027: Deploy, rollback, restart and health on a team's own Ansible repository

status: approved
serves: G-4

## Problem

G-4 asks that Cloudfall work on a team's own Ansible inventory and config, not only on a
Cloudfall project. Its check: deploy, rollback, restart, health, backups and the restore drill
run on a team's own Ansible repository with no Cloudfall project beside it. The accepted plan
for it (cloudfall-dev/cloudfall#188) puts deploy first, because it is the operation a team
leaving a PaaS needs first, and because backups (W-3) reuse what it adds to the `cloudfall`
variables. This spec is its item W-2 (cloudfall-dev/cloudfall#618).

Part of the brownfield path is built. On a repository with an `ansible.cfg` that names an
inventory, `inventory show`, `audit` and `observe` read the fleet from the team's inventory:
each host's `cloudfall` block becomes a `Server`, `cloudfall_defaults` gives group defaults and
`cloudfall_baselines` the baseline catalog (`sdk/src/cloudfall/ansible_reader.py`). The
operations catalog, `decisions`, `why` and `mcp serve --repository` serve the team's own
playbooks. None of that deploys anything:

- the reader builds `Server` and `Baseline` documents only; there is no place in the team's
  variables to declare an `Application` or a `Component`, and the `Server` schema refuses any
  unknown key in the `cloudfall` block
- `deploy`, `rollback`, `restart` and `health` acquire a project only (`Fleet.acquire` in
  `sdk/src/cloudfall/app.py`); in a repository they exit `PROJECT_INVALID`
- every lifecycle plan in `sdk/src/cloudfall/lifecycle.py` starts with
  `cloudfall-engine inventory render --project`, and runs `ansible-playbook` with the
  rendered inventory alone under the engine's own `ansible.cfg`, so the team's SSH keys,
  users, `become` settings and jump hosts never reach the run
- `cloudfall-engine artifact build` reads a component from a project only
- `mcp serve --repository` serves no `deploy`, `rollback`, `restart` or `health` tool

So a team that runs Ansible must still create a Cloudfall project, a second description of its
servers, to get a health-gated deploy and a rollback. That second copy is what the brownfield
design rules out: one config, theirs.

## Behaviour

### Declaring applications and components

Two more group variables join `cloudfall_baselines`, read by `sdk/src/cloudfall/ansible_reader.py`
the same way, here in `inventories/production/group_vars/all/cloudfall.yml`:

```yaml
cloudfall_applications:
  crm:
    linux_user: crm
    approval: automatic
    components: [crm-backend]
cloudfall_components:
  crm-backend:
    application: crm
    repository: {url: "https://github.com/acme/crm.git"}
    runtime: {type: python, version: "3.13", package_manager: uv}
    deployment:
      strategy: artifact-symlink
      servers: [web-1, web-2]
      install_root: /srv/apps/crm/crm-backend
      retain_until_cleanup: true
    service: {manager: systemd, name: crm-backend, command: [.venv/bin/gunicorn, crm.wsgi]}
    health_check: {type: http, port: 8000, path: /health}
```

- Each is a mapping of resource id to the fields of the `Application` or `Component`
  resource, written in snake_case and camelized as `cloudfall_baselines` is (the keys under
  `environment` are the team's data and are not rewritten, like `labels`). Each entry becomes
  one document that goes through the same schemas and reference checks as a project's
  (`config/schemas/v1/application.schema.json`, `config/schemas/v1/component.schema.json`,
  unchanged), so every reader of `PlatformInventory` works on it unchanged
- `deployment.servers` names inventory hosts. A host it names must carry a `cloudfall` block;
  one that does not, or that the inventory does not hold, is a reference error
  (`CONFIG_INVALID`) naming the component and the host
- An entry that differs between two hosts the variable reaches fails the read with
  `ansible_catalog_conflict`, a variable that is not a mapping of id to mapping with
  `ansible_catalog_invalid`, both `INVENTORY_UNREADABLE`, as for `cloudfall_baselines`
- Cloudfall reads these variables and never writes them. The per-host `cloudfall` block and
  the `Server` schema do not change
- `docs/compatibility.md` lists `cloudfall_applications` and `cloudfall_components` beside
  `cloudfall`, `cloudfall_defaults` and `cloudfall_baselines` as contract

### Finding the repository

`deploy`, `rollback`, `restart`, `health`, `release approve`, `operator approve`, and in the engine
`cloudfall-engine inventory render` and `cloudfall-engine artifact build`, take the
`--inventory` flag `inventory show`, `audit` and `observe` take (`FleetArgs`,
`InventoryFleet` in `sdk/src/cloudfall/app.py`), with the same rule:

1. `--inventory`, naming the team's inventory
2. otherwise, when neither `--project` nor `CLOUDFALL_PROJECT` names a project and the
   current directory is no project, the inventory an `ansible.cfg` in the current directory
   names
3. otherwise the project, exactly as today

`--inventory` with `--project` is a usage error naming `inventory` (`ONE_FLEET`). A directory
that is a project and also holds an `ansible.cfg` is read as the project. Relative paths
(`tmp/`, `record/`, `decisions/`) resolve against the current directory, which for a
repository is its root, as `inventory show` and `observe` resolve them today.

### What runs

The lifecycle commands work in a repository as in a project, with these differences only:

- **The inventory render** (`_render_inventory_step` in `sdk/src/cloudfall/lifecycle.py`)
  runs `cloudfall-engine inventory render --inventory <team inventory>`, which renders the
  same Cloudfall variables from the read fleet (`engine/src/cloudfall_engine/cli.py`,
  `engine/src/cloudfall_engine/ansible_inventory.py`) into `tmp/ansible-inventory.json`
- **The playbook run** (`_playbook_step`) passes the team's inventory as the first
  `--inventory` and the rendered one as the second, and sets `ANSIBLE_CONFIG` to the
  repository's `ansible.cfg` and `ANSIBLE_ROLES_PATH` to the engine's roles followed by any
  roles path the team's config sets, so a team role of the same name never replaces an
  engine role. A host is therefore reached the way the
  team's own playbooks reach it: its user, key, port, `become` settings, SSH arguments and
  vault come from the team's inventory and config. The engine's playbooks
  (`engine/ansible/playbooks/deploy.yml`, `rollback.yml`, `restart.yml`, `health.yml`) and
  the `cloudfall_deploy` role are unchanged
- **The component** is found in the read fleet (`_component`, `find_component`) rather than
  by validating a project again
- **Build**: `cloudfall-engine artifact build <component> --ref <ref>` in a repository builds
  from the component declared in `cloudfall_components` and writes
  `tmp/artifacts/<component>/<release>.tar.gz` and its metadata, as in a project

Everything else is as in a project: the artifact check (`verify_artifact`), the component lock
under `tmp/locks/`, the release command once per deploy, the health gate and the automatic
rollback when it fails, retention (D-15), the environment file per release (D-21) from
`--env-file` or the previous release, a manual application's release approval (S-008) read
from `decisions/releases/` and written there by `release approve`, the release receipt with
one entry per server (D-13) under `record/releases/`, the release command's output under
`tmp/output/releases/`, the plan without `--yes`, and the exit codes. `deploy`, `rollback`,
`restart` and `health` in a repository declare `INVENTORY_UNREADABLE` among their exit codes,
as `inventory show` does.

The record a repository gains lives at the same paths a project uses (D-16, D-24): `record/`
and `decisions/` committed, `tmp/` ignored. Nothing else in the repository is written: no
inventory file, variable file, playbook, role or `ansible.cfg` of the team's changes.

### Through an agent

`mcp serve --repository` (`sdk/src/cloudfall/agent_server.py`) adds `deploy`, `rollback`,
`restart`, `health` and `build_artifact` to `FLEET_COMMANDS`' tools, with the repository, its
inventory and every directory they write bound when the server starts (`bound_arguments`), as
for the project tools.

- `health` and `build_artifact` run on a call; `build_artifact` writes under `tmp/artifacts/`
  only
- `deploy`, `rollback` and `restart` go through the agent gate of S-018. A repository can
  declare no `OperatorPolicy`, so no grant ever covers a call: a call with `yes: true`
  answers `PRECONDITION` with `operator_approval_required`, writes one proposal to
  `record/operator/proposals/`, and starts no `ansible-playbook`. A person runs it with
  `cloudfall operator approve <id>` in the repository, at a terminal; the proposal's binding
  (D-20) covers the repository's git commit and its uncommitted files outside `decisions/`,
  `record/` and `tmp/`
- `release approve`, `operator approve`, `decisions approve`, `converge_*`, `migrate`,
  `data_copy` and the other project tools stay off the repository's tool list; the
  repository's instructions name `cloudfall operator approve` for a deploy, a rollback and a
  restart
- `why --host <host>` in the repository lists those proposals and their outcome, as it lists
  them in a project (S-018-16)
- S-018-18 said the repository's tool list did not change with S-018; this list is the one
  its test pins from now on

### Project mode

A project reads, renders, runs and answers exactly as before: the same plan, the same single
rendered inventory under the engine's `ansible.cfg`, the same paths and the same exit codes.
The only change it sees is the new optional `--inventory` flag on the commands above.

### Docs

`sdk/README.md` shows the two variables, the four commands and the build in a repository, and
its `mcp serve --repository` paragraph names the new tools and the approval;
`changelog.d/` gains an entry under Added. Touches `sdk/src/cloudfall/ansible_reader.py`,
`sdk/src/cloudfall/app.py`, `sdk/src/cloudfall/lifecycle.py`,
`sdk/src/cloudfall/agent_server.py`, `sdk/src/cloudfall/release_approval.py`,
`engine/src/cloudfall_engine/cli.py`, `engine/src/cloudfall_engine/ansible_inventory.py`,
`sdk/tests/ansible_repository.py` (the fixture repository), `docs/compatibility.md`,
`sdk/README.md`, `changelog.d/` and `docs/proving-runs/`.

## Acceptance criteria

- S-027-1: In a repository whose `group_vars/all` declares `cloudfall_applications` with `crm`
  and `cloudfall_components` with `crm-backend` on `web-1`, `cloudfall inventory show` run in
  the repository exits 0 and lists application `crm` and component `crm-backend` with server
  `web-1`
- S-027-2: An entry of `cloudfall_components` set differently in two group files reaching two
  hosts exits `INVENTORY_UNREADABLE` with `ansible_catalog_conflict` naming the component; a
  `cloudfall_components` that is a list exits `INVENTORY_UNREADABLE` with
  `ansible_catalog_invalid`; a component whose `deployment.servers` names a host with no
  `cloudfall` block, or a host the inventory does not hold, and an entry with a field its
  schema does not know, each exit `CONFIG_INVALID` naming the component
- S-027-3: `cloudfall-engine artifact build crm-backend --ref main` run in the repository exits
  0 and writes `tmp/artifacts/crm-backend/<release>.tar.gz` and its metadata, which
  `verify_artifact` accepts
- S-027-4: `cloudfall deploy crm-backend --release R` without `--yes` in the repository exits 0
  with the plan naming `web-1`, starts no `ansible-playbook`, and writes no file outside
  `tmp/`
- S-027-5: `cloudfall deploy crm-backend --release R --yes` in the repository exits 0; the
  `ansible-playbook` it starts has the team's inventory as its first `--inventory` and
  `tmp/ansible-inventory.json` as its second, `ANSIBLE_CONFIG` equal to the repository's
  `ansible.cfg` and the engine's roles in `ANSIBLE_ROLES_PATH`; a connection variable set only
  in the team's `group_vars` (`ansible_connection: local`) is in effect for the play; and
  `record/releases/crm-backend.json` validates against its schema with one entry for `web-1`
  naming release `R`
- S-027-6: A deploy in the repository whose health check fails exits `ENGINE_STEP_FAILED` and
  leaves `web-1` on the previous release, as in a project
- S-027-7: In the repository, `rollback crm-backend --release R0 --yes` switches `web-1` back to
  `R0` and `restart crm-backend --yes` restarts it behind the health check; both exit 0 and
  neither writes a release receipt
- S-027-8: `cloudfall health crm-backend` in the repository exits 0 when `web-1` passes the
  health check and `UNHEALTHY` when it does not, and writes no file outside `tmp/`
- S-027-9: With `crm` declared `approval: manual` in `cloudfall_applications`, `deploy
  crm-backend --release R --yes` in the repository exits `PRECONDITION` with
  `deployment_approval_missing`; after `cloudfall release approve crm-backend --release R` at
  a terminal there, which writes one record under `decisions/releases/`, the same deploy
  exits 0
- S-027-10: After S-027-5 to S-027-9 in a git repository whose `.gitignore` holds `tmp/`,
  `git status --porcelain` lists only new files under `record/` and `decisions/`, and no file
  of the team's (inventory, variables, playbooks, `ansible.cfg`) has changed
- S-027-11: S-027-1 and S-027-5 hold with the inventory as an INI hosts file and its
  `host_vars` and `group_vars` as YAML
- S-027-12: `deploy --inventory <inventory> --project <project>` is a usage error naming
  `inventory`; a directory holding both a project and an `ansible.cfg` deploys from the
  project; `deploy missing --release R --yes` in the repository exits `PRECONDITION` naming
  the component; an `ansible.cfg` naming an inventory that does not exist exits
  `INVENTORY_UNREADABLE`
- S-027-13: In the example project, the plans of `deploy`, `rollback`, `restart` and `health`
  and the argv and environment of the `ansible-playbook` each starts (one rendered
  `--inventory`, the engine's `ANSIBLE_CONFIG`) are the same as before this change, and
  `cloudfall --schema` differs only by the `--inventory` flag on the commands this spec names
- S-027-14: `cloudfall mcp serve --repository <repo> --list-tools` lists `deploy`, `rollback`,
  `restart`, `health` and `build_artifact` beside the tools it listed before, and no
  `release_approve`, `operator_approve`, `converge_*`, `migrate` or `data_copy`; its `deploy`
  tool called with `yes: true` returns `PRECONDITION` with `operator_approval_required` and
  `context.command` `cloudfall operator approve <id>`, writes one proposal under
  `record/operator/proposals/` that validates against its schema, and starts no
  `ansible-playbook`; its `health` tool runs and answers the health verdict
- S-027-15: `cloudfall operator approve <id>` at a terminal in the repository, on the proposal
  of S-027-14, runs the deploy with the stored arguments and the proposal ends `verified`; a
  change to a team file (a variable file or a playbook) after the proposal makes it refuse with
  `proposal_binding_changed` before any `ansible-playbook` starts
- S-027-16: `cloudfall why --host web-1` in the repository lists the proposal of S-027-14 with
  the tool `deploy`, the approver `human` and its status
- S-027-17: `sdk/README.md` names `cloudfall_applications`, `cloudfall_components`, the four
  commands and `artifact build` in a repository, and the new tools of `mcp serve --repository`
  with `cloudfall operator approve`; `docs/compatibility.md` lists the two variables;
  `changelog.d/` holds an entry for #618
- S-027-18: A proving run recorded in `docs/proving-runs/`: on a disposable Debian host listed
  in an INI or YAML inventory with playbooks Cloudfall did not write, an agent through
  `cloudfall mcp serve --repository` builds and deploys an application (a person approving with
  `cloudfall operator approve`), restarts it, sees a failing release roll back by itself, rolls
  back explicitly and reads a passing `health`; afterwards `git status` shows changes under
  `record/` and `decisions/` only

## Out of scope

- Backups and the restore drill on a repository (W-3, cloudfall-dev/cloudfall#619), and
  `cloudfall get host` (W-4, cloudfall-dev/cloudfall#620). This spec leaves room for them: a
  `Service` catalog would be one more `cloudfall_*` variable read the same way
- Services, domains, logging stacks, alert rules, alert channels, SSH keys and operator
  policies declared in a repository; a component whose environment or route refers to one
  fails its reference check as it would in a project without it
- `secrets render` in a repository: the environment reaches a release through `--env-file` or
  the previous release's file (D-21), from whatever secret tool the team uses
- An `OperatorPolicy` in a repository, so no agent call self-confirms there
- `migrate`, `data copy`, `domains apply` and the `converge_*` tools in a repository
- Writing the team's files: Cloudfall reads `cloudfall_applications` and `cloudfall_components`
  and never edits them; safe edits are M17
- `owns`, and running Cloudfall's baseline or service roles on a team's hosts
- `cloudfall init` emitting a plain Ansible inventory, so greenfield and brownfield share one
  path (left out of #188, may follow this spec)
- A `--repository` flag or `CLOUDFALL_REPOSITORY` variable on these commands
- Updating the docs that still say the brownfield path is not built (W-1,
  cloudfall-dev/cloudfall#617)

## Decisions relied on

- D-13: a deploy in a repository writes one release receipt per component, one entry per server
- D-15: retention works the same on a repository's hosts
- D-16: a repository's record lives in a committed `record/`, as a project's does
- D-20: an agent's proposal in a repository binds the repository's commit and its uncommitted
  files outside `decisions/`, `record/` and `tmp/`
- D-21: each release reads its own environment file, so a rollback restores it
- D-24: raw output stays under the ignored `tmp/`
- D-27: a repository's gated call keeps today's approval object, since no grant covers it

## Issues

## Verification

Filled in when the spec reaches `built`: one line per criterion id, saying how it was
checked against the default branch (beyond its tests) and the result.
