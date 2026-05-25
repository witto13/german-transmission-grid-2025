#!/usr/bin/env python3
"""derive_topology_fixes.py — Consolidate scanner defects into a concrete fix list.

Reads results/topology_defects.csv and produces:
  data/topology_fixes_transformers.csv  — new EHV/110 transformer additions
  data/topology_fixes_line_uplifts.csv  — line s_nom uplifts (parallel circuits)

Fix derivation rules:
  • For each Class B (radial overload >=1.5x) or Class A (no-trafo pocket) defect, find the
    110 kV "pocket" it belongs to (BFS on 110 kV-only connected component bounded by
    transformer-equipped buses).  One transformer per distinct pocket, sized to the pocket's
    peak transit flow rounded up to {300, 600, 900, 1200} MVA.
  • Skip Class D foreign buses (200xxx) — those connect via HVDC Links, not lines.
"""
import logging
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

NC = Path("/root/egon_2025_project/results/dispatch_8760h_pf.nc")
DEFECTS = Path("/root/egon_2025_project/results/topology_defects.csv")
OUT_DIR = Path("/root/egon_2025_project/data")
OUT_DIR.mkdir(exist_ok=True)

# Standard MVA sizes for new transformers (snap upward)
SNOM_LADDER = [300, 600, 900, 1200, 1500, 2000]


def round_to_ladder(mw, factor=1.3):
    target = mw * factor   # 30% headroom over peak
    for s in SNOM_LADDER:
        if s >= target:
            return s
    return SNOM_LADDER[-1]


def main():
    log.info(f"Loading {NC}")
    n = pypsa.Network(str(NC))
    n.lines["x"] = n.lines["x"].clip(lower=0.05)
    n.calculate_dependent_values()

    defects = pd.read_csv(DEFECTS)
    bus_vnom = n.buses["v_nom"].to_dict()
    bus_xy = n.buses[["x", "y"]].to_dict("index")
    trafo_buses = set(n.transformers["bus0"]).union(set(n.transformers["bus1"]))

    # 110 kV-only adjacency (boundary = trafo-equipped buses)
    adj_110 = defaultdict(set)
    for b0, b1 in zip(n.lines["bus0"], n.lines["bus1"]):
        if bus_vnom.get(b0) == 110 and bus_vnom.get(b1) == 110:
            adj_110[b0].add(b1); adj_110[b1].add(b0)

    def pocket_of(seed):
        """BFS 110 kV pocket starting at `seed`, stopping at trafo-equipped buses."""
        if seed in trafo_buses:
            return {seed}
        comp = set([seed])
        stack = [seed]
        while stack:
            b = stack.pop()
            for nb in adj_110[b]:
                if nb in comp:
                    continue
                comp.add(nb)
                if nb not in trafo_buses:
                    stack.append(nb)
        return comp

    # Peak per-line flow MW (for sizing)
    max_flow = n.lines_t.p0.abs().max(axis=0)

    # Per-bus aggregate gen + peak load
    gen_by_bus = n.generators.groupby("bus")["p_nom"].sum().to_dict()
    if hasattr(n, "loads_t") and n.loads_t.p_set is not None and not n.loads_t.p_set.empty:
        peak_load_by_bus = (n.loads_t.p_set.groupby(n.loads["bus"], axis=1)
                              .sum().max(axis=0).to_dict())
    else:
        peak_load_by_bus = {}

    # Candidate buses to "anchor" a transformer fix: from Class A + Class B defects.
    # Bus IDs in PyPSA are strings (e.g. "30024"); the CSV reads as int — normalize.
    def to_str(b):
        try:
            return str(int(float(b)))
        except Exception:
            return str(b)
    candidates = []
    for _, r in defects[defects["class"] == "A_no_transformer_pocket"].iterrows():
        candidates.append((to_str(r["bus0"]), float(r["peak_flow_MW"]), "A"))
    for _, r in defects[defects["class"] == "B_radial_overloaded"].iterrows():
        # only meaningful where peak_loading > 1.1
        if float(r["peak_loading"]) > 1.1:
            candidates.append((to_str(r["bus0"]), float(r["peak_flow_MW"]), "B"))
            candidates.append((to_str(r["bus1"]), float(r["peak_flow_MW"]), "B"))

    # Group candidates by pocket (deduplicate via canonical pocket key)
    seen_pockets = {}            # frozenset of 110kV bus ids -> anchor info
    n_skip_vnom = n_skip_alreadyplaced = 0
    for anchor, flow_mw, cls in candidates:
        if bus_vnom.get(anchor) != 110:
            n_skip_vnom += 1; continue
        if anchor in trafo_buses:
            # bus already has a transformer — skip
            n_skip_alreadyplaced += 1; continue
        pocket = pocket_of(anchor)
        # pocket "interior" = non-trafo nodes (drop boundary trafo nodes)
        interior = {b for b in pocket if b not in trafo_buses}
        if not interior:
            continue
        key = frozenset(interior)
        rec = seen_pockets.get(key, {
            "buses": list(interior), "peak_flow_MW": 0.0, "from_classes": set(),
            "anchor_bus": anchor,
        })
        rec["peak_flow_MW"] = max(rec["peak_flow_MW"], flow_mw)
        rec["from_classes"].add(cls)
        # anchor = bus in the interior with the highest local injection (gen+peak_load)
        rec["anchor_bus"] = max(interior, key=lambda b: gen_by_bus.get(b, 0) + peak_load_by_bus.get(b, 0))
        seen_pockets[key] = rec

    # Find nearest EHV (220 or 380) bus for each anchor — prefer 380 if within +5 km of 220
    ehv = [(b, bus_vnom[b]) for b, v in bus_vnom.items() if v in (220, 380)]
    ehv_arr = np.array([[bus_xy[b]["x"], bus_xy[b]["y"]] for b, _ in ehv])
    ehv_v = np.array([v for _, v in ehv])
    ehv_ids = [b for b, _ in ehv]

    def nearest_ehv(bid, prefer_380=True):
        ax, ay = bus_xy[bid]["x"], bus_xy[bid]["y"]
        d = np.hypot((ehv_arr[:, 0] - ax) * 71, (ehv_arr[:, 1] - ay) * 111)
        # 380 candidate
        k380 = np.argmin(np.where(ehv_v == 380, d, np.inf))
        k220 = np.argmin(np.where(ehv_v == 220, d, np.inf))
        if prefer_380 and d[k380] <= d[k220] + 5:
            return ehv_ids[k380], 380, float(d[k380])
        return ehv_ids[k220], 220, float(d[k220])

    rows = []
    used_low = set()              # avoid placing two transformers on the same 110 kV bus
    used_pairs = set()
    for key, rec in sorted(seen_pockets.items(), key=lambda kv: -kv[1]["peak_flow_MW"]):
        anchor = rec["anchor_bus"]
        if anchor in used_low:
            continue
        ehv_id, ehv_kv, dist = nearest_ehv(anchor)
        if (anchor, ehv_id) in used_pairs:
            continue
        # Skip if EHV bus is itself far (>40 km) — likely no realistic transformer
        if dist > 40:
            continue
        # Aggregate pocket characteristics for the spec
        pocket_gen = sum(gen_by_bus.get(b, 0) for b in rec["buses"])
        pocket_peak_load = sum(peak_load_by_bus.get(b, 0) for b in rec["buses"])
        s_nom_mva = round_to_ladder(max(rec["peak_flow_MW"], pocket_gen * 0.3, pocket_peak_load))
        rows.append({
            "low_bus": anchor,
            "ehv_bus": ehv_id,
            "ehv_kv": int(ehv_kv),
            "s_nom_MVA": int(s_nom_mva),
            "x_pu": 0.12,
            "r_pu": 0.005,
            "distance_km": round(dist, 2),
            "pocket_n_buses": len(rec["buses"]),
            "pocket_peak_flow_MW": round(rec["peak_flow_MW"], 1),
            "pocket_gen_MW": round(pocket_gen, 1),
            "pocket_peak_load_MW": round(pocket_peak_load, 1),
            "from_classes": "+".join(sorted(rec["from_classes"])),
        })
        used_low.add(anchor)
        used_pairs.add((anchor, ehv_id))

    log.info(f"Candidates: {len(candidates)}; skipped by v_nom: {n_skip_vnom}; "
             f"skipped (already has trafo): {n_skip_alreadyplaced}; kept pockets: {len(seen_pockets)}")
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("pocket_peak_flow_MW", ascending=False).reset_index(drop=True)
    out = OUT_DIR / "topology_fixes_transformers.csv"
    df.to_csv(out, index=False)
    log.info(f"Wrote {len(df)} transformer additions → {out}")
    log.info("\n" + df.to_string(index=False, max_colwidth=80))


if __name__ == "__main__":
    main()
