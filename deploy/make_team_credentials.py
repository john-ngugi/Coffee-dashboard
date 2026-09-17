#!/usr/bin/env python3
"""
make_team_credentials.py -- generate personal database logins for the team.

Reads the roster below, generates one password per person and writes to
credentials/ (never committed):

    credentials/team_logins.sql       CREATE ROLE ... + coffee.team_member rows
    credentials/team_credentials.csv  name, title, login, password, rights

Apply on the server with
    scp credentials/team_logins.sql user@server:~/Coffee-dashboard/credentials/
    sudo docker compose exec -T db psql -U postgres -d coffee_eudr < credentials/team_logins.sql

Re-running regenerates passwords only for logins that do not exist yet
(--reset NAME forces a new password for one login). Requires deploy/sql/team.sql
and deploy/sql/polygon_history.sql to have been applied first.
"""
import csv, os, re, secrets, string, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "credentials"

# (seq, full name incl. rank, title, role_group, team lead's login)
ROSTER = [
    (1,  "Maj Claire Muhungi",        "Coordinator",                 "coordinator",    None),
    (2,  "Capt Jotahm Omusee",        "QC1",                         "qc",             None),
    (3,  "Capt Tajeu",                "QC2",                         "qc",             None),
    (4,  "Capt Peter Okello",         "QC3",                         "qc",             None),
    (5,  "WOI Andrew Musyoki",        "QC4",                         "qc",             None),
    (6,  "WOII Alex Wanjala",         "QC5",                         "qc",             None),
    (13, "SSgt Owiti",                "QC6",                         "qc",             None),
    (7,  "Ms Viola Kemunto",          "Data Compilation & Cleaning", "data",           None),
    (8,  "Mr Hillary Kibiwot",        "Data Compilation & Cleaning", "data",           None),
    (9,  "Mr John Ngugi",             "Data Compilation & Cleaning", "data",           None),
    (10, "James Kulundu",             "Data Management",             "management",     None),
    (11, "Ssgt Samuel Mburu",         "Image Classification",        "classification", None),
    (12, "Sgt James Kosgei Busi",     "Image Classification",        "classification", None),
    # Capt Tajeu's team
    (14, "Sgt Donald Cheboi",         "Digitization 1",  "digitiser", "tajeu"),
    (15, "Sgt Tobias Mutua",          "Digitization 2",  "digitiser", "tajeu"),
    (16, "SSgt Jeremiah Mbaisi",      "Digitization 3",  "digitiser", "tajeu"),
    (17, "SSgt Belinda Kwamboka",     "Digitization 4",  "digitiser", "tajeu"),
    (18, "SSgt Omondi Odende",        "Digitization 5",  "digitiser", "tajeu"),
    (19, "Sgt Esther Adhiambo",       "Digitization 6",  "digitiser", "tajeu"),
    # SSgt Owiti's team
    (20, "Sgt Akisa Moruri",          "Digitization 7",  "digitiser", "owiti"),
    (21, "Sgt Chebet Barno",          "Digitization 8",  "digitiser", "owiti"),
    (22, "Sgt Robert Kariuki",        "Digitization 9",  "digitiser", "owiti"),
    (23, "Cpl Brian Otieno",          "Digitization 10", "digitiser", "owiti"),
    (24, "John Mwangi",               "Digitization 11", "digitiser", "owiti"),
    (25, "Franklyn",                  "Digitization 12", "digitiser", "owiti"),
    (26, "Sharon Njoroge",            "Digitization 13", "digitiser", "owiti"),
    # WOI Andrew Musyoki's team
    (27, "Brian Terer",               "Digitization 14", "digitiser", "andrew_musyoki"),
    (28, "Sammy Onyango",             "Digitization 15", "digitiser", "andrew_musyoki"),
    (29, "Joe Mwasogona",             "Digitization 16", "digitiser", "andrew_musyoki"),
    (30, "Meshack Kipleting",         "Digitization 17", "digitiser", "andrew_musyoki"),
    (31, "David Siapai Kiraison",     "Digitization 18", "digitiser", "andrew_musyoki"),
    (32, "Moses Wekesa Sijui",        "Digitization 19", "digitiser", "andrew_musyoki"),
    # Capt Omusee's team
    (33, "Nerhius Otione Ogutu",      "Digitization 20", "digitiser", "jotahm_omusee"),
    (34, "Cynthia Katasi Ongachi",    "Digitization 21", "digitiser", "jotahm_omusee"),
    (35, "Mburu Walter Kago",         "Digitization 22", "digitiser", "jotahm_omusee"),
    (36, "Vincent Murimi",            "Digitization 23", "digitiser", "jotahm_omusee"),
    (37, "Gladkenneth Macharia",      "Digitization 24", "digitiser", "jotahm_omusee"),
    (38, "Israel Mutua",              "Digitization 25", "digitiser", "jotahm_omusee"),
    # Capt Okello's team
    (39, "Vincent Maundu",            "Digitization 26", "digitiser", "peter_okello"),
    (40, "Tonny Mwendwa",             "Digitization 27", "digitiser", "peter_okello"),
    (41, "Faith Hussein",             "Digitization 28", "digitiser", "peter_okello"),
    (42, "Negesa Shalton",            "Digitization 29", "digitiser", "peter_okello"),
    (43, "Clarice Muthua",            "Digitization 30", "digitiser", "peter_okello"),
    (44, "Joseph Ouma",               "Digitization 31", "digitiser", "peter_okello"),
    # WOII Wanjala's team
    (45, "Bleander Kiprotich",        "Digitization 32", "digitiser", "alex_wanjala"),
    (46, "ICT Attachee",              "Digitization 33", "digitiser", "alex_wanjala"),
    (47, "Godwin Kiptoo",             "Digitization 34", "digitiser", "alex_wanjala"),
    (48, "Felix Waititu",             "Digitization 35", "digitiser", "alex_wanjala"),
    (49, "Ezra Kipkoech",             "Digitization 36", "digitiser", "alex_wanjala"),
    (50, "Sheila Mwende Mwaniki",     "Digitization 37", "digitiser", "alex_wanjala"),
]

RANKS = {"maj", "capt", "woi", "woii", "ssgt", "sgt", "cpl", "ms", "mr", "mrs"}
# role_group -> database group role
DB_ROLE = dict(coordinator="coffee_supervisor", qc="coffee_supervisor", data="coffee_supervisor",
               management="coffee_supervisor", classification="coffee_editor", digitiser="coffee_editor")
RIGHTS = dict(coffee_supervisor="edit + undo (supervisor)", coffee_editor="edit polygons, move points")


def login_for(full_name):
    parts = [p for p in re.split(r"\s+", full_name.strip()) if p.lower() not in RANKS]
    parts = [re.sub(r"[^a-z0-9]", "", p.lower()) for p in parts]
    return parts[0] if len(parts) == 1 else f"{parts[0]}_{parts[-1]}"


def password():
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(12))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def main():
    reset = set(a for a in sys.argv[2:]) if "--reset" in sys.argv else set()
    OUT.mkdir(exist_ok=True)
    csv_path = OUT / "team_credentials.csv"
    existing = {}
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8") as f:
            existing = {r["login"]: r for r in csv.DictReader(f)}

    rows = []
    for seq, name, title, group, lead in ROSTER:
        login = login_for(name)
        pw = existing[login]["password"] if login in existing and login not in reset else password()
        rows.append(dict(seq=seq, name=name, title=title, group=group, lead=lead, login=login,
                         password=pw, db_role=DB_ROLE[group], rights=RIGHTS[DB_ROLE[group]]))
    assert len({r["login"] for r in rows}) == len(rows), "duplicate logins"
    leads = {r["login"] for r in rows if r["group"] == "qc"}
    assert all(r["lead"] in leads for r in rows if r["lead"]), "unknown team lead"

    def q(v):  # SQL string literal
        return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"

    sql = ["-- generated by deploy/make_team_credentials.py -- contains passwords, keep private",
           "BEGIN;"]
    for r in rows:
        sql.append(f"""DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {q(r['login'])}) THEN
    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L IN ROLE %I', {q(r['login'])}, {q(r['password'])}, {q(r['db_role'])});
  ELSE
    EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L', {q(r['login'])}, {q(r['password'])});
    EXECUTE format('GRANT %I TO %I', {q(r['db_role'])}, {q(r['login'])});
  END IF;
END $$;""")
    # leads first so the FK resolves
    for r in sorted(rows, key=lambda r: (r["lead"] is not None, r["seq"])):
        sql.append(f"INSERT INTO coffee.team_member (login, full_name, title, role_group, team_lead, seq) "
                   f"VALUES ({q(r['login'])}, {q(r['name'])}, {q(r['title'])}, {q(r['group'])}, {q(r['lead'])}, {r['seq']}) "
                   f"ON CONFLICT (login) DO UPDATE SET full_name = EXCLUDED.full_name, title = EXCLUDED.title, "
                   f"role_group = EXCLUDED.role_group, team_lead = EXCLUDED.team_lead, seq = EXCLUDED.seq, active = true;")
    sql.append("COMMIT;")
    (OUT / "team_logins.sql").write_text("\n".join(sql) + "\n", encoding="utf-8", newline="\n")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["seq", "name", "title", "login", "password", "rights", "lead", "group", "db_role"])
        w.writeheader(); w.writerows(sorted(rows, key=lambda r: r["seq"]))
    print(f"wrote {OUT / 'team_logins.sql'} and {csv_path} ({len(rows)} logins)")


if __name__ == "__main__":
    main()
