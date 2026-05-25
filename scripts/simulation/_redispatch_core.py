"""Shared helpers for the day-ahead Redispatch 2.0 workflow.

The redispatch LP follows German §13a EnWG practice (Mindestfaktor 10x RES, 5x KWK):
for every hour with any line at |f| > s_nom, ramp eligible units (PTDF-filtered,
aggregated by bus+carrier) so that all line limits are met at minimum cost,
with renewables and KWK weighted up so they're touched only when conventional
headroom is exhausted on the binding element.

Modules consume this from `run_redispatch_8760h.py` (runner) and
`build_redispatch_report.py` (post-hoc analytics).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from scipy.sparse import csr_matrix, lil_matrix, vstack as sp_vstack, eye as sp_eye, hstack as sp_hstack
from scipy.sparse.linalg import factorized

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Mindestfaktor (BNetzA BK6-20-059 / §13a EnWG)
# --------------------------------------------------------------------------- #
RES_CARRIERS = {
    "solar", "onwind", "offwind",
    "run_of_river", "reservoir",
    "biogas", "biomass", "waste",
}
KWK_CARRIERS = {"gas_chp"}
CONV_CARRIERS = {
    "gas_ccgt", "coal", "hard_coal", "lignite", "oil",
    "other", "other_conventional", "hydrogen",
}
IMPORT_PREFIX = "import_"
STORAGE_CARRIERS = {"battery", "pumped_hydro"}

# Default per-MWh imputed prices used if a unit has marginal_cost == 0
DEFAULT_C = {
    "solar":        0.0,
    "onwind":       0.0,
    "offwind":      0.0,
    "run_of_river": 0.0,
    "reservoir":    1.0,
    "biogas":       0.0,
    "biomass":      0.0,
    "waste":       -5.0,
    "gas_chp":     65.0,
    "gas_ccgt":   100.0,
    "coal":        90.0,
    "hard_coal":   90.0,
    "lignite":     50.0,
    "oil":        150.0,
    "other":       80.0,
    "hydrogen":   200.0,
}
# Imputed RES prices used for redispatch costing (so RES Δp is not free)
IMPUTED_C = {
    "solar":   45.0,
    "onwind":  85.0,
    "offwind":110.0,
    "run_of_river": 30.0,
    "reservoir":    40.0,
    "biogas":  60.0,
    "biomass": 60.0,
    "waste":   30.0,
}


def mindestfaktor_weight(carrier: str) -> float:
    """Cost multiplier in the redispatch objective."""
    if carrier in RES_CARRIERS:
        return 10.0
    if carrier in KWK_CARRIERS:
        return 5.0
    return 1.0


def effective_marginal_cost(carrier: str, c: float) -> float:
    """Use the unit's marginal_cost if non-zero, else an imputed default.

    RES units have c==0 in DA dispatch; for redispatch we need a positive
    proxy price so the Mindestfaktor multiplier works on a non-zero base.
    """
    if c and c > 0:
        return float(c)
    if carrier in IMPUTED_C:
        return IMPUTED_C[carrier]
    return float(DEFAULT_C.get(carrier, 50.0))


# --------------------------------------------------------------------------- #
# PTDF on the main subnetwork (lifted verbatim from run_dcpf_8760h.py)
# --------------------------------------------------------------------------- #
def main_subnetwork_ptdf(n):
    """Compute PTDF for the largest connected subnetwork.

    Returns:
        ptdf:        (L_sub, B_sub) float32 — column order = sub_buses
        sub_lines:   list[str] of line ids in row order
        sub_buses:   list[str] of bus ids in column order
    """
    log.info("Computing PTDF on main subnetwork...")
    n.determine_network_topology()

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

    # Use PER-UNIT reactance x_pu (populated by n.calculate_dependent_values() in the
    # caller), NOT raw ohms — raw ohms mix 110/220/380 kV on inconsistent bases and
    # misroute flow onto the 110 kV grid. Must match run_dcpf_8760h.py exactly.
    lines_in_main = n.lines.loc[sub_lines]
    x_arr = lines_in_main["x_pu"].values.astype(np.float64)
    b0_arr = np.array([bus_idx[b] for b in lines_in_main["bus0"]])
    b1_arr = np.array([bus_idx[b] for b in lines_in_main["bus1"]])
    b_susc = 1.0 / x_arr

    all_b0 = list(b0_arr); all_b1 = list(b1_arr); all_b = list(b_susc)

    if sub_trafos:
        trafos_in_main = n.transformers.loc[sub_trafos]
        tx = trafos_in_main["x_pu"].values.astype(np.float64)   # per-unit, same base as lines
        tx = np.where(tx > 0, tx, 1.0)
        tb0 = np.array([bus_idx[b] for b in trafos_in_main["bus0"]])
        tb1 = np.array([bus_idx[b] for b in trafos_in_main["bus1"]])
        tb_susc = 1.0 / tx
        all_b0.extend(tb0.tolist()); all_b1.extend(tb1.tolist()); all_b.extend(tb_susc.tolist())

    all_b0 = np.asarray(all_b0); all_b1 = np.asarray(all_b1)
    all_b = np.asarray(all_b, dtype=np.float64)

    rows = np.concatenate([all_b0, all_b1, all_b0, all_b1])
    cols = np.concatenate([all_b1, all_b0, all_b0, all_b1])
    vals = np.concatenate([-all_b, -all_b, all_b, all_b])
    B_bus = csr_matrix((vals, (rows, cols)), shape=(B, B))

    K_data = np.concatenate([b_susc, -b_susc])
    K_rows = np.concatenate([np.arange(L), np.arange(L)])
    K_cols = np.concatenate([b0_arr, b1_arr])
    K = csr_matrix((K_data, (K_rows, K_cols)), shape=(L, B))

    deg = np.bincount(np.concatenate([all_b0, all_b1]), minlength=B)
    slack = int(np.argmax(deg))
    log.info(f"  Slack bus: idx {slack} ({sub_buses[slack]})")

    keep = np.array([i for i in range(B) if i != slack])
    B_red = B_bus[keep][:, keep].tocsc()
    log.info(f"  Factorizing reduced B ({B_red.shape[0]} × {B_red.shape[1]}, {B_red.nnz} nnz)...")
    t0 = time.time()
    solve = factorized(B_red)
    log.info(f"  Factorisation: {time.time()-t0:.1f}s")

    log.info("  Building PTDF...")
    t0 = time.time()
    K_red = K[:, keep].toarray().astype(np.float64)
    # K_red already carries the susceptance (K = diag(b) @ A^T); the correct PTDF is
    # diag(b) @ A^T @ B^-1, so solve with K_red.T directly. (Multiplying by b again here
    # would over-count susceptance by a factor b on every branch.)
    rhs = K_red.T
    ptdf_red_T = np.empty_like(rhs)
    block = 256
    for start in range(0, L, block):
        end = min(start + block, L)
        ptdf_red_T[:, start:end] = np.column_stack([solve(rhs[:, j]) for j in range(start, end)])
    ptdf_red = ptdf_red_T.T

    ptdf = np.zeros((L, B), dtype=np.float32)
    ptdf[:, keep] = ptdf_red.astype(np.float32)
    log.info(f"  PTDF: shape {ptdf.shape}, {ptdf.nbytes/1e6:.0f} MB, built in {time.time()-t0:.1f}s")
    return ptdf, sub_lines, sub_buses


# --------------------------------------------------------------------------- #
# Snapshot LP
# --------------------------------------------------------------------------- #
@dataclass
class RedispatchPrecomp:
    """All time-invariant data needed by the per-snapshot LP solver."""
    ptdf: np.ndarray                # (L_sub, B_sub) float32
    sub_line_ids: list              # list[str]
    sub_bus_ids: list               # list[str]
    bus_idx_in_sub: dict            # bus_id -> col index, or -1 if not in main sub
    line_idx_in_sub: dict           # line_id -> row index, or -1 if not in main sub
    s_nom: np.ndarray               # (L_sub,) per-line s_nom
    # Generators
    gen_ids: np.ndarray             # full generator index
    gen_bus_sub_col: np.ndarray     # PTDF column for each generator's bus (-1 if not in sub)
    gen_carrier: np.ndarray         # str array
    gen_p_nom: np.ndarray
    gen_mc: np.ndarray              # effective marginal cost
    gen_weight: np.ndarray          # mindestfaktor multiplier
    # HVDC links (we treat all PyPSA links with carrier=='DC' as HVDC)
    link_ids: np.ndarray
    link_bus0_sub_col: np.ndarray   # PTDF column for bus0 of link (only bus0 is in sub; bus1 is foreign)
    link_p_nom: np.ndarray
    # PSTs (transformers with trafo_id >= 32000)
    pst_ids: np.ndarray
    pst_bus0_sub_col: np.ndarray
    pst_bus1_sub_col: np.ndarray
    pst_s_nom: np.ndarray


def build_precomp(n) -> RedispatchPrecomp:
    """Materialise all the time-invariant lookups."""
    log.info("Building redispatch precomputation tables...")
    ptdf, sub_line_ids, sub_bus_ids = main_subnetwork_ptdf(n)

    bus_idx_in_sub = {b: i for i, b in enumerate(sub_bus_ids)}
    line_idx_in_sub = {l: i for i, l in enumerate(sub_line_ids)}

    s_nom = n.lines.loc[sub_line_ids, "s_nom"].values.astype(np.float32)

    # Generators
    gens = n.generators
    gen_bus_sub_col = np.array([bus_idx_in_sub.get(b, -1) for b in gens["bus"]], dtype=np.int32)
    gen_carrier = gens["carrier"].fillna("other").values.astype(str)
    gen_p_nom = gens["p_nom"].values.astype(np.float32)
    gen_mc_raw = gens["marginal_cost"].values.astype(np.float32)
    gen_mc = np.array([effective_marginal_cost(c, m) for c, m in zip(gen_carrier, gen_mc_raw)],
                      dtype=np.float32)
    gen_weight = np.array([mindestfaktor_weight(c) for c in gen_carrier], dtype=np.float32)

    # Links (HVDC)
    links = n.links
    is_dc = (links["carrier"].astype(str) == "DC") if "carrier" in links.columns else np.ones(len(links), bool)
    link_ids = np.asarray(links.index[is_dc])
    link_bus0_sub_col = np.array(
        [bus_idx_in_sub.get(b, -1) for b in links.loc[link_ids, "bus0"]],
        dtype=np.int32,
    )
    link_p_nom = links.loc[link_ids, "p_nom"].values.astype(np.float32)

    # PSTs: trafo_id >= 32000 (from build_v6.py NEW_TRAFO_START)
    trafo_ids_int = np.array([int(t) for t in n.transformers.index])
    is_pst = trafo_ids_int >= 32000
    pst_ids = np.asarray(n.transformers.index[is_pst])
    pst_bus0_sub_col = np.array(
        [bus_idx_in_sub.get(b, -1) for b in n.transformers.loc[pst_ids, "bus0"]],
        dtype=np.int32,
    )
    pst_bus1_sub_col = np.array(
        [bus_idx_in_sub.get(b, -1) for b in n.transformers.loc[pst_ids, "bus1"]],
        dtype=np.int32,
    )
    pst_s_nom = n.transformers.loc[pst_ids, "s_nom"].values.astype(np.float32)

    log.info(
        f"  {len(gens)} generators, {len(link_ids)} HVDC links, {len(pst_ids)} PSTs"
    )

    return RedispatchPrecomp(
        ptdf=ptdf,
        sub_line_ids=sub_line_ids,
        sub_bus_ids=sub_bus_ids,
        bus_idx_in_sub=bus_idx_in_sub,
        line_idx_in_sub=line_idx_in_sub,
        s_nom=s_nom,
        gen_ids=np.asarray(gens.index),
        gen_bus_sub_col=gen_bus_sub_col,
        gen_carrier=gen_carrier,
        gen_p_nom=gen_p_nom,
        gen_mc=gen_mc,
        gen_weight=gen_weight,
        link_ids=link_ids,
        link_bus0_sub_col=link_bus0_sub_col,
        link_p_nom=link_p_nom,
        pst_ids=pst_ids,
        pst_bus0_sub_col=pst_bus0_sub_col,
        pst_bus1_sub_col=pst_bus1_sub_col,
        pst_s_nom=pst_s_nom,
    )


# --------------------------------------------------------------------------- #
# Per-snapshot LP
# --------------------------------------------------------------------------- #
PTDF_FILTER = 0.02   # |PTDF| sensitivity threshold for eligibility
PST_SWING_FRAC = 0.20  # ±20% of s_nom (~12° tap range × kV)
HVDC_PEN = 0.5       # €/MWh shadow cost on |Δp_hvdc|
PST_PEN = 0.1        # €/MWh shadow cost on |Δp_pst|
LINE_SLACK_COST = 500.0   # €/MWh penalty for residual overload (TSO emergency action)
MIN_GEN_MW = 1.0          # ignore generators with p_nom < 1 MW
MAX_BINDING_LINES = 40    # focus on the top-N most-overloaded lines per hour


@dataclass
class SnapshotResult:
    delta_p_gen: np.ndarray         # (G,) MW per generator (signed; up - down)
    delta_p_link: np.ndarray        # (n_links,) MW per HVDC link
    delta_p_pst: np.ndarray         # (n_psts,) MW phase-shift equivalent
    new_line_flows: np.ndarray      # (L_sub,) MW per line post-redispatch
    binding_line_mask: np.ndarray   # (L_sub,) bool — lines that were >s_nom pre-redispatch
    feasible: bool
    cost: float                     # objective value (€)
    n_binding: int


def solve_snapshot(
    pre: RedispatchPrecomp,
    gen_p_da: np.ndarray,           # (G,) DA dispatch in MW
    gen_p_max: np.ndarray,          # (G,) available upper bound in MW (= p_nom * p_max_pu)
    gen_p_min: np.ndarray,          # (G,) available lower bound in MW (= p_nom * p_min_pu)
    link_p_da: np.ndarray,          # (n_links,) DA HVDC flows in MW
    line_flow_da: np.ndarray,       # (L_sub,) DA line flows in MW
    overload_tol: float = 1.001,
) -> SnapshotResult:
    """Solve one hour of the redispatch LP.

    If there is no overload, returns zero deltas without invoking the solver.
    If there is overload but the LP would otherwise be infeasible (structural
    bottlenecks), line-slack variables absorb the residual at penalty cost.
    """
    # ---- 1. Detect binding lines ----
    s_nom = pre.s_nom
    s_nom_safe = np.where(s_nom > 0, s_nom, np.inf)
    abs_flow = np.abs(line_flow_da)
    binding_mask_full = abs_flow > (s_nom_safe * overload_tol)
    binding_idx_full = np.flatnonzero(binding_mask_full)

    if binding_idx_full.size == 0:
        return SnapshotResult(
            delta_p_gen=np.zeros(len(pre.gen_ids), dtype=np.float32),
            delta_p_link=np.zeros(len(pre.link_ids), dtype=np.float32),
            delta_p_pst=np.zeros(len(pre.pst_ids), dtype=np.float32),
            new_line_flows=line_flow_da.copy(),
            binding_line_mask=binding_mask_full,
            feasible=True,
            cost=0.0,
            n_binding=0,
        )

    # Cap to the top-N most-overloaded lines (loading ratio)
    loadings = abs_flow[binding_idx_full] / s_nom_safe[binding_idx_full]
    if binding_idx_full.size > MAX_BINDING_LINES:
        topN = np.argsort(loadings)[::-1][:MAX_BINDING_LINES]
        binding_idx = binding_idx_full[topN]
    else:
        binding_idx = binding_idx_full
    n_bind = binding_idx.size

    # ---- 2. PTDF row block ----
    PTDF_bind = pre.ptdf[binding_idx, :]   # (n_bind, B_sub)

    # ---- 3. Filter generators by sensitivity ----
    gen_cols = pre.gen_bus_sub_col
    head_up = np.maximum(gen_p_max - gen_p_da, 0.0)
    head_dn = np.maximum(gen_p_da - gen_p_min, 0.0)

    in_sub = (gen_cols >= 0) & (pre.gen_p_nom >= MIN_GEN_MW) & ((head_up + head_dn) > 1e-3)
    if in_sub.any():
        max_sens_in = np.abs(PTDF_bind[:, gen_cols[in_sub]]).max(axis=0)
    else:
        max_sens_in = np.array([], dtype=np.float32)
    in_sub_idx = np.flatnonzero(in_sub)
    elig_mask_in_sub = max_sens_in >= PTDF_FILTER
    elig_idx = in_sub_idx[elig_mask_in_sub]

    # ---- 4. Aggregate by (bus_sub_col, carrier) ----
    if elig_idx.size:
        df = pd.DataFrame({
            "i": elig_idx,
            "bus_col": gen_cols[elig_idx],
            "carrier": pre.gen_carrier[elig_idx],
            "head_up": head_up[elig_idx],
            "head_dn": head_dn[elig_idx],
            "mc": pre.gen_mc[elig_idx],
            "weight": pre.gen_weight[elig_idx],
        })
        agg = (df.groupby(["bus_col", "carrier"], sort=False, observed=True)
                 .agg(head_up=("head_up", "sum"),
                      head_dn=("head_dn", "sum"),
                      mc=("mc", "mean"),
                      weight=("weight", "first"))
                 .reset_index())
        agg = agg[(agg["head_up"] + agg["head_dn"]) > 1e-3].reset_index(drop=True)
        # Lookup: per-eligible-gen → agg row index (for fast disaggregation)
        agg_keys = pd.MultiIndex.from_arrays([agg["bus_col"].values, agg["carrier"].values])
        gen_keys = pd.MultiIndex.from_arrays([df["bus_col"].values, df["carrier"].values])
        gen_agg_row = agg_keys.get_indexer(gen_keys)   # (-1 if dropped)
    else:
        agg = pd.DataFrame(columns=["bus_col", "carrier", "head_up", "head_dn", "mc", "weight"])
        gen_agg_row = np.array([], dtype=int)

    N_agg = len(agg)

    # ---- 5. Filter HVDC + PSTs ----
    link_cols = pre.link_bus0_sub_col
    link_in_sub = link_cols >= 0
    if link_in_sub.any():
        link_max_sens = np.abs(PTDF_bind[:, link_cols[link_in_sub]]).max(axis=0)
        link_in_idx = np.flatnonzero(link_in_sub)
        link_idx = link_in_idx[link_max_sens >= PTDF_FILTER]
    else:
        link_idx = np.array([], dtype=int)
    N_link = link_idx.size

    pst_b0 = pre.pst_bus0_sub_col
    pst_b1 = pre.pst_bus1_sub_col
    pst_in_sub = (pst_b0 >= 0) & (pst_b1 >= 0)
    if pst_in_sub.any():
        impact = PTDF_bind[:, pst_b0[pst_in_sub]] - PTDF_bind[:, pst_b1[pst_in_sub]]
        pst_max_sens = np.abs(impact).max(axis=0)
        pst_in_idx = np.flatnonzero(pst_in_sub)
        pst_idx = pst_in_idx[pst_max_sens >= PTDF_FILTER]
    else:
        pst_idx = np.array([], dtype=int)
    N_pst = pst_idx.size

    # ---- 6. Build LP (sparse) ----
    # Variable layout:
    #   [0 .. N_agg)                 Δp_up      ≥0
    #   [N_agg .. 2N_agg)            Δp_down    ≥0
    #   [2N_agg .. 2N_agg+N_link)    Δp_link    free (bounded)
    #   [.. +N_pst)                  Δp_pst     free (bounded)
    #   [.. +N_link)                 s_link     ≥0  (|Δp_link| slack)
    #   [.. +N_pst)                  s_pst      ≥0  (|Δp_pst|  slack)
    #   [.. +n_bind)                 s_up_line  ≥0  (upper line-flow slack)
    #   [.. +n_bind)                 s_dn_line  ≥0  (lower line-flow slack)
    nu = 2 * N_agg                        # gen up/down end
    nlink_e = nu + N_link                 # link end
    npst_e = nlink_e + N_pst              # pst end
    slink_e = npst_e + N_link             # s_link end
    spst_e = slink_e + N_pst              # s_pst end
    sup_e = spst_e + n_bind               # s_up_line end
    sdn_e = sup_e + n_bind                # s_dn_line end
    n_var = sdn_e

    # ---- Objective ----
    c = np.zeros(n_var, dtype=np.float64)
    if N_agg:
        coeff = (agg["weight"].values * agg["mc"].values).astype(np.float64)
        c[:N_agg] = coeff
        c[N_agg:nu] = coeff
    c[npst_e:slink_e] = HVDC_PEN
    c[slink_e:spst_e] = PST_PEN
    c[spst_e:sdn_e] = LINE_SLACK_COST

    # ---- Bounds ----
    bounds = []
    if N_agg:
        for hu in agg["head_up"].values:
            bounds.append((0.0, float(max(hu, 0.0))))
        for hd in agg["head_dn"].values:
            bounds.append((0.0, float(max(hd, 0.0))))
    for k in link_idx:
        pn = float(pre.link_p_nom[k])
        pda = float(link_p_da[k])
        bounds.append((-pn - pda, pn - pda))
    for k in pst_idx:
        swing = float(PST_SWING_FRAC * pre.pst_s_nom[k])
        bounds.append((-swing, swing))
    for _ in range(N_link):
        bounds.append((0.0, None))
    for _ in range(N_pst):
        bounds.append((0.0, None))
    for _ in range(2 * n_bind):
        bounds.append((0.0, None))   # line slack ≥0

    # ---- Equality constraint: balance ----
    eq_rows = []
    eq_cols = []
    eq_vals = []
    if N_agg:
        eq_rows.append(np.zeros(N_agg, dtype=np.int32))
        eq_cols.append(np.arange(N_agg, dtype=np.int32))
        eq_vals.append(np.ones(N_agg))
        eq_rows.append(np.zeros(N_agg, dtype=np.int32))
        eq_cols.append(np.arange(N_agg, nu, dtype=np.int32))
        eq_vals.append(-np.ones(N_agg))
    if N_link:
        eq_rows.append(np.zeros(N_link, dtype=np.int32))
        eq_cols.append(np.arange(nu, nlink_e, dtype=np.int32))
        eq_vals.append(-np.ones(N_link))
    if eq_rows:
        A_eq = csr_matrix(
            (np.concatenate(eq_vals), (np.concatenate(eq_rows), np.concatenate(eq_cols))),
            shape=(1, n_var),
        )
    else:
        A_eq = csr_matrix((1, n_var))
    b_eq = np.zeros(1)

    # ---- Inequality constraints (line, slacks for HVDC/PST) ----
    f_da = line_flow_da[binding_idx]
    s_b = s_nom[binding_idx]

    ur_list = []; uc_list = []; uv_list = []   # upper line block
    lr_list = []; lc_list = []; lv_list = []   # lower line block (= mirror)

    if N_agg:
        agg_cols = agg["bus_col"].values.astype(np.int32)
        ptdf_agg = PTDF_bind[:, agg_cols].astype(np.float64)
        nz = np.abs(ptdf_agg) > 1e-4
        rr, cc = np.nonzero(nz)
        vv = ptdf_agg[rr, cc]
        # up: +ptdf for Δp_up (col cc), -ptdf for Δp_down (col cc + N_agg)
        ur_list += [rr, rr]
        uc_list += [cc.astype(np.int32), (cc + N_agg).astype(np.int32)]
        uv_list += [vv, -vv]
        # low: mirror
        lr_list += [rr, rr]
        lc_list += [cc.astype(np.int32), (cc + N_agg).astype(np.int32)]
        lv_list += [-vv, vv]
    if N_link:
        ptdf_link = PTDF_bind[:, link_cols[link_idx]].astype(np.float64)
        nz = np.abs(ptdf_link) > 1e-4
        rr, cc = np.nonzero(nz)
        vv = ptdf_link[rr, cc]
        ur_list += [rr]; uc_list += [(cc + nu).astype(np.int32)]; uv_list += [-vv]
        lr_list += [rr]; lc_list += [(cc + nu).astype(np.int32)]; lv_list += [vv]
    if N_pst:
        ptdf_pst = (PTDF_bind[:, pst_b0[pst_idx]]
                    - PTDF_bind[:, pst_b1[pst_idx]]).astype(np.float64)
        nz = np.abs(ptdf_pst) > 1e-4
        rr, cc = np.nonzero(nz)
        vv = ptdf_pst[rr, cc]
        ur_list += [rr]; uc_list += [(cc + nlink_e).astype(np.int32)]; uv_list += [vv]
        lr_list += [rr]; lc_list += [(cc + nlink_e).astype(np.int32)]; lv_list += [-vv]

    # Line slack vars (one column per binding line)
    bind_range = np.arange(n_bind, dtype=np.int32)
    ur_list += [bind_range]; uc_list += [(spst_e + bind_range).astype(np.int32)]
    uv_list += [-np.ones(n_bind)]
    lr_list += [bind_range]; lc_list += [(sup_e + bind_range).astype(np.int32)]
    lv_list += [-np.ones(n_bind)]

    rows_u = np.concatenate(ur_list) if ur_list else np.array([], dtype=np.int32)
    cols_u = np.concatenate(uc_list) if uc_list else np.array([], dtype=np.int32)
    vals_u = np.concatenate(uv_list) if uv_list else np.array([])
    rows_l = np.concatenate(lr_list) if lr_list else np.array([], dtype=np.int32)
    cols_l = np.concatenate(lc_list) if lc_list else np.array([], dtype=np.int32)
    vals_l = np.concatenate(lv_list) if lv_list else np.array([])

    # Stack upper + lower line blocks: shift lower rows by n_bind
    rows_all = np.concatenate([rows_u, rows_l + n_bind])
    cols_all = np.concatenate([cols_u, cols_l])
    vals_all = np.concatenate([vals_u, vals_l])
    b_ub_lines = np.concatenate([s_b - f_da, s_b + f_da]).astype(np.float64)

    # HVDC + PST slack rows (no extra cost beyond their slack columns we already added)
    n_slack_rows = 2 * (N_link + N_pst)
    if n_slack_rows:
        s_rows = np.empty(4 * (N_link + N_pst), dtype=np.int32)
        s_cols = np.empty_like(s_rows)
        s_vals = np.empty(s_rows.size, dtype=np.float64)
        row_offset = 2 * n_bind
        k_off = 0
        if N_link:
            link_var_cols = np.arange(nu, nlink_e, dtype=np.int32)
            link_slk_cols = np.arange(npst_e, slink_e, dtype=np.int32)
            for i in range(N_link):
                s_rows[k_off:k_off + 4] = [row_offset, row_offset, row_offset + 1, row_offset + 1]
                s_cols[k_off:k_off + 4] = [link_var_cols[i], link_slk_cols[i],
                                            link_var_cols[i], link_slk_cols[i]]
                s_vals[k_off:k_off + 4] = [1.0, -1.0, -1.0, -1.0]
                k_off += 4
                row_offset += 2
        if N_pst:
            pst_var_cols = np.arange(nlink_e, npst_e, dtype=np.int32)
            pst_slk_cols = np.arange(slink_e, spst_e, dtype=np.int32)
            for i in range(N_pst):
                s_rows[k_off:k_off + 4] = [row_offset, row_offset, row_offset + 1, row_offset + 1]
                s_cols[k_off:k_off + 4] = [pst_var_cols[i], pst_slk_cols[i],
                                            pst_var_cols[i], pst_slk_cols[i]]
                s_vals[k_off:k_off + 4] = [1.0, -1.0, -1.0, -1.0]
                k_off += 4
                row_offset += 2
        rows_all = np.concatenate([rows_all, s_rows])
        cols_all = np.concatenate([cols_all, s_cols])
        vals_all = np.concatenate([vals_all, s_vals])
        b_ub = np.concatenate([b_ub_lines, np.zeros(n_slack_rows)])
    else:
        b_ub = b_ub_lines

    A_ub = csr_matrix((vals_all, (rows_all, cols_all)),
                      shape=(2 * n_bind + n_slack_rows, n_var))

    # ---- Solve ----
    res = linprog(
        c=c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
        bounds=bounds, method="highs",
        options={"presolve": True},
    )

    delta_gen = np.zeros(len(pre.gen_ids), dtype=np.float32)
    delta_link = np.zeros(len(pre.link_ids), dtype=np.float32)
    delta_pst = np.zeros(len(pre.pst_ids), dtype=np.float32)

    if not res.success:
        log.debug(f"LP infeasible (n_bind={n_bind}): {res.message}")
        return SnapshotResult(
            delta_p_gen=delta_gen,
            delta_p_link=delta_link,
            delta_p_pst=delta_pst,
            new_line_flows=line_flow_da.copy(),
            binding_line_mask=binding_mask_full,
            feasible=False,
            cost=0.0,
            n_binding=binding_idx_full.size,
        )

    x = res.x

    # ---- 7. Disaggregate Δp to per-generator (vectorised) ----
    if N_agg and elig_idx.size:
        dp_up_agg = x[:N_agg]
        dp_dn_agg = x[N_agg:nu]
        # totals per agg
        tot_up = agg["head_up"].values.astype(np.float64)
        tot_dn = agg["head_dn"].values.astype(np.float64)
        tot_up_safe = np.where(tot_up > 1e-6, tot_up, 1.0)
        tot_dn_safe = np.where(tot_dn > 1e-6, tot_dn, 1.0)
        # Pro-rata factors per eligible gen
        my_ar = gen_agg_row
        valid = my_ar >= 0
        ar = my_ar[valid]
        frac_up = head_up[elig_idx[valid]] / tot_up_safe[ar]
        frac_dn = head_dn[elig_idx[valid]] / tot_dn_safe[ar]
        delta_gen[elig_idx[valid]] = (frac_up * dp_up_agg[ar] - frac_dn * dp_dn_agg[ar]).astype(np.float32)

    if N_link:
        delta_link[link_idx] = x[nu:nlink_e].astype(np.float32)
    if N_pst:
        delta_pst[pst_idx] = x[nlink_e:npst_e].astype(np.float32)

    # ---- 8. New line flows (full L_sub) ----
    delta_bus = np.zeros(pre.ptdf.shape[1], dtype=np.float64)
    valid = pre.gen_bus_sub_col >= 0
    np.add.at(delta_bus, pre.gen_bus_sub_col[valid], delta_gen[valid])
    # HVDC: -delta_link at bus0
    for k_i, k in enumerate(link_idx):
        col = link_cols[k]
        if col >= 0:
            delta_bus[col] -= delta_link[k]
    # PST: +delta_pst at bus0, -delta_pst at bus1
    for k_i, k in enumerate(pst_idx):
        if pst_b0[k] >= 0:
            delta_bus[pst_b0[k]] += delta_pst[k]
        if pst_b1[k] >= 0:
            delta_bus[pst_b1[k]] -= delta_pst[k]
    new_flows = (line_flow_da + (pre.ptdf @ delta_bus).astype(np.float32)).astype(np.float32)

    return SnapshotResult(
        delta_p_gen=delta_gen,
        delta_p_link=delta_link,
        delta_p_pst=delta_pst,
        new_line_flows=new_flows,
        binding_line_mask=binding_mask_full,
        feasible=True,
        cost=float(res.fun),
        n_binding=binding_idx_full.size,
    )


# --------------------------------------------------------------------------- #
# Helper: compute per-snapshot generator headroom from PyPSA timeseries
# --------------------------------------------------------------------------- #
def headroom_arrays(n, t_index: int, gen_ids: np.ndarray):
    """For snapshot t_index, return (p_da, p_max, p_min) MW arrays aligned to gen_ids."""
    sn = n.snapshots[t_index]
    p_nom = n.generators.loc[gen_ids, "p_nom"].values.astype(np.float32)
    p_max_pu_static = n.generators.loc[gen_ids, "p_max_pu"].values.astype(np.float32)
    p_min_pu_static = n.generators.loc[gen_ids, "p_min_pu"].values.astype(np.float32)

    # Override with timeseries where present
    pmax_t = n.generators_t.p_max_pu
    if pmax_t is not None and len(pmax_t.columns) > 0:
        cols = pmax_t.columns.intersection(gen_ids)
        if len(cols):
            p_max_pu_static = p_max_pu_static.copy()
            row = pmax_t.loc[sn, cols].values.astype(np.float32)
            sel = np.isin(gen_ids, cols)
            p_max_pu_static[sel] = row
    pmin_t = n.generators_t.p_min_pu
    if pmin_t is not None and len(pmin_t.columns) > 0:
        cols = pmin_t.columns.intersection(gen_ids)
        if len(cols):
            p_min_pu_static = p_min_pu_static.copy()
            row = pmin_t.loc[sn, cols].values.astype(np.float32)
            sel = np.isin(gen_ids, cols)
            p_min_pu_static[sel] = row

    p_max = p_nom * p_max_pu_static
    p_min = p_nom * p_min_pu_static
    p_da = n.generators_t.p.loc[sn, gen_ids].values.astype(np.float32)
    return p_da, p_max, p_min


# --------------------------------------------------------------------------- #
# Single-stage combined solver (no multi-stage; per-hour dynamic N-1)
# --------------------------------------------------------------------------- #
COMBINED_SLACK_COST = 1.0e6     # €/MWh — only used when physically infeasible
COMBINED_N1_LOADING_THRESHOLD = 0.40   # contingencies = lines with current loading > 40%
COMBINED_N0_MONITOR_THRESHOLD = 0.90   # include lines with loading > 90% as N-0 monitored
                                       # (tightened; computational tractability)
COMBINED_MAX_CONTINGENCIES = 250       # safety cap; ranked by loading
COMBINED_MAX_N1_PAIRS = 400            # top-K most violated N-1 pairs per hour
COMBINED_PTDF_FILTER = 0.01            # eligibility threshold for unit redispatch
COMBINED_N1_RADIAL_GUARD = 0.97        # near-radial contingencies excluded


@dataclass
class CombinedResult:
    delta_p_gen: np.ndarray
    delta_p_link: np.ndarray
    delta_p_pst: np.ndarray
    new_line_flows: np.ndarray
    feasible: bool
    cost: float
    n_binding_n0: int
    n_binding_n1: int
    slack_mw: float
    n_contingencies: int


def build_line_endpoint_cols(pre, n):
    """Cache sub-bus-col indices for bus0/bus1 of every sub line (used by combined solver)."""
    if hasattr(pre, "line_bus0_sub_col"):
        return
    lb0 = np.array([pre.bus_idx_in_sub[b] for b in n.lines.loc[pre.sub_line_ids, "bus0"]], dtype=np.int64)
    lb1 = np.array([pre.bus_idx_in_sub[b] for b in n.lines.loc[pre.sub_line_ids, "bus1"]], dtype=np.int64)
    pre.line_bus0_sub_col = lb0
    pre.line_bus1_sub_col = lb1


def solve_snapshot_combined(
    pre: RedispatchPrecomp,
    gen_p_da: np.ndarray,
    gen_p_max: np.ndarray,
    gen_p_min: np.ndarray,
    link_p_da: np.ndarray,
    line_flow_da: np.ndarray,
    overload_tol: float = 1.001,
) -> CombinedResult:
    """One-shot LP that resolves ALL N-0 binding lines plus all N-1 violations among
    contingencies dynamically chosen as the lines whose pre-redispatch loading
    exceeds COMBINED_N1_LOADING_THRESHOLD (40%). Monitored set for N-1 = every line.
    Prohibitive slack (COMBINED_SLACK_COST = 1e6 €/MWh) so the LP only uses slack
    when no redispatch can physically resolve a constraint.

    Requires `build_line_endpoint_cols(pre, n)` to have been called once (caches
    bus0/bus1 sub-bus column indices on pre).
    """
    s_nom = pre.s_nom
    s_nom_safe = np.where(s_nom > 0, s_nom, np.inf)
    abs_flow = np.abs(line_flow_da)
    loading_da = abs_flow / s_nom_safe

    # ---- N-0 MONITORED set: include all lines loaded > 50% so the LP can't push
    # ---- a non-binding line over its limit by relieving binding ones. Lines
    # ---- ≤50% loading have far too much headroom for redispatch to overload them
    # ---- (PTDF max ≈ 1 and aggregated Δp on any one bus is bounded).
    n0_idx = np.flatnonzero(loading_da > COMBINED_N0_MONITOR_THRESHOLD)
    n_n0 = n0_idx.size

    # ---- Dynamic contingency set: lines currently loaded > 40% ----
    cont_idx = np.flatnonzero(loading_da > COMBINED_N1_LOADING_THRESHOLD)
    if cont_idx.size > COMBINED_MAX_CONTINGENCIES:
        order = np.argsort(loading_da[cont_idx])[::-1][:COMBINED_MAX_CONTINGENCIES]
        cont_idx = cont_idx[order]
    n_cont = cont_idx.size

    # ---- LODF for monitored = ALL lines, contingency = cont_idx ----
    # LODF[m, c] = (PTDF[m, b0c] - PTDF[m, b1c]) / (1 - (PTDF[c, b0c] - PTDF[c, b1c]))
    n1_pairs_m, n1_pairs_c, n1_pairs_lodf, n1_pairs_fbase = [], [], [], []
    if n_cont > 0:
        b0c = pre.line_bus0_sub_col[cont_idx]
        b1c = pre.line_bus1_sub_col[cont_idx]
        # H = ptdf[:, b0c] - ptdf[:, b1c]   (L_sub, n_cont)
        H = (pre.ptdf[:, b0c] - pre.ptdf[:, b1c]).astype(np.float32)
        self_c = H[cont_idx, np.arange(n_cont)]                      # (n_cont,)
        denom = 1.0 - self_c
        valid_c = np.abs(denom) > (1.0 - COMBINED_N1_RADIAL_GUARD)
        denom_safe = np.where(valid_c, denom, 1.0).astype(np.float32)
        lodf = (H / denom_safe[None, :]).astype(np.float32)
        lodf[cont_idx, np.arange(n_cont)] = -1.0
        # Screen N-1 violations: f_post[m, c] = f_m + LODF[m, c] * f_c
        f_c = line_flow_da[cont_idx]
        fpost = line_flow_da[:, None] + lodf * f_c[None, :]          # (L_sub, n_cont)
        # mask: m == c invalid; near-radial c invalid
        mask = np.abs(fpost) > s_nom_safe[:, None] * overload_tol
        mask[:, ~valid_c] = False
        mask[cont_idx, np.arange(n_cont)] = False
        m_idx, c_idx = np.nonzero(mask)
        if m_idx.size > COMBINED_MAX_N1_PAIRS:
            sev = np.abs(fpost[m_idx, c_idx]) / s_nom_safe[m_idx]
            top = np.argsort(sev)[::-1][:COMBINED_MAX_N1_PAIRS]
            m_idx, c_idx = m_idx[top], c_idx[top]
        n1_pairs_m = m_idx
        n1_pairs_c = c_idx
        n1_pairs_lodf = lodf[m_idx, c_idx]
        n1_pairs_fbase = fpost[m_idx, c_idx]
    n_n1 = len(n1_pairs_m)

    if n_n0 == 0 and n_n1 == 0:
        return CombinedResult(
            delta_p_gen=np.zeros(len(pre.gen_ids), np.float32),
            delta_p_link=np.zeros(len(pre.link_ids), np.float32),
            delta_p_pst=np.zeros(len(pre.pst_ids), np.float32),
            new_line_flows=line_flow_da.copy(),
            feasible=True, cost=0.0, n_binding_n0=0, n_binding_n1=0,
            slack_mw=0.0, n_contingencies=n_cont,
        )

    # ---- Assemble generic constraint rows R (n_con, B_sub) ----
    R_blocks, fbase_list, slim_list = [], [], []
    if n_n0:
        R_blocks.append(pre.ptdf[n0_idx, :].astype(np.float64))
        fbase_list.append(line_flow_da[n0_idx].astype(np.float64))
        slim_list.append(s_nom[n0_idx].astype(np.float64))
    if n_n1:
        # N-1 row m,c: R = ptdf[m] + LODF[m,c]·ptdf[c]
        R_n1 = (pre.ptdf[n1_pairs_m, :].astype(np.float64)
                + np.asarray(n1_pairs_lodf, np.float64)[:, None]
                  * pre.ptdf[n1_pairs_c, :].astype(np.float64))
        R_blocks.append(R_n1)
        fbase_list.append(np.asarray(n1_pairs_fbase, np.float64))
        slim_list.append(s_nom[n1_pairs_m].astype(np.float64))
    R = np.vstack(R_blocks)
    f_base = np.concatenate(fbase_list)
    s_lim = np.concatenate(slim_list)
    n_con = R.shape[0]

    # ---- Eligible generators ----
    gen_cols = pre.gen_bus_sub_col
    head_up = np.maximum(gen_p_max - gen_p_da, 0.0)
    head_dn = np.maximum(gen_p_da - gen_p_min, 0.0)
    in_sub = (gen_cols >= 0) & (pre.gen_p_nom >= MIN_GEN_MW) & ((head_up + head_dn) > 1e-3)
    in_sub_idx = np.flatnonzero(in_sub)
    if in_sub_idx.size:
        max_sens = np.abs(R[:, gen_cols[in_sub_idx]]).max(axis=0)
        elig_idx = in_sub_idx[max_sens >= COMBINED_PTDF_FILTER]
    else:
        elig_idx = np.array([], dtype=int)

    # ---- Aggregate by (bus_col, carrier) ----
    if elig_idx.size:
        df = pd.DataFrame({
            "i": elig_idx, "bus_col": gen_cols[elig_idx],
            "carrier": pre.gen_carrier[elig_idx],
            "head_up": head_up[elig_idx], "head_dn": head_dn[elig_idx],
            "mc": pre.gen_mc[elig_idx], "weight": pre.gen_weight[elig_idx],
        })
        agg = (df.groupby(["bus_col", "carrier"], sort=False, observed=True)
                 .agg(head_up=("head_up", "sum"), head_dn=("head_dn", "sum"),
                      mc=("mc", "mean"), weight=("weight", "first")).reset_index())
        agg = agg[(agg["head_up"] + agg["head_dn"]) > 1e-3].reset_index(drop=True)
        agg_keys = pd.MultiIndex.from_arrays([agg["bus_col"].values, agg["carrier"].values])
        gen_keys = pd.MultiIndex.from_arrays([df["bus_col"].values, df["carrier"].values])
        gen_agg_row = agg_keys.get_indexer(gen_keys)
    else:
        agg = pd.DataFrame(columns=["bus_col", "carrier", "head_up", "head_dn", "mc", "weight"])
        gen_agg_row = np.array([], dtype=int)
    N_agg = len(agg)

    # ---- HVDC + PST eligibility ----
    link_cols = pre.link_bus0_sub_col
    link_in = link_cols >= 0
    if link_in.any():
        lin_idx = np.flatnonzero(link_in)
        link_idx = lin_idx[np.abs(R[:, link_cols[lin_idx]]).max(axis=0) >= COMBINED_PTDF_FILTER]
    else:
        link_idx = np.array([], dtype=int)
    N_link = link_idx.size

    pst_b0 = pre.pst_bus0_sub_col; pst_b1 = pre.pst_bus1_sub_col
    pst_in = (pst_b0 >= 0) & (pst_b1 >= 0)
    if pst_in.any():
        pin = np.flatnonzero(pst_in)
        impact = R[:, pst_b0[pin]] - R[:, pst_b1[pin]]
        pst_idx = pin[np.abs(impact).max(axis=0) >= COMBINED_PTDF_FILTER]
    else:
        pst_idx = np.array([], dtype=int)
    N_pst = pst_idx.size

    # ---- Variable layout ----
    nu = 2 * N_agg
    nlink_e = nu + N_link
    npst_e = nlink_e + N_pst
    slink_e = npst_e + N_link
    spst_e = slink_e + N_pst
    sup_e = spst_e + n_con
    sdn_e = sup_e + n_con
    n_var = sdn_e

    c = np.zeros(n_var)
    if N_agg:
        coeff = (agg["weight"].values * agg["mc"].values).astype(np.float64)
        c[:N_agg] = coeff; c[N_agg:nu] = coeff
    c[npst_e:slink_e] = HVDC_PEN
    c[slink_e:spst_e] = PST_PEN
    c[spst_e:sdn_e] = COMBINED_SLACK_COST

    bounds = []
    if N_agg:
        for hu in agg["head_up"].values:
            bounds.append((0.0, float(max(hu, 0.0))))
        for hd in agg["head_dn"].values:
            bounds.append((0.0, float(max(hd, 0.0))))
    for k in link_idx:
        pn = float(pre.link_p_nom[k]); pda = float(link_p_da[k])
        bounds.append((-pn - pda, pn - pda))
    for k in pst_idx:
        sw = float(PST_SWING_FRAC * pre.pst_s_nom[k])
        bounds.append((-sw, sw))
    for _ in range(N_link): bounds.append((0.0, None))
    for _ in range(N_pst): bounds.append((0.0, None))
    for _ in range(2 * n_con): bounds.append((0.0, None))

    # Balance equality
    eq_r, eq_c, eq_v = [], [], []
    if N_agg:
        eq_r.append(np.zeros(N_agg, np.int32)); eq_c.append(np.arange(N_agg, dtype=np.int32)); eq_v.append(np.ones(N_agg))
        eq_r.append(np.zeros(N_agg, np.int32)); eq_c.append(np.arange(N_agg, nu, dtype=np.int32)); eq_v.append(-np.ones(N_agg))
    if N_link:
        eq_r.append(np.zeros(N_link, np.int32)); eq_c.append(np.arange(nu, nlink_e, dtype=np.int32)); eq_v.append(-np.ones(N_link))
    A_eq = (csr_matrix((np.concatenate(eq_v), (np.concatenate(eq_r), np.concatenate(eq_c))), shape=(1, n_var))
            if eq_r else csr_matrix((1, n_var)))
    b_eq = np.zeros(1)

    # Constraint rows (upper + lower) — same generic builder as solve_snapshot_n1
    ur_r, ur_c, ur_v = [], [], []
    lr_r, lr_c, lr_v = [], [], []
    if N_agg:
        agg_cols = agg["bus_col"].values.astype(np.int32)
        Ragg = R[:, agg_cols]
        nz = np.abs(Ragg) > 1e-4
        rr, cc = np.nonzero(nz); vv = Ragg[rr, cc]
        ur_r += [rr, rr]; ur_c += [cc.astype(np.int32), (cc + N_agg).astype(np.int32)]; ur_v += [vv, -vv]
        lr_r += [rr, rr]; lr_c += [cc.astype(np.int32), (cc + N_agg).astype(np.int32)]; lr_v += [-vv, vv]
    if N_link:
        Rl = R[:, link_cols[link_idx]]
        nz = np.abs(Rl) > 1e-4; rr, cc = np.nonzero(nz); vv = Rl[rr, cc]
        ur_r += [rr]; ur_c += [(cc + nu).astype(np.int32)]; ur_v += [-vv]
        lr_r += [rr]; lr_c += [(cc + nu).astype(np.int32)]; lr_v += [vv]
    if N_pst:
        Rp = R[:, pst_b0[pst_idx]] - R[:, pst_b1[pst_idx]]
        nz = np.abs(Rp) > 1e-4; rr, cc = np.nonzero(nz); vv = Rp[rr, cc]
        ur_r += [rr]; ur_c += [(cc + nlink_e).astype(np.int32)]; ur_v += [vv]
        lr_r += [rr]; lr_c += [(cc + nlink_e).astype(np.int32)]; lr_v += [-vv]
    rng = np.arange(n_con, dtype=np.int32)
    ur_r += [rng]; ur_c += [(spst_e + rng).astype(np.int32)]; ur_v += [-np.ones(n_con)]
    lr_r += [rng]; lr_c += [(sup_e + rng).astype(np.int32)]; lr_v += [-np.ones(n_con)]

    rows_u = np.concatenate(ur_r); cols_u = np.concatenate(ur_c); vals_u = np.concatenate(ur_v)
    rows_l = np.concatenate(lr_r); cols_l = np.concatenate(lr_c); vals_l = np.concatenate(lr_v)
    rows_all = np.concatenate([rows_u, rows_l + n_con])
    cols_all = np.concatenate([cols_u, cols_l])
    vals_all = np.concatenate([vals_u, vals_l])
    b_ub_lines = np.concatenate([s_lim - f_base, s_lim + f_base])

    n_slack_rows = 2 * (N_link + N_pst)
    if n_slack_rows:
        s_rows = np.empty(4 * (N_link + N_pst), np.int32); s_cols = np.empty_like(s_rows)
        s_vals = np.empty(s_rows.size); row_off = 2 * n_con; ko = 0
        if N_link:
            lvc = np.arange(nu, nlink_e, dtype=np.int32); lsc = np.arange(npst_e, slink_e, dtype=np.int32)
            for i in range(N_link):
                s_rows[ko:ko+4] = [row_off, row_off, row_off+1, row_off+1]
                s_cols[ko:ko+4] = [lvc[i], lsc[i], lvc[i], lsc[i]]
                s_vals[ko:ko+4] = [1.0, -1.0, -1.0, -1.0]; ko += 4; row_off += 2
        if N_pst:
            pvc = np.arange(nlink_e, npst_e, dtype=np.int32); psc = np.arange(slink_e, spst_e, dtype=np.int32)
            for i in range(N_pst):
                s_rows[ko:ko+4] = [row_off, row_off, row_off+1, row_off+1]
                s_cols[ko:ko+4] = [pvc[i], psc[i], pvc[i], psc[i]]
                s_vals[ko:ko+4] = [1.0, -1.0, -1.0, -1.0]; ko += 4; row_off += 2
        rows_all = np.concatenate([rows_all, s_rows]); cols_all = np.concatenate([cols_all, s_cols])
        vals_all = np.concatenate([vals_all, s_vals])
        b_ub = np.concatenate([b_ub_lines, np.zeros(n_slack_rows)])
    else:
        b_ub = b_ub_lines

    A_ub = csr_matrix((vals_all, (rows_all, cols_all)), shape=(2 * n_con + n_slack_rows, n_var))

    res = linprog(c=c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                  bounds=bounds, method="highs", options={"presolve": True})

    delta_gen = np.zeros(len(pre.gen_ids), np.float32)
    delta_link = np.zeros(len(pre.link_ids), np.float32)
    delta_pst = np.zeros(len(pre.pst_ids), np.float32)
    if not res.success:
        return CombinedResult(delta_gen, delta_link, delta_pst, line_flow_da.copy(),
                              False, 0.0, n_n0, n_n1, 0.0, n_cont)
    x = res.x
    if N_agg and elig_idx.size:
        dp_up = x[:N_agg]; dp_dn = x[N_agg:nu]
        tu = np.where(agg["head_up"].values > 1e-6, agg["head_up"].values, 1.0)
        td = np.where(agg["head_dn"].values > 1e-6, agg["head_dn"].values, 1.0)
        valid = gen_agg_row >= 0; ar = gen_agg_row[valid]
        fu = head_up[elig_idx[valid]] / tu[ar]; fd = head_dn[elig_idx[valid]] / td[ar]
        delta_gen[elig_idx[valid]] = (fu * dp_up[ar] - fd * dp_dn[ar]).astype(np.float32)
    if N_link: delta_link[link_idx] = x[nu:nlink_e].astype(np.float32)
    if N_pst: delta_pst[pst_idx] = x[nlink_e:npst_e].astype(np.float32)

    delta_bus = np.zeros(pre.ptdf.shape[1])
    vg = pre.gen_bus_sub_col >= 0
    np.add.at(delta_bus, pre.gen_bus_sub_col[vg], delta_gen[vg])
    for k in link_idx:
        col = link_cols[k]
        if col >= 0: delta_bus[col] -= delta_link[k]
    for k in pst_idx:
        if pst_b0[k] >= 0: delta_bus[pst_b0[k]] += delta_pst[k]
        if pst_b1[k] >= 0: delta_bus[pst_b1[k]] -= delta_pst[k]
    new_flows = (line_flow_da + (pre.ptdf @ delta_bus).astype(np.float32)).astype(np.float32)

    slack_mw = float(x[spst_e:sdn_e].sum())
    return CombinedResult(delta_gen, delta_link, delta_pst, new_flows,
                          True, float(res.fun), n_n0, n_n1, slack_mw, n_cont)


# --------------------------------------------------------------------------- #
# Stage 2: N-0 + N-1 security redispatch
# --------------------------------------------------------------------------- #
# In the second pass we drop the cheap slack: the objective is to actually
# RESOLVE the bottleneck (base case AND single-contingency) wherever physically
# possible, ramping conventionals first and RES last (Mindestfaktor preserved).
# A prohibitively expensive slack is kept only so the LP never goes infeasible —
# residual slack then flags a structurally insecure element that no redispatch
# can fix (needs grid expansion / topology measures), not an economic choice.
N1_SLACK_COST = 1.0e5     # €/MWh — prohibitive; only used when physically unavoidable
N1_MAX_BINDING_N0 = 150   # cap on N-0 binding lines constrained per hour
N1_MAX_PAIRS = 300        # cap on screened N-1 (monitored, contingency) pairs per hour
N1_RADIAL_GUARD = 0.97    # skip contingencies whose self-term ≥ this (outage ≈ islanding)
# Stage 2 reaches FURTHER than stage 1 (0.02): real redispatch recruits distant units to
# relieve meshed corridors. 0.005 ≈ 2.5x more curtailable RES on meshed lines with no extra
# residual overload (verified). Lower still (0.001) over-curtails distant RES for tiny relief.
N1_PTDF_FILTER = 0.005    # |PTDF| eligibility threshold for the security stage


def compute_lodf(crit_ptdf, crit_bus0_col, crit_bus1_col):
    """Line Outage Distribution Factors among a curated critical line set.

    Args:
        crit_ptdf:      (n_crit, B_sub) PTDF rows for the critical lines.
        crit_bus0_col:  (n_crit,) sub-bus column index of each critical line's bus0.
        crit_bus1_col:  (n_crit,) sub-bus column index of each critical line's bus1.

    Returns:
        lodf:     (n_crit, n_crit) LODF[m, c] — fraction of pre-outage flow on
                  contingency line c that redistributes onto monitored line m.
        valid_c:  (n_crit,) bool — contingency usable (not near-radial).
    """
    # H[m, c] = PTDF of monitored line m to a unit injection transfer across line c
    H = crit_ptdf[:, crit_bus0_col] - crit_ptdf[:, crit_bus1_col]   # (n_crit, n_crit)
    self_c = np.diag(H).copy()                                       # (n_crit,)
    denom = 1.0 - self_c
    valid_c = np.abs(denom) > (1.0 - N1_RADIAL_GUARD)
    denom_safe = np.where(valid_c, denom, 1.0)
    lodf = (H / denom_safe[None, :]).astype(np.float32)
    np.fill_diagonal(lodf, -1.0)
    return lodf, valid_c


@dataclass
class SnapshotResultN1:
    delta_p_gen: np.ndarray
    delta_p_link: np.ndarray
    delta_p_pst: np.ndarray
    new_line_flows: np.ndarray      # (L_sub,) base-case flow after stage-2 Δp
    feasible: bool
    cost: float
    n_binding_n0: int               # N-0 lines overloaded at input (pre stage-2)
    n_binding_n1: int               # screened N-1 violated (monitored,contingency) pairs
    slack_mw: float                 # total residual slack used (MW·constraints) — insecurity flag


def solve_snapshot_n1(
    pre: RedispatchPrecomp,
    gen_p_base: np.ndarray,         # (G,) dispatch AFTER stage 1 (= p_da + Δp1)
    gen_p_max: np.ndarray,
    gen_p_min: np.ndarray,
    link_p_base: np.ndarray,        # (n_links,) HVDC flow after stage 1
    line_flow_base: np.ndarray,     # (L_sub,) base-case line flow after stage 1
    crit_idx: np.ndarray,           # (n_crit,) indices into sub_line space
    crit_ptdf: np.ndarray,          # (n_crit, B_sub) PTDF rows of critical lines
    lodf: np.ndarray,               # (n_crit, n_crit)
    lodf_valid_c: np.ndarray,       # (n_crit,) bool
    overload_tol: float = 1.001,
) -> SnapshotResultN1:
    """Second-stage redispatch enforcing N-0 (all lines) + N-1 (critical set).

    Builds one generic linear constraint per binding element:
        -s_lim ≤ f_base + R · Δp_bus ≤ s_lim
    where for N-0 rows R = PTDF row, f_base = base flow; for N-1 rows
    R = PTDF_m + LODF[m,c]·PTDF_c, f_base = f_m + LODF[m,c]·f_c.
    """
    s_nom = pre.s_nom
    s_nom_safe = np.where(s_nom > 0, s_nom, np.inf)

    # ---- N-0 binding lines (all lines) ----
    abs_flow = np.abs(line_flow_base)
    n0_mask = abs_flow > (s_nom_safe * overload_tol)
    n0_idx = np.flatnonzero(n0_mask)
    if n0_idx.size > N1_MAX_BINDING_N0:
        order = np.argsort(abs_flow[n0_idx] / s_nom_safe[n0_idx])[::-1][:N1_MAX_BINDING_N0]
        n0_idx = n0_idx[order]
    n_n0 = n0_idx.size

    # ---- N-1 screening over critical set ----
    f_crit = line_flow_base[crit_idx]                           # (n_crit,)
    s_crit = s_nom[crit_idx]
    s_crit_safe = np.where(s_crit > 0, s_crit, np.inf)
    # post-contingency flow matrix: fpost[m, c] = f_m + LODF[m,c]·f_c
    fpost = f_crit[:, None] + lodf * f_crit[None, :]            # (n_crit, n_crit)
    over = np.abs(fpost) > (s_crit_safe[:, None] * overload_tol)
    over[:, ~lodf_valid_c] = False                              # skip near-radial contingencies
    np.fill_diagonal(over, False)                              # m == c not a contingency for itself
    m_pairs, c_pairs = np.nonzero(over)
    if m_pairs.size > N1_MAX_PAIRS:
        sev = np.abs(fpost[m_pairs, c_pairs]) / s_crit_safe[m_pairs]
        keep = np.argsort(sev)[::-1][:N1_MAX_PAIRS]
        m_pairs, c_pairs = m_pairs[keep], c_pairs[keep]
    n_n1 = m_pairs.size

    if n_n0 == 0 and n_n1 == 0:
        return SnapshotResultN1(
            delta_p_gen=np.zeros(len(pre.gen_ids), np.float32),
            delta_p_link=np.zeros(len(pre.link_ids), np.float32),
            delta_p_pst=np.zeros(len(pre.pst_ids), np.float32),
            new_line_flows=line_flow_base.copy(),
            feasible=True, cost=0.0, n_binding_n0=0, n_binding_n1=0, slack_mw=0.0,
        )

    # ---- Assemble generic constraint rows R (n_con, B_sub), baseline, limit ----
    R_blocks = []
    fbase_list = []
    slim_list = []
    if n_n0:
        R_blocks.append(pre.ptdf[n0_idx, :].astype(np.float64))
        fbase_list.append(line_flow_base[n0_idx].astype(np.float64))
        slim_list.append(s_nom[n0_idx].astype(np.float64))
    if n_n1:
        R_n1 = (crit_ptdf[m_pairs, :].astype(np.float64)
                + lodf[m_pairs, c_pairs][:, None] * crit_ptdf[c_pairs, :].astype(np.float64))
        R_blocks.append(R_n1)
        fbase_list.append(fpost[m_pairs, c_pairs].astype(np.float64))
        slim_list.append(s_crit[m_pairs].astype(np.float64))
    R = np.vstack(R_blocks)                       # (n_con, B_sub)
    f_base = np.concatenate(fbase_list)
    s_lim = np.concatenate(slim_list)
    n_con = R.shape[0]

    # ---- Eligible generators (sensitivity to any constraint row) ----
    gen_cols = pre.gen_bus_sub_col
    head_up = np.maximum(gen_p_max - gen_p_base, 0.0)
    head_dn = np.maximum(gen_p_base - gen_p_min, 0.0)
    in_sub = (gen_cols >= 0) & (pre.gen_p_nom >= MIN_GEN_MW) & ((head_up + head_dn) > 1e-3)
    in_sub_idx = np.flatnonzero(in_sub)
    if in_sub_idx.size:
        max_sens = np.abs(R[:, gen_cols[in_sub_idx]]).max(axis=0)
        elig_idx = in_sub_idx[max_sens >= N1_PTDF_FILTER]
    else:
        elig_idx = np.array([], dtype=int)

    # ---- Aggregate eligible gens by (bus_col, carrier) ----
    if elig_idx.size:
        df = pd.DataFrame({
            "i": elig_idx, "bus_col": gen_cols[elig_idx],
            "carrier": pre.gen_carrier[elig_idx],
            "head_up": head_up[elig_idx], "head_dn": head_dn[elig_idx],
            "mc": pre.gen_mc[elig_idx], "weight": pre.gen_weight[elig_idx],
        })
        agg = (df.groupby(["bus_col", "carrier"], sort=False, observed=True)
                 .agg(head_up=("head_up", "sum"), head_dn=("head_dn", "sum"),
                      mc=("mc", "mean"), weight=("weight", "first")).reset_index())
        agg = agg[(agg["head_up"] + agg["head_dn"]) > 1e-3].reset_index(drop=True)
        agg_keys = pd.MultiIndex.from_arrays([agg["bus_col"].values, agg["carrier"].values])
        gen_keys = pd.MultiIndex.from_arrays([df["bus_col"].values, df["carrier"].values])
        gen_agg_row = agg_keys.get_indexer(gen_keys)
    else:
        agg = pd.DataFrame(columns=["bus_col", "carrier", "head_up", "head_dn", "mc", "weight"])
        gen_agg_row = np.array([], dtype=int)
    N_agg = len(agg)

    # ---- HVDC + PST filtering (sensitivity to constraint rows) ----
    link_cols = pre.link_bus0_sub_col
    link_in = link_cols >= 0
    if link_in.any():
        lin_idx = np.flatnonzero(link_in)
        link_idx = lin_idx[np.abs(R[:, link_cols[lin_idx]]).max(axis=0) >= N1_PTDF_FILTER]
    else:
        link_idx = np.array([], dtype=int)
    N_link = link_idx.size

    pst_b0 = pre.pst_bus0_sub_col; pst_b1 = pre.pst_bus1_sub_col
    pst_in = (pst_b0 >= 0) & (pst_b1 >= 0)
    if pst_in.any():
        pin = np.flatnonzero(pst_in)
        impact = R[:, pst_b0[pin]] - R[:, pst_b1[pin]]
        pst_idx = pin[np.abs(impact).max(axis=0) >= N1_PTDF_FILTER]
    else:
        pst_idx = np.array([], dtype=int)
    N_pst = pst_idx.size

    # ---- Variable layout (same as stage 1) ----
    nu = 2 * N_agg
    nlink_e = nu + N_link
    npst_e = nlink_e + N_pst
    slink_e = npst_e + N_link
    spst_e = slink_e + N_pst
    sup_e = spst_e + n_con
    sdn_e = sup_e + n_con
    n_var = sdn_e

    c = np.zeros(n_var)
    if N_agg:
        coeff = (agg["weight"].values * agg["mc"].values).astype(np.float64)
        c[:N_agg] = coeff; c[N_agg:nu] = coeff
    c[npst_e:slink_e] = HVDC_PEN
    c[slink_e:spst_e] = PST_PEN
    c[spst_e:sdn_e] = N1_SLACK_COST

    bounds = []
    if N_agg:
        for hu in agg["head_up"].values:
            bounds.append((0.0, float(max(hu, 0.0))))
        for hd in agg["head_dn"].values:
            bounds.append((0.0, float(max(hd, 0.0))))
    for k in link_idx:
        pn = float(pre.link_p_nom[k]); pb = float(link_p_base[k])
        bounds.append((-pn - pb, pn - pb))
    for k in pst_idx:
        sw = float(PST_SWING_FRAC * pre.pst_s_nom[k])
        bounds.append((-sw, sw))
    for _ in range(N_link): bounds.append((0.0, None))
    for _ in range(N_pst): bounds.append((0.0, None))
    for _ in range(2 * n_con): bounds.append((0.0, None))

    # Balance: Σup - Σdn - Σlink = 0
    eq_r, eq_c, eq_v = [], [], []
    if N_agg:
        eq_r.append(np.zeros(N_agg, np.int32)); eq_c.append(np.arange(N_agg, dtype=np.int32)); eq_v.append(np.ones(N_agg))
        eq_r.append(np.zeros(N_agg, np.int32)); eq_c.append(np.arange(N_agg, nu, dtype=np.int32)); eq_v.append(-np.ones(N_agg))
    if N_link:
        eq_r.append(np.zeros(N_link, np.int32)); eq_c.append(np.arange(nu, nlink_e, dtype=np.int32)); eq_v.append(-np.ones(N_link))
    A_eq = (csr_matrix((np.concatenate(eq_v), (np.concatenate(eq_r), np.concatenate(eq_c))), shape=(1, n_var))
            if eq_r else csr_matrix((1, n_var)))
    b_eq = np.zeros(1)

    # Constraint rows: upper (R·Δp ≤ s - f) and lower (-R·Δp ≤ s + f)
    ur_r, ur_c, ur_v = [], [], []
    lr_r, lr_c, lr_v = [], [], []
    if N_agg:
        agg_cols = agg["bus_col"].values.astype(np.int32)
        Ragg = R[:, agg_cols]
        nz = np.abs(Ragg) > 1e-4
        rr, cc = np.nonzero(nz); vv = Ragg[rr, cc]
        ur_r += [rr, rr]; ur_c += [cc.astype(np.int32), (cc + N_agg).astype(np.int32)]; ur_v += [vv, -vv]
        lr_r += [rr, rr]; lr_c += [cc.astype(np.int32), (cc + N_agg).astype(np.int32)]; lr_v += [-vv, vv]
    if N_link:
        Rl = R[:, link_cols[link_idx]]
        nz = np.abs(Rl) > 1e-4; rr, cc = np.nonzero(nz); vv = Rl[rr, cc]
        ur_r += [rr]; ur_c += [(cc + nu).astype(np.int32)]; ur_v += [-vv]
        lr_r += [rr]; lr_c += [(cc + nu).astype(np.int32)]; lr_v += [vv]
    if N_pst:
        Rp = R[:, pst_b0[pst_idx]] - R[:, pst_b1[pst_idx]]
        nz = np.abs(Rp) > 1e-4; rr, cc = np.nonzero(nz); vv = Rp[rr, cc]
        ur_r += [rr]; ur_c += [(cc + nlink_e).astype(np.int32)]; ur_v += [vv]
        lr_r += [rr]; lr_c += [(cc + nlink_e).astype(np.int32)]; lr_v += [-vv]
    rng = np.arange(n_con, dtype=np.int32)
    ur_r += [rng]; ur_c += [(spst_e + rng).astype(np.int32)]; ur_v += [-np.ones(n_con)]
    lr_r += [rng]; lr_c += [(sup_e + rng).astype(np.int32)]; lr_v += [-np.ones(n_con)]

    rows_u = np.concatenate(ur_r); cols_u = np.concatenate(ur_c); vals_u = np.concatenate(ur_v)
    rows_l = np.concatenate(lr_r); cols_l = np.concatenate(lr_c); vals_l = np.concatenate(lr_v)
    rows_all = np.concatenate([rows_u, rows_l + n_con])
    cols_all = np.concatenate([cols_u, cols_l])
    vals_all = np.concatenate([vals_u, vals_l])
    b_ub_lines = np.concatenate([s_lim - f_base, s_lim + f_base])

    # HVDC/PST |Δp| ≤ slack rows
    n_slack_rows = 2 * (N_link + N_pst)
    if n_slack_rows:
        s_rows = np.empty(4 * (N_link + N_pst), np.int32); s_cols = np.empty_like(s_rows)
        s_vals = np.empty(s_rows.size); row_off = 2 * n_con; ko = 0
        if N_link:
            lvc = np.arange(nu, nlink_e, dtype=np.int32); lsc = np.arange(npst_e, slink_e, dtype=np.int32)
            for i in range(N_link):
                s_rows[ko:ko+4] = [row_off, row_off, row_off+1, row_off+1]
                s_cols[ko:ko+4] = [lvc[i], lsc[i], lvc[i], lsc[i]]
                s_vals[ko:ko+4] = [1.0, -1.0, -1.0, -1.0]; ko += 4; row_off += 2
        if N_pst:
            pvc = np.arange(nlink_e, npst_e, dtype=np.int32); psc = np.arange(slink_e, spst_e, dtype=np.int32)
            for i in range(N_pst):
                s_rows[ko:ko+4] = [row_off, row_off, row_off+1, row_off+1]
                s_cols[ko:ko+4] = [pvc[i], psc[i], pvc[i], psc[i]]
                s_vals[ko:ko+4] = [1.0, -1.0, -1.0, -1.0]; ko += 4; row_off += 2
        rows_all = np.concatenate([rows_all, s_rows]); cols_all = np.concatenate([cols_all, s_cols])
        vals_all = np.concatenate([vals_all, s_vals])
        b_ub = np.concatenate([b_ub_lines, np.zeros(n_slack_rows)])
    else:
        b_ub = b_ub_lines

    A_ub = csr_matrix((vals_all, (rows_all, cols_all)), shape=(2 * n_con + n_slack_rows, n_var))

    res = linprog(c=c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                  bounds=bounds, method="highs", options={"presolve": True})

    delta_gen = np.zeros(len(pre.gen_ids), np.float32)
    delta_link = np.zeros(len(pre.link_ids), np.float32)
    delta_pst = np.zeros(len(pre.pst_ids), np.float32)
    if not res.success:
        return SnapshotResultN1(delta_gen, delta_link, delta_pst, line_flow_base.copy(),
                                False, 0.0, n_n0, n_n1, 0.0)
    x = res.x
    if N_agg and elig_idx.size:
        dp_up = x[:N_agg]; dp_dn = x[N_agg:nu]
        tu = np.where(agg["head_up"].values > 1e-6, agg["head_up"].values, 1.0)
        td = np.where(agg["head_dn"].values > 1e-6, agg["head_dn"].values, 1.0)
        valid = gen_agg_row >= 0; ar = gen_agg_row[valid]
        fu = head_up[elig_idx[valid]] / tu[ar]; fd = head_dn[elig_idx[valid]] / td[ar]
        delta_gen[elig_idx[valid]] = (fu * dp_up[ar] - fd * dp_dn[ar]).astype(np.float32)
    if N_link: delta_link[link_idx] = x[nu:nlink_e].astype(np.float32)
    if N_pst: delta_pst[pst_idx] = x[nlink_e:npst_e].astype(np.float32)

    delta_bus = np.zeros(pre.ptdf.shape[1])
    vg = pre.gen_bus_sub_col >= 0
    np.add.at(delta_bus, pre.gen_bus_sub_col[vg], delta_gen[vg])
    for k in link_idx:
        col = link_cols[k]
        if col >= 0: delta_bus[col] -= delta_link[k]
    for k in pst_idx:
        if pst_b0[k] >= 0: delta_bus[pst_b0[k]] += delta_pst[k]
        if pst_b1[k] >= 0: delta_bus[pst_b1[k]] -= delta_pst[k]
    new_flows = (line_flow_base + (pre.ptdf @ delta_bus).astype(np.float32)).astype(np.float32)

    slack_mw = float(x[spst_e:sdn_e].sum())
    return SnapshotResultN1(delta_gen, delta_link, delta_pst, new_flows,
                            True, float(res.fun), n_n0, n_n1, slack_mw)
