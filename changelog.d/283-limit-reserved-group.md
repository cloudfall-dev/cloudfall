### Fixed

- A command that runs a playbook on one server refuses a Server whose id is one of Ansible's implicit group names (`all`, `ungrouped`) with `lifecycle_limit_reserved_group` before anything runs: `--limit all` reached every host and `--limit ungrouped` none (#283)
