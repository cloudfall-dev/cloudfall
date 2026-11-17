# Proving run: one-node Elasticsearch on a fresh Debian 13 host (S-012-3, S-012-4)

| | |
| --- | --- |
| **Date** | 2026-11-17, 10:06 to 10:18 UTC |
| **Scope** | #320: S-012-3 and S-012-4, a one-node `search-main` converged on a fresh host, then the oversized-heap converge |
| **Hosts** | One Hetzner Cloud cx23 (nbg1), stock Debian 13.6, 3826 MB of memory; two 12 GB volumes made into `vg_data` by hand (the S-014 install step). Deleted after the run |
| **Code** | `main` at 45a0614; Elasticsearch 9.2.4; ansible-core 2.22.0 (as `uv.lock` pins it) |
| **Verdict** | S-012-3 did not hold as built: the first converge fails in the restart handler on ansible-core 2.22. A second converge, with no hand step, started the node, and every line of the check then held. S-012-4 held |

## Method

A project with one server, a default-deny baseline with the `data` pool (`primary` and
`secondary` on the two volumes), and S-012-1's `search-main` without `users`: version
`9.2.4`, `heapMb: 1536`. A cx23 has 3826 MB, so half its memory is 1913 MB; the check's
"heap of about 4096 MB" fits no cx23, and the declared heap was used instead.
`cloudfall secrets generate` stored the `cloudfall` and `elastic` passwords (sops, throwaway
age key).

Entry points: `cloudfall config validate`, `cloudfall secrets generate`,
`cloudfall baseline converge --yes`, and `converge_services` (the function behind the MCP
tool) from a short script that kept the playbook output.

## Results

| Step | Result |
| --- | --- |
| First services converge | **Failed**, `services.yml` rc 4, in the handler "Start the node and wait for its cluster": `[ERROR]: Using a block as a handler is not supported.`, origin `roles/cloudfall_elasticsearch/tasks/node-start.yml:16:7`. By then the package was installed and held, the configuration written, and the mirrored volume mounted; the node was not running and `cluster.initial_master_nodes` was still in `elasticsearch.yml` |
| Second services converge | Passed, `ok=75 changed=13`: "Enable and start Elasticsearch" started the node, the role wrote `search-main.cluster-formed` and removed `cluster.initial_master_nodes`, set the `elastic` password and registered the snapshot repository |
| `GET /` as `cloudfall` | `"number" : "9.2.4"`, `"cluster_name" : "search-main"` |
| `apt-mark showhold` | `elasticsearch` |
| `_nodes` | `mlockall: true`, `heap_max_in_bytes: 1610612736` (1536 MB, the declared heap) |
| `ss -ltnp \| grep 9200` | Only `[::ffff:127.0.0.1]:9200` |
| Unauthenticated `GET /` | `401` |
| `discovery.type`, certificates | No setting; `grep` finds only the role's comment line that names it. `/etc/elasticsearch/certs` does not exist |
| `elastic` password | Logs in (`authentication_realm.type: reserved`), read from the store with `sops`: `cloudfall secrets render` takes a component only and answers `secrets_component_unknown` for a service |
| Third services converge | `services.yml` `ok=72 changed=0`; `ActiveEnterTimestamp` unchanged (10:13:58); `cluster.initial_master_nodes` gone (`grep` finds only the comment line) |
| `heapMb: 2048` | `services.yml` rc 2, `ok=2 changed=0 failed=1`, at "Refuse an Elasticsearch heap above half of the server's memory", the first task after the rollout check and the facts: "heapMb 2048 of service search-main is above half of the 3826 MB of es1". The heap file and `ActiveEnterTimestamp` were unchanged |

| Criterion | Held |
| --- | --- |
| S-012-3 | **No**, as built: a fresh host needs two converges, because the first one fails in the restart handler. After the second, every part of the criterion held |
| S-012-4 | **Yes**: the next converge was `changed=0` with no restart, and the oversized heap stopped before any task changed the host |

## Findings

- `node-start.yml` holds a block, and the handler reaches it through `include_tasks`;
  ansible-core 2.22.0 refuses that with "Using a block as a handler is not supported". Every
  restart of a node runs this handler, so any change that notifies it (a first install, a
  configuration change, a version change under S-012-6) fails until the next converge. It
  was proven offline with `--check` and `ansible-lint`, which do not run handlers on a host
- The prepare step's `logging-volumes.yml` reports `changed=1` on every run (its "Hand the
  report to the controller" task, on the controller); `services.yml` itself is `changed=0`
- `cloudfall secrets render` cannot show a service's password, so the check's "the `elastic`
  password from `cloudfall secrets render`" has no command behind it today
- The check's literal `grep discovery.type` and `grep initial_master_nodes` match the role's
  comment in `elasticsearch.yml`; only the settings are absent

## Caveats

- One run, one node, one Debian release; a 1536 MB heap, not the issue's 4096 MB
- Hetzner Cloud block volumes, not NVMe partitions
