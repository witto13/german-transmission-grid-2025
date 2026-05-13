#!/usr/bin/env python3
"""
Build eGon2025v4 from eGon2025v3
=================================

Eight-phase pipeline:
  1. Copy scenario v3 -> v4
  2. 110 kV substation simplification (400 m clustering)
  3. Similarity-based parameter assignment for unmatched lines
     (with multi-circuit scaling for cables > 3)
  4. Missing transformer detection
  4b. Insert missing transformers (9 clusters)
  5. Validation
  6. Interactive HTML map
  7. CSV/JSON reports

Usage:
    python scripts/build_v4.py --dry-run
    python scripts/build_v4.py --apply
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from scripts.simplify_substations import (
    copy_scenario,
    spatial_cluster,
    split_by_connectivity,
    build_mapping,
    apply_remapping,
    delete_self_loops,
    delete_orphaned_buses,
    count_connected_components,
    UnionFind,
    GRID_TABLES,
    SINGLE_BUS_TABLES,
    DUAL_BUS_TABLES,
    KM_PER_DEG_LAT,
    KM_PER_DEG_LON,
)
from scripts.lib.node_mapping import NodeMapping

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
SOURCE = 'eGon2025v3'
TARGET = 'eGon2025v4'
RESULTS_DIR = 'results/v4'

# JAO line updates from v3 pipeline
JAO_LINE_UPDATES_CSV = 'results/jao_v3/line_updates.csv'

# Clustering radii — only 110 kV is re-clustered (400 m)
CLUSTER_RADII = {110: 400, 220: 0, 380: 0}

# Missing-transformer detection radius (km)
TRAFO_CLUSTER_RADIUS_KM = 1.0

# Minimum group size for per-cable median fallback
MIN_GROUP_SIZE = 5


def _native(params):
    """Convert numpy types to native Python for psycopg2."""
    return {k: (float(v) if hasattr(v, 'item') else v) for k, v in params.items()}


# ===================================================================
# Phase 1: Copy Scenario
# ===================================================================
def phase1_copy(engine, dry_run):
    """Copy eGon2025v3 -> eGon2025v4."""
    print(f"\n{'='*70}")
    print("PHASE 1: Copy scenario")
    print(f"{'='*70}")
    if dry_run:
        print(f"  [DRY RUN] Would copy {SOURCE} -> {TARGET}")
        return None
    counts = copy_scenario(engine, SOURCE, TARGET)
    return counts


# ===================================================================
# Phase 2: 110 kV Substation Simplification (400 m)
# ===================================================================
def phase2_cluster_110kv(engine, scenario, dry_run):
    """Re-cluster 110 kV buses at 400 m radius."""
    print(f"\n{'='*70}")
    print("PHASE 2: 110 kV substation simplification (400 m)")
    print(f"{'='*70}")

    # Load buses and lines
    buses = pd.read_sql(text(
        "SELECT bus_id, v_nom, x, y FROM grid.egon_etrago_bus "
        "WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})

    lines = pd.read_sql(text(
        "SELECT line_id, bus0, bus1 FROM grid.egon_etrago_line "
        "WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})

    trafos = pd.read_sql(text(
        "SELECT trafo_id, bus0, bus1 FROM grid.egon_etrago_transformer "
        "WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})

    buses_set = set(buses['bus_id'].values)
    pre_components = count_connected_components(buses_set, lines, trafos)

    n_110 = len(buses[buses['v_nom'] == 110])
    print(f"  110 kV buses before: {n_110:,}")
    print(f"  Total buses:         {len(buses):,}")
    print(f"  Pre-components:      {pre_components}")

    # Spatial clustering — only 110 kV gets clustered; 220/380 get identity
    # We pass radius 0 for 220/380 so they don't cluster (spatial_cluster
    # skips radius=0 naturally since query_pairs(0) returns empty set).
    # But spatial_cluster requires positive radii, so we handle it by
    # only passing 110 kV radius and letting the rest become identity.
    radii = {110: CLUSTER_RADII[110]}
    cluster_map = spatial_cluster(buses, radii)

    # Count multi-bus clusters
    clusters = defaultdict(set)
    for bid, root in cluster_map.items():
        clusters[root].add(bid)
    multi_clusters = {r: m for r, m in clusters.items() if len(m) > 1}
    total_in_multi = sum(len(m) for m in multi_clusters.values())
    print(f"  Spatial clusters (>1 bus): {len(multi_clusters)}")
    print(f"  Buses in multi-clusters:   {total_in_multi}")

    # Split by connectivity
    merge_groups = split_by_connectivity(cluster_map, lines)
    total_to_merge = sum(len(g) for g in merge_groups)
    print(f"  Merge groups (connected):  {len(merge_groups)}")
    print(f"  Buses to merge:            {total_to_merge}")

    # Build mapping
    mapping = build_mapping(merge_groups, lines)
    print(f"  Buses to remove: {len(mapping.removed_nodes):,}")

    # Pre-compute which lines become self-loops (for map overlay)
    removed_bus_positions = {}
    bus_xy = {int(r['bus_id']): (float(r['x']), float(r['y']))
              for _, r in buses.iterrows()}
    for old_id in mapping.removed_nodes:
        if old_id in bus_xy:
            removed_bus_positions[int(old_id)] = bus_xy[old_id]

    self_loop_lines_before = []
    for _, row in lines.iterrows():
        b0 = mapping.map(int(row['bus0']))
        b1 = mapping.map(int(row['bus1']))
        if b0 == b1:
            lid = int(row['line_id'])
            orig_b0 = int(row['bus0'])
            orig_b1 = int(row['bus1'])
            self_loop_lines_before.append({
                'line_id': lid,
                'orig_bus0': orig_b0,
                'orig_bus1': orig_b1,
                'merged_bus': b0,
            })

    if dry_run:
        print(f"  [DRY RUN] Would remove {len(mapping.removed_nodes)} buses, "
              f"{len(self_loop_lines_before)} self-loop lines")
        return {
            'mapping': mapping,
            'pre_components': pre_components,
            'removed_bus_positions': removed_bus_positions,
            'self_loop_lines': self_loop_lines_before,
            'buses_removed': len(mapping.removed_nodes),
            'lines_removed': len(self_loop_lines_before),
        }

    # Apply
    apply_remapping(engine, scenario, mapping)
    lines_deleted, trafos_deleted = delete_self_loops(engine, scenario)
    buses_deleted = delete_orphaned_buses(engine, scenario)

    # Post counts
    post_buses = pd.read_sql(text(
        "SELECT COUNT(*) as n FROM grid.egon_etrago_bus WHERE scn_name = :scn"
    ), engine, params={'scn': scenario}).iloc[0]['n']
    post_lines = pd.read_sql(text(
        "SELECT COUNT(*) as n FROM grid.egon_etrago_line WHERE scn_name = :scn"
    ), engine, params={'scn': scenario}).iloc[0]['n']

    print(f"\n  Post-clustering:")
    print(f"    Buses:  {len(buses):,} -> {post_buses:,} (-{len(buses) - post_buses:,})")
    print(f"    Lines:  self-loops deleted: {lines_deleted:,}")
    print(f"    Trafos: self-loops deleted: {trafos_deleted:,}")
    print(f"    Orphaned buses deleted:     {buses_deleted:,}")

    return {
        'mapping': mapping,
        'pre_components': pre_components,
        'removed_bus_positions': removed_bus_positions,
        'self_loop_lines': self_loop_lines_before,
        'buses_removed': buses_deleted + len(mapping.removed_nodes),
        'lines_removed': lines_deleted,
        'trafos_removed': trafos_deleted,
    }


# ===================================================================
# Phase 3: Similarity-Based Parameter Assignment
# ===================================================================
def phase3_similarity_params(engine, scenario, dry_run):
    """Assign parameters to unmatched lines using similarity medians."""
    print(f"\n{'='*70}")
    print("PHASE 3: Similarity-based parameter assignment")
    print(f"{'='*70}")

    # Load JAO-matched line IDs
    jao_updates = pd.read_csv(JAO_LINE_UPDATES_CSV)
    jao_line_ids = set(jao_updates['line_id'].astype(int).values)
    print(f"  JAO-matched lines: {len(jao_line_ids):,}")

    # Load all lines with full params
    lines = pd.read_sql(text(
        "SELECT line_id, bus0, bus1, r, x, b, s_nom, length, v_nom, cables "
        "FROM grid.egon_etrago_line WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    print(f"  Total lines: {len(lines):,}")

    # Load bus voltages
    bus_vnom = pd.read_sql(text(
        "SELECT bus_id, v_nom FROM grid.egon_etrago_bus WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    bus_vnom_dict = dict(zip(bus_vnom['bus_id'], bus_vnom['v_nom']))

    # Ensure v_nom is filled from bus voltage
    for idx, row in lines.iterrows():
        if pd.isna(row['v_nom']) or row['v_nom'] == 0:
            v0 = bus_vnom_dict.get(row['bus0'], 110)
            v1 = bus_vnom_dict.get(row['bus1'], 110)
            lines.at[idx, 'v_nom'] = max(v0, v1)

    lines['v_nom'] = lines['v_nom'].astype(int)
    lines['cables'] = lines['cables'].fillna(3).astype(int)

    # --- 220/380 kV: Update unmatched lines ---
    ehv_lines = lines[lines['v_nom'].isin([220, 380])].copy()
    ehv_jao = ehv_lines[ehv_lines['line_id'].isin(jao_line_ids)].copy()
    ehv_unmatched = ehv_lines[~ehv_lines['line_id'].isin(jao_line_ids)].copy()

    print(f"\n  220/380 kV lines: {len(ehv_lines):,}")
    print(f"    JAO-matched:   {len(ehv_jao):,}")
    print(f"    Unmatched:     {len(ehv_unmatched):,}")

    # Compute per-km medians from JAO-matched lines
    ehv_jao_good = ehv_jao[(ehv_jao['length'] > 0.1) & (ehv_jao['r'] > 0) &
                           (ehv_jao['x'] > 0) & (ehv_jao['s_nom'] > 0)].copy()
    ehv_jao_good['r_km'] = ehv_jao_good['r'] / ehv_jao_good['length']
    ehv_jao_good['x_km'] = ehv_jao_good['x'] / ehv_jao_good['length']
    ehv_jao_good['b_km'] = ehv_jao_good['b'].fillna(0) / ehv_jao_good['length']

    # Group by (v_nom, cables) then voltage-only fallback
    ehv_medians_vc = ehv_jao_good.groupby(['v_nom', 'cables']).agg(
        r_km_med=('r_km', 'median'),
        x_km_med=('x_km', 'median'),
        b_km_med=('b_km', 'median'),
        s_nom_med=('s_nom', 'median'),
        count=('line_id', 'count'),
    ).reset_index()

    ehv_medians_v = ehv_jao_good.groupby('v_nom').agg(
        r_km_med=('r_km', 'median'),
        x_km_med=('x_km', 'median'),
        b_km_med=('b_km', 'median'),
        s_nom_med=('s_nom', 'median'),
        count=('line_id', 'count'),
    ).reset_index()

    print(f"\n  Per-km medians by (v_nom, cables):")
    for _, row in ehv_medians_vc.iterrows():
        print(f"    {int(row['v_nom'])}kV cables={int(row['cables'])}: "
              f"r={row['r_km_med']:.6f}/km x={row['x_km_med']:.6f}/km "
              f"s_nom={row['s_nom_med']:.1f} MVA (n={int(row['count'])})")

    def _get_ehv_median(v_nom, cables):
        """Look up median params; fall back to voltage-only if cable group too small."""
        vc_row = ehv_medians_vc[
            (ehv_medians_vc['v_nom'] == v_nom) &
            (ehv_medians_vc['cables'] == cables)
        ]
        if len(vc_row) == 1 and vc_row.iloc[0]['count'] >= MIN_GROUP_SIZE:
            return vc_row.iloc[0]
        # Fallback: voltage only
        v_row = ehv_medians_v[ehv_medians_v['v_nom'] == v_nom]
        if len(v_row) == 1:
            return v_row.iloc[0]
        return None

    # Build updates for unmatched 220/380 kV lines
    ehv_updates = {}
    for _, row in ehv_unmatched.iterrows():
        lid = int(row['line_id'])
        length = float(row['length'])
        if length <= 0.01:
            continue
        med = _get_ehv_median(int(row['v_nom']), int(row['cables']))
        if med is None:
            continue
        before = {
            'r': float(row['r']),
            'x': float(row['x']),
            'b': float(row['b']) if pd.notna(row['b']) else 0.0,
            's_nom': float(row['s_nom']),
        }
        after = {
            'r': float(med['r_km_med'] * length),
            'x': float(med['x_km_med'] * length),
            'b': float(med['b_km_med'] * length),
            's_nom': float(med['s_nom_med']),
        }
        # Scale for multi-circuit lines: median is per-circuit
        n_circuits = int(row['cables']) // 3
        if n_circuits > 1:
            after['s_nom'] *= n_circuits   # capacity adds in parallel
            after['r'] /= n_circuits       # impedance halves in parallel
            after['x'] /= n_circuits
            after['b'] *= n_circuits        # susceptance adds in parallel
        ehv_updates[lid] = {
            'before': before,
            'after': after,
            'v_nom': int(row['v_nom']),
            'cables': int(row['cables']),
            'length': length,
            'source': 'similarity_ehv',
        }

    print(f"  EHV similarity updates: {len(ehv_updates):,}")

    # --- 110 kV: Fix lines with bad params ---
    hv_lines = lines[lines['v_nom'] == 110].copy()
    hv_good = hv_lines[(hv_lines['r'] > 0) & (hv_lines['x'] > 0) &
                        (hv_lines['s_nom'] > 0) & (hv_lines['length'] > 0.1)].copy()
    hv_bad = hv_lines[(hv_lines['r'] <= 0) | (hv_lines['x'] <= 0) |
                       (hv_lines['s_nom'] <= 0)].copy()

    print(f"\n  110 kV lines: {len(hv_lines):,}")
    print(f"    Good params:   {len(hv_good):,}")
    print(f"    Bad params:    {len(hv_bad):,}")

    hv_updates = {}
    if len(hv_bad) > 0 and len(hv_good) > 0:
        hv_good['r_km'] = hv_good['r'] / hv_good['length']
        hv_good['x_km'] = hv_good['x'] / hv_good['length']
        hv_good['b_km'] = hv_good['b'].fillna(0) / hv_good['length']

        hv_medians_vc = hv_good.groupby('cables').agg(
            r_km_med=('r_km', 'median'),
            x_km_med=('x_km', 'median'),
            b_km_med=('b_km', 'median'),
            s_nom_med=('s_nom', 'median'),
            count=('line_id', 'count'),
        ).reset_index()

        hv_med_all = pd.Series({
            'r_km_med': hv_good['r_km'].median(),
            'x_km_med': hv_good['x_km'].median(),
            'b_km_med': hv_good['b_km'].median(),
            's_nom_med': hv_good['s_nom'].median(),
        })

        print(f"  110 kV medians (all): r={hv_med_all['r_km_med']:.6f}/km "
              f"x={hv_med_all['x_km_med']:.6f}/km s_nom={hv_med_all['s_nom_med']:.1f}")

        def _get_hv_median(cables):
            vc_row = hv_medians_vc[hv_medians_vc['cables'] == cables]
            if len(vc_row) == 1 and vc_row.iloc[0]['count'] >= MIN_GROUP_SIZE:
                return vc_row.iloc[0]
            return hv_med_all

        for _, row in hv_bad.iterrows():
            lid = int(row['line_id'])
            length = float(row['length'])
            if length <= 0.01:
                length = 1.0  # default 1 km for zero-length bad lines
            med = _get_hv_median(int(row['cables']))
            before = {
                'r': float(row['r']),
                'x': float(row['x']),
                'b': float(row['b']) if pd.notna(row['b']) else 0.0,
                's_nom': float(row['s_nom']),
            }
            after = {
                'r': float(med['r_km_med'] * length),
                'x': float(med['x_km_med'] * length),
                'b': float(med['b_km_med'] * length),
                's_nom': float(med['s_nom_med']),
            }
            # Scale for multi-circuit lines: median is per-circuit
            n_circuits = int(row['cables']) // 3
            if n_circuits > 1:
                after['s_nom'] *= n_circuits
                after['r'] /= n_circuits
                after['x'] /= n_circuits
                after['b'] *= n_circuits
            hv_updates[lid] = {
                'before': before,
                'after': after,
                'v_nom': 110,
                'cables': int(row['cables']),
                'length': length,
                'source': 'similarity_110kv',
            }

    print(f"  110 kV similarity updates: {len(hv_updates):,}")

    # Combine all updates
    all_updates = {**ehv_updates, **hv_updates}
    print(f"\n  Total similarity updates: {len(all_updates):,}")

    # Apply to DB
    if not dry_run and all_updates:
        applied = 0
        with engine.begin() as conn:
            for lid, info in all_updates.items():
                params = _native({
                    'r': info['after']['r'],
                    'x': info['after']['x'],
                    'b': info['after']['b'],
                    's_nom': info['after']['s_nom'],
                })
                conn.execute(text(
                    "UPDATE grid.egon_etrago_line "
                    "SET r = :r, x = :x, b = :b, s_nom = :s_nom "
                    "WHERE scn_name = :scn AND line_id = :lid"
                ), {**params, 'scn': scenario, 'lid': int(lid)})
                applied += 1
        print(f"  Applied {applied:,} line updates to DB")

    return all_updates


# ===================================================================
# Phase 4: Missing Transformer Detection
# ===================================================================
def _load_substation_bus_ids(engine):
    """Load the set of bus IDs that are actual substations.

    A bus is a substation if it appears in:
      - grid.egon_ehv_substation (380/220 kV)
      - grid.egon_hvmv_substation (110 kV)
      - osmtgmod_results.bus_data with a non-null osm_substation_id
    """
    sub_ids = set()

    ehv = pd.read_sql(text(
        "SELECT bus_id FROM grid.egon_ehv_substation"
    ), engine)
    sub_ids.update(ehv['bus_id'].astype(int).values)

    hvmv = pd.read_sql(text(
        "SELECT bus_id FROM grid.egon_hvmv_substation"
    ), engine)
    sub_ids.update(hvmv['bus_id'].astype(int).values)

    osm = pd.read_sql(text(
        "SELECT bus_i FROM osmtgmod_results.bus_data "
        "WHERE osm_substation_id IS NOT NULL AND osm_substation_id != 0"
    ), engine)
    sub_ids.update(osm['bus_i'].astype(int).values)

    return sub_ids


def phase4_missing_trafos(engine, scenario):
    """Detect multi-voltage substations with zero transformers.

    Only considers buses that are actual substations (EHV/HVMV tables or
    OSM-mapped).  Clusters that already have at least one transformer are
    skipped entirely — they are not "missing".
    """
    print(f"\n{'='*70}")
    print("PHASE 4: Missing transformer detection")
    print(f"{'='*70}")

    # Load substation bus IDs (all scenarios share the substation tables)
    substation_ids = _load_substation_bus_ids(engine)
    print(f"  Known substation buses: {len(substation_ids):,}")

    # Load scenario buses — only keep those that are substations
    all_buses = pd.read_sql(text(
        "SELECT bus_id, v_nom, x, y FROM grid.egon_etrago_bus "
        "WHERE scn_name = :scn AND bus_id < 200000"
    ), engine, params={'scn': scenario})

    buses = all_buses[all_buses['bus_id'].isin(substation_ids)].copy()
    print(f"  Scenario buses that are substations: {len(buses):,} / {len(all_buses):,}")

    trafos = pd.read_sql(text(
        "SELECT trafo_id, bus0, bus1 FROM grid.egon_etrago_transformer "
        "WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})

    bus_vnom = dict(zip(buses['bus_id'], buses['v_nom']))

    # Build spatial clusters across substations only
    if len(buses) == 0:
        print("  No substation buses found — skipping")
        return []

    coords_km = np.column_stack([
        buses['x'].values * KM_PER_DEG_LON,
        buses['y'].values * KM_PER_DEG_LAT,
    ])
    bus_ids = buses['bus_id'].values

    tree = cKDTree(coords_km)
    pairs = tree.query_pairs(TRAFO_CLUSTER_RADIUS_KM)

    uf = UnionFind()
    for bid in bus_ids:
        uf.find(int(bid))
    for i, j in pairs:
        uf.union(int(bus_ids[i]), int(bus_ids[j]))

    # Build clusters
    clusters = defaultdict(set)
    for bid in bus_ids:
        clusters[uf.find(int(bid))].add(int(bid))

    # Load ALL scenario buses for voltage lookup (not just substations)
    all_bus_vnom = pd.read_sql(text(
        "SELECT bus_id, v_nom FROM grid.egon_etrago_bus "
        "WHERE scn_name = :scn AND bus_id < 200000"
    ), engine, params={'scn': scenario})
    all_bus_vnom_dict = dict(zip(all_bus_vnom['bus_id'], all_bus_vnom['v_nom']))

    # Count transformers per cluster — a trafo "touches" a cluster if EITHER
    # endpoint is in it (not requiring both). This catches trafos connecting
    # cluster buses to intermediate buses outside the substation set.
    cluster_trafo_count = defaultdict(int)
    trafo_connections = defaultdict(set)  # cluster_root -> set of (v_low, v_high)
    for _, row in trafos.iterrows():
        b0, b1 = int(row['bus0']), int(row['bus1'])
        v0 = all_bus_vnom_dict.get(b0)
        v1 = all_bus_vnom_dict.get(b1)
        if v0 is None or v1 is None:
            continue
        root0 = uf.find(b0) if b0 in uf.parent else None
        root1 = uf.find(b1) if b1 in uf.parent else None
        roots = set()
        if root0 is not None:
            roots.add(root0)
        if root1 is not None:
            roots.add(root1)
        if not roots:
            continue
        v_low, v_high = sorted([int(v0), int(v1)])
        for root in roots:
            cluster_trafo_count[root] += 1
            trafo_connections[root].add((v_low, v_high))

    # Pre-index bus coords for fast lookup
    bus_xy = dict(zip(buses['bus_id'], zip(buses['x'], buses['y'])))

    # Check each multi-voltage cluster — skip if it already has any trafo
    missing = []
    multi_v_count = 0
    skipped_has_trafo = 0
    for root, members in clusters.items():
        voltages = set()
        for bid in members:
            v = bus_vnom.get(bid)
            if v:
                voltages.add(int(v))
        if len(voltages) < 2:
            continue
        multi_v_count += 1

        # If cluster already has at least one transformer, it's fine
        if cluster_trafo_count.get(root, 0) > 0:
            skipped_has_trafo += 1
            continue

        # No transformers at all in this multi-voltage substation cluster
        coords = [bus_xy[bid] for bid in members if bid in bus_xy]
        if coords:
            cx = float(np.mean([c[0] for c in coords]))
            cy = float(np.mean([c[1] for c in coords]))
        else:
            cx, cy = 0.0, 0.0

        # List which voltage connections are missing
        flags = []
        if 380 in voltages and 220 in voltages:
            flags.append('380-220')
        if 220 in voltages and 110 in voltages:
            flags.append('220-110')
        if 380 in voltages and 110 in voltages:
            flags.append('380-110')

        if flags:
            missing.append({
                'cluster_root': root,
                'x': cx,
                'y': cy,
                'voltages': sorted(voltages),
                'bus_ids': sorted(members),
                'existing_trafos': [],
                'missing': flags,
            })

    print(f"  Multi-voltage substation clusters: {multi_v_count:,}")
    print(f"  Already have transformer(s):       {skipped_has_trafo:,} (OK)")
    print(f"  Clusters with zero transformers:    {len(missing):,}")
    for m in missing[:10]:
        print(f"    Cluster @ ({m['x']:.3f}, {m['y']:.3f}): "
              f"voltages={m['voltages']}, missing={m['missing']}")
    if len(missing) > 10:
        print(f"    ... and {len(missing) - 10} more")

    return missing


# ===================================================================
# Phase 4b: Insert Missing Transformers
# ===================================================================

# Default parameters for new transformers
NEW_TRAFO_PARAMS = {
    (110, 380): {'x': 0.12, 'r': 0.003, 's_nom': 1200.0},
    (110, 220): {'x': 0.12, 'r': 0.003, 's_nom': 1200.0},
}
NEW_TRAFO_ID_START = 31366


def phase4b_insert_trafos(engine, scenario, missing_trafos, dry_run):
    """Insert transformers for truly missing multi-voltage clusters.

    For each missing cluster, picks the highest-degree bus per voltage
    level and inserts a transformer between them.
    """
    print(f"\n{'='*70}")
    print("PHASE 4b: Insert missing transformers")
    print(f"{'='*70}")

    if not missing_trafos:
        print("  No missing transformers to insert")
        return []

    # Load buses and lines for degree computation
    buses = pd.read_sql(text(
        "SELECT bus_id, v_nom, x, y FROM grid.egon_etrago_bus "
        "WHERE scn_name = :scn AND bus_id < 200000"
    ), engine, params={'scn': scenario})
    bus_vnom = dict(zip(buses['bus_id'], buses['v_nom']))

    lines = pd.read_sql(text(
        "SELECT line_id, bus0, bus1 FROM grid.egon_etrago_line "
        "WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})

    # Compute bus degree (number of line connections)
    degree = defaultdict(int)
    for _, row in lines.iterrows():
        degree[int(row['bus0'])] += 1
        degree[int(row['bus1'])] += 1

    # Get current max trafo_id
    max_tid = pd.read_sql(text(
        "SELECT COALESCE(MAX(trafo_id), 0) as m FROM grid.egon_etrago_transformer "
        "WHERE scn_name = :scn"
    ), engine, params={'scn': scenario}).iloc[0]['m']
    next_tid = max(int(max_tid) + 1, NEW_TRAFO_ID_START)

    new_trafos = []
    for m in missing_trafos:
        members = m['bus_ids']
        voltages = m['voltages']

        # Group buses by voltage
        buses_by_v = defaultdict(list)
        for bid in members:
            v = bus_vnom.get(bid)
            if v:
                buses_by_v[int(v)].append(bid)

        # Determine which transformer type to insert:
        # prefer 110-380 if both present, else 110-220
        if 110 in buses_by_v:
            if 380 in buses_by_v:
                v_low, v_high = 110, 380
            elif 220 in buses_by_v:
                v_low, v_high = 110, 220
            else:
                continue
        else:
            continue

        params = NEW_TRAFO_PARAMS.get((v_low, v_high))
        if params is None:
            continue

        # Pick highest-degree bus per voltage
        bus_low = max(buses_by_v[v_low], key=lambda b: degree.get(b, 0))
        bus_high = max(buses_by_v[v_high], key=lambda b: degree.get(b, 0))

        new_trafos.append({
            'trafo_id': next_tid,
            'scn_name': scenario,
            'bus0': int(bus_low),
            'bus1': int(bus_high),
            'x': params['x'],
            'r': params['r'],
            's_nom': params['s_nom'],
            's_max_pu': 1.0,
            'tap_ratio': 1.0,
            'model': 't',
            'b': 0.0,
            'g': 0.0,
            'build_year': 0,
            'lifetime': 40.0,
            'cluster_root': m['cluster_root'],
            'v_low': v_low,
            'v_high': v_high,
        })
        next_tid += 1

    print(f"  Transformers to insert: {len(new_trafos)}")
    for t in new_trafos:
        print(f"    trafo {t['trafo_id']}: bus {t['bus0']}({t['v_low']}kV) <-> "
              f"bus {t['bus1']}({t['v_high']}kV)  s_nom={t['s_nom']} MVA")

    if dry_run or not new_trafos:
        print(f"  [{'DRY RUN' if dry_run else 'No trafos to insert'}]")
        return new_trafos

    # Insert into DB
    df = pd.DataFrame(new_trafos)
    # Keep only columns that match the DB table
    db_cols = ['trafo_id', 'scn_name', 'bus0', 'bus1', 'x', 'r', 's_nom',
               's_max_pu', 'tap_ratio', 'model', 'b', 'g', 'build_year', 'lifetime']
    df_db = df[db_cols].copy()
    df_db.to_sql('egon_etrago_transformer', engine, schema='grid',
                 if_exists='append', index=False)
    print(f"  Inserted {len(df_db)} transformers into DB")

    return new_trafos


# ===================================================================
# Phase 5: Validation
# ===================================================================
def phase5_validate(engine, scenario, pre_components):
    """Run post-build validation checks."""
    print(f"\n{'='*70}")
    print("PHASE 5: Validation")
    print(f"{'='*70}")

    buses = pd.read_sql(text(
        "SELECT bus_id, v_nom FROM grid.egon_etrago_bus WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    lines = pd.read_sql(text(
        "SELECT line_id, bus0, bus1, r, x, s_nom FROM grid.egon_etrago_line "
        "WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    trafos = pd.read_sql(text(
        "SELECT trafo_id, bus0, bus1 FROM grid.egon_etrago_transformer "
        "WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    links = pd.read_sql(text(
        "SELECT link_id, bus0, bus1, carrier FROM grid.egon_etrago_link "
        "WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})

    bus_vnom = dict(zip(buses['bus_id'], buses['v_nom']))
    buses_set = set(buses['bus_id'].values)

    issues = []
    results = {}

    # 1. Connected components
    trafos_cc = trafos[['trafo_id', 'bus0', 'bus1']].copy()
    post_components = count_connected_components(buses_set, lines, trafos_cc)
    ok = post_components == pre_components
    results['components'] = {'pre': pre_components, 'post': post_components, 'ok': ok}
    print(f"  Components: {pre_components} -> {post_components} {'OK' if ok else 'MISMATCH!'}")
    if not ok:
        issues.append(f"Components changed: {pre_components} -> {post_components}")

    # 2. No self-loops
    sl_lines = int((lines['bus0'] == lines['bus1']).sum())
    sl_trafos = int((trafos['bus0'] == trafos['bus1']).sum())
    ok = sl_lines == 0 and sl_trafos == 0
    results['self_loops'] = {'lines': sl_lines, 'trafos': sl_trafos, 'ok': ok}
    print(f"  Self-loops: lines={sl_lines}, trafos={sl_trafos} {'OK' if ok else 'FAIL!'}")
    if not ok:
        issues.append(f"Self-loops remain: lines={sl_lines}, trafos={sl_trafos}")

    # 3. Lines connect same-voltage buses
    bad_lines = 0
    for _, row in lines.iterrows():
        v0 = bus_vnom.get(int(row['bus0']))
        v1 = bus_vnom.get(int(row['bus1']))
        if v0 is not None and v1 is not None and v0 != v1:
            bad_lines += 1
    ok = bad_lines == 0
    results['voltage_mismatch'] = {'count': bad_lines, 'ok': ok}
    print(f"  Voltage mismatches: {bad_lines} {'OK' if ok else 'FAIL!'}")
    if not ok:
        issues.append(f"{bad_lines} lines connect different voltages")

    # 4. No orphaned buses
    referenced = set()
    for _, row in lines.iterrows():
        referenced.add(int(row['bus0']))
        referenced.add(int(row['bus1']))
    for _, row in trafos.iterrows():
        referenced.add(int(row['bus0']))
        referenced.add(int(row['bus1']))
    for _, row in links.iterrows():
        referenced.add(int(row['bus0']))
        referenced.add(int(row['bus1']))
    orphans = buses_set - referenced
    ok = len(orphans) == 0
    results['orphans'] = {'count': len(orphans), 'ok': ok}
    print(f"  Orphaned buses: {len(orphans)} {'OK' if ok else 'FAIL!'}")
    if not ok:
        issues.append(f"{len(orphans)} orphaned buses")

    # 5. HVDC links preserved
    hvdc_links = links[links['carrier'] == 'DC']
    foreign_buses = buses[buses['bus_id'] >= 200000]
    results['hvdc'] = {'links': len(hvdc_links), 'foreign_buses': len(foreign_buses)}
    print(f"  HVDC links: {len(hvdc_links)}, foreign buses: {len(foreign_buses)}")
    if len(hvdc_links) < 3:
        issues.append(f"Only {len(hvdc_links)} HVDC links (expected 3)")

    # 6. All lines have positive params after similarity assignment
    bad_r = int((lines['r'] <= 0).sum())
    bad_x = int((lines['x'] <= 0).sum())
    bad_s = int((lines['s_nom'] <= 0).sum())
    ok = bad_r == 0 and bad_x == 0 and bad_s == 0
    results['line_params'] = {
        'bad_r': bad_r, 'bad_x': bad_x, 'bad_s': bad_s, 'ok': ok
    }
    print(f"  Line params: r<=0: {bad_r}, x<=0: {bad_x}, s_nom<=0: {bad_s} "
          f"{'OK' if ok else 'WARN'}")
    if not ok:
        issues.append(f"Bad line params: r<=0:{bad_r}, x<=0:{bad_x}, s_nom<=0:{bad_s}")

    results['issues'] = issues
    results['passed'] = len(issues) == 0

    return results


# ===================================================================
# Phase 6: Interactive HTML Map
# ===================================================================
def phase6_map(engine, scenario, cluster_info, similarity_updates, missing_trafos,
               new_trafos=None):
    """Generate interactive Leaflet map."""
    print(f"\n{'='*70}")
    print("PHASE 6: Interactive HTML map")
    print(f"{'='*70}")

    # Load all data
    buses = pd.read_sql(text(
        "SELECT bus_id, x, y, v_nom, country "
        "FROM grid.egon_etrago_bus WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})

    lines = pd.read_sql(text(
        "SELECT line_id, bus0, bus1, length, r, x, b, s_nom, v_nom, cables "
        "FROM grid.egon_etrago_line WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})

    trafos = pd.read_sql(text(
        "SELECT trafo_id, bus0, bus1, r, x, s_nom "
        "FROM grid.egon_etrago_transformer WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})

    links = pd.read_sql(text(
        "SELECT link_id, bus0, bus1, p_nom, carrier "
        "FROM grid.egon_etrago_link WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})

    bus_vnom = dict(zip(buses['bus_id'], buses['v_nom']))
    bus_xy = {int(r['bus_id']): (float(r['x']), float(r['y']))
              for _, r in buses.iterrows()}

    # Similarity-updated line IDs for highlight
    sim_line_ids = set(similarity_updates.keys())

    # Removed bus positions
    removed_positions = cluster_info.get('removed_bus_positions', {})

    # --- Build JSON data ---
    # Buses
    bus_json = []
    for _, b in buses.iterrows():
        entry = {
            'id': int(b['bus_id']),
            'lat': round(float(b['y']), 6),
            'lon': round(float(b['x']), 6),
            'v': int(b['v_nom']),
        }
        c = b['country'] if pd.notna(b['country']) else 'DE'
        if c != 'DE':
            entry['c'] = c
        bus_json.append(entry)

    # Lines
    line_json = []
    for _, ln in lines.iterrows():
        lid = int(ln['line_id'])
        b0, b1 = ln['bus0'], ln['bus1']
        if b0 not in bus_xy or b1 not in bus_xy:
            continue

        v = int(ln['v_nom']) if pd.notna(ln['v_nom']) and ln['v_nom'] > 0 \
            else int(max(bus_vnom.get(b0, 110), bus_vnom.get(b1, 110)))

        entry = {
            'id': lid,
            'b0': [round(bus_xy[b0][1], 6), round(bus_xy[b0][0], 6)],
            'b1': [round(bus_xy[b1][1], 6), round(bus_xy[b1][0], 6)],
            'v': v,
            'sn': round(float(ln['s_nom']), 1) if pd.notna(ln['s_nom']) else 0,
            'r': round(float(ln['r']), 6) if pd.notna(ln['r']) else 0,
            'x': round(float(ln['x']), 6) if pd.notna(ln['x']) else 0,
            'ln': round(float(ln['length']), 1) if pd.notna(ln['length']) else 0,
        }

        if lid in sim_line_ids:
            info = similarity_updates[lid]
            entry['sim'] = 1
            entry['br'] = round(info['before']['r'], 6)
            entry['bx'] = round(info['before']['x'], 6)
            entry['bs'] = round(info['before']['s_nom'], 1)

        line_json.append(entry)

    # Transformers
    trafo_json = []
    for _, t in trafos.iterrows():
        b0, b1 = t['bus0'], t['bus1']
        if b0 not in bus_xy or b1 not in bus_xy:
            continue
        v0 = int(bus_vnom.get(b0, 110))
        v1 = int(bus_vnom.get(b1, 110))
        trafo_json.append({
            'id': int(t['trafo_id']),
            'b0': [round(bus_xy[b0][1], 6), round(bus_xy[b0][0], 6)],
            'b1': [round(bus_xy[b1][1], 6), round(bus_xy[b1][0], 6)],
            'v0': v0, 'v1': v1,
            'sn': round(float(t['s_nom']), 1) if pd.notna(t['s_nom']) else 0,
        })

    # HVDC links
    hvdc_json = []
    for _, lk in links.iterrows():
        b0, b1 = lk['bus0'], lk['bus1']
        if b0 not in bus_xy or b1 not in bus_xy:
            continue
        hvdc_json.append({
            'id': int(lk['link_id']),
            'b0': [round(bus_xy[b0][1], 6), round(bus_xy[b0][0], 6)],
            'b1': [round(bus_xy[b1][1], 6), round(bus_xy[b1][0], 6)],
            'pn': round(float(lk['p_nom']), 0),
            'carrier': lk['carrier'],
        })

    # Removed buses
    removed_json = []
    for bid, (x, y) in removed_positions.items():
        removed_json.append({
            'id': int(bid),
            'lat': round(y, 6),
            'lon': round(x, 6),
        })

    # Missing transformers
    mt_json = []
    for m in missing_trafos:
        mt_json.append({
            'lat': round(m['y'], 6),
            'lon': round(m['x'], 6),
            'v': m['voltages'],
            'missing': m['missing'],
            'buses': m['bus_ids'][:10],  # Limit for tooltip
            'existing': m['existing_trafos'],
        })

    # New transformers (inserted in Phase 4b)
    nt_json = []
    for t in (new_trafos or []):
        b0, b1 = t['bus0'], t['bus1']
        if b0 in bus_xy and b1 in bus_xy:
            nt_json.append({
                'id': t['trafo_id'],
                'b0': [round(bus_xy[b0][1], 6), round(bus_xy[b0][0], 6)],
                'b1': [round(bus_xy[b1][1], 6), round(bus_xy[b1][0], 6)],
                'v0': t['v_low'], 'v1': t['v_high'],
                'sn': t['s_nom'],
            })

    # Stats
    stats = {
        'buses': len(bus_json),
        'lines': len(line_json),
        'trafos': len(trafo_json),
        'hvdc': len(hvdc_json),
        'removed_buses': len(removed_json),
        'sim_lines': len(sim_line_ids),
        'missing_trafos': len(mt_json),
        'new_trafos': len(nt_json),
    }

    print(f"  Buses: {stats['buses']}")
    print(f"  Lines: {stats['lines']} ({stats['sim_lines']} similarity-updated)")
    print(f"  Transformers: {stats['trafos']}")
    print(f"  HVDC links: {stats['hvdc']}")
    print(f"  Removed buses: {stats['removed_buses']}")
    print(f"  Missing trafo flags: {stats['missing_trafos']}")
    print(f"  New transformers: {stats['new_trafos']}")

    html = _build_v4_map_html(
        bus_json, line_json, trafo_json, hvdc_json,
        removed_json, mt_json, stats, nt_json
    )

    output_path = os.path.join(RESULTS_DIR, 'v4_build_map.html')
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        f.write(html)
    print(f"  Map saved: {output_path}")
    return output_path


def _build_v4_map_html(buses, lines, trafos, hvdc, removed, missing_trafos, stats,
                       new_trafos=None):
    """Build interactive Leaflet HTML map for v4 build."""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>eGon2025v4 — Build Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; background: #1a1a2e; }}
#map {{ position: absolute; top: 0; left: 0; right: 0; bottom: 0; z-index: 1; }}

#panel {{
  position: absolute; top: 12px; right: 12px; z-index: 1000;
  background: rgba(22,33,62,0.95); border: 1px solid #2a2a5e;
  border-radius: 10px; padding: 14px 16px; min-width: 260px;
  color: #e0e0e0; font-size: 13px; max-height: calc(100vh - 24px);
  overflow-y: auto; backdrop-filter: blur(8px);
}}
#panel h2 {{ font-size: 15px; color: #64b5f6; margin-bottom: 10px; font-weight: 600; }}
#panel h3 {{ font-size: 12px; color: #8899aa; margin: 10px 0 6px; text-transform: uppercase; letter-spacing: 0.5px; }}
.cb-row {{ display: flex; align-items: center; gap: 8px; padding: 3px 0; cursor: pointer; }}
.cb-row:hover {{ background: rgba(255,255,255,0.05); border-radius: 4px; }}
.cb-row input {{ accent-color: #64b5f6; cursor: pointer; }}
.cb-row label {{ cursor: pointer; flex: 1; }}
.color-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
.stat {{ color: #8899aa; font-size: 11px; }}
.stat b {{ color: #e0e0e0; }}
.divider {{ border-top: 1px solid #2a2a5e; margin: 8px 0; }}

.bus-tooltip {{
  background: rgba(22,33,62,0.95) !important;
  color: #e0e0e0 !important;
  border: 1px solid #2a2a5e !important;
  border-radius: 6px !important;
  padding: 8px 10px !important;
  font-family: 'Segoe UI', system-ui, sans-serif !important;
  font-size: 12px !important;
  line-height: 1.5 !important;
  box-shadow: 0 4px 12px rgba(0,0,0,0.4) !important;
  max-width: 350px !important;
}}
.bus-tooltip .tt-title {{ color: #64b5f6; font-weight: 600; font-size: 13px; }}
.bus-tooltip .tt-v {{ display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; margin-left: 4px; }}
.bus-tooltip .tt-param {{ color: #8899aa; }}
.bus-tooltip .tt-old {{ color: #ef5350; text-decoration: line-through; }}
.bus-tooltip .tt-new {{ color: #66bb6a; font-weight: 600; }}
.bus-tooltip .tt-warn {{ color: #ffb74d; font-weight: 600; }}

#legend {{
  position: absolute; bottom: 12px; left: 12px; z-index: 1000;
  background: rgba(22,33,62,0.92); border: 1px solid #2a2a5e;
  border-radius: 8px; padding: 10px 14px; color: #e0e0e0; font-size: 12px;
}}
#legend .leg-item {{ display: flex; align-items: center; gap: 6px; padding: 2px 0; }}
#legend .leg-line {{ width: 20px; height: 3px; border-radius: 1px; }}
#legend .leg-dash {{ width: 20px; height: 0; border-top: 3px dashed; }}
</style>
</head>
<body>
<div id="map"></div>

<div id="panel">
  <h2>eGon2025v4 — Build Map</h2>
  <div class="stat"><b>{stats['buses']}</b> buses &middot; <b>{stats['lines']}</b> lines &middot; <b>{stats['trafos']}</b> trafos</div>
  <div class="stat"><b>{stats['sim_lines']}</b> similarity-updated &middot; <b>{stats['removed_buses']}</b> removed buses</div>
  <div class="stat"><b>{stats['missing_trafos']}</b> missing-trafo flags</div>

  <div class="divider"></div>
  <h3>Voltage Levels</h3>
  <div class="cb-row">
    <input type="checkbox" id="v380" checked onchange="updateLayers()">
    <span class="color-dot" style="background:#e53935"></span>
    <label for="v380">380 kV</label>
  </div>
  <div class="cb-row">
    <input type="checkbox" id="v220" checked onchange="updateLayers()">
    <span class="color-dot" style="background:#1e88e5"></span>
    <label for="v220">220 kV</label>
  </div>
  <div class="cb-row">
    <input type="checkbox" id="v110" checked onchange="updateLayers()">
    <span class="color-dot" style="background:#43a047"></span>
    <label for="v110">110 kV</label>
  </div>

  <div class="divider"></div>
  <h3>Components</h3>
  <div class="cb-row">
    <input type="checkbox" id="showBuses" checked onchange="updateLayers()">
    <label for="showBuses">Buses</label>
  </div>
  <div class="cb-row">
    <input type="checkbox" id="showLines" checked onchange="updateLayers()">
    <label for="showLines">Lines</label>
  </div>
  <div class="cb-row">
    <input type="checkbox" id="showTrafos" checked onchange="updateLayers()">
    <label for="showTrafos">Transformers</label>
  </div>
  <div class="cb-row">
    <input type="checkbox" id="showHVDC" checked onchange="updateLayers()">
    <label for="showHVDC">HVDC Links ({stats['hvdc']})</label>
  </div>

  <div class="divider"></div>
  <h3>Overlays</h3>
  <div class="cb-row">
    <input type="checkbox" id="showRemoved" onchange="updateLayers()">
    <span class="color-dot" style="background:#ef5350"></span>
    <label for="showRemoved">Removed Buses ({stats['removed_buses']})</label>
  </div>
  <div class="cb-row">
    <input type="checkbox" id="showSim" onchange="updateLayers()">
    <span class="color-dot" style="background:#00e5ff"></span>
    <label for="showSim">Similarity-Updated ({stats['sim_lines']})</label>
  </div>
  <div class="cb-row">
    <input type="checkbox" id="showMT" onchange="updateLayers()">
    <span class="color-dot" style="background:#ffb74d"></span>
    <label for="showMT">Missing Trafos ({stats['missing_trafos']})</label>
  </div>
  <div class="cb-row">
    <input type="checkbox" id="showNT" checked onchange="updateLayers()">
    <span class="color-dot" style="background:#66bb6a"></span>
    <label for="showNT">New Trafos ({stats['new_trafos']})</label>
  </div>

  <div class="divider"></div>
  <h3>Highlight</h3>
  <div class="cb-row">
    <input type="checkbox" id="hlSim" onchange="updateLayers()">
    <label for="hlSim">Similarity lines only</label>
  </div>
</div>

<div id="legend">
  <div class="leg-item"><div class="leg-line" style="background:#e53935"></div> 380 kV</div>
  <div class="leg-item"><div class="leg-line" style="background:#1e88e5"></div> 220 kV</div>
  <div class="leg-item"><div class="leg-line" style="background:#43a047"></div> 110 kV</div>
  <div class="leg-item"><div class="leg-line" style="background:#00e5ff"></div> Similarity-updated</div>
  <div class="leg-item"><div class="leg-dash" style="border-color:#ab47bc"></div> Transformer</div>
  <div class="leg-item"><div class="leg-dash" style="border-color:#ff7043"></div> HVDC Link</div>
  <div class="leg-item"><div class="leg-line" style="background:#ef5350"></div> Removed bus</div>
  <div class="leg-item"><div class="leg-line" style="background:#ffb74d"></div> Missing trafo</div>
  <div class="leg-item"><div class="leg-dash" style="border-color:#66bb6a"></div> New trafo</div>
</div>

<script>
const BUSES = {json.dumps(buses, separators=(',', ':'))};
const LINES = {json.dumps(lines, separators=(',', ':'))};
const TRAFOS = {json.dumps(trafos, separators=(',', ':'))};
const HVDC = {json.dumps(hvdc, separators=(',', ':'))};
const REMOVED = {json.dumps(removed, separators=(',', ':'))};
const MT = {json.dumps(missing_trafos, separators=(',', ':'))};
const NT = {json.dumps(new_trafos or [], separators=(',', ':'))};

const V_COLORS = {{380:'#e53935', 220:'#1e88e5', 110:'#43a047'}};
const V_WEIGHTS = {{380:2.5, 220:2, 110:1.2}};

const map = L.map('map', {{
  center: [51.2, 10.4], zoom: 6, zoomControl: true, preferCanvas: true,
}});
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; OSM &copy; CARTO', maxZoom: 19,
}}).addTo(map);

let lineLayer, busLayer, trafoLayer, hvdcLayer, removedLayer, simLayer, mtLayer, ntLayer;

function vBadge(v) {{
  const c = V_COLORS[v] || '#888';
  return `<span class="tt-v" style="background:${{c}}40;color:${{c}}">${{v}} kV</span>`;
}}

function busTooltip(b) {{
  let h = `<div class="bus-tooltip"><div class="tt-title">Bus ${{b.id}} ${{vBadge(b.v)}}</div>`;
  if (b.c) h += `<div class="tt-param">Country: ${{b.c}}</div>`;
  h += `<div class="tt-param">${{b.lat.toFixed(4)}}, ${{b.lon.toFixed(4)}}</div></div>`;
  return h;
}}

function lineTooltip(l) {{
  let h = `<div class="bus-tooltip"><div class="tt-title">Line ${{l.id}} ${{vBadge(l.v)}}</div>`;
  h += `<div class="tt-param">Length: ${{l.ln}} km &middot; s_nom: ${{l.sn}} MVA</div>`;
  if (l.sim) {{
    h += `<div style="margin-top:4px"><span class="tt-new">SIMILARITY-UPDATED</span></div>`;
    h += `<table style="font-size:11px;margin-top:4px;border-collapse:collapse">`;
    h += `<tr><td class="tt-param" style="padding-right:8px">Param</td><td class="tt-param">Before</td><td class="tt-param">After</td></tr>`;
    h += `<tr><td>r</td><td class="tt-old">${{l.br.toFixed(4)}}</td><td class="tt-new">${{l.r.toFixed(4)}}</td></tr>`;
    h += `<tr><td>x</td><td class="tt-old">${{l.bx.toFixed(4)}}</td><td class="tt-new">${{l.x.toFixed(4)}}</td></tr>`;
    h += `<tr><td>s_nom</td><td class="tt-old">${{l.bs}}</td><td class="tt-new">${{l.sn}}</td></tr>`;
    h += `</table>`;
  }} else {{
    h += `<div class="tt-param">r=${{l.r.toFixed(4)}}, x=${{l.x.toFixed(4)}}</div>`;
  }}
  h += `</div>`;
  return h;
}}

function trafoTooltip(t) {{
  let h = `<div class="bus-tooltip"><div class="tt-title">Transformer ${{t.id}}</div>`;
  h += `<div class="tt-param">${{t.v0}}/${{t.v1}} kV &middot; s_nom: ${{t.sn}} MVA</div></div>`;
  return h;
}}

function hvdcTooltip(h) {{
  return `<div class="bus-tooltip"><div class="tt-title">HVDC Link ${{h.id}}</div>`
    + `<div class="tt-param">Capacity: ${{h.pn}} MW &middot; ${{h.carrier}}</div></div>`;
}}

function removedTooltip(r) {{
  return `<div class="bus-tooltip"><div class="tt-title" style="color:#ef5350">Removed Bus ${{r.id}}</div>`
    + `<div class="tt-param">Merged during 110kV clustering</div></div>`;
}}

function mtTooltip(m) {{
  let h = `<div class="bus-tooltip"><div class="tt-title"><span class="tt-warn">Missing Transformer</span></div>`;
  h += `<div class="tt-param">Voltages: ${{m.v.join(', ')}} kV</div>`;
  h += `<div class="tt-param">Missing: ${{m.missing.join(', ')}}</div>`;
  if (m.existing.length) h += `<div class="tt-param">Existing: ${{m.existing.join(', ')}}</div>`;
  h += `<div class="tt-param">Buses: ${{m.buses.join(', ')}}</div></div>`;
  return h;
}}

function getVis() {{
  return {{
    v380: document.getElementById('v380').checked,
    v220: document.getElementById('v220').checked,
    v110: document.getElementById('v110').checked,
    buses: document.getElementById('showBuses').checked,
    lines: document.getElementById('showLines').checked,
    trafos: document.getElementById('showTrafos').checked,
    hvdc: document.getElementById('showHVDC').checked,
    removed: document.getElementById('showRemoved').checked,
    sim: document.getElementById('showSim').checked,
    mt: document.getElementById('showMT').checked,
    nt: document.getElementById('showNT').checked,
    hlSim: document.getElementById('hlSim').checked,
  }};
}}

function vVisible(v, vis) {{
  if (v === 380) return vis.v380;
  if (v === 220) return vis.v220;
  return vis.v110;
}}

function updateLayers() {{
  const vis = getVis();
  [lineLayer, busLayer, trafoLayer, hvdcLayer, removedLayer, simLayer, mtLayer, ntLayer]
    .forEach(l => {{ if (l) map.removeLayer(l); }});

  // Lines
  if (vis.lines) {{
    lineLayer = L.layerGroup();
    LINES.forEach(l => {{
      if (!vVisible(l.v, vis)) return;
      if (vis.hlSim && !l.sim) return;
      const isSim = l.sim && vis.sim;
      const color = isSim ? '#00e5ff' : (V_COLORS[l.v] || '#888');
      const weight = isSim ? 3 : (V_WEIGHTS[l.v] || 1);
      const opacity = isSim ? 0.9 : 0.55;
      L.polyline([l.b0, l.b1], {{color, weight, opacity}})
        .bindTooltip(lineTooltip(l), {{className:'bus-tooltip',sticky:true}})
        .addTo(lineLayer);
    }});
    lineLayer.addTo(map);
  }}

  // Transformers
  if (vis.trafos) {{
    trafoLayer = L.layerGroup();
    TRAFOS.forEach(t => {{
      const v = Math.max(t.v0, t.v1);
      if (!vVisible(v, vis) && !vVisible(Math.min(t.v0,t.v1), vis)) return;
      L.polyline([t.b0, t.b1], {{
        color: '#ab47bc', weight: 3, opacity: 0.8, dashArray: '5,5',
      }}).bindTooltip(trafoTooltip(t), {{className:'bus-tooltip',sticky:true}})
        .addTo(trafoLayer);
    }});
    trafoLayer.addTo(map);
  }}

  // HVDC
  if (vis.hvdc) {{
    hvdcLayer = L.layerGroup();
    HVDC.forEach(h => {{
      L.polyline([h.b0, h.b1], {{
        color: '#ff7043', weight: 3.5, opacity: 0.9, dashArray: '10,6',
      }}).bindTooltip(hvdcTooltip(h), {{className:'bus-tooltip',sticky:true}})
        .addTo(hvdcLayer);
    }});
    hvdcLayer.addTo(map);
  }}

  // Removed buses
  if (vis.removed) {{
    removedLayer = L.layerGroup();
    REMOVED.forEach(r => {{
      L.circleMarker([r.lat, r.lon], {{
        radius: 4, color: '#ef5350', fillColor: '#ef5350', fillOpacity: 0.8, weight: 1,
      }}).bindTooltip(removedTooltip(r), {{className:'bus-tooltip'}})
        .addTo(removedLayer);
    }});
    removedLayer.addTo(map);
  }}

  // Missing transformer flags
  if (vis.mt) {{
    mtLayer = L.layerGroup();
    MT.forEach(m => {{
      L.circleMarker([m.lat, m.lon], {{
        radius: 8, color: '#ffb74d', fillColor: '#ffb74d', fillOpacity: 0.7, weight: 2,
      }}).bindTooltip(mtTooltip(m), {{className:'bus-tooltip'}})
        .addTo(mtLayer);
    }});
    mtLayer.addTo(map);
  }}

  // New transformers (inserted in Phase 4b)
  if (vis.nt) {{
    ntLayer = L.layerGroup();
    NT.forEach(t => {{
      L.polyline([t.b0, t.b1], {{
        color: '#66bb6a', weight: 4, opacity: 0.9, dashArray: '8,4',
      }}).bindTooltip(
        `<div class="bus-tooltip"><div class="tt-title" style="color:#66bb6a">New Transformer ${{t.id}}</div>`
        + `<div class="tt-param">${{t.v0}}/${{t.v1}} kV &middot; s_nom: ${{t.sn}} MVA</div></div>`,
        {{className:'bus-tooltip',sticky:true}}
      ).addTo(ntLayer);
    }});
    ntLayer.addTo(map);
  }}

  // Buses (top)
  if (vis.buses) {{
    busLayer = L.layerGroup();
    BUSES.forEach(b => {{
      if (!vVisible(b.v, vis)) return;
      const color = V_COLORS[b.v] || '#888';
      L.circleMarker([b.lat, b.lon], {{
        radius: 2.5, color, fillColor: color, fillOpacity: 0.7, weight: 0.5,
      }}).bindTooltip(busTooltip(b), {{className:'bus-tooltip'}})
        .addTo(busLayer);
    }});
    busLayer.addTo(map);
  }}
}}

updateLayers();
</script>
</body>
</html>"""


# ===================================================================
# Phase 7: CSV/JSON Reports
# ===================================================================
def phase7_reports(cluster_info, similarity_updates, missing_trafos,
                   validation, scenario, v3_counts, v4_counts):
    """Save all reports to results/v4/."""
    print(f"\n{'='*70}")
    print("PHASE 7: Reports")
    print(f"{'='*70}")

    os.makedirs(RESULTS_DIR, exist_ok=True)

    # Node mapping CSV
    mapping = cluster_info.get('mapping')
    if mapping and len(mapping.removed_nodes) > 0:
        mapping.save_csv(os.path.join(RESULTS_DIR, 'node_mapping.csv'))
        print(f"  node_mapping.csv: {len(mapping.removed_nodes)} entries")
    else:
        print(f"  node_mapping.csv: no bus merges")

    # Similarity updates CSV
    if similarity_updates:
        rows = []
        for lid, info in similarity_updates.items():
            rows.append({
                'line_id': lid,
                'v_nom': info['v_nom'],
                'cables': info['cables'],
                'length': info['length'],
                'source': info['source'],
                'before_r': info['before']['r'],
                'before_x': info['before']['x'],
                'before_b': info['before']['b'],
                'before_s_nom': info['before']['s_nom'],
                'after_r': info['after']['r'],
                'after_x': info['after']['x'],
                'after_b': info['after']['b'],
                'after_s_nom': info['after']['s_nom'],
            })
        pd.DataFrame(rows).to_csv(
            os.path.join(RESULTS_DIR, 'similarity_updates.csv'), index=False)
        print(f"  similarity_updates.csv: {len(rows)} rows")

    # Missing transformers CSV
    if missing_trafos:
        rows = []
        for m in missing_trafos:
            rows.append({
                'cluster_root': m['cluster_root'],
                'x': m['x'],
                'y': m['y'],
                'voltages': ','.join(str(v) for v in m['voltages']),
                'missing': ','.join(m['missing']),
                'existing_trafos': ','.join(m['existing_trafos']),
                'bus_ids': ','.join(str(b) for b in m['bus_ids']),
            })
        pd.DataFrame(rows).to_csv(
            os.path.join(RESULTS_DIR, 'missing_transformers.csv'), index=False)
        print(f"  missing_transformers.csv: {len(rows)} rows")

    # Summary JSON
    summary = {
        'timestamp': datetime.now().isoformat(),
        'source': SOURCE,
        'target': scenario,
        'v3_counts': v3_counts,
        'v4_counts': v4_counts,
        'clustering': {
            'radii': CLUSTER_RADII,
            'buses_removed': cluster_info.get('buses_removed', 0),
            'lines_removed': cluster_info.get('lines_removed', 0),
        },
        'similarity': {
            'ehv_updates': sum(1 for v in similarity_updates.values()
                               if v['source'] == 'similarity_ehv'),
            'hv_updates': sum(1 for v in similarity_updates.values()
                              if v['source'] == 'similarity_110kv'),
            'total': len(similarity_updates),
        },
        'missing_trafos': len(missing_trafos),
        'validation': validation,
    }
    with open(os.path.join(RESULTS_DIR, 'summary.json'), 'w') as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"  summary.json written")

    return summary


# ===================================================================
# Helper: Get scenario counts
# ===================================================================
def _get_counts(engine, scenario):
    """Get component counts for a scenario."""
    counts = {}
    for name, table in [('buses', 'grid.egon_etrago_bus'),
                         ('lines', 'grid.egon_etrago_line'),
                         ('trafos', 'grid.egon_etrago_transformer'),
                         ('links', 'grid.egon_etrago_link'),
                         ('generators', 'grid.egon_etrago_generator'),
                         ('loads', 'grid.egon_etrago_load')]:
        r = pd.read_sql(text(
            f"SELECT COUNT(*) as n FROM {table} WHERE scn_name = :scn"
        ), engine, params={'scn': scenario})
        counts[name] = int(r.iloc[0]['n'])
    return counts


# ===================================================================
# Main Pipeline
# ===================================================================
def run_pipeline(dry_run=True):
    """Run the full v4 build pipeline."""
    engine = create_engine(DB_URI)

    print("=" * 70)
    print("Build eGon2025v4 from eGon2025v3")
    print(f"Mode: {'DRY RUN' if dry_run else 'APPLY'}")
    print("=" * 70)

    # Get v3 counts
    v3_counts = _get_counts(engine, SOURCE)
    print(f"\n  v3 counts: {v3_counts}")

    # Determine working scenario
    scenario = TARGET if not dry_run else SOURCE

    # Phase 1: Copy
    phase1_copy(engine, dry_run)

    # Phase 2: 110 kV clustering
    cluster_info = phase2_cluster_110kv(engine, scenario, dry_run)

    # Phase 3: Similarity params
    similarity_updates = phase3_similarity_params(engine, scenario, dry_run)

    # Phase 4: Missing trafos (detection)
    missing_trafos = phase4_missing_trafos(engine, scenario)

    # Phase 4b: Insert missing transformers
    new_trafos = phase4b_insert_trafos(engine, scenario, missing_trafos, dry_run)

    # Phase 5: Validation
    pre_components = cluster_info.get('pre_components', 0)
    validation = phase5_validate(engine, scenario, pre_components)

    # Get v4 counts
    v4_counts = _get_counts(engine, scenario)

    # Phase 6: Map (always generated)
    phase6_map(engine, scenario, cluster_info, similarity_updates, missing_trafos,
               new_trafos)

    # Phase 7: Reports
    summary = phase7_reports(
        cluster_info, similarity_updates, missing_trafos,
        validation, scenario, v3_counts, v4_counts,
    )

    # Final summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"  Buses:        {v3_counts['buses']:,} -> {v4_counts['buses']:,}")
    print(f"  Lines:        {v3_counts['lines']:,} -> {v4_counts['lines']:,}")
    print(f"  Transformers: {v3_counts['trafos']:,} -> {v4_counts['trafos']:,}")
    print(f"  Links:        {v3_counts['links']:,} -> {v4_counts['links']:,}")
    print(f"  Similarity:   {len(similarity_updates):,} lines updated")
    print(f"  Missing trafos: {len(missing_trafos):,} flagged")
    print(f"  Validation:   {'PASSED' if validation['passed'] else 'FAILED'}")
    if validation.get('issues'):
        for issue in validation['issues']:
            print(f"    WARNING: {issue}")
    print(f"\n  Results: {RESULTS_DIR}/")

    return summary


# ===================================================================
# CLI
# ===================================================================
def main():
    parser = argparse.ArgumentParser(
        description='Build eGon2025v4 from eGon2025v3')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true',
                      help='Analyse without modifying the database')
    mode.add_argument('--apply', action='store_true',
                      help='Create eGon2025v4 in the database')
    args = parser.parse_args()

    run_pipeline(dry_run=not args.apply)


if __name__ == '__main__':
    main()
