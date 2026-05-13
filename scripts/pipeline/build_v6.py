#!/usr/bin/env python3
"""
Build eGon2025v6 scenario from v5 with the following changes:
1. Copy v5 → v6
2. Add 19 phase-shifting transformers (PSTs) in series on 380kV lines
3. Add import/export generators and loads at 56 foreign buses
4. Generate 8,760-hour profiles for cross-border exchange & offshore wind

Usage:
    python scripts/build_v6.py [--apply]
"""

import argparse
import sys
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

DB_URI = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
V5 = 'eGon2025v5'
V6 = 'eGon2025v6'

HOURS = 8760  # full year

# --- PST definitions ---
# (name, substation_lon, substation_lat, v_high, v_low, r_ohm, x_ohm, s_nom_mva, angle_range_deg)
# Filtered: true PSTs with taps_rao angle range (excluding Doerpen converter trafos)
PSTS = [
    # 50Hertz — Röhrsdorf (2 units)
    ('PST Röhrsdorf 441',   12.815, 50.859, 380, 380, 0.100, 12.187, 1112.3, 10),
    ('PST Röhrsdorf 442',   12.815, 50.859, 380, 380, 0.097, 12.607, 1112.3, 10),
    # 50Hertz — Vierraden (2 units)
    ('PST Vierraden 441',   14.269, 53.098, 380, 380, 0.100, 12.187, 1112.3, 10),
    ('PST Vierraden 443',   14.269, 53.098, 380, 380, 0.097, 12.607, 1112.3, 10),
    # 50Hertz — Hamburg/Ost (4 units)
    ('PST Hamburg/Ost 441', 10.160, 53.555, 380, 380, 0.100, 12.187, 1112.3, 10),
    ('PST Hamburg/Ost 442', 10.160, 53.555, 380, 380, 0.100, 12.187, 1112.3, 10),
    ('PST Hamburg/Ost 443', 10.160, 53.555, 380, 380, 0.097, 12.607, 1112.3, 10),
    ('PST Hamburg/Ost 444', 10.160, 53.555, 380, 380, 0.097, 12.607, 1112.3, 10),
    # TenneT — Diele (2 units)
    ('PST Diele T441',       7.311, 53.126, 380, 380, 0.110, 12.320, 1230.8, 10),
    ('PST Diele T442',       7.311, 53.126, 380, 380, 0.110, 12.320, 1230.8, 10),
    # TenneT — Krempermarsch (4 units)
    ('PST Krempermarsch T441A', 9.501, 53.848, 380, 380, 0.100, 11.980, 1579.0, 10),
    ('PST Krempermarsch T441B', 9.501, 53.848, 380, 380, 0.100, 11.940, 1316.4, 10),
    ('PST Krempermarsch T442A', 9.501, 53.848, 380, 380, 0.100, 11.910, 1579.0, 10),
    ('PST Krempermarsch T442B', 9.501, 53.848, 380, 380, 0.100, 11.930, 1579.0, 10),
    # TenneT — Wuergau (4 units)
    ('PST Wuergau T441A',  11.107, 49.979, 380, 380, 0.100, 11.980, 1579.0, 10),
    ('PST Wuergau T441B',  11.107, 49.979, 380, 380, 0.100, 11.970, 1579.0, 10),
    ('PST Wuergau T442A',  11.107, 49.979, 380, 380, 0.100, 11.970, 1579.0, 10),
    ('PST Wuergau T442B',  11.107, 49.979, 380, 380, 0.100, 12.030, 1579.0, 10),
    # TransnetBW — Bürs (1 unit, 380/220 kV)
    ('PST Buers Trafo 37',  9.811, 47.143, 380, 220, 0.210, 19.320,  417.3, 17),
]

# --- Country exchange parameters ---
# (country, net_export_twh, max_import_mw, max_export_mw, marginal_cost_import)
# Positive net = DE exports; negative net = DE imports
COUNTRY_EXCHANGE = {
    'AT': {'net_twh': -3.0, 'max_import': 5000, 'max_export': 4000, 'mc_import': 55},
    'CH': {'net_twh': -2.0, 'max_import': 4000, 'max_export': 3500, 'mc_import': 50},
    'CZ': {'net_twh':  1.5, 'max_import': 2000, 'max_export': 3000, 'mc_import': 60},
    'DK': {'net_twh':  2.0, 'max_import': 2500, 'max_export': 4000, 'mc_import': 45},
    'FR': {'net_twh': -5.0, 'max_import': 3000, 'max_export': 2500, 'mc_import': 35},
    'LU': {'net_twh':  1.0, 'max_import':  500, 'max_export': 1500, 'mc_import': 65},
    'NL': {'net_twh':  5.0, 'max_import': 4000, 'max_export': 6000, 'mc_import': 70},
    'PL': {'net_twh':  3.0, 'max_import': 2000, 'max_export': 4000, 'mc_import': 55},
    'NO': {'net_twh': -2.0, 'max_import': 1400, 'max_export': 1400, 'mc_import': 40},
    'SE': {'net_twh': -0.5, 'max_import':  600, 'max_export':  600, 'mc_import': 38},
    'BE': {'net_twh':  0.5, 'max_import': 1000, 'max_export': 1000, 'mc_import': 65},
}

# Offshore HVDC link IDs (from v5: IDs 4-13)
OFFSHORE_HVDC_LINK_IDS = list(range(4, 14))

# --- ID ranges for v6 additions ---
NEW_BUS_START = 300000      # PST intermediate buses
NEW_TRAFO_START = 32000     # PST transformers
NEW_GEN_START = 1           # generators at foreign buses
NEW_LOAD_START = 1          # loads at foreign buses


def copy_v5_to_v6(engine):
    """Copy all v5 records to v6 scenario using DataFrame approach."""
    tables = [
        'egon_etrago_bus', 'egon_etrago_line', 'egon_etrago_transformer',
        'egon_etrago_link', 'egon_etrago_generator', 'egon_etrago_load',
        'egon_etrago_storage', 'egon_etrago_store',
    ]
    with engine.begin() as conn:
        for t in tables:
            conn.execute(text(f"DELETE FROM grid.{t} WHERE scn_name = '{V6}'"))
            print(f"  Cleared {t}")

        for t in tables:
            df = pd.read_sql(f"SELECT * FROM grid.{t} WHERE scn_name = '{V5}'", conn)
            if len(df) == 0:
                print(f"  {t}: 0 rows (skipped)")
                continue
            df['scn_name'] = V6
            df.to_sql(t, conn, schema='grid', if_exists='append', index=False)
            print(f"  {t}: {len(df)} rows copied")

    # Also clear timeseries
    with engine.begin() as conn:
        for ts in ['egon_etrago_generator_timeseries', 'egon_etrago_load_timeseries']:
            conn.execute(text(f"DELETE FROM grid.{ts} WHERE scn_name = '{V6}'"))
    print("Copied v5 → v6")


def find_nearest_380kv_bus(buses_380, lon, lat):
    """Find nearest 380kV bus to given coordinates."""
    dist = np.sqrt((buses_380['x'] - lon)**2 + (buses_380['y'] - lat)**2)
    idx = dist.idxmin()
    return int(buses_380.loc[idx, 'bus_id']), float(dist[idx])


def add_psts(engine):
    """Add 19 phase-shifting transformers in series on 380kV lines."""
    with engine.begin() as conn:
        # Load 380kV DE buses
        buses_380 = pd.read_sql(
            f"SELECT bus_id, x, y FROM grid.egon_etrago_bus "
            f"WHERE scn_name = '{V6}' AND v_nom = 380 AND country = 'DE'",
            conn
        )

        # Group PSTs by substation location
        from collections import defaultdict
        substation_psts = defaultdict(list)
        for pst in PSTS:
            key = (pst[1], pst[2])  # (lon, lat)
            substation_psts[key].append(pst)

        new_bus_id = NEW_BUS_START
        new_trafo_id = NEW_TRAFO_START
        pst_count = 0

        for (lon, lat), pst_list in substation_psts.items():
            matched_bus, dist = find_nearest_380kv_bus(buses_380, lon, lat)
            sub_name = pst_list[0][0].split(' ')[1]  # extract substation name
            print(f"  {sub_name}: matched to bus {matched_bus} (dist={dist:.4f}°)")

            # Get 380kV lines at this bus (only v_nom=380 for 380/380 PSTs)
            lines = pd.read_sql(
                f"SELECT line_id, bus0, bus1, s_nom FROM grid.egon_etrago_line "
                f"WHERE scn_name = '{V6}' AND v_nom = 380 "
                f"AND (bus0 = {matched_bus} OR bus1 = {matched_bus}) "
                f"ORDER BY s_nom DESC",
                conn
            )

            if len(lines) < len(pst_list):
                print(f"    WARNING: {sub_name} has {len(pst_list)} PSTs but only "
                      f"{len(lines)} 380kV lines. Will use available lines.")

            for i, pst in enumerate(pst_list):
                name, pst_lon, pst_lat, v_high, v_low, r_ohm, x_ohm, s_nom, angle_range = pst

                # Pick a line to split (cycle through available lines)
                line_idx = i % len(lines)
                line = lines.iloc[line_idx]
                lid = int(line['line_id'])
                other_bus = int(line['bus1']) if int(line['bus0']) == matched_bus else int(line['bus0'])

                # Get bus coordinates for intermediate bus
                bus_row = conn.execute(text(
                    f"SELECT x, y FROM grid.egon_etrago_bus "
                    f"WHERE scn_name = '{V6}' AND bus_id = {matched_bus}"
                )).fetchone()
                bus_x, bus_y = float(bus_row[0]), float(bus_row[1])

                # Create intermediate bus (slightly offset for visualization)
                int_bus_id = new_bus_id
                new_bus_id += 1

                # For PST Bürs (380/220), use 220kV for intermediate bus
                int_v_nom = v_low

                conn.execute(text(
                    f"INSERT INTO grid.egon_etrago_bus "
                    f"(scn_name, bus_id, v_nom, x, y, country) "
                    f"VALUES ('{V6}', {int_bus_id}, {int_v_nom}, "
                    f" {bus_x + 0.001 * (i + 1)}, {bus_y + 0.001 * (i + 1)}, 'DE')"
                ))

                # Reconnect line: change the endpoint from matched_bus to int_bus_id
                if int(line['bus0']) == matched_bus:
                    conn.execute(text(
                        f"UPDATE grid.egon_etrago_line SET bus0 = {int_bus_id} "
                        f"WHERE scn_name = '{V6}' AND line_id = {lid}"
                    ))
                else:
                    conn.execute(text(
                        f"UPDATE grid.egon_etrago_line SET bus1 = {int_bus_id} "
                        f"WHERE scn_name = '{V6}' AND line_id = {lid}"
                    ))

                # Convert impedance to per-unit: Z_pu = Z_ohm / Z_base
                # Z_base = V² / S_base where S_base = s_nom of the PST
                z_base = (v_high ** 2) / s_nom  # ohm
                x_pu = float(x_ohm / z_base)
                r_pu = float(r_ohm / z_base)

                # Tap ratio: 1.0 for same-voltage PSTs, v_high/v_low for 380/220
                tap_ratio = float(v_high / v_low) if v_high != v_low else 1.0

                # Insert PST transformer (bus0=matched_bus, bus1=intermediate)
                conn.execute(text(
                    f"INSERT INTO grid.egon_etrago_transformer "
                    f"(scn_name, trafo_id, bus0, bus1, x, r, s_nom, "
                    f" tap_ratio, phase_shift, s_nom_extendable, type, num_parallel) "
                    f"VALUES ('{V6}', {new_trafo_id}, {matched_bus}, {int_bus_id}, "
                    f" {x_pu}, {r_pu}, {s_nom}, "
                    f" {tap_ratio}, 0, false, 'PST', 1)"
                ))

                print(f"    {name}: trafo {new_trafo_id} on line {lid} "
                      f"(bus {matched_bus} → {int_bus_id} → {other_bus}), "
                      f"x_pu={x_pu:.5f}, r_pu={r_pu:.6f}, s_nom={s_nom}")

                new_trafo_id += 1
                pst_count += 1

                # Remove this line from future use at same substation
                # (mark used by setting s_nom to -1 in our local copy)
                lines.iloc[line_idx, lines.columns.get_loc('s_nom')] = -1

    print(f"  Added {pst_count} PSTs ({new_trafo_id - NEW_TRAFO_START} transformers, "
          f"{new_bus_id - NEW_BUS_START} intermediate buses)")
    return pst_count


def add_import_export(engine):
    """Add generators (import) and loads (export) at foreign buses."""
    gen_id = NEW_GEN_START
    load_id = NEW_LOAD_START

    with engine.begin() as conn:
        # Get foreign buses
        foreign_buses = pd.read_sql(
            f"SELECT bus_id, v_nom, x, y, country FROM grid.egon_etrago_bus "
            f"WHERE scn_name = '{V6}' AND country <> 'DE'",
            conn
        )

        # Get cross-border lines to compute capacity allocation
        cross_border = pd.read_sql(f"""
            SELECT l.line_id, l.bus0, l.bus1, l.s_nom,
                   b0.country as c0, b1.country as c1
            FROM grid.egon_etrago_line l
            JOIN grid.egon_etrago_bus b0
              ON b0.bus_id = l.bus0 AND b0.scn_name = l.scn_name
            JOIN grid.egon_etrago_bus b1
              ON b1.bus_id = l.bus1 AND b1.scn_name = l.scn_name
            WHERE l.scn_name = '{V6}'
              AND ((b0.country = 'DE' AND b1.country <> 'DE')
                OR (b1.country = 'DE' AND b0.country <> 'DE'))
        """, conn)

        # Get cross-border HVDC links
        cross_hvdc = pd.read_sql(f"""
            SELECT k.link_id, k.bus0, k.bus1, k.p_nom,
                   b0.country as c0, b1.country as c1
            FROM grid.egon_etrago_link k
            JOIN grid.egon_etrago_bus b0
              ON b0.bus_id = k.bus0 AND b0.scn_name = k.scn_name
            JOIN grid.egon_etrago_bus b1
              ON b1.bus_id = k.bus1 AND b1.scn_name = k.scn_name
            WHERE k.scn_name = '{V6}'
              AND b0.country <> b1.country
        """, conn)

        gen_count = 0
        load_count = 0

        for country, params in COUNTRY_EXCHANGE.items():
            country_buses = foreign_buses[foreign_buses['country'] == country]
            if len(country_buses) == 0:
                print(f"  {country}: no foreign buses found, skipping")
                continue

            # Compute capacity per bus from connected cross-border lines
            bus_capacity = {}
            for _, bus in country_buses.iterrows():
                bid = int(bus['bus_id'])
                # AC line capacity
                ac_cap = cross_border[
                    (cross_border['bus0'] == bid) | (cross_border['bus1'] == bid)
                ]['s_nom'].sum()
                # HVDC capacity
                hvdc_cap = cross_hvdc[
                    (cross_hvdc['bus0'] == bid) | (cross_hvdc['bus1'] == bid)
                ]['p_nom'].sum()
                bus_capacity[bid] = float(ac_cap + hvdc_cap)

            total_cap = sum(bus_capacity.values())
            if total_cap == 0:
                # Equal distribution if no direct lines found
                for bid in bus_capacity:
                    bus_capacity[bid] = 1.0
                total_cap = len(bus_capacity)

            # Distribute import/export capacity across buses
            max_import = params['max_import']
            max_export = params['max_export']
            mc = params['mc_import']

            for bid, cap in bus_capacity.items():
                frac = cap / total_cap

                # Import generator
                p_nom_import = max_import * frac
                if p_nom_import > 0:
                    conn.execute(text(
                        f"INSERT INTO grid.egon_etrago_generator "
                        f"(scn_name, generator_id, bus, carrier, p_nom, "
                        f" p_nom_extendable, p_min_pu, p_max_pu, "
                        f" marginal_cost, control, sign) "
                        f"VALUES ('{V6}', {gen_id}, {bid}, 'import_{country}', "
                        f" {p_nom_import:.1f}, false, 0, 1, {mc}, 'PQ', 1)"
                    ))
                    gen_id += 1
                    gen_count += 1

                # Export load
                p_set_export = max_export * frac
                if p_set_export > 0:
                    conn.execute(text(
                        f"INSERT INTO grid.egon_etrago_load "
                        f"(scn_name, load_id, bus, carrier, p_set, sign) "
                        f"VALUES ('{V6}', {load_id}, {bid}, 'export_{country}', "
                        f" {p_set_export:.1f}, -1)"
                    ))
                    load_id += 1
                    load_count += 1

            print(f"  {country}: {len(bus_capacity)} buses, "
                  f"import={max_import} MW, export={max_export} MW")

    print(f"  Total: {gen_count} generators, {load_count} loads")
    return gen_count, load_count


def generate_exchange_profiles(engine):
    """Generate 8,760-hour import/export profiles for all foreign generators and loads."""
    np.random.seed(42)
    hours = np.arange(HOURS)
    hour_of_day = hours % 24
    day_of_year = hours // 24

    # Synthetic German wind profile (higher in winter, variable)
    wind_seasonal = 0.35 + 0.15 * np.cos(2 * np.pi * (day_of_year - 15) / 365)
    wind_noise = np.random.normal(0, 0.12, HOURS)
    wind_profile = np.clip(wind_seasonal + wind_noise, 0.05, 0.95)

    with engine.begin() as conn:
        # Get all import generators
        gens = pd.read_sql(
            f"SELECT generator_id, bus, carrier, p_nom FROM grid.egon_etrago_generator "
            f"WHERE scn_name = '{V6}' AND carrier LIKE 'import_%%'",
            conn
        )

        # Get all export loads
        loads = pd.read_sql(
            f"SELECT load_id, bus, carrier, p_set FROM grid.egon_etrago_load "
            f"WHERE scn_name = '{V6}' AND carrier LIKE 'export_%%'",
            conn
        )

        print(f"  Generating profiles for {len(gens)} generators and {len(loads)} loads")

        # Generate per-generator p_max_pu timeseries
        for _, gen in gens.iterrows():
            country = gen['carrier'].replace('import_', '')
            params = COUNTRY_EXCHANGE.get(country, {})
            net_twh = params.get('net_twh', 0)

            # Seasonal: more import when net<0 (DE imports), peak in winter
            # More export when net>0, peak in summer
            if net_twh < 0:
                # Import country: higher in winter
                seasonal = 0.6 + 0.3 * np.cos(2 * np.pi * (day_of_year - 15) / 365)
            else:
                # Export-receiving country: higher in summer (DE solar surplus)
                seasonal = 0.4 + 0.2 * (-np.cos(2 * np.pi * (day_of_year - 15) / 365))

            # Daily pattern: peak during day
            daily = 1.0 + 0.15 * np.cos(2 * np.pi * (hour_of_day - 14) / 24)

            # Wind correlation for wind-heavy neighbors
            wind_factor = 1.0
            if country in ('DK', 'NL', 'PL'):
                wind_factor = 0.7 + 0.3 * wind_profile

            # Combine
            profile = seasonal * daily * wind_factor
            # Add noise
            noise = np.random.normal(0, 0.05, HOURS)
            profile = np.clip(profile + noise, 0.05, 1.0)

            # Store as p_max_pu array
            p_max_pu_list = [float(x) for x in profile]
            conn.execute(text(
                f"INSERT INTO grid.egon_etrago_generator_timeseries "
                f"(scn_name, generator_id, temp_id, p_max_pu) "
                f"VALUES (:scn, :gid, 1, :pmax)"
            ), {'scn': V6, 'gid': int(gen['generator_id']), 'pmax': p_max_pu_list})

        # Generate per-load p_set timeseries
        for _, load in loads.iterrows():
            country = load['carrier'].replace('export_', '')
            params = COUNTRY_EXCHANGE.get(country, {})
            net_twh = params.get('net_twh', 0)
            p_set_max = float(load['p_set'])

            if net_twh > 0:
                # Export country: higher export in summer
                seasonal = 0.5 + 0.3 * (-np.cos(2 * np.pi * (day_of_year - 15) / 365))
            else:
                # Import country: lower export in winter
                seasonal = 0.3 + 0.2 * (-np.cos(2 * np.pi * (day_of_year - 15) / 365))

            daily = 1.0 + 0.1 * np.cos(2 * np.pi * (hour_of_day - 14) / 24)

            wind_factor = 1.0
            if country in ('DK', 'NL', 'PL'):
                wind_factor = 0.8 + 0.2 * wind_profile

            profile = seasonal * daily * wind_factor
            noise = np.random.normal(0, 0.04, HOURS)
            profile = np.clip(profile + noise, 0.05, 1.0)

            # p_set timeseries = p_set_max * profile
            p_set_ts = [float(p_set_max * x) for x in profile]
            conn.execute(text(
                f"INSERT INTO grid.egon_etrago_load_timeseries "
                f"(scn_name, load_id, temp_id, p_set) "
                f"VALUES (:scn, :lid, 1, :pset)"
            ), {'scn': V6, 'lid': int(load['load_id']), 'pset': p_set_ts})

        print(f"  Written {len(gens)} generator timeseries + {len(loads)} load timeseries "
              f"({HOURS} hours each)")


def generate_offshore_wind_profiles(engine):
    """Generate 8,760-hour capacity factor profiles for offshore wind HVDC links."""
    np.random.seed(123)
    hours = np.arange(HOURS)
    day_of_year = hours // 24
    hour_of_day = hours % 24

    with engine.begin() as conn:
        # Add generators at offshore HVDC bus endpoints
        # Offshore buses are bus1 of links 4-13
        offshore_links = pd.read_sql(
            f"SELECT link_id, bus0, bus1, p_nom FROM grid.egon_etrago_link "
            f"WHERE scn_name = '{V6}' AND link_id >= 4 AND link_id <= 13",
            conn
        )

        # Check existing max generator_id
        max_gen = conn.execute(text(
            f"SELECT COALESCE(MAX(generator_id), 0) FROM grid.egon_etrago_generator "
            f"WHERE scn_name = '{V6}'"
        )).scalar()
        gen_id = int(max_gen) + 1

        for _, link in offshore_links.iterrows():
            offshore_bus = int(link['bus1'])
            p_nom = float(link['p_nom'])
            link_id_val = int(link['link_id'])

            # North Sea wind: higher in winter (~50% CF), lower in summer (~30%)
            seasonal_cf = 0.40 + 0.10 * np.cos(2 * np.pi * (day_of_year - 15) / 365)

            # Some diurnal variation (slight)
            diurnal = 1.0 + 0.03 * np.cos(2 * np.pi * (hour_of_day - 3) / 24)

            # Wind variability (autocorrelated)
            noise = np.zeros(HOURS)
            noise[0] = np.random.normal(0, 0.1)
            for h in range(1, HOURS):
                noise[h] = 0.85 * noise[h-1] + np.random.normal(0, 0.08)

            cf = seasonal_cf * diurnal + noise
            cf = np.clip(cf, 0.0, 0.95)

            # Add generator at offshore bus
            conn.execute(text(
                f"INSERT INTO grid.egon_etrago_generator "
                f"(scn_name, generator_id, bus, carrier, p_nom, "
                f" p_nom_extendable, p_min_pu, p_max_pu, "
                f" marginal_cost, control, sign) "
                f"VALUES ('{V6}', {gen_id}, {offshore_bus}, 'offwind', "
                f" {p_nom:.1f}, false, 0, 1, 0, 'PQ', 1)"
            ))

            # Timeseries
            p_max_pu_list = [float(x) for x in cf]
            conn.execute(text(
                f"INSERT INTO grid.egon_etrago_generator_timeseries "
                f"(scn_name, generator_id, temp_id, p_max_pu) "
                f"VALUES (:scn, :gid, 1, :pmax)"
            ), {'scn': V6, 'gid': gen_id, 'pmax': p_max_pu_list})

            print(f"    Offshore gen {gen_id} at bus {offshore_bus} "
                  f"(link {link_id_val}): {p_nom:.0f} MW, "
                  f"mean CF={float(cf.mean()):.2f}")
            gen_id += 1

    print(f"  Added {len(offshore_links)} offshore wind generators with profiles")


def print_summary(engine):
    """Print v6 summary statistics."""
    with engine.connect() as conn:
        buses = conn.execute(text(
            f"SELECT COUNT(*) FROM grid.egon_etrago_bus WHERE scn_name = '{V6}'"
        )).scalar()
        bus_v = conn.execute(text(
            f"SELECT v_nom, COUNT(*) FROM grid.egon_etrago_bus "
            f"WHERE scn_name = '{V6}' GROUP BY v_nom ORDER BY v_nom"
        )).fetchall()
        lines = conn.execute(text(
            f"SELECT COUNT(*) FROM grid.egon_etrago_line WHERE scn_name = '{V6}'"
        )).scalar()
        trafos = conn.execute(text(
            f"SELECT COUNT(*) FROM grid.egon_etrago_transformer WHERE scn_name = '{V6}'"
        )).scalar()
        pst_count = conn.execute(text(
            f"SELECT COUNT(*) FROM grid.egon_etrago_transformer "
            f"WHERE scn_name = '{V6}' AND type = 'PST'"
        )).scalar()
        links = conn.execute(text(
            f"SELECT COUNT(*) FROM grid.egon_etrago_link WHERE scn_name = '{V6}'"
        )).scalar()
        gens = conn.execute(text(
            f"SELECT COUNT(*) FROM grid.egon_etrago_generator WHERE scn_name = '{V6}'"
        )).scalar()
        gen_carriers = conn.execute(text(
            f"SELECT carrier, COUNT(*), SUM(p_nom) FROM grid.egon_etrago_generator "
            f"WHERE scn_name = '{V6}' GROUP BY carrier ORDER BY carrier"
        )).fetchall()
        loads = conn.execute(text(
            f"SELECT COUNT(*) FROM grid.egon_etrago_load WHERE scn_name = '{V6}'"
        )).scalar()
        gen_ts = conn.execute(text(
            f"SELECT COUNT(*) FROM grid.egon_etrago_generator_timeseries "
            f"WHERE scn_name = '{V6}'"
        )).scalar()
        load_ts = conn.execute(text(
            f"SELECT COUNT(*) FROM grid.egon_etrago_load_timeseries "
            f"WHERE scn_name = '{V6}'"
        )).scalar()

    print(f"\n{'='*55}")
    print(f"eGon2025v6 Summary")
    print(f"{'='*55}")
    print(f"Buses:          {buses:,}")
    for v, n in bus_v:
        print(f"  {int(v):>3} kV:      {n:,}")
    print(f"Lines:          {lines:,}")
    print(f"Transformers:   {trafos:,} (incl. {pst_count} PSTs)")
    print(f"HVDC links:     {links:,}")
    print(f"Generators:     {gens:,}")
    for carrier, cnt, p_total in gen_carriers:
        print(f"  {carrier:20s}: {cnt:>3} ({p_total:,.0f} MW)")
    print(f"Loads:          {loads:,}")
    print(f"Gen timeseries: {gen_ts:,}")
    print(f"Load timeseries:{load_ts:,}")


def print_dry_run():
    """Print planned changes without applying."""
    print("DRY RUN — pass --apply to write to database\n")
    print("Changes to apply:")
    print("  1. Copy v5 → v6 (all 8 component tables)")
    print()
    print("  2. Add 19 phase-shifting transformers (PSTs):")
    from collections import Counter
    sub_counts = Counter()
    for pst in PSTS:
        sub = pst[0].split(' ')[1]
        sub_counts[sub] += 1
    for sub, cnt in sorted(sub_counts.items()):
        print(f"     - {sub}: {cnt} PST(s)")
    print("     Each PST inserted in series on a 380kV line")
    print("     Creates intermediate bus + reconnects line")
    print()
    print("  3. Add import/export at foreign buses:")
    for country, params in sorted(COUNTRY_EXCHANGE.items()):
        net = params['net_twh']
        direction = "DE imports" if net < 0 else "DE exports"
        print(f"     - {country}: net {abs(net):.1f} TWh ({direction}), "
              f"import≤{params['max_import']} MW, export≤{params['max_export']} MW")
    print()
    print("  4. Generate 8,760-hour profiles:")
    print("     - Cross-border exchange (seasonal + daily + wind correlation)")
    print("     - Offshore wind (North Sea wind patterns)")
    print()
    print("  5. Summary and validation")


def main():
    parser = argparse.ArgumentParser(description='Build eGon2025v6 from v5')
    parser.add_argument('--apply', action='store_true', help='Apply changes to database')
    args = parser.parse_args()

    if not args.apply:
        print_dry_run()
        return

    engine = create_engine(DB_URI)

    print("Step 1: Copy v5 → v6")
    copy_v5_to_v6(engine)

    print("\nStep 2: Add phase-shifting transformers")
    add_psts(engine)

    print("\nStep 3: Add import/export generators and loads")
    add_import_export(engine)

    print("\nStep 4: Generate exchange profiles (8,760 hours)")
    generate_exchange_profiles(engine)

    print("\nStep 5: Generate offshore wind profiles")
    generate_offshore_wind_profiles(engine)

    print_summary(engine)


if __name__ == '__main__':
    main()
