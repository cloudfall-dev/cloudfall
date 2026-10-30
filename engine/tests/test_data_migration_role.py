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
    return str(_render(shell["cmd"], {"cloudfall_data_migration_schemas": named}))


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


def test_the_data_playbook_passes_the_named_schemas_to_the_role() -> None:
    plays = DataLoader().load_from_file(str(PLAYBOOK))
    assert isinstance(plays, list)
    include = plays[0]["tasks"][0]
    value = include["vars"]["cloudfall_data_migration_schemas"]

    assert _render(value, {"cloudfall_data_schemas": ["app"]}) == ["app"]
    assert _render(value, {}) == []
