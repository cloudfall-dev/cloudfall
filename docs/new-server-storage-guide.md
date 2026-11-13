# Provision hybrid disk space on a new Hetzner server

This guide provisions a new two-drive Debian server so the operating system can
boot from either drive while the remaining disk space is used as independent,
unmirrored storage for replicated services.

> **Destructive, new-server-only procedure.** Hetzner Installimage completely
> wipes every selected `DRIVE`. Later `sgdisk`, `pvcreate`, `lvcreate`, and
> `mkfs` commands also change storage. Do not run any command from this guide on
> an existing server. Use it only on a new or explicitly disposable
> server after verifying both drive serial numbers.

Read the [hybrid storage design](hybrid-storage-design.md) before using this
guide. The design records the accepted failure contract: the host remains
bootable after either drive fails, but services whose unmirrored data was on
that drive may remain stopped while replicas on other servers provide fleet
availability.

This is the one procedure in Cloudfall that a human runs by hand and that no
agent should ever run. Cloudfall is the operator's record for a fleet run by
an AI agent, and a record only makes sense for operations that are declared,
gated and verifiable; wiping two drives on a machine that is not yet in any
inventory is none of those. There is no Cloudfall command, no MCP tool and no
operator policy that reaches this layer, by construction. What the guide
does instead is get the server to the point where the record can start:
step 11 registers it and takes the first observation, and step 12 is the
verify step for everything before it. Keep the installation record and the
failure-test evidence; they are the first entries about this host, written
by you.

Bare-metal RAID and storage provisioning is the one implemented layer not
yet exercised in a live proving run (see the [roadmap](../ROADMAP.md)).

## 1. Complete the installation record

Record these values before opening the Rescue System:

```text
Server name:
Provider server ID:
IPv4 and IPv6 allocation:
Debian version:
Boot mode: BIOS or UEFI

Drive 0 device:
Drive 0 model:
Drive 0 serial:
Drive 0 reported size:

Drive 1 device:
Drive 1 model:
Drive 1 serial:
Drive 1 reported size:

Swap size:
/boot size:
/ size:

Data VG 0 allocations:
Data VG 1 allocations:
Replica hosts for every data service:
Backup and restore procedure:
```

Use two equal drives for the standard layout. If the sizes differ, RAID1 and
paired partition creation are constrained by the smaller drive; stop and make
a server-specific plan rather than copying the example sizes.

For two nominal 512 GB drives, the default starting point is 16 GiB swap, 1 GiB
`/boot`, and 96 GiB `/`. Increase root before installation when workload
evidence requires it. Keep `/var`, `/var/log`, `/tmp`, `/home`, and `/srv/apps`
inside mirrored root.

## 2. Inspect the new hardware in Rescue

Activate Hetzner's Linux Rescue System, reboot into it, and connect as root.
Identify the disks by device path and serial:

```console
lsblk -d -o NAME,PATH,SIZE,MODEL,SERIAL,ROTA,TRAN
ls -l /dev/disk/by-id/
smartctl -x /dev/nvme0n1
smartctl -x /dev/nvme1n1
```

Stop if either device is absent, the serials do not match the installation
record, SMART reports a failed health assessment, or an NVMe critical warning
is nonzero. Replace unhealthy new hardware before installing.

The commands below assume the verified devices are `/dev/nvme0n1` and
`/dev/nvme1n1`. Substitute the actual device names; do not infer disk identity
from the order alone.

## 3. Install the mirrored system area

Start the provider installer:

```console
installimage
```

Select the supported Debian image. In the generated configuration, retain the
selected image path and provider-generated boot settings, and set the relevant
storage lines to this pattern:

```text
DRIVE1 /dev/nvme0n1
DRIVE2 /dev/nvme1n1

SWRAID 1
SWRAIDLEVEL 1

BOOTLOADER grub
HOSTNAME new-host.example.net

PART swap swap 16G
PART /boot ext4 1G
PART / ext4 96G
```

Important checks in the editor:

- both verified physical drives are active `DRIVE` entries;
- software RAID is enabled at level 1;
- there is no `PART ... all` line;
- no LVM entry consumes the free tail;
- the hostname, Debian image, network settings, and SSH key are correct; and
- any provider-generated BIOS/UEFI boot partition settings are preserved.

Hetzner applies `SWRAID` to all selected drives and documents that omitting an
`all` partition leaves the remaining space unused. Save the configuration only
after calculating that the explicit system partitions fit on both disks.
Installimage validates the file and then wipes and installs the selected
drives.

Reboot into Debian when installation completes.

## 4. Validate boot and RAID before using the free tails

Run the following checks as root:

```console
cat /proc/mdstat
mdadm --detail --scan
lsblk -o NAME,PATH,TYPE,SIZE,FSTYPE,FSVER,MOUNTPOINTS
findmnt --verify --verbose
findmnt /
findmnt /boot
swapon --show
```

Do not continue unless:

- every system MD array reports RAID1 with both members active, normally
  displayed as `[UU]` in `/proc/mdstat`;
- no resync, recovery, or reshape is incomplete;
- `/` and `/boot` are mounted from MD-backed filesystems;
- swap is active on the intended mirrored device; and
- `findmnt --verify` reports no errors or filesystem-type mismatches.

Confirm bootloader coverage. On a BIOS/GRUB installation, the Debian selection
should include both disks:

```console
debconf-show grub-pc | grep 'grub-pc/install_devices'
```

UEFI installations use EFI system partitions and firmware boot entries rather
than the same `grub-pc` selection. Inspect the generated layout and entries:

```console
findmnt /boot/efi
efibootmgr -v
lsblk -o NAME,PATH,PARTTYPE,FSTYPE,MOUNTPOINTS
```

Do not assume that seeing two MD members proves that either physical disk can
boot. The controlled failure test in step 12 is the acceptance test: a layout
that looks right is not an outcome, a server that boots on either drive is.

Install the storage and monitoring tools if the selected image does not already
provide them:

```console
apt-get update
apt-get install --yes gdisk lvm2 mdadm smartmontools
```

## 5. Create one data partition in each free tail

First display all free regions. The final free region on each disk should be
approximately equal:

```console
parted /dev/nvme0n1 unit GiB print free
parted /dev/nvme1n1 unit GiB print free
sgdisk --print /dev/nvme0n1
sgdisk --print /dev/nvme1n1
```

Stop if the layout differs between disks, the free region overlaps an existing
partition, or its size does not match the capacity plan.

Create one partition in the largest free region of each disk:

```console
sgdisk --largest-new=0 /dev/nvme0n1
sgdisk --largest-new=0 /dev/nvme1n1
partprobe /dev/nvme0n1
partprobe /dev/nvme1n1
udevadm settle
```

Now inspect the result and record the new partition numbers:

```console
lsblk -o NAME,PATH,SIZE,TYPE,FSTYPE,PARTLABEL,MOUNTPOINTS
sgdisk --print /dev/nvme0n1
sgdisk --print /dev/nvme1n1
```

The rest of this guide uses `/dev/nvme0n1pN` and `/dev/nvme1n1pN` as
placeholders. Replace each `N` with the new partition number shown on that
specific disk. Never copy an existing RAID-member partition number.

Optionally set the GPT type and labels after substituting the verified numbers:

```console
sgdisk --typecode=N:8e00 --change-name=N:cloudfall-data0 /dev/nvme0n1
sgdisk --typecode=N:8e00 --change-name=N:cloudfall-data1 /dev/nvme1n1
partprobe /dev/nvme0n1
partprobe /dev/nvme1n1
udevadm settle
```

## 6. Create independent LVM volume groups

Confirm once more that the two selected partitions are empty, unmounted, and
are not MD members:

```console
lsblk -f /dev/nvme0n1 /dev/nvme1n1
blkid /dev/nvme0n1pN /dev/nvme1n1pN
cat /proc/mdstat
```

`blkid` should return no existing filesystem signature for the new partitions.
If it reports a signature, stop and investigate rather than erasing it.

Create one physical volume and one volume group per drive:

```console
pvcreate /dev/nvme0n1pN
pvcreate /dev/nvme1n1pN
vgcreate vg_data0 /dev/nvme0n1pN
vgcreate vg_data1 /dev/nvme1n1pN
pvs -o pv_name,pv_uuid,vg_name,pv_size,pv_free
vgs -o vg_name,pv_count,lv_count,vg_size,vg_free
```

Each VG must show exactly one PV. Do not add the second drive's partition to
the first VG.

## 7. Create service logical volumes and filesystems

Use the completed allocation worksheet, not the illustrative sizes below. Keep
10–15% of each VG free. This example separates workloads across the drives:

```console
lvcreate --size 120G --name lv_mysql vg_data0
lvcreate --size 180G --name lv_clickhouse vg_data0
lvcreate --size 240G --name lv_elasticsearch vg_data1
```

Verify allocation before formatting:

```console
lvs -o lv_name,vg_name,lv_size,devices
vgs -o vg_name,vg_size,vg_free
```

Format only the new data LVs. `-m 0` removes ext4's root-reserved percentage,
which is appropriate for dedicated application-data volumes but not for the
root filesystem:

```console
mkfs.ext4 -L mysql-data -m 0 /dev/vg_data0/lv_mysql
mkfs.ext4 -L clickhouse-data -m 0 /dev/vg_data0/lv_clickhouse
mkfs.ext4 -L elasticsearch-data -m 0 /dev/vg_data1/lv_elasticsearch
```

## 8. Mount by filesystem UUID without blocking boot

Create the intended mount points:

```console
install -d -o mysql -g mysql -m 0750 /srv/storage/mysql
install -d -o clickhouse -g clickhouse -m 0750 /srv/storage/clickhouse
install -d -o elasticsearch -g elasticsearch -m 0750 /srv/storage/elasticsearch
```

Install the service packages first if their users do not yet exist. Adjust
owners and modes to the actual service contract.

Record the filesystem UUIDs:

```console
blkid /dev/vg_data0/lv_mysql
blkid /dev/vg_data0/lv_clickhouse
blkid /dev/vg_data1/lv_elasticsearch
```

Add one line per data filesystem to `/etc/fstab`, substituting the UUIDs:

```fstab
UUID=<mysql-uuid>         /srv/storage/mysql         ext4  defaults,nofail,x-systemd.device-timeout=10s  0  2
UUID=<clickhouse-uuid>    /srv/storage/clickhouse    ext4  defaults,nofail,x-systemd.device-timeout=10s  0  2
UUID=<elasticsearch-uuid> /srv/storage/elasticsearch ext4  defaults,nofail,x-systemd.device-timeout=10s  0  2
```

Use filesystem UUIDs, not `/dev/nvme*`, `/dev/dm-*`, or assumed LV minor
numbers. Keep system entries conventional: root uses pass number `1`, other
mirrored filesystems use `2`, and swap uses `0`.

Validate before mounting:

```console
findmnt --verify --verbose
systemctl daemon-reload
mount /srv/storage/mysql
mount /srv/storage/clickhouse
mount /srv/storage/elasticsearch
findmnt -o TARGET,SOURCE,FSTYPE,OPTIONS /srv/storage/mysql /srv/storage/clickhouse /srv/storage/elasticsearch
df -hT / /boot /srv/storage/mysql /srv/storage/clickhouse /srv/storage/elasticsearch
```

The `nofail` option allows boot to complete if a data drive is absent. It does
not by itself protect a service from writing into the empty directory beneath
the missing mount; step 9 is mandatory.

## 9. Make every data service require its mount

Configure each application through its supported configuration so its actual
data directory is the corresponding `/srv/storage/...` path. Do this before
the service creates production data. Then add a systemd drop-in.

MariaDB example:

```console
systemctl edit mariadb.service
```

```ini
[Unit]
RequiresMountsFor=/srv/storage/mysql
```

ClickHouse example:

```console
systemctl edit clickhouse-server.service
```

```ini
[Unit]
RequiresMountsFor=/srv/storage/clickhouse
```

Elasticsearch example:

```console
systemctl edit elasticsearch.service
```

```ini
[Unit]
RequiresMountsFor=/srv/storage/elasticsearch
```

Reload and inspect the effective relationships:

```console
systemctl daemon-reload
systemctl show mariadb.service -p RequiresMountsFor
systemctl show clickhouse-server.service -p RequiresMountsFor
systemctl show elasticsearch.service -p RequiresMountsFor
systemctl list-dependencies mariadb.service
```

Repeat this for every service backed by an unmirrored mount. Verify the real
unit name with `systemctl list-unit-files`; package names and units can differ
by Debian release or vendor repository.

The expected behavior is now:

- the server boots when a data drive is missing because the mount uses
  `nofail`; and
- the affected service fails to start because its required mount cannot be
  established.

## 10. Configure RAID and drive monitoring

Verify that `/etc/mdadm/mdadm.conf` contains current `ARRAY` definitions:

```console
mdadm --detail --scan
grep -E '^(ARRAY|MAILADDR|MAILFROM)' /etc/mdadm/mdadm.conf
```

Add or update `MAILADDR` only when mail sent by root is delivered to an
actionable destination. On Debian, ensure automatic checks are enabled:

```console
grep '^AUTOCHECK=' /etc/default/mdadm
```

The required result is:

```text
AUTOCHECK=true
```

After changing MD or boot configuration, rebuild early-boot and GRUB metadata:

```console
update-initramfs -u -k all
update-grub
```

Check the distribution's RAID check schedule rather than assuming one exists:

```console
systemctl list-timers --all | grep -E 'mdcheck|mdadm'
grep -R 'checkarray\|sync_action' /etc/cron.d/mdadm /etc/cron.* 2>/dev/null
```

Enable periodic SSD trimming and verify SMART for both physical drives:

```console
systemctl enable --now fstrim.timer
systemctl status fstrim.timer --no-pager
smartctl -x /dev/nvme0n1
smartctl -x /dev/nvme1n1
```

## 11. Register and inspect the server with Cloudfall

This is where the record starts. Everything above happened outside
Cloudfall; from here the host is declared, observed and audited like any
other, and every later change to it goes through a gated operation with a
receipt.

Add the new `Server` only after the operating system and SSH access are stable.
Create a dedicated hybrid-storage `Baseline`; do not reuse
`debian-application` if its full-disk RAID and root thresholds do not match this
layout.

The following is a starting profile fragment for a 96 GiB root. Adjust mounts,
packages, services, ownership, and capacity thresholds to the actual server:

```yaml
---
apiVersion: cloudfall/v1
kind: Baseline
metadata:
  id: debian-hybrid-storage
  description: Two-drive RAID1 system with independent replicated-data tails
spec:
  os:
    distribution: Debian
    versions:
      - "13"
    serviceManager: systemd
  storage:
    softwareRaid:
      level: raid1
      minimumActiveDevices: 2
      minimumUsableBytes: 90000000000
    mounts:
      - path: /
        filesystem: ext4
        minimumBytes: 90000000000
      - path: /srv/storage/mysql
        filesystem: ext4
        minimumBytes: 110000000000
  packages:
    required:
      - name: mdadm
      - name: lvm2
      - name: smartmontools
    forbidden: []
  services:
    required:
      - name: ssh.service
        state: running
        status: enabled
  configuration:
    files:
      - path: /etc/fstab
        capture: hash
        owner: root
        group: root
        mode: "0644"
      - path: /etc/mdadm/mdadm.conf
        capture: hash
        owner: root
        group: root
        mode: "0644"
```

In Cloudfall v1, `minimumUsableBytes` refers in practice to the largest observed MD
device rather than total capacity across all arrays. A 90 GB threshold is
therefore appropriate for the example 96 GiB root array; do not enter the sum
of swap, boot, and root, or the total capacity of both drives.

Validate, render, inspect, and audit:

```console
task validate CONFIG_DIR=../my-project
task inventory CONFIG_DIR=../my-project
task inspect CONFIG_DIR=../my-project
task audit CONFIG_DIR=../my-project
```

`task inspect` also rebuilds the local dashboard. Review the generated
`tmp/dashboard/index.html`, storage observations, MD state, SMART evidence,
mount capacities, and service results.

Cloudfall v1 does not yet prove that every MD array has two healthy members, parse
`nofail`, verify physical-to-VG placement, or inspect `RequiresMountsFor=`.
Complete the manual checklist below even when `task audit` reports compliance.
The audit says the host matches what was declared; it does not yet say the
storage contract holds, and Cloudfall does not report what it cannot prove.

Under the [brownfield design](brownfield-design.md), where the team's own
Ansible inventory is the config, this step becomes adding the host to their
inventory with a `cloudfall` block naming its baseline; the observation
and audit are the same. Not built yet.

## 12. Run the new-server failure acceptance test

Run this test before production data is admitted, while rebuilding the server
is still acceptable. Confirm replicas and backups first. Do not use any
current production server for this drill.

This is the verify step for the whole guide. Cloudfall's rule that a run is
not an outcome until it is verified applies to hand work too: the layout is
not done because every command exited zero, it is done because the server
booted with each drive missing and came back to `[UU]`. Save the output of
the checks below from each boot next to the installation record; it is the
evidence that this host's failure contract was tested, and nothing in
Cloudfall can produce it later.

Use Hetzner's console/rescue capabilities or a provider-coordinated method to
make one complete physical drive unavailable. A software-only MD member failure
does not test firmware, bootloader, EFI, controller, and whole-disk behavior.

Test one drive at a time:

1. Record healthy baseline evidence and stop the server cleanly.
2. Make drive 0 unavailable and boot normally.
3. Confirm SSH access, mirrored root, degraded-but-active MD arrays, absent data
   mounts from drive 0, failed/inactive dependent services, healthy unrelated
   mounts and services, and healthy remote cluster replicas.
4. Restore drive 0, allow all MD arrays to resynchronize, and require `[UU]`.
5. Repeat with drive 1 unavailable.
6. Restore drive 1 and require a completely healthy final state.

Use these read-only checks after each boot:

```console
cat /proc/mdstat
mdadm --detail --scan
lsblk -o NAME,PATH,SIZE,TYPE,FSTYPE,MOUNTPOINTS
findmnt -o TARGET,SOURCE,FSTYPE,OPTIONS
systemctl --failed
systemctl status mariadb.service clickhouse-server.service elasticsearch.service --no-pager
smartctl -x /dev/nvme0n1
smartctl -x /dev/nvme1n1
```

An unavailable drive will make its `smartctl` command fail; that is expected.
Do not continue to production if either drive cannot boot the server, a missing
data volume blocks boot, an affected service starts on an unmounted directory,
or loss of the host/replica breaks cluster quorum.

## 13. Production activation checklist

- [ ] The installation record contains both model/serial pairs and the final
      partition/VG/LV/mount mapping.
- [ ] `/proc/mdstat` shows every system array healthy with both members active.
- [ ] Root, boot, system logs, temporary files, home directories, and
      `/srv/apps` are on mirrored storage.
- [ ] Each data tail is a separate one-PV VG.
- [ ] No RAID0, LVM stripe, or cross-drive VG exists.
- [ ] Every data filesystem uses a UUID in `/etc/fstab` with `nofail` and a
      finite device timeout.
- [ ] `findmnt --verify --verbose` reports no errors or type mismatches.
- [ ] Every storage service has `RequiresMountsFor=` for its real data mount.
- [ ] Every dataset has replicas on other servers in different failure
      domains, with single-host loss preserving quorum.
- [ ] Backups and a restore procedure exist independently of replication.
- [ ] MD checks, SMART monitoring, filesystem usage alerts, and `fstrim.timer`
      are active.
- [ ] Both one-drive boot tests passed and the arrays returned to `[UU]`.
- [ ] Cloudfall config validates, inspection is current, audit results were reviewed,
      and the operations dashboard has no unexplained critical task.
- [ ] The installation record and the failure-test output are kept with the
      project as the first evidence about this host.

## Troubleshooting

### Boot waits for or fails on an absent data drive

Confirm the data line uses the filesystem UUID and includes both `nofail` and a
finite `x-systemd.device-timeout=`. Run `findmnt --verify --verbose` and
`systemctl status <escaped-path>.mount` to see the generated mount-unit error.

### A storage service starts with its data filesystem missing

Stop the service immediately. Verify that the application data path and
`RequiresMountsFor=` point to the same absolute mount path, then inspect the
effective unit with `systemctl cat <service>` and `systemctl show <service>
-p RequiresMountsFor`.

### `findmnt --verify` reports ext3/ext4 mismatch

Use `blkid` or `lsblk -f` to identify the real on-disk filesystem. Correct the
`fstab` type to match it. Do not relabel an ext3 filesystem as ext4 without a
separate, tested conversion plan.

### An MD array is degraded immediately after installation

Inspect the specific array with `mdadm --detail /dev/mdX` and both drives with
SMART. A running resync may only need to finish; a missing, faulty, or rejected
member must be corrected before any data-volume work or production activation.

### GRUB appears to target only one disk

Stop before production. On BIOS systems, correct the `grub-pc` install-device
selection and reinstall GRUB to both verified whole disks. On UEFI systems,
verify the EFI system partitions, files, firmware entries, and provider boot
behavior. In either mode, repeat the physical one-drive boot tests.

## Primary references

- [Hetzner Installimage](https://docs.hetzner.com/robot/dedicated-server/operating-systems/installimage/)
- [Hetzner software RAID](https://docs.hetzner.com/robot/dedicated-server/raid/software-raid/)
- [Linux MD administration](https://kernel.org/doc/html/next/admin-guide/md.html)
- [Debian `mdadm(8)`](https://manpages.debian.org/trixie/mdadm/mdadm.8.en.html)
- [Debian `fstab(5)`](https://manpages.debian.org/bookworm/mount/fstab.5.en.html)
- [Debian `grub-install(8)`](https://manpages.debian.org/trixie/grub2-common/grub-install.8.en.html)
- [Debian `pvcreate(8)`](https://manpages.debian.org/testing/lvm2/pvcreate.8.en.html)
- [Debian `vgcreate(8)`](https://manpages.debian.org/trixie/lvm2/vgcreate.8.en.html)
- [systemd mount units and `nofail`](https://github.com/systemd/systemd/blob/main/man/systemd.mount.xml)
- [systemd `RequiresMountsFor=`](https://github.com/systemd/systemd/blob/main/man/systemd.unit.xml)

