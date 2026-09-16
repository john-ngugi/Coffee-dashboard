-- Clean points only, PII stripped, for QGIS digitisers.
-- build_qc.py recreates this on every full run; apply by hand once on a
-- database restored from a dump that predates it:
--   sudo docker compose exec -T db psql -U postgres -d coffee_eudr < deploy/sql/farm_point_clean_public.sql
CREATE OR REPLACE VIEW coffee.farm_point_clean_public AS
SELECT kobo_id, uuid, county_form, subcounty, ward, grower_type, grower_name, factory_name, grower_code, estate_name, estate_code, estate_to_society, society_supplied_to, parcel_number, trees_total, trees_ruiru11, trees_batian, trees_sl28, trees_sl34, trees_k7, trees_robusta, trees_bluemountain, harvest_quantity, variety, planting_year, certification, submitted_by, submission_time, form_version, latitude, longitude, geom, geom_utm, county_gis, county_match, in_target_county, dist_road_m, road_type, road_name, cluster_id, cluster_size, dup_group_size, nbrs_50m, flags, status, implied_ha, density_used, area_basis, trees_breakdown_sum, warnings, county_raw, consent_raw, consented, available_raw, available, other_member_society, respondent_gender, respondent_age_range, respondent_education, harvest_month, processing, validation_status, coord_valid, qa_flag, farm_shape, kobo_status, polygon_gid, digitised_at
FROM coffee.farm_point_qc
WHERE status = 'clean';

GRANT SELECT ON coffee.farm_point_clean_public TO coffee_reader, coffee_editor;
