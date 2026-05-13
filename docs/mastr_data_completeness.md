# MaStR Data Completeness Analysis

**Date**: 2026-01-24
**Purpose**: Comprehensive audit of available MaStR data vs. what we currently have

## Executive Summary

**Current Status**: We have INCOMPLETE data. Missing critical datasets for generators, loads, grid connections, and detailed technical information.

**Current Downloads**:
- ✅ Wind (basic)
- ✅ Solar (basic)
- ✅ Conventional/Combustion (basic)
- ❌ **MISSING 80%+ of available data**

---

## 1. Available MaStR Data Categories

### A. GENERATION UNITS (Stromerzeugung)

#### Currently Downloaded (3 of 8):
1. **wind** - 31,976 records ✅
2. **solar** - 5,709,377 records ✅
3. **combustion** (conventional) - 81,516 records ✅

#### MISSING (5 of 8):
4. **biomass** ❌ - Biomass power plants (biogas, wood, etc.)
5. **hydro** ❌ - Hydroelectric plants (run-of-river, reservoir, pumped storage)
6. **gsgk** ❌ - Geothermal, mine gas, pressure relaxation
7. **nuclear** ❌ - Nuclear power plants
8. **storage** ❌ - Battery storage units (critical for grid balancing!)

### B. CONSUMPTION/LOAD DATA ❌ **COMPLETELY MISSING**

9. **electricity_consumer** ❌
   - Large industrial consumers (>50 MW typically)
   - Essential for modeling industrial loads
   - Includes voltage level, capacity, location

10. **gas_consumer** ❌
    - Gas consumption units
    - Relevant for sector coupling

### C. GRID INFRASTRUCTURE ❌ **COMPLETELY MISSING**

11. **grid_connections** (Netzanschlusspunkte) ❌
    - Connection points to transmission/distribution grid
    - Voltage level information
    - Capacity data
    - **CRITICAL**: Links generators to specific grid buses

12. **grids** (Netze) ❌
    - Grid operator areas
    - Network boundaries
    - DSO/TSO mappings

### D. GAS INFRASTRUCTURE ❌ **MISSING**

13. **gas_producer** ❌ - Gas generation units
14. **gas_storage** ❌ - Gas storage facilities
15. **gas_storage_extended** ❌ - Detailed gas storage data

### E. SUPPORT/SUBSIDY DATA ❌ **MISSING**

For each technology, there are subsidy tables with additional technical and economic data:

16. **wind_eeg** ❌ - EEG subsidy data for wind
17. **solar_eeg** ❌ - EEG subsidy data for solar
18. **biomass_eeg** ❌ - EEG subsidy data for biomass
19. **hydro_eeg** ❌ - EEG subsidy data for hydro
20. **gsgk_eeg** ❌ - EEG subsidy data for gsgk
21. **storage_eeg** ❌ - EEG subsidy data for storage

22. **kwk** (CHP - Combined Heat and Power) ❌
    - CHP subsidy data
    - Applies to biomass, combustion, gsgk

### F. PERMIT DATA ❌ **MISSING**

23. **permit** ❌
    - Licensing/permit information for all units
    - Planned commissioning dates
    - Regulatory status

### G. LOCATION DATA ❌ **MISSING**

24. **locations_extended** ❌
    - Detailed geographic information
    - Better coordinates than basic unit data
    - Multiple location types:
      - location_elec_generation
      - location_elec_consumption
      - location_gas_generation
      - location_gas_consumption

### H. MARKET PARTICIPANT DATA ❌ **MISSING**

25. **market_actors** ❌ - Companies, operators
26. **market_roles** ❌ - Operator assignments, roles

### I. ADMINISTRATIVE DATA ❌ **MISSING**

27. **balancing_area** ❌ - Balancing zones
28. **deleted_units** ❌ - Decommissioned units (important for 2025 cutoff)
29. **deleted_market_actors** ❌
30. **retrofit_units** ❌ - Unit upgrades/modifications
31. **changed_dso_assignment** ❌ - Grid operator changes
32. **storage_units** ❌ - Storage system groupings

---

## 2. Data Quality Issues with Current Downloads

### Current CSV Structure:
Our current CSVs appear to be **manually filtered/processed**, NOT the raw MaStR extended tables.

#### Missing Fields in Current Data:

**Wind** - Should have ~80 fields, we have ~9:
- Missing: Manufacturer, model designation, turbine technology
- Missing: Hub height precision, rotor blade de-icing system
- Missing: Black start capability, island operation capability
- Missing: Remote controllability (grid operator, direct marketer)
- Missing: EEG data (feed-in tariff, subsidy type, auction award)
- Missing: Permit data
- Missing: Grid connection point (MaStR number)
- Missing: Detailed location data

**Solar** - Should have ~70 fields, we have ~7:
- Missing: Module count, module type, inverter capacity
- Missing: Orientation (azimuth), tilt angle
- Missing: Mounting type details (rooftop, ground-mounted, facade)
- Missing: Tenant power (Mieterstrom) status
- Missing: Direct marketing status
- Missing: EEG data
- Missing: Shared inverter with storage info
- Missing: Grid connection point

**Combustion** - Should have ~60 fields, we have ~8:
- Missing: Main fuel, additional fuels
- Missing: Thermal capacity (for CHP units)
- Missing: CHP status and data
- Missing: Emergency generator flag
- Missing: Grid reserve status
- Missing: Net capacity vs. gross capacity
- Missing: Combined operation (Kombibetrieb) data
- Missing: Black start capability

---

## 3. Critical Missing Data for Grid Modeling

### For PyPSA Network Building:

#### GENERATORS:
❌ **Missing Technologies**:
- Biomass (~8,000-10,000 units expected)
- Hydro (~7,000-8,000 units expected, includes pumped storage)
- Storage (~300,000+ battery units expected)
- Nuclear (~3-5 operating units in 2025)
- GSGK (~100-200 units expected)

❌ **Missing Technical Parameters** (ALL technologies):
- Actual grid connection point (bus assignment)
- Voltage level at connection point
- Installed vs. net capacity
- P_max_pu (maximum output per unit)
- Efficiency values
- Ramp rates
- Minimum stable operation level

❌ **Missing Operational Data**:
- Remote controllability (required for grid services)
- Black start capability
- Island operation capability
- Grid reserve participation
- Prequalification for control energy

#### LOADS:
❌ **COMPLETELY MISSING**:
- Industrial consumers (electricity_consumer table)
- Location data
- Voltage level
- Load profiles
- Controllability/DSM capability

#### GRID CONNECTIONS:
❌ **COMPLETELY MISSING**:
- Grid connection points (Netzanschlusspunkte)
- Connection point capacity
- Voltage level
- Grid operator assignment
- Balancing area

---

## 4. What We Need to Download

### Priority 1 - CRITICAL (Required for basic power flow):

1. **ALL Generation Technologies - Extended Tables**:
   ```
   - biomass_extended
   - hydro_extended
   - gsgk_extended
   - nuclear_extended
   - storage_extended
   - wind_extended (re-download with all fields)
   - solar_extended (re-download with all fields)
   - combustion_extended (re-download with all fields)
   ```

2. **Grid Connections**:
   ```
   - grid_connections (Netzanschlusspunkte)
   - grids (Netze)
   ```

3. **Locations**:
   ```
   - locations_extended
   ```

### Priority 2 - IMPORTANT (For accurate modeling):

4. **Electricity Consumers**:
   ```
   - electricity_consumer
   ```

5. **EEG Subsidy Data** (for all technologies):
   ```
   - wind_eeg
   - solar_eeg
   - biomass_eeg
   - hydro_eeg
   - gsgk_eeg
   - storage_eeg
   ```

6. **CHP Data**:
   ```
   - kwk (for biomass, combustion, gsgk)
   ```

7. **Permit Data**:
   ```
   - permit
   ```

### Priority 3 - USEFUL (For validation and completeness):

8. **Market Participants**:
   ```
   - market_actors
   - market_roles
   ```

9. **Administrative**:
   ```
   - balancing_area
   - deleted_units (to exclude decommissioned)
   - retrofit_units
   ```

---

## 5. Download Methods

### Method 1: Bulk Download (Recommended - Fastest)
Downloads entire XML files, then parses into SQLite database:

```python
from open_mastr import Mastr

db = Mastr()

# Download all generation technologies
db.download(method='bulk', data=['wind', 'solar', 'biomass', 'hydro',
                                   'gsgk', 'combustion', 'nuclear', 'storage'])

# Download grid and location data
db.download(method='bulk', data=['grid', 'location'])

# Download consumption data
db.download(method='bulk', data=['electricity_consumer'])

# Download administrative data
db.download(method='bulk', data=['permit', 'market', 'balancing_area',
                                   'deleted_units', 'retrofit_units'])
```

### Method 2: SOAP API (Slower, more control)
Query specific units or date ranges:

```python
from open_mastr.soap_api import MaStRAPI

api = MaStRAPI()

# Example: Get all wind units
wind_units = api.GetListeAlleEinheiten(
    einheittyp='Windeinheit',
    limit=10000
)
```

---

## 6. Data Volume Estimates

Based on open-MaStR documentation and MaStR statistics:

| Data Type | Estimated Records | Est. Size |
|-----------|------------------|-----------|
| Solar (all) | ~5.7M | 5-10 GB |
| Wind | ~32K | 50-100 MB |
| Biomass | ~10K | 20-30 MB |
| Hydro | ~8K | 15-25 MB |
| Combustion | ~82K | 100-200 MB |
| Nuclear | ~5 | < 1 MB |
| Storage | ~300K+ | 500 MB - 1 GB |
| GSGK | ~200 | 1-2 MB |
| Electricity Consumer | ~5K-10K | 10-20 MB |
| Grid Connections | ~100K+ | 100-200 MB |
| Locations | ~6M+ | 5-10 GB |
| **TOTAL** | **~12M records** | **15-25 GB** |

---

## 7. Recommended Download Strategy

### Step 1: Setup Database
```bash
# Create MaStR SQLite database (will be at ~/open-mastr-data/)
```

### Step 2: Bulk Download Core Data
Priority order:
1. All generation technologies (wind, solar, biomass, hydro, gsgk, combustion, nuclear, storage)
2. Grid connections and networks
3. Locations
4. Electricity consumers
5. EEG/KWK subsidy data (automatic with technologies)
6. Permits
7. Market actors
8. Administrative data

### Step 3: Export to CSV/Database
Convert SQLite to our PostgreSQL database or CSV files for processing

### Step 4: Data Integration
Link generators to grid buses using:
1. Grid connection point (NetzanschlusspunktMastrNummer)
2. Voltage level (Spannungsebene)
3. Geographic location (lat/lon) + spatial matching

---

## 8. Integration with eGon Database

We should integrate MaStR data into our PostgreSQL database:

### Proposed Schema:
```sql
-- Create MaStR schema
CREATE SCHEMA mastr;

-- Import all extended tables
CREATE TABLE mastr.wind_extended (...);
CREATE TABLE mastr.solar_extended (...);
CREATE TABLE mastr.biomass_extended (...);
CREATE TABLE mastr.hydro_extended (...);
CREATE TABLE mastr.gsgk_extended (...);
CREATE TABLE mastr.combustion_extended (...);
CREATE TABLE mastr.nuclear_extended (...);
CREATE TABLE mastr.storage_extended (...);

-- Import grid data
CREATE TABLE mastr.grid_connections (...);
CREATE TABLE mastr.grids (...);

-- Import consumer data
CREATE TABLE mastr.electricity_consumer (...);

-- Import location data
CREATE TABLE mastr.locations_extended (...);
```

### Linking Strategy:
```sql
-- Join generators to grid buses via grid connection points and voltage level
SELECT
    g.generator_id,
    m.EinheitMastrNummer as mastr_number,
    m.Nettonennleistung as capacity_mw,
    m.Spannungsebene as voltage_level,
    m.NetzanschlusspunktMastrNummer as grid_connection,
    gc.voltage_level as connection_voltage,
    -- Spatial join to find nearest bus
    b.bus_id,
    ST_Distance(m.geom, b.geom) as distance_m
FROM mastr.wind_extended m
LEFT JOIN mastr.grid_connections gc
    ON m.NetzanschlusspunktMastrNummer = gc.NetzanschlusspunktMastrNummer
LEFT JOIN grid.egon_etrago_bus b
    ON b.v_nom = gc.voltage_level_kv
    AND b.scn_name = 'eGon2025'
WHERE m.EinheitBetriebsstatus = 'InBetrieb'
    AND m.Inbetriebnahmedatum <= '2025-12-31'
ORDER BY distance_m
LIMIT 1;
```

---

## 9. Next Steps

1. ✅ Review this analysis
2. ⏳ Confirm download priorities
3. ⏳ Set up open-mastr bulk download
4. ⏳ Download all Priority 1 data (~15-20 GB)
5. ⏳ Import into PostgreSQL database
6. ⏳ Create integration scripts to link MaStR → eGon grid buses
7. ⏳ Validate data completeness

---

## Sources

- [MaStR Official API Documentation](https://www.marktstammdatenregister.de/MaStRHilfe/subpages/webdienst.html)
- [MaStR API Documentation V25.2 (PDF)](https://www.marktstammdatenregister.de/MaStRHilfe/files/webdienst/2025-10-01%20Dokumentation%20MaStR%20Webdienste%20V25.2.pdf)
- [open-MaStR Documentation](https://open-mastr.readthedocs.io/)
- [open-MaStR GitHub Repository](https://github.com/OpenEnergyPlatform/open-MaStR)
- [open-MaStR Dataset Documentation](https://open-mastr.readthedocs.io/en/latest/dataset/)
