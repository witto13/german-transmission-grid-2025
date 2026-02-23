#!/usr/bin/env python3
"""Analyze orphan cases to determine the fix strategy."""
import pandas as pd, math
from sqlalchemy import create_engine, text

engine = create_engine("postgresql+psycopg2://egon:data@127.0.0.1:59734/egon-data")

o1 = pd.read_csv("results/v7_orphan_transformers.csv")
o2 = pd.read_csv("results/v7_orphan_transformers_batch2.csv")
orphan_ids = set(list(o1.trafo_id) + list(o2.trafo_id))
print(f"Orphan trafo IDs: {len(orphan_ids)}")

with engine.connect() as conn:
    v6t = pd.read_sql(text(
        "SELECT t.trafo_id, t.bus0, t.bus1, t.s_nom, t.x as t_x, t.r as t_r, "
        "b0.v_nom as v0, b0.x as x0, b0.y as y0, b1.v_nom as v1, b1.x as x1, b1.y as y1 "
        "FROM grid.egon_etrago_transformer t "
        "JOIN grid.egon_etrago_bus b0 ON t.bus0=b0.bus_id AND b0.scn_name='eGon2025v6' "
        "JOIN grid.egon_etrago_bus b1 ON t.bus1=b1.bus_id AND b1.scn_name='eGon2025v6' "
        "WHERE t.scn_name='eGon2025v6'"
    ), conn)
    v7_bus_ids = set(conn.execute(text(
        "SELECT bus_id FROM grid.egon_etrago_bus WHERE scn_name='eGon2025v7'"
    )).scalars())
    jao = pd.read_sql(text(
        "SELECT bus_id, v_nom, x, y FROM grid.egon_etrago_bus "
        "WHERE scn_name='eGon2025v7' AND bus_id >= 100000"
    ), conn)

ot = v6t[v6t.trafo_id.isin(orphan_ids)].copy()
hv_buses = {}
for _, r in ot.iterrows():
    if r.v0 == 110:
        hv_buses.setdefault(int(r.bus1), {"v_nom": int(r.v1), "x": r.x1, "y": r.y1, "trafos": []})
        hv_buses[int(r.bus1)]["trafos"].append(int(r.trafo_id))
    else:
        hv_buses.setdefault(int(r.bus0), {"v_nom": int(r.v0), "x": r.x0, "y": r.y0, "trafos": []})
        hv_buses[int(r.bus0)]["trafos"].append(int(r.trafo_id))
print(f"Unique HV buses: {len(hv_buses)}")

with engine.connect() as conn:
    hv_list = ",".join(str(b) for b in hv_buses.keys())
    v6_lines = pd.read_sql(text(
        "SELECT l.line_id, l.bus0, l.bus1, l.s_nom::float as s_nom, "
        "l.x::float as l_x, l.r::float as l_r, l.b::float as l_b, l.length, "
        "b0.v_nom as v0, b1.v_nom as v1 "
        "FROM grid.egon_etrago_line l "
        "JOIN grid.egon_etrago_bus b0 ON l.bus0=b0.bus_id AND b0.scn_name='eGon2025v6' "
        "JOIN grid.egon_etrago_bus b1 ON l.bus1=b1.bus_id AND b1.scn_name='eGon2025v6' "
        f"WHERE l.scn_name='eGon2025v6' AND (l.bus0 IN ({hv_list}) OR l.bus1 IN ({hv_list}))"
    ), conn)


def hav(lon1, lat1, lon2, lat2):
    R = 6371.0
    d1 = math.radians(lon2 - lon1)
    d2 = math.radians(lat2 - lat1)
    a = math.sin(d2/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d1/2)**2
    return R * 2 * math.asin(math.sqrt(a))


print(f"\n{'Bus':>8} {'V':>4} {'#T':>3} {'#L':>3} {'#Nv7':>5} {'NrSame':>7} {'NrAny':>7} {'AnyV':>5} {'Case':>24}")
print("-" * 80)
case_counts = {}

for bid, info in sorted(hv_buses.items()):
    v, bx, by = info["v_nom"], info["x"], info["y"]
    ls = v6_lines[(v6_lines.bus0 == bid) | (v6_lines.bus1 == bid)]

    nv7 = []
    for _, l in ls.iterrows():
        other = int(l.bus1) if int(l.bus0) == bid else int(l.bus0)
        if other in v7_bus_ids:
            nv7.append(other)

    sv = jao[jao.v_nom == v]
    if len(sv) > 0:
        ds = sv.apply(lambda r: hav(bx, by, r.x, r.y), axis=1)
        d_same = ds.min()
    else:
        d_same = 999

    da = jao.apply(lambda r: hav(bx, by, r.x, r.y), axis=1)
    nearest = jao.iloc[da.idxmin()]
    d_any = da.min()

    if len(nv7) > 0:
        case = "HAS_V7_NEIGHBOR"
    elif d_same < 80:
        case = f"LINE_TO_{v}kV"
    elif d_any < 80:
        case = f"TRAFO_TO_{int(nearest.v_nom)}kV"
    else:
        case = "TOO_FAR"

    case_counts[case] = case_counts.get(case, 0) + 1
    print(f"{bid:>8} {v:>4} {len(info['trafos']):>3} {len(ls):>3} {len(nv7):>5} "
          f"{d_same:>7.1f} {d_any:>7.1f} {int(nearest.v_nom):>5} {case:>24}")

print(f"\nSummary:")
for c, n in sorted(case_counts.items()):
    print(f"  {c}: {n}")
print(f"  TOTAL: {sum(case_counts.values())}")
