# Secrets guide

Cloudfall separates secrets into references and values. References —
`{scope, environment, path}` — live in declared state and are safe
anywhere. Values live **sops-encrypted with age** in a private repository
and are decrypted only on the controller, at render time, into one `0600`
environment file per component. A value never appears in state, receipts,
logs, output envelopes, or the agent surface.

This is the one place the record is deliberately incomplete. Cloudfall
keeps evidence for every decision an agent makes about your fleet, and
that record must be safe to read, share and keep for years. So it holds
key names and content hashes, enough to prove which secrets a release
carried and whether the installed file matches, and never a value. An
agent that runs the fleet gets the same view: it can render, deploy and
audit secrets without ever being able to read one.

## First: prefer secrets that don't exist

The best credential story is elimination. PostgreSQL uses peer
authentication over the local socket — the app connects as its application's
Linux user with `postgresql:///<db>?host=/var/run/postgresql`, no username
or password anywhere. Redis binds loopback-only with protected mode. Use
the secrets pipeline only for values that must exist: external API keys,
SMTP passwords, third-party tokens.

## One-time setup

Install [sops](https://github.com/getsops/sops) and
[age](https://github.com/FiloSottile/age), then create a key and tell sops
to use it:

```sh
age-keygen -o ~/.config/cloudfall/age.key
export SOPS_AGE_KEY_FILE=~/.config/cloudfall/age.key
```

Put a `.sops.yaml` at the root of your project so every secret
file encrypts to your key automatically:

```yaml
creation_rules:
  - path_regex: secrets/.*\.env$
    age: age1yourpublickeyhere...
```

## Layout: the directory mirrors the references

A reference `{environment: production, path: /crm/backend}` resolves from
`secrets/production/crm/backend.env`. The conventional tree:

```
secrets/
  production/
    shared.env        # platform scope, declared on the application
    crm.env           # application scope
    crm/
      backend.env     # component scope
```

Each file is a dotenv fragment, encrypted in place:

```sh
sops secrets/production/crm/backend.env
# opens your editor; write KEY=VALUE lines; sops encrypts on save
```

## Non-secret configuration: declare it in state

Feature flags, Redis database ids, and other plain configuration do not
belong in encrypted fragments — they belong in the component's declared
`environment` block, where changes are diffable, reviewable, and audited
like any other state:

```yaml
# components/crm-backend.yaml
spec:
  environment:
    FEATURE_SIGNUPS: "true"
    REDIS_URL: redis://127.0.0.1:6379/0
```

Rendering merges declared entries with the secret fragments into one
environment file. A key may live in **exactly one place**: if a declared
key also appears in a secret source, rendering fails fast with a
`secrets_key_conflict` naming the key and the offending file — no silent
shadowing in either direction.

## Declaring and rendering

Applications declare platform-scope references; components declare application and
component scope. Rendering merges them in that order — later scopes
override earlier keys:

```sh
uv run cloudfall secrets render crm-backend \
  --secrets-dir secrets
```

This writes `tmp/env/crm-backend.env` with mode `0600` and prints an
envelope carrying the key **names**, the file hash, and the resolved
references — never a value. Pass that file to `cloudfall deploy --yes
--env-file` or map it with `cloudfall migrate --component-env-file`; the deploy role installs it
into the release owned by the application user and the systemd unit loads it
with `EnvironmentFile=`.

A missing source fails fast with the exact expected path; a malformed line
fails with its line number; an unreadable value never half-renders a file.

## Drift detection

Rendering writes an `EnvironmentReceipt` (component, content hash, key
names) alongside the file, and the inspect run captures the hash of the
env file actually installed on each server. `cloudfall audit` compares
them: a tampered, stale, or missing installed file surfaces as
`environment.file[<component>]` drift, and a render that was never
deployed shows the same way. Components without a receipt are skipped —
nothing rendered means nothing to compare.

The receipt is the record's entry for the secret: it answers "which keys
did this release carry, and is that still what is installed" without
answering "what were they". A rotation therefore shows up twice, as a
commit in the private repository and as a hash change in the next
receipt, and the audit proves the new file reached the server.

## Through an agent

`cloudfall mcp serve` exposes `secrets_render`, with the secrets directory
fixed by the server's `--secrets-dir`. The agent sees key names and hashes only; the decrypted file
stays on the controller. Rotation is a git commit in the private
repository — which means rotation has history.

The agent is the brain and it decides when to render and deploy; it is
never a party to the values. `secrets_render` is annotated as a mutating
tool because it writes a file, and its envelope is what lands in the
record. Nothing an agent can call returns a secret, and
a tool that did would be a bug against the [manifesto](../MANIFESTO.md).

## Other backends

The provider boundary is deliberately small (`fetch(reference) → pairs`).
A managed backend such as Infisical can implement the same interface later
without touching state, references, or the render surface.

Under the [brownfield design](brownfield-design.md), where the team's own
Ansible inventory is the config, the same rule holds: the `cloudfall` key
in their `group_vars` and `host_vars` carries references, never values.
A team that already keeps secrets in ansible-vault or sops-encrypted vars
keeps them there; reading them through the same provider boundary is a
backend, not a migration. None of that is built yet.
