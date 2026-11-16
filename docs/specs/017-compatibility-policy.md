# S-017: Compatibility policy

status: built
serves: G-4

## Problem

A team's project is its own repository, pinned to one Cloudfall release
(`cloudfall==<version>` in the project's `pyproject.toml`), and it outlives that release.
Nothing says what the project can rely on when the pin moves (cloudfall-dev/cloudfall#228):

- D-11 sets "kept for one minor release" for one rename (`ServerType` to `Baseline`); every
  other name, field, exit code and tool name has no stated lifetime
- `CONTRIBUTING.md` says each release records its interface changes with
  `treaty changelog-add cloudfall.app:app`, and the `AGENTS.md` that `cloudfall init` writes
  tells agents to read `cloudfall changelog`. The release bot never runs that step:
  `sdk/src/cloudfall/schema-changelog.json` does not exist, so `cloudfall changelog` answers
  with no entries
- A project carries no format version. Nothing at start reads the project's `pyproject.toml`;
  a project too old or too new for the installed Cloudfall is found out one
  `CONFIG_INVALID` at a time, on whichever command meets the first unknown name

Q-3 of `docs/product.md` already promises that configs which validate today keep validating
and rendering the same, or the change is a breaking one with a changelog entry. This spec
turns that promise into rules a release can be checked against, and a refusal a project
meets once, at start, with one way forward.

## Behaviour

### What is stable

These surfaces are the contract between a Cloudfall release and the projects, agents and
scripts that use it. Every other module, playbook variable and file is internal and may
change in any release.

| Surface | What it is, concretely |
|---|---|
| Resource schemas | `config/schemas/v1/*.schema.json` for the eleven kinds of `ResourceKind` in `sdk/src/cloudfall/domain.py` (`Server`, `Baseline`, `Application`, `Component`, `Domain`, `SshPublicKey`, `LoggingStack`, `Service`, `AlertRule`, `AlertChannel`, `OperatorPolicy`), all `apiVersion: cloudfall/v1`; their kind directories (`servers/`, `baselines/` and so on); the operation catalog in `operations/` (`operation.schema.json`); and, for a team's own inventory, the variables `cloudfall`, `cloudfall_defaults` and `cloudfall_baselines` read by `sdk/src/cloudfall/ansible_reader.py` |
| The CLI envelope | The JSON both CLIs print on stdout: `ok` (true exactly when the exit code is 0), `data` with the verdict in `data.status`, `error` with its code in `error.context.code`, `meta`, and `warnings` (each with a `code`); the streaming form of `operator run` and `dashboard serve` (one envelope per event, then `meta.end`). Command paths, their flags and each command's output schema, as `cloudfall --schema` and `cloudfall-engine manifest` print them |
| Exit codes | Each name and number in the exit-code table: `PROJECT_INVALID` 79, `CONFIG_INVALID` 80, `DRIFT` 83, `UNKNOWN` 84, `INVENTORY_UNREADABLE` 85, `RECORD_INVALID` 86, `ENGINE_STEP_FAILED` 87, `UNHEALTHY` 88, `CHECK_FAILED` 89, `NOT_VERIFIED` 90, `INCOMPLETE` 91, `MODEL_UNAVAILABLE` 92, `INVESTIGATION_INCOMPLETE` 93, `INVESTIGATION_STOPPED` 94 in `sdk/src/cloudfall/app.py`; 79 to 82 in `engine/src/cloudfall_engine/cli.py`; treaty's shared codes (`CONFLICT`, `NOT_FOUND`, `PRECONDITION`, `INPUT_REQUIRED` and the rest); and the snake_case codes in `error.context.code` and the warning codes (`DEPRECATED_NAME`, `ELASTICSEARCH_VERSION_ENDING`) |
| Record and receipt schemas | The documents Cloudfall writes as evidence: `DeploymentReceipt`, `ReleaseReceipt`, `BackupReceipt`, `EnvironmentReceipt`, `Artifact`, `ObservedServer`, `ObservedDomain` (under `tmp/`), `OperationDecision` (`decisions/`), `ReleaseApproval` (`decisions/releases/`), `OperatorProposal` (`tmp/operator/proposals/`) and `AgentInvestigation` (`investigations/`), each checked by its schema in `config/schemas/v1/` |
| MCP tool names | The names `cloudfall mcp serve` lists: one per served command, its path joined with `_` (`config_validate`, `domains_apply`, `import_render-api`; the sets are `PROJECT_COMMANDS` and `FLEET_COMMANDS` in `sdk/src/cloudfall/agent_server.py`), the provided tools `build_artifact`, `converge_baseline`, `converge_services`, `converge_domains`, and `operation_<id>` for each declared operation (`operation_tool_name`); each tool's input schema follows its command's flags |

Records are evidence, so they get a stronger rule than names: a record any release wrote is
read by every later release with the same `apiVersion`. A record is never rewritten, by
`config upgrade` or anything else, and its fields are never deprecated within `cloudfall/v1`;
a reader keeps understanding a field older releases wrote. `cloudfall why` answers from old
records as it does from new ones (G-2).

The policy is written for people in `docs/compatibility.md`, linked from `README.md` and
`CONTRIBUTING.md`; this spec is its source.

### What a release may change

A change is **additive** when every project, record and script that worked before still
works the same: a new kind, a new optional field, a new command, a new flag whose default
keeps today's behaviour, a new key in `data`, a new warning code, a new exit code for a new
condition, a new MCP tool. Anything else is **breaking**: a removed or renamed name, a new
required field, a narrowed enum or pattern, a changed default, another exit code for a
condition that had one, a key removed from `data`.

| Release | Before 1.0 (`0.N.Z`) | From 1.0 (`X.Y.Z`) |
|---|---|---|
| Patch | Fixes only: behaviour moves to what the docs and schemas already say. No additive change to a stable surface, no new deprecation. A config that validated still validates, except for a security fix listed under `Security` in `CHANGELOG.md` | The same |
| Minor | Additive changes; deprecations; removal of a name deprecated in an earlier minor; a format version step (below). A breaking change without a deprecation path only when no rewrite can exist, listed under `Changed` or `Removed` with what a project must do | Additive changes and deprecations only |
| Major | (none before 1.0) | Removal of deprecated names; a breaking change |

One existing rule fits this table and stays: a third-party version that has passed its end of
life leaves the accepted set in the next minor, after a warning ahead of it and with a
changelog entry (S-012, D-12). That is a version Cloudfall stops installing, not a name a
project uses, and it has no rewrite.

### How long a deprecated name stays

This generalises D-11 and does not supersede it: D-11 is the first name to go through it, with
the window it already set (`ServerType`, `server-types/`, `serverType`, the
`cloudfall_server_types` variable, `add server-type` and `add server --type`, deprecated in 0.8.0 and
gone in 0.9.0, as the `Deprecated(since="0.8.0", removed_in="0.9.0")` marks in
`sdk/src/cloudfall/app.py` say).

A stable name is deprecated in a minor release, and from that release:

- **It keeps working**, with the same result as its replacement, in every release of that
  minor line. Before 1.0 it is removed no earlier than the next minor; from 1.0, no earlier
  than the next major
- **Every use warns.** A name in a project file gets one `DEPRECATED_NAME` warning per file
  from `config validate`, naming the old and new names and `cloudfall config upgrade`. A
  command or flag carries treaty's `Deprecated(since=..., replacement=..., removed_in=...)`,
  so its use warns with the replacement and the release that removes it, and `--schema` shows
  it. An MCP tool is served under both names until the removal
- **A rewrite exists** for every deprecated name a project file can hold: `cloudfall config
  upgrade` rewrites it in place, keeping comments and formatting, and a second run changes
  nothing (`sdk/src/cloudfall/upgrade.py`, which already does this for D-11). A CLI name has
  no file to rewrite; the warning names its replacement
- **The changelog says so**, under `Deprecated`, with the release that removes it, and that
  release lists the removal under `Removed`
- **Removal is enforced**: once the package version reaches a declared `removed_in`, CI fails
  until the old name is gone, so a window cannot quietly stretch

### Project format version

A project states the format it is written in, as one integer in its own `pyproject.toml`,
beside the pin it already holds:

```toml
[tool.cloudfall]
format = 1
```

- **The number.** Format 0 is every project written before this spec: no `[tool.cloudfall]`
  table, and possibly D-11's old names. Format 1 is the first format with the key, and the
  names of today. A release raises the current format by one exactly when it adds a step to
  `cloudfall config upgrade`, so the format says which rewrites a project has had. Additive
  changes never raise it
- **Each release reads a range**, `oldest` to `current`, kept in one place in the SDK. A
  release removes reading of a format under the deprecation rule above: only in a later minor
  (before 1.0) or major (from 1.0) than the one that made its successor current. The release
  that ships this spec reads 0 and 1; 0.9.0, which drops D-11's names, reads 1 only
- **`cloudfall init`** writes the current format into the `pyproject.toml` it creates
- **Checked at start** of every command that reads a project (`Fleet.acquire` and
  `resolve_root` in `sdk/src/cloudfall/app.py` and `agent_server.py`, and the project reader of
  `cloudfall-engine`), before validation, before anything is written under `tmp/` and before any
  Ansible run. `cloudfall mcp serve` checks once at start and refuses to serve. A project with
  no `pyproject.toml` reads as format 0, like one without the table
- **Inside the range** the command runs. Below `current` it adds one warning,
  `PROJECT_FORMAT_OLD`, naming the project's format, the current one and `cloudfall config
  upgrade`
- **Outside the range** the command stops with exit `PROJECT_FORMAT_UNSUPPORTED` and
  `error.context` holding `code`, `projectFormat`, `oldest` and `current`:
  - `project_format_too_new`: the project was written by a newer Cloudfall. The message names
    the fix: raise the `cloudfall==` pin in the project's `pyproject.toml` and run `uv sync`.
    Nothing else can help, since an older release cannot know what a newer format means
  - `project_format_too_old`: the message and `error.context.fix` name `cloudfall config
    upgrade`
  - `project_format_invalid`: `format` is not a non-negative integer, or `[tool.cloudfall]` is
    not a table
- **`cloudfall config upgrade` is the one way forward.** It runs the rewrite of each step from
  the project's format to `current` in order, then writes `format = <current>` into the
  `pyproject.toml` (creating the table, or the file, and keeping every other byte), then
  validates. It skips the too-old refusal, since it exists for that project, and keeps the
  too-new and invalid refusals. A release keeps every step back to format 0, so a project left
  alone for several releases upgrades in one run. Today's command already does D-11's rewrite;
  that rewrite becomes the step from 0 to 1, and the command keeps its name
- **Commands that read no project run whatever the format**: `init`, `changelog`, `--schema`,
  `--help`, and `cloudfall-engine manifest`

### The interface changelog

Recommended: **write it in the release flow**, and keep the claim.

- `cloudfall changelog` is already a published contract: the `AGENTS.md` every project gets
  from `cloudfall init` tells agents to read it, and removing it is itself a breaking change to
  a stable surface
- It is the machine-readable side of this policy: an agent upgrading a pin asks it what
  changed in the commands, flags, output schemas and exit codes between two releases, which
  `CHANGELOG.md` says only in prose
- The step is mechanical (a diff of the manifest against the last snapshot), so it belongs to
  the bot, not to a person's memory. It was skipped because it was manual
- treaty already provides it; nothing new is built

How: the release flow runs `treaty changelog-add cloudfall.app:app` after the version is
stamped (`after_stamp` in `.github/shipmill.toml`), so `schema-changelog.json` and its snapshot
go into the release commit; a release whose interface did not change adds no entry. The
Releases section of `CONTRIBUTING.md` then describes that, with no manual step. The first
release after the build seeds the snapshot, so the first entry lists the whole interface as
added.

### Open questions

Each needs the maintainer; the recommendation is what the text above assumes.

1. **The exit code of the format refusal.** (a) A new `PROJECT_FORMAT_UNSUPPORTED`, 95 on
   `cloudfall` and the same number on `cloudfall-engine`; (b) reuse `PROJECT_INVALID` (79)
   with the three context codes. Recommended: (a). An agent branches on the exit code first,
   and 79 today means "no project was found, pass `--project`", a different remedy
2. **Where the format lives.** (a) `[tool.cloudfall] format` in the project's
   `pyproject.toml`; (b) a new file such as `cloudfall.toml`; (c) a field on every resource.
   Recommended: (a). `init` already writes that file, it is committed, it holds the pin the
   format must agree with, and `[tool.*]` is where Python tools keep their settings
3. **A project without the key in the release that ships this.** (a) Read as format 0 with
   `PROJECT_FORMAT_OLD` until 0.9.0, then refused as too old; (b) refuse at once. Recommended:
   (a). Existing projects (the examples, every team's project today) keep working through the
   window D-11 already promised, as Q-3 asks
4. **A team's own Ansible inventory.** The fleet `--inventory` reads has no `pyproject.toml`.
   (a) Read the format from a variable `cloudfall_format` of the inventory's `all` group,
   absent meaning 0, with the same refusals; `config upgrade` does not rewrite a team's
   inventory, and the warning lists the edits instead; (b) leave inventories out of the
   format until a later spec. Recommended: (a). G-4 is about exactly these fleets, and today
   their only deprecated name (`cloudfall_server_types`) is a one-line edit
5. **The interface changelog.** (a) Written by the release flow, claim kept, as above; (b)
   the claim removed from `CONTRIBUTING.md`, and `cloudfall changelog` and its line in the
   generated `AGENTS.md` removed after a deprecation window. Recommended: (a). If shipmill's
   `after_stamp` does not commit files its commands change, the fallback is a step in
   `publish.yml` that writes the file before the wheel is built, with the snapshot committed
   back by the same release; the build issue confirms which one works on the first rc
6. **The window from 1.0.** (a) A deprecated name goes only in the next major; (b) one minor,
   as before 1.0. Recommended: (a). That is what semantic versioning tells a team pinning
   `cloudfall>=1.2,<2`, and the pins in `pyproject.toml` become ranges at about that time
7. **Each command's `data` shape.** (a) Stable, additive only, as the output schemas
   `--schema` prints; (b) only the envelope keys are stable. Recommended: (a). Agents read
   `data`, and treaty already checks every answer against its schema, so the contract exists
   and only its lifetime is new

## Acceptance criteria

- S-017-1: `docs/compatibility.md` exists; it names each stable surface of the table above,
  the additive and breaking lists, the patch, minor and major rules before and from 1.0, the
  deprecation window, the format version and `cloudfall config upgrade`; `README.md` and
  `CONTRIBUTING.md` link it
- S-017-2: `cloudfall init <dir>` writes `[tool.cloudfall]` with `format` equal to the current
  format into `<dir>/pyproject.toml`, and `cloudfall config validate` in that project, once it
  declares a server, exits 0 with no `PROJECT_FORMAT_*` warning
- S-017-3: A valid project whose `pyproject.toml` has no `[tool.cloudfall]` table, and one with
  no `pyproject.toml`, each pass `cloudfall config validate` with exit 0 and exactly one
  `PROJECT_FORMAT_OLD` warning naming format 0, the current format and `cloudfall config
  upgrade`
- S-017-4: With `format` one above the current format, `cloudfall config validate`,
  `cloudfall audit`, `cloudfall deploy <component>` without `--yes`, and `cloudfall-engine
  inventory render` each exit with `PROJECT_FORMAT_UNSUPPORTED`, `error.context.code`
  `project_format_too_new`, and `error.context` holding `projectFormat`, `oldest` and
  `current`; no file is created under the project's `tmp/` and no `ansible-playbook` process
  starts
- S-017-5: `cloudfall mcp serve` started on the project of S-017-4 exits with the same exit
  and code before it accepts a client
- S-017-6: With a supported range whose `oldest` is above the project's `format`,
  `cloudfall config validate` exits with `PROJECT_FORMAT_UNSUPPORTED`, `error.context.code`
  `project_format_too_old`, and `error.context.fix` `cloudfall config upgrade`
- S-017-7: `format = "1"`, `format = -1`, and `tool.cloudfall = 1` each stop
  `cloudfall config validate` with `PROJECT_FORMAT_UNSUPPORTED` and `error.context.code`
  `project_format_invalid`
- S-017-8: `cloudfall config upgrade` on a format 0 project that uses `ServerType`,
  `server-types/` and `serverType` rewrites them to `Baseline`, `baselines/` and `baseline`,
  writes `format` equal to the current format, leaves every other line of `pyproject.toml`
  byte-identical, and exits 0; the next `config validate` warns nothing; a second
  `config upgrade` answers `effect: noop` and changes no file
- S-017-9: `cloudfall config upgrade` runs on a project below `oldest` (S-017-6) and brings it
  to the current format, and refuses a too-new project (S-017-4) with
  `project_format_too_new`, changing no file
- S-017-10: In the project of S-017-4, `cloudfall changelog`, `cloudfall --schema`,
  `cloudfall-engine manifest`, and `cloudfall init <empty dir>` each
  exit 0
- S-017-11: A committed fixture set holds one record of every record kind in the table above
  as v0.7.0 wrote it; each validates against the current schema in `config/schemas/v1/`, and
  `cloudfall decisions show`, `cloudfall operator show` and `cloudfall why` read the fixture
  decision, proposal and investigation with exit 0 and no `RECORD_INVALID`
- S-017-12: A test compares the exit-code tables of both CLIs and the MCP tool names served for
  `config/examples` and for a fixture Ansible repository with committed lists, and fails when
  a name is missing or a number changed, unless the missing name was declared deprecated with
  a `removed_in` the package version has reached; an added name passes once the list is
  updated
- S-017-13: A test fails when the package version is equal to or later than the `removed_in`
  of a `Deprecated(...)` mark still declared in `sdk/src/cloudfall/app.py` or
  `engine/src/cloudfall_engine/cli.py`, or of a deprecated project name still read by
  `sdk/src/cloudfall/validation.py` or `ansible_reader.py`; every such mark and name has a
  `removed_in`
- S-017-14: A deprecated flag (`cloudfall add server h2 --address 10.0.0.2 --type
  debian-application`) exits 0, writes the same `servers/h2.yaml` as `--baseline
  debian-application` does in a copy of the same project, and answers with a warning naming
  the replacement and `0.9.0`
- S-017-15: The release flow writes the interface changelog: in the first release built after
  this spec, `sdk/src/cloudfall/schema-changelog.json` is in the release commit and the
  published wheel, and `cloudfall changelog` from that wheel lists an entry for the release's
  version; the Releases section of `CONTRIBUTING.md` names no manual changelog step

## Out of scope

- treaty as a version range instead of the exact `treaty==1.0.0rc38` pin: waits for treaty 1.0
- A CI matrix over ansible-core minors, and widening `ansible-core>=2.21,<2.22` and
  `requires-python >=3.14`: the supported range is one minor of each, so a matrix has one
  cell; revisited with treaty 1.0
- The version on `main` (`0.7.0` in `pyproject.toml` while rc tags run ahead): that is how the
  rc lane in `.github/shipmill.toml` stamps versions, a release-flow behaviour, not this
  policy
- Rewriting a team's own Ansible inventory with `config upgrade` (see open question 4)
- Records under another `apiVersion` than `cloudfall/v1`, and any tool that migrates records:
  a `cloudfall/v2` is a later spec
- The internals of the bundled engine (role variables, task names, playbook layout) beyond
  the commands `cloudfall-engine manifest` lists

## Decisions relied on

- D-11: a renamed resource keeps validating for one minor release, with a deprecation warning
  from config validate and a command that rewrites a project; this spec generalises that rule
  to every stable name and makes `cloudfall config upgrade` the rewrite for all of them
- D-12: an Elasticsearch line leaves the accepted versions on Elastic's End of Maintenance
  date, the one version removal a minor makes without a rewrite

## Issues

- cloudfall-dev/cloudfall#375: S-017-2, S-017-3, S-017-6, S-017-7, S-017-10
- cloudfall-dev/cloudfall#379: S-017-4, S-017-5
- cloudfall-dev/cloudfall#380: S-017-8, S-017-9
- cloudfall-dev/cloudfall#376: S-017-11
- cloudfall-dev/cloudfall#377: S-017-12, S-017-13, S-017-14
- cloudfall-dev/cloudfall#378: S-017-15
- cloudfall-dev/cloudfall#381: S-017-1

## Verification

Checked on main at a49a1bf plus cloudfall-dev/cloudfall#381's pull request; every test named
here passed (`uv run pytest` over the `s017` tests, 77 passed, and
`sdk/tests/test_s017_compatibility_doc.py`, 26 passed).

- S-017-1: read `docs/compatibility.md` against this spec and the code (`project_format.py`,
  `upgrade.py`, `sdk/tests/surface/`, `config/tests/records/kinds/`); it names the five stable
  surfaces, the additive and breaking lists, the release table, the deprecation window, format
  1 and `cloudfall config upgrade`, and `README.md` and `CONTRIBUTING.md` link it;
  `test_s017_1_*` in `sdk/tests/test_s017_compatibility_doc.py`, which also resolves its
  relative links, holds
- S-017-2: `test_s017_2_init_writes_the_current_format_and_validate_warns_nothing_of_it`
  (`sdk/tests/test_s017_project_format.py`) runs `cloudfall init` and `config validate`;
  `SUPPORTED_FORMATS.current` is 1 in `sdk/src/cloudfall/project_format.py`, holds
- S-017-3: `test_s017_3_a_format_0_project_validates_with_one_old_format_warning`, for a
  `pyproject.toml` without the table and for none, one `PROJECT_FORMAT_OLD`, holds
- S-017-4: `test_s017_4_a_too_new_project_stops_each_command_before_tmp_or_ansible`
  (`sdk/tests/test_s017_format_every_command.py`) runs the four commands, exit 95 and the
  context keys, no `tmp/` file and no `ansible-playbook`, holds
- S-017-5: `test_s017_5_mcp_serve_refuses_a_too_new_project_before_a_client`, holds
- S-017-6: `test_s017_6_a_project_below_oldest_is_refused_as_too_old`, `error.context.fix`
  `cloudfall config upgrade`, holds
- S-017-7: `test_s017_7_a_malformed_format_stops_validate_as_invalid` with the three inputs,
  and `test_s017_7_a_pyproject_that_is_not_utf8_stops_validate_as_invalid`, holds
- S-017-8: `test_s017_8_upgrade_rewrites_the_old_names_and_writes_the_current_format` and
  `test_s017_8_the_format_is_written_keeping_every_other_byte`
  (`sdk/tests/test_s017_upgrade.py`), second run `effect: noop`, holds
- S-017-9: `test_s017_9_upgrade_runs_on_a_project_below_oldest` and
  `test_s017_9_upgrade_refuses_a_too_new_project_changing_no_file`, holds
- S-017-10: `test_s017_10_commands_that_read_no_project_run_in_a_too_new_project` and
  `test_s017_10_init_runs_in_a_too_new_project`, each exit 0, holds
- S-017-11: `config/tests/records/kinds/` holds one record of each of the eleven kinds;
  `test_s017_11_*` in `sdk/tests/test_s017_record_kinds.py` validates each against today's
  schema and reads them through `decisions show`, `operator show` and `why`, holds
- S-017-12: `sdk/tests/surface/exit-codes.json` and `mcp-tools.json` are the committed lists;
  `test_s017_12_*` in `sdk/tests/test_compatibility_surface.py` compares both CLIs and both
  tool sets and proves the missing, renumbered, removed-in and added cases, holds
- S-017-13: `test_s017_13_no_deprecation_outlives_its_removed_in`,
  `test_s017_13_the_d11_names_fail_once_0_9_0_is_reached` and
  `test_s017_13_a_mark_without_removed_in_fails`; every mark in `app.py` and name in
  `validation.py` and `ansible_reader.py` declares `removed_in` 0.9.0, holds
- S-017-14: `test_s017_14_add_server_type_writes_what_baseline_writes_and_names_0_9_0`
  (`sdk/tests/test_baseline_rename.py`), holds
- S-017-15: the release commits of v0.8.0rc48, the first release after
  cloudfall-dev/cloudfall#384 (v0.8.0rc47's has none), and of v0.8.0rc58 add `sdk/src/cloudfall/schema-changelog.json`;
  `uvx --from cloudfall==0.8.0rc58 cloudfall changelog` from the published wheel lists an
  entry for `0.8.0-rc.58`; the Releases section of `CONTRIBUTING.md` names no manual step;
  `test_s017_15_*` in `sdk/tests/test_interface_changelog.py`, holds
