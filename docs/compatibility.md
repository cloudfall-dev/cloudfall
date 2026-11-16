# Compatibility policy

A Cloudfall project is its own repository, pinned to one release with `cloudfall==<version>`
in its `pyproject.toml`, and it outlives that release. This page says what the project, and
the agents and scripts that drive it, can rely on when the pin moves. Its source is spec
[S-017](specs/017-compatibility-policy.md).

## What is stable

These surfaces are the contract between a release and the projects that use it. Every other
module, playbook variable and file is internal and may change in any release.

| Surface | What it covers |
|---|---|
| Resource schemas | The JSON Schemas in `config/schemas/v1/` for the eleven kinds (`Server`, `Baseline`, `Application`, `Component`, `Domain`, `SshPublicKey`, `LoggingStack`, `Service`, `AlertRule`, `AlertChannel`, `OperatorPolicy`), all `apiVersion: cloudfall/v1`; their kind directories (`servers/`, `baselines/` and so on); the operation catalog in `operations/`; and, for a team's own Ansible inventory, the variables `cloudfall`, `cloudfall_defaults` and `cloudfall_baselines` |
| The CLI envelope | The JSON `cloudfall` and `cloudfall-engine` print on stdout: `ok` (true exactly when the exit code is 0), `data` with the verdict in `data.status`, `error` with its code in `error.context.code`, `meta`, and `warnings`, each with a `code`; the streaming form of `operator run` and `dashboard serve`, one envelope per event. Command paths, their flags and each command's `data` shape, as `cloudfall --schema` and `cloudfall-engine manifest` print them |
| Exit codes | Each name and number of both CLIs: `PROJECT_INVALID` 79, `CONFIG_INVALID` 80, `DRIFT` 83, `UNKNOWN` 84, `INVENTORY_UNREADABLE` 85, `RECORD_INVALID` 86, `ENGINE_STEP_FAILED` 87, `UNHEALTHY` 88, `CHECK_FAILED` 89, `NOT_VERIFIED` 90, `INCOMPLETE` 91, `MODEL_UNAVAILABLE` 92, `INVESTIGATION_INCOMPLETE` 93, `INVESTIGATION_STOPPED` 94 and `PROJECT_FORMAT_UNSUPPORTED` 95 on `cloudfall`; `ARTIFACT_BUILD_FAILED` 81 and `PLAYBOOK_FAILED` 82 on `cloudfall-engine`; treaty's shared codes (`PRECONDITION` 4, `NOT_FOUND` 5, `CONFLICT` 6 and the rest); the codes in `error.context.code`; and the warning codes, such as `DEPRECATED_NAME`, `PROJECT_FORMAT_OLD` and `ELASTICSEARCH_VERSION_ENDING` |
| Record and receipt schemas | The documents Cloudfall writes as evidence, each checked by its schema in `config/schemas/v1/`: `DeploymentReceipt`, `ReleaseReceipt`, `BackupReceipt`, `EnvironmentReceipt`, `Artifact`, `ObservedServer`, `ObservedDomain`, `OperationDecision` and `ReleaseApproval` (under `decisions/`), `OperatorProposal` and `AgentInvestigation` |
| MCP tool names | The tools `cloudfall mcp serve` lists: one per served command, its path joined with `_` (`config_validate`, `domains_apply`, `import_render-api`), the provided tools `build_artifact`, `converge_baseline`, `converge_services` and `converge_domains`, and `operation_<id>` for each declared operation; each tool's input schema follows its command's flags |

The exit codes and MCP tool names are locked by a test against committed lists
(`sdk/tests/surface/`): a release that drops a name or renumbers a code fails CI, unless the
name was deprecated with a removal release that has been reached.

### Records are read by every later release

A record is evidence, so it gets a stronger rule than a name: a record any release wrote is
read by every later release with the same `apiVersion`. Its fields are never deprecated within
`cloudfall/v1`, and `cloudfall why`, `cloudfall decisions show` and `cloudfall operator show`
read old records as they read new ones. CI holds one record of every kind as v0.7.0 wrote it
(`config/tests/records/kinds/`) and fails when today's schemas or readers stop reading one.

`cloudfall config upgrade` changes no record's evidence. Moving a project from format 0
(below) it moves receipts and operator proposals from `tmp/` into the committed `record/`
directory, byte for byte (D-16), and takes the raw release output out of a release receipt
written before 0.8.0: the text goes to a file under `tmp/output/releases/` readable by its
owner only, and the receipt keeps that file's path and SHA-256.

## What a release may change

A change is **additive** when every project, record and script that worked before still works
the same:

- A new kind, a new optional field, a new command
- A new flag whose default keeps today's behaviour
- A new key in `data`, a new warning code
- A new exit code for a new condition
- A new MCP tool

Anything else is **breaking**:

- A removed or renamed name
- A new required field
- A narrowed enum or pattern
- A changed default
- Another exit code for a condition that had one
- A key removed from `data`

| Release | Before 1.0 (`0.N.Z`) | From 1.0 (`X.Y.Z`) |
|---|---|---|
| Patch | Fixes only: behaviour moves to what the docs and schemas already say. No additive change to a stable surface, no new deprecation. A config that validated still validates, except for a security fix listed under `Security` in `CHANGELOG.md` | The same |
| Minor | Additive changes; deprecations; removal of a name deprecated in an earlier minor; a format version step (below). A breaking change without a deprecation path only when no rewrite can exist, listed in `CHANGELOG.md` with what a project must do | Additive changes and deprecations only |
| Major | None before 1.0 | Removal of deprecated names; a breaking change |

One more removal fits the minor row: a third-party version past its end of life leaves the
accepted versions in the next minor, after a warning ahead of it, such as
`ELASTICSEARCH_VERSION_ENDING` (D-12), and with a changelog entry. It is a version Cloudfall
stops installing, not a name a project uses, so it has no rewrite.

## How long a deprecated name stays

A stable name is deprecated in a minor release. From that release:

- **It keeps working**, with the same result as its replacement, in every release of that
  minor line. Before 1.0 it is removed no earlier than the next minor; from 1.0, no earlier
  than the next major
- **Every use warns.** A deprecated name in a project file gets one `DEPRECATED_NAME` warning
  per file from `cloudfall config validate`, naming the old and new names and
  `cloudfall config upgrade`. A deprecated command or flag warns with its replacement and the
  release that removes it, and `cloudfall --schema` marks it. An MCP tool is served under both
  names until the removal
- **A rewrite exists** for every deprecated name a project file can hold: `cloudfall config
  upgrade` rewrites it in place, keeping comments and formatting. A command or flag has no file
  to rewrite; its warning names the replacement
- **The changelog says so**, under `Deprecated`, with the release that removes it, and that
  release lists the removal under `Removed`
- **Removal is enforced.** Once the package version reaches a declared removal release, CI
  fails until the old name is gone, so a window cannot quietly stretch

The first names through this window are D-11's: the kind `ServerType`, the folder
`server-types/`, the field `serverType`, the inventory variable `cloudfall_server_types`,
`cloudfall add server-type` and `cloudfall add server --type`. They were deprecated in 0.8.0
for `Baseline`, `baselines/`, `baseline`, `cloudfall_baselines`, `cloudfall add baseline` and
`--baseline`, and they are removed in 0.9.0. `cloudfall config upgrade` rewrites the three
names a project file holds; a team's own Ansible inventory is not rewritten, so rename its
`cloudfall_server_types` variable by hand.

## Project format version

A project states the format it is written in, as one integer in its own `pyproject.toml`,
beside the `cloudfall==` pin:

```toml
[tool.cloudfall]
format = 1
```

- **The number.** Format 0 is every project written before formats existed: no
  `[tool.cloudfall]` table, or no `pyproject.toml` at all, and possibly D-11's old names.
  Format 1 is the current format. A release raises the current format by one exactly when it
  adds a step to `cloudfall config upgrade`; additive changes never raise it
- **Each release reads a range**, from `oldest` to `current`. This release reads formats 0 and
  1. Reading of a format is removed under the deprecation window above, so 0.9.0, which drops
  D-11's names, will read format 1 only
- **`cloudfall init`** writes the current format into the `pyproject.toml` it creates
- **Checked at start** of every command that reads a project, on both CLIs, before validation,
  before anything is written under `tmp/` and before any Ansible run. `cloudfall mcp serve`
  checks once at start and refuses to serve
- **Inside the range** the command runs. A project below `current` gets one
  `PROJECT_FORMAT_OLD` warning naming its format, the current one and
  `cloudfall config upgrade`
- **Outside the range** the command stops with exit `PROJECT_FORMAT_UNSUPPORTED` (95), and
  `error.context` holds `code`, `projectFormat`, `oldest` and `current`. The code says what to
  do:

| `error.context.code` | Means | Fix |
|---|---|---|
| `project_format_too_new` | A newer Cloudfall wrote the project | Raise the `cloudfall==` pin in the project's `pyproject.toml`, then run `uv sync`; an older release cannot know what a newer format means |
| `project_format_too_old` | The project is below `oldest` | Run `cloudfall config upgrade`, which `error.context.fix` names |
| `project_format_invalid` | `format` is missing from `[tool.cloudfall]` or is not a non-negative integer, `tool.cloudfall` is not a table, or `pyproject.toml` is not valid TOML | Fix `[tool.cloudfall]` by hand |

Commands that read no project run whatever the format: `cloudfall init`, `cloudfall
changelog`, `cloudfall --schema`, `--help`, and `cloudfall-engine manifest`.

A team's own Ansible inventory, read with `--inventory`, has no format version yet.

## `cloudfall config upgrade`

`cloudfall config upgrade` is the one way forward for a project. It runs each rewrite step from
the project's format to the current one, in order, then writes `format = <current>` into
`pyproject.toml`, creating the table or the file and keeping every other byte, then validates
the project.

```console
cloudfall config upgrade
```

- It runs on a project below `oldest`, since it exists for that project, and still refuses a
  too-new or invalid format with `PROJECT_FORMAT_UNSUPPORTED`, changing no file
- A release keeps every step back to format 0, so a project left alone for several releases
  upgrades in one run
- The step from format 0 to 1 rewrites D-11's names (`server-types/` to `baselines/`, kind
  `ServerType` to `Baseline`, `spec.serverType` to `spec.baseline`) keeping comments and
  formatting, and moves receipts and operator proposals into `record/` (D-16). It refuses,
  with `CONFLICT`, to overwrite a different file already there or to follow a symlink
- A second run changes nothing and answers `effect: noop`

Upgrading a pin, then:

1. Read the release's `CHANGELOG.md` section: `Deprecated`, `Removed` and `Breaking` name what
   a project must do
2. Raise the `cloudfall==` pin and run `uv sync`
3. Run `cloudfall config validate`; on a `PROJECT_FORMAT_OLD` or `DEPRECATED_NAME` warning,
   run `cloudfall config upgrade` and commit what it changed

## The interface changelog

`cloudfall changelog` is the machine-readable side of this policy: entries naming the
commands, flags, output schemas and exit codes of the interface. The release flow writes it: after stamping the version it runs
`treaty changelog-add cloudfall.app:app`, and `sdk/src/cloudfall/schema-changelog.json` ships
in the release commit and the wheel, with an entry for the release's version.
