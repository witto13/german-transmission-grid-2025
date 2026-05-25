#!/usr/bin/env python3
"""run_sample_12weeks.py — Quality-check 1-week-per-month sample (≈2016 h).

Subsets the dispatch netCDF to a 12-week-per-year sample (one week per calendar
month), runs the full pipeline in-memory (DC PF → stage-1 economic redispatch →
stage-2 N-0+N-1 security redispatch), and reports headline metrics. Toggle
topology fixes via env var APPLY_TOPOLOGY_FIXES=1.

Output: results/sample12w_summary_<tag>.json where tag = "raw" or "fixed".
"""
import argparse
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

from _redispatch_core import (
    build_precomp, solve_snapshot, solve_snapshot_n1, compute_lodf,
    RES_CARRIERS, KWK_CARRIERS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

RES = Path("/root/egon_2025_project/results")
PF_NC_FULL = RES / "dispatch_8760h.nc"           # the unconstrained-dispatch (input to PF)


def sample_snapshot_indices(snapshots):
    """Return indices of one week per calendar month (Monday 00:00 → Sunday 23:00)."""
    s = pd.to_datetime(snapshots)
    idx = []
    for m in range(1, 13):
        in_m = s[s.month == m]
        if len(in_m) == 0:
            continue
        # find the first Monday in month m
        mondays = in_m[in_m.weekday == 0]
        start = mondays[0] if len(mondays) else in_m[0]
        end = start + pd.Timedelta(hours=168)
        mask = (s >= start) & (s < end)
        idx.extend(np.where(mask)[0].tolist())
    return np.array(sorted(set(idx)), dtype=int)


def compute_injection(n, snap_idx):
    """(T_sample, B) net injection MW for sampled snapshots."""
    log.info("Computing per-bus injection on sampled snapshots...")
    bus_ids = list(n.buses.index)
    bus_idx = {b: i for i, b in enumerate(bus_ids)}
    B = len(bus_ids); T = len(snap_idx)
    inj = np.zeros((T, B), dtype=np.float32)

    p_gen = n.generators_t.p
    if p_gen is not None and len(p_gen.columns):
        gb = n.generators.loc[p_gen.columns, "bus"].map(bus_idx).values
        sub = p_gen.iloc[snap_idx].values.astype(np.float32)
        for j, col_idx in enumerate(gb):
            inj[:, col_idx] += sub[:, j]
    p_load = n.loads_t.p if (n.loads_t.p is not None and not n.loads_t.p.empty) else n.loads_t.p_set
    lb = n.loads.loc[p_load.columns, "bus"].map(bus_idx).values
    sub = p_load.iloc[snap_idx].values.astype(np.float32)
    for j, col_idx in enumerate(lb):
        inj[:, col_idx] -= sub[:, j]
    pdis = n.storage_units_t.p_dispatch; pst = n.storage_units_t.p_store
    if pdis is not None and not pdis.empty:
        sb = n.storage_units.loc[pdis.columns, "bus"].map(bus_idx).values
        dsub = pdis.iloc[snap_idx].values.astype(np.float32)
        ssub = pst.iloc[snap_idx].values.astype(np.float32) if (pst is not None and not pst.empty) else 0
        for j, col_idx in enumerate(sb):
            inj[:, col_idx] += dsub[:, j] - (ssub[:, j] if not np.isscalar(ssub) else 0)
    return inj, bus_ids


def line_flows_da(pre, inj, bus_ids):
    """Apply PTDF to balanced injection → line_flow_da (T, L_sub)."""
    sub_cols = np.array([bus_ids.index(b) for b in pre.sub_bus_ids])
    inj_sub = inj[:, sub_cols].copy()
    imb = inj_sub.sum(axis=1)
    inj_sub -= imb[:, None] / inj_sub.shape[1]
    log.info(f"  injection imbalance: mean={imb.mean():.0f} MW max|abs|={np.abs(imb).max():.0f} MW")
    return (inj_sub @ pre.ptdf.T).astype(np.float32)


def precompute_gen_arrays(n, gen_ids, snap_idx):
    p_nom = n.generators.loc[gen_ids, "p_nom"].values.astype(np.float32)
    pmax_s = n.generators.loc[gen_ids, "p_max_pu"].values.astype(np.float32)
    pmin_s = n.generators.loc[gen_ids, "p_min_pu"].values.astype(np.float32)
    T = len(snap_idx); G = len(gen_ids)
    p_max = np.broadcast_to(p_nom * pmax_s, (T, G)).astype(np.float32).copy()
    p_min = np.broadcast_to(p_nom * pmin_s, (T, G)).astype(np.float32).copy()
    pmax_t = n.generators_t.p_max_pu
    if pmax_t is not None and len(pmax_t.columns):
        cols = pmax_t.columns.intersection(gen_ids)
        if len(cols):
            sub = pmax_t.iloc[snap_idx][cols].values.astype(np.float32)
            sel = pd.Series(range(G), index=gen_ids).reindex(cols).values
            p_max[:, sel] = sub * p_nom[sel]
    pmin_t = n.generators_t.p_min_pu
    if pmin_t is not None and len(pmin_t.columns):
        cols = pmin_t.columns.intersection(gen_ids)
        if len(cols):
            sub = pmin_t.iloc[snap_idx][cols].values.astype(np.float32)
            sel = pd.Series(range(G), index=gen_ids).reindex(cols).values
            p_min[:, sel] = sub * p_nom[sel]
    p_da = n.generators_t.p.iloc[snap_idx][gen_ids].values.astype(np.float32)
    p_max = np.maximum(p_max, p_da); p_min = np.minimum(p_min, p_da)
    return p_da, p_max, p_min


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="raw", help="tag for output filename")
    args = ap.parse_args()

    t0 = time.time()
    log.info(f"Loading {PF_NC_FULL}")
    n = pypsa.Network(str(PF_NC_FULL))
    log.info(f"  loaded in {time.time()-t0:.0f}s")

    # ---- Apply data cleaning + optional topology fixes ----
    n.lines["x"] = n.lines["x"].clip(lower=0.05)
    apply_fix = os.environ.get("APPLY_TOPOLOGY_FIXES", "0") == "1"
    if apply_fix:
        from _topology_fix import apply_fixes
        apply_fixes(n)
    n.calculate_dependent_values()

    # ---- Sample 12 weeks (1 per month) ----
    snap_idx = sample_snapshot_indices(n.snapshots)
    log.info(f"Sampled {len(snap_idx)} snapshots (one week per month).")

    pre = build_precomp(n)
    sub_line_ids = pre.sub_line_ids
    L_sub = pre.s_nom.shape[0]
    s_nom = pre.s_nom

    inj, bus_ids = compute_injection(n, snap_idx)
    line_flow_da = line_flows_da(pre, inj, bus_ids)

    abs_da = np.abs(line_flow_da)
    sn_safe = np.where(s_nom > 0, s_nom, np.inf)
    load_da = abs_da / sn_safe
    max_da_loading = float(load_da.max())
    n_lines_over_da = int((load_da.max(0) > 1).sum())
    lh_over_da = int((load_da > 1).sum())

    # ---- Stage 1 (economic) redispatch ----
    gen_ids = pre.gen_ids
    p_da, p_max, p_min = precompute_gen_arrays(n, gen_ids, snap_idx)
    T = len(snap_idx)
    link_p_da = np.zeros((T, len(pre.link_ids)), dtype=np.float32)
    if hasattr(n.links_t, "p0") and n.links_t.p0 is not None and len(n.links_t.p0.columns):
        for k, lid in enumerate(pre.link_ids):
            if lid in n.links_t.p0.columns:
                link_p_da[:, k] = n.links_t.p0[lid].iloc[snap_idx].values.astype(np.float32)

    log.info("\nStage-1 redispatch (economic, soft slack)...")
    s1_dg = np.zeros((T, len(gen_ids)), dtype=np.float32)
    s1_link = np.zeros_like(link_p_da)
    s1_flow_post = line_flow_da.copy()
    s1_cost = np.zeros(T, np.float32)
    for t in range(T):
        r = solve_snapshot(pre, p_da[t], p_max[t], p_min[t], link_p_da[t], line_flow_da[t])
        if r.feasible and r.n_binding > 0:
            s1_dg[t] = r.delta_p_gen; s1_link[t] = r.delta_p_link
            s1_flow_post[t] = r.new_line_flows; s1_cost[t] = r.cost

    res_mask = np.array([c in RES_CARRIERS for c in pre.gen_carrier])
    s1_up = float(np.maximum(s1_dg, 0).sum() / 1e6)
    s1_dn = float(-np.minimum(s1_dg, 0).sum() / 1e6)
    s1_res_dn = float(-np.minimum(s1_dg[:, res_mask], 0).sum() / 1e6)
    load_s1 = np.abs(s1_flow_post) / sn_safe
    n_lines_over_s1 = int((load_s1.max(0) > 1).sum())
    lh_over_s1 = int((load_s1 > 1).sum())
    max_s1_loading = float(load_s1.max())

    # ---- Stage 2 (security N-0 + N-1) redispatch ----
    log.info("\nStage-2 N-0+N-1 redispatch (prohibitive slack)...")
    # critical-line set: reuse selector from the production stage-2 orchestrator
    import run_redispatch_n1_8760h as M
    max_loading = abs_da.max(axis=0) / sn_safe
    sel = M.select_critical_lines(n, sub_line_ids, max_loading)
    crit_ids = list(sel.index)
    lp = {l: i for i, l in enumerate(sub_line_ids)}
    crit_idx = np.array([lp[i] for i in crit_ids])
    crit_ptdf = pre.ptdf[crit_idx, :].astype(np.float32)
    cb0 = np.array([pre.bus_idx_in_sub[b] for b in n.lines.loc[crit_ids, "bus0"]])
    cb1 = np.array([pre.bus_idx_in_sub[b] for b in n.lines.loc[crit_ids, "bus1"]])
    lodf, lvc = compute_lodf(crit_ptdf, cb0, cb1)
    log.info(f"  Crit lines: {len(crit_idx)}; LODF valid contingencies: {int(lvc.sum())}/{len(lvc)}")

    base_gen = (p_da + s1_dg).astype(np.float32)
    p_max_s2 = np.maximum(p_max, base_gen)
    p_min_s2 = np.minimum(p_min, base_gen)
    base_link = s1_link.copy()

    s2_dg = np.zeros((T, len(gen_ids)), dtype=np.float32)
    s2_flow_post = s1_flow_post.copy()
    s2_slack = np.zeros(T, np.float32)
    s2_n1 = np.zeros(T, np.int32)
    s2_n0 = np.zeros(T, np.int32)
    t_loop = time.time()
    for t in range(T):
        r = solve_snapshot_n1(pre, base_gen[t], p_max_s2[t], p_min_s2[t],
                              base_link[t], s1_flow_post[t],
                              crit_idx, crit_ptdf, lodf, lvc)
        s2_n0[t] = r.n_binding_n0; s2_n1[t] = r.n_binding_n1; s2_slack[t] = r.slack_mw
        if r.feasible and (r.n_binding_n0 + r.n_binding_n1) > 0:
            s2_dg[t] = r.delta_p_gen; s2_flow_post[t] = r.new_line_flows
        if t % 200 == 0:
            el = time.time() - t_loop
            log.info(f"  [{t+1:4d}/{T}] elapsed {el:.0f}s")

    s2_up = float(np.maximum(s2_dg, 0).sum() / 1e6)
    s2_dn = float(-np.minimum(s2_dg, 0).sum() / 1e6)
    s2_res_dn = float(-np.minimum(s2_dg[:, res_mask], 0).sum() / 1e6)
    load_s2 = np.abs(s2_flow_post) / sn_safe
    n_lines_over_s2 = int((load_s2.max(0) > 1).sum())
    lh_over_s2 = int((load_s2 > 1).sum())
    max_s2_loading = float(load_s2.max())

    # Annual scaling factor (12 weeks → year)
    annual_scale = 8760 / T

    summary = {
        "tag": args.tag,
        "topology_fixes_applied": apply_fix,
        "n_sample_snapshots": int(T),
        "annual_scale_factor": float(annual_scale),
        # baseline (DA copperplate → DC PF)
        "max_loading_da_pct": round(max_da_loading * 100, 1),
        "lines_overloaded_da": n_lines_over_da,
        "overloaded_line_hours_da_sample": lh_over_da,
        "overloaded_line_hours_da_annualized": int(round(lh_over_da * annual_scale)),
        # after stage 1
        "max_loading_after_s1_pct": round(max_s1_loading * 100, 1),
        "lines_overloaded_after_s1": n_lines_over_s1,
        "overloaded_line_hours_after_s1_annualized": int(round(lh_over_s1 * annual_scale)),
        "stage1_ramp_up_TWh_annualized": round(s1_up * annual_scale, 3),
        "stage1_ramp_down_TWh_annualized": round(s1_dn * annual_scale, 3),
        "stage1_RES_curtailed_TWh_annualized": round(s1_res_dn * annual_scale, 3),
        # after stage 2
        "max_loading_after_s2_pct": round(max_s2_loading * 100, 1),
        "lines_overloaded_after_s2": n_lines_over_s2,
        "overloaded_line_hours_after_s2_annualized": int(round(lh_over_s2 * annual_scale)),
        "stage2_ramp_up_TWh_annualized": round(s2_up * annual_scale, 3),
        "stage2_ramp_down_TWh_annualized": round(s2_dn * annual_scale, 3),
        "stage2_RES_curtailed_TWh_annualized": round(s2_res_dn * annual_scale, 3),
        # totals
        "total_ramp_up_TWh_annualized": round((s1_up + s2_up) * annual_scale, 3),
        "total_ramp_down_TWh_annualized": round((s1_dn + s2_dn) * annual_scale, 3),
        "total_RES_curtailed_TWh_annualized": round((s1_res_dn + s2_res_dn) * annual_scale, 3),
        "residual_slack_MW_total_sample": float(s2_slack.sum()),
        "hours_n1_binding_sample": int((s2_n1 > 0).sum()),
        "BNetzA_2024_benchmark_TWh": 30.3,
        "BNetzA_2024_benchmark_RES_TWh": 9.4,
    }
    out = RES / f"sample12w_summary_{args.tag}.json"
    out.write_text(json.dumps(summary, indent=2))
    log.info("\n" + json.dumps(summary, indent=2))
    log.info(f"\nDone in {time.time()-t0:.0f}s. → {out}")


if __name__ == "__main__":
    main()
