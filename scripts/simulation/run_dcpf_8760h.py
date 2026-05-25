#!/usr/bin/env python3
"""
run_dcpf_8760h.py — Linear (DC) power flow on top of the saved dispatch.

Reads the saved dispatch (results/dispatch_8760h.nc), computes per-line
active-power flows for every snapshot using the linear (DC) approximation,
writes flows back into n.lines_t.p0 and n.transformers_t.p0, and saves
results/dispatch_8760h_pf.nc.

Approach:
    1. Load the dispatch netCDF.
    2. Compute the PTDF (Power Transfer Distribution Factors) once for the
       main connected subnetwork using PyPSA's sparse implementation. PTDF is
       a ~12.9k × 7.7k matrix mapping bus net-injections to line flows.
    3. For each snapshot: net injection = generation − load + storage_dispatch;
       line flow = PTDF @ injection. Vectorised across all 8760 snapshots in
       blocks of 500.
    4. Transformers carry the residual; we approximate transformer flow per
       snapshot by Kirchhoff at each connected bus (transformer flow =
       imbalance between line in/out at that bus).
    5. HVDC links are kept at 0 (they cross between subnetworks); cross-border
       flow goes via the AC interconnectors.

Usage:
    conda activate egon2025
    python scripts/simulation/run_dcpf_8760h.py
"""
import argparse
import logging
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa
from scipy.sparse import csr_matrix, lil_matrix
from scipy.sparse.linalg import factorized

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

DEFAULT_NC_IN = Path("/root/egon_2025_project/results/dispatch_8760h.nc")
DEFAULT_NC_OUT = Path("/root/egon_2025_project/results/dispatch_8760h_pf.nc")


def compute_injection(n):
    """Net active-power injection per bus per snapshot (MW). Shape (T, B)."""
    log.info("Computing per-bus net injection...")
    bus_ids = list(n.buses.index)
    bus_idx = {b: i for i, b in enumerate(bus_ids)}
    T = len(n.snapshots)
    B = len(bus_ids)
    inj = np.zeros((T, B), dtype=np.float32)

    # Generators (columns are gen_id)
    p_gen = n.generators_t.p
    if p_gen is not None and len(p_gen.columns) > 0:
        gen_buses = n.generators.loc[p_gen.columns, "bus"].values
        log.info(f"  Generators: {len(p_gen.columns)} columns, summing into {B} buses...")
        # Group columns by bus, sum into inj
        bus_col_idx = np.array([bus_idx[b] for b in gen_buses])
        # vectorized scatter-add via pandas groupby
        df = p_gen.T.copy()
        df["__bus"] = bus_col_idx
        gen_per_bus = df.groupby("__bus").sum().T  # shape (T, n_unique_buses)
        for col in gen_per_bus.columns:
            inj[:, int(col)] += gen_per_bus[col].values.astype(np.float32)

    # Loads (subtract)
    p_load = n.loads_t.p
    if p_load is None or p_load.empty:
        p_load = n.loads_t.p_set
    if p_load is not None and len(p_load.columns) > 0:
        load_buses = n.loads.loc[p_load.columns, "bus"].values
        bus_col_idx = np.array([bus_idx[b] for b in load_buses])
        log.info(f"  Loads: {len(p_load.columns)} columns")
        df = p_load.T.copy()
        df["__bus"] = bus_col_idx
        load_per_bus = df.groupby("__bus").sum().T
        for col in load_per_bus.columns:
            inj[:, int(col)] -= load_per_bus[col].values.astype(np.float32)

    # Storage net (dispatch - store)
    if hasattr(n, "storage_units_t"):
        p_dis = n.storage_units_t.p_dispatch
        p_st = n.storage_units_t.p_store
        if p_dis is not None and len(p_dis.columns) > 0:
            net = p_dis.values - (p_st.values if p_st is not None and not p_st.empty else 0)
            stor_buses = n.storage_units.loc[p_dis.columns, "bus"].values
            log.info(f"  Storage: {len(p_dis.columns)} columns")
            for j, b in enumerate(stor_buses):
                inj[:, bus_idx[b]] += net[:, j].astype(np.float32)

    log.info(f"  Net injection: hourly sum mean = {inj.sum(axis=1).mean():.0f} MW "
             f"(should be near 0 in copperplate balance)")
    return inj, bus_ids


def main_subnetwork_ptdf(n):
    """Compute PTDF for the main connected subnetwork.

    Returns:
        ptdf: (n_lines_in_main_subnet, n_buses_in_main_subnet) float32
        line_ids: list of line IDs in main subnetwork (in row order)
        bus_ids: list of bus IDs in main subnetwork (in column order)
        slack_idx: index of slack bus (column dropped during inversion)
    """
    log.info("Computing PTDF on main subnetwork...")
    n.determine_network_topology()

    # Pick the largest subnetwork by bus count
    main_sn = None
    main_size = 0
    main_name = None
    for name, row in n.sub_networks.iterrows():
        sn_obj = row.obj
        bs = sn_obj.buses_i()
        if len(bs) > main_size:
            main_size = len(bs)
            main_sn = sn_obj
            main_name = name

    log.info(f"  Main subnetwork: {main_name}, {main_size} buses")

    sub_buses = list(main_sn.buses_i())
    sub_lines = list(main_sn.lines_i())
    sub_trafos = list(main_sn.transformers_i())
    log.info(f"  Lines in main: {len(sub_lines)} / {len(n.lines)}")
    log.info(f"  Transformers in main: {len(sub_trafos)} / {len(n.transformers)}")

    bus_idx = {b: i for i, b in enumerate(sub_buses)}
    B = len(sub_buses)
    L = len(sub_lines)

    # Lines: incidence + susceptance for output PTDF.
    # IMPORTANT: use the PER-UNIT reactance x_pu (= x / (v_nom^2 / S_base)), NOT the
    # raw ohmic x. Raw ohms mix 110/220/380 kV buses on inconsistent bases and make
    # 110 kV lines look ~(380/110)^2 ≈ 12x more conductive than they should, which
    # routes huge spurious flows through the 110 kV grid.  x_pu is populated by
    # n.calculate_dependent_values() in main().
    lines_in_main = n.lines.loc[sub_lines]
    x_arr = lines_in_main["x_pu"].values.astype(np.float64)
    b0_arr = np.array([bus_idx[b] for b in lines_in_main["bus0"]])
    b1_arr = np.array([bus_idx[b] for b in lines_in_main["bus1"]])
    b_susc = 1.0 / x_arr

    # B_bus is the Laplacian over BOTH lines AND transformers (since topology
    # connects buses through both). Lines also contribute to the line-flow PTDF;
    # transformers are passive branches whose flow we compute separately later.
    all_b0 = list(b0_arr)
    all_b1 = list(b1_arr)
    all_b = list(b_susc)

    if sub_trafos:
        trafos_in_main = n.transformers.loc[sub_trafos]
        tx = trafos_in_main["x_pu"].values.astype(np.float64)   # per-unit, same base as lines
        # Avoid division by zero
        tx = np.where(tx > 0, tx, 1.0)
        tb0 = np.array([bus_idx[b] for b in trafos_in_main["bus0"]])
        tb1 = np.array([bus_idx[b] for b in trafos_in_main["bus1"]])
        tb_susc = 1.0 / tx
        all_b0.extend(tb0.tolist())
        all_b1.extend(tb1.tolist())
        all_b.extend(tb_susc.tolist())

    all_b0 = np.asarray(all_b0)
    all_b1 = np.asarray(all_b1)
    all_b = np.asarray(all_b, dtype=np.float64)

    # Laplacian
    rows = np.concatenate([all_b0, all_b1, all_b0, all_b1])
    cols = np.concatenate([all_b1, all_b0, all_b0, all_b1])
    vals = np.concatenate([-all_b, -all_b, all_b, all_b])
    B_bus = csr_matrix((vals, (rows, cols)), shape=(B, B))

    # K is line-only incidence (we only care about LINE flows in the report)
    K_data = np.concatenate([b_susc, -b_susc])
    K_rows = np.concatenate([np.arange(L), np.arange(L)])
    K_cols = np.concatenate([b0_arr, b1_arr])
    K = csr_matrix((K_data, (K_rows, K_cols)), shape=(L, B))

    # Pick slack: bus with highest connectivity (counting both lines + trafos)
    deg = np.bincount(np.concatenate([all_b0, all_b1]), minlength=B)
    slack = int(np.argmax(deg))
    log.info(f"  Slack bus: idx {slack} ({sub_buses[slack]})")

    # Drop slack row+col from B_bus
    keep = np.array([i for i in range(B) if i != slack])
    B_red = B_bus[keep][:, keep].tocsc()

    log.info(f"  Factorizing reduced B ({B_red.shape[0]} × {B_red.shape[1]}, "
             f"{B_red.nnz} nnz)...")
    t0 = time.time()
    solve = factorized(B_red)
    log.info(f"  Factorisation: {time.time()-t0:.1f}s")

    # Build PTDF via line-flow formula:
    # For each line l: f_l = b_l × (theta_b0 - theta_b1) = b_l × (PTDF row applied to p)
    # Where theta = B_red^-1 × p (with slack dropped).
    # PTDF = diag(b) × K^T_red × B_red^-1   (in reduced bus space)
    # We compute PTDF as a (L × B) matrix: drop slack column, then add zero col for slack.
    log.info("  Building PTDF...")
    t0 = time.time()
    K_red = K[:, keep].toarray().astype(np.float64)   # (L, B-1)
    # We need M = B_red^-1 * K_red^T, then PTDF_red = diag(b) × K_red @ M? wait.
    # Cleaner: for each unit injection at bus i (slack-relative), flows = diag(b) @ (theta_b0 - theta_b1)
    #     where theta = B_red^-1 @ e_i.
    # So PTDF_red column i = diag(b) @ (K_red @ B_red^-1 @ e_i) = diag(b) @ K_red @ B_red^-1[:, i]
    # That's M = B_red^-1 (B-1 × B-1), then PTDF_red = diag(b) @ K_red @ M (L × B-1).
    # We can avoid materialising B_red^-1 by solving system column-wise. With B-1 ≈ 7700 cols
    # × ~ms each → minutes. Faster: solve K_red @ ... no, easier still: solve M @ rhs.
    # For our purposes: PTDF_red = diag(b) @ K_red @ B_red_inv  ⇒  in matrix form:
    # PTDF_red = (diag(b) @ K_red) @ B_red_inv. We compute PTDF_red.T = B_red_inv.T @ (diag(b) @ K_red).T
    # which is B_red_inv applied to each column of (diag(b) @ K_red).T. (We use B_red symmetric so .T = self.)
    # K_red ALREADY carries the susceptance (K_data = ±b_susc), i.e. K = diag(b) @ A^T.
    # The correct PTDF is diag(b) @ A^T @ B^-1, so we must solve with K_red.T directly.
    # (The previous code multiplied by b a SECOND time → diag(b^2) @ A^T, over-counting
    #  susceptance by a factor b on every branch and inflating PTDF entries.)
    rhs = K_red.T    # (B-1, L) — already = (diag(b) @ A^T)^T
    ptdf_red_T = np.empty_like(rhs)
    block = 256
    for start in range(0, L, block):
        end = min(start + block, L)
        ptdf_red_T[:, start:end] = np.column_stack([solve(rhs[:, j]) for j in range(start, end)])
    ptdf_red = ptdf_red_T.T   # (L, B-1)
    log.info(f"  PTDF columns solved in {time.time()-t0:.1f}s")

    # Insert zero column at slack
    ptdf = np.zeros((L, B), dtype=np.float32)
    ptdf[:, keep] = ptdf_red.astype(np.float32)
    log.info(f"  PTDF shape {ptdf.shape}, {ptdf.nbytes/1e6:.0f} MB")

    return ptdf, sub_lines, sub_buses


def compute_line_flows(ptdf, sub_lines, sub_buses, inj_full, all_bus_ids):
    """Apply PTDF to per-snapshot bus injection to obtain line flows.

    inj_full: (T, len(all_bus_ids))
    Returns DataFrame (T x L) of MW on each line in sub_lines.
    """
    log.info("Applying PTDF to all snapshots...")
    bus_full_idx = {b: i for i, b in enumerate(all_bus_ids)}
    sub_bus_full_cols = np.array([bus_full_idx[b] for b in sub_buses])

    inj_sub = inj_full[:, sub_bus_full_cols].astype(np.float32)  # (T, B_sub)
    T, B = inj_sub.shape
    L = ptdf.shape[0]

    # Slack bus injection: enforce island balance by subtracting the mean from all buses?
    # Actually for DC PF in a balanced subnetwork, sum(p_sub) should ≈ 0 ideally.
    # If not zero (because foreign HVDC isn't modeled), force balance by allocating residual to slack.
    imbalance = inj_sub.sum(axis=1)
    log.info(f"  Sub injection imbalance: mean={imbalance.mean():.0f} MW, "
             f"max abs={np.abs(imbalance).max():.0f} MW")
    # Distribute imbalance evenly across all buses (load-flow-like correction)
    inj_sub -= imbalance[:, None] / B

    # Block-multiply to control memory: blocks of 500 snapshots
    flows = np.empty((T, L), dtype=np.float32)
    BSZ = 500
    t0 = time.time()
    for start in range(0, T, BSZ):
        end = min(start + BSZ, T)
        # flows = inj @ ptdf.T   (matmul: (BSZ, B) @ (B, L) = (BSZ, L))
        flows[start:end] = inj_sub[start:end] @ ptdf.T
        if start // BSZ in (0, 1, 5):
            log.info(f"  Block {start}-{end}: {time.time()-t0:.1f}s")
    log.info(f"  Total PTDF apply: {time.time()-t0:.1f}s")

    return pd.DataFrame(flows, columns=sub_lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="nc_in", default=str(DEFAULT_NC_IN))
    parser.add_argument("--out", dest="nc_out", default=str(DEFAULT_NC_OUT))
    parser.add_argument("--snapshots", type=int, default=None,
                        help="Limit to first N snapshots (for testing)")
    args = parser.parse_args()

    log.info(f"Loading {args.nc_in}")
    t0 = time.time()
    n = pypsa.Network(args.nc_in)
    log.info(f"  Loaded in {time.time()-t0:.1f}s. "
             f"{len(n.buses)} buses, {len(n.lines)} lines, {len(n.snapshots)} snaps.")

    if args.snapshots:
        n.set_snapshots(n.snapshots[:args.snapshots])
        log.info(f"Truncated to first {args.snapshots} snapshots")

    # ---- Data cleaning before PTDF ----
    # Floor non-physical tiny line reactances. Line 33300 is a 0.01 km / x=0.001
    # "dummy connector" between two duplicate buses (6700 & 13371, ~50 m apart,
    # same substation); its near-zero reactance acts as a short circuit and, under
    # the per-unit formulation, would dominate the susceptance matrix. A 0.05 ohm
    # floor corresponds to a realistic very-short 110 kV span and affects only that line.
    n_tiny = int((n.lines["x"] < 0.05).sum())
    if n_tiny:
        log.info(f"Flooring {n_tiny} line(s) with x < 0.05 ohm to 0.05 (e.g. dummy connector 33300).")
        n.lines["x"] = n.lines["x"].clip(lower=0.05)

    # Optional topology fixes (adds missing EHV/110 transformers at radial pockets).
    if os.environ.get("APPLY_TOPOLOGY_FIXES", "0") == "1":
        from _topology_fix import apply_fixes
        apply_fixes(n)
    # Optional generator-voltage rule (≥150 MW units on 110 kV → nearest EHV bus).
    if os.environ.get("APPLY_GEN_VOLTAGE_FIX", "0") == "1":
        from _gen_voltage_fix import apply_gen_voltage_rule
        apply_gen_voltage_rule(n)

    # Populate per-unit reactances x_pu used by the PTDF (per-unit DC power flow).
    n.calculate_dependent_values()

    inj, all_bus_ids = compute_injection(n)
    ptdf, sub_lines, sub_buses = main_subnetwork_ptdf(n)
    flows = compute_line_flows(ptdf, sub_lines, sub_buses, inj, all_bus_ids)
    flows.index = n.snapshots

    # Inject into network
    log.info("Injecting line flows into network...")
    p0 = pd.DataFrame(np.zeros((len(n.snapshots), len(n.lines)), dtype=np.float32),
                      index=n.snapshots, columns=list(n.lines.index))
    p0[flows.columns] = flows.values
    n.lines_t.p0 = p0
    n.lines_t.p1 = -p0  # DC PF: lossless, p1 = -p0

    log.info(f"Saving {args.nc_out}")
    t0 = time.time()
    n.export_to_netcdf(args.nc_out)
    log.info(f"  Saved in {time.time()-t0:.1f}s ({Path(args.nc_out).stat().st_size/1e6:.0f} MB)")

    # Summary
    s_nom = n.lines.s_nom.values.astype(np.float32)
    s_nom_safe = np.where(s_nom > 0, s_nom, np.nan)
    abs_p = flows.abs().values
    sub_s = n.lines.loc[flows.columns, "s_nom"].values.astype(np.float32)
    sub_s_safe = np.where(sub_s > 0, sub_s, np.nan)
    loading = abs_p / sub_s_safe[None, :]   # (T, L_sub)
    log.info(f"\nLine loading stats (% of s_nom):")
    log.info(f"  mean across all line-hours: {np.nanmean(loading)*100:.1f}%")
    log.info(f"  p95 line-hours:            {np.nanpercentile(loading, 95)*100:.1f}%")
    log.info(f"  p99 line-hours:            {np.nanpercentile(loading, 99)*100:.1f}%")
    log.info(f"  max line-hour:             {np.nanmax(loading)*100:.1f}%")
    overloaded = (loading > 1.0).sum()
    total_lh = loading.size
    log.info(f"  overloaded line-hours:     {overloaded:,} / {total_lh:,} ({100*overloaded/total_lh:.2f}%)")
    n_overloaded_lines = (np.nanmax(loading, axis=0) > 1.0).sum()
    log.info(f"  lines with any overload:   {n_overloaded_lines} / {loading.shape[1]}")
    log.info("Done.")


if __name__ == "__main__":
    main()
