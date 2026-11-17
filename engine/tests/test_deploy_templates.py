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


def _render(component: dict[str, object] | None = None) -> str:
    """Render the unit with the whitespace rules Ansible's template module applies."""
    environment = Environment(
        loader=FileSystemLoader(TEMPLATES),
        undefined=StrictUndefined,
        autoescape=False,  # noqa: S701 - configuration files are not HTML.
        keep_trailing_newline=True,
        trim_blocks=True,
    )
    return environment.get_template("component.service.j2").render(
        cloudfall_deploy_component=_component() if component is None else component,
        cloudfall_deploy_environment_path=(
            "/srv/apps/crm/backend/shared/env/crm-backend.20261028T000000Z-abcdef2.env"
        ),
    )


# The unit main rendered before a component could declare service.stop (#497).
UNIT_WITHOUT_STOP = "".join(
    f"{line}\n"
    for line in (
        "# Managed by Cloudfall. Local changes will be overwritten.",
        "[Unit]",
        "Description=Cloudfall component crm-backend",
        "After=network.target",
        "",
        "[Service]",
        "Type=exec",
        "User=crm",
        "Group=crm",
        "WorkingDirectory=/srv/apps/crm/backend/current",
        (
            "EnvironmentFile=-/srv/apps/crm/backend/shared/env/"
            "crm-backend.20261028T000000Z-abcdef2.env"
        ),
        (
            "ExecStart=/srv/apps/crm/backend/current/.venv/bin/uvicorn"
            ' "crm.asgi:application" "--host" "127.0.0.1" "--port" "8100"'
        ),
        "Restart=on-failure",
        "RestartSec=2",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    )
)


def test_a_unit_without_a_stop_timeout_is_byte_identical_to_before() -> None:
    assert _render() == UNIT_WITHOUT_STOP


def test_a_declared_stop_timeout_renders_timeout_stop_sec_and_nothing_else() -> None:
    component = _component()
    service = component["service"]
    assert isinstance(service, dict)
    component["service"] = {**service, "stop": {"timeoutSeconds": 900}}

    assert _render(component) == UNIT_WITHOUT_STOP.replace(
        "RestartSec=2\n", "RestartSec=2\nTimeoutStopSec=900\n"
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
