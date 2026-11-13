# S-014: Data volumes in a Baseline's storage pools

status: approved
serves: G-1, G-3

## Problem

S-011 lets a workload say what it needs from its host, and Cloudfall meets every requirement
type but one: a `volume` (`sdk/src/cloudfall/requirements.py`). Loki and Prometheus each need a
filesystem of their own, sized, on the right disk, mounted before their unit starts. Today a
person makes each one by hand before the first deploy (`lvcreate`, `mkfs`, an fstab line,
`RequiresMountsFor`), as the storage guide (`docs/new-server-storage-guide.md`) shows, and
nothing checks it afterwards. A data directory that is not mounted when its service starts
fills the root filesystem; a linear volume that LVM spreads over both disks is lost with either.

cloudfall-dev/cloudfall#201 asks for this together with other host settings (sysctl, core dumps,
journald, SSH options, a full upgrade at bootstrap). This spec takes the volumes alone, the part
S-011 waits on; the other settings stay with #201.

## Behaviour

### Pools on the Baseline

A `Baseline` may declare storage pools: volume groups the install already made, which Cloudfall
fills but never creates or changes.

```yaml
spec:
  storage:
    pools:
      - id: data
        volumeGroup: vg_data
        primary: /dev/nvme0n1p5
        secondary: /dev/nvme1n1p5
```

- `volumeGroup` names an existing LVM volume group; `primary` and `secondary` name two of its
  physical volumes. `secondary` is optional: a pool without it can hold only expendable volumes
- A volume requirement's placement maps to an LV type: `mirrored` is `raid1` over `primary` and
  `secondary`; `expendable` is linear on `primary` alone. Every `lvcreate` and `lvextend` names
  its physical volumes, so LVM never spreads a linear volume over both disks
- A workload names the pool its volumes go to (`LoggingStack` `spec.backend.storage.pool`,
  `spec.metrics.backend.storagePool`); with one pool on the server's baseline it may leave the
  name out

### Converge

The volume provider runs before the workload's role (the same place S-011's other providers
run) and, for each volume requirement on a server:

- creates `lv_<name>` in the pool's volume group when it is missing, of the requirement's
  minimum size, with the requirement's placement; makes an ext4 filesystem with label `<name>`
  and no reserved blocks (`-m 0`) on that new LV only
- grows an existing `lv_<name>` smaller than the requirement with `lvextend -r`, naming the same
  physical volumes; it never shrinks one
- adds an fstab line by filesystem UUID with `nofail,x-systemd.device-timeout=10s`, mounts it,
  and adds a `RequiresMountsFor=<path>` drop-in to every unit the requirement names
- sets the mount's owner to the workload's service user, as the role sets it on a plain
  directory today

### Refusals

The provider changes nothing and stops, naming the server, the pool and the reason, when:

- the volume group or a named physical volume does not exist on the server
- the free extents on the physical volumes a placement needs are fewer than the volume needs
- `lv_<name>` exists with another type (`linear` where `raid1` is required, or the reverse), on
  other physical volumes, or with a filesystem other than ext4
- the mount path is a non-empty directory that is not already this volume's mount: mounting
  over it would hide data
- the path is already a mount point of another device
- a `mirrored` volume is asked of a pool without `secondary`

It never runs `pvcreate`, `vgcreate`, `vgextend`, `lvremove`, `lvreduce`, `mkfs` on an LV it did
not just create, or `wipefs`.

### Plan, show and audit

- `cloudfall requirements show` and the `logging deploy` plan give each volume the pool and LV
  it maps to; its state stays `unchecked` there, since only the host knows it
- `inspect` collects `lvs` (name, type, size, devices, health) and `findmnt` for the declared
  pools and mount paths; `audit` adds `volume.<name>`: compliant when the LV exists with its
  type and physical volumes, at least its size, healthy (empty `lv_health_status`), mounted at
  its path; drift otherwise
- `config validate` refuses a volume requirement on a server whose baseline declares no pool,
  or names a pool the baseline lacks, with a code naming the requirement (S-011-8)

## Acceptance criteria

- S-014-1: A `Baseline` with `storage.pools` passes `config validate`; a pool whose `primary`
  or `secondary` is not an absolute device path, or two pools with one `volumeGroup`, fail it
  with `CONFIG_INVALID`
- S-014-2: A `LoggingStack` whose backend's baseline declares no pool fails `config validate`
  with a code naming the `loki` volume requirement; naming a pool the baseline lacks fails the
  same way
- S-014-3: `requirements show` and the `logging deploy` plan list the `loki` and `prometheus`
  volumes with their pool, LV name and LV type
- S-014-4: On a Debian 13 host whose `vg_data` has free space on both physical volumes,
  `logging deploy --yes` creates `lv_loki` and `lv_prometheus` linear on `primary` only
  (`lvs -o devices`), ext4, mounted by UUID with `nofail`, owned by their service users, and
  `systemctl show loki.service -p RequiresMountsFor` names `/var/lib/loki-cloudfall`
- S-014-5: A second deploy changes nothing; raising a volume's size grows the LV and its
  filesystem online, on the same physical volumes; lowering it changes nothing
- S-014-6: A `mirrored` volume becomes `raid1` with `rimage_0` on `primary` and `rimage_1` on
  `secondary`
- S-014-7: Each refusal above stops the deploy before any change on that server, and its error
  names the server, the pool and the refusal; a mount path holding files is never mounted over
- S-014-8: `audit` reports `volume.loki` compliant after S-014-4, and drift when the LV is
  unmounted, smaller than required, of the wrong type, or degraded
- S-014-9: No command of this spec runs `pvcreate`, `vgcreate`, `vgextend`, `lvremove`,
  `lvreduce` or `wipefs`, and `mkfs` only on an LV created in the same run

## Out of scope

- The other host settings of cloudfall-dev/cloudfall#201 (sysctl, core dumps, journald caps, SSH
  options, a full upgrade at bootstrap)
- Creating or extending volume groups and physical volumes: the install guide's job, by hand
- Shrinking, moving or removing a volume, and converting between `linear` and `raid1` (the guide
  documents `lvconvert` for dropping a mirror once data has a replica elsewhere)
- Filesystems other than ext4, encryption at rest, and volumes for `Service` and `Component`
  kinds: same mechanism, later specs

## Decisions relied on

- none

## Issues

## Verification

Not built yet. Landed so far, checked by unit tests on the controller:

- `storage.pools` on a `Baseline` (schema, the `StoragePool` model, unique `id` and
  `volumeGroup`), the `pool` and `storagePool` fields of `LoggingStack`, and the pool refusals
  of `config validate` (`volume_requirement_pool_missing`, `_unknown`, `_ambiguous`): S-014-1,
  S-014-2
- Each volume requirement carries its pool, `lv_<name>`, LV type and physical volumes, listed
  by `requirements show` and the `logging deploy` plan: S-014-3
