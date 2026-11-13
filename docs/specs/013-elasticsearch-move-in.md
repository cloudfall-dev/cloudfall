# S-013: Moving into an Elasticsearch service

status: draft
serves: G-2, G-3

## Problem

S-012 lets a project declare an Elasticsearch `Service`, on supported versions only, and lets
its nodes join an existing cluster of a supported version. It leaves open how the data of a
cluster that runs elsewhere, often on a version Cloudfall will not install, gets there.

A built-in migration is the wrong answer: sources differ in version, mappings, writers and
access, and a matrix from every old version to every supported one cannot be tested. An
agent can build the move for one project, but then two things are missing:

- nothing tells it how: what to read from the source, which path fits, what its playbook may
  touch
- nothing proves the move. A migration the agent generated and then reports as done is its
  own witness. `cloudfall data copy` writes a receipt for PostgreSQL; Elasticsearch has no
  equivalent, and G-2 asks for a verify step on every operation

## Behaviour

### The guide

`docs/elasticsearch-migration-guide.md` is written for an agent working in a Cloudfall
project. It covers, in order:

1. **Read the source**, read-only: version, node count, indices with size, document count and
   the version each was created in, mappings and settings, which services write and how
   often, how clients authenticate
2. **Choose the path**:

   | Source | Path |
   |---|---|
   | The operator's own cluster, one major behind a supported line | Upgrade it by its own rolling upgrade, then join (`existingCluster`, S-012); no data is copied |
   | A supported version already | Join |
   | Anything else the target can read over HTTP | A new cluster (S-012) and reindex from remote, one index at a time |
   | A source the target cannot read from, or a snapshot as the only access | A snapshot restored on a temporary node of the intermediate major, then reindex from that node |

3. **Generate the move** as a playbook in the project: indices created on the target first
   with the source's mappings and settings, no replicas and no refresh during the load, a
   rate limit, and ranges instead of one long scroll on large indices
4. **Keep up with writes**: double writes started before the load with create-only copying,
   or repeated passes on a modification field with a short write stop; what each misses
   (deletes during the load) and how `data check` shows it
5. **Switch and soak**: reads move one instance first, the old cluster stays current and
   reachable until the check has passed and the soak has ended

Its rules, stated as rules:

- the source is read-only, unless the operator owns it and it is one major behind a supported
  line; nobody upgrades a cluster that is not theirs
- a generated playbook lives in the project and runs only through `cloudfall-engine playbook
  run`, behind the same approval as any change to a server; it never writes to the source
- credentials of the source are read from a file a person provides, never from a command line
  or a resource
- the move is done when `data check` says so, not when the playbook ends

### The check

```console
cloudfall data check <service> --source-url-file <file> [--index <pattern>]...
```

- `<service>` is a declared `Service` of kind `elasticsearch`; another kind fails with
  `PRECONDITION` (`data_check_kind_unsupported`)
- The file holds the source's URL with its credentials, `0600`, as `data copy` reads its
  source. The target is reached through its home server with Cloudfall's own account
- Without `--index` it selects every index of the source whose name does not start with `.`;
  with it, the indices matching any pattern. A pattern that matches nothing fails with
  `PRECONDITION`
- Per selected index it compares: presence on the target, document count, and mappings.
  Counts are read after a refresh on each side. Mappings are compared as the field paths and
  their types, so parameters the target's version fills in by default are no difference
- It changes neither cluster apart from that refresh: no index, document, setting or user is
  created, changed or removed
- Every index matching answers `data.status` `ok` and exit 0. Any difference answers `drift`
  and a non-zero exit, with each differing index, the kind of difference (`missing`,
  `count`, `mapping`), both counts, and for a mapping the differing paths
- It writes a `DataCheckReceipt` under `tmp/`, beside the receipts `data copy` writes: the
  service, the source's host without credentials, the time, each index with both counts and
  its verdict, and the overall verdict
- The source's URL and credentials appear in no answer, log line or receipt
- A source that cannot be reached or refuses the credentials fails with `PRECONDITION` and
  writes no receipt
- The command is in the read-only table of AGENTS.md and is served over MCP

Touches `sdk/src/cloudfall/app.py`, a new `sdk/src/cloudfall/data_check.py`, a new
`config/schemas/v1/data-check-receipt.schema.json`, the generator of the project's
`AGENTS.md`, and the guide.

## Acceptance criteria

- S-013-1: Against a source and a target holding the same three indices,
  `cloudfall data check` exits 0 with `data.status` `ok` and writes a receipt with three
  matching entries
- S-013-2: After one document is deleted on the target, it exits non-zero with `drift` and
  names that index, the kind `count` and both counts; with an index absent on the target the
  kind is `missing`; with a field of another type the kind is `mapping` and the path is named
- S-013-3: A source of an older major whose mappings differ from the target's only in
  parameters the newer version adds by default checks as `ok`
- S-013-4: The document counts, mappings, settings and users of both clusters are the same
  before and after a run
- S-013-5: The source's credentials appear in none of the answer, the receipt, the process
  arguments or the log output
- S-013-6: An unreachable source, refused credentials, and a `--index` pattern matching
  nothing each fail with `PRECONDITION`, and no receipt is written
- S-013-7: A service of kind `postgresql` fails with `PRECONDITION` and
  `data_check_kind_unsupported`
- S-013-8: The guide holds the path table and the four rules, and `AGENTS.md` generated by
  `cloudfall init` lists `data check` among the commands that change nothing on servers
- S-013-9: A proving run recorded in `docs/proving-runs/`: an agent given only the guide and
  a source one major behind a supported line moves it by upgrade and join; given a source two
  majors behind, by reindex from remote into a new cluster; each ends with a passing
  `data check`

## Out of scope

- A built-in `data copy` for Elasticsearch
- Comparing document contents; counts and mappings are the check
- Running or scheduling the generated playbook beyond `cloudfall-engine playbook run`
- Sources other than Elasticsearch, OpenSearch included
- `data check` for PostgreSQL and Redis, where `data copy` and the restore drill already
  leave receipts

## Decisions relied on

- none

## Issues

## Verification

No criterion is verified yet.
