### Fixed

- A missing Elasticsearch exporter credential no longer stops Alloy on the whole host: the collector's systemd drop-in gives each `LoadCredential=` a `SetCredential=` default, so when the node's `cloudfall` password or project CA is gone (the service taken out of the project, its files cleaned up) Alloy still starts and only that exporter's scrapes fail; the next `logging deploy` drops the exporter and its credential lines (#326)
