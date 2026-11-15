### Fixed

- One decision record or proposal receipt that cannot be read no longer stops every reader of the store: `operations propose`, `decisions list`, `why` and `operator list` go on with the records they can read and add a `RECORD_UNREADABLE` warning naming each skipped file and why; an operator pass reports the receipts it skipped under `unreadable`, and its autonomy pass grants nothing while a receipt cannot be read. `decisions show` and `decisions approve` still answer `RECORD_INVALID` for the one record they read (#218)
- Each record is read with the schema its `apiVersion` names, so a later schema version can sit beside `cloudfall/v1`; a record of a version this Cloudfall does not read is reported, not fatal. A golden set of records in the shapes released versions wrote is loaded by the tests (#218)

### Added

- A decision record and a proposal receipt name the Cloudfall version that last wrote them in `spec.writerVersion`, an optional schema field: records written before it stay valid (#218)
