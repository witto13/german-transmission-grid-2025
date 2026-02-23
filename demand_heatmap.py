#!/usr/bin/env python3
"""
Electricity Demand Heatmap by Municipality (Gemeinde)
=====================================================

Steps:
  1. Read NUTS-3 household electricity demand from DemandRegio/disaggregator
  2. Use population as proxy for CTS and industry spatial distribution
  3. Scale all sectors to 2025 national totals (BDEW/BNetzA):
       Households: 134 TWh, CTS: 124 TWh, Industry: 190 TWh
  4. Read municipality boundaries from PostGIS (boundaries.vg250_gem)
  5. Distribute NUTS-3 demand to municipalities by area fraction
  6. Read large industrial consumers from mastr.electricity_consumer
  7. Create interactive HTML choropleth heatmap

Output: demand_heatmap.html
"""

import json
import math
import warnings
import numpy as np
import pandas as pd
import geopandas as gpd
from sqlalchemy import create_engine

warnings.filterwarnings('ignore')

# ── Config ──────────────────────────────────────────────────────────────────
DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
DISAGG_DATA = '/root/miniconda3/envs/egon2025/lib/python3.10/site-packages/disaggregator/data_in/regional'

# 2025 national totals (TWh) from BDEW / BNetzA
NATIONAL_HH_TWH = 134.0    # Households
NATIONAL_CTS_TWH = 124.0   # Commercial, Trade, Services
NATIONAL_IND_TWH = 190.0   # Industry
NATIONAL_TOTAL_TWH = NATIONAL_HH_TWH + NATIONAL_CTS_TWH + NATIONAL_IND_TWH  # 448 TWh

print("=" * 60)
print("Electricity Demand Heatmap – Germany 2025")
print("=" * 60)

# ── Step 1: Read NUTS-3 demand data from disaggregator ──────────────────────
print("\n[Step 1] Reading NUTS-3 data from DemandRegio/disaggregator...")

# NUTS-3 mapping: id_ags → natcode_nuts3
nuts3_map = pd.read_csv(f'{DISAGG_DATA}/t_nuts3_lk.csv')
ags_to_nuts3 = dict(zip(nuts3_map['id_ags'], nuts3_map['natcode_nuts3']))
# Fix NUTS code version mismatch (DB uses 2021, disaggregator uses 2016)
NUTS_REMAP = {'DEB1C': 'DEB16', 'DEB1D': 'DEB19'}
print(f"  NUTS-3 regions in mapping: {len(nuts3_map)}")

# Household electricity consumption per NUTS-3 (MWh, year 2015)
hh_elc = pd.read_csv(f'{DISAGG_DATA}/elc_consumption_HH_spatial.csv')
hh_2015 = hh_elc[hh_elc['year'] == 2015][['id_region', 'value']].copy()
hh_2015.columns = ['id_ags', 'hh_mwh_2015']
hh_2015['nuts3'] = hh_2015['id_ags'].map(ags_to_nuts3)
total_hh_2015 = hh_2015['hh_mwh_2015'].sum() / 1e6  # TWh
print(f"  Household electricity 2015: {total_hh_2015:.1f} TWh ({len(hh_2015)} regions)")

# Population per NUTS-3 (year 2015)
pop = pd.read_csv(f'{DISAGG_DATA}/population.csv')
pop_2015 = pop[pop['year'] == 2015][['id_region', 'value']].copy()
pop_2015.columns = ['id_ags', 'population']
pop_2015['nuts3'] = pop_2015['id_ags'].map(ags_to_nuts3)
total_pop = pop_2015['population'].sum()
print(f"  Population 2015: {total_pop:,.0f} ({len(pop_2015)} regions)")

# ── Step 2: Compute NUTS-3 demand per sector, scaled to 2025 ────────────────
print("\n[Step 2] Computing NUTS-3 demand scaled to 2025...")

# Merge HH + population data per NUTS-3
nuts3_data = pd.merge(hh_2015[['nuts3', 'hh_mwh_2015']],
                       pop_2015[['nuts3', 'population']],
                       on='nuts3', how='outer')

# Some NUTS-3 might exist in one dataset but not the other
nuts3_data = nuts3_data.dropna(subset=['nuts3'])
nuts3_data = nuts3_data.groupby('nuts3').agg({
    'hh_mwh_2015': 'sum',
    'population': 'sum'
}).reset_index()

# Scale household demand from 2015 (132 TWh) → 2025 (134 TWh)
hh_scale = (NATIONAL_HH_TWH * 1e6) / nuts3_data['hh_mwh_2015'].sum()
nuts3_data['hh_mwh'] = nuts3_data['hh_mwh_2015'] * hh_scale

# CTS: distribute proportional to population
pop_total = nuts3_data['population'].sum()
nuts3_data['cts_mwh'] = (nuts3_data['population'] / pop_total) * (NATIONAL_CTS_TWH * 1e6)

# Industry: distribute proportional to population (rough proxy)
nuts3_data['ind_mwh'] = (nuts3_data['population'] / pop_total) * (NATIONAL_IND_TWH * 1e6)

# Total
nuts3_data['total_mwh'] = nuts3_data['hh_mwh'] + nuts3_data['cts_mwh'] + nuts3_data['ind_mwh']
nuts3_data['total_gwh'] = nuts3_data['total_mwh'] / 1e3

print(f"  NUTS-3 regions with demand: {len(nuts3_data)}")
print(f"  Total HH:  {nuts3_data['hh_mwh'].sum()/1e6:.1f} TWh")
print(f"  Total CTS: {nuts3_data['cts_mwh'].sum()/1e6:.1f} TWh")
print(f"  Total IND: {nuts3_data['ind_mwh'].sum()/1e6:.1f} TWh")
print(f"  TOTAL:     {nuts3_data['total_mwh'].sum()/1e6:.1f} TWh")

# ── Step 3: Read municipality boundaries from PostGIS ────────────────────────
print("\n[Step 3] Reading municipality boundaries from database...")

engine = create_engine(DB_URI)

# Read municipalities with geometry (use vg250_gem, convert to WGS84 for folium)
gem = gpd.read_postgis(
    """
    SELECT id, gen, bez, nuts, ags_0,
           ST_Transform(geometry, 4326) as geometry,
           ST_Area(ST_Transform(geometry, 3035)) / 1e6 as area_km2
    FROM boundaries.vg250_gem
    WHERE nuts IS NOT NULL AND nuts LIKE 'DE%%'
    """,
    engine,
    geom_col='geometry'
)
print(f"  Municipalities loaded: {len(gem)}")
print(f"  Total area: {gem['area_km2'].sum():,.0f} km²")
print(f"  NUTS-3 codes in municipalities: {gem['nuts'].nunique()}")

# Remap NUTS codes where DB and disaggregator differ
for new_code, old_code in NUTS_REMAP.items():
    n = (gem['nuts'] == new_code).sum()
    if n > 0:
        gem.loc[gem['nuts'] == new_code, 'nuts'] = old_code
        print(f"  Remapped {n} municipalities: {new_code} → {old_code}")

# Simplify geometry for performance (tolerance in degrees, ~500m)
gem['geometry'] = gem['geometry'].simplify(0.005, preserve_topology=True)
print(f"  Geometries simplified for rendering")

# ── Step 4: Distribute NUTS-3 demand to municipalities by area ───────────────
print("\n[Step 4] Distributing demand to municipalities...")

# Compute each municipality's area share within its NUTS-3 region
nuts3_areas = gem.groupby('nuts')['area_km2'].sum().reset_index()
nuts3_areas.columns = ['nuts', 'nuts3_total_area_km2']
gem = gem.merge(nuts3_areas, on='nuts', how='left')
gem['area_share'] = gem['area_km2'] / gem['nuts3_total_area_km2']

# Join demand data
gem = gem.merge(nuts3_data[['nuts3', 'hh_mwh', 'cts_mwh', 'ind_mwh', 'total_mwh', 'total_gwh']],
                left_on='nuts', right_on='nuts3', how='left')

# Distribute by area share
for col in ['hh_mwh', 'cts_mwh', 'ind_mwh', 'total_mwh']:
    gem[f'gem_{col}'] = gem[col] * gem['area_share']

gem['gem_total_gwh'] = gem['gem_total_mwh'] / 1e3
gem['demand_density_mwh_km2'] = gem['gem_total_mwh'] / gem['area_km2']

# Municipalities without demand data (not matched to NUTS-3)
missing = gem['gem_total_mwh'].isna().sum()
if missing > 0:
    print(f"  WARNING: {missing} municipalities without demand data")
    gem['gem_total_mwh'] = gem['gem_total_mwh'].fillna(0)
    gem['gem_total_gwh'] = gem['gem_total_gwh'].fillna(0)
    gem['demand_density_mwh_km2'] = gem['demand_density_mwh_km2'].fillna(0)
    gem['gem_hh_mwh'] = gem['gem_hh_mwh'].fillna(0)
    gem['gem_cts_mwh'] = gem['gem_cts_mwh'].fillna(0)
    gem['gem_ind_mwh'] = gem['gem_ind_mwh'].fillna(0)

print(f"  Demand range: {gem['gem_total_gwh'].min():.2f} – {gem['gem_total_gwh'].max():.1f} GWh/yr")
print(f"  Density range: {gem['demand_density_mwh_km2'].min():.0f} – {gem['demand_density_mwh_km2'].max():.0f} MWh/km²/yr")
print(f"  Total distributed: {gem['gem_total_mwh'].sum()/1e6:.1f} TWh")

# ── Step 5: Read large industrial consumers from MaStR ───────────────────────
print("\n[Step 5] Reading large industrial consumers from MaStR...")

consumers = pd.read_sql("""
    SELECT "NameStromverbrauchseinheit" as name,
           "Breitengrad" as lat, "Laengengrad" as lon,
           "Postleitzahl" as plz, "Bundesland" as bundesland,
           "Gemeinde" as gemeinde, "Landkreis" as landkreis,
           "AnzahlStromverbrauchseinheitenGroesser50Mw" as units_gt50mw,
           "Inbetriebnahmedatum" as commissioned
    FROM mastr.electricity_consumer
    WHERE "EinheitBetriebsstatus" = 'In Betrieb'
      AND "Laengengrad" IS NOT NULL
""", engine)
print(f"  Large consumers loaded: {len(consumers)}")
print(f"  With >50MW units info: {consumers['units_gt50mw'].notna().sum()}")

engine.dispose()

# ── Step 6: Build HTML heatmap ───────────────────────────────────────────────
print("\n[Step 6] Building HTML heatmap...")

# Prepare GeoJSON with properties for tooltips
gem_json = gem[['id', 'gen', 'bez', 'nuts', 'ags_0', 'area_km2',
                'gem_hh_mwh', 'gem_cts_mwh', 'gem_ind_mwh',
                'gem_total_mwh', 'gem_total_gwh', 'demand_density_mwh_km2',
                'geometry']].copy()

# Convert to GeoJSON string with reduced coordinate precision
import re

geojson_str = gem_json.to_json()

# Round all coordinates to 4 decimal places (~11m precision, sufficient for municipality level)
def round_coords(match):
    return str(round(float(match.group(0)), 4))

geojson_str = re.sub(r'-?\d+\.\d{5,}', round_coords, geojson_str)
print(f"  GeoJSON size: {len(geojson_str)/1e6:.1f} MB")

# Prepare consumer data as JSON
consumers_json = consumers.to_json(orient='records', date_format='iso')

# Color scale: use log scale for demand density
densities = gem['demand_density_mwh_km2']
densities_pos = densities[densities > 0]
log_min = math.log10(max(densities_pos.min(), 1))
log_max = math.log10(densities_pos.max())

# Top municipalities by total demand
top_10 = gem.nlargest(10, 'gem_total_gwh')[['gen', 'nuts', 'gem_total_gwh', 'demand_density_mwh_km2', 'area_km2']]

print(f"\n  Top 10 municipalities by total demand:")
for _, row in top_10.iterrows():
    print(f"    {row['gen']:25s} ({row['nuts']}) – {row['gem_total_gwh']:.1f} GWh/yr, "
          f"{row['demand_density_mwh_km2']:.0f} MWh/km²")

# Build HTML
html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Germany Electricity Demand Heatmap 2025</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #1a1a2e; }}
  #map {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; }}

  /* Info panel */
  .info-panel {{
    position: absolute; top: 12px; right: 12px; z-index: 1000;
    background: rgba(26,26,46,0.95); color: #e0e0e0; padding: 14px 18px;
    border-radius: 8px; font-size: 13px; max-width: 340px;
    border: 1px solid #333; backdrop-filter: blur(10px);
  }}
  .info-panel h3 {{ color: #ff6b6b; margin-bottom: 8px; font-size: 15px; }}
  .info-panel .sector {{ display: flex; justify-content: space-between; padding: 2px 0; }}
  .info-panel .sector .label {{ color: #aaa; }}
  .info-panel .sector .value {{ font-weight: 600; }}
  .info-panel .total {{ border-top: 1px solid #444; margin-top: 4px; padding-top: 4px; font-weight: 700; color: #fff; }}
  .info-panel .detail {{ color: #888; font-size: 11px; margin-top: 6px; }}

  /* Legend */
  .legend {{
    position: absolute; bottom: 30px; left: 12px; z-index: 1000;
    background: rgba(26,26,46,0.95); color: #e0e0e0; padding: 14px 18px;
    border-radius: 8px; font-size: 12px;
    border: 1px solid #333; backdrop-filter: blur(10px);
  }}
  .legend h4 {{ margin-bottom: 8px; color: #ffd93d; font-size: 13px; }}
  .legend-row {{ display: flex; align-items: center; margin: 3px 0; }}
  .legend-color {{ width: 24px; height: 14px; margin-right: 8px; border-radius: 2px; border: 1px solid #555; }}
  .legend .summary {{ margin-top: 10px; padding-top: 8px; border-top: 1px solid #444; color: #aaa; font-size: 11px; line-height: 1.6; }}

  /* Controls */
  .controls {{
    position: absolute; top: 12px; left: 12px; z-index: 1000;
    background: rgba(26,26,46,0.95); color: #e0e0e0; padding: 14px 18px;
    border-radius: 8px; font-size: 13px;
    border: 1px solid #333; backdrop-filter: blur(10px);
  }}
  .controls h4 {{ color: #ffd93d; margin-bottom: 10px; font-size: 14px; }}
  .controls label {{ display: block; margin: 5px 0; cursor: pointer; }}
  .controls input[type="radio"], .controls input[type="checkbox"] {{ margin-right: 6px; }}
  .radio-group {{ margin-bottom: 10px; padding-bottom: 8px; border-bottom: 1px solid #333; }}

  /* Tooltip style */
  .leaflet-tooltip {{
    background: rgba(26,26,46,0.95) !important;
    color: #e0e0e0 !important;
    border: 1px solid #555 !important;
    border-radius: 6px !important;
    padding: 8px 12px !important;
    font-size: 12px !important;
    font-family: 'Segoe UI', Arial, sans-serif !important;
  }}
  .leaflet-tooltip::before {{ border-right-color: rgba(26,26,46,0.95) !important; }}
</style>
</head>
<body>
<div id="map"></div>

<div class="controls">
  <h4>Demand Heatmap 2025</h4>
  <div class="radio-group">
    <label><input type="radio" name="metric" value="total" checked> Total Demand</label>
    <label><input type="radio" name="metric" value="density"> Demand Density (MWh/km²)</label>
    <label><input type="radio" name="metric" value="hh"> Households only</label>
    <label><input type="radio" name="metric" value="cts"> CTS only</label>
    <label><input type="radio" name="metric" value="ind"> Industry only</label>
  </div>
  <label><input type="checkbox" id="showConsumers" checked> Large Consumers (MaStR)</label>
  <label><input type="checkbox" id="showBorders" checked> Municipality Borders</label>
</div>

<div class="info-panel" id="infoPanel">
  <h3>Germany 2025</h3>
  <div class="sector"><span class="label">Households</span><span class="value">{NATIONAL_HH_TWH:.0f} TWh</span></div>
  <div class="sector"><span class="label">CTS (GHD)</span><span class="value">{NATIONAL_CTS_TWH:.0f} TWh</span></div>
  <div class="sector"><span class="label">Industry</span><span class="value">{NATIONAL_IND_TWH:.0f} TWh</span></div>
  <div class="sector total"><span class="label">Total</span><span class="value">{NATIONAL_TOTAL_TWH:.0f} TWh</span></div>
  <div class="detail">Source: BDEW/BNetzA 2025, spatial: DemandRegio NUTS-3<br>
  {len(gem):,} municipalities &middot; {nuts3_data.shape[0]} NUTS-3 regions</div>
</div>

<div class="legend" id="legend">
  <h4>Demand Density</h4>
  <div id="legendContent"></div>
  <div class="summary">
    {len(consumers)} large consumers (MaStR)<br>
    Peak load 2025: ~76 GW<br>
    Population: ~84.4 million
  </div>
</div>

<script>
// ── Data ──
const geojsonData = {geojson_str};
const consumers = {consumers_json};

// ── Map ──
const map = L.map('map', {{
  center: [51.2, 10.4],
  zoom: 6,
  zoomControl: true,
  preferCanvas: true
}});

L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; CartoDB &copy; OSM',
  maxZoom: 18
}}).addTo(map);

// ── Color scales ──
// Reds/oranges for demand
function getColor(value, metric) {{
  if (value <= 0 || isNaN(value)) return '#1a1a2e';

  let t;
  if (metric === 'density') {{
    // Log scale for density
    const logMin = {log_min:.2f};
    const logMax = {log_max:.2f};
    t = (Math.log10(Math.max(value, 1)) - logMin) / (logMax - logMin);
  }} else {{
    // Total demand in GWh: use log scale too
    const vLog = Math.log10(Math.max(value, 0.001));
    const minLog = -2;  // 0.01 GWh
    const maxLog = 3.5;  // ~3000 GWh
    t = (vLog - minLog) / (maxLog - minLog);
  }}
  t = Math.max(0, Math.min(1, t));

  // Color ramp: dark blue → yellow → orange → red → dark red
  const stops = [
    [0.0,  [30, 40, 80]],
    [0.15, [30, 80, 140]],
    [0.3,  [40, 150, 120]],
    [0.45, [120, 200, 80]],
    [0.6,  [255, 217, 61]],
    [0.75, [255, 140, 40]],
    [0.9,  [230, 60, 30]],
    [1.0,  [140, 20, 20]]
  ];

  // Interpolate between stops
  let i = 0;
  while (i < stops.length - 1 && stops[i + 1][0] < t) i++;
  if (i >= stops.length - 1) return `rgb(${{stops[stops.length-1][1].join(',')}})`;

  const [t0, c0] = stops[i];
  const [t1, c1] = stops[i + 1];
  const f = (t - t0) / (t1 - t0);
  const r = Math.round(c0[0] + (c1[0] - c0[0]) * f);
  const g = Math.round(c0[1] + (c1[1] - c0[1]) * f);
  const b = Math.round(c0[2] + (c1[2] - c0[2]) * f);
  return `rgb(${{r}},${{g}},${{b}})`;
}}

// ── Current metric ──
let currentMetric = 'total';

function getFeatureValue(props, metric) {{
  switch (metric) {{
    case 'total': return props.gem_total_gwh;
    case 'density': return props.demand_density_mwh_km2;
    case 'hh': return props.gem_hh_mwh / 1000;
    case 'cts': return props.gem_cts_mwh / 1000;
    case 'ind': return props.gem_ind_mwh / 1000;
    default: return props.gem_total_gwh;
  }}
}}

function formatValue(value, metric) {{
  if (metric === 'density') return value.toFixed(0) + ' MWh/km²';
  if (value >= 1000) return (value / 1000).toFixed(1) + ' TWh';
  if (value >= 1) return value.toFixed(1) + ' GWh';
  return (value * 1000).toFixed(0) + ' MWh';
}}

function metricLabel(metric) {{
  switch (metric) {{
    case 'total': return 'Total Demand';
    case 'density': return 'Demand Density';
    case 'hh': return 'Household Demand';
    case 'cts': return 'CTS Demand';
    case 'ind': return 'Industry Demand';
    default: return 'Total Demand';
  }}
}}

// ── GeoJSON layer ──
let geojsonLayer;
let showBorders = true;

function styleFeature(feature) {{
  const val = getFeatureValue(feature.properties, currentMetric);
  return {{
    fillColor: getColor(val, currentMetric),
    fillOpacity: 0.85,
    color: showBorders ? '#333' : 'transparent',
    weight: showBorders ? 0.3 : 0,
    opacity: 0.5
  }};
}}

function onEachFeature(feature, layer) {{
  layer.on({{
    mouseover: function(e) {{
      const p = feature.properties;
      const val = getFeatureValue(p, currentMetric);
      const panel = document.getElementById('infoPanel');
      panel.innerHTML = `
        <h3>${{p.gen}} (${{p.bez}})</h3>
        <div class="sector"><span class="label">NUTS-3</span><span class="value">${{p.nuts}}</span></div>
        <div class="sector"><span class="label">Area</span><span class="value">${{p.area_km2.toFixed(1)}} km²</span></div>
        <hr style="border-color:#444; margin:6px 0">
        <div class="sector"><span class="label">Households</span><span class="value">${{(p.gem_hh_mwh/1000).toFixed(1)}} GWh</span></div>
        <div class="sector"><span class="label">CTS (GHD)</span><span class="value">${{(p.gem_cts_mwh/1000).toFixed(1)}} GWh</span></div>
        <div class="sector"><span class="label">Industry</span><span class="value">${{(p.gem_ind_mwh/1000).toFixed(1)}} GWh</span></div>
        <div class="sector total"><span class="label">Total</span><span class="value">${{p.gem_total_gwh.toFixed(1)}} GWh/yr</span></div>
        <div class="detail">Density: ${{p.demand_density_mwh_km2.toFixed(0)}} MWh/km²/yr</div>
      `;
      e.target.setStyle({{ weight: 2, color: '#fff', fillOpacity: 0.95 }});
      e.target.bringToFront();
    }},
    mouseout: function(e) {{
      geojsonLayer.resetStyle(e.target);
      resetInfoPanel();
    }}
  }});
}}

function resetInfoPanel() {{
  document.getElementById('infoPanel').innerHTML = `
    <h3>Germany 2025</h3>
    <div class="sector"><span class="label">Households</span><span class="value">{NATIONAL_HH_TWH:.0f} TWh</span></div>
    <div class="sector"><span class="label">CTS (GHD)</span><span class="value">{NATIONAL_CTS_TWH:.0f} TWh</span></div>
    <div class="sector"><span class="label">Industry</span><span class="value">{NATIONAL_IND_TWH:.0f} TWh</span></div>
    <div class="sector total"><span class="label">Total</span><span class="value">{NATIONAL_TOTAL_TWH:.0f} TWh</span></div>
    <div class="detail">Source: BDEW/BNetzA 2025, spatial: DemandRegio NUTS-3<br>
    {len(gem):,} municipalities &middot; {nuts3_data.shape[0]} NUTS-3 regions</div>
  `;
}}

geojsonLayer = L.geoJSON(geojsonData, {{
  style: styleFeature,
  onEachFeature: onEachFeature
}}).addTo(map);

// ── Consumer markers ──
let consumerLayer = L.layerGroup();
consumers.forEach(c => {{
  if (!c.lat || !c.lon) return;
  const marker = L.circleMarker([c.lat, c.lon], {{
    radius: Math.max(4, Math.min(12, (c.units_gt50mw || 1) * 2)),
    color: '#00ffff',
    fillColor: '#00cccc',
    fillOpacity: 0.7,
    weight: 1.5
  }});
  let tip = `<b>${{c.name || 'Unknown'}}</b><br>`;
  if (c.gemeinde) tip += `${{c.gemeinde}}, ${{c.bundesland}}<br>`;
  if (c.units_gt50mw) tip += `Units >50MW: ${{c.units_gt50mw}}<br>`;
  if (c.plz) tip += `PLZ: ${{c.plz}}`;
  marker.bindTooltip(tip);
  consumerLayer.addLayer(marker);
}});
consumerLayer.addTo(map);

// ── Legend ──
function updateLegend() {{
  const el = document.getElementById('legendContent');
  let html = '';

  if (currentMetric === 'density') {{
    const labels = [10, 50, 200, 500, 1000, 5000, 20000, 100000];
    labels.forEach(v => {{
      html += `<div class="legend-row">
        <div class="legend-color" style="background:${{getColor(v, 'density')}}"></div>
        <span>${{v >= 1000 ? (v/1000) + 'k' : v}} MWh/km²</span>
      </div>`;
    }});
  }} else {{
    const labels = [0.01, 0.1, 1, 10, 50, 200, 500, 2000];
    labels.forEach(v => {{
      html += `<div class="legend-row">
        <div class="legend-color" style="background:${{getColor(v, 'total')}}"></div>
        <span>${{formatValue(v, 'total')}}</span>
      </div>`;
    }});
  }}

  // Consumer marker
  html += `<div class="legend-row" style="margin-top:6px">
    <div class="legend-color" style="background:#00cccc; border-radius:50%; width:14px; height:14px;"></div>
    <span>Large Consumer (MaStR)</span>
  </div>`;

  el.innerHTML = html;
  document.querySelector('.legend h4').textContent = metricLabel(currentMetric);
}}
updateLegend();

// ── Controls ──
document.querySelectorAll('input[name="metric"]').forEach(radio => {{
  radio.addEventListener('change', function() {{
    currentMetric = this.value;
    geojsonLayer.setStyle(styleFeature);
    updateLegend();
  }});
}});

document.getElementById('showConsumers').addEventListener('change', function() {{
  if (this.checked) map.addLayer(consumerLayer);
  else map.removeLayer(consumerLayer);
}});

document.getElementById('showBorders').addEventListener('change', function() {{
  showBorders = this.checked;
  geojsonLayer.setStyle(styleFeature);
}});
</script>
</body>
</html>"""

# ── Write HTML ───────────────────────────────────────────────────────────────
output_path = '/root/egon_2025_project/demand_heatmap.html'
with open(output_path, 'w', encoding='utf-8') as f:
    f.write(html)

print(f"\n[Done] Heatmap written to: {output_path}")
print(f"  File size: {len(html) / 1e6:.1f} MB")

# ── Also export demand CSV for reference ─────────────────────────────────────
csv_path = '/root/egon_2025_project/demand_by_municipality_2025.csv'
export = gem[['ags_0', 'gen', 'bez', 'nuts', 'area_km2',
              'gem_hh_mwh', 'gem_cts_mwh', 'gem_ind_mwh',
              'gem_total_mwh', 'gem_total_gwh', 'demand_density_mwh_km2']].copy()
export.columns = ['ags', 'name', 'type', 'nuts3', 'area_km2',
                   'hh_mwh', 'cts_mwh', 'industry_mwh',
                   'total_mwh', 'total_gwh', 'density_mwh_per_km2']
export = export.sort_values('total_gwh', ascending=False)
export.to_csv(csv_path, index=False)
print(f"  CSV exported: {csv_path} ({len(export)} rows)")

# ── Export NUTS-3 summary CSV ────────────────────────────────────────────────
nuts3_csv = '/root/egon_2025_project/demand_by_nuts3_2025.csv'
nuts3_export = nuts3_data[['nuts3', 'hh_mwh', 'cts_mwh', 'ind_mwh', 'total_mwh', 'total_gwh', 'population']].copy()
nuts3_export = nuts3_export.sort_values('total_gwh', ascending=False)
nuts3_export.to_csv(nuts3_csv, index=False)
print(f"  NUTS-3 CSV exported: {nuts3_csv} ({len(nuts3_export)} rows)")

print("\n" + "=" * 60)
print("DONE – Open demand_heatmap.html in a browser")
print("=" * 60)
