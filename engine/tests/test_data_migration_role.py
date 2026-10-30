"""The data-migration role's schema selection, rendered with Ansible's templating.

The role needs a live source and a target PostgreSQL, so these tests replay
what it renders: the dump command, the psql variable that carries the
selected schemas into each query, the empty-target assertion, and the
receipt built from the row-count lines psql returns.
"""

from __future__ import annotations

import json
from pathlib import Path

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_data_migration"
PLAYBOOK = ROOT / "engine" / "ansible" / "playbooks" / "data.yml"
URL_PATH = "/root/.cloudfall-data-source-url"
DUMP_PATH = "/var/tmp/cloudfall-data-migration.dump"  # noqa: S108 - role default.
# The dump command 0.7.0 ran, which a copy without --schema keeps.
WHOLE_DATABASE_DUMP = (
    f'pg_dump --dbname "$(cat {URL_PATH})" '
    "--no-owner --no-privileges --format custom "
    f"--file {DUMP_PATH}"
)


def _defaults() -> dict[str, object]:
    defaults = DataLoader().load_from_file(str(ROLE / "defaults" / "main.yml"))
    assert isinstance(defaults, dict)
    return defaults


def _tasks() -> list[dict[str, object]]:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "main.yml"))
    assert isinstance(tasks, list)
    flat: list[dict[str, object]] = []
    for task in tasks:
        flat.append(task)
        for section in ("block", "always"):
            nested = task.get(section, [])
            assert isinstance(nested, list)
            flat.extend(nested)
    return flat


def _task(name: str) -> dict[str, object]:
    return next(task for task in _tasks() if task.get("name") == name)


def _module(name: str, module: str) -> dict[str, object]:
    arguments = _task(name)[module]
    assert isinstance(arguments, dict)
    return arguments


def _trusted(value: object) -> object:
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, list):
        return [_trusted(item) for item in value]
    if isinstance(value, dict):
        return {key: _trusted(item) for key, item in value.items()}
    return value


def _render(value: object, variables: dict[str, object]) -> object:
    templar = Templar(loader=DataLoader(), variables={**_defaults(), **variables})
    return templar.template(_trusted(value))


def _holds(expression: object, variables: dict[str, object]) -> bool:
    templar = Templar(loader=DataLoader(), variables={**_defaults(), **variables})
    return bool(templar.evaluate_conditional(trust_as_template(str(expression))))


def _selected(named: list[str], source_schemas: list[str]) -> list[str]:
    """Replay how the role picks the schemas from --schema or the source."""
    fact = _module("Select the schemas to copy", "ansible.builtin.set_fact")
    rendered = _render(
        fact["cloudfall_data_migration_selected_schemas"],
        {
            "cloudfall_data_migration_schemas": named,
            # A named selection skips the source read, as the role does.
            "cloudfall_data_migration_source_schemas": (
                {"skipped": True}
                if named
                else {"stdout": json.dumps(source_schemas)}
            ),
        },
    )
    assert isinstance(rendered, list)
    return [str(schema) for schema in rendered]


def _schemas_variable(selected: list[str]) -> str:
    fact = _module(
        "Pass the selected schemas to psql as one JSON variable",
        "ansible.builtin.set_fact",
    )
    return str(
        _render(
            fact["cloudfall_data_migration_schemas_variable"],
            {"cloudfall_data_migration_selected_schemas": selected},
        )
    )


def _dump_command(named: list[str]) -> str:
    shell = _module(
        "Dump the source database on the target host", "ansible.builtin.shell"
    )
    return str(
        _render(
            shell["cmd"],
            {
                "cloudfall_data_migration_schemas": named,
                # The preflight found the schemas need no extension.
                "cloudfall_data_migration_needs": {"extensions": []},
            },
        )
    )


def _psql_substitute(sql: str, variable: str) -> str:
    """Do what psql does to ``:'cloudfall_data_schemas'``: quote it as a literal."""
    name, value = variable.split("=", 1)
    literal = "'" + value.replace("'", "''") + "'"
    return sql.replace(f":'{name}'", literal)


SOURCE_SCHEMAS = ["app", "audit", "public"]
SOURCE_LINES = [
    "app,accounts,3",
    "app,orders,12",
    "public,notes,5",
    'public,"odd, ""name""",1',
]


def _receipt(selected: list[str], lines: list[str]) -> dict[str, object]:
    tables_fact = _module(
        "Record the verified tables for the receipt", "ansible.builtin.set_fact"
    )
    counts = {"stdout_lines": lines}
    tables = _render(
        tables_fact["cloudfall_data_migration_tables"],
        {"cloudfall_data_migration_source_counts": counts},
    )
    copy = _module(
        "Write the data-migration receipt on the controller", "ansible.builtin.copy"
    )
    content = _render(
        copy["content"],
        {
            "cloudfall_data_migration_service": {"id": "postgresql-main"},
            "cloudfall_data_migration_database": "crm",
            "cloudfall_data_migration_owner": "crm",
            "cloudfall_server_id": "h1",
            "ansible_facts": {"date_time": {"iso8601": "2026-10-27T00:00:00Z"}},
            "cloudfall_data_migration_selected_schemas": selected,
            "cloudfall_data_migration_tables": tables,
        },
    )
    receipt = json.loads(str(content))
    assert isinstance(receipt, dict)
    return receipt


def test_s007_1_named_schemas_reach_pg_dump_and_the_receipt() -> None:
    selected = _selected(["public", "app"], SOURCE_SCHEMAS)
    command = _dump_command(["public", "app"])
    receipt = _receipt(selected, SOURCE_LINES[:3])

    assert selected == ["app", "public"]
    assert command == f"{WHOLE_DATABASE_DUMP} --schema '\"app\"' --schema '\"public\"'"
    spec = receipt["spec"]
    assert isinstance(spec, dict)
    assert spec["schemas"] == ["app", "public"]


def test_s007_1_a_schema_name_stays_one_quoted_pg_dump_argument() -> None:
    """A hostile name is one shell word and one literal pg_dump pattern."""
    command = _dump_command(['Bad;Name"$(touch x)'])

    assert command == (
        f"{WHOLE_DATABASE_DUMP} --schema '\"Bad;Name\"\"$(touch x)\"'"
    )


def test_s007_2_without_schema_the_dump_is_the_whole_database_as_in_0_7_0() -> None:
    selected = _selected([], SOURCE_SCHEMAS)
    receipt = _receipt(selected, SOURCE_LINES)

    assert _dump_command([]) == WHOLE_DATABASE_DUMP
    assert selected == ["app", "audit", "public"]
    spec = receipt["spec"]
    assert isinstance(spec, dict)
    assert spec["schemas"] == ["app", "audit", "public"]


def test_s007_2_source_schemas_query_skips_only_the_system_schemas() -> None:
    query = str(_defaults()["cloudfall_data_migration_source_schemas_query"])
    read = _task("Read every non-system schema of the source")

    assert "'pg_catalog', 'information_schema', 'pg_toast'" in query
    assert "'^pg_(toast_)?temp_'" in query
    assert "ORDER BY nspname" in query
    assert _holds(read["when"], {"cloudfall_data_migration_schemas": []})
    assert not _holds(read["when"], {"cloudfall_data_migration_schemas": ["app"]})


def test_s007_3_receipt_tables_carry_schema_table_and_rows() -> None:
    receipt = _receipt(["app", "public"], SOURCE_LINES)

    spec = receipt["spec"]
    assert isinstance(spec, dict)
    assert spec["tables"] == [
        {"schema": "app", "table": "accounts", "rows": 3},
        {"schema": "app", "table": "orders", "rows": 12},
        {"schema": "public", "table": "notes", "rows": 5},
        {"schema": "public", "table": 'odd, "name"', "rows": 1},
    ]
    assert spec["verification"] == "per-table row counts matched"


def test_s007_4_row_count_query_reads_only_the_selected_schemas() -> None:
    variable = _schemas_variable(["app", "public"])
    query = _psql_substitute(
        str(_defaults()["cloudfall_data_migration_count_query"]), variable
    )

    assert variable == 'cloudfall_data_schemas=["app", "public"]'
    assert "SELECT table_schema, table_name," in query
    assert (
        "WHERE table_schema IN\n"
        "  (SELECT jsonb_array_elements_text('[\"app\", \"public\"]'::jsonb))\n"
        "  AND table_type = 'BASE TABLE'"
    ) in query
    assert query.rstrip().endswith("ORDER BY table_schema, table_name")
    assert "'public'" not in query.replace('"public"', "")


def test_s007_4_both_sides_pass_the_schemas_to_the_row_count_query() -> None:
    variables: dict[str, object] = {
        "cloudfall_data_migration_schemas_variable": _schemas_variable(
            ["app", "public"]
        ),
        "cloudfall_data_migration_database": "crm",
    }
    source = _module("Read source table row counts", "ansible.builtin.shell")
    restored = _module("Read restored table row counts", "ansible.builtin.command")
    query = str(_defaults()["cloudfall_data_migration_count_query"])

    source_cmd = str(_render(source["cmd"], variables))
    restored_argv = _render(restored["argv"], variables)
    assert isinstance(restored_argv, list)
    assert "--set ON_ERROR_STOP=1" in source_cmd
    assert "--set 'cloudfall_data_schemas=[\"app\", \"public\"]'" in source_cmd
    assert restored_argv[-2:] == ["--set", 'cloudfall_data_schemas=["app", "public"]']
    assert _render(source["stdin"], variables) == f"{query};"
    assert _render(restored["stdin"], variables) == f"{query};"


def test_s007_5_empty_target_check_counts_only_the_selected_schemas() -> None:
    count = _module(
        "Count existing tables of the selected schemas in the target",
        "ansible.builtin.command",
    )
    refuse = _module(
        "Refuse to restore into a non-empty database", "ansible.builtin.assert"
    )
    variable = _schemas_variable(["public"])
    variables: dict[str, object] = {
        "cloudfall_data_migration_schemas_variable": variable,
        "cloudfall_data_migration_database": "crm",
    }
    query = _psql_substitute(
        str(_defaults()["cloudfall_data_migration_target_tables_query"]), variable
    )
    argv = _render(count["argv"], variables)
    conditions = refuse["that"]
    assert isinstance(conditions, list)
    (condition,) = conditions

    assert isinstance(argv, list)
    assert argv[-1] == 'cloudfall_data_schemas=["public"]'
    assert "jsonb_array_elements_text('[\"public\"]'::jsonb)" in query
    assert "'public'" not in query
    target = "cloudfall_data_migration_target_tables"
    assert _holds(condition, {target: {"stdout": "0\n"}})
    assert not _holds(condition, {target: {"stdout": "3"}})
    message = _render(
        refuse["fail_msg"],
        {target: {"stdout": "3"}, "cloudfall_data_migration_database": "crm"},
    )
    assert "already contains 3 tables" in str(message)


NO_FINDINGS: dict[str, list[str]] = {
    "missingSchemas": [],
    "extensions": [],
    "roles": [],
    "authReferences": [],
}


def _task_names() -> list[str]:
    return [str(task.get("name")) for task in _tasks()]


def _before(first: str, second: str) -> bool:
    names = _task_names()
    return names.index(first) < names.index(second)


def _findings(
    needs: dict[str, list[str]], target: dict[str, list[str]]
) -> dict[str, object]:
    """Replay how the role merges the source and target catalog answers."""
    fact = _module("Collect every preflight finding", "ansible.builtin.set_fact")
    rendered = _render(
        fact["cloudfall_data_migration_findings"],
        {
            "cloudfall_data_migration_needs": needs,
            "cloudfall_data_migration_target_preflight": {
                "stdout": json.dumps(target)
            },
        },
    )
    assert isinstance(rendered, dict)
    return rendered


def _refuses(findings: dict[str, object]) -> str | None:
    """Return the refusal message, or None when the preflight passes."""
    refuse = _module("Refuse a copy the target can't hold", "ansible.builtin.assert")
    conditions = refuse["that"]
    assert isinstance(conditions, list)
    variables: dict[str, object] = {"cloudfall_data_migration_findings": findings}
    if all(_holds(condition, variables) for condition in conditions):
        return None
    return str(_render(refuse["fail_msg"], variables))


def _findings_file(findings: dict[str, object]) -> tuple[str, object]:
    write = _module(
        "Write the preflight findings on the controller", "ansible.builtin.copy"
    )
    variables: dict[str, object] = {
        "cloudfall_data_migration_findings": findings,
        "cloudfall_data_migration_receipt_directory": "/srv/fleet/tmp/data-migrations",
        "cloudfall_data_migration_service": {"id": "postgresql-main"},
        "cloudfall_data_migration_database": "crm",
    }
    destination = str(_render(write["dest"], variables))
    return destination, json.loads(str(_render(write["content"], variables)))


def _source_preflight_sql(schemas: list[str], *, every_schema: bool) -> str:
    query = str(_defaults()["cloudfall_data_migration_source_preflight_query"])
    query = _psql_substitute(query, _schemas_variable(schemas))
    return _psql_substitute(
        query, f"cloudfall_data_every_schema={str(every_schema).lower()}"
    )


def _source_preflight_command(named: list[str]) -> str:
    shell = _module(
        "Read what the selected schemas need from the source",
        "ansible.builtin.shell",
    )
    return str(
        _render(
            shell["cmd"],
            {
                "cloudfall_data_migration_schemas": named,
                "cloudfall_data_migration_schemas_variable": _schemas_variable(
                    named or ["app"]
                ),
            },
        )
    )


def _target_preflight_argv(needs: dict[str, list[str]]) -> list[str]:
    command = _module(
        "Read which needed extensions and policy roles the target lacks",
        "ansible.builtin.command",
    )
    argv = _render(
        command["argv"],
        {
            "cloudfall_data_migration_needs": needs,
            "cloudfall_data_migration_database": "crm",
        },
    )
    assert isinstance(argv, list)
    return [str(argument) for argument in argv]


def test_s007_6_a_missing_schema_is_refused_before_the_dump() -> None:
    needs = {**NO_FINDINGS, "missingSchemas": ["missing"], "policyRoles": []}
    findings = _findings(needs, {"extensions": [], "roles": []})
    destination, written = _findings_file(findings)
    sql = _source_preflight_sql(["missing"], every_schema=False)

    assert findings["missingSchemas"] == ["missing"]
    assert written == {**NO_FINDINGS, "missingSchemas": ["missing"]}
    assert destination == (
        "/srv/fleet/tmp/data-migrations/postgresql-main-crm.preflight.json"
    )
    assert "Missing schemas ['missing']" in str(_refuses(findings))
    # The preflight runs after the empty-target check, and refuses before
    # pg_dump writes the dump file.
    assert _before(
        "Refuse to restore into a non-empty database",
        "Read what the selected schemas need from the source",
    )
    assert _before(
        "Refuse a copy the target can't hold",
        "Dump the source database on the target host",
    )
    assert _before(
        "Write the preflight findings on the controller",
        "Refuse a copy the target can't hold",
    )
    assert "jsonb_array_elements_text('[\"missing\"]'::jsonb)" in sql
    assert "'missing'" not in sql


def test_s007_6_the_source_preflight_takes_names_only_as_psql_variables() -> None:
    query = str(_defaults()["cloudfall_data_migration_source_preflight_query"])
    named = _source_preflight_command(['Bad;Name"$(touch x)'])
    every = _source_preflight_command([])

    assert ":'cloudfall_data_schemas'::jsonb" in query
    assert ":'cloudfall_data_every_schema'::boolean" in query
    assert "{{" not in query
    assert "--set ON_ERROR_STOP=1" in named
    assert "--set cloudfall_data_every_schema=false" in named
    assert "--set cloudfall_data_every_schema=true" in every
    assert "touch" not in named.replace(
        "'cloudfall_data_schemas=[\"Bad;Name\\\"$(touch x)\"]'", ""
    )
    assert '"$(cat /root/.cloudfall-data-source-url)"' in named


def test_s007_7_an_extension_the_target_lacks_is_refused() -> None:
    needs = {**NO_FINDINGS, "extensions": ["citext", "pg_trgm"], "policyRoles": []}
    argv = _target_preflight_argv(needs)
    target = str(_defaults()["cloudfall_data_migration_target_preflight_query"])
    findings = _findings(needs, {"extensions": ["citext"], "roles": []})
    sql = _source_preflight_sql(["public"], every_schema=False)

    assert argv[-4:] == [
        "--set",
        'cloudfall_data_extensions=["citext", "pg_trgm"]',
        "--set",
        "cloudfall_data_policy_roles=[]",
    ]
    assert ":'cloudfall_data_extensions'::jsonb" in target
    assert "pg_catalog.pg_available_extensions" in target
    assert findings["extensions"] == ["citext"]
    assert "extensions the target lacks ['citext']" in str(_refuses(findings))
    # A column's type is a member of the extension: two pg_depend hops.
    assert "d.refclassid = 'pg_catalog.pg_extension'::regclass" in sql
    assert "d.deptype = 'e'" in sql
    assert "e.extnamespace IN (SELECT oid FROM selected_ns)" in sql
    assert "e.extname <> 'plpgsql'" in sql


def test_s007_7_an_array_or_domain_of_an_extension_type_names_it() -> None:
    sql = _source_preflight_sql(["public"], every_schema=False)
    needed = sql[sql.index("needed AS (") : sql.index("missing AS (")]

    # A citext[] column depends on _citext, which depends on citext with
    # deptype 'i', not on the extension: the walk follows an array to its
    # element type and a domain to its base type, chained, before it
    # matches extension members, in the one query that feeds both the
    # extensions finding and pg_dump --extension.
    assert sql.startswith("WITH RECURSIVE selected AS (")
    assert "referenced(classid, objid) AS (" in sql
    assert "FROM referenced r JOIN pg_catalog.pg_type t" in sql
    assert "WHEN t.typcategory = 'A' THEN t.typelem" in sql
    assert "WHEN t.typtype = 'd' THEN t.typbasetype" in sql
    assert "FROM referenced r JOIN members m" in needed


def test_s007_7_named_schemas_dump_their_needed_extensions() -> None:
    shell = _module(
        "Dump the source database on the target host", "ansible.builtin.shell"
    )
    needs = {**NO_FINDINGS, "extensions": ["citext", "uuid-ossp"]}

    named = _render(
        shell["cmd"],
        {
            "cloudfall_data_migration_schemas": ["public"],
            "cloudfall_data_migration_needs": needs,
        },
    )
    every = _render(
        shell["cmd"],
        {
            "cloudfall_data_migration_schemas": [],
            "cloudfall_data_migration_needs": needs,
        },
    )

    assert named == (
        f"{WHOLE_DATABASE_DUMP} --schema '\"public\"' "
        "--extension '\"citext\"' --extension '\"uuid-ossp\"'"
    )
    assert every == WHOLE_DATABASE_DUMP
    # An extension name read from the source stays one quoted shell word.
    hostile = _render(
        shell["cmd"],
        {
            "cloudfall_data_migration_schemas": ["public"],
            "cloudfall_data_migration_needs": {"extensions": ['x";$(touch y)']},
        },
    )
    assert str(hostile).endswith("--extension '\"x\"\";$(touch y)\"'")


def test_s007_8_policy_roles_the_target_lacks_are_refused() -> None:
    needs = {**NO_FINDINGS, "policyRoles": ["anon", "authenticated"]}
    argv = _target_preflight_argv(needs)
    target = str(_defaults()["cloudfall_data_migration_target_preflight_query"])
    sql = _source_preflight_sql(["public"], every_schema=False)
    findings = _findings(needs, {"extensions": [], "roles": ["anon", "authenticated"]})

    assert argv[-1] == 'cloudfall_data_policy_roles=["anon", "authenticated"]'
    assert ":'cloudfall_data_policy_roles'::jsonb" in target
    assert "pg_catalog.pg_roles" in target
    assert "unnest(y.polroles)" in sql
    assert findings["roles"] == ["anon", "authenticated"]
    assert "policy roles the target lacks ['anon', 'authenticated']" in str(
        _refuses(findings)
    )


def test_s007_9_auth_references_name_the_function_and_the_policy() -> None:
    references = [
        "function public.current_tenant()",
        "policy notes_owner on public.notes",
    ]
    needs = {**NO_FINDINGS, "authReferences": references, "policyRoles": []}
    findings = _findings(needs, {"extensions": [], "roles": []})
    sql = _source_preflight_sql(["public"], every_schema=False)

    assert findings["authReferences"] == references
    assert "auth. references" in str(_refuses(findings))
    assert "format('function %I.%I(%s)', n.nspname, p.proname," in sql
    assert "format('policy %I on %I.%I', y.polname, n.nspname, c.relname)" in sql
    assert "pg_get_expr(y.polqual, y.polrelid)" in sql
    assert "pg_get_expr(y.polwithcheck, y.polrelid)" in sql
    assert "p.prosrc ~ '\\mauth\\.'" in sql
    # A selected auth schema copies auth too, so nothing is reported.
    assert "SELECT EXISTS (SELECT 1 FROM selected WHERE nspname = 'auth')" in sql


def test_s007_10_every_finding_is_named_in_one_answer() -> None:
    needs = {
        "missingSchemas": [],
        "extensions": ["postgis"],
        "policyRoles": ["anon"],
        "authReferences": ["function public.current_tenant()"],
    }
    findings = _findings(needs, {"extensions": ["postgis"], "roles": ["anon"]})
    _, written = _findings_file(findings)
    message = str(_refuses(findings))

    assert written == {
        "missingSchemas": [],
        "extensions": ["postgis"],
        "roles": ["anon"],
        "authReferences": ["function public.current_tenant()"],
    }
    for named in ("postgis", "anon", "public.current_tenant()"):
        assert named in message
    clean = _findings(
        {**NO_FINDINGS, "policyRoles": []}, {"extensions": [], "roles": []}
    )
    assert _refuses(clean) is None


def test_s007_11_the_restore_is_one_transaction_and_the_receipt_follows_it() -> None:
    restore = _module(
        "Restore the dump into the declared database", "ansible.builtin.command"
    )
    argv = restore["argv"]
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "main.yml"))
    assert isinstance(tasks, list)
    top = [task.get("name") for task in tasks]
    (block,) = [task for task in tasks if "block" in task]

    assert isinstance(argv, list)
    assert argv[:2] == ["/usr/bin/pg_restore", "--single-transaction"]
    # A failing block skips every task after it: no receipt is written.
    assert top.index(block["name"]) < top.index(
        "Write the data-migration receipt on the controller"
    )
    assert "Restore the dump into the declared database" in [
        task["name"] for task in block["block"]
    ]


def test_the_data_playbook_passes_the_named_schemas_to_the_role() -> None:
    plays = DataLoader().load_from_file(str(PLAYBOOK))
    assert isinstance(plays, list)
    include = plays[0]["tasks"][0]
    value = include["vars"]["cloudfall_data_migration_schemas"]

    assert _render(value, {"cloudfall_data_schemas": ["app"]}) == ["app"]
    assert _render(value, {}) == []
