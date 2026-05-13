#!/usr/bin/env python3
"""
Voltage-specific network reduction: eGon2025v2 -> eGon2025v3 (aggressive clustering).

Purpose
-------
Performs the second stage of network reduction, applying much larger,
voltage-specific clustering radii to the already-reduced ``eGon2025v2``
scenario. This aggressively merges nearby substations at the extra-high
voltage level (where large geographic spacing is expected) while keeping
the 110 kV sub-transmission layer relatively intact.

Algorithm / Method
------------------
1. Copies the ``eGon2025v2`` scenario into a new ``eGon2025v3`` scenario
   in the database.
2. Uses PostGIS ``ST_ClusterDBSCAN`` with voltage-specific radii:
   - 380 kV: 1200 m -- merges EHV substations in close proximity
   - 220 kV: 1200 m -- same treatment for HV transmission
   - 110 kV: 250 m  -- conservative merge for sub-transmission
3. Applies the same topology-protection rules as v2:
   - No merging of two substations or two transformer endpoints.
4. Selects survivor buses using a cascading priority:
   a. Substation buses (linked to OSM substations)
   b. Transformer endpoint buses
   c. Highest-degree node (most line connections)
5. Rewires all lines, transformers, and other components from absorbed
   buses to their cluster's survivor bus.
6. Removes absorbed buses and self-loops.
7. Writes final statistics and cluster mappings to a JSON file.

Inputs
------
- PostgreSQL database (``egon-data`` on port 59734):
  - ``grid.egon_etrago_bus`` and related component tables for ``eGon2025v2``
  - ``osmtgmod_results.bus_data`` for substation identification

Outputs
-------
- Database scenario ``eGon2025v3`` with further-reduced topology
  (typically ~8,750 buses from the ~8,925 in v2).
- ``reduction_info_v3.json`` -- JSON file containing:
  - Timestamp, source/target scenario names, per-voltage merge distances
  - Per-cluster mappings (survivor bus, absorbed buses, voltage level)
  - Final statistics (bus counts by voltage, line and transformer totals)

Usage
-----
::

    conda activate egon2025
    python reduce_network_v3.py
"""

import psycopg2
import json
from collections import defaultdict
from datetime import datetime

# Database connection
DB_CONFIG = {
    'host': '127.0.0.1',
    'port': 59734,
    'database': 'egon-data',
    'user': 'egon',
    'password': 'data'
}

# Voltage-specific merge distances
MERGE_DISTANCES = {
    380: 1200,  # meters
    220: 1200,  # meters
    110: 250    # meters
}

SOURCE_SCENARIO = 'eGon2025v2'  # Start from V2 (already reduced)
TARGET_SCENARIO = 'eGon2025v3'

def get_connection():
    """Create database connection"""
    return psycopg2.connect(**DB_CONFIG)

def copy_scenario(conn):
    """Copy eGon2025 to eGon2025v3"""
    print(f"\n1. Copying {SOURCE_SCENARIO} to {TARGET_SCENARIO}...")
    cur = conn.cursor()

    # Delete existing v3 scenario if it exists
    print("   Deleting existing eGon2025v3 if present...")
    tables = [
        'grid.egon_etrago_bus',
        'grid.egon_etrago_line',
        'grid.egon_etrago_transformer',
        'grid.egon_etrago_generator',
        'grid.egon_etrago_load',
        'grid.egon_etrago_storage',
        'grid.egon_etrago_store',
        'grid.egon_etrago_link'
    ]

    for table in tables:
        cur.execute(f"DELETE FROM {table} WHERE scn_name = %s", (TARGET_SCENARIO,))

    # Copy buses
    print("   Copying buses...")
    cur.execute(f"""
        INSERT INTO grid.egon_etrago_bus
        SELECT
            %s as scn_name,
            bus_id, v_nom, type, carrier, v_mag_pu_set, v_mag_pu_min,
            v_mag_pu_max, x, y, geom, country, geom_3035
        FROM grid.egon_etrago_bus
        WHERE scn_name = %s
    """, (TARGET_SCENARIO, SOURCE_SCENARIO))
    bus_count = cur.rowcount

    # Copy lines
    print("   Copying lines...")
    cur.execute(f"""
        INSERT INTO grid.egon_etrago_line
        SELECT
            %s as scn_name,
            line_id, bus0, bus1, type, carrier, x, r, g, b, s_nom,
            s_nom_extendable, s_nom_min, s_nom_max, s_max_pu, build_year,
            lifetime, capital_cost, length, cables, terrain_factor,
            num_parallel, v_ang_min, v_ang_max, v_nom, geom, topo
        FROM grid.egon_etrago_line
        WHERE scn_name = %s
    """, (TARGET_SCENARIO, SOURCE_SCENARIO))
    line_count = cur.rowcount

    # Copy transformers
    print("   Copying transformers...")
    cur.execute(f"""
        INSERT INTO grid.egon_etrago_transformer
        SELECT
            %s as scn_name,
            trafo_id, bus0, bus1, type, model, x, r, g, b, s_nom,
            s_nom_extendable, s_nom_min, s_nom_max, s_max_pu, tap_ratio,
            tap_side, tap_position, phase_shift, build_year, lifetime,
            v_ang_min, v_ang_max, capital_cost, num_parallel, geom, topo
        FROM grid.egon_etrago_transformer
        WHERE scn_name = %s
    """, (TARGET_SCENARIO, SOURCE_SCENARIO))
    trafo_count = cur.rowcount

    conn.commit()
    print(f"   ✓ Copied {bus_count} buses, {line_count} lines, {trafo_count} transformers")
    return bus_count, line_count, trafo_count

def find_clusters(conn):
    """Find clusters using voltage-specific distances"""
    print(f"\n2. Finding bus clusters with voltage-specific distances...")
    print(f"   380 kV: {MERGE_DISTANCES[380]}m radius")
    print(f"   220 kV: {MERGE_DISTANCES[220]}m radius")
    print(f"   110 kV: {MERGE_DISTANCES[110]}m radius")
    cur = conn.cursor()

    # Get substation buses
    cur.execute("""
        SELECT DISTINCT eb.bus_id
        FROM grid.egon_etrago_bus eb
        JOIN osmtgmod_results.bus_data bd ON eb.bus_id = bd.bus_i
        WHERE eb.scn_name = %s AND bd.osm_substation_id IS NOT NULL
    """, (TARGET_SCENARIO,))
    substation_buses = set(row[0] for row in cur.fetchall())

    # Get transformer buses
    cur.execute("""
        SELECT DISTINCT bus_id
        FROM (
            SELECT bus0 as bus_id FROM grid.egon_etrago_transformer WHERE scn_name = %s
            UNION
            SELECT bus1 as bus_id FROM grid.egon_etrago_transformer WHERE scn_name = %s
        ) t
    """, (TARGET_SCENARIO, TARGET_SCENARIO))
    transformer_buses = set(row[0] for row in cur.fetchall())

    print(f"   Found {len(substation_buses)} substation buses")
    print(f"   Found {len(transformer_buses)} transformer buses")

    # Get node degrees (number of connections)
    cur.execute("""
        SELECT bus_id, degree FROM (
            SELECT bus0 as bus_id, COUNT(*) as degree
            FROM grid.egon_etrago_line
            WHERE scn_name = %s
            GROUP BY bus0
            UNION ALL
            SELECT bus1 as bus_id, COUNT(*) as degree
            FROM grid.egon_etrago_line
            WHERE scn_name = %s
            GROUP BY bus1
        ) t
    """, (TARGET_SCENARIO, TARGET_SCENARIO))

    node_degrees = defaultdict(int)
    for bus_id, degree in cur.fetchall():
        node_degrees[bus_id] += degree

    # Cluster by voltage level with specific distances
    clusters = {}

    for v_nom in [380, 220, 110]:
        merge_distance = MERGE_DISTANCES[v_nom]
        print(f"   Processing {v_nom} kV buses (radius: {merge_distance}m)...")

        # Find clusters using DBSCAN
        cur.execute("""
            WITH clustered AS (
                SELECT
                    bus_id,
                    ST_ClusterDBSCAN(ST_Transform(geom, 3035), eps := %s, minpoints := 2)
                        OVER (ORDER BY bus_id) as cluster_id
                FROM grid.egon_etrago_bus
                WHERE scn_name = %s
                  AND v_nom = %s
                  AND geom IS NOT NULL
            )
            SELECT cluster_id, array_agg(bus_id ORDER BY bus_id) as bus_ids
            FROM clustered
            WHERE cluster_id IS NOT NULL
            GROUP BY cluster_id
            HAVING COUNT(*) > 1
            ORDER BY cluster_id
        """, (merge_distance, TARGET_SCENARIO, v_nom))

        raw_clusters = cur.fetchall()

        # Filter based on rules (don't merge multiple substations or transformers)
        valid_clusters = []
        filtered_count = 0

        for cluster_id, bus_ids in raw_clusters:
            cluster = list(bus_ids)

            substations_in_cluster = [b for b in cluster if b in substation_buses]
            transformers_in_cluster = [b for b in cluster if b in transformer_buses]

            # Skip if 2+ substations or 2+ transformer buses
            if len(substations_in_cluster) >= 2 or len(transformers_in_cluster) >= 2:
                filtered_count += 1
                continue

            valid_clusters.append(cluster)

        if valid_clusters:
            clusters[v_nom] = valid_clusters
            total_in_clusters = sum(len(c) for c in clusters[v_nom])
            print(f"      ✓ Found {len(clusters[v_nom])} valid clusters with {total_in_clusters} buses")
            if filtered_count > 0:
                print(f"      ℹ Filtered out {filtered_count} clusters (multiple substations/transformers)")
        else:
            print(f"      No valid clusters found")

    return clusters, substation_buses, transformer_buses, node_degrees

def merge_clusters(conn, clusters, substation_buses, transformer_buses, node_degrees):
    """Merge buses with priority-based keeper selection"""
    print(f"\n3. Merging clustered buses...")
    print(f"   Priority 1: Substations/Stations/Plants")
    print(f"   Priority 2: Highest degree node")
    cur = conn.cursor()

    merge_info = []
    total_merged = 0

    for v_nom, voltage_clusters in clusters.items():
        print(f"   Processing {v_nom} kV clusters...")

        for cluster in voltage_clusters:
            if len(cluster) < 2:
                continue

            # Select keeper based on priority
            keeper_id = None

            # Priority 1: Substation or transformer bus
            substations_in_cluster = [b for b in cluster if b in substation_buses]
            transformers_in_cluster = [b for b in cluster if b in transformer_buses]

            if substations_in_cluster:
                keeper_id = substations_in_cluster[0]
            elif transformers_in_cluster:
                keeper_id = transformers_in_cluster[0]

            # Priority 2: Highest degree node
            if keeper_id is None:
                # Find node with highest degree
                max_degree = -1
                for bus_id in cluster:
                    degree = node_degrees.get(bus_id, 0)
                    if degree > max_degree:
                        max_degree = degree
                        keeper_id = bus_id

            # Fallback
            if keeper_id is None:
                keeper_id = cluster[0]

            to_merge = [b for b in cluster if b != keeper_id]
            if not to_merge:
                continue

            # Get keeper location
            cur.execute("""
                SELECT ST_X(geom) as lon, ST_Y(geom) as lat,
                       ST_X(ST_Transform(geom, 3035)) as x_3035,
                       ST_Y(ST_Transform(geom, 3035)) as y_3035
                FROM grid.egon_etrago_bus
                WHERE scn_name = %s AND bus_id = %s
            """, (TARGET_SCENARIO, keeper_id))

            keeper_loc = cur.fetchone()
            if not keeper_loc:
                continue

            # Store merge info
            merge_info.append({
                'keeper_id': keeper_id,
                'merged_ids': to_merge,
                'v_nom': v_nom,
                'lon': keeper_loc[0],
                'lat': keeper_loc[1],
                'x_3035': keeper_loc[2],
                'y_3035': keeper_loc[3],
                'keeper_degree': node_degrees.get(keeper_id, 0),
                'is_substation': keeper_id in substation_buses,
                'is_transformer': keeper_id in transformer_buses
            })

            # Update line connections
            cur.execute("""
                UPDATE grid.egon_etrago_line
                SET bus0 = %s
                WHERE scn_name = %s AND bus0 = ANY(%s)
            """, (keeper_id, TARGET_SCENARIO, to_merge))

            cur.execute("""
                UPDATE grid.egon_etrago_line
                SET bus1 = %s
                WHERE scn_name = %s AND bus1 = ANY(%s)
            """, (keeper_id, TARGET_SCENARIO, to_merge))

            # Update transformer connections
            cur.execute("""
                UPDATE grid.egon_etrago_transformer
                SET bus0 = %s
                WHERE scn_name = %s AND bus0 = ANY(%s)
            """, (keeper_id, TARGET_SCENARIO, to_merge))

            cur.execute("""
                UPDATE grid.egon_etrago_transformer
                SET bus1 = %s
                WHERE scn_name = %s AND bus1 = ANY(%s)
            """, (keeper_id, TARGET_SCENARIO, to_merge))

            # Delete self-loops
            cur.execute("""
                DELETE FROM grid.egon_etrago_line
                WHERE scn_name = %s AND bus0 = bus1
            """, (TARGET_SCENARIO,))

            # Delete merged buses
            cur.execute("""
                DELETE FROM grid.egon_etrago_bus
                WHERE scn_name = %s AND bus_id = ANY(%s)
            """, (TARGET_SCENARIO, to_merge))

            total_merged += len(to_merge)

            if len(merge_info) % 100 == 0:
                print(f"      Processed {len(merge_info)} clusters...")

        print(f"   ✓ Merged {total_merged} buses in {len(voltage_clusters)} clusters")

    conn.commit()
    return merge_info

def get_final_stats(conn):
    """Get statistics for the reduced network"""
    print(f"\n4. Getting final statistics...")
    cur = conn.cursor()

    # Count buses
    cur.execute("""
        SELECT v_nom, COUNT(*)
        FROM grid.egon_etrago_bus
        WHERE scn_name = %s
        GROUP BY v_nom
        ORDER BY v_nom DESC
    """, (TARGET_SCENARIO,))
    bus_stats = cur.fetchall()

    # Count lines
    cur.execute("""
        SELECT COUNT(*)
        FROM grid.egon_etrago_line
        WHERE scn_name = %s
    """, (TARGET_SCENARIO,))
    line_count = cur.fetchone()[0]

    # Count transformers
    cur.execute("""
        SELECT COUNT(*)
        FROM grid.egon_etrago_transformer
        WHERE scn_name = %s
    """, (TARGET_SCENARIO,))
    trafo_count = cur.fetchone()[0]

    print("   Final network:")
    total_buses = 0
    for v_nom, count in bus_stats:
        print(f"      {v_nom} kV: {count} buses")
        total_buses += count
    print(f"      Total: {total_buses} buses")
    print(f"      Lines: {line_count}")
    print(f"      Transformers: {trafo_count}")

    return {
        'buses_by_voltage': dict(bus_stats),
        'total_buses': total_buses,
        'lines': line_count,
        'transformers': trafo_count
    }

def save_merge_info(merge_info, stats):
    """Save merge information to JSON file"""
    print(f"\n5. Saving merge information...")

    output_data = {
        'timestamp': datetime.now().isoformat(),
        'source_scenario': SOURCE_SCENARIO,
        'target_scenario': TARGET_SCENARIO,
        'merge_distances': MERGE_DISTANCES,
        'statistics': stats,
        'reductions': merge_info
    }

    output_file = '/root/egon_2025_project/reduction_info_v3.json'
    with open(output_file, 'w') as f:
        json.dump(output_data, f, indent=2)

    print(f"   ✓ Saved to {output_file}")
    print(f"   Total reduction areas: {len(merge_info)}")

def main():
    """Main reduction workflow"""
    print("=" * 70)
    print("Network Reduction V3: eGon2025 → eGon2025v3")
    print(f"Merge distances: 380/220kV={MERGE_DISTANCES[380]}m, 110kV={MERGE_DISTANCES[110]}m")
    print("=" * 70)

    conn = get_connection()

    try:
        # Step 1: Copy scenario
        copy_scenario(conn)

        # Step 2: Find clusters
        clusters, substation_buses, transformer_buses, node_degrees = find_clusters(conn)

        if not clusters:
            print("\n✗ No clusters found. Network unchanged.")
            return

        # Step 3: Merge clusters
        merge_info = merge_clusters(conn, clusters, substation_buses, transformer_buses, node_degrees)

        # Step 4: Get final statistics
        stats = get_final_stats(conn)

        # Step 5: Save merge information
        save_merge_info(merge_info, stats)

        print("\n" + "=" * 70)
        print("✓ Reduction V3 complete!")
        print(f"  Merged {len(merge_info)} areas")
        print(f"  V1 Original: 14,494 buses")
        print(f"  V2 Base: 11,575 buses")
        print(f"  V3 Final: {stats['total_buses']} buses")
        print(f"  Reduction from V2: {11575 - stats['total_buses']} buses ({100*(11575-stats['total_buses'])/11575:.1f}%)")
        print(f"  Total reduction from V1: {14494 - stats['total_buses']} buses ({100*(14494-stats['total_buses'])/14494:.1f}%)")
        print("=" * 70)

    except Exception as e:
        conn.rollback()
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        conn.close()

if __name__ == '__main__':
    main()
