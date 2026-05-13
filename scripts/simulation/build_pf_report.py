#!/usr/bin/env python3
"""
build_pf_report.py — Light-themed HTML report for the 8760-h DC power flow.

Reads results/dispatch_8760h_pf.nc (the dispatch netCDF augmented by
run_dcpf_8760h.py with line flows) and produces a self-contained,
filterable, modern HTML at results/dispatch_8760h_pf_report.html.

Sections:
    - Headline KPIs (max loading, mean, hours overloaded, etc.)
    - Annual loading distribution
    - Geographic map of the grid coloured by annual max % of s_nom
    - Top 30 most-loaded lines: pick a line, see its hourly flow
    - System-wide congestion timeline (max loading per hour)
    - All-lines table: filterable + sortable
    - Methodology

Usage:
    conda activate egon2025
    python scripts/simulation/build_pf_report.py
"""

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa
from sqlalchemy import create_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

DEFAULT_NC = Path("/root/egon_2025_project/results/dispatch_8760h_pf.nc")
RESULTS_DIR = Path("/root/egon_2025_project/results")
DEFAULT_PREFIX = "dispatch_pf_v"   # produces dispatch_pf_v1.html, v2.html, …
DB_URL = "postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data"

# Approximate TSO control area by Bundesland (German federal state).
# Real control areas don't strictly follow state borders (Hamburg & Bremen are
# split, etc.) — this is a coarse but useful first-order assignment.
BUNDESLAND_TO_TSO = {
    "Berlin": "50Hertz",
    "Brandenburg": "50Hertz",
    "Mecklenburg-Vorpommern": "50Hertz",
    "Sachsen": "50Hertz",
    "Sachsen-Anhalt": "50Hertz",
    "Thüringen": "50Hertz",
    "Hamburg": "50Hertz/TenneT",
    "Bayern": "TenneT",
    "Niedersachsen": "TenneT",
    "Bremen": "TenneT",
    "Schleswig-Holstein": "TenneT",
    "Hessen": "TenneT/Amprion",
    "Nordrhein-Westfalen": "Amprion",
    "Rheinland-Pfalz": "Amprion",
    "Saarland": "Amprion",
    "Baden-Württemberg": "TransnetBW",
}


def load_bus_regions(bus_ids):
    """Spatial-join each bus to its Bundesland + Kreis via PostGIS.

    Returns DataFrame indexed by bus id (string) with columns:
        state, kreis, kreis_nuts, country, tso
    """
    log.info(f"Spatially joining {len(bus_ids)} buses to Bundesländer + Kreise...")
    engine = create_engine(DB_URL)

    # Pull bus geometries from grid_beta and join to vg250_lan + vg250_krs.
    # Use ST_DWithin with a tiny tolerance to forgive buses on the border.
    sql = """
        WITH b AS (
            SELECT bus_id::text AS bus_id, geom, country
            FROM grid.egon_etrago_bus
            WHERE scn_name = 'grid_beta'
        )
        SELECT
            b.bus_id,
            b.country,
            l.gen AS state,
            k.gen AS kreis,
            k.bez AS kreis_type,
            k.nuts AS kreis_nuts
        FROM b
        LEFT JOIN boundaries.vg250_lan l
               ON ST_Contains(l.geometry, b.geom)
              AND l.gf = 4         -- 'Land mit größeren Strukturen' (skip Bodensee shells)
        LEFT JOIN boundaries.vg250_krs k
               ON ST_Contains(k.geometry, b.geom)
    """
    df = pd.read_sql(sql, engine).drop_duplicates(subset=["bus_id"])
    df = df.set_index("bus_id")
    df["state"] = df["state"].fillna("")
    df["kreis"] = df["kreis"].fillna("")
    df["country"] = df["country"].fillna("")
    df["tso"] = df["state"].map(BUNDESLAND_TO_TSO).fillna("")
    log.info(f"  Buses with Bundesland: {(df['state']!='').sum()}/{len(df)}")
    log.info(f"  Buses with Kreis:      {(df['kreis']!='').sum()}/{len(df)}")
    log.info(f"  Top TSOs: {df['tso'].value_counts().head().to_dict()}")
    return df


def compute_per_line_stats(n, bus_regions=None):
    """For every line: max, mean, p50, p95, p99 absolute flow + % of s_nom + peak hour.

    If bus_regions is provided, joins state/kreis/country/tso onto every line
    using bus0 (lines that span states are tagged by their bus0).
    """
    log.info("Computing per-line annual stats...")
    p0 = n.lines_t.p0
    s_nom = n.lines["s_nom"].astype(np.float32).values
    s_nom_safe = np.where(s_nom > 0, s_nom, np.nan)

    abs_p = p0.abs().values   # (T, L)
    T, L = abs_p.shape
    log.info(f"  flows shape {abs_p.shape}, computing percentiles...")

    pct = np.nanpercentile(abs_p, [50, 95, 99], axis=0)
    peak_idx = np.argmax(abs_p, axis=0)
    line_ids = list(p0.columns)
    bus0 = n.lines.loc[line_ids, "bus0"].astype(str).values
    bus1 = n.lines.loc[line_ids, "bus1"].astype(str).values

    df = pd.DataFrame({
        "line_id": line_ids,
        "bus0": bus0,
        "bus1": bus1,
        "v_nom": [n.buses.loc[b, "v_nom"] for b in bus0],
        "length_km": (n.lines.loc[line_ids, "length"].values
                      if "length" in n.lines.columns else np.zeros(L)),
        "s_nom_mw": s_nom,
        "max_mw": abs_p.max(axis=0),
        "mean_mw": abs_p.mean(axis=0),
        "p50_mw": pct[0],
        "p95_mw": pct[1],
        "p99_mw": pct[2],
        "peak_hour": peak_idx,
    })
    df["max_pct"] = 100 * df["max_mw"] / np.where(df["s_nom_mw"] > 0, df["s_nom_mw"], np.nan)
    df["mean_pct"] = 100 * df["mean_mw"] / np.where(df["s_nom_mw"] > 0, df["s_nom_mw"], np.nan)
    df["p99_pct"] = 100 * df["p99_mw"] / np.where(df["s_nom_mw"] > 0, df["s_nom_mw"], np.nan)
    df["overloaded_h"] = (abs_p > s_nom[None, :]).sum(axis=0)

    if bus_regions is not None:
        b0_meta = bus_regions.reindex(bus0)
        b1_meta = bus_regions.reindex(bus1)
        df["state"] = b0_meta["state"].values
        df["kreis"] = b0_meta["kreis"].values
        df["country"] = b0_meta["country"].values
        df["tso"] = b0_meta["tso"].values
        df["state1"] = b1_meta["state"].values
        # cross_state: line crosses Bundesland boundary
        df["cross_state"] = (df["state"] != df["state1"]) & (df["state"] != "") & (df["state1"] != "")

    return df, abs_p


def build_payload(n, line_stats, abs_p, top_n=30):
    """Build the JSON payload embedded in the HTML."""
    log.info(f"Building payload (top {top_n} lines fully embedded)...")

    # Sort lines by max_pct (overloads first)
    df = line_stats.copy()
    df["sort_key"] = df["max_pct"].fillna(0)
    df = df.sort_values("sort_key", ascending=False)
    top_ids = df.head(top_n)["line_id"].tolist()

    # Hourly arrays for top lines
    top_hourly = {}
    p0_df = n.lines_t.p0
    snapshots_iso = [str(t)[:13] for t in n.snapshots]
    for lid in top_ids:
        col = p0_df[lid].values.astype(np.float32)
        top_hourly[str(lid)] = [round(float(v), 1) for v in col]

    # System-wide congestion timeline
    s_nom = n.lines["s_nom"].astype(np.float32).values
    s_nom_safe = np.where(s_nom > 0, s_nom, np.nan)
    abs_p_safe = np.nan_to_num(abs_p, nan=0)
    loading_pct = abs_p_safe / s_nom_safe[None, :] * 100
    loading_pct = np.where(np.isfinite(loading_pct), loading_pct, 0)

    sys_max = loading_pct.max(axis=1)
    sys_p95 = np.percentile(loading_pct, 95, axis=1)
    sys_mean = loading_pct.mean(axis=1)
    n_overloaded_per_h = (loading_pct > 100).sum(axis=1)

    # Histograms
    bins = [0, 25, 50, 75, 90, 100, 125, 150, 200, 500, 1000, 1e6]
    bin_labels = ["0-25%", "25-50%", "50-75%", "75-90%", "90-100%",
                  "100-125%", "125-150%", "150-200%", "200-500%", "500-1000%", ">1000%"]
    max_pct = df["max_pct"].fillna(0).values
    hist_max, _ = np.histogram(max_pct, bins=bins)

    # Geographic map data: bus coords + line topology with annual max %
    buses_df = n.buses[["x", "y", "v_nom"]].copy()
    bus_ids = list(buses_df.index)
    buses_payload = {
        "ids": bus_ids,
        "x": [round(float(v), 4) for v in buses_df["x"].values],
        "y": [round(float(v), 4) for v in buses_df["y"].values],
        "v_nom": [int(v) for v in buses_df["v_nom"].values],
    }

    # For lines, embed only those with x,y on both ends (skip lines with NaN coords)
    line_geo = []
    bus_x = dict(zip(bus_ids, buses_df["x"].values))
    bus_y = dict(zip(bus_ids, buses_df["y"].values))
    for _, row in line_stats.iterrows():
        b0, b1 = row["bus0"], row["bus1"]
        if b0 not in bus_x or b1 not in bus_x:
            continue
        x0, y0 = bus_x[b0], bus_y[b0]
        x1, y1 = bus_x[b1], bus_y[b1]
        if not (np.isfinite(x0) and np.isfinite(x1) and np.isfinite(y0) and np.isfinite(y1)):
            continue
        line_geo.append({
            "id": str(row["line_id"]),
            "v": int(row["v_nom"]),
            "x0": round(float(x0), 4), "y0": round(float(y0), 4),
            "x1": round(float(x1), 4), "y1": round(float(y1), 4),
            "max_pct": round(float(row["max_pct"]) if np.isfinite(row["max_pct"]) else 0, 1),
            "max_mw": round(float(row["max_mw"]), 0),
            "s_nom_mw": round(float(row["s_nom_mw"]), 0),
        })

    # All-lines table (every line, but lean fields)
    has_regions = "state" in line_stats.columns
    all_lines_lite = []
    for _, row in line_stats.iterrows():
        rec = {
            "id": str(row["line_id"]),
            "v": int(row["v_nom"]),
            "len": round(float(row["length_km"]), 1),
            "snom": round(float(row["s_nom_mw"]), 0),
            "max": round(float(row["max_mw"]), 0),
            "mean": round(float(row["mean_mw"]), 0),
            "p99": round(float(row["p99_mw"]), 0),
            "maxp": round(float(row["max_pct"]) if np.isfinite(row["max_pct"]) else 0, 1),
            "meanp": round(float(row["mean_pct"]) if np.isfinite(row["mean_pct"]) else 0, 1),
            "ov_h": int(row["overloaded_h"]),
            "ph": int(row["peak_hour"]),
        }
        if has_regions:
            rec["state"] = row.get("state", "") or ""
            rec["kreis"] = row.get("kreis", "") or ""
            rec["tso"] = row.get("tso", "") or ""
            rec["country"] = row.get("country", "") or ""
            rec["cross"] = bool(row.get("cross_state", False))
            rec["state1"] = row.get("state1", "") or ""
        all_lines_lite.append(rec)

    # KPIs
    n_lines = len(line_stats)
    n_overloaded = int((line_stats["max_pct"] > 100).sum())
    n_overloaded_severe = int((line_stats["max_pct"] > 200).sum())
    total_lh = abs_p.size
    overloaded_lh = int((loading_pct > 100).sum())
    peak_global_idx = int(np.unravel_index(np.argmax(loading_pct), loading_pct.shape)[0])
    peak_global_line = line_stats.iloc[
        int(np.unravel_index(np.argmax(loading_pct), loading_pct.shape)[1])]["line_id"]
    n_lines_v = line_stats.groupby("v_nom").size().to_dict()

    kpis = {
        "n_lines": int(n_lines),
        "n_buses": int(len(n.buses)),
        "n_snapshots": int(len(n.snapshots)),
        "n_overloaded": n_overloaded,
        "pct_overloaded": round(100 * n_overloaded / n_lines, 1),
        "n_overloaded_severe": n_overloaded_severe,
        "overloaded_line_hours": overloaded_lh,
        "pct_overloaded_lh": round(100 * overloaded_lh / total_lh, 2),
        "mean_loading_pct": round(float(loading_pct.mean()), 1),
        "p95_loading_pct": round(float(np.percentile(loading_pct, 95)), 1),
        "max_loading_pct": round(float(loading_pct.max()), 1),
        "peak_hour": peak_global_idx,
        "peak_line": str(peak_global_line),
        "n_380kv": int(n_lines_v.get(380, 0)),
        "n_220kv": int(n_lines_v.get(220, 0)),
        "n_110kv": int(n_lines_v.get(110, 0)),
    }

    # Aggregated stats per region/TSO for the regional summary tab
    region_summary = {}
    if has_regions:
        # By Bundesland
        for state, grp in line_stats.groupby("state"):
            if not state:
                continue
            ov = int((grp["max_pct"] > 100).sum())
            region_summary.setdefault("by_state", {})[state] = {
                "n_lines": int(len(grp)),
                "n_overloaded": ov,
                "pct_overloaded": round(100 * ov / len(grp), 1) if len(grp) else 0.0,
                "mean_max_pct": round(float(grp["max_pct"].fillna(0).mean()), 1),
                "tso": BUNDESLAND_TO_TSO.get(state, ""),
            }
        # By TSO
        for tso, grp in line_stats.groupby("tso"):
            if not tso:
                continue
            ov = int((grp["max_pct"] > 100).sum())
            region_summary.setdefault("by_tso", {})[tso] = {
                "n_lines": int(len(grp)),
                "n_overloaded": ov,
                "pct_overloaded": round(100 * ov / len(grp), 1) if len(grp) else 0.0,
                "mean_max_pct": round(float(grp["max_pct"].fillna(0).mean()), 1),
            }

    # Top 100 lines for the dedicated "Top Lines Sheet" — richer columns + region info
    top100 = (line_stats
              .assign(sort_key=line_stats["max_pct"].fillna(0))
              .sort_values("sort_key", ascending=False)
              .head(100))
    top100_records = []
    for _, row in top100.iterrows():
        rec = {
            "id": str(row["line_id"]),
            "v": int(row["v_nom"]),
            "bus0": str(row["bus0"]),
            "bus1": str(row["bus1"]),
            "len": round(float(row["length_km"]), 1),
            "snom": round(float(row["s_nom_mw"]), 0),
            "max": round(float(row["max_mw"]), 0),
            "mean": round(float(row["mean_mw"]), 0),
            "p99": round(float(row["p99_mw"]), 0),
            "maxp": round(float(row["max_pct"]) if np.isfinite(row["max_pct"]) else 0, 1),
            "p99p": round(float(row["p99_pct"]) if np.isfinite(row["p99_pct"]) else 0, 1),
            "meanp": round(float(row["mean_pct"]) if np.isfinite(row["mean_pct"]) else 0, 1),
            "ov_h": int(row["overloaded_h"]),
            "ph": int(row["peak_hour"]),
        }
        if has_regions:
            rec["state"] = row.get("state", "") or ""
            rec["state1"] = row.get("state1", "") or ""
            rec["kreis"] = row.get("kreis", "") or ""
            rec["tso"] = row.get("tso", "") or ""
            rec["country"] = row.get("country", "") or ""
            rec["cross"] = bool(row.get("cross_state", False))
        top100_records.append(rec)

    payload = {
        "kpis": kpis,
        "snapshots": snapshots_iso,
        "system": {
            "max_pct": [round(float(v), 1) for v in sys_max],
            "p95_pct": [round(float(v), 1) for v in sys_p95],
            "mean_pct": [round(float(v), 1) for v in sys_mean],
            "n_overloaded": [int(v) for v in n_overloaded_per_h],
        },
        "hist_max": {
            "labels": bin_labels,
            "counts": [int(v) for v in hist_max],
        },
        "buses": buses_payload,
        "lines_geo": line_geo,
        "all_lines": all_lines_lite,
        "top_lines_hourly": top_hourly,
        "top_line_ids": [str(x) for x in top_ids],
        "top100": top100_records,
        "region_summary": region_summary,
        "has_regions": bool(has_regions),
    }
    return payload


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>DC Power Flow — grid_beta — 8760 h</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" charset="utf-8"></script>
<style>
  :root {
    --bg: #f6f7fb; --card: #ffffff; --text: #1a2332; --muted: #586781;
    --border: #e3e6ee; --accent: #2f6fed; --warn: #f76b4f; --crit: #c43a2c;
    --good: #22c55e;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Roboto, system-ui, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.55;
  }
  .container { max-width: 1380px; margin: 0 auto; padding: 28px 32px 80px; }
  header { margin-bottom: 18px; }
  header h1 { margin: 0 0 6px; font-size: 1.85rem; font-weight: 600; letter-spacing: -0.02em; }
  header .subtitle { color: var(--muted); font-size: 0.95rem; }
  header .specs { display: flex; flex-wrap: wrap; gap: 6px 14px; color: var(--muted);
    font-size: 0.85rem; margin-top: 10px; }
  header .specs span { background: var(--card); padding: 3px 10px; border-radius: 999px;
    border: 1px solid var(--border); }

  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px; margin: 22px 0; }
  .stat-card { background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px 16px; }
  .stat-card .v { font-size: 1.55rem; font-weight: 600; line-height: 1.1; }
  .stat-card .l { color: var(--muted); font-size: 0.78rem; margin-top: 4px;
    text-transform: uppercase; letter-spacing: 0.06em; }
  .stat-card.warn .v { color: var(--warn); }
  .stat-card.crit .v { color: var(--crit); }

  nav.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border);
    margin-bottom: 22px; overflow-x: auto; }
  nav.tabs button { background: transparent; border: none; padding: 11px 18px;
    color: var(--muted); cursor: pointer; font-size: 0.92rem; white-space: nowrap;
    border-bottom: 2px solid transparent; transition: color .15s; }
  nav.tabs button:hover { color: var(--text); }
  nav.tabs button.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 500; }

  section.tab { display: none; }
  section.tab.active { display: block; animation: fadein .25s; }
  @keyframes fadein { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }

  h2 { font-size: 1.18rem; font-weight: 600; margin: 28px 0 10px; }
  h2:first-child { margin-top: 0; }
  h3 { font-size: 0.98rem; font-weight: 600; color: var(--muted); margin: 18px 0 8px; }
  p.lead { color: var(--text); max-width: 80ch; }
  p.note { color: var(--muted); font-size: 0.88rem; max-width: 80ch; }

  .chart-card { background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px; margin: 8px 0 18px; }
  .chart { width: 100%; height: 420px; }
  .chart-tall { height: 540px; }
  .chart-map { height: 700px; border-radius: 8px; }
  /* Leaflet container */
  #leaflet-map { width: 100%; height: 700px; border-radius: 8px;
    background: #e8eef7; }
  .leaflet-container { background: #f0f4fa; font-family: inherit; }
  .line-popup { font-size: 12px; line-height: 1.45; }
  .line-popup b { color: #1a2332; }
  .line-popup .pct-crit { color: #c43a2c; font-weight: 600; }
  .line-popup .pct-warn { color: #f76b4f; font-weight: 600; }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .grid-3 { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; }
  @media (max-width: 900px) { .grid-2, .grid-3 { grid-template-columns: 1fr; } }

  .filters { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center;
    background: var(--card); padding: 14px 18px; border: 1px solid var(--border);
    border-radius: 10px; margin: 8px 0 14px; }
  .filters label { color: var(--muted); font-size: 0.82rem; }
  .filters input, .filters select {
    padding: 5px 10px; border: 1px solid var(--border); border-radius: 6px;
    font-size: 0.85rem; background: white; }
  .filters .chip {
    background: #eef1f8; padding: 3px 9px; border-radius: 999px;
    font-size: 0.78rem; cursor: pointer; user-select: none; border: 1px solid transparent;
  }
  .filters .chip.active { background: var(--accent); color: white; }

  table.data { width: 100%; border-collapse: collapse; background: var(--card);
    border-radius: 10px; overflow: hidden; border: 1px solid var(--border);
    font-size: 0.85rem; }
  table.data thead th { padding: 8px 12px; text-align: right;
    background: #f0f3fa; color: var(--muted); font-weight: 600;
    border-bottom: 1px solid var(--border); cursor: pointer; user-select: none;
    position: sticky; top: 0; }
  table.data thead th:first-child, table.data tbody td:first-child { text-align: left; }
  table.data thead th.sort-asc::after { content: " ▲"; font-size: 0.7em; }
  table.data thead th.sort-desc::after { content: " ▼"; font-size: 0.7em; }
  table.data tbody td { padding: 6px 12px; text-align: right; border-bottom: 1px solid #f4f6fb; }
  table.data tbody tr:hover td { background: #fafbfd; }
  td.crit { color: var(--crit); font-weight: 500; }
  td.warn { color: var(--warn); }

  .scroll-table { max-height: 700px; overflow-y: auto; border: 1px solid var(--border);
    border-radius: 10px; }
  .pagination { padding: 8px 14px; color: var(--muted); font-size: 0.85rem;
    background: var(--card); border-top: 1px solid var(--border); }

  .selector-bar { background: var(--card); padding: 12px 16px; border: 1px solid var(--border);
    border-radius: 10px; margin: 8px 0; display: flex; gap: 12px; align-items: center;
    flex-wrap: wrap; }
  .selector-bar select { padding: 6px 12px; border-radius: 6px; border: 1px solid var(--border); }

  .footer { color: var(--muted); font-size: 0.78rem; margin-top: 60px; text-align: center; }

  .legend { display: flex; flex-wrap: wrap; gap: 14px; padding: 6px 12px; }
  .legend .item { display: flex; align-items: center; gap: 6px; font-size: 0.82rem; }
  .legend .swatch { width: 24px; height: 4px; border-radius: 2px; }
  .legend .swatch-thick { height: 6px; }
  .filters .sep { color: #cbd5e1; padding: 0 4px; }
  .filters input[type=range] { accent-color: var(--accent); }

  .methodology p, .methodology li { max-width: 82ch; color: var(--text); }
  .methodology code { background: #eef1f8; padding: 1px 6px; border-radius: 4px; font-size: 0.85em; }
</style>
</head>
<body>

<div class="container">
  <header>
    <h1>DC Power Flow — grid_beta</h1>
    <div class="subtitle" id="hdr-sub">Loading…</div>
    <div class="specs">
      <span>linear DC PF</span>
      <span>full topology</span>
      <span>dispatch from MILP UC v5</span>
      <span>HVDC links open</span>
      <span>copperplate dispatch fixed</span>
    </div>
  </header>

  <div class="stats-grid" id="stats-grid"></div>

  <nav class="tabs" id="tab-bar">
    <button class="active" data-tab="overview">Overview</button>
    <button data-tab="map">Geographic Map</button>
    <button data-tab="topsheet">Top Lines Sheet</button>
    <button data-tab="topline">Hourly Inspector</button>
    <button data-tab="region">By Region / TSO</button>
    <button data-tab="hourly">Hourly Congestion</button>
    <button data-tab="all">All Lines (filterable)</button>
    <button data-tab="method">Methodology</button>
  </nav>

  <section id="tab-overview" class="tab active">
    <p class="lead">
      A linear (DC) power flow was solved on every one of the 8 760 snapshots, with the
      dispatch fixed from the MILP unit-commitment run. Line capacities are <strong>not
      enforced</strong> — these flows show <em>where</em> the unconstrained dispatch would
      stress the network. HVDC links are kept open (zero flow), so all cross-border power
      moves through AC tielines.
    </p>

    <h2>Annual Loading Distribution</h2>
    <p class="note">For each line, the maximum hourly loading across the year. Lines above
      100% would require redispatch, expansion, or curtailment in a real operation.</p>
    <div class="chart-card"><div id="chart-hist" class="chart"></div></div>

    <h2>Per-Voltage-Level Counts</h2>
    <div class="chart-card"><div id="chart-by-voltage" class="chart"></div></div>
  </section>

  <section id="tab-map" class="tab">
    <h2>Grid Map — Topology + Annual Loading</h2>
    <div class="filters">
      <label>Voltage:</label>
      <span class="chip active" data-v="380">380 kV</span>
      <span class="chip active" data-v="220">220 kV</span>
      <span class="chip" data-v="110">110 kV</span>
      <span class="sep">|</span>
      <label>Show:</label>
      <span class="chip active" data-mode="all">All lines</span>
      <span class="chip" data-mode="overload">Overloaded only (&gt;100%)</span>
      <span class="chip" data-mode="severe">Severe only (&gt;200%)</span>
      <span class="sep">|</span>
      <label>Min loading %:</label>
      <input type="range" id="map-threshold" min="0" max="200" value="0" step="5" style="width:140px">
      <span id="map-threshold-val">0%</span>
      <span style="margin-left:auto">Showing <b id="map-count">0</b> · <b id="map-overloaded">0</b> ≥ 100%</span>
    </div>
    <div class="chart-card" style="padding:0; overflow:hidden;">
      <div id="leaflet-map"></div>
    </div>
    <div class="legend">
      <div class="item"><span class="swatch" style="background:#cbd5e1"></span> 0-25%</div>
      <div class="item"><span class="swatch" style="background:#22c55e"></span> 25-50%</div>
      <div class="item"><span class="swatch" style="background:#84cc16"></span> 50-75%</div>
      <div class="item"><span class="swatch" style="background:#eab308"></span> 75-100%</div>
      <div class="item"><span class="swatch swatch-thick" style="background:#f76b4f"></span> 100-150%</div>
      <div class="item"><span class="swatch swatch-thick" style="background:#dc2626"></span> 150-200%</div>
      <div class="item"><span class="swatch swatch-thick" style="background:#7c1d1d"></span> &gt; 200%</div>
    </div>
    <p class="note">Overloaded lines (≥100 % of s_nom for at least one hour during the year)
      are drawn <strong>thicker on top</strong> of the topology so they pop visually.
      Click any line for its details. Map tiles by
      <a href="https://carto.com/" target="_blank" rel="noopener">CARTO</a> /
      <a href="https://openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>.</p>
  </section>

  <section id="tab-topsheet" class="tab">
    <h2>Top 100 Most-Loaded Lines</h2>
    <p class="note">Ranked by annual maximum loading. <span id="topsheet-region-note"></span></p>
    <div class="filters">
      <label>TSO:</label>
      <select id="topsheet-tso"><option value="all">All</option></select>
      <label>Bundesland:</label>
      <select id="topsheet-state"><option value="all">All</option></select>
      <label>Voltage:</label>
      <select id="topsheet-v">
        <option value="all">All</option>
        <option value="380">380 kV</option>
        <option value="220">220 kV</option>
        <option value="110">110 kV</option>
      </select>
      <label>Cross-state only:</label>
      <input type="checkbox" id="topsheet-cross">
      <button class="chip" id="topsheet-clear">Clear</button>
    </div>
    <div class="scroll-table">
      <table class="data" id="topsheet-table">
        <thead><tr>
          <th data-k="id">Line ID</th>
          <th data-k="v" class="num">kV</th>
          <th data-k="state">State</th>
          <th data-k="kreis">District (Kreis)</th>
          <th data-k="tso">TSO area</th>
          <th data-k="state1">→ State (other end)</th>
          <th data-k="len" class="num">Length km</th>
          <th data-k="snom" class="num">s_nom MW</th>
          <th data-k="max" class="num">Peak MW</th>
          <th data-k="maxp" class="num">Peak %</th>
          <th data-k="meanp" class="num">Mean %</th>
          <th data-k="ov_h" class="num">Hours &gt;100%</th>
          <th data-k="ph" class="num">Peak h</th>
        </tr></thead>
        <tbody id="topsheet-body"></tbody>
      </table>
    </div>
    <p class="note" style="margin-top:12px">
      <strong>How to read the table.</strong>
      <em>State</em> is determined by spatial-join of the line's <code>bus0</code>
      coordinate with the <code>boundaries.vg250_lan</code> Bundesländer polygons.
      <em>TSO area</em> is a coarse Bundesland → control-area mapping (real control areas
      don't strictly follow state borders — Hamburg and Hessen are split, hence labels
      like <em>50Hertz/TenneT</em>). Lines flagged <em>cross-state</em> have endpoints
      in different Bundesländer — these are the typical north–south transit corridors.
    </p>
  </section>

  <section id="tab-region" class="tab">
    <h2>By TSO Control Area</h2>
    <p class="note">Aggregated overload counts per (approximate) TSO area. Each line is
      assigned to a TSO via the Bundesland of its <code>bus0</code> endpoint.</p>
    <div class="chart-card"><div id="chart-by-tso" class="chart"></div></div>

    <h2>By Bundesland</h2>
    <div class="chart-card"><div id="chart-by-state" class="chart chart-tall"></div></div>
  </section>

  <section id="tab-topline" class="tab">
    <h2>Hourly Inspector — Top 30 Lines</h2>
    <div class="selector-bar">
      <label for="line-picker">Inspect a line:</label>
      <select id="line-picker"></select>
      <span id="line-info" class="note"></span>
    </div>
    <div class="chart-card"><div id="chart-line-hourly" class="chart chart-tall"></div></div>
    <h2>Top 30 by Annual Maximum (% of s_nom)</h2>
    <div class="chart-card"><div id="chart-top-bar" class="chart"></div></div>
  </section>

  <section id="tab-hourly" class="tab">
    <h2>System-Wide Congestion Timeline</h2>
    <p class="note">For each hour: maximum line loading anywhere on the grid (orange), 95th
      percentile (yellow), and number of simultaneously-overloaded lines (right axis, red).</p>
    <div class="chart-card"><div id="chart-sys" class="chart chart-tall"></div></div>
    <h2>Number of Overloaded Lines per Hour</h2>
    <div class="chart-card"><div id="chart-ovh" class="chart"></div></div>
  </section>

  <section id="tab-all" class="tab">
    <h2>All Lines</h2>
    <p class="note">All 12 911 AC lines. Use the filters or click a column header to sort.
      Showing <b id="all-count">0</b> of <b id="all-total">0</b> lines · page <b id="all-page">1</b>.</p>
    <div class="filters">
      <label>Voltage:</label>
      <select id="filter-v">
        <option value="all">All</option>
        <option value="380">380 kV</option>
        <option value="220">220 kV</option>
        <option value="110">110 kV</option>
      </select>
      <label>TSO:</label>
      <select id="filter-tso"><option value="all">All</option></select>
      <label>Bundesland:</label>
      <select id="filter-state"><option value="all">All</option></select>
      <label>Min max %:</label>
      <input type="number" id="filter-min" value="" placeholder="e.g. 100">
      <label>Search line ID:</label>
      <input type="text" id="filter-id" placeholder="line id substring">
      <button class="chip" id="filter-clear">Clear</button>
    </div>
    <div class="scroll-table">
      <table class="data" id="all-table">
        <thead><tr>
          <th data-k="id">Line ID</th>
          <th data-k="v" class="num">kV</th>
          <th data-k="state">State</th>
          <th data-k="tso">TSO</th>
          <th data-k="len" class="num">Length km</th>
          <th data-k="snom" class="num">s_nom MW</th>
          <th data-k="mean" class="num">Mean MW</th>
          <th data-k="p99" class="num">p99 MW</th>
          <th data-k="max" class="num">Max MW</th>
          <th data-k="meanp" class="num">Mean %</th>
          <th data-k="maxp" class="num">Max %</th>
          <th data-k="ov_h" class="num">Hours &gt; 100%</th>
        </tr></thead>
        <tbody id="all-body"></tbody>
      </table>
      <div class="pagination">
        <button class="chip" id="all-prev">‹ Prev</button>
        <button class="chip" id="all-next">Next ›</button>
        <span id="all-pageinfo"></span>
      </div>
    </div>
  </section>

  <section id="tab-method" class="tab methodology">
    <h2>Methodology</h2>
    <h3>Linear (DC) power flow</h3>
    <p>For every snapshot, a sparse linear system <code>B·θ = p</code> was solved on the
      main connected subnetwork (7 707 buses · 12 911 AC lines · 567 transformers).
      <code>B</code> is the susceptance Laplacian
      (<code>B<sub>ij</sub> = −1/x<sub>ij</sub></code> for each line + transformer between
      buses i and j); <code>p</code> is net active-power injection per bus from the dispatch
      (gen − load + storage); <code>θ</code> is the bus voltage angle. Per-line flow is
      <code>f<sub>l</sub> = (θ<sub>0</sub> − θ<sub>1</sub>) / x<sub>l</sub></code>.
      The PTDF was computed once via sparse LU factorization of the reduced
      <code>B</code> (one slack bus removed) and then matrix-multiplied against the
      8 760 injection vectors.</p>

    <h3>Why the loadings can be unrealistic</h3>
    <p>The dispatch was solved as <strong>copperplate</strong> — the merit order ignores
      transmission constraints. Once the dispatch is fixed and we enforce Kirchhoff on the
      real topology, you get the line flows that <em>would result</em> if every plant
      followed the merit-order schedule with no internal congestion management. In practice
      the German TSOs run ~28 TWh/year of redispatch (BNetzA 2024) precisely to avoid these
      flows. Every overloaded line in this report is a candidate for that redispatch.</p>

    <h3>Specific caveats</h3>
    <ul>
      <li><b>HVDC links</b> are held at 0 (controllable, but no setpoint was solved for).
        All cross-border flow therefore appears on the AC tielines, which inflates
        loadings near the borders.</li>
      <li><b>Per-generator dispatch is allocated, not solved.</b> Gen-by-gen dispatch
        within a carrier is split by <code>p_max_pu × p_nom</code>, so two coal plants of
        equal size at the same hour produce identical flows even if one is in the north and
        one in the south. This affects spatial flow patterns without changing the totals.</li>
      <li><b>No voltage / reactive-power info.</b> DC PF assumes flat voltage profile and
        ignores reactive flows. Use AC PF if you need Q / |V|.</li>
      <li><b>Line s_nom comes from JAO + defaults.</b> Some 110 kV s_nom values may be
        understated for parallel-circuit lines, inflating loading percentages.</li>
      <li><b>Transformer flows</b> are not shown in the per-line stats (only the 12 911 AC
        lines). Transformers are present in the susceptance matrix and absorb their share
        of flow correctly.</li>
    </ul>

    <h3>Files</h3>
    <p>Input: <code>results/dispatch_8760h.nc</code> (the dispatch). Output:
      <code>results/dispatch_8760h_pf.nc</code> (full network with
      <code>lines_t.p0/p1</code> filled in for all 8 760 hours).</p>
  </section>

  <p class="footer" id="footer-time">Generated —</p>
</div>

<script>
const DATA = __DATA__;

// ── Helpers ──────────────────────────────────────────
function escapeHTML(s) { return String(s).replace(/[&<>"]/g, c =>
  ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;'})[c]); }
const COMMON_LAYOUT = {
  margin: { l: 60, r: 30, t: 30, b: 50 },
  paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
  font: { family: 'Inter, system-ui, sans-serif', size: 12, color: '#1a2332' },
  hovermode: 'closest',
  xaxis: { gridcolor: '#eef1f8', zerolinecolor: '#dde3ee' },
  yaxis: { gridcolor: '#eef1f8', zerolinecolor: '#dde3ee' },
};
const COMMON_CONFIG = { responsive: true, displayModeBar: 'hover',
  modeBarButtonsToRemove: ['select2d','lasso2d','autoScale2d'] };

// ── KPI cards ────────────────────────────────────────
function renderKPIs() {
  const k = DATA.kpis;
  document.getElementById('hdr-sub').textContent =
    `${k.n_snapshots.toLocaleString()} hours · ${k.n_lines.toLocaleString()} lines · ${k.n_buses.toLocaleString()} buses`;

  const cards = [
    { v: k.max_loading_pct + '%', l: 'Peak Loading (any line, any hour)', cls: 'crit' },
    { v: k.p95_loading_pct + '%', l: 'p95 Loading' },
    { v: k.mean_loading_pct + '%', l: 'Mean Loading' },
    { v: k.n_overloaded, l: 'Lines with ≥ 1 hour > 100%',
      cls: k.n_overloaded > 100 ? 'warn' : '' },
    { v: k.n_overloaded_severe, l: 'Lines with > 200% peak', cls: 'crit' },
    { v: k.pct_overloaded_lh + '%', l: 'Line-Hours > 100%' },
    { v: '#' + k.peak_line, l: 'Peak Loaded Line ID' },
    { v: 'h' + k.peak_hour, l: 'Peak Loading Hour' },
  ];
  const grid = document.getElementById('stats-grid');
  cards.forEach(c => {
    const el = document.createElement('div');
    el.className = 'stat-card' + (c.cls ? ' ' + c.cls : '');
    el.innerHTML = `<div class="v">${escapeHTML(c.v)}</div><div class="l">${escapeHTML(c.l)}</div>`;
    grid.appendChild(el);
  });
  document.getElementById('footer-time').textContent =
    'Generated ' + new Date().toISOString().slice(0,16).replace('T',' ');
}

// ── Tabs ─────────────────────────────────────────────
function setupTabs() {
  document.querySelectorAll('nav.tabs button').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('nav.tabs button').forEach(t => t.classList.remove('active'));
      b.classList.add('active');
      document.querySelectorAll('section.tab').forEach(s => s.classList.remove('active'));
      document.getElementById('tab-' + b.dataset.tab).classList.add('active');
      window.dispatchEvent(new Event('resize'));
      // Lazy-init Leaflet on first visit to the Map tab; otherwise just relayout
      if (b.dataset.tab === 'map') {
        if (!mapInitialized) safe('chartMap', chartMap);
        else if (leafletMap) {
          setTimeout(() => leafletMap.invalidateSize(), 50);
        }
      }
    });
  });
}

// ── Histogram of annual max loading ──────────────────
function chartHist() {
  const colors = ['#22c55e','#84cc16','#a3e635','#facc15','#eab308',
                  '#f76b4f','#dc2626','#c43a2c','#7c1d1d','#4b1517','#27090a'];
  Plotly.newPlot('chart-hist', [{
    type: 'bar',
    x: DATA.hist_max.labels,
    y: DATA.hist_max.counts,
    marker: { color: colors.slice(0, DATA.hist_max.labels.length) },
    text: DATA.hist_max.counts.map(v => v.toLocaleString()),
    textposition: 'outside',
    hovertemplate: '%{x}<br>%{y:,d} lines<extra></extra>',
  }], {
    ...COMMON_LAYOUT,
    yaxis: { ...COMMON_LAYOUT.yaxis, title: 'Number of lines', type: 'log' },
    xaxis: { ...COMMON_LAYOUT.xaxis, title: 'Annual maximum loading (% of s_nom)' },
    title: { text: 'Distribution: 12 911 lines bucketed by their max-of-year loading',
             font: { size: 14 } },
  }, COMMON_CONFIG);
}

// ── By-voltage breakdown ─────────────────────────────
function chartByVoltage() {
  const k = DATA.kpis;
  const cats = [
    {v:380, c:'#1a53ff', n:k.n_380kv},
    {v:220, c:'#2f6fed', n:k.n_220kv},
    {v:110, c:'#5fa8f7', n:k.n_110kv},
  ];
  // Compute # overloaded per voltage from all_lines
  const ov = {380:0, 220:0, 110:0};
  for (const l of DATA.all_lines) {
    if (l.maxp > 100 && ov[l.v] !== undefined) ov[l.v]++;
  }
  Plotly.newPlot('chart-by-voltage', [
    {type:'bar', name:'Total lines', x: cats.map(c=>c.v+' kV'), y: cats.map(c=>c.n),
     marker:{color: cats.map(c=>c.c)}, text: cats.map(c=>c.n.toLocaleString()), textposition:'outside'},
    {type:'bar', name:'Overloaded (max > 100%)', x: cats.map(c=>c.v+' kV'),
     y: cats.map(c=>ov[c.v]), marker:{color:'#f76b4f'},
     text: cats.map(c=>ov[c.v].toLocaleString()), textposition:'outside'},
  ], {
    ...COMMON_LAYOUT, barmode: 'group',
    yaxis: {...COMMON_LAYOUT.yaxis, title: 'Number of lines'},
  }, COMMON_CONFIG);
}

// ── Geographic map (Leaflet, GPU-canvas-rendered) ──────────────
function colorForPct(p) {
  if (p < 25) return '#cbd5e1';
  if (p < 50) return '#22c55e';
  if (p < 75) return '#84cc16';
  if (p < 100) return '#eab308';
  if (p < 150) return '#f76b4f';
  if (p < 200) return '#dc2626';
  return '#7c1d1d';
}
let mapVoltagesActive = new Set([380, 220]);
let mapMode = 'all';      // 'all' | 'overload' | 'severe'
let mapThreshold = 0;     // min loading %
let leafletMap = null;
let lineLayerBg = null;   // <100% loading
let lineLayerOv = null;   // >=100% loading
let busLayer = null;
let mapInitialized = false;

const GERMANY_BOUNDS = [[47.0, 5.5], [55.5, 15.7]];   // [SW, NE]

function initMap() {
  if (mapInitialized) return;
  mapInitialized = true;

  if (typeof L === 'undefined') {
    showError('Leaflet failed to load (CDN blocked or offline). The map tab will be empty.');
    return;
  }

  leafletMap = L.map('leaflet-map', {
    preferCanvas: true,           // canvas renderer = GPU-fast for many lines
    zoomControl: true,
    worldCopyJump: false,
    maxZoom: 12, minZoom: 4,
  });
  leafletMap.fitBounds(GERMANY_BOUNDS);

  // CARTO Positron — light, beautiful, no API key, OSM-attributed
  L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 19,
  }).addTo(leafletMap);

  lineLayerBg = L.layerGroup().addTo(leafletMap);
  lineLayerOv = L.layerGroup().addTo(leafletMap);
  busLayer    = L.layerGroup();   // off by default — toggled
  // Bus dots at high zoom only (auto-toggled in zoomend)
  leafletMap.on('zoomend', () => {
    const z = leafletMap.getZoom();
    if (z >= 8 && !leafletMap.hasLayer(busLayer)) leafletMap.addLayer(busLayer);
    else if (z < 8 && leafletMap.hasLayer(busLayer)) leafletMap.removeLayer(busLayer);
  });

  // Pre-draw bus markers once (we toggle the whole layer in/out)
  const renderer = L.canvas({ padding: 0.5 });
  for (let i = 0; i < DATA.buses.x.length; i++) {
    const v = DATA.buses.v_nom[i];
    const r = v >= 380 ? 3 : v >= 220 ? 2 : 1.2;
    const col = v >= 380 ? '#1a53ff' : v >= 220 ? '#5fa8f7' : '#94a3b8';
    L.circleMarker([DATA.buses.y[i], DATA.buses.x[i]], {
      radius: r, fillColor: col, color: col, weight: 0, fillOpacity: 0.75,
      renderer,
    }).bindTooltip(`Bus ${DATA.buses.ids[i]} · ${v} kV`).addTo(busLayer);
  }
}

function chartMap() {
  initMap();
  if (!leafletMap) return;

  let visible = DATA.lines_geo.filter(l => mapVoltagesActive.has(l.v));
  if (mapMode === 'overload') visible = visible.filter(l => l.max_pct >= 100);
  else if (mapMode === 'severe') visible = visible.filter(l => l.max_pct >= 200);
  if (mapThreshold > 0) visible = visible.filter(l => l.max_pct >= mapThreshold);

  // Clear previous
  lineLayerBg.clearLayers();
  lineLayerOv.clearLayers();

  // Sort so heavier-loaded lines added LAST → drawn on top in canvas
  visible.sort((a,b) => a.max_pct - b.max_pct);

  const widthsBg = {380: 1.6, 220: 1.0, 110: 0.5};
  const widthsOv = {380: 3.6, 220: 2.8, 110: 2.0};
  const renderer = L.canvas({ padding: 0.5 });

  for (const l of visible) {
    const overloaded = l.max_pct >= 100;
    const cls = overloaded
      ? (l.max_pct >= 200 ? 'pct-crit' : 'pct-warn')
      : '';
    const popupHtml =
      `<div class="line-popup">` +
      `<b>Line ${l.id}</b> · ${l.v} kV<br>` +
      `Max: <span class="${cls}">${l.max_pct.toFixed(1)}%</span> ` +
      `(${l.max_mw.toLocaleString()} / ${l.s_nom_mw.toLocaleString()} MW)` +
      `</div>`;
    const poly = L.polyline(
      [[l.y0, l.x0], [l.y1, l.x1]],
      {
        color: colorForPct(l.max_pct),
        weight: overloaded ? widthsOv[l.v] : widthsBg[l.v],
        opacity: overloaded ? 0.95 : 0.55,
        renderer,
        interactive: true,
      }
    ).bindPopup(popupHtml, { maxWidth: 320 });
    (overloaded ? lineLayerOv : lineLayerBg).addLayer(poly);
  }

  document.getElementById('map-count').textContent = visible.length.toLocaleString();
  document.getElementById('map-overloaded').textContent =
    visible.filter(l => l.max_pct >= 100).length.toLocaleString();

  // Make sure the map paints to its current container size — important when
  // the tab was hidden when the map first initialised.
  setTimeout(() => leafletMap.invalidateSize(), 50);
}

function setupMapFilters() {
  document.querySelectorAll('.filters .chip[data-v]').forEach(c => {
    c.addEventListener('click', () => {
      const v = parseInt(c.dataset.v);
      if (mapVoltagesActive.has(v)) { mapVoltagesActive.delete(v); c.classList.remove('active'); }
      else { mapVoltagesActive.add(v); c.classList.add('active'); }
      chartMap();
    });
  });
  document.querySelectorAll('.filters .chip[data-mode]').forEach(c => {
    c.addEventListener('click', () => {
      document.querySelectorAll('.filters .chip[data-mode]').forEach(o => o.classList.remove('active'));
      c.classList.add('active');
      mapMode = c.dataset.mode;
      chartMap();
    });
  });
  const thr = document.getElementById('map-threshold');
  thr.addEventListener('input', () => {
    mapThreshold = parseInt(thr.value);
    document.getElementById('map-threshold-val').textContent = mapThreshold + '%';
    chartMap();
  });
}

// ── Top lines: bar + selectable hourly ───────────────
function chartTopBar() {
  const top = DATA.top_line_ids.map(id =>
    DATA.all_lines.find(l => l.id === id)).filter(Boolean);
  Plotly.newPlot('chart-top-bar', [{
    type: 'bar', orientation: 'h',
    x: top.map(l => l.maxp).reverse(),
    y: top.map(l => `#${l.id} (${l.v}kV)`).reverse(),
    marker: { color: top.map(l => colorForPct(l.maxp)).reverse() },
    text: top.map(l => l.maxp.toFixed(1) + '%').reverse(),
    textposition: 'outside',
    hovertemplate: '%{y}<br>Max %{x:.1f}%<extra></extra>',
  }], {
    ...COMMON_LAYOUT,
    margin: {...COMMON_LAYOUT.margin, l: 130},
    xaxis: {...COMMON_LAYOUT.xaxis, ticksuffix: '%', title: 'Annual max loading'},
  }, COMMON_CONFIG);
}
function setupLinePicker() {
  const sel = document.getElementById('line-picker');
  for (const id of DATA.top_line_ids) {
    const l = DATA.all_lines.find(ll => ll.id === id);
    const opt = document.createElement('option');
    opt.value = id;
    opt.textContent = `Line ${id} · ${l ? l.v + 'kV · max ' + l.maxp.toFixed(1) + '%' : ''}`;
    sel.appendChild(opt);
  }
  sel.addEventListener('change', () => chartLineHourly(sel.value));
  chartLineHourly(DATA.top_line_ids[0]);
}
function chartLineHourly(lineId) {
  const flows = DATA.top_lines_hourly[lineId] || [];
  const l = DATA.all_lines.find(ll => ll.id === lineId);
  const sn = l ? l.snom : 0;
  document.getElementById('line-info').textContent = l
    ? `s_nom = ${l.snom.toLocaleString()} MW · ${l.v} kV · Length ${l.len} km`
    : '';

  Plotly.newPlot('chart-line-hourly', [
    {
      type:'scatter', mode:'lines', name:'Flow MW', x: DATA.snapshots, y: flows,
      line: {color: '#2f6fed', width: 1},
      hovertemplate: '%{x}<br>%{y:.0f} MW<extra></extra>',
    },
    {
      type:'scatter', mode:'lines', name:'+ s_nom', x: DATA.snapshots,
      y: new Array(flows.length).fill(sn),
      line: {color: '#22c55e', width: 1, dash: 'dash'},
      hovertemplate: '+s_nom = %{y:.0f}<extra></extra>',
    },
    {
      type:'scatter', mode:'lines', name:'− s_nom', x: DATA.snapshots,
      y: new Array(flows.length).fill(-sn),
      line: {color: '#22c55e', width: 1, dash: 'dash'},
      hovertemplate: '−s_nom = %{y:.0f}<extra></extra>',
    },
  ], {
    ...COMMON_LAYOUT,
    yaxis: {...COMMON_LAYOUT.yaxis, title: 'Flow (MW)', zeroline: true},
    xaxis: {...COMMON_LAYOUT.xaxis, rangeslider: {visible:true, thickness:0.06}, type:'date'},
    showlegend: true, legend: {orientation: 'h', y: -0.18},
  }, COMMON_CONFIG);
}

// ── System congestion timeline ───────────────────────
function chartSystem() {
  Plotly.newPlot('chart-sys', [
    {type:'scatter', mode:'lines', name:'System max %', x: DATA.snapshots, y: DATA.system.max_pct,
     line: {color:'#c43a2c', width: 1}, hovertemplate: '%{x}<br>%{y:.1f}%<extra>system max</extra>'},
    {type:'scatter', mode:'lines', name:'System p95 %', x: DATA.snapshots, y: DATA.system.p95_pct,
     line: {color:'#f76b4f', width: 1}, hovertemplate: '%{x}<br>%{y:.1f}%<extra>p95</extra>'},
    {type:'scatter', mode:'lines', name:'# overloaded', x: DATA.snapshots, y: DATA.system.n_overloaded,
     yaxis: 'y2', line: {color:'#1a2332', width: 1},
     hovertemplate: '%{x}<br>%{y:d} lines<extra></extra>'},
  ], {
    ...COMMON_LAYOUT,
    yaxis: {...COMMON_LAYOUT.yaxis, title: 'Loading (%)'},
    yaxis2: {title: '# overloaded lines', overlaying: 'y', side: 'right',
             gridcolor: 'transparent', showgrid: false},
    xaxis: {...COMMON_LAYOUT.xaxis, rangeslider: {visible:true, thickness:0.06}, type:'date'},
    showlegend: true, legend: {orientation: 'h', y: -0.18},
  }, COMMON_CONFIG);
}
function chartOvHist() {
  // Hours bucketed by # overloaded lines
  const buckets = {};
  for (const n of DATA.system.n_overloaded) {
    const b = n === 0 ? '0' : n < 5 ? '1-4' : n < 25 ? '5-24' : n < 100 ? '25-99' : n < 500 ? '100-499' : '≥500';
    buckets[b] = (buckets[b] || 0) + 1;
  }
  const order = ['0','1-4','5-24','25-99','100-499','≥500'];
  const labels = order.filter(o => buckets[o]);
  const vals = labels.map(l => buckets[l]);
  Plotly.newPlot('chart-ovh', [{
    type: 'bar', x: labels, y: vals,
    marker: {color: ['#22c55e','#a3e635','#facc15','#f76b4f','#dc2626','#7c1d1d'].slice(0,labels.length)},
    text: vals.map(v => v.toLocaleString()),
    textposition: 'outside',
    hovertemplate: '%{y:,d} hours<extra>%{x} overloaded lines</extra>',
  }], {
    ...COMMON_LAYOUT,
    yaxis: {...COMMON_LAYOUT.yaxis, title: 'Hours'},
    xaxis: {...COMMON_LAYOUT.xaxis, title: 'Number of simultaneously overloaded lines'},
  }, COMMON_CONFIG);
}

// ── Populate region dropdowns from all_lines metadata ─────────
function uniqueSorted(arr) {
  return [...new Set(arr.filter(Boolean))].sort();
}
function fillSelect(id, values) {
  const sel = document.getElementById(id);
  if (!sel) return;
  for (const v of values) {
    const o = document.createElement('option');
    o.value = v; o.textContent = v;
    sel.appendChild(o);
  }
}
function populateRegionDropdowns() {
  if (!DATA.has_regions) return;
  const tsos = uniqueSorted(DATA.all_lines.map(l => l.tso));
  const states = uniqueSorted(DATA.all_lines.map(l => l.state));
  fillSelect('filter-tso', tsos);
  fillSelect('filter-state', states);
  fillSelect('topsheet-tso', tsos);
  fillSelect('topsheet-state', states);
}

// ── Top Lines Sheet (top 100, region-aware) ───────────────────
let topsheetState = {sortKey: 'maxp', sortDir: -1, tso:'all', state:'all', v:'all', cross:false};
function applyTopsheetFilters() {
  let rows = DATA.top100 || [];
  if (topsheetState.tso !== 'all') rows = rows.filter(l => l.tso === topsheetState.tso);
  if (topsheetState.state !== 'all') rows = rows.filter(l => l.state === topsheetState.state);
  if (topsheetState.v !== 'all') rows = rows.filter(l => l.v == topsheetState.v);
  if (topsheetState.cross) rows = rows.filter(l => l.cross);
  rows = [...rows].sort((a,b) => {
    const va = a[topsheetState.sortKey], vb = b[topsheetState.sortKey];
    if (typeof va === 'string' && typeof vb === 'string')
      return topsheetState.sortDir * va.localeCompare(vb);
    return topsheetState.sortDir * ((+va||0) - (+vb||0));
  });
  return rows;
}
function renderTopsheet() {
  const rows = applyTopsheetFilters();
  const tbody = document.getElementById('topsheet-body');
  tbody.innerHTML = '';
  for (const l of rows) {
    const ovCls = l.maxp > 200 ? 'crit' : l.maxp > 100 ? 'warn' : '';
    const stateLabel = (l.state || '—') + (l.cross ? ' →' : '');
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${escapeHTML(l.id)}</td>
      <td>${l.v}</td>
      <td>${escapeHTML(stateLabel)}</td>
      <td>${escapeHTML(l.kreis || '—')}</td>
      <td>${escapeHTML(l.tso || '—')}</td>
      <td>${escapeHTML(l.cross ? (l.state1 || '—') : '—')}</td>
      <td>${l.len.toLocaleString()}</td>
      <td>${l.snom.toLocaleString()}</td>
      <td>${l.max.toLocaleString()}</td>
      <td class="${ovCls}">${l.maxp.toFixed(1)}</td>
      <td>${l.meanp.toFixed(1)}</td>
      <td class="${l.ov_h>0?'warn':''}">${l.ov_h.toLocaleString()}</td>
      <td>${l.ph}</td>`;
    tbody.appendChild(tr);
  }
  // sort indicators
  document.querySelectorAll('#topsheet-table thead th').forEach(th => {
    th.classList.remove('sort-asc','sort-desc');
    if (th.dataset.k === topsheetState.sortKey)
      th.classList.add(topsheetState.sortDir > 0 ? 'sort-asc' : 'sort-desc');
  });
  document.getElementById('topsheet-region-note').textContent =
    DATA.has_regions ? `Filtered to ${rows.length} of ${DATA.top100.length}.`
                     : 'Region info unavailable (DB join failed) — region columns will be blank.';
}
function setupTopsheet() {
  document.querySelectorAll('#topsheet-table thead th').forEach(th => {
    th.addEventListener('click', () => {
      const k = th.dataset.k;
      if (!k) return;
      if (topsheetState.sortKey === k) topsheetState.sortDir *= -1;
      else { topsheetState.sortKey = k; topsheetState.sortDir = -1; }
      renderTopsheet();
    });
  });
  document.getElementById('topsheet-tso').addEventListener('change', e => {
    topsheetState.tso = e.target.value; renderTopsheet();
  });
  document.getElementById('topsheet-state').addEventListener('change', e => {
    topsheetState.state = e.target.value; renderTopsheet();
  });
  document.getElementById('topsheet-v').addEventListener('change', e => {
    topsheetState.v = e.target.value; renderTopsheet();
  });
  document.getElementById('topsheet-cross').addEventListener('change', e => {
    topsheetState.cross = e.target.checked; renderTopsheet();
  });
  document.getElementById('topsheet-clear').addEventListener('click', () => {
    topsheetState = {sortKey:'maxp', sortDir:-1, tso:'all', state:'all', v:'all', cross:false};
    document.getElementById('topsheet-tso').value = 'all';
    document.getElementById('topsheet-state').value = 'all';
    document.getElementById('topsheet-v').value = 'all';
    document.getElementById('topsheet-cross').checked = false;
    renderTopsheet();
  });
}

// ── By Region/TSO charts ──────────────────────────────────────
function chartByTSO() {
  const summ = (DATA.region_summary || {}).by_tso || {};
  const entries = Object.entries(summ).sort((a,b) => (b[1].mean_max_pct||0) - (a[1].mean_max_pct||0));
  if (!entries.length) {
    document.getElementById('chart-by-tso').innerHTML =
      '<p class="note" style="padding:24px">Region info not available — DB spatial-join failed.</p>';
    return;
  }
  Plotly.newPlot('chart-by-tso', [
    {type:'bar', name:'Total lines',
     x: entries.map(([t,_]) => t), y: entries.map(([_,d]) => d.n_lines),
     marker:{color:'#cbd5e1'}, text: entries.map(([_,d]) => d.n_lines.toLocaleString()), textposition:'outside'},
    {type:'bar', name:'Overloaded (max > 100%)',
     x: entries.map(([t,_]) => t), y: entries.map(([_,d]) => d.n_overloaded),
     marker:{color:'#dc2626'}, text: entries.map(([_,d]) => d.n_overloaded.toLocaleString()), textposition:'outside'},
  ], {
    ...COMMON_LAYOUT, barmode: 'group',
    yaxis: {...COMMON_LAYOUT.yaxis, title: 'Number of lines'},
    legend: {orientation:'h', y:-0.18},
  }, COMMON_CONFIG);
}
function chartByState() {
  const summ = (DATA.region_summary || {}).by_state || {};
  const entries = Object.entries(summ).sort((a,b) => (b[1].n_overloaded||0) - (a[1].n_overloaded||0));
  if (!entries.length) {
    document.getElementById('chart-by-state').innerHTML = '';
    return;
  }
  Plotly.newPlot('chart-by-state', [
    {type:'bar', name:'Total lines',
     x: entries.map(([s,_]) => s), y: entries.map(([_,d]) => d.n_lines),
     marker:{color:'#cbd5e1'}, hovertemplate:'%{x}<br>%{y:,d} lines<extra></extra>'},
    {type:'bar', name:'Overloaded (max > 100%)',
     x: entries.map(([s,_]) => s), y: entries.map(([_,d]) => d.n_overloaded),
     marker:{color:'#dc2626'}, hovertemplate:'%{x}<br>%{y:,d} overloaded<extra></extra>'},
    {type:'scatter', mode:'markers', name:'Mean max loading %',
     x: entries.map(([s,_]) => s), y: entries.map(([_,d]) => d.mean_max_pct),
     yaxis:'y2', marker:{color:'#1a2332', size:9},
     hovertemplate:'%{x}<br>mean max %{y:.1f}%<extra></extra>'},
  ], {
    ...COMMON_LAYOUT, barmode: 'group',
    yaxis: {...COMMON_LAYOUT.yaxis, title: 'Number of lines'},
    yaxis2: {title: 'Mean max loading %', overlaying:'y', side:'right',
             gridcolor:'transparent', showgrid:false},
    legend: {orientation:'h', y:-0.22},
  }, COMMON_CONFIG);
}

// ── All lines table (filterable + sortable + paginated) ─────
const PAGE = 200;
let allState = {sortKey: 'maxp', sortDir: -1, vFilter: 'all', tsoFilter: 'all', stateFilter: 'all',
                minPct: '', idSearch: '', page: 1};
function applyFilters() {
  let rows = DATA.all_lines;
  if (allState.vFilter !== 'all') rows = rows.filter(l => l.v == allState.vFilter);
  if (allState.tsoFilter !== 'all') rows = rows.filter(l => l.tso === allState.tsoFilter);
  if (allState.stateFilter !== 'all') rows = rows.filter(l => l.state === allState.stateFilter);
  if (allState.minPct !== '' && !isNaN(parseFloat(allState.minPct)))
    rows = rows.filter(l => l.maxp >= parseFloat(allState.minPct));
  if (allState.idSearch) {
    const q = allState.idSearch.toLowerCase();
    rows = rows.filter(l => l.id.toLowerCase().includes(q));
  }
  rows = [...rows].sort((a,b) => {
    const va = a[allState.sortKey], vb = b[allState.sortKey];
    if (typeof va === 'string' && typeof vb === 'string') return allState.sortDir * va.localeCompare(vb);
    return allState.sortDir * ((+va||0) - (+vb||0));
  });
  return rows;
}
function renderAllTable() {
  const rows = applyFilters();
  const total = rows.length;
  const pages = Math.max(1, Math.ceil(total / PAGE));
  if (allState.page > pages) allState.page = pages;
  const pageRows = rows.slice((allState.page-1)*PAGE, allState.page*PAGE);

  const tbody = document.getElementById('all-body');
  tbody.innerHTML = '';
  for (const l of pageRows) {
    const tr = document.createElement('tr');
    const ovCls = l.maxp > 200 ? 'crit' : l.maxp > 100 ? 'warn' : '';
    tr.innerHTML = `
      <td>${escapeHTML(l.id)}</td>
      <td>${l.v}</td>
      <td>${escapeHTML(l.state || '—')}</td>
      <td>${escapeHTML(l.tso || '—')}</td>
      <td>${l.len.toLocaleString()}</td>
      <td>${l.snom.toLocaleString()}</td>
      <td>${l.mean.toLocaleString()}</td>
      <td>${l.p99.toLocaleString()}</td>
      <td>${l.max.toLocaleString()}</td>
      <td>${l.meanp.toFixed(1)}</td>
      <td class="${ovCls}">${l.maxp.toFixed(1)}</td>
      <td class="${l.ov_h>0?'warn':''}">${l.ov_h.toLocaleString()}</td>`;
    tbody.appendChild(tr);
  }
  document.getElementById('all-count').textContent = total.toLocaleString();
  document.getElementById('all-total').textContent = DATA.all_lines.length.toLocaleString();
  document.getElementById('all-page').textContent = allState.page;
  document.getElementById('all-pageinfo').textContent =
    `  page ${allState.page} of ${pages} · ${total.toLocaleString()} matches`;

  // sort indicators
  document.querySelectorAll('#all-table thead th').forEach(th => {
    th.classList.remove('sort-asc','sort-desc');
    if (th.dataset.k === allState.sortKey)
      th.classList.add(allState.sortDir > 0 ? 'sort-asc' : 'sort-desc');
  });
}
function setupAllTable() {
  document.querySelectorAll('#all-table thead th').forEach(th => {
    th.addEventListener('click', () => {
      const k = th.dataset.k;
      if (!k) return;
      if (allState.sortKey === k) allState.sortDir *= -1;
      else { allState.sortKey = k; allState.sortDir = -1; }
      allState.page = 1;
      renderAllTable();
    });
  });
  document.getElementById('filter-v').addEventListener('change', e => {
    allState.vFilter = e.target.value; allState.page = 1; renderAllTable();
  });
  const tsoEl = document.getElementById('filter-tso');
  if (tsoEl) tsoEl.addEventListener('change', e => {
    allState.tsoFilter = e.target.value; allState.page = 1; renderAllTable();
  });
  const stEl = document.getElementById('filter-state');
  if (stEl) stEl.addEventListener('change', e => {
    allState.stateFilter = e.target.value; allState.page = 1; renderAllTable();
  });
  document.getElementById('filter-min').addEventListener('input', e => {
    allState.minPct = e.target.value; allState.page = 1; renderAllTable();
  });
  document.getElementById('filter-id').addEventListener('input', e => {
    allState.idSearch = e.target.value; allState.page = 1; renderAllTable();
  });
  document.getElementById('filter-clear').addEventListener('click', () => {
    allState = {sortKey:'maxp', sortDir:-1, vFilter:'all', tsoFilter:'all', stateFilter:'all',
                minPct:'', idSearch:'', page:1};
    document.getElementById('filter-v').value = 'all';
    if (tsoEl) tsoEl.value = 'all';
    if (stEl) stEl.value = 'all';
    document.getElementById('filter-min').value = '';
    document.getElementById('filter-id').value = '';
    renderAllTable();
  });
  document.getElementById('all-prev').addEventListener('click', () => {
    if (allState.page > 1) { allState.page--; renderAllTable(); }
  });
  document.getElementById('all-next').addEventListener('click', () => {
    allState.page++; renderAllTable();
  });
}

// Defensive boot: every step in its own try so one failure doesn't kill the
// rest. Tabs go FIRST so the navigation works even if Plotly is blocked or
// any chart helper throws. Errors are surfaced in-page (top banner) so the
// user can see what went wrong without opening devtools.
function showError(msg) {
  let bar = document.getElementById('boot-errors');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'boot-errors';
    bar.style.cssText = 'position:sticky;top:0;z-index:100;background:#fee2e2;color:#991b1b;'
      + 'padding:10px 14px;border-bottom:1px solid #fca5a5;font-family:monospace;font-size:0.85rem;'
      + 'white-space:pre-wrap;';
    document.body.insertBefore(bar, document.body.firstChild);
  }
  bar.textContent += (bar.textContent ? '\n' : '') + msg;
  console.error(msg);
}
function safe(label, fn) {
  try { fn(); }
  catch (e) { showError(`[${label}] ${e && (e.message || e)}\n${e && e.stack || ''}`); }
}
function boot() {
  if (typeof Plotly === 'undefined') {
    showError('Plotly failed to load (CDN blocked or offline). Tabs and tables will still work; charts will be empty.');
    window.Plotly = { newPlot: () => {} };  // no-op so chart calls don't blow up
  }
  // Tabs FIRST — must work even if everything else fails
  safe('setupTabs',     setupTabs);
  safe('renderKPIs',    renderKPIs);
  safe('regionDropdowns', populateRegionDropdowns);
  safe('setupTopsheet', setupTopsheet);
  safe('renderTopsheet', renderTopsheet);
  safe('setupAllTable', setupAllTable);
  safe('renderAllTable', renderAllTable);
  safe('setupLinePicker', setupLinePicker);
  safe('setupMapFilters', setupMapFilters);
  // Charts last, they're the most likely to break things
  safe('chartHist',     chartHist);
  safe('chartByVoltage', chartByVoltage);
  safe('chartByTSO',    chartByTSO);
  safe('chartByState',  chartByState);
  // chartMap deferred — initialises Leaflet only when the Map tab is opened
  safe('chartTopBar',   chartTopBar);
  safe('chartSystem',   chartSystem);
  safe('chartOvHist',   chartOvHist);
  // Confirmation: page initialised. Removed if user dismisses.
  console.log('[report] boot complete');
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();    // already loaded
}
</script>
</body></html>
"""


def next_versioned_path(prefix=DEFAULT_PREFIX, results_dir=RESULTS_DIR):
    """Return results/{prefix}{N+1}.html where N is the highest existing version."""
    import re
    pat = re.compile(rf"^{re.escape(prefix)}(\d+)\.html$")
    highest = 0
    for f in results_dir.glob(f"{prefix}*.html"):
        m = pat.match(f.name)
        if m:
            highest = max(highest, int(m.group(1)))
    return results_dir / f"{prefix}{highest + 1}.html"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nc", default=str(DEFAULT_NC))
    parser.add_argument("--out", default=None,
                        help="Output path. If omitted, auto-versioned as "
                             "results/dispatch_pf_v{N+1}.html based on existing files.")
    parser.add_argument("--top", type=int, default=30, help="Top N lines to embed full hourly")
    args = parser.parse_args()
    if args.out is None:
        args.out = str(next_versioned_path())

    log.info(f"Loading {args.nc}")
    n = pypsa.Network(args.nc)
    log.info(f"  {len(n.snapshots)} snaps, {len(n.lines)} lines")
    if n.lines_t.p0 is None or n.lines_t.p0.empty:
        log.error("Network has no line flows. Run run_dcpf_8760h.py first.")
        return

    # Spatial-join regions (optional; report still works without)
    bus_regions = None
    try:
        bus_regions = load_bus_regions(list(n.buses.index.astype(str)))
    except Exception as e:
        log.warning(f"Could not load bus regions ({e}); report will omit region columns.")

    line_stats, abs_p = compute_per_line_stats(n, bus_regions=bus_regions)
    payload = build_payload(n, line_stats, abs_p, top_n=args.top)

    log.info("Serialising payload...")
    json_blob = json.dumps(payload, separators=(",", ":"), default=float)
    log.info(f"  payload size: {len(json_blob)/1e6:.1f} MB")

    html = HTML_TEMPLATE.replace("__DATA__", json_blob)
    out_path = Path(args.out)
    out_path.write_text(html, encoding="utf-8")
    size_mb = out_path.stat().st_size / 1e6
    log.info(f"Wrote {out_path}  ({size_mb:.1f} MB)")
    log.info(f"  → URL on local server: http://127.0.0.1:8765/{out_path.name}")
    log.info(f"  → SCP this file to your laptop: {out_path}")


if __name__ == "__main__":
    main()
