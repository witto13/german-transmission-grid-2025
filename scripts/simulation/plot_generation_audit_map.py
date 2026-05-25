#!/usr/bin/env python3
"""plot_generation_audit_map.py — Map of every flagged generator/storage unit.
Colour codes by issue class. Bubble size = p_nom MW.
"""
import json
from pathlib import Path
import pandas as pd

RES = Path("/root/egon_2025_project/results")
df = pd.read_csv(RES / "audit_generation_all.csv")
df = df.dropna(subset=["lat", "lon"]).copy()

ISSUE_COLOR = {
    "GHOST": "#7c0d6c", "GHOST_CONV": "#7c0d6c",
    "AGGREGATION": "#cc2222", "AGGREGATION_CONV": "#cc2222",
    "VOLT_MISMATCH": "#e2891b",
    "OVERSIZED": "#0a8a8a",
    "EVAC_DEFICIT": "#000000",
}


def first_issue(s):
    return s.split(";")[0].split("(")[0].strip()


df["issue_class"] = df["issues"].apply(first_issue)
df["color"] = df["issue_class"].map(ISSUE_COLOR).fillna("#666")

records = df.to_dict(orient="records")
center = [df["lat"].mean(), df["lon"].mean()]

HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Generation audit map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
body{margin:0;font-family:sans-serif} #map{height:100vh}
.info{position:absolute;top:10px;right:10px;z-index:1000;background:rgba(255,255,255,0.96);padding:10px 14px;border-radius:8px;box-shadow:0 2px 12px rgba(0,0,0,0.15);font-size:12px;max-width:320px;line-height:1.5}
.info h3{margin:0 0 6px 0;font-size:14px}
.dot{display:inline-block;width:12px;height:12px;border-radius:50%;border:1px solid #333}
.row{display:flex;align-items:center;gap:6px;margin-top:3px}
</style></head><body>
<div id="map"></div>
<div class="info">
  <h3>Generator/storage audit — flagged units</h3>
  <div class="row"><span class="dot" style="background:#7c0d6c"></span> GHOST &mdash; no real plant within 30–50 km</div>
  <div class="row"><span class="dot" style="background:#cc2222"></span> AGGREGATION &mdash; modeled MW &gt;1.5&ndash;1.6&times; nearest real plant</div>
  <div class="row"><span class="dot" style="background:#e2891b"></span> VOLT_MISMATCH &mdash; voltage level differs from real</div>
  <div class="row"><span class="dot" style="background:#0a8a8a"></span> OVERSIZED &mdash; p_nom exceeds voltage-class threshold</div>
  <div class="row"><span class="dot" style="background:#000000"></span> EVAC_DEFICIT &mdash; bus's line+trafo capacity less than total gen</div>
  <p style="margin-top:6px">Bubble radius &prop; &radic;p_nom. Click for details.</p>
</div>
<script>
const PAYLOAD = __PAYLOAD__;
const map = L.map('map').setView(__CENTER__, 6);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'&copy; OSM contributors'}).addTo(map);
PAYLOAD.forEach(r => {
  const radius = Math.max(5, Math.min(28, Math.sqrt(r.p_nom_MW)*1.2));
  L.circleMarker([r.lat, r.lon], {radius, color: r.color, weight: 2, fillColor: r.color, fillOpacity: 0.55})
    .bindPopup(`<div><b>${r.carrier}</b> ${r.type} ${r.id}<br>
       <b>${r.p_nom_MW} MW</b> at ${r.v_nom} kV, bus ${r.bus} (${r.state||''})<br>
       bus evacuation: ${r.bus_evac_MVA} MVA<br>
       nearest real: <b>${r.nearest_real||'-'}</b> ${r.real_MW||''} MW @ ${r.real_kV||''} kV, ${r.dist_km||''} km away<br>
       <br>${r.issues}</div>`)
    .addTo(map);
});
</script></body></html>"""

html = (HTML
        .replace("__PAYLOAD__", json.dumps(records, default=str, separators=(",", ":")))
        .replace("__CENTER__", json.dumps(center)))
out = RES / "generation_audit_map.html"
out.write_text(html)
print(f"Wrote {out} ({out.stat().st_size/1024:.0f} KB)")
