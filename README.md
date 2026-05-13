# German Transmission Grid Model 2025

A PyPSA-compatible power flow model of the German high-voltage transmission grid
(110 / 220 / 380 kV) for the year 2025, built from OpenStreetMap topology,
official registry data (MaStR, BNetzA), and TSO grid parameters (JAO / CORE).

The pipeline starts from the [eGon-data](https://github.com/openego/eGon-data)
OSM extraction, reduces it through several simplification stages, populates it
with 18 793 generators and 12 154 loads from official 2025 data, and produces
a calibrated 8 760 h merit-order dispatch that matches every category of real
2025 generation within ±10 %.

## Headline numbers (`grid_beta`)

| Component        | Count   | Notes                                                       |
| ---------------- | ------- | ----------------------------------------------------------- |
| Buses            | 7 723   | 110 / 220 / 380 kV                                          |
| AC lines         | 12 911  | OSM topology + JAO electrical parameters                    |
| Transformers     | 567     | Includes 19 phase-shifters                                  |
| HVDC links       | 14      | Incl. ALEGRO, NordLink, Baltic Cable, internal corridors    |
| Generators       | 18 793  | 296.5 GW installed (incl. 9.8 GW offshore wind)             |
| Storage units    | 2 444   | 12.6 GW (pumped hydro + battery)                            |
| Loads            | 12 154  | 76.2 GW peak / 448 TWh annual                               |
| Generator TS     | 10 838  | 8 760 h profiles (CHP + renewables)                         |
| Load TS          | 12 154  | 8 760 h BDEW standard load profiles                         |

Merit-order dispatch matches **all 13 validation metrics** (per-carrier energy,
mean price, imports, exports) within **±10 %** of Energy-Charts 2025 after
behind-the-meter corrections. See [`docs/merit_order_analysis.md`](docs/merit_order_analysis.md).

## Pipeline overview

```
OpenStreetMap (via eGon-data)
        |
        v
 eGon2025      v1   14 494 buses,  26 489 lines   (raw OSM extraction)
        |
        | substation simplification (spatial clustering, 110 kV / EHV)
        v
 eGon2025v2          8 925 buses,  15 516 lines
        |
        | aggressive voltage-specific clustering + degree-2 elimination
        v
 eGon2025v3          8 756 buses,  15 060 lines
        |
        | 110 kV re-clustering, JAO parameter transfer (95 % bus match)
        v
 eGon2025v4          7 687 buses,  12 911 lines,  535 trafos,   3 HVDC
        |
        | add HVDC corridors, 10 offshore wind clusters
        v
 eGon2025v5          7 704 buses,  12 911 lines,  548 trafos,  14 HVDC
        |
        | 19 phase-shifting transformers + 56 import/export bus pairs
        |   + 8 760 h timeseries
        v
 eGon2025v6          7 723 buses,  12 911 lines,  567 trafos,  14 HVDC
        |
        | MaStR generator + storage allocation (Huelk et al. 2017)
        v
 grid_alpha          + 18 793 generators (296.5 GW) + 2 444 storage
        |
        | municipality demand split (PostGIS spatial join + bus degree)
        |   + BDEW standard load profiles
        v
 grid_beta           + 12 154 loads (76.2 GW peak / 448 TWh)
        |
        | merit-order calibration (SRMC, BTM corrections, seasonal CHP)
        v
 Annual dispatch     8 760 h merit-order, all metrics within +/-10 %
```

Each step is reproducible with a single script. See **[docs/grid_pipeline_v1_v5.md](docs/grid_pipeline_v1_v5.md)**,
[`docs/grid_alpha_build.md`](docs/grid_alpha_build.md), and
[`docs/grid_beta.md`](docs/grid_beta.md) for the per-step methodology.

## Quick start

### Prerequisites

- Python 3.10 with conda
- PostgreSQL 16 + PostGIS (via Docker)
- CBC solver (`apt install coinor-cbc` or `conda install -c conda-forge coincbc`)

### Environment

```bash
conda create -n egon2025 python=3.10
conda activate egon2025
pip install pypsa==0.20.1 pandas==2.3.3 geopandas==1.1.2 \
            sqlalchemy psycopg2-binary scipy shapely folium
```

### Database

```bash
docker run -d --name egon-data-local-database-container \
  -p 59734:5432 \
  -e POSTGRES_USER=egon \
  -e POSTGRES_PASSWORD=data \
  -e POSTGRES_DB=egon-data \
  postgis/postgis:16-3
```

A filtered database dump containing all scenarios (v1 – grid_beta) is published
as a **GitHub Release** asset. Restore with:

```bash
gunzip -c german_grid_2025.sql.gz | \
  docker exec -i egon-data-local-database-container \
    psql -U egon -d egon-data
```

### Load a scenario in Python

```python
from sqlalchemy import create_engine
import pandas as pd

engine = create_engine('postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data')

scn = 'grid_beta'  # or eGon2025v6, grid_alpha, etc.
buses = pd.read_sql(
    f"SELECT * FROM grid.egon_etrago_bus WHERE scn_name = '{scn}'", engine
)
lines = pd.read_sql(
    f"SELECT * FROM grid.egon_etrago_line WHERE scn_name = '{scn}'", engine
)
```

### Run the merit-order comparison

```bash
python scripts/simulation/merit_order_comparison.py
```

Produces dispatch summary, validation metrics vs. SMARD / Energy-Charts 2025,
and prints the carrier-by-carrier breakdown.

## Repository layout

```
.
├── scripts/
│   ├── pipeline/        v1 → grid_beta build scripts (one per stage)
│   ├── reduction/       Substation merging, degree-2 elimination, JAO transfer
│   ├── simulation/      Power flow, dispatch, merit-order calibration
│   ├── tso_grid/        Independent JAO-backbone TSO grid builder
│   ├── visualization/   Comparison maps + profile dashboards
│   ├── lib/             Reusable metrics + node-mapping utilities
│   ├── utils/           Name matching + KDTree spatial matching
│   └── sql/             MaStR / substation reference SQL setup
│
├── docs/
│   ├── methodology.md                Top-level project methodology
│   ├── grid_pipeline_v1_v5.md        Detailed v1 → v5 stages
│   ├── grid_alpha_build.md           Generator + storage allocation
│   ├── grid_beta.md                  Load allocation + BDEW profiles
│   ├── merit_order_methodology.md    Calibration approach
│   ├── merit_order_analysis.md       Validation results (vs. SMARD)
│   ├── jao_matching_summary.md       JAO/CORE-TSO parameter transfer
│   ├── load_mapping.md               Demand allocation across municipalities
│   ├── mastr_*.md                    MaStR data model + completeness
│   ├── network_reduction.md          Reduction algorithms
│   ├── topology_analysis.md          eGon vs. JAO comparison
│   ├── database_import.md            DB restore + schema notes
│   └── handover/                     Stage-by-stage handover docs
│
├── app/                  FastAPI + JS grid-explorer web app
├── tests/                pytest unit tests (topology, parameters, DB)
├── data/                 Reference + processed input data (large files via Release)
└── results/              Per-version snapshots (large outputs gitignored)
```

## Pipeline stages

### Stage 1 — Substation simplification (v1 → v2)

`scripts/pipeline/reduce_network.py` — Merges OSM nodes that represent a single
physical substation. Union-Find clustering with KDTree spatial index; per-voltage
merge radii (110 kV: 200 m, 220 / 380 kV: 1 000 m). Result: **14 494 → 8 925 buses (−38 %)**.

### Stage 2 — Voltage-specific clustering + degree-2 elimination (v2 → v3)

`scripts/pipeline/reduce_network_v3.py` — Aggressive clustering per voltage level
and removal of pass-through buses on 220 / 380 kV lines. Two incident lines are
merged into one equivalent line (series r / x, bottleneck s_nom). Result: **8 925 → 8 756 buses**.

### Stage 3 — v4 build (110 kV re-clustering + JAO parameters)

`scripts/pipeline/build_v4.py --apply` (2026-03-02) — Re-clusters 110 kV at
400 m, applies JAO electrical parameters to **95 % of buses** and 71 % of lines,
fills 1 082 EHV lines with similarity-based parameters, flags 28 missing
transformers. Adds 3 HVDC links (ALEGRO, NordLink, Baltic Cable). Result: **7 687 buses, 12 911 lines, 535 trafos, 3 HVDC**.

### Stage 4 — v5 build (HVDC + offshore wind)

`scripts/pipeline/build_v5.py` — Adds 11 internal HVDC corridors and 10 offshore
wind cluster connections. Result: **7 704 buses, 548 trafos, 14 HVDC**.

### Stage 5 — v6 build (phase-shifters + imports / exports + 8 760 h)

`scripts/pipeline/build_v6.py --apply` (2026-03-04) — Adds **19 phase-shifting
transformers** on 380 kV cross-corridor lines, **56 import generators + 56
export loads** at foreign buses, and full **8 760 h timeseries**. Result: **7 723 buses, 567 trafos, 14 HVDC, 66 generators, 56 loads**.

### Stage 6 — grid_alpha (generator + storage allocation)

`scripts/pipeline/build_grid_alpha.py --apply` (2026-03-11) — Maps 18 793 MaStR
generators (296.5 GW, incl. 9.8 GW offshore wind) and 2 444 storage units
(12.6 GW) to grid buses using the **MaStR SEL → SAN join chain** with
Huelk et al. (2017) voltage allocation. Companion script
`grid_alpha_offshore_and_map.py --apply` handles offshore connections.

### Stage 7 — grid_beta (loads + BDEW profiles)

`scripts/pipeline/build_grid_beta.py --apply` (2026-03-11) — Splits demand from
12 154 municipalities to bus loads via PostGIS spatial join and bus-degree
weighting. Generates **8 760 h BDEW standard load profiles**. 376 large
industrial consumers attached directly from MaStR. Result: **12 154 loads, 76.2 GW peak, 448 TWh / year**.

### Stage 8 — Merit-order dispatch + calibration

`scripts/pipeline/build_merit_order.py --apply` and
`scripts/simulation/merit_order_comparison.py` — Sub-classifies gas into
`gas_ccgt` (18 units, 9.8 GW) and `gas_chp` (739 units, 23.4 GW); computes
SRMC from fuel costs, CO₂, and efficiency; generates CHP seasonal must-run
profiles; computes renewable p_max_pu from **SMARD 2024 regionally-scaled
profiles** (121 solar bins, 114 wind bins). All 13 validation metrics land
within ±10 % of real 2025 after behind-the-meter corrections to the benchmark
(EC `public_power` undercounts ~70 TWh of BTM CHP + rooftop PV).

## Validation (2026-04-26)

| Carrier         | Model TWh | Real TWh   | Δ %      |
| --------------- | --------: | ---------: | -------: |
| Solar           | 101.1     | ~100       | +1 %     |
| Wind onshore    | 105.3     | ~100–110   | ±5 %     |
| Wind offshore   |  26.1     | ~25        | +4 %     |
| Biomass         |  49.1     | ~50        | −2 %     |
| Hydro           |  16.7     | ~17        | <1 %     |
| Pumped storage  |   9.9     | ~10        | <1 %     |
| Gas (corrected) |  92.3     | ~88        | +5 %     |
| Hard coal       |  28.5     | ~28        | +2 %     |
| Lignite         |  72.6     | ~72        | +1 %     |
| Imports         |  49.9     |  45.6 (CBPF) | +9.4 % |
| Exports         |  59.4     |  63.9 (CBPF) | −7 %   |
| Mean price      | 97.0 €/MWh | 89.3 €/MWh | +9 %    |

See [`docs/merit_order_analysis.md`](docs/merit_order_analysis.md) for the full
breakdown and the BTM correction rationale.

## Data sources

| Source                                                                    | Content                              | License    |
| ------------------------------------------------------------------------- | ------------------------------------ | ---------- |
| [eGon-data](https://egon-data.readthedocs.io)                             | Grid topology from OpenStreetMap     | AGPL v3    |
| [MaStR](https://www.marktstammdatenregister.de)                           | German power plant + load registry   | Open Data  |
| [JAO / CORE-TSO](https://www.jao.eu)                                      | TSO grid model (electrical params)   | Public     |
| [SMARD](https://www.smard.de)                                             | 2024 generation + load profiles      | Public     |
| [Energy-Charts](https://www.energy-charts.info)                           | 2025 dispatch validation             | Public     |
| [BDEW](https://www.bdew.de)                                               | Standard load profiles (SLP)         | Public     |
| [BNetzA](https://www.bnetza.de)                                           | National totals + curtailment 2025   | Public     |
| [DemandRegio](https://opendata.ffe.de/project/demandregio)                | Regional demand baseline             | GPL v3     |
| [BKG VG250](https://gdz.bkg.bund.de)                                      | Municipality boundaries              | Open Data  |

## Database schemas

PostgreSQL + PostGIS. Schema follows eTraGo conventions; every grid component
table is filtered by `scn_name`.

| Schema       | Purpose                                                    |
| ------------ | ---------------------------------------------------------- |
| `grid`       | Buses, lines, transformers, generators, loads, storage     |
| `mastr`      | MaStR registry (31 tables, unit / location / contracts)    |
| `boundaries` | Municipalities, NUTS regions, state outlines               |
| `scenario`   | Scenario parameters and metadata                           |
| `openstreetmap`, `osmtgmod_results` | Raw + processed OSM topology      |

Available `scn_name` values (newest first):

- `grid_beta`       — generators + loads + 8 760 h profiles (final)
- `grid_alpha`      — generators + storage, no loads yet
- `eGon2025v6`      — pure grid (PSTs, imports / exports, timeseries)
- `eGon2025v5`, `v4`, `v3`, `v2`, `eGon2025` — earlier pipeline snapshots
- `eGon2025_tso`    — independent JAO-backbone TSO grid (12 093 buses)

## Reproducing the pipeline

```bash
# Stage 1–2: substation merging + degree-2 elimination
python scripts/pipeline/reduce_network.py --apply
python scripts/pipeline/reduce_network_v3.py --apply

# Stage 3–5: JAO parameters, HVDC, phase-shifters
python scripts/pipeline/build_v4.py --apply
python scripts/pipeline/build_v5.py --apply
python scripts/pipeline/build_v6.py --apply

# Stage 6: generators + storage
python scripts/pipeline/build_grid_alpha.py --apply
python scripts/pipeline/grid_alpha_offshore_and_map.py --apply

# Stage 7: loads + BDEW profiles
python scripts/pipeline/build_grid_beta.py --apply

# Stage 8: merit-order calibration + 8 760 h dispatch
python scripts/pipeline/build_merit_order.py --apply
python scripts/simulation/merit_order_comparison.py
```

Each `--apply` script supports `--dry-run` for inspection without writing
to the database.

## Known limitations

- **LOPF on full `grid_beta` is slow.** PyPSA 0.20.1's LP-build phase (~5 min
  per snapshot before CBC even starts) is the bottleneck on 18 793 generators
  × 12 911 lines. The merit-order comparison uses an aggregated model. Consider
  upgrading to PyPSA / linopy for interactive power flow on the full network.
- HiGHS CLI is not bundled — PyPSA 0.20.1 needs the CLI binary. Use CBC.
- Some dev DB credentials are hard-coded as `egon:data` — fine for local dev,
  do not deploy as-is.

## References

- Huelk, L. et al. (2017). “Allocation of annual electricity consumption and
  power generation capacities across multiple voltage levels in a high spatial
  resolution.” *Int. J. Sustainable Energy Planning and Management*, 13: 79–92.
- [eGon-data documentation](https://egon-data.readthedocs.io)
- [eTraGo documentation](https://etrago.readthedocs.io)
- [PyPSA](https://pypsa.org) — Python for Power System Analysis
- [SMARD](https://www.smard.de) — Strommarktdaten of the BNetzA

## License

The scripts and documentation in this repository are released under **AGPL v3**
to match the licensing of the upstream
[eGon-data](https://github.com/openego/eGon-data) and
[eTraGo](https://github.com/openego/eTraGo) projects from which this work
derives.
