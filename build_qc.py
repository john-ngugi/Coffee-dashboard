#!/usr/bin/env python3
"""
build_qc.py -- build coffee.farm_point_qc: a QC copy of every located farm point
with per-point cleaning metrics and flags.  The source table coffee.farm_point is
READ ONLY here; nothing is deleted.  Re-run after changing CRITERIA.

Stages
  1. copy points (+ attributes) -> coffee.farm_point_qc, add UTM geometry
  2. county  : spatial join to ref.kenya_counties -> county_gis, in_target_county
  3. roads   : distance to nearest OSM road (ref.roads_lines)  -> dist_road_m
  4. cluster : DBSCAN (eps metres, minpoints) -> cluster_id, cluster_size,
               plus exact-coordinate duplicate groups and 50 m neighbour count
  4b.area    : implied hectares from tree counts x KPCU planting density
  5. flags   : apply CRITERIA -> flags text[], status ('clean' | 'removed')
  6. heat    : hex-bin density grid -> coffee.farm_point_hex (for the heatmap)

Usage:  python build_qc.py            # full rebuild (~10 min)
        python build_qc.py --flags    # re-apply CRITERIA only (~3 min)
        python build_qc.py --hex      # rebuild the heatmap grid only
        python build_qc.py --area     # recompute implied_ha, then flags + hex
"""
import os, sys, time
from pathlib import Path
import psycopg2

HERE = Path(__file__).resolve().parent
for line in ((HERE / ".env").read_text().splitlines() if (HERE / ".env").exists() else []):  # optional in Docker
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

# ----------------------------------------------------------------------------
# CRITERIA -- provisional; edit and re-run with --flags
# ----------------------------------------------------------------------------
def _target_counties():
    f = HERE / "target_counties.txt"
    if not f.exists():
        return None                      # None = keep every county in the data
    return [c.strip() for c in f.read_text().splitlines()
            if c.strip() and not c.startswith("#")]

CRITERIA = dict(
    target_counties   = _target_counties(),
    road_buffer_m     = 2.0,      # point within this distance of a road -> on_road
    max_ha            = 4.0,      # implied area above this (ha), any grower type -> over_4ha
    trees_max_estate  = 100000,   # estate raw tree count above this -> trees_high
    trees_max_society = 100000,   # society raw tree count above this -> trees_high (absurd values)
    trees_min         = 1,        # below this (0 / null) -> trees_zero
    cluster_eps_m     = 10.0,     # DBSCAN neighbourhood radius (metres)
    cluster_minpts    = 5,        # min points to form a cluster
    cluster_flag_size = 5,        # cluster_size >= this -> dense_cluster
    dup_flag_size     = 2,        # identical coordinates shared by >= this many -> dup_coord
    mismatch_frac     = 0.5,      # per-variety sum differs from trees_total by more than this -> trees_mismatch
    warn_only         = ["trees_mismatch"],   # recorded in `warnings`, do NOT remove the point
)
UTM = 32637  # WGS 84 / UTM 37N -- metric CRS for Kenya

# Trees per hectare by variety (New KPCU: Ruiru 11 @ 2x2 m, Batian @ 2.1x2.4 m,
# SL28/SL34/K7 @ 2.74x2.74 m).  Robusta (3x3 m) and Blue Mountain (tall, as SL)
# are not in the KPCU note -- assumptions, flagged in the README.
DENSITY = dict(ruiru11=2500, batian=1905, sl28=1330, sl34=1330, k7=1330,
               robusta=1111, bluemountain=1330)
DENSITY_DEFAULT = 1330   # unknown variety: traditional spacing (largest area => conservative)

# farm_point_qc.polygon_gid <- coffee.digitized_polygon (kept current by trigger)
TRACK_SQL = """
ALTER TABLE coffee.digitized_polygon ADD COLUMN IF NOT EXISTS created_by text NOT NULL DEFAULT current_user;
ALTER TABLE coffee.farm_point_qc ADD COLUMN IF NOT EXISTS polygon_gid integer;
ALTER TABLE coffee.farm_point_qc ADD COLUMN IF NOT EXISTS digitised_at timestamptz;
CREATE INDEX IF NOT EXISTS farm_point_qc_polygon_gid_idx ON coffee.farm_point_qc (polygon_gid);

-- keep farm_point_qc.polygon_gid in step with every polygon edit (row-level, touches
-- only the points inside the old/new shape, so a QGIS save costs milliseconds)
CREATE OR REPLACE FUNCTION coffee.digitized_polygon_track() RETURNS trigger AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        UPDATE coffee.farm_point_qc SET polygon_gid = NULL, digitised_at = NULL
        WHERE polygon_gid = OLD.gid;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        UPDATE coffee.farm_point_qc SET polygon_gid = NEW.gid, digitised_at = NEW.created_at
        WHERE geom && NEW.geom AND ST_Intersects(geom, NEW.geom);
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = coffee, public;
CREATE OR REPLACE TRIGGER digitized_polygon_track AFTER INSERT OR UPDATE OR DELETE
    ON coffee.digitized_polygon FOR EACH ROW EXECUTE FUNCTION coffee.digitized_polygon_track();

-- backfill (idempotent): latest polygon wins where shapes overlap
UPDATE coffee.farm_point_qc q SET polygon_gid = NULL, digitised_at = NULL WHERE polygon_gid IS NOT NULL;
UPDATE coffee.farm_point_qc q SET polygon_gid = d.gid, digitised_at = d.created_at
FROM (SELECT DISTINCT ON (q2.kobo_id) q2.kobo_id, d.gid, d.created_at
      FROM coffee.farm_point_qc q2 JOIN coffee.digitized_polygon d ON q2.geom && d.geom AND ST_Intersects(q2.geom, d.geom)
      ORDER BY q2.kobo_id, d.created_at DESC) d
WHERE d.kobo_id = q.kobo_id;
"""


def connect():
    return psycopg2.connect(host=os.environ["PGHOST"], port=os.environ["PGPORT"],
                            user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"],
                            dbname=os.environ["PGDATABASE"])


def run(cur, label, sql, params=None):
    t = time.time(); cur.execute(sql, params); cur.connection.commit()
    print(f"  [{time.time()-t:6.1f}s] {label}", flush=True)


def stage_copy(cur):
    run(cur, "drop old qc table", "DROP TABLE IF EXISTS coffee.farm_point_qc CASCADE")
    run(cur, "copy points + attributes", f"""
        CREATE TABLE coffee.farm_point_qc AS
        SELECT kobo_id, uuid, county AS county_form, county_raw, subcounty, ward,
               consent_raw, consented, available_raw, available,
               grower_type, grower_name, factory_name, grower_code, other_member_society,
               respondent_name, respondent_id_no, respondent_phone, respondent_email,
               respondent_gender, respondent_age_range, respondent_education,
               estate_name, estate_code, estate_to_society, society_supplied_to,
               parcel_number, trees_total, trees_ruiru11, trees_batian, trees_sl28,
               trees_sl34, trees_k7, trees_robusta, trees_bluemountain,
               harvest_quantity, variety, planting_year, harvest_month, processing, certification,
               interviewer_name, interviewer_id, interviewer_phone, comments,
               submitted_by, submission_time, status AS kobo_status, validation_status, form_version,
               latitude, longitude, coord_valid, qa_flag, farm_shape, geom,
               ST_Transform(geom, {UTM}) AS geom_utm,
               NULL::text        AS county_gis,
               NULL::boolean     AS county_match,
               NULL::boolean     AS in_target_county,
               NULL::real        AS dist_road_m,
               NULL::text        AS road_type,
               NULL::text        AS road_name,
               NULL::integer     AS cluster_id,
               NULL::integer     AS cluster_size,
               NULL::integer     AS dup_group_size,
               NULL::integer     AS nbrs_50m,
               NULL::text[]      AS flags,
               NULL::text        AS status
        FROM coffee.farm_point
        WHERE geom IS NOT NULL AND coord_valid""")
    run(cur, "pk + indexes", """
        ALTER TABLE coffee.farm_point_qc ADD PRIMARY KEY (kobo_id);
        CREATE INDEX farm_point_qc_geom_idx ON coffee.farm_point_qc USING gist (geom);
        CREATE INDEX farm_point_qc_geom_utm_idx ON coffee.farm_point_qc USING gist (geom_utm);
        ANALYZE coffee.farm_point_qc""")


def stage_county(cur):
    run(cur, "spatial join -> county_gis", """
        UPDATE coffee.farm_point_qc q
        SET county_gis = c.counties
        FROM ref.kenya_counties c
        WHERE ST_Intersects(q.geom, c.geom)""")
    # form spelling uses hyphens (Homa-Bay); shapefile uses spaces (Homa Bay)
    run(cur, "county_match", """
        UPDATE coffee.farm_point_qc
        SET county_match = (lower(replace(county_form,'-',' ')) = lower(county_gis))""")


def stage_roads(cur):
    run(cur, "roads: UTM geometry", f"""
        ALTER TABLE ref.roads_lines DROP COLUMN IF EXISTS geom_utm;
        ALTER TABLE ref.roads_lines ADD COLUMN geom_utm geometry(MultiLineString,{UTM});
        UPDATE ref.roads_lines SET geom_utm = ST_Transform(geom, {UTM});
        CREATE INDEX IF NOT EXISTS roads_lines_geom_utm_idx ON ref.roads_lines USING gist (geom_utm);
        ANALYZE ref.roads_lines""")
    run(cur, "nearest road distance (KNN)", """
        UPDATE coffee.farm_point_qc q
        SET dist_road_m = n.d, road_type = n.highway, road_name = n.name
        FROM (
          SELECT q2.kobo_id, r.d, r.highway, r.name
          FROM coffee.farm_point_qc q2
          CROSS JOIN LATERAL (
            SELECT r.highway, r.name, ST_Distance(q2.geom_utm, r.geom_utm) AS d
            FROM ref.roads_lines r
            ORDER BY q2.geom_utm <-> r.geom_utm
            LIMIT 1) r
        ) n
        WHERE n.kobo_id = q.kobo_id""")


def stage_cluster(cur, C):
    run(cur, f"DBSCAN eps={C['cluster_eps_m']}m minpts={C['cluster_minpts']}", """
        UPDATE coffee.farm_point_qc q
        SET cluster_id = s.cid
        FROM (SELECT kobo_id,
                     ST_ClusterDBSCAN(geom_utm, eps := %s, minpoints := %s) OVER () AS cid
              FROM coffee.farm_point_qc) s
        WHERE s.kobo_id = q.kobo_id""", (C['cluster_eps_m'], C['cluster_minpts']))
    run(cur, "cluster_size", """
        UPDATE coffee.farm_point_qc q SET cluster_size = s.n
        FROM (SELECT cluster_id, count(*) n FROM coffee.farm_point_qc
              WHERE cluster_id IS NOT NULL GROUP BY cluster_id) s
        WHERE s.cluster_id = q.cluster_id;
        UPDATE coffee.farm_point_qc SET cluster_size = 0 WHERE cluster_id IS NULL""")
    run(cur, "exact duplicate coordinates", """
        UPDATE coffee.farm_point_qc q SET dup_group_size = s.n
        FROM (SELECT geom, count(*) n FROM coffee.farm_point_qc GROUP BY geom) s
        WHERE s.geom = q.geom""")
    run(cur, "neighbours within 50 m", """
        UPDATE coffee.farm_point_qc q SET nbrs_50m = s.n
        FROM (SELECT a.kobo_id, count(b.kobo_id) n
              FROM coffee.farm_point_qc a
              JOIN coffee.farm_point_qc b
                ON ST_DWithin(a.geom_utm, b.geom_utm, 50) AND a.kobo_id <> b.kobo_id
              GROUP BY a.kobo_id) s
        WHERE s.kobo_id = q.kobo_id;
        UPDATE coffee.farm_point_qc SET nbrs_50m = 0 WHERE nbrs_50m IS NULL""")
    run(cur, "cluster index", "CREATE INDEX IF NOT EXISTS farm_point_qc_cluster_idx ON coffee.farm_point_qc (cluster_id)")


def stage_area(cur):
    """implied_ha = trees_total / density.  Density is the blend of the per-variety
    counts when given (e.g. 60% Ruiru 11 + 40% SL28), else the mean of the
    varieties ticked on the form, else DENSITY_DEFAULT.  trees_total is the
    direct 'number of bushes' answer and is always the count used; the per-
    variety sum is kept alongside it as trees_breakdown_sum."""
    run(cur, "add area columns", """
        ALTER TABLE coffee.farm_point_qc ADD COLUMN IF NOT EXISTS implied_ha numeric(12,3);
        ALTER TABLE coffee.farm_point_qc ADD COLUMN IF NOT EXISTS density_used integer;
        ALTER TABLE coffee.farm_point_qc ADD COLUMN IF NOT EXISTS area_basis text;
        ALTER TABLE coffee.farm_point_qc ADD COLUMN IF NOT EXISTS trees_breakdown_sum bigint""")
    d = DENSITY
    run(cur, "implied hectares", f"""
        WITH v AS (
          SELECT kobo_id, trees_total,
                 coalesce(trees_ruiru11,0) r11, coalesce(trees_batian,0) bat, coalesce(trees_sl28,0) s28,
                 coalesce(trees_sl34,0) s34, coalesce(trees_k7,0) k7, coalesce(trees_robusta,0) rob,
                 coalesce(trees_bluemountain,0) bm,
                 (SELECT avg(CASE x WHEN 'Ruiru 11' THEN {d['ruiru11']} WHEN 'Batian' THEN {d['batian']}
                                     WHEN 'SL-28' THEN {d['sl28']} WHEN 'SL-34' THEN {d['sl34']} WHEN 'K-7' THEN {d['k7']}
                                     WHEN 'Robusta' THEN {d['robusta']} WHEN 'Blue Mountain' THEN {d['bluemountain']} END)
                  FROM unnest(coalesce(variety, ARRAY[]::text[])) x) AS list_density
          FROM coffee.farm_point_qc),
        a AS (
          SELECT kobo_id, trees_total,
                 (r11+bat+s28+s34+k7+rob+bm)::bigint AS bsum,
                 r11/{d['ruiru11']}::numeric + bat/{d['batian']}::numeric + s28/{d['sl28']}::numeric
                 + s34/{d['sl34']}::numeric + k7/{d['k7']}::numeric + rob/{d['robusta']}::numeric + bm/{d['bluemountain']}::numeric AS bha,
                 list_density AS ld
          FROM v),
        b AS (
          SELECT kobo_id, trees_total, bsum,
                 CASE WHEN bsum > 0 AND bha > 0 THEN bsum / bha ELSE coalesce(ld, {DENSITY_DEFAULT}) END AS dens,
                 CASE WHEN bsum > 0 AND bha > 0 THEN 'total/breakdown_mix'
                      WHEN ld IS NOT NULL THEN 'total/variety_list'
                      ELSE 'total/default_density' END AS basis
          FROM a)
        UPDATE coffee.farm_point_qc q
        SET implied_ha = round(coalesce(b.trees_total, 0) / b.dens, 3),
            density_used = round(b.dens),
            area_basis = b.basis,
            trees_breakdown_sum = b.bsum
        FROM b WHERE b.kobo_id = q.kobo_id""")


def stage_flags(cur, C):
    if C["target_counties"] is None:
        run(cur, "in_target_county (all counties kept)",
            "UPDATE coffee.farm_point_qc SET in_target_county = (county_gis IS NOT NULL)")
    else:
        run(cur, f"in_target_county ({len(C['target_counties'])} counties)", """
            UPDATE coffee.farm_point_qc
            SET in_target_county = county_gis IS NOT NULL AND lower(county_gis) = ANY(%s)""",
            ([c.lower().replace('-', ' ') for c in C["target_counties"]],))
    run(cur, "apply flags", """
        ALTER TABLE coffee.farm_point_qc ADD COLUMN IF NOT EXISTS warnings text[];
        UPDATE coffee.farm_point_qc SET flags = ARRAY_REMOVE(ARRAY[
            CASE WHEN county_gis IS NULL            THEN 'outside_kenya' END,
            CASE WHEN county_gis IS NOT NULL AND NOT in_target_county THEN 'outside_target_county' END,
            CASE WHEN dist_road_m <= %(road)s      THEN 'on_road' END,
            CASE WHEN trees_total IS NULL OR trees_total < %(tmin)s THEN 'trees_zero' END,
            CASE WHEN grower_type = 'Estate' AND trees_total > %(test)s THEN 'trees_high' END,
            CASE WHEN grower_type IS DISTINCT FROM 'Estate' AND trees_total > %(tsoc)s THEN 'trees_high' END,
            CASE WHEN implied_ha > %(maxha)s THEN 'over_4ha' END,
            CASE WHEN cluster_size >= %(csize)s    THEN 'dense_cluster' END,
            CASE WHEN dup_group_size >= %(dsize)s  THEN 'dup_coord' END,
            CASE WHEN trees_breakdown_sum > 0
                  AND abs(trees_breakdown_sum - coalesce(trees_total,0)) > greatest(%(mfrac)s * coalesce(trees_total,0), 10)
                 THEN 'trees_mismatch' END
        ], NULL)""", dict(mfrac=C["mismatch_frac"], road=C["road_buffer_m"], tmin=C["trees_min"], test=C["trees_max_estate"],
                          tsoc=C["trees_max_society"], maxha=C["max_ha"], csize=C["cluster_flag_size"], dsize=C["dup_flag_size"]))
    run(cur, "split warnings / status", """
        UPDATE coffee.farm_point_qc
        SET warnings = ARRAY(SELECT f FROM unnest(flags) f WHERE f = ANY(%(warn)s)),
            flags    = ARRAY(SELECT f FROM unnest(flags) f WHERE NOT (f = ANY(%(warn)s)));
        UPDATE coffee.farm_point_qc
        SET status = CASE WHEN cardinality(flags) = 0 THEN 'clean' ELSE 'removed' END;
        CREATE INDEX IF NOT EXISTS farm_point_qc_status_idx ON coffee.farm_point_qc (status);
        CREATE INDEX IF NOT EXISTS farm_point_qc_flags_idx ON coffee.farm_point_qc USING gin (flags);
        ANALYZE coffee.farm_point_qc""", dict(warn=C["warn_only"]))
    run(cur, "views: clean / removed", """
        CREATE OR REPLACE VIEW coffee.farm_point_clean   AS SELECT * FROM coffee.farm_point_qc WHERE status = 'clean';
        CREATE OR REPLACE VIEW coffee.farm_point_removed AS SELECT * FROM coffee.farm_point_qc WHERE status = 'removed'""")
    stage_public_view(cur)


# Columns never exposed to the QGIS roles (same set farm_point_public withholds).
PII_COLUMNS = ("respondent_name", "respondent_id_no", "respondent_phone", "respondent_email",
               "interviewer_name", "interviewer_id", "interviewer_phone", "comments", "raw")


def stage_public_view(cur):
    """coffee.farm_point_clean_public: clean points only, PII stripped, readable by
    coffee_reader / coffee_editor. Rebuilt here because the CASCADE drop in
    stage_copy removes it."""
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_schema = 'coffee' AND table_name = 'farm_point_qc'
                   ORDER BY ordinal_position""")
    cols = ", ".join(c for (c,) in cur.fetchall() if c not in PII_COLUMNS)
    run(cur, "view: clean_public (PII stripped)", f"""
        CREATE OR REPLACE VIEW coffee.farm_point_clean_public AS
        SELECT {cols} FROM coffee.farm_point_qc WHERE status = 'clean';
        GRANT SELECT ON coffee.farm_point_clean_public TO coffee_reader, coffee_editor""")


def stage_hex(cur):
    run(cur, "hex-bin density grid (1 km)", """
        DROP TABLE IF EXISTS coffee.farm_point_hex;
        CREATE TABLE coffee.farm_point_hex AS
        SELECT h.i, h.j,
               ST_Transform(ST_SetSRID(h.geom, 32637), 4326) AS geom,
               count(q.kobo_id)                                   AS n_all,
               count(q.kobo_id) FILTER (WHERE q.status='clean')   AS n_clean,
               count(q.kobo_id) FILTER (WHERE q.status='removed') AS n_removed,
               count(q.kobo_id) FILTER (WHERE 'dense_cluster' = ANY(q.flags)) AS n_cluster,
               sum(q.trees_total) FILTER (WHERE q.status='clean') AS trees_clean
        FROM ST_HexagonGrid(1000, (SELECT ST_SetSRID(ST_Extent(geom_utm), 32637) FROM coffee.farm_point_qc)) h
        JOIN coffee.farm_point_qc q ON ST_Intersects(q.geom_utm, ST_SetSRID(h.geom, 32637))
        GROUP BY h.i, h.j, h.geom;
        CREATE INDEX farm_point_hex_geom_idx ON coffee.farm_point_hex USING gist (geom)""")


def report(cur):
    cur.execute("SELECT status, count(*) FROM coffee.farm_point_qc GROUP BY 1 ORDER BY 1")
    print("\n  status         rows")
    for s, n in cur.fetchall(): print(f"  {s:12s} {n:>10,}")
    cur.execute("SELECT f, count(*) FROM coffee.farm_point_qc, unnest(flags) f GROUP BY 1 ORDER BY 2 DESC")
    print("\n  flag (removes)            rows")
    for f, n in cur.fetchall(): print(f"  {f:24s} {n:>10,}")
    cur.execute("SELECT f, count(*) FROM coffee.farm_point_qc, unnest(warnings) f GROUP BY 1 ORDER BY 2 DESC")
    print("\n  warning (kept)            rows")
    for f, n in cur.fetchall(): print(f"  {f:24s} {n:>10,}")
    cur.execute("""SELECT county_gis, count(*), count(*) FILTER (WHERE status='clean')
                   FROM coffee.farm_point_qc GROUP BY 1 ORDER BY 2 DESC""")
    print("\n  county_gis              total      clean")
    for c, n, k in cur.fetchall(): print(f"  {str(c):18s} {n:>10,} {k:>10,}")


if __name__ == "__main__":
    flags_only = "--flags" in sys.argv
    hex_only = "--hex" in sys.argv
    area_only = "--area" in sys.argv
    conn = connect(); cur = conn.cursor()
    t0 = time.time()
    if not flags_only and not hex_only and not area_only:
        print("1. copy");    stage_copy(cur)
        print("2. county");  stage_county(cur)
        print("3. roads");   stage_roads(cur)
        print("4. cluster"); stage_cluster(cur, CRITERIA)
    if not flags_only and not hex_only:
        print("4b. area");   stage_area(cur)
    if not hex_only:
        print("5. flags");   stage_flags(cur, CRITERIA)
    print("6. hex");     stage_hex(cur)
    report(cur)
    print(f"\nDONE in {time.time()-t0:.0f}s")
