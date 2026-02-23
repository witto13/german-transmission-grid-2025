-- Delete OSM Raw Data from PostgreSQL Database
-- This will free up ~224 GB of space
-- The grid model is already extracted and safe in the 'grid' schema

-- OSM node/way tables
DROP TABLE IF EXISTS public.nodes CASCADE;
DROP TABLE IF EXISTS public.way_nodes CASCADE;
DROP TABLE IF EXISTS public.osm_ways CASCADE;
DROP TABLE IF EXISTS public.osm_nodes CASCADE;
DROP TABLE IF EXISTS public.osm_polygon CASCADE;
DROP TABLE IF EXISTS public.ways CASCADE;
DROP TABLE IF EXISTS public.osm_line CASCADE;
DROP TABLE IF EXISTS public.osm_point CASCADE;
DROP TABLE IF EXISTS public.relation_members CASCADE;
DROP TABLE IF EXISTS public.osm_roads CASCADE;
DROP TABLE IF EXISTS public.osm_rels CASCADE;
DROP TABLE IF EXISTS public.relations CASCADE;

-- OSM power infrastructure tables
DROP TABLE IF EXISTS public.power_ways CASCADE;
DROP TABLE IF EXISTS public.power_line CASCADE;
DROP TABLE IF EXISTS public.power_substation CASCADE;
DROP TABLE IF EXISTS public.power_circ_members CASCADE;
DROP TABLE IF EXISTS public.power_line_sep CASCADE;
DROP TABLE IF EXISTS public.edit_power_relations CASCADE;
DROP TABLE IF EXISTS public.power_relations CASCADE;
DROP TABLE IF EXISTS public.power_circuits CASCADE;

-- OSM processing/intermediate tables
DROP TABLE IF EXISTS public.branch_data CASCADE;
DROP TABLE IF EXISTS public.bus_data CASCADE;
DROP TABLE IF EXISTS public.dcline_data CASCADE;
DROP TABLE IF EXISTS public.transfer_busses_complete CASCADE;
DROP TABLE IF EXISTS public.transfer_busses CASCADE;
DROP TABLE IF EXISTS public.transfer_busses_connect_all CASCADE;

-- Geographic/boundary tables (keep plz_poly and nuts_poly as they might be useful)
-- DROP TABLE IF EXISTS public.plz_poly CASCADE;
-- DROP TABLE IF EXISTS public.nuts_poly CASCADE;

-- Other OSM processing tables
DROP TABLE IF EXISTS public.problem_log CASCADE;

-- Show database size before deletion
\echo 'Database size BEFORE deletion:'
SELECT pg_size_pretty(pg_database_size('egon-data')) as database_size;

\echo 'Starting deletion of OSM tables...'
\echo 'This may take several minutes but should not hang.'
\echo ''

-- Vacuum individual large tables (optional, faster than VACUUM FULL)
-- Run this AFTER confirming deletions worked, not during the same transaction
-- VACUUM ANALYZE public.grid.egon_etrago_bus;
-- VACUUM ANALYZE public.grid.egon_etrago_line;

-- Show database size after deletion
\echo 'Database size AFTER deletion:'
SELECT pg_size_pretty(pg_database_size('egon-data')) as database_size;

\echo ''
\echo 'Remaining tables in public schema:'
SELECT
    schemaname,
    COUNT(*) as tables,
    pg_size_pretty(SUM(pg_total_relation_size(schemaname||'.'||tablename))) as total_size
FROM pg_tables
WHERE schemaname = 'public'
GROUP BY schemaname;

\echo ''
\echo 'NOTE: Disk space may not be immediately freed. To reclaim space, run separately:'
\echo '  VACUUM;  (takes a few minutes)'
\echo 'NOT recommended: VACUUM FULL (takes hours and locks database)'
