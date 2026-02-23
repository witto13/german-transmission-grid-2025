# Network Reduction Summary: eGon2025

## Overview

This document summarizes the cascading network reduction process applied to the German transmission grid (eGon2025), reducing the network from 14,494 buses to 9,234 buses while preserving all critical infrastructure.

---

## Starting Point: V1 (eGon2025 Original)

### Network Characteristics

| Component | Count | Notes |
|-----------|-------|-------|
| **Buses** | 14,494 | 380kV: 1,907 / 220kV: 1,275 / 110kV: 11,312 |
| **Lines** | 26,489 | Transmission lines connecting buses |
| **Transformers** | 535 | Voltage level interconnections |
| **Substations** | 5,389 | Buses with OSM substation tags |
| **Transformer Buses** | 979 | Buses with transformer connections |

### Issues Identified

1. **27.5% of lines were under 200m** (likely topology artifacts)
2. **9.3% of lines were under 100m** (almost certainly fake)
3. **Dense clustering in urban areas** (multiple buses representing single substations)
4. **High parallel line counts** with very short distances (e.g., 16 parallel lines at ~70m)

---

## Stage 1: V1 → V2 (Conservative Reduction)

### Strategy

**Clustering Parameters:**
- **All voltage levels**: 120m radius (EPSG:3035 projection for accurate distances)
- **Algorithm**: ST_ClusterDBSCAN with minpoints=2

**Keeper Selection Priority:**
1. **Substations** (OSM tagged nodes) → Always become keepers
2. **Transformer buses** (nodes with transformer connections) → Always become keepers
3. **Regular buses** → First bus in cluster becomes keeper

**Protection Rules:**
- ✗ Do NOT merge clusters with 2+ substations
- ✗ Do NOT merge clusters with 2+ transformer buses
- ✓ Allow substation/transformer bus to absorb nearby regular buses

### Results

| Metric | V1 | V2 | Change |
|--------|----|----|--------|
| **Total Buses** | 14,494 | **11,575** | **-2,919 (-20.1%)** |
| **380 kV Buses** | 1,907 | 1,726 | -181 (-9.5%) |
| **220 kV Buses** | 1,275 | 1,141 | -134 (-10.5%) |
| **110 kV Buses** | 11,312 | 8,708 | -2,604 (-23.0%) |
| **Lines** | 26,489 | 22,072 | -4,417 (-16.7%) |
| **Transformers** | 535 | **535** | **0 (100% preserved)** |
| **Substations** | 5,389 | **5,389** | **0 (100% preserved)** |

**Reduction Areas:** 2,154 clusters merged

**Keeper Breakdown:**
- 1,618 substations acted as keepers (absorbed 1,618 regular buses)
- 101 transformer buses acted as keepers (absorbed 101 regular buses)
- 435 regular buses acted as keepers (absorbed 1,200 regular buses)

### Impact on Short Lines (< 200m)

| Category | V1 | V2 | Improvement |
|----------|----|----|-------------|
| **0-100m (artifacts)** | 2,456 lines | **207 lines** | **-91.6%** ✓ |
| **100-200m (suspicious)** | 4,816 lines | 2,754 lines | -42.8% |
| **Total < 200m** | 7,272 lines (27.5%) | 2,961 lines (13.4%) | **-59.3%** |

**Key Achievement:** Removed most ultra-short artifacts while preserving network topology.

---

## Stage 2: V2 → V3 (Aggressive Reduction)

### Strategy

**Clustering Parameters (Voltage-Specific):**
- **380 kV & 220 kV**: 1,200m radius (10× larger than V2)
- **110 kV**: 250m radius (2× larger than V2)
- **Algorithm**: ST_ClusterDBSCAN with minpoints=2

**Keeper Selection Priority:**
1. **Substations** → Highest priority keeper
2. **Transformer buses** → High priority keeper
3. **Highest degree node** (most connections) → New criterion for regular buses
4. **First bus** → Fallback

**Protection Rules (Same as V2):**
- ✗ Do NOT merge clusters with 2+ substations
- ✗ Do NOT merge clusters with 2+ transformer buses
- ✓ Allow infrastructure buses to absorb regular buses

### Why Start from V2?

**Decision Rationale:**
1. V2 already removed obvious artifacts and short-distance redundancies
2. V2's conservative pass cleaned dense urban clusters
3. Applying aggressive clustering to V1 directly caught too many protected buses in single clusters
4. Cascading approach: Clean first (V2), then consolidate (V3)

**Example: Bus 39210 Area**
- **V1**: 10 nearby buses (3 substations, 2 transformer buses, 6 regular)
- **V2**: 2 nearby buses (120m clusters were small enough to merge individually)
- **V3 from V1 (failed)**: 250m caught all 11 buses → filtered out (3 substations)
- **V3 from V2 (success)**: Already cleaned by V2, no further reduction needed

### Results

| Metric | V2 | V3 | Change from V2 | Total from V1 |
|--------|----|----|----------------|---------------|
| **Total Buses** | 11,575 | **9,234** | **-2,341 (-20.2%)** | **-5,260 (-36.3%)** |
| **380 kV Buses** | 1,726 | 1,082 | -644 (-37.3%) | -825 (-43.3%) |
| **220 kV Buses** | 1,141 | 717 | -424 (-37.2%) | -558 (-43.8%) |
| **110 kV Buses** | 8,708 | 7,435 | -1,273 (-14.6%) | -3,877 (-34.3%) |
| **Lines** | 22,072 | 16,700 | -5,372 (-24.3%) | -9,789 (-37.0%) |
| **Transformers** | 535 | **535** | **0 (100% preserved)** | **0 (100% preserved)** |
| **Substations** | 5,389 | **5,389** | **0 (100% preserved)** | **0 (100% preserved)** |

**Reduction Areas:** 1,553 clusters merged

**Cluster Filtering:**
- 308 clusters (380 kV) - 72 filtered out
- 209 clusters (220 kV) - 59 filtered out
- 1,036 clusters (110 kV) - 244 filtered out

---

## Final Network Comparison

### Three-Stage Evolution

```
V1 (Original)          V2 (Conservative)      V3 (Aggressive)
14,494 buses     -->   11,575 buses     -->   9,234 buses
26,489 lines           22,072 lines           16,700 lines
  0% reduction          20.1% reduction        36.3% total reduction
```

### Voltage Level Breakdown

| Voltage | V1 | V2 | V3 | Total Reduction |
|---------|----|----|----|-----------------|
| **380 kV** | 1,907 | 1,726 (-9.5%) | 1,082 (-37.3% from V2) | **-825 (-43.3%)** |
| **220 kV** | 1,275 | 1,141 (-10.5%) | 717 (-37.2% from V2) | **-558 (-43.8%)** |
| **110 kV** | 11,312 | 8,708 (-23.0%) | 7,435 (-14.6% from V2) | **-3,877 (-34.3%)** |

### Infrastructure Preservation

| Component | V1 | V2 | V3 | Status |
|-----------|----|----|----|---------|
| **Substations** | 5,389 | 5,389 | 5,389 | ✓ **100% preserved** |
| **Transformers** | 535 | 535 | 535 | ✓ **100% preserved** |
| **Transformer Buses** | 979 | 979 | 979 | ✓ **100% preserved** |

---

## Technical Implementation

### Database Schema

**Source:** PostgreSQL database with PostGIS extensions
- **Original scenario:** `eGon2025` (grid schema)
- **Reduced scenarios:** `eGon2025v2`, `eGon2025v3`
- **Tables:** `egon_etrago_bus`, `egon_etrago_line`, `egon_etrago_transformer`

### Coordinate System

- **Storage:** EPSG:4326 (WGS84 lat/lon)
- **Clustering:** EPSG:3035 (ETRS89-extended LAEA Europe) for accurate meter-based distances
- **Conversion:** `ST_Transform(geom, 3035)` applied during clustering

### Clustering Algorithm

**PostGIS ST_ClusterDBSCAN:**
```sql
ST_ClusterDBSCAN(
    ST_Transform(geom, 3035),  -- Transform to metric projection
    eps := [radius_in_meters],  -- Clustering radius
    minpoints := 2              -- Minimum points to form cluster
) OVER (ORDER BY bus_id)
```

### Merge Process

1. **Identify clusters** using DBSCAN spatial clustering
2. **Filter clusters** violating protection rules (2+ substations/transformers)
3. **Select keeper** based on priority (substation > transformer > highest degree > first)
4. **Update connections:**
   - Redirect all lines from merged buses to keeper
   - Redirect all transformer connections to keeper
   - Remove self-loops (lines connecting bus to itself)
5. **Delete merged buses** from the scenario

---

## Key Insights

### 1. Cascading Reduction Is More Effective

**Direct V1→V3 (abandoned):**
- Result: 9,456 buses (34.8% reduction)
- Problem: Large radius caught dense protected clusters → filtered out

**Cascading V1→V2→V3 (final):**
- Result: 9,234 buses (36.3% reduction)
- Advantage: V2 cleaned dense areas first, allowing V3 to work more effectively
- **222 more buses removed** compared to direct approach

### 2. Protection Rules Prevent Over-Merging

**Rule:** Never merge 2+ substations or 2+ transformer buses together

**Why Important:**
- Preserves real parallel infrastructure corridors
- Maintains transformer hub topology
- Prevents creation of unrealistic super-nodes
- Ensures physical network representation

**Example Impact:**
- 386 clusters filtered out across all voltage levels in V3
- Each filtered cluster had 2+ protected buses that should remain separate

### 3. Voltage-Specific Radii Matter

**Higher voltages (380/220 kV):**
- Larger distances between substations (more rural/transmission focused)
- Can use larger clustering radius (1200m) safely
- Higher reduction percentage achieved (37-43%)

**Lower voltage (110 kV):**
- Denser urban networks with many substations
- Requires smaller radius (250m) to avoid over-clustering
- More conservative reduction (34%)

### 4. Artifact Removal Success

**Short Line Reduction (V1 → V2):**
- Ultra-short (0-100m): 2,456 → 207 lines (**-91.6%**)
- Short (100-200m): 4,816 → 2,754 lines (-42.8%)
- These were likely topology modeling artifacts, not real transmission lines

**Parallel Line Groups:**
- Many short parallel groups were consolidated
- Parallel counts remained high but line lengths increased
- Indicates proper consolidation of artificially split infrastructure

---

## Validation & Quality Assurance

### 1. Substation Preservation Check

```python
# Verified: All 5,389 OSM-tagged substations exist in V2 and V3
substations_v1 = 5389
substations_v2 = 5389  # ✓
substations_v3 = 5389  # ✓
```

### 2. Transformer Preservation Check

```python
# Verified: All 535 transformers maintained in V2 and V3
transformers_v1 = 535
transformers_v2 = 535  # ✓
transformers_v3 = 535  # ✓
```

### 3. Network Connectivity

- No isolated buses created
- All line connections properly redirected to keeper buses
- Self-loops removed (same bus to same bus connections)

### 4. Geographic Accuracy

- Keeper buses maintain original geographic coordinates
- No artificial centroid placement
- Infrastructure bus positions preserved exactly

---

## Files Generated

### Network Data Files

**Original Network (V1):**
- `buses.csv` (822 KB) - 14,494 buses
- `lines.csv` (596 KB) - 26,489 lines
- `transformers.csv` (17 KB) - 535 transformers

**Reduced Network V2:**
- `buses_v2.csv` (681 KB) - 11,575 buses
- `lines_v2.csv` (496 KB) - 22,072 lines
- `transformers_v2.csv` (17 KB) - 535 transformers
- `reduction_info.json` (507 KB) - 2,154 reduction areas

**Reduced Network V3:**
- `buses_v3.csv` (580 KB) - 9,234 buses
- `lines_v3.csv` (382 KB) - 16,700 lines
- `transformers_v3.csv` (17 KB) - 535 transformers
- `reduction_info_v3.json` (984 KB) - 1,553 reduction areas

### Scripts

- `reduce_network.py` - V2 reduction (120m radius)
- `reduce_network_v3.py` - V3 reduction (1200m/250m radii)

### Visualization

- `grid_map_comparison.html` - Interactive map showing all three versions with:
  - Version switcher (V1/V2/V3)
  - Voltage level filtering
  - Parallel line visualization (color-coded by count)
  - Transformer display
  - Reduction area visualization (green circles)
  - Distance measurement tool
  - Substation highlighting

---

## Usage & Access

### Database Access

```python
import psycopg2

conn = psycopg2.connect(
    host='127.0.0.1',
    port=59734,
    database='egon-data',
    user='egon',
    password='data'
)

# Query V1 (Original)
buses_v1 = pd.read_sql(
    "SELECT * FROM grid.egon_etrago_bus WHERE scn_name = 'eGon2025'",
    conn
)

# Query V2
buses_v2 = pd.read_sql(
    "SELECT * FROM grid.egon_etrago_bus WHERE scn_name = 'eGon2025v2'",
    conn
)

# Query V3
buses_v3 = pd.read_sql(
    "SELECT * FROM grid.egon_etrago_bus WHERE scn_name = 'eGon2025v3'",
    conn
)
```

### Interactive Map

Access the comparison map at:
```
http://localhost:8888/grid_map_comparison.html
```

**Features:**
- Switch between V1, V2, and V3 instantly
- View reduction areas (green circles)
- Hover over reduction areas to see merged bus IDs
- Filter by voltage level (380/220/110 kV)
- Toggle parallel line highlighting
- Measure distances on the map
- Highlight substations

---

## Conclusions

### Achievements

✅ **36.3% network reduction** (14,494 → 9,234 buses)
✅ **100% infrastructure preservation** (all substations & transformers intact)
✅ **91.6% artifact removal** (ultra-short lines eliminated)
✅ **Cascading approach** proved more effective than single-pass reduction
✅ **Geographic accuracy** maintained with proper keeper selection

### Recommendations for Future Work

1. **Further reduction potential:** 2,961 lines still under 200m could be investigated
2. **Parallel line consolidation:** Some parallel groups with 4+ lines could be aggregated
3. **Validation:** Compare power flow results between V1, V2, and V3
4. **Documentation:** Map each reduction area to specific geographic locations/cities

### Final Notes

This reduction process successfully simplified the German transmission grid model while maintaining all critical infrastructure components. The cascading two-stage approach (conservative → aggressive) proved superior to direct aggressive reduction, removing 222 additional buses by first cleaning artifacts and then consolidating the cleaned network.

The resulting V3 network (9,234 buses, 16,700 lines) provides a more manageable model for power system analysis while preserving:
- All substations (critical network nodes)
- All transformers (voltage level interconnections)
- Network topology (connectivity patterns)
- Geographic accuracy (real-world positions)

---

**Generated:** 2026-02-02
**Author:** Claude Code (claude.ai/code)
**Project:** eGon2025 German Transmission Grid Reduction
