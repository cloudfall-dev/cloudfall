"""Component service-unit template tests."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).parents[2]
TEMPLATES = (
    ROOT / "engine" / "ansible" / "roles" / "cloudfall_deploy" / "templates"
)


def _component() -> dict[str, object]:
    return {
        "id": "crm-backend",
        "application": "crm",
        "application_user": "crm",
        "install_root": "/srv/apps/crm/backend",
        "service": {
            "manager": "systemd",
            "name": "crm-backend",
            "command": [
                ".venv/bin/uvicorn",
                "crm.asgi:application",
                "--host",
                "127.0.0.1",
                "--port",
                "8100",
            ],
        },
    }


def _render() -> str:
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
    )
    return environment.get_template("component.service.j2").render(
        cloudfall_deploy_component=_component(),
        cloudfall_deploy_environment_path=(
            "/srv/apps/crm/backend/shared/env/crm-backend.20261028T000000Z-abcdef2.env"
        ),
    )


def test_unit_runs_the_release_command_as_the_application_user() -> None:
    rendered = _render()

    assert "User=crm" in rendered
    assert "WorkingDirectory=/srv/apps/crm/backend/current" in rendered
    assert (
        "ExecStart=/srv/apps/crm/backend/current/.venv/bin/uvicorn"
        ' "crm.asgi:application" "--host" "127.0.0.1" "--port" "8100"'
        in rendered
    )
    # The release's own environment file (D-21), as the deploy computes it.
    assert (
        "EnvironmentFile=-/srv/apps/crm/backend/shared/env/"
        "crm-backend.20261028T000000Z-abcdef2.env"
    ) in rendered
    assert "Restart=on-failure" in rendered
    assert "NoNewPrivileges=true" in rendered
    assert "WantedBy=multi-user.target" in rendered
