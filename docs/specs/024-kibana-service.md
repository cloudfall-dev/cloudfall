# S-024: Kibana service

status: built
serves: G-1, G-3

## Problem

A `Service` is PostgreSQL, Redis, Elasticsearch or MariaDB (`serviceKind` in
`config/schemas/v1/service.schema.json`, `ServiceKind` in `sdk/src/cloudfall/domain.py`).
S-012 gave a team its Elasticsearch cluster and left Kibana out of scope. The people who
work with that data have nothing to open: an analyst who builds a dashboard, a developer
who reads an index in Discover. A team that wants Kibana runs it by hand: the package at
the version of the cluster, the account Kibana itself uses, three encryption keys, the
Node.js heap, a hostname with a certificate in front of it, and for each person a login
that opens Kibana and reads only that person's indices. None of it is declared, so `config
validate` and `audit` know nothing about it, and an agent has no operation to propose for
it.

Kibana differs from the kinds before it in three ways:

- It holds no data of its own. Dashboards, data views and saved searches are documents in
  system indices of the cluster, so they are in the snapshots S-012 already takes. The kind
  has no `backup`
- It has no version of its own. Kibana runs at the version of the cluster it reads, so the
  declaration names the Elasticsearch service and no version
- Its users are people with a browser, not applications on the fleet. They reach it through
  a `Domain` and log in with a user S-012 declares

## Behaviour

### Declaration

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
    domain: search-kibana-site
    users:
      - name: analyst
        access: edit
      - name: support
        access: view
```

`spec.kibana` is required for the kind and refused for the others:

| Field | Required | Meaning |
|---|---|---|
| `elasticsearch` | yes | The id of the Elasticsearch `Service` (S-012) Kibana reads; `spec.server` must be one of its nodes, and the environments must match |
| `heapMb` | yes | `--max-old-space-size` of Kibana's Node.js process, 1024 to 8192, written to `/etc/kibana/node.options` |
| `domain` | no | The id of the `Domain` that publishes Kibana (below). Absent means Kibana answers on loopback only |
| `users` | no | Which users of the Elasticsearch service may work in Kibana: `name`, and `access` of `view` or `edit` |

`backup`, `metrics` and `remoteAccess` are refused for this kind, and `backup` stays required
for the others. `bind.address` is a loopback address: Kibana never listens on another one.

A person who builds dashboards is two declarations: the user in the Elasticsearch service,
with a password and the indices it may read, and its line in `kibana.users`. In
`search-main`:

```yaml
elasticsearch:
  users:
    - name: analyst
      access: read
      indexPatterns: ["crm-*"]
      passwordRef: { environment: production, path: /services/search-main, key: ANALYST_PASSWORD }
```

### Version

Kibana is installed as `kibana=<version>` from the same Elastic APT repository as the node,
where `<version>` is the `version` of the Elasticsearch service, and held (`apt-mark hold`).
Elastic supports Kibana only against a cluster of its own major, and not when Kibana is newer
than a node, so the kind takes no `version` of its own and the supported lines of S-012
apply unchanged.

A change of the Elasticsearch service's `version` moves Kibana in the same converge, in the
order Elastic gives: every node first, then Kibana.

- `services.yml` converges a Kibana service after every node of its cluster
- Before it changes the package, the role reads the version of every node (`GET /_nodes`)
  and stops with `PRECONDITION` when a node is older than the declared version, leaving
  Kibana as it is
- Kibana migrates its saved objects when a new version starts. The role waits until Kibana
  reports itself available and stops the converge when it does not

### Connection to the cluster

Kibana runs on a node of its cluster and talks to that node only, on the address and with
the scheme the role's own calls use: plain HTTP on loopback, or HTTPS verified against the
project CA when the Elasticsearch service declares `remoteAccess`.

- It logs in with a token of the `elastic/kibana` service account that the role creates in
  the `service_tokens` file of that node (`elasticsearch-service-tokens create`), named
  after the Kibana service. The token is valid on that node only and is not kept in the
  cluster's security index
- The token goes into Kibana's keystore and into no file a person reads. It is created on
  the server that uses it and is not in the project's secret store: a rebuilt server gets a
  new one
- The password of the built-in `kibana_system` user is never set or changed. A cluster the
  team already had keeps whatever its own Kibana used
- When the node Kibana runs on is down, Kibana is down, whatever the other nodes do. The
  guide says so

### What the server needs

The kind yields S-011 requirements, so nothing below is prepared by hand:

| Requirement | Value |
|---|---|
| `package` | `kibana=<version>` from the repository of the Elasticsearch service's major |
| `secret` | `generate`, three: the keys Kibana encrypts sessions, saved objects and reports with (`xpack.security.encryptionKey`, `xpack.encryptedSavedObjects.encryptionKey`, `xpack.reporting.encryptionKey`) |

No volume, port or certificate is required: Kibana keeps its state in the cluster and
listens on loopback.

The three keys are generated once, stored sops-encrypted, and put into Kibana's keystore.
They stay the same across converges and rebuilds, so a rebuilt server logs nobody's saved
connectors out of reach and no session key changes by accident.

The role writes the managed settings to `/etc/kibana/kibana.yml` (`server.host`,
`server.port`, `server.publicBaseUrl`, `elasticsearch.hosts`, the CA when HTTPS is used,
and `telemetry.optIn: false`) and the heap to `/etc/kibana/node.options`. Where the heap
meets the host, it refuses, before changing anything, a `heapMb` that together with the
node's Elasticsearch heap exceeds three quarters of the server's memory.

One server holds at most one Kibana, and one Elasticsearch service has at most one Kibana
service.

### People and what they may do

Kibana has no accounts of its own. A person logs in with a user of the Elasticsearch
service, and what that person sees is decided by two things S-012 already converges: the
user's password and its role.

- The Kibana role keeps two roles, `cloudfall-kibana-view` and `cloudfall-kibana-edit`:
  Kibana's base privilege `read`, or `all`, on every space, and no privilege in
  Elasticsearch. It writes them through Kibana's own role API, because Elastic tells
  operators not to manage Kibana's privileges through the Elasticsearch role API
- A user named in `kibana.users` gets the role of its `access` next to its own role. The
  user converge of S-012 sets that list, as it sets the rest of the user; the user's own
  role is unchanged, so a dashboard shows that person only the indices its `indexPatterns`
  name
- `view` opens dashboards, Discover and saved objects, and changes nothing. `edit` also
  creates and changes data views, visualisations and dashboards, and every other saved
  object in Kibana
- Neither can manage users or roles: the role holds no cluster privilege. Dev Tools sends
  requests as the logged-in user, so it reaches what the role reaches
- A user of the Elasticsearch service that `kibana.users` does not name can log in and gets
  Kibana's page for an account with no access. An application's user has no reason to
  be listed
- Removing a line from `kibana.users` and converging takes the Kibana role from the user;
  the user and its index privileges stay
- A person may change their own password in Kibana. S-012 sets a password only when the
  stored one changes, so the person's choice stays until then; the guide says that the
  store then no longer holds the password in use
- `elastic` stays the break-glass account of S-012. Nothing here uses it, and the guide
  tells a person not to work in Kibana with it

### Reaching Kibana

Kibana listens on `bind.address`, a loopback address, in plain HTTP. Without `domain`
nothing else reaches it: an operator opens it through an SSH forward, which the guide
gives.

With `domain`, an existing `Domain` publishes it, with the certificate and the nginx site
Cloudfall already manages:

```yaml
apiVersion: cloudfall/v1
kind: Domain
metadata:
  id: search-kibana-site
spec:
  primaryName: kibana.example.test
  proxy:
    server: h1
    configurationPath: /etc/nginx/sites-available/kibana.example.test.conf
    service: nginx.service
    upstream: { address: 127.0.0.1, port: 5601 }
  tls:
    mode: required
```

- The Domain's `proxy.server` is the Kibana server, its upstream is the service's `bind`,
  and its `tls.mode` is `required`; anything else fails validation. Kibana takes the whole
  hostname: a Domain with `routes` is refused
- The role sets `server.publicBaseUrl` to `https://<primaryName>` and marks session cookies
  as secure
- The login is Kibana's own form, checked against the users of the cluster. Nothing else
  stands between the internet and that form, so `config validate` warns `KIBANA_PUBLISHED`
  for a published Kibana and the guide states the cost: every declared password is then
  open to guessing from anywhere, and it has to be long

### Saved objects and backups

Everything a person makes in Kibana is a document in the cluster's system indices, which
Elasticsearch groups as the feature state `kibana`. The snapshots of S-012 hold the cluster
state and every feature state, so `cloudfall backup run <elasticsearch service>` is also
the backup of the dashboards.

- This kind has no `backup`, no timer and no receipt of its own
- The guide gives the restore: stop Kibana, restore the feature state `kibana` from a
  snapshot, start Kibana. Encrypted saved objects open again because the three keys are in
  the project's store
- An Elasticsearch service that declares `existingCluster` can't have a Kibana service: the
  cluster's own Kibana still writes those indices, with keys Cloudfall does not hold. The
  team declares Kibana after the old nodes are gone and `existingCluster` is removed

### Validation

`cloudfall config validate` refuses with `CONFIG_INVALID`:

- `kibana` on another kind, or its absence on this one
- `backup`, `metrics` or `remoteAccess` on this kind (`service_kibana_field_refused`)
- `elasticsearch` that names no Elasticsearch service (`resource_reference_missing`), or
  one in another environment (`service_kibana_environment`)
- `spec.server` that is not a node of that service (`service_kibana_server_not_node`)
- an Elasticsearch service that declares `existingCluster`
  (`service_kibana_existing_cluster`)
- a second Kibana service for the same Elasticsearch service, or on the same server
  (`service_kibana_duplicate`)
- a `bind.address` that is not a loopback address (`service_kibana_bind`)
- a `heapMb` outside 1024 to 8192 (`service_kibana_heap`)
- a name in `users` that the Elasticsearch service does not declare, or that appears twice
  (`service_kibana_user_unknown`)
- a user of an Elasticsearch service named `cloudfall-kibana-view` or
  `cloudfall-kibana-edit` (`service_elasticsearch_user_reserved`, the S-012 code)
- a `domain` that names no Domain (`resource_reference_missing`), or one whose
  `proxy.server`, upstream or `tls.mode` is not as above, or that declares `routes`
  (`service_kibana_domain`)

It warns `KIBANA_PUBLISHED` when `domain` is declared.

### Audit

`audit` reports drift when the installed version differs from the Elasticsearch service's
`version`, when `kibana.service` is not enabled and running, when a managed file differs
from the rendered one, when Kibana listens on an address that is not loopback, when Kibana
does not report itself available (the overall level of `GET /api/status`), when one of the
two Kibana roles differs from what the role writes, when a declared user holds another
Kibana role than its `access` gives, and when a requirement of the server is unmet (S-011).

Kibana's log goes to the journal, which the collector already ships. The kind has no
metrics in this version.

### Documentation

`docs/kibana-service-guide.md` covers declaring Kibana next to a one-node Elasticsearch
service, declaring a person who views and one who builds dashboards, publishing it with a
Domain and what that exposes, opening it through an SSH forward, what happens to Kibana on
a version change, where the dashboards are backed up and how to restore them, and states
that the role installs Elastic's own packages under the licence S-012's guide names, and
which features of Kibana need a paid licence: single sign-on, PDF and PNG reports, most
alert connectors, and privileges narrower than a whole feature.

Touches `config/schemas/v1/service.schema.json`, `config/examples/services/`,
`sdk/src/cloudfall/domain.py`, `sdk/src/cloudfall/inventory.py`,
`sdk/src/cloudfall/validation.py`, `sdk/src/cloudfall/requirements.py`,
`sdk/src/cloudfall/secrets.py`, `sdk/src/cloudfall/audit.py`,
`engine/src/cloudfall_engine/ansible_inventory.py`, a new role
`engine/ansible/roles/cloudfall_kibana`,
`engine/ansible/roles/cloudfall_elasticsearch/tasks/users.yml`,
`engine/ansible/playbooks/services.yml`, ROADMAP.md, ARCHITECTURE.md, and the guide.

## Acceptance criteria

- S-024-1: A project with one `Server`, its baseline, the Elasticsearch service of S-012-1
  with the user `analyst`, and the declaration above without `domain` and `support` passes
  `cloudfall config validate` with exit 0, and `cloudfall-engine inventory render` carries
  the Kibana service, the Elasticsearch service it reads, and `analyst` with its Kibana
  access
- S-024-2: For the project in `config/examples` without a Kibana service, the rendered
  inventory is byte-identical before and after the change
- S-024-3: After `services.yml` converges S-024-1 on a fresh Debian 13 host, Kibana reports
  the version of the Elasticsearch service and itself as available, the package is held,
  the Node.js process runs with the declared heap, no listener for 5601 exists on a
  non-loopback address, and an unauthenticated request for a dashboard is sent to the login
  form
- S-024-4: A second converge changes nothing (`changed=0`) and restarts neither Kibana nor
  the node; a `heapMb` that with the node's heap exceeds three quarters of the host's
  memory stops the converge before any task changes the host
- S-024-5: Kibana is logged in to the node with a token of `elastic/kibana` that exists in
  the `service_tokens` file of that node; the token, the three encryption keys and every user's
  password appear in none of the rendered inventory, playbook output, command answers,
  `kibana.yml` or the journal; the hash of the `kibana_system` password is the same before
  and after the converge
- S-024-6: `analyst` with `access: edit` logs in, creates a data view on `crm-*` and a
  dashboard on it, and sees no document of `billing-1`; `support` with `access: view` opens
  that dashboard and is refused when saving a change to it
- S-024-7: A declared user that `kibana.users` does not name logs in and can open no
  application of Kibana; removing `analyst` from `kibana.users` and converging does the
  same to `analyst`, whose search of `crm-1` through Elasticsearch still works. The role of
  each declared user holds no application privilege, as S-012 leaves it
- S-024-8: With `domain` as above and the Domain applied, `https://kibana.example.test`
  answers the login form with a certificate for that name, `analyst` logs in through it,
  the session cookie is marked secure, and links Kibana generates start with that address
- S-024-9: Changing the Elasticsearch service's `version` to a later patch and converging
  leaves the node and Kibana on the new version, with Kibana changed after the node; the
  dashboard of S-024-6 opens afterwards. With a cluster of two nodes of which one is still
  on the old version, the Kibana package is not changed and the converge stops with
  `PRECONDITION`
- S-024-10: After `cloudfall backup run search-main`, the snapshot lists the feature state
  `kibana`. After the dashboard of S-024-6 is deleted and the feature state is restored
  from that snapshot by the guide's steps, the dashboard opens again
- S-024-11: After the Kibana server is rebuilt empty and converged, `analyst` logs in and
  the dashboard of S-024-6 opens; the three encryption keys in Kibana's keystore are the
  ones from the project's store
- S-024-12: Each refusal listed under Validation fails `cloudfall config validate` with
  `CONFIG_INVALID` and its code; a declaration with `domain` validates with the warning
  `KIBANA_PUBLISHED`
- S-024-13: `cloudfall audit` reports drift when the installed version differs from the
  Elasticsearch service's, when the unit is stopped, when `server.host` is changed by hand
  to a non-loopback address, and when `support` is given `cloudfall-kibana-edit` by hand

## Out of scope

- Kibana on a server that is not a node of the cluster, and more than one Kibana for a
  cluster
- A cluster Cloudfall did not create while `existingCluster` is declared, and moving the
  saved objects of another Kibana in
- Single sign-on (SAML, OpenID Connect) and every other feature of a paid licence
- Spaces in the declaration, and privileges narrower than all of Kibana: per space, per
  feature, or inside a feature (the last needs a paid licence)
- Dashboards, data views and other saved objects in the declaration: people make them in
  Kibana
- A restore of saved objects as a Cloudfall operation, and a drill for it
- Limiting who may reach a published Kibana by address, and a login in front of Kibana's
  own
- Kibana under a path of a hostname shared with something else
- Metrics, and alert rules about Kibana
- Fleet and Elastic Agent, APM, and the Security and Observability solutions: nothing here
  configures or refuses them
- Logstash and Beats
- A pass-through of Kibana settings in the declaration
- OpenSearch Dashboards
- Removing Kibana from a server

## Decisions relied on

- D-9: a user's password reaches the cluster only as a hash computed on the controller;
  this kind adds no password of its own

## Issues

- cloudfall-dev/cloudfall#567: S-024-1, S-024-2, S-024-12
- cloudfall-dev/cloudfall#569: S-024-3, S-024-4, S-024-5
- cloudfall-dev/cloudfall#572: S-024-6, S-024-7
- cloudfall-dev/cloudfall#573: S-024-8
- cloudfall-dev/cloudfall#579: S-024-9
- cloudfall-dev/cloudfall#580: S-024-10, S-024-11
- cloudfall-dev/cloudfall#582: S-024-13

## Verification

Checked on main at 785d7a0 with the audit of #582 on top. No criterion about a live host has
run on one: Kibana and Elasticsearch were not installed on a Debian 13 host for any of them.
They are proven by evaluating the role's conditions, templates and host variables with
Ansible's own templating, and by replaying the role's tasks in `ansible-playbook` on
localhost against stand-ins: `kibana-keystore` and `elasticsearch-service-tokens`
(`engine/tests/kibana_node.py`), an HTTP stand-in answering Elasticsearch's security API and
Kibana's role API (`test_s024_kibana_access.py`), a stand-in node answering `GET /_nodes`
(`test_s024_9_kibana_version_change.py`), and the fake node of the S-012 snapshot tests. A
live proving run is still owed before the stable release. Per criterion:

- S-024-1: `test_s024_1_the_declaration_validates` runs `cloudfall config validate` on the
  declaration (exit 0), and `test_s024_1_render_carries_kibana_the_cluster_and_analyst` runs
  `cloudfall-engine inventory render`: the Kibana service, `search-main` and `analyst` with its
  Kibana access are in it (`test_s024_1_*` in `test_s024_kibana.py` cover the inventory too).
  Holds
- S-024-2: `test_s024_2_examples_without_kibana_render_byte_identical` renders
  `config/examples` and compares it with the golden inventory byte for byte; `cloudfall config
  validate --project config/examples` exits 0. Holds
- S-024-3: on templates and stand-ins only, not on a fresh Debian 13 host: the package task
  installs `kibana=<the service's version>` and holds it, `kibana.yml` answers on loopback and
  reads the node, `node.options` gets the declared heap, a listener off loopback and a
  dashboard request not sent to the login form stop the converge, and the converge waits until
  Kibana reports itself available at the version (`test_s024_3_*` in
  `test_s024_kibana_role.py`); the server's requirements are the package at the cluster's
  version and three keys (`test_s024_3_the_server_needs_kibana_at_the_clusters_version_and_three_keys`).
  A real Kibana's status, Node.js process, listener and login page were not observed
- S-024-4: replayed on the stand-ins: a second run of the keystore and token tasks gives
  `changed=0` and notifies no restart
  (`test_s024_4_a_second_converge_changes_nothing_and_restarts_nothing`), and the heap refusal
  and every other refusal come before any task that changes the host
  (`test_s024_4_*`). A whole `services.yml` run with no restart was not checked on a live host
- S-024-5: on the stand-ins: Kibana gets a token of `elastic/kibana` from the node's
  `service_tokens` file, made again when the keystore lost it, every task that sees a secret is
  `no_log`, no secret is in `kibana.yml`, and no task touches `kibana_system`
  (`test_s024_5_*` in `test_s024_kibana_role.py`); the keys are generated secrets of the store,
  staged `0600`, in no output, and a missing one stops before any playbook (`test_s024_5_*` in
  `test_s024_kibana_converge.py`); the node read is `no_log`
  (`test_s024_5_the_node_read_is_no_log`), and inspect keeps no password or digest in the
  evidence audit reads (`test_s024_5_inspect_keeps_no_password_or_digest_in_the_evidence`; a
  hand-written setting's value is in no finding,
  `test_kibana_yml_changed_by_hand_is_drift`). The journal and the `kibana_system` hash of a
  live node were not read
- S-024-6: replayed against the HTTP stand-in, not a live Kibana: both roles are written
  through Kibana's role API with no index privilege, `analyst` holds the edit role and
  `support` the view role next to their own roles, and a second converge puts nothing
  (`test_s024_6_*`). Logging in, building a data view and a dashboard, not seeing `billing-1`,
  and the refused save were not tried
- S-024-7: replayed against the same stand-in: a user `kibana.users` does not name keeps its
  own role alone, removing `analyst` takes only its Kibana role, and `services.yml` hands the
  cluster's home the access its Kibana names (`test_s024_7_*`). Opening Kibana as such a user
  and the search of `crm-1` were not tried live
- S-024-8: on templates: `kibana.yml` links to `https://kibana.example.test` with secure
  cookies, an unpublished Kibana keeps its loopback settings, a Domain the server does not
  hold is refused, and the Domain's nginx site fronts Kibana with the certificate of its name
  (`test_s024_8_*`). The certificate, a login through it and the cookie in a browser were not
  checked live
- S-024-9: replayed against the stand-in node: the nodes older than the declared version are
  named and the converge stops with `PRECONDITION` and Kibana's package unchanged, every node
  is read before the package changes, the held package moves only once every node is on the
  new version, Kibana converges after every node, and the converge waits for its migration
  (`test_s024_9_*`). A real package upgrade, the saved-object migration and the dashboard
  afterwards were not run on a live host
- S-024-10: S-012's backup script runs against the fake node, whose snapshot then lists the
  feature state `kibana`, and asks for every feature state; the guide's restore steps stop
  Kibana, restore the feature state `kibana` alone and start Kibana (`test_s024_10_*`). A
  restore on a live cluster and the dashboard opening again were not tried
- S-024-11: on the stand-ins: a rebuilt empty host gets the store's three keys in its keystore
  and a new token, and a second converge keeps them (`test_s024_11_*` in
  `test_s024_10_11_kibana_dashboards.py`); every converge stages the stored keys and the store
  never replaces a held key (`test_s024_11_*` in `test_s024_11_kibana_rebuild_keys.py`).
  `analyst` logging in on a rebuilt live server was not tried
- S-024-12: every refusal under Validation runs `cloudfall config validate` and asserts
  `CONFIG_INVALID` and its code, and a declaration with `domain` validates with
  `KIBANA_PUBLISHED` (`test_s024_12_*`). Holds
- S-024-13: `audit` over observation fixtures reports drift for a Kibana version other than
  the Elasticsearch service's declared one, a stopped or disabled unit, `server.host` changed
  by hand to `0.0.0.0` (`kibana.configuration`, and `services.bind` once Kibana listens
  there), and `support` given `cloudfall-kibana-edit` by hand, while a Kibana converged as
  declared, its `kibana.yml` rendered from the role's own template in every shape, is
  compliant (`test_s024_13_*` in `test_s024_13_kibana_audit.py`); the other Audit cases
  (Kibana's status, the two roles, a user without access given a role, the heap, the keystore)
  are covered there too, and an Elasticsearch-only project gets no `kibana.` check. Inspect's
  reading of Kibana's files, keystore, status, roles and users runs through Ansible's
  templating (`test_s024_13_inspect_reads_kibanas_files_keystore_status_roles_and_users`).
  Not against a live inspect

Still open before the stable release:

- S-024-3 to S-024-11, S-024-13: a proving run on a live Debian 13 host with Elasticsearch and
  Kibana, which none of these criteria has had
