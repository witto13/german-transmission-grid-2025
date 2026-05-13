# Grid Beta Build — 2026-03-11

Scenario: `grid_beta` (copy of `grid_alpha` + domestic loads + BDEW profiles)

## Overview

| Component | Count | Capacity |
|-----------|-------|----------|
| Buses | 7,723 | — |
| Lines | 12,911 | — |
| Transformers | 567 (incl 19 PSTs) | — |
| HVDC links | 14 | — |
| Generators | 18,793 | 296.5 GW |
| Storage | 2,444 | 12.6 GW |
| Domestic loads | 12,154 | 76.2 GW peak / 448 TWh |
| Export loads | 56 | 31.5 GW |

## What Was Done

### 1. Voltage Allocation Bug Fix (Generators)

**Problem**: `build_grid_alpha.py` applied Hülk et al. (2017) capacity-based voltage thresholds (>20 MW → 220kV, >120 MW → 380kV) **unconditionally** to all generators, overriding the actual MaStR Spannungsebene. A 28 MW wind farm registered at Mittelspannung (MV) would get bumped to 220kV.

**Root cause**: Lines 253–261 in `sel_based_allocation()` — the capacity-based override did not check whether a valid SAN voltage already existed from the MaStR registry chain (SEE → SEL → SAN → Spannungsebene).

**Fix applied** (3 parts):

1. **Respect SAN voltage**: Track `has_san_voltage` flag per unit. Only apply capacity-based override when MaStR has **no SAN data** (NULL voltage_level).

2. **Hülk sanity check**: Even when SAN voltage exists, downgrade to 110kV if capacity is below the Hülk threshold for that voltage level. Catches MaStR data errors (e.g., 0.13 MW solar registered at "Umspannebene Höchstspannung/Hochspannung").

3. **Fallback spatial matching**: Pass `preferred_voltage` to `find_nearest_any_voltage()` so the fallback prefers the target voltage level.

**Files fixed**:
- `scripts/build_grid_alpha.py` — `sel_based_allocation()`, `allocate_solar()`, `allocate_hydro()`, `allocate_storage()`, `spatial_match_sel_groups()`

**Impact — onwind example**:

| Voltage | Before (buggy) | After (fixed) |
|---------|---------------|---------------|
| 110 kV | 50.7 GW | 66.2 GW |
| 220 kV | 15.4 GW | 0 GW → now only >20MW |
| 380 kV | 2.1 GW | 2.0 GW |

**Total downgraded SEL groups**: 220 across all technologies:
- Wind onshore: 68
- Conventional: 13
- Solar HV+: 54
- Biomass HV+: 6
- Hydro: 40
- Storage: 39

### 2. Voltage Allocation Bug Fix (Loads)

**Problem**: `build_grid_beta.py` municipality-to-bus spatial join found **all** buses inside each municipality polygon regardless of voltage. When a municipality had 220/380kV buses but no 110kV buses inside its polygon, small residential/industry loads (0.2–1 MW) ended up on eHV buses.

**Fix**: Changed `build_load_entries()` to strictly filter buses by target voltage. If no bus at the target voltage exists inside the municipality polygon, fall back to KD-tree nearest-bus matching at the correct voltage — never spill onto higher voltage.

**Also fixed** `_fallback_nearest()`: when the target voltage bus isn't found within max distance, prefer lower voltages (110 → 220 → 380) rather than absolute nearest regardless of voltage.

### 3. Grid Beta Load Mapping

Grid beta adds 12,154 domestic loads to the grid_alpha topology using:

- **Source**: 11,003 German municipalities, 448 TWh annual demand
- **Peak factor**: 1.49 (76.2 GW peak / 51.1 GW average)
- **Voltage thresholds** (Hülk et al. 2017): ≤20 MW → 110kV, >20 MW → 220kV, >120 MW → 380kV

**Load types**:

| Carrier | Peak (GW) | Annual (TWh) | Entries |
|---------|-----------|--------------|---------|
| residential_cts | 43.9 | 258 | 6,377 |
| industry | 21.0 | 124 | 5,401 |
| large_industry | 11.3 | 67 | 376 |

**By voltage level**:

| Voltage | Peak (GW) | Share | Entries |
|---------|-----------|-------|---------|
| 110 kV | 67.1 | 88.1% | 11,962 |
| 220 kV | 3.9 | 5.1% | 134 |
| 380 kV | 5.2 | 6.9% | 58 |

**Smart municipality splitting**:
- PostGIS `ST_Contains` spatial join to find buses inside each municipality polygon
- Weight by bus degree (connectivity): Stadt uses `degree^1.5`, Gemeinde uses `degree^0.7`
- 2,810 municipalities have internal 110kV buses (1,250 with 2+ buses)

**Large industrial consumers**: 376 MaStR `electricity_consumer` entries (66.5 TWh), placed at precise coordinates. Municipality industry reduced by 35% upfront to avoid double-counting.

**BDEW SLP profiles**: 8,760h synthetic hourly profiles per load (H0 household, G0 commercial, industry flat).

### 4. Generation Summary (grid_beta = grid_alpha)

Per carrier, smallest → largest installation:

| Carrier | N | Total (GW) | Min (MW) | Median (MW) | Max (MW) | 110kV | 220kV | 380kV |
|---------|---|------------|----------|-------------|---------|-------|-------|-------|
| biomass | 1,835 | 1.8 | 0.00 | 0.08 | 140 | 1.8 | — | — |
| biogas | 5,319 | 7.1 | 0.00 | 0.44 | 34 | 7.1 | — | — |
| run_of_river | 1,355 | 4.2 | 0.03 | 0.38 | 272 | 2.4 | 1.7 | — |
| solar | 6,395 | 104.4 | 0.03 | 8.80 | 447 | 103.5 | — | 0.9 |
| reservoir | 99 | 1.1 | 0.03 | 0.41 | 640 | 0.9 | 0.2 | — |
| onwind | 2,303 | 68.2 | 0.04 | 14.75 | 505 | 66.2 | — | 2.0 |
| gas | 756 | 33.2 | 1.00 | 4.77 | 1,722 | 23.3 | 0.4 | 9.6 |
| oil | 441 | 5.8 | 1.00 | 2.53 | 720 | 4.5 | 0.2 | 1.1 |
| waste | 74 | 1.6 | 1.10 | 15.85 | 73 | 1.5 | 0.1 | — |
| coal | 34 | 15.1 | 3.25 | 412 | 1,351 | 2.0 | — | 13.1 |
| lignite | 17 | 14.7 | 3.57 | 63 | 4,340 | 0.4 | — | 14.3 |
| offwind | 9 | 9.8 | 113 | 608 | 3,469 | — | — | 9.8 |

**All 220kV generators are ≥20 MW. All 380kV generators are ≥20 MW.**

### 5. Validation Map + Excel Export

**Map**: `results/grid_beta_map.html` (7.1 MB)
- 6,632 active DE buses with generation or load
- Click any bus to see generation breakdown (carrier, MW, unit count) and load breakdown (carrier, MW, percentage, mini color bar)
- MaStR SEL feed-in locations shown with dashed connection lines on click
- Legend includes Load section (residential_cts, industry, large_industry)

**Excel**: `results/grid_beta_bus_breakdown.xlsx`
- 3 sheets: 110kV (6,377 buses), 220kV (153 buses), 380kV (116 buses)
- Columns per bus: lon, lat, v_nom, gen_{carrier}, gen_TOTAL, stor_{carrier}, stor_TOTAL, load_{carrier}, load_TOTAL

## Files Modified

| File | Change |
|------|--------|
| `scripts/build_grid_alpha.py` | Fixed voltage allocation: respect SAN voltage, add Hülk sanity check, fix fallback matching. Applied to `sel_based_allocation()`, `allocate_solar()`, `allocate_hydro()`, `allocate_storage()`, `spatial_match_sel_groups()` |
| `scripts/build_grid_beta.py` | Fixed load voltage allocation: strict voltage filtering in `build_load_entries()`, improved `_fallback_nearest()` to prefer lower voltages |
| `scripts/create_substation_test_map.py` | Added load display (query, tooltip, panel, legend), updated scenario to grid_beta, updated `pick_substations()` to include buses with loads |

## Files Created

| File | Purpose |
|------|---------|
| `results/grid_beta_map.html` | Interactive validation map with gen + load per bus |
| `results/grid_beta_bus_breakdown.xlsx` | Per-bus breakdown (3 sheets by voltage) |
| `docs/grid_beta_2026-03-11.md` | This document |

## Pipeline Rebuild Order

```bash
conda activate egon2025

# 1. Rebuild grid_alpha generators (with voltage fixes)
python scripts/build_grid_alpha.py --apply

# 2. Re-add offshore wind
python scripts/grid_alpha_offshore_and_map.py --apply

# 3. Rebuild grid_beta (copies grid_alpha + adds loads)
python scripts/build_grid_beta.py --apply

# 4. Generate map
python scripts/create_substation_test_map.py --all --output results/grid_beta_map.html
```
