### Changed

- The managed vhost's HTTPS server no longer serves HTTP/2 by default: the `cloudfall_nginx_site` role takes `cloudfall_nginx_site_http2` (bool, default `false`), and the next `domains` run renders `listen 443 ssl;` and `listen [::]:443 ssl;` with no `http2` on any Nginx version. Once the installed Nginx carries the HTTP/2 fix, set it to `true` in the inventory or with `--extra-vars cloudfall_nginx_site_http2=true` to render HTTP/2 as before (#551)
