#!/usr/bin/env python3
"""run_combined_8760h.py — Single-stage Redispatch 2.0 LP.

One LP per hour that:
  • Hard-constrains EVERY currently-overloaded line (N-0, uncapped).
  • Dynamically picks contingencies = every line currently loaded > 40 %
    (capped at 800 for safety; near-radial excluded).
  • Monitors ALL lines for N-1 violations against those contingencies.
  • Uses prohibitive slack (1e6 €/MWh) so residual slack only flags
    physically-infeasible constraints.
  • Costs follow Redispatch 2.0: conventional MC + RES Mindestfaktor ×10,
    KWK ×5, RES priced at opportunity cost (wind 85, solar 45, offwind 110).

Inputs (with APPLY_GEN_VOLTAGE_FIX baked into the PF):
  results/dispatch_year_genreloc_v2_pf.nc

Outputs:
  results/redispatch_combined_8760h.npz       (hourly Δp, flows, slack, etc.)
  results/redispatch_combined_8760h_summary.json
"""
import argparse, json, logging, multiprocessing as mp, os, time
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

from _redispatch_core import (
    build_precomp, build_line_endpoint_cols, solve_snapshot_combined,
    RES_CARRIERS, KWK_CARRIERS,
)
from run_redispatch_8760h import precompute_gen_arrays

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

RES = Path("/root/egon_2025_project/results")
DEFAULT_IN_PF = RES / "dispatch_year_genreloc_v2_pf.nc"

_W = {}


def _worker(snap_indices):
    pre = _W["pre"]
    p_da_all = _W["p_da_all"]; p_max_all = _W["p_max_all"]; p_min_all = _W["p_min_all"]
    link_p_da_all = _W["link_p_da_all"]; line_flow_all = _W["line_flow_all"]
    mm_dg = _W["mm_dg"]; mm_nf = _W["mm_nf"]
    out_link, out_pst, out_cost, out_n0, out_n1, out_slack, out_cont = [], [], [], [], [], [], []
    n_link = len(pre.link_ids); n_pst = len(pre.pst_ids)
    for t in snap_indices:
        r = solve_snapshot_combined(
            pre, p_da_all[t], p_max_all[t], p_min_all[t],
            link_p_da_all[t], line_flow_all[t],
        )
        out_n0.append(r.n_binding_n0); out_n1.append(r.n_binding_n1)
        out_slack.append(r.slack_mw); out_cont.append(r.n_contingencies)
        if (r.n_binding_n0 == 0 and r.n_binding_n1 == 0) or not r.feasible:
            out_link.append(np.zeros(n_link, np.float32))
            out_pst.append(np.zeros(n_pst, np.float32))
            out_cost.append(0.0); continue
        mm_dg[t] = r.delta_p_gen; mm_nf[t] = r.new_line_flows
        out_link.append(r.delta_p_link); out_pst.append(r.delta_p_pst); out_cost.append(r.cost)
    return (snap_indices,
            np.array(out_link, np.float32), np.array(out_pst, np.float32),
            np.array(out_cost, np.float32), np.array(out_n0, np.int32),
            np.array(out_n1, np.int32), np.array(out_slack, np.float32),
            np.array(out_cont, np.int32))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-pf", default=str(DEFAULT_IN_PF))
    ap.add_argument("--out-summary", default=str(RES / "redispatch_combined_8760h_summary.json"))
    ap.add_argument("--out-npz", default=str(RES / "redispatch_combined_8760h.npz"))
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--snapshots", type=int, default=None)
    args = ap.parse_args()

    t0 = time.time()
    log.info(f"Loading {args.in_pf}")
    n = pypsa.Network(args.in_pf)
    n.lines["x"] = n.lines["x"].clip(lower=0.05)
    if os.environ.get("APPLY_GEN_VOLTAGE_FIX", "0") == "1":
        from _gen_voltage_fix import apply_gen_voltage_rule
        apply_gen_voltage_rule(n)
    n.calculate_dependent_values()
    if args.snapshots:
        n.set_snapshots(n.snapshots[:args.snapshots])
    T = len(n.snapshots)

    pre = build_precomp(n)
    build_line_endpoint_cols(pre, n)
    sub_line_ids = pre.sub_line_ids
    L_sub = pre.s_nom.shape[0]
    gen_ids = pre.gen_ids
    G = len(gen_ids)

    line_flow_all = n.lines_t.p0[sub_line_ids].values.astype(np.float32)
    log.info(f"  DA flows shape {line_flow_all.shape}")

    p_da_all, p_max_all, p_min_all = precompute_gen_arrays(n, gen_ids)
    log.info(f"  Gen p_da annual {p_da_all.sum()/1e6:.1f} TWh")

    link_ids = pre.link_ids
    link_p_da_all = np.zeros((T, len(link_ids)), np.float32)
    if hasattr(n.links_t, "p0") and n.links_t.p0 is not None and len(n.links_t.p0.columns):
        for k, lid in enumerate(link_ids):
            if lid in n.links_t.p0.columns:
                link_p_da_all[:, k] = n.links_t.p0[lid].values.astype(np.float32)

    mm_dg_path = str(RES / ".combined_dg.mmap")
    mm_nf_path = str(RES / ".combined_nf.mmap")
    delta_gen_all = np.memmap(mm_dg_path, dtype=np.float32, mode="w+", shape=(T, G)); delta_gen_all[:] = 0
    new_flow_all = np.memmap(mm_nf_path, dtype=np.float32, mode="w+", shape=(T, L_sub))
    new_flow_all[:] = line_flow_all
    delta_link_all = np.zeros((T, len(link_ids)), np.float32)
    delta_pst_all = np.zeros((T, len(pre.pst_ids)), np.float32)
    cost_per_h = np.zeros(T, np.float32)
    n0_per_h = np.zeros(T, np.int32)
    n1_per_h = np.zeros(T, np.int32)
    slack_per_h = np.zeros(T, np.float32)
    n_cont_per_h = np.zeros(T, np.int32)

    _W.update(pre=pre, p_da_all=p_da_all, p_max_all=p_max_all, p_min_all=p_min_all,
              link_p_da_all=link_p_da_all, line_flow_all=line_flow_all,
              mm_dg=delta_gen_all, mm_nf=new_flow_all)

    log.info(f"\nSingle-stage combined LP for {T} snapshots, {args.workers} workers...")
    tl = time.time()
    chunk = max(1, T // (args.workers * 8))
    chunks = [list(range(s, min(s + chunk, T))) for s in range(0, T, chunk)]
    done = 0
    ctx = mp.get_context("fork")
    with ctx.Pool(args.workers) as pool:
        for idxs, lk, ps, cs, b0, b1, sl, nc in pool.imap_unordered(_worker, chunks):
            idxs = np.asarray(idxs)
            delta_link_all[idxs] = lk; delta_pst_all[idxs] = ps
            cost_per_h[idxs] = cs; n0_per_h[idxs] = b0; n1_per_h[idxs] = b1
            slack_per_h[idxs] = sl; n_cont_per_h[idxs] = nc
            done += len(idxs); el = time.time() - tl
            log.info(f"  [{done:5d}/{T}] n0_hrs={int((n0_per_h>0).sum())} "
                     f"n1_hrs={int((n1_per_h>0).sum())} "
                     f"avg_cont={n_cont_per_h[:done+1].mean():.0f} "
                     f"el={el:.0f}s eta={el/max(done,1)*(T-done):.0f}s")
    delta_gen_all.flush(); new_flow_all.flush()

    s_nom = pre.s_nom
    sn = np.where(s_nom > 0, s_nom, np.inf)
    final = np.asarray(new_flow_all)
    da = line_flow_all
    res_mask = np.array([c in RES_CARRIERS for c in pre.gen_carrier])
    kwk_mask = np.array([c in KWK_CARRIERS for c in pre.gen_carrier])
    conv_mask = ~(res_mask | kwk_mask)

    dg_all = np.asarray(delta_gen_all)
    up = float(np.maximum(dg_all, 0).sum() / 1e6)
    dn = float(-np.minimum(dg_all, 0).sum() / 1e6)
    res_dn = float(-np.minimum(dg_all[:, res_mask], 0).sum() / 1e6)
    res_up = float(np.maximum(dg_all[:, res_mask], 0).sum() / 1e6)
    conv_up = float(np.maximum(dg_all[:, conv_mask], 0).sum() / 1e6)
    conv_dn = float(-np.minimum(dg_all[:, conv_mask], 0).sum() / 1e6)

    wind_dn = float(-np.minimum(dg_all[:, np.isin(pre.gen_carrier, ["onwind","offwind"])], 0).sum() / 1e6)
    solar_dn = float(-np.minimum(dg_all[:, np.isin(pre.gen_carrier, ["solar"])], 0).sum() / 1e6)

    over_da_lh = int((np.abs(da) > sn * 1.001).sum())
    over_post_lh = int((np.abs(final) > sn * 1.001).sum())
    hours_over_da = int(((np.abs(da) > sn).any(axis=1)).sum())
    hours_over_post = int(((np.abs(final) > sn).any(axis=1)).sum())
    real_cost = float((np.maximum(dg_all, 0) * pre.gen_mc[None, :]).sum() / 1e6)

    summary = {
        "scenario": "grid_beta + gen_voltage_v2 + single-stage combined LP",
        "snapshots": T,
        "n_contingencies_avg": float(n_cont_per_h.mean()),
        "n_contingencies_max": int(n_cont_per_h.max()),
        "hrs_overload_da": hours_over_da,
        "hrs_overload_post": hours_over_post,
        "hrs_n1_binding": int((n1_per_h > 0).sum()),
        "overloaded_line_hours_da": over_da_lh,
        "overloaded_line_hours_post": over_post_lh,
        "total_ramp_up_TWh": round(up, 3),
        "total_ramp_down_TWh": round(dn, 3),
        "RES_curtailed_TWh": round(res_dn, 3),
        "RES_ramped_up_TWh": round(res_up, 3),
        "wind_curtailed_TWh": round(wind_dn, 3),
        "solar_curtailed_TWh": round(solar_dn, 3),
        "conventional_ramped_up_TWh": round(conv_up, 3),
        "conventional_ramped_down_TWh": round(conv_dn, 3),
        "real_redispatch_cost_MEUR": round(real_cost, 1),
        "lp_objective_total_MEUR": float(cost_per_h.sum() / 1e6),
        "residual_slack_MWsum": float(slack_per_h.sum()),
        "BNetzA_2024": {"total_TWh": 30.3, "RES_TWh": 5.95, "wind_TWh": 4.56,
                        "solar_TWh": 1.39, "cost_MEUR": 2776},
    }
    Path(args.out_summary).write_text(json.dumps(summary, indent=2))
    log.info(json.dumps(summary, indent=2))

    np.savez_compressed(
        args.out_npz,
        snapshots=np.array([str(s) for s in n.snapshots]),
        gen_ids=pre.gen_ids, gen_carrier=pre.gen_carrier,
        sub_line_ids=np.array(sub_line_ids),
        delta_gen=dg_all, delta_link=delta_link_all, delta_pst=delta_pst_all,
        line_flow_da=da, line_flow_post=final,
        s_nom=pre.s_nom,
        n_binding=(n0_per_h + n1_per_h),
        n_binding_n0=n0_per_h, n_binding_n1=n1_per_h,
        slack_mw=slack_per_h, cost=cost_per_h, n_cont=n_cont_per_h,
    )
    log.info(f"Saved {args.out_npz}")

    for p in [mm_dg_path, mm_nf_path]:
        Path(p).unlink(missing_ok=True)
    log.info(f"\nDone in {time.time()-t0:.0f}s.")


if __name__ == "__main__":
    main()
