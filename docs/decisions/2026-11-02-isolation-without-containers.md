# Decision: isolation without containers or self-run VMs

Date: 2026-11-02. Status: decided, revisit on the triggers listed below.

## Question

How does Cloudfall keep components, runners and environments apart, and does
that need containers or virtual machines it runs itself?

## Decision

No containers and no hypervisor of Cloudfall's own. Isolation has two
boundaries:

- **Inside a server**, the boundary is the systemd sandbox of each unit, a
  database role per component, and cgroup limits. This is required on every
  server
- **Between trust levels**, the stronger boundary is a separate server: its
  own kernel. A VPS counts as a server; its hypervisor belongs to the
  provider, not to Cloudfall. This is recommended, not required

A team may run everything on one server, several independent projects
included, when it accepts the shared-kernel risk. The acceptance is declared
in the plan, never implied: Cloudfall warns about a placement that puts two
trust levels on one server unless the plan declares it.

A separate Unix user per component is necessary but is not a boundary on its
own.

## Why a Unix user is not enough

A user keeps files and signals apart. It does not stop:

- a local privilege escalation in the shared kernel, which reaches every
  component on the host
- a database, cache or queue on localhost that trusts every local user, or
  that components share one account of
- membership in the `docker` group or a broad sudo rule, either of which is
  root
- a runner that executes code from a pull request or a compromised
  dependency, which gets every key the runner holds; the keys, not the uid,
  set the blast radius
- reading other processes' command lines in `/proc`, world-readable home
  directories, and one component exhausting memory, disk or processes for
  the rest

## Why not containers

Docker and Podman isolate with the same kernel mechanisms systemd exposes
directly: namespaces, cgroups, seccomp and capabilities. A container shares
the kernel exactly as a sandboxed unit does, so it adds no boundary. What it
adds is packaging (images, independent library versions) and a runtime to
operate. Cloudfall's base is Debian and systemd
([`2026-09-14-no-nixos-base.md`](2026-09-14-no-nixos-base.md)), container
orchestration is a non-goal ([`../product.md`](../product.md), N-1), and the
exit test is that any Linux admin can take over a server with Debian
knowledge.

## Why not self-run VMs

A hypervisor on Cloudfall's hosts (Proxmox, KVM, Firecracker) is a second
infrastructure layer to provision, patch, back up and explain to whoever
takes over. A separate small server gives the same kernel boundary for a
few euros a month and fits the model Cloudfall already manages.

## The target model

The first four parts are required on every server, one server or many. The
last is a recommendation.

**Each unit is sandboxed by systemd.** The component unit gets, by default:

- its own user (or `DynamicUser=`), `NoNewPrivileges=` and an empty
  `CapabilityBoundingSet=`
- `ProtectSystem=strict`, `ProtectHome=`, `PrivateTmp=`, `PrivateDevices=`,
  writes only to `ReadWritePaths=` and `StateDirectory=`
- `ProtectProc=invisible`, `SystemCallFilter=@system-service`,
  `RestrictNamespaces=`, `RestrictAddressFamilies=`
- `IPAddressAllow=` and `IPAddressDeny=` where the component's peers are
  known
- `MemoryMax=`, `CPUQuota=`, `TasksMax=` from the component's declared
  envelope ([`../fleet-goals.md`](../fleet-goals.md), goal 8)
- secrets through `LoadCredentialEncrypted=` rather than `EnvironmentFile=`,
  so a value is readable by that unit only and never sits in the process
  environment

`systemd-analyze security` scores a unit; a score threshold can become a
deploy gate.

**Shared services authenticate per component.** Each component gets its own
PostgreSQL role, preferably through `peer` authentication over the Unix
socket, so the unit's user is the role and no password exists. A service
without authentication is never shared between components of different
owners.

**Runners are ephemeral and sandboxed.** A runner executes untrusted code.
It runs one job per transient unit started with `systemd-run` under the same
sandbox and `DynamicUser=`, its state removed after the job, and its network
open only for the dependency step. On a server that also runs production
components, the runner's unit is also denied the production components'
directories and network peers (`InaccessiblePaths=`, `IPAddressDeny=`) and
holds no production keys. This is the most exposed point of a one-server
placement.

**Access is scoped by environment.** A runner holds the SSH and age keys of
one environment ([`../ci-deploy-guide.md`](../ci-deploy-guide.md)). Deploys
log in as a dedicated user with sudo limited to the commands a deploy runs,
not as root.

**Separate servers separate trust levels (recommended).** Where the budget
allows, these do not share a kernel:

- production and staging
- build and CI, and production; production servers then receive built
  artifacts only
- components of different customers or owners

A team can only accept risk for its own projects. Placing components of
different owners on one server puts the risk on owners who did not accept
it, so Cloudfall keeps a separate, stronger warning for it. Cloudfall cannot
verify ownership, so this stays a warning, not a refusal.

## Where Cloudfall is today

- Each component already runs as its own application user with
  `NoNewPrivileges=true` and `PrivateTmp=true`
  (`engine/ansible/roles/cloudfall_deploy/templates/component.service.j2`);
  the rest of the sandbox is missing
- SSH keys are scoped by environment, but the baseline installs them into
  root's `authorized_keys`, so a runner's key is root on every server of its
  environment
- Secrets reach units through `EnvironmentFile=`
- Resource envelopes are not declared or enforced yet

## Triggers to revisit

- **A customer needs untrusted code from several tenants on one server**,
  where a separate server per tenant does not pay; then a stronger sandbox
  (gVisor, microVMs) is the question, not containers
- **A required workload ships only as a container image** with no reasonable
  Debian install path
- **systemd sandboxing proves insufficient** for a concrete attack that a
  container runtime would have stopped

## Consequences

- The component unit template gains the sandbox directives above as
  defaults, with per-component opt-outs that are declared, not silent
- No container runtime, image build or registry appears in Cloudfall's
  engine or docs as the way to isolate components
- One server is a supported placement, not a degraded one; the required
  parts above carry the whole isolation there
- A plan that puts two trust levels on one server declares it as an
  accepted risk; Cloudfall warns when it is undeclared, and warns again,
  more strongly, when the trust levels belong to different owners
