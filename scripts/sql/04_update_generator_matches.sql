-- Update MaStR generator tables with matching results
-- This propagates the matched bus IDs from the hierarchy view to individual generator tables

-- First, add matching columns to generator tables if they don't exist
DO $$
BEGIN
    -- Add columns to egon_mastr_wind
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'grid' AND table_name = 'egon_mastr_wind'
                   AND column_name = 'matched_bus_id') THEN
        ALTER TABLE grid.egon_mastr_wind
            ADD COLUMN matched_bus_id bigint,
            ADD COLUMN match_method text,
            ADD COLUMN match_distance_km float,
            ADD COLUMN match_confidence float;
    END IF;

    -- Add columns to egon_mastr_combustion
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'grid' AND table_name = 'egon_mastr_combustion'
                   AND column_name = 'matched_bus_id') THEN
        ALTER TABLE grid.egon_mastr_combustion
            ADD COLUMN matched_bus_id bigint,
            ADD COLUMN match_method text,
            ADD COLUMN match_distance_km float,
            ADD COLUMN match_confidence float;
    END IF;

    -- Add columns to egon_mastr_solar
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'grid' AND table_name = 'egon_mastr_solar'
                   AND column_name = 'matched_bus_id') THEN
        ALTER TABLE grid.egon_mastr_solar
            ADD COLUMN matched_bus_id bigint,
            ADD COLUMN match_method text,
            ADD COLUMN match_distance_km float,
            ADD COLUMN match_confidence float;
    END IF;

    -- Add columns to egon_mastr_biomass
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'grid' AND table_name = 'egon_mastr_biomass'
                   AND column_name = 'matched_bus_id') THEN
        ALTER TABLE grid.egon_mastr_biomass
            ADD COLUMN matched_bus_id bigint,
            ADD COLUMN match_method text,
            ADD COLUMN match_distance_km float,
            ADD COLUMN match_confidence float;
    END IF;

    -- Add columns to egon_mastr_hydro
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'grid' AND table_name = 'egon_mastr_hydro'
                   AND column_name = 'matched_bus_id') THEN
        ALTER TABLE grid.egon_mastr_hydro
            ADD COLUMN matched_bus_id bigint,
            ADD COLUMN match_method text,
            ADD COLUMN match_distance_km float,
            ADD COLUMN match_confidence float;
    END IF;
END $$;

-- Update wind generators
UPDATE grid.egon_mastr_wind w
SET
    matched_bus_id = h.matched_bus_id,
    match_method = h.match_method,
    match_distance_km = h.match_distance_km,
    match_confidence = h.match_confidence
FROM grid.mastr_location_hierarchy h
WHERE w."LokationMastrNummer" = h.sel_id
  AND h.matched_bus_id IS NOT NULL;

-- Update combustion generators
UPDATE grid.egon_mastr_combustion c
SET
    matched_bus_id = h.matched_bus_id,
    match_method = h.match_method,
    match_distance_km = h.match_distance_km,
    match_confidence = h.match_confidence
FROM grid.mastr_location_hierarchy h
WHERE c."LokationMastrNummer" = h.sel_id
  AND h.matched_bus_id IS NOT NULL;

-- Update solar generators
UPDATE grid.egon_mastr_solar s
SET
    matched_bus_id = h.matched_bus_id,
    match_method = h.match_method,
    match_distance_km = h.match_distance_km,
    match_confidence = h.match_confidence
FROM grid.mastr_location_hierarchy h
WHERE s."LokationMastrNummer" = h.sel_id
  AND h.matched_bus_id IS NOT NULL;

-- Update biomass generators
UPDATE grid.egon_mastr_biomass b
SET
    matched_bus_id = h.matched_bus_id,
    match_method = h.match_method,
    match_distance_km = h.match_distance_km,
    match_confidence = h.match_confidence
FROM grid.mastr_location_hierarchy h
WHERE b."LokationMastrNummer" = h.sel_id
  AND h.matched_bus_id IS NOT NULL;

-- Update hydro generators
UPDATE grid.egon_mastr_hydro h2
SET
    matched_bus_id = h.matched_bus_id,
    match_method = h.match_method,
    match_distance_km = h.match_distance_km,
    match_confidence = h.match_confidence
FROM grid.mastr_location_hierarchy h
WHERE h2."LokationMastrNummer" = h.sel_id
  AND h.matched_bus_id IS NOT NULL;

-- Report update statistics
SELECT
    'Wind generators updated' AS metric,
    COUNT(*)::text AS value
FROM grid.egon_mastr_wind
WHERE matched_bus_id IS NOT NULL
UNION ALL
SELECT
    'Combustion generators updated',
    COUNT(*)::text
FROM grid.egon_mastr_combustion
WHERE matched_bus_id IS NOT NULL
UNION ALL
SELECT
    'Solar generators updated',
    COUNT(*)::text
FROM grid.egon_mastr_solar
WHERE matched_bus_id IS NOT NULL
UNION ALL
SELECT
    'Biomass generators updated',
    COUNT(*)::text
FROM grid.egon_mastr_biomass
WHERE matched_bus_id IS NOT NULL
UNION ALL
SELECT
    'Hydro generators updated',
    COUNT(*)::text
FROM grid.egon_mastr_hydro
WHERE matched_bus_id IS NOT NULL;
