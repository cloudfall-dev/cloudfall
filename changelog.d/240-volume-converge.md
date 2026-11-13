### Added

- `cloudfall logging deploy` provides the stack's data volumes: a read-only report from each host, refusals decided on the controller before any host change (`volume_refused`), then `lvcreate`/`mkfs` for missing volumes, `lvextend --resizefs` for small ones, an fstab line by UUID, the mount and `RequiresMountsFor` drop-ins (S-014)
