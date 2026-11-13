# Hybrid storage design for new two-drive servers

Status: approved design for future installations only.

This document defines the storage and failure model for new dedicated servers
with two equal local drives. It does not authorize repartitioning, formatting,
or changing any existing server. Use the separate
[new-server provisioning guide](new-server-storage-guide.md) when installing a
new or explicitly disposable host.

## Decision

Use a hybrid layout:

- mirror the bootable operating system with Linux software RAID1 across both
  drives;
- keep `/boot`, `/`, `/var`, `/var/log`, `/tmp`, `/home`, and `/srv/apps` on
  that mirrored system area;
- use the remaining tail of each drive as independent, unmirrored application
  storage;
- put each physical tail in its own LVM volume group; and
- rely on application replication to other servers for availability of data
  stored on those unmirrored volumes.

Do not create RAID0, a striped logical volume, or one LVM volume group spanning
both data tails. Those arrangements turn either drive failure into loss of the
whole storage pool.

## Failure contract

The design deliberately provides different guarantees for system and service
data.

| Event | Expected local result | Required fleet result |
| --- | --- | --- |
| Either drive fails | The RAID1 system remains bootable in degraded mode. | Cloudfall and hardware monitoring raise a critical alert. |
| A data tail is lost | Its filesystems do not mount and their dependent services do not start. | Replicas on other servers continue serving the affected datasets. |
| The other data tail remains healthy | Its filesystems and unrelated services remain available. | No workload may require both local data tails to operate. |
| The entire server is lost | No local service is available. | The cluster tolerates the complete host failure. |
| Data is deleted or corrupted and replication propagates it | Local mirroring would not help. | A separate, tested backup provides recovery. |

The accepted tradeoff is that MariaDB, ClickHouse, Elasticsearch, or another
storage service can fail locally when its unmirrored drive is lost. A service
must never silently start against the empty mount-point directory underneath a
missing filesystem. The provisioning guide prevents that with systemd mount
dependencies.

Replication is not a backup. Before assigning a dataset to unmirrored storage,
record its replica placement, verify that a single-host loss preserves quorum,
and confirm that a separate backup can recover logical deletion or corruption.

Declaration in Cloudfall config is the enforcement mechanism for all of the
above, and it is a precondition, not paperwork: a dataset the audit cannot see
is a dataset whose replication and backups rot silently — a snapshot
repository pointing at an unmounted path can sit dead for years while the
service keeps writing a single copy. No dataset may be placed on a data tail
before it is declared with its owning service, replica hosts, and backup
policy, so that audit and backup receipts continuously prove the failure
contract instead of assuming it.

## Recommended system footprint

For a general-purpose server with two 512 GB NVMe drives, start with this
mirrored allocation and adjust it before installation for measured workload
requirements:

| Mirrored allocation | Example size | Purpose |
| --- | ---: | --- |
| Swap | 16 GiB | Controlled memory pressure and crash handling; use 8–16 GiB unless the workload requires more. |
| `/boot` | 1 GiB | Kernels, initramfs images, and GRUB files. |
| `/` | 96 GiB | Debian, packages, logs, temporary files, application releases, and normal operational growth. |

Keep high-volume database, search, analytics, object, and cache data out of the
root filesystem. Increase root to 128–192 GiB when container images, build
artifacts, large package caches, or unusually high system-log retention make
96 GiB inadequate. Plan to keep at least 25% of root free during normal
operation.

Do not create separate unmirrored `/var/log`, `/tmp`, or `/home` partitions.
Their loss can break otherwise healthy services or prevent normal system
operation, while their space can be controlled with log rotation, retention,
quotas, and application-specific data paths.

## Capacity model

For two equal drives, calculate approximate usable capacity in GiB as follows:

```text
mirrored_system = swap + boot + root
tail_per_drive = drive_size - mirrored_system - boot_and_partition_overhead
logical_usable = mirrored_system + (2 * tail_per_drive)
efficiency = logical_usable / (2 * drive_size)
```

A marketed 512 GB drive is about 476.8 GiB. With the 113 GiB example system
footprint, each drive retains roughly 363.8 GiB before small alignment and
metadata costs. Logical usable capacity is therefore about 840.6 GiB, or 88%
of the two drives' raw capacity. Full-disk RAID1 would expose about 476.8 GiB,
or 50%.

This comparison counts local capacity only. The hybrid design still consumes
capacity on other servers for replicas and backups. Its advantage is that the
local system survives either drive while non-critical data uses both local
tails independently.

## Data-volume allocation rules

Create one physical volume and one volume group per physical data tail, for
example `vg_data0` and `vg_data1`. Create thick logical volumes within those
groups and mount them below `/srv/storage/<service>`.

Before provisioning, complete an allocation worksheet:

| Logical volume | Physical VG | Size | Mount point | Owning service | Replica hosts | Backup | Warning limit |
| --- | --- | ---: | --- | --- | --- | --- | ---: |
| `lv_mysql` | `vg_data0` | workload-specific | `/srv/storage/mysql` | MariaDB | required | required | 75% |
| `lv_clickhouse` | `vg_data0` or `vg_data1` | workload-specific | `/srv/storage/clickhouse` | ClickHouse | required | required | 75% |
| `lv_elasticsearch` | the other VG when practical | workload-specific | `/srv/storage/elasticsearch` | Elasticsearch | required | required | 75% |

Apply these rules:

1. Keep 10–15% of each volume group unallocated so an existing logical volume
   can grow without repartitioning.
2. Spread high-I/O services across the two physical drives when their replica
   topology permits it.
3. Do not place two replicas of the same dataset on the same server, even when
   they use different local drives.
4. Do not make one local service depend on volumes from both drives unless it
   is explicitly acceptable for either drive failure to stop that service.
5. Use thick LVs by default. Thin provisioning adds overcommitment and metadata
   failure modes and requires dedicated capacity monitoring.
6. Use ext4 for the default data filesystem. Set a zero reserved-block
   percentage only on dedicated data volumes, never on `/`.
7. Size filesystems from measured retention and growth, not merely from all
   currently available space.
8. Declare every tail-resident dataset in Cloudfall config — the owning
   `Service`, its replica hosts, and its backup policy — before it stores
   production data. An undeclared mount on a data tail is a provisioning
   defect: nothing audits it, nothing backs it up, and its failure contract
   exists only in someone's memory.
9. Keep data for services without working cross-server replication on the
   mirrored system area, not on a tail. PostgreSQL qualifies today: until the
   high-availability formation ships (M14 on the roadmap), its only recovery
   path is scheduled dumps, so a tail placement sets the loss window to the
   dump interval on a single drive. Reassess per service as replication
   becomes available.

## Boot and service semantics

System RAID1 and application replication solve different problems:

- RAID1 keeps the local operating system, configuration, logs, application
  binaries, SSH access, and monitoring available after either drive fails.
- `nofail` on unmirrored data entries lets Debian continue booting when a data
  drive is absent.
- `RequiresMountsFor=` on each storage service makes that service fail closed
  if its required data filesystem is absent.
- cluster replication and routing keep the fleet service available while that
  local instance is down.

The combination is intentional. `nofail` without service dependencies risks a
database writing into an empty directory on the mirrored root filesystem.
Service dependencies without `nofail` can turn an accepted data-volume loss
into a server boot failure.

## Operations requirements

Every server using this layout must have:

- GRUB installed and verified for boot from either physical drive;
- all software RAID arrays healthy before production activation;
- `mdadm` checks and actionable alert delivery;
- SMART/NVMe health monitoring for both physical drives;
- filesystem-usage alerts for every persistent mount;
- a documented mapping from drive serial to partition, VG, LV, mount, service,
  replicas, and backup;
- a successful new-server single-drive failure drill before production data is
  admitted; and
- a documented drive-replacement and replica-recovery procedure.

A software-only `mdadm --fail` test does not prove that firmware and GRUB can
boot from the other physical drive. The acceptance test must make one complete
drive unavailable, using the provider console or another controlled physical
method, while the server is still disposable.

## Cloudfall audit boundaries

Create a dedicated Cloudfall `Baseline` for this storage class. Do not reuse a
legacy profile whose root and RAID capacity thresholds describe a full-disk
mirror.

Cloudfall v1 observations are useful for ongoing evidence, but the current audit
does not prove the complete failure contract:

- `softwareRaid.minimumUsableBytes` is compared with the largest observed MD
  device, not the sum of all RAID devices;
- active-member evaluation does not establish that every MD array is healthy;
- mount checks establish filesystem type and capacity, not that the backing
  device is RAID1 or the intended physical drive;
- the audit does not parse `nofail` from `/etc/fstab`; and
- the audit does not validate each service's `RequiresMountsFor=` dependency.

Keep the manual activation checklist in the provisioning guide until those
constraints are represented directly in Cloudfall state and audit logic.

## Primary references

- [Hetzner Installimage](https://docs.hetzner.com/robot/dedicated-server/operating-systems/installimage/)
- [Hetzner software RAID](https://docs.hetzner.com/robot/dedicated-server/raid/software-raid/)
- [Linux MD administration](https://kernel.org/doc/html/next/admin-guide/md.html)
- [Debian `mdadm(8)`](https://manpages.debian.org/trixie/mdadm/mdadm.8.en.html)
- [Debian `fstab(5)`](https://manpages.debian.org/bookworm/mount/fstab.5.en.html)
- [systemd mount units and `nofail`](https://github.com/systemd/systemd/blob/main/man/systemd.mount.xml)
- [systemd `RequiresMountsFor=`](https://github.com/systemd/systemd/blob/main/man/systemd.unit.xml)
- [Debian LVM overview](https://manpages.debian.org/trixie/lvm2/lvm.8.en.html)

