### Fixed

- A deploy run with Ansible's `--diff` no longer prints the component's environment file, secrets included: the tasks that install a release's environment file or carry the previous one over set `diff: false` (#374)
