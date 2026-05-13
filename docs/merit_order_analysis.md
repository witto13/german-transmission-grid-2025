# Merit Order & Unit Commitment Analysis — Germany 2025

## Objective

Simulate the German electricity market for all 8,760 hours of 2025 using our grid_beta generator fleet, compare against real market data (SMARD day-ahead prices + Energy-Charts generation by fuel type), and iteratively improve until the model matches reality.

## Data Sources

| Source | What | Resolution |
|--------|------|------------|
| **SMARD** (Bundesnetzagentur) | Day-ahead prices, hourly load | Hourly, 8760 pts |
| **Energy-Charts** (Fraunhofer ISE) | Generation by fuel type, cross-border trade, **neighbor zone prices** | 15-min → resampled to hourly |
| **grid_beta scenario** (our DB) | 18,792 generators, 12,210 loads, timeseries | From eGon-data pipeline |
| **SMARD 2025 profiles** | Solar/wind/offshore capacity factors | Hourly, downloaded via API |
| **MaStR** (Marktstammdatenregister) | Generator commissioning dates (COD) for efficiency ranking | Per-unit |
| **Energy-Charts CBPF** | Physical cross-border flows (NTC estimation) | 15-min → hourly |

### SMARD Data Quality Issue

SMARD filter IDs 1223 ("nuclear") and 4075 ("oil") return **bogus data** for 2025. Germany has zero nuclear since April 2023, yet filter 1223 shows 67 TWh (actually returns lignite data — avg 7664 MW matches Energy-Charts lignite exactly). Filter 4075 shows a constant 15.5 GW which matches nothing. We replaced all generation comparison data with Energy-Charts API, keeping SMARD only for prices and load.

## Evolution of the Model

### Phase 1-3: Simple Merit Order → Heuristic UC

See git history for earlier phases. Key progression: r = 0.30 → 0.45 → 0.32.

### Phase 4: MILP Unit Commitment (v1)

Rolling-horizon MILP with 48h windows, 24h stride. Static cross-border prices, flat 35% markup. **r = 0.459, MAE = 39.1 EUR/MWh.**

### Phase 5: Market Coupling Model (v2) — Current

**Key improvements over v1:**

1. **Hourly neighbor prices** — Downloaded 2025 day-ahead prices for all 11 neighbor bidding zones (AT, BE, CZ, DK1+DK2, FR, NL, NO2, PL, SE4, CH, DE-LU) from Energy-Charts API. Import costs and export revenues now vary hour-by-hour, approximating EUPHEMIA market coupling.

2. **Per-border NTC constraints** — Each border has its own import and export capacity limit derived from 99th percentile of observed physical cross-border flows (Energy-Charts CBPF data). Replaces the old flat 8 GW aggregate import cap.

3. **Seasonal fuel prices** — Monthly TTF gas (33-48 EUR/MWhth), coal (12-15), and CO2 (69-79) prices instead of flat annual averages. Thermal MCs recalculated each month within the rolling horizon.

4. **COD-based efficiency ranking** — Generator efficiency ranked by commissioning date from MaStR (5,289 conventional units with COD data) instead of p_nom proxy. Newer plants get higher η, mapped via fleet capacity distribution.

5. **Dynamic scarcity markup** — Residual-load-dependent markup replaces flat 35%. Ranges from 5% (RES surplus) to 60% (p95 scarcity). Produces realistic price spikes up to 326 EUR/MWh.

**Architecture:**

| Feature | v1 | v2 |
|---------|----|----|
| **Cross-border imports** | 15 binned clusters, static MCs (50-95 EUR/MWh) | 11 per-border variables, hourly prices from Energy-Charts |
| **Cross-border exports** | 5 fixed tranches (-4 to 75 EUR/MWh) | 11 per-border variables, hourly neighbor prices as revenue |
| **NTC constraints** | Flat 8 GW aggregate | Per-border (1-5 GW each, ~25 GW total) |
| **Fuel prices** | Static (TTF=40, coal=13.6, CO2=75) | Monthly (TTF 33-48, coal 12-15, CO2 69-79) |
| **Efficiency ranking** | p_nom descending (larger=newer) | COD-based from MaStR (5,289 units) |
| **Market premium** | Flat 35% on all positive prices | Dynamic 5-60% based on residual load percentile |

## Results

### Price Comparison

| Metric | v1 | v2 | SMARD Real |
|--------|----|----|------------|
| **Pearson r** | 0.459 | **0.674** | 1.0 |
| **Spearman ρ** | — | **0.676** | 1.0 |
| **MAE** | 39.1 | **27.6** | 0 |
| **RMSE** | — | 41.8 | 0 |
| **Average price** | 89.3 | 92.2 | 89.3 |
| **Bias** | -0.3 | +2.9 | 0 |
| **Negative hours** | 318 (3.6%) | 311 (3.6%) | 573 (6.5%) |
| **Price range** | -100 to 180 | -100 to 326 | -250 to 583 |
| **r(price, resid_load)** | 0.46 | 0.66 | 0.88 |
| **AC(1)** | 0.94 | 0.92 | 0.91 |

### Monthly Price Tracking

| Month | Model v2 | SMARD | Diff |
|-------|----------|-------|------|
| Jan | 109 | 114 | -5 |
| Feb | 113 | 129 | -15 |
| Mar | 93 | 95 | -2 |
| Apr | 74 | 78 | -4 |
| May | 70 | 67 | +3 |
| Jun | 71 | 64 | +7 |
| Jul | 90 | 88 | +2 |
| Aug | 82 | 77 | +5 |
| Sep | 87 | 84 | +4 |
| Oct | 96 | 84 | +12 |
| Nov | 121 | 102 | +19 |
| Dec | 102 | 93 | +9 |

Monthly correlation: strong seasonal pattern captured (winter high, summer low). Feb underestimate likely due to cold snap / gas price spike not fully captured in monthly TTF averages.

### Key Diagnostics

- **Price vs residual load**: Model r = 0.66, SMARD r = 0.88. Gap narrowed significantly via market coupling — neighbor prices transmit demand signals.
- **Autocorrelation (lag 1h)**: Model 0.92, SMARD 0.91. Excellent temporal price persistence.
- **Hourly neighbor prices are the #1 improvement** — they alone explain ~0.15 of the +0.21 correlation gain.
- **Winter gap**: Feb model underestimates by 15 EUR/MWh — likely missing cold snap demand response and intra-month TTF volatility.

## What Drives the Remaining Gap (r = 0.67 vs 1.0)

1. **No network constraints** — copper-plate model can't represent grid congestion, redispatch, or locational marginal pricing.
2. **Still no intra-month fuel price volatility** — TTF can swing ±10 EUR/MWh within a month; we use monthly averages.
3. **No multi-day look-ahead** — 48h rolling windows can't capture week-ahead fuel scheduling decisions.
4. **Imperfect NTC model** — real NTCs vary hourly (flow-based market coupling); we use fixed 99th-percentile caps.
5. **No demand-side response** — real market has industrial load shifting, battery storage, flexible EV charging.
6. **Dispatch volume overshoot** — copper-plate model dispatches more fossil than reality (exports surplus), but this doesn't significantly affect price accuracy.

## Fuel Price Assumptions (2025)

### Monthly Variation

| Month | TTF (EUR/MWhth) | Coal (EUR/MWhth) | CO2 (EUR/tCO2) |
|-------|-----------------|-------------------|-----------------|
| Jan | 47.5 | 14.8 | 72.0 |
| Feb | 44.8 | 14.2 | 68.5 |
| Mar | 40.2 | 13.5 | 70.0 |
| Apr | 35.1 | 12.8 | 73.5 |
| May | 32.8 | 12.2 | 75.0 |
| Jun | 33.5 | 12.5 | 76.2 |
| Jul | 35.2 | 13.0 | 78.5 |
| Aug | 36.1 | 13.2 | 77.0 |
| Sep | 38.5 | 13.8 | 75.8 |
| Oct | 42.3 | 14.5 | 74.5 |
| Nov | 45.6 | 14.8 | 73.0 |
| Dec | 48.2 | 15.2 | 71.5 |

Lignite (5 EUR/MWhth) and oil (50 EUR/MWhth) are static — mine-mouth/refinery contracts don't vary seasonally.

### Resulting Marginal Costs (January example)

| Carrier | Efficiency range | MC range (EUR/MWh) |
|---------|-----------------|-------------------|
| Lignite | 32-43% | 80-107 |
| Hard coal | 34-46% | 87-116 |
| Gas CCGT | 50-62% | 100-124 |
| Gas CHP | 38-52% | 107-151 (after 12 EUR heat credit) |
| Oil | 30-40% | 173-230 |

### NTC Estimates (MW, from 99th percentile of observed flows)

| Border | Import NTC | Export NTC |
|--------|-----------|-----------|
| AT | 3,091 | 2,270 |
| BE | 1,002 | 1,002 |
| CH | 3,205 | 2,896 |
| CZ | 2,436 | 2,644 |
| DK | 3,429 | 3,266 |
| FR | 3,972 | 4,143 |
| LU | 659 | 156 |
| NL | 3,274 | 4,773 |
| NO | 1,401 | 1,406 |
| PL | 2,296 | 2,011 |
| SE | 600 | 618 |
| **Total** | **25,365** | **25,185** |

## Files

| File | Description |
|------|-------------|
| `scripts/simulation/merit_order_comparison.py` | Main script (MILP UC + HTML report generation) |
| `results/merit_order_comparison_2025.html` | Interactive HTML comparison report |
| `results/.smard_cache_2025.json` | Cached SMARD price + load data |
| `results/.energy_charts_cache_2025.json` | Cached Energy-Charts generation data |
| `results/.cbet_cache_2025.json` | Cached cross-border trade data (12 countries) |
| `results/.neighbor_prices_2025.json` | Hourly day-ahead prices for 11 neighbor zones |
| `results/.cbpf_2025.json` | Hourly physical cross-border flows (NTC estimation) |
| `data/processed/conventional_cod.csv` | MaStR commissioning dates for conventional generators |
| `data/profiles/*_2025.csv` | SMARD 2025 RES capacity factor profiles |

## Technical Details

### MILP Formulation (v2)

**Objective**: Minimize total system cost
```
min Σ_c Σ_t [ MC_c × p_c,t + noload_c × u_c,t + startup_cost_c × v_c,t ]
    + Σ_b Σ_t [ price_b,t × p_imp_b,t ]      ← hourly neighbor prices
    - Σ_b Σ_t [ price_b,t × p_exp_b,t ]      ← hourly export revenue
```

**Constraints**:
- Power balance: Σ thermal + Σ imports + CHP + RES + PS_gen - curtail = demand + Σ exports + PS_charge
- Generator bounds: p_min × u ≤ p ≤ p_nom × u
- Startup/shutdown: v_t ≥ u_t - u_{t-1}, w_t ≥ u_{t-1} - u_t
- Min-up/min-down time constraints
- Ramp rates with startup relaxation
- Per-border import: p_imp_b,t ≤ NTC_import_b
- Per-border export: p_exp_b,t ≤ NTC_export_b
- Pumped storage SoC tracking

**Solver**: CBC via PuLP, 30s timeout, 0.5% gap, ~365 solves/year.

### Market Coupling Mechanism

The per-border import/export variables with hourly neighbor prices naturally implement market coupling:
- When German dual price > neighbor price → MILP imports (up to NTC)
- When German dual price < neighbor price → MILP exports (up to NTC)
- Price convergence occurs when NTCs are not binding

This approximates the EUPHEMIA algorithm used in real European day-ahead markets.

### Dynamic Scarcity Markup

| Residual load percentile | Markup | Rationale |
|-------------------------|--------|-----------|
| >p95 (>39 GW) | 60% | Scarcity rent + strategic bidding |
| p90-p95 (35-39 GW) | 45% | Tight supply, limited alternatives |
| p75-p90 (28-35 GW) | 35% | Moderate tightness |
| p50-p75 (20-28 GW) | 25% | Average conditions |
| 0-p50 (<20 GW) | 15% | Low residual, competitive pressure |
| <0 (RES surplus) | 5% | Minimal markup |

### Literature Context

Key references supporting this modeling approach:
- **Sensfuß et al. (2008)** — "The merit-order effect": foundational German merit order framework
- **Hirth (2013)** — "The market value of variable renewables": residual load → price formation
- **Pape et al. (2016)** — "Are fundamentals enough?": cross-border coupling explains 15-25% of DE price variance
- **ACER Market Monitoring Reports** — European electricity market coupling efficiency metrics
- Open-source dispatch models: ELMOD (DIW Berlin), DIETER (Öko-Institut)
