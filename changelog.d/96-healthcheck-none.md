### Fixed

- A deploy of a component with `healthCheck: {type: none}` no longer fails its health gate on the missing HTTP fields and stops the release: the gate now requires the systemd unit to be active right after the restart and still active 10 seconds later, so a worker that crashes on start still fails and rolls back (#96)
