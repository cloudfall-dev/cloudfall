# cloudfall — Environment Profile

**Generated:** 2026-09-14

## OS
- Platform: darwin
- Version: 25.2.0 (arm64)

## Runtime
- Language: Python
- Version: 3.14.7 (`requires-python = ">=3.14"`)
- Toolchain: uv 0.12.5 (hatchling build backend; `uv sync` provisions the interpreter and dependencies)

## Binary
- Entry point: `uv run cloudfall` (from the repository root)
- Version: no `--version` flag (`cloudfall --version` exits 2 with "the following arguments are required: command"); package metadata reports `0.1.0`
- Resolved path: `.venv/bin/cloudfall` (`cloudfall.cli:run`)
- Sibling entry points: `cloudfall-engine` (`cloudfall_engine.cli:run`), `cloudfall-mcp` (`cloudfall.mcp_server:run`, MCP stdio server; needs the `mcp` extra)
- Parser: argparse; `-h/--help` exits 0 at every level
- Command tree:
  - `init [directory] [--name] [--rev] [--source]`
  - `add ssh-key|server-type|server`
  - `config validate`
  - `inventory show`
  - `audit --observed DIR`
  - `operator run|list|show|approve`
  - `backup run|verify <service>`
  - `secrets render <component>`
  - `services inspect|status`
  - `dashboard build|serve`
  - `data migrate <service>`
  - `deploy|rollback|restart|health <component>`
  - `import render|render-api`
  - `migrate` (plan-then-execute orchestrator)

## Non-Interactive Flags
- `migrate --yes`: execute the plan; without it the plan is only shown (dry-run by default)
- `migrate --restart`: discard the persisted plan (`tmp/migrate/plan.json`) and start over
- `operator run --interval N`: without it, one watch pass then exit (non-blocking default)
- `operator approve --verify-timeout N`: bound the trigger-resolution wait (default 180s)
- `dashboard serve --port 0`: pick a free port
- `import render-api --api-key-file`, `data migrate --source-url-file`: secrets are passed by file, never argv
- No global `--yes`/`--non-interactive` flag was found; no subcommand's help lists an interactive prompt

## Output Format Flags
- None discovered. README states successful and failed results are emitted as structured JSON; `cli.py` writes JSON results to stdout and JSON error objects (`error.as_dict()`) to stderr
- `--output DIR` on `services inspect`, `dashboard build`, `import render*`, `secrets render` sets an output location, not a format

## Config
- `CLOUDFALL_PROJECT`: project directory when `--project` is not given; else the current directory when it is a project. Relative paths (all `tmp/` defaults) resolve against the project
- `--schemas`: versioned schema directory (default: bundled schemas in the wheel)
- `--engine`: engine directory with Ansible contracts (default: bundled engine)
- No other `CLOUDFALL_*` env vars found in `sdk/src` or `engine/src`

## Timeout Method
- `timeout N` (GNU coreutils via Homebrew at `/opt/homebrew/bin/timeout`; `gtimeout` also present). `subprocess.run(timeout=N)` as a portable fallback

## Source
- `README.md` (Quickstart, project model, JSON output claim), `pyproject.toml` (`[project.scripts]`), `cloudfall -h` and every subcommand's `-h`, grep of `sdk/src` and `engine/src` for env vars and stdout/stderr writes
- `AGENTS.md`, `CODING_AGENTS.md`, `CLAUDE.md`: not present in the repository
- Discrepancy: README says "structured JSON" results, but no `--format`/`--json` flag exists; JSON is the only mode (to verify in evaluation)

## Updates
- 2026-09-23: package version is 0.5.1; `cloudfall --version` prints JSON and exits 0; every document carries `meta.schema_version` + `meta.tool_version` + `warnings`; global `--schema-version MAJOR` pin; `changelog [--since MAJOR.MINOR]` command; `--schema`/`--print-schema` manifest (flags, effect, output schema with `x-stability` per top-level key); `--project` is no longer accepted before the subcommand (use `CLOUDFALL_PROJECT`); argparse usage errors are JSON; new commands `why`, `observe`, `operations`
