-- Team roster: one row per database login, used by the /team portal and the
-- dashboard to show names, roles and who supervises whom. Rows are inserted by
-- the (private, not committed) credentials script. Idempotent.
--   sudo docker compose exec -T db psql -U postgres -d coffee_eudr < deploy/sql/team.sql

CREATE TABLE IF NOT EXISTS coffee.team_member (
    login       text PRIMARY KEY,                 -- database role name
    full_name   text NOT NULL,
    title       text NOT NULL,                    -- e.g. 'Coordinator', 'QC2', 'Digitization 14'
    role_group  text NOT NULL CHECK (role_group IN
                  ('coordinator', 'qc', 'data', 'management', 'classification', 'digitiser')),
    team_lead   text REFERENCES coffee.team_member (login),   -- QC lead for digitisers
    seq         integer,                          -- order in the roster
    active      boolean NOT NULL DEFAULT true
);
GRANT SELECT ON coffee.team_member TO coffee_reader, coffee_editor;

-- what a login may do, as seen by the portal
CREATE OR REPLACE VIEW coffee.team_member_access AS
SELECT m.*,
       pg_has_role(m.login, 'coffee_supervisor', 'MEMBER') AS can_undo,
       pg_has_role(m.login, 'coffee_editor', 'MEMBER')     AS can_edit,
       (m.role_group IN ('coordinator', 'data', 'management')) AS sees_all
FROM coffee.team_member m
WHERE EXISTS (SELECT 1 FROM pg_roles r WHERE r.rolname = m.login);
GRANT SELECT ON coffee.team_member_access TO coffee_reader, coffee_editor;
