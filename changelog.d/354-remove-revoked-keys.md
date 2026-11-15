### Fixed

- A baseline run now removes an SSH key declared `lifecycle: revoked` from root's `authorized_keys`, after the active keys are in, matching the key's type and body whatever its comment or options; keys the project doesn't declare stay (D-22). The rendered Ansible inventory's `cloudfall_ssh_public_keys` now carries a server's revoked keys too, each with its `lifecycle`, so the role can remove them (#354)

### Breaking

- `cloudfall config validate` refuses one SSH key (same type and body, any comment) declared `active` and `revoked` in the same environment, with `CONFIG_INVALID` and code `ssh_public_key_lifecycle_conflict`, so a project that declares a key that way stops validating; before, the baseline installed it. Before upgrading, delete the revoked declaration if the key should keep access, or mark the active one `revoked` if it should not (#354)
