from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from ..db import SCN, get_engine
from ..services import county_agg

router = APIRouter()


@router.get("")
def list_loads(
    carrier: Optional[str] = Query(None),
    bbox: Optional[str] = Query(None),
    pmin: float = Query(0.0, description="Minimum static p_set in MW"),
    limit: int = Query(15000, ge=1, le=50000),
):
    """Return loads with bus coordinates as a GeoJSON FeatureCollection."""

    where = ["l.scn_name = :scn", "ABS(l.p_set) >= :pmin"]
    params: dict = {"scn": SCN, "pmin": pmin, "limit": limit}

    if carrier:
        carriers = [c.strip() for c in carrier.split(",") if c.strip()]
        where.append("l.carrier = ANY(:carriers)")
        params["carriers"] = carriers
    if bbox:
        try:
            x0, y0, x1, y1 = [float(v) for v in bbox.split(",")]
        except ValueError:
            raise HTTPException(400, "bbox must be 'minLon,minLat,maxLon,maxLat'")
        where.append("b.x BETWEEN :x0 AND :x1 AND b.y BETWEEN :y0 AND :y1")
        params.update({"x0": x0, "y0": y0, "x1": x1, "y1": y1})

    sql = text(f"""
        SELECT l.load_id, l.bus, l.carrier, l.p_set,
               b.x AS lon, b.y AS lat, b.v_nom, b.country
        FROM grid.egon_etrago_load l
        JOIN grid.egon_etrago_bus b
          ON l.bus = b.bus_id AND b.scn_name = l.scn_name
        WHERE {' AND '.join(where)}
        ORDER BY ABS(l.p_set) DESC NULLS LAST
        LIMIT :limit
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, params).mappings().all()

    features = []
    for r in rows:
        if r["lon"] is None or r["lat"] is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": {
                "id": int(r["load_id"]),
                "bus": int(r["bus"]),
                "carrier": r["carrier"],
                "p_set": float(r["p_set"]) if r["p_set"] is not None else 0.0,
                "v_nom": float(r["v_nom"]) if r["v_nom"] is not None else None,
                "country": r["country"],
            },
        })
    return {"type": "FeatureCollection", "features": features, "count": len(features)}


@router.get("/by-county")
def loads_by_county(force: bool = Query(False, description="Bypass cache")):
    return county_agg.loads_by_county(force=force)


@router.get("/by-county/{krs_id}")
def county_detail(krs_id: int):
    detail = county_agg.county_detail(krs_id)
    if detail is None:
        raise HTTPException(404, f"County {krs_id} not found")
    return detail


@router.get("/carriers")
def list_load_carriers():
    sql = text("""
        SELECT carrier, COUNT(*) AS n, ROUND(SUM(ABS(p_set))::numeric, 1) AS total_mw
        FROM grid.egon_etrago_load
        WHERE scn_name = :scn
        GROUP BY carrier
        ORDER BY total_mw DESC NULLS LAST
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"scn": SCN}).mappings().all()
    return [{"carrier": r["carrier"], "count": int(r["n"]),
             "total_mw": float(r["total_mw"] or 0)} for r in rows]
