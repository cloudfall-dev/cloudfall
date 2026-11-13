"""The PostgreSQL restore drill compares the restored rows with the dump.

The drill runs for real against stand-ins for the PostgreSQL client tools:
``pg_restore`` prints a fixed data-only script for the dump, as pg_dump wrote
it, and ``psql`` answers the restored database's per-table row counts. The
role's scratch-name assert is evaluated with Ansible's own templating.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_postgresql"
TEMPLATES = ROLE / "templates"
# What pg_restore prints for a dump of crm without --dbname: its data blocks.
DUMP_DATA = """\
--
-- PostgreSQL database dump
--

SELECT pg_catalog.set_config('search_path', '', false);

--
-- Data for Name: customers; Type: TABLE DATA; Schema: public; Owner: crm
--

COPY public.customers (id, name) FROM stdin;
1\tAda
2\tGrace FROM stdin;
\\.


--
-- Data for Name: Order Lines; Type: TABLE DATA; Schema: sales; Owner: crm
--

COPY sales."Order Lines" (id, "order id") FROM stdin;
1\t1
2\t1
3\t2
\\.


--
-- Data for Name: empty; Type: TABLE DATA; Schema: public; Owner: crm
--

COPY public.empty  FROM stdin;
\\.


--
-- PostgreSQL database dump complete
--

"""
RESTORED = 'public.customers\t2\npublic.empty\t0\nsales."Order Lines"\t3\n'


def _render(directory: Path, *names: str) -> str:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - shell scripts are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    return environment.get_template("restore-check.sh.j2").render(
        cloudfall_postgresql_service={
            "bind": {"address": "127.0.0.1", "port": 5432},
            "postgresql": {
                "databases": [
                    {"name": name, "application": name, "owner": name}
                    for name in names
                ]
            },
            "backup": {"directory": str(directory)},
        }
    )


def _stand_in(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n")
    path.chmod(0o755)


def _drill(
    tmp_path: Path,
    dump_data: str,
    restored: str,
    *names: str,
    restored_with_children: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the rendered drill with stand-ins for the PostgreSQL tools.

    ``restored`` is what the restored database answers when each table is
    counted without its inheritance children (``FROM ONLY``);
    ``restored_with_children`` what it answers otherwise, ``restored`` when
    no table has children.
    """
    backups = tmp_path / "backups"
    backups.mkdir()
    for name in names or ("crm",):
        (backups / f"{name}-20261113T020000Z.dump").write_bytes(b"PGDMP")
    tools = tmp_path / "bin"
    tools.mkdir()
    (tmp_path / "dump.sql").write_text(dump_data)
    (tmp_path / "restored.tsv").write_text(restored)
    (tmp_path / "restored-with-children.tsv").write_text(
        restored if restored_with_children is None else restored_with_children
    )
    log = tmp_path / "calls.log"
    _stand_in(
        tools,
        "pg_restore",
        f'echo "pg_restore $*" >> "{log}"\n'
        'case " $* " in *" --dbname "*) exit 0 ;; esac\n'
        f'cat "{tmp_path / "dump.sql"}"',
    )
    _stand_in(
        tools,
        "psql",
        f'echo "psql $*" >> "{log}"\n'
        'case "$*" in\n'
        f'  *"FROM ONLY %I.%I"*) cat "{tmp_path / "restored.tsv"}" ;;\n'
        f'  *) cat "{tmp_path / "restored-with-children.tsv"}" ;;\n'
        "esac",
    )
    _stand_in(tools, "createdb", f'echo "createdb $*" >> "{log}"')
    _stand_in(tools, "dropdb", f'echo "dropdb $*" >> "{log}"')
    script = tmp_path / "restore-check.sh"
    script.write_text(_render(backups, *(names or ("crm",))))
    return subprocess.run(  # noqa: S603 - the rendered drill under test.
        ["/usr/bin/env", f"PATH={tools}:/usr/bin:/bin", "bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_restore_matching_the_dump_passes(tmp_path: Path) -> None:
    result = _drill(tmp_path, DUMP_DATA, RESTORED)

    assert result.returncode == 0, result.stderr
    assert "restore check passed for crm:" in result.stdout
    assert "(3 tables, 5 rows, as in the dump)" in result.stdout
    calls = (tmp_path / "calls.log").read_text()
    assert "dropdb --port 5432 crm_restore_check" in calls


def test_a_restore_that_brought_back_nothing_fails(tmp_path: Path) -> None:
    """pg_class has rows in an empty database: only the dump's tables count."""
    result = _drill(tmp_path, DUMP_DATA, "")

    assert result.returncode != 0
    assert "restore check failed for crm" in result.stderr
    assert 'sales."Order Lines"\t3' in result.stderr
    assert "restore check passed" not in result.stdout


def test_a_restored_table_with_other_rows_than_the_dump_fails(
    tmp_path: Path,
) -> None:
    restored = RESTORED.replace("public.customers\t2", "public.customers\t1")

    result = _drill(tmp_path, DUMP_DATA, restored)

    assert result.returncode != 0
    assert "public.customers\t2" in result.stderr
    assert "restore check passed" not in result.stdout


def test_a_parent_table_is_counted_without_its_inheritance_children(
    tmp_path: Path,
) -> None:
    """pg_dump writes a parent's own rows; each child has its own block."""
    dump_data = """\
COPY public.events (id) FROM stdin;
1
2
\\.

COPY public.events_2026 (id) FROM stdin;
3
4
5
\\.
"""
    restored = "public.events\t2\npublic.events_2026\t3\n"
    with_children = "public.events\t5\npublic.events_2026\t3\n"

    result = _drill(
        tmp_path, dump_data, restored, restored_with_children=with_children
    )

    assert result.returncode == 0, result.stderr
    assert "(2 tables, 5 rows, as in the dump)" in result.stdout


def test_a_dump_without_tables_fails(tmp_path: Path) -> None:
    """A dump that holds no table proves nothing restorable."""
    empty = "SELECT pg_catalog.set_config('search_path', '', false);\n"

    result = _drill(tmp_path, empty, "")

    assert result.returncode != 0
    assert "holds no table" in result.stderr
    assert "restore check passed" not in result.stdout


def _scratch_assert_holds(*names: str) -> bool:
    """Evaluate the role's scratch-name assert as Ansible would."""
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "main.yml"))
    assert isinstance(tasks, list)
    task = next(
        task
        for task in tasks
        if task["name"] == "Refuse a database named as a restore-drill scratch database"
    )
    variables: dict[str, object] = {
        "cloudfall_postgresql_service": {
            "id": "postgresql-main",
            "postgresql": {"databases": [{"name": name} for name in names]},
        },
    }
    templar = Templar(loader=DataLoader(), variables=variables)
    for name, value in task["vars"].items():
        variables[name] = templar.template(trust_as_template(str(value)))
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(
        templar.evaluate_conditional(trust_as_template(str(condition)))
        for condition in task["ansible.builtin.assert"]["that"]
    )


def test_the_role_refuses_a_database_named_as_a_scratch_database() -> None:
    assert _scratch_assert_holds("crm", "billing")
    assert _scratch_assert_holds("a" * 63)
    assert not _scratch_assert_holds("crm", "crm_restore_check")
    assert not _scratch_assert_holds("a" * 63, "a" * 49 + "_restore_check")


def test_the_scratch_name_stays_distinct_within_the_identifier_limit() -> None:
    """PostgreSQL cuts names at 63 bytes; a cut scratch name is its database."""
    longest = "a" * 63

    rendered = _render(Path("/var/backups"), "crm", longest)

    assert 'scratch="crm_restore_check"' in rendered
    assert f'scratch="{"a" * 49}_restore_check"' in rendered
    assert len("a" * 49 + "_restore_check") == 63


def test_a_lone_database_of_the_longest_name_drills_into_a_63_byte_scratch(
    tmp_path: Path,
) -> None:
    """The schema allows 63 bytes, the most PostgreSQL keeps of a name."""
    longest = "a" * 63
    scratch = "a" * 49 + "_restore_check"

    result = _drill(tmp_path, DUMP_DATA, RESTORED, longest)

    assert result.returncode == 0, result.stderr
    assert f"restore check passed for {longest}:" in result.stdout
    calls = (tmp_path / "calls.log").read_text()
    assert f"createdb --port 5432 {scratch}\n" in calls
    assert f"--dbname {scratch} " in calls
    assert f"dropdb --port 5432 {scratch}\n" in calls
    assert len(scratch.encode()) == 63


def test_a_name_is_cut_only_past_49_bytes() -> None:
    """At 49 bytes the scratch name is exactly 63; one byte more is cut."""
    rendered = _render(Path("/var/backups"), "b" * 49, "c" * 50)

    assert f'scratch="{"b" * 49}_restore_check"' in rendered
    assert f'scratch="{"c" * 49}_restore_check"' in rendered
    assert f'scratch="{"c" * 50}_restore_check"' not in rendered
