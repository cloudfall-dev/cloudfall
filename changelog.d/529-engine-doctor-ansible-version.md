### Fixed

- `cloudfall-engine doctor` reads the `ansible-playbook` version only from ansible's own `ansible-playbook [core X.Y.Z]` line, as `cloudfall doctor` does: an ansible that fails at start no longer passes the check with the Python version its traceback names, such as `3.15`; the check fails with `no version in the output of ansible-playbook --version`, and `data.dependencies` now lists `ansible-playbook` (#529)
