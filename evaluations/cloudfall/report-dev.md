# cloudfall — Fix Report

**Generated:** 2026-09-14
**CLI version:** 0.1.0
**Scope:** critical
**In findings:** 22 failure modes evaluated

## Summary

| Severity | Pass (3/3) | Partial (1–2) | Fail (0) | Indeterminate (?) |
|---|---|---|---|---|
| Critical | 0 | 20 | 2 | 0 |
| High | 0 | 0 | 0 | 0 |
| Medium | 0 | 0 | 0 | 0 |

---

## Required Fixes  _(score < 3, sorted: severity desc, score asc)_

### §43 — Tool Output Result Size Unboundedness  [Critical · 0/3]

**Gap:** `inventory show` on a 600-server project returned 181,068 bytes as one JSON line, exit 0, no `meta.truncated`/`total_bytes`; `--max-output` rejected (exit 2); no limit flag on any command. Only internal caps: engine log tail in `detail` ≤ 2000 chars, proposal description ≤ 500 chars. `cloudfall-mcp` tools return the same unbounded payloads

**Solutions:**
**For CLI/tool authors:**
```bash
# Provide a --max-length or --truncate flag
my-tool get-record --id 12345 --max-length 10000 --truncate-mode head

# Output envelope should signal truncation
{
  "ok": true,
  "data": {"id": "12345", "description": "First 10000 chars..."},
  "meta": {"truncated": true, "total_bytes": 204800, "returned_bytes": 10000,
           "truncation_hint": "Use --offset and --max-length for subsequent chunks"}
}
```

**For framework design:**
- Implement a default output size limit per command (e.g., 50KB of text content) with the excess truncated and `meta.truncated: true` set.
- Provide a `--max-output` flag (injected automatically on all commands) that the agent can set to control output size.
- For large string fields in responses, automatically truncate at a configurable `max_field_length` (default: 10,000 chars) and add a `"_truncated": true` marker on the field.
- In MCP tool definitions, expose `maxOutputBytes` as a tool annotation so clients can pre-negotiate output size.
- Schema should declare `"max_output_bytes": 51200` as a tool property, allowing agents to assess expected output size before calling.

**Requirements that address this:**
- REQ-F-052 (P0) — Response Size Hard Cap with Truncation Indicator [Tier: F = framework handles]
- REQ-O-049 (P2) — LLM Token Budget Flags [Tier: O = you opt in]

---

### §74 — Credential Scope Declaration Absence  [Critical · 0/3]

**Gap:** `cloudfall --schema` and `check-permissions` do not exist (both exit 2). No per-command credential map: guides list prerequisites only ('root SSH access' in render-migration-guide; operator 'client certificate … signed by the same CA that validates collector certificates'). Nothing states that `config validate`/`inventory show`/`services status` need no credentials, `import render-api` needs only read access, or that `add server` defaults `--ssh-user root`. The operator reuses the collector CA, and the gateway nginx template has no per-location client check, so any collector cert can read `/api/v1/alerts` and the operator cert can push logs/metrics

**Solutions:**
**Declare `required_scopes` per command in `--schema` output:**
```json
{
  "command": "issue list",
  "danger_level": "safe",
  "required_scopes": ["repo:read"],
  "flags": { "repo": { "type": "string", "required": true } }
}
```

**Provide a `check-permissions` pre-flight command:**
```bash
$ tool check-permissions --for issue:list
{
  "ok": true,
  "required_scopes": ["repo:read"],
  "active_scopes": ["repo:read", "repo:write"],
  "over_privileged": true,
  "warnings": ["Active credential has scopes beyond what this command needs"]
}
```

**Warn in `warnings[]` when active credential exceeds declared scopes:**
```json
{
  "ok": true,
  "data": { ... },
  "warnings": [
    "Credential has write access; this command only requires read — consider a scoped token"
  ]
}
```

**Document minimal credential recipes in AGENTS.md:**
```markdown
## Minimal credentials by workflow

| Workflow | Required scopes | How to create |
|----------|----------------|---------------|
| Read issues and PRs | `repo:read` | Fine-grained PAT → Contents: Read |
| Comment on issues | `repo:read`, `issues:write` | Fine-grained PAT → Issues: Read+Write |
| Never needed by agents | `delete_repo`, `admin:org` | Do not grant |
```

**For framework design:**
- Commands declare `required_scopes: []` at registration; framework enforces that the field is present
- Framework compares `required_scopes` against the credential's active scopes at invocation and emits structured warnings on over-privilege
- `check-permissions` is a built-in command that accepts `--for <command>` and returns a machine-readable scope report
- Credentials with `admin` or `owner`-level scopes trigger an unconditional warning when used in agent sessions

**Requirements that address this:**
- REQ-C-029 (P0) — Command Declares Required Scopes [Tier: C = you declare]
- REQ-O-047 (P0) — tool check-permissions Built-In Command [Tier: O = you opt in]

---

### §1 — Exit Codes & Status Signaling  [Critical · 1/3]

**Gap:** Semantic codes exist but overload: 0 ok; 1 = audit drift, `health` unhealthy, engine execution failed, `migrate` step failed, `operator approve` unverified, and any uncaught traceback; 2 = argparse usage, config/validation error, not-found (`lifecycle_component_missing`, `operator_proposal_missing`, `observation_directory_missing`); 3 = audit unknown / `migrate` paused. Documented only for `audit` (README) and not in `--help`; no `exit_code` in JSON bodies. Observed: missing args 2 (prose), unknown component 2 (JSON), invalid id `Bad ID!` 1 (traceback), unresolvable hosts 1 (`status: unhealthy`)

**Solutions:**
**For CLI tool authors:**
```
Exit code conventions to follow:
  0  = success, operation completed as intended
  1  = general error (use sparingly — be specific)
  2  = misuse / bad arguments (before operation starts)
  3  = operation started but failed mid-way
  4  = precondition not met (dependency missing, not initialized)
  5  = not found (the thing you asked about doesn't exist)
  6  = conflict / already exists
  7  = timeout
  8  = permission denied
  9  = rate limited / quota exceeded
```

**Separate "not found" from "error":**
```bash
# Bad: exits 1 for both "error" and "not found"
tool get-user --id 123
# exit 1

# Good: exits 5 for "not found", 1 for actual errors
tool get-user --id 123
# exit 5  ← agent knows to stop, not retry
```

**For CLI framework design:**
- Define a standard exit code table in your framework
- Provide typed exit code constants (not magic numbers)
- Make every command document its possible exit codes in `--help`
- Support `--exit-on-warning` flag to make strict mode opt-in

**Requirements that address this:**
- REQ-F-001 (P0) — Standard Exit Code Table [Tier: F = framework handles]
- REQ-F-002 (P0) — Exit Code 2 Reserved for Validation Failures [Tier: F = framework handles]
- REQ-C-001 (P0) — Command Declares Exit Codes [Tier: C = you declare]

---

### §2 — Output Format & Parseability  [Critical · 1/3]

**Gap:** JSON is the only mode and stdout never carried prose in any run (`sort_keys`, one object per line). But no `--output json` (rejected, exit 2) and no consistent envelope: success objects use `status: ok` with command-specific keys (`byKind`, `inventory`, `proposals`); `operator show` prints the raw proposal document with no `status`; `health` returns `status: unhealthy` on stdout; `operator run` emits several JSON documents (NDJSON, one per pass); errors are `{status: error, error: {...}}` on stderr except `operator_*` errors (bare `{code, message}`) and `migrate` step failures (stdout). argparse usage errors and tracebacks are prose on stderr. Zero-item result is valid: `{"proposals": [], "status": "ok"}`

**Solutions:**
**Machine-readable output flag:**
```bash
# Always provide a structured output mode
tool list-users --output json
tool list-users --output jsonl   # one JSON object per line for streaming
tool list-users --output tsv     # tab-separated, good for piping
tool list-users --output plain   # minimal, no decoration (for humans too)
```

**JSON output schema:**
```json
{
  "ok": true,
  "data": [...],      // always present, even if empty array/null
  "error": null,      // always present
  "meta": {
    "count": 2,
    "duration_ms": 45
  }
}
```

**On failure:**
```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "NOT_FOUND",
    "message": "User with id=999 does not exist",
    "details": {}
  }
}
```

**Rules for agent-compatible output:**
1. Same schema whether 0, 1, or N results
2. No prose mixed into data output (prose goes to stderr)
3. No color codes in `--output json` mode (detect `NO_COLOR` env var)
4. Numbers always in invariant locale (`.` decimal, no thousands separator)
5. Dates always in ISO 8601 (`2024-03-11T14:30:00Z`)
6. Boolean as `true`/`false`, never `yes`/`no`/`1`/`0` in JSON mode

**For framework design:**
- Auto-detect output format based on `--output` flag or `CI=true` env
- Provide output formatters as first-class framework primitives
- Emit a JSON schema for every command's output via `--output-schema`

---

> **Merged from §48:** The following content was originally a separate challenge.
> It is consolidated here because it describes a specific case of the same root problem.

**Requirements that address this:**
- REQ-F-003 (P0) — JSON Output Mode Auto-Activation [Tier: F = framework handles]
- REQ-F-004 (P0) — Consistent JSON Response Envelope [Tier: F = framework handles]
- REQ-F-005 (P0) — Locale-Invariant Serialization [Tier: F = framework handles]
- REQ-F-074 (P1) — JSON Null/Absent/Empty Convention [Tier: F = framework handles]
- REQ-O-001 (P0) — --output Format Flag [Tier: O = you opt in]
- REQ-O-042 (P2) — Output Format Environment Variable Default [Tier: O = you opt in]

---

### §11 — Timeouts & Hanging Processes  [Critical · 1/3]

**Gap:** Never hung, but no caller-set timeout: `--timeout 2` rejected (exit 2); only `operator approve --verify-timeout`. Built-in bounds: SSH `ConnectTimeout=10`, HTTP 30s (Render) / 10s (gateway), 3600s per engine step. `health` on blackholed 203.0.113.10/11 took 10.89s → exit 1 `"status": "unhealthy"` (the timeout is reported as an unhealthy app); `import render-api` took 30.19s → `render_api_unreachable` ('urlopen error timed out') exit 2. No `TIMEOUT` code, no exit 10, no `completed_steps`

**Solutions:**
**Built-in timeout flags:**
```bash
tool operation --timeout 30s        # fail after 30 seconds
tool operation --connect-timeout 5s # specifically for connection phase
```

**Progress heartbeats to stderr:**
```bash
$ tool long-operation --output json
# stderr:
[  2s] Starting...
[  5s] Phase 1/3: downloading (23%)
[ 10s] Phase 1/3: downloading (67%)
[ 15s] Phase 2/3: processing
# stdout (only on completion):
{"ok": true, "data": {...}}
```

**Emit partial results before timeout:**
```json
{
  "ok": false,
  "partial": true,
  "data": {"processed": 42, "total": 100},
  "error": {"code": "TIMEOUT", "message": "Operation timed out after 30s"},
  "resume_token": "abc123"   // allows resuming if supported
}
```

**For framework design:**
- Every command has a default timeout; `--timeout 0` means no timeout (must be explicit)
- Timeout exits with a specific code (e.g., `7`) and always emits JSON error
- Provide `--heartbeat-interval` to control stderr progress frequency
- Track and report wall time in every JSON response's `meta.duration_ms`

**Requirements that address this:**
- REQ-F-011 (P0) — Default Timeout Per Command [Tier: F = framework handles]
- REQ-F-012 (P0) — Timeout Exit Code and JSON Error [Tier: F = framework handles]
- REQ-F-039 (P1) — Duration Tracking in Response Meta [Tier: F = framework handles]
- REQ-F-078 (P2) — Retry Count in Response Meta [Tier: F = framework handles]
- REQ-C-012 (P0) — Commands with Network I/O Support --timeout [Tier: C = you declare]
- REQ-O-012 (P2) — --heartbeat-interval Flag [Tier: O = you opt in]

---

### §12 — Idempotency & Safe Retries  [Critical · 1/3]

**Gap:** No `--idempotency-key` (rejected, exit 2) and no `effect` field. Retries do not duplicate: a second identical `add server h8` / `add ssh-key` → `resource_exists` exit 2 (not a noop exit 0, so a retry after a lost response looks like a failure); `migrate --yes` persists progress in `--plan-file` and a re-run resumes at the recorded `next` step (plan mode writes nothing); engine playbooks are convergent Ansible. `init` on an existing dir reported `project_revision_uncommitted` before checking emptiness. `deploy` of the same release twice not exercised (needs a live host)

**Solutions:**
**Idempotency keys:**
```bash
tool create-order --amount 100 --idempotency-key "order-$(date +%s)-$RANDOM"
# Server deduplicates based on key
# Safe to retry indefinitely
```

**Declare operation effect in output:**
```json
{
  "ok": true,
  "effect": "created",        // "created" | "updated" | "noop" | "deleted"
  "data": {"id": 42}
}
```

```json
{
  "ok": true,
  "effect": "noop",
  "reason": "Already at version 1.2.3",
  "data": {"current_version": "1.2.3"}
}
```

**`--dry-run` flag for all mutating commands:**
```bash
tool deploy --version 1.2.3 --dry-run
# Output:
{
  "ok": true,
  "effect": "would_create",
  "changes": ["would update service to 1.2.3", "would restart 2 instances"]
}
```

**For framework design:**
- Mark commands as `safe` (read-only, always idempotent) or `unsafe` (mutating)
- Require `--idempotency-key` for all `unsafe` commands, or generate one automatically
- Emit `effect` field in all responses
- Implement `--dry-run` as a framework-level feature, not per-command

**Requirements that address this:**
- REQ-C-003 (P0) — Mutating Commands Declare effect Field [Tier: C = you declare]
- REQ-C-007 (P1) — Mutating Commands Accept --idempotency-key [Tier: C = you declare]
- REQ-C-028 (P1) — ALREADY_EXISTS Response Pattern [Tier: C = you declare]

---

### §23 — Side Effects & Destructive Operations  [Critical · 1/3]

**Gap:** `migrate` is plan-by-default: without `--yes` it prints the 13-step plan, exit 0, and writes no plan file. `deploy`, `rollback`, `restart`, `backup run\|verify`, `data migrate`, `secrets render`, `operator approve` execute immediately: `--dry-run` rejected (exit 2), no confirmation, no `danger_level`, no `effect`. Guards live elsewhere: `data migrate` refuses non-empty targets, deploys are health-gated with receipts, and `cloudfall-mcp` marks mutating tools destructive and requires a `confirm=true` second call

**Solutions:**
**Explicit destructive flag:**
```bash
tool delete-account --user 42 --confirm-destructive
# Without the flag: exits with clear error explaining the flag is required
```

**Machine-readable danger level in help:**
```json
{
  "command": "delete-account",
  "danger_level": "destructive",   // "safe" | "mutating" | "destructive"
  "reversible": false,
  "requires_confirmation": true
}
```

**Dry-run always available for destructive commands:**
```bash
$ tool delete-account --user 42 --dry-run
{
  "ok": true,
  "effect": "would_delete",
  "would_affect": {
    "user": {"id": 42, "name": "Alice"},
    "related_records": 234,
    "reversible": false
  }
}
```

**Audit output:**
```json
{
  "ok": true,
  "effect": "deleted",
  "audit": {
    "timestamp": "2024-03-11T14:30:00Z",
    "operator": "agent-session-abc123",
    "target": {"type": "user", "id": 42},
    "reversible": false
  }
}
```

**For framework design:**
- Commands declare `danger_level` in their schema
- Framework enforces `--dry-run` availability for all `destructive` commands
- `--yes` / `--confirm-destructive` flags auto-supplied by agent harness
- Generate audit log entries for all `mutating` and `destructive` operations

**Requirements that address this:**
- REQ-C-002 (P0) — Command Declares Danger Level [Tier: C = you declare]
- REQ-C-004 (P0) — Destructive Commands Must Support --dry-run [Tier: C = you declare]
- REQ-O-021 (P0) — --confirm-destructive Flag [Tier: O = you opt in]

---

### §24 — Authentication & Secret Handling  [Critical · 1/3]

**Gap:** Designed file-only: Render key via `--api-key-file`, DB URL via `--source-url-file`, sops/age env files, mTLS cert paths; invalid Render key (401) error names the URL, not the key (0 occurrences of the key in all captured output); `secrets render` outputs key names only. But argparse prefix matching accepts `--api-key rnd_x` as `--api-key-file` and the error echoes it: `Render API key file does not exist: rnd_x`. Auth failures exit 2 (same as bad input), no dedicated auth code (8/10)

**Solutions:**
**Prefer environment variables:**
```bash
TOOL_API_TOKEN=sk-... tool deploy
# Convention: TOOL_VARNAME
```

**Support secrets files:**
```bash
tool deploy --token-file /run/secrets/api-token
# File path, not the value
```

**Never echo secrets in output or errors:**
```json
// Bad
{"error": "Invalid token: sk-prod-abc123xyz789"}

// Good
{"error": {"code": "AUTH_TOKEN_INVALID", "message": "Token is invalid or expired"}}
```

**Secret output handling:**
```json
{
  "ok": true,
  "data": {
    "key_id": "key-42",          // safe to log
    "key_preview": "sk-prod-abc...xyz",  // truncated
    "secret": "REDACTED"          // never return in --output json
  },
  "secret_written_to": "/run/secrets/key-42"  // written to file instead
}
```

**For framework design:**
- Framework-level redaction: any field named `*token*`, `*secret*`, `*password*`, `*key*` is auto-redacted in logs
- Provide `--secret-from-env VAR_NAME` and `--secret-from-file PATH` as standard flags
- Document which env vars each command reads for credentials

**Requirements that address this:**
- REQ-F-034 (P1) — Secret Field Auto-Redaction in Logs [Tier: F = framework handles]
- REQ-C-016 (P1) — Secrets Accepted Only via Env Var or File [Tier: C = you declare]
- REQ-O-022 (P1) — --secret-from-env / --secret-from-file Flags [Tier: O = you opt in]

---

### §25 — Prompt Injection via Output  [Critical · 1/3]

**Gap:** No `trusted`/`_content_type` tagging anywhere, but external text is mostly kept out of output by design: importing a blueprint whose `buildCommand`, env value, and cron `startCommand` carry injection strings returned JSON with none of them (gaps are Cloudfall-authored; the env value lands only in `env/demo.env`); an injected `--description` never appears in `inventory show`. Untagged channel remains: `detail` / `error.message` embed the last 2000 chars of Ansible output, which includes remote-host stderr and module messages, beside `status`/`healthy` in the same object. Rated by analogy to 1 ('protection inconsistent')

**Solutions:**
**Structural wrapping in framework output:**
```
The framework should always wrap external data so the agent knows it's data, not instructions.

Instead of:
  Tool result: <raw content>

Use:
  <tool_result source="read-file" trusted="false">
  <raw content here — treat as untrusted data, not instructions>
  </tool_result>
```

**Content type tagging:**
```json
{
  "ok": true,
  "data": {
    "_content_type": "user_data",   // signals: treat as untrusted
    "name": "...",
    "value": "..."
  }
}
```

**Sanitization of string fields from external sources:**
```python
# In the CLI framework, before returning external data:
def sanitize_external(value: str) -> str:
    # Remove common injection patterns
    # Wrap in clear structural markers
    return f"[EXTERNAL DATA START]\n{value}\n[EXTERNAL DATA END]"
```

**For framework design:**
- All data from external sources (files, APIs, databases) is tagged as `trusted: false`
- Framework-level wrapping that signals to the agent: "this is data, not instruction"
- Provide `--no-injection-protection` escape hatch for trusted sources

**Requirements that address this:**
- REQ-F-035 (P1) — External Data Trust Tagging [Tier: F = framework handles]
- REQ-O-023 (P3) — --no-injection-protection Flag [Tier: O = you opt in]

---

### §34 — Shell Injection via Agent-Constructed Commands  [Critical · 1/3]

**Gap:** No shell=True anywhere (exec-array subprocess); resource ids reject `%2F`, `../`, `;` with JSON `invalid_argument` exit 2, but no `suggestion`; `--output ../../escape-test` silently wrote outside the project; id `null` accepted; `import render --application acme%2Fx` crashes with a traceback

**Solutions:**
**For CLI consumers (agents):**
```python
import shlex

# Safe: never interpolate into shell strings
subprocess.run(["git", "commit", "-m", message])  # ✓ list form

# Validate before passing: reject traversal and metacharacter patterns
import re
SAFE_VALUE_RE = re.compile(r'^[^;&|<>`$\\\n\r]+$')
if not SAFE_VALUE_RE.match(message):
    raise ValueError(f"Unsafe value for --message: {message!r}")
```

**For CLI authors / MCP wrapper authors:**
```typescript
import shellEscape from 'shell-escape';

// In MCP tool handler: receive typed args from JSON, construct safely
const args = ["git", "commit", "-m", request.params.arguments.message];
const result = await execFile(args[0], args.slice(1));  // ✓ never shell=True
```

**For framework design:**
- Reject arguments containing `../`, `./`, percent-encoded characters (`%[0-9a-fA-F]{2}`), embedded query string markers (`?`, `#`), and shell metacharacters (`;`, `&&`, `||`, backtick, `$()`) by default.
- Provide a whitelist-based argument sanitizer as a framework primitive: `@arg(pattern=r'^[\w\-\.]+$')`.
- Default to `subprocess.run(args_list)` (never `shell=True`) in all generated subprocess calls.
- Apply jpoehnelt Axis 5 level 2 checks at argument parsing time, before any execution.
- MCP wrappers: always receive arguments as typed JSON objects, never concatenate into shell strings.

**Requirements that address this:**
- REQ-F-044 (P0) — Shell Argument Escaping Enforcement [Tier: F = framework handles]
- REQ-C-019 (P1) — Subprocess-Invoking Commands Declare Argument Schema [Tier: C = you declare]

---

### §42 — Debug / Trace Mode Secret Leakage  [Critical · 1/3]

**Gap:** No `--debug`/`--trace`/`--token` flags; secrets enter only by file (`--api-key-file`, `--source-url-file`, sops dir), so none reach argv or the process table; Render 401 error names the URL, not the key; data-migration URL is staged with `no_log` and read via `$(cat)` on the host. No `[REDACTED]` layer: argparse echoes a hallucinated `--token sk-live-SECRET-abc123` verbatim, and `ANSIBLE_VERBOSITY=4` leaks into the engine subprocess and its verbose log tail lands in the JSON `detail`. Lowered to 1 after §24: argparse prefix-matches `--api-key`/`--source-url` to the `*-file` flags and the not-found error echoes the secret (`postgresql:/u:hunter2@db/x`)

**Solutions:**
**For CLI authors:**
```python
from pydantic import SecretStr

class DeployConfig(BaseModel):
    api_key: SecretStr  # repr never shows value; model_dump() returns "[REDACTED]"
    region: str

# Argparse: use action to mask value in namespace repr
import argparse
class SecretAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
    def __repr__(self):
        return f"{self.dest}=[REDACTED]"
```

**For framework design:**
- Apply name-based heuristics to automatically redact argument values whose names match `token|secret|password|key|credential|auth|apikey` in all trace/debug output.
- Never echo argument values in error messages for arguments marked `sensitive=True` or matching the redaction pattern.
- Provide a framework-level `--trace-safe` mode that produces a trace with sensitive fields replaced by `[REDACTED]`.
- For `--trace` or `--debug` modes: require explicit `--no-redact` opt-out to expose sensitive values.
- Use environment variables (not CLI flags) as the preferred injection mechanism for secrets — they are not visible in `process.argv` or process tables.
- Document in `--schema` output which arguments are marked sensitive: `"sensitive": true`.

**Requirements that address this:**
- REQ-F-051 (P0) — Debug and Trace Mode Secret Redaction [Tier: F = framework handles]

---

### §45 — Headless Authentication / OAuth Browser Flow Blocking  [Critical · 1/3]

**Gap:** No browser/OAuth flow anywhere; credentials are files (Render API key, mTLS cert/key, SSH keys via Ansible with `PasswordAuthentication=no`). Missing Render key → `render_api_key_missing` JSON exit 2 in 0.21s; Render 401 and a rejected mTLS client cert both surface as `*_unreachable`, no `AUTH_REQUIRED`, no `auth_methods`; missing `--gateway-cert` file → uncaught `FileNotFoundError` traceback exit 1. SSH auth prompts not exercised (no local sshd)

**Solutions:**
**For CLI authors:**
```python
# Check for non-interactive auth options before attempting browser flow
if not sys.stdin.isatty():
    # Non-interactive mode: check for token in env vars
    token = os.environ.get("MY_TOOL_TOKEN") or os.environ.get("MY_TOOL_API_KEY")
    if not token:
        print(json.dumps({"ok": False, "error": {
            "code": "AUTH_REQUIRED",
            "message": "No credentials found. Set MY_TOOL_TOKEN environment variable.",
            "auth_methods": [
                {"type": "env_var", "name": "MY_TOOL_TOKEN", "description": "API token"},
                {"type": "env_var", "name": "MY_TOOL_API_KEY", "description": "Legacy API key"}
            ]
        }}))
        sys.exit(8)  # PERMISSION_DENIED exit code
    authenticate_with_token(token)
else:
    # Interactive: offer browser flow
    launch_browser_auth_flow()
```

**For framework design:**
- Any command that triggers authentication must check `isatty()` and return a structured `AUTH_REQUIRED` error in non-interactive mode, never hang.
- The `AUTH_REQUIRED` error must include `auth_methods` — an array of structured objects describing how to authenticate non-interactively (env var name, config file format, token endpoint).
- Schema output should include `"requires_auth": true` and `"auth_methods": [...]` so agents can determine how to authenticate before first invocation.
- Support `--token` / `--api-key` as universal authentication flags that bypass stored credentials for headless use.
- Credential expiry should produce `{"code": "AUTH_EXPIRED"}` distinct from `AUTH_REQUIRED`, with instructions for renewal that work in headless mode.

**Requirements that address this:**
- REQ-C-021 (P0) — Auth Commands Declare Headless Mode Support [Tier: C = you declare]
- REQ-O-033 (P0) — --headless and --token-env-var Flags for Auth Commands [Tier: O = you opt in]

---

### §53 — Credential Expiry Mid-Session  [Critical · 1/3]

**Gap:** Expired mTLS client cert → `operator_feed_unreachable` with 'ssl/tls alert certificate expired' in message text; expired gateway server cert → same code, 'certificate has expired'; unknown-CA client cert and a network outage also map to `operator_feed_unreachable` exit 2. Expiry appears only in prose: no `CREDENTIALS_EXPIRED`, no `expired_at`, no `reauth_command`. Render API key expiry is indistinguishable from any 401 (`render_api_unreachable`)

**Solutions:**
**Auth errors MUST distinguish expiry from permission denial:**
```json
{
  "ok": false,
  "error": {
    "code": "CREDENTIALS_EXPIRED",
    "message": "Access token expired at 2024-03-11T14:15:00Z.",
    "expired": true,
    "expired_at": "2024-03-11T14:15:00Z",
    "retryable": true,
    "reauth_command": "tool auth refresh",
    "reauth_env_var": "TOOL_TOKEN"
  }
}
```

**For framework design:**
- Add `exit 10` to the standard exit code table: `10 = credentials expired (retryable with refresh)`. Exit 8 = permanent permission denied.
- Framework MUST intercept HTTP 401/403 responses and attempt to classify expiry vs permission denial before surfacing the error.
- `error.reauth_command` is a mandatory field for all auth errors — the exact command to run to recover credentials.

**Requirements that address this:**
- REQ-F-063 (P1) — Credential Expiry Structured Error [Tier: F = framework handles]
- REQ-C-030 (P1) — Error Responses Include Executable fix_command [Tier: C = you declare]

---

### §60 — OS Output Buffer Deadlock  [Critical · 1/3]

**Gap:** `operator run --interval 2` piped: 0 bytes received in 9s (5 passes, 10 JSON lines written) and 0 bytes after SIGTERM, so all output is lost; with `PYTHONUNBUFFERED=1` each pass arrives on time (t=0.3, 2.3, 4.3, 6.3, 8.4s). `dashboard serve` flushes its URL line explicitly (t=0.4s). `_write_json` never flushes; no heartbeat on `migrate --yes`, `deploy`, `backup run` (single JSON at the end, engine steps up to 3600s each)

**Solutions:**
**Unbuffer stdout explicitly in non-TTY mode:**
```python
# Python: disable buffering
import sys, os
if not sys.stdout.isatty():
    sys.stdout.reconfigure(line_buffering=True)
    # or: os.environ['PYTHONUNBUFFERED'] = '1'
```

```bash
# Wrapper: force unbuffered output
$ stdbuf -o0 my-tool migrate
$ unbuffer my-tool migrate   # via expect package
```

**Emit JSON heartbeats every N seconds for long operations:**
```json
{"status": "running", "step": "migrating table users", "elapsed_ms": 5000, "heartbeat": true}
```

**For framework design:**
- Framework MUST call `sys.stdout.reconfigure(line_buffering=True)` (Python) or `setvbuf(stdout, NULL, _IOLBF, 0)` (C) on startup when stdout is not a TTY.
- Long-running commands MUST emit a JSON heartbeat object to stdout every configurable interval (default: 10s) so the agent has proof of life.
- `PYTHONUNBUFFERED=1` and equivalent env vars MUST be set in the framework's bootstrap before any output.

**Requirements that address this:**
- REQ-F-053 (P0) — Stdout Unbuffering in Non-TTY Mode [Tier: F = framework handles]
- REQ-O-038 (P1) — --heartbeat-ms Flag for Long-Running Commands [Tier: O = you opt in]

---

### §61 — Bidirectional Pipe Payload Deadlock  [Critical · 1/3]

**Gap:** Reproduced: agent writes 1 MiB to stdin before reading stdout while `inventory show` (600-server project) writes 181 KB to stdout; after 10s the writer is stuck at 65,536 bytes and the CLI is stuck on a full stdout pipe, a deadlock until killed. Cloudfall never reads stdin and never detects or rejects piped input (no `STDIN_TOO_LARGE`); every input already comes from file flags (`--source-url-file`, `--api-key-file`, blueprint path), so agents have no reason to pipe

**Solutions:**
**Use temporary files for large payloads instead of pipes:**
```bash
# Avoid: pipe large data
echo "$large_json" | my-tool transform

# Good: use file reference
echo "$large_json" > /tmp/input.json
my-tool transform --input-file /tmp/input.json > result.json
```

**Schema declares maximum stdin payload size:**
```json
{
  "stdin_input": {
    "max_bytes": 65536,
    "overflow_flag": "--input-file",
    "overflow_hint": "For payloads >64KB, use --input-file <path> instead of stdin"
  }
}
```

**Framework enforces size limit on stdin reads:**
```python
# Framework reads stdin with size limit:
data = sys.stdin.buffer.read(MAX_STDIN_BYTES)
if len(data) >= MAX_STDIN_BYTES:
    exit_with_error("STDIN_TOO_LARGE", "Payload exceeds 64KB. Use --input-file instead.")
```

**For framework design:**
- Framework MUST enforce a maximum stdin payload size (default: 64KB) and fail with exit 2 if exceeded, directing the caller to use `--input-file` instead.
- The `--input-file` flag MUST be auto-generated by the framework for any command that accepts stdin input.
- Framework MUST document the pipe buffer limit prominently in the agent integration guide.

**Requirements that address this:**
- REQ-F-054 (P0) — Stdin Payload Size Cap with --input-file Fallback [Tier: F = framework handles]
- REQ-O-039 (P1) — --input-file Flag for Stdin Commands [Tier: O = you opt in]

---

### §10 — Interactivity & TTY Requirements  [Critical · 2/3]

**Gap:** No prompts, pager, or editor on any path; with stdin=/dev/null and a 5s cap every mutating command returned: `migrate` plan 0.28s exit 0 (execution gated by `--yes`, not a prompt), `restart`/`backup run`/`data migrate` 0.8–0.9s exit 1 with JSON `lifecycle_execution_failed` (unresolvable hosts). Ansible runs with `PasswordAuthentication=no`. Side finding: `rollback --release r1` crashes with a `ValueError` traceback. Held at 2, not 3: Ansible's SSH host-key confirmation for an unknown reachable host (read from /dev/tty, not stdin) is neither disabled nor exercised here

**Solutions:**
**Always provide non-interactive flags:**
```bash
tool deploy --non-interactive
tool deploy --yes          # auto-confirm all prompts
tool deploy --no-input     # fail immediately if input would be needed
tool init --defaults       # use defaults, skip all prompts
```

**Detect non-interactive context and adapt:**
```python
import sys
if not sys.stdin.isatty():
    # non-interactive mode: use defaults, fail on ambiguity
    # never prompt
```

**Fail fast instead of hanging:**
```bash
$ tool deploy --no-input
Error: Config file not found. Run `tool init` first or provide --config.
exit 4   # precondition not met
# ← agent gets an immediate, actionable error instead of a hang
```

**For framework design:**
- Auto-detect `sys.stdin.isatty()` and set `--non-interactive` implicitly
- Never use pagers; respect `NO_COLOR`, `TERM=dumb`, `CI` env vars
- Any command with a confirmation prompt MUST have a `--yes`/`--force` flag
- Document which commands are interactive in help text
- Set `PAGER=cat` and `GIT_PAGER=cat` in agent execution environments

---

> **Merged from §36:** The following content was originally a separate challenge.
> It is consolidated here because it describes a specific case of the same root problem.

**Requirements that address this:**
- REQ-F-009 (P0) — Non-Interactive Mode Auto-Detection [Tier: F = framework handles]
- REQ-F-010 (P0) — Pager Suppression [Tier: F = framework handles]
- REQ-F-046 (P0) — Pager Environment Variable Suppression [Tier: F = framework handles]
- REQ-C-005 (P0) — Interactive Commands Must Support --yes / --non-interactive [Tier: C = you declare]

---

### §13 — Partial Failure & Atomicity  [Critical · 2/3]

**Gap:** `migrate --yes` with unreachable hosts: exit 1 (distinct from 0 ok, 3 paused), stdout JSON `status: error`, `step: baseline`, `next: baseline`, `completed: 0`, full 13-step manifest, engine error in `error.message`; progress persists in `--plan-file`, re-running resumes at `baseline`. Gaps: failed step stays `status: pending` (not `failed`), no `partial: true`, no `--rollback-on-failure` (rollback is a manual `rollback-window` recipe); this failure JSON goes to stdout while other `migrate` errors go to stderr. Only step 1 failure exercised; a mid-plan failure after completed steps needs live hosts. Single-lifecycle commands (`deploy`, `backup run`) report no step progress

**Solutions:**
**Structured partial failure output:**
```json
{
  "ok": false,
  "partial": true,
  "completed_steps": ["backup", "apply_schema"],
  "failed_step": "migrate_data",
  "error": {"code": "DISK_FULL", "message": "..."},
  "resume_from": "migrate_data",
  "rollback_available": true
}
```

**Batch result per item:**
```json
{
  "ok": false,
  "partial": true,
  "results": [
    {"id": 1, "ok": true,  "effect": "sent"},
    {"id": 2, "ok": true,  "effect": "sent"},
    {"id": 3, "ok": false, "error": {"code": "INVALID_EMAIL"}},
    {"id": 4, "ok": true,  "effect": "sent"},
    {"id": 5, "ok": false, "error": {"code": "RATE_LIMITED"}}
  ],
  "summary": {"total": 5, "succeeded": 3, "failed": 2}
}
```

**Resumable commands:**
```bash
tool migrate-database --resume-from migrate_data
# Only runs remaining steps
```

**For framework design:**
- All multi-step commands emit a step manifest at start
- Each step emits its result as it completes (streaming JSON lines)
- Final summary always includes `completed`, `failed`, `skipped` counts
- `--rollback-on-failure` flag as standard option

**Requirements that address this:**
- REQ-C-008 (P1) — Multi-Step Commands Emit Step Manifest [Tier: C = you declare]
- REQ-C-009 (P1) — Multi-Step Commands Report completed/failed/skipped [Tier: C = you declare]
- REQ-O-010 (P2) — --resume-from Flag for Multi-Step Commands [Tier: O = you opt in]
- REQ-O-011 (P2) — --rollback-on-failure Flag [Tier: O = you opt in]

---

### §37 — REPL / Interactive Mode Accidental Triggering  [Critical · 2/3]

**Gap:** No REPL or shell mode exists (no `input()`, `cmd.Cmd`, `readline`, `sys.stdin` in sdk/engine sources); bare `cloudfall` exits 2 in 0.14s with argparse usage (prose, not JSON); `cloudfall-mcp` stdio server exits 0 in 0.53s on EOF stdin. Capped at 2: nothing declares `requires_interactive` because no schema exists

**Solutions:**
**For CLI authors:**
```python
import sys

# Gate ALL REPL/interactive modes behind TTY check
@app.command()
def shell():
    if not sys.stdin.isatty():
        print(json.dumps({"ok": False, "error": {"code": "INTERACTIVE_REQUIRED",
            "message": "Shell mode requires an interactive terminal. Run without redirection."}}))
        sys.exit(2)
    launch_repl()

# Python Fire: never register --interactive as a reachable flag in non-TTY environments
```

**For agents:**
```python
# Scan --help output for REPL-triggering flags before first invocation
REPL_FLAGS = {"--interactive", "--shell", "--repl", "-i"}
help_output = subprocess.run([tool, "--help"], capture_output=True).stdout.decode()
risky_flags = [f for f in REPL_FLAGS if f in help_output]
# Avoid those flags; set stdin=subprocess.DEVNULL to prevent stdin reads

result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
```

**For framework design:**
- Any command flagged as `interactive=True` or `mode="repl"` must gate on `sys.stdin.isatty()` and return a structured error if the check fails, rather than attempting to launch.
- Provide a framework-level `REPL_GUARD` decorator that wraps REPL-launching commands.
- Default `stdin=subprocess.DEVNULL` in all framework-generated subprocess calls and test harnesses.
- Document in `--schema` output which commands require interactive mode, so agents can skip them.

**Requirements that address this:**
- REQ-F-047 (P0) — REPL Mode Prohibition in Non-TTY Context [Tier: F = framework handles]

---

### §50 — Stdin Consumption Deadlock  [Critical · 2/3]

**Gap:** No `cloudfall` command reads stdin; with stdin held open by a never-closing pipe `config validate` exits 0 in 0.26s and `migrate` (plan mode) exits 2 in 0.29s with JSON. `add ssh-key -` treats `-` as a path → `ssh_key_file_missing` JSON exit 2, so there is no stdin convention to hang on. Only `cloudfall-mcp` consumes stdin, by design. Capped at 2: no schema declares stdin behaviour

**Solutions:**
**Non-TTY stdin reads must fail immediately with exit 4:**
```json
{
  "ok": false,
  "error": {
    "code": "STDIN_REQUIRED",
    "message": "Argument '--ids' requires input but stdin is not a TTY and no value was provided.",
    "hint": "Pass --ids <value> or pipe data: echo '123' | my-tool delete --ids -"
  }
}
```

**Schema must declare all stdin-reading paths:**
```json
{
  "args": [
    {
      "name": "ids",
      "stdin_fallback": true,
      "stdin_format": "newline-separated IDs",
      "non_tty_behavior": "fail_with_exit_4"
    }
  ]
}
```

**For framework design:**
- All stdin reads must be declared in the command schema; undeclared stdin reads are a framework error.
- In non-TTY mode, the framework wraps `stdin.read()` calls with an immediate-fail guard that exits 4 with a structured error listing the flag to pass instead.
- The `--schema` output for every command must indicate which args accept stdin as input and what format is expected.

**Requirements that address this:**
- REQ-O-039 (P1) — --input-file Flag for Stdin Commands [Tier: O = you opt in]

---

### §62 — $EDITOR and $VISUAL Trap  [Critical · 2/3]

**Gap:** No command opens an editor (no `EDITOR`/`VISUAL` reads in sources); every authoring path is flag-driven: `add server` with `EDITOR=vim VISUAL=vim` exit 0 in 0.31s, `init` with empty `EDITOR` exit 0 in 0.15s, no git commit is made. Capped at 2: no schema declares `requires_editor`. Side finding: `init`'s `next` hint `uv run cloudfall config validate .` fails verbatim (exit 2, 'unrecognized arguments: .')

**Solutions:**
**Provide non-editor alternatives for all editor-requiring operations:**
```bash
# Good: explicit content flag bypasses editor
$ git commit -m "message"
$ kubectl patch deployment/my-app --patch '{"spec": {...}}'
$ tool config set key=value   # instead of tool config edit
```

**Set $EDITOR to a non-blocking shim in non-TTY mode:**
```bash
# Framework sets: EDITOR="tee /dev/stderr" or EDITOR="cat > /dev/null"
# Or: EDITOR="my-tool-editor-shim" which reads content from --editor-content flag
```

**Detect editor invocation in non-TTY mode and fail fast:**
```json
{
  "ok": false,
  "error": {
    "code": "EDITOR_REQUIRED",
    "message": "This command requires an interactive editor. Use --message or --from-file instead.",
    "alternatives": ["git commit -m '<message>'", "git commit --file <path>"]
  }
}
```

**For framework design:**
- Framework MUST set `EDITOR=true` (a no-op) and `VISUAL=true` in the subprocess environment when in non-TTY mode, preventing any spawned subprocess from launching an interactive editor.
- Commands that use `$EDITOR` MUST declare `requires_editor: true` in their schema and provide a `--content` or `--from-file` alternative for non-TTY operation.
- Framework MUST detect editor invocations in non-TTY mode and intercept them with exit 4 and a structured error listing the non-interactive alternative.

**Requirements that address this:**
- REQ-F-055 (P0) — $EDITOR and $VISUAL No-Op in Non-TTY Mode [Tier: F = framework handles]
- REQ-C-023 (P1) — Editor-Requiring Commands Declare Non-Interactive Alternative [Tier: C = you declare]

---

### §64 — Headless Display and GUI Launch Blocking  [Critical · 2/3]

**Gap:** No command launches a browser or GUI (no `webbrowser`/`xdg-open` in sources); `dashboard serve --port 0` with `DISPLAY=` emitted `{"dashboard": {"url": "http://127.0.0.1:65098/", ...}, "status": "ok"}` at t=0.4s (explicit flush), then served until SIGTERM; `dashboard build` returns file paths in JSON. Capped at 2: no schema declares `headless_behavior`

**Solutions:**
**Detect headless environment and skip GUI operations:**
```python
import os, sys
def is_headless():
    return (
        not sys.stdout.isatty() or
        os.environ.get('CI') or
        not os.environ.get('DISPLAY') and not os.environ.get('WAYLAND_DISPLAY')
    )

if is_headless():
    # Skip browser launch; emit URL in JSON instead
    return {"ok": True, "data": {"url": url, "opened": False, "open_hint": f"open {url}"}}
```

**Schema declares GUI operations:**
```json
{
  "name": "deploy",
  "gui_operations": ["browser_open"],
  "headless_behavior": "emit_url_in_output"
}
```

**Wrap graphical commands in headless fallback:**
```bash
# Tool wraps GUI launch:
if [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
    echo '{"url": "'"$URL"'", "note": "open this URL in your browser"}'
else
    xdg-open "$URL"
fi
```

**For framework design:**
- Framework MUST detect headless environment on startup and set `framework.headless = true`.
- Commands that declare `gui_operations` MUST implement headless fallbacks; framework raises a registration error if `headless_behavior` is not declared.
- In headless mode, browser/GUI launch attempts MUST be replaced with URL/path emission in the JSON response rather than blocking.

**Requirements that address this:**
- REQ-F-057 (P0) — Headless Environment Detection and GUI Suppression [Tier: F = framework handles]
- REQ-C-024 (P1) — GUI-Launching Commands Declare Headless Behavior [Tier: C = you declare]

---

### §71 — Non-Interactive Installation Absence  [Critical · 2/3]

**Gap:** README documents `git clone … && uv sync` (and `uvx --from git+https://github.com/cloudfall-dev/cloudfall.git cloudfall init`); `uv sync` with `CI=true` exit 0 twice (idempotent, 'Checked 70 packages'). Not 3: no AGENTS.md, and no verify command: `cloudfall --version` exits 2 ('the following arguments are required: command'). Fresh-clone install not re-run (existing checkout; `uvx` path needs network)

**Solutions:**
**For CLI authors:**

Document a fully non-interactive install command in AGENTS.md:

```bash
# In AGENTS.md — exact non-interactive install command agents must use
## Installation
pip install my-cli==2.1.0        # exact version pin
my-cli --version                  # verify install succeeded
```

Design installation to be non-interactive by default:
- Accept license terms implicitly when `--yes` or `CI=true` is detected
- Move post-install configuration to first-use, with `--non-interactive` producing a JSON error rather than a wizard
- Use package manager flags: `pip install --yes`, `apt-get install -y`, `brew install --quiet`
- Document any system dependency with its non-interactive install command

Make installation idempotent — running the install command twice must succeed:

```bash
# Idempotent: second run must exit 0
pip install my-cli==2.1.0   # first run: installs
pip install my-cli==2.1.0   # second run: already satisfied, exit 0
```

Provide a health-check command agents can run after install to confirm the binary is functional:

```bash
my-cli --version             # exits 0, prints version string
my-cli doctor --json         # optional: structured health check
```

**For framework designers:**

Provide a `--non-interactive` flag that suppresses all post-install prompts and fails fast with a JSON error if any required configuration is absent.

**Requirements that address this:**
- REQ-O-044 (P1) — Non-Interactive Install Command Documentation [Tier: O = you opt in]

---

## Already Passing

None  _(score 3/3 — no action needed)_
