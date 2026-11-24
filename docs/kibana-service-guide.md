# Run a Kibana service

Status: this guide covers where Kibana's dashboards are backed up, how to
restore them, and what a rebuilt Kibana server gets. The backup check and the
keys of a rebuilt server are proven against stand-ins of Elasticsearch and of
Kibana's keystore, not yet on a live Debian 13 host. The other sections the
spec names (declaring Kibana, its users, publishing it with a Domain, an SSH
forward, a version change and licences) come with later releases.

A Kibana `Service` runs on a node of the Elasticsearch service it reads and
holds no data of its own: dashboards, data views, saved searches and every
other saved object are documents in the cluster's system indices. The
behaviour is specified in [spec 024](specs/024-kibana-service.md).

## Where the dashboards are backed up

Elasticsearch groups Kibana's system indices as the feature state `kibana`.
The snapshots of the Elasticsearch service hold the cluster state and every
feature state, so the backup of the cluster is also the backup of the
dashboards. The Kibana service has no `backup`, timer or receipt of its own:

```console
uv run cloudfall backup run search-main
```

When a Kibana service reads the cluster, `cloudfall-elasticsearch-backup`
also checks that the snapshot it took lists the feature state `kibana`, and
fails the run when it does not: Kibana then never started against the
cluster, or its system indices are gone. The snapshot itself is unchanged: it
asks for the cluster state and every feature state, as for a cluster without
Kibana. The restore drill of the Elasticsearch service restores renamed
copies of data indices only and never touches a feature state.

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
