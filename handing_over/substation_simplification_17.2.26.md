# Substation Simplification — eGon2025 → eGon2025v2

**Date:** 17 February 2026

## Problem

The eGon2025 grid topology is derived from OpenStreetMap, which models substations with many internal nodes (bus-bar segments, switchgear connections). This creates thousands of short lines (<500m) that are artifacts of OSM detail, not real transmission elements. JAO/CORE-TSO treats each substation as a single node.

**Before simplification:**
- 14,494 buses
- 26,489 lines
- 535 transformers
- 1 connected component

## What Was Done

Created `scripts/simplify_substations.py` — a script that collapses intra-substation bus nodes into single representative buses per voltage level, producing the `eGon2025v2` scenario in the database.

### Algorithm

1. **Spatial clustering** (Union-Find + KDTree) per voltage level:
   - 110 kV: 200m radius
   - 220 kV: 1000m radius
   - 380 kV: 1000m radius

2. **Connectivity split** (critical safety step): Each spatial cluster is split into connected sub-clusters using only lines between cluster members. This prevents merging spatially close but topologically disconnected buses (e.g. 784 unconnected 220kV pairs within 1km, 1,365 at 380kV).

3. **Representative selection**: For each connected sub-cluster, the bus with the highest line-degree is chosen as the representative (tiebreak: lowest bus_id). All other buses in the group are remapped to the representative.

4. **Remapping**: All component endpoints (lines, transformers, generators, loads, storage, stores, links) are updated in the database.

5. **Cleanup**: Self-loops (lines/transformers where bus0 = bus1 after remapping) are deleted. Orphaned buses (not referenced by any component) are deleted.

### Pipeline Phases

| Phase | Action |
|-------|--------|
| 1 | Copy eGon2025 → eGon2025v2 (all 8 grid tables) |
| 2 | Load buses, lines, transformers from target scenario |
| 3 | Spatial clustering per voltage level |
| 4 | Split clusters by graph connectivity (BFS) |
| 5 | Build node mapping (highest-degree = representative) |
| 6 | SQL UPDATE remapping on all component tables |
| 7 | Delete self-loops |
| 8 | Delete orphaned buses |
| 9 | Validation and export |

## Results

| Metric | eGon2025 | eGon2025v2 | Change |
|--------|----------|------------|--------|
| Buses | 14,494 | 8,925 | -5,569 (-38.4%) |
| Lines | 26,489 | 15,516 | -10,973 (-41.4%) |
| Transformers | 535 | 535 | 0 |
| Connected components | 1 | 1 | preserved |

### Clustering Statistics

- 3,308 spatial clusters identified (>1 bus)
- 9,107 buses in multi-bus clusters
- After connectivity split: 3,209 connected sub-clusters
- Cluster sizes: min=2, max=19, median=2
- 99 spatial clusters were split by the connectivity safety step

### Validation (all passed)

- Connected components: 1 → 1 (preserved)
- Self-loops: 0
- Lines with voltage mismatch: 0 (all lines connect same-voltage buses)
- Transformers with same voltage: 0 (all transformers connect different-voltage buses)
- Orphaned buses: 0

## Files Created

| File | Purpose |
|------|---------|
| `scripts/simplify_substations.py` | Main simplification script |
| `tests/test_simplify_substations.py` | 16 unit tests + 3 integration tests |
| `scripts/create_simplification_map.py` | Interactive HTML comparison map generator |
| `grid_simplification_comparison.html` | Interactive map: eGon2025 vs eGon2025v2 |
| `results/simplification/node_mapping.csv` | Full bus mapping (old_bus → new_bus) |
| `results/simplification/summary.json` | Machine-readable summary |

## How to Use

### Run simplification (already done)

```bash
# Dry-run (no DB changes)
python scripts/simplify_substations.py --dry-run

# Apply
python scripts/simplify_substations.py --apply

# Custom radii
python scripts/simplify_substations.py --apply --radius-110 300 --radius-220 800 --radius-380 1500
```

### Run tests

```bash
# Unit tests (no DB needed)
pytest tests/test_simplify_substations.py -v -k "not integration"

# Integration tests (needs DB with eGon2025)
pytest tests/test_simplify_substations.py -v -k integration
```

### Generate comparison map

```bash
python scripts/create_simplification_map.py
# Output: grid_simplification_comparison.html
```

### Query the simplified scenario

```sql
SELECT count(*) FROM grid.egon_etrago_bus WHERE scn_name = 'eGon2025v2';
SELECT count(*) FROM grid.egon_etrago_line WHERE scn_name = 'eGon2025v2';
```

## Interactive Map

The comparison map (`grid_simplification_comparison.html`) has three views:

- **eGon2025** — original grid with all OSM-derived internal substation nodes
- **eGon2025v2** — simplified grid with collapsed substations
- **Diff** — removed lines (red), removed buses (red dots), representative buses (green dots sized by merge count), unchanged lines (grey)

Controls: voltage level toggles (380/220/110 kV), component toggles (lines, transformers, HVDC links, bus markers), clickable popups on all elements.

## Database Scenario

The `eGon2025v2` scenario is live in the database alongside the original `eGon2025`. Both can be queried independently using `scn_name` filtering. The original scenario is untouched.
