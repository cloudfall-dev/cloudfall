### Added

- `cloudfall operator mark-interrupted <id> --confirm-dead` also closes a receipt left `executed`, whose verification never recorded an outcome, as `interrupted`, as it does a `running` one; it then stops blocking new proposals for the same alert or drift (#358)

### Fixed

- An operator verify step that errors (the alert feed unreachable, the audit failing) now settles the receipt `failed` with the error as its detail, so autonomy for that operation kind is suspended, and the command still fails with the error; before, the receipt stayed `executed` for good and blocked its trigger (#358)
