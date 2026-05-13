#!/usr/bin/env python3
"""
build_merit_order.py - Comprehensive merit order for grid_beta.

1. Sub-classifies gas generators into gas_ccgt and gas_chp (from MaStR Technologie)
2. Assigns SRMC marginal costs based on 2025 fuel/CO2 prices
3. Creates seasonal CHP must-run profiles (8760h p_min_pu timeseries)
4. Creates regional renewable capacity factor profiles (8760h p_max_pu per bus)
5. Updates storage parameters

SRMC formula:
    SRMC (€/MWh_el) = fuel_price / η + CO₂_factor × CO₂_price / η + var_O&M

References:
    UBA 2022 (CO₂ factors), DIW Berlin DD68 (efficiencies),
    Agora 2017 (flexibility), Fraunhofer ISE 2024 (fleet stats)

Usage:
    python scripts/build_merit_order.py              # Dry run
    python scripts/build_merit_order.py --apply       # Apply all changes
    python scripts/build_merit_order.py --co2=80      # Override CO₂ price
    python scripts/build_merit_order.py --gas=45      # Override gas price
"""

import argparse
import os
import sys
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

SCENARIO = 'grid_beta'
DB_URL = 'postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data'
YEAR = 2025
N_HOURS = 8760
PROFILES_DIR = 'data/profiles'

# ═══════════════════════════════════════════════════════════════════════════════
# FUEL & CO₂ PRICES — 2025 estimates
# ═══════════════════════════════════════════════════════════════════════════════

FUEL_PRICES = {
    'gas':        40.0,    # TTF natural gas (€/MWh_th)
    'gas_ccgt':   40.0,    # Same fuel, different efficiency
    'gas_chp':    40.0,
    'coal':       13.6,    # API2 hard coal ($100/t ÷ 6.98 MWh/t ÷ 1.05 EUR/USD)
    'lignite':     5.0,    # Mine-mouth (Rheinisches/Lausitzer Revier)
    'oil':        35.0,    # Heavy fuel oil (~Brent $65/bbl)
    'biomass':     7.0,    # Wood chips / forestry residues
    'biogas':      8.0,    # Substrate cost (EEG-subsidized)
    'waste':      -5.0,    # Gate fee (paid to receive waste)
    'other':      30.0,    # Industrial CHP / steam (gas-derived)
    'hydrogen':  100.0,    # Green H₂ (~€5/kg)
}

CO2_PRICE = 75.0  # €/tCO₂ (EU ETS)

# CO₂ emission factors (tCO₂/MWh_th) — UBA 2022
CO2_FACTORS = {
    'lignite': 0.399, 'coal': 0.338, 'gas': 0.201, 'gas_ccgt': 0.201,
    'gas_chp': 0.201, 'oil': 0.267, 'other': 0.201, 'hydrogen': 0.0,
}

# ═══════════════════════════════════════════════════════════════════════════════
# PLANT PARAMETERS
# ═══════════════════════════════════════════════════════════════════════════════

EFFICIENCIES = {
    'lignite':      0.36,   # Fleet avg (33-48%)
    'coal':         0.40,   # Fleet avg (36-46%)
    'gas_ccgt':     0.58,   # Modern CCGT
    'gas_chp':      0.42,   # CHP electrical efficiency (lower, but has heat credit)
    'gas':          0.55,   # Fallback if not sub-classified
    'oil':          0.37,
    'biomass':      0.30,   # Small wood-fired CHP
    'biogas':       0.35,   # Biogas engine CHP
    'waste':        0.25,   # Waste incineration
    'other':        0.40,   # Industrial CHP / steam
    'hydrogen':     0.55,
    'solar': 1.0, 'onwind': 1.0, 'offwind': 1.0,
    'run_of_river': 1.0, 'reservoir': 1.0,
}

VAR_OM = {  # Variable O&M (€/MWh_el)
    'lignite': 4.0, 'coal': 3.0, 'gas_ccgt': 2.0, 'gas_chp': 3.0, 'gas': 2.0,
    'oil': 3.0, 'biomass': 5.0, 'biogas': 5.0, 'waste': 5.0, 'other': 3.0,
    'hydrogen': 2.0, 'solar': 0.0, 'onwind': 0.0, 'offwind': 0.0,
    'run_of_river': 0.0, 'reservoir': 0.0,
}

# CHP heat credit reduces effective SRMC (€/MWh_el)
CHP_HEAT_CREDIT = 20.0

# Must-run (static, for non-CHP carriers)
P_MIN_PU = {
    'lignite': 0.45, 'coal': 0.35,
    'biomass': 0.40, 'biogas': 0.40, 'waste': 0.50,
}

RAMP_LIMITS = {'lignite': 0.6, 'coal': 0.8}

# CHP seasonal must-run profile (month → p_min_pu)
CHP_MONTHLY_PMIN = {
    1: 0.60, 2: 0.58, 3: 0.50,       # Winter
    4: 0.35, 5: 0.25,                 # Spring transition
    6: 0.15, 7: 0.12, 8: 0.15,       # Summer (hot water only)
    9: 0.25, 10: 0.38,                # Autumn transition
    11: 0.52, 12: 0.60,               # Winter
}

# Storage
STORAGE_PARAMS = {
    'pumped_hydro': {'efficiency_store': 0.87, 'efficiency_dispatch': 0.87,
                     'marginal_cost': 0.5},
    'battery':      {'efficiency_store': 0.93, 'efficiency_dispatch': 0.93,
                     'marginal_cost': 2.0},
}


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def smooth(x, window):
    """Moving average smoothing using numpy convolution."""
    kernel = np.ones(window) / window
    return np.convolve(x, kernel, mode='same')


def compute_srmc(carrier, co2_price=75.0, heat_credit=0.0):
    """Short-run marginal cost: fuel/η + CO₂×CO₂_price/η + VOM - heat_credit."""
    eta = EFFICIENCIES.get(carrier, 1.0)
    fuel = FUEL_PRICES.get(carrier, 0.0)
    co2f = CO2_FACTORS.get(carrier, 0.0)
    vom = VAR_OM.get(carrier, 0.0)
    return round(fuel / eta + co2f * co2_price / eta + vom - heat_credit, 2)


# ═══════════════════════════════════════════════════════════════════════════════
# GAS SUB-CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

def classify_gas_generators(engine, scenario):
    """Sub-classify 'gas' generators into 'gas_ccgt' and 'gas_chp'.

    Uses voltage level + capacity heuristic based on MaStR statistics:
    - MaStR ≥1 MW gas: CCGT=11 GW (34%), CHP=19 GW (59%), OCGT=2 GW (6%)
    - 380 kV gas → always CCGT (large combined-cycle plants)
    - 220 kV gas ≥ 50 MW → CCGT
    - Everything else → CHP (engines, extraction-condensing, backpressure)

    Returns DataFrame of (generator_id, new_carrier).
    """
    gas = pd.read_sql(f"""
        SELECT g.generator_id, g.p_nom, b.v_nom
        FROM grid.egon_etrago_generator g
        JOIN grid.egon_etrago_bus b
          ON g.bus = b.bus_id AND b.scn_name = '{scenario}'
        WHERE g.scn_name = '{scenario}' AND g.carrier = 'gas'
    """, engine)

    if len(gas) == 0:
        return pd.DataFrame(columns=['generator_id', 'new_carrier'])

    # Classification rules
    is_ccgt = (
        (gas['v_nom'] == 380) |                          # All 380 kV gas
        ((gas['v_nom'] == 220) & (gas['p_nom'] >= 50))   # Large 220 kV gas
    )
    gas['new_carrier'] = np.where(is_ccgt, 'gas_ccgt', 'gas_chp')

    ccgt = gas[gas['new_carrier'] == 'gas_ccgt']
    chp = gas[gas['new_carrier'] == 'gas_chp']
    print(f"\n── Gas Sub-classification {'─' * 54}")
    print(f"  gas_ccgt: {len(ccgt):>4} generators, {ccgt['p_nom'].sum()/1e3:>6.1f} GW "
          f"(380 kV + 220 kV ≥50 MW)")
    print(f"  gas_chp:  {len(chp):>4} generators, {chp['p_nom'].sum()/1e3:>6.1f} GW "
          f"(110 kV + small 220 kV)")
    print(f"  MaStR reference: CCGT=11.0 GW (34%), CHP+OCGT=21.0 GW (66%)")

    return gas[['generator_id', 'new_carrier']]


# ═══════════════════════════════════════════════════════════════════════════════
# PROFILE GENERATORS
# ═══════════════════════════════════════════════════════════════════════════════

def create_weather_state(n_hours=N_HOURS, seed=2025):
    """Correlated 'weather state' for anti-correlated wind/solar.

    Positive = high pressure (clear sky, low wind)
    Negative = low pressure (cloudy, windy)
    Multi-timescale noise captures synoptic (3-5 day) and blocking (10-14 day).
    """
    rng = np.random.RandomState(seed)
    white = rng.normal(0, 1, n_hours + 500)  # pad for convolution
    synoptic = smooth(white, 96)[250:250 + n_hours]   # 4-day patterns
    blocking = smooth(white, 288)[250:250 + n_hours]   # 12-day patterns
    state = 0.6 * synoptic + 0.4 * blocking
    return (state - state.mean()) / (state.std() + 1e-10)


def create_solar_profile(lat, lon, weather):
    """8760h solar capacity factor profile using clear-sky model + clouds.

    Lat/lon determine solar geometry. Weather state modulates cloudiness.
    Target CF: ~0.12 (south, lat≈48) to ~0.09 (north, lat≈55).
    """
    hours = np.arange(N_HOURS)
    doy = hours / 24.0 + 1       # day of year
    hod = hours % 24              # hour of day (UTC)

    # Solar declination (degrees)
    decl = 23.45 * np.sin(np.radians(360.0 * (284 + doy) / 365.25))

    # Solar hour angle — solar noon at UTC + lon/15
    solar_noon_utc = 12.0 - lon / 15.0
    ha = 15.0 * (hod - solar_noon_utc)

    # Solar altitude angle
    lat_r = np.radians(lat)
    decl_r = np.radians(decl)
    ha_r = np.radians(ha)
    sin_alt = (np.sin(lat_r) * np.sin(decl_r) +
               np.cos(lat_r) * np.cos(decl_r) * np.cos(ha_r))

    # Clear-sky GHI (power-law for air mass effect)
    ghi = np.maximum(0, sin_alt) ** 1.2

    # Cloud cover: seasonal (clearer in summer) + weather modulation
    seasonal_clear = 0.60 + 0.28 * np.cos(2 * np.pi * (doy - 172) / 365.25)
    cloud = seasonal_clear + 0.12 * weather
    cloud = np.clip(cloud, 0.08, 0.95)

    cf = ghi * cloud

    # Normalize to target annual CF based on latitude
    target_cf = np.clip(0.115 - 0.004 * (lat - 51.0), 0.07, 0.14)
    if cf.mean() > 0:
        cf *= target_cf / cf.mean()

    return np.clip(cf, 0, 1.0)


def create_wind_onshore_profile(lat, lon, weather, seed_offset=0):
    """8760h wind onshore CF profile with regional variation.

    Northern/coastal Germany: higher CF (~0.25-0.28)
    Southern Germany: lower CF (~0.15-0.18)
    Anti-correlated with solar via weather state.
    """
    hours = np.arange(N_HOURS)
    doy = hours / 24.0 + 1
    hod = hours % 24

    # Seasonal wind: stronger in winter (peak Jan), weaker in summer
    seasonal = 1.0 + 0.30 * np.cos(2 * np.pi * (doy - 15) / 365.25)

    # Diurnal: slightly stronger during afternoon (thermal convection)
    diurnal = 1.0 + 0.06 * np.cos(2 * np.pi * (hod - 14) / 24)

    # Weather: low pressure → stronger wind (anti-correlated)
    wind_speed = seasonal * diurnal * (1.0 - 0.35 * weather)

    # Local variation (each location gets slightly different noise)
    rng = np.random.RandomState(int(abs(lat * 1000 + lon * 100)) % (2**31) + seed_offset)
    local = smooth(rng.normal(0, 0.12, N_HOURS + 200), 48)[100:100 + N_HOURS]
    wind_speed += local
    wind_speed = np.maximum(wind_speed, 0)

    # Power curve: cubic with cut-in (0.3) and rated (1.0) thresholds
    # Normalize wind_speed so median ≈ 0.7
    ws_norm = wind_speed / (np.median(wind_speed) + 1e-10) * 0.7
    cf = np.clip(ws_norm ** 3, 0, 1)

    # Regional target CF
    target_cf = 0.21 + 0.007 * (lat - 51.0)  # north-south gradient
    if lon < 10.0:        # Coastal/western bonus
        target_cf += 0.025
    target_cf = np.clip(target_cf, 0.13, 0.30)

    if cf.mean() > 0:
        cf *= target_cf / cf.mean()

    return np.clip(cf, 0, 1.0)


def create_wind_offshore_profile(weather, seed_offset=0):
    """8760h wind offshore CF profile. Higher and more stable than onshore."""
    hours = np.arange(N_HOURS)
    doy = hours / 24.0 + 1

    # Less seasonal variation than onshore
    seasonal = 1.0 + 0.18 * np.cos(2 * np.pi * (doy - 15) / 365.25)

    # Strong anti-correlation with weather state
    wind_speed = seasonal * (1.0 - 0.30 * weather)

    rng = np.random.RandomState(7777 + seed_offset)
    local = smooth(rng.normal(0, 0.08, N_HOURS + 200), 72)[100:100 + N_HOURS]
    wind_speed += local
    wind_speed = np.maximum(wind_speed, 0)

    ws_norm = wind_speed / (np.median(wind_speed) + 1e-10) * 0.75
    cf = np.clip(ws_norm ** 3, 0, 1)

    target_cf = 0.37
    if cf.mean() > 0:
        cf *= target_cf / cf.mean()

    return np.clip(cf, 0, 1.0)


def create_ror_profile(lat, weather):
    """8760h run-of-river CF profile. Spring snowmelt peak, summer low."""
    hours = np.arange(N_HOURS)
    doy = hours / 24.0 + 1

    # Seasonal: peak April (snowmelt), low August-September
    seasonal = 0.50 + 0.20 * np.cos(2 * np.pi * (doy - 105) / 365.25)

    # Rain events slightly correlated with low pressure
    rain = -0.06 * weather  # more rain in low pressure
    cf = seasonal + rain

    # Alpine rivers (south) have stronger snowmelt peak
    if lat < 49.0:
        spring_boost = 0.15 * np.exp(-0.5 * ((doy - 120) / 30) ** 2)
        cf += spring_boost

    return np.clip(cf, 0.08, 0.95)


def create_chp_seasonal_profile():
    """8760h CHP must-run p_min_pu profile based on heating degree days."""
    hours = np.arange(N_HOURS)
    profile = np.zeros(N_HOURS)

    # Assign monthly values with smooth daily transitions
    for h in range(N_HOURS):
        doy = h // 24 + 1
        # Map day-of-year to month (approximate)
        month = min(12, max(1, int((doy - 1) / 30.44) + 1))
        profile[h] = CHP_MONTHLY_PMIN[month]

    # Smooth transitions between months (7-day smoothing)
    profile = smooth(profile, 168)

    # Add daily modulation: higher at night (heating), lower midday
    hod = hours % 24
    daily_mod = 1.0 + 0.08 * np.cos(2 * np.pi * (hod - 3) / 24)  # peak at 3am
    profile *= daily_mod

    return np.clip(profile, 0.05, 0.75)


def try_load_smard_profiles():
    """Try to load real SMARD profiles from data/profiles/ if available.

    Handles both 'cf' and 'capacity_factor' column names.
    """
    profiles = {}
    for carrier, filename in [
        ('solar', 'solar_cf_2025.csv'),
        ('onwind', 'wind_onshore_cf_2025.csv'),
        ('offwind', 'wind_offshore_cf_2025.csv'),
    ]:
        path = os.path.join(PROFILES_DIR, filename)
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path)
        # Find the CF column
        cf_col = None
        for col in ['cf', 'capacity_factor']:
            if col in df.columns:
                cf_col = col
                break
        if cf_col is None or len(df) < N_HOURS:
            continue

        cf = df[cf_col].values[:N_HOURS]
        profiles[carrier] = cf
        print(f"  Loaded SMARD 2025: {filename} (avg CF={cf.mean():.3f})")

    return profiles if profiles else None


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='Comprehensive merit order for grid_beta')
    parser.add_argument('--apply', action='store_true',
                        help='Apply all changes to database')
    parser.add_argument('--scenario', default=SCENARIO)
    parser.add_argument('--co2', type=float, default=CO2_PRICE,
                        help=f'CO₂ price €/tCO₂ (default: {CO2_PRICE})')
    parser.add_argument('--gas', type=float, default=FUEL_PRICES['gas'],
                        help=f'Gas price €/MWh_th (default: {FUEL_PRICES["gas"]})')
    parser.add_argument('--coal', type=float, default=FUEL_PRICES['coal'],
                        help=f'Coal price €/MWh_th (default: {FUEL_PRICES["coal"]})')
    parser.add_argument('--no-profiles', action='store_true',
                        help='Skip creating hourly profiles')
    parser.add_argument('--no-storage', action='store_true',
                        help='Skip updating storage parameters')
    args = parser.parse_args()

    co2_price = args.co2
    FUEL_PRICES['gas'] = args.gas
    FUEL_PRICES['gas_ccgt'] = args.gas
    FUEL_PRICES['gas_chp'] = args.gas
    FUEL_PRICES['coal'] = args.coal

    engine = create_engine(DB_URL)
    scn = args.scenario

    # ── Read generators + bus coordinates ─────────────────────────────────────
    gens = pd.read_sql(f"""
        SELECT g.generator_id, g.carrier, g.p_nom, g.marginal_cost,
               g.efficiency, g.p_min_pu, g.p_max_pu, g.bus,
               b.v_nom, b.x AS lon, b.y AS lat
        FROM grid.egon_etrago_generator g
        JOIN grid.egon_etrago_bus b
          ON g.bus = b.bus_id AND b.scn_name = '{scn}'
        WHERE g.scn_name = '{scn}'
    """, engine)

    domestic = gens[~gens['carrier'].str.startswith('import_')].copy()
    imports = gens[gens['carrier'].str.startswith('import_')]

    print(f"\n{'═' * 80}")
    print(f"  MERIT ORDER — scenario: {scn}")
    print(f"{'═' * 80}")
    print(f"\n  Fuel prices:  Gas={FUEL_PRICES['gas']:.0f}  Coal={FUEL_PRICES['coal']:.1f}"
          f"  Lignite={FUEL_PRICES['lignite']:.0f}  CO₂={co2_price:.0f} €/tCO₂")
    print(f"  Generators:   {len(domestic):,} domestic ({domestic['p_nom'].sum()/1e3:.1f} GW)"
          f" + {len(imports):,} import ({imports['p_nom'].sum()/1e3:.1f} GW)")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 1: GAS SUB-CLASSIFICATION
    # ══════════════════════════════════════════════════════════════════════════

    has_gas = (domestic['carrier'] == 'gas').any()
    gas_classified = has_gas  # track if we need to classify

    if has_gas:
        gas_class = classify_gas_generators(engine, scn)
        # Update domestic DataFrame in memory
        id_to_carrier = dict(zip(gas_class['generator_id'],
                                 gas_class['new_carrier']))
        domestic['carrier'] = domestic.apply(
            lambda r: id_to_carrier.get(r['generator_id'], r['carrier']),
            axis=1)
    else:
        gas_class = pd.DataFrame()
        # Check if already classified
        if (domestic['carrier'] == 'gas_ccgt').any():
            print(f"\n  Gas already sub-classified (gas_ccgt + gas_chp found)")
            gas_classified = False

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 2: COMPUTE MERIT ORDER
    # ══════════════════════════════════════════════════════════════════════════

    carriers = sorted(domestic['carrier'].unique())
    rows = []
    for c in carriers:
        mask = domestic['carrier'] == c
        hc = CHP_HEAT_CREDIT if c == 'gas_chp' else 0
        rows.append({
            'carrier': c,
            'srmc': compute_srmc(c, co2_price=co2_price, heat_credit=hc),
            'srmc_raw': compute_srmc(c, co2_price=co2_price, heat_credit=0),
            'efficiency': EFFICIENCIES.get(c, 1.0),
            'p_min_pu': P_MIN_PU.get(c, 0.0),
            'capacity_gw': domestic.loc[mask, 'p_nom'].sum() / 1e3,
            'count': int(mask.sum()),
        })

    merit_df = pd.DataFrame(rows).sort_values('srmc').reset_index(drop=True)

    # ── Print merit order ─────────────────────────────────────────────────────
    print(f"\n── Merit Order {'─' * 64}")
    print(f"  {'Carrier':<16} {'SRMC':>8} {'Fuel/η':>7} {'CO₂/η':>7} {'VOM':>5}"
          f" {'η':>5} {'Pmin':>5} {'Cap(GW)':>8} {'Count':>6}")
    print(f"  {'─' * 72}")
    for _, row in merit_df.iterrows():
        c = row['carrier']
        eta = EFFICIENCIES.get(c, 1.0)
        fuel_c = FUEL_PRICES.get(c, 0.0) / eta if eta > 0 else 0
        co2_c = CO2_FACTORS.get(c, 0.0) * co2_price / eta if eta > 0 else 0
        vom = VAR_OM.get(c, 0.0)
        hc_note = f" -{CHP_HEAT_CREDIT:.0f}hc" if c == 'gas_chp' else ""
        print(f"  {c:<16} {row['srmc']:>7.1f}{hc_note:>4} {fuel_c:>7.1f} {co2_c:>7.1f}"
              f" {vom:>5.1f} {eta:>5.2f} {row['p_min_pu']:>5.2f}"
              f" {row['capacity_gw']:>8.1f} {row['count']:>6}")

    # ── Capacity summary ──────────────────────────────────────────────────────
    ren_carriers = ['solar', 'onwind', 'offwind', 'run_of_river', 'reservoir']
    bio_carriers = ['biomass', 'biogas', 'waste']
    thermal_carriers = [c for c in carriers
                        if c not in ren_carriers + bio_carriers]

    ren_cap = merit_df[merit_df['carrier'].isin(ren_carriers)]['capacity_gw'].sum()
    bio_cap = merit_df[merit_df['carrier'].isin(bio_carriers)]['capacity_gw'].sum()
    therm_cap = merit_df[merit_df['carrier'].isin(thermal_carriers)]['capacity_gw'].sum()
    imp_cap = imports['p_nom'].sum() / 1e3

    # Approximate average renewable output
    ren_avg = sum(
        merit_df.loc[merit_df['carrier'] == c, 'capacity_gw'].sum() * cf
        for c, cf in [('solar', 0.11), ('onwind', 0.21), ('offwind', 0.37),
                       ('run_of_river', 0.50), ('reservoir', 0.50)]
    )
    bio_avg = bio_cap * 0.42  # ~42% average CF for biomass/biogas/waste

    print(f"\n── System Capacity Summary {'─' * 53}")
    print(f"  Renewables:   {ren_cap:>6.1f} GW installed → ~{ren_avg:.1f} GW average output")
    print(f"  Bio/waste:    {bio_cap:>6.1f} GW installed → ~{bio_avg:.1f} GW average (must-run)")
    print(f"  Thermal:      {therm_cap:>6.1f} GW installed (gas_ccgt + gas_chp + coal + lignite + oil + other)")
    print(f"  Imports:      {imp_cap:>6.1f} GW NTC capacity")
    print(f"  Storage:      ~12.5 GW (pumped hydro + battery)")
    print(f"  ────────────────────────")
    print(f"  Total supply: {ren_cap + bio_cap + therm_cap + imp_cap:.0f} GW installed"
          f" / ~{ren_avg + bio_avg + therm_cap:.0f} GW available at peak")
    print(f"  Peak load:    76.2 GW  |  Avg load: ~51 GW")

    # ── Dispatchable stack ────────────────────────────────────────────────────
    disp = merit_df.copy()
    # Add expected average output for renewables
    avg_cf_map = {'solar': 0.11, 'onwind': 0.21, 'offwind': 0.37,
                  'run_of_river': 0.50, 'reservoir': 0.50}
    disp['avg_gw'] = disp.apply(
        lambda r: r['capacity_gw'] * avg_cf_map.get(r['carrier'], 1.0),
        axis=1)
    disp['cum_gw'] = disp['avg_gw'].cumsum()

    print(f"\n── Full Dispatch Stack (by average expected output) {'─' * 28}")
    print(f"  {'Carrier':<16} {'SRMC':>7} {'Inst(GW)':>9} {'AvgOut(GW)':>11} {'Cumul(GW)':>10}")
    print(f"  {'─' * 57}")
    for _, row in disp.iterrows():
        print(f"  {row['carrier']:<16} {row['srmc']:>7.1f} {row['capacity_gw']:>9.1f}"
              f" {row['avg_gw']:>11.1f} {row['cum_gw']:>10.1f}")

    # ── Imports & storage ─────────────────────────────────────────────────────
    if len(imports) > 0:
        print(f"\n── Cross-border Imports (unchanged) {'─' * 44}")
        print(f"  {'Carrier':<16} {'MC':>8} {'Cap(GW)':>8} {'Count':>6}")
        print(f"  {'─' * 42}")
        for c in sorted(imports['carrier'].unique()):
            m = imports['carrier'] == c
            print(f"  {c:<16} {imports.loc[m, 'marginal_cost'].mean():>8.1f}"
                  f" {imports.loc[m, 'p_nom'].sum()/1e3:>8.1f}"
                  f" {int(m.sum()):>6}")

    storage = pd.read_sql(f"""
        SELECT carrier, COUNT(*) as cnt, SUM(p_nom) as total_mw,
               AVG(max_hours) as max_hours,
               AVG(efficiency_store) as eta_s, AVG(efficiency_dispatch) as eta_d,
               AVG(marginal_cost) as mc
        FROM grid.egon_etrago_storage WHERE scn_name = '{scn}'
        GROUP BY carrier ORDER BY SUM(p_nom) DESC
    """, engine)

    if len(storage) > 0:
        print(f"\n── Storage {'─' * 68}")
        print(f"  {'Carrier':<16} {'Cap(GW)':>8} {'Hours':>6}"
              f" {'η_s':>6} {'η_d':>6} {'RT%':>6} {'MC':>6} {'Count':>6}")
        print(f"  {'─' * 62}")
        for _, row in storage.iterrows():
            sp = STORAGE_PARAMS.get(row['carrier'], {})
            es = sp.get('efficiency_store', row['eta_s'])
            ed = sp.get('efficiency_dispatch', row['eta_d'])
            mc = sp.get('marginal_cost', row['mc'])
            change = "" if abs(es - row['eta_s']) < 0.001 else \
                f"  (was {row['eta_s']:.2f}×{row['eta_d']:.2f})"
            print(f"  {row['carrier']:<16} {row['total_mw']/1e3:>8.1f}"
                  f" {row['max_hours']:>6.0f} {es:>6.2f} {ed:>6.2f}"
                  f" {es*ed*100:>5.0f}% {mc:>6.1f} {int(row['cnt']):>6}{change}")

    # ══════════════════════════════════════════════════════════════════════════
    # PROFILES PREVIEW
    # ══════════════════════════════════════════════════════════════════════════

    if not args.no_profiles:
        print(f"\n── Hourly Profiles to Create {'─' * 51}")

        # CHP seasonal
        chp_profile = create_chp_seasonal_profile()
        chp_count = int((domestic['carrier'] == 'gas_chp').sum())
        print(f"  CHP seasonal p_min_pu:  {chp_count:,} generators × 8760h"
              f"  (winter={chp_profile[:744].mean():.2f},"
              f" summer={chp_profile[4344:6552].mean():.2f})")

        # Renewable profiles
        smard = try_load_smard_profiles()
        weather = create_weather_state()

        for carrier, label in [('solar', 'Solar'), ('onwind', 'Wind onshore'),
                                ('offwind', 'Wind offshore'),
                                ('run_of_river', 'Run-of-river')]:
            mask = domestic['carrier'] == carrier
            n_gen = int(mask.sum())
            if n_gen == 0:
                continue

            lats = domestic.loc[mask, 'lat'].values
            avg_lat = np.mean(lats) if len(lats) > 0 else 51.0
            avg_lon = np.mean(domestic.loc[mask, 'lon'].values) if n_gen > 0 else 10.0

            # Sample profile for display
            if carrier == 'solar':
                sample = create_solar_profile(avg_lat, avg_lon, weather)
            elif carrier == 'onwind':
                sample = create_wind_onshore_profile(avg_lat, avg_lon, weather)
            elif carrier == 'offwind':
                sample = create_wind_offshore_profile(weather)
            else:
                sample = create_ror_profile(avg_lat, weather)

            src = "SMARD" if smard and carrier in smard else "synthetic"
            print(f"  {label:<18} p_max_pu: {n_gen:>5,} gens × 8760h"
                  f"  avg CF={sample.mean():.3f}  [{src}]"
                  f"  (lat range: {lats.min():.1f}°–{lats.max():.1f}°)")

    # ══════════════════════════════════════════════════════════════════════════
    # DRY RUN EXIT
    # ══════════════════════════════════════════════════════════════════════════

    if not args.apply:
        print(f"\n{'═' * 80}")
        print(f"  DRY RUN — use --apply to write changes to database")
        print(f"{'═' * 80}\n")
        return

    # ══════════════════════════════════════════════════════════════════════════
    # APPLY TO DATABASE
    # ══════════════════════════════════════════════════════════════════════════

    print(f"\n{'═' * 80}")
    print(f"  APPLYING to {scn}...")
    print(f"{'═' * 80}")

    with engine.begin() as conn:
        # ── Step 1: Gas reclassification ──────────────────────────────────────
        if has_gas and len(gas_class) > 0:
            print(f"\n  [1/5] Gas sub-classification...")
            for new_carrier in ['gas_ccgt', 'gas_chp']:
                ids = gas_class[gas_class['new_carrier'] == new_carrier]['generator_id'].tolist()
                if ids:
                    # Update in batches
                    for i in range(0, len(ids), 500):
                        batch = ids[i:i+500]
                        conn.execute(text("""
                            UPDATE grid.egon_etrago_generator
                            SET carrier = :carrier
                            WHERE scn_name = :scn
                              AND generator_id = ANY(:ids)
                        """), {'carrier': new_carrier, 'scn': scn,
                               'ids': batch})
                    print(f"    {new_carrier}: {len(ids)} generators updated")
        else:
            print(f"\n  [1/5] Gas classification: skipped (already done or no gas)")

        # ── Step 2: Merit order parameters ────────────────────────────────────
        print(f"\n  [2/5] Setting merit order parameters...")
        for _, row in merit_df.iterrows():
            c = row['carrier']
            srmc = float(row['srmc'])
            eta = float(row['efficiency'])
            pmin = float(row['p_min_pu'])
            ramp = float(RAMP_LIMITS.get(c, 0.0))

            result = conn.execute(text("""
                UPDATE grid.egon_etrago_generator
                SET marginal_cost = :mc, efficiency = :eta,
                    p_min_pu = :pmin,
                    ramp_limit_up = :ramp, ramp_limit_down = :ramp
                WHERE scn_name = :scn AND carrier = :carrier
            """), {'mc': srmc, 'eta': eta, 'pmin': pmin, 'ramp': ramp,
                   'scn': scn, 'carrier': c})
            print(f"    {c:<16} mc={srmc:>7.1f}  η={eta:.2f}"
                  f"  p_min={pmin:.2f}  ({result.rowcount} rows)")

        # ── Step 3: Storage parameters ────────────────────────────────────────
        if not args.no_storage:
            print(f"\n  [3/5] Updating storage parameters...")
            for carrier, params in STORAGE_PARAMS.items():
                result = conn.execute(text("""
                    UPDATE grid.egon_etrago_storage
                    SET efficiency_store = :es, efficiency_dispatch = :ed,
                        marginal_cost = :mc
                    WHERE scn_name = :scn AND carrier = :carrier
                """), {'es': float(params['efficiency_store']),
                       'ed': float(params['efficiency_dispatch']),
                       'mc': float(params['marginal_cost']),
                       'scn': scn, 'carrier': carrier})
                print(f"    {carrier:<16} η_s={params['efficiency_store']:.2f}"
                      f" η_d={params['efficiency_dispatch']:.2f}"
                      f" mc={params['marginal_cost']:.1f}"
                      f"  ({result.rowcount} rows)")
        else:
            print(f"\n  [3/5] Storage: skipped")

    # ── Step 4 & 5: Hourly profiles (outside transaction for memory) ──────
    if not args.no_profiles:
        _apply_profiles(engine, scn, domestic)
    else:
        print(f"\n  [4/5] CHP profiles: skipped")
        print(f"  [5/5] Renewable profiles: skipped")

    # ── Verification ──────────────────────────────────────────────────────────
    print(f"\n── Verification {'─' * 64}")
    verify = pd.read_sql(f"""
        SELECT carrier, COUNT(*) as n,
               ROUND(SUM(p_nom)::numeric, 0) AS total_mw,
               ROUND(AVG(marginal_cost)::numeric, 1) AS mc,
               ROUND(AVG(efficiency)::numeric, 2) AS eta,
               ROUND(AVG(p_min_pu)::numeric, 2) AS pmin
        FROM grid.egon_etrago_generator
        WHERE scn_name = '{scn}'
        GROUP BY carrier ORDER BY AVG(marginal_cost)
    """, engine)
    print(f"  {'Carrier':<16} {'MC':>8} {'η':>6} {'Pmin':>6}"
          f" {'Cap(MW)':>10} {'Count':>6}")
    print(f"  {'─' * 56}")
    for _, row in verify.iterrows():
        print(f"  {row['carrier']:<16} {row['mc']:>8.1f} {row['eta']:>6.2f}"
              f" {row['pmin']:>6.2f} {int(row['total_mw']):>10,} {int(row['n']):>6}")

    # Count timeseries
    ts_count = pd.read_sql(f"""
        SELECT COUNT(*) as n FROM grid.egon_etrago_generator_timeseries
        WHERE scn_name = '{scn}'
    """, engine).iloc[0]['n']
    print(f"\n  Generator timeseries: {int(ts_count):,} records")
    print(f"\n  Done. Merit order + profiles applied to {scn}.\n")


def _apply_profiles(engine, scn, domestic):
    """Create and insert hourly profiles for CHP and renewables."""
    weather = create_weather_state()
    smard = try_load_smard_profiles()

    # ── Step 4: CHP seasonal p_min_pu ─────────────────────────────────────
    print(f"\n  [4/5] Creating CHP seasonal profiles...")
    chp_mask = domestic['carrier'] == 'gas_chp'
    chp_ids = domestic.loc[chp_mask, 'generator_id'].tolist()
    chp_profile = create_chp_seasonal_profile()

    if chp_ids:
        _insert_timeseries(engine, scn, chp_ids, p_min_pu=chp_profile)
        print(f"    Inserted p_min_pu for {len(chp_ids)} gas_chp generators"
              f" (winter={chp_profile[:744].mean():.2f},"
              f" summer={chp_profile[4344:6552].mean():.2f})")

    # ── Step 5: Renewable p_max_pu ────────────────────────────────────────
    print(f"\n  [5/5] Creating renewable profiles...")

    for carrier in ['solar', 'onwind', 'offwind', 'run_of_river']:
        mask = domestic['carrier'] == carrier
        gen_df = domestic[mask].copy()
        n_gen = len(gen_df)
        if n_gen == 0:
            continue

        print(f"    {carrier}: generating {n_gen:,} profiles...", end='', flush=True)

        # Create profiles using lat/lon binning for efficiency
        # Bin to 0.5° lat × 1.0° lon → ~80 unique bins for Germany
        gen_df['lat_bin'] = (gen_df['lat'] * 2).round() / 2
        gen_df['lon_bin'] = gen_df['lon'].round()
        bins = gen_df.groupby(['lat_bin', 'lon_bin']).first()[['lat', 'lon']].reset_index()

        # Generate one profile per bin
        profile_cache = {}
        for _, b in bins.iterrows():
            key = (b['lat_bin'], b['lon_bin'])
            if smard and carrier in smard:
                # Use SMARD national profile with regional scaling
                # SMARD gives the temporal pattern; we scale absolute level
                # to match expected regional CF
                base = smard[carrier].copy()
                national_cf = base.mean()
                if carrier == 'solar':
                    regional_cf = np.clip(0.115 - 0.004 * (b['lat'] - 51.0),
                                          0.07, 0.14)
                elif carrier == 'onwind':
                    regional_cf = 0.21 + 0.007 * (b['lat'] - 51.0)
                    if b['lon'] < 10:
                        regional_cf += 0.025
                    regional_cf = np.clip(regional_cf, 0.13, 0.30)
                elif carrier == 'offwind':
                    regional_cf = 0.37
                else:  # run_of_river
                    regional_cf = 0.50
                scale = regional_cf / national_cf if national_cf > 0 else 1.0
                profile_cache[key] = np.clip(base * scale, 0, 1)
            else:
                # Generate synthetic profile
                if carrier == 'solar':
                    profile_cache[key] = create_solar_profile(
                        b['lat'], b['lon'], weather)
                elif carrier == 'onwind':
                    profile_cache[key] = create_wind_onshore_profile(
                        b['lat'], b['lon'], weather,
                        seed_offset=int(b['lat_bin'] * 10))
                elif carrier == 'offwind':
                    profile_cache[key] = create_wind_offshore_profile(
                        weather, seed_offset=int(b['lon_bin'] * 10))
                else:
                    profile_cache[key] = create_ror_profile(
                        b['lat'], weather)

        # Map each generator to its bin profile
        gen_ids = []
        gen_profiles = []
        for _, g in gen_df.iterrows():
            key = (g['lat_bin'], g['lon_bin'])
            gen_ids.append(int(g['generator_id']))
            gen_profiles.append(profile_cache[key])

        # Batch insert
        _insert_timeseries(engine, scn, gen_ids,
                           p_max_pu_list=gen_profiles)

        avg_cf = np.mean([p.mean() for p in gen_profiles])
        n_bins = len(profile_cache)
        print(f" done. {n_bins} regional bins, avg CF={avg_cf:.3f}")


def _insert_timeseries(engine, scn, gen_ids, p_min_pu=None,
                       p_max_pu_list=None):
    """Insert generator timeseries into DB. Handles both uniform and per-gen profiles.

    p_min_pu: single array applied to all gen_ids (CHP case)
    p_max_pu_list: list of arrays, one per gen_id (renewable case)
    """
    BATCH = 200

    with engine.begin() as conn:
        # Delete existing timeseries for these generators
        for i in range(0, len(gen_ids), BATCH):
            batch_ids = gen_ids[i:i + BATCH]
            conn.execute(text("""
                DELETE FROM grid.egon_etrago_generator_timeseries
                WHERE scn_name = :scn AND generator_id = ANY(:ids)
            """), {'scn': scn, 'ids': batch_ids})

        # Insert new timeseries
        records = []
        for idx, gid in enumerate(gen_ids):
            rec = {'scn_name': scn, 'generator_id': int(gid), 'temp_id': 1}
            if p_min_pu is not None:
                rec['p_min_pu'] = p_min_pu.tolist()
            if p_max_pu_list is not None:
                rec['p_max_pu'] = p_max_pu_list[idx].tolist()
            records.append(rec)

            if len(records) >= BATCH:
                pd.DataFrame(records).to_sql(
                    'egon_etrago_generator_timeseries', engine,
                    schema='grid', if_exists='append', index=False)
                records = []

        if records:
            pd.DataFrame(records).to_sql(
                'egon_etrago_generator_timeseries', engine,
                schema='grid', if_exists='append', index=False)


if __name__ == '__main__':
    main()
