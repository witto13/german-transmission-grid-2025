#!/usr/bin/env python3
"""scan_topology_defects.py — Enumerate model-topology defects in grid_beta.

Classes scanned (using the corrected per-unit PTDF + observed DA flows):

  A) 110 kV pockets carrying EHV-level transit through under-rated lines that lack
     any EHV step-up transformer (the "line 25296 / 2 GW through 260 MW" class).
  B) Radial 110 kV lines (max|PTDF|=1) whose peak DA flow exceeds s_nom.
  C) Dummy/non-physical short connectors (length<0.5 km AND x<0.05 ohm).
  D) Isolated buses (no line and no transformer) carrying gen or load.
  E) Very-large gen aggregation on a single 110 kV bus (>500 MW p_nom).
  F) Duplicate-coordinate bus clusters at the same voltage (sub-100 m).

Output: results/topology_defects.csv (sorted by impact = peak transit MW).
"""
import logging
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

from _redispatch_core import build_precomp

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

RES = Path("/root/egon_2025_project/results")
PF_NC = RES / "dispatch_8760h_pf.nc"
OUT = RES / "topology_defects.csv"


def main():
    log.info(f"Loading {PF_NC}")
    n = pypsa.Network(str(PF_NC))
    n.lines["x"] = n.lines["x"].clip(lower=0.05)
    n.calculate_dependent_values()
    pre = build_precomp(n)
    sub_line_ids = pre.sub_line_ids
    ptdf = pre.ptdf
    L = ptdf.shape[0]
    log.info(f"  {len(n.buses)} buses, {len(n.lines)} lines, {len(n.transformers)} trafos")

    # ---- line max DA flow (from DC PF stored in n.lines_t.p0) ----
    p0 = n.lines_t.p0
    max_flow = p0.abs().max(axis=0)                              # MW
    max_loading = (max_flow / n.lines["s_nom"].replace(0, np.inf)).fillna(0)

    # ---- buses with transformers ----
    trafo_buses = set(n.transformers["bus0"]).union(set(n.transformers["bus1"]))

    # ---- precompute line endpoints per bus (degree) ----
    lines_by_bus = defaultdict(list)
    for lid, b0, b1 in zip(n.lines.index, n.lines["bus0"], n.lines["bus1"]):
        lines_by_bus[b0].append(lid)
        lines_by_bus[b1].append(lid)

    bus_vnom = n.buses["v_nom"].to_dict()
    bus_xy = n.buses[["x", "y"]].to_dict("index")

    # Generator + load p_nom per bus
    gen_by_bus = n.generators.groupby("bus")["p_nom"].sum().to_dict()
    if hasattr(n, "loads_t") and n.loads_t.p_set is not None and not n.loads_t.p_set.empty:
        peak_load_by_bus = (n.loads_t.p_set.groupby(n.loads["bus"], axis=1).sum().max(axis=0)
                             .to_dict())
    else:
        peak_load_by_bus = {}

    defects = []

    # =============================================================== #
    # (C) Dummy connector lines (length<0.5km AND x<0.05 — small even after the floor)
    # =============================================================== #
    li = n.lines
    mask_dummy = (li["length"] < 0.5) & (li["x"] <= 0.051)
    for lid in li.index[mask_dummy]:
        defects.append({
            "class": "C_dummy_connector",
            "line_id": lid, "bus0": li.at[lid, "bus0"], "bus1": li.at[lid, "bus1"],
            "v_nom": bus_vnom[li.at[lid, "bus0"]],
            "s_nom": float(li.at[lid, "s_nom"]),
            "length_km": float(li.at[lid, "length"]),
            "x_ohm": float(li.at[lid, "x"]),
            "peak_flow_MW": float(max_flow.get(lid, 0)),
            "peak_loading": float(max_loading.get(lid, 0)),
            "suggested_fix": "merge bus0+bus1 (same substation) or raise x to physical ≥0.05",
        })

    # =============================================================== #
    # (D) Isolated buses carrying gen or load (no line, no trafo)
    # =============================================================== #
    for bid in n.buses.index:
        if bid in trafo_buses or len(lines_by_bus.get(bid, [])):
            continue
        g = gen_by_bus.get(bid, 0); pl = peak_load_by_bus.get(bid, 0)
        if g > 0 or pl > 0:
            defects.append({
                "class": "D_isolated_gen_load",
                "line_id": "", "bus0": bid, "bus1": "",
                "v_nom": bus_vnom.get(bid, 0),
                "s_nom": 0, "length_km": 0, "x_ohm": 0,
                "peak_flow_MW": 0, "peak_loading": 0,
                "gen_MW": float(g), "peak_load_MW": float(pl),
                "suggested_fix": "connect to nearest same-voltage bus via short tie-line, "
                                  "OR move gen/load to the nearest connected bus",
            })

    # =============================================================== #
    # (E) Over-aggregated generation on a single 110 kV bus
    # =============================================================== #
    for bus, g_mw in gen_by_bus.items():
        if bus_vnom.get(bus, 0) == 110 and g_mw > 500:
            # is it adequately evacuated? count outgoing line capacity + trafo capacity
            line_cap = sum(n.lines.at[lid, "s_nom"] for lid in lines_by_bus.get(bus, []))
            trafo_cap = (n.transformers[(n.transformers.bus0 == bus) | (n.transformers.bus1 == bus)]
                          ["s_nom"].sum())
            evac = float(line_cap + trafo_cap)
            if evac < g_mw * 1.2:
                defects.append({
                    "class": "E_overconcentrated_gen_110kv",
                    "line_id": "", "bus0": bus, "bus1": "",
                    "v_nom": 110, "s_nom": 0,
                    "length_km": 0, "x_ohm": 0,
                    "peak_flow_MW": 0, "peak_loading": 0,
                    "gen_MW": float(g_mw),
                    "evacuation_MW": evac,
                    "suggested_fix": "split aggregated gens across neighbour buses, "
                                      "OR add 380/110 transformer (≥500 MVA)",
                })

    # =============================================================== #
    # (F) Duplicate-coordinate bus clusters (same v_nom, <100 m apart)
    # =============================================================== #
    coords = n.buses[["x", "y", "v_nom"]].copy()
    # Round to ~100 m grid (≈0.001° lat at DE latitude). Group ties.
    coords["xr"] = (coords["x"] * 1000).round().astype(int)
    coords["yr"] = (coords["y"] * 1000).round().astype(int)
    groups = coords.groupby(["v_nom", "xr", "yr"]).size()
    dup_keys = groups[groups > 1].index
    for v, xr, yr in dup_keys:
        bset = coords[(coords["v_nom"] == v) & (coords["xr"] == xr) & (coords["yr"] == yr)].index.tolist()
        defects.append({
            "class": "F_duplicate_buses",
            "line_id": "", "bus0": bset[0], "bus1": ";".join(bset[1:]),
            "v_nom": v, "s_nom": 0, "length_km": 0, "x_ohm": 0,
            "peak_flow_MW": 0, "peak_loading": 0,
            "n_duplicates": len(bset),
            "suggested_fix": "merge cluster into single bus (same substation)",
        })

    # =============================================================== #
    # (B) Radial 110 kV lines with peak DA flow > s_nom
    #     A line is radial (in the meshed graph) if max|PTDF[l,:]| ≈ 1 and the
    #     |PTDF| > 0.5 set is small (single side of the cut).
    # =============================================================== #
    L_sub = ptdf.shape[0]
    sub_line_pos = {lid: i for i, lid in enumerate(sub_line_ids)}
    sub_to_ml = n.lines.loc[sub_line_ids]
    # vectorised: per row, max|PTDF| and count of |PTDF|>0.5
    abs_ptdf = np.abs(ptdf)
    max_ptdf = abs_ptdf.max(axis=1)
    n_strong = (abs_ptdf > 0.5).sum(axis=1)
    radial_mask = (max_ptdf > 0.97) & (n_strong < 50)            # tight: leaf-cut

    for i in np.where(radial_mask)[0]:
        lid = sub_line_ids[i]
        snom = float(sub_to_ml.at[lid, "s_nom"])
        flow = float(max_flow.get(lid, 0))
        load = flow / max(snom, 1e-6)
        if load < 1.1:
            continue
        b0, b1 = sub_to_ml.at[lid, "bus0"], sub_to_ml.at[lid, "bus1"]
        v = bus_vnom[b0]
        has_trafo = (b0 in trafo_buses) or (b1 in trafo_buses)
        defects.append({
            "class": "B_radial_overloaded",
            "line_id": lid, "bus0": b0, "bus1": b1, "v_nom": v,
            "s_nom": snom, "length_km": float(sub_to_ml.at[lid, "length"]),
            "x_ohm": float(sub_to_ml.at[lid, "x"]),
            "peak_flow_MW": flow, "peak_loading": load,
            "endpoint_has_trafo": bool(has_trafo),
            "suggested_fix": ("uplift s_nom (add parallel circuit) by "
                               + f"{int(np.ceil(flow / snom))}x"
                               + ("" if has_trafo else "; add EHV transformer at this pocket")),
        })

    # =============================================================== #
    # (A) "Pockets" — connected 110 kV components containing a residual-overloaded
    #     line with NO transformer endpoint anywhere in the pocket.
    #     Pocket = 110 kV-only connected component bounded by EHV transformer nodes.
    # =============================================================== #
    # BFS on 110 kV subgraph; pocket boundaries are buses that DO have a transformer.
    pocket_id = {}
    pid = 0
    visited_110 = set()
    adj_110 = defaultdict(set)
    for lid, b0, b1 in zip(n.lines.index, n.lines["bus0"], n.lines["bus1"]):
        if bus_vnom.get(b0) == 110 and bus_vnom.get(b1) == 110:
            adj_110[b0].add(b1)
            adj_110[b1].add(b0)
    for seed in [b for b, v in bus_vnom.items() if v == 110]:
        if seed in visited_110 or seed in trafo_buses:
            continue
        # BFS
        comp = []
        stack = [seed]
        visited_110.add(seed)
        while stack:
            b = stack.pop()
            comp.append(b)
            for nb in adj_110[b]:
                if nb in visited_110:
                    continue
                visited_110.add(nb)
                # don't expand past transformer-equipped buses (boundary)
                if nb in trafo_buses:
                    comp.append(nb)
                else:
                    stack.append(nb)
        for b in comp:
            pocket_id[b] = pid
        pid += 1
    # Tag each line by its pocket (only lines fully inside a no-trafo pocket)
    pocket_stats = defaultdict(lambda: dict(
        buses=set(), lines=set(), has_trafo=False,
        peak_in_flow=0.0, peak_in_line=None,
        gen=0.0, peak_load=0.0,
    ))
    for lid, b0, b1 in zip(n.lines.index, n.lines["bus0"], n.lines["bus1"]):
        if bus_vnom.get(b0) != 110 or bus_vnom.get(b1) != 110:
            continue
        p0 = pocket_id.get(b0); p1 = pocket_id.get(b1)
        if p0 is None or p1 is None or p0 != p1:
            continue
        st = pocket_stats[p0]
        st["buses"].update([b0, b1])
        st["lines"].add(lid)
        if (b0 in trafo_buses) or (b1 in trafo_buses):
            st["has_trafo"] = True
        f = float(max_flow.get(lid, 0))
        if f > st["peak_in_flow"]:
            st["peak_in_flow"] = f; st["peak_in_line"] = lid

    # Worst pockets: those without a transformer-equipped boundary that carry high flow
    pocket_rows = []
    for p, st in pocket_stats.items():
        if st["has_trafo"]:
            continue
        if len(st["buses"]) < 2:
            continue
        if st["peak_in_flow"] < 200:    # filter trivial
            continue
        # local gen + load
        gen_mw = sum(gen_by_bus.get(b, 0) for b in st["buses"])
        load_mw = sum(peak_load_by_bus.get(b, 0) for b in st["buses"])
        pocket_rows.append((p, st, gen_mw, load_mw))
    pocket_rows.sort(key=lambda r: -r[1]["peak_in_flow"])

    # Distance helper
    def km_between(b0, b1):
        x0, y0 = bus_xy[b0]["x"], bus_xy[b0]["y"]
        x1, y1 = bus_xy[b1]["x"], bus_xy[b1]["y"]
        return float(np.hypot((x0 - x1) * 71, (y0 - y1) * 111))  # rough km at DE latitude

    # nearest EHV (220/380) bus suggestion for the heaviest pocket bus
    ehv_buses = [b for b, v in bus_vnom.items() if v in (220, 380)]
    ehv_coords = np.array([[bus_xy[b]["x"], bus_xy[b]["y"]] for b in ehv_buses])

    for p, st, gen_mw, load_mw in pocket_rows[:200]:
        worst_line = st["peak_in_line"]
        # pick centroid bus = bus0 of worst line as anchor
        anchor = n.lines.at[worst_line, "bus0"]
        ax, ay = bus_xy[anchor]["x"], bus_xy[anchor]["y"]
        d = np.hypot((ehv_coords[:, 0] - ax) * 71, (ehv_coords[:, 1] - ay) * 111)
        k = int(np.argmin(d))
        nearest_ehv = ehv_buses[k]
        defects.append({
            "class": "A_no_transformer_pocket",
            "line_id": worst_line,
            "bus0": anchor, "bus1": nearest_ehv,
            "v_nom": 110, "s_nom": float(n.lines.at[worst_line, "s_nom"]),
            "length_km": float(n.lines.at[worst_line, "length"]),
            "x_ohm": float(n.lines.at[worst_line, "x"]),
            "peak_flow_MW": float(st["peak_in_flow"]),
            "peak_loading": float(st["peak_in_flow"] / max(n.lines.at[worst_line, "s_nom"], 1e-6)),
            "pocket_n_buses": len(st["buses"]),
            "pocket_gen_MW": float(gen_mw), "pocket_peak_load_MW": float(load_mw),
            "nearest_ehv_bus": nearest_ehv, "nearest_ehv_kv": int(bus_vnom[nearest_ehv]),
            "nearest_ehv_km": float(d[k]),
            "suggested_fix": (f"add {int(bus_vnom[nearest_ehv])}/110 kV transformer "
                              f"(≥{int(np.ceil(st['peak_in_flow']/300)*300)} MVA) "
                              f"between bus {anchor} and EHV bus {nearest_ehv} "
                              f"({d[k]:.1f} km away)"),
        })

    df = pd.DataFrame(defects)
    df = df.sort_values(by=["class", "peak_flow_MW"], ascending=[True, False])
    df.to_csv(OUT, index=False)
    log.info(f"Wrote {len(df)} defects → {OUT}")
    log.info("\nDefect class summary:")
    log.info(df.groupby("class").size().to_string())
    log.info("\nTop 10 by peak flow:")
    log.info(df.nlargest(10, "peak_flow_MW")[["class","line_id","bus0","bus1","v_nom","s_nom","peak_flow_MW","peak_loading","suggested_fix"]].to_string(max_colwidth=80))


if __name__ == "__main__":
    main()
