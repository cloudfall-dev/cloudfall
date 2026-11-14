### Added

- `cloudfall logging open` opens the LoggingStack's Grafana: it forwards a local port over ssh to Grafana on the backend's loopback, built from the backend server's declared address, SSH user and port, answers with the URL of the Cloudfall dashboards once the forward is up, and holds it until stopped. `--port` picks the local port (one in use exits 4 before ssh starts), `--stack` picks the stack, and `--print-command` answers with the ssh command and runs nothing. The answer names where the admin password is kept, never the password (S-016)
