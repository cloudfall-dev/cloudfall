### Added

- `Baseline` `storage.pools`: existing LVM volume groups that workload volumes go into; each `LoggingStack` volume names its pool, LV, LV type and physical volumes in `requirements show`, and `config validate` refuses a volume with no pool (S-014)

### Changed

- A `LoggingStack` now needs a storage pool on its backend's baseline (S-014-2)
