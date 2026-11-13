# S-011: Host requirements, satisfied by Cloudfall

status: approved
serves: G-1, G-3

## Problem

A workload that Cloudfall deploys needs things from its host that the workload's own role
does not create, and today a person prepares each of them by hand before the first converge.
`LoggingStack` is the sharpest case (`docs/logging-service-guide.md`, steps 1 and 2):

| Need | Today |
|---|---|
| A data volume for Loki and one for Prometheus, sized, on the right disk, mounted before the unit starts | made by hand (`lvcreate`, `mkfs`, fstab, `RequiresMountsFor`); the role only checks that the path exists |
| The gateway port open to the collector hosts | added by hand to the `Baseline` firewall, open to everyone |
| A private CA, a gateway certificate, a client certificate per collector | made with the operator's own PKI and copied to `/etc/cloudfall/logging/`; the role refuses to run without them |
| The Grafana admin password, the SMTP password of the alert receiver | written by hand to files on the backend host |
| A DNS name for the gateway | created by hand in the DNS provider |
| Package versions that exist for the host's Debian release | checked by hand with `apt-cache policy` |
| A migration guard that fits the host | only `parallel` with `preserveLegacyAgents: true` exists, which means nothing on a host with no legacy agents |

The same gaps return for every `Service` with data (PostgreSQL, Garage) and for any future
platform workload. The result: "Cloudfall prepared this server" does not mean "Cloudfall can
deploy onto it". cloudfall-dev/cloudfall#201 covers declaring volumes in `Baseline`; this spec
covers the other half: a workload says what it needs, and Cloudfall provides it.

## Behaviour

### Requirements

A workload kind (`LoggingStack`, `Service`, later `Component`) yields a list of
**requirements** per server it lands on. Most come from the kind itself, so a resource
declares nothing extra; a resource may override sizes and placement.

| Requirement type | Says | Example from `LoggingStack` |
|---|---|---|
| `volume` | name, minimum size, `mirrored` or `expendable`, mount path, owner, the units that need it | `loki`, 10G, expendable, `/srv/storage/loki`, `loki.service` |
| `port` | port, protocol, the servers or CIDRs allowed in | 3101/tcp from every collector server |
| `secret` | name, how it is produced (`generate` or `input`), the file it lands in, owner and mode | Grafana admin password, `generate`; SMTP password, `input` |
| `certificate` | the CA it chains to, subject names, usage (`server` or `client`), the paths of the certificate and key | gateway certificate for the gateway name; one client certificate per collector |
| `dns-name` | a name and the server it points to | the gateway name |
| `package` | name and pinned version, checked against the host's release | `loki=3.7.7`, `alloy=1.19.2-1` |

A resource may override what the kind yields, for example:

```yaml
spec:
  backend:
    storage:
      volume: { size: 50G, placement: expendable }
```

### Providers

Each requirement type has a **provider**: a unit with `check`, `apply` and `audit` that runs
before the workload's role. Providers are how Cloudfall stays a plugin system: a new
requirement type is a new provider, not new code in every role. v1 ships these built-in:

- `volume`: LVM inside the storage pools a `Baseline` declares (#201). `mirrored` becomes a
  raid1 LV over two PVs, `expendable` a linear LV on the PV the pool names. It creates the LV,
  the filesystem, an fstab line by UUID with `nofail`, the mount, the owner, and a
  `RequiresMountsFor` drop-in for every unit named. It only takes free space, never shrinks,
  and grows an LV that is smaller than the requirement (`lvextend -r`)
- `port`: one nftables rule per source, rendered into the firewall the baseline already
  owns, scoped to the declared sources; never `0.0.0.0/0` unless the resource says so
- `secret`: `generate` makes a random value of the declared length and stores it
  sops-encrypted in the project's `secrets/`, so it survives a rebuild; `input` reads the value
  once, at a terminal, with echo off, and stores it the same way. Either way the value then
  lands on the host as the declared file
- `certificate`: a project CA whose key lives sops-encrypted in `secrets/`. Each host makes its
  own key and a CSR; the CLI signs the CSR and installs the certificate, so private keys of
  hosts never leave them. A certificate within 30 days of expiry is renewed on converge
- `dns-name`: the project's DNS provider when one is declared (Cloudflare first); otherwise a
  name derived from the server's hostname, when the backend and the gateway share a host
- `package`: checks each pinned version against the host's APT sources in check mode, so a
  missing version fails the plan, not the converge

### When it runs, and who approves

- `config validate` resolves every requirement and fails on one no provider can meet: no
  storage pool with enough free space declared, a port that collides with another workload, a
  `dns-name` with no provider and no derivable name
- Check mode (the plan without `--yes`, or `operations propose`) lists every requirement per
  server with its state: `met`, `will create`, `will grow`, `needs input`. That plan is what a
  person approves; the gates of AGENTS.md do not change
- Converge runs the providers first, then the workload's role. The role keeps checking its
  preconditions, so a provider bug still stops it before it mutates
- `audit` checks every requirement as declared: the LV exists with its type and size, the
  mount is active, the rule is in the ruleset, the certificate chains to the project CA and is
  not near expiry
- Off a terminal (an agent, CI), a requirement in `needs input` stops the command with exit 4
  and `error.context.code` `INPUT_REQUIRED` (the CLI framework's code for an answer no one can
  give; `PERSON_REQUIRED` shares exit 4, so branch on the code), naming each missing input and the command a person
  runs to supply it (`cloudfall secrets input <ref>`). Nothing else stops for input

### LoggingStack, the first consumer

- The kind yields: two `volume`s (Loki, Prometheus), one `port` (the gateway, from every
  collector server; none when the only collector is the backend host), the CA and the
  certificates, the Grafana password (`generate`), the SMTP password or webhook URL of each
  receiver (`input`), the gateway `dns-name`, and its `package` pins
- `migration.mode` gains `none` for hosts with no legacy agents; `parallel` keeps its guard
- A project that still provides the files by hand keeps working: a requirement whose file
  already exists and passes the role's checks is `met`, and nothing overwrites it
- After this, deploying a stack onto a server that `cloudfall add server` plus `baseline`
  prepared needs: the `LoggingStack` resource, a storage pool in the baseline, and the
  inputs Cloudfall cannot generate

## Acceptance criteria

- S-011-1: A project with one `Server` whose baseline declares a storage pool, and a
  `LoggingStack` with that server as backend and only collector, `migration.mode: none`, and no
  files under `/etc/cloudfall/logging/`, passes `cloudfall config validate` with exit 0
- S-011-2: The plan of S-011-1 lists, per requirement, its type, its target and the state
  `will create`; the Grafana password shows `will create`, an SMTP receiver password shows
  `needs input`
- S-011-3: The same plan run off a terminal exits 4 with `INPUT_REQUIRED`, and
  `error.context` names the SMTP password reference and the command that supplies it
- S-011-4: After the input is supplied and the stack converges on a fresh Debian 13 host
  prepared by `bootstrap` and `baseline`, Loki and Prometheus write to their own LVs
  (`findmnt`), each unit carries `RequiresMountsFor` for its mount, and the end-to-end checks
  of `docs/logging-service-guide.md` step 6 pass, with no step done by hand
- S-011-5: A second converge changes nothing (`changed=0` for providers and roles)
- S-011-6: With a second server as collector, the backend's ruleset admits the gateway port
  from that server's addresses only, and the collector's client certificate chains to the
  project CA; no host private key exists in the project or in any command output
- S-011-7: `cloudfall audit` reports drift when a requirement's LV is unmounted, when the
  gateway rule is removed, and when the gateway certificate expires within 30 days
- S-011-8: A requirement no provider can meet (pool too small, port taken by another
  workload) fails `config validate` with `CONFIG_INVALID` and a code naming the requirement
- S-011-9: A secret value, generated or input, appears in none of the rendered inventory,
  playbook output, command answers or receipts; answers name the reference and its SHA-256
- S-011-10: A `LoggingStack` whose files were provided by hand before this change converges
  with every such requirement `met` and no file rewritten

## Out of scope

- Declaring storage pools, VGs and PVs in `Baseline`: cloudfall-dev/cloudfall#201. This spec
  consumes that declaration
- Third-party providers loaded from outside the package. v1 keeps the provider interface
  internal and documented; loading external ones is a later spec
- High availability of the logging backend
- Requirements of `Component` (application code): same mechanism, a later spec

## Decisions relied on

- none

## Issues

## Verification

No criterion is verified yet. Landed so far, each checked by unit tests on the controller and
none against a live Debian host:

- `migration.mode: none` (cloudfall-dev/cloudfall#203), part of S-011-1
- `cloudfall requirements show`: the requirements every declared `LoggingStack` places on its
  servers, by server, read from the project only (cloudfall-dev/cloudfall#204), the listing
  half of S-011-2
- Secrets provider, project side (cloudfall-dev/cloudfall#205): each secret requirement names a
  key in `secrets/<environment>/logging-stacks/<stack>.env`. `requirements show` gives secrets
  their state (`met`, `will-create`, `needs-input`) from key names, which sops keeps in clear,
  and lists in `data.missing` what a person must supply; other types say `unchecked` until
  their provider lands. `cloudfall secrets generate` writes every missing `generate` secret;
  `cloudfall secrets input <name> --owner <id>` takes one `input` secret at a terminal with echo
  off, or through `--value-from-file` / `--value-from-env`, and off a terminal without either
  exits 4 with `INPUT_REQUIRED`. A stored key is never overwritten (exit 6, `CONFLICT`). Part of
  S-011-2 and S-011-9
- `cloudfall logging deploy`: without `--yes` the plan, with each requirement's state; with
  `--yes` it generates the missing generated secrets, stops with exit 4 and
  `error.context.code` `INPUT_REQUIRED` while an input secret is missing (S-011-3), then runs
  `logging.yml` with each stored value staged in a `0600` file on the controller; the backend
  role copies it to its host path and keeps a file already there (S-011-10 for secrets). The
  role's delivery tasks ran for real on the controller; the full stack has not run against a
  Debian host
- Certificate provider, controller side: one CA per environment, its key and certificate
  sops-encrypted at `secrets/<environment>/pki/ca.env`, created on first use. It signs only the
  public key of a host's request: names, usage (`serverAuth` or `clientAuth`), a 90-day lifetime
  and `CA:FALSE` come from the requirement, never from the request. A held certificate is `met`,
  `will-renew` within 30 days of expiry or for other names or usage, or `foreign` when another
  CA signed it, and is then kept (part of S-011-6, S-011-7 and S-011-10). Checked with
  `openssl verify` for both purposes
- Certificate provider, host side: `logging deploy --yes` runs `logging-requests.yml`, where each
  host makes the key a requirement names unless it has one (`0600`, `umask 077`) and a signing
  request, and hands the request and any certificate already in place to the controller. The CA
  signs the missing and due ones; `logging.yml` copies each new certificate and the CA to its
  host path, replacing the old certificate, while a `met` or `foreign` one is left as it is.
  The answer lists each certificate's state and whether the CA was created (S-011-6 on the
  controller: no host key reaches the project or an answer). The request playbook and the
  delivery tasks ran for real on the controller; no Debian host yet
