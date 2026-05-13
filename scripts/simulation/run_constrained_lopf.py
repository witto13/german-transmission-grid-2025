#!/usr/bin/env python3
"""
run_constrained_lopf.py  -  Constrained LOPF with stub fixes, 12 random hours.

Changes from the unconstrained 12-month run:
  1. Fix 4 oversubscribed stub buses (move excess gen to neighbours)
  2. Prune dead-end buses (degree ≤ 1, no gen/load)
  3. Line & transformer capacity constraints  (s_max_pu = 1.0)
  4. 12 random hours instead of monthly snapshots

Usage:
    conda activate egon2025
    python scripts/run_constrained_lopf.py
"""

import json, sys, os
import numpy as np
import pandas as pd
from datetime import datetime
from collections import defaultdict
from sqlalchemy import create_engine
import pypsa, warnings, logging

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
log = logging.getLogger(__name__)

DB_URL  = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
SCN     = 'grid_beta'
YEAR    = 2025
NH      = 12           # number of hours
OUTDIR  = '/root/egon_2025_project/results'

# ── Oversubscribed stubs: {stub_bus_id: (neighbour_bus_id, line_s_nom)} ────
STUB_FIXES = {
    40330: (35268, 280.0),   # Cologne area – 881 MW gen on 280 MW line
    40118: (40119, 280.0),   # Düsseldorf area – 833 MW gen on 280 MW line
    39070: (38554, 280.0),   # Schleswig-Holstein – 443 MW gen on 280 MW line
    36206: (35967, 280.0),   # Berlin – 458 MW gen on 280 MW line
}

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

# ──────────────────────────────────────────────────────────────────────────────
#  HOUR SELECTION
# ──────────────────────────────────────────────────────────────────────────────
def pick_hours(seed=77):
    """Pick 12 diverse random hours across 2025."""
    rng = np.random.RandomState(seed)
    hours = sorted(rng.choice(8760, size=NH, replace=False))

    snapshots = []
    for h in hours:
        dt = pd.Timestamp(f'{YEAR}-01-01') + pd.Timedelta(hours=int(h))
        snapshots.append(dt)
        log.info(f"  h={h:>5d}  {dt.strftime('%Y-%m-%d %H:%M')}")

    return pd.DatetimeIndex(snapshots), [int(h) for h in hours]


# ──────────────────────────────────────────────────────────────────────────────
#  STUB FIX
# ──────────────────────────────────────────────────────────────────────────────
def fix_stubs(gens):
    """Move excess generators from oversubscribed stub buses to neighbours.

    For each stub, generators are moved (largest first) until remaining
    generation capacity at the stub ≤ connecting line s_nom.
    """
    total_moved_mw = 0
    total_moved_n = 0

    for stub_bus, (nbr_bus, line_cap) in STUB_FIXES.items():
        at_stub = gens[gens['bus'] == stub_bus].copy()
        if at_stub.empty:
            continue

        total_gen = at_stub['p_nom'].sum()
        if total_gen <= line_cap:
            log.info(f"  Stub {stub_bus}: {total_gen:.0f} MW ≤ {line_cap:.0f} MW line → no fix needed")
            continue

        excess = total_gen - line_cap
        log.info(f"  Stub {stub_bus}: {total_gen:.0f} MW gen on {line_cap:.0f} MW line → "
                 f"moving ~{excess:.0f} MW to bus {nbr_bus}")

        # Sort by p_nom descending – move big plants first
        sorted_gens = at_stub.sort_values('p_nom', ascending=False)
        remaining = total_gen

        for idx, row in sorted_gens.iterrows():
            if remaining <= line_cap:
                break
            # Move this generator to neighbour
            gens.loc[idx, 'bus'] = nbr_bus
            remaining -= row['p_nom']
            total_moved_mw += row['p_nom']
            total_moved_n += 1
            log.info(f"    Moved gen {row['generator_id']} ({row['carrier']}, "
                     f"{row['p_nom']:.1f} MW) → bus {nbr_bus}")

        log.info(f"    Remaining at stub: {remaining:.0f} MW")

    log.info(f"  Total: {total_moved_n} generators ({total_moved_mw:.0f} MW) reassigned")
    return gens


# ──────────────────────────────────────────────────────────────────────────────
#  DEAD-END PRUNING
# ──────────────────────────────────────────────────────────────────────────────
def prune_dead_ends(buses, lines, trafos, gens, loads, links):
    """Iteratively remove degree-≤1 buses with no gen/load/link."""
    has_component = set()
    has_component.update(gens['bus'].unique())
    has_component.update(loads['bus'].unique())
    has_component.update(links['bus0'].unique())
    has_component.update(links['bus1'].unique())

    n_pruned = 0
    for iteration in range(10):
        adj = defaultdict(set)
        for _, row in lines.iterrows():
            adj[row['bus0']].add(row['bus1'])
            adj[row['bus1']].add(row['bus0'])
        for _, row in trafos.iterrows():
            adj[row['bus0']].add(row['bus1'])
            adj[row['bus1']].add(row['bus0'])

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
        log.info(f"  Pruned {n_pruned} dead-end buses ({iteration+1} iterations)")
    return buses, lines, trafos


# ──────────────────────────────────────────────────────────────────────────────
#  NETWORK LOADING
# ──────────────────────────────────────────────────────────────────────────────
def load_network(engine, scn, snapshots, hour_indices):
    """Build PyPSA network from DB, fix stubs, prune, and constrain."""
    log.info("Loading components from database...")

    buses  = pd.read_sql(f"SELECT * FROM grid.egon_etrago_bus WHERE scn_name='{scn}'", engine)
    lines  = pd.read_sql(f"SELECT * FROM grid.egon_etrago_line WHERE scn_name='{scn}'", engine)
    trafos = pd.read_sql(f"SELECT * FROM grid.egon_etrago_transformer WHERE scn_name='{scn}'", engine)
    gens   = pd.read_sql(f"SELECT * FROM grid.egon_etrago_generator WHERE scn_name='{scn}'", engine)
    loads  = pd.read_sql(f"SELECT * FROM grid.egon_etrago_load WHERE scn_name='{scn}'", engine)
    links  = pd.read_sql(f"SELECT * FROM grid.egon_etrago_link WHERE scn_name='{scn}'", engine)

    log.info(f"  {len(buses)} buses, {len(lines)} lines, {len(trafos)} trafos, "
             f"{len(gens)} gens, {len(loads)} loads, {len(links)} links")

    # ── Fix oversubscribed stubs ─────────────────────────────────────────
    log.info("Fixing oversubscribed stubs...")
    gens = fix_stubs(gens)

    # ── Prune dead-end buses ─────────────────────────────────────────────
    log.info("Pruning dead-end buses...")
    buses, lines, trafos = prune_dead_ends(buses, lines, trafos, gens, loads, links)

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

    # ── Build PyPSA network ──────────────────────────────────────────────
    n = pypsa.Network()
    n.set_snapshots(snapshots)

    # Buses
    buses = buses.set_index('bus_id')
    n.madd('Bus', buses.index.astype(str),
           v_nom=buses['v_nom'].values,
           x=buses['x'].values, y=buses['y'].values,
           carrier='AC', country=buses['country'].values)

    # Lines – CONSTRAINED (s_max_pu = 1.0)
    valid = lines[lines['x'] > 0].copy()
    log.info(f"  Lines with x>0: {len(valid)}/{len(lines)}")
    n.madd('Line', valid['line_id'].astype(str),
           bus0=valid['bus0'].astype(str).values,
           bus1=valid['bus1'].astype(str).values,
           x=valid['x'].values, r=valid['r'].values,
           s_nom=valid['s_nom'].values,
           length=valid['length'].values,
           v_nom=valid['v_nom'].values,
           s_max_pu=1.0)

    # Transformers – CONSTRAINED (s_max_pu = 1.0)
    trafos = trafos.copy()
    bad_x = trafos['x'].abs() < 1e-6
    if bad_x.any():
        trafos.loc[bad_x, 'x'] = 0.10
        log.warning(f"  Fixed {bad_x.sum()} trafos with near-zero x")

    # PST phase-shift angle correction
    pst_mask = trafos['phase_shift'].abs() > 0.01
    if pst_mask.any():
        for idx in trafos[pst_mask].index:
            ps = trafos.loc[idx, 'phase_shift']
            if abs(ps) > 90:
                trafos.loc[idx, 'phase_shift'] = -25.0 if ps < 0 else 25.0
            elif abs(ps) > 45:
                trafos.loc[idx, 'phase_shift'] = 20.0 if ps > 0 else -20.0
        log.info(f"  Corrected {pst_mask.sum()} PST phase-shift angles")

    n.madd('Transformer', trafos['trafo_id'].astype(str),
           bus0=trafos['bus0'].astype(str).values,
           bus1=trafos['bus1'].astype(str).values,
           x=trafos['x'].values, r=trafos['r'].values,
           s_nom=trafos['s_nom'].values,
           tap_ratio=trafos['tap_ratio'].values,
           phase_shift=trafos['phase_shift'].values,
           s_max_pu=1.0)

    # HVDC Links
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

    # ── Apply timeseries ─────────────────────────────────────────────────
    log.info("Applying timeseries...")

    # Load timeseries
    if len(load_ts_raw) > 0:
        load_ts_df = pd.DataFrame(index=snapshots)
        for _, row in load_ts_raw.iterrows():
            lid = str(int(row['load_id']))
            if lid in n.loads.index:
                load_ts_df[lid] = [row[f'h{i}'] for i in range(nh)]
        for lid in n.loads.index:
            if lid not in load_ts_df.columns:
                load_ts_df[lid] = n.loads.loc[lid, 'p_set']
        n.loads_t.p_set = load_ts_df.abs()

    # Generator timeseries – skip import generators (erroneous DB entries)
    if len(gen_ts_raw) > 0:
        pmax_df = pd.DataFrame(index=snapshots)
        pmin_df = pd.DataFrame(index=snapshots)

        import_gids = set(n.generators[n.generators.carrier.str.startswith('import_')].index)
        skipped = 0

        for _, row in gen_ts_raw.iterrows():
            gid = str(int(row['generator_id']))
            if gid not in n.generators.index:
                continue
            if gid in import_gids:
                skipped += 1
                continue

            pmax_vals = [row[f'pmax_{i}'] for i in range(nh)]
            if any(v is not None for v in pmax_vals):
                pmax_df[gid] = [v if v is not None else 1.0 for v in pmax_vals]

            pmin_vals = [row[f'pmin_{i}'] for i in range(nh)]
            if any(v is not None for v in pmin_vals):
                pmin_df[gid] = [v if v is not None else 0.0 for v in pmin_vals]

        if skipped:
            log.info(f"  Skipped {skipped} import gen timeseries")
        if len(pmax_df.columns) > 0:
            n.generators_t.p_max_pu = pmax_df
            log.info(f"  p_max_pu: {len(pmax_df.columns)} generators")
        if len(pmin_df.columns) > 0:
            # CRITICAL: NaN p_min_pu → PyPSA treats as unconstrained lower bound
            # (allows generators to absorb power = negative dispatch).
            # All renewables have NaN in DB → fill with 0.0
            nan_before = pmin_df.isna().sum().sum()
            pmin_df = pmin_df.fillna(0.0)
            if nan_before > 0:
                log.info(f"  Fixed {nan_before} NaN p_min_pu values → 0.0")
            n.generators_t.p_min_pu = pmin_df
            log.info(f"  p_min_pu: {len(pmin_df.columns)} generators")

    # ── Remove isolated buses ────────────────────────────────────────────
    connected = set()
    for attr in ('bus0', 'bus1'):
        for comp in (n.lines, n.transformers, n.links):
            connected.update(comp[attr])
    connected.update(n.generators.bus)
    connected.update(n.loads.bus)

    isolated = n.buses.index.difference(pd.Index(list(connected)))
    if len(isolated) > 0:
        log.info(f"  Removing {len(isolated)} isolated buses")
        n.mremove('Bus', isolated)

    log.info(f"Network ready: {len(n.buses)} buses, {len(n.lines)} lines, "
             f"{len(n.transformers)} trafos, {len(n.generators)} gens, "
             f"{len(n.loads)} loads, {len(n.links)} links")

    return n


# ──────────────────────────────────────────────────────────────────────────────
#  LOPF
# ──────────────────────────────────────────────────────────────────────────────
def run_lopf(n):
    """Run constrained LOPF with CBC solver."""
    log.info(f"\nRunning CONSTRAINED LOPF ({NH} snapshots, CBC)...")
    log.info(f"  s_max_pu: lines={n.lines.s_max_pu.unique()}, "
             f"trafos={n.transformers.s_max_pu.unique()}")

    status, condition = n.lopf(pyomo=False, solver_name='cbc')
    log.info(f"  Status: {status}, Condition: {condition}")

    if status != 'ok':
        log.warning("LOPF failed – relaxing p_min_pu and retrying...")
        n.generators.p_min_pu = 0.0
        if len(n.generators_t.p_min_pu) > 0:
            n.generators_t.p_min_pu = pd.DataFrame(index=n.snapshots)
        status, condition = n.lopf(pyomo=False, solver_name='cbc')
        log.info(f"  Retry: {status}, {condition}")

    return status, condition


# ──────────────────────────────────────────────────────────────────────────────
#  RESULTS ANALYSIS
# ──────────────────────────────────────────────────────────────────────────────
def analyze_results(n, snapshots, hour_labels):
    """Print dispatch and loading summary."""
    log.info("\n" + "=" * 80)
    log.info("RESULTS – CONSTRAINED LOPF")
    log.info("=" * 80)

    gen_p = n.generators_t.p
    carriers = n.generators.carrier

    re_carriers  = ['solar', 'onwind', 'offwind', 'run_of_river', 'reservoir']
    bio_carriers = ['biogas', 'biomass', 'waste']
    gas_carriers = ['gas_ccgt', 'gas_chp', 'gas']
    imp_carriers = [c for c in carriers.unique() if c.startswith('import_')]

    log.info(f"\n{'Hour':<22} {'Load':>7} {'Solar':>7} {'Wind':>7} "
             f"{'Bio':>6} {'Gas':>7} {'Coal':>6} {'Lign':>6} {'Imp':>6} {'Curt%':>5}")
    log.info("-" * 90)

    for i, snap in enumerate(snapshots):
        load_h  = n.loads_t.p_set.loc[snap].sum() / 1e3
        solar_h = gen_p.loc[snap, carriers == 'solar'].sum() / 1e3
        wind_h  = gen_p.loc[snap, carriers.isin(['onwind', 'offwind'])].sum() / 1e3
        bio_h   = gen_p.loc[snap, carriers.isin(bio_carriers)].sum() / 1e3
        gas_h   = gen_p.loc[snap, carriers.isin(gas_carriers)].sum() / 1e3
        coal_h  = gen_p.loc[snap, carriers == 'coal'].sum() / 1e3
        lign_h  = gen_p.loc[snap, carriers == 'lignite'].sum() / 1e3
        imp_h   = gen_p.loc[snap, carriers.isin(imp_carriers)].sum() / 1e3

        # Curtailment
        curt = 0.0
        re_ids = carriers[carriers.isin(re_carriers)].index
        if len(re_ids) > 0 and len(n.generators_t.p_max_pu) > 0:
            re_ts = [g for g in re_ids if g in n.generators_t.p_max_pu.columns]
            if re_ts:
                pot = (n.generators_t.p_max_pu.loc[snap, re_ts] *
                       n.generators.loc[re_ts, 'p_nom']).sum() / 1e3
                act = gen_p.loc[snap, re_ts].sum() / 1e3
                curt = max(0, (pot - act) / pot * 100) if pot > 0 else 0

        log.info(f"{hour_labels[i]:<22} {load_h:>7.1f} {solar_h:>7.1f} {wind_h:>7.1f} "
                 f"{bio_h:>6.1f} {gas_h:>7.1f} {coal_h:>6.1f} {lign_h:>6.1f} "
                 f"{imp_h:>6.1f} {curt:>4.0f}%")

    # Line loading stats
    if len(n.lines_t.p0) > 0:
        log.info(f"\nLine loading (% of s_nom):")
        for i, snap in enumerate(snapshots):
            loading = n.lines_t.p0.loc[snap].abs() / n.lines.s_nom * 100
            over100 = (loading > 100).sum()
            over50  = (loading > 50).sum()
            log.info(f"  {hour_labels[i]:<20s}: mean={loading.mean():.1f}%, "
                     f"max={loading.max():.1f}%, >50%: {over50}, >100%: {over100}")

    # Transformer loading
    if len(n.transformers_t.p0) > 0:
        log.info(f"\nTransformer loading:")
        for i, snap in enumerate(snapshots):
            loading = n.transformers_t.p0.loc[snap].abs() / n.transformers.s_nom * 100
            over100 = (loading > 100).sum()
            log.info(f"  {hour_labels[i]:<20s}: mean={loading.mean():.1f}%, "
                     f"max={loading.max():.1f}%, >100%: {over100}")


# ──────────────────────────────────────────────────────────────────────────────
#  MAP CREATION
# ──────────────────────────────────────────────────────────────────────────────
def create_map(n, snapshots, hour_indices):
    """Create interactive HTML map."""
    log.info("\nCreating interactive map...")
    nh = len(snapshots)

    # ── Bus coordinates ──────────────────────────────────────────────────
    bus_coords = {}
    for bid, row in n.buses.iterrows():
        bus_coords[bid] = (round(row['y'], 5), round(row['x'], 5), int(row['v_nom']))

    # ── Per-bus generation by carrier per hour ───────────────────────────
    gen_p = n.generators_t.p
    gen_info = n.generators[['bus', 'carrier']].copy()
    hcols = [f'h{i}' for i in range(nh)]
    for i, snap in enumerate(snapshots):
        gen_info[hcols[i]] = gen_p.loc[snap].reindex(gen_info.index, fill_value=0.0).values

    bus_gen = gen_info.groupby(['bus', 'carrier'])[hcols].sum()
    bus_gen_dict = defaultdict(dict)
    for (bus, carrier), row in bus_gen.iterrows():
        vals = [round(float(v), 1) for v in row.values]
        if any(abs(v) > 0.5 for v in vals):
            bus_gen_dict[bus][carrier] = vals

    # ── Per-bus load per hour ────────────────────────────────────────────
    load_ps = n.loads_t.p_set
    load_info = n.loads[['bus']].copy()
    for i, snap in enumerate(snapshots):
        load_info[hcols[i]] = load_ps.loc[snap].reindex(load_info.index, fill_value=0.0).values
    bus_load = load_info.groupby('bus')[hcols].sum()

    bus_load_dict = {}
    for bus, row in bus_load.iterrows():
        vals = [round(float(v), 1) for v in row.values]
        if any(abs(v) > 0.5 for v in vals):
            bus_load_dict[bus] = vals

    # ── Bus JSON ─────────────────────────────────────────────────────────
    bus_json = {}
    for bid, (lat, lon, v) in bus_coords.items():
        entry = {'la': lat, 'lo': lon, 'v': v}
        if bid in bus_gen_dict:  entry['g'] = bus_gen_dict[bid]
        if bid in bus_load_dict: entry['ld'] = bus_load_dict[bid]
        bus_json[bid] = entry

    # ── Parallel line detection ──────────────────────────────────────────
    par_groups = defaultdict(list)
    for lid, row in n.lines.iterrows():
        key = tuple(sorted([row['bus0'], row['bus1']]))
        par_groups[key].append(lid)

    par_map = {}
    for key, lids in par_groups.items():
        for idx, lid in enumerate(lids):
            par_map[lid] = (len(lids), idx)

    # ── Line data ────────────────────────────────────────────────────────
    lines_data = []
    for lid, row in n.lines.iterrows():
        b0, b1 = row['bus0'], row['bus1']
        if b0 not in bus_coords or b1 not in bus_coords: continue
        s_nom = row['s_nom']
        v = int(row['v_nom']) if not pd.isna(row.get('v_nom', np.nan)) else 110
        flows = n.lines_t.p0[lid].values if lid in n.lines_t.p0.columns else np.zeros(nh)
        loadings = (np.abs(flows) / s_nom * 100).tolist() if s_nom > 0 else [0.0] * nh
        gs, gi = par_map.get(lid, (1, 0))
        lines_data.append({
            'id': str(lid),
            'b0': [bus_coords[b0][0], bus_coords[b0][1]],
            'b1': [bus_coords[b1][0], bus_coords[b1][1]],
            'v': v, 'sn': round(s_nom, 1),
            'f': [round(float(f), 1) for f in flows.tolist()],
            'l': [round(float(l), 1) for l in loadings],
            'ps': gs, 'pi': gi,
        })

    # ── Transformer data ─────────────────────────────────────────────────
    trafos_data = []
    for tid, row in n.transformers.iterrows():
        b0, b1 = row['bus0'], row['bus1']
        if b0 not in bus_coords or b1 not in bus_coords: continue
        flows = n.transformers_t.p0[tid].values if tid in n.transformers_t.p0.columns else np.zeros(nh)
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
        if b0 not in bus_coords or b1 not in bus_coords: continue
        p_nom = row['p_nom']
        flows = n.links_t.p0[lid].values if lid in n.links_t.p0.columns else np.zeros(nh)
        loadings = (np.abs(flows) / p_nom * 100).tolist() if p_nom > 0 else [0.0] * nh
        links_data.append({
            'id': str(lid), 'pn': round(p_nom, 1),
            'b0': [bus_coords[b0][0], bus_coords[b0][1]],
            'b1': [bus_coords[b1][0], bus_coords[b1][1]],
            'f': [round(float(f), 1) for f in flows.tolist()],
            'l': [round(float(l), 1) for l in loadings],
        })

    # ── Dispatch per hour ────────────────────────────────────────────────
    all_carriers = sorted(n.generators.carrier.unique())
    re_carriers = ['solar', 'onwind', 'offwind', 'run_of_river', 'reservoir']
    dispatch_data = []
    for i, snap in enumerate(snapshots):
        hourly = {}
        for c in all_carriers:
            gids = n.generators.carrier[n.generators.carrier == c].index
            if len(gids) > 0:
                hourly[c] = round(float(gen_p.loc[snap, gids].sum()), 0)
        hourly['_load'] = round(float(n.loads_t.p_set.loc[snap].sum()), 0)
        hourly['_label'] = snap.strftime('%b %d, %H:%M')

        # System-wide curtailment
        re_ids = n.generators.carrier[n.generators.carrier.isin(re_carriers)].index
        re_ts = [g for g in re_ids if g in n.generators_t.p_max_pu.columns]
        if re_ts:
            pot = float((n.generators_t.p_max_pu.loc[snap, re_ts] *
                         n.generators.loc[re_ts, 'p_nom']).sum())
            act = float(gen_p.loc[snap, re_ts].sum())
            hourly['_curt_mw'] = round(max(0, pot - act), 0)
            hourly['_curt_pct'] = round(max(0, (pot - act) / pot * 100), 1) if pot > 0 else 0
        else:
            hourly['_curt_mw'] = 0
            hourly['_curt_pct'] = 0

        dispatch_data.append(hourly)

    snap_labels = [s.strftime('%b %d %H:%M') for s in snapshots]

    # ── Per-bus curtailment data (with installed, potential, actual) ─────
    # {bus_id: {cap: installed_re_MW, p: [potential], a: [actual], k: [curtailed]}}
    re_gens = n.generators[n.generators.carrier.isin(re_carriers)].copy()
    re_in_ts = [g for g in re_gens.index if g in n.generators_t.p_max_pu.columns]
    bus_curt_dict = {}

    if re_in_ts:
        re_sub = re_gens.loc[re_in_ts]

        # Installed RE capacity per bus (static)
        cap_by_bus = re_sub.groupby('bus')['p_nom'].sum()

        # Per-carrier installed capacity per bus
        carrier_cap = re_sub.groupby(['bus', 'carrier'])['p_nom'].sum()
        bus_carrier_cap = {}
        for (bus, carrier), val in carrier_cap.items():
            if val > 0.5:
                bus_carrier_cap.setdefault(bus, {})[carrier] = round(float(val), 1)

        # Hourly potential, actual, curtailment
        all_pot = {}
        all_act = {}
        all_curt = {}
        for i, snap in enumerate(snapshots):
            potential = n.generators_t.p_max_pu.loc[snap, re_in_ts] * re_sub['p_nom']
            actual = gen_p.loc[snap, re_in_ts]
            curt = (potential - actual).clip(lower=0)

            pot_by_bus = potential.groupby(re_sub['bus']).sum()
            act_by_bus = actual.groupby(re_sub['bus']).sum()
            curt_by_bus = curt.groupby(re_sub['bus']).sum()

            for bus in curt_by_bus.index:
                c = float(curt_by_bus[bus])
                if c > 0.5 or bus in all_curt:
                    if bus not in all_curt:
                        all_pot[bus] = [0.0] * nh
                        all_act[bus] = [0.0] * nh
                        all_curt[bus] = [0.0] * nh
                    all_pot[bus][i] = round(float(pot_by_bus.get(bus, 0)), 1)
                    all_act[bus][i] = round(float(act_by_bus.get(bus, 0)), 1)
                    all_curt[bus][i] = round(c, 1)

        # Build final dict
        for bus in all_curt:
            if any(x > 0.5 for x in all_curt[bus]):
                bus_curt_dict[bus] = {
                    'c': round(float(cap_by_bus.get(bus, 0)), 1),
                    'cc': bus_carrier_cap.get(bus, {}),
                    'p': all_pot[bus],
                    'a': all_act[bus],
                    'k': all_curt[bus],
                }

    bus_curt_clean = bus_curt_dict
    log.info(f"  Curtailment data: {len(bus_curt_clean)} buses with curtailment")

    # ── Write HTML ───────────────────────────────────────────────────────
    html = _build_html(lines_data, trafos_data, links_data, bus_json,
                       dispatch_data, snap_labels, all_carriers, bus_curt_clean)
    outfile = os.path.join(OUTDIR, 'powerflow_constrained_map.html')
    with open(outfile, 'w') as f:
        f.write(html)
    log.info(f"Map saved to {outfile} ({len(html)//1024} KB)")
    log.info(f"  {len(lines_data)} lines, {len(trafos_data)} trafos, "
             f"{len(links_data)} links, {len(bus_json)} buses")


def _build_html(lines_data, trafos_data, links_data, bus_json,
                dispatch_data, snap_labels, carriers, bus_curt):
    """Build interactive HTML for the constrained LOPF map."""
    nh = len(snap_labels)

    carrier_colors_js = json.dumps(CARRIER_COLORS)
    lines_js   = json.dumps(lines_data)
    trafos_js  = json.dumps(trafos_data)
    links_js   = json.dumps(links_data)
    buses_js   = json.dumps(bus_json)
    dispatch_js = json.dumps(dispatch_data)
    labels_js  = json.dumps(snap_labels)
    curt_js    = json.dumps(bus_curt)

    domestic = [c for c in carriers if not c.startswith('import_')]
    imports  = sorted([c for c in carriers if c.startswith('import_')])
    carrier_order = json.dumps(domestic + imports)

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>German Grid - Constrained LOPF ({nh} hours)</title>
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
  .hour-btns {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; margin: 10px 0; }}
  .hour-btn {{ padding: 6px 4px; border: 1px solid #333; background: #1a1a2e; color: #ccc;
    cursor: pointer; border-radius: 4px; font-size: 11px; text-align: center; transition: all 0.2s; }}
  .hour-btn:hover {{ background: #2a2a4e; }}
  .hour-btn.active {{ background: #e94560; color: white; border-color: #e94560; font-weight: bold; }}
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
  .constrained-tag {{ display: inline-block; background: #27ae60; color: #fff; padding: 2px 8px;
    border-radius: 3px; font-size: 11px; font-weight: bold; margin-left: 6px; }}
</style>
</head><body>

<div id="sidebar">
  <h2>German Transmission Grid <span class="constrained-tag">CONSTRAINED</span></h2>
  <div style="font-size:11px;color:#888;">{nh} Random Hours &middot; s_max_pu=1.0 &middot; grid_beta</div>

  <h3>Select Hour</h3>
  <div class="hour-btns" id="hour-btns"></div>
  <div class="snap-info" id="snap-info"></div>

  <h3>System Balance</h3>
  <div class="summary-box">
    <div class="stat"><span>Total Load</span><span class="val" id="total-load">-</span></div>
    <div class="stat"><span>Total Generation</span><span class="val" id="total-gen">-</span></div>
    <div class="stat"><span>Renewable Share</span><span class="val" id="re-share">-</span></div>
    <div class="stat"><span>RE Curtailment</span><span class="val" id="curtailment">-</span></div>
    <div class="stat"><span>Max Line Loading</span><span class="val" id="max-loading">-</span></div>
    <div class="stat"><span>Lines &gt;80%</span><span class="val" id="overloaded">-</span></div>
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
    <span class="toggle-btn" id="curtBtn" onclick="toggleCurt()" style="background:#ff6600;color:#fff;border-color:#ff6600;">Show Curtailment</span>
  </div>

  <h3>Loading Legend</h3>
  <div class="legend">
    <div class="legend-item"><div class="legend-color" style="background:#2ecc71"></div> &lt;50%</div>
    <div class="legend-item"><div class="legend-color" style="background:#f1c40f"></div> 50-75%</div>
    <div class="legend-item"><div class="legend-color" style="background:#e67e22"></div> 75-100%</div>
    <div class="legend-item"><div class="legend-color" style="background:#e74c3c"></div> ~100% (constrained)</div>
    <div class="legend-item"><div class="legend-color" style="background:#00e5ff;height:3px;border-top:1px dashed #00e5ff"></div> HVDC</div>
  </div>

  <h3>Curtailment Levels</h3>
  <div class="legend">
    <div class="legend-item"><svg width="16" height="16"><circle cx="8" cy="8" r="6" fill="#cc0000" stroke="#fff" stroke-width="1"/></svg><span style="margin-left:6px">&gt;70% SEVERE</span></div>
    <div class="legend-item"><svg width="16" height="16"><circle cx="8" cy="8" r="5" fill="#ff2200" stroke="#fff" stroke-width="1"/></svg><span style="margin-left:6px">50-70% HIGH</span></div>
    <div class="legend-item"><svg width="16" height="16"><circle cx="8" cy="8" r="4.5" fill="#ff6600" stroke="#fff" stroke-width="1"/></svg><span style="margin-left:6px">30-50% MODERATE</span></div>
    <div class="legend-item"><svg width="16" height="16"><circle cx="8" cy="8" r="4" fill="#ffaa00" stroke="#fff" stroke-width="1"/></svg><span style="margin-left:6px">10-30% LOW</span></div>
    <div class="legend-item"><svg width="16" height="16"><circle cx="8" cy="8" r="3" fill="#ffdd44" stroke="#fff" stroke-width="1"/></svg><span style="margin-left:6px">&lt;10% MINIMAL</span></div>
    <div class="legend-item" style="margin-top:4px"><svg width="16" height="16"><circle cx="8" cy="8" r="6" fill="rgba(46,204,113,0.3)" stroke="#2ecc71" stroke-width="2"/></svg><span style="margin-left:6px">RE Potential (outer)</span></div>
    <div class="legend-item"><svg width="16" height="16"><circle cx="8" cy="8" r="4" fill="#2ecc71" stroke="#fff" stroke-width="1"/></svg><span style="margin-left:6px">RE Produced (inner)</span></div>
  </div>
</div>

<div id="map"></div>

<div id="right-panel">
  <h3 style="margin-top:0;">Top 20 Loaded Lines</h3>
  <div id="top20-list" style="margin-top:8px;"></div>

  <h3>Top 10 Loaded Trafos</h3>
  <div id="top10-trafos" style="margin-top:8px;"></div>
</div>

<script>
const NH = {nh};
const LINES = {lines_js};
const TRAFOS = {trafos_js};
const HVDC = {links_js};
const BUSES = {buses_js};
const DISPATCH = {dispatch_js};
const LABELS = {labels_js};
const CARRIER_COLORS = {carrier_colors_js};
const CARRIER_ORDER = {carrier_order};
const CURT = {curt_js};

let currentHour = 0;
let lineLayer, trafoLayer, hvdcLayer, busLayer, curtLayer, potLayer;
let showParallel = false;
let showCurt = false;
let showPotential = false;

// ── Tile layers ─────────────────────────────────────────────────────────
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

// ── Loading color ───────────────────────────────────────────────────────
function loadColor(pct) {{
  if (pct < 50)  return '#2ecc71';
  if (pct < 75)  return '#f1c40f';
  if (pct < 100) return '#e67e22';
  return '#e74c3c';
}}

function lineWeight(v) {{
  if (v >= 380) return 2.5;
  if (v >= 220) return 1.8;
  return 1.0;
}}

// ── Parallel offset ─────────────────────────────────────────────────────
function offsetCoords(b0, b1, offset) {{
  const dx = b1[1] - b0[1];
  const dy = b1[0] - b0[0];
  const len = Math.sqrt(dx*dx + dy*dy);
  if (len < 1e-8) return [b0, b1];
  const px = -dy / len * offset;
  const py = dx / len * offset;
  return [[b0[0]+px, b0[1]+py], [b1[0]+px, b1[1]+py]];
}}

// ── Draw functions ──────────────────────────────────────────────────────
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

  const h = currentHour;
  const lineFeatures = [];
  let maxL = 0, countOver80 = 0;

  LINES.forEach(line => {{
    const v = line.v;
    if ((v >= 380 && !show380) || (v >= 220 && v < 380 && !show220) || (v < 220 && !show110)) return;

    const loading = line.l[h];
    const flow = line.f[h];
    if (loading > maxL) maxL = loading;
    if (loading > 80) countOver80++;

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
      `Loading: <b>${{loading.toFixed(1)}}%</b>` + parLabel,
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
        `Flow: ${{Math.abs(flow).toFixed(0)}} MW<br>Loading: <b>${{loading.toFixed(1)}}%</b>`,
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

  document.getElementById('max-loading').textContent = maxL.toFixed(1) + '%';
  document.getElementById('overloaded').textContent = countOver80;
}}

function updateDispatch() {{
  const h = currentHour;
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

  // Curtailment from pre-computed data (updated by drawCurt when active)
  if (!showCurt) {{
    const curtMW = d._curt_mw || 0;
    const curtPct = d._curt_pct || 0;
    document.getElementById('curtailment').textContent =
      curtMW > 0 ? curtPct.toFixed(0) + '% (' + (curtMW/1000).toFixed(1) + ' GW)' : '0%';
  }}

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
  const h = currentHour;
  // Lines
  const sorted = LINES.filter(l => l.l[h] > 0)
    .sort((a, b) => b.l[h] - a.l[h])
    .slice(0, 20);

  let html = '';
  sorted.forEach((line, i) => {{
    const pct = line.l[h];
    const cls = pct > 95 ? 'red' : '';
    const par = line.ps > 1 ? ` (${{line.ps}}x)` : '';
    html += `<div class="top20-item" onclick="zoomToLine(${{LINES.indexOf(line)}})">
      <span class="top20-rank">#${{i+1}}</span>
      <span class="top20-info">${{line.v}}kV #${{line.id}}${{par}}<br>
        <span style="color:#888;font-size:10px">${{line.sn}} MW cap</span></span>
      <span class="top20-val ${{cls}}">${{pct.toFixed(1)}}%</span>
    </div>`;
  }});
  if (sorted.length === 0) html = '<div style="color:#888;font-size:12px;padding:10px;">No loaded lines</div>';
  document.getElementById('top20-list').innerHTML = html;

  // Trafos top 10
  const tSorted = TRAFOS.map((t,idx) => ({{...t, idx, loading: t.sn>0 ? Math.abs(t.f[h])/t.sn*100 : 0}}))
    .sort((a,b) => b.loading - a.loading).slice(0,10);
  let tHtml = '';
  tSorted.forEach((t,i) => {{
    const cls = t.loading > 95 ? 'red' : '';
    tHtml += `<div class="top20-item" onclick="map.setView([${{(t.b0[0]+t.b1[0])/2}},${{(t.b0[1]+t.b1[1])/2}}],11)">
      <span class="top20-rank">#${{i+1}}</span>
      <span class="top20-info">Trafo<br><span style="color:#888;font-size:10px">${{t.sn}} MW</span></span>
      <span class="top20-val ${{cls}}">${{t.loading.toFixed(1)}}%</span>
    </div>`;
  }});
  document.getElementById('top10-trafos').innerHTML = tHtml;
}}

function curtLevelColor(pct) {{
  // Curtailment severity: % of potential that is curtailed
  if (pct >= 70) return '#cc0000';    // deep red
  if (pct >= 50) return '#ff2200';    // red
  if (pct >= 30) return '#ff6600';    // orange
  if (pct >= 10) return '#ffaa00';    // yellow-orange
  return '#ffdd44';                    // light yellow
}}

function curtLevelLabel(pct) {{
  if (pct >= 70) return 'SEVERE';
  if (pct >= 50) return 'HIGH';
  if (pct >= 30) return 'MODERATE';
  if (pct >= 10) return 'LOW';
  return 'MINIMAL';
}}

function drawCurt() {{
  if (curtLayer) map.removeLayer(curtLayer);
  if (!showCurt) return;

  const h = currentHour;
  const curtFeatures = [];
  let totalCurt = 0, totalPot = 0;

  for (const [bid, d] of Object.entries(CURT)) {{
    const curtMW = d.k[h];
    const potMW = d.p[h];
    const actMW = d.a[h];
    if (curtMW < 1 && potMW < 1) continue;

    const pct = potMW > 0 ? curtMW / potMW * 100 : 0;
    totalCurt += curtMW;
    totalPot += potMW;

    const b = BUSES[bid];
    if (!b) continue;

    // Circle size by curtailed MW (sqrt scale)
    const r = Math.max(4, Math.min(35, Math.sqrt(curtMW) * 1.5));
    const color = curtLevelColor(pct);
    const level = curtLevelLabel(pct);

    const marker = L.circleMarker([b.la, b.lo], {{
      radius: r,
      fillColor: color,
      color: '#fff',
      weight: 1.5,
      fillOpacity: 0.8
    }});

    // Rich tooltip
    let tip = `<b>Bus ${{bid}}</b> (${{b.v}} kV)<br>`;
    tip += `<hr style="margin:2px 0;border-color:#444">`;
    tip += `<b>RE Installed:</b> ${{d.c.toFixed(0)}} MW<br>`;

    // Carrier breakdown of installed
    if (d.cc) {{
      for (const [c, mw] of Object.entries(d.cc)) {{
        const col = CARRIER_COLORS[c] || '#888';
        tip += `&nbsp;<span style="color:${{col}}">&#9679;</span> ${{c}}: ${{mw}} MW<br>`;
      }}
    }}

    tip += `<hr style="margin:2px 0;border-color:#444">`;
    tip += `<b>This hour:</b><br>`;
    tip += `&nbsp;Potential: <b>${{potMW.toFixed(0)}} MW</b><br>`;
    tip += `&nbsp;Produced: <b style="color:#2ecc71">${{actMW.toFixed(0)}} MW</b><br>`;
    tip += `&nbsp;Curtailed: <b style="color:${{color}}">${{curtMW.toFixed(0)}} MW (${{pct.toFixed(0)}}%)</b><br>`;
    tip += `&nbsp;Level: <b style="color:${{color}}">${{level}}</b>`;

    marker.bindTooltip(tip, {{sticky: true, className: 'dark-tooltip'}});
    curtFeatures.push(marker);
  }}

  curtLayer = L.layerGroup(curtFeatures).addTo(map);

  // Update summary
  const sysPct = totalPot > 0 ? (totalCurt/totalPot*100).toFixed(0) : '0';
  document.getElementById('curtailment').textContent =
    totalCurt > 0 ? sysPct + '% (' + (totalCurt/1000).toFixed(1) + ' GW / ' + (totalPot/1000).toFixed(1) + ' GW)' : '0%';
}}

function drawPotential() {{
  if (potLayer) map.removeLayer(potLayer);
  if (!showPotential) return;

  const h = currentHour;
  const potFeatures = [];

  for (const [bid, d] of Object.entries(CURT)) {{
    const potMW = d.p[h];
    const actMW = d.a[h];
    const curtMW = d.k[h];
    if (potMW < 1) continue;

    const b = BUSES[bid];
    if (!b) continue;

    // Green circle = actual production, outer ring = potential
    const rPot = Math.max(4, Math.min(35, Math.sqrt(potMW) * 1.5));

    // Outer circle: potential (green border)
    const potMarker = L.circleMarker([b.la, b.lo], {{
      radius: rPot,
      fillColor: curtMW > 0.5 ? 'rgba(46,204,113,0.3)' : '#2ecc71',
      color: '#2ecc71',
      weight: 2,
      fillOpacity: curtMW > 0.5 ? 0.3 : 0.7
    }});

    // If curtailed, add inner circle for actual
    if (curtMW > 0.5 && actMW > 0.5) {{
      const rAct = Math.max(3, rPot * Math.sqrt(actMW / potMW));
      const actMarker = L.circleMarker([b.la, b.lo], {{
        radius: rAct,
        fillColor: '#2ecc71',
        color: '#fff',
        weight: 1,
        fillOpacity: 0.8
      }});
      potFeatures.push(actMarker);
    }}

    const pct = potMW > 0 ? curtMW / potMW * 100 : 0;
    let tip = `<b>Bus ${{bid}}</b> (${{b.v}} kV) — RE Production<br>`;
    tip += `<b style="color:#2ecc71">&#9679; Produced: ${{actMW.toFixed(0)}} MW</b><br>`;
    tip += `<span style="color:#888">&#9675; Potential: ${{potMW.toFixed(0)}} MW</span><br>`;
    if (curtMW > 0.5) {{
      tip += `<b style="color:#ff6600">Curtailed: ${{curtMW.toFixed(0)}} MW (${{pct.toFixed(0)}}%)</b>`;
    }}
    potMarker.bindTooltip(tip, {{sticky: true, className: 'dark-tooltip'}});
    potFeatures.push(potMarker);
  }}

  potLayer = L.layerGroup(potFeatures).addTo(map);
}}

function zoomToLine(idx) {{
  const line = LINES[idx];
  if (!line) return;
  map.setView([(line.b0[0]+line.b1[0])/2, (line.b0[1]+line.b1[1])/2], 10);
}}

function toggleParallel() {{
  showParallel = !showParallel;
  document.getElementById('parBtn').classList.toggle('active', showParallel);
  updateMap();
}}

function toggleCurt() {{
  // Cycle: OFF → curtailment → potential → OFF
  if (!showCurt && !showPotential) {{
    showCurt = true; showPotential = false;
    document.getElementById('curtBtn').textContent = 'Curtailment ON';
    document.getElementById('curtBtn').style.background = '#0f3460';
    document.getElementById('curtBtn').style.borderColor = '#e94560';
  }} else if (showCurt && !showPotential) {{
    showCurt = false; showPotential = true;
    document.getElementById('curtBtn').textContent = 'RE Potential ON';
    document.getElementById('curtBtn').style.background = '#0a5e0a';
    document.getElementById('curtBtn').style.borderColor = '#2ecc71';
  }} else {{
    showCurt = false; showPotential = false;
    document.getElementById('curtBtn').textContent = 'Show Curtailment';
    document.getElementById('curtBtn').style.background = '#ff6600';
    document.getElementById('curtBtn').style.borderColor = '#ff6600';
  }}
  drawCurt();
  drawPotential();
}}

function updateMap() {{
  drawLines();
  updateDispatch();
  updateTop20();
  drawCurt();
  drawPotential();
}}

function selectHour(h) {{
  currentHour = h;
  document.querySelectorAll('.hour-btn').forEach((btn, i) => btn.classList.toggle('active', i === h));
  updateMap();
}}

// ── Hour buttons ────────────────────────────────────────────────────────
const btnContainer = document.getElementById('hour-btns');
LABELS.forEach((label, i) => {{
  const btn = document.createElement('div');
  btn.className = 'hour-btn' + (i === 0 ? ' active' : '');
  btn.textContent = label;
  btn.onclick = () => selectHour(i);
  btnContainer.appendChild(btn);
}});

// ── Tooltip style ───────────────────────────────────────────────────────
const style = document.createElement('style');
style.textContent = `.dark-tooltip {{ background: rgba(22,33,62,0.95) !important;
  color: #e0e0e0 !important; border: 1px solid #333 !important;
  border-radius: 4px !important; font-size: 12px !important; padding: 6px 8px !important; }}
  .dark-tooltip .leaflet-tooltip-tip {{ border-top-color: rgba(22,33,62,0.95) !important; }}`;
document.head.appendChild(style);

// ── Initial render ──────────────────────────────────────────────────────
updateMap();
</script>
</body></html>"""


# ──────────────────────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────────────────────
def main():
    os.makedirs(OUTDIR, exist_ok=True)
    engine = create_engine(DB_URL)

    log.info("=" * 70)
    log.info("CONSTRAINED LOPF – grid_beta – s_max_pu=1.0")
    log.info("=" * 70)

    log.info("\nPicking random hours...")
    snapshots, hour_indices = pick_hours()
    hour_labels = [s.strftime('%Y-%m-%d %H:%M') for s in snapshots]

    log.info("\nBuilding network...")
    n = load_network(engine, SCN, snapshots, hour_indices)

    status, condition = run_lopf(n)
    if status != 'ok':
        log.error(f"LOPF failed: {status}/{condition}")
        sys.exit(1)

    analyze_results(n, snapshots, hour_labels)

    nc_file = os.path.join(OUTDIR, 'powerflow_constrained.nc')
    n.export_to_netcdf(nc_file)
    log.info(f"\nNetwork saved to {nc_file}")

    create_map(n, snapshots, hour_indices)
    log.info("\nDone!")


if __name__ == '__main__':
    main()
