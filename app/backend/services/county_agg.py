"""Aggregate loads by German Landkreis (county).

Cached as JSON on first call so the spatial join is paid once.
"""

import json
import pathlib
from collections import defaultdict
from typing import Optional

from sqlalchemy import text

from ..db import SCN, get_engine

CACHE_DIR = pathlib.Path(__file__).resolve().parent.parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)


def _cache_path(name: str) -> pathlib.Path:
    return CACHE_DIR / f"{name}_{SCN}.json"


def loads_by_county(force: bool = False) -> dict:
    """Return GeoJSON FeatureCollection of Landkreise with peak/annual load."""
    cache = _cache_path("loads_by_county")
    if cache.exists() and not force:
        return json.loads(cache.read_text())

    engine = get_engine()
    with engine.connect() as conn:
        # Per (county, carrier) aggregates. Peak comes from the max-of-array;
        # annual energy from the sum-of-array. Loads without a timeseries fall
        # back to static p_set scaled by 8760.
        sql = text("""
            WITH bus_county AS (
                SELECT b.bus_id, k.id AS krs_id
                FROM grid.egon_etrago_bus b
                JOIN boundaries.vg250_krs k
                  ON ST_Contains(k.geometry,
                                 ST_SetSRID(ST_MakePoint(b.x, b.y), 4326))
                WHERE b.scn_name = :scn AND b.country = 'DE'
            ),
            load_ts AS (
                SELECT t.load_id,
                       (SELECT MAX(v) FROM unnest(t.p_set) AS v) AS peak_mw,
                       (SELECT SUM(v) FROM unnest(t.p_set) AS v) AS annual_mwh
                FROM grid.egon_etrago_load_timeseries t
                WHERE t.scn_name = :scn
            )
            SELECT k.id   AS krs_id,
                   k.gen  AS krs_name,
                   k.ags  AS ags,
                   k.nuts AS nuts,
                   l.carrier,
                   SUM(COALESCE(lt.peak_mw, ABS(l.p_set)))            AS peak_mw,
                   SUM(COALESCE(lt.annual_mwh, ABS(l.p_set) * 8760))  AS annual_mwh
            FROM grid.egon_etrago_load l
            JOIN bus_county bc ON l.bus = bc.bus_id
            JOIN boundaries.vg250_krs k ON k.id = bc.krs_id
            LEFT JOIN load_ts lt ON l.load_id = lt.load_id
            WHERE l.scn_name = :scn
            GROUP BY k.id, k.gen, k.ags, k.nuts, l.carrier
        """)
        carrier_rows = conn.execute(sql, {"scn": SCN}).mappings().all()

        # Simplified geometry per county.
        geom_sql = text("""
            SELECT id AS krs_id, gen, ags, nuts,
                   ST_AsGeoJSON(ST_Simplify(geometry, 0.005)) AS geom
            FROM boundaries.vg250_krs
        """)
        geom_rows = conn.execute(geom_sql).mappings().all()

    # Aggregate per county across carriers
    per_county: dict = defaultdict(lambda: {
        "name": None, "ags": None, "nuts": None,
        "peak_mw": 0.0, "annual_mwh": 0.0, "by_carrier_peak": {}
    })
    for r in carrier_rows:
        kid = int(r["krs_id"])
        c = per_county[kid]
        c["name"] = r["krs_name"]
        c["ags"] = r["ags"]
        c["nuts"] = r["nuts"]
        peak = float(r["peak_mw"] or 0)
        annual = float(r["annual_mwh"] or 0)
        c["peak_mw"] += peak
        c["annual_mwh"] += annual
        c["by_carrier_peak"][r["carrier"] or "unknown"] = round(peak, 1)

    features = []
    for r in geom_rows:
        kid = int(r["krs_id"])
        if not r["geom"]:
            continue
        agg = per_county.get(kid, {})
        peak_mw = round(agg.get("peak_mw", 0.0), 1)
        annual_gwh = round(agg.get("annual_mwh", 0.0) / 1000.0, 1)
        features.append({
            "type": "Feature",
            "geometry": json.loads(r["geom"]),
            "properties": {
                "krs_id": kid,
                "name": agg.get("name") or r["gen"],
                "ags": r["ags"],
                "nuts": r["nuts"],
                "peak_mw": peak_mw,
                "annual_gwh": annual_gwh,
                "by_carrier_peak": agg.get("by_carrier_peak", {}),
            },
        })

    fc = {"type": "FeatureCollection", "features": features, "count": len(features)}
    cache.write_text(json.dumps(fc))
    return fc


def county_detail(krs_id: int) -> Optional[dict]:
    """Return drill-down for a single county: top buses, full carrier breakdown."""
    sql = text("""
        WITH bus_in_krs AS (
            SELECT b.bus_id, b.x, b.y, b.v_nom
            FROM grid.egon_etrago_bus b
            JOIN boundaries.vg250_krs k
              ON ST_Contains(k.geometry, ST_SetSRID(ST_MakePoint(b.x, b.y), 4326))
            WHERE b.scn_name = :scn AND k.id = :kid
        )
        SELECT l.bus, l.carrier, l.p_set,
               b.v_nom, b.x AS lon, b.y AS lat,
               (SELECT MAX(v) FROM unnest(t.p_set) AS v) AS peak_mw,
               (SELECT SUM(v) FROM unnest(t.p_set) AS v) AS annual_mwh
        FROM grid.egon_etrago_load l
        JOIN bus_in_krs b ON l.bus = b.bus_id
        LEFT JOIN grid.egon_etrago_load_timeseries t
          ON t.load_id = l.load_id AND t.scn_name = l.scn_name
        WHERE l.scn_name = :scn
        ORDER BY peak_mw DESC NULLS LAST
        LIMIT 200
    """)
    with get_engine().connect() as conn:
        rows = conn.execute(sql, {"scn": SCN, "kid": krs_id}).mappings().all()
        meta = conn.execute(
            text("SELECT gen, ags, nuts FROM boundaries.vg250_krs WHERE id = :kid"),
            {"kid": krs_id},
        ).mappings().first()

    if not meta:
        return None

    by_bus: dict = {}
    by_carrier: dict = {}
    for r in rows:
        bus = int(r["bus"])
        peak = float(r["peak_mw"] or abs(r["p_set"] or 0))
        annual = float(r["annual_mwh"] or abs(r["p_set"] or 0) * 8760)
        by_bus.setdefault(bus, {"v_nom": float(r["v_nom"] or 0), "lon": float(r["lon"]),
                                "lat": float(r["lat"]), "peak_mw": 0.0, "annual_gwh": 0.0})
        by_bus[bus]["peak_mw"] += peak
        by_bus[bus]["annual_gwh"] += annual / 1000.0

        carrier = r["carrier"] or "unknown"
        by_carrier.setdefault(carrier, {"peak_mw": 0.0, "annual_gwh": 0.0})
        by_carrier[carrier]["peak_mw"] += peak
        by_carrier[carrier]["annual_gwh"] += annual / 1000.0

    top_buses = sorted(
        [{"bus": b, **info} for b, info in by_bus.items()],
        key=lambda x: x["peak_mw"], reverse=True,
    )[:10]
    for b in top_buses:
        b["peak_mw"] = round(b["peak_mw"], 1)
        b["annual_gwh"] = round(b["annual_gwh"], 1)

    carriers = [{"carrier": c, **{k: round(v, 1) for k, v in info.items()}}
                for c, info in sorted(by_carrier.items(), key=lambda kv: -kv[1]["peak_mw"])]

    return {
        "name": meta["gen"],
        "ags": meta["ags"],
        "nuts": meta["nuts"],
        "by_carrier": carriers,
        "top_buses": top_buses,
    }
