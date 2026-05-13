#!/usr/bin/env python3
"""
Apply JAO Parameters to eGon2025v3 + Add HVDC Links + Interactive Map
=====================================================================

Runs the JAO/CORE-TSO matching pipeline against the simplified v3 grid,
applies line/transformer parameters, adds HVDC interconnectors, and
generates an interactive Leaflet map with voltage-level toggles, JAO
overlay, and bus hover tooltips.

Usage:
    python scripts/apply_jao_v3.py --dry-run          # preview only
    python scripts/apply_jao_v3.py --apply             # write to DB + map
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

# Allow imports from scripts/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jao_matching

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
SCN = 'eGon2025v3'
RESULTS_DIR = 'results/jao_v3'
MAP_OUTPUT = 'results/jao_v3/v3_jao_map.html'

# HVDC links — bus IDs mapped from v1 through simplification
HVDC_LINKS = [
    {
        'name': 'ALEGRO',
        'p_nom': 1000.0,
        'de_bus': 35187,       # Oberzier 380kV — survives to v3
        'foreign_bus_id': 200001,
        'foreign_country': 'BE',
        'foreign_x': 6.0825,
        'foreign_y': 50.7375,
        'foreign_v_nom': 380,
        'length': 90.0,
    },
    {
        'name': 'NordLink',
        'p_nom': 1400.0,
        'de_bus': 38907,       # Wilster 380kV — v1:38906 -> v3:38907
        'foreign_bus_id': 200002,
        'foreign_country': 'NO',
        'foreign_x': 6.567,
        'foreign_y': 58.362,
        'foreign_v_nom': 380,
        'length': 623.0,
    },
    {
        'name': 'Baltic Cable',
        'p_nom': 600.0,
        'de_bus': 21261,       # Herrenwyk 380kV — v1:36614 -> v3:21261
        'foreign_bus_id': 200003,
        'foreign_country': 'SE',
        'foreign_x': 13.157,
        'foreign_y': 55.377,
        'foreign_v_nom': 380,
        'length': 262.0,
    },
]

TRAFO_DEFAULTS = {
    (220, 380): {'x': 0.04, 'r': 0.0005},
    (110, 220): {'x': 0.12, 'r': 0.003},
    (110, 380): {'x': 0.12, 'r': 0.003},
}


def _native(params):
    """Convert numpy types to native Python for psycopg2."""
    return {k: float(v) if hasattr(v, 'item') else v for k, v in params.items()}


# ===================================================================
# Phase 1: JAO Matching
# ===================================================================
def run_jao_matching(engine):
    """Run full JAO matching pipeline on v3 and return all results."""
    print("\n" + "=" * 70)
    print("PHASE 1: JAO Matching on eGon2025v3")
    print("=" * 70)

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
    trafo_updates, trafo_eic_map = jao_matching.match_and_compute_trafo_updates(
        data['jao_trafos'], bus_matches, cluster_info,
        bus_to_cluster, data['egon_hv_trafos'], data['bus_vnom']
    )

    return {
        'data': data,
        'bus_to_cluster': bus_to_cluster,
        'cluster_info': cluster_info,
        'bus_matches': bus_matches,
        'line_updates': line_updates,
        'trafo_updates': trafo_updates,
        'eic_map': eic_map,
        'trafo_eic_map': trafo_eic_map,
        'assignments': assignments,
        'line_matches': line_matches,
    }


# ===================================================================
# Phase 2: Apply Line Parameters
# ===================================================================
def apply_line_params(engine, line_updates, dry_run):
    print(f"\n  Line parameter updates: {len(line_updates)}")
    if dry_run or not line_updates:
        return
    with engine.begin() as conn:
        for lid, params in line_updates.items():
            native = _native(params)
            set_clauses = ', '.join(f"{k} = :{k}" for k in native)
            conn.execute(text(
                f"UPDATE grid.egon_etrago_line SET {set_clauses} "
                f"WHERE scn_name = :scn AND line_id = :lid"
            ), {**native, 'scn': SCN, 'lid': int(lid)})
    print(f"  Applied {len(line_updates)} line updates")


# ===================================================================
# Phase 3: Apply Transformer Parameters (JAO + defaults)
# ===================================================================
def apply_trafo_params(engine, trafo_updates, dry_run):
    print(f"\n  JAO transformer updates: {len(trafo_updates)}")
    if dry_run or not trafo_updates:
        return
    with engine.begin() as conn:
        for tid, params in trafo_updates.items():
            native = _native(params)
            set_clauses = ', '.join(f"{k} = :{k}" for k in native)
            conn.execute(text(
                f"UPDATE grid.egon_etrago_transformer SET {set_clauses} "
                f"WHERE scn_name = :scn AND trafo_id = :tid"
            ), {**native, 'scn': SCN, 'tid': int(tid)})
    print(f"  Applied {len(trafo_updates)} transformer updates")


def apply_trafo_defaults(engine, jao_trafo_ids, dry_run):
    """Set default x/r for transformers not covered by JAO."""
    print("\n" + "=" * 70)
    print("PHASE 3b: Default transformer parameters")
    print("=" * 70)

    trafos = pd.read_sql(text("""
        SELECT t.trafo_id, t.bus0, t.bus1, t.r, t.x, t.s_nom,
               b0.v_nom as v0, b1.v_nom as v1
        FROM grid.egon_etrago_transformer t
        JOIN grid.egon_etrago_bus b0 ON t.bus0 = b0.bus_id AND b0.scn_name = :scn
        JOIN grid.egon_etrago_bus b1 ON t.bus1 = b1.bus_id AND b1.scn_name = :scn
        WHERE t.scn_name = :scn
    """), engine, params={'scn': SCN})

    # v3 trafos have tiny but non-zero x (max ~3e-6); treat < 0.001 as needing defaults
    needs_default = trafos[
        (trafos['x'] < 0.001) & (trafos['s_nom'] > 0) &
        (~trafos['trafo_id'].isin(jao_trafo_ids))
    ].copy()

    needs_default['v_low'] = needs_default[['v0', 'v1']].min(axis=1).astype(int)
    needs_default['v_high'] = needs_default[['v0', 'v1']].max(axis=1).astype(int)
    needs_default['v_pair'] = list(zip(needs_default['v_low'], needs_default['v_high']))

    default_updates = {}
    for v_pair, group in needs_default.groupby('v_pair'):
        defaults = TRAFO_DEFAULTS.get(v_pair)
        if not defaults:
            print(f"  WARNING: No defaults for {v_pair} ({len(group)} trafos)")
            continue
        print(f"  {v_pair[0]}/{v_pair[1]}kV: {len(group)} trafos -> x={defaults['x']}, r={defaults['r']}")
        for _, row in group.iterrows():
            update = {}
            if row['x'] < 0.001:
                update['x'] = defaults['x']
            if row['r'] < 0.001:
                update['r'] = defaults['r']
            if update:
                default_updates[row['trafo_id']] = update

    print(f"  Default updates: {len(default_updates)}")

    if not dry_run and default_updates:
        with engine.begin() as conn:
            for tid, params in default_updates.items():
                native = _native(params)
                set_clauses = ', '.join(f"{k} = :{k}" for k in native)
                conn.execute(text(
                    f"UPDATE grid.egon_etrago_transformer SET {set_clauses} "
                    f"WHERE scn_name = :scn AND trafo_id = :tid"
                ), {**native, 'scn': SCN, 'tid': int(tid)})
        print(f"  Applied {len(default_updates)} default transformer updates")

    return default_updates


# ===================================================================
# Phase 4: HVDC Links
# ===================================================================
def add_hvdc_links(engine, dry_run):
    print("\n" + "=" * 70)
    print("PHASE 4: Add HVDC links")
    print("=" * 70)

    existing_buses = pd.read_sql(text(
        "SELECT bus_id FROM grid.egon_etrago_bus "
        "WHERE scn_name = :scn AND bus_id >= 200000"
    ), engine, params={'scn': SCN})
    existing_ids = set(existing_buses['bus_id'].tolist())

    max_link_id = pd.read_sql(text(
        "SELECT COALESCE(MAX(link_id), 0) as mid FROM grid.egon_etrago_link "
        "WHERE scn_name = :scn"
    ), engine, params={'scn': SCN}).iloc[0]['mid']

    buses_to_add = []
    links_to_add = []
    link_id = int(max_link_id) + 1

    for hvdc in HVDC_LINKS:
        if hvdc['foreign_bus_id'] in existing_ids:
            print(f"  {hvdc['name']}: already exists — skipping")
            continue

        # Verify DE bus exists
        de_check = pd.read_sql(text(
            "SELECT bus_id FROM grid.egon_etrago_bus "
            "WHERE scn_name = :scn AND bus_id = :bid"
        ), engine, params={'scn': SCN, 'bid': hvdc['de_bus']})
        if len(de_check) == 0:
            print(f"  WARNING: {hvdc['name']} DE bus {hvdc['de_bus']} not in v3!")
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
            'p_min_pu': -1.0,
            'p_max_pu': 1.0,
            'length': hvdc['length'],
            'marginal_cost': 0.0,
            'capital_cost': 0.0,
            'terrain_factor': 1.0,
        })
        print(f"  {hvdc['name']}: {hvdc['p_nom']} MW, "
              f"bus {hvdc['de_bus']} <-> {hvdc['foreign_bus_id']} ({hvdc['foreign_country']})")
        link_id += 1

    if not dry_run and links_to_add:
        pd.DataFrame(buses_to_add).to_sql(
            'egon_etrago_bus', engine, schema='grid', if_exists='append', index=False)
        pd.DataFrame(links_to_add).to_sql(
            'egon_etrago_link', engine, schema='grid', if_exists='append', index=False)
        print(f"  Inserted {len(buses_to_add)} buses + {len(links_to_add)} links")

    return links_to_add


# ===================================================================
# Phase 5: Interactive Map
# ===================================================================
def generate_map(engine, matching_results, default_trafo_updates, output_path):
    """Generate interactive Leaflet HTML map."""
    print("\n" + "=" * 70)
    print("PHASE 5: Generating interactive map")
    print("=" * 70)

    data = matching_results['data']
    line_updates = matching_results['line_updates']
    trafo_updates = matching_results['trafo_updates']
    bus_matches = matching_results['bus_matches']
    cluster_info = matching_results['cluster_info']
    bus_to_cluster = matching_results['bus_to_cluster']

    # Load ALL v3 buses (all voltages)
    all_buses = pd.read_sql(text(
        "SELECT bus_id, x, y, v_nom, country "
        "FROM grid.egon_etrago_bus WHERE scn_name = :scn"
    ), engine, params={'scn': SCN})

    # Load ALL v3 lines
    all_lines = pd.read_sql(text(
        "SELECT line_id, bus0, bus1, length, r, x, b, s_nom, v_nom, cables "
        "FROM grid.egon_etrago_line WHERE scn_name = :scn"
    ), engine, params={'scn': SCN})

    # Load ALL v3 transformers
    all_trafos = pd.read_sql(text(
        "SELECT trafo_id, bus0, bus1, r, x, s_nom "
        "FROM grid.egon_etrago_transformer WHERE scn_name = :scn"
    ), engine, params={'scn': SCN})

    # Load HVDC links
    links = pd.read_sql(text(
        "SELECT link_id, bus0, bus1, p_nom, carrier "
        "FROM grid.egon_etrago_link WHERE scn_name = :scn"
    ), engine, params={'scn': SCN})

    # Substation names from ehv_substation
    ehv_subs = pd.read_sql(text(
        "SELECT s.bus_id, s.subst_name FROM grid.egon_ehv_substation s "
        "JOIN grid.egon_etrago_bus b ON s.bus_id = b.bus_id AND b.scn_name = :scn"
    ), engine, params={'scn': SCN})
    sub_names = dict(zip(ehv_subs['bus_id'], ehv_subs['subst_name']))

    # Bus voltage lookup
    bus_vnom = dict(zip(all_buses['bus_id'], all_buses['v_nom']))
    bus_xy = {r['bus_id']: (r['x'], r['y']) for _, r in all_buses.iterrows()}

    # Determine line voltage from endpoint buses
    def get_line_voltage(row):
        if pd.notna(row.get('v_nom')) and row['v_nom'] > 0:
            return int(row['v_nom'])
        v0 = bus_vnom.get(row['bus0'], 110)
        v1 = bus_vnom.get(row['bus1'], 110)
        return int(max(v0, v1))

    # Load JAO data for overlay
    jao_buses_df = data['jao_buses']
    jao_lines_df = data['jao_lines']

    # --- Build JSON data for the map ---
    # Buses
    bus_json = []
    for _, b in all_buses.iterrows():
        bid = int(b['bus_id'])
        entry = {
            'id': bid,
            'lat': round(b['y'], 6),
            'lon': round(b['x'], 6),
            'v': int(b['v_nom']),
            'c': b['country'] if pd.notna(b['country']) else 'DE',
        }
        name = sub_names.get(bid, '')
        if name:
            entry['n'] = name
        # Add cluster info for 220/380 buses
        cid = bus_to_cluster.get(bid)
        if cid:
            ci = cluster_info.get(cid, {})
            if ci.get('name') and not name:
                entry['n'] = ci['name']
        bus_json.append(entry)

    # Lines — include before/after for matched ones
    line_json = []
    for _, ln in all_lines.iterrows():
        lid = int(ln['line_id'])
        b0, b1 = ln['bus0'], ln['bus1']
        if b0 not in bus_xy or b1 not in bus_xy:
            continue
        v = get_line_voltage(ln)

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

        if lid in line_updates:
            upd = line_updates[lid]
            entry['jao'] = 1
            entry['jr'] = round(upd.get('r', 0), 6)
            entry['jx'] = round(upd.get('x', 0), 6)
            entry['js'] = round(upd.get('s_nom', 0), 1)

        line_json.append(entry)

    # Transformers
    trafo_json = []
    all_trafo_updates = {**trafo_updates}
    if default_trafo_updates:
        for tid, upd in default_trafo_updates.items():
            if tid not in all_trafo_updates:
                all_trafo_updates[tid] = {**upd, 'source': 'default'}

    for _, t in all_trafos.iterrows():
        tid = int(t['trafo_id'])
        b0, b1 = t['bus0'], t['bus1']
        if b0 not in bus_xy or b1 not in bus_xy:
            continue
        v0 = bus_vnom.get(b0, 110)
        v1 = bus_vnom.get(b1, 110)

        entry = {
            'id': tid,
            'b0': [round(bus_xy[b0][1], 6), round(bus_xy[b0][0], 6)],
            'b1': [round(bus_xy[b1][1], 6), round(bus_xy[b1][0], 6)],
            'v0': int(v0),
            'v1': int(v1),
            'sn': round(float(t['s_nom']), 1) if pd.notna(t['s_nom']) else 0,
            'ox': round(float(t['x']), 6) if pd.notna(t['x']) else 0,
            'or': round(float(t['r']), 6) if pd.notna(t['r']) else 0,
        }

        if tid in trafo_updates:
            upd = trafo_updates[tid]
            entry['jao'] = 1
            entry['jx'] = round(upd.get('x', 0), 6)
            entry['jr'] = round(upd.get('r', 0), 6)
        elif tid in (default_trafo_updates or {}):
            entry['def'] = 1
            upd = default_trafo_updates[tid]
            entry['jx'] = round(upd.get('x', 0), 6)
            entry['jr'] = round(upd.get('r', 0), 6)

        trafo_json.append(entry)

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

    # Also add the planned HVDC links that haven't been written yet
    for hvdc in HVDC_LINKS:
        de_bus = hvdc['de_bus']
        fb = hvdc['foreign_bus_id']
        if de_bus in bus_xy and fb not in bus_xy:
            # Not yet in DB — add from definition
            hvdc_json.append({
                'id': -1,
                'b0': [round(bus_xy[de_bus][1], 6), round(bus_xy[de_bus][0], 6)],
                'b1': [hvdc['foreign_y'], hvdc['foreign_x']],
                'pn': hvdc['p_nom'],
                'carrier': 'DC',
                'name': hvdc['name'],
            })

    # JAO buses for overlay
    jao_bus_json = []
    for _, jb in jao_buses_df.iterrows():
        jn = str(jb['name'])
        entry = {
            'lat': round(jb['y'], 6),
            'lon': round(jb['x'], 6),
            'v': int(jb['v_nom_mapped']),
            'osm': jn,
        }
        match = bus_matches.get(jn)
        if match:
            entry['m'] = 1
            entry['t'] = match['tier']
            ci = cluster_info.get(match['cluster_id'], {})
            if ci.get('name'):
                entry['cn'] = ci['name']
        jao_bus_json.append(entry)

    # JAO lines for overlay
    jao_line_json = []
    jao_bus_coords = {str(r['name']): (r['x'], r['y']) for _, r in jao_buses_df.iterrows()}
    for _, jl in jao_lines_df.iterrows():
        b0 = str(jl['bus0_base'])
        b1 = str(jl['bus1_base'])
        if b0 in jao_bus_coords and b1 in jao_bus_coords:
            c0 = jao_bus_coords[b0]
            c1 = jao_bus_coords[b1]
            jao_line_json.append({
                'b0': [round(c0[1], 6), round(c0[0], 6)],
                'b1': [round(c1[1], 6), round(c1[0], 6)],
                'v': int(jl['v_nom_mapped']),
                'ne': str(jl.get('NE_name', '')),
                'sn': round(float(jl['s_nom'] * math.sqrt(3)), 1) if pd.notna(jl['s_nom']) else 0,
            })

    # --- Statistics for header ---
    stats = {
        'buses': len(bus_json),
        'lines': len(line_json),
        'trafos': len(trafo_json),
        'jao_lines_matched': len(line_updates),
        'jao_trafos_matched': len(trafo_updates),
        'trafo_defaults': len(default_trafo_updates) if default_trafo_updates else 0,
        'hvdc': len(hvdc_json),
        'jao_buses': len(jao_bus_json),
        'jao_lines': len(jao_line_json),
    }

    print(f"  Buses: {stats['buses']}")
    print(f"  Lines: {stats['lines']} ({stats['jao_lines_matched']} JAO-matched)")
    print(f"  Transformers: {stats['trafos']} ({stats['jao_trafos_matched']} JAO + {stats['trafo_defaults']} defaults)")
    print(f"  HVDC: {stats['hvdc']}")
    print(f"  JAO overlay: {stats['jao_buses']} buses, {stats['jao_lines']} lines")

    # --- Build HTML ---
    html = _build_map_html(
        bus_json, line_json, trafo_json, hvdc_json,
        jao_bus_json, jao_line_json, stats
    )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        f.write(html)
    print(f"  Map saved: {output_path}")


def _build_map_html(buses, lines, trafos, hvdc, jao_buses, jao_lines, stats):
    """Build the full interactive Leaflet HTML."""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>eGon2025v3 — JAO Parameter Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; background: #1a1a2e; }}
#map {{ position: absolute; top: 0; left: 0; right: 0; bottom: 0; z-index: 1; }}

/* Control panel */
#panel {{
  position: absolute; top: 12px; right: 12px; z-index: 1000;
  background: rgba(22,33,62,0.95); border: 1px solid #2a2a5e;
  border-radius: 10px; padding: 14px 16px; min-width: 240px;
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

/* Tooltip */
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
.bus-tooltip .tt-jao {{ color: #ffb74d; font-weight: 600; }}

/* Legend */
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
  <h2>eGon2025v3 — JAO Map</h2>
  <div class="stat"><b>{stats['buses']}</b> buses &middot; <b>{stats['lines']}</b> lines &middot; <b>{stats['trafos']}</b> trafos</div>
  <div class="stat"><b>{stats['jao_lines_matched']}</b> JAO line matches &middot; <b>{stats['jao_trafos_matched']}</b> JAO trafo matches</div>

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
    <label for="showBuses">Buses (nodes)</label>
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
  <h3>JAO Overlay</h3>
  <div class="cb-row">
    <input type="checkbox" id="showJaoBuses" onchange="updateLayers()">
    <label for="showJaoBuses">JAO Substations ({stats['jao_buses']})</label>
  </div>
  <div class="cb-row">
    <input type="checkbox" id="showJaoLines" onchange="updateLayers()">
    <label for="showJaoLines">JAO Lines ({stats['jao_lines']})</label>
  </div>

  <div class="divider"></div>
  <h3>Highlight</h3>
  <div class="cb-row">
    <input type="checkbox" id="hlJaoMatch" onchange="updateLayers()">
    <label for="hlJaoMatch">JAO-matched lines only</label>
  </div>
  <div class="cb-row">
    <input type="checkbox" id="hlTrafoJao" onchange="updateLayers()">
    <label for="hlTrafoJao">JAO-matched trafos only</label>
  </div>
</div>

<div id="legend">
  <div class="leg-item"><div class="leg-line" style="background:#e53935"></div> 380 kV</div>
  <div class="leg-item"><div class="leg-line" style="background:#1e88e5"></div> 220 kV</div>
  <div class="leg-item"><div class="leg-line" style="background:#43a047"></div> 110 kV</div>
  <div class="leg-item"><div class="leg-line" style="background:#ffb74d"></div> JAO-matched</div>
  <div class="leg-item"><div class="leg-dash" style="border-color:#ab47bc"></div> Transformer</div>
  <div class="leg-item"><div class="leg-dash" style="border-color:#ff7043"></div> HVDC Link</div>
  <div class="leg-item"><div class="leg-line" style="background:#00e5ff;opacity:0.5"></div> JAO grid</div>
</div>

<script>
// ── Data ──
const BUSES = {json.dumps(buses, separators=(',', ':'))};
const LINES = {json.dumps(lines, separators=(',', ':'))};
const TRAFOS = {json.dumps(trafos, separators=(',', ':'))};
const HVDC = {json.dumps(hvdc, separators=(',', ':'))};
const JAO_BUSES = {json.dumps(jao_buses, separators=(',', ':'))};
const JAO_LINES = {json.dumps(jao_lines, separators=(',', ':'))};

// ── Color scheme ──
const V_COLORS = {{380: '#e53935', 220: '#1e88e5', 110: '#43a047'}};
const V_WEIGHTS = {{380: 2.5, 220: 2, 110: 1.2}};

// ── Map init ──
const map = L.map('map', {{
  center: [51.2, 10.4],
  zoom: 6,
  zoomControl: true,
  preferCanvas: true,
}});

L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; OSM &copy; CARTO',
  maxZoom: 19,
}}).addTo(map);

// ── Layers ──
let lineLayer, busLayer, trafoLayer, hvdcLayer, jaoBusLayer, jaoLineLayer;

// Bus lookup for tooltips
const busMap = {{}};
BUSES.forEach(b => busMap[b.id] = b);

// ── Tooltip builders ──
function vBadge(v) {{
  const c = V_COLORS[v] || '#888';
  return `<span class="tt-v" style="background:${{c}}40;color:${{c}}">${{v}} kV</span>`;
}}

function busTooltip(b) {{
  let h = `<div class="bus-tooltip">`;
  h += `<div class="tt-title">Bus ${{b.id}} ${{vBadge(b.v)}}</div>`;
  if (b.n) h += `<div>${{b.n}}</div>`;
  if (b.c && b.c !== 'DE') h += `<div class="tt-param">Country: ${{b.c}}</div>`;
  h += `<div class="tt-param">Coords: ${{b.lat.toFixed(4)}}, ${{b.lon.toFixed(4)}}</div>`;
  h += `</div>`;
  return h;
}}

function lineTooltip(l) {{
  let h = `<div class="bus-tooltip">`;
  h += `<div class="tt-title">Line ${{l.id}} ${{vBadge(l.v)}}</div>`;
  h += `<div class="tt-param">Length: ${{l.ln}} km &middot; s_nom: ${{l.sn}} MVA</div>`;
  if (l.jao) {{
    h += `<div style="margin-top:4px"><span class="tt-jao">JAO-MATCHED</span></div>`;
    h += `<table style="font-size:11px;margin-top:4px;border-collapse:collapse">`;
    h += `<tr><td class="tt-param" style="padding-right:8px">Param</td><td class="tt-param">Before</td><td class="tt-param">After (JAO)</td></tr>`;
    h += `<tr><td>r</td><td class="tt-old">${{l.r.toFixed(4)}}</td><td class="tt-new">${{l.jr.toFixed(4)}}</td></tr>`;
    h += `<tr><td>x</td><td class="tt-old">${{l.x.toFixed(4)}}</td><td class="tt-new">${{l.jx.toFixed(4)}}</td></tr>`;
    h += `<tr><td>s_nom</td><td class="tt-old">${{l.sn}}</td><td class="tt-new">${{l.js}}</td></tr>`;
    h += `</table>`;
  }} else {{
    h += `<div class="tt-param">r=${{l.r.toFixed(4)}}, x=${{l.x.toFixed(4)}}</div>`;
  }}
  h += `</div>`;
  return h;
}}

function trafoTooltip(t) {{
  let h = `<div class="bus-tooltip">`;
  h += `<div class="tt-title">Transformer ${{t.id}}</div>`;
  h += `<div class="tt-param">${{t.v0}}/${{t.v1}} kV &middot; s_nom: ${{t.sn}} MVA</div>`;
  if (t.jao) {{
    h += `<div style="margin-top:4px"><span class="tt-jao">JAO-MATCHED</span></div>`;
    h += `<table style="font-size:11px;margin-top:4px;border-collapse:collapse">`;
    h += `<tr><td class="tt-param" style="padding-right:8px">Param</td><td class="tt-param">Before</td><td class="tt-param">After</td></tr>`;
    h += `<tr><td>r</td><td class="tt-old">${{t.or.toFixed(6)}}</td><td class="tt-new">${{t.jr.toFixed(6)}}</td></tr>`;
    h += `<tr><td>x</td><td class="tt-old">${{t.ox.toFixed(6)}}</td><td class="tt-new">${{t.jx.toFixed(6)}}</td></tr>`;
    h += `</table>`;
  }} else if (t.def) {{
    h += `<div style="margin-top:4px"><span class="tt-param" style="color:#ce93d8">DEFAULT PARAMS</span></div>`;
    h += `<div class="tt-param">x=${{t.jx}}, r=${{t.jr}}</div>`;
  }} else {{
    h += `<div class="tt-param">r=${{t.or.toFixed(6)}}, x=${{t.ox.toFixed(6)}}</div>`;
  }}
  h += `</div>`;
  return h;
}}

function hvdcTooltip(h) {{
  let s = `<div class="bus-tooltip">`;
  s += `<div class="tt-title">HVDC Link` + (h.name ? ` — ${{h.name}}` : ` ${{h.id}}`) + `</div>`;
  s += `<div class="tt-param">Capacity: ${{h.pn}} MW</div>`;
  s += `</div>`;
  return s;
}}

function jaoBusTooltip(j) {{
  let h = `<div class="bus-tooltip">`;
  h += `<div class="tt-title">JAO Substation ${{vBadge(j.v)}}</div>`;
  h += `<div class="tt-param">OSM: ${{j.osm}}</div>`;
  if (j.m) {{
    h += `<div class="tt-new">Matched (tier ${{j.t}})</div>`;
    if (j.cn) h += `<div>→ ${{j.cn}}</div>`;
  }} else {{
    h += `<div class="tt-old">Unmatched</div>`;
  }}
  h += `</div>`;
  return h;
}}

function jaoLineTooltip(j) {{
  let h = `<div class="bus-tooltip">`;
  h += `<div class="tt-title">JAO Line ${{vBadge(j.v)}}</div>`;
  if (j.ne) h += `<div>${{j.ne}}</div>`;
  h += `<div class="tt-param">s_nom: ${{j.sn}} MVA (3-phase)</div>`;
  h += `</div>`;
  return h;
}}

// ── Layer drawing ──
function getVis() {{
  return {{
    v380: document.getElementById('v380').checked,
    v220: document.getElementById('v220').checked,
    v110: document.getElementById('v110').checked,
    buses: document.getElementById('showBuses').checked,
    lines: document.getElementById('showLines').checked,
    trafos: document.getElementById('showTrafos').checked,
    hvdc: document.getElementById('showHVDC').checked,
    jaoBuses: document.getElementById('showJaoBuses').checked,
    jaoLines: document.getElementById('showJaoLines').checked,
    hlJao: document.getElementById('hlJaoMatch').checked,
    hlTrafoJao: document.getElementById('hlTrafoJao').checked,
  }};
}}

function vVisible(v, vis) {{
  if (v === 380) return vis.v380;
  if (v === 220) return vis.v220;
  return vis.v110;
}}

function updateLayers() {{
  const vis = getVis();

  // Remove old layers
  if (lineLayer) map.removeLayer(lineLayer);
  if (busLayer) map.removeLayer(busLayer);
  if (trafoLayer) map.removeLayer(trafoLayer);
  if (hvdcLayer) map.removeLayer(hvdcLayer);
  if (jaoBusLayer) map.removeLayer(jaoBusLayer);
  if (jaoLineLayer) map.removeLayer(jaoLineLayer);

  // JAO lines (bottom)
  if (vis.jaoLines) {{
    jaoLineLayer = L.layerGroup();
    JAO_LINES.forEach(j => {{
      if (!vVisible(j.v, vis)) return;
      L.polyline([j.b0, j.b1], {{
        color: '#00e5ff', weight: 2, opacity: 0.4, dashArray: '6,4',
      }}).bindTooltip(jaoLineTooltip(j), {{className:'bus-tooltip',sticky:true}}).addTo(jaoLineLayer);
    }});
    jaoLineLayer.addTo(map);
  }}

  // Lines
  if (vis.lines) {{
    lineLayer = L.layerGroup();
    LINES.forEach(l => {{
      if (!vVisible(l.v, vis)) return;
      if (vis.hlJao && !l.jao) return;
      const color = l.jao ? '#ffb74d' : (V_COLORS[l.v] || '#888');
      const weight = l.jao ? 3 : (V_WEIGHTS[l.v] || 1);
      const opacity = l.jao ? 0.9 : 0.55;
      L.polyline([l.b0, l.b1], {{
        color, weight, opacity,
      }}).bindTooltip(lineTooltip(l), {{className:'bus-tooltip',sticky:true}}).addTo(lineLayer);
    }});
    lineLayer.addTo(map);
  }}

  // Transformers
  if (vis.trafos) {{
    trafoLayer = L.layerGroup();
    TRAFOS.forEach(t => {{
      const v = Math.max(t.v0, t.v1);
      if (!vVisible(v, vis) && !vVisible(Math.min(t.v0,t.v1), vis)) return;
      if (vis.hlTrafoJao && !t.jao) return;
      const color = t.jao ? '#ffb74d' : (t.def ? '#ce93d8' : '#ab47bc');
      L.polyline([t.b0, t.b1], {{
        color, weight: 3, opacity: 0.8, dashArray: '5,5',
      }}).bindTooltip(trafoTooltip(t), {{className:'bus-tooltip',sticky:true}}).addTo(trafoLayer);
    }});
    trafoLayer.addTo(map);
  }}

  // HVDC
  if (vis.hvdc) {{
    hvdcLayer = L.layerGroup();
    HVDC.forEach(h => {{
      L.polyline([h.b0, h.b1], {{
        color: '#ff7043', weight: 3.5, opacity: 0.9, dashArray: '10,6',
      }}).bindTooltip(hvdcTooltip(h), {{className:'bus-tooltip',sticky:true}}).addTo(hvdcLayer);
    }});
    hvdcLayer.addTo(map);
  }}

  // JAO buses
  if (vis.jaoBuses) {{
    jaoBusLayer = L.layerGroup();
    JAO_BUSES.forEach(j => {{
      if (!vVisible(j.v, vis)) return;
      const color = j.m ? (j.t === 0 ? '#66bb6a' : '#42a5f5') : '#ef5350';
      L.circleMarker([j.lat, j.lon], {{
        radius: 5, color, fillColor: color, fillOpacity: 0.8, weight: 1,
      }}).bindTooltip(jaoBusTooltip(j), {{className:'bus-tooltip'}}).addTo(jaoBusLayer);
    }});
    jaoBusLayer.addTo(map);
  }}

  // Buses (top)
  if (vis.buses) {{
    busLayer = L.layerGroup();
    BUSES.forEach(b => {{
      if (!vVisible(b.v, vis)) return;
      const color = V_COLORS[b.v] || '#888';
      L.circleMarker([b.lat, b.lon], {{
        radius: 2.5, color, fillColor: color, fillOpacity: 0.7, weight: 0.5,
      }}).bindTooltip(busTooltip(b), {{className:'bus-tooltip'}}).addTo(busLayer);
    }});
    busLayer.addTo(map);
  }}
}}

// Initial render
updateLayers();
</script>
</body>
</html>"""


# ===================================================================
# Phase 6: Reporting
# ===================================================================
def save_reports(results_dir, matching_results, default_trafo_updates):
    print("\n" + "=" * 70)
    print("PHASE 6: Reports")
    print("=" * 70)

    os.makedirs(results_dir, exist_ok=True)

    lu = matching_results['line_updates']
    tu = matching_results['trafo_updates']

    if lu:
        rows = [{'line_id': lid, **p} for lid, p in lu.items()]
        pd.DataFrame(rows).to_csv(os.path.join(results_dir, 'line_updates.csv'), index=False)
        print(f"  line_updates.csv: {len(rows)} rows")

    all_tu = {}
    if tu:
        for tid, p in tu.items():
            all_tu[tid] = {**p, 'source': 'jao'}
    if default_trafo_updates:
        for tid, p in default_trafo_updates.items():
            if tid not in all_tu:
                all_tu[tid] = {**p, 'source': 'default'}
    if all_tu:
        rows = [{'trafo_id': tid, **p} for tid, p in all_tu.items()]
        pd.DataFrame(rows).to_csv(os.path.join(results_dir, 'trafo_updates.csv'), index=False)
        print(f"  trafo_updates.csv: {len(rows)} rows")

    eic = matching_results.get('eic_map', [])
    teic = matching_results.get('trafo_eic_map', [])
    if eic or teic:
        combined = pd.concat([pd.DataFrame(eic), pd.DataFrame(teic)], ignore_index=True, sort=False)
        combined.to_csv(os.path.join(results_dir, 'eic_mapping.csv'), index=False)
        print(f"  eic_mapping.csv: {len(combined)} rows")

    # Summary
    print(f"\n  === SUMMARY ===")
    print(f"  Line updates:        {len(lu)}")
    print(f"  Trafo updates (JAO): {len(tu)}")
    print(f"  Trafo updates (def): {len(default_trafo_updates) if default_trafo_updates else 0}")
    print(f"  Total trafos fixed:  {len(all_tu)}/535")


# ===================================================================
# Main
# ===================================================================
def main():
    parser = argparse.ArgumentParser(description='Apply JAO params to eGon2025v3')
    parser.add_argument('--dry-run', action='store_true', default=True,
                        help='Preview only (default)')
    parser.add_argument('--apply', action='store_true',
                        help='Write changes to database')
    parser.add_argument('--output-dir', default=RESULTS_DIR)
    parser.add_argument('--map-output', default=MAP_OUTPUT)
    args = parser.parse_args()

    dry_run = not args.apply

    print("=" * 70)
    print(f"Apply JAO Parameters to eGon2025v3")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE — writing to DB'}")
    print("=" * 70)

    engine = create_engine(DB_URI)

    # Phase 1: JAO matching
    results = run_jao_matching(engine)

    # Phase 2: Apply line params
    print("\n" + "=" * 70)
    print("PHASE 2: Apply line parameters")
    print("=" * 70)
    apply_line_params(engine, results['line_updates'], dry_run)

    # Phase 3: Apply transformer params
    print("\n" + "=" * 70)
    print("PHASE 3a: Apply JAO transformer parameters")
    print("=" * 70)
    apply_trafo_params(engine, results['trafo_updates'], dry_run)

    jao_trafo_ids = set(results['trafo_updates'].keys())
    default_updates = apply_trafo_defaults(engine, jao_trafo_ids, dry_run)

    # Phase 4: HVDC links
    add_hvdc_links(engine, dry_run)

    # Phase 5: Interactive map (always generated, even in dry-run)
    generate_map(engine, results, default_updates, args.map_output)

    # Phase 6: Reports
    save_reports(args.output_dir, results, default_updates)

    print("\nDone.")


if __name__ == '__main__':
    main()
