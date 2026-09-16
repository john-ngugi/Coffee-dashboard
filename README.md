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

## Exposing it outside the LAN (nginx + Let's Encrypt)

Prerequisites: a domain (e.g. `coffee.example.com`) with an A record pointing
at your public IP, and the router forwarding TCP 80 and 443 to this server.

1. In `.env` set `DOMAIN=coffee.example.com` and `LETSENCRYPT_EMAIL=you@example.com`.
2. Bootstrap once (creates the basic-auth user `coffee`, obtains the certificate,
   starts nginx and the auto-renewer):

   ```bash
   sudo bash deploy/init_https.sh
   ```

3. From then on include the `public` profile whenever you bring the stack up:

   ```bash
   sudo docker compose --profile public up -d
   ```

The dashboard is then at `https://coffee.example.com` behind a username/password
prompt. Port 5055 is bound to `127.0.0.1` on the host, so the only way in from
outside is through nginx. Add or change users with:

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
