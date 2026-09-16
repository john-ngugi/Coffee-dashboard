#!/usr/bin/env python3
"""
export_qc.py -- copy the QC results out of PostGIS into files (nothing is
deleted from the database; the source coffee.farm_point is untouched).

Writes to  QC_outputs/<timestamp>/
    farm_points_clean.{gpkg,shp,csv}     points that passed every criterion
    farm_points_removed.{gpkg,shp,csv}   flagged points, with the reasons
    farm_points_all_qc.gpkg              everything, with every metric column
    clusters_summary.csv                 one row per DBSCAN cluster
    README.txt                           criteria used + counts

The GeoPackage is the full-fidelity copy (long column names, arrays kept as
text).  The shapefile is provided for older tools; its column names are
truncated to 10 characters by the format.
"""
import os, subprocess, datetime, json, importlib.util
from pathlib import Path
import psycopg2

HERE = Path(__file__).resolve().parent
PGBIN = Path(r"C:\Program Files\PostgreSQL\17\bin")
for line in ((HERE / ".env").read_text().splitlines() if (HERE / ".env").exists() else []):  # optional in Docker
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
ENV = dict(os.environ)  # pgsql2shp / psql pick up PGHOST etc. from the environment

OUT = HERE / "QC_outputs" / datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
OUT.mkdir(parents=True, exist_ok=True)

# columns exported (arrays flattened to '; ' text, geom_utm dropped)
COLS = """kobo_id, uuid, county_form, county_raw, county_gis, county_match, in_target_county,
  subcounty, ward, consent_raw, consented, available_raw, available,
  grower_type, grower_name, factory_name, grower_code, other_member_society,
  respondent_name, respondent_id_no, respondent_phone, respondent_email,
  respondent_gender, respondent_age_range, respondent_education,
  estate_name, estate_code, estate_to_society, society_supplied_to,
  parcel_number, trees_total, trees_ruiru11, trees_batian, trees_sl28, trees_sl34,
  trees_k7, trees_robusta, trees_bluemountain, harvest_quantity,
  array_to_string(variety, '; ')       AS variety,
  array_to_string(planting_year, '; ') AS planting_year,
  array_to_string(harvest_month, '; ') AS harvest_month,
  array_to_string(processing, '; ')    AS processing,
  array_to_string(certification, '; ') AS certification,
  interviewer_name, interviewer_id, interviewer_phone, comments,
  submitted_by, submission_time, kobo_status, validation_status, form_version,
  latitude, longitude, coord_valid, qa_flag, ST_AsText(farm_shape) AS farm_shape_wkt,
  round(dist_road_m::numeric, 2) AS dist_road_m, road_type, road_name,
  trees_breakdown_sum, implied_ha, density_used, area_basis,
  cluster_id, cluster_size, dup_group_size, nbrs_50m, polygon_gid, digitised_at,
  array_to_string(flags, '; ') AS flags, array_to_string(warnings, '; ') AS warnings, status, geom"""


def sh(cmd):
    print("  $", " ".join(str(c) for c in cmd[:3]), "...")
    subprocess.run(cmd, check=True, env=ENV)


def export(name, where):
    shp = OUT / f"{name}.shp"
    sh([PGBIN / "pgsql2shp", "-f", shp, "-g", "geom", os.environ["PGDATABASE"],
        f"SELECT {COLS} FROM coffee.farm_point_qc WHERE {where}"])
    # shapefile -> GeoPackage + CSV via ogr2ogr (keeps full column names from the .dbf as truncated;
    # so for gpkg/csv we go from the DB directly through psql COPY where names matter)
    sh([PGBIN / "ogr2ogr", "-f", "GPKG", OUT / f"{name}.gpkg", shp, "-nln", name])
    csv = OUT / f"{name}.csv"
    cols_no_geom = COLS.rsplit(", geom", 1)[0]
    sh([PGBIN / "psql", "-q", "-c",
        f"\\copy (SELECT {cols_no_geom} FROM coffee.farm_point_qc WHERE {where} ORDER BY kobo_id) TO '{csv}' CSV HEADER"])


def main():
    conn = psycopg2.connect(host=os.environ["PGHOST"], port=os.environ["PGPORT"], user=os.environ["PGUSER"],
                            password=os.environ["PGPASSWORD"], dbname=os.environ["PGDATABASE"])
    cur = conn.cursor()
    print("clean");   export("farm_points_clean",   "status = 'clean'")
    print("removed"); export("farm_points_removed", "status = 'removed'")
    print("all");     export("farm_points_all_qc",  "TRUE")

    print("clusters")
    with open(OUT / "clusters_summary.csv", "w", newline="", encoding="utf-8") as f:
        cur.copy_expert("""COPY (SELECT cluster_id, count(*) n_points, count(DISTINCT submitted_by) n_enumerators,
                          min(county_gis) county, mode() WITHIN GROUP (ORDER BY factory_name) factory,
                          round(avg(latitude)::numeric,6) lat, round(avg(longitude)::numeric,6) lon,
                          sum(trees_total) trees
                   FROM coffee.farm_point_qc WHERE cluster_id IS NOT NULL
                   GROUP BY cluster_id ORDER BY n_points DESC) TO STDOUT CSV HEADER""", f)

    spec = importlib.util.spec_from_file_location("build_qc", HERE / "build_qc.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    cur.execute("SELECT status, count(*) FROM coffee.farm_point_qc GROUP BY 1")
    status = dict(cur.fetchall())
    cur.execute("SELECT f, count(*) FROM coffee.farm_point_qc, unnest(flags) f GROUP BY 1 ORDER BY 2 DESC")
    flags = cur.fetchall()
    cur.execute("SELECT f, count(*) FROM coffee.farm_point_qc, unnest(warnings) f GROUP BY 1 ORDER BY 2 DESC")
    warnings = cur.fetchall()
    (OUT / "README.txt").write_text(
        f"Coffee farm point QC export  {datetime.datetime.now():%Y-%m-%d %H:%M}\n"
        f"Source: coffee.farm_point (unchanged)  ->  coffee.farm_point_qc\n\n"
        f"Criteria:\n{json.dumps(mod.CRITERIA, indent=2)}\n\n"
        f"Counts:\n  clean   {status.get('clean',0):>10,}\n  removed {status.get('removed',0):>10,}\n\n"
        "Flags (a point may carry several; any flag => removed):\n" + "".join(f"  {f:24s}{n:>10,}\n" for f, n in flags) +
        "\nWarnings (recorded in the 'warnings' column, point kept):\n" + "".join(f"  {f:24s}{n:>10,}\n" for f, n in warnings) +
        "\nFiles: *_clean = passed every criterion; *_removed = at least one flag (see 'flags' column);\n"
        "*_all_qc = every located point with all metric columns.  CRS: EPSG:4326.\n")
    print("\nwritten to", OUT)
    for p in sorted(OUT.iterdir()): print(f"  {p.name:36s} {p.stat().st_size/1e6:8.1f} MB")


if __name__ == "__main__":
    main()
