-- resolve_flag.sql -- let a supervisor close a flag without erasing it.
-- Safe to re-run.
--
--   sudo docker exec -i coffee-pg psql -U postgres -d coffee_eudr < deploy/sql/resolve_flag.sql
--
-- coffee.flag_change() marks a change as questionable. Until now the only way
-- for that flag to go away was to undo the change, so a flag raised by mistake
-- -- or one that was looked at and found to be fine -- stayed in the review
-- queue for ever. Resolving keeps the flag and its note in the history (the
-- point of an audit trail is that nothing quietly disappears) and records who
-- closed it, when, and why; the review queue then stops showing it.

BEGIN;

ALTER TABLE coffee.digitized_polygon_history
    ADD COLUMN IF NOT EXISTS resolved_at   timestamptz,
    ADD COLUMN IF NOT EXISTS resolved_by   text,
    ADD COLUMN IF NOT EXISTS resolve_note  text;

-- the review queue is "flagged, not undone, not yet resolved"
CREATE INDEX IF NOT EXISTS digitized_polygon_history_open_flag_idx
    ON coffee.digitized_polygon_history (gid)
    WHERE flagged AND undone_by IS NULL AND resolved_at IS NULL;

-- Raising a flag always opens the question, even if an older flag on the same
-- change was resolved -- otherwise re-flagging looks like it did nothing.
-- Supersedes the definition in polygon_history.sql.
CREATE OR REPLACE FUNCTION coffee.flag_change(p_hist_id bigint, p_note text DEFAULT NULL) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = coffee, public AS $$
    UPDATE coffee.digitized_polygon_history
    SET flagged = true, flag_note = p_note, flagged_by = coffee.actor(),
        resolved_at = NULL, resolved_by = NULL, resolve_note = NULL
    WHERE hist_id = p_hist_id;
$$;
REVOKE ALL ON FUNCTION coffee.flag_change(bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION coffee.flag_change(bigint, text) TO coffee_editor;

-- Resolve a flag. Supervisors only: flagging asks a question of the person who
-- made the change, and closing that question is a supervisor's call, the same
-- authority as undoing it.
CREATE OR REPLACE FUNCTION coffee.resolve_flag(p_hist_id bigint, p_note text DEFAULT NULL) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = coffee, public AS $$
DECLARE h coffee.digitized_polygon_history;
BEGIN
    SELECT * INTO h FROM coffee.digitized_polygon_history WHERE hist_id = p_hist_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'history row % not found', p_hist_id;
    END IF;
    IF NOT h.flagged THEN
        RAISE EXCEPTION 'change % is not flagged', p_hist_id;
    END IF;
    IF h.resolved_at IS NOT NULL THEN
        RAISE EXCEPTION 'flag on change % was already resolved by % on %',
            p_hist_id, h.resolved_by, h.resolved_at::date;
    END IF;
    UPDATE coffee.digitized_polygon_history
    SET resolved_at = now(), resolved_by = coffee.actor(), resolve_note = p_note
    WHERE hist_id = p_hist_id;
END $$;
REVOKE ALL ON FUNCTION coffee.resolve_flag(bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION coffee.resolve_flag(bigint, text) TO coffee_supervisor;

-- reopen, in case a flag was closed too quickly
CREATE OR REPLACE FUNCTION coffee.reopen_flag(p_hist_id bigint) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = coffee, public AS $$
    UPDATE coffee.digitized_polygon_history
    SET resolved_at = NULL, resolved_by = NULL, resolve_note = NULL
    WHERE hist_id = p_hist_id AND flagged;
$$;
REVOKE ALL ON FUNCTION coffee.reopen_flag(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION coffee.reopen_flag(bigint) TO coffee_supervisor;

-- the dashboard reads the history through this view
CREATE OR REPLACE VIEW coffee.digitized_polygon_activity AS
SELECT h.hist_id, h.gid, h.op, h.changed_at, h.changed_by, h.client_addr,
       coalesce(h.after->>'name', h.before->>'name') AS name,
       (h.after->>'area_ha')::numeric  AS area_ha_after,
       (h.before->>'area_ha')::numeric AS area_ha_before,
       h.flagged, h.flag_note, h.flagged_by, h.undone_by, h.undoes,
       (h.undone_by IS NOT NULL) AS undone,
       coalesce(h.geom_after, h.geom_before) AS geom,
       -- appended at the end: CREATE OR REPLACE VIEW can only add columns there
       h.resolved_at, h.resolved_by, h.resolve_note,
       (h.flagged AND h.undone_by IS NULL AND h.resolved_at IS NULL) AS flag_open
FROM coffee.digitized_polygon_history h;
GRANT SELECT ON coffee.digitized_polygon_activity TO coffee_reader, coffee_editor;

COMMIT;
