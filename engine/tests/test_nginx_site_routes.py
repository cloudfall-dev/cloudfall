"""Domain routes rendered by ``cloudfall_nginx_site`` (S-003).

There is no Nginx here, so the vhost is rendered by Ansible's own template
module on localhost and read back: its locations, their order and their
directives. What a request answers is worked out by ``_serve``, a small model
of Nginx's longest-prefix match, ``alias`` and ``try_files`` over a release
tree under ``tmp_path``. The ACL tasks run on localhost with stand-ins for
``getfacl`` and ``setfacl``. ``nginx -t`` and real requests are left for a live
proving run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
import yaml
from cloudfall.inventory import PlatformInventory
from cloudfall.validation import validate_config
from cloudfall_engine.ansible_inventory import render_ansible_inventory

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "config" / "examples"
SCHEMAS = ROOT / "config" / "schemas" / "v1"
ROLE = ROOT / "engine" / "ansible" / "roles" / "cloudfall_nginx_site"
TEMPLATE = ROLE / "templates" / "domain-site.conf.j2"
DEPLOY_TASKS = ROOT / "engine" / "ansible" / "roles" / "cloudfall_deploy" / "tasks"
GOLDEN = Path(__file__).parent / "golden" / "nginx-site"
NGINX_VERSIONS = ("1.22.1", "1.26.3")
INSTALL_ROOT = "/srv/apps/crm/backend"

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
# The same routes as _SPA_ROUTES plus a nested one, declared shortest first.
_NESTED_ROUTES = """\
  routes:
    - path: /
      static:
        component: crm-backend
        directory: web/dist
        spaFallback: index.html
    - path: /api/
      upstream: { address: 127.0.0.1, port: 8310 }
    - path: /api/v2/
      upstream: { address: 127.0.0.1, port: 8320 }
    - path: /docs/
      static:
        component: crm-backend
        directory: web/docs
"""
# Routes with no `/` route: nothing the Domain declares answers `/`.
_NO_ROOT_ROUTES = """\
  routes:
    - path: /api/
      upstream: { address: 127.0.0.1, port: 8310 }
    - path: /docs/
      static:
        component: crm-backend
        directory: web/docs
"""
_NESTED_ROUTES_REVERSED = """\
  routes:
    - path: /docs/
      static:
        component: crm-backend
        directory: web/docs
    - path: /api/v2/
      upstream: { address: 127.0.0.1, port: 8320 }
    - path: /api/
      upstream: { address: 127.0.0.1, port: 8310 }
    - path: /
      static:
        component: crm-backend
        directory: web/dist
        spaFallback: index.html
"""


def _upstream_only_domain() -> dict[str, object]:
    """Return the Domain the golden files were rendered from on origin/main."""
    return {
        "id": "crm-site",
        "primaryName": "crm.example.test",
        "aliases": ["www.crm.example.test"],
        "proxy": {
            "server": "h2",
            "configurationPath": "/etc/nginx/sites-available/crm.example.test.conf",
            "service": "nginx.service",
            "upstream": {"address": "127.0.0.1", "port": 8100},
        },
        "tls": {"mode": "required"},
    }


def _routed_domain(tmp_path: Path, routes: str) -> dict[str, object]:
    """Render the example Domain with ``routes`` through the engine inventory."""
    project = tmp_path / f"project-{abs(hash(routes))}"
    shutil.copytree(EXAMPLES, project)
    path = project / "domains" / "crm-site.yaml"
    text = path.read_text(encoding="utf-8")
    assert _UPSTREAM in text
    text = text.replace(_UPSTREAM, "").replace("  origin:\n", routes + "  origin:\n")
    path.write_text(text, encoding="utf-8")
    inventory = render_ansible_inventory(
        PlatformInventory.from_state(validate_config(project, SCHEMAS))
    )
    children = cast("dict[str, dict[str, object]]", inventory["all"])["children"]
    servers = cast("dict[str, dict[str, dict[str, object]]]", children)
    hosts = cast("dict[str, dict[str, object]]", servers["cloudfall_servers"]["hosts"])
    domains = hosts["h2"]["cloudfall_domains"]
    (domain,) = cast("list[dict[str, object]]", domains)
    return domain


def _ansible_playbook() -> str:
    binary = shutil.which("ansible-playbook")
    assert binary is not None
    return binary


def _play(tmp_path: Path, name: str, play: dict[str, object]) -> str:
    playbook = tmp_path / f"{name}.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {"hosts": "localhost", "connection": "local", "gather_facts": False}
                | play
            ]
        ),
        encoding="utf-8",
    )
    done = subprocess.run(  # noqa: S603 - fixed binary and arguments.
        [_ansible_playbook(), "--inventory", "localhost,", str(playbook)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def _render_all(
    tmp_path: Path, domains: dict[str, dict[str, object]]
) -> dict[str, str]:
    """Render each Domain with TLS on and off on both Nginx versions."""
    output = tmp_path / "rendered"
    output.mkdir()
    tasks: list[dict[str, object]] = []
    names: list[str] = []
    for label, domain in domains.items():
        for version in NGINX_VERSIONS:
            for tls_active in (True, False):
                name = f"{label}/{'tls' if tls_active else 'http'}-{version}.conf"
                (output / label).mkdir(exist_ok=True)
                names.append(name)
                tasks.append(
                    {
                        "name": f"Render {name}",
                        "ansible.builtin.template": {
                            "src": str(TEMPLATE),
                            "dest": str(output / name),
                            "mode": "0644",
                        },
                        "vars": {
                            "cloudfall_nginx_site_domain": domain,
                            "cloudfall_nginx_site_acme_webroot": "/var/lib/letsencrypt",
                            "cloudfall_nginx_site_tls_active": tls_active,
                            "cloudfall_nginx_site_version": version,
                        },
                    }
                )
    _play(tmp_path, "render", {"tasks": tasks})
    return {name: (output / name).read_text(encoding="utf-8") for name in names}


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    tmp_path = tmp_path_factory.mktemp("nginx-site-routes")
    return _render_all(
        tmp_path,
        {
            "upstream": _upstream_only_domain(),
            "spa": _routed_domain(tmp_path, _SPA_ROUTES),
            "nested": _routed_domain(tmp_path, _NESTED_ROUTES),
            "reversed": _routed_domain(tmp_path, _NESTED_ROUTES_REVERSED),
            "no-root": _routed_domain(tmp_path, _NO_ROOT_ROUTES),
        },
    )


def _servers(vhost: str) -> list[str]:
    """Split a vhost into its top-level ``server { ... }`` blocks."""
    blocks: list[str] = []
    depth = 0
    start = 0
    for index, character in enumerate(vhost):
        if character == "{":
            if depth == 0:
                start = vhost.rindex("server", 0, index)
            depth += 1
        elif character == "}":
            depth -= 1
            assert depth >= 0, vhost
            if depth == 0:
                blocks.append(vhost[start : index + 1])
    assert depth == 0, vhost
    return blocks


def _locations(server: str) -> dict[str, list[str]]:
    """Each ``location <path> { ... }`` of a server block, in order."""
    found: dict[str, list[str]] = {}
    lines = server.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line.startswith("location "):
            path = line.removeprefix("location ").removesuffix(" {")
            directives: list[str] = []
            index += 1
            while lines[index].strip() != "}":
                directives.append(lines[index].strip())
                index += 1
            assert path not in found, server
            found[path] = directives
        index += 1
    return found


def _serving_server(vhost: str, *, tls_active: bool) -> str:
    servers = _servers(vhost)
    assert len(servers) == (2 if tls_active else 1)
    return servers[-1]


def _serve(server: str, uri: str, filesystem: Path) -> tuple[int, bytes | str]:
    """Answer ``uri`` the way Nginx matches and serves these locations.

    The longest prefix wins; a proxy location answers its upstream and the
    path the upstream sees, a static one maps the rest of the URI under its
    alias and falls back through ``try_files`` to an internal redirect.
    ``filesystem`` stands in for ``/``.
    """
    locations = _locations(server)
    for _ in range(10):
        path = max((p for p in locations if uri.startswith(p)), key=len)
        directives = locations[path]
        proxy = [d for d in directives if d.startswith("proxy_pass ")]
        if proxy:
            target = proxy[0].removeprefix("proxy_pass ").removesuffix(";")
            # No URI part on proxy_pass: the request path passes unchanged.
            assert target.count("/") == 2, target
            return 200, f"{target}{uri}"
        if directives[0].startswith("return "):
            _, code, *location = directives[0].removesuffix(";").split()
            return int(code), " ".join(location)
        directive, directory = directives[0].removesuffix(";").split()
        assert path.endswith("/")
        if directive == "root":
            # `root` appends the whole URI.
            candidate = filesystem / (directory + uri).lstrip("/")
        else:
            assert directive == "alias"
            assert directory.endswith("/")
            candidate = filesystem / (directory + uri[len(path) :]).lstrip("/")
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return 200, (candidate / "index.html").read_bytes()
        if candidate.is_file():
            return 200, candidate.read_bytes()
        fallback = [d for d in directives if d.startswith("try_files ")]
        if not fallback:
            return 404, b""
        uri = fallback[0].split()[-1].removesuffix(";")
    pytest.fail(f"internal redirection cycle for {uri}")


def _release_tree(filesystem: Path, releases: dict[str, str]) -> Path:
    """Lay out ``releases/<id>/web/dist`` and point ``current`` at the last."""
    install_root = filesystem / INSTALL_ROOT.lstrip("/")
    for release, marker in releases.items():
        dist = install_root / "releases" / release / "web" / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text(f"<html>{marker}</html>\n", encoding="utf-8")
        (dist / "assets" / "app.js").write_text(f"// {marker}\n", encoding="utf-8")
    (install_root / "current").symlink_to(install_root / "releases" / release)
    env = install_root / "shared" / "env"
    env.mkdir(parents=True)
    (env / "crm-backend.env").write_text("SECRET=1\n", encoding="utf-8")
    return install_root


# proves: S-003-7
@pytest.mark.parametrize("nginx_version", NGINX_VERSIONS)
@pytest.mark.parametrize("tls", ["tls", "http"])
def test_s003_7_an_upstream_only_domain_renders_byte_identical(
    rendered: dict[str, str], tls: str, nginx_version: str
) -> None:
    """The golden files are origin/main's render, captured before S-003."""
    name = f"{tls}-{nginx_version}.conf"
    golden = (GOLDEN / name).read_text(encoding="utf-8")

    assert rendered[f"upstream/{name}"] == golden


# proves: S-003-2
@pytest.mark.parametrize("nginx_version", NGINX_VERSIONS)
@pytest.mark.parametrize("tls_active", [True, False])
def test_s003_2_routes_render_where_the_single_proxy_rendered(
    rendered: dict[str, str], *, tls_active: bool, nginx_version: str
) -> None:
    vhost = rendered[f"spa/{'tls' if tls_active else 'http'}-{nginx_version}.conf"]
    servers = _servers(vhost)
    plain = _locations(servers[0])

    if tls_active:
        assert plain["/"] == ["return 301 https://$host$request_uri;"]
        assert ("listen 443 ssl http2;" in servers[1]) is (nginx_version == "1.22.1")
        assert ("http2 on;" in servers[1]) is (nginx_version == "1.26.3")
    serving = _locations(_serving_server(vhost, tls_active=tls_active))
    assert set(serving) >= {"/api/", "/"}
    for directives in serving.values():
        assert directives
        assert all(d.endswith(";") for d in directives), directives
    assert "None" not in vhost
    assert "{{" not in vhost
    assert "{%" not in vhost


# proves: S-003-3
@pytest.mark.parametrize("tls_active", [True, False])
def test_s003_3_unknown_paths_fall_back_and_assets_are_served(
    rendered: dict[str, str], tmp_path: Path, *, tls_active: bool
) -> None:
    vhost = rendered[f"spa/{'tls' if tls_active else 'http'}-1.26.3.conf"]
    server = _serving_server(vhost, tls_active=tls_active)
    _release_tree(tmp_path, {"r1": "release one"})
    dist = tmp_path / INSTALL_ROOT.lstrip("/") / "current" / "web" / "dist"

    assert _locations(server)["/"] == [
        f"alias {INSTALL_ROOT}/current/web/dist/;",
        "try_files $uri /index.html;",
    ]
    assert _serve(server, "/some/unknown/path", tmp_path) == (
        200,
        (dist / "index.html").read_bytes(),
    )
    assert _serve(server, "/assets/app.js", tmp_path) == (
        200,
        (dist / "assets" / "app.js").read_bytes(),
    )


# proves: S-003-4
@pytest.mark.parametrize("tls_active", [True, False])
def test_s003_4_api_requests_reach_the_upstream_with_their_full_path(
    rendered: dict[str, str], tmp_path: Path, *, tls_active: bool
) -> None:
    vhost = rendered[f"spa/{'tls' if tls_active else 'http'}-1.22.1.conf"]
    server = _serving_server(vhost, tls_active=tls_active)

    assert "proxy_pass http://127.0.0.1:8310;" in _locations(server)["/api/"]
    assert _serve(server, "/api/health", tmp_path) == (
        200,
        "http://127.0.0.1:8310/api/health",
    )


# proves: S-003-5
def test_s003_5_a_static_route_without_a_fallback_answers_404(
    rendered: dict[str, str], tmp_path: Path
) -> None:
    server = _serving_server(rendered["nested/http-1.26.3.conf"], tls_active=False)
    docs = tmp_path / INSTALL_ROOT.lstrip("/") / "current" / "web" / "docs"
    _release_tree(tmp_path, {"r1": "release one"})
    docs.mkdir()
    (docs / "guide.html").write_text("guide\n", encoding="utf-8")

    assert _locations(server)["/docs/"] == [f"alias {INSTALL_ROOT}/current/web/docs/;"]
    assert _serve(server, "/docs/guide.html", tmp_path) == (200, b"guide\n")
    assert _serve(server, "/docs/missing.html", tmp_path) == (404, b"")


@pytest.mark.parametrize("tls_active", [True, False])
def test_a_path_no_route_matches_answers_404(
    rendered: dict[str, str], tmp_path: Path, *, tls_active: bool
) -> None:
    """Without a `/` route, Nginx would serve its default root for `/`.

    On Debian that is `/usr/share/nginx/html`, whose welcome page answers 200,
    so a Domain health check on `/` would pass against nothing the Domain
    declares.
    """
    vhost = rendered[f"no-root/{'tls' if tls_active else 'http'}-1.22.1.conf"]
    server = _serving_server(vhost, tls_active=tls_active)
    locations = _locations(server)

    assert locations["/"] == ["return 404;"]
    assert list(locations)[-1] == "/"
    assert _serve(server, "/", tmp_path) == (404, "")
    assert _serve(server, "/elsewhere", tmp_path) == (404, "")
    assert _serve(server, "/api/health", tmp_path) == (
        200,
        "http://127.0.0.1:8310/api/health",
    )


def _switch_current(tmp_path: Path, install_root: Path, release: str) -> None:
    """Run rollback.yml's own symlink switch on localhost."""
    tasks = yaml.safe_load((DEPLOY_TASKS / "rollback.yml").read_text(encoding="utf-8"))
    (switch,) = (
        task
        for task in tasks
        if task["name"] == "Switch the current release symlink to the rollback target"
    )
    _play(
        tmp_path,
        "rollback",
        {
            "vars": {
                "cloudfall_deploy_release_directory": str(
                    install_root / "releases" / release
                ),
                "cloudfall_deploy_current_link": str(install_root / "current"),
            },
            "tasks": [switch],
        },
    )


# proves: S-003-6
def test_s003_6_a_rollback_serves_the_previous_release_without_nginx(
    rendered: dict[str, str], tmp_path: Path
) -> None:
    vhost = rendered["spa/tls-1.26.3.conf"]
    server = _serving_server(vhost, tls_active=True)
    install_root = _release_tree(tmp_path, {"r1": "release one", "r2": "release two"})
    assert _serve(server, "/", tmp_path) == (200, b"<html>release two</html>\n")

    _switch_current(tmp_path, install_root, "r1")

    assert _serve(server, "/", tmp_path) == (200, b"<html>release one</html>\n")
    # The vhost reads the release through `current`, never a release id, and
    # the rollback tasks neither render the vhost nor reload Nginx.
    assert "/releases/" not in vhost
    rollback = (DEPLOY_TASKS / "rollback.yml").read_text(encoding="utf-8")
    assert "nginx" not in rollback.lower()


# proves: S-003-13
@pytest.mark.parametrize("nginx_version", NGINX_VERSIONS)
@pytest.mark.parametrize("tls", ["tls", "http"])
def test_s003_13_locations_render_longest_path_first_whatever_the_order(
    rendered: dict[str, str], tls: str, nginx_version: str
) -> None:
    name = f"{tls}-{nginx_version}.conf"
    vhost = rendered[f"nested/{name}"]
    server = _serving_server(vhost, tls_active=tls == "tls")

    assert vhost == rendered[f"reversed/{name}"]
    routes = [
        path for path in _locations(server) if path != "/.well-known/acme-challenge/"
    ]
    assert routes == ["/api/v2/", "/docs/", "/api/", "/"]


# proves: S-003-15
@pytest.mark.parametrize("tls_active", [True, False])
def test_s003_15_the_acme_challenge_still_renders_on_a_domain_with_routes(
    rendered: dict[str, str], tmp_path: Path, *, tls_active: bool
) -> None:
    vhost = rendered[f"spa/{'tls' if tls_active else 'http'}-1.22.1.conf"]
    plain = _servers(vhost)[0]
    challenge = tmp_path / "var" / "lib" / "letsencrypt" / ".well-known"
    (challenge / "acme-challenge").mkdir(parents=True)
    (challenge / "acme-challenge" / "token").write_text("proof\n", encoding="utf-8")

    assert _locations(plain)["/.well-known/acme-challenge/"] == [
        "root /var/lib/letsencrypt;"
    ]
    assert _serve(plain, "/.well-known/acme-challenge/token", tmp_path) == (
        200,
        b"proof\n",
    )
    assert _serve(plain, "/.well-known/acme-challenge/missing", tmp_path) == (
        404,
        b"",
    )


def _stand_in(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


_GETFACL = """\
import json, sys
state = json.load(open(sys.argv[0] + ".json"))
directory = sys.argv[-1]
print("user::rwx")
for entry in state.get(directory, []):
    print(entry)
print("group::r-x")
print("other::---")
"""
_SETFACL = """\
import json, sys
path = sys.argv[0].replace("setfacl", "getfacl") + ".json"
state = json.load(open(path))
assert sys.argv[1] == "--modify", sys.argv
state.setdefault(sys.argv[3], []).append(sys.argv[2])
json.dump(state, open(path, "w"))
with open(sys.argv[0] + ".log", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
"""


def _grant_play(tmp_path: Path, domain: dict[str, object]) -> list[list[str]]:
    """Run the role's collect-and-grant tasks with stand-in ACL tools."""
    getfacl = _stand_in(tmp_path / "getfacl", _GETFACL)
    setfacl = _stand_in(tmp_path / "setfacl", _SETFACL)
    state = Path(f"{getfacl}.json")
    if not state.exists():
        state.write_text("{}", encoding="utf-8")
    main = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    (collect,) = (
        t
        for t in main
        if t["name"] == "Collect the directories on the static routes' paths"
    )
    access = yaml.safe_load(
        (ROLE / "tasks" / "static_access.yml").read_text(encoding="utf-8")
    )
    commands = [t for t in access if "ansible.builtin.command" in t]
    for task, binary in zip(commands, (getfacl, setfacl), strict=True):
        task["ansible.builtin.command"]["argv"][0] = str(binary)
    _play(
        tmp_path,
        "grant",
        {
            "vars": {
                "cloudfall_nginx_site_domain": domain,
                "cloudfall_nginx_site_worker_user": "www-data",
            },
            "tasks": [collect, *commands],
        },
    )
    log = Path(f"{setfacl}.log")
    calls = (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )
    log.unlink(missing_ok=True)
    return calls


# proves: S-003-14
def test_s003_14_the_worker_may_only_traverse_to_the_release(tmp_path: Path) -> None:
    domain = _routed_domain(tmp_path, _NESTED_ROUTES)
    routes = cast("list[dict[str, dict[str, object]]]", domain["routes"])
    statics = [route["static"] for route in routes if "static" in route]
    path = [
        "/srv/apps/crm",
        "/srv/apps/crm/backend",
        "/srv/apps/crm/backend/releases",
    ]
    assert [s["servedDirectory"] for s in statics] == [
        f"{INSTALL_ROOT}/current/web/dist",
        f"{INSTALL_ROOT}/current/web/docs",
    ]
    assert all(s["traverseDirectories"] == path for s in statics)

    first = _grant_play(tmp_path, domain)
    second = _grant_play(tmp_path, domain)

    assert first == [["--modify", "user:www-data:--x", d] for d in path]
    assert second == []
    # Nothing under shared/ is touched, and the worker joins no group: the
    # env file stays root:<application> 0640, which www-data cannot read.
    deploy = yaml.safe_load((DEPLOY_TASKS / "main.yml").read_text(encoding="utf-8"))
    (env,) = (
        t for t in deploy if t["name"] == "Install the component environment file"
    )
    assert env["ansible.builtin.copy"]["mode"] == "0640"
    assert env["ansible.builtin.copy"]["owner"] == "root"
    role_tasks = "".join(
        p.read_text(encoding="utf-8") for p in (ROLE / "tasks").glob("*.yml")
    )
    assert "ansible.builtin.user" not in role_tasks
    assert "ansible.builtin.group" not in role_tasks


# proves: S-003-14
def test_s003_14_an_upstream_only_domain_grants_nothing(tmp_path: Path) -> None:
    assert _grant_play(tmp_path, _upstream_only_domain()) == []
