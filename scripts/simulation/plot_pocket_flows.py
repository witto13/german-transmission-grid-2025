#!/usr/bin/env python3
"""plot_pocket_flows.py — Zoomed map of a specific 110 kV pocket showing the
worst-hour flow pattern: every bus's net injection (gen − load) at that hour,
every line's actual flow and loading, the existing transformer(s), and the
fix I proposed. Helps decide whether the fix is the right structural answer.

Usage:
    python plot_pocket_flows.py --anchor 25946          # default: pocket of 25946
"""
import argparse
import json
import logging
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

NC = "/root/egon_2025_project/results/dispatch_8760h_pf.nc"
FIX_CSV = "/root/egon_2025_project/data/topology_fixes_transformers.csv"
OUT_DIR = Path("/root/egon_2025_project/results")

V_COLOR = {110: "#9aa1a8", 220: "#e2891b", 380: "#cc2222"}


def pocket_bfs(adj, trafo_buses, seed, hops=3):
    """BFS pocket: include `seed`'s 110 kV connected component (boundary = trafo buses),
    plus up to `hops` extra layers beyond the boundary so we see context."""
    interior = set([seed]); stack = [seed]; depth = {seed: 0}
    while stack:
        b = stack.pop()
        for nb in adj[b]:
            if nb in interior: continue
            interior.add(nb); depth[nb] = depth[b] + 1
            if nb not in trafo_buses: stack.append(nb)
    # Add neighborhood layer beyond trafo boundary (purely for context)
    fringe = list(interior); seen = set(interior)
    for _ in range(hops):
        new_fringe = []
        for b in fringe:
            for nb in adj.get(b, []):
                if nb in seen: continue
                seen.add(nb); new_fringe.append(nb)
        fringe = new_fringe
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchor", default="25946")
    args = ap.parse_args()

    log.info(f"Loading {NC}")
    n = pypsa.Network(NC)
    n.lines["x"] = n.lines["x"].clip(lower=0.05)

    bx = n.buses["x"].astype(float).to_dict()
    by = n.buses["y"].astype(float).to_dict()
    bv = n.buses["v_nom"].astype(float).to_dict()

    # Adjacency over BOTH lines and transformers (so we walk through transformer-equipped buses)
    adj_all = defaultdict(set)
    for b0, b1 in zip(n.lines["bus0"], n.lines["bus1"]):
        adj_all[b0].add(b1); adj_all[b1].add(b0)
    for b0, b1 in zip(n.transformers["bus0"], n.transformers["bus1"]):
        adj_all[b0].add(b1); adj_all[b1].add(b0)
    trafo_buses = set(n.transformers["bus0"]).union(set(n.transformers["bus1"]))

    pocket = pocket_bfs(adj_all, trafo_buses, args.anchor, hops=2)
    log.info(f"Pocket around bus {args.anchor}: {len(pocket)} buses")

    # ---- Find the worst hour for any line in this pocket ----
    pocket_line_mask = (n.lines["bus0"].isin(pocket)) | (n.lines["bus1"].isin(pocket))
    pocket_line_ids = list(n.lines.index[pocket_line_mask])
    log.info(f"Lines touching pocket: {len(pocket_line_ids)}")
    snom = n.lines.loc[pocket_line_ids, "s_nom"].astype(float).values
    sn_safe = np.where(snom > 0, snom, np.inf)
    flows = n.lines_t.p0[pocket_line_ids].values                # (T, L_pocket)
    abs_loading = np.abs(flows) / sn_safe[None, :]
    # worst hour = the one maximising any pocket line's loading
    worst_per_h = abs_loading.max(axis=1)
    t_worst = int(np.argmax(worst_per_h))
    log.info(f"Worst pocket hour: t={t_worst} ({n.snapshots[t_worst]}) "
             f"max_loading={worst_per_h[t_worst]:.2f}x")

    # ---- Bus-level gen and load at that hour ----
    snap = n.snapshots[t_worst]
    gens = n.generators
    gen_at_t = n.generators_t.p.loc[snap]                       # MW per generator
    gen_by_bus = gen_at_t.groupby(gens["bus"]).sum()
    load_at_t = n.loads_t.p_set.loc[snap] if (n.loads_t.p is None or n.loads_t.p.empty) else n.loads_t.p.loc[snap]
    load_by_bus = load_at_t.groupby(n.loads["bus"]).sum()
    # Carrier breakdown per bus
    gens["__p"] = gen_at_t.reindex(gens.index).fillna(0).values
    gen_by_bus_car = (gens[gens["bus"].isin(pocket) & (gens["__p"].abs() > 0.1)]
                      .groupby(["bus", "carrier"])["__p"].sum())

    # ---- Build payload ----
    buses_pl = []
    for b in pocket:
        if b not in bx: continue
        if not (np.isfinite(bx[b]) and np.isfinite(by[b]) and bx[b] != 0): continue
        g = float(gen_by_bus.get(b, 0))
        ld = float(load_by_bus.get(b, 0))
        # carrier mix
        car_lines = ""
        if b in gen_by_bus_car.index.get_level_values(0):
            for car, v in gen_by_bus_car.loc[b].items():
                car_lines += f"<br>&nbsp;&nbsp;{car}: {v:.0f} MW"
        buses_pl.append({
            "id": b, "lat": round(by[b], 5), "lon": round(bx[b], 5),
            "v_nom": int(bv[b]) if not np.isnan(bv[b]) else 0,
            "gen_MW": round(g, 1), "load_MW": round(ld, 1),
            "net_MW": round(g - ld, 1),
            "is_pocket_anchor": b == args.anchor,
            "carrier_html": car_lines,
        })

    # ---- Lines: only those whose BOTH endpoints are in the pocket ----
    lines_pl = []
    for lid in pocket_line_ids:
        b0 = n.lines.at[lid, "bus0"]; b1 = n.lines.at[lid, "bus1"]
        if b0 not in pocket or b1 not in pocket: continue
        if b0 not in bx or b1 not in bx: continue
        flow_mw = float(n.lines_t.p0.at[snap, lid])
        s = float(n.lines.at[lid, "s_nom"])
        loading = abs(flow_mw) / s if s > 0 else 0
        lines_pl.append({
            "id": lid, "b0": b0, "b1": b1,
            "lat0": round(by[b0], 5), "lon0": round(bx[b0], 5),
            "lat1": round(by[b1], 5), "lon1": round(bx[b1], 5),
            "v": int(n.buses.at[b0, "v_nom"]),
            "s_nom": int(s), "length_km": round(float(n.lines.at[lid, "length"]), 2),
            "flow_MW": round(flow_mw, 1),
            "loading_pct": round(loading * 100, 1),
            "from_bus": b0 if flow_mw >= 0 else b1,
            "to_bus": b1 if flow_mw >= 0 else b0,
        })

    # ---- Existing transformers in/touching pocket ----
    trafo_pl = []
    for tid in n.transformers.index:
        b0 = n.transformers.at[tid, "bus0"]; b1 = n.transformers.at[tid, "bus1"]
        if b0 not in pocket and b1 not in pocket: continue
        sn = float(n.transformers.at[tid, "s_nom"])
        flow = float(n.transformers_t.p0.at[snap, tid]) if (
            n.transformers_t.p0 is not None and tid in n.transformers_t.p0.columns) else 0
        trafo_pl.append({
            "id": tid, "b0": b0, "b1": b1,
            "lat": round((by[b0] + by[b1]) / 2, 5),
            "lon": round((bx[b0] + bx[b1]) / 2, 5),
            "s_nom": int(sn), "flow_MW": round(flow, 1),
            "v0": int(n.buses.at[b0, "v_nom"]),
            "v1": int(n.buses.at[b1, "v_nom"]),
        })

    # ---- Proposed new transformer (from my fix CSV) ----
    fixes = pd.read_csv(FIX_CSV).astype({"low_bus": str, "ehv_bus": str})
    new_trafo_pl = []
    for _, r in fixes.iterrows():
        if r["low_bus"] not in pocket and r["ehv_bus"] not in pocket: continue
        new_trafo_pl.append({
            "low_bus": r["low_bus"], "ehv_bus": r["ehv_bus"],
            "ehv_kv": int(r["ehv_kv"]), "s_nom_MVA": int(r["s_nom_MVA"]),
            "distance_km": float(r["distance_km"]),
            "lat0": round(by[r["low_bus"]], 5), "lon0": round(bx[r["low_bus"]], 5),
            "lat1": round(by[r["ehv_bus"]], 5), "lon1": round(bx[r["ehv_bus"]], 5),
        })

    payload = {
        "snapshot_iso": str(snap), "anchor": args.anchor,
        "n_buses": len(buses_pl), "n_lines": len(lines_pl),
        "buses": buses_pl, "lines": lines_pl,
        "trafos_existing": trafo_pl, "trafos_proposed": new_trafo_pl,
    }
    out = OUT_DIR / f"pocket_flows_{args.anchor}.html"
    html = HTML.replace("__PAYLOAD__", json.dumps(payload, separators=(",", ":")))
    out.write_text(html)
    log.info(f"Wrote {out} ({out.stat().st_size/1e3:.0f} KB)")

    # Print a summary
    print(f"\n=== Worst pocket hour: {snap} ===")
    print(f"Bus {args.anchor}: gen {gen_by_bus.get(args.anchor,0):.0f} MW, "
          f"load {load_by_bus.get(args.anchor,0):.0f} MW")
    print(f"\nLines in pocket, loading ≥ 50 %:")
    df_l = pd.DataFrame([l for l in lines_pl if l["loading_pct"] >= 50])
    if len(df_l):
        df_l = df_l.sort_values("loading_pct", ascending=False)
        print(df_l[["id","b0","b1","s_nom","length_km","flow_MW","loading_pct"]].to_string(index=False))


HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Pocket flow map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
  #map { height: 100vh; width: 100%; }
  .info {
    position: absolute; top: 10px; right: 10px; z-index: 1000;
    background: rgba(255,255,255,0.96); padding: 10px 14px;
    border-radius: 8px; box-shadow: 0 2px 12px rgba(0,0,0,0.15);
    font-size: 12px; max-width: 320px; line-height: 1.5;
  }
  .info h3 { margin: 0 0 6px 0; font-size: 14px; }
  .pillar { display: flex; gap: 6px; align-items: center; }
  .swatch { display: inline-block; width: 22px; height: 4px; }
  .dot { display: inline-block; width: 11px; height: 11px; border-radius: 50%; border: 1px solid #444; }
  .popup b { color: #cc2222; }
</style></head><body>
<div id="map"></div>
<div class="info" id="info"></div>
<script>
const P = __PAYLOAD__;
document.getElementById('info').innerHTML = `
  <h3>Pocket of bus ${P.anchor} — worst-hour flow</h3>
  Snapshot: <b>${P.snapshot_iso}</b><br>
  Buses shown: ${P.n_buses} &nbsp; Lines: ${P.n_lines}<br>
  <hr style="border:0;border-top:1px solid #ddd"/>
  <div><b>Line color = loading:</b></div>
  <div class="pillar"><span class="swatch" style="background:#3a7"></span> &lt; 50 %</div>
  <div class="pillar"><span class="swatch" style="background:#e9a035"></span> 50–80 %</div>
  <div class="pillar"><span class="swatch" style="background:#cc2222"></span> 80–100 %</div>
  <div class="pillar"><span class="swatch" style="background:#7c0d6c"></span> &gt; 100 % (over-loaded)</div>
  <div><b>Line thickness</b> ∝ flow MW</div>
  <hr style="border:0;border-top:1px solid #ddd"/>
  <div class="pillar"><span class="dot" style="background:#fff;border-color:#9aa1a8"></span> 110 kV bus &nbsp; (size = |net injection|)</div>
  <div class="pillar"><span class="dot" style="background:#fff;border-color:#e2891b"></span> 220 kV bus</div>
  <div class="pillar"><span class="dot" style="background:#fff;border-color:#cc2222"></span> 380 kV bus</div>
  <div class="pillar"><span class="dot" style="background:#0a0"></span> net &gt; 0 (generator)</div>
  <div class="pillar"><span class="dot" style="background:#c00"></span> net &lt; 0 (load)</div>
  <hr style="border:0;border-top:1px solid #ddd"/>
  <div class="pillar"><span class="dot" style="background:#6a4cad"></span> existing transformer</div>
  <div class="pillar"><span class="dot" style="background:#00d4ff"></span> proposed NEW transformer</div>
  <p style="margin-top:6px;color:#555">Click any element for details.</p>
`;

const allLats = P.buses.map(b=>b.lat), allLons = P.buses.map(b=>b.lon);
const bounds = L.latLngBounds([[Math.min(...allLats), Math.min(...allLons)],
                                [Math.max(...allLats), Math.max(...allLons)]]);
const map = L.map('map', { preferCanvas: false }).fitBounds(bounds, {padding:[40,40]});
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution:'&copy; OSM contributors', maxZoom:18}).addTo(map);

function lineColor(loading) {
  if (loading < 50) return '#3a7';
  if (loading < 80) return '#e9a035';
  if (loading <= 100) return '#cc2222';
  return '#7c0d6c';
}
P.lines.forEach(L_ => {
  const w = Math.max(2, Math.min(12, Math.abs(L_.flow_MW)/100));
  L.polyline([[L_.lat0, L_.lon0], [L_.lat1, L_.lon1]],
    { color: lineColor(L_.loading_pct), weight: w, opacity: 0.9 })
   .bindPopup(`<div class="popup"><b>Line ${L_.id}</b> (${L_.v} kV)<br>
     s_nom: ${L_.s_nom} MVA &middot; ${L_.length_km} km<br>
     <b>flow: ${L_.flow_MW.toFixed(0)} MW</b> &middot; loading ${L_.loading_pct.toFixed(1)} %<br>
     direction: ${L_.from_bus} &rarr; ${L_.to_bus}</div>`)
   .addTo(map);
});

P.buses.forEach(b => {
  const radius = Math.max(5, Math.min(28, Math.sqrt(Math.abs(b.net_MW))*1.5));
  const vColor = b.v_nom===380 ? '#cc2222' : b.v_nom===220 ? '#e2891b' : '#9aa1a8';
  const fill = b.net_MW > 0 ? '#0a0' : (b.net_MW < 0 ? '#c00' : '#888');
  L.circleMarker([b.lat, b.lon],
    {radius: radius, color: vColor, weight: 2,
     fillColor: fill, fillOpacity: 0.55})
   .bindPopup(`<div class="popup"><b>Bus ${b.id}</b> (${b.v_nom} kV)
     ${b.is_pocket_anchor ? ' &mdash; <span style="color:#06c"><b>pocket anchor</b></span>' : ''}<br>
     gen: ${b.gen_MW.toFixed(0)} MW<br>
     load: ${b.load_MW.toFixed(0)} MW<br>
     <b>net: ${b.net_MW.toFixed(0)} MW</b>${b.carrier_html}</div>`)
   .addTo(map);
});

P.trafos_existing.forEach(t => {
  L.circleMarker([t.lat, t.lon], {radius: 6, color: '#3a2a78', weight: 2,
    fillColor: '#a78cd9', fillOpacity: 0.95})
   .bindPopup(`<div class="popup"><b>Existing transformer ${t.id}</b><br>
     ${t.v0}/${t.v1} kV &middot; ${t.s_nom} MVA<br>
     flow at this hour: ${t.flow_MW.toFixed(0)} MW<br>
     bus0=${t.b0} bus1=${t.b1}</div>`)
   .addTo(map);
});

P.trafos_proposed.forEach(t => {
  L.polyline([[t.lat0, t.lon0], [t.lat1, t.lon1]],
    { color: '#00d4ff', weight: 4, opacity: 0.9, dashArray: '8,5' })
    .bindPopup(`<div class="popup"><b>PROPOSED new transformer</b><br>
       110 kV bus ${t.low_bus} &rarr; ${t.ehv_kv} kV bus ${t.ehv_bus}<br>
       ${t.s_nom_MVA} MVA &middot; ${t.distance_km} km</div>`)
    .addTo(map);
  L.circleMarker([t.lat0, t.lon0], {radius: 9, color:'#003', weight:2,
    fillColor:'#00d4ff', fillOpacity:0.95})
    .addTo(map);
});
</script></body></html>
"""

if __name__ == "__main__":
    main()
