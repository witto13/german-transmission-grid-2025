from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from ..db import SCN, get_engine

router = APIRouter()


@router.get("")
def list_generators(
    carrier: Optional[str] = Query(None, description="Comma-separated list (e.g., solar,onwind)"),
    v_nom: Optional[str] = Query(None, description="Comma-separated voltages (e.g., 110,220,380)"),
    pnom_min: float = Query(0.0, description="Minimum p_nom in MW"),
    pnom_max: Optional[float] = Query(None),
    country: Optional[str] = Query(None, description="ISO-2 country code (e.g., DE)"),
    bbox: Optional[str] = Query(None, description="minLon,minLat,maxLon,maxLat"),
    limit: int = Query(20000, ge=1, le=50000),
):
    """Return generators as a GeoJSON FeatureCollection."""

    where = ["g.scn_name = :scn"]
    params: dict = {"scn": SCN, "pnom_min": pnom_min, "limit": limit}

    if pnom_min > 0:
        where.append("g.p_nom >= :pnom_min")
    if pnom_max is not None:
        where.append("g.p_nom <= :pnom_max")
        params["pnom_max"] = pnom_max
    if carrier:
        carriers = [c.strip() for c in carrier.split(",") if c.strip()]
        where.append("g.carrier = ANY(:carriers)")
        params["carriers"] = carriers
    if v_nom:
        try:
            voltages = [float(v) for v in v_nom.split(",")]
        except ValueError:
            raise HTTPException(400, "v_nom must be comma-separated numbers")
        where.append("b.v_nom = ANY(:voltages)")
        params["voltages"] = voltages
    if country:
        where.append("b.country = :country")
        params["country"] = country
    if bbox:
        try:
            x0, y0, x1, y1 = [float(v) for v in bbox.split(",")]
        except ValueError:
            raise HTTPException(400, "bbox must be 'minLon,minLat,maxLon,maxLat'")
        where.append("b.x BETWEEN :x0 AND :x1 AND b.y BETWEEN :y0 AND :y1")
        params.update({"x0": x0, "y0": y0, "x1": x1, "y1": y1})

    sql = text(f"""
        SELECT g.generator_id, g.bus, g.carrier, g.p_nom, g.marginal_cost,
               g.efficiency, g.build_year, b.x AS lon, b.y AS lat,
               b.v_nom, b.country
        FROM grid.egon_etrago_generator g
        JOIN grid.egon_etrago_bus b
          ON g.bus = b.bus_id AND b.scn_name = g.scn_name
        WHERE {' AND '.join(where)}
        ORDER BY g.p_nom DESC NULLS LAST
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
                "id": int(r["generator_id"]),
                "bus": int(r["bus"]) if r["bus"] is not None else None,
                "carrier": r["carrier"],
                "p_nom": float(r["p_nom"]) if r["p_nom"] is not None else 0.0,
                "marginal_cost": float(r["marginal_cost"]) if r["marginal_cost"] is not None else None,
                "efficiency": float(r["efficiency"]) if r["efficiency"] is not None else None,
                "build_year": int(r["build_year"]) if r["build_year"] is not None else None,
                "v_nom": float(r["v_nom"]) if r["v_nom"] is not None else None,
                "country": r["country"],
            },
        })

    return {"type": "FeatureCollection", "features": features, "count": len(features)}


@router.get("/carriers")
def list_carriers():
    """Return all distinct carriers with counts and total capacity."""
    sql = text("""
        SELECT carrier, COUNT(*) AS n, ROUND(SUM(p_nom)::numeric, 1) AS total_mw
        FROM grid.egon_etrago_generator
        WHERE scn_name = :scn
        GROUP BY carrier
        ORDER BY total_mw DESC NULLS LAST
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"scn": SCN}).mappings().all()
    return [{"carrier": r["carrier"], "count": int(r["n"]), "total_mw": float(r["total_mw"] or 0)} for r in rows]


@router.get("/{generator_id}")
def get_generator(generator_id: int):
    """Return full details for a single generator."""
    sql = text("""
        SELECT g.*, b.x AS lon, b.y AS lat, b.v_nom AS bus_v_nom, b.country
        FROM grid.egon_etrago_generator g
        JOIN grid.egon_etrago_bus b
          ON g.bus = b.bus_id AND b.scn_name = g.scn_name
        WHERE g.scn_name = :scn AND g.generator_id = :gid
    """)
    with get_engine().connect() as conn:
        row = conn.execute(sql, {"scn": SCN, "gid": generator_id}).mappings().first()

    if not row:
        raise HTTPException(404, f"Generator {generator_id} not found in {SCN}")

    return _clean_row(row, skip={"scn_name"})


def _clean_row(row, skip: set[str]) -> dict:
    """Convert SQLAlchemy row to JSON-safe dict (handles inf/nan/datetimes)."""
    import math

    out: dict = {}
    for k, v in row.items():
        if k in skip:
            continue
        if v is None:
            out[k] = None
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, float):
            out[k] = None if (math.isinf(v) or math.isnan(v)) else v
        elif isinstance(v, (int, str, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    return out
