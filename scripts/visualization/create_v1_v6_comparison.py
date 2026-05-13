#!/usr/bin/env python3
"""
Generate self-contained HTML comparison map for grid versions V1–V6.
Queries all 6 scenarios from DB, embeds data as inline JSON.
V6 adds PSTs, import/export generators+loads, offshore wind profiles.
"""

import json
import os
import pandas as pd
from sqlalchemy import create_engine

DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
OUT_PATH = '/root/egon_2025_project/results/v6/grid_comparison_v1_v6.html'
SIMILARITY_CSV = '/root/egon_2025_project/results/v4/similarity_updates.csv'

VERSIONS = {
    'v1': 'eGon2025',
    'v2': 'eGon2025v2',
    'v3': 'eGon2025v3',
    'v4': 'eGon2025v4',
    'v5': 'eGon2025v5',
    'v6': 'eGon2025v6',
}

VERSION_LABELS = {
    'v1': 'V1 Raw',
    'v2': 'V2 Clustered',
    'v3': 'V3 JAO',
    'v4': 'V4 Recluster',
    'v5': 'V5 Offshore',
    'v6': 'V6 PST+Trade',
}

# HVDC link names (for v4/v5/v6)
HVDC_NAMES = {
    1: 'ALEGRO (DE-BE)',
    2: 'NordLink (DE-NO)',
    3: 'Baltic Cable (DE-SE)',
    4: 'SylWin 1',
    5: 'HelWin 1',
    6: 'HelWin 2',
    7: 'Nordergründe',
    8: 'Riffgat',
    9: 'DolWin 6',
    10: 'BorWin 1&2',
    11: 'DolWin 3',
    12: 'DolWin 1&2',
    13: 'Alpha Ventus',
    14: 'Kontek (DE-DK)',
}


def query_version(engine, scn):
    buses = pd.read_sql(
        f"SELECT bus_id, v_nom, y AS lat, x AS lon "
        f"FROM grid.egon_etrago_bus WHERE scn_name = '{scn}'", engine)
    lines = pd.read_sql(
        f"SELECT line_id, bus0, bus1, v_nom, r, x, b, s_nom, length "
        f"FROM grid.egon_etrago_line WHERE scn_name = '{scn}'", engine)
    trafos = pd.read_sql(
        f"SELECT trafo_id, bus0, bus1, s_nom, r, x, type "
        f"FROM grid.egon_etrago_transformer WHERE scn_name = '{scn}'", engine)
    return buses, lines, trafos


def query_links(engine, scn):
    return pd.read_sql(
        f"SELECT link_id, bus0, bus1, carrier, p_nom, length "
        f"FROM grid.egon_etrago_link WHERE scn_name = '{scn}'", engine)


def query_generators(engine, scn):
    return pd.read_sql(
        f"SELECT generator_id, bus, carrier, p_nom "
        f"FROM grid.egon_etrago_generator WHERE scn_name = '{scn}'", engine)


def query_loads(engine, scn):
    return pd.read_sql(
        f"SELECT load_id, bus, carrier, p_set "
        f"FROM grid.egon_etrago_load WHERE scn_name = '{scn}'", engine)


def load_similarity_ids():
    if not os.path.exists(SIMILARITY_CSV):
        return set()
    df = pd.read_csv(SIMILARITY_CSV)
    return set(df['line_id'].astype(int).tolist())


def build_json_data(engine):
    data = {}
    similarity_ids = load_similarity_ids()

    for vkey, scn in VERSIONS.items():
        print(f"Querying {vkey} ({scn})...")
        buses, lines, trafos = query_version(engine, scn)

        bus_dict = {}
        for _, b in buses.iterrows():
            bus_dict[int(b.bus_id)] = [round(float(b.lat), 5), round(float(b.lon), 5), int(b.v_nom)]

        line_list = []
        for _, l in lines.iterrows():
            flags = 0
            if vkey in ('v4', 'v5', 'v6') and int(l.line_id) in similarity_ids:
                flags = 1
            line_list.append([
                int(l.line_id), int(l.bus0), int(l.bus1), int(l.v_nom),
                round(float(l.r), 6), round(float(l.x), 6),
                round(float(l.s_nom), 1), round(float(l.length), 2), flags
            ])

        trafo_list = []
        for _, t in trafos.iterrows():
            flags = 0
            trafo_type = str(t.get('type', '')) if pd.notna(t.get('type')) else ''
            if vkey in ('v4', 'v5', 'v6') and int(t.trafo_id) >= 31366:
                flags = 1
            if trafo_type == 'PST':
                flags = 2  # PST flag
            trafo_list.append([
                int(t.trafo_id), int(t.bus0), int(t.bus1),
                round(float(t.s_nom), 1), round(float(t.r), 6), round(float(t.x), 6),
                flags
            ])

        data[vkey] = {
            'buses': bus_dict,
            'lines': line_list,
            'trafos': trafo_list,
        }
        print(f"  {len(buses)} buses, {len(lines)} lines, {len(trafos)} trafos")

    # Query HVDC links for v4, v5, v6
    for vkey in ('v4', 'v5', 'v6'):
        print(f"Querying {vkey} HVDC links...")
        links = query_links(engine, VERSIONS[vkey])
        link_list = []
        for _, lk in links.iterrows():
            link_list.append([
                int(lk.link_id), int(lk.bus0), int(lk.bus1),
                round(float(lk.p_nom), 0),
                round(float(lk.length), 1) if pd.notna(lk.length) else 0
            ])
        data[vkey]['links'] = link_list
        print(f"  {len(links)} HVDC links")

    # Query generators and loads for v6
    print("Querying v6 generators and loads...")
    gens = query_generators(engine, VERSIONS['v6'])
    loads = query_loads(engine, VERSIONS['v6'])
    gen_list = []
    for _, g in gens.iterrows():
        gen_list.append([
            int(g.generator_id), int(g.bus), str(g.carrier),
            round(float(g.p_nom), 1)
        ])
    load_list = []
    for _, ld in loads.iterrows():
        load_list.append([
            int(ld.load_id), int(ld.bus), str(ld.carrier),
            round(float(ld.p_set), 1)
        ])
    data['v6']['generators'] = gen_list
    data['v6']['loads'] = load_list
    print(f"  {len(gens)} generators, {len(loads)} loads")

    return data


def generate_html(data):
    stats = {}
    for vkey in VERSIONS:
        nb = len(data[vkey]['buses'])
        nl = len(data[vkey]['lines'])
        nt = len(data[vkey]['trafos'])
        nk = len(data[vkey].get('links', []))
        ng = len(data[vkey].get('generators', []))
        nld = len(data[vkey].get('loads', []))
        npst = sum(1 for t in data[vkey]['trafos'] if t[6] == 2)
        stats[vkey] = {'buses': nb, 'lines': nl, 'trafos': nt, 'links': nk,
                        'gens': ng, 'loads': nld, 'psts': npst}

    json_str = json.dumps(data, separators=(',', ':'))
    hvdc_names_js = json.dumps(HVDC_NAMES)

    html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>German Transmission Grid — V1–V6 Comparison</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body {{ margin: 0; padding: 0; font-family: Arial, sans-serif; }}
        #map {{ position: absolute; top: 0; bottom: 0; width: 100%; }}
        .control-panel {{
            position: absolute; top: 10px; right: 10px; background: white;
            padding: 15px; border-radius: 5px; box-shadow: 0 2px 10px rgba(0,0,0,0.3);
            z-index: 1000; max-width: 400px; max-height: 90vh; overflow-y: auto;
        }}
        .control-section {{
            margin-bottom: 12px; padding-bottom: 12px; border-bottom: 1px solid #ddd;
        }}
        .control-section:last-child {{ border-bottom: none; margin-bottom: 0; padding-bottom: 0; }}
        .control-section h3 {{ margin: 0 0 8px 0; font-size: 14px; font-weight: bold; }}
        .checkbox-item {{ margin: 4px 0; }}
        .checkbox-item label {{ display: flex; align-items: center; cursor: pointer; font-size: 13px; }}
        .checkbox-item input {{ margin-right: 8px; cursor: pointer; }}
        .color-box {{ display: inline-block; width: 20px; height: 3px; margin-right: 5px; vertical-align: middle; }}
        .stats {{ background: #f5f5f5; padding: 10px; border-radius: 3px; font-size: 12px; margin-top: 10px; }}
        .stats-line {{ margin: 3px 0; }}
        .loading {{
            position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
            background: white; padding: 20px 40px; border-radius: 5px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.3); z-index: 2000; font-size: 16px;
        }}
        .version-selector {{ display: flex; gap: 4px; margin-bottom: 8px; flex-wrap: wrap; }}
        .version-button {{
            flex: 1; min-width: 55px; padding: 8px 2px; border: 2px solid #3498db;
            background: white; color: #3498db; border-radius: 5px; cursor: pointer;
            font-size: 10px; font-weight: bold; text-align: center; transition: all 0.15s;
        }}
        .version-button.active {{ background: #3498db; color: white; }}
        .version-button:hover {{ background: #ebf5fb; }}
        .version-button.active:hover {{ background: #2980b9; }}
        .version-button.v5-btn {{ border-color: #27ae60; color: #27ae60; }}
        .version-button.v5-btn.active {{ background: #27ae60; color: white; border-color: #27ae60; }}
        .version-button.v5-btn:hover {{ background: #eafaf1; }}
        .version-button.v5-btn.active:hover {{ background: #229954; }}
        .version-button.v6-btn {{ border-color: #e67e22; color: #e67e22; }}
        .version-button.v6-btn.active {{ background: #e67e22; color: white; border-color: #e67e22; }}
        .version-button.v6-btn:hover {{ background: #fef5e7; }}
        .version-button.v6-btn.active:hover {{ background: #d35400; }}
        .legend {{
            background: white; padding: 10px; border-radius: 5px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.3); font-size: 12px;
        }}
        .legend-item {{ margin: 4px 0; display: flex; align-items: center; }}
        .legend-line {{ width: 30px; height: 4px; margin-right: 8px; }}
        .legend-circle {{ width: 10px; height: 10px; border-radius: 50%; margin-right: 8px; }}
        .legend-section {{ margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid #eee; }}
        .legend-section:last-child {{ border-bottom: none; }}
        .legend-title {{ font-weight: bold; margin-bottom: 4px; font-size: 11px; color: #666; }}
    </style>
</head>
<body>
    <div id="loading" class="loading">Loading grid data...</div>
    <div id="map"></div>
    <div class="control-panel">
        <div class="control-section">
            <h3>Network Version</h3>
            <div class="version-selector">
                <button id="btnV1" class="version-button" onclick="switchVersion('v1')">
                    V1 Raw<br><small>{stats['v1']['buses']:,}</small>
                </button>
                <button id="btnV2" class="version-button" onclick="switchVersion('v2')">
                    V2 Cluster<br><small>{stats['v2']['buses']:,}</small>
                </button>
                <button id="btnV3" class="version-button" onclick="switchVersion('v3')">
                    V3 JAO<br><small>{stats['v3']['buses']:,}</small>
                </button>
                <button id="btnV4" class="version-button" onclick="switchVersion('v4')">
                    V4 Reclust<br><small>{stats['v4']['buses']:,}</small>
                </button>
                <button id="btnV5" class="version-button v5-btn" onclick="switchVersion('v5')">
                    V5 Offshore<br><small>{stats['v5']['buses']:,}</small>
                </button>
                <button id="btnV6" class="version-button v6-btn active" onclick="switchVersion('v6')">
                    V6 PST<br><small>{stats['v6']['buses']:,}</small>
                </button>
            </div>
        </div>

        <div class="control-section">
            <h3>Voltage Levels</h3>
            <div class="checkbox-item"><label><input type="checkbox" id="show380" checked>
                <span class="color-box" style="background: #e74c3c; height: 4px;"></span>380 kV</label></div>
            <div class="checkbox-item"><label><input type="checkbox" id="show220" checked>
                <span class="color-box" style="background: #27ae60; height: 3px;"></span>220 kV</label></div>
            <div class="checkbox-item"><label><input type="checkbox" id="show110" checked>
                <span class="color-box" style="background: #3498db; height: 2px;"></span>110 kV</label></div>
        </div>

        <div class="control-section">
            <h3>Components</h3>
            <div class="checkbox-item"><label><input type="checkbox" id="showTransformers" checked>
                <span class="color-box" style="background: #e91e63; height: 3px; border-top: 2px dashed #e91e63;"></span>
                Transformers</label></div>
            <div class="checkbox-item"><label><input type="checkbox" id="showBuses">Show buses</label></div>
        </div>

        <div class="control-section">
            <h3>Parallel Lines</h3>
            <div class="checkbox-item"><label><input type="checkbox" id="spreadParallel">
                Spread parallel circuits</label></div>
            <div style="font-size: 11px; color: #888; margin-top: 4px;" id="parallelInfo">-</div>
        </div>

        <div class="control-section" id="extraSection" style="display: none;">
            <h3 id="extraTitle">Extras</h3>
            <div class="checkbox-item"><label><input type="checkbox" id="showHVDC" checked>
                <span class="color-box" style="background: #9b59b6; height: 3px; border-top: 2px dashed #9b59b6;"></span>
                HVDC links</label></div>
            <div class="checkbox-item"><label><input type="checkbox" id="showNewTrafos" checked>
                <span class="color-box" style="background: #66bb6a; height: 3px; border-top: 2px dashed #66bb6a;"></span>
                New transformers</label></div>
            <div class="checkbox-item"><label><input type="checkbox" id="showSimilarity">
                <span class="color-box" style="background: #f1c40f; height: 4px;"></span>
                Similarity-updated lines</label></div>
            <div class="checkbox-item"><label><input type="checkbox" id="showPSTs" checked>
                <span class="color-box" style="background: #ff5722; height: 3px; border-top: 2px dashed #ff5722;"></span>
                Phase-shifting transformers</label></div>
            <div class="checkbox-item"><label><input type="checkbox" id="showGenLoads" checked>
                <span class="color-box" style="background: #00bcd4; height: 3px;"></span>
                Import/export &amp; offshore wind</label></div>
        </div>

        <div class="stats">
            <div class="stats-line"><strong>Grid Statistics:</strong></div>
            <div class="stats-line" id="versionInfo">-</div>
            <div class="stats-line" id="busCount">-</div>
            <div class="stats-line" id="lineCount">-</div>
            <div class="stats-line" id="trafoCount">-</div>
            <div class="stats-line" id="busBreakdown"></div>
            <div class="stats-line" id="lineBreakdown"></div>
            <div class="stats-line" id="extraStats" style="display: none;"></div>
        </div>
    </div>

    <script>
        var DATA = {json_str};
        var HVDC_NAMES = {hvdc_names_js};

        var map = L.map('map').setView([51.2, 10.4], 6);
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '&copy; OpenStreetMap', maxZoom: 18
        }}).addTo(map);

        var allVersions = ['v1', 'v2', 'v3', 'v4', 'v5', 'v6'];
        var versionsWithExtras = ['v4', 'v5', 'v6'];
        var versionNames = {{
            v1: 'V1 Raw — {stats["v1"]["buses"]:,} buses, {stats["v1"]["lines"]:,} lines',
            v2: 'V2 Clustered — {stats["v2"]["buses"]:,} buses, {stats["v2"]["lines"]:,} lines',
            v3: 'V3 JAO — {stats["v3"]["buses"]:,} buses, {stats["v3"]["lines"]:,} lines',
            v4: 'V4 Recluster — {stats["v4"]["buses"]:,} buses, {stats["v4"]["lines"]:,} lines',
            v5: 'V5 Offshore — {stats["v5"]["buses"]:,} buses, {stats["v5"]["lines"]:,} lines, {stats["v5"]["links"]} HVDC',
            v6: 'V6 PST+Trade — {stats["v6"]["buses"]:,} buses, {stats["v6"]["lines"]:,} lines, {stats["v6"]["trafos"]} trafos ({stats["v6"]["psts"]} PSTs), {stats["v6"]["gens"]} gens, {stats["v6"]["loads"]} loads'
        }};

        var currentVersion = 'v6';
        var voltageColors = {{380: '#e74c3c', 220: '#27ae60', 110: '#3498db'}};
        var voltageWeights = {{380: 3.5, 220: 2.5, 110: 1.5}};

        var linesGroup = L.layerGroup().addTo(map);
        var busesGroup = L.layerGroup().addTo(map);
        var transformersGroup = L.layerGroup().addTo(map);
        var hvdcGroup = L.layerGroup().addTo(map);
        var genLoadGroup = L.layerGroup().addTo(map);

        var lineLayers = {{}};
        var busLayers = {{}};
        var trafoLayers = {{}};
        var hvdcLayers = {{}};
        var genLoadLayers = {{}};
        var versionStats = {{}};
        var spreadAmount = 0;
        var animating = false;
        var SPREAD_OFFSET = 0.003;

        function offsetCoords(lat0, lon0, lat1, lon1, idx, total) {{
            if (total <= 1) return [[lat0, lon0], [lat1, lon1]];
            var shift = (idx - (total - 1) / 2) * SPREAD_OFFSET;
            var dlat = lat1 - lat0, dlon = lon1 - lon0;
            var len = Math.sqrt(dlat * dlat + dlon * dlon);
            if (len === 0) return [[lat0, lon0], [lat1, lon1]];
            var px = dlat / len, py = -dlon / len;
            return [
                [lat0 + py * shift, lon0 + px * shift],
                [lat1 + py * shift, lon1 + px * shift]
            ];
        }}

        function init() {{
            allVersions.forEach(function(v) {{
                var d = DATA[v];
                var buses = d.buses;
                lineLayers[v] = {{}};
                busLayers[v] = {{}};
                trafoLayers[v] = [];
                hvdcLayers[v] = [];
                genLoadLayers[v] = [];

                var busByV = {{}}, lineByV = {{}};

                // Group lines by bus pair
                var pairGroups = {{}};
                d.lines.forEach(function(l) {{
                    var b0 = l[1], b1 = l[2];
                    var key = b0 < b1 ? b0 + '_' + b1 : b1 + '_' + b0;
                    if (!pairGroups[key]) pairGroups[key] = [];
                    pairGroups[key].push(l);
                }});

                var parallelPairs = 0, parallelLines = 0, maxP = 0;
                Object.keys(pairGroups).forEach(function(key) {{
                    var g = pairGroups[key];
                    if (g.length > 1) {{ parallelPairs++; parallelLines += g.length; if (g.length > maxP) maxP = g.length; }}
                    for (var i = 0; i < g.length; i++) {{ g[i]._gi = i; g[i]._gs = g.length; }}
                }});

                d.lines.forEach(function(l) {{
                    var b0 = buses[l[1]], b1 = buses[l[2]];
                    if (!b0 || !b1) return;
                    var vnom = l[3], isSim = l[8] === 1;
                    var gi = l._gi || 0, gs = l._gs || 1;

                    var origCoords = [[b0[0], b0[1]], [b1[0], b1[1]]];
                    var spreadCoords = offsetCoords(b0[0], b0[1], b1[0], b1[1], gi, gs);

                    var poly = L.polyline(origCoords, {{
                        color: voltageColors[vnom] || '#999',
                        weight: voltageWeights[vnom] || 1, opacity: 0.6
                    }});

                    var popup = '<b>Line ' + l[0] + '</b><br>' + vnom + ' kV<br>Bus ' + l[1] + ' &harr; ' + l[2];
                    if (gs > 1) popup += '<br><b>Circuit ' + (gi + 1) + ' of ' + gs + '</b>';
                    if (l[6]) popup += '<br>Capacity: ' + Math.round(l[6]) + ' MVA';
                    if (l[7]) popup += '<br>Length: ' + l[7].toFixed(1) + ' km';
                    if (l[4] > 0) popup += '<br>r=' + l[4].toFixed(4) + ', x=' + l[5].toFixed(4);
                    if (isSim) popup += '<br><b style="color:#f1c40f;">Similarity-updated</b>';
                    poly.bindPopup(popup);

                    if (!lineLayers[v][vnom]) lineLayers[v][vnom] = [];
                    lineLayers[v][vnom].push({{
                        layer: poly, isSim: isSim,
                        origCoords: origCoords, spreadCoords: spreadCoords, groupSize: gs
                    }});
                    lineByV[vnom] = (lineByV[vnom] || 0) + 1;
                }});

                Object.keys(buses).forEach(function(bid) {{
                    var b = buses[bid];
                    var vnom = b[2];
                    var marker = L.circleMarker([b[0], b[1]], {{
                        radius: 2, fillColor: voltageColors[vnom] || '#999',
                        color: '#fff', weight: 1, opacity: 1, fillOpacity: 0.8
                    }});
                    marker.bindPopup('<b>Bus ' + bid + '</b><br>' + vnom + ' kV');
                    if (!busLayers[v][vnom]) busLayers[v][vnom] = [];
                    busLayers[v][vnom].push(marker);
                    busByV[vnom] = (busByV[vnom] || 0) + 1;
                }});

                d.trafos.forEach(function(t) {{
                    var b0 = buses[t[1]], b1 = buses[t[2]];
                    if (!b0 || !b1) return;
                    var isNew = t[6] === 1;
                    var isPST = t[6] === 2;
                    var color = isPST ? '#ff5722' : (isNew ? '#66bb6a' : '#e91e63');
                    var weight = isPST ? 4 : 2;
                    var dash = isPST ? '8, 4' : '6, 4';
                    var poly = L.polyline([[b0[0], b0[1]], [b1[0], b1[1]]], {{
                        color: color, weight: weight, opacity: 0.8, dashArray: dash
                    }});
                    var v0 = b0[2], v1 = b1[2];
                    var popup = '<b>Trafo ' + t[0] + '</b>';
                    if (isPST) popup = '<b style="color:#ff5722;">PST ' + t[0] + '</b>';
                    popup += '<br>' + v0 + ' kV &harr; ' + v1 + ' kV';
                    if (t[3]) popup += '<br>Capacity: ' + Math.round(t[3]) + ' MVA';
                    if (t[5] > 0) popup += '<br>x=' + t[5].toFixed(4) + ', r=' + t[4].toFixed(4);
                    if (isNew) popup += '<br><b style="color:#66bb6a;">New</b>';
                    if (isPST) popup += '<br><b style="color:#ff5722;">Phase-shifting transformer</b>';
                    poly.bindPopup(popup);
                    trafoLayers[v].push({{ layer: poly, isNew: isNew, isPST: isPST }});
                }});

                // HVDC links
                if (d.links) {{
                    d.links.forEach(function(lk) {{
                        var b0 = buses[lk[1]], b1 = buses[lk[2]];
                        if (!b0 || !b1) return;
                        var poly = L.polyline([[b0[0], b0[1]], [b1[0], b1[1]]], {{
                            color: '#9b59b6', weight: 4, opacity: 0.8, dashArray: '10, 6'
                        }});
                        var name = HVDC_NAMES[lk[0]] || ('Link ' + lk[0]);
                        poly.bindPopup('<b style="color:#9b59b6;">' + name + '</b><br>HVDC ' + Math.round(lk[3]) + ' MW');
                        hvdcLayers[v].push(poly);
                    }});
                }}

                // Generators and loads (v6 only)
                if (d.generators) {{
                    d.generators.forEach(function(g) {{
                        var b = buses[g[1]];
                        if (!b) return;
                        var isOffwind = g[2] === 'offwind';
                        var color = isOffwind ? '#00897b' : '#00bcd4';
                        var radius = Math.max(4, Math.min(12, Math.sqrt(g[3] / 50)));
                        var marker = L.circleMarker([b[0], b[1]], {{
                            radius: radius, fillColor: color, color: '#fff',
                            weight: 2, opacity: 1, fillOpacity: 0.7
                        }});
                        var popup = '<b style="color:' + color + ';">Generator ' + g[0] + '</b><br>';
                        popup += 'Carrier: ' + g[2] + '<br>Capacity: ' + Math.round(g[3]) + ' MW';
                        popup += '<br>Bus: ' + g[1];
                        marker.bindPopup(popup);
                        genLoadLayers[v].push(marker);
                    }});
                }}
                if (d.loads) {{
                    d.loads.forEach(function(ld) {{
                        var b = buses[ld[1]];
                        if (!b) return;
                        var radius = Math.max(3, Math.min(10, Math.sqrt(ld[3] / 50)));
                        var marker = L.circleMarker([b[0], b[1]], {{
                            radius: radius, fillColor: '#ff9800', color: '#fff',
                            weight: 2, opacity: 1, fillOpacity: 0.6
                        }});
                        var popup = '<b style="color:#ff9800;">Load ' + ld[0] + '</b><br>';
                        popup += 'Carrier: ' + ld[2] + '<br>Demand: ' + Math.round(ld[3]) + ' MW';
                        popup += '<br>Bus: ' + ld[1];
                        marker.bindPopup(popup);
                        genLoadLayers[v].push(marker);
                    }});
                }}

                versionStats[v] = {{
                    totalBuses: Object.keys(buses).length,
                    totalLines: d.lines.length,
                    totalTrafos: d.trafos.length,
                    totalLinks: (d.links || []).length,
                    totalGens: (d.generators || []).length,
                    totalLoads: (d.loads || []).length,
                    pstCount: d.trafos.filter(function(t) {{ return t[6] === 2; }}).length,
                    busByV: busByV, lineByV: lineByV,
                    simLines: d.lines.filter(function(l) {{ return l[8] === 1; }}).length,
                    newTrafos: d.trafos.filter(function(t) {{ return t[6] === 1; }}).length,
                    parallelPairs: parallelPairs, parallelLines: parallelLines, maxP: maxP
                }};
            }});

            updateStats();
            updateDisplay();
            document.getElementById('loading').style.display = 'none';
        }}

        function animateSpread(target, duration) {{
            if (animating) return;
            animating = true;
            var start = spreadAmount, t0 = performance.now();
            function step(now) {{
                var t = Math.min((now - t0) / duration, 1);
                t = t < 0.5 ? 2*t*t : 1 - Math.pow(-2*t+2,2)/2;
                spreadAmount = start + (target - start) * t;
                applySpread();
                if (t < 1) requestAnimationFrame(step);
                else {{ spreadAmount = target; animating = false; }}
            }}
            requestAnimationFrame(step);
        }}

        function applySpread() {{
            [110,220,380].forEach(function(vnom) {{
                if (!lineLayers[currentVersion][vnom]) return;
                lineLayers[currentVersion][vnom].forEach(function(item) {{
                    if (item.groupSize <= 1 || !linesGroup.hasLayer(item.layer)) return;
                    var o = item.origCoords, s = item.spreadCoords, a = spreadAmount;
                    item.layer.setLatLngs([
                        [o[0][0]+(s[0][0]-o[0][0])*a, o[0][1]+(s[0][1]-o[0][1])*a],
                        [o[1][0]+(s[1][0]-o[1][0])*a, o[1][1]+(s[1][1]-o[1][1])*a]
                    ]);
                }});
            }});
        }}

        function switchVersion(version) {{
            currentVersion = version;
            allVersions.forEach(function(v) {{
                document.getElementById('btn'+v.toUpperCase()).classList.toggle('active', v === version);
            }});
            var hasExtras = versionsWithExtras.indexOf(version) >= 0;
            document.getElementById('extraSection').style.display = hasExtras ? 'block' : 'none';
            document.getElementById('extraTitle').textContent = version.toUpperCase() + ' Extras';
            updateStats();
            updateDisplay();
        }}

        function updateStats() {{
            var s = versionStats[currentVersion];
            document.getElementById('versionInfo').textContent = 'Version: ' + versionNames[currentVersion];
            document.getElementById('busCount').textContent = 'Buses: ' + s.totalBuses.toLocaleString();
            document.getElementById('lineCount').textContent = 'Lines: ' + s.totalLines.toLocaleString();
            var trafoText = 'Transformers: ' + s.totalTrafos.toLocaleString();
            if (s.pstCount > 0) trafoText += ' (' + s.pstCount + ' PSTs)';
            document.getElementById('trafoCount').textContent = trafoText;

            var bv = s.busByV;
            document.getElementById('busBreakdown').textContent =
                '  110: ' + (bv[110]||0).toLocaleString() + ' | 220: ' + (bv[220]||0).toLocaleString() + ' | 380: ' + (bv[380]||0).toLocaleString();
            var lv = s.lineByV;
            document.getElementById('lineBreakdown').textContent =
                '  110: ' + (lv[110]||0).toLocaleString() + ' | 220: ' + (lv[220]||0).toLocaleString() + ' | 380: ' + (lv[380]||0).toLocaleString();

            document.getElementById('parallelInfo').innerHTML =
                s.parallelPairs + ' pairs, ' + s.parallelLines + ' circuits (max ' + s.maxP + 'x)';

            var ex = document.getElementById('extraStats');
            if (versionsWithExtras.indexOf(currentVersion) >= 0) {{
                ex.style.display = 'block';
                var txt = 'HVDC: ' + s.totalLinks + ' | Sim lines: ' + s.simLines + ' | New trafos: ' + s.newTrafos;
                if (s.pstCount > 0) txt += ' | PSTs: ' + s.pstCount;
                if (s.totalGens > 0) txt += '<br>Gens: ' + s.totalGens + ' | Loads: ' + s.totalLoads;
                ex.innerHTML = txt;
            }} else {{
                ex.style.display = 'none';
            }}
        }}

        function updateDisplay() {{
            linesGroup.clearLayers(); busesGroup.clearLayers();
            transformersGroup.clearLayers(); hvdcGroup.clearLayers();
            genLoadGroup.clearLayers();

            var show380 = document.getElementById('show380').checked;
            var show220 = document.getElementById('show220').checked;
            var show110 = document.getElementById('show110').checked;
            var showTrafos = document.getElementById('showTransformers').checked;
            var showBuses = document.getElementById('showBuses').checked;
            var showHVDC = document.getElementById('showHVDC') ? document.getElementById('showHVDC').checked : false;
            var showNewTrafos = document.getElementById('showNewTrafos') ? document.getElementById('showNewTrafos').checked : false;
            var showSim = document.getElementById('showSimilarity') ? document.getElementById('showSimilarity').checked : false;
            var showPSTs = document.getElementById('showPSTs') ? document.getElementById('showPSTs').checked : false;
            var showGenLoads = document.getElementById('showGenLoads') ? document.getElementById('showGenLoads').checked : false;
            var vnomShow = {{380: show380, 220: show220, 110: show110}};

            [110,220,380].forEach(function(vnom) {{
                if (vnomShow[vnom] && lineLayers[currentVersion][vnom]) {{
                    lineLayers[currentVersion][vnom].forEach(function(item) {{
                        if (item.groupSize > 1 && spreadAmount > 0) {{
                            var o = item.origCoords, s = item.spreadCoords, a = spreadAmount;
                            item.layer.setLatLngs([
                                [o[0][0]+(s[0][0]-o[0][0])*a, o[0][1]+(s[0][1]-o[0][1])*a],
                                [o[1][0]+(s[1][0]-o[1][0])*a, o[1][1]+(s[1][1]-o[1][1])*a]
                            ]);
                        }} else {{ item.layer.setLatLngs(item.origCoords); }}
                        if (showSim && item.isSim) {{
                            item.layer.setStyle({{ color: '#f1c40f', weight: 4, opacity: 0.85 }});
                        }} else {{
                            item.layer.setStyle({{ color: voltageColors[vnom], weight: voltageWeights[vnom], opacity: 0.6 }});
                        }}
                        linesGroup.addLayer(item.layer);
                    }});
                }}
            }});

            if (showTrafos && trafoLayers[currentVersion]) {{
                trafoLayers[currentVersion].forEach(function(item) {{
                    if (item.isPST && !showPSTs) return;
                    if (item.isNew && !showNewTrafos) return;
                    transformersGroup.addLayer(item.layer);
                }});
            }}

            if (showBuses) {{
                [110,220,380].forEach(function(vnom) {{
                    if (vnomShow[vnom] && busLayers[currentVersion][vnom]) {{
                        busLayers[currentVersion][vnom].forEach(function(m) {{ busesGroup.addLayer(m); }});
                    }}
                }});
            }}

            if (showHVDC && hvdcLayers[currentVersion]) {{
                hvdcLayers[currentVersion].forEach(function(l) {{ hvdcGroup.addLayer(l); }});
            }}

            if (showGenLoads && genLoadLayers[currentVersion]) {{
                genLoadLayers[currentVersion].forEach(function(m) {{ genLoadGroup.addLayer(m); }});
            }}
        }}

        ['show380','show220','show110','showTransformers','showBuses',
         'showHVDC','showNewTrafos','showSimilarity','showPSTs','showGenLoads'
        ].forEach(function(id) {{
            var el = document.getElementById(id);
            if (el) el.addEventListener('change', updateDisplay);
        }});

        document.getElementById('spreadParallel').addEventListener('change', function() {{
            animateSpread(this.checked ? 1 : 0, 400);
        }});

        window.switchVersion = switchVersion;

        var legend = L.control({{position: 'bottomleft'}});
        legend.onAdd = function() {{
            var div = L.DomUtil.create('div', 'legend');
            div.innerHTML =
                '<div style="font-weight: bold; margin-bottom: 8px;">Legend</div>' +
                '<div class="legend-section">' +
                '  <div class="legend-title">VOLTAGE LEVELS</div>' +
                '  <div class="legend-item"><div class="legend-line" style="background: #e74c3c; height: 4px;"></div>380 kV</div>' +
                '  <div class="legend-item"><div class="legend-line" style="background: #27ae60; height: 3px;"></div>220 kV</div>' +
                '  <div class="legend-item"><div class="legend-line" style="background: #3498db; height: 2px;"></div>110 kV</div>' +
                '</div>' +
                '<div class="legend-section">' +
                '  <div class="legend-title">COMPONENTS</div>' +
                '  <div class="legend-item"><div class="legend-line" style="background: #e91e63; border-top: 2px dashed #e91e63;"></div>Transformer</div>' +
                '  <div class="legend-item"><div class="legend-line" style="background: #ff5722; height: 4px; border-top: 2px dashed #ff5722;"></div>PST (V6)</div>' +
                '  <div class="legend-item"><div class="legend-line" style="background: #66bb6a; border-top: 2px dashed #66bb6a;"></div>New trafo</div>' +
                '  <div class="legend-item"><div class="legend-line" style="background: #9b59b6; height: 4px; border-top: 2px dashed #9b59b6;"></div>HVDC link</div>' +
                '  <div class="legend-item"><div class="legend-line" style="background: #f1c40f; height: 4px;"></div>Similarity-updated</div>' +
                '  <div class="legend-item"><div class="legend-circle" style="background: #00bcd4;"></div>Import gen</div>' +
                '  <div class="legend-item"><div class="legend-circle" style="background: #00897b;"></div>Offshore wind</div>' +
                '  <div class="legend-item"><div class="legend-circle" style="background: #ff9800;"></div>Export load</div>' +
                '</div>';
            return div;
        }};
        legend.addTo(map);

        init();
    </script>
</body>
</html>'''

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w') as f:
        f.write(html)
    size_mb = os.path.getsize(OUT_PATH) / 1024 / 1024
    print(f"\nHTML written to {OUT_PATH} ({size_mb:.1f} MB)")


def main():
    engine = create_engine(DB_URI)
    data = build_json_data(engine)
    generate_html(data)


if __name__ == '__main__':
    main()
