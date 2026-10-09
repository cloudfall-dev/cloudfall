"""Managed virtual-host template tests."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def _render(*, tls_active: bool, nginx_version: str = "1.26.3") -> str:
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
    return environment.get_template("domain-site.conf.j2").render(
        cloudfall_nginx_site_domain=_domain(),
        cloudfall_nginx_site_acme_webroot="/var/lib/letsencrypt",
        cloudfall_nginx_site_tls_active=tls_active,
        cloudfall_nginx_site_version=nginx_version,
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
