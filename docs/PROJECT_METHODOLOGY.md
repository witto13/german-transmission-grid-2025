# eGon2025: Building a PyPSA Power Flow Model of the German Transmission Grid

**Target Year**: 2025
**Voltage Levels**: 110 kV / 220 kV / 380 kV (HV/eHV only)
**Final Network**: 7,316 buses, 10,863 lines, 535 transformers, 24,972 generators, 7,256 loads
**Tool Stack**: PostgreSQL 16 + PostGIS, PyPSA 0.20.1, Python 3.10, Docker

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Infrastructure Setup](#2-infrastructure-setup)
3. [Grid Topology: From OpenStreetMap to eTraGo](#3-grid-topology-from-openstreetmap-to-etrago)
4. [Network Reduction Pipeline](#4-network-reduction-pipeline)
   - [Stage 1: Conservative Clustering (V1 to V2)](#stage-1-conservative-clustering-v1--v2)
   - [Stage 2: Voltage-Specific Clustering (V2 to V3)](#stage-2-voltage-specific-clustering-v2--v3)
   - [Stage 3: Degree-2 Bus Elimination (V3 to V4)](#stage-3-degree-2-bus-elimination-v3--v4)
   - [Stage 4: Substation Proximity Merge (V4 to V5)](#stage-4-substation-proximity-merge-v4--v5)
   - [Stage 5: Parallel Line Capping (V5 to V6)](#stage-5-parallel-line-capping-v5--v6)
5. [Generator Mapping from MaStR](#5-generator-mapping-from-mastr)
   - [Data Acquisition](#51-data-acquisition)
   - [Filtering and Carrier Classification](#52-filtering-and-carrier-classification)
   - [Voltage Level Assignment](#53-voltage-level-assignment)
   - [Spatial Matching to Grid Buses](#54-spatial-matching-to-grid-buses)
   - [Municipality Distribution for Distributed Generation](#55-municipality-distribution-for-distributed-generation)
   - [Aggregation and Database Insertion](#56-aggregation-and-database-insertion)
6. [Load Mapping by Municipality](#6-load-mapping-by-municipality)
   - [Demand Data Sources](#61-demand-data-sources)
   - [Disaggregation to Municipalities](#62-disaggregation-to-municipalities)
   - [Sector-Based Voltage Assignment](#63-sector-based-voltage-assignment)
   - [Spatial Matching and Aggregation](#64-spatial-matching-and-aggregation)
7. [Power Flow Simulation](#7-power-flow-simulation)
   - [Network Assembly](#71-network-assembly)
   - [Synthetic Time Series](#72-synthetic-time-series)
   - [LOPF Execution and Results](#73-lopf-execution-and-results)
8. [Results and Outputs](#8-results-and-outputs)
9. [File Reference](#9-file-reference)

---

## 1. Project Overview

This project constructs a research-ready PyPSA power system model of the German high-voltage transmission grid for the year 2025. The model covers all three transmission voltage levels (110, 220, 380 kV) and includes:

- **Grid topology** derived from OpenStreetMap via the eGon-data pipeline
- **Generation fleet** mapped from the official German power plant registry (MaStR)
- **Electricity demand** disaggregated from national totals to 11,135 municipalities
- **Power flow analysis** using linear optimal power flow (LOPF)

The grid topology is progressively reduced from the raw OSM extraction (14,494 buses, 26,489 lines) through a 5-stage pipeline to a computationally tractable model (7,316 buses, 10,863 lines) while preserving all substations, transformers, and electrical equivalence.

---

## 2. Infrastructure Setup

### Database

The project uses a PostgreSQL 16 database with PostGIS spatial extensions, running in a Docker container on a Hetzner cloud server.

| Parameter | Value |
|-----------|-------|
| Container | `egon-data-local-database-container` |
| Image | `postgis/postgis:16-3` |
| Host / Port | `127.0.0.1:59734` |
| Database | `egon-data` |
| Credentials | `egon` / `data` |
| Total size | ~234 GB (mostly OSM raw data; active data ~10 GB) |

**Key schemas:**
- `grid` -- Network components (buses, lines, transformers, generators, loads)
- `mastr` -- MaStR power plant registry (31 tables, 36.4 million records)
- `boundaries` -- Geographic boundaries (municipalities, districts, states)
- `osmtgmod_results` -- OSM topology modeling intermediate results

### Python Environment

```
conda environment: egon2025 (Python 3.10)
Key packages: pypsa 0.20.1, pandas 2.3.3, geopandas 1.1.2, scipy, sqlalchemy, psycopg2
Solver: CBC (coin-or-cbc 2.10.12)
```

---

## 3. Grid Topology: From OpenStreetMap to eTraGo

The starting grid topology comes from the **eGon-data** pipeline, an open-source project that extracts German power grid topology from OpenStreetMap and processes it through **osmTGmod** (a topology modeling tool for transmission grids).

### What eGon-data Provides

The pipeline downloads raw OSM data for Germany (~224 GB), identifies power infrastructure elements (substations, lines, cables, transformers), and constructs a topological network model stored in the `grid` schema following the eTraGo convention.

**Initial extraction (eGon2025 scenario):**

| Component | Count | Notes |
|-----------|-------|-------|
| Buses | 14,494 | Substations and connection points |
| Lines | 26,489 | Overhead lines and underground cables |
| Transformers | 535 | Voltage level interconnections |
| Substations | 5,389 | OSM-tagged substation nodes |

**Voltage distribution of buses:**
- 110 kV: 11,312 (78%)
- 220 kV: 1,275 (9%)
- 380 kV: 1,907 (13%)

### Known Issues with Raw OSM Topology

OpenStreetMap is a crowdsourced dataset and the raw extraction contains topology artifacts:
- **27.5% of lines** are under 200m long (many are OSM modeling artifacts, not real circuits)
- Dense clustering in urban areas with multiple buses per physical substation location
- Intermediate nodes along line routes that serve no electrical purpose (pass-through points)
- Excessive parallel lines between the same bus pairs (up to 8+ in some corridors)

These artifacts inflate the network size without adding electrical information, making power flow analysis unnecessarily expensive. The reduction pipeline addresses each category.

---

## 4. Network Reduction Pipeline

The reduction proceeds through five stages, each targeting a specific class of topology artifact. Every stage preserves all 535 transformers, all 5,389 substation buses, network connectivity (single connected component), and electrical equivalence through proper impedance aggregation.

### Reduction Summary

| Stage | Method | Buses | Lines | Key Parameter |
|-------|--------|-------|-------|---------------|
| V1 (original) | -- | 14,494 | 26,489 | -- |
| V2 | Conservative clustering | 11,575 | 22,072 | 120m radius |
| V3 | Voltage-specific clustering | 9,234 | 16,700 | 1200m/250m |
| V4 | Degree-2 elimination | 7,458 | 12,102 | Series merge |
| V5 | Substation proximity | 7,316 | 11,728 | 300m to substation |
| V6 | Parallel line capping | 7,316 | 10,863 | Max 2 parallels |

**Total reduction: 49.5% of buses removed, 59.0% of lines removed.**

---

### Stage 1: Conservative Clustering (V1 &rarr; V2)

**Goal:** Merge buses that sit within 120 meters of each other -- these are almost always OSM artifacts where a single physical location is represented by multiple nodes.

**Algorithm:** PostGIS `ST_ClusterDBSCAN` in EPSG:3035 (ETRS89 LAEA Europe) for meter-accurate distance computation.

**Parameters:**
- Clustering radius: **120m** for all voltage levels
- Minimum cluster size: 2 buses

**Protection rules:**
- Clusters containing 2+ substation buses are skipped (never merge distinct substations)
- Clusters containing 2+ transformer buses are skipped
- Within valid clusters, the **substation bus** becomes the keeper; if none, the **transformer bus**; otherwise the first bus

**Merge process:**
1. Identify spatial clusters using DBSCAN
2. Filter out clusters violating protection rules
3. For each valid cluster: redirect all line and transformer endpoints from merged buses to the keeper bus
4. Delete merged buses and any resulting self-loop lines

**Results:**
- 2,919 buses removed (20.1% reduction)
- Ultra-short lines (<100m) reduced by 91.6% (2,456 &rarr; 207)
- All substations and transformers preserved

**Script:** `reduce_network.py`

---

### Stage 2: Voltage-Specific Clustering (V2 &rarr; V3)

**Goal:** Apply more aggressive clustering with voltage-appropriate radii. The 380 kV and 220 kV networks are geographically sparse, so a wider radius is safe. The 110 kV network is denser and requires a tighter radius.

**Why cascading (V1&rarr;V2&rarr;V3) instead of direct (V1&rarr;V3)?** Direct application of the 1200m radius to the raw network created too many clusters containing multiple protected buses, most of which had to be skipped. By first cleaning the dense sub-120m clusters in V2, the V3 stage finds cleaner, more actionable clusters.

**Parameters:**
- 380 kV: **1,200m** radius
- 220 kV: **1,200m** radius
- 110 kV: **250m** radius

**Enhanced keeper selection:** Among regular (non-substation, non-transformer) buses in a cluster, the bus with the **highest degree** (most line connections) is chosen as keeper. This preserves the most-connected topology node.

**Results:**
- 2,341 additional buses removed (20.2% of V2)
- Cumulative from V1: 5,260 buses removed (36.3%)
- 380 kV and 220 kV networks reduced by ~37% each (sparse networks, wide radius effective)

**Script:** `reduce_network_v3.py`

---

### Stage 3: Degree-2 Bus Elimination (V3 &rarr; V4)

**Goal:** Remove pass-through buses. A degree-2 bus connects exactly two lines in series; it adds no branching or switching capability and exists only because OSM drew an intermediate node along a transmission corridor. These buses can be eliminated by merging their two incident lines into a single equivalent line.

**Algorithm:**

The pipeline has three components:

1. **Parallel Compression** -- Groups lines by bus pair and computes equivalent parallel impedance:
   - `r_eq = 1 / sum(1/r_i)` (parallel resistance)
   - `x_eq = 1 / sum(1/x_i)` (parallel reactance)
   - `s_nom_eq = sum(s_nom_i)` (capacities add)

2. **Chain Detection** -- On the compressed topology, finds maximal chains of degree-2 buses: sequences `[A, B1, B2, ..., Bn, Z]` where all B_i have degree 2 and A, Z have degree != 2 or are protected.

3. **Series Merge** -- For each chain, computes a single equivalent line from A to Z:
   - `r_eq = sum(r_segment)` (series resistance)
   - `x_eq = sum(x_segment)` (series reactance)
   - `s_nom_eq = min(s_nom_segment)` (bottleneck capacity)
   - `length_eq = sum(length_segment)` (total length)

**Protected buses (never eliminated):**
- All substation buses (queried from `osmtgmod_results.bus_data`)
- All transformer buses

**Validation:** After elimination, the script verifies that the number of connected components is unchanged and all substations remain reachable from each other.

**Results:**
- ~1,776 buses removed (degree-2 nodes along corridors)
- ~4,598 lines removed and replaced with fewer, longer equivalent lines

**Script:** `scripts/reduction/v4/pipeline.py`, `scripts/reduction/v4/degree2_elimination.py`

---

### Stage 4: Substation Proximity Merge (V4 &rarr; V5)

**Goal:** Eliminate non-substation 110 kV buses that sit very close to actual substations. These are OSM artifacts where a line enters a substation via one or more intermediate nodes just outside the substation boundary.

**Criteria:**
- Voltage: 110 kV only
- Not a substation bus
- Not a transformer bus
- Within **300m** of a substation bus (EPSG:3035)

**Process:** Each qualifying bus is merged into its nearest substation bus. Line and transformer endpoints are redirected; self-loops are deleted.

**Results:**
- 142 buses removed
- All substations and transformers preserved

**Script:** `reduce_network_v5.py`

---

### Stage 5: Parallel Line Capping (V5 &rarr; V6)

**Goal:** Limit parallel lines between any bus pair to a maximum of 2 circuits. Real-world transmission corridors typically have at most double-circuit overhead lines. Higher parallel counts in the model are artifacts from merging adjacent buses in previous stages.

**Selection:** When a bus pair has 3+ parallel lines, keep the 2 with the highest `s_nom` (capacity). Delete the rest.

**Implementation:** SQL window function ranks lines within each canonical bus pair `(min(bus0,bus1), max(bus0,bus1))` by `s_nom DESC`, then deletes all rows with rank > 2.

**Results:**
- 865 lines removed (0 buses removed)
- All bus pairs now have at most 2 parallel lines

**Script:** `reduce_network_v6.py`

---

## 5. Generator Mapping from MaStR

The Marktstammdatenregister (MaStR) is Germany's official power plant registry. Every generation unit in Germany -- from a 3 kW rooftop solar panel to a 1,400 MW lignite block -- is registered here. The generator mapping script extracts operational units, classifies them by technology, assigns them to voltage levels, spatially matches them to grid buses, and inserts aggregated generator records into the eTraGo database.

### 5.1 Data Acquisition

**Source:** MaStR bulk data export (downloaded January 24, 2026)

**Import:** CSV files were loaded into a PostgreSQL `mastr` schema containing 31 tables with 36.4 million records. Key tables:

| Table | Records | Content |
|-------|---------|---------|
| `wind_extended` | 41,765 | Wind turbines with location, capacity, on/offshore flag |
| `solar_extended` | 5,853,282 | Solar PV units with location, capacity, module type |
| `combustion_extended` | 92,837 | Thermal plants with fuel type, capacity |
| `hydro_extended` | 8,792 | Hydroelectric plants with type (run-of-river, reservoir) |
| `biomass_extended` | 23,907 | Biomass/biogas plants with fuel type |
| `storage_extended` | 2,324,959 | Battery and pumped hydro storage |
| `grid_connections` | 5,400,000 | Links units to grid connection points with voltage level |

The `grid_connections` table is critical: it provides the voltage level (`Spannungsebene`) at which each unit connects to the grid.

### 5.2 Filtering and Carrier Classification

**Operational status filter:** Only units with `EinheitBetriebsstatus = 'In Betrieb'` (operational). Excludes planned, decommissioned, and temporarily shut-down units.

**Carrier mapping** translates MaStR fuel/technology fields into PyPSA carrier names:

| MaStR Field | Value | PyPSA Carrier |
|-------------|-------|---------------|
| `Hauptbrennstoff` (combustion) | Erdgas, Grubengas | `gas` |
| | Steinkohle | `coal` |
| | Rohbraunkohlen, Braunkohle | `lignite` |
| | Heizoel | `oil` |
| | Abfall (Hausmull) | `waste` |
| | Wasserstoff | `hydrogen` |
| `Hauptbrennstoff` (biomass) | Biogas, Biomethan, Klargas, Deponiegas | `biogas` |
| | Feste Biomasse, Holzgas | `biomass` |
| `ArtDerWasserkraftanlage` (hydro) | Laufwasseranlage | `run_of_river` |
| | Speicherwasseranlage | `reservoir` |
| `WindAnLandOderAufSee` (wind) | Windkraft an Land | `onwind` |
| | Windkraft auf See | `offwind` |
| (solar) | All PV | `solar` |

**Capacity conversion:** MaStR stores capacity in kW (`Nettonennleistung`). All values are converted to MW: `p_nom = kW / 1000`.

### 5.3 Voltage Level Assignment

Generators are assigned to voltage levels using capacity-based thresholds from **Hulk et al. (2017)** ("Allocation of annual electricity consumption and power generation capacities across multiple voltage levels in a high spatial resolution", *Int. J. Sustainable Energy Planning and Management*, 13:79-92).

| Capacity | Assigned Voltage |
|----------|-----------------|
| > 120 MW | 380 kV (extra-high voltage) |
| > 20 MW | 220 kV (high voltage) |
| &le; 20 MW | 110 kV (sub-transmission) |

Where available, the MaStR `Spannungsebene` field is used directly. The Hulk thresholds serve as the fallback for units without explicit voltage information.

### 5.4 Spatial Matching to Grid Buses

For generators with coordinates (99%+ of wind, ~98% of conventional, ~99% of hydro), a **KD-tree nearest-neighbor search** matches each generator to its closest grid bus at the target voltage level.

**Implementation:** `scipy.spatial.cKDTree` operating on km-scaled coordinates (lat &times; 111 km/deg, lon &times; 71.5 km/deg at 52&deg;N).

**Search parameters:**

| Voltage | Max Search Radius | Tight Threshold |
|---------|-------------------|-----------------|
| 380 kV | 50 km | 5 km |
| 220 kV | 30 km | 3 km |
| 110 kV | 20 km | 2 km |

**Matching priority:**
1. Find the 5 nearest buses at the target voltage
2. Prefer **substation buses** (from `egon_ehv_substation` / `egon_hvmv_substation` tables) within 5 km
3. If no match at target voltage, fall back to nearest bus at **any** HV/eHV voltage within 100 km (with a confidence penalty)

**Confidence scoring:** Each match receives a score (0-1) based on:
- Distance to bus (closer = higher)
- Whether voltage level matched directly or via fallback
- Whether the matched bus is a substation

### 5.5 Municipality Distribution for Distributed Generation

A large fraction of solar PV installations (particularly rooftop systems) lack precise coordinates. These are handled through municipality-based aggregation:

1. Query generators without valid coordinates from MaStR
2. Group by municipality (`Gemeindeschluessel` / AGS code) and carrier
3. Sum capacity within each municipality-carrier group
4. Compute the municipality centroid from the `boundaries.vg250_gem` table using `ST_Centroid(ST_Union(geometry))`
5. Spatially match the centroid point to the nearest grid bus at the appropriate voltage

This preserves the geographical distribution of generation across Germany without assuming specific locations within municipalities.

### 5.6 Aggregation and Database Insertion

After spatial matching, generators are **aggregated by bus and carrier**: all units of the same technology connected to the same bus become a single PyPSA generator with summed capacity.

**Example:** 47 individual solar PV units totaling 12.3 MW all matched to bus 5432 become one generator record:
```
generator_id=1234, bus=5432, carrier='solar', p_nom=12.3
```

**Database insertion:** Records are written to `grid.egon_etrago_generator` with all PyPSA-required columns (control='PQ', sign=1, p_min_pu=0, p_max_pu=1, etc.).

### Final Generator Statistics

| Carrier | Generators | Installed Capacity (GW) |
|---------|-----------|------------------------|
| Solar | 15,928 | 104.6 |
| Onshore wind | 2,346 | 68.2 |
| Gas | 771 | 33.9 |
| Coal | 35 | 14.2 |
| Lignite | 18 | 14.0 |
| Offshore wind | 10 | 9.5 |
| Biogas | 3,157 | 7.1 |
| Oil | 440 | 5.3 |
| Other | 154 | 4.8 |
| Run-of-river | 1,267 | 4.2 |
| Waste | 90 | 1.8 |
| Biomass | 652 | 1.8 |
| Reservoir hydro | 103 | 1.1 |
| Hydrogen | 1 | 0.001 |
| **Total** | **24,972** | **270.4** |

**Script:** `scripts/generator_mapping.py`

---

## 6. Load Mapping by Municipality

Germany's total electricity demand of 448 TWh/year is disaggregated to individual municipalities, split by sector, assigned to voltage levels, and spatially matched to grid buses.

### 6.1 Demand Data Sources

| Data | Source | Resolution |
|------|--------|------------|
| National sector totals | BDEW / BNetzA 2025 | Germany-wide |
| Household demand by NUTS-3 | DemandRegio (FFE Munich) | 401 NUTS-3 regions |
| CTS and industry | Population-proportional allocation | 401 NUTS-3 regions |
| Municipality boundaries | BKG VG250 (`boundaries.vg250_gem`) | 11,135 municipalities |

**National totals:**
- Households: 134 TWh (30%)
- Commerce/Trade/Services (CTS): 124 TWh (28%)
- Industry: 190 TWh (42%)
- **Total: 448 TWh**

### 6.2 Disaggregation to Municipalities

The disaggregation follows a three-step process:

**Step 1: NUTS-3 level allocation**

Household demand comes directly from DemandRegio (2015 baseline, scaled by factor 1.015 to match 134 TWh 2025 target). CTS and industry are distributed across NUTS-3 regions proportional to population.

```
NUTS3_hh = DemandRegio_hh_2015 * (134 TWh / 132 TWh)
NUTS3_cts = (NUTS3_population / DE_population) * 124 TWh
NUTS3_ind = (NUTS3_population / DE_population) * 190 TWh
```

**Note on industry allocation:** Using population as a proxy for industrial demand is a first-order approximation. Real industrial demand is concentrated at specific sites (steel, chemicals, refineries). A dedicated large-consumer module is available as an option.

**Step 2: Municipality disaggregation within NUTS-3 regions**

Within each NUTS-3 region, demand is distributed to municipalities proportional to their land area:

```
municipality_share = municipality_area_km2 / NUTS3_total_area_km2
municipality_demand = NUTS3_demand * municipality_share
```

**Step 3: NUTS code remapping**

Two NUTS-3 codes required manual remapping between the 2016 (DemandRegio) and 2021 (database) classification systems:
- `DEB1C` &rarr; `DEB16` (Cochem-Zell, 89 municipalities)
- `DEB1D` &rarr; `DEB19` (Rhein-Hunsruck-Kreis, 137 municipalities)

**Output:** `demand_by_municipality_2025.csv` with 11,135 rows containing household, CTS, and industry demand per municipality.

### 6.3 Sector-Based Voltage Assignment

Each municipality creates up to **two load points**, differentiated by sector:

**1. Residential + CTS load &rarr; always 110 kV**

Households and commercial buildings connect at low/medium voltage. In this HV-only model, they aggregate to the nearest 110 kV bus.

**2. Industrial load &rarr; voltage depends on peak demand**

Peak demand is estimated from annual energy using a **peak factor of 1.49** (derived from Germany's 76 GW system peak divided by 51.1 GW average load).

```
peak_MW = (annual_MWh / 8760) * 1.49
```

| Industrial Peak | Voltage |
|-----------------|---------|
| > 120 MW | 380 kV |
| > 20 MW | 220 kV |
| &le; 20 MW | 110 kV |

These are the same Hulk et al. (2017) thresholds used for generators, ensuring consistency in the model.

### 6.4 Spatial Matching and Aggregation

Municipality centroids are computed from the `boundaries.vg250_gem` table using PostGIS:

```sql
SELECT ags,
       ST_X(ST_Centroid(ST_Union(geometry))) AS lon,
       ST_Y(ST_Centroid(ST_Union(geometry))) AS lat
FROM boundaries.vg250_gem
GROUP BY ags
```

The `ST_Union` handles municipalities with multiple geometry entries (exclaves).

Each load point is matched to the nearest grid bus at its assigned voltage using the same KD-tree spatial matcher used for generators. Search radii: 50 km (110 kV), 75 km (220 kV), 100 km (380 kV).

Multiple load points mapping to the same bus are aggregated (summed).

### Final Load Statistics

| Voltage | Loads | Peak (GW) | Annual (TWh) | Share |
|---------|-------|-----------|-------------|-------|
| 110 kV | 7,123 | 64.7 | 380.2 | 84.9% |
| 220 kV | 102 | 5.2 | 31.0 | 6.9% |
| 380 kV | 21 | 6.3 | 36.8 | 8.2% |
| **Total** | **7,246** | **76.2** | **448.0** | **100%** |

**By sector:**
- Residential + CTS: 3,621 loads, 43.9 GW peak
- Industry: 3,625 loads, 32.3 GW peak

**Validation:** Total annual demand matches national target (448 TWh). Total peak (76.2 GW) is consistent with historical German system peak.

**Script:** `scripts/load_mapping.py`

---

## 7. Power Flow Simulation

With topology, generators, and loads in the database, a linear optimal power flow (LOPF) was run for a single April day to verify the model produces physically reasonable results.

### 7.1 Network Assembly

The script loads all components from the database and builds a PyPSA network object:

```python
n = pypsa.Network()
n.set_snapshots(pd.date_range('2025-04-15 00:00', '2025-04-15 23:00', freq='h'))
# Add buses, lines, transformers, generators, loads from DB
```

**Pre-run fixes required:**

1. **Line electrical parameters:** The `eGon2025` scenario had `x = r = s_nom = 0` for all lines (only topology was populated). Parameters were copied from the `eGon2025v6` scenario which had proper values from the reduction pipeline:

```sql
UPDATE grid.egon_etrago_line a
SET x=b.x, r=b.r, g=b.g, b=b.b, s_nom=b.s_nom, length=b.length
FROM grid.egon_etrago_line b
WHERE a.line_id=b.line_id AND a.scn_name='eGon2025' AND b.scn_name='eGon2025v6'
```

2. **Transformer reactance:** All transformers had `x = 0` in the database. A nominal value of `x = 0.01` (per-unit) was set for all transformers with `s_nom > 0`.

3. **Marginal costs:** All generators had `marginal_cost = 0` in the database. Merit-order costs were set in the script:

| Carrier | Marginal Cost (EUR/MWh) |
|---------|------------------------|
| Solar, Wind, Run-of-river | 0 |
| Reservoir hydro | 5 |
| Waste | 15 |
| Biogas | 25 |
| Biomass | 30 |
| Lignite | 35 |
| Coal | 45 |
| Gas | 55 |
| Other | 60 |
| Oil | 80 |
| Hydrogen | 100 |

4. **Line constraints disabled:** `s_max_pu = 1e6` on all lines (effectively unconstrained). This is a pure dispatch + flow calculation, not a congestion management study.

### 7.2 Synthetic Time Series

Since the database contains no time-series data, synthetic hourly profiles were created for one day:

**Date chosen:** April 15, 2025 (Tuesday) -- moderate solar, moderate wind, typical spring day.

**Solar capacity factor profile:** Bell curve peaking at 0.40 at 13:00, zero before 06:00 and after 20:00. Represents a typical partly-cloudy April day in Germany.

**Onshore wind capacity factor:** Gentle diurnal variation, average ~0.25. Slightly higher in the afternoon (typical thermally-driven pattern).

**Offshore wind capacity factor:** Moderate and steady, average ~0.35. Less diurnal variation than onshore.

**Load profile:** Typical German weekday shape (ENTSO-E pattern):
- Night minimum: ~54% of peak (00:00-05:00)
- Morning ramp: 58-96% (06:00-10:00)
- Midday plateau: 92-100% (11:00-17:00)
- Evening decline: 67-95% (18:00-23:00)

Each load's time series is its `p_set` peak value multiplied by the hourly profile factor.

### 7.3 LOPF Execution and Results

**Solver:** CBC (3 minutes solve time for 24 snapshots, ~25k generators, ~7k loads, ~11k lines)

**Dispatch results (April 15, 2025):**

| Carrier | 00:00 | 06:00 | 12:00 | 18:00 | Daily GWh |
|---------|-------|-------|-------|-------|-----------|
| Solar | 0 | 0 | 40,257 | 11,952 | 325.5 |
| Onshore wind | 15,007 | 15,689 | 21,829 | 17,054 | 403.8 |
| Offshore wind | 3,144 | 3,239 | 3,716 | 3,239 | 78.7 |
| Lignite | 13,961 | 13,961 | **0** | 13,961 | 248.9 |
| Coal | 2,137 | 3,646 | **0** | 13,952 | 94.8 |
| Gas | ~0 | ~0 | ~0 | ~0 | 19.0 |
| Must-run (hydro+bio) | 11,923 | 11,923 | 9,400 | 11,923 | ~274 |
| **Total** | **47,245** | **49,531** | **76,201** | **73,153** | **1,469.9** |
| **Load** | **47,245** | **49,531** | **76,201** | **73,153** | **1,469.9** |

**Key observations:**
- **Merit order works correctly:** Renewables dispatched first (zero cost), then lignite (35), coal (45), gas (55, barely used)
- **At noon:** Solar + wind produce ~66 GW, displacing all coal and lignite. Demand fully met by renewables + must-run
- **At night:** Without solar, lignite and coal fill the 17 GW gap between wind output and demand
- **Zero curtailment** -- all available renewable generation was used
- **107 lines overloaded at noon** (up to 802% on 110 kV lines) -- expected since lines are unconstrained; this highlights where real bottlenecks would be
- **Total system cost:** 18.8 million EUR for the day, average 12.8 EUR/MWh

---

## 8. Results and Outputs

### Power Flow Output Files

| File | Content |
|------|---------|
| `results/powerflow_april15.nc` | Full PyPSA network with dispatch and flow results (netCDF) |
| `results/dispatch_april15.csv` | Hourly generation dispatch by carrier |
| `results/line_loading_april15.csv` | Per-line loading statistics (mean, max, s_nom) |
| `results/powerflow_map.html` | Interactive HTML map (3.4 MB, self-contained) |

### Interactive Map Features

The map (`powerflow_map.html`) provides:
- **Hour slider** (0-23) with play/pause animation
- **Voltage level toggles** (110/220/380 kV) to show/hide lines
- **Lines colored by loading** (green &lt;25%, yellow &lt;50%, orange &lt;75%, red &lt;100%, purple &gt;100%)
- **Left sidebar** with:
  - Generation dispatch by carrier (real-time as hour changes)
  - System load bar (GW and % of peak)
  - Line loading statistics (mean, max, count overloaded)
- **Hover tooltips** on each line showing flow (MW), capacity (MVA), and loading (%)

---

## 9. File Reference

### Scripts

| File | Purpose |
|------|---------|
| `reduce_network.py` | V1&rarr;V2: Conservative 120m clustering |
| `reduce_network_v3.py` | V2&rarr;V3: Voltage-specific clustering |
| `scripts/reduction/v4/pipeline.py` | V3&rarr;V4: Degree-2 elimination |
| `reduce_network_v5.py` | V4&rarr;V5: Substation proximity merge |
| `reduce_network_v6.py` | V5&rarr;V6: Parallel line capping |
| `scripts/generator_mapping.py` | MaStR &rarr; eTraGo generator mapping |
| `scripts/load_mapping.py` | Municipality demand &rarr; eTraGo loads |
| `scripts/utils/spatial_matching.py` | KD-tree bus matching utility |
| `scripts/utils/name_matching.py` | Fuzzy substation name matching |
| `scripts/run_powerflow.py` | LOPF execution for April 15 |
| `scripts/create_powerflow_map.py` | Interactive result map generator |

### Data Files

| File | Content |
|------|---------|
| `demand_by_municipality_2025.csv` | 11,135 municipality demand records |
| `demand_by_nuts3_2025.csv` | 401 NUTS-3 aggregated demand |
| `buses_v6.csv` | Final bus topology (7,316 buses) |
| `lines_v6.csv` | Final line topology (10,863 lines) |
| `transformers_v6.csv` | Transformer data (535 units) |
| `data/mastr/*.csv` | MaStR raw data exports |

### Reduction Metadata

| File | Content |
|------|---------|
| `reduction_info.json` | V1&rarr;V2 merge details (507 KB) |
| `reduction_info_v3.json` | V2&rarr;V3 merge details (499 KB) |
| `reduction_info_v4.json` | V3&rarr;V4 chain details (85 KB) |
| `reduction_info_v5.json` | V4&rarr;V5 proximity merges (16 KB) |
| `reduction_info_v6.json` | V5&rarr;V6 parallel caps (153 KB) |

### References

- Hulk, L. et al. (2017). "Allocation of annual electricity consumption and power generation capacities across multiple voltage levels in a high spatial resolution." *International Journal of Sustainable Energy Planning and Management*, 13:79-92.
- eGon-data: https://egon-data.readthedocs.io
- eTraGo: https://etrago.readthedocs.io
- MaStR: https://www.marktstammdatenregister.de
- DemandRegio: https://opendata.ffe.de/project/demandregio
- PyPSA: https://pypsa.org
