# Grid v3 Summary: From Raw OSM to JAO-Parameterized Network

**Date:** March 2026
**Final scenario:** `eGon2025v3` — 8,753 buses, 15,060 lines, 535 transformers, 3 HVDC links

---

## Overview

The goal was to build a PyPSA-compatible power flow model of the German 110/220/380 kV transmission grid for 2025. The starting point was OpenStreetMap topology extracted through the eGon-data pipeline, which gives accurate geographic topology but lacks real electrical parameters (impedances, thermal ratings). The JAO/CORE-TSO 8th release dataset provides official TSO-reported parameters but uses a different, coarser node model.

The pipeline bridges these two worlds: simplify the fine-grained OSM topology to a level where it can be matched against JAO's substation-level model, then transfer JAO's electrical parameters onto the simplified grid.

```
eGon2025 (v1)  ──simplify_substations──▶  eGon2025v2  ──simplify_degree2──▶  eGon2025v3
  14,494 buses                               8,925 buses                        8,753 buses
  26,489 lines                              15,516 lines                       15,060 lines
     535 trafos                                535 trafos                          535 trafos
                                                                                     │
                                                                        apply_jao_v3 │
                                                                                     ▼
                                                                          1,351 lines updated
                                                                            535 trafos fixed
                                                                          3 HVDC links added
```

---

## Stage 1: v1 → v2 — Substation Simplification

**Script:** `scripts/simplify_substations.py`
**Problem:** OSM models substations with many internal nodes (bus-bar segments, switchgear, isolators). This creates thousands of very short lines (<100m) that are modeling artifacts. JAO treats each substation as a single node, so we need to collapse these internal nodes to enable matching.

### Algorithm

1. **Spatial clustering** using Union-Find with KDTree, per voltage level:
   - 110 kV: 200 m radius
   - 220/380 kV: 1,000 m radius

2. **Connectivity split** — each spatial cluster is further split by BFS reachability within the cluster. This prevents merging buses that are geographically close but topologically disconnected (e.g., two parallel substations separated by a river). 99 out of 3,308 spatial clusters were split this way.

3. **Representative selection** — the bus with the highest degree (most line connections) in each sub-cluster becomes the surviving representative.

4. **Remapping** — all component endpoints (lines, transformers, generators, loads, storage, stores, links) are remapped to point to the representative bus. Self-loops (lines where both ends collapse to the same bus) are deleted.

5. **Validation** — connected components preserved (still 1), no self-loops, no voltage mismatches, no orphaned buses.

### Results

| Metric | Before (v1) | After (v2) | Change |
|--------|-------------|------------|--------|
| Buses | 14,494 | 8,925 | -38.4% |
| Lines | 26,489 | 15,516 | -41.4% |
| Transformers | 535 | 535 | unchanged |
| Connected components | 1 | 1 | preserved |

**Output:** Scenario `eGon2025v2` in database + `results/simplification/node_mapping.csv`

---

## Stage 2: v2 → v3 — Degree-2 Waypoint Elimination

**Script:** `scripts/simplify_degree2.py`
**Problem:** After substation simplification, many 220/380 kV buses remain that are pure pass-through waypoints — they have exactly two line connections and host no equipment. These add unnecessary complexity without representing real decision points in the network.

### Algorithm

A bus is eligible for elimination if ALL of these hold:
1. Exactly 2 line connections (degree-2 in the line graph)
2. No transformer connected
3. No OSM substation ID recorded
4. Not in the `grid.egon_ehv_substation` table
5. No different-voltage bus within 1 km
6. Voltage is 220 or 380 kV only
7. No link, generator, load, storage, or store connected

When a bus is eliminated, the two incident lines are merged:
- **r, x:** summed (series combination)
- **s_nom:** minimum of the two (bottleneck)
- **length:** summed
- **b:** summed (parallel capacitance)

### Results

| Metric | Before (v2) | After (v3) | Change |
|--------|-------------|------------|--------|
| Buses | 8,925 | 8,753 | -172 (-1.9%) |
| Lines | 15,516 | 15,060 | -456 |
| Transformers | 535 | 535 | unchanged |
| Connected components | 1 | 1 | preserved |

**Output:** Scenario `eGon2025v3` in database

---

## Stage 3: JAO Parameter Matching and Application

**Scripts:** `scripts/jao_matching.py` (matching engine) + `scripts/apply_jao_v3.py` (database application)

### Data Sources

| Source | Content | Records |
|--------|---------|---------|
| JAO 8th release XLSX | Official TSO grid model (50Hertz, TenneT, Amprion, TransnetBW) | 422 buses, 867 lines, 127 trafos |
| Georef CSV | Substation coordinates + OSM IDs | 588 entries |
| eGon EHV substations | `grid.egon_ehv_substation` table with OSM IDs | 458 entries |
| eGon2025v3 | Simplified grid topology | 8,753 buses, 15,060 lines, 535 trafos |

### Step 1: Bus Clustering

The 1,137 eGon v3 buses at 220/380 kV are grouped into 946 clusters to create a node model comparable to JAO's substation-level view:

- **Named clusters (458):** Built around buses that have an OSM substation ID in `egon_ehv_substation`
- **Absorbed buses (174):** Buses within 200 m of a named cluster center, absorbed into that cluster
- **Spatial clusters (488):** Remaining buses clustered via Union-Find with KDTree

### Step 2: Bus Matching (4-tier)

Each of JAO's 422 German substations is matched to an eGon cluster:

| Tier | Method | Threshold | Confidence | Matched | Share |
|------|--------|-----------|------------|---------|-------|
| 0 | OSM ID exact match via georef | — | 1.0 | 358 | 86.5% |
| 1 | KDTree spatial nearest | 500 m | 0.9 | 50 | 12.1% |
| 2 | Spatial + degree heuristic | 2 km | 0.7 | 4 | 1.0% |
| 3 | Spatial + degree heuristic | 5 km | 0.5 | 2 | 0.5% |
| — | Unmatched | — | — | 8 | 1.9% |

**Result:** 414/422 JAO buses matched (98.1%), mean confidence 0.976

### Step 3: Line Matching (graph-based Dijkstra)

For each JAO line, the algorithm:
1. Resolves both endpoint substations to their matched eGon clusters
2. Builds a cluster-level graph per voltage level (380 kV: 590 nodes/681 edges; 220 kV: 454 nodes/475 edges)
3. Finds the shortest path (weighted by line length) between the two endpoint clusters
4. Validates the path: total eGon path length must be between 0.3x and 3.0x the JAO-reported length
5. For multi-circuit corridors, ranks eGon lines by s_nom and pairs them with JAO circuits

**Results:**

| Metric | Count | Rate |
|--------|-------|------|
| JAO lines matched | 767/867 | 88.5% |
| eGon line segments updated | 1,000/1,731 (220/380 kV) | 57.8% |
| Mean path length ratio | 0.99 | — |

Path hop distribution:

| Hops | Lines | Share |
|------|-------|-------|
| 1 | 271 | 27.1% |
| 2 | 335 | 33.5% |
| 3 | 231 | 23.1% |
| 4+ | 163 | 16.3% |

### Step 4: Parameter Transfer

**Line parameters** (for 1,351 eGon lines via `apply_jao_v3.py`):
- **r, x, b:** Distributed proportionally by segment length in multi-hop paths
  - `param_segment = param_jao * (length_segment / total_path_length)`
- **s_nom:** Computed from 3-phase formula: `s_nom = √3 * V_kV * Imax_A / 1000`

**Transformer parameters** (535/535 fixed):
- **27 from JAO matching:** Per-unit r/x converted from JAO's s_nom base to eGon's s_nom base. For parallel transformers, impedance combined in parallel equivalent.
- **508 from defaults:** eGon v3 transformers had near-zero x (~3e-6, not exactly zero), requiring a threshold of `x < 0.001` to detect. Defaults applied:
  - 220/380 kV: x=0.04, r=0.0005
  - 110/220 or 110/380 kV: x=0.12, r=0.003

### Step 5: HVDC Links (3 added)

| Link | Capacity | German Bus (v3) | Foreign Side |
|------|----------|-----------------|--------------|
| ALEGRO | 1,000 MW | 35187 (Oberzier 380 kV) | Belgium |
| NordLink | 1,400 MW | 38907 (Wilster 380 kV) | Norway |
| Baltic Cable | 600 MW | 21261 (Herrenwyk 380 kV) | Sweden |

Bus ID mapping through the simplification pipeline was required (e.g., v1 bus 38906 → v3 bus 38907 for Wilster, v1 bus 36614 → v3 bus 21261 for Herrenwyk).

### Step 6: Interactive Map

An interactive Leaflet HTML map (`results/jao_v3/v3_jao_map.html`, 2.6 MB) was generated with:
- Voltage-level toggles (380/220/110 kV)
- Component toggles (buses, lines, transformers, HVDC links)
- JAO overlay showing matched corridors
- Hover tooltips with before/after parameter values
- Bus names from the EHV substation table

---

## Unit Conversion Pitfalls

Several non-obvious unit issues were discovered and resolved:

1. **JAO s_nom is per-phase:** The XLSX reports single-phase MVA. Must multiply by √3 for 3-phase (PyPSA convention). Confirmed by: `s_nom_jao / (√3 * V * Imax) ≈ 1/√3` for all lines with Imax data.

2. **JAO transformer r/x are per-unit on individual s_nom base:** Cannot use directly — must convert to Ohms first, then back to per-unit on eGon's (often different) s_nom base.

3. **JAO B is in microSiemens:** Multiply by 1e-6 for Siemens before writing to database.

4. **numpy types crash psycopg2:** `np.float64` values cannot be passed as SQL parameters to psycopg2. A `_native()` helper converts them to Python `float`.

5. **v3 transformer x ≈ 3e-6 (not zero):** The degree-2 elimination slightly perturbs impedance values, so the "needs defaults" check must use `x < 0.001` rather than `x == 0`.

---

## Output Files

```
results/jao_v3/
├── eic_mapping.csv      # 1,087 EIC code mappings (JAO line → eGon line/trafo)
├── line_updates.csv     # 1,351 lines with updated r, x, b, s_nom
├── trafo_updates.csv    # 535 transformers (27 JAO + 508 defaults)
└── v3_jao_map.html      # Interactive Leaflet map (2.6 MB)
```

---

## Script Inventory

| Script | Stage | Purpose |
|--------|-------|---------|
| `scripts/simplify_substations.py` | v1→v2 | Spatial clustering of internal substation nodes |
| `scripts/simplify_degree2.py` | v2→v3 | Eliminate pure pass-through waypoints |
| `scripts/jao_matching.py` | v3 matching | Core matching engine (buses, lines, trafos) |
| `scripts/apply_jao_v3.py` | v3 application | Apply matched parameters to DB + HVDC + map |
| `scripts/lib/node_mapping.py` | utility | Track bus ID transformations across stages |
| `scripts/utils/name_matching.py` | utility | Substation name normalization and fuzzy matching |
| `scripts/utils/spatial_matching.py` | utility | KDTree-based spatial matching |
| `scripts/reduction/core/topology.py` | utility | Union-Find, graph analysis |
| `scripts/reduction/core/electrical_params.py` | utility | Impedance conversion, validation |

---

## Supporting Infrastructure

### Alternative Approach: TSO Grid Pipeline

A parallel approach was explored in `scripts/tso_grid/` (5 sequential scripts) that builds the 220/380 kV backbone directly from JAO data and connects eGon's 110 kV layer underneath. This produced the `eGon2025_tso` scenario (12,093 buses). The v3 approach was preferred because it preserves the full OSM topology while enriching it with JAO parameters, rather than replacing it.

### Tests

| Test File | Coverage |
|-----------|----------|
| `tests/test_simplify_substations.py` | Clustering, connectivity split, mapping, validation |
| `tests/test_simplify_degree2.py` | Waypoint detection, impedance merging |
| `tests/test_topology.py` | Connected components, paths |
| `tests/test_electrical_params.py` | Unit conversions, impedance math |
| `tests/test_database_connection.py` | DB connectivity |

---

## Final v3 Grid Statistics

| Component | Count | JAO-Updated |
|-----------|-------|-------------|
| Buses (total) | 8,753 | — |
| Buses (220/380 kV) | 1,137 | — |
| Buses (110 kV) | 7,616 | — |
| Lines (total) | 15,060 | — |
| Lines (220/380 kV) | 1,731 | 1,351 (78%) |
| Lines (110 kV) | 13,329 | — |
| Transformers | 535 | 535 (100%) |
| HVDC Links | 3 | 3 (new) |
| Connected components | 1 | — |
