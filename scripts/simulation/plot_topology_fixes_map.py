#!/usr/bin/env python3
"""plot_topology_fixes_map.py — Interactive Leaflet map of grid_beta with the
20 transformer additions visually highlighted, plus the worst residual-overload
locations and the dummy-connector clusters. Output: results/topology_fixes_map.html
"""
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

NC = Path("/root/egon_2025_project/results/dispatch_8760h_pf.nc")
FIX_CSV = Path("/root/egon_2025_project/data/topology_fixes_transformers.csv")
DEFECTS_CSV = Path("/root/egon_2025_project/results/topology_defects.csv")
OUT = Path("/root/egon_2025_project/results/topology_fixes_map.html")

V_COLOR = {110: "#9aa1a8", 220: "#e2891b", 380: "#cc2222"}
V_R = {110: 1.2, 220: 1.8, 380: 2.6}


def main():
    log.info(f"Loading {NC}")
    n = pypsa.Network(str(NC))
    bx = n.buses["x"].astype(float).values
    by = n.buses["y"].astype(float).values
    bv = n.buses["v_nom"].astype(float).values
    bid = list(n.buses.index)
    bidx = {b: i for i, b in enumerate(bid)}

    # Filter buses with coords (drop offshore 200xxx with NaN)
    have_xy = np.isfinite(bx) & np.isfinite(by) & (bx != 0) & (by != 0)
    log.info(f"  {have_xy.sum()} of {len(bid)} buses have coords")

    # Compact bus list
    buses_payload = []
    for i in range(len(bid)):
        if not have_xy[i]:
            continue
        v = int(bv[i]) if not np.isnan(bv[i]) else 0
        if v not in V_COLOR:
            continue
        buses_payload.append([bid[i], round(by[i], 4), round(bx[i], 4), v])

    # Lines: keep only ones with both endpoints having coords
    lines_payload = {110: [], 220: [], 380: []}
    for lid, b0, b1, v_, s_, ln_ in zip(
            n.lines.index, n.lines["bus0"], n.lines["bus1"],
            n.buses.loc[n.lines["bus0"].values, "v_nom"].values,
            n.lines["s_nom"].values, n.lines["length"].values):
        i0 = bidx.get(b0); i1 = bidx.get(b1)
        if i0 is None or i1 is None: continue
        if not (have_xy[i0] and have_xy[i1]): continue
        v = int(v_) if not np.isnan(v_) else 0
        if v not in lines_payload: continue
        lines_payload[v].append([round(by[i0], 4), round(bx[i0], 4),
                                  round(by[i1], 4), round(bx[i1], 4)])
    log.info(f"  lines: 110={len(lines_payload[110])} 220={len(lines_payload[220])} 380={len(lines_payload[380])}")

    # Existing transformers (small purple dots at the midpoint)
    trafos_payload = []
    for tid, b0, b1, sn in zip(n.transformers.index, n.transformers["bus0"],
                                n.transformers["bus1"], n.transformers["s_nom"]):
        i0 = bidx.get(b0); i1 = bidx.get(b1)
        if i0 is None or i1 is None: continue
        if not (have_xy[i0] and have_xy[i1]): continue
        trafos_payload.append([
            round((by[i0] + by[i1]) / 2, 4), round((bx[i0] + bx[i1]) / 2, 4),
            int(sn), b0, b1,
            int(bv[i0]) if not np.isnan(bv[i0]) else 0,
            int(bv[i1]) if not np.isnan(bv[i1]) else 0,
        ])
    log.info(f"  existing transformers: {len(trafos_payload)}")

    # NEW transformer additions (from CSV)
    fixes = pd.read_csv(FIX_CSV).astype({"low_bus": str, "ehv_bus": str})
    fix_payload = []
    for _, r in fixes.iterrows():
        lo, hi = r["low_bus"], r["ehv_bus"]
        i0 = bidx.get(lo); i1 = bidx.get(hi)
        if i0 is None or i1 is None: continue
        if not (have_xy[i0] and have_xy[i1]): continue
        # classify "real-ish" vs "made-up" by distance threshold
        is_long = float(r["distance_km"]) > 10
        fix_payload.append({
            "low_bus": lo, "ehv_bus": hi, "ehv_kv": int(r["ehv_kv"]),
            "s_nom_MVA": int(r["s_nom_MVA"]),
            "distance_km": float(r["distance_km"]),
            "pocket_peak_MW": float(r["pocket_peak_flow_MW"]),
            "pocket_gen_MW": float(r["pocket_gen_MW"]),
            "low_lat": round(by[i0], 4), "low_lon": round(bx[i0], 4),
            "ehv_lat": round(by[i1], 4), "ehv_lon": round(bx[i1], 4),
            "interpretation": "made-up (>10 km — proposed new infrastructure)"
                              if is_long else "plausibly real but missing (≤10 km — same substation)",
        })

    # Worst residual overload lines (from defects CSV) for context
    defects = pd.read_csv(DEFECTS_CSV)
    worst = defects[defects["class"] == "B_radial_overloaded"].nlargest(12, "peak_loading")
    worst_payload = []
    for _, r in worst.iterrows():
        b0 = str(int(float(r["bus0"]))); b1 = str(int(float(r["bus1"])))
        i0 = bidx.get(b0); i1 = bidx.get(b1)
        if i0 is None or i1 is None: continue
        if not (have_xy[i0] and have_xy[i1]): continue
        worst_payload.append({
            "line_id": str(r["line_id"]),
            "loading": round(float(r["peak_loading"]), 2),
            "s_nom": int(r["s_nom"]),
            "peak_MW": int(float(r["peak_flow_MW"])),
            "lat0": round(by[i0], 4), "lon0": round(bx[i0], 4),
            "lat1": round(by[i1], 4), "lon1": round(bx[i1], 4),
        })

    payload = {
        "buses": buses_payload,
        "lines110": lines_payload[110],
        "lines220": lines_payload[220],
        "lines380": lines_payload[380],
        "trafos_existing": trafos_payload,
        "trafos_new": fix_payload,
        "worst_radial": worst_payload,
    }
    log.info(f"  payload sizes: buses={len(buses_payload)}, "
             f"new trafos={len(fix_payload)}, worst radial={len(worst_payload)}")

    html = HTML_TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, separators=(",", ":")))
    OUT.write_text(html)
    log.info(f"Wrote {OUT}  ({OUT.stat().st_size/1e6:.1f} MB)")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>grid_beta — topology fixes map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  #map { height: 100vh; width: 100%; }
  .info {
    position: absolute; top: 10px; right: 10px; z-index: 1000;
    background: rgba(255,255,255,0.96); padding: 10px 14px;
    border-radius: 8px; box-shadow: 0 2px 12px rgba(0,0,0,0.15);
    font-size: 12px; max-width: 280px; line-height: 1.4;
  }
  .info h3 { margin: 0 0 6px 0; font-size: 14px; }
  .legend-row { display: flex; align-items: center; gap: 6px; margin-top: 3px; }
  .swatch { display: inline-block; width: 18px; height: 4px; }
  .dot { display: inline-block; width: 12px; height: 12px; border-radius: 50%; }
  .popup b { color: #cc2222; }
</style></head>
<body>
<div id="map"></div>
<div class="info">
  <h3>grid_beta + 20 transformer fixes</h3>
  <div class="legend-row"><span class="swatch" style="background:#9aa1a8"></span> 110 kV lines</div>
  <div class="legend-row"><span class="swatch" style="background:#e2891b"></span> 220 kV lines</div>
  <div class="legend-row"><span class="swatch" style="background:#cc2222"></span> 380 kV lines</div>
  <div class="legend-row"><span class="dot" style="background:#6a4cad;border:1px solid #432"></span> existing transformers</div>
  <div class="legend-row"><span class="dot" style="background:#00d4ff;border:2px solid #003"></span> NEW trafo &le;10 km (probably real, missing in data)</div>
  <div class="legend-row"><span class="dot" style="background:#ff8800;border:2px solid #410"></span> NEW trafo &gt;10 km (made-up; proposed new infra)</div>
  <div class="legend-row"><span class="swatch" style="background:#ff0000;height:6px"></span> worst residual 110 kV overload (post-fix)</div>
  <p style="margin-top:8px;color:#555;">Toggle layers via the box top-left. Click any marker for details.</p>
</div>
<script>
const PAYLOAD = __PAYLOAD__;
const map = L.map('map', { preferCanvas: true }).setView([51.2, 10.3], 6);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution:'&copy; OSM contributors', maxZoom:18}).addTo(map);

function lineLayer(arr, color, weight) {
  const layer = L.layerGroup();
  arr.forEach(c => L.polyline([[c[0], c[1]], [c[2], c[3]]],
       { color: color, weight: weight, opacity: 0.55, interactive: false }).addTo(layer));
  return layer;
}
const ll110 = lineLayer(PAYLOAD.lines110, '#9aa1a8', 1.0);
const ll220 = lineLayer(PAYLOAD.lines220, '#e2891b', 1.6);
const ll380 = lineLayer(PAYLOAD.lines380, '#cc2222', 2.2);
ll110.addTo(map); ll220.addTo(map); ll380.addTo(map);

// Existing transformers
const trafoLayer = L.layerGroup();
PAYLOAD.trafos_existing.forEach(t => {
  L.circleMarker([t[0], t[1]], {radius: 2.5, color: '#6a4cad', weight: 1,
    fillColor: '#a78cd9', fillOpacity: 0.85})
    .bindPopup(`<div class="popup">Existing transformer<br>${t[5]}/${t[6]} kV &middot; ${t[2]} MVA<br>bus0=${t[3]} bus1=${t[4]}</div>`)
    .addTo(trafoLayer);
});
trafoLayer.addTo(map);

// NEW transformer additions
const newLayer = L.layerGroup();
PAYLOAD.trafos_new.forEach(t => {
  const isLong = t.distance_km > 10;
  const color = isLong ? '#ff8800' : '#00d4ff';
  // dashed line connecting low_bus and ehv_bus
  L.polyline([[t.low_lat, t.low_lon], [t.ehv_lat, t.ehv_lon]],
    { color: color, weight: 4, opacity: 0.85, dashArray: '8,4' }).addTo(newLayer);
  // marker at the 110 kV side (anchor)
  L.circleMarker([t.low_lat, t.low_lon], {radius: 8, color: '#001',
    weight: 2, fillColor: color, fillOpacity: 0.95})
    .bindPopup(`<div class="popup">
       <b>NEW transformer added</b> (${t.interpretation})<br>
       <b>110 kV bus:</b> ${t.low_bus}<br>
       <b>${t.ehv_kv} kV bus:</b> ${t.ehv_bus}<br>
       <b>s_nom:</b> ${t.s_nom_MVA} MVA<br>
       <b>distance:</b> ${t.distance_km} km<br>
       <b>pocket peak transit:</b> ${t.pocket_peak_MW.toFixed(0)} MW<br>
       <b>pocket installed gen:</b> ${t.pocket_gen_MW.toFixed(0)} MW
     </div>`)
    .addTo(newLayer);
});
newLayer.addTo(map);

// Worst residual radial overloads
const worstLayer = L.layerGroup();
PAYLOAD.worst_radial.forEach(w => {
  L.polyline([[w.lat0, w.lon0], [w.lat1, w.lon1]],
    { color: '#ff0000', weight: 5, opacity: 0.9 })
    .bindPopup(`<div class="popup"><b>Residual radial overload (pre-fix)</b><br>line ${w.line_id} &middot; ${w.s_nom} MVA<br>peak ${w.peak_MW} MW (${w.loading}&times;)</div>`)
    .addTo(worstLayer);
});
worstLayer.addTo(map);

// Bus dots (rendered last so they overlap lines)
const busLayer = L.layerGroup();
PAYLOAD.buses.forEach(b => {
  const v = b[3];
  const color = v===380 ? '#cc2222' : v===220 ? '#e2891b' : '#9aa1a8';
  const r = v===380 ? 1.6 : v===220 ? 1.2 : 0.7;
  L.circleMarker([b[1], b[2]], {radius: r, color: color, weight: 0,
    fillColor: color, fillOpacity: 0.7, interactive: false}).addTo(busLayer);
});
busLayer.addTo(map);

// Layer control
L.control.layers(null, {
  "110 kV lines": ll110, "220 kV lines": ll220, "380 kV lines": ll380,
  "Existing transformers": trafoLayer,
  "NEW transformers (the 20 fixes)": newLayer,
  "Worst residual radial overloads": worstLayer,
  "Buses (dots)": busLayer,
}, { collapsed: false, position: 'topleft' }).addTo(map);
</script></body></html>
"""


if __name__ == "__main__":
    main()
