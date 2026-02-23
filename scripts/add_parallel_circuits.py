#!/usr/bin/env python3
"""
Add Missing Parallel Circuits to eGon2025
==========================================

Cross-examination of eGon2025 vs JAO/CORE-TSO shows ~90 corridors where JAO
reports more parallel circuits than eGon has line segments. This is caused by
OSM modeling double-circuit towers as a single line (cables=3 instead of 6).

This script duplicates existing eGon line segments for corridors where JAO
reports more circuits, using single-hop matches only (most reliable).

Usage:
    python scripts/add_parallel_circuits.py --dry-run     # preview
    python scripts/add_parallel_circuits.py --apply        # write to DB
"""

import argparse
import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jao_matching import (
    load_data, cluster_buses, match_buses, build_cluster_graph,
    DB_URI, PATH_LENGTH_MIN, PATH_LENGTH_MAX,
)
import networkx as nx

SCN = 'eGon2025'
RESULTS_DIR = 'results/jao_params'


def find_single_hop_jao(jao_lines, bus_matches, graphs, corridor_lines,
                        egon_hv_lines):
    """Map JAO lines to eGon corridors, returning only single-hop matches."""
    line_data = egon_hv_lines.set_index('line_id')
    single_hop = defaultdict(list)  # corridor_key -> [jao line rows]

    for _, jao_line in jao_lines.iterrows():
        b0_base = jao_line['bus0_base']
        b1_base = jao_line['bus1_base']
        jao_v = int(jao_line['v_nom_mapped'])

        m0 = bus_matches.get(b0_base)
        m1 = bus_matches.get(b1_base)
        if not m0 or not m1:
            continue

        c0, c1 = m0['cluster_id'], m1['cluster_id']
        if c0 == c1 or jao_v not in graphs:
            continue

        G = graphs[jao_v]
        if c0 not in G or c1 not in G:
            continue

        try:
            path = nx.shortest_path(G, c0, c1, weight='weight')
        except nx.NetworkXNoPath:
            continue

        if len(path) != 2:
            continue  # Only single-hop

        # Validate path length
        jao_length = jao_line['length'] if pd.notna(jao_line['length']) else 0
        corridor_key = (min(c0, c1), max(c0, c1), jao_v)
        seg_lids = corridor_lines.get(corridor_key, [])
        if seg_lids and jao_length > 0:
            seg_length = sum(
                line_data.loc[lid, 'length']
                for lid in seg_lids if lid in line_data.index
            )
            if seg_length > 0:
                ratio = seg_length / jao_length
                if ratio < PATH_LENGTH_MIN or ratio > PATH_LENGTH_MAX:
                    continue

        single_hop[corridor_key].append(jao_line)

    return single_hop


def compute_duplications(corridor_lines, single_hop, egon_hv_lines, bus_vnom):
    """Determine which corridors need additional line segments."""
    line_data = egon_hv_lines.set_index('line_id')
    duplications = []  # list of {corridor_key, n_to_add, template_line_id, ...}

    for corridor_key, jao_list in single_hop.items():
        c0, c1, voltage = corridor_key
        egon_lids = corridor_lines.get(corridor_key, [])
        n_egon = len(egon_lids)

        # Deduplicate JAO lines by index
        seen = set()
        unique_jao = []
        for jl in jao_list:
            if jl.name not in seen:
                seen.add(jl.name)
                unique_jao.append(jl)
        n_jao = len(unique_jao)

        if n_jao <= n_egon:
            continue  # No duplication needed

        n_to_add = n_jao - n_egon

        # Pick the first existing eGon line as template
        template_lid = None
        for lid in egon_lids:
            if lid in line_data.index:
                template_lid = lid
                break

        if template_lid is None:
            continue

        duplications.append({
            'corridor_key': corridor_key,
            'n_to_add': n_to_add,
            'template_line_id': template_lid,
            'n_egon': n_egon,
            'n_jao': n_jao,
            'voltage': voltage,
        })

    return duplications


def execute_duplications(engine, duplications, dry_run):
    """Create duplicate line records in the database."""
    print("\n" + "=" * 70)
    print(f"PARALLEL CIRCUIT DUPLICATION {'(DRY RUN)' if dry_run else '(LIVE)'}")
    print("=" * 70)

    if not duplications:
        print("  No duplications needed")
        return []

    # Load full line data for templates
    template_ids = [d['template_line_id'] for d in duplications]
    template_ids_str = ','.join(str(int(lid)) for lid in template_ids)
    templates = pd.read_sql(text(
        f"SELECT * FROM grid.egon_etrago_line "
        f"WHERE scn_name = :scn AND line_id IN ({template_ids_str})"
    ), engine, params={'scn': SCN})
    templates = templates.set_index('line_id')

    # Get max existing line_id
    max_lid = pd.read_sql(text(
        "SELECT MAX(line_id) as max_id FROM grid.egon_etrago_line "
        "WHERE scn_name = :scn"
    ), engine, params={'scn': SCN}).iloc[0]['max_id']
    next_lid = int(max_lid) + 1

    # Summary by voltage
    by_voltage = defaultdict(lambda: {'corridors': 0, 'lines': 0})
    total_lines = 0
    new_lines = []

    for dup in duplications:
        template_lid = dup['template_line_id']
        if template_lid not in templates.index:
            continue

        row = templates.loc[template_lid]
        v = dup['voltage']
        by_voltage[v]['corridors'] += 1

        for i in range(dup['n_to_add']):
            new_line = row.to_dict()
            new_line['line_id'] = next_lid
            # Keep same bus0, bus1, length, r, x, b, s_nom, cables, etc.
            new_lines.append(new_line)
            next_lid += 1
            total_lines += 1
            by_voltage[v]['lines'] += 1

    for v in sorted(by_voltage):
        info = by_voltage[v]
        print(f"  {v}kV: {info['corridors']} corridors, "
              f"+{info['lines']} new lines")
    print(f"  Total: {len(duplications)} corridors, +{total_lines} new lines")

    if dry_run:
        # Show a few examples
        print("\n  Sample duplications:")
        for dup in duplications[:10]:
            template_lid = dup['template_line_id']
            if template_lid in templates.index:
                row = templates.loc[template_lid]
                print(f"    Line {template_lid}: bus {int(row['bus0'])} -> "
                      f"{int(row['bus1'])}, {row['length']:.1f}km, "
                      f"s_nom={row['s_nom']:.0f}MW "
                      f"({dup['n_egon']} existing + {dup['n_to_add']} new = "
                      f"{dup['n_jao']} JAO circuits)")
        return new_lines

    # Insert new lines
    new_df = pd.DataFrame(new_lines)
    # Ensure correct types
    for col in ['line_id', 'bus0', 'bus1']:
        new_df[col] = new_df[col].astype(int)
    for col in ['r', 'x', 'b', 's_nom', 'length', 'v_nom', 'num_parallel',
                'cables', 'x_pu', 'r_pu', 'g', 'b_pu', 's_max_pu',
                'terrain_factor']:
        if col in new_df.columns:
            new_df[col] = pd.to_numeric(new_df[col], errors='coerce')

    new_df.to_sql('egon_etrago_line', engine, schema='grid',
                   if_exists='append', index=False)
    print(f"\n  Inserted {len(new_df)} new line records")

    return new_lines


def save_report(duplications, new_lines, templates_df=None):
    """Save duplication report."""
    os.makedirs(RESULTS_DIR, exist_ok=True)

    rows = []
    for dup in duplications:
        rows.append({
            'corridor': f"{dup['corridor_key'][0]}_{dup['corridor_key'][1]}",
            'voltage_kv': dup['voltage'],
            'template_line_id': dup['template_line_id'],
            'egon_existing': dup['n_egon'],
            'jao_circuits': dup['n_jao'],
            'lines_added': dup['n_to_add'],
        })

    if rows:
        df = pd.DataFrame(rows)
        path = os.path.join(RESULTS_DIR, 'parallel_circuit_additions.csv')
        df.to_csv(path, index=False)
        print(f"\n  Report saved: {path} ({len(df)} rows)")


def main():
    parser = argparse.ArgumentParser(
        description='Add missing parallel circuits to eGon2025')
    parser.add_argument('--dry-run', action='store_true', default=True,
                        help='Preview changes (default)')
    parser.add_argument('--apply', action='store_true',
                        help='Write changes to database')
    args = parser.parse_args()
    dry_run = not args.apply

    print("=" * 70)
    print("Add Missing Parallel Circuits to eGon2025")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print("=" * 70)

    engine = create_engine(DB_URI)

    # Reuse jao_matching pipeline
    print("\nLoading data and matching buses...")
    data = load_data(engine, SCN)
    bus_to_cluster, cluster_info = cluster_buses(
        data['egon_hv'], data['ehv_subs'],
        data['egon_hv_lines'], data['bus_vnom']
    )
    bus_matches = match_buses(
        data['jao_buses'], cluster_info, bus_to_cluster, data['egon_hv']
    )
    graphs, corridor_lines = build_cluster_graph(
        data['egon_hv_lines'], bus_to_cluster, cluster_info, data['bus_vnom']
    )

    # Find single-hop JAO line matches
    print("\nMapping JAO lines to corridors (single-hop only)...")
    single_hop = find_single_hop_jao(
        data['jao_lines'], bus_matches, graphs, corridor_lines,
        data['egon_hv_lines']
    )
    n_corridors_with_jao = len(single_hop)
    n_jao_lines = sum(len(v) for v in single_hop.values())
    print(f"  {n_corridors_with_jao} corridors with single-hop JAO lines "
          f"({n_jao_lines} JAO lines total)")

    # Determine which corridors need duplication
    print("\nComputing needed duplications...")
    duplications = compute_duplications(
        corridor_lines, single_hop, data['egon_hv_lines'], data['bus_vnom']
    )

    # Execute
    new_lines = execute_duplications(engine, duplications, dry_run)

    # Report
    save_report(duplications, new_lines)

    print("\nDone.")


if __name__ == '__main__':
    main()
