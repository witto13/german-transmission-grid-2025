#!/usr/bin/env python3
"""
Compare parallel circuit counts between eGon2025 and JAO/CORE-TSO datasets.

Strategy:
---------
A "corridor" is a direct connection between two adjacent substation clusters at a
given voltage level. Each eGon line segment in that corridor represents one physical
circuit (or possibly N circuits if cables > 3).

JAO lines connect named substations which may or may not be adjacent in the eGon
topology. For a fair comparison, we:
  1. Map each JAO line to a path through the eGon cluster graph
  2. Only use single-hop matches (JAO endpoints = adjacent eGon clusters)
     for the primary circuit count comparison, since these are unambiguous
  3. For multi-hop JAO lines, we still note them but handle separately

For each matched corridor:
  - eGon circuits = max(cables) / 3 across segments (or number of segments if inconsistent)
  - JAO circuits = number of distinct single-hop JAO lines in that corridor

Reuses jao_matching.py functions for bus matching and graph building.
"""

import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd
from sqlalchemy import create_engine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.jao_matching import (
    load_data, cluster_buses, match_buses, build_cluster_graph,
    DB_URI, DE_TSOS, JAO_DIR,
    PATH_LENGTH_MIN, PATH_LENGTH_MAX,
)
import networkx as nx

SCENARIO = 'eGon2025'


def load_cables_column(engine, scenario):
    """Load the cables column separately (not included in load_data's egon_hv_lines)."""
    cables_df = pd.read_sql(
        f"SELECT line_id, cables FROM grid.egon_etrago_line WHERE scn_name = '{scenario}'",
        engine
    )
    return cables_df.set_index('line_id')['cables'].to_dict()


def map_jao_lines_to_corridors(jao_lines, bus_matches, graphs, corridor_lines,
                                cluster_info, egon_hv_lines, bus_vnom):
    """
    Map each JAO line to its path through the eGon cluster graph.
    Classifies each as single-hop (direct corridor) or multi-hop.

    Returns:
        single_hop: dict (corridor_key) -> [list of jao line info dicts]
        multi_hop: dict (corridor_key) -> [list of jao line info dicts]
                   (same JAO line appears in all corridors along its path)
        jao_details: list of all JAO line match details
    """
    line_data = egon_hv_lines.set_index('line_id')

    single_hop = defaultdict(list)
    multi_hop = defaultdict(list)
    jao_details = []
    stats = defaultdict(int)

    for jao_idx, jao_line in jao_lines.iterrows():
        b0_base = jao_line['bus0_base']
        b1_base = jao_line['bus1_base']
        jao_v = int(jao_line['v_nom_mapped'])
        jao_length = jao_line['length'] if pd.notna(jao_line['length']) else 0

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
        if c0 not in G or c1 not in G:
            stats['cluster_not_in_graph'] += 1
            continue

        try:
            path = nx.shortest_path(G, c0, c1, weight='weight')
        except nx.NetworkXNoPath:
            stats['no_path'] += 1
            continue

        # Compute path length for validation
        total_path_length = 0
        path_corridors = []
        for i in range(len(path) - 1):
            ca, cb = path[i], path[i + 1]
            corridor_key = (min(ca, cb), max(ca, cb), jao_v)
            seg_lines = corridor_lines.get(corridor_key, [])
            if seg_lines:
                lid = seg_lines[0]
                if lid in line_data.index:
                    total_path_length += line_data.loc[lid, 'length']
            path_corridors.append(corridor_key)

        if not path_corridors:
            stats['no_segments'] += 1
            continue

        # Validate path length
        if jao_length > 0 and total_path_length > 0:
            ratio = total_path_length / jao_length
            if ratio < PATH_LENGTH_MIN or ratio > PATH_LENGTH_MAX:
                stats['length_mismatch'] += 1
                continue

        n_hops = len(path) - 1
        info = {
            'jao_idx': jao_idx,
            'jao_name': jao_line.get('NE_name', ''),
            'jao_eic': jao_line.get('EIC_Code', ''),
            'path_hops': n_hops,
            'bus0_base': b0_base,
            'bus1_base': b1_base,
            'cluster_0': c0,
            'cluster_1': c1,
            'jao_length': jao_length,
            'jao_v_nom': jao_v,
            'path': path,
            'path_corridors': path_corridors,
        }
        jao_details.append(info)

        if n_hops == 1:
            ck = path_corridors[0]
            single_hop[ck].append(info)
            stats['single_hop'] += 1
        else:
            for ck in path_corridors:
                multi_hop[ck].append(info)
            stats['multi_hop'] += 1

    print(f"\n  JAO line routing stats:")
    print(f"    Single-hop (direct corridor): {stats['single_hop']}")
    print(f"    Multi-hop (routed through intermediate clusters): {stats['multi_hop']}")
    print(f"    Endpoint unmatched: {stats['endpoint_unmatched']}")
    print(f"    Same cluster: {stats['same_cluster']}")
    print(f"    No path: {stats['no_path']}")
    print(f"    Length mismatch: {stats['length_mismatch']}")

    return single_hop, multi_hop, jao_details


def analyze_circuits(corridor_lines, single_hop, multi_hop, egon_hv_lines,
                     bus_vnom, cluster_info, cables_lookup):
    """
    For each corridor, compute:
      - eGon circuit count (from cables column)
      - JAO single-hop circuit count (direct matches only)
      - JAO multi-hop traversal count (additional lines routed through)
    """
    line_data = egon_hv_lines.set_index('line_id')
    results = []

    all_corridors = set(corridor_lines.keys())
    # Also add corridors that only appear in JAO
    all_corridors |= set(single_hop.keys()) | set(multi_hop.keys())

    for corridor_key in sorted(all_corridors):
        c0, c1, voltage = corridor_key

        # --- eGon side ---
        egon_lids = corridor_lines.get(corridor_key, [])
        n_egon_segments = len(egon_lids)
        total_length = 0
        cables_list = []
        s_nom_list = []

        for lid in egon_lids:
            if lid in line_data.index:
                row = line_data.loc[lid]
                c = cables_lookup.get(lid, 3)
                if pd.isna(c):
                    c = 3
                cables_list.append(int(c))
                total_length += row['length']
                s_nom_list.append(row['s_nom'])

        # eGon circuit count: max cables / 3
        # cables=3 => 1 circuit, cables=6 => 2 circuits, etc.
        if cables_list:
            max_cables = max(cables_list)
            egon_circuits = max_cables // 3
        else:
            max_cables = 0
            egon_circuits = 0

        # --- JAO single-hop side ---
        sh_entries = single_hop.get(corridor_key, [])
        # Deduplicate by jao_idx
        seen = set()
        unique_sh = []
        for e in sh_entries:
            if e['jao_idx'] not in seen:
                seen.add(e['jao_idx'])
                unique_sh.append(e)
        n_jao_single = len(unique_sh)

        # --- JAO multi-hop side ---
        mh_entries = multi_hop.get(corridor_key, [])
        seen_mh = set()
        unique_mh = []
        for e in mh_entries:
            if e['jao_idx'] not in seen_mh:
                seen_mh.add(e['jao_idx'])
                unique_mh.append(e)
        n_jao_multi = len(unique_mh)

        # Combined unique JAO lines that traverse this corridor
        all_jao_idxs = set()
        for e in unique_sh + unique_mh:
            all_jao_idxs.add(e['jao_idx'])
        n_jao_total = len(all_jao_idxs)

        # Names
        name0 = cluster_info.get(c0, {}).get('name', '') or c0
        name1 = cluster_info.get(c1, {}).get('name', '') or c1

        results.append({
            'cluster_0': c0,
            'cluster_1': c1,
            'name_0': name0,
            'name_1': name1,
            'voltage_kv': voltage,
            'egon_segments': n_egon_segments,
            'egon_max_cables': max_cables,
            'egon_circuits': egon_circuits,
            'jao_single_hop': n_jao_single,
            'jao_multi_hop': n_jao_multi,
            'jao_total_traversals': n_jao_total,
            'diff_vs_single': egon_circuits - n_jao_single if n_jao_single > 0 else None,
            'diff_vs_total': egon_circuits - n_jao_total if n_jao_total > 0 else None,
            'egon_length_km': round(total_length, 1),
            'cables_detail': str(cables_list),
            'sh_names': '; '.join([e['jao_name'] for e in unique_sh]),
            'mh_names': '; '.join([e['jao_name'] for e in unique_mh]),
        })

    return pd.DataFrame(results)


def print_comparison_tables(df, cluster_info):
    """Print detailed comparison tables."""

    pd.set_option('display.max_colwidth', 60)
    pd.set_option('display.width', 220)
    pd.set_option('display.max_rows', 60)

    # Focus on corridors where JAO has data
    has_jao = df[df['jao_total_traversals'] > 0].copy()
    has_single = df[df['jao_single_hop'] > 0].copy()

    print(f"\n{'='*80}")
    print("SECTION 1: SINGLE-HOP COMPARISON (most reliable)")
    print("(Only JAO lines whose endpoints map to adjacent eGon clusters)")
    print(f"{'='*80}")

    for voltage in [220, 380]:
        vdf = has_single[has_single['voltage_kv'] == voltage].copy()
        print(f"\n{'='*80}")
        print(f"  {voltage} kV  --  {len(vdf)} corridors with single-hop JAO lines")
        print(f"{'='*80}")

        if len(vdf) == 0:
            continue

        # Classification
        vdf['status'] = 'MATCH'
        vdf.loc[vdf['diff_vs_single'] > 0, 'status'] = 'eGon_MORE'
        vdf.loc[vdf['diff_vs_single'] < 0, 'status'] = 'eGon_FEWER'

        status_counts = vdf['status'].value_counts()
        print(f"\n  Circuit count agreement (eGon vs JAO single-hop):")
        print(f"    MATCH:      {status_counts.get('MATCH', 0)}")
        print(f"    eGon MORE:  {status_counts.get('eGon_MORE', 0)}")
        print(f"    eGon FEWER: {status_counts.get('eGon_FEWER', 0)}")

        # Distribution of differences
        print(f"\n  Difference distribution (eGon_circuits - JAO_single_hop):")
        diff_dist = vdf['diff_vs_single'].value_counts().sort_index()
        for d, cnt in diff_dist.items():
            label = f"+{int(d)}" if d > 0 else str(int(d))
            bar = '#' * cnt
            print(f"    {label:>4s}: {cnt:4d}  {bar}")

        # Cross-tabulation
        print(f"\n  Cross-tabulation (rows=eGon_circuits, cols=JAO_single_hop):")
        ct = pd.crosstab(
            vdf['egon_circuits'].clip(upper=5),
            vdf['jao_single_hop'].clip(upper=5),
            margins=True
        )
        ct.index.name = 'eGon\\JAO'
        print(ct.to_string())

        # Show discrepancies
        disc = vdf[vdf['diff_vs_single'] != 0].copy()
        if len(disc) > 0:
            fewer = disc[disc['diff_vs_single'] < 0].sort_values('diff_vs_single')
            if len(fewer) > 0:
                print(f"\n  --- eGon has FEWER circuits (top 25) ---")
                show_cols = ['name_0', 'name_1', 'egon_circuits', 'jao_single_hop',
                             'diff_vs_single', 'egon_max_cables', 'egon_length_km',
                             'sh_names']
                print(fewer[show_cols].head(25).to_string(index=False))

            more = disc[disc['diff_vs_single'] > 0].sort_values('diff_vs_single', ascending=False)
            if len(more) > 0:
                print(f"\n  --- eGon has MORE circuits (top 25) ---")
                show_cols = ['name_0', 'name_1', 'egon_circuits', 'jao_single_hop',
                             'diff_vs_single', 'egon_max_cables', 'egon_length_km',
                             'sh_names']
                print(more[show_cols].head(25).to_string(index=False))

    # =========================================================================
    print(f"\n{'='*80}")
    print("SECTION 2: TOTAL TRAVERSAL COMPARISON (single-hop + multi-hop)")
    print("(All JAO lines routed through each corridor, including multi-hop)")
    print(f"{'='*80}")

    for voltage in [220, 380]:
        vdf = has_jao[has_jao['voltage_kv'] == voltage].copy()
        print(f"\n  {voltage} kV -- {len(vdf)} corridors with any JAO traversal")

        if len(vdf) == 0:
            continue

        vdf['status'] = 'MATCH'
        vdf.loc[vdf['diff_vs_total'] > 0, 'status'] = 'eGon_MORE'
        vdf.loc[vdf['diff_vs_total'] < 0, 'status'] = 'eGon_FEWER'

        status_counts = vdf['status'].value_counts()
        print(f"    MATCH:      {status_counts.get('MATCH', 0)}")
        print(f"    eGon MORE:  {status_counts.get('eGon_MORE', 0)}")
        print(f"    eGon FEWER: {status_counts.get('eGon_FEWER', 0)}")

        # Cross-tabulation
        ct = pd.crosstab(
            vdf['egon_circuits'].clip(upper=5),
            vdf['jao_total_traversals'].clip(upper=8),
            margins=True
        )
        ct.index.name = 'eGon\\JAO_total'
        print(f"\n    Cross-tabulation (rows=eGon_circuits, cols=JAO_total_traversals):")
        print(ct.to_string())

    # =========================================================================
    print(f"\n{'='*80}")
    print("SECTION 3: CABLES COLUMN ANALYSIS")
    print(f"{'='*80}")

    for voltage in [220, 380]:
        vdf = df[df['voltage_kv'] == voltage].copy()
        if len(vdf) == 0:
            continue

        all_cables = vdf[vdf['egon_segments'] > 0]
        print(f"\n  {voltage}kV -- all eGon corridors ({len(all_cables)}):")
        cables_dist = all_cables['egon_max_cables'].value_counts().sort_index()
        for c, n in cables_dist.items():
            circuits = c // 3 if c > 0 else 0
            print(f"    cables={c:2d} ({circuits} circuit{'s' if circuits>1 else ''}): {n:4d} corridors")

        # Compared to JAO
        jao_matched = has_jao[has_jao['voltage_kv'] == voltage]
        if len(jao_matched) > 0:
            print(f"\n  {voltage}kV -- JAO-matched corridors only ({len(jao_matched)}):")
            cables_dist2 = jao_matched['egon_max_cables'].value_counts().sort_index()
            for c, n in cables_dist2.items():
                circuits = c // 3 if c > 0 else 0
                # How many of these have JAO>1 circuit?
                subset = jao_matched[jao_matched['egon_max_cables'] == c]
                jao_multi = (subset['jao_total_traversals'] > 1).sum()
                print(f"    cables={c:2d} ({circuits} ckt): {n:4d} corridors "
                      f"(JAO says >1 circuit: {jao_multi})")

    # =========================================================================
    print(f"\n{'='*80}")
    print("SECTION 4: SYSTEMATIC PATTERN ANALYSIS")
    print(f"{'='*80}")

    # For corridors where JAO says 2 circuits but eGon says 1:
    # Is this always cables=3? Are these real double-circuit lines represented
    # as single lines in OSM?
    for voltage in [220, 380]:
        vdf = has_single[has_single['voltage_kv'] == voltage].copy()

        # JAO=2, eGon=1 corridors
        j2_e1 = vdf[(vdf['jao_single_hop'] == 2) & (vdf['egon_circuits'] == 1)]
        if len(j2_e1) > 0:
            print(f"\n  {voltage}kV: JAO says 2 circuits, eGon says 1:")
            print(f"    Count: {len(j2_e1)} corridors")
            cables_here = j2_e1['egon_max_cables'].value_counts().sort_index()
            for c, n in cables_here.items():
                print(f"    cables={c}: {n}")
            # Show a few examples
            sample = j2_e1.head(5)
            for _, row in sample.iterrows():
                print(f"      {row['name_0']} -- {row['name_1']}: "
                      f"cables={row['egon_max_cables']}, "
                      f"len={row['egon_length_km']}km, "
                      f"JAO: {row['sh_names']}")

        # JAO=1, eGon=2 corridors (cables=6)
        j1_e2 = vdf[(vdf['jao_single_hop'] == 1) & (vdf['egon_circuits'] == 2)]
        if len(j1_e2) > 0:
            print(f"\n  {voltage}kV: JAO says 1 circuit, eGon says 2 (cables=6):")
            print(f"    Count: {len(j1_e2)} corridors")
            for _, row in j1_e2.iterrows():
                print(f"      {row['name_0']} -- {row['name_1']}: "
                      f"cables={row['egon_max_cables']}, "
                      f"len={row['egon_length_km']}km, "
                      f"JAO: {row['sh_names']}")


def main():
    print("=" * 80)
    print("PARALLEL CIRCUIT COMPARISON: eGon2025 vs JAO/CORE-TSO")
    print("=" * 80)

    engine = create_engine(DB_URI)

    # Step 1: Load data
    data = load_data(engine, SCENARIO)

    # Step 2: Cluster buses
    bus_to_cluster, cluster_info = cluster_buses(
        data['egon_hv'], data['ehv_subs'],
        data['egon_hv_lines'], data['bus_vnom']
    )

    # Step 3: Match JAO buses to eGon clusters
    bus_matches = match_buses(
        data['jao_buses'], cluster_info, bus_to_cluster, data['egon_hv']
    )

    # Step 4: Build cluster graph
    graphs, corridor_lines = build_cluster_graph(
        data['egon_hv_lines'], bus_to_cluster, cluster_info, data['bus_vnom']
    )

    # Step 5: Map JAO lines to corridors
    single_hop, multi_hop, jao_details = map_jao_lines_to_corridors(
        data['jao_lines'], bus_matches, graphs, corridor_lines,
        cluster_info, data['egon_hv_lines'], data['bus_vnom']
    )

    # Load cables column (not included in load_data)
    cables_lookup = load_cables_column(engine, SCENARIO)

    # Step 6: Analyze circuit counts
    df = analyze_circuits(
        corridor_lines, single_hop, multi_hop, data['egon_hv_lines'],
        data['bus_vnom'], cluster_info, cables_lookup
    )

    # Step 7: Print detailed comparison tables
    print_comparison_tables(df, cluster_info)

    # =========================================================================
    # OVERALL SUMMARY
    # =========================================================================
    print(f"\n{'='*80}")
    print("OVERALL SUMMARY")
    print(f"{'='*80}")

    has_single = df[df['jao_single_hop'] > 0]
    has_jao = df[df['jao_total_traversals'] > 0]

    print(f"\n  Total eGon corridors (220/380kV): {len(df[df['egon_segments'] > 0])}")
    print(f"  Corridors with JAO single-hop matches: {len(has_single)}")
    print(f"  Corridors with any JAO traversal: {len(has_jao)}")

    print(f"\n  --- Single-hop comparison (most reliable) ---")
    for voltage in [220, 380]:
        vdf = has_single[has_single['voltage_kv'] == voltage]
        if len(vdf) == 0:
            continue
        agree = (vdf['diff_vs_single'] == 0).sum()
        more = (vdf['diff_vs_single'] > 0).sum()
        fewer = (vdf['diff_vs_single'] < 0).sum()
        print(f"    {voltage}kV: {len(vdf)} corridors, "
              f"agree={agree} ({agree/len(vdf)*100:.1f}%), "
              f"eGon_more={more} ({more/len(vdf)*100:.1f}%), "
              f"eGon_fewer={fewer} ({fewer/len(vdf)*100:.1f}%)")

    print(f"\n  --- Total traversal comparison ---")
    for voltage in [220, 380]:
        vdf = has_jao[has_jao['voltage_kv'] == voltage]
        if len(vdf) == 0:
            continue
        agree = (vdf['diff_vs_total'] == 0).sum()
        more = (vdf['diff_vs_total'] > 0).sum()
        fewer = (vdf['diff_vs_total'] < 0).sum()
        print(f"    {voltage}kV: {len(vdf)} corridors, "
              f"agree={agree} ({agree/len(vdf)*100:.1f}%), "
              f"eGon_more={more} ({more/len(vdf)*100:.1f}%), "
              f"eGon_fewer={fewer} ({fewer/len(vdf)*100:.1f}%)")

    # Multi-hop analysis
    mh_jao = [d for d in jao_details if d['path_hops'] > 1]
    sh_jao = [d for d in jao_details if d['path_hops'] == 1]
    print(f"\n  JAO line routing breakdown:")
    print(f"    Total matched JAO lines: {len(jao_details)}")
    print(f"    Single-hop (adjacent clusters): {len(sh_jao)} ({len(sh_jao)/len(jao_details)*100:.1f}%)")
    print(f"    Multi-hop (routed through intermediates): {len(mh_jao)} ({len(mh_jao)/len(jao_details)*100:.1f}%)")

    # Hop count distribution
    hop_dist = defaultdict(int)
    for d in jao_details:
        hop_dist[d['path_hops']] += 1
    print(f"\n  Hop count distribution:")
    for h in sorted(hop_dist):
        print(f"    {h} hops: {hop_dist[h]} JAO lines")

    # Save results
    os.makedirs('results', exist_ok=True)
    output_path = os.path.join('results', 'parallel_circuit_comparison.csv')
    df.to_csv(output_path, index=False)
    print(f"\n  Full results saved to: {output_path}")

    disc_path = os.path.join('results', 'parallel_circuit_discrepancies.csv')
    disc = has_single[has_single['diff_vs_single'] != 0].sort_values(
        ['voltage_kv', 'diff_vs_single'])
    disc.to_csv(disc_path, index=False)
    print(f"  Single-hop discrepancies saved to: {disc_path} ({len(disc)} rows)")


if __name__ == '__main__':
    main()
