import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from ..db import SCN, get_engine
from ..services import parallel_lines

router = APIRouter()


def _parse_voltages(v_nom: Optional[str]) -> Optional[list[float]]:
    if not v_nom:
        return None
    try:
        return [float(v) for v in v_nom.split(",")]
    except ValueError:
        raise HTTPException(400, "v_nom must be comma-separated numbers")


def _parse_countries(country: Optional[str]) -> Optional[list[str]]:
    if not country:
        return None
    return [c.strip() for c in country.split(",") if c.strip()]


def _fetch_buses(conn, voltages, countries) -> list[dict]:
    where = ["scn_name = :scn"]
    params: dict = {"scn": SCN}
    if voltages:
        where.append("v_nom = ANY(:voltages)")
        params["voltages"] = voltages
    if countries:
        where.append("country = ANY(:countries)")
        params["countries"] = countries
    sql = text(f"""
        SELECT bus_id, v_nom, x, y, country, carrier
        FROM grid.egon_etrago_bus
        WHERE {' AND '.join(where)}
    """)
    rows = conn.execute(sql, params).mappings().all()
    return [
        {
            "bus_id": int(r["bus_id"]),
            "v_nom": float(r["v_nom"]) if r["v_nom"] is not None else None,
            "lon": float(r["x"]) if r["x"] is not None else None,
            "lat": float(r["y"]) if r["y"] is not None else None,
            "country": r["country"],
            "carrier": r["carrier"],
        }
        for r in rows
    ]


def _fetch_lines(conn, voltages, bus_ids: set[int]) -> list[dict]:
    where = ["scn_name = :scn"]
    params: dict = {"scn": SCN}
    if voltages:
        where.append("v_nom = ANY(:voltages)")
        params["voltages"] = voltages
    sql = text(f"""
        SELECT line_id, bus0, bus1, v_nom, s_nom, length, cables, num_parallel,
               x, r, ST_AsGeoJSON(topo) AS geom
        FROM grid.egon_etrago_line
        WHERE {' AND '.join(where)}
    """)
    rows = conn.execute(sql, params).mappings().all()
    out = []
    for r in rows:
        b0, b1 = int(r["bus0"]), int(r["bus1"])
        if bus_ids and (b0 not in bus_ids or b1 not in bus_ids):
            continue
        geom = json.loads(r["geom"]) if r["geom"] else None
        out.append({
            "line_id": int(r["line_id"]),
            "bus0": b0,
            "bus1": b1,
            "v_nom": float(r["v_nom"]) if r["v_nom"] is not None else None,
            "s_nom": float(r["s_nom"]) if r["s_nom"] is not None else None,
            "length": float(r["length"]) if r["length"] is not None else None,
            "cables": int(r["cables"]) if r["cables"] is not None else None,
            "num_parallel": float(r["num_parallel"]) if r["num_parallel"] is not None else None,
            "x": float(r["x"]) if r["x"] is not None else None,
            "r": float(r["r"]) if r["r"] is not None else None,
            "geom": geom,
        })
    return out


def _fetch_transformers(conn, bus_ids: set[int]) -> list[dict]:
    sql = text("""
        SELECT trafo_id, bus0, bus1, s_nom, x, r, tap_ratio, phase_shift
        FROM grid.egon_etrago_transformer
        WHERE scn_name = :scn
    """)
    rows = conn.execute(sql, {"scn": SCN}).mappings().all()
    out = []
    for r in rows:
        b0, b1 = int(r["bus0"]), int(r["bus1"])
        if bus_ids and (b0 not in bus_ids or b1 not in bus_ids):
            continue
        out.append({
            "trafo_id": int(r["trafo_id"]),
            "bus0": b0,
            "bus1": b1,
            "s_nom": float(r["s_nom"]) if r["s_nom"] is not None else None,
            "x": float(r["x"]) if r["x"] is not None else None,
            "r": float(r["r"]) if r["r"] is not None else None,
            "tap_ratio": float(r["tap_ratio"]) if r["tap_ratio"] is not None else None,
            "phase_shift": float(r["phase_shift"]) if r["phase_shift"] is not None else None,
        })
    return out


def _fetch_links(conn, bus_ids: set[int]) -> list[dict]:
    sql = text("""
        SELECT link_id, bus0, bus1, p_nom, efficiency, length, carrier
        FROM grid.egon_etrago_link
        WHERE scn_name = :scn
    """)
    rows = conn.execute(sql, {"scn": SCN}).mappings().all()
    out = []
    for r in rows:
        b0, b1 = int(r["bus0"]), int(r["bus1"])
        if bus_ids and (b0 not in bus_ids or b1 not in bus_ids):
            continue
        out.append({
            "link_id": int(r["link_id"]),
            "bus0": b0,
            "bus1": b1,
            "p_nom": float(r["p_nom"]) if r["p_nom"] is not None else None,
            "efficiency": float(r["efficiency"]) if r["efficiency"] is not None else None,
            "length": float(r["length"]) if r["length"] is not None else None,
            "carrier": r["carrier"],
        })
    return out


@router.get("")
def topology(
    v_nom: Optional[str] = Query(None, description="Comma-separated voltages, e.g. 110,220,380"),
    country: Optional[str] = Query("DE", description="Comma-separated ISO-2 codes; default DE"),
    include_geom: bool = Query(True, description="Include real OSM line geometry (heavy)"),
):
    """Return buses + lines + transformers + links, filtered by voltage/country."""
    voltages = _parse_voltages(v_nom)
    countries = _parse_countries(country)

    with get_engine().connect() as conn:
        buses = _fetch_buses(conn, voltages, countries)
        bus_ids = {b["bus_id"] for b in buses}
        lines = _fetch_lines(conn, voltages, bus_ids)
        trafos = _fetch_transformers(conn, bus_ids)
        links = _fetch_links(conn, bus_ids)

    lines = parallel_lines.annotate(lines)
    if not include_geom:
        for ln in lines:
            ln.pop("geom", None)

    return {
        "buses": buses,
        "lines": lines,
        "transformers": trafos,
        "links": links,
        "counts": {
            "buses": len(buses),
            "lines": len(lines),
            "transformers": len(trafos),
            "links": len(links),
        },
    }


@router.get("/enriched")
def enriched_topology(
    v_nom: Optional[str] = Query(None),
    country: Optional[str] = Query("DE"),
    include_geom: bool = Query(False, description="Default off for enriched view"),
):
    """Topology + per-bus capacity-by-carrier and total-load aggregates."""
    voltages = _parse_voltages(v_nom)
    countries = _parse_countries(country)

    with get_engine().connect() as conn:
        buses = _fetch_buses(conn, voltages, countries)
        bus_ids = {b["bus_id"] for b in buses}
        lines = _fetch_lines(conn, voltages, bus_ids)
        trafos = _fetch_transformers(conn, bus_ids)
        links = _fetch_links(conn, bus_ids)

        # Capacity-by-carrier per bus
        gen_sql = text("""
            SELECT bus, carrier, SUM(p_nom) AS p_nom_sum, COUNT(*) AS n
            FROM grid.egon_etrago_generator
            WHERE scn_name = :scn AND bus = ANY(:buses)
            GROUP BY bus, carrier
        """)
        gen_rows = conn.execute(gen_sql, {"scn": SCN, "buses": list(bus_ids)}).mappings().all()

        # Peak load per bus (use timeseries when available)
        load_sql = text("""
            SELECT l.bus, l.carrier,
                   SUM(COALESCE(
                       (SELECT MAX(v) FROM unnest(t.p_set) AS v),
                       ABS(l.p_set)
                   )) AS peak_mw
            FROM grid.egon_etrago_load l
            LEFT JOIN grid.egon_etrago_load_timeseries t
              ON t.load_id = l.load_id AND t.scn_name = l.scn_name
            WHERE l.scn_name = :scn AND l.bus = ANY(:buses)
            GROUP BY l.bus, l.carrier
        """)
        load_rows = conn.execute(load_sql, {"scn": SCN, "buses": list(bus_ids)}).mappings().all()

    cap_by_bus: dict = {}
    for r in gen_rows:
        bus = int(r["bus"])
        cap_by_bus.setdefault(bus, {"total_mw": 0.0, "by_carrier": {}})
        mw = float(r["p_nom_sum"] or 0)
        cap_by_bus[bus]["total_mw"] += mw
        cap_by_bus[bus]["by_carrier"][r["carrier"] or "unknown"] = round(mw, 1)

    load_by_bus: dict = {}
    for r in load_rows:
        bus = int(r["bus"])
        load_by_bus.setdefault(bus, {"total_peak_mw": 0.0, "by_carrier": {}})
        mw = float(r["peak_mw"] or 0)
        load_by_bus[bus]["total_peak_mw"] += mw
        load_by_bus[bus]["by_carrier"][r["carrier"] or "unknown"] = round(mw, 1)

    for b in buses:
        bid = b["bus_id"]
        cap = cap_by_bus.get(bid)
        ld = load_by_bus.get(bid)
        b["capacity"] = {
            "total_mw": round(cap["total_mw"], 1) if cap else 0.0,
            "by_carrier": cap["by_carrier"] if cap else {},
        }
        b["load"] = {
            "total_peak_mw": round(ld["total_peak_mw"], 1) if ld else 0.0,
            "by_carrier": ld["by_carrier"] if ld else {},
        }

    lines = parallel_lines.annotate(lines)
    if not include_geom:
        for ln in lines:
            ln.pop("geom", None)

    return {
        "buses": buses,
        "lines": lines,
        "transformers": trafos,
        "links": links,
        "counts": {
            "buses": len(buses),
            "lines": len(lines),
            "transformers": len(trafos),
            "links": len(links),
        },
    }


@router.get("/buses/{bus_id}")
def bus_detail(bus_id: int):
    sql = text("""
        SELECT b.*,
               (SELECT COUNT(*) FROM grid.egon_etrago_line
                  WHERE scn_name=:scn AND (bus0=b.bus_id OR bus1=b.bus_id)) AS line_degree,
               (SELECT COUNT(*) FROM grid.egon_etrago_transformer
                  WHERE scn_name=:scn AND (bus0=b.bus_id OR bus1=b.bus_id)) AS trafo_degree
        FROM grid.egon_etrago_bus b
        WHERE b.scn_name=:scn AND b.bus_id=:bid
    """)
    with get_engine().connect() as conn:
        row = conn.execute(sql, {"scn": SCN, "bid": bus_id}).mappings().first()
    if not row:
        raise HTTPException(404, f"Bus {bus_id} not found")
    import math
    out: dict = {}
    for k, v in row.items():
        if k in ("scn_name",):
            continue
        if v is None:
            out[k] = None
        elif isinstance(v, float):
            out[k] = None if (math.isinf(v) or math.isnan(v)) else v
        elif isinstance(v, (int, str, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    return out
