# cloudfall — Issues Report

**Generated:** 2026-09-14
**CLI version:** 0.1.0
**Scope:** critical
**Findings in scope:** 22 failure modes

---

## Observed Bugs  _(from evaluation notes)_

These were witnessed directly when running checks against this CLI.

### §34 — output paths escape the project directory

**Discovered during:** §34 evaluation — 2026-09-14
**Symptom:** `cloudfall dashboard build --output ../../escape-test` (run with `--project tmp/eval/proj`) exit 0 and wrote `index.html` and `operations.json` to `tmp/escape-test`, two levels above the project. README says relative paths resolve against the project; nothing confines them to it. Same pattern applies to every `--output`, `--receipts`, `--inventory-file` flag.
**Impact:** Exit 0 with files written two levels above the project; an agent-constructed `--output ../..` path overwrites anything the user can write
**Trigger:** `cloudfall add server acme%2Fwidgets --address 203.0.113.10 --project tmp/eval/proj < /dev/null`; `cloudfall add server-type ../../evil --project tmp/eval/proj < /dev/null`; `cloudfall dashboard build --project tmp/eval/proj --observed tmp/eval/emptyobs --output ../../escape-test < /dev/null`; `cloudfall add server h9 --address '203.0.113.10; touch /tmp/pwned' --project tmp/eval/proj < /dev/null`; `cloudfall add server null --address 203.0.113.10 --project tmp/eval/proj < /dev/null`; `cloudfall import render tmp/eval/nope.yaml --application acme%2Fx --server h1 --project tmp/eval/proj < /dev/null`

---

### §34/§1 — `import render` crashes on an invalid id

**Discovered during:** §34 evaluation — 2026-09-14
**Symptom:** `cloudfall import render x.yaml --application acme%2Fx --server h1` raises an uncaught `ValueError: invalid resource id` traceback (exit 1). `_run_import_render` catches `RenderImportError`/`ConfigValidationError` but not the `ValueError` from `ResourceId.from_boundary`, unlike `add`, which returns `invalid_argument` JSON.
**Impact:** Exit 1 with a Python traceback on stderr instead of `invalid_argument` JSON; agents branching on exit 1 misread a typo as an execution failure
**Trigger:** `cloudfall add server acme%2Fwidgets --address 203.0.113.10 --project tmp/eval/proj < /dev/null`; `cloudfall add server-type ../../evil --project tmp/eval/proj < /dev/null`; `cloudfall dashboard build --project tmp/eval/proj --observed tmp/eval/emptyobs --output ../../escape-test < /dev/null`; `cloudfall add server h9 --address '203.0.113.10; touch /tmp/pwned' --project tmp/eval/proj < /dev/null`; `cloudfall add server null --address 203.0.113.10 --project tmp/eval/proj < /dev/null`; `cloudfall import render tmp/eval/nope.yaml --application acme%2Fx --server h1 --project tmp/eval/proj < /dev/null`

---

### §34 — literal `null` accepted as a resource id

**Discovered during:** §34 evaluation — 2026-09-14
**Symptom:** `cloudfall add server null --address 203.0.113.10` exit 0 and wrote `servers/null.yaml`. The id pattern accepts null-like literals an agent emits when a value is missing.
**Impact:** Exit 0 and `servers/null.yaml` written; a missing value from an agent becomes a real declared server
**Trigger:** `cloudfall add server acme%2Fwidgets --address 203.0.113.10 --project tmp/eval/proj < /dev/null`; `cloudfall add server-type ../../evil --project tmp/eval/proj < /dev/null`; `cloudfall dashboard build --project tmp/eval/proj --observed tmp/eval/emptyobs --output ../../escape-test < /dev/null`; `cloudfall add server h9 --address '203.0.113.10; touch /tmp/pwned' --project tmp/eval/proj < /dev/null`; `cloudfall add server null --address 203.0.113.10 --project tmp/eval/proj < /dev/null`; `cloudfall import render tmp/eval/nope.yaml --application acme%2Fx --server h1 --project tmp/eval/proj < /dev/null`

---

### §42 — inherited `ANSIBLE_*` env changes engine output

**Discovered during:** §42 evaluation — 2026-09-14
**Symptom:** Lifecycle steps run with `{**os.environ, ...}`, so `ANSIBLE_VERBOSITY=4` in the agent's environment turns on `-vvvv` for the engine; the raw SSH command lines (user, control path, options) are then embedded in the JSON `detail` field of `cloudfall health`. Nothing strips or pins `ANSIBLE_*` variables, so a debug env var set elsewhere silently widens what lands in agent context.
**Impact:** Exit 1 with verbose SSH command lines inside JSON `detail`; agent context grows and connection details leak into logs
**Trigger:** `cloudfall health crm-backend --project tmp/eval/proj --token sk-live-SECRET-abc123 --debug < /dev/null`; `cloudfall import render-api --project tmp/eval/proj --api-key-file tmp/eval/render.key --api-url http://127.0.0.1:18401/v1 --application acme --server h1 < /dev/null`; `cloudfall health crm-backend --project tmp/eval/proj < /dev/null`

---

### §42 — argparse echoes unrecognized flag values

**Discovered during:** §42 evaluation — 2026-09-14
**Symptom:** `cloudfall health crm-backend --token sk-live-SECRET-abc123 --debug` exits 2 with `unrecognized arguments: --token sk-live-SECRET-abc123 --debug` on stderr. Harmless for real inputs (no secret flags exist) but a hallucinated secret flag is copied into the error an agent logs.
**Impact:** Exit 2; a hallucinated secret flag value is printed verbatim to stderr
**Trigger:** `cloudfall health crm-backend --project tmp/eval/proj --token sk-live-SECRET-abc123 --debug < /dev/null`; `cloudfall import render-api --project tmp/eval/proj --api-key-file tmp/eval/render.key --api-url http://127.0.0.1:18401/v1 --application acme --server h1 < /dev/null`; `cloudfall health crm-backend --project tmp/eval/proj < /dev/null`

---

### §45/§1 — missing gateway cert file crashes `operator run`

**Discovered during:** §45 evaluation — 2026-09-14
**Symptom:** `cloudfall operator run --gateway-cert tmp/eval/tls/absent.crt --gateway-key tmp/eval/tls/absent.key ...` raises an uncaught `FileNotFoundError` from `ssl.SSLContext.load_cert_chain` (traceback, exit 1). `GatewayAlertFeed.fetch` builds the SSL context outside its `try`, and the CLI only catches `OperatorError`. Exit 1 also collides with the 'proposal not verified' result code.
**Impact:** Exit 1 with a traceback (same code as 'proposal not verified'); no JSON error to branch on
**Trigger:** `cloudfall import render-api --project tmp/eval/proj --api-key-file tmp/eval/none.key --application acme --server h1 < /dev/null`; `cloudfall import render-api --project tmp/eval/proj --api-key-file tmp/eval/render.key --api-url http://127.0.0.1:18401/v1 --application acme --server h1 < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client.crt --gateway-key tmp/eval/tls/client.key < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/absent.crt --gateway-key tmp/eval/tls/absent.key < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client-foreign.crt --gateway-key tmp/eval/tls/client-foreign.key < /dev/null`

---

### §45/§24 — authentication failures reported as `unreachable`

**Discovered during:** §45 evaluation — 2026-09-14
**Symptom:** Render API HTTP 401 → `render_api_unreachable` ('HTTP Error 401: Unauthorized'); gateway rejecting the client cert (`TLSV1_ALERT_UNKNOWN_CA`) → `operator_feed_unreachable`. `urllib.error.HTTPError` and `ssl.SSLError` are both `OSError`, caught by one `except OSError`. An agent cannot tell 'fix the credential' from 'retry later' without regex on the message.
**Impact:** Exit 2 `*_unreachable` for HTTP 401 and rejected client certs; agents retry a credential problem as a network blip
**Trigger:** `cloudfall import render-api --project tmp/eval/proj --api-key-file tmp/eval/none.key --application acme --server h1 < /dev/null`; `cloudfall import render-api --project tmp/eval/proj --api-key-file tmp/eval/render.key --api-url http://127.0.0.1:18401/v1 --application acme --server h1 < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client.crt --gateway-key tmp/eval/tls/client.key < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/absent.crt --gateway-key tmp/eval/tls/absent.key < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client-foreign.crt --gateway-key tmp/eval/tls/client-foreign.key < /dev/null`

---

### §53 — no pre-flight expiry check on gateway credentials

**Discovered during:** §53 evaluation — 2026-09-14
**Symptom:** `operator run --interval N` loads the client cert on every pass but never reads its `notAfter`; the first signal is a failed pass after expiry, reported as `operator_feed_unreachable`. For a long-running watcher, a warning in the pass JSON N days before expiry would let an agent rotate the cert before alerts go dark.
**Impact:** Alerts go dark at expiry; the first signal is a failed pass reported as `operator_feed_unreachable`
**Trigger:** `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client-expired.crt --gateway-key tmp/eval/tls/client-expired.key < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18444/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client.crt --gateway-key tmp/eval/tls/client.key < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client-foreign.crt --gateway-key tmp/eval/tls/client-foreign.key < /dev/null`; `cloudfall import render-api --project tmp/eval/proj --api-key-file tmp/eval/render.key --api-url http://127.0.0.1:18401/v1 --application acme --server h1 < /dev/null`

---

### §60 — `operator run --interval` output is block-buffered and lost on termination

**Discovered during:** §60 evaluation — 2026-09-14
**Symptom:** Under a pipe (systemd journal capture, agent subprocess), `cloudfall operator run --interval 2` emitted nothing for 9 seconds across 5 passes, and SIGTERM discarded the buffer: 0 bytes ever reached the reader. `_write_json` in `sdk/src/cloudfall/cli.py` writes to `sys.stdout` without `flush()`; only `dashboard serve` flushes. Fix: flush in `_write_json` (or `sys.stdout.reconfigure(line_buffering=True)` in `run()`). Workaround: `PYTHONUNBUFFERED=1`.

Impact on the documented deployment: the systemd unit in `docs/operator-guide.md` runs `operator run --interval 30` under journald without `PYTHONUNBUFFERED=1`. At ~155 bytes per quiet pass, the 8 KiB buffer fills only after ~50 passes (~25 minutes), so `journalctl -u` lags by that much, and a restart drops the unflushed passes.
**Impact:** 0 bytes read over 9s and 0 bytes after SIGTERM; a watching agent or journald sees nothing and loses unflushed passes on restart
**Trigger:** `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client.crt --gateway-key tmp/eval/tls/client.key --interval 2 < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client.crt --gateway-key tmp/eval/tls/client.key --interval 2 < /dev/null`; `cloudfall dashboard serve --project tmp/eval/proj --observed tmp/eval/emptyobs --port 0 < /dev/null`

---

### §73 — `init` suggests a command that no longer parses

**Discovered during:** §62 evaluation — 2026-09-14
**Symptom:** `cloudfall init <dir>` returns `next: [..., "uv run cloudfall config validate ."]`. Run verbatim inside the new project, it exits 2 with `unrecognized arguments: .` because the positional project argument was replaced by `--project`/`CLOUDFALL_PROJECT`/cwd. Agents copy `next` hints literally; the hint should be `uv run cloudfall config validate`. Check the scaffolded project README for the same drift.
**Impact:** Exit 2 `unrecognized arguments: .` when the agent follows the `next` hint verbatim
**Trigger:** `cloudfall add server h7 --address 203.0.113.7 --project tmp/eval/proj < /dev/null`; `cloudfall init tmp/eval/init-s62 < /dev/null`; `cloudfall config validate . < /dev/null`

---

### §71/§20 — no `--version` flag

**Discovered during:** §71 evaluation — 2026-09-14
**Symptom:** `cloudfall --version` exits 2 with an argparse usage error; `cloudfall-engine` and `cloudfall-mcp` behave the same. Agents and install scripts cannot verify which Cloudfall build (package version or pinned git commit) is active. `init` already resolves the installed revision (`resolve_installed_revision`), so `--version` could print `0.1.0 (b9677a6)`.
**Impact:** Exit 2; install verification and version pinning checks cannot run
**Trigger:** `uv sync < /dev/null`; `uv sync < /dev/null`; `cloudfall --version < /dev/null`

---

### §1 — invalid ids crash lifecycle commands with tracebacks

**Discovered during:** §10 evaluation — 2026-09-14
**Symptom:** `cloudfall rollback crm-backend --release r1` → uncaught `ValueError: invalid release id: 'r1'`; `cloudfall health 'Bad ID!'` → `ValueError: invalid resource id`. Both exit 1 with a Python traceback on stderr. `deploy`, `rollback`, `restart`, `health`, `backup`, `data migrate`, `secrets render`, `operator show|approve` call `ResourceId`/`ReleaseId.from_boundary` outside any `except ValueError`, while `add` and `init` wrap it as `invalid_argument` exit 2. Exit 1 is also what `health` returns for 'unhealthy', so an agent reads a typo as an outage.
**Impact:** Exit 1 traceback; `health` also uses exit 1 for 'unhealthy', so a malformed id reads as an outage
**Trigger:** `cloudfall migrate --project tmp/eval/proj --build crm-backend=main --plan-file tmp/eval/plan-s10.json < /dev/null`; `cloudfall restart crm-backend --project tmp/eval/proj < /dev/null`; `cloudfall rollback crm-backend --release r1 --project tmp/eval/proj < /dev/null`; `cloudfall backup run postgresql-main --project tmp/eval/proj < /dev/null`; `cloudfall data migrate postgresql-main --database crm --source-url-file tmp/eval/src.url --project tmp/eval/proj < /dev/null`

---

### §11/§1 — unreachable hosts reported as an unhealthy component

**Discovered during:** §11 evaluation — 2026-09-14
**Symptom:** `cloudfall health crm-backend` against hosts that time out on SSH (203.0.113.10/11) and against unresolvable hostnames returns exit 1 with `"healthy": false, "status": "unhealthy"`, the same result as a component failing its HTTP check. The Ansible `UNREACHABLE!` signal is only inside the `detail` log tail. `operator` autonomy and agents that restart on 'unhealthy' would act on a network partition as if the app were down.
**Impact:** Exit 1 `status: unhealthy` after a 10.89s SSH timeout; restart-on-unhealthy logic acts on a network partition
**Trigger:** `cloudfall health crm-backend --project tmp/eval/blackhole --timeout 2 < /dev/null`; `cloudfall health crm-backend --project tmp/eval/blackhole < /dev/null`; `cloudfall import render-api --project tmp/eval/blackhole --api-key-file tmp/eval/render.key --api-url http://203.0.113.10/v1 --application acme --server h1 < /dev/null`

---

### §12 — identical `add` retry is an error, not a noop

**Discovered during:** §12 evaluation — 2026-09-14
**Symptom:** `cloudfall add server h8 --address 203.0.113.8` run twice: first exit 0 `added`, second exit 2 `resource_exists`, even though the requested state already holds. An agent retrying after a timeout cannot tell 'already done' from 'conflicting resource with the same id' without reading the YAML. Comparing the existing document with the requested one and returning `status: ok` with `unchanged` would make the retry safe.
**Impact:** Exit 2 `resource_exists` on a retry of a succeeded call; the agent cannot tell 'already done' from a conflict
**Trigger:** `cloudfall add server h8 --address 203.0.113.8 --project tmp/eval/proj < /dev/null`; `cloudfall add server h8 --address 203.0.113.8 --project tmp/eval/proj < /dev/null`; `cloudfall add ssh-key tmp/eval/agent_key.pub --owner agent --project tmp/eval/proj < /dev/null`; `cloudfall add ssh-key tmp/eval/agent_key.pub --owner agent --project tmp/eval/proj < /dev/null`; `cloudfall add server h8 --address 203.0.113.8 --project tmp/eval/proj --idempotency-key k1 < /dev/null`; `cloudfall migrate --project tmp/eval/proj --build crm-backend=main --plan-file tmp/eval/plan-s10.json < /dev/null`; `cloudfall init tmp/eval/init-s62 < /dev/null`

---

### §13/§3 — failed migrate step keeps `status: pending` and lands on stdout

**Discovered during:** §13 evaluation — 2026-09-14
**Symptom:** After `cloudfall migrate --yes` fails at `baseline`, the step list still shows `{"id": "baseline", "status": "pending"}`; the failure is only in top-level `step`/`error`. The failure payload is written to stdout with exit 1, while `MigrateError` (e.g. `migrate_component_unbuilt`) goes to stderr with exit 2, so an agent must read both streams to find the error object.
**Impact:** Exit 1 with the error object on stdout while other migrate errors use stderr; the step list never shows `failed`
**Trigger:** `cloudfall migrate --project tmp/eval/proj --build crm-backend=main --plan-file tmp/eval/plan-s13.json --yes < /dev/null`; `cloudfall migrate --project tmp/eval/proj --build crm-backend=main --plan-file tmp/eval/plan-s13.json < /dev/null`

---

### §23 — CLI lacks the confirmation gate the MCP server enforces

**Discovered during:** §23 evaluation — 2026-09-14
**Symptom:** `cloudfall-mcp` requires a preview call then `confirm=true` for every server-changing tool, but the same operations through the CLI (`cloudfall restart crm-backend`, `deploy`, `rollback`, `data migrate`) run on first invocation with no `--dry-run` or `--yes`. An agent given shell access instead of MCP bypasses the handshake. `migrate`'s plan/`--yes` split shows the pattern already exists in the CLI.
**Impact:** `restart`, `deploy`, `rollback`, `data migrate` act on first call (observed: `restart` reached Ansible in 0.92s); shell-based agents skip the MCP two-step handshake
**Trigger:** `cloudfall restart crm-backend --project tmp/eval/proj --dry-run < /dev/null`; `cloudfall deploy crm-backend --release r1 --project tmp/eval/proj --dry-run < /dev/null`; `cloudfall migrate --project tmp/eval/proj --build crm-backend=main --plan-file tmp/eval/plan-s23.json < /dev/null`; `cloudfall restart crm-backend --project tmp/eval/proj < /dev/null`

---

### §24 — `--api-key <secret>` is silently accepted and echoed

**Discovered during:** §24 evaluation — 2026-09-14
**Symptom:** argparse `allow_abbrev` is on (default) for every parser. `cloudfall import render-api --api-key rnd_x ...` is parsed as `--api-key-file rnd_x`, and the resulting error prints the value: `{"code": "render_api_key_missing", "message": "Render API key file does not exist: rnd_x"}`. An agent that guesses the natural flag name leaks the real key into stderr, logs, and its own context. The same prefix matching applies to `--source-url` → `--source-url-file` on `data migrate`. Fix: `ArgumentParser(allow_abbrev=False)` on every parser and omit file contents-looking values from the not-found message.
**Impact:** Exit 2 with the secret (Render key, or DB URL with password) printed in the JSON error message
**Trigger:** `cloudfall import render-api --project tmp/eval/proj --api-key rnd_x --application acme --server h1 < /dev/null`; `cloudfall import render-api --project tmp/eval/proj --api-key-file tmp/eval/render.key --api-url http://127.0.0.1:18401/v1 --application acme --server h1 < /dev/null`; `cloudfall secrets render crm-backend --project tmp/eval/proj --secrets-dir tmp/eval/secrets < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client-foreign.crt --gateway-key tmp/eval/tls/client-foreign.key < /dev/null`

---

### §74 — operator and collectors share one mTLS trust domain

**Discovered during:** §74 evaluation — 2026-09-14
**Symptom:** `docs/operator-guide.md`: the operator's client certificate is 'signed by the same CA that validates collector certificates (`collectors.clientTls`); the operator is just another mTLS client of the gateway'. A collector cert on any application host is therefore accepted by the read-only alerts route the operator uses, and nothing in the config distinguishes an operator identity from a log shipper. Confirmed in `engine/ansible/roles/cloudfall_logging_backend/templates/nginx-loki-gateway.conf.j2`: `ssl_verify_client on` with a single `ssl_client_certificate` CA and no per-location client check, so the operator cert can also push to `/loki/api/v1/push` and `/api/v1/write`, and collector certs can read `/api/v1/alerts`. Not exploited live.
**Impact:** Any collector certificate can read the alerts route and the operator certificate can push logs/metrics; no per-identity scope
**Trigger:** `cloudfall --schema < /dev/null`; `cloudfall check-permissions --for deploy < /dev/null`

---

### §2 — three different error shapes

**Discovered during:** §2 evaluation — 2026-09-14
**Symptom:** `{"status": "error", "error": {"code", "message"}}` (validation, lifecycle, secrets, render import, authoring) vs `{"code", "message"}` with no envelope (`OperatorError.as_dict()`, e.g. `operator show ghost`) vs a failure object on stdout (`migrate --yes` step failure), plus prose for argparse errors and uncaught exceptions. An agent needs four parsers to read one failure. `_operator_exit` should wrap errors in the shared envelope.
**Impact:** Four parsers needed for one failure path (envelope JSON, bare `{code, message}`, stdout failure object, prose)
**Trigger:** `cloudfall config validate --project tmp/eval/proj < /dev/null`; `cloudfall operator list --project tmp/eval/proj < /dev/null`; `cloudfall config validate --project tmp/eval/proj --output json < /dev/null`; `cloudfall services status --project tmp/eval/proj --observed tmp/eval/emptyobs < /dev/null`; `cloudfall operator show ghost --project tmp/eval/proj < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client.crt --gateway-key tmp/eval/tls/client.key < /dev/null`

---

---

## Failure-Mode Gaps  _(score 0–2, sorted: score asc, severity desc; ?/3 entries listed last)_

These are not confirmed bugs but verified gaps — the CLI does not meet the bar for reliable agent use. `?/3` entries (check timed out) are included at the end with **What fails:** check timed out — behavior unknown.

### §43 — Tool Output Result Size Unboundedness  [Critical · score 0/3]

**What fails:** `inventory show` on a 600-server project returned 181,068 bytes as one JSON line, exit 0, no `meta.truncated`/`total_bytes`; `--max-output` rejected (exit 2); no limit flag on any command. Only internal caps: engine log tail in `detail` ≤ 2000 chars, proposal description ≤ 500 chars. `cloudfall-mcp` tools return the same unbounded payloads
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: Critical · Time: High
**Workaround exists:** Partial

---

### §74 — Credential Scope Declaration Absence  [Critical · score 0/3]

**What fails:** `cloudfall --schema` and `check-permissions` do not exist (both exit 2). No per-command credential map: guides list prerequisites only ('root SSH access' in render-migration-guide; operator 'client certificate … signed by the same CA that validates collector certificates'). Nothing states that `config validate`/`inventory show`/`services status` need no credentials, `import render-api` needs only read access, or that `add server` defaults `--ssh-user root`. The operator reuses the collector CA, and the gateway nginx template has no per-location client check, so any collector cert can read `/api/v1/alerts` and the operator cert can push logs/metrics
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: Low · Time: Medium
**Workaround exists:** Partial

---

### §1 — Exit Codes & Status Signaling  [Critical · score 1/3]

**What fails:** Semantic codes exist but overload: 0 ok; 1 = audit drift, `health` unhealthy, engine execution failed, `migrate` step failed, `operator approve` unverified, and any uncaught traceback; 2 = argparse usage, config/validation error, not-found (`lifecycle_component_missing`, `operator_proposal_missing`, `observation_directory_missing`); 3 = audit unknown / `migrate` paused. Documented only for `audit` (README) and not in `--help`; no `exit_code` in JSON bodies. Observed: missing args 2 (prose), unknown component 2 (JSON), invalid id `Bad ID!` 1 (traceback), unresolvable hosts 1 (`status: unhealthy`)
**Frequency:** Very Common
**Token/time cost when it triggers:** Token Spend: High · Time: High
**Workaround exists:** Partial

---

### §2 — Output Format & Parseability  [Critical · score 1/3]

**What fails:** JSON is the only mode and stdout never carried prose in any run (`sort_keys`, one object per line). But no `--output json` (rejected, exit 2) and no consistent envelope: success objects use `status: ok` with command-specific keys (`byKind`, `inventory`, `proposals`); `operator show` prints the raw proposal document with no `status`; `health` returns `status: unhealthy` on stdout; `operator run` emits several JSON documents (NDJSON, one per pass); errors are `{status: error, error: {...}}` on stderr except `operator_*` errors (bare `{code, message}`) and `migrate` step failures (stdout). argparse usage errors and tracebacks are prose on stderr. Zero-item result is valid: `{"proposals": [], "status": "ok"}`
**Frequency:** Very Common
**Token/time cost when it triggers:** Token Spend: High · Time: Medium
**Workaround exists:** Partial

---

### §11 — Timeouts & Hanging Processes  [Critical · score 1/3]

**What fails:** Never hung, but no caller-set timeout: `--timeout 2` rejected (exit 2); only `operator approve --verify-timeout`. Built-in bounds: SSH `ConnectTimeout=10`, HTTP 30s (Render) / 10s (gateway), 3600s per engine step. `health` on blackholed 203.0.113.10/11 took 10.89s → exit 1 `"status": "unhealthy"` (the timeout is reported as an unhealthy app); `import render-api` took 30.19s → `render_api_unreachable` ('urlopen error timed out') exit 2. No `TIMEOUT` code, no exit 10, no `completed_steps`
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: High · Time: Critical
**Workaround exists:** Partial

---

### §12 — Idempotency & Safe Retries  [Critical · score 1/3]

**What fails:** No `--idempotency-key` (rejected, exit 2) and no `effect` field. Retries do not duplicate: a second identical `add server h8` / `add ssh-key` → `resource_exists` exit 2 (not a noop exit 0, so a retry after a lost response looks like a failure); `migrate --yes` persists progress in `--plan-file` and a re-run resumes at the recorded `next` step (plan mode writes nothing); engine playbooks are convergent Ansible. `init` on an existing dir reported `project_revision_uncommitted` before checking emptiness. `deploy` of the same release twice not exercised (needs a live host)
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: High · Time: High
**Workaround exists:** Partial

---

### §23 — Side Effects & Destructive Operations  [Critical · score 1/3]

**What fails:** `migrate` is plan-by-default: without `--yes` it prints the 13-step plan, exit 0, and writes no plan file. `deploy`, `rollback`, `restart`, `backup run\|verify`, `data migrate`, `secrets render`, `operator approve` execute immediately: `--dry-run` rejected (exit 2), no confirmation, no `danger_level`, no `effect`. Guards live elsewhere: `data migrate` refuses non-empty targets, deploys are health-gated with receipts, and `cloudfall-mcp` marks mutating tools destructive and requires a `confirm=true` second call
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: Medium · Time: High
**Workaround exists:** Partial

---

### §24 — Authentication & Secret Handling  [Critical · score 1/3]

**What fails:** Designed file-only: Render key via `--api-key-file`, DB URL via `--source-url-file`, sops/age env files, mTLS cert paths; invalid Render key (401) error names the URL, not the key (0 occurrences of the key in all captured output); `secrets render` outputs key names only. But argparse prefix matching accepts `--api-key rnd_x` as `--api-key-file` and the error echoes it: `Render API key file does not exist: rnd_x`. Auth failures exit 2 (same as bad input), no dedicated auth code (8/10)
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: Medium · Time: Medium
**Workaround exists:** Partial

---

### §25 — Prompt Injection via Output  [Critical · score 1/3]

**What fails:** No `trusted`/`_content_type` tagging anywhere, but external text is mostly kept out of output by design: importing a blueprint whose `buildCommand`, env value, and cron `startCommand` carry injection strings returned JSON with none of them (gaps are Cloudfall-authored; the env value lands only in `env/demo.env`); an injected `--description` never appears in `inventory show`. Untagged channel remains: `detail` / `error.message` embed the last 2000 chars of Ansible output, which includes remote-host stderr and module messages, beside `status`/`healthy` in the same object. Rated by analogy to 1 ('protection inconsistent')
**Frequency:** Situational
**Token/time cost when it triggers:** Token Spend: High · Time: High
**Workaround exists:** Partial

---

### §34 — Shell Injection via Agent-Constructed Commands  [Critical · score 1/3]

**What fails:** No shell=True anywhere (exec-array subprocess); resource ids reject `%2F`, `../`, `;` with JSON `invalid_argument` exit 2, but no `suggestion`; `--output ../../escape-test` silently wrote outside the project; id `null` accepted; `import render --application acme%2Fx` crashes with a traceback
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: High · Time: High
**Workaround exists:** Partial

---

### §42 — Debug / Trace Mode Secret Leakage  [Critical · score 1/3]

**What fails:** No `--debug`/`--trace`/`--token` flags; secrets enter only by file (`--api-key-file`, `--source-url-file`, sops dir), so none reach argv or the process table; Render 401 error names the URL, not the key; data-migration URL is staged with `no_log` and read via `$(cat)` on the host. No `[REDACTED]` layer: argparse echoes a hallucinated `--token sk-live-SECRET-abc123` verbatim, and `ANSIBLE_VERBOSITY=4` leaks into the engine subprocess and its verbose log tail lands in the JSON `detail`. Lowered to 1 after §24: argparse prefix-matches `--api-key`/`--source-url` to the `*-file` flags and the not-found error echoes the secret (`postgresql:/u:hunter2@db/x`)
**Frequency:** Situational
**Token/time cost when it triggers:** Token Spend: Low · Time: Low
**Workaround exists:** Partial

---

### §45 — Headless Authentication / OAuth Browser Flow Blocking  [Critical · score 1/3]

**What fails:** No browser/OAuth flow anywhere; credentials are files (Render API key, mTLS cert/key, SSH keys via Ansible with `PasswordAuthentication=no`). Missing Render key → `render_api_key_missing` JSON exit 2 in 0.21s; Render 401 and a rejected mTLS client cert both surface as `*_unreachable`, no `AUTH_REQUIRED`, no `auth_methods`; missing `--gateway-cert` file → uncaught `FileNotFoundError` traceback exit 1. SSH auth prompts not exercised (no local sshd)
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: High · Time: Critical
**Workaround exists:** Partial

---

### §53 — Credential Expiry Mid-Session  [Critical · score 1/3]

**What fails:** Expired mTLS client cert → `operator_feed_unreachable` with 'ssl/tls alert certificate expired' in message text; expired gateway server cert → same code, 'certificate has expired'; unknown-CA client cert and a network outage also map to `operator_feed_unreachable` exit 2. Expiry appears only in prose: no `CREDENTIALS_EXPIRED`, no `expired_at`, no `reauth_command`. Render API key expiry is indistinguishable from any 401 (`render_api_unreachable`)
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: High · Time: High
**Workaround exists:** Partial

---

### §60 — OS Output Buffer Deadlock  [Critical · score 1/3]

**What fails:** `operator run --interval 2` piped: 0 bytes received in 9s (5 passes, 10 JSON lines written) and 0 bytes after SIGTERM, so all output is lost; with `PYTHONUNBUFFERED=1` each pass arrives on time (t=0.3, 2.3, 4.3, 6.3, 8.4s). `dashboard serve` flushes its URL line explicitly (t=0.4s). `_write_json` never flushes; no heartbeat on `migrate --yes`, `deploy`, `backup run` (single JSON at the end, engine steps up to 3600s each)
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: High · Time: Critical
**Workaround exists:** Partial

---

### §61 — Bidirectional Pipe Payload Deadlock  [Critical · score 1/3]

**What fails:** Reproduced: agent writes 1 MiB to stdin before reading stdout while `inventory show` (600-server project) writes 181 KB to stdout; after 10s the writer is stuck at 65,536 bytes and the CLI is stuck on a full stdout pipe, a deadlock until killed. Cloudfall never reads stdin and never detects or rejects piped input (no `STDIN_TOO_LARGE`); every input already comes from file flags (`--source-url-file`, `--api-key-file`, blueprint path), so agents have no reason to pipe
**Frequency:** Situational
**Token/time cost when it triggers:** Token Spend: High · Time: Critical
**Workaround exists:** Partial

---

### §10 — Interactivity & TTY Requirements  [Critical · score 2/3]

**What fails:** No prompts, pager, or editor on any path; with stdin=/dev/null and a 5s cap every mutating command returned: `migrate` plan 0.28s exit 0 (execution gated by `--yes`, not a prompt), `restart`/`backup run`/`data migrate` 0.8–0.9s exit 1 with JSON `lifecycle_execution_failed` (unresolvable hosts). Ansible runs with `PasswordAuthentication=no`. Side finding: `rollback --release r1` crashes with a `ValueError` traceback. Held at 2, not 3: Ansible's SSH host-key confirmation for an unknown reachable host (read from /dev/tty, not stdin) is neither disabled nor exercised here
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: High · Time: Critical
**Workaround exists:** Partial

---

### §13 — Partial Failure & Atomicity  [Critical · score 2/3]

**What fails:** `migrate --yes` with unreachable hosts: exit 1 (distinct from 0 ok, 3 paused), stdout JSON `status: error`, `step: baseline`, `next: baseline`, `completed: 0`, full 13-step manifest, engine error in `error.message`; progress persists in `--plan-file`, re-running resumes at `baseline`. Gaps: failed step stays `status: pending` (not `failed`), no `partial: true`, no `--rollback-on-failure` (rollback is a manual `rollback-window` recipe); this failure JSON goes to stdout while other `migrate` errors go to stderr. Only step 1 failure exercised; a mid-plan failure after completed steps needs live hosts. Single-lifecycle commands (`deploy`, `backup run`) report no step progress
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: High · Time: High
**Workaround exists:** Partial

---

### §37 — REPL / Interactive Mode Accidental Triggering  [Critical · score 2/3]

**What fails:** No REPL or shell mode exists (no `input()`, `cmd.Cmd`, `readline`, `sys.stdin` in sdk/engine sources); bare `cloudfall` exits 2 in 0.14s with argparse usage (prose, not JSON); `cloudfall-mcp` stdio server exits 0 in 0.53s on EOF stdin. Capped at 2: nothing declares `requires_interactive` because no schema exists
**Frequency:** Situational
**Token/time cost when it triggers:** Token Spend: High · Time: Critical
**Workaround exists:** Partial

---

### §50 — Stdin Consumption Deadlock  [Critical · score 2/3]

**What fails:** No `cloudfall` command reads stdin; with stdin held open by a never-closing pipe `config validate` exits 0 in 0.26s and `migrate` (plan mode) exits 2 in 0.29s with JSON. `add ssh-key -` treats `-` as a path → `ssh_key_file_missing` JSON exit 2, so there is no stdin convention to hang on. Only `cloudfall-mcp` consumes stdin, by design. Capped at 2: no schema declares stdin behaviour
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: High · Time: Critical
**Workaround exists:** Partial

---

### §62 — $EDITOR and $VISUAL Trap  [Critical · score 2/3]

**What fails:** No command opens an editor (no `EDITOR`/`VISUAL` reads in sources); every authoring path is flag-driven: `add server` with `EDITOR=vim VISUAL=vim` exit 0 in 0.31s, `init` with empty `EDITOR` exit 0 in 0.15s, no git commit is made. Capped at 2: no schema declares `requires_editor`. Side finding: `init`'s `next` hint `uv run cloudfall config validate .` fails verbatim (exit 2, 'unrecognized arguments: .')
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: High · Time: Critical
**Workaround exists:** Partial

---

### §64 — Headless Display and GUI Launch Blocking  [Critical · score 2/3]

**What fails:** No command launches a browser or GUI (no `webbrowser`/`xdg-open` in sources); `dashboard serve --port 0` with `DISPLAY=` emitted `{"dashboard": {"url": "http://127.0.0.1:65098/", ...}, "status": "ok"}` at t=0.4s (explicit flush), then served until SIGTERM; `dashboard build` returns file paths in JSON. Capped at 2: no schema declares `headless_behavior`
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: High · Time: Critical
**Workaround exists:** Partial

---

### §71 — Non-Interactive Installation Absence  [Critical · score 2/3]

**What fails:** README documents `git clone … && uv sync` (and `uvx --from git+https://github.com/cloudfall-dev/cloudfall.git cloudfall init`); `uv sync` with `CI=true` exit 0 twice (idempotent, 'Checked 70 packages'). Not 3: no AGENTS.md, and no verify command: `cloudfall --version` exits 2 ('the following arguments are required: command'). Fresh-clone install not re-run (existing checkout; `uvx` path needs network)
**Frequency:** Common
**Token/time cost when it triggers:** Token Spend: Low · Time: Critical
**Workaround exists:** Partial

---

---

## Passing  _(score 3/3 — safe to use without special handling)_

None — no failure mode in scope scored 3/3

---

## Risk Summary

| Category | Count | §N list |
|---|---|---|
| Observed bugs | 19 | §1, §2, §3, §11, §12, §13, §20, §23, §24, §34, §42, §45, §53, §60, §71, §73, §74 |
| Score 0 — complete failure | 2 | §43, §74 |
| Score 1 — major gap | 13 | §34, §42, §45, §53, §60, §61, §11, §12, §23, §24, §25, §1, §2 |
| Score 2 — minor gap | 7 | §37, §50, §62, §64, §71, §10, §13 |
| Score 3 — passing | 0 | — |
| Indeterminate (?/3 — timed out) | 0 | — |

**Highest-risk combination:** §24 + §23: an agent driving the shell (not MCP) can leak a secret by guessing `--api-key`/`--source-url`, and nothing stops its next `restart`/`deploy`/`data migrate` from acting on the first call.
