#!/usr/bin/env python3
"""
Build grid_alpha scenario: v6 grid topology + all MaStR generators.

Copies eGon2025v6 (7,723 buses, 12,911 lines, 567 trafos, 14 HVDC, 66 gens, 56 loads)
to grid_alpha, then allocates all operational MaStR generation (~250 GW) to grid buses.

Allocation approach:
- SEL-based grouping: group MaStR units by SEL (grid feed-in location), get voltage
  from the SEL→SAN→grid_connections join chain, spatial match group centroid to buses
- Municipality aggregation: for LV/MV units without coordinates (4.7M solar, etc.),
  aggregate by (Gemeindeschluessel, carrier) and match municipality centroid to 110kV bus

Technologies: wind onshore, conventional, solar, biomass, hydro, storage
(Offshore wind excluded — v6 already has 7.1 GW via HVDC generators)

Usage:
    python scripts/build_grid_alpha.py [--dry-run] [--apply] [--technology TECH]
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from scripts.utils.spatial_matching import SpatialMatcher

DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
V6 = 'eGon2025v6'
ALPHA = 'grid_alpha'

# Existing v6 generators use IDs 1-66, loads 1-56
GEN_START_ID = 100
STORAGE_START_ID = 100

# MaStR Spannungsebene → v_nom (all MV and below → 110)
VOLTAGE_MAPPING = {
    'Höchstspannung': 380,
    'Umspannebene Höchstspannung/Hochspannung': 220,
    'Hochspannung': 110,
    'Umspannebene Hochspannung/Mittelspannung': 110,
    'Mittelspannung': 110,
    'Umspannebene Mittelspannung/Niederspannung': 110,
    'Niederspannung (= Hausanschluss/Haushaltsstrom)': 110,
}

# Voltage thresholds for capacity-based assignment (Hülk et al. 2017)
VOLTAGE_THRESHOLDS = {380: 120.0, 220: 20.0, 110: 0.0}

# Carrier mappings
CONVENTIONAL_CARRIER_MAPPING = {
    'Erdgas, Erdölgas': 'gas', 'Erdgas': 'gas', 'Grubengas': 'gas',
    'Andere Gase': 'gas', 'Sonstige hergestellte Gase': 'gas',
    'Hochofengas, Konvertergas': 'gas', 'Raffineriegas': 'gas', 'Kokereigas': 'gas',
    'Steinkohlen': 'coal', 'Steinkohle': 'coal',
    'Steinkohlenbriketts': 'coal', 'Steinkohlenkoks': 'coal',
    'Rohbraunkohlen': 'lignite', 'Braunkohle': 'lignite',
    'Braunkohlenbriketts': 'lignite', 'Wirbelschichtkohle': 'lignite',
    'Heizöl, leicht': 'oil', 'Heizöl, schwer': 'oil',
    'Dieselkraftstoff': 'oil', 'Andere Mineralölprodukte': 'oil', 'Mineralölprodukte': 'oil',
    'Abfall (Hausmüll, Siedl.abf.)': 'waste', 'Industrieabfall': 'waste',
    'nicht biogener Abfall': 'waste',
    'Dampf (zum Beispiel Prozesswärme)': 'other', 'Wärme': 'other',
    'Wasserstoff': 'hydrogen',
}

BIOMASS_CARRIER_MAPPING = {
    'Biogas': 'biogas', 'Biomethan (Bioerdgas)': 'biogas',
    'Klärgas': 'biogas', 'Deponiegas': 'biogas',
    'feste Biomasse': 'biomass', 'Holzgas': 'biomass',
    'Altholz, Gebrauchtholz, Holz(sperr)müll': 'biomass',
    'biogener Abfall': 'biomass', 'Pflanzenöl': 'biomass',
}

HYDRO_CARRIER_MAPPING = {
    'Laufwasseranlage': 'run_of_river',
    'Speicherwasseranlage': 'reservoir',
    'Wasserkraftanlage in Trinkwassersystem': 'run_of_river',
    'Wasserkraftanlage in Brauchwassersystem': 'run_of_river',
    'Abwasserkraftanlage': 'run_of_river',
    'Meeresenergie': 'run_of_river',
}

# Search distances by voltage (km)
MAX_DISTANCE_KM = {380: 50.0, 220: 30.0, 110: 20.0}


def copy_v6_to_alpha(engine):
    """Copy all v6 records to grid_alpha scenario."""
    tables = [
        'egon_etrago_bus', 'egon_etrago_line', 'egon_etrago_transformer',
        'egon_etrago_link', 'egon_etrago_generator', 'egon_etrago_load',
        'egon_etrago_storage', 'egon_etrago_store',
    ]
    ts_tables = ['egon_etrago_generator_timeseries', 'egon_etrago_load_timeseries']

    with engine.begin() as conn:
        # Clear existing
        for t in tables + ts_tables:
            conn.execute(text(f"DELETE FROM grid.{t} WHERE scn_name = :scn"), {'scn': ALPHA})
        print("  Cleared existing grid_alpha records")

        # Copy component tables
        for t in tables:
            df = pd.read_sql(f"SELECT * FROM grid.{t} WHERE scn_name = '{V6}'", conn)
            if len(df) == 0:
                print(f"  {t}: 0 rows (skipped)")
                continue
            df['scn_name'] = ALPHA
            df.to_sql(t, conn, schema='grid', if_exists='append', index=False)
            print(f"  {t}: {len(df)} rows copied")

        # Copy timeseries tables
        for t in ts_tables:
            df = pd.read_sql(f"SELECT * FROM grid.{t} WHERE scn_name = '{V6}'", conn)
            if len(df) == 0:
                print(f"  {t}: 0 rows (skipped)")
                continue
            df['scn_name'] = ALPHA
            df.to_sql(t, conn, schema='grid', if_exists='append', index=False)
            print(f"  {t}: {len(df)} rows copied")

    print("Copied v6 → grid_alpha")


def load_alpha_buses(engine):
    """Load German buses from grid_alpha for spatial matching."""
    query = text("""
        SELECT bus_id, x, y, v_nom, country
        FROM grid.egon_etrago_bus
        WHERE scn_name = :scn AND country = 'DE'
          AND v_nom IN (110, 220, 380)
    """)
    with engine.begin() as conn:
        buses = pd.read_sql(query, conn, params={'scn': ALPHA})
    print(f"Loaded {len(buses)} DE buses from grid_alpha")
    print(f"  By voltage: {buses.groupby('v_nom').size().to_dict()}")
    return buses


def load_municipality_centroids(engine):
    """Load municipality centroids from boundaries.vg250_gem."""
    query = """
        SELECT ags, gen as name,
               ST_X(ST_Centroid(ST_Union(geometry))) as lon,
               ST_Y(ST_Centroid(ST_Union(geometry))) as lat
        FROM boundaries.vg250_gem
        GROUP BY ags, gen
    """
    centroids = pd.read_sql(query, engine)
    centroids['ags'] = centroids['ags'].astype(str).str.zfill(8)
    print(f"Loaded {len(centroids)} municipality centroids")
    return centroids


def load_buses_per_municipality(engine, scenario):
    """
    For each municipality, find all 110kV buses within its polygon via PostGIS.
    Returns dict: ags -> list of bus_ids.
    """
    print("Loading 110kV buses per municipality (PostGIS spatial join)...")
    query = f"""
        SELECT g.ags, b.bus_id
        FROM boundaries.vg250_gem g
        JOIN grid.egon_etrago_bus b
          ON ST_Contains(g.geometry, ST_SetSRID(ST_MakePoint(b.x, b.y), 4326))
        WHERE b.scn_name = '{scenario}'
          AND b.v_nom = 110
          AND b.country = 'DE'
        ORDER BY g.ags, b.bus_id
    """
    df = pd.read_sql(query, engine)
    df['ags'] = df['ags'].astype(str).str.zfill(8)

    buses_per_muni = df.groupby('ags')['bus_id'].apply(list).to_dict()
    n_with_buses = sum(1 for v in buses_per_muni.values() if len(v) > 0)
    n_multi = sum(1 for v in buses_per_muni.values() if len(v) > 1)
    print(f"  {n_with_buses} municipalities have 110kV buses ({n_multi} with 2+ buses)")
    return buses_per_muni


def assign_voltage_level(capacity_mw):
    """Assign voltage based on capacity using Hülk et al. thresholds."""
    if capacity_mw > VOLTAGE_THRESHOLDS[380]:
        return 380
    elif capacity_mw > VOLTAGE_THRESHOLDS[220]:
        return 220
    return 110


def sel_based_allocation(engine, tech_table, where_clause, extra_cols='',
                         carrier_col=None, carrier_mapping=None,
                         default_carrier='other', technology_name=''):
    """
    Generic SEL-based allocation: group MaStR units by SEL, get voltage from
    SAN chain, sum capacity, compute centroid, and return grouped DataFrame.

    Returns DataFrame with columns: [sel, carrier, p_nom_mw, n_units, lon, lat, v_nom]
    """
    # Build the carrier selection expression
    carrier_select = f'w."{carrier_col}" as fuel_type,' if carrier_col else ''

    query = f"""
        SELECT
            w."LokationMastrNummer" as sel,
            {carrier_select}
            w."Nettonennleistung" / 1000.0 as p_nom,
            w."Breitengrad" as lat,
            w."Laengengrad" as lon,
            w."Gemeindeschluessel" as ags,
            gc."Spannungsebene" as voltage_level
            {', ' + extra_cols if extra_cols else ''}
        FROM mastr.{tech_table} w
        LEFT JOIN mastr.locations_extended l
            ON w."LokationMastrNummer" = l."MastrNummer"
        LEFT JOIN mastr.grid_connections gc
            ON gc."NetzanschlusspunktMastrNummer" = SPLIT_PART(l."Netzanschlusspunkte", ', ', 1)
        WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
          AND w."Nettonennleistung" > 0
          {where_clause}
    """
    print(f"  Loading {technology_name} from database...")
    df = pd.read_sql(query, engine)
    print(f"  Loaded {len(df)} units, {df['p_nom'].sum()/1000:.2f} GW total")

    # Map carrier
    if carrier_mapping and carrier_col:
        df['carrier'] = df['fuel_type'].map(carrier_mapping).fillna(default_carrier)
    elif 'carrier' not in df.columns:
        df['carrier'] = default_carrier

    # Map voltage from SAN chain; track whether SAN voltage was present
    df['v_nom_raw'] = df['voltage_level'].map(VOLTAGE_MAPPING)
    df['has_san_voltage'] = df['v_nom_raw'].notna()
    df['v_nom'] = df['v_nom_raw'].fillna(110).astype(int)

    # Group by SEL
    grouped = df.groupby('sel').agg(
        carrier=('carrier', lambda x: x.mode().iloc[0] if len(x) > 0 else default_carrier),
        p_nom_mw=('p_nom', 'sum'),
        n_units=('p_nom', 'count'),
        lon=('lon', 'mean'),
        lat=('lat', 'mean'),
        v_nom=('v_nom', 'first'),
        has_san_voltage=('has_san_voltage', 'any'),
        ags=('ags', 'first'),
    ).reset_index()

    # Only apply capacity-based voltage override for SEL groups WITHOUT SAN voltage
    no_san = ~grouped['has_san_voltage']
    grouped.loc[no_san & (grouped['p_nom_mw'] > VOLTAGE_THRESHOLDS[380]), 'v_nom'] = 380
    grouped.loc[
        no_san &
        (grouped['p_nom_mw'] > VOLTAGE_THRESHOLDS[220]) &
        (grouped['p_nom_mw'] <= VOLTAGE_THRESHOLDS[380]),
        'v_nom'
    ] = 220
    n_override = no_san.sum()
    print(f"  SAN voltage: {(~no_san).sum()} with, {n_override} without (capacity-based fallback)")

    # Sanity check: downgrade to 110kV if SAN says 220/380 but capacity is below
    # the Hülk threshold (MaStR data errors — e.g. 0.1 MW solar at 220kV)
    bad_220 = (grouped['v_nom'] == 220) & (grouped['p_nom_mw'] <= VOLTAGE_THRESHOLDS[220])
    bad_380 = (grouped['v_nom'] == 380) & (grouped['p_nom_mw'] <= VOLTAGE_THRESHOLDS[380])
    n_downgrade = bad_220.sum() + bad_380.sum()
    grouped.loc[bad_220, 'v_nom'] = 110
    grouped.loc[bad_380 & (grouped['p_nom_mw'] > VOLTAGE_THRESHOLDS[220]), 'v_nom'] = 220
    grouped.loc[bad_380 & (grouped['p_nom_mw'] <= VOLTAGE_THRESHOLDS[220]), 'v_nom'] = 110
    if n_downgrade > 0:
        print(f"  Downgraded {n_downgrade} SEL groups with implausible SAN voltage")

    has_coords = grouped['lon'].notna() & grouped['lat'].notna()
    print(f"  SEL groups: {len(grouped)}, with coords: {has_coords.sum()}")
    print(f"  Capacity: {grouped['p_nom_mw'].sum()/1000:.2f} GW")

    return grouped, df


def spatial_match_sel_groups(sel_groups, matcher, technology_name):
    """Spatial match SEL groups to grid buses."""
    print(f"  Spatial matching {technology_name} SEL groups...")
    bus_ids = []
    distances = []

    for _, row in sel_groups.iterrows():
        if pd.isna(row['lon']) or pd.isna(row['lat']):
            bus_ids.append(None)
            distances.append(None)
            continue

        target_v = int(row['v_nom'])
        max_dist = MAX_DISTANCE_KM.get(target_v, 20.0)
        result = matcher.find_nearest(row['lon'], row['lat'], target_v, max_dist)

        if result:
            bus_ids.append(result[0])
            distances.append(result[1])
        else:
            # Fallback to any voltage, preferring the target
            fallback = matcher.find_nearest_any_voltage(
                row['lon'], row['lat'], max_distance_km=100.0,
                preferred_voltage=target_v
            )
            if fallback:
                bus_ids.append(fallback[0])
                distances.append(fallback[1])
            else:
                bus_ids.append(None)
                distances.append(None)

    sel_groups = sel_groups.copy()
    sel_groups['bus_id'] = bus_ids
    sel_groups['distance_km'] = distances

    matched = sel_groups['bus_id'].notna().sum()
    print(f"  Matched: {matched}/{len(sel_groups)} SEL groups "
          f"({sel_groups.loc[sel_groups['bus_id'].notna(), 'p_nom_mw'].sum()/1000:.2f} GW)")

    return sel_groups


def municipality_aggregation(units_df, muni_centroids, matcher, carrier_col_name,
                             carrier_mapping, default_carrier, technology_name,
                             buses_per_muni=None):
    """
    Aggregate MV/LV units by municipality and spread across all 110kV buses
    within that municipality. If a municipality has N 110kV buses, each gets
    1/N of the capacity. Fallback to nearest bus if no buses in polygon.

    Returns DataFrame with [bus_id, carrier, p_nom_mw, n_units, is_aggregated].
    """
    print(f"  Municipality aggregation for {technology_name}...")

    df = units_df.copy()
    df['ags'] = df['ags'].astype(str).str.zfill(8)

    if carrier_mapping and carrier_col_name and carrier_col_name in df.columns:
        df['carrier'] = df[carrier_col_name].map(carrier_mapping).fillna(default_carrier)
    elif 'carrier' not in df.columns:
        df['carrier'] = default_carrier

    has_ags = df['ags'].notna() & (df['ags'] != '') & (df['ags'] != '00000000')
    df_with_ags = df[has_ags]

    if len(df_with_ags) == 0:
        print(f"  No units with municipality codes")
        return pd.DataFrame()

    # Aggregate by (ags, carrier)
    muni_agg = df_with_ags.groupby(['ags', 'carrier']).agg(
        p_nom_mw=('p_nom', 'sum'),
        n_units=('p_nom', 'count'),
    ).reset_index()

    # Merge with centroids for fallback matching
    muni_agg = muni_agg.merge(
        muni_centroids[['ags', 'lon', 'lat']], on='ags', how='left'
    )

    # Spread across all buses in each municipality
    spread_rows = []
    n_spread = 0
    n_fallback = 0

    for _, row in muni_agg.iterrows():
        ags = row['ags']
        carrier = row['carrier']
        total_mw = row['p_nom_mw']
        total_units = row['n_units']

        # Try to find buses within this municipality polygon
        muni_buses = buses_per_muni.get(ags, []) if buses_per_muni else []

        if len(muni_buses) > 0:
            # Spread evenly across all buses in the municipality
            n_buses = len(muni_buses)
            mw_per_bus = total_mw / n_buses
            units_per_bus_base = int(total_units // n_buses)
            units_remainder = int(total_units % n_buses)

            for i, bus_id in enumerate(muni_buses):
                u = units_per_bus_base + (1 if i < units_remainder else 0)
                spread_rows.append({
                    'bus_id': bus_id,
                    'carrier': carrier,
                    'p_nom_mw': mw_per_bus,
                    'n_units': u,
                    'is_aggregated': True,
                })
            if n_buses > 1:
                n_spread += 1
        else:
            # Fallback: nearest bus to municipality centroid
            if pd.isna(row['lon']) or pd.isna(row['lat']):
                continue
            result = matcher.find_nearest(row['lon'], row['lat'], 110, MAX_DISTANCE_KM[110])
            if not result:
                fallback = matcher.find_nearest_any_voltage(row['lon'], row['lat'], 100.0)
                if fallback:
                    result = (fallback[0], fallback[1])
            if result:
                spread_rows.append({
                    'bus_id': result[0],
                    'carrier': carrier,
                    'p_nom_mw': total_mw,
                    'n_units': int(total_units),
                    'is_aggregated': True,
                })
                n_fallback += 1

    result_df = pd.DataFrame(spread_rows)

    if len(result_df) == 0:
        print(f"  No matched entries")
        return pd.DataFrame()

    matched_cap = result_df['p_nom_mw'].sum()
    print(f"  Municipality groups: {len(muni_agg)}, "
          f"spread to {len(result_df)} bus entries ({n_spread} multi-bus, {n_fallback} fallback)")
    print(f"  Capacity matched: {matched_cap/1000:.2f} GW")

    return result_df


def aggregate_final(matched_groups, technology_name):
    """Final aggregation by (bus_id, carrier) → ready for DB insertion."""
    if len(matched_groups) == 0:
        return pd.DataFrame()

    valid = matched_groups[matched_groups['bus_id'].notna()].copy()

    # Ensure is_aggregated column exists
    if 'is_aggregated' not in valid.columns:
        valid['is_aggregated'] = False

    agg = valid.groupby(['bus_id', 'carrier']).agg(
        p_nom_mw=('p_nom_mw', 'sum'),
        n_units=('n_units', 'sum'),
        is_aggregated=('is_aggregated', 'any'),
    ).reset_index()

    print(f"  {technology_name} final: {len(agg)} bus-carrier entries, "
          f"{agg['p_nom_mw'].sum()/1000:.2f} GW")
    return agg


# ─── Technology-specific allocation functions ─────────────────────────────

def allocate_wind(engine, matcher, muni_centroids, buses_per_muni=None):
    """Allocate onshore wind via SEL-based grouping."""
    print("\n" + "=" * 60)
    print("WIND ONSHORE")
    print("=" * 60)

    sel_groups, raw = sel_based_allocation(
        engine,
        tech_table='wind_extended',
        where_clause="AND w.\"WindAnLandOderAufSee\" = 'Windkraft an Land'",
        technology_name='wind onshore',
    )
    # All wind → carrier 'onwind'
    sel_groups['carrier'] = 'onwind'

    matched = spatial_match_sel_groups(sel_groups, matcher, 'wind onshore')
    agg = aggregate_final(matched, 'wind onshore')
    return agg


def allocate_conventional(engine, matcher, muni_centroids, buses_per_muni=None):
    """Allocate conventional generation (>= 1 MW) via SEL-based grouping."""
    print("\n" + "=" * 60)
    print("CONVENTIONAL")
    print("=" * 60)

    sel_groups, raw = sel_based_allocation(
        engine,
        tech_table='combustion_extended',
        where_clause='AND w."Nettonennleistung" >= 1000',
        carrier_col='Hauptbrennstoff',
        carrier_mapping=CONVENTIONAL_CARRIER_MAPPING,
        default_carrier='other',
        technology_name='conventional',
    )

    matched = spatial_match_sel_groups(sel_groups, matcher, 'conventional')
    agg = aggregate_final(matched, 'conventional')
    return agg


def allocate_solar(engine, matcher, muni_centroids, buses_per_muni=None):
    """Allocate solar: HV+ via SEL, MV/LV via municipality aggregation."""
    print("\n" + "=" * 60)
    print("SOLAR")
    print("=" * 60)

    # --- HV+ solar: SEL-based (Spannungsebene >= HV/MV transformer) ---
    print("\n--- Solar HV+ (SEL-based) ---")
    hv_voltages = "'Höchstspannung', 'Umspannebene Höchstspannung/Hochspannung', 'Hochspannung', 'Umspannebene Hochspannung/Mittelspannung'"

    # Load HV+ solar via the join chain
    hv_query = f"""
        SELECT
            w."LokationMastrNummer" as sel,
            w."Nettonennleistung" / 1000.0 as p_nom,
            w."Breitengrad" as lat,
            w."Laengengrad" as lon,
            w."Gemeindeschluessel" as ags,
            gc."Spannungsebene" as voltage_level
        FROM mastr.solar_extended w
        LEFT JOIN mastr.locations_extended l
            ON w."LokationMastrNummer" = l."MastrNummer"
        LEFT JOIN mastr.grid_connections gc
            ON gc."NetzanschlusspunktMastrNummer" = SPLIT_PART(l."Netzanschlusspunkte", ', ', 1)
        WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
          AND w."Nettonennleistung" > 0
          AND gc."Spannungsebene" IN ({hv_voltages})
    """
    print("  Loading HV+ solar...")
    hv_solar = pd.read_sql(hv_query, engine)
    hv_solar['carrier'] = 'solar'
    hv_solar['v_nom'] = hv_solar['voltage_level'].map(VOLTAGE_MAPPING).fillna(110).astype(int)
    print(f"  HV+ solar: {len(hv_solar)} units, {hv_solar['p_nom'].sum()/1000:.2f} GW")

    # Group by SEL
    if len(hv_solar) > 0:
        hv_grouped = hv_solar.groupby('sel').agg(
            carrier=('carrier', 'first'),
            p_nom_mw=('p_nom', 'sum'),
            n_units=('p_nom', 'count'),
            lon=('lon', 'mean'),
            lat=('lat', 'mean'),
            v_nom=('v_nom', 'first'),
            ags=('ags', 'first'),
        ).reset_index()

        # Sanity check: downgrade implausible voltages (Hülk thresholds)
        bad = (hv_grouped['v_nom'] == 220) & (hv_grouped['p_nom_mw'] <= VOLTAGE_THRESHOLDS[220])
        bad |= (hv_grouped['v_nom'] == 380) & (hv_grouped['p_nom_mw'] <= VOLTAGE_THRESHOLDS[380])
        if bad.any():
            print(f"  Downgraded {bad.sum()} solar HV+ SEL groups with implausible voltage")
            hv_grouped.loc[bad & (hv_grouped['p_nom_mw'] <= VOLTAGE_THRESHOLDS[220]), 'v_nom'] = 110
            hv_grouped.loc[bad & (hv_grouped['p_nom_mw'] > VOLTAGE_THRESHOLDS[220]), 'v_nom'] = 220

        hv_matched = spatial_match_sel_groups(hv_grouped, matcher, 'solar HV+')
        hv_agg = aggregate_final(hv_matched, 'solar HV+')
    else:
        hv_agg = pd.DataFrame()

    # --- MV/LV solar: municipality aggregation ---
    print("\n--- Solar MV/LV (municipality aggregation) ---")
    mv_lv_query = f"""
        SELECT
            w."Nettonennleistung" / 1000.0 as p_nom,
            w."Gemeindeschluessel" as ags
        FROM mastr.solar_extended w
        LEFT JOIN mastr.locations_extended l
            ON w."LokationMastrNummer" = l."MastrNummer"
        LEFT JOIN mastr.grid_connections gc
            ON gc."NetzanschlusspunktMastrNummer" = SPLIT_PART(l."Netzanschlusspunkte", ', ', 1)
        WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
          AND w."Nettonennleistung" > 0
          AND (gc."Spannungsebene" NOT IN ({hv_voltages})
               OR gc."Spannungsebene" IS NULL)
    """
    print("  Loading MV/LV solar...")
    mv_lv_solar = pd.read_sql(mv_lv_query, engine)
    mv_lv_solar['carrier'] = 'solar'
    print(f"  MV/LV solar: {len(mv_lv_solar)} units, {mv_lv_solar['p_nom'].sum()/1000:.2f} GW")

    if len(mv_lv_solar) > 0:
        mv_lv_agg = municipality_aggregation(
            mv_lv_solar, muni_centroids, matcher,
            carrier_col_name=None, carrier_mapping=None,
            default_carrier='solar', technology_name='solar MV/LV',
            buses_per_muni=buses_per_muni,
        )
        mv_lv_final = aggregate_final(mv_lv_agg, 'solar MV/LV')
    else:
        mv_lv_final = pd.DataFrame()

    # Combine
    parts = [df for df in [hv_agg, mv_lv_final] if len(df) > 0]
    if parts:
        combined = pd.concat(parts, ignore_index=True)
        # Ensure is_aggregated column exists
        if 'is_aggregated' not in combined.columns:
            combined['is_aggregated'] = False
        # Re-aggregate in case HV and MV/LV overlap on same bus
        combined = combined.groupby(['bus_id', 'carrier']).agg(
            p_nom_mw=('p_nom_mw', 'sum'),
            n_units=('n_units', 'sum'),
            is_aggregated=('is_aggregated', 'any'),
        ).reset_index()
        print(f"  Solar total: {len(combined)} entries, {combined['p_nom_mw'].sum()/1000:.2f} GW")
        return combined
    return pd.DataFrame()


def allocate_biomass(engine, matcher, muni_centroids, buses_per_muni=None):
    """Allocate biomass: HV+ via SEL, MV/LV via municipality."""
    print("\n" + "=" * 60)
    print("BIOMASS")
    print("=" * 60)

    hv_voltages = "'Höchstspannung', 'Umspannebene Höchstspannung/Hochspannung', 'Hochspannung', 'Umspannebene Hochspannung/Mittelspannung'"

    # --- HV+ biomass: SEL-based ---
    print("\n--- Biomass HV+ (SEL-based) ---")
    sel_groups, raw_hv = sel_based_allocation(
        engine,
        tech_table='biomass_extended',
        where_clause=f'AND gc."Spannungsebene" IN ({hv_voltages})',
        carrier_col='Hauptbrennstoff',
        carrier_mapping=BIOMASS_CARRIER_MAPPING,
        default_carrier='biomass',
        technology_name='biomass HV+',
    )
    if len(sel_groups) > 0:
        hv_matched = spatial_match_sel_groups(sel_groups, matcher, 'biomass HV+')
        hv_agg = aggregate_final(hv_matched, 'biomass HV+')
    else:
        hv_agg = pd.DataFrame()

    # --- MV/LV biomass: municipality aggregation ---
    print("\n--- Biomass MV/LV (municipality aggregation) ---")
    mv_lv_query = f"""
        SELECT
            w."Nettonennleistung" / 1000.0 as p_nom,
            w."Gemeindeschluessel" as ags,
            w."Hauptbrennstoff" as fuel_type
        FROM mastr.biomass_extended w
        LEFT JOIN mastr.locations_extended l
            ON w."LokationMastrNummer" = l."MastrNummer"
        LEFT JOIN mastr.grid_connections gc
            ON gc."NetzanschlusspunktMastrNummer" = SPLIT_PART(l."Netzanschlusspunkte", ', ', 1)
        WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
          AND w."Nettonennleistung" > 0
          AND (gc."Spannungsebene" NOT IN ({hv_voltages})
               OR gc."Spannungsebene" IS NULL)
    """
    mv_lv_bio = pd.read_sql(mv_lv_query, engine)
    mv_lv_bio['carrier'] = mv_lv_bio['fuel_type'].map(BIOMASS_CARRIER_MAPPING).fillna('biomass')
    print(f"  MV/LV biomass: {len(mv_lv_bio)} units, {mv_lv_bio['p_nom'].sum()/1000:.2f} GW")

    if len(mv_lv_bio) > 0:
        mv_lv_agg = municipality_aggregation(
            mv_lv_bio, muni_centroids, matcher,
            carrier_col_name='fuel_type', carrier_mapping=BIOMASS_CARRIER_MAPPING,
            default_carrier='biomass', technology_name='biomass MV/LV',
            buses_per_muni=buses_per_muni,
        )
        mv_lv_final = aggregate_final(mv_lv_agg, 'biomass MV/LV')
    else:
        mv_lv_final = pd.DataFrame()

    parts = [df for df in [hv_agg, mv_lv_final] if len(df) > 0]
    if parts:
        combined = pd.concat(parts, ignore_index=True)
        if 'is_aggregated' not in combined.columns:
            combined['is_aggregated'] = False
        combined = combined.groupby(['bus_id', 'carrier']).agg(
            p_nom_mw=('p_nom_mw', 'sum'), n_units=('n_units', 'sum'),
            is_aggregated=('is_aggregated', 'any'),
        ).reset_index()
        print(f"  Biomass total: {len(combined)} entries, {combined['p_nom_mw'].sum()/1000:.2f} GW")
        return combined
    return pd.DataFrame()


def allocate_hydro(engine, matcher, muni_centroids, buses_per_muni=None):
    """Allocate hydro via SEL-based grouping."""
    print("\n" + "=" * 60)
    print("HYDRO")
    print("=" * 60)

    sel_groups, raw = sel_based_allocation(
        engine,
        tech_table='hydro_extended',
        where_clause='',
        extra_cols='w."ArtDerWasserkraftanlage" as hydro_type',
        technology_name='hydro',
    )

    # Map carrier from raw data hydro_type at the SEL level
    # Re-derive carrier from raw units
    raw['carrier'] = raw.get('hydro_type', pd.Series(dtype=str)).map(
        HYDRO_CARRIER_MAPPING
    ).fillna('run_of_river') if 'hydro_type' in raw.columns else 'run_of_river'

    # Re-group with correct carrier
    if 'hydro_type' in raw.columns:
        raw['carrier'] = raw['hydro_type'].map(HYDRO_CARRIER_MAPPING).fillna('run_of_river')
    else:
        raw['carrier'] = 'run_of_river'

    regrouped = raw.groupby('sel').agg(
        carrier=('carrier', lambda x: x.mode().iloc[0] if len(x) > 0 else 'run_of_river'),
        p_nom_mw=('p_nom', 'sum'),
        n_units=('p_nom', 'count'),
        lon=('lon', 'mean'),
        lat=('lat', 'mean'),
        v_nom=('v_nom', 'first'),
        has_san_voltage=('has_san_voltage', 'any'),
        ags=('ags', 'first'),
    ).reset_index()

    # Override voltage for large plants WITHOUT SAN voltage
    no_san = ~regrouped['has_san_voltage']
    regrouped.loc[no_san & (regrouped['p_nom_mw'] > VOLTAGE_THRESHOLDS[380]), 'v_nom'] = 380
    regrouped.loc[
        no_san &
        (regrouped['p_nom_mw'] > VOLTAGE_THRESHOLDS[220]) &
        (regrouped['p_nom_mw'] <= VOLTAGE_THRESHOLDS[380]),
        'v_nom'
    ] = 220

    # Sanity check: downgrade implausible SAN voltages
    bad_220 = (regrouped['v_nom'] == 220) & (regrouped['p_nom_mw'] <= VOLTAGE_THRESHOLDS[220])
    bad_380 = (regrouped['v_nom'] == 380) & (regrouped['p_nom_mw'] <= VOLTAGE_THRESHOLDS[380])
    n_down = bad_220.sum() + bad_380.sum()
    regrouped.loc[bad_220, 'v_nom'] = 110
    regrouped.loc[bad_380 & (regrouped['p_nom_mw'] > VOLTAGE_THRESHOLDS[220]), 'v_nom'] = 220
    regrouped.loc[bad_380 & (regrouped['p_nom_mw'] <= VOLTAGE_THRESHOLDS[220]), 'v_nom'] = 110
    if n_down > 0:
        print(f"  Downgraded {n_down} hydro SEL groups with implausible voltage")

    matched = spatial_match_sel_groups(regrouped, matcher, 'hydro')
    agg = aggregate_final(matched, 'hydro')
    return agg


def allocate_storage(engine, matcher, muni_centroids, buses_per_muni=None):
    """Allocate storage (batteries >= 100 kW, pumped hydro >= 1 MW) via SEL."""
    print("\n" + "=" * 60)
    print("STORAGE")
    print("=" * 60)

    # Load storage with type info
    query = """
        SELECT
            w."LokationMastrNummer" as sel,
            w."Nettonennleistung" / 1000.0 as p_nom,
            w."Breitengrad" as lat,
            w."Laengengrad" as lon,
            w."Gemeindeschluessel" as ags,
            w."Batterietechnologie" as battery_type,
            w."Pumpspeichertechnologie" as pump_type,
            gc."Spannungsebene" as voltage_level
        FROM mastr.storage_extended w
        LEFT JOIN mastr.locations_extended l
            ON w."LokationMastrNummer" = l."MastrNummer"
        LEFT JOIN mastr.grid_connections gc
            ON gc."NetzanschlusspunktMastrNummer" = SPLIT_PART(l."Netzanschlusspunkte", ', ', 1)
        WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
          AND w."Nettonennleistung" > 0
    """
    print("  Loading storage units...")
    df = pd.read_sql(query, engine)

    # Map carrier
    def get_carrier(row):
        if pd.notna(row['pump_type']) and row['pump_type'] != '':
            return 'pumped_hydro'
        return 'battery'
    df['carrier'] = df.apply(get_carrier, axis=1)

    # Filter: batteries >= 100 kW, pumped hydro >= 1 MW
    df = df[
        ((df['carrier'] == 'battery') & (df['p_nom'] >= 0.1)) |
        ((df['carrier'] == 'pumped_hydro') & (df['p_nom'] >= 1.0))
    ].copy()

    print(f"  Filtered: {len(df)} units, {df['p_nom'].sum()/1000:.2f} GW")
    print(f"  By carrier: {df.groupby('carrier')['p_nom'].sum().to_dict()}")

    # Map voltage; track SAN presence
    df['v_nom_raw'] = df['voltage_level'].map(VOLTAGE_MAPPING)
    df['has_san_voltage'] = df['v_nom_raw'].notna()
    df['v_nom'] = df['v_nom_raw'].fillna(110).astype(int)

    # Override with capacity-based only for units WITHOUT SAN voltage
    no_san = ~df['has_san_voltage']
    df.loc[no_san & (df['p_nom'] > VOLTAGE_THRESHOLDS[380]), 'v_nom'] = 380
    df.loc[
        no_san &
        (df['p_nom'] > VOLTAGE_THRESHOLDS[220]) &
        (df['p_nom'] <= VOLTAGE_THRESHOLDS[380]),
        'v_nom'
    ] = 220

    # Sanity check: downgrade implausible SAN voltages
    bad_220 = (df['v_nom'] == 220) & (df['p_nom'] <= VOLTAGE_THRESHOLDS[220])
    bad_380 = (df['v_nom'] == 380) & (df['p_nom'] <= VOLTAGE_THRESHOLDS[380])
    n_down = bad_220.sum() + bad_380.sum()
    df.loc[bad_220, 'v_nom'] = 110
    df.loc[bad_380 & (df['p_nom'] > VOLTAGE_THRESHOLDS[220]), 'v_nom'] = 220
    df.loc[bad_380 & (df['p_nom'] <= VOLTAGE_THRESHOLDS[220]), 'v_nom'] = 110
    if n_down > 0:
        print(f"  Downgraded {n_down} storage units with implausible voltage")

    # --- Pumped hydro: SEL-based ---
    pumped = df[df['carrier'] == 'pumped_hydro'].copy()
    batteries = df[df['carrier'] == 'battery'].copy()

    results = []

    if len(pumped) > 0:
        print(f"\n--- Pumped hydro: {len(pumped)} units, {pumped['p_nom'].sum()/1000:.2f} GW ---")
        ph_grouped = pumped.groupby('sel').agg(
            carrier=('carrier', 'first'),
            p_nom_mw=('p_nom', 'sum'),
            n_units=('p_nom', 'count'),
            lon=('lon', 'mean'),
            lat=('lat', 'mean'),
            v_nom=('v_nom', 'max'),  # pumped hydro → highest voltage
            ags=('ags', 'first'),
        ).reset_index()
        ph_matched = spatial_match_sel_groups(ph_grouped, matcher, 'pumped hydro')
        ph_agg = aggregate_final(ph_matched, 'pumped hydro')
        if len(ph_agg) > 0:
            results.append(ph_agg)

    if len(batteries) > 0:
        print(f"\n--- Batteries: {len(batteries)} units, {batteries['p_nom'].sum()/1000:.2f} GW ---")
        # HV+ batteries: SEL-based
        hv_batt = batteries[batteries['v_nom'].isin([220, 380])].copy()
        mv_lv_batt = batteries[batteries['v_nom'] == 110].copy()

        if len(hv_batt) > 0:
            hv_grouped = hv_batt.groupby('sel').agg(
                carrier=('carrier', 'first'),
                p_nom_mw=('p_nom', 'sum'),
                n_units=('p_nom', 'count'),
                lon=('lon', 'mean'),
                lat=('lat', 'mean'),
                v_nom=('v_nom', 'first'),
                ags=('ags', 'first'),
            ).reset_index()
            hv_matched = spatial_match_sel_groups(hv_grouped, matcher, 'batteries HV+')
            hv_agg = aggregate_final(hv_matched, 'batteries HV+')
            if len(hv_agg) > 0:
                results.append(hv_agg)

        if len(mv_lv_batt) > 0:
            mv_lv_agg = municipality_aggregation(
                mv_lv_batt, muni_centroids, matcher,
                carrier_col_name=None, carrier_mapping=None,
                default_carrier='battery', technology_name='batteries MV/LV',
                buses_per_muni=buses_per_muni,
            )
            mv_lv_final = aggregate_final(mv_lv_agg, 'batteries MV/LV')
            if len(mv_lv_final) > 0:
                results.append(mv_lv_final)

    if results:
        combined = pd.concat(results, ignore_index=True)
        if 'is_aggregated' not in combined.columns:
            combined['is_aggregated'] = False
        combined = combined.groupby(['bus_id', 'carrier']).agg(
            p_nom_mw=('p_nom_mw', 'sum'), n_units=('n_units', 'sum'),
            is_aggregated=('is_aggregated', 'any'),
        ).reset_index()
        print(f"  Storage total: {len(combined)} entries, {combined['p_nom_mw'].sum()/1000:.2f} GW")
        return combined
    return pd.DataFrame()


# ─── Database insertion ───────────────────────────────────────────────────

def prepare_generator_records(aggregated, start_id):
    """Prepare generator records for egon_etrago_generator table."""
    if len(aggregated) == 0:
        return pd.DataFrame()

    records = pd.DataFrame({
        'scn_name': ALPHA,
        'generator_id': range(start_id, start_id + len(aggregated)),
        'bus': aggregated['bus_id'].astype(int).values,
        'control': 'PQ',
        'type': '',
        'carrier': aggregated['carrier'].values,
        'p_nom': aggregated['p_nom_mw'].astype(float).values,
        'p_nom_extendable': False,
        'p_nom_min': 0.0,
        'p_nom_max': float('inf'),
        'p_min_pu': 0.0,
        'p_max_pu': 1.0,
        'p_set': None,
        'q_set': None,
        'sign': 1.0,
        'marginal_cost': 0.0,
        'build_year': 0,
        'lifetime': float('inf'),
        'capital_cost': 0.0,
        'efficiency': 1.0,
        'committable': False,
        'start_up_cost': 0.0,
        'shut_down_cost': 0.0,
        'min_up_time': 0,
        'min_down_time': 0,
        'up_time_before': 0,
        'down_time_before': 0,
        'ramp_limit_up': None,
        'ramp_limit_down': None,
        'ramp_limit_start_up': 1.0,
        'ramp_limit_shut_down': 1.0,
        'e_nom_max': float('inf'),
    })

    # Convert numpy types to Python native
    for col in records.columns:
        if records[col].dtype == np.float64:
            records[col] = records[col].astype(object).where(records[col].notna(), None)
        elif records[col].dtype == np.int64:
            records[col] = records[col].astype(int)

    return records


def prepare_storage_records(aggregated, start_id):
    """Prepare storage records for egon_etrago_storage table."""
    if len(aggregated) == 0:
        return pd.DataFrame()

    max_hours = aggregated['carrier'].map({
        'battery': 4.0, 'pumped_hydro': 8.0,
    }).fillna(4.0)

    records = pd.DataFrame({
        'scn_name': ALPHA,
        'storage_id': range(start_id, start_id + len(aggregated)),
        'bus': aggregated['bus_id'].astype(int).values,
        'control': 'PQ',
        'type': '',
        'carrier': aggregated['carrier'].values,
        'p_nom': aggregated['p_nom_mw'].astype(float).values,
        'p_nom_extendable': False,
        'p_nom_min': 0.0,
        'p_nom_max': float('inf'),
        'p_min_pu': -1.0,
        'p_max_pu': 1.0,
        'p_set': None,
        'q_set': None,
        'sign': 1.0,
        'marginal_cost': 0.0,
        'capital_cost': 0.0,
        'build_year': 0,
        'lifetime': float('inf'),
        'state_of_charge_initial': 0.0,
        'cyclic_state_of_charge': True,
        'state_of_charge_set': None,
        'max_hours': max_hours.values,
        'efficiency_store': 0.9,
        'efficiency_dispatch': 0.9,
        'standing_loss': 0.0,
        'inflow': 0.0,
    })

    # Convert numpy types
    for col in records.columns:
        if records[col].dtype == np.float64:
            records[col] = records[col].astype(object).where(records[col].notna(), None)
        elif records[col].dtype == np.int64:
            records[col] = records[col].astype(int)

    return records


def insert_generators(gen_records, engine, dry_run):
    """Insert new generators into grid_alpha (preserving existing v6 generators)."""
    if len(gen_records) == 0:
        print("No generator records to insert")
        return

    print(f"\n{'='*60}")
    print(f"GENERATORS SUMMARY: {len(gen_records)} entries")
    print(f"{'='*60}")
    by_carrier = gen_records.groupby('carrier')['p_nom'].sum().sort_values(ascending=False)
    for carrier, cap in by_carrier.items():
        count = (gen_records['carrier'] == carrier).sum()
        print(f"  {carrier:20s}: {count:6d} entries, {float(cap)/1000:8.2f} GW")
    print(f"  {'TOTAL':20s}: {len(gen_records):6d} entries, {float(gen_records['p_nom'].sum())/1000:8.2f} GW")

    if dry_run:
        print("\n  [DRY RUN — no database writes]")
        return

    # Delete only MaStR generators (ID >= GEN_START_ID), keep v6 originals
    with engine.begin() as conn:
        result = conn.execute(text(
            "DELETE FROM grid.egon_etrago_generator "
            "WHERE scn_name = :scn AND generator_id >= :start_id"
        ), {'scn': ALPHA, 'start_id': GEN_START_ID})
        print(f"  Deleted {result.rowcount} existing MaStR generators")

    gen_records.to_sql(
        'egon_etrago_generator', engine, schema='grid',
        if_exists='append', index=False,
    )
    print(f"  Inserted {len(gen_records)} generator records")

    # Verify
    with engine.begin() as conn:
        result = conn.execute(text("""
            SELECT carrier, COUNT(*), ROUND(SUM(p_nom)::numeric, 1) as mw
            FROM grid.egon_etrago_generator WHERE scn_name = :scn
            GROUP BY carrier ORDER BY mw DESC
        """), {'scn': ALPHA}).fetchall()
        print("\n  Verification — all generators in grid_alpha:")
        for row in result:
            print(f"    {row[0]:20s}: {row[1]:6d} entries, {float(row[2])/1000:8.2f} GW")


def insert_storage(storage_records, engine, dry_run):
    """Insert storage into grid_alpha."""
    if len(storage_records) == 0:
        print("No storage records to insert")
        return

    print(f"\n{'='*60}")
    print(f"STORAGE SUMMARY: {len(storage_records)} entries")
    print(f"{'='*60}")
    by_carrier = storage_records.groupby('carrier')['p_nom'].sum().sort_values(ascending=False)
    for carrier, cap in by_carrier.items():
        count = (storage_records['carrier'] == carrier).sum()
        print(f"  {carrier:20s}: {count:6d} entries, {float(cap)/1000:8.2f} GW")

    if dry_run:
        print("\n  [DRY RUN — no database writes]")
        return

    with engine.begin() as conn:
        result = conn.execute(text(
            "DELETE FROM grid.egon_etrago_storage "
            "WHERE scn_name = :scn AND storage_id >= :start_id"
        ), {'scn': ALPHA, 'start_id': STORAGE_START_ID})
        print(f"  Deleted {result.rowcount} existing MaStR storage")

    storage_records.to_sql(
        'egon_etrago_storage', engine, schema='grid',
        if_exists='append', index=False,
    )
    print(f"  Inserted {len(storage_records)} storage records")


def sanity_checks(engine):
    """Run sanity checks on grid_alpha generators and storage."""
    print(f"\n{'='*60}")
    print("SANITY CHECKS")
    print(f"{'='*60}")

    with engine.begin() as conn:
        # Check for invalid bus references
        orphan_gens = conn.execute(text("""
            SELECT COUNT(*) FROM grid.egon_etrago_generator g
            WHERE g.scn_name = :scn
              AND NOT EXISTS (
                SELECT 1 FROM grid.egon_etrago_bus b
                WHERE b.scn_name = :scn AND b.bus_id = g.bus
              )
        """), {'scn': ALPHA}).scalar()
        print(f"  Generators with invalid bus: {orphan_gens}")

        orphan_storage = conn.execute(text("""
            SELECT COUNT(*) FROM grid.egon_etrago_storage s
            WHERE s.scn_name = :scn
              AND NOT EXISTS (
                SELECT 1 FROM grid.egon_etrago_bus b
                WHERE b.scn_name = :scn AND b.bus_id = s.bus
              )
        """), {'scn': ALPHA}).scalar()
        print(f"  Storage with invalid bus: {orphan_storage}")

        # Check no single 110kV bus > 5 GW
        hot_buses = conn.execute(text("""
            SELECT g.bus, b.v_nom, ROUND(SUM(g.p_nom)::numeric, 1) as total_mw
            FROM grid.egon_etrago_generator g
            JOIN grid.egon_etrago_bus b ON b.bus_id = g.bus AND b.scn_name = g.scn_name
            WHERE g.scn_name = :scn AND b.v_nom = 110
            GROUP BY g.bus, b.v_nom
            HAVING SUM(g.p_nom) > 5000
            ORDER BY total_mw DESC
            LIMIT 10
        """), {'scn': ALPHA}).fetchall()
        if hot_buses:
            print(f"  WARNING: {len(hot_buses)} 110kV buses with > 5 GW:")
            for row in hot_buses:
                print(f"    Bus {row[0]}: {float(row[2])/1000:.2f} GW")
        else:
            print(f"  No 110kV bus exceeds 5 GW ✓")

        # Total capacity summary
        totals = conn.execute(text("""
            SELECT 'generators' as type, COUNT(*), ROUND(SUM(p_nom)::numeric, 1) as mw
            FROM grid.egon_etrago_generator WHERE scn_name = :scn
            UNION ALL
            SELECT 'storage', COUNT(*), ROUND(SUM(p_nom)::numeric, 1)
            FROM grid.egon_etrago_storage WHERE scn_name = :scn
            UNION ALL
            SELECT 'loads', COUNT(*), ROUND(SUM(p_set)::numeric, 1)
            FROM grid.egon_etrago_load WHERE scn_name = :scn
        """), {'scn': ALPHA}).fetchall()
        print(f"\n  Grid Alpha totals:")
        for row in totals:
            val = float(row[2]) / 1000 if row[2] else 0
            print(f"    {row[0]:12s}: {row[1]:6d} entries, {val:8.2f} GW")


def main():
    parser = argparse.ArgumentParser(
        description='Build grid_alpha: v6 grid + all MaStR generators'
    )
    parser.add_argument('--apply', action='store_true',
                        help='Write to database (default: dry run)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Dry run (no DB writes) — this is the default')
    parser.add_argument('--skip-copy', action='store_true',
                        help='Skip v6→grid_alpha copy (if already done)')
    parser.add_argument('--technology',
                        choices=['wind', 'conventional', 'solar', 'biomass',
                                 'hydro', 'storage', 'all'],
                        default='all', help='Technology to process (default: all)')
    args = parser.parse_args()

    dry_run = not args.apply

    print("=" * 70)
    print("BUILD GRID ALPHA: v6 Grid + All MaStR Generators")
    print("=" * 70)
    print(f"Source scenario: {V6}")
    print(f"Target scenario: {ALPHA}")
    print(f"Mode: {'DRY RUN' if dry_run else 'APPLY (writing to DB)'}")
    print(f"Technology: {args.technology}")
    print()

    engine = create_engine(DB_URI)

    # Step 1: Copy v6 → grid_alpha
    if not args.skip_copy:
        print("Step 1: Copy v6 → grid_alpha")
        if dry_run:
            print("  [DRY RUN — skipping copy, will use v6 buses for matching]")
        else:
            copy_v6_to_alpha(engine)
    else:
        print("Step 1: Skipped (--skip-copy)")

    # Step 2: Load buses for spatial matching
    print("\nStep 2: Load buses and municipality centroids")
    # Use grid_alpha buses if they exist, otherwise v6
    source_scn = ALPHA if not dry_run or args.skip_copy else V6
    query = text("""
        SELECT bus_id, x, y, v_nom, country
        FROM grid.egon_etrago_bus
        WHERE scn_name = :scn AND country = 'DE' AND v_nom IN (110, 220, 380)
    """)
    with engine.begin() as conn:
        buses = pd.read_sql(query, conn, params={'scn': source_scn})
    print(f"Loaded {len(buses)} DE buses from {source_scn}")
    print(f"  By voltage: {buses.groupby('v_nom').size().to_dict()}")

    muni_centroids = load_municipality_centroids(engine)
    matcher = SpatialMatcher(buses)

    # Load buses per municipality for spreading MV/LV capacity
    buses_per_muni = load_buses_per_municipality(engine, source_scn)

    # Step 3-8: Allocate each technology
    all_gen_agg = []
    all_storage_agg = []

    if args.technology in ['wind', 'all']:
        agg = allocate_wind(engine, matcher, muni_centroids, buses_per_muni)
        if len(agg) > 0:
            all_gen_agg.append(agg)

    if args.technology in ['conventional', 'all']:
        agg = allocate_conventional(engine, matcher, muni_centroids, buses_per_muni)
        if len(agg) > 0:
            all_gen_agg.append(agg)

    if args.technology in ['solar', 'all']:
        agg = allocate_solar(engine, matcher, muni_centroids, buses_per_muni)
        if len(agg) > 0:
            all_gen_agg.append(agg)

    if args.technology in ['biomass', 'all']:
        agg = allocate_biomass(engine, matcher, muni_centroids, buses_per_muni)
        if len(agg) > 0:
            all_gen_agg.append(agg)

    if args.technology in ['hydro', 'all']:
        agg = allocate_hydro(engine, matcher, muni_centroids, buses_per_muni)
        if len(agg) > 0:
            all_gen_agg.append(agg)

    if args.technology in ['storage', 'all']:
        agg = allocate_storage(engine, matcher, muni_centroids, buses_per_muni)
        if len(agg) > 0:
            all_storage_agg.append(agg)

    # Step 9: Final aggregation and insertion
    print(f"\n{'='*70}")
    print("Step 9: Final aggregation and database insertion")
    print(f"{'='*70}")

    # Generators
    if all_gen_agg:
        combined_gen = pd.concat(all_gen_agg, ignore_index=True)
        if 'is_aggregated' not in combined_gen.columns:
            combined_gen['is_aggregated'] = False
        # Final re-aggregation by (bus_id, carrier)
        combined_gen = combined_gen.groupby(['bus_id', 'carrier']).agg(
            p_nom_mw=('p_nom_mw', 'sum'),
            n_units=('n_units', 'sum'),
            is_aggregated=('is_aggregated', 'any'),
        ).reset_index()

        gen_records = prepare_generator_records(combined_gen, GEN_START_ID)
        insert_generators(gen_records, engine, dry_run)

        # Save metadata (n_units, is_aggregated) for the map
        meta = combined_gen[['bus_id', 'carrier', 'n_units', 'is_aggregated', 'p_nom_mw']].copy()
        meta['component'] = 'generator'
        meta.to_csv('results/grid_alpha_gen_metadata.csv', index=False)
        print(f"  Saved generator metadata ({len(meta)} rows)")

    # Storage
    if all_storage_agg:
        combined_storage = pd.concat(all_storage_agg, ignore_index=True)
        if 'is_aggregated' not in combined_storage.columns:
            combined_storage['is_aggregated'] = False
        combined_storage = combined_storage.groupby(['bus_id', 'carrier']).agg(
            p_nom_mw=('p_nom_mw', 'sum'),
            n_units=('n_units', 'sum'),
            is_aggregated=('is_aggregated', 'any'),
        ).reset_index()

        storage_records = prepare_storage_records(combined_storage, STORAGE_START_ID)
        insert_storage(storage_records, engine, dry_run)

        # Save storage metadata
        meta = combined_storage[['bus_id', 'carrier', 'n_units', 'is_aggregated', 'p_nom_mw']].copy()
        meta['component'] = 'storage'
        meta.to_csv('results/grid_alpha_stor_metadata.csv', index=False)
        print(f"  Saved storage metadata ({len(meta)} rows)")

    # Sanity checks
    if not dry_run:
        sanity_checks(engine)

    print(f"\n{'='*70}")
    print("BUILD GRID ALPHA COMPLETE")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
