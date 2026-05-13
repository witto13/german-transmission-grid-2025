# Grid Alpha: MaStR Generator Allocation to German Transmission Grid

**Date**: 2026-03-08
**Scenario**: `grid_alpha` (based on `eGon2025v6`)

## Overview

Built the `grid_alpha` scenario by allocating all operational generation capacity from the German MaStR (Marktstammdatenregister) to the v6 transmission grid topology. The result is a complete power system model with 18,951 generators (296.5 GW), 2,422 storage units (12.6 GW), and 56 cross-border loads on 7,723 buses.

## Base Grid (from v6)

- 7,723 buses (110/220/380 kV)
- 12,911 lines
- 567 transformers (including 19 PSTs)
- 14 HVDC links
- 56 cross-border import generators + 56 export loads with 8,760h profiles

## Scripts Created

| Script | Purpose |
|--------|---------|
| `scripts/build_grid_alpha.py` | Main allocation pipeline: copies v6, allocates all MaStR technologies |
| `scripts/grid_alpha_offshore_and_map.py` | Offshore wind allocation + interactive HTML map generation |

## Allocation Methods

### SEL-Based Grouping (HV/EHV units)
For large generators connected at 110 kV and above, the MaStR SEL (grid feed-in location) join chain provides voltage level and approximate coordinates:

```
Unit → LokationMastrNummer → locations_extended → Netzanschlusspunkte (SAN)
→ grid_connections → Spannungsebene (voltage level)
```

SEL groups are spatially matched to grid buses using a KD-tree (`SpatialMatcher`) with voltage-specific distance thresholds (380 kV: 5 km, 220 kV: 3 km, 110 kV: 2 km).

### Municipality Aggregation (MV/LV units)
For millions of small generators (rooftop solar, small biomass, home batteries) connected at medium/low voltage:

1. Group units by municipality (`Gemeindeschluessel`) and carrier
2. Find all 110 kV buses within the municipality polygon using PostGIS `ST_Contains`
3. Spread capacity evenly across all buses in that municipality
4. Fallback to nearest 110 kV bus if no buses are within the polygon

This prevents unrealistic capacity concentration (e.g., Berlin's 416 MW solar is spread across ~144 buses rather than dumped on one).

## Results by Technology

### Generators (18,951 total, 296.5 GW)

| Carrier | Count | Capacity (GW) | Method |
|---------|-------|---------------|--------|
| Solar | 6,395 | 104.4 | SEL (HV+) + Municipality (MV/LV) |
| Wind onshore | 2,407 | 68.2 | SEL-based grouping |
| Gas | 768 | 33.2 | SEL-based grouping |
| Coal | 33 | 15.1 | SEL-based grouping |
| Lignite | 17 | 14.7 | SEL-based grouping |
| Wind offshore | 9 | 9.8 | SEL → offshore HVDC buses |
| Biogas | 5,320 | 7.1 | SEL (HV+) + Municipality (MV/LV) |
| Oil | 449 | 5.8 | SEL-based grouping |
| Run of river | 1,355 | 4.2 | SEL (HV+) + Municipality (MV/LV) |
| Other conventional | 127 | 3.6 | SEL-based grouping |
| Biomass | 1,839 | 1.8 | SEL (HV+) + Municipality (MV/LV) |
| Waste | 76 | 1.6 | SEL-based grouping |
| Reservoir hydro | 99 | 1.1 | SEL-based grouping |
| Cross-border imports | 56 | 25.0 | Inherited from v6 |

### Storage (2,422 total, 12.6 GW)

| Carrier | Count | Capacity (GW) | Method |
|---------|-------|---------------|--------|
| Pumped hydro | 27 | 9.9 | SEL-based grouping |
| Battery | 2,395 | 2.7 | SEL (large) + Municipality (home batteries) |

## Interactive Map

Generated `results/grid_alpha_map.html` with:

- **Voltage-colored grid lines**: blue (110 kV), green (220 kV), red (380 kV), purple dashed (HVDC)
- **Toggleable voltage layers** via layer control
- **Bus circles** sized by installed capacity with pulse animation on hover
- **Hover tooltips** showing per-carrier capacity breakdown and unit counts
- **AGG tag** on municipality-aggregated entries (MV/LV) to distinguish from direct SEL matches
- 6,786 active buses displayed (buses with at least one generator, storage, or load)

## Metadata Files

- `results/grid_alpha_gen_metadata.csv` — generator metadata (bus_id, carrier, n_units, is_aggregated, p_nom_mw)
- `results/grid_alpha_stor_metadata.csv` — storage metadata (same columns)

These sidecar files preserve unit counts and aggregation flags that are not stored in the database tables.

## Key Design Decisions

1. **MV/LV → 110 kV**: All sub-HV generation aggregates to nearest 110 kV bus
2. **Municipality spreading**: Capacity distributed evenly across all 110 kV buses within the municipality polygon, avoiding single-bus concentration
3. **Offshore wind**: MaStR turbines matched to existing offshore HVDC bus nodes using KD-tree; synthetic generators replaced with real data
4. **Conventional ≥ 1 MW**: Micro-CHP units below 1 MW excluded to reduce noise
5. **Unit tracking**: Actual MaStR unit counts preserved through the pipeline for transparency

## Usage

```bash
# Rebuild from scratch (copies v6 + allocates all technologies)
conda run -n egon2025 python scripts/build_grid_alpha.py --apply

# Add offshore wind + generate map (run after build_grid_alpha)
conda run -n egon2025 python scripts/grid_alpha_offshore_and_map.py --apply --output results/grid_alpha_map.html

# Dry run (print counts without DB writes)
conda run -n egon2025 python scripts/build_grid_alpha.py --dry-run
```
