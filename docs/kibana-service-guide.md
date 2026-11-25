# Run a Kibana service

Status: declaring Kibana, its users, publishing it with a Domain, a version
change, the backup and restore of the dashboards, a rebuilt server and the
audit are built. The role and inspect are proven by replaying their tasks
against stand-ins of Kibana's keystore, of the node's token tool and of the
answers Kibana and Elasticsearch give, not yet on a live Debian 13 host.

A Kibana `Service` runs on a node of the Elasticsearch service it reads and
holds no data of its own: dashboards, data views, saved searches and every
other saved object are documents in the cluster's system indices. It has no
version of its own either: it runs at the version of its cluster. The
behaviour is specified in [spec 024](specs/024-kibana-service.md).

## Safety boundary

- Kibana listens on loopback only, in plain HTTP. Nothing off the server
  reaches it unless a `Domain` publishes it
- Kibana logs in to its node with a token of the `elastic/kibana` service
  account, made on that node and kept in Kibana's keystore. The password of
  the built-in `kibana_system` user is never set or changed
- The token and the three encryption keys are in Kibana's keystore, never in
  `kibana.yml`, a playbook's output, a command's answer or an audit finding
- Kibana has no accounts of its own: a person logs in with a user of the
  Elasticsearch service and sees only the indices that user may read
- When the node Kibana runs on is down, Kibana is down, whatever the other
  nodes of the cluster do. Kibana talks to that node only

## Licence

The role installs Elastic's own `kibana` package from the same Elastic
repository as the node, which Elastic distributes under the Elastic License
2.0, as the Elasticsearch packages are. Check that licence against your use:
offering Kibana to third parties as a service is the case it restricts.

Cloudfall configures only what the free licence of that distribution gives.
These features of Kibana need a paid Elastic licence, and Cloudfall neither
configures nor refuses them:

- Single sign-on (SAML, OpenID Connect): people log in with Kibana's own form
- PDF and PNG reports of dashboards and visualisations
- Most alert connectors, such as email, Slack, webhooks and PagerDuty
- Privileges narrower than a whole feature (sub-feature privileges): the
  two roles below give a base privilege on all of Kibana

## Declare Kibana next to a one-node Elasticsearch service

Kibana reads one Elasticsearch service and runs on one of its nodes. Here
`search-main` is a one-node cluster on `h1`, with a user `analyst` who may
read the `crm-*` indices:

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
      - name: analyst
        access: read
        indexPatterns: ["crm-*"]
        passwordRef: { environment: production, path: /services/search-main, key: ANALYST_PASSWORD }
  backup:
    directory: /var/backups/cloudfall/search-main
    onCalendar: "*-*-* 03:20:00 UTC"
    restoreCheckOnCalendar: "Sun *-*-* 05:00:00 UTC"
    retentionDays: 14
```

The Kibana service on the same server:

```yaml
apiVersion: cloudfall/v1
kind: Service
metadata:
  id: search-kibana
  description: Dashboards over product search
spec:
  serviceKind: kibana
  environment: production
  server: h1
  bind:
    address: 127.0.0.1
    port: 5601
  kibana:
    elasticsearch: search-main
    heapMb: 1024
    users:
      - name: analyst
        access: edit
```

| Field | Meaning |
| --- | --- |
| `bind.address` | A loopback address; any other is refused (`service_kibana_bind`) |
| `kibana.elasticsearch` | The Elasticsearch service Kibana reads, of the same environment, with `spec.server` one of its nodes |
| `kibana.heapMb` | `--max-old-space-size` of Kibana's Node.js process, 1024 to 8192, written to `/etc/kibana/node.options` |
| `kibana.domain` | The `Domain` that publishes Kibana (see Publish Kibana with a Domain); absent, Kibana answers on loopback only |
| `kibana.users` | Which users of the Elasticsearch service may work in Kibana, each with `access` of `view` or `edit` (see the next section) |

The kind declares no `version`, no `backup`, no `metrics` and no
`remoteAccess`; `config validate` refuses each of the last three
(`service_kibana_field_refused`). One server holds at most one Kibana, and
one Elasticsearch service has at most one (`service_kibana_duplicate`). An
Elasticsearch service that declares `existingCluster` can't have a Kibana
service (`service_kibana_existing_cluster`): that cluster's own Kibana still
writes its indices, with keys Cloudfall does not hold. Declare Kibana once the
old nodes are gone and `existingCluster` is removed.

Validate, see what the server needs, and generate the keys:

```console
uv run cloudfall config validate
uv run cloudfall requirements show
uv run cloudfall secrets generate
```

`requirements show` lists the `kibana` package at the Elasticsearch service's
version and three generated secrets: the keys Kibana encrypts sessions,
saved objects and reports with. `secrets generate` makes each once and stores
it sops-encrypted in `secrets/<environment>/services/search-kibana.env`; it
never overwrites a stored key.

Then converge the services, as for any service: the `converge_services` tool
of `cloudfall mcp serve`, or `cloudfall operator approve` of a services
proposal. The converge reads the keys from `--secrets-dir` first, so a
missing one stops it with `PRECONDITION` before any host changes. It
converges every node of the cluster first, then Kibana: it installs
`kibana=<version>` and holds it, writes `/etc/kibana/kibana.yml` and the heap,
puts the token and the keys in the keystore, and waits until Kibana reports
itself available. A `heapMb` that together with the node's Elasticsearch heap
is above three quarters of the server's memory stops the converge before
anything changes.

## Declare who may view and who may build dashboards

A person who builds dashboards is two declarations: a user of the
Elasticsearch service, with a password and the indices it may read, and its
line in `kibana.users`. Add a second user, `support`, to `search-main`:

```yaml
  elasticsearch:
    users:
      - name: analyst
        access: read
        indexPatterns: ["crm-*"]
        passwordRef: { environment: production, path: /services/search-main, key: ANALYST_PASSWORD }
      - name: support
        access: read
        indexPatterns: ["crm-*"]
        passwordRef: { environment: production, path: /services/search-main, key: SUPPORT_PASSWORD }
```

and give `analyst` edit access and `support` view access in `search-kibana`:

```yaml
  kibana:
    elasticsearch: search-main
    heapMb: 1024
    users:
      - name: analyst
        access: edit
      - name: support
        access: view
```

Store each password at its `passwordRef` as the [secrets
guide](secrets-guide.md) describes, and converge the services.

| `access` | Role | What the person may do |
| --- | --- | --- |
| `view` | `cloudfall-kibana-view` | Open dashboards, Discover and saved objects; change nothing |
| `edit` | `cloudfall-kibana-edit` | Also create and change data views, visualisations, dashboards and every other saved object |

The Kibana role keeps both roles through Kibana's own role API: Kibana's base
privilege `read` or `all` on every space, and no privilege in Elasticsearch.
The user converge gives each user named in `kibana.users` the role of its
`access` next to its own role, which stays as it is, so a dashboard shows
that person only the indices its `indexPatterns` name. Neither role can
manage users or roles. Dev Tools sends requests as the logged-in user, so it
reaches what that user's role reaches.

- A user of the Elasticsearch service that `kibana.users` does not name can
  log in and gets Kibana's page for an account with no access. An
  application's user has no reason to be listed
- Removing a line from `kibana.users` and converging takes the Kibana role
  from the user; the user and its index privileges stay
- A person may change their own password in Kibana. The user converge sets a
  password only when the stored one changes, so the person's choice stays
  until then, and the secrets store no longer holds the password in use
- `elastic` is the cluster's break-glass account. Nothing here uses it; don't
  work in Kibana with it

## Open Kibana through an SSH forward

Without `domain`, Kibana answers on `127.0.0.1:5601` of its server only.
Forward that port to your machine:

```console
ssh -N -L 5601:127.0.0.1:5601 admin@h1.example.test
```

and open `http://127.0.0.1:5601` in a browser. Log in with a user of the
Elasticsearch service, such as `analyst`, and its password. Kibana's links
start with `http://127.0.0.1:5601` too, so keep the same local port.

## Publish Kibana with a Domain

A `Domain` whose proxy is the Kibana server, whose upstream is the service's
`bind`, and whose `tls.mode` is `required` publishes Kibana under its own
hostname, with the certificate and nginx site Cloudfall already manages:

```yaml
apiVersion: cloudfall/v1
kind: Domain
metadata:
  id: search-kibana-site
spec:
  primaryName: kibana.example.test
  aliases: []
  proxy:
    server: h1
    configurationPath: /etc/nginx/sites-available/kibana.example.test.conf
    service: nginx.service
    upstream: { address: 127.0.0.1, port: 5601 }
  origin:
    server: h1
    configurationPath: /etc/nginx/sites-available/kibana-origin.conf
    service: nginx.service
    scheme: http
    port: 5601
    serverName: kibana.example.test
  edge: { provider: cloudflare, mode: dns-only }
  tls:
    mode: required
  healthCheck:
    scheme: https
    path: /login
    expectedStatuses: [200]
    timeoutSeconds: 5
```

Name it in the Kibana service with `domain: search-kibana-site`. Kibana takes
the whole hostname: a Domain with `routes`, another proxy server or upstream,
or another `tls.mode` is refused (`service_kibana_domain`). Apply the Domain
with `cloudfall domains apply` and converge the services: the role sets
`server.publicBaseUrl` to `https://kibana.example.test`, so the links Kibana
generates start with that address, and marks the session cookie secure.
Kibana itself still listens on loopback.

What that exposes: the login is Kibana's own form, checked against the users
of the cluster, and nothing else stands between the internet and that form.
Every declared password is then open to guessing from anywhere, so make each
one long. `config validate` warns `KIBANA_PUBLISHED` for a published Kibana
to say so. Limiting who may reach it by address, and a login in front of
Kibana's own, are not part of this service.

## A version change

Kibana runs at the `version` of the Elasticsearch service and follows a
change of it, in the order Elastic gives: every node first, then Kibana.
Change `version` in `search-main` to a later patch or minor of the same major
and converge the services:

- every node of the cluster moves to the new version first
- before Kibana's package changes, the role reads the version of every node
  (`GET /_nodes`). While a node is still older, Kibana stays as it is and the
  converge stops with `PRECONDITION` (`service_kibana_node_older`), naming the
  nodes behind. Converge them, then run the command again
- Kibana restarts on the new version and migrates its saved objects; the
  converge waits until Kibana reports itself available, and stops when it
  does not

A Kibana newer than the declared version, or of another major, is refused
before anything changes: Elastic does not support Kibana newer than a node,
nor against a cluster of another major.

## Where the dashboards are backed up

Elasticsearch groups Kibana's system indices as the feature state `kibana`.
The snapshots of the Elasticsearch service hold the cluster state and every
feature state, so the backup of the cluster is also the backup of the
dashboards. The Kibana service has no `backup`, timer or receipt of its own:

```console
uv run cloudfall backup run search-main
```

The snapshot asks for the cluster state with no list of feature states,
which Elasticsearch takes as every feature state, so once Kibana has started
against the cluster each snapshot lists the feature state `kibana`. The
restore drill of the Elasticsearch service restores renamed copies of data
indices only and never touches a feature state.

To see the feature states a snapshot holds, ask the node (on the Kibana
server, which is a node of its cluster):

```console
curl --user cloudfall http://127.0.0.1:9200/_snapshot/cloudfall/cloudfall-20261124-032000
```

`curl` asks for the password of Cloudfall's account, which is in
`/etc/elasticsearch/cloudfall-password` on the node (`sudo cat` it). Each
snapshot lists `feature_states`, one of them `"feature_name": "kibana"`. With
`remoteAccess`, use `https://<listenAddress>:9200` and
`--cacert /etc/elasticsearch/certs/ca.crt` instead.

## Restore the dashboards

The restore is three steps on the Kibana server: stop Kibana, restore the
feature state `kibana` from a snapshot, start Kibana. It brings back every
saved object as the snapshot holds it, and drops those made since. Nothing
else in the cluster changes: no data index and no other feature state is
restored.

1. Stop Kibana, so it writes nothing while its indices are replaced:

   ```console
   sudo systemctl stop kibana.service
   ```

2. Restore the feature state `kibana` alone. `"indices": "-*"` restores no
   data index, and `include_global_state: false` leaves the cluster's
   settings, templates and other feature states as they are. Elasticsearch
   deletes Kibana's current system indices and restores the snapshot's:

   ```console
   curl --user cloudfall -X POST \
     -H 'Content-Type: application/json' \
     'http://127.0.0.1:9200/_snapshot/cloudfall/cloudfall-20261124-032000/_restore?wait_for_completion=true' \
     -d '{"indices": "-*", "include_global_state": false, "feature_states": ["kibana"]}'
   ```

   The answer lists the restored shards under `snapshot.shards`; `failed`
   must be `0`

3. Start Kibana again:

   ```console
   sudo systemctl start kibana.service
   ```

   Kibana reports itself available once it has read the restored indices.
   A services converge also starts it and waits for that

Encrypted saved objects, such as an alert connector's credentials, open
again because Kibana still has the three encryption keys they were encrypted
with: the keys are in the project's secret store, not only on the server.

## A rebuilt Kibana server

Kibana encrypts sessions, saved objects and reports with three keys,
`xpack.security.encryptionKey`, `xpack.encryptedSavedObjects.encryptionKey`
and `xpack.reporting.encryptionKey`. `cloudfall secrets generate` makes each
once and stores it sops-encrypted in `secrets/<environment>/services/<kibana
service>.env`; it never overwrites a stored key. Every services converge puts
the stored keys in Kibana's keystore and never makes new ones on the server.

So when the Kibana server is rebuilt empty, converging its baseline and its
services brings Kibana back as it was:

- the node of the Elasticsearch service is converged first and rejoins its
  cluster, whose other nodes still hold Kibana's saved objects. A cluster of
  one lost them with the server: restore the feature state `kibana` from a
  snapshot in the cluster's repository as above, with Kibana stopped
- Kibana gets the same three keys from the store, so the saved objects
  encrypted before the rebuild still decrypt, and the dashboards open for the
  same users with the same passwords
- Kibana gets a new token of the `elastic/kibana` service account, created
  in the `service_tokens` file of the rebuilt node. The old token lived only
  in the old node's file, so it went with the old server and is valid nowhere

Keep the secrets repository: a key lost from the store can't be recovered,
and Kibana can no longer read what was encrypted with it.

## Audit

`cloudfall audit` reads each Kibana service from the observation of its
server. Inspect reads Kibana and its node as the `cloudfall` account, and
keeps names and privileges only: the keystore by setting name, never a
value, and of each user only the Kibana roles it holds.

| Check | Drift when |
| --- | --- |
| `kibana.version[<id>]` | The installed `kibana` package is not the Elasticsearch service's declared `version` |
| `kibana.unit[<id>]` | `kibana.service` is not enabled and running |
| `kibana.configuration[<id>]` | `/etc/kibana/kibana.yml` differs from the rendered one (a changed or removed setting, or one the role does not write, named without its value), or `node.options` gives another heap |
| `services.bind[<id>]` | Kibana listens on an address that is not loopback, such as after `server.host` was changed by hand and Kibana restarted |
| `kibana.status[<id>]` | The overall level of `GET /api/status` is not `available`, or Kibana does not answer |
| `kibana.roles[<id>]` | `cloudfall-kibana-view` or `cloudfall-kibana-edit` is missing or differs from what the role writes: another base privilege, a feature privilege, another space, a privilege in Elasticsearch, or another marker |
| `kibana.users[<id>]` | A declared user of the Elasticsearch service holds another Kibana role than its `access` gives, such as `support` given `cloudfall-kibana-edit` by hand, or a user with no `access` given one |
| `kibana.keystore[<id>]` | One of the three encryption keys is not in Kibana's keystore: an unmet S-011 requirement (the package requirement is `kibana.version`) |

A user the Elasticsearch service does not declare is not Cloudfall's: one
that holds a Kibana role is listed as unmanaged in the message, not counted
as drift. A check whose evidence inspect could not read, for example when the
`cloudfall` password is not on the server, is `unknown`, not drift.

```console
uv run cloudfall-engine inventory render --output tmp/ansible-inventory.json
uv run cloudfall-engine playbook run inspect --inventory tmp/ansible-inventory.json --extra-vars cloudfall_inspect_output_directory=$PWD/tmp/observed
uv run cloudfall audit --observed tmp/observed
```

Kibana's log goes to the journal (`journalctl -u kibana.service`), which the
logging collector already ships. The kind has no metrics.
