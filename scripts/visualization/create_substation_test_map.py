#!/usr/bin/env python3
"""
Substation Test Map: 100 random substations with connected generator visualization.

Picks 100 random substations (70x110kV, 15x220kV, 15x380kV) from grid_alpha.
Click any substation to see installed capacity and individual generator locations
(MaStR SEL groups) plotted on the map with dashed connection lines.

Usage:
    python scripts/create_substation_test_map.py [--output PATH]
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from sqlalchemy import create_engine

DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
ALPHA = 'grid_beta'
KM_LON = 71.5
KM_LAT = 111.0

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


def pick_substations(engine, pick_all=False):
    """Pick substations with generation or load. --all for every bus, else 100 random."""
    active = pd.read_sql(f"""
        SELECT DISTINCT b.bus_id, b.x as lon, b.y as lat, b.v_nom
        FROM grid.egon_etrago_bus b
        WHERE b.scn_name = '{ALPHA}'
          AND b.country = 'DE'
          AND b.v_nom IN (110, 220, 380)
          AND (
            EXISTS (
              SELECT 1 FROM grid.egon_etrago_generator g
              WHERE g.scn_name = '{ALPHA}' AND g.bus = b.bus_id AND g.p_nom > 0
            )
            OR EXISTS (
              SELECT 1 FROM grid.egon_etrago_load l
              WHERE l.scn_name = '{ALPHA}' AND l.bus = b.bus_id
                AND l.carrier NOT LIKE 'export%%'
            )
          )
    """, engine)

    if pick_all:
        print(f"All active DE buses: {len(active)} "
              f"(110kV:{(active['v_nom']==110).sum()}, "
              f"220kV:{(active['v_nom']==220).sum()}, "
              f"380kV:{(active['v_nom']==380).sum()})")
        return active

    b110 = active[active['v_nom'] == 110].sample(min(70, len(active[active['v_nom'] == 110])), random_state=42)
    b220 = active[active['v_nom'] == 220].sample(min(15, len(active[active['v_nom'] == 220])), random_state=42)
    b380 = active[active['v_nom'] == 380].sample(min(15, len(active[active['v_nom'] == 380])), random_state=42)

    selected = pd.concat([b110, b220, b380])
    print(f"  Selected: {len(selected)} buses "
          f"(110kV:{len(b110)}, 220kV:{len(b220)}, 380kV:{len(b380)})")
    return selected


def get_bus_summary(engine, bus_ids):
    """Get generator/storage summary for buses using metadata CSVs."""
    print(f"Building bus summaries for {len(bus_ids)} buses...")
    bus_id_set = set(int(b) for b in bus_ids)

    # Load all generators/storage for the scenario (no per-bus filtering)
    gens = pd.read_sql(f"""
        SELECT bus, carrier, SUM(p_nom) as p_nom, COUNT(*) as n_db
        FROM grid.egon_etrago_generator
        WHERE scn_name = '{ALPHA}'
        GROUP BY bus, carrier
    """, engine)

    stor = pd.read_sql(f"""
        SELECT bus, carrier, SUM(p_nom) as p_nom, COUNT(*) as n_db
        FROM grid.egon_etrago_storage
        WHERE scn_name = '{ALPHA}'
        GROUP BY bus, carrier
    """, engine)

    # Build metadata lookups from CSVs (vectorised, not row-by-row)
    meta_lookup = {}
    for path in ['results/grid_alpha_gen_metadata.csv', 'results/grid_alpha_offwind_metadata.csv']:
        if Path(path).exists():
            df = pd.read_csv(path)
            for (bid, carrier), grp in df.groupby(['bus_id', 'carrier']):
                key = (int(bid), carrier)
                n = int(grp['n_units'].sum())
                a = bool(grp['is_aggregated'].any())
                if key in meta_lookup:
                    meta_lookup[key]['n'] += n
                    meta_lookup[key]['a'] = meta_lookup[key]['a'] or a
                else:
                    meta_lookup[key] = {'n': n, 'a': a}

    stor_lookup = {}
    if Path('results/grid_alpha_stor_metadata.csv').exists():
        df = pd.read_csv('results/grid_alpha_stor_metadata.csv')
        for (bid, carrier), grp in df.groupby(['bus_id', 'carrier']):
            key = (int(bid), carrier)
            stor_lookup[key] = {
                'n': int(grp['n_units'].sum()),
                'a': bool(grp['is_aggregated'].any()),
            }

    # Load domestic loads (exclude export_* carriers)
    loads = pd.read_sql(f"""
        SELECT bus, carrier, SUM(p_set) as p_set, COUNT(*) as n_db
        FROM grid.egon_etrago_load
        WHERE scn_name = '{ALPHA}'
          AND carrier NOT LIKE 'export%%'
        GROUP BY bus, carrier
    """, engine)

    # Index DataFrames by bus for fast lookup
    gens_grouped = {bus: grp for bus, grp in gens.groupby('bus')}
    stor_grouped = {bus: grp for bus, grp in stor.groupby('bus')}
    loads_grouped = {bus: grp for bus, grp in loads.groupby('bus')}

    summary = {}
    for bus_id in bus_ids:
        bus_id = int(bus_id)

        gen_entries = []
        if bus_id in gens_grouped:
            for _, g in gens_grouped[bus_id].iterrows():
                meta = meta_lookup.get((bus_id, g['carrier']), {})
                gen_entries.append({
                    'c': g['carrier'],
                    'p': round(float(g['p_nom']), 1),
                    'n': meta.get('n', int(g['n_db'])),
                    'a': meta.get('a', False),
                })
            gen_entries.sort(key=lambda x: -x['p'])

        stor_entries = []
        if bus_id in stor_grouped:
            for _, s in stor_grouped[bus_id].iterrows():
                meta = stor_lookup.get((bus_id, s['carrier']), {})
                stor_entries.append({
                    'c': s['carrier'],
                    'p': round(float(s['p_nom']), 1),
                    'n': meta.get('n', int(s['n_db'])),
                    'a': meta.get('a', False),
                })
            stor_entries.sort(key=lambda x: -x['p'])

        load_entries = []
        if bus_id in loads_grouped:
            for _, ld in loads_grouped[bus_id].iterrows():
                load_entries.append({
                    'c': ld['carrier'],
                    'p': round(float(ld['p_set']), 1),
                    'n': int(ld['n_db']),
                })
            load_entries.sort(key=lambda x: -x['p'])

        summary[bus_id] = {
            'g': gen_entries,
            's': stor_entries,
            'l': load_entries,
            'tg': round(sum(g['p'] for g in gen_entries), 1),
            'ts': round(sum(s['p'] for s in stor_entries), 1),
            'tl': round(sum(ld['p'] for ld in load_entries), 1),
        }

    n_gen = sum(1 for s in summary.values() if s['tg'] > 0)
    n_load = sum(1 for s in summary.values() if s['tl'] > 0)
    print(f"  Done. {n_gen} buses with generation, {n_load} buses with load")
    return summary


def _sel_extra_cols():
    """Common extra columns for COD, operator, and registry IDs in SEL queries."""
    return """
        MIN(w."Inbetriebnahmedatum")::text as cod_min,
        MAX(w."Inbetriebnahmedatum")::text as cod_max,
        MODE() WITHIN GROUP (ORDER BY m."Firmenname") as operator,
        MODE() WITHIN GROUP (ORDER BY w."EinheitBetriebsstatus") as status,
        MODE() WITHIN GROUP (ORDER BY gc."Spannungsebene") as voltage_level,
        MODE() WITHIN GROUP (ORDER BY l."Netzanschlusspunkte") as san,
        STRING_AGG(DISTINCT w."EinheitMastrNummer", ', ') as unit_ids
    """


def _sel_all_joins(alias='w'):
    """JOIN clauses for operator, location, and grid connection data."""
    return f"""
        LEFT JOIN mastr.market_actors m
            ON {alias}."AnlagenbetreiberMastrNummer" = m."MastrNummer"
        LEFT JOIN mastr.locations_extended l
            ON {alias}."LokationMastrNummer" = l."MastrNummer"
        LEFT JOIN mastr.grid_connections gc
            ON gc."NetzanschlusspunktMastrNummer" = SPLIT_PART(l."Netzanschlusspunkte", ', ', 1)
    """


def load_sel_groups(engine):
    """Load MaStR SEL groups for all technologies with coordinates, COD, and operator."""
    print("Loading MaStR SEL groups...")
    sels = []
    extra = _sel_extra_cols()
    joins = _sel_all_joins()

    # Wind onshore
    print("  Wind onshore...", end=' ', flush=True)
    wind = pd.read_sql(f"""
        SELECT w."LokationMastrNummer" as sel, COUNT(*) as n_units,
               SUM(w."Nettonennleistung")/1000.0 as p_nom_mw,
               AVG(w."Laengengrad") as lon, AVG(w."Breitengrad") as lat,
               MODE() WITHIN GROUP (ORDER BY w."NameWindpark") as name,
               {extra}
        FROM mastr.wind_extended w {joins}
        WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
          AND w."Nettonennleistung" > 0
          AND w."WindAnLandOderAufSee" = 'Windkraft an Land'
          AND w."Laengengrad" IS NOT NULL AND w."Breitengrad" IS NOT NULL
        GROUP BY w."LokationMastrNummer"
    """, engine)
    wind['carrier'] = 'onwind'
    sels.append(wind)
    print(f"{len(wind)} SELs, {wind['p_nom_mw'].sum()/1000:.1f} GW")

    # Wind offshore
    print("  Wind offshore...", end=' ', flush=True)
    offshore = pd.read_sql(f"""
        SELECT w."LokationMastrNummer" as sel, COUNT(*) as n_units,
               SUM(w."Nettonennleistung")/1000.0 as p_nom_mw,
               AVG(w."Laengengrad") as lon, AVG(w."Breitengrad") as lat,
               MODE() WITHIN GROUP (ORDER BY w."NameWindpark") as name,
               {extra}
        FROM mastr.wind_extended w {joins}
        WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
          AND w."Nettonennleistung" > 0
          AND w."WindAnLandOderAufSee" = 'Windkraft auf See'
          AND w."Laengengrad" IS NOT NULL AND w."Breitengrad" IS NOT NULL
        GROUP BY w."LokationMastrNummer"
    """, engine)
    offshore['carrier'] = 'offwind'
    sels.append(offshore)
    print(f"{len(offshore)} SELs, {offshore['p_nom_mw'].sum()/1000:.1f} GW")

    # Conventional (>= 1 MW)
    print("  Conventional...", end=' ', flush=True)
    conv = pd.read_sql(f"""
        SELECT w."LokationMastrNummer" as sel, COUNT(*) as n_units,
               SUM(w."Nettonennleistung")/1000.0 as p_nom_mw,
               AVG(w."Laengengrad") as lon, AVG(w."Breitengrad") as lat,
               MODE() WITHIN GROUP (ORDER BY w."Hauptbrennstoff") as fuel_type,
               MODE() WITHIN GROUP (ORDER BY w."NameStromerzeugungseinheit") as name,
               {extra}
        FROM mastr.combustion_extended w {joins}
        WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
          AND w."Nettonennleistung" >= 1000
          AND w."Laengengrad" IS NOT NULL AND w."Breitengrad" IS NOT NULL
        GROUP BY w."LokationMastrNummer"
    """, engine)
    conv['carrier'] = conv['fuel_type'].map(CONVENTIONAL_CARRIER_MAPPING).fillna('other')
    conv.drop(columns=['fuel_type'], inplace=True)
    sels.append(conv)
    print(f"{len(conv)} SELs, {conv['p_nom_mw'].sum()/1000:.1f} GW")

    # Solar HV+
    print("  Solar HV+...", end=' ', flush=True)
    solar = pd.read_sql(f"""
        SELECT w."LokationMastrNummer" as sel, COUNT(*) as n_units,
               SUM(w."Nettonennleistung")/1000.0 as p_nom_mw,
               AVG(w."Laengengrad") as lon, AVG(w."Breitengrad") as lat,
               MODE() WITHIN GROUP (ORDER BY w."NameStromerzeugungseinheit") as name,
               {extra}
        FROM mastr.solar_extended w {joins}
        WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
          AND w."Nettonennleistung" > 0
          AND gc."Spannungsebene" IN (
              'Höchstspannung', 'Umspannebene Höchstspannung/Hochspannung',
              'Hochspannung', 'Umspannebene Hochspannung/Mittelspannung')
          AND w."Laengengrad" IS NOT NULL AND w."Breitengrad" IS NOT NULL
        GROUP BY w."LokationMastrNummer"
    """, engine)
    solar['carrier'] = 'solar'
    sels.append(solar)
    print(f"{len(solar)} SELs, {solar['p_nom_mw'].sum()/1000:.1f} GW")

    # Biomass HV+
    print("  Biomass HV+...", end=' ', flush=True)
    biomass = pd.read_sql(f"""
        SELECT w."LokationMastrNummer" as sel, COUNT(*) as n_units,
               SUM(w."Nettonennleistung")/1000.0 as p_nom_mw,
               AVG(w."Laengengrad") as lon, AVG(w."Breitengrad") as lat,
               MODE() WITHIN GROUP (ORDER BY w."Hauptbrennstoff") as fuel_type,
               MODE() WITHIN GROUP (ORDER BY w."NameStromerzeugungseinheit") as name,
               {extra}
        FROM mastr.biomass_extended w {joins}
        WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
          AND w."Nettonennleistung" > 0
          AND gc."Spannungsebene" IN (
              'Höchstspannung', 'Umspannebene Höchstspannung/Hochspannung',
              'Hochspannung', 'Umspannebene Hochspannung/Mittelspannung')
          AND w."Laengengrad" IS NOT NULL AND w."Breitengrad" IS NOT NULL
        GROUP BY w."LokationMastrNummer"
    """, engine)
    biomass['carrier'] = biomass['fuel_type'].map(BIOMASS_CARRIER_MAPPING).fillna('biomass')
    biomass.drop(columns=['fuel_type'], inplace=True)
    sels.append(biomass)
    print(f"{len(biomass)} SELs, {biomass['p_nom_mw'].sum()/1000:.1f} GW")

    # Hydro
    print("  Hydro...", end=' ', flush=True)
    hydro = pd.read_sql(f"""
        SELECT w."LokationMastrNummer" as sel, COUNT(*) as n_units,
               SUM(w."Nettonennleistung")/1000.0 as p_nom_mw,
               AVG(w."Laengengrad") as lon, AVG(w."Breitengrad") as lat,
               MODE() WITHIN GROUP (ORDER BY w."ArtDerWasserkraftanlage") as hydro_type,
               MODE() WITHIN GROUP (ORDER BY w."NameStromerzeugungseinheit") as name,
               {extra}
        FROM mastr.hydro_extended w {joins}
        WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
          AND w."Nettonennleistung" > 0
          AND w."Laengengrad" IS NOT NULL AND w."Breitengrad" IS NOT NULL
        GROUP BY w."LokationMastrNummer"
    """, engine)
    hydro['carrier'] = hydro['hydro_type'].map(HYDRO_CARRIER_MAPPING).fillna('run_of_river')
    hydro.drop(columns=['hydro_type'], inplace=True)
    sels.append(hydro)
    print(f"{len(hydro)} SELs, {hydro['p_nom_mw'].sum()/1000:.1f} GW")

    # Storage (>= 100 kW)
    print("  Storage...", end=' ', flush=True)
    stor_raw = pd.read_sql(f"""
        SELECT w."LokationMastrNummer" as sel,
               w."Nettonennleistung"/1000.0 as p_nom,
               w."Laengengrad" as lon, w."Breitengrad" as lat,
               w."Inbetriebnahmedatum"::text as cod,
               m."Firmenname" as operator,
               w."NameStromerzeugungseinheit" as name,
               w."EinheitBetriebsstatus" as status,
               w."EinheitMastrNummer" as unit_id,
               l."Netzanschlusspunkte" as san_raw,
               gc."Spannungsebene" as voltage_level,
               CASE WHEN w."Pumpspeichertechnologie" IS NOT NULL
                     AND w."Pumpspeichertechnologie" != ''
               THEN 'pumped_hydro' ELSE 'battery' END as carrier
        FROM mastr.storage_extended w {joins}
        WHERE w."EinheitBetriebsstatus" = 'In Betrieb'
          AND w."Nettonennleistung" >= 100
          AND w."Laengengrad" IS NOT NULL AND w."Breitengrad" IS NOT NULL
    """, engine)
    storage = stor_raw.groupby('sel').agg(
        carrier=('carrier', lambda x: 'pumped_hydro' if 'pumped_hydro' in x.values else 'battery'),
        n_units=('p_nom', 'count'),
        p_nom_mw=('p_nom', 'sum'),
        lon=('lon', 'mean'),
        lat=('lat', 'mean'),
        cod_min=('cod', 'min'),
        cod_max=('cod', 'max'),
        operator=('operator', lambda x: x.mode().iloc[0] if len(x.mode()) > 0 else ''),
        name=('name', lambda x: x.mode().iloc[0] if len(x.mode()) > 0 else ''),
        status=('status', lambda x: x.mode().iloc[0] if len(x.mode()) > 0 else ''),
        voltage_level=('voltage_level', lambda x: x.mode().iloc[0] if len(x.mode()) > 0 else ''),
        san=('san_raw', lambda x: x.mode().iloc[0] if len(x.mode()) > 0 else ''),
        unit_ids=('unit_id', lambda x: ', '.join(x.dropna().unique()[:10])),
    ).reset_index()
    sels.append(storage)
    print(f"{len(storage)} SELs, {storage['p_nom_mw'].sum()/1000:.1f} GW")

    combined = pd.concat(sels, ignore_index=True)
    # Clean up NaN/NaT strings
    for col in ['name', 'operator', 'cod_min', 'cod_max', 'status',
                'voltage_level', 'san', 'unit_ids']:
        if col in combined.columns:
            combined[col] = combined[col].fillna('').astype(str).replace('None', '')
    print(f"  Total: {len(combined)} SEL groups")
    return combined


def match_sels_to_buses(sels, all_buses, selected_bus_ids):
    """Match SEL groups to ALL buses, then filter to selected 100.

    This replicates the real allocation: each SEL matches to its nearest
    bus among all ~7700, not just the 100 sample buses.  Only SELs that
    land on one of the selected buses are kept for visualization.
    """
    print("Matching SEL groups to ALL buses, then filtering to selected...")

    bus_coords = np.column_stack([
        all_buses['lon'].values * KM_LON,
        all_buses['lat'].values * KM_LAT
    ])
    tree = cKDTree(bus_coords)
    bus_ids = all_buses['bus_id'].values
    bus_vnoms = all_buses['v_nom'].values

    valid = sels.dropna(subset=['lon', 'lat']).copy()
    sel_coords = np.column_stack([
        valid['lon'].values * KM_LON,
        valid['lat'].values * KM_LAT
    ])

    distances, indices = tree.query(sel_coords, k=1)
    valid['bus_id'] = bus_ids[indices]
    valid['bus_v_nom'] = bus_vnoms[indices]
    valid['dist_km'] = distances

    # Apply voltage-specific distance thresholds
    max_d = valid['bus_v_nom'].map({110: 20.0, 220: 30.0, 380: 50.0}).fillna(20.0)
    matched = valid[valid['dist_km'] <= max_d].copy()

    print(f"  Total matched: {len(matched)} SEL groups to {matched['bus_id'].nunique()} buses")

    # Filter to only the 100 selected buses
    selected_set = set(int(b) for b in selected_bus_ids)
    filtered = matched[matched['bus_id'].apply(lambda x: int(x) in selected_set)]

    print(f"  On selected buses: {len(filtered)} SEL groups across {filtered['bus_id'].nunique()} buses")

    result = {}
    for bus_id, group in filtered.groupby('bus_id'):
        items = []
        for _, r in group.iterrows():
            # Truncate unit_ids to first 5 for JSON size
            ui_raw = str(r.get('unit_ids', '')) if pd.notna(r.get('unit_ids')) else ''
            ui_list = [x.strip() for x in ui_raw.split(',') if x.strip()]
            ui_show = ', '.join(ui_list[:5])
            if len(ui_list) > 5:
                ui_show += f' (+{len(ui_list)-5} more)'
            items.append({
                'a': round(float(r['lat']), 5),
                'o': round(float(r['lon']), 5),
                'c': r['carrier'],
                'p': round(float(r['p_nom_mw']), 1),
                'n': int(r['n_units']),
                'nm': str(r.get('name', ''))[:60] if pd.notna(r.get('name')) else '',
                'op': str(r.get('operator', ''))[:60] if pd.notna(r.get('operator')) else '',
                'cd': str(r.get('cod_min', ''))[:10] if pd.notna(r.get('cod_min')) else '',
                'cx': str(r.get('cod_max', ''))[:10] if pd.notna(r.get('cod_max')) else '',
                'sl': str(r.get('sel', ''))[:20] if pd.notna(r.get('sel')) else '',
                'sn': str(r.get('san', ''))[:20] if pd.notna(r.get('san')) else '',
                'vl': str(r.get('voltage_level', ''))[:50] if pd.notna(r.get('voltage_level')) else '',
                'st': str(r.get('status', ''))[:30] if pd.notna(r.get('status')) else '',
                'ui': ui_show,
            })
        result[int(bus_id)] = items
    return result


def load_grid_lines(engine):
    """Load grid lines with voltage information."""
    print("Loading grid lines...")

    lines = pd.read_sql(f"""
        SELECT l.bus0, l.bus1, b0.v_nom as v0, b1.v_nom as v1
        FROM grid.egon_etrago_line l
        JOIN grid.egon_etrago_bus b0 ON b0.bus_id = l.bus0 AND b0.scn_name = l.scn_name
        JOIN grid.egon_etrago_bus b1 ON b1.bus_id = l.bus1 AND b1.scn_name = l.scn_name
        WHERE l.scn_name = '{ALPHA}'
    """, engine)

    links = pd.read_sql(f"""
        SELECT bus0, bus1 FROM grid.egon_etrago_link WHERE scn_name = '{ALPHA}'
    """, engine)

    buses = pd.read_sql(f"""
        SELECT bus_id, x as lon, y as lat FROM grid.egon_etrago_bus WHERE scn_name = '{ALPHA}'
    """, engine)
    bus_lk = buses.set_index('bus_id')

    grid = {'110': [], '220': [], '380': [], 'hvdc': []}

    for _, ln in lines.iterrows():
        b0, b1 = int(ln['bus0']), int(ln['bus1'])
        if b0 not in bus_lk.index or b1 not in bus_lk.index:
            continue
        r0, r1 = bus_lk.loc[b0], bus_lk.loc[b1]
        coords = [[round(float(r0['lat']), 5), round(float(r0['lon']), 5)],
                   [round(float(r1['lat']), 5), round(float(r1['lon']), 5)]]
        v = max(float(ln['v0']), float(ln['v1']))
        if v >= 380:
            grid['380'].append(coords)
        elif v >= 220:
            grid['220'].append(coords)
        else:
            grid['110'].append(coords)

    for _, lk in links.iterrows():
        b0, b1 = int(lk['bus0']), int(lk['bus1'])
        if b0 in bus_lk.index and b1 in bus_lk.index:
            r0, r1 = bus_lk.loc[b0], bus_lk.loc[b1]
            grid['hvdc'].append([[round(float(r0['lat']), 5), round(float(r0['lon']), 5)],
                                 [round(float(r1['lat']), 5), round(float(r1['lon']), 5)]])

    print(f"  Lines: 110kV={len(grid['110'])}, 220kV={len(grid['220'])}, "
          f"380kV={len(grid['380'])}, HVDC={len(grid['hvdc'])}")
    return grid


def build_html(buses_df, bus_summary, gen_locations, grid_lines,
               subtitle='', n110=0, n220=0, n380=0):
    """Generate production-quality interactive HTML map."""

    bus_features = []
    for _, bus in buses_df.iterrows():
        bid = int(bus['bus_id'])
        s = bus_summary.get(bid, {'g': [], 's': [], 'l': [], 'tg': 0, 'ts': 0, 'tl': 0})
        bus_features.append({
            'id': bid,
            'lat': round(float(bus['lat']), 5),
            'lon': round(float(bus['lon']), 5),
            'v': int(bus['v_nom']),
            'tg': s['tg'], 'ts': s['ts'], 'tl': s['tl'],
            'g': s['g'], 's': s['s'], 'l': s['l'],
        })

    data_json = json.dumps({
        'buses': bus_features,
        'gens': gen_locations,
        'lines': grid_lines,
    }, separators=(',', ':'))

    total = len(bus_features)
    return (HTML_TEMPLATE
            .replace('__DATA__', data_json)
            .replace('__SUBTITLE__', subtitle)
            .replace('__N110__', str(n110))
            .replace('__N220__', str(n220))
            .replace('__N380__', str(n380))
            .replace('__NTOTAL__', str(total)))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Substation Explorer – Grid Beta</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root {
    --bg: #ffffff; --bg-glass: rgba(255,255,255,0.92);
    --text: #0f172a; --text2: #475569; --text3: #94a3b8;
    --border: #e2e8f0; --border2: #f1f5f9;
    --blue: #3b82f6; --green: #10b981; --red: #ef4444; --purple: #a855f7;
    --radius: 12px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter', system-ui, -apple-system, sans-serif; }
  #map { width: 100vw; height: 100vh; }

  /* ── Header ── */
  .header {
    position: absolute; top: 0; left: 0; right: 0; z-index: 1000;
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 20px;
    background: var(--bg-glass); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
    border-bottom: 1px solid var(--border);
  }
  .header-left { display: flex; align-items: baseline; gap: 10px; }
  .header h1 { font-size: 15px; font-weight: 700; color: var(--text); letter-spacing: -0.3px; }
  .header .sub { font-size: 12px; color: var(--text3); font-weight: 400; }
  .filters { display: flex; gap: 6px; }
  .fbtn {
    padding: 5px 14px; border-radius: 20px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text2); font-size: 11px; font-weight: 600;
    cursor: pointer; transition: all .2s ease; font-family: inherit; letter-spacing: 0.2px;
  }
  .fbtn:hover { border-color: var(--blue); color: var(--blue); }
  .fbtn.on { background: var(--text); border-color: var(--text); color: #fff; }

  /* ── Detail Panel ── */
  .panel {
    position: absolute; top: 0; right: 0; width: 370px; height: 100vh;
    background: var(--bg-glass); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
    border-left: 1px solid var(--border);
    transform: translateX(100%); transition: transform .3s cubic-bezier(.4,0,.2,1);
    z-index: 1001; display: flex; flex-direction: column;
  }
  .panel.open { transform: translateX(0); }
  .panel-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 54px 20px 14px; border-bottom: 1px solid var(--border);
  }
  .panel-head h2 { font-size: 15px; font-weight: 700; color: var(--text); }
  .panel-close {
    width: 28px; height: 28px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text2); font-size: 16px;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
    transition: all .15s ease; font-family: inherit;
  }
  .panel-close:hover { background: #f1f5f9; }
  .panel-body { flex: 1; overflow-y: auto; padding: 16px 20px; }
  .sec-title {
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.8px; color: var(--text3); margin: 16px 0 8px;
    display: flex; align-items: center; gap: 8px;
  }
  .sec-title:first-child { margin-top: 0; }
  .sec-total { font-size: 12px; font-weight: 700; color: var(--text); text-transform: none; letter-spacing: 0; }
  .crow {
    display: flex; align-items: center; padding: 7px 0;
    border-bottom: 1px solid var(--border2); gap: 8px;
  }
  .crow:last-child { border-bottom: none; }
  .cdot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .cname { flex: 1; font-size: 13px; color: var(--text); font-weight: 500; }
  .cval { font-size: 13px; font-weight: 700; color: var(--text); min-width: 70px; text-align: right; }
  .cmeta { font-size: 11px; color: var(--text3); min-width: 80px; text-align: right; }
  .agg {
    display: inline-block; background: #dbeafe; color: #1e40af;
    font-size: 9px; font-weight: 700; padding: 1px 5px; border-radius: 4px;
    letter-spacing: 0.5px; margin-left: 3px; vertical-align: middle;
  }
  .gen-note {
    margin-top: 14px; padding: 10px 12px; border-radius: 8px;
    background: #f8fafc; border: 1px solid var(--border);
    font-size: 11px; color: var(--text2); line-height: 1.5;
  }
  .gen-note strong { color: var(--text); }
  .empty-state {
    color: var(--text3); font-size: 13px; line-height: 1.6;
    padding: 30px 0; text-align: center;
  }

  /* ── Legend ── */
  .legend {
    position: absolute; bottom: 16px; left: 16px; z-index: 1000;
    background: var(--bg-glass); backdrop-filter: blur(12px);
    border-radius: var(--radius); border: 1px solid var(--border);
    padding: 12px 16px; font-size: 11px; max-height: calc(100vh - 80px); overflow-y: auto;
  }
  .legend h4 { font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.8px; color: var(--text3); margin-bottom: 6px; }
  .lrow { display: flex; align-items: center; gap: 8px; padding: 2px 0; color: var(--text2); }
  .lline { width: 20px; height: 0; flex-shrink: 0; }
  .lsep { border-top: 1px solid var(--border); margin: 5px 0; }
  .lsq { width: 8px; height: 8px; flex-shrink: 0; border: 1.5px solid; transform: rotate(45deg); }

  /* ── Tooltip ── */
  .leaflet-control-zoom { border: 1px solid var(--border) !important; border-radius: 8px !important; overflow: hidden; }
  .leaflet-control-zoom a { width: 32px !important; height: 32px !important; line-height: 32px !important;
    font-size: 16px !important; color: var(--text2) !important; border-color: var(--border) !important; }
  .leaflet-control-layers {
    border-radius: 8px !important; border: 1px solid var(--border) !important;
    font-family: 'Inter', system-ui !important; font-size: 12px !important;
    margin-top: 52px !important; /* push below header bar */
  }

  .bus-tooltip {
    background: var(--bg-glass) !important; backdrop-filter: blur(12px);
    border: 1px solid var(--border) !important; border-radius: 10px !important;
    padding: 0 !important; font-family: 'Inter', system-ui !important;
    font-size: 11px !important; color: var(--text) !important;
    box-shadow: 0 8px 24px rgba(0,0,0,0.12) !important; min-width: 180px;
  }
  .bus-tooltip .leaflet-tooltip-tip { display: none; }
  .tt-head { padding: 8px 12px; font-weight: 700; font-size: 12px;
    border-bottom: 1px solid var(--border); }
  .tt-body { padding: 6px 12px; }
  .tt-row { display: flex; align-items: center; gap: 6px; padding: 2px 0; font-size: 11px; }
  .tt-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .tt-name { flex: 1; color: var(--text2); }
  .tt-val { font-weight: 600; color: var(--text); }
  .tt-total { padding: 5px 12px; font-weight: 700; font-size: 12px; color: var(--text);
    border-top: 1px solid var(--border); }

  .gen-tooltip {
    background: rgba(15,23,42,0.92) !important; color: #fff !important;
    border: none !important; border-radius: 6px !important;
    padding: 5px 10px !important; font-family: 'Inter', system-ui !important;
    font-size: 11px !important; font-weight: 500 !important;
    box-shadow: 0 4px 12px rgba(0,0,0,0.25) !important;
  }
  .gen-tooltip .leaflet-tooltip-tip { display: none; }

  .gen-popup .leaflet-popup-content-wrapper {
    border-radius: 10px !important; border: 1px solid var(--border) !important;
    box-shadow: 0 8px 24px rgba(0,0,0,0.12) !important;
    font-family: 'Inter', system-ui !important;
  }
  .gen-popup .leaflet-popup-tip { border-top-color: var(--border) !important; }

  @keyframes pulse { 0%{transform:scale(1);opacity:.5} 100%{transform:scale(2.5);opacity:0} }
  .pulse-ring { animation: pulse 1.2s ease-out infinite; }
</style>
</head>
<body>
<div id="map"></div>

<div class="header">
  <div class="header-left">
    <h1>Substation Explorer</h1>
    <span class="sub">__SUBTITLE__</span>
  </div>
  <div class="filters">
    <button class="fbtn on" data-v="all">All (__NTOTAL__)</button>
    <button class="fbtn" data-v="110">110 kV (__N110__)</button>
    <button class="fbtn" data-v="220">220 kV (__N220__)</button>
    <button class="fbtn" data-v="380">380 kV (__N380__)</button>
  </div>
</div>

<div class="panel" id="panel">
  <div class="panel-head">
    <h2 id="ptitle">Select a substation</h2>
    <button class="panel-close" id="pclose">&times;</button>
  </div>
  <div class="panel-body" id="pbody">
    <p class="empty-state">Click any substation to explore<br>its connected generation.</p>
  </div>
</div>

<div class="legend" id="legend"></div>

<script>
const D = __DATA__;

const CC = {
  solar:'#f59e0b', onwind:'#3b82f6', offwind:'#06b6d4',
  gas:'#ef4444', coal:'#374151', lignite:'#92400e', oil:'#b91c1c',
  waste:'#6b7280', hydrogen:'#14b8a6', other:'#d97706',
  biogas:'#22c55e', biomass:'#15803d',
  run_of_river:'#0ea5e9', reservoir:'#0284c7',
  battery:'#8b5cf6', pumped_hydro:'#6d28d9',
  import_AT:'#9ca3af', import_CH:'#9ca3af', import_NL:'#9ca3af',
  import_FR:'#9ca3af', import_DK:'#9ca3af', import_PL:'#9ca3af',
  import_CZ:'#9ca3af', import_BE:'#9ca3af', import_SE:'#9ca3af',
  import_NO:'#9ca3af', import_LU:'#9ca3af',
  // Load carriers
  residential_cts:'#f97316', industry:'#6366f1', large_industry:'#ec4899',
};
const CN = {
  solar:'Solar PV', onwind:'Wind Onshore', offwind:'Wind Offshore',
  gas:'Natural Gas', coal:'Hard Coal', lignite:'Lignite', oil:'Oil',
  waste:'Waste', hydrogen:'Hydrogen', other:'Other Thermal',
  biogas:'Biogas', biomass:'Solid Biomass',
  run_of_river:'Run-of-River', reservoir:'Reservoir Hydro',
  battery:'Battery', pumped_hydro:'Pumped Hydro',
  // Load carriers
  residential_cts:'Residential + CTS', industry:'Industry', large_industry:'Large Industry',
};
const VC = {110:'#3b82f6', 220:'#10b981', 380:'#ef4444'};

function fmt(mw) { return Math.abs(mw)>=1000 ? (mw/1000).toFixed(1)+' GW' : mw.toFixed(1)+' MW'; }
function nu(n) { return n.toLocaleString(); }

// ── Basemaps ──
const osmBase = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{
  attribution:'&copy; OpenStreetMap', maxZoom:19});
const cartoPos = L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png',{
  attribution:'&copy; OSM &copy; CARTO', maxZoom:20});
const cartoVoy = L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}@2x.png',{
  attribution:'&copy; OSM &copy; CARTO', maxZoom:20});
const esriSat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{
  attribution:'&copy; Esri', maxZoom:19});

const baseMaps = {
  'OpenStreetMap': osmBase,
  'CartoDB Positron': cartoPos,
  'CartoDB Voyager': cartoVoy,
  'Satellite': esriSat,
};

// ── Map ──
const map = L.map('map', {zoomControl:false, preferCanvas:true}).setView([51.2,10.4],6);
L.control.zoom({position:'bottomright'}).addTo(map);
cartoPos.addTo(map); // default basemap

// ── Grid lines ──
function addLines(data, color, weight, opacity, dash) {
  const lg = L.layerGroup();
  data.forEach(c => L.polyline(c,{color,weight,opacity,dashArray:dash||null}).addTo(lg));
  return lg;
}
const gl110 = addLines(D.lines['110'],'#3b82f6', 1.0, 0.40);
const gl220 = addLines(D.lines['220'],'#10b981', 1.8, 0.55);
const gl380 = addLines(D.lines['380'],'#ef4444', 2.5, 0.65);
const glHVDC = addLines(D.lines.hvdc, '#a855f7', 2.2, 0.60,'8 5');
gl110.addTo(map); gl220.addTo(map); gl380.addTo(map); glHVDC.addTo(map);

// ── Layer control (basemaps + overlays) ──
const overlays = {
  '<span style="color:#ef4444">380 kV</span>': gl380,
  '<span style="color:#10b981">220 kV</span>': gl220,
  '<span style="color:#3b82f6">110 kV</span>': gl110,
  '<span style="color:#a855f7">HVDC</span>': glHVDC,
};
L.control.layers(baseMaps, overlays, {position:'topright', collapsed:true}).addTo(map);

// ── Build tooltip HTML for a bus ──
function buildTT(bus) {
  let h = '<div class="tt-head">Bus '+bus.id+' &middot; '+bus.v+' kV</div><div class="tt-body">';
  // Generation side
  const genItems = [...bus.g.filter(g=>!g.c.startsWith('import_')), ...bus.s];
  genItems.sort((a,b) => b.p - a.p);
  if (genItems.length > 0) {
    h += '<div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:#94a3b8;padding:2px 0">Generation</div>';
    genItems.slice(0, 4).forEach(g => {
      h += '<div class="tt-row"><div class="tt-dot" style="background:'+(CC[g.c]||'#666')+'"></div>';
      h += '<span class="tt-name">'+(CN[g.c]||g.c)+'</span>';
      h += '<span class="tt-val">'+fmt(g.p)+'</span></div>';
    });
    if (genItems.length > 4) h += '<div class="tt-row" style="color:var(--text3)">+'+(genItems.length-4)+' more</div>';
  }
  // Load side
  if (bus.l && bus.l.length > 0) {
    h += '<div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:#94a3b8;padding:4px 0 2px;border-top:1px solid #e2e8f0;margin-top:2px">Load</div>';
    bus.l.forEach(ld => {
      h += '<div class="tt-row"><div class="tt-dot" style="background:'+(CC[ld.c]||'#666')+'"></div>';
      h += '<span class="tt-name">'+(CN[ld.c]||ld.c)+'</span>';
      h += '<span class="tt-val">'+fmt(ld.p)+'</span></div>';
    });
  }
  h += '</div>';
  // Totals
  let totals = '';
  if (bus.tg+bus.ts > 0) totals += 'Gen '+fmt(bus.tg+bus.ts);
  if (bus.tl > 0) totals += (totals?' &middot; ':'')+'Load '+fmt(bus.tl);
  if (totals) h += '<div class="tt-total">'+totals+'</div>';
  return h;
}

// ── Bus markers (small clean diamonds) ──
const busMap = {}; D.buses.forEach(b => busMap[b.id]=b);
const markers = {};
const busLG = {110:L.layerGroup(), 220:L.layerGroup(), 380:L.layerGroup()};

D.buses.forEach(bus => {
  const vc = VC[bus.v] || '#666';

  // Small uniform marker — no capacity-based sizing
  const m = L.circleMarker([bus.lat, bus.lon], {
    radius: 3.5, fillColor: vc, fillOpacity: 1,
    color: '#fff', weight: 1, opacity: 0.9,
  });
  m._vc = vc;

  m.bindTooltip('', {direction:'top', offset:[0,-6], className:'bus-tooltip', sticky:false});
  m.on('mouseover', function() {
    this.setTooltipContent(buildTT(bus));
    if (selId !== bus.id) { this.setStyle({fillColor:'#fff', color:vc, weight:2.5}); this.setRadius(6); }
  });
  m.on('mouseout', function() {
    if (selId !== bus.id) { this.setStyle({fillColor:vc, color:'#fff', weight:1}); this.setRadius(3.5); }
  });
  m.on('click', function(e) {
    L.DomEvent.stopPropagation(e);
    selectBus(bus.id);
  });

  m.addTo(busLG[bus.v]);
  markers[bus.id] = m;
});
Object.values(busLG).forEach(lg => lg.addTo(map));

// ── Generator layer + selection state ──
const genLayer = L.layerGroup().addTo(map);
let selId = null, selMarker = null, ringMarker = null;

function selectBus(id) {
  // Reset previous
  if (selMarker) {
    selMarker.setStyle({fillColor:selMarker._vc, color:'#fff', weight:1, fillOpacity:1});
    selMarker.setRadius(3.5);
  }
  if (ringMarker) { map.removeLayer(ringMarker); ringMarker=null; }
  genLayer.clearLayers();

  if (selId === id) { selId=null; selMarker=null; closePanel(); return; }

  selId = id;
  selMarker = markers[id];
  const bus = busMap[id];
  const vc = selMarker._vc;

  // Selected style: inverted colors + larger
  selMarker.setStyle({fillColor:'#fff', color:vc, weight:3, fillOpacity:1});
  selMarker.setRadius(7);
  selMarker.bringToFront();

  // Animated ring
  ringMarker = L.circleMarker([bus.lat, bus.lon], {
    radius: 16, fillColor:vc, fillOpacity:0.1,
    color:vc, weight:1.5, opacity:0.4, className:'pulse-ring',
  }).addTo(map);

  // Plot connected generators
  const gens = D.gens[id] || [];
  gens.forEach(g => {
    const gc = CC[g.c] || '#666';
    // Dashed connection line (behind marker)
    L.polyline([[g.a, g.o],[bus.lat, bus.lon]], {
      color: gc, weight: 1.5, opacity: 0.4, dashArray:'6 4',
    }).addTo(genLayer);
    // Generator location marker — colored by carrier, clickable
    const gm = L.circleMarker([g.a, g.o], {
      radius: 6, fillColor: gc, fillOpacity: 0.9,
      color:'#fff', weight: 2,
    }).addTo(genLayer);
    // Hover: brief tooltip
    gm.bindTooltip(
      '<b>'+(CN[g.c]||g.c)+'</b> &middot; '+fmt(g.p),
      {direction:'top', className:'gen-tooltip'}
    );
    // Click: popup with full details
    const prow = (label, val) => '<div style="display:flex;justify-content:space-between;padding:3px 0">' +
      '<span style="color:#64748b">'+label+'</span><span style="font-weight:600;max-width:180px;text-align:right;word-break:break-all">'+val+'</span></div>';
    let pop = '<div style="font-family:Inter,system-ui;font-size:12px;min-width:240px;max-width:320px">';
    pop += '<div style="font-weight:700;font-size:13px;margin-bottom:4px">'+(CN[g.c]||g.c)+'</div>';
    if (g.nm) pop += '<div style="color:#475569;margin-bottom:6px">'+g.nm+'</div>';
    pop += '<div style="border-top:1px solid #e2e8f0;padding-top:4px">';
    pop += prow('Capacity', fmt(g.p));
    pop += prow('Units', nu(g.n));
    if (g.cd) {
      let cod = g.cd;
      if (g.cx && g.cx !== g.cd) cod += ' — '+g.cx;
      pop += prow('COD', cod);
    }
    if (g.op) pop += prow('Operator', g.op);
    if (g.st) pop += prow('Status', g.st);
    pop += '</div>';
    // Registry section
    if (g.sl || g.sn || g.vl || g.ui) {
      pop += '<div style="border-top:1px solid #e2e8f0;margin-top:4px;padding-top:4px">';
      pop += '<div style="font-weight:700;font-size:10px;text-transform:uppercase;letter-spacing:0.5px;color:#94a3b8;margin-bottom:3px">Registry</div>';
      if (g.sl) pop += prow('SEL (Location)', g.sl);
      if (g.sn) pop += prow('SAN (Grid Conn.)', g.sn);
      if (g.vl) pop += prow('Voltage Level', g.vl);
      if (g.ui) pop += '<div style="padding:3px 0"><span style="color:#64748b">Unit MaStR IDs</span>' +
        '<div style="font-size:10px;font-family:monospace;color:#334155;margin-top:2px;word-break:break-all;line-height:1.5">'+g.ui+'</div></div>';
      pop += '</div>';
    }
    pop += '</div>';
    gm.bindPopup(pop, {className:'gen-popup', maxWidth:340});
    gm.on('click', function(e) { L.DomEvent.stopPropagation(e); });
  });

  if (gens.length > 0) {
    const pts = [[bus.lat,bus.lon], ...gens.map(g=>[g.a,g.o])];
    map.fitBounds(L.latLngBounds(pts).pad(0.15), {maxZoom:13});
  }

  openPanel(id);
}

function openPanel(id) {
  const bus = busMap[id];
  const nGens = (D.gens[id]||[]).length;
  document.getElementById('ptitle').textContent = 'Bus '+id+' \u00b7 '+bus.v+' kV';

  let h = '';
  if (bus.g.length) {
    h += '<div class="sec-title">Generation <span class="sec-total">'+fmt(bus.tg)+'</span></div>';
    bus.g.forEach(g => {
      if (g.c.startsWith('import_')) return;
      h += '<div class="crow">';
      h += '<div class="cdot" style="background:'+(CC[g.c]||'#666')+'"></div>';
      h += '<span class="cname">'+(CN[g.c]||g.c)+(g.a?' <span class="agg">AGG</span>':'')+'</span>';
      h += '<span class="cval">'+fmt(g.p)+'</span>';
      h += '<span class="cmeta">'+nu(g.n)+' unit'+(g.n!==1?'s':'')+'</span>';
      h += '</div>';
    });
    const imports = bus.g.filter(g => g.c.startsWith('import_'));
    if (imports.length) {
      imports.forEach(g => {
        h += '<div class="crow"><div class="cdot" style="background:#9ca3af"></div>';
        h += '<span class="cname" style="color:var(--text3)">'+g.c.replace('import_','Import ')+'</span>';
        h += '<span class="cval" style="color:var(--text3)">'+fmt(g.p)+'</span>';
        h += '<span class="cmeta"></span></div>';
      });
    }
  }
  if (bus.s.length) {
    h += '<div class="sec-title">Storage <span class="sec-total">'+fmt(bus.ts)+'</span></div>';
    bus.s.forEach(s => {
      h += '<div class="crow">';
      h += '<div class="cdot" style="background:'+(CC[s.c]||'#666')+'"></div>';
      h += '<span class="cname">'+(CN[s.c]||s.c)+(s.a?' <span class="agg">AGG</span>':'')+'</span>';
      h += '<span class="cval">'+fmt(s.p)+'</span>';
      h += '<span class="cmeta">'+nu(s.n)+' unit'+(s.n!==1?'s':'')+'</span>';
      h += '</div>';
    });
  }
  if (bus.l && bus.l.length) {
    h += '<div class="sec-title">Load <span class="sec-total">'+fmt(bus.tl)+'</span></div>';
    const tlSum = bus.l.reduce((a,x)=>a+x.p, 0);
    bus.l.forEach(ld => {
      const pct = tlSum > 0 ? Math.round(100*ld.p/tlSum) : 0;
      h += '<div class="crow">';
      h += '<div class="cdot" style="background:'+(CC[ld.c]||'#666')+'"></div>';
      h += '<span class="cname">'+(CN[ld.c]||ld.c)+'</span>';
      h += '<span class="cval">'+fmt(ld.p)+'</span>';
      h += '<span class="cmeta">'+pct+'%</span>';
      h += '</div>';
    });
    // Mini bar showing load mix
    if (bus.l.length > 1 && tlSum > 0) {
      h += '<div style="display:flex;height:6px;border-radius:3px;overflow:hidden;margin-top:6px">';
      bus.l.forEach(ld => {
        const w = (100*ld.p/tlSum).toFixed(1);
        h += '<div style="width:'+w+'%;background:'+(CC[ld.c]||'#666')+'"></div>';
      });
      h += '</div>';
    }
  }
  if (nGens > 0) {
    h += '<div class="gen-note"><strong>'+nGens+' feed-in location'+(nGens>1?'s':'')+
         '</strong> shown on map with dashed lines. These are MaStR SEL groups '+
         '(grid feed-in locations) matched to this bus by proximity.</div>';
  } else {
    h += '<div class="gen-note">No individual generator locations found. '+
         'Capacity is from municipality-aggregated small-scale units (AGG).</div>';
  }
  document.getElementById('pbody').innerHTML = h;
  document.getElementById('panel').classList.add('open');
}

function closePanel() {
  document.getElementById('panel').classList.remove('open');
  genLayer.clearLayers();
  if (ringMarker) { map.removeLayer(ringMarker); ringMarker=null; }
  if (selMarker) {
    selMarker.setStyle({fillColor:selMarker._vc, color:'#fff', weight:1, fillOpacity:1});
    selMarker.setRadius(3.5);
    selMarker = null;
  }
  selId = null;
}

document.getElementById('pclose').addEventListener('click', closePanel);
map.on('click', function() { if (selId !== null) closePanel(); });

// ── Voltage filter buttons ──
document.querySelectorAll('.fbtn').forEach(btn => {
  btn.addEventListener('click', function() {
    document.querySelectorAll('.fbtn').forEach(b => b.classList.remove('on'));
    this.classList.add('on');
    const v = this.dataset.v;
    [110,220,380].forEach(vl => {
      if (v==='all' || parseInt(v)===vl) { map.addLayer(busLG[vl]); }
      else { map.removeLayer(busLG[vl]); }
    });
  });
});

// ── Legend ──
(function() {
  const vc = {110:0,220:0,380:0}; D.buses.forEach(b=>vc[b.v]++);
  let h = '<h4>Substations</h4>';
  h += '<div class="lrow"><div class="lsq" style="border-color:#3b82f6;background:#3b82f6"></div>110 kV ('+vc[110]+')</div>';
  h += '<div class="lrow"><div class="lsq" style="border-color:#10b981;background:#10b981"></div>220 kV ('+vc[220]+')</div>';
  h += '<div class="lrow"><div class="lsq" style="border-color:#ef4444;background:#ef4444"></div>380 kV ('+vc[380]+')</div>';
  h += '<div class="lsep"></div><h4>Grid Lines</h4>';
  h += '<div class="lrow"><div class="lline" style="border-top:2.5px solid #ef4444"></div>380 kV</div>';
  h += '<div class="lrow"><div class="lline" style="border-top:1.8px solid #10b981"></div>220 kV</div>';
  h += '<div class="lrow"><div class="lline" style="border-top:1px solid #3b82f6"></div>110 kV</div>';
  h += '<div class="lrow"><div class="lline" style="border-top:2px dashed #a855f7"></div>HVDC</div>';
  h += '<div class="lsep"></div><h4>Generation</h4>';
  ['solar','onwind','offwind','gas','coal','lignite','biogas','biomass','run_of_river','battery','pumped_hydro'].forEach(c => {
    h += '<div class="lrow"><div class="cdot" style="background:'+(CC[c]||'#666')+'"></div>'+(CN[c]||c)+'</div>';
  });
  h += '<div class="lsep"></div><h4>Load</h4>';
  ['residential_cts','industry','large_industry'].forEach(c => {
    h += '<div class="lrow"><div class="cdot" style="background:'+(CC[c]||'#666')+'"></div>'+(CN[c]||c)+'</div>';
  });
  document.getElementById('legend').innerHTML = h;
})();
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description='Substation test map')
    parser.add_argument('--output', default='results/substation_test_map.html')
    parser.add_argument('--all', action='store_true',
                        help='Include ALL active buses (not just 100 random)')
    args = parser.parse_args()

    engine = create_engine(DB_URI)

    selected = pick_substations(engine, pick_all=args.all)
    summary = get_bus_summary(engine, selected['bus_id'].tolist())

    # Load ALL DE buses for correct SEL matching
    print("Loading all DE buses for SEL matching...")
    all_buses = pd.read_sql(f"""
        SELECT bus_id, x as lon, y as lat, v_nom
        FROM grid.egon_etrago_bus
        WHERE scn_name = '{ALPHA}' AND country = 'DE' AND v_nom IN (110, 220, 380)
    """, engine)
    print(f"  {len(all_buses)} DE buses loaded")

    sels = load_sel_groups(engine)
    gen_locs = match_sels_to_buses(sels, all_buses, selected['bus_id'].tolist())
    grid = load_grid_lines(engine)

    # Dynamic subtitle
    n110 = int((selected['v_nom'] == 110).sum())
    n220 = int((selected['v_nom'] == 220).sum())
    n380 = int((selected['v_nom'] == 380).sum())
    if args.all:
        subtitle = f'{len(selected)} Active Substations &middot; Grid Beta'
    else:
        subtitle = f'100 Test Substations &middot; Grid Beta'

    html = build_html(selected, summary, gen_locs, grid,
                      subtitle=subtitle, n110=n110, n220=n220, n380=n380)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, 'w') as f:
        f.write(html)
    sz_mb = Path(args.output).stat().st_size / 1024 / 1024
    print(f"\nWrote map to {args.output} ({sz_mb:.1f} MB)")
    print("Open in browser to explore.")


if __name__ == '__main__':
    main()
