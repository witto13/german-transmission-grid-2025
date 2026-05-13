# Merit Order Simulation — Methodology

This document describes the methodology used by `scripts/simulation/merit_order_comparison.py`
to simulate annual hourly dispatch and clearing prices for an electricity market.
It is written to be **technology-** and **year-agnostic** so the same approach can
be applied to future scenarios (2030, 2040, etc.) by swapping inputs.

---

## 1. What problem the model solves

Given:

- A **fleet** of generators (RES + thermal) with capacity, fuel, and commissioning year
- An **hourly demand profile** (8760 hours)
- **Hourly per-technology availability profiles** for variable RES (solar, wind, hydro)
- **Hourly cross-border prices** for each interconnected neighbor
- **Per-border NTC caps** (import/export limits)
- **Fuel and CO₂ prices** (monthly resolution acceptable)

It produces:

- An **hourly dispatch** for each fuel category (TWh totals + 8760-hour vector)
- An **hourly clearing price** (EUR/MWh)
- **Cross-border flows** (imports/exports per border per hour)
- **Pumped-storage SOC trajectory**

The model is a **rolling-horizon mixed-integer linear program (MILP)** over 48-hour
windows with 24-hour stride, solved with CBC. Each window minimizes total dispatch
cost subject to power balance, unit-commitment, ramping, storage SOC, and trade caps.

---

## 2. Input layers

| Layer | Source examples | What it provides |
|---|---|---|
| Generator fleet | MaStR, BNetzA Kraftwerksliste, EU JRC PPDB | unit-level capacity, fuel, COD, location |
| Hourly demand | SMARD load, ENTSO-E TP | grid load (MW per hour) |
| RES profiles | SMARD generation / Open-Power-System-Data / regional reanalysis | per-MW availability per technology |
| Cross-border prices | Energy-Charts, ENTSO-E TP day-ahead | hourly EUR/MWh per neighbor |
| NTCs | TSO publications, JAO, p75 of physical CBPF flows | MW caps per border |
| Fuel / CO₂ | EEX, ICE TTF, BAFA, EUA | monthly EUR/MWhth, EUR/tCO₂ |
| Validation set | Energy-Charts public_power, SMARD price | per-fuel TWh, hourly clearing price |

For future-year runs, replace each layer's inputs while keeping the methodology
unchanged. Capacity additions, RES expansion, fuel-price scenarios, and NTC
upgrades all flow through these layers.

---

## 3. Marginal cost (SRMC) construction

For thermal plants:

```
SRMC[plant, month] = fuel_price[fuel, month] / efficiency[plant]
                   + co2_intensity[fuel] * co2_price[month] / efficiency[plant]
                   + variable_O&M[fuel]
```

- `efficiency[plant]` is a function of commissioning year (newer ⇒ higher η).
  We rank a plant within its fuel cohort by COD, then map rank-percentile to an
  η-band (e.g. for CCGT: 0.50…0.62). This **diversifies SRMC within a fuel
  group**, which is essential for a realistic merit order.
- For RES we set SRMC ≈ 0 (or slightly negative for FIT-eligible legacy units to
  reflect curtailment-loss pricing in the day-ahead market).
- For pumped storage we let the MILP choose dispatch; the implicit SRMC is the
  charging price ÷ round-trip efficiency.

Imports/exports use **time-varying neighbor prices** as their marginal cost.

---

## 4. Renewable dispatch model

RES output for hour *h* is:

```
output[h] = capacity * profile[h] * AVAILABILITY_FACTOR
```

Where `AVAILABILITY_FACTOR` is a per-technology multiplier that **bundles three
conceptually distinct effects** into one parameter:

1. **Vintage**: snapshots of the fleet are typically taken at a single date,
   while annual production reflects the fleet at year-average. If the snapshot
   is post-year (e.g. Jan-2026 capacity for a 2025 simulation), ½ of that year's
   capacity additions are double-counted.
2. **Maintenance / forced outages** (a few percent for solar/wind).
3. **Profile bias residual** — discrepancies between the reanalysis/SMARD
   profile and actual realized production.

`profile[h]` is normalized so `mean(profile) ≈ realized_capacity_factor`.
Profiles already include curtailment if they come from realized SMARD data; do
not double-count it via an additional curtailment factor.

For the future: if you expand RES capacity, **keep the AVAILABILITY_FACTOR
fixed** (it represents fleet-wide losses, not vintage-specific). Update only
`capacity`. If profiles come from ERA5/MERRA reanalysis (no curtailment baked
in), apply a separate curtailment factor based on grid-curtailment expectations.

---

## 5. Storage and dispatch

### Pumped storage / batteries

Modeled as a single aggregated unit with:
- `p_gen` (MW max discharge)
- `p_charge` (MW max charge)
- `e_max` (MWh reservoir capacity)
- `eta_one_way = sqrt(eta_round_trip)` so that `eta_charge × eta_discharge = eta_round_trip`

SOC dynamics:
```
soc[t] = soc[t-1] - gen[t] / eta_one_way + charge[t] * eta_one_way
```

The MILP picks discharge/charge to minimize total cost, which automatically
yields arbitrage during peak/trough hours.

State (SOC) is **passed between rolling-horizon windows** so multi-day
arbitrage is preserved. The 48h window is sufficient for daily-cycle storage
(reservoir ≈ 4–8 hours of discharge); longer-cycle storage (P2G, seasonal)
requires a longer window or a separate seasonal optimization layer.

### CHP must-run

Heat-driven CHP capacity is partially "must-run": it generates regardless of
electricity price because the heat is needed. We model:

```
must_run[h] = sum_units( capacity * heat_p_min_pu[h] * MUST_RUN_SCALE )
flex[h]     = sum_units( capacity * (p_max_pu[h] - p_min_pu[h]) )
```

`MUST_RUN_SCALE` captures the fraction of CHP capacity that's actually
heat-constrained. Typical values 0.25–0.50 — the rest is industrial or
flexible CHP that bids economically.

`heat_p_min_pu[h]` is a seasonal profile (high in winter, low in summer)
driven by heating-degree-days.

### Unit commitment

For lignite, coal, CCGT, oil, and other big thermal units we use UC variables
(commit, startup, shutdown) with:
- `min_up_time` (hours plant must run after startup)
- `min_down_time` (hours offline after shutdown)
- `startup_cost` (EUR/MW for a cold/warm start)
- `noload_cost` (EUR/h while online)
- `ramp_rate` (MW/h)
- `p_min` (technical minimum, fraction of `p_nom`)

These rigidities matter most for **lignite** (long min-up of ~1 week, high
p_min) and least for **gas peakers** (1-hour min-up, near-zero p_min).

---

## 6. Cross-border trade

For each border *b* and hour *t*:

```
import[b,t] in [0, NTC_IMPORT[b]]   with marginal cost = neighbor_price[b,t]
export[b,t] in [0, NTC_EXPORT[b]]   with revenue       = neighbor_price[b,t]
```

Plus a global **aggregate export cap** (sum across borders ≤ AGG_EXPORT_CAP)
to prevent the MILP from "exporting profit" by over-generating cheap thermal.
Calibrate this to ~p99 of observed peak exports.

NTC values come from p75 of observed CBPF flows in the calibration year. For
future scenarios, scale NTCs based on planned grid expansion (e.g. ENTSO-E
TYNDP).

---

## 7. Power balance

For each hour *t*:

```
sum_thermal(p[t])          // thermal dispatch
+ sum_borders(import[b,t]) // imports
+ chp_must_run[t]           // mandatory CHP
+ chp_flex[t]               // flexible CHP
+ res_total[t]              // RES (already capped by p_max_pu × availability)
+ ps_gen[t]                 // pumped storage discharge
- curtail[t]                // RES curtailment slack (free disposal)
==
demand[t]
+ sum_borders(export[b,t])
+ ps_charge[t]
```

The dual variable of this constraint is the **clearing price** (EUR/MWh) for
hour *t*.

---

## 8. Clearing-price markup

Raw LP duals tend to under-state real-world prices because they reflect only
SRMC, ignoring ancillary services, scarcity rents, strategic bidding, and risk
premia. We apply a **dynamic scarcity markup** keyed to residual load
percentiles (over the year):

| residual load tier | markup |
|---|---|
| > p95 (very tight) | 1.50× |
| p90–p95 | 1.35× |
| p75–p90 | 1.25× |
| p50–p75 | 1.18× |
| > 0 (low) | 1.10× |
| ≤ 0 (RES surplus) | 1.05× |

The markup applies to the **clearing price for display only** — it does NOT
affect the MILP's dispatch decisions, which are based on physical SRMC.

Calibrate the tiers so the model's annual mean clearing price matches the
calibration year's day-ahead spot mean within ±10%.

For RES-surplus hours where dispatch is curtailed and the LP dual is near zero,
we override to **negative prices** scaled by the size of the RES surplus
(pricing in FIT loss avoidance).

---

## 9. Calibration philosophy: behind-the-meter (BTM) corrections

Public-data benchmarks (Energy-Charts `public_power`, SMARD) typically only
cover **grid-connected metered generation**. Behind-the-meter generation —
rooftop PV self-consumption, industrial CHP not feeding the grid, distributed
biogas — does not appear in those datasets.

The model, by contrast, **balances total demand**, so it implicitly produces
both grid-fed and BTM electricity.

To compare apples-to-apples we add a per-fuel **BTM correction** to the
benchmark side, with each correction sourced from independent industry
statistics (BDEW, Fraunhofer ISE, AGEB):

```
real_adjusted[fuel] = benchmark[fuel] + BTM_CORRECTION[fuel]
```

Document each BTM correction with:
- The underlying industry statistic that justifies it
- The order of magnitude (typically a few to tens of TWh per fuel)
- A footnote in the output report

For future scenarios, BTM shares evolve: rooftop PV grows faster than utility
PV, industrial gas-CHP changes with electrification. Update BTM corrections
along with the generator fleet.

---

## 10. Calibration workflow

### Step 1 — Get the wholesale-RES carriers right

For each variable-RES technology (solar, onshore wind, offshore wind):
1. Pull realized annual TWh from the benchmark.
2. Add the BTM correction → real_adjusted.
3. Adjust `AVAILABILITY_FACTOR` so model dispatch ≈ real_adjusted within ±5%.

Wind has no BTM (all metered) — its factor is purely vintage + maintenance.
Solar's factor stays close to vintage-only because BTM goes on the benchmark side.

### Step 2 — Set storage and hydro

- Pumped-storage capacity: take BNetzA / TSO fleet number including any
  electrically coupled foreign plants.
- Hydro CF: derive from realized annual TWh ÷ (capacity × hours).

### Step 3 — Tune thermal availability

For each thermal carrier, build a 12-month `SEASONAL_AVAIL[carrier]` profile:
- Baseline: ENTSO-E ERAA / VGB outage statistics.
- Apply a **fleet-multiplier** (×0.6…×0.95) to reflect:
  - Security-reserve units (BNetzA Sicherheitsbereitschaft / Netzreserve) that
    do not bid into the day-ahead market but still appear in MaStR.
  - Mothballed / phase-out plants with very low realized utilization.

Iterate this multiplier until each thermal carrier's annual TWh is within
±10% of the BTM-adjusted benchmark.

### Step 4 — Tune CHP must-run

`CHP_MUST_RUN_SCALE` typically lands in 0.25–0.50. Lower it until model gas
dispatch matches BTM-adjusted benchmark.

### Step 5 — Tune trade caps

- `NTC_IMPORT` × scaling factor (commonly 0.85–0.90 of raw p75) to bring annual
  import volume to within ±10% of CBPF observations.
- `AGG_EXPORT_CAP`: ≈ p95–p99 of observed total export across all borders.

### Step 6 — Tune scarcity markup

Adjust the six markup tiers as a group (multiply all by a constant) until
annual mean clearing price is within ±10% of day-ahead spot mean.

### Step 7 — Verify all metrics

A "passing" calibration has every reported metric (per-fuel TWh, mean price,
imports, exports) within ±10% of the benchmark.

If two metrics conflict (tightening one breaks another), use the **biomass /
RES CF nudge**: a small bump of 0.01–0.02 in `BIOMASS_CF` displaces ~1 TWh of
the marginal thermal/import combination without disturbing prices.

---

## 11. Validation outputs

The HTML report (`results/merit_order_comparison_<YEAR>.html`) contains:

- Time-series overlay of model vs benchmark hourly clearing price
- Per-fuel annual TWh comparison bar chart
- Monthly clearing-price overlay
- Hour-of-day correlation
- Price percentile comparison
- Daily-resolution price MAE table
- Statistical metrics: Pearson, Spearman, MAE, RMSE, bias, AC1
- Negative-price hour count (model vs benchmark)

A successful run shows correlation ≥ 0.6 hour-by-hour, mean price within ±10%,
all per-fuel TWh within ±10% (BTM-adjusted), and a reasonable count of
negative-price hours (300–800/year for a high-RES European grid).

---

## 12. Adapting for future-year scenarios

To run a future-year scenario (e.g. 2030 or 2040):

1. **Replace generator fleet** with the planned fleet for that year (sources:
   national NDP, TYNDP, ministry capacity targets).
2. **Replace demand profile** with projected hourly load (climate-corrected
   reanalysis × electrification growth × demographic).
3. **Replace RES profiles** with reanalysis-based hourly profiles for the
   target year's fleet (regional weighting if rooftop fleet changes mix).
4. **Replace fuel and CO₂ prices** with scenario assumptions (e.g. WEO,
   EUA-trajectory studies).
5. **Replace neighbor prices** with results from a compatible run of each
   neighbor's model, or with TYNDP-aligned scenario prices.
6. **Update NTCs** based on TYNDP grid expansion timelines.
7. **Re-derive BTM corrections** consistent with the scenario's BTM share
   trajectory (rooftop PV growth, electrification, distributed CHP retirement).
8. **Hold AVAILABILITY_FACTORs constant** unless there's a structural reason to
   change (e.g. dramatic curtailment scenario).
9. **Re-calibrate scarcity markup** if the demand-supply tightness profile
   differs from the calibration year (more RES surplus hours → softer markup).

The methodology itself does not depend on a specific year — only the inputs
do. A calibrated baseline year provides the trusted parameter values
(AVAILABILITY_FACTORs, MUST_RUN_SCALE, scarcity tiers) that carry forward.

---

## 13. Known limitations

- **Single-bus / copper-plate model**: no transmission constraints inside the
  country. Real congestion drives ~5–10 TWh of redispatch in DE — invisible to
  this model. Add a network-aware redispatch step if needed.
- **No reactive power / voltage constraints**: dispatch only, no ancillaries.
- **48-hour MILP windows**: limits arbitrage of long-cycle storage (seasonal
  H₂, multi-day batteries). Extend window or add a seasonal layer for those.
- **Aggregated thermal clusters**: individual unit dynamics lost; OK for
  aggregate dispatch but bad for unit-level analysis.
- **BTM corrections are estimates**: the largest source of calibration
  uncertainty. Sensitivity-test ±20% on each correction to gauge robustness.
- **Neighbor prices are exogenous**: real coupled markets have feedback. For
  multi-country self-consistent runs, iterate prices between country models.

---

## 14. Files in the implementation

| File | Purpose |
|---|---|
| `scripts/simulation/merit_order_comparison.py` | Main simulator + HTML report |
| `data/processed/conventional_cod.csv` | MaStR commissioning dates → efficiency rank |
| `results/.energy_charts_cache_<YEAR>.json` | Cached benchmark data |
| `results/.cbpf_<YEAR>.json` | Cached cross-border physical-flow data |
| `results/.neighbor_prices_<YEAR>.json` | Cached neighbor day-ahead prices |
| `results/merit_order_comparison_<YEAR>.html` | Output report |

The simulator reads its fleet from the database (currently `grid_beta` scenario)
plus profile timeseries; for future-year runs, populate a new scenario
(`grid_<future_year>`) and pass `--scenario` to the simulator.
