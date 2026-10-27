# S-003: Domain routes

status: approved

## Problem

A Domain sends every request to one upstream. `domain.schema.json` has a single
`proxy.upstream` (`address`, `port`), and `domain-site.conf.j2` renders one `location /` that
proxies to it. A web app at `/` and its API at `/api/` on one hostname (one certificate, no
CORS) can't be declared. Abecra (Cloudfall's customer zero) works around it by mounting the
web build inside the API with an SPA fallback, so static serving lives in application code.
Two components behind one hostname can't be expressed at all
(cloudfall-dev/cloudfall#90).

S-002 (Component build steps) puts a frontend build in the component's artifact; this spec
serves it from the release, so the web build switches and rolls back with the API.

## Behaviour

A Domain may declare `spec.routes`, a list of path routes, each either a proxy or static files
from a component's release:

```yaml
spec:
  proxy:
    server: h2
    configurationPath: /etc/nginx/sites-available/app.example.test.conf
    service: nginx.service
  routes:
    - path: /api/
      upstream: { address: 127.0.0.1, port: 8310 }
    - path: /
      static:
        component: abecra-staging-api
        directory: web/dist
        spaFallback: index.html
```

- A proxy route has `path` and `upstream` (`address` and `port`, the same shape as
  `proxy.upstream`). The request keeps its full path: `/api/health` reaches the upstream as
  `/api/health`, as the single proxy does today
- A static route has `path` and `static`: `component` (a Component id), `directory` (a path
  inside the release, relative, no `..`, not the release root) and an optional `spaFallback`
  (a file inside `directory`). It serves `<installRoot>/current/<directory>`, so it follows the
  component's `current` symlink: a deploy or `cloudfall rollback` changes what it serves with
  no nginx reload. Without `spaFallback`, an unknown path answers 404; with it, an unknown
  path under the route answers the fallback file
- Each route has exactly one of `upstream` or `static`

`cloudfall config validate` checks routes:

- A Domain declares at most one of `proxy.upstream` or `routes` (`domain_route_source_invalid`
  for both). A Domain with neither stays valid, as settled on #90: a fleet may build that
  Domain's route outside `cloudfall_nginx_site`. The separate fix for #90 makes the domains
  playbook fail before any change for a Domain bound for `cloudfall_nginx_site` without
  `proxy.upstream`; with routes, that check reads "`proxy.upstream` or `routes`"
- `proxy.upstream` stays as shorthand for one `path: /` proxy route; an existing Domain with
  only `proxy.upstream` validates and renders unchanged
- Every `path` starts with `/` and uses only letters, digits and `._~/-`, so a path can't
  inject nginx syntax (`domain_route_path_invalid`)
- A static route's `path` ends with `/`, which rules out the nginx alias "off-by-slash"
  traversal (`domain_route_path_invalid`)
- Paths are unique within the Domain (`domain_route_path_duplicate`)
- A path equal to or under `/.well-known/acme-challenge` is refused, since certificate
  issuance owns it (`domain_route_path_reserved`)
- A static route's component must exist (`resource_reference_missing`, as for other
  references) and list the Domain's `proxy.server` in its `deployment.servers`
  (`domain_route_component_not_on_proxy`), because nginx reads the files from that server's
  disk
- `directory` and `spaFallback` use only letters, digits and `._-/`, with no `..` segment

Rendering (`engine/ansible/roles/cloudfall_nginx_site/templates/domain-site.conf.j2`): routes
render where the single proxy location renders today, in the port 80 server without active
TLS and in the 443 server with it; the port 80 redirect and the ACME location stay as they
are. Locations render most specific first (longest path first, then by path), so the vhost
doesn't depend on the declared order; nginx matches by longest prefix either way. A Domain
with only `proxy.upstream` renders through the existing template path and gives a
byte-identical vhost.

The engine's Ansible inventory (`engine/src/cloudfall_engine/ansible_inventory.py`) resolves
each static route to its served directory, `<installRoot>/current/<directory>`, when it builds
the Domain variable, and `cloudfall_nginx_site` (`tasks/main.yml`, `meta/argument_specs.yml`)
accepts `routes` and asserts "upstream or routes" instead of "upstream"; the early check the
#90 fix adds reads the same.

The nginx worker must read the static files. Today `cloudfall_bootstrap` creates the
application root, the install root and `releases/` with mode `0750` owned by the application
user, so `www-data` can't reach them. The domain role grants the nginx worker user traverse-only
access along the path to each static route's directory (a POSIX ACL `u:www-data:x` on
those directories is the recommended way). It never adds the worker to the application's
group, because `shared/env/<component>.env` is group-readable.

`healthCheck` stays one per Domain. Its path may fall on any route.

Touches `config/schemas/v1/domain.schema.json`, `sdk/src/cloudfall/inventory.py`
(`DomainInventory`), `sdk/src/cloudfall/validation.py` (`_validate_domain_references`),
`engine/src/cloudfall_engine/ansible_inventory.py`, `engine/ansible/roles/cloudfall_nginx_site`,
`engine/ansible/playbooks/domains.yml`, and `cloudfall config validate`.

## Acceptance criteria

- S-003-1: A Domain with a `/api/` proxy route and a `/` static route with `spaFallback: index.html` passes `cloudfall config validate`
- S-003-2: That Domain renders a vhost that `nginx -t` accepts, with TLS active and without it, on nginx 1.22.1 and 1.26.3
- S-003-3: Against that vhost, `GET /some/unknown/path` answers 200 with the fallback file's bytes from `<installRoot>/current/web/dist/index.html`, and `GET /assets/app.js` answers that file's bytes
- S-003-4: Against that vhost, `GET /api/health` is proxied to `127.0.0.1:8310` and the upstream sees the path `/api/health`
- S-003-5: A static route without `spaFallback` answers 404 for a path with no matching file
- S-003-6: After `cloudfall rollback` of the static route's component, `GET /` serves the previous release's `index.html` without re-running the domains playbook or reloading nginx
- S-003-7: An existing Domain with only `proxy.upstream` renders a vhost byte-identical to the one the template on origin/main renders, for TLS active and inactive and both nginx versions
- S-003-8: A Domain with two routes for the same path fails `cloudfall config validate` with exit `CONFIG_INVALID` and code `domain_route_path_duplicate`
- S-003-9: A Domain with both `proxy.upstream` and `routes` fails `cloudfall config validate` with code `domain_route_source_invalid`, and a Domain with neither still passes it
- S-003-10: A route path under `/.well-known/acme-challenge` fails with `domain_route_path_reserved`, and a static route path without a trailing `/` or a path with a space, `;` or `{` fails with `domain_route_path_invalid`
- S-003-11: A static route naming a missing component fails with `resource_reference_missing`, and one whose component doesn't list the Domain's `proxy.server` fails with `domain_route_component_not_on_proxy`
- S-003-12: A static route whose `directory` or `spaFallback` contains a `..` segment or is absolute, or whose `directory` is `.`, fails `cloudfall config validate`
- S-003-13: Locations in the rendered vhost appear longest path first, and two Domains with the same routes in different declared order render identical bytes
- S-003-14: After the domains playbook runs, the `www-data` user can read `<installRoot>/current/web/dist/index.html` and cannot read `<installRoot>/shared/env/<component>.env`
- S-003-15: The ACME challenge location still renders and serves `/.well-known/acme-challenge/` from the webroot on a Domain with routes

## Out of scope

- Per-route health checks: `healthCheck` stays per Domain as the issue proposes; per-route checks can follow
- Regex, exact-match or rewritten (prefix-stripped) locations: proxy routes keep the full request path, matching today's single proxy. Stripping is a later option on the route
- Caching headers, compression and per-route nginx directives: a raw `extraConfig` snippet would bring back the unvalidated config the managed vhost exists to avoid
- Upstreams by component or hostname: `upstream` keeps today's IP address and port shape
- Static files from a component on another server: nginx reads them from its own disk, so the component must be deployed on the proxy server
- A separate static component kind: S-002 puts the build in the component's own artifact, so one release covers both

## Decisions relied on

- none

## Issues

## Verification
