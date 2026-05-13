#!/usr/bin/env python3
"""Script 4: Connect eGon 110kV grid to the new 220/380kV backbone.

Copies 110kV buses and lines from eGon2025 scenario, then remaps the
HV-side of connecting transformers from eGon bus_ids to JAO backbone bus_ids.

Transformer remapping tiers:
  Tier 0: eGon ehv_substation OSM_id → georef OSM_id → JAO bus_id
  Tier 1: Spatial match within 5km to nearest JAO bus at same voltage
  Tier 2: Virtual bus fallback — zero-impedance line to nearest JAO bus

Input:
  data/tso_grid/pypsa/buses.csv (backbone from Script 3)
  data/tso_grid/substations_geolocated.csv (from Script 2)
  eGon2025 database (110kV buses, lines, connecting transformers)

Output:
  data/tso_grid/110kv_buses.csv
  data/tso_grid/110kv_lines.csv
  data/tso_grid/connecting_transformers.csv
  data/tso_grid/bus_mapping.csv
"""

import argparse
import os
import sys

import numpy as np
import pandas as pd
from sqlalchemy import create_engine

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_DIR)

from scripts.utils.spatial_matching import SpatialMatcher

DB_URI = "postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data"
DATA_DIR = os.path.join(PROJECT_DIR, "data/tso_grid")
PYPSA_DIR = os.path.join(DATA_DIR, "pypsa")
GEOREF_CSV = os.path.join(PROJECT_DIR, "data/jao_core_tso/georef.csv")

# Transformer default parameters (from apply_jao_params.py)
TRAFO_DEFAULTS = {
    (220, 380): {"x": 0.04, "r": 0.0005},     # Autotransformer
    (110, 220): {"x": 0.12, "r": 0.003},       # Full-winding HV transformer
    (110, 380): {"x": 0.12, "r": 0.003},       # Full-winding HV transformer
}

# Virtual bus / zero-impedance line IDs
VIRTUAL_BUS_START = 600001
VIRTUAL_LINE_START = 900001


def load_egon_110kv(engine):
    """Load 110kV buses and lines from eGon2025."""
    print("\n--- Loading eGon 110kV data ---")

    buses = pd.read_sql(
        "SELECT bus_id, x, y, v_nom, country, carrier "
        "FROM grid.egon_etrago_bus WHERE scn_name = 'eGon2025'",
        engine,
    )
    bus_vnom = buses.set_index("bus_id")["v_nom"].to_dict()

    b110 = buses[buses["v_nom"] == 110].copy()
    print(f"  110kV buses: {len(b110)}")

    lines = pd.read_sql(
        "SELECT line_id, bus0, bus1, r, x, b, s_nom, length, num_parallel, cables "
        "FROM grid.egon_etrago_line WHERE scn_name = 'eGon2025'",
        engine,
    )
    l110 = lines[
        lines["bus0"].map(bus_vnom).fillna(0).eq(110)
        & lines["bus1"].map(bus_vnom).fillna(0).eq(110)
    ].copy()
    print(f"  110kV lines: {len(l110)}")

    # Connecting transformers: bus0=110kV, bus1=220/380kV
    trafos = pd.read_sql(
        "SELECT trafo_id, bus0, bus1, r, x, b, g, s_nom, tap_ratio, phase_shift "
        "FROM grid.egon_etrago_transformer WHERE scn_name = 'eGon2025'",
        engine,
    )
    t_connect = trafos[
        (trafos["bus0"].map(bus_vnom).fillna(0).eq(110))
        & (trafos["bus1"].map(bus_vnom).fillna(0).isin([220, 380]))
    ].copy()
    print(f"  Connecting transformers (110↔220/380): {len(t_connect)}")

    # Add HV-side voltage for each trafo
    t_connect["hv_v_nom"] = t_connect["bus1"].map(bus_vnom)
    t_connect["hv_bus_x"] = t_connect["bus1"].map(buses.set_index("bus_id")["x"])
    t_connect["hv_bus_y"] = t_connect["bus1"].map(buses.set_index("bus_id")["y"])

    return b110, l110, t_connect, buses, bus_vnom


def load_ehv_substations(engine):
    """Load eGon ehv_substation table for OSM ID matching."""
    df = pd.read_sql(
        "SELECT s.bus_id, s.osm_id, s.subst_name, s.lon, s.lat "
        "FROM grid.egon_ehv_substation s "
        "JOIN grid.egon_etrago_bus b ON s.bus_id = b.bus_id "
        "  AND b.scn_name = 'eGon2025'",
        engine,
    )
    return df


def load_backbone_buses():
    """Load the 220/380kV backbone buses from Script 3."""
    buses = pd.read_csv(os.path.join(PYPSA_DIR, "buses.csv"))
    return buses


def load_georef():
    """Load georef.csv for OSM ID matching."""
    geo = pd.read_csv(GEOREF_CSV, index_col=0)
    geo["geo_name"] = geo.index
    geo["osm_id"] = geo["OSM_id"].apply(
        lambda x: int(float(x)) if pd.notna(x) else None
    )
    return geo


def _parse_osm_id(val):
    """Parse numeric OSM ID from various formats."""
    if pd.isna(val) or val is None:
        return None
    s = str(val).strip().lstrip("wnrWNR")
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def remap_transformers(t_connect, ehv_subs, backbone_buses, georef, all_egon_buses):
    """Remap HV-side of connecting transformers to JAO backbone bus_ids.

    Returns: remapped trafos DataFrame, new virtual buses, new virtual lines, mapping report
    """
    print("\n--- Remapping transformer HV endpoints ---")

    # Build OSM ID → JAO bus_id mapping via georef
    # eGon ehv_substation osm_id → georef name → georef osm_id → backbone bus_id
    georef_de = georef[georef["CORE-TSO_country"] == "DE"]
    geo_osm_to_name = {}
    for _, row in georef_de.iterrows():
        if row["osm_id"]:
            geo_osm_to_name[int(row["osm_id"])] = row["geo_name"]

    # backbone buses indexed by bus_id
    bb_by_id = backbone_buses.set_index("bus_id")
    bb_ids_380 = set(backbone_buses[backbone_buses["v_nom"] == 380]["bus_id"])
    bb_ids_220 = set(backbone_buses[backbone_buses["v_nom"] == 220]["bus_id"])

    # Build eGon bus_id → OSM ID map
    egon_bus_to_osm = {}
    for _, row in ehv_subs.iterrows():
        osm = _parse_osm_id(row["osm_id"])
        if osm:
            egon_bus_to_osm[int(row["bus_id"])] = osm

    # Build spatial matcher from backbone buses
    bb_for_spatial = backbone_buses[
        backbone_buses["v_nom"].isin([220, 380])
        & backbone_buses["country"].eq("DE")
        & backbone_buses["x"].notna()
        & backbone_buses["y"].notna()
    ].rename(columns={"x": "lon", "y": "lat"})
    spatial = SpatialMatcher(bb_for_spatial)

    virtual_buses = []
    virtual_lines = []
    mapping = []
    next_vbus = VIRTUAL_BUS_START
    next_vline = VIRTUAL_LINE_START

    remapped_bus1 = []
    remapped_r = []
    remapped_x = []

    for idx, row in t_connect.iterrows():
        egon_hv_bus = int(row["bus1"])
        hv_v = int(row["hv_v_nom"])
        hv_x = row["hv_bus_x"]
        hv_y = row["hv_bus_y"]

        new_bus = None
        method = "unmatched"

        # --- Tier 0: OSM ID match ---
        osm_id = egon_bus_to_osm.get(egon_hv_bus)
        if osm_id and osm_id in geo_osm_to_name:
            # This OSM ID exists in georef — find matching backbone bus
            # The backbone bus_id is derived from georef osm_id
            candidate_380 = osm_id  # bus_id_380 = osm_id
            candidate_220 = osm_id + 500000  # bus_id_220 = osm_id + offset

            if hv_v == 380 and candidate_380 in bb_ids_380:
                new_bus = candidate_380
                method = "osm_id_380"
            elif hv_v == 220 and candidate_220 in bb_ids_220:
                new_bus = candidate_220
                method = "osm_id_220"
            elif hv_v == 380 and candidate_220 in bb_ids_220:
                # Voltage mismatch but OSM match — use anyway
                new_bus = candidate_220
                method = "osm_id_v_fallback"
            elif hv_v == 220 and candidate_380 in bb_ids_380:
                new_bus = candidate_380
                method = "osm_id_v_fallback"

        # --- Tier 1: Spatial match ---
        if new_bus is None and pd.notna(hv_x) and pd.notna(hv_y):
            result = spatial.find_nearest(hv_x, hv_y, hv_v, max_distance_km=5.0)
            if result:
                new_bus = result[0]
                method = f"spatial_{result[1]:.1f}km"
            else:
                # Try any voltage within 5km
                result2 = spatial.find_nearest_any_voltage(hv_x, hv_y, max_distance_km=5.0)
                if result2:
                    new_bus = result2[0]
                    method = f"spatial_anyv_{result2[1]:.1f}km"

        # --- Tier 2: Virtual bus fallback ---
        if new_bus is None:
            # Create a virtual bus at eGon coordinates, connect to nearest backbone
            if pd.notna(hv_x) and pd.notna(hv_y):
                result3 = spatial.find_nearest_any_voltage(hv_x, hv_y, max_distance_km=50.0)
                if result3:
                    nearest_bb = result3[0]
                    vbus = next_vbus
                    next_vbus += 1

                    virtual_buses.append({
                        "bus_id": vbus,
                        "v_nom": hv_v,
                        "x": hv_x,
                        "y": hv_y,
                        "country": "DE",
                        "carrier": "AC",
                        "substation": f"virtual_{egon_hv_bus}",
                        "tso": "virtual",
                    })

                    # Zero-impedance line connecting virtual bus to backbone
                    virtual_lines.append({
                        "line_id": next_vline,
                        "bus0": vbus,
                        "bus1": nearest_bb,
                        "r": 0.0001,
                        "x": 0.001,
                        "b": 0.0,
                        "s_nom": 9999,
                        "length": result3[1],
                        "v_nom": hv_v,
                        "num_parallel": 1,
                        "ne_name": f"virtual_link_{egon_hv_bus}",
                        "eic_code": "",
                        "tso": "virtual",
                        "geom": "",
                    })
                    next_vline += 1

                    new_bus = vbus
                    method = f"virtual_{result3[1]:.1f}km"

        if new_bus is None:
            new_bus = egon_hv_bus  # Keep original — will be caught in validation
            method = "FAILED"

        remapped_bus1.append(new_bus)

        # Apply default transformer parameters
        v_pair = tuple(sorted([110, hv_v]))
        defaults = TRAFO_DEFAULTS.get(v_pair, TRAFO_DEFAULTS.get((110, 380)))
        # Keep eGon s_nom, apply default r/x if current values are zero
        r_val = float(row["r"]) if pd.notna(row["r"]) and float(row["r"]) > 0 else defaults["r"]
        x_val = float(row["x"]) if pd.notna(row["x"]) and float(row["x"]) > 0 else defaults["x"]
        remapped_r.append(r_val)
        remapped_x.append(x_val)

        mapping.append({
            "trafo_id": row["trafo_id"],
            "egon_hv_bus": egon_hv_bus,
            "new_hv_bus": new_bus,
            "hv_v_nom": hv_v,
            "method": method,
        })

    t_connect = t_connect.copy()
    t_connect["bus1"] = remapped_bus1
    t_connect["r"] = remapped_r
    t_connect["x"] = remapped_x

    df_mapping = pd.DataFrame(mapping)

    # Stats
    method_counts = df_mapping["method"].apply(lambda m: m.split("_")[0]).value_counts()
    print(f"  Remapping results:")
    for method, count in method_counts.items():
        print(f"    {method}: {count}")
    failed = (df_mapping["method"] == "FAILED").sum()
    if failed > 0:
        print(f"    WARNING: {failed} transformers could not be remapped!")

    print(f"  Virtual buses: {len(virtual_buses)}")
    print(f"  Virtual lines: {len(virtual_lines)}")

    df_vbuses = pd.DataFrame(virtual_buses) if virtual_buses else pd.DataFrame()
    df_vlines = pd.DataFrame(virtual_lines) if virtual_lines else pd.DataFrame()

    return t_connect, df_vbuses, df_vlines, df_mapping


def main():
    parser = argparse.ArgumentParser(description="Connect 110kV grid to backbone")
    args = parser.parse_args()

    print("=" * 70)
    print("Script 4: Connect 110kV grid to backbone")
    print("=" * 70)

    engine = create_engine(DB_URI)

    # Load data
    b110, l110, t_connect, all_egon_buses, bus_vnom = load_egon_110kv(engine)
    ehv_subs = load_ehv_substations(engine)
    backbone = load_backbone_buses()
    georef = load_georef()

    print(f"\nBackbone buses: {len(backbone)} (from Script 3)")
    print(f"  220kV: {(backbone['v_nom']==220).sum()}, 380kV: {(backbone['v_nom']==380).sum()}")

    # Remap transformers
    t_remapped, vbuses, vlines, mapping = remap_transformers(
        t_connect, ehv_subs, backbone, georef, all_egon_buses
    )

    # Save outputs
    print("\n" + "=" * 70)
    print("Saving outputs")
    print("=" * 70)

    # 110kV buses — add required columns
    b110_out = b110[["bus_id", "x", "y", "v_nom"]].copy()
    b110_out["country"] = "DE"
    b110_out["carrier"] = "AC"
    b110_out["substation"] = ""
    b110_out["tso"] = ""

    path = os.path.join(DATA_DIR, "110kv_buses.csv")
    b110_out.to_csv(path, index=False)
    print(f"  {path}: {len(b110_out)} buses")

    # 110kV lines
    l110_out = l110.copy()
    path = os.path.join(DATA_DIR, "110kv_lines.csv")
    l110_out.to_csv(path, index=False)
    print(f"  {path}: {len(l110_out)} lines")

    # Connecting transformers
    t_out = t_remapped[["trafo_id", "bus0", "bus1", "r", "x", "b", "g", "s_nom",
                         "tap_ratio", "phase_shift"]].copy()
    path = os.path.join(DATA_DIR, "connecting_transformers.csv")
    t_out.to_csv(path, index=False)
    print(f"  {path}: {len(t_out)} transformers")

    # Bus mapping
    path = os.path.join(DATA_DIR, "bus_mapping.csv")
    mapping.to_csv(path, index=False)
    print(f"  {path}: {len(mapping)} entries")

    # Virtual buses and lines (if any)
    if len(vbuses) > 0:
        path = os.path.join(DATA_DIR, "virtual_buses.csv")
        vbuses.to_csv(path, index=False)
        print(f"  {path}: {len(vbuses)} virtual buses")
    if len(vlines) > 0:
        path = os.path.join(DATA_DIR, "virtual_lines.csv")
        vlines.to_csv(path, index=False)
        print(f"  {path}: {len(vlines)} virtual lines")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY — 110kV Connection")
    print("=" * 70)
    print(f"  110kV buses:               {len(b110_out)}")
    print(f"  110kV lines:               {len(l110_out)}")
    print(f"  Connecting transformers:    {len(t_out)}")
    print(f"  Virtual buses:             {len(vbuses)}")
    print(f"  Virtual lines:             {len(vlines)}")

    success = (mapping["method"] != "FAILED").sum()
    print(f"  Remap success:             {success}/{len(mapping)} ({100*success/len(mapping):.1f}%)")


if __name__ == "__main__":
    main()
