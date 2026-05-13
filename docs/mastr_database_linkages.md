# MaStR Database Linkages and Data Model

This document describes the complete data model of the Marktstammdatenregister (MaStR) database as stored in the eGon-data PostgreSQL database, including all table relationships, linking chains, and strategies for allocating generators to grid buses.

## Table of Contents

1. [Overview](#overview)
2. [Database Schemas](#database-schemas)
3. [ID Types and Prefixes](#id-types-and-prefixes)
4. [Core Tables](#core-tables)
5. [Linking Chains](#linking-chains)
6. [Voltage Level Classification](#voltage-level-classification)
7. [TSO Control Zones](#tso-control-zones)
8. [Data Coverage Analysis](#data-coverage-analysis)
9. [Pre-processed Grid Tables](#pre-processed-grid-tables)
10. [Spatial Matching Resources](#spatial-matching-resources)
11. [Recommended Matching Strategies](#recommended-matching-strategies)
12. [SQL Query Examples](#sql-query-examples)

---

## Overview

The MaStR (Marktstammdatenregister) is Germany's official registry for energy generation and storage units. The database contains information about:

- **5.8 million solar units** (mostly rooftop PV)
- **41,765 wind turbines** (onshore and offshore)
- **92,837 combustion units** (gas, coal, oil plants)
- **2.3 million storage units** (mostly home batteries)
- **8,792 hydro units**
- **23,907 biomass units**
- **6 nuclear units** (all shut down)

The data is stored in the `mastr` schema with additional pre-processed tables in the `grid` schema.

---

## Database Schemas

### `mastr` Schema - Raw MaStR Data
Contains the original MaStR export data with German column names.

| Table | Records | Description |
|-------|---------|-------------|
| `wind_extended` | 41,765 | Wind turbine units |
| `solar_extended` | 5,853,282 | Solar/PV units |
| `combustion_extended` | 92,837 | Thermal power plants |
| `hydro_extended` | 8,792 | Hydropower units |
| `biomass_extended` | 23,907 | Biomass plants |
| `storage_extended` | 2,324,959 | Battery/pumped storage |
| `nuclear_extended` | 6 | Nuclear plants |
| `gsgk_extended` | 325 | Geothermal, solar thermal, mine gas, sewage gas |
| `grid_connections` | 5,410,901 | Grid connection points with voltage levels |
| `locations_extended` | 6,198,730 | Technical locations (SEL) |
| `grids` | 1,740 | Grid/network definitions |
| `market_actors` | - | Grid operators, plant operators |
| `balancing_area` | - | Balancing areas (Bilanzierungsgebiete) |

### `grid` Schema - Pre-processed Tables
Contains filtered and enriched MaStR data ready for bus matching.

| Table | Records | Description |
|-------|---------|-------------|
| `egon_mastr_wind` | 31,974 | Wind units for eGon2025 scenario |
| `egon_mastr_solar` | 349 | HV-connected solar units |
| `egon_mastr_combustion` | 81,469 | Combustion units |
| `egon_mastr_hydro` | 8,598 | Hydro units |
| `egon_mastr_biomass` | - | Biomass units |
| `egon_mastr_grid_connection` | - | Grid connection data |
| `egon_mastr_location` | - | Location data |
| `plz_to_bus_mapping` | 8,201 | Postal code to bus mapping |
| `egon_ehv_substation` | 458 | Extra-high voltage substations |
| `egon_hvmv_substation` | 4,037 | High/medium voltage substations |

---

## ID Types and Prefixes

MaStR uses a consistent ID scheme with prefixes indicating the entity type:

| Prefix | German Name | English | Example |
|--------|-------------|---------|---------|
| **SEE** | Stromerzeugungseinheit | Electricity generation unit | `SEE948219812307` |
| **SEL** | Stromeinspeise-Lokation | Electricity feed-in location | `SEL906984105062` |
| **SAN** | Strom-Anschluss-Nummer | Grid connection point | `SAN921662892546` |
| **SNE** | Stromnetz | Electricity grid | `SNE974913162356` |
| **SNB** | Stromnetzbetreiber | Grid operator | `SNB982046657236` |
| **ABR** | Anlagenbetreiber | Plant operator | `ABR111222333123` |
| **SPE** | Speichereinheit | Storage unit | `SPE...` |
| **GNE** | Gasnetz | Gas grid | `GNE932542987757` |

### ID Relationships

```
Generator Unit (SEE)
    │
    ├── LokationMastrNummer ──► Location (SEL)
    │                              │
    │                              ├── Netzanschlusspunkte ──► Grid Connection Point (SAN)
    │                              │
    │                              └── via grid_connections table
    │                                      │
    │                                      ├── Spannungsebene (voltage level)
    │                                      ├── NetzMastrNummer ──► Grid (SNE)
    │                                      └── NetzbetreiberMastrNummer ──► Operator (SNB)
    │
    ├── NetzbetreiberMastrNummer ──► Grid Operator (SNB)
    │
    └── AnlagenbetreiberMastrNummer ──► Plant Operator (ABR)
```

---

## Core Tables

### Generator Extended Tables (`mastr.*_extended`)

All generator tables share a common structure with technology-specific additions.

#### Common Columns (all generator types)

| Column | Type | Description |
|--------|------|-------------|
| `EinheitMastrNummer` | text | **Primary key** - Unique unit ID (SEE...) |
| `LokationMastrNummer` | text | **Foreign key** - Links to grid_connections (SEL...) |
| `Laengengrad` | double | Longitude (WGS84) |
| `Breitengrad` | double | Latitude (WGS84) |
| `Gemeindeschluessel` | text | Municipality code (8 digits) |
| `Postleitzahl` | text | Postal code (5 digits) |
| `Bundesland` | text | Federal state |
| `Landkreis` | text | District |
| `Gemeinde` | text | Municipality name |
| `Nettonennleistung` | double | Net rated capacity in **kW** |
| `Bruttoleistung` | double | Gross capacity in kW |
| `Inbetriebnahmedatum` | text | Commissioning date |
| `EinheitBetriebsstatus` | text | Operational status |
| `EinheitSystemstatus` | text | System status |
| `AnschlussAnHoechstOderHochSpannung` | double | Flag: 1=HV/eHV connected, 0=not |
| `Energietraeger` | text | Energy carrier/fuel type |
| `NetzbetreiberMastrNummer` | text | Grid operator ID |
| `Weic` | text | ENTSO-E W-EIC code |
| `Kraftwerksnummer` | text | Power plant number (BNA) |

#### Operational Status Values (`EinheitBetriebsstatus`)

| Value | Meaning | Include in Model? |
|-------|---------|-------------------|
| `In Betrieb` | Operational | **Yes** |
| `In Planung` | Planned/under construction | Depends on target year |
| `Vorübergehend stillgelegt` | Temporarily shut down | Maybe (reserve) |
| `Endgültig stillgelegt` | Permanently shut down | No |

#### Technology-Specific Columns

**Wind (`wind_extended`):**
- `NameWindpark` - Wind park name
- `Nabenhoehe` - Hub height (m)
- `Rotordurchmesser` - Rotor diameter (m)
- `Hersteller` - Manufacturer
- `Typenbezeichnung` - Turbine model
- `Lage` - Location type (Land/See)
- `ClusterNordsee`, `ClusterOstsee` - Offshore cluster

**Solar (`solar_extended`):**
- `Lage` - Installation type (Gebäude, Freifläche, etc.)
- `Hauptausrichtung` - Main orientation (S, SW, etc.)
- `HauptausrichtungNeigungswinkel` - Tilt angle
- `AnzahlModule` - Number of modules

**Combustion (`combustion_extended`):**
- `NameKraftwerk` - Power plant name
- `NameKraftwerksblock` - Block name
- `Hauptbrennstoff` - Primary fuel
- `Technologie` - Technology (GT, CCGT, etc.)

**Storage (`storage_extended`):**
- `Batterietechnologie` - Battery technology
- `Pumpspeichertechnologie` - Pumped storage type
- `NutzbareSpeicherkapazitaet` - Usable storage capacity

---

### Grid Connections Table (`mastr.grid_connections`)

The central table linking generators to the grid with voltage level information.

| Column | Type | Description |
|--------|------|-------------|
| `LokationMastrNummer` | text | **Primary key** - Location ID (SEL...) |
| `NetzanschlusspunktMastrNummer` | text | Connection point ID (SAN...) |
| `NetzMastrNummer` | text | Grid ID (SNE...) |
| `NetzbetreiberMaStRNummer` | text | Grid operator ID (SNB...) |
| `Spannungsebene` | text | **Voltage level** (critical!) |
| `RegelzoneNetzanschlusspunkt` | text | TSO control zone ID |
| `MaximaleEinspeiseleistung` | double | Max feed-in capacity (kW) |
| `Netzanschlusskapazitaet` | double | Grid connection capacity |
| `NochInPlanung` | bigint | Still in planning phase |

#### Voltage Level Values (`Spannungsebene`)

| Value | Voltage | Records | Description |
|-------|---------|---------|-------------|
| `Höchstspannung` | 220/380 kV | 362 | Extra-high voltage (eHV) |
| `Hochspannung` | 110 kV | 2,975 | High voltage (HV) |
| `Umspannebene Höchstspannung/Hochspannung` | 220-110 kV | 74 | eHV/HV transformer |
| `Umspannebene Hochspannung/Mittelspannung` | 110-20 kV | 1,856 | HV/MV transformer |
| `Mittelspannung` | 10-30 kV | 82,109 | Medium voltage (MV) |
| `Umspannebene Mittelspannung/Niederspannung` | 20-0.4 kV | 22,089 | MV/LV transformer |
| `Niederspannung (= Hausanschluss/Haushaltsstrom)` | 230/400 V | 5,300,348 | Low voltage (LV) |

---

### Grids Table (`mastr.grids`)

Information about electricity grids and their operators.

| Column | Type | Description |
|--------|------|-------------|
| `MastrNummer` | text | Grid ID (SNE...) |
| `Bezeichnung` | text | Grid name |
| `Sparte` | text | Sector (Strom/Gas) |
| `Bundesland` | text | Federal state |
| `Bilanzierungsgebiete` | text | Balancing area codes |

#### Major Grid Operators (HV/eHV connections)

| Operator | HV Connections |
|----------|----------------|
| Avacon Netz GmbH | 395 |
| Netze BW GmbH | 309 |
| Westnetz | 240 |
| Amprion GmbH (TSO) | 118 |
| 50Hertz Transmission GmbH (TSO) | 108 |
| TenneT TSO GmbH (TSO) | 81 |
| TransnetBW (TSO) | 50 |

---

### Locations Extended Table (`mastr.locations_extended`)

Technical location information linking units to connection points.

| Column | Type | Description |
|--------|------|-------------|
| `MastrNummer` | text | Location ID (SEL...) |
| `NameDerTechnischenLokation` | text | Technical location name |
| `VerknuepfteEinheiten` | text | Linked unit IDs (comma-separated SEE...) |
| `Netzanschlusspunkte` | text | Connection point IDs (SAN...) |
| `Lokationtyp` | text | Location type |

---

## Linking Chains

### Primary Chain: Generator → Voltage Level

```sql
Generator (SEE)
    │
    │  JOIN ON LokationMastrNummer
    ▼
grid_connections (SEL)
    │
    ├── Spannungsebene ──► Voltage Level (110/220/380 kV)
    ├── RegelzoneNetzanschlusspunkt ──► TSO Zone
    └── NetzMastrNummer ──► Grid/DSO
```

**SQL Example:**
```sql
SELECT
    w.EinheitMastrNummer,
    w.Nettonennleistung,
    w.Laengengrad,
    w.Breitengrad,
    gc.Spannungsebene,
    gc.RegelzoneNetzanschlusspunkt
FROM mastr.wind_extended w
INNER JOIN mastr.grid_connections gc
    ON w.LokationMastrNummer = gc.LokationMastrNummer
WHERE gc.Spannungsebene IN ('Hochspannung', 'Höchstspannung');
```

### Secondary Chain: Generator → Grid Operator

```sql
Generator (SEE)
    │
    │  JOIN ON NetzbetreiberMastrNummer
    ▼
market_actors (SNB)
    │
    ├── Firmenname ──► Company name
    ├── Netz ──► Grid ID (SNE...)
    └── AcerCode ──► ACER registration
```

### Tertiary Chain: Location → Connection Points

```sql
locations_extended (SEL)
    │
    │  Netzanschlusspunkte contains SAN IDs
    ▼
grid_connections (SAN)
    │
    └── Multiple connection points per location possible
```

---

## Voltage Level Classification

### Mapping MaStR Voltage Levels to Grid Model

| MaStR `Spannungsebene` | Target `v_nom` | Bus Type |
|------------------------|----------------|----------|
| `Höchstspannung` | 380 or 220 | eHV bus |
| `Hochspannung` | 110 | HV bus |
| `Umspannebene Höchstspannung/Hochspannung` | 380/220 or 110 | Transformer bus |
| `Umspannebene Hochspannung/Mittelspannung` | 110 | HV/MV transformer |
| `Mittelspannung` | Aggregate to 110 | Via HV/MV transformer |
| `Niederspannung` | Aggregate to 110 | Via distribution |

### Capacity by Voltage Level (Operational Units)

#### Wind Power
| Voltage Level | Units | Capacity (GW) | % of Total |
|---------------|-------|---------------|------------|
| Hochspannung (110 kV) | 9,534 | 28.0 | 36% |
| Höchstspannung (220/380 kV) | 2,423 | 17.6 | 23% |
| Mittelspannung (MV) | 14,684 | 31.2 | 40% |
| Umspannebene HV/MV | 3,834 | 9.7 | 12% |
| **Total HV/eHV** | **11,957** | **45.6** | **58%** |

#### Solar Power
| Voltage Level | Units | Capacity (GW) | % of Total |
|---------------|-------|---------------|------------|
| Niederspannung (LV) | 4,886,921 | 51.4 | 49% |
| Mittelspannung (MV) | 70,914 | 32.0 | 31% |
| Hochspannung (110 kV) | 1,074 | 7.7 | 7% |
| Höchstspannung (220/380 kV) | 50 | 0.9 | 1% |
| **Total HV/eHV** | **1,124** | **8.6** | **8%** |

#### Combustion (Thermal)
| Voltage Level | Units | Capacity (GW) | % of Total |
|---------------|-------|---------------|------------|
| Höchstspannung (220/380 kV) | 166 | 46.1 | 59% |
| Hochspannung (110 kV) | 995 | 45.4 | 58% |
| Mittelspannung (MV) | 12,343 | 14.0 | 18% |
| **Total HV/eHV** | **1,161** | **91.5** | **~100%** |

---

## TSO Control Zones

### Regelzone Codes

| Code | TSO | Coverage |
|------|-----|----------|
| `1000001` | 50Hertz | East Germany (Brandenburg, Saxony, etc.) |
| `1000010` | Amprion | West Germany (NRW, Rhineland-Palatinate, etc.) |
| `1001564` | TenneT | North/Central Germany (Lower Saxony, Bavaria, etc.) |
| `1001572` | TransnetBW | Baden-Württemberg |

### HV Connection Distribution by TSO

```sql
SELECT
    RegelzoneNetzanschlusspunkt,
    COUNT(*) as connections
FROM mastr.grid_connections
WHERE Spannungsebene IN ('Hochspannung', 'Höchstspannung')
GROUP BY RegelzoneNetzanschlusspunkt;
```

| TSO Zone | HV/eHV Connections |
|----------|-------------------|
| 50Hertz (1000001) | 1,278 |
| Amprion (1000010) | 700 |
| TenneT (1001564) | 948 |
| TransnetBW (1001572) | 411 |

---

## Data Coverage Analysis

### Generators with Grid Connection Data

| Technology | Total Units | With SEL | Coverage |
|------------|-------------|----------|----------|
| Wind | 41,765 | 31,545 | 76% |
| Solar | 5,853,282 | 4,980,861 | 85% |
| Combustion | 92,837 | 75,723 | 82% |
| Hydro | 8,792 | 7,842 | 89% |
| Storage | 2,324,959 | 1,987,432 | 85% |

### Generators WITHOUT Grid Connection (NULL SEL)

| Status | Wind Units | Wind GW | Reason |
|--------|------------|---------|--------|
| In Planung | 7,193 | 43.8 | Not yet connected |
| Endgültig stillgelegt | 2,225 | 2.6 | Historical |
| In Betrieb | 795 | 3.2 | Data quality issue |

### Coordinate Coverage (HV/eHV Units)

| Technology | HV/eHV Units | With Coordinates | Coverage |
|------------|--------------|------------------|----------|
| Wind | 11,957 | 11,956 | 99.99% |
| Combustion | 1,161 | 1,135 | 97.8% |
| Solar | 1,124 | 1,120 | 99.6% |
| Hydro | 453 | 450 | 99.3% |

---

## Pre-processed Grid Tables

### `grid.egon_mastr_*` Tables

These tables contain filtered MaStR data for the eGon2025 scenario with additional columns for bus matching:

| Column | Type | Description |
|--------|------|-------------|
| `scn_name` | text | Scenario name (eGon2025) |
| `voltage_level_gc` | text | Voltage level from grid_connections |
| `substation_name` | text | Matched substation name |
| `matched_bus_id` | text | **Target: bus ID to populate** |
| `match_method` | text | **Target: matching method used** |
| `match_distance_km` | text | **Target: distance to matched bus** |

**Note:** The `matched_bus_id`, `match_method`, and `match_distance_km` columns are currently empty and need to be populated by the bus allocation process.

### `grid.plz_to_bus_mapping`

Pre-computed mapping from postal codes to nearest buses.

| Column | Type | Description |
|--------|------|-------------|
| `plz` | varchar(10) | Postal code (primary key) |
| `bus_id` | bigint | Nearest bus ID |
| `distance_km` | double | Distance to bus |
| `plz_centroid_lon` | double | PLZ centroid longitude |
| `plz_centroid_lat` | double | PLZ centroid latitude |

**Coverage:** 8,201 postal codes mapped

### `grid.egon_ehv_substation` / `grid.egon_hvmv_substation`

Substation data from OSM with names and coordinates.

| Column | Type | Description |
|--------|------|-------------|
| `bus_id` | integer | Bus ID |
| `lon`, `lat` | double | Coordinates |
| `voltage` | text | Voltage level(s) |
| `subst_name` | text | Substation name |
| `operator` | text | Operator name |
| `osm_id` | text | OpenStreetMap ID |
| `point` | geometry | Point geometry |
| `polygon` | geometry | Polygon geometry |

**Coverage:**
- eHV substations: 458
- HV/MV substations: 4,037

---

## Spatial Matching Resources

### Available in Database

1. **Bus Coordinates** (`grid.egon_etrago_bus`)
   - 14,494 buses with x, y coordinates
   - 110 kV: 11,312 buses
   - 220 kV: 1,275 buses
   - 380 kV: 1,907 buses

2. **Substation Geometries** (`grid.egon_ehv_substation`, `grid.egon_hvmv_substation`)
   - Point and polygon geometries
   - Names for text matching

3. **Municipal Boundaries** (`boundaries.vg250_gem`)
   - 11,000+ municipalities with polygons
   - `ags` column = Gemeindeschlüssel (matches MaStR)

4. **PLZ Mapping** (`grid.plz_to_bus_mapping`)
   - Direct PLZ → bus lookup

### PostGIS Functions for Matching

```sql
-- Distance between generator and bus (in km)
ST_Distance(
    ST_SetSRID(ST_MakePoint(generator_lon, generator_lat), 4326)::geography,
    ST_SetSRID(ST_MakePoint(bus_x, bus_y), 4326)::geography
) / 1000 AS distance_km

-- Find nearest bus
ORDER BY ST_Distance(...) LIMIT 1

-- Point in polygon (municipality)
ST_Contains(municipality.geometry, ST_SetSRID(ST_MakePoint(lon, lat), 4326))
```

---

## Recommended Matching Strategies

### Strategy 1: Voltage-Filtered Nearest Bus (Primary)

**Best for:** HV/eHV generators with coordinates

```sql
-- For each HV generator, find nearest bus at same voltage level
SELECT DISTINCT ON (g.einheitmastrnummer)
    g.einheitmastrnummer,
    b.bus_id,
    ST_Distance(
        ST_SetSRID(ST_MakePoint(g.laengengrad, g.breitengrad), 4326)::geography,
        ST_SetSRID(ST_MakePoint(b.x, b.y), 4326)::geography
    ) / 1000 AS distance_km
FROM grid.egon_mastr_wind g
JOIN grid.egon_etrago_bus b ON b.scn_name = 'eGon2025'
WHERE g.voltage_level_gc = 'Hochspannung' AND b.v_nom = 110
ORDER BY g.einheitmastrnummer, distance_km;
```

### Strategy 2: PLZ-Based Matching (Fallback)

**Best for:** Generators without coordinates

```sql
SELECT
    g.einheitmastrnummer,
    p.bus_id,
    p.distance_km
FROM grid.egon_mastr_wind g
JOIN grid.plz_to_bus_mapping p ON g.postleitzahl = p.plz;
```

### Strategy 3: Substation Name Matching

**Best for:** Large power plants with known names

```sql
-- Match power plant names to substation names
SELECT
    c.einheitmastrnummer,
    c.namekraftwerk,
    s.bus_id,
    s.subst_name
FROM grid.egon_mastr_combustion c
JOIN grid.egon_ehv_substation s
    ON LOWER(c.namekraftwerk) LIKE '%' || LOWER(s.subst_name) || '%'
WHERE c.voltage_level_gc = 'Höchstspannung';
```

### Strategy 4: TSO Zone Constraint

**Best for:** Ensuring generators connect to correct TSO area

```sql
-- Only match to buses in same TSO zone
-- (Requires TSO zone boundaries or bus-to-TSO mapping)
```

### Strategy 5: Municipality-Based (Gemeindeschlüssel)

**Best for:** Regional aggregation

```sql
SELECT
    g.einheitmastrnummer,
    m.ags,
    m.gen as municipality_name
FROM grid.egon_mastr_wind g
JOIN boundaries.vg250_gem m ON g.gemeindeschluessel = m.ags;
```

---

## SQL Query Examples

### 1. Get All HV/eHV Wind with Grid Connection Data

```sql
SELECT
    w."EinheitMastrNummer" as unit_id,
    w."LokationMastrNummer" as location_id,
    w."Nettonennleistung" / 1000 as capacity_mw,
    w."Laengengrad" as lon,
    w."Breitengrad" as lat,
    w."Gemeindeschluessel" as municipality_code,
    w."Postleitzahl" as plz,
    gc."Spannungsebene" as voltage_level,
    gc."RegelzoneNetzanschlusspunkt" as tso_zone,
    gc."NetzMastrNummer" as grid_id
FROM mastr.wind_extended w
INNER JOIN mastr.grid_connections gc
    ON w."LokationMastrNummer" = gc."LokationMastrNummer"
WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
  AND gc."Spannungsebene" IN ('Hochspannung', 'Höchstspannung');
```

### 2. Aggregate Capacity by Voltage Level and Technology

```sql
SELECT
    'Wind' as technology,
    COALESCE(gc."Spannungsebene", 'NO_CONNECTION') as voltage_level,
    COUNT(DISTINCT w."EinheitMastrNummer") as units,
    ROUND((SUM(w."Nettonennleistung")/1000000)::numeric, 2) as capacity_gw
FROM mastr.wind_extended w
LEFT JOIN mastr.grid_connections gc
    ON w."LokationMastrNummer" = gc."LokationMastrNummer"
WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
GROUP BY gc."Spannungsebene"
ORDER BY capacity_gw DESC;
```

### 3. Find Generators Near a Specific Bus

```sql
SELECT
    w."EinheitMastrNummer",
    w."Nettonennleistung" / 1000 as mw,
    ST_Distance(
        ST_SetSRID(ST_MakePoint(w."Laengengrad", w."Breitengrad"), 4326)::geography,
        ST_SetSRID(ST_MakePoint(9.728737, 54.290219), 4326)::geography
    ) / 1000 AS distance_km
FROM mastr.wind_extended w
WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
  AND w."Laengengrad" IS NOT NULL
ORDER BY distance_km
LIMIT 20;
```

### 4. Get TSO Distribution for HV Generators

```sql
SELECT
    CASE gc."RegelzoneNetzanschlusspunkt"
        WHEN '1000001' THEN '50Hertz'
        WHEN '1000001.0' THEN '50Hertz'
        WHEN '1000010' THEN 'Amprion'
        WHEN '1000010.0' THEN 'Amprion'
        WHEN '1001564' THEN 'TenneT'
        WHEN '1001564.0' THEN 'TenneT'
        WHEN '1001572' THEN 'TransnetBW'
        WHEN '1001572.0' THEN 'TransnetBW'
        ELSE 'Unknown'
    END as tso,
    COUNT(*) as connections,
    ROUND((SUM(w."Nettonennleistung")/1000000)::numeric, 2) as capacity_gw
FROM mastr.wind_extended w
INNER JOIN mastr.grid_connections gc
    ON w."LokationMastrNummer" = gc."LokationMastrNummer"
WHERE gc."Spannungsebene" IN ('Hochspannung', 'Höchstspannung')
GROUP BY gc."RegelzoneNetzanschlusspunkt"
ORDER BY capacity_gw DESC;
```

### 5. Match Large Power Plants to Substations

```sql
SELECT
    c."EinheitMastrNummer",
    c."NameKraftwerk",
    c."Nettonennleistung" / 1000 as capacity_mw,
    c."Laengengrad",
    c."Breitengrad",
    s.bus_id,
    s.subst_name,
    ST_Distance(
        ST_SetSRID(ST_MakePoint(c."Laengengrad", c."Breitengrad"), 4326)::geography,
        s.point::geography
    ) / 1000 AS distance_km
FROM mastr.combustion_extended c
CROSS JOIN LATERAL (
    SELECT bus_id, subst_name, point
    FROM grid.egon_ehv_substation
    ORDER BY ST_Distance(
        ST_SetSRID(ST_MakePoint(c."Laengengrad", c."Breitengrad"), 4326),
        point
    )
    LIMIT 1
) s
WHERE c."EinheitBetriebsstatus" = 'In Betrieb'
  AND c."Nettonennleistung" > 100000  -- > 100 MW
  AND c."Laengengrad" IS NOT NULL
ORDER BY c."Nettonennleistung" DESC
LIMIT 50;
```

---

## Appendix: Complete Table Schemas

### mastr.wind_extended (Key Columns)

```
EinheitMastrNummer          | text    | Unit ID (SEE...)
LokationMastrNummer         | text    | Location ID (SEL...)
Laengengrad                 | double  | Longitude
Breitengrad                 | double  | Latitude
Gemeindeschluessel          | text    | Municipality code
Postleitzahl                | text    | Postal code
Bundesland                  | text    | Federal state
Nettonennleistung           | double  | Capacity (kW)
Inbetriebnahmedatum         | text    | Commissioning date
EinheitBetriebsstatus       | text    | Operational status
AnschlussAnHoechstOderHochSpannung | double | HV flag
NameWindpark                | text    | Wind park name
Nabenhoehe                  | double  | Hub height (m)
Rotordurchmesser            | double  | Rotor diameter (m)
Hersteller                  | text    | Manufacturer
Lage                        | text    | Location (Land/See)
```

### mastr.grid_connections (All Columns)

```
NetzanschlusspunktMastrNummer        | text    | Connection point (SAN...)
NetzanschlusspunktBezeichnung        | text    | Connection point name
LetzteAenderung                      | text    | Last modified
LokationMastrNummer                  | text    | Location ID (SEL...)
Lokationtyp                          | text    | Location type
MaximaleEinspeiseleistung            | double  | Max feed-in (kW)
Gasqualitaet                         | text    | Gas quality
NetzMastrNummer                      | text    | Grid ID (SNE...)
NochInPlanung                        | bigint  | Still planned
NameDerTechnischenLokation           | text    | Technical location name
MaximaleAusspeiseleistung            | double  | Max withdrawal (kW)
Messlokation                         | text    | Metering location
Spannungsebene                       | text    | Voltage level
BilanzierungsgebietNetzanschlusspunktId | double | Balancing area ID
Nettoengpassleistung                 | double  | Net bottleneck capacity
Netzanschlusskapazitaet              | double  | Connection capacity
RegelzoneNetzanschlusspunkt          | text    | TSO control zone
DatenQuelle                          | text    | Data source
DatumDownload                        | text    | Download date
NetzbetreiberMaStRNummer             | text    | Operator ID (SNB...)
```

---

## References

- [Marktstammdatenregister (MaStR)](https://www.marktstammdatenregister.de/)
- [eGon-data Documentation](https://egon-data.readthedocs.io/)
- [Energy Charts - Fraunhofer ISE](https://www.energy-charts.info/)
- [Bundesnetzagentur](https://www.bundesnetzagentur.de/)

---

*Document created: 2026-02-05*
*Data source: eGon-data PostgreSQL database (MaStR download: 2026-01-24)*
