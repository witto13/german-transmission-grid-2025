#!/usr/bin/env python3
"""
Degree-2 Waypoint Elimination — eGon2025v2 → eGon2025v3
=========================================================

Eliminates pure degree-2 waypoint buses on 220/380kV lines.  These are
intermediate nodes along transmission lines that don't correspond to real
substations.  When bus B is eliminated from chain A—B—C, the two lines
A→B and B→C are replaced by one merged line A→C with series-combined
impedance (r/x summed, s_nom = bottleneck, length = sum).

A bus is eliminable if ALL conditions hold:
  1. Exactly 2 line connections (degree-2 in line graph)
  2. No transformer connected
  3. No OSM substation ID (osmtgmod_results.bus_data.osm_substation_id)
  4. Not in grid.egon_ehv_substation
  5. No different-voltage bus within 1 km
  6. v_nom is 220 or 380 kV
  7. No link connected
  8. No generator, load, storage, or store connected

Usage:
    python scripts/simplify_degree2.py --dry-run
    python scripts/simplify_degree2.py --apply
    python scripts/simplify_degree2.py --apply --source eGon2025v2 --target eGon2025v3
"""

import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from sqlalchemy import create_engine, text

# Add project root so we can import from scripts.*
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.reduction.v4.degree2_elimination import Degree2Eliminator
from scripts.simplify_substations import (
    copy_scenario,
    count_connected_components,
    GRID_TABLES,
    SINGLE_BUS_TABLES,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'

DEFAULT_SOURCE = 'eGon2025v2'
DEFAULT_TARGET = 'eGon2025v3'

# Approximate km per degree at ~52°N
KM_PER_DEG_LAT = 111.0
KM_PER_DEG_LON = 71.5

# Distance threshold for "different-voltage bus nearby" check (km)
NEARBY_THRESHOLD_KM = 1.0


# ---------------------------------------------------------------------------
# Phase 2: Load data
# ---------------------------------------------------------------------------

def load_data(engine, scenario):
    """Load all components needed for degree-2 analysis."""
    print(f"\n{'='*70}")
    print(f"Phase 2: Load data from {scenario}")
    print(f"{'='*70}")

    buses = pd.read_sql(text(
        "SELECT bus_id, v_nom, x, y FROM grid.egon_etrago_bus WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    print(f"  Buses: {len(buses):,}")

    lines = pd.read_sql(text(
        "SELECT line_id, bus0, bus1, x, r, g, b, s_nom, length, v_nom, "
        "       cables, carrier, s_nom_extendable, s_nom_min, s_nom_max, "
        "       s_max_pu, build_year, lifetime, capital_cost, "
        "       terrain_factor, num_parallel, v_ang_min, v_ang_max, type "
        "FROM grid.egon_etrago_line WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    print(f"  Lines: {len(lines):,}")

    trafos = pd.read_sql(text(
        "SELECT trafo_id, bus0, bus1 FROM grid.egon_etrago_transformer WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    print(f"  Transformers: {len(trafos):,}")

    links = pd.read_sql(text(
        "SELECT link_id, bus0, bus1 FROM grid.egon_etrago_link WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    print(f"  Links: {len(links):,}")

    generators = pd.read_sql(text(
        "SELECT generator_id, bus FROM grid.egon_etrago_generator WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    print(f"  Generators: {len(generators):,}")

    loads = pd.read_sql(text(
        "SELECT load_id, bus FROM grid.egon_etrago_load WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    print(f"  Loads: {len(loads):,}")

    storages = pd.read_sql(text(
        "SELECT storage_id, bus FROM grid.egon_etrago_storage WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    print(f"  Storage: {len(storages):,}")

    stores = pd.read_sql(text(
        "SELECT store_id, bus FROM grid.egon_etrago_store WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    print(f"  Stores: {len(stores):,}")

    # OSM substation IDs
    osm_subs = pd.read_sql(text(
        "SELECT bus_i, osm_substation_id FROM osmtgmod_results.bus_data "
        "WHERE osm_substation_id IS NOT NULL AND osm_substation_id != 0"
    ), engine)
    print(f"  OSM substation mappings: {len(osm_subs):,}")

    # EHV substation bus IDs
    ehv_subs = pd.read_sql(text(
        "SELECT bus_id FROM grid.egon_ehv_substation"
    ), engine)
    print(f"  EHV substation buses: {len(ehv_subs):,}")

    return {
        'buses': buses,
        'lines': lines,
        'trafos': trafos,
        'links': links,
        'generators': generators,
        'loads': loads,
        'storages': storages,
        'stores': stores,
        'osm_subs': osm_subs,
        'ehv_subs': ehv_subs,
    }


# ---------------------------------------------------------------------------
# Phase 3: Build protected bus set
# ---------------------------------------------------------------------------

def build_protected_set(data):
    """Build set of buses that must NOT be eliminated.

    A bus is protected if ANY of:
      1. v_nom == 110 (only simplify 220/380 kV)
      2. Has a transformer connected
      3. Has an OSM substation ID
      4. Is in grid.egon_ehv_substation
      5. Has a different-voltage bus within 1 km
      6. Has a link connected
      7. Has a generator/load/storage/store connected
    """
    buses = data['buses']
    lines = data['lines']
    trafos = data['trafos']
    links = data['links']

    protected = set()

    # 1. All 110 kV buses
    mask_110 = buses['v_nom'] == 110
    protected.update(buses.loc[mask_110, 'bus_id'].values)
    print(f"    110 kV buses:              {mask_110.sum():,}")

    # 2. Buses with transformers
    trafo_buses = set()
    if len(trafos) > 0:
        trafo_buses.update(trafos['bus0'].astype(int).values)
        trafo_buses.update(trafos['bus1'].astype(int).values)
    protected.update(trafo_buses)
    print(f"    Transformer-connected:     {len(trafo_buses):,}")

    # 3. OSM substation IDs
    osm_bus_ids = set(data['osm_subs']['bus_i'].astype(int).values)
    protected.update(osm_bus_ids)
    print(f"    OSM substation buses:      {len(osm_bus_ids):,}")

    # 4. EHV substation buses
    ehv_bus_ids = set(data['ehv_subs']['bus_id'].astype(int).values)
    protected.update(ehv_bus_ids)
    print(f"    EHV substation buses:      {len(ehv_bus_ids):,}")

    # 5. Different-voltage bus within 1 km (KDTree)
    nearby_count = _find_cross_voltage_neighbors(buses, protected)
    print(f"    Cross-voltage neighbors:   {nearby_count:,}")

    # 6. Link-connected buses
    link_buses = set()
    if len(links) > 0:
        link_buses.update(links['bus0'].astype(int).values)
        link_buses.update(links['bus1'].astype(int).values)
    protected.update(link_buses)
    print(f"    Link-connected:            {len(link_buses):,}")

    # 7. Buses with generators/loads/storage/stores
    component_buses = set()
    for key in ('generators', 'loads', 'storages', 'stores'):
        df = data[key]
        if len(df) > 0:
            component_buses.update(df['bus'].astype(int).values)
    protected.update(component_buses)
    print(f"    Generator/load/storage:    {len(component_buses):,}")

    return protected


def _find_cross_voltage_neighbors(buses, protected):
    """Find buses that have a different-voltage neighbor within 1 km.

    These are likely at real substations even if they don't have an
    explicit OSM substation ID.
    """
    if len(buses) < 2:
        return 0

    # Build KDTree over all buses
    coords_km = np.column_stack([
        buses['x'].values * KM_PER_DEG_LON,
        buses['y'].values * KM_PER_DEG_LAT,
    ])
    bus_ids = buses['bus_id'].values
    bus_vnoms = buses['v_nom'].values

    tree = cKDTree(coords_km)
    pairs = tree.query_pairs(NEARBY_THRESHOLD_KM)

    count = 0
    for i, j in pairs:
        if bus_vnoms[i] != bus_vnoms[j]:
            bid_i = int(bus_ids[i])
            bid_j = int(bus_ids[j])
            if bid_i not in protected:
                protected.add(bid_i)
                count += 1
            if bid_j not in protected:
                protected.add(bid_j)
                count += 1

    return count


# ---------------------------------------------------------------------------
# Phase 5: Apply to database
# ---------------------------------------------------------------------------

def apply_elimination(engine, scenario, lines_to_delete, buses_to_delete,
                      merged_lines):
    """Apply degree-2 elimination to the database in a single transaction."""
    print(f"\n{'='*70}")
    print(f"Phase 5: Apply to database")
    print(f"{'='*70}")

    with engine.begin() as conn:
        # 1. Delete old lines
        if lines_to_delete:
            line_ids = list(lines_to_delete)
            # Delete in batches to avoid overly long IN clauses
            batch_size = 500
            total_deleted = 0
            for i in range(0, len(line_ids), batch_size):
                batch = line_ids[i:i + batch_size]
                placeholders = ','.join(str(int(lid)) for lid in batch)
                r = conn.execute(text(
                    f"DELETE FROM grid.egon_etrago_line "
                    f"WHERE scn_name = :scn AND line_id IN ({placeholders})"
                ), {'scn': scenario})
                total_deleted += r.rowcount
            print(f"  Lines deleted: {total_deleted:,}")

        # 2. Insert new merged lines
        if merged_lines:
            for ml in merged_lines:
                conn.execute(text("""
                    INSERT INTO grid.egon_etrago_line
                    (scn_name, line_id, bus0, bus1, x, r, g, b, s_nom, length,
                     v_nom, cables, carrier, s_nom_extendable, s_nom_min,
                     s_nom_max, s_max_pu, build_year, lifetime, capital_cost,
                     terrain_factor, num_parallel, v_ang_min, v_ang_max, type,
                     geom, topo)
                    VALUES
                    (:scn, :line_id, :bus0, :bus1, :x, :r, :g, :b, :s_nom,
                     :length, :v_nom, :cables, :carrier, :s_nom_extendable,
                     :s_nom_min, :s_nom_max, :s_max_pu, :build_year, :lifetime,
                     :capital_cost, :terrain_factor, :num_parallel, :v_ang_min,
                     :v_ang_max, :type, :geom, :topo)
                """), {
                    'scn': scenario,
                    'line_id': int(ml['line_id']),
                    'bus0': int(ml['bus0']),
                    'bus1': int(ml['bus1']),
                    'x': float(ml['x']),
                    'r': float(ml['r']),
                    'g': float(ml.get('g', 0) or 0),
                    'b': float(ml.get('b', 0) or 0),
                    's_nom': float(ml['s_nom']),
                    'length': float(ml['length']),
                    'v_nom': float(ml['v_nom']) if ml.get('v_nom') is not None else None,
                    'cables': int(ml['cables']) if ml.get('cables') is not None and pd.notna(ml.get('cables')) else None,
                    'carrier': ml.get('carrier', 'AC'),
                    's_nom_extendable': bool(ml.get('s_nom_extendable', False)) if ml.get('s_nom_extendable') is not None else False,
                    's_nom_min': float(ml.get('s_nom_min', 0) or 0),
                    's_nom_max': float(ml.get('s_nom_max', 0) or 0) if ml.get('s_nom_max') is not None and pd.notna(ml.get('s_nom_max')) else None,
                    's_max_pu': float(ml.get('s_max_pu', 1) or 1),
                    'build_year': int(ml.get('build_year', 0) or 0),
                    'lifetime': float(ml.get('lifetime', 0) or 0) if ml.get('lifetime') is not None and pd.notna(ml.get('lifetime')) else None,
                    'capital_cost': float(ml.get('capital_cost', 0) or 0),
                    'terrain_factor': float(ml.get('terrain_factor', 1) or 1),
                    'num_parallel': float(ml.get('num_parallel', 1) or 1),
                    'v_ang_min': float(ml.get('v_ang_min', 0) or 0) if ml.get('v_ang_min') is not None and pd.notna(ml.get('v_ang_min')) else None,
                    'v_ang_max': float(ml.get('v_ang_max', 0) or 0) if ml.get('v_ang_max') is not None and pd.notna(ml.get('v_ang_max')) else None,
                    'type': ml.get('type', ''),
                    'geom': None,
                    'topo': None,
                })
            print(f"  Lines inserted: {len(merged_lines):,}")

        # 3. Delete eliminated buses
        if buses_to_delete:
            bus_ids = list(buses_to_delete)
            batch_size = 500
            total_deleted = 0
            for i in range(0, len(bus_ids), batch_size):
                batch = bus_ids[i:i + batch_size]
                placeholders = ','.join(str(int(bid)) for bid in batch)
                r = conn.execute(text(
                    f"DELETE FROM grid.egon_etrago_bus "
                    f"WHERE scn_name = :scn AND bus_id IN ({placeholders})"
                ), {'scn': scenario})
                total_deleted += r.rowcount
            print(f"  Buses deleted: {total_deleted:,}")


# ---------------------------------------------------------------------------
# Phase 6: Validation
# ---------------------------------------------------------------------------

def validate(engine, scenario, pre_counts, pre_components):
    """Post-elimination validation."""
    print(f"\n{'='*70}")
    print(f"Phase 6: Validation")
    print(f"{'='*70}")

    buses = pd.read_sql(text(
        "SELECT bus_id, v_nom FROM grid.egon_etrago_bus WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    lines = pd.read_sql(text(
        "SELECT line_id, bus0, bus1 FROM grid.egon_etrago_line WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    trafos = pd.read_sql(text(
        "SELECT trafo_id, bus0, bus1 FROM grid.egon_etrago_transformer WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})

    buses_set = set(buses['bus_id'].values)
    bus_vnom = dict(zip(buses['bus_id'], buses['v_nom']))
    post_components = count_connected_components(buses_set, lines, trafos)

    issues = []

    # 1. Connected components preserved
    if post_components != pre_components:
        issues.append(
            f"Connected components changed: {pre_components} -> {post_components}")
    print(f"  Connected components: {pre_components} -> {post_components} "
          f"{'OK' if post_components == pre_components else 'MISMATCH!'}")

    # 2. No self-loops
    self_loops = len(lines[lines['bus0'] == lines['bus1']])
    if self_loops > 0:
        issues.append(f"{self_loops} self-loops remain")
    print(f"  Self-loops: {self_loops} "
          f"{'OK' if self_loops == 0 else 'FAIL!'}")

    # 3. Lines connect same-voltage buses
    bad_lines = 0
    for _, row in lines.iterrows():
        v0 = bus_vnom.get(int(row['bus0']))
        v1 = bus_vnom.get(int(row['bus1']))
        if v0 is not None and v1 is not None and v0 != v1:
            bad_lines += 1
    if bad_lines > 0:
        issues.append(f"{bad_lines} lines connect different-voltage buses")
    print(f"  Voltage mismatch lines: {bad_lines} "
          f"{'OK' if bad_lines == 0 else 'FAIL!'}")

    # 4. No orphaned buses
    referenced = set()
    for _, row in lines.iterrows():
        referenced.add(int(row['bus0']))
        referenced.add(int(row['bus1']))
    for _, row in trafos.iterrows():
        referenced.add(int(row['bus0']))
        referenced.add(int(row['bus1']))
    for tbl, col in SINGLE_BUS_TABLES:
        df = pd.read_sql(text(
            f"SELECT {col} FROM {tbl} WHERE scn_name = :scn"
        ), engine, params={'scn': scenario})
        if len(df) > 0:
            referenced.update(df[col].astype(int).values)
    link_df = pd.read_sql(text(
        "SELECT bus0, bus1 FROM grid.egon_etrago_link WHERE scn_name = :scn"
    ), engine, params={'scn': scenario})
    if len(link_df) > 0:
        referenced.update(link_df['bus0'].astype(int).values)
        referenced.update(link_df['bus1'].astype(int).values)

    orphans = buses_set - referenced
    if orphans:
        issues.append(f"{len(orphans)} orphaned buses remain")
    print(f"  Orphaned buses: {len(orphans)} "
          f"{'OK' if len(orphans) == 0 else 'FAIL!'}")

    post_counts = {
        'buses': len(buses),
        'lines': len(lines),
        'transformers': len(trafos),
    }

    return {
        'pre_components': pre_components,
        'post_components': post_components,
        'post_counts': post_counts,
        'self_loops': self_loops,
        'voltage_mismatch_lines': bad_lines,
        'orphaned_buses': len(orphans),
        'issues': issues,
        'passed': len(issues) == 0,
    }


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(source, target, dry_run=True):
    """Run the full degree-2 elimination pipeline."""
    engine = create_engine(DB_URI)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    print(f"\nDegree-2 Waypoint Elimination Pipeline")
    print(f"  Source:  {source}")
    print(f"  Target:  {target}")
    print(f"  Mode:    {'DRY RUN' if dry_run else 'APPLY'}")

    # Phase 1: Copy scenario
    if not dry_run:
        print(f"\n{'='*70}")
        print(f"Phase 1: Copy scenario {source} -> {target}")
        print(f"{'='*70}")
        copy_scenario(engine, source, target)
    else:
        print(f"\n{'='*70}")
        print(f"Phase 1: [DRY RUN] Would copy {source} -> {target}")
        print(f"{'='*70}")
        # In dry-run, analyse the source scenario directly
        target = source

    # Phase 2: Load data
    data = load_data(engine, target)
    buses = data['buses']
    lines = data['lines']
    trafos = data['trafos']

    # Pre-elimination stats
    all_bus_ids = set(buses['bus_id'].astype(int).values)
    buses_set_for_topo = set(buses['bus_id'].values)
    trafos_for_cc = pd.DataFrame({
        'bus0': trafos['bus0'] if len(trafos) > 0 else pd.Series(dtype=int),
        'bus1': trafos['bus1'] if len(trafos) > 0 else pd.Series(dtype=int),
    })
    lines_for_cc = pd.DataFrame({
        'bus0': lines['bus0'],
        'bus1': lines['bus1'],
    })
    pre_components = count_connected_components(
        buses_set_for_topo, lines_for_cc, trafos_for_cc)

    pre_counts = {
        'buses': len(buses),
        'lines': len(lines),
        'transformers': len(trafos),
        'components': pre_components,
    }

    print(f"\n  Pre-elimination:")
    print(f"    Buses:      {pre_counts['buses']:,}")
    print(f"    Lines:      {pre_counts['lines']:,}")
    print(f"    Trafos:     {pre_counts['transformers']:,}")
    print(f"    Components: {pre_components}")

    # Phase 3: Build protected set
    print(f"\n{'='*70}")
    print(f"Phase 3: Build protected bus set")
    print(f"{'='*70}")

    # Only consider 220/380 kV lines for degree-2 analysis
    lines_220_380 = lines[lines['v_nom'].isin([220, 380])].copy()
    print(f"  220/380 kV lines: {len(lines_220_380):,}")

    protected = build_protected_set(data)
    print(f"  Total protected buses: {len(protected):,}")

    # Phase 4: Run Degree2Eliminator
    print(f"\n{'='*70}")
    print(f"Phase 4: Degree-2 chain detection")
    print(f"{'='*70}")

    # Only pass 220/380kV lines to the eliminator
    bus_ids_220_380 = set(
        buses.loc[buses['v_nom'].isin([220, 380]), 'bus_id'].astype(int).values
    )

    eliminator = Degree2Eliminator(lines_220_380, protected, bus_ids_220_380)
    analysis = eliminator.analyze()

    print(f"  Compressed edges: {analysis['compressed_edges']:,}")
    print(f"  Parallel groups: {analysis['parallel_groups']:,}")
    print(f"  Eliminable buses: {analysis['eliminable_count']:,}")
    print(f"  Chains found: {analysis['chain_count']:,}")

    if analysis['eliminable_count'] == 0:
        print("\n  No eliminable buses found. Nothing to do.")
        return None

    # Show chain length distribution
    chain_lengths = [len(c) - 2 for c in analysis['chains']]  # -2 for endpoints
    if chain_lengths:
        print(f"  Chain interior sizes: min={min(chain_lengths)}, "
              f"max={max(chain_lengths)}, "
              f"total_interior={sum(chain_lengths)}")

    # Per-voltage breakdown
    bus_vnom = dict(zip(buses['bus_id'].astype(int), buses['v_nom'].astype(int)))
    elim_380 = sum(1 for b in analysis['eliminable_buses'] if bus_vnom.get(b) == 380)
    elim_220 = sum(1 for b in analysis['eliminable_buses'] if bus_vnom.get(b) == 220)
    print(f"  By voltage: 380kV={elim_380}, 220kV={elim_220}")

    # Compute merged lines
    max_line_id = int(lines['line_id'].max())
    next_line_id = max_line_id + 1
    merged_lines = eliminator.compute_merged_lines(next_line_id)
    lines_to_delete = eliminator.get_lines_to_delete()

    print(f"\n  Merged lines to create: {len(merged_lines):,}")
    print(f"  Lines to delete: {len(lines_to_delete):,}")
    print(f"  Buses to delete: {analysis['eliminable_count']:,}")

    if dry_run:
        print(f"\n{'='*70}")
        print(f"DRY RUN complete — no database changes made")
        print(f"{'='*70}")

        # Export analysis for review
        output_dir = 'results/degree2_elimination'
        os.makedirs(output_dir, exist_ok=True)

        summary = {
            'timestamp': timestamp,
            'source': source,
            'target': target,
            'mode': 'dry_run',
            'pre_counts': pre_counts,
            'eliminable_buses': analysis['eliminable_count'],
            'eliminable_380': elim_380,
            'eliminable_220': elim_220,
            'chains': analysis['chain_count'],
            'merged_lines': len(merged_lines),
            'lines_to_delete': len(lines_to_delete),
        }
        with open(os.path.join(output_dir, f'summary_dryrun_{timestamp}.json'), 'w') as f:
            json.dump(summary, f, indent=2)

        # Export eliminated bus list
        elim_records = [
            {'bus_id': int(b), 'v_nom': bus_vnom.get(b, 0)}
            for b in sorted(analysis['eliminable_buses'])
        ]
        pd.DataFrame(elim_records).to_csv(
            os.path.join(output_dir, f'eliminated_buses_dryrun_{timestamp}.csv'),
            index=False)

        # Export merged line parameters
        if merged_lines:
            pd.DataFrame(merged_lines).to_csv(
                os.path.join(output_dir, f'merged_lines_dryrun_{timestamp}.csv'),
                index=False)

        return {
            'eliminable_buses': analysis['eliminable_buses'],
            'chains': analysis['chains'],
            'merged_lines': merged_lines,
            'lines_to_delete': lines_to_delete,
        }

    # Phase 5: Apply to database
    apply_elimination(
        engine, target,
        lines_to_delete=lines_to_delete,
        buses_to_delete=analysis['eliminable_buses'],
        merged_lines=merged_lines,
    )

    # Phase 6: Validation
    validation = validate(engine, target, pre_counts, pre_components)

    # Export results
    output_dir = 'results/degree2_elimination'
    os.makedirs(output_dir, exist_ok=True)

    # Node mapping CSV
    elim_records = [
        {'old_bus': int(b), 'new_bus': 'DELETED', 'v_nom': bus_vnom.get(b, 0),
         'reason': 'degree2_elimination'}
        for b in sorted(analysis['eliminable_buses'])
    ]
    pd.DataFrame(elim_records).to_csv(
        os.path.join(output_dir, 'node_mapping.csv'), index=False)

    summary = {
        'timestamp': timestamp,
        'source': source,
        'target': target,
        'mode': 'apply',
        'pre_counts': pre_counts,
        'post_counts': validation['post_counts'],
        'eliminable_buses': analysis['eliminable_count'],
        'eliminable_380': elim_380,
        'eliminable_220': elim_220,
        'chains': analysis['chain_count'],
        'merged_lines_created': len(merged_lines),
        'lines_deleted': len(lines_to_delete),
        'buses_deleted': analysis['eliminable_count'],
        'validation': {
            'passed': validation['passed'],
            'issues': validation['issues'],
            'pre_components': validation['pre_components'],
            'post_components': validation['post_components'],
        },
    }
    with open(os.path.join(output_dir, 'summary.json'), 'w') as f:
        json.dump(summary, f, indent=2)

    # Merged lines detail
    if merged_lines:
        pd.DataFrame(merged_lines).to_csv(
            os.path.join(output_dir, 'merged_lines.csv'), index=False)

    print(f"\n{'='*70}")
    print(f"SUMMARY")
    print(f"{'='*70}")
    print(f"  Buses:        {pre_counts['buses']:,} -> "
          f"{validation['post_counts']['buses']:,} "
          f"(-{pre_counts['buses'] - validation['post_counts']['buses']:,})")
    print(f"  Lines:        {pre_counts['lines']:,} -> "
          f"{validation['post_counts']['lines']:,} "
          f"(-{pre_counts['lines'] - validation['post_counts']['lines']:,})")
    print(f"  Transformers: {pre_counts['transformers']:,} -> "
          f"{validation['post_counts']['transformers']:,}")
    print(f"  Components:   {pre_components} -> "
          f"{validation['post_components']}")
    print(f"  Validation:   "
          f"{'PASSED' if validation['passed'] else 'FAILED'}")
    if validation['issues']:
        for issue in validation['issues']:
            print(f"    WARNING: {issue}")

    print(f"\n  Results saved to: {output_dir}/")

    return {
        'eliminable_buses': analysis['eliminable_buses'],
        'chains': analysis['chains'],
        'merged_lines': merged_lines,
        'lines_to_delete': lines_to_delete,
        'validation': validation,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description='Eliminate degree-2 waypoint buses on 220/380 kV lines')
    parser.add_argument('--source', default=DEFAULT_SOURCE,
                        help=f'Source scenario (default: {DEFAULT_SOURCE})')
    parser.add_argument('--target', default=DEFAULT_TARGET,
                        help=f'Target scenario (default: {DEFAULT_TARGET})')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true',
                      help='Analyse without modifying the database')
    mode.add_argument('--apply', action='store_true',
                      help='Apply changes to the database')
    return parser.parse_args()


def main():
    args = parse_args()
    run_pipeline(
        source=args.source,
        target=args.target,
        dry_run=args.dry_run,
    )


if __name__ == '__main__':
    main()
