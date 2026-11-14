"""S-016: the bundled Grafana dashboards and their provisioning."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROLE = Path(__file__).parents[1] / "ansible" / "roles" / "cloudfall_logging_backend"
DASHBOARDS = ROLE / "files" / "dashboards"
UIDS = {"cloudfall-fleet", "cloudfall-server", "cloudfall-logs", "cloudfall-services"}
DATASOURCES = {"cloudfall-loki", "cloudfall-prometheus"}
SCHEMA_VERSION = 42

# Labels the collector writes (config.alloy.j2) and Prometheus adds when scraping
COLLECTOR_LABELS = {
    "environment",
    "server",
    "unit",
    "source",
    "job",
    "application",
    "component",
    "service",
    "instance",
}
# Labels of the exporters' and Prometheus' own series the dashboards read
EXPORTER_LABELS = {
    "__name__",
    "mode",
    "mountpoint",
    "fstype",
    "device",
    "state",
    "alertstate",
}
ALLOWED_LABELS = COLLECTOR_LABELS | EXPORTER_LABELS

_SELECTOR = re.compile(r"\{([^{}]*)\}")
_MATCHER = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=~|!~|!=|=)\s*\"")
_GROUPING = re.compile(r"\b(?:by|without)\s*\(([^)]*)\)")
_LABEL_VALUES = re.compile(
    r"label_values\((?:[^,()]*(?:\{[^{}]*\})?\s*,)?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)"
)


def labels_of(expression: str) -> set[str]:
    """Return every label name a PromQL or LogQL expression matches or groups by."""
    # Legend templates such as {{server}} are not selectors
    expression = re.sub(r"\{\{[^}]*\}\}", "", expression)
    found: set[str] = set()
    for selector in _SELECTOR.findall(expression):
        found.update(_MATCHER.findall(selector))
    for grouping in _GROUPING.findall(expression):
        found.update(name.strip() for name in grouping.split(",") if name.strip())
    found.update(_LABEL_VALUES.findall(expression))
    return found


def _dashboards() -> dict[str, dict[str, object]]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(DASHBOARDS.glob("*.json"))
    }


def _panels(dashboard: dict[str, object]) -> list[dict[str, object]]:
    panels = dashboard["panels"]
    assert isinstance(panels, list)
    return panels


def _variables(dashboard: dict[str, object]) -> list[dict[str, object]]:
    templating = dashboard["templating"]
    assert isinstance(templating, dict)
    return templating["list"]


def _expressions(dashboard: dict[str, object]) -> list[str]:
    found = [
        target["expr"] for panel in _panels(dashboard) for target in panel["targets"]
    ]
    found.extend(
        variable["definition"]
        for variable in _variables(dashboard)
        if variable["type"] == "query"
    )
    return found


def test_the_role_bundles_exactly_the_four_dashboards() -> None:
    dashboards = _dashboards()

    assert set(dashboards) == UIDS
    for stem, dashboard in dashboards.items():
        assert dashboard["uid"] == stem
        assert dashboard["schemaVersion"] == SCHEMA_VERSION
        assert "cloudfall" in dashboard["tags"]
        assert dashboard["editable"] is False


@pytest.mark.parametrize("uid", sorted(UIDS))
def test_every_dashboard_has_an_environment_variable(uid: str) -> None:
    names = [variable["name"] for variable in _variables(_dashboards()[uid])]

    assert names[0] == "environment"


@pytest.mark.parametrize("uid", sorted(UIDS))
def test_every_panel_names_a_provisioned_data_source_by_uid(uid: str) -> None:
    dashboard = _dashboards()[uid]

    for panel in _panels(dashboard):
        assert panel["datasource"]["uid"] in DATASOURCES, panel["title"]
        for target in panel["targets"]:
            assert target["datasource"]["uid"] in DATASOURCES, panel["title"]
    for variable in _variables(dashboard):
        if variable["type"] == "query":
            assert variable["datasource"]["uid"] in DATASOURCES, variable["name"]


@pytest.mark.parametrize("uid", sorted(UIDS))
def test_every_query_uses_only_labels_the_collector_writes(uid: str) -> None:
    for expression in _expressions(_dashboards()[uid]):
        assert labels_of(expression) <= ALLOWED_LABELS, expression


@pytest.mark.parametrize(
    ("expression", "label"),
    [
        ('node_load1{hostname="h1"}', "hostname"),
        ("sum by (pod) (rate(x[5m]))", "pod"),
        ('{environment="prod", app=~"web"} |~ "error"', "app"),
        ("label_values(node_uname_info, nodename)", "nodename"),
    ],
)
def test_the_label_check_catches_a_label_outside_the_list(
    expression: str, label: str
) -> None:
    assert label in labels_of(expression)
    assert not labels_of(expression) <= ALLOWED_LABELS


def test_panel_ids_and_positions_are_unique_per_dashboard() -> None:
    for uid, dashboard in _dashboards().items():
        ids = [panel["id"] for panel in _panels(dashboard)]
        assert len(ids) == len(set(ids)), uid
        for panel in _panels(dashboard):
            position = panel["gridPos"]
            assert position["x"] + position["w"] <= 24, (uid, panel["title"])


def _defaults() -> dict[str, str]:
    return yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))


def _render_provider() -> dict[str, object]:
    defaults = _defaults()
    environment = Environment(
        loader=FileSystemLoader(ROLE / "templates"),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
    )
    rendered = environment.get_template("dashboards-provider.yml.j2").render(**defaults)
    return yaml.safe_load(rendered)


def test_the_provider_keeps_bundled_dashboards_as_shipped() -> None:
    defaults = _defaults()
    (provider,) = _render_provider()["providers"]

    assert provider["folderUid"] == "cloudfall"
    assert provider["folder"] == "Cloudfall"
    assert provider["allowUiUpdates"] is False
    assert provider["disableDeletion"] is False
    path = defaults["cloudfall_logging_backend_dashboards_path"]
    assert provider["options"]["path"] == path


def test_the_role_installs_prunes_and_provisions_the_dashboards() -> None:
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    by_name = {task["name"]: task for task in tasks}

    install = by_name["Install the bundled dashboards"]
    assert "files/dashboards/*.json" in install["loop"]
    prune = by_name["Remove dashboards this release does not bundle"]
    assert prune["ansible.builtin.file"]["state"] == "absent"
    provision = by_name["Provision the bundled dashboards"]
    assert provision["ansible.builtin.template"]["dest"] == (
        "/etc/grafana/provisioning/dashboards/cloudfall.yml"
    )
    names = list(by_name)
    assert names.index("Install the bundled dashboards") < names.index(
        "Remove dashboards this release does not bundle"
    )
