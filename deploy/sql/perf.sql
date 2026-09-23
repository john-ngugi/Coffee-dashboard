-- perf.sql -- speed-up structures for the dashboard.  Safe to re-run.
-- PERF VERSION: 3
--
--   sudo docker exec -i coffee-pg psql -U postgres -d coffee_eudr < deploy/sql/perf.sql
--
-- 1. coffee.farm_point_tile  a narrow copy of what the map actually draws
--    (~25 MB instead of the 1.5 GB QC table), so building a vector tile reads
--    a fraction of the pages.  Kept in step by the polygon trigger and
--    rebuilt by build_qc.py.
-- 2. point counts cached on coffee.digitized_polygon, so drawing polygons no
--    longer joins 385k points at read time.
-- 3. the polygon trigger maintains both, and tells LISTEN clients that a
--    polygon save is NOT a QC rebuild (so dashboards stop reloading
--    everything each time a digitiser saves).

BEGIN;

-- ---------------------------------------------------------------- 1. tile table
CREATE TABLE IF NOT EXISTS coffee.farm_point_tile (
    kobo_id  integer PRIMARY KEY,
    status   text,
    flag     text,                      -- first flag: the colour the dot gets
    flags    text[],                    -- for the flag filter
    covered  boolean NOT NULL DEFAULT false,
    county   text,
    geom     geometry(Point, 4326) NOT NULL
);

CREATE OR REPLACE FUNCTION coffee.rebuild_point_tile() RETURNS void AS $$
BEGIN
    TRUNCATE coffee.farm_point_tile;
    INSERT INTO coffee.farm_point_tile (kobo_id, status, flag, flags, covered, county, geom)
    SELECT kobo_id, status, flags[1], flags, polygon_gid IS NOT NULL, county_gis, geom
    FROM coffee.farm_point_qc WHERE geom IS NOT NULL;
    ANALYZE coffee.farm_point_tile;
END $$ LANGUAGE plpgsql;

-- populate on first run (and whenever it has fallen behind the QC table)
DO $$
BEGIN
    IF (SELECT count(*) FROM coffee.farm_point_tile) <>
       (SELECT count(*) FROM coffee.farm_point_qc WHERE geom IS NOT NULL) THEN
        PERFORM coffee.rebuild_point_tile();
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS farm_point_tile_geom_idx   ON coffee.farm_point_tile USING gist (geom);
CREATE INDEX IF NOT EXISTS farm_point_tile_status_idx ON coffee.farm_point_tile (status);
CREATE INDEX IF NOT EXISTS farm_point_tile_county_idx ON coffee.farm_point_tile (county);
CREATE INDEX IF NOT EXISTS farm_point_tile_cov_idx    ON coffee.farm_point_tile (covered);
CREATE INDEX IF NOT EXISTS farm_point_tile_flags_idx  ON coffee.farm_point_tile USING gin (flags);
-- covered points are a few hundred out of 385k: a partial index lets the
-- dashboard count them live on every request instead of reading a snapshot
CREATE INDEX IF NOT EXISTS farm_point_tile_covered_idx ON coffee.farm_point_tile (county) WHERE covered;

GRANT SELECT ON coffee.farm_point_tile TO PUBLIC;

-- ------------------------------------------------- 2. cached counts per polygon
ALTER TABLE coffee.digitized_polygon ADD COLUMN IF NOT EXISTS n_points    integer NOT NULL DEFAULT 0;
ALTER TABLE coffee.digitized_polygon ADD COLUMN IF NOT EXISTS n_clean     integer NOT NULL DEFAULT 0;
ALTER TABLE coffee.digitized_polygon ADD COLUMN IF NOT EXISTS trees_clean bigint  NOT NULL DEFAULT 0;
ALTER TABLE coffee.digitized_polygon ADD COLUMN IF NOT EXISTS county      text;
CREATE INDEX IF NOT EXISTS digitized_polygon_by_idx     ON coffee.digitized_polygon (created_by, created_at DESC);
CREATE INDEX IF NOT EXISTS digitized_polygon_county_idx ON coffee.digitized_polygon (county);
CREATE INDEX IF NOT EXISTS digitized_polygon_at_idx     ON coffee.digitized_polygon (created_at DESC);

-- ------------------------------------------------------------ 3. the trigger
-- Replaces the tracking trigger from build_qc.py: same job (stamp the points a
-- polygon covers) plus the tile table, the cached counts, and a quieter NOTIFY.
CREATE OR REPLACE FUNCTION coffee.digitized_polygon_track() RETURNS trigger AS $$
BEGIN
    -- this function writes back to digitized_polygon (the cached counts), which
    -- re-fires this very trigger; ignore that second pass
    IF pg_trigger_depth() > 1 THEN
        RETURN NULL;
    END IF;

    -- mark this transaction so coffee.notify_change() reports 'farm_point_track'
    -- rather than 'farm_point_qc' (a polygon save is not a QC rebuild)
    PERFORM set_config('coffee.tracking', 'on', true);

    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        UPDATE coffee.farm_point_qc SET polygon_gid = NULL, digitised_at = NULL
        WHERE polygon_gid = OLD.gid;
        UPDATE coffee.farm_point_tile t SET covered = false
        WHERE t.geom && OLD.geom AND ST_Intersects(t.geom, OLD.geom);
    END IF;

    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        UPDATE coffee.farm_point_qc SET polygon_gid = NEW.gid, digitised_at = NEW.created_at
        WHERE geom && NEW.geom AND ST_Intersects(geom, NEW.geom);
        UPDATE coffee.farm_point_tile t SET covered = true
        WHERE t.geom && NEW.geom AND ST_Intersects(t.geom, NEW.geom);

        -- cache the counts and the county on the polygon row itself
        UPDATE coffee.digitized_polygon d SET
            n_points    = s.n,
            n_clean     = s.n_clean,
            trees_clean = s.trees,
            county      = (SELECT c.counties FROM ref.kenya_counties c
                           WHERE ST_Intersects(c.geom, ST_Centroid(NEW.geom)) LIMIT 1)
        FROM (SELECT count(*) n,
                     count(*) FILTER (WHERE q.status = 'clean') n_clean,
                     coalesce(sum(q.trees_total) FILTER (WHERE q.status = 'clean'), 0) trees
              FROM coffee.farm_point_qc q
              WHERE q.geom && NEW.geom AND ST_Intersects(q.geom, NEW.geom)) s
        WHERE d.gid = NEW.gid;
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = coffee, public;

DROP TRIGGER IF EXISTS digitized_polygon_track ON coffee.digitized_polygon;
CREATE TRIGGER digitized_polygon_track AFTER INSERT OR UPDATE OR DELETE
    ON coffee.digitized_polygon FOR EACH ROW EXECUTE FUNCTION coffee.digitized_polygon_track();

-- quieter NOTIFY: name the tracking case separately
CREATE OR REPLACE FUNCTION coffee.notify_change() RETURNS trigger AS $$
DECLARE tbl text := TG_TABLE_NAME;
BEGIN
    IF tbl = 'farm_point_qc' AND coalesce(current_setting('coffee.tracking', true), '') = 'on' THEN
        tbl := 'farm_point_track';
    END IF;
    PERFORM pg_notify('coffee_changes',
        json_build_object('table', tbl, 'op', TG_OP, 'at', now())::text);
    RETURN NULL;
END $$ LANGUAGE plpgsql;

-- backfill the cached counts once (a one-off admin write across every polygon,
-- so the bulk guard from team.sql is lifted for this statement only)
SET LOCAL coffee.allow_bulk = 'on';
UPDATE coffee.digitized_polygon d SET n_points = s.n, n_clean = s.n_clean, trees_clean = s.trees
FROM (SELECT d2.gid,
             count(q.kobo_id) n,
             count(q.kobo_id) FILTER (WHERE q.status = 'clean') n_clean,
             coalesce(sum(q.trees_total) FILTER (WHERE q.status = 'clean'), 0) trees
      FROM coffee.digitized_polygon d2
      LEFT JOIN coffee.farm_point_qc q ON q.geom && d2.geom AND ST_Intersects(q.geom, d2.geom)
      GROUP BY d2.gid) s
WHERE s.gid = d.gid;

UPDATE coffee.digitized_polygon d SET county = c.counties
FROM ref.kenya_counties c
WHERE d.county IS NULL AND ST_Intersects(c.geom, ST_Centroid(d.geom));

COMMENT ON TABLE coffee.farm_point_tile IS 'perf v3';

COMMIT;

-- ------------------------------------------------- 4. summary counters (tiny)
-- The sidebar numbers come from one 40-row table instead of eight sequential
-- scans of the QC table.  Refreshed by build_qc.py and by /api/refresh-stats.
-- Only figures that change when build_qc.py runs belong in here. Digitising
-- coverage is NOT one of them -- it changes on every polygon save, so it is
-- counted live from coffee.farm_point_tile (see /api/progress).
DROP MATERIALIZED VIEW IF EXISTS coffee.county_stats CASCADE;
CREATE MATERIALIZED VIEW coffee.county_stats AS
SELECT coalesce(county_gis, '(outside Kenya)') AS county,
       count(*)                                                        AS n,
       count(*) FILTER (WHERE status = 'clean')                        AS clean,
       count(*) FILTER (WHERE status = 'removed')                      AS removed,
       count(*) FILTER (WHERE NOT county_match OR county_match IS NULL) AS mismatch,
       bool_or(in_target_county)                                       AS target,
       sum(trees_total) FILTER (WHERE status = 'clean')                AS trees_clean
FROM coffee.farm_point_qc GROUP BY 1;
CREATE UNIQUE INDEX IF NOT EXISTS county_stats_county_idx ON coffee.county_stats (county);
GRANT SELECT ON coffee.county_stats TO PUBLIC;

CREATE MATERIALIZED VIEW IF NOT EXISTS coffee.flag_stats AS
SELECT f AS flag, count(*) AS n, false AS is_warning
FROM coffee.farm_point_qc, unnest(flags) f GROUP BY f
UNION ALL
SELECT f, count(*), true FROM coffee.farm_point_qc, unnest(warnings) f GROUP BY f;
CREATE UNIQUE INDEX IF NOT EXISTS flag_stats_idx ON coffee.flag_stats (flag, is_warning);
GRANT SELECT ON coffee.flag_stats TO PUBLIC;

CREATE MATERIALIZED VIEW IF NOT EXISTS coffee.hist_stats AS
WITH t AS (
  SELECT 'trees' AS kind,
         CASE WHEN trees_total IS NULL OR trees_total = 0 THEN '0'
              WHEN trees_total < 50 THEN '1-49'      WHEN trees_total < 100 THEN '50-99'
              WHEN trees_total < 200 THEN '100-199'  WHEN trees_total < 500 THEN '200-499'
              WHEN trees_total < 1000 THEN '500-999' WHEN trees_total < 2000 THEN '1k-2k'
              WHEN trees_total < 5000 THEN '2k-5k'   WHEN trees_total < 10000 THEN '5k-10k'
              WHEN trees_total < 100000 THEN '10k-100k' ELSE '100k+' END AS bin,
         grower_type FROM coffee.farm_point_qc
  UNION ALL
  SELECT 'road',
         CASE WHEN dist_road_m <= 2 THEN '0-2 m'    WHEN dist_road_m <= 5 THEN '2-5 m'
              WHEN dist_road_m <= 10 THEN '5-10 m'  WHEN dist_road_m <= 20 THEN '10-20 m'
              WHEN dist_road_m <= 50 THEN '20-50 m' WHEN dist_road_m <= 100 THEN '50-100 m'
              WHEN dist_road_m <= 500 THEN '100-500 m' ELSE '500 m+' END, grower_type
  FROM coffee.farm_point_qc
  UNION ALL
  SELECT 'cluster',
         CASE WHEN cluster_size = 0 THEN 'none'   WHEN cluster_size < 10 THEN '5-9'
              WHEN cluster_size < 20 THEN '10-19' WHEN cluster_size < 50 THEN '20-49'
              WHEN cluster_size < 100 THEN '50-99' ELSE '100+' END, grower_type
  FROM coffee.farm_point_qc
  UNION ALL
  SELECT 'area',
         CASE WHEN implied_ha = 0 THEN '0'          WHEN implied_ha < 0.25 THEN '<0.25 ha'
              WHEN implied_ha < 0.5 THEN '0.25-0.5' WHEN implied_ha < 1 THEN '0.5-1'
              WHEN implied_ha < 2 THEN '1-2'        WHEN implied_ha < 4 THEN '2-4'
              WHEN implied_ha < 10 THEN '4-10'      WHEN implied_ha < 40 THEN '10-40'
              ELSE '40+ ha' END, grower_type
  FROM coffee.farm_point_qc)
SELECT kind, bin, count(*) AS n,
       count(*) FILTER (WHERE grower_type = 'Estate') AS estate
FROM t GROUP BY 1, 2;
CREATE UNIQUE INDEX IF NOT EXISTS hist_stats_idx ON coffee.hist_stats (kind, bin);
GRANT SELECT ON coffee.hist_stats TO PUBLIC;

CREATE OR REPLACE FUNCTION coffee.refresh_stats() RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY coffee.county_stats;
    REFRESH MATERIALIZED VIEW CONCURRENTLY coffee.flag_stats;
    REFRESH MATERIALIZED VIEW CONCURRENTLY coffee.hist_stats;
END $$ LANGUAGE plpgsql;

-- ---------------------------------------- 5. low-zoom cluster source + scalars
-- Web-Mercator centroid of each 1 km hex, so cluster tiles for z < 12 aggregate
-- 8k hexes instead of 385k points.
ALTER TABLE coffee.farm_point_hex ADD COLUMN IF NOT EXISTS geom3857 geometry(Point, 3857);
UPDATE coffee.farm_point_hex SET geom3857 = ST_Transform(ST_Centroid(geom), 3857) WHERE geom3857 IS NULL;
CREATE INDEX IF NOT EXISTS farm_point_hex_3857_idx ON coffee.farm_point_hex USING gist (geom3857);
GRANT SELECT ON coffee.farm_point_hex TO PUBLIC;

CREATE MATERIALIZED VIEW IF NOT EXISTS coffee.summary_stats AS
SELECT (SELECT count(*) FROM coffee.farm_point)                                             AS src_total,
       (SELECT count(*) FROM coffee.farm_point WHERE geom IS NULL)                          AS src_no_coord,
       (SELECT count(*) FROM coffee.farm_point WHERE geom IS NOT NULL AND NOT coord_valid)  AS src_bad_coord,
       count(*)                                                                             AS n,
       sum(trees_total)                                                                     AS trees_total,
       sum(trees_total) FILTER (WHERE status = 'clean')                                     AS trees_clean,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY trees_total)
           FILTER (WHERE status = 'clean')                                                  AS trees_median,
       count(DISTINCT cluster_id) FILTER (WHERE cluster_id IS NOT NULL)                     AS n_clusters,
       max(cluster_size)                                                                    AS biggest_cluster
FROM coffee.farm_point_qc;
GRANT SELECT ON coffee.summary_stats TO PUBLIC;

CREATE MATERIALIZED VIEW IF NOT EXISTS coffee.cluster_stats AS
SELECT cluster_id, count(*) AS n, count(DISTINCT submitted_by) AS enumerators,
       min(county_gis) AS county, round(avg(latitude)::numeric, 5) AS lat,
       round(avg(longitude)::numeric, 5) AS lon,
       mode() WITHIN GROUP (ORDER BY factory_name) AS factory
FROM coffee.farm_point_qc WHERE cluster_id IS NOT NULL
GROUP BY cluster_id ORDER BY n DESC LIMIT 40;
CREATE UNIQUE INDEX IF NOT EXISTS cluster_stats_idx ON coffee.cluster_stats (cluster_id);
GRANT SELECT ON coffee.cluster_stats TO PUBLIC;

CREATE MATERIALIZED VIEW IF NOT EXISTS coffee.county_geom AS
SELECT c.counties AS county,
       ST_AsGeoJSON(ST_SimplifyPreserveTopology(c.geom, 0.002))::json AS gj
FROM ref.kenya_counties c;
CREATE UNIQUE INDEX IF NOT EXISTS county_geom_idx ON coffee.county_geom (county);
GRANT SELECT ON coffee.county_geom TO PUBLIC;

CREATE OR REPLACE FUNCTION coffee.refresh_stats() RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY coffee.county_stats;
    REFRESH MATERIALIZED VIEW CONCURRENTLY coffee.flag_stats;
    REFRESH MATERIALIZED VIEW CONCURRENTLY coffee.hist_stats;
    REFRESH MATERIALIZED VIEW coffee.summary_stats;
    REFRESH MATERIALIZED VIEW CONCURRENTLY coffee.cluster_stats;
    REFRESH MATERIALIZED VIEW CONCURRENTLY coffee.county_geom;
END $$ LANGUAGE plpgsql;
