#!/usr/bin/env python3
"""
dashboard.py -- local QC dashboard for coffee.farm_point_qc.

Serves vector tiles straight out of PostGIS (ST_AsMVT) so all ~386k points
render without preloading, plus summary / histogram endpoints for the sidebar.

    python dashboard.py            # http://localhost:5055
"""
import os, json, time, select, gzip, hashlib, functools, secrets
from datetime import timedelta
from pathlib import Path
from flask import Flask, Response, request, jsonify, send_file, stream_with_context, session, abort
import psycopg2, psycopg2.pool, psycopg2.extensions

HERE = Path(__file__).resolve().parent
for line in ((HERE / ".env").read_text().splitlines() if (HERE / ".env").exists() else []):  # optional in Docker
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

import queue, threading

class BlockingPool:
    """Fixed-size pool that waits for a free connection instead of raising."""
    def __init__(self, n):
        self.q = queue.Queue()
        for _ in range(n):
            self.q.put(psycopg2.connect(host=os.environ["PGHOST"], port=os.environ["PGPORT"],
                                        user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"],
                                        dbname=os.environ["PGDATABASE"]))
    def getconn(self): return self.q.get()
    def putconn(self, c):
        if c.closed:
            c = psycopg2.connect(host=os.environ["PGHOST"], port=os.environ["PGPORT"], user=os.environ["PGUSER"],
                                 password=os.environ["PGPASSWORD"], dbname=os.environ["PGDATABASE"])
        else:
            c.rollback()
        self.q.put(c)

# Each gunicorn worker keeps its own pool plus one LISTEN connection, and the
# response cache means most requests never reach the database -- so keep this
# small. 19 QGIS digitisers hold several connections each and the server's
# max_connections is the scarce resource, not throughput here.
POOL = BlockingPool(int(os.environ.get("DASH_POOL", "6")))
app = Flask(__name__, static_folder=None)
# The /team login cookie is signed with this. It has to be the SAME value in
# every gunicorn worker and across restarts -- a per-process random key means a
# cookie signed by one worker is rejected by the other, which logs people out
# at random. So: DASH_SECRET if set, otherwise one generated once and kept in
# the database (coffee.app_config, readable only by the dashboard's own login).
# Set in boot(); see session_secret().
app.secret_key = os.environ.get("DASH_SECRET") or "unset-see-boot"
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=int(os.environ.get("DASH_SESSION_DAYS", "7"))),
    SESSION_REFRESH_EACH_REQUEST=True,   # a day's work does not time out mid-shift
)


# ---------------------------------------------------------------- response cache
# Nothing here changes on a timer: the QC numbers change when build_qc.py runs,
# the polygons when a digitiser saves, and the LISTEN thread below hears both.
# So every answer is cached in memory under a version stamp and a change bumps
# that stamp -- the first viewer pays for the query, everyone else is served
# from RAM, and an edit invalidates immediately instead of expiring late.
#
#   qc    QC results: flags, counties, histograms, clusters
#   poly  digitised polygons and the progress figures
#   tile  map tiles (bumped at most once a minute; see bump())
VERSION = {"qc": 1, "poly": 1, "tile": 1}
CACHE = {}                       # (group, name, key) -> (bytes, etag, content_type)
CACHE_LOCK = threading.Lock()
CACHE_MAX_BYTES = int(os.environ.get("DASH_CACHE_MB", "192")) * 1024 * 1024
_cache_bytes = 0
_last_bump = {}
STATS = {"hits": 0, "misses": 0}


def bump(group, min_interval=0.0):
    """Invalidate a cache group. min_interval coalesces floods (polygon saves)."""
    global _cache_bytes
    now = time.time()
    if min_interval and now - _last_bump.get(group, 0) < min_interval:
        return False
    with CACHE_LOCK:
        _last_bump[group] = now
        VERSION[group] = VERSION.get(group, 0) + 1
        for k in [k for k in CACHE if k[0] == group]:
            _cache_bytes -= len(CACHE.pop(k)[0])
    return True


def cache_get(group, name, key):
    with CACHE_LOCK:
        hit = CACHE.get((group, name, key))
        STATS["hits" if hit else "misses"] += 1
        return hit


def cache_put(group, name, key, payload, ctype):
    global _cache_bytes
    etag = '"%s"' % hashlib.md5(payload).hexdigest()[:16]
    with CACHE_LOCK:
        if _cache_bytes > CACHE_MAX_BYTES:          # simple flush, not an LRU:
            CACHE.clear(); _cache_bytes = 0         # the working set is small
        CACHE[(group, name, key)] = (payload, etag, ctype)
        _cache_bytes += len(payload)
    return etag


def respond(payload, etag, ctype):
    """Serve bytes with an ETag, a 304 when the browser already has them, and
    gzip when it is worth it (GeoJSON compresses ~6x, MVT ~1.5x)."""
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    body = payload
    if len(payload) > 1400 and "gzip" in request.headers.get("Accept-Encoding", ""):
        body = gzip.compress(payload, 5)
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return Response(body, mimetype=ctype, headers=headers)


def cached(group, ctype="application/json", vary=()):
    """Cache a view's body under `group`, keyed by its arguments."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            key = (a, tuple(sorted(kw.items())), tuple(request.args.get(v) for v in vary))
            hit = cache_get(group, fn.__name__, key)
            if hit is None:
                out = fn(*a, **kw)
                payload = out if isinstance(out, (bytes, bytearray)) else json.dumps(out).encode()
                payload = bytes(payload)
                etag = cache_put(group, fn.__name__, key, payload, ctype)
                hit = (payload, etag, ctype)
            return respond(*hit)
        return wrapper
    return deco


@app.get("/api/cache")
def cache_stats():
    total = STATS["hits"] + STATS["misses"]
    return jsonify(dict(entries=len(CACHE), bytes=_cache_bytes, versions=VERSION,
                        hits=STATS["hits"], misses=STATS["misses"],
                        hit_rate=round(100.0 * STATS["hits"] / total, 1) if total else None))


# ---------------------------------------------------------------- live updates
# Statement-level triggers call pg_notify('coffee_changes', ...) whenever the
# QC table or the digitised polygons change (QGIS edits, build_qc.py runs, ...).
# A listener thread relays those to every open browser tab over Server-Sent
# Events; the page reloads only the layer that changed.
NOTIFY_SQL = """
CREATE OR REPLACE FUNCTION coffee.notify_change() RETURNS trigger AS $$
DECLARE tbl text := TG_TABLE_NAME;
BEGIN
    -- a polygon save also stamps farm_point_qc (coverage); report that as
    -- 'farm_point_track' so dashboards refresh the polygons only, not everything
    IF tbl = 'farm_point_qc' AND coalesce(current_setting('coffee.tracking', true), '') = 'on' THEN
        tbl := 'farm_point_track';
    END IF;
    PERFORM pg_notify('coffee_changes',
        json_build_object('table', tbl, 'op', TG_OP, 'at', now())::text);
    RETURN NULL;
END $$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER notify_change AFTER INSERT OR UPDATE OR DELETE
    ON coffee.digitized_polygon FOR EACH STATEMENT EXECUTE FUNCTION coffee.notify_change();
CREATE OR REPLACE TRIGGER notify_change AFTER INSERT OR UPDATE OR DELETE
    ON coffee.farm_point_qc FOR EACH STATEMENT EXECUTE FUNCTION coffee.notify_change();
CREATE OR REPLACE TRIGGER notify_change AFTER INSERT OR UPDATE OR DELETE
    ON coffee.farm_point_hex FOR EACH STATEMENT EXECUTE FUNCTION coffee.notify_change();
"""
SUBSCRIBERS: set = set()
SUB_LOCK = threading.Lock()


def _pg_connect():
    return psycopg2.connect(host=os.environ["PGHOST"], port=os.environ["PGPORT"], user=os.environ["PGUSER"],
                            password=os.environ["PGPASSWORD"], dbname=os.environ["PGDATABASE"])


def session_secret():
    """One signing key shared by every worker, kept across restarts.

    Without this each gunicorn worker invents its own key at import, so a
    session cookie signed by worker 1 fails in worker 2 and the person is
    thrown back to the login screen on roughly half their clicks.
    """
    env = os.environ.get("DASH_SECRET")
    if env:
        return env
    c = _pg_connect()
    c.autocommit = True
    try:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS coffee.app_config (
                    key        text PRIMARY KEY,
                    value      text NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now());
                REVOKE ALL ON coffee.app_config FROM PUBLIC;""")
            cur.execute("""INSERT INTO coffee.app_config (key, value) VALUES ('session_secret', %s)
                           ON CONFLICT (key) DO NOTHING""", (secrets.token_hex(32),))
            cur.execute("SELECT value FROM coffee.app_config WHERE key = 'session_secret'")
            return cur.fetchone()[0]
    finally:
        c.close()


def ensure_perf_objects():
    """Create the speed-up structures (deploy/sql/perf.sql) if they are missing,
    so a fresh deployment needs no manual migration step."""
    sql_file = HERE / "deploy" / "sql" / "perf.sql"
    if not sql_file.exists():
        return
    c = _pg_connect()
    c.autocommit = True          # perf.sql manages its own BEGIN/COMMIT
    try:
        body = sql_file.read_text(encoding="utf-8")
        want = "perf v" + (body.split("-- PERF VERSION:")[1].split("\n")[0].strip()
                           if "-- PERF VERSION:" in body else "1")
        with c.cursor() as cur:
            cur.execute("""SELECT obj_description(to_regclass('coffee.farm_point_tile'))""")
            have = cur.fetchone()[0]
            if have != want:
                print(f"applying deploy/sql/perf.sql ({have or 'nothing'} -> {want}) ...", flush=True)
                cur.execute(body)
                print("perf structures up to date", flush=True)
    except Exception as e:
        print("perf.sql skipped:", e, flush=True)
    finally:
        c.close()


def ensure_triggers():
    c = _pg_connect()
    try:
        with c.cursor() as cur:
            cur.execute(NOTIFY_SQL)
        c.commit()
    finally:
        c.close()


def listener():
    """Dedicated connection: LISTEN and fan out to subscriber queues.  Reconnects on error."""
    while True:
        try:
            c = _pg_connect()
            c.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            cur = c.cursor(); cur.execute("LISTEN coffee_changes")
            while True:
                if select.select([c], [], [], 5) == ([], [], []):
                    continue
                c.poll()
                while c.notifies:
                    n = c.notifies.pop(0)
                    try:
                        tbl = json.loads(n.payload).get("table", "")
                    except Exception:
                        tbl = ""
                    # a polygon save changes the polygons and the progress figures
                    # at once; the map tiles only carry a "covered" ring, so those
                    # are refreshed at most once a minute rather than on every save
                    if tbl in ("digitized_polygon", "digitized_polygon_history", "farm_point_track"):
                        bump("poly")
                        bump("tile", min_interval=60)
                    else:
                        bump("qc"); bump("poly"); bump("tile")
                    with SUB_LOCK:
                        for q in list(SUBSCRIBERS):
                            q.put(n.payload)
        except Exception as e:
            print("listener error:", e, "- reconnecting in 3s", flush=True)
            time.sleep(3)


@app.get("/events")
def events():
    q = queue.Queue()
    with SUB_LOCK:
        SUBSCRIBERS.add(q)

    @stream_with_context
    def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    msg = q.get(timeout=15)
                    # collapse a burst (one build_qc run fires many statements) into one event
                    burst = [msg]
                    t_end = time.time() + 0.8
                    while time.time() < t_end:
                        try: burst.append(q.get(timeout=max(0.01, t_end - time.time())))
                        except queue.Empty: break
                    tables = sorted({json.loads(b)["table"] for b in burst})
                    yield f"event: change\ndata: {json.dumps(dict(tables=tables, n=len(burst)))}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with SUB_LOCK:
                SUBSCRIBERS.discard(q)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


FLAGS = ["outside_kenya", "outside_target_county", "on_road", "trees_high",
         "trees_zero", "over_4ha", "dense_cluster", "dup_coord"]


def query(sql, params=None, one=False):
    conn = POOL.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone() if one else cur.fetchall()
    finally:
        POOL.putconn(conn)


def point_filter(args):
    """Translate ?status=&flag=&county= into a SQL WHERE fragment + params."""
    where, params = ["TRUE"], []
    status = args.get("status", "all")
    if status in ("clean", "removed"):
        where.append("status = %s"); params.append(status)
    flag = args.get("flag")
    if flag in FLAGS:
        where.append("%s = ANY(flags)"); params.append(flag)
    county = args.get("county")
    if county:
        where.append("county_gis = %s"); params.append(county)
    dig = args.get("digitised")
    if dig == "yes":
        where.append("polygon_gid IS NOT NULL")
    elif dig == "no":
        where.append("polygon_gid IS NULL")
    return " AND ".join(where), params


# ---------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_file(HERE / "dashboard.html")


@app.get("/api/config")
def config():
    # basemap key stays in .env; the page asks for it at runtime
    return jsonify(dict(carto_key=os.environ.get("CARTO_KEY", ""),
                        maptiler_key=os.environ.get("MAPTILER_KEY", "")))


# ---------------------------------------------------------------- tiles
# Below CLUSTER_ZOOM the map draws one circle per grid cell (counts aggregated
# from the 1 km hex grid: 8k rows instead of 385k points -- a z9 tile is 15 KB
# instead of 3.4 MB). At and above it, individual points come from
# coffee.farm_point_tile, a narrow copy holding only what the map draws.
CLUSTER_ZOOM = int(os.environ.get("DASH_CLUSTER_ZOOM", "12"))


def tile_filter(args):
    """WHERE fragment for the narrow tile table (same filters as the sidebar)."""
    where, params = ["TRUE"], []
    status = args.get("status", "all")
    if status in ("clean", "removed"):
        where.append("status = %s"); params.append(status)
    flag = args.get("flag")
    if flag in FLAGS:
        where.append("%s = ANY(flags)"); params.append(flag)
    county = args.get("county")
    if county:
        where.append("county = %s"); params.append(county)
    dig = args.get("digitised")
    if dig == "yes":
        where.append("covered")
    elif dig == "no":
        where.append("NOT covered")
    return " AND ".join(where), params


@app.get("/tiles/points/<int:z>/<int:x>/<int:y>.pbf")
@cached("tile", "application/x-protobuf", vary=("status", "flag", "county", "digitised"))
def points_tile(z, x, y):
    where, params = tile_filter(request.args)
    sql = f"""
      WITH b AS (SELECT ST_TileEnvelope(%s,%s,%s) AS g),
      m AS (
        SELECT kobo_id, status, flag, covered::int AS covered,
               ST_AsMVTGeom(ST_Transform(t.geom,3857), b.g, 4096, 16, true) AS geom
        FROM coffee.farm_point_tile t, b
        WHERE t.geom && ST_Transform(b.g, 4326) AND {where})
      SELECT ST_AsMVT(m, 'points', 4096, 'geom') FROM m"""
    row = query(sql, [z, x, y] + params, one=True)
    return bytes(row[0]) if row and row[0] else b""


@app.get("/tiles/clusters/<int:z>/<int:x>/<int:y>.pbf")
@cached("tile", "application/x-protobuf", vary=("g",))
def clusters_tile(z, x, y):
    """One weighted dot per grid cell, from the hex grid -- the low-zoom view.
    ?g=N splits the tile into an NxN grid (N cells across 256 px), so a smaller
    N means fewer, larger circles."""
    try:
        grid = min(64, max(4, int(request.args.get("g", 12))))
    except ValueError:
        grid = 12
    sql = f"""
      WITH b AS (SELECT ST_TileEnvelope(%s,%s,%s) AS g),
      c AS (
        SELECT ST_SnapToGrid(h.geom3857, (SELECT (ST_XMax(g)-ST_XMin(g))/{grid} FROM b)) cell,
               sum(n_all) n, sum(n_clean) n_ok, sum(n_removed) n_bad,
               ST_Centroid(ST_Collect(h.geom3857)) g3
        FROM coffee.farm_point_hex h, b
        WHERE h.geom3857 && b.g GROUP BY 1),
      m AS (
        SELECT n, n_ok, n_bad, ST_AsMVTGeom(g3, (SELECT g FROM b), 4096, 0, true) AS geom
        FROM c)
      SELECT ST_AsMVT(m, 'clusters', 4096, 'geom') FROM m"""
    row = query(sql, [z, x, y], one=True)
    return bytes(row[0]) if row and row[0] else b""


@app.get("/tiles/hex/<int:z>/<int:x>/<int:y>.pbf")
@cached("tile", "application/x-protobuf", vary=("metric",))
def hex_tile(z, x, y):
    metric = request.args.get("metric", "n_all")
    if metric not in ("n_all", "n_clean", "n_removed", "n_cluster"):
        metric = "n_all"
    sql = f"""
      WITH b AS (SELECT ST_TileEnvelope(%s,%s,%s) AS g),
      m AS (
        SELECT n_all, n_clean, n_removed, n_cluster, {metric} AS v,
               ST_AsMVTGeom(ST_Transform(h.geom,3857), b.g, 4096, 8, true) AS geom
        FROM coffee.farm_point_hex h, b
        WHERE h.geom && ST_Transform(b.g, 4326))
      SELECT ST_AsMVT(m, 'hex', 4096, 'geom') FROM m"""
    row = query(sql, [z, x, y], one=True)
    return bytes(row[0]) if row and row[0] else b""


@app.get("/tiles/polygons/<int:z>/<int:x>/<int:y>.pbf")
@cached("poly", "application/x-protobuf")
def polygons_tile(z, x, y):
    """Digitised polygons as tiles, with the counts already cached on the row."""
    sql = """
      WITH b AS (SELECT ST_TileEnvelope(%s,%s,%s) AS g),
      m AS (
        SELECT gid, n_points, n_clean, area_ha::float8 AS area_ha,
               ST_AsMVTGeom(ST_Transform(d.geom,3857), b.g, 4096, 16, true) AS geom
        FROM coffee.digitized_polygon d, b
        WHERE d.geom && ST_Transform(b.g, 4326))
      SELECT ST_AsMVT(m, 'polygons', 4096, 'geom') FROM m"""
    row = query(sql, [z, x, y], one=True)
    return bytes(row[0]) if row and row[0] else b""


# ---------------------------------------------------------------- data
@app.get("/api/counties")
@cached("qc")
def counties():
    rows = query("""
      SELECT g.county, g.gj, coalesce(s.n,0), coalesce(s.clean,0), coalesce(s.target,false)
      FROM coffee.county_geom g
      LEFT JOIN coffee.county_stats s ON s.county = g.county
      ORDER BY 1""")
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": gj,
         "properties": {"name": n, "n": cnt, "n_clean": nc, "target": t}}
        for n, gj, cnt, nc, t in rows]}


@app.get("/api/polygons")
@cached("poly", vary=("bbox",))
def polygons():
    """Digitised polygons with their cached point counts. ?bbox=w,s,e,n limits
    the answer to the viewport; without it every polygon is returned."""
    where, params = "TRUE", []
    bbox = request.args.get("bbox")
    if bbox:
        try:
            w, so, e, no = [float(v) for v in bbox.split(",")][:4]
            where = "d.geom && ST_MakeEnvelope(%s,%s,%s,%s,4326)"; params = [w, so, e, no]
        except ValueError:
            pass
    rows = query(f"""
      SELECT d.gid, d.id, d.name, d.notes, d.source || ' · ' || d.created_by, d.area_ha, d.created_at::date,
             ST_AsGeoJSON(d.geom)::json, d.n_points, d.n_clean, d.trees_clean
      FROM coffee.digitized_polygon d WHERE {where} ORDER BY d.gid""", params)
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": g,
         "properties": {"gid": gid, "id": id_, "name": nm, "notes": nt, "source": src,
                        "area_ha": float(a) if a is not None else None, "created": str(cr),
                        "n": n, "n_clean": nc, "trees_clean": int(t)}}
        for gid, id_, nm, nt, src, a, cr, g, n, nc, t in rows]}


@app.get("/api/progress")
@cached("poly")
def progress():
    """Digitising progress: polygons done, points covered, per day and per county."""
    tot = query("""SELECT count(*), coalesce(round(sum(area_ha),1),0), count(DISTINCT created_by),
                          min(created_at)::date, max(created_at)::date
                   FROM coffee.digitized_polygon""", one=True)
    # Coverage is counted live: it moves with every polygon save, so a
    # materialised view would report yesterday's number while the polygon
    # count kept rising. The partial index on (county) WHERE covered keeps
    # this to a few hundred rows.
    cov = query("""SELECT count(*) FILTER (WHERE status = 'clean'), count(*)
                   FROM coffee.farm_point_tile WHERE covered""", one=True)
    clean_total = query("SELECT sum(clean) FROM coffee.county_stats", one=True)[0]
    pts = (clean_total, cov[0], cov[1])
    days = query("""SELECT created_at::date, count(*), round(sum(area_ha),1), count(DISTINCT created_by)
                    FROM coffee.digitized_polygon GROUP BY 1 ORDER BY 1 DESC LIMIT 30""")
    who = query("""SELECT coalesce(m.full_name, d.created_by), count(*), round(sum(area_ha),1), max(created_at)
                   FROM coffee.digitized_polygon d LEFT JOIN coffee.team_member m ON m.login = d.created_by
                   GROUP BY 1 ORDER BY 2 DESC""") if has_table("coffee.team_member") else \
          query("""SELECT created_by, count(*), round(sum(area_ha),1), max(created_at)
                   FROM coffee.digitized_polygon GROUP BY 1 ORDER BY 2 DESC""")
    counties = query("""
      SELECT s.county, s.clean, c.clean_covered
      FROM coffee.county_stats s
      JOIN (SELECT county, count(*) FILTER (WHERE status = 'clean') AS clean_covered
            FROM coffee.farm_point_tile WHERE covered GROUP BY county) c ON c.county = s.county
      WHERE s.target AND c.clean_covered > 0 ORDER BY c.clean_covered DESC""")
    empty = query("SELECT count(*) FROM coffee.digitized_polygon WHERE n_points = 0", one=True)
    return dict(
        polygons=tot[0], area_ha=float(tot[1]), editors=tot[2], first=str(tot[3]), last=str(tot[4]),
        clean=int(pts[0] or 0), clean_covered=int(pts[1] or 0), any_covered=int(pts[2] or 0),
        empty_polygons=empty[0],
        days=[dict(day=str(d), n=n, ha=float(h), editors=e) for d, n, h, e in days],
        who=[dict(user=u, n=n, ha=float(h), last=str(l)[:16]) for u, n, h, l in who],
        counties=[dict(county=c, clean=n, covered=k, pct=(100.0 * k / n if n else 0)) for c, n, k in counties],
    )


def has_table(name):
    return bool(query("SELECT to_regclass(%s)", (name,), one=True)[0])


# ---------------------------------------------------------------- team portal
# /team: each person signs in with their own database login (deploy/sql/team.sql,
# deploy/make_team_credentials.py) and sees their digitising stats; supervisors
# see their team and can flag / undo. Actions run as the signed-in role
# (SET LOCAL ROLE) so the database enforces who may undo, and coffee.actor
# makes the history name the person rather than the dashboard's connection.
@app.errorhandler(401)
def _unauth(e): return jsonify(error="sign in first"), 401


@app.errorhandler(403)
def _forbidden(e): return jsonify(error="not allowed"), 403


@app.get("/team")
def team_page():
    return send_file(HERE / "team.html")


def team_user():
    u = session.get("user")
    if not u:
        abort(401)
    m = query("""SELECT login, full_name, title, role_group, team_lead, can_undo, can_edit, sees_all
                 FROM coffee.team_member_access WHERE login = %s AND active""", (u,), one=True)
    if not m:
        session.clear(); abort(401)
    return dict(zip(("login", "name", "title", "group", "lead", "can_undo", "can_edit", "sees_all"), m))


@app.post("/api/team/login")
def team_login():
    d = request.get_json(force=True) or {}
    login, pw = (d.get("user") or "").strip().lower(), d.get("password") or ""
    if not login or not pw or not has_table("coffee.team_member"):
        return jsonify(error="missing login or password"), 400
    try:
        psycopg2.connect(host=os.environ["PGHOST"], port=os.environ["PGPORT"], user=login, password=pw,
                         dbname=os.environ["PGDATABASE"], connect_timeout=5).close()
    except psycopg2.OperationalError:
        return jsonify(error="wrong login or password"), 401
    session.permanent = True      # outlives closing the browser tab
    session["user"] = login
    try:
        return jsonify(team_user())
    except Exception:
        session.clear(); return jsonify(error="login is valid but not in the team roster"), 403


@app.post("/api/team/logout")
def team_logout():
    session.clear(); return jsonify(ok=True)


@app.get("/api/team/me")
def team_me():
    return jsonify(team_user())


def visible_logins(me):
    """Which members this user may see: everyone, their own team, or just themselves."""
    if me["sees_all"]:
        return [r[0] for r in query("SELECT login FROM coffee.team_member WHERE active ORDER BY seq")]
    if me["group"] == "qc":
        return [r[0] for r in query("SELECT login FROM coffee.team_member WHERE active AND (login = %s OR team_lead = %s) ORDER BY seq",
                                    (me["login"], me["login"]))]
    return [me["login"]]


@app.get("/api/team/stats")
def team_stats():
    """Per-member digitising stats for the members the signed-in user may see."""
    me = team_user(); logins = visible_logins(me)
    rows = query("""
      WITH poly AS (
        SELECT created_by, count(*) n, round(sum(area_ha),1) ha, max(created_at) last,
               count(*) FILTER (WHERE created_at::date = current_date) today,
               count(*) FILTER (WHERE created_at > now() - interval '7 days') week
        FROM coffee.digitized_polygon WHERE created_by = ANY(%s) GROUP BY 1),
      mv AS (SELECT moved_by, count(*) n FROM coffee.farm_point_move
             WHERE undoes IS NULL AND undone_by IS NULL AND moved_by = ANY(%s) GROUP BY 1),
      fl AS (SELECT changed_by, count(*) n FROM coffee.digitized_polygon_history
             WHERE flagged AND undone_by IS NULL AND changed_by = ANY(%s) GROUP BY 1)
      SELECT m.login, m.full_name, m.title, m.role_group, m.team_lead, l.full_name,
             coalesce(p.n,0), coalesce(p.ha,0), p.last, coalesce(p.today,0), coalesce(p.week,0),
             coalesce(mv.n,0), coalesce(fl.n,0)
      FROM coffee.team_member m
      LEFT JOIN coffee.team_member l ON l.login = m.team_lead
      LEFT JOIN poly p ON p.created_by = m.login
      LEFT JOIN mv ON mv.moved_by = m.login
      LEFT JOIN fl ON fl.changed_by = m.login
      WHERE m.login = ANY(%s) ORDER BY m.seq""", (logins, logins, logins, logins))
    return jsonify(dict(me=me, members=[
        dict(login=a, name=b, title=c, group=g, lead=ld, lead_name=ln, polygons=n, ha=float(ha),
             last=(str(last)[:16] if last else None), today=t, week=w, moved=mv, flagged=f)
        for a, b, c, g, ld, ln, n, ha, last, t, w, mv, f in rows]))


@app.get("/api/team/member/<login>")
def team_member(login):
    """Detail for one member: counties, per-day, recent changes and moves."""
    me = team_user()
    if login not in visible_logins(me):
        abort(403)
    counties = query("""
      SELECT c.counties, count(*), round(sum(d.area_ha),1)
      FROM coffee.digitized_polygon d JOIN ref.kenya_counties c ON ST_Intersects(c.geom, ST_Centroid(d.geom))
      WHERE d.created_by = %s GROUP BY 1 ORDER BY 2 DESC""", (login,))
    days = query("""SELECT created_at::date, count(*), round(sum(area_ha),1)
                    FROM coffee.digitized_polygon WHERE created_by = %s GROUP BY 1 ORDER BY 1 DESC LIMIT 14""", (login,))
    changes = query("""
      SELECT hist_id, gid, op, changed_at, name, coalesce(area_ha_after, area_ha_before), flagged, flag_note, undone, undoes IS NOT NULL,
             ST_Y(ST_Centroid(geom)), ST_X(ST_Centroid(geom))
      FROM coffee.digitized_polygon_activity WHERE changed_by = %s ORDER BY hist_id DESC LIMIT 40""", (login,))
    moves = query("""
      SELECT move_id, kobo_id, moved_at, round(dist_m), undone_by IS NOT NULL, undoes IS NOT NULL,
             ST_Y(geom_after), ST_X(geom_after)
      FROM coffee.farm_point_move WHERE moved_by = %s ORDER BY move_id DESC LIMIT 40""", (login,))
    return jsonify(dict(
        counties=[dict(county=c, n=n, ha=float(h)) for c, n, h in counties],
        days=[dict(day=str(d), n=n, ha=float(h)) for d, n, h in days],
        changes=[dict(id=i, gid=g, op=op, at=str(at)[:16], name=nm, ha=(float(ha) if ha is not None else None),
                      flagged=f, note=note, undone=u, is_undo=iu, lat=lat, lon=lon)
                 for i, g, op, at, nm, ha, f, note, u, iu, lat, lon in changes],
        moves=[dict(id=i, kobo_id=k, at=str(at)[:16], dist=int(d), undone=u, is_undo=iu, lat=lat, lon=lon)
               for i, k, at, d, u, iu, lat, lon in moves]))


def team_filters(me):
    """Shared filter for the team map and its figures: who / date / county / text.
    `who` is always intersected with what this user is allowed to see, so a
    crafted request cannot widen it."""
    asked = [w for w in (request.args.get("who") or "").split(",") if w]
    if me["sees_all"]:
        # a coordinator sees every polygon, including ones saved by an admin
        # login that is not on the roster (the original shapefile import)
        logins = asked or None
    else:
        allowed = visible_logins(me)
        logins = [w for w in asked if w in allowed] or allowed
    where, params = [], []
    if logins is not None:
        where.append("d.created_by = ANY(%s)"); params.append(logins)

    for arg, clause in (("from", "d.created_at >= %s"), ("to", "d.created_at < (%s::date + 1)")):
        v = request.args.get(arg)
        if v:
            where.append(clause); params.append(v)
    county = request.args.get("county")
    if county:
        where.append("d.county = %s"); params.append(county)
    q = (request.args.get("q") or "").strip()
    if q:
        where.append("(d.name ILIKE %s OR d.notes ILIKE %s)"); params += ["%" + q + "%"] * 2
    if request.args.get("flagged") == "1":
        where.append("""EXISTS (SELECT 1 FROM coffee.digitized_polygon_history h
                                WHERE h.gid = d.gid AND h.flagged AND h.undone_by IS NULL)""")
    bbox = request.args.get("bbox")
    if bbox:
        try:
            w, so, e, no = [float(v) for v in bbox.split(",")][:4]
            where.append("d.geom && ST_MakeEnvelope(%s,%s,%s,%s,4326)"); params += [w, so, e, no]
        except ValueError:
            pass
    return (" AND ".join(where) or "TRUE"), params, logins


MAP_LIMIT = 4000


@app.get("/api/team/map")
def team_map():
    """Polygons for the team map, filtered by person, date, county or text."""
    me = team_user()
    where, params, _ = team_filters(me)
    rows = query(f"""
      SELECT d.gid, d.created_by, coalesce(m.full_name, d.created_by), d.created_at,
             d.name, d.county, d.area_ha, d.n_points, d.n_clean,
             EXISTS (SELECT 1 FROM coffee.digitized_polygon_history h
                     WHERE h.gid = d.gid AND h.flagged AND h.undone_by IS NULL) AS flagged,
             ST_AsGeoJSON(d.geom)::json
      FROM coffee.digitized_polygon d
      LEFT JOIN coffee.team_member m ON m.login = d.created_by
      WHERE {where}
      ORDER BY d.created_at DESC LIMIT {MAP_LIMIT + 1}""", params)
    capped = len(rows) > MAP_LIMIT
    return jsonify({"type": "FeatureCollection", "capped": capped, "limit": MAP_LIMIT,
                    "features": [
        {"type": "Feature", "geometry": gj,
         "properties": dict(gid=gid, by=by, by_name=bn, at=str(at)[:16], name=nm, county=co,
                            area_ha=float(ha or 0), n=n, n_clean=nc, flagged=fl)}
        for gid, by, bn, at, nm, co, ha, n, nc, fl, gj in rows[:MAP_LIMIT]]})


@app.get("/api/team/map/summary")
def team_map_summary():
    """The figures beside the map, under exactly the same filters."""
    me = team_user()
    where, params, logins = team_filters(me)
    tot = query(f"""
      SELECT count(*), coalesce(round(sum(d.area_ha),1),0), coalesce(sum(d.n_points),0),
             coalesce(sum(d.n_clean),0), count(DISTINCT d.created_by),
             min(d.created_at)::date, max(d.created_at)::date,
             count(*) FILTER (WHERE d.n_points = 0)
      FROM coffee.digitized_polygon d WHERE {where}""", params, one=True)
    who = query(f"""
      SELECT d.created_by, coalesce(m.full_name, d.created_by), count(*),
             coalesce(round(sum(d.area_ha),1),0), coalesce(sum(d.n_clean),0), max(d.created_at)
      FROM coffee.digitized_polygon d
      LEFT JOIN coffee.team_member m ON m.login = d.created_by
      WHERE {where} GROUP BY 1,2 ORDER BY 3 DESC""", params)
    days = query(f"""SELECT d.created_at::date, count(*), coalesce(round(sum(d.area_ha),1),0)
                     FROM coffee.digitized_polygon d WHERE {where}
                     GROUP BY 1 ORDER BY 1 DESC LIMIT 30""", params)
    counties = query(f"""SELECT coalesce(d.county,'(outside)'), count(*), coalesce(round(sum(d.area_ha),1),0)
                         FROM coffee.digitized_polygon d WHERE {where}
                         GROUP BY 1 ORDER BY 2 DESC""", params)
    return jsonify(dict(
        polygons=tot[0], area_ha=float(tot[1]), points=int(tot[2]), clean=int(tot[3]),
        people=tot[4], first=str(tot[5]) if tot[5] else None, last=str(tot[6]) if tot[6] else None,
        empty=tot[7],
        who=[dict(login=a, name=b, n=n, ha=float(h), clean=int(c), last=str(l)[:16])
             for a, b, n, h, c, l in who],
        days=[dict(day=str(d), n=n, ha=float(h)) for d, n, h in days],
        counties=[dict(county=c, n=n, ha=float(h)) for c, n, h in counties],
    ))


@app.get("/api/team/polygon/<int:gid>")
def team_polygon(gid):
    """One polygon with its recent history -- what the map popup shows, and
    what the flag / undo buttons there act on."""
    me = team_user()
    row = query("""SELECT d.gid, d.created_by, coalesce(m.full_name, d.created_by), d.created_at,
                          d.name, d.notes, d.county, d.area_ha, d.n_points, d.n_clean, d.trees_clean,
                          ST_Y(ST_Centroid(d.geom)), ST_X(ST_Centroid(d.geom))
                   FROM coffee.digitized_polygon d
                   LEFT JOIN coffee.team_member m ON m.login = d.created_by
                   WHERE d.gid = %s""", (gid,), one=True)
    if not row:
        return jsonify(error="no such polygon"), 404
    if not me["sees_all"] and row[1] not in visible_logins(me):
        abort(403)
    hist = query("""SELECT a.hist_id, a.op, a.changed_at, a.changed_by,
                           coalesce(m.full_name, a.changed_by), a.flagged, a.flag_note,
                           a.flagged_by, a.undone, a.undoes IS NOT NULL
                    FROM coffee.digitized_polygon_activity a
                    LEFT JOIN coffee.team_member m ON m.login = a.changed_by
                    WHERE a.gid = %s ORDER BY a.hist_id DESC LIMIT 10""", (gid,))
    return jsonify(dict(
        gid=row[0], by=row[1], by_name=row[2], at=str(row[3])[:16], name=row[4], notes=row[5],
        county=row[6], area_ha=float(row[7] or 0), n=row[8], n_clean=row[9],
        trees_clean=int(row[10] or 0), lat=row[11], lon=row[12],
        can_undo=me["can_undo"],
        history=[dict(id=h, op=op, at=str(at)[:16], by=by, by_name=bn, flagged=f, note=note,
                      flagged_by=fb, undone=u, is_undo=iu)
                 for h, op, at, by, bn, f, note, fb, u, iu in hist]))


@app.get("/api/team/map/options")
def team_map_options():
    """What the filter dropdowns should offer this user."""
    me = team_user()
    logins = visible_logins(me)
    if me["sees_all"]:
        # every login that has saved a polygon, named where it is on the roster
        people = query("""SELECT d.created_by, coalesce(m.full_name, d.created_by),
                                 coalesce(m.title, 'not on the roster'),
                                 count(*), max(d.created_at)::date
                          FROM coffee.digitized_polygon d
                          LEFT JOIN coffee.team_member m ON m.login = d.created_by
                          GROUP BY 1,2,3 ORDER BY count(*) DESC""")
        counties = query("""SELECT county, count(*) FROM coffee.digitized_polygon
                            WHERE county IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""")
        span = query("SELECT min(created_at)::date, max(created_at)::date FROM coffee.digitized_polygon", one=True)
    else:
        people = query("""SELECT m.login, coalesce(m.full_name, m.login), m.title,
                                 count(d.gid), max(d.created_at)::date
                          FROM coffee.team_member m
                          LEFT JOIN coffee.digitized_polygon d ON d.created_by = m.login
                          WHERE m.login = ANY(%s) GROUP BY 1,2,3 ORDER BY m.seq""", (logins,))
        counties = query("""SELECT county, count(*) FROM coffee.digitized_polygon
                            WHERE created_by = ANY(%s) AND county IS NOT NULL
                            GROUP BY 1 ORDER BY 2 DESC""", (logins,))
        span = query("""SELECT min(created_at)::date, max(created_at)::date
                        FROM coffee.digitized_polygon WHERE created_by = ANY(%s)""", (logins,), one=True)
    return jsonify(dict(
        me=me,
        people=[dict(login=a, name=b, title=t, n=n, last=str(l) if l else None)
                for a, b, t, n, l in people],
        counties=[dict(county=c, n=n) for c, n in counties],
        first=str(span[0]) if span[0] else None, last=str(span[1]) if span[1] else None))


@app.get("/api/team/flagged")
def team_flagged():
    me = team_user(); logins = visible_logins(me)
    rows = query("""
      SELECT h.hist_id, h.gid, h.op, h.changed_at, h.changed_by, coalesce(m.full_name, h.changed_by),
             h.name, h.flag_note, h.flagged_by, ST_Y(ST_Centroid(h.geom)), ST_X(ST_Centroid(h.geom))
      FROM coffee.digitized_polygon_activity h LEFT JOIN coffee.team_member m ON m.login = h.changed_by
      WHERE h.flagged AND NOT h.undone AND h.changed_by = ANY(%s) ORDER BY h.hist_id DESC LIMIT 100""", (logins,))
    return jsonify([dict(id=i, gid=g, op=op, at=str(at)[:16], by=by, by_name=bn, name=nm, note=note, flagged_by=fb, lat=lat, lon=lon)
                    for i, g, op, at, by, bn, nm, note, fb, lat, lon in rows])


def run_as(login, sql, params):
    """Execute one statement as the signed-in role; the DB decides if it is allowed."""
    conn = POOL.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL ROLE " + psycopg2.extensions.quote_ident(login, conn))
            cur.execute("SELECT set_config('coffee.actor', %s, true)", (login,))
            cur.execute(sql, params)
            out = cur.fetchone()
        conn.commit()
        return out, None
    except psycopg2.Error as e:
        conn.rollback()
        return None, (e.diag.message_primary or str(e)).strip()
    finally:
        POOL.putconn(conn)


@app.post("/api/team/action")
def team_action():
    me = team_user(); d = request.get_json(force=True) or {}
    what, ident, note = d.get("what"), d.get("id"), d.get("note")
    if what == "flag":
        out, err = run_as(me["login"], "SELECT coffee.flag_change(%s, %s)", (ident, note))
    elif what == "undo":
        out, err = run_as(me["login"], "SELECT coffee.undo_change(%s)", (ident,))
    elif what == "undo_move":
        out, err = run_as(me["login"], "SELECT coffee.undo_point_move(%s)", (ident,))
    else:
        return jsonify(error="unknown action"), 400
    if err:
        return jsonify(error=err), 403 if "permission denied" in err else 400
    return jsonify(ok=True, result=out[0])


@app.get("/api/history")
def history():
    """Recent polygon edits from coffee.digitized_polygon_history (see deploy/sql/polygon_history.sql)."""
    if not query("SELECT to_regclass('coffee.digitized_polygon_activity')", one=True)[0]:
        return jsonify(dict(enabled=False, rows=[], flagged=0))
    lim = min(int(request.args.get("limit", 60)), 500)
    rows = query("""
      SELECT hist_id, gid, op, changed_at, changed_by, host(client_addr), name,
             coalesce(area_ha_after, area_ha_before), flagged, flag_note, undone, undoes,
             ST_Y(ST_Centroid(geom)), ST_X(ST_Centroid(geom))
      FROM coffee.digitized_polygon_activity ORDER BY hist_id DESC LIMIT %s""", (lim,))
    flagged = query("SELECT count(*) FROM coffee.digitized_polygon_history WHERE flagged AND undone_by IS NULL", one=True)[0]
    return jsonify(dict(enabled=True, flagged=flagged, rows=[
        dict(id=h, gid=g, op=op, at=str(at)[:16], by=by, ip=ip, name=n, ha=(float(ha) if ha is not None else None),
             flagged=f, note=note, undone=u, is_undo=(uo is not None), lat=lat, lon=lon)
        for h, g, op, at, by, ip, n, ha, f, note, u, uo, lat, lon in rows]))


@app.get("/api/summary")
@cached("qc")
def summary():
    """Sidebar figures. Every number comes from a materialised view (a few dozen
    rows) rather than a scan of the 1.5 GB QC table."""
    st = query("""SELECT src_total, src_no_coord, src_bad_coord, n, trees_total, trees_clean,
                         trees_median, n_clusters, biggest_cluster FROM coffee.summary_stats""", one=True)
    counties = query("""SELECT county, n, clean, removed, mismatch, target
                        FROM coffee.county_stats ORDER BY n DESC""")
    fl = query("SELECT flag, n, is_warning FROM coffee.flag_stats ORDER BY n DESC")
    status = {"clean": sum(c[2] for c in counties), "removed": sum(c[3] for c in counties)}
    return dict(
        source=dict(total=st[0], no_coordinate=st[1], invalid_coordinate=st[2]),
        status=status,
        flags=[dict(flag=f, n=n) for f, n, w in fl if not w],
        warnings=[dict(flag=f, n=n) for f, n, w in fl if w],
        counties=[dict(county=c, n=n, clean=k, removed=r, mismatch=m, target=t)
                  for c, n, k, r, m, t in counties],
        trees=dict(n=st[3], total=int(st[4] or 0), clean_total=int(st[5] or 0),
                   clean_median=float(st[6]) if st[6] is not None else None),
        clusters=dict(n=st[7], largest=st[8]),
        criteria=criteria(),
    )


@functools.lru_cache(maxsize=1)
def criteria():
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_qc", HERE / "build_qc.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    c = dict(mod.CRITERIA)
    c["target_counties"] = c["target_counties"] or "all counties in data"
    c["warn_only"] = ", ".join(c["warn_only"]) or "none"
    return c


@app.get("/api/hist")
@cached("qc")
def hist():
    """The four sidebar histograms, read from coffee.hist_stats in one query."""
    rows = query("SELECT kind, bin, n, estate FROM coffee.hist_stats")
    got = {(k, b): (n, e) for k, b, n, e in rows}
    ORDER = dict(
        trees=['0','1-49','50-99','100-199','200-499','500-999','1k-2k','2k-5k','5k-10k','10k-100k','100k+'],
        road=['0-2 m','2-5 m','5-10 m','10-20 m','20-50 m','50-100 m','100-500 m','500 m+'],
        cluster=['none','5-9','10-19','20-49','50-99','100+'],
        area=['0','<0.25 ha','0.25-0.5','0.5-1','1-2','2-4','4-10','10-40','40+ ha'])
    def series(kind, with_estate=False):
        out = []
        for b in ORDER[kind]:
            n, e = got.get((kind, b), (0, 0))
            row = dict(bin=b, n=n - e if with_estate else n)
            if with_estate:
                row["estate"] = e
            out.append(row)
        return out
    return dict(trees=series("trees"), road=series("road"),
                cluster=series("cluster"), area=series("area", True))


@app.get("/api/point/<int:kobo_id>")
def point(kobo_id):
    row = query("""
      SELECT kobo_id, status, flags, county_form, county_gis, subcounty, ward,
             grower_type, grower_name, factory_name, grower_code, estate_name,
             trees_total, parcel_number, harvest_quantity, variety, certification,
             implied_ha, density_used, area_basis, trees_breakdown_sum, warnings,
             round(dist_road_m::numeric,1), road_type, road_name,
             cluster_id, cluster_size, dup_group_size, nbrs_50m,
             submitted_by, submission_time, latitude, longitude, polygon_gid, digitised_at,
             respondent_name, respondent_phone, respondent_gender, interviewer_name, interviewer_id, interviewer_phone, comments
      FROM coffee.farm_point_qc WHERE kobo_id=%s""", (kobo_id,), one=True)
    if not row:
        return jsonify({}), 404
    keys = ["kobo_id","status","flags","county_form","county_gis","subcounty","ward",
            "grower_type","grower_name","factory_name","grower_code","estate_name",
            "trees_total","parcel_number","harvest_quantity","variety","certification",
            "implied_ha","density_used","area_basis","trees_breakdown_sum","warnings",
            "dist_road_m","road_type","road_name","cluster_id","cluster_size",
            "dup_group_size","nbrs_50m","submitted_by","submission_time","latitude","longitude","polygon_gid","digitised_at",
            "respondent_name","respondent_phone","respondent_gender","interviewer_name","interviewer_id","interviewer_phone","comments"]
    out = {k: (str(v)[:16] if k in ("submission_time", "digitised_at") and v else v) for k, v in zip(keys, row)}
    out["implied_ha"] = float(out["implied_ha"]) if out["implied_ha"] is not None else None
    return jsonify(out)


@app.get("/api/heat")
@cached("qc", vary=("metric",))
def heat():
    """Hex centroids weighted by count -- feeds the Leaflet.heat layer."""
    metric = request.args.get("metric", "n_all")
    if metric not in ("n_all", "n_clean", "n_removed", "n_cluster"):
        metric = "n_all"
    rows = query(f"""SELECT round(ST_Y(ST_Centroid(geom))::numeric,4),
                            round(ST_X(ST_Centroid(geom))::numeric,4), {metric}
                     FROM coffee.farm_point_hex WHERE {metric} > 0""")
    return [[float(a), float(b), int(c)] for a, b, c in rows]


@app.get("/api/clusters")
@cached("qc")
def clusters():
    """Largest DBSCAN clusters -- for the 'suspicious clusters' table."""
    rows = query("""SELECT cluster_id, n, enumerators, county, lat, lon, factory
                    FROM coffee.cluster_stats ORDER BY n DESC""")
    return [dict(id=a, n=b, enumerators=c, county=d, lat=float(e), lon=float(f), factory=g)
            for a, b, c, d, e, f, g in rows]


# Boot runs at import as well as under `python dashboard.py`, so the LISTEN
# thread and the schema checks also happen when a WSGI server (gunicorn) loads
# this module. It is idempotent: each worker process boots once.
_BOOTED = False
_BOOT_LOCK = threading.Lock()


def boot():
    global _BOOTED
    with _BOOT_LOCK:
        if _BOOTED:
            return
        _BOOTED = True
    try:
        app.secret_key = session_secret()
    except Exception as e:
        print("could not read the shared session secret:", e, flush=True)
    ensure_triggers()
    ensure_perf_objects()
    threading.Thread(target=listener, daemon=True).start()


boot()

if __name__ == "__main__":
    app.run(host=os.environ.get("DASH_HOST", "127.0.0.1"), port=int(os.environ.get("DASH_PORT", "5055")),
            debug=False, threaded=True)
