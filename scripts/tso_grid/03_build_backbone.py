#!/usr/bin/env python3
"""Script 3: Build 220/380kV network as PyPSA-compatible DataFrames.

Creates buses, lines, transformers, and HVDC links from parsed TSO data.

Input:
  data/tso_grid/substations_geolocated.csv (from Script 2)
  data/tso_grid/lines.csv (from Script 1)
  data/tso_grid/tielines_crossborder.csv (from Script 1)
  data/tso_grid/transformers.csv (from Script 1)

Output:
  data/tso_grid/pypsa/buses.csv
  data/tso_grid/pypsa/lines.csv
  data/tso_grid/pypsa/transformers.csv
  data/tso_grid/pypsa/links.csv
"""

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(PROJECT_DIR, "data/tso_grid")
PYPSA_DIR = os.path.join(DATA_DIR, "pypsa")

# Line IDs start from 100001 (eGon max ~33k)
LINE_ID_START = 100001
# Trafo IDs start from 50001
TRAFO_ID_START = 50001
# HVDC foreign bus IDs
HVDC_BUS_START = 200001

# HVDC link definitions (from apply_jao_params.py)
HVDC_LINKS = [
    {
        "name": "ALEGRO",
        "p_nom": 1000.0,
        "de_sub": "Oberzier",
        "de_voltage": 380,
        "foreign_bus_id": HVDC_BUS_START,
        "foreign_country": "BE",
        "foreign_x": 6.0825,
        "foreign_y": 50.7375,
        "foreign_v_nom": 380,
        "length": 90.0,
    },
    {
        "name": "NordLink",
        "p_nom": 1400.0,
        "de_sub": "Wilster/W",  # JAO name
        "de_voltage": 380,
        "foreign_bus_id": HVDC_BUS_START + 1,
        "foreign_country": "NO",
        "foreign_x": 6.567,
        "foreign_y": 58.362,
        "foreign_v_nom": 380,
        "length": 623.0,
    },
    {
        "name": "Baltic Cable",
        "p_nom": 600.0,
        "de_sub": "Siems T421",  # 380kV near Lübeck/Herrenwyk
        "de_voltage": 380,
        "foreign_bus_id": HVDC_BUS_START + 2,
        "foreign_country": "SE",
        "foreign_x": 13.157,
        "foreign_y": 55.377,
        "foreign_v_nom": 380,
        "length": 262.0,
    },
]


def load_data():
    """Load substations, lines, and transformers from Script 1+2 outputs."""
    subs = pd.read_csv(os.path.join(DATA_DIR, "substations_geolocated.csv"))
    lines = pd.read_csv(os.path.join(DATA_DIR, "lines.csv"))
    xborder = pd.read_csv(os.path.join(DATA_DIR, "tielines_crossborder.csv"))
    trafos = pd.read_csv(os.path.join(DATA_DIR, "transformers.csv"))
    return subs, lines, xborder, trafos


def build_sub_lookup(subs):
    """Build name+tso → bus_id lookup for 380 and 220 kV."""
    lookup = {}  # (name, tso, v_nom) → bus_id
    for _, row in subs.iterrows():
        voltages = eval(row["voltages"]) if isinstance(row["voltages"], str) else row["voltages"]
        name = str(row["name"]).strip()
        tso = row["tso"]

        if 380 in voltages and pd.notna(row.get("bus_id_380")):
            lookup[(name, tso, 380)] = int(row["bus_id_380"])
        if 220 in voltages and pd.notna(row.get("bus_id_220")):
            lookup[(name, tso, 220)] = int(row["bus_id_220"])
    return lookup


def resolve_bus(name, tso, v_nom, lookup, subs):
    """Resolve a substation name to bus_id, with fallback search."""
    if not name or pd.isna(name):
        return None

    name = str(name).strip()

    # Exact key match
    key = (name, tso, v_nom)
    if key in lookup:
        return lookup[key]

    # Try any TSO at same voltage
    for (n, t, v), bid in lookup.items():
        if n == name and v == v_nom:
            return bid

    # Try this name at any voltage (for cross-border where voltage might differ)
    for (n, t, v), bid in lookup.items():
        if n == name:
            return bid

    return None


def build_buses(subs):
    """Build PyPSA bus DataFrame — one bus per substation per voltage level."""
    print("\n--- Building buses ---")
    buses = []

    for _, row in subs.iterrows():
        voltages = eval(row["voltages"]) if isinstance(row["voltages"], str) else row["voltages"]
        x = row["x"]
        y = row["y"]

        if 380 in voltages and pd.notna(row.get("bus_id_380")):
            buses.append({
                "bus_id": int(row["bus_id_380"]),
                "v_nom": 380,
                "x": x,
                "y": y,
                "country": "DE",
                "carrier": "AC",
                "substation": row["name"],
                "tso": row["tso"],
            })
        if 220 in voltages and pd.notna(row.get("bus_id_220")):
            buses.append({
                "bus_id": int(row["bus_id_220"]),
                "v_nom": 220,
                "x": x,
                "y": y,
                "country": "DE",
                "carrier": "AC",
                "substation": row["name"],
                "tso": row["tso"],
            })

    df = pd.DataFrame(buses)
    # Drop rows without coordinates (unmatched substations)
    n_before = len(df)
    df = df.dropna(subset=["x", "y"])
    if n_before > len(df):
        print(f"  Dropped {n_before - len(df)} buses without coordinates")

    print(f"  Total buses: {len(df)}")
    print(f"    380 kV: {(df['v_nom']==380).sum()}")
    print(f"    220 kV: {(df['v_nom']==220).sum()}")
    return df


def build_lines(lines_df, lookup, subs, buses):
    """Build PyPSA line DataFrame from JAO lines."""
    print("\n--- Building lines ---")
    valid_bus_ids = set(buses["bus_id"])
    pypsa_lines = []
    line_id = LINE_ID_START
    skipped = {"no_bus0": 0, "no_bus1": 0, "bad_params": 0}

    for _, row in lines_df.iterrows():
        v_nom = int(row["v_nom"])
        tso = row["tso_norm"]

        bus0 = resolve_bus(row["sub1"], tso, v_nom, lookup, subs)
        bus1 = resolve_bus(row["sub2"], tso, v_nom, lookup, subs)

        if bus0 is None:
            skipped["no_bus0"] += 1
            continue
        if bus1 is None:
            skipped["no_bus1"] += 1
            continue
        if bus0 not in valid_bus_ids or bus1 not in valid_bus_ids:
            skipped["bad_params"] += 1
            continue

        # Electrical parameters — already in physical units (Ohms, uS)
        r = float(row["r_ohm"]) if pd.notna(row["r_ohm"]) else 0.0
        x = float(row["x_ohm"]) if pd.notna(row["x_ohm"]) else 0.01
        # Convert susceptance from μS to S
        b = float(row["b_us"]) * 1e-6 if pd.notna(row["b_us"]) else 0.0
        s_nom = float(row["s_nom"]) if pd.notna(row["s_nom"]) else 0.0
        length = float(row["length_km"]) if pd.notna(row["length_km"]) else 0.0

        # Get coordinates for geometry
        b0 = buses[buses["bus_id"] == bus0].iloc[0] if bus0 in valid_bus_ids else None
        b1 = buses[buses["bus_id"] == bus1].iloc[0] if bus1 in valid_bus_ids else None

        geom = ""
        if b0 is not None and b1 is not None:
            geom = f"LINESTRING ({b0['x']} {b0['y']}, {b1['x']} {b1['y']})"

        pypsa_lines.append({
            "line_id": line_id,
            "bus0": bus0,
            "bus1": bus1,
            "r": r,
            "x": x,
            "b": b,
            "s_nom": s_nom,
            "length": length,
            "v_nom": v_nom,
            "num_parallel": 1,
            "ne_name": row["ne_name"],
            "eic_code": row.get("eic_code", ""),
            "tso": tso,
            "geom": geom,
        })
        line_id += 1

    df = pd.DataFrame(pypsa_lines)
    print(f"  Lines created: {len(df)}")
    if any(v > 0 for v in skipped.values()):
        print(f"  Skipped: {skipped}")
    return df


def build_crossborder_lines(xborder_df, lookup, subs, buses, next_line_id, next_bus_id):
    """Build cross-border tieline connections.

    Foreign endpoints get new buses.
    """
    print("\n--- Building cross-border tielines ---")
    valid_bus_ids = set(buses["bus_id"])
    new_buses = []
    new_lines = []
    line_id = next_line_id
    bus_id = next_bus_id
    skipped = 0

    # Track foreign bus names to avoid duplicates
    foreign_bus_lookup = {}  # (sub_name) → bus_id

    for _, row in xborder_df.iterrows():
        v_nom = int(row["v_nom"])
        tso = row["tso_norm"]

        # DE side: sub1
        bus0 = resolve_bus(row["sub1"], tso, v_nom, lookup, subs)
        if bus0 is None or bus0 not in valid_bus_ids:
            skipped += 1
            continue

        # Foreign side: sub2 — create new bus if needed
        foreign_name = str(row["sub2"]).strip() if pd.notna(row.get("sub2")) else None
        if not foreign_name:
            skipped += 1
            continue

        # Determine foreign country from comment
        comment = str(row.get("comment", "")) if pd.notna(row.get("comment")) else ""
        country = "XX"
        country_patterns = {
            "CZ": "CZ", "PL": "PL", "AT": "AT", "CH": "CH",
            "FR": "FR", "NL": "NL", "DK": "DK", "LU": "LU",
            "BE": "BE", "SE": "SE", "NO": "NO",
        }
        for code in country_patterns:
            if code in comment:
                country = code
                break

        # Create or reuse foreign bus
        fkey = (foreign_name, v_nom)
        if fkey in foreign_bus_lookup:
            bus1 = foreign_bus_lookup[fkey]
        else:
            bus1 = bus_id
            bus_id += 1
            foreign_bus_lookup[fkey] = bus1

            # Get approximate coords from DE side bus + offset
            de_bus = buses[buses["bus_id"] == bus0].iloc[0]
            # Place foreign bus slightly offset (rough approximation)
            new_buses.append({
                "bus_id": bus1,
                "v_nom": v_nom,
                "x": de_bus["x"],  # will be at DE border
                "y": de_bus["y"],
                "country": country,
                "carrier": "AC",
                "substation": foreign_name,
                "tso": "foreign",
            })
            valid_bus_ids.add(bus1)

        r = float(row["r_ohm"]) if pd.notna(row["r_ohm"]) else 0.0
        x = float(row["x_ohm"]) if pd.notna(row["x_ohm"]) else 0.01
        b = float(row["b_us"]) * 1e-6 if pd.notna(row["b_us"]) else 0.0
        s_nom = float(row["s_nom"]) if pd.notna(row["s_nom"]) else 0.0
        length = float(row["length_km"]) if pd.notna(row["length_km"]) else 0.0

        new_lines.append({
            "line_id": line_id,
            "bus0": bus0,
            "bus1": bus1,
            "r": r,
            "x": x,
            "b": b,
            "s_nom": s_nom,
            "length": length,
            "v_nom": v_nom,
            "num_parallel": 1,
            "ne_name": row["ne_name"],
            "eic_code": row.get("eic_code", ""),
            "tso": tso,
            "geom": "",
        })
        line_id += 1

    df_buses = pd.DataFrame(new_buses) if new_buses else pd.DataFrame()
    df_lines = pd.DataFrame(new_lines) if new_lines else pd.DataFrame()

    print(f"  Cross-border lines: {len(df_lines)}")
    print(f"  Foreign buses created: {len(df_buses)}")
    if skipped > 0:
        print(f"  Skipped: {skipped}")

    return df_buses, df_lines, line_id, bus_id


def build_transformers(trafos_df, lookup, subs, buses):
    """Build PyPSA transformer DataFrame."""
    print("\n--- Building transformers ---")
    valid_bus_ids = set(buses["bus_id"])
    pypsa_trafos = []
    trafo_id = TRAFO_ID_START
    skipped = {"no_bus": 0, "same_bus": 0}

    for _, row in trafos_df.iterrows():
        v_high = int(row["v_high"])
        v_low = int(row["v_low"])
        tso = row["tso_norm"]
        sub_name = row["substation"] if pd.notna(row.get("substation")) else None

        if not sub_name:
            skipped["no_bus"] += 1
            continue

        bus0 = resolve_bus(sub_name, tso, v_high, lookup, subs)
        bus1 = resolve_bus(sub_name, tso, v_low, lookup, subs)

        # For PSTs (same voltage), bus0 = bus1 — use a single bus
        if v_high == v_low:
            bus0 = resolve_bus(sub_name, tso, v_high, lookup, subs)
            bus1 = bus0

        if bus0 is None or bus1 is None:
            skipped["no_bus"] += 1
            continue
        if bus0 not in valid_bus_ids or bus1 not in valid_bus_ids:
            skipped["no_bus"] += 1
            continue

        # Electrical parameters — R, X in Ohms at primary voltage
        r_ohm = float(row["r_ohm"]) if pd.notna(row["r_ohm"]) else 0.0
        x_ohm = float(row["x_ohm"]) if pd.notna(row["x_ohm"]) else 0.0
        s_nom = float(row["s_nom"]) if pd.notna(row["s_nom"]) else 0.0

        # Convert from physical Ohms to per-unit: Z_pu = Z_ohm * S_nom / V^2
        if s_nom > 0 and v_high > 0:
            v_base = v_high  # Primary voltage in kV
            z_base = (v_base ** 2) / s_nom  # Ohms
            r_pu = r_ohm / z_base
            x_pu = x_ohm / z_base
        else:
            r_pu = 0.0005
            x_pu = 0.04

        # Phase shift
        theta = float(row["theta_deg"]) if pd.notna(row.get("theta_deg")) else 0.0

        # Tap ratio
        tap_ratio = 1.0

        pypsa_trafos.append({
            "trafo_id": trafo_id,
            "bus0": bus0,
            "bus1": bus1,
            "r": r_pu,
            "x": x_pu,
            "b": 0.0,
            "g": 0.0,
            "s_nom": s_nom,
            "tap_ratio": tap_ratio,
            "phase_shift": theta,
            "v_high": v_high,
            "v_low": v_low,
            "full_name": row["full_name"],
            "eic_code": row.get("eic_code", ""),
            "tso": tso,
            "is_pst": bool(row.get("is_pst", False)),
        })
        trafo_id += 1

    df = pd.DataFrame(pypsa_trafos)
    print(f"  Transformers created: {len(df)}")
    if any(v > 0 for v in skipped.values()):
        print(f"  Skipped: {skipped}")

    # Stats
    if len(df) > 0:
        pst_count = df["is_pst"].sum() if "is_pst" in df.columns else 0
        print(f"    Phase-shifting transformers: {pst_count}")
        print(f"    Regular transformers: {len(df) - pst_count}")
    return df


def build_hvdc_links(subs, buses):
    """Build HVDC link DataFrame + foreign buses."""
    print("\n--- Building HVDC links ---")
    sys.path.insert(0, PROJECT_DIR)
    from scripts.utils.name_matching import normalize_substation_name

    new_buses = []
    links = []
    valid_bus_ids = set(buses["bus_id"])

    for hvdc in HVDC_LINKS:
        # Find DE endpoint bus
        de_name = hvdc["de_sub"]
        de_v = hvdc["de_voltage"]
        de_bus = None

        # Search substations by name — try exact first, then normalized
        de_name_norm = normalize_substation_name(de_name)
        for _, row in subs.iterrows():
            row_name = str(row["name"]).strip()
            row_norm = normalize_substation_name(row_name)
            if row_name == de_name or row_norm == de_name_norm:
                voltages = eval(row["voltages"]) if isinstance(row["voltages"], str) else row["voltages"]
                if de_v in voltages:
                    if de_v == 380 and pd.notna(row.get("bus_id_380")):
                        de_bus = int(row["bus_id_380"])
                    elif de_v == 220 and pd.notna(row.get("bus_id_220")):
                        de_bus = int(row["bus_id_220"])
                    break

        if de_bus is None or de_bus not in valid_bus_ids:
            print(f"  WARNING: Cannot find DE endpoint '{de_name}' for {hvdc['name']}")
            continue

        # Create foreign bus
        fb_id = hvdc["foreign_bus_id"]
        new_buses.append({
            "bus_id": fb_id,
            "v_nom": hvdc["foreign_v_nom"],
            "x": hvdc["foreign_x"],
            "y": hvdc["foreign_y"],
            "country": hvdc["foreign_country"],
            "carrier": "DC",
            "substation": f"{hvdc['name']}_{hvdc['foreign_country']}",
            "tso": "foreign",
        })

        links.append({
            "link_id": len(links),
            "bus0": de_bus,
            "bus1": fb_id,
            "p_nom": hvdc["p_nom"],
            "length": hvdc["length"],
            "carrier": "DC",
            "name": hvdc["name"],
        })

        print(f"  {hvdc['name']}: bus {de_bus} ({de_name}) ↔ bus {fb_id} ({hvdc['foreign_country']}) {hvdc['p_nom']}MW")

    df_buses = pd.DataFrame(new_buses) if new_buses else pd.DataFrame()
    df_links = pd.DataFrame(links) if links else pd.DataFrame()
    return df_buses, df_links


def main():
    parser = argparse.ArgumentParser(description="Build 220/380kV backbone network")
    args = parser.parse_args()

    os.makedirs(PYPSA_DIR, exist_ok=True)

    print("=" * 70)
    print("Script 3: Build 220/380kV backbone")
    print("=" * 70)

    subs, lines_df, xborder_df, trafos_df = load_data()
    print(f"Input: {len(subs)} substations, {len(lines_df)} lines, "
          f"{len(xborder_df)} cross-border, {len(trafos_df)} transformers")

    # Build substation name → bus_id lookup
    lookup = build_sub_lookup(subs)
    print(f"Bus lookup entries: {len(lookup)}")

    # Build buses
    buses = build_buses(subs)

    # Build internal lines
    pypsa_lines = build_lines(lines_df, lookup, subs, buses)

    # Build cross-border lines
    next_line_id = pypsa_lines["line_id"].max() + 1 if len(pypsa_lines) > 0 else LINE_ID_START
    next_bus_id = 400001  # For foreign buses
    xb_buses, xb_lines, next_line_id, next_bus_id = build_crossborder_lines(
        xborder_df, lookup, subs, buses, next_line_id, next_bus_id
    )

    # Build transformers
    all_buses = pd.concat([buses, xb_buses], ignore_index=True) if len(xb_buses) > 0 else buses
    pypsa_trafos = build_transformers(trafos_df, lookup, subs, all_buses)

    # Build HVDC links
    hvdc_buses, hvdc_links = build_hvdc_links(subs, all_buses)

    # Combine all buses
    all_buses_final = pd.concat(
        [b for b in [buses, xb_buses, hvdc_buses] if len(b) > 0],
        ignore_index=True,
    )

    # Combine all lines
    all_lines_final = pd.concat(
        [l for l in [pypsa_lines, xb_lines] if len(l) > 0],
        ignore_index=True,
    )

    # --- Save ---
    print("\n" + "=" * 70)
    print("Saving outputs")
    print("=" * 70)

    buses_path = os.path.join(PYPSA_DIR, "buses.csv")
    all_buses_final.to_csv(buses_path, index=False)
    print(f"  {buses_path}: {len(all_buses_final)} buses")

    lines_path = os.path.join(PYPSA_DIR, "lines.csv")
    all_lines_final.to_csv(lines_path, index=False)
    print(f"  {lines_path}: {len(all_lines_final)} lines")

    trafos_path = os.path.join(PYPSA_DIR, "transformers.csv")
    pypsa_trafos.to_csv(trafos_path, index=False)
    print(f"  {trafos_path}: {len(pypsa_trafos)} transformers")

    links_path = os.path.join(PYPSA_DIR, "links.csv")
    hvdc_links.to_csv(links_path, index=False)
    print(f"  {links_path}: {len(hvdc_links)} links")

    # --- Summary ---
    print("\n" + "=" * 70)
    print("SUMMARY — 220/380kV Backbone")
    print("=" * 70)
    print(f"  Buses:         {len(all_buses_final)}")
    if len(all_buses_final) > 0:
        by_v = all_buses_final.groupby("v_nom").size()
        for v, cnt in sorted(by_v.items()):
            print(f"    {int(v)} kV: {cnt}")
        by_country = all_buses_final.groupby("country").size()
        for c, cnt in sorted(by_country.items()):
            print(f"    {c}: {cnt}")
    print(f"  Lines:         {len(all_lines_final)}")
    if len(all_lines_final) > 0:
        by_v = all_lines_final.groupby("v_nom").size()
        for v, cnt in sorted(by_v.items()):
            print(f"    {int(v)} kV: {cnt}")
    print(f"  Transformers:  {len(pypsa_trafos)}")
    print(f"  HVDC links:    {len(hvdc_links)}")

    # Parameter quality
    if len(all_lines_final) > 0:
        print(f"\n  Line parameter quality:")
        print(f"    r > 0: {(all_lines_final['r'] > 0).sum()}/{len(all_lines_final)}")
        print(f"    x > 0: {(all_lines_final['x'] > 0).sum()}/{len(all_lines_final)}")
        print(f"    s_nom > 0: {(all_lines_final['s_nom'] > 0).sum()}/{len(all_lines_final)}")


if __name__ == "__main__":
    main()
