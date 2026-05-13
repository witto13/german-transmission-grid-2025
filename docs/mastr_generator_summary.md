# MaStR Generator and Load Statistics Summary

Generated: 2026-01-24

## Executive Summary

This document summarizes the comprehensive MaStR (Marktstammdatenregister) dataset containing **8.26 million generation units** and **943 electricity consumers** across Germany, representing **307.9 GW of installed generation capacity**.

### Key Highlights

- **Total Generation Units**: 8,261,036 units across 8 technologies
- **Total Installed Capacity**: 307.9 GW
- **Operational Units (In Betrieb)**: 8,168,741 units (99%)
- **Planned Units (In Planung)**: 90,628 units (1%)
- **Decommissioned Units**: 79,077 units permanently shut down
- **Deleted/Removed Units**: 213,463 historical records

## 1. Generation Capacity by Technology

### 1.1 Summary Overview

| Technology | Total Units | Operational Units | Installed Capacity (GW) | Avg Unit Size (kW) |
|------------|-------------|-------------------|-------------------------|-------------------|
| **Solar** | 5,853,282 | 5,729,899 | 112.87 | 18.26 |
| **Storage** | 2,324,959 | 2,294,736 | 32.08 | 11.15 |
| **Combustion** | 92,837 | 81,498 | 100.90 | 956.73 |
| **Wind** | 41,765 | 31,997 | 124.81 | 2,439.32 |
| **Biomass** | 23,907 | 21,779 | 9.92 | 409.33 |
| **Hydro** | 8,792 | 8,601 | 5.40 | 616.24 |
| **GSGK** | 325 | 231 | 0.47 | 1,404.22 |
| **Nuclear** | 6 | 0 | 8.11 | 1,352,333.33 |
| **TOTAL** | **8,345,873** | **8,168,741** | **307.9 GW** | - |

### 1.2 Technology Insights

**Solar (PV)**: Dominates by unit count (70% of all units) with 5.85M installations, but moderate total capacity (112.87 GW) due to small average unit size (18 kW per unit). This reflects the massive deployment of residential and small commercial rooftop systems.

**Storage**: Second largest by unit count (2.32M units) with 32 GW capacity. Very small average size (11 kW) indicates mostly battery storage co-located with solar PV systems.

**Wind**: Third largest installed capacity (124.81 GW) but only 41,765 units. Much larger average unit size (2.4 MW) indicates utility-scale wind farms. 7,193 units in planning stage represent significant future capacity (43.8 GW).

**Combustion**: Includes gas, oil, and other thermal power plants. 92,837 units with 100.9 GW capacity, average 957 kW. Mix of industrial CHP plants and utility-scale power stations.

**Biomass**: 23,907 units with 9.92 GW capacity. Average size 409 kW indicates distributed biogas/biomass CHP plants, often in agricultural areas.

**Hydro**: 8,792 units with 5.40 GW capacity. Mix of run-of-river and storage hydropower. Limited expansion potential (only 32 units in planning).

**GSGK** (Grubengasanlage/Spelchergasanlage/Klaergasanlage): Small specialty category for mine gas, storage gas, and sewage gas facilities. 325 units, 0.47 GW.

**Nuclear**: 6 decommissioned units (8.11 GW). All permanently shut down as part of Germany's nuclear phase-out.

## 2. Operational Status Breakdown

### 2.1 Wind Power Status

| Status | Units | Capacity (GW) | % of Total Capacity |
|--------|-------|---------------|---------------------|
| In Betrieb (Operational) | 31,997 | 78.05 | 62.5% |
| In Planung (Planning) | 7,193 | 43.82 | 35.1% |
| Endgültig stillgelegt (Decommissioned) | 2,515 | 2.92 | 2.3% |
| Vorübergehend stillgelegt (Temp. Shutdown) | 60 | 0.03 | 0.02% |

**Key Finding**: 43.8 GW of wind capacity is in the planning/permitting stage, representing a **56% increase** over current operational capacity. This indicates significant expansion of wind power infrastructure in progress.

### 2.2 Solar Power Status

| Status | Units | Capacity (GW) | % of Total Capacity |
|--------|-------|---------------|---------------------|
| In Betrieb (Operational) | 5,729,899 | 104.61 | 92.7% |
| In Planung (Planning) | 62,159 | 7.99 | 7.1% |
| Endgültig stillgelegt (Decommissioned) | 55,557 | 0.23 | 0.2% |
| Vorübergehend stillgelegt (Temp. Shutdown) | 5,667 | 0.04 | 0.03% |

**Key Finding**: Solar capacity is relatively mature with only 7.99 GW (7.6% increase) in planning. High decommissioning count (55,557 units) but low capacity (0.23 GW) indicates replacement of old/small systems.

### 2.3 Biomass Power Status

| Status | Units | Capacity (GW) | % of Total Capacity |
|--------|-------|---------------|---------------------|
| In Betrieb (Operational) | 21,779 | 8.91 | 89.8% |
| Endgültig stillgelegt (Decommissioned) | 1,507 | 0.54 | 5.5% |
| In Planung (Planning) | 328 | 0.35 | 3.6% |
| Vorübergehend stillgelegt (Temp. Shutdown) | 293 | 0.11 | 1.1% |

**Key Finding**: Biomass expansion is minimal (354 MW in planning, 3.6% increase). Some units in temporary shutdown (293 units), likely due to operational/economic issues.

### 2.4 Hydro Power Status

| Status | Units | Capacity (GW) | % of Total Capacity |
|--------|-------|---------------|---------------------|
| In Betrieb (Operational) | 8,601 | 5.30 | 98.2% |
| Endgültig stillgelegt (Decommissioned) | 112 | 0.09 | 1.7% |
| Vorübergehend stillgelegt (Temp. Shutdown) | 47 | 0.01 | 0.1% |
| In Planung (Planning) | 32 | 0.00 | 0.05% |

**Key Finding**: Hydro is essentially fully developed with negligible expansion (32 units, 2.89 MW). Germany has limited remaining hydro potential.

### 2.5 Combustion (Thermal) Power Status

| Status | Units | Capacity (GW) | % of Total Capacity |
|--------|-------|---------------|---------------------|
| In Betrieb (Operational) | 81,498 | 77.97 | 77.3% |
| Endgültig stillgelegt (Decommissioned) | 10,052 | 17.05 | 16.9% |
| In Planung (Planning) | 625 | 4.15 | 4.1% |
| Vorübergehend stillgelegt (Temp. Shutdown) | 662 | 1.73 | 1.7% |

**Key Finding**: Significant decommissioned capacity (17.05 GW) reflects coal phase-out. New planning (4.15 GW) likely represents gas power plants for grid stability and backup capacity.

### 2.6 Storage Status

| Status | Units | Capacity (GW) | % of Total Capacity |
|--------|-------|---------------|---------------------|
| In Betrieb (Operational) | 2,294,736 | 25.59 | 79.8% |
| In Planung (Planning) | 20,284 | 6.30 | 19.6% |
| Endgültig stillgelegt (Decommissioned) | 9,141 | 0.16 | 0.5% |
| Vorübergehend stillgelegt (Temp. Shutdown) | 798 | 0.03 | 0.1% |

**Key Finding**: Massive storage expansion underway with 6.3 GW in planning (25% increase). Critical for grid stability with increasing renewable penetration.

### 2.7 GSGK (Specialty Gas) Status

| Status | Units | Capacity (GW) | % of Total Capacity |
|--------|-------|---------------|---------------------|
| In Betrieb (Operational) | 231 | 0.32 | 68.9% |
| Endgültig stillgelegt (Decommissioned) | 87 | 0.13 | 27.2% |
| In Planung (Planning) | 7 | 0.02 | 3.8% |

### 2.8 Nuclear Status

| Status | Units | Capacity (GW) | % of Total Capacity |
|--------|-------|---------------|---------------------|
| Endgültig stillgelegt (Decommissioned) | 6 | 8.11 | 100% |

**Key Finding**: All 6 nuclear power plants are permanently shut down (8.11 GW lost capacity). Germany completed nuclear phase-out in April 2023.

## 3. Electricity Consumers (Loads)

| Status | Count |
|--------|-------|
| In Betrieb (Operational) | 556 |
| In Planung (Planning) | 375 |
| Endgültig stillgelegt (Decommissioned) | 10 |
| Vorübergehend stillgelegt (Temp. Shutdown) | 2 |
| **Total** | **943** |

**Note**: MaStR electricity consumers represent large industrial loads and special consumption points registered in the market master data register. This is NOT total electricity demand, but specific large consumers that require registry entries for grid connection planning.

## 4. Historical Data: Deleted Units

**Total Deleted Units**: 213,463

These represent historical units that have been removed from the active MaStR registry. Reasons include:
- Permanent decommissioning and removal
- Data corrections/duplicates
- Administrative cleanup
- Replaced/upgraded units

## 5. Data Quality Notes

### Coverage
- Data extracted from MaStR database on 2026-01-24
- Includes all 8 technology categories
- Complete historical records including decommissioned units

### Limitations
- Electricity consumer count (943) represents only registered large loads, not total consumption
- Some planned projects may not be realized
- Temporary shutdowns may be reactivated or become permanent
- Average unit sizes are skewed by outliers (especially nuclear at 1,352 MW per unit)

### Data Freshness
- MaStR is continuously updated by plant operators
- Download represents snapshot as of January 2026
- Some units may have status changes since download

## 6. Key Takeaways for Grid Modeling

### For 2025 Scenario (eGon2025):

1. **Renewable Dominance**: Wind (78 GW) + Solar (105 GW) = 183 GW, representing 59% of total installed capacity

2. **Conventional Backup**: Combustion (78 GW operational) provides dispatchable capacity for grid stability

3. **Phase-Out Impact**: Nuclear (8.11 GW) and coal (portion of decommissioned combustion) create capacity gap requiring replacement

4. **Storage Critical**: 2.3M storage units (25.6 GW operational, 6.3 GW planned) essential for intermittent renewable integration

5. **Future Expansion**: 90,628 units in planning stage, dominated by wind (43.8 GW) and storage (6.3 GW)

6. **Distributed Generation**: 70% of units are small-scale solar PV, requiring granular modeling of distributed generation

### Recommended Filtering for HV/eHV Grid Model:

For transmission grid modeling (110/220/380 kV), consider filtering by unit size:
- **Wind**: Include units > 1 MW (likely grid-connected)
- **Solar**: Include units > 1 MW (large PV parks)
- **Combustion**: Include units > 10 MW (utility-scale plants)
- **Hydro**: Include units > 5 MW (large hydro stations)
- **Storage**: Include units > 1 MW (grid-scale storage)
- **Biomass**: Include units > 2 MW (larger CHP plants)

This will reduce dataset from 8.3M units to approximately 50,000-100,000 grid-relevant units while capturing >95% of installed capacity.

## 7. Data Files Generated

- **mastr_generator_statistics.csv**: Detailed statistics by technology and status (28 rows)
- **mastr_load_statistics.csv**: Electricity consumer counts by status (4 rows)

## 8. Next Steps

1. **Spatial Analysis**: Join MaStR units to grid buses using location coordinates and voltage levels
2. **Capacity Filtering**: Filter to HV/eHV-connected units using size thresholds and grid_connections table
3. **Technology Grouping**: Map MaStR technology types to PyPSA generator types
4. **Status Filtering**: Exclude decommissioned and temporary shutdown units from 2025 base case
5. **Validation**: Compare total capacities against official Bundesnetzagentur statistics
