#!/usr/bin/env python3
"""
Substation Simplification — eGon2025 → eGon2025v2
===================================================

Collapses intra-substation bus nodes into single representative buses per
voltage level.  OSM models substations with many internal nodes (bus-bar
segments, switchgear connections) that create thousands of short lines.
JAO/CORE-TSO treats each substation as a single node, so we do the same.

Algorithm:
  1. Spatial clustering per voltage level (Union-Find + KDTree)
  2. Split each spatial cluster by graph connectivity (BFS)
  3. Pick highest-degree bus as representative per connected sub-cluster
  4. Remap all endpoints, delete self-loops and orphaned buses

Safety: After spatial clustering, each cluster is split into connected
components using only lines within the cluster.  This prevents merging
spatially close but topologically disconnected buses.

Usage:
    python scripts/simplify_substations.py --dry-run
    python scripts/simplify_substations.py --apply
    python scripts/simplify_substations.py --apply --source eGon2025 --target eGon2025v2
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from sqlalchemy import create_engine, text

# Add project root so we can import from scripts.lib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scripts.lib.node_mapping import NodeMapping

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
KM_PER_DEG_LAT = 111.0
KM_PER_DEG_LON = 71.5  # cos(52°) * 111

DEFAULT_SOURCE = 'eGon2025'
DEFAULT_TARGET = 'eGon2025v2'

# Default clustering radii (meters)
DEFAULT_RADIUS_110 = 200
DEFAULT_RADIUS_220 = 1000
DEFAULT_RADIUS_380 = 1000

# All grid tables that need scenario copying
GRID_TABLES = [
    'grid.egon_etrago_bus',
    'grid.egon_etrago_line',
    'grid.egon_etrago_transformer',
    'grid.egon_etrago_generator',
    'grid.egon_etrago_load',
    'grid.egon_etrago_storage',
    'grid.egon_etrago_store',
    'grid.egon_etrago_link',
]

# Tables with a 'bus' column (single-bus reference)
SINGLE_BUS_TABLES = [
    ('grid.egon_etrago_generator', 'bus'),
    ('grid.egon_etrago_load', 'bus'),
    ('grid.egon_etrago_storage', 'bus'),
    ('grid.egon_etrago_store', 'bus'),
]

# Tables with bus0/bus1 columns (two-bus reference)
DUAL_BUS_TABLES = [
    'grid.egon_etrago_line',
    'grid.egon_etrago_transformer',
    'grid.egon_etrago_link',
]


# ---------------------------------------------------------------------------
# Union-Find (reused from jao_matching.py)
# ---------------------------------------------------------------------------
class UnionFind:
    """Union-Find data structure for spatial clustering."""
    def __init__(self):
        self.parent = {}
        self.rank = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


# ---------------------------------------------------------------------------
# Core algorithm functions (testable without DB)
# ---------------------------------------------------------------------------

def spatial_cluster(buses_df, radii):
    """
    Spatial clustering per voltage level using Union-Find + KDTree.

    Args:
        buses_df: DataFrame with columns [bus_id, v_nom, x, y]
        radii: dict mapping v_nom -> radius_meters, e.g. {110: 200, 220: 1000, 380: 1000}

    Returns:
        dict mapping bus_id -> cluster_root_id (from Union-Find)
    """
    uf = UnionFind()

    for voltage, radius_m in radii.items():
        vbuses = buses_df[buses_df['v_nom'] == voltage]
        if len(vbuses) < 2:
            # Still initialise singletons
            for bid in vbuses['bus_id'].values:
                uf.find(bid)
            continue

        bus_ids = vbuses['bus_id'].values
        coords_km = np.column_stack([
            vbuses['x'].values * KM_PER_DEG_LON,
            vbuses['y'].values * KM_PER_DEG_LAT,
        ])
        tree = cKDTree(coords_km)
        pairs = tree.query_pairs(radius_m / 1000.0)

        # Initialise all buses
        for bid in bus_ids:
            uf.find(bid)
        # Union close pairs
        for i, j in pairs:
            uf.union(bus_ids[i], bus_ids[j])

    # Build cluster map for ALL buses (including those not in radii dict)
    cluster_map = {}
    for bid in buses_df['bus_id'].values:
        cluster_map[bid] = uf.find(bid)

    return cluster_map


def split_by_connectivity(cluster_map, lines_df):
    """
    Split spatial clusters into connected sub-clusters.

    For each spatial cluster with >1 member, build a sub-graph from lines
    where BOTH endpoints are in the cluster, then find connected components.

    Args:
        cluster_map: dict bus_id -> cluster_root_id
        lines_df: DataFrame with columns [bus0, bus1]

    Returns:
        list of sets, each set being a connected sub-cluster of bus_ids
    """
    # Group buses by cluster root
    clusters = defaultdict(set)
    for bid, root in cluster_map.items():
        clusters[root].add(bid)

    # Pre-filter: find lines where both endpoints share the same cluster root
    # This avoids iterating all lines for each cluster
    bus0_arr = lines_df['bus0'].values.astype(int)
    bus1_arr = lines_df['bus1'].values.astype(int)

    # Map each line endpoint to its cluster root
    bus0_roots = np.array([cluster_map.get(b, -1) for b in bus0_arr])
    bus1_roots = np.array([cluster_map.get(b, -1) for b in bus1_arr])

    # Lines where both endpoints are in the same cluster AND not self-loops
    intra_mask = (bus0_roots == bus1_roots) & (bus0_arr != bus1_arr)
    intra_bus0 = bus0_arr[intra_mask]
    intra_bus1 = bus1_arr[intra_mask]
    intra_roots = bus0_roots[intra_mask]

    # Build per-cluster adjacency from pre-filtered intra-cluster lines
    cluster_adj = defaultdict(lambda: defaultdict(set))
    for b0, b1, root in zip(intra_bus0, intra_bus1, intra_roots):
        cluster_adj[root][b0].add(b1)
        cluster_adj[root][b1].add(b0)

    merge_groups = []

    for root, members in clusters.items():
        if len(members) == 1:
            continue

        adj = cluster_adj.get(root, {})

        # BFS to find connected components
        visited = set()
        for bus in members:
            if bus in visited:
                continue
            component = set()
            queue = [bus]
            while queue:
                node = queue.pop()
                if node in visited:
                    continue
                visited.add(node)
                component.add(node)
                for neighbour in adj.get(node, set()):
                    if neighbour not in visited:
                        queue.append(neighbour)

            if len(component) > 1:
                merge_groups.append(component)

    return merge_groups


def build_mapping(merge_groups, lines_df):
    """
    Build bus mapping: for each merge group, pick the highest-degree bus
    as representative.

    Args:
        merge_groups: list of sets of bus_ids
        lines_df: DataFrame with columns [bus0, bus1] (for degree computation)

    Returns:
        NodeMapping object
    """
    # Compute line degree for all buses (vectorized)
    bus0_counts = pd.Series(lines_df['bus0'].values.astype(int)).value_counts()
    bus1_counts = pd.Series(lines_df['bus1'].values.astype(int)).value_counts()
    degree = (bus0_counts.add(bus1_counts, fill_value=0)).astype(int).to_dict()

    mapping = NodeMapping()

    for group in merge_groups:
        # Pick representative: highest degree, tiebreak lowest bus_id
        rep = max(group, key=lambda b: (degree.get(b, 0), -b))

        for bid in group:
            if bid != rep:
                mapping.add_mapping(
                    bid, rep,
                    reason='substation_simplification',
                    voltage=None,  # filled below if available
                )

    return mapping


def count_connected_components(buses_set, lines_df, trafos_df):
    """
    Count connected components in the graph defined by buses, lines and
    transformers.

    Args:
        buses_set: set of bus_ids
        lines_df: DataFrame with [bus0, bus1]
        trafos_df: DataFrame with [bus0, bus1]

    Returns:
        int: number of connected components
    """
    # Build adjacency using vectorized extraction
    adj = defaultdict(set)

    def _add_edges(df):
        if len(df) == 0:
            return
        b0_arr = df['bus0'].values.astype(int)
        b1_arr = df['bus1'].values.astype(int)
        for b0, b1 in zip(b0_arr, b1_arr):
            if b0 in buses_set and b1 in buses_set:
                adj[b0].add(b1)
                adj[b1].add(b0)

    _add_edges(lines_df)
    _add_edges(trafos_df)

    visited = set()
    components = 0
    for bus in buses_set:
        if bus in visited:
            continue
        components += 1
        queue = [bus]
        while queue:
            node = queue.pop()
            if node in visited:
                continue
            visited.add(node)
            for nb in adj.get(node, set()):
                if nb not in visited:
                    queue.append(nb)

    return components


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def copy_scenario(engine, source, target):
    """Copy source scenario to target across all grid tables."""
    print(f"\n{'='*70}")
    print(f"Phase 1: Copy scenario {source} -> {target}")
    print(f"{'='*70}")

    with engine.begin() as conn:
        # Delete existing target data
        for table in GRID_TABLES:
            conn.execute(text(f"DELETE FROM {table} WHERE scn_name = :scn"),
                         {'scn': target})

        # Copy bus
        r = conn.execute(text("""
            INSERT INTO grid.egon_etrago_bus
            SELECT :tgt as scn_name,
                bus_id, v_nom, type, carrier, v_mag_pu_set, v_mag_pu_min,
                v_mag_pu_max, x, y, geom, country, geom_3035
            FROM grid.egon_etrago_bus WHERE scn_name = :src
        """), {'tgt': target, 'src': source})
        bus_count = r.rowcount
        print(f"  Buses:        {bus_count:,}")

        # Copy line
        r = conn.execute(text("""
            INSERT INTO grid.egon_etrago_line
            SELECT :tgt as scn_name,
                line_id, bus0, bus1, type, carrier, x, r, g, b, s_nom,
                s_nom_extendable, s_nom_min, s_nom_max, s_max_pu, build_year,
                lifetime, capital_cost, length, cables, terrain_factor,
                num_parallel, v_ang_min, v_ang_max, v_nom, geom, topo
            FROM grid.egon_etrago_line WHERE scn_name = :src
        """), {'tgt': target, 'src': source})
        line_count = r.rowcount
        print(f"  Lines:        {line_count:,}")

        # Copy transformer
        r = conn.execute(text("""
            INSERT INTO grid.egon_etrago_transformer
            SELECT :tgt as scn_name,
                trafo_id, bus0, bus1, type, model, x, r, g, b, s_nom,
                s_nom_extendable, s_nom_min, s_nom_max, s_max_pu, tap_ratio,
                tap_side, tap_position, phase_shift, build_year, lifetime,
                v_ang_min, v_ang_max, capital_cost, num_parallel, geom, topo
            FROM grid.egon_etrago_transformer WHERE scn_name = :src
        """), {'tgt': target, 'src': source})
        trafo_count = r.rowcount
        print(f"  Transformers: {trafo_count:,}")

        # Copy generator
        r = conn.execute(text("""
            INSERT INTO grid.egon_etrago_generator
            SELECT :tgt as scn_name,
                generator_id, bus, control, type, carrier, p_nom, p_nom_extendable,
                p_nom_min, p_nom_max, p_min_pu, p_max_pu, p_set, q_set, sign,
                marginal_cost, build_year, lifetime, capital_cost, efficiency,
                committable, start_up_cost, shut_down_cost, min_up_time,
                min_down_time, up_time_before, down_time_before,
                ramp_limit_up, ramp_limit_down, ramp_limit_start_up,
                ramp_limit_shut_down, e_nom_max
            FROM grid.egon_etrago_generator WHERE scn_name = :src
        """), {'tgt': target, 'src': source})
        gen_count = r.rowcount
        print(f"  Generators:   {gen_count:,}")

        # Copy load
        r = conn.execute(text("""
            INSERT INTO grid.egon_etrago_load
            SELECT :tgt as scn_name,
                load_id, bus, type, carrier, p_set, q_set, sign
            FROM grid.egon_etrago_load WHERE scn_name = :src
        """), {'tgt': target, 'src': source})
        load_count = r.rowcount
        print(f"  Loads:        {load_count:,}")

        # Copy storage
        r = conn.execute(text("""
            INSERT INTO grid.egon_etrago_storage
            SELECT :tgt as scn_name,
                storage_id, bus, control, type, carrier, p_nom, p_nom_extendable,
                p_nom_min, p_nom_max, p_min_pu, p_max_pu, p_set, q_set, sign,
                marginal_cost, capital_cost, build_year, lifetime,
                state_of_charge_initial, cyclic_state_of_charge,
                state_of_charge_set, max_hours, efficiency_store,
                efficiency_dispatch, standing_loss, inflow
            FROM grid.egon_etrago_storage WHERE scn_name = :src
        """), {'tgt': target, 'src': source})
        storage_count = r.rowcount
        print(f"  Storage:      {storage_count:,}")

        # Copy store
        r = conn.execute(text("""
            INSERT INTO grid.egon_etrago_store
            SELECT :tgt as scn_name,
                store_id, bus, type, carrier, e_nom, e_nom_extendable, e_nom_min,
                e_nom_max, e_min_pu, e_max_pu, p_set, q_set, e_initial, e_cyclic,
                sign, marginal_cost, capital_cost, standing_loss, build_year,
                lifetime
            FROM grid.egon_etrago_store WHERE scn_name = :src
        """), {'tgt': target, 'src': source})
        store_count = r.rowcount
        print(f"  Stores:       {store_count:,}")

        # Copy link
        r = conn.execute(text("""
            INSERT INTO grid.egon_etrago_link
            SELECT :tgt as scn_name,
                link_id, bus0, bus1, type, carrier, efficiency, build_year,
                lifetime, p_nom, p_nom_extendable, p_nom_min, p_nom_max,
                p_min_pu, p_max_pu, p_set, capital_cost, marginal_cost,
                length, terrain_factor, geom, topo
            FROM grid.egon_etrago_link WHERE scn_name = :src
        """), {'tgt': target, 'src': source})
        link_count = r.rowcount
        print(f"  Links:        {link_count:,}")

    return {
        'buses': bus_count, 'lines': line_count, 'transformers': trafo_count,
        'generators': gen_count, 'loads': load_count, 'storage': storage_count,
        'stores': store_count, 'links': link_count,
    }


def load_data(engine, scenario):
    """Load buses and lines from target scenario."""
    print(f"\n{'='*70}")
    print(f"Phase 2: Load data from {scenario}")
    print(f"{'='*70}")

    buses = pd.read_sql(text(
        "SELECT bus_id, v_nom, x, y FROM grid.egon_etrago_bus WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    print(f"  Buses: {len(buses):,}")

    lines = pd.read_sql(text(
        "SELECT line_id, bus0, bus1 FROM grid.egon_etrago_line WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    print(f"  Lines: {len(lines):,}")

    trafos = pd.read_sql(text(
        "SELECT trafo_id, bus0, bus1 FROM grid.egon_etrago_transformer WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    print(f"  Transformers: {len(trafos):,}")

    return buses, lines, trafos


def apply_remapping(engine, scenario, mapping):
    """Apply bus remapping to all component tables via SQL UPDATEs."""
    print(f"\n{'='*70}")
    print(f"Phase 6: Apply remapping ({len(mapping.removed_nodes)} bus merges)")
    print(f"{'='*70}")

    # Build old->new pairs (cast to Python int for psycopg2 compatibility)
    remap_pairs = {}
    for old_id in mapping.removed_nodes:
        remap_pairs[int(old_id)] = int(mapping.map(old_id))

    if not remap_pairs:
        print("  No remapping needed.")
        return

    with engine.begin() as conn:
        total_updates = 0

        # Dual-bus tables: bus0 and bus1
        for table in DUAL_BUS_TABLES:
            for col in ['bus0', 'bus1']:
                for old_id, new_id in remap_pairs.items():
                    r = conn.execute(text(
                        f"UPDATE {table} SET {col} = :new_id "
                        f"WHERE scn_name = :scn AND {col} = :old_id"
                    ), {'new_id': new_id, 'old_id': old_id, 'scn': scenario})
                    total_updates += r.rowcount

        # Single-bus tables
        for table, col in SINGLE_BUS_TABLES:
            for old_id, new_id in remap_pairs.items():
                r = conn.execute(text(
                    f"UPDATE {table} SET {col} = :new_id "
                    f"WHERE scn_name = :scn AND {col} = :old_id"
                ), {'new_id': new_id, 'old_id': old_id, 'scn': scenario})
                total_updates += r.rowcount

        print(f"  Total row updates: {total_updates:,}")


def delete_self_loops(engine, scenario):
    """Delete lines and transformers where bus0 = bus1."""
    print(f"\n{'='*70}")
    print(f"Phase 7: Delete self-loops")
    print(f"{'='*70}")

    with engine.begin() as conn:
        r = conn.execute(text(
            "DELETE FROM grid.egon_etrago_line "
            "WHERE scn_name = :scn AND bus0 = bus1"
        ), {'scn': scenario})
        lines_deleted = r.rowcount
        print(f"  Lines deleted (self-loops):  {lines_deleted:,}")

        r = conn.execute(text(
            "DELETE FROM grid.egon_etrago_transformer "
            "WHERE scn_name = :scn AND bus0 = bus1"
        ), {'scn': scenario})
        trafos_deleted = r.rowcount
        print(f"  Trafos deleted (self-loops): {trafos_deleted:,}")

    return lines_deleted, trafos_deleted


def delete_orphaned_buses(engine, scenario):
    """Remove buses not referenced by any component."""
    print(f"\n{'='*70}")
    print(f"Phase 8: Delete orphaned buses")
    print(f"{'='*70}")

    with engine.begin() as conn:
        r = conn.execute(text("""
            DELETE FROM grid.egon_etrago_bus
            WHERE scn_name = :scn AND bus_id NOT IN (
                SELECT bus0 FROM grid.egon_etrago_line WHERE scn_name = :scn
                UNION SELECT bus1 FROM grid.egon_etrago_line WHERE scn_name = :scn
                UNION SELECT bus0 FROM grid.egon_etrago_transformer WHERE scn_name = :scn
                UNION SELECT bus1 FROM grid.egon_etrago_transformer WHERE scn_name = :scn
                UNION SELECT bus FROM grid.egon_etrago_generator WHERE scn_name = :scn
                UNION SELECT bus FROM grid.egon_etrago_load WHERE scn_name = :scn
                UNION SELECT bus FROM grid.egon_etrago_storage WHERE scn_name = :scn
                UNION SELECT bus FROM grid.egon_etrago_store WHERE scn_name = :scn
                UNION SELECT bus0 FROM grid.egon_etrago_link WHERE scn_name = :scn
                UNION SELECT bus1 FROM grid.egon_etrago_link WHERE scn_name = :scn
            )
        """), {'scn': scenario})
        buses_deleted = r.rowcount
        print(f"  Orphaned buses deleted: {buses_deleted:,}")

    return buses_deleted


def validate(engine, scenario, pre_components, mapping):
    """Post-simplification validation."""
    print(f"\n{'='*70}")
    print(f"Phase 9: Validation")
    print(f"{'='*70}")

    buses, lines, trafos = load_data(engine, scenario)
    buses_set = set(buses['bus_id'].values)
    post_components = count_connected_components(buses_set, lines, trafos)

    # Load voltage info for validation
    bus_vnom = dict(zip(buses['bus_id'], buses['v_nom']))

    issues = []

    # 1. Connected components preserved
    if post_components != pre_components:
        issues.append(
            f"Connected components changed: {pre_components} -> {post_components}")
    print(f"  Connected components: {pre_components} -> {post_components} "
          f"{'OK' if post_components == pre_components else 'MISMATCH!'}")

    # 2. No self-loops
    self_loop_lines = len(lines[lines['bus0'] == lines['bus1']])
    self_loop_trafos = len(trafos[trafos['bus0'] == trafos['bus1']])
    if self_loop_lines > 0:
        issues.append(f"{self_loop_lines} line self-loops remain")
    if self_loop_trafos > 0:
        issues.append(f"{self_loop_trafos} transformer self-loops remain")
    print(f"  Self-loops: lines={self_loop_lines}, trafos={self_loop_trafos} "
          f"{'OK' if self_loop_lines == 0 and self_loop_trafos == 0 else 'FAIL!'}")

    # 3. Lines connect same-voltage buses
    bad_lines = 0
    for _, row in lines.iterrows():
        v0 = bus_vnom.get(int(row['bus0']))
        v1 = bus_vnom.get(int(row['bus1']))
        if v0 is not None and v1 is not None and v0 != v1:
            bad_lines += 1
    if bad_lines > 0:
        issues.append(f"{bad_lines} lines connect different-voltage buses")
    print(f"  Lines with voltage mismatch: {bad_lines} "
          f"{'OK' if bad_lines == 0 else 'FAIL!'}")

    # 4. Transformers connect different-voltage buses
    bad_trafos = 0
    for _, row in trafos.iterrows():
        v0 = bus_vnom.get(int(row['bus0']))
        v1 = bus_vnom.get(int(row['bus1']))
        if v0 is not None and v1 is not None and v0 == v1:
            bad_trafos += 1
    if bad_trafos > 0:
        issues.append(f"{bad_trafos} transformers connect same-voltage buses")
    print(f"  Transformers with same voltage: {bad_trafos} "
          f"{'OK' if bad_trafos == 0 else 'WARN'}")

    # 5. No orphaned buses (re-check)
    referenced = set()
    for _, row in lines.iterrows():
        referenced.add(int(row['bus0']))
        referenced.add(int(row['bus1']))
    for _, row in trafos.iterrows():
        referenced.add(int(row['bus0']))
        referenced.add(int(row['bus1']))
    # Also check generators, loads, etc.
    for tbl, col in SINGLE_BUS_TABLES:
        df = pd.read_sql(text(
            f"SELECT {col} FROM {tbl} WHERE scn_name = :scn"
        ), engine, params={'scn': scenario})
        referenced.update(df[col].astype(int).values)
    # Check links
    links = pd.read_sql(text(
        "SELECT bus0, bus1 FROM grid.egon_etrago_link WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    if len(links) > 0:
        referenced.update(links['bus0'].astype(int).values)
        referenced.update(links['bus1'].astype(int).values)

    orphans = buses_set - referenced
    if orphans:
        issues.append(f"{len(orphans)} orphaned buses remain")
    print(f"  Orphaned buses: {len(orphans)} "
          f"{'OK' if len(orphans) == 0 else 'FAIL!'}")

    return {
        'pre_components': pre_components,
        'post_components': post_components,
        'self_loop_lines': self_loop_lines,
        'self_loop_trafos': self_loop_trafos,
        'voltage_mismatch_lines': bad_lines,
        'same_voltage_trafos': bad_trafos,
        'orphaned_buses': len(orphans),
        'issues': issues,
        'passed': len(issues) == 0,
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(source, target, radii, dry_run=True):
    """Run the full simplification pipeline."""
    engine = create_engine(DB_URI)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    print(f"\nSubstation Simplification Pipeline")
    print(f"  Source:  {source}")
    print(f"  Target:  {target}")
    print(f"  Radii:   110kV={radii[110]}m, 220kV={radii[220]}m, 380kV={radii[380]}m")
    print(f"  Mode:    {'DRY RUN' if dry_run else 'APPLY'}")

    # Phase 1: Copy scenario
    if not dry_run:
        counts = copy_scenario(engine, source, target)
    else:
        print(f"\n{'='*70}")
        print(f"Phase 1: [DRY RUN] Would copy {source} -> {target}")
        print(f"{'='*70}")
        # In dry-run, work on the source scenario directly (read-only)
        target = source

    # Phase 2: Load data
    buses, lines, trafos = load_data(engine, target)

    # Pre-simplification stats
    buses_set = set(buses['bus_id'].values)
    pre_components = count_connected_components(buses_set, lines, trafos)
    print(f"\n  Pre-simplification:")
    print(f"    Buses:      {len(buses):,}")
    print(f"    Lines:      {len(lines):,}")
    print(f"    Trafos:     {len(trafos):,}")
    print(f"    Components: {pre_components}")

    # Phase 3: Spatial clustering
    print(f"\n{'='*70}")
    print(f"Phase 3: Spatial clustering")
    print(f"{'='*70}")
    cluster_map = spatial_cluster(buses, radii)

    # Count clusters
    clusters = defaultdict(set)
    for bid, root in cluster_map.items():
        clusters[root].add(bid)
    multi_clusters = {r: m for r, m in clusters.items() if len(m) > 1}
    total_clustered_buses = sum(len(m) for m in multi_clusters.values())
    print(f"  Total spatial clusters (>1 bus): {len(multi_clusters)}")
    print(f"  Buses in multi-bus clusters:     {total_clustered_buses}")

    # Phase 4: Split by connectivity
    print(f"\n{'='*70}")
    print(f"Phase 4: Split clusters by connectivity")
    print(f"{'='*70}")
    merge_groups = split_by_connectivity(cluster_map, lines)
    total_merged = sum(len(g) for g in merge_groups)
    print(f"  Connected sub-clusters (>1 bus): {len(merge_groups)}")
    print(f"  Buses to be merged:              {total_merged}")
    if merge_groups:
        sizes = [len(g) for g in merge_groups]
        print(f"  Cluster sizes: min={min(sizes)}, max={max(sizes)}, "
              f"median={sorted(sizes)[len(sizes)//2]}")

    # Phase 5: Build mapping
    print(f"\n{'='*70}")
    print(f"Phase 5: Build node mapping")
    print(f"{'='*70}")
    mapping = build_mapping(merge_groups, lines)
    print(f"  Buses to remove: {len(mapping.removed_nodes):,}")
    print(f"  Representative buses: {len(mapping.kept_nodes):,}")

    if dry_run:
        print(f"\n{'='*70}")
        print(f"DRY RUN complete — no database changes made")
        print(f"{'='*70}")

        # Still export mapping for review
        output_dir = 'results/simplification'
        os.makedirs(output_dir, exist_ok=True)
        mapping.save_csv(os.path.join(output_dir, f'node_mapping_dryrun_{timestamp}.csv'))

        # Estimate impact
        # Count how many lines would become self-loops
        self_loops = 0
        for _, row in lines.iterrows():
            b0 = mapping.map(int(row['bus0']))
            b1 = mapping.map(int(row['bus1']))
            if b0 == b1:
                self_loops += 1
        print(f"\n  Estimated impact:")
        print(f"    Buses to remove:         {len(mapping.removed_nodes):,}")
        print(f"    Lines becoming self-loops: {self_loops:,}")

        return mapping

    # Phase 6: Apply remapping
    apply_remapping(engine, target, mapping)

    # Phase 7: Delete self-loops
    lines_deleted, trafos_deleted = delete_self_loops(engine, target)

    # Phase 8: Delete orphaned buses
    buses_deleted = delete_orphaned_buses(engine, target)

    # Phase 9: Validation
    validation = validate(engine, target, pre_components, mapping)

    # Export results
    output_dir = 'results/simplification'
    os.makedirs(output_dir, exist_ok=True)

    mapping.save_csv(os.path.join(output_dir, 'node_mapping.csv'))

    summary = {
        'timestamp': timestamp,
        'source': source,
        'target': target,
        'radii': radii,
        'pre': {
            'buses': len(buses),
            'lines': len(lines),
            'transformers': len(trafos),
            'components': pre_components,
        },
        'removed': {
            'buses_merged': len(mapping.removed_nodes),
            'buses_orphaned': buses_deleted,
            'lines_self_loops': lines_deleted,
            'trafos_self_loops': trafos_deleted,
        },
        'validation': validation,
    }

    # Add post counts
    post_buses, post_lines, post_trafos = load_data(engine, target)
    summary['post'] = {
        'buses': len(post_buses),
        'lines': len(post_lines),
        'transformers': len(post_trafos),
        'components': validation['post_components'],
    }

    with open(os.path.join(output_dir, 'summary.json'), 'w') as f:
        json.dump(summary, f, indent=2)

    print(f"\n{'='*70}")
    print(f"SUMMARY")
    print(f"{'='*70}")
    print(f"  Buses:        {len(buses):,} -> {summary['post']['buses']:,} "
          f"(-{len(buses) - summary['post']['buses']:,})")
    print(f"  Lines:        {len(lines):,} -> {summary['post']['lines']:,} "
          f"(-{len(lines) - summary['post']['lines']:,})")
    print(f"  Transformers: {len(trafos):,} -> {summary['post']['transformers']:,} "
          f"(-{len(trafos) - summary['post']['transformers']:,})")
    print(f"  Components:   {pre_components} -> {validation['post_components']}")
    print(f"  Validation:   {'PASSED' if validation['passed'] else 'FAILED'}")
    if validation['issues']:
        for issue in validation['issues']:
            print(f"    WARNING: {issue}")

    print(f"\n  Results saved to: {output_dir}/")

    return mapping


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description='Simplify substations by collapsing internal bus nodes')
    parser.add_argument('--source', default=DEFAULT_SOURCE,
                        help=f'Source scenario (default: {DEFAULT_SOURCE})')
    parser.add_argument('--target', default=DEFAULT_TARGET,
                        help=f'Target scenario (default: {DEFAULT_TARGET})')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true',
                      help='Analyse without modifying the database')
    mode.add_argument('--apply', action='store_true',
                      help='Apply changes to the database')
    parser.add_argument('--radius-110', type=int, default=DEFAULT_RADIUS_110,
                        help=f'Clustering radius for 110kV (meters, default: {DEFAULT_RADIUS_110})')
    parser.add_argument('--radius-220', type=int, default=DEFAULT_RADIUS_220,
                        help=f'Clustering radius for 220kV (meters, default: {DEFAULT_RADIUS_220})')
    parser.add_argument('--radius-380', type=int, default=DEFAULT_RADIUS_380,
                        help=f'Clustering radius for 380kV (meters, default: {DEFAULT_RADIUS_380})')
    return parser.parse_args()


def main():
    args = parse_args()
    radii = {
        110: args.radius_110,
        220: args.radius_220,
        380: args.radius_380,
    }
    run_pipeline(
        source=args.source,
        target=args.target,
        radii=radii,
        dry_run=args.dry_run,
    )


if __name__ == '__main__':
    main()
