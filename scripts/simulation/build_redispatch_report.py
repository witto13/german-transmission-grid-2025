#!/usr/bin/env python3
"""build_redispatch_report.py — light-themed HTML report for §13a Redispatch 2.0.

Reads:
    results/redispatch_8760h_deltas_hourly.npz   (per-hour Δp + DA & post line flows)
    results/redispatch_8760h_summary.json        (annual KPIs)
    results/dispatch_8760h_pf.nc                 (bus coords, gen carriers, line topology)

Writes:
    results/redispatch_8760h_report.html         (~5 MB self-contained)

Sections:
    1. Headline KPIs
    2. Before vs After: max-loading timeline + loading histogram
    3. Redispatch volumes by carrier (stacked bar, TWh signed)
    4. Redispatch volumes by TSO area (4 zones)
    5. Top 30 lines driving redispatch (table + flow plots)
    6. Methodology

Usage:
    conda activate egon2025
    python scripts/simulation/build_redispatch_report.py
"""
import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa
from sqlalchemy import create_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

DEFAULT_NPZ = Path("/root/egon_2025_project/results/redispatch_8760h_deltas_hourly.npz")
DEFAULT_SUMMARY = Path("/root/egon_2025_project/results/redispatch_8760h_summary.json")
DEFAULT_BASE_NC = Path("/root/egon_2025_project/results/dispatch_8760h_pf.nc")
DEFAULT_OUT = Path("/root/egon_2025_project/results/redispatch_8760h_report.html")
DB_URL = "postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data"

BUNDESLAND_TO_TSO = {
    "Berlin": "50Hertz", "Brandenburg": "50Hertz", "Mecklenburg-Vorpommern": "50Hertz",
    "Sachsen": "50Hertz", "Sachsen-Anhalt": "50Hertz", "Thüringen": "50Hertz",
    "Hamburg": "50Hertz/TenneT",
    "Bayern": "TenneT", "Niedersachsen": "TenneT", "Bremen": "TenneT",
    "Schleswig-Holstein": "TenneT",
    "Hessen": "TenneT/Amprion",
    "Nordrhein-Westfalen": "Amprion", "Rheinland-Pfalz": "Amprion", "Saarland": "Amprion",
    "Baden-Württemberg": "TransnetBW",
}

RES_CARRIERS = {"solar", "onwind", "offwind", "run_of_river", "reservoir",
                "biogas", "biomass", "waste"}
KWK_CARRIERS = {"gas_chp"}


def carrier_pretty(c: str) -> str:
    return {
        "solar": "Solar PV", "onwind": "Wind onshore", "offwind": "Wind offshore",
        "biogas": "Biogas", "biomass": "Biomass", "waste": "Waste",
        "run_of_river": "Run-of-river", "reservoir": "Hydro reservoir",
        "gas_chp": "Gas CHP", "gas_ccgt": "Gas CCGT",
        "coal": "Hard coal", "hard_coal": "Hard coal",
        "lignite": "Lignite", "oil": "Oil",
        "other": "Other", "other_conventional": "Other (conv.)",
        "hydrogen": "Hydrogen",
    }.get(c, c if not c.startswith("import_") else f"Import {c[7:]}")


def carrier_color(c: str) -> str:
    return {
        "solar": "#f6c83c", "onwind": "#a3d4f7", "offwind": "#2b6cb0",
        "biogas": "#7ed957", "biomass": "#4b8a3a", "waste": "#9e7e3a",
        "run_of_river": "#71b8d9", "reservoir": "#5a8db4",
        "gas_chp": "#e8915f", "gas_ccgt": "#d35d24",
        "coal": "#444", "hard_coal": "#444", "lignite": "#7a4e2f",
        "oil": "#222", "other": "#9aa1a8", "other_conventional": "#9aa1a8",
        "hydrogen": "#19c2c2",
    }.get(c, "#888")


def load_bus_regions(bus_ids):
    """Spatial-join buses to Bundesland + Kreis via PostGIS."""
    log.info(f"Loading bus regions for {len(bus_ids)} buses...")
    engine = create_engine(DB_URL)
    sql = """
        WITH b AS (
            SELECT bus_id::text AS bus_id, geom, country
            FROM grid.egon_etrago_bus
            WHERE scn_name = 'grid_beta'
        )
        SELECT
            b.bus_id, b.country,
            l.gen AS state, k.gen AS kreis
        FROM b
        LEFT JOIN boundaries.vg250_lan l
               ON ST_Contains(l.geometry, b.geom) AND l.gf = 4
        LEFT JOIN boundaries.vg250_krs k
               ON ST_Contains(k.geometry, b.geom)
    """
    df = pd.read_sql(sql, engine).drop_duplicates(subset=["bus_id"]).set_index("bus_id")
    df["state"] = df["state"].fillna("")
    df["kreis"] = df["kreis"].fillna("")
    df["country"] = df["country"].fillna("")
    df["tso"] = df["state"].map(BUNDESLAND_TO_TSO).fillna("")
    log.info(f"  Buses with TSO: {(df['tso']!='').sum()}/{len(df)}")
    return df


def build_payload(npz_path: Path, summary_path: Path, base_nc_path: Path, top_n: int = 30):
    log.info(f"Loading {npz_path}")
    z = np.load(npz_path, allow_pickle=True)

    snaps_iso = [s.split(".")[0] for s in z["snapshots"]]   # strip microseconds
    gen_ids = z["gen_ids"]
    gen_carrier = z["gen_carrier"]
    sub_line_ids = z["sub_line_ids"]
    delta_gen = z["delta_gen"]            # (T, G)
    line_flow_da = z["line_flow_da"]      # (T, L_sub)
    line_flow_post = z["line_flow_post"]  # (T, L_sub)
    s_nom = z["s_nom"]                    # (L_sub,)
    cost_per_h = z["cost"]
    n_binding_per_h = z["n_binding"]

    T = len(snaps_iso)
    log.info(f"  T={T}, G={len(gen_ids)}, L_sub={len(sub_line_ids)}")

    log.info(f"Loading bus/line topology from {base_nc_path}")
    n = pypsa.Network(base_nc_path)
    if T < len(n.snapshots):
        n.set_snapshots(n.snapshots[:T])

    # Per-line loading pre/post
    s_safe = np.where(s_nom > 0, s_nom, np.nan)
    abs_da = np.abs(line_flow_da)
    abs_post = np.abs(line_flow_post)
    loading_da = abs_da / s_safe[None, :] * 100.0
    loading_post = abs_post / s_safe[None, :] * 100.0
    loading_da = np.nan_to_num(loading_da, nan=0.0)
    loading_post = np.nan_to_num(loading_post, nan=0.0)

    # Per-line summary
    max_da = loading_da.max(axis=0)
    max_post = loading_post.max(axis=0)
    overload_h_da = (loading_da > 100).sum(axis=0)
    overload_h_post = (loading_post > 100).sum(axis=0)
    # Total energy ramped through redispatch on each line (sum |Δflow| × Δt=1h)
    relief_mwh = np.abs(line_flow_post - line_flow_da).sum(axis=0)

    # Bus-level mapping (v_nom is a bus property — bring it onto the line via bus0)
    bus_vnom = n.buses["v_nom"].astype(int)
    sub_line_ids_list = list(sub_line_ids)
    line_attrs = n.lines.loc[sub_line_ids_list, ["bus0", "bus1", "s_nom", "length"]].copy()
    line_attrs["v_nom"] = line_attrs["bus0"].map(bus_vnom).fillna(0).astype(int)
    bus_ids = list(n.buses.index)
    bus_x = n.buses["x"].to_dict()
    bus_y = n.buses["y"].to_dict()

    # Regions
    bus_regions = load_bus_regions(bus_ids)

    # Generator → carrier → tso mapping
    gen_bus = n.generators.loc[gen_ids, "bus"].astype(str).values
    gen_tso = pd.Series(gen_bus).map(bus_regions["tso"]).fillna("Unknown").values

    # ---- Top N lines by relief ----
    order = np.argsort(relief_mwh)[::-1]
    top_idx = order[:top_n]
    top_ids = [str(sub_line_ids[i]) for i in top_idx]
    top_hourly = {}
    for i in top_idx:
        lid = str(sub_line_ids[i])
        top_hourly[lid] = {
            "da": [round(float(v), 1) for v in line_flow_da[:, i]],
            "post": [round(float(v), 1) for v in line_flow_post[:, i]],
        }

    # Geo
    line_geo = []
    sub_set = set(map(str, sub_line_ids))
    for lid in sub_line_ids:
        lid_s = str(lid)
        try:
            row = line_attrs.loc[lid]
            b0, b1 = str(row["bus0"]), str(row["bus1"])
        except KeyError:
            continue
        if b0 not in bus_x or b1 not in bus_x:
            continue
        x0, y0 = bus_x[b0], bus_y[b0]
        x1, y1 = bus_x[b1], bus_y[b1]
        if not (np.isfinite(x0) and np.isfinite(x1) and np.isfinite(y0) and np.isfinite(y1)):
            continue
        i = list(sub_line_ids).index(lid)
        line_geo.append({
            "id": lid_s,
            "v": int(row["v_nom"]),
            "x0": round(float(x0), 4), "y0": round(float(y0), 4),
            "x1": round(float(x1), 4), "y1": round(float(y1), 4),
            "max_da": round(float(max_da[i]), 1),
            "max_post": round(float(max_post[i]), 1),
            "s_nom": round(float(row["s_nom"]), 0),
        })
    log.info(f"  Geo lines: {len(line_geo)}")

    # ---- System-wide timelines ----
    sys_max_da = loading_da.max(axis=1)
    sys_max_post = loading_post.max(axis=1)
    n_over_da = (loading_da > 100).sum(axis=1)
    n_over_post = (loading_post > 100).sum(axis=1)

    # ---- Volumes by carrier ----
    dp_up = np.maximum(delta_gen, 0).sum(axis=0)   # (G,) annual MWh
    dp_dn = -np.minimum(delta_gen, 0).sum(axis=0)
    df_gen = pd.DataFrame({
        "carrier": gen_carrier,
        "tso": gen_tso,
        "up": dp_up,
        "dn": dp_dn,
    })
    by_carrier = (df_gen.groupby("carrier")
                  .agg(up=("up", "sum"), dn=("dn", "sum"))
                  .sort_values("up", ascending=False))
    by_carrier["net"] = by_carrier["up"] - by_carrier["dn"]
    by_carrier_payload = []
    for c, r in by_carrier.iterrows():
        by_carrier_payload.append({
            "carrier": c,
            "label": carrier_pretty(c),
            "color": carrier_color(c),
            "up_TWh": round(float(r["up"]) / 1e6, 3),
            "dn_TWh": round(float(r["dn"]) / 1e6, 3),
            "net_TWh": round(float(r["net"]) / 1e6, 3),
        })

    # ---- Volumes by TSO ----
    df_gen["tso4"] = df_gen["tso"].apply(
        lambda s: s.split("/")[0] if s else "Unknown"
    )
    by_tso = (df_gen.groupby(["tso4", "carrier"])
              .agg(up=("up", "sum"), dn=("dn", "sum"))
              .reset_index())
    by_tso_payload = []
    for _, r in by_tso.iterrows():
        by_tso_payload.append({
            "tso": r["tso4"],
            "carrier": r["carrier"],
            "label": carrier_pretty(r["carrier"]),
            "color": carrier_color(r["carrier"]),
            "up_TWh": round(float(r["up"]) / 1e6, 3),
            "dn_TWh": round(float(r["dn"]) / 1e6, 3),
        })

    # ---- Histogram of max-loading: before vs after ----
    bins = [0, 50, 75, 90, 100, 125, 150, 200, 500, 1000, 1e6]
    bin_labels = ["0-50%", "50-75%", "75-90%", "90-100%",
                  "100-125%", "125-150%", "150-200%", "200-500%",
                  "500-1000%", ">1000%"]
    hist_da, _ = np.histogram(np.nan_to_num(max_da), bins=bins)
    hist_post, _ = np.histogram(np.nan_to_num(max_post), bins=bins)

    # ---- KPIs ----
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    n_lines_overloaded_da = int((max_da > 100).sum())
    n_lines_overloaded_post = int((max_post > 100).sum())
    overloaded_lh_da = int((loading_da > 100).sum())
    overloaded_lh_post = int((loading_post > 100).sum())
    kpis = {
        "n_lines": int(len(sub_line_ids)),
        "n_snapshots": T,
        "n_overloaded_lines_da": n_lines_overloaded_da,
        "n_overloaded_lines_post": n_lines_overloaded_post,
        "overloaded_lh_da": overloaded_lh_da,
        "overloaded_lh_post": overloaded_lh_post,
        "max_loading_da_pct": round(float(np.nan_to_num(max_da).max()), 1),
        "max_loading_post_pct": round(float(np.nan_to_num(max_post).max()), 1),
        "hours_with_overload_pre": int((n_over_da > 0).sum()),
        "hours_with_overload_post": int((n_over_post > 0).sum()),
        "total_ramp_up_TWh": float(summary.get("total_ramp_up_TWh", float(np.maximum(delta_gen, 0).sum()/1e6))),
        "total_ramp_down_TWh": float(summary.get("total_ramp_down_TWh", float(-np.minimum(delta_gen, 0).sum()/1e6))),
        "RES_curtailed_TWh": float(summary.get("RES_curtailed_TWh", 0.0)),
        "conventional_ramped_up_TWh": float(summary.get("conventional_ramped_up_TWh", 0.0)),
        "total_cost_MEUR": float(summary.get("total_cost_MEUR", float(cost_per_h.sum() / 1e6))),
        "BNetzA_2024_TWh": 30.3,
        "BNetzA_2024_RES_TWh": 9.4,
        "BNetzA_2024_cost_MEUR": 2776,
    }
    log.info(f"KPIs: {json.dumps(kpis, indent=2)}")

    # ---- Top 30 line records ----
    top30 = []
    for rank, i in enumerate(top_idx):
        lid = str(sub_line_ids[i])
        row = line_attrs.loc[sub_line_ids[i]]
        b0, b1 = str(row["bus0"]), str(row["bus1"])
        top30.append({
            "rank": rank + 1,
            "id": lid,
            "v": int(row["v_nom"]),
            "s_nom": round(float(row["s_nom"]), 0),
            "len_km": round(float(row["length"]), 1),
            "max_da_pct": round(float(max_da[i]), 1),
            "max_post_pct": round(float(max_post[i]), 1),
            "overload_h_da": int(overload_h_da[i]),
            "overload_h_post": int(overload_h_post[i]),
            "relief_GWh": round(float(relief_mwh[i]) / 1e3, 1),
            "state": bus_regions.loc[b0, "state"] if b0 in bus_regions.index else "",
            "tso": bus_regions.loc[b0, "tso"] if b0 in bus_regions.index else "",
        })

    payload = {
        "kpis": kpis,
        "snapshots": snaps_iso,
        "system": {
            "max_da": [round(float(v), 1) for v in sys_max_da],
            "max_post": [round(float(v), 1) for v in sys_max_post],
            "n_over_da": [int(v) for v in n_over_da],
            "n_over_post": [int(v) for v in n_over_post],
            "cost_eur_per_h": [round(float(v), 0) for v in cost_per_h],
            "n_binding_per_h": [int(v) for v in n_binding_per_h],
        },
        "hist": {
            "labels": bin_labels,
            "da": [int(v) for v in hist_da],
            "post": [int(v) for v in hist_post],
        },
        "by_carrier": by_carrier_payload,
        "by_tso": by_tso_payload,
        "lines_geo": line_geo,
        "top30": top30,
        "top_hourly": top_hourly,
    }
    return payload


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Redispatch 2.0 — grid_beta — 8760 h</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" charset="utf-8"></script>
<style>
:root {
  --bg: #fafbfc; --fg: #1c1f25; --muted: #5f6773; --accent: #2563eb;
  --good: #16a34a; --bad: #dc2626; --warn: #d97706; --soft: #e5e7eb;
  --card: #fff; --border: #d1d5db;
}
* { box-sizing: border-box; }
body { font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       margin: 0; background: var(--bg); color: var(--fg); }
.wrap { max-width: 1300px; margin: 0 auto; padding: 24px; }
h1 { margin: 0 0 4px; font-size: 26px; letter-spacing: -0.01em; }
h2 { margin: 28px 0 12px; font-size: 18px; }
.sub { color: var(--muted); margin-bottom: 18px; font-size: 13px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
        gap: 12px; margin-bottom: 22px; }
.kpi { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
       padding: 12px 14px; }
.kpi .v { font-size: 22px; font-weight: 600; }
.kpi .l { font-size: 12px; color: var(--muted); margin-top: 2px; }
.kpi .b { font-size: 11px; color: var(--muted); margin-top: 4px; }
.kpi.good .v { color: var(--good); }
.kpi.bad .v  { color: var(--bad); }
.kpi.warn .v { color: var(--warn); }
.chartbox, .mapbox { background: var(--card); border: 1px solid var(--border);
                     border-radius: 8px; padding: 8px; margin-bottom: 18px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.grid2 .chartbox { margin-bottom: 0; }
table { width: 100%; border-collapse: collapse; font-size: 13px; background: var(--card); }
th { background: #f3f4f6; padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); }
td { padding: 7px 10px; border-bottom: 1px solid var(--soft); }
tr:hover { background: #f9fafb; cursor: pointer; }
tr.active { background: #eff6ff; }
.legend { font-size: 12px; color: var(--muted); margin: 6px 0 0; }
.method { color: var(--muted); font-size: 13px; line-height: 1.7; background: var(--card);
          border: 1px solid var(--border); border-radius: 8px; padding: 14px 18px; }
.method code { background: #f0f0f0; padding: 1px 4px; border-radius: 3px; font-size: 12px; }
#mapDa, #mapPost { height: 420px; border-radius: 6px; }
.tabs { display: flex; gap: 8px; margin: 10px 0; }
.tab { padding: 6px 14px; border: 1px solid var(--border); background: var(--card);
       border-radius: 6px; cursor: pointer; font-size: 13px; }
.tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
</style>
</head>
<body>
<div class="wrap">

<h1>Redispatch 2.0 — German transmission grid 2025</h1>
<div class="sub">
  Scenario <b>grid_beta</b> · 7723 buses · 12911 lines · 8760-h day-ahead workflow:
  copperplate dispatch → DC power flow → §13a redispatch LP → revised flows.
  Line limit 100 % of s_nom · RES Mindestfaktor ×10 · KWK ×5 · PSTs and HVDC free.
</div>

<div class="kpis" id="kpis"></div>

<h2>Before vs After</h2>
<div class="grid2">
  <div class="chartbox"><div id="hist"></div></div>
  <div class="chartbox"><div id="timeline"></div></div>
</div>

<h2>Loading map — DA dispatch vs Post-redispatch</h2>
<div class="grid2">
  <div class="mapbox"><div class="sub" style="margin: 4px 4px 8px">DA dispatch (pre-redispatch)</div><div id="mapDa"></div></div>
  <div class="mapbox"><div class="sub" style="margin: 4px 4px 8px">Post-redispatch</div><div id="mapPost"></div></div>
</div>

<h2>Redispatch volumes by carrier</h2>
<div class="chartbox"><div id="byCarrier"></div></div>
<div class="legend">RES units (×10 cost weight) and KWK (×5) are curtailed only when conventional headroom is exhausted on the binding element. BNetzA real-life 2024: 30.3 TWh total, 9.4 TWh RES, 2.78 G€ cost.</div>

<h2>Redispatch volumes by TSO area</h2>
<div class="chartbox"><div id="byTso"></div></div>

<h2>Top 30 lines driving redispatch</h2>
<div class="sub">Click a row to see DA vs post-redispatch hourly flow.</div>
<div class="chartbox">
  <table id="lineTable">
    <thead>
      <tr><th>#</th><th>Line</th><th>kV</th><th>s_nom (MW)</th><th>km</th>
          <th>max DA %</th><th>max post %</th><th>over-h DA</th><th>over-h post</th>
          <th>relief GWh</th><th>state</th><th>TSO</th></tr>
    </thead>
    <tbody></tbody>
  </table>
</div>
<div class="chartbox"><div id="lineFlow"></div></div>

<h2>Methodology</h2>
<div class="method">
  <p><b>Day-Ahead Redispatch 2.0</b> (German Redispatch 2.0 / §§ 13a–c EnWG, in force since 2021-10-01).
  Each of the 8760 hourly DA dispatch outcomes is checked against PTDF-based linear line flows.
  For every hour with at least one line at <code>|f| &gt; s_nom</code>, the runner solves a cost-minimising
  redispatch LP that minimises <code>Σ_g  m_g · c_g · |Δp_g|</code> with the regulatory Mindestfaktor:
  <code>m = 10</code> for RES, <code>m = 5</code> for KWK / CHP, <code>m = 1</code> for everything else.</p>

  <p>Decision variables: aggregate per <code>(bus, carrier)</code> ramps Δp± for every unit with
  <code>|PTDF| ≥ 2 %</code> on any binding line, plus free Δp on all 14 HVDC links (ALEGRO,
  NordLink, Baltic Cable, etc.) and 19 phase-shifting transformers (PSTs). The LP is solved by
  <code>scipy.optimize.linprog(method=&quot;highs&quot;)</code>. The top-40 most-overloaded lines per hour
  are constrained; structurally infeasible residuals are absorbed by slack variables at
  500 €/MWh, representing TSO emergency action beyond DA-redispatch.</p>

  <p>Post-redispatch flows are computed as <code>f_post = f_DA + PTDF · Δp_net</code>. The model
  intentionally idealises the day-ahead mechanism: there is no intraday market, no 15-min
  resolution, no N-1 contingency reserve, and no balancing-group bookkeeping. Real BNetzA 2024
  redispatch volume was 30.3 TWh at 2.78 G€ — substantial deviations indicate either residual
  structural overload in the topology (under-rated 110 kV lines in radial regions) or unmodelled
  topology measures (PST swing limits, network switching) that real TSOs use under § 13(1).</p>
</div>

</div>

<script>
const PAYLOAD = __PAYLOAD__;

// ---- KPIs ----
function fmt(n, p=1) { return Number(n).toLocaleString(undefined, { maximumFractionDigits: p }); }
function kpiCard(label, value, badge, cls="") {
  return `<div class="kpi ${cls}"><div class="v">${value}</div>` +
         `<div class="l">${label}</div>` +
         (badge ? `<div class="b">${badge}</div>` : "") + `</div>`;
}
const k = PAYLOAD.kpis;
document.getElementById("kpis").innerHTML = [
  kpiCard("Total redispatched (up)", fmt(k.total_ramp_up_TWh, 2) + " TWh",
          "BNetzA 2024: ~15 TWh", "warn"),
  kpiCard("Total redispatched (down)", fmt(k.total_ramp_down_TWh, 2) + " TWh",
          "BNetzA 2024: ~14 TWh", "warn"),
  kpiCard("RES curtailed", fmt(k.RES_curtailed_TWh, 2) + " TWh",
          "BNetzA 2024: 9.4 TWh", "warn"),
  kpiCard("Conventional ramped up", fmt(k.conventional_ramped_up_TWh, 2) + " TWh", null, "warn"),
  kpiCard("Hours with overload (pre)", fmt(k.hours_with_overload_pre, 0) + " / " + fmt(k.n_snapshots, 0),
          fmt(100 * k.hours_with_overload_pre / k.n_snapshots, 1) + "%", "bad"),
  kpiCard("Hours with overload (post)", fmt(k.hours_with_overload_post, 0) + " / " + fmt(k.n_snapshots, 0),
          fmt(100 * k.hours_with_overload_post / k.n_snapshots, 1) + "% (residual)",
          k.hours_with_overload_post < k.hours_with_overload_pre ? "good" : "bad"),
  kpiCard("Max line loading (pre)", fmt(k.max_loading_da_pct, 0) + "%", null, "bad"),
  kpiCard("Max line loading (post)", fmt(k.max_loading_post_pct, 0) + "%",
          k.max_loading_post_pct < k.max_loading_da_pct ?
              "↓ " + fmt(k.max_loading_da_pct - k.max_loading_post_pct, 0) + " pp" : "",
          k.max_loading_post_pct < 110 ? "good" : "warn"),
  kpiCard("Redispatch cost", fmt(k.total_cost_MEUR, 0) + " M€",
          "BNetzA 2024: 2 776 M€", "warn"),
  kpiCard("Lines overloaded (pre)", fmt(k.n_overloaded_lines_da, 0) + " / " + fmt(k.n_lines, 0),
          fmt(100 * k.n_overloaded_lines_da / k.n_lines, 1) + "%", "bad"),
  kpiCard("Lines overloaded (post)", fmt(k.n_overloaded_lines_post, 0) + " / " + fmt(k.n_lines, 0),
          fmt(100 * k.n_overloaded_lines_post / k.n_lines, 1) + "%",
          k.n_overloaded_lines_post < k.n_overloaded_lines_da ? "good" : "bad"),
  kpiCard("Line-hours overloaded (pre)", fmt(k.overloaded_lh_da, 0), null, "bad"),
].join("");

// ---- Histogram ----
Plotly.newPlot("hist", [
  { x: PAYLOAD.hist.labels, y: PAYLOAD.hist.da,   type: "bar",
    name: "DA (pre)",  marker: { color: "#dc2626" } },
  { x: PAYLOAD.hist.labels, y: PAYLOAD.hist.post, type: "bar",
    name: "Post-RD",   marker: { color: "#16a34a" } },
], {
  title: { text: "Line max-loading distribution", font: { size: 14 } },
  barmode: "group", margin: { l: 50, r: 10, t: 40, b: 50 },
  yaxis: { title: "# lines (log)", type: "log" },
  height: 320,
}, { displayModeBar: false });

// ---- System max-loading timeline ----
Plotly.newPlot("timeline", [
  { x: PAYLOAD.snapshots, y: PAYLOAD.system.max_da,   type: "scatter",
    mode: "lines", name: "max % DA",   line: { color: "#dc2626", width: 0.8 } },
  { x: PAYLOAD.snapshots, y: PAYLOAD.system.max_post, type: "scatter",
    mode: "lines", name: "max % post", line: { color: "#16a34a", width: 0.8 } },
], {
  title: { text: "System-wide max loading per hour (% of s_nom)", font: { size: 14 } },
  margin: { l: 50, r: 10, t: 40, b: 40 },
  yaxis: { title: "%", type: "log" },
  hovermode: "x unified",
  height: 320,
}, { displayModeBar: false });

// ---- Maps ----
function makeMap(divId, key) {
  const m = L.map(divId, { preferCanvas: true }).setView([51.2, 10.4], 5);
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 18, attribution: "Esri"
  }).addTo(m);
  const lines = PAYLOAD.lines_geo;
  function colorFor(v) {
    if (v >= 200) return "#7f1d1d";
    if (v >= 150) return "#dc2626";
    if (v >= 100) return "#f97316";
    if (v >= 80)  return "#eab308";
    if (v >= 50)  return "#84cc16";
    return "#22c55e";
  }
  for (const ln of lines) {
    const w = ln.v === 380 ? 1.6 : ln.v === 220 ? 1.1 : 0.7;
    const c = colorFor(ln[key]);
    L.polyline([[ln.y0, ln.x0], [ln.y1, ln.x1]],
               { color: c, weight: w, opacity: 0.7 }).addTo(m);
  }
  return m;
}
makeMap("mapDa",   "max_da");
makeMap("mapPost", "max_post");

// ---- Volumes by carrier ----
function byCarrierPlot() {
  const labels = PAYLOAD.by_carrier.map(d => d.label);
  const upY    = PAYLOAD.by_carrier.map(d => d.up_TWh);
  const dnY    = PAYLOAD.by_carrier.map(d => -d.dn_TWh);
  const colors = PAYLOAD.by_carrier.map(d => d.color);
  Plotly.newPlot("byCarrier", [
    { x: labels, y: upY, type: "bar", name: "Ramp-up (TWh)", marker: { color: colors } },
    { x: labels, y: dnY, type: "bar", name: "Ramp-down (TWh)", marker: { color: colors, opacity: 0.55 } },
  ], {
    barmode: "relative",
    title: { text: "Redispatch volumes by carrier (annual TWh)", font: { size: 14 } },
    margin: { l: 50, r: 10, t: 40, b: 80 },
    yaxis: { title: "TWh" },
    height: 380,
  }, { displayModeBar: false });
}
byCarrierPlot();

// ---- Volumes by TSO ----
function byTsoPlot() {
  const tsos = ["50Hertz", "Amprion", "TenneT", "TransnetBW", "Unknown"];
  // Pivot: carrier × tso → up - dn
  const by_carrier_in_tso = {};
  for (const r of PAYLOAD.by_tso) {
    by_carrier_in_tso[r.carrier] = by_carrier_in_tso[r.carrier] || {};
    by_carrier_in_tso[r.carrier][r.tso] = (r.up_TWh - r.dn_TWh);
  }
  const carriers = Object.keys(by_carrier_in_tso).sort();
  const traces = [];
  for (const c of carriers) {
    const ys = tsos.map(t => by_carrier_in_tso[c][t] || 0);
    const tot = ys.reduce((a, b) => a + Math.abs(b), 0);
    if (tot < 0.01) continue;
    traces.push({
      x: tsos, y: ys, name: PAYLOAD.by_carrier.find(d => d.carrier === c)?.label || c,
      type: "bar",
      marker: { color: PAYLOAD.by_carrier.find(d => d.carrier === c)?.color || "#999" },
    });
  }
  Plotly.newPlot("byTso", traces, {
    barmode: "relative",
    title: { text: "Net redispatch by TSO area (TWh; +up / −down)", font: { size: 14 } },
    margin: { l: 50, r: 10, t: 40, b: 60 },
    yaxis: { title: "TWh" },
    height: 380,
  }, { displayModeBar: false });
}
byTsoPlot();

// ---- Line table + flow plot ----
const tbody = document.querySelector("#lineTable tbody");
PAYLOAD.top30.forEach((r, i) => {
  const tr = document.createElement("tr");
  tr.dataset.id = r.id;
  tr.innerHTML = `<td>${r.rank}</td><td>${r.id}</td><td>${r.v}</td>
    <td style="text-align:right">${fmt(r.s_nom, 0)}</td>
    <td style="text-align:right">${fmt(r.len_km, 1)}</td>
    <td style="text-align:right">${fmt(r.max_da_pct, 0)}%</td>
    <td style="text-align:right">${fmt(r.max_post_pct, 0)}%</td>
    <td style="text-align:right">${fmt(r.overload_h_da, 0)}</td>
    <td style="text-align:right">${fmt(r.overload_h_post, 0)}</td>
    <td style="text-align:right">${fmt(r.relief_GWh, 1)}</td>
    <td>${r.state || ""}</td><td>${r.tso || ""}</td>`;
  tr.onclick = () => {
    document.querySelectorAll("#lineTable tbody tr.active").forEach(x => x.classList.remove("active"));
    tr.classList.add("active");
    showFlow(r.id);
  };
  tbody.appendChild(tr);
});
function showFlow(lineId) {
  const h = PAYLOAD.top_hourly[lineId];
  if (!h) return;
  Plotly.newPlot("lineFlow", [
    { x: PAYLOAD.snapshots, y: h.da,   type: "scatter", mode: "lines",
      name: "DA flow (MW)",   line: { color: "#dc2626", width: 1 } },
    { x: PAYLOAD.snapshots, y: h.post, type: "scatter", mode: "lines",
      name: "Post-RD flow",   line: { color: "#16a34a", width: 1 } },
  ], {
    title: { text: `Hourly flow on line ${lineId}`, font: { size: 14 } },
    margin: { l: 50, r: 10, t: 40, b: 40 },
    yaxis: { title: "MW" },
    height: 340,
    hovermode: "x unified",
  }, { displayModeBar: false });
}
// Default to top line
if (PAYLOAD.top30.length) {
  tbody.firstChild.classList.add("active");
  showFlow(PAYLOAD.top30[0].id);
}
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default=str(DEFAULT_NPZ))
    ap.add_argument("--summary", default=str(DEFAULT_SUMMARY))
    ap.add_argument("--base-nc", default=str(DEFAULT_BASE_NC))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--top-n", type=int, default=30)
    args = ap.parse_args()

    payload = build_payload(Path(args.npz), Path(args.summary),
                            Path(args.base_nc), top_n=args.top_n)
    html = HTML_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, allow_nan=False))
    Path(args.out).write_text(html, encoding="utf-8")
    log.info(f"Wrote {args.out}  ({Path(args.out).stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
