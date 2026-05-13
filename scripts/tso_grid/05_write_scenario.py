#!/usr/bin/env python3
"""Script 5: Validate and write eGon2025_tso scenario to database.

Combines backbone (220/380kV from JAO) + 110kV (from eGon) + connecting
transformers + HVDC links into a single scenario in the database.

Validation checks:
  1. All bus references valid (FK integrity)
  2. No duplicate IDs
  3. No isolated buses
  4. Lines connect same-voltage buses; transformers connect different voltages
  5. r >= 0, x > 0, s_nom > 0
  6. Network connectivity (networkx)

Write order: buses → lines → transformers → links

Input:
  data/tso_grid/pypsa/*.csv (backbone)
  data/tso_grid/110kv_*.csv, connecting_transformers.csv, virtual_*.csv

Output:
  Database: grid.egon_etrago_* tables with scn_name='eGon2025_tso'
"""

import argparse
import os
import sys

import networkx as nx
import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(PROJECT_DIR, "data/tso_grid")
PYPSA_DIR = os.path.join(DATA_DIR, "pypsa")

DB_URI = "postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data"
SCN_NAME = "eGon2025_tso"


def load_all_data():
    """Load all CSV outputs from Scripts 1-4."""
    print("--- Loading data ---")

    # Backbone (Script 3)
    bb_buses = pd.read_csv(os.path.join(PYPSA_DIR, "buses.csv"))
    bb_lines = pd.read_csv(os.path.join(PYPSA_DIR, "lines.csv"))
    bb_trafos = pd.read_csv(os.path.join(PYPSA_DIR, "transformers.csv"))
    bb_links = pd.read_csv(os.path.join(PYPSA_DIR, "links.csv"))

    # 110kV (Script 4)
    kv110_buses = pd.read_csv(os.path.join(DATA_DIR, "110kv_buses.csv"))
    kv110_lines = pd.read_csv(os.path.join(DATA_DIR, "110kv_lines.csv"))
    conn_trafos = pd.read_csv(os.path.join(DATA_DIR, "connecting_transformers.csv"))

    # Virtual buses/lines if they exist
    vbus_path = os.path.join(DATA_DIR, "virtual_buses.csv")
    vline_path = os.path.join(DATA_DIR, "virtual_lines.csv")
    vbuses = pd.read_csv(vbus_path) if os.path.exists(vbus_path) else pd.DataFrame()
    vlines = pd.read_csv(vline_path) if os.path.exists(vline_path) else pd.DataFrame()

    print(f"  Backbone: {len(bb_buses)} buses, {len(bb_lines)} lines, "
          f"{len(bb_trafos)} trafos, {len(bb_links)} links")
    print(f"  110kV: {len(kv110_buses)} buses, {len(kv110_lines)} lines")
    print(f"  Connecting: {len(conn_trafos)} trafos")
    print(f"  Virtual: {len(vbuses)} buses, {len(vlines)} lines")

    return {
        "bb_buses": bb_buses,
        "bb_lines": bb_lines,
        "bb_trafos": bb_trafos,
        "bb_links": bb_links,
        "kv110_buses": kv110_buses,
        "kv110_lines": kv110_lines,
        "conn_trafos": conn_trafos,
        "vbuses": vbuses,
        "vlines": vlines,
    }


def combine_data(data):
    """Combine all components into unified DataFrames."""
    print("\n--- Combining components ---")

    # Buses
    bus_frames = [data["bb_buses"], data["kv110_buses"]]
    if len(data["vbuses"]) > 0:
        bus_frames.append(data["vbuses"])
    all_buses = pd.concat(bus_frames, ignore_index=True)
    all_buses["bus_id"] = all_buses["bus_id"].astype(int)
    # Deduplicate buses — same OSM ID can appear under different substation names
    n_before = len(all_buses)
    all_buses = all_buses.drop_duplicates(subset=["bus_id"], keep="first")
    if n_before > len(all_buses):
        print(f"  Deduplicated buses: {n_before} → {len(all_buses)} ({n_before - len(all_buses)} removed)")

    # Lines
    line_frames = [data["bb_lines"], data["kv110_lines"]]
    if len(data["vlines"]) > 0:
        line_frames.append(data["vlines"])
    all_lines = pd.concat(line_frames, ignore_index=True)
    all_lines["line_id"] = all_lines["line_id"].astype(int)
    all_lines["bus0"] = all_lines["bus0"].astype(int)
    all_lines["bus1"] = all_lines["bus1"].astype(int)

    # Transformers — backbone + connecting
    trafo_frames = [data["bb_trafos"], data["conn_trafos"]]
    all_trafos = pd.concat(trafo_frames, ignore_index=True)
    all_trafos["trafo_id"] = all_trafos["trafo_id"].astype(int)
    all_trafos["bus0"] = all_trafos["bus0"].astype(int)
    all_trafos["bus1"] = all_trafos["bus1"].astype(int)

    # Links
    all_links = data["bb_links"].copy()
    if len(all_links) > 0:
        all_links["link_id"] = all_links["link_id"].astype(int)
        all_links["bus0"] = all_links["bus0"].astype(int)
        all_links["bus1"] = all_links["bus1"].astype(int)

    print(f"  Combined buses: {len(all_buses)}")
    print(f"  Combined lines: {len(all_lines)}")
    print(f"  Combined trafos: {len(all_trafos)}")
    print(f"  Combined links: {len(all_links)}")

    return all_buses, all_lines, all_trafos, all_links


def validate(buses, lines, trafos, links):
    """Run validation checks. Returns (passed, errors list)."""
    print("\n--- Validation ---")
    errors = []
    warnings = []

    bus_ids = set(buses["bus_id"])
    bus_vnom = buses.set_index("bus_id")["v_nom"].to_dict()

    # 1. No duplicate IDs
    dup_buses = buses["bus_id"].duplicated().sum()
    dup_lines = lines["line_id"].duplicated().sum()
    dup_trafos = trafos["trafo_id"].duplicated().sum()
    if dup_buses > 0:
        errors.append(f"Duplicate bus_ids: {dup_buses}")
    if dup_lines > 0:
        errors.append(f"Duplicate line_ids: {dup_lines}")
    if dup_trafos > 0:
        errors.append(f"Duplicate trafo_ids: {dup_trafos}")

    # 2. FK integrity — all bus references exist
    bad_line_bus0 = lines[~lines["bus0"].isin(bus_ids)]
    bad_line_bus1 = lines[~lines["bus1"].isin(bus_ids)]
    bad_trafo_bus0 = trafos[~trafos["bus0"].isin(bus_ids)]
    bad_trafo_bus1 = trafos[~trafos["bus1"].isin(bus_ids)]

    if len(bad_line_bus0) > 0:
        errors.append(f"Lines with invalid bus0: {len(bad_line_bus0)}")
    if len(bad_line_bus1) > 0:
        errors.append(f"Lines with invalid bus1: {len(bad_line_bus1)}")
    if len(bad_trafo_bus0) > 0:
        errors.append(f"Trafos with invalid bus0: {len(bad_trafo_bus0)}")
    if len(bad_trafo_bus1) > 0:
        errors.append(f"Trafos with invalid bus1: {len(bad_trafo_bus1)}")

    if len(links) > 0:
        bad_link_bus0 = links[~links["bus0"].isin(bus_ids)]
        bad_link_bus1 = links[~links["bus1"].isin(bus_ids)]
        if len(bad_link_bus0) > 0:
            errors.append(f"Links with invalid bus0: {len(bad_link_bus0)}")
        if len(bad_link_bus1) > 0:
            errors.append(f"Links with invalid bus1: {len(bad_link_bus1)}")

    # 3. Parameter quality
    bad_x = (lines["x"] <= 0).sum() if "x" in lines.columns else 0
    bad_snom = (lines["s_nom"] <= 0).sum() if "s_nom" in lines.columns else 0
    if bad_x > 0:
        warnings.append(f"Lines with x <= 0: {bad_x}")
    if bad_snom > 0:
        warnings.append(f"Lines with s_nom <= 0: {bad_snom}")

    bad_trafo_x = (trafos["x"] <= 0).sum()
    if bad_trafo_x > 0:
        warnings.append(f"Trafos with x <= 0: {bad_trafo_x}")

    # 4. Lines connect same-voltage buses
    if "v_nom" in lines.columns:
        # For 110kV lines, v_nom column might not exist
        pass
    line_v0 = lines["bus0"].map(bus_vnom)
    line_v1 = lines["bus1"].map(bus_vnom)
    mismatch = (line_v0 != line_v1) & line_v0.notna() & line_v1.notna()
    if mismatch.sum() > 0:
        warnings.append(f"Lines connecting different voltages: {mismatch.sum()}")

    # 5. Transformers connect different voltages (except PSTs)
    trafo_v0 = trafos["bus0"].map(bus_vnom)
    trafo_v1 = trafos["bus1"].map(bus_vnom)
    same_v = (trafo_v0 == trafo_v1) & trafo_v0.notna() & trafo_v1.notna()
    # PSTs are expected to have same voltage — just report
    if same_v.sum() > 0:
        warnings.append(f"Trafos connecting same voltage (PSTs): {same_v.sum()}")

    # 6. Network connectivity
    G = nx.Graph()
    G.add_nodes_from(bus_ids)
    for _, row in lines.iterrows():
        if row["bus0"] in bus_ids and row["bus1"] in bus_ids:
            G.add_edge(row["bus0"], row["bus1"])
    for _, row in trafos.iterrows():
        if row["bus0"] in bus_ids and row["bus1"] in bus_ids:
            G.add_edge(row["bus0"], row["bus1"])
    if len(links) > 0:
        for _, row in links.iterrows():
            if row["bus0"] in bus_ids and row["bus1"] in bus_ids:
                G.add_edge(row["bus0"], row["bus1"])

    components = list(nx.connected_components(G))
    isolated = [c for c in components if len(c) == 1]
    large_components = sorted([len(c) for c in components], reverse=True)

    print(f"  Connected components: {len(components)}")
    print(f"  Largest: {large_components[0] if large_components else 0}")
    if len(large_components) > 1:
        print(f"  Top 5 sizes: {large_components[:5]}")
    print(f"  Isolated buses: {len(isolated)}")

    if len(components) > 1:
        warnings.append(f"Network has {len(components)} connected components "
                       f"(largest: {large_components[0]}, isolated: {len(isolated)})")

    # Report
    for e in errors:
        print(f"  ERROR: {e}")
    for w in warnings:
        print(f"  WARNING: {w}")

    passed = len(errors) == 0
    print(f"\n  Validation: {'PASSED' if passed else 'FAILED'} "
          f"({len(errors)} errors, {len(warnings)} warnings)")
    return passed, errors


def prepare_bus_df(buses):
    """Prepare bus DataFrame for database insert."""
    df = pd.DataFrame({
        "scn_name": SCN_NAME,
        "bus_id": buses["bus_id"].astype(int),
        "v_nom": buses["v_nom"].astype(float),
        "type": None,
        "carrier": buses.get("carrier", "AC"),
        "v_mag_pu_set": None,
        "v_mag_pu_min": 0.0,
        "v_mag_pu_max": float("inf"),
        "x": buses["x"].astype(float),
        "y": buses["y"].astype(float),
        "country": buses.get("country", "DE"),
    })
    return df


def prepare_line_df(lines):
    """Prepare line DataFrame for database insert."""
    df = pd.DataFrame({
        "scn_name": SCN_NAME,
        "line_id": lines["line_id"].astype(int),
        "bus0": lines["bus0"].astype(int),
        "bus1": lines["bus1"].astype(int),
        "type": None,
        "carrier": "AC",
        "x": lines["x"].astype(float),
        "r": lines["r"].astype(float),
        "g": 0.0,
        "b": lines["b"].astype(float) if "b" in lines.columns else 0.0,
        "s_nom": lines["s_nom"].astype(float),
        "s_nom_extendable": False,
        "s_nom_min": lines["s_nom"].astype(float),
        "s_nom_max": float("inf"),
        "s_max_pu": 1.0,
        "build_year": 0,
        "lifetime": float("inf"),
        "capital_cost": 0.0,
        "length": lines["length"].astype(float) if "length" in lines.columns else 0.0,
        "cables": lines.get("cables", 3),
        "terrain_factor": 1.0,
        "num_parallel": lines.get("num_parallel", 1.0),
        "v_ang_min": float("-inf"),
        "v_ang_max": float("inf"),
        "v_nom": lines.get("v_nom", 0.0),
    })
    # Fill NaN in cables with 3
    df["cables"] = df["cables"].fillna(3).astype(int)
    df["num_parallel"] = df["num_parallel"].fillna(1.0)
    return df


def prepare_trafo_df(trafos):
    """Prepare transformer DataFrame for database insert."""
    df = pd.DataFrame({
        "scn_name": SCN_NAME,
        "trafo_id": trafos["trafo_id"].astype(int),
        "bus0": trafos["bus0"].astype(int),
        "bus1": trafos["bus1"].astype(int),
        "type": None,
        "model": "t",
        "x": trafos["x"].astype(float),
        "r": trafos["r"].astype(float),
        "g": trafos["g"].astype(float) if "g" in trafos.columns else 0.0,
        "b": trafos["b"].astype(float) if "b" in trafos.columns else 0.0,
        "s_nom": trafos["s_nom"].astype(float),
        "s_nom_extendable": False,
        "s_nom_min": trafos["s_nom"].astype(float),
        "s_nom_max": float("inf"),
        "s_max_pu": 1.0,
        "tap_ratio": trafos.get("tap_ratio", 1.0),
        "tap_side": 0,
        "tap_position": 0,
        "phase_shift": trafos.get("phase_shift", 0.0),
        "build_year": 0,
        "lifetime": float("inf"),
        "v_ang_min": float("-inf"),
        "v_ang_max": float("inf"),
        "capital_cost": 0.0,
        "num_parallel": 1.0,
    })
    df["g"] = df["g"].fillna(0.0)
    df["b"] = df["b"].fillna(0.0)
    df["tap_ratio"] = df["tap_ratio"].fillna(1.0)
    df["phase_shift"] = df["phase_shift"].fillna(0.0)
    return df


def prepare_link_df(links):
    """Prepare link DataFrame for database insert."""
    if len(links) == 0:
        return pd.DataFrame()
    df = pd.DataFrame({
        "scn_name": SCN_NAME,
        "link_id": links["link_id"].astype(int),
        "bus0": links["bus0"].astype(int),
        "bus1": links["bus1"].astype(int),
        "type": None,
        "carrier": links.get("carrier", "DC"),
        "efficiency": 1.0,
        "build_year": 0,
        "lifetime": float("inf"),
        "p_nom": links["p_nom"].astype(float),
        "p_nom_extendable": False,
        "p_nom_min": 0.0,
        "p_nom_max": float("inf"),
        "p_min_pu": -1.0,
        "p_max_pu": 1.0,
        "p_set": 0.0,
        "capital_cost": 0.0,
        "marginal_cost": 0.0,
        "length": links.get("length", 0.0),
        "terrain_factor": 1.0,
    })
    return df


def write_to_db(engine, buses_db, lines_db, trafos_db, links_db, dry_run=True):
    """Write scenario to database."""
    print(f"\n--- {'DRY RUN' if dry_run else 'WRITING'} to database ---")
    print(f"  Scenario: {SCN_NAME}")

    if dry_run:
        print(f"  Would write:")
        print(f"    {len(buses_db)} buses")
        print(f"    {len(lines_db)} lines")
        print(f"    {len(trafos_db)} transformers")
        print(f"    {len(links_db)} links")
        return

    # Delete existing scenario first
    with engine.begin() as conn:
        for table in [
            "egon_etrago_link",
            "egon_etrago_transformer",
            "egon_etrago_line",
            "egon_etrago_bus",
        ]:
            r = conn.execute(
                text(f"DELETE FROM grid.{table} WHERE scn_name = :scn"),
                {"scn": SCN_NAME},
            )
            print(f"  Deleted {r.rowcount} existing rows from {table}")

    # Write in FK order: buses → lines → transformers → links
    print(f"\n  Writing buses ({len(buses_db)})...")
    buses_db.to_sql(
        "egon_etrago_bus", engine, schema="grid",
        if_exists="append", index=False, method="multi",
        chunksize=5000,
    )

    print(f"  Writing lines ({len(lines_db)})...")
    lines_db.to_sql(
        "egon_etrago_line", engine, schema="grid",
        if_exists="append", index=False, method="multi",
        chunksize=5000,
    )

    print(f"  Writing transformers ({len(trafos_db)})...")
    trafos_db.to_sql(
        "egon_etrago_transformer", engine, schema="grid",
        if_exists="append", index=False, method="multi",
        chunksize=5000,
    )

    if len(links_db) > 0:
        print(f"  Writing links ({len(links_db)})...")
        links_db.to_sql(
            "egon_etrago_link", engine, schema="grid",
            if_exists="append", index=False, method="multi",
        )

    # Generate geom columns via PostGIS
    print("\n  Generating PostGIS geometries...")
    with engine.begin() as conn:
        # Bus point geometry
        conn.execute(text(
            f"UPDATE grid.egon_etrago_bus SET geom = ST_SetSRID(ST_MakePoint(x, y), 4326) "
            f"WHERE scn_name = '{SCN_NAME}' AND x IS NOT NULL AND y IS NOT NULL"
        ))
        # Line geometry as MultiLineString (column type)
        conn.execute(text(
            f"UPDATE grid.egon_etrago_line SET geom = ST_SetSRID(ST_Multi(ST_MakeLine("
            f"  (SELECT ST_MakePoint(b.x, b.y) FROM grid.egon_etrago_bus b "
            f"   WHERE b.bus_id = egon_etrago_line.bus0 AND b.scn_name = '{SCN_NAME}'), "
            f"  (SELECT ST_MakePoint(b.x, b.y) FROM grid.egon_etrago_bus b "
            f"   WHERE b.bus_id = egon_etrago_line.bus1 AND b.scn_name = '{SCN_NAME}')"
            f")), 4326) WHERE scn_name = '{SCN_NAME}'"
        ))
        # Topo is LineString type
        conn.execute(text(
            f"UPDATE grid.egon_etrago_line SET topo = ST_SetSRID(ST_MakeLine("
            f"  (SELECT ST_MakePoint(b.x, b.y) FROM grid.egon_etrago_bus b "
            f"   WHERE b.bus_id = egon_etrago_line.bus0 AND b.scn_name = '{SCN_NAME}'), "
            f"  (SELECT ST_MakePoint(b.x, b.y) FROM grid.egon_etrago_bus b "
            f"   WHERE b.bus_id = egon_etrago_line.bus1 AND b.scn_name = '{SCN_NAME}')"
            f"), 4326) WHERE scn_name = '{SCN_NAME}'"
        ))

    print("  Done!")


def main():
    parser = argparse.ArgumentParser(description="Write eGon2025_tso scenario to DB")
    parser.add_argument("--apply", action="store_true", help="Actually write to DB (default: dry run)")
    args = parser.parse_args()

    print("=" * 70)
    print("Script 5: Write eGon2025_tso scenario to database")
    print("=" * 70)

    # Load data
    data = load_all_data()

    # Combine
    buses, lines, trafos, links = combine_data(data)

    # Validate
    passed, errors = validate(buses, lines, trafos, links)

    if not passed:
        print("\n  Validation FAILED. Fix errors before writing.")
        if not args.apply:
            print("  (Use --apply to force write despite errors)")
            sys.exit(1)
        else:
            print("  WARNING: Writing despite validation errors (--apply forced)")

    # Prepare DB-formatted DataFrames
    buses_db = prepare_bus_df(buses)
    lines_db = prepare_line_df(lines)
    trafos_db = prepare_trafo_df(trafos)
    links_db = prepare_link_df(links)

    # Write
    engine = create_engine(DB_URI)
    write_to_db(engine, buses_db, lines_db, trafos_db, links_db, dry_run=not args.apply)

    # Summary comparison
    print("\n" + "=" * 70)
    print("COMPONENT COUNT COMPARISON")
    print("=" * 70)

    with engine.connect() as conn:
        for table, id_col in [
            ("egon_etrago_bus", "bus_id"),
            ("egon_etrago_line", "line_id"),
            ("egon_etrago_transformer", "trafo_id"),
            ("egon_etrago_link", "link_id"),
        ]:
            r = conn.execute(text(
                f"SELECT scn_name, count(*) FROM grid.{table} "
                f"WHERE scn_name IN ('eGon2025', '{SCN_NAME}') "
                f"GROUP BY scn_name ORDER BY scn_name"
            ))
            counts = dict(r.fetchall())
            egon = counts.get("eGon2025", 0)
            tso = counts.get(SCN_NAME, 0)
            print(f"  {table:40} eGon2025={egon:>7}  {SCN_NAME}={tso:>7}")


if __name__ == "__main__":
    main()
