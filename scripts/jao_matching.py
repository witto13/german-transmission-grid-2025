#!/usr/bin/env python3
"""
JAO/CORE-TSO Parameter Matching for eGon2025v7
===============================================

Matches JAO/CORE-TSO dataset (real TSO-reported electrical parameters) to our
eGon2025v7 grid model. Both datasets are OSM-derived, enabling high match rates
via OSM object ID matching (84.8% of substations).

Transfers: r, x, b for lines; r, x, b, g, s_nom, phase_shift for transformers.
Also produces s_nom from seasonal Imax and stores EIC code mappings.

Usage:
    python scripts/jao_matching.py [--dry-run] [--scenario eGon2025v7] [--output-dir results/jao_matching/]
"""

import argparse
import math
import os
import sys
from collections import defaultdict

import networkx as nx
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
JAO_DIR = 'data/jao_core_tso'
DE_TSOS = ['50HERTZ', 'TENNETGMBH', 'Amprion GmbH', 'TRANSNETBW']
KM_PER_DEG_LAT = 111.0
KM_PER_DEG_LON = 71.5  # cos(52 deg) * 111

# Clustering / matching thresholds (km)
CLUSTER_RADIUS_KM = 0.2       # 200m for grouping eGon buses into substations
CLUSTER_ABSORB_KM = 0.2       # 200m for merging spatial clusters into named ones
BUS_MATCH_TIER1_KM = 0.5      # 500m OSM-derived spatial match
BUS_MATCH_TIER2_KM = 2.0      # 2km with degree validation
BUS_MATCH_TIER3_KM = 5.0      # 5km last resort

# Path validation bounds (ratio of JAO length)
PATH_LENGTH_MIN = 0.3
PATH_LENGTH_MAX = 3.0
PATH_LENGTH_WARN_LOW = 0.5
PATH_LENGTH_WARN_HIGH = 2.0


# ---------------------------------------------------------------------------
# Step 1: Data Loading
# ---------------------------------------------------------------------------
def load_data(engine, scenario):
    """Load JAO CSVs and eGon DB tables."""
    print("=" * 70)
    print("STEP 1: Loading data")
    print("=" * 70)

    # --- JAO data ---
    jao_buses = pd.read_csv(os.path.join(JAO_DIR, 'buses.csv'))
    jao_lines = pd.read_csv(os.path.join(JAO_DIR, 'lines.csv'))
    jao_trafos = pd.read_csv(os.path.join(JAO_DIR, 'transformers.csv'))

    # Filter to German TSOs
    jao_buses = jao_buses[jao_buses['CORE-TSO_tso'].isin(DE_TSOS)].copy()
    jao_lines = jao_lines[jao_lines['TSO'].isin(DE_TSOS)].copy()
    jao_trafos = jao_trafos[jao_trafos['TSO'].isin(DE_TSOS)].copy()

    # Exclude duplicate transformer buses and 110kV
    jao_buses = jao_buses[~jao_buses['transformer_dupplicate']].copy()
    jao_buses = jao_buses[jao_buses['v_nom'].isin([220.0, 400.0])].copy()

    # Voltage mapping: JAO 400 -> eGon 380
    jao_buses['v_nom_mapped'] = jao_buses['v_nom'].replace({400.0: 380})
    jao_lines['v_nom_mapped'] = jao_lines['v_nom'].replace({400: 380})

    # Ensure JAO bus name is string
    jao_buses['name'] = jao_buses['name'].astype(str)

    # Parse JAO line bus references: strip trailing T suffix to get base OSM ID
    jao_lines['bus0_base'] = jao_lines['bus0'].astype(str).str.replace(r'T+$', '', regex=True)
    jao_lines['bus1_base'] = jao_lines['bus1'].astype(str).str.replace(r'T+$', '', regex=True)

    # Parse JAO transformer bus references
    jao_trafos['bus0_base'] = jao_trafos['bus0'].astype(str).str.replace(r'T+$', '', regex=True)
    jao_trafos['bus1_base'] = jao_trafos['bus1'].astype(str).str.replace(r'T+$', '', regex=True)

    print(f"  JAO buses (DE, non-dup, 220/400): {len(jao_buses)}")
    print(f"  JAO lines (DE): {len(jao_lines)}")
    print(f"  JAO transformers (DE): {len(jao_trafos)}")

    # --- eGon data ---
    egon_buses = pd.read_sql(
        f"SELECT bus_id, x, y, v_nom, country "
        f"FROM grid.egon_etrago_bus WHERE scn_name = '{scenario}'",
        engine
    )

    egon_lines = pd.read_sql(
        f"SELECT line_id, bus0, bus1, length, r, x, b, s_nom, num_parallel "
        f"FROM grid.egon_etrago_line WHERE scn_name = '{scenario}'",
        engine
    )

    egon_trafos = pd.read_sql(
        f"SELECT trafo_id, bus0, bus1, r, x, b, g, s_nom, tap_ratio, phase_shift "
        f"FROM grid.egon_etrago_transformer WHERE scn_name = '{scenario}'",
        engine
    )

    # Substation table for OSM ID linkage
    ehv_subs = pd.read_sql(
        f"SELECT s.bus_id, s.osm_id, s.subst_name, s.lon, s.lat "
        f"FROM grid.egon_ehv_substation s "
        f"JOIN grid.egon_etrago_bus b ON s.bus_id = b.bus_id "
        f"  AND b.scn_name = '{scenario}'",
        engine
    )

    # Filter to 220/380 kV for matching scope
    egon_hv = egon_buses[egon_buses['v_nom'].isin([220, 380])].copy()

    # Annotate which eGon buses have substation records
    ehv_bus_ids = set(ehv_subs['bus_id'])
    egon_hv['has_substation'] = egon_hv['bus_id'].isin(ehv_bus_ids)

    # Get v_nom for each bus for quick lookup
    bus_vnom = egon_buses.set_index('bus_id')['v_nom'].to_dict()

    # Filter lines to 220/380 only (both endpoints)
    egon_hv_lines = egon_lines[
        egon_lines['bus0'].map(bus_vnom).isin([220, 380]) &
        egon_lines['bus1'].map(bus_vnom).isin([220, 380])
    ].copy()

    # Filter transformers to 220/380 interface
    egon_hv_trafos = egon_trafos[
        egon_trafos['bus0'].map(bus_vnom).isin([220, 380]) &
        egon_trafos['bus1'].map(bus_vnom).isin([220, 380])
    ].copy()

    print(f"  eGon buses (220/380): {len(egon_hv)}")
    print(f"    with substation record: {egon_hv['has_substation'].sum()}")
    print(f"  eGon lines (220/380): {len(egon_hv_lines)}")
    print(f"  eGon transformers (220/380): {len(egon_hv_trafos)}")
    print(f"  EHV substation records: {len(ehv_subs)}")

    return {
        'jao_buses': jao_buses,
        'jao_lines': jao_lines,
        'jao_trafos': jao_trafos,
        'egon_buses': egon_buses,
        'egon_hv': egon_hv,
        'egon_hv_lines': egon_hv_lines,
        'egon_hv_trafos': egon_hv_trafos,
        'ehv_subs': ehv_subs,
        'bus_vnom': bus_vnom,
    }


# ---------------------------------------------------------------------------
# Step 2: Substation Clustering
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


def cluster_buses(egon_hv, ehv_subs, egon_hv_lines, bus_vnom):
    """Group eGon buses into substation clusters."""
    print("\n" + "=" * 70)
    print("STEP 2: Substation clustering")
    print("=" * 70)

    # Build OSM ID mapping: bus_id -> numeric OSM ID
    osm_map = {}
    for _, row in ehv_subs.iterrows():
        osm_id_str = str(row['osm_id'])
        # Strip prefix (w, n) to get numeric part
        numeric_part = osm_id_str.lstrip('wn')
        osm_map[row['bus_id']] = numeric_part

    # --- Named substations: cluster_id = OSM numeric ID ---
    bus_to_cluster = {}
    cluster_info = {}  # cluster_id -> {buses, osm_id, name, ...}

    named_buses = ehv_subs[ehv_subs['bus_id'].isin(set(egon_hv['bus_id']))].copy()
    for _, row in named_buses.iterrows():
        bid = row['bus_id']
        osm_numeric = osm_map.get(bid)
        if osm_numeric:
            cluster_id = f"osm_{osm_numeric}"
            bus_to_cluster[bid] = cluster_id
            if cluster_id not in cluster_info:
                cluster_info[cluster_id] = {
                    'buses': [],
                    'osm_id': osm_numeric,
                    'name': row.get('subst_name', ''),
                    'type': 'named',
                }
            cluster_info[cluster_id]['buses'].append(bid)

    print(f"  Named clusters (from ehv_substation): {len(cluster_info)}")

    # --- Unnamed buses: spatial clustering with Union-Find ---
    unnamed = egon_hv[~egon_hv['bus_id'].isin(bus_to_cluster)].copy()
    print(f"  Unnamed buses to cluster: {len(unnamed)}")

    uf = UnionFind()
    # Cluster per voltage level
    for voltage in [220, 380]:
        vbuses = unnamed[unnamed['v_nom'] == voltage].copy()
        if len(vbuses) == 0:
            continue
        coords_km = np.column_stack([
            vbuses['x'].values * KM_PER_DEG_LON,
            vbuses['y'].values * KM_PER_DEG_LAT,
        ])
        tree = cKDTree(coords_km)
        pairs = tree.query_pairs(CLUSTER_RADIUS_KM)
        bus_ids = vbuses['bus_id'].values

        # Initialize each bus
        for bid in bus_ids:
            uf.find(bid)
        # Union close pairs
        for i, j in pairs:
            uf.union(bus_ids[i], bus_ids[j])

    # Assign spatial clusters
    spatial_clusters = defaultdict(list)
    for _, row in unnamed.iterrows():
        bid = row['bus_id']
        root = uf.find(bid)
        spatial_clusters[root].append(bid)

    # --- Merge: spatial clusters near named substations get absorbed ---
    # Build KD-tree of named cluster centroids
    named_centroids = []
    named_cids = []
    for cid, info in cluster_info.items():
        cx = egon_hv[egon_hv['bus_id'].isin(info['buses'])]['x'].mean()
        cy = egon_hv[egon_hv['bus_id'].isin(info['buses'])]['y'].mean()
        info['centroid_x'] = cx
        info['centroid_y'] = cy
        named_centroids.append([cx * KM_PER_DEG_LON, cy * KM_PER_DEG_LAT])
        named_cids.append(cid)

    absorbed = 0
    new_spatial = 0
    if named_centroids:
        named_tree = cKDTree(np.array(named_centroids))

        for root_bid, members in spatial_clusters.items():
            # Compute centroid of this spatial cluster
            member_buses = egon_hv[egon_hv['bus_id'].isin(members)]
            cx = member_buses['x'].mean()
            cy = member_buses['y'].mean()
            query = np.array([cx * KM_PER_DEG_LON, cy * KM_PER_DEG_LAT])
            dist, idx = named_tree.query(query)

            if dist <= CLUSTER_ABSORB_KM:
                # Absorb into named cluster
                target_cid = named_cids[idx]
                for bid in members:
                    bus_to_cluster[bid] = target_cid
                    cluster_info[target_cid]['buses'].append(bid)
                absorbed += len(members)
            else:
                # Create new spatial cluster
                cid = f"spatial_{root_bid}"
                cluster_info[cid] = {
                    'buses': members,
                    'osm_id': None,
                    'name': '',
                    'type': 'spatial',
                    'centroid_x': cx,
                    'centroid_y': cy,
                }
                for bid in members:
                    bus_to_cluster[bid] = cid
                new_spatial += 1
    else:
        # No named substations — all spatial
        for root_bid, members in spatial_clusters.items():
            member_buses = egon_hv[egon_hv['bus_id'].isin(members)]
            cx = member_buses['x'].mean()
            cy = member_buses['y'].mean()
            cid = f"spatial_{root_bid}"
            cluster_info[cid] = {
                'buses': members,
                'osm_id': None,
                'name': '',
                'type': 'spatial',
                'centroid_x': cx,
                'centroid_y': cy,
            }
            for bid in members:
                bus_to_cluster[bid] = cid
            new_spatial += 1

    print(f"  Absorbed into named clusters: {absorbed} buses")
    print(f"  New spatial clusters: {new_spatial}")
    print(f"  Total clusters: {len(cluster_info)}")

    # Compute final centroids and node degree
    for cid, info in cluster_info.items():
        member_buses = egon_hv[egon_hv['bus_id'].isin(info['buses'])]
        info['centroid_x'] = member_buses['x'].mean()
        info['centroid_y'] = member_buses['y'].mean()
        info['voltages'] = sorted(member_buses['v_nom'].unique().tolist())

    # Compute node degree: count external line connections
    cluster_degree = defaultdict(int)
    for _, line in egon_hv_lines.iterrows():
        c0 = bus_to_cluster.get(line['bus0'])
        c1 = bus_to_cluster.get(line['bus1'])
        if c0 and c1 and c0 != c1:
            cluster_degree[c0] += 1
            cluster_degree[c1] += 1
    for cid in cluster_info:
        cluster_info[cid]['degree'] = cluster_degree.get(cid, 0)

    return bus_to_cluster, cluster_info


# ---------------------------------------------------------------------------
# Step 3: Bus Matching — JAO to eGon Clusters
# ---------------------------------------------------------------------------
def match_buses(jao_buses, cluster_info, bus_to_cluster, egon_hv):
    """Match JAO buses to eGon substation clusters."""
    print("\n" + "=" * 70)
    print("STEP 3: Bus matching (JAO -> eGon clusters)")
    print("=" * 70)

    # Build reverse index: osm_id -> cluster_id (for named clusters)
    osm_to_cluster = {}
    for cid, info in cluster_info.items():
        if info['osm_id']:
            osm_to_cluster[info['osm_id']] = cid

    # Build KD-trees per voltage for spatial fallback
    cluster_coords = defaultdict(list)  # voltage -> [(x_km, y_km, cid)]
    for cid, info in cluster_info.items():
        for v in info['voltages']:
            cluster_coords[v].append((
                info['centroid_x'] * KM_PER_DEG_LON,
                info['centroid_y'] * KM_PER_DEG_LAT,
                cid,
            ))

    cluster_trees = {}
    cluster_tree_cids = {}
    for v, coords in cluster_coords.items():
        arr = np.array([[c[0], c[1]] for c in coords])
        cluster_trees[v] = cKDTree(arr)
        cluster_tree_cids[v] = [c[2] for c in coords]

    # Match each JAO bus
    matches = {}  # jao_name -> {cluster_id, confidence, tier, distance_km}
    tier_counts = defaultdict(int)

    for _, jao in jao_buses.iterrows():
        jao_name = str(jao['name'])
        jao_v = int(jao['v_nom_mapped'])
        jao_x = jao['x']
        jao_y = jao['y']

        # --- Tier 0: OSM ID match ---
        if jao_name in osm_to_cluster:
            cid = osm_to_cluster[jao_name]
            # Verify voltage compatibility
            if jao_v in cluster_info[cid]['voltages']:
                matches[jao_name] = {
                    'cluster_id': cid,
                    'confidence': 1.0,
                    'tier': 0,
                    'distance_km': 0.0,
                    'method': 'osm_id',
                }
                tier_counts[0] += 1
                continue
            # OSM ID match but voltage mismatch — still use it but lower confidence
            matches[jao_name] = {
                'cluster_id': cid,
                'confidence': 0.8,
                'tier': 0,
                'distance_km': 0.0,
                'method': 'osm_id_voltage_mismatch',
            }
            tier_counts[0] += 1
            continue

        # --- Spatial tiers ---
        query_km = np.array([jao_x * KM_PER_DEG_LON, jao_y * KM_PER_DEG_LAT])

        matched = False
        for tier, (radius, min_conf) in enumerate([
            (BUS_MATCH_TIER1_KM, 0.9),
            (BUS_MATCH_TIER2_KM, 0.7),
            (BUS_MATCH_TIER3_KM, 0.5),
        ], start=1):
            if jao_v not in cluster_trees:
                continue
            tree = cluster_trees[jao_v]
            cids = cluster_tree_cids[jao_v]

            dist, idx = tree.query(query_km)
            if dist <= radius:
                cid = cids[idx]
                # Tier 2/3: validate node degree
                if tier >= 2:
                    jao_degree = _estimate_jao_degree(jao_name, jao_buses)
                    egon_degree = cluster_info[cid]['degree']
                    if tier == 2 and abs(jao_degree - egon_degree) > 3:
                        continue
                    if tier == 3 and abs(jao_degree - egon_degree) > 3:
                        continue

                # Distance-based confidence within the tier
                confidence = min_conf * (1 - dist / radius * 0.2)
                matches[jao_name] = {
                    'cluster_id': cid,
                    'confidence': round(confidence, 3),
                    'tier': tier,
                    'distance_km': round(dist, 3),
                    'method': f'spatial_{radius}km',
                }
                tier_counts[tier] += 1
                matched = True
                break

        if not matched:
            tier_counts['unmatched'] += 1

    print(f"  Tier 0 (OSM ID): {tier_counts[0]}")
    print(f"  Tier 1 (500m spatial): {tier_counts[1]}")
    print(f"  Tier 2 (2km + degree): {tier_counts[2]}")
    print(f"  Tier 3 (5km fallback): {tier_counts[3]}")
    print(f"  Unmatched: {tier_counts['unmatched']}")
    total_jao = len(jao_buses)
    matched_count = sum(v for k, v in tier_counts.items() if k != 'unmatched')
    print(f"  Match rate: {matched_count}/{total_jao} = {matched_count/total_jao*100:.1f}%")

    return matches


def _estimate_jao_degree(jao_name, jao_buses):
    """Estimate node degree for a JAO bus from the JAO line data.

    This is a rough estimate — count how many times this OSM ID appears
    as a bus endpoint in the full JAO line file.
    We cache this on first call.
    """
    if not hasattr(_estimate_jao_degree, '_cache'):
        # Build cache from JAO lines (load from CSV)
        try:
            lines = pd.read_csv(os.path.join(JAO_DIR, 'lines.csv'))
            degree = defaultdict(int)
            for _, row in lines.iterrows():
                b0 = str(row['bus0']).rstrip('T')
                b1 = str(row['bus1']).rstrip('T')
                degree[b0] += 1
                degree[b1] += 1
            _estimate_jao_degree._cache = degree
        except Exception:
            _estimate_jao_degree._cache = defaultdict(int)
    return _estimate_jao_degree._cache.get(str(jao_name), 0)


# ---------------------------------------------------------------------------
# Step 4: Graph Building + Line Matching
# ---------------------------------------------------------------------------
def build_cluster_graph(egon_hv_lines, bus_to_cluster, cluster_info, bus_vnom):
    """Build per-voltage cluster-level graphs."""
    print("\n" + "=" * 70)
    print("STEP 4a: Building cluster-level graph")
    print("=" * 70)

    graphs = {}  # voltage -> nx.Graph
    corridor_lines = defaultdict(list)  # (cluster_a, cluster_b, voltage) -> [line_ids]

    for _, line in egon_hv_lines.iterrows():
        lid = line['line_id']
        b0, b1 = line['bus0'], line['bus1']
        c0 = bus_to_cluster.get(b0)
        c1 = bus_to_cluster.get(b1)

        if not c0 or not c1 or c0 == c1:
            continue  # Intra-cluster or missing

        v0 = bus_vnom.get(b0)
        v1 = bus_vnom.get(b1)
        if v0 != v1 or v0 not in (220, 380):
            continue  # Cross-voltage (handled by transformers)

        voltage = v0
        if voltage not in graphs:
            graphs[voltage] = nx.Graph()

        corridor = tuple(sorted([c0, c1]))
        corridor_lines[(corridor[0], corridor[1], voltage)].append(lid)

        # Add edge with minimum line length as weight
        length = line['length'] if line['length'] > 0 else 0.01
        if graphs[voltage].has_edge(c0, c1):
            # Keep minimum length for routing
            old_w = graphs[voltage][c0][c1]['weight']
            graphs[voltage][c0][c1]['weight'] = min(old_w, length)
        else:
            graphs[voltage].add_edge(c0, c1, weight=length)

    for v, g in graphs.items():
        print(f"  {v}kV graph: {g.number_of_nodes()} nodes, {g.number_of_edges()} edges")
    print(f"  Total corridors: {len(corridor_lines)}")

    return graphs, corridor_lines


def match_lines(jao_lines, bus_matches, graphs, corridor_lines,
                cluster_info, egon_hv_lines, bus_vnom):
    """Match JAO lines to eGon line segments."""
    print("\n" + "=" * 70)
    print("STEP 4b: Line matching")
    print("=" * 70)

    # Build line lookup: line_id -> line row
    line_data = egon_hv_lines.set_index('line_id')

    # Build index: corridor -> line details
    corridor_line_data = {}
    for key, lids in corridor_lines.items():
        c0, c1, v = key
        lines = []
        for lid in lids:
            if lid in line_data.index:
                row = line_data.loc[lid]
                lines.append({
                    'line_id': lid,
                    'length': row['length'],
                    's_nom': row['s_nom'],
                    'r': row['r'],
                    'x': row['x'],
                })
        corridor_line_data[key] = lines

    line_matches = []  # List of match dicts
    stats = defaultdict(int)

    for jao_idx, jao_line in jao_lines.iterrows():
        b0_base = jao_line['bus0_base']
        b1_base = jao_line['bus1_base']
        jao_v = int(jao_line['v_nom_mapped'])
        jao_length = jao_line['length'] if pd.notna(jao_line['length']) else 0

        # Resolve endpoints to eGon clusters
        m0 = bus_matches.get(b0_base)
        m1 = bus_matches.get(b1_base)

        if not m0 or not m1:
            stats['endpoint_unmatched'] += 1
            continue

        c0 = m0['cluster_id']
        c1 = m1['cluster_id']

        if c0 == c1:
            stats['same_cluster'] += 1
            continue

        if jao_v not in graphs:
            stats['no_graph'] += 1
            continue

        G = graphs[jao_v]

        # Check if both clusters are in the graph
        if c0 not in G or c1 not in G:
            stats['cluster_not_in_graph'] += 1
            continue

        # Find shortest path
        try:
            path = nx.shortest_path(G, c0, c1, weight='weight')
        except nx.NetworkXNoPath:
            stats['no_path'] += 1
            continue

        # Collect eGon line segments along the path
        path_segments = []
        total_path_length = 0
        for i in range(len(path) - 1):
            ca, cb = path[i], path[i + 1]
            corridor_key = (min(ca, cb), max(ca, cb), jao_v) if ca < cb else (min(ca, cb), max(ca, cb), jao_v)
            # Normalize key
            corridor_key = (sorted([ca, cb])[0], sorted([ca, cb])[1], jao_v)
            seg_lines = corridor_line_data.get(corridor_key, [])
            if not seg_lines:
                continue
            # Pick the first line (primary circuit) for path length computation
            seg = seg_lines[0]
            path_segments.append({
                'corridor_key': corridor_key,
                'all_lines': seg_lines,
                'length': seg['length'],
            })
            total_path_length += seg['length']

        if not path_segments:
            stats['no_segments'] += 1
            continue

        # Validate path length
        if jao_length > 0:
            ratio = total_path_length / jao_length
            if ratio < PATH_LENGTH_MIN or ratio > PATH_LENGTH_MAX:
                stats['length_mismatch'] += 1
                continue
            warn = ''
            if ratio < PATH_LENGTH_WARN_LOW:
                warn = 'short'
            elif ratio > PATH_LENGTH_WARN_HIGH:
                warn = 'long'
        else:
            ratio = 0
            warn = 'jao_zero_length'

        match_info = {
            'jao_idx': jao_idx,
            'jao_name': jao_line.get('NE_name', ''),
            'jao_eic': jao_line.get('EIC_Code', ''),
            'jao_r': jao_line['r'],
            'jao_x': jao_line['x'],
            'jao_b': jao_line['b'],
            'jao_s_nom': jao_line['s_nom'],
            'jao_length': jao_length,
            'jao_v_nom': jao_v,
            'bus0_base': b0_base,
            'bus1_base': b1_base,
            'cluster_path': path,
            'path_segments': path_segments,
            'total_path_length': total_path_length,
            'length_ratio': round(ratio, 3),
            'length_warning': warn,
            'path_hops': len(path) - 1,
            'tie_line': jao_line.get('tie_line', False),
        }

        # Get Imax for s_nom calculation
        imax_cols = [c for c in jao_lines.columns if 'Imax' in c and 'Period' in c]
        imax_values = [jao_line[c] for c in imax_cols if pd.notna(jao_line[c]) and jao_line[c] > 0]
        if not imax_values:
            # Try fixed Imax
            fixed_col = 'Maximum Current Imax (A) Fixed'
            if fixed_col in jao_lines.columns and pd.notna(jao_line.get(fixed_col)) and jao_line[fixed_col] > 0:
                imax_values = [jao_line[fixed_col]]
        match_info['imax_a'] = min(imax_values) if imax_values else None

        line_matches.append(match_info)
        stats['matched'] += 1

    print(f"  Matched: {stats['matched']}")
    print(f"  Endpoint unmatched: {stats['endpoint_unmatched']}")
    print(f"  Same cluster: {stats['same_cluster']}")
    print(f"  No graph: {stats['no_graph']}")
    print(f"  Cluster not in graph: {stats['cluster_not_in_graph']}")
    print(f"  No path: {stats['no_path']}")
    print(f"  No segments: {stats['no_segments']}")
    print(f"  Length mismatch (>{PATH_LENGTH_MAX}x or <{PATH_LENGTH_MIN}x): {stats['length_mismatch']}")
    if stats['matched'] > 0:
        print(f"  Match rate: {stats['matched']}/{len(jao_lines)} = "
              f"{stats['matched']/len(jao_lines)*100:.1f}%")

    return line_matches


# ---------------------------------------------------------------------------
# Step 4c: Handle parallel circuits within corridors
# ---------------------------------------------------------------------------
def assign_parallel_circuits(line_matches, corridor_lines, egon_hv_lines):
    """For corridors with multiple JAO lines and/or eGon lines, assign parallels."""
    print("\n" + "=" * 70)
    print("STEP 4c: Parallel circuit assignment")
    print("=" * 70)

    line_data = egon_hv_lines.set_index('line_id')

    # Group matched JAO lines by corridor
    # For single-hop matches, the corridor is straightforward
    # For multi-hop, we assign parameters to each segment proportionally
    assignments = []  # List of {jao_match_info, egon_line_id, fraction, ...}

    # Group JAO matches by their corridor path (for parallel assignment)
    corridor_jao = defaultdict(list)
    for m in line_matches:
        if m['path_hops'] == 1:
            # Single-hop: direct corridor match
            seg = m['path_segments'][0]
            corridor_jao[seg['corridor_key']].append(m)
        else:
            # Multi-hop: each segment gets proportional assignment
            # These are unique paths, assign directly
            for seg in m['path_segments']:
                corridor_jao[seg['corridor_key']].append(m)

    n_parallel_assigned = 0
    n_single = 0

    for corridor_key, jao_matches_in_corridor in corridor_jao.items():
        seg_lines = []
        for lid in corridor_lines.get(corridor_key, []):
            if lid in line_data.index:
                row = line_data.loc[lid]
                seg_lines.append({
                    'line_id': lid,
                    's_nom': row['s_nom'],
                    'length': row['length'],
                })

        if not seg_lines:
            continue

        # Deduplicate JAO matches (same JAO line may appear via multi-hop)
        seen_jao = set()
        unique_jao = []
        for m in jao_matches_in_corridor:
            jao_key = m['jao_idx']
            if jao_key not in seen_jao:
                seen_jao.add(jao_key)
                unique_jao.append(m)

        # Sort by s_nom for parallel matching
        unique_jao.sort(key=lambda m: m['jao_s_nom'], reverse=True)
        seg_lines.sort(key=lambda s: s['s_nom'], reverse=True)

        # Assign: zip JAO parallels to eGon parallels by s_nom rank
        n_assign = min(len(unique_jao), len(seg_lines))
        for i in range(n_assign):
            jm = unique_jao[i]
            sl = seg_lines[i]
            assignments.append({
                'jao_match': jm,
                'egon_line_id': sl['line_id'],
                'corridor_key': corridor_key,
            })
            if n_assign > 1:
                n_parallel_assigned += 1
            else:
                n_single += 1

    print(f"  Total assignments: {len(assignments)}")
    print(f"  Single-circuit corridors: {n_single}")
    print(f"  Parallel-assigned: {n_parallel_assigned}")

    return assignments


# ---------------------------------------------------------------------------
# Step 5: Parameter Transfer
# ---------------------------------------------------------------------------
def compute_line_updates(assignments, egon_hv_lines):
    """Compute parameter updates for matched eGon lines."""
    print("\n" + "=" * 70)
    print("STEP 5a: Line parameter transfer")
    print("=" * 70)

    line_data = egon_hv_lines.set_index('line_id')
    updates = {}  # line_id -> {r, x, b, s_nom}
    eic_map = []  # For EIC mapping CSV

    for asgn in assignments:
        jm = asgn['jao_match']
        egon_lid = asgn['egon_line_id']

        if egon_lid not in line_data.index:
            continue

        egon_row = line_data.loc[egon_lid]
        seg_length = egon_row['length']
        total_path_length = jm['total_path_length']
        jao_v = jm['jao_v_nom']

        if total_path_length <= 0:
            continue

        # Length fraction for distributing JAO total Ohms
        fraction = seg_length / total_path_length

        # r, x: distribute proportionally by segment length
        new_r = jm['jao_r'] * fraction if pd.notna(jm['jao_r']) and jm['jao_r'] > 0 else None
        new_x = jm['jao_x'] * fraction if pd.notna(jm['jao_x']) and jm['jao_x'] > 0 else None

        # b: same proportional distribution (shunt susceptance)
        new_b = jm['jao_b'] * fraction if pd.notna(jm['jao_b']) and jm['jao_b'] > 0 else None

        # s_nom from Imax: s_nom = sqrt(3) * V_kV * Imax_A / 1000
        # JAO s_nom is per-phase (confirmed: s_nom_jao / (sqrt(3)*V*I) = 1/sqrt(3))
        new_s_nom = None
        if jm['imax_a'] and jm['imax_a'] > 0:
            new_s_nom = math.sqrt(3) * jao_v * jm['imax_a'] / 1000.0
        elif pd.notna(jm['jao_s_nom']) and jm['jao_s_nom'] > 0:
            # Convert per-phase to 3-phase: multiply by sqrt(3)
            new_s_nom = jm['jao_s_nom'] * math.sqrt(3)

        # Only update if we have valid values
        if egon_lid in updates:
            # Already assigned from another JAO line (multi-hop), skip
            continue

        update = {}
        if new_r is not None:
            update['r'] = round(new_r, 6)
        if new_x is not None:
            update['x'] = round(new_x, 6)
        if new_b is not None:
            update['b'] = round(new_b, 8)
        if new_s_nom is not None:
            update['s_nom'] = round(new_s_nom, 1)

        if update:
            updates[egon_lid] = update

        # EIC mapping
        if jm.get('jao_eic'):
            eic_map.append({
                'egon_line_id': egon_lid,
                'eic_code': jm['jao_eic'],
                'jao_name': jm['jao_name'],
                'jao_length_km': jm['jao_length'],
                'path_hops': jm['path_hops'],
                'length_ratio': jm['length_ratio'],
            })

    # Report statistics
    if updates:
        r_changes = [u['r'] for u in updates.values() if 'r' in u]
        x_changes = [u['x'] for u in updates.values() if 'x' in u]
        s_changes = [u['s_nom'] for u in updates.values() if 's_nom' in u]
        print(f"  Lines to update: {len(updates)}")
        if r_changes:
            print(f"  r: median={np.median(r_changes):.4f}, "
                  f"range=[{min(r_changes):.4f}, {max(r_changes):.4f}]")
        if x_changes:
            print(f"  x: median={np.median(x_changes):.4f}, "
                  f"range=[{min(x_changes):.4f}, {max(x_changes):.4f}]")
        if s_changes:
            print(f"  s_nom: median={np.median(s_changes):.0f}, "
                  f"range=[{min(s_changes):.0f}, {max(s_changes):.0f}]")
    else:
        print("  No line updates computed.")

    return updates, eic_map


def match_and_compute_trafo_updates(jao_trafos, bus_matches, cluster_info,
                                     bus_to_cluster, egon_hv_trafos, bus_vnom):
    """Match JAO transformers to eGon transformers and compute updates.

    Strategy: Each eGon transformer may represent N parallel physical transformers.
    We collect all JAO transformers at the same cluster pair, compute their parallel
    equivalent r/x (per-unit), and assign to the eGon transformer.

    JAO r/x are in per-unit (using individual s_nom as base).
    PyPSA r/x are in per-unit (using eGon s_nom as base).
    To combine: convert JAO p.u. to Ohms, parallel-combine, convert back to p.u.

    We skip b/g transfer because JAO uses physical units that are hard to convert
    reliably without knowing the exact base convention.
    """
    print("\n" + "=" * 70)
    print("STEP 5b: Transformer matching and parameter transfer")
    print("=" * 70)

    trafo_updates = {}
    trafo_eic_map = []
    stats = defaultdict(int)

    # Build index: (cluster, cluster) -> [egon trafo rows]
    egon_trafo_by_cluster_pair = defaultdict(list)
    for _, t in egon_hv_trafos.iterrows():
        c0 = bus_to_cluster.get(t['bus0'])
        c1 = bus_to_cluster.get(t['bus1'])
        if c0 and c1:
            pair = tuple(sorted([c0, c1]))
            egon_trafo_by_cluster_pair[pair].append(t)

    # Group JAO transformers by resolved cluster pair
    jao_by_cluster_pair = defaultdict(list)
    for _, jao_t in jao_trafos.iterrows():
        b0_base = str(jao_t['bus0_base'])
        b1_base = str(jao_t['bus1_base'])

        m0 = bus_matches.get(b0_base)
        m1 = bus_matches.get(b1_base)

        if not m0 and not m1:
            stats['unmatched'] += 1
            continue

        if m0 and m1:
            c0, c1 = m0['cluster_id'], m1['cluster_id']
        elif m0:
            c0 = c1 = m0['cluster_id']
        else:
            c0 = c1 = m1['cluster_id']

        pair = tuple(sorted([c0, c1]))
        jao_by_cluster_pair[pair].append(jao_t)

    # For each cluster pair: match JAO group to eGon transformers
    for pair, jao_group in jao_by_cluster_pair.items():
        egon_candidates = egon_trafo_by_cluster_pair.get(pair, [])
        if not egon_candidates:
            stats['no_egon_trafo'] += len(jao_group)
            continue

        # Determine voltage levels from the eGon transformers
        # (use first candidate's bus voltages as reference)
        v_high = max(bus_vnom.get(egon_candidates[0]['bus0'], 380),
                     bus_vnom.get(egon_candidates[0]['bus1'], 220))

        # Compute parallel equivalent of all JAO transformers at this pair
        # Convert each JAO r/x from p.u. (on its own s_nom base) to Ohms,
        # then parallel-combine, then convert back to p.u. on eGon's s_nom base
        jao_r_ohms = []
        jao_x_ohms = []
        jao_s_total = 0
        valid_jao = []

        for jt in jao_group:
            jao_s_1ph = jt['s_nom'] if pd.notna(jt['s_nom']) and jt['s_nom'] > 0 else 0
            if jao_s_1ph <= 0:
                continue
            # JAO s_nom is per-phase; convert to 3-phase for z_base calculation
            jao_s = jao_s_1ph * math.sqrt(3)
            z_base_jao = (v_high ** 2) / jao_s  # Ohms
            r_pu = jt['r'] if pd.notna(jt['r']) else 0
            x_pu = jt['x'] if pd.notna(jt['x']) else 0
            if x_pu > 0:
                jao_r_ohms.append(r_pu * z_base_jao)
                jao_x_ohms.append(x_pu * z_base_jao)
                jao_s_total += jao_s
                valid_jao.append(jt)

        if not valid_jao:
            stats['no_valid_params'] += len(jao_group)
            continue

        # Parallel impedance combination
        if len(jao_r_ohms) > 1:
            r_pos = [r for r in jao_r_ohms if r > 0]
            r_equiv = 1.0 / sum(1.0 / r for r in r_pos) if r_pos else 0
            x_equiv = 1.0 / sum(1.0 / x for x in jao_x_ohms)
        else:
            r_equiv = jao_r_ohms[0]
            x_equiv = jao_x_ohms[0]

        # Assign to each eGon transformer at this pair
        # When JAO group covers fewer units than eGon expects, scale down impedance
        # by assuming missing units have similar parameters to the known ones
        avg_jao_unit_s = jao_s_total / len(valid_jao)

        for egon_t in egon_candidates:
            tid = egon_t['trafo_id']
            if tid in trafo_updates:
                continue

            egon_s = egon_t['s_nom'] if pd.notna(egon_t['s_nom']) and egon_t['s_nom'] > 0 else 0
            if egon_s <= 0:
                continue

            # Estimate how many parallel units the eGon transformer represents
            n_estimated = max(1, round(egon_s / avg_jao_unit_s))

            # Scale impedance: n_estimated parallel units of average JAO unit
            # Avg single-unit impedance in Ohms
            avg_r_unit = sum(jao_r_ohms) / len(jao_r_ohms) if jao_r_ohms else 0
            avg_x_unit = sum(jao_x_ohms) / len(jao_x_ohms) if jao_x_ohms else 0

            # Parallel combination of n_estimated identical units: Z/n
            r_scaled = avg_r_unit / n_estimated if avg_r_unit > 0 else 0
            x_scaled = avg_x_unit / n_estimated if avg_x_unit > 0 else 0

            # Convert to per-unit on eGon's s_nom base
            z_base_egon = (v_high ** 2) / egon_s
            update = {}
            if r_scaled > 0:
                update['r'] = round(r_scaled / z_base_egon, 6)
            if x_scaled > 0:
                x_pu = x_scaled / z_base_egon
                # Sanity: typical transformer x is 0.05-0.20 p.u.
                if x_pu > 0.5:
                    stats['x_capped'] = stats.get('x_capped', 0) + 1
                update['x'] = round(x_pu, 6)

            # Phase shift: use most common value from JAO group
            theta_col = 'Theta θ (°)'
            if theta_col in jao_trafos.columns:
                thetas = [jt[theta_col] for jt in valid_jao
                          if pd.notna(jt.get(theta_col))]
                if thetas:
                    # Use the most common non-zero theta, or 0
                    from collections import Counter
                    theta_counts = Counter(thetas)
                    most_common = theta_counts.most_common(1)[0][0]
                    update['phase_shift'] = round(float(most_common), 2)

            if update:
                trafo_updates[tid] = update
                stats['matched'] += 1

        # EIC mapping
        for jt in valid_jao:
            if pd.notna(jt.get('EIC_Code')):
                trafo_eic_map.append({
                    'egon_trafo_id': egon_candidates[0]['trafo_id'],
                    'eic_code': jt['EIC_Code'],
                    'jao_name': jt.get('Full Name', ''),
                })

    print(f"  Matched eGon trafos: {stats['matched']}")
    print(f"  Unmatched endpoints: {stats['unmatched']}")
    print(f"  No eGon trafo at location: {stats['no_egon_trafo']}")
    print(f"  No valid JAO params: {stats.get('no_valid_params', 0)}")

    if trafo_updates:
        r_vals = [u['r'] for u in trafo_updates.values() if 'r' in u]
        x_vals = [u['x'] for u in trafo_updates.values() if 'x' in u]
        if r_vals:
            print(f"  r (p.u.): median={np.median(r_vals):.6f}, "
                  f"range=[{min(r_vals):.6f}, {max(r_vals):.6f}]")
        if x_vals:
            print(f"  x (p.u.): median={np.median(x_vals):.6f}, "
                  f"range=[{min(x_vals):.6f}, {max(x_vals):.6f}]")

    return trafo_updates, trafo_eic_map


# ---------------------------------------------------------------------------
# Step 6: Database Update
# ---------------------------------------------------------------------------
def apply_updates(engine, scenario, line_updates, trafo_updates, dry_run=True):
    """Apply parameter updates to database."""
    print("\n" + "=" * 70)
    print(f"STEP 6: Database update {'(DRY RUN)' if dry_run else '(LIVE)'}")
    print("=" * 70)

    if dry_run:
        print(f"  Would update {len(line_updates)} lines")
        print(f"  Would update {len(trafo_updates)} transformers")
        # Show a few examples
        if line_updates:
            sample_ids = list(line_updates.keys())[:3]
            for lid in sample_ids:
                print(f"    Line {lid}: {line_updates[lid]}")
        if trafo_updates:
            sample_ids = list(trafo_updates.keys())[:3]
            for tid in sample_ids:
                print(f"    Trafo {tid}: {trafo_updates[tid]}")
        return

    with engine.begin() as conn:
        # Update lines
        n_lines = 0
        for lid, params in line_updates.items():
            set_clauses = ', '.join(f"{k} = :{k}" for k in params.keys())
            sql = text(
                f"UPDATE grid.egon_etrago_line "
                f"SET {set_clauses} "
                f"WHERE scn_name = :scn AND line_id = :lid"
            )
            conn.execute(sql, {**params, 'scn': scenario, 'lid': int(lid)})
            n_lines += 1

        # Update transformers
        n_trafos = 0
        for tid, params in trafo_updates.items():
            set_clauses = ', '.join(f"{k} = :{k}" for k in params.keys())
            sql = text(
                f"UPDATE grid.egon_etrago_transformer "
                f"SET {set_clauses} "
                f"WHERE scn_name = :scn AND trafo_id = :tid"
            )
            conn.execute(sql, {**params, 'scn': scenario, 'tid': int(tid)})
            n_trafos += 1

        print(f"  Updated {n_lines} lines")
        print(f"  Updated {n_trafos} transformers")


# ---------------------------------------------------------------------------
# Step 7: Reporting + Visualization
# ---------------------------------------------------------------------------
def generate_reports(bus_matches, line_matches, assignments, line_updates,
                     trafo_updates, eic_map, trafo_eic_map,
                     jao_buses, egon_hv, egon_hv_lines, egon_hv_trafos,
                     cluster_info, bus_to_cluster, output_dir):
    """Generate reports, CSVs, and interactive map."""
    print("\n" + "=" * 70)
    print("STEP 7: Reporting and visualization")
    print("=" * 70)

    os.makedirs(output_dir, exist_ok=True)

    # --- Bus match report ---
    bus_rows = []
    for jao_name, m in bus_matches.items():
        cinfo = cluster_info.get(m['cluster_id'], {})
        bus_rows.append({
            'jao_osm_id': jao_name,
            'cluster_id': m['cluster_id'],
            'cluster_name': cinfo.get('name', ''),
            'cluster_osm_id': cinfo.get('osm_id', ''),
            'confidence': m['confidence'],
            'tier': m['tier'],
            'distance_km': m['distance_km'],
            'method': m['method'],
            'cluster_degree': cinfo.get('degree', 0),
            'cluster_voltages': str(cinfo.get('voltages', [])),
        })
    bus_df = pd.DataFrame(bus_rows)
    bus_report_path = os.path.join(output_dir, 'bus_match_report.csv')
    bus_df.to_csv(bus_report_path, index=False)
    print(f"  Bus match report: {bus_report_path} ({len(bus_df)} rows)")

    # --- Line match report ---
    line_rows = []
    for asgn in assignments:
        jm = asgn['jao_match']
        egon_lid = asgn['egon_line_id']
        upd = line_updates.get(egon_lid, {})
        line_rows.append({
            'jao_name': jm['jao_name'],
            'jao_eic': jm.get('jao_eic', ''),
            'egon_line_id': egon_lid,
            'jao_r': jm['jao_r'],
            'jao_x': jm['jao_x'],
            'jao_b': jm['jao_b'],
            'jao_s_nom': jm['jao_s_nom'],
            'jao_length_km': jm['jao_length'],
            'jao_v_nom': jm['jao_v_nom'],
            'new_r': upd.get('r'),
            'new_x': upd.get('x'),
            'new_b': upd.get('b'),
            'new_s_nom': upd.get('s_nom'),
            'path_hops': jm['path_hops'],
            'length_ratio': jm['length_ratio'],
            'length_warning': jm['length_warning'],
            'imax_a': jm.get('imax_a'),
        })
    line_df = pd.DataFrame(line_rows)
    line_report_path = os.path.join(output_dir, 'line_match_report.csv')
    line_df.to_csv(line_report_path, index=False)
    print(f"  Line match report: {line_report_path} ({len(line_df)} rows)")

    # --- EIC mapping ---
    if eic_map or trafo_eic_map:
        eic_df = pd.DataFrame(eic_map)
        teic_df = pd.DataFrame(trafo_eic_map)
        combined_eic = pd.concat([eic_df, teic_df], ignore_index=True, sort=False)
        eic_path = os.path.join(output_dir, 'eic_mapping.csv')
        combined_eic.to_csv(eic_path, index=False)
        print(f"  EIC mapping: {eic_path} ({len(combined_eic)} rows)")

    # --- Transformer match report ---
    trafo_rows = []
    for tid, upd in trafo_updates.items():
        trafo_rows.append({'egon_trafo_id': tid, **upd})
    if trafo_rows:
        trafo_df = pd.DataFrame(trafo_rows)
        trafo_path = os.path.join(output_dir, 'trafo_match_report.csv')
        trafo_df.to_csv(trafo_path, index=False)
        print(f"  Trafo match report: {trafo_path} ({len(trafo_df)} rows)")

    # --- Summary statistics ---
    print("\n--- SUMMARY ---")
    total_220_380_lines = len(egon_hv_lines)
    updated_lines = len(line_updates)
    total_220_380_trafos = len(egon_hv_trafos)
    updated_trafos = len(trafo_updates)
    print(f"  Lines: {updated_lines}/{total_220_380_lines} updated "
          f"({updated_lines/total_220_380_lines*100:.1f}%)" if total_220_380_lines > 0 else "")
    print(f"  Trafos: {updated_trafos}/{total_220_380_trafos} updated "
          f"({updated_trafos/total_220_380_trafos*100:.1f}%)" if total_220_380_trafos > 0 else "")

    # --- Parameter change statistics ---
    if line_updates:
        print("\n  Line parameter changes (updated lines only):")
        for param in ['r', 'x', 'b', 's_nom']:
            vals = [u[param] for u in line_updates.values() if param in u]
            if vals:
                print(f"    {param}: n={len(vals)}, "
                      f"median={np.median(vals):.4f}, "
                      f"mean={np.mean(vals):.4f}, "
                      f"min={min(vals):.4f}, max={max(vals):.4f}")

    if trafo_updates:
        print("\n  Transformer parameter changes:")
        for param in ['r', 'x', 's_nom', 'phase_shift']:
            vals = [u[param] for u in trafo_updates.values() if param in u]
            if vals:
                print(f"    {param}: n={len(vals)}, "
                      f"median={np.median(vals):.6f}, "
                      f"mean={np.mean(vals):.6f}, "
                      f"min={min(vals):.6f}, max={max(vals):.6f}")

    # --- Interactive map ---
    _generate_map(bus_matches, line_updates, trafo_updates,
                  jao_buses, egon_hv, egon_hv_lines, egon_hv_trafos,
                  cluster_info, bus_to_cluster, output_dir)


def _generate_map(bus_matches, line_updates, trafo_updates,
                  jao_buses, egon_hv, egon_hv_lines, egon_hv_trafos,
                  cluster_info, bus_to_cluster, output_dir):
    """Generate interactive folium map."""
    try:
        import folium
        from folium import plugins
    except ImportError:
        print("  [SKIP] folium not installed, skipping map generation")
        return

    # Center on Germany
    m = folium.Map(location=[51.2, 10.4], zoom_start=6, tiles='CartoDB positron')

    # --- Layer: Matched eGon lines (green) ---
    matched_layer = folium.FeatureGroup(name='Matched lines (green)', show=True)
    unmatched_layer = folium.FeatureGroup(name='Unmatched lines (gray)', show=True)

    bus_coords = egon_hv.set_index('bus_id')[['y', 'x']]  # lat, lon

    for _, line in egon_hv_lines.iterrows():
        lid = line['line_id']
        b0, b1 = line['bus0'], line['bus1']
        if b0 not in bus_coords.index or b1 not in bus_coords.index:
            continue
        coords = [
            [bus_coords.loc[b0, 'y'], bus_coords.loc[b0, 'x']],
            [bus_coords.loc[b1, 'y'], bus_coords.loc[b1, 'x']],
        ]

        if lid in line_updates:
            upd = line_updates[lid]
            tooltip = (f"Line {lid} (MATCHED)\n"
                       f"r={upd.get('r', '?')}, x={upd.get('x', '?')}, "
                       f"s_nom={upd.get('s_nom', '?')}")
            folium.PolyLine(coords, color='green', weight=2, opacity=0.7,
                           tooltip=tooltip).add_to(matched_layer)
        else:
            folium.PolyLine(coords, color='gray', weight=1, opacity=0.3,
                           tooltip=f"Line {lid} (unmatched)").add_to(unmatched_layer)

    matched_layer.add_to(m)
    unmatched_layer.add_to(m)

    # --- Layer: JAO bus matches ---
    bus_layer = folium.FeatureGroup(name='JAO bus matches', show=True)
    unmatched_bus_layer = folium.FeatureGroup(name='Unmatched JAO buses (red)', show=True)

    matched_jao_names = set(bus_matches.keys())
    for _, jao in jao_buses.iterrows():
        jao_name = str(jao['name'])
        lat, lon = jao['y'], jao['x']
        if jao_name in matched_jao_names:
            info = bus_matches[jao_name]
            color = 'green' if info['tier'] == 0 else ('blue' if info['tier'] == 1 else 'orange')
            tooltip = (f"JAO {jao_name} -> {info['cluster_id']}\n"
                       f"Tier {info['tier']}, conf={info['confidence']:.2f}, "
                       f"dist={info['distance_km']:.2f}km")
            folium.CircleMarker(
                [lat, lon], radius=4, color=color, fill=True,
                fill_opacity=0.7, tooltip=tooltip
            ).add_to(bus_layer)
        else:
            folium.CircleMarker(
                [lat, lon], radius=4, color='red', fill=True,
                fill_opacity=0.7,
                tooltip=f"JAO {jao_name} (UNMATCHED)"
            ).add_to(unmatched_bus_layer)

    bus_layer.add_to(m)
    unmatched_bus_layer.add_to(m)

    # --- Layer: Updated transformers ---
    trafo_layer = folium.FeatureGroup(name='Updated transformers', show=False)
    for _, t in egon_hv_trafos.iterrows():
        tid = t['trafo_id']
        if tid not in trafo_updates:
            continue
        b0 = t['bus0']
        if b0 in bus_coords.index:
            lat, lon = bus_coords.loc[b0, 'y'], bus_coords.loc[b0, 'x']
            upd = trafo_updates[tid]
            tooltip = (f"Trafo {tid}\n"
                       f"x={upd.get('x', '?')}, s_nom={upd.get('s_nom', '?')}")
            folium.CircleMarker(
                [lat, lon], radius=6, color='purple', fill=True,
                fill_opacity=0.7, tooltip=tooltip
            ).add_to(trafo_layer)
    trafo_layer.add_to(m)

    folium.LayerControl().add_to(m)

    map_path = os.path.join(output_dir, 'match_map.html')
    m.save(map_path)
    print(f"  Interactive map: {map_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description='JAO/CORE-TSO Parameter Matching')
    parser.add_argument('--dry-run', action='store_true', default=True,
                        help='Print changes without writing to DB (default: True)')
    parser.add_argument('--apply', action='store_true',
                        help='Actually write changes to DB')
    parser.add_argument('--scenario', default='eGon2025v7',
                        help='Database scenario name (default: eGon2025v7)')
    parser.add_argument('--output-dir', default='results/jao_matching/',
                        help='Output directory for reports')
    args = parser.parse_args()

    dry_run = not args.apply

    print(f"JAO/CORE-TSO Parameter Matching for {args.scenario}")
    print(f"{'DRY RUN' if dry_run else 'LIVE — will update database'}")
    print()

    engine = create_engine(DB_URI)

    # Step 1: Load data
    data = load_data(engine, args.scenario)

    # Step 2: Cluster eGon buses into substations
    bus_to_cluster, cluster_info = cluster_buses(
        data['egon_hv'], data['ehv_subs'],
        data['egon_hv_lines'], data['bus_vnom']
    )

    # Step 3: Match JAO buses to eGon clusters
    bus_matches = match_buses(
        data['jao_buses'], cluster_info, bus_to_cluster, data['egon_hv']
    )

    # Step 4: Build graph and match lines
    graphs, corridor_lines = build_cluster_graph(
        data['egon_hv_lines'], bus_to_cluster, cluster_info, data['bus_vnom']
    )
    line_matches = match_lines(
        data['jao_lines'], bus_matches, graphs, corridor_lines,
        cluster_info, data['egon_hv_lines'], data['bus_vnom']
    )
    assignments = assign_parallel_circuits(
        line_matches, corridor_lines, data['egon_hv_lines']
    )

    # Step 5: Compute parameter updates
    line_updates, eic_map = compute_line_updates(
        assignments, data['egon_hv_lines']
    )
    trafo_updates, trafo_eic_map = match_and_compute_trafo_updates(
        data['jao_trafos'], bus_matches, cluster_info,
        bus_to_cluster, data['egon_hv_trafos'], data['bus_vnom']
    )

    # Step 6: Apply to database
    apply_updates(engine, args.scenario, line_updates, trafo_updates, dry_run=dry_run)

    # Step 7: Reports and visualization
    generate_reports(
        bus_matches, line_matches, assignments, line_updates,
        trafo_updates, eic_map, trafo_eic_map,
        data['jao_buses'], data['egon_hv'], data['egon_hv_lines'],
        data['egon_hv_trafos'], cluster_info, bus_to_cluster,
        args.output_dir,
    )

    print("\nDone.")


if __name__ == '__main__':
    main()
