### Fixed

- `cloudfall lock clear <component>` no longer deletes a server's lock that a run took again after it was shown: `lock.yml` compares the shown token and removes the lock in one command on the server, instead of reading it and deleting it in two tasks, so a lock taken since is kept and the command exits `CONFLICT`; and with no lock found while a server could not be reached it exits `CONFLICT` with `component_lock_not_cleared` and `unreadServers`, instead of answering `noop` as if no run held the lock (#271)
