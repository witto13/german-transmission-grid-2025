-- Create PLZ centroid to 110kV bus mapping for LV generator distribution
-- Maps each German PLZ to its nearest 110kV bus in the reduced network

-- Drop existing table if present
DROP TABLE IF EXISTS grid.plz_to_bus_mapping CASCADE;

-- Create the mapping table
CREATE TABLE grid.plz_to_bus_mapping (
    plz varchar(10) PRIMARY KEY,
    bus_id bigint NOT NULL,
    distance_km float,
    plz_centroid_lon float,
    plz_centroid_lat float,
    created_at timestamp DEFAULT now()
);

-- Populate using spatial nearest-neighbor query
-- This finds the nearest 110kV bus to each PLZ centroid
INSERT INTO grid.plz_to_bus_mapping (plz, bus_id, distance_km, plz_centroid_lon, plz_centroid_lat)
SELECT DISTINCT ON (p.plz)
    p.plz,
    b.bus_id,
    ST_Distance(
        ST_Centroid(p.geom)::geography,
        ST_SetSRID(ST_MakePoint(b.x, b.y), 4326)::geography
    ) / 1000 AS distance_km,
    ST_X(ST_Centroid(p.geom)) AS plz_centroid_lon,
    ST_Y(ST_Centroid(p.geom)) AS plz_centroid_lat
FROM public.plz_poly p
CROSS JOIN LATERAL (
    SELECT bus_id, x, y
    FROM grid.egon_etrago_bus
    WHERE scn_name = 'egon2025_prod'
      AND v_nom = 110
      AND country = 'DE'
    ORDER BY ST_SetSRID(ST_MakePoint(x, y), 4326)::geography <-> ST_Centroid(p.geom)::geography
    LIMIT 1
) b
WHERE p.plz IS NOT NULL
  AND p.plz ~ '^\d{5}$';  -- Only valid 5-digit PLZ codes

-- Create indexes
CREATE INDEX idx_plz_bus_mapping_bus ON grid.plz_to_bus_mapping (bus_id);
CREATE INDEX idx_plz_bus_mapping_distance ON grid.plz_to_bus_mapping (distance_km);

-- Analyze for query optimization
ANALYZE grid.plz_to_bus_mapping;

-- Report statistics
SELECT
    'Total PLZ mapped' AS metric,
    COUNT(*)::text AS value
FROM grid.plz_to_bus_mapping
UNION ALL
SELECT
    'Unique buses used',
    COUNT(DISTINCT bus_id)::text
FROM grid.plz_to_bus_mapping
UNION ALL
SELECT
    'Avg distance (km)',
    ROUND(AVG(distance_km)::numeric, 2)::text
FROM grid.plz_to_bus_mapping
UNION ALL
SELECT
    'Max distance (km)',
    ROUND(MAX(distance_km)::numeric, 2)::text
FROM grid.plz_to_bus_mapping
UNION ALL
SELECT
    'PLZ within 5 km',
    COUNT(*)::text
FROM grid.plz_to_bus_mapping
WHERE distance_km <= 5
UNION ALL
SELECT
    'PLZ within 10 km',
    COUNT(*)::text
FROM grid.plz_to_bus_mapping
WHERE distance_km <= 10;
