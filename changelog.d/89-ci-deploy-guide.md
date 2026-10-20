### Added

- A CI deploy guide (`docs/ci-deploy-guide.md`): one GitHub Actions workflow that renders secrets, builds the artifact with `cloudfall-engine artifact build`, deploys with `cloudfall deploy --yes` and uploads the receipts, with one SSH key and one age key per environment and required reviewers standing in for `approval: manual`, which `deploy` does not enforce; not yet run against a real server (#89)
