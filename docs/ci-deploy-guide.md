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
            tmp/releases/
            tmp/env-receipts/
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
  receipt `tmp/env-receipts/<component>.json`; the log shows key names and a
  hash, never a value
- **Build the artifact.** Clones the component's repository at `REF` and
  packages it under `tmp/artifacts/<component>/`
- **Show the plan.** `deploy` without `--yes` validates the release and
  prints which servers it would change, and changes nothing
- **Deploy.** Ships the release behind the component's health check, and
  writes the release receipt `tmp/releases/<component>.json`. A smoke check
  declared as a `type: command` health check runs on each server after its
  restart, so it gates the deploy and needs no CI step of its own: when it
  fails, the server goes back to the previous release, the deploy fails, and
  the receipt records the failure with `healthCheckPassed: false`
- **Remove the decrypted files.** GitHub-hosted runners are thrown away
  after the job; on a self-hosted runner this step is what keeps the values
  off its disk

If the project is not at the root of the repository, set `CLOUDFALL_PROJECT`
to its directory in the job's `env`.

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

Every `Application` declares `spec.approval: manual` or `automatic`.
Cloudfall validates the field and writes it into the rendered inventory, but
no command acts on it today: `cloudfall deploy --yes` deploys an application
marked `manual` exactly as it deploys one marked `automatic`. The only gate
inside `deploy` is `--yes`, and a workflow that passes `--yes` has approved
its own deploy.

So in CI, the approval comes from GitHub, not from Cloudfall:

- Give every environment that holds an application marked `manual` required
  reviewers, and turn on "Prevent self-review", so the person who pushed the
  change cannot approve its deploy
- The job waits before its first step until a reviewer approves, so it never
  sees the SSH key or the age key of that environment without approval
- Leave `staging` without reviewers if its applications are `automatic`

Read `approval: manual` as your declaration that such a gate must exist; the
environment's reviewers are what enforce it. The approval itself lands in
GitHub's deployment history, not in Cloudfall's record.

## Keeping the receipts

`tmp/` is ignored by git, and the runner disappears with the job. The upload
step keeps, for 90 days:

- `tmp/artifacts/<component>/<release>.json`: the artifact metadata, with the
  commit and the archive's SHA-256
- `tmp/releases/<component>.json`: the release receipt from `deploy`; after
  a failed command health check its `previousRelease` names the release that
  serves again (`null` when a first release was stopped)
- `tmp/env-receipts/<component>.json`: the key names and hash of the
  rendered environment file, never the values

It never uploads `tmp/env/`, which holds the decrypted values. Add
`tmp/artifacts/` whole if you want the archives kept as well.

If you want the receipts in git, copy them to a tracked directory and commit
them back from the job. Give the job `contents: write` and add a step after
the deploy:

```yaml
      - name: Commit the receipt
        env:
          RELEASE: ${{ steps.build.outputs.release }}
        run: |
          mkdir -p receipts/releases
          cp "tmp/releases/$COMPONENT.json" "receipts/releases/$COMPONENT-$RELEASE.json"
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add receipts/releases
          git commit -m "Record $COMPONENT $RELEASE"
          git push
```

Only the kind directories are read as resources, so `receipts/` is safe in
the project. A push made with the workflow's own token does not start
another workflow run, so this does not loop. A branch protection rule on
`main` will refuse the push unless it allows the workflow to bypass it.

## What this guide does not cover

- Moving `artifact build` into the `cloudfall` CLI, so CI installs and pins
  one tool
- A published reusable action
- Converging the baseline, services or domains from CI; the same pattern
  applies, with the matching `cloudfall-engine playbook run` command
