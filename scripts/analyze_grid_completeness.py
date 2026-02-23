"""
Analyze eGon2025 grid completeness against real-world JAO/CORE-TSO reference data.

Purpose
-------
Quantifies how well the OSM-derived eGon2025 grid model covers the actual
German transmission network as documented by the JAO/CORE-TSO dataset.
This script is the primary quality-assurance tool used before building
reduced scenarios (v2, v3, v4), highlighting capacity gaps, missing
corridors, connectivity issues, and known backbone verification.

Algorithm / Method
------------------
The script runs four major analyses:

1. **Capacity comparison (eGon vs JAO)** -- Aggregates total transmission
   capacity (GVA) by voltage level for both networks. Identifies individual
   matched lines where eGon capacity is more than 2x or less than 0.5x the
   JAO three-phase capacity (JAO s_nom is per-phase, multiplied by sqrt(3)).
2. **Missing corridors** -- Finds JAO lines that have no match in the eGon
   network (using pre-computed line-match reports). Groups them by voltage
   level and ranks the top missing corridors by capacity.
3. **Network connectivity** -- Builds a NetworkX graph of the eGon 220/380 kV
   subnetwork and reports the number of connected components, largest
   component size, and orphan (degree-0/1) buses. Uses scipy ``cKDTree``
   for nearest-neighbour spatial analysis of isolated nodes.
4. **Known backbone corridors** -- Verifies presence of major real-world
   German 380 kV corridors (e.g., Hamburg--Hannover, Frankfurt--Mannheim)
   by spatial proximity search between known endpoint coordinates.

Inputs
------
- PostgreSQL database (``egon-data`` on port 59734):
  - ``grid.egon_etrago_bus`` (eGon2025, all voltage levels)
  - ``grid.egon_etrago_line`` (eGon2025, all voltage levels)
  - ``grid.egon_etrago_transformer`` (eGon2025)
- ``data/jao_core_tso/buses.csv`` -- JAO/CORE-TSO bus data
- ``data/jao_core_tso/lines.csv`` -- JAO/CORE-TSO line data
- ``data/jao_core_tso/transformers.csv`` -- JAO/CORE-TSO transformer data
- ``results/jao_matching/bus_match_report.csv`` -- Pre-computed bus matches
- ``results/jao_matching/line_match_report.csv`` -- Pre-computed line matches

Outputs
-------
- Detailed analysis report printed to stdout covering all four analyses
  plus a final summary section. No files are written.

Usage
-----
::

    conda activate egon2025
    python scripts/analyze_grid_completeness.py
"""

import pandas as pd
import numpy as np
from sqlalchemy import create_engine
import networkx as nx
from scipy.spatial import cKDTree
import sys

# Database connection
engine = create_engine('postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data')

print("="*80)
print("EGON2025 GRID COMPLETENESS ANALYSIS")
print("="*80)
print()

# ============================================================================
# LOAD DATA
# ============================================================================
print("Loading data...")

# Load eGon data from database
egon_buses = pd.read_sql(
    "SELECT * FROM grid.egon_etrago_bus WHERE scn_name = 'eGon2025'",
    engine
)
egon_lines = pd.read_sql(
    "SELECT * FROM grid.egon_etrago_line WHERE scn_name = 'eGon2025'",
    engine
)
egon_transformers = pd.read_sql(
    "SELECT * FROM grid.egon_etrago_transformer WHERE scn_name = 'eGon2025'",
    engine
)

# Load JAO data
jao_buses = pd.read_csv('data/jao_core_tso/buses.csv')
jao_lines = pd.read_csv('data/jao_core_tso/lines.csv')
jao_transformers = pd.read_csv('data/jao_core_tso/transformers.csv')

# Load matching reports
bus_match = pd.read_csv('results/jao_matching/bus_match_report.csv')
line_match = pd.read_csv('results/jao_matching/line_match_report.csv')

print(f"eGon buses: {len(egon_buses)}")
print(f"eGon lines: {len(egon_lines)}")
print(f"eGon transformers: {len(egon_transformers)}")
print(f"JAO buses: {len(jao_buses)}")
print(f"JAO lines: {len(jao_lines)}")
print()

# ============================================================================
# ANALYSIS 1: CAPACITY COMPARISON
# ============================================================================
print("="*80)
print("ANALYSIS 1: CAPACITY COMPARISON (eGon vs JAO)")
print("="*80)
print()

# eGon capacity by voltage level - use v_nom from line table directly
egon_capacity = egon_lines.groupby('v_nom')['s_nom'].sum() / 1000  # GVA

# JAO capacity (convert per-phase to 3-phase)
JAO_SQRT3 = np.sqrt(3)
jao_lines['s_nom_3phase'] = jao_lines['s_nom'] * JAO_SQRT3
jao_capacity = jao_lines.groupby('v_nom')['s_nom_3phase'].sum() / 1000  # GVA

print("Total transmission capacity by voltage level:")
print("-" * 60)
print(f"{'Voltage':<15} {'eGon (GVA)':<20} {'JAO (GVA)':<20} {'Ratio':<10}")
print("-" * 60)

for v_nom in sorted(set(egon_capacity.index) | set(jao_capacity.index)):
    egon_val = egon_capacity.get(v_nom, 0)
    jao_val = jao_capacity.get(v_nom, 0)
    ratio = egon_val / jao_val if jao_val > 0 else np.inf
    print(f"{v_nom:<15.0f} {egon_val:<20.1f} {jao_val:<20.1f} {ratio:<10.2f}x")

print("-" * 60)
print(f"{'TOTAL':<15} {egon_capacity.sum():<20.1f} {jao_capacity.sum():<20.1f} "
      f"{egon_capacity.sum()/jao_capacity.sum():<10.2f}x")
print()

# Lines with significant capacity differences
print("Lines with significant capacity differences (matched lines only):")
print("-" * 80)

# Get matched lines - those with non-null egon_line_id
matched_lines = line_match[line_match['egon_line_id'].notna()].copy()

# Merge with JAO lines to get bus names and other info
jao_lines['jao_eic'] = jao_lines['EIC_Code']
matched_lines = matched_lines.merge(
    jao_lines[['EIC_Code', 'name', 'CORE-TSO_bus0', 'CORE-TSO_bus1', 'v_nom']], 
    left_on='jao_eic', 
    right_on='EIC_Code', 
    how='left'
)
matched_lines = matched_lines.rename(columns={
    'CORE-TSO_bus0': 'bus0_name',
    'CORE-TSO_bus1': 'bus1_name'
})

# Get eGon line s_nom - match reports may have multiple eGon lines per JAO line
egon_line_snom = egon_lines[['line_id', 's_nom']].copy()
matched_lines = matched_lines.merge(
    egon_line_snom, 
    left_on='egon_line_id', 
    right_on='line_id', 
    how='left'
)

# Convert JAO to 3-phase and calculate difference
matched_lines['s_nom_jao_3phase'] = matched_lines['jao_s_nom'] * JAO_SQRT3
matched_lines['s_nom_egon'] = matched_lines['new_s_nom']  # Use aggregated s_nom from match report
matched_lines['capacity_ratio'] = matched_lines['s_nom_egon'] / matched_lines['s_nom_jao_3phase']
matched_lines['capacity_diff_mva'] = matched_lines['s_nom_egon'] - matched_lines['s_nom_jao_3phase']

# Top 20 lines where eGon has MUCH MORE capacity than JAO
print("\nTop 20 lines where eGon has MUCH MORE capacity than JAO (ratio > 2):")
print(f"{'Corridor':<50} {'Voltage':<10} {'eGon MVA':<12} {'JAO MVA':<12} {'Ratio':<10}")
print("-" * 100)
over_capacity = matched_lines[matched_lines['capacity_ratio'] > 2].sort_values('capacity_ratio', ascending=False).head(20)
for _, row in over_capacity.iterrows():
    corridor = f"{row['bus0_name']} - {row['bus1_name']}"[:48]
    print(f"{corridor:<50} {row['v_nom']:<10.0f} {row['s_nom_egon']:<12.0f} "
          f"{row['s_nom_jao_3phase']:<12.0f} {row['capacity_ratio']:<10.2f}x")

# Top 20 lines where eGon has MUCH LESS capacity than JAO
print("\nTop 20 lines where eGon has MUCH LESS capacity than JAO (ratio < 0.5):")
print(f"{'Corridor':<50} {'Voltage':<10} {'eGon MVA':<12} {'JAO MVA':<12} {'Ratio':<10}")
print("-" * 100)
under_capacity = matched_lines[matched_lines['capacity_ratio'] < 0.5].sort_values('capacity_ratio').head(20)
for _, row in under_capacity.iterrows():
    corridor = f"{row['bus0_name']} - {row['bus1_name']}"[:48]
    print(f"{corridor:<50} {row['v_nom']:<10.0f} {row['s_nom_egon']:<12.0f} "
          f"{row['s_nom_jao_3phase']:<12.0f} {row['capacity_ratio']:<10.2f}x")

print()

# ============================================================================
# ANALYSIS 2: MISSING JAO LINES
# ============================================================================
print("="*80)
print("ANALYSIS 2: MISSING CORRIDORS (JAO lines not matched in eGon)")
print("="*80)
print()

# Find unmatched JAO lines - those whose EIC is not in the match report
matched_eics = set(line_match['jao_eic'])
unmatched_jao = jao_lines[~jao_lines['EIC_Code'].isin(matched_eics)].copy()

print(f"Matched JAO lines: {len(matched_eics)} / {len(jao_lines)} ({100*len(matched_eics)/len(jao_lines):.1f}%)")
print(f"Unmatched JAO lines: {len(unmatched_jao)} ({100*len(unmatched_jao)/len(jao_lines):.1f}%)")
print()

# Convert to 3-phase capacity
unmatched_jao['s_nom_3phase'] = unmatched_jao['s_nom'] * JAO_SQRT3

# Group by voltage level
print("Unmatched lines by voltage level:")
print("-" * 60)
print(f"{'Voltage':<15} {'Count':<15} {'Total Capacity (GVA)':<25}")
print("-" * 60)
for v_nom in sorted(unmatched_jao['v_nom'].unique()):
    subset = unmatched_jao[unmatched_jao['v_nom'] == v_nom]
    print(f"{v_nom:<15.0f} {len(subset):<15} {subset['s_nom_3phase'].sum()/1000:<25.1f}")
print()

# Top 30 most important missing corridors by capacity
print("Top 30 most important missing corridors (by 3-phase capacity):")
print("-" * 100)
print(f"{'Corridor':<60} {'Voltage':<10} {'Capacity (MVA)':<20}")
print("-" * 100)

unmatched_sorted = unmatched_jao.sort_values('s_nom_3phase', ascending=False).head(30)
for _, row in unmatched_sorted.iterrows():
    corridor = f"{row['CORE-TSO_bus0']} - {row['CORE-TSO_bus1']}"[:58]
    print(f"{corridor:<60} {row['v_nom']:<10.0f} {row['s_nom_3phase']:<20.0f}")

print()

# ============================================================================
# ANALYSIS 3: NETWORK CONNECTIVITY
# ============================================================================
print("="*80)
print("ANALYSIS 3: NETWORK CONNECTIVITY ANALYSIS")
print("="*80)
print()

def analyze_connectivity(buses, lines, voltage_level, network_name):
    """Analyze connectivity of a network at a specific voltage level."""
    # Filter to voltage level
    buses_v = buses[buses['v_nom'] == voltage_level].copy()
    
    # Get bus IDs for this voltage
    bus_ids = set(buses_v['bus_id'])
    
    # Filter lines to only those connecting buses at this voltage
    lines_v = lines[
        lines['bus0'].isin(bus_ids) & 
        lines['bus1'].isin(bus_ids)
    ].copy()
    
    # Build graph
    G = nx.Graph()
    for bus_id in bus_ids:
        G.add_node(bus_id)
    for _, line in lines_v.iterrows():
        G.add_edge(line['bus0'], line['bus1'])
    
    # Analyze connectivity
    num_components = nx.number_connected_components(G)
    is_connected = nx.is_connected(G)
    
    print(f"{network_name} {voltage_level}kV network:")
    print(f"  Buses: {len(bus_ids)}")
    print(f"  Lines: {len(lines_v)}")
    print(f"  Connected: {'YES' if is_connected else 'NO'}")
    print(f"  Number of connected components: {num_components}")
    
    if num_components > 1:
        # Find component sizes
        components = list(nx.connected_components(G))
        component_sizes = sorted([len(c) for c in components], reverse=True)
        print(f"  Component sizes: {component_sizes[:10]}")  # Top 10
        
        if len(component_sizes) > 1:
            print(f"  Largest component: {component_sizes[0]} buses ({100*component_sizes[0]/len(bus_ids):.1f}%)")
            print(f"  Isolated buses/small islands: {sum(component_sizes[1:])} buses")
    
    print()
    
    return {
        'buses': len(bus_ids),
        'lines': len(lines_v),
        'connected': is_connected,
        'num_components': num_components
    }

# eGon connectivity
print("eGon2025 Grid Connectivity:")
print("-" * 60)
egon_380 = analyze_connectivity(egon_buses, egon_lines, 380, "eGon")
egon_220 = analyze_connectivity(egon_buses, egon_lines, 220, "eGon")
egon_110 = analyze_connectivity(egon_buses, egon_lines, 110, "eGon")

# JAO connectivity (note: JAO uses 400 instead of 380)
print("JAO/CORE-TSO Grid Connectivity:")
print("-" * 60)
jao_400 = analyze_connectivity(jao_buses, jao_lines, 400, "JAO")
jao_220 = analyze_connectivity(jao_buses, jao_lines, 220, "JAO")

# ============================================================================
# ANALYSIS 4: KNOWN BACKBONE LINES CHECK
# ============================================================================
print("="*80)
print("ANALYSIS 4: KNOWN MAJOR GERMAN 380kV CORRIDORS")
print("="*80)
print()

# Known major corridors (approximate locations)
KNOWN_CORRIDORS = [
    ("Hamburg", 53.55, 10.0),
    ("Lübeck", 53.87, 10.69),
    ("Hannover", 52.37, 9.74),
    ("Dortmund", 51.51, 7.47),
    ("Cologne", 50.94, 6.96),  # Köln
    ("Frankfurt", 50.11, 8.68),
    ("Stuttgart", 48.78, 9.18),
    ("Munich", 48.14, 11.58),  # München
    ("Berlin", 52.52, 13.40),
    ("Leipzig", 51.34, 12.37),
    ("Nuremberg", 49.45, 11.08),  # Nürnberg
    ("Rostock", 54.09, 12.13),
]

# Build KDTree for eGon 380kV buses
egon_380_buses = egon_buses[egon_buses['v_nom'] == 380].copy()
egon_coords = egon_380_buses[['x', 'y']].values
egon_tree = cKDTree(egon_coords)

# Find nearest eGon bus for each known location
print("Finding nearest eGon 380kV buses to known major substations:")
print("-" * 80)
print(f"{'City':<20} {'Lat':<10} {'Lon':<10} {'Nearest Bus ID':<20} {'Distance (km)':<15}")
print("-" * 80)

city_to_bus = {}
for city, lat, lon in KNOWN_CORRIDORS:
    # Query nearest bus
    dist, idx = egon_tree.query([lon, lat])
    nearest_bus = egon_380_buses.iloc[idx]
    city_to_bus[city] = nearest_bus['bus_id']
    
    # Convert distance to km (approximate)
    dist_km = dist * 111  # rough conversion from degrees to km
    
    print(f"{city:<20} {lat:<10.2f} {lon:<10.2f} {nearest_bus['bus_id']:<20} {dist_km:<15.1f}")

print()

# Check known corridors
MAJOR_CORRIDORS = [
    ("Hamburg", "Lübeck"),
    ("Hamburg", "Hannover"),
    ("Hannover", "Dortmund"),
    ("Dortmund", "Cologne"),
    ("Frankfurt", "Stuttgart"),
    ("Munich", "Stuttgart"),
    ("Berlin", "Hamburg"),
    ("Berlin", "Leipzig"),
    ("Nuremberg", "Munich"),
    ("Rostock", "Berlin"),
]

print("Checking for major 380kV corridors in eGon:")
print("-" * 100)
print(f"{'Corridor':<40} {'Exists?':<15} {'Direct Lines':<15} {'Total Capacity (MVA)':<20}")
print("-" * 100)

# Get 380kV lines
egon_380_lines = egon_lines[egon_lines['v_nom'] == 380].copy()

for city1, city2 in MAJOR_CORRIDORS:
    bus1 = city_to_bus.get(city1)
    bus2 = city_to_bus.get(city2)
    
    if bus1 is None or bus2 is None:
        print(f"{city1:<20} - {city2:<20} {'N/A':<15} {'N/A':<15} {'N/A':<20}")
        continue
    
    # Check for direct lines
    direct_lines = egon_380_lines[
        ((egon_380_lines['bus0'] == bus1) & (egon_380_lines['bus1'] == bus2)) |
        ((egon_380_lines['bus0'] == bus2) & (egon_380_lines['bus1'] == bus1))
    ]
    
    num_direct = len(direct_lines)
    total_capacity = direct_lines['s_nom'].sum() if num_direct > 0 else 0
    exists = "YES" if num_direct > 0 else "NO"
    
    corridor_name = f"{city1} - {city2}"
    print(f"{corridor_name:<40} {exists:<15} {num_direct:<15} {total_capacity:<20.0f}")

print()

# Check path connectivity between major cities
print("Path connectivity check (shortest path length in 380kV network):")
print("-" * 80)

# Build 380kV graph
G_380 = nx.Graph()
for _, line in egon_380_lines.iterrows():
    G_380.add_edge(line['bus0'], line['bus1'])

print(f"{'Corridor':<40} {'Shortest Path Hops':<20} {'Connected?':<15}")
print("-" * 80)

for city1, city2 in MAJOR_CORRIDORS:
    bus1 = city_to_bus.get(city1)
    bus2 = city_to_bus.get(city2)
    
    if bus1 is None or bus2 is None:
        corridor_name = f"{city1} - {city2}"
        print(f"{corridor_name:<40} {'N/A':<20} {'N/A':<15}")
        continue
    
    try:
        path_length = nx.shortest_path_length(G_380, bus1, bus2)
        corridor_name = f"{city1} - {city2}"
        print(f"{corridor_name:<40} {path_length:<20} {'YES':<15}")
    except nx.NetworkXNoPath:
        corridor_name = f"{city1} - {city2}"
        print(f"{corridor_name:<40} {'No path':<20} {'NO':<15}")

print()

# ============================================================================
# SUMMARY
# ============================================================================
print("="*80)
print("SUMMARY")
print("="*80)
print()

print("Key Findings:")
print()

print("1. CAPACITY:")
total_egon = egon_capacity.sum()
total_jao = jao_capacity.sum()
print(f"   - eGon total capacity: {total_egon:.1f} GVA")
print(f"   - JAO total capacity: {total_jao:.1f} GVA")
print(f"   - Ratio: {total_egon/total_jao:.2f}x")
print()

print("2. MISSING CORRIDORS:")
print(f"   - {len(unmatched_jao)} JAO lines ({100*len(unmatched_jao)/len(jao_lines):.1f}%) have no match in eGon")
print(f"   - Missing capacity: {unmatched_jao['s_nom_3phase'].sum()/1000:.1f} GVA")
print()

print("3. CONNECTIVITY:")
egon_380_status = 'Connected' if egon_380['connected'] else f"{egon_380['num_components']} components"
egon_220_status = 'Connected' if egon_220['connected'] else f"{egon_220['num_components']} components"
jao_400_status = 'Connected' if jao_400['connected'] else f"{jao_400['num_components']} components"
jao_220_status = 'Connected' if jao_220['connected'] else f"{jao_220['num_components']} components"

print(f"   - eGon 380kV: {egon_380_status}")
print(f"   - eGon 220kV: {egon_220_status}")
print(f"   - JAO 400kV: {jao_400_status}")
print(f"   - JAO 220kV: {jao_220_status}")
print()

print("4. KNOWN BACKBONE LINES:")
corridors_found = sum(1 for c1, c2 in MAJOR_CORRIDORS 
                     if len(egon_380_lines[
                         ((egon_380_lines['bus0'] == city_to_bus.get(c1)) & 
                          (egon_380_lines['bus1'] == city_to_bus.get(c2))) |
                         ((egon_380_lines['bus0'] == city_to_bus.get(c2)) & 
                          (egon_380_lines['bus1'] == city_to_bus.get(c1)))
                     ]) > 0)
print(f"   - {corridors_found} / {len(MAJOR_CORRIDORS)} known major corridors have direct 380kV lines")
print()

print("="*80)
print("Analysis complete!")
print("="*80)
