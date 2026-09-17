-- Let digitisers drag a clean survey point to the centre of the farm.
--
-- coffee.farm_point_clean_public gets an INSTEAD OF UPDATE trigger: only the
-- geometry may change, every move is logged in coffee.farm_point_move (who,
-- when, from, to, distance) and can be undone by a supervisor. Moves are
-- re-applied by build_qc.py after a rebuild so they survive QC reruns.
-- Requires polygon_history.sql (coffee.actor, coffee_supervisor). Idempotent.
--   sudo docker compose exec -T db psql -U postgres -d coffee_eudr < deploy/sql/point_moves.sql

CREATE TABLE IF NOT EXISTS coffee.farm_point_move (
    move_id      bigserial PRIMARY KEY,
    kobo_id      integer     NOT NULL,
    moved_at     timestamptz NOT NULL DEFAULT now(),
    moved_by     text        NOT NULL DEFAULT coffee.actor(),
    client_addr  inet                 DEFAULT inet_client_addr(),
    geom_before  geometry(Point, 4326) NOT NULL,
    geom_after   geometry(Point, 4326) NOT NULL,
    dist_m       real        NOT NULL,
    undone_by    bigint REFERENCES coffee.farm_point_move (move_id),
    undoes       bigint REFERENCES coffee.farm_point_move (move_id)
);
CREATE INDEX IF NOT EXISTS farm_point_move_kobo_idx ON coffee.farm_point_move (kobo_id, move_id);
CREATE INDEX IF NOT EXISTS farm_point_move_by_idx   ON coffee.farm_point_move (moved_by, moved_at DESC);
GRANT SELECT ON coffee.farm_point_move TO coffee_reader, coffee_editor;

-- the one place a point's position changes: updates the QC row, re-stamps the
-- containing polygon and writes the log entry
CREATE OR REPLACE FUNCTION coffee.move_point(p_kobo_id integer, p_geom geometry) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = coffee, public AS $$
DECLARE old_geom geometry; d real; mid bigint; pg integer; pat timestamptz;
BEGIN
    IF p_geom IS NULL OR ST_IsEmpty(p_geom) THEN RAISE EXCEPTION 'point geometry cannot be empty'; END IF;
    p_geom := ST_SetSRID(ST_Force2D(p_geom), 4326);
    SELECT geom INTO old_geom FROM coffee.farm_point_qc WHERE kobo_id = p_kobo_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'point % not in farm_point_qc', p_kobo_id; END IF;
    IF ST_Equals(old_geom, p_geom) THEN RETURN NULL; END IF;
    d := ST_Distance(old_geom::geography, p_geom::geography);
    IF d > 500 AND current_setting('coffee.allow_far_move', true) IS DISTINCT FROM 'on' THEN
        RAISE EXCEPTION 'refusing to move point % by % m (limit 500 m); a supervisor can SET coffee.allow_far_move = ''on''',
            p_kobo_id, round(d);
    END IF;

    SELECT gid, created_at INTO pg, pat FROM coffee.digitized_polygon d
    WHERE d.geom && p_geom AND ST_Intersects(d.geom, p_geom)
    ORDER BY created_at DESC LIMIT 1;                      -- NULLs when outside every polygon

    UPDATE coffee.farm_point_qc
    SET geom = p_geom, latitude = ST_Y(p_geom), longitude = ST_X(p_geom),
        geom_utm = ST_Transform(p_geom, 32637),
        polygon_gid = pg, digitised_at = pat
    WHERE kobo_id = p_kobo_id;

    INSERT INTO coffee.farm_point_move (kobo_id, geom_before, geom_after, dist_m)
    VALUES (p_kobo_id, old_geom, p_geom, d) RETURNING move_id INTO mid;
    RETURN mid;
END $$;
REVOKE ALL ON FUNCTION coffee.move_point(integer, geometry) FROM PUBLIC;

-- QGIS edits the view; only geom is writable
CREATE OR REPLACE FUNCTION coffee.farm_point_clean_public_upd() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = coffee, public AS $$
BEGIN
    PERFORM coffee.move_point(OLD.kobo_id, NEW.geom);
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS farm_point_clean_public_upd ON coffee.farm_point_clean_public;
CREATE TRIGGER farm_point_clean_public_upd INSTEAD OF UPDATE ON coffee.farm_point_clean_public
    FOR EACH ROW EXECUTE FUNCTION coffee.farm_point_clean_public_upd();
GRANT UPDATE (geom) ON coffee.farm_point_clean_public TO coffee_editor;

-- undo a move (supervisors): puts the point back where it was; logged as a new move
CREATE OR REPLACE FUNCTION coffee.undo_point_move(p_move_id bigint) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = coffee, public AS $$
DECLARE m coffee.farm_point_move; new_id bigint;
BEGIN
    SELECT * INTO m FROM coffee.farm_point_move WHERE move_id = p_move_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'move % not found', p_move_id; END IF;
    IF m.undone_by IS NOT NULL THEN RAISE EXCEPTION 'move % was already undone by %', p_move_id, m.undone_by; END IF;
    IF p_move_id <> (SELECT max(move_id) FROM coffee.farm_point_move WHERE kobo_id = m.kobo_id) THEN
        RAISE EXCEPTION 'move % is not the latest for point %; undo the newer moves first', p_move_id, m.kobo_id;
    END IF;
    PERFORM set_config('coffee.allow_far_move', 'on', true);
    new_id := coffee.move_point(m.kobo_id, m.geom_before);
    UPDATE coffee.farm_point_move SET undoes = p_move_id WHERE move_id = new_id;
    UPDATE coffee.farm_point_move SET undone_by = new_id WHERE move_id = p_move_id;
    IF m.undoes IS NOT NULL THEN
        UPDATE coffee.farm_point_move SET undone_by = NULL WHERE move_id = m.undoes;
    END IF;
    RETURN new_id;
END $$;
REVOKE ALL ON FUNCTION coffee.undo_point_move(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION coffee.undo_point_move(bigint) TO coffee_supervisor;

-- re-apply the current position of every moved point onto a freshly built
-- farm_point_qc (called by build_qc.py right after the copy stage)
CREATE OR REPLACE FUNCTION coffee.reapply_point_moves() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = coffee, public AS $$
DECLARE n integer;
BEGIN
    UPDATE coffee.farm_point_qc q
    SET geom = m.geom_after, latitude = ST_Y(m.geom_after), longitude = ST_X(m.geom_after),
        geom_utm = ST_Transform(m.geom_after, 32637)
    FROM (SELECT DISTINCT ON (kobo_id) kobo_id, geom_after
          FROM coffee.farm_point_move ORDER BY kobo_id, move_id DESC) m
    WHERE q.kobo_id = m.kobo_id AND NOT ST_Equals(q.geom, m.geom_after);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;
