# S-002: Component build steps

status: approved

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

## Verification
