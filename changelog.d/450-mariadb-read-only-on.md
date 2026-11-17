### Fixed

- On MariaDB 11.8, where `read_only` reads `ON` rather than `1` in the role's replication state query, the role now parses both servers' state through one filter that takes `ON`, `NO_LOCK` and `NO_LOCK_NO_ADMIN` (or `1` to `3`) as read-only and `OFF` (or `0`) as writable, and fails on any other value. A second converge of a healthy replica no longer turns writes off again and reports a change (S-019-13), and a promotion from a declared source that is read-only and applied now goes through instead of waiting for it to turn read-only (S-019-15) (#450)
