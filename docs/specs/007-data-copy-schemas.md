# S-007: Data copy schemas

status: approved
serves: G-3

## Problem

`cloudfall data copy` dumps the whole source database: `cloudfall_data_migration` runs
`pg_dump --no-owner --no-privileges --format custom` with no schema or extension filter
(`engine/ansible/roles/cloudfall_data_migration/tasks/main.yml`). Its checks read only
`public`: the empty-target check and `cloudfall_data_migration_count_query` in
`defaults/main.yml` both filter `table_schema = 'public'`. So the dump copies every schema,
but the receipt's "per-table row counts matched" vouches for one of them. `pg_restore` runs
without `--exit-on-error` or `--single-transaction`, so a restore that hits a missing
extension or role keeps going, exits non-zero, and leaves a half-restored database that the
empty-target check (on `public` only) may not even see. A Supabase source shows all of it at
once: platform schemas, Supabase-only extensions, and RLS policies on `anon` and
`authenticated` (cloudfall-dev/cloudfall#82).

## Behaviour

**Choosing the schemas.** `cloudfall data copy` gains a repeatable `--source-schema NAME`
flag, and `cloudfall migrate` gains a repeatable `--data-schema DATABASE=NAME` flag beside its
`--data DATABASE=PATH`. The `data_copy` MCP tool, generated from the command, gains the
same `source_schema` list. The flag isn't `--schema` because the CLI framework (treaty)
reserves `--schema` on every command to print that command's schema; `--source-schema` pairs
with `--source-url-file` (#134). A flag, not a declared field, because a copy is configured by flags
today (`--database`, `--source-url-file`; `DataMigrateArgs` in `sdk/src/cloudfall/app.py`)
and the `Service` declares only the target: the source and what to take from it belong to
the one-time action, not to the desired state.

A schema name must match `^[a-z_][a-z0-9_]*$` and be at most 63 characters. A name that
doesn't, a name given twice, or a `--data-schema` for a database with no `--data` stops the
command with `PRECONDITION` before any playbook runs. The plan shown without `--yes` names
the schemas it will copy, or says it copies every non-system schema of the source.

**What gets copied.** With `--source-schema`, `pg_dump` gets one `--schema` per name and one
`--extension` per extension the preflight found the selected schemas need, so only those
schemas and their extensions reach the target. Without `--source-schema`, the dump command is
today's, unchanged, and the selected schemas are every schema of the source except
`pg_catalog`, `information_schema`, `pg_toast`, and the `pg_temp_*` and `pg_toast_temp_*`
families: what `pg_dump` dumps today.

**Checks cover exactly the selected schemas.** The empty-target check counts tables in the
selected schemas of the target, so a table in another schema doesn't block the copy. The
row-count query reads every base table of the selected schemas on both sides and returns
one `schema,table,rows` line per table, ordered by schema and table. The source must hold
at least one table across them.

**Preflight before dumping.** On the target host, after the empty-target check and before
`pg_dump`, the role reads the source and the target catalogs and collects every finding:

- `missingSchemas`: a named schema the source doesn't have
- `extensions`: an extension the selected schemas need that is not in the target's
  `pg_available_extensions`. "Need" means an object in a selected schema depends on it in
  `pg_depend`, or it is installed in a selected schema; without `--source-schema`, every extension
  installed in the source except `plpgsql` counts, since the whole dump creates them all
- `roles`: a role named in a row-level security policy (`pg_policy.polroles`) on a table in
  a selected schema that the target's `pg_roles` lacks, such as `anon` or `authenticated`
- `authReferences`: a function in a selected schema whose definition references `auth.`, or
  a policy whose `USING` or `WITH CHECK` expression does, when `auth` is not a selected
  schema. Each is named by its qualified name, as a blocker the team must rewrite

Any finding refuses the copy. Nothing is dumped, the target is untouched, no receipt is
written, and `data copy` (and the `data:<database>` step of `cloudfall migrate`) exits
`PRECONDITION` with code `data_copy_preflight_refused` and the four lists, each item named,
in its error context. The role writes the findings to a file under the `--data-migrations`
directory for the SDK to read, so the list isn't cut to the 2000-character output tail
that `_execute_step_with_output` in `sdk/src/cloudfall/lifecycle.py` keeps.

**No half-failed restore.** `pg_restore` runs with `--single-transaction`, which also stops
at the first error. A restore that fails rolls back whole: the target holds no table in the
selected schemas, no receipt is written, the command exits `ENGINE_STEP_FAILED`, and a
re-run needs no manual cleanup.

**The receipt.** `DataMigrationReceipt` keeps its id, path, and fields. `spec.schemas` lists
the copied schemas in sorted order, and each `spec.tables[]` entry gains `schema` beside
`table` and `rows`. Both are additions; a reader of `table` and `rows` keeps working.

**Compatibility.** Without `--source-schema`, what is dumped and restored stays the same as 0.7.0:
the whole database. The default stays "every non-system schema", not `public`, because
`public` was only what the checks read, never what the copy covered. Narrowing the default
would silently drop schemas existing migrations copy today (an ORM's migration table, such
as Drizzle's `drizzle.__drizzle_migrations`, or an app's own schemas) while the receipt still
said the counts matched. Two things do change for a copy without `--source-schema`. The row-count
check now covers every copied schema, so a mismatch outside `public` that passed before now
fails. And the preflight can refuse, but only for extensions and policy roles the target
lacks, which make today's restore fail too; `auth` is a selected schema in this mode
whenever the source has one, so `authReferences` stays empty. A Supabase project is copied
with `--source-schema public` (or the app's own schemas), the way the refusal points to.

Touches `sdk/src/cloudfall/app.py` (`data copy`, `DataMigrateArgs`, `cloudfall migrate`),
`sdk/src/cloudfall/lifecycle.py` (`plan_data_migration`, `migrate_data`,
`preview_data_migration`), `sdk/src/cloudfall/migrate.py`, the `data_copy` MCP tool,
`engine/ansible/playbooks/data.yml`, `engine/ansible/roles/cloudfall_data_migration`
(`tasks/main.yml`, `defaults/main.yml`, `meta/argument_specs.yml`), and
`docs/neon-migration-guide.md` and `docs/render-migration-guide.md` where they describe the
copy.

## Acceptance criteria

- S-007-1: Against a source with tables in schemas `public`, `app`, and `audit`, `cloudfall data copy <service> --database <db> --source-url-file <file> --source-schema public --source-schema app --yes` leaves the target with the tables of `public` and `app` only, and the receipt's `spec.schemas` is `["app", "public"]`
- S-007-2: Against the same source, the copy without `--source-schema` leaves the target with the tables of all three schemas, as 0.7.0 does, and the receipt's `spec.schemas` is `["app", "audit", "public"]`
- S-007-3: Each `spec.tables[]` entry of the receipt has `schema`, `table`, and `rows`, and the entries are exactly the base tables of the selected schemas
- S-007-4: The rendered row-count query for schemas `app` and `public` reads only those two schemas and returns one `schema,table,rows` line per base table, ordered by schema and table
- S-007-5: A target with a table in a selected schema refuses the copy with `ENGINE_STEP_FAILED` naming the table count, as today, and a target whose only table is in a schema not selected goes ahead
- S-007-6: `--source-schema missing` for a schema the source lacks exits `PRECONDITION` with code `data_copy_preflight_refused` and `missingSchemas` of `["missing"]`, and no dump file is written
- S-007-7: A selected table using a type from an extension that is not in the target's `pg_available_extensions` exits `PRECONDITION` with code `data_copy_preflight_refused` naming that extension under `extensions`, the target holds no new table, and no receipt is written
- S-007-8: A policy `TO anon, authenticated` on a selected table, with neither role on the target, exits `PRECONDITION` with code `data_copy_preflight_refused` and `roles` of `["anon", "authenticated"]`
- S-007-9: With `--source-schema public`, a function `public.current_tenant()` whose body calls `auth.uid()` and a policy on `public.notes` whose `USING` calls `auth.uid()` are both named under `authReferences`, and the copy is refused
- S-007-10: A source with a missing extension, a missing role, and an `auth.` reference at once is refused with all three named in one answer
- S-007-11: A restore that fails after the preflight passes exits `ENGINE_STEP_FAILED`, leaves no table in the selected schemas of the target, writes no receipt, and a second run against the same target passes the empty-target check
- S-007-12: `--source-schema 'Bad;Name'`, `--source-schema app --source-schema app`, or `cloudfall migrate --data-schema crm=public` without `--data crm=<file>` exits `PRECONDITION` and runs no playbook
- S-007-13: `cloudfall migrate --data crm=<file> --data-schema crm=public --yes` copies only `public` into `crm`, as `data copy --source-schema public` does
- S-007-14: `cloudfall data copy` without `--yes` names the selected schemas in its plan, or says it copies every non-system schema when no `--source-schema` is given

## Out of scope

- A Supabase migration guide: it needs a proving run on a real Supabase project before it goes on the site, and nothing here depends on it
- Recreating Supabase Auth, Storage, or Realtime: those stay with Supabase or get replaced by the team. The preflight names the `auth.` references to rewrite and stops there
- Rewriting `auth.` references or creating missing extensions and roles on the target: Cloudfall refuses and lists them; the operator decides how to provide them
- Narrowing the default to `public`: it would change what existing migrations copy, as the Compatibility paragraph says
- Copying owners, grants, or roles: the copy keeps `--no-owner --no-privileges` and restores as the declared owner
- Selecting single tables: schemas are the unit the dump, the checks, and the preflight share

## Decisions relied on

- none

## Issues

- cloudfall-dev/cloudfall#124: S-007-1, S-007-2, S-007-3, S-007-4, S-007-5, S-007-14
- cloudfall-dev/cloudfall#125: S-007-6, S-007-7, S-007-8, S-007-9, S-007-10, S-007-11
- cloudfall-dev/cloudfall#126: S-007-12, S-007-13

## Verification
