# German Transmission Grid Model 2025

A PyPSA-compatible power flow model of the German high-voltage transmission grid (110/220/380 kV) for the year 2025, built from OpenStreetMap topology and official registry data.

## Key Numbers

| Component | Count |
|-----------|-------|
| Buses (v3) | 8,753 |
| Lines (v3) | 15,060 |
| Transformers | 535 |
| Generators | 24,972 (270 GW installed) |
| Loads | 7,256 (76 GW peak, 448 TWh/yr) |

## Pipeline Overview

```
OpenStreetMap (via eGon-data)
        |
        v
 eGon2025 (v1) ---- 14,494 buses, 26,489 lines
        |
        | simplify_substations.py (spatial clustering)
        v
 eGon2025v2 ------- 8,925 buses, 15,516 lines
        |
        | simplify_degree2.py (waypoint elimination)
        v
 eGon2025v3 ------- 8,753 buses, 15,060 lines
        |
        | generator_mapping.py (MaStR -> buses)
        | load_mapping.py (448 TWh -> buses)
        v
 Complete model ---- ready for power flow
        |
        | run_powerflow.py (LOPF, CBC solver)
        v
 Results ----------- dispatch, line loading, maps
```

## Quick Start

### Prerequisites

- Python 3.10 with conda
- PostgreSQL 16 + PostGIS (via Docker)
- CBC solver

### Environment Setup

```bash
# Create conda environment
conda create -n egon2025 python=3.10
conda activate egon2025
pip install pypsa==0.20.1 pandas geopandas sqlalchemy psycopg2-binary scipy shapely folium

# Start database (restore from release dump)
docker run -d --name egon-data-local-database-container \
  -p 59734:5432 \
  -e POSTGRES_USER=egon \
  -e POSTGRES_PASSWORD=data \
  -e POSTGRES_DB=egon-data \
  postgis/postgis:16-3

# Import database dump (download from GitHub Releases)
tar xzf german_grid_v3_database.tar.gz
# See db_dump/README.md for import instructions
```

### Database Connection

```python
from sqlalchemy import create_engine
engine = create_engine('postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data')
```

## Project Structure

```
german-grid-model-2025/
|
|-- scripts/                    # Core pipeline scripts
|   |-- simplify_substations.py # v1 -> v2: spatial clustering (Union-Find + KDTree)
|   |-- simplify_degree2.py     # v2 -> v3: degree-2 waypoint elimination
|   |-- generator_mapping.py    # MaStR power plants -> grid buses
|   |-- load_mapping.py         # Municipality demand -> grid buses
|   |-- jao_matching.py         # JAO/CORE-TSO parameter transfer
|   |-- apply_jao_params.py     # Apply JAO electrical parameters to grid
|   |-- add_parallel_circuits.py     # Add missing parallel circuits
|   |-- compare_parallel_circuits.py # JAO vs eGon circuit comparison
|   |-- run_powerflow.py        # Linear optimal power flow (LOPF)
|   |-- analyze_grid_completeness.py # Grid coverage analysis
|   |-- analyze_orphans.py      # Orphan bus/line detection
|   |-- create_grid_map.py      # Interactive grid topology map
|   |-- create_powerflow_map.py # Power flow results visualization
|   |-- create_simplification_map.py # Simplification comparison map
|   |-- create_version_comparison.py # Version side-by-side comparison
|   |-- create_combined_map.py  # Combined grid + power flow map
|   |-- lib/                    # Reusable libraries
|   |   |-- metrics.py          #   Grid statistics capture
|   |   |-- node_mapping.py     #   Bus remapping utilities
|   |-- utils/                  # Utility modules
|   |   |-- name_matching.py    #   German substation name matching
|   |   |-- spatial_matching.py #   KDTree nearest-neighbor matching
|   |-- sql/                    # SQL helper scripts
|       |-- 01_create_mastr_location_hierarchy.sql
|       |-- 02_create_substation_reference.sql
|       |-- 03_create_plz_to_bus_mapping.sql
|       |-- 04_update_generator_matches.sql
|
|-- reduce_network.py           # v1 -> v2 (alternative clustering approach)
|-- reduce_network_v3.py        # v2 -> v3 (voltage-specific clustering)
|-- demand_heatmap.py           # Municipality demand heatmap generator
|-- analyze_mastr_data.py       # MaStR technology breakdown
|-- analyze_topology_differences.py  # eGon vs JAO comparison
|
|-- data/
|   |-- jao/                    # JAO/CORE-TSO reference data
|   |   |-- jao_grid_model.xlsx #   Official TSO grid model
|   |   |-- german_lines.csv    #   Extracted German lines
|   |   |-- german_substations.csv  # Extracted substations
|   |   |-- jao_substations_geocoded.csv  # Geocoded substations
|   |   |-- our_buses_380_220.csv   # Reference bus list
|   |-- jao_core_tso/           # Processed JAO data for parameter transfer
|   |   |-- buses.csv           #   588 German substations
|   |   |-- lines.csv           #   Line parameters
|   |   |-- transformers.csv    #   Transformer parameters
|   |   |-- georef.csv          #   Georeference (OSM IDs + coords)
|   |-- mastr/                  # MaStR power plant data (HV-filtered)
|   |   |-- solar_2025_hv.csv   #   HV-connected solar (336 KB)
|   |-- processed/              # Processed intermediates
|
|-- docs/                       # Technical documentation
|   |-- PROJECT_METHODOLOGY.md  #   Complete methodology (30 KB)
|   |-- MASTR_DATABASE_LINKAGES.md  # MaStR data model (25 KB)
|   |-- jao_matching_summary.md #   JAO parameter transfer process
|   |-- load_mapping.md         #   Demand allocation methodology
|
|-- documentation/              # Setup and import documentation
|   |-- EGON_DATABASE_ANALYSIS.md
|   |-- MASTR_DATA_COMPLETENESS_ANALYSIS.md
|   |-- MASTR_DOWNLOAD_COMPLETE.md
|   |-- MASTR_GENERATOR_SUMMARY.md
|   |-- MASTR_POSTGRESQL_IMPORT_COMPLETE.md
|   |-- topology_analysis.md
|
|-- handing_over/               # Handover documentation
|   |-- substation_simplification_17.2.26.md  # v1->v2 algorithm docs
|
|-- tests/                      # Test suite (pytest)
|   |-- test_simplify_substations.py  # Substation merging tests
|   |-- test_simplify_degree2.py      # Degree-2 elimination tests
|   |-- test_topology.py              # Graph topology tests
|   |-- test_electrical_params.py     # Electrical parameter tests
|   |-- test_database_connection.py   # DB connectivity tests
|
|-- results/                    # Analysis outputs
|   |-- simplification/         #   v1->v2 reduction outputs
|   |-- degree2_elimination/    #   v2->v3 reduction outputs
|   |-- jao_matching/           #   JAO matching reports
|   |-- jao_params/             #   Parameter transfer logs
|   |-- powerflow_april15.nc    #   Power flow results (netCDF)
|   |-- dispatch_april15.csv    #   Hourly generation dispatch
|   |-- line_loading_april15.csv #  Per-line loading statistics
|
|-- buses.csv ... buses_v3.csv  # Network snapshots per version
|-- lines.csv ... lines_v3.csv
|-- transformers.csv ... transformers_v3.csv
|-- reduction_info.json         # v1->v2 merge metadata
|-- reduction_info_v3.json      # v2->v3 merge metadata
|-- demand_by_municipality_2025.csv  # 11,135 municipalities
|-- demand_by_nuts3_2025.csv    # 401 NUTS-3 regions
```

## Pipeline Stages

### Stage 1: Substation Simplification (v1 -> v2)

**Script:** `scripts/simplify_substations.py`

Merges multiple OSM nodes that represent a single physical substation into one representative bus. Uses Union-Find clustering with KDTree spatial indexing, with voltage-specific merge radii (110 kV: 200m, 220-380 kV: 1000m). Protected nodes (substations, transformer buses) are never merged with each other.

**Result:** 14,494 -> 8,925 buses (-38.4%)

### Stage 2: Degree-2 Elimination (v2 -> v3)

**Script:** `scripts/simplify_degree2.py`

Removes pass-through buses on 220/380 kV lines that connect exactly two lines in series. These waypoint nodes add no branching or switching capability. Their two incident lines are merged into a single equivalent line with aggregated impedance (series r/x, bottleneck s_nom).

**Result:** 8,925 -> 8,753 buses (-1.9%), with significantly fewer redundant line segments

### Stage 3: Generator Mapping

**Script:** `scripts/generator_mapping.py`

Maps 24,972 power plants from the MaStR registry to grid buses using KDTree nearest-neighbor matching. Plants are classified by carrier (solar, onwind, gas, etc.) and assigned to voltage levels using Hulk et al. (2017) capacity thresholds (>120 MW -> 380 kV, >20 MW -> 220 kV, else 110 kV).

**Installed capacity:** 270 GW (solar 105 GW, onwind 68 GW, gas 34 GW, coal 14 GW, lignite 14 GW, ...)

### Stage 4: Load Mapping

**Script:** `scripts/load_mapping.py`

Distributes 448 TWh annual demand from 11,135 German municipalities to 7,256 load points. Demand is split by sector (households 134 TWh, CTS 124 TWh, industry 190 TWh) and assigned to voltage levels using the same Hulk et al. thresholds.

**Peak load:** 76.2 GW (consistent with historical German system peak)

### Stage 5: Power Flow

**Script:** `scripts/run_powerflow.py`

Runs linear optimal power flow (LOPF) with CBC solver for a typical April day (24 snapshots). Uses synthetic capacity factor profiles for solar (bell curve, peak 0.40), wind (diurnal, avg 0.25), and a standard German weekday load shape.

**Result:** Merit-order dispatch works correctly. At noon, renewables displace all fossil generation. System cost: 12.8 EUR/MWh average.

## Data Sources

| Source | Content | License |
|--------|---------|---------|
| [eGon-data](https://egon-data.readthedocs.io) | Grid topology from OSM | AGPL v3 |
| [MaStR](https://www.marktstammdatenregister.de) | German power plant registry | Open Data |
| [JAO/CORE-TSO](https://www.jao.eu) | TSO grid model (electrical parameters) | Public |
| [DemandRegio](https://opendata.ffe.de/project/demandregio) | Regional electricity demand | GPL v3 |
| [BKG VG250](https://gdz.bkg.bund.de) | Municipality boundaries | Open Data |
| [BDEW/BNetzA](https://www.smard.de) | National demand totals 2025 | Public |

## Database

The PostgreSQL database (14 GB) contains all grid components, MaStR registry data, and geographic boundaries. A filtered dump of the v1-v3 grid scenarios (104 MB) is available as a [GitHub Release](../../releases) asset.

**Schemas:**
- `grid` -- Network components (buses, lines, transformers, generators, loads)
- `mastr` -- MaStR power plant registry (31 tables)
- `boundaries` -- Geographic boundaries (municipalities, NUTS regions)
- `scenario` -- Scenario metadata

All grid tables use `scn_name` for scenario filtering:
- `eGon2025` -- Raw OSM extraction (14,494 buses)
- `eGon2025v2` -- After substation simplification (8,925 buses)
- `eGon2025v3` -- After degree-2 elimination (8,753 buses)

## References

- Hulk, L. et al. (2017). "Allocation of annual electricity consumption and power generation capacities across multiple voltage levels in a high spatial resolution." *Int. J. Sustainable Energy Planning and Management*, 13:79-92.
- [eGon-data documentation](https://egon-data.readthedocs.io)
- [eTraGo documentation](https://etrago.readthedocs.io)
- [PyPSA](https://pypsa.org) -- Python for Power System Analysis

## License

This project builds on [eGon-data](https://github.com/openego/eGon-data) (AGPL v3) and [eTraGo](https://github.com/openego/eTraGo) (AGPL v3). The scripts and documentation in this repository are provided for research purposes.
