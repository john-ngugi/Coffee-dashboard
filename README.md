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

### Day-to-day

```bash
sudo docker compose logs -f dashboard              # app logs
sudo docker compose up -d --build dashboard        # redeploy after git pull
sudo docker compose exec dashboard python build_qc.py --flags   # rerun QC
```

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
