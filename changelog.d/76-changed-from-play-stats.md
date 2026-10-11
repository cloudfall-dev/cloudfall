### Fixed

- A decision records a host `changed` when any task of the play changed it,
  not only the last one. The check preview, the execution and the verify run
  read each host's counts from the play recap through a callback the SDK
  ships (`cloudfall_host_stats`), in place of Ansible's `tree` callback,
  which keeps only the last task's result: a run whose recap said
  `changed=2` was recorded `changed: []`, and a verify run that changed a
  host was recorded `verified`. The callback is added ahead of the callback
  directories the team's `ansible.cfg` names, not in place of them (#76)
