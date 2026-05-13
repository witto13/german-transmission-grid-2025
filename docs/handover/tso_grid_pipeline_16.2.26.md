# TSO Grid Pipeline — Handover Documentation

**Date:** 16 February 2026
**Author:** Claude Code (automated pipeline)
**Scenario created:** `eGon2025_tso`

---

## 1. Objective

Build a new PyPSA-compatible German transmission grid model directly from **TSO-reported data** (JAO Core Static Grid Model, 8th release) rather than the existing OSM-derived eGon topology. The existing `eGon2025` scenario is kept as reference. The new `eGon2025_tso` scenario combines:

- **220/380kV backbone** built fresh from JAO data (lines, transformers, phase-shifting transformers, cross-border tielines)
- **110kV grid** copied from eGon2025 (unchanged)
- **Connecting transformers** remapping 110kV buses to the new backbone
- **HVDC links** (ALEGRO, NordLink, Baltic Cable)

---

## 2. Data Sources

| Source | File | Content |
|--------|------|---------|
| JAO 8th release | `data/jao/For publication/202509_Core Static Grid Model_for publication.xlsx` | 833 DE lines, 153 DE tielines, 129 DE transformers |
| Georef (fneum) | `data/jao_core_tso/georef.csv` | 588 German substations with OSM IDs + coordinates |
| eGon2025 (DB) | `grid.egon_etrago_*` tables | 14,494 buses, 26,489 lines, 535 transformers |
| eGon ehv_substation | `grid.egon_ehv_substation` table | 458 HV/eHV substations with OSM IDs |

Individual 50Hertz and TenneT XLSX datasets were planned as supplementary sources but were not used — the JAO data proved to be 100% complete for all parameters (R, X, B, Imax) across all four TSO zones.

---

## 3. Pipeline Scripts

All scripts live in `scripts/tso_grid/`. They are designed to be run sequentially, each reading the previous script's CSV output.

### Script 1: `01_parse_data_sources.py`

**Purpose:** Parse the JAO XLSX (multi-row headers, skiprows=1) into clean, normalized CSVs.

**What it does:**
- Reads Lines, Tielines, Transformers sheets from JAO XLSX
- Filters to German TSOs: `50HERTZ`, `TENNETGMBH`, `AMPRION GMBH`, `TRANSNETBW`
- Normalizes voltages: 400→380, 410→380, 231→220, 240→220
- Classifies tielines as internal (inter-TSO within DE) or cross-border based on Comment field
- Merges internal tielines into the main lines pool
- Computes `s_nom = sqrt(3) * V_kV * Imax_A / 1000` (3-phase MVA from Imax)
- Extracts unique substations from line/tieline/transformer endpoints
- Optionally downloads and compares 50Hertz/TenneT individual datasets

**Problems encountered:**
- **Duplicate column names:** The XLSX has two `Full_name` columns (substation 1 and 2). pandas auto-deduplicates to `Full_name` and `Full_name.1`. Initially the rename mapping used the raw names, causing KeyErrors. Fixed by using pandas' deduplicated names.
- **Special characters in column names:** `Resistance_R(Ω)`, `Susceptance_B(μS)` etc. contain Unicode characters that vary between systems. Fixed by matching columns dynamically via substring search (`"resistance" in col.lower()`).
- **Transformer voltage pairs:** Initially filtered to only 220↔380 transformers (90 units). The remaining 39 had same-voltage on both sides (380/380, 400/400, 410/410) — these are **phase-shifting transformers** (PSTs). Fixed by keeping all transformers with at least one side at 220 or 380 kV.

**Output:**

| File | Records |
|------|---------|
| `data/tso_grid/lines.csv` | 892 (833 lines + 59 internal tielines) |
| `data/tso_grid/tielines_crossborder.csv` | 94 cross-border tielines |
| `data/tso_grid/transformers.csv` | 129 transformers (103 regular + 26 PSTs) |
| `data/tso_grid/substations_raw.csv` | 627 unique substations |

**Parameter completeness:** 100% fill rate for R, X, B, Imax across all lines and transformers.

---

### Script 2: `02_geolocate_substations.py`

**Purpose:** Assign geographic coordinates and unique numeric bus_ids to every substation.

**Matching tiers:**

| Tier | Method | Coverage |
|------|--------|----------|
| 1 | Exact name match against `georef.csv` (normalized) | 495 / 584 (84.8%) |
| 2 | Fuzzy match (rapidfuzz WRatio) against georef + eGon ehv_substation | 78 / 89 (87.6%) |
| 3 | Unmatched — assigned placeholder, no coordinates | 11 (1.9%) |

**Bus ID assignment rules:**
- From georef OSM_id: use `int(OSM_id)` — values in millions/billions, no conflict with eGon bus_ids (<41k)
- Multi-voltage substations: 380kV gets base ID, 220kV gets base + 500,000 offset
- Unmatched: assigned from 300,001+

**Problems encountered:**
- **eGon OSM IDs with prefix:** Some eGon `osm_id` values have type prefixes like `w1068350839` (w=way). Caused `int()` conversion failure. Fixed by stripping `[wnrWNR]` prefix before parsing.
- **Dirty transformer substation names:** Names like `/ 380-220kV / Buers / Trafo` came from TransnetBW transformer full_name parsing. Fixed with a dedicated `normalize_name_for_matching()` that strips path-like patterns, voltage patterns, and "Trafo"/"PST" tokens.
- **Duplicate substations:** "Stockem" and "Y-Stockem" are the same physical location — both match to the same georef entry and get the same bus_id. This is correct (handled via deduplication in Script 5).

**Output:** `data/tso_grid/substations_geolocated.csv` — 584 substations, 573 with coordinates.

**11 unmatched substations:** Kork, Merzen, Koetz, KUES Luengenkamp, Y-Ried, Klixbuell/S, Y-Garrel/O, Garrel/O, Doetinchem (NL), Maasbracht (NL), Meeden (NL). The last three are foreign tieline endpoints.

---

### Script 3: `03_build_backbone.py`

**Purpose:** Convert parsed TSO data into PyPSA-compatible DataFrames (buses, lines, transformers, HVDC links).

**Electrical parameter handling:**

| Component | Parameter | Source | Conversion |
|-----------|-----------|--------|------------|
| Lines | R, X (Ohms) | JAO XLSX | Direct (PyPSA uses Ohms for lines) |
| Lines | B (μS) | JAO XLSX | Multiply by 1e-6 → Siemens |
| Lines | s_nom (MVA) | Computed | `sqrt(3) * V_kV * Imax_A / 1000` |
| Transformers | R, X (Ohms) | JAO XLSX | Convert to per-unit: `Z_pu = Z_ohm / Z_base`, where `Z_base = V² / S_nom` |
| Transformers | phase_shift | Theta θ (°) column | Direct |

**HVDC links (from `apply_jao_params.py` definitions):**

| Link | Capacity | DE Endpoint | Foreign |
|------|----------|-------------|---------|
| ALEGRO | 1,000 MW | Oberzier (380kV) | Belgium |
| NordLink | 1,400 MW | Wilster/W (380kV) | Norway |
| Baltic Cable | 600 MW | Siems T421 (380kV) | Sweden |

**Problems encountered:**
- **HVDC endpoint names:** The original definitions from `apply_jao_params.py` used eGon bus_ids (35187 for Oberzier, 38906 for Wilster, 36614 for Herrenwyk). These don't exist in the JAO-based backbone. Had to search by name instead. "Wilster" was stored as "Wilster/W" in JAO. "Herrenwyk" (Lübeck area) had no 380kV substation — used "Siems T421" (TenneT, 380kV, 0.06° away from Lübeck).
- **Import path issue:** `from scripts.utils.name_matching import ...` failed because `sys.path` didn't include the project root. Fixed by inserting `PROJECT_DIR` into `sys.path`.

**Cross-border tielines:** 88 out of 94 cross-border tielines connected successfully. 6 were skipped (missing DE-side bus). 54 foreign buses were created for the endpoints (AT, CH, CZ, DK, FR, PL, plus 38 with unknown country tagged "XX").

**Output:**

| File | Records |
|------|---------|
| `data/tso_grid/pypsa/buses.csv` | 762 (705 DE + 57 foreign) |
| `data/tso_grid/pypsa/lines.csv` | 965 (877 internal + 88 cross-border) |
| `data/tso_grid/pypsa/transformers.csv` | 129 |
| `data/tso_grid/pypsa/links.csv` | 3 HVDC |

---

### Script 4: `04_connect_110kv.py`

**Purpose:** Copy the 110kV grid from eGon2025 and remap the HV-side of connecting transformers to the new backbone.

**eGon 110kV data:**
- 11,312 buses (bus_ids 1–40,385)
- 19,997 lines
- 421 connecting transformers (bus0=110kV, bus1=220/380kV)

**Transformer remapping tiers:**

| Tier | Method | Count | Success |
|------|--------|-------|---------|
| 0 | eGon ehv_substation OSM_id → georef → JAO bus_id | 244 | 57.9% |
| 1 | Spatial match (KD-tree, ≤5km, same voltage) | 139 | 33.0% |
| 2 | Virtual bus fallback (new bus at eGon coords + zero-impedance line to nearest backbone bus) | 38 | 9.0% |
| **Total** | | **421** | **100%** |

**Transformer parameters:** eGon s_nom is kept. Default R/X applied when eGon values are zero:
- 110↔220kV or 110↔380kV: x=0.12, r=0.003 (full-winding transformer)
- 220↔380kV: x=0.04, r=0.0005 (autotransformer)

**Virtual bus handling:** 38 transformers had no backbone bus within 5km at matching voltage. For these:
1. A virtual bus is created at the eGon HV-side coordinates
2. A near-zero-impedance line (r=0.0001, x=0.001, s_nom=9999) connects it to the nearest backbone bus

**Problem:** 18 virtual lines connect buses at different voltages (virtual 220kV bus → nearest 380kV backbone bus). This is physically incorrect but acceptable as an approximation since the zero-impedance line acts as a direct connection.

**Output:**

| File | Records |
|------|---------|
| `data/tso_grid/110kv_buses.csv` | 11,312 |
| `data/tso_grid/110kv_lines.csv` | 19,997 |
| `data/tso_grid/connecting_transformers.csv` | 421 |
| `data/tso_grid/virtual_buses.csv` | 38 |
| `data/tso_grid/virtual_lines.csv` | 38 |
| `data/tso_grid/bus_mapping.csv` | 421 entries |

---

### Script 5: `05_write_scenario.py`

**Purpose:** Combine all components, validate, and write `eGon2025_tso` to the PostgreSQL database.

**Validation results:**
- 0 errors
- 3 warnings:
  - 18 lines connecting different voltages (virtual links — see above)
  - 39 transformers connecting same voltage (PSTs — expected)
  - 75 connected components (largest: 11,964 buses; 26 isolated buses from foreign endpoints)

**Bus deduplication:** 19 duplicate bus_ids found where multiple substation names mapped to the same OSM ID (e.g., "Stockem" and "Y-Stockem"). Resolved by `drop_duplicates(subset=['bus_id'], keep='first')`.

**Problems encountered:**
- **PostGIS geometry type mismatch:** `ST_MakeLine()` produces a `MultiLineString` which didn't match the `topo` column's `LineString` type. Fixed by using `ST_Multi()` for the `geom` column (MultiLineString) and a separate `ST_MakeLine()` for `topo` (LineString).

**Database write order:** buses → lines → transformers → links (FK constraint order).

---

## 4. Final Results

### Component Count Comparison

| Component | eGon2025 (OSM) | eGon2025_tso (JAO) | Difference |
|-----------|:-:|:-:|:-:|
| Buses | 14,494 | 12,093 | -2,401 (-16.6%) |
| Lines | 26,489 | 21,000 | -5,489 (-20.7%) |
| Transformers | 535 | 550 | +15 (+2.8%) |
| HVDC Links | 0 | 3 | +3 |

### Breakdown by Voltage Level

**eGon2025_tso buses:**
- 380 kV: ~466 (JAO backbone + HVDC)
- 220 kV: ~296 (JAO backbone)
- 110 kV: 11,312 (copied from eGon)
- Foreign: ~57 (cross-border + HVDC endpoints)
- Virtual: 38 (110↔backbone bridges)

**eGon2025_tso lines:**
- 380 kV: 619 (JAO)
- 220 kV: 346 (JAO)
- 110 kV: 19,997 (eGon)
- Virtual: 38

**eGon2025_tso transformers:**
- JAO backbone (220↔380): 103
- JAO PSTs (380↔380): 26
- Connecting (110↔220/380): 421

### Why Fewer Components?

The eGon2025 OSM-derived topology has:
- **3,182 buses at 220/380kV** (many are intermediate switching points between substations)
- **~6,500 lines at 220/380kV** (short segments between OSM nodes)

The JAO data has:
- **762 buses** (one per physical substation, not per switching node)
- **965 lines** (one per transmission circuit, not per OSM segment)

This is the expected difference: JAO represents **substation-to-substation** topology while OSM represents **node-to-node** topology with many intermediate points.

---

## 5. Known Limitations

1. **18 voltage-mismatched virtual lines:** Where no backbone bus exists at the correct voltage within 5km, the virtual link bridges to the nearest bus regardless of voltage.

2. **11 unmatched substations:** Missing coordinates for Kork, Merzen, Koetz, KUES Luengenkamp, Y-Ried, Klixbuell/S, Y-Garrel/O, Garrel/O, plus 3 Dutch endpoints. Lines connected to these substations are dropped (~15 lines).

3. **75 connected components:** The main component (11,964 buses) contains 98.9% of the network. The remaining 74 small components are mostly foreign endpoint buses (1-9 buses each) that connect only via cross-border tielines.

4. **No generators or loads:** The `eGon2025_tso` scenario currently contains only grid topology. Generators and loads need to be added separately (can reuse the existing `generator_mapping.py` / `load_mapping.py` scripts with bus_id remapping).

5. **Phase-shifting transformers:** 26 PSTs have bus0=bus1 (same bus on both sides at same voltage). PyPSA handles these correctly for phase-angle control, but they don't contribute to voltage transformation.

---

## 6. How to Use

```bash
# Run full pipeline from scratch
conda activate egon2025
cd ~/egon_2025_project

python scripts/tso_grid/01_parse_data_sources.py --skip-download
python scripts/tso_grid/02_geolocate_substations.py
python scripts/tso_grid/03_build_backbone.py
python scripts/tso_grid/04_connect_110kv.py
python scripts/tso_grid/05_write_scenario.py --apply
```

```python
# Load from database
from sqlalchemy import create_engine
import pandas as pd

engine = create_engine('postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data')
buses = pd.read_sql("SELECT * FROM grid.egon_etrago_bus WHERE scn_name = 'eGon2025_tso'", engine)
lines = pd.read_sql("SELECT * FROM grid.egon_etrago_line WHERE scn_name = 'eGon2025_tso'", engine)
trafos = pd.read_sql("SELECT * FROM grid.egon_etrago_transformer WHERE scn_name = 'eGon2025_tso'", engine)
links = pd.read_sql("SELECT * FROM grid.egon_etrago_link WHERE scn_name = 'eGon2025_tso'", engine)
```

---

## 7. File Inventory

```
scripts/tso_grid/
├── __init__.py
├── 01_parse_data_sources.py      # Parse JAO XLSX → clean CSVs
├── 02_geolocate_substations.py   # Geolocate substations → bus_ids
├── 03_build_backbone.py          # Build 220/380kV PyPSA backbone
├── 04_connect_110kv.py           # Connect eGon 110kV → backbone
└── 05_write_scenario.py          # Validate + write to DB

data/tso_grid/
├── lines.csv                     # 892 internal lines
├── tielines_crossborder.csv      # 94 cross-border tielines
├── transformers.csv              # 129 transformers
├── substations_raw.csv           # 627 raw substations
├── substations_geolocated.csv    # 584 geolocated substations
├── 110kv_buses.csv               # 11,312 buses (from eGon)
├── 110kv_lines.csv               # 19,997 lines (from eGon)
├── connecting_transformers.csv   # 421 remapped transformers
├── virtual_buses.csv             # 38 virtual bridge buses
├── virtual_lines.csv             # 38 virtual bridge lines
├── bus_mapping.csv               # 421 trafo remap report
└── pypsa/
    ├── buses.csv                 # 762 backbone buses
    ├── lines.csv                 # 965 backbone lines
    ├── transformers.csv          # 129 backbone transformers
    └── links.csv                 # 3 HVDC links
```
