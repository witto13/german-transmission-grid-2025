#!/usr/bin/env python3
"""
Generate an interactive HTML map of power flow results.

Features:
- Lines colored by loading (green → yellow → red → purple)
- Filter by voltage level (110/220/380 kV)
- Slider to select hour of day (0-23)
- Info panel: generation by carrier, total load, overloaded lines
"""

import json
import numpy as np
import pandas as pd
import pypsa
import logging
import warnings

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
log = logging.getLogger(__name__)

NC_FILE = '/root/egon_2025_project/results/powerflow_april15.nc'
OUTPUT = '/root/egon_2025_project/results/powerflow_map.html'


def main():
    log.info("Loading network...")
    n = pypsa.Network(NC_FILE)

    # ── Bus data ─────────────────────────────────────────────────────────
    log.info("Preparing bus data...")
    bus_data = {}
    for bus_id, row in n.buses.iterrows():
        bus_data[bus_id] = {
            'lat': round(row['y'], 5),
            'lon': round(row['x'], 5),
            'v': int(row['v_nom']),
        }

    # ── Line data (with hourly flows) ────────────────────────────────────
    log.info("Preparing line data...")
    lines_json = []
    for line_id, row in n.lines.iterrows():
        b0 = row['bus0']
        b1 = row['bus1']
        if b0 not in bus_data or b1 not in bus_data:
            continue
        s_nom = row['s_nom']
        v_nom = int(row['v_nom']) if not np.isnan(row['v_nom']) else 110

        # Hourly absolute flows
        flows = n.lines_t.p0[line_id].abs().values
        # Loading percentages
        if s_nom > 0:
            loadings = (flows / s_nom * 100).tolist()
        else:
            loadings = [0.0] * 24

        lines_json.append({
            'id': str(line_id),
            'b0': [bus_data[b0]['lat'], bus_data[b0]['lon']],
            'b1': [bus_data[b1]['lat'], bus_data[b1]['lon']],
            'v': v_nom,
            'sn': round(s_nom, 1),
            'f': [round(f, 1) for f in flows.tolist()],
            'l': [round(l, 1) for l in loadings],
        })

    # ── Transformer data ─────────────────────────────────────────────────
    log.info("Preparing transformer data...")
    trafo_json = []
    for t_id, row in n.transformers.iterrows():
        b0 = row['bus0']
        b1 = row['bus1']
        if b0 not in bus_data or b1 not in bus_data:
            continue
        trafo_json.append({
            'b0': [bus_data[b0]['lat'], bus_data[b0]['lon']],
            'b1': [bus_data[b1]['lat'], bus_data[b1]['lon']],
            'sn': round(row['s_nom'], 1),
        })

    # ── Hourly dispatch by carrier ───────────────────────────────────────
    log.info("Preparing dispatch data...")
    gen_p = n.generators_t.p
    carriers = n.generators.carrier

    carrier_order = ['solar', 'onwind', 'offwind', 'run_of_river', 'biogas',
                     'biomass', 'waste', 'lignite', 'coal', 'gas', 'oil',
                     'other', 'reservoir', 'hydrogen']

    dispatch_by_hour = {}
    for h in range(24):
        snap = n.snapshots[h]
        hourly = {}
        for carrier in carrier_order:
            gen_ids = n.generators[carriers == carrier].index
            if len(gen_ids) > 0:
                hourly[carrier] = round(gen_p.loc[snap, gen_ids].sum(), 0)
            else:
                hourly[carrier] = 0
        dispatch_by_hour[h] = hourly

    # ── Hourly load ──────────────────────────────────────────────────────
    load_by_hour = {}
    for h in range(24):
        snap = n.snapshots[h]
        load_by_hour[h] = round(n.loads_t.p_set.loc[snap].sum(), 0)

    # ── Hourly overloaded line counts ────────────────────────────────────
    overload_by_hour = {}
    for h in range(24):
        snap = n.snapshots[h]
        flow = n.lines_t.p0.loc[snap].abs()
        loading_pct = flow / n.lines.s_nom * 100
        loading_pct = loading_pct.replace([np.inf, -np.inf], 0).fillna(0)
        overload_by_hour[h] = {
            'gt100': int((loading_pct > 100).sum()),
            'gt200': int((loading_pct > 200).sum()),
            'gt50': int((loading_pct > 50).sum()),
            'mean': round(loading_pct.mean(), 1),
            'max': round(loading_pct.max(), 1),
        }

    # ── Bus counts by voltage ────────────────────────────────────────────
    bus_counts = n.buses.v_nom.value_counts().to_dict()
    line_counts = {}
    for v in [110, 220, 380]:
        line_counts[v] = int((n.lines.v_nom == v).sum())

    # ── Installed capacity by carrier ────────────────────────────────────
    installed = {}
    for carrier in carrier_order:
        gen_ids = n.generators[carriers == carrier].index
        installed[carrier] = round(n.generators.loc[gen_ids, 'p_nom'].sum(), 0)

    # ── Build HTML ───────────────────────────────────────────────────────
    log.info(f"Building HTML ({len(lines_json)} lines, {len(bus_data)} buses)...")

    # Carrier display config
    carrier_colors = {
        'solar': '#FFD700',
        'onwind': '#4CAF50',
        'offwind': '#00BCD4',
        'run_of_river': '#2196F3',
        'biogas': '#8BC34A',
        'biomass': '#689F38',
        'waste': '#795548',
        'lignite': '#5D4037',
        'coal': '#424242',
        'gas': '#FF9800',
        'oil': '#F44336',
        'other': '#9E9E9E',
        'reservoir': '#1565C0',
        'hydrogen': '#E040FB',
    }

    carrier_labels = {
        'solar': 'Solar',
        'onwind': 'Wind Onshore',
        'offwind': 'Wind Offshore',
        'run_of_river': 'Run of River',
        'biogas': 'Biogas',
        'biomass': 'Biomass',
        'waste': 'Waste',
        'lignite': 'Lignite',
        'coal': 'Coal',
        'gas': 'Natural Gas',
        'oil': 'Oil',
        'other': 'Other',
        'reservoir': 'Hydro Reservoir',
        'hydrogen': 'Hydrogen',
    }

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>eGon2025 Power Flow - April 15, 2025</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #1a1a2e; }}
  #map {{ position: absolute; top: 0; right: 0; bottom: 0; left: 370px; z-index: 1; }}

  #sidebar {{
    position: absolute; top: 0; left: 0; bottom: 0; width: 370px;
    background: #16213e; color: #e0e0e0; overflow-y: auto; z-index: 2;
    padding: 16px; font-size: 13px;
  }}
  #sidebar h1 {{ font-size: 16px; color: #fff; margin-bottom: 4px; }}
  #sidebar .subtitle {{ font-size: 11px; color: #8899aa; margin-bottom: 16px; }}

  .panel {{
    background: #1a1a3e; border: 1px solid #2a2a5e; border-radius: 8px;
    padding: 12px; margin-bottom: 12px;
  }}
  .panel h2 {{ font-size: 13px; color: #64b5f6; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 1px; }}

  /* Hour slider */
  .hour-display {{
    text-align: center; font-size: 28px; font-weight: 700; color: #fff;
    margin: 4px 0;
  }}
  .hour-label {{ text-align: center; color: #8899aa; font-size: 11px; margin-bottom: 8px; }}
  input[type=range] {{
    width: 100%; height: 6px; -webkit-appearance: none; background: #2a2a5e;
    border-radius: 3px; outline: none;
  }}
  input[type=range]::-webkit-slider-thumb {{
    -webkit-appearance: none; width: 20px; height: 20px; border-radius: 50%;
    background: #64b5f6; cursor: pointer;
  }}
  .hour-ticks {{
    display: flex; justify-content: space-between; color: #556; font-size: 10px;
    margin-top: 2px;
  }}
  .play-btn {{
    display: block; margin: 8px auto 0; padding: 4px 20px; background: #64b5f6;
    color: #16213e; border: none; border-radius: 4px; cursor: pointer;
    font-weight: 600; font-size: 12px;
  }}
  .play-btn:hover {{ background: #90caf9; }}

  /* Voltage filters */
  .vfilter {{ display: flex; gap: 8px; }}
  .vfilter label {{
    flex: 1; text-align: center; padding: 6px 0; border-radius: 4px;
    cursor: pointer; font-weight: 600; font-size: 12px; transition: all 0.15s;
  }}
  .vfilter input {{ display: none; }}
  .vfilter .v110 {{ background: #2e7d32; color: #fff; opacity: 0.4; }}
  .vfilter .v220 {{ background: #f57f17; color: #fff; opacity: 0.4; }}
  .vfilter .v380 {{ background: #c62828; color: #fff; opacity: 0.4; }}
  .vfilter input:checked + .v110, .vfilter input:checked + .v220,
  .vfilter input:checked + .v380 {{ opacity: 1; box-shadow: 0 0 8px rgba(255,255,255,0.2); }}

  /* Dispatch table */
  .dispatch-row {{
    display: flex; align-items: center; padding: 3px 0;
    border-bottom: 1px solid #1e1e4e;
  }}
  .dispatch-row:last-child {{ border-bottom: none; }}
  .carrier-dot {{
    width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; flex-shrink: 0;
  }}
  .carrier-name {{ flex: 1; }}
  .carrier-val {{ font-weight: 600; color: #fff; text-align: right; min-width: 70px; }}
  .carrier-bar-bg {{
    width: 60px; height: 6px; background: #2a2a5e; border-radius: 3px;
    margin-left: 6px; flex-shrink: 0; overflow: hidden;
  }}
  .carrier-bar {{ height: 100%; border-radius: 3px; transition: width 0.3s; }}

  /* Stats */
  .stat-row {{ display: flex; justify-content: space-between; padding: 4px 0; }}
  .stat-label {{ color: #8899aa; }}
  .stat-val {{ font-weight: 700; color: #fff; }}
  .stat-val.warn {{ color: #ff9800; }}
  .stat-val.danger {{ color: #f44336; }}
  .stat-val.good {{ color: #4caf50; }}

  /* Load bar */
  .load-bar-bg {{
    height: 18px; background: #2a2a5e; border-radius: 4px; margin-top: 6px;
    overflow: hidden; position: relative;
  }}
  .load-bar {{
    height: 100%; background: linear-gradient(90deg, #64b5f6, #1565c0);
    border-radius: 4px; transition: width 0.3s;
  }}
  .load-bar-text {{
    position: absolute; top: 0; left: 0; right: 0; height: 100%;
    display: flex; align-items: center; justify-content: center;
    color: #fff; font-size: 11px; font-weight: 600;
  }}

  /* Legend */
  .legend-bar {{
    height: 12px; border-radius: 3px; margin: 6px 0 2px;
    background: linear-gradient(90deg, #4caf50, #cddc39, #ffeb3b, #ff9800, #f44336, #9c27b0);
  }}
  .legend-labels {{ display: flex; justify-content: space-between; font-size: 10px; color: #8899aa; }}

  /* Line counts */
  .line-counts {{ display: flex; gap: 8px; margin-top: 4px; }}
  .line-count-chip {{
    flex: 1; text-align: center; padding: 4px; border-radius: 4px;
    background: #2a2a5e; font-size: 11px;
  }}
  .line-count-chip .num {{ font-size: 16px; font-weight: 700; color: #fff; }}
</style>
</head>
<body>

<div id="sidebar">
  <h1>eGon2025 Power Flow</h1>
  <div class="subtitle">April 15, 2025 &mdash; 7,316 buses &middot; 10,863 lines &middot; 24,972 generators</div>

  <!-- Hour selector -->
  <div class="panel">
    <h2>Time of Day</h2>
    <div class="hour-display"><span id="hourVal">12</span>:00</div>
    <div class="hour-label" id="hourDesc">Peak solar production</div>
    <input type="range" id="hourSlider" min="0" max="23" value="12" step="1">
    <div class="hour-ticks"><span>0</span><span>6</span><span>12</span><span>18</span><span>23</span></div>
    <button class="play-btn" id="playBtn" onclick="togglePlay()">&#9654; Play</button>
  </div>

  <!-- Voltage filter -->
  <div class="panel">
    <h2>Voltage Level</h2>
    <div class="vfilter">
      <label><input type="checkbox" id="v110" checked onchange="updateMap()"><span class="v110">110 kV</span></label>
      <label><input type="checkbox" id="v220" checked onchange="updateMap()"><span class="v220">220 kV</span></label>
      <label><input type="checkbox" id="v380" checked onchange="updateMap()"><span class="v380">380 kV</span></label>
    </div>
    <div class="line-counts" id="lineCounts"></div>
  </div>

  <!-- Load -->
  <div class="panel">
    <h2>System Load</h2>
    <div class="stat-row">
      <span class="stat-label">Total demand</span>
      <span class="stat-val" id="loadVal">--</span>
    </div>
    <div class="load-bar-bg">
      <div class="load-bar" id="loadBar" style="width:80%"></div>
      <div class="load-bar-text" id="loadBarText">--</div>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:#556;margin-top:2px">
      <span>0 GW</span><span>76.2 GW peak</span>
    </div>
  </div>

  <!-- Generation dispatch -->
  <div class="panel">
    <h2>Generation Dispatch</h2>
    <div class="stat-row" style="margin-bottom:6px">
      <span class="stat-label">Total generation</span>
      <span class="stat-val" id="totalGen">--</span>
    </div>
    <div id="dispatchList"></div>
  </div>

  <!-- Line loading -->
  <div class="panel">
    <h2>Line Loading</h2>
    <div class="legend-bar"></div>
    <div class="legend-labels"><span>0%</span><span>50%</span><span>100%</span><span>200%</span><span>500%+</span></div>
    <div style="margin-top:10px">
      <div class="stat-row">
        <span class="stat-label">Mean loading</span>
        <span class="stat-val" id="meanLoading">--</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Max loading</span>
        <span class="stat-val" id="maxLoading">--</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Lines &gt; 50%</span>
        <span class="stat-val" id="gt50">--</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Lines &gt; 100% (overloaded)</span>
        <span class="stat-val danger" id="gt100">--</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Lines &gt; 200% (critical)</span>
        <span class="stat-val danger" id="gt200">--</span>
      </div>
    </div>
  </div>
</div>

<div id="map"></div>

<script>
// ── Embedded data ──────────────────────────────────────────────────────
const LINES = {json.dumps(lines_json, separators=(',', ':'))};
const TRAFOS = {json.dumps(trafo_json, separators=(',', ':'))};
const DISPATCH = {json.dumps(dispatch_by_hour, separators=(',', ':'))};
const LOAD = {json.dumps(load_by_hour, separators=(',', ':'))};
const OVERLOAD = {json.dumps(overload_by_hour, separators=(',', ':'))};
const INSTALLED = {json.dumps(installed, separators=(',', ':'))};
const PEAK_LOAD = 76201;

const CARRIER_COLORS = {json.dumps(carrier_colors, separators=(',', ':'))};
const CARRIER_LABELS = {json.dumps(carrier_labels, separators=(',', ':'))};
const CARRIER_ORDER = {json.dumps(carrier_order, separators=(',', ':'))};

const HOUR_DESCS = [
  "Night - low load, no solar", "Night - minimum demand", "Night - valley",
  "Night - minimum demand", "Night - demand rising", "Early morning",
  "Dawn - demand ramping up", "Morning - solar starting", "Morning - solar ramping",
  "Mid-morning - renewables rising", "Late morning - high RE",
  "Approaching noon peak", "Noon - peak solar production",
  "Early afternoon - high solar", "Afternoon - solar still strong",
  "Afternoon - solar declining", "Late afternoon",
  "Early evening - solar fading", "Evening - solar ending, demand peak",
  "Evening - demand high, solar gone", "Night beginning - demand dropping",
  "Night - demand declining", "Night - demand declining", "Night - approaching minimum"
];

// ── Map setup ──────────────────────────────────────────────────────────
const map = L.map('map', {{
  center: [51.2, 10.4],
  zoom: 6,
  preferCanvas: true,
  renderer: L.canvas({{ padding: 0.5 }})
}});

L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; OSM &copy; CARTO',
  maxZoom: 18
}}).addTo(map);

// ── Line layers ────────────────────────────────────────────────────────
let lineLayers = {{ 110: [], 220: [], 380: [] }};
let trafoLayer = [];

function loadingColor(pct) {{
  if (pct <= 25) return '#4caf50';
  if (pct <= 50) return '#cddc39';
  if (pct <= 75) return '#ffeb3b';
  if (pct <= 100) return '#ff9800';
  if (pct <= 200) return '#f44336';
  return '#9c27b0';
}}

function lineWeight(v) {{
  if (v >= 380) return 2.5;
  if (v >= 220) return 1.8;
  return 1.0;
}}

// Create all line polylines
LINES.forEach(line => {{
  const pl = L.polyline([line.b0, line.b1], {{
    color: '#4caf50',
    weight: lineWeight(line.v),
    opacity: 0.7,
  }});
  pl._lineData = line;
  pl.bindTooltip('', {{ sticky: true, className: 'line-tip' }});
  lineLayers[line.v].push(pl);
}});

// Create transformer lines (dashed)
TRAFOS.forEach(t => {{
  const pl = L.polyline([t.b0, t.b1], {{
    color: '#b0bec5',
    weight: 2,
    opacity: 0.4,
    dashArray: '4 4',
  }});
  pl.bindTooltip(`Transformer: ${{t.sn}} MVA`, {{ sticky: true }});
  trafoLayer.push(pl);
}});

// Layer groups
let lineGroups = {{
  110: L.layerGroup(lineLayers[110]),
  220: L.layerGroup(lineLayers[220]),
  380: L.layerGroup(lineLayers[380]),
}};
let trafoGroup = L.layerGroup(trafoLayer);

// Add all to map initially
Object.values(lineGroups).forEach(g => g.addTo(map));
trafoGroup.addTo(map);

// ── Update function ────────────────────────────────────────────────────
let currentHour = 12;
let playing = false;
let playTimer = null;

function updateMap() {{
  const h = currentHour;
  const show110 = document.getElementById('v110').checked;
  const show220 = document.getElementById('v220').checked;
  const show380 = document.getElementById('v380').checked;

  // Toggle layer visibility
  if (show110) {{ if (!map.hasLayer(lineGroups[110])) lineGroups[110].addTo(map); }}
  else {{ map.removeLayer(lineGroups[110]); }}
  if (show220) {{ if (!map.hasLayer(lineGroups[220])) lineGroups[220].addTo(map); }}
  else {{ map.removeLayer(lineGroups[220]); }}
  if (show380) {{ if (!map.hasLayer(lineGroups[380])) lineGroups[380].addTo(map); }}
  else {{ map.removeLayer(lineGroups[380]); }}

  // Update line colors for current hour
  [110, 220, 380].forEach(v => {{
    lineLayers[v].forEach(pl => {{
      const d = pl._lineData;
      const loading = d.l[h];
      const flow = d.f[h];
      pl.setStyle({{ color: loadingColor(loading), opacity: 0.75 }});
      pl.setTooltipContent(
        `<b>${{d.v}} kV</b> (Line ${{d.id}})<br>` +
        `Flow: <b>${{flow.toFixed(0)}} MW</b> / ${{d.sn}} MVA<br>` +
        `Loading: <b>${{loading.toFixed(1)}}%</b>`
      );
    }});
  }});

  // Update hour display
  document.getElementById('hourVal').textContent = String(h).padStart(2, '0');
  document.getElementById('hourDesc').textContent = HOUR_DESCS[h];

  // Update load
  const load = LOAD[h];
  document.getElementById('loadVal').textContent = (load / 1000).toFixed(1) + ' GW';
  const loadPct = (load / PEAK_LOAD * 100);
  document.getElementById('loadBar').style.width = loadPct + '%';
  document.getElementById('loadBarText').textContent = (load / 1000).toFixed(1) + ' GW (' + loadPct.toFixed(0) + '%)';

  // Update dispatch
  const disp = DISPATCH[h];
  let total = 0;
  let maxDisp = 0;
  CARRIER_ORDER.forEach(c => {{
    total += disp[c] || 0;
    maxDisp = Math.max(maxDisp, disp[c] || 0);
  }});
  document.getElementById('totalGen').textContent = (total / 1000).toFixed(1) + ' GW';

  let dispHTML = '';
  CARRIER_ORDER.forEach(c => {{
    const val = disp[c] || 0;
    if (val < 1 && INSTALLED[c] < 100) return;
    const pct = maxDisp > 0 ? (val / maxDisp * 100) : 0;
    const inst = INSTALLED[c];
    dispHTML += `
      <div class="dispatch-row">
        <div class="carrier-dot" style="background:${{CARRIER_COLORS[c]}}"></div>
        <div class="carrier-name">${{CARRIER_LABELS[c]}}</div>
        <div class="carrier-val">${{val >= 1000 ? (val/1000).toFixed(1) + ' GW' : val.toFixed(0) + ' MW'}}</div>
        <div class="carrier-bar-bg"><div class="carrier-bar" style="width:${{pct}}%;background:${{CARRIER_COLORS[c]}}"></div></div>
      </div>`;
  }});
  document.getElementById('dispatchList').innerHTML = dispHTML;

  // Update overload stats
  const ol = OVERLOAD[h];
  document.getElementById('meanLoading').textContent = ol.mean + '%';
  document.getElementById('maxLoading').textContent = ol.max + '%';
  document.getElementById('maxLoading').className = 'stat-val' + (ol.max > 100 ? ' danger' : ' good');
  document.getElementById('gt50').textContent = ol.gt50;
  document.getElementById('gt50').className = 'stat-val' + (ol.gt50 > 500 ? ' warn' : '');
  document.getElementById('gt100').textContent = ol.gt100;
  document.getElementById('gt200').textContent = ol.gt200;

  // Update line counts for visible voltages
  let lcHTML = '';
  [110, 220, 380].forEach(v => {{
    const checked = document.getElementById('v' + v).checked;
    const cnt = lineLayers[v].length;
    lcHTML += `<div class="line-count-chip" style="opacity:${{checked ? 1 : 0.3}}">
      <div class="num">${{cnt.toLocaleString()}}</div>
      <div>${{v}} kV</div>
    </div>`;
  }});
  document.getElementById('lineCounts').innerHTML = lcHTML;
}}

// ── Slider events ──────────────────────────────────────────────────────
const slider = document.getElementById('hourSlider');
slider.addEventListener('input', function() {{
  currentHour = parseInt(this.value);
  updateMap();
}});

function togglePlay() {{
  playing = !playing;
  const btn = document.getElementById('playBtn');
  if (playing) {{
    btn.innerHTML = '&#9724; Pause';
    playTimer = setInterval(() => {{
      currentHour = (currentHour + 1) % 24;
      slider.value = currentHour;
      updateMap();
    }}, 800);
  }} else {{
    btn.innerHTML = '&#9654; Play';
    clearInterval(playTimer);
  }}
}}

// Initial render
updateMap();
</script>
</body>
</html>"""

    with open(OUTPUT, 'w') as f:
        f.write(html)

    log.info(f"Map written to {OUTPUT}")
    import os
    size_mb = os.path.getsize(OUTPUT) / 1e6
    log.info(f"File size: {size_mb:.1f} MB")


if __name__ == '__main__':
    main()
