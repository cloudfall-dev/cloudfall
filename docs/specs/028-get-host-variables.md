# S-028: A host's merged variables, and the file that sets each

status: approved
serves: G-4

## Problem

Item W-4 of the accepted plan for G-4 (cloudfall-dev/cloudfall#188, issue
cloudfall-dev/cloudfall#620): `cloudfall get host` on a team's repository returns a host's
merged variables and, for each, the file that sets the winning value. The plan's Order says
this closes the exit of the roadmap's M16, "on a proving repository with an INI or YAML
inventory and playbooks Cloudfall did not write, `cloudfall get host` returns the merged
variables and the file that wins".

Cloudfall already reads a team's inventory in process (`cloudfall.ansible_api`, behind
`inventory show`, `audit`, `observe` and `mcp serve --repository`), and that read applies
Ansible's precedence, but it keeps only the result. Neither a person nor an agent can ask it
what a host will run with, or why: which of `group_vars/all.yml`, `group_vars/web.yml`, a
`[web:vars]` line in the INI file or `host_vars/web-3.yml` decided `nginx_workers`. Today the
agent answers that by reading files and replaying Ansible's precedence in its head (group
depth, `ansible_group_priority`, the order of group names, inventory file against
`group_vars`), which is the guess Q-1 forbids. The brownfield design (part 6) names
`get host` as the read that makes precedence visible before anything is changed; the writes
it goes with (`set`, `unset`, `add host`) stay in M17.

A variable can hold a secret. A team keeps those in Ansible Vault, and nothing an agent can
call may return one (`docs/secrets-guide.md`, Q-4).

## Behaviour

### The command

```console
cloudfall get host <name> [--repository <dir>] [--inventory <path>]
```

- A new command group `get` in `sdk/src/cloudfall/app.py` with one command, `host`, of
  effect `read`, registered in `sdk/src/cloudfall/commands.py` as a `CommandContract` with
  its output shape
- `--repository` is the Ansible repository, the current directory by default, as for
  `operations` and `mcp serve --repository`. `--inventory` is the inventory inside it, by
  default the first source its `ansible.cfg` names (`inventory_from_config`); a relative path
  resolves against the repository
- `<name>` is one host as the inventory spells it (`InventoryHostname`). It is not a host
  pattern: a name holding a pattern character (`*`, `?`, `[`, `:`, `,`, `!`, `&`, `~`) is a
  usage error, exit 2, naming the argument
- It reads every host of the inventory, those without a `cloudfall` block included: it does
  not go through `read_fleet` and needs no host to declare one
- It writes nothing: no file in the repository or under it changes, and no record, decision
  or receipt is written

### Which variables, and which file wins

The command answers the variables a host has before any play runs: the inventory's levels of
Ansible's variable precedence, read as `ansible-playbook` would read them when started in the
repository. From lowest to highest:

| Level (`level` in the answer) | Where |
|---|---|
| `inventory_group_vars` | Variables a group carries in the inventory source itself: `[web:vars]` in INI, `vars:` under a group in YAML |
| `inventory_group_vars_all` | `group_vars/all` beside the inventory |
| `playbook_group_vars_all` | `group_vars/all` in the repository, when the inventory is elsewhere |
| `inventory_group_vars_files` | `group_vars/<group>` beside the inventory |
| `playbook_group_vars_files` | `group_vars/<group>` in the repository, when the inventory is elsewhere |
| `inventory_host_vars` | Variables on the host's line or entry in the inventory source |
| `inventory_host_vars_files` | `host_vars/<host>` beside the inventory |
| `playbook_host_vars_files` | `host_vars/<host>` in the repository, when the inventory is elsewhere |

Within a group level, groups apply in Ansible's order: by depth (`all`, then parents, then
children), then by `ansible_group_priority`, then by name; the later one wins. A
`group_vars/<group>` or `host_vars/<host>` path may be a file (`.yml`, `.yaml`, `.json` or no
suffix) or a directory of files, which apply in Ansible's own order. Each file is named by its
path relative to the repository. When the repository is the inventory's directory, a file is
listed once, at its inventory level.

The repository is passed to ansible-core as the playbook directory, as
`ansible-inventory --playbook-dir` does, so `group_vars` and `host_vars` beside the
playbooks count as they do in a run. Play vars, role defaults and vars, facts, `set_fact`,
`include_vars` and extra vars are not read; the answer says so in `data.scope`, `inventory`.
Variables Ansible defines itself (`inventory_hostname`, `group_names`, `groups`,
`playbook_dir`, `ansible_version` and the other magic variables) are not listed.

The merged values are ansible-core's own, read in process through `cloudfall.ansible_api`
and nowhere else (`sdk/src/cloudfall/ansible_api.py`), with the existing fallback: when the
in-process API fails on a release, the values come from
`ansible-inventory --host <name> --playbook-dir <repository>` and the answer reports
`inProcess: false`. The file that sets each winning value comes from Cloudfall's own walk of
the levels above, over the files Ansible would read, in that order: the winner is the last
file that defines the variable. Before naming it, the walk checks that the value that file
holds is the merged value ansible-core reported. When it is not (a vars plugin other than
`host_group_vars`, `hash_behaviour = merge`, a value a dynamic inventory plugin composes), the
variable's `file` and `level` are `null` and it is listed under `data.unattributed`: the
command says it does not know rather than naming a file that does not decide the value (Q-1).

### The answer

```json
{
  "status": "ok",
  "host": "web-3",
  "groups": ["all", "production", "web"],
  "repository": "/srv/ansible",
  "inventory": "inventory/hosts.ini",
  "inProcess": true,
  "scope": "inventory",
  "variables": {
    "nginx_workers": {
      "value": 8,
      "file": "host_vars/web-3.yml",
      "level": "inventory_host_vars_files",
      "overridden": [
        {"file": "group_vars/web.yml", "level": "inventory_group_vars_files", "group": "web"},
        {"file": "group_vars/all.yml", "level": "inventory_group_vars_all", "group": "all"}
      ]
    },
    "db_password": {
      "value": {"vaulted": true},
      "vaulted": true,
      "file": "group_vars/production/vault.yml",
      "level": "inventory_group_vars_files",
      "group": "production",
      "overridden": []
    }
  },
  "unattributed": [],
  "unreadable": []
}
```

- `variables` maps each variable name to its entry: the winning `value`, the `file` and
  `level` that set it (with `group` on a group level), and `overridden`, the lower files that
  also define it, highest first
- The output is `--format json` like every command, and its human rendering lists one
  variable per line with its file

### Secrets

- A vaulted value is never printed, decrypted or not: an inline `!vault` value, a value from
  a file Ansible Vault encrypts whole, and a vaulted value nested in a dictionary or list are
  each replaced by `{"vaulted": true}` in place, and the variable's entry carries
  `"vaulted": true`. No hash of a vaulted value is printed
- Ansible's own connection secrets (`ansible_password`, `ansible_ssh_pass`,
  `ansible_become_password`, `ansible_become_pass`, `ansible_ssh_private_key_passphrase`) are
  replaced the same way, `{"redacted": true}`, when they are not vaulted. Every other plain
  value is printed as the file holds it
- A file Ansible Vault encrypts whole is read with the vault secrets Ansible itself would use
  in the repository (`vault_password_file` and `vault_identity_list` in `ansible.cfg`,
  `ANSIBLE_VAULT_PASSWORD_FILE`, `ANSIBLE_VAULT_IDENTITY_LIST`); the command never prompts.
  The plaintext stays in the process, used to learn the file's variable names and to decide
  precedence. With no secret that opens it, the file is listed under `data.unreadable`, the
  command exits `INVENTORY_UNREADABLE` with code `ansible_vault_locked`, and the message names
  the file and the `ansible.cfg` setting that would open it
- The same holds through `mcp serve --repository`, the only way an agent reaches it

### Refusals

| Case | Exit | Code |
|---|---|---|
| No `--inventory` and no `ansible.cfg` naming one in the repository, a Cloudfall project included; the message names `cloudfall inventory show` for a project | `INVENTORY_UNREADABLE` | `fleet_inventory_undeclared` |
| The inventory or its `ansible.cfg` cannot be read | `INVENTORY_UNREADABLE` | the existing `ansible_*` codes |
| A vaulted file with no secret that opens it | `INVENTORY_UNREADABLE` | `ansible_vault_locked` |
| `<name>` is no host of the inventory | `NOT_FOUND` | `inventory_host_unknown`, the message naming the inventory |
| `<name>` is a host pattern | usage error, exit 2 | |

### Through an agent

`get.host` joins `FLEET_COMMANDS` in `sdk/src/cloudfall/agent_server.py`, so
`cloudfall mcp serve --repository` serves it as the read-only tool `get_host` with the
repository and inventory bound by the server, like `inventory.show`. It is not served on a
project.

### Docs

`sdk/README.md` and `docs/brownfield-design.md` (part 6: `get host` built, the writes not)
describe the command; `ROADMAP.md` marks the `get host` part of M16's exit done.

Touches `sdk/src/cloudfall/app.py` (`get host`), `sdk/src/cloudfall/ansible_api.py` (the
host read with sources, vault handling), a new `sdk/src/cloudfall/host_variables.py` (the
precedence walk, attribution and redaction, importing nothing from `ansible.*`),
`sdk/src/cloudfall/commands.py`, `sdk/src/cloudfall/agent_server.py`, `sdk/README.md`,
`docs/brownfield-design.md` and `ROADMAP.md`.

## Acceptance criteria

- S-028-1: On a fixture repository with an INI inventory, `ansible.cfg`, `group_vars/all.yml`,
  `group_vars/web.yml`, a `[web:vars]` section and `host_vars/web-3.yml` that each set
  `nginx_workers`, `cloudfall get host web-3` run in the repository exits 0, and
  `data.variables.nginx_workers` holds the value from `host_vars/web-3.yml`, `file`
  `host_vars/web-3.yml`, `level` `inventory_host_vars_files`, and the three other places in
  `overridden`, highest first; the value is the one `ansible-inventory --host web-3` reports
- S-028-2: On the same repository with the inventory in YAML, a variable set by
  `group_vars/all.yml` and by a group's `vars:` in the inventory file answers
  `group_vars/all.yml` at `inventory_group_vars_all`; one set on the host's own entry and in
  `group_vars/web.yml` answers the inventory file at `inventory_host_vars`
- S-028-3: A variable set by a parent group and its child answers the child's file; two
  sibling groups that set it answer the one with the higher `ansible_group_priority`, and with
  equal priority the one whose name sorts last; each matches `ansible-inventory --host`
- S-028-4: `group_vars/production/` as a directory of two files names the file that sets the
  winning value; `group_vars` beside the playbooks at the repository root, with the inventory
  in `inventory/`, count at the playbook levels and win over `group_vars` beside the inventory
- S-028-5: A host with no `cloudfall` block, in an inventory where no host has one, answers
  exit 0 with its variables
- S-028-6: `data.variables` holds no magic variable (`inventory_hostname`, `groups`,
  `group_names`, `playbook_dir`), and `data.scope` is `inventory`
- S-028-7: An inline `!vault` value, a value in a file vault-encrypted whole (opened through
  `vault_password_file` in `ansible.cfg`), and a vaulted value nested in a dictionary each
  appear as `{"vaulted": true}` with `"vaulted": true` on the variable's entry, and the
  plaintext of none of them appears anywhere in the output, as JSON or as text
- S-028-8: An unvaulted `ansible_become_password` appears as `{"redacted": true}`; an
  unvaulted `app_port` appears as its value
- S-028-9: With a vault-encrypted vars file and no vault secret configured, the command exits
  `INVENTORY_UNREADABLE` with code `ansible_vault_locked` and names the file, and it never
  waits for input
- S-028-10: A variable whose merged value is not the value of the last file that defines it
  (with `hash_behaviour = merge` and a dictionary set in two files) has `file` and `level`
  `null` and is listed in `data.unattributed`
- S-028-11: With the in-process read unavailable, the command answers through
  `ansible-inventory` with `data.inProcess` `false` and the same variables, values and files as
  in process
- S-028-12: An unknown host exits `NOT_FOUND` with code `inventory_host_unknown`; `web-*` is
  refused as a usage error, exit 2; a directory with no `ansible.cfg`, a Cloudfall project
  included, exits `INVENTORY_UNREADABLE` with `fleet_inventory_undeclared`, the message naming
  `cloudfall inventory show` for the project
- S-028-13: After any run, `git status` of the fixture repository is unchanged and no file is
  written under it
- S-028-14: `cloudfall mcp serve --repository` lists the tool `get_host` as read-only, a call
  with only `name` answers the same `data` as the CLI with the repository and inventory the
  server was started with, and a project's server does not list it
- S-028-15: The command catalog in `sdk/src/cloudfall/commands.py` holds `get host` with
  effect `read` and the output keys of the answer above, the `AGENTS.md` that `cloudfall init`
  renders lists it among the read commands, and `sdk/README.md` and
  `docs/brownfield-design.md` describe it

## Out of scope

- `set`, `unset`, `add host`, `remove host` and every other write to the team's files (M17,
  safe edits)
- `get group`, `get play`, `get file`, and reading one variable by path
  (`get host web-3 nginx.workers`)
- Variables a play adds: play vars, `vars_files`, role defaults and vars, facts, `set_fact`,
  `include_vars`, extra vars, and `group_vars` beside a playbook in a subdirectory
- Line numbers of the winning value
- Printing a hash or a fingerprint of a vaulted value, and redacting plain values by name
  beyond Ansible's connection secrets
- Attributing values that vars plugins other than `host_group_vars`, or dynamic inventory
  plugins, compute; those are reported as unattributed
- `get host` on a Cloudfall project, and `cloudfall init` emitting a plain inventory (the
  plan's Notes)
- Deploy on a team's repository (W-2, cloudfall-dev/cloudfall#618) and backups there (W-3)
- A latency target: the brownfield design's 250 ms is a goal to measure, not a criterion

## Decisions relied on

- D-16: `get host` writes nothing to `record/`, so no record gains a value it reads, and
  nothing it answers holds a vaulted value

## Issues

## Verification

Filled in when the spec reaches `built`: one line per criterion id, saying how it was
checked against the default branch (beyond its tests) and the result.
