# cloudfall — Trace

## §34 — Shell Injection via Agent-Constructed Commands
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall add server acme%2Fwidgets --address 203.0.113.10 --project tmp/eval/proj < /dev/null`; `cloudfall add server-type ../../evil --project tmp/eval/proj < /dev/null`; `cloudfall dashboard build --project tmp/eval/proj --observed tmp/eval/emptyobs --output ../../escape-test < /dev/null`; `cloudfall add server h9 --address '203.0.113.10; touch /tmp/pwned' --project tmp/eval/proj < /dev/null`; `cloudfall add server null --address 203.0.113.10 --project tmp/eval/proj < /dev/null`; `cloudfall import render tmp/eval/nope.yaml --application acme%2Fx --server h1 --project tmp/eval/proj < /dev/null`
**Exit code:** s34-pct-encoded-id=2, s34-traversal-id=2, s34-traversal-output=0, s34-metachar-address=2, s34-null-literal=0, s34-render-name=1
**Score:** 1/3

### s34-pct-encoded-id (exit 2, 0.17s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "invalid_argument", "message": "invalid resource id: 'acme%2Fwidgets'"}, "status": "error"}
```

### s34-traversal-id (exit 2, 0.16s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "invalid_argument", "message": "invalid resource id: '../../evil'"}, "status": "error"}
```

### s34-traversal-output (exit 0, 0.39s, 157 stdout bytes)

**stdout** (first 20 lines):
```
{"dashboard": {"index": "../../escape-test/index.html", "operations": "../../escape-test/operations.json"}, "health": "unknown", "status": "ok", "tasks": 2}
```

**stderr** (first 20 lines):
```

```

### s34-metachar-address (exit 2, 0.26s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "invalid_argument", "message": "invalid hostname: '203.0.113.10; touch /tmp/pwned'"}, "status": "error"}
```

### s34-null-literal (exit 0, 0.38s, 154 stdout bytes)

**stdout** (first 20 lines):
```
{"added": [{"id": "null", "kind": "Server", "path": "servers/null.yaml"}], "project": "/Users/roman/PycharmProjects/Atlas/tmp/eval/proj", "status": "ok"}
```

**stderr** (first 20 lines):
```

```

### s34-render-name (exit 1, 0.15s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
Traceback (most recent call last):
  File "/Users/roman/PycharmProjects/Atlas/.venv/bin/cloudfall", line 10, in <module>
    sys.exit(run())
             ~~~^^
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/cli.py", line 1649, in run
    raise SystemExit(main())
                     ~~~~^^
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/cli.py", line 979, in main
    return _run_import_render(arguments)
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/cli.py", line 1077, in _run_import_render
    application_id=ResourceId.from_boundary(arguments.application),
                   ~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/domain.py", line 59, in from_boundary
    return cls(value)
  File "<string>", line 4, in __init__
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/domain.py", line 48, in __post_init__
    raise ValueError(message)
ValueError: invalid resource id: 'acme%2Fx'
```

## §37 — REPL / Interactive Mode Accidental Triggering
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall < /dev/null`; `cloudfall-mcp --project tmp/eval/proj < /dev/null`
**Exit code:** s37-bare=2, s37-mcp-devnull=0
**Score:** 2/3

### s37-bare (exit 2, 0.14s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
usage: cloudfall [-h]
                 {init,add,config,inventory,audit,operator,backup,secrets,services,dashboard,data,deploy,rollback,restart,health,import,migrate} ...
cloudfall: error: the following arguments are required: command
```

### s37-mcp-devnull (exit 0, 0.53s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```

```

## §42 — Debug / Trace Mode Secret Leakage
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall health crm-backend --project tmp/eval/proj --token sk-live-SECRET-abc123 --debug < /dev/null`; `cloudfall import render-api --project tmp/eval/proj --api-key-file tmp/eval/render.key --api-url http://127.0.0.1:18401/v1 --application acme --server h1 < /dev/null`; `cloudfall health crm-backend --project tmp/eval/proj < /dev/null`
**Exit code:** s42-token-debug-flags=2, s42-render-401=2, s42-ansible-verbosity-env=1
**Score:** 1/3 (revised after §24 abbreviation leak; see `s24-password-flag`, `s24-source-url-abbrev`)

### s42-token-debug-flags (exit 2, 0.21s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
usage: cloudfall [-h]
                 {init,add,config,inventory,audit,operator,backup,secrets,services,dashboard,data,deploy,rollback,restart,health,import,migrate} ...
cloudfall: error: unrecognized arguments: --token sk-live-SECRET-abc123 --debug
```

### s42-render-401 (exit 2, 0.14s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "render_api_unreachable", "message": "Render API unreachable: http://127.0.0.1:18401/v1/postgres?limit=100: HTTP Error 401: Unauthorized"}, "status": "error"}
```

### s42-ansible-verbosity-env (exit 1, 1.07s, 2204 stdout bytes)

**stdout** (first 20 lines):
```
{"action": "health", "component": "crm-backend", "detail": "run health.yml failed: PasswordAuthentication=no -o 'User=\"cloudfall\"' -o ConnectTimeout=10 -o 'ControlPath=\"/Users/roman/.ansible/cp/34b2d4c4d5\"' -o NumberOfPasswordPrompts=1 h2.example.internal '/bin/sh -c '\"'\"'echo ~cloudfall'\"'\"''\n<h2.example.internal> (255, b'', b'ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\\r\\n')\n<h1.example.internal> (255, b'', b'ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\\r\\n')\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\nOrigin: /Users/roman/PycharmProjects/Atlas/engine/ansible/roles/cloudfall_deploy/tasks/health_gate.yml:2:3\n\n1 ---\n2 - name: Gate the component on its declared HTTP health check\n    ^ column 3\n\nfatal: [h2]: UNREACHABLE! => {\n    \"changed\": false,\n    \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\",\n    \"unreachable\": true\n}\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\nOrigin: /Users/roman/PycharmProjects/Atlas/engine/ansible/roles/cloudfall_deploy/tasks/health_gate.yml:2:3\n\n1 ---\n2 - name: Gate the component on its declared HTTP health check\n    ^ column 3\n\nfatal: [h1]: UNREACHABLE! => {\n    \"changed\": false,\n    \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\",\n    \"unreachable\": true\n}\n\nPLAY RECAP *********************************************************************\nh1                         : ok=4    changed=0    unreachable=1    failed=0    skipped=0    rescued=0    ignored=0   \nh2                         : ok=3    changed=0    unreachable=1    failed=0    skipped=0    rescued=0    ignored=0", "healthy": false, "servers": ["h1", "h2"], "status": "unhealthy"}
```

**stderr** (first 20 lines):
```

```

## §43 — Tool Output Result Size Unboundedness
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall inventory show --project tmp/eval/big < /dev/null`; `cloudfall inventory show --project tmp/eval/big --max-output 4096 < /dev/null`
**Exit code:** s43-inventory-600-servers=0, s43-max-output-flag=2
**Score:** 0/3

### s43-inventory-600-servers (exit 0, 0.71s, 181068 stdout bytes)

**stdout** (first 20 lines):
```
{"inventory": {"alertRules": [{"environment": "production", "expr": "pg_up{service=\"postgresql-main\"} == 0", "for": "1m", "id": "postgresql-down", "severity": "critical", "summary": "PostgreSQL postgresql-main is not answering its exporter"}], "applications": [{"approval": "manual", "components": ["crm-backend"], "id": "crm", "linuxUser": "crm"}], "components": [{"application": "crm", "environment": {"FEATURE_SIGNUPS": "true", "REDIS_URL": "redis://127.0.0.1:6379/0"}, "healthCheck": {"attempts": 5, "expectedStatuses": [200], "path": "/health", "port": 8100, "scheme": "http", "timeoutSeconds": 5, "type": "http"}, "id": "crm-backend", "installRoot": "/srv/apps/crm/backend", "repository": {"url": "https://github.com/example/crm-backend.git"}, "retainUntilCleanup": true, "runtime": {"packageManager": "uv", "type": "python", "version": "3.14"}, "servers": ["h1", "h2"], "service": {"command": [".venv/bin/uvicorn", "crm.asgi:application", "--host", "127.0.0.1", "--port", "8100"], "manager": "systemd", "name": "crm-backend"}}], "domains": [{"aliases": ["www.crm.example.test"], "edge": {"mode": "dns-only", "provider": "cloudflare"}, "healthCheck": {"expectedStatuses": [200], "path": "/health", "scheme": "https", "timeoutSeconds": 5}, "id": "crm-site", "origin": {"configurationPath": "/etc/nginx/sites-available/crm-origin.conf", "port": 8100, "scheme": "http", "server": "h1", "serverName": "crm.example.test", "service": "nginx.service"}, "primaryName": "crm.example.test", "proxy": {"
[truncated — 181067 characters total]
```

**stderr** (first 20 lines):
```

```

### s43-max-output-flag (exit 2, 0.2s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
usage: cloudfall [-h]
                 {init,add,config,inventory,audit,operator,backup,secrets,services,dashboard,data,deploy,rollback,restart,health,import,migrate} ...
cloudfall: error: unrecognized arguments: --max-output 4096
```

## §45 — Headless Authentication / OAuth Browser Flow Blocking
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall import render-api --project tmp/eval/proj --api-key-file tmp/eval/none.key --application acme --server h1 < /dev/null`; `cloudfall import render-api --project tmp/eval/proj --api-key-file tmp/eval/render.key --api-url http://127.0.0.1:18401/v1 --application acme --server h1 < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client.crt --gateway-key tmp/eval/tls/client.key < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/absent.crt --gateway-key tmp/eval/tls/absent.key < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client-foreign.crt --gateway-key tmp/eval/tls/client-foreign.key < /dev/null`
**Exit code:** s45-render-key-missing=2, s42-render-401=2, s45-mtls-valid=0, s45-mtls-cert-files-missing=1, s45-mtls-foreign-client=2
**Score:** 1/3

### s45-render-key-missing (exit 2, 0.21s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "render_api_key_missing", "message": "Render API key file does not exist: /Users/roman/PycharmProjects/Atlas/tmp/eval/none.key"}, "status": "error"}
```

### s42-render-401 (exit 2, 0.14s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "render_api_unreachable", "message": "Render API unreachable: http://127.0.0.1:18401/v1/postgres?limit=100: HTTP Error 401: Unauthorized"}, "status": "error"}
```

### s45-mtls-valid (exit 0, 0.3s, 155 stdout bytes)

**stdout** (first 20 lines):
```
{"openProposals": 0, "pass": "alerts", "proposed": [], "skipped": [], "status": "ok"}
{"executed": [], "pass": "autonomy", "status": "ok", "withheld": []}
```

**stderr** (first 20 lines):
```

```

### s45-mtls-cert-files-missing (exit 1, 0.29s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
Traceback (most recent call last):
  File "/Users/roman/PycharmProjects/Atlas/.venv/bin/cloudfall", line 10, in <module>
    sys.exit(run())
             ~~~^^
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/cli.py", line 1649, in run
    raise SystemExit(main())
                     ~~~~^^
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/cli.py", line 982, in main
    return _dispatch(arguments, state, schema_directory)
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/cli.py", line 1128, in _dispatch
    return handler(arguments, state, schema_directory)
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/cli.py", line 1290, in _run_operator_run
    report = operator_run_once(feed, inventory, store)
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/operator.py", line 624, in run_once
    for alert in feed.fetch():
                 ~~~~~~~~~~^^
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/operator.py", line 268, in fetch
    context.load_cert_chain(
    ~~~~~~~~~~~~~~~~~~~~~~~^
        certfile=str(self.certificate_path), keyfile=str(self.key_path)
[truncated — 24 lines total]
```

### s45-mtls-foreign-client (exit 2, 0.27s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"code": "operator_feed_unreachable", "message": "alert feed unreachable: https://127.0.0.1:18443/api/v1/alerts: [SSL: TLSV1_ALERT_UNKNOWN_CA] tlsv1 alert unknown ca (_ssl.c:2713)"}
```

## §50 — Stdin Consumption Deadlock
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall config validate --project tmp/eval/proj < /dev/null`; `cloudfall migrate --project tmp/eval/proj --plan-file tmp/eval/plan-s50.json < /dev/null`; `cloudfall add ssh-key - --owner agent --project tmp/eval/proj < /dev/null`
**Exit code:** s50-validate-stdin-held-open=0, s50-migrate-plan-stdin-held-open=2, s50-add-sshkey-dash=2
**Score:** 2/3

**Observed:** Check run with stdin connected to the stdout of `sleep 30` (open, never written) rather than /dev/null.

### s50-validate-stdin-held-open (exit 0, 0.26s, 212 stdout bytes)

**stdout** (first 20 lines):
```
{"byKind": {"AlertRule": 1, "Application": 1, "Component": 1, "Domain": 1, "LoggingStack": 1, "OperatorPolicy": 1, "Server": 2, "ServerType": 1, "Service": 2, "SshPublicKey": 1}, "resources": 12, "status": "ok"}
```

**stderr** (first 20 lines):
```

```

### s50-migrate-plan-stdin-held-open (exit 2, 0.29s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "migrate_component_unbuilt", "message": "declare a git ref (--build component=ref) or a pinned release (--release component=id) for: crm-backend"}, "status": "error"}
```

### s50-add-sshkey-dash (exit 2, 0.14s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "ssh_key_file_missing", "message": "SSH public key file does not exist: -"}, "status": "error"}
```

## §53 — Credential Expiry Mid-Session
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client-expired.crt --gateway-key tmp/eval/tls/client-expired.key < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18444/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client.crt --gateway-key tmp/eval/tls/client.key < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client-foreign.crt --gateway-key tmp/eval/tls/client-foreign.key < /dev/null`; `cloudfall import render-api --project tmp/eval/proj --api-key-file tmp/eval/render.key --api-url http://127.0.0.1:18401/v1 --application acme --server h1 < /dev/null`
**Exit code:** s53-mtls-client-expired=2, s53-mtls-server-expired=2, s45-mtls-foreign-client=2, s42-render-401=2
**Score:** 1/3

**Observed:** Local mTLS gateway mock (Python `ssl`, `CERT_REQUIRED`) with a test CA; client and server certs generated with `cryptography`, expired variants valid until yesterday.

### s53-mtls-client-expired (exit 2, 0.27s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"code": "operator_feed_unreachable", "message": "alert feed unreachable: https://127.0.0.1:18443/api/v1/alerts: [SSL: SSLV3_ALERT_CERTIFICATE_EXPIRED] ssl/tls alert certificate expired (_ssl.c:2713)"}
```

### s53-mtls-server-expired (exit 2, 0.28s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"code": "operator_feed_unreachable", "message": "alert feed unreachable: https://127.0.0.1:18444/api/v1/alerts: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired (_ssl.c:1082)>"}
```

### s45-mtls-foreign-client (exit 2, 0.27s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"code": "operator_feed_unreachable", "message": "alert feed unreachable: https://127.0.0.1:18443/api/v1/alerts: [SSL: TLSV1_ALERT_UNKNOWN_CA] tlsv1 alert unknown ca (_ssl.c:2713)"}
```

### s42-render-401 (exit 2, 0.14s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "render_api_unreachable", "message": "Render API unreachable: http://127.0.0.1:18401/v1/postgres?limit=100: HTTP Error 401: Unauthorized"}, "status": "error"}
```

## §60 — OS Output Buffer Deadlock
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client.crt --gateway-key tmp/eval/tls/client.key --interval 2 < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client.crt --gateway-key tmp/eval/tls/client.key --interval 2 < /dev/null`; `cloudfall dashboard serve --project tmp/eval/proj --observed tmp/eval/emptyobs --port 0 < /dev/null`
**Exit code:** s60-operator-interval-pipe=-15, s60-operator-interval-pipe-unbuffered=-15, s60-dashboard-serve-pipe=-15
**Score:** 1/3

**Observed:** stdout read non-blocking via select() with arrival timestamps; process stopped with SIGTERM after the window.

### s60-operator-interval-pipe (exit -15, 9s, 0 stdout bytes)

**stdout** (first 20 lines):
```
after SIGTERM at t=9s: received 0 more bytes; exit=-15
--- captured ---
```

**stderr** (first 20 lines):
```

```

### s60-operator-interval-pipe-unbuffered (exit -15, 9s, 775 stdout bytes)

**stdout** (first 20 lines):
```
t=0.3s received 86 bytes, 1 lines
t=0.3s received 69 bytes, 1 lines
t=2.3s received 86 bytes, 1 lines
t=2.3s received 69 bytes, 1 lines
t=4.3s received 86 bytes, 1 lines
t=4.3s received 69 bytes, 1 lines
t=6.3s received 86 bytes, 1 lines
t=6.3s received 69 bytes, 1 lines
t=8.4s received 86 bytes, 1 lines
t=8.4s received 69 bytes, 1 lines
after SIGTERM at t=9s: received 0 more bytes; exit=-15
--- captured ---
{"openProposals": 0, "pass": "alerts", "proposed": [], "skipped": [], "status": "ok"}
{"executed": [], "pass": "autonomy", "status": "ok", "withheld": []}
{"openProposals": 0, "pass": "alerts", "proposed": [], "skipped": [], "status": "ok"}
{"executed": [], "pass": "autonomy", "status": "ok", "withheld": []}
{"openProposals": 0, "pass": "alerts", "proposed": [], "skipped": [], "status": "ok"}
{"executed": [], "pass": "autonomy", "status": "ok", "withheld": []}
{"openProposals": 0, "pass": "alerts", "proposed": [], "skipped": [], "status": "ok"}
{"executed": [], "pass": "autonomy", "status": "ok", "withheld": []}
[truncated — 22 lines total]
```

**stderr** (first 20 lines):
```

```

### s60-dashboard-serve-pipe (exit -15, 4s, 114 stdout bytes)

**stdout** (first 20 lines):
```
t=0.4s received 114 bytes, 1 lines
after SIGTERM at t=4s: received 0 more bytes; exit=-15
--- captured ---
{"dashboard": {"inspectServices": false, "refreshSeconds": 10, "url": "http://127.0.0.1:65098/"}, "status": "ok"}
```

**stderr** (first 20 lines):
```

```

## §61 — Bidirectional Pipe Payload Deadlock
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall inventory show --project tmp/eval/big < /dev/null`
**Exit code:** s61-naive-write-then-read-1MB=124
**Score:** 1/3

**Observed:** Writer thread pushed 64 KiB chunks into stdin; main thread waited 10s before reading. Process killed at t=10.0s.

### s61-naive-write-then-read-1MB (exit 124, 10.01s, 65536 stdout bytes)

**stdout** (first 20 lines):
```
writer blocked=True wrote=65536 error=None at t=10.0s; cli alive=True (stuck writing 181KB stdout into a full pipe) -> deadlock; killing
```

**stderr** (first 20 lines):
```

```

## §62 — $EDITOR and $VISUAL Trap
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall add server h7 --address 203.0.113.7 --project tmp/eval/proj < /dev/null`; `cloudfall init tmp/eval/init-s62 < /dev/null`; `cloudfall config validate . < /dev/null`
**Exit code:** s62-add-server-editor-vim=0, s62-init-editor-empty=0, s62-init-next-step-verbatim=2
**Score:** 2/3

### s62-add-server-editor-vim (exit 0, 0.31s, 150 stdout bytes)

**stdout** (first 20 lines):
```
{"added": [{"id": "h7", "kind": "Server", "path": "servers/h7.yaml"}], "project": "/Users/roman/PycharmProjects/Atlas/tmp/eval/proj", "status": "ok"}
```

**stderr** (first 20 lines):
```

```

### s62-init-editor-empty (exit 0, 0.15s, 774 stdout bytes)

**stdout** (first 20 lines):
```
{"files": ["pyproject.toml", ".gitignore", "README.md", "servers/.gitkeep", "server-types/.gitkeep", "applications/.gitkeep", "components/.gitkeep", "domains/.gitkeep", "ssh-public-keys/.gitkeep", "logging-stacks/.gitkeep", "services/.gitkeep", "alert-rules/.gitkeep", "operator-policies/.gitkeep"], "next": ["cd /Users/roman/PycharmProjects/Atlas/tmp/eval/init-s62 && uv sync", "uv run cloudfall add ssh-key ~/.ssh/id_ed25519.pub --owner <you>", "uv run cloudfall add server h1 --address <ip-or-hostname>", "uv run cloudfall config validate ."], "project": {"directory": "/Users/roman/PycharmProjects/Atlas/tmp/eval/init-s62", "name": "init-s62", "revision": "b9677a68ceac642c42b300a219a2a552dab1f0da", "source": "https://github.com/cloudfall-dev/cloudfall.git"}, "status": "ok"}
```

**stderr** (first 20 lines):
```

```

### s62-init-next-step-verbatim (exit 2, 0.16s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
usage: cloudfall [-h]
                 {init,add,config,inventory,audit,operator,backup,secrets,services,dashboard,data,deploy,rollback,restart,health,import,migrate} ...
cloudfall: error: unrecognized arguments: .
```

## §64 — Headless Display and GUI Launch Blocking
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall dashboard serve --project tmp/eval/proj --observed tmp/eval/emptyobs --port 0 < /dev/null`
**Exit code:** s60-dashboard-serve-pipe=-15
**Score:** 2/3

### s60-dashboard-serve-pipe (exit -15, 4s, 114 stdout bytes)

**stdout** (first 20 lines):
```
t=0.4s received 114 bytes, 1 lines
after SIGTERM at t=4s: received 0 more bytes; exit=-15
--- captured ---
{"dashboard": {"inspectServices": false, "refreshSeconds": 10, "url": "http://127.0.0.1:65098/"}, "status": "ok"}
```

**stderr** (first 20 lines):
```

```

## §71 — Non-Interactive Installation Absence
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `uv sync < /dev/null`; `uv sync < /dev/null`; `cloudfall --version < /dev/null`
**Exit code:** s71-uv-sync-1=0, s71-uv-sync-2=0, s71-version=2
**Score:** 2/3

### s71-uv-sync-1 (exit 0, 0.02s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
Resolved 73 packages in 2ms
Checked 70 packages in 1ms
```

### s71-uv-sync-2 (exit 0, 0.01s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
Resolved 73 packages in 2ms
Checked 70 packages in 1ms
```

### s71-version (exit 2, 0.17s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
usage: cloudfall [-h]
                 {init,add,config,inventory,audit,operator,backup,secrets,services,dashboard,data,deploy,rollback,restart,health,import,migrate} ...
cloudfall: error: the following arguments are required: command
```

## §10 — Interactivity & TTY Requirements
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall migrate --project tmp/eval/proj --build crm-backend=main --plan-file tmp/eval/plan-s10.json < /dev/null`; `cloudfall restart crm-backend --project tmp/eval/proj < /dev/null`; `cloudfall rollback crm-backend --release r1 --project tmp/eval/proj < /dev/null`; `cloudfall backup run postgresql-main --project tmp/eval/proj < /dev/null`; `cloudfall data migrate postgresql-main --database crm --source-url-file tmp/eval/src.url --project tmp/eval/proj < /dev/null`
**Exit code:** s10-migrate-plan=0, s10-restart=1, s10-rollback=1, s10-backup-run=1, s10-data-migrate=1
**Score:** 2/3

### s10-migrate-plan (exit 0, 0.28s, 1545 stdout bytes)

**stdout** (first 20 lines):
```
{"completed": 0, "next": "baseline", "status": "plan", "steps": [{"description": "converge every server to the managed baseline", "id": "baseline", "status": "pending"}, {"description": "converge every declared infrastructure service", "id": "services", "status": "pending"}, {"description": "measure authoritative DNS TTLs and require them at or below 300s before the cutover", "id": "ttl-lower", "status": "pending"}, {"description": "build a release artifact for crm-backend", "id": "build:crm-backend", "status": "pending"}, {"description": "deploy crm-backend behind its health gate", "id": "deploy:crm-backend", "status": "pending"}, {"description": "render every declared domain route over HTTP", "id": "domains-http", "status": "pending"}, {"description": "prove the new origin serves every declared domain before any DNS record changes", "id": "parallel-run", "status": "pending"}, {"description": "verify public DNS points at the declared proxy servers", "id": "dns-verify", "status": "pending"}, {"description": "issue certificates and enable HTTPS routes", "id": "domains-tls", "status": "pending"}, {"description": "collect read-only server evidence", "id": "inspect", "status": "pending"}, {"description": "require a compliant desired-state audit", "id": "audit", "status": "pending"}, {"description": "require every declared route to be healthy", "id": "verify-routes", "status": "pending"}, {"description": "record the pre-switch DNS answers and the explicit 24h rollback recipe", "id
[truncated — 1544 characters total]
```

**stderr** (first 20 lines):
```

```

### s10-restart (exit 1, 0.92s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "lifecycle_execution_failed", "message": "run restart.yml failed: PLAY [Restart one Cloudfall component behind its health check] *****************\n\nTASK [Gathering Facts] *********************************************************\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\n\nTask failed.\n\n<<< caused by >>>\n\nFailed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\n\nfatal: [h1]: UNREACHABLE! => {\"changed\": false, \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\", \"unreachable\": true}\n\nPLAY [Restart one Cloudfall component behind its health check] *****************\n\nTASK [Gathering Facts] *********************************************************\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\n\nTask failed.\n\n<<< caused by >>>\n\nFailed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\n\nfatal: [h2]: UNREACHABLE! => {\"changed\": false, \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal:
[truncated — 1912 characters total]
```

### s10-rollback (exit 1, 0.22s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
Traceback (most recent call last):
  File "/Users/roman/PycharmProjects/Atlas/.venv/bin/cloudfall", line 10, in <module>
    sys.exit(run())
             ~~~^^
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/cli.py", line 1649, in run
    raise SystemExit(main())
                     ~~~~^^
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/cli.py", line 982, in main
    return _dispatch(arguments, state, schema_directory)
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/cli.py", line 1128, in _dispatch
    return handler(arguments, state, schema_directory)
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/cli.py", line 1539, in _run_rollback
    ReleaseId.from_boundary(arguments.release),
    ~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/domain.py", line 713, in from_boundary
    return cls(_required_string(value, "release id"))
  File "<string>", line 4, in __init__
  File "/Users/roman/PycharmProjects/Atlas/sdk/src/cloudfall/domain.py", line 708, in __post_init__
    raise ValueError(message)
ValueError: invalid release id: 'r1'
```

### s10-backup-run (exit 1, 0.83s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "lifecycle_execution_failed", "message": "run backup.yml failed: PLAY [Run one declared backup operation with a controller receipt] *************\n\nTASK [Gathering Facts] *********************************************************\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\n\nTask failed.\n\n<<< caused by >>>\n\nFailed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\n\nfatal: [h1]: UNREACHABLE! => {\"changed\": false, \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\", \"unreachable\": true}\n\nPLAY [Run one declared backup operation with a controller receipt] *************\n\nTASK [Gathering Facts] *********************************************************\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\n\nTask failed.\n\n<<< caused by >>>\n\nFailed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\n\nfatal: [h2]: UNREACHABLE! => {\"changed\": false, \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: 
[truncated — 1911 characters total]
```

### s10-data-migrate (exit 1, 0.82s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "lifecycle_execution_failed", "message": "run data.yml failed: PLAY [Migrate external data into declared Cloudfall services] ******************\n\nTASK [Gathering Facts] *********************************************************\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\n\nTask failed.\n\n<<< caused by >>>\n\nFailed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\n\nfatal: [h1]: UNREACHABLE! => {\"changed\": false, \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\", \"unreachable\": true}\n\nPLAY [Migrate external data into declared Cloudfall services] ******************\n\nTASK [Gathering Facts] *********************************************************\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\n\nTask failed.\n\n<<< caused by >>>\n\nFailed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\n\nfatal: [h2]: UNREACHABLE! => {\"changed\": false, \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: no
[truncated — 1909 characters total]
```

## §11 — Timeouts & Hanging Processes
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall health crm-backend --project tmp/eval/blackhole --timeout 2 < /dev/null`; `cloudfall health crm-backend --project tmp/eval/blackhole < /dev/null`; `cloudfall import render-api --project tmp/eval/blackhole --api-key-file tmp/eval/render.key --api-url http://203.0.113.10/v1 --application acme --server h1 < /dev/null`
**Exit code:** s11-timeout-flag=2, s11-health-blackhole=1, s11-render-api-blackhole=2
**Score:** 1/3

### s11-timeout-flag (exit 2, 0.17s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
usage: cloudfall [-h]
                 {init,add,config,inventory,audit,operator,backup,secrets,services,dashboard,data,deploy,rollback,restart,health,import,migrate} ...
cloudfall: error: unrecognized arguments: --timeout 2
```

### s11-health-blackhole (exit 1, 10.89s, 2204 stdout bytes)

**stdout** (first 20 lines):
```
{"action": "health", "component": "crm-backend", "detail": "run health.yml failed: ******************\nok: [h1] => {\n    \"changed\": false,\n    \"msg\": \"All assertions passed\"\n}\n\nTASK [Probe the selected component on its declared servers] ********************\nincluded: cloudfall_deploy for h2, h1 => (item=crm-backend)\n\nTASK [cloudfall_deploy : Validating arguments against arg spec 'health' - Probe one component's declared health check] ***\nok: [h1]\nok: [h2]\n\nTASK [cloudfall_deploy : Probe the component's declared health check] **********\nincluded: /Users/roman/PycharmProjects/Atlas/engine/ansible/roles/cloudfall_deploy/tasks/health_gate.yml for h1, h2\n\nTASK [cloudfall_deploy : Gate the component on its declared HTTP health check] ***\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: connect to host 203.0.113.11 port 22: Operation timed out\nOrigin: /Users/roman/PycharmProjects/Atlas/engine/ansible/roles/cloudfall_deploy/tasks/health_gate.yml:2:3\n\n1 ---\n2 - name: Gate the component on its declared HTTP health check\n    ^ column 3\n\nfatal: [h2]: UNREACHABLE! => {\"changed\": false, \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: connect to host 203.0.113.11 port 22: Operation timed out\", \"unreachable\": true}\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: connect to host 203.0.113.10 port 22: Operation timed out\nOrigin: /Users/roman/PycharmProjects/Atlas/engine/ansible/roles/cloudfall_deploy/tas
[truncated — 2203 characters total]
```

**stderr** (first 20 lines):
```

```

### s11-render-api-blackhole (exit 2, 30.19s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "render_api_unreachable", "message": "Render API unreachable: http://203.0.113.10/v1/postgres?limit=100: <urlopen error timed out>"}, "status": "error"}
```

## §12 — Idempotency & Safe Retries
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall add server h8 --address 203.0.113.8 --project tmp/eval/proj < /dev/null`; `cloudfall add server h8 --address 203.0.113.8 --project tmp/eval/proj < /dev/null`; `cloudfall add ssh-key tmp/eval/agent_key.pub --owner agent --project tmp/eval/proj < /dev/null`; `cloudfall add ssh-key tmp/eval/agent_key.pub --owner agent --project tmp/eval/proj < /dev/null`; `cloudfall add server h8 --address 203.0.113.8 --project tmp/eval/proj --idempotency-key k1 < /dev/null`; `cloudfall migrate --project tmp/eval/proj --build crm-backend=main --plan-file tmp/eval/plan-s10.json < /dev/null`; `cloudfall init tmp/eval/init-s62 < /dev/null`
**Exit code:** s12-add-server-1=0, s12-add-server-2-same=2, s12-add-sshkey-1=0, s12-add-sshkey-2-same=2, s12-idempotency-key=2, s12-migrate-plan-2=0, s12-init-2-same=2
**Score:** 1/3

### s12-add-server-1 (exit 0, 0.32s, 150 stdout bytes)

**stdout** (first 20 lines):
```
{"added": [{"id": "h8", "kind": "Server", "path": "servers/h8.yaml"}], "project": "/Users/roman/PycharmProjects/Atlas/tmp/eval/proj", "status": "ok"}
```

**stderr** (first 20 lines):
```

```

### s12-add-server-2-same (exit 2, 0.19s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "resource_exists", "message": "Server/h8 already exists: /Users/roman/PycharmProjects/Atlas/tmp/eval/proj/servers/h8.yaml"}, "status": "error"}
```

### s12-add-sshkey-1 (exit 0, 0.27s, 170 stdout bytes)

**stdout** (first 20 lines):
```
{"added": [{"id": "agent", "kind": "SshPublicKey", "path": "ssh-public-keys/agent.yaml"}], "project": "/Users/roman/PycharmProjects/Atlas/tmp/eval/proj", "status": "ok"}
```

**stderr** (first 20 lines):
```

```

### s12-add-sshkey-2-same (exit 2, 0.19s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "resource_exists", "message": "SshPublicKey/agent already exists: /Users/roman/PycharmProjects/Atlas/tmp/eval/proj/ssh-public-keys/agent.yaml"}, "status": "error"}
```

### s12-idempotency-key (exit 2, 0.13s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
usage: cloudfall [-h]
                 {init,add,config,inventory,audit,operator,backup,secrets,services,dashboard,data,deploy,rollback,restart,health,import,migrate} ...
cloudfall: error: unrecognized arguments: --idempotency-key k1
```

### s12-migrate-plan-2 (exit 0, 0.28s, 1545 stdout bytes)

**stdout** (first 20 lines):
```
{"completed": 0, "next": "baseline", "status": "plan", "steps": [{"description": "converge every server to the managed baseline", "id": "baseline", "status": "pending"}, {"description": "converge every declared infrastructure service", "id": "services", "status": "pending"}, {"description": "measure authoritative DNS TTLs and require them at or below 300s before the cutover", "id": "ttl-lower", "status": "pending"}, {"description": "build a release artifact for crm-backend", "id": "build:crm-backend", "status": "pending"}, {"description": "deploy crm-backend behind its health gate", "id": "deploy:crm-backend", "status": "pending"}, {"description": "render every declared domain route over HTTP", "id": "domains-http", "status": "pending"}, {"description": "prove the new origin serves every declared domain before any DNS record changes", "id": "parallel-run", "status": "pending"}, {"description": "verify public DNS points at the declared proxy servers", "id": "dns-verify", "status": "pending"}, {"description": "issue certificates and enable HTTPS routes", "id": "domains-tls", "status": "pending"}, {"description": "collect read-only server evidence", "id": "inspect", "status": "pending"}, {"description": "require a compliant desired-state audit", "id": "audit", "status": "pending"}, {"description": "require every declared route to be healthy", "id": "verify-routes", "status": "pending"}, {"description": "record the pre-switch DNS answers and the explicit 24h rollback recipe", "id
[truncated — 1544 characters total]
```

**stderr** (first 20 lines):
```

```

### s12-init-2-same (exit 2, 0.17s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "project_revision_uncommitted", "message": "the source checkout /Users/roman/PycharmProjects/Atlas has uncommitted changes, so HEAD b9677a68ceac642c42b300a219a2a552dab1f0da is not the code that is running; commit them or pass --rev"}, "status": "error"}
```

## §13 — Partial Failure & Atomicity
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall migrate --project tmp/eval/proj --build crm-backend=main --plan-file tmp/eval/plan-s13.json --yes < /dev/null`; `cloudfall migrate --project tmp/eval/proj --build crm-backend=main --plan-file tmp/eval/plan-s13.json < /dev/null`
**Exit code:** s13-migrate-yes-fails-step1=1, s13-migrate-plan-after-failure=0
**Score:** 2/3

### s13-migrate-yes-fails-step1 (exit 1, 0.88s, 3460 stdout bytes)

**stdout** (first 20 lines):
```
{"completed": 0, "error": {"code": "lifecycle_execution_failed", "message": "run baseline.yml failed: PLAY [Converge Cloudfall servers to the managed baseline] **********************\n\nTASK [Gathering Facts] *********************************************************\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\n\nTask failed.\n\n<<< caused by >>>\n\nFailed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\n\nfatal: [h1]: UNREACHABLE! => {\"changed\": false, \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\", \"unreachable\": true}\n\nPLAY [Converge Cloudfall servers to the managed baseline] **********************\n\nTASK [Gathering Facts] *********************************************************\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\n\nTask failed.\n\n<<< caused by >>>\n\nFailed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\n\nfatal: [h2]: UNREACHABLE! => {\"changed\": false, \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h2.
[truncated — 3459 characters total]
```

**stderr** (first 20 lines):
```

```

### s13-migrate-plan-after-failure (exit 0, 0.28s, 1545 stdout bytes)

**stdout** (first 20 lines):
```
{"completed": 0, "next": "baseline", "status": "plan", "steps": [{"description": "converge every server to the managed baseline", "id": "baseline", "status": "pending"}, {"description": "converge every declared infrastructure service", "id": "services", "status": "pending"}, {"description": "measure authoritative DNS TTLs and require them at or below 300s before the cutover", "id": "ttl-lower", "status": "pending"}, {"description": "build a release artifact for crm-backend", "id": "build:crm-backend", "status": "pending"}, {"description": "deploy crm-backend behind its health gate", "id": "deploy:crm-backend", "status": "pending"}, {"description": "render every declared domain route over HTTP", "id": "domains-http", "status": "pending"}, {"description": "prove the new origin serves every declared domain before any DNS record changes", "id": "parallel-run", "status": "pending"}, {"description": "verify public DNS points at the declared proxy servers", "id": "dns-verify", "status": "pending"}, {"description": "issue certificates and enable HTTPS routes", "id": "domains-tls", "status": "pending"}, {"description": "collect read-only server evidence", "id": "inspect", "status": "pending"}, {"description": "require a compliant desired-state audit", "id": "audit", "status": "pending"}, {"description": "require every declared route to be healthy", "id": "verify-routes", "status": "pending"}, {"description": "record the pre-switch DNS answers and the explicit 24h rollback recipe", "id
[truncated — 1544 characters total]
```

**stderr** (first 20 lines):
```

```

## §23 — Side Effects & Destructive Operations
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall restart crm-backend --project tmp/eval/proj --dry-run < /dev/null`; `cloudfall deploy crm-backend --release r1 --project tmp/eval/proj --dry-run < /dev/null`; `cloudfall migrate --project tmp/eval/proj --build crm-backend=main --plan-file tmp/eval/plan-s23.json < /dev/null`; `cloudfall restart crm-backend --project tmp/eval/proj < /dev/null`
**Exit code:** s23-restart-dry-run=2, s23-deploy-dry-run=2, s23-migrate-plan-fresh=0, s10-restart=1
**Score:** 1/3

### s23-restart-dry-run (exit 2, 0.18s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
usage: cloudfall [-h]
                 {init,add,config,inventory,audit,operator,backup,secrets,services,dashboard,data,deploy,rollback,restart,health,import,migrate} ...
cloudfall: error: unrecognized arguments: --dry-run
```

### s23-deploy-dry-run (exit 2, 0.14s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
usage: cloudfall [-h]
                 {init,add,config,inventory,audit,operator,backup,secrets,services,dashboard,data,deploy,rollback,restart,health,import,migrate} ...
cloudfall: error: unrecognized arguments: --dry-run
```

### s23-migrate-plan-fresh (exit 0, 0.29s, 1545 stdout bytes)

**stdout** (first 20 lines):
```
{"completed": 0, "next": "baseline", "status": "plan", "steps": [{"description": "converge every server to the managed baseline", "id": "baseline", "status": "pending"}, {"description": "converge every declared infrastructure service", "id": "services", "status": "pending"}, {"description": "measure authoritative DNS TTLs and require them at or below 300s before the cutover", "id": "ttl-lower", "status": "pending"}, {"description": "build a release artifact for crm-backend", "id": "build:crm-backend", "status": "pending"}, {"description": "deploy crm-backend behind its health gate", "id": "deploy:crm-backend", "status": "pending"}, {"description": "render every declared domain route over HTTP", "id": "domains-http", "status": "pending"}, {"description": "prove the new origin serves every declared domain before any DNS record changes", "id": "parallel-run", "status": "pending"}, {"description": "verify public DNS points at the declared proxy servers", "id": "dns-verify", "status": "pending"}, {"description": "issue certificates and enable HTTPS routes", "id": "domains-tls", "status": "pending"}, {"description": "collect read-only server evidence", "id": "inspect", "status": "pending"}, {"description": "require a compliant desired-state audit", "id": "audit", "status": "pending"}, {"description": "require every declared route to be healthy", "id": "verify-routes", "status": "pending"}, {"description": "record the pre-switch DNS answers and the explicit 24h rollback recipe", "id
[truncated — 1544 characters total]
```

**stderr** (first 20 lines):
```

```

### s10-restart (exit 1, 0.92s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "lifecycle_execution_failed", "message": "run restart.yml failed: PLAY [Restart one Cloudfall component behind its health check] *****************\n\nTASK [Gathering Facts] *********************************************************\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\n\nTask failed.\n\n<<< caused by >>>\n\nFailed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\n\nfatal: [h1]: UNREACHABLE! => {\"changed\": false, \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\", \"unreachable\": true}\n\nPLAY [Restart one Cloudfall component behind its health check] *****************\n\nTASK [Gathering Facts] *********************************************************\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\n\nTask failed.\n\n<<< caused by >>>\n\nFailed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\n\nfatal: [h2]: UNREACHABLE! => {\"changed\": false, \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal:
[truncated — 1912 characters total]
```

## §24 — Authentication & Secret Handling
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall import render-api --project tmp/eval/proj --api-key rnd_x --application acme --server h1 < /dev/null`; `cloudfall import render-api --project tmp/eval/proj --api-key-file tmp/eval/render.key --api-url http://127.0.0.1:18401/v1 --application acme --server h1 < /dev/null`; `cloudfall secrets render crm-backend --project tmp/eval/proj --secrets-dir tmp/eval/secrets < /dev/null`; `cloudfall operator run --project tmp/eval/proj --gateway-url https://127.0.0.1:18443/api/v1/alerts --gateway-ca tmp/eval/tls/ca.crt --gateway-cert tmp/eval/tls/client-foreign.crt --gateway-key tmp/eval/tls/client-foreign.key < /dev/null`
**Exit code:** s24-password-flag=2, s42-render-401=2, s24-secrets-render-no-sops=2, s45-mtls-foreign-client=2
**Score:** 1/3

### s24-password-flag (exit 2, 0.14s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "render_api_key_missing", "message": "Render API key file does not exist: rnd_x"}, "status": "error"}
```

### s42-render-401 (exit 2, 0.14s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "render_api_unreachable", "message": "Render API unreachable: http://127.0.0.1:18401/v1/postgres?limit=100: HTTP Error 401: Unauthorized"}, "status": "error"}
```

### s24-secrets-render-no-sops (exit 2, 0.31s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "secrets_source_missing", "message": "secret source does not exist: /Users/roman/PycharmProjects/Atlas/tmp/eval/secrets/production/shared.env (referenced as scope=platform path=/shared)"}, "status": "error"}
```

### s45-mtls-foreign-client (exit 2, 0.27s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"code": "operator_feed_unreachable", "message": "alert feed unreachable: https://127.0.0.1:18443/api/v1/alerts: [SSL: TLSV1_ALERT_UNKNOWN_CA] tlsv1 alert unknown ca (_ssl.c:2713)"}
```

### s24-source-url-abbrev (exit 2, 0.33s, 0 stdout bytes)

`cloudfall data migrate postgresql-main --database crm --source-url postgresql://u:hunter2@db/x --project tmp/eval/proj < /dev/null`

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
{"error": {"code": "lifecycle_source_url_missing", "message": "source URL file does not exist or is empty: postgresql:/u:hunter2@db/x"}, "status": "error"}
```

## §25 — Prompt Injection via Output
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall import render tmp/eval/inj/render.yaml --application demo --server h1 --output tmp/eval/inj/out --env-dir tmp/eval/inj/env --project tmp/eval/proj < /dev/null`; `/.venv/bin/cloudfall inventory show --project tmp/eval/proj < /dev/null`; `cloudfall health crm-backend --project tmp/eval/proj < /dev/null`
**Exit code:** s25-import-render-injection=0, s25-inventory-show-injected-description=0, s1-network-dns-fail=1
**Score:** 1/3

**Observed:** CLI JSON, generated YAML, env files, and `IMPORT-REPORT.md` grepped for `IGNORE`, `SYSTEM`, `Assistant`: one hit, in `env/demo.env` (the declared env value, by design).

### s25-import-render-injection (exit 0, 0.23s, 1505 stdout bytes)

**stdout** (first 20 lines):
```
{"application": "demo", "components": ["demo"], "domains": [], "environmentFiles": ["/Users/roman/PycharmProjects/Atlas/tmp/eval/inj/env/demo.env"], "gaps": [{"category": "assumption", "detail": "assigned listen port 8100; PORT is written to the component environment file and the application must honor it", "subject": "demo"}, {"category": "assumption", "detail": "buildCommand is not executed; Cloudfall materializes locked dependencies at deploy time instead", "subject": "demo"}, {"category": "action", "detail": "build releases from the declared branch: cloudfall-engine artifact build ... --ref main", "subject": "demo"}, {"category": "assumption", "detail": "no PYTHON_VERSION declared; assuming 3.14", "subject": "demo"}, {"category": "action", "detail": "Cloudfall deploys python components with uv sync --frozen; the repository needs pyproject.toml and uv.lock", "subject": "demo"}, {"category": "unsupported", "detail": "cron services are not modeled yet; recreate the schedule ('0 3 * * *') as a systemd timer manually", "subject": "nightly"}, {"category": "action", "detail": "review /Users/roman/PycharmProjects/Atlas/tmp/eval/inj/env/demo.env and pass it to deployment with --env-file", "subject": "demo"}], "report": "/Users/roman/PycharmProjects/Atlas/tmp/eval/inj/out/IMPORT-REPORT.md", "services": [], "status": "ok", "written": ["/Users/roman/PycharmProjects/Atlas/tmp/eval/inj/out/applications/demo.yaml", "/Users/roman/PycharmProjects/Atlas/tmp/eval/inj/out/components/demo.yam
[truncated — 1504 characters total]
```

**stderr** (first 20 lines):
```

```

### s25-inventory-show-injected-description (exit 0, 0.3s, 6989 stdout bytes)

**stdout** (first 20 lines):
```
servers[id=h6] = {"address": "203.0.113.6", "environment": "production", "hostname": "h6", "id": "h6", "labels": {}, "lifecycle": "active", "serverType": "debian-application", "ssh": {"port": 22, "user": "root"}}
```

**stderr** (first 20 lines):
```

```

### s1-network-dns-fail (exit 1, 1.07s, 2192 stdout bytes)

**stdout** (first 20 lines):
```
{"action": "health", "component": "crm-backend", "detail": "run health.yml failed: red servers] ********************\nincluded: cloudfall_deploy for h1, h2 => (item=crm-backend)\n\nTASK [cloudfall_deploy : Validating arguments against arg spec 'health' - Probe one component's declared health check] ***\nok: [h1]\nok: [h2]\n\nTASK [cloudfall_deploy : Probe the component's declared health check] **********\nincluded: /Users/roman/PycharmProjects/Atlas/engine/ansible/roles/cloudfall_deploy/tasks/health_gate.yml for h1, h2\n\nTASK [cloudfall_deploy : Gate the component on its declared HTTP health check] ***\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\nOrigin: /Users/roman/PycharmProjects/Atlas/engine/ansible/roles/cloudfall_deploy/tasks/health_gate.yml:2:3\n\n1 ---\n2 - name: Gate the component on its declared HTTP health check\n    ^ column 3\n\nfatal: [h1]: UNREACHABLE! => {\"changed\": false, \"msg\": \"Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h1.example.internal: nodename nor servname provided, or not known\", \"unreachable\": true}\n[ERROR]: Task failed: Failed to connect to the host via ssh: ssh: Could not resolve hostname h2.example.internal: nodename nor servname provided, or not known\nOrigin: /Users/roman/PycharmProjects/Atlas/engine/ansible/roles/cloudfall_deploy/tasks/health_gate.yml:2:3\n\n1 ---\n2 - name: Gate
[truncated — 2191 characters total]
```

**stderr** (first 20 lines):
```

```

## §74 — Credential Scope Declaration Absence
**Date:** 2026-09-14
**CLI version:** 0.1.0
**Check command:** `cloudfall --schema < /dev/null`; `cloudfall check-permissions --for deploy < /dev/null`
**Exit code:** s74-schema=2, s74-check-permissions=2
**Score:** 0/3

### s74-schema (exit 2, 0.16s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
usage: cloudfall [-h]
                 {init,add,config,inventory,audit,operator,backup,secrets,services,dashboard,data,deploy,rollback,restart,health,import,migrate} ...
cloudfall: error: the following arguments are required: command
```

### s74-check-permissions (exit 2, 0.13s, 0 stdout bytes)

**stdout** (first 20 lines):
```

```

**stderr** (first 20 lines):
```
usage: cloudfall [-h]
                 {init,add,config,inventory,audit,operator,backup,secrets,services,dashboard,data,deploy,rollback,restart,health,import,migrate} ...
cloudfall: error: argument command: invalid choice: 'check-permissions' (choose from 'init', 'add', 'config', 'inventory', 'audit', 'operator', 'backup', 'secrets', 'services', 'dashboard', 'data', 'deploy', 'rollback', 'restart', 'health', 'import', 'migrate')
```

## §1 — Exit Codes & Status Signaling
**Date:** 2026-09-26 (third refresh, at 578cd4a)
**CLI version:** 0.5.1 at 578cd4a (output schema 1.0)
**Check command:** `uv run cloudfall <cmd> --project $PWD/config/examples </dev/null`, exit code read with the last JSON line: missing args, nonexistent resources, a conflict, unresolvable hosts, Render API without network access, `init` under a read-only dir, `audit` without observations; then `cloudfall --help` and `cloudfall health --help` (stdout discarded) and `--schema`
**Exit code:** per case below
**Score:** 1/3

**stdout** (first 20 lines):
```
missing required arg (deploy)                  exit=2 status=error code=invalid_argument
nonexistent component (health ghost)           exit=2 status=error code=lifecycle_component_missing
nonexistent proposal (operator show)           exit=2 status=error code=operator_proposal_missing
conflict: add existing server                  exit=2 status=error code=resource_exists
network: unresolvable hosts (restart --yes)    exit=1 status=error code=lifecycle_execution_failed
network: render API unreachable                exit=2 status=error code=render_api_unreachable
permission: init under read-only dir           exit=1 status=error code=path_not_writable
audit unknown (no observations)                exit=3 status=unknown code=None
exit_code in any JSON body: never
cloudfall health --help: 4 exit-code rows
exit_codes in --schema: False
```

**stderr** (first 20 lines):
```
exit codes:
  0  the command ran and its result is positive (ok: true)
  1  the command ran and its result is negative: drift, unhealthy, a failed
     step, or a path it had to write turned out read-only partway
  2  nothing ran: invalid input or usage, a resource not found or already
     there, or another unmet precondition
  3  the result is unknown: observations missing or stale (audit), or a
     migration paused before its next step (migrate)
```

## §2 — Output Format & Parseability
**Date:** 2026-09-24 (fifth refresh, after c14d6d4)
**CLI version:** 0.5.1 at c14d6d4 (output schema 1.0)
**Check command:** `CLOUDFALL_PROJECT=$PWD/tmp/eval-s2/p uv run cloudfall <cmd> --output json 2>/dev/null </dev/null` for `why` (0 answers), `operator list` (1 proposal), `inventory show` (3 servers), and `operator show ghost --output json` (stderr kept); `changelog` without the flag through a pipe, with `CI=true`, and under a pty (`pty.fork`); the 30-command sweep; `--schema` flag coverage
**Exit code:** 0 (results), 2 (`operator show ghost`)
**Score:** 3/3

**stdout** (first 20 lines):
```
0 items (why)            | keys: ['data', 'error', 'meta', 'ok', 'status', 'warnings'] | ok: True | error: None | data type: dict | meta: ['duration_ms', 'request_id', 'schema_version', 'tool_version'] | types: str int
   why answers: 0
1 item (operator list)   | keys: [same six] | ok: True | error: None | data type: dict | meta: [same four] | types: str int
   proposals: 1
N items (inventory show) | keys: [same six] | ok: True | error: None | data type: dict | meta: [same four] | types: str int
   servers: 3
pipe:    ['data', 'error', 'meta', 'ok', 'status', 'warnings']
CI=true: ['data', 'error', 'meta', 'ok', 'status', 'warnings']
pty (isatty): {"data": {"entries": [{"added": ["ok", "status", "data", "error", ...  keys: [same six]
  (a first attempt with `script -q /dev/null` failed to parse: `script` echoed `^D\b\b` from the /dev/null stdin into the capture; harness artifact, rerun with pty.fork)
sweep: 29 OK
migrate --yes --restart   exit=1 ['out:error'] ['error on stdout']
34 commands; without --output: []
```

**stderr** (first 20 lines):
```
error (stderr) | keys: ['data', 'error', 'meta', 'ok', 'status', 'warnings'] | ok: False | error: {'code': 'operator_proposal_missing', 'message': 'proposal does not exist: tmp/operator/proposals/ghost.json'} | data type: NoneType | meta: ['duration_ms', 'request_id', 'schema_version', 'tool_version'] | types: str int
```

## §22 — Schema Versioning & Output Stability
**Date:** 2026-09-23 (third refresh)
**CLI version:** 0.5.1 (`cloudfall --version`)
**Check command:** with `CLOUDFALL_PROJECT=<repo>/tmp/ev22` (fresh `init`), each via `timeout 30 uv run cloudfall <args> </dev/null`: level 2 over `--version`, `add server h1 --address 192.0.2.1`, `config validate`, `inventory show`, `operator list`, `why`, `changelog`, `operator show ghost`, `deploy`, `--schema-version 9 config validate`; level 3: `--schema-version 1|2 config validate`, `changelog`, `changelog --since 1.0`, `--schema` (twice) and `--print-schema` compared with `cmp`, tier count over every `output_schema`, and `jsonschema` validation of real `config validate`/`inventory show`/`operator list`/`changelog` output against the declared schemas
**Exit code:** 0 for results, `--schema`, `--schema-version 1`; 2 for `operator show ghost`, `deploy`, `--schema-version 2|9`
**Score:** 3/3

**stdout** (first 20 lines):
```
== level 2: meta + warnings on every document
  --version            exit=0 meta={'schema_version': '1.0', 'tool_version': '0.5.1'} warnings=[]
  config validate      exit=0 meta={'schema_version': '1.0', 'tool_version': '0.5.1'} warnings=[]
  operator show ghost  exit=2 meta={'schema_version': '1.0', 'tool_version': '0.5.1'} warnings=[]
  (all 10 documents: same meta, warnings=[])
== level 3a: --schema-version pin
{"byKind": {"Server": 1, "ServerType": 1}, "meta": {"schema_version": "1.0", ...}, "resources": 2, ...
== level 3b: changelog JSON
{"entries": [{"added": ["meta.schema_version", "meta.tool_version", "warnings"], "breaking": false, "changed": [], "date": "2026-09-23", "removed": [], "version": "1.0"}], ..., "schemaVersions": {"current": "1.0", "minimum": "1.0"}, "status": "ok", "warnings": []}
{"entries": [], ...}   ← --since 1.0
== level 3c: stability tiers in schema
  --print-schema identical
  stable across calls
  commands: 34 | key tiers: {'stable': 255, 'experimental': 8} | keys without tier: 0 | etag: sha256:be0fee49bcdae718
  experimental keys in: ['changelog', 'why']
  config validate: {"additionalProperties": false, "properties": {"byKind": {"x-stability": "stable"}, "meta": {"x-stability": "stable"}, "resources": {"x-stability": "stable"}, "status": {"x-stability": "stable"}, "warnings": {"x-stability": "stable"}}, "required": [...], "type": "object"}
  top-level keys: ['commands', 'etag', 'global_flags', 'meta', 'status', 'warnings'] | global flags: ['schema', 'schema-version', 'version']
== cross-check: real output vs declared schema
  config validate  errors=[]   inventory show  errors=[]   operator list  errors=[]   changelog  errors=[]
```

**stderr** (first 20 lines):
```
{"error": {"code": "invalid_argument", "message": "cloudfall: argument --schema-version: schema version 2 is not supported; this build writes 1 to 1"}, "meta": {"schema_version": "1.0", "tool_version": "0.5.1"}, "status": "error", "warnings": []}
```

## §3 — Stderr vs Stdout Discipline
**Date:** 2026-09-26 (second refresh, after 40163ca)
**CLI version:** 0.5.1 at 40163ca (output schema 1.0)
**Check command:** a script running `uv run cloudfall <cmd>` for 13 paths against `--project $PWD/config/examples` (stdin `/dev/null`, stdout and stderr captured apart, every stdout line parsed as JSON), once plain and once with `--quiet` appended; then `dashboard serve --port 0 --quiet` with `GET /missing`, and `inventory show --warnings-as-errors`
**Exit code:** per path below
**Score:** 3/3

**stdout** (first 20 lines):
```
=== plain                                  exit  out  non_json_out  err
observe --output-dir tmp/eval-s3/obs          1    1  0             0
restart crm-backend --yes                     1    0  0             1 json
health crm-backend                            1    1  0             0
config validate / inventory show              0    1  0             0
audit (observations missing) / health ghost   2    0  0             1 json
inventory shoe / (no args)                    2    0  0             1 json
--help                                        0    0  0             45 prose
inventory show --help                         0    0  0             19 prose
migrate --yes --restart / --output text       2    0  0             1 json
=== with --quiet: same exits and stdout; stderr 0 lines on all 13 paths
dashboard serve --quiet: GET /missing 404 | stderr=0B
--warnings-as-errors accepted: ok True warnings []
```

**stderr** (first 20 lines):
```
(plain) one JSON error line per failure; help text on --help; nothing on success
(--quiet) empty on every path
```

