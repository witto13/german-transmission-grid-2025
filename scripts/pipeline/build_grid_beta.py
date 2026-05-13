#!/usr/bin/env python3
"""
Build grid_beta: grid_alpha topology + domestic loads + BDEW SLP profiles.

Creates grid_beta scenario by:
1. Copying all grid_alpha components (buses, lines, trafos, gens, storage, links, export loads)
2. Adding 448 TWh domestic loads from 11,003 municipalities
3. Adding 556 MaStR large industrial consumers (redistributed from municipality industry)
4. Generating BDEW-style SLP hourly profiles (8760h) for all loads
5. Smart municipality-to-bus splitting using PostGIS spatial containment + bus degree

Municipality splitting logic:
  - Spatial join: find all buses INSIDE each municipality polygon
  - Weight by bus degree (number of connected lines/trafos)
  - Urban factor: Städte use degree^1.5 (concentrate at major substations)
  - Rural factor: Gemeinden use degree^0.7 (spread more evenly)
  - Municipalities with no internal buses fall back to nearest-bus matching

Usage:
    python scripts/build_grid_beta.py --dry-run
    python scripts/build_grid_beta.py --apply
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from sqlalchemy import create_engine, text

DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
SRC_SCENARIO = 'grid_alpha'
TGT_SCENARIO = 'grid_beta'

# Hülk et al. (2017) voltage thresholds for peak load
PEAK_FACTOR = 1.49  # 76 GW peak / 51.1 GW average
VOLTAGE_THRESHOLDS = {380: 120.0, 220: 20.0, 110: 0.0}  # MW peak

# Spatial matching fallback distances (km)
MAX_DIST = {380: 100.0, 220: 75.0, 110: 50.0}

# Coordinate conversion (approximate for Germany ~51°N)
KM_LON = 71.5
KM_LAT = 111.0

# Large consumer share of industry (TWh)
LARGE_CONSUMER_TOTAL_TWH = 66.5  # ~35% of 190 TWh industry


# ═══════════════════════════════════════════════════════════════════
#  Phase 1: Copy scenario
# ═══════════════════════════════════════════════════════════════════

GRID_TABLES = [
    'egon_etrago_bus',
    'egon_etrago_line',
    'egon_etrago_line_timeseries',
    'egon_etrago_transformer',
    'egon_etrago_transformer_timeseries',
    'egon_etrago_link',
    'egon_etrago_link_timeseries',
    'egon_etrago_generator',
    'egon_etrago_generator_timeseries',
    'egon_etrago_storage',
    'egon_etrago_storage_timeseries',
    'egon_etrago_store',
    'egon_etrago_store_timeseries',
    'egon_etrago_load',
    'egon_etrago_load_timeseries',
]


def copy_scenario(engine):
    """Copy all grid_alpha records to grid_beta."""
    print("Phase 1: Copy grid_alpha → grid_beta")

    with engine.begin() as conn:
        # Check if grid_beta already exists
        n = conn.execute(text(
            f"SELECT COUNT(*) FROM grid.egon_etrago_bus "
            f"WHERE scn_name = '{TGT_SCENARIO}'"
        )).scalar()
        if n > 0:
            print(f"  grid_beta already exists ({n} buses). Deleting...")
            for table in reversed(GRID_TABLES):
                r = conn.execute(text(
                    f"DELETE FROM grid.{table} WHERE scn_name = '{TGT_SCENARIO}'"
                ))
                if r.rowcount > 0:
                    print(f"    Deleted {r.rowcount} from {table}")

        # Copy each table: select all columns except scn_name, prepend new scn_name
        for table in GRID_TABLES:
            cols_q = text(f"""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'grid' AND table_name = '{table}'
                AND column_name != 'scn_name'
                ORDER BY ordinal_position
            """)
            cols = [row[0] for row in conn.execute(cols_q)]
            cols_str = ', '.join(f'"{c}"' for c in cols)

            q = text(f"""
                INSERT INTO grid.{table} (scn_name, {cols_str})
                SELECT '{TGT_SCENARIO}', {cols_str}
                FROM grid.{table}
                WHERE scn_name = '{SRC_SCENARIO}'
            """)
            r = conn.execute(q)
            print(f"  {table}: {r.rowcount} rows copied")

    print(f"  Done. grid_beta created from grid_alpha.\n")


# ═══════════════════════════════════════════════════════════════════
#  Phase 2: Municipality data + spatial bus mapping
# ═══════════════════════════════════════════════════════════════════

def load_municipality_demand():
    """Load demand CSV (11,135 rows → 11,003 unique AGS)."""
    path = Path(__file__).parent.parent / 'demand_by_municipality_2025.csv'
    df = pd.read_csv(path)
    df['ags'] = df['ags'].astype(str).str.zfill(8)

    # Aggregate duplicate AGS codes (city districts etc.)
    agg = df.groupby('ags').agg({
        'name': 'first', 'type': 'first', 'nuts3': 'first',
        'area_km2': 'sum',
        'hh_mwh': 'sum', 'cts_mwh': 'sum', 'industry_mwh': 'sum',
        'total_mwh': 'sum',
    }).reset_index()

    print(f"  Loaded {len(agg)} municipalities, {agg['total_mwh'].sum()/1e6:.1f} TWh total")
    print(f"    Household: {agg['hh_mwh'].sum()/1e6:.1f} TWh")
    print(f"    CTS:       {agg['cts_mwh'].sum()/1e6:.1f} TWh")
    print(f"    Industry:  {agg['industry_mwh'].sum()/1e6:.1f} TWh")
    return agg


def compute_bus_degree(engine, scenario):
    """Compute bus connectivity degree (number of lines + trafos connected)."""
    degree = pd.read_sql(f"""
        WITH conns AS (
            SELECT bus0 AS bus_id FROM grid.egon_etrago_line WHERE scn_name = '{scenario}'
            UNION ALL
            SELECT bus1 FROM grid.egon_etrago_line WHERE scn_name = '{scenario}'
            UNION ALL
            SELECT bus0 FROM grid.egon_etrago_transformer WHERE scn_name = '{scenario}'
            UNION ALL
            SELECT bus1 FROM grid.egon_etrago_transformer WHERE scn_name = '{scenario}'
        )
        SELECT bus_id, COUNT(*) AS degree FROM conns GROUP BY bus_id
    """, engine)
    return dict(zip(degree['bus_id'], degree['degree']))


def spatial_join_buses_to_municipalities(engine, scenario):
    """PostGIS spatial join: find all grid buses inside each municipality polygon.

    Returns DataFrame with (ags, bus_id, v_nom) for every bus inside a municipality.
    """
    print("  Spatial join: buses → municipality polygons (PostGIS)...")
    df = pd.read_sql(f"""
        SELECT m.ags, m.gen AS muni_name, m.bez AS muni_type,
               b.bus_id, b.v_nom
        FROM grid.egon_etrago_bus b
        JOIN boundaries.vg250_gem m
            ON ST_Contains(m.geometry,
                           ST_SetSRID(ST_MakePoint(b.x, b.y), 4326))
        WHERE b.scn_name = '{scenario}'
          AND b.country = 'DE'
          AND b.v_nom IN (110, 220, 380)
          AND m.gf = 4
    """, engine)
    df['ags'] = df['ags'].astype(str).str.zfill(8)
    n_munis = df['ags'].nunique()
    n_buses = df['bus_id'].nunique()
    print(f"    {n_buses} buses inside {n_munis} municipalities")
    return df


def load_municipality_centroids(engine):
    """Load municipality centroids from PostGIS for fallback matching."""
    centroids = pd.read_sql("""
        SELECT ags, gen AS name, bez AS muni_type,
               ST_X(ST_Centroid(ST_Union(geometry))) AS lon,
               ST_Y(ST_Centroid(ST_Union(geometry))) AS lat
        FROM boundaries.vg250_gem
        WHERE gf = 4
        GROUP BY ags, gen, bez
    """, engine)
    centroids['ags'] = centroids['ags'].astype(str).str.zfill(8)
    return centroids


def estimate_peak(annual_mwh):
    """Annual MWh → peak MW using national peak factor."""
    return (annual_mwh / 8760) * PEAK_FACTOR


def assign_voltage(peak_mw):
    """Assign target voltage level from peak load (Hülk et al. 2017)."""
    if peak_mw > VOLTAGE_THRESHOLDS[380]:
        return 380
    elif peak_mw > VOLTAGE_THRESHOLDS[220]:
        return 220
    return 110


def build_load_entries(muni_demand, bus_muni_join, bus_degree, centroids,
                       all_buses):
    """Create per-bus load entries by splitting municipality demand across buses.

    For each municipality:
      1. If buses exist inside the polygon → split proportionally by degree
         - Stadt: weight = degree^1.5 (concentrate at major substations)
         - Gemeinde: weight = degree^0.7 (spread more evenly)
      2. If no buses inside → fall back to nearest bus by KD-tree

    Each municipality produces up to 2 load types:
      - residential_cts → always targets 110 kV
      - industry → voltage based on peak load threshold
    """
    print("\nPhase 2: Building load entries with smart municipality splitting...")

    # Group the spatial join by municipality
    muni_buses = {}  # ags → list of (bus_id, v_nom)
    muni_types = {}  # ags → muni_type
    for _, row in bus_muni_join.iterrows():
        ags = row['ags']
        muni_buses.setdefault(ags, []).append(
            (int(row['bus_id']), int(row['v_nom'])))
        muni_types[ags] = row['muni_type']

    # Add municipality types from centroids for those not in spatial join
    for _, row in centroids.iterrows():
        ags = row['ags']
        if ags not in muni_types:
            muni_types[ags] = row['muni_type']

    # Build KD-trees per voltage for fallback matching
    trees = {}
    bus_arrays = {}
    for v in [110, 220, 380]:
        bv = all_buses[all_buses['v_nom'] == v]
        if len(bv) > 0:
            coords = np.column_stack([
                bv['lon'].values * KM_LON, bv['lat'].values * KM_LAT])
            trees[v] = cKDTree(coords)
            bus_arrays[v] = bv['bus_id'].values

    # Merge demand with centroids for fallback coordinates
    demand_with_coords = muni_demand.merge(
        centroids[['ags', 'lon', 'lat']], on='ags', how='left')

    loads = []
    fallback_count = 0
    split_count = 0

    for _, muni in demand_with_coords.iterrows():
        ags = muni['ags']
        is_stadt = muni_types.get(ags, 'Gemeinde') == 'Stadt'
        buses_in_muni = muni_buses.get(ags, [])

        # ── Residential + CTS load (always → 110 kV) ──
        hh_cts_mwh = muni['hh_mwh'] + muni['cts_mwh']
        if hh_cts_mwh > 0:
            peak_mw = estimate_peak(hh_cts_mwh)
            target_v = 110

            # Only use buses at target voltage inside municipality
            v_buses = [(b, v) for b, v in buses_in_muni if v == target_v]

            if v_buses:
                # Weight by degree
                alpha = 1.5 if is_stadt else 0.7
                weights = []
                for bus_id, _ in v_buses:
                    d = bus_degree.get(bus_id, 1)
                    weights.append(d ** alpha)
                total_w = sum(weights)

                for (bus_id, v_nom), w in zip(v_buses, weights):
                    frac = w / total_w
                    loads.append({
                        'bus': bus_id, 'carrier': 'residential_cts',
                        'p_set': peak_mw * frac,
                        'annual_mwh': hh_cts_mwh * frac,
                        'ags': ags,
                    })
                split_count += 1
            else:
                # No 110kV bus inside polygon → nearest 110kV bus by KD-tree
                bus_id = _fallback_nearest(
                    muni['lon'], muni['lat'], target_v, trees, bus_arrays)
                if bus_id is not None:
                    loads.append({
                        'bus': bus_id, 'carrier': 'residential_cts',
                        'p_set': peak_mw, 'annual_mwh': hh_cts_mwh,
                        'ags': ags,
                    })
                    fallback_count += 1

        # ── Industry load (voltage by peak threshold) ──
        if muni['industry_mwh'] > 0:
            ind_peak = estimate_peak(muni['industry_mwh'])
            target_v = assign_voltage(ind_peak)

            # Only use buses at target voltage inside municipality
            v_buses = [(b, v) for b, v in buses_in_muni if v == target_v]

            if v_buses:
                alpha = 1.5 if is_stadt else 0.7
                weights = []
                for bus_id, _ in v_buses:
                    d = bus_degree.get(bus_id, 1)
                    weights.append(d ** alpha)
                total_w = sum(weights)

                for (bus_id, v_nom), w in zip(v_buses, weights):
                    frac = w / total_w
                    loads.append({
                        'bus': bus_id, 'carrier': 'industry',
                        'p_set': ind_peak * frac,
                        'annual_mwh': muni['industry_mwh'] * frac,
                        'ags': ags,
                    })
            else:
                # No bus at target voltage inside polygon → KD-tree fallback
                bus_id = _fallback_nearest(
                    muni['lon'], muni['lat'], target_v, trees, bus_arrays)
                if bus_id is not None:
                    loads.append({
                        'bus': bus_id, 'carrier': 'industry',
                        'p_set': ind_peak,
                        'annual_mwh': muni['industry_mwh'],
                        'ags': ags,
                    })

    loads_df = pd.DataFrame(loads)
    print(f"  Created {len(loads_df)} raw load entries")
    print(f"  Municipalities with internal buses (split): {split_count}")
    print(f"  Municipalities using fallback (nearest bus): {fallback_count}")

    return loads_df


def _fallback_nearest(lon, lat, target_v, trees, bus_arrays):
    """Find nearest bus at target voltage using KD-tree.

    Fallback strategy: try target voltage first within max distance.
    If not found, try lower voltages before higher ones (prefer 110kV
    for loads that should be at MV/LV). Only go higher if no lower bus
    is within reasonable range.
    """
    if pd.isna(lon) or pd.isna(lat):
        return None

    query = np.array([lon * KM_LON, lat * KM_LAT])
    max_d = MAX_DIST.get(target_v, 50.0)

    # Try target voltage
    if target_v in trees:
        dist, idx = trees[target_v].query(query)
        if dist <= max_d:
            return int(bus_arrays[target_v][idx])

    # Fallback: prefer lower voltages first (110 → 220 → 380)
    for v in sorted(trees.keys()):
        if v == target_v:
            continue
        dist, idx = trees[v].query(query)
        if dist <= MAX_DIST.get(v, 50.0):
            return int(bus_arrays[v][idx])

    # Last resort: absolute nearest regardless of distance
    best_bus, best_dist = None, float('inf')
    for v in trees:
        dist, idx = trees[v].query(query)
        if dist < best_dist:
            best_dist = dist
            best_bus = int(bus_arrays[v][idx])
    return best_bus


# ═══════════════════════════════════════════════════════════════════
#  Phase 3: Large industrial consumers
# ═══════════════════════════════════════════════════════════════════

def load_large_consumers(engine):
    """Load 556 MaStR large consumers and estimate their demand."""
    print("\nPhase 3: Large industrial consumers (MaStR)...")
    consumers = pd.read_sql("""
        SELECT "EinheitMastrNummer" as unit_id,
               "NameStromverbrauchseinheit" as name,
               "Breitengrad" as lat, "Laengengrad" as lon,
               "Gemeinde" as gemeinde,
               "Gemeindeschluessel" as ags,
               "AnzahlStromverbrauchseinheitenGroesser50Mw" as units_gt50mw
        FROM mastr.electricity_consumer
        WHERE "EinheitBetriebsstatus" = 'In Betrieb'
          AND "Laengengrad" IS NOT NULL
    """, engine)
    consumers['ags'] = consumers['ags'].astype(str).str.zfill(8)

    # Weight by units_gt50mw (proxy for size); minimum 1
    consumers['weight'] = consumers['units_gt50mw'].fillna(1.0).clip(lower=1.0)
    total_w = consumers['weight'].sum()
    consumers['annual_mwh'] = (
        consumers['weight'] / total_w * LARGE_CONSUMER_TOTAL_TWH * 1e6
    )
    consumers['peak_mw'] = consumers['annual_mwh'].apply(estimate_peak)
    consumers['target_v'] = consumers['peak_mw'].apply(assign_voltage)

    print(f"  {len(consumers)} operational consumers")
    print(f"  Total: {consumers['annual_mwh'].sum()/1e6:.1f} TWh, "
          f"peak range: {consumers['peak_mw'].min():.1f}–{consumers['peak_mw'].max():.1f} MW")
    return consumers


def integrate_large_consumers(consumers, all_buses, trees, bus_arrays):
    """Create load entries for MaStR large consumers at their precise locations.

    The 35% industry reduction was already applied to municipality data upfront,
    so these loads are additive without double counting.
    """
    print("  Placing large consumers at precise locations...")

    added = []
    for _, c in consumers.iterrows():
        target_v = int(c['target_v'])
        bus_id = _fallback_nearest(
            c['lon'], c['lat'], target_v, trees, bus_arrays)
        if bus_id is not None:
            added.append({
                'bus': bus_id, 'carrier': 'large_industry',
                'p_set': c['peak_mw'],
                'annual_mwh': c['annual_mwh'],
                'ags': c['ags'],
            })

    added_df = pd.DataFrame(added)
    print(f"  Placed {len(added_df)} large consumers, "
          f"{added_df['annual_mwh'].sum()/1e6:.1f} TWh, "
          f"{added_df['p_set'].sum()/1000:.1f} GW peak")
    return added_df


# ═══════════════════════════════════════════════════════════════════
#  Phase 4: Aggregate by bus
# ═══════════════════════════════════════════════════════════════════

def aggregate_loads(loads_df):
    """Aggregate load entries by (bus, carrier) — multiple municipalities may
    map to the same bus."""
    print("\nPhase 4: Aggregating loads by bus...")

    agg = loads_df.groupby(['bus', 'carrier']).agg(
        p_set=('p_set', 'sum'),
        annual_mwh=('annual_mwh', 'sum'),
        n_munis=('ags', 'nunique'),
    ).reset_index()

    # Remove zero/tiny loads
    agg = agg[agg['p_set'] > 0.001].copy()

    total_peak = agg['p_set'].sum()
    total_annual = agg['annual_mwh'].sum()
    n_buses = agg['bus'].nunique()

    print(f"  {len(agg)} aggregated load entries across {n_buses} buses")
    print(f"  Total peak: {total_peak/1000:.1f} GW")
    print(f"  Total annual: {total_annual/1e6:.1f} TWh")
    print(f"  By carrier:")
    for carrier, grp in agg.groupby('carrier'):
        print(f"    {carrier}: {grp['p_set'].sum()/1000:.1f} GW, "
              f"{grp['annual_mwh'].sum()/1e6:.1f} TWh, "
              f"{len(grp)} entries")

    return agg


# ═══════════════════════════════════════════════════════════════════
#  Phase 5: BDEW SLP profiles (8760h)
# ═══════════════════════════════════════════════════════════════════

def generate_bdew_profiles(year=2025):
    """Generate normalized BDEW-style load profiles for 8760 hours.

    Returns dict with keys: 'household', 'cts', 'industry'
    Each value is a numpy array of 8760 values normalized to sum to 1.0.

    Profile characteristics:
      household (H0): strong seasonal variation, morning+evening peaks
      cts (G0): business-hours peak, weekday-heavy, mild seasonal
      industry: relatively flat, mild weekend dip, very little seasonal
    """
    print("\nPhase 5: Generating BDEW SLP profiles (8760h)...")

    hours = pd.date_range(f'{year}-01-01', periods=8760, freq='h')
    doy = hours.dayofyear.values.astype(float)
    hod = hours.hour.values.astype(float)
    dow = hours.weekday.values  # 0=Mon, 6=Sun

    is_saturday = dow == 5
    is_sunday = dow == 6
    is_weekday = dow < 5

    # ── H0 Household ──
    # Seasonal: +20% in winter, -20% in summer
    h0_seasonal = 1.0 + 0.20 * np.cos(2 * np.pi * (doy - 15) / 365)
    # Daily: morning bump (7-9), evening peak (18-20), night trough
    h0_daily = (0.50
                + 0.20 * np.exp(-0.5 * ((hod - 7.5) / 1.8) ** 2)
                + 0.35 * np.exp(-0.5 * ((hod - 18.5) / 2.2) ** 2)
                + 0.10 * np.exp(-0.5 * ((hod - 12.5) / 2.0) ** 2))
    # Weekend: later morning, lower overall
    h0_weekend = np.where(
        is_sunday,
        0.80 + 0.05 * np.exp(-0.5 * ((hod - 12) / 3) ** 2),
        np.where(is_saturday, 0.90, 1.0))
    h0 = h0_seasonal * h0_daily * h0_weekend
    h0 /= h0.sum()

    # ── G0 Commercial/Services ──
    # Seasonal: mild (+8% winter, -8% summer)
    g0_seasonal = 1.0 + 0.08 * np.cos(2 * np.pi * (doy - 15) / 365)
    # Daily: business hours peak (8-18)
    g0_daily = (0.40
                + 0.55 * np.exp(-0.5 * ((hod - 13) / 4.0) ** 2)
                + 0.15 * np.exp(-0.5 * ((hod - 10) / 2.0) ** 2))
    # Weekend: Saturday 70%, Sunday 50%
    g0_weekend = np.where(
        is_sunday, 0.50,
        np.where(is_saturday, 0.70, 1.0))
    g0 = g0_seasonal * g0_daily * g0_weekend
    g0 /= g0.sum()

    # ── Industry ──
    # Seasonal: very mild (+3% winter, -3% summer)
    ind_seasonal = 1.0 + 0.03 * np.cos(2 * np.pi * (doy - 15) / 365)
    # Daily: 3-shift flat with mild daytime bump
    ind_daily = (0.85
                 + 0.15 * np.exp(-0.5 * ((hod - 14) / 6.0) ** 2))
    # Weekend: 80% (some operations continue)
    ind_weekend = np.where(
        is_sunday, 0.75,
        np.where(is_saturday, 0.85, 1.0))
    ind = ind_seasonal * ind_daily * ind_weekend
    ind /= ind.sum()

    # Validation
    for name, prof in [('H0', h0), ('G0', g0), ('Industry', ind)]:
        peak_avg = prof.max() / prof.mean()
        print(f"  {name}: peak/avg = {peak_avg:.2f}, "
              f"sum = {prof.sum():.6f}")

    return {'household': h0, 'cts': g0, 'industry': ind}


def build_timeseries(agg_loads, profiles):
    """Build 8760h p_set timeseries arrays for each load.

    carrier → profile mapping:
      residential_cts → weighted blend of H0 (52%) + G0 (48%)  [134/(134+124)]
      industry → industry profile
      large_industry → industry profile
      export_* → keep existing timeseries (not touched here)
    """
    print("  Building load timeseries arrays...")

    # Blend residential + CTS profile
    hh_share = 134.0 / (134.0 + 124.0)  # ~52%
    rcts_profile = hh_share * profiles['household'] + (1 - hh_share) * profiles['cts']
    rcts_profile /= rcts_profile.sum()  # renormalize

    carrier_profiles = {
        'residential_cts': rcts_profile,
        'industry': profiles['industry'],
        'large_industry': profiles['industry'],
    }

    ts_records = []
    for _, row in agg_loads.iterrows():
        carrier = row['carrier']
        annual_mwh = row['annual_mwh']

        prof = carrier_profiles.get(carrier)
        if prof is None:
            continue

        # Scale: profile sums to 1.0, multiply by annual MWh to get hourly MW
        # (since each hour = 1h, MWh = MW for that hour)
        hourly_mw = prof * annual_mwh
        ts_records.append(hourly_mw)

    return ts_records


# ═══════════════════════════════════════════════════════════════════
#  Phase 6: Write to database
# ═══════════════════════════════════════════════════════════════════

def write_loads(engine, agg_loads, ts_arrays, dry_run=True):
    """Write load entries + timeseries to grid_beta."""
    print(f"\nPhase 6: {'DRY RUN' if dry_run else 'Writing'} to database...")

    # Get max existing load_id in grid_beta
    with engine.begin() as conn:
        max_id = conn.execute(text(
            f"SELECT COALESCE(MAX(load_id), 0) FROM grid.egon_etrago_load "
            f"WHERE scn_name = '{TGT_SCENARIO}'"
        )).scalar()
    print(f"  Existing max load_id: {max_id}")

    # Prepare load records
    n = len(agg_loads)
    load_ids = list(range(max_id + 1, max_id + 1 + n))

    db_loads = pd.DataFrame({
        'scn_name': TGT_SCENARIO,
        'load_id': load_ids,
        'bus': agg_loads['bus'].astype(int).values,
        'type': '',
        'carrier': agg_loads['carrier'].values,
        'p_set': agg_loads['p_set'].values,
        'q_set': 0.0,
        'sign': -1.0,
    })

    print(f"  Prepared {n} load records (IDs {load_ids[0]}–{load_ids[-1]})")
    print(f"  Total peak: {db_loads['p_set'].sum()/1000:.1f} GW")

    if dry_run:
        print("  DRY RUN — no database changes")
        return load_ids

    # Write loads
    db_loads.to_sql('egon_etrago_load', engine, schema='grid',
                    if_exists='append', index=False)
    print(f"  Inserted {n} load records")

    # Write timeseries
    print("  Writing timeseries (this may take a moment)...")
    ts_rows = []
    for load_id, ts_arr in zip(load_ids, ts_arrays):
        ts_rows.append({
            'scn_name': TGT_SCENARIO,
            'load_id': load_id,
            'temp_id': 1,
            'p_set': list(ts_arr.astype(float)),
            'q_set': None,
        })

    # Insert in batches to avoid memory issues
    batch_size = 500
    for i in range(0, len(ts_rows), batch_size):
        batch = ts_rows[i:i + batch_size]
        with engine.begin() as conn:
            for row in batch:
                p_arr = '{' + ','.join(f'{v:.4f}' for v in row['p_set']) + '}'
                conn.execute(text("""
                    INSERT INTO grid.egon_etrago_load_timeseries
                    (scn_name, load_id, temp_id, p_set, q_set)
                    VALUES (:scn, :lid, :tid, CAST(:pset AS double precision[]), NULL)
                """), {
                    'scn': row['scn_name'],
                    'lid': row['load_id'],
                    'tid': row['temp_id'],
                    'pset': p_arr,
                })
        if (i + batch_size) % 2000 == 0 or i + batch_size >= len(ts_rows):
            print(f"    {min(i + batch_size, len(ts_rows))}/{len(ts_rows)} timeseries written")

    print(f"  Done. {len(ts_rows)} timeseries inserted.")
    return load_ids


# ═══════════════════════════════════════════════════════════════════
#  Phase 7: Validation
# ═══════════════════════════════════════════════════════════════════

def validate(engine):
    """Validate grid_beta loads."""
    print("\n" + "=" * 60)
    print("VALIDATION — grid_beta loads")
    print("=" * 60)

    loads = pd.read_sql(f"""
        SELECT l.load_id, l.bus, l.carrier, l.p_set,
               b.v_nom
        FROM grid.egon_etrago_load l
        JOIN grid.egon_etrago_bus b ON l.bus = b.bus_id AND b.scn_name = l.scn_name
        WHERE l.scn_name = '{TGT_SCENARIO}'
    """, engine)

    # Separate domestic vs export
    domestic = loads[~loads['carrier'].str.startswith('export_')]
    exports = loads[loads['carrier'].str.startswith('export_')]

    print(f"\nDomestic loads: {len(domestic)} entries, "
          f"{domestic['p_set'].sum()/1000:.1f} GW peak")
    print(f"Export loads:   {len(exports)} entries, "
          f"{exports['p_set'].sum()/1000:.1f} GW (cross-border)")

    # By carrier
    print("\nDomestic loads by carrier:")
    for carrier, grp in domestic.groupby('carrier'):
        peak_gw = grp['p_set'].sum() / 1000
        annual_twh = grp['p_set'].sum() * 8760 / PEAK_FACTOR / 1e6
        print(f"  {carrier:20s}: {peak_gw:6.1f} GW peak, "
              f"~{annual_twh:.0f} TWh, {len(grp)} entries")

    # By voltage level
    print("\nDomestic loads by voltage level:")
    for v, grp in domestic.groupby('v_nom'):
        peak_gw = grp['p_set'].sum() / 1000
        pct = 100 * grp['p_set'].sum() / domestic['p_set'].sum()
        print(f"  {int(v)} kV: {peak_gw:6.1f} GW ({pct:.1f}%), "
              f"{len(grp)} entries")

    # Check timeseries
    ts_count = pd.read_sql(f"""
        SELECT COUNT(*) FROM grid.egon_etrago_load_timeseries
        WHERE scn_name = '{TGT_SCENARIO}'
    """, engine).iloc[0, 0]
    print(f"\nTimeseries: {ts_count} records (expected: {len(loads)})")

    # Energy balance
    total_peak = domestic['p_set'].sum()
    implied_twh = total_peak * 8760 / PEAK_FACTOR / 1e6
    print(f"\nEnergy balance:")
    print(f"  Total domestic peak: {total_peak/1000:.1f} GW")
    print(f"  Implied annual:      {implied_twh:.1f} TWh (target: ~448 TWh)")
    print(f"  BNetzA 2024 peak:    75.8 GW")


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Build grid_beta with loads')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be done without writing to DB')
    parser.add_argument('--apply', action='store_true',
                        help='Actually write to database')
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        print("Specify --dry-run or --apply")
        sys.exit(1)

    dry_run = not args.apply

    print("=" * 60)
    print("Build grid_beta: grid_alpha + domestic loads + BDEW profiles")
    print("=" * 60)
    print(f"Mode: {'DRY RUN' if dry_run else 'APPLY'}\n")

    engine = create_engine(DB_URI)

    # Phase 1: Copy scenario
    if not dry_run:
        copy_scenario(engine)
    else:
        # Check if grid_beta exists for dry-run validation
        n = pd.read_sql(
            f"SELECT COUNT(*) FROM grid.egon_etrago_bus "
            f"WHERE scn_name = '{TGT_SCENARIO}'", engine
        ).iloc[0, 0]
        if n > 0:
            print(f"Phase 1: grid_beta exists ({n} buses) — dry run skip\n")
        else:
            print("Phase 1: grid_beta does not exist — will be created on --apply\n")
            # For dry-run, use grid_alpha buses
            pass

    # Determine which scenario to read buses from
    read_scn = TGT_SCENARIO if not dry_run else SRC_SCENARIO
    with engine.begin() as conn:
        n_beta = conn.execute(text(
            f"SELECT COUNT(*) FROM grid.egon_etrago_bus "
            f"WHERE scn_name = '{TGT_SCENARIO}'"
        )).scalar()
    if n_beta > 0:
        read_scn = TGT_SCENARIO

    # Load all DE buses
    all_buses = pd.read_sql(f"""
        SELECT bus_id, x AS lon, y AS lat, v_nom
        FROM grid.egon_etrago_bus
        WHERE scn_name = '{read_scn}' AND country = 'DE'
          AND v_nom IN (110, 220, 380)
    """, engine)
    print(f"Loaded {len(all_buses)} DE buses from {read_scn}")

    # Phase 2: Municipality loads
    muni_demand = load_municipality_demand()

    # Upfront: reduce municipality industry by 35% — this share will be
    # re-added as precisely-located large consumer loads (Phase 3)
    LARGE_IND_SHARE = 0.35
    ind_before = muni_demand['industry_mwh'].sum()
    muni_demand['industry_mwh'] *= (1 - LARGE_IND_SHARE)
    ind_after = muni_demand['industry_mwh'].sum()
    print(f"  Industry reduced by {LARGE_IND_SHARE:.0%}: "
          f"{ind_before/1e6:.1f} → {ind_after/1e6:.1f} TWh "
          f"(−{(ind_before - ind_after)/1e6:.1f} TWh → large consumers)")

    # Spatial join: buses inside municipality polygons
    bus_muni = spatial_join_buses_to_municipalities(engine, read_scn)

    bus_degree = compute_bus_degree(engine, read_scn)
    centroids = load_municipality_centroids(engine)

    loads_df = build_load_entries(
        muni_demand, bus_muni, bus_degree, centroids, all_buses)

    # Build KD-trees for consumer matching
    trees = {}
    bus_arrays = {}
    for v in [110, 220, 380]:
        bv = all_buses[all_buses['v_nom'] == v]
        if len(bv) > 0:
            coords = np.column_stack([
                bv['lon'].values * KM_LON, bv['lat'].values * KM_LAT])
            trees[v] = cKDTree(coords)
            bus_arrays[v] = bv['bus_id'].values

    # Phase 3: Large consumers
    consumers = load_large_consumers(engine)
    consumer_loads = integrate_large_consumers(
        consumers, all_buses, trees, bus_arrays)
    loads_df = pd.concat([loads_df, consumer_loads], ignore_index=True)

    # Phase 4: Aggregate
    agg = aggregate_loads(loads_df)

    # Phase 5: Profiles
    profiles = generate_bdew_profiles()
    ts_arrays = build_timeseries(agg, profiles)
    print(f"  Built {len(ts_arrays)} timeseries arrays (8760h each)")

    # Phase 6: Write
    write_loads(engine, agg, ts_arrays, dry_run=dry_run)

    # Phase 7: Validate
    if not dry_run:
        validate(engine)

    print("\nDone.")


if __name__ == '__main__':
    main()
