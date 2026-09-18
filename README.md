# Coffee QC dashboard

Flask + Leaflet web dashboard over the Kobo coffee-farm survey held in PostGIS
(`coffee_eudr`). Vector tiles are served straight from `coffee.farm_point_qc`
with `ST_AsMVT`; edits to the QC table or the digitised polygons are pushed to
open browser tabs over Server-Sent Events.

## Deploy on the server (Docker)

```bash
git clone https://github.com/john-ngugi/Coffee-dashboard.git
cd Coffee-dashboard
cp .env.example .env        # set POSTGRES_PASSWORD, CARTO_KEY, MAPTILER_KEY
```

`POSTGRES_PASSWORD` must be the password of the `postgres` superuser in the
database volume. If you created the database with `docker run` earlier, use
the same value you passed there.

If a container named `coffee-pg` already exists from a manual `docker run`,
remove it first — the data lives in the `coffee_pgdata` volume and is reused:

```bash
sudo docker rm -f coffee-pg
```

Then:

```bash
sudo docker compose up -d --build
```

The dashboard is at `http://<server-ip>:5055`, Postgres on `5432` (for QGIS).

### Restoring the database from dumps

With the `db` service running and the dump files
(`01_roles.sql`, `02_coffee_eudr.dump`, `03_grants.sql`) copied next to
`deploy/restore_db.sh`:

```bash
sudo bash deploy/restore_db.sh coffee-pg
```

### QGIS layers

Connect to `<server-ip>:5432`, database `coffee_eudr`:

| Layer | Login | Notes |
|---|---|---|
| `coffee.farm_point_clean_public` | personal login / `coffee_editor` | cleaned points, PII stripped (key `kobo_id`); geometry editable, see below |
| `coffee.digitized_polygon` | `coffee_editor` or personal login | editable; leave `gid`/`id` blank, area + timestamps fill automatically |
| `coffee.farm_point_public` | `coffee_reader` / `coffee_editor` | all raw points, PII stripped |

`farm_point_clean_public` is created by `build_qc.py`; on a database restored
from an older dump, create it once with:

```bash
sudo docker compose exec -T db psql -U postgres -d coffee_eudr < deploy/sql/farm_point_clean_public.sql
```

### Multi-user digitising: personal logins, history, undo, point moves

Enable once per database (all idempotent, in this order):

```bash
for f in polygon_history team point_moves; do sudo docker compose exec -T db psql -U postgres -d coffee_eudr < deploy/sql/$f.sql; done
```

This adds `coffee.digitized_polygon_history` (every insert / update / delete
of a polygon with who, when, client IP and the full before/after row), a
`coffee_supervisor` role, and these functions:

| Function | Who | What |
|---|---|---|
| `coffee.flag_change(hist_id, note)` | any editor | mark a change for review (shows red on the dashboard) |
| `coffee.undo_change(hist_id)` | supervisors | revert one change (re-adds a deleted polygon, restores an edited one, removes an added one) |
| `coffee.undo_user_changes('user', since)` | supervisors | revert everything one person did since a timestamp, newest first |

Undos are themselves logged and can be undone again. A single statement may
not delete or update more than 25 polygons (`SET coffee.allow_bulk = 'on'` to
override in a session). Editors no longer have write access to `farm_point_public`.

#### Team logins

Every team member gets their own database login (so history names the person)
generated from the roster in `deploy/make_team_credentials.py`:

```bash
python deploy/make_team_credentials.py        # on your PC -> credentials/ (never committed)
scp credentials/team_logins.sql user@server:~/Coffee-dashboard/credentials/
sudo docker compose exec -T db psql -U postgres -d coffee_eudr < credentials/team_logins.sql   # on the server
```

`credentials/team_credentials.csv` has name, login, password and rights for
distribution. Coordinator, QC leads, data and management get
`coffee_supervisor` (edit + undo); digitisers and image classification get
`coffee_editor`. Re-running the script keeps existing passwords;
`--reset login` issues a new one. `deploy/add_digitiser.sh` still works for a
quick extra login outside the roster.

#### Moving points to the farm centre

`coffee.farm_point_clean_public` is editable in QGIS **for geometry only**:
digitisers drag a point onto the farm, save, and the move is applied to
`farm_point_qc` (lat/lon, UTM geometry and containing polygon updated) and
logged in `coffee.farm_point_move`. Attributes cannot be changed, points cannot
be deleted, and a single move over 500 m is refused. Supervisors undo with
`coffee.undo_point_move(move_id)` or from the portal. `build_qc.py` re-applies
every move after a rebuild, so QC reruns never lose them.

#### Team portal — `/team`

Each person signs in at `http://<server>:5055/team` with their database login
and sees their own polygons, hectares, counties, per-day progress, point moves
and anything flagged on their work. QC leads see their team; the coordinator,
data and management see all teams. Supervisors can flag and undo from the
page; the action runs as their database role, so the database itself decides
who may undo. Set `DASH_SECRET` in `.env` so sign-ins survive restarts.

Typical supervisor session (psql or the QGIS DB Manager SQL window):

```sql
SELECT hist_id, changed_at, changed_by, op, gid, name, flagged, undone
FROM coffee.digitized_polygon_activity ORDER BY hist_id DESC LIMIT 50;

SELECT coffee.undo_change(123);                                   -- one change
SELECT coffee.undo_user_changes('bob', now() - interval '2 hours'); -- all of bob's recent work
```

`coffee.digitized_polygon_activity` also loads in QGIS as a polygon layer
(geometry = the shape before/after each change), useful for seeing what a
deleted polygon looked like.

### Day-to-day

```bash
sudo bash deploy/update.sh                         # git pull + rebuild + swap dashboard (~3 s downtime)
sudo docker compose logs -f dashboard              # app logs
sudo docker compose exec dashboard python build_qc.py --flags   # rerun QC
```

`update.sh` builds the new image while the old container keeps serving, then
swaps only the dashboard (`--no-deps`, so Postgres and nginx are untouched) and
waits for it to answer. Open browser tabs reconnect on their own.

## Exposing it outside the LAN

Both routes put nginx (HTTPS + username/password) in front of the app. nginx
listens on host ports **8443** (HTTPS) and **8088** (HTTP redirect) because 80/443
are used by other systems; change with `NGINX_HTTPS_PORT` / `NGINX_HTTP_PORT` in
`.env`. Port 5055 itself is bound to `127.0.0.1` on the host, so nginx is the
only way in from outside.

### No domain: public IP + self-signed certificate

1. In `.env` set `DOMAIN=<your public IP>` (`curl ifconfig.me` on the server).
2. Bootstrap once (creates the basic-auth user `coffee`, generates a 10-year
   self-signed certificate, starts nginx):

   ```bash
   sudo bash deploy/init_selfsigned.sh
   ```

3. Open `https://<public IP>:8443`. Browsers warn once about the certificate
   ("Advanced -> Proceed"); the connection is still encrypted.
4. From then on bring the stack up with `sudo docker compose --profile public up -d`.

### With a domain: Let's Encrypt

Prerequisites: a domain (e.g. `coffee.example.com`) with an A record pointing
at your public IP, and the router forwarding external TCP 80 -> this server:8088
and 443 -> this server:8443 (Let's Encrypt needs external port 80).

1. In `.env` set `DOMAIN=coffee.example.com` and `LETSENCRYPT_EMAIL=you@example.com`.
2. Bootstrap once (creates the basic-auth user `coffee`, obtains the certificate,
   starts nginx and the auto-renewer):

   ```bash
   sudo bash deploy/init_https.sh
   ```

3. From then on bring the stack up with
   `sudo docker compose --profile public --profile letsencrypt up -d`.

### Managing users

Add or change dashboard users with:

```bash
sudo docker run --rm httpd:2.4-alpine htpasswd -nbB alice 'her-password' | sudo tee -a deploy/nginx/.htpasswd
sudo docker compose exec nginx nginx -s reload
```

Alternatives that avoid opening router ports: a Cloudflare Tunnel (+ Access
for login) or Tailscale for a private VPN-only setup.

## Run locally without Docker

```bash
pip install -r requirements.txt
python dashboard.py            # http://localhost:5055
```

`dashboard.py` reads `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` /
`PGDATABASE` from `.env` (or the environment). `DASH_HOST` / `DASH_PORT`
override the bind address.

## Notes

- The dashboard has no login. It exposes respondent details from the QC
  table, so keep port 5055 on the LAN / behind a VPN or a reverse proxy with auth.
- Leaflet must stay at 1.7.1 and the vector layers must not set
  `maxNativeZoom`, or VectorGrid click handling breaks.
