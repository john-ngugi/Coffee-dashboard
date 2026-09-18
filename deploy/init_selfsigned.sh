#!/usr/bin/env bash
# Bootstrap nginx with a self-signed certificate (no domain needed).
# Run from the repo root after setting DOMAIN (may be an IP) in .env.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
: "${DOMAIN:?set DOMAIN in .env (the public IP is fine)}"

if [ ! -f deploy/nginx/.htpasswd ]; then
  echo "== no deploy/nginx/.htpasswd yet; creating user 'coffee'"
  read -rsp "password for dashboard user 'coffee': " PW; echo
  docker run --rm httpd:2.4-alpine htpasswd -nbB coffee "$PW" > deploy/nginx/.htpasswd
fi

echo "== generating a 10-year self-signed certificate for $DOMAIN"
docker compose run --rm --no-deps --entrypoint sh nginx -c "
  mkdir -p /etc/letsencrypt/live/$DOMAIN &&
  apk add --no-cache openssl >/dev/null &&
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 -subj '/CN=$DOMAIN' \
    -addext 'subjectAltName=IP:$DOMAIN' \
    -keyout /etc/letsencrypt/live/$DOMAIN/privkey.pem \
    -out    /etc/letsencrypt/live/$DOMAIN/fullchain.pem" 2>/dev/null \
|| docker compose run --rm --no-deps --entrypoint sh nginx -c "
  apk add --no-cache openssl >/dev/null &&
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 -subj '/CN=$DOMAIN' \
    -keyout /etc/letsencrypt/live/$DOMAIN/privkey.pem \
    -out    /etc/letsencrypt/live/$DOMAIN/fullchain.pem"

docker compose --profile public up -d nginx
echo "== done: https://$DOMAIN:${NGINX_HTTPS_PORT:-8443}  (browser will warn once about the self-signed cert)"
