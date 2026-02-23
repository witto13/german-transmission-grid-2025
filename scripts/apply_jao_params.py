#!/usr/bin/env python3
"""
Apply JAO Parameters and Fix eGon2025 Grid
===========================================

Single script to fix all known data quality issues in the eGon2025 scenario:

  Phase A: Fix cross-border bus country labels (63 buses mislabeled as DE)
  Phase B: Add HVDC links (ALEGRO, NordLink, Baltic Cable)
  Phase C: JAO line parameter transfer (~1000 line segments)
  Phase D: JAO transformer parameter transfer (~72 autotransformers)
  Phase E: Default parameters for unmatched transformers (~463)
  Phase F: Scenario cleanup (delete v7/v8, optional)
  Phase G: Reporting

Usage:
    python scripts/apply_jao_params.py --dry-run                     # preview
    python scripts/apply_jao_params.py --apply                       # write to DB
    python scripts/apply_jao_params.py --apply --cleanup-scenarios   # also delete v7/v8
"""

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
SCN = 'eGon2025'
SCN_V6 = 'eGon2025v6'
RESULTS_DIR = 'results/jao_params'

# HVDC link definitions
HVDC_LINKS = [
    {
        'name': 'ALEGRO',
        'p_nom': 1000.0,   # MW
        'de_bus': 35187,    # Oberzier 380kV
        'foreign_bus_id': 200001,
        'foreign_country': 'BE',
        'foreign_x': 6.0825,
        'foreign_y': 50.7375,
        'foreign_v_nom': 380,
        'length': 90.0,     # km
    },
    {
        'name': 'NordLink',
        'p_nom': 1400.0,   # MW
        'de_bus': 38906,    # Wilster 380kV
        'foreign_bus_id': 200002,
        'foreign_country': 'NO',
        'foreign_x': 6.567,
        'foreign_y': 58.362,
        'foreign_v_nom': 380,
        'length': 623.0,    # km
    },
    {
        'name': 'Baltic Cable',
        'p_nom': 600.0,    # MW
        'de_bus': 36614,    # Herrenwyk/Lübeck 380kV
        'foreign_bus_id': 200003,
        'foreign_country': 'SE',
        'foreign_x': 13.157,
        'foreign_y': 55.377,
        'foreign_v_nom': 380,
        'length': 262.0,    # km
    },
]

# Transformer default parameters by voltage pair
TRAFO_DEFAULTS = {
    (220, 380): {'x': 0.04, 'r': 0.0005},   # Autotransformer median from JAO
    (110, 220): {'x': 0.12, 'r': 0.003},     # Full-winding HV transformer
    (110, 380): {'x': 0.12, 'r': 0.003},     # Full-winding HV transformer
}

# Scenarios to clean up (only failed v7/v8 experiments)
CLEANUP_SCENARIOS = [
    'eGon2025v7', 'eGon2025v8',
]

# All grid tables with scn_name column
GRID_TABLES = [
    'egon_etrago_bus', 'egon_etrago_bus_timeseries',
    'egon_etrago_generator', 'egon_etrago_generator_timeseries',
    'egon_etrago_line', 'egon_etrago_line_timeseries',
    'egon_etrago_link', 'egon_etrago_link_timeseries',
    'egon_etrago_load', 'egon_etrago_load_timeseries',
    'egon_etrago_storage', 'egon_etrago_storage_timeseries',
    'egon_etrago_store', 'egon_etrago_store_timeseries',
    'egon_etrago_transformer', 'egon_etrago_transformer_timeseries',
    'egon_etrago_hv_busmap',
]


def _native_params(params):
    """Convert numpy types to native Python for psycopg2 compatibility."""
    return {k: float(v) if hasattr(v, 'item') else v for k, v in params.items()}


def phase_a_fix_country_labels(engine, dry_run):
    """Phase A: Fix cross-border bus country labels."""
    print("\n" + "=" * 70)
    print("PHASE A: Fix cross-border bus country labels")
    print("=" * 70)

    # Get buses that are non-DE in v6
    v6_foreign = pd.read_sql(text(
        "SELECT bus_id, country FROM grid.egon_etrago_bus "
        "WHERE scn_name = :v6 AND (country <> 'DE' OR country IS NULL)"
    ), engine, params={'v6': SCN_V6})

    if len(v6_foreign) == 0:
        print("  No cross-border buses found in v6 — skipping")
        return 0

    # Check which of these exist in eGon2025
    bus_ids = tuple(int(b) for b in v6_foreign['bus_id'].tolist())
    egon_current = pd.read_sql(text(
        "SELECT bus_id, country FROM grid.egon_etrago_bus "
        f"WHERE scn_name = :scn AND bus_id IN :bus_ids"
    ), engine, params={'scn': SCN, 'bus_ids': bus_ids})

    # Build update map: bus_id -> correct country
    country_map = dict(zip(v6_foreign['bus_id'], v6_foreign['country']))
    to_fix = []
    for _, row in egon_current.iterrows():
        correct = country_map.get(row['bus_id'])
        if correct != row['country']:
            to_fix.append((row['bus_id'], row['country'], correct))

    print(f"  v6 foreign buses: {len(v6_foreign)}")
    print(f"  Exist in {SCN}: {len(egon_current)}")
    print(f"  Need country fix: {len(to_fix)}")

    # Show breakdown
    fix_df = pd.DataFrame(to_fix, columns=['bus_id', 'current', 'correct'])
    if len(fix_df) > 0:
        for country, count in fix_df['correct'].value_counts().items():
            label = country if country else 'NULL'
            print(f"    {label}: {count}")

    if dry_run or len(to_fix) == 0:
        return len(to_fix)

    # Apply updates
    with engine.begin() as conn:
        for bus_id, _, correct in to_fix:
            if correct is None or (isinstance(correct, float) and math.isnan(correct)):
                conn.execute(text(
                    "UPDATE grid.egon_etrago_bus SET country = NULL "
                    "WHERE scn_name = :scn AND bus_id = :bid"
                ), {'scn': SCN, 'bid': int(bus_id)})
            else:
                conn.execute(text(
                    "UPDATE grid.egon_etrago_bus SET country = :country "
                    "WHERE scn_name = :scn AND bus_id = :bid"
                ), {'scn': SCN, 'bid': int(bus_id), 'country': correct})

    print(f"  Updated {len(to_fix)} bus country labels")
    return len(to_fix)


def phase_b_add_hvdc_links(engine, dry_run):
    """Phase B: Add HVDC links (ALEGRO, NordLink, Baltic Cable)."""
    print("\n" + "=" * 70)
    print("PHASE B: Add HVDC links")
    print("=" * 70)

    # Check which links already exist (by checking foreign bus IDs)
    existing_buses = pd.read_sql(text(
        "SELECT bus_id FROM grid.egon_etrago_bus "
        "WHERE scn_name = :scn AND bus_id >= 200000"
    ), engine, params={'scn': SCN})
    existing_bus_ids = set(existing_buses['bus_id'].tolist())

    # Get max link_id
    max_link_id = pd.read_sql(text(
        "SELECT COALESCE(MAX(link_id), 0) as max_id FROM grid.egon_etrago_link "
        "WHERE scn_name = :scn"
    ), engine, params={'scn': SCN}).iloc[0]['max_id']

    links_to_add = []
    buses_to_add = []
    link_id = int(max_link_id) + 1

    for hvdc in HVDC_LINKS:
        if hvdc['foreign_bus_id'] in existing_bus_ids:
            print(f"  {hvdc['name']}: foreign bus {hvdc['foreign_bus_id']} "
                  f"already exists — skipping")
            continue

        buses_to_add.append({
            'scn_name': SCN,
            'bus_id': hvdc['foreign_bus_id'],
            'v_nom': hvdc['foreign_v_nom'],
            'x': hvdc['foreign_x'],
            'y': hvdc['foreign_y'],
            'country': hvdc['foreign_country'],
            'carrier': 'AC',
        })

        links_to_add.append({
            'scn_name': SCN,
            'link_id': link_id,
            'bus0': hvdc['de_bus'],
            'bus1': hvdc['foreign_bus_id'],
            'carrier': 'DC',
            'efficiency': 0.98,
            'p_nom': hvdc['p_nom'],
            'p_nom_extendable': False,
            'p_nom_min': 0,
            'p_nom_max': hvdc['p_nom'],
            'p_min_pu': -1.0,  # Bidirectional
            'p_max_pu': 1.0,
            'length': hvdc['length'],
            'marginal_cost': 0.0,
            'capital_cost': 0.0,
            'terrain_factor': 1.0,
        })

        print(f"  {hvdc['name']}: {hvdc['p_nom']} MW, "
              f"bus {hvdc['de_bus']} <-> {hvdc['foreign_bus_id']} ({hvdc['foreign_country']})")
        link_id += 1

    print(f"  New buses: {len(buses_to_add)}, New links: {len(links_to_add)}")

    if dry_run or not links_to_add:
        return len(links_to_add)

    # Insert buses then links
    buses_df = pd.DataFrame(buses_to_add)
    links_df = pd.DataFrame(links_to_add)

    buses_df.to_sql('egon_etrago_bus', engine, schema='grid',
                     if_exists='append', index=False)
    links_df.to_sql('egon_etrago_link', engine, schema='grid',
                     if_exists='append', index=False)

    print(f"  Inserted {len(buses_to_add)} buses and {len(links_to_add)} links")
    return len(links_to_add)


def phase_c_jao_line_params(engine, dry_run):
    """Phase C: Transfer JAO line parameters via jao_matching.py."""
    print("\n" + "=" * 70)
    print("PHASE C: JAO line parameter transfer")
    print("=" * 70)

    # Import matching functions from jao_matching.py
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import jao_matching

    data = jao_matching.load_data(engine, SCN)
    bus_to_cluster, cluster_info = jao_matching.cluster_buses(
        data['egon_hv'], data['ehv_subs'],
        data['egon_hv_lines'], data['bus_vnom']
    )
    bus_matches = jao_matching.match_buses(
        data['jao_buses'], cluster_info, bus_to_cluster, data['egon_hv']
    )
    graphs, corridor_lines = jao_matching.build_cluster_graph(
        data['egon_hv_lines'], bus_to_cluster, cluster_info, data['bus_vnom']
    )
    line_matches = jao_matching.match_lines(
        data['jao_lines'], bus_matches, graphs, corridor_lines,
        cluster_info, data['egon_hv_lines'], data['bus_vnom']
    )
    assignments = jao_matching.assign_parallel_circuits(
        line_matches, corridor_lines, data['egon_hv_lines']
    )
    line_updates, eic_map = jao_matching.compute_line_updates(
        assignments, data['egon_hv_lines']
    )

    print(f"\n  Total line updates: {len(line_updates)}")

    if not dry_run and line_updates:
        with engine.begin() as conn:
            for lid, params in line_updates.items():
                native = _native_params(params)
                set_clauses = ', '.join(f"{k} = :{k}" for k in native.keys())
                sql = text(
                    f"UPDATE grid.egon_etrago_line "
                    f"SET {set_clauses} "
                    f"WHERE scn_name = :scn AND line_id = :lid"
                )
                conn.execute(sql, {**native, 'scn': SCN, 'lid': int(lid)})
        print(f"  Applied {len(line_updates)} line updates to DB")

    return line_updates, bus_matches, cluster_info, bus_to_cluster, data, eic_map


def phase_d_jao_trafo_params(engine, dry_run, bus_matches, cluster_info,
                              bus_to_cluster, data):
    """Phase D: Transfer JAO transformer parameters."""
    print("\n" + "=" * 70)
    print("PHASE D: JAO transformer parameter transfer")
    print("=" * 70)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import jao_matching

    trafo_updates, trafo_eic_map = jao_matching.match_and_compute_trafo_updates(
        data['jao_trafos'], bus_matches, cluster_info,
        bus_to_cluster, data['egon_hv_trafos'], data['bus_vnom']
    )

    print(f"\n  Total transformer updates from JAO: {len(trafo_updates)}")

    if not dry_run and trafo_updates:
        with engine.begin() as conn:
            for tid, params in trafo_updates.items():
                native = _native_params(params)
                set_clauses = ', '.join(f"{k} = :{k}" for k in native.keys())
                sql = text(
                    f"UPDATE grid.egon_etrago_transformer "
                    f"SET {set_clauses} "
                    f"WHERE scn_name = :scn AND trafo_id = :tid"
                )
                conn.execute(sql, {**native, 'scn': SCN, 'tid': int(tid)})
        print(f"  Applied {len(trafo_updates)} transformer updates to DB")

    return trafo_updates, trafo_eic_map


def phase_e_trafo_defaults(engine, dry_run, jao_trafo_ids):
    """Phase E: Set default parameters for unmatched transformers."""
    print("\n" + "=" * 70)
    print("PHASE E: Default parameters for unmatched transformers")
    print("=" * 70)

    # Load all transformers with their bus voltages
    trafos = pd.read_sql(text("""
        SELECT t.trafo_id, t.bus0, t.bus1, t.r, t.x, t.s_nom,
               b0.v_nom as v0, b1.v_nom as v1
        FROM grid.egon_etrago_transformer t
        JOIN grid.egon_etrago_bus b0
            ON t.bus0 = b0.bus_id AND b0.scn_name = :scn
        JOIN grid.egon_etrago_bus b1
            ON t.bus1 = b1.bus_id AND b1.scn_name = :scn
        WHERE t.scn_name = :scn
    """), engine, params={'scn': SCN})

    # Find transformers still with x=0 after JAO matching
    # (exclude those already updated by JAO in phase D)
    needs_default = trafos[
        (trafos['x'] == 0) &
        (trafos['s_nom'] > 0) &
        (~trafos['trafo_id'].isin(jao_trafo_ids))
    ].copy()

    # Classify by voltage pair
    needs_default['v_low'] = needs_default[['v0', 'v1']].min(axis=1).astype(int)
    needs_default['v_high'] = needs_default[['v0', 'v1']].max(axis=1).astype(int)
    needs_default['v_pair'] = list(zip(needs_default['v_low'], needs_default['v_high']))

    default_updates = {}
    for v_pair, group in needs_default.groupby('v_pair'):
        defaults = TRAFO_DEFAULTS.get(v_pair)
        if defaults is None:
            print(f"  WARNING: No defaults for voltage pair {v_pair} "
                  f"({len(group)} transformers)")
            continue

        print(f"  {v_pair[0]}/{v_pair[1]}kV: {len(group)} transformers "
              f"-> x={defaults['x']}, r={defaults['r']}")

        for _, row in group.iterrows():
            tid = row['trafo_id']
            update = {}
            if row['x'] == 0:
                update['x'] = defaults['x']
            if row['r'] == 0:
                update['r'] = defaults['r']
            if update:
                default_updates[tid] = update

    # In live mode, check if JAO-matched trafos still have x=0 (shouldn't happen)
    if not dry_run:
        still_zero = trafos[
            (trafos['x'] == 0) &
            (trafos['s_nom'] > 0) &
            (trafos['trafo_id'].isin(jao_trafo_ids))
        ]
        if len(still_zero) > 0:
            print(f"  WARNING: {len(still_zero)} JAO-matched trafos still have x=0")

    print(f"\n  Total default updates: {len(default_updates)}")

    if not dry_run and default_updates:
        with engine.begin() as conn:
            for tid, params in default_updates.items():
                native = _native_params(params)
                set_clauses = ', '.join(f"{k} = :{k}" for k in native.keys())
                sql = text(
                    f"UPDATE grid.egon_etrago_transformer "
                    f"SET {set_clauses} "
                    f"WHERE scn_name = :scn AND trafo_id = :tid"
                )
                conn.execute(sql, {**native, 'scn': SCN, 'tid': int(tid)})
        print(f"  Applied {len(default_updates)} default transformer updates to DB")

    return default_updates


def phase_f_cleanup_scenarios(engine, dry_run):
    """Phase F: Delete old scenarios (v7, v8, etc.)."""
    print("\n" + "=" * 70)
    print("PHASE F: Scenario cleanup")
    print("=" * 70)

    # Check what exists
    for scn in CLEANUP_SCENARIOS:
        count = pd.read_sql(text(
            "SELECT COUNT(*) as n FROM grid.egon_etrago_bus WHERE scn_name = :scn"
        ), engine, params={'scn': scn}).iloc[0]['n']
        if count > 0:
            print(f"  {scn}: {count} buses")

    if dry_run:
        print("  (dry run — no deletions)")
        return

    total_deleted = 0
    with engine.begin() as conn:
        for scn in CLEANUP_SCENARIOS:
            scn_total = 0
            for table in GRID_TABLES:
                result = conn.execute(text(
                    f"DELETE FROM grid.{table} WHERE scn_name = :scn"
                ), {'scn': scn})
                scn_total += result.rowcount
            if scn_total > 0:
                print(f"  Deleted {scn_total} rows from {scn}")
                total_deleted += scn_total

    print(f"  Total rows deleted: {total_deleted}")


def phase_g_reporting(country_fixes, hvdc_count, line_updates, trafo_updates,
                      default_updates, eic_map, trafo_eic_map, output_dir):
    """Phase G: Generate summary reports."""
    print("\n" + "=" * 70)
    print("PHASE G: Reporting")
    print("=" * 70)

    os.makedirs(output_dir, exist_ok=True)

    # Line updates CSV
    if line_updates:
        rows = [{'line_id': lid, **params} for lid, params in line_updates.items()]
        df = pd.DataFrame(rows)
        path = os.path.join(output_dir, 'line_updates.csv')
        df.to_csv(path, index=False)
        print(f"  Line updates: {path} ({len(df)} rows)")

    # Transformer updates CSV (JAO + defaults combined)
    all_trafo = {}
    if trafo_updates:
        for tid, params in trafo_updates.items():
            all_trafo[tid] = {**params, 'source': 'jao'}
    if default_updates:
        for tid, params in default_updates.items():
            all_trafo[tid] = {**params, 'source': 'default'}

    if all_trafo:
        rows = [{'trafo_id': tid, **params} for tid, params in all_trafo.items()]
        df = pd.DataFrame(rows)
        path = os.path.join(output_dir, 'trafo_updates.csv')
        df.to_csv(path, index=False)
        print(f"  Transformer updates: {path} ({len(df)} rows)")

    # EIC mapping
    all_eic = []
    if eic_map:
        all_eic.extend(eic_map)
    if trafo_eic_map:
        all_eic.extend(trafo_eic_map)
    if all_eic:
        df = pd.DataFrame(all_eic)
        path = os.path.join(output_dir, 'eic_mapping.csv')
        df.to_csv(path, index=False)
        print(f"  EIC mapping: {path} ({len(df)} rows)")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Phase A — Country label fixes:     {country_fixes}")
    print(f"  Phase B — HVDC links added:         {hvdc_count}")
    print(f"  Phase C — JAO line updates:         {len(line_updates)}")
    print(f"  Phase D — JAO transformer updates:  {len(trafo_updates)}")
    print(f"  Phase E — Default trafo updates:    {len(default_updates)}")
    total_trafo = len(trafo_updates) + len(default_updates)
    print(f"  Total transformer updates:          {total_trafo}/535")

    if line_updates:
        r_vals = [u['r'] for u in line_updates.values() if 'r' in u]
        x_vals = [u['x'] for u in line_updates.values() if 'x' in u]
        s_vals = [u['s_nom'] for u in line_updates.values() if 's_nom' in u]
        if r_vals:
            print(f"\n  Line r: median={np.median(r_vals):.4f}, "
                  f"range=[{min(r_vals):.4f}, {max(r_vals):.4f}]")
        if x_vals:
            print(f"  Line x: median={np.median(x_vals):.4f}, "
                  f"range=[{min(x_vals):.4f}, {max(x_vals):.4f}]")
        if s_vals:
            print(f"  Line s_nom: median={np.median(s_vals):.0f}, "
                  f"range=[{min(s_vals):.0f}, {max(s_vals):.0f}]")


def main():
    parser = argparse.ArgumentParser(
        description='Apply JAO parameters and fix eGon2025 grid')
    parser.add_argument('--dry-run', action='store_true', default=True,
                        help='Preview changes without writing (default)')
    parser.add_argument('--apply', action='store_true',
                        help='Write changes to database')
    parser.add_argument('--cleanup-scenarios', action='store_true',
                        help='Also delete v7/v8 and other old scenarios')
    parser.add_argument('--output-dir', default=RESULTS_DIR,
                        help='Output directory for reports')
    args = parser.parse_args()

    dry_run = not args.apply
    results_dir = args.output_dir

    print("=" * 70)
    print(f"Apply JAO Parameters & Fix eGon2025 Grid")
    print(f"Mode: {'DRY RUN (preview only)' if dry_run else 'LIVE — writing to database'}")
    print("=" * 70)

    engine = create_engine(DB_URI)

    # Phase A: Fix country labels
    country_fixes = phase_a_fix_country_labels(engine, dry_run)

    # Phase B: Add HVDC links
    hvdc_count = phase_b_add_hvdc_links(engine, dry_run)

    # Phase C: JAO line parameters (returns data needed by Phase D)
    line_updates, bus_matches, cluster_info, bus_to_cluster, data, eic_map = \
        phase_c_jao_line_params(engine, dry_run)

    # Phase D: JAO transformer parameters
    trafo_updates, trafo_eic_map = phase_d_jao_trafo_params(
        engine, dry_run, bus_matches, cluster_info, bus_to_cluster, data)

    # Phase E: Default transformer parameters for unmatched
    jao_trafo_ids = set(trafo_updates.keys())
    default_updates = phase_e_trafo_defaults(engine, dry_run, jao_trafo_ids)

    # Phase F: Scenario cleanup (optional)
    if args.cleanup_scenarios:
        phase_f_cleanup_scenarios(engine, dry_run)
    else:
        print("\n  Phase F: Skipped (use --cleanup-scenarios to enable)")

    # Phase G: Reporting
    phase_g_reporting(country_fixes, hvdc_count, line_updates, trafo_updates,
                      default_updates, eic_map, trafo_eic_map, results_dir)

    print("\nDone.")


if __name__ == '__main__':
    main()
