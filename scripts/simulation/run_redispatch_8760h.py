#!/usr/bin/env python3
"""run_redispatch_8760h.py — Day-Ahead Redispatch 2.0 on top of grid_beta dispatch.

Reads the saved DC power flow (results/dispatch_8760h_pf.nc), solves a per-hour
redispatch LP for every snapshot whose line loadings exceed s_nom, and writes
post-redispatch generation, line flows and a sidecar JSON summary.

Mechanics:
    1. Load `dispatch_8760h_pf.nc` (post DC PF).
    2. Compute PTDF once on the main connected subnetwork.
    3. Precompute (T, G) DA dispatch / per-gen p_max / p_min arrays.
    4. For each of 8760 snapshots:
         a. Pull DA line flows from `n.lines_t.p0`.
         b. Identify binding lines (|f| > s_nom).
         c. Build redispatch LP with §13a Mindestfaktor (RES ×10, KWK ×5),
            PTDF sensitivity filter ≥ 2 %, HVDC + PSTs as free variables.
         d. Solve via scipy HiGHS, disaggregate Δp to per-gen units.
       (Most snapshots have no binding line → LP skipped.)
    5. Apply all Δp / new flows to the network.
    6. Save `results/redispatch_8760h.nc`, `results/redispatch_8760h_deltas.parquet`
       and `results/redispatch_8760h_summary.json`.

Usage:
    conda activate egon2025
    python scripts/simulation/run_redispatch_8760h.py [--snapshots N] [--out PATH]
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

from _redispatch_core import (
    build_precomp, solve_snapshot, RES_CARRIERS, KWK_CARRIERS,
)

# --------------------------------------------------------------------------- #
# Worker globals (populated in parent before fork; inherited copy-on-write)
# --------------------------------------------------------------------------- #
_W = {}


def _worker_chunk(snap_indices):
    """Solve a contiguous list of snapshots; write big outputs to memmaps.

    Returns compact per-snapshot results for the small arrays.
    """
    pre = _W["pre"]
    p_da_all = _W["p_da_all"]
    p_max_all = _W["p_max_all"]
    p_min_all = _W["p_min_all"]
    link_p_da_all = _W["link_p_da_all"]
    line_flow_all = _W["line_flow_all"]
    mm_delta_gen = _W["mm_delta_gen"]
    mm_new_flow = _W["mm_new_flow"]
    s_nom = pre.s_nom

    n_link = len(pre.link_ids)
    n_pst = len(pre.pst_ids)
    out_link = []
    out_pst = []
    out_cost = []
    out_nbind = []
    out_feasible = []
    out_residual = []
    for t in snap_indices:
        res = solve_snapshot(
            pre,
            gen_p_da=p_da_all[t],
            gen_p_max=p_max_all[t],
            gen_p_min=p_min_all[t],
            link_p_da=link_p_da_all[t],
            line_flow_da=line_flow_all[t],
        )
        out_nbind.append(res.n_binding)
        out_feasible.append(res.feasible and res.n_binding > 0)
        if res.n_binding == 0 or not res.feasible:
            out_link.append(np.zeros(n_link, dtype=np.float32))
            out_pst.append(np.zeros(n_pst, dtype=np.float32))
            out_cost.append(0.0)
            out_residual.append(False)
            continue
        mm_delta_gen[t] = res.delta_p_gen
        mm_new_flow[t] = res.new_line_flows
        out_link.append(res.delta_p_link)
        out_pst.append(res.delta_p_pst)
        out_cost.append(res.cost)
        out_residual.append(bool((np.abs(res.new_line_flows) > s_nom * 1.001).any()))
    return (snap_indices, np.array(out_link, dtype=np.float32),
            np.array(out_pst, dtype=np.float32), np.array(out_cost, dtype=np.float32),
            np.array(out_nbind, dtype=np.int32), np.array(out_feasible, dtype=bool),
            np.array(out_residual, dtype=bool))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

DEFAULT_NC_IN = Path("/root/egon_2025_project/results/dispatch_8760h_pf.nc")
DEFAULT_NC_OUT = Path("/root/egon_2025_project/results/redispatch_8760h.nc")
DEFAULT_DELTAS = Path("/root/egon_2025_project/results/redispatch_8760h_deltas.csv")
DEFAULT_SUMMARY = Path("/root/egon_2025_project/results/redispatch_8760h_summary.json")


def precompute_gen_arrays(n, gen_ids):
    """Precompute (T, G) DA / max / min MW arrays for all generators."""
    log.info("Precomputing per-snapshot generator headrooms...")
    T = len(n.snapshots)
    G = len(gen_ids)

    p_nom = n.generators.loc[gen_ids, "p_nom"].values.astype(np.float32)
    pmax_static = n.generators.loc[gen_ids, "p_max_pu"].values.astype(np.float32)
    pmin_static = n.generators.loc[gen_ids, "p_min_pu"].values.astype(np.float32)

    p_max = np.broadcast_to(p_nom * pmax_static, (T, G)).astype(np.float32).copy()
    p_min = np.broadcast_to(p_nom * pmin_static, (T, G)).astype(np.float32).copy()

    pmax_t = n.generators_t.p_max_pu
    if pmax_t is not None and len(pmax_t.columns) > 0:
        cols = pmax_t.columns.intersection(gen_ids)
        if len(cols):
            log.info(f"  Overlaying p_max_pu timeseries for {len(cols)} gens...")
            sub = pmax_t[cols].values.astype(np.float32)   # (T, n_cols)
            sel = pd.Series(range(G), index=gen_ids).reindex(cols).values
            pn_sub = p_nom[sel]
            p_max[:, sel] = sub * pn_sub[None, :]

    pmin_t = n.generators_t.p_min_pu
    if pmin_t is not None and len(pmin_t.columns) > 0:
        cols = pmin_t.columns.intersection(gen_ids)
        if len(cols):
            log.info(f"  Overlaying p_min_pu timeseries for {len(cols)} gens...")
            sub = pmin_t[cols].values.astype(np.float32)
            sel = pd.Series(range(G), index=gen_ids).reindex(cols).values
            pn_sub = p_nom[sel]
            p_min[:, sel] = sub * pn_sub[None, :]

    # DA dispatch
    p_da_df = n.generators_t.p
    p_da = p_da_df.reindex(columns=gen_ids).values.astype(np.float32)

    # Sanity clamps: p_min ≤ p_da ≤ p_max (DA may slightly violate due to rounding)
    p_max = np.maximum(p_max, p_da)
    p_min = np.minimum(p_min, p_da)

    return p_da, p_max, p_min


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="nc_in", default=str(DEFAULT_NC_IN))
    ap.add_argument("--out", dest="nc_out", default=str(DEFAULT_NC_OUT))
    ap.add_argument("--deltas-out", default=str(DEFAULT_DELTAS))
    ap.add_argument("--summary-out", default=str(DEFAULT_SUMMARY))
    ap.add_argument("--snapshots", type=int, default=None,
                    help="Limit to first N snapshots (for testing)")
    ap.add_argument("--workers", type=int, default=12,
                    help="Parallel worker processes (1 = serial)")
    ap.add_argument("--save-nc", action="store_true", default=True,
                    help="Save the post-redispatch netCDF (default on; disable with --no-save-nc)")
    ap.add_argument("--no-save-nc", dest="save_nc", action="store_false")
    args = ap.parse_args()

    # ---- Load ----
    log.info(f"Loading {args.nc_in}")
    t0 = time.time()
    n = pypsa.Network(args.nc_in)
    log.info(f"  Loaded in {time.time()-t0:.1f}s.  "
             f"{len(n.buses)} buses, {len(n.lines)} lines, "
             f"{len(n.transformers)} trafos, {len(n.links)} links, "
             f"{len(n.generators)} gens, {len(n.snapshots)} snaps.")

    if args.snapshots:
        n.set_snapshots(n.snapshots[:args.snapshots])
        log.info(f"  Truncated to first {args.snapshots} snapshots.")

    T = len(n.snapshots)

    # ---- Data cleaning before PTDF (must match run_dcpf_8760h.py) ----
    n_tiny = int((n.lines["x"] < 0.05).sum())
    if n_tiny:
        log.info(f"Flooring {n_tiny} line(s) with x < 0.05 ohm to 0.05.")
        n.lines["x"] = n.lines["x"].clip(lower=0.05)
    if os.environ.get("APPLY_TOPOLOGY_FIXES", "0") == "1":
        from _topology_fix import apply_fixes
        apply_fixes(n)
    if os.environ.get("APPLY_GEN_VOLTAGE_FIX", "0") == "1":
        from _gen_voltage_fix import apply_gen_voltage_rule
        apply_gen_voltage_rule(n)
    # Populate per-unit reactances x_pu for the per-unit PTDF.
    n.calculate_dependent_values()

    # ---- Precompute PTDF, etc ----
    pre = build_precomp(n)
    L_sub = pre.s_nom.shape[0]

    # ---- DA line flows (extract from existing n.lines_t.p0) ----
    log.info("Extracting DA line flows from netCDF...")
    sub_line_ids = pre.sub_line_ids
    line_flow_all = n.lines_t.p0[sub_line_ids].values.astype(np.float32)   # (T, L_sub)
    log.info(f"  DA flows shape {line_flow_all.shape}")

    # ---- DA link flows (HVDC kept at 0 in dispatch) ----
    link_ids = pre.link_ids
    link_p_da_all = np.zeros((T, len(link_ids)), dtype=np.float32)
    if hasattr(n.links_t, "p0") and n.links_t.p0 is not None and len(n.links_t.p0.columns):
        for k, lid in enumerate(link_ids):
            if lid in n.links_t.p0.columns:
                link_p_da_all[:, k] = n.links_t.p0[lid].values.astype(np.float32)

    # ---- Precompute generator arrays ----
    gen_ids = pre.gen_ids
    G = len(gen_ids)
    p_da_all, p_max_all, p_min_all = precompute_gen_arrays(n, gen_ids)
    log.info(f"  Gen p_da total annual: {p_da_all.sum()/1e6:.1f} TWh")
    log.info(f"  Gen headroom up median: {(p_max_all - p_da_all).mean():.1f} MW/gen/h")

    # ---- Storage net (kept fixed at DA — no redispatch of storage) ----
    # We don't need this for the LP itself but for post-RD line flows it's already
    # baked into the DA line_flow_all, so no separate handling.

    # ---- Allocate big outputs as on-disk memmaps (workers write directly) ----
    tmpdir = Path(args.nc_out).parent
    mm_dg_path = str(tmpdir / ".rd_delta_gen.mmap")
    mm_nf_path = str(tmpdir / ".rd_new_flow.mmap")
    delta_gen_all = np.memmap(mm_dg_path, dtype=np.float32, mode="w+", shape=(T, G))
    new_flow_all = np.memmap(mm_nf_path, dtype=np.float32, mode="w+", shape=(T, L_sub))
    delta_gen_all[:] = 0
    new_flow_all[:] = line_flow_all      # default = DA flow when no overload
    delta_link_all = np.zeros((T, len(link_ids)), dtype=np.float32)
    delta_pst_all = np.zeros((T, len(pre.pst_ids)), dtype=np.float32)
    cost_per_h = np.zeros(T, dtype=np.float32)
    n_binding_per_h = np.zeros(T, dtype=np.int32)

    # ---- Loop ----
    log.info(f"\nSolving redispatch LPs for {T} snapshots with {args.workers} workers...")
    t0 = time.time()
    n_infeasible = 0
    n_solved = 0
    residual_overload_h = 0

    if args.workers <= 1:
        # Serial path
        log_every = max(1, T // 40)
        for t in range(T):
            res = solve_snapshot(
                pre, gen_p_da=p_da_all[t], gen_p_max=p_max_all[t],
                gen_p_min=p_min_all[t], link_p_da=link_p_da_all[t],
                line_flow_da=line_flow_all[t],
            )
            n_binding_per_h[t] = res.n_binding
            if res.n_binding == 0:
                continue
            if not res.feasible:
                n_infeasible += 1
                continue
            n_solved += 1
            delta_gen_all[t] = res.delta_p_gen
            delta_link_all[t] = res.delta_p_link
            delta_pst_all[t] = res.delta_p_pst
            new_flow_all[t] = res.new_line_flows
            cost_per_h[t] = res.cost
            if (np.abs(res.new_line_flows) > pre.s_nom * 1.001).any():
                residual_overload_h += 1
            if (t + 1) % log_every == 0:
                elapsed = time.time() - t0
                eta = elapsed / (t + 1) * (T - t - 1)
                log.info(f"  [{t+1:5d}/{T}] solved={n_solved} infeasible={n_infeasible} "
                         f"res_overload={residual_overload_h} elapsed={elapsed:.0f}s eta={eta:.0f}s")
    else:
        # Parallel path: fork inherits the big read-only arrays + memmaps.
        _W["pre"] = pre
        _W["p_da_all"] = p_da_all
        _W["p_max_all"] = p_max_all
        _W["p_min_all"] = p_min_all
        _W["link_p_da_all"] = link_p_da_all
        _W["line_flow_all"] = line_flow_all
        _W["mm_delta_gen"] = delta_gen_all
        _W["mm_new_flow"] = new_flow_all

        # Chunk snapshots; smaller chunks → smoother progress
        chunk = max(1, T // (args.workers * 8))
        chunks = [list(range(s, min(s + chunk, T))) for s in range(0, T, chunk)]
        done = 0
        ctx = mp.get_context("fork")
        with ctx.Pool(processes=args.workers) as pool:
            for (idxs, link_rows, pst_rows, cost_rows, nbind_rows,
                 feas_rows, resid_rows) in pool.imap_unordered(_worker_chunk, chunks):
                idxs = np.asarray(idxs)
                delta_link_all[idxs] = link_rows
                delta_pst_all[idxs] = pst_rows
                cost_per_h[idxs] = cost_rows
                n_binding_per_h[idxs] = nbind_rows
                n_solved += int(feas_rows.sum())
                n_infeasible += int(((nbind_rows > 0) & (~feas_rows)).sum())
                residual_overload_h += int(resid_rows.sum())
                done += len(idxs)
                elapsed = time.time() - t0
                eta = elapsed / max(done, 1) * (T - done)
                log.info(f"  [{done:5d}/{T}] solved={n_solved} infeasible={n_infeasible} "
                         f"res_overload={residual_overload_h} elapsed={elapsed:.0f}s eta={eta:.0f}s")

    delta_gen_all.flush()
    new_flow_all.flush()
    log.info(f"\nRedispatch LP loop done in {time.time()-t0:.0f}s")
    log.info(f"  Hours with any overload (pre-RD): {(n_binding_per_h>0).sum()} / {T}")
    log.info(f"  Hours solved successfully:        {n_solved}")
    log.info(f"  Hours infeasible:                 {n_infeasible}")
    log.info(f"  Hours with residual overload:     {residual_overload_h}")

    # ---- Annual aggregates ----
    total_up = float(np.maximum(delta_gen_all, 0).sum() / 1e6)   # TWh
    total_dn = float(-np.minimum(delta_gen_all, 0).sum() / 1e6)
    res_mask = np.array([c in RES_CARRIERS for c in pre.gen_carrier])
    kwk_mask = np.array([c in KWK_CARRIERS for c in pre.gen_carrier])
    res_dn = float(-np.minimum(delta_gen_all[:, res_mask], 0).sum() / 1e6)
    conv_mask = ~(res_mask | kwk_mask)
    conv_up = float(np.maximum(delta_gen_all[:, conv_mask], 0).sum() / 1e6)
    log.info(f"\n--- Redispatch volumes ---")
    log.info(f"  Total ramp-up:          {total_up:.2f} TWh")
    log.info(f"  Total ramp-down:        {total_dn:.2f} TWh")
    log.info(f"  RES curtailed:          {res_dn:.2f} TWh")
    log.info(f"  Conventional ramped up: {conv_up:.2f} TWh")
    log.info(f"  Total cost (LP obj):    {cost_per_h.sum()/1e6:.2f} M€")

    # ---- Apply to network ----
    if args.save_nc:
        log.info("\nApplying Δp to network and recomputing flows...")
        # New generator output
        new_p = (p_da_all + delta_gen_all).astype(np.float32)
        new_p_df = pd.DataFrame(new_p, index=n.snapshots, columns=gen_ids)
        n.generators_t.p = new_p_df

        # New HVDC link flows
        new_link = (link_p_da_all + delta_link_all).astype(np.float32)
        new_link_df = pd.DataFrame(new_link, index=n.snapshots, columns=link_ids)
        # Merge into existing n.links_t.p0
        existing_links = n.links_t.p0
        if existing_links is not None and len(existing_links.columns):
            existing_links = existing_links.copy()
        else:
            existing_links = pd.DataFrame(np.zeros((T, len(n.links))), index=n.snapshots, columns=n.links.index)
        for lid in link_ids:
            existing_links[lid] = new_link_df[lid]
        n.links_t.p0 = existing_links
        n.links_t.p1 = -existing_links

        # New line flows (only main subnet got updates; lines outside main remain 0)
        new_lines = pd.DataFrame(np.zeros((T, len(n.lines)), dtype=np.float32),
                                 index=n.snapshots, columns=n.lines.index)
        new_lines[sub_line_ids] = new_flow_all
        n.lines_t.p0 = new_lines
        n.lines_t.p1 = -new_lines

        log.info(f"Saving {args.nc_out}")
        t0 = time.time()
        n.export_to_netcdf(args.nc_out)
        log.info(f"  Saved in {time.time()-t0:.1f}s "
                 f"({Path(args.nc_out).stat().st_size/1e6:.0f} MB)")

    # ---- Save deltas ----
    log.info(f"Saving deltas → {args.deltas_out}")
    deltas = {
        "carrier": pre.gen_carrier,
        "bus": n.generators.loc[pre.gen_ids, "bus"].values,
        "gen_id": pre.gen_ids,
        "annual_dp_up_MWh": np.maximum(delta_gen_all, 0).sum(axis=0),
        "annual_dp_dn_MWh": -np.minimum(delta_gen_all, 0).sum(axis=0),
    }
    pd.DataFrame(deltas).to_csv(args.deltas_out, index=False)

    # Save per-snapshot binding line mask, cost, and full Δp_gen matrix (npz)
    summary_extra = pd.DataFrame({
        "snapshot": n.snapshots,
        "n_binding": n_binding_per_h,
        "cost_eur": cost_per_h,
    })
    summary_extra.to_csv(args.deltas_out.replace(".csv", "_per_snapshot.csv"), index=False)
    # Full hourly delta matrix as npz (compact)
    npz_path = args.deltas_out.replace(".csv", "_hourly.npz")
    np.savez_compressed(
        npz_path,
        snapshots=np.array([str(s) for s in n.snapshots]),
        gen_ids=pre.gen_ids,
        gen_carrier=pre.gen_carrier,
        sub_line_ids=np.array(sub_line_ids),
        delta_gen=delta_gen_all,
        delta_link=delta_link_all,
        delta_pst=delta_pst_all,
        line_flow_da=line_flow_all,
        line_flow_post=new_flow_all,
        s_nom=pre.s_nom,
        n_binding=n_binding_per_h,
        cost=cost_per_h,
    )
    log.info(f"Hourly deltas → {npz_path}")

    # ---- Save summary JSON ----
    summary = {
        "scenario": "grid_beta",
        "snapshots": T,
        "hours_with_overload_pre": int((n_binding_per_h > 0).sum()),
        "hours_solved": int(n_solved),
        "hours_infeasible": int(n_infeasible),
        "hours_residual_overload_post": int(residual_overload_h),
        "total_ramp_up_TWh": total_up,
        "total_ramp_down_TWh": total_dn,
        "RES_curtailed_TWh": res_dn,
        "conventional_ramped_up_TWh": conv_up,
        "total_cost_MEUR": float(cost_per_h.sum() / 1e6),
        "BNetzA_2024_benchmark_TWh": 30.3,
        "BNetzA_2024_benchmark_RES_TWh": 9.4,
        "BNetzA_2024_benchmark_cost_MEUR": 2776,
    }
    Path(args.summary_out).write_text(json.dumps(summary, indent=2))
    log.info(f"Summary → {args.summary_out}")
    log.info(json.dumps(summary, indent=2))

    # ---- Clean up scratch memmaps ----
    try:
        del delta_gen_all, new_flow_all
        for p in [mm_dg_path, mm_nf_path]:
            Path(p).unlink(missing_ok=True)
    except Exception as e:
        log.warning(f"memmap cleanup: {e}")


if __name__ == "__main__":
    main()
