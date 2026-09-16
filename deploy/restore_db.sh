#!/usr/bin/env bash
# Recreate coffee_eudr inside the Postgres/PostGIS docker container.
# Usage: ./restore_on_server.sh [container_name]   (default: coffee-pg)
set -euo pipefail
C="${1:-coffee-pg}"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== copying dumps into container $C"
docker cp "$HERE/01_roles.sql"       "$C:/tmp/01_roles.sql"
docker cp "$HERE/02_coffee_eudr.dump" "$C:/tmp/02_coffee_eudr.dump"
docker cp "$HERE/03_grants.sql"      "$C:/tmp/03_grants.sql"

echo "== creating roles (coffee_reader / coffee_editor, same passwords as local)"
docker exec -i "$C" psql -U postgres -v ON_ERROR_STOP=0 -f /tmp/01_roles.sql

echo "== creating database + postgis"
docker exec -i "$C" psql -U postgres -c "DROP DATABASE IF EXISTS coffee_eudr;"
docker exec -i "$C" psql -U postgres -c "CREATE DATABASE coffee_eudr;"
docker exec -i "$C" psql -U postgres -d coffee_eudr -c "CREATE EXTENSION IF NOT EXISTS postgis;"

echo "== restoring data (several minutes for ~4 GB)"
docker exec -i "$C" pg_restore -U postgres -d coffee_eudr --no-owner -j 4 /tmp/02_coffee_eudr.dump

echo "== applying grants"
docker exec -i "$C" psql -U postgres -d coffee_eudr -v ON_ERROR_STOP=1 -f /tmp/03_grants.sql

echo "== verifying"
docker exec -i "$C" psql -U postgres -d coffee_eudr -c "
  SELECT 'farm_point' t, count(*) FROM coffee.farm_point
  UNION ALL SELECT 'farm_point_qc', count(*) FROM coffee.farm_point_qc
  UNION ALL SELECT 'farm_point_hex', count(*) FROM coffee.farm_point_hex
  UNION ALL SELECT 'digitized_polygon', count(*) FROM coffee.digitized_polygon
  UNION ALL SELECT 'ref.kenya_counties', count(*) FROM ref.kenya_counties
  UNION ALL SELECT 'ref.roads_lines', count(*) FROM ref.roads_lines;"
docker exec -i "$C" rm -f /tmp/01_roles.sql /tmp/02_coffee_eudr.dump /tmp/03_grants.sql
echo "== done"
