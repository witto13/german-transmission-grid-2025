"""
Compare eGon2025 220/380 kV grid topology against the JAO/CORE-TSO reference network.

Purpose
-------
Performs a detailed structural comparison between the OSM-derived eGon2025
high-voltage grid (220 and 380 kV) and the official JAO/CORE-TSO network
model published by European transmission system operators. The analysis
identifies topological gaps, artifacts, and structural mismatches that must
be corrected before the eGon2025 model can be used for realistic power flow
studies.

Algorithm / Method
------------------
The script runs six sequential analyses, each printed as a numbered section:

1. **Cross-border connections** -- Identifies and compares cross-border
   transmission lines in both networks, broken down by country pair.
2. **Parallel line analysis** -- Counts corridors with multiple parallel
   circuits in eGon vs JAO; reports distribution and top-20 corridors.
3. **Very short lines** -- Flags eGon lines shorter than 1 km and 0.5 km
   as potential modeling artifacts from OSM data processing.
4. **Degree analysis** -- Computes node degree distribution for the eGon
   220/380 kV subnetwork and identifies high-degree hubs vs dead-end buses.
5. **Missing major substations** -- Cross-references JAO bus names against
   eGon bus locations to find JAO substations without a nearby eGon match,
   using results from ``results/jao_matching/bus_match_report.csv``.
6. **Unmatched eGon lines** -- Lists eGon 220/380 kV lines that have no
   corresponding corridor in the JAO dataset.

A final summary section aggregates key metrics from all six analyses.

Inputs
------
- PostgreSQL database (``egon-data`` on port 59734):
  - ``grid.egon_etrago_bus`` (eGon2025, filtered to 220/380 kV)
  - ``grid.egon_etrago_line`` (eGon2025, filtered to 220/380 kV)
- ``data/jao_core_tso/buses.csv`` -- JAO/CORE-TSO bus reference data
- ``data/jao_core_tso/lines.csv`` -- JAO/CORE-TSO line reference data
- ``results/jao_matching/bus_match_report.csv`` -- Pre-computed bus matches
- ``results/jao_matching/line_match_report.csv`` -- Pre-computed line matches

Outputs
-------
- Detailed analysis report printed to stdout (no files written).

Usage
-----
::

    conda activate egon2025
    python analyze_topology_differences.py
"""

import pandas as pd
import numpy as np
from sqlalchemy import create_engine
from collections import Counter

# Database connection
engine = create_engine('postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data')

print("=" * 80)
print("TOPOLOGY ANALYSIS: eGon2025 vs JAO/CORE-TSO Networks")
print("=" * 80)

# Load data from database
print("\nLoading data from database...")
egon_buses = pd.read_sql("""
    SELECT bus_id, v_nom, x, y, country, geom
    FROM grid.egon_etrago_bus
    WHERE scn_name = 'eGon2025' AND v_nom IN (220, 380)
""", engine)

egon_lines = pd.read_sql("""
    SELECT line_id, bus0, bus1, v_nom, s_nom, length, x, r, b, cables, geom
    FROM grid.egon_etrago_line
    WHERE scn_name = 'eGon2025' AND v_nom IN (220, 380)
""", engine)

print(f"Loaded {len(egon_buses):,} eGon buses and {len(egon_lines):,} eGon lines")

# Load JAO data
print("Loading JAO data...")
jao_buses = pd.read_csv('/root/egon_2025_project/data/jao_core_tso/buses.csv')
jao_lines = pd.read_csv('/root/egon_2025_project/data/jao_core_tso/lines.csv')

# Filter JAO to German TSOs and 220/400kV
german_tsos = ['50HERTZ', 'AMPRION', 'TENNET', 'TRANSNETBW']
jao_buses = jao_buses[jao_buses['CORE-TSO_tso'].isin(german_tsos)]
jao_lines = jao_lines[
    (jao_lines['TSO'].isin(german_tsos)) &
    (jao_lines['v_nom'].isin([220, 400]))
]

print(f"Loaded {len(jao_buses):,} JAO buses and {len(jao_lines):,} JAO lines (German TSOs, 220/400kV)")

# Load matching results
bus_match = pd.read_csv('/root/egon_2025_project/results/jao_matching/bus_match_report.csv')
line_match = pd.read_csv('/root/egon_2025_project/results/jao_matching/line_match_report.csv')

print("\n" + "=" * 80)
print("1. CROSS-BORDER CONNECTIONS")
print("=" * 80)

# eGon cross-border lines
egon_buses_dict = egon_buses.set_index('bus_id')['country'].to_dict()
egon_lines['bus0_country'] = egon_lines['bus0'].map(egon_buses_dict)
egon_lines['bus1_country'] = egon_lines['bus1'].map(egon_buses_dict)
egon_cross_border = egon_lines[
    (egon_lines['bus0_country'] != 'DE') | (egon_lines['bus1_country'] != 'DE')
]

print(f"\neGon cross-border lines (at least one bus with country != 'DE'): {len(egon_cross_border)}")
if len(egon_cross_border) > 0:
    print("\nBreakdown by country pair:")
    country_pairs = egon_cross_border.apply(
        lambda row: tuple(sorted([row['bus0_country'], row['bus1_country']])), axis=1
    )
    for pair, count in Counter(country_pairs).most_common():
        print(f"  {pair[0]} <-> {pair[1]}: {count} lines")
    
    print(f"\nSample cross-border lines (first 10):")
    for idx, row in egon_cross_border.head(10).iterrows():
        print(f"  {row['line_id']}: bus{row['bus0']} ({row['bus0_country']}) -> "
              f"bus{row['bus1']} ({row['bus1_country']}), {row['v_nom']}kV, {row['length']:.1f}km")
else:
    print("  Note: eGon2025 scenario appears to be Germany-only (no cross-border lines)")

# JAO cross-border lines
# Create a mapping from bus name to country
jao_bus_country = jao_buses.set_index('name')['CORE-TSO_country'].to_dict()

# Map bus0 and bus1 to countries using CORE-TSO_bus0 and CORE-TSO_bus1
jao_lines['bus0_country'] = jao_lines['CORE-TSO_bus0'].map(jao_bus_country)
jao_lines['bus1_country'] = jao_lines['CORE-TSO_bus1'].map(jao_bus_country)

# Filter out lines where we couldn't determine country (both NaN)
jao_lines_with_country = jao_lines[~(jao_lines['bus0_country'].isna() | jao_lines['bus1_country'].isna())]
jao_cross_border = jao_lines_with_country[jao_lines_with_country['bus0_country'] != jao_lines_with_country['bus1_country']]

print(f"\nJAO cross-border lines: {len(jao_cross_border)}")
if len(jao_cross_border) > 0:
    print("\nBreakdown by country pair:")
    country_pairs = jao_cross_border.apply(
        lambda row: tuple(sorted([row['bus0_country'], row['bus1_country']])), axis=1
    )
    for pair, count in Counter(country_pairs).most_common():
        print(f"  {pair[0]} <-> {pair[1]}: {count} lines")

# Note about missing country info
missing_country = len(jao_lines) - len(jao_lines_with_country)
if missing_country > 0:
    print(f"\nNote: {missing_country} JAO lines have unknown country info")

print("\n" + "=" * 80)
print("2. PARALLEL LINE ANALYSIS")
print("=" * 80)

# eGon parallel lines
egon_lines['corridor'] = egon_lines.apply(
    lambda row: tuple(sorted([row['bus0'], row['bus1']])), axis=1
)
egon_corridor_counts = egon_lines.groupby('corridor').size()
egon_parallel = egon_corridor_counts[egon_corridor_counts > 1]

print(f"\neGon corridors with parallel lines: {len(egon_parallel)}")
print(f"Total eGon corridors: {len(egon_corridor_counts)}")
print(f"Percentage with parallels: {100 * len(egon_parallel) / len(egon_corridor_counts):.1f}%")

# Distribution
parallel_dist = Counter(egon_corridor_counts.values)
print("\nDistribution of lines per corridor:")
for num_lines in sorted(parallel_dist.keys()):
    count = parallel_dist[num_lines]
    if num_lines == 1:
        print(f"  {num_lines} line:  {count:4d} corridors ({100*count/len(egon_corridor_counts):5.1f}%)")
    else:
        print(f"  {num_lines} lines: {count:4d} corridors ({100*count/len(egon_corridor_counts):5.1f}%)")

# Top 20 corridors with most parallel lines
print("\nTop 20 corridors with most parallel lines in eGon:")
top_corridors = egon_corridor_counts.sort_values(ascending=False).head(20)
for corridor, count in top_corridors.items():
    bus0, bus1 = corridor
    # Get voltage level and total length
    corridor_lines = egon_lines[egon_lines['corridor'] == corridor]
    v_nom = corridor_lines.iloc[0]['v_nom']
    total_length = corridor_lines['length'].sum()
    avg_length = corridor_lines['length'].mean()
    print(f"  bus{bus0} <-> bus{bus1}: {count} lines, {v_nom}kV, "
          f"avg {avg_length:.1f}km, total {total_length:.1f}km")

# JAO parallel circuits
jao_lines['corridor'] = jao_lines.apply(
    lambda row: tuple(sorted([row['CORE-TSO_bus0'], row['CORE-TSO_bus1']])), axis=1
)
jao_corridor_counts = jao_lines.groupby('corridor').size()
jao_parallel = jao_corridor_counts[jao_corridor_counts > 1]

print(f"\nJAO corridors with parallel circuits: {len(jao_parallel)}")
print(f"Total JAO corridors: {len(jao_corridor_counts)}")
print(f"Percentage with parallels: {100 * len(jao_parallel) / len(jao_corridor_counts):.1f}%")

parallel_dist_jao = Counter(jao_corridor_counts.values)
print("\nDistribution of circuits per JAO corridor:")
for num_lines in sorted(parallel_dist_jao.keys()):
    count = parallel_dist_jao[num_lines]
    if num_lines == 1:
        print(f"  {num_lines} circuit:  {count:4d} corridors ({100*count/len(jao_corridor_counts):5.1f}%)")
    else:
        print(f"  {num_lines} circuits: {count:4d} corridors ({100*count/len(jao_corridor_counts):5.1f}%)")

print("\n" + "=" * 80)
print("3. VERY SHORT LINES (Potential Artifacts)")
print("=" * 80)

short_1km = egon_lines[egon_lines['length'] < 1.0]
short_500m = egon_lines[egon_lines['length'] < 0.5]

print(f"\neGon lines < 1 km: {len(short_1km)} ({100*len(short_1km)/len(egon_lines):.2f}%)")
print(f"eGon lines < 0.5 km: {len(short_500m)} ({100*len(short_500m)/len(egon_lines):.2f}%)")

if len(short_500m) > 0:
    print(f"\nSample very short lines (< 0.5 km):")
    for idx, row in short_500m.head(20).iterrows():
        print(f"  {row['line_id']}: {row['length']*1000:.0f}m, {row['v_nom']}kV, "
              f"{row['cables']} cables, s_nom={row['s_nom']:.0f}MVA")

# Breakdown by voltage
print("\nBreakdown by voltage level:")
for v_nom in [220, 380]:
    short_v = short_1km[short_1km['v_nom'] == v_nom]
    total_v = egon_lines[egon_lines['v_nom'] == v_nom]
    print(f"  {v_nom}kV: {len(short_v)} lines < 1km ({100*len(short_v)/len(total_v):.2f}%)")

print("\n" + "=" * 80)
print("4. DEGREE ANALYSIS")
print("=" * 80)

# eGon degree distribution
def calculate_degrees(lines_df, bus0_col='bus0', bus1_col='bus1'):
    """Calculate node degrees from line dataframe."""
    from collections import defaultdict
    degree = defaultdict(int)
    for _, row in lines_df.iterrows():
        degree[row[bus0_col]] += 1
        degree[row[bus1_col]] += 1
    return degree

egon_220_lines = egon_lines[egon_lines['v_nom'] == 220]
egon_380_lines = egon_lines[egon_lines['v_nom'] == 380]

egon_220_degree = calculate_degrees(egon_220_lines)
egon_380_degree = calculate_degrees(egon_380_lines)
egon_all_degree = calculate_degrees(egon_lines)

print("\neGon Network Degrees:")
print(f"  220kV: avg degree = {np.mean(list(egon_220_degree.values())):.2f}")
print(f"  380kV: avg degree = {np.mean(list(egon_380_degree.values())):.2f}")
print(f"  Combined: avg degree = {np.mean(list(egon_all_degree.values())):.2f}")

# Degree distribution for combined
degree_dist = Counter(egon_all_degree.values())
print("\neGon degree distribution (220/380kV combined):")
for degree in sorted(degree_dist.keys()):
    count = degree_dist[degree]
    print(f"  Degree {degree}: {count:4d} nodes ({100*count/len(egon_all_degree):5.1f}%)")

# JAO degree distribution
jao_220_lines = jao_lines[jao_lines['v_nom'] == 220]
jao_400_lines = jao_lines[jao_lines['v_nom'] == 400]

jao_220_degree = calculate_degrees(jao_220_lines, 'CORE-TSO_bus0', 'CORE-TSO_bus1')
jao_400_degree = calculate_degrees(jao_400_lines, 'CORE-TSO_bus0', 'CORE-TSO_bus1')
jao_all_degree = calculate_degrees(jao_lines, 'CORE-TSO_bus0', 'CORE-TSO_bus1')

print("\nJAO Network Degrees:")
print(f"  220kV: avg degree = {np.mean(list(jao_220_degree.values())):.2f}")
print(f"  400kV: avg degree = {np.mean(list(jao_400_degree.values())):.2f}")
print(f"  Combined: avg degree = {np.mean(list(jao_all_degree.values())):.2f}")

degree_dist_jao = Counter(jao_all_degree.values())
print("\nJAO degree distribution (220/400kV combined):")
for degree in sorted(degree_dist_jao.keys()):
    count = degree_dist_jao[degree]
    print(f"  Degree {degree}: {count:4d} nodes ({100*count/len(jao_all_degree):5.1f}%)")

print("\n" + "=" * 80)
print("5. MISSING MAJOR SUBSTATIONS")
print("=" * 80)

# High-degree JAO substations
jao_high_degree = {bus: deg for bus, deg in jao_all_degree.items() if deg >= 4}
print(f"\nJAO substations with degree >= 4: {len(jao_high_degree)}")

# Create a reverse mapping from cluster_name to egon bus_id
# The bus_match file has jao_osm_id and cluster_name columns
# We need to match JAO bus names to these cluster names
bus_match_by_cluster = bus_match.set_index('cluster_name')['cluster_osm_id'].to_dict()

# Create JAO bus name to OSM ID mapping
jao_bus_to_osm = jao_buses.set_index('name')['OSM_name'].to_dict()

unmatched_high_degree = []

for jao_bus, degree in sorted(jao_high_degree.items(), key=lambda x: x[1], reverse=True):
    jao_bus_info = jao_buses[jao_buses['name'] == jao_bus]
    if len(jao_bus_info) > 0:
        jao_bus_info = jao_bus_info.iloc[0]
        osm_name = jao_bus_info.get('OSM_name', '')
        
        # Check if this bus is in the match report
        if osm_name not in bus_match_by_cluster:
            unmatched_high_degree.append({
                'jao_bus': jao_bus,
                'osm_name': osm_name,
                'tso': jao_bus_info['CORE-TSO_tso'],
                'degree': degree
            })

if unmatched_high_degree:
    print(f"\nUnmatched high-degree JAO substations: {len(unmatched_high_degree)}")
    for item in unmatched_high_degree[:20]:
        print(f"  {item['jao_bus']} / {item['osm_name']} ({item['tso']}), degree={item['degree']}")
else:
    print("\nAll high-degree JAO substations matched to eGon!")

# Also analyze matched high-degree substations
# Since we don't have a direct mapping, let's just count
matched_count = len(jao_high_degree) - len(unmatched_high_degree)
print(f"\nMatched high-degree JAO substations: {matched_count} / {len(jao_high_degree)}")

print("\n" + "=" * 80)
print("6. UNMATCHED eGon LINES")
print("=" * 80)

# Get matched eGon lines from line_match
# line_match has egon_line_id column - lines with non-null values are matched
matched_egon_segments = set(line_match[~line_match['egon_line_id'].isna()]['egon_line_id'].astype(int))
unmatched_egon_lines = egon_lines[~egon_lines['line_id'].isin(matched_egon_segments)]

print(f"\nUnmatched eGon line segments: {len(unmatched_egon_lines)}")
print(f"Total eGon line segments: {len(egon_lines)}")
print(f"Percentage unmatched: {100*len(unmatched_egon_lines)/len(egon_lines):.1f}%")

total_length = egon_lines['length'].sum()
unmatched_length = unmatched_egon_lines['length'].sum()
print(f"\nTotal eGon network length: {total_length:.1f} km")
print(f"Unmatched eGon length: {unmatched_length:.1f} km")
print(f"Percentage of network length: {100*unmatched_length/total_length:.1f}%")

# Breakdown by voltage
print("\nUnmatched lines by voltage level:")
for v_nom in [220, 380]:
    unmatched_v = unmatched_egon_lines[unmatched_egon_lines['v_nom'] == v_nom]
    total_v = egon_lines[egon_lines['v_nom'] == v_nom]
    if len(total_v) > 0:
        print(f"  {v_nom}kV: {len(unmatched_v)} lines ({100*len(unmatched_v)/len(total_v):.1f}%), "
              f"{unmatched_v['length'].sum():.1f} km ({100*unmatched_v['length'].sum()/total_v['length'].sum():.1f}%)")

# Check if unmatched lines are disproportionately short
matched_egon_lines = egon_lines[egon_lines['line_id'].isin(matched_egon_segments)]
print(f"\nAverage length comparison:")
print(f"  All eGon lines: {egon_lines['length'].mean():.2f} km")
if len(matched_egon_lines) > 0:
    print(f"  Matched eGon lines: {matched_egon_lines['length'].mean():.2f} km")
if len(unmatched_egon_lines) > 0:
    print(f"  Unmatched eGon lines: {unmatched_egon_lines['length'].mean():.2f} km")

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)

print(f"""
Network Topology Comparison:

1. Cross-border:
   - eGon: {len(egon_cross_border)} lines (Germany-only scenario)
   - JAO:  {len(jao_cross_border)} lines

2. Parallel lines:
   - eGon: {len(egon_parallel)} corridors with parallels ({100*len(egon_parallel)/len(egon_corridor_counts):.1f}%)
   - JAO:  {len(jao_parallel)} corridors with parallels ({100*len(jao_parallel)/len(jao_corridor_counts):.1f}%)

3. Short lines:
   - eGon < 1km: {len(short_1km)} lines ({100*len(short_1km)/len(egon_lines):.2f}%)
   - eGon < 0.5km: {len(short_500m)} lines ({100*len(short_500m)/len(egon_lines):.2f}%)

4. Average degree:
   - eGon 220/380kV combined: {np.mean(list(egon_all_degree.values())):.2f}
   - JAO 220/400kV combined: {np.mean(list(jao_all_degree.values())):.2f}

5. High-degree substations:
   - JAO substations with degree >= 4: {len(jao_high_degree)}
   - Unmatched: {len(unmatched_high_degree)}
   - Matched: {matched_count}

6. Unmatched eGon lines:
   - Count: {len(unmatched_egon_lines)} / {len(egon_lines)} ({100*len(unmatched_egon_lines)/len(egon_lines):.1f}%)
   - Length: {unmatched_length:.1f} / {total_length:.1f} km ({100*unmatched_length/total_length:.1f}%)
""")

print("\nAnalysis complete!")
