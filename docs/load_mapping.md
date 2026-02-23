# Load Mapping: Municipality Demand to Grid Buses

This document describes the methodology for mapping 448 TWh of annual electricity demand from German municipalities to HV/eHV buses in the `egon2025_reduced` scenario.

## Overview

| Metric | Value |
|--------|-------|
| Input municipalities | 11,003 |
| Total annual demand | 448 TWh |
| Output load points | 7,246 |
| Total peak load | 76.2 GW |
| Target scenario | `egon2025_reduced` |

## Methodology

### Data Sources

1. **Municipality demand** (`demand_by_municipality_2025.csv`)
   - 11,003 unique municipalities (11,135 rows before AGS aggregation)
   - Sector breakdown: Households (134 TWh), CTS (124 TWh), Industry (190 TWh)
   - Source: Derived from population, employment, and industrial site data

2. **Municipality geometries** (`boundaries.vg250_gem`)
   - Official German municipality boundaries (VG250)
   - Used to compute centroids for spatial matching

3. **Grid buses** (`grid.egon_etrago_bus`)
   - 7,316 buses in `egon2025_reduced` scenario
   - 6,179 at 110 kV, 494 at 220 kV, 643 at 380 kV

### Voltage Level Assignment

Voltage levels are assigned based on peak load thresholds from **Hülk et al. (2017)** - "Allocation of annual electricity consumption and power generation capacities across multiple voltage levels in a high spatial resolution" (International Journal of Sustainable Energy Planning and Management).

These thresholds are also used in the eGon-data pipeline (see `eGon-data/src/egon/data/datasets/storages/pumped_hydro.py:353-378`).

| Peak Load | Voltage Level | Grid Level |
|-----------|---------------|------------|
| ≤ 5.5 MW | Levels 4-7 | MV/LV (aggregated to 110 kV) |
| > 5.5 MW | Level 4 | 110 kV |
| > 20 MW | Level 3 | 220 kV |
| > 120 MW | Level 1 | 380 kV |

### Peak Load Calculation

Peak load is derived from annual consumption using a peak factor of 1.49:

```
peak_mw = (annual_mwh / 8760) * 1.49
```

The factor 1.49 represents the ratio of peak demand (~76 GW) to average demand (~51 GW) for Germany.

### Sector-Based Load Assignment

Each municipality generates up to two load points:

1. **Residential + CTS load** → Always assigned to 110 kV
   - Households and commercial/services connect at LV/MV
   - In an HV-only model, these aggregate to 110 kV substations

2. **Industrial load** → Voltage based on peak load threshold
   - Small/medium industry (peak ≤ 20 MW): 110 kV
   - Large industry (20-120 MW peak): 220 kV
   - Very large industry (> 120 MW peak): 380 kV

### Spatial Matching

Loads are assigned to the nearest bus at the target voltage level using KD-tree spatial indexing:

1. For each load, find the nearest bus at the target voltage within a search radius
2. If no match at target voltage, fall back to nearest bus at any HV/eHV voltage
3. Aggregate loads when multiple municipalities map to the same bus

Search radii by voltage:
- 110 kV: 50 km
- 220 kV: 75 km
- 380 kV: 100 km (substations are sparse)

## Results

### Load Distribution by Voltage Level

| Voltage | Loads | Peak (GW) | Annual (TWh) | Share |
|---------|-------|-----------|--------------|-------|
| 110 kV | 7,123 | 64.7 | 380.2 | 84.9% |
| 220 kV | 102 | 5.2 | 31.0 | 6.9% |
| 380 kV | 21 | 6.3 | 36.8 | 8.2% |

### Load Distribution by Type

| Type | Loads | Peak (GW) | Share |
|------|-------|-----------|-------|
| residential_cts | 3,621 | 43.9 | 57.6% |
| industry | 3,625 | 32.3 | 42.4% |

### Validation

| Check | Expected | Actual | Status |
|-------|----------|--------|--------|
| Total annual demand | 448 TWh | 448.0 TWh | PASS |
| Peak load | ~76 GW | 76.2 GW | PASS |
| Energy balance error | < 1% | 0.00% | PASS |
| Unmatched loads | 0 | 0 | PASS |

## Why 85% of Load is at 110 kV

This distribution is correct because:

1. **Residential + CTS (258 TWh, 58%)**: All households and commercial buildings connect at LV/MV. In an HV-only model, these loads aggregate to the nearest 110 kV substation.

2. **Small/medium industry (122 TWh, 27%)**: Most industrial facilities have peak loads below 20 MW and connect at MV or 110 kV.

3. **Only large industry connects at higher voltages**:
   - 119 municipalities have industrial peak > 20 MW → 220 kV
   - 21 municipalities have industrial peak > 120 MW → 380 kV

### Cross-Validation with Real-World Data

German large industrial consumers by category:

| Category | Typical Sites | Typical Load | Est. Total |
|----------|---------------|--------------|------------|
| Steel works | 5 | 300-1000 MW | ~4 GW |
| Aluminum smelters | 3 | 200-500 MW | ~1 GW |
| Major chemical complexes | 5 | 200-1000 MW | ~3 GW |
| Large refineries | 10 | 100-300 MW | ~2 GW |
| **Very large total** | ~25 | >120 MW | **~10 GW** |

Our model: 21 loads at 380 kV with 6.3 GW peak → **plausible**

Medium-large consumers (cement, paper, car factories, data centers):
- ~100-200 sites at 20-120 MW
- Estimated ~5-10 GW total

Our model: 102 loads at 220 kV with 5.2 GW peak → **plausible**

## Usage

### Run the mapping script

```bash
cd /root/egon_2025_project
conda activate egon2025
python scripts/load_mapping.py
```

### Options

- `--dry-run`: Show what would be done without writing to database
- `--include-large-consumers`: Add 556 MaStR large consumers (66.5 TWh additional)

### Verify results

```sql
-- Total load count
SELECT COUNT(*) FROM grid.egon_etrago_load
WHERE scn_name='egon2025_reduced';

-- Load by voltage level
SELECT b.v_nom, COUNT(*) as loads, SUM(l.p_set) as peak_mw
FROM grid.egon_etrago_load l
JOIN grid.egon_etrago_bus b ON l.bus = b.bus_id AND l.scn_name = b.scn_name
WHERE l.scn_name='egon2025_reduced'
GROUP BY b.v_nom ORDER BY b.v_nom;

-- Energy balance check (should be ~448 TWh)
SELECT SUM(p_set) * 8760 / 1.49 / 1e6 as annual_twh
FROM grid.egon_etrago_load WHERE scn_name='egon2025_reduced';
```

## References

1. Hülk, L., Müller, B., Glauer, M., Förster, E., & Schachler, B. (2017). Allocation of annual electricity consumption and power generation capacities across multiple voltage levels in a high spatial resolution. *International Journal of Sustainable Energy Planning and Management*, 13, 79-92.

2. eGon-data repository: `src/egon/data/datasets/storages/pumped_hydro.py` (voltage level thresholds)

3. SMARD - Strommarktdaten: [Network levels](https://www.smard.de/page/en/wiki-article/5884/214026)

## Files

| File | Purpose |
|------|---------|
| `scripts/load_mapping.py` | Main mapping script |
| `scripts/utils/spatial_matching.py` | KD-tree spatial matching utilities |
| `demand_by_municipality_2025.csv` | Input demand data |
| `docs/load_mapping.md` | This documentation |
