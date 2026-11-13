### Fixed

- Deploying the release that is already `current` unpacks into a fresh `releases/<release>.redeploy-<UTC time>` directory instead of writing into the live one, so a failed health gate rolls back to the untouched copy; `rollback` to that release id takes its newest copy, and receipts still name the release id (#223)
- A failed health gate restores the service unit the previous release ran under, not only the `current` symlink, then gates the restored release on its health check; a restored release that fails it fails the deploy with its own message (#223)
- `deploy.yml --check` runs to its end: the dry run reports the uv release it would install and skips the digest check, the unpack and `uv sync` of an archive it never transferred (#223)
