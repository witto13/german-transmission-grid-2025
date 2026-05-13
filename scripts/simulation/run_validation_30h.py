#!/usr/bin/env python3
"""
run_validation_30h.py - 30-hour validation LOPF with topology analysis.

Picks 4 specific weather scenarios + 26 random hours, cleans topology,
runs LOPF, and analyzes overloaded lines to find root causes.
"""

import json, sys, os, warnings, logging
import numpy as np
import pandas as pd
from datetime import datetime
from sqlalchemy import create_engine, text
from collections import defaultdict
import pypsa

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
log = logging.getLogger(__name__)

DB_URL = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
SCN = 'grid_beta'
YEAR = 2025
OUTDIR = '/root/egon_2025_project/results'
N_HOURS = 30

CARRIER_COLORS = {
    'solar': '#FFD700', 'onwind': '#4CAF50', 'offwind': '#00BCD4',
    'run_of_river': '#2196F3', 'reservoir': '#1565C0',
    'biogas': '#8BC34A', 'biomass': '#795548', 'waste': '#607D8B',
    'gas_ccgt': '#FF9800', 'gas_chp': '#FF5722', 'gas': '#FF9800',
    'coal': '#424242', 'lignite': '#6D4C41', 'oil': '#E91E63',
    'other': '#9E9E9E', 'hydrogen': '#00E5FF',
}


def pick_weather_hours():
    """Pick 4 specific weather + 26 random hours using SMARD profiles."""
    solar_path = '/root/egon_2025_project/data/profiles/solar_cf_2024.csv'
    wind_path = '/root/egon_2025_project/data/profiles/wind_onshore_cf_2024.csv'

    solar_cf = pd.read_csv(solar_path)['capacity_factor'].values
    wind_cf = pd.read_csv(wind_path)['capacity_factor'].values

    # Handle leap year (2024 has 8784h, we need 8760h for 2025)
    if len(solar_cf) == 8784:
        feb29 = 59 * 24
        solar_cf = np.delete(solar_cf, range(feb29, feb29 + 24))
        wind_cf = np.delete(wind_cf, range(feb29, feb29 + 24))

    # Find specific weather hours
    combined = solar_cf + wind_cf
    solar_only = solar_cf - wind_cf  # high when solar high, wind low
    wind_only = wind_cf - solar_cf   # high when wind high, solar low

    # 1) High PV + high wind (spring/autumn daytime with wind)
    both_high = np.argsort(combined)[::-1]
    # Pick first where both are individually > 0.3
    h_both = None
    for h in both_high:
        if solar_cf[h] > 0.3 and wind_cf[h] > 0.3:
            h_both = h; break
    if h_both is None:
        h_both = both_high[0]

    # 2) High wind only (winter night)
    wind_only_sorted = np.argsort(wind_only)[::-1]
    h_wind = None
    for h in wind_only_sorted:
        if wind_cf[h] > 0.5 and solar_cf[h] < 0.05:
            h_wind = h; break
    if h_wind is None:
        h_wind = wind_only_sorted[0]

    # 3) High PV only (summer midday, calm)
    solar_only_sorted = np.argsort(solar_only)[::-1]
    h_solar = None
    for h in solar_only_sorted:
        if solar_cf[h] > 0.5 and wind_cf[h] < 0.1:
            h_solar = h; break
    if h_solar is None:
        h_solar = solar_only_sorted[0]

    # 4) Low both (calm night)
    low_sorted = np.argsort(combined)
    h_low = None
    for h in low_sorted:
        if solar_cf[h] < 0.01 and wind_cf[h] < 0.05:
            h_low = h; break
    if h_low is None:
        h_low = low_sorted[0]

    specific = [h_both, h_wind, h_solar, h_low]
    labels = ['HIGH_PV+WIND', 'HIGH_WIND', 'HIGH_PV', 'LOW_BOTH']

    log.info("  Specific weather hours:")
    for i, (h, label) in enumerate(zip(specific, labels)):
        dt = pd.Timestamp(f'{YEAR}-01-01') + pd.Timedelta(hours=int(h))
        log.info(f"    {label}: hour {h} = {dt.strftime('%b %d %H:%M')} "
                 f"(solar={solar_cf[h]:.2f}, wind={wind_cf[h]:.2f})")

    # 26 random hours (avoid duplicates with specific)
    rng = np.random.RandomState(123)
    random_hours = []
    while len(random_hours) < N_HOURS - 4:
        h = int(rng.randint(0, 8760))
        if h not in specific and h not in random_hours:
            random_hours.append(h)

    all_hours = specific + sorted(random_hours)

    # Build snapshots
    snapshots = []
    hour_labels = []
    for i, h in enumerate(all_hours):
        dt = pd.Timestamp(f'{YEAR}-01-01') + pd.Timedelta(hours=int(h))
        snapshots.append(dt)
        if i < 4:
            hour_labels.append(labels[i])
        else:
            hour_labels.append(f'RND_{dt.strftime("%b%d_%H")}')

    return pd.DatetimeIndex(snapshots), all_hours, hour_labels


def load_network(engine, scn, snapshots, hour_indices):
    """Build PyPSA network with topology cleanup."""
    log.info("Loading components from database...")

    buses = pd.read_sql(f"SELECT * FROM grid.egon_etrago_bus WHERE scn_name='{scn}'", engine)
    lines = pd.read_sql(f"SELECT * FROM grid.egon_etrago_line WHERE scn_name='{scn}'", engine)
    trafos = pd.read_sql(f"SELECT * FROM grid.egon_etrago_transformer WHERE scn_name='{scn}'", engine)
    gens = pd.read_sql(f"SELECT * FROM grid.egon_etrago_generator WHERE scn_name='{scn}'", engine)
    loads = pd.read_sql(f"SELECT * FROM grid.egon_etrago_load WHERE scn_name='{scn}'", engine)
    links = pd.read_sql(f"SELECT * FROM grid.egon_etrago_link WHERE scn_name='{scn}'", engine)

    log.info(f"  Raw: {len(buses)} buses, {len(lines)} lines, {len(trafos)} trafos, "
             f"{len(gens)} gens, {len(loads)} loads, {len(links)} links")

    # ── Topology cleanup: prune dead-end buses with no gen/load ──────────
    gen_buses = set(gens['bus'].unique())
    load_buses = set(loads['bus'].unique())
    link_buses = set(links['bus0'].unique()) | set(links['bus1'].unique())
    has_component = gen_buses | load_buses | link_buses

    # Build adjacency from lines and trafos
    n_pruned = 0
    for iteration in range(10):  # iterative pruning
        adj = defaultdict(set)
        for _, row in lines.iterrows():
            adj[row['bus0']].add(row['bus1'])
            adj[row['bus1']].add(row['bus0'])
        for _, row in trafos.iterrows():
            adj[row['bus0']].add(row['bus1'])
            adj[row['bus1']].add(row['bus0'])

        # Find dead-end buses: degree=1, no gen/load/link
        dead_ends = set()
        for bid in buses['bus_id']:
            if len(adj[bid]) <= 1 and bid not in has_component:
                dead_ends.add(bid)

        if not dead_ends:
            break

        n_pruned += len(dead_ends)
        buses = buses[~buses['bus_id'].isin(dead_ends)]
        lines = lines[~lines['bus0'].isin(dead_ends) & ~lines['bus1'].isin(dead_ends)]
        trafos = trafos[~trafos['bus0'].isin(dead_ends) & ~trafos['bus1'].isin(dead_ends)]

    if n_pruned:
        log.info(f"  Pruned {n_pruned} dead-end buses (no gen/load, degree<=1)")

    log.info(f"  Clean: {len(buses)} buses, {len(lines)} lines, {len(trafos)} trafos")

    # ── Load timeseries ──────────────────────────────────────────────────
    nh = len(hour_indices)
    h_cols = ", ".join(f"p_set[{h+1}] as h{i}" for i, h in enumerate(hour_indices))
    load_ts_raw = pd.read_sql(f"""
        SELECT load_id, {h_cols}
        FROM grid.egon_etrago_load_timeseries WHERE scn_name='{scn}'
    """, engine)

    pmax_cols = ", ".join(f"p_max_pu[{h+1}] as pmax_{i}" for i, h in enumerate(hour_indices))
    pmin_cols = ", ".join(f"p_min_pu[{h+1}] as pmin_{i}" for i, h in enumerate(hour_indices))
    gen_ts_raw = pd.read_sql(f"""
        SELECT generator_id, {pmax_cols}, {pmin_cols}
        FROM grid.egon_etrago_generator_timeseries WHERE scn_name='{scn}'
    """, engine)

    log.info(f"  Timeseries: {len(load_ts_raw)} loads, {len(gen_ts_raw)} generators")

    # ── Build network ────────────────────────────────────────────────────
    n = pypsa.Network()
    n.set_snapshots(snapshots)

    buses_df = buses.set_index('bus_id')
    n.madd('Bus', buses_df.index.astype(str),
           v_nom=buses_df['v_nom'].values,
           x=buses_df['x'].values, y=buses_df['y'].values,
           carrier='AC', country=buses_df['country'].values)

    valid = lines[lines['x'] > 0].copy()
    log.info(f"  Lines with x>0: {len(valid)}/{len(lines)}")
    n.madd('Line', valid['line_id'].astype(str),
           bus0=valid['bus0'].astype(str).values,
           bus1=valid['bus1'].astype(str).values,
           x=valid['x'].values, r=valid['r'].values,
           s_nom=valid['s_nom'].values,
           length=valid['length'].values,
           v_nom=valid['v_nom'].values,
           s_max_pu=1e6)

    trafos = trafos.copy()
    bad_x = trafos['x'].abs() < 1e-6
    if bad_x.any():
        trafos.loc[bad_x, 'x'] = 0.10

    # Fix PST angles
    pst_mask = trafos['phase_shift'].abs() > 0.01
    if pst_mask.any():
        for idx in trafos[pst_mask].index:
            ps = trafos.loc[idx, 'phase_shift']
            if abs(ps) > 90:
                trafos.loc[idx, 'phase_shift'] = -25.0 if ps < 0 else 25.0
            elif abs(ps) > 45:
                trafos.loc[idx, 'phase_shift'] = 20.0 if ps > 0 else -20.0

    n.madd('Transformer', trafos['trafo_id'].astype(str),
           bus0=trafos['bus0'].astype(str).values,
           bus1=trafos['bus1'].astype(str).values,
           x=trafos['x'].values, r=trafos['r'].values,
           s_nom=trafos['s_nom'].values,
           tap_ratio=trafos['tap_ratio'].values,
           phase_shift=trafos['phase_shift'].values,
           s_max_pu=1e6)

    if len(links) > 0:
        n.madd('Link', links['link_id'].astype(str),
               bus0=links['bus0'].astype(str).values,
               bus1=links['bus1'].astype(str).values,
               p_nom=links['p_nom'].values,
               p_min_pu=links['p_min_pu'].values,
               efficiency=links['efficiency'].values,
               carrier=links['carrier'].values if 'carrier' in links.columns else 'DC')

    n.madd('Generator', gens['generator_id'].astype(str),
           bus=gens['bus'].astype(str).values,
           carrier=gens['carrier'].values,
           p_nom=gens['p_nom'].values,
           marginal_cost=gens['marginal_cost'].values,
           efficiency=gens['efficiency'].values,
           p_min_pu=gens['p_min_pu'].values,
           p_max_pu=gens['p_max_pu'].values)

    n.madd('Load', loads['load_id'].astype(str),
           bus=loads['bus'].astype(str).values,
           carrier=loads['carrier'].values if 'carrier' in loads.columns else 'AC',
           p_set=loads['p_set'].abs().values)

    # ── Apply timeseries ─────────────────────────────────────────────────
    log.info("Applying timeseries...")

    if len(load_ts_raw) > 0:
        load_ts_df = pd.DataFrame(index=snapshots)
        for _, row in load_ts_raw.iterrows():
            lid = str(int(row['load_id']))
            if lid in n.loads.index:
                vals = [row[f'h{i}'] for i in range(nh)]
                load_ts_df[lid] = vals
        for lid in n.loads.index:
            if lid not in load_ts_df.columns:
                load_ts_df[lid] = n.loads.loc[lid, 'p_set']
        n.loads_t.p_set = load_ts_df.abs()

    if len(gen_ts_raw) > 0:
        pmax_df = pd.DataFrame(index=snapshots)
        pmin_df = pd.DataFrame(index=snapshots)
        import_gids = set(n.generators[n.generators.carrier.str.startswith('import_')].index)

        for _, row in gen_ts_raw.iterrows():
            gid = str(int(row['generator_id']))
            if gid not in n.generators.index or gid in import_gids:
                continue

            pmax_vals = [row[f'pmax_{i}'] for i in range(nh)]
            if any(v is not None for v in pmax_vals):
                pmax_df[gid] = [v if v is not None else 1.0 for v in pmax_vals]

            pmin_vals = [row[f'pmin_{i}'] for i in range(nh)]
            if any(v is not None for v in pmin_vals):
                pmin_df[gid] = [v if v is not None else 0.0 for v in pmin_vals]

        if len(pmax_df.columns) > 0:
            n.generators_t.p_max_pu = pmax_df
            log.info(f"  p_max_pu: {len(pmax_df.columns)} generators")
        if len(pmin_df.columns) > 0:
            n.generators_t.p_min_pu = pmin_df
            log.info(f"  p_min_pu: {len(pmin_df.columns)} generators")

    # Remove isolated buses
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
    """Run LOPF."""
    log.info(f"\nRunning LOPF ({len(n.snapshots)} snapshots, CBC solver)...")
    status, condition = n.lopf(pyomo=False, solver_name='cbc')
    log.info(f"  Status: {status}, Condition: {condition}")

    if status != 'ok':
        log.warning("LOPF failed, retrying with relaxed p_min_pu...")
        n.generators.p_min_pu = 0.0
        if len(n.generators_t.p_min_pu) > 0:
            n.generators_t.p_min_pu = pd.DataFrame(index=n.snapshots)
        status, condition = n.lopf(pyomo=False, solver_name='cbc')
        log.info(f"  Retry: {status}, {condition}")

    return status, condition


def analyze_results(n, snapshots, hour_labels):
    """Comprehensive analysis of power flow results."""
    log.info("\n" + "=" * 80)
    log.info("VALIDATION RESULTS — 30 SCENARIOS")
    log.info("=" * 80)

    gen_p = n.generators_t.p
    carriers = n.generators.carrier
    nh = len(snapshots)

    # ── 1. Dispatch summary ──────────────────────────────────────────────
    log.info(f"\n{'#':<3} {'Label':<16} {'Load':>6} {'Solar':>6} {'Wind':>6} "
             f"{'Bio':>5} {'Gas':>5} {'Coal':>5} {'Lign':>5} {'Imp':>5} {'Curt':>4}")
    log.info("-" * 85)

    re_carriers = ['solar', 'onwind', 'offwind']
    import_carriers = [c for c in carriers.unique() if c.startswith('import_')]

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
        re_ids = carriers[carriers.isin(re_carriers)].index
        re_in_ts = [g for g in re_ids if g in n.generators_t.p_max_pu.columns]
        if re_in_ts:
            pot = (n.generators_t.p_max_pu.loc[snap, re_in_ts] *
                   n.generators.loc[re_in_ts, 'p_nom']).sum() / 1e3
            act = gen_p.loc[snap, re_in_ts].sum() / 1e3
            curt = max(0, (pot - act) / pot * 100) if pot > 0 else 0
        else:
            curt = 0

        label = hour_labels[i][:16]
        log.info(f"{i:<3} {label:<16} {load_h:>6.1f} {solar_h:>6.1f} {wind_h:>6.1f} "
                 f"{bio_h:>5.1f} {gas_h:>5.1f} {coal_h:>5.1f} {lign_h:>5.1f} "
                 f"{imp_h:>5.1f} {curt:>3.0f}%")

    # ── 2. Generator constraint violations ───────────────────────────────
    log.info("\n--- GENERATOR CONSTRAINT VIOLATIONS ---")
    violations = []
    for gid in n.generators.index:
        pnom = n.generators.loc[gid, 'p_nom']
        p_min_static = n.generators.loc[gid, 'p_min_pu']
        p_max_static = n.generators.loc[gid, 'p_max_pu']

        for si, snap in enumerate(snapshots):
            dispatch = gen_p.loc[snap, gid]

            # Get effective bounds
            if gid in n.generators_t.p_max_pu.columns:
                p_max_pu = n.generators_t.p_max_pu.loc[snap, gid]
            else:
                p_max_pu = p_max_static

            if gid in n.generators_t.p_min_pu.columns:
                p_min_pu = n.generators_t.p_min_pu.loc[snap, gid]
            else:
                p_min_pu = p_min_static

            p_min = p_min_pu * pnom
            p_max = p_max_pu * pnom

            if dispatch < p_min - 1.0 or dispatch > p_max + 1.0:
                violations.append({
                    'gid': gid, 'snap': si, 'carrier': carriers[gid],
                    'bus': n.generators.loc[gid, 'bus'],
                    'p_nom': pnom, 'dispatch': dispatch,
                    'p_min': p_min, 'p_max': p_max,
                    'violation_mw': min(dispatch - p_min, 0) + max(dispatch - p_max, 0)
                })

    if violations:
        vdf = pd.DataFrame(violations).sort_values('violation_mw')
        log.info(f"  Found {len(violations)} constraint violations!")
        log.info(f"  Top 10 worst:")
        for _, v in vdf.head(10).iterrows():
            log.info(f"    Gen {v['gid']} ({v['carrier']}, {v['p_nom']:.1f} MW, bus {v['bus']}): "
                     f"dispatch={v['dispatch']:.0f} MW, bounds=[{v['p_min']:.0f}, {v['p_max']:.0f}] MW "
                     f"(snap {v['snap']}: {hour_labels[v['snap']]})")
    else:
        log.info("  No constraint violations found!")

    # ── 3. Line loading analysis ─────────────────────────────────────────
    log.info("\n--- LINE LOADING ANALYSIS ---")

    if len(n.lines_t.p0) == 0:
        log.info("  No line flow data!")
        return

    # Compute loading per line per snapshot
    s_nom = n.lines.s_nom
    p0 = n.lines_t.p0
    loading = p0.abs().div(s_nom, axis=1) * 100

    max_loading = loading.max()  # max across all snapshots per line
    max_snap_idx = loading.idxmax()  # which snapshot has max loading

    # Bins
    bins = [0, 50, 75, 100, 130, 200, 500, 1000, float('inf')]
    labels_bins = ['<50%', '50-75%', '75-100%', '100-130%', '130-200%', '200-500%', '500-1000%', '>1000%']
    counts = pd.cut(max_loading, bins=bins, labels=labels_bins).value_counts().sort_index()

    log.info("  Max loading distribution (across all 30 snapshots):")
    for label, count in counts.items():
        if count > 0:
            log.info(f"    {label}: {count} lines")

    # ── 4. Deep dive on overloaded lines (>200%) ─────────────────────────
    log.info("\n--- DEEP DIVE: LINES >200% LOADING ---")

    overloaded_200 = max_loading[max_loading > 200].sort_values(ascending=False)
    if len(overloaded_200) == 0:
        log.info("  No lines exceed 200% loading!")
    else:
        log.info(f"  {len(overloaded_200)} lines exceed 200% loading\n")

        # Build bus degree map
        bus_degree = defaultdict(int)
        for lid in n.lines.index:
            bus_degree[n.lines.loc[lid, 'bus0']] += 1
            bus_degree[n.lines.loc[lid, 'bus1']] += 1
        for tid in n.transformers.index:
            bus_degree[n.transformers.loc[tid, 'bus0']] += 1
            bus_degree[n.transformers.loc[tid, 'bus1']] += 1

        # Check each overloaded line
        reasons = defaultdict(list)
        for lid in overloaded_200.head(50).index:
            line = n.lines.loc[lid]
            v = int(line['v_nom'])
            sn = line['s_nom']
            b0, b1 = line['bus0'], line['bus1']
            max_l = max_loading[lid]
            max_flow = p0[lid].abs().max()

            worst_snap = max_snap_idx[lid]
            worst_snap_i = list(snapshots).index(worst_snap)

            # Check if endpoints are stubs
            deg0 = bus_degree[b0]
            deg1 = bus_degree[b1]

            # Gen/load at endpoints
            gen_at_b0 = n.generators[n.generators.bus == b0]['p_nom'].sum()
            gen_at_b1 = n.generators[n.generators.bus == b1]['p_nom'].sum()
            load_at_b0 = n.loads[n.loads.bus == b0].index
            load_at_b1 = n.loads[n.loads.bus == b1].index

            # Check if this line has parallel circuits
            key = tuple(sorted([b0, b1]))
            n_par = len(n.lines[(n.lines.bus0.isin([b0, b1])) & (n.lines.bus1.isin([b0, b1]))])

            # Classify reason
            reason = []
            if deg0 == 1 or deg1 == 1:
                stub_bus = b0 if deg0 == 1 else b1
                stub_gen = gen_at_b0 if deg0 == 1 else gen_at_b1
                reason.append(f"STUB_END (bus {stub_bus}, deg={min(deg0,deg1)}, "
                              f"gen={stub_gen:.0f}MW)")

            if sn < 300 and v >= 110:
                reason.append(f"LOW_CAPACITY ({sn:.0f} MW for {v}kV)")

            if max_flow > sn * 5:
                reason.append(f"SOLVER_DEGENERACY (flow {max_flow:.0f} >> s_nom {sn:.0f})")

            if not reason:
                reason.append(f"BOTTLENECK (corridor capacity insufficient)")

            reasons['; '.join(reason)].append(lid)

            if lid in overloaded_200.head(20).index:
                log.info(f"  Line {lid} ({v}kV, s_nom={sn:.0f}MW, par={n_par}): "
                         f"max {max_l:.0f}% ({max_flow:.0f}MW)")
                log.info(f"    bus0={b0} (deg={deg0}, gen={gen_at_b0:.0f}MW) → "
                         f"bus1={b1} (deg={deg1}, gen={gen_at_b1:.0f}MW)")
                log.info(f"    Worst at: {hour_labels[worst_snap_i]}")
                log.info(f"    Reason: {'; '.join(reason)}")

        # Summary by reason
        log.info(f"\n  Overload reasons summary:")
        for reason, lids in sorted(reasons.items(), key=lambda x: -len(x[1])):
            log.info(f"    {reason}: {len(lids)} lines")

    # ── 5. Stub/dead-end topology analysis ───────────────────────────────
    log.info("\n--- 110kV STUB TOPOLOGY ANALYSIS ---")

    buses_110 = n.buses[n.buses.v_nom == 110].index
    stub_110 = [b for b in buses_110 if bus_degree.get(b, 0) == 1]
    gen_buses_set = set(n.generators.bus)
    load_buses_set = set(n.loads.bus)

    stub_no_gen = [b for b in stub_110 if b not in gen_buses_set]
    stub_no_load = [b for b in stub_110 if b not in load_buses_set]
    stub_nothing = [b for b in stub_110 if b not in gen_buses_set and b not in load_buses_set]
    stub_gen_only = [b for b in stub_110 if b in gen_buses_set and b not in load_buses_set]

    log.info(f"  110kV buses: {len(buses_110)}")
    log.info(f"  110kV stubs (degree=1): {len(stub_110)}")
    log.info(f"    - with gen + load: {len(stub_110) - len(stub_no_gen) - len(stub_gen_only)}")
    log.info(f"    - gen only (no load): {len(stub_gen_only)}")
    log.info(f"    - no gen, no load: {len(stub_nothing)}")

    # Check stubs with large generation but small connecting line
    log.info(f"\n  Stubs with gen > connecting line capacity:")
    for b in stub_110:
        if b not in gen_buses_set:
            continue
        gen_cap = n.generators[n.generators.bus == b]['p_nom'].sum()
        # Find connecting line
        connecting = n.lines[(n.lines.bus0 == b) | (n.lines.bus1 == b)]
        if len(connecting) == 0:
            continue
        total_line_cap = connecting['s_nom'].sum()
        if gen_cap > total_line_cap * 1.5:
            # Get dominant carrier
            bus_gens = n.generators[n.generators.bus == b]
            dom_carrier = bus_gens.groupby('carrier')['p_nom'].sum().idxmax()
            log.info(f"    Bus {b}: gen={gen_cap:.0f}MW ({dom_carrier}) on "
                     f"{total_line_cap:.0f}MW line capacity "
                     f"({gen_cap/total_line_cap:.1f}x oversubscribed)")

    # ── 6. Per-snapshot loading summary ──────────────────────────────────
    log.info("\n--- PER-SNAPSHOT LOADING SUMMARY ---")
    log.info(f"{'#':<3} {'Label':<16} {'Mean%':>6} {'Max%':>7} {'>100%':>6} {'>200%':>6} {'>500%':>6}")
    log.info("-" * 55)

    for i, snap in enumerate(snapshots):
        snap_loading = loading.loc[snap]
        mean_l = snap_loading.mean()
        max_l = snap_loading.max()
        over100 = (snap_loading > 100).sum()
        over200 = (snap_loading > 200).sum()
        over500 = (snap_loading > 500).sum()

        label = hour_labels[i][:16]
        log.info(f"{i:<3} {label:<16} {mean_l:>6.1f} {max_l:>7.0f} {over100:>6} {over200:>6} {over500:>6}")


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    engine = create_engine(DB_URL)

    log.info("=" * 80)
    log.info("30-HOUR VALIDATION RUN")
    log.info("=" * 80)

    log.info("\nPicking hours...")
    snapshots, hour_indices, hour_labels = pick_weather_hours()

    log.info(f"\n  Total: {len(snapshots)} snapshots")

    log.info("\nBuilding network...")
    n = load_network(engine, SCN, snapshots, hour_indices)

    status, condition = run_lopf(n)
    if status != 'ok':
        log.error(f"LOPF failed: {status}/{condition}")
        sys.exit(1)

    analyze_results(n, snapshots, hour_labels)

    # Save network
    nc_file = os.path.join(OUTDIR, 'validation_30h.nc')
    n.export_to_netcdf(nc_file)
    log.info(f"\nNetwork saved to {nc_file}")
    log.info("\nDone!")


if __name__ == '__main__':
    main()
