# eGon2025v4 Build Summary

**Date:** 2026-03-02
**Source:** eGon2025v3 (8,756 buses, 15,060 lines, 535 transformers, 3 HVDC links)
**Script:** `scripts/build_v4.py --apply`

## v3 → v4 Delta

| Component    | v3     | v4     | Delta   |
|--------------|--------|--------|---------|
| Buses        | 8,756  | 7,687  | -1,069  |
| Lines        | 15,060 | 12,911 | -2,149  |
| Transformers | 535    | 535    | 0       |
| HVDC links   | 3      | 3      | 0       |
| Generators   | 0      | 0      | 0       |
| Loads         | 0      | 0      | 0       |

## Pipeline Phases

### Phase 1: Copy Scenario
Copied all 8 grid tables from `eGon2025v3` to `eGon2025v4`.

### Phase 2: 110 kV Substation Simplification (400 m)
Re-clustered 110 kV buses at a tighter 400 m radius (v1→v2 used 200 m). 220/380 kV buses were left untouched.

- 819 spatial clusters found (>1 bus)
- 751 connected merge groups after connectivity splitting
- 1,069 buses removed (merged into representatives)
- 2,149 lines deleted (became self-loops after merging)
- 0 transformers lost
- Connected components preserved: 4 → 4

Reused `spatial_cluster()`, `split_by_connectivity()`, `build_mapping()`, `apply_remapping()`, `delete_self_loops()`, `delete_orphaned_buses()` from `scripts/simplify_substations.py`.

### Phase 3: Similarity-Based Parameter Assignment
Assigned electrical parameters to 1,082 unmatched 220/380 kV lines using per-km medians derived from the 1,351 JAO-matched lines.

- Medians computed per `(v_nom, cables)` group from JAO-matched lines
- Fallback to voltage-only median if cable group has < 5 samples
- 110 kV lines: 0 bad params found (all already good)

Key median values used:

| Voltage | Cables | r/km     | x/km     | s_nom (MVA) | Sample Size |
|---------|--------|----------|----------|-------------|-------------|
| 220 kV  | 3      | 0.058667 | 0.317837 | 491.6       | 367         |
| 220 kV  | 6      | 0.045572 | 0.321456 | 419.9       | 17          |
| 380 kV  | 3      | 0.028011 | 0.261101 | 1,790.2     | 925         |
| 380 kV  | 6      | 0.025925 | 0.255283 | 1,895.6     | 37          |

### Phase 4: Missing Transformer Detection
Flagged multi-voltage substation clusters that have zero transformers.

- Only buses identified as actual substations were considered (from `egon_ehv_substation`, `egon_hvmv_substation`, and `osmtgmod_results.bus_data` with OSM substation IDs) — 2,559 out of 7,684 buses
- Waypoint nodes on transmission lines are excluded to avoid false positives
- Clusters that already have at least one transformer are skipped (142 clusters OK)
- **28 clusters flagged** with zero transformers (mostly 380-110 kV gaps)

### Phase 5: Validation
All checks passed:

- Connected components: 4 → 4 (preserved)
- Self-loops: 0 lines, 0 transformers
- Voltage mismatches: 0 (all lines connect same-voltage buses)
- Orphaned buses: 0
- HVDC links: 3 preserved, 3 foreign buses preserved
- Line parameters: 0 lines with r≤0, x≤0, or s_nom≤0

### Phase 6: Interactive Map
Generated `results/v4/v4_build_map.html` — dark-themed Leaflet map with toggleable layers:

- **v4 grid:** voltage-colored lines, buses, transformers, HVDC links
- **Removed buses:** red markers at original v3 positions (1,069)
- **Similarity-updated lines:** teal/cyan highlight with before→after tooltip (1,082)
- **Missing transformer flags:** orange markers with voltage and bus details (28)

### Phase 7: Reports

| File | Content |
|------|---------|
| `results/v4/summary.json` | Full build statistics |
| `results/v4/node_mapping.csv` | 1,069 bus merge mappings (old_bus → new_bus) |
| `results/v4/similarity_updates.csv` | 1,082 lines with before/after r, x, b, s_nom |
| `results/v4/missing_transformers.csv` | 28 flagged substation clusters |
| `results/v4/v4_build_map.html` | Interactive map |

## Files Created

| File | Lines | Purpose |
|------|-------|---------|
| `scripts/build_v4.py` | ~810 | Main 7-phase pipeline |
| `scripts/reduction/v4/__init__.py` | 1 | Package init |
| `scripts/reduction/v4/degree2_elimination.py` | ~160 | Degree-2 bus elimination class (used by existing tests) |

## How to Reproduce

```bash
conda activate egon2025

# Dry run (no DB changes)
python scripts/build_v4.py --dry-run

# Build v4 in database
python scripts/build_v4.py --apply
```

## Verification

```sql
-- v3 should be untouched
SELECT COUNT(*) FROM grid.egon_etrago_bus WHERE scn_name = 'eGon2025v3';
-- → 8756

-- v4 counts
SELECT COUNT(*) FROM grid.egon_etrago_bus WHERE scn_name = 'eGon2025v4';
-- → 7687
SELECT COUNT(*) FROM grid.egon_etrago_line WHERE scn_name = 'eGon2025v4';
-- → 12911
SELECT COUNT(*) FROM grid.egon_etrago_transformer WHERE scn_name = 'eGon2025v4';
-- → 535
SELECT COUNT(*) FROM grid.egon_etrago_link WHERE scn_name = 'eGon2025v4';
-- → 3
```
