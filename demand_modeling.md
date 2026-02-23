# Electricity Demand Modeling – Germany 2025

This document describes the methodology used to create the electricity demand heatmap by municipality for Germany 2025.

## Overview

**Goal**: Model electricity demand at municipality (Gemeinde) level for Germany, split by sector (Households, CTS, Industry), using 2025 reference data.

**Output Files**:
- `demand_heatmap.html` – Interactive choropleth map (7.4 MB)
- `demand_by_municipality_2025.csv` – Demand per municipality (11,135 rows)
- `demand_by_nuts3_2025.csv` – Demand per NUTS-3 region (401 rows)
- `demand_heatmap.py` – Python script to regenerate all outputs

---

## National Totals (2025)

Source: **BDEW / BNetzA** (Bundesnetzagentur 2025)

| Sector | Annual Demand | Share |
|--------|---------------|-------|
| Households | 134 TWh | 30% |
| CTS (Gewerbe, Handel, Dienstleistungen) | 124 TWh | 28% |
| Industry | 190 TWh | 42% |
| **Total** | **448 TWh** | 100% |

Note: Transport sector (19 TWh) excluded as it's mostly rail traction at specific substations.

Peak load 2025: ~76 GW

---

## Data Sources

### 1. DemandRegio (FFE Munich)
- **What**: NUTS-3 level electricity demand data
- **Location**: Bundled with `disaggregator` Python package
- **Path**: `/root/miniconda3/envs/egon2025/lib/python3.10/site-packages/disaggregator/data_in/regional/`
- **Files used**:
  - `elc_consumption_HH_spatial.csv` – Household electricity per NUTS-3 (MWh, 2015)
  - `population.csv` – Population per NUTS-3 (2015)
  - `t_nuts3_lk.csv` – NUTS-3 code mapping (AGS ↔ NUTS)
- **License**: GPLv3
- **URL**: https://github.com/DemandRegioTeam/disaggregator

### 2. Municipality Boundaries
- **Source**: `boundaries.vg250_gem` table in PostgreSQL
- **Count**: 11,135 municipalities
- **Total area**: 357,732 km²
- **Contains**: Geometry, NUTS-3 code, AGS code, name, type (Stadt/Gemeinde)

### 3. Large Industrial Consumers (MaStR)
- **Source**: `mastr.electricity_consumer` table in PostgreSQL
- **Count**: 556 operational consumers
- **Fields**: Name, coordinates, units >50MW count, PLZ, Bundesland
- **Note**: No actual consumption values – only registry metadata

---

## Methodology

### Step 1: Read NUTS-3 Demand Data

Read household electricity consumption from DemandRegio's bundled CSV files:

```python
hh_elc = pd.read_csv('elc_consumption_HH_spatial.csv')
# Filter to year 2015 (latest available)
# Total: 132 TWh across 401 NUTS-3 regions
```

Also read population data as proxy for CTS/Industry distribution.

### Step 2: Scale to 2025 National Totals

**Households**: Scale DemandRegio 2015 data (132 TWh) → 2025 target (134 TWh)
```python
scale_factor = 134 / 132  # ≈ 1.015
nuts3['hh_mwh'] = nuts3['hh_mwh_2015'] * scale_factor
```

**CTS**: Distribute proportional to population per NUTS-3
```python
nuts3['cts_mwh'] = (nuts3['population'] / total_pop) * 124e6  # MWh
```

**Industry**: Distribute proportional to population per NUTS-3
```python
nuts3['ind_mwh'] = (nuts3['population'] / total_pop) * 190e6  # MWh
```

Note: Using population as proxy for CTS/Industry is a first-order approximation. Refinement with actual industrial site data is possible.

### Step 3: Load Municipality Boundaries

Query PostGIS for all German municipalities with NUTS-3 codes:

```sql
SELECT id, gen, bez, nuts, ags_0,
       ST_Transform(geometry, 4326) as geometry,
       ST_Area(ST_Transform(geometry, 3035)) / 1e6 as area_km2
FROM boundaries.vg250_gem
WHERE nuts IS NOT NULL AND nuts LIKE 'DE%'
```

**NUTS Code Fix**: The database uses 2021 NUTS codes, while DemandRegio uses 2016 codes. Two codes required remapping:
- `DEB1C` → `DEB16` (Cochem-Zell, 89 municipalities)
- `DEB1D` → `DEB19` (Rhein-Hunsrück-Kreis, 137 municipalities)

### Step 4: Distribute NUTS-3 Demand to Municipalities

Within each NUTS-3 region, distribute demand to municipalities by **area fraction**:

```python
# Area share within NUTS-3 region
gem['area_share'] = gem['area_km2'] / nuts3_total_area_km2

# Distribute demand
gem['gem_hh_mwh'] = nuts3_hh_mwh * gem['area_share']
gem['gem_cts_mwh'] = nuts3_cts_mwh * gem['area_share']
gem['gem_ind_mwh'] = nuts3_ind_mwh * gem['area_share']
```

This means:
- Cities (small area, high population density) → high demand density (MWh/km²)
- Rural areas (large area, low population) → low demand density
- Total demand per NUTS-3 region is preserved

### Step 5: Load Large Consumers from MaStR

```sql
SELECT "NameStromverbrauchseinheit", "Breitengrad", "Laengengrad",
       "AnzahlStromverbrauchseinheitenGroesser50Mw"
FROM mastr.electricity_consumer
WHERE "EinheitBetriebsstatus" = 'In Betrieb'
  AND "Laengengrad" IS NOT NULL
```

556 operational large consumers with coordinates, displayed as markers on the map.

### Step 6: Generate Interactive HTML Map

Built with **Leaflet.js** and embedded GeoJSON:
- Choropleth coloring with log-scale color ramp
- Radio buttons to switch metric (Total, Density, HH, CTS, Industry)
- Hover tooltip with sector breakdown
- Large consumer markers (toggleable)
- Municipality borders (toggleable)
- Dark CartoDB basemap

Optimization:
- Geometry simplified (0.005° tolerance ≈ 500m)
- Coordinates rounded to 4 decimals (11m precision)
- Final size: 7.4 MB (down from 30 MB)

---

## Results

### Top 10 Municipalities by Total Demand

| Municipality | NUTS-3 | Total (GWh/yr) | Density (MWh/km²) |
|--------------|--------|----------------|-------------------|
| Berlin | DE300 | 19,190 | 21,518 |
| Hamburg | DE600 | 9,616 | 12,950 |
| München | DE212 | 7,907 | 25,392 |
| Köln | DEA23 | 5,782 | 14,218 |
| Frankfurt am Main | DE712 | 3,994 | 16,091 |
| Stuttgart | DE111 | 3,401 | 16,193 |
| Düsseldorf | DEA11 | 3,337 | 15,348 |
| Dortmund | DEA52 | 3,196 | 11,426 |
| Essen | DEA13 | 3,176 | 15,092 |
| Leipzig | DED51 | 3,056 | 10,219 |

### Demand Range

- **Total demand**: 0.003 GWh – 19,190 GWh per municipality
- **Demand density**: 198 – 25,392 MWh/km²/yr
- **Highest density**: München (25,392 MWh/km²)

---

## HTML Map Features

**Controls** (top-left):
- Total Demand (default)
- Demand Density (MWh/km²)
- Households only
- CTS only
- Industry only
- Toggle: Large Consumers (MaStR)
- Toggle: Municipality Borders

**Info Panel** (top-right):
- Hover any municipality to see:
  - Name and type (Stadt/Gemeinde)
  - NUTS-3 code
  - Area (km²)
  - Sector breakdown (HH/CTS/Industry in GWh)
  - Total demand (GWh/yr)
  - Demand density (MWh/km²/yr)

**Legend** (bottom-left):
- Color scale (log-scale)
- Large consumer marker
- Summary statistics

**Consumer Markers**:
- Cyan circles, size proportional to units >50MW
- Hover for name, location, and capacity info

---

## File Outputs

### demand_heatmap.html
Interactive map, open in any browser.

### demand_by_municipality_2025.csv
```
ags,name,type,nuts3,area_km2,hh_mwh,cts_mwh,industry_mwh,total_mwh,total_gwh,density_mwh_per_km2
11000000,Berlin,Stadt,DE300,891.82,5739948,5311594,8138732,19190274,19190.27,21518.17
02000000,Hamburg,Stadt,DE600,742.52,2876157,2661519,4078133,9615809,9615.81,12950.29
...
```

### demand_by_nuts3_2025.csv
```
nuts3,hh_mwh,cts_mwh,ind_mwh,total_mwh,total_gwh,population
DE300,5739948,5311594,8138732,19190274,19190.27,3520031
DE600,2876157,2661519,4078133,9615809,9615.81,1762791
...
```

---

## Limitations & Future Improvements

### Current Limitations

1. **Industry distribution by population**: Real industrial demand is concentrated at specific sites (steel mills, chemical plants, refineries). Using population as proxy smooths this out.

2. **Area-based municipality distribution**: Within NUTS-3, demand is split by area fraction. Population-weighted distribution would be more accurate.

3. **No temporal profiles yet**: This is annual demand only. Hourly load profiles (8,760 values) can be added using BDEW SLP profiles.

4. **MaStR consumers have no consumption values**: Only registry metadata (name, location, unit count). Actual consumption must be estimated.

### Potential Improvements

1. **Industrial site allocation**: Use `mastr.electricity_consumer` locations + known industrial facilities (Hotmaps/Schmidt DB) to allocate industry demand to specific sites.

2. **Population-weighted distribution**: Instead of area fraction, use Census 2011 population per municipality within each NUTS-3.

3. **Add temporal profiles**:
   - BDEW H0 profile for households
   - BDEW G0/SLP profiles for CTS
   - Industrial shift profiles for industry
   - SMARD hourly national load for validation

4. **Connect to HV buses**: Map municipalities to nearest HV bus (Voronoi cells) to create `grid.egon_etrago_load` entries.

---

## How to Regenerate

```bash
# Activate environment
conda activate egon2025

# Run the script
python demand_heatmap.py

# Output:
# - demand_heatmap.html (7.4 MB)
# - demand_by_municipality_2025.csv
# - demand_by_nuts3_2025.csv
```

---

## References

- **BDEW**: Die Energieversorgung 2025 – https://www.bdew.de/
- **BNetzA/SMARD**: https://www.smard.de/
- **DemandRegio**: https://github.com/DemandRegioTeam/disaggregator
- **VG250 (BKG)**: Administrative boundaries – https://gdz.bkg.bund.de/
- **MaStR**: Marktstammdatenregister – https://www.marktstammdatenregister.de/
