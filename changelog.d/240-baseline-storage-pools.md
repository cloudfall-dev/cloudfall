### Added

- `Baseline` `storage.pools`: existing LVM volume groups that workload volumes go into; each `LoggingStack` volume names its pool, LV, LV type and physical volumes in `requirements show`, and `config validate` refuses a volume with no pool (S-014)

### Breaking

- A `LoggingStack` now needs a storage pool on its backend's baseline: `cloudfall config validate` refuses a `LoggingStack` whose backend server's `Baseline` declares no `storage.pools` with `CONFIG_INVALID` and code `volume_requirement_pool_missing`, so a project whose `LoggingStack` validated on 0.8.0rc22 stops validating. Before upgrading, declare the volume group the Loki and Prometheus volumes go into as a pool on that baseline (`storage.pools` with `id`, `volumeGroup` and `primary`); with more than one pool, name it on each volume in `spec.backend.storage.pool` and `spec.metrics.backend.storagePool`, or validation refuses with `volume_requirement_pool_ambiguous` (S-014-2)
