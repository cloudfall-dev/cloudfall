### Fixed

- `config validate` refuses a storage pool device path with `..`, a `volumeGroup` of `.` or `..`, and a pool naming one device as both `primary` and `secondary` (`storage_pool_same_physical_volume`); before, the first two passed validation and crashed `requirements show` (#248)
