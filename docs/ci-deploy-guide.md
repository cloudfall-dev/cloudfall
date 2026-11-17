# CI deploy guide

Every other guide runs Cloudfall from an operator's machine: your age key in
`~/.config/cloudfall/age.key`, your SSH agent, your shell. This guide moves
the same deploy into a GitHub Actions workflow, so a merge to `main` updates
staging and a production deploy waits for a person's approval.

The record does not change. The runner renders the secrets, builds the
artifact and deploys it with the same commands you run by hand, and the
receipts they write are uploaded with the run so they outlive the runner.

> **Status:** not proven live. The workflow below is written from the code
> and checked against the flags of Cloudfall 0.7.0 (`--help` and the
> source), but it has not had a run against a real server yet. Treat the
> first run on staging as the proving run, and open an issue for anything
> that differs.

## What the runner needs

The runner is a controller like your laptop. It needs:

- The project checked out: the repository `cloudfall init` created, with its
  `pyproject.toml` and `uv.lock` committed
- `uv`, which installs Python 3.14, Cloudfall and `ansible-core` from the
  project's lock file
- `ansible-playbook` 2.21.0 or later: it comes with Cloudfall
  (`ansible-core>=2.21,<2.22`), so `uv run` finds it in the project's
  virtualenv
- `git` 2.24.0 or later, which `artifact build` uses to clone the
  component's repository; GitHub's Ubuntu runners ship a newer one
- `sops` 3.8.0 or later, which `secrets render` runs to decrypt fragments;
  `age` itself is not needed, because sops decrypts age files on its own
- An SSH key the servers accept, and their host keys in `known_hosts`
- The age key that decrypts this environment's secrets

These are the versions the commands check before they run (their
`required_tools`); a missing or older tool stops the command with an error
naming it. `uv run cloudfall doctor` checks all of them at once, so the
workflow runs it first.

## One key per environment

A runner should hold the keys of one environment and nothing else. Cloudfall
already scopes two of them.

**SSH keys.** Each `SshPublicKey` names an environment, and the baseline
installs a server's active keys only when their environment matches the
server's. Make a key for the staging runner and declare it for staging:

```sh
ssh-keygen -t ed25519 -N "" -C "ci staging" -f ci-staging
uv run cloudfall add ssh-key ci-staging.pub --owner ci --id ci-staging --environment staging
uv run cloudfall-engine inventory render --output tmp/ansible-inventory.json
uv run cloudfall-engine playbook run baseline --inventory tmp/ansible-inventory.json
```

The baseline writes the key into root's `authorized_keys` on staging servers
only, so the staging runner cannot log in to production. A server whose
`ssh.user` is not `root` needs the key installed for that user by hand.

**Age keys.** This one needs a change to your project. `cloudfall init`
writes a `.sops.yaml` with one rule that encrypts every fragment under
`secrets/` to one key. A runner holding that key can decrypt everything.
Split the rule by environment directory, and list your own key in each rule
so you can still edit every file:

```sh
age-keygen -o staging.key
age-keygen -o production.key
```

```yaml
creation_rules:
  - path_regex: secrets/staging/.*\.env$
    age: age1staging...,age1yourown...
  - path_regex: secrets/production/.*\.env$
    age: age1production...,age1yourown...
```

sops uses the first rule that matches a path. Files encrypted before the
change keep their old recipients until you re-encrypt them:

```sh
sops updatekeys secrets/staging/crm.env
```

Why this stops a staging runner: a secret reference
`{environment: production, path: /crm}` resolves from
`secrets/production/crm.env`, and `secrets render` decrypts only the files
the component's references name. If a component on the staging runner
references a production fragment, sops has no matching key, and the render
fails with `secrets_decrypt_failed` before anything is written. Cloudfall
does not itself check that a component's references name the environment of
its servers; the key the runner holds is what enforces it.

Store `staging.key` and `production.key` in a password manager, then delete
the local copies once they are in GitHub.

## GitHub environments and secrets

Create one GitHub environment per Cloudfall environment, with the same name
(`staging`, `production`), and put three secrets in each:

| Secret | Content |
| --- | --- |
| `CLOUDFALL_SSH_KEY` | The private key of that environment's runner key, such as `ci-staging` |
| `CLOUDFALL_KNOWN_HOSTS` | The `known_hosts` lines of that environment's servers |
| `SOPS_AGE_KEY` | The age key file of that environment, such as `staging.key` |

```sh
gh secret set CLOUDFALL_SSH_KEY --env staging < ci-staging
gh secret set SOPS_AGE_KEY --env staging < staging.key
ssh-keygen -F 203.0.113.20 > staging-known-hosts
gh secret set CLOUDFALL_KNOWN_HOSTS --env staging < staging-known-hosts
```

`ssh-keygen -F` reads the host key your own machine already trusts, which is
safer than scanning it from the runner. For a server on another port, look it
up as `[203.0.113.20]:2222`.

In the repository's settings, under Environments:

- `production`: add required reviewers and turn on "Prevent self-review"
- Both: limit deployment branches to `main`, so a job on another branch never
  sees the secrets

## The workflow

Save this as `.github/workflows/deploy.yml` in the project repository.
Replace `crm-backend-staging` with your staging component.

```yaml
name: deploy

on:
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      environment:
        description: Cloudfall environment, and the GitHub environment of the same name
        type: choice
        options: [staging, production]
        default: staging
      component:
        description: Component to deploy
        required: true
        default: crm-backend-staging
      ref:
        description: Git ref of the component's repository to build
        required: true
        default: main
  repository_dispatch:
    types: [deploy]

concurrency:
  group: deploy-${{ inputs.environment || github.event.client_payload.environment || 'staging' }}
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-24.04
    environment: ${{ inputs.environment || github.event.client_payload.environment || 'staging' }}
    env:
      COMPONENT: ${{ inputs.component || github.event.client_payload.component || 'crm-backend-staging' }}
      REF: ${{ inputs.ref || github.event.client_payload.ref || 'main' }}
      SOPS_VERSION: v3.13.3
    steps:
      - uses: actions/checkout@v7

      - uses: astral-sh/setup-uv@v10.1.0

      - name: Install Cloudfall from the lock file
        run: uv sync --locked

      - name: Install sops
        run: |
          base="https://github.com/getsops/sops/releases/download/$SOPS_VERSION"
          curl -fsSLO "$base/sops-$SOPS_VERSION.linux.amd64"
          curl -fsSLO "$base/sops-$SOPS_VERSION.checksums.txt"
          sha256sum --check --ignore-missing "sops-$SOPS_VERSION.checksums.txt"
          sudo install -m 0755 "sops-$SOPS_VERSION.linux.amd64" /usr/local/bin/sops

      - name: Load the SSH key
        env:
          CLOUDFALL_SSH_KEY: ${{ secrets.CLOUDFALL_SSH_KEY }}
          CLOUDFALL_KNOWN_HOSTS: ${{ secrets.CLOUDFALL_KNOWN_HOSTS }}
        run: |
          install -m 0700 -d ~/.ssh
          printf '%s\n' "$CLOUDFALL_KNOWN_HOSTS" > ~/.ssh/known_hosts
          eval "$(ssh-agent -s)"
          printf '%s\n' "$CLOUDFALL_SSH_KEY" | ssh-add -
          echo "SSH_AUTH_SOCK=$SSH_AUTH_SOCK" >> "$GITHUB_ENV"

      - name: Write the age key
        env:
          SOPS_AGE_KEY: ${{ secrets.SOPS_AGE_KEY }}
        run: |
          install -m 0600 /dev/null "$RUNNER_TEMP/age.key"
          printf '%s\n' "$SOPS_AGE_KEY" > "$RUNNER_TEMP/age.key"
          echo "SOPS_AGE_KEY_FILE=$RUNNER_TEMP/age.key" >> "$GITHUB_ENV"

      - name: Check the toolchain
        run: uv run cloudfall doctor

      - name: Validate the config
        run: uv run cloudfall config validate

      - name: Render secrets
        run: uv run cloudfall secrets render "$COMPONENT"

      - name: Build the artifact
        id: build
        run: |
          uv run cloudfall-engine artifact build "$COMPONENT" --ref "$REF" --format json > "$RUNNER_TEMP/build.json"
          echo "release=$(jq -r '.data.release' "$RUNNER_TEMP/build.json")" >> "$GITHUB_OUTPUT"

      - name: Show the plan
        env:
          RELEASE: ${{ steps.build.outputs.release }}
        run: uv run cloudfall deploy "$COMPONENT" --release "$RELEASE"

      - name: Deploy
        env:
          RELEASE: ${{ steps.build.outputs.release }}
        run: uv run cloudfall deploy "$COMPONENT" --release "$RELEASE" --env-file "tmp/env/$COMPONENT.env" --yes

      - name: Remove the decrypted files
        if: always()
        run: rm -f "tmp/env/$COMPONENT.env" "$RUNNER_TEMP/age.key"

      - name: Upload the receipts
        if: always()
        uses: actions/upload-artifact@v7
        with:
          name: cloudfall-${{ env.COMPONENT }}-${{ github.run_id }}
          path: |
            tmp/artifacts/**/*.json
            record/releases/
            record/env-receipts/
          retention-days: 90
```

What each step does:

- **Install Cloudfall from the lock file.** `uv sync --locked` installs the
  Cloudfall release the project pins, and fails if `uv.lock` is out of date
  rather than resolving a different one
- **Install sops.** The binary is checked against the release's checksum
  file before it is installed
- **Load the SSH key.** The key goes into an `ssh-agent`, never onto disk;
  Ansible reaches the agent through `SSH_AUTH_SOCK`
- **Write the age key.** The file is created `0600` before the key is
  written into it, and `SOPS_AGE_KEY_FILE` points sops at it, as on your own
  machine
- **Render secrets.** Writes `tmp/env/<component>.env` (`0600`) and the
  receipt `record/env-receipts/<component>.json`; the log shows key names and a
  hash, never a value
- **Build the artifact.** Clones the component's repository at `REF` and
  packages it under `tmp/artifacts/<component>/`
- **Show the plan.** `deploy` without `--yes` validates the release and
  prints which servers it would change, and changes nothing
- **Deploy.** Ships the release behind the component's health check, and
  writes the release receipt `record/releases/<component>.json`. A smoke check
  declared as a `type: command` health check runs on each server after its
  restart, so it gates the deploy and needs no CI step of its own: when it
  fails, the server goes back to the previous release, and so does every
  server that switched to the release earlier in the deploy; the deploy
  fails, and the receipt records the failure with `healthCheckPassed: false`
  in that server's entry and `rolledBack: true` in each entry rolled back.
  The release already serves traffic while its check runs, since the check
  comes after the switch and the restart. A rollback, this automatic one or
  a later `cloudfall rollback`, does not undo the component's release
  command (database migrations, say), which ran before any server switched,
  so a migration must stay compatible with the release before it.
  `cloudfall restart`, `cloudfall rollback` and `cloudfall health` run the
  same check, so it must be safe to run against the live service at any time
- **Remove the decrypted files.** GitHub-hosted runners are thrown away
  after the job; on a self-hosted runner this step is what keeps the values
  off its disk

If the project is not at the root of the repository, set `CLOUDFALL_PROJECT`
to its directory in the job's `env`.

## Deploying a worker

A queue worker (Celery, RQ) is a component like an HTTP service, with two
differences in its declaration:

- **A command health check.** A worker has no HTTP port to probe, so give it
  `healthCheck.type: command` with a command that proves it is up, such as
  `[.venv/bin/celery, -A, crm, inspect, ping]`
- **A stop timeout longer than its longest task.** Every deploy, `restart`
  and `rollback` stops the service: systemd sends SIGTERM, which a worker
  answers by finishing its running tasks, then SIGKILL once the stop timeout
  runs out, after 90 seconds unless the component says otherwise. Set
  `service.stop.timeoutSeconds` (1 to 1800) above the longest task the worker
  runs, so a deploy never cuts one off; the deploy waits for the stop. The
  unit takes a new value on the next deploy: `restart` and `rollback` keep
  the unit as it was deployed

```yaml
  service:
    manager: systemd
    name: crm-worker
    command: [.venv/bin/celery, -A, crm, worker]
    stop:
      timeoutSeconds: 900
  healthCheck:
    type: command
    command: [.venv/bin/celery, -A, crm, inspect, ping]
    timeoutSeconds: 30
    attempts: 5
```

A task longer than 1800 seconds still dies on a deploy; such a task needs a
queue that redelivers it, as Celery's `acks_late` does, and must be safe to
run twice.

## Passing the release id

`cloudfall deploy` takes the release to ship as `--release`, and only
`artifact build` produces one. Two things trip people up:

- `artifact build` is a `cloudfall-engine` command, not a `cloudfall` one.
  Through an agent it is the `build_artifact` tool of `cloudfall mcp serve`
- The release id is in the build's JSON answer at `data.release`, such as
  `20260101T000000Z-abcdef0` (the build time and the short commit). The
  workflow reads it with `jq` and passes it on as a step output

Off a terminal both CLIs answer in JSON by default; the workflow passes
`--format json` so the build step does not depend on that.

The artifact is a file on the runner. `deploy` verifies it from
`tmp/artifacts` before shipping it, so build and deploy must run in the same
job. Splitting them across jobs means uploading `tmp/artifacts/` from one and
downloading it into the other.

`REF` is a ref of the component's repository (its `repository.url`), not of
the project repository. A push to the project's `main` therefore rebuilds the
component's `main`. To ship to production exactly the commit staging ran,
dispatch the production deploy with that commit's full hash as `ref`; the
staging receipt records it as `gitCommit`. The build packages the same
commit again, which gives a new release id and archive.

## Triggering it from an application repository

An application repository can start a deploy when its own `main` changes,
with a `repository_dispatch` event on the project repository:

```sh
gh api repos/acme/fleet/dispatches \
  -f event_type=deploy \
  -f 'client_payload[component]=crm-backend-staging' \
  -f 'client_payload[environment]=staging' \
  -f "client_payload[ref]=$GITHUB_SHA"
```

Run it as a step in the application's workflow, authenticated with a token
that may write the project repository's contents (a fine-grained token or a
GitHub App token, stored as a secret of the application repository). The
event runs the workflow from the project's default branch, and a dispatch
naming `production` still waits for its reviewers.

## Approval: who presses the button

Every `Application` declares `spec.approval: manual` or `automatic`, and
`cloudfall deploy` enforces it:

- `automatic`: `deploy --yes` deploys, as the workflow above does. Keep
  staging's applications `automatic` and its one job stays as it is
- `manual`: `deploy --yes` deploys a release only when a person approved
  that release with `cloudfall release approve`. Without an approval that
  counts it exits 4 `PRECONDITION` with `deployment_approval_missing`, before
  any playbook runs, and names the command to run. `deploy` without `--yes`
  shows the approval in its plan: `approval.status` is `approved` or
  `missing`

An approval is a record under `decisions/releases/` in the project. It names
the archive's SHA-256, the component's servers and when it expires (24 hours
by default, `--valid-for` up to a week). It counts only while the archive
being deployed has that digest, the component is declared on exactly those
servers, and it has not expired. A failed health gate does not use it up, so
the same release can be retried until it expires.

A person approves at a terminal by typing the release id back; no flag, and
no CI job, answers that prompt. So a production deploy of a `manual`
application is two jobs with a person between them, and approval needs the
exact archive the deploy ships:

1. A build job builds the release and uploads `tmp/artifacts/<component>/`
   as a workflow artifact
2. The person downloads that artifact into `tmp/artifacts/<component>/` of
   their checkout and approves it:

   ```sh
   gh run download <run-id> --name cloudfall-artifact-crm-backend --dir tmp/artifacts/crm-backend
   uv run cloudfall release approve crm-backend --release 20260101T000000Z-abcdef0
   git add decisions/releases
   git commit -m "Approve crm-backend 20260101T000000Z-abcdef0"
   git push
   ```

3. They dispatch the deploy job with the build's run id and the release. It
   checks out the commit holding the record, downloads the same artifact and
   deploys it:

```yaml
  build:
    runs-on: ubuntu-24.04
    steps:
      # checkout, uv, uv sync --locked, as in the workflow above
      - name: Build the artifact
        id: build
        run: |
          uv run cloudfall-engine artifact build "$COMPONENT" --ref "$REF" --format json > "$RUNNER_TEMP/build.json"
          echo "release=$(jq -r '.data.release' "$RUNNER_TEMP/build.json")" >> "$GITHUB_OUTPUT"
      - uses: actions/upload-artifact@v7
        with:
          name: cloudfall-artifact-${{ env.COMPONENT }}
          path: tmp/artifacts/${{ env.COMPONENT }}/
```

```yaml
  deploy:
    runs-on: ubuntu-24.04
    environment: production
    steps:
      # checkout, uv, sops, the SSH and age keys, secrets render, as above
      - uses: actions/download-artifact@v7
        with:
          name: cloudfall-artifact-${{ env.COMPONENT }}
          path: tmp/artifacts/${{ env.COMPONENT }}/
          run-id: ${{ inputs.build_run_id }}
          github-token: ${{ github.token }}
      - name: Deploy the approved release
        run: uv run cloudfall deploy "$COMPONENT" --release "$RELEASE" --env-file "tmp/env/$COMPONENT.env" --yes
```

The deploy job reads the record from its checkout, so dispatch it on the
commit that holds the approval. A rebuilt release has a new archive, and
its old approval no longer counts.

GitHub's required reviewers remain a useful outer gate: give the
`production` environment reviewers and turn on "Prevent self-review", and
the deploy job never sees that environment's SSH key or age key until a
reviewer lets it start. Their approval lands in GitHub's deployment
history; the `release approve` record is the one in Cloudfall's.

When the reviewers are the real gate and nobody will run `release approve`,
switch the application to `approval: automatic`, so the project no longer
claims a gate Cloudfall does not hold.

## Keeping the receipts

The receipts land in `record/`, which the project commits, but the runner
disappears with the job. The upload step keeps, for 90 days:

- `tmp/artifacts/<component>/<release>.json`: the artifact metadata, with the
  commit and the archive's SHA-256
- `record/releases/<component>.json`: the release receipt from `deploy`, with
  one entry per server under `servers`; each entry's `release` names the
  release that server ended on (`null` when a first release was stopped)
- `record/env-receipts/<component>.json`: the key names and hash of the
  rendered environment file, never the values

It never uploads `tmp/env/`, which holds the decrypted values. Add
`tmp/artifacts/` whole if you want the archives kept as well.

To keep the receipts in git, commit `record/` back from the job. Give the
job `contents: write` and add a step after the deploy:

```yaml
      - name: Commit the receipts
        env:
          RELEASE: ${{ steps.build.outputs.release }}
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add record
          git commit -m "Record $COMPONENT $RELEASE"
          git push
```

Each deploy rewrites `record/releases/<component>.json`, so the git history
of that file is the component's deploy history. A push made with the
workflow's own token does not start another workflow run, so this does not
loop. A branch protection rule on
`main` will refuse the push unless it allows the workflow to bypass it.

## What this guide does not cover

- Moving `artifact build` into the `cloudfall` CLI, so CI installs and pins
  one tool
- A published reusable action
- Converging the baseline, services or domains from CI; the same pattern
  applies, with the matching `cloudfall-engine playbook run` command
