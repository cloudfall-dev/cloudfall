"""The services converge answers a Kibana held back by an older node (S-024-9, #579).

``services.yml`` needs Debian 13 hosts, so the converge runs with a recording
step runner that reports, as the Kibana role does, the refusal of a cluster
whose node is older than the declared version. The role's own check is tested
in ``engine/tests/test_s024_9_kibana_version_change.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cloudfall.lifecycle import (
    EngineContext,
    ExecutionStep,
    LifecycleError,
    converge_services,
)
from cloudfall.mariadb_replication import REPORT_DIRECTORY_VARIABLE
from cloudfall.secrets import SopsSecretProvider
from elasticsearch_hosts import FakeNodes
from kibana_project import SCHEMAS, kibana_service, search_main, write_project

ROOT = Path(__file__).parents[2]
SEARCH_SECRETS = (
    "CLOUDFALL_PASSWORD=synthetic-cloudfall-value\n"
    "ELASTIC_PASSWORD=synthetic-elastic-value\n"
    "ANALYST_PASSWORD=synthetic-analyst-value\n"
)
KIBANA_SECRETS = (
    "SECURITY_ENCRYPTION_KEY=synthetic-security-key-0123456789abcdef\n"
    "SAVED_OBJECTS_ENCRYPTION_KEY=synthetic-saved-objects-key-0123456789\n"
    "REPORTING_ENCRYPTION_KEY=synthetic-reporting-key-0123456789abcd\n"
)
MESSAGE = (
    "Kibana service search-kibana on h1 is left at 9.2.4: these nodes of "
    "Elasticsearch service search-main run older than the declared 9.2.5: "
    "h2 9.2.4. Kibana never runs ahead of a node; converge every node to that "
    "version, then run the command again"
)


@dataclass(frozen=True)
class Finished:
    """A step's exit and output."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Recorder:
    """Answer the provider playbooks; report ``refusal`` from services.yml."""

    refusal: dict[str, str] | None = None
    nodes: FakeNodes = field(default_factory=FakeNodes)
    services: dict[str, object] = field(default_factory=dict)

    def __call__(self, step: ExecutionStep) -> Finished:
        """Answer a provider playbook, or fail services.yml with the refusal."""
        if "--extra-vars" not in step.argv:
            return Finished()
        extra = json.loads(step.argv[step.argv.index("--extra-vars") + 1])
        self.nodes.answer(Path(step.argv[-1]).name, extra)
        if not step.argv[-1].endswith("services.yml"):
            return Finished()
        self.services = extra
        if self.refusal is None:
            return Finished()
        reports = Path(extra[REPORT_DIRECTORY_VARIABLE])
        (reports / f"{self.refusal['service']}.json").write_text(
            json.dumps(self.refusal), encoding="utf-8"
        )
        return Finished(returncode=2)


def _converge(tmp_path: Path, recorder: Recorder) -> None:
    project = write_project(
        tmp_path / "project", search_main(version="9.2.5"), kibana_service()
    )
    services = project / "secrets" / "production" / "services"
    services.mkdir(parents=True)
    (services / "search-main.env").write_text(SEARCH_SECRETS, encoding="utf-8")
    (services / "search-kibana.env").write_text(KIBANA_SECRETS, encoding="utf-8")
    converge_services(
        EngineContext(
            project_directory=project,
            schema_directory=SCHEMAS,
            engine_directory=ROOT / "engine",
            inventory_file=project / "tmp" / "ansible-inventory.json",
            run=recorder,
        ),
        SopsSecretProvider(
            secrets_directory=project / "secrets",
            decrypt=lambda path: path.read_text(encoding="utf-8"),
        ),
    )


# proves: S-024-9
def test_s024_9_a_node_older_than_the_declared_version_answers_precondition(
    tmp_path: Path,
) -> None:
    recorder = Recorder(
        refusal={
            "service": "search-kibana",
            "server": "h1",
            "outcome": "refused",
            "code": "service_kibana_node_older",
            "message": MESSAGE,
        }
    )

    with pytest.raises(LifecycleError) as error:
        _converge(tmp_path, recorder)

    # Not the failed playbook: the role's refusal, which the converge command
    # answers with PRECONDITION
    assert error.value.code == "service_kibana_node_older"
    assert error.value.detail == MESSAGE
    assert error.value.context["service"] == "search-kibana"
    assert error.value.context["server"] == "h1"


# proves: S-024-9
def test_s024_9_a_project_with_kibana_hands_the_role_its_report_directory(
    tmp_path: Path,
) -> None:
    recorder = Recorder()

    _converge(tmp_path, recorder)

    assert REPORT_DIRECTORY_VARIABLE in recorder.services
