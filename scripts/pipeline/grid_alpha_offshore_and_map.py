#!/usr/bin/env python3
"""
Grid Alpha: Add MaStR offshore wind + generate interactive HTML map.

1. Remove synthetic offwind generators (IDs 57-66) from grid_alpha
2. Match 81 MaStR offshore wind SELs to 10 offshore bus nodes by proximity
3. Insert real MaStR offshore generators
4. Generate interactive HTML map of all generation by bus

Usage:
    python scripts/grid_alpha_offshore_and_map.py [--apply] [--map-only]
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
ALPHA = 'grid_alpha'

# Offshore wind bus IDs (from v6 HVDC link endpoints)
OFFSHORE_BUS_IDS = [200104, 200105, 200106, 200107, 200108,
                    200109, 200110, 200111, 200112, 200113]

# Synthetic offwind generator IDs to remove
SYNTHETIC_OFFWIND_IDS = list(range(57, 67))  # 57-66


def add_offshore_wind(engine, dry_run=True):
    """Replace synthetic offwind generators with MaStR offshore wind."""
    print("\n" + "=" * 60)
    print("OFFSHORE WIND: MaStR -> Grid Alpha")
    print("=" * 60)

    bus_ids_str = ','.join(str(b) for b in OFFSHORE_BUS_IDS)
    offshore_buses = pd.read_sql(f"""
        SELECT bus_id, x as lon, y as lat, v_nom
        FROM grid.egon_etrago_bus
        WHERE scn_name = '{ALPHA}' AND bus_id IN ({bus_ids_str})
    """, engine)
    print(f"  Offshore bus nodes: {len(offshore_buses)}")

    offshore_sels = pd.read_sql("""
        SELECT w."LokationMastrNummer" as sel,
               COUNT(*) as n_turbines,
               SUM(w."Nettonennleistung") / 1000.0 as p_nom_mw,
               AVG(w."Laengengrad") as lon,
               AVG(w."Breitengrad") as lat
        FROM mastr.wind_extended w
        WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
          AND w."Nettonennleistung" > 0
          AND w."WindAnLandOderAufSee" = 'Windkraft auf See'
        GROUP BY w."LokationMastrNummer"
        ORDER BY p_nom_mw DESC
    """, engine)
    print(f"  MaStR offshore SELs: {len(offshore_sels)}, "
          f"total: {offshore_sels['p_nom_mw'].sum()/1000:.2f} GW")

    from scipy.spatial import cKDTree
    KM_PER_DEG_LON, KM_PER_DEG_LAT = 71.5, 111.0

    bus_coords = np.column_stack([
        offshore_buses['lon'].values * KM_PER_DEG_LON,
        offshore_buses['lat'].values * KM_PER_DEG_LAT
    ])
    tree = cKDTree(bus_coords)

    matched_bus_ids, distances = [], []
    for _, sel in offshore_sels.iterrows():
        if pd.isna(sel['lon']) or pd.isna(sel['lat']):
            matched_bus_ids.append(None); distances.append(None); continue
        q = np.array([sel['lon'] * KM_PER_DEG_LON, sel['lat'] * KM_PER_DEG_LAT])
        dist, idx = tree.query(q, k=1)
        matched_bus_ids.append(int(offshore_buses.iloc[idx]['bus_id']))
        distances.append(float(dist))

    offshore_sels['bus_id'] = matched_bus_ids

    matched = offshore_sels[offshore_sels['bus_id'].notna()]
    agg = matched.groupby('bus_id').agg(
        p_nom_mw=('p_nom_mw', 'sum'),
        n_turbines=('n_turbines', 'sum'),
        n_sels=('sel', 'count'),
    ).reset_index()

    print(f"\n  Allocation to offshore buses:")
    for _, row in agg.iterrows():
        print(f"    Bus {int(row['bus_id'])}: {row['p_nom_mw']:.0f} MW "
              f"({int(row['n_turbines'])} turbines, {int(row['n_sels'])} farms)")
    print(f"  Total: {agg['p_nom_mw'].sum()/1000:.2f} GW")

    if dry_run:
        print("\n  [DRY RUN]")
        return agg

    # Delete synthetic offwind generators and timeseries
    with engine.begin() as conn:
        ids_str = ','.join(str(i) for i in SYNTHETIC_OFFWIND_IDS)
        r = conn.execute(text(
            f"DELETE FROM grid.egon_etrago_generator_timeseries "
            f"WHERE scn_name = '{ALPHA}' AND generator_id IN ({ids_str})"
        ))
        print(f"  Deleted {r.rowcount} synthetic offwind timeseries rows")
        r = conn.execute(text(
            f"DELETE FROM grid.egon_etrago_generator "
            f"WHERE scn_name = '{ALPHA}' AND generator_id IN ({ids_str})"
        ))
        print(f"  Deleted {r.rowcount} synthetic offwind generators")

    # Also delete any existing offwind gens (from previous runs)
    with engine.begin() as conn:
        r = conn.execute(text(
            f"DELETE FROM grid.egon_etrago_generator "
            f"WHERE scn_name = '{ALPHA}' AND carrier = 'offwind'"
        ))
        if r.rowcount > 0:
            print(f"  Deleted {r.rowcount} remaining offwind generators")

    with engine.begin() as conn:
        max_id = conn.execute(text(
            f"SELECT MAX(generator_id) FROM grid.egon_etrago_generator "
            f"WHERE scn_name = '{ALPHA}'"
        )).scalar() or 0
    next_id = int(max_id) + 1

    records = pd.DataFrame({
        'scn_name': ALPHA,
        'generator_id': range(next_id, next_id + len(agg)),
        'bus': agg['bus_id'].astype(int).values,
        'control': 'PQ', 'type': '', 'carrier': 'offwind',
        'p_nom': agg['p_nom_mw'].astype(float).values,
        'p_nom_extendable': False, 'p_nom_min': 0.0, 'p_nom_max': float('inf'),
        'p_min_pu': 0.0, 'p_max_pu': 1.0, 'p_set': None, 'q_set': None,
        'sign': 1.0, 'marginal_cost': 0.0, 'build_year': 0,
        'lifetime': float('inf'), 'capital_cost': 0.0, 'efficiency': 1.0,
        'committable': False, 'start_up_cost': 0.0, 'shut_down_cost': 0.0,
        'min_up_time': 0, 'min_down_time': 0, 'up_time_before': 0,
        'down_time_before': 0, 'ramp_limit_up': None, 'ramp_limit_down': None,
        'ramp_limit_start_up': 1.0, 'ramp_limit_shut_down': 1.0,
        'e_nom_max': float('inf'),
    })
    for col in records.columns:
        if records[col].dtype == np.float64:
            records[col] = records[col].astype(object).where(records[col].notna(), None)
        elif records[col].dtype == np.int64:
            records[col] = records[col].astype(int)

    records.to_sql('egon_etrago_generator', engine, schema='grid',
                   if_exists='append', index=False)
    print(f"  Inserted {len(records)} MaStR offshore wind generators")

    # Save offshore metadata for map
    offshore_meta = agg[['bus_id', 'p_nom_mw', 'n_turbines']].copy()
    offshore_meta['carrier'] = 'offwind'
    offshore_meta['is_aggregated'] = False
    offshore_meta.rename(columns={'p_nom_mw': 'p_nom_mw', 'n_turbines': 'n_units'}, inplace=True)
    offshore_meta.to_csv('results/grid_alpha_offwind_metadata.csv', index=False)

    with engine.begin() as conn:
        result = conn.execute(text(f"""
            SELECT bus, p_nom FROM grid.egon_etrago_generator
            WHERE scn_name = '{ALPHA}' AND carrier = 'offwind' ORDER BY bus
        """)).fetchall()
        total = sum(r[1] for r in result)
        print(f"  Verification: {len(result)} offwind gens, {total/1000:.2f} GW total")

    return agg


def generate_html_map(engine, output_path):
    """Generate interactive HTML map with voltage-colored grid lines."""
    print("\n" + "=" * 60)
    print("GENERATING INTERACTIVE HTML MAP")
    print("=" * 60)

    # Load data
    buses = pd.read_sql(f"SELECT bus_id, x as lon, y as lat, v_nom, country "
                        f"FROM grid.egon_etrago_bus WHERE scn_name = '{ALPHA}'", engine)
    generators = pd.read_sql(f"""
        SELECT g.generator_id, g.bus, g.carrier, g.p_nom
        FROM grid.egon_etrago_generator g WHERE g.scn_name = '{ALPHA}'
    """, engine)
    storage = pd.read_sql(f"""
        SELECT s.storage_id, s.bus, s.carrier, s.p_nom
        FROM grid.egon_etrago_storage s WHERE s.scn_name = '{ALPHA}'
    """, engine)
    loads = pd.read_sql(f"""
        SELECT l.load_id, l.bus, l.carrier, l.p_set
        FROM grid.egon_etrago_load l WHERE l.scn_name = '{ALPHA}'
    """, engine)
    lines = pd.read_sql(f"""
        SELECT l.line_id, l.bus0, l.bus1, l.s_nom,
               b0.v_nom as v_nom0, b1.v_nom as v_nom1
        FROM grid.egon_etrago_line l
        JOIN grid.egon_etrago_bus b0 ON b0.bus_id = l.bus0 AND b0.scn_name = l.scn_name
        JOIN grid.egon_etrago_bus b1 ON b1.bus_id = l.bus1 AND b1.scn_name = l.scn_name
        WHERE l.scn_name = '{ALPHA}'
    """, engine)
    links = pd.read_sql(f"SELECT link_id, bus0, bus1, p_nom "
                        f"FROM grid.egon_etrago_link WHERE scn_name = '{ALPHA}'", engine)

    print(f"  Buses: {len(buses)}, Gens: {len(generators)}, Storage: {len(storage)}, "
          f"Loads: {len(loads)}, Lines: {len(lines)}, Links: {len(links)}")

    # Load metadata CSV files for n_units and is_aggregated info
    gen_meta = pd.DataFrame()
    stor_meta = pd.DataFrame()
    offwind_meta = pd.DataFrame()
    for path, target in [('results/grid_alpha_gen_metadata.csv', 'gen'),
                         ('results/grid_alpha_stor_metadata.csv', 'stor'),
                         ('results/grid_alpha_offwind_metadata.csv', 'offwind')]:
        if Path(path).exists():
            df = pd.read_csv(path)
            if target == 'gen':
                gen_meta = df
            elif target == 'stor':
                stor_meta = df
            else:
                offwind_meta = df

    # Build metadata lookup: (bus_id, carrier) -> {n_units, is_aggregated}
    meta_lookup = {}
    for df in [gen_meta, offwind_meta]:
        if len(df) == 0:
            continue
        for _, row in df.iterrows():
            key = (int(row['bus_id']), row['carrier'])
            if key in meta_lookup:
                meta_lookup[key]['n_units'] += int(row.get('n_units', 1))
                meta_lookup[key]['is_aggregated'] = meta_lookup[key]['is_aggregated'] or bool(row.get('is_aggregated', False))
            else:
                meta_lookup[key] = {
                    'n_units': int(row.get('n_units', 1)),
                    'is_aggregated': bool(row.get('is_aggregated', False)),
                }
    stor_meta_lookup = {}
    if len(stor_meta) > 0:
        for _, row in stor_meta.iterrows():
            key = (int(row['bus_id']), row['carrier'])
            if key in stor_meta_lookup:
                stor_meta_lookup[key]['n_units'] += int(row.get('n_units', 1))
                stor_meta_lookup[key]['is_aggregated'] = stor_meta_lookup[key]['is_aggregated'] or bool(row.get('is_aggregated', False))
            else:
                stor_meta_lookup[key] = {
                    'n_units': int(row.get('n_units', 1)),
                    'is_aggregated': bool(row.get('is_aggregated', False)),
                }

    bus_lookup = buses.set_index('bus_id')

    # Aggregate DB data by (bus, carrier)
    gen_by_bus = generators.groupby(['bus', 'carrier']).agg(
        p_nom_total=('p_nom', 'sum'),
        n_db_entries=('generator_id', 'count'),
    ).reset_index()
    storage_by_bus = storage.groupby(['bus', 'carrier']).agg(
        p_nom_total=('p_nom', 'sum'),
        n_db_entries=('storage_id', 'count'),
    ).reset_index()
    load_by_bus = loads.groupby(['bus', 'carrier']).agg(
        p_set_total=('p_set', 'sum'),
        n_loads=('load_id', 'count'),
    ).reset_index()

    carrier_colors = {
        'solar': '#FFD700', 'onwind': '#4169E1', 'offwind': '#1E90FF',
        'gas': '#FF6347', 'coal': '#2F4F4F', 'lignite': '#8B4513',
        'oil': '#B22222', 'waste': '#808080', 'hydrogen': '#00CED1',
        'other': '#DEB887', 'biogas': '#32CD32', 'biomass': '#228B22',
        'run_of_river': '#4682B4', 'reservoir': '#20B2AA',
        'battery': '#9370DB', 'pumped_hydro': '#6A5ACD',
    }

    # Build bus features
    active_buses = (set(generators['bus']) | set(storage['bus']) | set(loads['bus']))
    bus_features = []

    for bus_id in active_buses:
        if bus_id not in bus_lookup.index:
            continue
        brow = bus_lookup.loc[bus_id]
        lon, lat = float(brow['lon']), float(brow['lat'])
        v_nom = float(brow['v_nom'])
        country = str(brow['country'])

        bus_gens = gen_by_bus[gen_by_bus['bus'] == bus_id]
        gen_entries = []
        total_gen_mw = 0
        for _, g in bus_gens.iterrows():
            p = float(g['p_nom_total'])
            carrier = g['carrier']
            key = (int(bus_id), carrier)
            meta = meta_lookup.get(key, {})
            n_units = meta.get('n_units', int(g['n_db_entries']))
            is_agg = meta.get('is_aggregated', False)
            gen_entries.append({
                'carrier': carrier,
                'p_nom_mw': round(p, 1),
                'n_units': n_units,
                'aggregated': is_agg,
                'color': carrier_colors.get(carrier, '#999'),
            })
            total_gen_mw += p

        bus_stor = storage_by_bus[storage_by_bus['bus'] == bus_id]
        stor_entries = []
        total_stor_mw = 0
        for _, s in bus_stor.iterrows():
            p = float(s['p_nom_total'])
            carrier = s['carrier']
            key = (int(bus_id), carrier)
            meta = stor_meta_lookup.get(key, {})
            n_units = meta.get('n_units', int(s['n_db_entries']))
            is_agg = meta.get('is_aggregated', False)
            stor_entries.append({
                'carrier': carrier,
                'p_nom_mw': round(p, 1),
                'n_units': n_units,
                'aggregated': is_agg,
                'color': carrier_colors.get(carrier, '#999'),
            })
            total_stor_mw += p

        bus_loads = load_by_bus[load_by_bus['bus'] == bus_id]
        load_entries = []
        total_load_mw = 0
        for _, ld in bus_loads.iterrows():
            load_entries.append({
                'carrier': ld['carrier'],
                'p_set_mw': round(float(ld['p_set_total']), 1),
                'n_loads': int(ld['n_loads']),
            })
            total_load_mw += float(ld['p_set_total'])

        if gen_entries:
            marker_color = max(gen_entries, key=lambda x: x['p_nom_mw'])['color']
        elif stor_entries:
            marker_color = max(stor_entries, key=lambda x: x['p_nom_mw'])['color']
        else:
            marker_color = '#999'

        bus_features.append({
            'bus_id': int(bus_id), 'lon': lon, 'lat': lat,
            'v_nom': v_nom, 'country': country,
            'total_gen_mw': round(total_gen_mw, 1),
            'total_stor_mw': round(total_stor_mw, 1),
            'total_load_mw': round(total_load_mw, 1),
            'generators': gen_entries, 'storage': stor_entries,
            'loads': load_entries, 'marker_color': marker_color,
        })

    # Build line features with voltage info
    line_features_110 = []
    line_features_220 = []
    line_features_380 = []

    for _, ln in lines.iterrows():
        b0, b1 = int(ln['bus0']), int(ln['bus1'])
        if b0 not in bus_lookup.index or b1 not in bus_lookup.index:
            continue
        r0, r1 = bus_lookup.loc[b0], bus_lookup.loc[b1]
        coords = [[float(r0['lat']), float(r0['lon'])],
                   [float(r1['lat']), float(r1['lon'])]]
        v = max(float(ln['v_nom0']), float(ln['v_nom1']))
        if v >= 380:
            line_features_380.append(coords)
        elif v >= 220:
            line_features_220.append(coords)
        else:
            line_features_110.append(coords)

    link_features = []
    for _, lk in links.iterrows():
        b0, b1 = int(lk['bus0']), int(lk['bus1'])
        if b0 in bus_lookup.index and b1 in bus_lookup.index:
            r0, r1 = bus_lookup.loc[b0], bus_lookup.loc[b1]
            link_features.append([[float(r0['lat']), float(r0['lon'])],
                                  [float(r1['lat']), float(r1['lon'])]])

    print(f"  Active buses: {len(bus_features)}")
    print(f"  Lines: 110kV={len(line_features_110)}, 220kV={len(line_features_220)}, "
          f"380kV={len(line_features_380)}, HVDC={len(link_features)}")

    html = build_html(bus_features, line_features_110, line_features_220,
                      line_features_380, link_features,
                      json.dumps(carrier_colors))

    with open(output_path, 'w') as f:
        f.write(html)
    print(f"  Wrote map to {output_path}")


def build_html(bus_features, lines_110, lines_220, lines_380, links, colors_json):
    bus_json = json.dumps(bus_features)
    l110_json = json.dumps(lines_110)
    l220_json = json.dumps(lines_220)
    l380_json = json.dumps(lines_380)
    link_json = json.dumps(links)

    return f"""<!DOCTYPE html>
<html>
<head>
<title>Grid Alpha - Generation Capacity Map</title>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; }}
  #map {{ width: 100%; height: 100vh; }}

  .panel {{
    position: absolute; z-index: 1000;
    background: rgba(255,255,255,0.96); border-radius: 10px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.15); backdrop-filter: blur(8px);
    font-size: 13px;
  }}

  .info-panel {{
    top: 12px; right: 12px; width: 360px; padding: 16px;
    max-height: calc(100vh - 24px); overflow-y: auto;
  }}
  .info-panel h3 {{ margin: 0 0 6px; font-size: 16px; font-weight: 700; }}
  .info-panel .subtitle {{ color: #666; margin-bottom: 10px; font-size: 12px; }}
  .info-panel h4 {{
    margin: 12px 0 4px; font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.5px; color: #888; font-weight: 600;
  }}

  .carrier-row {{
    display: flex; align-items: center; padding: 4px 0;
    border-bottom: 1px solid #f0f0f0;
  }}
  .carrier-dot {{
    width: 10px; height: 10px; border-radius: 50%;
    margin-right: 8px; flex-shrink: 0; border: 1px solid rgba(0,0,0,0.15);
  }}
  .carrier-name {{ flex: 1; font-size: 13px; }}
  .carrier-cap {{ font-weight: 700; text-align: right; min-width: 80px; font-size: 13px; }}
  .carrier-count {{
    color: #888; font-size: 11px; margin-left: 8px; min-width: 70px; text-align: right;
  }}
  .agg-tag {{
    background: #e8f4fd; color: #1565c0; font-size: 9px; font-weight: 700;
    padding: 1px 5px; border-radius: 3px; margin-left: 4px;
    letter-spacing: 0.5px;
  }}
  .total-row {{
    display: flex; padding: 6px 0 2px; font-weight: 700; font-size: 14px;
    border-top: 2px solid #333; margin-top: 6px;
  }}

  .stats-panel {{
    top: 12px; left: 12px; padding: 14px 18px;
  }}
  .stats-panel h4 {{ margin: 0 0 8px; font-size: 14px; font-weight: 700; }}
  .stat-row {{ display: flex; justify-content: space-between; padding: 2px 0; }}
  .stat-label {{ color: #555; }}
  .stat-value {{ font-weight: 600; margin-left: 16px; }}

  .legend-panel {{
    bottom: 12px; left: 12px; padding: 12px 16px;
  }}
  .legend-panel h4 {{ margin: 0 0 6px; font-size: 12px; font-weight: 700; }}
  .legend-item {{ display: flex; align-items: center; padding: 2px 0; font-size: 12px; }}
  .legend-dot {{
    width: 10px; height: 10px; border-radius: 50%;
    margin-right: 8px; flex-shrink: 0; border: 1px solid rgba(0,0,0,0.15);
  }}
  .legend-sep {{ border-top: 1px solid #eee; margin: 4px 0; }}
  .legend-line {{
    display: flex; align-items: center; padding: 2px 0; font-size: 12px;
  }}
  .legend-line-sample {{
    width: 24px; height: 0; margin-right: 8px; flex-shrink: 0;
  }}

  /* Pulse animation for hovered marker */
  @keyframes pulse-ring {{
    0% {{ transform: scale(1); opacity: 0.6; }}
    100% {{ transform: scale(2.2); opacity: 0; }}
  }}
</style>
</head>
<body>
<div id="map"></div>
<div class="panel info-panel" id="info">
  <h3>Grid Alpha</h3>
  <div class="subtitle">Hover over a substation to see installed generation capacity</div>
  <div style="color:#888; font-size:11px; line-height:1.5">
    Circle size reflects total installed capacity.<br>
    <span class="agg-tag">AGG</span> = small MV/LV units aggregated to this
    bus by municipality. Actual plants are spread across the surrounding area.
  </div>
</div>
<div class="panel stats-panel" id="stats"></div>
<div class="panel legend-panel" id="legend"></div>

<script>
const busData = {bus_json};
const lines110 = {l110_json};
const lines220 = {l220_json};
const lines380 = {l380_json};
const linksData = {link_json};
const carrierColors = {colors_json};

const carrierNames = {{
  'solar':'Solar PV','onwind':'Wind Onshore','offwind':'Wind Offshore',
  'gas':'Natural Gas','coal':'Hard Coal','lignite':'Lignite','oil':'Oil',
  'waste':'Waste','hydrogen':'Hydrogen','other':'Other Thermal',
  'biogas':'Biogas','biomass':'Solid Biomass',
  'run_of_river':'Run-of-River','reservoir':'Reservoir Hydro',
  'battery':'Battery','pumped_hydro':'Pumped Hydro',
}};

const map = L.map('map', {{ zoomControl: false }}).setView([51.2, 10.4], 6);
L.control.zoom({{ position: 'topright' }}).addTo(map);

L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_nolabels/{{z}}/{{x}}/{{y}}@2x.png', {{
  attribution: '&copy; OSM &copy; CARTO', maxZoom: 18,
}}).addTo(map);

// --- Grid lines by voltage ---
function makeLineLayer(data, color, weight, opacity) {{
  const lg = L.layerGroup();
  data.forEach(c => L.polyline(c, {{ color, weight, opacity }}).addTo(lg));
  return lg;
}}

const layer110 = makeLineLayer(lines110, '#3b82f6', 1.0, 0.45);
const layer220 = makeLineLayer(lines220, '#22c55e', 1.8, 0.6);
const layer380 = makeLineLayer(lines380, '#ef4444', 2.5, 0.7);
const layerHVDC = L.layerGroup();
linksData.forEach(c => L.polyline(c, {{
  color: '#a855f7', weight: 2.5, opacity: 0.7, dashArray: '8 5',
}}).addTo(layerHVDC));

layer110.addTo(map);
layer220.addTo(map);
layer380.addTo(map);
layerHVDC.addTo(map);

// --- Format ---
function fmtCap(mw) {{
  if (Math.abs(mw) >= 1000) return (mw/1000).toFixed(2) + ' GW';
  return mw.toFixed(1) + ' MW';
}}
function fmtUnits(n, singular, plural) {{
  if (!plural) plural = singular + 's';
  return n.toLocaleString() + ' ' + (n === 1 ? singular : plural);
}}

// --- Bus markers ---
const markerLayer = L.layerGroup();
const infoPanel = document.getElementById('info');
let pulseMarker = null;

busData.forEach(bus => {{
  const totalMW = bus.total_gen_mw + bus.total_stor_mw;
  if (totalMW <= 0 && bus.total_load_mw <= 0) return;

  // Graduated circle: area proportional to capacity
  const r = Math.max(3, Math.min(28, 1.5 + Math.sqrt(totalMW) * 0.65));

  const circle = L.circleMarker([bus.lat, bus.lon], {{
    radius: r,
    fillColor: bus.marker_color,
    color: 'rgba(0,0,0,0.25)',
    weight: 1.5,
    opacity: 1,
    fillOpacity: 0.75,
  }}).addTo(markerLayer);

  circle.on('mouseover', function(e) {{
    // Highlight
    this.setStyle({{ weight: 2.5, color: '#000', fillOpacity: 0.95 }});
    this.setRadius(r + 3);
    this.bringToFront();

    // Pulse ring
    if (pulseMarker) map.removeLayer(pulseMarker);
    pulseMarker = L.circleMarker([bus.lat, bus.lon], {{
      radius: r + 8, fillColor: bus.marker_color, fillOpacity: 0.25,
      color: bus.marker_color, weight: 2, opacity: 0.5,
      className: 'pulse-ring',
    }}).addTo(map);

    // Build tooltip
    let h = '<h3>Bus ' + bus.bus_id + '</h3>';
    h += '<div class="subtitle">' + bus.v_nom + ' kV';
    if (bus.country !== 'DE') h += ' &middot; ' + bus.country;
    h += ' &middot; (' + bus.lon.toFixed(3) + ', ' + bus.lat.toFixed(3) + ')</div>';

    if (bus.generators.length > 0) {{
      h += '<h4>Generation</h4>';
      const sorted = [...bus.generators].sort((a,b) => b.p_nom_mw - a.p_nom_mw);
      sorted.forEach(g => {{
        const name = carrierNames[g.carrier] || g.carrier;
        h += '<div class="carrier-row">';
        h += '<div class="carrier-dot" style="background:'+g.color+'"></div>';
        h += '<span class="carrier-name">'+name;
        if (g.aggregated) h += ' <span class="agg-tag">AGG</span>';
        h += '</span>';
        h += '<span class="carrier-cap">'+fmtCap(g.p_nom_mw)+'</span>';
        h += '<span class="carrier-count">'+fmtUnits(g.n_units, 'unit')+'</span>';
        h += '</div>';
      }});
      h += '<div class="total-row">';
      h += '<span class="carrier-name">Total Generation</span>';
      h += '<span class="carrier-cap">'+fmtCap(bus.total_gen_mw)+'</span>';
      h += '</div>';
    }}

    if (bus.storage.length > 0) {{
      h += '<h4>Storage</h4>';
      bus.storage.forEach(s => {{
        const name = carrierNames[s.carrier] || s.carrier;
        h += '<div class="carrier-row">';
        h += '<div class="carrier-dot" style="background:'+s.color+'"></div>';
        h += '<span class="carrier-name">'+name;
        if (s.aggregated) h += ' <span class="agg-tag">AGG</span>';
        h += '</span>';
        h += '<span class="carrier-cap">'+fmtCap(s.p_nom_mw)+'</span>';
        h += '<span class="carrier-count">'+fmtUnits(s.n_units, 'unit')+'</span>';
        h += '</div>';
      }});
      h += '<div class="total-row">';
      h += '<span class="carrier-name">Total Storage</span>';
      h += '<span class="carrier-cap">'+fmtCap(bus.total_stor_mw)+'</span>';
      h += '</div>';
    }}

    if (bus.loads.length > 0) {{
      h += '<h4>Loads / Export</h4>';
      bus.loads.forEach(ld => {{
        h += '<div class="carrier-row">';
        h += '<span class="carrier-name">'+ld.carrier+'</span>';
        h += '<span class="carrier-cap">'+fmtCap(ld.p_set_mw)+'</span>';
        h += '</div>';
      }});
    }}

    infoPanel.innerHTML = h;
  }});

  circle.on('mouseout', function() {{
    this.setStyle({{ weight: 1.5, color: 'rgba(0,0,0,0.25)', fillOpacity: 0.75 }});
    this.setRadius(r);
    if (pulseMarker) {{ map.removeLayer(pulseMarker); pulseMarker = null; }}
  }});
}});
markerLayer.addTo(map);

// --- Legend ---
const legendDiv = document.getElementById('legend');
const carrierTotals = {{}};
busData.forEach(b => {{
  b.generators.forEach(g => {{ carrierTotals[g.carrier] = (carrierTotals[g.carrier]||0) + g.p_nom_mw; }});
  b.storage.forEach(s => {{ carrierTotals[s.carrier] = (carrierTotals[s.carrier]||0) + s.p_nom_mw; }});
}});
const sortedC = Object.entries(carrierTotals).sort((a,b) => b[1]-a[1]);

let lh = '<h4>Carriers</h4>';
sortedC.forEach(([c, total]) => {{
  const name = carrierNames[c] || c;
  const color = carrierColors[c] || '#999';
  lh += '<div class="legend-item"><div class="legend-dot" style="background:'+color+'"></div>'
      + '<span>'+name+' ('+fmtCap(total)+')</span></div>';
}});
lh += '<div class="legend-sep"></div><h4>Grid Lines</h4>';
lh += '<div class="legend-line"><div class="legend-line-sample" style="border-top:2.5px solid #ef4444"></div>380 kV</div>';
lh += '<div class="legend-line"><div class="legend-line-sample" style="border-top:1.8px solid #22c55e"></div>220 kV</div>';
lh += '<div class="legend-line"><div class="legend-line-sample" style="border-top:1px solid #3b82f6"></div>110 kV</div>';
lh += '<div class="legend-line"><div class="legend-line-sample" style="border-top:2.5px dashed #a855f7"></div>HVDC</div>';
legendDiv.innerHTML = lh;

// --- Stats ---
const statsDiv = document.getElementById('stats');
let tGen=0, tStor=0, tLoad=0, nGB=0, nSB=0;
busData.forEach(b => {{
  tGen += b.total_gen_mw; tStor += b.total_stor_mw; tLoad += b.total_load_mw;
  if (b.total_gen_mw > 0) nGB++;
  if (b.total_stor_mw > 0) nSB++;
}});
statsDiv.innerHTML = '<h4>Grid Alpha</h4>'
  + '<div class="stat-row"><span class="stat-label">Generation</span><span class="stat-value">'+fmtCap(tGen)+'</span></div>'
  + '<div class="stat-row"><span class="stat-label">at buses</span><span class="stat-value">'+nGB.toLocaleString()+'</span></div>'
  + '<div class="stat-row"><span class="stat-label">Storage</span><span class="stat-value">'+fmtCap(tStor)+'</span></div>'
  + '<div class="stat-row"><span class="stat-label">at buses</span><span class="stat-value">'+nSB.toLocaleString()+'</span></div>'
  + '<div class="stat-row"><span class="stat-label">Export/Load</span><span class="stat-value">'+fmtCap(tLoad)+'</span></div>';

// --- Layer control ---
const overlays = {{
  '<span style="color:#ef4444;font-weight:600">380 kV</span>': layer380,
  '<span style="color:#22c55e;font-weight:600">220 kV</span>': layer220,
  '<span style="color:#3b82f6;font-weight:600">110 kV</span>': layer110,
  '<span style="color:#a855f7;font-weight:600">HVDC</span>': layerHVDC,
  'Generation': markerLayer,
}};
L.control.layers(null, overlays, {{ collapsed: false, position: 'bottomright' }}).addTo(map);
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(
        description='Grid Alpha: offshore wind + interactive map'
    )
    parser.add_argument('--apply', action='store_true',
                        help='Write offshore wind to database')
    parser.add_argument('--map-only', action='store_true',
                        help='Only generate the map (skip offshore wind)')
    parser.add_argument('--output', default='results/grid_alpha_map.html',
                        help='Output HTML file path')
    args = parser.parse_args()

    dry_run = not args.apply
    engine = create_engine(DB_URI)

    if not args.map_only:
        add_offshore_wind(engine, dry_run=dry_run)

    generate_html_map(engine, args.output)
    print(f"\n=== Done. Open {args.output} in a browser. ===")


if __name__ == '__main__':
    main()
