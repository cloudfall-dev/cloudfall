"""Domain routes: the `routes` schema, its value objects and its validation (S-003)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from cloudfall.cli import main
from cloudfall.domain import DomainRoutePath, ReleaseRelativePath, ResourceId
from cloudfall.inventory import (
    DomainProxyRoute,
    DomainStaticRoute,
    PlatformInventory,
)
from cloudfall.validation import validate_config

ROOT = Path(__file__).parents[2]
SCHEMAS = ROOT / "config" / "schemas" / "v1"
EXAMPLES = ROOT / "config" / "examples"

_UPSTREAM = "    upstream:\n      address: 127.0.0.1\n      port: 8100\n"
_SPA_ROUTES = """\
  routes:
    - path: /api/
      upstream: { address: 127.0.0.1, port: 8310 }
    - path: /
      static:
        component: crm-backend
        directory: web/dist
        spaFallback: index.html
"""


def _project(tmp_path: Path, routes: str, *, keep_upstream: bool = False) -> Path:
    """Copy the examples and give their Domain ``routes`` in place of its upstream."""
    project = tmp_path / "config"
    shutil.copytree(EXAMPLES, project)
    path = project / "domains" / "crm-site.yaml"
    text = path.read_text(encoding="utf-8")
    assert _UPSTREAM in text
    if not keep_upstream:
        text = text.replace(_UPSTREAM, "")
    text = text.replace("  origin:\n", routes + "  origin:\n")
    path.write_text(text, encoding="utf-8")
    return project


def _validate(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any]]:
    exit_code = main(
        [
            "config",
            "validate",
            "--project",
            str(project),
            "--schemas",
            str(SCHEMAS),
        ]
    )
    return exit_code, json.loads(capsys.readouterr().out)


def _refused(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> dict[str, object]:
    exit_code, payload = _validate(project, capsys)
    assert exit_code == 80
    assert payload["error"]["code"] == "CONFIG_INVALID"
    context: dict[str, object] = payload["error"]["context"]
    return context


def _static_route(path: str, directory: str = "web/dist", extra: str = "") -> str:
    return (
        "  routes:\n"
        f"    - path: {path}\n"
        "      static:\n"
        "        component: crm-backend\n"
        f"        directory: {directory}\n"
        f"{extra}"
    )


def _proxy_route(path: str) -> str:
    return (
        "  routes:\n"
        f"    - path: {path}\n"
        "      upstream: { address: 127.0.0.1, port: 8310 }\n"
    )


def test_s003_1_proxy_and_spa_static_routes_pass_config_validate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, _SPA_ROUTES)

    exit_code, payload = _validate(project, capsys)

    assert exit_code == 0
    assert payload["data"]["status"] == "ok"
    domain =PlatformInventory.from_state(validate_config(project, SCHEMAS)).domains[0]
    assert domain.proxy.upstream_address is None
    api, web = domain.routes
    assert isinstance(api, DomainProxyRoute)
    assert api.path == DomainRoutePath("/api/")
    assert (api.upstream_address.value, api.upstream_port.value) == ("127.0.0.1", 8310)
    assert isinstance(web, DomainStaticRoute)
    assert web.component_id == ResourceId("crm-backend")
    assert web.directory == ReleaseRelativePath("web/dist")
    assert web.spa_fallback == ReleaseRelativePath("index.html")
    serialized: dict[str, Any] = domain.as_dict()
    assert "upstream" not in serialized["proxy"]
    assert serialized["routes"] == [
        {"path": "/api/", "upstream": {"address": "127.0.0.1", "port": 8310}},
        {
            "path": "/",
            "static": {
                "component": "crm-backend",
                "directory": "web/dist",
                "spaFallback": "index.html",
            },
        },
    ]


def test_s003_1_a_domain_with_only_an_upstream_serializes_as_before() -> None:
    domain = PlatformInventory.from_state(validate_config(EXAMPLES, SCHEMAS)).domains[0]

    assert domain.routes == ()
    assert "routes" not in domain.as_dict()


def test_s003_8_two_routes_for_one_path_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    routes = (
        "  routes:\n"
        "    - path: /api/\n"
        "      upstream: { address: 127.0.0.1, port: 8310 }\n"
        "    - path: /api/\n"
        "      upstream: { address: 127.0.0.1, port: 8311 }\n"
    )

    context = _refused(_project(tmp_path, routes), capsys)

    assert context["code"] == "domain_route_path_duplicate"
    assert context["field"] == "spec.routes.1.path"


def test_s003_9_a_domain_with_upstream_and_routes_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    context = _refused(_project(tmp_path, _SPA_ROUTES, keep_upstream=True), capsys)

    assert context["code"] == "domain_route_source_invalid"
    assert context["field"] == "spec.routes"


def test_s003_9_a_domain_with_neither_upstream_nor_routes_still_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, payload = _validate(_project(tmp_path, ""), capsys)

    assert exit_code == 0
    assert payload["data"]["status"] == "ok"


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/acme-challenge",
        "/.well-known/acme-challenge/",
        "/.well-known/acme-challenge/token/",
    ],
)
def test_s003_10_a_route_on_the_acme_challenge_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    context = _refused(_project(tmp_path, _proxy_route(path)), capsys)

    assert context["code"] == "domain_route_path_reserved"
    assert context["field"] == "spec.routes.0.path"


@pytest.mark.parametrize(
    "routes",
    [
        _static_route("/web"),
        _static_route("'/a b/'"),
        _static_route("'/a;b/'"),
        _static_route("'/a{b/'"),
        _proxy_route("api/"),
        _proxy_route("'/a b'"),
        _proxy_route("''"),
    ],
    ids=[
        "static-no-slash",
        "space",
        "semicolon",
        "brace",
        "relative",
        "proxy-space",
        "empty",
    ],
)
def test_s003_10_an_invalid_route_path_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], routes: str
) -> None:
    context = _refused(_project(tmp_path, routes), capsys)

    assert context["code"] == "domain_route_path_invalid"
    assert context["field"] == "spec.routes.0.path"


def test_s003_10_a_proxy_route_path_needs_no_trailing_slash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    routes = _proxy_route("/api.v1_~x-y")

    exit_code, _ = _validate(_project(tmp_path, routes), capsys)

    assert exit_code == 0


def test_s003_11_a_static_route_naming_a_missing_component_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    routes = _static_route("/").replace("crm-backend", "crm-missing")

    context = _refused(_project(tmp_path, routes), capsys)

    assert context["code"] == "resource_reference_missing"
    assert context["field"] == "spec.routes.0.static.component"
    assert "Component/crm-missing" in str(context["message"])


def test_s003_11_a_static_component_off_the_proxy_server_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path, _static_route("/"))
    component = project / "components" / "crm-backend.yaml"
    text = component.read_text(encoding="utf-8")
    component.write_text(
        text.replace("      - h1\n      - h2\n", "      - h1\n"), encoding="utf-8"
    )

    context = _refused(project, capsys)

    assert context["code"] == "domain_route_component_not_on_proxy"
    assert context["field"] == "spec.routes.0.static.component"
    assert "h2" in str(context["message"])


@pytest.mark.parametrize(
    "routes",
    [
        _static_route("/", directory="../web"),
        _static_route("/", directory="web/../../etc"),
        _static_route("/", directory="/srv/web"),
        _static_route("/", directory="."),
        _static_route("/", directory="./"),
        _static_route("/", directory="'web dist'"),
        _static_route("/", extra="        spaFallback: ../index.html\n"),
        _static_route("/", extra="        spaFallback: /index.html\n"),
        _static_route("/", extra="        spaFallback: a/../../index.html\n"),
    ],
    ids=[
        "directory-parent",
        "directory-inner-parent",
        "directory-absolute",
        "directory-root",
        "directory-root-slash",
        "directory-space",
        "fallback-parent",
        "fallback-absolute",
        "fallback-inner-parent",
    ],
)
def test_s003_12_a_static_directory_or_fallback_outside_the_release_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], routes: str
) -> None:
    context = _refused(_project(tmp_path, routes), capsys)

    assert context["code"] == "schema_validation_failed"
    assert str(context["field"]).startswith("spec.routes.0")


def test_s003_12_route_value_objects_match_the_schema() -> None:
    for path in ("/", "/api/", "/a.b_c~d-e/f"):
        assert DomainRoutePath.from_boundary(path).value == path
    for path in ("", "api/", "/a b", "/a;b", "/a{b}", "/a\nb"):
        with pytest.raises(ValueError, match="invalid route path"):
            DomainRoutePath(path)
    assert DomainRoutePath("/.well-known/acme-challenge/x").is_reserved
    assert not DomainRoutePath("/.well-known/").is_reserved
    assert not DomainRoutePath("/.well-known/acme-challenger").is_reserved
    for value in ("web/dist", "index.html", "...", "a/./b"):
        assert ReleaseRelativePath.from_boundary(value).value == value
    for value in ("", "/web", "..", "a/../b", "a b", "a;b"):
        with pytest.raises(ValueError, match="invalid release path"):
            ReleaseRelativePath(value)
    for root in (".", "./", "././", ".//."):
        assert ReleaseRelativePath(root).is_release_root
    assert not ReleaseRelativePath("web").is_release_root
