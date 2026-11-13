### Added

- `cloudfall baseline converge`: run `baseline.yml` with each host's alert channel; hosts whose baseline names an `AlertChannel` get `/usr/local/sbin/cloudfall-alert`, the channel's token and chat id, and mdadm and smartd pointed at the sender; a token not in the store stops it before any server (S-015)
