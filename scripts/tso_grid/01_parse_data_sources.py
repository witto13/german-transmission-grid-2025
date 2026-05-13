#!/usr/bin/env python3
"""Script 1: Parse and normalize all TSO data sources into clean CSVs.

Parses:
  1a. JAO 8th release XLSX (Lines, Tielines, Transformers)
  1b. 50Hertz individual dataset (download + parse)
  1c. TenneT individual dataset (download + parse)
  1d. Merge — pick most complete source per TSO zone

Output:
  data/tso_grid/lines.csv
  data/tso_grid/tielines_crossborder.csv
  data/tso_grid/transformers.csv
  data/tso_grid/substations_raw.csv
"""

import argparse
import math
import os
import sys
import tempfile
import urllib.request

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JAO_XLSX = os.path.join(
    PROJECT_DIR,
    "data/jao/For publication/202509_Core Static Grid Model_for publication.xlsx",
)
OUT_DIR = os.path.join(PROJECT_DIR, "data/tso_grid")

DE_TSOS = {"50HERTZ", "TENNETGMBH", "AMPRION GMBH", "TRANSNETBW"}

# Voltage normalization: JAO uses 400/410 for 380kV, 231/240 for 220kV
VOLTAGE_MAP = {400: 380, 410: 380, 231: 220, 240: 220}

# Default Imax (A) when no value provided
DEFAULT_IMAX = {380: 2000, 220: 1200}

# TSO name normalization
TSO_NORM = {
    "50HERTZ": "50Hertz",
    "TENNETGMBH": "TenneT",
    "AMPRION GMBH": "Amprion",
    "TRANSNETBW": "TransnetBW",
}

# URLs for individual TSO datasets
URL_50HERTZ = (
    "https://www.50hertz.com/xspProxy/api/StaticFiles/50Hertz-Client/Images/"
    "TRANSPARENZ/Netzmodell_de/"
    "Statisches%20Netzmodell%20ogd%20-%20Datentabelle%202024.xlsx"
)
URL_TENNET = (
    "https://tennet-drupal.s3.eu-central-1.amazonaws.com/default/2025-04/"
    "D2_Core%20Static%20Grid%20Model_Mar%202025.xlsx"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def norm_voltage(v):
    """Normalize voltage to standard kV (380, 220, 110)."""
    try:
        v = int(float(v))
    except (ValueError, TypeError):
        return None
    return VOLTAGE_MAP.get(v, v)


def norm_tso(tso):
    """Normalize TSO name."""
    return TSO_NORM.get(tso, tso)


def safe_float(val):
    """Convert value to float, returning NaN for non-numeric."""
    if val is None:
        return np.nan
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("\xa0", "").replace(",", ".")
    if not s or s == "-":
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


def best_imax(row):
    """Get best Imax from period/fixed columns. Returns Amps."""
    # Try Fixed first
    fixed = safe_float(row.get("imax_fixed"))
    if not np.isnan(fixed) and fixed > 0:
        return fixed
    # Try max of period values
    period_vals = []
    for i in range(1, 7):
        v = safe_float(row.get(f"imax_period_{i}"))
        if not np.isnan(v) and v > 0:
            period_vals.append(v)
    if period_vals:
        return max(period_vals)
    return np.nan


def compute_s_nom(imax_a, v_kv):
    """Compute 3-phase s_nom in MVA from Imax (A) and voltage (kV)."""
    if np.isnan(imax_a) or imax_a <= 0 or v_kv <= 0:
        return np.nan
    return math.sqrt(3) * v_kv * imax_a / 1000.0


def extract_substation_name(full_name, position):
    """Extract substation name from NE_name like 'SubA - SubB 123'.

    position: 0 for first substation, 1 for second.
    """
    if not full_name or pd.isna(full_name):
        return None
    # NE_name format: "SubA - SubB 123" or full_name columns exist
    return str(full_name).strip()


# ---------------------------------------------------------------------------
# 1a: Parse JAO XLSX
# ---------------------------------------------------------------------------
def parse_jao_xlsx(xlsx_path):
    """Parse Lines, Tielines, Transformers from JAO 8th release."""
    print("=" * 70)
    print("STEP 1a: Parsing JAO 8th release XLSX")
    print("=" * 70)

    # --- Lines sheet ---
    df_lines_raw = pd.read_excel(xlsx_path, sheet_name="Lines", header=1)
    # Rename columns to internal names.
    # pandas auto-deduplicates "Full_name" → "Full_name", "Full_name.1"
    lines_cols = {
        "NE_name": "ne_name",
        "EIC_Code": "eic_code",
        "TSO": "tso",
        "Voltage_level(kV)": "v_nom_raw",
        "Full_name": "sub1",
        "Full_name.1": "sub2",
        "Period 1": "imax_period_1",
        "Period 2": "imax_period_2",
        "Period 3": "imax_period_3",
        "Period 4": "imax_period_4",
        "Period 5": "imax_period_5",
        "Period 6": "imax_period_6",
        "Fixed": "imax_fixed",
        "DLRmin(A)": "dlr_min",
        "DLRmax(A)": "dlr_max",
    }
    # Handle Ω/μ chars which may vary between systems
    for c in df_lines_raw.columns:
        cl = str(c).lower()
        if "resistance" in cl:
            lines_cols[c] = "r_ohm"
        elif "reactance" in cl:
            lines_cols[c] = "x_ohm"
        elif "susceptance" in cl:
            lines_cols[c] = "b_us"
        elif "length" in cl:
            lines_cols[c] = "length_km"
        elif c == "Comment":
            lines_cols[c] = "comment"

    df_lines_raw = df_lines_raw.rename(columns=lines_cols)
    # Keep only mapped columns
    keep = [v for v in lines_cols.values() if v in df_lines_raw.columns]
    df_lines = df_lines_raw[keep].copy()
    # Drop empty rows
    df_lines = df_lines.dropna(subset=["ne_name"])

    # Filter to German TSOs
    df_lines = df_lines[df_lines["tso"].isin(DE_TSOS)].copy()

    # Normalize voltage
    df_lines["v_nom"] = df_lines["v_nom_raw"].apply(norm_voltage)
    df_lines = df_lines[df_lines["v_nom"].isin([220, 380])].copy()

    # Compute electrical params
    for col in ["r_ohm", "x_ohm", "b_us", "length_km"]:
        df_lines[col] = df_lines[col].apply(safe_float)

    # Imax → s_nom
    df_lines["imax_a"] = df_lines.apply(best_imax, axis=1)
    # Fill missing Imax with defaults
    mask_nan = df_lines["imax_a"].isna()
    df_lines.loc[mask_nan, "imax_a"] = df_lines.loc[mask_nan, "v_nom"].map(DEFAULT_IMAX)
    df_lines["s_nom"] = df_lines.apply(
        lambda r: compute_s_nom(r["imax_a"], r["v_nom"]), axis=1
    )

    # Normalize TSO
    df_lines["tso_norm"] = df_lines["tso"].map(norm_tso)

    # Extract substation names if present
    if "sub1" not in df_lines.columns:
        # Parse from ne_name: "SubA - SubB 123"
        parts = df_lines["ne_name"].str.extract(r"^(.+?)\s*-\s*(.+?)\s+\d+")
        df_lines["sub1"] = parts[0].str.strip()
        df_lines["sub2"] = parts[1].str.strip()

    df_lines["source"] = "JAO"
    df_lines["is_tieline"] = False

    print(f"  Lines: {len(df_lines)} German (220/380kV)")

    # --- Tielines sheet ---
    df_tie_raw = pd.read_excel(xlsx_path, sheet_name="Tielines", header=1)
    # Same column structure as Lines — build rename map the same way
    tie_cols = dict(lines_cols)  # copy the mapping we built for Lines
    for c in df_tie_raw.columns:
        cl = str(c).lower()
        if "resistance" in cl:
            tie_cols[c] = "r_ohm"
        elif "reactance" in cl:
            tie_cols[c] = "x_ohm"
        elif "susceptance" in cl:
            tie_cols[c] = "b_us"
        elif "length" in cl:
            tie_cols[c] = "length_km"
        elif c == "Comment":
            tie_cols[c] = "comment"

    df_tie_raw = df_tie_raw.rename(columns=tie_cols)
    keep_t = [v for v in lines_cols.values() if v in df_tie_raw.columns]
    df_tie = df_tie_raw[keep_t].copy()
    df_tie = df_tie.dropna(subset=["ne_name"])
    df_tie = df_tie[df_tie["tso"].isin(DE_TSOS)].copy()
    df_tie["v_nom"] = df_tie["v_nom_raw"].apply(norm_voltage)
    df_tie = df_tie[df_tie["v_nom"].isin([220, 380])].copy()

    for col in ["r_ohm", "x_ohm", "b_us", "length_km"]:
        df_tie[col] = df_tie[col].apply(safe_float)

    df_tie["imax_a"] = df_tie.apply(best_imax, axis=1)
    mask_nan_t = df_tie["imax_a"].isna()
    df_tie.loc[mask_nan_t, "imax_a"] = df_tie.loc[mask_nan_t, "v_nom"].map(DEFAULT_IMAX)
    df_tie["s_nom"] = df_tie.apply(
        lambda r: compute_s_nom(r["imax_a"], r["v_nom"]), axis=1
    )
    df_tie["tso_norm"] = df_tie["tso"].map(norm_tso)

    if "sub1" not in df_tie.columns:
        parts_t = df_tie["ne_name"].str.extract(r"^(.+?)\s*-\s*(.+?)\s+\d+")
        df_tie["sub1"] = parts_t[0].str.strip()
        df_tie["sub2"] = parts_t[1].str.strip()

    # Classify: internal (inter-TSO within DE) vs cross-border
    comment_str = df_tie["comment"].fillna("")
    df_tie["is_internal_tieline"] = comment_str.str.contains(
        r"Tie-line to DE|German internal", case=False, regex=True
    )
    df_tie["source"] = "JAO"
    df_tie["is_tieline"] = True

    n_internal = df_tie["is_internal_tieline"].sum()
    n_xborder = len(df_tie) - n_internal
    print(f"  Tielines: {len(df_tie)} DE TSO ({n_internal} internal, {n_xborder} cross-border)")

    # Internal tielines go into the lines pool
    df_internal = df_tie[df_tie["is_internal_tieline"]].copy()
    df_crossborder = df_tie[~df_tie["is_internal_tieline"]].copy()

    # Merge internal tielines with lines
    all_lines = pd.concat([df_lines, df_internal], ignore_index=True)
    print(f"  Total internal lines (lines + internal tielines): {len(all_lines)}")

    # --- Transformers sheet ---
    df_tr_raw = pd.read_excel(xlsx_path, sheet_name="Transformers", header=1)
    tr_cols = {
        "Full Name": "full_name",
        "EIC_Code": "eic_code",
        "TSO": "tso",
        "Min": "imax_min",
        "Max": "imax_max",
        "Fixed": "imax_fixed",
        "Primary": "v_primary_raw",
        "Secondary": "v_secondary_raw",
        "Taps used for RAO": "taps_rao",
        "Symmetrical/Asymmetrical": "symmetry",
        "Comment": "comment",
    }
    # Handle special-character columns dynamically
    for c in df_tr_raw.columns:
        cl = str(c).lower()
        if "resistance" in cl:
            tr_cols[c] = "r_ohm"
        elif "reactance" in cl:
            tr_cols[c] = "x_ohm"
        elif "susceptance" in cl:
            tr_cols[c] = "b_us"
        elif "conductance" in cl:
            tr_cols[c] = "g_us"
        elif "theta" in cl:
            tr_cols[c] = "theta_deg"
        elif "phase regulation" in cl:
            tr_cols[c] = "phase_reg_pct"
        elif "angle regulation" in cl:
            tr_cols[c] = "angle_reg_pct"
    df_tr_raw = df_tr_raw.rename(columns=tr_cols)
    keep_tr = [v for v in tr_cols.values() if v in df_tr_raw.columns]
    df_tr = df_tr_raw[keep_tr].copy()
    df_tr = df_tr.dropna(subset=["full_name"])
    df_tr = df_tr[df_tr["tso"].isin(DE_TSOS)].copy()

    # Normalize voltages
    df_tr["v_primary"] = df_tr["v_primary_raw"].apply(norm_voltage)
    df_tr["v_secondary"] = df_tr["v_secondary_raw"].apply(norm_voltage)

    # Ensure bus0 = higher voltage, bus1 = lower voltage
    df_tr["v_high"] = df_tr[["v_primary", "v_secondary"]].max(axis=1)
    df_tr["v_low"] = df_tr[["v_primary", "v_secondary"]].min(axis=1)

    # Keep transformers with at least one side at 220 or 380 kV
    df_tr = df_tr[
        df_tr["v_high"].isin([220, 380]) | df_tr["v_low"].isin([220, 380])
    ].copy()

    # Same-voltage units are phase-shifting transformers — keep them
    df_tr["is_pst"] = df_tr["v_high"] == df_tr["v_low"]

    for col in ["r_ohm", "x_ohm", "b_us", "g_us"]:
        df_tr[col] = df_tr[col].apply(safe_float)
    for col in ["imax_min", "imax_max", "imax_fixed"]:
        df_tr[col] = df_tr[col].apply(safe_float)

    # Best Imax for trafos
    df_tr["imax_a"] = df_tr["imax_fixed"].copy()
    mask_no_fixed = df_tr["imax_a"].isna() | (df_tr["imax_a"] <= 0)
    df_tr.loc[mask_no_fixed, "imax_a"] = df_tr.loc[mask_no_fixed, "imax_max"]
    mask_still_na = df_tr["imax_a"].isna() | (df_tr["imax_a"] <= 0)
    df_tr.loc[mask_still_na, "imax_a"] = df_tr.loc[mask_still_na, "imax_min"]

    # s_nom based on primary (higher) voltage
    df_tr["s_nom"] = df_tr.apply(
        lambda r: compute_s_nom(r["imax_a"], r["v_high"]), axis=1
    )

    # Parse substation from full_name: "TR SubName V1/V2 123"
    name_parts = df_tr["full_name"].str.extract(
        r"^(?:TR|PST)\s+(.+?)\s+\d+(?:/\d+)?\s*$"
    )
    if name_parts[0].isna().all():
        # Try simpler pattern: "TR SubName VV1/VV2 NNN"
        name_parts = df_tr["full_name"].str.extract(
            r"^(?:TR|PST)\s+(.+?)(?:\s+\d+/\d+|\s+\d+)\s*$"
        )
    df_tr["substation"] = name_parts[0].str.strip()

    # Fallback: strip TR/PST prefix and trailing numbers
    mask_empty = df_tr["substation"].isna()
    df_tr.loc[mask_empty, "substation"] = (
        df_tr.loc[mask_empty, "full_name"]
        .str.replace(r"^(TR|PST)\s+", "", regex=True)
        .str.replace(r"\s+\d+(/\d+)?\s*$", "", regex=True)
        .str.strip()
    )
    # Remove voltage pattern from substation name (e.g., "220/400")
    df_tr["substation"] = df_tr["substation"].str.replace(
        r"\s*\d{2,3}/\d{2,3}\s*", " ", regex=True
    ).str.strip()

    df_tr["theta_deg"] = df_tr["theta_deg"].apply(safe_float)
    df_tr["tso_norm"] = df_tr["tso"].map(norm_tso)
    df_tr["source"] = "JAO"

    print(f"  Transformers: {len(df_tr)} German (HV/eHV)")
    by_vpair = df_tr.groupby(["v_high", "v_low"]).size()
    for (vh, vl), cnt in by_vpair.items():
        print(f"    {int(vh)}/{int(vl)} kV: {cnt}")

    # --- Extract substations ---
    subs = set()
    for _, row in all_lines.iterrows():
        if pd.notna(row.get("sub1")):
            subs.add((row["sub1"], row["tso_norm"], row["v_nom"]))
        if pd.notna(row.get("sub2")):
            subs.add((row["sub2"], row["tso_norm"], row["v_nom"]))
    for _, row in df_crossborder.iterrows():
        if pd.notna(row.get("sub1")):
            subs.add((row["sub1"], row["tso_norm"], row["v_nom"]))
        # sub2 might be foreign — still include for completeness
        if pd.notna(row.get("sub2")):
            subs.add((row["sub2"], row["tso_norm"], row["v_nom"]))
    for _, row in df_tr.iterrows():
        if pd.notna(row.get("substation")):
            subs.add((row["substation"], row["tso_norm"], int(row["v_high"])))
            subs.add((row["substation"], row["tso_norm"], int(row["v_low"])))

    df_subs = pd.DataFrame(list(subs), columns=["name", "tso", "v_nom"])
    # Aggregate: unique name+tso, collect voltage levels
    df_subs_agg = (
        df_subs.groupby(["name", "tso"])
        .agg(voltages=("v_nom", lambda x: sorted(set(x))))
        .reset_index()
    )
    print(f"  Unique substations: {len(df_subs_agg)}")

    return {
        "lines": all_lines,
        "crossborder": df_crossborder,
        "transformers": df_tr,
        "substations": df_subs_agg,
    }


# ---------------------------------------------------------------------------
# 1b/1c: Download and parse individual TSO datasets
# ---------------------------------------------------------------------------
def download_xlsx(url, label):
    """Download XLSX to temp file, return path."""
    print(f"\n  Downloading {label}...")
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        urllib.request.urlretrieve(url, tmp.name)
        size_kb = os.path.getsize(tmp.name) / 1024
        print(f"    Downloaded {size_kb:.0f} KB → {tmp.name}")
        return tmp.name
    except Exception as e:
        print(f"    FAILED to download {label}: {e}")
        return None


def parse_50hertz_xlsx(path):
    """Parse 50Hertz individual static grid model."""
    print("\n--- Parsing 50Hertz dataset ---")
    try:
        xl = pd.ExcelFile(path)
        print(f"  Sheets: {xl.sheet_names}")
    except Exception as e:
        print(f"  ERROR reading file: {e}")
        return None

    result = {"lines": None, "transformers": None}

    # Try to find lines/transformers sheets
    for sheet in xl.sheet_names:
        sl = sheet.lower()
        # Read with header detection
        try:
            df = pd.read_excel(path, sheet_name=sheet, header=1)
        except Exception:
            df = pd.read_excel(path, sheet_name=sheet, header=0)

        cols_lower = [str(c).lower() for c in df.columns]
        print(f"  Sheet '{sheet}': {len(df)} rows, cols={list(df.columns)[:8]}...")

        if "line" in sl or "leitun" in sl:
            result["lines"] = df
        elif "transform" in sl or "trafo" in sl:
            result["transformers"] = df

    return result


def parse_tennet_xlsx(path):
    """Parse TenneT individual static grid model."""
    print("\n--- Parsing TenneT dataset ---")
    try:
        xl = pd.ExcelFile(path)
        print(f"  Sheets: {xl.sheet_names}")
    except Exception as e:
        print(f"  ERROR reading file: {e}")
        return None

    result = {"lines": None, "transformers": None}

    for sheet in xl.sheet_names:
        sl = sheet.lower()
        try:
            df = pd.read_excel(path, sheet_name=sheet, header=1)
        except Exception:
            df = pd.read_excel(path, sheet_name=sheet, header=0)

        cols_lower = [str(c).lower() for c in df.columns]
        print(f"  Sheet '{sheet}': {len(df)} rows, cols={list(df.columns)[:8]}...")

        if "line" in sl and "tie" not in sl:
            result["lines"] = df
        elif "tie" in sl:
            # TenneT might have tielines separate
            pass
        elif "transform" in sl or "trafo" in sl:
            result["transformers"] = df

    return result


def evaluate_completeness(df, label):
    """Evaluate parameter completeness of a line/trafo DataFrame."""
    if df is None or df.empty:
        return {"label": label, "count": 0, "r_fill": 0, "x_fill": 0, "imax_fill": 0}

    n = len(df)
    cols_lower = {str(c).lower(): c for c in df.columns}

    def find_col(patterns):
        for p in patterns:
            for cl, orig in cols_lower.items():
                if p in cl:
                    return orig
        return None

    r_col = find_col(["resistance", "r(", "r_ohm", "r ("])
    x_col = find_col(["reactance", "x(", "x_ohm", "x ("])
    imax_col = find_col(["imax", "current", "fixed"])

    r_fill = df[r_col].notna().sum() / n if r_col and n > 0 else 0
    x_fill = df[x_col].notna().sum() / n if x_col and n > 0 else 0
    imax_fill = df[imax_col].notna().sum() / n if imax_col and n > 0 else 0

    return {
        "label": label,
        "count": n,
        "r_fill": r_fill,
        "x_fill": x_fill,
        "imax_fill": imax_fill,
    }


def compare_sources(jao_lines, tso_data, tso_name):
    """Compare JAO vs individual TSO dataset for a zone."""
    # Filter JAO to this TSO zone
    jao_zone = jao_lines[jao_lines["tso_norm"] == tso_name]
    jao_stats = {
        "label": f"JAO ({tso_name})",
        "count": len(jao_zone),
        "r_fill": jao_zone["r_ohm"].notna().mean() if len(jao_zone) > 0 else 0,
        "x_fill": jao_zone["x_ohm"].notna().mean() if len(jao_zone) > 0 else 0,
        "imax_fill": jao_zone["imax_a"].notna().mean() if len(jao_zone) > 0 else 0,
    }

    if tso_data and tso_data.get("lines") is not None:
        ind_stats = evaluate_completeness(tso_data["lines"], f"Individual ({tso_name})")
    else:
        ind_stats = {"label": f"Individual ({tso_name})", "count": 0,
                     "r_fill": 0, "x_fill": 0, "imax_fill": 0}

    print(f"\n  === {tso_name} Zone Comparison ===")
    print(f"  {'Source':<25} {'Lines':>6} {'R fill':>8} {'X fill':>8} {'Imax fill':>10}")
    print(f"  {'-'*60}")
    for s in [jao_stats, ind_stats]:
        print(f"  {s['label']:<25} {s['count']:>6} {s['r_fill']:>7.1%} {s['x_fill']:>7.1%} {s['imax_fill']:>9.1%}")

    # Decision: use JAO unless individual has strictly more lines AND better fill rates
    use_individual = (
        ind_stats["count"] > jao_stats["count"] * 1.1
        and ind_stats["r_fill"] >= jao_stats["r_fill"]
        and ind_stats["x_fill"] >= jao_stats["x_fill"]
    )

    winner = f"Individual ({tso_name})" if use_individual else f"JAO"
    print(f"  → Winner: {winner}")
    return use_individual


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Parse TSO data sources")
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip downloading 50Hertz/TenneT datasets",
    )
    parser.add_argument(
        "--50hertz-xlsx",
        dest="hertz50_xlsx",
        help="Path to already-downloaded 50Hertz XLSX",
    )
    parser.add_argument(
        "--tennet-xlsx",
        help="Path to already-downloaded TenneT XLSX",
    )
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    # --- 1a: Parse JAO ---
    jao = parse_jao_xlsx(JAO_XLSX)

    # --- 1b: 50Hertz ---
    print("\n" + "=" * 70)
    print("STEP 1b: 50Hertz individual dataset")
    print("=" * 70)
    hertz50_data = None
    if not args.skip_download:
        hertz50_path = args.hertz50_xlsx or download_xlsx(URL_50HERTZ, "50Hertz")
        if hertz50_path:
            hertz50_data = parse_50hertz_xlsx(hertz50_path)
    else:
        print("  Skipped (--skip-download)")

    # --- 1c: TenneT ---
    print("\n" + "=" * 70)
    print("STEP 1c: TenneT individual dataset")
    print("=" * 70)
    tennet_data = None
    if not args.skip_download:
        tennet_path = args.tennet_xlsx or download_xlsx(URL_TENNET, "TenneT")
        if tennet_path:
            tennet_data = parse_tennet_xlsx(tennet_path)
    else:
        print("  Skipped (--skip-download)")

    # --- 1d: Merge ---
    print("\n" + "=" * 70)
    print("STEP 1d: Merge — pick best source per TSO zone")
    print("=" * 70)

    print("\n  Amprion: always JAO (no individual dataset)")
    print("  TransnetBW: always JAO (no individual dataset)")

    use_50h_individual = compare_sources(jao["lines"], hertz50_data, "50Hertz")
    use_tnt_individual = compare_sources(jao["lines"], tennet_data, "TenneT")

    # For now, we always use JAO — the individual datasets follow the same
    # CORE-TSO format and rarely provide additional lines.
    if use_50h_individual:
        print("\n  NOTE: Would use 50Hertz individual dataset, but merging logic "
              "not yet implemented. Falling back to JAO.")
    if use_tnt_individual:
        print("\n  NOTE: Would use TenneT individual dataset, but merging logic "
              "not yet implemented. Falling back to JAO.")

    # Final output is always JAO for this version
    final_lines = jao["lines"]
    final_xborder = jao["crossborder"]
    final_trafos = jao["transformers"]
    final_subs = jao["substations"]

    # --- Save outputs ---
    print("\n" + "=" * 70)
    print("Saving outputs")
    print("=" * 70)

    lines_path = os.path.join(OUT_DIR, "lines.csv")
    final_lines.to_csv(lines_path, index=False)
    print(f"  {lines_path}: {len(final_lines)} lines")

    xb_path = os.path.join(OUT_DIR, "tielines_crossborder.csv")
    final_xborder.to_csv(xb_path, index=False)
    print(f"  {xb_path}: {len(final_xborder)} cross-border tielines")

    tr_path = os.path.join(OUT_DIR, "transformers.csv")
    final_trafos.to_csv(tr_path, index=False)
    print(f"  {tr_path}: {len(final_trafos)} transformers")

    subs_path = os.path.join(OUT_DIR, "substations_raw.csv")
    final_subs.to_csv(subs_path, index=False)
    print(f"  {subs_path}: {len(final_subs)} unique substations")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Lines:               {len(final_lines)}")
    by_tso = final_lines.groupby("tso_norm").size()
    for tso, cnt in sorted(by_tso.items()):
        print(f"    {tso}: {cnt}")
    by_v = final_lines.groupby("v_nom").size()
    for v, cnt in sorted(by_v.items()):
        print(f"    {int(v)} kV: {cnt}")
    print(f"  Cross-border tielines: {len(final_xborder)}")
    print(f"  Transformers:         {len(final_trafos)}")
    print(f"  Substations:          {len(final_subs)}")

    # Parameter quality
    for label, df, cols in [
        ("Lines", final_lines, ["r_ohm", "x_ohm", "b_us", "length_km", "s_nom"]),
        ("Transformers", final_trafos, ["r_ohm", "x_ohm", "s_nom"]),
    ]:
        print(f"\n  {label} parameter fill rates:")
        for col in cols:
            if col in df.columns:
                fill = df[col].notna().mean()
                print(f"    {col}: {fill:.1%}")


if __name__ == "__main__":
    main()
