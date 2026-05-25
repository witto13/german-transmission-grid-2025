"""_gen_voltage_fix.py — Move every generator / storage unit larger than a threshold
from 110 kV onto the nearest 220 / 380 kV bus.

Rationale: real German plants of ≥150 MW connect at 220 kV or 380 kV. The eGon / MaStR
aggregation in build_grid_alpha sometimes lands them on 110 kV nodes whose evacuation
network can't carry the dispatched power (see bus 25378 = 1538 MW pumped-hydro phantom).

Used at runtime in the DC PF / redispatch via env var `APPLY_GEN_VOLTAGE_FIX=1`. Does
NOT touch the database — purely an in-memory reassignment of the `bus` column for the
affected generators / storage units.
"""
import logging
import os

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# Threshold can be overridden at runtime via env var GEN_VOLTAGE_THRESHOLD_MW.
DEFAULT_THRESHOLD_MW = 150.0

# Carriers that legitimately connect at 110 kV (DSO/TSO boundary) even at high p_nom and
# whose 110 kV placement DRIVES real curtailment we want the model to capture. These get
# the *aggregated* placement (multiple real farms collapsed onto one bus) but they are NOT
# relocated to EHV. Override with env var GEN_VOLTAGE_SKIP_CARRIERS=comma,sep,list.
DEFAULT_SKIP_CARRIERS = {"onwind", "offwind", "solar"}


def _haversine_km(lat1, lon1, lat2_arr, lon2_arr):
    return np.hypot((lat1 - lat2_arr) * 111.0, (lon1 - lon2_arr) * 71.0)


def _nearest_ehv(bus_lat, bus_lon, ehv_buses, prefer_380_within_km=5.0):
    """Return (bus_id, kV, distance_km) of nearest 220/380 kV bus.

    Preference: if a 380 kV bus is within (nearest 220 + prefer_380_within_km),
    pick the 380 kV bus. Otherwise pick the nearest of either voltage.
    """
    d = _haversine_km(bus_lat, bus_lon, ehv_buses["lat"].values, ehv_buses["lon"].values)
    k380 = np.where(ehv_buses["v_nom"].values == 380, d, np.inf)
    k220 = np.where(ehv_buses["v_nom"].values == 220, d, np.inf)
    i380 = int(np.argmin(k380)) if np.isfinite(k380.min()) else None
    i220 = int(np.argmin(k220)) if np.isfinite(k220.min()) else None
    if i220 is None and i380 is None:
        return None, None, np.inf
    if i220 is None:
        return ehv_buses.index[i380], 380, float(d[i380])
    if i380 is None:
        return ehv_buses.index[i220], 220, float(d[i220])
    if d[i380] <= d[i220] + prefer_380_within_km:
        return ehv_buses.index[i380], 380, float(d[i380])
    return ehv_buses.index[i220], 220, float(d[i220])


def apply_gen_voltage_rule(n, threshold_mw: float = None, verbose: bool = True,
                            skip_carriers=None):
    """Reassign generators + storage units with p_nom > threshold from 110 kV onto
    the nearest 220 / 380 kV bus. Carriers in `skip_carriers` (default: wind+solar)
    are NOT relocated — they legitimately connect at 110 kV and their placement
    DRIVES the curtailment that the model needs to reproduce. Returns a report
    DataFrame.
    """
    if threshold_mw is None:
        threshold_mw = float(os.environ.get("GEN_VOLTAGE_THRESHOLD_MW", DEFAULT_THRESHOLD_MW))
    if skip_carriers is None:
        env_skip = os.environ.get("GEN_VOLTAGE_SKIP_CARRIERS")
        skip_carriers = (set(c.strip() for c in env_skip.split(",") if c.strip())
                         if env_skip is not None else DEFAULT_SKIP_CARRIERS)
    skip_carriers = set(skip_carriers)

    buses = n.buses[["x", "y", "v_nom"]].copy()
    buses.rename(columns={"x": "lon", "y": "lat"}, inplace=True)
    # EHV buses with valid coords
    ehv = buses[(buses["v_nom"].isin([220.0, 380.0])) &
                (np.isfinite(buses["lat"])) & (np.isfinite(buses["lon"])) &
                (buses["lon"] != 0) & (buses["lat"] != 0)].copy()

    reports = []
    move_count = {"generator": 0, "storage": 0}
    move_mw = {"generator": 0.0, "storage": 0.0}

    # ---- Generators ----
    g = n.generators
    g_buses = g["bus"].map(n.buses["v_nom"])
    elig = g[(g["p_nom"] > threshold_mw) & (g_buses == 110.0)
             & (~g["carrier"].isin(skip_carriers))].copy()
    for gid, row in elig.iterrows():
        old_bus = row["bus"]
        if old_bus not in n.buses.index:
            continue
        bx = float(n.buses.at[old_bus, "x"]); by = float(n.buses.at[old_bus, "y"])
        if not (np.isfinite(bx) and np.isfinite(by) and bx != 0):
            continue
        new_bus, new_kv, dist = _nearest_ehv(by, bx, ehv)
        if new_bus is None or new_bus == old_bus:
            continue
        n.generators.at[gid, "bus"] = new_bus
        move_count["generator"] += 1
        move_mw["generator"] += float(row["p_nom"])
        reports.append({
            "type": "generator", "id": gid, "carrier": row["carrier"],
            "p_nom_MW": round(float(row["p_nom"]), 1),
            "from_bus": old_bus, "to_bus": new_bus,
            "from_kV": 110, "to_kV": int(new_kv),
            "distance_km": round(dist, 2),
        })

    # ---- Storage units ----
    s = n.storage_units
    s_buses = s["bus"].map(n.buses["v_nom"])
    elig_s = s[(s["p_nom"] > threshold_mw) & (s_buses == 110.0)].copy()
    for sid, row in elig_s.iterrows():
        old_bus = row["bus"]
        if old_bus not in n.buses.index:
            continue
        bx = float(n.buses.at[old_bus, "x"]); by = float(n.buses.at[old_bus, "y"])
        if not (np.isfinite(bx) and np.isfinite(by) and bx != 0):
            continue
        new_bus, new_kv, dist = _nearest_ehv(by, bx, ehv)
        if new_bus is None or new_bus == old_bus:
            continue
        n.storage_units.at[sid, "bus"] = new_bus
        move_count["storage"] += 1
        move_mw["storage"] += float(row["p_nom"])
        reports.append({
            "type": "storage", "id": sid, "carrier": row["carrier"],
            "p_nom_MW": round(float(row["p_nom"]), 1),
            "from_bus": old_bus, "to_bus": new_bus,
            "from_kV": 110, "to_kV": int(new_kv),
            "distance_km": round(dist, 2),
        })

    if verbose:
        log.info(f"  Gen-voltage rule (>{threshold_mw:.0f} MW): moved "
                 f"{move_count['generator']} generators ({move_mw['generator']:.0f} MW) "
                 f"+ {move_count['storage']} storage units ({move_mw['storage']:.0f} MW) "
                 f"from 110 kV → nearest EHV bus. "
                 f"Skipped carriers (kept at 110 kV): {sorted(skip_carriers)}")

    return pd.DataFrame(reports)
