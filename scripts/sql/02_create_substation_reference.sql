-- Create unified substation reference view for the reduced network (egon2025_prod)
-- This provides a single lookup table for all named substations

-- Drop existing view if present
DROP VIEW IF EXISTS grid.substation_reference CASCADE;

-- Create substation reference from bus metadata
CREATE OR REPLACE VIEW grid.substation_reference AS
SELECT
    bm.bus_id,
    bm.v_nom AS voltage_kv,
    bm.lon,
    bm.lat,
    bm.subst_name,
    bm.subst_type,
    bm.operator,
    bm.tso_zone,
    bm.osm_id,
    bm.bnetza_id,
    -- Normalized name for matching (remove common prefixes/suffixes)
    lower(trim(regexp_replace(
        regexp_replace(
            regexp_replace(bm.subst_name,
                '^(Umspannwerk|UW|Umspannanlage|Station|Schaltanlage|Netzanschluss)\s*', '', 'i'),
            '\s+(380|220|110)\s*kV.*$', '', 'i'),
        '\s+', ' ', 'g'
    ))) AS normalized_name
FROM grid.egon_bus_metadata bm
WHERE bm.scn_name = 'egon2025_prod'
  AND bm.subst_name IS NOT NULL
  AND bm.subst_name != ''
  AND bm.subst_name != 'NA'
  AND bm.subst_name !~ '^\s*$';

-- Create index on the base table for efficient lookups
CREATE INDEX IF NOT EXISTS idx_bus_meta_subst_name_lower
    ON grid.egon_bus_metadata (lower(subst_name))
    WHERE scn_name = 'egon2025_prod' AND subst_name IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_bus_meta_voltage_prod
    ON grid.egon_bus_metadata (v_nom)
    WHERE scn_name = 'egon2025_prod';

CREATE INDEX IF NOT EXISTS idx_bus_meta_coords_prod
    ON grid.egon_bus_metadata (lon, lat)
    WHERE scn_name = 'egon2025_prod';

-- Report statistics
SELECT
    'Total substations with names' AS metric,
    COUNT(*)::text AS value
FROM grid.substation_reference
UNION ALL
SELECT
    'Unique normalized names',
    COUNT(DISTINCT normalized_name)::text
FROM grid.substation_reference
UNION ALL
SELECT
    'By voltage: 110 kV',
    COUNT(*)::text
FROM grid.substation_reference
WHERE voltage_kv = 110
UNION ALL
SELECT
    'By voltage: 220 kV',
    COUNT(*)::text
FROM grid.substation_reference
WHERE voltage_kv = 220
UNION ALL
SELECT
    'By voltage: 380 kV',
    COUNT(*)::text
FROM grid.substation_reference
WHERE voltage_kv = 380;
