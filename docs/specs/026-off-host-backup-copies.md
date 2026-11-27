# S-026: Off host backup copies

status: approved
serves: G-3, G-2

## Problem

Every service backup Cloudfall converges writes to `backup.directory` on the server it protects
and stops there (cloudfall-dev/cloudfall#225). A lost disk or a lost server loses the data and its
backups together. A failed backup or restore-drill run shows only in the journal: the units set no
`OnFailure=`, so nobody hears of it, and nothing records when a copy last left the host.

The maintainer wants the off-host copy now (#225, 2026-11-21). The first targets are Hetzner
Storage Boxes: one free 100 GB box per dedicated server, reached by SFTP and rsync over SSH on
port 23, a sub-account per tenant with its own directory, login and `authorized_keys`. Two
settled points shape this spec:

- Cloudfall only writes to a target. Setting a box up (SSH on, a sub-account per tenant, its key)
  stays a manual step in Hetzner Robot, so Cloudfall needs no Robot login and holds no credential
  that could cancel a server
- Encryption is chosen per data set, not imposed on every copy

The restore-drill and globals part of #225 already landed in #236.

## Behaviour

### The target

A new kind, `BackupTarget` (`backup-targets/`, checked by `config/schemas/v1/backup-target.schema.json`),
declares one place a copy may go. It is the location Cloudfall is given, set up by a person
beforehand:

```yaml
apiVersion: cloudfall/v1
kind: BackupTarget
metadata:
  id: tenant-acme
spec:
  sftp:
    host: u123456-sub1.your-storagebox.de
    port: 23
    user: u123456-sub1
    path: /home/backups
    hostKey: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA..."
    quotaGb: 100
```

- `sftp.host`, `sftp.user` and `sftp.path` (absolute) are required; `port` defaults to 22, and a
  Storage Box needs `23`
- `hostKey` is required: the target's public host key, one `known_hosts` key line. Every
  connection runs with `StrictHostKeyChecking=yes` against it, so a copy never goes to a host
  that only claims the name
- `quotaGb` is optional; when set, the copy step stops before an upload that would take the
  target's used space over it (below)
- The login key is a secret requirement (S-011) of every server that copies to the target:
  source `input`, kept in `secrets/<environment>/backup-targets/<id>.env` as `SSH_PRIVATE_KEY`,
  set once with `cloudfall secrets input backup-target-ssh-key --owner <id>`. Its public half is
  what the person adds to the sub-account's `authorized_keys`
- One target is one login. Tenants kept apart on one box are two targets, one per sub-account,
  so a server holding one tenant's key can't read another tenant's copies

### A service's copy

A `Service`'s `backup` (`config/schemas/v1/service.schema.json`) gains an optional `offHost`:

```yaml
backup:
  directory: /var/backups/postgresql/billing
  onCalendar: daily
  retentionDays: 7
  offHost:
    target: tenant-acme
    retentionDays: 30
    encryption:
      age:
        recipients:
          - age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p
```

- `target` names a declared `BackupTarget`; `retentionDays` (1 to 3650) is how long copies stay
  on the target, independent of the local `retentionDays`
- `encryption` is required, so a plaintext copy is a choice someone wrote down: either
  `encryption: none`, or `encryption.age.recipients`, one or more age public keys. The server
  holds only the public keys, so it can encrypt a copy and never decrypt one; the identity that
  decrypts stays with the person or the controller
- `offHost` applies to the kinds whose backup writes files to `backup.directory`: `postgresql`,
  `redis`, `mariadb` and `rabbitmq`. On another kind, `config validate` refuses it

### The copy step

On a server with a service that declares `offHost`, the service's role (`engine/ansible/roles/
cloudfall_postgresql`, `cloudfall_redis`, `cloudfall_mariadb`, `cloudfall_rabbitmq`) installs
`/usr/local/sbin/cloudfall-backup-copy` (from a shared `cloudfall_backup_copy` role) and runs it at
the end of the backup script (`templates/backup.sh.j2`), after the local backup succeeded:

1. Takes the files this run wrote (the run's timestamp), and with `age` recipients encrypts each
   to `<file>.age` in a private staging directory; with `none` it copies them as they are
2. Before uploading, reads the target's used space (`df` over the target's restricted SSH, or the
   SFTP `df` extension); with `quotaGb` set and the upload not fitting, it exits non-zero naming
   the target, the bytes needed and the bytes free, and uploads nothing
3. Uploads them with `rsync` over SSH to `<path>/<service id>/`, keeping their modification time
4. Lists `<path>/<service id>/` over SFTP and checks each uploaded file is there with its size
5. Removes from `<path>/<service id>/` the files whose modification time is older than
   `offHost.retentionDays`, and nothing outside that directory
6. Writes `/var/lib/cloudfall/backups/<service id>/off-host.json` (`0644`): the time, the target
   id, host and directory, the encryption (`none`, or the recipients), and per file its name,
   size and SHA-256 as uploaded (the ciphertext when encrypted)

A failed step exits non-zero, so the backup unit fails; the local backup stays as it was written.
The login key reaches the server at `/etc/cloudfall/backup-targets/<id>/ssh-key` (`0600`, owned by
the backup unit's user), through the S-011 provider: `services` converge stops before any change
with exit 4 and `INPUT_REQUIRED` when it isn't in the store. The key, its path's content and the
staged plaintext appear in no `argv` of another user, journal line, rendered inventory, command
answer, receipt or observation. The staging directory is removed when the step ends.

### A failed run is heard

On a server whose `Baseline` names an alert channel (S-015), every backup and restore-check unit of
the four kinds sets `OnFailure=cloudfall-unit-failed@%n.service`. That template unit, installed by
the `cloudfall_alerting` role, sends one message through `/usr/local/sbin/cloudfall-alert` naming
the host's FQDN, the failed unit and the last lines of its journal (`journalctl -u <unit> -n 20`).
On a server with no channel, the units stay exactly as today. This holds whether or not the
service declares `offHost`.

### Inspect and audit

- `inspect` records each service's `off-host.json` (never the key) and whether the key file
  exists
- `audit` adds `backup.offHost` for each service with `offHost`: compliant when `off-host.json`
  names the declared target and its time is within `offHost.staleAfterHours` (optional, default
  `26`, 1 to 8760) of the observation; drift when it is older, missing, or names another target;
  unknown without evidence. A timer that never fires shows here, where `OnFailure=` can't see it

## Acceptance criteria

- S-026-1: A `BackupTarget` with `sftp.host`, `user`, an absolute `path` and a `hostKey` passes
  `config validate`; one missing any of them, with a relative path, or with a `hostKey` that is
  not one key line fails it with `CONFIG_INVALID`; a `Service` whose `backup.offHost` names an
  undeclared target, lacks `encryption`, gives an `age` recipient that is not an `age1` public
  key, or is on a kind other than `postgresql`, `redis`, `mariadb` or `rabbitmq` fails it with
  `CONFIG_INVALID`
- S-026-2: `requirements show` lists `backup-target-ssh-key` as `needs-input` on every server
  with a service whose `offHost` names the target, once per server, and `met` after `secrets
  input`; `services` converge without it stored exits 4 with `INPUT_REQUIRED` and changes nothing
- S-026-3: `services` converge with the key stored installs `cloudfall-backup-copy`, the key file
  (`0600`, the backup unit's user) and a backup script that calls the copy step after the local
  backup, and a second run changes nothing; a service with no `offHost` gets the same backup
  script as before
- S-026-4: A backup run against an SFTP server with the declared host key uploads that run's files
  to `<path>/<service id>/` and writes `off-host.json` with each file's size and SHA-256 matching
  the uploaded file; with `age` recipients the uploaded files are `.age` files that `age
  --decrypt` with a matching identity turns back into the local backup byte for byte; with
  `none` they equal the local files
- S-026-5: A target whose host key differs from `hostKey`, an unreachable target, or an upload
  that `quotaGb` would exceed makes the backup unit exit non-zero with the target named, uploads
  nothing, leaves the local backup in place and does not rewrite `off-host.json`
- S-026-6: After a copy, the files in `<path>/<service id>/` older than `offHost.retentionDays` are
  gone and younger ones and every file outside that directory remain
- S-026-7: On a server whose baseline names an alert channel, every backup and restore-check unit
  of the four kinds has `OnFailure=cloudfall-unit-failed@%n.service`, and a failing backup run
  sends one message through `cloudfall-alert` starting with the host's FQDN and naming the unit;
  on a server with no channel the units render exactly as before
- S-026-8: The login key and, for an encrypted copy, the plaintext of the backup appear in no
  `ansible-playbook` argv, rendered inventory, command answer, receipt, observation or journal
  line, and the staging directory is gone after the copy step, failed or not
- S-026-9: `audit` reports `backup.offHost` compliant when `off-host.json` names the declared
  target within `staleAfterHours`, drift when it is older, missing or names another target, and
  unknown without evidence; a service with no `offHost` gets no `backup.offHost` check

## Out of scope

- Setting a target up (enabling SSH, creating a sub-account, adding its key) and any use of the
  Hetzner Robot webservice: a person does it in Robot, decided on #225
- Restoring from an off-host copy, and a restore drill that reads one: a later spec, since an
  encrypted copy needs the age identity, which this spec keeps off the server
- Which tenant's data sets are encrypted: each project chooses per service with `encryption`
- S3-compatible targets: the kind leaves room for an `s3` block in a later spec
- Elasticsearch snapshot repositories, and the ClickHouse `BACKUP` of S-022: they are not files in
  `backup.directory` this way; each gets its copy in its own spec
- An alert rule on backup age from the logging stack's metrics: `backup.offHost` in `audit` covers
  a copy that stopped arriving until backup metrics exist
- Placing tenants' data on boxes (which box holds which copy): that is the project's choice of
  `target`, not something Cloudfall decides

## Decisions relied on

- D-10: tenants are kept apart by a target per sub-account, as components are kept apart by a
  user and a key of their own
- D-16: `off-host.json` and every record hold names, sizes and digests, never a key

## Issues

The build issues, filled in once they are filed: one `- owner/repo#N: S-026-1, S-026-2` per
line, naming the criteria that issue delivers. Each criterion belongs to exactly one.

## Verification

Filled in when the spec reaches `built`: one line per criterion id, saying how it was
checked against the default branch (beyond its tests) and the result.
