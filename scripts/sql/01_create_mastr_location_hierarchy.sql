-- Create SEL → SAN hierarchy view with coordinates for generator matching
-- This materializes the relationship between MaStR locations (SEL) and grid connections (SAN)
-- Coordinates are aggregated from the unit tables (wind, solar, etc.)

-- Drop existing view if present
DROP MATERIALIZED VIEW IF EXISTS grid.mastr_location_hierarchy CASCADE;

-- Create the hierarchy view
CREATE MATERIALIZED VIEW grid.mastr_location_hierarchy AS
WITH hv_connections AS (
    -- Filter to HV/eHV connections only from mastr.grid_connections
    SELECT
        san."NetzanschlusspunktMastrNummer" AS san_id,
        san."LokationMastrNummer" AS sel_id,
        san."NetzanschlusspunktBezeichnung" AS san_name,
        san."Spannungsebene" AS voltage_raw,
        san."Nettoengpassleistung" AS bottleneck_kw,
        san."NetzMastrNummer" AS grid_operator_id,
        san."NameDerTechnischenLokation" AS sel_name_from_san,
        -- Normalize voltage to kV
        CASE
            WHEN san."Spannungsebene" = 'Höchstspannung' THEN 380
            WHEN san."Spannungsebene" = 'Hochspannung' THEN 110
            WHEN san."Spannungsebene" = 'Umspannebene Höchstspannung/Hochspannung' THEN 220
            WHEN san."Spannungsebene" = 'Umspannebene Hochspannung/Mittelspannung' THEN 110
            ELSE NULL
        END AS voltage_kv
    FROM mastr.grid_connections san
    WHERE san."Spannungsebene" IN (
        'Höchstspannung',
        'Hochspannung',
        'Umspannebene Höchstspannung/Hochspannung',
        'Umspannebene Hochspannung/Mittelspannung'
    )
),
-- Get coordinates from wind units (most reliable for HV connections)
wind_coords AS (
    SELECT
        "LokationMastrNummer" AS sel_id,
        AVG("Laengengrad") AS lon,
        AVG("Breitengrad") AS lat,
        MAX("Postleitzahl") AS plz,
        MAX("Ort") AS city,
        MAX("Bundesland") AS state
    FROM mastr.wind_extended
    WHERE "Laengengrad" IS NOT NULL
      AND "Breitengrad" IS NOT NULL
      AND "Laengengrad" BETWEEN 5.0 AND 16.0
      AND "Breitengrad" BETWEEN 47.0 AND 56.0
    GROUP BY "LokationMastrNummer"
),
-- Get coordinates from solar units
solar_coords AS (
    SELECT
        "LokationMastrNummer" AS sel_id,
        AVG("Laengengrad") AS lon,
        AVG("Breitengrad") AS lat,
        MAX("Postleitzahl") AS plz,
        MAX("Ort") AS city,
        MAX("Bundesland") AS state
    FROM mastr.solar_extended
    WHERE "Laengengrad" IS NOT NULL
      AND "Breitengrad" IS NOT NULL
      AND "Laengengrad" BETWEEN 5.0 AND 16.0
      AND "Breitengrad" BETWEEN 47.0 AND 56.0
    GROUP BY "LokationMastrNummer"
),
-- Get coordinates from combustion/conventional units
combustion_coords AS (
    SELECT
        "LokationMastrNummer" AS sel_id,
        AVG("Laengengrad") AS lon,
        AVG("Breitengrad") AS lat,
        MAX("Postleitzahl") AS plz,
        MAX("Ort") AS city,
        MAX("Bundesland") AS state
    FROM mastr.combustion_extended
    WHERE "Laengengrad" IS NOT NULL
      AND "Breitengrad" IS NOT NULL
      AND "Laengengrad" BETWEEN 5.0 AND 16.0
      AND "Breitengrad" BETWEEN 47.0 AND 56.0
    GROUP BY "LokationMastrNummer"
),
-- Combine coordinates with priority: wind > combustion > solar
combined_coords AS (
    SELECT DISTINCT ON (sel_id)
        sel_id,
        lon, lat, plz, city, state
    FROM (
        SELECT sel_id, lon, lat, plz, city, state, 1 AS priority FROM wind_coords
        UNION ALL
        SELECT sel_id, lon, lat, plz, city, state, 2 AS priority FROM combustion_coords
        UNION ALL
        SELECT sel_id, lon, lat, plz, city, state, 3 AS priority FROM solar_coords
    ) all_coords
    ORDER BY sel_id, priority
),
-- Get location names from mastr.locations_extended
locations AS (
    SELECT
        "MastrNummer" AS sel_id,
        "NameDerTechnischenLokation" AS sel_name
    FROM mastr.locations_extended
),
-- Get grid operator names
grid_operators AS (
    SELECT
        "MastrNummer" AS grid_id,
        "Bezeichnung" AS operator_name
    FROM mastr.grids
)
SELECT
    -- SEL (Location) Level
    COALESCE(loc.sel_id, hv.sel_id) AS sel_id,
    COALESCE(loc.sel_name, hv.sel_name_from_san) AS sel_name,

    -- SAN (Grid Connection) Level
    hv.san_id,
    hv.san_name,
    hv.voltage_raw,
    hv.voltage_kv,
    hv.bottleneck_kw,

    -- Grid Operator
    go.operator_name AS grid_operator_name,

    -- Location coordinates (from unit tables)
    cc.lon,
    cc.lat,
    cc.plz,
    cc.city,
    cc.state,

    -- Matching columns (to be populated by Python script)
    NULL::bigint AS matched_bus_id,
    NULL::text AS match_method,
    NULL::float AS match_distance_km,
    NULL::float AS match_confidence

FROM hv_connections hv
LEFT JOIN locations loc ON hv.sel_id = loc.sel_id
LEFT JOIN grid_operators go ON hv.grid_operator_id = go.grid_id
LEFT JOIN combined_coords cc ON hv.sel_id = cc.sel_id;

-- Create indexes for efficient lookups
CREATE INDEX idx_mlh_san_name ON grid.mastr_location_hierarchy (lower(san_name)) WHERE san_name IS NOT NULL;
CREATE INDEX idx_mlh_sel_name ON grid.mastr_location_hierarchy (lower(sel_name)) WHERE sel_name IS NOT NULL;
CREATE INDEX idx_mlh_voltage ON grid.mastr_location_hierarchy (voltage_kv);
CREATE INDEX idx_mlh_san_id ON grid.mastr_location_hierarchy (san_id);
CREATE INDEX idx_mlh_sel_id ON grid.mastr_location_hierarchy (sel_id);
CREATE INDEX idx_mlh_plz ON grid.mastr_location_hierarchy (plz) WHERE plz IS NOT NULL;
CREATE INDEX idx_mlh_coords ON grid.mastr_location_hierarchy (lon, lat) WHERE lon IS NOT NULL AND lat IS NOT NULL;

-- Analyze for query optimization
ANALYZE grid.mastr_location_hierarchy;

-- Report statistics
SELECT
    'Total grid connections' AS metric,
    COUNT(*)::text AS value
FROM grid.mastr_location_hierarchy
UNION ALL
SELECT
    'With SAN name',
    COUNT(*)::text
FROM grid.mastr_location_hierarchy
WHERE san_name IS NOT NULL AND san_name != ''
UNION ALL
SELECT
    'With SEL name',
    COUNT(*)::text
FROM grid.mastr_location_hierarchy
WHERE sel_name IS NOT NULL AND sel_name != ''
UNION ALL
SELECT
    'With coordinates',
    COUNT(*)::text
FROM grid.mastr_location_hierarchy
WHERE lon IS NOT NULL AND lat IS NOT NULL
UNION ALL
SELECT
    'With PLZ',
    COUNT(*)::text
FROM grid.mastr_location_hierarchy
WHERE plz IS NOT NULL
UNION ALL
SELECT
    'By voltage: 110 kV',
    COUNT(*)::text
FROM grid.mastr_location_hierarchy
WHERE voltage_kv = 110
UNION ALL
SELECT
    'By voltage: 220 kV',
    COUNT(*)::text
FROM grid.mastr_location_hierarchy
WHERE voltage_kv = 220
UNION ALL
SELECT
    'By voltage: 380 kV',
    COUNT(*)::text
FROM grid.mastr_location_hierarchy
WHERE voltage_kv = 380;
