# JAO/CORE-TSO Parameter Matching for eGon2025v7

## Overview

The `scripts/jao_matching.py` script transfers real TSO-reported electrical parameters from the JAO/CORE-TSO dataset to our eGon2025v7 grid model. Both datasets are OSM-derived, enabling direct substation matching via OSM object IDs for 84.8% of substations.

**What we gain**: Real r/x/b impedances for 1,000 line segments, transformer impedances (previously all zero), Imax-based thermal ratings, and EIC code mappings.

**Scope**: German 220/380 kV transmission grid only. 110 kV lines and non-German assets are unaffected.

## Match Results

| Component | Matched | Total (220/380 kV) | Rate |
|-----------|---------|---------------------|------|
| JAO buses -> eGon clusters | 414 | 422 | 98.1% |
| JAO lines matched | 767 | 867 | 88.5% |
| eGon line segments updated | 1,000 | 1,731 | 57.8% |
| eGon transformers updated | 72 | 114 | 63.2% |
| EIC code mappings | 1,087 | — | — |

## Data Sources

| Dataset | Source | Content |
|---------|--------|---------|
| JAO/CORE-TSO buses | `data/jao_core_tso/buses.csv` | 422 DE substations (non-duplicate, 220/400 kV) |
| JAO/CORE-TSO lines | `data/jao_core_tso/lines.csv` | 867 DE transmission lines with r, x, b, Imax |
| JAO/CORE-TSO transformers | `data/jao_core_tso/transformers.csv` | 127 DE transformers with r, x, s_nom |
| eGon2025v7 grid | PostgreSQL `grid.egon_etrago_*` tables | 1,137 buses, 1,731 lines, 114 transformers (220/380 kV) |
| EHV substations | `grid.egon_ehv_substation` | 458 substations with OSM IDs |

## Methodology

### Step 1: Data Loading

- Loads JAO CSVs and eGon database tables
- Filters JAO to German TSOs: 50Hertz, TenneT, Amprion, TransnetBW
- Excludes JAO `transformer_dupplicate=True` buses and 110 kV buses
- Maps JAO 400 kV to eGon 380 kV

### Step 2: Substation Clustering

Groups eGon's 1,137 buses (220/380 kV) into 946 substation clusters:

| Cluster type | Count | Description |
|-------------|-------|-------------|
| Named (OSM ID) | 458 | From `egon_ehv_substation` table, cluster_id = OSM numeric ID |
| Absorbed | 174 buses | Unnamed buses within 200 m of a named substation |
| Spatial | 488 | Unnamed buses grouped by 200 m Union-Find clustering |

Uses Union-Find algorithm with per-voltage-level spatial clustering (200 m radius). Computes cluster centroids and node degree (external line connections).

### Step 3: Bus Matching (JAO -> eGon Clusters)

Four-tier matching strategy:

| Tier | Method | Radius | Confidence | Count | Share |
|------|--------|--------|------------|-------|-------|
| 0 | OSM ID exact match | — | 1.0 | 358 | 86.5% |
| 1 | Spatial (KD-tree) | 500 m | 0.9 | 50 | 12.1% |
| 2 | Spatial + degree validation | 2 km | 0.7 | 4 | 1.0% |
| 3 | Spatial + degree (fallback) | 5 km | 0.5 | 2 | 0.5% |
| — | Unmatched | — | — | 8 | 1.9% |

Mean confidence across all matches: **0.976**.

### Step 4: Graph-Based Line Matching

1. **Cluster graph** (per voltage level):
   - 380 kV: 590 nodes, 681 edges
   - 220 kV: 454 nodes, 475 edges

2. **Shortest-path routing**: For each JAO line, resolve both endpoints to eGon clusters, then find the shortest path (Dijkstra, weighted by line length) through the cluster graph.

3. **Path validation**: Accept paths where eGon total length is 0.3x–3.0x of JAO reported length. Mean length ratio: **0.99** (near-perfect alignment).

4. **Parallel circuit assignment**: Within each corridor, rank JAO and eGon lines by s_nom and assign pairwise.

| Path hops | Lines | Share | Avg length ratio |
|-----------|-------|-------|-----------------|
| 1 | 271 | 27.1% | 0.96 |
| 2 | 335 | 33.5% | 0.98 |
| 3 | 231 | 23.1% | 1.00 |
| 4 | 76 | 7.6% | 1.03 |
| 5–7 | 87 | 8.7% | 0.99 |

### Step 5: Parameter Transfer

#### Lines

| Parameter | Unit | Method | Median | Range |
|-----------|------|--------|--------|-------|
| r | Ohm | Proportional by segment length | 0.359 | 0.001 – 15.31 |
| x | Ohm | Proportional by segment length | 3.060 | 0.025 – 56.84 |
| b | Siemens | Proportional by segment length | 4.2e-5 | 1.2e-7 – 5.8e-4 |
| s_nom | MVA | sqrt(3) * V * Imax / 1000 | 1,645 | 158 – 2,494 |

For multi-hop paths, JAO total Ohms are distributed proportionally:
```
r_segment = r_jao * (length_segment / total_path_length)
```

**s_nom computation**: Primary method uses seasonal Imax (614 lines). Fallback uses `s_nom_jao * sqrt(3)` (386 lines).

**Per-km impedance validation** (JAO vs existing eGon):

| Voltage | Param | JAO | eGon (existing) |
|---------|-------|-----|-----------------|
| 380 kV | r/km | 0.027 | 0.028 |
| 380 kV | x/km | 0.253 | 0.248 |
| 220 kV | r/km | 0.055 | 0.108 |
| 220 kV | x/km | 0.294 | 0.310 |

Reactance values match closely. The 220 kV resistance difference likely reflects different conductor assumptions; JAO data is authoritative.

#### Transformers

| Parameter | Unit | Method | Median | Range |
|-----------|------|--------|--------|-------|
| r | p.u. | Parallel equivalent, base-converted | 0.0009 | 0.0001 – 0.009 |
| x | p.u. | Parallel equivalent, base-converted | 0.056 | 0.012 – 0.608 |
| phase_shift | degrees | Most common from JAO group | 0.0 | -120 – +60 |

**Transformer impedance method**:
1. Collect all JAO transformers at the same cluster pair
2. Convert each JAO r/x from per-unit (on individual JAO s_nom base) to Ohms
3. Compute parallel equivalent impedance
4. Estimate parallel count: `n = round(egon_s_nom / avg_jao_unit_s_nom)`
5. Scale impedance for n parallel units: `Z_parallel = Z_avg / n`
6. Convert back to per-unit on eGon's s_nom base

**Skipped**: b and g are not transferred because JAO uses physical units (not per-unit) with an undocumented base convention.

## Critical Unit Conversion

**JAO s_nom is per-phase** (single-phase MVA). Confirmed by:
```
s_nom_jao / (sqrt(3) * V * Imax) = 1/sqrt(3) = 0.577  (for all lines with Imax data)
```

Always multiply by sqrt(3) before use in PyPSA (which expects 3-phase MVA).

## Output Files

| File | Rows | Description |
|------|------|-------------|
| `results/jao_matching/bus_match_report.csv` | 414 | All bus matches with tier, confidence, distance |
| `results/jao_matching/line_match_report.csv` | 1,000 | Line matches with old/new parameters, path info |
| `results/jao_matching/trafo_match_report.csv` | 72 | Transformer updates (r, x, phase_shift) |
| `results/jao_matching/eic_mapping.csv` | 1,087 | EIC code to eGon component ID mapping |
| `results/jao_matching/match_map.html` | — | Interactive map (folium) with color-coded layers |

## Usage

```bash
# Preview changes (default)
python scripts/jao_matching.py --dry-run

# Apply changes to database
python scripts/jao_matching.py --apply

# Custom scenario and output directory
python scripts/jao_matching.py --apply --scenario eGon2025v7 --output-dir results/jao_matching/
```

## Configuration

| Constant | Value | Description |
|----------|-------|-------------|
| `CLUSTER_RADIUS_KM` | 0.2 | 200 m for grouping eGon buses into substations |
| `CLUSTER_ABSORB_KM` | 0.2 | 200 m for merging spatial clusters into named ones |
| `BUS_MATCH_TIER1_KM` | 0.5 | 500 m spatial match radius |
| `BUS_MATCH_TIER2_KM` | 2.0 | 2 km with degree validation |
| `BUS_MATCH_TIER3_KM` | 5.0 | 5 km last resort |
| `PATH_LENGTH_MIN` | 0.3 | Reject paths < 30% of JAO length |
| `PATH_LENGTH_MAX` | 3.0 | Reject paths > 300% of JAO length |

## Unmatched Components

**8 unmatched JAO buses**: Likely substations not present in the eGon model (border areas or recently built).

**100 unmatched JAO lines** (of 867):
- 42 due to unmatched endpoints
- 25 due to cluster not in voltage graph
- 17 due to path length mismatch (>3x or <0.3x)
- 11 same-cluster (intra-substation)
- 5 no path in graph

**731 unupdated eGon lines** (of 1,731): Primarily 220 kV lines where the OSM segmentation creates many short segments not covered by JAO corridors, plus parallel circuits beyond what JAO reports.

**42 unupdated eGon transformers** (of 114): At locations where no JAO transformer data was available (mostly 110/220 kV interface or locations not in the CORE-TSO reporting scope).

## Verification Checklist

1. Run `--dry-run` and inspect summary statistics
2. Check match rate targets: >95% bus match, >80% line match
3. Verify per-km impedance values against textbook ranges (380 kV: r~0.03, x~0.25 Ohm/km)
4. Spot-check long 380 kV backbone corridors in the interactive map
5. Run power flow on updated network to confirm solvability
6. Compare total network impedance before and after
