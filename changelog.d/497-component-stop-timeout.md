### Added

- Components: an optional `service.stop.timeoutSeconds` (1 to 1800, no default) renders `TimeoutStopSec=` in the component's unit, so a worker that finishes its running task on SIGTERM gets that long before systemd kills it, in place of systemd's 90 seconds; a component without it renders the same unit as before. The runner's limit on a deploy, `restart` or `rollback` playbook grows by that timeout for every restart it may make, so a slow stop is waited for rather than reported as a failed run, and the CI deploy guide says how to deploy a worker (#497)
