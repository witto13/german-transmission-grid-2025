#!/usr/bin/env python3
"""audit_all_generation.py — Verify every generator + storage unit in grid_beta against
real-world plant data; flag voltage / location / capacity anomalies; output a prioritized
fix list. Output:
    results/audit_generation_all.csv         per-unit verdict (every flagged anomaly)
    results/audit_generation_summary.json    headline counts by carrier / issue
"""
import json
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa
from sqlalchemy import create_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

NC = "/root/egon_2025_project/results/dispatch_8760h_pf.nc"
DB_URL = "postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data"
OUT_DIR = Path("/root/egon_2025_project/results")

# Real DE pumped-hydro plants (BNetzA Kraftwerksliste + Wikipedia consolidated)
REAL_PH = [
    ("Goldisthal",           50.503, 11.012, 1060, 380),
    ("Markersbach",          50.519, 12.881, 1050, 380),
    ("Vianden (LU)",         49.927,  6.205, 1296, 380),
    ("Wehr",                 47.616,  7.927,  910, 380),
    ("Säckingen",            47.547,  7.967,  360, 220),
    ("Häusern",              47.788,  8.147,  132, 220),
    ("Schluchsee",           47.819,  8.166,  147, 220),
    ("Witznau",              47.749,  8.198,  220, 220),
    ("Erzhausen",            51.940,  9.917,  220, 220),
    ("Hohenwarte II",        50.625, 11.443,  320, 220),
    ("Hohenwarte I",         50.642, 11.434,   63, 110),
    ("Bleiloch",             50.498, 11.706,   80, 110),
    ("Geesthacht",           53.422, 10.388,  120, 110),
    ("Niederwartha",         51.072, 13.654,  120, 110),
    ("Waldeck I+II",         51.198,  9.054,  480, 380),
    ("Reisach-Tanzmühle",    49.552, 12.300,  105, 110),
    ("Glems",                48.495,  9.302,   90, 110),
    ("Langenprozelten",      50.058,  9.658,  164, 110),
    ("Happurg",              49.498, 11.494,  160, 110),
    ("Walchensee",           47.601, 11.323,  124, 110),
    ("Koepchenwerk Herdecke",51.398,  7.450,  153, 110),
    ("Rönkhausen",           51.324,  7.881,  140, 110),
    ("Wendefurth",           51.692, 10.957,   80, 110),
    ("Forbach",              48.676,  8.351,   65, 110),
    ("Leitzachwerke",        47.722, 11.846,   91, 110),
]
# Real DE large conventional plants (selected, >= ~200 MW)
REAL_CONV = [
    # (name, lat, lon, MW, family, kV)
    ("Niederaußem",           51.040,  6.668, 3864, "lignite",  380),
    ("Neurath",               51.038,  6.624, 4400, "lignite",  380),
    ("Weisweiler",            50.840,  6.318, 1851, "lignite",  380),
    ("Boxberg",               51.412, 14.566, 2667, "lignite",  380),
    ("Schwarze Pumpe",        51.534, 14.367, 1500, "lignite",  380),
    ("Lippendorf",            51.183, 12.354, 1840, "lignite",  380),
    ("Schkopau",              51.396, 12.001,  900, "lignite",  380),
    ("Jänschwalde",           51.836, 14.460, 3000, "lignite",  380),
    ("Buschhaus",             52.224, 10.949,  350, "lignite",  220),
    # hard coal (many decommissioned by 2025 but still in legacy data)
    ("Datteln 4",             51.659,  7.354, 1052, "hard_coal", 380),
    ("Mehrum",                52.319, 10.135,  690, "hard_coal", 380),
    ("Heyden 4",              52.379,  8.730,  920, "hard_coal", 380),
    ("Bexbach",               49.346,  7.245,  721, "hard_coal", 220),
    ("Heilbronn",             49.165,  9.181,  860, "hard_coal", 220),
    ("Mannheim GKM",          49.450,  8.475, 2050, "hard_coal", 220),
    ("Karlsruhe RDK",         49.014,  8.319, 1340, "hard_coal", 380),
    ("Walheim",               49.012,  9.183,  154, "hard_coal", 220),
    ("Bremen Hafen",          53.108,  8.770,  720, "hard_coal", 220),
    ("Wedel",                 53.581,  9.706,  273, "hard_coal", 110),
    ("Tiefstack (HH)",        53.521, 10.054,  184, "hard_coal", 110),
    ("Werdohl-Elverlingsen",  51.288,  7.755,  700, "hard_coal", 220),
    ("Westfalen E",           51.731,  7.953,  800, "hard_coal", 380),
    ("Bergkamen",             51.633,  7.620,  717, "hard_coal", 380),
    ("Knepper",               51.572,  7.395,  345, "hard_coal", 220),
    # gas
    ("Irsching 4/5",          48.846, 11.701, 1410, "gas",      380),
    ("Lausward (DUS)",        51.235,  6.731,  600, "gas",      380),
    ("Knapsack",              50.871,  6.832,  792, "gas",      220),
    ("Hamm-Uentrop",          51.692,  7.964,  860, "gas",      380),
    ("Mainz-Wiesbaden",       50.005,  8.301,  398, "gas",      220),
    ("Emsland-D",             52.471,  7.314,  863, "gas",      380),
    ("Block Mainz K6",        50.005,  8.301,  300, "gas",      220),
    ("Kiel-Ost CHP",          54.337, 10.157,  191, "gas",      110),
    # oil
    ("Ingolstadt 4",          48.717, 11.484,  446, "oil",      220),
]

# Voltage-allocation thresholds (rough, from BNetzA/DSO connection rules)
#   <100 kV (LV/MV)  : ≤ 10 MW typical
#   110 kV (HV)      : 10–200 MW typical, up to ~300 MW max single unit
#   220 kV / 380 kV  : > 200 MW typical
VOLT_RULE = {
    110: 250,      # warn if p_nom > 250 MW on a 110 kV bus
    220: 1000,     # warn > 1000 MW on a 220 kV bus
    380: 5000,     # warn > 5000 MW on a 380 kV bus (very rare)
}


def km(lat1, lon1, lat2, lon2):
    return math.hypot((lat1 - lat2) * 111, (lon1 - lon2) * 71)


def nearest(row, real_df, family=None):
    if family is not None and "carrier" in real_df.columns:
        real_df = real_df[real_df["carrier"] == family]
    if real_df.empty or not (np.isfinite(row.get("lat", np.nan))
                              and np.isfinite(row.get("lon", np.nan))):
        return None, None, None, None, 999
    dists = [(km(row["lat"], row["lon"], r["lat"], r["lon"]),
              r["name"], r["MW"], r["kV"]) for _, r in real_df.iterrows()]
    dists.sort()
    d, name, mw, kv = dists[0]
    return name, mw, kv, d, d


def main():
    log.info(f"Loading {NC}")
    n = pypsa.Network(NC)
    eng = create_engine(DB_URL)
    bs = pd.read_sql(
        "SELECT bus_id::text bus_id, ST_X(geom) lon, ST_Y(geom) lat, "
        "(SELECT gen FROM boundaries.vg250_lan WHERE ST_Contains(geometry, b.geom) AND gf=4 LIMIT 1) state "
        "FROM grid.egon_etrago_bus b WHERE scn_name='grid_beta'", eng
    ).drop_duplicates("bus_id").set_index("bus_id")

    # ---- per-bus evacuation capacity (sum of incident line + trafo s_nom) ----
    log.info("Computing per-bus evacuation capacity...")
    line_cap = (n.lines.groupby("bus0")["s_nom"].sum()
                + n.lines.groupby("bus1")["s_nom"].sum()).fillna(0)
    trafo_cap = (n.transformers.groupby("bus0")["s_nom"].sum()
                 + n.transformers.groupby("bus1")["s_nom"].sum()).fillna(0)
    bus_evac = line_cap.add(trafo_cap, fill_value=0).to_dict()

    # ---- gen total per bus (so we know shared evacuation) ----
    gen_per_bus = n.generators.groupby("bus")["p_nom"].sum().to_dict()
    stor_per_bus = n.storage_units.groupby("bus")["p_nom"].sum().to_dict()

    real_ph = pd.DataFrame(REAL_PH, columns=["name", "lat", "lon", "MW", "kV"])
    real_conv = pd.DataFrame(REAL_CONV, columns=["name", "lat", "lon", "MW", "carrier", "kV"])

    fam_map = {
        "lignite": "lignite", "coal": "hard_coal", "hard_coal": "hard_coal",
        "gas_ccgt": "gas", "gas_chp": "gas", "gas": "gas",
        "oil": "oil",
    }

    rows = []

    # ---- Storage units ----
    log.info("Auditing storage units...")
    su = n.storage_units.copy()
    su["v_nom"] = su["bus"].map(n.buses["v_nom"])
    su["lat"] = su["bus"].map(bs["lat"])
    su["lon"] = su["bus"].map(bs["lon"])
    su["state"] = su["bus"].map(bs["state"])
    for sid, r in su.iterrows():
        cls = []
        if r["carrier"] == "pumped_hydro":
            name, mw, kv, dist, _ = nearest(r, real_ph)
            if dist > 30:
                cls.append(f"GHOST (>{dist:.0f} km from any real pumped-hydro plant)")
            elif r["p_nom"] > 1.5 * mw:
                cls.append(f"AGGREGATION ({r['p_nom']/mw:.1f}x nearest real)")
            if not np.isnan(r["v_nom"]) and int(r["v_nom"]) != kv:
                cls.append(f"VOLT_MISMATCH (model {int(r['v_nom'])} vs real {kv})")
        # evacuation deficit (storage + gen on bus vs bus evac)
        bus_demand = stor_per_bus.get(r["bus"], 0) + gen_per_bus.get(r["bus"], 0)
        evac = bus_evac.get(r["bus"], 0)
        if evac > 0 and bus_demand > evac:
            cls.append(f"EVAC_DEFICIT (bus capacity {evac:.0f} MVA < gen+stor {bus_demand:.0f} MW)")
        # general voltage-allocation rule (p_nom too big for v_nom)
        vlim = VOLT_RULE.get(int(r["v_nom"]) if not np.isnan(r["v_nom"]) else 0)
        if vlim and r["p_nom"] > vlim:
            cls.append(f"OVERSIZED ({r['p_nom']:.0f} MW > {vlim} MW threshold for {int(r['v_nom'])} kV)")
        if cls:
            rows.append({
                "type": "storage", "id": sid, "carrier": r["carrier"], "bus": r["bus"],
                "v_nom": int(r["v_nom"]) if not np.isnan(r["v_nom"]) else 0,
                "p_nom_MW": round(r["p_nom"], 1), "state": r.get("state", ""),
                "lat": round(r["lat"], 3) if np.isfinite(r["lat"]) else None,
                "lon": round(r["lon"], 3) if np.isfinite(r["lon"]) else None,
                "bus_evac_MVA": round(bus_evac.get(r["bus"], 0)),
                "nearest_real": locals().get("name", "") if r["carrier"] == "pumped_hydro" else "",
                "real_MW": locals().get("mw", "") if r["carrier"] == "pumped_hydro" else "",
                "real_kV": locals().get("kv", "") if r["carrier"] == "pumped_hydro" else "",
                "dist_km": round(locals().get("dist", 0), 1) if r["carrier"] == "pumped_hydro" else "",
                "issues": "; ".join(cls),
            })

    # ---- Generators ----
    log.info("Auditing generators...")
    g = n.generators.copy()
    g["v_nom"] = g["bus"].map(n.buses["v_nom"])
    g["lat"] = g["bus"].map(bs["lat"])
    g["lon"] = g["bus"].map(bs["lon"])
    g["state"] = g["bus"].map(bs["state"])
    for gid, r in g.iterrows():
        cls = []
        # check against real plant list only for conventional & sizable units
        car = r["carrier"]
        if car in fam_map:
            fam = fam_map[car]
            if r["p_nom"] >= 150:  # only big enough to be a flagged plant
                name, mw, kv, dist, _ = nearest(r, real_conv, family=fam)
                if dist > 50:
                    cls.append(f"GHOST_CONV (>{dist:.0f} km from any real {fam} plant)")
                elif r["p_nom"] > 1.6 * mw:
                    cls.append(f"AGGREGATION_CONV ({r['p_nom']/mw:.1f}x nearest real {name})")
                if not np.isnan(r["v_nom"]) and int(r["v_nom"]) != kv:
                    cls.append(f"VOLT_MISMATCH (model {int(r['v_nom'])} vs real {name}'s {kv} kV)")
            else:
                name = mw = kv = dist = ""
        else:
            name = mw = kv = dist = ""
        # general voltage-allocation rule
        vlim = VOLT_RULE.get(int(r["v_nom"]) if not np.isnan(r["v_nom"]) else 0)
        if vlim and r["p_nom"] > vlim and r["carrier"] not in {"solar", "onwind", "offwind"}:
            cls.append(f"OVERSIZED ({r['p_nom']:.0f} MW > {vlim} MW threshold for {int(r['v_nom'])} kV)")
        if cls:
            rows.append({
                "type": "generator", "id": gid, "carrier": car, "bus": r["bus"],
                "v_nom": int(r["v_nom"]) if not np.isnan(r["v_nom"]) else 0,
                "p_nom_MW": round(r["p_nom"], 1), "state": r.get("state", ""),
                "lat": round(r["lat"], 3) if np.isfinite(r["lat"]) else None,
                "lon": round(r["lon"], 3) if np.isfinite(r["lon"]) else None,
                "bus_evac_MVA": round(bus_evac.get(r["bus"], 0)),
                "nearest_real": name, "real_MW": mw, "real_kV": kv,
                "dist_km": round(dist, 1) if isinstance(dist, (int, float)) and dist != "" else "",
                "issues": "; ".join(cls),
            })

    df = pd.DataFrame(rows)
    if df.empty:
        log.warning("No anomalies found.")
        return
    df = df.sort_values(["p_nom_MW"], ascending=False)
    out_csv = OUT_DIR / "audit_generation_all.csv"
    df.to_csv(out_csv, index=False)
    log.info(f"Wrote {len(df)} flagged entries → {out_csv}")

    # Summary by issue family + carrier
    def first_issue(s):
        return s.split(";")[0].split("(")[0].strip()
    df["issue_class"] = df["issues"].apply(first_issue)
    summ = df.groupby(["type", "carrier", "issue_class"]).agg(
        n=("id", "size"),
        total_MW=("p_nom_MW", "sum"),
    ).reset_index().sort_values("total_MW", ascending=False)
    summary_path = OUT_DIR / "audit_generation_summary.json"
    summary = {
        "total_flagged": int(len(df)),
        "total_flagged_MW": float(df["p_nom_MW"].sum()),
        "by_carrier": df.groupby("carrier")["p_nom_MW"].sum().round(0).to_dict(),
        "by_issue": df.groupby("issue_class")["p_nom_MW"].sum().round(0).to_dict(),
        "top10_flags": df.head(10)[["type","id","carrier","bus","v_nom","p_nom_MW","state","issues"]].to_dict(orient="records"),
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    log.info(f"Summary → {summary_path}")
    print("\n=== ISSUE SUMMARY ===")
    print(summ.to_string(index=False))
    print(f"\nTotal flagged: {len(df)} units, total {df['p_nom_MW'].sum()/1000:.1f} GW")
    print(f"\nTop 15 flagged by p_nom:")
    print(df.head(15)[["type","id","carrier","bus","v_nom","p_nom_MW","state","issues"]].to_string(index=False, max_colwidth=80))


if __name__ == "__main__":
    main()
