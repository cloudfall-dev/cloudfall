### Fixed

- An observed server snapshot written before 0.8.0, which names its baseline `spec.serverType`, validates against `observed-server.schema.json` again: the schema takes `spec.serverType` or `spec.baseline`, exactly one of the two, and the reader keeps reading `serverType` as the baseline with no removal date, since a record's fields are never deprecated within `cloudfall/v1` (#376)
