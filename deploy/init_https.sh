#!/usr/bin/env bash
# One-time bootstrap for nginx + Let's Encrypt.
# Run from the repo root after setting DOMAIN and LETSENCRYPT_EMAIL in .env
# and forwarding router ports 80/443 to this machine's NGINX_HTTP_PORT/NGINX_HTTPS_PORT
# (Let's Encrypt must reach the HTTP-01 challenge on external port 80).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
: "${DOMAIN:?set DOMAIN in .env}" "${LETSENCRYPT_EMAIL:?set LETSENCRYPT_EMAIL in .env}"

if [ ! -f deploy/nginx/.htpasswd ]; then
  echo "== no deploy/nginx/.htpasswd yet; creating user 'coffee'"
  read -rsp "password for dashboard user 'coffee': " PW; echo
  docker run --rm httpd:2.4-alpine htpasswd -nbB coffee "$PW" > deploy/nginx/.htpasswd
fi

echo "== temporary self-signed cert so nginx can start"
docker compose --profile letsencrypt run --rm --entrypoint sh certbot -c "
  mkdir -p /etc/letsencrypt/live/$DOMAIN &&
  openssl req -x509 -nodes -newkey rsa:2048 -days 1 -subj '/CN=$DOMAIN' \
    -keyout /etc/letsencrypt/live/$DOMAIN/privkey.pem \
    -out    /etc/letsencrypt/live/$DOMAIN/fullchain.pem"

docker compose --profile public up -d nginx

echo "== requesting the real certificate"
docker compose --profile letsencrypt run --rm --entrypoint sh certbot -c "
  rm -rf /etc/letsencrypt/live/$DOMAIN /etc/letsencrypt/archive/$DOMAIN /etc/letsencrypt/renewal/$DOMAIN.conf;
  certbot certonly --webroot -w /var/www/certbot -d $DOMAIN \
    --email $LETSENCRYPT_EMAIL --agree-tos --no-eff-email --non-interactive"

docker compose exec nginx nginx -s reload
docker compose --profile letsencrypt up -d certbot
echo "== done: https://$DOMAIN:${NGINX_HTTPS_PORT:-8443}"
