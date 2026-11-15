### Fixed

- An operator proposal recorded with the audit check `server_type.id` keeps verifying once `baseline.id` is compliant, and keeps blocking a new proposal for the same drift, with no removal date instead of until 0.9.0, since a record's fields are never deprecated within `cloudfall/v1` (#387)
