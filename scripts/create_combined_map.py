#!/usr/bin/env python3
"""
Generate a combined interactive HTML map with grid topology + power flow results.

Features:
- Light / Dark mode toggle
- Two view modes: Grid (voltage-colored) and Power Flow (loading heatmap)
- Filter by voltage level (110 / 220 / 380 kV)
- Bus hover tooltips with installed capacity & load breakdown
- Hour slider for power flow animation (0-23)
- Loading heatmap: 0% green → 50% yellow → 100% red → >100% stays red
"""

import json
import numpy as np
import pandas as pd
import pypsa
from sqlalchemy import create_engine
import logging
import warnings

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
log = logging.getLogger(__name__)

NC_FILE = '/root/egon_2025_project/results/powerflow_april15.nc'
DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
SCENARIO = 'eGon2025'
OUTPUT = '/root/egon_2025_project/results/combined_grid_map.html'


def main():
    log.info("Loading PyPSA network...")
    n = pypsa.Network(NC_FILE)

    log.info("Loading bus generation/load data from database...")
    engine = create_engine(DB_URI)

    # Generators aggregated by bus and carrier
    generators = pd.read_sql(f"""
        SELECT bus, carrier, SUM(p_nom) as capacity_mw, COUNT(*) as n_units
        FROM grid.egon_etrago_generator
        WHERE scn_name = '{SCENARIO}'
        GROUP BY bus, carrier
    """, engine)

    # Loads aggregated by bus and type
    loads = pd.read_sql(f"""
        SELECT bus, type, SUM(p_set) as load_mw, COUNT(*) as n_units
        FROM grid.egon_etrago_load
        WHERE scn_name = '{SCENARIO}'
        GROUP BY bus, type
    """, engine)

    # Storage aggregated by bus and carrier
    storage = pd.read_sql(f"""
        SELECT bus, carrier, SUM(p_nom) as capacity_mw, COUNT(*) as n_units
        FROM grid.egon_etrago_storage
        WHERE scn_name = '{SCENARIO}'
        GROUP BY bus, carrier
    """, engine)

    # ── Build generation breakdown per bus ─────────────────────────────
    gen_by_bus = {}
    for _, row in generators.iterrows():
        bus_id = int(row['bus'])
        carrier = row['carrier']
        cap = round(float(row['capacity_mw']), 1)
        if bus_id not in gen_by_bus:
            gen_by_bus[bus_id] = {}
        gen_by_bus[bus_id][carrier] = gen_by_bus[bus_id].get(carrier, 0) + cap

    # ── Build load breakdown per bus ───────────────────────────────────
    load_by_bus = {}
    for _, row in loads.iterrows():
        bus_id = int(row['bus'])
        ltype = row['type'] if row['type'] else 'other'
        load_mw = round(float(row['load_mw']), 1)
        if bus_id not in load_by_bus:
            load_by_bus[bus_id] = {}
        load_by_bus[bus_id][ltype] = load_by_bus[bus_id].get(ltype, 0) + load_mw

    # ── Build storage breakdown per bus ────────────────────────────────
    storage_by_bus = {}
    for _, row in storage.iterrows():
        bus_id = int(row['bus'])
        carrier = row['carrier']
        cap = round(float(row['capacity_mw']), 1)
        if bus_id not in storage_by_bus:
            storage_by_bus[bus_id] = {}
        storage_by_bus[bus_id][carrier] = storage_by_bus[bus_id].get(carrier, 0) + cap

    # ── Bus data ──────────────────────────────────────────────────────
    log.info("Preparing bus data...")
    bus_list = []
    bus_lookup = {}
    for bus_id, row in n.buses.iterrows():
        bid = int(bus_id)
        gen_info = gen_by_bus.get(bid, {})
        load_info = load_by_bus.get(bid, {})
        stor_info = storage_by_bus.get(bid, {})
        total_gen = sum(gen_info.values())
        total_load = sum(load_info.values())
        total_stor = sum(stor_info.values())

        entry = {
            'id': bid,
            'lat': round(row['y'], 5),
            'lon': round(row['x'], 5),
            'v': int(row['v_nom']),
            'tg': round(total_gen, 1),
            'tl': round(total_load, 1),
            'ts': round(total_stor, 1),
        }
        if gen_info:
            entry['g'] = gen_info
        if load_info:
            entry['ld'] = load_info
        if stor_info:
            entry['st'] = stor_info

        bus_list.append(entry)
        bus_lookup[bus_id] = entry

    # ── Line data with hourly flows ───────────────────────────────────
    log.info("Preparing line data...")
    lines_json = []
    for line_id, row in n.lines.iterrows():
        b0 = row['bus0']
        b1 = row['bus1']
        if b0 not in bus_lookup or b1 not in bus_lookup:
            continue
        s_nom = row['s_nom']
        v_nom = int(row['v_nom']) if not np.isnan(row['v_nom']) else 110

        flows = n.lines_t.p0[line_id].abs().values
        if s_nom > 0:
            loadings = (flows / s_nom * 100).tolist()
        else:
            loadings = [0.0] * len(n.snapshots)

        lines_json.append({
            'id': str(line_id),
            'b0': [bus_lookup[b0]['lat'], bus_lookup[b0]['lon']],
            'b1': [bus_lookup[b1]['lat'], bus_lookup[b1]['lon']],
            'v': v_nom,
            'sn': round(s_nom, 1),
            'ln': round(row['length'], 1) if not np.isnan(row['length']) else 0,
            'f': [round(f, 1) for f in flows.tolist()],
            'l': [round(l, 1) for l in loadings],
        })

    # ── Transformer data ──────────────────────────────────────────────
    log.info("Preparing transformer data...")
    trafo_json = []
    for t_id, row in n.transformers.iterrows():
        b0 = row['bus0']
        b1 = row['bus1']
        if b0 not in bus_lookup or b1 not in bus_lookup:
            continue
        s_nom = row['s_nom']
        flows = n.transformers_t.p0[t_id].abs().values if t_id in n.transformers_t.p0.columns else [0.0] * len(n.snapshots)
        if s_nom > 0:
            loadings = (np.array(flows) / s_nom * 100).tolist()
        else:
            loadings = [0.0] * len(n.snapshots)
        trafo_json.append({
            'b0': [bus_lookup[b0]['lat'], bus_lookup[b0]['lon']],
            'b1': [bus_lookup[b1]['lat'], bus_lookup[b1]['lon']],
            'sn': round(s_nom, 1),
            'f': [round(f, 1) for f in flows.tolist()] if hasattr(flows, 'tolist') else [round(f, 1) for f in flows],
            'l': [round(l, 1) for l in loadings],
        })

    # ── Hourly dispatch by carrier ────────────────────────────────────
    log.info("Preparing dispatch data...")
    gen_p = n.generators_t.p
    carriers = n.generators.carrier
    carrier_order = ['solar', 'onwind', 'offwind', 'run_of_river', 'biogas',
                     'biomass', 'waste', 'lignite', 'coal', 'gas', 'oil',
                     'other', 'reservoir', 'hydrogen']

    dispatch_by_hour = {}
    for h in range(len(n.snapshots)):
        snap = n.snapshots[h]
        hourly = {}
        for carrier in carrier_order:
            gen_ids = n.generators[carriers == carrier].index
            if len(gen_ids) > 0:
                hourly[carrier] = round(float(gen_p.loc[snap, gen_ids].sum()), 0)
            else:
                hourly[carrier] = 0
        dispatch_by_hour[h] = hourly

    # ── Hourly load ───────────────────────────────────────────────────
    load_by_hour = {}
    for h in range(len(n.snapshots)):
        snap = n.snapshots[h]
        load_by_hour[h] = round(float(n.loads_t.p_set.loc[snap].sum()), 0)

    # ── Hourly overload stats ─────────────────────────────────────────
    overload_by_hour = {}
    for h in range(len(n.snapshots)):
        snap = n.snapshots[h]
        flow = n.lines_t.p0.loc[snap].abs()
        loading_pct = flow / n.lines.s_nom * 100
        loading_pct = loading_pct.replace([np.inf, -np.inf], 0).fillna(0)
        overload_by_hour[h] = {
            'gt100': int((loading_pct > 100).sum()),
            'gt50': int((loading_pct > 50).sum()),
            'mean': round(float(loading_pct.mean()), 1),
            'max': round(float(loading_pct.max()), 1),
        }

    # ── Installed capacity by carrier ─────────────────────────────────
    installed = {}
    for carrier in carrier_order:
        gen_ids = n.generators[carriers == carrier].index
        installed[carrier] = round(float(n.generators.loc[gen_ids, 'p_nom'].sum()), 0)

    peak_load = round(float(n.loads.p_set.sum()), 0)

    # ── Carrier display config ────────────────────────────────────────
    carrier_colors = {
        'solar': '#FFD700', 'onwind': '#4CAF50', 'offwind': '#00BCD4',
        'run_of_river': '#2196F3', 'biogas': '#8BC34A', 'biomass': '#689F38',
        'waste': '#795548', 'lignite': '#5D4037', 'coal': '#424242',
        'gas': '#FF9800', 'oil': '#F44336', 'other': '#9E9E9E',
        'reservoir': '#1565C0', 'hydrogen': '#E040FB',
        'battery': '#9370DB', 'pumped_hydro': '#4682B4',
    }
    carrier_labels = {
        'solar': 'Solar', 'onwind': 'Wind Onshore', 'offwind': 'Wind Offshore',
        'run_of_river': 'Run of River', 'biogas': 'Biogas', 'biomass': 'Biomass',
        'waste': 'Waste', 'lignite': 'Lignite', 'coal': 'Coal',
        'gas': 'Natural Gas', 'oil': 'Oil', 'other': 'Other',
        'reservoir': 'Hydro Reservoir', 'hydrogen': 'Hydrogen',
        'battery': 'Battery', 'pumped_hydro': 'Pumped Hydro',
    }

    n_hours = len(n.snapshots)

    # ── Build HTML ────────────────────────────────────────────────────
    log.info(f"Building HTML ({len(lines_json)} lines, {len(bus_list)} buses, {len(trafo_json)} transformers)...")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>eGon2025 Combined Grid &amp; Power Flow Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
/* ── CSS Variables for theming ─────────────────────────────────────── */
:root {{
  --bg-primary: #16213e;
  --bg-secondary: #1a1a3e;
  --bg-panel: #1a1a3e;
  --border-panel: #2a2a5e;
  --text-primary: #e0e0e0;
  --text-secondary: #8899aa;
  --text-heading: #64b5f6;
  --text-white: #fff;
  --bg-bar: #2a2a5e;
  --bg-body: #1a1a2e;
  --tile-url: 'https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png';
  --tooltip-bg: rgba(22,33,62,0.95);
  --tooltip-text: #e0e0e0;
  --tooltip-border: #2a2a5e;
}}

[data-theme="light"] {{
  --bg-primary: #f5f7fa;
  --bg-secondary: #ffffff;
  --bg-panel: #ffffff;
  --border-panel: #dde1e8;
  --text-primary: #333;
  --text-secondary: #666;
  --text-heading: #1565c0;
  --text-white: #222;
  --bg-bar: #e0e4ea;
  --bg-body: #ebeef3;
  --tooltip-bg: rgba(255,255,255,0.96);
  --tooltip-text: #333;
  --tooltip-border: #ccc;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  background: var(--bg-body);
  transition: background 0.3s;
}}
#map {{
  position: absolute; top: 0; right: 0; bottom: 0; left: 380px; z-index: 1;
}}

/* ── Sidebar ───────────────────────────────────────────────────────── */
#sidebar {{
  position: absolute; top: 0; left: 0; bottom: 0; width: 380px;
  background: var(--bg-primary); color: var(--text-primary);
  overflow-y: auto; z-index: 2; padding: 16px; font-size: 13px;
  transition: background 0.3s, color 0.3s;
  border-right: 1px solid var(--border-panel);
}}
#sidebar h1 {{ font-size: 17px; color: var(--text-white); margin-bottom: 2px; }}
#sidebar .subtitle {{ font-size: 11px; color: var(--text-secondary); margin-bottom: 14px; }}

.panel {{
  background: var(--bg-panel); border: 1px solid var(--border-panel);
  border-radius: 8px; padding: 12px; margin-bottom: 12px;
  transition: background 0.3s, border-color 0.3s;
}}
.panel h2 {{
  font-size: 12px; color: var(--text-heading); margin-bottom: 8px;
  text-transform: uppercase; letter-spacing: 1px; font-weight: 700;
}}

/* ── Theme toggle ──────────────────────────────────────────────────── */
.theme-toggle {{
  display: flex; gap: 6px; align-items: center;
}}
.theme-btn {{
  flex: 1; padding: 7px 0; text-align: center; border-radius: 6px;
  cursor: pointer; font-size: 12px; font-weight: 600;
  border: 2px solid var(--border-panel);
  background: transparent; color: var(--text-primary);
  transition: all 0.2s;
}}
.theme-btn.active {{
  background: var(--text-heading); color: #fff; border-color: var(--text-heading);
}}

/* ── View mode toggle ──────────────────────────────────────────────── */
.view-toggle {{
  display: flex; gap: 6px;
}}
.view-btn {{
  flex: 1; padding: 8px 0; text-align: center; border-radius: 6px;
  cursor: pointer; font-size: 12px; font-weight: 700;
  border: 2px solid var(--border-panel);
  background: transparent; color: var(--text-primary);
  transition: all 0.2s;
}}
.view-btn.active {{
  border-color: var(--text-heading);
  background: var(--text-heading);
  color: #fff;
}}
.view-btn:hover:not(.active) {{ border-color: var(--text-heading); }}

/* ── Voltage filters ───────────────────────────────────────────────── */
.vfilter {{ display: flex; gap: 8px; }}
.vfilter label {{
  flex: 1; text-align: center; padding: 7px 0; border-radius: 6px;
  cursor: pointer; font-weight: 700; font-size: 12px; transition: all 0.15s;
  user-select: none;
}}
.vfilter input {{ display: none; }}
.vfilter .v110 {{ background: #2e7d32; color: #fff; opacity: 0.35; }}
.vfilter .v220 {{ background: #f57f17; color: #fff; opacity: 0.35; }}
.vfilter .v380 {{ background: #c62828; color: #fff; opacity: 0.35; }}
.vfilter input:checked + .v110,
.vfilter input:checked + .v220,
.vfilter input:checked + .v380 {{ opacity: 1; box-shadow: 0 0 10px rgba(255,255,255,0.15); }}

.line-counts {{ display: flex; gap: 8px; margin-top: 6px; }}
.line-count-chip {{
  flex: 1; text-align: center; padding: 4px; border-radius: 6px;
  background: var(--bg-bar); font-size: 11px; transition: background 0.3s;
}}
.line-count-chip .num {{ font-size: 15px; font-weight: 700; color: var(--text-white); }}

/* ── Hour slider ───────────────────────────────────────────────────── */
.hour-display {{
  text-align: center; font-size: 30px; font-weight: 700;
  color: var(--text-white); margin: 2px 0;
}}
.hour-label {{
  text-align: center; color: var(--text-secondary); font-size: 11px; margin-bottom: 8px;
}}
input[type=range] {{
  width: 100%; height: 6px; -webkit-appearance: none;
  background: var(--bg-bar); border-radius: 3px; outline: none;
}}
input[type=range]::-webkit-slider-thumb {{
  -webkit-appearance: none; width: 20px; height: 20px;
  border-radius: 50%; background: var(--text-heading); cursor: pointer;
}}
.hour-ticks {{
  display: flex; justify-content: space-between;
  color: var(--text-secondary); font-size: 10px; margin-top: 2px;
}}
.play-btn {{
  display: block; margin: 8px auto 0; padding: 5px 24px;
  background: var(--text-heading); color: #fff; border: none;
  border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 12px;
}}
.play-btn:hover {{ opacity: 0.85; }}

/* ── Dispatch table ────────────────────────────────────────────────── */
.dispatch-row {{
  display: flex; align-items: center; padding: 3px 0;
  border-bottom: 1px solid var(--border-panel);
}}
.dispatch-row:last-child {{ border-bottom: none; }}
.carrier-dot {{
  width: 10px; height: 10px; border-radius: 50%;
  margin-right: 8px; flex-shrink: 0;
}}
.carrier-name {{ flex: 1; font-size: 12px; }}
.carrier-val {{
  font-weight: 600; color: var(--text-white); text-align: right; min-width: 65px; font-size: 12px;
}}
.carrier-bar-bg {{
  width: 55px; height: 5px; background: var(--bg-bar);
  border-radius: 3px; margin-left: 6px; flex-shrink: 0; overflow: hidden;
}}
.carrier-bar {{ height: 100%; border-radius: 3px; transition: width 0.3s; }}

/* ── Stats ─────────────────────────────────────────────────────────── */
.stat-row {{ display: flex; justify-content: space-between; padding: 3px 0; }}
.stat-label {{ color: var(--text-secondary); font-size: 12px; }}
.stat-val {{ font-weight: 700; color: var(--text-white); font-size: 12px; }}
.stat-val.warn {{ color: #ff9800; }}
.stat-val.danger {{ color: #f44336; }}
.stat-val.good {{ color: #4caf50; }}

/* ── Load bar ──────────────────────────────────────────────────────── */
.load-bar-bg {{
  height: 18px; background: var(--bg-bar); border-radius: 4px;
  margin-top: 6px; overflow: hidden; position: relative;
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

/* ── Legend bar ─────────────────────────────────────────────────────── */
.legend-bar {{
  height: 14px; border-radius: 4px; margin: 6px 0 2px;
  background: linear-gradient(90deg, #4caf50 0%, #8bc34a 20%, #cddc39 35%, #ffeb3b 50%, #ff9800 70%, #f44336 100%);
}}
.legend-labels {{
  display: flex; justify-content: space-between;
  font-size: 10px; color: var(--text-secondary);
}}

/* ── Powerflow panel visibility ────────────────────────────────────── */
.pf-only {{ display: none; }}
body.mode-powerflow .pf-only {{ display: block; }}
body.mode-powerflow .grid-only {{ display: none; }}

/* ── Custom bus tooltip ────────────────────────────────────────────── */
.bus-tooltip {{
  background: var(--tooltip-bg) !important;
  color: var(--tooltip-text) !important;
  border: 1px solid var(--tooltip-border) !important;
  border-radius: 8px !important;
  padding: 10px 14px !important;
  font-size: 12px !important;
  line-height: 1.5 !important;
  box-shadow: 0 4px 16px rgba(0,0,0,0.3) !important;
  max-width: 280px !important;
  pointer-events: none;
}}
.bus-tooltip .tip-header {{
  font-weight: 700; font-size: 13px; margin-bottom: 6px;
  padding-bottom: 4px; border-bottom: 1px solid var(--border-panel);
}}
.bus-tooltip .tip-section {{ margin-top: 6px; }}
.bus-tooltip .tip-section-title {{
  font-weight: 700; font-size: 11px; color: var(--text-heading);
  text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 3px;
}}
.bus-tooltip .tip-row {{
  display: flex; justify-content: space-between; align-items: center; padding: 1px 0;
}}
.bus-tooltip .tip-carrier {{
  display: flex; align-items: center; gap: 5px;
}}
.bus-tooltip .tip-dot {{
  width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0;
}}
.bus-tooltip .tip-val {{ font-weight: 600; }}

/* ── Leaflet tooltip override ──────────────────────────────────────── */
.leaflet-tooltip.bus-tooltip::before {{ display: none; }}

/* ── Grid mode legend on map ───────────────────────────────────────── */
.map-legend {{
  background: var(--tooltip-bg);
  color: var(--tooltip-text);
  border: 1px solid var(--tooltip-border);
  border-radius: 8px;
  padding: 12px 16px;
  font-size: 12px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.2);
  line-height: 1.8;
}}
.map-legend .title {{ font-weight: 700; margin-bottom: 4px; }}
.map-legend .item {{ display: flex; align-items: center; gap: 8px; }}
.map-legend .swatch {{
  display: inline-block; width: 24px; height: 4px; border-radius: 2px;
}}

/* ── Scrollbar ─────────────────────────────────────────────────────── */
#sidebar::-webkit-scrollbar {{ width: 6px; }}
#sidebar::-webkit-scrollbar-track {{ background: transparent; }}
#sidebar::-webkit-scrollbar-thumb {{
  background: var(--bg-bar); border-radius: 3px;
}}
</style>
</head>
<body class="mode-grid">

<div id="sidebar">
  <h1>eGon2025 Grid &amp; Power Flow</h1>
  <div class="subtitle">April 15, 2025 &mdash; {len(bus_list):,} buses &middot; {len(lines_json):,} lines &middot; {len(trafo_json)} transformers</div>

  <!-- Theme -->
  <div class="panel">
    <h2>Appearance</h2>
    <div class="theme-toggle">
      <div class="theme-btn active" id="btnDark" onclick="setTheme('dark')">Dark</div>
      <div class="theme-btn" id="btnLight" onclick="setTheme('light')">Light</div>
    </div>
  </div>

  <!-- View mode -->
  <div class="panel">
    <h2>View Mode</h2>
    <div class="view-toggle">
      <div class="view-btn active" id="btnGrid" onclick="setView('grid')">Grid Topology</div>
      <div class="view-btn" id="btnPF" onclick="setView('powerflow')">Power Flow</div>
    </div>
  </div>

  <!-- Voltage filter -->
  <div class="panel">
    <h2>Voltage Levels</h2>
    <div class="vfilter">
      <label><input type="checkbox" id="v110" checked onchange="updateAll()"><span class="v110">110 kV</span></label>
      <label><input type="checkbox" id="v220" checked onchange="updateAll()"><span class="v220">220 kV</span></label>
      <label><input type="checkbox" id="v380" checked onchange="updateAll()"><span class="v380">380 kV</span></label>
    </div>
    <div class="line-counts" id="lineCounts"></div>
  </div>

  <!-- Hour selector (power flow only) -->
  <div class="panel pf-only" id="panelHour">
    <h2>Time of Day</h2>
    <div class="hour-display"><span id="hourVal">12</span>:00</div>
    <div class="hour-label" id="hourDesc">Peak solar production</div>
    <input type="range" id="hourSlider" min="0" max="{n_hours - 1}" value="12" step="1">
    <div class="hour-ticks"><span>0</span><span>6</span><span>12</span><span>18</span><span>23</span></div>
    <button class="play-btn" id="playBtn" onclick="togglePlay()">&#9654; Play</button>
  </div>

  <!-- System load (power flow only) -->
  <div class="panel pf-only">
    <h2>System Load</h2>
    <div class="stat-row">
      <span class="stat-label">Total demand</span>
      <span class="stat-val" id="loadVal">--</span>
    </div>
    <div class="load-bar-bg">
      <div class="load-bar" id="loadBar" style="width:50%"></div>
      <div class="load-bar-text" id="loadBarText">--</div>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-secondary);margin-top:2px">
      <span>0 GW</span><span>{peak_load / 1000:.1f} GW peak</span>
    </div>
  </div>

  <!-- Generation dispatch (power flow only) -->
  <div class="panel pf-only">
    <h2>Generation Dispatch</h2>
    <div class="stat-row" style="margin-bottom:6px">
      <span class="stat-label">Total generation</span>
      <span class="stat-val" id="totalGen">--</span>
    </div>
    <div id="dispatchList"></div>
  </div>

  <!-- Line loading stats (power flow only) -->
  <div class="panel pf-only">
    <h2>Line Loading</h2>
    <div class="legend-bar"></div>
    <div class="legend-labels"><span>0%</span><span>25%</span><span>50%</span><span>75%</span><span>100%+</span></div>
    <div style="margin-top:8px">
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
    </div>
  </div>

  <!-- Grid mode stats -->
  <div class="panel grid-only">
    <h2>Network Statistics</h2>
    <div class="stat-row">
      <span class="stat-label">Total buses</span>
      <span class="stat-val" id="statBuses">--</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Total lines</span>
      <span class="stat-val" id="statLines">--</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Installed generation</span>
      <span class="stat-val" id="statGen">--</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Total load</span>
      <span class="stat-val" id="statLoad">--</span>
    </div>
  </div>
</div>

<div id="map"></div>

<script>
// ── Embedded data ─────────────────────────────────────────────────────
const BUSES = {json.dumps(bus_list, separators=(',', ':'))};
const LINES = {json.dumps(lines_json, separators=(',', ':'))};
const TRAFOS = {json.dumps(trafo_json, separators=(',', ':'))};
const DISPATCH = {json.dumps(dispatch_by_hour, separators=(',', ':'))};
const LOAD_H = {json.dumps(load_by_hour, separators=(',', ':'))};
const OVERLOAD = {json.dumps(overload_by_hour, separators=(',', ':'))};
const INSTALLED = {json.dumps(installed, separators=(',', ':'))};
const PEAK_LOAD = {peak_load};
const N_HOURS = {n_hours};

const CARRIER_COLORS = {json.dumps(carrier_colors, separators=(',', ':'))};
const CARRIER_LABELS = {json.dumps(carrier_labels, separators=(',', ':'))};
const CARRIER_ORDER = {json.dumps(carrier_order, separators=(',', ':'))};

const VOLTAGE_COLORS = {{ 110: '#2e7d32', 220: '#e65100', 380: '#c62828' }};
const VOLTAGE_WEIGHTS = {{ 110: 1.2, 220: 2.0, 380: 3.0 }};

const HOUR_DESCS = [
  "Night - low load", "Night - minimum demand", "Night - valley",
  "Night - minimum demand", "Night - demand rising", "Early morning",
  "Dawn - demand ramping up", "Morning - solar starting", "Morning - solar ramping",
  "Mid-morning - renewables rising", "Late morning - high RE",
  "Approaching noon peak", "Noon - peak solar production",
  "Early afternoon - high solar", "Afternoon - solar still strong",
  "Afternoon - solar declining", "Late afternoon",
  "Early evening - solar fading", "Evening - demand peak",
  "Evening - demand high, solar gone", "Night beginning - demand dropping",
  "Night - demand declining", "Night - demand declining", "Night - approaching minimum"
];

// ── State ─────────────────────────────────────────────────────────────
let currentTheme = 'dark';
let currentView = 'grid';   // 'grid' or 'powerflow'
let currentHour = 12;
let playing = false;
let playTimer = null;

// ── Map setup ─────────────────────────────────────────────────────────
const map = L.map('map', {{
  center: [51.2, 10.4],
  zoom: 6,
  preferCanvas: true,
  renderer: L.canvas({{ padding: 0.5 }})
}});

// Tile layers
const darkTile = L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; OSM &copy; CARTO', maxZoom: 18
}});
const lightTile = L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; OSM &copy; CARTO', maxZoom: 18
}});
darkTile.addTo(map);

// ── Line & bus layers ─────────────────────────────────────────────────
let lineLayers = {{ 110: [], 220: [], 380: [] }};
let trafoLayerArr = [];
let busMarkers = {{ 110: [], 220: [], 380: [] }};

// Build bus lookup for tooltip
const busMap = {{}};
BUSES.forEach(b => {{ busMap[b.id] = b; }});

// ── Loading color: 0% green → 50% yellow → 100% red, >100% red ──────
function loadingColor(pct) {{
  const p = Math.min(pct, 100) / 100;  // clamp to [0,1]
  let r, g, b;
  if (p <= 0.5) {{
    // green(76,175,80) → yellow(255,235,59)
    const t = p / 0.5;
    r = Math.round(76 + (255 - 76) * t);
    g = Math.round(175 + (235 - 175) * t);
    b = Math.round(80 + (59 - 80) * t);
  }} else {{
    // yellow(255,235,59) → red(244,67,54)
    const t = (p - 0.5) / 0.5;
    r = Math.round(255 + (244 - 255) * t);
    g = Math.round(235 + (67 - 235) * t);
    b = Math.round(59 + (54 - 59) * t);
  }}
  return `rgb(${{r}},${{g}},${{b}})`;
}}

function lineWeight(v) {{
  return VOLTAGE_WEIGHTS[v] || 1.5;
}}

// ── Create bus tooltip HTML ───────────────────────────────────────────
function busTooltipHTML(b) {{
  let html = `<div class="tip-header">Bus ${{b.id}} &mdash; ${{b.v}} kV</div>`;

  if (b.tg > 0 && b.g) {{
    html += `<div class="tip-section"><div class="tip-section-title">Generation (${{fmt(b.tg)}})</div>`;
    const entries = Object.entries(b.g).sort((a,b) => b[1] - a[1]);
    entries.forEach(([c, v]) => {{
      const col = CARRIER_COLORS[c] || '#888';
      const label = CARRIER_LABELS[c] || c;
      html += `<div class="tip-row"><span class="tip-carrier"><span class="tip-dot" style="background:${{col}}"></span>${{label}}</span><span class="tip-val">${{fmt(v)}}</span></div>`;
    }});
    html += '</div>';
  }}

  if (b.tl > 0 && b.ld) {{
    html += `<div class="tip-section"><div class="tip-section-title">Load (${{fmt(b.tl)}})</div>`;
    Object.entries(b.ld).sort((a,b) => b[1] - a[1]).forEach(([t, v]) => {{
      html += `<div class="tip-row"><span>${{t}}</span><span class="tip-val">${{fmt(v)}}</span></div>`;
    }});
    html += '</div>';
  }}

  if (b.ts > 0 && b.st) {{
    html += `<div class="tip-section"><div class="tip-section-title">Storage (${{fmt(b.ts)}})</div>`;
    Object.entries(b.st).sort((a,b) => b[1] - a[1]).forEach(([c, v]) => {{
      const col = CARRIER_COLORS[c] || '#888';
      const label = CARRIER_LABELS[c] || c;
      html += `<div class="tip-row"><span class="tip-carrier"><span class="tip-dot" style="background:${{col}}"></span>${{label}}</span><span class="tip-val">${{fmt(v)}}</span></div>`;
    }});
    html += '</div>';
  }}

  if (b.tg === 0 && b.tl === 0 && b.ts === 0) {{
    html += `<div style="color:var(--text-secondary);font-style:italic;margin-top:4px">No generation, load, or storage</div>`;
  }}

  return html;
}}

function fmt(mw) {{
  if (mw >= 1000) return (mw / 1000).toFixed(1) + ' GW';
  return mw.toFixed(1) + ' MW';
}}

// ── Create lines ──────────────────────────────────────────────────────
LINES.forEach(line => {{
  const pl = L.polyline([line.b0, line.b1], {{
    color: VOLTAGE_COLORS[line.v] || '#888',
    weight: lineWeight(line.v),
    opacity: 0.7,
  }});
  pl._ld = line;
  pl.bindTooltip('', {{ sticky: true }});
  lineLayers[line.v].push(pl);
}});

// Create transformers
TRAFOS.forEach(t => {{
  const pl = L.polyline([t.b0, t.b1], {{
    color: '#b0bec5', weight: 2, opacity: 0.4, dashArray: '5 5',
  }});
  pl._td = t;
  pl.bindTooltip('', {{ sticky: true }});
  trafoLayerArr.push(pl);
}});

// ── Create bus markers ────────────────────────────────────────────────
BUSES.forEach(b => {{
  const hasData = b.tg > 0 || b.tl > 0 || b.ts > 0;
  const radius = hasData ? Math.max(3, Math.min(8, 2 + Math.sqrt((b.tg + b.tl) / 100))) : 2;
  const marker = L.circleMarker([b.lat, b.lon], {{
    radius: radius,
    fillColor: VOLTAGE_COLORS[b.v] || '#888',
    color: 'rgba(0,0,0,0.3)',
    weight: 1,
    opacity: 1,
    fillOpacity: hasData ? 0.85 : 0.4,
  }});
  if (hasData) {{
    marker.bindTooltip(busTooltipHTML(b), {{
      className: 'bus-tooltip',
      direction: 'top',
      offset: [0, -8],
    }});
  }} else {{
    marker.bindTooltip(`Bus ${{b.id}} (${{b.v}} kV)`, {{ direction: 'top', offset: [0, -6] }});
  }}
  marker._bd = b;
  busMarkers[b.v].push(marker);
}});

// Layer groups
let lineGroups = {{
  110: L.layerGroup(lineLayers[110]),
  220: L.layerGroup(lineLayers[220]),
  380: L.layerGroup(lineLayers[380]),
}};
let trafoGroup = L.layerGroup(trafoLayerArr);
let busGroups = {{
  110: L.layerGroup(busMarkers[110]),
  220: L.layerGroup(busMarkers[220]),
  380: L.layerGroup(busMarkers[380]),
}};

// Add to map
[380, 220, 110].forEach(v => {{ lineGroups[v].addTo(map); }});
trafoGroup.addTo(map);
[110, 220, 380].forEach(v => {{ busGroups[v].addTo(map); }});

// ── Grid-mode map legend control ──────────────────────────────────────
const gridLegend = L.control({{ position: 'bottomright' }});
gridLegend.onAdd = function() {{
  const div = L.DomUtil.create('div', 'map-legend');
  div.innerHTML = `
    <div class="title">Voltage Levels</div>
    <div class="item"><span class="swatch" style="background:#c62828"></span>380 kV</div>
    <div class="item"><span class="swatch" style="background:#e65100"></span>220 kV</div>
    <div class="item"><span class="swatch" style="background:#2e7d32"></span>110 kV</div>
    <div class="item" style="margin-top:6px"><span class="swatch" style="background:#b0bec5;border-style:dashed"></span>Transformers</div>
  `;
  return div;
}};

const pfLegend = L.control({{ position: 'bottomright' }});
pfLegend.onAdd = function() {{
  const div = L.DomUtil.create('div', 'map-legend');
  div.innerHTML = `
    <div class="title">Line Loading</div>
    <div style="height:12px;border-radius:3px;margin:4px 0;background:linear-gradient(90deg,#4caf50,#8bc34a,#cddc39,#ffeb3b,#ff9800,#f44336);"></div>
    <div style="display:flex;justify-content:space-between;font-size:10px;"><span>0%</span><span>50%</span><span>100%+</span></div>
  `;
  return div;
}};
gridLegend.addTo(map);

// ── Theme switching ───────────────────────────────────────────────────
function setTheme(theme) {{
  currentTheme = theme;
  document.documentElement.setAttribute('data-theme', theme);
  document.getElementById('btnDark').classList.toggle('active', theme === 'dark');
  document.getElementById('btnLight').classList.toggle('active', theme === 'light');

  // Swap tile layer
  if (theme === 'dark') {{
    if (map.hasLayer(lightTile)) map.removeLayer(lightTile);
    if (!map.hasLayer(darkTile)) darkTile.addTo(map);
  }} else {{
    if (map.hasLayer(darkTile)) map.removeLayer(darkTile);
    if (!map.hasLayer(lightTile)) lightTile.addTo(map);
  }}
}}

// ── View mode switching ───────────────────────────────────────────────
function setView(mode) {{
  currentView = mode;
  document.body.className = 'mode-' + mode;
  document.getElementById('btnGrid').classList.toggle('active', mode === 'grid');
  document.getElementById('btnPF').classList.toggle('active', mode === 'powerflow');

  // Switch legend
  if (mode === 'grid') {{
    map.removeControl(pfLegend);
    gridLegend.addTo(map);
  }} else {{
    map.removeControl(gridLegend);
    pfLegend.addTo(map);
  }}

  updateAll();
}}

// ── Update everything ─────────────────────────────────────────────────
function updateAll() {{
  const h = currentHour;
  const show = {{
    110: document.getElementById('v110').checked,
    220: document.getElementById('v220').checked,
    380: document.getElementById('v380').checked,
  }};

  // Toggle visibility
  [110, 220, 380].forEach(v => {{
    if (show[v]) {{
      if (!map.hasLayer(lineGroups[v])) lineGroups[v].addTo(map);
      if (!map.hasLayer(busGroups[v])) busGroups[v].addTo(map);
    }} else {{
      map.removeLayer(lineGroups[v]);
      map.removeLayer(busGroups[v]);
    }}
  }});

  // Update line styles
  [110, 220, 380].forEach(v => {{
    lineLayers[v].forEach(pl => {{
      const d = pl._ld;
      if (currentView === 'powerflow') {{
        const loading = d.l[h];
        const flow = d.f[h];
        pl.setStyle({{ color: loadingColor(loading), opacity: 0.8, weight: lineWeight(d.v) }});
        pl.setTooltipContent(
          `<b>${{d.v}} kV</b> (Line ${{d.id}})<br>` +
          `Flow: <b>${{flow.toFixed(0)}} MW</b> / ${{d.sn}} MVA<br>` +
          `Loading: <b>${{loading.toFixed(1)}}%</b>`
        );
      }} else {{
        pl.setStyle({{ color: VOLTAGE_COLORS[d.v], opacity: 0.7, weight: lineWeight(d.v) }});
        pl.setTooltipContent(
          `<b>${{d.v}} kV</b> (Line ${{d.id}})<br>` +
          `Capacity: ${{d.sn}} MVA<br>` +
          `Length: ${{d.ln}} km`
        );
      }}
    }});
  }});

  // Update transformer styles
  trafoLayerArr.forEach(pl => {{
    const t = pl._td;
    if (currentView === 'powerflow') {{
      const loading = t.l[h];
      const flow = t.f[h];
      pl.setStyle({{ color: loadingColor(loading), opacity: 0.7, dashArray: '5 5' }});
      pl.setTooltipContent(
        `<b>Transformer</b><br>` +
        `Flow: <b>${{flow.toFixed(0)}} MW</b> / ${{t.sn}} MVA<br>` +
        `Loading: <b>${{loading.toFixed(1)}}%</b>`
      );
    }} else {{
      pl.setStyle({{ color: '#b0bec5', opacity: 0.4, dashArray: '5 5' }});
      pl.setTooltipContent(`Transformer: ${{t.sn}} MVA`);
    }}
  }});

  // Update hour display
  document.getElementById('hourVal').textContent = String(h).padStart(2, '0');
  document.getElementById('hourDesc').textContent = HOUR_DESCS[h] || '';

  // Update powerflow stats
  if (currentView === 'powerflow') {{
    const load = LOAD_H[h];
    document.getElementById('loadVal').textContent = (load / 1000).toFixed(1) + ' GW';
    const loadPct = load / PEAK_LOAD * 100;
    document.getElementById('loadBar').style.width = Math.min(loadPct, 100) + '%';
    document.getElementById('loadBarText').textContent = (load / 1000).toFixed(1) + ' GW (' + loadPct.toFixed(0) + '%)';

    // Dispatch
    const disp = DISPATCH[h];
    let total = 0, maxDisp = 0;
    CARRIER_ORDER.forEach(c => {{ total += disp[c] || 0; maxDisp = Math.max(maxDisp, disp[c] || 0); }});
    document.getElementById('totalGen').textContent = (total / 1000).toFixed(1) + ' GW';

    let dispHTML = '';
    CARRIER_ORDER.forEach(c => {{
      const val = disp[c] || 0;
      if (val < 1 && INSTALLED[c] < 100) return;
      const pct = maxDisp > 0 ? (val / maxDisp * 100) : 0;
      dispHTML += `<div class="dispatch-row">
        <div class="carrier-dot" style="background:${{CARRIER_COLORS[c]}}"></div>
        <div class="carrier-name">${{CARRIER_LABELS[c] || c}}</div>
        <div class="carrier-val">${{val >= 1000 ? (val/1000).toFixed(1) + ' GW' : val.toFixed(0) + ' MW'}}</div>
        <div class="carrier-bar-bg"><div class="carrier-bar" style="width:${{pct}}%;background:${{CARRIER_COLORS[c]}}"></div></div>
      </div>`;
    }});
    document.getElementById('dispatchList').innerHTML = dispHTML;

    // Overload stats
    const ol = OVERLOAD[h];
    document.getElementById('meanLoading').textContent = ol.mean + '%';
    document.getElementById('maxLoading').textContent = ol.max + '%';
    document.getElementById('maxLoading').className = 'stat-val' + (ol.max > 100 ? ' danger' : ' good');
    document.getElementById('gt50').textContent = ol.gt50;
    document.getElementById('gt50').className = 'stat-val' + (ol.gt50 > 500 ? ' warn' : '');
    document.getElementById('gt100').textContent = ol.gt100;
  }}

  // Update grid stats
  let totalBuses = 0, totalLines = 0, totalGen = 0, totalLoad = 0;
  BUSES.forEach(b => {{
    if (show[b.v]) {{ totalBuses++; totalGen += b.tg; totalLoad += b.tl; }}
  }});
  LINES.forEach(l => {{ if (show[l.v]) totalLines++; }});
  document.getElementById('statBuses').textContent = totalBuses.toLocaleString();
  document.getElementById('statLines').textContent = totalLines.toLocaleString();
  document.getElementById('statGen').textContent = (totalGen / 1000).toFixed(1) + ' GW';
  document.getElementById('statLoad').textContent = (totalLoad / 1000).toFixed(1) + ' GW';

  // Update line counts
  let lcHTML = '';
  [110, 220, 380].forEach(v => {{
    lcHTML += `<div class="line-count-chip" style="opacity:${{show[v] ? 1 : 0.3}}">
      <div class="num">${{lineLayers[v].length.toLocaleString()}}</div>
      <div>${{v}} kV</div>
    </div>`;
  }});
  document.getElementById('lineCounts').innerHTML = lcHTML;
}}

// ── Hour slider ───────────────────────────────────────────────────────
const slider = document.getElementById('hourSlider');
slider.addEventListener('input', function() {{
  currentHour = parseInt(this.value);
  updateAll();
}});

function togglePlay() {{
  playing = !playing;
  const btn = document.getElementById('playBtn');
  if (playing) {{
    btn.innerHTML = '&#9724; Pause';
    playTimer = setInterval(() => {{
      currentHour = (currentHour + 1) % N_HOURS;
      slider.value = currentHour;
      updateAll();
    }}, 800);
  }} else {{
    btn.innerHTML = '&#9654; Play';
    clearInterval(playTimer);
  }}
}}

// ── Initial render ────────────────────────────────────────────────────
updateAll();
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
