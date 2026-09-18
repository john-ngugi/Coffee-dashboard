#!/usr/bin/env bash
# Pull the latest code and redeploy only the dashboard container.
# The image is built while the old container keeps serving, then swapped;
# db and nginx are not restarted. Downtime = dashboard start-up (a few seconds).
#   sudo bash deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== git pull"
git pull --ff-only

echo "== build new image (old container still serving)"
docker compose build dashboard

echo "== swap container"
docker compose up -d --no-deps dashboard

echo "== waiting for health"
for i in $(seq 1 30); do
  if curl -fs http://127.0.0.1:5055/api/config >/dev/null 2>&1; then
    echo "dashboard up after ${i}s"; docker image prune -f >/dev/null; exit 0
  fi
  sleep 1
done
echo "dashboard did not come up; last log lines:" >&2
docker compose logs --tail 30 dashboard >&2
exit 1
