# eGon Database & Dataset Analysis

**Analysis Date**: 2026-01-24
**Purpose**: Understanding what's in the 230 GB database and 226 GB eGon-data repository

---

## Executive Summary

### Current Database Size: **230 GB**

**Critical Finding**: **97.4% of your database (224 GB) is raw OpenStreetMap data that you NO LONGER NEED after grid extraction.**

### Breakdown:
| Component | Size | Status | Action |
|-----------|------|--------|--------|
| **OSM Raw Data** (public schema) | **224 GB** | ❌ Not needed | ⚠️ **DELETE** |
| **MaStR Data** (mastr schema) | **5.6 GB** | ✅ Actively using | ✅ **KEEP** |
| **Boundaries** (vg250) | **123 MB** | ✅ Useful | ✅ **KEEP** |
| **Grid Model** (grid schema) | **78 MB** | ✅ Actively using | ✅ **KEEP** |
| **OSM Processing Results** (osmtgmod) | **14 MB** | ⚠️ Intermediate | ⚠️ Can delete |
| **Metadata/Scenario** | **144 KB** | ✅ Needed | ✅ **KEEP** |

**After cleanup**: 230 GB → **~6 GB** (reduction of 224 GB / 97%)

---

## Detailed Database Breakdown

### 1. OSM Raw Data (Public Schema) - **224 GB** ❌ **DELETE**

**Purpose**: Raw OpenStreetMap data used to extract grid topology
**Status**: ✅ Grid already extracted → **No longer needed**

#### Tables (Top 15):

| Table | Size | Description | Needed? |
|-------|------|-------------|---------|
| nodes | 62 GB | OSM node coordinates | ❌ DELETE |
| way_nodes | 52 GB | OSM way-node relationships | ❌ DELETE |
| osm_ways | 34 GB | OSM way geometries | ❌ DELETE |
| osm_nodes | 24 GB | OSM node attributes | ❌ DELETE |
| osm_polygon | 19 GB | OSM polygon features | ❌ DELETE |
| ways | 16 GB | OSM ways table | ❌ DELETE |
| osm_line | 9 GB | OSM line features | ❌ DELETE |
| osm_point | 4 GB | OSM point features | ❌ DELETE |
| relation_members | 2 GB | OSM relations | ❌ DELETE |
| osm_roads | 1 GB | OSM road network | ❌ DELETE |
| osm_rels | 841 MB | OSM relation attributes | ❌ DELETE |
| power_ways | 360 MB | Power line ways | ❌ DELETE |
| relations | 189 MB | OSM relation table | ❌ DELETE |
| power_line | 173 MB | Power lines extracted | ❌ DELETE |
| Other tables | ~500 MB | Various OSM processing | ❌ DELETE |

**What This Data Was Used For**:
- eGon-data's osmTGmod module extracts grid topology from OSM
- Identified substations (14,494 buses)
- Identified transmission lines (26,489 lines)
- Identified transformers (535 transformers)

**Why You Don't Need It Anymore**:
- Grid extraction is **COMPLETE**
- All grid data is in `grid.egon_etrago_*` tables (78 MB)
- OSM data is only needed if you want to re-extract the grid from scratch
- You have the final grid model - the raw OSM data is like keeping concrete mixer after building is finished

**Safe to Delete**: ✅ YES - Your grid model is already extracted and stored in the `grid` schema

---

### 2. MaStR Data (mastr schema) - **5.6 GB** ✅ **KEEP & USING**

**Purpose**: Complete German power plant registry (Marktstammdatenregister)
**Status**: ✅ Currently importing, actively using

#### Current Tables (14 imported so far):

| Table | Size | Records | Description | Using? |
|-------|------|---------|-------------|--------|
| solar_extended | 2.9 GB | 5,853,282 | All solar PV units | ✅ YES |
| solar_eeg | 1.0 GB | 5,853,282 | Solar EEG subsidies | ✅ YES |
| storage_extended | 1.1 GB | 2,324,959 | Battery storage units | ✅ YES |
| wind_extended | 25 MB | 41,765 | Wind turbines | ✅ YES |
| combustion_extended | 45 MB | 92,837 | Coal/Gas/Oil plants | ✅ YES |
| biomass_extended | 13 MB | 23,907 | Biomass plants | ✅ YES |
| wind_eeg | 10 MB | 41,765 | Wind EEG subsidies | ✅ YES |
| hydro_extended | 4 MB | 8,792 | Hydro plants | ✅ YES |
| gsgk_extended | 200 KB | 325 | Geothermal/mine gas | ✅ YES |
| nuclear_extended | 16 KB | 6 | Nuclear plants | ✅ YES |
| Others | ~500 MB | Various | EEG, CHP, permits | ✅ YES |

**Still Importing** (23 more tables):
- grid_connections (5.4M records) - **CRITICAL** for linking to buses
- locations_extended (6.2M records) - Better coordinates
- electricity_consumer (943 records) - Industrial loads
- market_actors, permits, etc.

**Expected Final Size**: ~12-15 GB when import completes

**Why You Need This**:
- **Primary source** for generator data (2025 scenario)
- Contains all 8 generation technologies (wind, solar, biomass, hydro, etc.)
- **grid_connections** table is CRITICAL for linking generators to grid buses
- Detailed technical parameters (capacity, voltage level, coordinates)
- EEG subsidy data for operational parameters

**Safe to Delete**: ❌ NO - This is your primary generation unit database

---

### 3. Grid Model (grid schema) - **78 MB** ✅ **KEEP & USING**

**Purpose**: Your actual transmission grid model (110/220/380 kV)
**Status**: ✅ Actively using, core of your project

#### Tables:

| Table | Size | Records (eGon2025) | Description | Using? |
|-------|------|-------------------|-------------|--------|
| egon_etrago_bus | 16 MB | 14,494 | Substations/buses | ✅ YES |
| egon_etrago_line | 45 MB | 26,489 | Transmission lines | ✅ YES |
| egon_hvmv_substation | 7 MB | ~40,000 | HV/MV substations | ⚠️ May need |
| egon_etrago_load_timeseries | 5 MB | Time data | Load profiles | ⚠️ Future |
| egon_hvmv_transfer_buses | 2.5 MB | Transfer points | HV/MV connections | ⚠️ May need |
| egon_ehv_substation | 1 MB | EHV substations | Extra-high voltage | ✅ YES |
| egon_etrago_transformer | 904 KB | 535 | Transformers | ✅ YES |
| egon_etrago_generator | 736 KB | 0 | Generators (empty) | 🔄 Will populate |
| egon_etrago_load | 320 KB | 0 | Loads (empty) | 🔄 Will populate |
| Other tables | ~500 KB | Various | Carriers, links, etc. | ⚠️ Some needed |

**What You're Using**:
- ✅ **Buses** (14,494 substations at 110/220/380 kV)
- ✅ **Lines** (26,489 transmission lines with capacities, lengths)
- ✅ **Transformers** (535 voltage level transformers)
- 🔄 **Generators** (will populate from MaStR)
- 🔄 **Loads** (will populate from MaStR electricity_consumer + demand model)

**Why You Need This**:
- This IS your grid model
- PyPSA network will be built from these tables
- All topology, electrical parameters, coordinates

**Safe to Delete**: ❌ NO - This is the core of your project

---

### 4. Boundaries (boundaries schema) - **123 MB** ✅ **KEEP**

**Purpose**: German administrative boundaries (VG250 dataset)
**Status**: ✅ Useful for geographic analysis

#### Tables:

| Table | Size | Records | Description | Need? |
|-------|------|---------|-------------|-------|
| vg250_gem | 42 MB | 11,135 | Municipalities (Gemeinden) | ✅ YES |
| vg250_gem_clean | 34 MB | 11,135 | Cleaned municipalities | ✅ YES |
| vg250_vwg | 30 MB | ~4,000 | Administrative associations | ⚠️ Maybe |
| vg250_krs | 9 MB | ~400 | Districts (Kreise) | ✅ YES |
| vg250_lan | 4 MB | 16 | Federal states (Bundesländer) | ✅ YES |
| vg250_sta | 2 MB | 1 | Germany boundary | ✅ YES |
| vg250_rbz | 2 MB | ~40 | Government regions | ⚠️ Maybe |

**What You're Using**:
- Currently: Not directly using
- Future: Useful for:
  - Mapping generators to municipalities
  - Regional analysis (state-level generation capacity)
  - Demand allocation by administrative areas
  - Visualizations with boundaries

**Why Keep This**:
- Only 123 MB (0.05% of database)
- Very useful for geographic queries
- Standard reference dataset for Germany
- Needed for regional analysis

**Safe to Delete**: ⚠️ OPTIONAL - Small, useful for future analysis, recommend keeping

---

### 5. OSM Processing Results (osmtgmod_results) - **14 MB** ⚠️ **OPTIONAL**

**Purpose**: Intermediate results from OSM topology modeling
**Status**: ⚠️ Grid extraction complete, may not need

#### Tables:

| Table | Size | Description | Need? |
|-------|------|-------------|-------|
| branch_data | 12 MB | Line/branch connections | ⚠️ Maybe |
| bus_data | 2 MB | Bus extraction data | ⚠️ Maybe |
| dcline_data | 96 KB | DC line data | ⚠️ Maybe |
| results_metadata | 32 KB | Processing metadata | ⚠️ Maybe |

**What This Is**:
- Intermediate results from osmTGmod (OSM topology modeling)
- Used during grid extraction process
- Final results are in `grid.egon_etrago_*` tables

**Why You Might Keep**:
- Useful for debugging/validation
- Very small (14 MB)
- Shows how grid was extracted from OSM

**Safe to Delete**: ⚠️ PROBABLY - Only 14 MB, intermediate data, but could keep for reference

---

### 6. Scenario & Metadata - **144 KB** ✅ **KEEP**

**Purpose**: Scenario parameters and metadata
**Status**: ✅ Needed for scenario configuration

#### Tables:
- `scenario.egon_scenario_parameters` (112 KB) - eGon2025 parameters
- `metadata.*` (32 KB) - Database metadata

**Why You Need This**:
- Defines scenario settings
- Future scenarios may need this
- Tiny size (144 KB)

**Safe to Delete**: ❌ NO - Needed, and negligible size

---

## What's in the eGon-data Repository (226 GB on disk)

### Directory Breakdown:

| Directory | Size | Description | Need? |
|-----------|------|-------------|-------|
| **docker/database-data/** | **231 GB** | PostgreSQL database files | ✅ Active DB |
| src/ | 3.8 MB | Python source code | ✅ Reference |
| docs/ | 25 MB | Documentation | ⚠️ Reference |
| airflow/ | 64 KB | Airflow DAGs | ❌ Don't use |
| tests/ | 12 KB | Test files | ❌ Don't need |
| ci/ | 32 KB | CI/CD configs | ❌ Don't need |
| Config files | ~100 KB | Various configs | ⚠️ Some useful |

**What's in docker/database-data/**: The actual PostgreSQL database (230 GB as analyzed above)

---

## eGon-data Dataset Modules (118 Python files, 32 categories)

### What eGon-data CAN Do (Full Pipeline):

#### **Grid & Topology** ✅ USED
- `osmTGmod` - Extract grid from OpenStreetMap → **USED**
- `etrago_setup` - Setup eTraGo grid model → **USED**
- `electrical_neighbours` - European grid connections → **NOT USED**
- `fix_ehv_subnetworks` - Grid topology fixes → **USED**

#### **Generation (Power Plants)** ❌ NOT USED (using MaStR directly instead)
- `fill_etrago_gen` - Fill generator data from various sources
- `chp` - Combined heat & power
- `mastr` - MaStR integration (we're doing this ourselves)

#### **Demand & Load** ❌ NOT USED YET
- `electricity_demand` - Demand by region
- `electricity_demand_timeseries` - Hourly load profiles
- `demandregio` - Regional demand allocation
- `DSM_cts_ind` - Demand-side management (industry, commerce)
- `electricity_demand_etrago` - Load assignment to buses

#### **Sector Coupling** ❌ NOT USED (out of scope)
- `heat_demand` / `heat_demand_timeseries` - Heat sector
- `heat_supply` - District heating
- `heat_etrago` - Heat in eTraGo
- `emobility` - Electric vehicles (5 modules)
- `industrial_sites` - Industrial consumers
- `industry` - Industrial processes
- `gas_grid` - Gas network
- `gas_areas` - Gas demand areas
- `ch4_prod` / `ch4_storages` - Methane/gas storage
- `hydrogen_etrago` - Hydrogen sector

#### **Weather & Renewables** ❌ NOT USED YET
- `era5` - Weather data (for wind/solar profiles)
- `calculate_dlr` - Dynamic line rating

#### **Geographic & Boundaries** ✅ USED
- `vg250` (boundaries) - German administrative areas → **USED**
- `mv_grid_districts` - Medium voltage districts → **NOT USED**
- `loadarea` - Load areas → **NOT USED**
- `district_heating_areas` - District heating → **NOT USED**

#### **Data Bundling & Export** ⚠️ MAY USE
- `data_bundle` - Export datasets
- `etrago_helpers` - Helper functions

#### **Scenarios** ❌ NOT USED
- `low_flex_scenario` - Low flexibility scenario

**Summary**: You're using **~5%** of eGon-data's capabilities (just grid topology extraction)

---

## What You're Actually Using

### Currently Active:

1. ✅ **Grid Topology** (78 MB in `grid` schema)
   - 14,494 buses (substations)
   - 26,489 lines
   - 535 transformers
   - Source: eGon-data osmTGmod

2. ✅ **MaStR Power Plant Data** (5.6 GB in `mastr` schema, importing)
   - All 8 generation technologies
   - 36M+ records
   - Grid connection points
   - Source: Direct MaStR download via open-mastr

3. ✅ **Boundaries** (123 MB in `boundaries` schema)
   - German municipalities, states, districts
   - Source: eGon-data VG250 dataset

**Total Active Data**: ~6 GB

---

## What You're NOT Using (But Sits in Database)

### ❌ OSM Raw Data - **224 GB**

**Tables**: nodes, way_nodes, osm_ways, osm_nodes, osm_polygon, etc.

**Purpose**: Raw OpenStreetMap data for grid extraction

**Status**:
- ✅ Grid extraction **COMPLETE**
- ❌ No longer needed
- ⚠️ **Wasting 224 GB** (97% of database)

**Can Delete**: ✅ **YES** - Grid is already extracted

**How to Delete**:
```sql
-- Drop all OSM tables in public schema
DROP TABLE IF EXISTS public.nodes CASCADE;
DROP TABLE IF EXISTS public.way_nodes CASCADE;
DROP TABLE IF EXISTS public.osm_ways CASCADE;
-- ... (all OSM tables)

-- Or drop entire public schema and recreate
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
```

**Risk**: ⚠️ LOW - Only needed if you want to re-extract grid from OSM (unlikely)

---

## What You WILL Use in the Future

### High Priority (Definitely Will Use):

1. ✅ **MaStR Grid Connections** (importing now)
   - Links generators to grid buses
   - Essential for network building

2. ✅ **MaStR Locations** (importing now)
   - Precise coordinates for all units
   - Better than unit-level coordinates

3. ✅ **MaStR Electricity Consumers** (importing now)
   - Industrial loads
   - Large consumers to assign to buses

4. ⚠️ **Demand Timeseries** (from eGon or external)
   - Hourly load profiles
   - Currently empty in database
   - May use eGon's demandregio or external data

5. ⚠️ **Weather Data / Renewable Profiles** (not in DB)
   - Wind/solar generation profiles
   - May use renewables.ninja, ERA5, or eGon's weather data

### Medium Priority (Might Use):

6. ⚠️ **Load Allocation** (eGon-data datasets)
   - `electricity_demand_etrago` - Assign demand to buses
   - `demandregio` - Regional demand data
   - Currently not using, but datasets exist

7. ⚠️ **European Grid Connections** (in eGon)
   - `electrical_neighbours` - Cross-border flows
   - May need for realistic model

8. ⚠️ **District/Regional Analysis**
   - MV grid districts
   - Load areas
   - For regional validation

### Low Priority (Probably Won't Use):

9. ❌ **Sector Coupling** (heat, gas, hydrogen, e-mobility)
   - Out of scope for transmission grid model
   - eGon-data has extensive datasets
   - You're focusing on electricity only

10. ❌ **Low Flexibility Scenarios**
    - Scenario variants
    - Not needed for base 2025 model

---

## Recommendations

### Immediate Actions:

#### 1. ✅ **DELETE OSM Raw Data** → Free **224 GB** (97% of database)

**Benefit**: Database size 230 GB → **6 GB**

**Risk**: Low - Grid already extracted, you have final model

**How**:
```sql
-- Option 1: Drop individual OSM tables
DROP TABLE IF EXISTS public.nodes CASCADE;
DROP TABLE IF EXISTS public.way_nodes CASCADE;
DROP TABLE IF EXISTS public.osm_ways CASCADE;
DROP TABLE IF EXISTS public.osm_nodes CASCADE;
DROP TABLE IF EXISTS public.osm_polygon CASCADE;
DROP TABLE IF EXISTS public.ways CASCADE;
DROP TABLE IF EXISTS public.osm_line CASCADE;
DROP TABLE IF EXISTS public.osm_point CASCADE;
DROP TABLE IF EXISTS public.relation_members CASCADE;
DROP TABLE IF EXISTS public.osm_roads CASCADE;
DROP TABLE IF EXISTS public.osm_rels CASCADE;
DROP TABLE IF EXISTS public.power_ways CASCADE;
DROP TABLE IF EXISTS public.relations CASCADE;
DROP TABLE IF EXISTS public.power_line CASCADE;
-- ... (list all OSM tables)

-- Option 2: Nuclear option (careful!)
-- DROP SCHEMA public CASCADE;
-- CREATE SCHEMA public;
-- GRANT ALL ON SCHEMA public TO egon;
-- GRANT ALL ON SCHEMA public TO public;
```

**Before deleting**:
```bash
# Verify grid data is complete
docker exec -e PGPASSWORD=data egon-data-local-database-container \
  psql -U egon -d egon-data -c \
  "SELECT COUNT(*) FROM grid.egon_etrago_bus WHERE scn_name = 'eGon2025';"
# Should return: 14494
```

#### 2. ⚠️ **Keep Current Setup** for now
- MaStR import is in progress
- Wait until complete
- Then assess what's needed

#### 3. ✅ **Keep These Schemas**:
- `grid` (78 MB) - Your network
- `mastr` (~15 GB when complete) - Generator data
- `boundaries` (123 MB) - Geographic boundaries
- `scenario` (144 KB) - Scenario config
- `metadata` (32 KB) - Database metadata

#### 4. ⚠️ **Optional Delete**:
- `osmtgmod_results` (14 MB) - Intermediate data, can delete
- `openstreetmap` schema (empty currently)

---

### Future Considerations:

#### If You Need to Re-Extract Grid from OSM:
- You can always re-run eGon-data pipeline
- OSM data is publicly available
- But unlikely you'll need this

#### If You Need Demand Data:
- eGon-data has `demandregio` and `electricity_demand_timeseries` datasets
- Can re-run specific eGon pipelines
- Or use external sources

#### If You Need Weather/Renewable Profiles:
- eGon-data has ERA5 weather integration
- renewables.ninja is alternative
- Or use open-mastr EEG data for feed-in profiles

---

## Final Summary

### Your 230 GB Database Contains:

| Component | Size | Using? | Keep? | Future? |
|-----------|------|--------|-------|---------|
| OSM Raw Data | 224 GB (97%) | ❌ NO | ❌ DELETE | ❌ Unlikely |
| MaStR Data | 5.6 GB (2.4%) | ✅ YES | ✅ KEEP | ✅ Primary data |
| Grid Model | 78 MB (0.03%) | ✅ YES | ✅ KEEP | ✅ Core model |
| Boundaries | 123 MB (0.05%) | ⚠️ Useful | ✅ KEEP | ⚠️ Analysis |
| OSM Processing | 14 MB (<0.01%) | ❌ NO | ⚠️ Optional | ❌ No |
| Metadata | 144 KB | ✅ YES | ✅ KEEP | ✅ Config |

### After Cleanup:
- Current: **230 GB**
- After deleting OSM: **~6 GB** (when MaStR import completes: ~16 GB)
- **Space saved: 224 GB (97%)**

### What's in eGon-data Repository:
- 226 GB on disk = PostgreSQL database files
- 118 Python dataset modules
- 32 dataset categories
- You're using ~5% of its capabilities (just grid topology)
- Other 95% is for sector coupling, demand modeling, scenarios

### Bottom Line:
- ✅ Your grid model is solid (78 MB)
- ✅ MaStR data is complete and importing (will be ~15 GB)
- ❌ 97% of database is unused OSM raw data
- ⚠️ **Safe to delete OSM data → Free 224 GB**
- ⚠️ Keep eGon-data repository for reference, but you're not using most modules
