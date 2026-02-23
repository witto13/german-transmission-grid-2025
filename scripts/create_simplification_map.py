#!/usr/bin/env python3
"""
Generate interactive HTML comparison map for grid simplification.

Supports 3 versions:
  - eGon2025    (original)
  - eGon2025v2  (substation-simplified)
  - eGon2025v3  (degree-2 eliminated)

Plus diff views:
  - Diff orig→v2: substation merges
  - Diff v2→v3:   waypoint eliminations + merged lines

Embeds all data as JSON directly into a single self-contained HTML file.

Usage:
    python scripts/create_simplification_map.py
    python scripts/create_simplification_map.py --v1 eGon2025 --v2 eGon2025v2 --v3 eGon2025v3
"""

import argparse
import json
import os

import pandas as pd
from sqlalchemy import create_engine, text

DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'


def load_scenario(engine, scn_name):
    """Load buses, lines, transformers, links for a scenario."""
    buses = pd.read_sql(text("""
        SELECT bus_id, v_nom, x, y, country
        FROM grid.egon_etrago_bus WHERE scn_name = :scn
    """), engine, params={'scn': scn_name})

    lines = pd.read_sql(text("""
        SELECT line_id, bus0, bus1, v_nom, s_nom, length, x, r, cables
        FROM grid.egon_etrago_line WHERE scn_name = :scn
    """), engine, params={'scn': scn_name})

    trafos = pd.read_sql(text("""
        SELECT trafo_id, bus0, bus1, s_nom, x, r
        FROM grid.egon_etrago_transformer WHERE scn_name = :scn
    """), engine, params={'scn': scn_name})

    links = pd.read_sql(text("""
        SELECT link_id, bus0, bus1, p_nom, carrier
        FROM grid.egon_etrago_link WHERE scn_name = :scn
    """), engine, params={'scn': scn_name})

    return buses, lines, trafos, links


def load_mapping(path, default_cols=('old_bus', 'new_bus', 'reason')):
    """Load a node mapping CSV if it exists."""
    if os.path.exists(path):
        return pd.read_csv(path)
    return pd.DataFrame(columns=list(default_cols))


def prepare_json(buses, lines, trafos, links):
    """Convert DataFrames to JSON-serialisable lists of dicts."""
    bus_records = []
    for _, r in buses.iterrows():
        bus_records.append({
            'id': int(r['bus_id']),
            'v': int(r['v_nom']),
            'x': round(float(r['x']), 6),
            'y': round(float(r['y']), 6),
            'c': str(r['country']) if pd.notna(r['country']) else 'DE',
        })

    bus_coords = {int(r['bus_id']): (float(r['y']), float(r['x']))
                  for _, r in buses.iterrows()}

    line_records = []
    for _, r in lines.iterrows():
        b0, b1 = int(r['bus0']), int(r['bus1'])
        c0, c1 = bus_coords.get(b0), bus_coords.get(b1)
        if c0 is None or c1 is None:
            continue
        line_records.append({
            'id': int(r['line_id']),
            'b0': b0, 'b1': b1,
            'v': int(r['v_nom']) if pd.notna(r['v_nom']) else 0,
            's': round(float(r['s_nom']), 1) if pd.notna(r['s_nom']) else 0,
            'len': round(float(r['length']), 2) if pd.notna(r['length']) else 0,
            'lat0': c0[0], 'lon0': c0[1],
            'lat1': c1[0], 'lon1': c1[1],
        })

    trafo_records = []
    for _, r in trafos.iterrows():
        b0, b1 = int(r['bus0']), int(r['bus1'])
        c0, c1 = bus_coords.get(b0), bus_coords.get(b1)
        if c0 is None or c1 is None:
            continue
        trafo_records.append({
            'id': int(r['trafo_id']),
            'b0': b0, 'b1': b1,
            's': round(float(r['s_nom']), 1) if pd.notna(r['s_nom']) else 0,
            'lat0': c0[0], 'lon0': c0[1],
            'lat1': c1[0], 'lon1': c1[1],
        })

    link_records = []
    for _, r in links.iterrows():
        b0, b1 = int(r['bus0']), int(r['bus1'])
        c0, c1 = bus_coords.get(b0), bus_coords.get(b1)
        if c0 is None or c1 is None:
            continue
        link_records.append({
            'id': int(r['link_id']),
            'b0': b0, 'b1': b1,
            'p': round(float(r['p_nom']), 1) if pd.notna(r['p_nom']) else 0,
            'carrier': str(r['carrier']) if pd.notna(r['carrier']) else '',
            'lat0': c0[0], 'lon0': c0[1],
            'lat1': c1[0], 'lon1': c1[1],
        })

    return bus_records, line_records, trafo_records, link_records


def compute_stats(buses, lines, trafos, links):
    """Compute summary statistics."""
    bv = buses['v_nom'].value_counts().to_dict()
    lv = lines['v_nom'].value_counts().to_dict() if len(lines) > 0 else {}
    return {
        'totalBuses': int(len(buses)),
        'totalLines': int(len(lines)),
        'totalTrafos': int(len(trafos)),
        'totalLinks': int(len(links)),
        'busByV': {int(k): int(v) for k, v in bv.items()},
        'lineByV': {int(k): int(v) for k, v in lv.items()},
    }


def generate_html(versions, stats, mappings, names):
    """Generate self-contained HTML with 3 versions and 2 diff views.

    Args:
        versions: dict with keys 'orig', 'v2', 'v3' -> (buses, lines, trafos, links) JSON lists
        stats: dict with keys 'orig', 'v2', 'v3' -> stats dict
        mappings: dict with 'v1_v2' and 'v2_v3' -> list of {old, new} dicts
        names: dict with keys 'orig', 'v2', 'v3' -> scenario name strings
    """

    # Compute rep buses for each mapping
    rep_v1v2 = sorted(set(m['new'] for m in mappings['v1_v2']))
    rep_v2v3_eliminated = sorted(set(m['old'] for m in mappings['v2_v3']))

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Grid Simplification: 3-Version Comparison</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
#map {{ position: absolute; top: 0; left: 0; right: 0; bottom: 0; }}

.control-panel {{
    position: absolute;
    top: 10px;
    right: 10px;
    background: rgba(255,255,255,0.97);
    padding: 16px;
    border-radius: 8px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.25);
    z-index: 1000;
    max-width: 380px;
    max-height: 92vh;
    overflow-y: auto;
    font-size: 13px;
    line-height: 1.45;
}}
.control-panel h2 {{
    font-size: 15px;
    margin-bottom: 10px;
    color: #2c3e50;
    border-bottom: 2px solid #3498db;
    padding-bottom: 6px;
}}

/* Version buttons */
.version-row {{
    display: flex;
    gap: 4px;
    margin-bottom: 6px;
    flex-wrap: wrap;
}}
.version-btn {{
    flex: 1;
    min-width: 60px;
    padding: 7px 4px;
    border: 2px solid #bbb;
    background: #fff;
    border-radius: 5px;
    cursor: pointer;
    font-size: 11px;
    font-weight: 600;
    text-align: center;
    transition: all 0.15s;
}}
.version-btn:hover {{ background: #f0f0f0; }}
.version-btn.active {{ color: #fff; }}
.version-btn.orig {{ border-color: #e74c3c; }}
.version-btn.orig.active {{ background: #e74c3c; }}
.version-btn.v2 {{ border-color: #27ae60; }}
.version-btn.v2.active {{ background: #27ae60; }}
.version-btn.v3 {{ border-color: #2980b9; }}
.version-btn.v3.active {{ background: #2980b9; }}
.version-btn.diff12 {{ border-color: #8e44ad; }}
.version-btn.diff12.active {{ background: #8e44ad; }}
.version-btn.diff23 {{ border-color: #d35400; }}
.version-btn.diff23.active {{ background: #d35400; }}

/* Stats */
.stats {{ margin-bottom: 10px; padding: 8px; background: #f8f9fa; border-radius: 5px; }}
.stats-line {{ font-size: 12px; padding: 1px 0; }}
.stats-line strong {{ color: #2c3e50; }}

/* Section */
.section {{ margin-bottom: 10px; }}
.section-title {{
    font-weight: 700;
    font-size: 12px;
    color: #555;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    margin-bottom: 5px;
}}

/* Checkboxes */
.cb-row {{ display: flex; align-items: center; gap: 5px; padding: 2px 0; cursor: pointer; }}
.cb-row input {{ margin: 0; cursor: pointer; }}
.color-swatch {{
    display: inline-block;
    width: 22px;
    height: 3px;
    border-radius: 1px;
    vertical-align: middle;
}}
.color-dot {{
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    vertical-align: middle;
}}

/* Diff legend */
.diff-legend {{ margin-top: 8px; padding: 8px; background: #fdf2f8; border-radius: 5px; display: none; }}
.diff-legend .legend-item {{ display: flex; align-items: center; gap: 6px; padding: 2px 0; font-size: 12px; }}
.legend-swatch {{ width: 18px; height: 3px; border-radius: 1px; display: inline-block; }}

/* Delta badge */
.delta {{ font-size: 11px; font-weight: 600; margin-left: 4px; }}
.delta.neg {{ color: #e74c3c; }}
.delta.pos {{ color: #27ae60; }}
.delta.zero {{ color: #888; }}
</style>
</head>
<body>
<div id="map"></div>

<div class="control-panel">
    <h2>Grid Simplification Pipeline</h2>

    <div class="version-row">
        <button class="version-btn orig active" onclick="switchVersion('orig')">
            {names['orig']}<br><span style="font-weight:400;font-size:10px">Original</span>
        </button>
        <button class="version-btn v2" onclick="switchVersion('v2')">
            {names['v2']}<br><span style="font-weight:400;font-size:10px">Substations</span>
        </button>
        <button class="version-btn v3" onclick="switchVersion('v3')">
            {names['v3']}<br><span style="font-weight:400;font-size:10px">Degree-2</span>
        </button>
    </div>
    <div class="version-row">
        <button class="version-btn diff12" onclick="switchVersion('diff12')">
            Diff orig&rarr;v2<br><span style="font-weight:400;font-size:10px">Substations</span>
        </button>
        <button class="version-btn diff23" onclick="switchVersion('diff23')">
            Diff v2&rarr;v3<br><span style="font-weight:400;font-size:10px">Waypoints</span>
        </button>
    </div>

    <div class="stats">
        <div class="stats-line"><strong id="versionLabel">{names['orig']} (Original)</strong></div>
        <div class="stats-line">Buses: <strong id="busCount">-</strong> <span id="busDelta"></span></div>
        <div class="stats-line" id="busBreakdown" style="padding-left:10px;color:#666"></div>
        <div class="stats-line">Lines: <strong id="lineCount">-</strong> <span id="lineDelta"></span></div>
        <div class="stats-line" id="lineBreakdown" style="padding-left:10px;color:#666"></div>
        <div class="stats-line">Transformers: <strong id="trafoCount">-</strong> <span id="trafoDelta"></span></div>
        <div class="stats-line">Links: <strong id="linkCount">-</strong></div>
    </div>

    <div class="section">
        <div class="section-title">Voltage Levels</div>
        <label class="cb-row">
            <input type="checkbox" id="cb380" checked onchange="updateDisplay()">
            <span class="color-swatch" style="background:#e74c3c"></span> 380 kV
        </label>
        <label class="cb-row">
            <input type="checkbox" id="cb220" checked onchange="updateDisplay()">
            <span class="color-swatch" style="background:#27ae60"></span> 220 kV
        </label>
        <label class="cb-row">
            <input type="checkbox" id="cb110" checked onchange="updateDisplay()">
            <span class="color-swatch" style="background:#3498db"></span> 110 kV
        </label>
    </div>

    <div class="section">
        <div class="section-title">Components</div>
        <label class="cb-row">
            <input type="checkbox" id="cbLines" checked onchange="updateDisplay()">
            Lines
        </label>
        <label class="cb-row">
            <input type="checkbox" id="cbTrafos" checked onchange="updateDisplay()">
            <span class="color-swatch" style="background:#e91e63;height:2px"></span> Transformers
        </label>
        <label class="cb-row">
            <input type="checkbox" id="cbLinks" checked onchange="updateDisplay()">
            <span class="color-swatch" style="background:#ff9800;height:3px"></span> HVDC Links
        </label>
        <label class="cb-row">
            <input type="checkbox" id="cbBuses" onchange="updateDisplay()">
            <span class="color-dot" style="background:#555"></span> Bus markers
        </label>
    </div>

    <div class="diff-legend" id="diffLegend12">
        <div class="section-title">Diff orig&rarr;v2 Legend</div>
        <div class="legend-item">
            <span class="legend-swatch" style="background:#e74c3c"></span>
            Removed lines (self-loops)
        </div>
        <div class="legend-item">
            <span class="color-dot" style="background:#e74c3c;opacity:0.7"></span>
            Removed buses (merged)
        </div>
        <div class="legend-item">
            <span class="color-dot" style="background:#2ecc71;border:2px solid #27ae60"></span>
            Representative buses
        </div>
        <div class="legend-item">
            <span class="legend-swatch" style="background:#95a5a6"></span>
            Unchanged lines
        </div>
    </div>

    <div class="diff-legend" id="diffLegend23">
        <div class="section-title">Diff v2&rarr;v3 Legend</div>
        <div class="legend-item">
            <span class="legend-swatch" style="background:#e74c3c"></span>
            Removed lines (replaced)
        </div>
        <div class="legend-item">
            <span class="legend-swatch" style="background:#2ecc71;height:3px"></span>
            New merged lines
        </div>
        <div class="legend-item">
            <span class="color-dot" style="background:#e74c3c;opacity:0.7"></span>
            Eliminated waypoint buses
        </div>
        <div class="legend-item">
            <span class="legend-swatch" style="background:#95a5a6"></span>
            Unchanged lines
        </div>
    </div>
</div>

<script>
// ── Embedded Data ──────────────────────────────────────────────────────

var DATA = {{
    orig: {{
        buses: {json.dumps(versions['orig'][0])},
        lines: {json.dumps(versions['orig'][1])},
        trafos: {json.dumps(versions['orig'][2])},
        links: {json.dumps(versions['orig'][3])},
    }},
    v2: {{
        buses: {json.dumps(versions['v2'][0])},
        lines: {json.dumps(versions['v2'][1])},
        trafos: {json.dumps(versions['v2'][2])},
        links: {json.dumps(versions['v2'][3])},
    }},
    v3: {{
        buses: {json.dumps(versions['v3'][0])},
        lines: {json.dumps(versions['v3'][1])},
        trafos: {json.dumps(versions['v3'][2])},
        links: {json.dumps(versions['v3'][3])},
    }},
}};

var STATS = {{
    orig: {json.dumps(stats['orig'])},
    v2: {json.dumps(stats['v2'])},
    v3: {json.dumps(stats['v3'])},
}};

var MAPPING_V1V2 = {json.dumps(mappings['v1_v2'])};
var REP_V1V2 = new Set({json.dumps(rep_v1v2)});

var MAPPING_V2V3 = {json.dumps(mappings['v2_v3'])};
var ELIM_V2V3 = new Set({json.dumps(rep_v2v3_eliminated)});

var NAMES = {{
    orig: '{names["orig"]}',
    v2: '{names["v2"]}',
    v3: '{names["v3"]}',
}};

// ── Map Setup ──────────────────────────────────────────────────────────

var map = L.map('map', {{ zoomControl: true }}).setView([51.2, 10.4], 6);
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}@2x.png', {{
    maxZoom: 18,
    attribution: '&copy; OSM contributors &copy; CARTO',
}}).addTo(map);

var linesGroup = L.layerGroup().addTo(map);
var trafosGroup = L.layerGroup().addTo(map);
var linksGroup = L.layerGroup().addTo(map);
var busesGroup = L.layerGroup().addTo(map);

// ── Colour helpers ─────────────────────────────────────────────────────

var VCOL = {{ 380: '#e74c3c', 220: '#27ae60', 110: '#3498db' }};
function vColor(v) {{ return VCOL[v] || '#888'; }}
function vKey(v) {{ return (v >= 350) ? 380 : (v >= 180) ? 220 : 110; }}

// ── Build layers for each version ──────────────────────────────────────

var layers = {{ orig: {{}}, v2: {{}}, v3: {{}}, diff12: {{}}, diff23: {{}} }};

function buildVersionLayers(ver) {{
    var d = DATA[ver];
    var ll = {{ 110: [], 220: [], 380: [] }};
    d.lines.forEach(function(ln) {{
        var vk = vKey(ln.v || 0);
        var poly = L.polyline([[ln.lat0, ln.lon0], [ln.lat1, ln.lon1]], {{
            color: vColor(vk), weight: vk === 380 ? 2.5 : vk === 220 ? 2 : 1.2,
            opacity: 0.75,
        }});
        poly.bindPopup('<b>Line ' + ln.id + '</b><br>' + (ln.v||0) + ' kV<br>Bus ' +
            ln.b0 + ' \\u2192 ' + ln.b1 +
            (ln.s ? '<br>s_nom: ' + ln.s + ' MVA' : '') +
            (ln.len ? '<br>Length: ' + ln.len + ' km' : ''));
        if (!ll[vk]) ll[vk] = [];
        ll[vk].push(poly);
    }});
    layers[ver].lines = ll;

    var tl = [];
    d.trafos.forEach(function(t) {{
        var poly = L.polyline([[t.lat0, t.lon0], [t.lat1, t.lon1]], {{
            color: '#e91e63', weight: 2, opacity: 0.8, dashArray: '6,4',
        }});
        poly.bindPopup('<b>Trafo ' + t.id + '</b><br>Bus ' + t.b0 + ' \\u2192 ' + t.b1 +
            (t.s ? '<br>s_nom: ' + t.s + ' MVA' : ''));
        tl.push(poly);
    }});
    layers[ver].trafos = tl;

    var lkl = [];
    d.links.forEach(function(lk) {{
        var poly = L.polyline([[lk.lat0, lk.lon0], [lk.lat1, lk.lon1]], {{
            color: '#ff9800', weight: 3, opacity: 0.9, dashArray: '8,6',
        }});
        poly.bindPopup('<b>Link ' + lk.id + '</b><br>Bus ' + lk.b0 + ' \\u2192 ' + lk.b1 +
            '<br>p_nom: ' + lk.p + ' MW' +
            (lk.carrier ? '<br>Carrier: ' + lk.carrier : ''));
        lkl.push(poly);
    }});
    layers[ver].links = lkl;

    var bl = {{ 110: [], 220: [], 380: [] }};
    d.buses.forEach(function(b) {{
        var vk = vKey(b.v);
        var marker = L.circleMarker([b.y, b.x], {{
            radius: vk === 380 ? 3.5 : vk === 220 ? 3 : 2,
            color: vColor(vk), fillColor: vColor(vk),
            fillOpacity: 0.7, weight: 1,
        }});
        marker.bindPopup('<b>Bus ' + b.id + '</b><br>' + b.v + ' kV' +
            (b.c && b.c !== 'DE' ? '<br>Country: ' + b.c : ''));
        if (!bl[vk]) bl[vk] = [];
        bl[vk].push(marker);
    }});
    layers[ver].buses = bl;
}}

buildVersionLayers('orig');
buildVersionLayers('v2');
buildVersionLayers('v3');

// ── Build diff12 layers (orig→v2: substation merges) ───────────────────

(function buildDiff12() {{
    var v2BusIds = new Set(DATA.v2.buses.map(function(b) {{ return b.id; }}));
    var origBusMap = {{}};
    DATA.orig.buses.forEach(function(b) {{ origBusMap[b.id] = b; }});
    var v2LineIds = new Set(DATA.v2.lines.map(function(l) {{ return l.id; }}));

    var removedLines = [];
    DATA.orig.lines.forEach(function(ln) {{
        if (!v2LineIds.has(ln.id)) {{
            var poly = L.polyline([[ln.lat0, ln.lon0], [ln.lat1, ln.lon1]], {{
                color: '#e74c3c', weight: 2.5, opacity: 0.8,
            }});
            poly.bindPopup('<b>Removed Line ' + ln.id + '</b><br>' +
                (ln.v||0) + ' kV<br>Bus ' + ln.b0 + ' \\u2192 ' + ln.b1);
            removedLines.push(poly);
        }}
    }});

    var unchangedLines = [];
    DATA.v2.lines.forEach(function(ln) {{
        var poly = L.polyline([[ln.lat0, ln.lon0], [ln.lat1, ln.lon1]], {{
            color: '#95a5a6', weight: 1, opacity: 0.4,
        }});
        unchangedLines.push(poly);
    }});

    var removedBuses = [];
    var repBusMarkers = [];
    MAPPING_V1V2.forEach(function(m) {{
        var b = origBusMap[m.old];
        if (b) {{
            var marker = L.circleMarker([b.y, b.x], {{
                radius: 3, color: '#e74c3c', fillColor: '#e74c3c',
                fillOpacity: 0.6, weight: 1,
            }});
            marker.bindPopup('<b>Removed Bus ' + b.id + '</b><br>' + b.v + ' kV' +
                '<br>Merged into Bus ' + m.new);
            removedBuses.push(marker);
        }}
    }});

    REP_V1V2.forEach(function(rid) {{
        var b = origBusMap[rid];
        if (!b) return;
        var cnt = MAPPING_V1V2.filter(function(m) {{ return m.new === rid; }}).length;
        var marker = L.circleMarker([b.y, b.x], {{
            radius: Math.min(4 + cnt, 12),
            color: '#27ae60', fillColor: '#2ecc71',
            fillOpacity: 0.8, weight: 2,
        }});
        marker.bindPopup('<b>Representative Bus ' + rid + '</b><br>' + b.v + ' kV' +
            '<br>Absorbed ' + cnt + ' bus(es)');
        repBusMarkers.push(marker);
    }});

    layers.diff12 = {{
        removedLines: removedLines,
        unchangedLines: unchangedLines,
        removedBuses: removedBuses,
        repBuses: repBusMarkers,
    }};
}})();

// ── Build diff23 layers (v2→v3: degree-2 waypoint elimination) ─────────

(function buildDiff23() {{
    var v2BusMap = {{}};
    DATA.v2.buses.forEach(function(b) {{ v2BusMap[b.id] = b; }});
    var v3LineIds = new Set(DATA.v3.lines.map(function(l) {{ return l.id; }}));
    var v2LineIds = new Set(DATA.v2.lines.map(function(l) {{ return l.id; }}));

    // Lines removed in v2→v3 (in v2 but not in v3)
    var removedLines = [];
    DATA.v2.lines.forEach(function(ln) {{
        if (!v3LineIds.has(ln.id)) {{
            var poly = L.polyline([[ln.lat0, ln.lon0], [ln.lat1, ln.lon1]], {{
                color: '#e74c3c', weight: 2.5, opacity: 0.8,
            }});
            poly.bindPopup('<b>Removed Line ' + ln.id + '</b><br>' +
                (ln.v||0) + ' kV<br>Bus ' + ln.b0 + ' \\u2192 ' + ln.b1 +
                (ln.len ? '<br>Length: ' + ln.len + ' km' : ''));
            removedLines.push(poly);
        }}
    }});

    // New merged lines (in v3 but not in v2)
    var newLines = [];
    DATA.v3.lines.forEach(function(ln) {{
        if (!v2LineIds.has(ln.id)) {{
            var poly = L.polyline([[ln.lat0, ln.lon0], [ln.lat1, ln.lon1]], {{
                color: '#2ecc71', weight: 3, opacity: 0.9,
            }});
            poly.bindPopup('<b>New Merged Line ' + ln.id + '</b><br>' +
                (ln.v||0) + ' kV<br>Bus ' + ln.b0 + ' \\u2192 ' + ln.b1 +
                (ln.s ? '<br>s_nom: ' + ln.s + ' MVA' : '') +
                (ln.len ? '<br>Length: ' + ln.len + ' km' : ''));
            newLines.push(poly);
        }}
    }});

    // Unchanged lines (in both v2 and v3)
    var unchangedLines = [];
    DATA.v3.lines.forEach(function(ln) {{
        if (v2LineIds.has(ln.id)) {{
            var poly = L.polyline([[ln.lat0, ln.lon0], [ln.lat1, ln.lon1]], {{
                color: '#95a5a6', weight: 1, opacity: 0.4,
            }});
            unchangedLines.push(poly);
        }}
    }});

    // Eliminated waypoint buses
    var elimBuses = [];
    ELIM_V2V3.forEach(function(bid) {{
        var b = v2BusMap[bid];
        if (!b) return;
        var marker = L.circleMarker([b.y, b.x], {{
            radius: 4, color: '#e74c3c', fillColor: '#e74c3c',
            fillOpacity: 0.7, weight: 1,
        }});
        marker.bindPopup('<b>Eliminated Waypoint Bus ' + bid + '</b><br>' + b.v + ' kV');
        elimBuses.push(marker);
    }});

    layers.diff23 = {{
        removedLines: removedLines,
        newLines: newLines,
        unchangedLines: unchangedLines,
        elimBuses: elimBuses,
    }};
}})();

// ── State & Display ────────────────────────────────────────────────────

var currentVersion = 'orig';

function switchVersion(ver) {{
    currentVersion = ver;
    document.querySelectorAll('.version-btn').forEach(function(btn) {{
        btn.classList.remove('active');
    }});
    var cls = ver;
    document.querySelector('.version-btn.' + cls).classList.add('active');

    document.getElementById('diffLegend12').style.display = ver === 'diff12' ? 'block' : 'none';
    document.getElementById('diffLegend23').style.display = ver === 'diff23' ? 'block' : 'none';
    updateStats();
    updateDisplay();
}}

function deltaSpan(before, after) {{
    var d = after - before;
    if (d === 0) return '<span class="delta zero">(0)</span>';
    var sign = d > 0 ? '+' : '';
    var cls = d < 0 ? 'neg' : 'pos';
    return '<span class="delta ' + cls + '">(' + sign + d.toLocaleString() + ')</span>';
}}

function updateStats() {{
    // Map version to stats key and comparison base
    var statsMap = {{
        'orig': {{ key: 'orig', base: null }},
        'v2': {{ key: 'v2', base: 'orig' }},
        'v3': {{ key: 'v3', base: 'orig' }},
        'diff12': {{ key: 'v2', base: 'orig' }},
        'diff23': {{ key: 'v3', base: 'v2' }},
    }};
    var info = statsMap[currentVersion];
    var s = STATS[info.key];
    var sb = info.base ? STATS[info.base] : null;

    var labels = {{
        'orig': NAMES.orig + ' (Original)',
        'v2': NAMES.v2 + ' (Substations)',
        'v3': NAMES.v3 + ' (Degree-2)',
        'diff12': 'Diff: ' + NAMES.orig + ' \\u2192 ' + NAMES.v2,
        'diff23': 'Diff: ' + NAMES.v2 + ' \\u2192 ' + NAMES.v3,
    }};
    document.getElementById('versionLabel').textContent = labels[currentVersion];

    document.getElementById('busCount').textContent = s.totalBuses.toLocaleString();
    document.getElementById('busDelta').innerHTML = sb ? deltaSpan(sb.totalBuses, s.totalBuses) : '';

    var bv = s.busByV;
    document.getElementById('busBreakdown').textContent =
        '380kV: ' + (bv[380]||0).toLocaleString() +
        '  220kV: ' + (bv[220]||0).toLocaleString() +
        '  110kV: ' + (bv[110]||0).toLocaleString();

    document.getElementById('lineCount').textContent = s.totalLines.toLocaleString();
    document.getElementById('lineDelta').innerHTML = sb ? deltaSpan(sb.totalLines, s.totalLines) : '';

    var lv = s.lineByV;
    document.getElementById('lineBreakdown').textContent =
        '380kV: ' + (lv[380]||0).toLocaleString() +
        '  220kV: ' + (lv[220]||0).toLocaleString() +
        '  110kV: ' + (lv[110]||0).toLocaleString();

    document.getElementById('trafoCount').textContent = s.totalTrafos.toLocaleString();
    document.getElementById('trafoDelta').innerHTML = sb ? deltaSpan(sb.totalTrafos, s.totalTrafos) : '';
    document.getElementById('linkCount').textContent = s.totalLinks.toLocaleString();
}}

function updateDisplay() {{
    linesGroup.clearLayers();
    trafosGroup.clearLayers();
    linksGroup.clearLayers();
    busesGroup.clearLayers();

    var show380 = document.getElementById('cb380').checked;
    var show220 = document.getElementById('cb220').checked;
    var show110 = document.getElementById('cb110').checked;
    var showLines = document.getElementById('cbLines').checked;
    var showTrafos = document.getElementById('cbTrafos').checked;
    var showLinks = document.getElementById('cbLinks').checked;
    var showBuses = document.getElementById('cbBuses').checked;
    var vnomShow = {{ 380: show380, 220: show220, 110: show110 }};

    if (currentVersion === 'diff12') {{
        if (showLines) {{
            layers.diff12.unchangedLines.forEach(function(l) {{ linesGroup.addLayer(l); }});
            layers.diff12.removedLines.forEach(function(l) {{ linesGroup.addLayer(l); }});
        }}
        layers.diff12.removedBuses.forEach(function(m) {{ busesGroup.addLayer(m); }});
        layers.diff12.repBuses.forEach(function(m) {{ busesGroup.addLayer(m); }});
        if (showTrafos) layers.orig.trafos.forEach(function(l) {{ trafosGroup.addLayer(l); }});
        if (showLinks) layers.orig.links.forEach(function(l) {{ linksGroup.addLayer(l); }});

    }} else if (currentVersion === 'diff23') {{
        if (showLines) {{
            layers.diff23.unchangedLines.forEach(function(l) {{ linesGroup.addLayer(l); }});
            layers.diff23.removedLines.forEach(function(l) {{ linesGroup.addLayer(l); }});
            layers.diff23.newLines.forEach(function(l) {{ linesGroup.addLayer(l); }});
        }}
        layers.diff23.elimBuses.forEach(function(m) {{ busesGroup.addLayer(m); }});
        if (showTrafos) layers.v2.trafos.forEach(function(l) {{ trafosGroup.addLayer(l); }});
        if (showLinks) layers.v2.links.forEach(function(l) {{ linksGroup.addLayer(l); }});

    }} else {{
        var ver = currentVersion;
        if (showLines) {{
            [110, 220, 380].forEach(function(vk) {{
                if (vnomShow[vk] && layers[ver].lines[vk]) {{
                    layers[ver].lines[vk].forEach(function(l) {{ linesGroup.addLayer(l); }});
                }}
            }});
        }}
        if (showTrafos) {{
            layers[ver].trafos.forEach(function(l) {{ trafosGroup.addLayer(l); }});
        }}
        if (showLinks) {{
            layers[ver].links.forEach(function(l) {{ linksGroup.addLayer(l); }});
        }}
        if (showBuses) {{
            [110, 220, 380].forEach(function(vk) {{
                if (vnomShow[vk] && layers[ver].buses[vk]) {{
                    layers[ver].buses[vk].forEach(function(m) {{ busesGroup.addLayer(m); }});
                }}
            }});
        }}
    }}
}}

// Initial display
updateStats();
updateDisplay();

</script>
</body>
</html>'''

    return html


def main():
    parser = argparse.ArgumentParser(
        description='Generate 3-version grid simplification comparison map')
    parser.add_argument('--v1', default='eGon2025', help='Original scenario')
    parser.add_argument('--v2', default='eGon2025v2', help='Substation-simplified scenario')
    parser.add_argument('--v3', default='eGon2025v3', help='Degree-2 eliminated scenario')
    parser.add_argument('--output', default='grid_simplification_comparison.html',
                        help='Output HTML file')
    args = parser.parse_args()

    engine = create_engine(DB_URI)

    names = {'orig': args.v1, 'v2': args.v2, 'v3': args.v3}

    # Load all three scenarios
    versions_raw = {}
    for key, scn in [('orig', args.v1), ('v2', args.v2), ('v3', args.v3)]:
        print(f"Loading {scn}...")
        buses, lines, trafos, links = load_scenario(engine, scn)
        print(f"  Buses: {len(buses):,}  Lines: {len(lines):,}  "
              f"Trafos: {len(trafos):,}  Links: {len(links):,}")
        versions_raw[key] = (buses, lines, trafos, links)

    # Prepare JSON data
    print("Preparing data...")
    versions = {}
    stats = {}
    for key in ('orig', 'v2', 'v3'):
        buses, lines, trafos, links = versions_raw[key]
        versions[key] = prepare_json(buses, lines, trafos, links)
        stats[key] = compute_stats(buses, lines, trafos, links)

    # Load mappings
    print("Loading mappings...")
    mapping_v1v2 = load_mapping('results/simplification/node_mapping.csv')
    mapping_v2v3 = load_mapping('results/degree2_elimination/node_mapping.csv',
                                default_cols=('old_bus', 'new_bus', 'v_nom', 'reason'))

    mappings = {
        'v1_v2': [{'old': int(r['old_bus']), 'new': int(r['new_bus'])}
                  for _, r in mapping_v1v2.iterrows()
                  if 'old_bus' in r and 'new_bus' in r],
        'v2_v3': [{'old': int(r['old_bus'])}
                  for _, r in mapping_v2v3.iterrows()
                  if 'old_bus' in r],
    }
    print(f"  v1->v2 mappings: {len(mappings['v1_v2']):,}")
    print(f"  v2->v3 eliminated: {len(mappings['v2_v3']):,}")

    print("Generating HTML...")
    html = generate_html(versions, stats, mappings, names)

    with open(args.output, 'w') as f:
        f.write(html)
    size_mb = os.path.getsize(args.output) / (1024 * 1024)
    print(f"Saved: {args.output} ({size_mb:.1f} MB)")


if __name__ == '__main__':
    main()
