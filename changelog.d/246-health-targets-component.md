### Fixed

- `health` contacts only the component's own servers and fails before any host when the component has no servers in the inventory, instead of reporting success with nothing probed; and `deploy`, `rollback` and `restart` stop at the first of the component's servers they could not reach, where before Ansible skipped the unreachable server and went on to the next one (#246)
