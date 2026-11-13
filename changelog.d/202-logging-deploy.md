### Added

- `cloudfall logging deploy`: deploy the declared `LoggingStack` after meeting its secret requirements; generated secrets are written to the store, an input secret that is missing stops the run (exit 4, `INPUT_REQUIRED`), and each stored secret is copied to its host path without overwriting a file already there; without `--yes` it shows the plan (S-011)
