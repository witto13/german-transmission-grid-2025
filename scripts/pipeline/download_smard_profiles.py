#!/usr/bin/env python3
"""
Download hourly generation and load data from SMARD API for Germany 2025.

SMARD API returns weekly JSON chunks. We fetch all weeks covering 2025.

Correct SMARD filter IDs (from bundesAPI/smard-api documentation):
  4068 - Stromerzeugung: Photovoltaik (Solar PV)
  4067 - Stromerzeugung: Wind Onshore
  1225 - Stromerzeugung: Wind Offshore
  410  - Stromverbrauch: Gesamt (Netzlast / total grid load)

Output: CSV files in data/profiles/
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

import pandas as pd
import numpy as np

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "profiles")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# SMARD filter IDs (from bundesAPI/smard-api GitHub documentation)
# 4068 = Photovoltaik, 4067 = Wind Onshore, 1225 = Wind Offshore, 410 = Gesamt (Netzlast)
FILTERS = {
    "solar": 4068,
    "wind_onshore": 4067,
    "wind_offshore": 1225,
    "load": 410,
}

BASE_URL = "https://www.smard.de/app/chart_data/{filt}/DE/{filt}_DE_hour_{ts}.json"
INDEX_URL = "https://www.smard.de/app/chart_data/{filt}/DE/index_hour.json"

# 2025 boundaries (UTC)
START = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
END = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
YEAR = 2025


def fetch_json(url, retries=3, delay=2):
    """Fetch JSON from URL with retries."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, Exception) as e:
            if attempt < retries - 1:
                print(f"  Retry {attempt+1} for {url}: {e}")
                time.sleep(delay)
            else:
                print(f"  FAILED {url}: {e}")
                return None


def get_weekly_timestamps(filt_id):
    """Get list of available weekly timestamps from SMARD index."""
    url = INDEX_URL.format(filt=filt_id)
    data = fetch_json(url)
    if data is None:
        print(f"ERROR: Could not fetch index for filter {filt_id}")
        sys.exit(1)

    # Handle both {"timestamps": [...]} and plain array
    if isinstance(data, dict):
        ts_list = data.get("timestamps", list(data.values())[0])
    else:
        ts_list = data

    # Filter timestamps that overlap with target year
    week_ms = 7 * 24 * 3600 * 1000
    timestamps = [int(ts) for ts in ts_list if int(ts) < END and int(ts) + week_ms > START]
    return sorted(timestamps)


def download_series(name, filt_id):
    """Download all weekly chunks for a given filter covering target year."""
    print(f"\n--- Downloading {name} (filter {filt_id}) ---")
    timestamps = get_weekly_timestamps(filt_id)
    print(f"  {len(timestamps)} weekly chunks to download")

    all_points = []
    for i, ts in enumerate(timestamps):
        url = BASE_URL.format(filt=filt_id, ts=ts)
        data = fetch_json(url)
        if data and "series" in data:
            for point in data["series"]:
                ts_val, value = point
                if START <= ts_val < END and value is not None:
                    all_points.append((ts_val, value))
        if (i + 1) % 10 == 0:
            print(f"  Fetched {i+1}/{len(timestamps)} chunks...")
        # Be polite to the API
        time.sleep(0.3)

    print(f"  Total data points for {YEAR}: {len(all_points)}")
    return all_points


def main():
    results = {}
    for name, filt_id in FILTERS.items():
        points = download_series(name, filt_id)
        if not points:
            print(f"WARNING: No data for {name}")
            continue

        df = pd.DataFrame(points, columns=["timestamp_ms", "value_mw"])
        df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
        df = df.sort_values("timestamp").drop_duplicates(subset="timestamp")
        df = df.set_index("timestamp")
        results[name] = df

        # Report
        print(f"  Date range: {df.index.min()} to {df.index.max()}")
        print(f"  Rows: {len(df)}, Min: {df['value_mw'].min():.1f} MW, "
              f"Max: {df['value_mw'].max():.1f} MW, Mean: {df['value_mw'].mean():.1f} MW")

    # Save generation files (MW)
    for name in ["solar", "wind_onshore", "wind_offshore"]:
        if name not in results:
            continue
        df = results[name][["value_mw"]].copy()
        df.columns = ["generation_mw"]
        outpath = os.path.join(OUTPUT_DIR, f"{name}_gen_mw_{YEAR}.csv")
        df.to_csv(outpath)
        print(f"\nSaved: {outpath} ({len(df)} rows)")

    # Save load file (MW)
    if "load" in results:
        df = results["load"][["value_mw"]].copy()
        df.columns = ["load_mw"]
        outpath = os.path.join(OUTPUT_DIR, f"load_mw_{YEAR}.csv")
        df.to_csv(outpath)
        print(f"\nSaved: {outpath} ({len(df)} rows)")

    # Compute and save capacity factors
    # Installed capacities for Germany mid-2025 (approximate, from BNetzA/MaStR):
    # Solar: ~105 GW (grew from ~97 to ~113 GW during 2025)
    # Wind Onshore: ~65 GW
    # Wind Offshore: ~10 GW (NordSee cluster ramp-up)
    # We use mid-2025 estimates. CF = generation / installed_capacity.
    installed_capacity = {
        "solar": 105000,        # MW (mid-2025 estimate)
        "wind_onshore": 65000,  # MW
        "wind_offshore": 10000, # MW
    }

    print("\n--- Computing Capacity Factors ---")
    for name, cap_mw in installed_capacity.items():
        if name not in results:
            continue
        df = results[name][["value_mw"]].copy()
        df["capacity_factor"] = (df["value_mw"] / cap_mw).clip(0, 1)
        cf_df = df[["capacity_factor"]]
        outpath = os.path.join(OUTPUT_DIR, f"{name}_cf_{YEAR}.csv")
        cf_df.to_csv(outpath)
        print(f"  {name}: CF mean={cf_df['capacity_factor'].mean():.3f}, "
              f"max={cf_df['capacity_factor'].max():.3f}, installed_cap={cap_mw} MW")
        print(f"  Saved: {outpath}")

    # Summary
    print("\n=== SUMMARY ===")
    for name in ["solar", "wind_onshore", "wind_offshore", "load"]:
        if name in results:
            print(f"  {name}: {len(results[name])} hourly points")
        else:
            print(f"  {name}: MISSING")

    expected = 8760  # 2025 is not a leap year: 365 * 24
    for name, df in results.items():
        n = len(df)
        if n < expected:
            print(f"  WARNING: {name} has {n} points, expected {expected} (missing {expected - n})")
        elif n > expected:
            print(f"  NOTE: {name} has {n} points, expected {expected} (extra {n - expected})")


if __name__ == "__main__":
    main()
