### Changed

- Managed nginx vhosts no longer serve HTTP/2: the `cloudfall_nginx_site` role renders `http2` on its TLS `listen` lines (or `http2 on;` from nginx 1.25.1) only when `cloudfall_nginx_site_http2` is true, and it defaults to false while nginx's HTTP/2 flow control is exploitable (NGINX2Root, CVE-2026-123007, fixed in nginx 1.31.9). Turn it back on with `--extra-vars cloudfall_nginx_site_http2=true` or the inventory once the fixed nginx is installed (#551)
