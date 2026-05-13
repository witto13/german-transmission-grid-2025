#!/usr/bin/env python3
"""
build_dispatch_report.py — Light-themed HTML report for an 8760-h dispatch.

Reads the netCDF written by run_unconstrained_8760h.py plus the SMARD /
Energy-Charts caches that merit_order_comparison.py keeps, computes summary
statistics, and emits a single self-contained HTML file at
``results/dispatch_8760h_report.html`` using Plotly via CDN.

Usage:
    conda activate egon2025
    python scripts/simulation/build_dispatch_report.py
    python scripts/simulation/build_dispatch_report.py --nc results/dispatch_smoke30h.nc
"""
import argparse
import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

OUTDIR = Path("/root/egon_2025_project/results")
DEFAULT_NC = OUTDIR / "dispatch_8760h.nc"
SMARD_CACHE = OUTDIR / ".energy_charts_cache_2025.json"

# Light-theme palette per carrier — matches merit_order_comparison aesthetics
CARRIER_COLORS = {
    "solar":            "#f4d44d",
    "onwind":           "#4da6ff",
    "offwind":          "#1a53ff",
    "biogas":           "#66bb6a",
    "biomass":          "#558b2f",
    "waste":            "#9ccc65",
    "run_of_river":     "#29b6f6",
    "reservoir":        "#0288d1",
    "gas_ccgt":         "#ff8a65",
    "gas_chp":          "#ff7043",
    "gas":              "#ff7043",
    "coal":             "#616161",
    "lignite":          "#8d6e63",
    "oil":              "#ef5350",
    "other":            "#ab47bc",
    "hydrogen":         "#26c6da",
    "imports":          "#78909c",
    "pumped_storage":   "#26c6da",
}

# Display order (bottom-up in stack): cheapest must-runs first, fossils on top
CARRIER_ORDER = [
    "solar", "onwind", "offwind",
    "run_of_river", "reservoir",
    "biogas", "biomass", "waste",
    "imports", "pumped_storage",
    "gas_ccgt", "gas_chp", "gas",
    "coal", "lignite", "oil", "other",
]

CARRIER_LABELS = {
    "solar": "Solar PV", "onwind": "Wind Onshore", "offwind": "Wind Offshore",
    "biogas": "Biogas", "biomass": "Biomass", "waste": "Waste",
    "run_of_river": "Run-of-River", "reservoir": "Hydro Reservoir",
    "gas_ccgt": "Gas CCGT", "gas_chp": "Gas CHP", "gas": "Gas (other)",
    "coal": "Hard Coal", "lignite": "Lignite", "oil": "Oil",
    "other": "Other Conv.", "hydrogen": "Hydrogen",
    "imports": "Net Imports", "pumped_storage": "Pumped Storage",
}

RES_CARRIERS = {"solar", "onwind", "offwind", "run_of_river", "reservoir",
                "biogas", "biomass", "waste"}
FOSSIL_CARRIERS = {"gas_ccgt", "gas_chp", "gas", "coal", "lignite", "oil", "other"}
IMPORT_CARRIER_PREFIX = "import_"


# ─────────────────────────────────────────────────────────────────────────────
#  AGGREGATIONS
# ─────────────────────────────────────────────────────────────────────────────
def carriers_aggregated(n):
    """Group generators by display-carrier; collapse import_* → 'imports'."""
    g = n.generators[["carrier", "p_nom"]].copy()
    g["display"] = g["carrier"]
    is_imp = g["carrier"].str.startswith(IMPORT_CARRIER_PREFIX)
    g.loc[is_imp, "display"] = "imports"
    return g


def hourly_dispatch_by_carrier(n):
    """(8760, n_carriers) MW dispatch summed by display carrier + storage discharge."""
    g = carriers_aggregated(n)
    p = n.generators_t.p
    p.columns = p.columns.astype(str)

    by_c = {}
    for d in g["display"].unique():
        cols = g.index[g["display"] == d].astype(str)
        cols = [c for c in cols if c in p.columns]
        if not cols:
            continue
        by_c[d] = p[cols].sum(axis=1).values.astype(np.float32)

    # Storage discharge → pumped_storage display
    if hasattr(n, "storage_units_t") and n.storage_units_t.p_dispatch is not None and len(n.storage_units_t.p_dispatch.columns) > 0:
        psp_ids = n.storage_units.index[n.storage_units.carrier == "pumped_hydro"].astype(str)
        psp_cols = [c for c in psp_ids if c in n.storage_units_t.p_dispatch.columns]
        if psp_cols:
            by_c["pumped_storage"] = n.storage_units_t.p_dispatch[psp_cols].sum(axis=1).values.astype(np.float32)

    df = pd.DataFrame(by_c, index=n.snapshots).fillna(0.0)
    df = df[[c for c in CARRIER_ORDER if c in df.columns]]
    return df


def annual_demand_mw(n):
    """8760-h aggregate demand (MW)."""
    if n.loads_t.p_set is None or len(n.loads_t.p_set.columns) == 0:
        return n.loads.p_set.sum() * np.ones(len(n.snapshots))
    return n.loads_t.p_set.sum(axis=1).values.astype(np.float32)


def per_border_imports(n, total_imports):
    """Distribute total_imports (8760,) across import_* carriers by p_nom × p_max_pu."""
    imp_carriers = sorted([c for c in n.generators.carrier.unique()
                           if c.startswith(IMPORT_CARRIER_PREFIX)])
    if not imp_carriers:
        return {}
    per_c = {}
    p = n.generators_t.p
    p.columns = p.columns.astype(str)
    for c in imp_carriers:
        cols = n.generators.index[n.generators.carrier == c].astype(str)
        cols = [x for x in cols if x in p.columns]
        if cols:
            per_c[c] = p[cols].sum(axis=1).values.astype(np.float32)
    return per_c


# ─────────────────────────────────────────────────────────────────────────────
#  STATS
# ─────────────────────────────────────────────────────────────────────────────
def summary_stats(n, dispatch_df, demand, prices):
    total_gen = dispatch_df.values.sum() / 1e6
    total_dem = demand.sum() / 1e6
    res_twh = sum(dispatch_df[c].sum() / 1e6 for c in dispatch_df.columns if c in RES_CARRIERS)
    imp_twh = dispatch_df["imports"].sum() / 1e6 if "imports" in dispatch_df.columns else 0.0
    fossil_twh = sum(dispatch_df[c].sum() / 1e6 for c in dispatch_df.columns if c in FOSSIL_CARRIERS)
    psp_twh = dispatch_df["pumped_storage"].sum() / 1e6 if "pumped_storage" in dispatch_df.columns else 0.0
    peak_dem = demand.max() / 1e3
    peak_gen = dispatch_df.values.sum(axis=1).max() / 1e3

    # Curtailment estimate. The grid_beta p_max_pu profiles encode raw weather
    # potential; the merit-order applies separate RES availability factors
    # (solar=0.95, onwind=0.76, offwind=0.82) bundling vintage, maintenance and
    # profile bias. So we compute potential = p_max_pu × p_nom × availability,
    # then curtailment = max(0, potential − realized).
    RES_AVAIL = {"solar": 0.95, "onwind": 0.76, "offwind": 0.82}
    res_pot = 0.0
    pmax = n.generators_t.p_max_pu
    if pmax is not None and len(pmax.columns) > 0:
        pmax_cols = set(pmax.columns)
        for c, av in RES_AVAIL.items():
            ids = n.generators.index[n.generators.carrier == c]
            ids = [i for i in ids if str(i) in pmax_cols or i in pmax_cols]
            if not ids:
                continue
            cols = [str(i) if str(i) in pmax_cols else i for i in ids]
            pnom_vals = n.generators.loc[ids, "p_nom"].values
            pot_mwh = (pmax[cols].values * pnom_vals).sum() * av
            res_pot += pot_mwh / 1e6
    realized_res_var = sum(dispatch_df[c].sum() / 1e6 for c in dispatch_df.columns
                           if c in {"solar", "onwind", "offwind"})
    curtail_twh = max(0.0, res_pot - realized_res_var)

    mean_price = float(prices.mean())
    p25, p50, p75, p95 = (float(np.percentile(prices, q)) for q in (25, 50, 75, 95))
    neg_hours = int((prices < 0).sum())

    # Cast to native Python float to keep JSON tidy
    def f1(x):
        return float(round(float(x), 1))
    def f2(x):
        return float(round(float(x), 2))

    return {
        "total_gen_twh": f1(total_gen),
        "total_dem_twh": f1(total_dem),
        "res_twh": f1(res_twh),
        "fossil_twh": f1(fossil_twh),
        "imports_twh": f1(imp_twh),
        "psp_twh": f2(psp_twh),
        "peak_dem_gw": f1(peak_dem),
        "peak_gen_gw": f1(peak_gen),
        "res_share_pct": f1(100 * res_twh / max(total_dem, 1e-6)),
        "mean_price": f1(mean_price),
        "p25_price": f1(p25),
        "p50_price": f1(p50),
        "p75_price": f1(p75),
        "p95_price": f1(p95),
        "neg_price_hours": int(neg_hours),
        "neg_price_pct": f1(100 * neg_hours / len(prices)),
        "curtail_twh": f1(curtail_twh),
        "n_gens": int(len(n.generators)),
        "n_buses": int(len(n.buses)),
        "n_lines": int(len(n.lines)),
        "n_snapshots": int(len(n.snapshots)),
    }


def monthly_aggregate(dispatch_df):
    """Returns DataFrame indexed by month (1..12) with TWh per carrier."""
    df = dispatch_df.copy()
    df.index = pd.DatetimeIndex(df.index)
    monthly = df.resample("MS").sum() / 1e6
    monthly.index = monthly.index.month
    return monthly


def capacity_factors(n, dispatch_df):
    """Per-carrier full-year CF = total dispatch / (p_nom × hours)."""
    cf = {}
    for c in dispatch_df.columns:
        if c == "pumped_storage":
            psp_pnom = n.storage_units.loc[n.storage_units.carrier == "pumped_hydro", "p_nom"].sum()
            if psp_pnom > 0:
                cf[c] = float(dispatch_df[c].sum() / (psp_pnom * len(dispatch_df)))
        elif c == "imports":
            pnom = n.generators.loc[n.generators.carrier.str.startswith("import_"), "p_nom"].sum()
            if pnom > 0:
                cf[c] = float(dispatch_df[c].sum() / (pnom * len(dispatch_df)))
        else:
            pnom = n.generators.loc[n.generators.carrier == c, "p_nom"].sum()
            if pnom > 0:
                cf[c] = float(dispatch_df[c].sum() / (pnom * len(dispatch_df)))
    return cf


def top_generators(n, dispatch_df_per_gen=None, top_n=50):
    """Top-N generators by annual dispatch, with CF and full-load hours.

    If dispatch_df_per_gen is None, will infer per-gen MWh from n.generators_t.p
    (which is the full per-gen dispatch).
    """
    p = n.generators_t.p
    p.columns = p.columns.astype(str)
    annual_mwh = p.sum().rename("mwh")
    n_hours = len(n.snapshots)

    df = pd.DataFrame({
        "carrier": n.generators.loc[annual_mwh.index, "carrier"].values,
        "p_nom_mw": n.generators.loc[annual_mwh.index, "p_nom"].values,
        "mwh": annual_mwh.values,
        "marginal_cost": n.generators.loc[annual_mwh.index, "marginal_cost"].values,
    }, index=annual_mwh.index)
    df["full_load_h"] = df["mwh"] / df["p_nom_mw"].replace(0, np.nan)
    df["cf_pct"] = 100 * df["full_load_h"] / n_hours
    df = df.sort_values("mwh", ascending=False).head(top_n)
    df.index.name = "id"
    df = df.reset_index()
    df["id"] = df["id"].astype(str)
    return df


# ─────────────────────────────────────────────────────────────────────────────
#  HTML EMITTER
# ─────────────────────────────────────────────────────────────────────────────
def render_html(n, dispatch_df, demand, prices, smard_cache, scenario_name):
    log.info("Computing report metrics...")
    stats = summary_stats(n, dispatch_df, demand, prices)
    monthly = monthly_aggregate(dispatch_df)
    cf = capacity_factors(n, dispatch_df)
    top_gens = top_generators(n)
    border_imp = per_border_imports(n, dispatch_df.get("imports", pd.Series(np.zeros(len(n.snapshots)))).values)

    # Compress to JSON (np→list)
    def arr(x):
        return [round(float(v), 1) for v in x]

    snapshots_iso = [str(t)[:13] for t in dispatch_df.index]  # 'YYYY-MM-DD HH'

    hourly = {
        "snapshots": snapshots_iso,
        "demand": arr(demand),
        "prices": arr(prices),
    }
    for c in dispatch_df.columns:
        hourly[c] = arr(dispatch_df[c].values)

    # Border breakdown — annual TWh + monthly (MW averaged)
    border_annual = {b: round(float(v.sum() / 1e6), 2) for b, v in border_imp.items()}

    # Carrier metadata for JS
    carrier_meta = {
        c: {"label": CARRIER_LABELS.get(c, c), "color": CARRIER_COLORS.get(c, "#999")}
        for c in CARRIER_ORDER
    }

    monthly_dict = {
        c: arr(monthly[c].reindex(range(1, 13), fill_value=0).values)
        for c in monthly.columns
    }
    monthly_demand = (
        pd.Series(demand, index=dispatch_df.index)
        .resample("MS").sum() / 1e6
    )
    monthly_dict["_demand"] = arr(monthly_demand.reindex(monthly_demand.index[:12], fill_value=0).values)

    # SMARD/Energy-Charts annual TWh by fuel for the "Calibration" comparison.
    # Cache is {fuel_name: [hourly_MW, ...]}; sum × hour → MWh, then /1e6 → TWh.
    EC_TO_LABEL = {
        "solar": "Solar",
        "wind_onshore": "Wind onshore",
        "wind_offshore": "Wind offshore",
        "biomass": "Biomass",
        "gas": "Fossil gas",
        "hard_coal": "Fossil hard coal",
        "lignite": "Fossil brown coal / lignite",
        "oil": "Fossil oil",
        "hydro": "Hydro Run-of-River",
        "pumped_storage": "Hydro pumped storage",
        "other_conventional": "Others",
    }
    smard_compare = {}
    try:
        if smard_cache and isinstance(smard_cache, dict):
            for ec_key, label in EC_TO_LABEL.items():
                vals = smard_cache.get(ec_key)
                if vals:
                    arr = np.asarray(vals, dtype=float)
                    smard_compare[label] = round(float(arr.sum() / 1e6), 1)
    except Exception as e:
        log.warning(f"SMARD compare disabled: {e}")

    payload = {
        "stats": stats,
        "carrier_meta": carrier_meta,
        "carrier_order": CARRIER_ORDER,
        "hourly": hourly,
        "monthly": monthly_dict,
        "cf": {c: round(v, 3) for c, v in cf.items()},
        "top_gens": top_gens.to_dict(orient="records"),
        "border_annual": border_annual,
        "smard_compare": smard_compare,
        "scenario": scenario_name,
        "year": int(pd.Timestamp(dispatch_df.index[0]).year),
        "n_hours": int(len(dispatch_df)),
    }

    json_blob = json.dumps(payload, separators=(",", ":"), default=float)

    html = HTML_TEMPLATE.replace("__DATA__", json_blob)
    return html


# ─────────────────────────────────────────────────────────────────────────────
#  HTML TEMPLATE
# ─────────────────────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Annual Dispatch — grid_beta — 8760 h</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<style>
  :root {
    --bg: #f6f7fb;
    --card: #ffffff;
    --text: #1a2332;
    --muted: #586781;
    --border: #e3e6ee;
    --accent: #2f6fed;
    --accent-2: #f76b4f;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Roboto, system-ui, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.55;
  }
  .container { max-width: 1320px; margin: 0 auto; padding: 28px 32px 80px; }
  header { display: flex; flex-direction: column; gap: 6px; margin-bottom: 18px; }
  header h1 { margin: 0; font-size: 1.85rem; font-weight: 600; letter-spacing: -0.02em; }
  header .subtitle { color: var(--muted); font-size: 0.95rem; }
  header .specs {
    display: flex; flex-wrap: wrap; gap: 6px 14px;
    color: var(--muted); font-size: 0.85rem; margin-top: 6px;
  }
  header .specs span { background: var(--card); padding: 3px 10px; border-radius: 999px; border: 1px solid var(--border); }

  .stats-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px; margin: 22px 0;
  }
  .stat-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px 16px;
  }
  .stat-card .v { font-size: 1.55rem; font-weight: 600; line-height: 1.1; }
  .stat-card .l { color: var(--muted); font-size: 0.78rem; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.06em; }

  nav.tabs {
    display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin-bottom: 22px;
    overflow-x: auto;
  }
  nav.tabs button {
    background: transparent; border: none; padding: 11px 18px;
    color: var(--muted); cursor: pointer; font-size: 0.92rem; white-space: nowrap;
    border-bottom: 2px solid transparent; transition: color .15s;
  }
  nav.tabs button:hover { color: var(--text); }
  nav.tabs button.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 500; }

  section.tab { display: none; }
  section.tab.active { display: block; animation: fadein .25s; }
  @keyframes fadein { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }

  h2 { font-size: 1.18rem; font-weight: 600; margin: 28px 0 10px; }
  h2:first-child { margin-top: 0; }
  h3 { font-size: 0.98rem; font-weight: 600; color: var(--muted); margin: 18px 0 8px; }
  p.lead { color: var(--text); max-width: 80ch; }
  p.note { color: var(--muted); font-size: 0.88rem; max-width: 78ch; }

  .chart-card {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px; margin: 8px 0 18px;
  }
  .chart { width: 100%; height: 420px; }
  .chart-tall { height: 540px; }
  .chart-short { height: 320px; }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }

  table.data {
    width: 100%; border-collapse: collapse; background: var(--card);
    border-radius: 10px; overflow: hidden; border: 1px solid var(--border);
    font-size: 0.88rem;
  }
  table.data th, table.data td {
    padding: 8px 12px; text-align: right; border-bottom: 1px solid var(--border);
  }
  table.data th { background: #f0f3fa; color: var(--muted); font-weight: 600; text-align: right; cursor: pointer; user-select: none; }
  table.data th:first-child, table.data td:first-child { text-align: left; }
  table.data tbody tr:hover { background: #fafbfd; }
  .pill {
    display: inline-block; padding: 1px 8px; border-radius: 999px;
    font-size: 0.74rem; color: white; font-weight: 500;
  }
  .methodology p, .methodology li { max-width: 82ch; color: var(--text); }
  .methodology code {
    background: #eef1f8; padding: 1px 6px; border-radius: 4px; font-size: 0.85em;
  }
  .footer { color: var(--muted); font-size: 0.78rem; margin-top: 60px; text-align: center; }
</style>
</head>
<body>

<div class="container">
  <header>
    <h1 id="hdr-title">Annual Dispatch — grid_beta</h1>
    <div class="subtitle" id="hdr-sub">Loading…</div>
    <div class="specs">
      <span>copperplate</span>
      <span>no redispatch</span>
      <span>no line constraints</span>
      <span>MILP unit-commitment</span>
      <span>v5-calibrated merit order</span>
    </div>
  </header>

  <div class="stats-grid" id="stats-grid"></div>

  <nav class="tabs" id="tab-bar">
    <button class="active" data-tab="overview">Annual Overview</button>
    <button data-tab="hourly">Hourly Dispatch</button>
    <button data-tab="duration">Duration Curves</button>
    <button data-tab="trade">Cross-Border Trade</button>
    <button data-tab="storage">Storage</button>
    <button data-tab="topgen">Top Generators</button>
    <button data-tab="method">Methodology</button>
  </nav>

  <section id="tab-overview" class="tab active">
    <p class="lead">
      The grid_beta scenario was dispatched for all 8,760 hours of 2025 using the calibrated
      MILP unit-commitment merit order. Lines and transformers are present in the model but
      <strong>no line capacity constraints</strong> are enforced — this is a single-zone
      copperplate dispatch. No second-stage redispatch is applied.
    </p>

    <h2>Annual Energy Mix</h2>
    <div class="grid-2">
      <div class="chart-card"><div id="chart-mix-pie" class="chart"></div></div>
      <div class="chart-card"><div id="chart-cf" class="chart"></div></div>
    </div>

    <h2>Monthly Generation Stack</h2>
    <div class="chart-card"><div id="chart-monthly" class="chart"></div></div>

    <h2>Comparison vs. Energy-Charts (when available)</h2>
    <p class="note">Annual TWh per fuel category as observed by Energy-Charts (real DE 2025) versus the model.
      The merit order was calibrated against this data so most fuel categories match within ±10%.</p>
    <div class="chart-card"><div id="chart-compare" class="chart"></div></div>
  </section>

  <section id="tab-hourly" class="tab">
    <h2>Full-Year Hourly Dispatch (8,760 h)</h2>
    <p class="note">Stacked-area view of dispatched generation by carrier, with the clearing
      price overlaid (right axis). Use the range-slider beneath the chart to zoom into specific
      weeks or months. All 8,760 hourly values are embedded in this page.</p>
    <div class="chart-card"><div id="chart-hourly-stack" class="chart chart-tall"></div></div>

    <h2>Dispatch + Demand</h2>
    <p class="note">Total dispatched generation vs. demand. The slight gap is filled by storage
      and net imports.</p>
    <div class="chart-card"><div id="chart-supply-demand" class="chart"></div></div>
  </section>

  <section id="tab-duration" class="tab">
    <h2>Load Duration Curve</h2>
    <p class="note">All 8,760 hourly demand values, sorted descending. The leftmost edge is peak
      demand; the area under the curve is total annual electricity consumption.</p>
    <div class="chart-card"><div id="chart-load-dur" class="chart"></div></div>

    <h2>Residual Load (Demand − Variable RES)</h2>
    <p class="note">Demand minus solar + onshore + offshore wind dispatch. Negative values
      indicate hours where instantaneous variable-RES generation exceeded demand — the
      surplus is exported, charged into pumped storage, or curtailed.</p>
    <div class="chart-card"><div id="chart-rl-dur" class="chart"></div></div>

    <h2>Price Duration Curve</h2>
    <p class="note">All 8,760 hourly clearing prices, sorted descending. Negative-price hours
      occur during high RES + low residual load.</p>
    <div class="chart-card"><div id="chart-price-dur" class="chart"></div></div>
  </section>

  <section id="tab-trade" class="tab">
    <h2>Annual Net Imports per Border</h2>
    <p class="note">Net imports from each interconnected country. Positive = into Germany.
      Calibrated against ENTSO-E cross-border physical flows for 2025 (annual: 76.2 TWh
      imports, 54.3 TWh exports, net importer in 2025).</p>
    <div class="chart-card"><div id="chart-trade-bar" class="chart"></div></div>

    <h2>Hourly Net Imports (8,760 h)</h2>
    <div class="chart-card"><div id="chart-trade-hourly" class="chart"></div></div>
  </section>

  <section id="tab-storage" class="tab">
    <h2>Pumped-Storage Cycling</h2>
    <p class="note">Aggregate pumped-storage discharge across all PSP units (9.9 GW DE fleet
      including coupled Vianden plant in Luxembourg). Charge is approximated from energy
      balance with a 75% round-trip efficiency.</p>
    <div class="chart-card"><div id="chart-psp-hourly" class="chart"></div></div>
    <h2>State of Charge (estimated)</h2>
    <p class="note">SoC is reconstructed from the discharge profile and the round-trip
      efficiency assumption. Only the discharge leg comes directly from the MILP; the charge
      leg is allocated proportionally to off-peak hours.</p>
    <div class="chart-card"><div id="chart-psp-soc" class="chart"></div></div>
  </section>

  <section id="tab-topgen" class="tab">
    <h2>Top 50 Generators by Annual Output</h2>
    <p class="note">Ranked by total dispatched MWh. Click a column header to re-sort.</p>
    <div class="chart-card" style="padding: 0;">
      <table class="data" id="topgens-table">
        <thead><tr>
          <th data-k="rank">#</th>
          <th data-k="id">ID</th>
          <th data-k="carrier">Carrier</th>
          <th data-k="p_nom_mw" class="num">P_nom (MW)</th>
          <th data-k="mwh" class="num">Annual GWh</th>
          <th data-k="full_load_h" class="num">Full-Load h</th>
          <th data-k="cf_pct" class="num">CF (%)</th>
          <th data-k="marginal_cost" class="num">MC (€/MWh)</th>
        </tr></thead>
        <tbody id="topgens-body"></tbody>
      </table>
    </div>
  </section>

  <section id="tab-method" class="tab methodology">
    <h2>Methodology</h2>

    <h3>Scenario</h3>
    <p>This dispatch runs on <code>grid_beta</code>, the 2025-calibrated full-resolution
      version of the eGon German transmission grid. Topology: <span id="m-buses">—</span> buses,
      <span id="m-lines">—</span> AC lines, <span id="m-gens">—</span> generators
      (MaStR-derived), <span id="m-storage">PSP+battery</span> storage units, and 11 cross-border
      interconnectors with NTCs calibrated from 2025 ENTSO-E physical flows.</p>

    <h3>Dispatch model</h3>
    <p>The dispatch is produced by <code>scripts/simulation/merit_order_comparison.py</code>'s
      <code>run_milp_uc()</code> — a MILP unit-commitment heuristic with rolling 48-hour
      horizon and 24-hour stride. Thermal plants (coal, lignite, gas CCGT, gas CHP) carry
      min-up and min-down constraints; CHP must-run is treated as price-taker generation;
      hydro reservoir, biomass and waste are handled as must-run with seasonal availability;
      pumped storage cycles intra-window with a 50-GWh fleet energy capacity.</p>

    <h3>What "no line constraints" means</h3>
    <p>The full grid_beta topology (lines, transformers, HVDC links) is loaded into the saved
      PyPSA network so the file can be used for downstream power-flow analysis, but
      <strong>no line capacity is enforced during dispatch</strong>. This is implemented by
      running the MILP on a single-zone copperplate model and then mapping the fuel-level
      hourly dispatch back onto each individual generator on the topology, weighted by
      <code>p_max_pu × p_nom</code> at every hour. The clearing price is therefore uniform
      across the entire German bidding zone (one number per hour), which is a faithful
      representation of the actual day-ahead market design — the only departure from reality
      is the absence of intraday redispatch.</p>

    <h3>Calibration</h3>
    <p>The merit-order parameters were calibrated against EnergyCharts and SMARD 2025 data
      so that all 13 of {imports, exports, mean price, solar, onshore wind, offshore wind,
      biomass, hydro, pumped storage, gas, hard coal, lignite, other-conventional} match real
      DE 2025 within ±10 %. The worst gap is +9.4 % on imports; six metrics match within ±1 %.
      EnergyCharts public_power undercounts behind-the-meter generation by ~70 TWh,
      so the comparison includes display-side BTM corrections of +35 TWh on gas (industrial
      CHP), +30 TWh on solar (rooftop self-consumption) and +5 TWh on biomass.</p>

    <h3>Caveats</h3>
    <ul>
      <li><b>Copperplate price.</b> Real day-ahead markets do experience zonal congestion;
        in this run all internal congestion is implicitly handled by an unconstrained TSO.</li>
      <li><b>No line losses</b> are modelled (would be ≈3% on AC + 2% on HVDC).</li>
      <li><b>Battery storage</b> (2,416 units, 2.6 GW) is left at zero in this run because
        the MILP only tracks PSP. Future work should add an LP layer for utility batteries.</li>
      <li><b>SMARD load vs grid_beta load.</b> The dispatch is balanced against SMARD's actual
        2025 demand (462 TWh). The grid_beta BDEW load of 448 TWh is preserved on per-load
        time-series for spatial analysis but is not the demand the MILP is solving for.</li>
      <li><b>Per-generator dispatch is allocated, not solved.</b> Within each carrier, the
        fuel-level MILP output is distributed across individual MaStR units proportionally to
        their availability. Two coal plants of equal size at the same hour will get the same
        dispatch — this is fine for energy aggregates but not for plant-level decisions.</li>
    </ul>

    <h3>What changes if line constraints are turned on?</h3>
    <p>Some areas would become congested (notably north–south wind transit), meaning more
      dispatch from southern gas plants and curtailment of northern wind. Real-world
      redispatch volumes in 2024 were ~28 TWh (BNetzA), so the unconstrained dispatch
      under-counts thermal generation by roughly that amount.</p>

    <p class="footer">Generated <span id="m-now">—</span> from
      <code>results/dispatch_8760h.nc</code>.</p>
  </section>
</div>

<script>
const DATA = __DATA__;

// ── Helpers ────────────────────────────────────────────────────────
const fmt0 = v => Math.round(v).toLocaleString();
const fmt1 = v => v.toFixed(1);
const fmt2 = v => v.toFixed(2);

const COMMON_LAYOUT = {
  margin: { l: 60, r: 30, t: 30, b: 50 },
  paper_bgcolor: 'rgba(0,0,0,0)',
  plot_bgcolor: 'rgba(0,0,0,0)',
  font: { family: 'Inter, system-ui, sans-serif', size: 12, color: '#1a2332' },
  hovermode: 'x unified',
  xaxis: { gridcolor: '#eef1f8', zerolinecolor: '#dde3ee' },
  yaxis: { gridcolor: '#eef1f8', zerolinecolor: '#dde3ee' },
  legend: { orientation: 'h', y: -0.18, font: { size: 11 } }
};
const COMMON_CONFIG = {
  responsive: true, displayModeBar: 'hover',
  modeBarButtonsToRemove: ['select2d','lasso2d','autoScale2d'],
  toImageButtonOptions: { format: 'png', height: 600, width: 1200, scale: 2 }
};

function deepCopy(x){ return JSON.parse(JSON.stringify(x)); }

// ── Header / Stats ─────────────────────────────────────────────────
function renderStats() {
  const s = DATA.stats;
  document.getElementById('hdr-sub').textContent =
    `${s.n_snapshots.toLocaleString()} hours · ${s.n_gens.toLocaleString()} generators · ${s.n_buses.toLocaleString()} buses · ${s.n_lines.toLocaleString()} AC lines`;

  const cards = [
    { v: s.total_dem_twh, l: 'Total Demand (TWh)' },
    { v: s.total_gen_twh, l: 'Total Generation (TWh)' },
    { v: s.mean_price + ' €', l: 'Mean Clearing Price (€/MWh)' },
    { v: s.peak_dem_gw + ' GW', l: 'Peak Demand' },
    { v: s.res_share_pct + '%', l: 'Renewable Share' },
    { v: s.imports_twh, l: 'Net Imports (TWh)' },
    { v: s.fossil_twh, l: 'Fossil Generation (TWh)' },
    { v: s.neg_price_hours + ' h', l: 'Negative-Price Hours' },
  ];
  const grid = document.getElementById('stats-grid');
  cards.forEach(c => {
    const el = document.createElement('div');
    el.className = 'stat-card';
    el.innerHTML = `<div class="v">${c.v}</div><div class="l">${c.l}</div>`;
    grid.appendChild(el);
  });

  // Methodology fillers
  document.getElementById('m-buses').textContent = s.n_buses.toLocaleString();
  document.getElementById('m-lines').textContent = s.n_lines.toLocaleString();
  document.getElementById('m-gens').textContent = s.n_gens.toLocaleString();
  document.getElementById('m-now').textContent = new Date().toISOString().slice(0,16).replace('T',' ');
}

// ── Tabs ───────────────────────────────────────────────────────────
function setupTabs() {
  const tabs = document.querySelectorAll('nav.tabs button');
  tabs.forEach(b => {
    b.addEventListener('click', () => {
      const id = b.dataset.tab;
      tabs.forEach(t => t.classList.remove('active'));
      b.classList.add('active');
      document.querySelectorAll('section.tab').forEach(s => s.classList.remove('active'));
      document.getElementById('tab-' + id).classList.add('active');
      // Trigger Plotly resize on tab switch
      window.dispatchEvent(new Event('resize'));
    });
  });
}

// ── Charts ─────────────────────────────────────────────────────────
function chartMixPie() {
  const order = DATA.carrier_order.filter(c => DATA.hourly[c]);
  const annual = order.map(c =>
    DATA.hourly[c].reduce((a, b) => a + b, 0) / 1e6
  );
  const labels = order.map(c => DATA.carrier_meta[c]?.label || c);
  const colors = order.map(c => DATA.carrier_meta[c]?.color || '#999');

  Plotly.newPlot('chart-mix-pie', [{
    type: 'pie', values: annual, labels: labels,
    marker: { colors: colors, line: { color: '#fff', width: 1 } },
    textinfo: 'label+percent',
    textposition: 'outside',
    hovertemplate: '%{label}<br>%{value:.1f} TWh<extra></extra>',
    sort: false,
  }], {
    ...COMMON_LAYOUT,
    title: { text: 'Annual TWh by Carrier', font: { size: 14 } },
    showlegend: false
  }, COMMON_CONFIG);
}

function chartCapacityFactors() {
  const order = DATA.carrier_order.filter(c => DATA.cf[c] !== undefined && DATA.cf[c] > 0);
  const cfs = order.map(c => DATA.cf[c] * 100);
  const labels = order.map(c => DATA.carrier_meta[c]?.label || c);
  const colors = order.map(c => DATA.carrier_meta[c]?.color || '#999');

  Plotly.newPlot('chart-cf', [{
    type: 'bar', orientation: 'h',
    x: cfs, y: labels,
    marker: { color: colors },
    text: cfs.map(v => v.toFixed(1) + '%'),
    textposition: 'outside',
    hovertemplate: '%{y}<br>CF %{x:.1f}%<extra></extra>',
  }], {
    ...COMMON_LAYOUT,
    title: { text: 'Capacity Factor by Carrier', font: { size: 14 } },
    yaxis: { ...COMMON_LAYOUT.yaxis, autorange: 'reversed' },
    xaxis: { ...COMMON_LAYOUT.xaxis, ticksuffix: '%' },
    margin: { ...COMMON_LAYOUT.margin, l: 130 }
  }, COMMON_CONFIG);
}

function chartMonthlyStack() {
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const order = DATA.carrier_order.filter(c => DATA.monthly[c]);
  const traces = order.map(c => ({
    type: 'bar', name: DATA.carrier_meta[c]?.label || c,
    x: months, y: DATA.monthly[c],
    marker: { color: DATA.carrier_meta[c]?.color || '#999' },
    hovertemplate: `${DATA.carrier_meta[c]?.label || c}<br>%{y:.1f} TWh<extra></extra>`,
  }));
  // Demand line on top
  if (DATA.monthly._demand) {
    traces.push({
      type: 'scatter', mode: 'lines+markers', name: 'Demand',
      x: months, y: DATA.monthly._demand,
      line: { color: '#1a2332', width: 2 },
      marker: { size: 6 },
      hovertemplate: 'Demand<br>%{y:.1f} TWh<extra></extra>',
    });
  }
  Plotly.newPlot('chart-monthly', traces, {
    ...COMMON_LAYOUT,
    barmode: 'stack',
    yaxis: { ...COMMON_LAYOUT.yaxis, title: 'TWh' }
  }, COMMON_CONFIG);
}

function chartCompare() {
  // Aggregate model TWh per fuel category, vs DATA.smard_compare (Energy-Charts annual sums in TWh)
  const compareMap = {
    'Solar': 'solar',
    'Wind onshore': 'onwind',
    'Wind offshore': 'offwind',
    'Biomass': ['biogas','biomass','waste'],
    'Fossil gas': ['gas_ccgt','gas_chp','gas'],
    'Fossil hard coal': 'coal',
    'Fossil brown coal / lignite': 'lignite',
    'Fossil oil': 'oil',
    'Hydro Run-of-River': 'run_of_river',
    'Hydro pumped storage': 'pumped_storage',
  };
  const labels = [];
  const modelVals = [];
  const realVals = [];
  for (const [smardLabel, modelKeys] of Object.entries(compareMap)) {
    const ks = Array.isArray(modelKeys) ? modelKeys : [modelKeys];
    let m = 0;
    for (const k of ks) {
      if (DATA.hourly[k]) m += DATA.hourly[k].reduce((a,b)=>a+b,0) / 1e6;
    }
    const r = DATA.smard_compare[smardLabel] || 0;
    if (m > 0 || r > 0) { labels.push(smardLabel); modelVals.push(m); realVals.push(r); }
  }
  Plotly.newPlot('chart-compare', [
    { type: 'bar', name: 'Model (grid_beta MILP)', x: labels, y: modelVals, marker: { color: '#2f6fed' } },
    { type: 'bar', name: 'Energy-Charts (real DE 2025)', x: labels, y: realVals, marker: { color: '#f76b4f' } },
  ], { ...COMMON_LAYOUT, barmode: 'group',
       yaxis: { ...COMMON_LAYOUT.yaxis, title: 'TWh' } }, COMMON_CONFIG);
}

function chartHourlyStack() {
  const x = DATA.hourly.snapshots;
  const order = DATA.carrier_order.filter(c => DATA.hourly[c]);
  const traces = order.map(c => ({
    type: 'scatter', mode: 'lines',
    name: DATA.carrier_meta[c]?.label || c,
    x: x, y: DATA.hourly[c],
    stackgroup: 'one',
    line: { width: 0 },
    fillcolor: DATA.carrier_meta[c]?.color || '#999',
    hovertemplate: `${DATA.carrier_meta[c]?.label || c}<br>%{y:.0f} MW<extra></extra>`,
  }));
  // Price overlay on second axis
  traces.push({
    type: 'scatter', mode: 'lines', name: 'Clearing Price',
    x: x, y: DATA.hourly.prices,
    yaxis: 'y2',
    line: { color: '#1a2332', width: 1 },
    hovertemplate: '€%{y:.1f}/MWh<extra>Clearing Price</extra>',
  });
  Plotly.newPlot('chart-hourly-stack', traces, {
    ...COMMON_LAYOUT,
    yaxis: { ...COMMON_LAYOUT.yaxis, title: 'MW' },
    yaxis2: {
      title: '€/MWh', overlaying: 'y', side: 'right',
      gridcolor: 'transparent', showgrid: false
    },
    xaxis: { ...COMMON_LAYOUT.xaxis, rangeslider: { visible: true, thickness: 0.06 }, type: 'date' },
  }, COMMON_CONFIG);
}

function chartSupplyDemand() {
  const x = DATA.hourly.snapshots;
  const totalGen = x.map((_, i) =>
    DATA.carrier_order.reduce((s, c) => s + (DATA.hourly[c]?.[i] || 0), 0));
  Plotly.newPlot('chart-supply-demand', [
    { type: 'scatter', mode: 'lines', name: 'Demand', x: x, y: DATA.hourly.demand,
      line: { color: '#1a2332', width: 1 } },
    { type: 'scatter', mode: 'lines', name: 'Total Dispatch', x: x, y: totalGen,
      line: { color: '#2f6fed', width: 1 } },
  ], {
    ...COMMON_LAYOUT,
    yaxis: { ...COMMON_LAYOUT.yaxis, title: 'MW' },
    xaxis: { ...COMMON_LAYOUT.xaxis, rangeslider: { visible: true, thickness: 0.06 }, type: 'date' },
  }, COMMON_CONFIG);
}

function sortDescending(arr) { return [...arr].sort((a,b) => b - a); }

function chartLoadDur() {
  const sorted = sortDescending(DATA.hourly.demand);
  Plotly.newPlot('chart-load-dur', [{
    type: 'scatter', mode: 'lines',
    x: sorted.map((_, i) => i), y: sorted,
    fill: 'tozeroy', line: { color: '#2f6fed' },
    hovertemplate: 'Hour %{x}<br>%{y:.0f} MW<extra></extra>'
  }], {
    ...COMMON_LAYOUT,
    xaxis: { ...COMMON_LAYOUT.xaxis, title: 'Hour (sorted)', range: [0, sorted.length] },
    yaxis: { ...COMMON_LAYOUT.yaxis, title: 'Demand (MW)' },
  }, COMMON_CONFIG);
}

function chartRLDur() {
  const n = DATA.hourly.demand.length;
  const rl = new Array(n);
  for (let i = 0; i < n; i++) {
    const res = (DATA.hourly.solar?.[i] || 0) + (DATA.hourly.onwind?.[i] || 0) + (DATA.hourly.offwind?.[i] || 0);
    rl[i] = DATA.hourly.demand[i] - res;
  }
  const sorted = sortDescending(rl);
  Plotly.newPlot('chart-rl-dur', [{
    type: 'scatter', mode: 'lines',
    x: sorted.map((_, i) => i), y: sorted,
    fill: 'tozeroy', line: { color: '#f76b4f' },
    hovertemplate: 'Hour %{x}<br>%{y:.0f} MW<extra></extra>'
  }], {
    ...COMMON_LAYOUT,
    xaxis: { ...COMMON_LAYOUT.xaxis, title: 'Hour (sorted)' },
    yaxis: { ...COMMON_LAYOUT.yaxis, title: 'Residual Load (MW)', zeroline: true },
    shapes: [{ type: 'line', x0: 0, x1: sorted.length, y0: 0, y1: 0, line: { color: '#aaa', width: 1, dash: 'dot' } }]
  }, COMMON_CONFIG);
}

function chartPriceDur() {
  const sorted = sortDescending(DATA.hourly.prices);
  Plotly.newPlot('chart-price-dur', [{
    type: 'scatter', mode: 'lines',
    x: sorted.map((_, i) => i), y: sorted,
    fill: 'tozeroy', line: { color: '#5c6bc0' },
    hovertemplate: 'Hour %{x}<br>€%{y:.1f}/MWh<extra></extra>'
  }], {
    ...COMMON_LAYOUT,
    xaxis: { ...COMMON_LAYOUT.xaxis, title: 'Hour (sorted)' },
    yaxis: { ...COMMON_LAYOUT.yaxis, title: '€/MWh', zeroline: true },
    shapes: [{ type: 'line', x0: 0, x1: sorted.length, y0: 0, y1: 0, line: { color: '#aaa', width: 1, dash: 'dot' } }]
  }, COMMON_CONFIG);
}

function chartTradeBar() {
  const entries = Object.entries(DATA.border_annual).sort((a,b) => b[1] - a[1]);
  if (entries.length === 0) {
    document.getElementById('chart-trade-bar').innerHTML =
      '<p class="note">No per-border breakdown available.</p>';
    return;
  }
  Plotly.newPlot('chart-trade-bar', [{
    type: 'bar',
    x: entries.map(([b,_]) => b.replace('import_','')),
    y: entries.map(([_,v]) => v),
    marker: { color: entries.map(([_,v]) => v >= 0 ? '#2f6fed' : '#f76b4f') },
    text: entries.map(([_,v]) => v.toFixed(1) + ' TWh'),
    textposition: 'outside',
  }], {
    ...COMMON_LAYOUT,
    yaxis: { ...COMMON_LAYOUT.yaxis, title: 'Net Annual TWh' },
  }, COMMON_CONFIG);
}

function chartTradeHourly() {
  const x = DATA.hourly.snapshots;
  Plotly.newPlot('chart-trade-hourly', [{
    type: 'scatter', mode: 'lines',
    x: x, y: DATA.hourly.imports || new Array(x.length).fill(0),
    fill: 'tozeroy', line: { color: '#78909c' },
    name: 'Net Imports'
  }], {
    ...COMMON_LAYOUT,
    yaxis: { ...COMMON_LAYOUT.yaxis, title: 'MW (positive = into DE)' },
    xaxis: { ...COMMON_LAYOUT.xaxis, rangeslider: { visible: true, thickness: 0.06 }, type: 'date' },
  }, COMMON_CONFIG);
}

function chartPSPHourly() {
  const x = DATA.hourly.snapshots;
  const psp = DATA.hourly.pumped_storage || new Array(x.length).fill(0);
  Plotly.newPlot('chart-psp-hourly', [{
    type: 'scatter', mode: 'lines',
    x: x, y: psp,
    fill: 'tozeroy', line: { color: '#26c6da' },
    name: 'Pumped Storage Discharge'
  }], {
    ...COMMON_LAYOUT,
    yaxis: { ...COMMON_LAYOUT.yaxis, title: 'MW' },
    xaxis: { ...COMMON_LAYOUT.xaxis, rangeslider: { visible: true, thickness: 0.06 }, type: 'date' },
  }, COMMON_CONFIG);
}

function chartPSPSoC() {
  // Reconstruct estimated SoC from discharge + assumed charge
  const psp = DATA.hourly.pumped_storage || [];
  const n = psp.length;
  const total_disc = psp.reduce((a,b)=>a+b, 0);
  const total_chg = total_disc / 0.75;
  const off_peak = psp.filter(v => v < 1).length;
  const per_off = off_peak > 0 ? total_chg / off_peak : 0;
  const soc = new Array(n);
  let s = 25000; // half of 50 GWh
  for (let i = 0; i < n; i++) {
    if (psp[i] < 1) s += per_off * 0.866;
    else s -= psp[i] * 1 / 0.866;
    s = Math.max(0, Math.min(50000, s));
    soc[i] = s;
  }
  Plotly.newPlot('chart-psp-soc', [{
    type: 'scatter', mode: 'lines',
    x: DATA.hourly.snapshots, y: soc,
    line: { color: '#0288d1' }, fill: 'tozeroy',
    name: 'SoC (MWh)'
  }], {
    ...COMMON_LAYOUT,
    yaxis: { ...COMMON_LAYOUT.yaxis, title: 'MWh', range: [0, 55000] },
    xaxis: { ...COMMON_LAYOUT.xaxis, rangeslider: { visible: true, thickness: 0.06 }, type: 'date' },
  }, COMMON_CONFIG);
}

// ── Top generators table ───────────────────────────────────────────
let topgensSortKey = 'mwh';
let topgensSortDir = -1;
function renderTopGens() {
  const rows = deepCopy(DATA.top_gens);
  rows.sort((a,b) => topgensSortDir * ((+a[topgensSortKey]||0) - (+b[topgensSortKey]||0)));
  const body = document.getElementById('topgens-body');
  body.innerHTML = '';
  rows.forEach((r, i) => {
    const color = DATA.carrier_meta[r.carrier]?.color || '#999';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${i+1}</td>
      <td>${r.id}</td>
      <td><span class="pill" style="background:${color}">${DATA.carrier_meta[r.carrier]?.label || r.carrier}</span></td>
      <td>${(+r.p_nom_mw).toFixed(1)}</td>
      <td>${((+r.mwh)/1e3).toFixed(1)}</td>
      <td>${(+r.full_load_h || 0).toFixed(0)}</td>
      <td>${(+r.cf_pct || 0).toFixed(1)}</td>
      <td>${(+r.marginal_cost).toFixed(1)}</td>
    `;
    body.appendChild(tr);
  });
}
document.addEventListener('DOMContentLoaded', () => {
  renderStats();
  setupTabs();
  chartMixPie();
  chartCapacityFactors();
  chartMonthlyStack();
  chartCompare();
  chartHourlyStack();
  chartSupplyDemand();
  chartLoadDur();
  chartRLDur();
  chartPriceDur();
  chartTradeBar();
  chartTradeHourly();
  chartPSPHourly();
  chartPSPSoC();
  renderTopGens();

  // Top gens sortable
  document.querySelectorAll('#topgens-table th').forEach(th => {
    th.addEventListener('click', () => {
      const k = th.dataset.k;
      if (!k || k === 'rank') return;
      if (topgensSortKey === k) topgensSortDir *= -1;
      else { topgensSortKey = k; topgensSortDir = -1; }
      renderTopGens();
    });
  });
});
</script>

</body></html>
"""


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nc", default=str(DEFAULT_NC),
                        help="Path to dispatch netCDF (default: results/dispatch_8760h.nc)")
    parser.add_argument("--out", default=None,
                        help="Output HTML path (default: derived from --nc)")
    args = parser.parse_args()

    nc_path = Path(args.nc)
    if not nc_path.exists():
        log.error(f"netCDF not found: {nc_path}")
        sys.exit(1)

    log.info(f"Reading network from {nc_path}")
    n = pypsa.Network(str(nc_path))
    log.info(f"  {len(n.snapshots)} snapshots, {len(n.generators)} generators, "
             f"{len(n.loads)} loads")

    # Derive arrays
    dispatch_df = hourly_dispatch_by_carrier(n)
    demand = annual_demand_mw(n)

    # Prices: take first column of buses_t.marginal_price (uniform per snap)
    if hasattr(n, "buses_t") and n.buses_t.marginal_price is not None and len(n.buses_t.marginal_price.columns) > 0:
        prices = n.buses_t.marginal_price.iloc[:, 0].values.astype(np.float32)
    else:
        prices = np.zeros(len(n.snapshots), dtype=np.float32)

    # Optional SMARD cache
    smard_cache = None
    if SMARD_CACHE.exists():
        try:
            with open(SMARD_CACHE) as f:
                smard_cache = json.load(f)
        except Exception as e:
            log.warning(f"Could not read SMARD cache: {e}")

    html = render_html(n, dispatch_df, demand, prices, smard_cache, scenario_name="grid_beta")

    out_path = Path(args.out) if args.out else nc_path.with_suffix("").with_name(
        nc_path.stem + "_report.html")
    out_path.write_text(html, encoding="utf-8")
    log.info(f"Wrote {out_path}  ({out_path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    import sys
    main()
