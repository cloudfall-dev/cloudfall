"""Kibana published through an existing Domain (S-024-8, #573).

The spec's project, with ``domain: search-kibana-site``, is rendered through
the engine inventory; what ``services.yml`` hands the Kibana role, the role's
refusal and its ``kibana.yml``, and the Domain's nginx site are evaluated from
that server's host variables with Ansible's own templating. The certificate
the site names is the one the existing Domain apply path issues; a browser's
login through it is left for a live proving run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory
from kibana_project import SCHEMAS, kibana_domain, kibana_service, search_main
from kibana_project import write_project as write_kibana_project

if TYPE_CHECKING:
    from collections.abc import Iterable

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_kibana"
SITE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_nginx_site"
SERVICES = ROOT / "engine" / "ansible" / "playbooks" / "services.yml"
REFUSAL = "Refuse a Domain this server does not hold for Kibana"


def _trusted(value: object) -> object:
    """Trust the role's own files as templates, as Ansible trusts them."""
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return {key: _trusted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_trusted(item) for item in value]
    return value


def _template(expression: str, variables: dict[str, object]) -> object:
    templar = Templar(loader=DataLoader(), variables=variables)
    return templar.template(trust_as_template(expression))


def _host(tmp_path: Path, *, domain: str | None) -> dict[str, Any]:
    """Return the host variables the engine inventory hands h1."""
    project = write_kibana_project(
        tmp_path / "project",
        search_main(),
        kibana_service(domain=domain),
        domains=(kibana_domain(),),
    )
    inventory = render_ansible_inventory(
        PlatformInventory.from_state(validate_config(project, SCHEMAS))
    )
    children = cast("dict[str, Any]", inventory["all"])["children"]
    return cast("dict[str, Any]", children["cloudfall_servers"]["hosts"]["h1"])


def _role_variables(host: dict[str, Any]) -> dict[str, object]:
    """Return the variables the Kibana role sees, as services.yml hands them."""
    play = yaml.safe_load(SERVICES.read_text(encoding="utf-8"))[1]
    (block,) = play["tasks"]
    _, deploy = block["block"]
    (kibana,) = (
        service for service in host["cloudfall_services"] if "kibana" in service
    )
    scope = {**host, "cloudfall_server_id": "h1", "cloudfall_service_item": kibana}
    handed = {
        name: _template(value, scope)
        for name, value in deploy["vars"].items()
        if name in {"cloudfall_kibana_service", "cloudfall_kibana_domain"}
    }
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text("utf-8"))
    return {
        **{key: _trusted(value) for key, value in defaults.items()},
        **scope,
        **handed,
        "cloudfall_kibana_elasticsearch_service": next(
            service
            for service in host["cloudfall_services"]
            if service["id"] == "search-main"
        ),
    }


def _kibana_yml(variables: dict[str, object]) -> dict[str, object]:
    text = (ROLE / "templates" / "kibana.yml.j2").read_text(encoding="utf-8")
    settings = yaml.safe_load(str(_template(text, variables)))
    assert isinstance(settings, dict)
    return cast("dict[str, object]", settings)


def _task(name: str, tasks: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return next(task for task in tasks if task["name"] == name)


def _refusal_holds(variables: dict[str, object]) -> bool:
    tasks = DataLoader().load_from_file(str(ROLE / "tasks" / "main.yml"))
    that = _task(REFUSAL, tasks)["ansible.builtin.assert"]["that"]
    templar = Templar(loader=DataLoader(), variables=variables)
    return all(
        templar.evaluate_conditional(trust_as_template(str(condition)))
        for condition in that
    )


# proves: S-024-8
def test_s024_8_kibana_links_to_its_domain_over_https_with_secure_cookies(
    tmp_path: Path,
) -> None:
    variables = _role_variables(_host(tmp_path, domain="search-kibana-site"))

    # services.yml hands the role the Domain from the server's own Domains
    domain = cast("dict[str, Any]", variables["cloudfall_kibana_domain"])
    assert domain["id"] == "search-kibana-site"
    assert _refusal_holds(variables)
    settings = _kibana_yml(variables)
    # Links start with the Domain's address, with no trailing slash
    assert settings["server.publicBaseUrl"] == "https://kibana.example.test"
    assert settings["xpack.security.secureCookies"] is True
    # Kibana still answers on loopback only, behind the Domain's site
    assert settings["server.host"] == "127.0.0.1"
    assert settings["server.port"] == 5601
    # The role's own calls still go to the loopback listener
    assert _template("{{ cloudfall_kibana_url }}", variables) == (
        "http://127.0.0.1:5601"
    )


# proves: S-024-8
def test_s024_8_an_undomained_kibana_keeps_its_loopback_settings(
    tmp_path: Path,
) -> None:
    variables = _role_variables(_host(tmp_path, domain=None))

    assert variables["cloudfall_kibana_domain"] == {}
    assert _refusal_holds(variables)
    settings = _kibana_yml(variables)
    assert settings["server.publicBaseUrl"] == "http://127.0.0.1:5601"
    assert "xpack.security.secureCookies" not in settings


# proves: S-024-8
@pytest.mark.parametrize(
    "domain",
    [
        pytest.param({}, id="not a Domain of this server"),
        pytest.param({"id": "other-site"}, id="another Domain"),
    ],
)
def test_s024_8_a_domain_the_server_does_not_hold_is_refused(
    tmp_path: Path, domain: dict[str, object]
) -> None:
    variables = _role_variables(_host(tmp_path, domain="search-kibana-site"))
    held = cast("dict[str, Any]", variables["cloudfall_kibana_domain"])

    assert not _refusal_holds({**variables, "cloudfall_kibana_domain": domain})
    # A Domain whose site is not the loopback listener's over TLS, too
    for changed in (
        {**held, "tls": {"mode": "optional"}},
        {**held, "proxy": {**held["proxy"], "server": "h2"}},
        {**held, "proxy": {**held["proxy"], "upstream": {"port": 5602}}},
        {**held, "routes": [{"path": "/"}]},
    ):
        assert not _refusal_holds({**variables, "cloudfall_kibana_domain": changed})


# proves: S-024-8
def test_s024_8_the_domains_site_fronts_kibana_with_the_certificate_of_its_name(
    tmp_path: Path,
) -> None:
    host = _host(tmp_path, domain="search-kibana-site")
    (domain,) = host["cloudfall_domains"]
    defaults = yaml.safe_load((SITE / "defaults" / "main.yml").read_text("utf-8"))
    text = (SITE / "templates" / "domain-site.conf.j2").read_text(encoding="utf-8")

    site = str(
        _template(
            text,
            {
                **{key: _trusted(value) for key, value in defaults.items()},
                "cloudfall_nginx_site_domain": domain,
                "cloudfall_nginx_site_tls_active": True,
                "cloudfall_nginx_site_version": "1.26.3",
            },
        )
    )

    plain, secure = re.split(r"\n(?=server \{)", site.split("\n", 1)[1].strip())
    # Plain HTTP only redirects to HTTPS: the login form is never sent over it
    assert "return 301 https://$host$request_uri;" in plain
    assert "proxy_pass" not in plain
    assert "server_name kibana.example.test;" in secure
    assert (
        "ssl_certificate /etc/letsencrypt/live/kibana.example.test/fullchain.pem;"
        in secure
    )
    # To Kibana's loopback listener, telling it the request came over HTTPS
    assert "proxy_pass http://127.0.0.1:5601;" in secure
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in secure
    assert "proxy_set_header Host $host;" in secure
