#!/usr/bin/env python3
"""regional_dispatch_check.py — Compare modeled gen/load by Bundesland to real 2024.

Computes annual TWh by state × carrier and by state for load, then prints a
side-by-side vs the BNetzA/BDEW/AGEB 2024 reference. Flags mis-allocations
worth investigating.

Output: results/regional_dispatch_check.csv + console summary.
"""
import logging
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa
from sqlalchemy import create_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger(__name__)

NC = "/root/egon_2025_project/results/dispatch_8760h.nc"
OUT_CSV = Path("/root/egon_2025_project/results/regional_dispatch_check.csv")
DB_URL = "postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data"

# Reference (BNetzA / BDEW / AGEB 2024 generation TWh by state; approximate)
# Sources cross-checked: BNetzA SMARD per-Bundesland & BDEW Strommarkt Daten 2024.
GEN_REF_TWH = {
    "Schleswig-Holstein":   {"onwind": 21.0, "offwind": 14.0, "solar": 2.5, "biogas": 2.0, "gas": 1.0},
    "Niedersachsen":        {"onwind": 22.0, "offwind": 11.0, "solar": 8.0, "biogas": 5.5, "gas": 4.5, "lignite": 0.0, "hard_coal": 2.0, "biomass": 1.0},
    "Bremen":               {"hard_coal": 2.0, "gas": 1.0},
    "Mecklenburg-Vorpommern":{"onwind": 7.5, "solar": 1.8, "biogas": 1.4},
    "Brandenburg":          {"onwind": 14.0, "solar": 7.0, "lignite": 17.0, "biogas": 1.4, "gas": 1.0},
    "Berlin":               {"gas": 2.0, "hard_coal": 1.0, "solar": 0.2},
    "Hamburg":              {"hard_coal": 4.0, "gas": 0.8},
    "Sachsen-Anhalt":       {"onwind": 11.0, "solar": 5.0, "lignite": 13.0, "biogas": 1.4, "biomass": 0.8},
    "Sachsen":              {"onwind": 3.0, "solar": 3.5, "lignite": 12.0, "gas": 0.5},
    "Thüringen":            {"onwind": 2.5, "solar": 2.5, "biogas": 0.6, "pumped_hydro": 0.0},
    "Hessen":               {"onwind": 3.0, "solar": 4.0, "biogas": 1.0, "gas": 4.0, "biomass": 0.5},
    "Nordrhein-Westfalen":  {"onwind": 12.0, "solar": 9.0, "hard_coal": 11.0, "lignite": 25.0, "gas": 27.0, "biogas": 2.0, "biomass": 1.5},
    "Rheinland-Pfalz":      {"onwind": 6.0, "solar": 4.0, "gas": 3.5, "biogas": 0.7},
    "Saarland":             {"hard_coal": 2.0, "gas": 1.5},
    "Bayern":               {"onwind": 4.0, "solar": 21.0, "biogas": 7.0, "gas": 5.0, "run_of_river": 7.5, "reservoir": 5.0, "biomass": 1.5},
    "Baden-Württemberg":    {"onwind": 2.0, "solar": 9.0, "biogas": 3.0, "gas": 6.0, "run_of_river": 4.0, "reservoir": 1.5, "hard_coal": 4.0},
}
# Reference electricity consumption by state TWh 2024 (BDEW Marktbeobachtung)
LOAD_REF_TWH = {
    "Nordrhein-Westfalen": 140.0, "Bayern": 80.0, "Baden-Württemberg": 75.0,
    "Niedersachsen": 65.0, "Hessen": 45.0, "Rheinland-Pfalz": 30.0,
    "Sachsen": 22.0, "Sachsen-Anhalt": 18.0, "Brandenburg": 18.0,
    "Schleswig-Holstein": 17.0, "Thüringen": 13.0, "Berlin": 13.0,
    "Hamburg": 14.0, "Mecklenburg-Vorpommern": 8.0, "Bremen": 7.0, "Saarland": 11.0,
}

# Map our carriers to reference categories
CARRIER_MAP = {
    "solar": "solar", "onwind": "onwind", "offwind": "offwind",
    "biogas": "biogas", "biomass": "biomass", "waste": "biomass",
    "gas_chp": "gas", "gas_ccgt": "gas", "gas": "gas",
    "coal": "hard_coal", "hard_coal": "hard_coal", "lignite": "lignite",
    "oil": "gas",  # collapse small oil into gas for ref comparison
    "run_of_river": "run_of_river", "reservoir": "reservoir",
    "pumped_hydro": "pumped_hydro",
    "other": "gas", "other_conventional": "gas", "hydrogen": "gas",
}


def main():
    log.info(f"Loading {NC}")
    n = pypsa.Network(NC)

    # Get bus → Bundesland (PostGIS)
    eng = create_engine(DB_URL)
    bus_states = pd.read_sql(
        """
        SELECT b.bus_id::text AS bus_id, l.gen AS state
        FROM grid.egon_etrago_bus b
        LEFT JOIN boundaries.vg250_lan l ON ST_Contains(l.geometry, b.geom) AND l.gf = 4
        WHERE b.scn_name='grid_beta'
        """, eng).drop_duplicates("bus_id").set_index("bus_id")["state"].fillna("Foreign").to_dict()

    # --- Generation by state × carrier ---
    log.info("Aggregating generator dispatch by state × carrier...")
    p = n.generators_t.p
    if p is None or p.empty:
        log.error("No generators_t.p in netCDF — exiting.")
        return
    gens = n.generators.loc[p.columns]
    gens["state"] = gens["bus"].map(bus_states).fillna("Foreign")
    gens["ref_carrier"] = gens["carrier"].map(CARRIER_MAP).fillna("other")
    annual_gen_mwh = p.sum(axis=0)                            # MWh per generator
    gens["annual_TWh"] = annual_gen_mwh.values / 1e6
    gen_by_sc = gens.groupby(["state", "ref_carrier"])["annual_TWh"].sum().unstack(fill_value=0)

    # --- Load by state ---
    log.info("Aggregating load by state...")
    pl = n.loads_t.p if (n.loads_t.p is not None and not n.loads_t.p.empty) else n.loads_t.p_set
    loads = n.loads.loc[pl.columns]
    loads["state"] = loads["bus"].map(bus_states).fillna("Foreign")
    load_annual_mwh = pl.sum(axis=0)
    loads["annual_TWh"] = load_annual_mwh.values / 1e6
    load_by_s = loads.groupby("state")["annual_TWh"].sum()

    # --- Build comparison table ---
    rows = []
    states_ordered = list(LOAD_REF_TWH.keys())
    for st in states_ordered:
        # Load
        m_load = float(load_by_s.get(st, 0))
        r_load = float(LOAD_REF_TWH.get(st, 0))
        rows.append({"state": st, "category": "LOAD_total_TWh",
                     "modeled": round(m_load, 1), "reference": round(r_load, 1),
                     "diff_TWh": round(m_load - r_load, 1),
                     "diff_pct": round((m_load - r_load) / max(r_load, 1) * 100, 1) if r_load else 0})
        # Generation per carrier
        ref_for_state = GEN_REF_TWH.get(st, {})
        for car in sorted(set(list(ref_for_state.keys()) + list(gen_by_sc.columns))):
            m = float(gen_by_sc.loc[st, car]) if (st in gen_by_sc.index and car in gen_by_sc.columns) else 0
            r = float(ref_for_state.get(car, 0))
            if abs(m) < 0.1 and r < 0.1:
                continue
            rows.append({"state": st, "category": f"GEN_{car}_TWh",
                         "modeled": round(m, 1), "reference": round(r, 1),
                         "diff_TWh": round(m - r, 1),
                         "diff_pct": round((m - r) / max(r, 1) * 100, 1) if r else 999})

    df = pd.DataFrame(rows)
    df.to_csv(OUT_CSV, index=False)
    log.info(f"Wrote {OUT_CSV}")

    # National totals
    print("\n=== NATIONAL TOTALS (TWh, modeled) ===")
    print(f"Total load: {load_by_s[load_by_s.index!='Foreign'].sum():.1f} TWh "
          f"(real DE 2024 ≈ 520 TWh)")
    nat_by_car = gens[gens.state!='Foreign'].groupby("ref_carrier")["annual_TWh"].sum().sort_values(ascending=False)
    print("\nDE gen by carrier:")
    print(nat_by_car.to_string())

    print("\n=== Mis-allocations (|diff|>5 TWh OR >50%) ===")
    bad = df[(df["diff_TWh"].abs() > 5) | (df["diff_pct"].abs() > 50)]
    if not bad.empty:
        print(bad[["state","category","modeled","reference","diff_TWh","diff_pct"]].to_string(index=False))
    else:
        print("(none above threshold)")


if __name__ == "__main__":
    main()
