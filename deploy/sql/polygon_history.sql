-- Change history for coffee.digitized_polygon: every insert/update/delete is
-- recorded with who/when/from-where and the full before/after row, so any
-- change can be flagged and undone. Idempotent; run as postgres.
--   sudo docker compose exec -T db psql -U postgres -d coffee_eudr < deploy/sql/polygon_history.sql

-- 0. digitisers must never be able to touch the survey points themselves
REVOKE INSERT, UPDATE, DELETE ON coffee.farm_point_public FROM coffee_editor;

-- 1. group role for the people allowed to undo other people's changes
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'coffee_supervisor') THEN
    CREATE ROLE coffee_supervisor NOLOGIN;
  END IF;
END $$;
GRANT coffee_editor TO coffee_supervisor;

-- 1b. who is acting: the login, or -- when the dashboard's superuser connection
--     acts on behalf of a signed-in team member -- the coffee.actor setting.
CREATE OR REPLACE FUNCTION coffee.actor() RETURNS text LANGUAGE sql STABLE AS $$
    SELECT CASE WHEN coalesce(current_setting('coffee.actor', true), '') <> ''
                 AND (SELECT rolsuper FROM pg_roles WHERE rolname = session_user)
           THEN current_setting('coffee.actor', true) ELSE session_user::text END
$$;

-- 2. history table
CREATE TABLE IF NOT EXISTS coffee.digitized_polygon_history (
    hist_id      bigserial PRIMARY KEY,
    gid          integer     NOT NULL,
    op           text        NOT NULL CHECK (op IN ('INSERT', 'UPDATE', 'DELETE')),
    changed_at   timestamptz NOT NULL DEFAULT now(),
    changed_by   text        NOT NULL DEFAULT coffee.actor(),
    client_addr  inet                 DEFAULT inet_client_addr(),
    application  text                 DEFAULT current_setting('application_name', true),
    before       jsonb,               -- full row before the change (UPDATE / DELETE)
    after        jsonb,               -- full row after the change  (INSERT / UPDATE)
    geom_before  geometry(MultiPolygon, 4326),
    geom_after   geometry(MultiPolygon, 4326),
    flagged      boolean     NOT NULL DEFAULT false,
    flag_note    text,
    flagged_by   text,
    undone_by    bigint REFERENCES coffee.digitized_polygon_history (hist_id),  -- set when this change was undone
    undoes       bigint REFERENCES coffee.digitized_polygon_history (hist_id)   -- set when this row IS an undo
);
-- upgrades for a table created by an earlier version of this script
ALTER TABLE coffee.digitized_polygon_history ALTER COLUMN changed_by SET DEFAULT coffee.actor();
ALTER TABLE coffee.digitized_polygon_history ADD COLUMN IF NOT EXISTS undoes bigint REFERENCES coffee.digitized_polygon_history (hist_id);
CREATE INDEX IF NOT EXISTS digitized_polygon_history_gid_idx  ON coffee.digitized_polygon_history (gid, hist_id);
CREATE INDEX IF NOT EXISTS digitized_polygon_history_at_idx   ON coffee.digitized_polygon_history (changed_at DESC);
CREATE INDEX IF NOT EXISTS digitized_polygon_history_by_idx   ON coffee.digitized_polygon_history (changed_by, changed_at DESC);
CREATE INDEX IF NOT EXISTS digitized_polygon_history_flag_idx ON coffee.digitized_polygon_history (flagged) WHERE flagged;

GRANT SELECT ON coffee.digitized_polygon_history TO coffee_reader, coffee_editor;

-- 3. row-level logger
CREATE OR REPLACE FUNCTION coffee.digitized_polygon_log() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = coffee, public AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO coffee.digitized_polygon_history (gid, op, after, geom_after)
        VALUES (NEW.gid, TG_OP, to_jsonb(NEW), NEW.geom);
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD IS NOT DISTINCT FROM NEW THEN RETURN NULL; END IF;   -- no-op save
        INSERT INTO coffee.digitized_polygon_history (gid, op, before, after, geom_before, geom_after)
        VALUES (NEW.gid, TG_OP, to_jsonb(OLD), to_jsonb(NEW), OLD.geom, NEW.geom);
    ELSE
        INSERT INTO coffee.digitized_polygon_history (gid, op, before, geom_before)
        VALUES (OLD.gid, TG_OP, to_jsonb(OLD), OLD.geom);
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS digitized_polygon_log ON coffee.digitized_polygon;
CREATE TRIGGER digitized_polygon_log AFTER INSERT OR UPDATE OR DELETE ON coffee.digitized_polygon
    FOR EACH ROW EXECUTE FUNCTION coffee.digitized_polygon_log();

-- 4. guard: one statement may not delete/update more than 25 polygons unless
--    the session opts in with  SET coffee.allow_bulk = 'on';
CREATE OR REPLACE FUNCTION coffee.digitized_polygon_bulk_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE n bigint;
BEGIN
    SELECT count(*) INTO n FROM affected;
    IF n > 25 AND current_setting('coffee.allow_bulk', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'refusing to % % polygons in one statement (limit 25); SET coffee.allow_bulk = ''on'' to override',
            lower(TG_OP), n;
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS digitized_polygon_bulk_delete ON coffee.digitized_polygon;
CREATE TRIGGER digitized_polygon_bulk_delete AFTER DELETE ON coffee.digitized_polygon
    REFERENCING OLD TABLE AS affected FOR EACH STATEMENT EXECUTE FUNCTION coffee.digitized_polygon_bulk_guard();
DROP TRIGGER IF EXISTS digitized_polygon_bulk_update ON coffee.digitized_polygon;
CREATE TRIGGER digitized_polygon_bulk_update AFTER UPDATE ON coffee.digitized_polygon
    REFERENCING OLD TABLE AS affected FOR EACH STATEMENT EXECUTE FUNCTION coffee.digitized_polygon_bulk_guard();

-- 5. flag a change for review (any editor)
CREATE OR REPLACE FUNCTION coffee.flag_change(p_hist_id bigint, p_note text DEFAULT NULL) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = coffee, public AS $$
    UPDATE coffee.digitized_polygon_history
    SET flagged = true, flag_note = p_note, flagged_by = coffee.actor()
    WHERE hist_id = p_hist_id;
$$;
REVOKE ALL ON FUNCTION coffee.flag_change(bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION coffee.flag_change(bigint, text) TO coffee_editor;

-- 6. undo one change (supervisors). The undo itself is logged as a new change
--    and linked back via undone_by, so it can be undone again if needed.
CREATE OR REPLACE FUNCTION coffee.undo_change(p_hist_id bigint) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = coffee, public AS $$
DECLARE h coffee.digitized_polygon_history; r coffee.digitized_polygon; new_hist bigint;
BEGIN
    SELECT * INTO h FROM coffee.digitized_polygon_history WHERE hist_id = p_hist_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'history row % not found', p_hist_id; END IF;
    IF h.undone_by IS NOT NULL THEN RAISE EXCEPTION 'change % was already undone by %', p_hist_id, h.undone_by; END IF;
    IF h.undoes IS NULL AND p_hist_id <> (SELECT max(hist_id) FROM coffee.digitized_polygon_history
                                          WHERE gid = h.gid AND undone_by IS NULL AND undoes IS NULL) THEN
        RAISE EXCEPTION 'change % is not the latest for polygon %; undo the newer changes first', p_hist_id, h.gid;
    END IF;

    IF h.op = 'INSERT' THEN
        DELETE FROM coffee.digitized_polygon WHERE gid = h.gid;
    ELSE
        r := jsonb_populate_record(NULL::coffee.digitized_polygon, h.before);
        IF h.op = 'UPDATE' THEN
            UPDATE coffee.digitized_polygon
            SET id = r.id, name = r.name, notes = r.notes, source = r.source, geom = r.geom,
                created_at = r.created_at, created_by = r.created_by
            WHERE gid = h.gid;
        ELSE
            INSERT INTO coffee.digitized_polygon (gid, id, name, notes, source, geom, created_at, created_by)
            VALUES (r.gid, r.id, r.name, r.notes, r.source, r.geom, r.created_at, r.created_by);
        END IF;
    END IF;

    new_hist := currval('coffee.digitized_polygon_history_hist_id_seq');
    UPDATE coffee.digitized_polygon_history SET undoes = p_hist_id WHERE hist_id = new_hist;
    UPDATE coffee.digitized_polygon_history SET undone_by = new_hist WHERE hist_id = p_hist_id;
    -- undoing an undo re-applies the change that undo had cancelled
    IF h.undoes IS NOT NULL THEN
        UPDATE coffee.digitized_polygon_history SET undone_by = NULL WHERE hist_id = h.undoes;
    END IF;
    RETURN new_hist;
END $$;
REVOKE ALL ON FUNCTION coffee.undo_change(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION coffee.undo_change(bigint) TO coffee_supervisor;

-- 7. undo everything one person did since a point in time (newest first)
CREATE OR REPLACE FUNCTION coffee.undo_user_changes(p_user text, p_since timestamptz) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = coffee, public AS $$
DECLARE hid bigint; n integer := 0;
BEGIN
    FOR hid IN SELECT hist_id FROM coffee.digitized_polygon_history
               WHERE changed_by = p_user AND changed_at >= p_since AND undone_by IS NULL AND undoes IS NULL
               ORDER BY hist_id DESC
    LOOP
        PERFORM coffee.undo_change(hid); n := n + 1;
    END LOOP;
    RETURN n;
END $$;
REVOKE ALL ON FUNCTION coffee.undo_user_changes(text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION coffee.undo_user_changes(text, timestamptz) TO coffee_supervisor;

-- 8. convenient view for the dashboard / QGIS
CREATE OR REPLACE VIEW coffee.digitized_polygon_activity AS
SELECT h.hist_id, h.gid, h.op, h.changed_at, h.changed_by, h.client_addr,
       coalesce(h.after->>'name', h.before->>'name') AS name,
       (h.after->>'area_ha')::numeric  AS area_ha_after,
       (h.before->>'area_ha')::numeric AS area_ha_before,
       h.flagged, h.flag_note, h.flagged_by, h.undone_by, h.undoes,
       (h.undone_by IS NOT NULL) AS undone,
       coalesce(h.geom_after, h.geom_before) AS geom
FROM coffee.digitized_polygon_history h;
GRANT SELECT ON coffee.digitized_polygon_activity TO coffee_reader, coffee_editor;
