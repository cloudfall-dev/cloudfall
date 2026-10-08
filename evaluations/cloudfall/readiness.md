# cloudfall — Readiness

**CLI version:** 0.1.0
**Date:** 2026-09-14
**Depth:** full
**Total:** 8/15  [C]

| Dimension | Score | Notes |
|---|---|---|
| Documentation Quality | 1/3 | No AGENTS.md; README has copy-pasteable usage, `CLOUDFALL_PROJECT`, `--yes`, exit codes, but no agent-specific guide |
| Self-Description | 1/3 | No `--schema`/`manifest` command (all exit 2); argparse `--help` is structured at every level |
| Pre-built Integrations | 2/3 | `cloudfall-mcp` ships in the same package (co-versioned), verified live (24 tools); covers 20/26 CLI leaf commands, `init`/`add`/`dashboard` absent |
| Setup Reproducibility | 1/3 | `git clone` + `uv sync` is non-interactive and idempotent (`uv sync --dry-run`: no changes), deps in `pyproject.toml`/`uv.lock`; `--version` exits 2 |
| Workflow Coverage | 3/3 | README "Start your own project" and migrate walkthroughs are multi-step; `config validate --project config/examples` verified exit 0 |

---

## Dimension Details

### 1. Documentation Quality — 1/3

- `AGENTS.md`, `CODING_AGENTS.md`: absent. `.claude/` holds only `settings.local.json`
- `README.md` has CLI usage examples (Quickstart, Start your own project, Migrate from Render, audit), documents `CLOUDFALL_PROJECT`, `--project`, `migrate --yes`, audit exit codes `0/1/2/3`, and the JSON output contract. It positions Cloudfall as "AI-agent native" but gives no agent operating guidance (canonical invocation, which commands mutate servers, input conventions) outside `cloudfall-mcp` instructions
- Spot-check against `--help`: `--project` (present on every project-scoped command), `migrate --yes` (present, "execute the plan; without this flag only the plan is shown"), `audit --observed` (present, required). No functional discrepancies

### 2. Self-Description — 1/3

- Tried `cloudfall --schema`, `cloudfall manifest`, `cloudfall --manifest`: all exit 2 with argparse usage errors
- `--help` is argparse-generated at root, group, and leaf level: flag names, metavars, required vs optional, and defaults are readable. Types and exit codes are not stated; the full tree needs 1 + 12 group + ~26 leaf `--help` calls to discover

### 3. Pre-built Integrations — 2/3

- Artifacts found: `cloudfall-mcp` (MCP stdio server, `[project.scripts]` in `pyproject.toml`, `mcp` optional extra). No OpenAPI, SKILL.md, recipes. `engine/ansible/playbooks` are engine internals, not agent integrations; `.github/workflows` is CI
- Co-versioning: **co-versioned (same package)** — `cloudfall`, `cloudfall-engine`, `cloudfall-mcp` are all entry points of one wheel
- Functional check: connected over stdio with `--project config/examples`, `list_tools` returned 24 tools, `validate_config` returned the same JSON as the CLI (`"status": "ok"`, 12 resources)
- Coverage: 20 of 26 CLI leaf commands have an MCP tool (77%; 14/17 top-level groups, 82%). Missing: `init`, `add ssh-key|server-type|server`, `dashboard build|serve`. MCP additionally exposes engine operations (`build_artifact`, `inspect_servers`, `converge_*`) the `cloudfall` CLI does not. Scored 2 as a complete, working, co-versioned server that falls just short of full command coverage

### 4. Setup Reproducibility — 1/3

- Install: `git clone https://github.com/cloudfall-dev/cloudfall.git && cd cloudfall && uv sync` (README Quickstart); for projects `uvx --from git+https://github.com/cloudfall-dev/cloudfall.git cloudfall init my-project`. Not on PyPI
- Non-interactive: yes. Idempotent: `uv sync --dry-run` reports "Would make no changes" (exit 0)
- Dependencies: declared in `pyproject.toml`, locked in `uv.lock`; `uv` provisions Python 3.14
- Verification fails: `cloudfall --version` exits 2; no `doctor`/health-check command. Install lives only in README

### 5. Workflow Coverage — 3/3

- Examples are copy-pasteable with documentation-range placeholders (`203.0.113.10`, `~/.ssh/id_ed25519.pub`, `acme-api=main`) and cover create (`init`, `add`), read (`config validate`, `inventory show`, `audit`), and execute (`migrate --yes`)
- Multi-step workflows: README "Start your own project" (init → add → validate → render → inspect → audit), "Migrate from Render" (import → migrate plan → migrate `--yes`), plus `docs/render-migration-guide.md`, `docs/neon-migration-guide.md`, `docs/operator-guide.md`
- Verified: `uv run cloudfall config validate --project config/examples` → exit 0, `{"byKind": {...}, "resources": 12, "status": "ok"}`; `inventory show` → exit 0, JSON inventory
- Gaps: no documented update/delete path for resources (edit YAML by hand); `cloudfall-engine` steps in the workflow sit outside the `cloudfall` CLI

---

## Recommended Improvements

### Documentation Quality — currently 1/3

**To reach 2/3:** Add `AGENTS.md` at the repo root with the canonical invocation (`uv run cloudfall`), the `--project`/`CLOUDFALL_PROJECT` resolution order, and `migrate --yes`
**To reach 3/3:** Extend `AGENTS.md` with the stdout-JSON/stderr-JSON contract, exit codes per command, and which commands change servers

### Self-Description — currently 1/3

**To reach 2/3:** Add `cloudfall --schema` that walks the argparse tree and emits commands and flags as JSON
**To reach 3/3:** Include typed flags, per-command exit codes, and an `etag` so the output validates against `ManifestResponse`

### Pre-built Integrations — currently 2/3

**To reach 3/3:** Add MCP tools for `add ssh-key|server-type|server` and `dashboard build` (or document their deliberate exclusion) so every CLI command has an agent path

### Setup Reproducibility — currently 1/3

**To reach 2/3:** Add a root `--version` flag reporting the package version and pinned commit
**To reach 3/3:** Document install plus a `cloudfall --version` health check in `AGENTS.md`

---

## Related failure modes

| §N | Title | Severity | Readiness dimension |
|---|---|---|---|
| §44 | Agent Knowledge Packaging Absence | Medium | Documentation Quality |
| §52 | Recursive Command Tree Discovery Cost | Medium | Self-Description |
| §21 | Schema & Help Discoverability | Medium | Self-Description |
| §20 | Environment & Dependency Discovery | Medium | Setup Reproducibility |
