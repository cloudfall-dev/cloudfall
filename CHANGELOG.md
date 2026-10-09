# Changelog

Notable changes to Cloudfall. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/) once the project reaches 1.0.

## [Unreleased]

### Added

- A decision record has two new statuses, added to
  `operation-decision.schema.json` beside the old ones. `ran`: a `risk: read`
  operation has run by the time it is recorded, so it is recorded with its
  run's exit code (`ranExitCode`) instead of waiting as `proposed` forever.
  `superseded`: when `operations propose` records an operation, an older
  `proposed` record of the same operation, targets and inputs is closed and
  names the new decision (`supersededBy`); an approved or finished record is
  never touched. Since it reads the record first, `operations propose` now
  stops with `RECORD_INVALID` on a record that does not load, before check
  mode runs. `decisions approve` refuses a `ran` or `superseded` record with
  `PRECONDITION` before asking for the decision id. `decisions list --status S` and
  `cloudfall why --status S` (repeatable) keep only those statuses, and an
  unknown status is refused. Records already on disk keep their status. One
  disk-full incident left 47 records for `cloudfall why --host hz1`, most of
  them reads and repeats (#26)
- Every turn of `agent investigate` keeps its raw trace, in the stream's
  `turn` event (`trace`) and in the investigation record (`spec.trace`): the
  model's reasoning when it returns one (Nemotron's `reasoning_content`),
  each tool call exactly as written (name and raw arguments), the
  endpoint's response id and `x-request-id`, the milliseconds the turn took,
  and its prompt, completion and reasoning tokens. The record can now say
  what the model thought when it proposed a change
- `agent investigate` streams: one bare JSON line with `_seq` per model turn
  (its words and how many tools it called), one per tool call (the step and
  what the tool handed back, `effect: created` when it wrote a decision),
  and the saved investigation record last (`kind: investigation`), then the
  `"_summary": true` line with `effects`, or the error envelope when it
  failed. `--no-stream` returns every event in one envelope. A watcher, such
  as the hosted demo, shows the agent working instead of waiting for the end
- A decision record's approval says how it arrived, as `approval.via`:
  `terminal` when a person typed the decision id at a terminal. A record
  written before reads as `via: unknown`. `cloudfall why` tells it in the
  approval sentence. The field is optional in
  `operation-decision.schema.json` (#25)
- `agent investigate --replay DIR` plays a real run back instead of running
  Ansible: DIR is the decisions directory of a run on real hosts, and each
  check-mode call the model makes answers with the output and per-host
  results recorded for the same playbook, target and inputs. Validation,
  the decision records and the gate work as they do live, the model is
  live, and no host is touched; a call the recording does not hold fails
  and says so, and a recording that is missing or holds no decision record
  is refused before the model is asked. The investigation record names the recording under
  `replay`. An agent can be tried on a real incident again and again, for
  a demo or as a regression test. `examples/disk-full-incident/recordings`
  holds the disk-full incident recorded on a Hetzner cx23
- A mutating operation whose check failed now hands the model the end of
  its output, so the model sees why (such as `No space left on device`)
  instead of only the exit code
- `examples/disk-full-incident`: a one-host Ansible repository with an
  operations catalog, a destructive trap, and a scenario that fills the disk
  with unrotated logs so PostgreSQL goes down for a reason that is not the
  symptom; and `evaluations/nemotron-incident`, four Nemotron models given
  that alert ten times each through `agent investigate` on a real host.
  Super found the cause and the fix 10 of 10; Lightning proposed dropping
  the database 10 of 10 and Ultra tried to approve itself 2 of 10, and the
  gate kept every one of those a proposal
- `agent investigate` no longer lists a decision whose check failed under
  `unrecorded`: it was recorded, only not proposed
- `cloudfall agent investigate --alert TEXT --base-url URL --model NAME`
  lets a model work an alert through the operations catalog, over any
  OpenAI-compatible endpoint (the API key from `--api-key-from-env`,
  `--api-key-from-file` or `CLOUDFALL_API_KEY`). The model sees the same
  operation tools `mcp serve --repository` gives any client: a read
  operation runs and returns what the hosts reported, any other operation is
  proposed in check mode and recorded as a decision, and no tool approves
  anything. A call repeated with the same arguments is answered from the
  first result instead of running again. The investigation is recorded in
  `investigations/` as an `AgentInvestigation`: the alert, the model, the
  tokens, every call and how it ended, and the finding, with any decision id
  the model named but never recorded kept apart under `unrecorded`. Exit 92
  `MODEL_UNAVAILABLE` when the endpoint refuses, exit 93
  `INVESTIGATION_INCOMPLETE` with the record when the model runs out of
  `--max-turns` or does not answer in the asked-for shape. Proven on a
  disposable host with Nemotron 3 Super on Nebius Token Factory: a full disk
  behind a PostgreSQL alert was traced to unrotated application logs in five
  turns, and the two proposals it recorded, once approved, brought the
  database back
- `agent investigate` records the investigation when the model endpoint
  fails partway (refused, unreachable, not JSON, or a malformed answer),
  so the decisions earlier turns proposed are never left without one: the
  record ends `model-unavailable` with the steps, turns and tokens so far,
  an empty `answer` and no `finding`, the command still exits 92
  `MODEL_UNAVAILABLE`, and `error.context.investigation` names the record.
  A failure on the first request records an investigation with no steps;
  a refusal before any request (exit 2) still records nothing (#29)
- An operation tool of `mcp serve --repository` describes a read operation
  as running and returning what the hosts reported, instead of saying it
  runs check mode only
- `operations propose` returns a read operation's play output in
  `data.output`, so an agent acts on what the operation reported without
  opening the decision's `.diff` file; the record keeps the file as before.
  The output is the hosts' text, so `data` carries `_trusted: false`.
  A proposed change answers `output: null`, and the read operation's MCP
  tool no longer suggests approving a run that needs no approval (#24)
- `cloudfall why` answers "why did the agent do that" from the decision
  records alone, for a host (`--host`), an operation (`--operation`) or a
  time window (`--since`, `--until`, ISO 8601 or a bare date), as JSON or
  as one HTML page (`--format html`). Each decision is told as what it was
  based on, what was proposed, what check mode showed, what gated it, who
  approved, what the run and the verify step did and how it ended, every
  sentence drawn from a field of the record. A decision is about a host
  when the record names it, as the target or in the per-host evidence of
  a stage; a group pattern is not expanded, because that would need an
  inventory the record does not depend on. The same question is the
  read-only `why` tool on `cloudfall-mcp --repository`
- Every JSON document `cloudfall` prints, results and errors alike, carries
  `meta.schema_version` (`MAJOR.MINOR` of the output contract, now `1.0`)
  and `meta.tool_version` (the installed package version), so an agent can
  tell when the output shape may have changed without a separate call.
  `meta.request_id` names the invocation with a UUID, the same on every
  document it writes, for quoting in a retry or a bug report, and
  `meta.duration_ms` counts the milliseconds from the start of the command
  to that document, not counting Python's own startup.
  MINOR moves when a command gains a key, MAJOR when one is removed,
  renamed or changes meaning. `cloudfall --version` prints the same
  version as JSON and exits 0
- Every JSON document also carries a `warnings` list. A key is removed only
  after a release in which documents holding it warn `FIELD_DEPRECATED`
  with the replacement key and the schema version that drops it
- `cloudfall changelog [--since MAJOR.MINOR]` lists changes to the output
  contract, newest first: version, date, `breaking`, and the added,
  removed and changed keys
- `cloudfall --schema-version MAJOR <command>` pins the output contract: it
  fails with `invalid_argument` before the command runs when this build no
  longer writes that MAJOR, so a pinned caller stops at a breaking release
  instead of misreading it. `--version` and `changelog` report the current
  and minimum supported versions
- `cloudfall --schema` (alias `--print-schema`) prints every command in one
  JSON document: its flags, read from the parser; its effect and gate, from
  the command catalog; and an `output_schema` per command, a JSON Schema of
  each stdout shape with the condition that selects it. Every key, at the
  top level and under `data`, carries `x-stability`: `stable` keys change
  only in a MAJOR release after
  a deprecation warning, `experimental` keys (unreleased commands such as
  `why` and `changelog`) may change in any release. What each key holds is
  not declared yet. The tests validate real output against these schemas,
  and an `etag` changes only when a command, flag or declared output does

- Every error carries `exit_code`, the code the process exits with, so a
  caller that reads the JSON needs no second channel: `{"code",
  "message", "exit_code"}`. Results that exit non-zero, such as an
  `audit` that finds drift, say so with `ok: false` and keep `error`
  `null`
- Every `cloudfall` `--help` ends with the exit codes and what each one
  means: 0 positive, 1 ran with a negative result, 2 nothing ran, 3
  unknown. The generated `AGENTS.md` renders the same table from the same
  source, so the two cannot drift
- `--quiet` writes nothing to stderr: no error document, no help text, and
  no request lines from `dashboard serve`. The exit code still says what
  happened, and results still go to stdout
- `--warnings-as-errors` fails a result that carries a warning: the
  document says `ok: false` with the error `warnings_as_errors`, its
  `status` and `data` unchanged, and the command exits 1 instead of 0

### Changed

- The raw trace of `agent investigate` keeps at most 8000 characters of a
  turn's reasoning and of its text, like the host output a tool result hands
  the model: a longer one keeps its end after a `[first N characters cut]`
  mark, in the investigation record (`spec.trace`) and in the stream's `turn`
  event alike. A model's reasoning often quotes the host output it just
  read, and the record no longer keeps it unbounded.
  `agent-investigation.schema.json` bounds both fields with `maxLength`, and
  says that a step's parsed arguments are what ran while
  `toolCalls[].arguments` is only the model's text (#50)
- treaty 1.0.0rc38 instead of 1.0.0rc29. `dashboard serve` and
  `operator run` write each event in `json` and `jsonl` as a bare JSON line
  with `_seq` instead of an envelope with `data` and `meta.seq`, and end
  with one `"_summary": true` line or the error envelope. `mcp serve`
  stopped by `SIGINT` or `SIGTERM` exits 130 or 143 with the `CANCELLED`
  envelope instead of 0, and writes no envelope when it stops cleanly (#25)
- The package is classified as Beta on PyPI instead of Alpha
- The site's landing page is redesigned for technical founders leaving the
  cloud and for teams running servers for clients: a light, illustrated
  look, the hero's call to action rotating between moving off Render and
  moving off Neon, a section for MSPs on gated operations and the record
  as the page sent to a client, a section on how the gate works with a
  real decision record, and a free pilot by email; the manifesto page and
  the link preview image follow the same look, and the manifesto's release
  policy is stated as "proven live or labeled unproven" instead of "pre-1.0"
- The README, the site, the roadmap and the architecture say what is built
  on a team's own Ansible repository (the operations catalog, the gate,
  decision records and `cloudfall why`) instead of calling it unbuilt, show
  a real `cloudfall why` answer, and address both teams leaving a PaaS and
  teams already running Ansible
- The README opens with what Cloudfall is, a diagram of the gate, a short
  quickstart and a guide index; the long walkthroughs moved to
  [docs/getting-started.md](docs/getting-started.md). The repository has issue
  forms (bug, feature, pilot request), a pull request template, a support
  page and a code of conduct, and the PyPI summary matches the project's description
- The repository moved to the `cloudfall-dev` organization:
  github.com/cloudfall-dev/cloudfall. The package metadata, the README,
  the site and `REPOSITORY_URL` point there; the old
  github.com/romamo/cloudfall address redirects
- Releases are cut by [shipmill](https://github.com/shipmill/shipmill) from
  this CHANGELOG: an rc (`0.6.0rc1`, ...) after each batch of merges, and a
  stable release promoted from an rc that soaked 3 days once its milestone
  closes. The source distribution leaves out the repository's tooling,
  evaluations and website
- **Breaking:** the CLI follows one name per meaning, and its output is
  typed:
  - Decision records have their own group: `decisions list` (was
    `operations decisions`), `decisions approve` (was `operations
    approve`), and a new `decisions show`. `operations` keeps the catalog:
    `list`, `show`, `propose`
  - `data migrate` is `data copy`, no longer read as `migrate`, and
    `services inspect` is `services observe`, as `observe` collects server
    evidence. Each old path answers 13 `REDIRECTED` with the new command in
    `error.redirect.command`
  - `migrate --release` and `--env-file` are `--component-release` and
    `--component-env-file`: they take `COMPONENT=VALUE` pairs, where
    `deploy`'s take one value
  - `--observed` defaults to `tmp/observed` on every command, where
    `observe` writes: `audit`, `services status` and `dashboard` required
    it, and the operator's drift checks used `tmp/operator/observed`
  - Field names in `data` are snake_case at every depth, as treaty's own
    envelope keys are (`openProposals` is `open_proposals`, `wouldRun` is
    `would_run`). Map keys that are data keep their spelling (resource
    kinds, environment variable names, ids), and a record or resource in
    `data`, anything with an `apiVersion`, keeps its stored form
  - Every command's output schema gives each `data` key its JSON type, and
    an optional list or object is written empty rather than `null`
    (`inventory show` writes `ansible` as `{}` for a project)
- **Breaking:** argparse is gone: the last three `cloudfall` commands
  move to treaty, and so do root `--help`, `--version` and `--schema`.
  - `operator run` streams one envelope line per pass as it ends, with
    `data.pass` (`alerts`, `drift`, `autonomy`) and `data.effect`:
    `created` for a pass that wrote proposals, `updated` for one that ran
    licensed proposals, `noop` otherwise. Stopping it ends the stream with a
    `CANCELLED` line and exit 130 (SIGINT) or 143 (SIGTERM), so a systemd
    unit needs `SuccessExitStatus=143` (the
    [operator guide](docs/operator-guide.md) has it). A gateway it cannot
    use (none declared, or a missing or unreadable certificate) is 4
    `PRECONDITION`, an unreachable one 12 `UNAVAILABLE`. `--interval` and `--drift-interval` must be above 0. The
    operating contract now lists it with the commands that change servers,
    since a declared `OperatorPolicy` lets it run proposals
  - `dashboard serve` streams one line once the server listens, then serves
    until stopped, ending like `operator run`. Its request lines are info
    log lines on stderr, shown at a terminal or with `--verbose`
  - `changelog` is treaty's: it lists interface changes per release from
    the packaged `schema-changelog.json`, which `treaty changelog-add
    cloudfall.app:app` writes, and `--since` takes a release version
  - `operator approve` reports such a gateway as 4 `PRECONDITION` (was 86
    `RECORD_INVALID`) and an unreachable one as 12 `UNAVAILABLE`
  - The argparse-era output contract added earlier in this release is
    gone: the six-key document with a top-level `status`, `--output json`,
    `--schema-version MAJOR` against that contract, the parser-built
    `--schema`, the `--help` exit-code table and exit codes 0 to 3. Every
    command answers treaty's envelope, and `cloudfall --schema` lists every
    command's flags, output schema and exit codes. The `AGENTS.md` that
    `cloudfall init` writes describes this contract
- **Breaking:** `cloudfall-mcp` is gone; the agent server is `cloudfall mcp
  serve` (treaty 1.0.0rc29, from issues #239, #240, #281 and #285 filed for
  it). Its tools are Cloudfall's own commands, answering with the CLI's
  envelope, and tool names follow the commands (`list_operations` is
  `operations_list`, `validate_config` is `config_validate`). MCP client
  configs change from `cloudfall-mcp …` to `cloudfall mcp serve …`
  - The server serves one fleet, chosen as before: `--project`, or
    `--repository` for a team's Ansible repository. The fleet, the schema
    and engine directories, and every directory a tool writes (snapshots,
    receipts, artifacts, proposals, import output, the migrate plan) are
    fixed for the run: they leave the tool schemas, and a call that passes
    one is refused, so no call reaches another fleet or engine or writes
    outside the project. So are the Render API URL `import_render-api`
    sends its key file to and the env file `secrets_render` writes. Files
    a tool reads as input (a blueprint, a key file, an env file) stay
    arguments
  - On a project, `deploy`, `rollback`, `restart`, `migrate` and
    `data_migrate` return their plan until called with `yes: true`, as on
    the CLI; the old `confirm=true` handshake is gone. `build_artifact` and
    the `converge_baseline`, `converge_services` and `converge_domains`
    tools stay, the converges destructive and run only with
    `confirm_destructive: true`. `backup_run` and `backup_verify` run in
    one call, as their commands do (they were confirm-gated in
    `cloudfall-mcp`), and `operator_watch` and `operator_approve` are gone:
    the watch loop runs as a service, and a person approves a proposal from
    a shell
  - On a repository, the tools are the read-only fleet commands and one
    `operation_*` tool per declared operation, which runs check mode,
    records a proposal and answers with the approval command in
    `data.next`; a destructive operation's tool asks for
    `confirm_destructive` even to preview
  - Each directory has one flag name across the commands that read and
    write it, so a server can fix it once and a non-default one reaches
    them all: `observe --output-dir` is `--observed` (as `audit` reads it),
    `services inspect --output-dir` is `--service-observed` (as `services
    status` reads it), and `--receipts` is `--releases` on `deploy` and
    `migrate`, `--backups` on `backup run|verify`, `--data-migrations` on
    `data migrate` and `--env-receipts` on `secrets render` (as `audit`
    reads it). `mcp serve` takes `--releases`, `--backups` and
    `--data-migrations` too
  - `init`, `operations approve`, `operator run` and `dashboard serve` are
    never MCP tools (`mcp=False`), on this server or `treaty-mcp`
  - A fleet that cannot be read stops the server before a client connects,
    with the envelope on stderr: 79 `PROJECT_INVALID`, 80 `CONFIG_INVALID`,
    85 `INVENTORY_UNREADABLE`. `--list-tools` prints the tool list
  - `dashboard serve` reports a port it cannot bind as 4 `PRECONDITION`
    with `dashboard_listen_failed`; an unreadable evidence file is no
    longer reported as a listen failure
- **Breaking:** `cloudfall-engine` runs on [treaty](https://github.com/romamo/treaty).
  Every command answers one JSON envelope on stdout (`ok`, `data`, `error`,
  `warnings`, `meta`), errors included, which used to go to stderr in their
  own shape; `inventory render` puts the inventory under `data.inventory`
  (the `--output` file is unchanged). Failures exit with their own codes:
  79 `PROJECT_INVALID`, 80 `CONFIG_INVALID`, 81 `ARTIFACT_BUILD_FAILED`,
  82 `PLAYBOOK_FAILED`; the old snake_case code is in `error.context.code`.
  A missing playbook, inventory, or role directory exits 2 before Ansible
  starts. `playbook run --check` is now `--dry-run`; Ansible's play log
  streams to stderr. `cloudfall-engine manifest` describes every command
- **Breaking:** the last eight commands that change state move to treaty:
  `observe`, `secrets render`, `import render`, `import render-api`,
  `deploy`, `rollback`, `restart` and `data migrate` (treaty #183 lets
  their camelCase keys through). Each keeps its `data` keys, `status`
  included; the ones that write add `effect`. `deploy`, `rollback`,
  `restart` and `data migrate` still change nothing without `--yes`: that
  run is treaty's dry run (`effect: would_update`, `meta.dry_run: true`,
  treaty #197). The plan writes the result keys as `null`, and the result
  writes the plan keys (`wouldRun`, `instruction`) as `null`, since treaty
  answers one object per command. `observe` is read-only (it declares the
  snapshots it writes under the project as output, kept by `cleanup`) and exits 91 `INCOMPLETE` (was 1)
  with the run in `data` when a server gave no snapshot. Engine steps,
  sops and the Render API go through treaty: `doctor` checks sops, and
  `import render-api` honours `--proxy` and marks its answer as content
  from outside. Failures that were exit 2 are 4 `PRECONDITION` with the
  old code in `error.context.code`. A key a command writes only sometimes
  is `null` when absent, so `inventory show` writes `data.ansible` as
  `null` for a project
- **Breaking:** `why` runs on treaty, `--format html` included (treaty
  #179): the page is drawn from the answer's JSON document, as before. Its
  verdict moves to `data.status`; a bad `--since` or `--until` is 2
  `ARG_ERROR` with `why_instant_invalid` in `error.context.code`, and a
  record that cannot be read 86 `RECORD_INVALID`. `--format html` is a
  `why` format only; other commands refuse it
- **Breaking:** seven commands that run Ansible move to treaty: `health`,
  `backup run`, `backup verify`, `operations propose`, `operations approve`,
  `operator approve` and `migrate`. Ansible runs through treaty in the
  project directory, so `--timeout` and Ctrl-C stop it, and `doctor` checks
  ansible-playbook. `operations approve` and `migrate` still change nothing
  without `--yes`; that preview is treaty's dry run, `effect: would_update`
  and `meta.dry_run: true`, with `data.status` `pending` or `plan`. New exit
  codes, each with the result
  in `data`: 87 `ENGINE_STEP_FAILED` (an inventory render or playbook
  failed, was 1), 88 `UNHEALTHY` (was 1), 89 `CHECK_FAILED` when check mode
  fails in `propose` (was 1), 90 `NOT_VERIFIED` when an approved run fails
  or does not verify (was 1), 3 `PARTIAL_FAILURE` for a paused `migrate`.
  A failed `migrate` step's own code and message move from the top-level
  `error` to `error.context`. `deploy`, `rollback`, `restart` and
  `data migrate` stay on argparse until treaty #183, since their plan
  answer has a `wouldRun` key
- **Breaking:** six more `cloudfall` commands run on treaty: `init`,
  `add ssh-key`, `add server-type`, `add server`, `dashboard build` and
  `services inspect`. Their answer is the treaty envelope described below,
  with the same `data` keys, plus `effect: created` on `init` and `add`,
  which treaty requires of a command that changes the project.
  `dashboard build` and `services inspect` stay read-only and declare the
  reports they write under `tmp/` as their output, which treaty's `cleanup`
  never removes. Failures: 6 `CONFLICT` for a resource that already
  exists (was 2), 7 `PERMISSION_DENIED` for a read-only project or path
  (was 1 or 2), 4 `PRECONDITION` for a non-empty `init` directory (was 2),
  5 `NOT_FOUND` for a missing key file, 2 `ARG_ERROR` for a bad id, name or
  address; the old code is in `error.context.code`. `init` runs git through
  treaty, so `cloudfall doctor` checks for git 2.24. `observe`,
  `secrets render` and the two `import` commands stay on argparse until
  treaty can carry their camelCase keys on a command that writes (#183)
- **Breaking:** nine read-only `cloudfall` commands run on treaty, the
  first step of moving the CLI off argparse: `config validate`,
  `inventory show`, `operations list`, `operations show`,
  `operations decisions`, `operator list`, `operator show`, `audit` and
  `services status`. `cloudfall manifest` lists them; every other command,
  root `--help` and `--version` stay on argparse for now. Their answer is a
  treaty envelope (`ok`, `data`, `error`, `meta`, `warnings`): the verdict
  moves from the top-level `status` to `data.status`, and the other `data`
  keys are unchanged. `inventory show` writes `data.ansible` as `null` for a
  project. Failures are an envelope on stdout with their own exit codes:
  79 `PROJECT_INVALID`, 80 `CONFIG_INVALID` (resources or evidence files),
  85 `INVENTORY_UNREADABLE`, 86 `RECORD_INVALID`, 5 `NOT_FOUND` for an
  undeclared operation or a missing proposal, 2 `ARG_ERROR` for bad input;
  the old snake_case code is in `error.context.code`. `audit` exits 83
  `DRIFT` (was 1) or 84 `UNKNOWN` (was 3) with the report in `data`. Pick
  the format with `--format json`: `--output` after one of these commands
  is refused, since treaty's `--output` names a file. Relative paths still
  resolve against the project, without changing the working directory
- `cloudfall-engine` runs Ansible and git through treaty's `ctx.run`
  (treaty 1.0.0rc18), so `--timeout` and Ctrl-C stop the whole process
  group, and secrets are redacted from the play log. The play log streams
  to stderr as plain text, as before, terminal or not; `--quiet` silences
  it. A failed playbook carries the last 4096 characters of the log in
  `error.context.output`, marked as untrusted content from the hosts.
  `playbook run` reports `meta.dry_run: true` under `--dry-run`. A git step
  that runs past its 600-second limit now exits 10 `TIMEOUT`, not 81 with
  `artifact_git_timeout`. `cloudfall-engine doctor` checks for
  ansible-playbook 2.21 and git 2.24
- Every JSON document has the same six top-level keys: `ok` (true exactly
  when the exit code is 0), `status` (the command's verdict, such as `ok`,
  `plan`, `drift` or `unhealthy`), `data` (the command's payload, which
  used to sit at the top level beside `status`), `error` (`null` unless
  the command failed), and `meta` and `warnings`. A failure on stderr is
  `{"ok": false, "status": "error", "data": null, "error": {"code",
  "message"}}`. An agent reads `ok` and `data` without
  knowing which command it ran. Anything that read a payload key from the
  top level, such as `.inventory`, now reads it under `data`
- `--help` prints to stderr when stdout is not a terminal, so a caller
  capturing stdout never finds usage text where it expects JSON. At a
  terminal it stays on stdout
- `--output json` is accepted before the command and after every command.
  JSON is the only format, so the flag changes nothing; a caller that asks
  for JSON the usual way no longer gets a usage error. `--output` with any
  other value is `invalid_argument`
- The six options that named an output path are renamed so `--output`
  means one thing everywhere: `--output-dir` on `observe`,
  `services inspect`, `dashboard build`, `import render` and
  `import render-api`, and `--output-file` on `secrets render`. Scripts
  passing `--output DIR` to these commands must switch
- `operator` errors use the same envelope as every other command,
  `{"status": "error", "error": {"code", "message"}}`, instead of a bare
  `{"code", "message"}`, so one parser reads every failure
- `operator show` and `operator approve` print the receipt under
  `data.proposal` with a top-level `status`, instead of the bare receipt.
  `approve` reports `status: failed` when the verify step did not confirm
  the fix, as the `cloudfall-mcp` tool already did

### Fixed

- `agent investigate` keeps its investigation record when the reader closes
  stdout mid-run (such as `| head -2`). Since the command streams, such a
  run left the decisions it proposed without the investigation that made
  them, and exited 0. The record is now saved ended `stopped`, a new status
  in `agent-investigation.schema.json`, with the steps, turns and tokens so
  far, an empty `answer` and no `finding`; nothing more is written to the
  closed stdout, and the command exits 94 `INVESTIGATION_STOPPED`. A stream
  stopped by a signal or a timeout is recorded `stopped` too and keeps its
  own exit code; `--no-stream` is unchanged (#44)
- The operator proposal schema refuses `approval.via` on an autonomous
  (policy) approval. Such a receipt used to pass validation and lose the
  field on load; it now fails schema validation like any other invalid
  receipt. Cloudfall never wrote one, so only a hand-edited receipt is
  affected (#46)
- A decision record that is not JSON or does not match its schema answers
  `RECORD_INVALID` (exit 86, code `decision_record_invalid`) naming the file
  and the failing field, from `decisions show`, `decisions list`,
  `decisions approve` and `why`; it used to crash them with exit 1 and no
  envelope. `decisions list` and `why` still stop at the first bad record
  rather than skip it (#41)
- `migrate --env-file NAME=PATH` and `--data NAME=PATH` resolve a relative
  path against the project again, as every other path does; since the
  move to treaty they resolved against the current directory
- `migrate --build` and the `cloudfall-mcp` `build_artifact` tool read the
  release from the engine's treaty envelope, under `data`. Since the engine
  moved to treaty every build step failed with "artifact builder returned no
  release id". `build_artifact` now returns the engine's `data` keys
  (`git_ref`, `archive_sha256`, ...) beside `status`
- The time baseline accepts a host whose clock another daemon keeps
  (ntp, ntpsec, chrony). `timedatectl set-ntp` drives systemd-timesyncd
  only, so on such hosts check mode reported "Would enable network time
  synchronization" and a deploy failed with "NTP not supported". The role
  now switches NTP on only where systemd-timesyncd is installed, and
  otherwise requires the clock to be synchronized; check mode warns when
  nothing keeps time, and a deploy fails with a message naming the daemon
  to check when the clock does not synchronize within 60 seconds
- `cloudfall add` into a read-only or full project failed with a Python
  traceback and could leave a new server type behind when only the
  server file failed. It now removes what it wrote and fails with
  `project_write_failed` (exit 2), naming the file and the reason. Any
  other command that cannot write a path because it is read-only or not
  the caller's reports `path_not_writable` as JSON (exit 1, since part of
  the work may be done) instead of a traceback
- `cloudfall observe` no longer prints Ansible's play log on stdout ahead
  of its JSON document, which broke any caller parsing stdout as JSON.
  The playbook's output is captured, and when the run fails its last 2000
  characters, ending with the play recap, are kept in `data.detail`. The
  same capture keeps the `observe` tool of `cloudfall-mcp` from writing
  Ansible text into the protocol stream
- `cloudfall-mcp` `operator_watch` and `operator_approve` wrapped engine
  failures in the error envelope twice (`error.error.code`); they now
  return one envelope like every other tool
- A failed `operator approve` is recorded even when the engine's log is
  long. The receipt keeps the error code and the end of the log, where the
  failing task and the play recap are, within the 1000-character cap.
  Before, a run against unreachable hosts crashed while writing the
  `failed` receipt, left the proposal `proposed` with no trace that it ran,
  and showed the caller a traceback

### Security

- `cloudfall decisions approve <id>` asks the person to type the decision id
  at a terminal before it runs anything, and no flag answers that, `--yes`
  included. Off a terminal, as from an agent's shell or a script, it exits 4
  with `PERSON_REQUIRED` and runs nothing; a mistyped id exits 4 with
  `ATTESTATION_MISMATCH`. Before, `--yes` ran it headless and `--approver`
  fell back to `$USER`, so an agent could approve its own proposal in the
  person's name. Without `--yes` the command no longer previews the
  proposal: `decisions show` does. Scripted or CI approvals stop working.
  This is a speed bump, not a boundary: a process running as the same OS
  user can fake a terminal (#25)
- `operator approve` is no longer an MCP tool. On a project,
  `cloudfall mcp serve` served it as `operator_approve`, so an agent could
  run and verify its own proposal with no person in the loop. A person now
  approves from a shell with `cloudfall operator approve`, as with
  `decisions approve`; `operator_list` and `operator_show` stay tools. The
  `--gateway-url`, `--gateway-ca`, `--gateway-cert` and `--gateway-key`
  flags of `cloudfall mcp serve`, which only that tool used, are removed
  (#35)
- `cloudfall operator approve <id>` asks the person to type the proposal id
  at a terminal before it runs the playbook or the verify step, and no flag
  answers that, `--yes` or `"yes": true` in `--raw-payload` included. Off a
  terminal it exits 4 with `PERSON_REQUIRED`; a mistyped id exits 4 with
  `ATTESTATION_MISMATCH`; either way the proposal stays `proposed`. Before,
  any shell call ran it, so an agent could approve its own proposal.
  `operator show` previews it. The receipt's approval records
  `via: terminal`; a person's approval written before reads as
  `via: unknown`, and a policy's has none. The field is optional in
  `operator-proposal.schema.json`. Scripted approvals stop working; a
  declared `OperatorPolicy` still licenses autonomous runs (#35)

## [0.5.1] - 2026-09-22

### Changed

- Decision records default to `decisions/` beside the operations rather
  than to `tmp/decisions`: the record is what a team keeps, and `tmp/` is
  what they throw away. The diffs and logs beside each record are raw
  Ansible output, so a repository that commits them wants `no_log` on the
  tasks that handle secrets

### Fixed

- A decision is `verified` only when its verify run changed nothing. A
  verify step that exits zero while changing a host found the fleet not as
  the run left it and converged it further, which verifies nothing, so it
  is recorded `failed`. Every approved decision now carries a `verdict`
  saying why it ended where it did
- A record cites its artifacts and its observation basis as the repository
  sees them rather than by absolute path, because a record is committed
  and one machine's home directory means nothing in anyone else's checkout

## [0.5.0] - 2026-09-21

## [0.4.0] - 2026-09-21

### Added

- Brownfield fleet reader: `cloudfall audit` and `cloudfall inventory show`
  read the fleet from the team's own Ansible inventory, with `--inventory`
  or from the inventory an `ansible.cfg` in the current directory names.
  Hosts, groups and connection settings come from Ansible; a `cloudfall`
  block per host, `cloudfall_defaults` per group and the
  `cloudfall_server_types` catalog carry what Ansible does not model. The
  documents are validated against the same schemas a project directory
  gets, so `PlatformInventory` and everything built on it is unchanged
- `cloudfall.ansible_api` is the only module that imports ansible-core. It
  reads in process and falls back to the `ansible-inventory` command,
  reporting which served the read
- `cloudfall observe` collects one read-only snapshot per server through
  the team's inventory plus an ephemeral overlay holding only the
  `cloudfall_servers` group and the two variables the inspect role cannot
  derive, so the audit loop closes without a Cloudfall project. Their own
  `ansible.cfg` keeps deciding how Ansible connects; only the roles path
  is forced. `--limit` takes an Ansible host pattern, which Ansible
  resolves, and the run is judged against the hosts it asked for
- `StateValidator.validate_documents` validates resource documents
  assembled in memory, with the schemas, reference checks and error codes
  documents on disk get
- The operations catalog: every playbook the team runs is declared in
  `operations/` with a risk level, a target scope, typed inputs,
  preconditions and a verify step, and `cloudfall operations list|show`
  reads it. A playbook nobody declared is not an operation
- The gate and the record: `cloudfall operations propose` runs an
  operation in check mode and records the operation, targets, inputs, the
  evidence it was based on and the diff by sha256;
  `cloudfall operations approve --yes` records the approver, runs it, runs
  the verify playbook and closes the record as executed, verified or
  failed; `cloudfall operations decisions` reads the trail without the
  catalog
- The agent surface: `cloudfall-mcp --repository` serves a brownfield
  repository, where the tool list is the catalog. Each declared operation
  is one tool carrying its risk as MCP annotations, calling it runs check
  mode and records a proposal, and no tool approves anything: that stays a
  command a person runs

## [0.3.0] - 2026-09-20

### Removed

- `cloudfall init --rev` and `--source`, and the machinery behind them:
  `GitRevision`, `GitSourceUrl`, `GitPin`, `IndexPin`, `CheckoutState`, the
  checkout inspection, and the `project_revision_unresolved`,
  `project_revision_uncommitted` and `project_revision_unpublished` errors.
  Cloudfall is on PyPI, so a project pins a release and resolves it like any
  other dependency; `resolve_installed_version` replaces
  `resolve_installed_pin`, and the `init` envelope reports `version` rather
  than a `pin` object. To run an unreleased Cloudfall in a project, point the
  dependency at a checkout with uv's own `[tool.uv.sources]`

### Changed

- The quickstart and the Render migration guide install Cloudfall from the
  index rather than from git

## [0.2.1] - 2026-09-20

### Fixed

- `cloudfall init` works from a PyPI install. An install from the package
  index records no origin to read a commit from, so `init` failed with
  `project_revision_unresolved` on the first command a new user runs, and
  `--rev` only worked around it by writing a project that installed
  Cloudfall from git instead of from the index. A project now pins whatever
  ran `init`: `cloudfall==<version>` with no `[tool.uv.sources]` table for a
  released install, the commit and repository for a git or source install
- The project README links the secrets guide and the reference examples at
  the release tag when the pin is a released version, rather than at a
  commit the install does not know

### Changed

- `InitOptions` and the `cloudfall init` envelope carry one `pin` instead of
  a `revision` and a `source`: `{"kind": "index", "version": ...}` or
  `{"kind": "git", "revision": ..., "source": ...}`. `resolve_installed_pin`
  replaces `resolve_installed_revision`
- `--rev` documents that it pins a commit from `--source`, which is now the
  explicit alternative to the default rather than the only mechanism

## [0.2.0] - 2026-09-20

### Added

- `cloudfall-mcp` exposes `add_ssh_key`, `add_server_type`, and `add_server`
  so an agent can declare the fleet without writing YAML by hand; each
  returns the `cloudfall add` envelope, writes into the project only, and
  re-validates it. `cloudfall init` stays CLI-only because the server runs
  inside an existing project (#3)
- `cloudfall init` lays out the secrets setup (`secrets/` and a `.sops.yaml`
  template), runs `git init` unless the directory already lies inside a
  repository, links the secrets guide and the reference examples at the
  pinned commit, and takes `--description` for an About section in the
  README and the `description` in `pyproject.toml`
- `cloudfall init` writes `AGENTS.md`, the operating contract for AI agents
  in the project: canonical invocation, every command sorted by effect with
  the gate each server-changing command demands, the output contract and
  exit codes, what may be read under `tmp/`, the secrets rule, and git
  discipline; plus a one-line `CLAUDE.md` pointing at it. The
  classification comes from the new `cloudfall.commands` catalog, which
  the tests check against both argparse trees (#2)
- `cloudfall init` from a source checkout refuses to pin `HEAD` when tracked
  files are modified (`project_revision_uncommitted`) or when `HEAD` is on
  no remote branch (`project_revision_unpublished`), so a project never pins
  a commit that is not the running code or that `uv sync` cannot fetch
- Fleet repositories consume Cloudfall as a package: the wheel bundles the
  v1 schema catalog and the Ansible engine, and every `--schemas` and
  `--engine` default resolves to the bundled copies (or to the source tree
  when run from a checkout), so no submodule or sibling checkout is needed
- `cloudfall-engine playbook run` executes a bundled playbook by name or a
  fleet playbook by path under the engine's Ansible configuration, with
  `--roles` directories searched before the bundled roles and `--check`,
  `--diff`, `--syntax-check`, `--limit`, `--tags`, and `--extra-vars`
  passed through; `cloudfall-engine playbook list` names the bundled ones
- Timer-driven restore drill: an optional `backup.restoreCheckOnCalendar`
  schedule installs an audited restore-check service and timer for
  PostgreSQL and Redis, so backup restorability is verified continuously
  (M10; proven live on 2026-09-11 together with alert delivery to an
  external destination)

### Changed

- `ansible-core` is a runtime dependency rather than a development one, so
  an installed `cloudfall` package can run its engine
- **Breaking: common-vocabulary rename across schemas, CLI, and layout.**
  Resource kinds `Project` → `Application` and `HostProfile` → `ServerType`
  (schema files renamed to match); the `Server` spec field `profile` →
  `serverType`; the `Component` spec field `project` → `application`; the
  top-level `state/` directory → `config/` with resource directories
  `projects/` → `applications/` and `host-profiles/` → `server-types/`;
  `cloudfall state validate` → `cloudfall config validate`; importer flag
  `--project` → `--application`; Python API `validate_state` →
  `validate_config`, `ValidatedState` → `ValidatedConfig`,
  `StateValidationError` → `ConfigValidationError`; Taskfile variable
  `STATE_DIR` → `CONFIG_DIR`; inventory payload key `hostProfiles` →
  `serverTypes` and observed-server key `profile` → `serverType`. Existing
  config directories must be migrated by renaming the directories and the
  `kind`/`profile`/`project` fields; no compatibility aliases are provided
- `cloudfall-mcp --help` now documents every option and the confirmation
  handshake
- README quickstart covers cloning, `uv sync`, and automatic Python 3.14
  provisioning; the Task prerequisite is documented with a direct `uv run`
  equivalent
- Roadmap gained a forward-looking M6 section (catalog breadth, Render API
  import, cutover generator, secrets v2, bare-metal provisioning, backup and
  restore operations, fleet observability)
- `cloudfall`, `cloudfall-engine`, and `cloudfall-mcp` match long options
  exactly and report usage errors as the JSON error envelope
  (`invalid_argument`, exit 2) instead of argparse prose; unrecognized
  options are named without their values
- **Breaking: `cloudfall deploy`, `rollback`, `restart`, and `data migrate`
  change servers only with `--yes`.** Without it they run every
  controller-side check (declared component or service, verified artifact,
  declared database, source URL file) and print a `status: plan` preview
  naming the target servers, exit `0`, matching `cloudfall migrate` and the
  MCP confirmation handshake; scripts and Taskfile targets must add `--yes`
- Runtime path options (`--output`, `--receipts`, `--inventory-file`,
  `--observed`, `--plan-file`, and the other evidence and receipt
  directories) on all three entry points, and the MCP tools' output and plan
  paths, refuse a relative path that climbs out of the project
  (`tmp/../../x`); a location outside the project must be given as an
  absolute path

### Fixed

- A guessed `--api-key` or `--source-url` no longer binds to
  `--api-key-file`/`--source-url-file` and echoes the secret in the
  resulting file-not-found error
- Malformed component, service, proposal, application, server, and release
  ids fail as `invalid_argument` before any work starts instead of crashing
  with a traceback on exit 1
- JSON documents are flushed as they are written, so `operator run
  --interval` under a pipe or journald delivers each pass instead of
  holding it in the buffer and losing it on termination
- A missing or unreadable gateway CA, certificate, or key reports
  `operator_gateway_material_invalid` instead of an uncaught traceback
- `cloudfall init` suggests `uv run cloudfall config validate` as the next
  step; the previous `config validate .` hint no longer parsed

## [0.1.0] - 2026-09-08

First public milestone: the Render-to-Hetzner wedge proven live end to end
on disposable Hetzner Cloud Debian 13 servers (see
[`docs/proving-runs/`](docs/proving-runs/)).

### Added

- Declarative state module: versioned v1 JSON Schemas for `Server`,
  `HostProfile`, `Project`, `Component`, `Service`, `Domain`, `SshPublicKey`,
  and `LoggingStack`, plus observation, receipt, and artifact schemas
- `cloudfall` CLI and Python API: state validation, typed inventory,
  config-versus-observed drift audit with distinct exit codes, service
  lifecycle status, and the evidence-derived operations dashboard
  (static build and live `dashboard serve`)
- `cloudfall-engine`: deterministic Ansible inventory rendering and the
  artifact builder (git ref to hashed tarball with release metadata)
- Engine roles: Debian bootstrap, UTC time baseline, SSH access hardening,
  nftables default-deny firewall, unattended security upgrades, read-only
  inspection, PostgreSQL with peer-auth databases and backup timers,
  Nginx/TLS domain routes with Let's Encrypt issuance, and the guarded
  Loki/Grafana/Alloy logging stack over an mTLS gateway
- Health-gated deploy slice: digest-verified artifact transfer, symlink
  releases, automatic rollback on failed health checks, and release receipts
- `cloudfall import render`: `render.yaml` blueprint importer with a
  structured gap report (`IMPORT-REPORT.md`)
- `cloudfall data migrate`: guided managed-Postgres dump and restore with
  row-count verification and a `DataMigrationReceipt`
- `cloudfall migrate`: resumable end-to-end orchestrator with persisted step
  progress and a DNS-verification pause at the cutover moment
- `cloudfall-mcp`: seventeen annotated MCP tools; read-only evidence tools
  exposed freely, every server-changing tool gated behind a two-step
  confirmation handshake
