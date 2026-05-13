# German Transmission Grid Model — Version Pipeline (V1 → V5)

This document describes the full pipeline that transforms raw OpenStreetMap topology data into a parameterized, PyPSA-compatible power flow model of the German transmission grid (110/220/380 kV) for the year 2025.

Each version is stored as a separate PostgreSQL scenario (`scn_name`) in the `grid` schema.

## Version Summary

| Version | Scenario | Buses | Lines | Trafos | HVDC | Key Change |
|---------|----------|------:|------:|-------:|-----:|------------|
| V1 | `eGon2025` | 14,494 | 26,489 | 535 | 0 | Raw OSM topology |
| V2 | `eGon2025v2` | 8,925 | 15,516 | 535 | 0 | Substation simplification |
| V3 | `eGon2025v3` | 8,756 | 15,060 | 535 | 3 | Degree-2 elimination + JAO parameters |
| V4 | `eGon2025v4` | 7,687 | 12,911 | 544 | 3 | 110 kV re-clustering + similarity params |
| V5 | `eGon2025v5` | 7,704 | 12,911 | 548 | 14 | Offshore HVDC + topology corrections |

---

## V1: Raw OSM Topology (Baseline)

**Scenario:** `eGon2025`
**Source:** eGon-data pipeline processing OpenStreetMap data through osmtgmod

The eGon-data pipeline extracts the German transmission grid from OpenStreetMap, where substations are modeled as many internal nodes (bus-bar segments, switchgear). This produces a high-resolution but overly detailed topology.

**Initial issues fixed before V2:**
- Line parameters (r, x, s_nom) were all zero — fixed by copying from the eGon2025v6 reference scenario
- Transformer reactance (x) was zero for all 535 units — fixed with JAO-matched values (72) and defaults (463)

**Counts:** 14,494 buses · 26,489 lines · 535 transformers · 0 generators · 0 loads

---

## V1 → V2: Substation Simplification

**Script:** `scripts/simplify_substations.py`

OSM models substations with excessive internal detail. This step merges co-located buses into single representative nodes.

### Algorithm
1. **Spatial clustering** using Union-Find + KDTree, per voltage level:
   - 110 kV: 200 m radius
   - 220/380 kV: 1,000 m radius
2. **Connectivity split** via BFS — 99 of 3,308 spatial clusters were split to avoid merging topologically disconnected but geographically close substations
3. **Representative selection:** highest-degree bus in each cluster survives
4. **Remapping:** all component endpoints (lines, transformers, links) updated to point to surviving buses
5. **Cleanup:** self-loops deleted, orphaned buses removed

### Results

| Component | V1 | V2 | Change |
|-----------|---:|---:|--------|
| Buses | 14,494 | 8,925 | −38.4% |
| Lines | 26,489 | 15,516 | −41.4% |
| Transformers | 535 | 535 | unchanged |
| Connected components | 1 | 1 | preserved |

**Output:** `results/simplification/node_mapping.csv`

---

## V2 → V3: Degree-2 Elimination + JAO Parameterization

Two operations are applied to produce V3: waypoint elimination and JAO parameter assignment.

### Degree-2 Waypoint Elimination

**Script:** `scripts/simplify_degree2.py`

Removes pass-through waypoint buses (degree-2 nodes with no equipment) at 220/380 kV only. When a bus is eliminated, the two incident lines are merged:
- r, x, length: summed (series connection)
- s_nom: minimum of the two (bottleneck)
- b: summed (parallel capacitance)

**Eligibility criteria:** exactly 2 line connections, no transformer, no OSM substation ID, no different-voltage bus within 1 km, 220 or 380 kV only.

**Result:** 172 buses and 456 lines removed.

### JAO Parameter Application

**Scripts:** `scripts/jao_matching.py` + `scripts/apply_jao_v3.py`

Applies real electrical parameters from the JAO (Joint Allocation Office) 8th release dataset to the OSM-derived grid.

#### Bus Matching (4-tier)

JAO provides 422 buses at 220/380 kV. Matching proceeds through progressively relaxed tiers:

| Tier | Method | Threshold | Matched |
|------|--------|-----------|---------|
| 0 | OSM ID exact match | — | 358 (86.5%) |
| 1 | KDTree spatial | 500 m | 50 (12.1%) |
| 2 | Spatial + degree heuristic | 2 km | 4 (1.0%) |
| 3 | Spatial + degree heuristic | 5 km | 2 (0.5%) |

**Result:** 414/422 JAO buses matched (98.1%)

#### Line Parameter Updates (1,351 lines)

Graph-based Dijkstra matching finds the shortest path between matched endpoint clusters per voltage level. JAO parameters (r, x, b, s_nom) are distributed proportionally by segment length along multi-hop paths.

- 767/867 JAO lines matched (88.5%)
- 1,351 eGon line segments received JAO-derived parameters

**Key unit conversions:**
- JAO s_nom is per-phase → multiply by √3 for 3-phase
- JAO B is in µS → multiply by 1e-6 for Siemens

#### Transformer Parameters (535/535)

| Source | Count | Method |
|--------|------:|--------|
| JAO matched | 27 | Per-unit r/x converted from JAO s_nom base to eGon s_nom base |
| Default 220/380 kV | remaining at 220/380 | x=0.04, r=0.0005 |
| Default 110/220 or 110/380 kV | remaining | x=0.12, r=0.003 |

**Threshold note:** V3 transformer x values are near-zero (~3e-6), not exactly zero. The threshold `x < 0.001` is used to detect unparameterized transformers.

#### HVDC Links (3 added)

| Link | Capacity | German Bus | Foreign Endpoint |
|------|----------|------------|------------------|
| ALEGRO | 1,000 MW | 35187 (Oberzier, 380 kV) | Belgium |
| NordLink | 1,400 MW | 38907 (Wilster, 380 kV) | Norway |
| Baltic Cable | 600 MW | 21261 (Herrenwyk, 380 kV) | Sweden |

All links: efficiency=0.98, bidirectional (p_min_pu=−1, p_max_pu=1)

### Final V3 Counts

| Component | V2 | V3 | Change |
|-----------|---:|---:|--------|
| Buses | 8,925 | 8,756 | −169 |
| Lines | 15,516 | 15,060 | −456 |
| Transformers | 535 | 535 | unchanged |
| HVDC links | 0 | 3 | +3 |

**Output files:** `results/jao_v3/line_updates.csv`, `results/jao_v3/trafo_updates.csv`, `results/jao_v3/eic_mapping.csv`, `results/jao_v3/v3_jao_map.html`

---

## V3 → V4: 110 kV Re-clustering + Parameter Completion

**Script:** `scripts/build_v4.py`
**Built:** 2026-03-02

This step performs a second round of 110 kV bus simplification at a tighter radius and fills remaining parameter gaps using similarity-based estimation.

### Phase 1: Copy Scenario
All 8 grid tables copied from V3 to V4 via pandas read/write.

### Phase 2: 110 kV Re-clustering (400 m radius)

The V1→V2 clustering used 200 m for 110 kV, which left many nearby buses unsimplified. This phase re-clusters at 400 m using the same Union-Find + KDTree + BFS connectivity algorithm. 220/380 kV buses are untouched.

- 819 spatial clusters found
- 751 merge groups after connectivity splitting
- 1,069 buses removed
- 2,149 lines deleted (became self-loops)
- 0 transformers lost

### Phase 3: Similarity-Based Parameter Assignment (1,082 EHV lines)

After JAO matching, 1,082 lines at 220/380 kV still lacked JAO-derived parameters. These are filled using per-km medians from JAO-matched lines, grouped by (voltage, cable count):

| Voltage | Cables | r/km | x/km | s_nom (MVA) | JAO Samples |
|---------|--------|------|------|-------------|-------------|
| 220 kV | 3 | 0.0587 | 0.3178 | 491.6 | 367 |
| 220 kV | 6 | 0.0456 | 0.3215 | 419.9 | 17 |
| 380 kV | 3 | 0.0280 | 0.2611 | 1,790.2 | 925 |
| 380 kV | 6 | 0.0259 | 0.2553 | 1,895.6 | 37 |

Multi-circuit lines (cables > 3) are scaled: s_nom × n_circuits, r ÷ n_circuits, x ÷ n_circuits, b × n_circuits.

### Phase 4: Missing Transformer Detection + Insertion

Substations with multiple voltage levels but no interconnecting transformer are flagged. 28 clusters detected; 9 received new transformers (trafo_id ≥ 31366):
- Default parameters: x=0.12, r=0.003, s_nom=1,200 MVA
- Connects highest-degree bus at each voltage level

### Phase 5: Validation
- Connected components: 4 (preserved from V3)
- Self-loops: 0
- Voltage mismatches: 0
- Orphaned buses: 0
- Lines with bad parameters: 0
- HVDC links: 3 preserved

### Final V4 Counts

| Component | V3 | V4 | Change |
|-----------|---:|---:|--------|
| Buses | 8,756 | 7,687 | −1,069 |
| Lines | 15,060 | 12,911 | −2,149 |
| Transformers | 535 | 544 | +9 |
| HVDC links | 3 | 3 | unchanged |

**Output files:** `results/v4/summary.json`, `results/v4/node_mapping.csv`, `results/v4/similarity_updates.csv`, `results/v4/missing_transformers.csv`, `results/v4/v4_build_map.html`

---

## V4 → V5: Offshore HVDC + Topology Corrections

**Script:** `scripts/build_v5.py` + manual SQL adjustments
**Built:** 2026-03-04

V5 adds North Sea offshore wind farm HVDC connections, the Kontek interconnector, Baltic Sea offshore wind connections, and several topology corrections.

### Step 1: Copy V4 → V5
All 8 grid tables copied via pandas.

### Step 2: Connectivity Fix — Buses 13371 ↔ 6700
Added short 110 kV line (line 33300, 0.01 km) connecting two nearby buses at the same substation.

### Step 3: Line Deletions
Deleted lines 33159, 33103 (380 kV topology corrections).

### Step 4: Voltage Upgrades — 220 kV → 380 kV

Three lines upgraded from 220 kV to 380 kV. This required creating new 380 kV buses at substations that only had 220 kV busbars, plus 220/380 kV transformers to maintain connectivity.

**Bus remapping:**

| 220 kV Bus | 380 kV Target | Method |
|------------|---------------|--------|
| 5636 | 35471 | Existing 380 kV neighbor (trafo 30830) |
| 35963 | 5284 | Existing 380 kV neighbor (trafo 31305) |
| 600 | 200630 (new) | Created + trafo 31400 |
| 5633 | 205663 (new) | Created + trafo 31401 |
| 38671 | 238701 (new) | Created + trafo 31402 |

**Upgraded lines** (all set to 2 circuits / 6 cables / 3,580 MVA):

| Line | Bus0 | Bus1 | Length |
|------|------|------|--------|
| 4355 | 200630 | 238701 | 38.0 km |
| 14398 | 205663 | 5284 | 23.7 km |
| 4191 | 205663 | 35471 | 8.6 km |

New 380 kV transformer defaults: s_nom=2,877 MVA, x=0.05423, r=0.000727.

### Step 5: Additional 220 kV Offshore Lines (Baltic Sea)
Added 2 more 220 kV cables between bus 39204 and 40387 (lines 33301, 33302), cloned from line 30823. Total offshore cables to bus 39204: 4.

### Step 6: North Sea Offshore Wind HVDC Connections (11 links)

| HVDC | Wind Farm | Capacity | Onshore Bus | Offshore Bus | Length |
|------|-----------|----------|-------------|-------------|--------|
| 4 | SylWin 1 | 864 MW | 1049 (Büttel) | 200104 | 205 km |
| 5 | HelWin 1 | 576 MW | 1049 (Büttel) | 200105 | 130 km |
| 6 | HelWin 2 | 690 MW | 1049 (Büttel) | 200106 | 130 km |
| 7 | Nordergründe | 111 MW | 19082 | 200107 | 30 km |
| 8 | Riffgat | 108 MW | 20229 (Emden/Ost) | 200108 | 80 km |
| 9 | DolWin 6 | 900 MW | 20229 (Emden/Ost) | 200109 | 90 km |
| 10 | BorWin 1&2 | 1,200 MW | 38263 | 200110 | 200 km |
| 11 | DolWin 3 | 900 MW | 37592 (Dörpen) | 200111 | 83 km |
| 12 | DolWin 1&2 | 1,716 MW | 37592 (Dörpen) | 200112 | 165 km |
| 13 | Alpha Ventus | 60 MW | 3057 | 200113 | 60 km |

### Step 7: Kontek Interconnector (Denmark ↔ Germany)

| HVDC | Capacity | German Bus | Danish Bus | Length |
|------|----------|------------|-----------|--------|
| 14 | Kontek | 600 MW | 35620 (Bentwisch) | 200200 (DK) | 170 km |

All new HVDC links: carrier=DC, efficiency=0.98, bidirectional.

### Post-Script Manual Adjustments

Additional changes applied directly via SQL after the initial `build_v5.py` run:

#### 220 kV Line Deletions (replaced by 380 kV upgrades)
- Deleted lines 14639 (38671↔5633), 14637 (600↔38671), 14401 (5633↔5636), 4192 (5633↔35963)
- These 220 kV lines became redundant after the 380 kV corridor was established

#### New 380 kV Line
- Line 33303: 205663 ↔ 238701 (38.9 km, 2 circuits, 3,580 MVA) — replaces deleted 220 kV route

#### Line 4356 Deletion
- Deleted line 4356 (220 kV, 38671↔5633) — redundant after 380 kV corridor

#### Line 14407 Voltage Upgrade
- Upgraded from 220 kV to 380 kV (5636↔2751 → 35471↔202781)
- Created new 380 kV bus 202781 at location of bus 2751
- Created transformer 31403 (220/380 kV) between bus 2751 and 202781
- Deleted parallel 220 kV line 4190

#### Baltic Sea Offshore Wind (OWP Baltic 1 & 2)
- **Baltic 1** (bus 200301): 54.609°N, 12.651°E — 48.3 MW, 16 km north of Darß-Zingst
- **Baltic 2** (bus 200302): 55.000°N, 13.200°E — 288 MW, 33 km from shore
- 2 × 110 kV lines: Baltic 2 → Baltic 1 (60 km each, lines 33304–33305)
- 2 × 110 kV lines: Baltic 1 → Bentwisch bus 35642 (55 km each, lines 33306–33307)

#### Circuit Count Upgrades
- Line 30825 (40386↔40387): upgraded to 6 circuits (983 MVA)
- All 4 lines between 39204↔40387: upgraded to 6 circuits each (983 MVA)

### Final V5 Counts

| Component | V4 | V5 | Change |
|-----------|---:|---:|--------|
| Buses | 7,687 | 7,704 | +17 |
| Lines | 12,911 | 12,911 | ±0 (adds and deletes cancel) |
| Transformers | 544 | 548 | +4 |
| HVDC links | 3 | 14 | +11 |

**Breakdown of V5 bus additions (17):**
- 3 new 380 kV buses for voltage upgrades (200630, 205663, 238701)
- 1 new 380 kV bus for line 14407 upgrade (202781)
- 10 offshore wind farm buses (200104–200113)
- 1 Danish bus for Kontek (200200)
- 2 Baltic offshore wind buses (200301, 200302)

**Total HVDC capacity in V5:** 3,000 MW (cross-border V4) + 7,125 MW (North Sea offshore) + 600 MW (Kontek) = **10,725 MW**

---

## Key Parameters Reference

### Clustering Radii

| Stage | Voltage | Radius |
|-------|---------|--------|
| V1→V2 | 110 kV | 200 m |
| V1→V2 | 220/380 kV | 1,000 m |
| V3→V4 | 110 kV | 400 m |

### Typical Line Parameters (per km)

| Voltage | Cables | r/km | x/km | b/km | s_nom (MVA) |
|---------|--------|------|------|------|-------------|
| 110 kV | 3 | — | — | — | 260 |
| 220 kV | 3 | 0.0587 | 0.3178 | — | 491.6 |
| 380 kV | 3 | 0.0269 | 0.2579 | 4.94e-6 | 1,790 |

### Transformer Defaults

| Voltage Pair | x (pu) | r (pu) | s_nom (MVA) |
|-------------|--------|--------|-------------|
| 110/220 kV | 0.12 | 0.003 | 1,200 |
| 110/380 kV | 0.12 | 0.003 | 1,200 |
| 220/380 kV | 0.04 | 0.0005 | 2,877 |
| 220/380 kV (V5 new) | 0.05423 | 0.000727 | 2,877 |

---

## Script Inventory

| Script | Stage | Purpose |
|--------|-------|---------|
| `scripts/simplify_substations.py` | V1→V2 | Spatial bus clustering |
| `scripts/simplify_degree2.py` | V2→V3 | Degree-2 waypoint elimination |
| `scripts/jao_matching.py` | V3 | JAO bus/line/trafo matching |
| `scripts/apply_jao_v3.py` | V3 | Apply JAO parameters to database |
| `scripts/build_v4.py` | V3→V4 | 110 kV re-clustering + similarity params |
| `scripts/build_v5.py` | V4→V5 | Offshore HVDC + topology corrections |
| `scripts/create_v1_v5_comparison.py` | Viz | Interactive 5-version comparison map |

---

## Visualization

The interactive comparison map at `results/v5/grid_comparison_v1_v5.html` shows all 5 versions with:
- Version switching buttons (V1/V2/V3/V4/V5)
- Voltage level toggles (380/220/110 kV)
- Parallel line spread animation
- HVDC links with named popups (V4/V5)
- New transformer highlighting (V4/V5)
- Similarity-updated line highlighting (V4/V5)
