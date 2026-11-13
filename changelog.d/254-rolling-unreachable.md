### Fixed

- `baseline`, `bootstrap`, `services`, `data`, `domains`, `logging`, `time` and `backup` stop at the first server they could not reach, as `deploy`, `rollback` and `restart` do since #246, where before Ansible skipped the unreachable server and changed the next one; the check lives in one shared task file, `playbooks/tasks/rollout-start.yml`, and `inspect` still goes on past a server it cannot reach, reporting it as missing (#254)
