#!/usr/bin/env python3
"""
run_12month_lopf.py - 12 monthly LOPF snapshots with interactive power flow map.

Picks one random hour per month, runs LOPF for all 12 snapshots, and creates
an interactive HTML map showing line loading, dispatch, and power flow.

Usage:
    conda activate egon2025
    python scripts/run_12month_lopf.py
"""

import json
import sys
import os
import numpy as np
import pandas as pd
from datetime import datetime
from sqlalchemy import create_engine, text
import pypsa
import warnings
import logging

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
log = logging.getLogger(__name__)

DB_URL = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
SCN = 'grid_beta'
YEAR = 2025
OUTDIR = '/root/egon_2025_project/results'

MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

CARRIER_COLORS = {
    'solar': '#FFD700', 'onwind': '#4CAF50', 'offwind': '#00BCD4',
    'run_of_river': '#2196F3', 'reservoir': '#1565C0',
    'biogas': '#8BC34A', 'biomass': '#795548', 'waste': '#607D8B',
    'gas_ccgt': '#FF9800', 'gas_chp': '#FF5722', 'gas': '#FF9800',
    'coal': '#424242', 'lignite': '#6D4C41', 'oil': '#E91E63',
    'other': '#9E9E9E', 'hydrogen': '#00E5FF',
    'import_FR': '#003399', 'import_AT': '#CC0000', 'import_CH': '#CC0000',
    'import_NL': '#FF6600', 'import_DK': '#CC0000', 'import_PL': '#FFFFFF',
    'import_CZ': '#003399', 'import_NO': '#003399', 'import_SE': '#003399',
    'import_BE': '#000000', 'import_LU': '#003399',
}


def pick_snapshots(seed=42):
    """Pick one random hour per month. Returns (snapshots, hour_indices)."""
    rng = np.random.RandomState(seed)
    snapshots = []
    hour_indices = []

    for month in range(1, 13):
        max_day = 28 if month == 2 else (30 if month in [4, 6, 9, 11] else 31)
        day = int(rng.randint(8, min(max_day, 24)))
        hour = int(rng.randint(0, 24))
        dt = pd.Timestamp(datetime(YEAR, month, day, hour))
        snapshots.append(dt)
        hoy = int((dt - pd.Timestamp(f'{YEAR}-01-01')).total_seconds() / 3600)
        hour_indices.append(hoy)
        log.info(f"  {MONTH_NAMES[month-1]}: {dt.strftime('%Y-%m-%d %H:%M')} (hour {hoy})")

    return pd.DatetimeIndex(snapshots), hour_indices


def load_network(engine, scn, snapshots, hour_indices):
    """Build PyPSA network from DB with timeseries at specific hours."""
    log.info("Loading components from database...")

    # ── Load tables ───────────────────────────────────────────────────────
    buses = pd.read_sql(f"SELECT * FROM grid.egon_etrago_bus WHERE scn_name='{scn}'", engine)
    lines = pd.read_sql(f"SELECT * FROM grid.egon_etrago_line WHERE scn_name='{scn}'", engine)
    trafos = pd.read_sql(f"SELECT * FROM grid.egon_etrago_transformer WHERE scn_name='{scn}'", engine)
    gens = pd.read_sql(f"SELECT * FROM grid.egon_etrago_generator WHERE scn_name='{scn}'", engine)
    loads = pd.read_sql(f"SELECT * FROM grid.egon_etrago_load WHERE scn_name='{scn}'", engine)
    links = pd.read_sql(f"SELECT * FROM grid.egon_etrago_link WHERE scn_name='{scn}'", engine)

    log.info(f"  {len(buses)} buses, {len(lines)} lines, {len(trafos)} trafos, "
             f"{len(gens)} gens, {len(loads)} loads, {len(links)} links")

    # ── Load timeseries (extract only needed hours) ───────────────────────
    h_cols = ", ".join(f"p_set[{h+1}] as h{i}" for i, h in enumerate(hour_indices))
    load_ts_raw = pd.read_sql(f"""
        SELECT load_id, {h_cols}
        FROM grid.egon_etrago_load_timeseries WHERE scn_name='{scn}'
    """, engine)

    # Generator timeseries
    pmax_cols = ", ".join(f"p_max_pu[{h+1}] as pmax_{i}" for i, h in enumerate(hour_indices))
    pmin_cols = ", ".join(f"p_min_pu[{h+1}] as pmin_{i}" for i, h in enumerate(hour_indices))
    gen_ts_raw = pd.read_sql(f"""
        SELECT generator_id,
               {pmax_cols}, {pmin_cols}
        FROM grid.egon_etrago_generator_timeseries WHERE scn_name='{scn}'
    """, engine)

    log.info(f"  Timeseries: {len(load_ts_raw)} loads, {len(gen_ts_raw)} generators")

    # ── Build network ─────────────────────────────────────────────────────
    n = pypsa.Network()
    n.set_snapshots(snapshots)

    # Buses
    buses = buses.set_index('bus_id')
    n.madd('Bus', buses.index.astype(str),
           v_nom=buses['v_nom'].values,
           x=buses['x'].values, y=buses['y'].values,
           carrier='AC', country=buses['country'].values)

    # Lines (filter x > 0)
    valid = lines[lines['x'] > 0].copy()
    log.info(f"  Lines with x>0: {len(valid)}/{len(lines)}")
    n.madd('Line', valid['line_id'].astype(str),
           bus0=valid['bus0'].astype(str).values,
           bus1=valid['bus1'].astype(str).values,
           x=valid['x'].values, r=valid['r'].values,
           s_nom=valid['s_nom'].values,
           length=valid['length'].values,
           v_nom=valid['v_nom'].values,
           s_max_pu=1e6)  # unconstrained

    # Transformers
    trafos = trafos.copy()
    bad_x = trafos['x'].abs() < 1e-6
    if bad_x.any():
        trafos.loc[bad_x, 'x'] = 0.10
        log.warning(f"  Fixed {bad_x.sum()} trafos with near-zero x")

    # Fix extreme PST phase_shift angles (±60/±120° → ±20/±25°)
    pst_mask = trafos['phase_shift'].abs() > 0.01
    if pst_mask.any():
        orig = trafos.loc[pst_mask, 'phase_shift'].copy()
        trafos.loc[pst_mask, 'phase_shift'] = trafos.loc[pst_mask, 'phase_shift'].clip(-25, 25)
        # Preserve sign, scale to realistic range
        for idx in trafos[pst_mask].index:
            ps = orig[idx]
            if abs(ps) > 90:  # -120° → -25°
                trafos.loc[idx, 'phase_shift'] = -25.0 if ps < 0 else 25.0
            elif abs(ps) > 45:  # ±60° → ±20°
                trafos.loc[idx, 'phase_shift'] = 20.0 if ps > 0 else -20.0
        log.info(f"  Fixed {pst_mask.sum()} PST phase_shift angles:")
        for idx in trafos[pst_mask].index:
            log.info(f"    Trafo {trafos.loc[idx, 'trafo_id']}: {orig[idx]:.0f}° → {trafos.loc[idx, 'phase_shift']:.0f}°")

    n.madd('Transformer', trafos['trafo_id'].astype(str),
           bus0=trafos['bus0'].astype(str).values,
           bus1=trafos['bus1'].astype(str).values,
           x=trafos['x'].values, r=trafos['r'].values,
           s_nom=trafos['s_nom'].values,
           tap_ratio=trafos['tap_ratio'].values,
           phase_shift=trafos['phase_shift'].values,
           s_max_pu=1e6)  # unconstrained

    # Links (HVDC)
    if len(links) > 0:
        n.madd('Link', links['link_id'].astype(str),
               bus0=links['bus0'].astype(str).values,
               bus1=links['bus1'].astype(str).values,
               p_nom=links['p_nom'].values,
               p_min_pu=links['p_min_pu'].values,
               efficiency=links['efficiency'].values,
               carrier=links['carrier'].values if 'carrier' in links.columns else 'DC')

    # Generators
    n.madd('Generator', gens['generator_id'].astype(str),
           bus=gens['bus'].astype(str).values,
           carrier=gens['carrier'].values,
           p_nom=gens['p_nom'].values,
           marginal_cost=gens['marginal_cost'].values,
           efficiency=gens['efficiency'].values,
           p_min_pu=gens['p_min_pu'].values,
           p_max_pu=gens['p_max_pu'].values)

    # Loads
    n.madd('Load', loads['load_id'].astype(str),
           bus=loads['bus'].astype(str).values,
           carrier=loads['carrier'].values if 'carrier' in loads.columns else 'AC',
           p_set=loads['p_set'].abs().values)

    # ── Apply timeseries ──────────────────────────────────────────────────
    log.info("Applying timeseries...")

    # Load timeseries (p_set)
    if len(load_ts_raw) > 0:
        load_ts_df = pd.DataFrame(index=snapshots)
        for _, row in load_ts_raw.iterrows():
            lid = str(int(row['load_id']))
            if lid in n.loads.index:
                vals = [row[f'h{i}'] for i in range(12)]
                load_ts_df[lid] = vals
        # Fill missing loads with static p_set
        for lid in n.loads.index:
            if lid not in load_ts_df.columns:
                load_ts_df[lid] = n.loads.loc[lid, 'p_set']
        n.loads_t.p_set = load_ts_df.abs()

    # Generator timeseries (p_max_pu, p_min_pu)
    if len(gen_ts_raw) > 0:
        pmax_df = pd.DataFrame(index=snapshots)
        pmin_df = pd.DataFrame(index=snapshots)

        # Import generators should NOT have timeseries (DB has erroneous entries)
        import_gids = set(n.generators[n.generators.carrier.str.startswith('import_')].index)
        skipped_import = 0

        for _, row in gen_ts_raw.iterrows():
            gid = str(int(row['generator_id']))
            if gid not in n.generators.index:
                continue
            if gid in import_gids:
                skipped_import += 1
                continue

            # p_max_pu
            pmax_vals = [row[f'pmax_{i}'] for i in range(12)]
            if any(v is not None for v in pmax_vals):
                pmax_df[gid] = [v if v is not None else 1.0 for v in pmax_vals]

            # p_min_pu
            pmin_vals = [row[f'pmin_{i}'] for i in range(12)]
            if any(v is not None for v in pmin_vals):
                pmin_df[gid] = [v if v is not None else 0.0 for v in pmin_vals]

        if skipped_import:
            log.info(f"  Skipped {skipped_import} import gen timeseries (erroneous DB entries)")

        if len(pmax_df.columns) > 0:
            n.generators_t.p_max_pu = pmax_df
            log.info(f"  p_max_pu timeseries: {len(pmax_df.columns)} generators")
        if len(pmin_df.columns) > 0:
            n.generators_t.p_min_pu = pmin_df
            log.info(f"  p_min_pu timeseries: {len(pmin_df.columns)} generators")

    # ── Remove isolated buses ──────────────────────────────────────────
    connected = set()
    for b in n.lines.bus0: connected.add(b)
    for b in n.lines.bus1: connected.add(b)
    for b in n.transformers.bus0: connected.add(b)
    for b in n.transformers.bus1: connected.add(b)
    for b in n.links.bus0: connected.add(b)
    for b in n.links.bus1: connected.add(b)
    for b in n.generators.bus: connected.add(b)
    for b in n.loads.bus: connected.add(b)

    isolated = n.buses.index.difference(pd.Index(list(connected)))
    if len(isolated) > 0:
        log.info(f"  Removing {len(isolated)} isolated buses")
        n.mremove('Bus', isolated)

    log.info(f"Network ready: {len(n.buses)} buses, {len(n.lines)} lines, "
             f"{len(n.generators)} gens, {len(n.loads)} loads, {len(n.links)} links")

    return n


def run_lopf(n):
    """Run LOPF with CBC solver."""
    log.info("\nRunning LOPF (12 snapshots, CBC solver)...")
    status, condition = n.lopf(pyomo=False, solver_name='cbc')
    log.info(f"  Status: {status}, Condition: {condition}")

    if status != 'ok':
        log.error(f"LOPF failed! Trying with relaxed constraints...")
        # Remove p_min_pu constraints
        n.generators.p_min_pu = 0.0
        if len(n.generators_t.p_min_pu) > 0:
            n.generators_t.p_min_pu = pd.DataFrame(index=n.snapshots)
        status, condition = n.lopf(pyomo=False, solver_name='cbc')
        log.info(f"  Retry status: {status}, Condition: {condition}")

    return status, condition


def analyze_results(n, snapshots):
    """Print dispatch and loading summary."""
    log.info("\n" + "=" * 70)
    log.info("RESULTS")
    log.info("=" * 70)

    gen_p = n.generators_t.p
    carriers = n.generators.carrier

    # Group carriers for display
    display_carriers = ['solar', 'onwind', 'offwind', 'run_of_river',
                        'biogas', 'biomass', 'waste',
                        'gas_ccgt', 'gas_chp', 'coal', 'lignite',
                        'oil', 'other']
    import_carriers = [c for c in carriers.unique() if c.startswith('import_')]

    log.info(f"\n{'Month':<6} {'Load':>7} {'Solar':>7} {'Wind':>7} "
             f"{'Bio':>6} {'Gas':>7} {'Coal':>6} {'Lign':>6} {'Imp':>6} {'Curt%':>5}")
    log.info("-" * 70)

    for i, snap in enumerate(snapshots):
        load_h = n.loads_t.p_set.loc[snap].sum() / 1e3
        solar_h = gen_p.loc[snap, carriers == 'solar'].sum() / 1e3
        wind_h = gen_p.loc[snap, carriers.isin(['onwind', 'offwind'])].sum() / 1e3
        bio_h = gen_p.loc[snap, carriers.isin(['biogas', 'biomass', 'waste'])].sum() / 1e3
        gas_h = gen_p.loc[snap, carriers.isin(['gas_ccgt', 'gas_chp', 'gas'])].sum() / 1e3
        coal_h = gen_p.loc[snap, carriers == 'coal'].sum() / 1e3
        lign_h = gen_p.loc[snap, carriers == 'lignite'].sum() / 1e3
        imp_h = gen_p.loc[snap, carriers.isin(import_carriers)].sum() / 1e3

        # Curtailment
        re_carriers = ['solar', 'onwind', 'offwind']
        re_ids = carriers[carriers.isin(re_carriers)].index
        if len(re_ids) > 0 and len(n.generators_t.p_max_pu) > 0:
            re_in_ts = [g for g in re_ids if g in n.generators_t.p_max_pu.columns]
            if re_in_ts:
                pot = (n.generators_t.p_max_pu.loc[snap, re_in_ts] *
                       n.generators.loc[re_in_ts, 'p_nom']).sum() / 1e3
                act = gen_p.loc[snap, re_in_ts].sum() / 1e3
                curt = max(0, (pot - act) / pot * 100) if pot > 0 else 0
            else:
                curt = 0
        else:
            curt = 0

        log.info(f"{MONTH_NAMES[i]:<6} {load_h:>7.1f} {solar_h:>7.1f} {wind_h:>7.1f} "
                 f"{bio_h:>6.1f} {gas_h:>7.1f} {coal_h:>6.1f} {lign_h:>6.1f} "
                 f"{imp_h:>6.1f} {curt:>4.0f}%")

    # Line loading stats
    if len(n.lines_t.p0) > 0:
        log.info(f"\nLine loading (% of s_nom):")
        for i, snap in enumerate(snapshots):
            loading = n.lines_t.p0.loc[snap].abs() / n.lines.s_nom * 100
            over = (loading > 100).sum()
            log.info(f"  {MONTH_NAMES[i]}: mean={loading.mean():.1f}%, "
                     f"max={loading.max():.0f}%, >100%: {over} lines")


def create_map(n, snapshots, hour_indices):
    """Create interactive HTML map with monthly power flow visualization."""
    log.info("\nCreating interactive map...")
    from collections import defaultdict

    # ── Bus coordinates ───────────────────────────────────────────────────
    bus_coords = {}
    for bid, row in n.buses.iterrows():
        bus_coords[bid] = (round(row['y'], 5), round(row['x'], 5), int(row['v_nom']))

    # ── Per-bus generation by carrier per month (vectorized) ──────────────
    gen_p = n.generators_t.p
    gen_info = n.generators[['bus', 'carrier']].copy()
    mcols = [f'm{i}' for i in range(12)]
    for i, snap in enumerate(snapshots):
        gen_info[mcols[i]] = gen_p.loc[snap].reindex(gen_info.index, fill_value=0.0).values

    bus_gen = gen_info.groupby(['bus', 'carrier'])[mcols].sum()
    bus_gen_dict = defaultdict(dict)
    for (bus, carrier), row in bus_gen.iterrows():
        vals = [round(float(v), 1) for v in row.values]
        if any(abs(v) > 0.5 for v in vals):
            bus_gen_dict[bus][carrier] = vals

    # ── Per-bus load per month (vectorized) ───────────────────────────────
    load_ps = n.loads_t.p_set
    load_info = n.loads[['bus']].copy()
    for i, snap in enumerate(snapshots):
        load_info[mcols[i]] = load_ps.loc[snap].reindex(load_info.index, fill_value=0.0).values
    bus_load = load_info.groupby('bus')[mcols].sum()

    bus_load_dict = {}
    for bus, row in bus_load.iterrows():
        vals = [round(float(v), 1) for v in row.values]
        if any(abs(v) > 0.5 for v in vals):
            bus_load_dict[bus] = vals

    # ── Build bus JSON ────────────────────────────────────────────────────
    bus_json = {}
    for bid, (lat, lon, v) in bus_coords.items():
        entry = {'la': lat, 'lo': lon, 'v': v}
        if bid in bus_gen_dict:
            entry['g'] = bus_gen_dict[bid]
        if bid in bus_load_dict:
            entry['ld'] = bus_load_dict[bid]
        bus_json[bid] = entry

    # ── Parallel line detection ───────────────────────────────────────────
    par_groups = defaultdict(list)
    for lid, row in n.lines.iterrows():
        key = tuple(sorted([row['bus0'], row['bus1']]))
        par_groups[key].append(lid)

    par_map = {}
    for key, lids in par_groups.items():
        for idx, lid in enumerate(lids):
            par_map[lid] = (len(lids), idx)

    n_parallel = sum(1 for lid, (gs, _) in par_map.items() if gs > 1)
    log.info(f"  Parallel lines: {n_parallel} lines in {sum(1 for lids in par_groups.values() if len(lids)>1)} corridors")

    # ── Line data ─────────────────────────────────────────────────────────
    lines_data = []
    for lid, row in n.lines.iterrows():
        b0, b1 = row['bus0'], row['bus1']
        if b0 not in bus_coords or b1 not in bus_coords:
            continue
        s_nom = row['s_nom']
        v = int(row['v_nom']) if not pd.isna(row.get('v_nom', np.nan)) else 110

        flows = n.lines_t.p0[lid].values if lid in n.lines_t.p0.columns else np.zeros(12)
        loadings = (np.abs(flows) / s_nom * 100).tolist() if s_nom > 0 else [0.0] * 12

        gs, gi = par_map.get(lid, (1, 0))
        lines_data.append({
            'id': str(lid), 'b0': [bus_coords[b0][0], bus_coords[b0][1]],
            'b1': [bus_coords[b1][0], bus_coords[b1][1]],
            'v': v, 'sn': round(s_nom, 1),
            'f': [round(float(f), 1) for f in flows.tolist()],
            'l': [round(float(l), 1) for l in loadings],
            'ps': gs, 'pi': gi,
        })

    # ── Transformer data ──────────────────────────────────────────────────
    trafos_data = []
    for tid, row in n.transformers.iterrows():
        b0, b1 = row['bus0'], row['bus1']
        if b0 not in bus_coords or b1 not in bus_coords:
            continue
        flows = n.transformers_t.p0[tid].values if tid in n.transformers_t.p0.columns else np.zeros(12)
        trafos_data.append({
            'b0': [bus_coords[b0][0], bus_coords[b0][1]],
            'b1': [bus_coords[b1][0], bus_coords[b1][1]],
            'sn': round(row['s_nom'], 1),
            'f': [round(float(f), 1) for f in flows.tolist()],
        })

    # ── HVDC Link data ───────────────────────────────────────────────────
    links_data = []
    for lid, row in n.links.iterrows():
        b0, b1 = row['bus0'], row['bus1']
        if b0 not in bus_coords or b1 not in bus_coords:
            continue
        p_nom = row['p_nom']
        flows = n.links_t.p0[lid].values if lid in n.links_t.p0.columns else np.zeros(12)
        loadings = (np.abs(flows) / p_nom * 100).tolist() if p_nom > 0 else [0.0] * 12
        links_data.append({
            'id': str(lid),
            'b0': [bus_coords[b0][0], bus_coords[b0][1]],
            'b1': [bus_coords[b1][0], bus_coords[b1][1]],
            'pn': round(p_nom, 1),
            'f': [round(float(f), 1) for f in flows.tolist()],
            'l': [round(float(l), 1) for l in loadings],
        })
    log.info(f"  HVDC links: {len(links_data)}")

    # ── Hourly dispatch by carrier ────────────────────────────────────────
    carriers = n.generators.carrier
    all_carriers = sorted(carriers.unique())
    dispatch_data = []
    for i, snap in enumerate(snapshots):
        hourly = {}
        for c in all_carriers:
            gids = carriers[carriers == c].index
            if len(gids) > 0:
                hourly[c] = round(float(gen_p.loc[snap, gids].sum()), 0)
        hourly['_load'] = round(float(n.loads_t.p_set.loc[snap].sum()), 0)
        hourly['_label'] = snapshots[i].strftime('%b %d, %H:%M')
        dispatch_data.append(hourly)

    snap_labels = [s.strftime('%b %d %H:%M') for s in snapshots]

    # ── Write HTML ────────────────────────────────────────────────────────
    html = _build_html(lines_data, trafos_data, links_data, bus_json, dispatch_data,
                       snap_labels, all_carriers)

    outfile = os.path.join(OUTDIR, 'powerflow_12month_map.html')
    with open(outfile, 'w') as f:
        f.write(html)
    log.info(f"Map saved to {outfile}")
    log.info(f"  {len(lines_data)} lines, {len(trafos_data)} trafos, {len(links_data)} links, {len(bus_json)} buses")


def _build_html(lines_data, trafos_data, links_data, bus_json, dispatch_data, snap_labels, carriers):
    """Build the full HTML string for the interactive map."""

    carrier_colors_js = json.dumps(CARRIER_COLORS)
    lines_js = json.dumps(lines_data)
    trafos_js = json.dumps(trafos_data)
    links_js = json.dumps(links_data)
    buses_js = json.dumps(bus_json)
    dispatch_js = json.dumps(dispatch_data)
    labels_js = json.dumps(snap_labels)

    domestic = [c for c in carriers if not c.startswith('import_')]
    imports = sorted([c for c in carriers if c.startswith('import_')])
    carrier_order = json.dumps(domestic + imports)

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>German Grid - 12 Month Power Flow</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', system-ui, sans-serif; background: #1a1a2e; color: #e0e0e0; }}
  #map {{ position: absolute; top: 0; left: 320px; right: 300px; bottom: 0; }}
  #sidebar {{ position: absolute; top: 0; left: 0; width: 320px; bottom: 0;
    background: #16213e; overflow-y: auto; padding: 15px; z-index: 1000; }}
  #right-panel {{ position: absolute; top: 0; right: 0; width: 300px; bottom: 0;
    background: #16213e; overflow-y: auto; padding: 15px; z-index: 1000;
    border-left: 1px solid #333; }}
  h2 {{ color: #e94560; margin-bottom: 10px; font-size: 16px; }}
  h3 {{ color: #aaa; margin: 12px 0 6px; font-size: 13px; text-transform: uppercase; letter-spacing: 1px; }}
  .month-btns {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; margin: 10px 0; }}
  .month-btn {{ padding: 8px 4px; border: 1px solid #333; background: #1a1a2e; color: #ccc;
    cursor: pointer; border-radius: 4px; font-size: 12px; text-align: center; transition: all 0.2s; }}
  .month-btn:hover {{ background: #2a2a4e; }}
  .month-btn.active {{ background: #e94560; color: white; border-color: #e94560; font-weight: bold; }}
  .stat {{ display: flex; justify-content: space-between; padding: 3px 0; font-size: 13px; border-bottom: 1px solid #1a1a2e; }}
  .stat .val {{ font-weight: bold; color: #fff; }}
  .carrier-row {{ display: flex; align-items: center; padding: 3px 0; font-size: 12px; }}
  .carrier-dot {{ width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; flex-shrink: 0; }}
  .carrier-name {{ flex: 1; }}
  .carrier-val {{ font-weight: bold; color: #fff; min-width: 55px; text-align: right; }}
  .carrier-bar {{ height: 6px; border-radius: 3px; margin-left: 4px; transition: width 0.3s; }}
  #dispatch-panel {{ background: #1a1a2e; border-radius: 6px; padding: 10px; margin: 8px 0; }}
  .legend {{ margin: 10px 0; }}
  .legend-item {{ display: flex; align-items: center; font-size: 11px; margin: 2px 0; }}
  .legend-color {{ width: 30px; height: 4px; margin-right: 6px; border-radius: 2px; }}
  .snap-info {{ font-size: 18px; color: #e94560; font-weight: bold; margin: 5px 0; }}
  .voltage-toggles {{ margin: 8px 0; }}
  .voltage-toggles label {{ display: inline-block; margin-right: 10px; font-size: 12px; cursor: pointer; }}
  .voltage-toggles input {{ margin-right: 3px; }}
  .summary-box {{ background: #1a1a2e; border-radius: 6px; padding: 8px 10px; margin: 6px 0; }}
  .toggle-btn {{ display: inline-block; padding: 6px 12px; border: 1px solid #444; background: #1a1a2e;
    color: #ccc; cursor: pointer; border-radius: 4px; font-size: 12px; margin: 4px 4px 4px 0; transition: all 0.2s; }}
  .toggle-btn:hover {{ background: #2a2a4e; }}
  .toggle-btn.active {{ background: #0f3460; color: #fff; border-color: #e94560; }}
  .top20-item {{ display: flex; align-items: center; padding: 5px 6px; margin: 2px 0;
    background: #1a1a2e; border-radius: 4px; font-size: 11px; cursor: pointer; transition: background 0.2s; }}
  .top20-item:hover {{ background: #2a2a4e; }}
  .top20-rank {{ width: 24px; font-weight: bold; color: #e94560; flex-shrink: 0; }}
  .top20-info {{ flex: 1; }}
  .top20-val {{ font-weight: bold; min-width: 50px; text-align: right; }}
  .top20-val.red {{ color: #e74c3c; }}
  .top20-val.pink {{ color: #ff69b4; }}
  .bg-btn {{ padding: 4px 8px; border: 1px solid #444; background: #1a1a2e; color: #ccc;
    cursor: pointer; border-radius: 3px; font-size: 11px; margin: 2px; transition: all 0.2s; }}
  .bg-btn:hover {{ background: #2a2a4e; }}
  .bg-btn.active {{ background: #0f3460; border-color: #e94560; color: #fff; }}
  .bg-selector {{ display: flex; flex-wrap: wrap; margin: 6px 0; }}
</style>
</head><body>

<div id="sidebar">
  <h2>German Transmission Grid</h2>
  <div style="font-size:11px;color:#888;">12 Monthly LOPF Snapshots &middot; grid_beta</div>

  <h3>Select Month</h3>
  <div class="month-btns" id="month-btns"></div>
  <div class="snap-info" id="snap-info"></div>

  <h3>System Balance</h3>
  <div class="summary-box">
    <div class="stat"><span>Total Load</span><span class="val" id="total-load">-</span></div>
    <div class="stat"><span>Total Generation</span><span class="val" id="total-gen">-</span></div>
    <div class="stat"><span>Renewable Share</span><span class="val" id="re-share">-</span></div>
    <div class="stat"><span>Max Line Loading</span><span class="val" id="max-loading">-</span></div>
    <div class="stat"><span>Lines &gt;100%</span><span class="val" id="overloaded">-</span></div>
  </div>

  <h3>Generation Dispatch</h3>
  <div id="dispatch-panel"></div>

  <h3>Map Background</h3>
  <div class="bg-selector" id="bg-selector">
    <div class="bg-btn active" onclick="setBg(0)">Dark</div>
    <div class="bg-btn" onclick="setBg(1)">Light</div>
    <div class="bg-btn" onclick="setBg(2)">Satellite</div>
    <div class="bg-btn" onclick="setBg(3)">Terrain</div>
  </div>

  <h3>Layers</h3>
  <div class="voltage-toggles">
    <label><input type="checkbox" id="v380" checked onchange="updateMap()"> 380 kV</label>
    <label><input type="checkbox" id="v220" checked onchange="updateMap()"> 220 kV</label>
    <label><input type="checkbox" id="v110" checked onchange="updateMap()"> 110 kV</label>
    <label><input type="checkbox" id="vTrafo" checked onchange="updateMap()"> Trafos</label>
    <label><input type="checkbox" id="vHVDC" checked onchange="updateMap()"> HVDC</label>
    <label><input type="checkbox" id="vBus" checked onchange="updateMap()"> Substations</label>
  </div>
  <div style="margin-top:6px;">
    <span class="toggle-btn" id="parBtn" onclick="toggleParallel()">Show Parallel Circuits</span>
  </div>

  <h3>Loading Legend</h3>
  <div class="legend">
    <div class="legend-item"><div class="legend-color" style="background:#2ecc71"></div> &lt;50%</div>
    <div class="legend-item"><div class="legend-color" style="background:#f1c40f"></div> 50-75%</div>
    <div class="legend-item"><div class="legend-color" style="background:#e67e22"></div> 75-100%</div>
    <div class="legend-item"><div class="legend-color" style="background:#e74c3c"></div> 100-200%</div>
    <div class="legend-item"><div class="legend-color" style="background:#ff69b4"></div> &gt;200%</div>
    <div class="legend-item"><div class="legend-color" style="background:#00e5ff;height:3px;border-top:1px dashed #00e5ff"></div> HVDC</div>
  </div>
</div>

<div id="map"></div>

<div id="right-panel">
  <h3 style="margin-top:0;">Top 20 Overloaded Lines</h3>
  <div id="top20-list" style="margin-top:8px;"></div>
</div>

<script>
const LINES = {lines_js};
const TRAFOS = {trafos_js};
const HVDC = {links_js};
const BUSES = {buses_js};
const DISPATCH = {dispatch_js};
const LABELS = {labels_js};
const CARRIER_COLORS = {carrier_colors_js};
const CARRIER_ORDER = {carrier_order};
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

let currentMonth = 0;
let lineLayer, trafoLayer, hvdcLayer, busLayer;
let showParallel = false;

// ── Tile layers ──────────────────────────────────────────────────────────
const tiles = [
  L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}@2x.png',
    {{attribution:'&copy; CartoDB &copy; OSM', maxZoom:18}}),
  L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}@2x.png',
    {{attribution:'&copy; CartoDB &copy; OSM', maxZoom:18}}),
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
    {{attribution:'&copy; Esri', maxZoom:18}}),
  L.tileLayer('https://{{s}}.tile.opentopomap.org/{{z}}/{{x}}/{{y}}.png',
    {{attribution:'&copy; OpenTopoMap', maxZoom:17}})
];
let currentTile = tiles[0];

const map = L.map('map', {{zoomControl: true}}).setView([51.2, 10.4], 6);
currentTile.addTo(map);

function setBg(idx) {{
  map.removeLayer(currentTile);
  currentTile = tiles[idx];
  currentTile.addTo(map);
  document.querySelectorAll('.bg-btn').forEach((b,i) => b.classList.toggle('active', i===idx));
}}

// ── Loading color ────────────────────────────────────────────────────────
function loadColor(pct) {{
  if (pct < 50) return '#2ecc71';
  if (pct < 75) return '#f1c40f';
  if (pct < 100) return '#e67e22';
  if (pct < 200) return '#e74c3c';
  return '#ff69b4';
}}

function lineWeight(v) {{
  if (v >= 380) return 2.5;
  if (v >= 220) return 1.8;
  return 1.0;
}}

// ── Parallel offset ──────────────────────────────────────────────────────
function offsetCoords(b0, b1, offset) {{
  const dx = b1[1] - b0[1];
  const dy = b1[0] - b0[0];
  const len = Math.sqrt(dx*dx + dy*dy);
  if (len < 1e-8) return [b0, b1];
  const px = -dy / len * offset;
  const py = dx / len * offset;
  return [[b0[0]+px, b0[1]+py], [b1[0]+px, b1[1]+py]];
}}

// ── Draw functions ───────────────────────────────────────────────────────
function drawLines() {{
  if (lineLayer) map.removeLayer(lineLayer);
  if (trafoLayer) map.removeLayer(trafoLayer);
  if (hvdcLayer) map.removeLayer(hvdcLayer);
  if (busLayer) map.removeLayer(busLayer);

  const show380 = document.getElementById('v380').checked;
  const show220 = document.getElementById('v220').checked;
  const show110 = document.getElementById('v110').checked;
  const showTrafo = document.getElementById('vTrafo').checked;
  const showBus = document.getElementById('vBus').checked;

  const h = currentMonth;
  const lineFeatures = [];
  let maxL = 0, countOver100 = 0;

  LINES.forEach(line => {{
    const v = line.v;
    if ((v >= 380 && !show380) || (v >= 220 && v < 380 && !show220) || (v < 220 && !show110)) return;

    const loading = line.l[h];
    const flow = line.f[h];
    if (loading > maxL) maxL = loading;
    if (loading > 100) countOver100++;

    let coords = [line.b0, line.b1];
    if (showParallel && line.ps > 1) {{
      const off = (line.pi - (line.ps - 1) / 2) * 0.004;
      coords = offsetCoords(line.b0, line.b1, off);
    }}

    const polyline = L.polyline(coords, {{
      color: loadColor(loading),
      weight: lineWeight(v),
      opacity: 0.85
    }});

    const parLabel = line.ps > 1 ? `<br>Parallel: ${{line.ps}} circuits` : '';
    polyline.bindTooltip(
      `<b>${{v}} kV Line #${{line.id}}</b><br>` +
      `Capacity: <b>${{line.sn}} MW</b><br>` +
      `Flow: ${{Math.abs(flow).toFixed(0)}} MW<br>` +
      `Loading: ${{loading.toFixed(1)}}%` + parLabel,
      {{sticky: true, className: 'dark-tooltip'}}
    );
    lineFeatures.push(polyline);
  }});

  lineLayer = L.layerGroup(lineFeatures).addTo(map);

  // Transformers
  if (showTrafo) {{
    const trafoFeatures = [];
    TRAFOS.forEach(t => {{
      const flow = t.f[h];
      const loading = t.sn > 0 ? Math.abs(flow) / t.sn * 100 : 0;
      const poly = L.polyline([t.b0, t.b1], {{
        color: loadColor(loading),
        weight: 3.5, opacity: 0.9, dashArray: '6,4'
      }});
      poly.bindTooltip(
        `<b>Transformer</b><br>Capacity: <b>${{t.sn}} MW</b><br>` +
        `Flow: ${{Math.abs(flow).toFixed(0)}} MW<br>Loading: ${{loading.toFixed(1)}}%`,
        {{sticky: true, className: 'dark-tooltip'}}
      );
      trafoFeatures.push(poly);
    }});
    trafoLayer = L.layerGroup(trafoFeatures).addTo(map);
  }}

  // HVDC links
  const showHVDC = document.getElementById('vHVDC').checked;
  if (showHVDC) {{
    const hvdcFeatures = [];
    HVDC.forEach(lnk => {{
      const flow = lnk.f[h];
      const loading = lnk.l[h];
      const poly = L.polyline([lnk.b0, lnk.b1], {{
        color: '#00e5ff', weight: 4, opacity: 0.9, dashArray: '10,6'
      }});
      const dir = flow >= 0 ? '&rarr;' : '&larr;';
      poly.bindTooltip(
        `<b>HVDC Link #${{lnk.id}}</b><br>` +
        `Capacity: <b>${{lnk.pn}} MW</b><br>` +
        `Flow: ${{Math.abs(flow).toFixed(0)}} MW ${{dir}}<br>` +
        `Loading: ${{loading.toFixed(1)}}%`,
        {{sticky: true, className: 'dark-tooltip'}}
      );
      hvdcFeatures.push(poly);
    }});
    hvdcLayer = L.layerGroup(hvdcFeatures).addTo(map);
  }}

  // Bus markers
  if (showBus) {{
    const busFeatures = [];
    const vColor = {{380: '#e94560', 220: '#3498db', 110: '#2ecc71'}};

    for (const [bid, b] of Object.entries(BUSES)) {{
      const r = (b.g || b.ld) ? 4 : 2;
      const marker = L.circleMarker([b.la, b.lo], {{
        radius: r, fillColor: vColor[b.v] || '#888',
        color: '#222', weight: 1, fillOpacity: 0.8
      }});

      // Build tooltip
      let tip = `<b>Bus ${{bid}}</b> (${{b.v}} kV)<br>`;
      if (b.g) {{
        let totalG = 0;
        for (const [c, vals] of Object.entries(b.g)) {{
          const mw = vals[h] || 0;
          if (Math.abs(mw) > 0.5) {{
            const color = CARRIER_COLORS[c] || '#888';
            tip += `<span style="color:${{color}}">&#9679;</span> ${{c}}: ${{mw.toFixed(1)}} MW<br>`;
          }}
          totalG += mw;
        }}
        tip += `<b>Total gen: ${{totalG.toFixed(0)}} MW</b><br>`;
      }}
      if (b.ld) {{
        const ld = b.ld[h] || 0;
        tip += `Load: <b>${{ld.toFixed(1)}} MW</b>`;
      }}
      if (!b.g && !b.ld) tip += '<i>Junction (no gen/load)</i>';

      marker.bindTooltip(tip, {{sticky: true, className: 'dark-tooltip'}});
      busFeatures.push(marker);
    }}
    busLayer = L.layerGroup(busFeatures).addTo(map);
  }}

  document.getElementById('max-loading').textContent = maxL.toFixed(0) + '%';
  document.getElementById('overloaded').textContent = countOver100;
}}

function updateDispatch() {{
  const h = currentMonth;
  const d = DISPATCH[h];
  const load = d._load || 0;

  let totalGen = 0, reGen = 0;
  const reCarriers = ['solar','onwind','offwind','run_of_river','reservoir'];
  CARRIER_ORDER.forEach(c => {{ if (d[c]) totalGen += d[c]; }});
  reCarriers.forEach(c => {{ if (d[c]) reGen += d[c]; }});

  document.getElementById('snap-info').textContent = d._label || LABELS[h];
  document.getElementById('total-load').textContent = (load/1000).toFixed(1) + ' GW';
  document.getElementById('total-gen').textContent = (totalGen/1000).toFixed(1) + ' GW';
  document.getElementById('re-share').textContent = totalGen > 0 ? (reGen/totalGen*100).toFixed(0) + '%' : '-';

  let html = '';
  CARRIER_ORDER.forEach(c => {{
    const val = d[c] || 0;
    if (Math.abs(val) < 1) return;
    const color = CARRIER_COLORS[c] || '#888';
    const pct = totalGen > 0 ? Math.abs(val) / totalGen * 100 : 0;
    html += `<div class="carrier-row">
      <div class="carrier-dot" style="background:${{color}}"></div>
      <span class="carrier-name">${{c}}</span>
      <span class="carrier-val">${{(val/1000).toFixed(1)}} GW</span>
      <div class="carrier-bar" style="background:${{color}};width:${{Math.min(pct*1.5,100)}}px"></div>
    </div>`;
  }});
  document.getElementById('dispatch-panel').innerHTML = html;
}}

function updateTop20() {{
  const h = currentMonth;
  const sorted = LINES.filter(l => l.l[h] > 0)
    .sort((a, b) => b.l[h] - a.l[h])
    .slice(0, 20);

  let html = '';
  sorted.forEach((line, i) => {{
    const pct = line.l[h];
    const cls = pct > 200 ? 'pink' : (pct > 100 ? 'red' : '');
    const par = line.ps > 1 ? ` (${{line.ps}}x)` : '';
    html += `<div class="top20-item" onclick="zoomToLine(${{LINES.indexOf(line)}})">
      <span class="top20-rank">#${{i+1}}</span>
      <span class="top20-info">${{line.v}}kV #${{line.id}}${{par}}<br>
        <span style="color:#888;font-size:10px">${{line.sn}} MW cap</span></span>
      <span class="top20-val ${{cls}}">${{pct.toFixed(0)}}%</span>
    </div>`;
  }});
  if (sorted.length === 0) html = '<div style="color:#888;font-size:12px;padding:10px;">No loaded lines</div>';
  document.getElementById('top20-list').innerHTML = html;
}}

function zoomToLine(idx) {{
  const line = LINES[idx];
  if (!line) return;
  const lat = (line.b0[0] + line.b1[0]) / 2;
  const lon = (line.b0[1] + line.b1[1]) / 2;
  map.setView([lat, lon], 10);
}}

function toggleParallel() {{
  showParallel = !showParallel;
  document.getElementById('parBtn').classList.toggle('active', showParallel);
  updateMap();
}}

function updateMap() {{
  drawLines();
  updateDispatch();
  updateTop20();
}}

function selectMonth(m) {{
  currentMonth = m;
  document.querySelectorAll('.month-btn').forEach((btn, i) => btn.classList.toggle('active', i === m));
  updateMap();
}}

// ── Month buttons ──────────────────────────────────────────────────────
const btnContainer = document.getElementById('month-btns');
MONTHS.forEach((name, i) => {{
  const btn = document.createElement('div');
  btn.className = 'month-btn' + (i === 0 ? ' active' : '');
  btn.textContent = name;
  btn.onclick = () => selectMonth(i);
  btnContainer.appendChild(btn);
}});

// ── Tooltip style ──────────────────────────────────────────────────────
const style = document.createElement('style');
style.textContent = `.dark-tooltip {{ background: rgba(22,33,62,0.95) !important;
  color: #e0e0e0 !important; border: 1px solid #333 !important;
  border-radius: 4px !important; font-size: 12px !important; padding: 6px 8px !important; }}
  .dark-tooltip .leaflet-tooltip-tip {{ border-top-color: rgba(22,33,62,0.95) !important; }}`;
document.head.appendChild(style);

// ── Initial render ─────────────────────────────────────────────────────
updateMap();
</script>
</body></html>"""


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    engine = create_engine(DB_URL)

    log.info("=" * 70)
    log.info("12-Month Power Flow - grid_beta")
    log.info("=" * 70)

    log.info("\nPicking random hours...")
    snapshots, hour_indices = pick_snapshots()

    log.info("\nBuilding network...")
    n = load_network(engine, SCN, snapshots, hour_indices)

    status, condition = run_lopf(n)
    if status != 'ok':
        log.error(f"LOPF failed: {status}/{condition}")
        sys.exit(1)

    analyze_results(n, snapshots)

    # Save network
    nc_file = os.path.join(OUTDIR, 'powerflow_12month.nc')
    n.export_to_netcdf(nc_file)
    log.info(f"\nNetwork saved to {nc_file}")

    create_map(n, snapshots, hour_indices)
    log.info("\nDone!")


if __name__ == '__main__':
    main()
