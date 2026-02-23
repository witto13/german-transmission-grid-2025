#!/usr/bin/env python3
"""
Generator Mapping: MaStR power plants → eGon2025 grid buses

Maps all generation technologies from the German Marktstammdatenregister (MaStR)
to eGon2025 grid buses:

- Wind (onshore + offshore, detected via WindAnLandOderAufSee field)
- Solar (HV-connected spatially matched + distributed by municipality)
- Conventional (gas, coal, lignite, oil)
- Hydro (run-of-river, storage)
- Biomass (biogas, solid biomass, biogenic waste)
- Storage (batteries, pumped hydro → storage table)

Matching approach:
1. Units with coordinates → spatial match to nearest bus at target voltage
2. Units without coordinates → distribute by municipality (like loads)

Usage:
    python scripts/generator_mapping.py [--dry-run] [--technology TECH]
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

# Add project root to path for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from scripts.utils.spatial_matching import SpatialMatcher, calculate_spatial_confidence


# Database connection
DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
SCENARIO = 'eGon2025'

# Maximum search distances by voltage level (km)
MAX_DISTANCE_KM = {
    380: 50.0,
    220: 30.0,
    110: 20.0,
}

# Substation preference distance threshold (km)
SUBSTATION_PREFERENCE_KM = 5.0

# Voltage thresholds for distributed generators (MW capacity)
# Based on Hülk et al. (2017) - same as loads
VOLTAGE_THRESHOLDS = {
    380: 120.0,  # > 120 MW → 380 kV
    220: 20.0,   # > 20 MW → 220 kV
    110: 0.0,    # All else → 110 kV
}

# MaStR Spannungsebene mapping to v_nom
VOLTAGE_MAPPING = {
    'Höchstspannung': 380,
    'Hochspannung': 110,
    'Umspannung zur Höchstspannung': 220,
    'Umspannung zur Hochspannung': 110,
    'Mittelspannung': 110,
    'Umspannung zur Mittelspannung': 110,
    'Niederspannung': 110,
    'Umspannung zur Niederspannung': 110,
}

# Carrier mapping for conventional fuels (from MaStR combustion_extended)
CONVENTIONAL_CARRIER_MAPPING = {
    # Gas
    'Erdgas, Erdölgas': 'gas',
    'Erdgas': 'gas',
    'Grubengas': 'gas',
    'Andere Gase': 'gas',
    'Sonstige hergestellte Gase': 'gas',
    'Hochofengas, Konvertergas': 'gas',
    'Raffineriegas': 'gas',
    'Kokereigas': 'gas',
    # Coal
    'Steinkohlen': 'coal',
    'Steinkohle': 'coal',
    'Steinkohlenbriketts': 'coal',
    'Steinkohlenkoks': 'coal',
    # Lignite
    'Rohbraunkohlen': 'lignite',
    'Braunkohle': 'lignite',
    'Braunkohlenbriketts': 'lignite',
    'Wirbelschichtkohle': 'lignite',
    # Oil
    'Heizöl, leicht': 'oil',
    'Heizöl, schwer': 'oil',
    'Dieselkraftstoff': 'oil',
    'Andere Mineralölprodukte': 'oil',
    'Mineralölprodukte': 'oil',
    # Waste
    'Abfall (Hausmüll, Siedl.abf.)': 'waste',
    'Industrieabfall': 'waste',
    'nicht biogener Abfall': 'waste',
    # Other
    'Dampf (zum Beispiel Prozesswärme)': 'other',
    'Wärme': 'other',
    'Wasserstoff': 'hydrogen',
}

# Carrier mapping for biomass fuels
BIOMASS_CARRIER_MAPPING = {
    'Biogas': 'biogas',
    'Biomethan (Bioerdgas)': 'biogas',
    'Klärgas': 'biogas',
    'Deponiegas': 'biogas',
    'feste Biomasse': 'biomass',
    'Holzgas': 'biomass',
    'Altholz, Gebrauchtholz, Holz(sperr)müll': 'biomass',
    'biogener Abfall': 'biomass',
    'Pflanzenöl': 'biomass',
}

# Carrier mapping for hydro types
HYDRO_CARRIER_MAPPING = {
    'Laufwasseranlage': 'run_of_river',
    'Speicherwasseranlage': 'reservoir',
    'Wasserkraftanlage in Trinkwassersystem': 'run_of_river',
    'Wasserkraftanlage in Brauchwassersystem': 'run_of_river',
    'Abwasserkraftanlage': 'run_of_river',
    'Meeresenergie': 'run_of_river',
}

# Storage carrier mapping
STORAGE_CARRIER_MAPPING = {
    'Lithium-Batterie': 'battery',
    'Blei-Batterie': 'battery',
    'Sonstige Batterie': 'battery',
    'Redox-Flow-Batterie': 'battery',
    'Hochtemperaturbatterie': 'battery',
    'Nickel-Cadmium- / Nickel-Metallhydridbatterie': 'battery',
    'Pumpspeicheranlage ohne natürlichen Zufluss': 'pumped_hydro',
    'Pumpspeicheranlage mit natürlichem Zufluss': 'pumped_hydro',
}


def load_buses_with_substations(engine) -> pd.DataFrame:
    """Load buses for eGon2025 with substation name information."""
    print(f"Loading buses with substation info for scenario '{SCENARIO}'...")

    query = f"""
        WITH bus_substations AS (
            SELECT
                b.bus_id, b.x, b.y, b.v_nom, b.country,
                COALESCE(s.subst_name, '') as subst_name,
                CASE WHEN s.bus_id IS NOT NULL THEN TRUE ELSE FALSE END as is_substation
            FROM grid.egon_etrago_bus b
            LEFT JOIN grid.egon_ehv_substation s ON b.bus_id = s.bus_id
            WHERE b.scn_name = '{SCENARIO}'
              AND b.v_nom IN (220, 380)
              AND b.country = 'DE'

            UNION ALL

            SELECT
                b.bus_id, b.x, b.y, b.v_nom, b.country,
                COALESCE(s.subst_name, '') as subst_name,
                CASE WHEN s.bus_id IS NOT NULL THEN TRUE ELSE FALSE END as is_substation
            FROM grid.egon_etrago_bus b
            LEFT JOIN grid.egon_hvmv_substation s ON b.bus_id = s.bus_id
            WHERE b.scn_name = '{SCENARIO}'
              AND b.v_nom = 110
              AND b.country = 'DE'
        )
        SELECT * FROM bus_substations
    """
    buses = pd.read_sql(query, engine)
    buses['subst_name'] = buses['subst_name'].replace('NA', '').fillna('')

    print(f"  Total buses: {len(buses)}")
    print(f"  By voltage: {buses.groupby('v_nom').size().to_dict()}")

    return buses


def load_municipality_centroids(engine) -> pd.DataFrame:
    """Load municipality centroids for distributing generators without coordinates."""
    print("Loading municipality centroids...")

    query = """
        SELECT ags, gen as name,
               ST_X(ST_Centroid(ST_Union(geometry))) as lon,
               ST_Y(ST_Centroid(ST_Union(geometry))) as lat
        FROM boundaries.vg250_gem
        GROUP BY ags, gen
    """
    centroids = pd.read_sql(query, engine)
    centroids['ags'] = centroids['ags'].astype(str).str.zfill(8)
    print(f"  Loaded {len(centroids)} municipality centroids")
    return centroids


def assign_voltage_level(capacity_mw: float) -> int:
    """Assign voltage level based on capacity using Hülk et al. (2017) thresholds."""
    if capacity_mw > VOLTAGE_THRESHOLDS[380]:
        return 380
    elif capacity_mw > VOLTAGE_THRESHOLDS[220]:
        return 220
    else:
        return 110


def load_wind_generators(engine) -> pd.DataFrame:
    """Load wind generators from MaStR database with onshore/offshore distinction."""
    print("Loading wind generators from MaStR...")

    query = """
        SELECT
            "EinheitMastrNummer" as unit_id,
            "Nettonennleistung" / 1000.0 as p_nom,
            "Breitengrad" as lat,
            "Laengengrad" as lon,
            "Gemeindeschluessel" as ags,
            "WindAnLandOderAufSee" as location_type
        FROM mastr.wind_extended
        WHERE "EinheitBetriebsstatus" = 'In Betrieb'
          AND "Nettonennleistung" > 0
    """
    df = pd.read_sql(query, engine)

    # Map location to carrier
    df['carrier'] = df['location_type'].map({
        'Windkraft an Land': 'onwind',
        'Windkraft auf See': 'offwind'
    }).fillna('onwind')

    # All wind connects at 110 kV (aggregated from lower voltages)
    df['v_nom'] = 110

    # Summary
    by_carrier = df.groupby('carrier')['p_nom'].sum()
    print(f"  Loaded {len(df)} wind units")
    print(f"  Onshore: {by_carrier.get('onwind', 0)/1000:.2f} GW")
    print(f"  Offshore: {by_carrier.get('offwind', 0)/1000:.2f} GW")

    return df


def load_solar_generators(engine) -> pd.DataFrame:
    """Load all solar generators from MaStR database."""
    print("Loading solar generators from MaStR...")

    query = """
        SELECT
            "EinheitMastrNummer" as unit_id,
            "Nettonennleistung" / 1000.0 as p_nom,
            "Breitengrad" as lat,
            "Laengengrad" as lon,
            "Gemeindeschluessel" as ags
        FROM mastr.solar_extended
        WHERE "EinheitBetriebsstatus" = 'In Betrieb'
          AND "Nettonennleistung" > 0
    """
    df = pd.read_sql(query, engine)

    df['carrier'] = 'solar'
    df['v_nom'] = 110  # Distributed solar aggregates to 110 kV

    # Check coordinate coverage
    has_coords = df['lat'].notna() & df['lon'].notna()
    print(f"  Loaded {len(df)} solar units, {df['p_nom'].sum()/1000:.2f} GW total")
    print(f"  With coordinates: {has_coords.sum()} ({df.loc[has_coords, 'p_nom'].sum()/1000:.2f} GW)")
    print(f"  Without coordinates: {(~has_coords).sum()} ({df.loc[~has_coords, 'p_nom'].sum()/1000:.2f} GW)")

    return df


def load_conventional_generators(engine) -> pd.DataFrame:
    """Load conventional generators from MaStR database."""
    print("Loading conventional generators from MaStR...")

    query = """
        SELECT
            "EinheitMastrNummer" as unit_id,
            "Nettonennleistung" / 1000.0 as p_nom,
            "Breitengrad" as lat,
            "Laengengrad" as lon,
            "Gemeindeschluessel" as ags,
            "Hauptbrennstoff" as fuel_type,
            "AnschlussAnHoechstOderHochSpannung" as hv_connection
        FROM mastr.combustion_extended
        WHERE "EinheitBetriebsstatus" = 'In Betrieb'
          AND "Nettonennleistung" >= 1000
    """
    df = pd.read_sql(query, engine)

    # Map carrier
    df['carrier'] = df['fuel_type'].map(CONVENTIONAL_CARRIER_MAPPING).fillna('other')

    # Assign voltage based on capacity (Hülk et al. thresholds)
    df['v_nom'] = df['p_nom'].apply(assign_voltage_level)

    # Summary
    by_carrier = df.groupby('carrier')['p_nom'].sum().sort_values(ascending=False)
    print(f"  Loaded {len(df)} conventional units (>= 1 MW)")
    print(f"  Total capacity: {df['p_nom'].sum()/1000:.2f} GW")
    for carrier, cap in by_carrier.head(5).items():
        print(f"    {carrier}: {cap/1000:.2f} GW")

    return df


def load_hydro_generators(engine) -> pd.DataFrame:
    """Load hydro generators from MaStR database."""
    print("Loading hydro generators from MaStR...")

    query = """
        SELECT
            "EinheitMastrNummer" as unit_id,
            "Nettonennleistung" / 1000.0 as p_nom,
            "Breitengrad" as lat,
            "Laengengrad" as lon,
            "Gemeindeschluessel" as ags,
            "ArtDerWasserkraftanlage" as hydro_type
        FROM mastr.hydro_extended
        WHERE "EinheitBetriebsstatus" = 'In Betrieb'
          AND "Nettonennleistung" > 0
    """
    df = pd.read_sql(query, engine)

    # Map carrier
    df['carrier'] = df['hydro_type'].map(HYDRO_CARRIER_MAPPING).fillna('run_of_river')

    # Assign voltage based on capacity
    df['v_nom'] = df['p_nom'].apply(assign_voltage_level)

    print(f"  Loaded {len(df)} hydro units, {df['p_nom'].sum()/1000:.2f} GW total")
    print(f"  By type: {df.groupby('carrier')['p_nom'].sum().to_dict()}")

    return df


def load_biomass_generators(engine) -> pd.DataFrame:
    """Load biomass generators from MaStR database."""
    print("Loading biomass generators from MaStR...")

    query = """
        SELECT
            "EinheitMastrNummer" as unit_id,
            "Nettonennleistung" / 1000.0 as p_nom,
            "Breitengrad" as lat,
            "Laengengrad" as lon,
            "Gemeindeschluessel" as ags,
            "Hauptbrennstoff" as fuel_type
        FROM mastr.biomass_extended
        WHERE "EinheitBetriebsstatus" = 'In Betrieb'
          AND "Nettonennleistung" > 0
    """
    df = pd.read_sql(query, engine)

    # Map carrier
    df['carrier'] = df['fuel_type'].map(BIOMASS_CARRIER_MAPPING).fillna('biomass')

    # Assign voltage based on capacity
    df['v_nom'] = df['p_nom'].apply(assign_voltage_level)

    print(f"  Loaded {len(df)} biomass units, {df['p_nom'].sum()/1000:.2f} GW total")

    return df


def load_storage_units(engine) -> pd.DataFrame:
    """Load storage units from MaStR database."""
    print("Loading storage units from MaStR...")

    query = """
        SELECT
            "EinheitMastrNummer" as unit_id,
            "Nettonennleistung" / 1000.0 as p_nom,
            "NutzbareSpeicherkapazitaet" as max_hours,
            "Breitengrad" as lat,
            "Laengengrad" as lon,
            "Gemeindeschluessel" as ags,
            "Batterietechnologie" as battery_type,
            "Pumpspeichertechnologie" as pump_type
        FROM mastr.storage_extended
        WHERE "EinheitBetriebsstatus" = 'In Betrieb'
          AND "Nettonennleistung" > 0
    """
    df = pd.read_sql(query, engine)

    # Determine carrier from battery or pump type
    def get_storage_carrier(row):
        if pd.notna(row['pump_type']) and row['pump_type'] != '':
            return 'pumped_hydro'
        elif pd.notna(row['battery_type']) and row['battery_type'] != '':
            return 'battery'
        else:
            return 'battery'  # Default to battery

    df['carrier'] = df.apply(get_storage_carrier, axis=1)

    # Filter to larger units for HV model (>= 100 kW for batteries, >= 1 MW for pumped hydro)
    df = df[
        ((df['carrier'] == 'battery') & (df['p_nom'] >= 0.1)) |
        ((df['carrier'] == 'pumped_hydro') & (df['p_nom'] >= 1.0))
    ].copy()

    # Assign voltage based on capacity
    df['v_nom'] = df['p_nom'].apply(assign_voltage_level)

    print(f"  Loaded {len(df)} storage units (>= 100 kW batteries, >= 1 MW pumped hydro)")
    print(f"  By type: {df.groupby('carrier')['p_nom'].sum().to_dict()}")

    return df


def spatial_match_generators(
    generators: pd.DataFrame,
    buses: pd.DataFrame,
    technology: str
) -> pd.DataFrame:
    """Spatially match generators to buses using KD-tree."""
    print(f"Spatial matching for {technology} generators...")

    valid_coords = generators['lat'].notna() & generators['lon'].notna()
    print(f"  {valid_coords.sum()} / {len(generators)} have valid coordinates")

    matcher = SpatialMatcher(buses)
    substation_buses = set(buses[buses['is_substation']]['bus_id'].values)

    results = []
    for idx, row in generators.iterrows():
        result = {
            'gen_idx': idx,
            'bus_id': None,
            'distance_km': None,
            'match_method': 'unmatched',
            'confidence': 0.0,
        }

        if not valid_coords.loc[idx]:
            results.append(result)
            continue

        lon, lat = row['lon'], row['lat']
        target_v = int(row['v_nom'])
        max_dist = MAX_DISTANCE_KM.get(target_v, 20.0)

        candidates = matcher.find_nearest_k(lon, lat, target_v, k=5, max_distance_km=max_dist)

        if candidates:
            best_bus, best_dist = candidates[0]
            for bus_id, dist in candidates:
                if bus_id in substation_buses and dist <= SUBSTATION_PREFERENCE_KM:
                    best_bus, best_dist = bus_id, dist
                    break

            result['bus_id'] = best_bus
            result['distance_km'] = best_dist
            result['match_method'] = 'spatial_voltage'
            result['confidence'] = calculate_spatial_confidence(best_dist, target_v)
        else:
            fallback = matcher.find_nearest_any_voltage(lon, lat, max_distance_km=100.0)
            if fallback:
                bus_id, dist, matched_v = fallback
                result['bus_id'] = bus_id
                result['distance_km'] = dist
                result['match_method'] = 'spatial_fallback'
                result['confidence'] = calculate_spatial_confidence(dist, matched_v) * 0.8

        results.append(result)

    results_df = pd.DataFrame(results).set_index('gen_idx')

    matched = generators.copy()
    matched['bus_id'] = results_df['bus_id']
    matched['distance_km'] = results_df['distance_km']
    matched['match_method'] = results_df['match_method']
    matched['confidence'] = results_df['confidence']

    matched_count = matched['bus_id'].notna().sum()
    print(f"  Spatially matched: {matched_count} / {valid_coords.sum()}")

    return matched


def distribute_by_municipality(
    generators: pd.DataFrame,
    buses: pd.DataFrame,
    muni_centroids: pd.DataFrame,
    technology: str
) -> pd.DataFrame:
    """Distribute generators without coordinates by municipality."""
    print(f"Distributing {technology} generators without coordinates by municipality...")

    # Get generators without coordinates but with municipality
    no_coords = generators['lat'].isna() | generators['lon'].isna()
    has_muni = generators['ags'].notna() & (generators['ags'] != '')
    to_distribute = generators[no_coords & has_muni].copy()

    if len(to_distribute) == 0:
        print("  No generators to distribute")
        return generators

    # Aggregate by municipality and carrier
    to_distribute['ags'] = to_distribute['ags'].astype(str).str.zfill(8)
    muni_agg = to_distribute.groupby(['ags', 'carrier']).agg({
        'p_nom': 'sum',
        'unit_id': 'count',
        'v_nom': 'first'
    }).reset_index()
    muni_agg = muni_agg.rename(columns={'unit_id': 'n_units'})

    # Merge with centroids
    muni_agg = muni_agg.merge(muni_centroids[['ags', 'lon', 'lat']], on='ags', how='left')

    # Assign voltage based on aggregated capacity
    muni_agg['v_nom'] = muni_agg['p_nom'].apply(assign_voltage_level)

    print(f"  Aggregated to {len(muni_agg)} municipality-carrier combinations")
    print(f"  Total capacity: {muni_agg['p_nom'].sum()/1000:.2f} GW")

    # Spatial match the aggregated entries
    matcher = SpatialMatcher(buses)

    bus_ids = []
    distances = []
    for idx, row in muni_agg.iterrows():
        if pd.isna(row['lon']) or pd.isna(row['lat']):
            bus_ids.append(None)
            distances.append(None)
            continue

        target_v = int(row['v_nom'])
        result = matcher.find_nearest(row['lon'], row['lat'], target_v, MAX_DISTANCE_KM.get(target_v, 50.0))
        if result:
            bus_ids.append(result[0])
            distances.append(result[1])
        else:
            # Fallback to any voltage
            fallback = matcher.find_nearest_any_voltage(row['lon'], row['lat'], max_distance_km=100.0)
            if fallback:
                bus_ids.append(fallback[0])
                distances.append(fallback[1])
            else:
                bus_ids.append(None)
                distances.append(None)

    muni_agg['bus_id'] = bus_ids
    muni_agg['distance_km'] = distances
    muni_agg['match_method'] = 'municipality_distributed'
    muni_agg['confidence'] = 0.5

    # Update original generators with matched info
    # For the ones we distributed, mark as distributed
    generators = generators.copy()
    distributed_mask = no_coords & has_muni
    generators.loc[distributed_mask, 'match_method'] = 'municipality_distributed'

    matched = muni_agg['bus_id'].notna().sum()
    print(f"  Matched {matched} / {len(muni_agg)} municipality aggregations")

    return generators, muni_agg


def aggregate_by_bus(matched_generators: pd.DataFrame, technology: str) -> pd.DataFrame:
    """Aggregate matched generators by bus for database insertion."""
    print(f"Aggregating {technology} generators by bus...")

    matched = matched_generators[matched_generators['bus_id'].notna()].copy()

    if len(matched) == 0:
        print("  No matched generators to aggregate")
        return pd.DataFrame()

    aggregated = matched.groupby(['bus_id', 'carrier']).agg({
        'p_nom': 'sum',
        'unit_id': 'count',
        'distance_km': 'mean',
        'confidence': 'mean',
    }).reset_index()
    aggregated = aggregated.rename(columns={'unit_id': 'n_units'})

    print(f"  Aggregated to {len(aggregated)} bus-carrier combinations")
    print(f"  Total capacity: {aggregated['p_nom'].sum()/1000:.2f} GW")

    return aggregated


def prepare_generator_records(aggregated: pd.DataFrame, start_id: int = 1) -> pd.DataFrame:
    """Prepare generator records for database insertion."""
    if len(aggregated) == 0:
        return pd.DataFrame()

    records = pd.DataFrame({
        'scn_name': SCENARIO,
        'generator_id': range(start_id, start_id + len(aggregated)),
        'bus': aggregated['bus_id'].astype(int),
        'control': 'PQ',
        'type': '',
        'carrier': aggregated['carrier'],
        'p_nom': aggregated['p_nom'],
        'p_nom_extendable': False,
        'p_nom_min': 0.0,
        'p_nom_max': np.inf,
        'p_min_pu': 0.0,
        'p_max_pu': 1.0,
        'p_set': None,
        'q_set': None,
        'sign': 1.0,
        'marginal_cost': 0.0,
        'build_year': 0,
        'lifetime': np.inf,
        'capital_cost': 0.0,
        'efficiency': 1.0,
        'committable': False,
        'start_up_cost': 0.0,
        'shut_down_cost': 0.0,
        'min_up_time': 0,
        'min_down_time': 0,
        'up_time_before': 0,
        'down_time_before': 0,
        'ramp_limit_up': np.nan,
        'ramp_limit_down': np.nan,
        'ramp_limit_start_up': 1.0,
        'ramp_limit_shut_down': 1.0,
        'e_nom_max': np.inf,
    })

    return records


def prepare_storage_records(aggregated: pd.DataFrame, start_id: int = 1) -> pd.DataFrame:
    """Prepare storage records for database insertion."""
    if len(aggregated) == 0:
        return pd.DataFrame()

    # Set max_hours based on carrier (pumped hydro has more storage)
    max_hours = aggregated['carrier'].map({
        'battery': 4.0,
        'pumped_hydro': 8.0,
    }).fillna(4.0)

    records = pd.DataFrame({
        'scn_name': SCENARIO,
        'storage_id': range(start_id, start_id + len(aggregated)),
        'bus': aggregated['bus_id'].astype(int),
        'control': 'PQ',
        'type': '',
        'carrier': aggregated['carrier'],
        'p_nom': aggregated['p_nom'],
        'p_nom_extendable': False,
        'p_nom_min': 0.0,
        'p_nom_max': np.inf,
        'p_min_pu': -1.0,  # Can discharge
        'p_max_pu': 1.0,   # Can charge
        'p_set': None,
        'q_set': None,
        'sign': 1.0,
        'marginal_cost': 0.0,
        'capital_cost': 0.0,
        'build_year': 0,
        'lifetime': np.inf,
        'state_of_charge_initial': 0.0,
        'cyclic_state_of_charge': True,
        'state_of_charge_set': None,
        'max_hours': max_hours.values,
        'efficiency_store': 0.9,
        'efficiency_dispatch': 0.9,
        'standing_loss': 0.0,
        'inflow': 0.0,
    })

    return records


def write_generators_to_database(db_records: pd.DataFrame, engine, dry_run: bool = True):
    """Write generator records to database."""
    if len(db_records) == 0:
        print("No generator records to write")
        return

    if dry_run:
        print(f"\n=== DRY RUN - Would insert {len(db_records)} generator records ===")
        print("Capacity by carrier:")
        for carrier, cap in db_records.groupby('carrier')['p_nom'].sum().sort_values(ascending=False).items():
            print(f"  {carrier}: {cap/1000:.2f} GW")
        return

    print(f"\n=== Writing {len(db_records)} generator records to database ===")

    with engine.begin() as conn:
        delete_query = text(f"DELETE FROM grid.egon_etrago_generator WHERE scn_name = '{SCENARIO}'")
        result = conn.execute(delete_query)
        print(f"Deleted {result.rowcount} existing generator records")

    db_records.to_sql(
        'egon_etrago_generator',
        engine,
        schema='grid',
        if_exists='append',
        index=False
    )
    print(f"Inserted {len(db_records)} generator records")

    # Verify
    with engine.begin() as conn:
        verify_query = text(f"""
            SELECT carrier, COUNT(*), ROUND(SUM(p_nom)::numeric, 1) as capacity_mw
            FROM grid.egon_etrago_generator
            WHERE scn_name = '{SCENARIO}'
            GROUP BY carrier ORDER BY capacity_mw DESC
        """)
        result = conn.execute(verify_query).fetchall()
        print("\nVerification - Generators by carrier:")
        for row in result:
            print(f"  {row[0]}: {row[1]} units, {row[2]/1000:.2f} GW")


def write_storage_to_database(db_records: pd.DataFrame, engine, dry_run: bool = True):
    """Write storage records to database."""
    if len(db_records) == 0:
        print("No storage records to write")
        return

    if dry_run:
        print(f"\n=== DRY RUN - Would insert {len(db_records)} storage records ===")
        print("Capacity by carrier:")
        for carrier, cap in db_records.groupby('carrier')['p_nom'].sum().sort_values(ascending=False).items():
            print(f"  {carrier}: {cap/1000:.2f} GW")
        return

    print(f"\n=== Writing {len(db_records)} storage records to database ===")

    with engine.begin() as conn:
        delete_query = text(f"DELETE FROM grid.egon_etrago_storage WHERE scn_name = '{SCENARIO}'")
        result = conn.execute(delete_query)
        print(f"Deleted {result.rowcount} existing storage records")

    db_records.to_sql(
        'egon_etrago_storage',
        engine,
        schema='grid',
        if_exists='append',
        index=False
    )
    print(f"Inserted {len(db_records)} storage records")


def main():
    parser = argparse.ArgumentParser(description='Map MaStR generators to eGon2025 grid buses')
    parser.add_argument('--dry-run', action='store_true', help='Do not write to database')
    parser.add_argument('--technology', choices=['wind', 'solar', 'conventional', 'hydro', 'biomass', 'storage', 'all'],
                        default='all', help='Technology to process')
    args = parser.parse_args()

    print("=" * 70)
    print("Generator Mapping: MaStR → eGon2025 Grid (Full Version)")
    print("=" * 70)
    print(f"Scenario: {SCENARIO}")
    print(f"Technology: {args.technology}")
    print(f"Dry run: {args.dry_run}")
    print()

    engine = create_engine(DB_URI)

    # Load buses and municipality centroids
    buses = load_buses_with_substations(engine)
    muni_centroids = load_municipality_centroids(engine)

    all_gen_aggregated = []
    all_storage_aggregated = []

    # Process each technology
    if args.technology in ['wind', 'all']:
        print("\n" + "=" * 50)
        print("WIND")
        print("=" * 50)
        wind = load_wind_generators(engine)
        wind_matched = spatial_match_generators(wind, buses, 'wind')
        wind_agg = aggregate_by_bus(wind_matched, 'wind')
        all_gen_aggregated.append(wind_agg)

    if args.technology in ['solar', 'all']:
        print("\n" + "=" * 50)
        print("SOLAR")
        print("=" * 50)
        solar = load_solar_generators(engine)

        # Split into coordinated and distributed
        has_coords = solar['lat'].notna() & solar['lon'].notna()

        # Spatial match those with coordinates
        solar_coords = solar[has_coords].copy()
        solar_coords_matched = spatial_match_generators(solar_coords, buses, 'solar (with coords)')
        solar_coords_agg = aggregate_by_bus(solar_coords_matched, 'solar (with coords)')

        # Distribute those without coordinates by municipality
        solar_no_coords = solar[~has_coords].copy()
        _, solar_muni_agg = distribute_by_municipality(solar_no_coords, buses, muni_centroids, 'solar (distributed)')

        # Combine
        all_gen_aggregated.append(solar_coords_agg)
        if len(solar_muni_agg) > 0:
            solar_muni_agg = solar_muni_agg[solar_muni_agg['bus_id'].notna()].copy()
            solar_muni_agg['unit_id'] = 'distributed'
            all_gen_aggregated.append(solar_muni_agg[['bus_id', 'carrier', 'p_nom', 'n_units', 'distance_km', 'confidence']])

    if args.technology in ['conventional', 'all']:
        print("\n" + "=" * 50)
        print("CONVENTIONAL")
        print("=" * 50)
        conv = load_conventional_generators(engine)
        conv_matched = spatial_match_generators(conv, buses, 'conventional')
        conv_agg = aggregate_by_bus(conv_matched, 'conventional')
        all_gen_aggregated.append(conv_agg)

    if args.technology in ['hydro', 'all']:
        print("\n" + "=" * 50)
        print("HYDRO")
        print("=" * 50)
        hydro = load_hydro_generators(engine)
        hydro_matched = spatial_match_generators(hydro, buses, 'hydro')
        hydro_agg = aggregate_by_bus(hydro_matched, 'hydro')
        all_gen_aggregated.append(hydro_agg)

    if args.technology in ['biomass', 'all']:
        print("\n" + "=" * 50)
        print("BIOMASS")
        print("=" * 50)
        biomass = load_biomass_generators(engine)
        biomass_matched = spatial_match_generators(biomass, buses, 'biomass')
        biomass_agg = aggregate_by_bus(biomass_matched, 'biomass')
        all_gen_aggregated.append(biomass_agg)

    if args.technology in ['storage', 'all']:
        print("\n" + "=" * 50)
        print("STORAGE")
        print("=" * 50)
        storage = load_storage_units(engine)
        storage_matched = spatial_match_generators(storage, buses, 'storage')
        storage_agg = aggregate_by_bus(storage_matched, 'storage')
        all_storage_aggregated.append(storage_agg)

    # Combine generator results
    if all_gen_aggregated:
        combined_gen = pd.concat(all_gen_aggregated, ignore_index=True)
        print(f"\n{'=' * 50}")
        print(f"COMBINED GENERATORS: {len(combined_gen)} bus-carrier combinations")
        print(f"Total capacity: {combined_gen['p_nom'].sum()/1000:.2f} GW")

        gen_records = prepare_generator_records(combined_gen)
        write_generators_to_database(gen_records, engine, dry_run=args.dry_run)

    # Combine storage results
    if all_storage_aggregated:
        combined_storage = pd.concat(all_storage_aggregated, ignore_index=True)
        print(f"\n{'=' * 50}")
        print(f"COMBINED STORAGE: {len(combined_storage)} bus-carrier combinations")
        print(f"Total capacity: {combined_storage['p_nom'].sum()/1000:.2f} GW")

        storage_records = prepare_storage_records(combined_storage)
        write_storage_to_database(storage_records, engine, dry_run=args.dry_run)

    print("\n=== Complete ===")


if __name__ == '__main__':
    main()
