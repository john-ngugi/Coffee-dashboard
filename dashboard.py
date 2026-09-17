#!/usr/bin/env python3
"""
dashboard.py -- local QC dashboard for coffee.farm_point_qc.

Serves vector tiles straight out of PostGIS (ST_AsMVT) so all ~386k points
render without preloading, plus summary / histogram endpoints for the sidebar.

    python dashboard.py            # http://localhost:5055
"""
import os, json, time, select
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

POOL = BlockingPool(8)
app = Flask(__name__, static_folder=None)
# signs the /team login cookie; set DASH_SECRET in .env so logins survive restarts
app.secret_key = os.environ.get("DASH_SECRET") or __import__("secrets").token_hex(32)


# ---------------------------------------------------------------- live updates
# Statement-level triggers call pg_notify('coffee_changes', ...) whenever the
# QC table or the digitised polygons change (QGIS edits, build_qc.py runs, ...).
# A listener thread relays those to every open browser tab over Server-Sent
# Events; the page reloads only the layer that changed.
NOTIFY_SQL = """
CREATE OR REPLACE FUNCTION coffee.notify_change() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('coffee_changes',
        json_build_object('table', TG_TABLE_NAME, 'op', TG_OP, 'at', now())::text);
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
@app.get("/tiles/points/<int:z>/<int:x>/<int:y>.pbf")
def points_tile(z, x, y):
    where, params = point_filter(request.args)
    sql = f"""
      WITH b AS (SELECT ST_TileEnvelope(%s,%s,%s) AS g),
      m AS (
        SELECT kobo_id, status, flags[1] AS flag, cardinality(flags) AS nflags,
               trees_total, grower_type, county_gis, (polygon_gid IS NOT NULL)::int AS covered,
               ST_AsMVTGeom(ST_Transform(q.geom,3857), b.g, 4096, 32, true) AS geom
        FROM coffee.farm_point_qc q, b
        WHERE q.geom && ST_Transform(b.g, 4326) AND {where})
      SELECT ST_AsMVT(m, 'points', 4096, 'geom') FROM m"""
    row = query(sql, [z, x, y] + params, one=True)
    return Response(bytes(row[0]) if row and row[0] else b"", mimetype="application/x-protobuf")


@app.get("/tiles/hex/<int:z>/<int:x>/<int:y>.pbf")
def hex_tile(z, x, y):
    metric = request.args.get("metric", "n_all")
    if metric not in ("n_all", "n_clean", "n_removed", "n_cluster"):
        metric = "n_all"
    sql = f"""
      WITH b AS (SELECT ST_TileEnvelope(%s,%s,%s) AS g),
      m AS (
        SELECT i, j, n_all, n_clean, n_removed, n_cluster, {metric} AS v,
               ST_AsMVTGeom(ST_Transform(h.geom,3857), b.g, 4096, 8, true) AS geom
        FROM coffee.farm_point_hex h, b
        WHERE h.geom && ST_Transform(b.g, 4326))
      SELECT ST_AsMVT(m, 'hex', 4096, 'geom') FROM m"""
    row = query(sql, [z, x, y], one=True)
    return Response(bytes(row[0]) if row and row[0] else b"", mimetype="application/x-protobuf")


# ---------------------------------------------------------------- data
@app.get("/api/counties")
def counties():
    rows = query("""
      SELECT c.counties,
             ST_AsGeoJSON(ST_SimplifyPreserveTopology(c.geom, 0.002))::json,
             coalesce(s.n, 0), coalesce(s.n_clean, 0), coalesce(s.target, false)
      FROM ref.kenya_counties c
      LEFT JOIN (SELECT county_gis, count(*) n, count(*) FILTER (WHERE status='clean') n_clean,
                        bool_or(in_target_county) target
                 FROM coffee.farm_point_qc GROUP BY county_gis) s ON s.county_gis = c.counties
      ORDER BY 1""")
    return jsonify({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": g,
         "properties": {"name": n, "n": cnt, "n_clean": nc, "target": t}}
        for n, g, cnt, nc, t in rows]})


@app.get("/api/polygons")
def polygons():
    """Digitised coffee polygons (coffee.digitized_polygon) with points-inside counts."""
    rows = query("""
      SELECT d.gid, d.id, d.name, d.notes, d.source || ' · ' || d.created_by, d.area_ha, d.created_at::date,
             ST_AsGeoJSON(d.geom)::json,
             count(q.kobo_id), count(q.kobo_id) FILTER (WHERE q.status='clean'),
             coalesce(sum(q.trees_total) FILTER (WHERE q.status='clean'), 0)
      FROM coffee.digitized_polygon d
      LEFT JOIN coffee.farm_point_qc q ON ST_Intersects(q.geom, d.geom)
      GROUP BY d.gid ORDER BY d.gid""")
    return jsonify({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": g,
         "properties": {"gid": gid, "id": id_, "name": nm, "notes": nt, "source": src,
                        "area_ha": float(a) if a is not None else None, "created": str(cr),
                        "n": n, "n_clean": nc, "trees_clean": int(t)}}
        for gid, id_, nm, nt, src, a, cr, g, n, nc, t in rows]})


@app.get("/api/progress")
def progress():
    """Digitising progress: polygons done, points covered, per day and per county."""
    tot = query("""SELECT count(*), coalesce(round(sum(area_ha),1),0), count(DISTINCT created_by),
                          min(created_at)::date, max(created_at)::date
                   FROM coffee.digitized_polygon""", one=True)
    pts = query("""SELECT count(*) FILTER (WHERE status='clean'),
                          count(*) FILTER (WHERE status='clean' AND polygon_gid IS NOT NULL),
                          count(*) FILTER (WHERE polygon_gid IS NOT NULL)
                   FROM coffee.farm_point_qc""", one=True)
    days = query("""SELECT created_at::date, count(*), round(sum(area_ha),1), count(DISTINCT created_by)
                    FROM coffee.digitized_polygon GROUP BY 1 ORDER BY 1 DESC LIMIT 30""")
    who = query("""SELECT coalesce(m.full_name, d.created_by), count(*), round(sum(area_ha),1), max(created_at)
                   FROM coffee.digitized_polygon d LEFT JOIN coffee.team_member m ON m.login = d.created_by
                   GROUP BY 1 ORDER BY 2 DESC""") if has_table("coffee.team_member") else \
          query("""SELECT created_by, count(*), round(sum(area_ha),1), max(created_at)
                   FROM coffee.digitized_polygon GROUP BY 1 ORDER BY 2 DESC""")
    counties = query("""
      SELECT county_gis, count(*) FILTER (WHERE status='clean'),
             count(*) FILTER (WHERE status='clean' AND polygon_gid IS NOT NULL)
      FROM coffee.farm_point_qc WHERE in_target_county GROUP BY 1
      HAVING count(*) FILTER (WHERE status='clean' AND polygon_gid IS NOT NULL) > 0
      ORDER BY 3 DESC""")
    empty = query("""SELECT count(*) FROM coffee.digitized_polygon d
                     WHERE NOT EXISTS (SELECT 1 FROM coffee.farm_point_qc q WHERE q.polygon_gid = d.gid)""", one=True)
    return jsonify(dict(
        polygons=tot[0], area_ha=float(tot[1]), editors=tot[2], first=str(tot[3]), last=str(tot[4]),
        clean=pts[0], clean_covered=pts[1], any_covered=pts[2], empty_polygons=empty[0],
        days=[dict(day=str(d), n=n, ha=float(h), editors=e) for d, n, h, e in days],
        who=[dict(user=u, n=n, ha=float(h), last=str(l)[:16]) for u, n, h, l in who],
        counties=[dict(county=c, clean=n, covered=k, pct=(100.0 * k / n if n else 0)) for c, n, k in counties],
    ))


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
def summary():
    status = dict(query("SELECT status, count(*) FROM coffee.farm_point_qc GROUP BY 1"))
    flags = query("""SELECT f, count(*) FROM coffee.farm_point_qc, unnest(flags) f
                     GROUP BY 1 ORDER BY 2 DESC""")
    warnings = query("""SELECT f, count(*) FROM coffee.farm_point_qc, unnest(warnings) f
                        GROUP BY 1 ORDER BY 2 DESC""")
    counties = query("""
      SELECT coalesce(county_gis,'(outside Kenya)'), count(*),
             count(*) FILTER (WHERE status='clean'),
             count(*) FILTER (WHERE status='removed'),
             count(*) FILTER (WHERE NOT county_match OR county_match IS NULL),
             bool_or(in_target_county)
      FROM coffee.farm_point_qc GROUP BY 1 ORDER BY 2 DESC""")
    trees = query("""
      SELECT count(*), sum(trees_total), sum(trees_total) FILTER (WHERE status='clean'),
             percentile_cont(0.5) WITHIN GROUP (ORDER BY trees_total) FILTER (WHERE status='clean')
      FROM coffee.farm_point_qc""", one=True)
    src = query("""SELECT count(*), count(*) FILTER (WHERE geom IS NULL),
                          count(*) FILTER (WHERE geom IS NOT NULL AND NOT coord_valid)
                   FROM coffee.farm_point""", one=True)
    clusters = query("""SELECT count(DISTINCT cluster_id), max(cluster_size)
                        FROM coffee.farm_point_qc WHERE cluster_id IS NOT NULL""", one=True)
    return jsonify(dict(
        source=dict(total=src[0], no_coordinate=src[1], invalid_coordinate=src[2]),
        status=status,
        flags=[dict(flag=f, n=n) for f, n in flags],
        warnings=[dict(flag=f, n=n) for f, n in warnings],
        counties=[dict(county=c, n=n, clean=k, removed=r, mismatch=m, target=t)
                  for c, n, k, r, m, t in counties],
        trees=dict(n=trees[0], total=int(trees[1] or 0), clean_total=int(trees[2] or 0),
                   clean_median=trees[3]),
        clusters=dict(n=clusters[0], largest=clusters[1]),
        criteria=criteria(),
    ))


def criteria():
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_qc", HERE / "build_qc.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    c = dict(mod.CRITERIA)
    c["target_counties"] = c["target_counties"] or "all counties in data"
    c["warn_only"] = ", ".join(c["warn_only"]) or "none"
    return c


@app.get("/api/hist")
def hist():
    """Histograms used to discuss thresholds: trees, road distance, cluster size."""
    trees = query("""
      SELECT b, count(*) FROM (
        SELECT CASE WHEN trees_total IS NULL OR trees_total=0 THEN '0'
                    WHEN trees_total<50 THEN '1-49' WHEN trees_total<100 THEN '50-99'
                    WHEN trees_total<200 THEN '100-199' WHEN trees_total<500 THEN '200-499'
                    WHEN trees_total<1000 THEN '500-999' WHEN trees_total<2000 THEN '1k-2k'
                    WHEN trees_total<5000 THEN '2k-5k' WHEN trees_total<10000 THEN '5k-10k'
                    WHEN trees_total<100000 THEN '10k-100k' ELSE '100k+' END b,
               trees_total FROM coffee.farm_point_qc) s GROUP BY b""")
    order = ['0','1-49','50-99','100-199','200-499','500-999','1k-2k','2k-5k','5k-10k','10k-100k','100k+']
    d = dict(trees); trees = [dict(bin=b, n=d.get(b, 0)) for b in order]
    road = query("""
      SELECT b, count(*) FROM (
        SELECT CASE WHEN dist_road_m<=2 THEN '0-2 m' WHEN dist_road_m<=5 THEN '2-5 m'
                    WHEN dist_road_m<=10 THEN '5-10 m' WHEN dist_road_m<=20 THEN '10-20 m'
                    WHEN dist_road_m<=50 THEN '20-50 m' WHEN dist_road_m<=100 THEN '50-100 m'
                    WHEN dist_road_m<=500 THEN '100-500 m' ELSE '500 m+' END b
        FROM coffee.farm_point_qc) s GROUP BY b""")
    order = ['0-2 m','2-5 m','5-10 m','10-20 m','20-50 m','50-100 m','100-500 m','500 m+']
    d = dict(road); road = [dict(bin=b, n=d.get(b, 0)) for b in order]
    clus = query("""
      SELECT b, count(*) FROM (
        SELECT CASE WHEN cluster_size=0 THEN 'none' WHEN cluster_size<10 THEN '5-9'
                    WHEN cluster_size<20 THEN '10-19' WHEN cluster_size<50 THEN '20-49'
                    WHEN cluster_size<100 THEN '50-99' ELSE '100+' END b
        FROM coffee.farm_point_qc) s GROUP BY b""")
    order = ['none','5-9','10-19','20-49','50-99','100+']
    d = dict(clus); clus = [dict(bin=b, n=d.get(b, 0)) for b in order]
    area = query("""
      SELECT b, count(*) FILTER (WHERE grower_type='Society'), count(*) FILTER (WHERE grower_type='Estate') FROM (
        SELECT grower_type, CASE WHEN implied_ha=0 THEN '0' WHEN implied_ha<0.25 THEN '<0.25 ha' WHEN implied_ha<0.5 THEN '0.25-0.5'
                    WHEN implied_ha<1 THEN '0.5-1' WHEN implied_ha<2 THEN '1-2' WHEN implied_ha<4 THEN '2-4'
                    WHEN implied_ha<10 THEN '4-10' WHEN implied_ha<40 THEN '10-40' ELSE '40+ ha' END b
        FROM coffee.farm_point_qc) s GROUP BY b""")
    order = ['0','<0.25 ha','0.25-0.5','0.5-1','1-2','2-4','4-10','10-40','40+ ha']
    d = {b: (a, e) for b, a, e in area}
    area = [dict(bin=b, n=d.get(b, (0, 0))[0], estate=d.get(b, (0, 0))[1]) for b in order]
    return jsonify(dict(trees=trees, road=road, cluster=clus, area=area))


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
def heat():
    """Hex centroids weighted by count -- feeds the Leaflet.heat layer."""
    metric = request.args.get("metric", "n_all")
    if metric not in ("n_all", "n_clean", "n_removed", "n_cluster"):
        metric = "n_all"
    rows = query(f"""SELECT round(ST_Y(ST_Centroid(geom))::numeric,4), round(ST_X(ST_Centroid(geom))::numeric,4), {metric}
                     FROM coffee.farm_point_hex WHERE {metric} > 0""")
    return jsonify([[float(a), float(b), int(c)] for a, b, c in rows])


@app.get("/api/clusters")
def clusters():
    """Largest DBSCAN clusters -- for the 'suspicious clusters' table."""
    rows = query("""
      SELECT cluster_id, count(*) n, count(DISTINCT submitted_by) enumerators,
             min(county_gis), round(avg(latitude)::numeric,5), round(avg(longitude)::numeric,5),
             mode() WITHIN GROUP (ORDER BY factory_name)
      FROM coffee.farm_point_qc WHERE cluster_id IS NOT NULL
      GROUP BY cluster_id ORDER BY n DESC LIMIT 40""")
    return jsonify([dict(id=a, n=b, enumerators=c, county=d, lat=float(e), lon=float(f), factory=g)
                    for a, b, c, d, e, f, g in rows])


if __name__ == "__main__":
    ensure_triggers()
    threading.Thread(target=listener, daemon=True).start()
    app.run(host=os.environ.get("DASH_HOST", "127.0.0.1"), port=int(os.environ.get("DASH_PORT", "5055")),
            debug=False, threaded=True)
