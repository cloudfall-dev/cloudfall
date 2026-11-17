### Fixed

- The `ansible-core` dependency asks for 2.22 on Python 3.15 and later, where 2.21 fails at start (`AttributeError: module 'dataclasses' has no attribute '_is_type'`), so a lock holding 2.21 no longer resolves there; Python 3.14 keeps `>=2.21,<2.23`. `docs/compatibility.md` names the supported Python versions per release line and says 0.7.0 needs Python 3.14 (`requires-python = ">=3.14,<3.15"` in the project) (#514)

### Added

- `cloudfall doctor` checks that `ansible-playbook --version` starts on the Python that runs Cloudfall (`ansible-start` in `data.checks`), and when it doesn't, names the error and the fix: relock, or cap `requires-python` below 3.15. It runs no playbook and touches no host (#514)
