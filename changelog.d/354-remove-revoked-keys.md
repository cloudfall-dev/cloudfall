### Fixed

- A baseline run now removes an SSH key declared `lifecycle: revoked` from root's `authorized_keys`, after the active keys are in, matching the key's type and body whatever its comment or options; keys the project doesn't declare stay (D-22). The same key declared `active` and `revoked` in one environment is refused by `cloudfall config validate` as `ssh_public_key_lifecycle_conflict` (#354)
