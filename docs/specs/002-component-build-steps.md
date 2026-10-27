# S-002: Component build steps

status: built

## Problem

A component can't ship a frontend build. `artifact build`
(`engine/src/cloudfall_engine/artifact.py`) clones the repository at a ref and tars the tree
(or `repository.subdirectory`) with no build step, so a Vite app's `web/dist/` never reaches
the artifact. `cloudfall_deploy` (`engine/ansible/roles/cloudfall_deploy/tasks/main.yml`)
asserts `runtime.type == "python"` and `runtime.packageManager == "uv"` and runs only
`uv sync` on the server. Abecra (Cloudfall's customer zero) works around it with a side
script that builds and rsyncs the web app into its own release tree: no digest check, no
receipt, and no rollback together with the API release (cloudfall-dev/cloudfall#86).

The same issue shows the schema promising more than the engine does: `component.schema.json`
accepts `runtime.type: node | pm2` and `packageManager: npm | pnpm | yarn`, which pass
`cloudfall config validate` and then fail in the role.

S-003 (Domain routes) is the pair to this spec: it serves the build output from the
component's release.

## Behaviour

A Component may declare build steps that run on the controller before packaging:

```yaml
spec:
  buildTools:
    node: "22.0.0"
    pnpm: "9.0.0"
  build:
    - workingDirectory: web
      command: [pnpm, install, --frozen-lockfile]
    - workingDirectory: web
      command: [pnpm, build]
  artifact:
    include: [web/dist]
```

- `spec.build` is an ordered list of steps, each with `workingDirectory` (relative, no `..`,
  same rule as `repository.subdirectory`), `command` (an argv list, never a shell string) and
  an optional `timeoutSeconds` (1 to 3600, default 900)
- `spec.buildTools` maps each tool a step runs to its minimum version, like a command's
  `required_tools`. Every step's `command[0]` must be a key of `buildTools`; it is looked up
  on the controller's `PATH`
- `spec.artifact.include` lists paths (relative, no `..`) that the build produces and the
  archive must keep, even when the repository's `.gitignore` names them
- `buildTools`, `build` and `artifact` are optional. `artifact` and `buildTools` require
  `build`. A Component without `build` validates, builds and deploys exactly as today

`artifact build` with declared steps works in this order:

1. Before cloning, each `buildTools` entry is checked: the tool must be on `PATH`
   (`artifact_build_tool_missing` otherwise) and `<tool> --version` must print a version at
   least the declared one (`artifact_build_tool_unsupported` otherwise, also when no version
   can be read). Both exit `ARTIFACT_BUILD_FAILED` with the code in the error context
2. The clean checkout is made as today, at the resolved commit
3. Each step runs in order, in `<packaged root>/<workingDirectory>`, where the packaged root is
   `repository.subdirectory` when set and the checkout otherwise. A step that exits non-zero
   or outruns its timeout stops the build with `artifact_build_step_failed`, naming the step
   index, its argv and its exit code, with the tail of its stderr
4. Each `artifact.include` path must exist after the last step (`artifact_include_missing`
   otherwise)
5. The archive holds the files git tracks at the commit under the packaged root, plus every
   `artifact.include` path. Other files the build left behind (`node_modules/`, caches) are
   not packaged. Without `build`, the archive is the tree as today

Any failure in steps 1 to 4 writes no archive and no metadata file. Step output never goes to
stdout, so `artifact build` still answers one JSON envelope there; it goes to stderr.

Build steps run on the controller (the operator's machine or CI), not on the server, with the
operator's user and file access. A step runs code from the component's repository
(`package.json` scripts, install hooks). To limit what that code sees, a step gets only
`PATH`, `HOME`, `LANG`, `TMPDIR` from the controller's environment plus `CI=true`; cloud
tokens, SOPS keys and other variables are not passed. The `build_artifact` MCP tool stays
`mutating`, so an agent can only start a build where the operator's policy allows mutating
tools.

The Artifact metadata file (`config/schemas/v1/artifact.schema.json`, written by
`build_artifact`) gains an optional `spec.build` object: `steps` (each step's
`workingDirectory` and `command` as run) and `tools` (each tool's version as read in step 1).
Metadata without `build` validates as before, and a build without steps writes none.

The archive is then deployed by `cloudfall_deploy` unchanged: the digest check, the
`releases/<release>/` unpack, the `current` symlink switch, the receipt, and
`cloudfall rollback` cover the build output with the rest of the release.

`cloudfall config validate` refuses a Component whose runtime the deploy role can't run: any
`runtime.type` other than `python` or any `runtime.packageManager` other than `uv`, with
`component_runtime_unsupported` naming the field and saying only Python with uv deploys
today. The schema enums keep the other values as reserved. This refuses configs that validate
today but could never deploy. The Render importer (`sdk/src/cloudfall/importer.py`) records a
Node service as an unsupported gap and writes no Component for it, instead of writing one
that `config validate` now refuses.

Touches `engine/src/cloudfall_engine/artifact.py`, `engine/src/cloudfall_engine/cli.py`
(`artifact build`), `config/schemas/v1/component.schema.json`,
`config/schemas/v1/artifact.schema.json`, `sdk/src/cloudfall/inventory.py`,
`sdk/src/cloudfall/validation.py`, `sdk/src/cloudfall/importer.py`, `cloudfall config
validate`, and the `build_artifact` MCP tool.

## Acceptance criteria

- S-002-1: A Component with `buildTools`, a `pnpm install` and a `pnpm build` step in `web`, and `artifact.include: [web/dist]` passes `cloudfall config validate`; every existing Component without `build` still validates
- S-002-2: `artifact build --ref <sha>` for that Component produces a tarball containing `web/dist/index.html`, even though the repository's `.gitignore` lists `web/dist`
- S-002-3: The same tarball contains no `web/node_modules/` entry and contains every file `git ls-files` lists at that commit
- S-002-4: The metadata file next to the tarball has `spec.build.steps` listing both steps' `workingDirectory` and `command` in order, and `spec.build.tools` with the versions `node --version` and `pnpm --version` printed, and validates against `artifact.schema.json`
- S-002-5: When a declared build tool is not on `PATH`, `artifact build` exits `ARTIFACT_BUILD_FAILED` with code `artifact_build_tool_missing` naming the tool, and no git clone runs
- S-002-6: When a declared build tool prints a version older than its `buildTools` minimum, `artifact build` exits `ARTIFACT_BUILD_FAILED` with code `artifact_build_tool_unsupported` naming the tool and both versions
- S-002-7: A build step that exits non-zero makes `artifact build` exit `ARTIFACT_BUILD_FAILED` with code `artifact_build_step_failed` naming the step and its exit code, and the output directory holds no new `.tar.gz` or `.json` file
- S-002-8: A step that finishes but leaves an `artifact.include` path missing makes `artifact build` exit `ARTIFACT_BUILD_FAILED` with code `artifact_include_missing` and write no artifact
- S-002-9: A step that prints to stdout leaves the stdout of `artifact build` a single valid JSON envelope
- S-002-10: A build step sees `CI=true` and does not see a variable such as `HCLOUD_TOKEN` set in the controller's environment
- S-002-11: `cloudfall config validate` refuses a step whose `command[0]` is not a key of `buildTools`, a `workingDirectory` or `include` path that is absolute or contains `..`, and `artifact` or `buildTools` declared without `build`
- S-002-12: `cloudfall deploy` of a built release puts `web/dist/index.html` under `<installRoot>/releases/<release>/`, and `cloudfall rollback` to the previous release makes `<installRoot>/current/web/dist/` that release's files
- S-002-13: For a Component without `build`, `artifact build` packages the same file list as before this change and writes metadata with no `spec.build` key
- S-002-14: `cloudfall config validate` on a Component with `runtime.type: node`, `runtime.type: pm2`, or `runtime.packageManager` of `npm`, `pnpm` or `yarn` exits `CONFIG_INVALID` with code `component_runtime_unsupported` naming the field
- S-002-15: Importing a Render blueprint with a Node web service writes no Component for it and lists the service as an unsupported gap

## Out of scope

- Node runtimes on the server (`runtime.type: node | pm2`, npm, pnpm or yarn installs and a Node process under systemd): a separate feature. Until it ships, S-002-14 refuses those runtimes at validate time rather than in the role
- Building on the server: the server keeps running only `uv sync`, so build toolchains stay off production hosts
- Pinning exact tool versions or installing tools: `buildTools` checks minimums like `required_tools`; the operator or CI image provides the tools, and the metadata records the versions that ran
- Sandboxing build steps (containers, network off): the reduced environment limits what a step sees, not what it can do. Revisit with container runtimes
- Build steps outside the packaged root (a monorepo root lockfile when `repository.subdirectory` is set): `workingDirectory` stays inside the packaged root so the archive and the build see the same tree
- A separate `static` component kind with its own artifact: two artifacts for one commit would still need a joint rollback, which one artifact gives for free
- Build caching between runs: each build starts from a clean checkout

## Decisions relied on

- none

## Issues

- cloudfall-dev/cloudfall#113: S-002-1, S-002-11
- cloudfall-dev/cloudfall#114: S-002-14, S-002-15
- cloudfall-dev/cloudfall#115: S-002-2, S-002-3, S-002-4, S-002-5, S-002-6, S-002-7, S-002-8, S-002-9, S-002-10, S-002-12, S-002-13

## Verification

- S-002-1: `test_s002_1_component_with_build_steps_validates`, `test_s002_1_existing_component_without_build_is_unchanged` and `test_s002_1_build_step_timeout_accepts_its_bounds` (`sdk/tests/test_component_build_validation.py`) passed, and `cloudfall config validate --project config/examples` still exits 0
- S-002-2: `test_s002_2_the_tarball_holds_the_gitignored_build_output` runs `cloudfall-engine artifact build --ref <sha>` as a child process with stand-in `node` and `pnpm` on a temporary `PATH`; the tarball held `./web/dist/index.html` although the fixture's `.gitignore` lists `web/dist/`
- S-002-3: `test_s002_3_the_tarball_holds_every_tracked_file_and_no_node_modules` compared the tarball against `git ls-tree -r` at the commit: every tracked file, no `node_modules` entry, no `.git`. A step that deletes a tracked file would break this, so the build refuses it: `test_s002_3_a_step_that_deletes_a_tracked_file_writes_no_artifact` exited 81 with the code `artifact_tracked_file_missing` naming `web/package.json`, and wrote nothing
- S-002-4: `test_s002_4_the_metadata_records_the_steps_and_tool_versions` read `spec.build.steps` (both steps in order) and `spec.build.tools` `{"node": "22.3.0", "pnpm": "9.1.0"}` from the versions the stand-ins printed (`v22.3.0`, `9.1.0`), and validated the file against `artifact.schema.json`
- S-002-5: `test_s002_5_a_missing_tool_fails_before_any_clone` exited 81 with `artifact_build_tool_missing` and `tool: node`; the logging `git` wrapper on `PATH` recorded no `clone`, and no file was written
- S-002-6: `test_s002_6_an_old_tool_names_the_tool_and_both_versions` (`node` printing `v20.11.1` against `22.0.0`) and `test_s002_6_a_tool_that_prints_no_version_is_unsupported` exited 81 with `artifact_build_tool_unsupported`, naming the tool and both versions
- S-002-7: `test_s002_7_a_failing_step_names_itself_and_writes_nothing` exited 81 with `artifact_build_step_failed`, `step: 1`, `argv: ["pnpm", "fail"]`, `exitCode: 3` and the step's stderr tail; `test_s002_7_a_step_past_its_timeout_fails_the_build` showed a step's own 2-second `timeoutSeconds` firing as `artifact_build_step_failed`; neither left a `.tar.gz` or `.json` in the output directory. The command's limit is the clone time (1800 s) plus every declared step timeout, on the CLI and on the SDK/MCP path, whose engine call carries no 3600 s cap of its own: `test_s002_7_the_run_limit_covers_every_declared_step` (three 3600 s steps), `test_s002_7_the_sdk_leaves_the_build_limit_to_the_engine`, `test_s002_7_an_explicit_timeout_still_wins` (`--timeout 3` ends in `TIMEOUT`), `test_s002_7_without_timeout_the_computed_limit_applies`, and `test_s002_7_timeout_zero_still_means_no_limit` (`--timeout 0` applies no limit, as before). The command takes `--timeout` as a field of its own, which treaty allows in place of its framework flag, so the build can tell an omitted flag apart from 0
- S-002-8: `test_s002_8_a_missing_include_path_writes_no_artifact` exited 81 with `artifact_include_missing` naming `web/dist` and wrote nothing; `test_s002_8_an_include_through_a_symlink_out_of_the_checkout_is_refused` exited 81 with `artifact_include_outside_checkout` for an include path under a tracked symlink to a directory outside the checkout, and wrote nothing
- S-002-9: `test_s002_9_step_stdout_stays_out_of_the_json_envelope` and `test_s002_9_stdout_is_exactly_one_envelope` parsed the command's whole stdout as one JSON envelope, on success and on a failed step; the steps' stdout lines were on stderr
- S-002-10: `test_s002_10_a_step_sees_ci_and_none_of_the_controller_secrets` set `HCLOUD_TOKEN` and `SOPS_AGE_KEY` for the command; the environment the build step dumped into the archive had `CI=true`, the controller's `PATH`, `HOME` and `LANG`, and neither secret
- S-002-11: the `test_s002_11_*` tests in `sdk/tests/test_component_build_validation.py` passed (undeclared tool, absolute and `..` paths, `buildTools` or `artifact` without `build`, malformed steps)
- S-002-12: `test_s002_12_deploy_and_rollback_carry_the_built_files` builds two releases, runs the `cloudfall_deploy` role's own path, digest, unpack and switch tasks and then `rollback.yml`'s switch tasks under `ansible-playbook` against a directory on the controller (ownership dropped, no systemd); `releases/<release>/web/dist/index.html` held each build's page and `current/web/dist/` followed the deploy and the rollback. It needs GNU tar for `unarchive`, so it runs on CI's Linux runner; on macOS the same flow passed with the unpack done by `tar -xzf`. Not run against a live Debian host
- S-002-13: `test_s002_13_a_component_without_build_packages_the_clean_tree` found the archive equal to the clean checkout's tracked files and directories, no `git ls-files` run and no `spec.build` key; `test_s002_13_the_built_archive_lists_entries_like_the_plain_one` checked a build adds only its include paths
- S-002-14: `test_s002_14_reserved_runtimes_are_refused_as_config_invalid` (`sdk/tests/test_validation.py`) passed
- S-002-15: `test_s002_15_node_web_service_is_a_gap_not_a_component` (`sdk/tests/test_importer.py`) passed
