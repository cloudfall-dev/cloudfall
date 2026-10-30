# State

This module is the declarative source of truth for Cloudfall: versioned JSON
Schemas and the YAML resources they validate — servers, server types,
applications, components, services, domains, logging stacks, and SSH public keys.

It must not contain credentials or execution logic. Secret values live in
environment files rendered under a project's `tmp/`, never in a resource;
state stores references only.

## v1 contract

Schemas live in `schemas/v1/` and use JSON Schema Draft 2020-12. Every resource
has an `apiVersion`, `kind`, strict `metadata.id`, and kind-specific `spec`.
Unknown properties are rejected.

The current contract includes `Server`, `ServerType`, `Application`, `Component`,
`Domain`, `Service`, `SshPublicKey`, and `LoggingStack` desired-state
resources. A `Service` declares one infrastructure service (v1: PostgreSQL)
with a loopback-only bind, application-owned databases resolved to peer-auth
roles, and a scheduled dump-and-prune backup contract. A
`LoggingStack` pins the guarded parallel migration, backend and collector
placement, package versions, loopback listeners (including the Prometheus
metrics backend), mTLS paths, retention, and explicit journal/file sources;
collectors push host metrics through the same gateway. A `Domain` pins the proxy
and origin servers, public names, edge mode, TLS policy, configuration evidence
path, and end-to-end health contract. Public keys are non-secret and are
scoped to an environment; private keys never belong in state. A server profile
captures the required Debian/systemd baseline, software RAID level and
capacity, mounts, packages, required service and timer units, an optional
default-deny inbound firewall contract, and an explicit allowlist of
configuration files whose metadata or hash may be inspected.

`observed-server.schema.json` defines the normalized, read-only evidence emitted
by the engine. Observations are runtime evidence, not desired state, and belong
under ignored controller storage such as `tmp/observed/`. They include
structured NVMe SMART health when available, systemd timer states, the
managed nftables table as raw JSON evidence, and listening-socket evidence
used to prove declared services bind only to loopback; configuration
contents and credentials must never be written there.

`observed-domain.schema.json` validates controller-side DNS, edge, TLS, origin,
and public HTTP evidence. `deployment-receipt.schema.json` validates receipts
written only after an Ansible proxy run has applied and validated its final
configuration. `artifact.schema.json` validates build metadata for hashed
release tarballs, and `release-receipt.schema.json` validates receipts written
after a component release has passed its health gate, or failed a command
health check (`healthCheckPassed: false`, with the check's attempts, exit code
and timeout under `healthCheck`, never its output). Together with server observations, these inputs derive planned,
ready-to-deploy, deployed, configured, and healthy service states.

`examples/` contains a complete valid desired-state set. `tests/invalid/`
contains deliberately invalid desired state, while
`tests/observed/compliant/` contains validated audit fixtures.

Real servers and applications belong in a project of their own (created
with `cloudfall init`), kept private. A project holds one directory per
resource kind and only those directories are read as resources. Keep
examples and test fixtures synthetic; provider identifiers, network
allocations, and desired profiles for live hosts never belong here.
