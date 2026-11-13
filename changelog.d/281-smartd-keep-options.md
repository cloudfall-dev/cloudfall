### Fixed

- `cloudfall baseline converge` keeps a host's own smartd `DEVICESCAN` options (`-s` self-test schedules, `-W` temperature limits) and replaces only its `-M exec`, adding `-m root` when the line has no `-m`; a failed restart of mdadm's monitor or smartd now fails the run instead of reporting success, and only a unit the host does not have is skipped (#281)
