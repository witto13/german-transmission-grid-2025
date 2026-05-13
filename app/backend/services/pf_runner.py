"""Power flow runner.

Adapted from scripts/simulation/run_12month_lopf.py. Loads the grid_beta
network from PostgreSQL, slices the 8760-hour timeseries arrays at the
requested date range, and runs LOPF (CBC) or linear PF (PyPSA built-in).

Jobs run in a background thread. Status is in-memory (one process); results
are persisted to results/pf_jobs/{job_id}.json so they survive a server
restart.
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
import traceback
import uuid
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import pypsa
from sqlalchemy import text

from ..db import SCN, YEAR, get_engine

log = logging.getLogger(__name__)

JOB_DIR = pathlib.Path("/root/egon_2025_project/results/pf_jobs")
JOB_DIR.mkdir(parents=True, exist_ok=True)

# In-memory job registry
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_run_lock = threading.Lock()  # serialize PF runs (one at a time)


def _hour_of_year(dt: datetime) -> int:
    """0-based hour-of-year for the configured YEAR."""
    base = pd.Timestamp(year=YEAR, month=1, day=1)
    diff = pd.Timestamp(dt) - base
    return int(diff.total_seconds() // 3600)


def _set_state(job_id: str, **kw):
    with _jobs_lock:
        if job_id not in _jobs:
            return
        _jobs[job_id].update(kw)
        _jobs[job_id]["updated_at"] = time.time()


def submit(start: datetime, end: datetime, mode: str = "lopf",
           aggregate: bool = True) -> str:
    """Queue a PF job; returns the job_id immediately. Runs in a daemon thread.

    `aggregate=True` groups generators by (bus, carrier) before solving — this
    is essential for grid_beta because it has 18,792 individual SEL generators,
    which makes the LP intractable for interactive use. Aggregation preserves
    total capacity, weighted-average marginal cost, and weighted p_min/p_max.
    """
    if mode not in ("lopf", "pf"):
        raise ValueError("mode must be 'lopf' or 'pf'")

    snapshots = pd.date_range(start, end, freq="h")
    if len(snapshots) < 1:
        raise ValueError("Date range must contain at least one hour")
    if len(snapshots) > 8760:
        raise ValueError("Date range exceeds 8760 hours (full year)")

    job_id = uuid.uuid4().hex[:10]
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "state": "queued",
            "progress": 0,
            "message": "Queued",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "mode": mode,
            "aggregate": aggregate,
            "n_snapshots": len(snapshots),
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    t = threading.Thread(target=_run_job, args=(job_id, snapshots, mode, aggregate),
                         daemon=True)
    t.start()
    return job_id


def status(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        return _jobs.get(job_id)


def result(job_id: str) -> Optional[dict]:
    f = JOB_DIR / f"{job_id}.json"
    if f.exists():
        return json.loads(f.read_text())
    return None


def list_jobs() -> list[dict]:
    with _jobs_lock:
        return sorted(_jobs.values(), key=lambda j: -j["created_at"])


# ── Network construction ────────────────────────────────────────────────────


def _aggregate_generators(gens: pd.DataFrame, gen_ts: pd.DataFrame,
                          n_hours: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Group generators by (bus, carrier). Pre-compute capacity-weighted timeseries.

    For each (bus, carrier) group we sum p_nom and produce a single row with:
    - p_nom: sum
    - marginal_cost: capacity-weighted mean (weighted by p_nom)
    - efficiency: capacity-weighted mean
    - timeseries (p_max_pu, p_min_pu): capacity-weighted average across members
    """
    gens = gens.copy()
    gens["p_nom"] = gens["p_nom"].astype(float).fillna(0)
    gens["marginal_cost"] = gens["marginal_cost"].astype(float).fillna(0)
    gens["efficiency"] = gens["efficiency"].astype(float).fillna(1.0)
    gens["p_min_pu"] = gens["p_min_pu"].astype(float).fillna(0.0)
    gens["p_max_pu"] = gens["p_max_pu"].astype(float).fillna(1.0)

    # Build aggregated static frame
    grouped = gens.groupby(["bus", "carrier"], dropna=False)
    agg_static = grouped.apply(lambda g: pd.Series({
        "p_nom": g["p_nom"].sum(),
        "marginal_cost": (
            (g["marginal_cost"] * g["p_nom"]).sum() / g["p_nom"].sum()
            if g["p_nom"].sum() > 0 else g["marginal_cost"].mean()
        ),
        "efficiency": (
            (g["efficiency"] * g["p_nom"]).sum() / g["p_nom"].sum()
            if g["p_nom"].sum() > 0 else g["efficiency"].mean()
        ),
        "p_min_pu_static": g["p_min_pu"].mean(),
        "p_max_pu_static": g["p_max_pu"].mean(),
        "member_ids": list(g["generator_id"].astype(int)),
    }), include_groups=False).reset_index()
    agg_static["generator_id"] = (
        agg_static["bus"].astype(str) + "_" + agg_static["carrier"].fillna("none")
    )

    # Build aggregated timeseries (capacity-weighted)
    member_to_group = {}
    member_pnom = {}
    for _, row in agg_static.iterrows():
        for mid in row["member_ids"]:
            member_to_group[mid] = row["generator_id"]
            member_pnom[mid] = (
                gens.loc[gens["generator_id"] == mid, "p_nom"].iloc[0]
                if (gens["generator_id"] == mid).any() else 0
            )

    agg_pmax: dict[str, np.ndarray] = {}
    agg_pmin: dict[str, np.ndarray] = {}
    agg_weights_pmax: dict[str, float] = {}
    agg_weights_pmin: dict[str, float] = {}

    for _, row in gen_ts.iterrows():
        gid = int(row["generator_id"])
        grp = member_to_group.get(gid)
        if grp is None:
            continue
        w = member_pnom.get(gid, 0)
        if w <= 0:
            continue
        for src_key, target, weights in (
            ("pmax_slice", agg_pmax, agg_weights_pmax),
            ("pmin_slice", agg_pmin, agg_weights_pmin),
        ):
            sl = row[src_key]
            if sl is None or len(sl) == 0:
                continue
            arr = np.array(sl, dtype=float)
            if len(arr) < n_hours:
                arr = np.concatenate([arr, np.full(n_hours - len(arr), arr[-1])])
            arr = arr[:n_hours]
            if grp not in target:
                target[grp] = arr * w
                weights[grp] = w
            else:
                target[grp] = target[grp] + arr * w
                weights[grp] = weights[grp] + w

    agg_pmax_norm = {g: a / agg_weights_pmax[g] for g, a in agg_pmax.items()
                     if agg_weights_pmax[g] > 0}
    agg_pmin_norm = {g: a / agg_weights_pmin[g] for g, a in agg_pmin.items()
                     if agg_weights_pmin[g] > 0}

    # Build a "fake" gen_ts DataFrame with aggregated rows
    agg_ts_rows = []
    for grp in set(list(agg_pmax_norm.keys()) + list(agg_pmin_norm.keys())):
        agg_ts_rows.append({
            "generator_id_str": grp,
            "pmax_slice": agg_pmax_norm.get(grp),
            "pmin_slice": agg_pmin_norm.get(grp),
        })
    agg_ts = pd.DataFrame(agg_ts_rows)

    return agg_static, agg_ts


def _build_network(snapshots: pd.DatetimeIndex, hour_indices: list[int],
                   progress_cb, aggregate: bool = True) -> pypsa.Network:
    """Build PyPSA network from grid_beta DB at the given snapshots/hours.

    When `aggregate=True`, generators are grouped by (bus, carrier) — drastically
    reduces LP size for grid_beta (18792 → ~few hundred).
    """
    engine = get_engine()
    progress_cb(5, "Loading static components")

    with engine.connect() as conn:
        buses = pd.read_sql(
            text("SELECT * FROM grid.egon_etrago_bus WHERE scn_name = :scn"),
            conn, params={"scn": SCN},
        )
        lines = pd.read_sql(
            text("SELECT * FROM grid.egon_etrago_line WHERE scn_name = :scn"),
            conn, params={"scn": SCN},
        )
        trafos = pd.read_sql(
            text("SELECT * FROM grid.egon_etrago_transformer WHERE scn_name = :scn"),
            conn, params={"scn": SCN},
        )
        gens = pd.read_sql(
            text("SELECT * FROM grid.egon_etrago_generator WHERE scn_name = :scn"),
            conn, params={"scn": SCN},
        )
        loads_df = pd.read_sql(
            text("SELECT * FROM grid.egon_etrago_load WHERE scn_name = :scn"),
            conn, params={"scn": SCN},
        )
        links = pd.read_sql(
            text("SELECT * FROM grid.egon_etrago_link WHERE scn_name = :scn"),
            conn, params={"scn": SCN},
        )

    progress_cb(15, f"Slicing timeseries for {len(hour_indices)} hours")

    # PG arrays are 1-indexed and inclusive on both ends
    h_start = hour_indices[0] + 1
    h_end = hour_indices[-1] + 1

    with engine.connect() as conn:
        load_ts = pd.read_sql(
            text(f"""
                SELECT load_id, p_set[{h_start}:{h_end}] AS slice
                FROM grid.egon_etrago_load_timeseries
                WHERE scn_name = :scn
            """),
            conn, params={"scn": SCN},
        )
        gen_ts = pd.read_sql(
            text(f"""
                SELECT generator_id,
                       p_max_pu[{h_start}:{h_end}] AS pmax_slice,
                       p_min_pu[{h_start}:{h_end}] AS pmin_slice
                FROM grid.egon_etrago_generator_timeseries
                WHERE scn_name = :scn
            """),
            conn, params={"scn": SCN},
        )

    progress_cb(25, "Assembling PyPSA network")
    n = pypsa.Network()
    n.set_snapshots(snapshots)

    buses = buses.set_index("bus_id")
    n.madd("Bus", buses.index.astype(str),
           v_nom=buses["v_nom"].values,
           x=buses["x"].values, y=buses["y"].values,
           carrier="AC", country=buses["country"].values)

    valid = lines[lines["x"].astype(float) > 0].copy()
    n.madd("Line", valid["line_id"].astype(str),
           bus0=valid["bus0"].astype(str).values,
           bus1=valid["bus1"].astype(str).values,
           x=valid["x"].astype(float).values,
           r=valid["r"].astype(float).values,
           s_nom=valid["s_nom"].astype(float).values,
           length=valid["length"].fillna(0).values,
           v_nom=valid["v_nom"].values,
           s_max_pu=1e6)

    trafos = trafos.copy()
    bad_x = trafos["x"].abs() < 1e-6
    if bad_x.any():
        trafos.loc[bad_x, "x"] = 0.10
    pst_mask = trafos["phase_shift"].abs() > 0.01
    if pst_mask.any():
        for idx in trafos[pst_mask].index:
            ps = trafos.loc[idx, "phase_shift"]
            if abs(ps) > 90:
                trafos.loc[idx, "phase_shift"] = -25.0 if ps < 0 else 25.0
            elif abs(ps) > 45:
                trafos.loc[idx, "phase_shift"] = 20.0 if ps > 0 else -20.0
    n.madd("Transformer", trafos["trafo_id"].astype(str),
           bus0=trafos["bus0"].astype(str).values,
           bus1=trafos["bus1"].astype(str).values,
           x=trafos["x"].astype(float).values,
           r=trafos["r"].astype(float).values,
           s_nom=trafos["s_nom"].values,
           tap_ratio=trafos["tap_ratio"].fillna(1.0).values,
           phase_shift=trafos["phase_shift"].fillna(0.0).values,
           s_max_pu=1e6)

    if len(links) > 0:
        n.madd("Link", links["link_id"].astype(str),
               bus0=links["bus0"].astype(str).values,
               bus1=links["bus1"].astype(str).values,
               p_nom=links["p_nom"].astype(float).values,
               p_min_pu=links["p_min_pu"].fillna(-1).values,
               efficiency=links["efficiency"].fillna(1.0).values,
               carrier=links["carrier"].fillna("DC").values)

    if aggregate:
        progress_cb(30, f"Aggregating {len(gens)} generators by (bus, carrier)")
        agg_gens, agg_ts = _aggregate_generators(gens, gen_ts, len(snapshots))
        progress_cb(35, f"  → {len(agg_gens)} aggregated generator groups")
        n.madd("Generator", agg_gens["generator_id"].astype(str),
               bus=agg_gens["bus"].astype(str).values,
               carrier=agg_gens["carrier"].fillna("none").values,
               p_nom=agg_gens["p_nom"].astype(float).values,
               marginal_cost=agg_gens["marginal_cost"].astype(float).values,
               efficiency=agg_gens["efficiency"].astype(float).values,
               p_min_pu=agg_gens["p_min_pu_static"].astype(float).values,
               p_max_pu=agg_gens["p_max_pu_static"].astype(float).values)
        # Replace the per-row gen_ts with the aggregated frame (uses string IDs)
        gen_ts = agg_ts.rename(columns={"generator_id_str": "generator_id"})
        gen_ts = gen_ts.assign(generator_id=gen_ts["generator_id"])
    else:
        n.madd("Generator", gens["generator_id"].astype(str),
               bus=gens["bus"].astype(str).values,
               carrier=gens["carrier"].values,
               p_nom=gens["p_nom"].values,
               marginal_cost=gens["marginal_cost"].fillna(0).values,
               efficiency=gens["efficiency"].fillna(1.0).values,
               p_min_pu=gens["p_min_pu"].fillna(0.0).values,
               p_max_pu=gens["p_max_pu"].fillna(1.0).values)

    n.madd("Load", loads_df["load_id"].astype(str),
           bus=loads_df["bus"].astype(str).values,
           carrier=loads_df["carrier"].fillna("AC").values,
           p_set=loads_df["p_set"].abs().values)

    progress_cb(40, "Applying timeseries")

    # Load p_set timeseries
    if len(load_ts) > 0:
        load_dict = {}
        for _, row in load_ts.iterrows():
            lid = str(int(row["load_id"]))
            if lid not in n.loads.index:
                continue
            sl = row["slice"]
            if sl is None or len(sl) == 0:
                continue
            arr = np.array(sl, dtype=float)
            if len(arr) < len(snapshots):
                arr = np.concatenate([arr, np.zeros(len(snapshots) - len(arr))])
            load_dict[lid] = np.abs(arr[:len(snapshots)])
        if load_dict:
            ldf = pd.DataFrame(load_dict, index=snapshots)
            # Backfill loads not in TS with their static p_set
            for lid in n.loads.index:
                if lid not in ldf.columns:
                    ldf[lid] = n.loads.loc[lid, "p_set"]
            n.loads_t.p_set = ldf

    # Generator p_max_pu / p_min_pu
    if len(gen_ts) > 0:
        import_gids = set(n.generators[n.generators.carrier.str.startswith("import_")].index)
        pmax_dict, pmin_dict = {}, {}
        for _, row in gen_ts.iterrows():
            gid = str(row["generator_id"]) if isinstance(row["generator_id"], str) else str(int(row["generator_id"]))
            if gid not in n.generators.index or gid in import_gids:
                continue
            for src_key, dst_dict in (("pmax_slice", pmax_dict), ("pmin_slice", pmin_dict)):
                sl = row[src_key]
                if sl is None or len(sl) == 0:
                    continue
                arr = np.array(sl, dtype=float)
                if len(arr) < len(snapshots):
                    arr = np.concatenate([arr, np.full(len(snapshots) - len(arr), arr[-1])])
                dst_dict[gid] = arr[:len(snapshots)]
        if pmax_dict:
            n.generators_t.p_max_pu = pd.DataFrame(pmax_dict, index=snapshots)
        if pmin_dict:
            n.generators_t.p_min_pu = pd.DataFrame(pmin_dict, index=snapshots)

    # Drop isolated buses
    connected: set[str] = set()
    for col in (n.lines.bus0, n.lines.bus1, n.transformers.bus0, n.transformers.bus1,
                n.links.bus0, n.links.bus1, n.generators.bus, n.loads.bus):
        for b in col:
            connected.add(b)
    isolated = n.buses.index.difference(pd.Index(list(connected)))
    if len(isolated) > 0:
        n.mremove("Bus", isolated)

    return n


def _aggregate_results(n: pypsa.Network, snapshots: pd.DatetimeIndex,
                       mode: str) -> dict:
    """Build a JSON-serialisable summary of the PF result."""
    gen_p = n.generators_t.p
    carriers = n.generators.carrier
    all_carriers = sorted(carriers.unique())

    timeseries = []
    for snap in snapshots:
        h = {"t": snap.isoformat()}
        h["load_mw"] = round(float(n.loads_t.p_set.loc[snap].sum()), 1)
        for c in all_carriers:
            gids = carriers[carriers == c].index
            if len(gids) > 0:
                h[c] = round(float(gen_p.loc[snap, gids].sum()), 1)
        timeseries.append(h)

    # Carrier totals over the period
    totals = {}
    for c in all_carriers:
        gids = carriers[carriers == c].index
        if len(gids) > 0:
            totals[c] = round(float(gen_p[gids].sum().sum()), 1)
    total_load_mwh = round(float(n.loads_t.p_set.sum().sum()), 1)
    total_gen_mwh = sum(totals.values())

    # Line loading per snapshot (max% across lines)
    line_loadings = {}
    if len(n.lines_t.p0) > 0:
        loading_pct = n.lines_t.p0.abs() / n.lines.s_nom.replace(0, np.nan) * 100
        for snap in snapshots:
            row = loading_pct.loc[snap].dropna()
            line_loadings[snap.isoformat()] = {
                "max_pct": round(float(row.max()), 1) if len(row) else 0,
                "mean_pct": round(float(row.mean()), 1) if len(row) else 0,
                "n_overloaded": int((row > 100).sum()),
            }

    # Top-N congested lines (max loading across the period)
    top_lines = []
    if len(n.lines_t.p0) > 0:
        max_load = (n.lines_t.p0.abs().max() / n.lines.s_nom.replace(0, np.nan) * 100).dropna()
        top = max_load.sort_values(ascending=False).head(20)
        for line_id, pct in top.items():
            line = n.lines.loc[line_id]
            top_lines.append({
                "line_id": line_id,
                "v_nom": float(line["v_nom"]) if "v_nom" in line else None,
                "s_nom": float(line["s_nom"]),
                "bus0": line["bus0"],
                "bus1": line["bus1"],
                "max_loading_pct": round(float(pct), 1),
            })

    # Per-line max-loading map (frontend overlay)
    line_max_loading = {}
    if len(n.lines_t.p0) > 0:
        max_load = (n.lines_t.p0.abs().max() / n.lines.s_nom.replace(0, np.nan) * 100).dropna()
        for lid, pct in max_load.items():
            line_max_loading[lid] = round(float(pct), 1)

    objective = float(n.objective) if hasattr(n, "objective") and n.objective is not None else None

    return {
        "mode": mode,
        "n_snapshots": len(snapshots),
        "start": snapshots[0].isoformat(),
        "end": snapshots[-1].isoformat(),
        "carriers": all_carriers,
        "totals_mwh": totals,
        "total_load_mwh": total_load_mwh,
        "total_gen_mwh": total_gen_mwh,
        "objective": objective,
        "timeseries": timeseries,
        "line_loadings": line_loadings,
        "top_lines": top_lines,
        "line_max_loading": line_max_loading,
    }


# ── Job thread body ─────────────────────────────────────────────────────────


def _run_job(job_id: str, snapshots: pd.DatetimeIndex, mode: str, aggregate: bool = True):
    try:
        with _run_lock:
            _set_state(job_id, state="running", progress=2, message="Acquired runner")

            hour_indices = [_hour_of_year(s) for s in snapshots]

            def cb(p, msg):
                _set_state(job_id, progress=p, message=msg)

            n = _build_network(snapshots, hour_indices, cb, aggregate=aggregate)

            if mode == "lopf":
                cb(60, f"Running LOPF on {len(snapshots)} snapshots (CBC)")
                status, condition = n.lopf(pyomo=False, solver_name="cbc")
                if status != "ok":
                    cb(60, f"LOPF infeasible ({condition}); retrying with relaxed p_min_pu")
                    n.generators.p_min_pu = 0.0
                    if len(n.generators_t.p_min_pu) > 0:
                        n.generators_t.p_min_pu = pd.DataFrame(index=n.snapshots)
                    status, condition = n.lopf(pyomo=False, solver_name="cbc")
                if status != "ok":
                    raise RuntimeError(f"LOPF failed: {status}/{condition}")
            else:
                cb(60, f"Running linear PF on {len(snapshots)} snapshots")
                # Need a dispatch first — set generators to follow p_max_pu (no opt)
                # For a true PF, we should use a precomputed dispatch. For now,
                # warn and run lopf as a fallback.
                raise NotImplementedError(
                    "Pure PF mode requires a precomputed dispatch; use mode='lopf' for now."
                )

            cb(85, "Aggregating results")
            res = _aggregate_results(n, snapshots, mode)

            out_file = JOB_DIR / f"{job_id}.json"
            out_file.write_text(json.dumps(res))

            _set_state(job_id, state="done", progress=100,
                       message=f"Complete ({len(snapshots)} snapshots)",
                       result_file=str(out_file))
    except Exception as e:
        log.exception(f"PF job {job_id} failed")
        _set_state(job_id, state="failed", progress=0,
                   message=f"Error: {e}",
                   traceback=traceback.format_exc())
