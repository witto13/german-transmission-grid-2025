#!/usr/bin/env python3
"""
Create interactive map of the eGon2025 grid topology.

Features:
- Filter by voltage level (110/220/380 kV)
- Filter by technology (all carriers)
- Circles showing capacity at each bus
- Hover tooltips with detailed breakdown
"""

import json
import pandas as pd
from sqlalchemy import create_engine

# Database connection
DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
SCENARIO = 'eGon2025'

def load_grid_data(engine):
    """Load all grid components from database."""

    # Buses
    buses = pd.read_sql(f"""
        SELECT bus_id, x as lon, y as lat, v_nom
        FROM grid.egon_etrago_bus
        WHERE scn_name = '{SCENARIO}' AND country = 'DE'
    """, engine)

    # Lines
    lines = pd.read_sql(f"""
        SELECT l.line_id, l.bus0, l.bus1, l.s_nom, l.length,
               b0.x as lon0, b0.y as lat0, b1.x as lon1, b1.y as lat1,
               b0.v_nom as v_nom
        FROM grid.egon_etrago_line l
        JOIN grid.egon_etrago_bus b0 ON l.bus0 = b0.bus_id AND b0.scn_name = '{SCENARIO}'
        JOIN grid.egon_etrago_bus b1 ON l.bus1 = b1.bus_id AND b1.scn_name = '{SCENARIO}'
        WHERE l.scn_name = '{SCENARIO}'
    """, engine)

    # Generators aggregated by bus and carrier
    generators = pd.read_sql(f"""
        SELECT bus, carrier, SUM(p_nom) as capacity_mw, COUNT(*) as n_units
        FROM grid.egon_etrago_generator
        WHERE scn_name = '{SCENARIO}'
        GROUP BY bus, carrier
    """, engine)

    # Loads aggregated by bus
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

    return buses, lines, generators, loads, storage


def prepare_bus_data(buses, generators, loads, storage):
    """Prepare bus data with aggregated generation and load info."""

    # Pivot generators by carrier
    gen_pivot = generators.pivot_table(
        index='bus',
        columns='carrier',
        values='capacity_mw',
        fill_value=0
    ).reset_index()

    # Total generation per bus
    gen_total = generators.groupby('bus')['capacity_mw'].sum().reset_index()
    gen_total.columns = ['bus_id', 'total_gen_mw']

    # Total load per bus
    load_total = loads.groupby('bus')['load_mw'].sum().reset_index()
    load_total.columns = ['bus_id', 'total_load_mw']

    # Load by type
    load_pivot = loads.pivot_table(
        index='bus',
        columns='type',
        values='load_mw',
        fill_value=0
    ).reset_index()

    # Storage total
    storage_total = storage.groupby('bus')['capacity_mw'].sum().reset_index()
    storage_total.columns = ['bus_id', 'total_storage_mw']

    # Storage by carrier
    storage_pivot = storage.pivot_table(
        index='bus',
        columns='carrier',
        values='capacity_mw',
        fill_value=0
    ).reset_index()
    if 'bus' in storage_pivot.columns:
        storage_pivot = storage_pivot.rename(columns={'bus': 'bus_id'})

    # Merge all into buses
    bus_data = buses.copy()
    bus_data = bus_data.merge(gen_total, left_on='bus_id', right_on='bus_id', how='left')
    bus_data = bus_data.merge(load_total, left_on='bus_id', right_on='bus_id', how='left')
    bus_data = bus_data.merge(storage_total, left_on='bus_id', right_on='bus_id', how='left')

    # Fill NaN with 0
    bus_data['total_gen_mw'] = bus_data['total_gen_mw'].fillna(0)
    bus_data['total_load_mw'] = bus_data['total_load_mw'].fillna(0)
    bus_data['total_storage_mw'] = bus_data['total_storage_mw'].fillna(0)

    # Create detailed breakdown for tooltips
    gen_pivot_renamed = gen_pivot.rename(columns={'bus': 'bus_id'})
    load_pivot_renamed = load_pivot.rename(columns={'bus': 'bus_id'})

    # Get all carrier columns
    carrier_cols = [c for c in gen_pivot_renamed.columns if c != 'bus_id']
    load_type_cols = [c for c in load_pivot_renamed.columns if c != 'bus_id']
    storage_cols = [c for c in storage_pivot.columns if c != 'bus_id']

    # Create generation breakdown dict for each bus
    gen_breakdown = {}
    for _, row in gen_pivot_renamed.iterrows():
        bus_id = row['bus_id']
        breakdown = {c: row[c] for c in carrier_cols if row[c] > 0}
        if breakdown:
            gen_breakdown[bus_id] = breakdown

    # Create load breakdown dict
    load_breakdown = {}
    for _, row in load_pivot_renamed.iterrows():
        bus_id = row['bus_id']
        breakdown = {c: row[c] for c in load_type_cols if row[c] > 0}
        if breakdown:
            load_breakdown[bus_id] = breakdown

    # Create storage breakdown dict
    storage_breakdown = {}
    for _, row in storage_pivot.iterrows():
        bus_id = row['bus_id']
        breakdown = {c: row[c] for c in storage_cols if row[c] > 0}
        if breakdown:
            storage_breakdown[bus_id] = breakdown

    return bus_data, gen_breakdown, load_breakdown, storage_breakdown, carrier_cols


def generate_html_map(buses, lines, gen_breakdown, load_breakdown, storage_breakdown, carriers):
    """Generate the interactive HTML map."""

    # Prepare bus data as JSON
    bus_list = []
    for _, row in buses.iterrows():
        bus_id = int(row['bus_id'])
        bus_entry = {
            'id': bus_id,
            'lat': row['lat'],
            'lon': row['lon'],
            'v_nom': int(row['v_nom']),
            'total_gen': round(row['total_gen_mw'], 2),
            'total_load': round(row['total_load_mw'], 2),
            'total_storage': round(row['total_storage_mw'], 2),
            'gen': {k: round(v, 2) for k, v in gen_breakdown.get(bus_id, {}).items()},
            'load': {k: round(v, 2) for k, v in load_breakdown.get(bus_id, {}).items()},
            'storage': {k: round(v, 2) for k, v in storage_breakdown.get(bus_id, {}).items()},
        }
        bus_list.append(bus_entry)

    # Prepare line data as JSON
    line_list = []
    for _, row in lines.iterrows():
        line_list.append({
            'id': int(row['line_id']),
            'coords': [[row['lat0'], row['lon0']], [row['lat1'], row['lon1']]],
            'v_nom': int(row['v_nom']),
            's_nom': round(row['s_nom'], 1) if pd.notna(row['s_nom']) else 0,
            'length': round(row['length'], 1) if pd.notna(row['length']) else 0,
        })

    # Get unique carriers
    all_carriers = sorted(set(carriers))

    # Color mapping for carriers
    carrier_colors = {
        'solar': '#FFD700',
        'onwind': '#4169E1',
        'offwind': '#000080',
        'gas': '#FF6347',
        'coal': '#2F4F4F',
        'lignite': '#8B4513',
        'oil': '#800000',
        'biogas': '#32CD32',
        'biomass': '#228B22',
        'run_of_river': '#00CED1',
        'reservoir': '#1E90FF',
        'waste': '#808080',
        'other': '#A9A9A9',
        'hydrogen': '#00FFFF',
        'battery': '#9370DB',
        'pumped_hydro': '#4682B4',
    }

    # Voltage level colors
    voltage_colors = {
        110: '#2ca02c',  # Green
        220: '#1f77b4',  # Blue
        380: '#d62728',  # Red
    }

    html_content = f'''<!DOCTYPE html>
<html>
<head>
    <title>eGon2025 Grid Map</title>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
        body {{ margin: 0; padding: 0; font-family: Arial, sans-serif; }}
        #map {{ position: absolute; top: 0; bottom: 0; left: 300px; right: 0; }}
        #sidebar {{
            position: absolute;
            top: 0;
            left: 0;
            width: 300px;
            height: 100%;
            background: #f8f9fa;
            padding: 15px;
            box-sizing: border-box;
            overflow-y: auto;
            border-right: 2px solid #dee2e6;
        }}
        h2 {{ margin-top: 0; color: #333; font-size: 18px; }}
        h3 {{ margin: 15px 0 10px 0; color: #555; font-size: 14px; border-bottom: 1px solid #ddd; padding-bottom: 5px; }}
        .filter-group {{ margin-bottom: 15px; }}
        .filter-item {{
            display: flex;
            align-items: center;
            margin: 5px 0;
            cursor: pointer;
        }}
        .filter-item input {{ margin-right: 8px; cursor: pointer; }}
        .filter-item label {{ cursor: pointer; font-size: 13px; }}
        .color-box {{
            width: 14px;
            height: 14px;
            margin-right: 8px;
            border-radius: 3px;
            border: 1px solid #666;
        }}
        .voltage-110 {{ background-color: #2ca02c; }}
        .voltage-220 {{ background-color: #1f77b4; }}
        .voltage-380 {{ background-color: #d62728; }}
        .stats {{
            background: #fff;
            padding: 10px;
            border-radius: 5px;
            margin-bottom: 15px;
            font-size: 12px;
            border: 1px solid #ddd;
        }}
        .stats-row {{ display: flex; justify-content: space-between; margin: 3px 0; }}
        .btn-group {{ margin: 10px 0; }}
        .btn {{
            padding: 5px 10px;
            margin-right: 5px;
            border: 1px solid #ccc;
            background: #fff;
            cursor: pointer;
            border-radius: 3px;
            font-size: 11px;
        }}
        .btn:hover {{ background: #e9ecef; }}
        .leaflet-popup-content {{ min-width: 200px; }}
        .popup-section {{ margin: 8px 0; }}
        .popup-section h4 {{ margin: 0 0 5px 0; font-size: 12px; color: #333; border-bottom: 1px solid #eee; }}
        .popup-row {{ display: flex; justify-content: space-between; font-size: 11px; padding: 2px 0; }}
        .popup-carrier {{ display: flex; align-items: center; }}
        .popup-color {{ width: 10px; height: 10px; margin-right: 5px; border-radius: 2px; }}
        .legend {{
            background: white;
            padding: 10px;
            border-radius: 5px;
            box-shadow: 0 1px 5px rgba(0,0,0,0.4);
        }}
        .legend-title {{ font-weight: bold; margin-bottom: 5px; font-size: 12px; }}
        .legend-item {{ display: flex; align-items: center; font-size: 11px; margin: 3px 0; }}
        .legend-color {{ width: 20px; height: 3px; margin-right: 5px; }}
    </style>
</head>
<body>
    <div id="sidebar">
        <h2>eGon2025 Grid Map</h2>

        <div class="stats">
            <div class="stats-row"><span>Buses:</span><span id="stat-buses">0</span></div>
            <div class="stats-row"><span>Lines:</span><span id="stat-lines">0</span></div>
            <div class="stats-row"><span>Generation:</span><span id="stat-gen">0 GW</span></div>
            <div class="stats-row"><span>Load:</span><span id="stat-load">0 GW</span></div>
            <div class="stats-row"><span>Storage:</span><span id="stat-storage">0 GW</span></div>
        </div>

        <h3>Voltage Levels</h3>
        <div class="filter-group" id="voltage-filters">
            <div class="filter-item">
                <input type="checkbox" id="v110" checked>
                <div class="color-box voltage-110"></div>
                <label for="v110">110 kV</label>
            </div>
            <div class="filter-item">
                <input type="checkbox" id="v220" checked>
                <div class="color-box voltage-220"></div>
                <label for="v220">220 kV</label>
            </div>
            <div class="filter-item">
                <input type="checkbox" id="v380" checked>
                <div class="color-box voltage-380"></div>
                <label for="v380">380 kV</label>
            </div>
        </div>
        <div class="btn-group">
            <button class="btn" onclick="selectAllVoltages()">All</button>
            <button class="btn" onclick="selectNoVoltages()">None</button>
        </div>

        <h3>Generation Technologies</h3>
        <div class="filter-group" id="gen-filters"></div>
        <div class="btn-group">
            <button class="btn" onclick="selectAllGen()">All</button>
            <button class="btn" onclick="selectNoGen()">None</button>
        </div>

        <h3>Display Options</h3>
        <div class="filter-group">
            <div class="filter-item">
                <input type="checkbox" id="show-lines" checked>
                <label for="show-lines">Show Lines</label>
            </div>
            <div class="filter-item">
                <input type="checkbox" id="show-gen-circles" checked>
                <label for="show-gen-circles">Show Generation Circles</label>
            </div>
            <div class="filter-item">
                <input type="checkbox" id="show-load-circles" checked>
                <label for="show-load-circles">Show Load Circles</label>
            </div>
            <div class="filter-item">
                <input type="checkbox" id="show-storage-circles" checked>
                <label for="show-storage-circles">Show Storage Circles</label>
            </div>
        </div>
    </div>

    <div id="map"></div>

    <script>
        // Data
        const buses = {json.dumps(bus_list)};
        const lines = {json.dumps(line_list)};
        const carrierColors = {json.dumps(carrier_colors)};
        const voltageColors = {json.dumps(voltage_colors)};
        const allCarriers = {json.dumps(all_carriers)};

        // Map setup
        const map = L.map('map').setView([51.2, 10.4], 6);
        L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
            attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
            maxZoom: 19
        }}).addTo(map);

        // Layer groups
        const lineLayers = {{}};
        const busLayers = {{}};
        const genCircleLayers = {{}};
        const loadCircleLayers = {{}};
        const storageCircleLayers = {{}};

        // Initialize voltage layer groups
        [110, 220, 380].forEach(v => {{
            lineLayers[v] = L.layerGroup().addTo(map);
            busLayers[v] = L.layerGroup().addTo(map);
            genCircleLayers[v] = L.layerGroup().addTo(map);
            loadCircleLayers[v] = L.layerGroup().addTo(map);
            storageCircleLayers[v] = L.layerGroup().addTo(map);
        }});

        // Active filters
        let activeVoltages = new Set([110, 220, 380]);
        let activeCarriers = new Set(allCarriers);

        // Create carrier filter checkboxes
        const genFiltersDiv = document.getElementById('gen-filters');
        allCarriers.forEach(carrier => {{
            const color = carrierColors[carrier] || '#888';
            const div = document.createElement('div');
            div.className = 'filter-item';
            div.innerHTML = `
                <input type="checkbox" id="gen-${{carrier}}" checked>
                <div class="color-box" style="background-color: ${{color}}"></div>
                <label for="gen-${{carrier}}">${{carrier}}</label>
            `;
            genFiltersDiv.appendChild(div);

            document.getElementById(`gen-${{carrier}}`).addEventListener('change', function() {{
                if (this.checked) {{
                    activeCarriers.add(carrier);
                }} else {{
                    activeCarriers.delete(carrier);
                }}
                updateCircles();
                updateStats();
            }});
        }});

        // Voltage filter listeners
        [110, 220, 380].forEach(v => {{
            document.getElementById(`v${{v}}`).addEventListener('change', function() {{
                if (this.checked) {{
                    activeVoltages.add(v);
                    map.addLayer(lineLayers[v]);
                    map.addLayer(busLayers[v]);
                    map.addLayer(genCircleLayers[v]);
                    map.addLayer(loadCircleLayers[v]);
                    map.addLayer(storageCircleLayers[v]);
                }} else {{
                    activeVoltages.delete(v);
                    map.removeLayer(lineLayers[v]);
                    map.removeLayer(busLayers[v]);
                    map.removeLayer(genCircleLayers[v]);
                    map.removeLayer(loadCircleLayers[v]);
                    map.removeLayer(storageCircleLayers[v]);
                }}
                updateStats();
            }});
        }});

        // Display option listeners
        document.getElementById('show-lines').addEventListener('change', function() {{
            activeVoltages.forEach(v => {{
                if (this.checked) map.addLayer(lineLayers[v]);
                else map.removeLayer(lineLayers[v]);
            }});
        }});

        document.getElementById('show-gen-circles').addEventListener('change', function() {{
            activeVoltages.forEach(v => {{
                if (this.checked) map.addLayer(genCircleLayers[v]);
                else map.removeLayer(genCircleLayers[v]);
            }});
        }});

        document.getElementById('show-load-circles').addEventListener('change', function() {{
            activeVoltages.forEach(v => {{
                if (this.checked) map.addLayer(loadCircleLayers[v]);
                else map.removeLayer(loadCircleLayers[v]);
            }});
        }});

        document.getElementById('show-storage-circles').addEventListener('change', function() {{
            activeVoltages.forEach(v => {{
                if (this.checked) map.addLayer(storageCircleLayers[v]);
                else map.removeLayer(storageCircleLayers[v]);
            }});
        }});

        // Helper functions
        function selectAllVoltages() {{
            [110, 220, 380].forEach(v => {{
                document.getElementById(`v${{v}}`).checked = true;
                activeVoltages.add(v);
                map.addLayer(lineLayers[v]);
                map.addLayer(busLayers[v]);
                map.addLayer(genCircleLayers[v]);
                map.addLayer(loadCircleLayers[v]);
                map.addLayer(storageCircleLayers[v]);
            }});
            updateStats();
        }}

        function selectNoVoltages() {{
            [110, 220, 380].forEach(v => {{
                document.getElementById(`v${{v}}`).checked = false;
                activeVoltages.delete(v);
                map.removeLayer(lineLayers[v]);
                map.removeLayer(busLayers[v]);
                map.removeLayer(genCircleLayers[v]);
                map.removeLayer(loadCircleLayers[v]);
                map.removeLayer(storageCircleLayers[v]);
            }});
            updateStats();
        }}

        function selectAllGen() {{
            allCarriers.forEach(c => {{
                document.getElementById(`gen-${{c}}`).checked = true;
                activeCarriers.add(c);
            }});
            updateCircles();
            updateStats();
        }}

        function selectNoGen() {{
            allCarriers.forEach(c => {{
                document.getElementById(`gen-${{c}}`).checked = false;
                activeCarriers.delete(c);
            }});
            updateCircles();
            updateStats();
        }}

        function getFilteredGen(bus) {{
            let total = 0;
            for (const [carrier, cap] of Object.entries(bus.gen)) {{
                if (activeCarriers.has(carrier)) {{
                    total += cap;
                }}
            }}
            return total;
        }}

        function createPopupContent(bus) {{
            let html = `<b>Bus ${{bus.id}}</b> (${{bus.v_nom}} kV)<br>`;

            // Generation section
            const genEntries = Object.entries(bus.gen).filter(([c, v]) => v > 0);
            if (genEntries.length > 0) {{
                html += `<div class="popup-section"><h4>Generation (${{bus.total_gen.toFixed(1)}} MW)</h4>`;
                genEntries.sort((a, b) => b[1] - a[1]).forEach(([carrier, cap]) => {{
                    const color = carrierColors[carrier] || '#888';
                    html += `<div class="popup-row">
                        <span class="popup-carrier"><span class="popup-color" style="background:${{color}}"></span>${{carrier}}</span>
                        <span>${{cap.toFixed(1)}} MW</span>
                    </div>`;
                }});
                html += '</div>';
            }}

            // Storage section
            const storageEntries = Object.entries(bus.storage).filter(([c, v]) => v > 0);
            if (storageEntries.length > 0) {{
                html += `<div class="popup-section"><h4>Storage (${{bus.total_storage.toFixed(1)}} MW)</h4>`;
                storageEntries.sort((a, b) => b[1] - a[1]).forEach(([carrier, cap]) => {{
                    const color = carrierColors[carrier] || '#888';
                    html += `<div class="popup-row">
                        <span class="popup-carrier"><span class="popup-color" style="background:${{color}}"></span>${{carrier}}</span>
                        <span>${{cap.toFixed(1)}} MW</span>
                    </div>`;
                }});
                html += '</div>';
            }}

            // Load section
            const loadEntries = Object.entries(bus.load).filter(([c, v]) => v > 0);
            if (loadEntries.length > 0) {{
                html += `<div class="popup-section"><h4>Load (${{bus.total_load.toFixed(1)}} MW)</h4>`;
                loadEntries.sort((a, b) => b[1] - a[1]).forEach(([type, load]) => {{
                    html += `<div class="popup-row">
                        <span>${{type}}</span>
                        <span>${{load.toFixed(1)}} MW</span>
                    </div>`;
                }});
                html += '</div>';
            }}

            return html;
        }}

        // Calculate circle radius based on capacity (MW)
        function getRadius(capacity_mw, type) {{
            if (capacity_mw <= 0) return 0;
            // Scale: sqrt for better visual representation
            const base = type === 'gen' ? 50 : (type === 'load' ? 40 : 30);
            return Math.sqrt(capacity_mw) * base;
        }}

        // Draw lines
        lines.forEach(line => {{
            const v = line.v_nom;
            const color = voltageColors[v] || '#888';
            const weight = v === 380 ? 2.5 : (v === 220 ? 2 : 1.5);

            const polyline = L.polyline(line.coords, {{
                color: color,
                weight: weight,
                opacity: 0.7
            }});
            polyline.bindTooltip(`Line ${{line.id}}<br>${{v}} kV<br>${{line.s_nom}} MVA<br>${{line.length.toFixed(1)}} km`);
            lineLayers[v].addLayer(polyline);
        }});

        // Store circle references for updates
        const busCircleRefs = {{}};

        // Draw buses and circles
        buses.forEach(bus => {{
            const v = bus.v_nom;
            const color = voltageColors[v] || '#888';

            // Bus marker (small circle)
            const marker = L.circleMarker([bus.lat, bus.lon], {{
                radius: 3,
                fillColor: color,
                color: '#333',
                weight: 1,
                opacity: 1,
                fillOpacity: 0.8
            }});
            marker.bindPopup(createPopupContent(bus), {{maxWidth: 300}});
            busLayers[v].addLayer(marker);

            // Generation circle
            const genCap = getFilteredGen(bus);
            if (genCap > 0) {{
                const genCircle = L.circle([bus.lat, bus.lon], {{
                    radius: getRadius(genCap, 'gen'),
                    fillColor: '#FFD700',
                    color: '#B8860B',
                    weight: 1,
                    opacity: 0.6,
                    fillOpacity: 0.3
                }});
                genCircle.bindPopup(createPopupContent(bus), {{maxWidth: 300}});
                genCircleLayers[v].addLayer(genCircle);

                if (!busCircleRefs[bus.id]) busCircleRefs[bus.id] = {{}};
                busCircleRefs[bus.id].gen = genCircle;
                busCircleRefs[bus.id].bus = bus;
            }}

            // Load circle
            if (bus.total_load > 0) {{
                const loadCircle = L.circle([bus.lat, bus.lon], {{
                    radius: getRadius(bus.total_load, 'load'),
                    fillColor: '#FF6B6B',
                    color: '#C0392B',
                    weight: 1,
                    opacity: 0.6,
                    fillOpacity: 0.3
                }});
                loadCircle.bindPopup(createPopupContent(bus), {{maxWidth: 300}});
                loadCircleLayers[v].addLayer(loadCircle);
            }}

            // Storage circle
            if (bus.total_storage > 0) {{
                const storageCircle = L.circle([bus.lat, bus.lon], {{
                    radius: getRadius(bus.total_storage, 'storage'),
                    fillColor: '#9370DB',
                    color: '#663399',
                    weight: 1,
                    opacity: 0.6,
                    fillOpacity: 0.3
                }});
                storageCircle.bindPopup(createPopupContent(bus), {{maxWidth: 300}});
                storageCircleLayers[v].addLayer(storageCircle);
            }}
        }});

        // Update circles when carrier filter changes
        function updateCircles() {{
            Object.values(busCircleRefs).forEach(ref => {{
                if (ref.gen && ref.bus) {{
                    const newCap = getFilteredGen(ref.bus);
                    ref.gen.setRadius(getRadius(newCap, 'gen'));
                }}
            }});
        }}

        // Update statistics
        function updateStats() {{
            let totalBuses = 0;
            let totalLines = 0;
            let totalGen = 0;
            let totalLoad = 0;
            let totalStorage = 0;

            buses.forEach(bus => {{
                if (activeVoltages.has(bus.v_nom)) {{
                    totalBuses++;
                    totalGen += getFilteredGen(bus);
                    totalLoad += bus.total_load;
                    totalStorage += bus.total_storage;
                }}
            }});

            lines.forEach(line => {{
                if (activeVoltages.has(line.v_nom)) {{
                    totalLines++;
                }}
            }});

            document.getElementById('stat-buses').textContent = totalBuses.toLocaleString();
            document.getElementById('stat-lines').textContent = totalLines.toLocaleString();
            document.getElementById('stat-gen').textContent = (totalGen / 1000).toFixed(1) + ' GW';
            document.getElementById('stat-load').textContent = (totalLoad / 1000).toFixed(1) + ' GW';
            document.getElementById('stat-storage').textContent = (totalStorage / 1000).toFixed(1) + ' GW';
        }}

        // Add legend
        const legend = L.control({{position: 'bottomright'}});
        legend.onAdd = function(map) {{
            const div = L.DomUtil.create('div', 'legend');
            div.innerHTML = `
                <div class="legend-title">Voltage Levels</div>
                <div class="legend-item"><div class="legend-color" style="background:#d62728"></div>380 kV</div>
                <div class="legend-item"><div class="legend-color" style="background:#1f77b4"></div>220 kV</div>
                <div class="legend-item"><div class="legend-color" style="background:#2ca02c"></div>110 kV</div>
                <div class="legend-title" style="margin-top:10px">Circles</div>
                <div class="legend-item"><div class="legend-color" style="background:#FFD700; height:10px; width:10px; border-radius:50%"></div>Generation</div>
                <div class="legend-item"><div class="legend-color" style="background:#FF6B6B; height:10px; width:10px; border-radius:50%"></div>Load</div>
                <div class="legend-item"><div class="legend-color" style="background:#9370DB; height:10px; width:10px; border-radius:50%"></div>Storage</div>
            `;
            return div;
        }};
        legend.addTo(map);

        // Initial stats update
        updateStats();
    </script>
</body>
</html>
'''

    return html_content


def main():
    print("Creating interactive grid map...")

    engine = create_engine(DB_URI)

    # Load data
    print("Loading grid data from database...")
    buses, lines, generators, loads, storage = load_grid_data(engine)

    print(f"  Buses: {len(buses)}")
    print(f"  Lines: {len(lines)}")
    print(f"  Generator entries: {len(generators)}")
    print(f"  Load entries: {len(loads)}")
    print(f"  Storage entries: {len(storage)}")

    # Prepare bus data
    print("Preparing bus aggregations...")
    bus_data, gen_breakdown, load_breakdown, storage_breakdown, carriers = prepare_bus_data(
        buses, generators, loads, storage
    )

    # Generate HTML
    print("Generating HTML map...")
    html_content = generate_html_map(
        bus_data, lines, gen_breakdown, load_breakdown, storage_breakdown, carriers
    )

    # Save
    output_path = 'egon2025_grid_map.html'
    with open(output_path, 'w') as f:
        f.write(html_content)

    print(f"\nMap saved to: {output_path}")
    print("Open in a web browser to view the interactive map.")


if __name__ == '__main__':
    main()
