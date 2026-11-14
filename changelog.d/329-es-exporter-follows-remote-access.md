### Fixed

- Adding or removing `remoteAccess` on an Elasticsearch service with `metrics.enabled` no longer leaves the collector scraping the node the old way until the next `logging deploy`: the services converge re-renders the Alloy configuration and its systemd drop-in on each logging collector where `logging deploy` already set Alloy up, validates it, and restarts Alloy only when a file changed, so the exporter switches between plain HTTP and HTTPS with the project CA in the same run; a host that is not a collector, or whose Alloy is not set up yet, is left untouched (#329)
