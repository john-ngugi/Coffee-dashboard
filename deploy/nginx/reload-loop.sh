#!/bin/sh
# nginx reads its certificates once, at start-up, so a certificate renewed by
# certbot is only served after a reload. Everything in /docker-entrypoint.d/ is
# run by the nginx image before it starts nginx, so background a reload loop
# here rather than overriding the container's command -- overriding it skips
# the image's own start-up steps, including rendering
# /etc/nginx/templates/*.template into /etc/nginx/conf.d/.
( while :; do
    sleep 6h
    nginx -s reload 2>/dev/null || true
  done ) &
