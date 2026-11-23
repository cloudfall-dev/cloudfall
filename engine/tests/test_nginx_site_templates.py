"""Managed virtual-host template tests."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml
from ansible.plugins.filter.core import FilterModule as AnsibleFilters

# Aliased so pytest does not collect Ansible's plugin class as a test class.
from ansible.plugins.test.core import TestModule as AnsibleTests
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parents[2]
TEMPLATES = (
    ROOT / "engine" / "ansible" / "roles" / "cloudfall_nginx_site" / "templates"
)
DEBIAN_12_NGINX = "1.22.1"
FIRST_HTTP2_DIRECTIVE_NGINX = "1.25.1"


def _domain() -> dict[str, object]:
    return {
        "id": "crm-site",
        "primaryName": "crm.example.test",
        "aliases": ["www.crm.example.test"],
        "proxy": {
            "server": "h2",
            "configurationPath": (
                "/etc/nginx/sites-available/crm.example.test.conf"
            ),
            "service": "nginx.service",
            "upstream": {"address": "127.0.0.1", "port": 8100},
        },
        "tls": {"mode": "required"},
    }


def _render(
    *,
    tls_active: bool,
    nginx_version: str = "1.26.3",
    http2: object = True,
    domain: dict[str, object] | None = None,
) -> str:
    # trim_blocks matches the template module's default.
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    # Ansible's own tests, so `is version(...)` compares as on the host.
    environment.tests.update(AnsibleTests().tests())
    environment.filters.update(AnsibleFilters().filters())
    return environment.get_template("domain-site.conf.j2").render(
        cloudfall_nginx_site_domain=_domain() if domain is None else domain,
        cloudfall_nginx_site_acme_webroot="/var/lib/letsencrypt",
        cloudfall_nginx_site_tls_active=tls_active,
        cloudfall_nginx_site_version=nginx_version,
        cloudfall_nginx_site_http2=http2,
    )


def test_site_discards_client_forwarding_headers() -> None:
    rendered = _render(tls_active=True)

    assert "proxy_set_header X-Forwarded-For $remote_addr;" in rendered
    assert "proxy_add_x_forwarded_for" not in rendered
    assert "proxy_set_header X-Real-IP $remote_addr;" in rendered
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in rendered


def test_tls_site_redirects_http_and_terminates_https() -> None:
    rendered = _render(tls_active=True)

    assert "server_name crm.example.test www.crm.example.test;" in rendered
    assert "return 301 https://$host$request_uri;" in rendered
    assert "listen 443 ssl;" in rendered
    assert (
        "ssl_certificate /etc/letsencrypt/live/crm.example.test/fullchain.pem;"
        in rendered
    )
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in rendered
    assert "proxy_pass http://127.0.0.1:8100;" in rendered
    assert "location /.well-known/acme-challenge/" in rendered


def test_bootstrap_site_serves_http_until_a_certificate_exists() -> None:
    rendered = _render(tls_active=False)

    assert "listen 443" not in rendered
    assert "return 301" not in rendered
    assert "proxy_pass http://127.0.0.1:8100;" in rendered
    assert "location /.well-known/acme-challenge/" in rendered


@pytest.mark.parametrize("nginx_version", [DEBIAN_12_NGINX, "1.24.0", "1.25.0"])
def test_nginx_before_1_25_1_gets_http2_on_the_listen_directive(
    nginx_version: str,
) -> None:
    """`http2 on;` is an unknown directive before 1.25.1 and fails `nginx -t`."""
    rendered = _render(tls_active=True, nginx_version=nginx_version)

    assert "    listen 443 ssl http2;\n" in rendered
    assert "    listen [::]:443 ssl http2;\n" in rendered
    assert "http2 on;" not in rendered


@pytest.mark.parametrize(
    "nginx_version", [FIRST_HTTP2_DIRECTIVE_NGINX, "1.26.3", "1.27.10"]
)
def test_nginx_from_1_25_1_gets_the_http2_directive(nginx_version: str) -> None:
    rendered = _render(tls_active=True, nginx_version=nginx_version)

    assert (
        "server {\n"
        "    listen 443 ssl;\n"
        "    listen [::]:443 ssl;\n"
        "    http2 on;\n"
        "    server_name crm.example.test www.crm.example.test;\n"
    ) in rendered
    assert "ssl http2;" not in rendered


@pytest.mark.parametrize("http2", [False, "false"])
@pytest.mark.parametrize("nginx_version", [DEBIAN_12_NGINX, "1.26.3"])
def test_http2_off_renders_no_http2_on_either_side_of_1_25_1(
    nginx_version: str, http2: object
) -> None:
    """`"false"` is what a key=value `--extra-vars` hands the role (#551)."""
    rendered = _render(tls_active=True, nginx_version=nginx_version, http2=http2)

    assert (
        "server {\n"
        "    listen 443 ssl;\n"
        "    listen [::]:443 ssl;\n"
        "    server_name crm.example.test www.crm.example.test;\n"
    ) in rendered
    assert "http2" not in rendered


def _body_limited_domain(megabytes: int) -> dict[str, object]:
    domain = _domain()
    proxy = dict(cast("dict[str, object]", domain["proxy"]))
    proxy["maxRequestBodyMegabytes"] = megabytes
    return domain | {"proxy": proxy}


@pytest.mark.parametrize("nginx_version", [DEBIAN_12_NGINX, "1.26.3"])
def test_a_body_limit_renders_in_both_server_blocks_with_tls(
    nginx_version: str,
) -> None:
    """The port 80 block only redirects then, but every block carries it (#600)."""
    rendered = _render(
        tls_active=True,
        nginx_version=nginx_version,
        domain=_body_limited_domain(55),
    )
    plain, tls = rendered.split("\nserver {\n")[1:]

    for block in (plain, tls):
        assert (
            "    server_name crm.example.test www.crm.example.test;\n"
            "    client_max_body_size 55m;\n"
            "\n"
        ) in block
    assert rendered.count("client_max_body_size") == 2


@pytest.mark.parametrize("nginx_version", [DEBIAN_12_NGINX, "1.26.3"])
def test_a_body_limit_renders_in_the_one_block_without_tls(
    nginx_version: str,
) -> None:
    rendered = _render(
        tls_active=False,
        nginx_version=nginx_version,
        domain=_body_limited_domain(55),
    )

    assert (
        "    server_name crm.example.test www.crm.example.test;\n"
        "    client_max_body_size 55m;\n"
        "\n"
        "    location /.well-known/acme-challenge/ {\n"
    ) in rendered
    assert rendered.count("client_max_body_size") == 1
    assert rendered == _render(
        tls_active=False, nginx_version=nginx_version
    ).replace(
        "www.crm.example.test;\n",
        "www.crm.example.test;\n    client_max_body_size 55m;\n",
    )


@pytest.mark.parametrize("tls_active", [True, False])
def test_no_body_limit_renders_no_directive(*, tls_active: bool) -> None:
    assert "client_max_body_size" not in _render(tls_active=tls_active)


def test_http2_defaults_on_in_the_role_and_its_argument_spec() -> None:
    """On again once Debian shipped the HTTP/2 fix for both targets (#555)."""
    role = TEMPLATES.parent
    defaults = yaml.safe_load((role / "defaults" / "main.yml").read_text("utf-8"))
    specs = yaml.safe_load((role / "meta" / "argument_specs.yml").read_text("utf-8"))
    option = specs["argument_specs"]["main"]["options"]["cloudfall_nginx_site_http2"]

    assert defaults["cloudfall_nginx_site_http2"] is True
    assert option == {"type": "bool", "default": True}
