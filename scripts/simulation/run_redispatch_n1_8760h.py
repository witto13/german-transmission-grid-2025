#!/usr/bin/env python3
"""run_redispatch_n1_8760h.py — Stage-2 (N-0 + N-1 security) redispatch.

German redispatch is run to keep the grid secure at BOTH the base case (N-0) and
after any single credible contingency (N-1). This is the second pass on top of
the economic day-ahead redispatch (`run_redispatch_8760h.py`):

    Stage 1 (unchanged): economic §13(1) redispatch, soft 500 €/MWh slack.
    Stage 2 (here)     : security redispatch starting from the stage-1 dispatch.
                         Objective = RESOLVE the bottleneck (prohibitive slack),
                         enforcing N-0 on every overloaded line AND N-1 on a
                         curated set of ~200 critical/long lines (≈20 per TSO and
                         big DSO) via LODF contingency flows.

Inputs:
    results/dispatch_8760h_pf.nc                (topology, gen p_da, headroom ts)
    results/redispatch_8760h_deltas_hourly.npz  (stage-1 Δp + post-stage-1 flows)

Outputs:
    results/redispatch_n1_8760h_summary.json
    results/redispatch_n1_deltas_hourly.npz     (report-compatible: total Δp,
                                                 line_flow_da, line_flow_post=final)
    results/redispatch_n1_critical_lines.csv    (the N-1 monitored/contingency set)
    results/redispatch_n1_8760h.nc              (optional, --save-nc)

Usage:
    conda activate egon2025
    python scripts/simulation/run_redispatch_n1_8760h.py [--workers N] [--snapshots K]
"""
import argparse
import json
import logging
import multiprocessing as mp
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa
from sqlalchemy import create_engine

from _redispatch_core import (
    build_precomp, compute_lodf, solve_snapshot_n1, RES_CARRIERS, KWK_CARRIERS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

DB_URL = "postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data"
RES = Path("/root/egon_2025_project/results")
PF_NC = RES / "dispatch_8760h_pf.nc"
S1_NPZ = RES / "redispatch_8760h_deltas_hourly.npz"

# State → single TSO control zone (resolve the ambiguous combos to the dominant op).
BL_TO_TSO = {
    "Berlin": "50Hertz", "Brandenburg": "50Hertz", "Mecklenburg-Vorpommern": "50Hertz",
    "Sachsen": "50Hertz", "Sachsen-Anhalt": "50Hertz", "Thüringen": "50Hertz",
    "Hamburg": "50Hertz", "Bayern": "TenneT", "Niedersachsen": "TenneT", "Bremen": "TenneT",
    "Schleswig-Holstein": "TenneT", "Hessen": "Amprion",
    "Nordrhein-Westfalen": "Amprion", "Rheinland-Pfalz": "Amprion", "Saarland": "Amprion",
    "Baden-Württemberg": "TransnetBW",
}
# State → big DSO proxy (for 110 kV contingencies).
BL_TO_DSO = {
    "Nordrhein-Westfalen": "Westnetz", "Rheinland-Pfalz": "Westnetz", "Saarland": "Westnetz",
    "Bayern": "Bayernwerk", "Baden-Württemberg": "NetzeBW",
    "Niedersachsen": "Avacon", "Bremen": "Avacon", "Sachsen-Anhalt": "Avacon",
    "Schleswig-Holstein": "SH-Netz", "Hamburg": "SH-Netz",
    "Brandenburg": "E.DIS", "Mecklenburg-Vorpommern": "E.DIS", "Berlin": "E.DIS",
    "Sachsen": "MITNETZ", "Thüringen": "MITNETZ", "Hessen": "Syna",
}
PER_ZONE = 20          # ~20 critical lines per TSO / big DSO
MAX_CRIT = 200         # hard cap on the N-1 set
V_FACTOR = {380.0: 3.0, 220.0: 2.0, 110.0: 1.0}


def line_states(line_ids):
    """bus0 Bundesland for each line id (via PostGIS), as a dict line_id->state."""
    eng = create_engine(DB_URL)
    sql = """
        SELECT l.line_id::text AS line_id, lan.gen AS state
        FROM grid.egon_etrago_line l
        JOIN grid.egon_etrago_bus b ON b.bus_id = l.bus0 AND b.scn_name='grid_beta'
        LEFT JOIN boundaries.vg250_lan lan
               ON ST_Contains(lan.geometry, b.geom) AND lan.gf = 4
        WHERE l.scn_name='grid_beta'
    """
    df = pd.read_sql(sql, eng).drop_duplicates("line_id").set_index("line_id")
    return df["state"].fillna("").to_dict()


def select_critical_lines(n, sub_line_ids, max_loading):
    """Pick ≤200 critical+long lines (≈20 per TSO and big DSO) for N-1.

    Score = voltage_factor × length_km × (0.5 + max base-case loading). Lines are
    grouped into 4 TSO zones (220/380 kV) and big-DSO proxies (110 kV); top
    PER_ZONE per group, then globally capped at MAX_CRIT by score.
    """
    lines = n.lines.loc[sub_line_ids].copy()
    lines["v_nom"] = n.buses.loc[lines["bus0"].values, "v_nom"].values
    load_by_id = pd.Series(max_loading, index=sub_line_ids)
    lines["maxload"] = load_by_id.reindex(lines.index).fillna(0.0).values
    states = line_states(sub_line_ids)
    lines["state"] = [states.get(str(i), "") for i in lines.index]
    lines = lines[lines["s_nom"] > 0].copy()
    lines["vf"] = lines["v_nom"].map(V_FACTOR).fillna(1.0)
    lines["score"] = lines["vf"] * lines["length"].clip(lower=0.1) * (0.5 + lines["maxload"])

    def zone(row):
        if row["v_nom"] >= 220:
            return "TSO:" + BL_TO_TSO.get(row["state"], "Unknown")
        return "DSO:" + BL_TO_DSO.get(row["state"], "Unknown")
    lines["zone"] = lines.apply(zone, axis=1)
    lines = lines[~lines["zone"].str.endswith("Unknown")]

    picks = []
    for z, grp in lines.groupby("zone"):
        picks.append(grp.sort_values("score", ascending=False).head(PER_ZONE))
    sel = pd.concat(picks).sort_values("score", ascending=False).head(MAX_CRIT)
    return sel


def _worker(snap_indices):
    pre = _W["pre"]; base_gen = _W["base_gen"]; pmax = _W["pmax"]; pmin = _W["pmin"]
    base_link = _W["base_link"]; base_flow = _W["base_flow"]
    crit_idx = _W["crit_idx"]; crit_ptdf = _W["crit_ptdf"]; lodf = _W["lodf"]; lvc = _W["lvc"]
    mm_dg = _W["mm_dg"]; mm_nf = _W["mm_nf"]; s_nom = pre.s_nom
    n_link = len(pre.link_ids); n_pst = len(pre.pst_ids)
    out_link, out_pst, out_cost, out_n0, out_n1, out_slack, out_resid = [], [], [], [], [], [], []
    for t in snap_indices:
        r = solve_snapshot_n1(
            pre, gen_p_base=base_gen[t], gen_p_max=pmax[t], gen_p_min=pmin[t],
            link_p_base=base_link[t], line_flow_base=base_flow[t],
            crit_idx=crit_idx, crit_ptdf=crit_ptdf, lodf=lodf, lodf_valid_c=lvc,
        )
        out_n0.append(r.n_binding_n0); out_n1.append(r.n_binding_n1)
        out_slack.append(r.slack_mw)
        if (r.n_binding_n0 == 0 and r.n_binding_n1 == 0) or not r.feasible:
            out_link.append(np.zeros(n_link, np.float32)); out_pst.append(np.zeros(n_pst, np.float32))
            out_cost.append(0.0); out_resid.append(False); continue
        mm_dg[t] = r.delta_p_gen; mm_nf[t] = r.new_line_flows
        out_link.append(r.delta_p_link); out_pst.append(r.delta_p_pst); out_cost.append(r.cost)
        out_resid.append(bool((np.abs(r.new_line_flows) > s_nom * 1.001).any()))
    return (snap_indices, np.array(out_link, np.float32), np.array(out_pst, np.float32),
            np.array(out_cost, np.float32), np.array(out_n0, np.int32), np.array(out_n1, np.int32),
            np.array(out_slack, np.float32), np.array(out_resid, bool))


_W = {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--snapshots", type=int, default=None)
    ap.add_argument("--save-nc", action="store_true", default=False)
    ap.add_argument("--in-pf", default=str(PF_NC))
    ap.add_argument("--in-s1-npz", default=str(S1_NPZ))
    ap.add_argument("--out-summary", default=None)
    ap.add_argument("--out-npz", default=None)
    args = ap.parse_args()

    log.info(f"Loading {args.in_pf}")
    n = pypsa.Network(args.in_pf)
    if args.snapshots:
        n.set_snapshots(n.snapshots[:args.snapshots])
    T = len(n.snapshots)
    # data cleaning + per-unit (must match the PTDF build)
    n.lines["x"] = n.lines["x"].clip(lower=0.05)
    if os.environ.get("APPLY_TOPOLOGY_FIXES", "0") == "1":
        from _topology_fix import apply_fixes
        apply_fixes(n)
    if os.environ.get("APPLY_GEN_VOLTAGE_FIX", "0") == "1":
        from _gen_voltage_fix import apply_gen_voltage_rule
        apply_gen_voltage_rule(n)
    n.calculate_dependent_values()

    pre = build_precomp(n)
    sub_line_ids = pre.sub_line_ids
    L_sub = pre.s_nom.shape[0]
    line_pos = {l: i for i, l in enumerate(sub_line_ids)}

    # ---- stage-1 results ----
    log.info(f"Loading stage-1 deltas {args.in_s1_npz}")
    z = np.load(args.in_s1_npz, allow_pickle=True)
    s1_gen_ids = z["gen_ids"]; s1_dg = z["delta_gen"]                 # (T,G)
    s1_link = z["delta_link"]; s1_sub = z["sub_line_ids"]
    s1_flow_post = z["line_flow_post"]; s1_flow_da = z["line_flow_da"]
    if args.snapshots:
        s1_dg = s1_dg[:T]; s1_link = s1_link[:T]
        s1_flow_post = s1_flow_post[:T]; s1_flow_da = s1_flow_da[:T]
    # align gens / lines (same network → should match; reindex defensively)
    assert list(s1_gen_ids) == list(pre.gen_ids), "gen id order mismatch stage1↔stage2"
    if list(s1_sub) != list(sub_line_ids):
        remap = np.array([line_pos[l] for l in s1_sub])
        tmp_post = np.zeros((T, L_sub), np.float32); tmp_post[:, remap] = s1_flow_post
        tmp_da = np.zeros((T, L_sub), np.float32); tmp_da[:, remap] = s1_flow_da
        s1_flow_post, s1_flow_da = tmp_post, tmp_da

    # ---- generator headroom arrays (same as stage 1) ----
    from run_redispatch_8760h import precompute_gen_arrays
    p_da, p_max, p_min = precompute_gen_arrays(n, pre.gen_ids)
    base_gen = (p_da + s1_dg).astype(np.float32)            # dispatch after stage 1
    p_max = np.maximum(p_max, base_gen)
    p_min = np.minimum(p_min, base_gen)
    # link base after stage 1 (stage-1 DA link flow was 0 → base = Δp1_link)
    base_link = s1_link.astype(np.float32)
    base_flow = s1_flow_post.astype(np.float32)             # base-case flows after stage 1

    # ---- critical line set ----
    max_loading = np.abs(s1_flow_da).max(axis=0) / np.where(pre.s_nom > 0, pre.s_nom, np.inf)
    log.info("Selecting critical N-1 lines...")
    sel = select_critical_lines(n, sub_line_ids, max_loading)
    crit_ids = list(sel.index)
    crit_idx = np.array([line_pos[i] for i in crit_ids], dtype=np.int64)
    log.info(f"  Selected {len(crit_idx)} critical lines. Zone counts:\n"
             + sel["zone"].value_counts().to_string())
    sel_out = sel[["v_nom", "length", "s_nom", "maxload", "state", "zone", "score"]].copy()
    sel_out.to_csv(RES / "redispatch_n1_critical_lines.csv")

    # ---- LODF ----
    crit_ptdf = pre.ptdf[crit_idx, :].astype(np.float32)
    crit_b0 = np.array([pre.bus_idx_in_sub[b] for b in n.lines.loc[crit_ids, "bus0"]])
    crit_b1 = np.array([pre.bus_idx_in_sub[b] for b in n.lines.loc[crit_ids, "bus1"]])
    lodf, lvc = compute_lodf(crit_ptdf, crit_b0, crit_b1)
    log.info(f"  LODF {lodf.shape}, {int(lvc.sum())}/{len(lvc)} contingencies valid (non-radial).")

    # ---- outputs / memmaps ----
    G = len(pre.gen_ids)
    mm_dg = np.memmap(RES / ".n1_dg.mmap", dtype=np.float32, mode="w+", shape=(T, G)); mm_dg[:] = 0
    mm_nf = np.memmap(RES / ".n1_nf.mmap", dtype=np.float32, mode="w+", shape=(T, L_sub))
    mm_nf[:] = base_flow
    dl2 = np.zeros((T, len(pre.link_ids)), np.float32)
    dp2 = np.zeros((T, len(pre.pst_ids)), np.float32)
    cost = np.zeros(T, np.float32); n0 = np.zeros(T, np.int32); n1 = np.zeros(T, np.int32)
    slack = np.zeros(T, np.float32); resid = np.zeros(T, bool)

    _W.update(pre=pre, base_gen=base_gen, pmax=p_max, pmin=p_min, base_link=base_link,
              base_flow=base_flow, crit_idx=crit_idx, crit_ptdf=crit_ptdf, lodf=lodf, lvc=lvc,
              mm_dg=mm_dg, mm_nf=mm_nf)

    log.info(f"\nStage-2 N-0+N-1 LPs for {T} snapshots, {args.workers} workers...")
    t0 = time.time()
    chunk = max(1, T // (args.workers * 8))
    chunks = [list(range(s, min(s + chunk, T))) for s in range(0, T, chunk)]
    done = 0
    ctx = mp.get_context("fork")
    with ctx.Pool(args.workers) as pool:
        for idxs, lk, ps, cs, b0, b1, sl, rs in pool.imap_unordered(_worker, chunks):
            idxs = np.asarray(idxs)
            dl2[idxs] = lk; dp2[idxs] = ps; cost[idxs] = cs
            n0[idxs] = b0; n1[idxs] = b1; slack[idxs] = sl; resid[idxs] = rs
            done += len(idxs); el = time.time() - t0
            log.info(f"  [{done:5d}/{T}] n0_hrs={int((n0>0).sum())} n1_hrs={int((n1>0).sum())} "
                     f"resid_hrs={int(resid.sum())} el={el:.0f}s eta={el/max(done,1)*(T-done):.0f}s")
    mm_dg.flush(); mm_nf.flush()
    log.info(f"Stage-2 loop done in {time.time()-t0:.0f}s")

    # ---- totals (stage1 + stage2) ----
    dg_total = (s1_dg + np.asarray(mm_dg)).astype(np.float32)
    up = float(np.maximum(dg_total, 0).sum() / 1e6)
    dn = float(-np.minimum(dg_total, 0).sum() / 1e6)
    res_mask = np.array([c in RES_CARRIERS for c in pre.gen_carrier])
    kwk_mask = np.array([c in KWK_CARRIERS for c in pre.gen_carrier])
    res_dn = float(-np.minimum(dg_total[:, res_mask], 0).sum() / 1e6)
    conv_mask = ~(res_mask | kwk_mask)
    conv_up = float(np.maximum(dg_total[:, conv_mask], 0).sum() / 1e6)
    # stage-2-only volumes
    s2 = np.asarray(mm_dg)
    s2_up = float(np.maximum(s2, 0).sum() / 1e6); s2_dn = float(-np.minimum(s2, 0).sum() / 1e6)
    s2_res_dn = float(-np.minimum(s2[:, res_mask], 0).sum() / 1e6)
    # Real monetary redispatch cost ≈ Σ up-ramp × marginal cost (money paid to ramp expensive
    # plants up). Excludes the prohibitive line-slack penalty, which is NOT a real cost but a
    # flag for structurally insecure elements no redispatch can fix.
    real_cost_MEUR = float((np.maximum(dg_total, 0) * pre.gen_mc[None, :]).sum() / 1e6)

    final_flow = np.asarray(mm_nf)
    over_final = int(((np.abs(final_flow) > pre.s_nom * 1.001).any(axis=1)).sum())
    lh_final = int((np.abs(final_flow) > pre.s_nom * 1.001).sum())
    lh_s1 = int((np.abs(base_flow) > pre.s_nom * 1.001).sum())

    log.info("\n--- Two-stage redispatch volumes (TWh) ---")
    log.info(f"  Total ramp-up   {up:.2f}  ramp-down {dn:.2f}  RES curtailed {res_dn:.2f}  conv up {conv_up:.2f}")
    log.info(f"  Stage-2 only: up {s2_up:.2f}  dn {s2_dn:.2f}  RES curtailed {s2_res_dn:.2f}")
    log.info(f"  Overloaded line-hours: stage1 {lh_s1} -> final {lh_final}")
    log.info(f"  Hours with N-1 binding: {int((n1>0).sum())}; residual-overload hrs (N-0): {over_final}")

    # ---- save report-compatible npz (final state) + N-1 extras ----
    npz = Path(args.out_npz) if args.out_npz else (RES / "redispatch_n1_deltas_hourly.npz")
    np.savez_compressed(
        npz,
        snapshots=np.array([str(s) for s in n.snapshots]),
        gen_ids=pre.gen_ids, gen_carrier=pre.gen_carrier, sub_line_ids=np.array(sub_line_ids),
        delta_gen=dg_total,                       # total Δ (stage1+2) — report-compatible
        delta_gen_stage2=s2,
        delta_link=(s1_link + dl2), delta_pst=dp2,
        line_flow_da=s1_flow_da,                  # original copperplate DC flow
        line_flow_stage1=base_flow,               # after stage 1
        line_flow_post=final_flow,                # after stage 2 (final)
        s_nom=pre.s_nom,
        crit_idx=crit_idx, crit_ids=np.array(crit_ids),
        n_binding=(n0 + n1),                      # report uses this as 'binding'
        n_binding_n0=n0, n_binding_n1=n1, slack_mw=slack, cost=cost,
    )
    log.info(f"Saved {npz}")

    summary = {
        "scenario": "grid_beta", "snapshots": T,
        "stage": "N-0 + N-1 security (stage 2 on top of stage 1)",
        "n_critical_lines": int(len(crit_idx)),
        "hours_n0_binding_pre_stage2": int((n0 > 0).sum()),
        "hours_n1_binding": int((n1 > 0).sum()),
        "hours_residual_overload_post": over_final,
        "overloaded_line_hours_stage1": lh_s1,
        "overloaded_line_hours_final": lh_final,
        "total_ramp_up_TWh": up, "total_ramp_down_TWh": dn,
        "RES_curtailed_TWh": res_dn, "conventional_ramped_up_TWh": conv_up,
        "stage2_ramp_up_TWh": s2_up, "stage2_ramp_down_TWh": s2_dn,
        "stage2_RES_curtailed_TWh": s2_res_dn,
        "real_redispatch_cost_MEUR": real_cost_MEUR,
        "lp_objective_with_slack_MEUR": float(cost.sum() / 1e6),
        "residual_slack_MWsum": float(slack.sum()),
        "note": ("residual_slack flags structurally insecure elements (radial 110kV pockets / "
                 "N-1-insecure corridors) no redispatch can resolve — needs grid expansion. "
                 "lp_objective is slack-laden and NOT a money figure; use real_redispatch_cost."),
        "BNetzA_2024_benchmark_TWh": 30.3, "BNetzA_2024_benchmark_RES_TWh": 9.4,
        "BNetzA_2024_benchmark_cost_MEUR": 2776,
    }
    sum_path = Path(args.out_summary) if args.out_summary else (RES / "redispatch_n1_8760h_summary.json")
    sum_path.write_text(json.dumps(summary, indent=2))
    log.info(json.dumps(summary, indent=2))

    if args.save_nc:
        log.info("Applying totals to network and saving netCDF...")
        new_p = (p_da + dg_total).astype(np.float32)
        n.generators_t.p = pd.DataFrame(new_p, index=n.snapshots, columns=pre.gen_ids)
        nl = pd.DataFrame(np.zeros((T, len(n.lines)), np.float32), index=n.snapshots, columns=n.lines.index)
        nl[sub_line_ids] = final_flow
        n.lines_t.p0 = nl; n.lines_t.p1 = -nl
        n.export_to_netcdf(str(RES / "redispatch_n1_8760h.nc"))

    for p in [RES / ".n1_dg.mmap", RES / ".n1_nf.mmap"]:
        try:
            del mm_dg, mm_nf
        except Exception:
            pass
        Path(p).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
