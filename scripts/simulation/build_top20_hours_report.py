#!/usr/bin/env python3
"""build_top20_hours_report.py — Rich annual HTML with:
   - Annual headline KPIs (overload reduction, redispatch volumes, vs BNetzA)
   - Generation mix (carrier breakdown) annual + by month
   - Geographic map of the 20 worst hours: bus dispatch, line overloads, redispatch
     deltas, storage state, N-1 violations
   - For each of the 20 worst hours: a focused panel with all numbers

Inputs:
  results/dispatch_year_genreloc_pf.nc            (full-year DC PF with gen-reloc)
  results/redispatch_year_genreloc_n1.npz         (stage-2 hourly deltas)
  results/sample12w_summary_genreloc_s2.json      (for reference)  — replaced by annual summary
  results/sample12w_summary_baseline.json         (for delta-vs-baseline comparison)

Output:
  results/top20_hours_report.html
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

RES = Path("/root/egon_2025_project/results")
DB_URL = "postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data"

RES_CARRIERS = {"solar", "onwind", "offwind", "run_of_river", "reservoir",
                "biogas", "biomass", "waste"}
CONV_CARRIERS = {"coal", "hard_coal", "lignite", "gas_ccgt", "gas_chp", "oil",
                 "other", "other_conventional", "hydrogen"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pf-nc", default=str(RES / "dispatch_year_genreloc_pf.nc"))
    ap.add_argument("--n1-npz", default=str(RES / "redispatch_year_genreloc_n1.npz"))
    ap.add_argument("--out", default=str(RES / "top20_hours_report.html"))
    ap.add_argument("--top-n", type=int, default=20)
    args = ap.parse_args()

    log.info(f"Loading {args.pf_nc}")
    n = pypsa.Network(args.pf_nc)
    n.lines["x"] = n.lines["x"].clip(lower=0.05)
    n.calculate_dependent_values()
    T = len(n.snapshots)
    snapshots = pd.to_datetime(n.snapshots)
    log.info(f"  {T} snapshots, {len(n.buses)} buses, {len(n.lines)} lines, "
             f"{len(n.transformers)} trafos, {len(n.generators)} gens")

    log.info(f"Loading {args.n1_npz}")
    z = np.load(args.n1_npz, allow_pickle=True)
    delta_gen = z["delta_gen"]                # (T, G) — total deltas (stage1+2)
    line_flow_da = z["line_flow_da"]          # original copperplate flows
    line_flow_post = z["line_flow_post"]      # post stage-2 final flows
    s_nom = z["s_nom"]
    sub_line_ids = list(z["sub_line_ids"])
    gen_ids = list(z["gen_ids"])
    gen_carrier = z["gen_carrier"]
    n_binding_n0 = z["n_binding_n0"] if "n_binding_n0" in z.files else z["n_binding"]
    n_binding_n1 = z["n_binding_n1"] if "n_binding_n1" in z.files else np.zeros(T, int)
    slack_mw = z["slack_mw"] if "slack_mw" in z.files else np.zeros(T)
    cost = z["cost"] if "cost" in z.files else np.zeros(T)
    res_mask = np.isin(gen_carrier, list(RES_CARRIERS))
    conv_mask = np.isin(gen_carrier, list(CONV_CARRIERS))

    sn_safe = np.where(s_nom > 0, s_nom, np.inf).astype(np.float32)

    log.info("Computing per-hour severity metrics...")
    abs_post = np.abs(line_flow_post)
    abs_da = np.abs(line_flow_da)
    loading_post = abs_post / sn_safe
    loading_da = abs_da / sn_safe
    max_load_post_h = loading_post.max(axis=1)
    max_load_da_h = loading_da.max(axis=1)
    n_over_post_h = (loading_post > 1).sum(axis=1)
    n_over_da_h = (loading_da > 1).sum(axis=1)
    res_curt_h = -np.minimum(delta_gen[:, res_mask], 0).sum(axis=1)
    ramp_up_h = np.maximum(delta_gen, 0).sum(axis=1)
    ramp_dn_h = -np.minimum(delta_gen, 0).sum(axis=1)

    # severity score = max post loading + n binding + slack — pick top N
    severity = max_load_post_h + 0.5 * n_binding_n0 + 0.2 * n_binding_n1
    top_idx = np.argsort(severity)[::-1][:args.top_n]
    top_idx = np.sort(top_idx)              # chronological inside the panel
    log.info(f"Selected top {len(top_idx)} severity hours.")

    # ---- bus regions / state map ----
    log.info("Mapping buses to states...")
    eng = create_engine(DB_URL)
    bs = pd.read_sql(
        "SELECT b.bus_id::text bus_id, ST_X(geom) lon, ST_Y(geom) lat, "
        "(SELECT gen FROM boundaries.vg250_lan WHERE ST_Contains(geometry, b.geom) AND gf=4 LIMIT 1) state "
        "FROM grid.egon_etrago_bus b WHERE scn_name='grid_beta'", eng
    ).drop_duplicates("bus_id").set_index("bus_id")

    # ---- annual aggregates ----
    gen_by_carrier = n.generators.groupby("carrier").apply(
        lambda g: n.generators_t.p[g.index.intersection(n.generators_t.p.columns)].sum().sum() / 1e6
    )
    annual_gen_TWh = gen_by_carrier.sort_values(ascending=False)
    annual_load_TWh = (
        (n.loads_t.p_set if (n.loads_t.p is None or n.loads_t.p.empty) else n.loads_t.p).sum().sum() / 1e6
    )
    total_ramp_up = float(ramp_up_h.sum() / 1e6)
    total_ramp_dn = float(ramp_dn_h.sum() / 1e6)
    total_res_curt = float(res_curt_h.sum() / 1e6)
    total_conv_up = float(np.maximum(delta_gen[:, conv_mask], 0).sum() / 1e6)
    total_slack = float(slack_mw.sum())
    hrs_over_da = int((n_over_da_h > 0).sum())
    hrs_over_post = int((n_over_post_h > 0).sum())
    hrs_n1 = int((n_binding_n1 > 0).sum())

    # ---- monthly breakdown ----
    df_h = pd.DataFrame({
        "snap": snapshots,
        "max_load_da": max_load_da_h, "max_load_post": max_load_post_h,
        "n_over_da": n_over_da_h, "n_over_post": n_over_post_h,
        "res_curt_MW": res_curt_h, "ramp_up_MW": ramp_up_h, "ramp_dn_MW": ramp_dn_h,
        "n_binding_n1": n_binding_n1, "slack_mw": slack_mw,
    })
    df_h["month"] = df_h["snap"].dt.month
    monthly = df_h.groupby("month").agg(
        max_load_da=("max_load_da", "max"),
        max_load_post=("max_load_post", "max"),
        hrs_over_da=("n_over_da", lambda s: int((s > 0).sum())),
        hrs_over_post=("n_over_post", lambda s: int((s > 0).sum())),
        res_curt_TWh=("res_curt_MW", lambda s: s.sum() / 1e6),
        ramp_up_TWh=("ramp_up_MW", lambda s: s.sum() / 1e6),
        ramp_dn_TWh=("ramp_dn_MW", lambda s: s.sum() / 1e6),
        hrs_n1=("n_binding_n1", lambda s: int((s > 0).sum())),
    ).reset_index()
    monthly_payload = monthly.to_dict(orient="records")

    # ---- carrier breakdowns: annual + redispatch ----
    log.info("Building carrier breakdowns...")
    cs = n.generators["carrier"].values
    bus_for_gen = n.generators["bus"].values
    # annual gen TWh per gen
    g_in_p = list(set(gen_ids) & set(n.generators_t.p.columns))
    p_total_per_gen = n.generators_t.p[g_in_p].sum().to_dict()
    # redispatch volume per carrier
    rd_up_carrier = {}
    rd_dn_carrier = {}
    for i, c in enumerate(gen_carrier):
        rd_up_carrier[c] = rd_up_carrier.get(c, 0) + float(max(delta_gen[:, i].sum(), 0))
        rd_dn_carrier[c] = rd_dn_carrier.get(c, 0) + float(max(-delta_gen[:, i].sum(), 0))
    # also: aggregate redispatch by sign-flipped MW per carrier
    by_carrier_payload = []
    for c in sorted(set(gen_carrier)):
        idx = np.where(gen_carrier == c)[0]
        up = float(np.maximum(delta_gen[:, idx], 0).sum() / 1e6)
        dn = float(-np.minimum(delta_gen[:, idx], 0).sum() / 1e6)
        gen_TWh = float(annual_gen_TWh.get(c, 0))
        by_carrier_payload.append({"carrier": c, "annual_gen_TWh": round(gen_TWh, 2),
                                   "rd_up_TWh": round(up, 3), "rd_dn_TWh": round(dn, 3)})
    by_carrier_payload.sort(key=lambda r: -r["annual_gen_TWh"])

    # ---- per-hour panels for top N ----
    log.info("Building top-N hour panels...")
    p_at = lambda t: n.generators_t.p.iloc[t]
    load_at = lambda t: (n.loads_t.p_set if (n.loads_t.p is None or n.loads_t.p.empty) else n.loads_t.p).iloc[t]

    panels = []
    for t in top_idx:
        snap = snapshots[t]
        # carrier breakdown at this hour
        p_t = p_at(t)
        cgens = n.generators.loc[p_t.index, "carrier"]
        gen_mix = p_t.groupby(cgens).sum().sort_values(ascending=False).to_dict()
        load_total = float(load_at(t).sum())
        # top binding lines (worst 8 by loading after stage 2)
        load_h = abs_post[t] / sn_safe
        top_l_idx = np.argsort(load_h)[::-1][:8]
        top_lines = []
        for li in top_l_idx:
            if load_h[li] < 1.01:
                continue
            lid = sub_line_ids[li]
            b0 = n.lines.at[lid, "bus0"]; b1 = n.lines.at[lid, "bus1"]
            v = int(n.buses.at[b0, "v_nom"])
            top_lines.append({
                "id": lid, "v": v, "s_nom": int(s_nom[li]),
                "flow_da_MW": round(float(line_flow_da[t, li]), 0),
                "flow_post_MW": round(float(line_flow_post[t, li]), 0),
                "loading_da": round(float(loading_da[t, li]), 2),
                "loading_post": round(load_h[li], 2),
                "state_b0": bs.loc[b0, "state"] if b0 in bs.index else "",
            })
        # storage state at this hour (top 5 dispatchers)
        ph = n.storage_units_t.p_dispatch.iloc[t] if (n.storage_units_t.p_dispatch is not None
                                                      and not n.storage_units_t.p_dispatch.empty) else pd.Series()
        ph_top = []
        if len(ph):
            ph_t = ph.sort_values(ascending=False).head(5)
            for sid, p in ph_t.items():
                if p < 1: continue
                sub = n.storage_units.loc[sid]
                ph_top.append({"id": sid, "bus": sub["bus"], "carrier": sub["carrier"],
                               "p_dispatch_MW": round(float(p), 0)})
        # top redispatch ramps at this hour
        dg = delta_gen[t]
        top_up = np.argsort(dg)[::-1][:5]
        top_dn = np.argsort(dg)[:5]
        rd_up_payload = []
        for i in top_up:
            if dg[i] < 1: continue
            rd_up_payload.append({"gen_id": gen_ids[i], "carrier": str(gen_carrier[i]),
                                  "delta_MW": round(float(dg[i]), 1)})
        rd_dn_payload = []
        for i in top_dn:
            if dg[i] > -1: continue
            rd_dn_payload.append({"gen_id": gen_ids[i], "carrier": str(gen_carrier[i]),
                                  "delta_MW": round(float(dg[i]), 1)})
        panels.append({
            "snap": str(snap), "weekday": snap.day_name(), "hour": int(snap.hour),
            "max_load_da_pct": round(float(max_load_da_h[t]) * 100, 0),
            "max_load_post_pct": round(float(max_load_post_h[t]) * 100, 0),
            "n_over_da": int(n_over_da_h[t]), "n_over_post": int(n_over_post_h[t]),
            "n_binding_n1": int(n_binding_n1[t]),
            "slack_mw": round(float(slack_mw[t]), 0),
            "ramp_up_MW": round(float(ramp_up_h[t]), 0),
            "ramp_dn_MW": round(float(ramp_dn_h[t]), 0),
            "res_curt_MW": round(float(res_curt_h[t]), 0),
            "gen_mix": {k: round(float(v), 0) for k, v in gen_mix.items() if abs(v) > 1},
            "load_total_MW": round(load_total, 0),
            "top_lines": top_lines,
            "storage_top": ph_top,
            "rd_up": rd_up_payload,
            "rd_dn": rd_dn_payload,
        })

    # ---- bus geo + worst-line geo for map context (just for the top-N) ----
    line_geo = {}
    for tinfo in panels:
        for L in tinfo["top_lines"]:
            lid = L["id"]
            if lid in line_geo: continue
            b0 = n.lines.at[lid, "bus0"]; b1 = n.lines.at[lid, "bus1"]
            if b0 not in n.buses.index or b1 not in n.buses.index: continue
            x0, y0 = float(n.buses.at[b0, "x"]), float(n.buses.at[b0, "y"])
            x1, y1 = float(n.buses.at[b1, "x"]), float(n.buses.at[b1, "y"])
            if not (np.isfinite(x0) and np.isfinite(x1)): continue
            line_geo[lid] = [round(y0, 4), round(x0, 4), round(y1, 4), round(x1, 4),
                             int(n.buses.at[b0, "v_nom"])]

    payload = {
        "scenario": "grid_beta + APPLY_GEN_VOLTAGE_FIX (≥150 MW on 110kV → nearest EHV)",
        "T": T, "n_lines": int(len(n.lines)), "n_buses": int(len(n.buses)),
        "annual": {
            "load_TWh": round(annual_load_TWh, 1),
            "gen_TWh_by_carrier": {k: round(float(v), 1) for k, v in annual_gen_TWh.items()},
            "ramp_up_TWh": round(total_ramp_up, 2),
            "ramp_dn_TWh": round(total_ramp_dn, 2),
            "res_curtailed_TWh": round(total_res_curt, 2),
            "conv_up_TWh": round(total_conv_up, 2),
            "hrs_overload_da": hrs_over_da,
            "hrs_overload_post": hrs_over_post,
            "hrs_n1_binding": hrs_n1,
            "residual_slack_MWsum": round(total_slack, 0),
            "max_loading_da_pct": round(float(max_load_da_h.max() * 100), 0),
            "max_loading_post_pct": round(float(max_load_post_h.max() * 100), 0),
            "overload_LH_da": int((loading_da > 1).sum()),
            "overload_LH_post": int((loading_post > 1).sum()),
        },
        "bnetza_2024": {"total_TWh": 30.3, "res_TWh": 9.4, "cost_MEUR": 2776},
        "monthly": monthly_payload,
        "by_carrier": by_carrier_payload,
        "panels": panels,
        "line_geo": line_geo,
    }

    html = HTML.replace("__PAYLOAD__", json.dumps(payload, default=str, separators=(",", ":")))
    Path(args.out).write_text(html)
    log.info(f"Wrote {args.out}  ({Path(args.out).stat().st_size/1e6:.1f} MB)")


HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Top-20 hours · grid_beta with gen-relocation</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f8f9fb; color: #222; }
  header { background: #1c2733; color: white; padding: 14px 22px; }
  header h1 { margin: 0; font-size: 19px; font-weight: 600; }
  header .sub { font-size: 12px; opacity: 0.85; margin-top: 4px; }
  .container { max-width: 1500px; margin: 0 auto; padding: 14px 18px; }
  .kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 14px; }
  .kpi { background: white; padding: 12px 14px; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  .kpi .v { font-size: 22px; font-weight: 700; color: #1c2733; }
  .kpi .l { font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }
  .kpi .d { font-size: 11px; margin-top: 2px; }
  .kpi.good .d { color: #0a8a3a; } .kpi.bad .d { color: #cc2222; }
  .panel { background: white; border-radius: 8px; padding: 16px 18px; margin: 14px 0; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  .panel h2 { margin: 0 0 12px 0; font-size: 16px; color: #1c2733; border-bottom: 1px solid #eee; padding-bottom: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { padding: 6px 9px; text-align: left; border-bottom: 1px solid #eef; }
  th { background: #f2f4f8; font-weight: 600; }
  .hour-list { display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }
  .hour-card { background: white; border-radius: 8px; padding: 14px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); border-left: 4px solid #cc2222; }
  .hour-card h3 { margin: 0 0 8px 0; font-size: 14px; color: #1c2733; }
  .hour-card h3 .badge { background: #cc2222; color: white; padding: 2px 7px; border-radius: 10px; font-size: 11px; margin-left: 6px; }
  .hour-card .sub-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin: 6px 0 10px 0; font-size: 11px; }
  .hour-card .sub-stats div { background: #f6f7fa; padding: 4px 7px; border-radius: 4px; }
  .hour-card .sub-stats b { color: #1c2733; }
  .hour-card details { margin-top: 6px; }
  .hour-card details summary { cursor: pointer; font-size: 12px; padding: 4px 0; color: #335; font-weight: 600; }
  .hour-card details table { font-size: 11px; }
  .map-mini { height: 220px; border-radius: 6px; overflow: hidden; margin: 8px 0 6px 0; }
  .small { font-size: 11px; color: #666; }
  .pos { color: #0a8a3a; } .neg { color: #cc2222; }
  .chart-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
</style></head><body>
<header>
  <h1>grid_beta · annual two-stage redispatch · with ≥150 MW gen-voltage fix</h1>
  <div class="sub" id="hdr-sub"></div>
</header>
<div class="container" id="root"></div>

<script>
const P = __PAYLOAD__;
const A = P.annual, BN = P.bnetza_2024;
document.getElementById('hdr-sub').textContent =
   `8760 snapshots · ${P.n_buses} buses · ${P.n_lines} lines · scenario: ${P.scenario}`;

function pct(v, d) { d = d === undefined ? 1 : d; return v.toFixed(d) + '%'; }
function num(v, d) { d = d === undefined ? 1 : d; return v.toLocaleString(undefined, {minimumFractionDigits: d, maximumFractionDigits: d}); }
function el(tag, attrs, html) {
  const e = document.createElement(tag); if (attrs) Object.assign(e.style, attrs.style||{});
  if (attrs && attrs.cls) e.className = attrs.cls;
  if (html !== undefined) e.innerHTML = html; return e;
}
function kpi(label, value, delta, kind) {
  return `<div class="kpi ${kind||''}"><div class="l">${label}</div><div class="v">${value}</div><div class="d">${delta||''}</div></div>`;
}

const root = document.getElementById('root');

// ---- KPI grid ----
const kpiGrid = el('div', {cls: 'kpi-grid'});
kpiGrid.innerHTML =
  kpi('Annual load',         num(A.load_TWh,0)+' TWh', '(BDEW DE 2024 ≈ 462)') +
  kpi('Annual generation',   num(Object.values(A.gen_TWh_by_carrier).reduce((a,b)=>a+b,0),0)+' TWh','') +
  kpi('Total ramp up',       num(A.ramp_up_TWh,2)+' TWh',`BNetzA total congestion 2024: ${BN.total_TWh} TWh`) +
  kpi('RES curtailed',       num(A.res_curtailed_TWh,2)+' TWh',`BNetzA RES 2024: ${BN.res_TWh} TWh`) +
  kpi('Max DA loading',      pct(A.max_loading_da_pct,0), '', 'bad') +
  kpi('Max POST loading',    pct(A.max_loading_post_pct,0), '', A.max_loading_post_pct<150?'good':'bad') +
  kpi('Overloaded line-hours (DA)',   num(A.overload_LH_da,0), '') +
  kpi('Overloaded line-hours (POST)', num(A.overload_LH_post,0), `−${Math.round((1-A.overload_LH_post/Math.max(A.overload_LH_da,1))*100)}% vs DA`, 'good') +
  kpi('Hours overload DA',   num(A.hrs_overload_da,0)+' / 8760','') +
  kpi('Hours overload POST', num(A.hrs_overload_post,0)+' / 8760','') +
  kpi('Hours N-1 binding',   num(A.hrs_n1_binding,0)+' / 8760','') +
  kpi('Residual slack (MW·sum)', num(A.residual_slack_MWsum,0), 'structural insecurity flag', A.residual_slack_MWsum<1e6?'good':'bad');
root.appendChild(kpiGrid);

// ---- Generation mix ----
const genMixPanel = el('div', {cls: 'panel'});
genMixPanel.innerHTML = '<h2>Annual generation mix (TWh)</h2>';
const cars = Object.entries(A.gen_TWh_by_carrier).sort((a,b) => b[1]-a[1]);
const genDiv = el('div'); genDiv.id = 'gen-mix-chart';
genMixPanel.appendChild(genDiv);
root.appendChild(genMixPanel);
Plotly.newPlot('gen-mix-chart',
  [{x: cars.map(c=>c[0]), y: cars.map(c=>c[1]), type:'bar',
    marker:{color:cars.map(([c,v])=> c.startsWith('import')?'#888':({solar:'#f6c83c',onwind:'#a3d4f7',offwind:'#2b6cb0',biogas:'#7ed957',biomass:'#4b8a3a',reservoir:'#5a8db4',run_of_river:'#71b8d9',waste:'#9e7e3a',gas_chp:'#e8915f',gas_ccgt:'#d35d24',gas:'#d35d24',coal:'#444',hard_coal:'#444',lignite:'#7a4e2f',oil:'#222',hydrogen:'#19c2c2'})[c]||'#9aa1a8')},
    text: cars.map(c=> c[1].toFixed(1)), textposition:'outside'}],
  {margin:{l:50,r:20,t:10,b:90}, height:340, yaxis:{title:'TWh'}, xaxis:{tickangle:-30}});

// ---- Monthly breakdown chart ----
const monPanel = el('div', {cls:'panel'});
monPanel.innerHTML = '<h2>Monthly redispatch & overload pattern</h2><div class="chart-row"><div id="mon1" style="height:280px"></div><div id="mon2" style="height:280px"></div></div>';
root.appendChild(monPanel);
const months = P.monthly.map(m=>m.month);
Plotly.newPlot('mon1', [
  {x: months, y: P.monthly.map(m=>m.ramp_up_TWh), name:'ramp up', type:'bar', marker:{color:'#0a8a3a'}},
  {x: months, y: P.monthly.map(m=>m.ramp_dn_TWh), name:'ramp down', type:'bar', marker:{color:'#cc2222'}},
  {x: months, y: P.monthly.map(m=>m.res_curt_TWh), name:'RES curtailed', type:'bar', marker:{color:'#a3d4f7'}},
], {margin:{l:50,r:20,t:20,b:35}, barmode:'group', yaxis:{title:'TWh'}, xaxis:{title:'Month'}, legend:{orientation:'h'}});
Plotly.newPlot('mon2', [
  {x: months, y: P.monthly.map(m=>m.hrs_over_da), name:'hrs overload (DA)', type:'bar', marker:{color:'#cc2222'}},
  {x: months, y: P.monthly.map(m=>m.hrs_over_post), name:'hrs overload (POST)', type:'bar', marker:{color:'#e9a035'}},
  {x: months, y: P.monthly.map(m=>m.hrs_n1), name:'hrs N-1 binding', type:'bar', marker:{color:'#7c0d6c'}},
], {margin:{l:50,r:20,t:20,b:35}, barmode:'group', yaxis:{title:'hours'}, xaxis:{title:'Month'}, legend:{orientation:'h'}});

// ---- Carrier redispatch table ----
const carPanel = el('div', {cls:'panel'});
carPanel.innerHTML = '<h2>Annual redispatch volume by carrier (TWh)</h2>';
const carTable = el('table');
carTable.innerHTML = '<tr><th>Carrier</th><th>Annual gen</th><th>Ramp UP</th><th>Ramp DOWN</th><th>Net</th></tr>' +
  P.by_carrier.filter(r=>r.annual_gen_TWh>0.05 || r.rd_up_TWh>0.01 || r.rd_dn_TWh>0.01).map(r => `
    <tr><td>${r.carrier}</td><td>${num(r.annual_gen_TWh,2)}</td>
        <td class="pos">+${num(r.rd_up_TWh,3)}</td>
        <td class="neg">−${num(r.rd_dn_TWh,3)}</td>
        <td>${(r.rd_up_TWh-r.rd_dn_TWh).toFixed(3)}</td></tr>`).join('');
carPanel.appendChild(carTable);
root.appendChild(carPanel);

// ---- Top 20 worst hours ----
const top20Panel = el('div', {cls:'panel'});
top20Panel.innerHTML = `<h2>Top ${P.panels.length} worst hours — every metric, every line, every redispatch</h2>
  <div class="small">Severity = max post-loading + 0.5·N0-binding-lines + 0.2·N1-binding-pairs.
  Click each line / storage / ramp row to expand. Mini-map shows the worst overloaded lines at that hour.</div>`;
root.appendChild(top20Panel);
const list = el('div', {cls:'hour-list'});
root.appendChild(list);

P.panels.forEach((H, idx) => {
  const c = el('div', {cls:'hour-card'});
  const mapId = `map-${idx}`;
  let mixHtml = Object.entries(H.gen_mix).filter(([k,v])=>v>10).sort((a,b)=>b[1]-a[1]).slice(0,8).map(([k,v])=>`<span style="background:#eef;padding:2px 6px;border-radius:3px;margin-right:4px">${k}: <b>${num(v,0)}</b></span>`).join('');
  c.innerHTML = `
    <h3>${H.snap} (${H.weekday}, ${H.hour}:00) <span class="badge">#${idx+1} worst</span></h3>
    <div class="sub-stats">
      <div>Max load (DA): <b>${num(H.max_load_da_pct,0)}%</b></div>
      <div>Max load (POST): <b>${num(H.max_load_post_pct,0)}%</b></div>
      <div>Slack: <b>${num(H.slack_mw,0)} MW</b></div>
      <div>Overloaded DA: <b>${H.n_over_da}</b></div>
      <div>Overloaded POST: <b>${H.n_over_post}</b></div>
      <div>N-1 binding: <b>${H.n_binding_n1}</b></div>
      <div>Ramp up: <b>${num(H.ramp_up_MW,0)} MW</b></div>
      <div>Ramp down: <b>${num(H.ramp_dn_MW,0)} MW</b></div>
      <div>RES curt: <b>${num(H.res_curt_MW,0)} MW</b></div>
    </div>
    <div class="small">Total load: <b>${num(H.load_total_MW,0)} MW</b> · Gen mix top: ${mixHtml}</div>
    <div class="map-mini" id="${mapId}"></div>
    <details><summary>Top binding lines (${H.top_lines.length})</summary>
      <table><tr><th>Line</th><th>kV</th><th>s_nom</th><th>DA flow</th><th>POST flow</th><th>POST loading</th><th>state</th></tr>
       ${H.top_lines.map(L=>`<tr><td>${L.id}</td><td>${L.v}</td><td>${L.s_nom}</td><td>${num(L.flow_da_MW,0)}</td><td>${num(L.flow_post_MW,0)}</td><td><b>${pct(L.loading_post*100,0)}</b></td><td>${L.state_b0}</td></tr>`).join('')}
      </table></details>
    <details><summary>Storage top dispatchers (${H.storage_top.length})</summary>
      <table><tr><th>Unit</th><th>Bus</th><th>Carrier</th><th>p_dispatch MW</th></tr>
       ${H.storage_top.map(S=>`<tr><td>${S.id}</td><td>${S.bus}</td><td>${S.carrier}</td><td>${num(S.p_dispatch_MW,0)}</td></tr>`).join('')}
      </table></details>
    <details><summary>Top redispatch ramps</summary>
      <table><tr><th>Gen</th><th>Carrier</th><th>Δp (MW)</th></tr>
        ${H.rd_up.map(r=>`<tr><td>${r.gen_id}</td><td>${r.carrier}</td><td class="pos">+${num(r.delta_MW,1)}</td></tr>`).join('')}
        ${H.rd_dn.map(r=>`<tr><td>${r.gen_id}</td><td>${r.carrier}</td><td class="neg">${num(r.delta_MW,1)}</td></tr>`).join('')}
      </table></details>`;
  list.appendChild(c);
  setTimeout(() => {
    const coords = H.top_lines.map(L => P.line_geo[L.id]).filter(g => g);
    if (!coords.length) return;
    const lats = coords.flatMap(g => [g[0], g[2]]);
    const lons = coords.flatMap(g => [g[1], g[3]]);
    const map = L.map(mapId).setView([lats.reduce((a,b)=>a+b)/lats.length, lons.reduce((a,b)=>a+b)/lons.length], 8);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'OSM'}).addTo(map);
    coords.forEach((g, i) => {
      const loading = H.top_lines[i].loading_post;
      const color = loading > 2 ? '#7c0d6c' : (loading > 1.3 ? '#cc2222' : '#e9a035');
      L.polyline([[g[0],g[1]],[g[2],g[3]]], {color, weight: 4, opacity:.9})
        .bindPopup(`line ${H.top_lines[i].id} ${g[4]}kV · ${pct(loading*100,0)} loading · ${H.top_lines[i].flow_post_MW} MW`)
        .addTo(map);
    });
    map.fitBounds([[Math.min(...lats), Math.min(...lons)],[Math.max(...lats), Math.max(...lons)]], {padding:[20,20]});
  }, 50);
});

</script></body></html>
"""

if __name__ == "__main__":
    main()
