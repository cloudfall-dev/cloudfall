### Added

- The `cloudfall_nginx_site` role takes `cloudfall_nginx_site_http2` (bool, default `true`): set it to `false` in the inventory or with `--extra-vars cloudfall_nginx_site_http2=false` and the managed vhost's HTTPS server renders `listen 443 ssl;` and `listen [::]:443 ssl;` with no `http2` on any Nginx version, so turning HTTP/2 off survives the next `domains` run. The default renders a byte-identical vhost (#551)
