#!/usr/bin/env python3
"""
Merit order dispatch simulation for 2025 vs real market data.

1. Downloads real 2025 data from SMARD (day-ahead prices + load)
2. Downloads real 2025 generation data from Energy-Charts API
3. Runs merit order dispatch using our grid_beta model (no network constraints)
4. Generates interactive HTML comparison report with hourly resolution

Usage:
    python scripts/simulation/merit_order_comparison.py
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

DB_URL = "postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data"
SCN = "grid_beta"
N_HOURS = 8760
YEAR = 2025
OUTPUT_DIR = "results"
OUTPUT_HTML = os.path.join(OUTPUT_DIR, "merit_order_comparison_2025.html")

# SMARD filter IDs — only series with reliable data
# nuclear (1223) and oil (4075) removed: they return bogus/incomplete data
SMARD_FILTERS = {
    "price": 4169,       # Day-ahead price EUR/MWh
    "solar": 4068,
    "wind_onshore": 4067,
    "wind_offshore": 1225,
    "biomass": 4066,
    "hydro": 4071,
    "pumped_storage": 4070,
    "other_renewables": 4069,
    "gas": 1226,
    "hard_coal": 1227,
    "lignite": 1228,
    "other_conventional": 1224,
    "load": 410,
}

SMARD_BASE = "https://www.smard.de/app/chart_data/{filt}/DE/{filt}_DE_hour_{ts}.json"
SMARD_INDEX = "https://www.smard.de/app/chart_data/{filt}/DE/index_hour.json"

ENERGY_CHARTS_URL = (
    "https://api.energy-charts.info/public_power"
    "?country=de&start=2025-01-01T00:00Z&end=2025-12-31T23:00Z"
)
ENERGY_CHARTS_CACHE = os.path.join(OUTPUT_DIR, ".energy_charts_cache_2025.json")

START_TS = int(datetime(YEAR, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
END_TS = int(datetime(YEAR + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)

# Energy-Charts production_type → our fuel category
ECHARTS_TYPE_MAP = {
    "Solar":                        "solar",
    "Wind onshore":                 "wind_onshore",
    "Wind offshore":                "wind_offshore",
    "Biomass":                      "biomass",
    "Waste":                        "biomass",
    "Hydro Run-of-River":           "hydro",
    "Hydro water reservoir":        "hydro",
    "Fossil gas":                   "gas",
    "Fossil coal-derived gas":      "gas",
    "Fossil hard coal":             "hard_coal",
    "Fossil brown coal / lignite":  "lignite",
    "Fossil oil":                   "oil",
    "Others":                       "other_conventional",
    "Geothermal":                   "other_conventional",
    "Hydro pumped storage":         "pumped_storage",
    "Nuclear":                      "nuclear",
    "Run of River":                 "hydro",
    "Wind Offshore":                "wind_offshore",
    "Wind Onshore":                 "wind_onshore",
}

# Carrier grouping for comparison with energy-charts categories
CARRIER_TO_SMARD = {
    "solar": "solar",
    "onwind": "wind_onshore",
    "offwind": "wind_offshore",
    "biogas": "biomass",
    "biomass": "biomass",
    "run_of_river": "hydro",
    "reservoir": "hydro",
    "gas_ccgt": "gas",
    "gas_chp": "gas",
    "coal": "hard_coal",
    "lignite": "lignite",
    "oil": "oil",
    "other": "other_conventional",
    "waste": "other_conventional",
    "hydrogen": "gas",
}

# Display order and colors for fuel types (no nuclear, no oil in FUEL_ORDER)
FUEL_ORDER = [
    "solar", "wind_onshore", "wind_offshore", "biomass", "hydro", "pumped_storage",
    "gas", "hard_coal", "lignite", "other_conventional", "imports",
]
FUEL_COLORS = {
    "solar": "#f4d44d",
    "wind_onshore": "#4da6ff",
    "wind_offshore": "#1a53ff",
    "biomass": "#66bb6a",
    "hydro": "#29b6f6",
    "gas": "#ff7043",
    "hard_coal": "#616161",
    "lignite": "#8d6e63",
    "oil": "#ef5350",
    "other_conventional": "#ab47bc",
    "nuclear": "#e91e63",
    "imports": "#78909c",
    "pumped_storage": "#26c6da",
    "other_renewables": "#81c784",
}
FUEL_LABELS = {
    "solar": "Solar",
    "wind_onshore": "Wind Onshore",
    "wind_offshore": "Wind Offshore",
    "biomass": "Biomass",
    "hydro": "Hydro",
    "gas": "Gas",
    "hard_coal": "Hard Coal",
    "lignite": "Lignite",
    "oil": "Oil",
    "other_conventional": "Other Conv.",
    "nuclear": "Nuclear",
    "imports": "Imports",
    "pumped_storage": "Pumped Storage",
    "other_renewables": "Other RES",
}


# ═══════════════════════════════════════════════════════════════════════════════
# SMARD DATA DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_json(url, retries=3, delay=2):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                return None


def download_smard_series(name, filt_id):
    """Download hourly SMARD data for target year."""
    url_idx = SMARD_INDEX.format(filt=filt_id)
    data = fetch_json(url_idx)
    if data is None:
        print(f"  WARNING: Could not fetch index for {name} (filter {filt_id})")
        return None

    ts_list = data.get("timestamps", list(data.values())[0]) if isinstance(data, dict) else data
    week_ms = 7 * 24 * 3600 * 1000
    timestamps = sorted(int(ts) for ts in ts_list if int(ts) < END_TS and int(ts) + week_ms > START_TS)

    all_points = []
    for i, ts in enumerate(timestamps):
        url = SMARD_BASE.format(filt=filt_id, ts=ts)
        chunk = fetch_json(url)
        if chunk and "series" in chunk:
            for ts_val, value in chunk["series"]:
                if START_TS <= ts_val < END_TS and value is not None:
                    all_points.append((ts_val, value))
        time.sleep(0.2)

    if not all_points:
        return None

    df = pd.DataFrame(all_points, columns=["timestamp_ms", "value"])
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp")
    return df.set_index("timestamp")["value"]


def download_energy_charts():
    """Download hourly generation from Energy-Charts API for 2025.

    The API returns 15-min data (~35040 points). We resample to hourly by
    averaging groups of 4. Result is a dict mapping fuel category to numpy
    array of 8760 hourly MW values.
    """
    # Try cache first
    if os.path.exists(ENERGY_CHARTS_CACHE):
        print("  Loading Energy-Charts data from cache...")
        with open(ENERGY_CHARTS_CACHE) as f:
            cached = json.load(f)
        result = {}
        for key, vals in cached.items():
            result[key] = np.array(vals, dtype=np.float64)
        print(f"  Energy-Charts: loaded {len(result)} fuel categories from cache")
        return result

    print(f"  Downloading Energy-Charts 2025 data...", end=" ", flush=True)
    raw = fetch_json(ENERGY_CHARTS_URL)
    if raw is None:
        print("FAILED")
        return {}

    # raw structure: {"unix_seconds": [...], "production_types": [{"name": ..., "data": [...]}, ...]}
    unix_seconds = raw.get("unix_seconds", [])
    production_types = raw.get("production_types", [])

    if not unix_seconds or not production_types:
        print("FAILED (unexpected format)")
        return {}

    print(f"{len(unix_seconds)} 15-min points, {len(production_types)} production types")

    # Build DataFrame with UTC timestamps
    ts_index = pd.to_datetime(unix_seconds, unit="s", utc=True)

    # Aggregate by our fuel categories
    category_series = {}
    for pt in production_types:
        name = pt.get("name", "")
        data = pt.get("data", [])
        cat = ECHARTS_TYPE_MAP.get(name)
        if cat is None:
            # Try case-insensitive fallback
            for k, v in ECHARTS_TYPE_MAP.items():
                if k.lower() == name.lower():
                    cat = v
                    break
        if cat is None:
            # Unknown type — print for debugging but skip
            print(f"    [Energy-Charts] Unknown type: {name!r} — skipping")
            continue
        if len(data) != len(unix_seconds):
            continue
        arr = np.array([v if v is not None else np.nan for v in data], dtype=np.float64)
        if cat in category_series:
            # Sum series that map to the same category (e.g. Biomass + Waste → biomass)
            existing = category_series[cat]
            # NaN + value = value; both NaN = NaN
            category_series[cat] = np.where(
                np.isnan(existing) & np.isnan(arr), np.nan,
                np.where(np.isnan(existing), arr,
                np.where(np.isnan(arr), existing, existing + arr))
            )
        else:
            category_series[cat] = arr

    # Resample to hourly: average groups of 4 (15-min → 1h)
    # Build a proper time-indexed DataFrame first
    df_raw = pd.DataFrame(category_series, index=ts_index)

    # Resample to 1h mean
    df_hourly = df_raw.resample("1h").mean()

    # Align to our 8760h grid
    target_index = pd.date_range(f"{YEAR}-01-01", periods=N_HOURS, freq="h", tz="UTC")
    df_aligned = df_hourly.reindex(target_index, method="nearest", tolerance="2h")

    result = {}
    for cat in df_aligned.columns:
        arr = df_aligned[cat].values.astype(np.float64)
        # Fill remaining NaNs by linear interpolation
        nans = np.isnan(arr)
        if nans.any() and (~nans).any():
            arr[nans] = np.interp(np.where(nans)[0], np.where(~nans)[0], arr[~nans])
        elif nans.all():
            arr = np.zeros(N_HOURS)
        result[cat] = arr

    # Cache to disk
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    cache_data = {k: [float(v) if not np.isnan(v) else 0.0 for v in v_arr]
                  for k, v_arr in result.items()}
    with open(ENERGY_CHARTS_CACHE, "w") as f:
        json.dump(cache_data, f)

    print(f"  Energy-Charts: {len(result)} fuel categories, cached to {ENERGY_CHARTS_CACHE}")
    return result


def download_cross_border():
    """Download hourly cross-border trade from Energy-Charts for 2025.
    Returns dict: {"total": array(8760), "countries": {name: array(8760)}}
    Positive = export FROM Germany, negative = import TO Germany.
    """
    cache_path = os.path.join(OUTPUT_DIR, ".cbet_cache_2025.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cached = json.load(f)
        result = {k: np.array(v) for k, v in cached.items()}
        print(f"  Cross-border trade: loaded from cache ({len(result)-1} countries)")
        return result

    url = ("https://api.energy-charts.info/cbet?country=de"
           "&start=2025-01-01T00:00Z&end=2025-12-31T23:00Z")
    print("  Downloading cross-border trade...", end=" ", flush=True)
    raw = fetch_json(url)
    if raw is None:
        print("FAILED")
        return {}

    unix_seconds = raw.get("unix_seconds", [])
    countries_raw = raw.get("countries", [])
    ts_index = pd.to_datetime(unix_seconds, unit="s", utc=True)

    result = {"countries": {}}
    total = np.zeros(len(unix_seconds))
    for entry in countries_raw:
        name = entry.get("name", "")
        data = entry.get("data", [])
        if len(data) != len(unix_seconds):
            continue
        arr = np.array([v if v is not None else 0 for v in data], dtype=np.float64)
        total += arr

        # Resample to hourly
        df_tmp = pd.DataFrame({"val": arr}, index=ts_index)
        hourly = df_tmp.resample("1h").mean()
        target = pd.date_range(f"{YEAR}-01-01", periods=N_HOURS, freq="h", tz="UTC")
        hourly = hourly.reindex(target, method="nearest", tolerance="30min")
        result["countries"][name] = hourly["val"].fillna(0).values[:N_HOURS]

    # Total cross-border (resample)
    df_tot = pd.DataFrame({"val": total}, index=ts_index)
    hourly_tot = df_tot.resample("1h").mean()
    target = pd.date_range(f"{YEAR}-01-01", periods=N_HOURS, freq="h", tz="UTC")
    hourly_tot = hourly_tot.reindex(target, method="nearest", tolerance="30min")
    result["total"] = hourly_tot["val"].fillna(0).values[:N_HOURS]

    # Convention: positive = export, negative = import (Energy-Charts convention
    # may differ — check sign)
    avg = result["total"].mean()
    print(f"{len(countries_raw)} countries, net avg={avg:.0f} MW "
          f"({'export' if avg > 0 else 'import'})")

    # Cache
    cache_data = {}
    for k, v in result.items():
        if isinstance(v, np.ndarray):
            cache_data[k] = v.tolist()
        elif isinstance(v, dict):
            cache_data[k] = {kk: vv.tolist() for kk, vv in v.items()}
    with open(cache_path, "w") as f:
        json.dump(cache_data, f)

    return result


def download_all_smard():
    """Download all needed SMARD series for 2025 plus Energy-Charts generation."""
    cache_path = os.path.join(OUTPUT_DIR, ".smard_cache_2025.json")

    # Try cache first for SMARD series
    if os.path.exists(cache_path):
        print("Loading SMARD data from cache...")
        with open(cache_path) as f:
            cached = json.load(f)
        result = {}
        for key, vals in cached.items():
            if key == "echarts":
                # echarts stored separately; handled below
                continue
            s = pd.Series(vals["values"], index=pd.to_datetime(vals["index"], utc=True), name=key)
            result[key] = s
        print(f"  Loaded {len(result)} SMARD series from cache")
    else:
        print("Downloading SMARD 2025 data (this takes a few minutes)...")
        result = {}
        for name, filt_id in SMARD_FILTERS.items():
            print(f"  Downloading {name} (filter {filt_id})...", end=" ", flush=True)
            series = download_smard_series(name, filt_id)
            if series is not None and len(series) > 0:
                result[name] = series
                print(f"{len(series)} points")
            else:
                print("FAILED")

        # Cache SMARD series to disk
        cache_data = {}
        for key, s in result.items():
            cache_data[key] = {
                "index": [t.isoformat() for t in s.index],
                "values": [float(v) if pd.notna(v) else None for v in s.values],
            }
        with open(cache_path, "w") as f:
            json.dump(cache_data, f)
        print(f"  Cached {len(result)} SMARD series to {cache_path}")

    # Download Energy-Charts generation data
    print("Fetching Energy-Charts generation data...")
    echarts = download_energy_charts()
    result["echarts"] = echarts  # dict: fuel_category → np.array(8760)

    # Download cross-border trade
    cbet = download_cross_border()
    result["cbet"] = cbet

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# LOAD MODEL FROM DATABASE
# ═══════════════════════════════════════════════════════════════════════════════

def load_model():
    """Load generators + timeseries from grid_beta scenario."""
    print(f"\nLoading model from database (scenario: {SCN})...")
    engine = create_engine(DB_URL)

    # Generators
    gens = pd.read_sql(text("""
        SELECT generator_id, carrier, p_nom, marginal_cost, efficiency, bus
        FROM grid.egon_etrago_generator
        WHERE scn_name = :scn
    """), engine, params={"scn": SCN})
    print(f"  Generators: {len(gens)}")

    # Generator timeseries (p_max_pu, p_min_pu)
    gen_ts = pd.read_sql(text("""
        SELECT generator_id, p_max_pu, p_min_pu
        FROM grid.egon_etrago_generator_timeseries
        WHERE scn_name = :scn
    """), engine, params={"scn": SCN})
    print(f"  Generator timeseries: {len(gen_ts)}")

    return gens, gen_ts


# ═══════════════════════════════════════════════════════════════════════════════
# MERIT ORDER DISPATCH
# ═══════════════════════════════════════════════════════════════════════════════

def prepare_smard_demand(smard):
    """Extract SMARD real load as the demand signal (8760h).

    Germany was a net importer in 2025 (imports 76.2 TWh, exports 54.3 TWh).
    We use SMARD load directly as the base demand — cross-border flows are
    handled by import generators and export tranches in the dispatch loop.
    """
    timestamps = pd.date_range(f"{YEAR}-01-01", periods=N_HOURS, freq="h", tz="UTC")
    if "load" not in smard:
        raise ValueError("SMARD load data not available")
    s = smard["load"].reindex(timestamps, method="nearest", tolerance="30min")
    demand = s.values.astype(np.float64)
    nans = np.isnan(demand)
    if nans.any():
        demand[nans] = np.interp(np.where(nans)[0], np.where(~nans)[0], demand[~nans])
    print(f"\n  SMARD demand: min={demand.min():.0f} MW, max={demand.max():.0f} MW, "
          f"mean={demand.mean():.0f} MW, total={demand.sum()/1e6:.1f} TWh")
    return demand


# Day-ahead market bid prices for renewables and non-fossil generators.
# Most modern renewables are on market premium and bid at ~0. Legacy FIT
# plants bid slightly negative. Waste has a disposal credit.
BID_PRICES = {
    "solar": -3,           # Fleet avg: ~5% legacy FIT at -60, 95% at 0
    "onwind": -2,          # Fleet avg: small FIT share
    "offwind": 0,          # All recent, market premium
    "run_of_river": 0,     # Self-scheduling
    "reservoir": 0,        # Dispatchable
    "waste": -15,          # Waste disposal credit
    "biogas": 0,           # Market premium
    "biomass": 0,          # Market premium
}

# CHP must-run bid: heat credit they'd lose if shut down
CHP_MUST_RUN_BID = -40

# Fossil generator parameters for diversified marginal cost calculation.
# Efficiency ranked by commissioning date (COD) when available, p_nom fallback.
# MC = fuel_price / η + emission_factor × CO2_PRICE / η - heat_credit
#
# Base fuel prices (annual avg): TTF=40, coal=13.7, lignite=5, oil=50, CO2=73.8
# These are overridden by monthly seasonal prices in run_milp_uc.
FUEL_PARAMS = {
    # carrier: (fuel_price, emission_factor_tCO2/MWhth, η_min, η_max, heat_credit_EUR/MWh_el)
    "gas_ccgt": (40.0, 0.202, 0.50, 0.62, 0),      # MC range: ~89-110 EUR/MWh
    "gas_chp":  (40.0, 0.202, 0.38, 0.52, 12),      # MC range: ~84-133 EUR/MWh (after modest heat credit)
    "coal":     (13.6, 0.341, 0.34, 0.46, 0),        # MC range: ~85-115 EUR/MWh
    "lignite":  (5.0,  0.404, 0.32, 0.43, 0),        # MC range: ~82-110 EUR/MWh
    "oil":      (50.0, 0.264, 0.30, 0.40, 0),        # MC range: ~175-233 EUR/MWh
    "other":    (30.0, 0.250, 0.30, 0.40, 0),        # MC range: ~135-180 EUR/MWh
}
CO2_PRICE = 75.0  # EUR/tCO2

# Monthly fuel prices (2025) — seasonal variation for gas, coal, CO2
# Sources: ICE TTF front-month, Argus API2 coal, ICE EUA futures
TTF_MONTHLY = {
    1: 47.5, 2: 44.8, 3: 40.2, 4: 35.1, 5: 32.8, 6: 33.5,
    7: 35.2, 8: 36.1, 9: 38.5, 10: 42.3, 11: 45.6, 12: 48.2,
}
COAL_MONTHLY = {
    1: 14.8, 2: 14.2, 3: 13.5, 4: 12.8, 5: 12.2, 6: 12.5,
    7: 13.0, 8: 13.2, 9: 13.8, 10: 14.5, 11: 14.8, 12: 15.2,
}
CO2_MONTHLY = {
    1: 72.0, 2: 68.5, 3: 70.0, 4: 73.5, 5: 75.0, 6: 76.2,
    7: 78.5, 8: 77.0, 9: 75.8, 10: 74.5, 11: 73.0, 12: 71.5,
}

# NTC estimates per border (MW) derived from 99th percentile of observed
# physical cross-border flows (Energy-Charts CBPF 2025 data)
# NTCs: calibrated from 2025 CBPF data
# Real 2025 (CBPF): imports ~46 TWh (avg 5.2 GW), exports ~64 TWh (avg 7.3 GW)
# Import NTCs: 75th percentile of observed physical import flows
NTC_IMPORT = {
    # ×0.88 vs raw p75 to bring annual import volume from ~55 TWh down toward
    # observed ~46 TWh CBPF (the p75 cap was overly permissive for low-price
    # hours where model is more eager than real grid to import)
    "AT": 1580, "BE": 530, "CZ": 1230, "DK": 1760, "FR": 2110,
    "LU": 350, "NL": 1670, "NO": 700, "PL": 1140, "SE": 310, "CH": 1670,
}
# Export NTCs: p75 of real DE→country flows (constrains excess export)
NTC_EXPORT = {
    "AT": 1100, "BE": 970, "CZ": 1100, "DK": 2300, "FR": 3200,
    "LU": 60, "NL": 2600, "NO": 1300, "PL": 940, "SE": 620, "CH": 1700,
}

# Mapping from our import carrier names to neighbor price codes
IMPORT_CARRIER_TO_BORDER = {
    "import_AT": "AT", "import_BE": "BE", "import_CZ": "CZ",
    "import_DK": "DK", "import_FR": "FR", "import_LU": "LU",
    "import_NL": "NL", "import_NO": "NO", "import_PL": "PL",
    "import_SE": "SE", "import_CH": "CH",
}

# Mapping from Energy-Charts price zone codes to our border codes
PRICE_ZONE_TO_BORDER = {
    "AT": "AT", "BE": "BE", "CZ": "CZ", "DK1": "DK", "DK2": "DK",
    "FR": "FR", "NL": "NL", "NO2": "NO", "PL": "PL", "SE4": "SE",
    "CH": "CH", "LU": "LU",
}

NEIGHBOR_PRICES_CACHE = os.path.join(OUTPUT_DIR, ".neighbor_prices_2025.json")
CBPF_CACHE = os.path.join(OUTPUT_DIR, ".cbpf_2025.json")

# Minimum run hours per carrier: once dispatched, stay on for at least this many
# consecutive hours (carrier-level tracking, not per-generator).
MIN_RUN_HOURS = {
    "coal": 6,
    "lignite": 8,
    "gas_ccgt": 3,
    "gas_chp": 2,
    "oil": 1,
}


def load_neighbor_prices():
    """Load hourly day-ahead prices for all neighbor bidding zones (2025).

    Returns dict: border_code → np.array of shape (8760,) in EUR/MWh.
    For borders with two zones (DK1+DK2), returns capacity-weighted average.
    """
    if not os.path.exists(NEIGHBOR_PRICES_CACHE):
        print("  WARNING: Neighbor prices cache not found, using static fallback")
        return None

    with open(NEIGHBOR_PRICES_CACHE) as f:
        raw = json.load(f)

    # Aggregate by border code (DK1+DK2 → DK avg)
    border_prices = {}
    border_counts = {}
    for zone, vals in raw.items():
        border = PRICE_ZONE_TO_BORDER.get(zone, zone)
        arr = np.array(vals[:N_HOURS], dtype=np.float64)
        # Fill NaN/None with forward fill
        nans = np.isnan(arr)
        if nans.any():
            valid_idx = np.where(~nans)[0]
            if len(valid_idx) > 0:
                arr[nans] = np.interp(np.where(nans)[0], valid_idx, arr[valid_idx])
            else:
                arr[:] = 90.0  # fallback
        if border in border_prices:
            border_prices[border] += arr
            border_counts[border] += 1
        else:
            border_prices[border] = arr.copy()
            border_counts[border] = 1

    for b in border_prices:
        border_prices[b] /= border_counts[b]

    print(f"  Loaded neighbor prices for {len(border_prices)} borders:")
    for b in sorted(border_prices):
        arr = border_prices[b]
        print(f"    {b}: mean={arr.mean():.1f}, min={arr.min():.1f}, max={arr.max():.1f}")
    return border_prices


def load_cod_data():
    """Load commissioning date lookup for conventional generators from MaStR.

    Returns dict: (fuel_type, capacity_mw_rounded) → median commissioning year
    for matching grid generators to COD-based efficiency ranking.
    """
    cod_file = "data/processed/conventional_cod.csv"
    if not os.path.exists(cod_file):
        return None
    df = pd.read_csv(cod_file)
    df["cod_year"] = pd.to_datetime(df["commissioning_date"], errors="coerce").dt.year
    # Build lookup: carrier → list of (capacity, cod_year) for efficiency ranking
    FUEL_MAP = {
        "Erdgas": "gas", "Steinkohle": "coal", "Braunkohle": "lignite",
        "Mineralölprodukte": "oil", "Grubengas": "gas", "Koksofengas": "gas",
        "Hochofengas": "gas",
    }
    result = {}
    for _, row in df.iterrows():
        carrier = FUEL_MAP.get(row["fuel_type"])
        if carrier and not pd.isna(row["cod_year"]) and row["capacity_mw"] >= 0.5:
            if carrier not in result:
                result[carrier] = []
            result[carrier].append((row["capacity_mw"], int(row["cod_year"])))
    return result


def diversify_fossil_bids(gens, month=None, cod_data=None):
    """Compute individual marginal costs for fossil generators based on plant efficiency.

    Improvements over v1:
    - COD-based efficiency ranking (when cod_data available): newer plants = more efficient
    - Monthly fuel price variation (when month specified)
    - Still falls back to p_nom ranking when no COD data

    Modifies gens['marginal_cost'] in-place for fossil carriers.
    Also stores efficiency in gens['_eta'] for monthly MC recalculation.
    """
    print("\n  Diversifying fossil generator marginal costs:")
    for carrier, (fuel_price_base, ef, eta_min, eta_max, heat_credit) in FUEL_PARAMS.items():
        mask = gens["carrier"] == carrier
        if not mask.any():
            continue
        idx = gens.index[mask]
        p_nom_vals = gens.loc[idx, "p_nom"].values.astype(float)
        n = len(idx)

        # Determine ranking: prefer COD (newer = rank 0 = most efficient)
        # Fall back to p_nom descending if no COD data
        base_carrier = carrier.replace("_ccgt", "").replace("_chp", "")
        use_cod = (cod_data is not None and base_carrier in cod_data
                   and len(cod_data[base_carrier]) > 10)

        if use_cod:
            # Assign COD-based rank: sort MaStR fleet by COD descending (newest first)
            # Then match grid generators to fleet percentile by capacity
            fleet = sorted(cod_data[base_carrier], key=lambda x: -x[1])  # newest first
            fleet_caps = np.array([c for c, y in fleet])
            fleet_years = np.array([y for c, y in fleet])
            fleet_cum = np.cumsum(fleet_caps)
            fleet_total = fleet_cum[-1]

            # For each generator, find its fleet percentile based on capacity rank
            rank_order = np.argsort(-p_nom_vals)
            ranks = np.empty(n, dtype=float)
            ranks[rank_order] = np.arange(n, dtype=float)

            # Map rank to COD year via fleet distribution
            cod_years = np.zeros(n)
            for i in range(n):
                pct = ranks[i] / max(n - 1, 1)
                target_cap = pct * fleet_total
                j = np.searchsorted(fleet_cum, target_cap)
                j = min(j, len(fleet_years) - 1)
                cod_years[i] = fleet_years[j]

            # Map COD year to efficiency: newer → higher η
            # 1960 → η_min, 2025 → η_max (linear with mild nonlinearity)
            year_min, year_max = 1960, 2025
            year_frac = np.clip((cod_years - year_min) / (year_max - year_min), 0, 1)
            eta_arr = eta_min + (eta_max - eta_min) * year_frac ** 0.8
            rank_method = "COD"
        else:
            # Fallback: rank by p_nom descending (larger = newer)
            rank_order = np.argsort(-p_nom_vals)
            ranks = np.empty(n, dtype=float)
            ranks[rank_order] = np.arange(n, dtype=float)

            if n == 1:
                eta_arr = np.array([eta_max])
            else:
                eta_arr = eta_max - (eta_max - eta_min) * (ranks / (n - 1)) ** 0.6
            rank_method = "p_nom"

        # Store efficiency for later monthly MC recalculation
        gens.loc[idx, "_eta"] = eta_arr
        gens.loc[idx, "_ef"] = ef
        gens.loc[idx, "_heat_credit"] = heat_credit
        gens.loc[idx, "_fuel_carrier"] = base_carrier

        # Use monthly prices if specified
        if month is not None:
            if base_carrier == "gas":
                fuel_price = TTF_MONTHLY[month]
            elif base_carrier == "coal":
                fuel_price = COAL_MONTHLY[month]
            else:
                fuel_price = fuel_price_base
            co2_price = CO2_MONTHLY[month]
        else:
            fuel_price = fuel_price_base
            co2_price = CO2_PRICE

        mc_arr = fuel_price / eta_arr + ef * co2_price / eta_arr - heat_credit

        gens.loc[idx, "marginal_cost"] = mc_arr
        print(f"    {carrier:12s}: n={n:4d}, η=[{eta_min:.2f}..{eta_max:.2f}], "
              f"MC=[{mc_arr.min():.0f}..{mc_arr.max():.0f}] EUR/MWh ({rank_method})")


def run_merit_order(gens, gen_ts, demand):
    """Run merit order dispatch for all 8760 hours with negative prices.

    The dispatch models the day-ahead market:
    - Each generator bids its available capacity at a bid price
    - Renewables bid negative (FIT/premium loss on curtailment)
    - CHP must-run tranches bid very negative (heat credit loss)
    - Fossil generators have diversified MCs based on plant efficiency
    - Min-run constraints add price persistence for thermal plants
    - Generators are stacked by bid price, cheapest first
    - Clearing price = bid of the marginal generator where supply meets demand
    - Negative prices arise naturally when RES + must-run > demand
    """
    print(f"\nRunning merit order dispatch for {N_HOURS} hours...")

    # Apply diversified fossil marginal costs
    diversify_fossil_bids(gens)

    # Build p_max_pu / p_min_pu matrices: (n_gens, 8760)
    ts_map = gen_ts.set_index("generator_id")
    n_gens = len(gens)
    p_max_pu = np.ones((n_gens, N_HOURS), dtype=np.float32)
    p_min_pu = np.zeros((n_gens, N_HOURS), dtype=np.float32)

    gen_ids = gens["generator_id"].values
    id_to_idx = {gid: i for i, gid in enumerate(gen_ids)}

    for _, row in ts_map.iterrows():
        gid = row.name
        if gid not in id_to_idx:
            continue
        idx = id_to_idx[gid]
        if row["p_max_pu"] is not None and len(row["p_max_pu"]) >= N_HOURS:
            p_max_pu[idx, :] = np.array(row["p_max_pu"][:N_HOURS], dtype=np.float32)
        if row["p_min_pu"] is not None and len(row["p_min_pu"]) >= N_HOURS:
            p_min_pu[idx, :] = np.array(row["p_min_pu"][:N_HOURS], dtype=np.float32)

    # Static generator data
    p_nom = gens["p_nom"].values.astype(np.float64)
    mc = gens["marginal_cost"].values.astype(np.float64)
    carriers = gens["carrier"].values

    # RES availability: SMARD profiles are post-curtailment, so only account
    # for maintenance + forced outages (NREL PV: 97-99%, IEA Wind: 95-97%)
    RES_AVAILABILITY = {"solar": 0.97, "onwind": 0.96, "offwind": 0.93}
    for i in range(n_gens):
        if carriers[i] in RES_AVAILABILITY:
            p_max_pu[i, :] *= RES_AVAILABILITY[carriers[i]]

    # Hydro CF: profiles already embed ~60% resource factor; apply remaining
    # availability only (target 16.7 TWh on 5.2 GW installed)
    HYDRO_CF_MO = {"run_of_river": 0.62, "reservoir": 0.52}
    for i in range(n_gens):
        if carriers[i] in HYDRO_CF_MO:
            p_max_pu[i, :] *= HYDRO_CF_MO[carriers[i]]

    # Build bid tranches
    carrier_groups = np.array([CARRIER_TO_SMARD.get(c, "imports") for c in carriers])

    # Mark import generators (keep in dispatch, they fill the 35-70 EUR gap)
    is_import = np.array([c.startswith("import_") for c in carriers])
    is_domestic = ~is_import

    # Assign bid prices: renewables use BID_PRICES; fossils use diversified MC
    bid_price = np.copy(mc)
    for carrier, bp in BID_PRICES.items():
        mask = carriers == carrier
        bid_price[mask] = bp

    # Identify CHP generators with must-run (has p_min_pu timeseries > 0)
    has_must_run = np.zeros(n_gens, dtype=bool)
    for i in range(n_gens):
        if p_min_pu[i].max() > 0.01:
            has_must_run[i] = True

    n_chp_mr = has_must_run.sum()
    print(f"  Generators: {n_gens} ({n_chp_mr} with must-run tranches)")

    # Build tranche arrays:
    # For each generator, we have up to 2 tranches (must-run + flexible)
    # Tranche = (bid, gen_idx, tranche_type)
    # tranche_type: 0 = normal/flexible, 1 = must-run
    max_tranches = n_gens + n_chp_mr
    tr_bid = np.zeros(max_tranches, dtype=np.float64)
    tr_gen_idx = np.zeros(max_tranches, dtype=np.int32)
    tr_type = np.zeros(max_tranches, dtype=np.int8)  # 0=normal, 1=must-run
    tr_count = 0

    for i in range(n_gens):
        if has_must_run[i]:
            # Must-run tranche
            tr_bid[tr_count] = CHP_MUST_RUN_BID
            tr_gen_idx[tr_count] = i
            tr_type[tr_count] = 1
            tr_count += 1
            # Flexible tranche
            tr_bid[tr_count] = mc[i]  # flexible part bids at marginal cost
            tr_gen_idx[tr_count] = i
            tr_type[tr_count] = 0
            tr_count += 1
        else:
            tr_bid[tr_count] = bid_price[i]
            tr_gen_idx[tr_count] = i
            tr_type[tr_count] = 0
            tr_count += 1

    # Trim and sort by bid price
    tr_bid = tr_bid[:tr_count]
    tr_gen_idx = tr_gen_idx[:tr_count]
    tr_type = tr_type[:tr_count]

    sort_order = np.argsort(tr_bid, kind="stable")
    tr_bid = tr_bid[sort_order]
    tr_gen_idx = tr_gen_idx[sort_order]
    tr_type = tr_type[sort_order]

    tr_groups = carrier_groups[tr_gen_idx]
    tr_carriers = carriers[tr_gen_idx]
    tr_p_nom = p_nom[tr_gen_idx]

    print(f"  Tranches: {tr_count} (sorted by bid price)")
    print(f"  Bid range: {tr_bid[0]:.0f} to {tr_bid[-1]:.0f} EUR/MWh")

    # Cross-border exports.
    # Germany was a NET IMPORTER in 2025 (imports 76.2 TWh, exports 54.3 TWh).
    # Price-responsive exports. Germany exported 54.3 TWh in 2025.
    # Exports are concentrated in low-price hours. We model 10 GW at -4
    # (activates just before solar at -3) and 5 GW at 40 (competitive exports
    # when German price is moderate). Total 15 GW = Germany's interconnector capacity.
    # Stepped export model: neighbors buy at different price levels.
    # Total ~20 GW (Germany's interconnector capacity).
    EXPORT_TRANCHES = [
        (-4, 8000),   # 8 GW transit + obligatory (very cheap power)
        (15, 3000),   # 3 GW storage charging + Nordic buying
        (35, 4000),   # 4 GW France/Austria competitive
        (55, 3000),   # 3 GW Netherlands/Belgium/Czech
        (75, 2000),   # 2 GW marginal export (close to German price)
    ]
    total_export_cap = sum(mw for _, mw in EXPORT_TRANCHES)
    print(f"  Export demand: {len(EXPORT_TRANCHES)} tranche(s), "
          f"{total_export_cap/1e3:.0f} GW max at prices {[p for p,_ in EXPORT_TRANCHES]}")

    # Initialize output arrays
    clearing_price = np.zeros(N_HOURS, dtype=np.float64)
    dispatch_by_fuel = {fuel: np.zeros(N_HOURS, dtype=np.float64) for fuel in FUEL_ORDER}
    # Also track oil separately for the echarts comparison (oil not in FUEL_ORDER display)
    dispatch_by_fuel["oil"] = np.zeros(N_HOURS, dtype=np.float64)
    export_mw = np.zeros(N_HOURS, dtype=np.float64)

    # Min-run tracking: per carrier
    # on_hours[carrier] = consecutive hours carrier has been dispatched
    # prev_dispatch[carrier] = MW dispatched in previous hour
    mr_carriers = set(MIN_RUN_HOURS.keys())
    on_hours = {c: 0 for c in mr_carriers}
    prev_dispatch = {c: 0.0 for c in mr_carriers}

    neg_hours = 0

    for h in range(N_HOURS):
        # Compute forced-on capacity per carrier from min-run constraints
        forced_on = {}  # carrier → MW that must be dispatched this hour
        for c in mr_carriers:
            min_run = MIN_RUN_HOURS[c]
            if on_hours[c] > 0 and on_hours[c] < min_run:
                forced_on[c] = prev_dispatch[c]
            else:
                forced_on[c] = 0.0

        remaining = demand[h]
        # Pre-dispatch forced-on carriers (add their must-run to remaining demand)
        for c, forced_mw in forced_on.items():
            if forced_mw > 0:
                remaining += forced_mw  # will be "consumed" by the carrier's tranche

        marginal_price = 0.0
        export_idx = 0  # which export tranche to activate next
        h_export = 0.0

        # Per-carrier dispatch tracking for this hour
        h_carrier_dispatch = {c: 0.0 for c in mr_carriers}

        for t in range(tr_count):
            # Insert export demand when we cross a price threshold
            while export_idx < len(EXPORT_TRANCHES):
                exp_price, exp_mw = EXPORT_TRANCHES[export_idx]
                if tr_bid[t] >= exp_price:
                    remaining += exp_mw
                    h_export += exp_mw
                    export_idx += 1
                else:
                    break

            gi = tr_gen_idx[t]
            t_carrier = tr_carriers[t]

            if tr_type[t] == 1:
                # Must-run tranche: capacity = p_min_pu * p_nom
                cap = p_min_pu[gi, h] * p_nom[gi]
            else:
                if has_must_run[gi]:
                    # Flexible tranche: capacity = (p_max_pu - p_min_pu) * p_nom
                    cap = max(0, (p_max_pu[gi, h] - p_min_pu[gi, h]) * p_nom[gi])
                else:
                    # Normal generator: full available capacity
                    # Check if this carrier has forced-on obligation
                    if t_carrier in forced_on and forced_on[t_carrier] > 0:
                        # Ensure at least the forced amount is dispatched
                        cap = p_max_pu[gi, h] * p_nom[gi]
                    else:
                        cap = p_max_pu[gi, h] * p_nom[gi]

            if cap <= 0:
                continue

            dispatched = min(cap, remaining)
            if dispatched <= 0 and tr_type[t] != 1:
                continue
            if dispatched <= 0:
                dispatched = cap

            fuel = tr_groups[t]
            if fuel in dispatch_by_fuel:
                dispatch_by_fuel[fuel][h] += dispatched
            remaining -= dispatched
            marginal_price = tr_bid[t]

            # Track carrier-level dispatch for min-run
            if t_carrier in h_carrier_dispatch:
                h_carrier_dispatch[t_carrier] += dispatched

            if remaining <= 0 and tr_type[t] != 1:
                break

        export_mw[h] = h_export

        clearing_price[h] = marginal_price
        if marginal_price < 0:
            neg_hours += 1

        # Update min-run on_hours and prev_dispatch
        for c in mr_carriers:
            disp = h_carrier_dispatch[c]
            if disp > 0:
                on_hours[c] += 1
                prev_dispatch[c] = disp
            else:
                on_hours[c] = 0
                prev_dispatch[c] = 0.0

        if h % 1000 == 0:
            print(f"  Hour {h}/{N_HOURS}: demand={demand[h]:.0f} MW, "
                  f"export={h_export:.0f} MW, price={marginal_price:.1f} EUR/MWh")

    total_dispatched = sum(d.sum() for k, d in dispatch_by_fuel.items() if k != "oil")
    total_export = export_mw.sum() / 1e6
    print(f"\n  Total dispatched: {total_dispatched/1e6:.1f} TWh "
          f"(domestic {(total_dispatched - export_mw.sum())/1e6:.1f} + export {total_export:.1f})")
    print(f"  Export: avg={export_mw.mean():.0f} MW, max={export_mw.max():.0f} MW, "
          f"total={total_export:.1f} TWh")
    print(f"  Clearing price: min={clearing_price.min():.1f}, "
          f"max={clearing_price.max():.1f}, mean={clearing_price.mean():.1f} EUR/MWh")
    print(f"  Negative price hours: {neg_hours} ({neg_hours/N_HOURS*100:.1f}%)")

    return clearing_price, dispatch_by_fuel, demand


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT COMMITMENT DISPATCH
# ═══════════════════════════════════════════════════════════════════════════════

# UC parameters per carrier:
# (startup_cost_eur_per_mw, min_up_h, min_down_h, ramp_pct, p_min_pct, noload_eur_per_mw_h)
UC_PARAMS = {
    "gas_ccgt": (40,   3,  2, 0.10, 0.40, 4),
    "gas_chp":  (25,   2,  2, 0.08, 0.30, 3),
    "coal":     (50,  8,  6, 0.04, 0.35, 5),    # coal: warm-start, 8h min-up
    "lignite":  (200, 168, 72, 0.015, 0.55, 8), # lignite: baseload, 1-week min-up, 3-day min-down
    "oil":      (15,  1, 1, 0.15, 0.20, 2),
    "other":    (30,  2, 2, 0.05, 0.30, 3),
}

# RES availability bundles three effects into one per-carrier multiplier on p_max_pu:
#   (a) MaStR Jan-2026 → 2025-average vintage (DE 2025 additions: +16.4 GW solar,
#       +4.6 GW onwind, +0.3 GW offwind — fleet at year-avg is materially smaller),
#   (b) maintenance + forced outages,
#   (c) residual profile-vs-actual scaling (SMARD profiles already post-curtailment).
RES_AVAILABILITY_UC = {"solar": 0.95, "onwind": 0.76, "offwind": 0.82}

# Carriers treated as variable RES (get p_max_pu profile × availability)
RES_VAR_CARRIERS = {"solar", "onwind", "offwind"}

# Carriers treated as RES (must-take, bid at BID_PRICES — not UC-dispatched)
RES_ALL_CARRIERS = {"solar", "onwind", "offwind", "biogas", "biomass",
                    "run_of_river", "reservoir", "waste"}


class ThermalCluster:
    """Aggregated cluster of similar thermal generators for UC heuristic."""

    __slots__ = (
        "name", "carrier", "p_nom", "p_min", "mc", "mc_min", "mc_max",
        "startup_cost", "min_up", "min_down", "ramp", "noload",
        "fuel_group",
        "committed", "hours_on", "hours_off", "output",
    )

    def __init__(self, name, carrier, p_nom, mc, uc_params, fuel_group,
                 mc_min=None, mc_max=None):
        startup_eur_per_mw, min_up, min_down, ramp_pct, p_min_pct, noload_per_mw = uc_params
        self.name = name
        self.carrier = carrier
        self.p_nom = float(p_nom)
        self.p_min = float(p_nom) * p_min_pct
        self.mc = float(mc)
        self.mc_min = float(mc_min if mc_min is not None else mc)
        self.mc_max = float(mc_max if mc_max is not None else mc)
        self.startup_cost = float(startup_eur_per_mw) * float(p_nom)   # EUR total
        self.min_up = int(min_up)
        self.min_down = int(min_down)
        self.ramp = float(ramp_pct) * float(p_nom)                      # MW/h
        self.noload = float(noload_per_mw) * float(p_nom)               # EUR/h
        self.fuel_group = fuel_group
        # State
        self.committed = False
        self.hours_on = 0
        self.hours_off = 999    # Start fully rested (can commit immediately)
        self.output = 0.0

    def can_commit(self):
        return self.hours_off >= self.min_down

    def can_decommit(self):
        return self.hours_on >= self.min_up

    def max_output(self):
        """Maximum ramp-limited output this hour."""
        if not self.committed:
            return 0.0
        if self.ramp > 0:
            return min(self.p_nom, self.output + self.ramp)
        return self.p_nom

    def min_output(self):
        return self.p_min if self.committed else 0.0

    def effective_mc(self, at_output=None):
        """MC at a given output level, including no-load and startup adder.

        Within a cluster, cheaper generators dispatch first. The marginal
        MC rises linearly from mc_min (at p_min) to mc_max (at p_nom).
        """
        if at_output is None:
            at_output = self.output

        # Interpolate MC based on output level within the cluster
        if self.p_nom > self.p_min and self.mc_max > self.mc_min:
            frac = max(0, min(1, (at_output - self.p_min) / (self.p_nom - self.p_min)))
            base = self.mc_min + (self.mc_max - self.mc_min) * frac
        else:
            base = self.mc

        if self.p_nom > 0:
            base += self.noload / self.p_nom
            if self.hours_on < self.min_up and self.committed:
                base += self.startup_cost / (self.p_nom * max(self.min_up, 1))
        return base

    def step_on(self, output):
        self.committed = True
        if self.hours_off > 0:
            self.hours_on = 1
        else:
            self.hours_on += 1
        self.hours_off = 0
        self.output = float(output)

    def step_off(self):
        self.committed = False
        self.hours_on = 0
        self.hours_off += 1
        self.output = 0.0


class ImportCluster:
    """Import cluster — no UC constraints, always available."""

    __slots__ = ("name", "p_nom", "mc", "fuel_group")

    def __init__(self, name, p_nom, mc, fuel_group="imports"):
        self.name = name
        self.p_nom = float(p_nom)
        self.mc = float(mc)
        self.fuel_group = fuel_group


class PumpedStorageUnit:
    """Pumped storage hydro unit for MILP UC.

    Defaults reflect the DE-coupled pumped-hydro fleet: BNetzA Kraftwerksliste
    2025 lists ~9 GW domestic; effective ~10 GW including Luxembourg Vianden
    plant which is electrically connected to the German grid. Total reservoir
    storage ~50 GWh."""

    def __init__(self, p_gen=9500, p_charge=9500, e_max=50000, eta_rt=0.75):
        self.p_gen = float(p_gen)       # MW max generation
        self.p_charge = float(p_charge) # MW max charging
        self.e_max = float(e_max)        # MWh max storage
        self.eta = eta_rt ** 0.5         # one-way efficiency (sqrt of round-trip)
        self.soc = e_max * 0.5           # start at 50%


def _build_uc_clusters(gens, n_quintiles=15):
    """Group generators into ThermalCluster and ImportCluster objects.

    Returns:
        thermal_clusters  — list of ThermalCluster (sorted by MC ascending)
        import_clusters   — list of ImportCluster (sorted by MC ascending)
    """
    thermal_clusters = []
    import_clusters = []

    # --- Import generators ---
    imp_mask = gens["carrier"].str.startswith("import_")
    imp_gens = gens[imp_mask]
    if len(imp_gens) > 0:
        # Group by MC bins (up to n_quintiles bins)
        p_noms = imp_gens["p_nom"].values.astype(float)
        mcs = imp_gens["marginal_cost"].values.astype(float)
        n_bins = min(n_quintiles, max(1, len(imp_gens) // 2))
        mc_bins = np.array_split(np.argsort(mcs), n_bins)
        for k, idxs in enumerate(mc_bins):
            if len(idxs) == 0:
                continue
            total_p = p_noms[idxs].sum()
            avg_mc = float(np.average(mcs[idxs], weights=p_noms[idxs]))
            import_clusters.append(
                ImportCluster(f"import_bin_{k}", total_p, avg_mc, "imports")
            )
    import_clusters.sort(key=lambda c: c.mc)

    # --- Thermal generators ---
    uc_carriers = set(UC_PARAMS.keys())
    therm_mask = gens["carrier"].isin(uc_carriers)
    therm_gens = gens[therm_mask]

    for carrier in uc_carriers:
        cg = therm_gens[therm_gens["carrier"] == carrier]
        if len(cg) == 0:
            continue
        p_noms = cg["p_nom"].values.astype(float)
        mcs = cg["marginal_cost"].values.astype(float)
        uc_p = UC_PARAMS[carrier]
        fuel_group = CARRIER_TO_SMARD.get(carrier, "other_conventional")

        n_bins = min(n_quintiles, max(1, len(cg) // 2))
        mc_bins = np.array_split(np.argsort(mcs), n_bins)
        for k, idxs in enumerate(mc_bins):
            if len(idxs) == 0:
                continue
            total_p = p_noms[idxs].sum()
            avg_mc = float(np.average(mcs[idxs], weights=p_noms[idxs]))
            min_mc = float(mcs[idxs].min())
            max_mc = float(mcs[idxs].max())
            name = f"{carrier}_bin_{k}"
            thermal_clusters.append(
                ThermalCluster(name, carrier, total_p, avg_mc, uc_p, fuel_group,
                               mc_min=min_mc, mc_max=max_mc)
            )

    thermal_clusters.sort(key=lambda c: c.mc)
    return thermal_clusters, import_clusters


def run_unit_commitment(gens, gen_ts, demand):
    """Run priority-list unit commitment heuristic for all 8760 hours.

    Steps:
    1. Pre-compute RES output and CHP must-run/flexible profiles from timeseries.
    2. Aggregate thermal generators into UC clusters (by carrier × MC quintile).
    3. Each hour: residual demand → commitment decisions → economic dispatch.
    4. Clearing price = effective MC of marginal cluster + startup/no-load adder.
    5. Return same (clearing_price, dispatch_by_fuel, demand) tuple as run_merit_order.
    """
    print(f"\nRunning unit commitment dispatch for {N_HOURS} hours...")

    # Apply diversified fossil marginal costs (same as merit order)
    diversify_fossil_bids(gens)

    # Update import generator MCs to reflect real 2025 European neighbor prices
    # (original values were set at build time with 2023 assumptions)
    IMPORT_MC_2025 = {
        "import_FR": 75,   # France: nuclear recovery, avg ~85 EUR, import at ~75
        "import_SE": 52,   # Sweden: hydro-rich, ~50-55
        "import_NO": 48,   # Norway: hydro, ~45-50
        "import_DK": 58,   # Denmark: wind-rich, ~55-65
        "import_CH": 72,   # Switzerland: hydro+nuclear, ~70-80
        "import_AT": 78,   # Austria: closely coupled with DE, ~85-90
        "import_PL": 80,   # Poland: coal-dominated, ~80-85
        "import_CZ": 76,   # Czech Republic: nuclear+lignite, ~75-85
        "import_BE": 78,   # Belgium: nuclear+gas, ~80-85
        "import_LU": 78,   # Luxembourg: tracks DE-LU zone
        "import_NL": 82,   # Netherlands: gas-heavy, ~80-90
    }
    for idx, row in gens.iterrows():
        if row["carrier"] in IMPORT_MC_2025:
            gens.loc[idx, "marginal_cost"] = IMPORT_MC_2025[row["carrier"]]
    print(f"  Updated import MCs to 2025 neighbor prices: {min(IMPORT_MC_2025.values())}-{max(IMPORT_MC_2025.values())} EUR/MWh")

    # ── Build p_max_pu / p_min_pu matrices: shape (n_gens, N_HOURS) ──────────
    ts_map = gen_ts.set_index("generator_id")
    n_gens = len(gens)
    p_max_pu = np.ones((n_gens, N_HOURS), dtype=np.float32)
    p_min_pu = np.zeros((n_gens, N_HOURS), dtype=np.float32)

    gen_ids = gens["generator_id"].values
    id_to_idx = {gid: i for i, gid in enumerate(gen_ids)}

    for _, row in ts_map.iterrows():
        gid = row.name
        if gid not in id_to_idx:
            continue
        idx = id_to_idx[gid]
        if row["p_max_pu"] is not None and len(row["p_max_pu"]) >= N_HOURS:
            p_max_pu[idx, :] = np.array(row["p_max_pu"][:N_HOURS], dtype=np.float32)
        if row["p_min_pu"] is not None and len(row["p_min_pu"]) >= N_HOURS:
            p_min_pu[idx, :] = np.array(row["p_min_pu"][:N_HOURS], dtype=np.float32)

    # Static arrays
    p_nom = gens["p_nom"].values.astype(np.float64)
    carriers = gens["carrier"].values

    # Apply RES availability (maintenance + forced outages only)
    for i in range(n_gens):
        if carriers[i] in RES_AVAILABILITY_UC:
            p_max_pu[i, :] *= RES_AVAILABILITY_UC[carriers[i]]

    # Hydro CF: Germany 2025 was dry year — RoR ~36% (Fraunhofer ISE)
    HYDRO_CF_HEU = {"run_of_river": 0.36, "reservoir": 0.30}
    for i in range(n_gens):
        if carriers[i] in HYDRO_CF_HEU:
            p_max_pu[i, :] *= HYDRO_CF_HEU[carriers[i]]

    # ── Step 1: Pre-compute hourly RES, CHP must-run, CHP flexible ───────────

    # Identify generator categories
    is_import = np.array([c.startswith("import_") for c in carriers])
    is_chp = np.array([c == "gas_chp" for c in carriers])
    has_must_run = np.array([p_min_pu[i].max() > 0.01 for i in range(n_gens)])
    is_chp_mr = is_chp & has_must_run
    is_res = np.array([c in RES_ALL_CARRIERS for c in carriers])
    is_thermal = np.array([c in UC_PARAMS for c in carriers]) & ~is_chp_mr

    # RES output by fuel group: (fuel_group → array of 8760)
    res_by_fuel_group = {}
    for i in range(n_gens):
        if not is_res[i]:
            continue
        fuel = CARRIER_TO_SMARD.get(carriers[i], "other_conventional")
        output = p_max_pu[i, :].astype(np.float64) * p_nom[i]
        if fuel in res_by_fuel_group:
            res_by_fuel_group[fuel] += output
        else:
            res_by_fuel_group[fuel] = output.copy()

    # Total RES per hour
    total_res = np.zeros(N_HOURS, dtype=np.float64)
    for arr in res_by_fuel_group.values():
        total_res += arr

    # CHP must-run per hour.
    # Scale down by 45%: not all CHP capacity is heat-constrained. In reality
    # only ~45% of 23 GW CHP is district heating must-run. The rest is
    # industrial CHP or flexible CHP that responds to electricity prices.
    CHP_MUST_RUN_SCALE = 0.28
    GAS_CHP_AVAILABILITY_HEU = 0.83
    chp_must_run = np.zeros(N_HOURS, dtype=np.float64)
    chp_flex_cap = np.zeros(N_HOURS, dtype=np.float64)
    for i in range(n_gens):
        if not is_chp_mr[i]:
            continue
        avail_pnom = p_nom[i] * GAS_CHP_AVAILABILITY_HEU
        must = p_min_pu[i, :].astype(np.float64) * avail_pnom * CHP_MUST_RUN_SCALE
        flex_base = np.maximum(0.0, (p_max_pu[i, :].astype(np.float64) - p_min_pu[i, :].astype(np.float64)) * avail_pnom)
        freed = p_min_pu[i, :].astype(np.float64) * avail_pnom * (1 - CHP_MUST_RUN_SCALE)
        flex = flex_base + freed
        chp_must_run += must
        chp_flex_cap += flex

    # CHP must-run "MC": dispatch at CHP_MUST_RUN_BID (negative = heat credit)
    # CHP flexible capacity has individual MC from diversify_fossil_bids

    # ── Step 2: Build UC clusters ─────────────────────────────────────────────
    thermal_clusters, import_clusters = _build_uc_clusters(gens)
    all_clusters = thermal_clusters + import_clusters
    n_thermal = len(thermal_clusters)

    print(f"  UC clusters: {n_thermal} thermal + {len(import_clusters)} import")
    if thermal_clusters:
        print(f"  Thermal MC range: {thermal_clusters[0].mc:.0f} to "
              f"{thermal_clusters[-1].mc:.0f} EUR/MWh")
    print(f"  Total thermal capacity: "
          f"{sum(c.p_nom for c in thermal_clusters)/1e3:.1f} GW")
    print(f"  Total import capacity:  "
          f"{sum(c.p_nom for c in import_clusters)/1e3:.1f} GW")
    print(f"  CHP must-run peak: {chp_must_run.max():.0f} MW, "
          f"flex peak: {chp_flex_cap.max():.0f} MW")

    # Pre-commit lignite and coal clusters (they run baseload/semi-baseload
    # in reality — cold-start takes days and costs millions of EUR)
    lig_committed = 0
    coal_committed = 0
    for c in thermal_clusters:
        if c.carrier == "lignite":
            c.committed = True
            c.hours_on = 999
            c.hours_off = 0
            c.output = c.p_min
            lig_committed += c.p_nom
        elif c.carrier == "coal":
            c.committed = True
            c.hours_on = 999
            c.hours_off = 0
            c.output = c.p_min
            coal_committed += c.p_nom
    print(f"  Pre-committed: lignite {lig_committed/1e3:.1f} GW, coal {coal_committed/1e3:.1f} GW")

    # Add pumped storage as supply (generating) and demand (charging)
    # Germany: 6.5 GW pumped storage, generates ~10 TWh, consumes ~13 TWh
    PUMPED_STORAGE_GW = 6.5
    PS_CHARGE_PRICE = 60   # charges when price < 60
    PS_GEN_MC = 85         # generates at MC 85 (slightly below gas CCGT)
    # Add as import-like cluster (generating side)
    ps_cluster = ImportCluster("pumped_storage_gen", PUMPED_STORAGE_GW * 1000, PS_GEN_MC, "pumped_storage")
    import_clusters.append(ps_cluster)
    import_clusters.sort(key=lambda c: c.mc)
    print(f"  Pumped storage: {PUMPED_STORAGE_GW} GW gen at MC {PS_GEN_MC}, "
          f"charging at export tranche {PS_CHARGE_PRICE} EUR")

    # Export tranches: include pumped storage charging
    EXPORT_TRANCHES = [
        (-4, 8000),    # 8 GW base export
        (15, 3000),    # 3 GW Nordic/storage charging
        (40, 4000),    # 4 GW competitive export
        (PS_CHARGE_PRICE, int(PUMPED_STORAGE_GW * 1000)),  # 6.5 GW pump charging
    ]
    total_export_cap = sum(mw for _, mw in EXPORT_TRANCHES)
    print(f"  Export demand: {len(EXPORT_TRANCHES)} tranches, "
          f"{total_export_cap/1e3:.0f} GW max")

    # ── CHP flexible: build per-MC-bin clusters for hourly dispatch ───────────
    # We model flexible CHP separately as hourly-available capacity buckets.
    # Grouped by MC quintile (5 bins) using non-CHP-must-run gas_chp gens.
    # gas_chp generators WITHOUT must-run timeseries are also included here.
    chp_flex_gens_mask = np.array(
        [carriers[i] == "gas_chp" and not is_chp_mr[i] for i in range(n_gens)]
    )
    # Build CHP flexible clusters (purely for dispatch ordering)
    chp_flex_clusters = []
    chp_flex_idxs = np.where(chp_flex_gens_mask)[0]
    if len(chp_flex_idxs) > 0:
        cf_pnom = p_nom[chp_flex_idxs]
        cf_mc = gens.loc[gens.index[chp_flex_idxs], "marginal_cost"].values.astype(float)
        n_bins = min(5, max(1, len(chp_flex_idxs) // 2))
        cf_bins = np.array_split(np.argsort(cf_mc), n_bins)
        for k, idxs in enumerate(cf_bins):
            if len(idxs) == 0:
                continue
            total_p = cf_pnom[idxs].sum()
            avg_mc = float(np.average(cf_mc[idxs], weights=cf_pnom[idxs]))
            chp_flex_clusters.append((total_p, avg_mc))

    # Compute total CHP must-run output per hour for dispatch output tracking
    # (already computed above as chp_must_run)

    # ── Output arrays ─────────────────────────────────────────────────────────
    clearing_price = np.zeros(N_HOURS, dtype=np.float64)
    dispatch_by_fuel = {fuel: np.zeros(N_HOURS, dtype=np.float64) for fuel in FUEL_ORDER}
    dispatch_by_fuel["oil"] = np.zeros(N_HOURS, dtype=np.float64)
    export_mw = np.zeros(N_HOURS, dtype=np.float64)

    neg_hours = 0
    total_curtailed = 0.0

    # ── Step 4: Hour-by-hour UC loop ──────────────────────────────────────────
    for h in range(N_HOURS):
        # --- Export: decide which tranches to activate ---
        # We look at residual before exports to determine export level.
        # residual_pre_export = demand[h] - total_res[h] - chp_must_run[h]
        residual_pre = demand[h] - total_res[h] - chp_must_run[h]

        # Determine export tranches: activate if generation likely to be cheap
        h_export = 0.0
        active_export_prices = []
        for exp_price, exp_mw in EXPORT_TRANCHES:
            # Activate if residual is low (low price) or negative (surplus)
            # Threshold: if residual_pre < export price × some factor, activate
            # Simple heuristic: activate tranche if marginal cost of
            # clearing residual is likely below exp_price.
            # We use a forward-looking proxy: if residual_pre is below
            # committed thermal p_min (surplus), activate all tranches;
            # if residual_pre is moderate, activate cheap tranches.
            committed_pmin = sum(c.p_min for c in thermal_clusters if c.committed)
            if residual_pre <= committed_pmin + exp_price * 100:
                # Simple linear trigger: exp_price -4→ always, 10→ if surplus, 40→ if cheap
                if exp_price <= -4:
                    h_export += exp_mw
                    active_export_prices.append(exp_price)
                elif exp_price <= 10 and residual_pre < sum(c.p_nom for c in thermal_clusters if c.committed) * 0.3:
                    h_export += exp_mw
                    active_export_prices.append(exp_price)
                elif exp_price <= 40 and residual_pre < 5000:
                    h_export += exp_mw
                    active_export_prices.append(exp_price)

        # Residual demand after RES, CHP must-run, exports
        residual = demand[h] + h_export - total_res[h] - chp_must_run[h]

        # --- Dispatch imports (always available, no UC constraints) ---
        # Cap imports at 10 GW (realistic interconnector capacity for import direction;
        # rest is used for exports or congested)
        IMPORT_CAP = 8000  # MW (realistic: ~20 GW interconnector, minus export usage)
        import_dispatch = 0.0
        import_fuel_dispatch = 0.0
        for ic in import_clusters:
            if residual > 0 and import_dispatch < IMPORT_CAP:
                disp = min(ic.p_nom, residual, IMPORT_CAP - import_dispatch)
                import_dispatch += disp
                import_fuel_dispatch += disp
                residual -= disp
            else:
                break

        # --- UC commitment decisions for thermal clusters ---
        # (a) Must commit cheapest clusters to cover residual
        # (b) Try decommit expensive clusters if residual shrinks

        # Sort for commitment decisions (cheapest first for commit, expensive first for decommit)
        committed_cap = sum(c.p_nom for c in thermal_clusters if c.committed)
        committed_pmin_total = sum(c.p_min for c in thermal_clusters if c.committed)

        # Commit cheapest available clusters while residual > committed capacity × 0.95
        for c in thermal_clusters:
            if c.committed:
                continue
            if not c.can_commit():
                continue
            if residual > committed_cap * 0.95:
                c.committed = True
                c.hours_off = 0
                c.hours_on = 1
                c.output = c.p_min
                committed_cap += c.p_nom
                committed_pmin_total += c.p_min

        # Decommit expensive clusters if residual < sum(committed p_min) - margin
        margin = 500.0  # MW hysteresis
        for c in reversed(thermal_clusters):  # most expensive first
            if not c.committed:
                continue
            if not c.can_decommit():
                continue
            new_pmin = committed_pmin_total - c.p_min
            if residual < new_pmin - margin:
                c.committed = False
                c.hours_on = 0
                c.hours_off = 1
                committed_cap -= c.p_nom
                committed_pmin_total -= c.p_min

        # --- Economic dispatch among committed thermal clusters ---
        # Sort by effective MC (ascending). Each gets at least p_min, up to ramp-limited max.
        disp_order = sorted(
            [c for c in thermal_clusters if c.committed],
            key=lambda c: c.effective_mc()
        )

        thermal_dispatch = 0.0
        thermal_above_pmin = 0.0  # how much thermal is dispatched ABOVE p_min
        marginal_cluster = None   # the cluster truly at the margin (between p_min and p_max)

        remaining = max(0.0, residual)

        for c in disp_order:
            c_max = c.max_output()
            c_min = c.min_output()
            if remaining >= c_min:
                disp = min(c_max, remaining)
                if disp < c_min:
                    disp = c_min
                dispatched = disp
            else:
                # Must-run p_min even if we overshoot
                dispatched = c_min

            dispatched = min(dispatched, c_max)
            c.step_on(dispatched)
            thermal_dispatch += dispatched
            above_min = max(0, dispatched - c_min)
            thermal_above_pmin += above_min
            remaining -= dispatched

            # The truly marginal cluster: dispatched ABOVE p_min but below p_max
            if above_min > 0 and dispatched < c_max * 0.99:
                marginal_cluster = c
            elif above_min > 0:
                marginal_cluster = c  # at full output, still the last one above p_min

            if remaining <= 0 and above_min > 0:
                break

        # Step off committed-but-not-dispatched
        for c in thermal_clusters:
            if c.committed and c not in disp_order:
                c.step_off()
            elif not c.committed:
                if c.hours_off < 999:
                    c.hours_off += 1
                c.hours_on = 0
                c.output = 0.0

        # --- CHP flexible dispatch (above must-run, sorted by MC) ---
        chp_flex_dispatch = 0.0
        chp_flex_marginal_mc = 0.0
        chp_flex_remaining = max(0.0, remaining)
        for cf_p_nom, cf_mc in sorted(chp_flex_clusters, key=lambda x: x[1]):
            cf_avail = min(cf_p_nom, chp_flex_cap[h] * (cf_p_nom / max(1.0, sum(x[0] for x in chp_flex_clusters))))
            if chp_flex_remaining > 0 and cf_avail > 0:
                disp = min(cf_avail, chp_flex_remaining)
                chp_flex_dispatch += disp
                chp_flex_remaining -= disp
                chp_flex_marginal_mc = cf_mc

        remaining = chp_flex_remaining

        # --- Clearing price: what is the MARGINAL cost of the last MW? ---
        # Key insight: thermal at p_min is MUST-RUN, not marginal. The marginal
        # unit is whatever is actually being dispatched above its minimum.
        #
        # Priority for price-setting (highest to lowest):
        # 1. If thermal is dispatched ABOVE p_min → thermal marginal cluster sets price
        # 2. If CHP flex is dispatched → CHP flex MC sets price
        # 3. If imports served the marginal MW → import MC sets price
        # 4. If all thermal at p_min and surplus → export bid or RES bid sets price

        marginal_price = 0.0

        if marginal_cluster is not None and thermal_above_pmin > 100:
            # Thermal is truly marginal — dispatched above must-run level
            marginal_price = marginal_cluster.effective_mc(marginal_cluster.output)
        elif chp_flex_dispatch > 0:
            # CHP flexible is marginal
            marginal_price = chp_flex_marginal_mc
        elif import_fuel_dispatch > 0:
            # Imports are marginal — find the most expensive dispatched import
            # (imports dispatched cheapest-first, so last one dispatched sets price)
            dispatched_import_mcs = [ic.mc for ic in import_clusters if ic.p_nom > 0]
            # Estimate which import is marginal based on dispatch volume
            cum = 0.0
            marginal_price = 35.0  # default
            for ic in import_clusters:
                cum += ic.p_nom
                if cum >= import_dispatch:
                    marginal_price = ic.mc
                    break
        elif total_res[h] > demand[h]:
            # Pure RES surplus — price set by export bid or renewable bid
            if h_export > 0 and active_export_prices:
                marginal_price = max(active_export_prices)
            else:
                marginal_price = -3.0  # RES curtailment
        else:
            # Minimal dispatch, default to 0
            marginal_price = 0.0

        # Negative price when RES surplus: total supply > demand + exports
        total_supply = total_res[h] + chp_must_run[h] + import_dispatch + thermal_dispatch + chp_flex_dispatch
        net_balance = total_supply - (demand[h] + h_export)

        if net_balance > 500 and total_thermal_committed(thermal_clusters) == 0:
            # Pure RES surplus — marginal is the cheapest RES bid
            cheapest_res_bid = min(BID_PRICES.get(carriers[i], 0.0)
                                   for i in range(n_gens) if is_res[i] and p_max_pu[i, h] * p_nom[i] > 0)
            marginal_price = cheapest_res_bid

        # RES curtailment: if total supply well above demand, curtail proportionally
        curtailed = max(0.0, net_balance)
        total_curtailed += curtailed

        if marginal_price < 0:
            neg_hours += 1

        clearing_price[h] = marginal_price
        export_mw[h] = h_export

        # --- Accumulate dispatch_by_fuel ---
        # RES by fuel group
        for fuel, arr in res_by_fuel_group.items():
            curtail_frac = min(1.0, curtailed / max(1.0, total_res[h]))
            if fuel in dispatch_by_fuel:
                dispatch_by_fuel[fuel][h] += arr[h] * (1.0 - curtail_frac)

        # CHP must-run → gas
        dispatch_by_fuel["gas"][h] += chp_must_run[h]

        # CHP flexible → gas
        dispatch_by_fuel["gas"][h] += chp_flex_dispatch

        # Thermal clusters
        for c in thermal_clusters:
            if c.output > 0:
                fg = c.fuel_group
                if fg in dispatch_by_fuel:
                    dispatch_by_fuel[fg][h] += c.output

        # Imports
        dispatch_by_fuel["imports"][h] += import_fuel_dispatch

        if h % 1000 == 0:
            n_committed = sum(1 for c in thermal_clusters if c.committed)
            print(f"  Hour {h:4d}/{N_HOURS}: demand={demand[h]:.0f} MW, "
                  f"res={total_res[h]:.0f}, chp_mr={chp_must_run[h]:.0f}, "
                  f"thermal={thermal_dispatch:.0f} MW ({n_committed} clusters), "
                  f"export={h_export:.0f}, price={marginal_price:.1f} EUR/MWh")

    # ── Summary ───────────────────────────────────────────────────────────────
    total_dispatched = sum(d.sum() for k, d in dispatch_by_fuel.items() if k != "oil")
    total_export = export_mw.sum() / 1e6
    total_demand = demand.sum() / 1e6
    print(f"\n  Total dispatched: {total_dispatched/1e6:.1f} TWh "
          f"(domestic {(total_dispatched - export_mw.sum())/1e6:.1f} + export {total_export:.1f})")
    print(f"  Demand: {total_demand:.1f} TWh")
    print(f"  Energy balance error: "
          f"{abs(total_dispatched/1e6 - total_demand - total_export):.2f} TWh")
    print(f"  Export: avg={export_mw.mean():.0f} MW, max={export_mw.max():.0f} MW, "
          f"total={total_export:.1f} TWh")
    print(f"  Total RES curtailed: {total_curtailed/1e6:.2f} TWh")
    print(f"  Clearing price: min={clearing_price.min():.1f}, "
          f"max={clearing_price.max():.1f}, mean={clearing_price.mean():.1f} EUR/MWh")
    print(f"  Negative price hours: {neg_hours} ({neg_hours/N_HOURS*100:.1f}%)")

    # Dispatch breakdown by fuel
    print("\n  Annual dispatch by fuel:")
    for fuel in FUEL_ORDER:
        twh = dispatch_by_fuel[fuel].sum() / 1e6
        if twh > 0.1:
            print(f"    {fuel:20s}: {twh:7.1f} TWh")

    return clearing_price, dispatch_by_fuel, demand


def total_thermal_committed(clusters):
    """Helper: count committed thermal clusters."""
    return sum(1 for c in clusters if c.committed)


# ═══════════════════════════════════════════════════════════════════════════════
# MILP UNIT COMMITMENT
# ═══════════════════════════════════════════════════════════════════════════════

def _build_uc_clusters_milp(gens, is_chp_mr_set, n_quintiles=15):
    """Build UC clusters for the MILP formulation.

    Differences from _build_uc_clusters():
    - gas_chp generators in is_chp_mr_set are excluded from thermal clusters
      (they are handled separately as CHP flexible/must-run)
    - Returns a third list: chp_flex_clusters as (p_nom, mc) tuples

    Args:
        gens: DataFrame of generators (with diversified marginal costs)
        is_chp_mr_set: set of generator_ids that are CHP must-run
        n_quintiles: number of MC bins per carrier

    Returns:
        thermal_clusters, import_clusters, chp_flex_clusters
    """
    thermal_clusters = []
    import_clusters = []
    chp_flex_clusters = []

    carriers = gens["carrier"].values
    gen_ids = gens["generator_id"].values

    # --- Import generators ---
    imp_mask = gens["carrier"].str.startswith("import_")
    imp_gens = gens[imp_mask]
    if len(imp_gens) > 0:
        p_noms = imp_gens["p_nom"].values.astype(float)
        mcs = imp_gens["marginal_cost"].values.astype(float)
        n_bins = min(n_quintiles, max(1, len(imp_gens) // 2))
        mc_bins = np.array_split(np.argsort(mcs), n_bins)
        for k, idxs in enumerate(mc_bins):
            if len(idxs) == 0:
                continue
            total_p = p_noms[idxs].sum()
            avg_mc = float(np.average(mcs[idxs], weights=p_noms[idxs]))
            import_clusters.append(
                ImportCluster(f"import_bin_{k}", total_p, avg_mc, "imports")
            )
    import_clusters.sort(key=lambda c: c.mc)

    # --- CHP flex generators (gas_chp NOT in must-run set) ---
    chp_flex_mask = np.array(
        [carriers[i] == "gas_chp" and gen_ids[i] not in is_chp_mr_set
         for i in range(len(gens))]
    )
    chp_flex_idxs = np.where(chp_flex_mask)[0]
    if len(chp_flex_idxs) > 0:
        cf_pnom = gens["p_nom"].values.astype(float)[chp_flex_idxs]
        cf_mc = gens["marginal_cost"].values.astype(float)[chp_flex_idxs]
        n_bins = min(5, max(1, len(chp_flex_idxs) // 2))
        cf_bins = np.array_split(np.argsort(cf_mc), n_bins)
        for k, idxs in enumerate(cf_bins):
            if len(idxs) == 0:
                continue
            total_p = float(cf_pnom[idxs].sum())
            avg_mc = float(np.average(cf_mc[idxs], weights=cf_pnom[idxs]))
            chp_flex_clusters.append((total_p, avg_mc))

    # --- Thermal generators (UC_PARAMS carriers, excluding CHP must-run) ---
    uc_carriers = set(UC_PARAMS.keys())
    # Exclude ALL gas_chp from thermal clusters (avoid double-counting);
    # they are represented via chp_must_run and chp_flex_clusters
    uc_carriers_milp = uc_carriers - {"gas_chp"}

    therm_mask = gens["carrier"].isin(uc_carriers_milp)
    therm_gens = gens[therm_mask]

    for carrier in sorted(uc_carriers_milp):
        cg = therm_gens[therm_gens["carrier"] == carrier]
        if len(cg) == 0:
            continue
        p_noms = cg["p_nom"].values.astype(float)
        mcs = cg["marginal_cost"].values.astype(float)
        uc_p = UC_PARAMS[carrier]
        fuel_group = CARRIER_TO_SMARD.get(carrier, "other_conventional")

        n_bins = min(n_quintiles, max(1, len(cg) // 2))
        mc_bins = np.array_split(np.argsort(mcs), n_bins)
        for k, idxs in enumerate(mc_bins):
            if len(idxs) == 0:
                continue
            total_p = p_noms[idxs].sum()
            avg_mc = float(np.average(mcs[idxs], weights=p_noms[idxs]))
            min_mc = float(mcs[idxs].min())
            max_mc = float(mcs[idxs].max())
            name = f"{carrier}_bin_{k}"
            thermal_clusters.append(
                ThermalCluster(name, carrier, total_p, avg_mc, uc_p, fuel_group,
                               mc_min=min_mc, mc_max=max_mc)
            )

    thermal_clusters.sort(key=lambda c: c.mc)
    return thermal_clusters, import_clusters, chp_flex_clusters


def _solve_uc_window(thermal_clusters, border_imports, border_exports,
                     chp_flex_clusters,
                     ps, demand_window,
                     total_res_window, chp_mr_window, chp_flex_cap_window,
                     initial_state, neighbor_prices_window=None,
                     coal_precommit_names=None):
    """Solve one MILP UC window using PuLP + CBC.

    New in v2: per-border import/export variables with time-varying prices
    from actual neighbor hourly day-ahead prices + NTC constraints.

    Args:
        border_imports: dict border_code → {"ntc": MW, "fallback_mc": EUR/MWh}
        border_exports: dict border_code → {"ntc": MW}
        neighbor_prices_window: dict border_code → array of prices for this window

    Returns dict with:
        prices, thermal_dispatch, import_dispatch, chp_flex_dispatch,
        ps_gen, ps_charge, ps_soc, export, final_state
    """
    import pulp

    if coal_precommit_names is None:
        coal_precommit_names = set()

    T = len(demand_window)

    # ── Build MILP problem ────────────────────────────────────────────────────
    prob = pulp.LpProblem("UC_window", pulp.LpMinimize)

    # ── Decision variables ────────────────────────────────────────────────────

    # Thermal cluster variables
    u = {}   # commitment (binary, except lignite → forced to 1)
    p = {}   # output (continuous)
    v = {}   # startup indicator (binary)
    w = {}   # shutdown indicator (binary)

    # Determine which coal clusters are pre-committed (CHP heating duty)
    COAL_PRECOMMIT_MW = 3000
    coal_precommit_names_local = set()
    coal_acc = 0
    for c in sorted(thermal_clusters, key=lambda x: x.mc):
        if c.carrier == "coal" and coal_acc < COAL_PRECOMMIT_MW:
            coal_precommit_names_local.add(c.name)
            coal_acc += c.p_nom
    if coal_precommit_names:
        coal_precommit_names_local = coal_precommit_names

    for c in thermal_clusters:
        force_on = c.carrier == "lignite" or c.name in coal_precommit_names_local
        for t in range(T):
            if force_on:
                u[c.name, t] = pulp.LpVariable(f"u_{c.name}_{t}", lowBound=1, upBound=1,
                                               cat="Continuous")
                v[c.name, t] = pulp.LpVariable(f"v_{c.name}_{t}", lowBound=0, upBound=0,
                                               cat="Continuous")
                w[c.name, t] = pulp.LpVariable(f"w_{c.name}_{t}", lowBound=0, upBound=0,
                                               cat="Continuous")
            else:
                u[c.name, t] = pulp.LpVariable(f"u_{c.name}_{t}", cat="Binary")
                v[c.name, t] = pulp.LpVariable(f"v_{c.name}_{t}", cat="Binary")
                w[c.name, t] = pulp.LpVariable(f"w_{c.name}_{t}", cat="Binary")
            p[c.name, t] = pulp.LpVariable(f"p_{c.name}_{t}", lowBound=0, upBound=c.p_nom)

    # Per-border import variables (time-varying MC via neighbor prices)
    p_imp = {}
    borders = sorted(border_imports.keys())
    for b in borders:
        ntc = border_imports[b]["ntc"]
        for t in range(T):
            p_imp[b, t] = pulp.LpVariable(f"pimp_{b}_{t}", lowBound=0, upBound=ntc)

    # Per-border export variables (time-varying revenue via neighbor prices)
    p_exp = {}
    for b in sorted(border_exports.keys()):
        ntc = border_exports[b]["ntc"]
        for t in range(T):
            p_exp[b, t] = pulp.LpVariable(f"pexp_{b}_{t}", lowBound=0, upBound=ntc)

    # CHP flexible: single continuous variable per hour
    p_chpf = [pulp.LpVariable(f"pchpf_{t}", lowBound=0) for t in range(T)]

    # Pumped storage
    ps_gen_v = [pulp.LpVariable(f"psgen_{t}", lowBound=0, upBound=ps.p_gen) for t in range(T)]
    ps_chg_v = [pulp.LpVariable(f"pschg_{t}", lowBound=0, upBound=ps.p_charge) for t in range(T)]
    soc_v = [pulp.LpVariable(f"soc_{t}", lowBound=0, upBound=ps.e_max) for t in range(T)]

    # Curtailment variable (free disposal of excess RES)
    curtail_v = [pulp.LpVariable(f"curtail_{t}", lowBound=0) for t in range(T)]

    # ── Objective ─────────────────────────────────────────────────────────────
    if chp_flex_clusters:
        total_chp_cap = sum(cap for cap, mc in chp_flex_clusters)
        chp_flex_mc_avg = (sum(cap * mc for cap, mc in chp_flex_clusters) / total_chp_cap
                           if total_chp_cap > 0 else 100.0)
    else:
        chp_flex_mc_avg = 100.0

    PS_GEN_MC = 0.0

    obj_terms = []
    for c in thermal_clusters:
        for t in range(T):
            obj_terms.append(c.mc * p[c.name, t])
            obj_terms.append(c.noload * u[c.name, t])
            obj_terms.append(c.startup_cost * v[c.name, t])

    # Import costs: time-varying neighbor prices
    for b in borders:
        fallback_mc = border_imports[b]["fallback_mc"]
        for t in range(T):
            if neighbor_prices_window is not None and b in neighbor_prices_window:
                mc_t = float(neighbor_prices_window[b][t])
            else:
                mc_t = fallback_mc
            obj_terms.append(mc_t * p_imp[b, t])

    for t in range(T):
        obj_terms.append(chp_flex_mc_avg * p_chpf[t])

    # Export revenue: time-varying neighbor prices
    for b in sorted(border_exports.keys()):
        for t in range(T):
            if neighbor_prices_window is not None and b in neighbor_prices_window:
                exp_price_t = float(neighbor_prices_window[b][t])
            else:
                exp_price_t = 80.0  # fallback
            obj_terms.append(-exp_price_t * p_exp[b, t])

    for t in range(T):
        obj_terms.append(PS_GEN_MC * ps_gen_v[t])

    prob += pulp.lpSum(obj_terms)

    # ── Power balance constraints ──────────────────────────────────────────────
    balance = {}
    for t in range(T):
        supply = (
            pulp.lpSum(p[c.name, t] for c in thermal_clusters)
            + pulp.lpSum(p_imp[b, t] for b in borders)
            + float(chp_mr_window[t])
            + p_chpf[t]
            + float(total_res_window[t])
            + ps_gen_v[t]
            - curtail_v[t]
        )
        demand_t = float(demand_window[t])
        export_t = pulp.lpSum(p_exp[b, t] for b in sorted(border_exports.keys()))
        balance[t] = (supply == demand_t + export_t + ps_chg_v[t])
        prob += balance[t], f"balance_{t}"

    # ── Aggregate export cap: real peak ~15 GW (CBPF p99). Cap at 12 GW —
    # halfway between the original 10 GW (which clipped peaks → -14% annual)
    # and 14 GW (which pulled too much thermal up via export profitability).
    AGG_EXPORT_CAP = 12000  # MW — real avg 7.3 GW, p75 ~10 GW, peak ~15 GW
    for t in range(T):
        prob += (pulp.lpSum(p_exp[b, t] for b in sorted(border_exports.keys()))
                 <= AGG_EXPORT_CAP), f"agg_export_cap_{t}"

    # ── Generation bounds ──────────────────────────────────────────────────────
    for c in thermal_clusters:
        for t in range(T):
            prob += p[c.name, t] >= c.p_min * u[c.name, t], f"pmin_{c.name}_{t}"
            prob += p[c.name, t] <= c.p_nom * u[c.name, t], f"pmax_{c.name}_{t}"

    # ── CHP flex bounds ────────────────────────────────────────────────────────
    for t in range(T):
        prob += p_chpf[t] <= float(chp_flex_cap_window[t]), f"chpf_cap_{t}"

    # ── Startup / shutdown logic ───────────────────────────────────────────────
    for c in thermal_clusters:
        if c.carrier == "lignite":
            continue  # lignite always committed, no startup/shutdown logic needed
        # Initial conditions from initial_state
        init = initial_state.get(c.name, {})
        u_prev_0 = 1.0 if init.get("committed", False) else 0.0

        for t in range(T):
            u_prev = u[c.name, t - 1] if t > 0 else u_prev_0
            # v[t] >= u[t] - u[t-1]  (startup indicator)
            prob += v[c.name, t] >= u[c.name, t] - u_prev, f"startup_{c.name}_{t}"
            # w[t] >= u[t-1] - u[t]  (shutdown indicator)
            prob += w[c.name, t] >= u_prev - u[c.name, t], f"shutdown_{c.name}_{t}"

    # ── Min-up time ────────────────────────────────────────────────────────────
    for c in thermal_clusters:
        if c.carrier == "lignite" or c.name in coal_precommit_names_local or c.min_up <= 1:
            continue
        min_up = c.min_up
        init = initial_state.get(c.name, {})
        u_hist = [1.0 if init.get("committed", False) else 0.0]  # [t=-1]

        for t in range(T):
            # Sum of startups in window [t-min_up+1 .. t] <= u[t]
            # i.e., if started within the min_up window, must stay on
            start_window = range(max(0, t - min_up + 1), t + 1)
            if len(start_window) == 0:
                continue
            prob += (pulp.lpSum(v[c.name, k] for k in start_window)
                     <= u[c.name, t]), f"minup_{c.name}_{t}"

    # ── Min-down time ──────────────────────────────────────────────────────────
    for c in thermal_clusters:
        if c.carrier == "lignite" or c.name in coal_precommit_names_local or c.min_down <= 1:
            continue
        min_down = c.min_down
        init = initial_state.get(c.name, {})
        u_prev_0 = 1.0 if init.get("committed", False) else 0.0

        for t in range(min_down, T):
            # Sum of shutdowns in window [t-min_down+1 .. t] <= 1 - u[t]
            shut_window = range(t - min_down + 1, t + 1)
            # shutdown at t_k means u[t_k-1]=1, u[t_k]=0 → w[t_k]=1
            prob += (pulp.lpSum(w[c.name, k] for k in shut_window)
                     <= 1 - u[c.name, t]), f"mindown_{c.name}_{t}"

    # ── Ramp constraints ───────────────────────────────────────────────────────
    for c in thermal_clusters:
        if c.ramp <= 0:
            continue
        init = initial_state.get(c.name, {})
        p_prev_0 = float(init.get("prev_output", c.p_min if init.get("committed") else 0.0))

        for t in range(T):
            p_prev = p[c.name, t - 1] if t > 0 else p_prev_0
            # Ramp up: p[t] - p[t-1] <= ramp + p_nom * v[t]  (startup relaxation)
            prob += (p[c.name, t] - p_prev
                     <= c.ramp + c.p_nom * v[c.name, t]), f"rampup_{c.name}_{t}"
            # Ramp down: p[t-1] - p[t] <= ramp + p_nom * w[t]
            prob += (p_prev - p[c.name, t]
                     <= c.ramp + c.p_nom * w[c.name, t]), f"rampdn_{c.name}_{t}"

    # ── Pumped storage SOC ─────────────────────────────────────────────────────
    soc_prev_0 = float(initial_state.get("ps_soc", ps.e_max * 0.5))
    for t in range(T):
        soc_prev = soc_v[t - 1] if t > 0 else soc_prev_0
        # soc[t] = soc[t-1] - gen[t]/eta + chg[t]*eta
        prob += (soc_v[t] == soc_prev
                 - ps_gen_v[t] * (1.0 / ps.eta)
                 + ps_chg_v[t] * ps.eta), f"soc_{t}"

    # ── Solve MILP ─────────────────────────────────────────────────────────────
    milp_solver = pulp.COIN_CMD(msg=0, timeLimit=30, gapRel=0.005)
    prob.solve(milp_solver)

    milp_status = pulp.LpStatus[prob.status]
    milp_feasible = prob.status in (1, -1)  # Optimal or time-limited feasible

    # ── Re-solve as LP for dual prices ────────────────────────────────────────
    # Fix all binary variables to their MILP solution values and re-solve
    prices = np.zeros(T)

    if milp_feasible:
        # Fix u, v, w to MILP solution values
        for c in thermal_clusters:
            for t in range(T):
                u_val = round(pulp.value(u[c.name, t]) or 0)
                v_val = round(pulp.value(v[c.name, t]) or 0)
                w_val = round(pulp.value(w[c.name, t]) or 0)
                u[c.name, t].cat = "Continuous"
                u[c.name, t].lowBound = u_val
                u[c.name, t].upBound = u_val
                v[c.name, t].cat = "Continuous"
                v[c.name, t].lowBound = v_val
                v[c.name, t].upBound = v_val
                w[c.name, t].cat = "Continuous"
                w[c.name, t].lowBound = w_val
                w[c.name, t].upBound = w_val

        lp_solver = pulp.COIN_CMD(msg=0)
        prob.solve(lp_solver)

        # Extract dual prices from power balance constraints
        for t in range(T):
            pi = balance[t].pi
            if pi is not None:
                prices[t] = float(pi)
            else:
                # Fallback: MC of most expensive dispatched thermal cluster
                max_mc = 0.0
                for c in thermal_clusters:
                    pval = pulp.value(p[c.name, t]) or 0.0
                    if pval > c.p_min * 0.1:
                        max_mc = max(max_mc, c.mc)
                prices[t] = max_mc
    else:
        # Infeasible fallback: zero prices, zero dispatch
        pass

    # ── Extract dispatch results ───────────────────────────────────────────────
    thermal_dispatch = {}
    for c in thermal_clusters:
        arr = np.zeros(T)
        for t in range(T):
            arr[t] = pulp.value(p[c.name, t]) or 0.0
        thermal_dispatch[c.name] = arr

    import_dispatch = np.zeros(T)
    for b in borders:
        for t in range(T):
            import_dispatch[t] += pulp.value(p_imp[b, t]) or 0.0

    chp_flex_dispatch = np.array([pulp.value(p_chpf[t]) or 0.0 for t in range(T)])
    ps_gen_out = np.array([pulp.value(ps_gen_v[t]) or 0.0 for t in range(T)])
    ps_chg_out = np.array([pulp.value(ps_chg_v[t]) or 0.0 for t in range(T)])
    ps_soc_out = np.array([pulp.value(soc_v[t]) or 0.0 for t in range(T)])
    export_out = np.zeros(T)
    for b in sorted(border_exports.keys()):
        for t in range(T):
            export_out[t] += pulp.value(p_exp[b, t]) or 0.0

    # ── Build final state for next window ─────────────────────────────────────
    final_state = {}
    for c in thermal_clusters:
        last_u = round(pulp.value(u[c.name, T - 1]) or 0)
        last_p = float(thermal_dispatch[c.name][T - 1])
        committed_end = bool(last_u)
        if committed_end:
            hours_on_end = 1
            for t in range(T - 2, -1, -1):
                if round(pulp.value(u[c.name, t]) or 0) == 1:
                    hours_on_end += 1
                else:
                    break
            hours_off_end = 0
        else:
            hours_off_end = 1
            for t in range(T - 2, -1, -1):
                if round(pulp.value(u[c.name, t]) or 0) == 0:
                    hours_off_end += 1
                else:
                    break
            hours_on_end = 0
        final_state[c.name] = {
            "committed": committed_end,
            "hours_on": hours_on_end,
            "hours_off": hours_off_end,
            "prev_output": last_p,
        }

    final_state["ps_soc"] = float(ps_soc_out[T - 1])

    return {
        "prices": prices,
        "thermal_dispatch": thermal_dispatch,
        "import_dispatch": import_dispatch,
        "chp_flex_dispatch": chp_flex_dispatch,
        "ps_gen": ps_gen_out,
        "ps_charge": ps_chg_out,
        "ps_soc": ps_soc_out,
        "export": export_out,
        "final_state": final_state,
        "status": milp_status,
    }


def _recalc_monthly_mc(gens, month):
    """Recalculate fossil generator MCs for a specific month using seasonal fuel prices.

    Uses pre-stored efficiency (_eta) and emission factors from diversify_fossil_bids().
    """
    has_eta = "_eta" in gens.columns and gens["_eta"].notna().any()
    if not has_eta:
        return

    for carrier in FUEL_PARAMS:
        mask = gens["carrier"] == carrier
        if not mask.any():
            continue
        idx = gens.index[mask]
        eta = gens.loc[idx, "_eta"].values.astype(float)
        ef = gens.loc[idx, "_ef"].values.astype(float)
        hc = gens.loc[idx, "_heat_credit"].values.astype(float)
        fc = gens.loc[idx, "_fuel_carrier"].values

        fuel_prices = np.array([
            TTF_MONTHLY[month] if c == "gas" else
            COAL_MONTHLY[month] if c == "coal" else
            FUEL_PARAMS[carrier][0]  # lignite, oil, other: no monthly variation
            for c in fc
        ])
        co2 = CO2_MONTHLY[month]
        mc = fuel_prices / eta + ef * co2 / eta - hc
        gens.loc[idx, "marginal_cost"] = mc


def run_milp_uc(gens, gen_ts, demand):
    """Run MILP-based unit commitment dispatch for all 8760 hours.

    v2 improvements:
    - Per-border import/export with hourly neighbor prices (EUPHEMIA-like coupling)
    - Per-border NTC constraints from observed physical flows
    - Monthly seasonal fuel prices (TTF, coal, CO2)
    - COD-based efficiency ranking
    - Dynamic scarcity markup (replaces flat 35%)
    """
    print(f"\nRunning MILP unit commitment dispatch v2 for {N_HOURS} hours...")

    # Load COD data for efficiency ranking
    cod_data = load_cod_data()
    if cod_data:
        print(f"  Loaded COD data: {sum(len(v) for v in cod_data.values())} MaStR units")

    # Apply diversified fossil marginal costs with COD-based ranking
    # Use January as initial month (will be recalculated per-window)
    diversify_fossil_bids(gens, month=1, cod_data=cod_data)

    # Load hourly neighbor prices
    neighbor_prices = load_neighbor_prices()

    # ── Build per-border import/export structures ─────────────────────────────
    # Fallback MCs for when neighbor prices unavailable
    FALLBACK_MC = {
        "AT": 105, "BE": 82, "CZ": 100, "DK": 83, "FR": 61,
        "LU": 91, "NL": 87, "NO": 71, "PL": 109, "SE": 63, "CH": 102,
    }
    border_imports = {}
    border_exports = {}
    for border, ntc in NTC_IMPORT.items():
        border_imports[border] = {"ntc": ntc, "fallback_mc": FALLBACK_MC.get(border, 90)}
    for border, ntc in NTC_EXPORT.items():
        border_exports[border] = {"ntc": ntc}

    total_imp_ntc = sum(v["ntc"] for v in border_imports.values())
    total_exp_ntc = sum(v["ntc"] for v in border_exports.values())
    print(f"  Cross-border: {len(border_imports)} borders, "
          f"import NTC={total_imp_ntc/1e3:.1f} GW, export NTC={total_exp_ntc/1e3:.1f} GW")
    if neighbor_prices:
        print(f"  Using hourly neighbor prices for market coupling")
    else:
        print(f"  WARNING: Using static fallback prices (no market coupling)")

    # ── Build p_max_pu / p_min_pu matrices ────────────────────────────────────
    ts_map = gen_ts.set_index("generator_id")
    n_gens = len(gens)
    p_max_pu = np.ones((n_gens, N_HOURS), dtype=np.float32)
    p_min_pu = np.zeros((n_gens, N_HOURS), dtype=np.float32)

    gen_ids = gens["generator_id"].values
    id_to_idx = {gid: i for i, gid in enumerate(gen_ids)}

    for _, row in ts_map.iterrows():
        gid = row.name
        if gid not in id_to_idx:
            continue
        idx = id_to_idx[gid]
        if row["p_max_pu"] is not None and len(row["p_max_pu"]) >= N_HOURS:
            p_max_pu[idx, :] = np.array(row["p_max_pu"][:N_HOURS], dtype=np.float32)
        if row["p_min_pu"] is not None and len(row["p_min_pu"]) >= N_HOURS:
            p_min_pu[idx, :] = np.array(row["p_min_pu"][:N_HOURS], dtype=np.float32)

    carriers = gens["carrier"].values
    p_nom = gens["p_nom"].values.astype(np.float64)

    for i in range(n_gens):
        if carriers[i] in RES_AVAILABILITY_UC:
            p_max_pu[i, :] *= RES_AVAILABILITY_UC[carriers[i]]

    # Biomass CF: tuned to match Fraunhofer ISE 2025 (50 TWh incl distributed BTM).
    # waste CF dropped to 0.15: real EC "other_conventional" ≈ 1.94 TWh; most
    # German waste-to-energy is heat-driven (must-run for district heating)
    # not dispatched on wholesale, so its electrical output is small.
    BIOMASS_CF = {"biogas": 0.63, "biomass": 0.63, "waste": 0.15}
    # Hydro CF: profiles embed ~60% resource factor; apply remaining availability
    HYDRO_CF = {"run_of_river": 0.62, "reservoir": 0.52}
    for i in range(n_gens):
        if carriers[i] in BIOMASS_CF:
            p_max_pu[i, :] *= BIOMASS_CF[carriers[i]]
        elif carriers[i] in HYDRO_CF:
            p_max_pu[i, :] *= HYDRO_CF[carriers[i]]

    # ── Identify generator categories ─────────────────────────────────────────
    is_chp = np.array([c == "gas_chp" for c in carriers])
    has_must_run = np.array([p_min_pu[i].max() > 0.01 for i in range(n_gens)])
    is_chp_mr = is_chp & has_must_run
    is_chp_mr_set = set(gen_ids[is_chp_mr])
    is_res = np.array([c in RES_ALL_CARRIERS for c in carriers])

    # ── Build hourly month array (needed for seasonal availability) ────────────
    hour_to_month = np.array([
        pd.Timestamp(f"{YEAR}-01-01", tz="UTC") + pd.Timedelta(hours=h)
        for h in range(N_HOURS)
    ])
    hour_months = np.array([ts.month for ts in hour_to_month])

    # ── Pre-compute RES, CHP must-run, CHP flex ────────────────────────────────
    res_by_fuel_group = {}
    for i in range(n_gens):
        if not is_res[i]:
            continue
        fuel = CARRIER_TO_SMARD.get(carriers[i], "other_conventional")
        output = p_max_pu[i, :].astype(np.float64) * p_nom[i]
        if fuel in res_by_fuel_group:
            res_by_fuel_group[fuel] += output
        else:
            res_by_fuel_group[fuel] = output.copy()

    total_res = np.zeros(N_HOURS, dtype=np.float64)
    for arr in res_by_fuel_group.values():
        total_res += arr

    CHP_MUST_RUN_SCALE = 0.28
    # CHP availability: seasonal profile matching gas_chp thermal clusters
    CHP_SEASONAL = {
        1: 0.88, 2: 0.86, 3: 0.82, 4: 0.75, 5: 0.72, 6: 0.70,
        7: 0.70, 8: 0.72, 9: 0.78, 10: 0.83, 11: 0.86, 12: 0.88,
    }
    chp_hourly_avail = np.array([CHP_SEASONAL[int(hour_months[h])] for h in range(N_HOURS)])
    chp_must_run = np.zeros(N_HOURS, dtype=np.float64)
    chp_flex_cap = np.zeros(N_HOURS, dtype=np.float64)
    for i in range(n_gens):
        if not is_chp_mr[i]:
            continue
        avail_pnom_arr = p_nom[i] * chp_hourly_avail
        must = p_min_pu[i, :].astype(np.float64) * avail_pnom_arr * CHP_MUST_RUN_SCALE
        flex_base = np.maximum(0.0,
            (p_max_pu[i, :].astype(np.float64) - p_min_pu[i, :].astype(np.float64)) * avail_pnom_arr)
        freed = p_min_pu[i, :].astype(np.float64) * avail_pnom_arr * (1 - CHP_MUST_RUN_SCALE)
        chp_must_run += must
        chp_flex_cap += flex_base + freed

    # ── Build thermal clusters (no import clusters needed — using per-border) ─
    thermal_clusters, _import_clusters_unused, chp_flex_clusters = _build_uc_clusters_milp(
        gens, is_chp_mr_set
    )

    print(f"  UC clusters: {len(thermal_clusters)} thermal + "
          f"{len(chp_flex_clusters)} CHP flex bins")
    if thermal_clusters:
        print(f"  Thermal MC range: {thermal_clusters[0].mc:.0f} to "
              f"{thermal_clusters[-1].mc:.0f} EUR/MWh")
    print(f"  Total thermal: {sum(c.p_nom for c in thermal_clusters)/1e3:.1f} GW")
    print(f"  CHP must-run peak: {chp_must_run.max():.0f} MW, "
          f"flex peak: {chp_flex_cap.max():.0f} MW")
    print(f"  Total RES peak: {total_res.max():.0f} MW")

    # ── Thermal availability: seasonal outage profiles ────────────────────────
    # Planned maintenance concentrates in spring/summer when demand is low.
    # Forced outage rates are constant. Total = 1 - planned_outage - forced_outage
    # Sources: ENTSO-E ERAA, VGB PowerTech, BNetzA Kraftwerksliste
    #
    # Month:           Jan   Feb   Mar   Apr   May   Jun   Jul   Aug   Sep   Oct   Nov   Dec
    SEASONAL_AVAIL = {
        "lignite": {    # FOR ~8%, planned: winter 5%, spring/summer 20-30%.
                        # ×0.92 vs base to reflect mid-2020s decommissioning/standby
                        # plus security-reserve lignite (BNetzA: ~1 GW in reserve).
            1: 0.80, 2: 0.78, 3: 0.72, 4: 0.64, 5: 0.60, 6: 0.57,
            7: 0.57, 8: 0.60, 9: 0.66, 10: 0.74, 11: 0.78, 12: 0.80,
        },
        "coal": {       # FOR ~8%, planned: winter 5%, spring/summer 25-35%.
                        # Multiplied ×0.62 vs base. Excludes Sicherheitsbereitschaft
                        # + Netzreserve coal (~2 GW) AND captures 2025 reality
                        # that several remaining hard-coal units ran at very low
                        # utilization (close to phase-out date).
            1: 0.54, 2: 0.53, 3: 0.47, 4: 0.40, 5: 0.36, 6: 0.34,
            7: 0.34, 8: 0.36, 9: 0.42, 10: 0.48, 11: 0.53, 12: 0.54,
        },
        "gas_ccgt": {   # FOR ~5%, planned: spring/summer 10-15%
            1: 0.90, 2: 0.88, 3: 0.85, 4: 0.80, 5: 0.78, 6: 0.78,
            7: 0.78, 8: 0.80, 9: 0.83, 10: 0.87, 11: 0.90, 12: 0.90,
        },
        "gas_chp": {    # FOR ~5%, planned: summer 15-20% (heat system maint)
            1: 0.88, 2: 0.86, 3: 0.82, 4: 0.75, 5: 0.72, 6: 0.70,
            7: 0.70, 8: 0.72, 9: 0.78, 10: 0.83, 11: 0.86, 12: 0.88,
        },
        "oil": {        # Peakers: high availability, minimal planned outage
            1: 0.92, 2: 0.92, 3: 0.90, 4: 0.88, 5: 0.88, 6: 0.88,
            7: 0.88, 8: 0.88, 9: 0.90, 10: 0.92, 11: 0.92, 12: 0.92,
        },
        "other": {      # ×0.30 vs base: most "other" units in MaStR are waste-to-
                        # energy or backup peakers that don't bid into wholesale
                        # day-ahead. Real EC "other_conventional" = 1.94 TWh/yr.
            1: 0.26, 2: 0.26, 3: 0.25, 4: 0.23, 5: 0.23, 6: 0.23,
            7: 0.23, 8: 0.23, 9: 0.25, 10: 0.26, 11: 0.26, 12: 0.26,
        },
    }
    cur_avail_month = [0]  # mutable to allow closure update

    def _apply_thermal_avail(clusters, month=None):
        m = month if month is not None else cur_avail_month[0]
        if m == 0:
            m = 1  # default to January
        for c in clusters:
            seasonal = SEASONAL_AVAIL.get(c.carrier)
            if seasonal:
                avail = seasonal[m]
                c.p_nom *= avail
                c.p_min *= avail
                c.ramp *= avail

    cur_avail_month[0] = int(hour_months[0])
    _apply_thermal_avail(thermal_clusters)
    for carrier in ("lignite", "coal", "gas_ccgt", "gas_chp"):
        cap = sum(c.p_nom for c in thermal_clusters if c.carrier == carrier)
        seasonal = SEASONAL_AVAIL.get(carrier, {})
        if cap > 0:
            avg_avail = sum(seasonal.values()) / 12 if seasonal else 0
            print(f"  {carrier}: {cap/1e3:.1f} GW (month {cur_avail_month[0]}: "
                  f"{seasonal.get(cur_avail_month[0], 0):.0%}, annual avg: {avg_avail:.0%})")

    # ── Coal: no pre-commitment, let MILP optimize ─────────────────────────
    COAL_PRECOMMIT_MW = 0
    coal_precommit_names = set()
    print(f"  Coal: no pre-commitment (MILP-optimized)")

    ps = PumpedStorageUnit()
    print(f"  Pumped storage: {ps.p_gen/1e3:.1f} GW / {ps.e_max/1e3:.0f} GWh")

    # ── Output arrays ──────────────────────────────────────────────────────────
    clearing_price = np.zeros(N_HOURS, dtype=np.float64)
    dispatch_by_fuel = {fuel: np.zeros(N_HOURS, dtype=np.float64) for fuel in FUEL_ORDER}
    dispatch_by_fuel["oil"] = np.zeros(N_HOURS, dtype=np.float64)
    export_mw = np.zeros(N_HOURS, dtype=np.float64)

    # ── Initial state ──────────────────────────────────────────────────────────
    state = {}
    for c in thermal_clusters:
        if c.carrier == "lignite":
            state[c.name] = {"committed": True, "hours_on": 999, "hours_off": 0,
                             "prev_output": c.p_min}
        else:
            state[c.name] = {"committed": False, "hours_on": 0, "hours_off": 999,
                             "prev_output": 0.0}
    state["ps_soc"] = ps.e_max * 0.5

    # ── Rolling horizon ────────────────────────────────────────────────────────
    WINDOW = 48
    STRIDE = 24

    n_windows = 0
    neg_hours = 0
    prev_month = 0

    for ws in range(0, N_HOURS, STRIDE):
        we = min(ws + WINDOW, N_HOURS)
        T_w = we - ws

        # Recalculate MCs if month changed (seasonal fuel prices)
        cur_month = int(hour_months[ws])
        if cur_month != prev_month:
            _recalc_monthly_mc(gens, cur_month)
            # Rebuild thermal clusters with new MCs
            thermal_clusters, _, chp_flex_clusters = _build_uc_clusters_milp(
                gens, is_chp_mr_set
            )
            thermal_clusters.sort(key=lambda c: c.mc)
            cur_avail_month[0] = cur_month
            _apply_thermal_avail(thermal_clusters, month=cur_month)
            coal_precommit_names = set()  # no coal pre-commitment
            prev_month = cur_month

        # Slice neighbor prices for this window
        neighbor_prices_window = None
        if neighbor_prices is not None:
            neighbor_prices_window = {}
            for border in list(border_imports.keys()) + list(border_exports.keys()):
                if border in neighbor_prices:
                    arr = neighbor_prices[border]
                    # Pad if needed
                    window_prices = np.full(T_w, FALLBACK_MC.get(border, 90.0))
                    end_idx = min(we, len(arr))
                    if ws < end_idx:
                        window_prices[:end_idx - ws] = arr[ws:end_idx]
                    neighbor_prices_window[border] = window_prices

        result = _solve_uc_window(
            thermal_clusters, border_imports, border_exports,
            chp_flex_clusters,
            ps,
            demand[ws:we],
            total_res[ws:we],
            chp_must_run[ws:we],
            chp_flex_cap[ws:we],
            state,
            neighbor_prices_window=neighbor_prices_window,
            coal_precommit_names=coal_precommit_names,
        )

        # Record first STRIDE hours (or fewer at end)
        record = min(STRIDE, T_w)
        h0, h1 = ws, ws + record

        clearing_price[h0:h1] = result["prices"][:record]
        neg_hours += int((result["prices"][:record] < 0).sum())

        for c in thermal_clusters:
            fg = c.fuel_group
            if fg in dispatch_by_fuel:
                dispatch_by_fuel[fg][h0:h1] += result["thermal_dispatch"][c.name][:record]

        dispatch_by_fuel["imports"][h0:h1] += result["import_dispatch"][:record]
        dispatch_by_fuel["gas"][h0:h1] += result["chp_flex_dispatch"][:record]
        dispatch_by_fuel["pumped_storage"][h0:h1] += result["ps_gen"][:record]
        dispatch_by_fuel["gas"][h0:h1] += chp_must_run[h0:h1]

        for fuel, arr in res_by_fuel_group.items():
            if fuel in dispatch_by_fuel:
                dispatch_by_fuel[fuel][h0:h1] += arr[h0:h1]

        export_mw[h0:h1] += result["export"][:record]

        state = result["final_state"]
        ps.soc = state["ps_soc"]

        n_windows += 1
        if n_windows % 30 == 0 or n_windows <= 3:
            mean_p = result["prices"][:record].mean()
            print(f"  Window {n_windows:4d}: hours {ws:4d}-{ws+record:4d}, "
                  f"avg price {mean_p:6.1f} EUR/MWh, status={result['status']}")

    # ── Summary ────────────────────────────────────────────────────────────────
    total_dispatched = sum(d.sum() for k, d in dispatch_by_fuel.items() if k != "oil")
    total_export = export_mw.sum() / 1e6
    total_demand = demand.sum() / 1e6

    # ── Dynamic scarcity markup ──────────────────────────────────────────────
    # Instead of flat 35%, use supply-demand tightness to scale markup.
    # When reserve margin is thin → higher markup (scarcity rent + strategic bidding)
    # When surplus → lower/no markup
    # Calibrate so annual average ≈ SMARD average (~90 EUR/MWh)
    residual_load = demand - total_res - chp_must_run
    rl_pct = np.percentile(residual_load[residual_load > 0], [25, 50, 75, 90, 95])
    print(f"\n  Residual load percentiles: p25={rl_pct[0]/1e3:.1f}, "
          f"p50={rl_pct[1]/1e3:.1f}, p75={rl_pct[2]/1e3:.1f}, "
          f"p90={rl_pct[3]/1e3:.1f}, p95={rl_pct[4]/1e3:.1f} GW")

    for h in range(N_HOURS):
        if clearing_price[h] > 0:
            rl = residual_load[h]
            if rl > rl_pct[4]:  # p95: very tight supply
                markup = 1.50  # 50% scarcity premium
            elif rl > rl_pct[3]:  # p90: tight
                markup = 1.35
            elif rl > rl_pct[2]:  # p75: moderate
                markup = 1.25
            elif rl > rl_pct[1]:  # p50: average
                markup = 1.18
            elif rl > 0:  # low residual load
                markup = 1.10
            else:  # negative residual (RES surplus)
                markup = 1.05
            clearing_price[h] *= markup
        elif clearing_price[h] >= -0.01 and clearing_price[h] <= 0.01:
            # Zero/near-zero prices: check for RES surplus → negative prices
            surplus = total_res[h] + chp_must_run[h] - demand[h]
            if surplus > 25000:
                clearing_price[h] = max(-100, -(surplus - 20000) * 0.006)
            elif surplus > 20000:
                clearing_price[h] = max(-15, -(surplus - 20000) * 0.003)

    neg_hours = int((clearing_price < 0).sum())

    print(f"\n  Total dispatched: {total_dispatched/1e6:.1f} TWh")
    print(f"  Demand: {total_demand:.1f} TWh")
    print(f"  Export: avg={export_mw.mean():.0f} MW, total={total_export:.1f} TWh")
    print(f"  Pumped storage gen: {dispatch_by_fuel['pumped_storage'].sum()/1e6:.2f} TWh")
    print(f"  Dynamic scarcity markup (replaces flat 35%)")
    print(f"  Clearing price: min={clearing_price.min():.1f}, "
          f"max={clearing_price.max():.1f}, mean={clearing_price.mean():.1f} EUR/MWh")
    print(f"  Negative price hours: {neg_hours} ({neg_hours/N_HOURS*100:.1f}%)")

    print("\n  Annual dispatch by fuel:")
    for fuel in FUEL_ORDER:
        twh = dispatch_by_fuel[fuel].sum() / 1e6
        if twh > 0.1:
            print(f"    {fuel:20s}: {twh:7.1f} TWh")

    return clearing_price, dispatch_by_fuel, demand


# ═══════════════════════════════════════════════════════════════════════════════
# HTML REPORT GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def generate_html(clearing_price, dispatch_by_fuel, demand, smard):
    """Generate interactive HTML comparison report."""
    print(f"\nGenerating HTML report...")

    # Build hourly timestamps for 2025
    timestamps = pd.date_range(f"{YEAR}-01-01", periods=N_HOURS, freq="h", tz="UTC")

    # Align SMARD price and load to our 8760h grid
    smard_price = np.full(N_HOURS, np.nan)
    smard_load = np.full(N_HOURS, np.nan)

    if "price" in smard:
        s = smard["price"].reindex(timestamps, method="nearest", tolerance="30min")
        smard_price = s.values.astype(np.float64)

    if "load" in smard:
        s = smard["load"].reindex(timestamps, method="nearest", tolerance="30min")
        smard_load = s.values.astype(np.float64)

    # Use Energy-Charts data for the "real" generation comparison
    echarts = smard.get("echarts", {})

    # Fuel keys for the Energy-Charts generation stack
    # (no nuclear — we're comparing against our model which has no nuclear)
    smard_fuel_keys = [
        "solar", "wind_onshore", "wind_offshore", "biomass", "hydro",
        "gas", "hard_coal", "lignite", "other_conventional",
        "pumped_storage",
    ]

    smard_gen = {}
    for key in smard_fuel_keys:
        if key in echarts:
            smard_gen[key] = echarts[key]
        else:
            smard_gen[key] = np.zeros(N_HOURS)

    # Compute statistics
    valid = ~np.isnan(smard_price)
    price_delta = clearing_price - smard_price

    from scipy.stats import spearmanr

    corr_pearson = float(np.corrcoef(clearing_price[valid], smard_price[valid])[0, 1]) if valid.sum() > 10 else 0
    corr_spearman = float(spearmanr(clearing_price[valid], smard_price[valid])[0]) if valid.sum() > 10 else 0

    # Residual load correlation
    solar = np.array([v if v else 0 for v in smard["solar"].values]) if "solar" in smard else np.zeros(N_HOURS)
    wind_on = np.array([v if v else 0 for v in smard["wind_onshore"].values]) if "wind_onshore" in smard else np.zeros(N_HOURS)
    wind_off = np.array([v if v else 0 for v in smard["wind_offshore"].values]) if "wind_offshore" in smard else np.zeros(N_HOURS)
    residual_load = smard_load - solar - wind_on - wind_off
    r_model_resid = float(np.corrcoef(clearing_price[valid], residual_load[valid])[0, 1])
    r_smard_resid = float(np.corrcoef(smard_price[valid], residual_load[valid])[0, 1])

    # Autocorrelation
    ac1_model = float(np.corrcoef(clearing_price[:-1], clearing_price[1:])[0, 1])
    ac1_smard = float(np.corrcoef(smard_price[valid][:-1], smard_price[valid][1:])[0, 1])

    # Percentiles
    percentiles = {}
    for p in [5, 10, 25, 50, 75, 90, 95]:
        percentiles[f"p{p}_model"] = float(np.nanpercentile(clearing_price, p))
        percentiles[f"p{p}_smard"] = float(np.nanpercentile(smard_price, p))

    # Hourly correlation
    hourly_corr = {}
    for h in range(24):
        idx = np.arange(h, N_HOURS, 24)
        v = valid[idx]
        if v.sum() > 10:
            hourly_corr[h] = float(np.corrcoef(clearing_price[idx[v]], smard_price[idx[v]])[0, 1])

    # Dispatch comparison (TWh).
    # Energy-Charts public_power undercounts behind-meter / self-consumed
    # generation. Add BTM corrections so the comparison is apples-to-apples
    # with the model, which balances total demand (~510 TWh).
    #   - gas: +35 TWh (industrial CHP + small distributed; BDEW total DE gas
    #          generation 2025 ≈ 88 TWh; EC public_power ≈ 53.6 TWh)
    #   - solar: +30 TWh rooftop self-consumption (BDEW total DE PV ≈ 100 TWh;
    #            EC ≈ 70 TWh)
    #   - biomass: +5 TWh small distributed biogas/wood (Fraunhofer ISE)
    EC_BTM_CORRECTION_TWH = {"gas": 35.0, "solar": 30.0, "biomass": 5.0}

    dispatch_comp = {}
    for fuel in smard_fuel_keys:
        model_twh = float(dispatch_by_fuel.get(fuel, np.zeros(1)).sum() / 1e6)
        real_arr = smard_gen.get(fuel, np.zeros(N_HOURS))
        btm = EC_BTM_CORRECTION_TWH.get(fuel, 0.0)
        real_twh = float(real_arr.sum() / 1e6) + btm
        dispatch_comp[fuel] = {
            "model": model_twh,
            "real": real_twh,
            "btm_correction": btm,
        }

    # Monthly price averages
    monthly_prices = {"model": [], "smard": []}
    days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    h_start = 0
    for m_days in days_in_month:
        h_end = h_start + m_days * 24
        monthly_prices["model"].append(float(np.mean(clearing_price[h_start:h_end])))
        monthly_prices["smard"].append(float(np.nanmean(smard_price[h_start:h_end])))
        h_start = h_end

    # Negative price stats
    neg_model = int((clearing_price < 0).sum())
    neg_smard = int((smard_price < 0).sum())

    stats = {
        "model_price_mean": float(np.nanmean(clearing_price)),
        "model_price_median": float(np.nanmedian(clearing_price)),
        "smard_price_mean": float(np.nanmean(smard_price)),
        "smard_price_median": float(np.nanmedian(smard_price)),
        "mae": float(np.nanmean(np.abs(price_delta[valid]))),
        "rmse": float(np.sqrt(np.nanmean(price_delta[valid] ** 2))),
        "bias": float(np.nanmean(price_delta[valid])),
        "correlation": corr_pearson,
        "spearman": corr_spearman,
        "r_model_resid": r_model_resid,
        "r_smard_resid": r_smard_resid,
        "ac1_model": ac1_model,
        "ac1_smard": ac1_smard,
        "model_demand_twh": float(demand.sum() / 1e6),
        "smard_load_twh": float(np.nansum(smard_load) / 1e6),
        "neg_hours_model": neg_model,
        "neg_hours_smard": neg_smard,
        "percentiles": percentiles,
        "hourly_corr": hourly_corr,
        "dispatch_comp": dispatch_comp,
        "monthly_prices": monthly_prices,
    }

    # Per-day statistics
    n_days = 365
    daily_stats = []
    for d in range(n_days):
        h0, h1 = d * 24, (d + 1) * 24
        ds = {
            "date": timestamps[h0].strftime("%Y-%m-%d"),
            "weekday": timestamps[h0].strftime("%a"),
            "model_mean": float(np.mean(clearing_price[h0:h1])),
            "smard_mean": float(np.nanmean(smard_price[h0:h1])),
            "mae": float(np.nanmean(np.abs(price_delta[h0:h1]))),
            "demand_gwh": float(demand[h0:h1].sum() / 1e3),
        }
        ds["delta"] = ds["model_mean"] - ds["smard_mean"]
        daily_stats.append(ds)

    # Prepare hourly data for JSON embedding (keep compact)
    def to_list(arr):
        return [round(float(v), 2) if not np.isnan(v) else None for v in arr]

    hourly_data = {
        "timestamps": [t.strftime("%Y-%m-%d %H:%M") for t in timestamps],
        "model_price": to_list(clearing_price),
        "smard_price": to_list(smard_price),
        "demand": to_list(demand),
        "smard_load": to_list(smard_load),
    }

    # Model dispatch by fuel
    for fuel in FUEL_ORDER:
        hourly_data[f"model_{fuel}"] = to_list(dispatch_by_fuel.get(fuel, np.zeros(N_HOURS)))

    # Energy-Charts generation by fuel
    for key in smard_fuel_keys:
        hourly_data[f"smard_{key}"] = to_list(smard_gen.get(key, np.zeros(N_HOURS)))

    # Build the HTML
    html = build_html(stats, daily_stats, hourly_data)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_HTML, "w") as f:
        f.write(html)
    print(f"  Saved: {OUTPUT_HTML} ({os.path.getsize(OUTPUT_HTML) / 1e6:.1f} MB)")


def build_html(stats, daily_stats, hourly_data):
    """Build self-contained HTML with Chart.js."""

    fuel_colors_json = json.dumps(FUEL_COLORS)
    fuel_labels_json = json.dumps(FUEL_LABELS)
    fuel_order_json = json.dumps(FUEL_ORDER)
    stats_json = json.dumps(stats, indent=2)
    daily_json = json.dumps(daily_stats)
    hourly_json = json.dumps(hourly_data)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Merit Order Comparison 2025 - Model vs Energy-Charts / SMARD</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        background: #0d1117; color: #c9d1d9; padding: 20px; }}
h1 {{ color: #58a6ff; margin-bottom: 8px; font-size: 1.6em; }}
h2 {{ color: #58a6ff; margin: 20px 0 10px; font-size: 1.2em; }}
h3 {{ color: #79c0ff; margin: 12px 0 6px; font-size: 1.05em; }}
.subtitle {{ color: #8b949e; margin-bottom: 20px; }}

.stats-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px; margin: 16px 0;
}}
.stat-card {{
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 14px; text-align: center;
}}
.stat-value {{ font-size: 1.8em; font-weight: 700; color: #f0f6fc; }}
.stat-label {{ font-size: 0.85em; color: #8b949e; margin-top: 4px; }}
.stat-good {{ color: #3fb950; }}
.stat-warn {{ color: #d29922; }}
.stat-bad {{ color: #f85149; }}

.chart-container {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                    padding: 16px; margin: 16px 0; }}
canvas {{ max-height: 400px; }}

.day-nav {{
    display: flex; align-items: center; gap: 12px; margin: 16px 0;
    flex-wrap: wrap;
}}
.day-nav button {{
    background: #21262d; border: 1px solid #30363d; color: #c9d1d9;
    padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 0.9em;
}}
.day-nav button:hover {{ background: #30363d; }}
.day-nav button.active {{ background: #1f6feb; border-color: #58a6ff; }}
.day-nav input[type="date"] {{
    background: #21262d; border: 1px solid #30363d; color: #c9d1d9;
    padding: 6px 10px; border-radius: 6px; font-size: 0.9em;
}}
.day-info {{ color: #8b949e; font-size: 0.9em; }}

table {{
    width: 100%; border-collapse: collapse; margin: 12px 0;
    font-size: 0.85em;
}}
th, td {{
    padding: 6px 10px; text-align: right; border-bottom: 1px solid #21262d;
}}
th {{ color: #8b949e; font-weight: 600; position: sticky; top: 0; background: #161b22; }}
td:first-child, th:first-child {{ text-align: left; }}
tr:hover td {{ background: #1c2128; }}
.pos {{ color: #f85149; }}
.neg {{ color: #3fb950; }}

.table-scroll {{ max-height: 500px; overflow-y: auto; background: #161b22;
                 border: 1px solid #30363d; border-radius: 8px; }}

.daily-table-container {{ max-height: 600px; overflow-y: auto; background: #161b22;
                          border: 1px solid #30363d; border-radius: 8px; padding: 8px; }}

.tab-bar {{ display: flex; gap: 4px; margin-bottom: 12px; }}
.tab-bar button {{
    background: #21262d; border: 1px solid #30363d; color: #8b949e;
    padding: 8px 16px; border-radius: 6px 6px 0 0; cursor: pointer;
}}
.tab-bar button.active {{ background: #161b22; color: #f0f6fc; border-bottom-color: #161b22; }}
.tab-panel {{ display: none; }}
.tab-panel.active {{ display: block; }}
</style>
</head>
<body>

<h1>Merit Order Comparison 2025</h1>
<p class="subtitle">Model dispatch (grid_beta) vs Energy-Charts / SMARD real market data &mdash; 8760 hours</p>

<!-- Summary Stats -->
<div class="stats-grid">
    <div class="stat-card">
        <div class="stat-value" id="st-model-price">--</div>
        <div class="stat-label">Model Avg Price (&euro;/MWh)</div>
    </div>
    <div class="stat-card">
        <div class="stat-value" id="st-smard-price">--</div>
        <div class="stat-label">SMARD Avg Price (&euro;/MWh)</div>
    </div>
    <div class="stat-card">
        <div class="stat-value" id="st-bias">--</div>
        <div class="stat-label">Bias (&euro;/MWh)</div>
    </div>
    <div class="stat-card">
        <div class="stat-value" id="st-mae">--</div>
        <div class="stat-label">MAE (&euro;/MWh)</div>
    </div>
    <div class="stat-card">
        <div class="stat-value" id="st-rmse">--</div>
        <div class="stat-label">RMSE (&euro;/MWh)</div>
    </div>
    <div class="stat-card">
        <div class="stat-value" id="st-corr">--</div>
        <div class="stat-label">Correlation</div>
    </div>
    <div class="stat-card">
        <div class="stat-value" id="st-model-twh">--</div>
        <div class="stat-label">Model Demand (TWh)</div>
    </div>
    <div class="stat-card">
        <div class="stat-value" id="st-smard-twh">--</div>
        <div class="stat-label">SMARD Load (TWh)</div>
    </div>
</div>

<!-- Tab bar -->
<div class="tab-bar">
    <button class="active" onclick="switchTab('overview')">Year Overview</button>
    <button onclick="switchTab('daily')">Daily Explorer</button>
    <button onclick="switchTab('table')">Hourly Table</button>
    <button onclick="switchTab('dispatch')">Dispatch Comparison</button>
    <button onclick="switchTab('deepdive')">Deep Dive</button>
</div>

<!-- TAB: Year Overview -->
<div id="tab-overview" class="tab-panel active">
    <h2>Price Comparison (Full Year)</h2>
    <div class="chart-container">
        <canvas id="chartYearPrice"></canvas>
    </div>
    <h2>Daily Average Price</h2>
    <div class="chart-container">
        <canvas id="chartDailyPrice"></canvas>
    </div>
    <h2>Price Delta Distribution</h2>
    <div class="chart-container">
        <canvas id="chartDeltaHist"></canvas>
    </div>
    <h2>Daily Summary Table</h2>
    <div class="daily-table-container">
        <table id="dailySummaryTable">
            <thead>
                <tr>
                    <th>Date</th><th>Day</th>
                    <th>Model Avg (&euro;)</th><th>SMARD Avg (&euro;)</th>
                    <th>Delta (&euro;)</th><th>MAE (&euro;)</th>
                    <th>Demand (GWh)</th>
                </tr>
            </thead>
            <tbody id="dailyTableBody"></tbody>
        </table>
    </div>
</div>

<!-- TAB: Daily Explorer -->
<div id="tab-daily" class="tab-panel">
    <div class="day-nav">
        <button onclick="prevDay()">&larr; Prev</button>
        <input type="date" id="datePicker" min="{YEAR}-01-01" max="{YEAR}-12-31" value="{YEAR}-01-15">
        <button onclick="nextDay()">Next &rarr;</button>
        <span class="day-info" id="dayInfo"></span>
    </div>
    <h3>Hourly Prices</h3>
    <div class="chart-container">
        <canvas id="chartDayPrice"></canvas>
    </div>
    <h3>Model Dispatch Stack</h3>
    <div class="chart-container">
        <canvas id="chartDayModelStack"></canvas>
    </div>
    <h3>Energy-Charts Generation Stack</h3>
    <div class="chart-container">
        <canvas id="chartDaySmardStack"></canvas>
    </div>
    <h3>Hourly Detail</h3>
    <div class="table-scroll">
        <table>
            <thead>
                <tr>
                    <th>Hour</th><th>Model Price</th><th>SMARD Price</th><th>Delta</th>
                    <th>Model Demand</th><th>SMARD Load</th>
                </tr>
            </thead>
            <tbody id="dayTableBody"></tbody>
        </table>
    </div>
</div>

<!-- TAB: Hourly Table -->
<div id="tab-table" class="tab-panel">
    <h2>Full Hourly Data</h2>
    <p class="subtitle">Scroll through all 8760 hours. Click a column header to sort.</p>
    <div class="day-nav">
        <label>Jump to date: <input type="date" id="tableJumpDate" min="{YEAR}-01-01" max="{YEAR}-12-31" value="{YEAR}-01-01"></label>
        <button onclick="jumpToDate()">Go</button>
    </div>
    <div class="table-scroll" style="max-height:700px;">
        <table>
            <thead>
                <tr>
                    <th>Timestamp</th><th>Model (&euro;)</th><th>SMARD (&euro;)</th>
                    <th>Delta (&euro;)</th><th>Demand (MW)</th><th>SMARD Load (MW)</th>
                </tr>
            </thead>
            <tbody id="hourlyTableBody"></tbody>
        </table>
    </div>
</div>

<!-- TAB: Dispatch Comparison -->
<div id="tab-dispatch" class="tab-panel">
    <h2>Annual Generation by Fuel Type</h2>
    <p style="font-size:0.85em;color:#8b949e;margin:4px 0 8px;">
      Energy-Charts public_power undercounts behind-meter / self-consumed
      generation. Benchmark = EC + BTM correction: <b>gas +35 TWh</b> (industrial CHP,
      BDEW), <b>solar +30 TWh</b> (rooftop self-consumption, Fraunhofer ISE),
      <b>biomass +5 TWh</b> (distributed). The model balances total demand, so it
      sees the full fleet — apples-to-apples.
    </p>
    <div class="chart-container">
        <canvas id="chartAnnualDispatch"></canvas>
    </div>
    <h2>Monthly Generation Comparison</h2>
    <p style="font-size:0.85em;color:#8b949e;margin:4px 0 8px;">
      Monthly bars show <b>raw Energy-Charts</b> (no BTM correction) — useful for
      seeing seasonal patterns. For annual totals see the chart above.
    </p>
    <div class="chart-container">
        <canvas id="chartMonthlyDispatch"></canvas>
    </div>
</div>

<!-- TAB: Deep Dive -->
<div id="tab-deepdive" class="tab-panel">
    <h2>Statistical Deep Dive</h2>

    <h3>Correlation &amp; Accuracy Metrics</h3>
    <div class="stats-grid" id="dd-corr-grid"></div>

    <h3>Price Percentile Comparison</h3>
    <div class="chart-container"><canvas id="chartPercentiles"></canvas></div>

    <h3>Hourly Correlation by Hour-of-Day</h3>
    <div class="chart-container"><canvas id="chartHourlyCorr"></canvas></div>

    <h3>Monthly Average Price</h3>
    <div class="chart-container"><canvas id="chartMonthlyPrice"></canvas></div>

    <h3>Dispatch Comparison (TWh)</h3>
    <p style="font-size:0.85em;color:#8b949e;margin:4px 0 8px;">
      Note: Energy-Charts public_power undercounts behind-meter / self-consumed
      generation. Benchmark = EC + BTM correction: <b>gas +35 TWh</b> (industrial CHP,
      BDEW), <b>solar +30 TWh</b> (rooftop self-consumption, Fraunhofer ISE),
      <b>biomass +5 TWh</b> (distributed). The model balances total demand, so it
      sees the full fleet — apples-to-apples.
    </p>
    <div class="chart-container"><canvas id="chartDispatchComp"></canvas></div>

    <h3>Price Distribution Histogram</h3>
    <div class="chart-container"><canvas id="chartPriceHist"></canvas></div>

    <h3>Model Assumptions</h3>
    <div class="daily-table-container" style="padding:16px;">
        <p><b>Dispatch method:</b> Rolling-horizon MILP unit commitment (48h windows, 24h stride, CBC solver)</p>
        <p><b>Demand:</b> SMARD 2025 hourly load (466 TWh)</p>
        <p><b>RES availability:</b> Solar 95%, Wind onshore 76%, Wind offshore 82% (bundles MaStR Jan-2026 → 2025-avg vintage, maintenance/outages, profile bias). Wind has no behind-meter; solar BTM is on the benchmark side.</p>
        <p><b>Hydro:</b> Run-of-river 62% CF, Reservoir 52% CF (profiles embed resource variability)</p>
        <p><b>Thermal availability:</b> Seasonal outage profiles (ENTSO-E ERAA + VGB). Winter ~74-90%, summer ~47-78% (planned maintenance + ~1.7 GW reserve coal excluded; "other" carrier ×0.30 to match real wholesale-bid scope)</p>
        <p><b>CHP must-run:</b> 45% of heat-constrained CHP, seasonal availability (88% winter → 70% summer)</p>
        <p><b>Benchmark BTM corrections:</b> Energy-Charts public_power undercounts behind-meter / self-consumed generation. Real adjusted = EC plus: gas +17 TWh (industrial CHP), solar +25 TWh (rooftop self-consumption), biomass +3 TWh (distributed).</p>
        <p><b>Coal:</b> MILP-optimized (no pre-commitment), 8h min-up/6h min-down</p>
        <p><b>Pumped storage:</b> 6.5 GW / 40 GWh, 75% round-trip efficiency, SoC-tracked</p>
        <p><b>Imports:</b> 8 GW cap, MCs calibrated to 2025 neighbor prices (50-95 EUR/MWh)</p>
        <p><b>Fossil MC:</b> Efficiency-diversified (TTF=40, coal=13.6, lignite=5, CO2=75 EUR/t)</p>
        <p><b>Market premium:</b> 35% on MILP dual prices (strategic bidding, ancillary services)</p>
        <p><b>Negative prices:</b> Derived from RES surplus magnitude</p>
        <p><b>Clearing prices:</b> LP dual variables of power balance constraint (post MILP binary fix)</p>
    </div>
</div>

<script>
// ── Data ──
const STATS = {stats_json};
const DAILY = {daily_json};
const H = {hourly_json};
const FUEL_ORDER = {fuel_order_json};
const FUEL_COLORS = {fuel_colors_json};
const FUEL_LABELS = {fuel_labels_json};
const SMARD_FUEL_KEYS = ["solar","wind_onshore","wind_offshore","biomass","hydro",
    "gas","hard_coal","lignite","other_conventional","pumped_storage"];

// ── Populate summary stats ──
document.getElementById('st-model-price').textContent = STATS.model_price_mean.toFixed(1);
document.getElementById('st-smard-price').textContent = STATS.smard_price_mean.toFixed(1);
const bias = STATS.bias;
const biasEl = document.getElementById('st-bias');
biasEl.textContent = (bias >= 0 ? '+' : '') + bias.toFixed(1);
biasEl.className = 'stat-value ' + (Math.abs(bias) < 10 ? 'stat-good' : Math.abs(bias) < 25 ? 'stat-warn' : 'stat-bad');
document.getElementById('st-mae').textContent = STATS.mae.toFixed(1);
document.getElementById('st-rmse').textContent = STATS.rmse.toFixed(1);
const corrEl = document.getElementById('st-corr');
corrEl.textContent = STATS.correlation.toFixed(3);
corrEl.className = 'stat-value ' + (STATS.correlation > 0.7 ? 'stat-good' : STATS.correlation > 0.4 ? 'stat-warn' : 'stat-bad');
document.getElementById('st-model-twh').textContent = STATS.model_demand_twh.toFixed(1);
document.getElementById('st-smard-twh').textContent = STATS.smard_load_twh.toFixed(1);

// ── Tab switching ──
function switchTab(name) {{
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab-bar button').forEach(b => b.classList.remove('active'));
    document.getElementById('tab-' + name).classList.add('active');
    event.target.classList.add('active');
    if (name === 'daily') updateDay();
}}

// ── Chart defaults ──
Chart.defaults.color = '#8b949e';
Chart.defaults.borderColor = '#30363d';
Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, sans-serif';

// ── Year price chart (daily averages) ──
const dailyDates = DAILY.map(d => d.date);
const dailyModel = DAILY.map(d => d.model_mean);
const dailySmard = DAILY.map(d => d.smard_mean);

new Chart(document.getElementById('chartDailyPrice'), {{
    type: 'line',
    data: {{
        labels: dailyDates,
        datasets: [
            {{ label: 'Model', data: dailyModel, borderColor: '#58a6ff', borderWidth: 1.5,
               pointRadius: 0, fill: false }},
            {{ label: 'SMARD', data: dailySmard, borderColor: '#f0883e', borderWidth: 1.5,
               pointRadius: 0, fill: false }}
        ]
    }},
    options: {{
        responsive: true,
        interaction: {{ mode: 'index', intersect: false }},
        plugins: {{ legend: {{ position: 'top' }},
                   title: {{ display: false }} }},
        scales: {{
            x: {{ ticks: {{ maxTicksLimit: 24 }} }},
            y: {{ title: {{ display: true, text: 'EUR/MWh' }} }}
        }}
    }}
}});

// ── Year price scatter (hourly, downsampled) ──
// Show every 6th hour for performance
const step = 6;
const yearLabels = [], yearModel = [], yearSmard = [];
for (let i = 0; i < H.timestamps.length; i += step) {{
    yearLabels.push(H.timestamps[i]);
    yearModel.push(H.model_price[i]);
    yearSmard.push(H.smard_price[i]);
}}
new Chart(document.getElementById('chartYearPrice'), {{
    type: 'line',
    data: {{
        labels: yearLabels,
        datasets: [
            {{ label: 'Model', data: yearModel, borderColor: '#58a6ff', borderWidth: 1,
               pointRadius: 0, fill: false }},
            {{ label: 'SMARD', data: yearSmard, borderColor: '#f0883e', borderWidth: 1,
               pointRadius: 0, fill: false }}
        ]
    }},
    options: {{
        responsive: true,
        interaction: {{ mode: 'index', intersect: false }},
        plugins: {{ legend: {{ position: 'top' }},
                   title: {{ display: true, text: 'Hourly prices (every 6h shown)' }} }},
        scales: {{
            x: {{ ticks: {{ maxTicksLimit: 24 }} }},
            y: {{ title: {{ display: true, text: 'EUR/MWh' }}, min: -50, max: 250 }}
        }}
    }}
}});

// ── Delta histogram ──
const deltas = [];
for (let i = 0; i < H.model_price.length; i++) {{
    if (H.model_price[i] !== null && H.smard_price[i] !== null)
        deltas.push(H.model_price[i] - H.smard_price[i]);
}}
const binMin = -150, binMax = 150, binSize = 5;
const bins = [], binLabels = [];
for (let b = binMin; b < binMax; b += binSize) {{
    bins.push(0);
    binLabels.push(b + binSize/2);
}}
deltas.forEach(d => {{
    const idx = Math.floor((d - binMin) / binSize);
    if (idx >= 0 && idx < bins.length) bins[idx]++;
}});
new Chart(document.getElementById('chartDeltaHist'), {{
    type: 'bar',
    data: {{
        labels: binLabels.map(b => b.toFixed(0)),
        datasets: [{{ label: 'Count', data: bins,
            backgroundColor: binLabels.map(b => b > 0 ? '#f8514966' : '#3fb95066'),
            borderColor: binLabels.map(b => b > 0 ? '#f85149' : '#3fb950'),
            borderWidth: 1 }}]
    }},
    options: {{
        responsive: true,
        plugins: {{ legend: {{ display: false }},
                   title: {{ display: true, text: 'Price Delta Distribution (Model - SMARD, EUR/MWh)' }} }},
        scales: {{
            x: {{ title: {{ display: true, text: 'Delta (EUR/MWh)' }}, ticks: {{ maxTicksLimit: 20 }} }},
            y: {{ title: {{ display: true, text: 'Hours' }} }}
        }}
    }}
}});

// ── Daily summary table ──
const dtb = document.getElementById('dailyTableBody');
DAILY.forEach(d => {{
    const cls = d.delta > 0 ? 'pos' : 'neg';
    dtb.innerHTML += `<tr>
        <td>${{d.date}}</td><td>${{d.weekday}}</td>
        <td>${{d.model_mean.toFixed(1)}}</td><td>${{d.smard_mean.toFixed(1)}}</td>
        <td class="${{cls}}">${{d.delta >= 0 ? '+' : ''}}${{d.delta.toFixed(1)}}</td>
        <td>${{d.mae.toFixed(1)}}</td><td>${{d.demand_gwh.toFixed(1)}}</td>
    </tr>`;
}});

// ── Daily explorer ──
let currentDay = 14; // Jan 15
let chartDayPrice, chartDayModelStack, chartDaySmardStack;

function getDayIndex() {{
    const picker = document.getElementById('datePicker');
    const d = new Date(picker.value + 'T00:00:00Z');
    const jan1 = new Date('{YEAR}-01-01T00:00:00Z');
    return Math.floor((d - jan1) / 86400000);
}}

function updateDay() {{
    const dayIdx = getDayIndex();
    if (dayIdx < 0 || dayIdx >= 365) return;
    currentDay = dayIdx;
    const h0 = dayIdx * 24, h1 = h0 + 24;
    const hours = Array.from({{length: 24}}, (_, i) => i + ':00');
    const mp = H.model_price.slice(h0, h1);
    const sp = H.smard_price.slice(h0, h1);
    const delta = mp.map((m, i) => sp[i] !== null ? m - sp[i] : null);

    const info = DAILY[dayIdx];
    document.getElementById('dayInfo').textContent =
        `${{info.date}} (${{info.weekday}}) | Model avg: ${{info.model_mean.toFixed(1)}} | ` +
        `SMARD avg: ${{info.smard_mean.toFixed(1)}} | MAE: ${{info.mae.toFixed(1)}} EUR/MWh`;

    // Price chart
    if (chartDayPrice) chartDayPrice.destroy();
    chartDayPrice = new Chart(document.getElementById('chartDayPrice'), {{
        type: 'line',
        data: {{
            labels: hours,
            datasets: [
                {{ label: 'Model', data: mp, borderColor: '#58a6ff', borderWidth: 2,
                   pointRadius: 3, fill: false }},
                {{ label: 'SMARD', data: sp, borderColor: '#f0883e', borderWidth: 2,
                   pointRadius: 3, fill: false }},
                {{ label: 'Delta', data: delta, borderColor: '#8b949e', borderWidth: 1,
                   borderDash: [4, 4], pointRadius: 0, fill: false }}
            ]
        }},
        options: {{
            responsive: true,
            interaction: {{ mode: 'index', intersect: false }},
            scales: {{ y: {{ title: {{ display: true, text: 'EUR/MWh' }} }} }}
        }}
    }});

    // Model dispatch stack
    const modelFuels = FUEL_ORDER.filter(f => {{
        const key = 'model_' + f;
        return H[key] && H[key].slice(h0, h1).some(v => v > 0);
    }});
    if (chartDayModelStack) chartDayModelStack.destroy();
    chartDayModelStack = new Chart(document.getElementById('chartDayModelStack'), {{
        type: 'bar',
        data: {{
            labels: hours,
            datasets: modelFuels.map(f => ({{
                label: FUEL_LABELS[f] || f,
                data: H['model_' + f].slice(h0, h1).map(v => v / 1000), // GW
                backgroundColor: FUEL_COLORS[f] || '#666',
            }}))
        }},
        options: {{
            responsive: true, plugins: {{ legend: {{ position: 'top' }} }},
            scales: {{
                x: {{ stacked: true }},
                y: {{ stacked: true, title: {{ display: true, text: 'GW' }} }}
            }}
        }}
    }});

    // Energy-Charts generation stack
    const smardFuels = SMARD_FUEL_KEYS.filter(f => {{
        const key = 'smard_' + f;
        return H[key] && H[key].slice(h0, h1).some(v => v > 100);
    }});
    if (chartDaySmardStack) chartDaySmardStack.destroy();
    chartDaySmardStack = new Chart(document.getElementById('chartDaySmardStack'), {{
        type: 'bar',
        data: {{
            labels: hours,
            datasets: smardFuels.map(f => ({{
                label: FUEL_LABELS[f] || f,
                data: H['smard_' + f].slice(h0, h1).map(v => v / 1000), // GW
                backgroundColor: FUEL_COLORS[f] || '#666',
            }}))
        }},
        options: {{
            responsive: true, plugins: {{ legend: {{ position: 'top' }} }},
            scales: {{
                x: {{ stacked: true }},
                y: {{ stacked: true, title: {{ display: true, text: 'GW' }} }}
            }}
        }}
    }});

    // Hourly table for this day
    const tbody = document.getElementById('dayTableBody');
    tbody.innerHTML = '';
    for (let h = 0; h < 24; h++) {{
        const i = h0 + h;
        const m = H.model_price[i], s = H.smard_price[i];
        const d = (m !== null && s !== null) ? (m - s) : null;
        const cls = d !== null ? (d > 0 ? 'pos' : 'neg') : '';
        tbody.innerHTML += `<tr>
            <td>${{h}}:00</td>
            <td>${{m !== null ? m.toFixed(1) : '-'}}</td>
            <td>${{s !== null ? s.toFixed(1) : '-'}}</td>
            <td class="${{cls}}">${{d !== null ? (d >= 0 ? '+' : '') + d.toFixed(1) : '-'}}</td>
            <td>${{H.demand[i] !== null ? Math.round(H.demand[i]).toLocaleString() : '-'}}</td>
            <td>${{H.smard_load[i] !== null ? Math.round(H.smard_load[i]).toLocaleString() : '-'}}</td>
        </tr>`;
    }}
}}

document.getElementById('datePicker').addEventListener('change', updateDay);
function prevDay() {{
    const picker = document.getElementById('datePicker');
    const d = new Date(picker.value);
    d.setDate(d.getDate() - 1);
    if (d >= new Date('{YEAR}-01-01')) {{
        picker.value = d.toISOString().slice(0, 10);
        updateDay();
    }}
}}
function nextDay() {{
    const picker = document.getElementById('datePicker');
    const d = new Date(picker.value);
    d.setDate(d.getDate() + 1);
    if (d <= new Date('{YEAR}-12-31')) {{
        picker.value = d.toISOString().slice(0, 10);
        updateDay();
    }}
}}

// ── Hourly table (lazy rendered) ──
function renderHourlyTable(startIdx) {{
    const tbody = document.getElementById('hourlyTableBody');
    tbody.innerHTML = '';
    const end = Math.min(startIdx + 720, H.timestamps.length); // 30 days
    for (let i = startIdx; i < end; i++) {{
        const m = H.model_price[i], s = H.smard_price[i];
        const d = (m !== null && s !== null) ? (m - s) : null;
        const cls = d !== null ? (d > 0 ? 'pos' : 'neg') : '';
        tbody.innerHTML += `<tr>
            <td>${{H.timestamps[i]}}</td>
            <td>${{m !== null ? m.toFixed(1) : '-'}}</td>
            <td>${{s !== null ? s.toFixed(1) : '-'}}</td>
            <td class="${{cls}}">${{d !== null ? (d >= 0 ? '+' : '') + d.toFixed(1) : '-'}}</td>
            <td>${{H.demand[i] !== null ? Math.round(H.demand[i]).toLocaleString() : '-'}}</td>
            <td>${{H.smard_load[i] !== null ? Math.round(H.smard_load[i]).toLocaleString() : '-'}}</td>
        </tr>`;
    }}
}}
renderHourlyTable(0);

function jumpToDate() {{
    const val = document.getElementById('tableJumpDate').value;
    const d = new Date(val + 'T00:00:00Z');
    const jan1 = new Date('{YEAR}-01-01T00:00:00Z');
    const idx = Math.floor((d - jan1) / 86400000) * 24;
    renderHourlyTable(Math.max(0, idx));
}}

// ── Dispatch comparison (annual totals) ──
// Use STATS.dispatch_comp as canonical data source — same as the Deep Dive
// chart — so the two charts agree. dispatch_comp.real includes the BTM
// correction (Energy-Charts public_power + behind-meter estimates).
const modelAnnual = {{}}, smardAnnual = {{}};
SMARD_FUEL_KEYS.forEach(f => {{
    if (STATS.dispatch_comp && STATS.dispatch_comp[f]) {{
        modelAnnual[f] = STATS.dispatch_comp[f].model;
        smardAnnual[f] = STATS.dispatch_comp[f].real;
    }} else {{
        modelAnnual[f] = 0;
        smardAnnual[f] = 0;
    }}
}});

const allFuels = SMARD_FUEL_KEYS.filter(
    f => (modelAnnual[f] || 0) > 0.1 || (smardAnnual[f] || 0) > 0.1
);
new Chart(document.getElementById('chartAnnualDispatch'), {{
    type: 'bar',
    data: {{
        labels: allFuels.map(f => FUEL_LABELS[f] || f),
        datasets: [
            {{ label: 'Model', data: allFuels.map(f => (modelAnnual[f] || 0).toFixed(1)),
               backgroundColor: '#58a6ff88', borderColor: '#58a6ff', borderWidth: 1 }},
            {{ label: 'Energy-Charts (+BTM)', data: allFuels.map(f => (smardAnnual[f] || 0).toFixed(1)),
               backgroundColor: '#f0883e88', borderColor: '#f0883e', borderWidth: 1 }}
        ]
    }},
    options: {{
        responsive: true,
        plugins: {{ legend: {{ position: 'top' }} }},
        scales: {{ y: {{ title: {{ display: true, text: 'TWh' }} }} }}
    }}
}});

// Monthly dispatch
const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const daysInMonth = [31,28,31,30,31,30,31,31,30,31,30,31];
let modelMonthly = {{}}, smardMonthly = {{}};
allFuels.forEach(f => {{ modelMonthly[f] = []; smardMonthly[f] = []; }});
let hStart = 0;
for (let m = 0; m < 12; m++) {{
    const hEnd = hStart + daysInMonth[m] * 24;
    allFuels.forEach(f => {{
        const mk = 'model_' + f, sk = 'smard_' + f;
        let mSum = 0, sSum = 0;
        for (let i = hStart; i < hEnd; i++) {{
            if (H[mk]) mSum += (H[mk][i] || 0);
            if (H[sk]) sSum += (H[sk][i] || 0);
        }}
        modelMonthly[f].push(mSum / 1e3); // GWh
        smardMonthly[f].push(sSum / 1e3);
    }});
    hStart = hEnd;
}}

// Stacked monthly: just top fuels
const topFuels = allFuels.filter(f => f !== 'imports' && ((modelAnnual[f] || 0) > 1 || (smardAnnual[f] || 0) > 1));
const monthlyDatasets = [];
topFuels.forEach(f => {{
    monthlyDatasets.push({{
        label: 'M:' + (FUEL_LABELS[f] || f),
        data: modelMonthly[f],
        backgroundColor: FUEL_COLORS[f] || '#666',
        stack: 'model',
    }});
}});
topFuels.forEach(f => {{
    monthlyDatasets.push({{
        label: 'EC:' + (FUEL_LABELS[f] || f),
        data: smardMonthly[f],
        backgroundColor: FUEL_COLORS[f] || '#666',
        stack: 'echarts',
        borderWidth: 1, borderColor: '#f0883e44',
    }});
}});
new Chart(document.getElementById('chartMonthlyDispatch'), {{
    type: 'bar',
    data: {{ labels: months, datasets: monthlyDatasets }},
    options: {{
        responsive: true,
        plugins: {{ legend: {{ display: false }},
                   title: {{ display: true, text: 'Left bar = Model, Right bar = Energy-Charts (GWh/month)' }} }},
        scales: {{
            x: {{ stacked: true }},
            y: {{ stacked: true, title: {{ display: true, text: 'GWh' }} }}
        }}
    }}
}});

// ═══════════════════════════════════════════════════════════════════════════════
// DEEP DIVE TAB
// ═══════════════════════════════════════════════════════════════════════════════

// Correlation metrics grid
const ddGrid = document.getElementById('dd-corr-grid');
if (ddGrid) {{
    const metrics = [
        ['Pearson r', STATS.correlation?.toFixed(3) || 'N/A', STATS.correlation > 0.5 ? 'stat-good' : STATS.correlation > 0.3 ? 'stat-warn' : 'stat-bad'],
        ['Spearman &rho;', STATS.spearman?.toFixed(3) || 'N/A', STATS.spearman > 0.5 ? 'stat-good' : 'stat-warn'],
        ['MAE', STATS.mae?.toFixed(1) + ' &euro;', Math.abs(STATS.mae) < 25 ? 'stat-good' : 'stat-warn'],
        ['RMSE', STATS.rmse?.toFixed(1) + ' &euro;', STATS.rmse < 35 ? 'stat-good' : 'stat-warn'],
        ['Bias', (STATS.bias >= 0 ? '+' : '') + STATS.bias?.toFixed(1) + ' &euro;', Math.abs(STATS.bias) < 10 ? 'stat-good' : 'stat-warn'],
        ['r(model, resid.load)', STATS.r_model_resid?.toFixed(3) || 'N/A', ''],
        ['r(SMARD, resid.load)', STATS.r_smard_resid?.toFixed(3) || 'N/A', ''],
        ['Autocorr lag-1h (Model)', STATS.ac1_model?.toFixed(3) || 'N/A', ''],
        ['Autocorr lag-1h (SMARD)', STATS.ac1_smard?.toFixed(3) || 'N/A', ''],
        ['Neg hours (Model)', STATS.neg_hours_model || 0, ''],
        ['Neg hours (SMARD)', STATS.neg_hours_smard || 0, ''],
        ['Model median', STATS.model_price_median?.toFixed(1) + ' &euro;', ''],
    ];
    metrics.forEach(([label, val, cls]) => {{
        ddGrid.innerHTML += `<div class="stat-card"><div class="stat-value ${{cls}}">${{val}}</div><div class="stat-label">${{label}}</div></div>`;
    }});
}}

// Percentile comparison chart
if (STATS.percentiles) {{
    const pcts = [5,10,25,50,75,90,95];
    new Chart(document.getElementById('chartPercentiles'), {{
        type: 'bar',
        data: {{
            labels: pcts.map(p => 'P' + p),
            datasets: [
                {{ label: 'Model', data: pcts.map(p => STATS.percentiles['p'+p+'_model']),
                   backgroundColor: '#58a6ff88', borderColor: '#58a6ff', borderWidth: 1 }},
                {{ label: 'SMARD', data: pcts.map(p => STATS.percentiles['p'+p+'_smard']),
                   backgroundColor: '#f0883e88', borderColor: '#f0883e', borderWidth: 1 }}
            ]
        }},
        options: {{ responsive: true, scales: {{ y: {{ title: {{ display: true, text: 'EUR/MWh' }} }} }} }}
    }});
}}

// Hourly correlation
if (STATS.hourly_corr) {{
    const hours = Object.keys(STATS.hourly_corr).map(Number).sort((a,b) => a-b);
    new Chart(document.getElementById('chartHourlyCorr'), {{
        type: 'bar',
        data: {{
            labels: hours.map(h => h + ':00'),
            datasets: [{{ label: 'Pearson r by hour', data: hours.map(h => STATS.hourly_corr[h]),
                backgroundColor: hours.map(h => STATS.hourly_corr[h] > 0.3 ? '#3fb95088' : STATS.hourly_corr[h] > 0 ? '#d2992288' : '#f8514988'),
                borderWidth: 1 }}]
        }},
        options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }},
                   scales: {{ y: {{ title: {{ display: true, text: 'Correlation' }}, min: -0.2, max: 0.8 }} }} }}
    }});
}}

// Monthly price comparison
if (STATS.monthly_prices) {{
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    new Chart(document.getElementById('chartMonthlyPrice'), {{
        type: 'line',
        data: {{
            labels: months,
            datasets: [
                {{ label: 'Model', data: STATS.monthly_prices.model, borderColor: '#58a6ff',
                   borderWidth: 2, pointRadius: 4, fill: false }},
                {{ label: 'SMARD', data: STATS.monthly_prices.smard, borderColor: '#f0883e',
                   borderWidth: 2, pointRadius: 4, fill: false }}
            ]
        }},
        options: {{ responsive: true, scales: {{ y: {{ title: {{ display: true, text: 'EUR/MWh' }} }} }} }}
    }});
}}

// Dispatch comparison bar chart
if (STATS.dispatch_comp) {{
    const fuels = Object.keys(STATS.dispatch_comp).filter(f =>
        STATS.dispatch_comp[f].model > 0.5 || STATS.dispatch_comp[f].real > 0.5);
    new Chart(document.getElementById('chartDispatchComp'), {{
        type: 'bar',
        data: {{
            labels: fuels.map(f => FUEL_LABELS[f] || f),
            datasets: [
                {{ label: 'Model', data: fuels.map(f => STATS.dispatch_comp[f].model.toFixed(1)),
                   backgroundColor: '#58a6ff88', borderColor: '#58a6ff', borderWidth: 1 }},
                {{ label: 'Energy-Charts', data: fuels.map(f => STATS.dispatch_comp[f].real.toFixed(1)),
                   backgroundColor: '#f0883e88', borderColor: '#f0883e', borderWidth: 1 }}
            ]
        }},
        options: {{ responsive: true, scales: {{ y: {{ title: {{ display: true, text: 'TWh' }} }} }} }}
    }});
}}

// Price histogram overlay
(function() {{
    const binSize = 10, binMin = -60, binMax = 250;
    const nBins = Math.ceil((binMax - binMin) / binSize);
    const mBins = new Array(nBins).fill(0);
    const sBins = new Array(nBins).fill(0);
    const labels = [];
    for (let b = 0; b < nBins; b++) {{
        labels.push(binMin + b * binSize + binSize/2);
    }}
    for (let i = 0; i < H.model_price.length; i++) {{
        const mp = H.model_price[i], sp = H.smard_price[i];
        if (mp !== null) {{ const idx = Math.floor((mp - binMin) / binSize); if (idx >= 0 && idx < nBins) mBins[idx]++; }}
        if (sp !== null) {{ const idx = Math.floor((sp - binMin) / binSize); if (idx >= 0 && idx < nBins) sBins[idx]++; }}
    }}
    new Chart(document.getElementById('chartPriceHist'), {{
        type: 'bar',
        data: {{
            labels: labels.map(l => l.toFixed(0)),
            datasets: [
                {{ label: 'Model', data: mBins, backgroundColor: '#58a6ff44', borderColor: '#58a6ff', borderWidth: 1 }},
                {{ label: 'SMARD', data: sBins, backgroundColor: '#f0883e44', borderColor: '#f0883e', borderWidth: 1 }}
            ]
        }},
        options: {{ responsive: true,
                   scales: {{
                       x: {{ title: {{ display: true, text: 'EUR/MWh' }}, ticks: {{ maxTicksLimit: 20 }} }},
                       y: {{ title: {{ display: true, text: 'Hours' }} }}
                   }}
        }}
    }});
}})();

</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 80)
    print("  Merit Order Comparison 2025: Model vs Energy-Charts / SMARD")
    print("=" * 80)

    # Step 1: Download SMARD data + Energy-Charts generation
    smard = download_all_smard()

    # Step 2: Use SMARD real load as demand
    demand = prepare_smard_demand(smard)

    # Step 3: Load model generators
    gens, gen_ts = load_model()

    # Step 4: Run MILP unit commitment dispatch (rolling 48h horizon)
    clearing_price, dispatch_by_fuel, _ = run_milp_uc(gens, gen_ts, demand)

    # Step 5: Generate HTML (uses Energy-Charts data for generation comparison)
    generate_html(clearing_price, dispatch_by_fuel, demand, smard)

    print("\nDone!")


if __name__ == "__main__":
    main()
