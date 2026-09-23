#!/usr/bin/env bash
# Report what the database looks like on the server: spatial indexes and whether
# queries actually use them, table bloat, memory settings, cache hit ratio and
# connection use.  Read-only -- it changes nothing.
#
#   sudo bash deploy/check_db.sh
set -euo pipefail
PSQL="docker exec -i coffee-pg psql -U postgres -d coffee_eudr -X -q"

$PSQL <<'SQL'
\echo '=================================================================='
\echo ' 1. spatial indexes (every layer the dashboard draws needs one)'
\echo '=================================================================='
SELECT c.relname AS table, i.relname AS index, am.amname AS type,
       pg_size_pretty(pg_relation_size(i.oid)) AS size,
       s.idx_scan AS times_used
FROM pg_index x
JOIN pg_class c ON c.oid = x.indrelid
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_am am ON am.oid = i.relam
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.oid
WHERE n.nspname IN ('coffee','ref') AND am.amname = 'gist'
ORDER BY 1, 2;

\echo ''
\echo ' tables WITHOUT a spatial index (should be empty):'
SELECT n.nspname || '.' || c.relname AS table_missing_gist
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.atttypid = 'geometry'::regtype
WHERE c.relkind = 'r' AND n.nspname IN ('coffee','ref')
  AND NOT EXISTS (SELECT 1 FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid
                  JOIN pg_am am ON am.oid = i.relam
                  WHERE x.indrelid = c.oid AND am.amname = 'gist')
GROUP BY 1 ORDER BY 1;

\echo ''
\echo '=================================================================='
\echo ' 2. table sizes and bloat (dead rows left by build_qc UPDATE passes)'
\echo '=================================================================='
SELECT relname AS table,
       pg_size_pretty(pg_relation_size(relid)) AS heap,
       pg_size_pretty(pg_indexes_size(relid))  AS indexes,
       n_live_tup AS live_rows, n_dead_tup AS dead_rows,
       CASE WHEN n_live_tup > 0
            THEN round(100.0 * n_dead_tup / n_live_tup, 1) END AS pct_dead,
       coalesce(last_vacuum, last_autovacuum)::date AS last_vacuum,
       coalesce(last_analyze, last_autoanalyze)::date AS last_analyze
FROM pg_stat_user_tables
WHERE schemaname IN ('coffee','ref')
ORDER BY pg_relation_size(relid) DESC LIMIT 12;

\echo ''
\echo ' bloat estimate -- heap size vs the size the live rows actually need.'
\echo ' Over ~2x means the repeated UPDATE passes in build_qc left dead space:'
\echo ' every scan then reads several times more disk than necessary.'
\echo ' Fix:  VACUUM (FULL, ANALYZE) coffee.farm_point_qc;   (locks the table,'
\echo ' takes a few minutes, needs free disk equal to the new table size)'
SELECT c.relname AS table,
       pg_size_pretty(pg_relation_size(c.oid)) AS heap_on_disk,
       pg_size_pretty((s.n_live * s.width)::bigint) AS live_data,
       round(pg_relation_size(c.oid)::numeric / nullif(s.n_live * s.width, 0), 1) AS bloat_factor
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN LATERAL (
    SELECT c.reltuples::bigint AS n_live,
           coalesce(sum(st.avg_width), 0) + 28 AS width
    FROM pg_stats st WHERE st.schemaname = n.nspname AND st.tablename = c.relname
) s ON true
WHERE n.nspname = 'coffee' AND c.relkind = 'r' AND pg_relation_size(c.oid) > 50*1024*1024
ORDER BY pg_relation_size(c.oid) DESC;

\echo ''
\echo '=================================================================='
\echo ' 3. memory / planner settings'
\echo '=================================================================='
SELECT name, setting, unit FROM pg_settings
WHERE name IN ('shared_buffers','work_mem','maintenance_work_mem','effective_cache_size',
               'random_page_cost','max_connections','max_parallel_workers_per_gather',
               'jit','autovacuum_vacuum_scale_factor')
ORDER BY name;

\echo ''
\echo ' host memory (the container sees the whole host):'
SELECT pg_size_pretty(sum(pg_database_size(datname))) AS all_databases FROM pg_database;

\echo ''
\echo '=================================================================='
\echo ' 4. buffer cache hit ratio (aim for > 99% on a warm server)'
\echo '=================================================================='
SELECT round(100.0 * sum(blks_hit) / nullif(sum(blks_hit + blks_read), 0), 2) AS cache_hit_pct,
       pg_size_pretty(sum(blks_read) * 8192) AS read_from_disk
FROM pg_stat_database WHERE datname = current_database();

\echo ''
\echo '=================================================================='
\echo ' 5. connections in use (QGIS digitisers + dashboard)'
\echo '=================================================================='
SELECT usename, state, count(*),
       max(now() - state_change)::interval(0) AS idle_longest
FROM pg_stat_activity WHERE datname = current_database()
GROUP BY 1, 2 ORDER BY 3 DESC;

\echo ''
\echo '=================================================================='
\echo ' 6. dashboard speed-up structures present?'
\echo '=================================================================='
SELECT 'farm_point_tile' AS object, to_regclass('coffee.farm_point_tile') IS NOT NULL AS exists
UNION ALL SELECT 'county_stats',   to_regclass('coffee.county_stats')   IS NOT NULL
UNION ALL SELECT 'flag_stats',     to_regclass('coffee.flag_stats')     IS NOT NULL
UNION ALL SELECT 'hist_stats',     to_regclass('coffee.hist_stats')     IS NOT NULL
UNION ALL SELECT 'summary_stats',  to_regclass('coffee.summary_stats')  IS NOT NULL
UNION ALL SELECT 'cluster_stats',  to_regclass('coffee.cluster_stats')  IS NOT NULL
UNION ALL SELECT 'county_geom',    to_regclass('coffee.county_geom')    IS NOT NULL
UNION ALL SELECT 'hex geom3857',   EXISTS (SELECT 1 FROM information_schema.columns
                                           WHERE table_schema='coffee' AND table_name='farm_point_hex'
                                             AND column_name='geom3857');

\echo ''
\echo ' tile table in step with the QC table?'
SELECT (SELECT count(*) FROM coffee.farm_point_tile) AS tile_rows,
       (SELECT count(*) FROM coffee.farm_point_qc WHERE geom IS NOT NULL) AS qc_rows;
SQL

echo
echo "=================================================================="
echo " 7. dashboard response cache (needs the dashboard container up)"
echo "=================================================================="
curl -fsS http://127.0.0.1:5055/api/cache 2>/dev/null || echo " (dashboard not reachable on 127.0.0.1:5055)"
echo
