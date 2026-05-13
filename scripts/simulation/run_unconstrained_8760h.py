#!/usr/bin/env python3
"""
run_unconstrained_8760h.py - Annual unconstrained dispatch on grid_beta.

Runs the v5-calibrated MILP unit-commitment dispatch from
``merit_order_comparison.py`` for the full 8,760 h of 2025, then maps the
fuel-level dispatch onto every individual generator/storage unit of the
grid_beta topology and writes the result as a single PyPSA netCDF.

Pipeline:
    1. Reuse merit_order_comparison.run_milp_uc(...)  (calibrated v5 dispatch)
    2. Build a full-topology PyPSA network from grid_beta (no LP)
    3. Disaggregate fuel-level dispatch onto per-generator hourly p
    4. Inject load timeseries, storage discharge, uniform clearing price
    5. Save  results/dispatch_8760h.nc

The dispatch is "copperplate": no line constraints, no redispatch, single
clearing price per snapshot. The full grid_beta topology (12,911 lines,
567 transformers, 14 HVDC links) is preserved in the saved network so
that downstream power flow analysis can be run on top of the dispatch.

Caches MILP output to results/.dispatch_cache_v5.npz so re-runs are fast.

Usage:
    conda activate egon2025
    python scripts/simulation/run_unconstrained_8760h.py            # full year
    python scripts/simulation/run_unconstrained_8760h.py --smoke    # 30-h smoke test
    python scripts/simulation/run_unconstrained_8760h.py --reuse    # reuse cached MILP
"""

import argparse
import logging
import os
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Make merit_order_comparison importable
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent.parent))

import pypsa  # noqa: E402

import merit_order_comparison as moc  # noqa: E402

DB_URL = "postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data"
SCN = "grid_beta"
YEAR = 2025
N_HOURS = 8760

OUTDIR = Path("/root/egon_2025_project/results")
OUTDIR.mkdir(exist_ok=True)
NC_PATH = OUTDIR / "dispatch_8760h.nc"
CACHE_PATH = OUTDIR / ".dispatch_cache_v5.npz"

# Mapping from merit_order fuel groups → grid_beta generator carriers
FUEL_TO_CARRIERS = {
    "solar": ["solar"],
    "wind_onshore": ["onwind"],
    "wind_offshore": ["offwind"],
    "biomass": ["biogas", "biomass", "waste"],
    "hydro": ["run_of_river", "reservoir"],
    "gas": ["gas_ccgt", "gas_chp", "hydrogen"],
    "hard_coal": ["coal"],
    "lignite": ["lignite"],
    "oil": ["oil"],
    "other_conventional": ["other"],
    "imports": [
        "import_FR", "import_AT", "import_CH", "import_NL", "import_DK",
        "import_PL", "import_CZ", "import_NO", "import_SE", "import_BE",
        "import_LU",
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1 — RUN OR REUSE MILP DISPATCH
# ─────────────────────────────────────────────────────────────────────────────
def run_or_load_milp(reuse=False, smoke=False):
    """Run merit_order_comparison MILP UC, or load cached arrays.

    Returns:
        clearing_price: (8760,) EUR/MWh
        dispatch_by_fuel: dict[str, np.ndarray (8760,)]
        demand: (8760,) MW (SMARD load)
        smard: dict (SMARD downloads, used by report)
    """
    if reuse and CACHE_PATH.exists():
        log.info(f"Loading cached MILP dispatch from {CACHE_PATH}")
        z = np.load(CACHE_PATH, allow_pickle=True)
        clearing_price = z["clearing_price"]
        demand = z["demand"]
        dispatch_by_fuel = {k: z[f"d_{k}"] for k in moc.FUEL_ORDER if f"d_{k}" in z.files}
        if "d_oil" in z.files:
            dispatch_by_fuel["oil"] = z["d_oil"]
        ts = pd.date_range(f"{YEAR}-01-01", periods=N_HOURS, freq="h", tz="UTC")
        smard_clean = {}
        for key in z.files:
            if not key.startswith("smard_"):
                continue
            short = key[len("smard_"):]
            smard_clean[short] = pd.Series(z[key], index=ts)
        return clearing_price, dispatch_by_fuel, demand, smard_clean

    log.info("Step 1/4: Downloading SMARD + Energy-Charts data...")
    smard = moc.download_all_smard()

    log.info("Step 2/4: Preparing demand from SMARD...")
    demand = moc.prepare_smard_demand(smard)

    if smoke:
        # Smoke: zero out the dispatch beyond first 30 h to make MILP fast.
        # We'll just run the full MILP on a 30 h demand instead.
        # Easiest: monkey-patch N_HOURS in the moc module to 30.
        log.info("SMOKE MODE: limiting MILP to first 30 h")
        moc.N_HOURS = 30
        demand = demand[:30]
        # The merit_order code uses moc.N_HOURS for output array sizing; this is enough.

    log.info("Step 3/4: Running MILP unit-commitment dispatch (this is the slow step)")
    t0 = time.time()
    clearing_price, dispatch_by_fuel, _ = moc.run_milp_uc(*moc.load_model(), demand)
    log.info(f"MILP finished in {(time.time()-t0)/60:.1f} min")

    # Cache (only full-year; smoke skips cache)
    if not smoke:
        log.info(f"Caching dispatch to {CACHE_PATH}")
        save = {"clearing_price": clearing_price, "demand": demand}
        for k, v in dispatch_by_fuel.items():
            save[f"d_{k}"] = v
        for k, v in smard.items():
            if hasattr(v, "values"):
                save[f"smard_{k}"] = v.values
        np.savez_compressed(CACHE_PATH, **save)

    return clearing_price, dispatch_by_fuel, demand, smard


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2 — BUILD FULL TOPOLOGY PYPSA NETWORK
# ─────────────────────────────────────────────────────────────────────────────
def load_full_network(engine, snapshots):
    """Build full-topology PyPSA network from grid_beta. No LP runs on this network."""
    log.info("Loading full grid_beta topology from database...")

    buses = pd.read_sql(f"SELECT * FROM grid.egon_etrago_bus WHERE scn_name='{SCN}'", engine)
    lines = pd.read_sql(f"SELECT * FROM grid.egon_etrago_line WHERE scn_name='{SCN}'", engine)
    trafos = pd.read_sql(f"SELECT * FROM grid.egon_etrago_transformer WHERE scn_name='{SCN}'", engine)
    gens = pd.read_sql(f"SELECT * FROM grid.egon_etrago_generator WHERE scn_name='{SCN}'", engine)
    loads = pd.read_sql(f"SELECT * FROM grid.egon_etrago_load WHERE scn_name='{SCN}'", engine)
    links = pd.read_sql(f"SELECT * FROM grid.egon_etrago_link WHERE scn_name='{SCN}'", engine)
    storage = pd.read_sql(f"SELECT * FROM grid.egon_etrago_storage WHERE scn_name='{SCN}'", engine)

    log.info(
        f"  {len(buses)} buses, {len(lines)} lines, {len(trafos)} trafos, "
        f"{len(gens)} gens, {len(loads)} loads, {len(links)} links, "
        f"{len(storage)} storage"
    )

    n = pypsa.Network()
    n.set_snapshots(snapshots)

    # Buses
    buses = buses.set_index("bus_id")
    n.madd(
        "Bus", buses.index.astype(str),
        v_nom=buses["v_nom"].values,
        x=buses["x"].values, y=buses["y"].values,
        carrier="AC",
        country=buses["country"].values,
    )

    # Lines (filter x > 0 for power-flow validity)
    valid_lines = lines[lines["x"] > 0].copy()
    log.info(f"  Lines with x>0: {len(valid_lines)}/{len(lines)}")
    n.madd(
        "Line", valid_lines["line_id"].astype(str),
        bus0=valid_lines["bus0"].astype(str).values,
        bus1=valid_lines["bus1"].astype(str).values,
        x=valid_lines["x"].values, r=valid_lines["r"].values,
        s_nom=valid_lines["s_nom"].values,
        s_nom_extendable=False,
        s_max_pu=1e6,           # effectively unconstrained
        length=valid_lines["length"].values if "length" in valid_lines.columns else 0.0,
    )

    # Transformers
    valid_tx = trafos[trafos["x"] > 0].copy()
    log.info(f"  Transformers with x>0: {len(valid_tx)}/{len(trafos)}")
    n.madd(
        "Transformer", valid_tx["trafo_id"].astype(str),
        bus0=valid_tx["bus0"].astype(str).values,
        bus1=valid_tx["bus1"].astype(str).values,
        x=valid_tx["x"].values, r=valid_tx["r"].values,
        s_nom=valid_tx["s_nom"].values,
        s_nom_extendable=False,
        s_max_pu=1e6,
    )

    # Links (HVDC)
    if len(links):
        n.madd(
            "Link", links["link_id"].astype(str),
            bus0=links["bus0"].astype(str).values,
            bus1=links["bus1"].astype(str).values,
            p_nom=links["p_nom"].values,
            efficiency=links.get("efficiency", pd.Series([1.0] * len(links))).fillna(1.0).values,
            carrier=links["carrier"].values if "carrier" in links.columns else "DC",
        )

    # Generators
    n.madd(
        "Generator", gens["generator_id"].astype(str),
        bus=gens["bus"].astype(str).values,
        carrier=gens["carrier"].values,
        p_nom=gens["p_nom"].values,
        marginal_cost=gens["marginal_cost"].fillna(0.0).values,
        efficiency=gens["efficiency"].fillna(1.0).values if "efficiency" in gens.columns else 1.0,
    )

    # Loads
    n.madd(
        "Load", loads["load_id"].astype(str),
        bus=loads["bus"].astype(str).values,
        carrier=loads["carrier"].values if "carrier" in loads.columns else "AC",
    )

    # Storage units (PSP + battery)
    if len(storage):
        n.madd(
            "StorageUnit", storage["storage_id"].astype(str),
            bus=storage["bus"].astype(str).values,
            carrier=storage["carrier"].values,
            p_nom=storage["p_nom"].values,
            max_hours=storage["max_hours"].fillna(6.0).values,
            efficiency_store=storage["efficiency_store"].fillna(0.866).values,
            efficiency_dispatch=storage["efficiency_dispatch"].fillna(0.866).values,
        )

    return n, gens, loads, storage


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3 — LOAD GENERATOR + LOAD TIMESERIES (8760 h)
# ─────────────────────────────────────────────────────────────────────────────
def load_timeseries(engine, n_snapshots):
    """Load 8760-h p_max_pu and load p_set arrays from DB. Returns DataFrames."""
    log.info("Loading generator p_max_pu timeseries...")
    gen_ts = pd.read_sql(
        f"""SELECT generator_id, p_max_pu, p_min_pu
            FROM grid.egon_etrago_generator_timeseries
            WHERE scn_name='{SCN}'""",
        engine,
    )
    log.info(f"  Generator timeseries: {len(gen_ts)} units")

    log.info("Loading load p_set timeseries...")
    load_ts = pd.read_sql(
        f"""SELECT load_id, p_set
            FROM grid.egon_etrago_load_timeseries
            WHERE scn_name='{SCN}'""",
        engine,
    )
    log.info(f"  Load timeseries: {len(load_ts)} units")
    return gen_ts, load_ts


def build_pmax_matrix(gen_ts, gens, n_snapshots):
    """Build (n_snapshots, n_gens) p_max_pu matrix indexed by gen_id (str).

    Generators without a timeseries get a flat 1.0 (dispatchable / no profile).
    Imports get 1.0 (always available up to NTC).
    """
    gen_ids = gens["generator_id"].astype(str).values
    pmax = pd.DataFrame(
        np.ones((n_snapshots, len(gen_ids)), dtype=np.float32),
        columns=gen_ids,
    )
    if len(gen_ts) == 0:
        return pmax

    for _, row in gen_ts.iterrows():
        gid = str(row["generator_id"])
        raw = row["p_max_pu"]
        if raw is None:
            continue
        arr = np.atleast_1d(np.asarray(raw, dtype=np.float32))
        if arr.size == 0:
            continue
        if arr.size >= n_snapshots:
            pmax[gid] = arr[:n_snapshots]
        else:
            # short series — pad with last value
            pmax[gid] = np.concatenate(
                [arr, np.full(n_snapshots - arr.size, arr[-1], dtype=np.float32)]
            )
    return pmax


def build_load_matrix(load_ts, loads, n_snapshots):
    """Build (n_snapshots, n_loads) load p_set matrix in MW."""
    load_ids = loads["load_id"].astype(str).values
    mat = pd.DataFrame(
        np.zeros((n_snapshots, len(load_ids)), dtype=np.float32),
        columns=load_ids,
    )
    for _, row in load_ts.iterrows():
        lid = str(row["load_id"])
        if lid not in mat.columns:
            continue
        raw = row["p_set"]
        if raw is None:
            continue
        arr = np.atleast_1d(np.asarray(raw, dtype=np.float32))
        if arr.size == 0:
            continue
        if arr.size >= n_snapshots:
            mat[lid] = arr[:n_snapshots]
        else:
            mat[lid] = np.concatenate(
                [arr, np.full(n_snapshots - arr.size, arr[-1], dtype=np.float32)]
            )
    return mat


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4 — DISAGGREGATE FUEL-LEVEL DISPATCH ONTO PER-GENERATOR
# ─────────────────────────────────────────────────────────────────────────────
def disaggregate_dispatch(n, gens_df, dispatch_by_fuel, pmax_matrix):
    """Distribute fuel-group dispatch arrays onto each generator.

    For each fuel group's (8760,) dispatch array, allocate to all matching
    grid_beta generators proportionally to p_max_pu × p_nom at each hour.
    Pumped storage is handled separately on storage_units.

    Returns the (n_snapshots, n_gens) dispatched-power dataframe.
    """
    log.info("Disaggregating fuel-level dispatch onto per-generator hourly p...")
    n_snap = len(n.snapshots)
    gen_ids = gens_df["generator_id"].astype(str).values
    n_gens = len(gen_ids)

    # Build static availability table  (n_gens x 1) of p_nom
    pnom = gens_df.set_index(gens_df["generator_id"].astype(str))["p_nom"].astype(np.float32)
    pnom_arr = pnom.reindex(gen_ids).fillna(0.0).values

    carriers = gens_df.set_index(gens_df["generator_id"].astype(str))["carrier"]
    carriers = carriers.reindex(gen_ids)

    p_dispatch = np.zeros((n_snap, n_gens), dtype=np.float32)

    # Convert pmax_matrix to ndarray ordered the same as gen_ids
    pmax_arr = pmax_matrix.reindex(columns=gen_ids).fillna(1.0).values  # (n_snap, n_gens)

    for fuel, target_carriers in FUEL_TO_CARRIERS.items():
        if fuel not in dispatch_by_fuel:
            continue
        target_arr = dispatch_by_fuel[fuel].astype(np.float64)
        if target_arr.sum() <= 0:
            continue

        # Mask of which generators belong to this fuel group
        mask = carriers.isin(target_carriers).values  # (n_gens,)
        idx = np.where(mask)[0]
        if len(idx) == 0:
            log.warning(f"  Fuel {fuel}: no generators in grid_beta for carriers {target_carriers}")
            continue

        # Available MW per gen per hour  (n_snap, |idx|)
        avail = pmax_arr[:, idx] * pnom_arr[idx]   # broadcasting
        avail_sum = avail.sum(axis=1)              # (n_snap,)

        # Hours where avail > 0 → distribute by share of avail
        # Hours where avail == 0 but target > 0 → distribute by p_nom share
        share = np.zeros_like(avail)
        nonzero = avail_sum > 1e-3
        share[nonzero] = avail[nonzero] / avail_sum[nonzero, None]

        # Fallback: pure p_nom share when nothing available
        zero_mask = ~nonzero & (target_arr[:n_snap] > 0)
        if zero_mask.any():
            pnom_subset = pnom_arr[idx]
            pnom_total = pnom_subset.sum()
            if pnom_total > 0:
                share[zero_mask] = pnom_subset / pnom_total

        # Allocate
        target_h = target_arr[:n_snap, None]   # (n_snap, 1)
        p_dispatch[:, idx] = (share * target_h).astype(np.float32)

        twh = p_dispatch[:, idx].sum() / 1e6
        log.info(f"  {fuel:>22s}: {twh:7.2f} TWh across {len(idx):>5d} gens "
                 f"(target={target_arr.sum()/1e6:.2f})")

    df = pd.DataFrame(p_dispatch, index=n.snapshots, columns=gen_ids)
    return df


def disaggregate_storage(n, storage_df, ps_dispatch):
    """Distribute pumped_storage discharge across the 28 PSP units.

    Battery storage (2,416 units, 2.6 GW total) is left at zero for this
    unconstrained run — the merit_order MILP only tracks aggregate PSP.
    """
    log.info("Disaggregating storage dispatch...")
    n_snap = len(n.snapshots)
    sids = storage_df["storage_id"].astype(str).values
    p_dis = pd.DataFrame(np.zeros((n_snap, len(sids)), dtype=np.float32), index=n.snapshots, columns=sids)
    p_chg = pd.DataFrame(np.zeros((n_snap, len(sids)), dtype=np.float32), index=n.snapshots, columns=sids)

    psp_mask = storage_df["carrier"].values == "pumped_hydro"
    psp_ids = sids[psp_mask]
    psp_pnom = storage_df.loc[psp_mask, "p_nom"].astype(np.float32).values
    if len(psp_ids) == 0 or psp_pnom.sum() == 0:
        return p_dis, p_chg

    weights = psp_pnom / psp_pnom.sum()  # (n_psp,)

    # Discharge: split aggregate by p_nom share
    psp_arr = ps_dispatch[:n_snap].astype(np.float32)
    p_dis.loc[:, psp_ids] = (psp_arr[:, None] * weights[None, :]).astype(np.float32)

    # Charge: estimate from energy balance (round-trip 0.75)
    # Total charge = total discharge / sqrt(0.75) approx
    # Distribute evenly across hours where dispatch == 0
    total_disc = psp_arr.sum()
    total_chg = total_disc / 0.75
    no_disc = psp_arr < 1.0
    if no_disc.any():
        per_hour_chg = total_chg / no_disc.sum()
        chg_total = np.zeros(n_snap, dtype=np.float32)
        chg_total[no_disc] = per_hour_chg
        # Cap at p_nom_total
        cap = psp_pnom.sum()
        chg_total = np.minimum(chg_total, cap)
        p_chg.loc[:, psp_ids] = (chg_total[:, None] * weights[None, :]).astype(np.float32)

    log.info(f"  PSP: discharge {total_disc/1e6:.2f} TWh, charge {total_chg/1e6:.2f} TWh "
             f"across {len(psp_ids)} units")
    return p_dis, p_chg


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true",
                        help="Run a 30-h smoke test instead of full year")
    parser.add_argument("--reuse", action="store_true",
                        help="Reuse cached MILP dispatch arrays if available")
    parser.add_argument("--skip-save", action="store_true",
                        help="Skip writing the netCDF file")
    args = parser.parse_args()

    n_hours = 30 if args.smoke else N_HOURS
    nc_path = OUTDIR / ("dispatch_smoke30h.nc" if args.smoke else "dispatch_8760h.nc")

    log.info("=" * 78)
    log.info(f"  Annual unconstrained dispatch — grid_beta — {n_hours} h")
    log.info("=" * 78)

    # Step 1 — MILP dispatch
    clearing_price, dispatch_by_fuel, demand, smard = run_or_load_milp(
        reuse=args.reuse, smoke=args.smoke
    )

    # Truncate everything to n_hours
    clearing_price = clearing_price[:n_hours]
    demand = demand[:n_hours]
    dispatch_by_fuel = {k: v[:n_hours] for k, v in dispatch_by_fuel.items()}

    # Step 2 — Build full topology
    snapshots = pd.date_range(f"{YEAR}-01-01", periods=n_hours, freq="h")
    engine = create_engine(DB_URL)
    n, gens_df, loads_df, storage_df = load_full_network(engine, snapshots)

    # Step 3 — Load timeseries
    gen_ts, load_ts = load_timeseries(engine, n_hours)
    pmax_matrix = build_pmax_matrix(gen_ts, gens_df, n_hours)
    load_matrix = build_load_matrix(load_ts, loads_df, n_hours)
    pmax_matrix.index = snapshots
    load_matrix.index = snapshots

    # Step 4 — Disaggregate dispatch
    p_gen = disaggregate_dispatch(n, gens_df, dispatch_by_fuel, pmax_matrix)
    p_dis, p_chg = disaggregate_storage(n, storage_df, dispatch_by_fuel.get(
        "pumped_storage", np.zeros(n_hours)))

    # Inject results
    n.generators_t.p = p_gen
    n.generators_t.p_max_pu = pmax_matrix.astype(np.float32)
    n.loads_t.p_set = load_matrix.astype(np.float32)
    n.loads_t.p = load_matrix.astype(np.float32)
    n.storage_units_t.p_dispatch = p_dis
    n.storage_units_t.p_store = p_chg
    n.storage_units_t.p = (p_dis - p_chg).astype(np.float32)
    # Marginal price uniform across all buses (copperplate)
    bus_ids = list(n.buses.index)
    n.buses_t.marginal_price = pd.DataFrame(
        np.broadcast_to(clearing_price[:, None], (n_hours, len(bus_ids))).copy(),
        index=snapshots, columns=bus_ids,
    ).astype(np.float32)

    # Sanity prints
    total_gen = n.generators_t.p.values.sum() / 1e6
    total_load = n.loads_t.p.values.sum() / 1e6
    total_psp = n.storage_units_t.p_dispatch.values.sum() / 1e6
    log.info(f"\nSanity check:")
    log.info(f"  Total generation: {total_gen:.2f} TWh")
    log.info(f"  Total load:       {total_load:.2f} TWh")
    log.info(f"  PSP discharge:    {total_psp:.2f} TWh")
    log.info(f"  Clearing price:   mean={clearing_price.mean():.1f}  "
             f"min={clearing_price.min():.1f}  max={clearing_price.max():.1f} EUR/MWh")

    # Save
    if not args.skip_save:
        log.info(f"Saving netCDF to {nc_path}")
        # PyPSA can choke on duplicated indices; ensure clean
        n.export_to_netcdf(str(nc_path))
        log.info(f"  File size: {nc_path.stat().st_size / 1e6:.1f} MB")

    log.info("Done.")


if __name__ == "__main__":
    main()
