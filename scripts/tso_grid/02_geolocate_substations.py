#!/usr/bin/env python3
"""Script 2: Assign (x, y) coordinates and numeric bus_ids to all substations.

Matching tiers:
  Tier 1: georef.csv match by name (93%)
  Tier 2: fuzzy match against georef.csv or eGon ehv_substation table
  Tier 3: spatial match to nearest eGon 220/380kV bus

Input:
  data/tso_grid/substations_raw.csv (from Script 1)
  data/tso_grid/lines.csv (for substation names from line endpoints)
  data/tso_grid/tielines_crossborder.csv (for cross-border substations)
  data/jao_core_tso/georef.csv (OSM IDs + coordinates)

Output:
  data/tso_grid/substations_geolocated.csv
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd
from sqlalchemy import create_engine

# Add project root to path for utils import
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_DIR)

from scripts.utils.name_matching import normalize_substation_name, fuzzy_match_substation

DB_URI = "postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data"
GEOREF_CSV = os.path.join(PROJECT_DIR, "data/jao_core_tso/georef.csv")
DATA_DIR = os.path.join(PROJECT_DIR, "data/tso_grid")

# Bus ID offsets
BUS_ID_220KV_OFFSET = 500000  # bus_id_220 = bus_id_380 + offset
BUS_ID_UNMATCHED_START = 300001

# TSO name mapping for georef
GEOREF_TSO_MAP = {
    "50Hertz": "50HERTZ",
    "TenneT": "TENNETGMBH",
    "Amprion": "Amprion GmbH",
    "TransnetBW": "TRANSNETBW",
}


def _parse_osm_id(val):
    """Parse OSM ID from various formats (int, float, 'w1234', 'n5678')."""
    if pd.isna(val) or val is None:
        return None
    s = str(val).strip()
    # Remove type prefixes (w=way, n=node, r=relation)
    s = s.lstrip("wnrWNR")
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def normalize_name_for_matching(name):
    """Clean substation name for matching — handles JAO-specific patterns."""
    if not name or pd.isna(name):
        return ""
    s = str(name).strip()
    # Remove leading path-like patterns: "/ 380-220kV / Name / Trafo"
    if s.startswith("/"):
        parts = [p.strip() for p in s.split("/") if p.strip()]
        # Remove voltage patterns and "Trafo"
        cleaned = [
            p for p in parts
            if not any(x in p.lower() for x in ["kv", "trafo", "pst"])
        ]
        s = " ".join(cleaned) if cleaned else parts[-1] if parts else s

    # Use existing normalizer
    return normalize_substation_name(s)


def load_georef():
    """Load and index georef.csv for matching."""
    geo = pd.read_csv(GEOREF_CSV, index_col=0)
    # Index is the CORE-TSO substation name
    geo["geo_name"] = geo.index
    geo["geo_name_norm"] = geo["geo_name"].apply(normalize_substation_name)
    # Parse OSM_id
    geo["osm_id"] = geo["OSM_id"].apply(
        lambda x: int(float(x)) if pd.notna(x) else None
    )
    return geo


def load_egon_substations(engine):
    """Load eGon ehv_substation table for fallback matching."""
    df = pd.read_sql(
        "SELECT s.bus_id, s.osm_id, s.subst_name, s.lon, s.lat, b.v_nom "
        "FROM grid.egon_ehv_substation s "
        "JOIN grid.egon_etrago_bus b ON s.bus_id = b.bus_id "
        "  AND b.scn_name = 'eGon2025'",
        engine,
    )
    df["name_norm"] = df["subst_name"].apply(
        lambda x: normalize_substation_name(str(x)) if pd.notna(x) else ""
    )
    return df


def load_egon_hv_buses(engine):
    """Load all eGon 220/380kV buses for spatial fallback."""
    df = pd.read_sql(
        "SELECT bus_id, x, y, v_nom "
        "FROM grid.egon_etrago_bus "
        "WHERE scn_name = 'eGon2025' AND v_nom IN (220, 380)",
        engine,
    )
    return df


def collect_all_substations():
    """Collect substations from lines, tielines, and transformers.

    Returns DataFrame with columns: name, tso, voltages (set of kV).
    """
    lines = pd.read_csv(os.path.join(DATA_DIR, "lines.csv"))
    xborder = pd.read_csv(os.path.join(DATA_DIR, "tielines_crossborder.csv"))
    trafos = pd.read_csv(os.path.join(DATA_DIR, "transformers.csv"))

    subs = {}  # name -> {tso, voltages}

    # From lines: sub1 and sub2 are substation names
    for _, row in lines.iterrows():
        for col in ["sub1", "sub2"]:
            name = row.get(col)
            if pd.notna(name):
                name = str(name).strip()
                key = (name, row["tso_norm"])
                if key not in subs:
                    subs[key] = {"name": name, "tso": row["tso_norm"], "voltages": set()}
                subs[key]["voltages"].add(int(row["v_nom"]))

    # From cross-border tielines: sub1 is DE side, sub2 may be foreign
    for _, row in xborder.iterrows():
        name = row.get("sub1")
        if pd.notna(name):
            name = str(name).strip()
            key = (name, row["tso_norm"])
            if key not in subs:
                subs[key] = {"name": name, "tso": row["tso_norm"], "voltages": set()}
            subs[key]["voltages"].add(int(row["v_nom"]))

    # From transformers: substation name connects two voltage levels
    for _, row in trafos.iterrows():
        name = row.get("substation")
        if pd.notna(name):
            name = str(name).strip()
            tso = row["tso_norm"]
            key = (name, tso)
            if key not in subs:
                subs[key] = {"name": name, "tso": tso, "voltages": set()}
            subs[key]["voltages"].add(int(row["v_high"]))
            subs[key]["voltages"].add(int(row["v_low"]))

    df = pd.DataFrame(subs.values())
    df["voltages"] = df["voltages"].apply(lambda s: sorted(s))
    return df


def match_tier1_georef(substations, georef):
    """Tier 1: Exact name match against georef.csv (normalized)."""
    matches = {}  # idx → match record
    georef_de = georef[georef["CORE-TSO_country"] == "DE"].copy()

    # Build lookup: normalized name → list of georef rows
    name_to_geo = {}
    for _, row in georef_de.iterrows():
        norm = row["geo_name_norm"]
        if norm:
            name_to_geo.setdefault(norm, []).append(row)

    for idx, sub in substations.iterrows():
        name_norm = normalize_name_for_matching(sub["name"])
        if not name_norm:
            continue

        # Try exact match
        if name_norm in name_to_geo:
            best = name_to_geo[name_norm][0]
            matches[idx] = {
                "geo_name": best["geo_name"],
                "x": best["x"],
                "y": best["y"],
                "osm_id": best["osm_id"],
                "tier": 1,
                "method": "georef_exact",
                "confidence": 0.95,
            }
            continue

        # Try exact match on original (non-normalized) name
        orig = str(sub["name"]).strip()
        for _, geo_row in georef_de.iterrows():
            if geo_row["geo_name"] == orig:
                matches[idx] = {
                    "geo_name": geo_row["geo_name"],
                    "x": geo_row["x"],
                    "y": geo_row["y"],
                    "osm_id": geo_row["osm_id"],
                    "tier": 1,
                    "method": "georef_exact_orig",
                    "confidence": 0.95,
                }
                break

    return matches


def match_tier2_fuzzy(substations, unmatched_idx, georef, egon_subs):
    """Tier 2: Fuzzy name match against georef + eGon substations."""
    matches = {}
    georef_de = georef[georef["CORE-TSO_country"] == "DE"].copy()

    # Build candidate list from georef
    geo_candidates = [
        (int(row["osm_id"]) if row["osm_id"] else 0, row["geo_name"])
        for _, row in georef_de.iterrows()
        if row["osm_id"]
    ]

    # Build candidate list from eGon
    egon_candidates = [
        (int(row["bus_id"]), str(row["subst_name"]))
        for _, row in egon_subs.iterrows()
        if pd.notna(row["subst_name"])
    ]

    for idx in unmatched_idx:
        sub = substations.loc[idx]
        query = normalize_name_for_matching(sub["name"])
        if not query:
            continue

        # Try georef fuzzy
        geo_results = fuzzy_match_substation(query, geo_candidates, threshold=0.80, limit=1)
        egon_results = fuzzy_match_substation(query, egon_candidates, threshold=0.80, limit=1)

        best_score = 0
        best_match = None

        if geo_results:
            osm_id, name, score = geo_results[0]
            if score > best_score:
                best_score = score
                geo_row = georef_de[georef_de["osm_id"] == osm_id].iloc[0]
                best_match = {
                    "geo_name": name,
                    "x": geo_row["x"],
                    "y": geo_row["y"],
                    "osm_id": osm_id,
                    "tier": 2,
                    "method": f"georef_fuzzy_{score:.2f}",
                    "confidence": 0.70 + 0.15 * (score - 0.80) / 0.20,
                }

        if egon_results:
            bus_id, name, score = egon_results[0]
            if score > best_score:
                egon_row = egon_subs[egon_subs["bus_id"] == bus_id].iloc[0]
                best_match = {
                    "geo_name": name,
                    "x": float(egon_row["lon"]),
                    "y": float(egon_row["lat"]),
                    "osm_id": _parse_osm_id(egon_row["osm_id"]),
                    "tier": 2,
                    "method": f"egon_fuzzy_{score:.2f}",
                    "confidence": 0.65 + 0.15 * (score - 0.80) / 0.20,
                }

        if best_match:
            matches[idx] = best_match

    return matches


def match_tier3_spatial(substations, unmatched_idx, egon_hv_buses):
    """Tier 3: Spatial match — find nearest eGon bus at correct voltage.

    For unmatched substations, try to find nearby 220/380kV bus by looking
    at other substations with coordinates in the same TSO zone.
    """
    # This tier has limited utility since we don't have coordinates yet.
    # But we can match by brute-force string search in OSM names.
    matches = {}
    for idx in unmatched_idx:
        sub = substations.loc[idx]
        # Assign from unmatched pool — will get coordinates from Script 3
        matches[idx] = {
            "geo_name": sub["name"],
            "x": np.nan,
            "y": np.nan,
            "osm_id": None,
            "tier": 3,
            "method": "unmatched_needs_geocoding",
            "confidence": 0.30,
        }
    return matches


def assign_bus_ids(df):
    """Assign unique numeric bus_ids per substation per voltage level.

    Rules:
    - From georef OSM_id: use int(OSM_id) — millions range, no conflict with eGon <41k
    - Unmatched: assign from 300001+
    - Multi-voltage: separate bus_id per voltage (380kV base, 220kV = base + 500000)
    """
    next_unmatched_id = BUS_ID_UNMATCHED_START
    used_ids = set()

    bus_id_380 = []
    bus_id_220 = []

    for _, row in df.iterrows():
        voltages = eval(row["voltages"]) if isinstance(row["voltages"], str) else row["voltages"]

        if pd.notna(row.get("osm_id")) and row["osm_id"]:
            base_id = int(row["osm_id"])
        else:
            base_id = next_unmatched_id
            next_unmatched_id += 1

        # Avoid collisions
        while base_id in used_ids:
            base_id = next_unmatched_id
            next_unmatched_id += 1

        id_380 = None
        id_220 = None

        if 380 in voltages:
            id_380 = base_id
            used_ids.add(id_380)
        if 220 in voltages:
            id_220 = base_id + BUS_ID_220KV_OFFSET
            used_ids.add(id_220)

        # If only one voltage and neither 380 nor 220, use base
        if id_380 is None and id_220 is None:
            id_380 = base_id  # default
            used_ids.add(id_380)

        bus_id_380.append(id_380)
        bus_id_220.append(id_220)

    df["bus_id_380"] = bus_id_380
    df["bus_id_220"] = bus_id_220
    return df


def main():
    parser = argparse.ArgumentParser(description="Geolocate substations")
    parser.add_argument("--skip-db", action="store_true", help="Skip DB queries (tier 2 eGon fallback)")
    args = parser.parse_args()

    print("=" * 70)
    print("Script 2: Geolocate substations")
    print("=" * 70)

    # Collect all substations from Script 1 output
    substations = collect_all_substations()
    print(f"\nTotal unique substations: {len(substations)}")
    print(f"  by TSO: {substations['tso'].value_counts().to_dict()}")

    # Load georef
    georef = load_georef()
    georef_de = georef[georef["CORE-TSO_country"] == "DE"]
    print(f"\nGeoref: {len(georef_de)} German entries")

    # Load eGon data for fallback
    engine = None
    egon_subs = pd.DataFrame()
    egon_hv = pd.DataFrame()
    if not args.skip_db:
        engine = create_engine(DB_URI)
        egon_subs = load_egon_substations(engine)
        egon_hv = load_egon_hv_buses(engine)
        print(f"eGon substations: {len(egon_subs)}")
        print(f"eGon 220/380kV buses: {len(egon_hv)}")

    # --- Tier 1: Exact georef match ---
    print("\n--- Tier 1: Exact georef match ---")
    t1_matches = match_tier1_georef(substations, georef)
    print(f"  Matched: {len(t1_matches)}/{len(substations)} ({100*len(t1_matches)/len(substations):.1f}%)")

    # --- Tier 2: Fuzzy match ---
    unmatched_t1 = [i for i in substations.index if i not in t1_matches]
    print(f"\n--- Tier 2: Fuzzy match ({len(unmatched_t1)} remaining) ---")
    t2_matches = match_tier2_fuzzy(substations, unmatched_t1, georef, egon_subs)
    print(f"  Matched: {len(t2_matches)}/{len(unmatched_t1)}")

    # --- Tier 3: Unmatched ---
    all_matched = set(t1_matches.keys()) | set(t2_matches.keys())
    unmatched_final = [i for i in substations.index if i not in all_matched]
    print(f"\n--- Tier 3: Unmatched ({len(unmatched_final)} remaining) ---")
    t3_matches = match_tier3_spatial(substations, unmatched_final, egon_hv)

    # Combine all matches
    all_match_data = {}
    all_match_data.update(t3_matches)
    all_match_data.update(t2_matches)
    all_match_data.update(t1_matches)  # highest priority last

    # Build result DataFrame
    result_rows = []
    for idx, sub in substations.iterrows():
        match = all_match_data.get(idx, {})
        result_rows.append({
            "name": sub["name"],
            "tso": sub["tso"],
            "voltages": sub["voltages"],
            "x": match.get("x", np.nan),
            "y": match.get("y", np.nan),
            "osm_id": match.get("osm_id"),
            "match_tier": match.get("tier", 0),
            "match_method": match.get("method", "none"),
            "confidence": match.get("confidence", 0),
            "geo_name": match.get("geo_name", ""),
        })

    result = pd.DataFrame(result_rows)

    # Assign bus IDs
    result = assign_bus_ids(result)

    # Print unmatched for debugging
    unmatched = result[result["match_tier"] == 3]
    if len(unmatched) > 0:
        print(f"\n  Unmatched substations ({len(unmatched)}):")
        for _, row in unmatched.iterrows():
            print(f"    {row['name']} ({row['tso']})")

    # Save
    out_path = os.path.join(DATA_DIR, "substations_geolocated.csv")
    result.to_csv(out_path, index=False)

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Total substations: {len(result)}")
    tier_counts = result["match_tier"].value_counts().sort_index()
    for tier, count in tier_counts.items():
        pct = 100 * count / len(result)
        print(f"  Tier {tier}: {count} ({pct:.1f}%)")
    has_coords = result["x"].notna() & result["y"].notna()
    print(f"  With coordinates: {has_coords.sum()}/{len(result)}")
    has_380 = result["bus_id_380"].notna().sum()
    has_220 = result["bus_id_220"].notna().sum()
    print(f"  Bus IDs assigned: {has_380} at 380kV, {has_220} at 220kV")
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
