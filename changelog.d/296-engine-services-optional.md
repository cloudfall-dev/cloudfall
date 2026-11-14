### Fixed

- `cloudfall-engine playbook run <playbook> --engine <dir>` runs a playbook of an engine directory that has no `services.yml`, where it used to crash with `HANDLER_CRASHED` and `playbook does not exist: .../services.yml` whatever playbook was asked for; a `services.yml` run still decrypts and stages the owner passwords as before (#296)
