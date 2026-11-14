"""S-016: the bundled Grafana dashboards and their provisioning."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROLE = Path(__file__).parents[1] / "ansible" / "roles" / "cloudfall_logging_backend"
DASHBOARDS = ROLE / "files" / "dashboards"
UIDS = {"cloudfall-fleet", "cloudfall-server", "cloudfall-logs", "cloudfall-services"}
DATASOURCES = {"cloudfall-loki", "cloudfall-prometheus"}
SCHEMA_VERSION = 42
INSTALL = "Install the bundled dashboards"
PRUNE = "Remove dashboards this release does not bundle"
PROVISION = "Provision the bundled dashboards"
REFUSE_PATH = (
    "Refuse a dashboards path that is not a dedicated Cloudfall dashboards directory"
)

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


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict), value
    return value


def _mappings(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list), value
    return [_mapping(item) for item in value]


def _text(value: object) -> str:
    assert isinstance(value, str), value
    return value


def _texts(value: object) -> list[str]:
    assert isinstance(value, list), value
    return [_text(item) for item in value]


def _integer(value: object) -> int:
    assert isinstance(value, int), value
    return value


def _dashboards() -> dict[str, dict[str, object]]:
    return {
        path.stem: _mapping(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(DASHBOARDS.glob("*.json"))
    }


def _panels(dashboard: dict[str, object]) -> list[dict[str, object]]:
    return _mappings(dashboard["panels"])


def _variables(dashboard: dict[str, object]) -> list[dict[str, object]]:
    return _mappings(_mapping(dashboard["templating"])["list"])


def _datasource_uid(holder: dict[str, object]) -> object:
    return _mapping(holder["datasource"])["uid"]


def _expressions(dashboard: dict[str, object]) -> list[str]:
    found = [
        _text(target["expr"])
        for panel in _panels(dashboard)
        for target in _mappings(panel["targets"])
    ]
    found.extend(
        _text(variable["definition"])
        for variable in _variables(dashboard)
        if variable["type"] == "query"
    )
    return found


# The offline half of S-016-1: the role ships exactly the four dashboards and their uids
def test_s016_1_the_role_bundles_exactly_the_four_dashboards() -> None:
    dashboards = _dashboards()

    assert set(dashboards) == UIDS
    for stem, dashboard in dashboards.items():
        assert dashboard["uid"] == stem
        assert dashboard["schemaVersion"] == SCHEMA_VERSION
        assert "cloudfall" in _texts(dashboard["tags"])
        assert dashboard["editable"] is False


@pytest.mark.parametrize("uid", sorted(UIDS))
def test_s016_3_every_dashboard_has_an_environment_variable(uid: str) -> None:
    names = [variable["name"] for variable in _variables(_dashboards()[uid])]

    assert names[0] == "environment"


@pytest.mark.parametrize("uid", sorted(UIDS))
def test_s016_3_every_panel_names_a_provisioned_data_source_by_uid(uid: str) -> None:
    dashboard = _dashboards()[uid]

    for panel in _panels(dashboard):
        assert _datasource_uid(panel) in DATASOURCES, panel["title"]
        for target in _mappings(panel["targets"]):
            assert _datasource_uid(target) in DATASOURCES, panel["title"]
    for variable in _variables(dashboard):
        if variable["type"] == "query":
            assert _datasource_uid(variable) in DATASOURCES, variable["name"]


@pytest.mark.parametrize("uid", sorted(UIDS))
def test_s016_3_every_query_uses_only_labels_the_collector_writes(uid: str) -> None:
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
def test_s016_3_the_label_check_catches_a_label_outside_the_list(
    expression: str, label: str
) -> None:
    assert label in labels_of(expression)
    assert not labels_of(expression) <= ALLOWED_LABELS


def test_panel_ids_and_positions_are_unique_per_dashboard() -> None:
    for uid, dashboard in _dashboards().items():
        ids = [panel["id"] for panel in _panels(dashboard)]
        assert len(ids) == len(set(ids)), uid
        for panel in _panels(dashboard):
            position = _mapping(panel["gridPos"])
            width = _integer(position["x"]) + _integer(position["w"])
            assert width <= 24, (uid, panel["title"])


def _defaults() -> dict[str, object]:
    return _mapping(
        yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))
    )


def _render_provider() -> dict[str, object]:
    defaults = _defaults()
    environment = Environment(
        loader=FileSystemLoader(ROLE / "templates"),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
    )
    rendered = environment.get_template("dashboards-provider.yml.j2").render(**defaults)
    return _mapping(yaml.safe_load(rendered))


# The offline half of S-016-4: Grafana refuses saves to a provisioned dashboard only
# when its provider disallows UI updates
def test_s016_4_the_provider_keeps_bundled_dashboards_as_shipped() -> None:
    defaults = _defaults()
    (provider,) = _mappings(_render_provider()["providers"])

    assert provider["folderUid"] == "cloudfall"
    assert provider["folder"] == "Cloudfall"
    assert provider["allowUiUpdates"] is False
    assert provider["disableDeletion"] is False
    path = defaults["cloudfall_logging_backend_dashboards_path"]
    assert _mapping(provider["options"])["path"] == path


def _main_tasks() -> list[dict[str, object]]:
    return _mappings(
        yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    )


# proves: S-016-1, S-016-7
def test_s016_7_the_role_installs_prunes_and_provisions_the_dashboards() -> None:
    by_name = {_text(task["name"]): task for task in _main_tasks()}

    install = by_name[INSTALL]
    assert "files/dashboards/*.json" in _text(install["loop"])
    prune = by_name[PRUNE]
    assert _mapping(prune["ansible.builtin.file"])["state"] == "absent"
    provision = by_name[PROVISION]
    assert _mapping(provision["ansible.builtin.template"])["dest"] == (
        "/etc/grafana/provisioning/dashboards/cloudfall.yml"
    )
    names = list(by_name)
    assert names.index(REFUSE_PATH) < names.index(INSTALL) < names.index(PRUNE)


def _dashboards_path_accepted(path: str) -> bool:
    task = next(t for t in _main_tasks() if t["name"] == REFUSE_PATH)
    conditions = _texts(_mapping(task["ansible.builtin.assert"])["that"])
    templar = Templar(
        loader=DataLoader(),
        variables={"cloudfall_logging_backend_dashboards_path": path},
    )
    return all(
        bool(templar.evaluate_conditional(trust_as_template(condition)))
        for condition in conditions
    )


def test_the_default_dashboards_path_is_accepted() -> None:
    path = _text(_defaults()["cloudfall_logging_backend_dashboards_path"])

    assert _dashboards_path_accepted(path)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/etc",
        "/etc/grafana",
        "/etc/grafana/provisioning",
        "/etc/cloudfall",
        "/etc/grafana/dashboards",
        "etc/grafana/dashboards/cloudfall",
        "/etc/grafana/dashboards/cloudfall/",
        "/etc/grafana/../grafana/dashboards/cloudfall",
        "/etc/grafana/dashboards/cloudfall/../..",
        "/etc/grafana/./dashboards/cloudfall",
    ],
)
def test_the_prune_refuses_a_path_beyond_a_dedicated_dashboards_directory(
    path: str,
) -> None:
    assert not _dashboards_path_accepted(path)
