# S-012: Elasticsearch service

status: draft
serves: G-1, G-3

## Problem

A `Service` is PostgreSQL or Redis (`serviceKind` in
`config/schemas/v1/service.schema.json`, `ServiceKind` in `sdk/src/cloudfall/domain.py`). A
team whose application searches through Elasticsearch runs that part by hand: the package and
its pin, the heap, memory locking, the data volume, TLS between nodes, the `elastic` password,
one user per application, snapshots, and the check that a snapshot restores. None of it is
declared, so `config validate`, `audit`, `backup run` and `backup verify` know nothing about
it, and an agent has no operation to propose for it. ROADMAP.md and ARCHITECTURE.md name
Elasticsearch as catalog breadth that follows the PostgreSQL and Redis pattern.

Elasticsearch also breaks one assumption of that pattern: a `Service` lives on one server and
listens on loopback, while Elasticsearch is a cluster, even when it has one node.

## Behaviour

### Declaration

```yaml
apiVersion: cloudfall/v1
kind: Service
metadata:
  id: search-main
  description: Product search
spec:
  serviceKind: elasticsearch
  environment: production
  server: h1
  bind:
    address: 127.0.0.1
    port: 9200
  elasticsearch:
    version: 9.2.4
    heapMb: 4096
    users:
      - name: crm
        access: write
        indexPatterns: ["crm-*"]
        passwordRef: { environment: production, path: /services/search-main, key: CRM_PASSWORD }
  backup:
    directory: /var/backups/cloudfall/search-main
    onCalendar: "*-*-* 03:20:00 UTC"
    restoreCheckOnCalendar: "Sun *-*-* 05:00:00 UTC"
    retentionDays: 14
  metrics:
    enabled: true
```

`spec.elasticsearch` is required for the kind and refused for the others:

| Field | Required | Meaning |
|---|---|---|
| `version` | yes | Exact `X.Y.Z` of major 8 or 9; installed as `elasticsearch=X.Y.Z` from Elastic's APT repository for that major and held (`apt-mark hold`), so an unattended upgrade never restarts a node |
| `heapMb` | yes | `-Xms` and `-Xmx`, 512 to 31744 (above that the JVM loses compressed object pointers) |
| `nodes` | no | The servers of the cluster; must contain `spec.server`. Absent means `[spec.server]` |
| `users` | no | Application users, each limited to its index patterns |
| `remoteAccess` | no | `listenAddress` and `allowedSources`, the shape PostgreSQL uses (S-006) |
| `snapshotRepository` | no | An S3-compatible repository; absent means a filesystem repository in `backup.directory` |
| `restoreCheckIndices` | no | Index patterns the restore drill restores; absent means the smallest index of the newest snapshot |

### One node is a cluster of one

The role never sets `discovery.type: single-node`. A service with one node is a cluster whose
only master-eligible node is `spec.server`; growing it is adding a server to `nodes` and
converging, with no change to the first node's identity or data.

- `cluster.name` is the service id; `node.name` is the server id
- `cluster.initial_master_nodes` is written only for the first start of `spec.server`, while
  its data path holds no cluster state, and removed from the file on the next converge. A
  node that joins later never gets it
- `discovery.seed_hosts` lists the addresses of every node
- `services.yml` already runs one server at a time (`serial: 1`). On a server whose node
  needs a restart (package version or managed configuration changed), the role restarts it
  only when the cluster is `green` (or `yellow` for one node), and waits for the node to
  rejoin and the cluster to return to the status it had before moving on, so a version change
  inside a major is a rolling restart
- Two nodes are accepted and documented as what they are: a second copy of the data, no
  tolerance for losing either node as a voter

`spec.server` is the service's home: the snapshot and restore-drill timers, the user
converge and `cloudfall backup run` execute there once, not on every node.

### What each node needs

The kind yields S-011 requirements per node, so nothing below is prepared by hand:

| Requirement | Value |
|---|---|
| `package` | `elasticsearch=<version>` from the declared major's repository |
| `volume` | `elasticsearch`, 10G unless overridden, `mirrored` for one node and `expendable` for two or more (the replica is on another server), mounted at `/var/lib/elasticsearch`, needed by `elasticsearch.service` |
| `port` | 9300/tcp from the other node servers only; with `remoteAccess`, `bind.port` from `allowedSources` only |
| `certificate` | one per node from the project CA, usage server and client, names: the server's hostname and addresses; used for transport TLS with `verification_mode: full`, and for HTTP when `remoteAccess` is declared |
| `secret` | `generate`: the password of Cloudfall's own account (below) |

The role then writes the managed settings (`network.host`, `http.port`, the transport and
security blocks, `path.repo`, `bootstrap.memory_lock: true`), a `jvm.options.d` file with the
heap, and a unit drop-in with `LimitMEMLOCK=infinity`. Elasticsearch never swaps: a node that
can't lock its memory fails its bootstrap check and the converge stops. A `Baseline` declares
no memory, so the role is where the heap meets the host: it refuses, before changing
anything, a `heapMb` above half of the node's memory.

One server holds at most one Elasticsearch node: the Debian package runs one instance per
host. Two services whose `nodes` share a server fail validation.

### Access

Security is always on (`xpack.security.enabled: true`); there is no declaration that turns
it off.

- **Cloudfall's own account** is `cloudfall` in the file realm of every node, so it works
  when the cluster is red. Its password is generated, stored sops-encrypted, and lands on each
  node as a `0600` file owned by `elasticsearch`; the role, the snapshot scripts and the
  metrics exporter read it there. Only its bcrypt hash is written to the `users` file
- **`elastic`** gets a generated password on the first converge, set as a hash; nothing in
  Cloudfall uses it afterwards. It is a person's break-glass account, read with
  `cloudfall secrets render`
- **Declared users** each get a role of the same name limited to `indexPatterns`: `read`
  grants `read` and `view_index_metadata`; `write` adds `write`, `create_index` and
  `manage` on those patterns. The password comes from `passwordRef` (the S-006 form). The
  controller computes its bcrypt hash and sends only the hash (`password_hash` of the user
  API), following D-9: the plaintext is never staged on a host or sent to the server
- A user or role that Cloudfall created and that is no longer declared is removed; users
  and roles it did not create are left alone and reported by `audit` as unmanaged

HTTP listens on `bind.address` (loopback) in plain HTTP. With `remoteAccess`, the node also
listens on `listenAddress` and HTTP uses TLS on both addresses, because Elasticsearch has one
HTTP TLS setting per node; the certificate is the node's, so a client verifies it against the
project CA.

### Snapshots and the restore drill

`backup` keeps its contract: a schedule, a retention, an optional drill, receipts from
`cloudfall backup run` and `cloudfall backup verify`.

- The repository is a filesystem repository in `backup.directory` (added to `path.repo`), or
  the declared `snapshotRepository` (`type: s3`, `bucket`, `endpoint`, `basePath`,
  `credentialsRef`), whose keys go into the Elasticsearch keystore on every node
- A cluster of two or more nodes requires `snapshotRepository`: a filesystem repository
  must be one shared mount on every node, which Cloudfall does not provide
- `cloudfall-elasticsearch-backup` takes the snapshot `cloudfall-<timestamp>` of every index
  and the cluster state, waits for it, fails unless its state is `SUCCESS`, then deletes
  snapshots older than `retentionDays`
- `cloudfall-elasticsearch-restore-check` verifies the repository, restores the drill
  indices from the newest snapshot under the prefix `cloudfall-restore-check-` with no
  replicas, waits for them to be `green`, compares each document count with the count the
  snapshot recorded, and deletes the restored copies. It fails when there is no snapshot, on
  any mismatch, and when the restore would cross the low disk watermark

### Validation

`cloudfall config validate` refuses with `CONFIG_INVALID`:

- `elasticsearch` on another kind, or its absence on this one
- `version` that is not `X.Y.Z` of major 8 or 9 (`service_elasticsearch_version`)
- `nodes` without `spec.server`, with an undeclared server, or with a duplicate
  (`service_elasticsearch_nodes`)
- a server that is a node of two Elasticsearch services (`service_elasticsearch_server_shared`)
- two or more nodes without `snapshotRepository`
  (`service_elasticsearch_snapshot_repository_local`)
- a user named `elastic`, `cloudfall`, or after a built-in user or role
  (`service_elasticsearch_user_reserved`), a pattern of `*` or one that starts with `.`
  (`service_elasticsearch_pattern`)
- `remoteAccess` under the S-006 rules: no firewall in the baseline, the port already open
  to everyone, a source of every address

### Audit and metrics

- `audit` reports drift when a node's installed version differs from `version`, when
  `elasticsearch.service` is not enabled and running, when the managed configuration differs
  from the rendered one, when the HTTP listener is on an address the declaration does not
  name, and when a requirement of the node is unmet (S-011)
- With `metrics.enabled`, the collector on each node scrapes it with Alloy's Elasticsearch
  exporter as the `cloudfall` account, with the service id as the label the PostgreSQL and
  Redis exporters use

### Documentation

`docs/elasticsearch-service-guide.md` covers declaring a one-node service, growing it, the
accounts, snapshots and the drill, and states that the role installs Elastic's own packages,
which Elastic distributes under the Elastic License 2.0, so the operator checks that license
against their use (offering the service to third parties is the case it restricts).

Touches `config/schemas/v1/service.schema.json`, `config/examples/services/`,
`sdk/src/cloudfall/domain.py`, `sdk/src/cloudfall/inventory.py`,
`sdk/src/cloudfall/validation.py`, `sdk/src/cloudfall/requirements.py`,
`sdk/src/cloudfall/secrets.py`, `sdk/src/cloudfall/audit.py`,
`engine/src/cloudfall_engine/ansible_inventory.py`, a new role
`engine/ansible/roles/cloudfall_elasticsearch`, `engine/ansible/playbooks/services.yml`,
`engine/ansible/playbooks/backup.yml`,
`engine/ansible/roles/cloudfall_logging_collector/templates/config.alloy.j2`, and the guide.

## Acceptance criteria

- S-012-1: A project with one `Server`, its baseline, and the declaration above without
  `users` passes `cloudfall config validate` with exit 0, and `cloudfall-engine inventory
  render` carries the service with `nodes` of exactly that server
- S-012-2: For the project in `config/examples` without an Elasticsearch service, the
  rendered inventory is byte-identical before and after the change
- S-012-3: After `services.yml` converges S-012-1 on a fresh Debian 13 host, `GET /` as
  `cloudfall` on `127.0.0.1:9200` reports the declared version and a `cluster_name` equal to
  the service id, the package is held, `_nodes` shows `mlockall: true` and the declared heap,
  no listener for 9200 exists on a non-loopback address, an unauthenticated request gets 401,
  and `elasticsearch.yml` holds no `discovery.type` and, after a second converge, no
  `cluster.initial_master_nodes`
- S-012-4: A second converge changes nothing (`changed=0`) and restarts no node; a `heapMb`
  above half of the host's memory stops the converge before any task changes the host
- S-012-5: Adding a second server to `nodes` with a `snapshotRepository` and converging gives
  a cluster of two nodes with the same `cluster_uuid` as before and every index of the first
  node present; the second node's ruleset admits 9300 from the first node's addresses only,
  and each node's transport certificate chains to the project CA
- S-012-6: Changing `version` to a later patch of the same major on the two-node cluster
  restarts one node at a time, the cluster is never red, and both nodes report the new version
- S-012-7: A declared user with `access: read` on `crm-*` can search `crm-1` and gets 403 on
  indexing into it and on searching `billing-1`; with `access: write` it can create and index
  `crm-2`
- S-012-8: The password of a declared user, of `elastic` and of `cloudfall` appears in none
  of the rendered inventory, playbook output, command answers or receipts; the staged file of
  a declared user holds a bcrypt hash and never the password
- S-012-9: Removing a declared user and converging deletes that user and its role, and leaves
  a user created by hand in place, which `audit` lists as unmanaged
- S-012-10: `cloudfall backup run search-main` creates a snapshot in state `SUCCESS` and
  writes a `BackupReceipt`; `cloudfall backup verify search-main` restores the drill index,
  finds equal document counts, deletes the restored copy and writes its receipt; with an empty
  repository it fails and says no snapshot exists
- S-012-11: Snapshots older than `retentionDays` are gone after the next backup run, and the
  newest is kept
- S-012-12: Each refusal listed under Validation fails `cloudfall config validate` with
  `CONFIG_INVALID` and its code
- S-012-13: `cloudfall audit` reports drift when the installed version differs from the
  declaration, when the unit is stopped, and when `network.host` is changed by hand to a
  non-loopback address
- S-012-14: With `remoteAccess` of `listenAddress: 10.0.0.2` and `allowedSources:
  [10.0.0.0/24]`, a client from an allowed address connects over HTTPS verifying the project
  CA, plain HTTP is refused on both addresses, and the ruleset holds `ip saddr 10.0.0.0/24
  tcp dport 9200 accept` and no unrestricted rule for 9200
- S-012-15: With `metrics.enabled`, the rendered collector configuration holds one
  Elasticsearch exporter for the node, labelled with the service id, and none without it

## Out of scope

- OpenSearch: a separate `serviceKind` with its own packages and security model
- Kibana, Logstash and Beats
- Upgrading across a major (8 to 9), which needs the upgrade checks and sometimes a reindex;
  the role refuses a `version` whose major differs from the installed one
- Removing a node: draining its shards and its vote is an operation with its own verify step
- Dedicated node roles (master-only, data tiers, coordinating); every node holds every role
- Moving data in from an existing cluster (reindex from remote, snapshot restore); a later
  `cloudfall data copy` source
- More than one Elasticsearch node per server
- Index templates, mappings, lifecycle policies and API keys: the application's own
- A shared mount for a filesystem repository on several nodes

## Decisions relied on

- D-9: a password reaches the service only as a hash computed on the controller

## Issues

## Verification

No criterion is verified yet.
