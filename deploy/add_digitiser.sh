#!/usr/bin/env bash
# Create personal database logins for digitisers so the polygon history
# records who did what. Each login inherits coffee_editor.
#
#   sudo bash deploy/add_digitiser.sh alice bob carol          # editors
#   sudo bash deploy/add_digitiser.sh --supervisor jane        # may undo changes
#
# Prints one line per user: name and generated password. Share those
# privately; the passwords are not stored anywhere else.
set -euo pipefail
cd "$(dirname "$0")/.."
ROLE=coffee_editor
if [ "${1:-}" = "--supervisor" ]; then ROLE=coffee_supervisor; shift; fi
[ $# -gt 0 ] || { echo "usage: $0 [--supervisor] name [name ...]" >&2; exit 1; }

for u in "$@"; do
  if ! [[ "$u" =~ ^[a-z][a-z0-9_]{1,30}$ ]]; then
    echo "skip '$u': use lowercase letters, digits, underscore" >&2; continue
  fi
  pw=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)
  docker compose exec -T db psql -U postgres -v ON_ERROR_STOP=1 -q \
    -v u="$u" -v pw="$pw" -v role="$ROLE" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L IN ROLE %I', :'u', :'pw', :'role') \gexec
SQL
  echo "$u  $pw  ($ROLE)"
done
