"""The ``cloudfall_rabbitmq`` role with ``clientAccess`` (S-023, #507).

A live host is what a client's TLS login needs, so the role's settings are
rendered from its templates with Ansible's own templating and its decisions
evaluated the same way. What only a live host proves (``jobs`` logging in on
5671 from an allowed address verifying the project CA, and a plain
connection to 5672 from that address refused) is listed in the pull request
as unverified.
"""

from __future__ import annotations

from typing import cast

from test_s023_rabbitmq_role import (
    FILTERS,
    JOBS,
    _holds,
    _render,
    _service,
    _settings,
    _task,
    _tasks,
    _template,
    _variables,
)

ACCESS = {"allowedSources": ["10.0.0.0/24"]}
RENDERED = {
    "rabbitmq-main": {
        "address": "203.0.113.11",
        "port": 5671,
        "certificatePath": "/etc/rabbitmq/cloudfall/node.crt",
        "keyPath": "/etc/rabbitmq/cloudfall/node.key",
        "caPath": "/etc/rabbitmq/cloudfall/ca.crt",
    }
}


def _with_access(**extra: object) -> dict[str, object]:
    return _variables(
        **{
            "cloudfall_rabbitmq_service": _service(JOBS, clientAccess=ACCESS),
            "cloudfall_rabbitmq_client_access": RENDERED,
            **extra,
        }
    )


# proves: S-023-3
def test_s023_3_without_client_access_the_node_renders_as_it_did() -> None:
    assert _render("90-cloudfall-listeners.conf.j2", _variables()).splitlines() == [
        "# Managed by Cloudfall: replaced on every converge (S-023)",
        "# Clients on loopback only, in plain text",
        "listeners.tcp.default = 127.0.0.1:5672",
        "listeners.ssl = none",
        "# One node: the inter-node port stays on loopback too",
        "distribution.listener.interface = 127.0.0.1",
        "distribution.listener.port_range.min = 25672",
        "distribution.listener.port_range.max = 25672",
    ]
    assert _template("{{ cloudfall_rabbitmq_allowed_listeners }}", _variables()) == []
    prepare = _task(
        "Prepare the node certificate for clients over TLS", _tasks("main.yml")
    )
    assert prepare["when"] == "cloudfall_rabbitmq_client_tls | bool"


# proves: S-023-19
def test_s023_19_clients_reach_the_node_over_tls_alone_on_its_ipv4() -> None:
    settings = _settings(_with_access())

    # TLS on the node's IPv4, with its certificate from the project CA
    assert settings["listeners.ssl.default"] == "203.0.113.11:5671"
    assert settings["ssl_options.cacertfile"] == "/etc/rabbitmq/cloudfall/ca.crt"
    assert settings["ssl_options.certfile"] == "/etc/rabbitmq/cloudfall/node.crt"
    assert settings["ssl_options.keyfile"] == "/etc/rabbitmq/cloudfall/node.key"
    assert settings["ssl_options.versions.1"] == "tlsv1.3"
    assert settings["ssl_options.versions.2"] == "tlsv1.2"
    assert "listeners.ssl" not in settings
    # Plain AMQP stays on loopback: a plain connection from 10.0.0.0/24 finds
    # nothing listening on the node's address
    assert settings["listeners.tcp.default"] == "127.0.0.1:5672"
    assert not any(
        key.startswith("listeners.tcp.") and key != "listeners.tcp.default"
        for key in settings
    )
    # Node discovery and the inter-node port stay on loopback for one node
    assert settings["distribution.listener.interface"] == "127.0.0.1"


# proves: S-023-19
def test_s023_19_only_the_tls_listener_leaves_loopback() -> None:
    allowed = _template("{{ cloudfall_rabbitmq_allowed_listeners }}", _with_access())
    assert allowed == ["203.0.113.11:5671"]
    sockets = "\n".join(  # noqa: FLY002 - one socket per line
        (
            'LISTEN 0 128 127.0.0.1:5672 0.0.0.0:* users:(("beam.smp",pid=9,fd=1))',
            'LISTEN 0 128 203.0.113.11:5671 0.0.0.0:* users:(("beam.smp",pid=9,fd=2))',
            'LISTEN 0 128 127.0.0.1:4369 0.0.0.0:* users:(("epmd",pid=8,fd=3))',
        )
    )
    assert FILTERS.public_listeners(sockets, cast("list[str]", allowed)) == []
    plain = (
        sockets
        + '\nLISTEN 0 128 203.0.113.11:5672 0.0.0.0:* users:(("beam.smp",pid=9,fd=4))'
    )
    assert FILTERS.public_listeners(plain, cast("list[str]", allowed)) == [
        "203.0.113.11:5672 beam.smp"
    ]


# proves: S-023-19
def test_s023_19_the_key_is_the_nodes_alone_and_the_certificate_required() -> None:
    tls = _tasks("tls.yml")
    key = _task("Let the node alone read its key", tls)
    assert key["ansible.builtin.file"]["owner"] == "rabbitmq"
    assert key["ansible.builtin.file"]["group"] == "rabbitmq"
    assert key["ansible.builtin.file"]["mode"] == "0600"
    require = _task("Require the node certificate before clients use TLS", tls)
    assert require["ansible.builtin.assert"]["that"] == [
        "item.stat.exists",
        "item.stat.isreg",
    ]
    # Checked before the settings that name the files are written
    names = [task["name"] for task in _tasks("main.yml")]
    assert names.index("Prepare the node certificate for clients over TLS") < (
        names.index("Install the managed settings")
    )
    # Only what the services converge staged for this node is copied
    staged = _template(
        "{{ cloudfall_rabbitmq_delivered_files }}",
        _with_access(
            cloudfall_services_rabbitmq_delivered_files={
                "h1": [{"path": "/etc/rabbitmq/cloudfall/node.crt"}],
                "h2": [{"path": "/etc/rabbitmq/cloudfall/ca.crt"}],
            }
        ),
    )
    assert staged == [{"path": "/etc/rabbitmq/cloudfall/node.crt"}]


def test_client_access_needs_the_rendered_address() -> None:
    require = _task("Require the node's rendered client access", _tasks("main.yml"))

    assert _holds(require, _variables())
    assert _holds(require, _with_access())
    assert not _holds(require, _with_access(cloudfall_rabbitmq_client_access={}))
