#!/usr/bin/env bash
# Look at (and free up) Postgres connection slots on the server.
#
#   sudo bash deploy/connections.sh                 # who is using the slots
#   sudo bash deploy/connections.sh --kill-idle     # free slots idle > 10 min
#   sudo bash deploy/connections.sh --kill-idle 2   # ... idle > 2 min (tighter)
#
# "sorry, too many clients already" means every slot in max_connections is
# taken. Postgres keeps a few in reserve for superusers, so `postgres` can
# usually still get in to clear them -- that is what this script uses.
#
# Killing an idle connection is safe: QGIS and the dashboard both reconnect on
# their next query. An "idle in transaction" session is the dangerous kind --
# it holds locks -- and is killed first. Sessions that are actively running a
# query are never touched.
set -euo pipefail
MINUTES="${2:-10}"
PSQL="docker exec -i coffee-pg psql -U postgres -d coffee_eudr -X -q"

if [ "${1:-}" = "--kill-idle" ]; then
  echo "== terminating sessions idle for more than ${MINUTES} minutes"
  $PSQL <<SQL
SELECT count(*) AS terminated FROM (
  SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
  WHERE datname = current_database()
    AND pid <> pg_backend_pid()
    AND state IN ('idle', 'idle in transaction')
    AND state_change < now() - interval '${MINUTES} minutes'
) t;
SQL
  echo
fi

$PSQL <<'SQL'
\echo '=================================================================='
\echo ' slots'
\echo '=================================================================='
SELECT (SELECT setting::int FROM pg_settings WHERE name = 'max_connections')        AS max_connections,
       (SELECT setting::int FROM pg_settings WHERE name = 'superuser_reserved_connections') AS reserved,
       (SELECT count(*) FROM pg_stat_activity)                                      AS in_use,
       (SELECT setting::int FROM pg_settings WHERE name = 'max_connections')
     - (SELECT count(*) FROM pg_stat_activity)                                      AS free;

\echo ''
\echo ' who is holding them (idle_for = how long that session has done nothing)'
SELECT coalesce(host(client_addr), 'local') AS client, usename, state,
       count(*), max(now() - state_change)::interval(0) AS idle_for,
       max(now() - backend_start)::interval(0) AS connected_for
FROM pg_stat_activity
WHERE datname = current_database()
GROUP BY 1, 2, 3 ORDER BY 4 DESC;

\echo ''
\echo ' anything stuck in a transaction (holds locks -- kill these first):'
SELECT pid, usename, host(client_addr) AS client,
       (now() - state_change)::interval(0) AS idle_in_txn,
       left(replace(query, E'\n', ' '), 60) AS last_query
FROM pg_stat_activity
WHERE datname = current_database() AND state = 'idle in transaction'
ORDER BY state_change LIMIT 20;

\echo ''
\echo ' longest-running live queries:'
SELECT pid, usename, (now() - query_start)::interval(0) AS running,
       left(replace(query, E'\n', ' '), 60) AS query
FROM pg_stat_activity
WHERE datname = current_database() AND state = 'active' AND pid <> pg_backend_pid()
ORDER BY query_start LIMIT 10;

\echo ''
\echo ' timeouts that recycle idle sessions by themselves:'
SELECT name, setting FROM pg_settings
WHERE name IN ('idle_session_timeout', 'idle_in_transaction_session_timeout', 'tcp_keepalives_idle')
ORDER BY name;
SQL

cat <<'NOTE'

------------------------------------------------------------------
If psql itself cannot connect, every reserved slot is gone too. Then:

  sudo docker restart coffee-pg      # drops ALL connections; digitisers
                                     # reconnect in QGIS, unsaved edits are lost

Permanent fixes, in order of effect:
  1. Each QGIS project opens one connection PER LAYER, per digitiser. Ask
     people to close projects they are not using, and keep the number of
     PostGIS layers in the digitising project small.
  2. Shorten the idle timeout so abandoned sessions free themselves:
       ALTER SYSTEM SET idle_session_timeout = '30min';
       SELECT pg_reload_conf();
  3. Give the dashboard a smaller pool: DASH_POOL=6 in .env (each gunicorn
     worker keeps its own pool, and most requests are served from its cache
     without touching the database at all).
  4. If it keeps happening, put PgBouncer in transaction mode in front of
     Postgres -- 19 QGIS clients then share a handful of real connections.
------------------------------------------------------------------
NOTE
